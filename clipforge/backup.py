"""Backup and restore of a workspace: the SQLite state (online backup API, consistent while the daemon runs), the
config file, and optionally the media files. The manifest records counts so a restore can be verified.

Absolute paths stored in the DB (video sources, clip files, metadata) are rebased when a backup is restored into a
different workspace directory, so a scratch restore is readable by the application.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings
from .db import DB
from .log import get_logger

log = get_logger(__name__)

MANIFEST = "manifest.json"
DB_NAME = "clipforge.db"
CONFIG_NAME = "clipforge.yaml"
MEDIA_DIR = "workspace"
SKIP_DIRS = {"browser_profile"}  # browser cookies are never copied
SKIP_FILES = {DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", f"{DB_NAME}-journal"}


@dataclass
class Manifest:
    created_at: str
    version: str
    workspace: str  # absolute path the DB paths refer to
    counts: dict[str, int]
    with_media: bool
    files: int = 0
    bytes: int = 0
    db_bytes: int = 0
    notes: list[str] = field(default_factory=list)


def _counts(db_path: Path) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    try:
        return {t: int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in ("videos", "clips", "posts", "log")}
    finally:
        con.close()


def create_backup(settings: Settings, dest: Path, *, with_media: bool = False, config_path: Path | None = None) -> Manifest:
    """Write <dest>/clipforge.db (online copy), clipforge.yaml (when present), manifest.json and, with `with_media`,
    every workspace file except the DB files and the browser profile. Returns the manifest."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    src_db = settings.db_path
    if not src_db.exists():
        raise FileNotFoundError(f"no database at {src_db}")
    started = time.perf_counter()
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(dest / DB_NAME)
    try:
        src.backup(dst)  # page-level online copy: consistent even while another process writes
    finally:
        dst.close()
        src.close()
    notes: list[str] = []
    cfg = config_path if config_path is not None else Path(os.environ.get("CLIPFORGE_CONFIG") or CONFIG_NAME)
    if cfg.is_file():
        shutil.copy2(cfg, dest / CONFIG_NAME)
    else:
        notes.append("no config file found; defaults were in use")
    files = total = 0
    if with_media:
        root = settings.workspace_dir
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if not path.is_file() or rel.parts[0] in SKIP_DIRS or path.name in SKIP_FILES:
                continue
            target = dest / MEDIA_DIR / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            files += 1
            total += path.stat().st_size
        notes.append("workspace files include sign-in token files: keep the backup private")
    manifest = Manifest(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        version=__version__,
        workspace=str(settings.workspace_dir.resolve()),
        counts=_counts(dest / DB_NAME),
        with_media=with_media,
        files=files,
        bytes=total,
        db_bytes=(dest / DB_NAME).stat().st_size,
        notes=notes,
    )
    (dest / MANIFEST).write_text(json.dumps(asdict(manifest), indent=1), encoding="utf-8")
    log.info("backup written to %s in %.1f s (%s)", dest, time.perf_counter() - started, manifest.counts)
    return manifest


def read_manifest(src: Path) -> Manifest:
    data = json.loads((Path(src) / MANIFEST).read_text(encoding="utf-8"))
    return Manifest(**{k: v for k, v in data.items() if k in Manifest.__dataclass_fields__})


@dataclass
class RestoreReport:
    workspace: str
    counts: dict[str, int]
    expected: dict[str, int]
    integrity: str
    rebased_paths: int
    files_restored: int
    missing_clip_files: int
    seconds: float

    @property
    def ok(self) -> bool:
        return self.integrity == "ok" and self.counts == self.expected


def _rebase(db_path: Path, old_root: str, new_root: str) -> int:
    """Rewrite absolute paths that lived under the backed-up workspace so they point into the restored one."""
    if not old_root or os.path.normcase(old_root) == os.path.normcase(new_root):
        return 0
    con = sqlite3.connect(db_path)
    n = 0
    try:
        for table, column in (("videos", "path"), ("clips", "path"), ("clips", "meta_path")):
            rows = con.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
            for rowid, value in rows:
                if value and os.path.normcase(str(value)).startswith(os.path.normcase(old_root)):
                    con.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (new_root + str(value)[len(old_root) :], rowid))
                    n += 1
        con.commit()
    finally:
        con.close()
    return n


def restore_backup(src: Path, settings: Settings, *, force: bool = False) -> RestoreReport:
    """Restore <src> into settings' workspace. Refuses when a database already exists there unless `force`.
    Verifies integrity and row counts against the manifest and reports missing clip files."""
    src = Path(src)
    manifest = read_manifest(src)
    target_db = settings.db_path
    if target_db.exists() and not force:
        raise FileExistsError(f"{target_db} exists; pass --force to overwrite it (the current data will be lost)")
    started = time.perf_counter()
    workspace = settings.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(target_db) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(src / DB_NAME, target_db)
    files = 0
    media_root = src / MEDIA_DIR
    if media_root.is_dir():
        for path in media_root.rglob("*"):
            if path.is_file():
                target = workspace / path.relative_to(media_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                files += 1
    rebased = _rebase(target_db, manifest.workspace, str(workspace.resolve()))
    con = sqlite3.connect(target_db)
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        missing = sum(1 for (p,) in con.execute("SELECT path FROM clips WHERE path IS NOT NULL") if not Path(p).is_file())
    finally:
        con.close()
    DB(target_db)  # applies schema/index migrations of this version
    report = RestoreReport(str(workspace.resolve()), _counts(target_db), manifest.counts, integrity, rebased, files, missing, time.perf_counter() - started)
    log.info("restore from %s: %s", src, "ok" if report.ok else "MISMATCH")
    return report
