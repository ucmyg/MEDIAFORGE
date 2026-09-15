"""Publisher interface + shared helpers (daily allowance, one-time audit warnings, configured checks)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from ..db import DB, Clip
from ..log import console, get_logger
from ..metadata import ClipMeta

log = get_logger(__name__)

AUDIT_WARNINGS = {
    "youtube": (
        "YouTube: API projects that have not passed Google's compliance audit upload every video as PRIVATE, whatever "
        "privacyStatus is sent. ClipForge defaults to private. Apply for the audit at "
        "https://support.google.com/youtube/contact/yt_api_form and check quota at "
        "https://developers.google.com/youtube/v3/determine_quota_cost (videos.insert = platforms.youtube.upload_cost "
        "units of platforms.youtube.daily_quota per day)."
    ),
    "tiktok": (
        "TikTok: apps that have not passed TikTok's audit can only post SELF_ONLY, the posting account must be set to "
        "private, and at most 5 users may post per 24 h. ClipForge defaults to SELF_ONLY and only sends a privacy level "
        "that creator_info/query returned. Submit the app for review at https://developers.tiktok.com/ (Manage apps -> "
        "your app -> Submit for review)."
    ),
}


class PublishError(RuntimeError):
    """Retryable failure (network, 5xx, rate limit)."""


class PublishFatal(PublishError):
    """Non-retryable failure (bad credentials, rejected file, budget exhausted)."""


@dataclass
class Limits:
    per_day: int  # max posts per day from config
    posted_today: int
    remaining: int  # min(per_day - posted_today, quota-derived remaining), never negative
    quota_used: int = 0  # youtube units used today (0 for platforms without a quota model)
    quota_total: int = 0
    note: str = ""

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


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

    def is_configured(self) -> bool:
        """Non-interactive: credentials/config present so the scheduler may use this publisher. Default True."""
        return True

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

    def warn_once(self, key: str | None = None, message: str | None = None) -> None:
        """Print a loud warning the first time (per workspace DB) and log it; later calls only log at debug level."""
        key = key or f"warned.{self.name}.audit"
        message = message or AUDIT_WARNINGS.get(self.name, "")
        if not message:
            return
        if self.db.kv_get(key):
            log.debug("warning already shown: %s", key)
            return
        console.rule("[bold yellow]read this once[/]")
        console.print(f"[bold yellow]{message}[/]", highlight=False)
        console.rule()
        self.db.log("warning", message, level="warning")
        self.db.kv_set(key, "1")

    def ensure_allowance(self) -> Limits:
        """Raise PublishFatal when today's allowance is exhausted; returns the Limits otherwise."""
        lim = self.limits()
        if lim.exhausted:
            raise PublishFatal(f"{self.name}: daily allowance exhausted ({lim.posted_today}/{lim.per_day} posts, {lim.note})".rstrip(", )") + ")")
        return lim

    def ensure_not_posted(self, clip: Clip) -> None:
        if self.db.is_posted(clip.id, self.name):
            raise PublishFatal(f"{self.name}: clip {clip.id} was already posted")
