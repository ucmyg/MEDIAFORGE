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

from .config import Settings

YOUTUBE_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|youtu\.be/|/embed/|/v/)([A-Za-z0-9_-]{11})")


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
    raise NotImplementedError


def find_sidecar_captions(media: Path, langs: list[str]) -> Path | None:
    """<stem>.<lang>.json3 or <stem>.json3 next to a local file."""
    raise NotImplementedError


def write_source_json(video_dir: Path, data: dict) -> None:
    (video_dir / "source.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def read_source_json(video_dir: Path) -> dict | None:
    p = video_dir / "source.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
