"""TikTok publisher, fully offline: a fake requests.Session with a queue of canned responses records every call."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import pytest

from clipforge.metadata import ClipMeta
from clipforge.publish import tiktok as tt
from clipforge.publish.base import AuthError, PublishError, PublishFatal
from clipforge.publish.tiktok import TikTokPublisher

MB = tt.MB
CHUNK = 1000  # forced tiny chunk size for upload tests
FILE_SIZE = 2500  # -> 2 chunks: (0, 999) and (1000, 2499)
REDIRECT = "https://example.com/cb"


# ---- fakes -----------------------------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.headers = headers or {}

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeSession:
    """Answers each request with the next queued response (or raises it); records (method, url, kwargs)."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kw) -> FakeResponse:
        self.calls.append((method, url, kw))
        assert kw["timeout"] == tt.HTTP_TIMEOUT_S
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok(data: dict) -> FakeResponse:
    return FakeResponse(200, {"data": data, "error": {"code": "ok", "message": "", "log_id": "L1"}})


def api_error(status: int, code: str, message: str = "boom") -> FakeResponse:
    return FakeResponse(status, {"data": {}, "error": {"code": code, "message": message, "log_id": "L2"}})


def creator(options: list[str] | None = None, **flags) -> FakeResponse:
    data = {
        "creator_avatar_url": "https://x/a.png",
        "creator_username": "clipper",
        "creator_nickname": "Clip [Forge]",
        "privacy_level_options": options if options is not None else ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": True,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 600,
    }
    data.update(flags)
    return ok(data)


def init_ok(publish_id: str = "p1") -> FakeResponse:
    return ok({"publish_id": publish_id, "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=u1"})


def status(state: str, **extra) -> FakeResponse:
    return ok({"status": state, **extra})


def token_payload(access: str = "tok", refresh: str = "rt") -> dict:
    return {
        "access_token": access,
        "expires_in": 86400,
        "refresh_token": refresh,
        "refresh_expires_in": 31536000,
        "open_id": "open-1",
        "scope": tt.SCOPES,
        "token_type": "Bearer",
    }


HAPPY = [creator(), init_ok(), FakeResponse(206), FakeResponse(201), status("PROCESSING_UPLOAD"), status("PUBLISH_COMPLETE", publicaly_available_post_id=["7"])]


# ---- fixtures ---------------------------------------------------------------------------------------------------------


@pytest.fixture()
def clip(settings, db, tmp_path: Path):
    db.add_video("vid1", "local", "in.mp4", title="Source")
    mp4 = tmp_path / "vid1_00.mp4"
    mp4.write_bytes(bytes(range(256)) * 9 + b"x" * (FILE_SIZE - 256 * 9))
    assert mp4.stat().st_size == FILE_SIZE
    db.upsert_clip("vid1_00", "vid1", 0, 0.0, 10.0, 1.0, "hook")
    db.update_clip("vid1_00", path=str(mp4), status="ready")
    return db.get_clip("vid1_00")


@pytest.fixture()
def meta() -> ClipMeta:
    return ClipMeta(title="Hello world", description="desc", hashtags={"tiktok": ["#fyp", "#test"], "youtube": ["#shorts"]})


def write_token(pub: TikTokPublisher, expires_in: float = 3600, refresh_in: float = 3600 * 24, access: str = "tok") -> Path:
    now = pub.now_fn()
    rec = {"access_token": access, "refresh_token": "rt", "expires_at": now + expires_in, "refresh_expires_at": now + refresh_in, "open_id": "o", "scope": tt.SCOPES}
    path = pub._token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


def saved_state(db, clip_id):
    raw = db.kv_get(tt.PUBLISH_ID_KEY.format(clip_id=clip_id))
    if raw is None:
        return None
    data = json.loads(raw)
    return data["publish_id"], data["uploaded"]


@pytest.fixture()
def pub(settings, db):
    """Configured publisher with a valid token, tiny chunks, fake session and recorded sleeps."""
    cfg = settings.platforms.tiktok
    cfg.client_key, cfg.client_secret, cfg.redirect_uri, cfg.chunk_size = "ck", "cs", REDIRECT, CHUNK
    cfg.privacy, cfg.music_usage_confirmed = "SELF_ONLY", True  # the user's explicit posting choices
    cfg.allow_comments = cfg.allow_duet = cfg.allow_stitch = True  # so init_body mirrors the creator's own settings below
    p = TikTokPublisher(settings, db)
    p._min_chunk = 1
    p.now_fn = lambda: 1_700_000_000.0
    p.sleeps = []
    p.sleep_fn = p.sleeps.append
    tt._random = lambda: 1.0  # pin the equal jitter to the full base delay
    p.input_fn = lambda prompt: (_ for _ in ()).throw(AssertionError("input() must not be called"))
    write_token(p)
    p._session = FakeSession([])
    return p


