"""SQLite state. One file, WAL mode, short-lived connections (safe for the process pool + daemon).

Tables: videos, clips, posts, budget, log, kv.  All timestamps are ISO-8601 UTC strings.
Idempotency rules enforced here: a video id is unique; (clip_id, platform) can only be `posted` once.
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
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|posted|failed
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  posted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS posts_unique_posted ON posts(clip_id, platform) WHERE status = 'posted';
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
"""


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

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN")
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
