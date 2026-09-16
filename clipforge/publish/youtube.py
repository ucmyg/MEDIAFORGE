"""YouTube Data API v3 publisher: OAuth desktop flow, resumable videos.insert, daily quota budget kept in SQLite.

Shorts are ordinary vertical videos of at most SHORTS_MAX_S seconds; there is no Shorts endpoint. One videos.insert
costs platforms.youtube.upload_cost units of the project's platforms.youtube.daily_quota (verify both at
UPLOAD_COST_DOC); the quota resets at midnight Pacific, which is the day Publisher.today_key() returns for youtube.
limits() derives today's remaining uploads from that budget and from per_day, and publish() refuses when it is 0.

Setup (free, once): in a Google Cloud project enable "YouTube Data API v3", configure the OAuth consent screen
(External, Testing, add your own account as a test user), create a *Desktop app* OAuth client and save its JSON as
platforms.youtube.client_secret (default <workspace>/client_secret.json). `clipforge auth youtube` then runs the
loopback flow once and stores the refreshable token at platforms.youtube.token_file. Until the project passes
Google's compliance audit every upload is forced private, whatever privacyStatus says (see base.AUDIT_WARNINGS).

Google libraries are imported lazily inside the methods that need them, so this module imports without them; tests
replace YouTubePublisher._load_credentials / _run_flow / _service with fakes and drive upload_video() with a fake
insert request.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import time
import webbrowser
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from rich.markup import escape

from ..config import Settings, YouTubeCfg
from ..db import DB, Clip
from ..log import console, get_logger
from ..metadata import ClipMeta
from .base import AuthError, Limits, PublishError, Publisher, PublishFatal

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_COST_DOC = "https://developers.google.com/youtube/v3/determine_quota_cost"  # videos.insert = 1600 units (2024)
VIDEOS_RESOURCE_DOC = "https://developers.google.com/youtube/v3/docs/videos"  # source of the field limits below
CLOUD_CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"
SHORTS_URL = "https://youtube.com/shorts/{video_id}"
NO_BROWSER_ENV = "CLIPFORGE_NO_BROWSER"  # =1 -> auth never tries to open a browser; the sign-in URL is printed instead
FLOW_TIMEOUT_S: float | None = 600  # how long the loopback server waits for the browser redirect before the sign-in fails
MIME_TYPE = "video/mp4"
CHUNK_SIZE = 8 * 1024 * 1024  # resumable upload chunk; the API requires a multiple of 256 KiB
TITLE_MAX = 100  # snippet.title (VIDEOS_RESOURCE_DOC)
DESCRIPTION_MAX = 5000  # snippet.description
TAGS_MAX_CHARS = 500  # snippet.tags: tag lengths plus one comma between tags; a tag with spaces counts its quotes too
FORBIDDEN_TEXT = str.maketrans("", "", "<>")  # title/description accept any UTF-8 except angle brackets
DEFAULT_TITLE = "Clip"
SHORTS_MAX_S = 180  # longer uploads are published as regular videos, not Shorts
BACKOFF_S = (2, 4, 8, 16, 32)  # seconds slept before retry 1..5 of a transient chunk failure; then give up
RETRY_STATUSES = frozenset({500, 502, 503, 504})  # HttpError statuses worth an in-process retry
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (OSError,)  # ConnectionError, TimeoutError, socket + requests errors
QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded")  # 4xx reasons (quotaExceeded is a 403, uploadLimitExceeded a 400) that mean "no more uploads today"
ERROR_DETAIL_CHARS = 300
SETUP_STEPS = (
    "1. https://console.cloud.google.com/ -> create (or pick) a project",
    "2. APIs & Services -> Library -> enable 'YouTube Data API v3'",
    "3. APIs & Services -> OAuth consent screen -> External -> publishing status Testing -> add your Google account as a test user",
    "4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID -> Desktop app -> Download JSON",
    "5. save that file as {path} (or point platforms.youtube.client_secret at it) and run `clipforge auth youtube`",
)

Sleep = Callable[[float], None]


class QuotaExhausted(PublishFatal):
    """Google refused the upload for today (quotaExceeded / uploadLimitExceeded); retry after the Pacific midnight reset."""


# ---- helpers (module level so tests can exercise them without credentials) ----------------------------------------


def to_rfc3339(value: str, local_tz: tzinfo | None = None) -> str:
    """Normalise a publish time to UTC RFC3339 ('YYYY-MM-DDTHH:MM:SSZ').

    Accepts 'YYYY-MM-DDTHH:MM[:SS]' (naive = local time, or `local_tz` when given) and any ISO-8601 string with an
    offset or 'Z'. Raises ValueError for anything else.
    """
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError) as err:
        raise ValueError(f"publish_at {value!r} is not a date-time like 2026-01-31T18:00 or 2026-01-31T18:00:00Z") from err
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz or datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def video_tags(meta: ClipMeta) -> list[str]:
    """Topic tags without '#', deduplicated, cut so the list fits YouTube's TAGS_MAX_CHARS (commas + quotes included)."""
    raw = meta.topics or meta.hashtags.get("youtube", [])
    tags: list[str] = []
    used = 0
    for tag in (t.strip().lstrip("#").strip() for t in raw):
        if not tag or tag in tags:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if tags else 0)
        if used + cost > TAGS_MAX_CHARS:
            break
        tags.append(tag)
        used += cost
    return tags


