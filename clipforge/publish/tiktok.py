"""TikTok publisher: Content Posting API, Direct Post with FILE_UPLOAD (chunked PUTs) and paste-back OAuth (PKCE).

publish() flow: ensure_not_posted -> ensure_allowance -> warn_once -> access token (refreshed when within
REFRESH_MARGIN_S of expiry) -> creator_info/query (privacy options + creator name) -> video/init -> PUT chunks
(read range by range, never the whole file) -> poll status/fetch -> mark_posted.  Every attempt is recorded in the
posts table and the log table.  Secrets (tokens, client secret) are never logged or printed.

Once every chunk is accepted TikTok owns the video and will publish it, so the publish_id is persisted in the kv
table (PUBLISH_ID_KEY) before polling; if polling times out, fails on transport or the process dies, the next attempt
resumes polling that id instead of uploading the clip a second time. The key is cleared once the outcome is known.

Docs: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post (init/upload/status),
https://developers.tiktok.com/doc/content-posting-api-reference-errors (error codes),
https://developers.tiktok.com/doc/login-kit-desktop (authorize URL, PKCE) and
https://developers.tiktok.com/doc/oauth-user-access-token-management (token endpoint).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from rich.markup import escape

from ..config import Settings, TikTokCfg
from ..db import DB, Clip
from ..log import console, get_logger
from ..metadata import ClipMeta
from .base import AuthError, Limits, PublishError, PublishFatal, Publisher

log = get_logger(__name__)

# ---- endpoints (verify: login-kit-desktop, oauth-user-access-token-management, content-posting-api-reference-direct-post)
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.publish"  # comma-separated; video.publish is the Direct Post scope

# ---- OAuth (verify: https://developers.tiktok.com/doc/login-kit-desktop)
STATE_BYTES = 16  # random state length (token_urlsafe input bytes)
VERIFIER_BYTES = 32  # 32 bytes -> 43-char base64url code_verifier (RFC 7636 allows 43..128 chars)
# TikTok's desktop Login Kit doc sample builds code_challenge as sha256(code_verifier).hexdigest(), unlike RFC 7636
# (base64url).  Switch to "base64url" if the token exchange answers "invalid code_verifier"; verify on the doc page.
PKCE_CHALLENGE_ENCODING = "hex"
REFRESH_MARGIN_S = 300  # refresh the access token when it expires within 5 minutes
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"}
JSON_CONTENT_TYPE = "application/json; charset=UTF-8"

# ---- upload (verify: content-posting-api-reference-direct-post, "source_info" / "Upload video")
MB = 1024 * 1024
MIN_CHUNK = 5 * MB  # chunk_size lower bound; a file smaller than this is uploaded as one chunk
MAX_CHUNK = 64 * MB  # chunk_size upper bound; the final chunk absorbs the remainder (< 2 * chunk_size <= 128 MB)
DEFAULT_CHUNK = TikTokCfg().chunk_size
UPLOAD_CONTENT_TYPE = "video/mp4"
UPLOAD_PARTIAL_STATUS = 206  # every chunk but the last
UPLOAD_DONE_STATUSES = (201, 200)  # the last chunk
TITLE_MAX = 2200  # post_info.title (caption + hashtags) character limit
COVER_TIMESTAMP_MS = 1000  # post_info.video_cover_timestamp_ms: frame used as cover
SOURCE_FILE_UPLOAD = "FILE_UPLOAD"
FALLBACK_PRIVACY = "SELF_ONLY"  # the only level unaudited apps may use

# ---- status polling (verify: content-posting-api-reference-get-video-status)
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 600
STATUS_COMPLETE = "PUBLISH_COMPLETE"
STATUS_FAILED = "FAILED"
STATUS_IN_PROGRESS = frozenset({"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "SEND_TO_USER_INBOX"})
POST_ID_FIELD = "publicaly_available_post_id"  # sic - TikTok's field name

# ---- transport
HTTP_TIMEOUT_S = 60
RETRY_ATTEMPTS = 3  # per request, on 5xx / connection errors
RETRY_BACKOFF_S = 2.0  # sleep RETRY_BACKOFF_S * 2**(attempt-1) between attempts
RATE_LIMIT_STATUS = 429
# error.code values that mean "try again later" (verify: content-posting-api-reference-errors); every other non-ok
# code (access_token_invalid, scope_not_authorized, invalid_params, spam_risk_*, ...) is fatal for this attempt.
RETRYABLE_ERROR_CODES = frozenset({"rate_limit_exceeded", "internal_error"})
AUTH_ERROR_CODES = frozenset({"access_token_invalid"})  # account-level: the row is left untouched, re-run `clipforge auth tiktok`
PUBLISH_ID_KEY = "tiktok.publish_id.{clip_id}"  # kv: JSON {publish_id, upload_url, chunk_size, uploaded} of an upload in flight; cleared only after mark_posted
PRIVACY_LEVELS = ("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE")
POSTING_CHOICES_DOC = "https://developers.tiktok.com/doc/content-sharing-guidelines"

UNAUDITED_NOTE = "unaudited apps: SELF_ONLY, private account, <= 5 users / 24 h"
SETUP_HELP = (
    "TikTok is not configured. Click-path:\n"
    "  1. https://developers.tiktok.com/ -> Manage apps -> create or open your app\n"
    "  2. Add the products Login Kit and Content Posting API, enable Direct Post\n"
    "  3. Scopes: user.info.basic, video.publish\n"
    "  4. Login Kit -> register a redirect URI (any page you can read back from the address bar)\n"
    "  5. Copy the client key and client secret into clipforge.yaml under platforms.tiktok\n"
    "     (client_key, client_secret, redirect_uri; env: CLIPFORGE_PLATFORMS__TIKTOK__CLIENT_KEY ...)\n"
    "  then run: clipforge auth tiktok"
)

ChunkRanges = list[tuple[int, int]]


class PublishRejected(PublishFatal):
    """TikTok processed the upload and reported status FAILED (the publish_id is dead; a new attempt starts over)."""


# ---- pure helpers -----------------------------------------------------------------------------------------------------


def chunk_plan(video_size: int, chunk_size: int = DEFAULT_CHUNK, *, min_chunk: int = MIN_CHUNK, max_chunk: int = MAX_CHUNK) -> tuple[int, int, ChunkRanges]:
    """(chunk_size, total_chunk_count, [(first_byte, last_byte), ...]) following TikTok's FILE_UPLOAD rules.

    A file smaller than `min_chunk` is a single chunk of its whole size.  Otherwise chunk_size is clamped to
    [min_chunk, min(max_chunk, video_size)], total = video_size // chunk_size and the last chunk takes the remainder.
    """
    if video_size <= 0:
        raise ValueError(f"video_size must be positive, got {video_size}")
    if video_size < min_chunk:
        return video_size, 1, [(0, video_size - 1)]
    chunk = max(min_chunk, min(chunk_size, max_chunk, video_size))
    total = video_size // chunk
    ranges = [(i * chunk, (i + 1) * chunk - 1 if i < total - 1 else video_size - 1) for i in range(total)]
    return chunk, total, ranges


def content_range(first: int, last: int, total: int) -> str:
    """HTTP Content-Range header value for one chunk: 'bytes first-last/total'."""
    return f"bytes {first}-{last}/{total}"


def clamp_caption(text: str, limit: int = TITLE_MAX) -> str:
    """Cut the caption to `limit` chars on a word boundary (TikTok rejects longer titles)."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip()


