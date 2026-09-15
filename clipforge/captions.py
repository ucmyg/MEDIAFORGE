"""ASS caption generation (karaoke word groups + hook card) and word-time remapping through a cut list.

Coordinates are in output pixel space (default 1080x1920). The .ass is burned by ffmpeg's `subtitles`
filter with fontsdir=<assets/fonts>, so only the bundled font family is referenced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import StyleCfg
from .transcript import Word


@dataclass
class Cut:
    """A kept region of the source segment. src times are absolute source seconds; dst_start is where it lands in the output."""

    src_start: float
    src_end: float
    dst_start: float

    @property
    def duration(self) -> float:
        return self.src_end - self.src_start

    @property
    def dst_end(self) -> float:
        return self.dst_start + self.duration


def remap_words(words: list[Word], cuts: list[Cut]) -> list[Word]:
    """Map words from source time to output time through the kept regions. Words entirely inside a removed gap are
    dropped; words straddling a cut boundary are clamped to the kept part. Output words are sorted and non-overlapping."""
    raise NotImplementedError


def group_words(words: list[Word], max_words: int = 4, max_gap: float = 0.6, max_chars: int = 22) -> list[list[Word]]:
    """2-4 word groups (1 allowed only for isolated words): break on max_words, max_chars, gaps > max_gap, and sentence-ending punctuation."""
    raise NotImplementedError


def ass_escape(text: str) -> str:
    """Escape for an ASS Dialogue text field: braces, backslashes, newlines."""
    raise NotImplementedError


def hex_to_ass(rgb_hex: str, alpha: int = 0) -> str:
    """'RRGGBB' -> '&HAABBGGRR'."""
    raise NotImplementedError


def build_ass(words: list[Word], style: StyleCfg, *, duration: float, hook: str | None = None, hook_seconds: float = 1.8, width: int = 1080, height: int = 1920) -> str:
    """Full .ass document. One Dialogue per word: the group text with the active word in accent colour (\\c override),
    `Caption` style anchored (alignment 5, centred) at pos_y*height. Hook card: `Hook` style at ~12% height for
    hook_seconds with \\fad(150,300); wrapped to fit ~90% width; optional dark box (BorderStyle 3 look-alike via \\bord/\\3c).
    Nothing is placed in the bottom 20% or right 15% of the frame."""
    raise NotImplementedError


def write_ass(path: str | Path, content: str) -> Path:
    p = Path(path)
    p.write_text(content, encoding="utf-8")
    return p
