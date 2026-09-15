"""TikTok Content Posting API publisher (Phase 2)."""
from __future__ import annotations

from .base import Publisher


class TikTokPublisher(Publisher):
    name = "tiktok"

    def auth(self) -> bool:
        raise NotImplementedError

    def publish(self, clip, meta) -> str:
        raise NotImplementedError

    def limits(self):
        raise NotImplementedError