def open_browser_allowed() -> bool:
    """False when CLIPFORGE_NO_BROWSER=1 (headless machines: the sign-in URL is printed for copy/paste instead)."""
    return os.environ.get(NO_BROWSER_ENV, "") != "1"


def _free_loopback_port() -> int:
    """A currently free TCP port on localhost for the OAuth redirect server (bound briefly, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def setup_instructions(client_secret: Path) -> str:
    """README click-path for creating the Desktop OAuth client, with the expected file location filled in."""
    steps = "\n".join("  " + step.format(path=client_secret) for step in SETUP_STEPS)
    return f"youtube: OAuth client secret not found at {client_secret}\nCreate it once (free):\n{steps}"


def _http_status(err: BaseException) -> int | None:
    """HTTP status of a googleapiclient HttpError-like exception (anything with .resp.status); None otherwise."""
    status = getattr(getattr(err, "resp", None), "status", None)
    return status if isinstance(status, int) else None


def _error_detail(err: BaseException) -> str:
    """'reason: message' from a Google API JSON error body, else the raw text/exception, cut to ERROR_DETAIL_CHARS."""
    content = getattr(err, "content", None)
    if content is None:
        return f"{type(err).__name__}: {err}"[:ERROR_DETAIL_CHARS]
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    with contextlib.suppress(ValueError, AttributeError):
        error = json.loads(text).get("error", {})
        reasons = ",".join(e.get("reason", "") for e in error.get("errors", []) if e.get("reason"))
        text = f"{reasons}: {error.get('message', '')}".strip(": ") or text
    return text[:ERROR_DETAIL_CHARS]


def _is_transient(err: BaseException) -> bool:
    return _http_status(err) in RETRY_STATUSES or isinstance(err, TRANSIENT_EXCEPTIONS)


def _classify(err: BaseException) -> PublishError:
    """Map a non-transient upload exception onto QuotaExhausted / PublishFatal / PublishError (retry later)."""
    status = _http_status(err)
    detail = _error_detail(err)
    if status is not None and 400 <= status < 500 and any(reason in detail for reason in QUOTA_REASONS):
        return QuotaExhausted(f"youtube: no more uploads allowed today ({detail}); quota resets at midnight Pacific")
    if status in (401, 403):  # account-level (revoked token, no channel, missing scope), not a verdict on this clip
        return AuthError(f"youtube: HTTP {status} ({detail}); run `clipforge auth youtube` to re-authorise")
    if status is not None and 400 <= status < 500 and status != 429:
        return PublishFatal(f"youtube: HTTP {status} rejected the upload ({detail})")
    return PublishError(f"youtube: upload failed ({detail})")


def _retry_or_raise(err: Exception, failures: int, sleep: Sleep) -> int:
    """Sleep and return failures+1 when `err` is transient and retries remain; otherwise raise the classified error."""
    if not _is_transient(err):
        raise _classify(err) from err
    if failures >= len(BACKOFF_S):
        raise PublishError(f"youtube: upload failed after {failures} retries ({_error_detail(err)})") from err
    delay = BACKOFF_S[failures]
    log.warning("youtube: transient upload error (%s); retry %d/%d in %ss", _error_detail(err), failures + 1, len(BACKOFF_S), delay)
    sleep(delay)
    return failures + 1


def upload_video(service: Any, path: Path | str, body: dict[str, Any], chunksize: int = CHUNK_SIZE, sleep: Sleep = time.sleep) -> dict[str, Any]:
    """Resumable videos.insert; returns the video resource. Transient failures retry with BACKOFF_S, others raise."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(path), mimetype=MIME_TYPE, chunksize=chunksize, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response: dict[str, Any] | None = None
    failures = 0
    while response is None:
        try:
            status, response = request.next_chunk()
        except Exception as err:  # classified below: transient -> retry, anything else -> PublishError/PublishFatal
            failures = _retry_or_raise(err, failures, sleep)
            continue
        if status is not None:
            log.info("youtube: uploaded %d%%", int(status.progress() * 100))
    return response