@pytest.fixture()
def warnings(monkeypatch) -> list[str]:
    out: list[str] = []
    monkeypatch.setattr(tt.log, "warning", lambda msg, *args: out.append(msg % args))
    return out


# ---- pure helpers -----------------------------------------------------------------------------------------------------


def test_chunk_plan_small_file_is_one_chunk():
    assert tt.chunk_plan(3 * MB) == (3 * MB, 1, [(0, 3 * MB - 1)])


def test_chunk_plan_splits_and_last_chunk_absorbs_remainder():
    chunk, total, ranges = tt.chunk_plan(23 * MB, 10 * MB)
    assert (chunk, total) == (10 * MB, 2)
    assert ranges == [(0, 10 * MB - 1), (10 * MB, 23 * MB - 1)]


def test_chunk_plan_clamps_to_limits():
    chunk, total, ranges = tt.chunk_plan(200 * MB, 100 * MB)
    assert chunk == tt.MAX_CHUNK == 64 * MB
    assert total == 200 * MB // tt.MAX_CHUNK == 3
    assert ranges[-1][1] == 200 * MB - 1 and ranges[0] == (0, tt.MAX_CHUNK - 1)
    chunk, total, ranges = tt.chunk_plan(12 * MB, 1)  # below the minimum -> MIN_CHUNK
    assert chunk == tt.MIN_CHUNK and total == 2 and ranges == [(0, 5 * MB - 1), (5 * MB, 12 * MB - 1)]
    chunk, total, _ = tt.chunk_plan(7 * MB, 10 * MB)  # chunk larger than the file -> one chunk of the whole file
    assert (chunk, total) == (7 * MB, 1)
    with pytest.raises(ValueError):
        tt.chunk_plan(0)


def test_content_range_and_caption_clamp():
    assert tt.content_range(0, 999, 2500) == "bytes 0-999/2500"
    assert tt.content_range(10 * MB, 23 * MB - 1, 23 * MB) == f"bytes {10 * MB}-{23 * MB - 1}/{23 * MB}"
    long = " ".join(["word"] * 600)
    assert len(tt.clamp_caption(long)) <= tt.TITLE_MAX and not tt.clamp_caption(long).endswith(" ")
    assert tt.clamp_caption("short #tag") == "short #tag"


def test_pkce_pair_and_auth_url():
    verifier, challenge = tt.pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in verifier
    assert challenge == tt.pkce_challenge(verifier)
    digest = hashlib.sha256(verifier.encode()).digest()
    assert tt.pkce_challenge(verifier, "hex") == digest.hex()
    assert tt.pkce_challenge(verifier, "base64url") == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    url = tt.build_auth_url("ck", REDIRECT, "st", challenge)
    assert url.startswith(tt.AUTH_URL + "?")
    assert "client_key=ck" in url and "scope=user.info.basic%2Cvideo.publish" in url and "code_challenge_method=S256" in url
    assert "response_type=code" in url and f"code_challenge={challenge}" in url and "state=st" in url


def test_parse_redirect_keeps_star_and_checks_state():
    assert tt.parse_redirect(f"{REDIRECT}?code=abc*123*def&state=S1&scopes=user.info.basic", "S1") == "abc*123*def"
    assert tt.parse_redirect(f"{REDIRECT}?code=abc%2A123&state=S1", "S1") == "abc*123"
    with pytest.raises(ValueError, match="state mismatch"):
        tt.parse_redirect(f"{REDIRECT}?code=abc&state=OTHER", "S1")
    with pytest.raises(ValueError, match="no 'code'"):
        tt.parse_redirect(f"{REDIRECT}?state=S1", "S1")
    with pytest.raises(ValueError, match="refused"):
        tt.parse_redirect(f"{REDIRECT}?error=access_denied&error_description=nope&state=S1", "S1")


