"""SQLite state. One file, WAL mode, short-lived connections (safe for the process pool + daemon).

Tables: videos, clips, posts, budget, log, kv.  All timestamps are ISO-8601 UTC strings.
Idempotency rules enforced here: a video id is unique; (clip_id, platform) can only be `posted` once, and a post row
must be claimed (`uploading`, see claim_post) before an upload starts so two processes never post the same clip.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- youtube | local
  source TEXT NOT NULL,            -- url or original path
  path TEXT,                       -- downloaded / referenced media file
  title TEXT,
  duration REAL,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued|downloaded|transcribed|selected|rendered|done|failed
  options TEXT NOT NULL DEFAULT '{}',     -- json: per-video overrides from `clipforge add`
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,             -- <video_id>_<idx:02d>
  video_id TEXT NOT NULL REFERENCES videos(id),
  idx INTEGER NOT NULL,
  start REAL NOT NULL,
  end REAL NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  hook TEXT NOT NULL DEFAULT '',
  path TEXT,                       -- rendered mp4
  meta_path TEXT,                  -- metadata json
  duration REAL,                   -- rendered duration (after tighten)
  status TEXT NOT NULL DEFAULT 'candidate',  -- candidate|rendered|ready|rejected|posted|failed
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id TEXT NOT NULL REFERENCES clips(id),
  platform TEXT NOT NULL,
  post_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|uploading|posted|failed
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  posted_at TEXT,
  claimed_at TEXT                           -- set while a process holds the row as `uploading`
);
CREATE UNIQUE INDEX IF NOT EXISTS posts_unique_posted ON posts(clip_id, platform) WHERE status = 'posted';
-- get_post / is_posted / claim_post look up the latest row per (clip, platform): was a full scan of posts
CREATE INDEX IF NOT EXISTS posts_clip_platform_id ON posts(clip_id, platform, id);
-- list_clips(video_id) with ORDER BY video_id, idx: was a scan plus a temp b-tree sort
CREATE INDEX IF NOT EXISTS clips_video_idx ON clips(video_id, idx);
CREATE TABLE IF NOT EXISTS budget (
  platform TEXT NOT NULL,
  day TEXT NOT NULL,               -- YYYY-MM-DD (platform's reset timezone; youtube = America/Los_Angeles)
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (platform, day)
);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
-- "post THIS clip on THIS platform at THIS time": the scheduler posts due rows first, then falls back to the slots
CREATE TABLE IF NOT EXISTS scheduled (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id TEXT NOT NULL REFERENCES clips(id),
  platform TEXT NOT NULL,
  run_at TEXT NOT NULL,                     -- UTC ISO
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|posted|failed|cancelled
  post_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_unique_pending ON scheduled(clip_id, platform) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS scheduled_status_run_at ON scheduled(status, run_at);
"""
# Columns added after the first release; CREATE TABLE IF NOT EXISTS does not add them to existing workspace DBs.
MIGRATIONS = {"posts": {"claimed_at": "TEXT"}}
CLAIM_STALE_S = 3 * 3600  # an `uploading` claim older than this belongs to a dead process and may be taken over


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Video:
    id: str
    kind: str
    source: str
    path: str | None
    title: str | None
    duration: float | None
    status: str
    options: dict[str, Any]
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class Clip:
    id: str
    video_id: str
    idx: int
    start: float
    end: float
    score: float
    hook: str
    path: str | None
    meta_path: str | None
    duration: float | None
    status: str
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class Post:
    id: int
    clip_id: str
    platform: str
    post_id: str | None
    status: str
    attempts: int
    next_attempt_at: str | None
    error: str | None
    created_at: str
    posted_at: str | None
    claimed_at: str | None = None


@dataclass
class Scheduled:
    id: int
    clip_id: str
    platform: str
    run_at: str
    status: str
    post_id: str | None
    error: str | None
    created_at: str
    updated_at: str


def _row_scheduled(r: sqlite3.Row) -> Scheduled:
    return Scheduled(**{k: r[k] for k in Scheduled.__dataclass_fields__})


def _row_video(r: sqlite3.Row) -> Video:
    d = dict(r)
    d["options"] = json.loads(d.get("options") or "{}")
    return Video(**d)


def _row_clip(r: sqlite3.Row) -> Clip:
    return Clip(**dict(r))


