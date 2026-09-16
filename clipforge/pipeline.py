"""Orchestration: queued video -> ingest -> transcribe -> select -> render -> metadata -> rendered clips.

Idempotent per stage: every stage checks its cache/DB status first. `process_video` is what `run`, `tick` and the
daemon call. Per-video option overrides (from `clipforge add ... --count/--style/...`) live in videos.options.

Caches used (all under workspace/<video_id>/): source.json (ingest), transcript.json (transcribe),
candidates.<selector>.json (select) and clips/<clip_id>.{mp4,ass,json} (render + metadata). A re-run of a finished
video walks the same stages, finds every cache and returns without doing work; `force` re-selects and re-renders.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import download
from . import render as R
from . import transcribe as T
from .config import Settings
from .db import DB, Clip, Video
from .log import console_level, get_logger
from .metadata import generate_metadata, write_meta
from .select import Candidate, SelectOptions, select_highlights
from .transcript import Transcript

log = get_logger(__name__)

TERMINAL_STATUSES = ("done", "failed")  # videos the queue leaves alone
DONE_STATUSES = ("rendered", "ready", "posted", "rejected")  # clips that were rendered at some point
KEEP_STATUSES = ("posted", "rejected")  # clips never re-rendered, whatever the file state
MUSIC_EXTS = (".mp3", ".wav", ".m4a")
OPTION_KEYS = ("count", "min_s", "max_s", "style", "layout", "tighten", "smart", "punch", "force_whisper", "music")
ERROR_MAX = 2000


@dataclass
class ProcessReport:
    video_id: str
    status: str
    clips: list[Clip]
    error: str | None = None


def effective_options(settings: Settings, options: dict) -> dict:
    """Merge per-video overrides (count, min_s, max_s, style, layout, tighten, smart, punch, force_whisper) over settings."""
    defaults: dict[str, Any] = {
        "count": settings.clips.count,
        "min_s": settings.clips.min_s,
        "max_s": settings.clips.max_s,
        "style": settings.style,
        "layout": settings.layout,
        "tighten": settings.tighten,
        "smart": settings.smart,
        "punch": settings.punch,
        "force_whisper": False,
        "music": settings.music.enabled,
    }
    overrides = {k: v for k, v in (options or {}).items() if v is not None}
    return {**defaults, **overrides}


def clip_id_for(video_id: str, idx: int) -> str:
    return f"{video_id}_{idx:02d}"


def clips_dir(settings: Settings, video_id: str) -> Path:
    p = settings.video_dir(video_id) / "clips"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_music(settings: Settings) -> Path | None:
    """settings.music.file (as given, else under music_dir), or the first mp3/wav/m4a in music_dir; None with a warning."""
    cfg = settings.music
    if cfg.file:
        for candidate in (Path(cfg.file).expanduser(), settings.music_dir / cfg.file):
            if candidate.is_file():
                return candidate
        log.warning("music file %r not found (also looked under %s); rendering without music", cfg.file, settings.music_dir)
        return None
    found = sorted(p for p in settings.music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS) if settings.music_dir.is_dir() else []
    if not found:
        log.warning("no music file (%s) in %s; rendering without music", "/".join(MUSIC_EXTS), settings.music_dir)
        return None
    return found[0]


# ---- entry points ------------------------------------------------------------
def process_video(video_id: str, settings: Settings, db: DB, *, force: bool = False) -> ProcessReport:
    """Run every stage for one video. Never raises: a failure marks the video failed and is returned in the report."""
    video = db.get_video(video_id)
    if video is None:
        return ProcessReport(video_id, "failed", [], error=f"unknown video {video_id!r}")
    try:
        status, error = _run_stages(video, settings, db, force)
    except Exception as e:
        status, error = "failed", f"{type(e).__name__}: {e}"
        log.error("processing %s failed: %s", video_id, error)
        db.set_video_status(video_id, "failed", error[:ERROR_MAX])
        db.log("pipeline.failed", f"{video_id}: {error}", level="error")
    return ProcessReport(video_id, status, db.list_clips(video_id), error=error)


def process_queue(settings: Settings, db: DB) -> list[ProcessReport]:
    """Process every video whose status is not done/failed, in order, plus done videos that still have a failed
    clip (every earlier stage is a cache hit there and only the failed clips are rendered again)."""
    pending = [v for v in db.list_videos() if v.status not in TERMINAL_STATUSES or (v.status == "done" and db.list_clips(v.id, "failed"))]
    log.info("queue: %d video(s) to process", len(pending))
    return [process_video(v.id, settings, db) for v in pending]


# ---- stages ------------------------------------------------------------------
def _run_stages(video: Video, settings: Settings, db: DB, force: bool) -> tuple[str, str | None]:
    opts = effective_options(settings, video.options)
    if force:
        _discard_clips(db, settings, video.id)
    ingested = _stage_ingest(video, settings, db)
    transcript = _stage_transcribe(video, ingested, opts, settings, db)
    clips = _stage_select(video, ingested, transcript, opts, settings, db, force)
    clips = _stage_render(video, ingested, transcript, clips, opts, settings, db)
    return _finish(video.id, clips, db)


def _stage_ingest(video: Video, settings: Settings, db: DB) -> download.Ingested:
    ing = download.ingest(video.source, settings)
    db.update_video(video.id, path=str(ing.path), title=ing.title, duration=ing.duration, status="downloaded", error=None)
    db.log("pipeline.ingest", f"{video.id}: {ing.path} ({ing.duration:.1f}s, captions={'yes' if ing.captions_path else 'no'})")
    return ing


def _stage_transcribe(video: Video, ing: download.Ingested, opts: dict, settings: Settings, db: DB) -> Transcript:
    transcript = T.transcribe(
        ing.path, video.id, settings, captions_path=ing.captions_path, duration=ing.duration, force_whisper=bool(opts["force_whisper"])
    )
    db.set_video_status(video.id, "transcribed")
    db.log("pipeline.transcribe", f"{video.id}: source={transcript.source} words={len(transcript.words())}")
    return transcript


def _stage_select(video: Video, ing: download.Ingested, transcript: Transcript, opts: dict, settings: Settings, db: DB, force: bool) -> list[Clip]:
    sel = SelectOptions(count=int(opts["count"]), min_s=float(opts["min_s"]), max_s=float(opts["max_s"]))
    candidates = select_highlights(transcript, ing.path, settings, sel, cache_dir=settings.video_dir(video.id), force=force)
    if not candidates:
        raise RuntimeError(f"no highlight candidates in a {transcript.duration:.0f}s transcript with {len(transcript.words())} words")
    out_dir = clips_dir(settings, video.id)
    clips = _assign_clips(db, video.id, sorted(candidates, key=lambda c: (c.start, c.end)), out_dir)
    db.set_video_status(video.id, "selected")
    db.log("pipeline.select", f"{video.id}: {len(clips)} clip(s) " + ", ".join(f"{c.id}[{c.start:.1f}-{c.end:.1f}]" for c in clips))
    return clips


def _protected(db: DB, clip: Clip) -> bool:
    """A clip that is out on any platform (or being uploaded right now) must keep its id, bounds and file forever."""
    return clip.status == "posted" or db.has_posted(clip.id)


def _assign_clips(db: DB, video_id: str, candidates: list[Candidate], out_dir: Path) -> list[Clip]:
    """Map candidates onto clip rows. Ids are `<video>_<idx>` in start order; a protected clip never changes, so a
    new selection that does not match its bounds gets a fresh id after the highest existing one instead."""
    existing = {c.id: c for c in db.list_clips(video_id)}
    protected = {cid for cid, c in existing.items() if _protected(db, c)}
    next_idx = max(len(candidates), max((c.idx for c in existing.values()), default=-1) + 1)  # never an id a later candidate takes
    clips: list[Clip] = []
    for idx, cand in enumerate(candidates):
        match = next((existing[cid] for cid in protected if not _bounds_moved(existing[cid], cand)), None)
        if match is not None:
            clips.append(match)
            continue
        clip_id = clip_id_for(video_id, idx)
        if clip_id in protected:
            log.warning("clip %s is published; the new selection [%.1f-%.1f] gets a fresh id", clip_id, cand.start, cand.end)
            idx, next_idx = next_idx, next_idx + 1
        clips.append(_upsert_clip(db, video_id, idx, cand, out_dir))
    seen = {c.id for c in clips}
    clips.extend(existing[cid] for cid in sorted(protected) if cid not in seen)
    return sorted(clips, key=lambda c: c.idx)


def _upsert_clip(db: DB, video_id: str, idx: int, cand: Candidate, out_dir: Path) -> Clip:
    """Insert/update the clip row for a candidate. A protected clip keeps its bounds (its file is already public); a
    clip whose bounds moved drops its stale render so the render stage redoes it."""
    clip_id = clip_id_for(video_id, idx)
    existing = db.get_clip(clip_id)
    if existing is not None and _protected(db, existing):
        log.warning("clip %s is published; keeping it instead of the new selection [%.1f-%.1f]", clip_id, cand.start, cand.end)
        return existing
    if existing is not None and _bounds_moved(existing, cand):
        log.info("clip %s moved [%.1f-%.1f] -> [%.1f-%.1f]; discarding its render", clip_id, existing.start, existing.end, cand.start, cand.end)
        _remove_clip_files(out_dir, clip_id)
        db.update_clip(clip_id, status="candidate", path=None, meta_path=None, duration=None, error=None)
    return db.upsert_clip(clip_id, video_id, idx, cand.start, cand.end, cand.score, cand.hook)


def _bounds_moved(clip: Clip, cand: Candidate, tolerance: float = 0.01) -> bool:
    return abs(clip.start - cand.start) > tolerance or abs(clip.end - cand.end) > tolerance


def _remove_clip_files(out_dir: Path, clip_id: str) -> None:
    for p in out_dir.glob(f"{clip_id}.*"):
        p.unlink(missing_ok=True)


def _stage_render(video: Video, ing: download.Ingested, transcript: Transcript, clips: list[Clip], opts: dict, settings: Settings, db: DB) -> list[Clip]:
    out_dir = clips_dir(settings, video.id)
    pending = [c for c in clips if _needs_render(c)]
    if pending:
        jobs = _build_jobs(pending, ing, transcript, opts, settings, out_dir)
        log.info("rendering %d of %d clip(s) for %s", len(pending), len(clips), video.id)
        for clip, result in zip(pending, R.render_all(jobs, settings.render.workers, log_dir=settings.logs_dir, level=console_level())):
            _record_render(db, clip, result)
    else:
        log.info("all %d clip(s) of %s already rendered", len(clips), video.id)
    full_text = transcript.text()
    for clip in db.list_clips(video.id):
        if _has_file(clip) and not _has_meta(clip):
            _write_metadata(db, clip, ing.title, transcript, full_text, settings, out_dir)
    return db.list_clips(video.id)


def _needs_render(clip: Clip) -> bool:
    return clip.status not in KEEP_STATUSES and not (clip.status in DONE_STATUSES and _has_file(clip))


def _has_file(clip: Clip) -> bool:
    return bool(clip.path) and Path(clip.path).is_file()


def _has_meta(clip: Clip) -> bool:
    return bool(clip.meta_path) and Path(clip.meta_path).is_file()


def _build_jobs(clips: list[Clip], ing: download.Ingested, transcript: Transcript, opts: dict, settings: Settings, out_dir: Path) -> list[R.RenderJob]:
    style = settings.style_cfg(opts["style"])
    encoder = R.pick_encoder(settings.render)
    music = resolve_music(settings) if opts["music"] else None
    pad = settings.clips.pad_s
    return [
        R.RenderJob(
            source=ing.path,
            out=out_dir / f"{clip.id}.mp4",
            start=clip.start,
            end=clip.end,
            words=transcript.words_between(clip.start - pad, clip.end + pad),
            hook=clip.hook,
            style=style,
            render=settings.render,
            fonts_dir=settings.fonts_dir,
            layout=str(opts["layout"]),
            tighten=bool(opts["tighten"]),
            smart=bool(opts["smart"]),
            punch=bool(opts["punch"]),
            pad=pad,
            music=music,
            music_gain_db=settings.music.gain_db,
            encoder=encoder,
        )
        for clip in clips
    ]


def _record_render(db: DB, clip: Clip, result: R.RenderResult) -> None:
    if result.ok:
        db.update_clip(clip.id, path=str(result.out), duration=result.duration, status="rendered", error=None)
        db.log("pipeline.render", f"{clip.id}: {result.out.name} {result.duration:.2f}s")
        return
    error = (result.error or "render failed")[-ERROR_MAX:]
    db.set_clip_status(clip.id, "failed", error)
    db.log("pipeline.render", f"{clip.id}: failed: {error[-300:]}", level="error")


def _write_metadata(db: DB, clip: Clip, video_title: str, transcript: Transcript, full_text: str, settings: Settings, out_dir: Path) -> None:
    pad = settings.clips.pad_s
    clip_text = " ".join(w.text for w in transcript.words_between(clip.start - pad, clip.end + pad))
    meta = generate_metadata(clip_text, clip.hook, video_title, settings, full_text=full_text)
    meta.clip_id, meta.video_id, meta.start, meta.end = clip.id, clip.video_id, clip.start, clip.end
    path = write_meta(out_dir / f"{clip.id}.json", meta)
    db.update_clip(clip.id, meta_path=str(path))
    db.log("pipeline.metadata", f"{clip.id}: {meta.title!r}")


def _finish(video_id: str, clips: list[Clip], db: DB) -> tuple[str, str | None]:
    """'done' as soon as one clip rendered; failed clips are summarised in the video's error column (cleared by the
    next run's ingest stage) so the queue picks the video up again and the summary shows on the run line."""
    rendered = [c for c in clips if c.status in DONE_STATUSES]
    if rendered:
        failed = [c for c in clips if c.status == "failed"]
        error = f"{len(failed)} of {len(clips)} clip(s) failed: {failed[0].error or 'render failed'}"[:ERROR_MAX] if failed else None
        db.set_video_status(video_id, "done", error)
        db.log("pipeline.done", f"{video_id}: {len(rendered)} of {len(clips)} clip(s) rendered" + (f", {len(failed)} failed" if failed else ""))
        return "done", error
    errors = [c.error for c in clips if c.error]
    error = ("no clip rendered: " + errors[0])[:ERROR_MAX] if errors else "no clip rendered"
    db.set_video_status(video_id, "failed", error)
    db.log("pipeline.failed", f"{video_id}: {error}", level="error")
    return "failed", error


# ---- force ---------------------------------------------------------------------
def _discard_clips(db: DB, settings: Settings, video_id: str) -> None:
    """Delete the rendered files and DB rows of a video's clips so a forced run starts from a clean selection.
    Posted clips (and clips referenced by any post row) are kept: their files are already out on a platform."""
    out_dir = clips_dir(settings, video_id)
    for clip in db.list_clips(video_id):
        if _protected(db, clip):
            log.warning("clip %s is published on at least one platform; --force keeps its file and bounds", clip.id)
            continue
        _remove_clip_files(out_dir, clip.id)
    with db.connect() as c:
        removed = c.execute("DELETE FROM clips WHERE video_id=? AND id NOT IN (SELECT clip_id FROM posts)", (video_id,)).rowcount
    db.log("pipeline.force", f"{video_id}: discarded {removed} clip(s)")
    log.info("force: discarded %d clip(s) of %s", removed, video_id)