def _video_id(response: dict[str, Any]) -> str:
    video_id = str(response.get("id") or "")
    if not video_id:
        raise PublishFatal(f"youtube: upload finished without a video id ({str(response)[:ERROR_DETAIL_CHARS]})")
    return video_id


# ---- publisher ----------------------------------------------------------------------------------------------------


class YouTubePublisher(Publisher):
    name = "youtube"

    flow_timeout_s: float | None = FLOW_TIMEOUT_S  # None waits forever (Ctrl-C on the CLI); the UI job keeps the bound
    on_auth_url: Callable[[str], None] | None = None  # receives the sign-in URL before the flow blocks (the UI shows it)

    def __init__(self, settings: Settings, db: DB, sleep: Sleep = time.sleep):
        super().__init__(settings, db)
        self.cfg: YouTubeCfg = settings.platforms.youtube
        self._sleep = sleep

    @property
    def token_path(self) -> Path:
        return self.settings.platform_path(self.cfg.token_file)

    @property
    def client_secret_path(self) -> Path:
        return self.settings.platform_path(self.cfg.client_secret)

    def is_configured(self) -> bool:
        """A stored token; the client secret alone still needs the interactive `clipforge auth youtube`."""
        return self.token_path.is_file()

    def credentials_ok(self) -> bool:
        return self._usable_credentials() is not None

    # ---- auth ----------------------------------------------------------
    def auth(self, interactive: bool = True) -> bool:
        """True when a valid (or refreshable) token exists; otherwise run the desktop OAuth flow when interactive."""
        if self._usable_credentials() is not None:
            log.info("youtube: credentials valid (%s)", self.token_path.name)
            return True
        if not interactive:
            log.warning("youtube: no usable credentials; run `clipforge auth youtube`")
            return False
        if not self.client_secret_path.is_file():
            console.print(setup_instructions(self.client_secret_path), markup=False, highlight=False, soft_wrap=True)
            log.warning("youtube: client secret missing at %s", self.client_secret_path)
            return False
        try:
            creds = self._run_flow()
        except Exception as err:  # oauthlib / socket / user-cancelled: report, never traceback
            console.print(f"[red]youtube: sign-in failed:[/] {escape(f'{type(err).__name__}: {err}')}", highlight=False)
            log.error("youtube: OAuth flow failed (%s)", type(err).__name__)
            return False
        self._save_credentials(creds)
        return bool(creds.valid)

    def _usable_credentials(self) -> Credentials | None:
        """Stored credentials, refreshed (and re-saved) when expired; None when absent or needing user interaction."""
        creds = self._load_credentials()
        if creds is None:
            return None
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token and self._refresh(creds):
            self._save_credentials(creds)
            return creds
        return None

    def _load_credentials(self) -> Credentials | None:
        """Credentials from the token file, or None when it is absent or unreadable (its content is never logged)."""
        path = self.token_path
        if not path.is_file():
            return None
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            from google.oauth2.credentials import Credentials

            return Credentials.from_authorized_user_info(info, SCOPES)
        except (OSError, ValueError) as err:
            log.warning("youtube: token file %s is unusable (%s); re-run `clipforge auth youtube`", path, type(err).__name__)
            return None

    def _refresh(self, creds: Credentials) -> bool:
        """Refresh an expired token in place; False (logged, token never printed) when Google refuses."""
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
        except Exception as err:  # RefreshError (revoked / expired grant) or a transport error -> back to the flow
            log.warning("youtube: token refresh failed (%s: %s)", type(err).__name__, err)
            return False
        log.info("youtube: access token refreshed")
        return True

    def _run_flow(self) -> Credentials:
        """Desktop OAuth flow on a loopback port; blocks until the browser redirects back or `flow_timeout_s` elapses
        (google_auth_oauthlib raises WSGITimeoutError then, which auth() reports as a failed sign-in).

        The redirect port is picked up front so the sign-in URL exists before anything can fail: it is printed, handed
        to `on_auth_url`, and only then is the browser opened (a machine without one just logs a warning). The state
        and PKCE verifier of that URL are pinned so the library's own second authorization_url() call reuses them.
        """
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secret_path), SCOPES)
        port = _free_loopback_port()
        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, state = flow.authorization_url(prompt="consent")
        flow.autogenerate_code_verifier = False  # keep the verifier behind auth_url's code_challenge
        console.print(
            "youtube: opening Google sign-in. If no browser opens, copy this URL into a browser on this machine "
            f"(the redirect goes to localhost; set {NO_BROWSER_ENV}=1 to skip the browser launch):\n{escape(auth_url)}",
            highlight=False,
            soft_wrap=True,
        )
        if self.on_auth_url is not None:
            self.on_auth_url(auth_url)
        if open_browser_allowed():
            try:
                if not webbrowser.open(auth_url, new=1, autoraise=True):
                    log.warning("youtube: no browser opened the sign-in URL; open it by hand")
            except Exception as err:  # webbrowser.Error (nothing to run) or a broken $BROWSER entry
                log.warning("youtube: could not open a browser (%s: %s); open the sign-in URL by hand", type(err).__name__, err)
        return flow.run_local_server(
            port=port, open_browser=False, authorization_prompt_message=None, prompt="consent", state=state, timeout_seconds=self.flow_timeout_s
        )

    def _save_credentials(self, creds: Credentials) -> None:
        path = self.token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(creds.to_json(), encoding="utf-8")
        with contextlib.suppress(OSError):  # best effort: owner-only on POSIX, no-op semantics on Windows
            path.chmod(0o600)
        log.info("youtube: token saved to %s", path)

    def _service(self) -> Any:
        """Authenticated Data API client (googleapiclient Resource). Non-interactive: raises AuthError without a token."""
        creds = self._usable_credentials()
        if creds is None:
            raise AuthError("youtube: no usable credentials; run `clipforge auth youtube`")
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    # ---- limits --------------------------------------------------------
    def limits(self) -> Limits:
        cfg = self.cfg
        used = self.db.budget_used(self.name, self.today_key())
        by_quota = max(0, cfg.daily_quota - used) // max(cfg.upload_cost, 1)
        posted = self.posted_today()
        by_uploads = max(0, cfg.uploads_per_day - posted)
        return Limits(
            per_day=cfg.per_day,
            posted_today=posted,
            remaining=max(0, min(cfg.per_day - posted, by_quota, by_uploads)),
            quota_used=used,
            quota_total=cfg.daily_quota,
            note=f"uploads {posted}/{cfg.uploads_per_day} today, quota {used}/{cfg.daily_quota} units",
        )

    # ---- publish -------------------------------------------------------
    def build_body(self, meta: ClipMeta) -> dict[str, Any]:
        """videos.insert body from metadata + config; publishAt only when privacy is private (YouTube ignores it otherwise)."""
        cfg = self.cfg
        status: dict[str, Any] = {"privacyStatus": cfg.privacy, "selfDeclaredMadeForKids": cfg.made_for_kids}
        if cfg.publish_at and cfg.privacy != "private":
            log.warning("youtube: publish_at=%r ignored because privacy=%r (scheduled publishing needs privacy=private)", cfg.publish_at, cfg.privacy)
        elif cfg.publish_at:
            try:
                status["publishAt"] = to_rfc3339(cfg.publish_at)
            except ValueError as err:
                raise PublishFatal(f"youtube: platforms.youtube.{err}") from err
        snippet = {
            "title": meta.title.translate(FORBIDDEN_TEXT).strip()[:TITLE_MAX] or DEFAULT_TITLE,
            "description": meta.description.translate(FORBIDDEN_TEXT)[:DESCRIPTION_MAX],
            "tags": video_tags(meta),
            "categoryId": cfg.category,
        }
        return {"snippet": snippet, "status": status}

    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        self.ensure_not_posted(clip)
        self.ensure_allowance()
        self.warn_once()
        body = self.build_body(meta)
        day = self.today_key()
        self.db.ensure_post(clip.id, self.name)
        try:
            path = self._clip_file(clip)
            log.info("youtube: uploading %s as %r (privacy=%s)", path.name, body["snippet"]["title"], body["status"]["privacyStatus"])
            video_id = _video_id(upload_video(self._service(), path, body, sleep=self._sleep))
        except PublishError as err:
            if isinstance(err, QuotaExhausted):
                self._mark_quota_exhausted(day)
            self._record_failure(clip, err)
            raise
        log.info("youtube: upload complete, video id %s (%s)", video_id, clip.id)  # survives any failure below (Ctrl-C, busy DB)
        return self._record_success(clip, day, video_id, body["status"]["privacyStatus"])

    def _clip_file(self, clip: Clip) -> Path:
        if not clip.path or not Path(clip.path).is_file():
            raise PublishFatal(f"youtube: clip {clip.id} has no rendered file ({clip.path!r}); run `clipforge run` first")
        if (clip.duration or 0) > SHORTS_MAX_S:
            log.warning("youtube: clip %s is %.0fs (> %ds): it will be a regular video, not a Short", clip.id, clip.duration, SHORTS_MAX_S)
        return Path(clip.path)

    def _mark_quota_exhausted(self, day: str) -> None:
        """Google says no more uploads today: raise the local budget to daily_quota so limits() reports 0 until reset."""
        missing = self.cfg.daily_quota - self.db.budget_used(self.name, day)
        if missing > 0:
            self.db.budget_add(self.name, day, missing)
        log.warning("youtube: daily quota marked exhausted for %s (resets at midnight Pacific)", day)

    def _record_failure(self, clip: Clip, err: PublishError) -> None:
        """Bump the post row's attempts with the error; final for PublishFatal except quota exhaustion (retry tomorrow).

        An AuthError is the account's problem, not the clip's: the row is left untouched (only logged).
        next_attempt_at is left to the scheduler, which owns the backoff policy.
        """
        if not isinstance(err, AuthError):
            final = isinstance(err, PublishFatal) and not isinstance(err, QuotaExhausted)
            self.db.mark_post_failed(clip.id, self.name, str(err), next_attempt_at=None, final=final)
        self.db.log("publish.youtube", f"{clip.id}: {err}", level="error")
        log.error("youtube: %s failed: %s", clip.id, err)

    def _record_success(self, clip: Clip, day: str, video_id: str, privacy: str) -> str:
        """mark_posted first: once the row is `posted` nothing can re-upload the video; the rest is best-effort bookkeeping."""
        try:
            self.db.mark_posted(clip.id, self.name, video_id)
        except Exception as err:  # e.g. sqlite3.OperationalError on a busy DB: final, so the scheduler never re-uploads
            raise PublishFatal(f"youtube: video {video_id} was uploaded but recording it failed ({type(err).__name__}: {err}); mark clip {clip.id} posted manually") from err
        url = SHORTS_URL.format(video_id=video_id)
        try:
            self.db.budget_add(self.name, day, self.cfg.upload_cost)
            self.db.log("publish.youtube", f"{clip.id} -> {video_id} ({privacy}) {url}")
        except Exception as err:  # the quota is at worst under-counted by one upload; quotaExceeded from the API still marks the day
            log.error("youtube: %s is posted as %s but the bookkeeping failed (%s: %s)", clip.id, video_id, type(err).__name__, err)
        log.info("youtube: posted %s -> %s (%s)", clip.id, video_id, privacy)
        console.print(f"[green]youtube:[/] {escape(url)} ({privacy})", highlight=False)
        return video_id
