"""Orchestration: queued video -> ingest -> transcribe -> select -> render -> metadata -> rendered clips.

Idempotent per stage: every stage checks its cache/DB status first. `process_video` is what `run`, `tick` and the
daemon call. Per-video option overrides (from `clipforge add ... --count/--style/...`) live in videos.options.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .db import DB, Clip


@dataclass
class ProcessReport:
    video_id: str
    status: str
    clips: list[Clip]
    error: str | None = None


def effective_options(settings: Settings, options: dict) -> dict:
    """Merge per-video overrides (count, min_s, max_s, style, layout, tighten, smart, punch, force_whisper) over settings."""
    raise NotImplementedError


def process_video(video_id: str, settings: Settings, db: DB, *, force: bool = False) -> ProcessReport:
    raise NotImplementedError


def process_queue(settings: Settings, db: DB) -> list[ProcessReport]:
    """Process every video whose status is not done/failed, in order."""
    raise NotImplementedError


def clips_dir(settings: Settings, video_id: str) -> Path:
    p = settings.video_dir(video_id) / "clips"
    p.mkdir(parents=True, exist_ok=True)
    return p