def _row_post(r: sqlite3.Row) -> Post:
    return Post(**dict(r))


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                present = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, decl in columns.items():
                    if column not in present:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """One transaction. `immediate=True` takes the write lock up front so a read-then-write decision (capacity
        check + claim) is atomic across processes instead of racing between the SELECT and the UPDATE."""
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield con
            if con.in_transaction:
                con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    # ---- videos --------------------------------------------------------
    def add_video(self, id: str, kind: str, source: str, options: dict | None = None, title: str | None = None) -> tuple[Video, bool]:
        """Insert if missing. Returns (video, created)."""
        now = utcnow()
        with self.connect() as c:
            cur = c.execute("SELECT * FROM videos WHERE id=?", (id,)).fetchone()
            if cur:
                return _row_video(cur), False
            c.execute(
                "INSERT INTO videos(id,kind,source,title,status,options,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (id, kind, source, title, "queued", json.dumps(options or {}), now, now),
            )
            return _row_video(c.execute("SELECT * FROM videos WHERE id=?", (id,)).fetchone()), True

    def get_video(self, id: str) -> Video | None:
        with self.connect() as c:
            r = c.execute("SELECT * FROM videos WHERE id=?", (id,)).fetchone()
            return _row_video(r) if r else None

    def list_videos(self, status: str | list[str] | None = None) -> list[Video]:
        with self.connect() as c:
            if status is None:
                rows = c.execute("SELECT * FROM videos ORDER BY created_at").fetchall()
            else:
                st = [status] if isinstance(status, str) else list(status)
                q = ",".join("?" * len(st))
                rows = c.execute(f"SELECT * FROM videos WHERE status IN ({q}) ORDER BY created_at", st).fetchall()
            return [_row_video(r) for r in rows]

    def update_video(self, id: str, **fields: Any) -> None:
        if "options" in fields and not isinstance(fields["options"], str):
            fields["options"] = json.dumps(fields["options"])
        fields["updated_at"] = utcnow()
        sets = ",".join(f"{k}=?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE videos SET {sets} WHERE id=?", (*fields.values(), id))

    def set_video_status(self, id: str, status: str, error: str | None = None) -> None:
        self.update_video(id, status=status, error=error)

    # ---- clips ---------------------------------------------------------
    def upsert_clip(self, id: str, video_id: str, idx: int, start: float, end: float, score: float, hook: str) -> Clip:
        now = utcnow()
        with self.connect() as c:
            r = c.execute("SELECT * FROM clips WHERE id=?", (id,)).fetchone()
            if r:
                c.execute(
                    "UPDATE clips SET start=?,end=?,score=?,hook=?,updated_at=? WHERE id=?",
                    (start, end, score, hook, now, id),
                )
            else:
                c.execute(
                    "INSERT INTO clips(id,video_id,idx,start,end,score,hook,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (id, video_id, idx, start, end, score, hook, "candidate", now, now),
                )
            return _row_clip(c.execute("SELECT * FROM clips WHERE id=?", (id,)).fetchone())

    def get_clip(self, id: str) -> Clip | None:
        with self.connect() as c:
            r = c.execute("SELECT * FROM clips WHERE id=?", (id,)).fetchone()
            return _row_clip(r) if r else None

    def list_clips(self, video_id: str | None = None, status: str | list[str] | None = None) -> list[Clip]:
        where, args = [], []
        if video_id:
            where.append("video_id=?")
            args.append(video_id)
        if status is not None:
            st = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({','.join('?' * len(st))})")
            args.extend(st)
        sql = "SELECT * FROM clips" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY video_id, idx"
        with self.connect() as c:
            return [_row_clip(r) for r in c.execute(sql, args).fetchall()]

    def list_clips_recent(self, limit: int) -> list[Clip]:
        """The `limit` most recently created clips (deterministic: created_at DESC, id DESC), returned in (video, idx) order."""
        with self.connect() as c:
            rows = c.execute("SELECT * FROM clips ORDER BY created_at DESC, id DESC LIMIT ?", (int(limit),)).fetchall()
        return sorted((_row_clip(r) for r in rows), key=lambda x: (x.video_id, x.idx))

    def count_clips(self) -> int:
        with self.connect() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"])

    def update_clip(self, id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        sets = ",".join(f"{k}=?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), id))

    def set_clip_status(self, id: str, status: str, error: str | None = None) -> None:
        self.update_clip(id, status=status, error=error)

    # ---- posts ---------------------------------------------------------
    def is_posted(self, clip_id: str, platform: str) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM posts WHERE clip_id=? AND platform=? AND status='posted'", (clip_id, platform)).fetchone() is not None

    def get_post(self, clip_id: str, platform: str) -> Post | None:
        """Latest post row for (clip, platform), any status."""
        with self.connect() as c:
            r = c.execute("SELECT * FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1", (clip_id, platform)).fetchone()
            return _row_post(r) if r else None

    def ensure_post(self, clip_id: str, platform: str) -> Post:
        """Get-or-create the pending post row for (clip, platform). Never duplicates a posted row."""
        with self.connect() as c:
            r = c.execute("SELECT * FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1", (clip_id, platform)).fetchone()
            if r:
                return _row_post(r)
            c.execute("INSERT INTO posts(clip_id,platform,status,created_at) VALUES(?,?,?,?)", (clip_id, platform, "pending", utcnow()))
            r = c.execute("SELECT * FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1", (clip_id, platform)).fetchone()
            return _row_post(r)

    def claim_post(self, clip_id: str, platform: str, now_iso: str, stale_before_iso: str) -> bool:
        """Atomically take the latest (clip, platform) row as `uploading`; False when another process holds it.

        Claimable: `pending` and `failed` rows (an explicit retry of a final failure) and `uploading` rows whose claim
        is older than `stale_before_iso` (a process that died mid-upload must not block the clip forever).
        """
        with self.connect() as c:
            cur = c.execute(
                "UPDATE posts SET status='uploading', claimed_at=? "
                "WHERE id=(SELECT id FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1) "
                "AND status IN ('pending','failed','uploading') AND (status<>'uploading' OR claimed_at IS NULL OR claimed_at<?)",
                (now_iso, clip_id, platform, stale_before_iso),
            )
            return cur.rowcount == 1

    def reserve_and_claim(
        self,
        clip_id: str,
        platform: str,
        now_iso: str,
        stale_before_iso: str,
        *,
        day_start_iso: str,
        per_day: int,
        gap_before_iso: str | None = None,
    ) -> str:
        """Atomically check the platform's capacity and take the (clip, platform) row as `uploading`.

        Capacity counts posts completed since `day_start_iso` PLUS uploads other processes are holding a live claim
        on (claimed_at >= stale_before_iso), so two schedulers cannot both see "0 posted today" and each start one.
        `gap_before_iso` (optional) refuses when any post or live claim is newer than it (the min_gap rule).
        Returns "ok", "busy" (row held by another process), "per_day" or "gap". Nothing is written unless "ok".
        """
        with self.connect(immediate=True) as c:
            active = c.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE platform=? AND ("
                "(status='posted' AND posted_at>=?) OR (status='uploading' AND claimed_at IS NOT NULL AND claimed_at>=? AND clip_id<>?))",
                (platform, day_start_iso, stale_before_iso, clip_id),
            ).fetchone()["n"]
            if per_day >= 0 and active >= per_day:
                return "per_day"
            if gap_before_iso is not None:
                recent = c.execute(
                    "SELECT COUNT(*) AS n FROM posts WHERE platform=? AND clip_id<>? AND ("
                    "(status='posted' AND posted_at>?) OR (status='uploading' AND claimed_at IS NOT NULL AND claimed_at>? AND claimed_at>=?))",
                    (platform, clip_id, gap_before_iso, gap_before_iso, stale_before_iso),
                ).fetchone()["n"]
                if recent:
                    return "gap"
            cur = c.execute(
                "UPDATE posts SET status='uploading', claimed_at=? "
                "WHERE id=(SELECT id FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1) "
                "AND status IN ('pending','failed','uploading') AND (status<>'uploading' OR claimed_at IS NULL OR claimed_at<?)",
                (now_iso, clip_id, platform, stale_before_iso),
            )
            return "ok" if cur.rowcount == 1 else "busy"

    def count_active(self, platform: str, since_iso: str, stale_before_iso: str) -> int:
        """Posts completed since `since_iso` plus uploads with a live claim: what the daily allowance must count."""
        with self.connect() as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) AS n FROM posts WHERE platform=? AND ("
                    "(status='posted' AND posted_at>=?) OR (status='uploading' AND claimed_at IS NOT NULL AND claimed_at>=?))",
                    (platform, since_iso, stale_before_iso),
                ).fetchone()["n"]
            )

    def latest_activity(self, platform: str, stale_before_iso: str) -> str | None:
        """Newest of: last completed post, last live claim (an upload in flight counts for the min-gap rule)."""
        with self.connect() as c:
            r = c.execute(
                "SELECT MAX(t) AS m FROM (SELECT posted_at AS t FROM posts WHERE platform=? AND status='posted' "
                "UNION ALL SELECT claimed_at FROM posts WHERE platform=? AND status='uploading' AND claimed_at>=?)",
                (platform, platform, stale_before_iso),
            ).fetchone()
            return r["m"] if r and r["m"] else None

    def has_posted(self, clip_id: str) -> bool:
        """True when the clip is out on any platform (posted, or an upload is in flight): its file must never change."""
        with self.connect() as c:
            return c.execute("SELECT 1 FROM posts WHERE clip_id=? AND status IN ('posted','uploading') LIMIT 1", (clip_id,)).fetchone() is not None

    def release_claim(self, clip_id: str, platform: str) -> None:
        """Hand an `uploading` row back as `pending` (interrupted or declined upload: no verdict to record)."""
        with self.connect() as c:
            c.execute(
                "UPDATE posts SET status='pending', claimed_at=NULL "
                "WHERE id=(SELECT id FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1) AND status='uploading'",
                (clip_id, platform),
            )

    def delete_post(self, post_row_id: int) -> bool:
        """Remove a row that never recorded an attempt (pending/uploading with attempts 0); True when one was deleted."""
        with self.connect() as c:
            return c.execute("DELETE FROM posts WHERE id=? AND status IN ('pending','uploading') AND attempts=0", (post_row_id,)).rowcount == 1

    def mark_posted(self, clip_id: str, platform: str, post_id: str) -> None:
        now = utcnow()
        with self.connect() as c:
            r = c.execute("SELECT id FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1", (clip_id, platform)).fetchone()
            if r:
                c.execute("UPDATE posts SET status='posted', post_id=?, posted_at=?, error=NULL WHERE id=?", (post_id, now, r["id"]))
            else:
                c.execute(
                    "INSERT INTO posts(clip_id,platform,post_id,status,attempts,created_at,posted_at) VALUES(?,?,?,?,?,?,?)",
                    (clip_id, platform, post_id, "posted", 1, now, now),
                )

    def mark_post_failed(self, clip_id: str, platform: str, error: str, next_attempt_at: str | None, final: bool = False) -> None:
        with self.connect() as c:
            r = c.execute("SELECT id, attempts FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1", (clip_id, platform)).fetchone()
            if r is None:
                c.execute("INSERT INTO posts(clip_id,platform,status,created_at) VALUES(?,?,?,?)", (clip_id, platform, "pending", utcnow()))
                r = c.execute("SELECT id, attempts FROM posts WHERE clip_id=? AND platform=? ORDER BY id DESC LIMIT 1", (clip_id, platform)).fetchone()
            c.execute(
                "UPDATE posts SET status=?, attempts=attempts+1, error=?, next_attempt_at=? WHERE id=?",
                ("failed" if final else "pending", error[:2000], next_attempt_at, r["id"]),
            )

    def list_posts(self, platform: str | None = None, status: str | None = None) -> list[Post]:
        where, args = [], []
        if platform:
            where.append("platform=?")
            args.append(platform)
        if status:
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM posts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        with self.connect() as c:
            return [_row_post(r) for r in c.execute(sql, args).fetchall()]

    def posts_since(self, platform: str, since_iso: str) -> list[Post]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM posts WHERE platform=? AND status='posted' AND posted_at>=? ORDER BY posted_at", (platform, since_iso)
            ).fetchall()
            return [_row_post(r) for r in rows]

    def last_posted_at(self, platform: str) -> str | None:
        with self.connect() as c:
            r = c.execute("SELECT MAX(posted_at) AS m FROM posts WHERE platform=? AND status='posted'", (platform,)).fetchone()
            return r["m"] if r and r["m"] else None

    # ---- scheduled posts (per-clip date/time) -------------------------
    def add_scheduled(self, clip_id: str, platform: str, run_at_iso: str) -> Scheduled:
        """Queue clip_id for platform at run_at (UTC ISO). ValueError when a pending entry for the pair exists."""
        now = utcnow()
        with self.connect(immediate=True) as c:
            try:
                cur = c.execute(
                    "INSERT INTO scheduled (clip_id, platform, run_at, status, created_at, updated_at) VALUES (?,?,?,'pending',?,?)",
                    (clip_id, platform, run_at_iso, now, now),
                )
            except sqlite3.IntegrityError as err:
                if "scheduled_unique_pending" in str(err) or "UNIQUE" in str(err).upper():
                    raise ValueError(f"clip {clip_id} is already scheduled on {platform}; cancel that entry first") from None
                raise
            row = c.execute("SELECT * FROM scheduled WHERE id=?", (cur.lastrowid,)).fetchone()
            return _row_scheduled(row)

    def get_scheduled(self, id: int) -> Scheduled | None:
        with self.connect() as c:
            r = c.execute("SELECT * FROM scheduled WHERE id=?", (id,)).fetchone()
            return _row_scheduled(r) if r else None

    def list_scheduled(self, status: str | list[str] | None = None, platform: str | None = None, clip_id: str | None = None, limit: int | None = None) -> list[Scheduled]:
        """Entries ordered by run_at (pending ones read as the upcoming queue)."""
        where, args = [], []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        if platform:
            where.append("platform=?")
            args.append(platform)
        if clip_id:
            where.append("clip_id=?")
            args.append(clip_id)
        sql = "SELECT * FROM scheduled" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY run_at, id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as c:
            return [_row_scheduled(r) for r in c.execute(sql, args).fetchall()]

    def due_scheduled(self, now_iso: str) -> list[Scheduled]:
        """Pending entries whose time has come, earliest first."""
        with self.connect() as c:
            rows = c.execute("SELECT * FROM scheduled WHERE status='pending' AND run_at<=? ORDER BY run_at, id", (now_iso,)).fetchall()
            return [_row_scheduled(r) for r in rows]

    def reserved_clips(self, platform: str) -> set[str]:
        """Clips with a pending scheduled entry on `platform`: the slot scheduler must leave them alone."""
        with self.connect() as c:
            return {r["clip_id"] for r in c.execute("SELECT clip_id FROM scheduled WHERE status='pending' AND platform=?", (platform,)).fetchall()}

    def count_scheduled_between(self, platform: str, start_iso: str, end_iso: str, exclude_id: int | None = None) -> int:
        """Pending entries on `platform` with run_at in [start, end): the per-day cap must count them too."""
        with self.connect() as c:
            r = c.execute(
                "SELECT COUNT(*) AS n FROM scheduled WHERE status='pending' AND platform=? AND run_at>=? AND run_at<? AND id<>?",
                (platform, start_iso, end_iso, exclude_id if exclude_id is not None else -1),
            ).fetchone()
            return int(r["n"])

    def set_scheduled(self, id: int, status: str, *, post_id: str | None = None, error: str | None = None) -> None:
        with self.connect() as c:
            c.execute("UPDATE scheduled SET status=?, post_id=?, error=?, updated_at=? WHERE id=?", (status, post_id, error, utcnow(), id))

    def cancel_scheduled(self, id: int) -> bool:
        """Cancel a pending entry; False when it is not pending (already posted / failed / cancelled / unknown)."""
        with self.connect(immediate=True) as c:
            cur = c.execute("UPDATE scheduled SET status='cancelled', updated_at=? WHERE id=? AND status='pending'", (utcnow(), id))
            return cur.rowcount == 1

    # ---- budget --------------------------------------------------------
    def budget_used(self, platform: str, day: str) -> int:
        with self.connect() as c:
            r = c.execute("SELECT used FROM budget WHERE platform=? AND day=?", (platform, day)).fetchone()
            return int(r["used"]) if r else 0

    def budget_add(self, platform: str, day: str, amount: int) -> int:
        with self.connect() as c:
            c.execute(
                "INSERT INTO budget(platform,day,used) VALUES(?,?,?) ON CONFLICT(platform,day) DO UPDATE SET used=used+excluded.used",
                (platform, day, amount),
            )
            return int(c.execute("SELECT used FROM budget WHERE platform=? AND day=?", (platform, day)).fetchone()["used"])

    # ---- log / kv ------------------------------------------------------
    def log(self, action: str, detail: str = "", level: str = "info") -> None:
        with self.connect() as c:
            c.execute("INSERT INTO log(ts,level,action,detail) VALUES(?,?,?,?)", (utcnow(), level, action, detail[:4000]))

    def recent_log(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def kv_delete(self, key: str) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM kv WHERE key=?", (key,))
