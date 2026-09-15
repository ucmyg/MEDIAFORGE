"""YouTube Data API v3 publisher (Phase 2)."""
from __future__ import annotations

from .base import Publisher


class YouTubePublisher(Publisher):
    name = "youtube"

    def auth(self) -> bool:
        raise NotImplementedError

    def publish(self, clip, meta) -> str:
        raise NotImplementedError

    def limits(self):
        raise NotImplementedError
