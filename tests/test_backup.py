"""Backup + restore round trip into a scratch workspace with path rebasing and manifest verification."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge import backup as B
from clipforge.config import Settings
from clipforge.db import DB
from test_scheduler import make_ready


def _seed(settings: Settings, db: DB) -> None:
    make_ready(db, settings.workspace_dir, "vid_00")
    make_ready(db, settings.workspace_dir, "vid_01", idx=1)
    db.mark_posted("vid_00", "youtube", "yt-1")
    (settings.workspace_dir / "youtube_token.json").write_text("{}", encoding="utf-8")
    (settings.workspace_dir / "browser_profile").mkdir()
    (settings.workspace_dir / "browser_profile" / "Cookies").write_bytes(b"secret")


def test_backup_and_restore_round_trip(settings: Settings, db: DB, tmp_path: Path):
    _seed(settings, db)
    cfg = tmp_path / "clipforge.yaml"
    cfg.write_text("style: clean\n", encoding="utf-8")
    dest = tmp_path / "bk"
    m = B.create_backup(settings, dest, with_media=True, config_path=cfg)
    assert m.counts["videos"] == 1 and m.counts["clips"] == 2 and m.counts["posts"] == 1 and m.with_media
    assert (dest / "clipforge.db").is_file() and (dest / "clipforge.yaml").is_file() and (dest / "manifest.json").is_file()
    assert (dest / "workspace" / "vid" / "clips" / "vid_00.mp4").is_file() and (dest / "workspace" / "youtube_token.json").is_file()
    assert not (dest / "workspace" / "browser_profile").exists() and not (dest / "workspace" / "clipforge.db").exists()
    assert json.loads((dest / "manifest.json").read_text())["workspace"] == str(settings.workspace_dir.resolve())

    scratch = Settings(paths={"workspace": str(tmp_path / "scratch"), "logs": str(tmp_path / "logs")})
    r = B.restore_backup(dest, scratch)
    assert r.ok and r.integrity == "ok" and r.counts == m.counts and r.files_restored >= 5 and r.missing_clip_files == 0
    assert r.rebased_paths == 4  # 2 clip paths + 2 meta paths (the seeded video has no stored media path)
    restored = DB(scratch.db_path)
    clip = restored.get_clip("vid_00")
    assert Path(clip.path).is_file() and Path(clip.path).is_relative_to(scratch.workspace_dir.resolve())
    assert restored.is_posted("vid_00", "youtube") and restored.get_video("vid").status == "done"


def test_restore_refuses_to_overwrite_without_force(settings: Settings, db: DB, tmp_path: Path):
    _seed(settings, db)
    dest = tmp_path / "bk"
    B.create_backup(settings, dest)
    with pytest.raises(FileExistsError, match="--force"):
        B.restore_backup(dest, settings)
    db.set_clip_status("vid_00", "rejected")  # diverge, then restore over it
    r = B.restore_backup(dest, settings, force=True)
    assert r.ok and DB(settings.db_path).get_clip("vid_00").status == "ready"


def test_backup_without_db_fails_cleanly(tmp_path: Path):
    s = Settings(paths={"workspace": str(tmp_path / "empty"), "logs": str(tmp_path / "logs")})
    with pytest.raises(FileNotFoundError):
        B.create_backup(s, tmp_path / "bk")
