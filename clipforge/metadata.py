"""Per-clip publishing metadata: title (<=100 chars), description, hashtags per platform. Heuristic by default, LLM optional.

Written to clips/<clip_id>.json next to the mp4.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings


@dataclass
class ClipMeta:
    title: str
    description: str
    hashtags: dict[str, list[str]] = field(default_factory=dict)  # {"youtube": [...], "tiktok": [...]}
    hook: str = ""
    topics: list[str] = field(default_factory=list)
    clip_id: str = ""
    video_id: str = ""
    start: float = 0.0
    end: float = 0.0

    def caption_for(self, platform: str) -> str:
        """Title + hashtags line for the given platform (TikTok caption / YouTube description head)."""
        tags = " ".join(self.hashtags.get(platform, []))
        return f"{self.title}\n\n{tags}".strip()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ClipMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def topic_tags(text: str, k: int = 4) -> list[str]:
    """Top-k content keywords (tf-idf-ish: frequent, non-stopword, >=4 chars) as lowercase hashtag stems."""
    raise NotImplementedError


def heuristic_metadata(clip_text: str, hook: str, video_title: str, full_text: str = "") -> ClipMeta:
    raise NotImplementedError


def llm_metadata(clip_text: str, hook: str, video_title: str, settings: Settings) -> ClipMeta | None:
    """One chat call; strict JSON {title, description, hashtags:[...]}; None on any failure."""
    raise NotImplementedError


def generate_metadata(clip_text: str, hook: str, video_title: str, settings: Settings, full_text: str = "") -> ClipMeta:
    """LLM when selector.mode == 'llm' (fallback heuristic), else heuristic. Title always clamped to 100 chars."""
    raise NotImplementedError


def write_meta(path: str | Path, meta: ClipMeta) -> Path:
    p = Path(path)
    p.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def read_meta(path: str | Path) -> ClipMeta:
    return ClipMeta.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
