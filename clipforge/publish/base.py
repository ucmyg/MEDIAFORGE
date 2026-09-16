"""Publisher interface + shared helpers (daily allowance, one-time audit warnings, configured checks)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache

from ..config import Settings
from ..db import CLAIM_STALE_S, DB, Clip
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


class AuthError(PublishFatal):
    """Platform-level credential failure (no token, refresh refused, HTTP 401/403 on the account).

    Not this clip's fault: publishers and the scheduler leave the post row untouched (no attempt, no backoff, never
    final) so the clip is picked up again once `clipforge auth <platform>` has been run.
    """


class NotConfirmed(PublishError):
    """The human declined the hand-off (manual/browser) or the automation never attached the file: nothing was attempted
    on the platform, so no attempt is recorded in the posts table."""


PACIFIC = "America/Los_Angeles"  # YouTube's quota day
PACIFIC_FALLBACK_OFFSET_H = -8  # used when the tz database is missing (Windows without the tzdata package)


@lru_cache(maxsize=None)
def pacific_tz() -> tzinfo:
    """America/Los_Angeles, or a fixed UTC-8 (warned once) when no tz database is available."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(PACIFIC)
    except ZoneInfoNotFoundError:
        log.warning("youtube: tz database missing (pip install tzdata); using fixed UTC%d for the quota day", PACIFIC_FALLBACK_OFFSET_H)
        return timezone(timedelta(hours=PACIFIC_FALLBACK_OFFSET_H))


def _day_in(iso_ts: str, tz: tzinfo) -> str:
    parsed = datetime.fromisoformat(iso_ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).strftime("%Y-%m-%d")


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

    def credentials_ok(self) -> bool:
        """Non-interactive probe run by the scheduler before it touches a clip: True when a post could be made now.

        Default True (nothing to check); API publishers verify/refresh the stored token so an unusable one becomes a
        platform-level skip instead of a failed attempt on some clip.
        """
        return True

    # ---- shared helpers ------------------------------------------------
    def budget_tz(self) -> tzinfo:
        """Budget-day zone: YouTube quota resets at midnight Pacific; others use UTC."""
        return pacific_tz() if self.name == "youtube" else timezone.utc

    def today_key(self) -> str:
        """Budget day key (YYYY-MM-DD in budget_tz())."""
        return datetime.now(self.budget_tz()).strftime("%Y-%m-%d")

    def day_start_iso(self) -> str:
        """UTC ISO timestamp of today_key()'s midnight in budget_tz() (posted_at / claimed_at are stored as UTC ISO)."""
        start = datetime.strptime(self.today_key(), "%Y-%m-%d").replace(tzinfo=self.budget_tz())
        return start.astimezone(timezone.utc).isoformat(timespec="seconds")

    def stale_before_iso(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=CLAIM_STALE_S)).isoformat(timespec="seconds")

    def posted_today(self) -> int:
        """Posts completed today (budget zone) plus uploads another process is holding a live claim on right now, so
        two schedulers cannot both count the same free slot."""
        return self.db.count_active(self.name, self.day_start_iso(), self.stale_before_iso())

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
