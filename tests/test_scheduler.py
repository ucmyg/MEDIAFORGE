"""clipforge.scheduler: slot logic, min gap, limits, backoff, idempotency, dry run, daemon loop.

Time is frozen per test: `clock.now` is the aware "now" handed to tick() and clipforge.db.utcnow is patched to the
same instant, so DB timestamps (created_at, posted_at, next_attempt_at) line up with the schedule. A fixed +02:00
zone keeps everything independent of the machine's time zone. Publishers are fakes that record their calls; the
pipeline queue step is a recording no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

import clipforge.db as dbmod
from clipforge import scheduler as S
from clipforge.config import Settings
from clipforge.db import DB, Clip
from clipforge.metadata import ClipMeta, write_meta
from clipforge.publish.base import Limits, Publisher, PublishError, PublishFatal

TZ = timezone(timedelta(hours=2))
DAY = date(2026, 9, 15)
TIMES = ["09:00", "13:00", "18:00"]


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), tzinfo=TZ)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def utcnow(self) -> str:
        return self.now.astimezone(timezone.utc).isoformat(timespec="seconds")


class FakePublisher(Publisher):
    """Records publish calls; `failures` are raised one per call (then success); marks posted like the real ones."""

    def __init__(self, name: str, settings: Settings, db: DB, *, per_day: int = 5, configured: bool = True, records_failures: bool = False):
        super().__init__(settings, db)
        self.name = name
        self.per_day = per_day
        self.configured = configured
        self.records_failures = records_failures  # mimic the real publishers, which mark_post_failed themselves
        self.auth_ok = True
        self.failures: list[Exception] = []
        self.calls: list[str] = []

    def auth(self) -> bool:
        return self.auth_ok

    def is_configured(self) -> bool:
        return self.configured

    def limits(self) -> Limits:
        posted = len(self.db.list_posts(self.name, "posted"))  # all-time: a frozen clock has no meaningful "today"
        return Limits(per_day=self.per_day, posted_today=posted, remaining=max(0, self.per_day - posted), note="fake")

    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        self.calls.append(clip.id)
        self.ensure_not_posted(clip)
        assert meta.clip_id == clip.id
        if self.failures:
            err = self.failures.pop(0)
            if self.records_failures:
                self.db.mark_post_failed(clip.id, self.name, str(err), None, final=isinstance(err, PublishFatal))
            raise err
        post_id = f"{self.name}-{len(self.calls)}"
        self.db.mark_posted(clip.id, self.name, post_id)
        return post_id


def make_ready(db: DB, workspace: Path, clip_id: str, *, video_id: str = "vid", idx: int = 0, status: str = "ready") -> Clip:
    """A clip row with a stub mp4 + metadata json on disk; its video is `done` so process_queue leaves it alone."""
    if db.get_video(video_id) is None:
        db.add_video(video_id, "local", str(workspace / "src.mp4"), title="Source")
        db.set_video_status(video_id, "done")
    db.upsert_clip(clip_id, video_id, idx, 1.0, 9.0, 0.5, f"hook {clip_id}")
    clips_dir = workspace / video_id / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    mp4 = clips_dir / f"{clip_id}.mp4"
    mp4.write_bytes(b"\x00" * 8)
    meta = ClipMeta(title=f"Title {clip_id}", description="desc", hashtags={"youtube": ["#shorts"], "tiktok": ["#fyp"]}, clip_id=clip_id, video_id=video_id)
    meta_path = write_meta(clips_dir / f"{clip_id}.json", meta)
    db.update_clip(clip_id, status=status, path=str(mp4), meta_path=str(meta_path), duration=8.0)
    clip = db.get_clip(clip_id)
    assert clip is not None
    return clip


@dataclass
class Sched:
    settings: Settings
    db: DB
    clock: Clock
    publishers: dict[str, FakePublisher]
    queue_calls: int = 0
    ready: list[Clip] = field(default_factory=list)

    def tick(self, hhmm: str, *, day: date = DAY, **kw) -> S.TickResult:
        self.clock.now = at(hhmm, day)
        return S.tick(self.settings, self.db, now=self.clock.now, publishers=dict(self.publishers), **kw)

    def add_ready(self, *clip_ids: str) -> None:
        for i, clip_id in enumerate(clip_ids, start=len(self.ready)):
            self.ready.append(make_ready(self.db, self.settings.workspace_dir, clip_id, idx=i))

    def post(self, clip_id: str, platform: str):
        return self.db.get_post(clip_id, platform)


@pytest.fixture()
def clock(monkeypatch) -> Clock:
    c = Clock(at("08:00"))
    monkeypatch.setattr(dbmod, "utcnow", c.utcnow)
    return c


@pytest.fixture()
def sched(settings: Settings, db: DB, clock: Clock, monkeypatch) -> Sched:
    settings.schedule.times = list(TIMES)
    settings.schedule.min_gap_h = 2.0
    settings.schedule.backoff_base_s = 300
    settings.schedule.backoff_max_s = 6 * 3600
    settings.schedule.max_attempts = 3
    s = Sched(settings, db, clock, {"youtube": FakePublisher("youtube", settings, db)})

    def fake_queue(_settings, _db):
        s.queue_calls += 1
        return []

    monkeypatch.setattr(S.pipeline, "process_queue", fake_queue)
    return s


# ---- pure helpers ---------------------------------------------------------------------------------------------------
def test_parse_times_valid_sorted_deduplicated():
    assert S.parse_times(["18:00", "09:00", "9:00", "13:30"]) == [time(9, 0), time(13, 30), time(18, 0)]
    assert S.parse_times([]) == []


@pytest.mark.parametrize("bad", ["9am", "25:00", "09:60", "09:00:00", "", "noon"])
def test_parse_times_rejects_bad_entries(bad: str):
    with pytest.raises(ValueError, match=repr(bad)):
        S.parse_times(["09:00", bad])


def test_slots_and_due_slot():
    times = S.parse_times(TIMES)
    assert S.slots_for_day(times, DAY, TZ) == [at("09:00"), at("13:00"), at("18:00")]
    assert S.due_slot(at("08:59"), times, None) is None
    assert S.due_slot(at("09:00"), times, None) == at("09:00")
    assert S.due_slot(at("09:05"), times, at("09:02")) is None  # posted since the slot opened
    assert S.due_slot(at("13:05"), times, at("09:02")) == at("13:00")
    assert S.due_slot(at("18:30"), times, at("09:02")) == at("18:00")  # missed 13:00 collapses into the latest due slot
    assert S.due_slot(at("18:30"), times, at("18:00")) is None
    assert S.due_slot(at("09:05", DAY + timedelta(days=1)), times, at("18:05")) == at("09:00", DAY + timedelta(days=1))
    assert S.due_slot(at("12:00"), [], None) is None


def test_backoff_delay():
    assert [S.backoff_delay(n, 300, 6 * 3600) for n in range(8)] == [300, 600, 1200, 2400, 4800, 9600, 19200, 21600]
    assert S.backoff_delay(-1, 300, 6 * 3600) == 300


def test_tick_result_summary():
    r = S.TickResult(processed=["v1"], posted=[("c1", "youtube", "yt-1")], skipped=["tiktok: x"], errors=[])
    assert r.summary() == "tick: processed 1 video(s), posted 1 (c1 -> youtube yt-1), skipped 1, errors 0"
    assert S.TickResult(dry_run=True, posted=[("c1", "youtube", "")]).summary() == "tick: processed 0 video(s), would post 1 (c1 -> youtube), skipped 0, errors 0"


# ---- slot logic -----------------------------------------------------------------------------------------------------
def test_before_first_slot_skips(sched: Sched):
    sched.add_ready("vid_00")
    res = sched.tick("08:30")
    assert res.posted == [] and res.errors == [] and sched.queue_calls == 1
    assert res.skipped == ["youtube: before the first slot (09:00)"]
    assert sched.publishers["youtube"].calls == []


def test_posts_one_clip_per_slot_then_next_slot(sched: Sched):
    sched.add_ready("vid_00", "vid_01", "vid_02")
    first = sched.tick("09:05")
    assert first.posted == [("vid_00", "youtube", "youtube-1")] and first.errors == [] and first.skipped == []
    post = sched.post("vid_00", "youtube")
    assert post.status == "posted" and post.post_id == "youtube-1" and post.posted_at == sched.clock.utcnow()
    assert sched.db.get_clip("vid_00").status == "posted"
    again = sched.tick("09:05")
    assert again.posted == [] and again.skipped == ["youtube: already posted for the 09:00 slot (at 09:05); next slot 13:00"]
    assert sched.tick("12:59").posted == []
    second = sched.tick("13:00")
    assert second.posted == [("vid_01", "youtube", "youtube-2")]
    assert sched.publishers["youtube"].calls == ["vid_00", "vid_01"]
    actions = [r["action"] for r in sched.db.recent_log(20)]
    assert actions.count("schedule.posted") == 2 and actions.count("schedule.tick") == 2


def test_min_gap_blocks_slot_soon_after_previous_post(sched: Sched):
    sched.settings.schedule.times = ["09:00", "10:00"]
    sched.add_ready("vid_00", "vid_01")
    assert sched.tick("09:05").posted == [("vid_00", "youtube", "youtube-1")]
    blocked = sched.tick("10:05")
    assert blocked.posted == [] and blocked.skipped == ["youtube: slot 10:00 due but the last post at 09:05 is less than 2 h ago"]
    assert sched.tick("11:05").posted == [("vid_01", "youtube", "youtube-2")]


def test_manual_post_counts_towards_min_gap(sched: Sched):
    sched.add_ready("vid_00", "vid_01")
    sched.clock.now = at("12:30")
    sched.db.mark_posted("vid_00", "youtube", "by-hand")  # clipforge publish --now at 12:30
    assert sched.tick("13:05").posted == []
    assert sched.tick("14:30").posted == [("vid_01", "youtube", "youtube-1")]


def test_per_day_limit_blocks_via_limits(sched: Sched):
    sched.publishers["youtube"].per_day = 1
    sched.add_ready("vid_00", "vid_01")
    assert sched.tick("09:05").posted == [("vid_00", "youtube", "youtube-1")]
    blocked = sched.tick("13:05")
    assert blocked.posted == [] and blocked.skipped == ["youtube: slot 13:00 due but the daily allowance is exhausted (1/1 posted today; fake)"]
    assert sched.publishers["youtube"].calls == ["vid_00"]


def test_no_ready_clip_is_a_skip_reason(sched: Sched):
    res = sched.tick("09:05")
    assert res.posted == [] and res.skipped == ["youtube: slot 09:00 due but no ready clip to post"]
    make_ready(sched.db, sched.settings.workspace_dir, "vid_00", status="rendered")  # not approved yet
    assert sched.tick("09:06").posted == []


# ---- failures and backoff -------------------------------------------------------------------------------------------
def test_backoff_then_give_up(sched: Sched):
    pub = sched.publishers["youtube"]
    pub.failures = [PublishError("boom 1"), PublishError("boom 2"), PublishError("boom 3")]
    sched.add_ready("vid_00")
    first = sched.tick("09:05")
    assert first.posted == [] and first.errors == ["youtube: vid_00 failed (attempt 1/3, retry after 09:10): boom 1"]
    post = sched.post("vid_00", "youtube")
    assert post.status == "pending" and post.attempts == 1 and "boom 1" in post.error
    assert datetime.fromisoformat(post.next_attempt_at) == at("09:05") + timedelta(seconds=300)
    early = sched.tick("09:08")
    assert early.errors == [] and early.posted == [] and pub.calls == ["vid_00"]
    assert early.skipped == ["youtube: slot 09:00 due but no ready clip to post (vid_00 retries after 09:10)"]
    second = sched.tick("09:11")
    assert second.errors == ["youtube: vid_00 failed (attempt 2/3, retry after 09:21): boom 2"]
    assert datetime.fromisoformat(sched.post("vid_00", "youtube").next_attempt_at) == at("09:11") + timedelta(seconds=600)
    third = sched.tick("09:22")
    assert third.errors == ["youtube: vid_00 failed (attempt 3/3, giving up): boom 3"]
    post = sched.post("vid_00", "youtube")
    assert post.status == "failed" and post.attempts == 3 and post.next_attempt_at is None
    assert sched.db.get_clip("vid_00").status == "ready"  # still available to other platforms
    nothing = sched.tick("09:30")
    assert nothing.posted == [] and nothing.skipped == ["youtube: slot 09:00 due but no ready clip to post (vid_00 failed permanently after 3 attempt(s))"]
    sched.add_ready("vid_01")
    later = sched.tick("09:31")
    assert later.posted == [("vid_01", "youtube", "youtube-4")] and pub.calls == ["vid_00", "vid_00", "vid_00", "vid_01"]
    assert [r["action"] for r in sched.db.recent_log(50)].count("schedule.failed") == 3


def test_waiting_clip_does_not_block_the_slot(sched: Sched):
    pub = sched.publishers["youtube"]
    pub.failures = [PublishError("boom")]
    sched.add_ready("vid_00", "vid_01")
    assert sched.tick("09:05").errors == ["youtube: vid_00 failed (attempt 1/3, retry after 09:10): boom"]
    res = sched.tick("09:06")  # vid_00 is in backoff; the slot is still due, so the next ready clip goes out
    assert res.posted == [("vid_01", "youtube", "youtube-2")] and res.errors == []
    assert sched.tick("09:11").posted == []  # posting vid_01 satisfied the 09:00 slot; vid_00 waits for 13:00
    assert sched.tick("13:05").posted == [("vid_00", "youtube", "youtube-3")] and pub.calls == ["vid_00", "vid_01", "vid_00"]


def test_backoff_is_capped(sched: Sched):
    sched.settings.schedule.backoff_max_s = 400
    sched.settings.schedule.max_attempts = 5
    sched.publishers["youtube"].failures = [PublishError("a"), PublishError("b")]
    sched.add_ready("vid_00")
    sched.tick("09:05")
    sched.tick("09:11")
    assert datetime.fromisoformat(sched.post("vid_00", "youtube").next_attempt_at) == at("09:11") + timedelta(seconds=400)


def test_publisher_recorded_failures_are_not_double_counted(sched: Sched):
    pub = FakePublisher("youtube", sched.settings, sched.db, records_failures=True)
    pub.failures = [PublishError("x"), PublishError("y"), PublishError("z")]
    sched.publishers["youtube"] = pub
    sched.add_ready("vid_00")
    sched.tick("09:05")
    post = sched.post("vid_00", "youtube")
    assert post.attempts == 1 and post.status == "pending" and datetime.fromisoformat(post.next_attempt_at) == at("09:10")
    sched.tick("09:11")
    assert sched.post("vid_00", "youtube").attempts == 2
    sched.tick("09:22")
    post = sched.post("vid_00", "youtube")
    assert post.attempts == 3 and post.status == "failed" and post.next_attempt_at is None
    assert sched.tick("13:05").posted == [] and pub.calls == ["vid_00"] * 3


def test_publisher_non_final_verdict_is_respected(sched: Sched):
    """A publisher may downgrade a PublishFatal to retryable (YouTube quota exhausted = retry tomorrow)."""
    pub = FakePublisher("youtube", sched.settings, sched.db, records_failures=True)

    class QuotaGone(PublishFatal):
        pass

    def record_soft(clip, err):
        sched.db.mark_post_failed(clip.id, "youtube", str(err), None, final=False)

    pub.failures = [QuotaGone("quota")]
    pub.records_failures = False
    original = pub.publish

    def publish(clip, meta):
        try:
            return original(clip, meta)
        except QuotaGone as err:
            record_soft(clip, err)
            raise

    pub.publish = publish  # type: ignore[method-assign]
    sched.publishers["youtube"] = pub
    sched.add_ready("vid_00")
    sched.tick("09:05")
    post = sched.post("vid_00", "youtube")
    assert post.status == "pending" and post.attempts == 1 and post.next_attempt_at is not None


def test_publish_fatal_is_final_immediately(sched: Sched):
    pub = sched.publishers["youtube"]
    pub.failures = [PublishFatal("rejected file")]
    sched.add_ready("vid_00", "vid_01")
    res = sched.tick("09:05")
    assert res.errors == ["youtube: vid_00 failed (attempt 1/3, giving up): rejected file"] and res.posted == []
    post = sched.post("vid_00", "youtube")
    assert post.status == "failed" and post.attempts == 1 and post.next_attempt_at is None
    assert sched.tick("09:06").posted == [("vid_01", "youtube", "youtube-2")]
    assert sched.tick("13:05").posted == [] and pub.calls == ["vid_00", "vid_01"]


def test_unexpected_exception_is_retried_like_publish_error(sched: Sched):
    sched.publishers["youtube"].failures = [RuntimeError("network cable eaten")]
    sched.add_ready("vid_00")
    res = sched.tick("09:05")
    assert res.errors == ["youtube: vid_00 failed (attempt 1/3, retry after 09:10): network cable eaten"]
    assert sched.post("vid_00", "youtube").status == "pending"
    assert sched.tick("09:11").posted == [("vid_00", "youtube", "youtube-2")]


def test_missing_metadata_is_fatal(sched: Sched):
    sched.add_ready("vid_00")
    Path(sched.db.get_clip("vid_00").meta_path).unlink()
    res = sched.tick("09:05")
    assert res.posted == [] and "no metadata file" in res.errors[0] and sched.post("vid_00", "youtube").status == "failed"
    assert sched.publishers["youtube"].calls == []


def test_allowance_exhausted_inside_publish_clip_records_nothing(sched: Sched):
    pub = sched.publishers["youtube"]
    pub.per_day = 0
    sched.add_ready("vid_00")
    with pytest.raises(PublishFatal, match="allowance exhausted"):
        S.publish_clip(sched.settings, sched.db, pub, sched.ready[0], ["youtube"], now=at("09:05"))
    assert sched.post("vid_00", "youtube") is None and pub.calls == []


# ---- idempotency / multi-platform -----------------------------------------------------------------------------------
def test_never_posts_the_same_clip_twice_on_a_platform(sched: Sched):
    sched.add_ready("vid_00", "vid_01")
    sched.tick("09:05")
    sched.db.set_clip_status("vid_00", "ready")  # even if someone flips the clip back to ready
    assert sched.tick("13:05").posted == [("vid_01", "youtube", "youtube-2")]
    assert sched.tick("18:05").posted == [] and sched.tick("09:05", day=DAY + timedelta(days=1)).posted == []
    assert sched.publishers["youtube"].calls == ["vid_00", "vid_01"]
    assert len(sched.db.list_posts("youtube", "posted")) == 2


def test_dry_run_posts_nothing(sched: Sched):
    sched.add_ready("vid_00")
    res = sched.tick("09:05", dry_run=True)
    assert res.dry_run and res.posted == [("vid_00", "youtube", "")] and "would post 1 (vid_00 -> youtube)" in res.summary()
    assert sched.publishers["youtube"].calls == [] and sched.db.list_posts() == []
    assert sched.db.get_clip("vid_00").status == "ready"
    assert sched.tick("09:06", dry_run=True).posted == [("vid_00", "youtube", "")]  # still due, still not posted


def test_unconfigured_publisher_is_skipped_unless_platforms_are_explicit(sched: Sched):
    sched.publishers["tiktok"] = FakePublisher("tiktok", sched.settings, sched.db, configured=False)
    sched.add_ready("vid_00", "vid_01")
    res = sched.tick("09:05")
    assert res.posted == [("vid_00", "youtube", "youtube-1")]
    assert res.skipped == ["tiktok: not configured (run `clipforge auth tiktok` or set platforms.tiktok in clipforge.yaml)"]
    assert sched.db.get_clip("vid_00").status == "posted"  # tiktok was not active, so youtube alone completes the clip
    explicit = sched.tick("09:06", platforms=["tiktok"])  # explicit platforms bypass the is_configured filter
    assert explicit.posted == [("vid_01", "tiktok", "tiktok-1")] and explicit.skipped == []


def test_clip_posted_only_once_every_active_platform_has_it(sched: Sched):
    sched.publishers["tiktok"] = FakePublisher("tiktok", sched.settings, sched.db, per_day=1)
    sched.add_ready("vid_00", "vid_01")
    res = sched.tick("09:05")
    assert res.posted == [("vid_00", "youtube", "youtube-1"), ("vid_00", "tiktok", "tiktok-1")]
    assert sched.db.get_clip("vid_00").status == "posted"
    res = sched.tick("13:05")
    assert res.posted == [("vid_01", "youtube", "youtube-2")] and "tiktok: slot 13:00 due but the daily allowance is exhausted" in res.skipped[0]
    assert sched.db.get_clip("vid_01").status == "ready"  # tiktok still owes this one
    assert sched.tick("18:05").posted == []  # youtube has nothing left; tiktok is still out of allowance
    sched.publishers["tiktok"].per_day = 2
    res = sched.tick("18:06")
    assert res.posted == [("vid_01", "tiktok", "tiktok-2")] and sched.db.get_clip("vid_01").status == "posted"


def test_unknown_platform_is_an_error_not_a_crash(sched: Sched):
    sched.add_ready("vid_00")
    res = sched.tick("09:05", platforms=["myspace", "youtube"])
    assert res.errors == ["myspace: unknown platform 'myspace'; choose from ('youtube', 'tiktok')"]
    assert res.posted == [("vid_00", "youtube", "youtube-1")]


# ---- queue step / config errors -------------------------------------------------------------------------------------
def test_queue_processing_error_does_not_stop_posting(sched: Sched, monkeypatch):
    def broken(_settings, _db):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(S.pipeline, "process_queue", broken)
    sched.add_ready("vid_00")
    res = sched.tick("09:05")
    assert res.errors == ["queue processing failed: RuntimeError: disk on fire"]
    assert res.posted == [("vid_00", "youtube", "youtube-1")]


def test_queue_reports_land_in_result(sched: Sched, monkeypatch):
    from clipforge.pipeline import ProcessReport

    monkeypatch.setattr(S.pipeline, "process_queue", lambda s, d: [ProcessReport("v1", "done", []), ProcessReport("v2", "failed", [], error="no audio")])
    res = sched.tick("08:00")
    assert res.processed == ["v1", "v2"] and res.errors == ["video v2: no audio"]
    assert sched.db.recent_log(1)[0]["action"] == "schedule.tick"


def test_invalid_schedule_times_is_an_error(sched: Sched):
    sched.settings.schedule.times = ["09:00", "half past nine"]
    sched.add_ready("vid_00")
    res = sched.tick("09:05")
    assert res.posted == [] and res.errors == ["schedule.times: invalid entry 'half past nine' (expected HH:MM, e.g. '09:00')"]
    assert sched.publishers["youtube"].calls == []


def test_naive_now_is_treated_as_local(sched: Sched, monkeypatch):
    seen: list[datetime] = []
    monkeypatch.setattr(S, "_tick_platform", lambda settings, db, pub, times, now, active, result: seen.append(now))
    S.tick(sched.settings, sched.db, now=datetime(2026, 9, 15, 9, 5), publishers=dict(sched.publishers))
    assert seen and seen[0].tzinfo is not None and seen[0].replace(tzinfo=None) == datetime(2026, 9, 15, 9, 5)


# ---- daemon ---------------------------------------------------------------------------------------------------------
def test_daemon_ticks_and_survives_a_crashing_tick(sched: Sched, monkeypatch, capsys):
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_tick(settings, db):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("tick exploded")
        return S.TickResult(posted=[("vid_00", "youtube", "yt-1")])

    monkeypatch.setattr(S, "tick", fake_tick)
    monkeypatch.setattr(S, "get_publisher", lambda name, settings, db, **kw: sched.publishers.get(name) or FakePublisher(name, settings, db, configured=False))
    sched.settings.schedule.tick_s = 7
    assert S.daemon(sched.settings, sched.db, ticks=3, sleep=sleeps.append) == 3
    assert calls == [0, 1, 2] and sleeps == [7, 7]  # no sleep after the last tick
    out = capsys.readouterr().out
    assert "clipforge daemon" in out and "configured: youtube" in out and out.count("posted 1 (vid_00 -> youtube yt-1)") == 2
    assert any(r["action"] == "schedule.tick" and "tick exploded" in r["detail"] for r in sched.db.recent_log(10))


def test_daemon_stops_on_keyboard_interrupt(sched: Sched, monkeypatch, capsys):
    monkeypatch.setattr(S, "tick", lambda settings, db: S.TickResult())
    monkeypatch.setattr(S, "get_publisher", lambda name, settings, db, **kw: FakePublisher(name, settings, db, configured=False))

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert S.daemon(sched.settings, sched.db, sleep=interrupt) == 1
    assert "stopped" in capsys.readouterr().out
