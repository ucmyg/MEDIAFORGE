"""Ingest: YouTube URL via yt-dlp (best mp4 <= max_height, plus json3 auto-captions) or a local file.

Everything lives under workspace/<video_id>/:
  source.mp4 (download) | source.json (metadata + pointer to the local file)   source.<lang>.json3 (captions, if any)
Local files are referenced, never copied. A local file with a sidecar `<stem>.<lang>.json3` / `<stem>.json3`
next to it uses that as captions (so user-supplied captions and the synthetic fixture take the fast path).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ffmpeg as F
from .config import Settings
from .db import utcnow
from .log import get_logger

log = get_logger(__name__)

YOUTUBE_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|youtu\.be/|/embed/|/v/)([A-Za-z0-9_-]{11})")
SOURCE_STEM = "source"
CAPTIONS_SUFFIX = ".json3"
MEDIA_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".flv", ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus"}


@dataclass
class Ingested:
    video_id: str
    kind: str  # youtube | local
    path: Path  # media file to process
    captions_path: Path | None  # json3 captions or None
    title: str
    duration: float
    source: str  # original url or path


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip(), re.I))


def video_id_for(source: str) -> tuple[str, str]:
    """(video_id, kind). YouTube -> the 11-char id; local -> 'l' + sha1(abs path)[:11]."""
    if is_url(source):
        m = YOUTUBE_ID_RE.search(source)
        if m:
            return m.group(1), "youtube"
        return "u" + hashlib.sha1(source.encode()).hexdigest()[:11], "youtube"
    p = Path(source).expanduser().resolve()
    return "l" + hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:11], "local"


def ingest(source: str, settings: Settings, *, force: bool = False) -> Ingested:
    """Download (or reference) the media and captions for `source`. Cached: re-runs return instantly."""
    video_id, kind = video_id_for(source)
    video_dir = settings.video_dir(video_id)
    if not force:
        cached = _cached(video_dir, video_id)
        if cached is not None:
            log.info("ingest cached: %s -> %s", video_id, cached.path)
            return cached
    if kind == "local":
        ing = _ingest_local(source, video_id, settings)
    else:
        ing = _ingest_youtube(source, video_id, video_dir, settings, force=force)
    write_source_json(video_dir, _source_record(ing))
    log.info("ingested %s (%s) title=%r duration=%.1fs captions=%s", video_id, kind, ing.title, ing.duration, ing.captions_path)
    return ing


def _cached(video_dir: Path, video_id: str) -> Ingested | None:
    """The Ingested described by source.json, provided the media it points at still exists."""
    record = read_source_json(video_dir)
    if not record or not record.get("path") or not Path(record["path"]).is_file():
        return None
    return _from_record(record, video_id)


def _from_record(record: dict[str, Any], video_id: str) -> Ingested:
    media = Path(record["path"])
    captions = Path(record["captions"]) if record.get("captions") else None
    if captions is not None and not captions.is_file():
        captions = None
    return Ingested(
        video_id=video_id,
        kind=str(record.get("kind") or "local"),
        path=media,
        captions_path=captions,
        title=str(record.get("title") or media.stem),
        duration=float(record.get("duration") or 0.0),
        source=str(record.get("source") or ""),
    )


def _source_record(ing: Ingested) -> dict[str, Any]:
    return {
        "video_id": ing.video_id,
        "kind": ing.kind,
        "source": ing.source,
        "title": ing.title,
        "duration": ing.duration,
        "path": str(ing.path),
        "captions": str(ing.captions_path) if ing.captions_path else None,
        "fetched_at": utcnow(),
    }


# ---- local files -----------------------------------------------------------
def _ingest_local(source: str, video_id: str, settings: Settings) -> Ingested:
    media = Path(source).expanduser().resolve()
    if not media.is_file():
        raise FileNotFoundError(f"local media not found: {media}")
    captions = find_sidecar_captions(media, settings.download.caption_langs)
    return Ingested(video_id, "local", media, captions, media.stem, F.probe(media).duration, str(media))


def find_sidecar_captions(media: Path, langs: list[str]) -> Path | None:
    """<stem>.<lang>.json3 or <stem>.json3 next to a local file."""
    media = Path(media)
    return _pick_captions(_caption_candidates(media.parent, media.stem), langs)


def _caption_candidates(directory: Path, stem: str) -> dict[str, Path]:
    """{lang_code: path} for every <stem>[.<code>].json3 in directory; the bare <stem>.json3 has code ''."""
    prefix = stem + "."
    out: dict[str, Path] = {}
    for p in sorted(directory.iterdir()):
        name = p.name
        if p.is_file() and name.startswith(prefix) and name.endswith(CAPTIONS_SUFFIX):
            out[name[len(prefix) : -len(CAPTIONS_SUFFIX)].rstrip(".")] = p
    return out


def _primary_lang(code: str) -> str:
    return re.split(r"[-_]", code, maxsplit=1)[0].lower()


def _pick_captions(candidates: dict[str, Path], langs: list[str]) -> Path | None:
    """First language in `langs` with an exact code, else one sharing its primary subtag (en ~ en-orig ~ en-US), else the bare file."""
    for lang in langs:
        if lang in candidates:
            return candidates[lang]
        for code, path in candidates.items():
            if code and _primary_lang(code) == _primary_lang(lang):
                return path
    return candidates.get("")


# ---- yt-dlp ----------------------------------------------------------------
def _ingest_youtube(url: str, video_id: str, video_dir: Path, settings: Settings, *, force: bool) -> Ingested:
    if force:
        _clear_download(video_dir)
    media, info = _download_youtube(url, video_dir, settings)
    captions = find_downloaded_captions(video_dir, settings.download.caption_langs)
    title = str(info.get("title") or media.stem)
    return Ingested(video_id, "youtube", media, captions, title, _duration(media, info), url)


def _clear_download(video_dir: Path) -> None:
    """Remove source.* (media, captions, metadata) so a forced ingest re-downloads."""
    for p in video_dir.iterdir():
        if p.is_file() and p.name.startswith(SOURCE_STEM + "."):
            p.unlink()


def _duration(media: Path, info: dict[str, Any]) -> float:
    probed = F.probe(media).duration
    return probed if probed > 0 else float(info.get("duration") or 0.0)


def ydl_opts(video_dir: Path, settings: Settings) -> dict[str, Any]:
    """yt-dlp options: best mp4 <= max_height merged with the bundled ffmpeg, json3 captions, quiet."""
    h = settings.download.max_height
    opts: dict[str, Any] = {
        "format": f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/best[ext=mp4][height<={h}]/best[height<={h}]/best",
        "merge_output_format": "mp4",
        "paths": {"home": str(video_dir)},
        "outtmpl": f"{SOURCE_STEM}.%(ext)s",
        "ffmpeg_location": F.ffmpeg_exe(),
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitlesformat": "json3",
        "subtitleslangs": list(settings.download.caption_langs),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # route yt-dlp's own messages into our log: with a logger set, report_warning ignores no_warnings, so the
        # missing-JS-runtime / signature-solving diagnostics reach logs/clipforge.log instead of vanishing
        "logger": log,
        "noprogress": True,
        "color": "never",
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},  # any installed supported runtime, not deno only
        "socket_timeout": settings.download.socket_timeout_s,
        "retries": settings.download.retries,
        "fragment_retries": settings.download.retries,
    }
    if settings.download.cookies_file:
        opts["cookiefile"] = str(Path(settings.download.cookies_file).expanduser())
    return opts


def _download_youtube(url: str, video_dir: Path, settings: Settings) -> tuple[Path, dict[str, Any]]:
    """Run yt-dlp (imported lazily) and return (media file, info dict). Tests replace this to avoid the network.

    Media and captions are fetched in two runs: yt-dlp writes subtitles before it downloads the media and raises on
    any subtitle fetch error (YouTube's timedtext endpoint fails intermittently), which would abort the whole ingest
    although captions are optional (transcribe falls back to whisper)."""
    import yt_dlp

    base = ydl_opts(video_dir, settings)
    with yt_dlp.YoutubeDL({**base, "writesubtitles": False, "writeautomaticsub": False}) as ydl:
        info = ydl.extract_info(url, download=True)
    info = _single_entry(info, url)
    media = locate_media(video_dir, info)
    if settings.download.caption_langs:
        try:  # captions are optional: a failure here is logged and whisper is used
            with yt_dlp.YoutubeDL({**base, "skip_download": True, "ignoreerrors": True}) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            log.warning("captions unavailable for %s (%s); whisper will be used", url, (str(e).strip().splitlines() or ["?"])[-1])
    return media, info


def _single_entry(info: dict[str, Any] | None, url: str) -> dict[str, Any]:
    if not info:
        raise RuntimeError(f"yt-dlp returned no information for {url}")
    entries = [e for e in info.get("entries") or [] if e]
    return entries[0] if entries else info


def locate_media(video_dir: Path, info: dict[str, Any] | None = None) -> Path:
    """The downloaded media: yt-dlp's reported final path, else source.mp4, else any source.<media ext>."""
    for rd in (info or {}).get("requested_downloads") or []:
        fp = rd.get("filepath")
        if fp and Path(fp).is_file():
            return Path(fp)
    mp4 = video_dir / f"{SOURCE_STEM}.mp4"
    if mp4.is_file():
        return mp4
    for p in sorted(video_dir.glob(f"{SOURCE_STEM}.*")):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            return p
    raise FileNotFoundError(f"no downloaded media under {video_dir}")


def find_downloaded_captions(video_dir: Path, langs: list[str]) -> Path | None:
    """source.<lang>.json3 in language order (prefix-tolerant: en-orig, en-US), else any *.json3 in the dir."""
    picked = _pick_captions(_caption_candidates(video_dir, SOURCE_STEM), langs)
    if picked is not None:
        return picked
    others = sorted(p for p in video_dir.iterdir() if p.is_file() and p.name.endswith(CAPTIONS_SUFFIX))
    return others[0] if others else None


# ---- source.json -----------------------------------------------------------
def write_source_json(video_dir: Path, data: dict) -> None:
    (video_dir / "source.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def read_source_json(video_dir: Path) -> dict | None:
    p = video_dir / "source.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