def new_state() -> str:
    return secrets.token_urlsafe(STATE_BYTES)


def pkce_challenge(verifier: str, encoding: str = PKCE_CHALLENGE_ENCODING) -> str:
    """S256 code_challenge of `verifier`, hex (TikTok's documented sample) or base64url without padding (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    if encoding == "hex":
        return digest.hex()
    if encoding == "base64url":
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    raise ValueError(f"unknown PKCE encoding {encoding!r}")


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for one authorization."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(VERIFIER_BYTES)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def build_auth_url(client_key: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_key": client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urlencode(params)


def parse_redirect(pasted_url: str, expected_state: str) -> str:
    """The authorization `code` from the URL the browser was redirected to.

    parse_qs percent-decodes once, so a code containing '*' (TikTok codes do) comes back verbatim; the token request
    form-encodes it exactly once again, as TikTok's own curl sample does.  ValueError on a missing code or a state
    mismatch (CSRF guard).
    """
    query = parse_qs(urlsplit(pasted_url.strip()).query, keep_blank_values=True)
    if query.get("error"):
        raise ValueError(f"TikTok refused the authorization: {query['error'][0]} {query.get('error_description', [''])[0]}".strip())
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("no 'code' parameter in the pasted URL (paste the full URL from the address bar)")
    if query.get("state", [""])[0] != expected_state:
        raise ValueError("state mismatch: the pasted URL does not belong to this authorization attempt")
    return code


def token_record(payload: dict[str, Any], now: float, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Token file shape from an OAuth token response (fields missing from a refresh answer are kept from `previous`)."""
    prev = previous or {}
    refresh_in = payload.get("refresh_expires_in")
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or prev.get("refresh_token", ""),
        "expires_at": int(now + int(payload.get("expires_in", 0))),
        "refresh_expires_at": int(now + int(refresh_in)) if refresh_in else prev.get("refresh_expires_at"),
        "open_id": payload.get("open_id") or prev.get("open_id", ""),
        "scope": payload.get("scope") or prev.get("scope", ""),
    }


