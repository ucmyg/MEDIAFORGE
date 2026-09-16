"""clipforge.db: the post-row claim used to serialise uploads across processes, and the schema migration behind it."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from clipforge.db import DB, SCHEMA

NOW = "2026-09-15T09:05:00+00:00"
STALE_BEFORE = "2026-09-15T06:05:00+00:00"


def make_clip(db: DB, clip_id: str = "vid_00") -> None:
    db.add_video("vid", "local", "src.mp4")
    db.upsert_clip(clip_id, "vid", 0, 0.0, 1.0, 0.0, "")


def test_claim_post_is_exclusive_and_accepts_failed_and_stale_rows(db: DB):
    make_clip(db)
    assert not db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE)  # no row yet: nothing to claim
    db.ensure_post("vid_00", "youtube")
    assert db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE)
    assert not db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE)  # a second process loses
    post = db.get_post("vid_00", "youtube")
    assert post.status == "uploading" and post.claimed_at == NOW

    db.release_claim("vid_00", "youtube")
    post = db.get_post("vid_00", "youtube")
    assert post.status == "pending" and post.claimed_at is None and post.attempts == 0

    db.mark_post_failed("vid_00", "youtube", "boom", None, final=True)
    assert db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE)  # `publish --now --clip-id` retries a final failure
    with db.connect() as c:
        c.execute("UPDATE posts SET claimed_at=?", ("2026-09-15T05:00:00+00:00",))
    assert db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE)  # a claim older than the cutoff belongs to a dead process

    db.mark_posted("vid_00", "youtube", "yt-1")
    assert not db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE) and db.is_posted("vid_00", "youtube")


def test_delete_post_only_removes_rows_without_attempts(db: DB):
    make_clip(db)
    row = db.ensure_post("vid_00", "youtube")
    assert db.delete_post(row.id) and db.get_post("vid_00", "youtube") is None
    db.mark_post_failed("vid_00", "youtube", "boom", None)
    row = db.get_post("vid_00", "youtube")
    assert not db.delete_post(row.id) and db.get_post("vid_00", "youtube").attempts == 1


def test_existing_db_without_claimed_at_is_migrated(tmp_path: Path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.replace(",\n  claimed_at TEXT                           -- set while a process holds the row as `uploading`", ""))
    con.close()
    assert "claimed_at" not in {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(posts)")}
    db = DB(path)
    make_clip(db)
    db.ensure_post("vid_00", "youtube")
    assert db.claim_post("vid_00", "youtube", NOW, STALE_BEFORE) and db.get_post("vid_00", "youtube").claimed_at == NOW
    DB(path)  # idempotent


def test_kv_delete(db: DB):
    db.kv_set("k", "v")
    db.kv_delete("k")
    assert db.kv_get("k") is None
    db.kv_delete("k")  # missing key is fine
