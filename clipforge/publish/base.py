"""Publisher interface + shared helpers (daily budget, audit warning)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from ..db import DB, Clip
from ..metadata import ClipMeta


class PublishError(RuntimeError):
    """Retryable failure (network, 5xx, rate limit)."""


class PublishFatal(PublishError):
    """Non-retryable failure (bad credentials, rejected file, budget exhausted)."""


@dataclass
class Limits:
    per_day: int  # max posts per day from config
    posted_today: int
    remaining: int  # min(per_day - posted_today, quota-derived remaining)
    quota_used: int = 0  # youtube units used today (0 for platforms without a quota model)
    quota_total: int = 0
    note: str = ""


class Publisher(ABC):
    name: str = ""

    def __init__(self, settings: Settings, db: DB):
        self.settings = settings
        self.db = db

    @abstractmethod
    def auth(self) -> bool:
        """Interactive auth if needed; returns True when credentials are usable."""

    @abstractmethod
    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        """Upload + post. Returns the platform post/video id. Raises PublishError/PublishFatal."""

    @abstractmethod
    def limits(self) -> Limits:
        """Remaining allowance for today."""

    # ---- shared helpers ------------------------------------------------
    def today_key(self) -> str:
        """Budget day key. YouTube quota resets at midnight Pacific; others use UTC."""
        if self.name == "youtube":
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def posted_today(self) -> int:
        day = self.today_key()
        return len([p for p in self.db.list_posts(self.name, "posted") if (p.posted_at or "")[:10] == day])
