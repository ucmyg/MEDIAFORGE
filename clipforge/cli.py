"""Typer CLI. Commands: add, run, review, publish, daemon, tick, auth, doctor, fixture, init-config.

Thin by design: every command maps arguments onto one module call and prints the result with rich. Expected errors
end in `typer.Exit` with a code (2 = usage, 1 = failure, 3 = not available in this phase); no tracebacks.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import typer
from rich.markup import escape
from rich.table import Table

from . import __version__
from . import ffmpeg as F
from .config import CONFIG_ENV, Settings, example_yaml, get_settings
from .db import DB
from .log import console, setup_logging

app = typer.Typer(name="clipforge", help="Long-form video -> captioned vertical clips -> YouTube Shorts / TikTok. Local and free.", no_args_is_help=True)

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_YET = 3
PHASE2_MESSAGE = "publishing arrives in Phase 2"
PHASE3_MESSAGE = "the scheduler arrives in Phase 3"
SKIP_WHISPER_ENV = "CLIPFORGE_TEST_SKIP_WHISPER"
WHISPER_DOWNLOAD_WAIT_S = 20.0
MIN_FREE_GB = 5.0
REQUIRED_FILTERS = ("subtitles", "overlay", "concat", "trim")
OPTIONAL_FILTERS = {"silencedetect": "--tighten", "loudnorm": "loudness normalisation"}
REQUIRED_ENCODERS = ("libx264", "aac")
STATUS_STYLE = {"OK": "green", "WARN": "yellow", "FAIL": "red", "INFO": "blue"}


class Layout(str, Enum):
    crop = "crop"
    blur = "blur"


# ---- helpers ------------------------------------------------------------------
def _fail(message: str, code: int = EXIT_FAILED) -> None:
    console.print(f"[red]error:[/] {escape(message)}")
    raise typer.Exit(code)


def _not_yet(message: str) -> None:
    console.print(escape(message))
    raise typer.Exit(EXIT_NOT_YET)


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj


def _open_db(settings: Settings) -> DB:
    return DB(settings.db_path)


def _passed(**options: Any) -> dict[str, Any]:
    """Only the options the user actually gave (None = not passed); enums become their values."""
    return {k: (v.value if isinstance(v, Enum) else v) for k, v in options.items() if v is not None}


def _platforms(to: str) -> list[str]:
    names = [p.strip().lower() for p in to.split(",") if p.strip()]
    if not names:
        _fail("--to needs at least one platform (youtube, tiktok)", EXIT_USAGE)
    return names


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"clipforge {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="clipforge.yaml to use (default: ./clipforge.yaml or $CLIPFORGE_CONFIG)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging on the console."),
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Print the version and exit."),
) -> None:
    if config is not None:
        if not config.is_file():
            _fail(f"config file not found: {config}", EXIT_USAGE)
        os.environ[CONFIG_ENV] = str(config)
    settings = get_settings(reload=True)
    # Non-UTF-8 stdout/stderr (Windows console or redirected output using cp1252) must never raise on a title or hook
    # with emoji/CJK: escape what the codec cannot encode instead. A no-op on UTF-8 streams; pythonw has no streams.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    setup_logging(settings.logs_dir, level=logging.DEBUG if verbose else logging.INFO)
    ctx.obj = settings


# ---- add ---------------------------------------------------------------------
@app.command()
def add(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="YouTube URL or local media file."),
    count: int | None = typer.Option(None, "--count", min=1, help="Clips to produce."),
    min_s: float | None = typer.Option(None, "--min", min=1.0, help="Minimum clip length in seconds."),
    max_s: float | None = typer.Option(None, "--max", min=1.0, help="Maximum clip length in seconds."),
    style: str | None = typer.Option(None, "--style", help="Caption preset (hormozi, clean, minimal or one from clipforge.yaml)."),
    layout: Layout | None = typer.Option(None, "--layout", help="9:16 layout: crop or blur."),
    tighten: bool | None = typer.Option(None, "--tighten/--no-tighten", help="Remove silences."),
    smart: bool | None = typer.Option(None, "--smart/--no-smart", help="Face-tracked crop."),
    punch: bool | None = typer.Option(None, "--punch/--no-punch", help="Punch-in zoom on hook words."),
    force_whisper: bool = typer.Option(False, "--force-whisper", help="Ignore captions and transcribe with whisper."),
    music: bool | None = typer.Option(None, "--music/--no-music", help="Mix a music bed from assets/music."),
) -> None:
    """Queue a video (URL or local file). Options given here are remembered for that video; never queues twice."""
    from .download import is_url, video_id_for

    settings = _settings(ctx)
    if style is not None:
        try:
            settings.style_cfg(style)
        except KeyError as e:
            _fail(str(e.args[0]), EXIT_USAGE)
    if min_s is not None and max_s is not None and min_s >= max_s:
        _fail(f"--min ({min_s:g}) must be smaller than --max ({max_s:g})", EXIT_USAGE)
    if is_url(source):
        stored = source.strip()
    else:
        local = Path(source).expanduser()
        if not local.is_file():
            _fail(f"file not found: {source}", EXIT_USAGE)
        stored = str(local.resolve())
    video_id, kind = video_id_for(stored)
    options = _passed(count=count, min_s=min_s, max_s=max_s, style=style, layout=layout, tighten=tighten, smart=smart, punch=punch, force_whisper=force_whisper or None, music=music)
    video, created = _open_db(settings).add_video(video_id, kind, stored, options=options)
    if created:
        console.print(f"queued {escape(video_id)}")
    else:
        console.print(f"already added {escape(video_id)} (status {escape(video.status)})")


# ---- run ---------------------------------------------------------------------
@app.command()
def run(
    ctx: typer.Context,
    video_id: str | None = typer.Option(None, "--video-id", help="Process only this video (any status)."),
    force: bool = typer.Option(False, "--force", help="Re-select and re-render (needs --video-id)."),
) -> None:
    """Process the queue: ingest -> transcribe -> select -> render -> metadata."""
    from .pipeline import process_queue, process_video

    settings = _settings(ctx)
    db = _open_db(settings)
    if force and not video_id:
        _fail("--force needs --video-id", EXIT_USAGE)
    reports = [process_video(video_id, settings, db, force=force)] if video_id else process_queue(settings, db)
    if not reports:
        console.print("nothing to do: no queued videos (clipforge add <url|file>)")
        return
    _print_run_summary(reports)
    if all(r.status == "failed" for r in reports):
        raise typer.Exit(EXIT_FAILED)


def _print_run_summary(reports: list) -> None:
    from .review import clip_length_s, mmss

    table = Table(title="run summary", expand=False)
    for name, kw in (("clip id", {"no_wrap": True}), ("status", {"no_wrap": True}), ("score", {"justify": "right"}), ("length", {"justify": "right"}), ("hook", {"overflow": "fold"}), ("path", {"overflow": "fold"})):
        table.add_column(name, **kw)
    for r in reports:
        for c in r.clips:
            table.add_row(escape(c.id), escape(c.status), f"{c.score:.2f}", mmss(clip_length_s(c)), escape(c.hook), escape(c.path or c.error or ""))
    console.print(table)
    for r in reports:
        colour = "green" if r.status == "done" else "red"
        detail = f": {escape(r.error)}" if r.error else ""
        console.print(f"video {escape(r.video_id)}: [{colour}]{escape(r.status)}[/]{detail}")


# ---- review ------------------------------------------------------------------
@app.command()
def review(
    ctx: typer.Context,
    video_id: str | None = typer.Option(None, "--video-id", help="Only clips of this video."),
    html: Path | None = typer.Option(None, "--html", help="Where to write the review page (default workspace/review.html)."),
    apply: Path | None = typer.Option(None, "--apply", help="Apply a decisions.json exported from the review page."),
    approve_all: bool = typer.Option(False, "--approve-all", help="Mark every rendered clip ready."),
    no_html: bool = typer.Option(False, "--no-html", help="Do not write the review page."),
) -> None:
    """Show rendered clips, write review.html with previews, apply decisions."""
    from . import review as RV

    settings = _settings(ctx)
    db = _open_db(settings)
    if apply is not None:
        if not apply.is_file():
            _fail(f"decisions file not found: {apply}", EXIT_USAGE)
        try:
            approved, rejected = RV.apply_decisions(db, apply)
        except ValueError as e:
            _fail(f"{apply}: {e}", EXIT_USAGE)
        console.print(f"applied {escape(str(apply))}: {approved} approved, {rejected} rejected")
    if approve_all:
        rendered = db.list_clips(video_id, "rendered")
        for clip in rendered:
            db.set_clip_status(clip.id, "ready")
        db.log("review.approve_all", f"{len(rendered)} clip(s)" + (f" of {video_id}" if video_id else ""))
        console.print(f"{len(rendered)} clip(s) marked ready")
    RV.print_review_table(db, console, video_id)
    if not no_html:
        out = RV.write_review_html(db, html or settings.workspace_dir / "review.html", video_id)
        console.print(f"review page: {escape(str(out))}")


# ---- publish / auth (Phase 2) ------------------------------------------------
@app.command()
def publish(
    ctx: typer.Context,
    to: str = typer.Option("youtube", "--to", help="Comma-separated platforms: youtube,tiktok."),
    now: bool = typer.Option(False, "--now/--schedule", help="Post now, or leave ready clips for the scheduler."),
    manual: bool = typer.Option(False, "--manual", help="Clipboard + browser upload page, no API credentials."),
    clip_id: str | None = typer.Option(None, "--clip-id", help="Post only this clip."),
    accept_risk: bool = typer.Option(False, "--i-accept-the-risk", help="Enable browser automation (opt-in; may breach platform terms)."),
) -> None:
    """Post ready clips to YouTube Shorts and/or TikTok."""
    from . import publish as P

    settings = _settings(ctx)
    db = _open_db(settings)
    platforms = _platforms(to)
    ready = [db.get_clip(clip_id)] if clip_id else db.list_clips(status="ready")
    if clip_id and ready[0] is None:
        _fail(f"unknown clip {clip_id}", EXIT_USAGE)
    if not now:
        console.print(f"{len(ready)} ready clip(s) left for the scheduler (clipforge daemon / tick) on {', '.join(platforms)}")
        return
    try:
        for name in platforms:
            publisher = P.get_publisher(name, settings, db, manual=manual, browser=accept_risk)
            publisher.auth()
            publisher.limits()
    except ValueError as e:
        _fail(str(e), EXIT_USAGE)
    except (NotImplementedError, ImportError):
        _not_yet(PHASE2_MESSAGE)


@app.command()
def auth(ctx: typer.Context, platform: str = typer.Argument(..., help="youtube or tiktok.")) -> None:
    """Interactive authentication for a platform; tokens are saved in the workspace."""
    from . import publish as P

    settings = _settings(ctx)
    try:
        ok = P.get_publisher(platform.lower(), settings, _open_db(settings)).auth()
    except ValueError as e:
        _fail(str(e), EXIT_USAGE)
    except NotImplementedError:
        _not_yet(PHASE2_MESSAGE)
    if not ok:
        _fail(f"{platform}: authentication failed")
    console.print(f"{escape(platform)}: authenticated")


# ---- daemon / tick (Phase 3) -------------------------------------------------
@app.command()
def tick(ctx: typer.Context, dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be posted without posting.")) -> None:
    """One scheduler pass (for cron / Task Scheduler): process the queue, then post what is due."""
    from . import scheduler

    settings = _settings(ctx)
    try:
        result = scheduler.tick(settings, _open_db(settings), dry_run=dry_run)
    except NotImplementedError:
        _not_yet(PHASE3_MESSAGE)
    console.print(escape(str(result)))


@app.command()
def daemon(ctx: typer.Context) -> None:
    """Scheduler loop: tick every schedule.tick_s seconds until interrupted."""
    from . import scheduler

    settings = _settings(ctx)
    try:
        scheduler.daemon(settings, _open_db(settings))
    except NotImplementedError:
        _not_yet(PHASE3_MESSAGE)


# ---- doctor ------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL | INFO
    detail: str


def _guarded(name: str, fn: Callable[[], list[Check]]) -> list[Check]:
    """Run one check group; an unexpected exception becomes a FAIL row instead of a traceback."""
    try:
        return fn()
    except Exception as e:
        return [Check(name, "FAIL", f"{type(e).__name__}: {e}")]


def _check_python() -> list[Check]:
    ok = sys.version_info >= (3, 11)
    return [Check("python", "OK" if ok else "FAIL", f"{sys.version.split()[0]} ({sys.executable})" + ("" if ok else "; 3.11+ required"))]


def _check_ffmpeg() -> list[Check]:
    checks = [Check("ffmpeg", "OK", f"{F.version()} at {F.ffmpeg_exe()}")]
    probe = F.ffprobe_exe()
    checks.append(Check("ffprobe", "OK", probe) if probe else Check("ffprobe", "WARN", "not found: using ffmpeg -i fallback"))
    available = F.filters()
    for name in REQUIRED_FILTERS:
        checks.append(Check(f"filter {name}", "OK" if name in available else "FAIL", "present" if name in available else "missing: rendering cannot work with this ffmpeg build"))
    for name, feature in OPTIONAL_FILTERS.items():
        checks.append(Check(f"filter {name}", "OK" if name in available else "WARN", "present" if name in available else f"missing: {feature} unavailable"))
    for name in REQUIRED_ENCODERS:
        checks.append(Check(f"encoder {name}", "OK" if F.has_encoder(name) else "FAIL", "present" if F.has_encoder(name) else "missing: needed for mp4 output"))
    nvenc = F.nvenc_available()
    checks.append(Check("encoder h264_nvenc", "OK" if nvenc else "INFO", "GPU encoding available" if nvenc else "not usable here; libx264 (CPU) is used"))
    return checks


def _check_fonts(settings: Settings) -> list[Check]:
    from .captions import font_family_names

    fonts_dir = settings.fonts_dir
    files = sorted(p for p in fonts_dir.glob("*") if p.suffix.lower() in (".ttf", ".otf")) if fonts_dir.is_dir() else []
    if not files:
        return [Check("fonts", "FAIL", f"no .ttf/.otf in {fonts_dir}")]
    family = settings.style_cfg().font
    names = {n for p in files for n in font_family_names(p)}
    if family in names:
        return [Check("fonts", "OK", f"{len(files)} font file(s) in {fonts_dir}; style font {family!r} found")]
    return [Check("fonts", "WARN", f"style font {family!r} not among {sorted(names)} in {fonts_dir}; libass will substitute")]


def _check_opencv() -> list[Check]:
    try:
        import cv2
    except ImportError as e:
        return [Check("opencv cascade", "WARN", f"opencv not importable ({e}); --smart falls back to a centred crop")]
    cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if cascade.is_file():
        return [Check("opencv cascade", "OK", str(cascade))]
    return [Check("opencv cascade", "WARN", f"{cascade} missing; --smart falls back to a centred crop")]


def _whisper_state(model: str, device: str, compute_type: str) -> str:
    """'cached' | 'skipped' | 'downloaded' | 'unreachable (...)' | 'timeout'. The download attempt is bounded by a join."""
    from faster_whisper import WhisperModel

    try:
        WhisperModel(model, device=device, compute_type=compute_type, local_files_only=True)
        return "cached"
    except Exception:
        pass
    if os.environ.get(SKIP_WHISPER_ENV) == "1":
        return "skipped"
    outcome: list[str] = []

    def load() -> None:
        try:
            WhisperModel(model, device=device, compute_type=compute_type)
            outcome.append("downloaded")
        except Exception as e:
            outcome.append(f"unreachable ({type(e).__name__}: {str(e)[:80]})")

    worker = threading.Thread(target=load, name="doctor-whisper", daemon=True)
    worker.start()
    worker.join(WHISPER_DOWNLOAD_WAIT_S)
    return outcome[0] if outcome else "timeout"


def _check_whisper(settings: Settings) -> list[Check]:
    from .transcribe import resolve_whisper

    model, device, compute_type = resolve_whisper(settings)
    label = f"model={model} device={device} compute_type={compute_type}"
    state = _whisper_state(model, device, compute_type)
    if state in ("cached", "downloaded"):
        return [Check("whisper", "OK", f"{label}: model {state}")]
    if state == "skipped":
        return [Check("whisper", "WARN", f"{label}: not cached; download check skipped ({SKIP_WHISPER_ENV}=1)")]
    if settings.whisper.device == "auto" and device == "cuda" and state.startswith(("unreachable (RuntimeError", "unreachable (ValueError", "unreachable (OSError")):
        cpu = "model={} device={} compute_type={}".format(*resolve_whisper(settings, device="cpu"))
        return [Check("whisper", "WARN", f"{label}: cuda unusable {state[len('unreachable '):]}; whisper will fall back to {cpu}")]
    return [Check("whisper", "WARN", f"{label}: {state}; captions fast path still works; whisper needs the model")]


def _check_ytdlp(settings: Settings) -> list[Check]:
    """yt-dlp version and whether it has a JavaScript runtime (without one YouTube formats may be missing/throttled)."""
    try:
        import yt_dlp
    except ImportError as e:
        return [Check("yt-dlp", "FAIL", f"yt-dlp not importable ({e})")]
    from .download import ydl_opts

    version = yt_dlp.version.__version__
    with yt_dlp.YoutubeDL(ydl_opts(settings.workspace_dir, settings)) as ydl:
        infos = {name: rt.info for name, rt in getattr(ydl, "_js_runtimes", {}).items()}
    usable = [i for i in infos.values() if i is not None and getattr(i, "supported", True)]
    if usable:
        rt = usable[0]
        return [Check("yt-dlp", "OK", f"yt-dlp {version}; JS runtime {rt.name} {rt.version} at {rt.path}")]
    return [Check("yt-dlp", "WARN", f"yt-dlp {version}: no enabled JS runtime found (deno/node/bun; YouTube formats may be missing or throttled)")]


def _check_credentials(settings: Settings) -> list[Check]:
    yt = settings.platform_path(settings.platforms.youtube.client_secret)
    tiktok_key = settings.platforms.tiktok.client_key
    return [
        Check("youtube client_secret", "OK", str(yt)) if yt.is_file() else Check("youtube client_secret", "WARN", f"{yt} not found (only needed for API publishing; --manual works without it)"),
        Check("tiktok client_key", "OK", "set") if tiktok_key else Check("tiktok client_key", "WARN", "not set (platforms.tiktok.client_key; --manual works without it)"),
    ]


def _check_workspace(settings: Settings) -> list[Check]:
    workspace = settings.workspace_dir
    probe = workspace / ".doctor-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = Check("workspace", "OK", f"{workspace.resolve()} is writable")
    except OSError as e:
        writable = Check("workspace", "FAIL", f"{workspace.resolve()} not writable: {e}")
    free_gb = shutil.disk_usage(workspace).free / 1e9
    disk = Check("disk space", "OK" if free_gb >= MIN_FREE_GB else "WARN", f"{free_gb:.1f} GB free on the workspace drive" + ("" if free_gb >= MIN_FREE_GB else f" (< {MIN_FREE_GB:g} GB)"))
    return [writable, disk]


def run_doctor(settings: Settings) -> list[Check]:
    """Every environment check, in display order. Completes offline in well under 30 s."""
    groups: list[tuple[str, Callable[[], list[Check]]]] = [
        ("python", _check_python),
        ("ffmpeg", _check_ffmpeg),
        ("yt-dlp", lambda: _check_ytdlp(settings)),
        ("fonts", lambda: _check_fonts(settings)),
        ("opencv cascade", _check_opencv),
        ("whisper", lambda: _check_whisper(settings)),
        ("credentials", lambda: _check_credentials(settings)),
        ("workspace", lambda: _check_workspace(settings)),
    ]
    return [check for name, fn in groups for check in _guarded(name, fn)]


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check ffmpeg, filters, encoders, yt-dlp, fonts, whisper model, credentials, disk and workspace. Exit 1 on any FAIL."""
    checks = run_doctor(_settings(ctx))
    table = Table(title="clipforge doctor", expand=False)
    table.add_column("check", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for c in checks:
        table.add_row(escape(c.name), f"[{STATUS_STYLE.get(c.status, 'white')}]{c.status}[/]", escape(c.detail))
    console.print(table)
    failed = sum(c.status == "FAIL" for c in checks)
    warned = sum(c.status == "WARN" for c in checks)
    console.print(f"{len(checks)} checks: {failed} failed, {warned} warnings")
    if failed:
        raise typer.Exit(EXIT_FAILED)


# ---- fixture / init-config -------------------------------------------------------
@app.command()
def fixture(
    ctx: typer.Context,
    out: Path = typer.Argument(..., help="Output mp4 path; a <name>.en.json3 caption sidecar is written next to it."),
    seconds: float = typer.Option(120.0, "--seconds", min=2.0, help="Length of the synthetic video."),
) -> None:
    """Create a synthetic demo video (colour bars + tone + timecode) with a caption sidecar. No network."""
    from .fixture import make_fixture

    video, captions = make_fixture(out, seconds=seconds)
    console.print(f"video: {escape(str(video))}\ncaptions: {escape(str(captions))}")


@app.command("init-config")
def init_config(
    ctx: typer.Context,
    path: Path = typer.Option(Path("clipforge.yaml"), "--path", help="Where to write the config file."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a fully commented clipforge.yaml with every default."""
    if path.exists() and not force:
        _fail(f"{path} exists; use --force to overwrite")
    path.write_text(example_yaml(), encoding="utf-8")
    console.print(f"wrote {escape(str(path))}")


if __name__ == "__main__":
    app()
