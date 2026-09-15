"""Scheduler (Phase 3): daemon loop / one-shot tick. Process queue, then post ready clips at configured local times."""
from __future__ import annotations


def tick(settings, db, *, now=None, dry_run: bool = False) -> dict:
    raise NotImplementedError


def daemon(settings, db) -> None:
    raise NotImplementedError
