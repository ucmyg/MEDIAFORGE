"""Transcription: json3 captions (instant, free) -> internal Transcript, else faster-whisper on CPU/GPU.

Cache: workspace/<video_id>/transcript.json. Skipped when present unless force.
"""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .transcript import Transcript


def json3_to_transcript(data: dict, video_id: str, duration: float, language: str | None = "en") -> Transcript:
    """Convert YouTube json3 (events/segs with tOffsetMs) to Transcript with word timestamps.

    Rules: skip events without segs; skip segs whose utf8 is only whitespace/newline; a word's end is the
    next word's start (capped at event end + small slack), the last word of an event ends at event end.
    Events with aAppend=1 continue the previous line. Text is collapsed whitespace.
    """
    raise NotImplementedError


def resolve_whisper(settings: Settings) -> tuple[str, str, str]:
    """(model, device, compute_type) after resolving 'auto': cpu -> (base, cpu, int8); nvidia -> (distil-large-v3, cuda, float16)."""
    raise NotImplementedError


def whisper_transcript(media_path: Path, video_id: str, settings: Settings, duration: float, model_factory=None) -> Transcript:
    """Run faster-whisper with word_timestamps=True, vad_filter=True. model_factory(model, device, compute_type) -> object with
    .transcribe(path, **kw) (injectable for tests)."""
    raise NotImplementedError


def transcribe(media_path: Path, video_id: str, settings: Settings, *, captions_path: Path | None, duration: float, force_whisper: bool = False, force: bool = False) -> Transcript:
    """Return the cached transcript, or build it from captions (unless force_whisper) or whisper, and save it."""
    raise NotImplementedError
