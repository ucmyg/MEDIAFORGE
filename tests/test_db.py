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


def test_reserve_and_claim_counts_posts_and_live_claims(db: DB):
    db.add_video("v", "local", "x.mp4")
    for cid in ("v_00", "v_01", "v_02"):
        db.upsert_clip(cid, "v", int(cid[-1]), 0.0, 1.0, 0.0, "")
    day, stale, now = "2026-09-16T00:00:00+00:00", "2026-09-15T21:00:00+00:00", "2026-09-16T10:00:00+00:00"
    db.ensure_post("v_00", "yt")
    db.mark_posted("v_00", "yt", "a")
    with db.connect() as c:
        c.execute("UPDATE posts SET posted_at=? WHERE clip_id='v_00'", ("2026-09-16T09:00:00+00:00",))
    db.ensure_post("v_01", "yt")
    kw = dict(day_start_iso=day)
    assert db.reserve_and_claim("v_01", "yt", now, stale, per_day=1, **kw) == "per_day"
    assert db.reserve_and_claim("v_01", "yt", now, stale, per_day=2, gap_before_iso="2026-09-16T08:30:00+00:00", **kw) == "gap"
    assert db.reserve_and_claim("v_01", "yt", now, stale, per_day=2, gap_before_iso="2026-09-16T09:30:00+00:00", **kw) == "ok"
    assert db.get_post("v_01", "yt").status == "uploading"
    db.ensure_post("v_02", "yt")
    assert db.reserve_and_claim("v_02", "yt", now, stale, per_day=2, **kw) == "per_day"  # the live claim on v_01 is capacity
    assert db.reserve_and_claim("v_02", "yt", now, stale, per_day=3, gap_before_iso="2026-09-16T09:59:00+00:00", **kw) == "gap"
    assert db.reserve_and_claim("v_01", "yt", now, stale, per_day=3, **kw) == "busy"  # held by the other process
    assert db.count_active("yt", day, stale) == 2 and db.latest_activity("yt", stale) == now
    assert db.has_posted("v_00") and db.has_posted("v_01") and not db.has_posted("v_02")
    assert db.reserve_and_claim("v_02", "yt", now, stale, per_day=3, **kw) == "ok"