def token_valid(tok: dict[str, Any], now: float) -> bool:
    return bool(tok.get("access_token")) and float(tok.get("expires_at", 0)) - now > REFRESH_MARGIN_S


def token_refreshable(tok: dict[str, Any], now: float) -> bool:
    if not tok.get("refresh_token"):
        return False
    refresh_expires_at = tok.get("refresh_expires_at")
    return refresh_expires_at is None or float(refresh_expires_at) > now


def pick_privacy(wanted: str, options: list[str]) -> str:
    """`wanted` when creator_info offers it; else SELF_ONLY with a warning; else PublishFatal listing the options."""
    if wanted in options:
        return wanted
    offered = ", ".join(options) or "nothing"
    if FALLBACK_PRIVACY in options:
        log.warning("tiktok: privacy %r not offered by creator_info (offers: %s); posting %s instead", wanted, offered, FALLBACK_PRIVACY)
        return FALLBACK_PRIVACY
    raise PublishFatal(f"tiktok: privacy {wanted!r} not available and no {FALLBACK_PRIVACY} either; creator_info offers: {offered}")


def init_body(caption: str, privacy: str, creator: dict[str, Any], video_size: int, chunk_size: int, total_chunks: int, cfg: TikTokCfg | None = None) -> dict[str, Any]:
    """video/init request body. Interactions are OFF unless the user enabled them in config AND the creator allows
    them (TikTok's guidelines: no auto-enabled interactions); commercial disclosure toggles only when declared."""
    allow = {
        "comment": bool(cfg.allow_comments) if cfg else False,
        "duet": bool(cfg.allow_duet) if cfg else False,
        "stitch": bool(cfg.allow_stitch) if cfg else False,
    }
    post_info: dict[str, Any] = {
        "title": clamp_caption(caption),
        "privacy_level": privacy,
        "disable_duet": bool(creator.get("duet_disabled", False)) or not allow["duet"],
        "disable_comment": bool(creator.get("comment_disabled", False)) or not allow["comment"],
        "disable_stitch": bool(creator.get("stitch_disabled", False)) or not allow["stitch"],
        "video_cover_timestamp_ms": COVER_TIMESTAMP_MS,
    }
    if cfg is not None and cfg.commercial_content:
        post_info["brand_content_toggle"] = bool(cfg.branded_content)
        post_info["brand_organic_toggle"] = bool(cfg.brand_organic)
    return {
        "post_info": post_info,
        "source_info": {
            "source": SOURCE_FILE_UPLOAD,
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }


def api_data(resp: requests.Response, what: str) -> dict[str, Any]:
    """The `data` object of a TikTok v2 response; PublishError when retryable, PublishFatal otherwise."""
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    code, message, log_id = str(err.get("code", "")), str(err.get("message", "")), str(err.get("log_id", ""))
    detail = f"HTTP {resp.status_code} {code}: {message} (log_id {log_id})".strip()
    if resp.status_code == RATE_LIMIT_STATUS or code in RETRYABLE_ERROR_CODES:
        raise PublishError(f"tiktok {what}: {detail} - retry later")
    if code in AUTH_ERROR_CODES:
        raise AuthError(f"tiktok {what}: {detail} - run `clipforge auth tiktok`")
    if resp.status_code >= 400 or code != "ok":
        raise PublishFatal(f"tiktok {what}: {detail}")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


# ---- publisher ----------------------------------------------------------------------------------------------------------


class TikTokPublisher(Publisher):
    name = "tiktok"

    def __init__(self, settings: Settings, db: DB):
        super().__init__(settings, db)
        self.cfg: TikTokCfg = settings.platforms.tiktok
        self._session = requests.Session()
        self._min_chunk = MIN_CHUNK  # tests lower this to exercise multi-chunk uploads with tiny files
        self.input_fn: Callable[[str], str] = input
        self.sleep_fn: Callable[[float], None] = time.sleep
        self.now_fn: Callable[[], float] = time.time

    # ---- Publisher API ------------------------------------------------------------------------------------------------
    def is_configured(self) -> bool:
        return self._has_app_credentials() and self._token_path.is_file()

    def credentials_ok(self) -> bool:
        try:
            return self._usable_token() is not None
        except PublishError:  # refresh refused
            return False

    def limits(self) -> Limits:
        posted = self.posted_today()
        return Limits(per_day=self.cfg.per_day, posted_today=posted, remaining=max(0, self.cfg.per_day - posted), note=UNAUDITED_NOTE)

    def auth(self) -> bool:
        try:
            if self._usable_token():
                return True
        except PublishError as e:  # refresh rejected -> authorize again interactively
            log.warning("tiktok: stored token unusable (%s); re-authorizing", e)
        if not (self._has_app_credentials() and self.cfg.redirect_uri):
            console.print(SETUP_HELP, markup=False, highlight=False)
            return False
        try:
            return self._interactive_auth()
        except PublishError as e:
            console.print(f"[red]tiktok auth failed:[/] {escape(str(e))}")
            log.error("tiktok auth failed: %s", e)
            return False

    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        self.ensure_not_posted(clip)
        self.ensure_allowance()
        self.check_posting_choices()  # a config gap is not a clip failure: nothing is recorded against the clip
        self.warn_once()
        self.db.ensure_post(clip.id, self.name)
        try:
            post_id = self._publish_flow(clip, meta)
        except PublishError as err:
            if not isinstance(err, AuthError):  # an AuthError is the account's problem, not the clip's: row untouched
                self.db.mark_post_failed(clip.id, self.name, str(err), next_attempt_at=None, final=isinstance(err, PublishFatal))
            self.db.log("publish.failed", f"tiktok {clip.id}: {err}", level="error")
            log.error("tiktok: publish %s failed: %s", clip.id, err)
            raise
        self.db.mark_posted(clip.id, self.name, post_id)
        self.db.kv_delete(PUBLISH_ID_KEY.format(clip_id=clip.id))  # only now: success is on record
        self.db.log("publish", f"tiktok {clip.id} -> post {post_id}")
        log.info("tiktok: posted %s as %s", clip.id, post_id)
        return post_id

    # ---- publish flow -------------------------------------------------------------------------------------------------
    def _publish_flow(self, clip: Clip, meta: ClipMeta) -> str:
        path = Path(clip.path or "")
        if not path.is_file():
            raise PublishFatal(f"tiktok: clip file missing: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise PublishFatal(f"tiktok: clip file is empty: {path}")
        token = self._access_token()
        key = PUBLISH_ID_KEY.format(clip_id=clip.id)
        state = self._load_state(key)
        if state:
            return self._resume(token, key, state, path, size)
        creator = self._creator_info(token)
        privacy = pick_privacy(str(self.cfg.privacy), [str(o) for o in creator.get("privacy_level_options") or []])
        chunk, total, ranges = chunk_plan(size, self.cfg.chunk_size, min_chunk=self._min_chunk)
        init = self._api_call(INIT_URL, token, init_body(meta.caption_for(self.name), privacy, creator, size, chunk, total, self.cfg), "video/init")
        publish_id, upload_url = str(init.get("publish_id") or ""), str(init.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise PublishFatal("tiktok video/init: response lacks publish_id or upload_url")
        state = {"publish_id": publish_id, "upload_url": upload_url, "chunk_size": chunk, "uploaded": False}
        self.db.kv_set(key, json.dumps(state))  # recorded before the first byte: a retry resumes this init, never starts a second one
        log.info("tiktok: uploading %s (%d bytes, %d chunk(s), %s) publish_id=%s", path.name, size, total, privacy, publish_id)
        self._upload(path, upload_url, ranges, size)
        state["uploaded"] = True
        self.db.kv_set(key, json.dumps(state))
        return self._finish_publish(token, key, publish_id)

    def check_posting_choices(self) -> None:
        """TikTok's guidelines require the user to choose privacy and interactions and to accept the music terms."""
        if not self.cfg.privacy:
            raise PublishFatal(
                "tiktok: choose who can view the post first - set platforms.tiktok.privacy to one of "
                f"{', '.join(PRIVACY_LEVELS)} (the UI's Publish tab has the form); TikTok forbids a default ({POSTING_CHOICES_DOC})"
            )
        if not self.cfg.music_usage_confirmed:
            raise PublishFatal("tiktok: accept TikTok's Music Usage Confirmation first (platforms.tiktok.music_usage_confirmed: true)")
        if self.cfg.commercial_content and self.cfg.branded_content and self.cfg.privacy == "SELF_ONLY":
            raise PublishFatal("tiktok: branded content cannot be posted as SELF_ONLY; choose a wider privacy level or untick branded content")

    def _load_state(self, key: str) -> dict[str, Any] | None:
        raw = self.db.kv_get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:  # pre-JSON format: a bare publish_id whose upload had completed
            data = {"publish_id": raw, "upload_url": "", "chunk_size": self.cfg.chunk_size, "uploaded": True}
        return data if isinstance(data, dict) and data.get("publish_id") else None

    def _resume(self, token: str, key: str, state: dict[str, Any], path: Path, size: int) -> str:
        """Continue an init recorded by an earlier attempt instead of creating a second post for the same clip."""
        publish_id = str(state["publish_id"])
        if not state.get("uploaded"):
            status = self._status(token, publish_id)
            log.info("tiktok: resuming publish_id=%s for %s (status %s)", publish_id, path.name, status or "unknown")
            if status == STATUS_FAILED:
                self.db.kv_delete(key)
                raise PublishError(f"tiktok: the interrupted upload {publish_id} was rejected; the next attempt starts a new one")
            if status in ("", "PROCESSING_UPLOAD"):  # bytes still missing: PUTs by byte range are idempotent on the same URL
                _, _, ranges = chunk_plan(size, int(state.get("chunk_size") or self.cfg.chunk_size), min_chunk=self._min_chunk)
                try:
                    self._upload(path, str(state["upload_url"]), ranges, size)
                except PublishFatal as err:  # the upload URL no longer accepts data: that init can never complete
                    self.db.kv_delete(key)
                    raise PublishError(f"tiktok: could not resume upload {publish_id} ({err}); the next attempt starts a new one") from err
            state["uploaded"] = True
            self.db.kv_set(key, json.dumps(state))
        else:
            log.info("tiktok: resuming publish_id=%s for %s (upload complete; polling status)", publish_id, path.name)
        return self._finish_publish(token, key, publish_id)

    def _status(self, token: str, publish_id: str) -> str:
        data = self._api_call(STATUS_URL, token, {"publish_id": publish_id}, "status/fetch")
        return str(data.get("status") or "")

    def _finish_publish(self, token: str, key: str, publish_id: str) -> str:
        """Poll until the outcome is known. The stored state survives until publish() has recorded success, so a
        crash or DB error after PUBLISH_COMPLETE resumes with a status poll instead of a second upload."""
        try:
            done = self._wait_for_publish(token, publish_id)
        except PublishRejected:
            self.db.kv_delete(key)
            raise
        ids = done.get(POST_ID_FIELD) or []
        return str(ids[0]) if ids else publish_id

    def _creator_info(self, token: str) -> dict[str, Any]:
        info = self._api_call(CREATOR_INFO_URL, token, {}, "creator_info/query")
        nickname, username = str(info.get("creator_nickname") or "?"), str(info.get("creator_username") or "?")
        console.print(f"TikTok account: {escape(nickname)} (@{escape(username)})")
        return info

    def _upload(self, path: Path, upload_url: str, ranges: ChunkRanges, size: int) -> None:
        """PUT each byte range; the file is read one chunk at a time."""
        total = len(ranges)
        with path.open("rb") as fh:
            for i, (first, last) in enumerate(ranges, 1):
                fh.seek(first)
                self._put_chunk(upload_url, fh.read(last - first + 1), first, last, size, final=i == total, what=f"upload chunk {i}/{total}")

    def _put_chunk(self, upload_url: str, data: bytes, first: int, last: int, size: int, *, final: bool, what: str) -> None:
        headers = {"Content-Type": UPLOAD_CONTENT_TYPE, "Content-Length": str(len(data)), "Content-Range": content_range(first, last, size)}
        resp = self._request("PUT", upload_url, what, headers=headers, data=data)
        expected = UPLOAD_DONE_STATUSES if final else (UPLOAD_PARTIAL_STATUS,)
        if resp.status_code not in expected:
            raise PublishFatal(f"tiktok {what}: unexpected HTTP {resp.status_code} (expected {' or '.join(map(str, expected))})")
        log.debug("tiktok: %s ok (%s)", what, headers["Content-Range"])

    def _wait_for_publish(self, token: str, publish_id: str) -> dict[str, Any]:
        """Poll status/fetch every POLL_INTERVAL_S until PUBLISH_COMPLETE; FAILED -> PublishFatal; timeout -> PublishError."""
        waited = 0
        while True:
            data = self._api_call(STATUS_URL, token, {"publish_id": publish_id}, "status/fetch")
            status = str(data.get("status") or "")
            if status == STATUS_COMPLETE:
                return data
            if status == STATUS_FAILED:
                raise PublishRejected(f"tiktok: publish {publish_id} failed: {data.get('fail_reason') or 'no fail_reason given'}")
            if waited >= POLL_TIMEOUT_S:
                raise PublishError(f"tiktok: publish {publish_id} still {status or 'pending'} after {POLL_TIMEOUT_S}s")
            if status not in STATUS_IN_PROGRESS:
                log.warning("tiktok: unknown publish status %r for %s; still waiting", status, publish_id)
            log.info("tiktok: publish %s status %s; next check in %ss", publish_id, status or "pending", POLL_INTERVAL_S)
            self.sleep_fn(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S

    # ---- HTTP ---------------------------------------------------------------------------------------------------------
    def _request(self, method: str, url: str, what: str, **kw: Any) -> requests.Response:
        """One HTTP call, retried up to RETRY_ATTEMPTS times on 5xx / connection errors with exponential backoff."""
        last = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if attempt > 1:
                self.sleep_fn(RETRY_BACKOFF_S * 2 ** (attempt - 2))
            try:
                resp = self._session.request(method, url, timeout=HTTP_TIMEOUT_S, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = f"{type(e).__name__}"
            else:
                if resp.status_code < 500:
                    return resp
                last = f"HTTP {resp.status_code}"
            log.warning("tiktok %s: %s (attempt %d/%d)", what, last, attempt, RETRY_ATTEMPTS)
        raise PublishError(f"tiktok {what}: {last} after {RETRY_ATTEMPTS} attempts - retry later")

    def _api_call(self, url: str, token: str, body: dict[str, Any], what: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": JSON_CONTENT_TYPE}
        return api_data(self._request("POST", url, what, headers=headers, json=body), what)

    def _token_request(self, form: dict[str, str], what: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST the form to TOKEN_URL, save and return the token record. Errors are fatal (bad code/secret/refresh)."""
        resp = self._request("POST", TOKEN_URL, what, headers=FORM_HEADERS, data=form)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400 or not isinstance(payload, dict) or payload.get("error") or not payload.get("access_token"):
            error = payload.get("error") if isinstance(payload, dict) else None
            description = payload.get("error_description", "") if isinstance(payload, dict) else ""
            raise PublishFatal(f"tiktok {what}: {error or f'HTTP {resp.status_code}'} {description}".rstrip())
        record = token_record(payload, self.now_fn(), previous)
        self._save_token(record)
        return record

    # ---- tokens ------------------------------------------------------------------------------------------------------
    @property
    def _token_path(self) -> Path:
        return self.settings.platform_path(self.cfg.token_file)

    def _has_app_credentials(self) -> bool:
        return bool(self.cfg.client_key and self.cfg.client_secret)

    def _load_token(self) -> dict[str, Any] | None:
        path = self._token_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("tiktok: unreadable token file %s (%s)", path, e)
            return None
        return data if isinstance(data, dict) else None

    def _save_token(self, record: dict[str, Any]) -> None:
        path = self._token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        log.info("tiktok: token saved to %s", path)

    def _usable_token(self) -> str | None:
        """Access token when stored and valid (refreshed if needed); None when there is nothing usable."""
        tok = self._load_token()
        if tok is None:
            return None
        now = self.now_fn()
        if token_valid(tok, now):
            return str(tok["access_token"])
        if token_refreshable(tok, now):
            return str(self._refresh(tok)["access_token"])
        return None

    def _access_token(self) -> str:
        token = self._usable_token()
        if token is None:
            raise AuthError("tiktok: not authenticated or token expired - run `clipforge auth tiktok`")
        return token

    def _refresh(self, tok: dict[str, Any]) -> dict[str, Any]:
        log.info("tiktok: refreshing access token")
        form = {
            "client_key": self.cfg.client_key,
            "client_secret": self.cfg.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": str(tok["refresh_token"]),
        }
        return self._token_request(form, "oauth/token (refresh)", previous=tok)

    def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        form = {
            "client_key": self.cfg.client_key,
            "client_secret": self.cfg.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.cfg.redirect_uri,
            "code_verifier": verifier,
        }
        return self._token_request(form, "oauth/token (authorization_code)")

    def _interactive_auth(self) -> bool:
        """Print the authorize URL, read the redirected URL back, exchange the code. False on a bad paste."""
        state = new_state()
        verifier, challenge = pkce_pair()
        url = build_auth_url(self.cfg.client_key, self.cfg.redirect_uri, state, challenge)
        console.print("Open this URL in a browser, log in to TikTok and authorize the app:")
        console.print(url, markup=False, highlight=False, soft_wrap=True)
        console.print("You will land on the redirect URI (the page itself may show an error - that is fine).")
        pasted = self.input_fn("Paste the full URL from the address bar: ")
        try:
            code = parse_redirect(pasted, state)
        except ValueError as e:
            console.print(f"[red]tiktok auth:[/] {escape(str(e))}")
            log.error("tiktok auth: %s", e)
            return False
        self._exchange_code(code, verifier)
        console.print(f"TikTok token saved to {escape(str(self._token_path))}")
        self.db.log("auth", "tiktok: authorized")
        return True
