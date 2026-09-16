"""clipforge.ui: the FastAPI backend behind the local web UI, driven through fastapi's TestClient.

Everything but the last test is offline and fast: the pipeline is replaced by a fake that writes stub clips (mp4
bytes + metadata json) so a `run` job finishes in milliseconds, publishers are fakes, scheduler.tick is a recording
stub. The last test runs the REAL pipeline on the 40 s fixture through POST /api/run (a few seconds).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import clipforge.publish as P
from clipforge import pipeline
from clipforge import scheduler as S
from clipforge.config import Settings
from clipforge.db import DB, Clip
from clipforge.metadata import ClipMeta, read_meta, write_meta
from clipforge.publish.base import Limits, Publisher, PublishError
from clipforge.publish.manual import UPLOAD_PAGES, build_caption
from clipforge.publish.tiktok import TikTokPublisher
from clipforge.publish.youtube import YouTubePublisher
from clipforge.ui import allowed_hosts_for, background
from clipforge.ui.jobs import AUTH_LANE, JobFailed, JobRunner
from clipforge.ui.server import ANY_HOST, CSRF_HEADER, create_app

JOB_WAIT_S = 10.0
E2E_WAIT_S = 60.0
POLL_S = 0.02
VID = "vid"
BASE_URL = "http://127.0.0.1"  # the request guard rejects TestClient's default Host 'testserver'
UI_HEADERS = {CSRF_HEADER: "1"}  # what app.js sends on every request; POST/PUT/DELETE are refused without it
TAGS = {"youtube": ["#shorts", "#one", "#two", "#three", "#four"], "tiktok": ["#fyp", "#one", "#two", "#three", "#four"]}


# ---- helpers ---------------------------------------------------------------------------------------------------------
def stub_clip(db: DB, workspace: Path, clip_id: str, *, video_id: str = VID, idx: int = 0, status: str = "ready", size: int = 1000) -> Clip:
    """A clip row with a stub mp4 (`size` bytes) + metadata json on disk; its video is `done`."""
    if db.get_video(video_id) is None:
        db.add_video(video_id, "local", str(workspace / "src.mp4"), title="Source")
        db.set_video_status(video_id, "done")
    db.upsert_clip(clip_id, video_id, idx, 1.0, 9.0, 0.5, f"hook {clip_id}")
    clips_dir = workspace / video_id / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    mp4 = clips_dir / f"{clip_id}.mp4"
    mp4.write_bytes(bytes(range(256)) * (size // 256) + bytes(size % 256))
    meta = ClipMeta(title=f"Title {clip_id}", description=f"desc {clip_id}\n\n#shorts", hashtags={"youtube": ["#shorts", "#a", "#b"], "tiktok": ["#fyp", "#a", "#b"]}, hook=f"hook {clip_id}", topics=["a", "b"], clip_id=clip_id, video_id=video_id)
    meta_path = write_meta(clips_dir / f"{clip_id}.json", meta)
    db.update_clip(clip_id, status=status, path=str(mp4), meta_path=str(meta_path), duration=8.0)
    clip = db.get_clip(clip_id)
    assert clip is not None
    return clip


def fake_pipeline(monkeypatch, settings: Settings, *, clips: int = 2, fail: bool = False, calls: list | None = None):
    """pipeline.process_video/process_queue -> stub clips (or a failed report) without ffmpeg."""

    def process_video(video_id: str, settings_: Settings, db: DB, *, force: bool = False) -> pipeline.ProcessReport:
        if calls is not None:
            calls.append((video_id, force))
        if fail:
            db.set_video_status(video_id, "failed", "boom")
            return pipeline.ProcessReport(video_id, "failed", [], error="boom")
        for i in range(clips):
            stub_clip(db, settings_.workspace_dir, f"{video_id}_{i:02d}", video_id=video_id, idx=i, status="rendered")
        db.set_video_status(video_id, "done")
        return pipeline.ProcessReport(video_id, "done", db.list_clips(video_id))

    def process_queue(settings_: Settings, db: DB) -> list[pipeline.ProcessReport]:
        return [process_video(v.id, settings_, db) for v in db.list_videos() if v.status not in pipeline.TERMINAL_STATUSES]

    monkeypatch.setattr(pipeline, "process_video", process_video)
    monkeypatch.setattr(pipeline, "process_queue", process_queue)


def wait_job(client: TestClient, job_id: str, timeout: float = JOB_WAIT_S, statuses: tuple[str, ...] = ("done", "failed")) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = {j["id"]: j for j in client.get("/api/state").json()["jobs"]}
        job = jobs.get(job_id)
        if job and job["status"] in statuses:
            return job
        time.sleep(POLL_S)
    raise AssertionError(f"job {job_id} did not reach {statuses} within {timeout}s")


def wait_for(predicate, timeout: float = JOB_WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(POLL_S)


def state_clips(client: TestClient) -> dict[str, dict]:
    return {c["id"]: c for c in client.get("/api/state").json()["clips"]}


class FakePublisher(Publisher):
    """Configured, authenticated, records publish calls; `failures` are raised one per call."""

    def __init__(self, name: str, settings: Settings, db: DB, *, configured: bool = True):
        super().__init__(settings, db)
        self.name = name
        self.configured = configured
        self.failures: list[BaseException] = []
        self.calls: list[str] = []

    def auth(self) -> bool:
        return self.configured

    def is_configured(self) -> bool:
        return self.configured

    def limits(self) -> Limits:
        posted = len(self.db.list_posts(self.name, "posted"))
        return Limits(per_day=5, posted_today=posted, remaining=max(0, 5 - posted), note="fake")

    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        self.calls.append(clip.id)
        self.ensure_not_posted(clip)
        if self.failures:
            raise self.failures.pop(0)
        post_id = f"{self.name}-{len(self.calls)}"
        self.db.mark_posted(clip.id, self.name, post_id)
        return post_id


# ---- fixtures --------------------------------------------------------------------------------------------------------
@pytest.fixture()
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><html><title>ClipForge</title><body>ui</body></html>", encoding="utf-8")
    (d / "app.js").write_text("console.log('ok');\n", encoding="utf-8")
    (d / "style.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    return d


@pytest.fixture()
def api(settings: Settings, db: DB, static_dir: Path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_TEST_SKIP_WHISPER", "1")
    app = create_app(settings, db, static_dir=static_dir)
    with TestClient(app, base_url=BASE_URL, headers=UI_HEADERS) as client:
        yield client


@pytest.fixture()
def publishers(settings: Settings, db: DB, monkeypatch) -> dict[str, FakePublisher]:
    fakes = {"youtube": FakePublisher("youtube", settings, db), "tiktok": FakePublisher("tiktok", settings, db, configured=False)}
    monkeypatch.setattr(P, "get_publisher", lambda name, settings_, db_, **kw: fakes[name])
    return fakes


# ---- pages -----------------------------------------------------------------------------------------------------------
def test_index_and_static(api: TestClient):
    page = api.get("/")
    assert page.status_code == 200 and "ClipForge" in page.text and page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache" and page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    js = api.get("/static/app.js")
    assert js.status_code == 200 and "console.log" in js.text
    # no version suffix on the asset URLs: browsers must revalidate instead of applying heuristic freshness after an upgrade
    assert js.headers["cache-control"] == "no-cache" and api.get("/static/style.css").headers["cache-control"] == "no-cache"
    unchanged = api.get("/static/app.js", headers={"If-None-Match": js.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.headers["cache-control"] == "no-cache"
    assert api.get("/static/missing.css").status_code == 404
    assert api.get("/docs").status_code == 404  # no CDN-backed docs page


def test_index_missing_is_helpful_404(settings: Settings, db: DB, tmp_path: Path):
    with TestClient(create_app(settings, db, static_dir=tmp_path / "nowhere"), base_url=BASE_URL) as client:
        res = client.get("/")
        assert res.status_code == 404 and "index.html" in res.text and "/api" in res.text


def test_request_guard_blocks_rebinding_and_cross_site_posts(api: TestClient, settings: Settings, db: DB, static_dir: Path, monkeypatch):
    ticks: list = []
    monkeypatch.setattr(S, "tick", lambda *a, **kw: ticks.append(kw) or S.TickResult(dry_run=bool(kw.get("dry_run"))))
    # Host allow-list: a DNS-rebinding page would otherwise be same-origin with the raw clipforge.yaml
    evil = api.get("/api/settings", headers={"Host": "evil.example:8765"})
    assert evil.status_code == 403 and "host" in evil.json()["detail"] and "evil.example" in evil.json()["detail"]
    assert api.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert api.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert api.put("/api/settings", json={"yaml": "clips:\n  count: 9\n"}, headers={"Host": "evil.example:8765"}).status_code == 403
    for ok in ("127.0.0.1:8765", "localhost:8765", "LocalHost", "[::1]:8765"):
        assert api.get("/api/state", headers={"Host": ok}).status_code == 200, ok
    # body-less cross-site POSTs are CORS 'simple' requests: the Origin (or Sec-Fetch-Site) a browser adds must be refused
    cross = api.post("/api/tick", headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert cross.status_code == 403 and "cross-site" in cross.json()["detail"] and ticks == []
    for path in ("/api/run", "/api/videos/vid1/run", "/api/auth/youtube/start"):
        assert api.post(path, headers={"Origin": "https://evil.example"}).status_code == 403, path
    assert api.post("/api/tick", headers={"Origin": "null"}).status_code == 403
    assert api.post("/api/tick", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert api.post("/api/tick", headers={"Origin": "http://127.0.0.1.evil.example"}).status_code == 403
    assert db.list_videos() == [] and ticks == []
    # what the UI's own fetch() sends passes, whichever loopback name the page was opened through
    same = api.post("/api/tick", json={"dry_run": True}, headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"})
    assert same.status_code == 200 and same.json()["dry_run"] is True
    assert api.post("/api/tick", json={"dry_run": True}, headers={"Origin": "http://localhost:8765"}).status_code == 200
    # the custom header is mandatory on non-safe methods (forces a CORS preflight, which the server never approves)
    bare = TestClient(api.app, base_url=BASE_URL)
    assert bare.get("/api/state").status_code == 200 and bare.get("/static/app.js").status_code == 200
    missing = bare.post("/api/tick", json={"dry_run": True})
    assert missing.status_code == 403 and CSRF_HEADER in missing.json()["detail"]
    assert bare.delete("/api/videos/x").status_code == 403 and bare.put("/api/settings", json={"yaml": ""}).status_code == 403
    assert bare.options("/api/tick").status_code == 405  # a preflight is never answered with CORS headers
    # the CLI's --host: the bind name is allowed too; 0.0.0.0 / :: (already warned about) switches the Host check off
    assert allowed_hosts_for("0.0.0.0") == {ANY_HOST} == allowed_hosts_for("::") and allowed_hosts_for("[fe80::1]") == {"fe80::1"}
    lan = TestClient(create_app(settings, db, static_dir=static_dir, allowed_hosts=allowed_hosts_for("mybox.lan")), base_url="http://mybox.lan:8765", headers=UI_HEADERS)
    assert lan.get("/api/state").status_code == 200 and lan.get("/api/state", headers={"Host": "other.lan"}).status_code == 403
    assert lan.post("/api/tick", json={"dry_run": True}, headers={"Origin": "http://mybox.lan:8765"}).status_code == 200
    anyhost = TestClient(create_app(settings, db, static_dir=static_dir, allowed_hosts=allowed_hosts_for("0.0.0.0")), base_url="http://192.168.1.5:8765", headers=UI_HEADERS)
    assert anyhost.get("/api/state").status_code == 200
    assert anyhost.post("/api/tick", json={"dry_run": True}, headers={"Origin": "http://192.168.1.5:8765"}).status_code == 200
    assert anyhost.post("/api/tick", json={"dry_run": True}, headers={"Origin": "http://evil.example"}).status_code == 403


# ---- state -----------------------------------------------------------------------------------------------------------
def test_state_shape(api: TestClient, settings: Settings, db: DB):
    stub_clip(db, settings.workspace_dir, "vid_00")
    state = api.get("/api/state").json()
    assert set(state) == {"videos", "clips", "jobs", "scheduler", "settings", "totals", "truncated"}
    assert state["totals"] == {"videos": 1, "clips": 1} and state["truncated"] == {"videos": False, "clips": False}
    (video,) = state["videos"]
    assert video["id"] == VID and video["status"] == "done" and video["clip_count"] == 1 and video["options"] == {}
    (clip,) = state["clips"]
    assert set(clip) >= {"id", "video_id", "idx", "start", "end", "duration", "score", "hook", "status", "error", "path", "media_url", "meta", "posts"}
    assert clip["media_url"] == f"/media/{VID}/clips/vid_00.mp4" and clip["status"] == "ready"
    assert clip["meta"]["title"] == "Title vid_00" and clip["meta"]["hashtags"]["youtube"] == ["#shorts", "#a", "#b"] and clip["meta"]["topics"] == ["a", "b"]
    assert clip["posts"] == {"youtube": None, "tiktok": None}
    sched = state["scheduler"]
    assert sched["running"] is False and sched["stopping"] is False and sched["tick_s"] == settings.schedule.tick_s and sched["times"] == settings.schedule.times
    assert sched["configured"] == {"youtube": False, "tiktok": False} and set(sched["next_slot"]) == {"youtube", "tiktok"} == set(sched["due"])
    assert sched["limits"]["youtube"]["per_day"] == settings.platforms.youtube.per_day and sched["last_tick"] is None
    cfg = state["settings"]
    assert cfg["style"] == "hormozi" and cfg["clips"] == {"count": 3, "min_s": 6.0, "max_s": 12.0}
    assert {"hormozi", "clean", "minimal"} <= set(cfg["styles"]) and cfg["workspace"] == str(settings.workspace_dir.absolute())
    assert cfg["config_path"].endswith("no-config.yaml")
    assert state["jobs"] == []


def test_state_media_url_none_without_file_and_meta_none_when_broken(api: TestClient, settings: Settings, db: DB):
    clip = stub_clip(db, settings.workspace_dir, "vid_00")
    Path(clip.path).unlink()
    Path(clip.meta_path).write_text("{not json", encoding="utf-8")
    (c,) = api.get("/api/state").json()["clips"]
    assert c["media_url"] is None and c["meta"] is None


# ---- videos ----------------------------------------------------------------------------------------------------------
def test_add_video(api: TestClient, fixture_video: Path, db: DB):
    res = api.post("/api/videos", json={"source": str(fixture_video), "options": {"count": 2, "punch": True, "style": None}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is True and body["status"] == "queued" and body["video_id"].startswith("l")
    video = db.get_video(body["video_id"])
    assert video.kind == "local" and video.options == {"count": 2, "punch": True} and video.source == str(fixture_video.resolve())
    again = api.post("/api/videos", json={"source": str(fixture_video)}).json()
    assert again["created"] is False and again["video_id"] == body["video_id"]
    url = api.post("/api/videos", json={"source": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}).json()
    assert url == {"video_id": "dQw4w9WgXcQ", "created": True, "status": "queued"}
    assert db.get_video("dQw4w9WgXcQ").kind == "youtube"


def test_add_video_rejects_bad_input(api: TestClient, tmp_path: Path, fixture_video: Path):
    missing = api.post("/api/videos", json={"source": str(tmp_path / "nope.mp4")})
    assert missing.status_code == 400 and "file not found" in missing.json()["detail"]
    empty = api.post("/api/videos", json={"source": "   "})
    assert empty.status_code == 400 and "empty" in empty.json()["detail"]
    style = api.post("/api/videos", json={"source": str(fixture_video), "options": {"style": "nope"}})
    assert style.status_code == 400 and "unknown style" in style.json()["detail"]
    minmax = api.post("/api/videos", json={"source": str(fixture_video), "options": {"min_s": 20, "max_s": 10}})
    assert minmax.status_code == 400 and "min_s" in minmax.json()["detail"]
    typed = api.post("/api/videos", json={"source": str(fixture_video), "options": {"count": 0}})
    assert typed.status_code == 400 and isinstance(typed.json()["detail"], str) and "count" in typed.json()["detail"]
    assert api.post("/api/videos", json={"source": 5}).status_code == 400


def test_run_video_job_lifecycle(api: TestClient, settings: Settings, db: DB, monkeypatch):
    calls: list = []
    fake_pipeline(monkeypatch, settings, clips=2, calls=calls)
    db.add_video("v1", "local", "/x/src.mp4")
    assert api.post("/api/videos/nope/run").status_code == 404
    res = api.post("/api/videos/v1/run", json={"force": True})
    assert res.status_code == 200
    job = wait_job(api, res.json()["job_id"])
    assert job["status"] == "done" and job["kind"] == "run" and job["error"] is None
    assert job["result"] == {"video_id": "v1", "status": "done", "error": None, "clips": 2, "rendered": 2}
    assert job["started_at"] and job["finished_at"] and "v1" in job["progress"]
    assert calls == [("v1", True)]
    state = api.get("/api/state").json()
    assert state["videos"][0]["status"] == "done" and state["videos"][0]["clip_count"] == 2
    assert [c["media_url"] for c in state["clips"]] == ["/media/v1/clips/v1_00.mp4", "/media/v1/clips/v1_01.mp4"]
    assert all(c["meta"]["title"] for c in state["clips"])
    assert state["jobs"][0]["id"] == job["id"]


def test_run_queue_job_and_failure(api: TestClient, settings: Settings, db: DB, monkeypatch):
    fake_pipeline(monkeypatch, settings, fail=True)
    db.add_video("v1", "local", "/x/src.mp4")
    job = wait_job(api, api.post("/api/run").json()["job_id"])
    assert job["status"] == "failed" and job["error"] == "v1: boom" and job["result"] == [{"video_id": "v1", "status": "failed", "error": "boom", "clips": 0, "rendered": 0}]
    empty = wait_job(api, api.post("/api/run").json()["job_id"])  # nothing queued any more: done, nothing to do
    assert empty["status"] == "done" and empty["result"] == [] and "nothing to do" in empty["progress"]
    jobs = api.get("/api/state").json()["jobs"]
    assert [j["id"] for j in jobs] == [empty["id"], job["id"]]  # newest first


def test_run_job_exception_does_not_kill_worker(api: TestClient, db: DB, monkeypatch):
    def broken(video_id, settings_, db_, *, force=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pipeline, "process_video", broken)
    db.add_video("v1", "local", "/x/src.mp4")
    job = wait_job(api, api.post("/api/videos/v1/run").json()["job_id"])
    assert job["status"] == "failed" and job["error"] == "RuntimeError: kaboom"
    monkeypatch.setattr(pipeline, "process_video", lambda video_id, s, d, *, force=False: pipeline.ProcessReport(video_id, "done", []))
    assert wait_job(api, api.post("/api/videos/v1/run").json()["job_id"])["status"] == "done"


def test_delete_video(api: TestClient, settings: Settings, db: DB):
    stub_clip(db, settings.workspace_dir, "vid_00")
    stub_clip(db, settings.workspace_dir, "vid_01", idx=1)
    folder = settings.workspace_dir / VID
    assert folder.is_dir()
    assert api.delete("/api/videos/nope").status_code == 404
    res = api.delete(f"/api/videos/{VID}")
    assert res.status_code == 200 and res.json() == {"deleted": True}
    assert db.get_video(VID) is None and db.list_clips(VID) == [] and not folder.exists()
    stub_clip(db, settings.workspace_dir, "vid_00")
    db.mark_posted("vid_00", "youtube", "yt-1")
    res = api.delete(f"/api/videos/{VID}")
    assert res.status_code == 409 and "posted" in res.json()["detail"]
    assert db.get_video(VID) is not None


def test_delete_video_refuses_while_a_pass_runs_or_an_upload_is_claimed(api: TestClient, settings: Settings, db: DB):
    stub_clip(db, settings.workspace_dir, "vid_00")
    api.app.state.work_lock.acquire()  # what a scheduler tick / pipeline pass in progress looks like
    try:
        busy = api.delete(f"/api/videos/{VID}")
        assert busy.status_code == 409 and "busy" in busy.json()["detail"]
        assert db.get_video(VID) is not None and (settings.workspace_dir / VID).is_dir()
    finally:
        api.app.state.work_lock.release()
    now = S._aware(None)
    db.ensure_post("vid_00", "youtube")
    assert db.claim_post("vid_00", "youtube", S._iso_utc(now), S._iso_utc(now - S.CLAIM_STALE))  # another process mid-upload
    live = api.delete(f"/api/videos/{VID}")
    assert live.status_code == 409 and "uploaded" in live.json()["detail"] and db.get_video(VID) is not None
    db.release_claim("vid_00", "youtube")
    assert api.delete(f"/api/videos/{VID}").status_code == 200 and db.get_video(VID) is None


def test_delete_video_refuses_during_a_publish_job(api: TestClient, settings: Settings, db: DB, publishers):
    stub_clip(db, settings.workspace_dir, "vid_00")
    gate, started = threading.Event(), threading.Event()
    real = publishers["youtube"].publish

    def slow_publish(clip, meta):
        started.set()
        assert gate.wait(JOB_WAIT_S)
        return real(clip, meta)

    publishers["youtube"].publish = slow_publish
    job_id = api.post("/api/publish", json={"clip_ids": ["vid_00"], "platforms": ["youtube"]}).json()["job_id"]
    assert started.wait(JOB_WAIT_S) and db.get_post("vid_00", "youtube").status == "uploading"
    res = api.delete(f"/api/videos/{VID}")
    assert res.status_code == 409 and db.get_video(VID) is not None and db.get_clip("vid_00") is not None
    gate.set()
    job = wait_job(api, job_id)
    assert job["status"] == "done" and job["result"][0]["state"] == "posted" and db.get_post("vid_00", "youtube").status == "posted"
    assert api.delete(f"/api/videos/{VID}").status_code == 409  # posted now


# ---- clips -----------------------------------------------------------------------------------------------------------
def test_clip_status(api: TestClient, settings: Settings, db: DB):
    stub_clip(db, settings.workspace_dir, "vid_00", status="rendered")
    res = api.post("/api/clips/vid_00/status", json={"status": "ready"})
    assert res.status_code == 200 and res.json()["status"] == "ready" and res.json()["id"] == "vid_00" and res.json()["media_url"]
    assert api.post("/api/clips/vid_00/status", json={"status": "rejected"}).json()["status"] == "rejected"
    assert db.get_clip("vid_00").status == "rejected"
    assert api.post("/api/clips/vid_00/status", json={"status": "posted"}).status_code == 400
    assert api.post("/api/clips/nope/status", json={"status": "ready"}).status_code == 404
    db.set_clip_status("vid_00", "posted")
    res = api.post("/api/clips/vid_00/status", json={"status": "rejected"})
    assert res.status_code == 409 and "posted" in res.json()["detail"]
    db.upsert_clip("vid_01", VID, 1, 1.0, 5.0, 0.1, "candidate")
    assert api.post("/api/clips/vid_01/status", json={"status": "ready"}).status_code == 409  # never rendered
    assert {r["action"] for r in db.recent_log(10)} >= {"review.ui"}


def test_meta_put_validation_and_rewrite(api: TestClient, settings: Settings, db: DB):
    clip = stub_clip(db, settings.workspace_dir, "vid_00")
    long_title = "x" * 101
    res = api.put("/api/clips/vid_00/meta", json={"title": long_title, "description": "d", "hashtags": TAGS})
    assert res.status_code == 400 and "101" in res.json()["detail"]
    assert api.put("/api/clips/vid_00/meta", json={"title": "  ", "description": "d", "hashtags": TAGS}).status_code == 400
    few = api.put("/api/clips/vid_00/meta", json={"title": "ok", "description": "d", "hashtags": {"youtube": ["#a", "#b"], "tiktok": TAGS["tiktok"]}})
    assert few.status_code == 400 and "youtube" in few.json()["detail"]
    bad = api.put("/api/clips/vid_00/meta", json={"title": "ok", "description": "d", "hashtags": {"youtube": ["shorts", "#a", "#b"], "tiktok": TAGS["tiktok"]}})
    assert bad.status_code == 400 and "shorts" in bad.json()["detail"]
    many = api.put("/api/clips/vid_00/meta", json={"title": "ok", "description": "d", "hashtags": {"youtube": [f"#t{i}" for i in range(7)], "tiktok": TAGS["tiktok"]}})
    assert many.status_code == 400
    assert api.put("/api/clips/nope/meta", json={"title": "ok", "hashtags": TAGS}).status_code == 404
    ok = api.put("/api/clips/vid_00/meta", json={"title": "  New   title ", "description": "New description\n\n#shorts", "hashtags": TAGS})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"title": "New title", "description": "New description\n\n#shorts", "hashtags": TAGS, "topics": ["a", "b"]}
    on_disk = read_meta(clip.meta_path)
    assert on_disk.title == "New title" and on_disk.hashtags == TAGS and on_disk.hook == "hook vid_00" and on_disk.clip_id == "vid_00"
    assert state_clips(api)["vid_00"]["meta"]["title"] == "New title"
    # a clip without metadata gets a fresh file next to its mp4
    db.upsert_clip("vid_01", VID, 1, 1.0, 5.0, 0.1, "the hook")
    created = api.put("/api/clips/vid_01/meta", json={"title": "Fresh", "description": "", "hashtags": TAGS})
    assert created.status_code == 200 and created.json()["topics"] == []
    meta_path = db.get_clip("vid_01").meta_path
    assert meta_path and Path(meta_path).name == "vid_01.json" and read_meta(meta_path).hook == "the hook"


def test_caption_route(api: TestClient, settings: Settings, db: DB):
    clip = stub_clip(db, settings.workspace_dir, "vid_00")
    meta = read_meta(clip.meta_path)
    yt = api.get("/api/clips/vid_00/caption", params={"platform": "youtube"}).json()
    assert yt["caption"] == build_caption("youtube", meta) == "Title vid_00\n\ndesc vid_00\n\n#shorts"
    assert yt["upload_url"] == UPLOAD_PAGES["youtube"][0] and yt["title"] == meta.title and yt["description"] == meta.description
    tt = api.get("/api/clips/vid_00/caption", params={"platform": "tiktok"}).json()
    assert tt["caption"] == meta.caption_for("tiktok") == "Title vid_00\n\n#fyp #a #b" and tt["upload_url"] == UPLOAD_PAGES["tiktok"][0]
    assert api.get("/api/clips/vid_00/caption", params={"platform": "vimeo"}).status_code == 400
    assert api.get("/api/clips/nope/caption").status_code == 404
    db.upsert_clip("vid_01", VID, 1, 1.0, 5.0, 0.1, "hook")
    assert api.get("/api/clips/vid_01/caption").status_code == 404  # no metadata yet


def test_mark_posted(api: TestClient, settings: Settings, db: DB):
    stub_clip(db, settings.workspace_dir, "vid_00")
    res = api.post("/api/clips/vid_00/posted", json={"platform": "youtube", "post_id": "abc123"})
    assert res.status_code == 200, res.text
    post = res.json()
    assert post["status"] == "posted" and post["post_id"] == "abc123" and post["posted_at"] and post["clip_status"] == "posted"
    assert db.is_posted("vid_00", "youtube") and db.get_clip("vid_00").status == "posted"  # no other platform configured
    again = api.post("/api/clips/vid_00/posted", json={"platform": "youtube", "post_id": "abc123"})
    assert again.status_code == 409 and "already posted" in again.json()["detail"]
    other = api.post("/api/clips/vid_00/posted", json={"platform": "tiktok"}).json()  # no id -> generated manual id
    assert other["post_id"].startswith("manual:") and db.is_posted("vid_00", "tiktok")
    assert api.post("/api/clips/vid_00/posted", json={"platform": "vimeo", "post_id": "x"}).status_code == 400
    assert api.post("/api/clips/nope/posted", json={"platform": "youtube", "post_id": "x"}).status_code == 404
    db.upsert_clip("vid_01", VID, 1, 1.0, 5.0, 0.1, "hook")
    assert api.post("/api/clips/vid_01/posted", json={"platform": "youtube", "post_id": "x"}).status_code == 409  # candidate
    actions = [r["action"] for r in db.recent_log(20)]
    assert "publish.manual" in actions and "publish.posted" in actions
    assert state_clips(api)["vid_00"]["posts"]["youtube"]["post_id"] == "abc123"


def test_mark_posted_waits_for_other_configured_platform(api: TestClient, settings: Settings, db: DB, publishers):
    publishers["tiktok"].configured = True  # both configured: posting on one leaves the clip ready
    stub_clip(db, settings.workspace_dir, "vid_00")
    res = api.post("/api/clips/vid_00/posted", json={"platform": "youtube", "post_id": "yt"})
    assert res.json()["clip_status"] == "ready" and db.get_clip("vid_00").status == "ready"
    res = api.post("/api/clips/vid_00/posted", json={"platform": "tiktok", "post_id": "tt"})
    assert res.json()["clip_status"] == "posted"


# ---- publishing -----------------------------------------------------------------------------------------------------
def test_publish_job(api: TestClient, settings: Settings, db: DB, publishers):
    stub_clip(db, settings.workspace_dir, "vid_00")
    stub_clip(db, settings.workspace_dir, "vid_01", idx=1)
    stub_clip(db, settings.workspace_dir, "vid_02", idx=2)
    db.mark_posted("vid_01", "youtube", "yt-old")
    publishers["youtube"].failures = [PublishError("network down")]
    res = api.post("/api/publish", json={"clip_ids": ["vid_00", "vid_01", "vid_02"], "platforms": ["youtube", "tiktok"]})
    assert res.status_code == 200, res.text
    job = wait_job(api, res.json()["job_id"])
    assert job["kind"] == "publish" and job["status"] == "done", job
    by_pair = {(o["clip_id"], o["platform"]): o for o in job["result"]}
    assert by_pair[("vid_00", "youtube")]["state"] == "error" and "network down" in by_pair[("vid_00", "youtube")]["detail"]
    assert by_pair[("vid_01", "youtube")] == {"clip_id": "vid_01", "platform": "youtube", "state": "already posted", "detail": "yt-old", "url": None}
    assert by_pair[("vid_02", "youtube")]["state"] == "posted" and by_pair[("vid_02", "youtube")]["detail"] == "youtube-2"
    assert all(by_pair[(c, "tiktok")]["state"] == "not configured" for c in ("vid_00", "vid_01", "vid_02"))
    assert publishers["youtube"].calls == ["vid_00", "vid_02"] and publishers["tiktok"].calls == []
    assert db.get_clip("vid_02").status == "posted" and db.get_post("vid_00", "youtube").attempts == 1
    assert api.post("/api/publish", json={"clip_ids": [], "platforms": ["youtube"]}).status_code == 400
    assert api.post("/api/publish", json={"clip_ids": ["vid_00"], "platforms": []}).status_code == 400
    assert api.post("/api/publish", json={"clip_ids": ["vid_00"], "platforms": ["vimeo"]}).status_code == 400
    assert api.post("/api/publish", json={"clip_ids": ["nope"], "platforms": ["youtube"]}).status_code == 404


def test_publish_job_fails_when_nothing_posted(api: TestClient, settings: Settings, db: DB, publishers):
    stub_clip(db, settings.workspace_dir, "vid_00")
    publishers["youtube"].failures = [PublishError("down")]
    job = wait_job(api, api.post("/api/publish", json={"clip_ids": ["vid_00"], "platforms": ["youtube"]}).json()["job_id"])
    assert job["status"] == "failed" and "down" in job["error"] and job["result"][0]["state"] == "error"


# ---- auth -----------------------------------------------------------------------------------------------------------
def test_tiktok_auth_start_and_complete(api: TestClient, settings: Settings, db: DB, monkeypatch):
    res = api.post("/api/auth/tiktok/start")
    assert res.status_code == 400 and "TikTok is not configured" in res.json()["detail"]
    settings.platforms.tiktok.client_key = "KEY123"
    settings.platforms.tiktok.client_secret = "SECRET"
    settings.platforms.tiktok.redirect_uri = "https://example.com/cb"
    res = api.post("/api/auth/tiktok/start")
    assert res.status_code == 200, res.text
    start = res.json()
    assert "client_key=KEY123" in start["auth_url"] and f"state={start['state']}" in start["auth_url"] and "code_challenge=" in start["auth_url"]
    exchanged: list[tuple[str, str]] = []

    def fake_exchange(self, code: str, verifier: str) -> dict:
        exchanged.append((code, verifier))
        return {"access_token": "tok", "refresh_token": "ref", "expires_at": 4_000_000_000, "refresh_expires_at": None, "open_id": "o", "scope": "s"}

    monkeypatch.setattr(TikTokPublisher, "_exchange_code", fake_exchange)
    wrong = api.post("/api/auth/tiktok/complete", json={"redirect_url": "https://example.com/cb?code=abc&state=other"})
    assert wrong.status_code == 400 and "state mismatch" in wrong.json()["detail"] and exchanged == []
    denied = api.post("/api/auth/tiktok/complete", json={"redirect_url": f"https://example.com/cb?error=access_denied&state={start['state']}"})
    assert denied.status_code == 400 and "refused" in denied.json()["detail"]
    ok = api.post("/api/auth/tiktok/complete", json={"redirect_url": f"https://example.com/cb?code=the*code&state={start['state']}&scopes=x"})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert len(exchanged) == 1 and exchanged[0][0] == "the*code" and len(exchanged[0][1]) >= 43
    token = json.loads(settings.platform_path(settings.platforms.tiktok.token_file).read_text(encoding="utf-8"))
    assert token["access_token"] == "tok"
    reused = api.post("/api/auth/tiktok/complete", json={"redirect_url": f"https://example.com/cb?code=x&state={start['state']}"})
    assert reused.status_code == 400  # a state is single-use
    assert api.get("/api/state").json()["scheduler"]["configured"]["tiktok"] is True
    assert "auth" in {r["action"] for r in db.recent_log(5)}


def test_tiktok_complete_reports_exchange_failure(api: TestClient, settings: Settings, monkeypatch):
    settings.platforms.tiktok.client_key, settings.platforms.tiktok.client_secret, settings.platforms.tiktok.redirect_uri = "k", "s", "https://x/cb"
    state = api.post("/api/auth/tiktok/start").json()["state"]

    def failing(self, code, verifier):
        raise PublishError("tiktok oauth/token (authorization_code): invalid_grant")

    monkeypatch.setattr(TikTokPublisher, "_exchange_code", failing)
    res = api.post("/api/auth/tiktok/complete", json={"redirect_url": f"https://x/cb?code=c&state={state}"})
    assert res.status_code == 400 and "invalid_grant" in res.json()["detail"]


def test_youtube_auth_start(api: TestClient, settings: Settings, db: DB, monkeypatch):
    res = api.post("/api/auth/youtube/start")
    assert res.status_code == 400 and "client secret" in res.json()["detail"] and "console.cloud.google.com" in res.json()["detail"]
    assert api.post("/api/auth/vimeo/start").status_code == 400
    settings.platform_path(settings.platforms.youtube.client_secret).write_text("{}", encoding="utf-8")
    fake = FakePublisher("youtube", settings, db, configured=False)
    monkeypatch.setattr(P, "get_publisher", lambda name, s, d, **kw: fake)
    job = wait_job(api, api.post("/api/auth/youtube/start").json()["job_id"])
    assert job["kind"] == "auth" and job["status"] == "failed" and "sign-in failed" in job["error"] and job["result"] == {"ok": False}
    fake.configured = True
    job = wait_job(api, api.post("/api/auth/youtube/start").json()["job_id"])
    assert job["status"] == "done" and job["result"] == {"ok": True}


def test_youtube_auth_runs_on_its_own_lane_and_never_stacks(api: TestClient, settings: Settings, db: DB, monkeypatch):
    """A sign-in waiting for the browser must not hold up run jobs, and a second click must not queue a second flow."""
    settings.platform_path(settings.platforms.youtube.client_secret).write_text("{}", encoding="utf-8")
    gate = threading.Event()

    class WaitingPublisher(FakePublisher):
        def auth(self) -> bool:
            assert gate.wait(JOB_WAIT_S)
            return self.configured

    fake = WaitingPublisher("youtube", settings, db, configured=True)
    monkeypatch.setattr(P, "get_publisher", lambda name, s, d, **kw: fake)
    fake_pipeline(monkeypatch, settings, clips=1)
    db.add_video("v1", "local", "/x/src.mp4")
    auth_id = api.post("/api/auth/youtube/start").json()["job_id"]
    assert wait_job(api, auth_id, statuses=("running",))["status"] == "running"
    again = api.post("/api/auth/youtube/start")
    assert again.status_code == 409 and "already in progress" in again.json()["detail"]
    run = wait_job(api, api.post("/api/videos/v1/run").json()["job_id"])  # the work lane is free
    assert run["status"] == "done" and db.get_video("v1").status == "done"
    assert api.app.state.jobs.get(auth_id).lane == AUTH_LANE and api.app.state.jobs.get(run["id"]).lane != AUTH_LANE
    assert {j["id"]: j["status"] for j in api.get("/api/state").json()["jobs"]}[auth_id] == "running"
    gate.set()
    assert wait_job(api, auth_id)["status"] == "done"
    assert api.post("/api/auth/youtube/start").status_code == 200  # a finished sign-in no longer blocks a new one
    gate.set()


def test_youtube_auth_timeout_fails_the_job_and_frees_the_worker(api: TestClient, settings: Settings, db: DB, monkeypatch):
    from google_auth_oauthlib.flow import WSGITimeoutError

    settings.platform_path(settings.platforms.youtube.client_secret).write_text("{}", encoding="utf-8")
    seen: list[str | None] = []

    def timed_out(self):
        seen.append(self.flow_timeout_s)
        raise WSGITimeoutError("Timed out waiting for response from authorization server")

    monkeypatch.setattr(YouTubePublisher, "_load_credentials", lambda self: None)
    monkeypatch.setattr(YouTubePublisher, "_run_flow", timed_out)
    job = wait_job(api, api.post("/api/auth/youtube/start").json()["job_id"])
    assert job["status"] == "failed" and "timed out" in job["error"] and job["result"] == {"ok": False}
    assert seen == [600.0] or seen == [600]  # the UI job keeps the bounded default, never None
    fake_pipeline(monkeypatch, settings, clips=1)
    db.add_video("v1", "local", "/x/src.mp4")
    assert wait_job(api, api.post("/api/videos/v1/run").json()["job_id"])["status"] == "done"


# ---- scheduler -------------------------------------------------------------------------------------------------------
def test_scheduler_start_stop(api: TestClient, settings: Settings, db: DB, monkeypatch):
    calls: list[bool] = []

    def fake_tick(settings_, db_, *, dry_run=False, **kw):
        calls.append(dry_run)
        return S.TickResult(processed=["v1"], dry_run=dry_run)

    monkeypatch.setattr(S, "tick", fake_tick)
    settings.schedule.tick_s = 3600
    res = api.post("/api/scheduler", json={"running": True})
    assert res.status_code == 200 and res.json()["running"] is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not calls:
        time.sleep(POLL_S)
    assert calls == [False]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and api.get("/api/state").json()["scheduler"]["last_tick"] is None:
        time.sleep(POLL_S)
    sched = api.get("/api/state").json()["scheduler"]
    assert sched["running"] is True and sched["last_tick"] and sched["last_summary"].startswith("tick: processed 1 video(s)")
    assert api.post("/api/scheduler", json={"running": True}).json()["running"] is True  # idempotent
    t0 = time.monotonic()
    res = api.post("/api/scheduler", json={"running": False})
    assert res.json()["running"] is False and time.monotonic() - t0 < 2.0  # stop does not wait for tick_s
    assert api.post("/api/scheduler", json={"running": False}).json()["running"] is False
    assert calls == [False]
    assert "schedule.ui" in {r["action"] for r in db.recent_log(10)}


def test_scheduler_loop_survives_crashing_tick(settings: Settings, db: DB, monkeypatch):
    def crash(settings_, db_, **kw):
        raise RuntimeError("no tz")

    monkeypatch.setattr(S, "tick", crash)
    settings.schedule.tick_s = 0  # -> MIN_WAIT_S between passes
    loop = background.SchedulerLoop(lambda: (settings, db))
    assert loop.start() and not loop.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and loop.ticks < 1:
        time.sleep(POLL_S)
    assert loop.running and loop.last_summary == "tick crashed: RuntimeError: no tz"
    assert loop.stop() and not loop.running and not loop.stop()
    assert any(r["action"] == "schedule.tick" and "crashed" in r["detail"] for r in db.recent_log(5))


def test_tick_dry_run(api: TestClient, settings: Settings, db: DB):
    db.add_video("v1", "local", "/x/src.mp4")
    res = api.post("/api/tick", json={"dry_run": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True and body["posted"] == [] and body["processed"] == []
    assert body["summary"].startswith("tick: processed 0 video(s), would post 0")
    assert any("would process 1 video(s)" in s for s in body["skipped"]) and any("not configured" in s for s in body["skipped"])
    assert body["errors"] == [] and db.get_video("v1").status == "queued"  # a dry run never runs the pipeline


def test_tick_real_pass_is_a_job_that_uses_pipeline_and_lock(api: TestClient, settings: Settings, db: DB, monkeypatch):
    fake_pipeline(monkeypatch, settings, clips=1)
    db.add_video("v1", "local", "/x/src.mp4")
    res = api.post("/api/tick")
    assert res.status_code == 200 and set(res.json()) == {"job_id"}  # a real tick may render for minutes: never inline
    job = wait_job(api, res.json()["job_id"])
    assert job["kind"] == "tick" and job["status"] == "done", job
    assert job["result"]["dry_run"] is False and job["result"]["processed"] == ["v1"] and job["result"]["posted"] == []
    assert job["result"]["summary"].startswith("tick: processed 1 video(s)") and job["progress"] == job["result"]["summary"]
    assert db.get_video("v1").status == "done"
    assert any(j["id"] == job["id"] for j in api.get("/api/state").json()["jobs"])
    api.app.state.work_lock.acquire()  # the scheduler thread is ticking: the job waits for the lock, nothing stacks behind it
    try:
        queued = api.post("/api/tick", json={"dry_run": False})
        assert queued.status_code == 200
        assert wait_job(api, queued.json()["job_id"], statuses=("queued", "running"))["status"] in ("queued", "running")
        second = api.post("/api/tick")
        assert second.status_code == 409 and "already" in second.json()["detail"]
        blocked = api.delete("/api/videos/v1")
        assert blocked.status_code == 409 and db.get_video("v1") is not None
    finally:
        api.app.state.work_lock.release()
    assert wait_job(api, queued.json()["job_id"])["status"] == "done"


def test_scheduler_block_flags_due_slots(api: TestClient, settings: Settings, db: DB):
    settings.schedule.times = ["00:00"]  # always opened today
    stub_clip(db, settings.workspace_dir, "vid_00")
    sched = api.get("/api/state").json()["scheduler"]
    assert sched["next_slot"] == {"youtube": "00:00", "tiktok": "00:00"} and sched["due"] == {"youtube": True, "tiktok": True}
    db.mark_posted("vid_00", "youtube", "yt-1")  # posted after the slot opened: youtube waits for tomorrow's
    sched = api.get("/api/state").json()["scheduler"]
    assert sched["due"] == {"youtube": False, "tiktok": True} and sched["next_slot"]["youtube"] == "00:00"


def test_scheduler_reports_stopping_while_its_last_tick_runs(settings: Settings, db: DB, monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    def slow_tick(settings_, db_, **kw):
        entered.set()
        assert gate.wait(JOB_WAIT_S)
        return S.TickResult()

    monkeypatch.setattr(S, "tick", slow_tick)
    settings.schedule.tick_s = 3600
    loop = background.SchedulerLoop(lambda: (settings, db))
    assert loop.start() and entered.wait(5)
    threads_before = {t.name for t in threading.enumerate() if t.name == "clipforge-scheduler"}
    assert loop.stop(timeout=0.1) is True and not loop.running and loop.stopping
    assert loop.start() is False and loop.stop(timeout=0.1) is False  # no second thread, no second 'stopped' log
    assert loop.stopping and [t.name for t in threading.enumerate() if t.name == "clipforge-scheduler"] == list(threads_before)
    gate.set()
    wait_for(lambda: not loop.stopping)
    assert not loop.running and not loop.stopping
    assert loop.start() and loop.running  # a fresh thread once the old one has exited
    assert loop.stop() and not loop.running and not loop.stopping


def test_next_slot_hm():
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=2))
    times = S.parse_times(["09:00", "13:00", "18:00"])
    at = lambda h, m: datetime(2026, 9, 15, h, m, tzinfo=tz)  # noqa: E731
    assert background.next_slot_hm(times, at(8, 0), None) == "09:00"
    assert background.next_slot_hm(times, at(9, 30), None) == "09:00"  # due: opened, nothing posted since
    assert background.next_slot_hm(times, at(9, 30), at(9, 5)) == "13:00"
    assert background.next_slot_hm(times, at(19, 0), at(18, 5)) == "09:00"  # tomorrow's first
    assert background.next_slot_hm([], at(8, 0), None) is None


# ---- diagnostics -------------------------------------------------------------------------------------------------------
def test_doctor(api: TestClient):
    res = api.get("/api/doctor")
    assert res.status_code == 200
    checks = res.json()
    names = {c["name"] for c in checks}
    assert {"python", "ffmpeg", "whisper", "yt-dlp", "workspace", "fonts"} <= names
    assert all(set(c) == {"name", "status", "detail"} and c["status"] in ("OK", "WARN", "FAIL", "INFO") for c in checks)
    assert next(c for c in checks if c["name"] == "python")["status"] == "OK"


def test_logs(api: TestClient, settings: Settings, db: DB):
    db.log("ui.test", "hello", level="info")
    logs_dir = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "clipforge.log").write_text("".join(f"line {i}\n" for i in range(10)), encoding="utf-8")
    body = api.get("/api/logs", params={"limit": 3}).json()
    assert body["db"][0] == {"ts": body["db"][0]["ts"], "level": "info", "action": "ui.test", "detail": "hello"} and len(body["db"]) <= 3
    assert body["file"] == ["line 7", "line 8", "line 9"]
    (logs_dir / "clipforge.log").unlink()
    assert api.get("/api/logs").json()["file"] == []


def test_settings_get_and_put(api: TestClient, settings: Settings, db: DB, tmp_path: Path):
    got = api.get("/api/settings").json()
    assert got["exists"] is False and got["path"].endswith("no-config.yaml")
    assert yaml.safe_load(got["yaml"])["clips"]["count"] == 3  # the live settings, ready to edit
    bad = api.put("/api/settings", json={"yaml": "clips: [unclosed"})
    assert bad.status_code == 400 and "YAML" in bad.json()["detail"]
    invalid = api.put("/api/settings", json={"yaml": "clips:\n  count: many\n"})
    assert invalid.status_code == 400 and "count" in invalid.json()["detail"]
    assert api.put("/api/settings", json={"yaml": "- a list\n"}).status_code == 400
    # Settings ignores unknown keys (env/CLI leniency); the editor must reject them or a typo saves and changes nothing
    typo = api.put("/api/settings", json={"yaml": "clip:\n  count: 9\n"})
    assert typo.status_code == 400 and "unknown key(s): clip" in typo.json()["detail"] and "clips" in typo.json()["detail"]
    nested = api.put("/api/settings", json={"yaml": "clips:\n  cont: 9\n"})
    assert nested.status_code == 400 and "clips.cont" in nested.json()["detail"]
    style = api.put("/api/settings", json={"yaml": "styles:\n  hormozi:\n    fnt: Arial\n"})
    assert style.status_code == 400 and "styles.hormozi.fnt" in style.json()["detail"]
    assert api.app.state.settings.clips.count == 3 and not Path(got["path"]).exists()
    api.app.state.work_lock.acquire()  # a pass in flight keeps the old settings/DB: saving now would show another world
    try:
        busy = api.put("/api/settings", json={"yaml": "clips:\n  count: 9\n"})
        assert busy.status_code == 409 and "busy" in busy.json()["detail"] and not Path(got["path"]).exists()
    finally:
        api.app.state.work_lock.release()
    new_workspace = tmp_path / "ws2"
    text = yaml.safe_dump({"paths": {"workspace": str(new_workspace), "logs": str(tmp_path / "logs2")}, "clips": {"count": 7, "min_s": 5, "max_s": 15}, "style": "clean"})
    ok = api.put("/api/settings", json={"yaml": text})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "path": got["path"]} and Path(got["path"]).read_text(encoding="utf-8") == text
    state = api.get("/api/state").json()
    assert state["settings"]["clips"]["count"] == 7 and state["settings"]["style"] == "clean" and state["settings"]["workspace"] == str(new_workspace)
    assert api.app.state.settings.clips.count == 7 and api.app.state.db.path == new_workspace / "clipforge.db"  # db reopened
    assert api.get("/api/state").json()["videos"] == []
    again = api.get("/api/settings").json()
    assert again["exists"] is True and again["yaml"] == text
    assert any(r["action"] == "settings.saved" for r in api.app.state.db.recent_log(5))


def test_setup_logging_replaces_the_file_handler(tmp_path: Path):
    from clipforge.log import setup_logging

    root = logging.getLogger("clipforge")
    file_handlers = lambda: [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]  # noqa: E731
    setup_logging(tmp_path / "logs1")
    setup_logging(tmp_path / "logs1")
    assert len(file_handlers()) == 1 and Path(file_handlers()[0].baseFilename) == (tmp_path / "logs1" / "clipforge.log").resolve()
    setup_logging(tmp_path / "logs2")  # a new logs dir (the UI reloaded a saved config): the old file is closed, not doubled
    assert len(file_handlers()) == 1 and Path(file_handlers()[0].baseFilename) == (tmp_path / "logs2" / "clipforge.log").resolve()
    logging.getLogger("clipforge.test").info("only in logs2")
    for h in file_handlers():
        h.flush()
    assert "only in logs2" in (tmp_path / "logs2" / "clipforge.log").read_text(encoding="utf-8")
    assert "only in logs2" not in (tmp_path / "logs1" / "clipforge.log").read_text(encoding="utf-8")


# ---- media -----------------------------------------------------------------------------------------------------------
def test_media_route_full_range_and_traversal(api: TestClient, settings: Settings, db: DB):
    clip = stub_clip(db, settings.workspace_dir, "vid_00", size=1000)
    url = state_clips(api)["vid_00"]["media_url"]
    full = api.get(url)
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4" and len(full.content) == 1000
    assert full.headers.get("accept-ranges") == "bytes"
    part = api.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.headers["content-range"] == "bytes 0-99/1000" and len(part.content) == 100
    assert part.content == Path(clip.path).read_bytes()[:100]
    tail = api.get(url, headers={"Range": "bytes=900-"})
    assert tail.status_code == 206 and len(tail.content) == 100
    assert api.get(f"/media/{VID}/clips/nope.mp4").status_code == 404
    assert api.get("/media/other/clips/vid_00.mp4").status_code == 404
    (settings.workspace_dir / "secret.txt").write_text("x", encoding="utf-8")
    for bad in (f"/media/{VID}/clips/..%2Fsecret.txt", "/media/..%2F..%2Fclips/x.mp4", f"/media/{VID}/clips/%2E%2E%2F%2E%2E%2Fsecret.txt", "/media/./clips/vid_00.mp4"):
        assert api.get(bad).status_code == 404, bad  # the router already turns these away
    sidecar = api.get(f"/media/{VID}/clips/vid_00.json")
    assert sidecar.status_code == 200 and sidecar.headers["content-type"].startswith("application/json")
    # symlinks are the cases that reach the handler's resolve() + containment checks
    secret = settings.workspace_dir.parent / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    outside = settings.workspace_dir.parent / "outside" / "clips"
    outside.mkdir(parents=True)
    (outside / "x.mp4").write_bytes(b"OUTSIDE")
    try:
        (settings.workspace_dir / VID / "clips" / "evil.mp4").symlink_to(secret)
        (settings.workspace_dir / "linkvid").symlink_to(outside.parent)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert api.get(f"/media/{VID}/clips/evil.mp4").status_code == 404
    assert api.get("/media/linkvid/clips/x.mp4").status_code == 404
    # suffix / unsatisfiable ranges, and HEAD (download managers and players probe with it before a ranged GET)
    suffix = api.get(url, headers={"Range": "bytes=-100"})
    assert suffix.status_code == 206 and suffix.headers["content-range"] == "bytes 900-999/1000" and len(suffix.content) == 100
    bad_range = api.get(url, headers={"Range": "bytes=5000-"})
    assert bad_range.status_code == 416 and bad_range.headers["content-range"] == "bytes */1000"
    head = api.head(url)
    assert head.status_code == 200 and head.headers["content-length"] == "1000" and head.headers.get("accept-ranges") == "bytes" and head.content == b""
    head_part = api.head(url, headers={"Range": "bytes=-100"})
    assert head_part.status_code == 206 and head_part.headers["content-range"] == "bytes 900-999/1000" and head_part.content == b""
    assert api.head(f"/media/{VID}/clips/nope.mp4").status_code == 404


# ---- job runner ---------------------------------------------------------------------------------------------------------
def test_job_runner_sequential_failures_and_history():
    runner = JobRunner(history=3)
    order: list[str] = []
    import threading

    gate = threading.Event()

    def slow(job):
        gate.wait(5)
        order.append(job.id)
        job.result = {"n": 1}

    def boom(job):
        job.progress = "half way"
        raise ValueError("bad")

    def soft(job):
        raise JobFailed("plain message")

    a = runner.submit("run", slow, "a", target="v1")
    b = runner.submit("run", boom, "b")
    c = runner.submit("publish", soft, "c", target={"v2", "v3"})
    assert runner.is_busy() and runner.active_targets() == {"v1", "v2", "v3"}
    assert [j.id for j in runner.list()] == [c.id, b.id, a.id]
    other = runner.submit("auth", lambda job: order.append("auth"), "sign-in", lane="auth")  # its own lane: not behind `slow`
    assert runner.wait_idle(0.2) is False and other.status == "done" and order == ["auth"] and a.status == "running"
    assert runner.current(lane="work") is a and runner.current(lane="auth") is None
    gate.set()
    assert runner.wait_idle(5)
    assert order == ["auth", a.id] and a.status == "done" and a.result == {"n": 1} and a.started_at and a.finished_at
    assert b.status == "failed" and b.error == "ValueError: bad" and b.progress == "half way"
    assert c.status == "failed" and c.error == "plain message"
    d = runner.submit("run", lambda job: None, "d")
    assert runner.wait_idle(5) and d.status == "done"
    assert [j.id for j in runner.list()] == [d.id, other.id, c.id]  # oldest finished jobs pruned
    assert set(a.to_dict()) == {"id", "kind", "status", "detail", "progress", "started_at", "finished_at", "error", "result"}
    runner.stop()


# ---- the real thing -------------------------------------------------------------------------------------------------------
def test_real_pipeline_end_to_end(api: TestClient, settings: Settings, fixture_video: Path):
    res = api.post("/api/videos", json={"source": str(fixture_video), "options": {"count": 2, "min_s": 6, "max_s": 12}})
    assert res.status_code == 200, res.text
    video_id = res.json()["video_id"]
    job_id = api.post("/api/run").json()["job_id"]
    job = wait_job(api, job_id, timeout=E2E_WAIT_S)
    assert job["status"] == "done", job
    assert job["result"][0]["video_id"] == video_id and job["result"][0]["status"] == "done" and job["result"][0]["rendered"] >= 2
    state = api.get("/api/state").json()
    (video,) = state["videos"]
    assert video["status"] == "done" and video["clip_count"] >= 2 and video["duration"] == pytest.approx(40.0, abs=1.0)
    clips = [c for c in state["clips"] if c["video_id"] == video_id]
    assert len(clips) >= 2
    for clip in clips:
        assert clip["status"] == "rendered" and clip["media_url"] == f"/media/{video_id}/clips/{clip['id']}.mp4"
        assert clip["meta"] and 0 < len(clip["meta"]["title"]) <= 100 and "#shorts" in clip["meta"]["hashtags"]["youtube"]
        assert 5.5 <= clip["duration"] <= 13.0
    head = api.get(clips[0]["media_url"], headers={"Range": "bytes=0-15"})
    assert head.status_code == 206 and len(head.content) == 16 and b"ftyp" in head.content
    caption = api.get(f"/api/clips/{clips[0]['id']}/caption", params={"platform": "tiktok"}).json()
    assert caption["caption"].startswith(clips[0]["meta"]["title"])


# ---- review fixes ----------------------------------------------------------------------------------------------------
def test_publish_job_rechecks_approval_when_it_runs(api: TestClient, settings: Settings, db: DB, publishers):
    """Rejected after the job was queued: the worker must not upload it."""
    from clipforge.ui.jobs import Job, JobFailed
    from clipforge.ui.server import _publish_fn

    stub_clip(db, settings.workspace_dir, "vid_00")
    fn = _publish_fn(api.app, ["vid_00"], ["youtube"])
    db.set_clip_status("vid_00", "rejected")
    job = Job(id="j1", kind="publish", detail="publish vid_00")
    with pytest.raises(JobFailed, match="rejected"):
        fn(job)
    assert publishers["youtube"].calls == [] and job.result[0]["state"] == "error" and "rejected" in job.result[0]["detail"]
    assert db.get_post("vid_00", "youtube") is None


def test_tiktok_posting_choices_are_saved_to_the_config(api: TestClient, settings: Settings, db: DB):
    import os

    import yaml

    from clipforge.config import CONFIG_ENV

    res = api.put("/api/platforms/tiktok", json={"privacy": "SELF_ONLY", "music_usage_confirmed": True, "allow_comments": True})
    assert res.status_code == 200, res.text
    data = yaml.safe_load(Path(os.environ[CONFIG_ENV]).read_text(encoding="utf-8"))
    assert data["platforms"]["tiktok"]["privacy"] == "SELF_ONLY" and data["platforms"]["tiktok"]["music_usage_confirmed"] is True
    assert data["paths"]["workspace"] == settings.paths.workspace  # the rest of the live settings survived
    tt = api.get("/api/state").json()["settings"]["tiktok"]
    assert tt["privacy"] == "SELF_ONLY" and tt["allow_comments"] is True and tt["allow_duet"] is False
    assert api.put("/api/platforms/tiktok", json={"bogus": 1}).status_code == 400
    assert api.put("/api/platforms/tiktok", json={"privacy": ""}).status_code == 200  # clearing the choice again
    assert api.get("/api/state").json()["settings"]["tiktok"]["privacy"] is None


# ---- hardening batch 2: health ---------------------------------------------------------------------------------------
def test_health_liveness_and_readiness(api: TestClient, monkeypatch):
    live = api.get("/api/health")
    assert live.status_code == 200 and live.json()["status"] == "ok" and set(live.json()) == {"status", "version"}
    ready = api.get("/api/health", params={"ready": "1"})
    assert ready.status_code == 200 and ready.json()["checks"] == {"db": True, "ffmpeg": True, "workspace": True}
    from clipforge.ui import server as srv

    monkeypatch.setattr(srv.F, "ffmpeg_exe", lambda: "/nonexistent/ffmpeg")
    degraded = api.get("/api/health", params={"ready": "1"})
    assert degraded.status_code == 503 and degraded.json()["status"] == "degraded" and degraded.json()["checks"]["ffmpeg"] is False
    assert "/" not in json.dumps(degraded.json())  # no paths leak


# ---- hardening batch 3: bounded state, metadata cache -------------------------------------------------------------
def test_state_is_bounded_and_video_filter_returns_everything_for_that_video(api: TestClient, settings: Settings, db: DB):
    for v in ("va", "vb", "vc"):
        for i in range(3):
            stub_clip(db, settings.workspace_dir, f"{v}_{i:02d}", video_id=v, idx=i)
    full = api.get("/api/state").json()
    assert full["totals"]["clips"] == 9 and len(full["clips"]) == 9 and full["truncated"]["clips"] is False
    bounded = api.get("/api/state", params={"clips_limit": 4}).json()
    assert len(bounded["clips"]) == 4 and bounded["truncated"]["clips"] is True and bounded["totals"]["clips"] == 9
    assert [c["id"] for c in bounded["clips"]] == sorted(c["id"] for c in bounded["clips"])  # deterministic (video, idx) order
    assert all(v["clip_count"] == 3 for v in bounded["videos"])  # counts stay global
    one = api.get("/api/state", params={"video": "vb", "clips_limit": 1}).json()
    assert [c["id"] for c in one["clips"]] == ["vb_00", "vb_01", "vb_02"] and one["truncated"]["clips"] is False
    videos = api.get("/api/state", params={"videos_limit": 2}).json()
    assert len(videos["videos"]) == 2 and videos["truncated"]["videos"] is True and videos["totals"]["videos"] == 3
    assert api.get("/api/state", params={"clips_limit": -1}).status_code == 400


def test_meta_cache_serves_fresh_data_after_edits(api: TestClient, settings: Settings, db: DB):
    from clipforge.ui import serialize

    clip = stub_clip(db, settings.workspace_dir, "vid_00")
    assert api.get("/api/state").json()["clips"][0]["meta"]["title"] == "Title vid_00"
    assert str(clip.meta_path) in serialize._meta_cache
    api.put("/api/clips/vid_00/meta", json={"title": "Edited", "description": "d", "hashtags": {"youtube": ["#shorts", "#a", "#b"], "tiktok": ["#fyp", "#a", "#b"]}})
    assert api.get("/api/state").json()["clips"][0]["meta"]["title"] == "Edited"
    Path(clip.meta_path).write_text("{not json", encoding="utf-8")  # external corruption: size changed -> re-read -> None
    assert api.get("/api/state").json()["clips"][0]["meta"] is None
    Path(clip.meta_path).unlink()
    assert api.get("/api/state").json()["clips"][0]["meta"] is None and str(clip.meta_path) not in serialize._meta_cache
