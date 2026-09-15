"""Manual publisher: caption to clipboard, open the upload page, confirm (Phase 2)."""
from __future__ import annotations

from .base import Publisher


class ManualPublisher(Publisher):
    def __init__(self, platform: str, settings, db):
        super().__init__(settings, db)
        self.name = platform

    def auth(self) -> bool:
        return True

    def publish(self, clip, meta) -> str:
        raise NotImplementedError

    def limits(self):
        raise NotImplementedError
