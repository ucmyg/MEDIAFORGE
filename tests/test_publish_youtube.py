"""YouTube publisher, fully offline: fake insert requests drive upload_video, fake credentials drive auth.

Google libraries are never imported at module level here; the loader/flow/service hooks on YouTubePublisher are
monkeypatched. Day keys and DB timestamps are frozen so posted_today()/budget arithmetic is deterministic.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import clipforge.db as dbmod
from clipforge.db import DB, Clip
from clipforge.metadata import ClipMeta
from clipforge.publish import youtube as yt
from clipforge.publish.base import PublishError, PublishFatal
from clipforge.publish.youtube import QuotaExhausted, YouTubePublisher

FROZEN_DAY = "2026-09-15"
FROZEN_TS = f"{FROZEN_DAY}T12:00:00+00:00"


# ---- fakes -----------------------------------------------------------------------------------------------------------
class FakeHttpError(Exception):
    """Shape of googleapiclient.errors.HttpError as seen by youtube._http_status / _error_detail."""

    def __init__(self, status: int, content: bytes | str = b""):
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)
        self.content = content


def api_error(status: int, reason: str, message: str = "boom") -> FakeHttpError:
    return FakeHttpError(status, json.dumps({"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}).encode())


class FakeRequest:
    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeService:
    """service.videos().insert(...) -> FakeRequest; remembers the insert kwargs."""

    def __init__(self, outcomes: list):
        self.request = FakeRequest(outcomes)
        self.insert_kwargs: dict | None = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return self.request


class FakeCreds:
    def __init__(self, valid: bool, expired: bool = False, refresh_token: str | None = None):
        self.valid, self.expired, self.refresh_token = valid, expired, refresh_token
        self.refreshed_with = None

    def refresh(self, request):
        self.refreshed_with = request
        self.valid, self.expired = True, False

    def to_json(self) -> str:
        return json.dumps({"token": "t", "refresh_token": self.refresh_token, "scopes": yt.SCOPES})


DONE = [(None, None), (None, {"id": "abc123"})]


# ---- fixtures --------------------------------------------------------------------------------------------------------
@pytest.fixture()
def publisher(settings, db: DB, monkeypatch) -> YouTubePublisher:
    monkeypatch.setattr(dbmod, "utcnow", lambda: FROZEN_TS)
    monkeypatch.setattr(YouTubePublisher, "today_key", lambda self: FROZEN_DAY)
    pub = YouTubePublisher(settings, db, sleep=lambda s: pub.sleeps.append(s))
    pub.sleeps = []
    return pub


@pytest.fixture()
def use_service(monkeypatch):
    def _install(outcomes: list) -> FakeService:
        svc = FakeService(outcomes)
        monkeypatch.setattr(YouTubePublisher, "_service", lambda self: svc)
        return svc

    return _install


@pytest.fixture()
def clip(settings, db: DB) -> Clip:
    return make_clip(settings, db, "vid_00")


@pytest.fixture()
def yt_log(caplog):
    """caplog attached straight to the module logger (the clipforge root stops propagating once the CLI configured it)."""
    yt.log.addHandler(caplog.handler)
    yield caplog
    yt.log.removeHandler(caplog.handler)


def make_clip(settings, db: DB, clip_id: str, duration: float = 10.0) -> Clip:
    db.add_video("vid", "local", "source.mp4")
    db.upsert_clip(clip_id, "vid", int(clip_id[-2:]), 0.0, duration, 1.0, "hook")
    path = settings.workspace_dir / f"{clip_id}.mp4"
    path.write_bytes(b"\0" * 4096)
    db.update_clip(clip_id, path=str(path), status="ready", duration=duration)
    return db.get_clip(clip_id)


def meta(**overrides) -> ClipMeta:
    base = dict(title="Hello world", description="A summary.\n\n#shorts #python", hashtags={"youtube": ["#shorts", "#python"]}, topics=["python", "testing"])
    base.update(overrides)
    return ClipMeta(**base)


# ---- limits ----------------------------------------------------------------------------------------------------------
def test_limits_arithmetic(publisher: YouTubePublisher, settings, db: DB):
    lim = publisher.limits()
    assert (lim.per_day, lim.posted_today, lim.remaining, lim.quota_used, lim.quota_total) == (3, 0, 3, 0, 10000)
    assert lim.note == "quota 0/10000 units" and not lim.exhausted

    for cid in ("vid_01", "vid_02"):
        make_clip(settings, db, cid)
        db.mark_posted(cid, "youtube", f"id-{cid}")
    db.budget_add("youtube", FROZEN_DAY, 3200)
    lim = publisher.limits()
    assert (lim.posted_today, lim.remaining, lim.quota_used) == (2, 1, 3200) and lim.note == "quota 3200/10000 units"

    db.budget_add("youtube", FROZEN_DAY, 5800)  # 9000 used -> 1000 left < one upload
    lim = publisher.limits()
    assert lim.remaining == 0 and lim.exhausted
    with pytest.raises(PublishFatal, match="allowance exhausted"):
        publisher.ensure_allowance()


def test_limits_quota_bound_beats_per_day(publisher: YouTubePublisher):
    publisher.cfg.per_day = 10
    assert publisher.limits().remaining == 6  # 10000 // 1600


# ---- publish ---------------------------------------------------------------------------------------------------------
def test_publish_happy_path(publisher: YouTubePublisher, db: DB, clip: Clip, use_service, capsys):
    svc = use_service(DONE)
    assert publisher.publish(clip, meta()) == "abc123"
    assert svc.request.calls == 2 and publisher.sleeps == []
    kwargs = svc.insert_kwargs
    assert kwargs["part"] == "snippet,status"
    assert kwargs["body"]["snippet"]["title"] == "Hello world" and kwargs["body"]["snippet"]["tags"] == ["python", "testing"]
    assert kwargs["body"]["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    media = kwargs["media_body"]
    assert media.resumable() and media.chunksize() == yt.CHUNK_SIZE and media.mimetype() == "video/mp4"

    assert db.is_posted(clip.id, "youtube")
    post = db.get_post(clip.id, "youtube")
    assert post.status == "posted" and post.post_id == "abc123" and post.posted_at == FROZEN_TS
    assert db.budget_used("youtube", FROZEN_DAY) == 1600
    assert publisher.limits().posted_today == 1
    assert any(r["action"] == "publish.youtube" and "abc123" in r["detail"] for r in db.recent_log())
    out = capsys.readouterr().out
    assert "https://youtube.com/shorts/abc123" in out and "private" in out
    assert "read this once" in out  # audit warning shown the first time

    with pytest.raises(PublishFatal, match="already posted"):
        publisher.publish(clip, meta())
    assert db.budget_used("youtube", FROZEN_DAY) == 1600 and len(db.list_posts("youtube")) == 1
    assert "read this once" not in capsys.readouterr().out


def test_transient_errors_retry_with_backoff(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    svc = use_service([FakeHttpError(503), ConnectionError("reset"), *DONE])
    assert publisher.publish(clip, meta()) == "abc123"
    assert svc.request.calls == 4 and publisher.sleeps == [2, 4]
    assert db.is_posted(clip.id, "youtube")


def test_retries_exhausted_is_retryable_error(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    svc = use_service([FakeHttpError(502)] * 6 + DONE)
    with pytest.raises(PublishError, match="after 5 retries") as info:
        publisher.publish(clip, meta())
    assert not isinstance(info.value, PublishFatal)
    assert svc.request.calls == 6 and publisher.sleeps == list(yt.BACKOFF_S)
    post = db.get_post(clip.id, "youtube")
    assert post.status == "pending" and post.attempts == 1 and "after 5 retries" in post.error
    assert db.budget_used("youtube", FROZEN_DAY) == 0 and not db.is_posted(clip.id, "youtube")


def test_quota_exceeded_marks_budget_exhausted(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    db.budget_add("youtube", FROZEN_DAY, 1600)
    svc = use_service([api_error(403, "quotaExceeded", "The request cannot be completed because you have exceeded your quota.")])
    with pytest.raises(QuotaExhausted, match="quotaExceeded"):
        publisher.publish(clip, meta())
    assert svc.request.calls == 1 and publisher.sleeps == []
    assert db.budget_used("youtube", FROZEN_DAY) == 10000 and publisher.limits().exhausted
    post = db.get_post(clip.id, "youtube")
    assert post.status == "pending" and post.attempts == 1 and "quotaExceeded" in post.error
    assert not db.is_posted(clip.id, "youtube")
    with pytest.raises(PublishFatal, match="allowance exhausted"):
        publisher.publish(clip, meta())
    assert any(r["level"] == "error" and r["action"] == "publish.youtube" for r in db.recent_log())


def test_upload_limit_exceeded_is_quota(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    use_service([api_error(403, "uploadLimitExceeded")])
    with pytest.raises(QuotaExhausted):
        publisher.publish(clip, meta())
    assert db.budget_used("youtube", FROZEN_DAY) == 10000


@pytest.mark.parametrize(
    "error, match",
    [
        (FakeHttpError(401, b"Invalid Credentials"), "clipforge auth youtube"),
        (api_error(403, "forbidden", "The caller does not have permission"), "clipforge auth youtube"),
        (api_error(400, "invalidTitle", "The request metadata specifies an invalid video title."), "invalidTitle: The request metadata"),
    ],
)
def test_fatal_http_errors(publisher: YouTubePublisher, db: DB, clip: Clip, use_service, error, match):
    use_service([error])
    with pytest.raises(PublishFatal, match=match) as info:
        publisher.publish(clip, meta())
    assert not isinstance(info.value, QuotaExhausted)
    post = db.get_post(clip.id, "youtube")
    assert post.status == "failed" and post.attempts == 1
    assert db.budget_used("youtube", FROZEN_DAY) == 0


def test_other_http_error_is_retryable(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    use_service([FakeHttpError(429, b"rate limited")])
    with pytest.raises(PublishError) as info:
        publisher.publish(clip, meta())
    assert not isinstance(info.value, PublishFatal) and db.get_post(clip.id, "youtube").status == "pending"


def test_publish_without_credentials_is_fatal(publisher: YouTubePublisher, db: DB, clip: Clip, monkeypatch):
    monkeypatch.setattr(YouTubePublisher, "_load_credentials", lambda self: None)
    with pytest.raises(PublishFatal, match="clipforge auth youtube"):
        publisher.publish(clip, meta())
    assert db.get_post(clip.id, "youtube").status == "failed"


def test_publish_missing_file_is_fatal(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    Path(clip.path).unlink()
    use_service(DONE)
    with pytest.raises(PublishFatal, match="no rendered file"):
        publisher.publish(clip, meta())
    assert db.get_post(clip.id, "youtube").status == "failed"


def test_response_without_id_is_fatal(publisher: YouTubePublisher, db: DB, clip: Clip, use_service):
    use_service([(None, {"kind": "youtube#video"})])
    with pytest.raises(PublishFatal, match="without a video id"):
        publisher.publish(clip, meta())
    assert db.budget_used("youtube", FROZEN_DAY) == 0


# ---- body construction --------------------------------------------------------------------------------------------
def test_build_body_clamps_and_flags(publisher: YouTubePublisher):
    publisher.cfg.made_for_kids = True
    publisher.cfg.category = "28"
    body = publisher.build_body(meta(title="x" * 50 + "<>" + "x" * 100, description="d" * 6000, topics=[], hashtags={"youtube": ["#shorts", "#Python", "#python", "#"]}))
    assert body["snippet"]["title"] == "x" * 100  # angle brackets dropped, then clamped
    assert len(body["snippet"]["description"]) == 5000
    assert body["snippet"]["tags"] == ["shorts", "Python", "python"]
    assert body["snippet"]["categoryId"] == "28"
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": True}
    assert publisher.build_body(meta(title="  ", topics=[], hashtags={}))["snippet"] == {"title": "Clip", "description": "A summary.\n\n#shorts #python", "tags": [], "categoryId": "28"}


def test_build_body_publish_at_only_when_private(publisher: YouTubePublisher, yt_log):
    publisher.cfg.publish_at = "2030-01-31T18:00:00Z"
    assert publisher.build_body(meta())["status"]["publishAt"] == "2030-01-31T18:00:00Z"

    publisher.cfg.privacy = "public"
    with yt_log.at_level(logging.WARNING, logger=yt.log.name):
        status = publisher.build_body(meta())["status"]
    assert "publishAt" not in status and status["privacyStatus"] == "public"
    assert any("publish_at" in r.getMessage() and "ignored" in r.getMessage() for r in yt_log.records)

    publisher.cfg.privacy = "private"
    publisher.cfg.publish_at = "tomorrow-ish"
    with pytest.raises(PublishFatal, match="platforms.youtube.publish_at"):
        publisher.build_body(meta())


def test_video_tags_limit():
    tags = [f"tag{i:03d}" for i in range(120)]  # 6 chars each + comma = 7 -> 71 fit in 500
    assert len(yt.video_tags(meta(topics=tags))) == 71
    assert yt.video_tags(meta(topics=["two words", "x"])) == ["two words", "x"]
    assert yt.video_tags(meta(topics=[], hashtags={})) == []


def test_to_rfc3339():
    plus2 = timezone(timedelta(hours=2))
    assert yt.to_rfc3339("2026-09-15T10:30", local_tz=plus2) == "2026-09-15T08:30:00Z"
    assert yt.to_rfc3339("2026-09-15T00:30:15", local_tz=plus2) == "2026-09-14T22:30:15Z"
    assert yt.to_rfc3339("2026-09-15T08:30:00Z") == "2026-09-15T08:30:00Z"
    assert yt.to_rfc3339(" 2026-09-15T10:30:00-05:00 ") == "2026-09-15T15:30:00Z"
    assert yt.to_rfc3339("2026-09-15T10:30").endswith("Z")  # system local zone, whatever it is
    for bad in ("", "soon", "2026-13-01T00:00", "18:00"):
        with pytest.raises(ValueError, match="publish_at"):
            yt.to_rfc3339(bad)


# ---- error helpers ---------------------------------------------------------------------------------------------------
def test_error_helpers():
    assert yt._http_status(FakeHttpError(503)) == 503 and yt._http_status(OSError("x")) is None
    assert yt._error_detail(api_error(403, "quotaExceeded", "Quota.")) == "quotaExceeded: Quota."
    assert yt._error_detail(FakeHttpError(500, b"<html>oops")) == "<html>oops"
    assert yt._error_detail(FakeHttpError(500, "[1]")) == "[1]"
    assert yt._error_detail(TimeoutError("slow")) == "TimeoutError: slow"
    assert yt._is_transient(FakeHttpError(504)) and yt._is_transient(TimeoutError()) and not yt._is_transient(FakeHttpError(404))
    assert isinstance(yt._classify(FakeHttpError(404, b"nope")), PublishFatal)
    assert type(yt._classify(ValueError("odd"))) is PublishError


# ---- auth ------------------------------------------------------------------------------------------------------------
def test_auth_valid_token_skips_flow(publisher: YouTubePublisher, monkeypatch):
    monkeypatch.setattr(YouTubePublisher, "_load_credentials", lambda self: FakeCreds(valid=True))
    monkeypatch.setattr(YouTubePublisher, "_run_flow", lambda self: pytest.fail("flow must not run"))
    assert publisher.auth() is True and publisher.auth(interactive=False) is True
    assert not publisher.token_path.exists()  # nothing rewritten


def test_auth_refreshes_expired_token(publisher: YouTubePublisher, monkeypatch):
    creds = FakeCreds(valid=False, expired=True, refresh_token="r1")
    monkeypatch.setattr(YouTubePublisher, "_load_credentials", lambda self: creds)
    monkeypatch.setattr(YouTubePublisher, "_run_flow", lambda self: pytest.fail("flow must not run"))
    assert publisher.auth(interactive=False) is True
    assert creds.refreshed_with is not None and creds.valid
    saved = json.loads(publisher.token_path.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "r1" and saved["scopes"] == yt.SCOPES


def test_auth_refresh_failure_falls_back_to_flow(publisher: YouTubePublisher, monkeypatch):
    class Revoked(FakeCreds):
        def refresh(self, request):
            raise RuntimeError("invalid_grant")

    monkeypatch.setattr(YouTubePublisher, "_load_credentials", lambda self: Revoked(valid=False, expired=True, refresh_token="r"))
    monkeypatch.setattr(YouTubePublisher, "_run_flow", lambda self: FakeCreds(valid=True, refresh_token="new"))
    publisher.client_secret_path.write_text("{}", encoding="utf-8")
    assert publisher.auth(interactive=False) is False
    assert publisher.auth() is True
    assert json.loads(publisher.token_path.read_text(encoding="utf-8"))["refresh_token"] == "new"


def test_auth_missing_client_secret_prints_instructions(publisher: YouTubePublisher, capsys):
    assert not publisher.is_configured()
    assert publisher.auth() is False
    out = capsys.readouterr().out
    assert "YouTube Data API v3" in out and "Desktop app" in out and "test user" in out
    assert str(publisher.client_secret_path) in out and "clipforge auth youtube" in out
    assert publisher.auth(interactive=False) is False
    assert "Desktop app" not in capsys.readouterr().out  # non-interactive: no instructions, only a log line


def test_auth_runs_flow_and_saves_token(publisher: YouTubePublisher, monkeypatch):
    publisher.client_secret_path.write_text('{"installed": {}}', encoding="utf-8")
    monkeypatch.setattr(YouTubePublisher, "_run_flow", lambda self: FakeCreds(valid=True, refresh_token="fresh"))
    assert publisher.is_configured() and publisher.auth() is True
    assert publisher.token_path.is_file() and json.loads(publisher.token_path.read_text(encoding="utf-8"))["refresh_token"] == "fresh"


def test_auth_flow_failure_reports_false(publisher: YouTubePublisher, monkeypatch, capsys):
    publisher.client_secret_path.write_text("{}", encoding="utf-8")

    def boom(self):
        raise OSError("address already in use")

    monkeypatch.setattr(YouTubePublisher, "_run_flow", boom)
    assert publisher.auth() is False and not publisher.token_path.exists()
    assert "sign-in failed" in capsys.readouterr().out


def test_open_browser_env(monkeypatch):
    monkeypatch.delenv(yt.NO_BROWSER_ENV, raising=False)
    assert yt.open_browser_allowed()
    monkeypatch.setenv(yt.NO_BROWSER_ENV, "1")
    assert not yt.open_browser_allowed()


def test_load_credentials_real_loader(publisher: YouTubePublisher):
    """The unpatched loader: absent -> None, garbage -> None, a minimal authorized-user JSON -> valid Credentials."""
    assert publisher._load_credentials() is None
    publisher.token_path.write_text("not json", encoding="utf-8")
    assert publisher._load_credentials() is None
    publisher.token_path.write_text(json.dumps({"token": "x"}), encoding="utf-8")  # no refresh material
    assert publisher._load_credentials() is None
    info = {"token": "at", "refresh_token": "rt", "client_id": "cid", "client_secret": "cs", "token_uri": "https://oauth2.googleapis.com/token", "scopes": yt.SCOPES}
    publisher.token_path.write_text(json.dumps(info), encoding="utf-8")
    creds = publisher._load_credentials()
    assert creds is not None and creds.refresh_token == "rt" and creds.expired and not creds.valid  # no expiry saved -> refresh first
    publisher.token_path.write_text(json.dumps({**info, "expiry": "2999-01-01T00:00:00Z"}), encoding="utf-8")
    assert publisher._load_credentials().valid
    assert publisher.is_configured()


def test_is_configured_cases(publisher: YouTubePublisher, settings):
    assert not publisher.is_configured()
    publisher.token_path.write_text("{}", encoding="utf-8")
    assert publisher.is_configured()
    publisher.token_path.unlink()
    settings.platforms.youtube.client_secret = str(settings.workspace_dir / "elsewhere" / "cs.json")
    assert not publisher.is_configured()
    publisher.client_secret_path.parent.mkdir(parents=True)
    publisher.client_secret_path.write_text("{}", encoding="utf-8")
    assert publisher.is_configured() and publisher.client_secret_path.is_absolute()