def test_pick_privacy(warnings):
    assert tt.pick_privacy("PUBLIC_TO_EVERYONE", ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]) == "PUBLIC_TO_EVERYONE"
    assert tt.pick_privacy("PUBLIC_TO_EVERYONE", ["SELF_ONLY"]) == "SELF_ONLY"
    assert len(warnings) == 1 and "PUBLIC_TO_EVERYONE" in warnings[0]
    with pytest.raises(PublishFatal, match="MUTUAL_FOLLOW_FRIENDS"):
        tt.pick_privacy("PUBLIC_TO_EVERYONE", ["MUTUAL_FOLLOW_FRIENDS"])


def test_token_record_and_validity():
    rec = tt.token_record(token_payload(), 1000.0)
    assert rec == {"access_token": "tok", "refresh_token": "rt", "expires_at": 87400, "refresh_expires_at": 31537000, "open_id": "open-1", "scope": tt.SCOPES}
    refreshed = tt.token_record({"access_token": "new", "expires_in": 10}, 2000.0, previous=rec)
    assert refreshed["refresh_token"] == "rt" and refreshed["refresh_expires_at"] == 31537000 and refreshed["expires_at"] == 2010
    assert tt.token_valid(rec, 87400 - tt.REFRESH_MARGIN_S - 1) and not tt.token_valid(rec, 87400 - tt.REFRESH_MARGIN_S)
    assert tt.token_refreshable(rec, 31536999) and not tt.token_refreshable(rec, 31537000)
    assert not tt.token_refreshable({"access_token": "x"}, 0)


# ---- publish ------------------------------------------------------------------------------------------------------------


def test_publish_happy_path(pub, clip, meta, db, capsys):
    pub._session = FakeSession(HAPPY)
    assert pub.publish(clip, meta) == "7"

    calls = pub._session.calls
    assert [(m, u) for m, u, _ in calls[:2]] == [("POST", tt.CREATOR_INFO_URL), ("POST", tt.INIT_URL)]
    assert calls[0][2]["headers"] == {"Authorization": "Bearer tok", "Content-Type": tt.JSON_CONTENT_TYPE} and calls[0][2]["json"] == {}
    body = calls[1][2]["json"]
    assert calls[1][2]["headers"]["Authorization"] == "Bearer tok"
    assert body["post_info"] == {
        "title": "Hello world\n\n#fyp #test",
        "privacy_level": "SELF_ONLY",
        "disable_duet": True,
        "disable_comment": False,
        "disable_stitch": False,
        "video_cover_timestamp_ms": tt.COVER_TIMESTAMP_MS,
    }
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": FILE_SIZE, "chunk_size": CHUNK, "total_chunk_count": 2}

    puts = calls[2:4]
    assert all(m == "PUT" and u.startswith("https://open-upload.tiktokapis.com/") for m, u, _ in puts)
    assert puts[0][2]["headers"] == {"Content-Type": "video/mp4", "Content-Length": "1000", "Content-Range": "bytes 0-999/2500"}
    assert puts[1][2]["headers"] == {"Content-Type": "video/mp4", "Content-Length": "1500", "Content-Range": "bytes 1000-2499/2500"}
    assert puts[0][2]["data"] + puts[1][2]["data"] == Path(clip.path).read_bytes()

    assert [(m, u, kw["json"]) for m, u, kw in calls[4:]] == [("POST", tt.STATUS_URL, {"publish_id": "p1"})] * 2
    assert pub.sleeps == [tt.POLL_INTERVAL_S]

    assert db.is_posted(clip.id, "tiktok")
    post = db.get_post(clip.id, "tiktok")
    assert post.status == "posted" and post.post_id == "7"
    actions = [r["action"] for r in db.recent_log()]
    assert "publish" in actions and "warning" in actions
    out = capsys.readouterr().out
    assert "Clip [Forge]" in out and "@clipper" in out and "read this once" in out


def test_publish_falls_back_to_publish_id_without_public_id(pub, clip, meta):
    pub._session = FakeSession([creator(), init_ok("pid-9"), FakeResponse(206), FakeResponse(200), status("PUBLISH_COMPLETE")])
    assert pub.publish(clip, meta) == "pid-9"


