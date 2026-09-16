"""clipforge.cli publish / auth / tick / daemon and the doctor credential rows, with fake publishers.

`clipforge.publish.get_publisher` (used by publish/auth) and `clipforge.scheduler.get_publisher` (used by tick/daemon)
are replaced by a factory handing out FakePublisher instances from tests/test_scheduler.py, so nothing talks to a
network. The schedule slot is 00:00 so a tick is always due whatever the wall-clock says.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from test_scheduler import FakePublisher, make_ready
from typer.testing import CliRunner

import clipforge.publish as P
import clipforge.scheduler as S
from clipforge import cli as C
from clipforge.cli import EXIT_FAILED, EXIT_USAGE, app
from clipforge.config import Settings
from clipforge.db import DB
from clipforge.publish.base import PublishError, PublishFatal

RUNNER = CliRunner()


@dataclass
class Factory:
    """Stands in for get_publisher; remembers every instance it made and the flags it was called with."""

    settings: Settings
    db: DB
    made: dict[str, FakePublisher] = field(default_factory=dict)
    flags: list[tuple[str, bool, bool]] = field(default_factory=list)
    auth_ok: bool = True
    failures: dict[str, list[Exception]] = field(default_factory=dict)

    def __call__(self, name: str, settings: Settings, db: DB, *, manual: bool = False, browser: bool = False) -> FakePublisher:
        if name not in P.PLATFORMS:
            raise ValueError(f"unknown platform {name!r}; choose from {P.PLATFORMS}")
        self.flags.append((name, manual, browser))
        pub = FakePublisher(name, settings, db)
        pub.auth_ok = self.auth_ok
        pub.failures = list(self.failures.get(name, []))
        self.made[name] = pub
        return pub


@dataclass
class Cli:
    root: Path
    env: dict[str, str]
    db: DB
    factory: Factory

    def __call__(self, *args: str):
        return RUNNER.invoke(app, list(args), env=self.env)

    def ready(self, *clip_ids: str) -> None:
        for i, clip_id in enumerate(clip_ids):
            make_ready(self.db, self.root / "workspace", clip_id, idx=i)


@pytest.fixture()
def cli(tmp_path: Path, monkeypatch) -> Cli:
    import os

    for k in list(os.environ):
        if k.startswith("CLIPFORGE_"):
            monkeypatch.delenv(k)
    cfg = {"paths": {"workspace": str(tmp_path / "workspace"), "logs": str(tmp_path / "logs")}, "schedule": {"times": ["00:00"], "tick_s": 5}}
    (tmp_path / "clipforge.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    db = DB(tmp_path / "workspace" / "clipforge.db")
    factory = Factory(Settings(paths=cfg["paths"]), db)
    monkeypatch.setattr(P, "get_publisher", factory)
    monkeypatch.setattr(S, "get_publisher", factory)
    return Cli(tmp_path, {"CLIPFORGE_CONFIG": str(tmp_path / "clipforge.yaml")}, db, factory)


# ---- publish ----------------------------------------------------------------------------------------------------------
def test_publish_now_manual_posts_ready_clips_and_prints_table(cli: Cli):
    cli.ready("vid_00", "vid_01")
    res = cli("publish", "--to", "youtube,tiktok", "--now", "--manual")
    assert res.exit_code == 0, res.output
    assert cli.factory.flags == [("youtube", True, False), ("tiktok", True, False)]
    assert cli.factory.made["youtube"].calls == ["vid_00", "vid_01"] and cli.factory.made["tiktok"].calls == ["vid_00", "vid_01"]
    out = res.output
    assert "publish summary" in out and out.count("posted") >= 4 and "youtube-1" in out and "tiktok-2" in out
    for clip_id in ("vid_00", "vid_01"):
        assert cli.db.get_clip(clip_id).status == "posted"
        assert cli.db.is_posted(clip_id, "youtube") and cli.db.is_posted(clip_id, "tiktok")
    assert [r["action"] for r in cli.db.recent_log(20)].count("publish.posted") == 4
    again = cli("publish", "--to", "youtube", "--now", "--manual", "--clip-id", "vid_00")
    assert again.exit_code == 0 and "already posted" in again.output and cli.factory.made["youtube"].calls == []
    assert len(cli.db.list_posts(status="posted")) == 4


def test_publish_browser_flag_reaches_the_factory(cli: Cli):
    cli.ready("vid_00")
    res = cli("publish", "--to", "tiktok", "--now", "--i-accept-the-risk")
    assert res.exit_code == 0, res.output
    assert cli.factory.flags == [("tiktok", False, True)] and cli.db.is_posted("vid_00", "tiktok")


def test_publish_clip_id_unknown_or_not_ready(cli: Cli):
    res = cli("publish", "--to", "youtube", "--now", "--manual", "--clip-id", "ghost")
    assert res.exit_code == EXIT_USAGE and "unknown clip ghost" in res.output
    make_ready(cli.db, cli.root / "workspace", "vid_00", status="candidate")
    res = cli("publish", "--to", "youtube", "--now", "--manual", "--clip-id", "vid_00")
    assert res.exit_code == EXIT_FAILED and "review --approve-all" in res.output and cli.factory.made == {}
    cli.db.set_clip_status("vid_00", "rendered")  # rendered-but-unreviewed is allowed when named explicitly
    res = cli("publish", "--to", "youtube", "--now", "--manual", "--clip-id", "vid_00")
    assert res.exit_code == 0 and cli.factory.made["youtube"].calls == ["vid_00"]


def test_publish_nothing_ready(cli: Cli):
    res = cli("publish", "--to", "youtube", "--now", "--manual")
    assert res.exit_code == 0 and "nothing to publish" in res.output and cli.factory.made == {}


def test_publish_schedule_leaves_clips_for_the_daemon(cli: Cli):
    cli.ready("vid_00", "vid_01", "vid_02")
    cli.db.mark_posted("vid_02", "youtube", "yt-x")
    cli.db.set_clip_status("vid_02", "posted")
    res = cli("publish", "--to", "youtube,tiktok")
    assert res.exit_code == 0 and "2 ready clip(s) left for the scheduler" in res.output and cli.factory.made == {}
    assert cli("publish", "--to", "myspace").exit_code == EXIT_USAGE
    assert cli("publish", "--to", " ").exit_code == EXIT_USAGE


def test_publish_auth_failure_exits_before_posting(cli: Cli):
    cli.ready("vid_00")
    cli.factory.auth_ok = False
    res = cli("publish", "--to", "youtube", "--now")
    assert res.exit_code == EXIT_FAILED and "clipforge auth youtube" in res.output and "--manual" in res.output
    assert cli.factory.made["youtube"].calls == [] and cli.db.list_posts() == []


def test_publish_errors_are_reported_and_set_exit_code(cli: Cli):
    cli.ready("vid_00", "vid_01")
    cli.factory.failures = {"youtube": [PublishError("upload timed out"), PublishFatal("rejected")]}
    res = cli("publish", "--to", "youtube", "--now", "--manual")
    assert res.exit_code == EXIT_FAILED, res.output
    assert "upload timed out" in res.output and "rejected" in res.output and "Traceback" not in res.output
    assert cli.db.get_post("vid_00", "youtube").status == "pending" and cli.db.get_post("vid_01", "youtube").status == "failed"
    cli.factory.failures = {"youtube": [PublishError("flaky")]}
    res = cli("publish", "--to", "youtube", "--now", "--manual")  # vid_00 retried and fails again; vid_01 is final
    assert res.exit_code == EXIT_FAILED and cli.factory.made["youtube"].calls == ["vid_00"]
    assert "failed earlier" in res.output and "--clip-id vid_01" in res.output.replace("\n", "")
    assert cli.db.get_post("vid_00", "youtube").attempts == 2
    cli.factory.failures = {}
    res = cli("publish", "--to", "youtube", "--now", "--manual")
    assert res.exit_code == 0 and cli.db.is_posted("vid_00", "youtube") and not cli.db.is_posted("vid_01", "youtube")
    assert cli.db.get_clip("vid_00").status == "posted" and cli.db.get_clip("vid_01").status == "ready"
    mixed = cli("publish", "--to", "tiktok", "--now", "--manual")  # vid_00 is posted on youtube only: still due on tiktok
    assert mixed.exit_code == 0 and cli.factory.made["tiktok"].calls == ["vid_00", "vid_01"]
    retry = cli("publish", "--to", "youtube", "--now", "--manual", "--clip-id", "vid_01")  # explicit retry of a final failure
    assert retry.exit_code == 0 and cli.db.is_posted("vid_01", "youtube") and cli.db.get_clip("vid_01").status == "posted"
    assert "nothing to publish" in cli("publish", "--to", "youtube,tiktok", "--now", "--manual").output  # everything is out


# ---- auth ---------------------------------------------------------------------------------------------------------------
def test_auth_command(cli: Cli):
    ok = cli("auth", "YouTube")
    assert ok.exit_code == 0 and "authenticated" in ok.output and cli.factory.flags == [("youtube", False, False)]
    cli.factory.auth_ok = False
    bad = cli("auth", "tiktok")
    assert bad.exit_code == EXIT_FAILED and "authentication failed" in bad.output
    assert cli("auth", "myspace").exit_code == EXIT_USAGE


# ---- tick / daemon --------------------------------------------------------------------------------------------------------
def test_tick_dry_run_prints_summary(cli: Cli):
    cli.ready("vid_00")
    res = cli("-v", "tick", "--dry-run")
    assert res.exit_code == 0, res.output
    assert "would post 2 (vid_00 -> youtube, vid_00 -> tiktok)" in res.output.replace("\n", "")
    assert cli.db.list_posts() == [] and cli.db.get_clip("vid_00").status == "ready"
    res = cli("tick")
    assert res.exit_code == 0 and "posted 2 (vid_00 -> youtube youtube-1, vid_00 -> tiktok tiktok-1)" in res.output.replace("\n", "")
    assert cli.db.get_clip("vid_00").status == "posted"
    quiet = cli("tick")
    assert quiet.exit_code == 0 and "posted 0" in quiet.output and "skipped 2" in quiet.output and "already posted" not in quiet.output
    verbose = cli("-v", "tick")
    assert "skipped: youtube: already posted for the 00:00 slot" in verbose.output.replace("\n", "")


def test_daemon_ticks_option(cli: Cli, monkeypatch):
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    cli.ready("vid_00")
    res = cli("daemon", "--ticks", "2")
    assert res.exit_code == 0, res.output
    out = res.output.replace("\n", "")
    assert "clipforge daemon: tick every 5s" in out and "configured: youtube, tiktok" in out
    assert "posted 2" in out and sleeps == [5] and cli.db.get_clip("vid_00").status == "posted"


# ---- doctor credential rows ---------------------------------------------------------------------------------------------
def test_doctor_token_rows_are_file_checks(settings: Settings):
    rows = {c.name: c for c in C._check_credentials(settings)}
    assert set(rows) == {"youtube client_secret", "youtube token", "tiktok client_key", "tiktok token"}
    assert rows["youtube token"].status == "WARN" and "clipforge auth youtube" in rows["youtube token"].detail
    assert rows["tiktok token"].status == "WARN" and "clipforge auth tiktok" in rows["tiktok token"].detail
    settings.platform_path(settings.platforms.youtube.token_file).write_text("{}", encoding="utf-8")
    settings.platform_path(settings.platforms.tiktok.token_file).write_text("{}", encoding="utf-8")
    rows = {c.name: c for c in C._check_credentials(settings)}
    assert rows["youtube token"].status == "OK" and rows["tiktok token"].status == "OK"
    assert rows["youtube client_secret"].status == "WARN" and rows["tiktok client_key"].status == "WARN"
