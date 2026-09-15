"""Highlight selection. `Selector` is pluggable; `heuristic` is the free default, `llm` is optional with hard fallback.

Cache: workspace/<video_id>/candidates.<selector>.json keyed by (selector name, count, min_s, max_s).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from .config import Settings
from .transcript import Transcript


@dataclass
class SelectOptions:
    count: int = 5
    min_s: float = 20.0
    max_s: float = 58.0


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    hook: str
    sent_start: int  # first sentence index (inclusive)
    sent_end: int  # last sentence index (inclusive)
    why: str = ""
    features: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(**d)


class Selector(Protocol):
    name: str

    def select(self, transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]: ...


class HeuristicSelector:
    """Windows of consecutive sentences within [min_s, max_s]; score = hook + distinctiveness (tf-idf) + energy z
    + speech-rate variance + completeness - filler density - overlap; greedy non-max suppression to top K."""

    name = "heuristic"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    def select(self, transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]:
        raise NotImplementedError


class LLMSelector:
    """One chat completion per video against an OpenAI-compatible endpoint. Strict JSON validation; any failure -> heuristic."""

    name = "llm"

    def __init__(self, settings: Settings, fallback: Selector | None = None):
        self.settings = settings
        self.fallback = fallback or HeuristicSelector(settings)

    def select(self, transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]:
        raise NotImplementedError


def get_selector(settings: Settings) -> Selector:
    return LLMSelector(settings) if settings.selector.mode == "llm" else HeuristicSelector(settings)


def hook_line(text: str, max_chars: int = 60) -> str:
    """One-line hook from the first sentence(s) of a window: trimmed, no trailing filler, <= max_chars."""
    raise NotImplementedError


def select_highlights(transcript: Transcript, media_path: Path | None, settings: Settings, opts: SelectOptions, cache_dir: Path | None = None, force: bool = False) -> list[Candidate]:
    """Selector dispatch + JSON cache."""
    raise NotImplementedError
