"""Review: rich table of rendered clips + review.html with <video> previews and approve/reject checkboxes that
export decisions JSON; `apply_decisions` moves clips to ready/rejected."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from .db import DB


def print_review_table(db: DB, console: Console, video_id: str | None = None, statuses: tuple[str, ...] = ("rendered", "ready", "rejected", "posted")) -> None:
    raise NotImplementedError


def write_review_html(db: DB, out_path: str | Path, video_id: str | None = None) -> Path:
    """Self-contained HTML (no external assets). Video src is a relative path from out_path's folder. A button
    downloads decisions.json: {"approve": [clip_id...], "reject": [clip_id...]}."""
    raise NotImplementedError


def apply_decisions(db: DB, decisions_path: str | Path) -> tuple[int, int]:
    """Returns (approved, rejected) counts. Never demotes a posted clip."""
    raise NotImplementedError