def test_privacy_fallback_with_warning(pub, clip, meta, warnings):
    pub.cfg.privacy = "PUBLIC_TO_EVERYONE"
    pub._session = FakeSession([creator(["MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]), init_ok(), FakeResponse(206), FakeResponse(201), status("PUBLISH_COMPLETE", publicaly_available_post_id=["8"])])
    assert pub.publish(clip, meta) == "8"
    assert pub._session.calls[1][2]["json"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert any("PUBLIC_TO_EVERYONE" in w and "SELF_ONLY" in w for w in warnings)


def test_privacy_unavailable_is_fatal(pub, clip, meta, db):
    pub.cfg.privacy = "PUBLIC_TO_EVERYONE"
    pub._session = FakeSession([creator(["MUTUAL_FOLLOW_FRIENDS"])])
    with pytest.raises(PublishFatal, match="MUTUAL_FOLLOW_FRIENDS"):
        pub.publish(clip, meta)
    assert len(pub._session.calls) == 1
    post = db.get_post(clip.id, "tiktok")
    assert post.status == "failed" and post.attempts == 1 and "PUBLIC_TO_EVERYONE" in post.error


def test_failed_status_is_fatal_and_recorded(pub, clip, meta, db):
    pub._session = FakeSession([creator(), init_ok(), FakeResponse(206), FakeResponse(201), status("PROCESSING_UPLOAD"), status("FAILED", fail_reason="video_too_long")])
    with pytest.raises(PublishFatal, match="video_too_long"):
        pub.publish(clip, meta)
    post = db.get_post(clip.id, "tiktok")
    assert post.status == "failed" and "video_too_long" in post.error
    assert not db.is_posted(clip.id, "tiktok")
    assert any(r["action"] == "publish.failed" for r in db.recent_log())


def test_poll_timeout_is_retryable(pub, clip, meta, db):
    polls = tt.POLL_TIMEOUT_S // tt.POLL_INTERVAL_S + 1
    pub._session = FakeSession([creator(), init_ok(), FakeResponse(206), FakeResponse(201)] + [status("PROCESSING_UPLOAD")] * polls)
    with pytest.raises(PublishError) as ei:
        pub.publish(clip, meta)
    assert not isinstance(ei.value, PublishFatal)
    assert len(pub.sleeps) == polls - 1 and all(s == tt.POLL_INTERVAL_S for s in pub.sleeps)
    assert db.get_post(clip.id, "tiktok").status == "pending"
    assert saved_state(db, clip.id) == ("p1", True)  # the upload is complete: remembered for a resume


def test_retry_after_poll_timeout_resumes_the_same_publish_id_without_reupload(pub, clip, meta, db):
    """TikTok owns the video once every chunk is accepted; the next attempt must only poll, never init/upload again."""
    polls = tt.POLL_TIMEOUT_S // tt.POLL_INTERVAL_S + 1
    pub._session = FakeSession([creator(), init_ok("first"), FakeResponse(206), FakeResponse(201)] + [status("PROCESSING_UPLOAD")] * polls)
    with pytest.raises(PublishError, match="first"):
        pub.publish(clip, meta)

    pub._session = FakeSession([status("PROCESSING_DOWNLOAD"), status("PUBLISH_COMPLETE", publicaly_available_post_id=["77"])])
    assert pub.publish(clip, meta) == "77"
    assert [(m, u, kw["json"]) for m, u, kw in pub._session.calls] == [("POST", tt.STATUS_URL, {"publish_id": "first"})] * 2
    post = db.get_post(clip.id, "tiktok")
    assert post.status == "posted" and post.post_id == "77" and post.attempts == 1
    assert db.kv_get(tt.PUBLISH_ID_KEY.format(clip_id=clip.id)) is None

    # a status/fetch transport failure after the upload keeps the id as well
    db.add_video("vid2", "local", "b.mp4")
    db.upsert_clip("vid2_00", "vid2", 0, 0.0, 1.0, 0.0, "")
    db.update_clip("vid2_00", path=clip.path, status="ready")
    other = db.get_clip("vid2_00")
    pub._session = FakeSession([creator(), init_ok("second"), FakeResponse(206), FakeResponse(201), FakeResponse(500), FakeResponse(502), FakeResponse(503)])
    with pytest.raises(PublishError, match="after 3 attempts"):
        pub.publish(other, meta)
    assert saved_state(db, "vid2_00") == ("second", True)
    pub._session = FakeSession([status("PUBLISH_COMPLETE")])
    assert pub.publish(other, meta) == "second"


def test_resumed_publish_that_failed_starts_over_next_time(pub, clip, meta, db):
    db.kv_set(tt.PUBLISH_ID_KEY.format(clip_id=clip.id), "stale")
    pub._session = FakeSession([status("FAILED", fail_reason="frame_rate_check_failed")])
    with pytest.raises(PublishFatal, match="frame_rate_check_failed"):
        pub.publish(clip, meta)
    assert db.kv_get(tt.PUBLISH_ID_KEY.format(clip_id=clip.id)) is None and db.get_post(clip.id, "tiktok").status == "failed"
    db.mark_post_failed(clip.id, "tiktok", "reset", None)
    pub._session = FakeSession(HAPPY)  # fresh init + upload
    assert pub.publish(clip, meta) == "7" and [u for _, u, _ in pub._session.calls].count(tt.INIT_URL) == 1


def test_init_5xx_is_retried_then_succeeds(pub, clip, meta):
    pub._session = FakeSession([creator(), FakeResponse(500), FakeResponse(502), init_ok(), FakeResponse(206), FakeResponse(201), status("PUBLISH_COMPLETE", publicaly_available_post_id=["7"])])
    assert pub.publish(clip, meta) == "7"
    assert [u for _, u, _ in pub._session.calls].count(tt.INIT_URL) == 3
    assert pub.sleeps[:2] == [tt.RETRY_BACKOFF_S, tt.RETRY_BACKOFF_S * 2]


def test_persistent_5xx_and_connection_errors_become_publish_error(pub, clip, meta, db):
    import requests

    pub._session = FakeSession([creator(), FakeResponse(503), requests.ConnectionError("down"), FakeResponse(500)])
    with pytest.raises(PublishError, match="after 3 attempts") as ei:
        pub.publish(clip, meta)
    assert not isinstance(ei.value, PublishFatal)
    assert db.get_post(clip.id, "tiktok").status == "pending"


def test_chunk_upload_retries_on_5xx_and_rejects_bad_status(pub, clip, meta):
    pub._session = FakeSession([creator(), init_ok(), FakeResponse(500), FakeResponse(206), FakeResponse(201), status("PUBLISH_COMPLETE", publicaly_available_post_id=["7"])])
    assert pub.publish(clip, meta) == "7"
    puts = [(kw["headers"]["Content-Range"], kw["data"]) for m, _, kw in pub._session.calls if m == "PUT"]
    assert puts[0] == puts[1] and puts[0][0] == "bytes 0-999/2500"

    pub.db.mark_post_failed(clip.id, "tiktok", "reset", None)
    pub._session = FakeSession([creator(), init_ok(), FakeResponse(201)])  # final status on an intermediate chunk
    with pytest.raises(PublishFatal, match="unexpected HTTP 201"):
        pub.publish(clip, meta)


def test_api_error_codes_map_to_fatal_or_retryable(pub, clip, meta, db):
    pub._session = FakeSession([api_error(401, "access_token_invalid", "expired")])
    with pytest.raises(AuthError, match="access_token_invalid"):  # account-level: the row is not touched
        pub.publish(clip, meta)
    post = db.get_post(clip.id, "tiktok")
    assert post.status == "pending" and post.attempts == 0
    assert any(r["action"] == "publish.failed" and "access_token_invalid" in r["detail"] for r in db.recent_log())

    db.mark_post_failed(clip.id, "tiktok", "reset", None)  # back to pending so the next attempt is allowed
    pub._session = FakeSession([api_error(429, "rate_limit_exceeded")])
    with pytest.raises(PublishError, match="rate_limit_exceeded") as ei:
        pub.publish(clip, meta)
    assert not isinstance(ei.value, PublishFatal)
    assert db.get_post(clip.id, "tiktok").status == "pending"

    pub._session = FakeSession([creator(), api_error(200, "spam_risk_too_many_posts")])
    with pytest.raises(PublishFatal, match="spam_risk_too_many_posts"):
        pub.publish(clip, meta)


def test_expired_token_is_refreshed_first(pub, clip, meta):
    write_token(pub, expires_in=-10, access="old")
    pub._session = FakeSession([FakeResponse(200, token_payload("new", "rt2"))] + HAPPY)
    assert pub.publish(clip, meta) == "7"
    method, url, kw = pub._session.calls[0]
    assert (method, url) == ("POST", tt.TOKEN_URL)
    assert kw["data"] == {"client_key": "ck", "client_secret": "cs", "grant_type": "refresh_token", "refresh_token": "rt"}
    assert kw["headers"]["Content-Type"] == "application/x-www-form-urlencoded" and "json" not in kw
    assert pub._session.calls[1][2]["headers"]["Authorization"] == "Bearer new"
    saved = json.loads(pub._token_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "new" and saved["refresh_token"] == "rt2" and saved["expires_at"] == int(pub.now_fn()) + 86400


def test_unrefreshable_token_is_fatal_without_http(pub, clip, meta, db):
    write_token(pub, expires_in=-10, refresh_in=-10)
    assert pub.credentials_ok() is False
    with pytest.raises(AuthError, match="clipforge auth tiktok"):
        pub.publish(clip, meta)
    post = db.get_post(clip.id, "tiktok")
    assert pub._session.calls == [] and post.status == "pending" and post.attempts == 0  # not this clip's fault


def test_credentials_ok_probe(pub):
    assert pub.credentials_ok() is True and pub._session.calls == []
    write_token(pub, expires_in=-1, access="old")
    pub._session = FakeSession([FakeResponse(400, {"error": "invalid_grant", "error_description": "revoked"})])
    assert pub.credentials_ok() is False  # refresh refused -> platform skip, no exception
    pub._token_path.unlink()
    assert pub.credentials_ok() is False


def test_already_posted_clip_makes_no_http_call(pub, clip, meta, db):
    db.mark_posted(clip.id, "tiktok", "existing")
    with pytest.raises(PublishFatal, match="already posted"):
        pub.publish(clip, meta)
    assert pub._session.calls == []
    assert db.get_post(clip.id, "tiktok").status == "posted" and db.get_post(clip.id, "tiktok").post_id == "existing"


def test_allowance_exhausted_does_not_touch_post_row(pub, clip, meta, db):
    pub.cfg.per_day = 1
    db.add_video("vid2", "local", "b.mp4")
    db.upsert_clip("vid2_00", "vid2", 0, 0.0, 1.0, 0.0, "")
    db.mark_posted("vid2_00", "tiktok", "1")
    with pytest.raises(PublishFatal, match="allowance exhausted"):
        pub.publish(clip, meta)
    assert pub._session.calls == [] and db.get_post(clip.id, "tiktok") is None


def test_missing_clip_file_is_fatal(pub, clip, meta, db):
    Path(clip.path).unlink()
    with pytest.raises(PublishFatal, match="missing"):
        pub.publish(clip, meta)
    assert pub._session.calls == [] and db.get_post(clip.id, "tiktok").status == "failed"


# ---- limits / configured ------------------------------------------------------------------------------------------------


def test_limits_per_day(pub, db):
    pub.cfg.per_day = 2
    lim = pub.limits()
    assert (lim.per_day, lim.posted_today, lim.remaining, lim.exhausted) == (2, 0, 2, False)
    assert "unaudited" in lim.note and "SELF_ONLY" in lim.note
    db.add_video("v", "local", "x.mp4")
    for i in range(2):
        db.upsert_clip(f"v_0{i}", "v", i, 0.0, 1.0, 0.0, "")
        db.mark_posted(f"v_0{i}", "tiktok", str(i))
    lim = pub.limits()
    assert (lim.posted_today, lim.remaining, lim.exhausted) == (2, 0, True)
    with pytest.raises(PublishFatal, match="allowance exhausted"):
        pub.ensure_allowance()


def test_is_configured_cases(settings, db):
    p = TikTokPublisher(settings, db)
    assert not p.is_configured()
    p.cfg.client_key, p.cfg.client_secret = "ck", "cs"
    assert not p.is_configured()
    write_token(p)
    assert p.is_configured()
    assert p._token_path == settings.workspace_dir / "tiktok_token.json"
    p.cfg.client_secret = ""
    assert not p.is_configured()


# ---- auth ---------------------------------------------------------------------------------------------------------------


def test_auth_paste_back_flow(pub, monkeypatch, capsys):
    pub._token_path.unlink()
    monkeypatch.setattr(tt, "new_state", lambda: "STATE1")
    prompts: list[str] = []

    def paste(prompt: str) -> str:
        prompts.append(prompt)
        return f"{REDIRECT}?code=abc*123*def&scopes=user.info.basic%2Cvideo.publish&state=STATE1"

    pub.input_fn = paste
    pub.cfg.client_secret = "SEKRIT-VALUE"
    pub._session = FakeSession([FakeResponse(200, token_payload("ACCESS-TOKEN-VALUE"))])
    assert pub.auth() is True
    assert len(prompts) == 1
    method, url, kw = pub._session.calls[0]
    assert (method, url) == ("POST", tt.TOKEN_URL)
    form = kw["data"]
    assert form["code"] == "abc*123*def" and form["grant_type"] == "authorization_code"
    assert form["client_key"] == "ck" and form["client_secret"] == "SEKRIT-VALUE" and form["redirect_uri"] == REDIRECT
    out = capsys.readouterr().out
    assert tt.AUTH_URL in out and "state=STATE1" in out and f"code_challenge={tt.pkce_challenge(form['code_verifier'])}" in out
    saved = json.loads(pub._token_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "ACCESS-TOKEN-VALUE" and saved["open_id"] == "open-1"
    assert "SEKRIT-VALUE" not in out and "ACCESS-TOKEN-VALUE" not in out
    assert pub.is_configured() and pub.auth() is True  # valid token now -> no prompt, no HTTP
    assert len(pub._session.calls) == 1


def test_auth_rejects_state_mismatch(pub):
    pub._token_path.unlink()
    pub.input_fn = lambda prompt: f"{REDIRECT}?code=abc&state=WRONG"
    assert pub.auth() is False
    assert pub._session.calls == [] and not pub._token_path.exists()


def test_auth_token_exchange_error_returns_false(pub, monkeypatch, capsys):
    pub._token_path.unlink()
    monkeypatch.setattr(tt, "new_state", lambda: "S9")
    pub.input_fn = lambda prompt: f"{REDIRECT}?code=abc&state=S9"
    pub._session = FakeSession([FakeResponse(400, {"error": "invalid_grant", "error_description": "bad code"})])
    assert pub.auth() is False
    assert not pub._token_path.exists()
    assert "invalid_grant" in capsys.readouterr().out


def test_auth_refreshes_expired_token_without_prompt(pub):
    write_token(pub, expires_in=-1, access="old")
    pub._session = FakeSession([FakeResponse(200, token_payload("fresh"))])
    assert pub.auth() is True
    assert pub._session.calls[0][2]["data"]["grant_type"] == "refresh_token"
    assert json.loads(pub._token_path.read_text(encoding="utf-8"))["access_token"] == "fresh"


def test_auth_without_credentials_prints_help(settings, db, capsys):
    p = TikTokPublisher(settings, db)
    p.input_fn = lambda prompt: (_ for _ in ()).throw(AssertionError("no prompt expected"))
    assert p.auth() is False
    out = capsys.readouterr().out
    assert "developers.tiktok.com" in out and "video.publish" in out and "redirect URI" in out


def test_publisher_factory_and_time_defaults(settings, db):
    from clipforge.publish import get_publisher

    p = get_publisher("tiktok", settings, db)
    assert isinstance(p, TikTokPublisher) and p.name == "tiktok"
    assert p.sleep_fn is time.sleep and p.now_fn is time.time and p.input_fn is input


# ---- review fixes: no duplicate posts on retry, posting choices ---------------------------------------------------------
def test_lost_final_upload_response_resumes_the_same_upload(pub, clip, meta, db):
    """The last PUT's response never arrives: the next attempt resumes the recorded init instead of a second post."""
    import requests as rq

    pub._session = FakeSession([creator(), init_ok("first"), FakeResponse(206), rq.ConnectionError("reset"), rq.ConnectionError("reset"), rq.ConnectionError("reset")])
    with pytest.raises(PublishError):
        pub.publish(clip, meta)
    assert saved_state(db, clip.id) == ("first", False)
    pub._session = FakeSession([status("PROCESSING_UPLOAD"), FakeResponse(206), FakeResponse(201), status("PUBLISH_COMPLETE", publicaly_available_post_id=["9"])])
    assert pub.publish(clip, meta) == "9"
    urls = [u for _, u, _ in pub._session.calls]
    assert tt.INIT_URL not in urls and tt.CREATOR_INFO_URL not in urls and urls.count("https://open-upload.tiktokapis.com/video/?upload_id=u1") == 2
    assert saved_state(db, clip.id) is None and db.is_posted(clip.id, "tiktok")


def test_resume_finds_the_interrupted_upload_already_processed(pub, clip, meta, db):
    db.kv_set(tt.PUBLISH_ID_KEY.format(clip_id=clip.id), json.dumps({"publish_id": "x1", "upload_url": "https://u", "chunk_size": CHUNK, "uploaded": False}))
    pub._session = FakeSession([status("PROCESSING_DOWNLOAD"), status("PUBLISH_COMPLETE", publicaly_available_post_id=["5"])])
    assert pub.publish(clip, meta) == "5"
    assert [u for _, u, _ in pub._session.calls] == [tt.STATUS_URL, tt.STATUS_URL]


def test_state_survives_a_failure_to_record_success(pub, clip, meta, db, monkeypatch):
    import sqlite3

    pub._session = FakeSession(HAPPY)
    real = db.mark_posted

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "mark_posted", broken)
    with pytest.raises(sqlite3.OperationalError):
        pub.publish(clip, meta)
    assert saved_state(db, clip.id) == ("p1", True)  # TikTok has the post; remembered until success is on record
    monkeypatch.setattr(db, "mark_posted", real)
    pub._session = FakeSession([status("PUBLISH_COMPLETE", publicaly_available_post_id=["7"])])
    assert pub.publish(clip, meta) == "7" and saved_state(db, clip.id) is None and db.is_posted(clip.id, "tiktok")
    assert [u for _, u, _ in pub._session.calls] == [tt.STATUS_URL]  # no second upload


def test_posting_choices_are_required_before_anything_is_recorded(pub, clip, meta, db):
    pub._session = FakeSession([])
    pub.cfg.privacy = None
    with pytest.raises(PublishFatal, match="choose who can view"):
        pub.publish(clip, meta)
    pub.cfg.privacy = "SELF_ONLY"
    pub.cfg.music_usage_confirmed = False
    with pytest.raises(PublishFatal, match="Music Usage"):
        pub.publish(clip, meta)
    pub.cfg.music_usage_confirmed = True
    pub.cfg.commercial_content = pub.cfg.branded_content = True
    with pytest.raises(PublishFatal, match="branded content"):
        pub.publish(clip, meta)
    assert pub._session.calls == [] and db.get_post(clip.id, "tiktok") is None


def test_init_body_interactions_off_unless_enabled_and_disclosure_toggles():
    from clipforge.config import TikTokCfg

    cr = {"comment_disabled": False, "duet_disabled": False, "stitch_disabled": False}
    body = tt.init_body("c", "SELF_ONLY", cr, 10, 10, 1)["post_info"]
    assert (body["disable_comment"], body["disable_duet"], body["disable_stitch"]) == (True, True, True) and "brand_content_toggle" not in body
    cfg = TikTokCfg(allow_comments=True, allow_duet=True, commercial_content=True, brand_organic=True)
    body = tt.init_body("c", "SELF_ONLY", cr, 10, 10, 1, cfg)["post_info"]
    assert (body["disable_comment"], body["disable_duet"], body["disable_stitch"]) == (False, False, True)
    assert body["brand_organic_toggle"] is True and body["brand_content_toggle"] is False
    body = tt.init_body("c", "SELF_ONLY", {"comment_disabled": True}, 10, 10, 1, cfg)["post_info"]
    assert body["disable_comment"] is True  # the creator's own setting still wins


# ---- hardening batch 2: jitter + Retry-After -----------------------------------------------------------------------
def test_retry_after_and_jitter_on_requests(pub, clip, meta, db, monkeypatch):
    monkeypatch.setattr(tt, "_random", lambda: 0.0)
    pub._session = FakeSession([FakeResponse(503, headers={"Retry-After": "5"}), FakeResponse(429, headers={"Retry-After": "3"}), creator(), init_ok(), FakeResponse(206), FakeResponse(201), status("PUBLISH_COMPLETE", publicaly_available_post_id=["7"])])
    assert pub.publish(clip, meta) == "7"
    assert pub.sleeps[:2] == [5.0, 3.0]  # hints win over the 1.0 / 2.0 jittered bases


def test_429_without_hint_is_a_retry_later_error(pub, clip, meta, db):
    pub._session = FakeSession([api_error(429, "rate_limit_exceeded")])
    with pytest.raises(PublishError) as ei:
        pub.publish(clip, meta)
    assert not isinstance(ei.value, PublishFatal) and pub.sleeps == []


def test_retry_after_parsing():
    assert tt._retry_after(FakeResponse(503, headers={"Retry-After": "600"})) == tt.RETRY_AFTER_MAX_S
    assert tt._retry_after(FakeResponse(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None
    assert tt._retry_after(FakeResponse(503)) is None
