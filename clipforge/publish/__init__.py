"""Publishers: youtube (Data API v3), tiktok (Content Posting API), manual (clipboard + browser), browser (Playwright, opt-in)."""
from __future__ import annotations

from ..config import Settings
from ..db import DB
from .base import Publisher

PLATFORMS = ("youtube", "tiktok")


def get_publisher(name: str, settings: Settings, db: DB, *, manual: bool = False, browser: bool = False) -> Publisher:
    """Factory. name in PLATFORMS; manual=True -> ManualPublisher(name); browser=True -> BrowserPublisher(name)."""
    if browser:
        from .browser import BrowserPublisher

        return BrowserPublisher(name, settings, db)
    if manual:
        from .manual import ManualPublisher

        return ManualPublisher(name, settings, db)
    if name == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher(settings, db)
    if name == "tiktok":
        from .tiktok import TikTokPublisher

        return TikTokPublisher(settings, db)
    raise ValueError(f"unknown platform {name!r}; choose from {PLATFORMS}")
