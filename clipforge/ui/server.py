"""FastAPI app for the local web UI. `create_app(settings, db)` builds it; state lives on app.state.

Routes (JSON everywhere, errors as HTTP 4xx with {"detail": str}, timestamps ISO-8601 strings, ids strings):
  GET  /                                   the SPA (static/index.html); /static/* -> app.js, style.css
  GET  /api/state                          videos, clips (+meta, +posts), jobs, scheduler, settings in one poll
  POST /api/videos                         queue a URL / local file with per-video options
  POST /api/videos/{id}/run                background job: pipeline.process_video
  POST /api/run                            background job: pipeline.process_queue
  DELETE /api/videos/{id}                  remove a video, its clips, posts and workspace folder (409 when posted)
  POST /api/clips/{id}/status              ready | rejected (409 once posted)
  PUT  /api/clips/{id}/meta                edit title / description / hashtags, rewrites clips/<id>.json
  GET  /api/clips/{id}/caption             the manual publisher's caption + upload page for a platform
  POST /api/clips/{id}/posted              "I posted this by hand": record the post id
  POST /api/publish                        background job: API publishers for clip x platform pairs
  POST /api/auth/{platform}/start          youtube -> job running the OAuth flow; tiktok -> {auth_url, state}
  POST /api/auth/tiktok/complete           paste-back of the redirect URL -> token saved
  POST /api/scheduler                      start/stop the tick loop thread
  POST /api/tick                           {dry_run: true} -> the report inline; otherwise a background job: one pass now
  GET  /api/doctor  GET /api/logs  GET/PUT /api/settings (raw clipforge.yaml, validated through Settings)
  GET/HEAD /media/{video_id}/clips/{file}  the rendered mp4 (Range requests supported for seeking)

Long work (pipeline runs, publishing, ticks) goes through the JobRunner's work lane (jobs.py, one job at a time);
the YouTube sign-in runs on its own auth lane so a pending browser redirect never holds up a render. The UI polls
/api/state for job and clip progress. Pipeline jobs, the scheduler thread, tick jobs and video deletion share one
lock so only one ffmpeg pool exists at a time and nothing is deleted under a running pass.

No authentication: this is a local tool bound to 127.0.0.1 by default. What keeps other web pages in the same
browser out is the request guard installed by create_app: the Host header must name an allowed host (closes DNS
rebinding) and every POST/PUT/DELETE must carry `X-ClipForge: 1` (app.js sends it; curl/scripts must too) plus,
when a browser sent them, a same-site Origin / Sec-Fetch-Site (closes cross-site form and no-cors fetch posts).
"""
from __future__ import annotations

import logging

import uuid

import inspect
import mimetypes
import os
import shutil
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Literal, get_args, get_origin
from urllib.parse import parse_qs, urlsplit

import yaml
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__, config, download, pipeline
from .. import ffmpeg as F
from .. import publish as P
from .. import scheduler as S
from ..config import CONFIG_ENV, DEFAULT_CONFIG_FILE, DEFAULT_STYLES, Settings
from ..db import DB, Clip, Video
from ..log import get_logger, setup_logging
from ..metadata import MAX_TAGS, MIN_TAGS, TITLE_MAX, ClipMeta, write_meta
from ..publish import PLATFORMS
from ..publish.base import NotConfirmed, Publisher
from ..publish.manual import UPLOAD_PAGES, build_caption, generated_post_id
from ..publish.tiktok import SETUP_HELP as TIKTOK_SETUP_HELP
from ..publish.tiktok import TikTokPublisher, build_auth_url, new_state, parse_redirect, pkce_pair
from ..publish.youtube import DESCRIPTION_MAX, YouTubePublisher
from ..publish.youtube import setup_instructions as youtube_setup_instructions
from ..review import APPROVABLE
from . import background, serialize
from .jobs import ACTIVE, AUTH_LANE, WORK_LANE, Job, JobFailed, JobRunner

log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG_FILE_NAME = "clipforge.log"
LOG_TAIL_MAX = 2000
AUTH_PENDING_TTL_S = 15 * 60  # a TikTok sign-in started in the UI must be completed within this window
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})  # Host names always accepted by the request guard
ANY_HOST = "*"  # allowed_hosts entry that disables the Host check (the CLI passes it for --host 0.0.0.0 / ::)
CSRF_HEADER = "X-ClipForge"  # required on every POST/PUT/PATCH/DELETE; a cross-site page cannot add it without CORS
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PAGE_HEADERS = {"Cache-Control": "no-cache", "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}
CLIP_DECISIONS = ("ready", "rejected")
POSTABLE_STATUSES = ("rendered", "ready", "posted")  # clips that may be marked posted by hand
QUEUE_TARGET = "*"  # job target meaning "every queued video"
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".json": "application/json", ".ass": "text/plain; charset=utf-8"}
YAML_HEADER = "# ClipForge configuration. Every key is optional; env vars CLIPFORGE_<SECTION>__<KEY> override.\n"


# ---- request bodies -----------------------------------------------------------------------------------------------------
class VideoOptions(BaseModel):
    count: int | None = Field(None, ge=1)
    min_s: float | None = Field(None, gt=0)
    max_s: float | None = Field(None, gt=0)
    style: str | None = None
    layout: Literal["crop", "blur"] | None = None
    tighten: bool | None = None
    smart: bool | None = None
    punch: bool | None = None
    force_whisper: bool | None = None
    music: bool | None = None


class AddVideoBody(BaseModel):
    source: str = ""
    options: VideoOptions = Field(default_factory=VideoOptions)


class RunBody(BaseModel):
    force: bool = False


class ClipStatusBody(BaseModel):
    status: str


class MetaBody(BaseModel):
    title: str
    description: str = ""
    hashtags: dict[str, list[str]] = Field(default_factory=dict)


class PostedBody(BaseModel):
    platform: str
    post_id: str = ""  # empty -> a generated 'manual:<timestamp>' id, like answering 'y' on the CLI


class PublishBody(BaseModel):
    clip_ids: list[str]
    platforms: list[str]


class TikTokCompleteBody(BaseModel):
    redirect_url: str
    state: str | None = None  # optional; taken from the URL's query when absent


class SchedulerBody(BaseModel):
    running: bool


class TickBody(BaseModel):
    dry_run: bool = False


class SettingsBody(BaseModel):
    yaml: str


# ---- app factory ----------------------------------------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    app.state.scheduler.stop(timeout=5.0)
    app.state.jobs.stop(timeout=1.0)


def create_app(settings: Settings, db: DB, *, static_dir: Path | None = None, allowed_hosts: Iterable[str] | None = None) -> FastAPI:
    """The UI app. `static_dir` overrides clipforge/ui/static (tests); `allowed_hosts` adds Host names to the loopback
    ones the request guard accepts (ANY_HOST switches the Host check off, for a server deliberately bound to a LAN)."""
    app = FastAPI(title="ClipForge UI", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    st = app.state
    st.settings = settings
    st.db = db
    st.jobs = JobRunner()
    st.work_lock = threading.Lock()  # pipeline jobs, scheduler ticks and /api/tick: one ffmpeg pool at a time
    st.scheduler = background.SchedulerLoop(lambda: (app.state.settings, app.state.db), st.work_lock)
    st.tiktok_pending: dict[str, tuple[str, float]] = {}  # state -> (code_verifier, monotonic time)
    st.static_dir = Path(static_dir) if static_dir is not None else STATIC_DIR
    _register_handlers(app)
    _register_routes(app)
    app.mount("/static", _RevalidatedStaticFiles(directory=str(st.static_dir), check_dir=False), name="static")
    app.add_middleware(_RequestGuard, allowed_hosts=set(LOOPBACK_HOSTS) | {h.lower() for h in (allowed_hosts or ())})
    app.add_middleware(_RequestLog)  # outermost: times the guard too
    return app


# ---- request guard + static files ------------------------------------------------------------------------------------------
def _hostname(value: str | None) -> str:
    """Lower-cased host name of a Host header or Origin URL ('[::1]:8765' -> '::1', 'http://Localhost:8765' -> 'localhost')."""
    if not value:
        return ""
    try:
        return (urlsplit(value if "//" in value else "//" + value).hostname or "").lower()
    except ValueError:
        return ""


class _RequestLog:
    """Pure-ASGI access log: request id, method, route, status, duration. No query strings, bodies or headers are
    logged (the TikTok redirect URL and settings YAML travel there). Polling and media routes log at DEBUG so the
    2-second /api/state poll does not flood logs/clipforge.log; everything else at INFO; 5xx at ERROR."""

    QUIET = ("/api/state", "/media/", "/static/", "/api/health")

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        status = {"code": 0}

        async def send_wrapped(message: Any) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 0))
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        path = scope.get("path", "")
        try:
            await self.app(scope, receive, send_wrapped)
        except Exception:
            log.exception("rid=%s %s %s -> unhandled error after %.0f ms", rid, scope.get("method"), path, (time.perf_counter() - started) * 1000)
            raise
        ms = (time.perf_counter() - started) * 1000
        level = logging.ERROR if status["code"] >= 500 else logging.DEBUG if path.startswith(self.QUIET) else logging.INFO
        log.log(level, "rid=%s %s %s -> %d in %.0f ms", rid, scope.get("method"), path, status["code"], ms)


class _RequestGuard:
    """Pure-ASGI guard in front of every route (see the module docstring): 403 {"detail": why} when a request fails.

    Host allow-list on every request (DNS rebinding would otherwise make an attacker's page same-origin with the API);
    on non-safe methods additionally a same-site Origin / Sec-Fetch-Site when a browser sent them and the CSRF_HEADER,
    which forces a CORS preflight (answered 405, no ACAO) on anything cross-site.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]):
        self.app = app
        self.allowed = {h.lower() for h in allowed_hosts}
        self.any_host = ANY_HOST in self.allowed

    def reject_reason(self, method: str, headers: Headers) -> str | None:
        host = _hostname(headers.get("host"))
        if not self.any_host and host not in self.allowed:
            return f"host {headers.get('host', '')!r} is not allowed: open the UI through one of {', '.join(sorted(self.allowed))} (or pass --host)"
        if method in SAFE_METHODS:
            return None
        origin = headers.get("origin")
        if origin is not None:
            origin_host = _hostname(origin)
            if origin == "null" or not origin_host or (origin_host != host and origin_host not in self.allowed):
                return f"cross-site request from origin {origin!r} refused"
        site = headers.get("sec-fetch-site")
        if site and site not in ("same-origin", "none"):
            return f"cross-site request ({site}) refused"
        if headers.get(CSRF_HEADER.lower()) != "1":
            return f"missing header {CSRF_HEADER}: 1 (required on {method}; the UI sends it, scripts must too)"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            reason = self.reject_reason(str(scope.get("method", "GET")).upper(), Headers(scope=scope))
            if reason is not None:
                await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _RevalidatedStaticFiles(StaticFiles):
    """app.js/style.css are referenced without a version suffix: make browsers revalidate (ETag -> 304) on every load
    instead of applying heuristic freshness, so an upgraded server never runs against a cached old frontend."""

    def file_response(self, full_path: Any, stat_result: Any, scope: Scope, status_code: int = 200) -> Any:
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result, headers={"Cache-Control": "no-cache"})
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


# ---- helpers ---------------------------------------------------------------------------------------------------------------
def _publisher(name: str, settings: Settings, db: DB) -> Publisher:
    """API publisher for `name` (looked up on the publish package at call time so tests can patch get_publisher)."""
    return P.get_publisher(name, settings, db)


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception as err:
        log.debug("%s failed: %s: %s", getattr(fn, "__qualname__", fn), type(err).__name__, err)
        return default


def _configured(settings: Settings, db: DB) -> dict[str, bool]:
    """Publisher.is_configured() per platform (no network); a publisher that cannot even be built counts as not configured."""
    out: dict[str, bool] = {}
    for name in PLATFORMS:
        pub = _safe(lambda: _publisher(name, settings, db))
        out[name] = bool(_safe(pub.is_configured, False)) if pub is not None else False
    return out


def _auth_noninteractive(publisher: Publisher) -> bool:
    """Credentials usable right now, without ever blocking on stdin or a browser.

    YouTubePublisher.auth(interactive=False) refreshes a stored token and answers False otherwise; other publishers
    (TikTok's auth() would prompt for a pasted URL) are probed with is_configured() + credentials_ok().
    """
    try:
        params = inspect.signature(publisher.auth).parameters
    except (TypeError, ValueError):
        params = {}
    if "interactive" in params:
        return bool(publisher.auth(interactive=False))
    return bool(publisher.is_configured() and publisher.credentials_ok())


def _config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE).expanduser().absolute()


def _settings_yaml(settings: Settings) -> str:
    """The live settings as a commented clipforge.yaml (what the editor shows when no file exists yet)."""
    return YAML_HEADER + yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


def _tail(path: Path, limit: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError as err:
        return [f"(could not read {path}: {err})"]


def _video_or_404(db: DB, video_id: str) -> Video:
    video = db.get_video(video_id)
    if video is None:
        raise HTTPException(404, f"unknown video {video_id!r}")
    return video


def _clip_or_404(db: DB, clip_id: str) -> Clip:
    clip = db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, f"unknown clip {clip_id!r}")
    return clip


def _platform_or_400(name: str) -> str:
    platform = (name or "").strip().lower()
    if platform not in PLATFORMS:
        raise HTTPException(400, f"unknown platform {name!r}; choose from {', '.join(PLATFORMS)}")
    return platform


def _latest_posts(db: DB) -> dict[tuple[str, str], Any]:
    """Latest post row per (clip, platform) in one query (list_posts is ordered by id, so the last write wins)."""
    return {(post.clip_id, post.platform): post for post in db.list_posts()}


def _clip_payload(db: DB, clip: Clip) -> dict[str, Any]:
    return serialize.clip_dict(clip, {p: db.get_post(clip.id, p) for p in PLATFORMS}, serialize.load_meta(clip))


def _report_dict(report: pipeline.ProcessReport) -> dict[str, Any]:
    return {
        "video_id": report.video_id,
        "status": report.status,
        "error": report.error,
        "clips": len(report.clips),
        "rendered": sum(c.status in pipeline.DONE_STATUSES for c in report.clips),
    }


def _unknown_keys(data: dict, model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths in `data` that no field of `model` accepts. Settings and the nested *Cfg models ignore extras
    silently (so env vars / CLI stay lenient); the UI editor must not, or a typo saves fine and changes nothing."""
    bad: list[str] = []
    for key, value in data.items():
        field = model.model_fields.get(str(key))
        if field is None:
            bad.append(f"{prefix}{key}")
            continue
        if not isinstance(value, dict):
            continue
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            bad += _unknown_keys(value, ann, f"{prefix}{key}.")
        elif get_origin(ann) is dict:  # e.g. styles: dict[str, StyleCfg]
            args = get_args(ann)
            item_model = args[1] if len(args) == 2 else None
            if isinstance(item_model, type) and issubclass(item_model, BaseModel):
                for name, item in value.items():
                    if isinstance(item, dict):
                        bad += _unknown_keys(item, item_model, f"{prefix}{key}.{name}.")
    return bad


def _tick_dict(result: S.TickResult) -> dict[str, Any]:
    return {
        "summary": result.summary(),
        "processed": list(result.processed),
        "posted": [{"clip_id": c, "platform": p, "post_id": i} for c, p, i in result.posted],
        "skipped": list(result.skipped),
        "errors": list(result.errors),
        "dry_run": result.dry_run,
    }


def _busy_detail(st: Any) -> str:
    current = st.jobs.current(lane=WORK_LANE)
    return current.detail if current else "the scheduler is ticking"


def _validation_text(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid")))
    return "invalid request: " + "; ".join(parts)


def _expire_pending(pending: dict[str, tuple[str, float]]) -> None:
    now = time.monotonic()
    for state in [s for s, (_, started) in pending.items() if now - started > AUTH_PENDING_TTL_S]:
        del pending[state]


# ---- /api/state blocks -------------------------------------------------------------------------------------------------------
def _scheduler_block(st: Any) -> dict[str, Any]:
    settings, db, loop = st.settings, st.db, st.scheduler
    sched = settings.schedule
    now = datetime.now().astimezone()
    times = _safe(lambda: S.parse_times(sched.times), [])
    configured: dict[str, bool] = {}
    limits: dict[str, Any] = {}
    next_slot: dict[str, str | None] = {}
    due: dict[str, bool] = {}  # next_slot[name] has opened and nothing was posted since: a post is due right now
    for name in PLATFORMS:
        pub = _safe(lambda: _publisher(name, settings, db))
        configured[name] = bool(_safe(pub.is_configured, False)) if pub is not None else False
        limits[name] = serialize.limits_dict(_safe(pub.limits)) if pub is not None else None
        last_iso = _safe(lambda: db.last_posted_at(name))
        last = background.parse_db_ts(last_iso, now.tzinfo) if last_iso else None
        next_slot[name] = background.next_slot_hm(times, now, last)
        due[name] = S.due_slot(now, times, last) is not None
    return {
        "running": loop.running,
        "stopping": loop.stopping,
        "tick_s": sched.tick_s,
        "times": list(sched.times),
        "min_gap_h": sched.min_gap_h,
        "next_slot": next_slot,
        "due": due,
        "configured": configured,
        "limits": limits,
        "last_tick": loop.last_tick,
        "last_summary": loop.last_summary,
    }


TIKTOK_POSTING_KEYS = ("privacy", "allow_comments", "allow_duet", "allow_stitch", "commercial_content", "brand_organic", "branded_content", "music_usage_confirmed")


def _settings_block(settings: Settings) -> dict[str, Any]:
    return {
        "style": settings.style,
        "layout": settings.layout,
        "tighten": settings.tighten,
        "smart": settings.smart,
        "punch": settings.punch,
        "clips": {"count": settings.clips.count, "min_s": settings.clips.min_s, "max_s": settings.clips.max_s},
        "styles": sorted({**DEFAULT_STYLES, **settings.styles}),
        "tiktok": settings.platforms.tiktok.model_dump(include=set(TIKTOK_POSTING_KEYS)),
        "workspace": str(settings.workspace_dir.absolute()),
        "config_path": str(_config_path()),
    }


STATE_CLIPS_DEFAULT = 500  # clips per /api/state unless ?video= narrows it or ?clips_limit= changes it (0 = all)
STATE_VIDEOS_DEFAULT = 500


def _state_payload(st: Any, *, video: str | None = None, clips_limit: int = STATE_CLIPS_DEFAULT, videos_limit: int = STATE_VIDEOS_DEFAULT) -> dict[str, Any]:
    """Everything the UI polls. Bounded: the newest `clips_limit` clips (or one video's clips with `video`) and the
    newest `videos_limit` videos; `totals`/`truncated` say what was left out so the page can offer a filter."""
    settings, db = st.settings, st.db
    total_clips = db.count_clips()
    if video:
        clips = db.list_clips(video_id=video)
        clips_truncated = False
    elif clips_limit and total_clips > clips_limit:
        clips = db.list_clips_recent(clips_limit)
        clips_truncated = True
    else:
        clips = db.list_clips()
        clips_truncated = False
    posts = _latest_posts(db)
    all_videos = db.list_videos()
    counts = Counter(c.video_id for c in (clips if video is None and not clips_truncated else db.list_clips()))
    videos = all_videos
    videos_truncated = bool(videos_limit) and len(all_videos) > videos_limit
    if videos_truncated:
        videos = sorted(all_videos, key=lambda v: (v.created_at, v.id), reverse=True)[:videos_limit]
        videos = sorted(videos, key=lambda v: (v.created_at, v.id))
    return {
        "videos": [serialize.video_dict(v, counts.get(v.id, 0)) for v in videos],
        "clips": [serialize.clip_dict(c, {p: posts.get((c.id, p)) for p in PLATFORMS}, serialize.load_meta(c)) for c in clips],
        "jobs": [j.to_dict() for j in st.jobs.list()],
        "scheduler": _scheduler_block(st),
        "settings": _settings_block(settings),
        "totals": {"videos": len(all_videos), "clips": total_clips},
        "truncated": {"videos": videos_truncated, "clips": clips_truncated},
    }


# ---- job functions -------------------------------------------------------------------------------------------------------------
def _run_video_fn(app: FastAPI, video_id: str, force: bool) -> Callable[[Job], None]:
    def run(job: Job) -> None:
        st = app.state
        job.progress = f"processing {video_id}"
        with st.work_lock:
            report = pipeline.process_video(video_id, st.settings, st.db, force=force)
        job.result = _report_dict(report)
        job.progress = f"{video_id}: {report.status}"
        if report.status == "failed":
            raise JobFailed(report.error or f"{video_id} failed")

    return run


def _run_queue_fn(app: FastAPI) -> Callable[[Job], None]:
    def run(job: Job) -> None:
        st = app.state
        job.progress = "processing queue"
        with st.work_lock:
            reports = pipeline.process_queue(st.settings, st.db)
        job.result = [_report_dict(r) for r in reports]
        job.progress = f"queue: {len(reports)} video(s) processed" if reports else "nothing to do: no queued videos"
        if reports and all(r.status == "failed" for r in reports):
            raise JobFailed("; ".join(f"{r.video_id}: {r.error}" for r in reports if r.error) or "every video failed")

    return run


def _publish_fn(app: FastAPI, clip_ids: list[str], platforms: list[str]) -> Callable[[Job], None]:
    def outcome(clip_id: str, platform: str, state: str, detail: str) -> dict[str, str | None]:
        url = serialize.post_url(platform, detail) if state in ("posted", "already posted") else None
        return {"clip_id": clip_id, "platform": platform, "state": state, "detail": detail, "url": url}

    def run(job: Job) -> None:
        st = app.state
        settings, db = st.settings, st.db
        outcomes: list[dict[str, str]] = []
        job.result = outcomes  # appended as pairs finish; /api/state shows partial results
        publishers: dict[str, Publisher] = {}
        for name in platforms:
            job.progress = f"checking {name} credentials"
            try:
                publisher = _publisher(name, settings, db)
                ok = _auth_noninteractive(publisher)
                reason = f"no usable credentials; run `clipforge auth {name}` (or the Sign in button)"
            except Exception as err:
                ok, reason = False, f"{type(err).__name__}: {err}"
            if ok:
                publishers[name] = publisher
            else:
                outcomes.extend(outcome(cid, name, "not configured", reason) for cid in clip_ids)
        active = list(publishers)
        for clip_id in clip_ids:
            for name, publisher in publishers.items():
                clip = db.get_clip(clip_id)  # fresh each time: a rejection since the job was queued must win
                if clip is not None and clip.status not in S.PUBLISHABLE:
                    outcomes.append(outcome(clip_id, name, "error", f"clip is {clip.status}; approve it first"))
                    continue
                if clip is None:
                    outcomes.append(outcome(clip_id, name, "error", "clip no longer exists"))
                    continue
                job.progress = f"publishing {clip_id} -> {name}"
                post = db.get_post(clip_id, name)
                if post is not None and post.status == "posted":
                    outcomes.append(outcome(clip_id, name, "already posted", post.post_id or ""))
                    continue
                try:
                    post_id = S.publish_clip(settings, db, publisher, clip, active, source="publish")
                except NotConfirmed:
                    outcomes.append(outcome(clip_id, name, "error", "not confirmed; left ready"))
                except S.InProgress as err:
                    outcomes.append(outcome(clip_id, name, "error", str(err)))
                except Exception as err:  # publish_clip recorded the attempt/backoff already
                    outcomes.append(outcome(clip_id, name, "error", f"{type(err).__name__}: {err}"))
                else:
                    outcomes.append(outcome(clip_id, name, "posted", post_id))
        posted = sum(o["state"] == "posted" for o in outcomes)
        errors = [o for o in outcomes if o["state"] in ("error", "not configured")]
        job.progress = f"{posted} posted, {len(errors)} not posted, {len(outcomes) - posted - len(errors)} already posted"
        if outcomes and not posted and errors:
            raise JobFailed(f"nothing posted: {errors[0]['platform']} {errors[0]['clip_id']}: {errors[0]['detail']}")

    return run


def _tick_fn(app: FastAPI) -> Callable[[Job], None]:
    def run(job: Job) -> None:
        st = app.state
        job.progress = "waiting for the pipeline lock"
        with st.work_lock:
            job.progress = "ticking: processing the queue, then posting what is due"
            result = S.tick(st.settings, st.db)
        job.result = _tick_dict(result)
        job.progress = result.summary()

    return run


def _youtube_auth_fn(app: FastAPI) -> Callable[[Job], None]:
    def run(job: Job) -> None:
        st = app.state
        job.progress = "waiting for the Google sign-in in your browser (the URL is on the server console if none opened)"
        publisher = _publisher("youtube", st.settings, st.db)
        if isinstance(publisher, YouTubePublisher):

            def show_url(url: str) -> None:
                job.result = {"auth_url": url}
                job.progress = f"waiting for the Google sign-in in your browser; if none opened, open this URL on the server machine: {url}"

            publisher.on_auth_url = show_url
        ok = bool(publisher.auth())
        job.result = {**(job.result if isinstance(job.result, dict) else {}), "ok": ok}
        if not ok:
            raise JobFailed("youtube sign-in failed, timed out or was cancelled; see the server console")
        st.db.log("auth", "youtube: authorized (ui)")
        job.progress = "signed in"

    return run


# ---- error handlers -----------------------------------------------------------------------------------------------------------
def _register_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_text(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("%s %s failed: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


# ---- routes -------------------------------------------------------------------------------------------------------------------------
def _register_routes(app: FastAPI) -> None:  # noqa: C901 - one closure per route, kept together on purpose
    st = app.state

    # ---- pages -----------------------------------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> Any:
        page = st.static_dir / "index.html"
        if not page.is_file():
            return PlainTextResponse(
                f"ClipForge UI page not found: {page}\nThe frontend files (index.html, app.js, style.css) ship in clipforge/ui/static; "
                "reinstall with `pip install -e .` or restore that folder. The JSON API under /api works regardless.",
                status_code=404,
            )
        return FileResponse(page, media_type="text/html", headers=PAGE_HEADERS)

    # ---- state -------------------------------------------------------------------------------------------------------------
    @app.get("/api/state")
    def api_state(
        video: str | None = None,
        clips_limit: int = Query(STATE_CLIPS_DEFAULT, ge=0, le=20000),
        videos_limit: int = Query(STATE_VIDEOS_DEFAULT, ge=0, le=20000),
    ) -> dict[str, Any]:
        return _state_payload(st, video=video, clips_limit=clips_limit, videos_limit=videos_limit)

    # ---- videos ------------------------------------------------------------------------------------------------------------
    @app.post("/api/videos")
    def add_video(body: AddVideoBody) -> dict[str, Any]:
        settings, db = st.settings, st.db
        source = body.source.strip()
        if not source:
            raise HTTPException(400, "source is empty: paste a YouTube URL or the path of a local media file")
        opts = body.options
        if opts.style is not None:
            try:
                settings.style_cfg(opts.style)
            except KeyError as err:
                raise HTTPException(400, str(err.args[0])) from None
        if opts.min_s is not None and opts.max_s is not None and opts.min_s >= opts.max_s:
            raise HTTPException(400, f"min_s ({opts.min_s:g}) must be smaller than max_s ({opts.max_s:g})")
        if download.is_url(source):
            stored = source
        else:
            local = Path(source).expanduser()
            if not local.is_file():
                raise HTTPException(400, f"file not found: {source}")
            stored = str(local.resolve())
        video_id, kind = download.video_id_for(stored)
        options = {k: v for k, v in opts.model_dump().items() if v is not None}
        video, created = db.add_video(video_id, kind, stored, options=options)
        if created:
            db.log("ui.add", f"{video_id}: {stored}" + (f" options={options}" if options else ""))
        return {"video_id": video_id, "created": created, "status": video.status}

    @app.post("/api/videos/{video_id}/run")
    def run_video(video_id: str, body: RunBody | None = None) -> dict[str, str]:
        _video_or_404(st.db, video_id)
        force = bool(body and body.force)
        job = st.jobs.submit("run", _run_video_fn(app, video_id, force), f"run {video_id}" + (" (force)" if force else ""), target=video_id)
        return {"job_id": job.id}

    @app.post("/api/run")
    def run_queue() -> dict[str, str]:
        job = st.jobs.submit("run", _run_queue_fn(app), "run queue", target=QUEUE_TARGET)
        return {"job_id": job.id}

    @app.delete("/api/videos/{video_id}")
    def delete_video(video_id: str) -> dict[str, bool]:
        settings, db = st.settings, st.db
        _video_or_404(db, video_id)
        clips = db.list_clips(video_id)
        for clip in clips:
            posts = [p for p in (db.get_post(clip.id, name) for name in PLATFORMS) if p is not None]
            if clip.status == "posted" or any(p.status == "posted" for p in posts):
                raise HTTPException(409, f"video {video_id} has posted clips; it cannot be deleted")
            live = next((p for p in posts if S.claim_is_live(p)), None)  # a publish job or another clipforge process mid-upload
            if live is not None:
                raise HTTPException(409, f"clip {clip.id} is being uploaded to {live.platform} right now; wait for it to finish")
        active = st.jobs.active_targets()
        if video_id in active or QUEUE_TARGET in active:
            raise HTTPException(409, f"video {video_id} is being processed or published; wait for the job to finish")
        if not st.work_lock.acquire(blocking=False):  # a scheduler tick / run job would keep writing into the folder
            raise HTTPException(409, f"busy: {_busy_detail(st)}; try again when it is done")
        try:
            with db.connect() as c:
                c.execute("DELETE FROM posts WHERE clip_id IN (SELECT id FROM clips WHERE video_id=?)", (video_id,))
                c.execute("DELETE FROM clips WHERE video_id=?", (video_id,))
                c.execute("DELETE FROM videos WHERE id=?", (video_id,))
            folder = settings.workspace_dir / video_id
            if folder.is_dir() and folder.resolve().parent == settings.workspace_dir.resolve():
                shutil.rmtree(folder, ignore_errors=True)
        finally:
            st.work_lock.release()
        db.log("ui.delete", f"{video_id}: removed with {len(clips)} clip(s)")
        return {"deleted": True}

    # ---- clips -------------------------------------------------------------------------------------------------------------
    @app.post("/api/clips/{clip_id}/status")
    def set_clip_status(clip_id: str, body: ClipStatusBody) -> dict[str, Any]:
        db = st.db
        clip = _clip_or_404(db, clip_id)
        status = body.status.strip().lower()
        if status not in CLIP_DECISIONS:
            raise HTTPException(400, f"status must be one of {', '.join(CLIP_DECISIONS)}")
        if clip.status == "posted":
            raise HTTPException(409, f"clip {clip_id} is posted; its status cannot change")
        if status == "ready" and clip.status not in APPROVABLE:
            raise HTTPException(409, f"clip {clip_id} is {clip.status}, not rendered; it cannot be approved")
        if clip.status != status:
            db.set_clip_status(clip_id, status)
            db.log("review.ui", f"{clip_id}: {clip.status} -> {status}")
        return _clip_payload(db, _clip_or_404(db, clip_id))

    @app.put("/api/clips/{clip_id}/meta")
    def put_clip_meta(clip_id: str, body: MetaBody) -> dict[str, Any]:
        settings, db = st.settings, st.db
        clip = _clip_or_404(db, clip_id)
        serialize.forget_meta(clip.meta_path)  # the rewrite below must never be served from the poll cache
        title = " ".join(body.title.split())
        if not title:
            raise HTTPException(400, "title must not be empty")
        if len(title) > TITLE_MAX:
            raise HTTPException(400, f"title is {len(title)} characters; at most {TITLE_MAX}")
        description = body.description.strip()
        if len(description) > DESCRIPTION_MAX:
            raise HTTPException(400, f"description is {len(description)} characters; at most {DESCRIPTION_MAX}")
        hashtags: dict[str, list[str]] = {}
        for platform in PLATFORMS:
            tags = [str(t).strip() for t in body.hashtags.get(platform, [])]
            tags = [t for t in tags if t]
            bad = [t for t in tags if not t.startswith("#") or len(t) < 2 or any(ch.isspace() for ch in t)]
            if bad:
                raise HTTPException(400, f"{platform}: hashtags must start with '#' and contain no spaces: {', '.join(bad)}")
            if not MIN_TAGS <= len(tags) <= MAX_TAGS:
                raise HTTPException(400, f"{platform}: {MIN_TAGS} to {MAX_TAGS} hashtags needed, got {len(tags)}")
            hashtags[platform] = tags
        meta = serialize.load_meta(clip) or ClipMeta(title=title, description="", hook=clip.hook, clip_id=clip.id, video_id=clip.video_id, start=clip.start, end=clip.end)
        meta.title, meta.description, meta.hashtags = title, description, hashtags
        path = Path(clip.meta_path) if clip.meta_path else pipeline.clips_dir(settings, clip.video_id) / f"{clip.id}.json"
        write_meta(path, meta)
        if not clip.meta_path:
            db.update_clip(clip.id, meta_path=str(path))
        db.log("meta.edit", f"{clip.id}: {title!r}")
        return serialize.meta_dict(meta) or {}

    @app.get("/api/clips/{clip_id}/caption")
    def clip_caption(clip_id: str, platform: str = "youtube") -> dict[str, Any]:
        name = _platform_or_400(platform)
        clip = _clip_or_404(st.db, clip_id)
        meta = serialize.load_meta(clip)
        if meta is None:
            raise HTTPException(404, f"clip {clip_id} has no metadata yet; run the pipeline first")
        return {"caption": build_caption(name, meta), "upload_url": UPLOAD_PAGES[name][0], "title": meta.title, "description": meta.description, "path": clip.path}

    @app.post("/api/clips/{clip_id}/posted")
    def mark_posted(clip_id: str, body: PostedBody) -> dict[str, Any]:
        settings, db = st.settings, st.db
        clip = _clip_or_404(db, clip_id)
        platform = _platform_or_400(body.platform)
        if db.is_posted(clip_id, platform):
            raise HTTPException(409, f"clip {clip_id} is already posted on {platform}")
        if clip.status not in POSTABLE_STATUSES:
            raise HTTPException(409, f"clip {clip_id} is {clip.status}; only rendered or ready clips can be marked posted")
        post_id = body.post_id.strip() or generated_post_id("manual")
        configured = _configured(settings, db)
        active = [p for p in PLATFORMS if configured.get(p) or p == platform]
        db.log("publish.manual", f"{platform} {clip_id} -> {post_id} (marked as posted in the UI)")
        S._record_success(db, clip, platform, post_id, active, source="publish")
        result = serialize.post_dict(db.get_post(clip_id, platform)) or {}
        result["clip_status"] = _clip_or_404(db, clip_id).status
        return result

    # ---- publishing ----------------------------------------------------------------------------------------------------------
    @app.post("/api/publish")
    def publish(body: PublishBody) -> dict[str, str]:
        db = st.db
        platforms = list(dict.fromkeys(_platform_or_400(p) for p in body.platforms))
        if not platforms:
            raise HTTPException(400, "platforms is empty: choose youtube and/or tiktok")
        clip_ids = list(dict.fromkeys(body.clip_ids))
        if not clip_ids:
            raise HTTPException(400, "clip_ids is empty: select at least one clip")
        video_ids: set[str] = set()
        for clip_id in clip_ids:
            clip = _clip_or_404(db, clip_id)
            if clip.status not in POSTABLE_STATUSES:
                raise HTTPException(409, f"clip {clip_id} is {clip.status}, not ready; approve it first")
            video_ids.add(clip.video_id)
        detail = f"publish {len(clip_ids)} clip(s) to {', '.join(platforms)}"
        job = st.jobs.submit("publish", _publish_fn(app, clip_ids, platforms), detail, target=video_ids)  # blocks DELETE of those videos
        return {"job_id": job.id}

    @app.post("/api/auth/{platform}/start")
    def auth_start(platform: str) -> dict[str, Any]:
        settings, db = st.settings, st.db
        name = _platform_or_400(platform)
        if name == "youtube":
            publisher = _publisher("youtube", settings, db)
            secret = settings.platform_path(settings.platforms.youtube.client_secret)
            if not publisher.is_configured() and not secret.is_file():
                raise HTTPException(400, youtube_setup_instructions(secret))
            pending = next((j for j in st.jobs.list() if j.kind == "auth" and j.status in ACTIVE), None)
            if pending is not None:
                raise HTTPException(409, f"a YouTube sign-in is already in progress (job {pending.id}); finish it in the browser or wait for it to time out")
            job = st.jobs.submit("auth", _youtube_auth_fn(app), "youtube sign-in", lane=AUTH_LANE)
            return {"job_id": job.id}
        cfg = settings.platforms.tiktok
        if not (cfg.client_key and cfg.client_secret and cfg.redirect_uri):
            raise HTTPException(400, TIKTOK_SETUP_HELP)
        _expire_pending(st.tiktok_pending)
        state = new_state()
        verifier, challenge = pkce_pair()
        st.tiktok_pending[state] = (verifier, time.monotonic())
        return {"auth_url": build_auth_url(cfg.client_key, cfg.redirect_uri, state, challenge), "state": state}

    @app.post("/api/auth/tiktok/complete")
    def auth_tiktok_complete(body: TikTokCompleteBody) -> dict[str, bool]:
        settings, db = st.settings, st.db
        _expire_pending(st.tiktok_pending)
        url = body.redirect_url.strip()
        state = (body.state or parse_qs(urlsplit(url).query).get("state", [""])[0]).strip()
        pending = st.tiktok_pending.get(state) if state else None
        if pending is None:
            raise HTTPException(400, "state mismatch: this URL does not belong to a sign-in started here (or it expired after 15 minutes); start again")
        verifier, _started = pending
        try:
            code = parse_redirect(url, state)
        except ValueError as err:
            raise HTTPException(400, str(err)) from None
        publisher = TikTokPublisher(settings, db)
        try:
            record = publisher._exchange_code(code, verifier)
        except Exception as err:  # PublishFatal (bad code / secret), transport errors after retries
            raise HTTPException(400, f"token exchange failed: {err}") from None
        publisher._save_token(record)
        st.tiktok_pending.pop(state, None)
        db.log("auth", "tiktok: authorized (ui)")
        return {"ok": True}

    # ---- scheduler ---------------------------------------------------------------------------------------------------------------
    @app.post("/api/scheduler")
    def set_scheduler(body: SchedulerBody) -> dict[str, Any]:
        changed = st.scheduler.start() if body.running else st.scheduler.stop()
        if changed:
            st.db.log("schedule.ui", "scheduler loop started" if body.running else "scheduler loop stopped")
        return _scheduler_block(st)

    @app.post("/api/tick")
    def tick(body: TickBody | None = None) -> dict[str, Any]:
        """Dry run: the report inline (no pipeline step, no lock). Real tick: a job (it may download, transcribe and render
        for minutes) -> {job_id}; the report lands in the job's `result`. 409 while a tick job is already pending."""
        if body and body.dry_run:
            return _tick_dict(S.tick(st.settings, st.db, dry_run=True))
        pending = next((j for j in st.jobs.list() if j.kind == "tick" and j.status in ACTIVE), None)
        if pending is not None:
            raise HTTPException(409, f"a tick is already {pending.status} (job {pending.id}); wait for it to finish")
        job = st.jobs.submit("tick", _tick_fn(app), "scheduler tick", target=QUEUE_TARGET)
        return {"job_id": job.id}

    # ---- diagnostics -------------------------------------------------------------------------------------------------------------
    @app.get("/api/health")
    def health(ready: bool = False) -> JSONResponse:
        """Liveness (default) or readiness (?ready=1: SQLite reachable, ffmpeg present, workspace writable).
        Booleans only: no paths, versions of dependencies or configuration are exposed."""
        if not ready:
            return JSONResponse({"status": "ok", "version": __version__})
        checks: dict[str, bool] = {}
        try:
            with st.db.connect() as c:
                c.execute("SELECT 1").fetchone()
            checks["db"] = True
        except Exception:
            checks["db"] = False
        try:
            checks["ffmpeg"] = Path(F.ffmpeg_exe()).is_file()
        except Exception:
            checks["ffmpeg"] = False
        try:
            ws = st.settings.workspace_dir
            checks["workspace"] = ws.is_dir() and os.access(ws, os.W_OK)
        except Exception:
            checks["workspace"] = False
        ok = all(checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "version": __version__, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/api/doctor")
    def doctor() -> list[dict[str, str]]:
        from .. import cli  # lazy: cli imports typer/rich and this module

        return [{"name": c.name, "status": c.status, "detail": c.detail} for c in cli.run_doctor(st.settings)]

    @app.get("/api/logs")
    def logs(limit: int = 200) -> dict[str, Any]:
        settings, db = st.settings, st.db
        limit = max(1, min(int(limit), LOG_TAIL_MAX))
        rows = [{"ts": r.get("ts"), "level": r.get("level"), "action": r.get("action"), "detail": r.get("detail")} for r in db.recent_log(limit)]
        return {"db": rows, "file": _tail(settings.logs_dir / LOG_FILE_NAME, limit)}

    # ---- settings ------------------------------------------------------------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings_file() -> dict[str, Any]:
        path = _config_path()
        if path.is_file():
            return {"path": str(path), "yaml": path.read_text(encoding="utf-8"), "exists": True}
        return {"path": str(path), "yaml": _settings_yaml(st.settings), "exists": False}

    @app.put("/api/platforms/tiktok")
    def put_tiktok_posting(body: dict[str, Any]) -> dict[str, Any]:
        """TikTok's posting choices (privacy, interactions, disclosure, music consent) written into clipforge.yaml."""
        unknown = sorted(set(body) - set(TIKTOK_POSTING_KEYS))
        if unknown:
            raise HTTPException(400, f"unknown key(s): {', '.join(unknown)}")
        path = _config_path()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
        if not isinstance(data, dict):  # no file yet: start from the live settings so nothing else changes
            data = st.settings.model_dump(mode="json")
        section = data.setdefault("platforms", {}).setdefault("tiktok", {})
        if not isinstance(section, dict):
            raise HTTPException(400, "platforms.tiktok in the YAML is not a mapping")
        section.update({k: (None if v == "" else v) for k, v in body.items()})
        try:
            Settings(**data)
        except ValidationError as err:
            raise HTTPException(400, str(err)) from None
        text = yaml.safe_dump(data, sort_keys=False)
        return put_settings_file(SettingsBody(yaml=text))

    @app.put("/api/settings")
    def put_settings_file(body: SettingsBody) -> dict[str, Any]:
        try:
            data = yaml.safe_load(body.yaml)
        except yaml.YAMLError as err:
            raise HTTPException(400, f"invalid YAML: {err}") from None
        data = {} if data is None else data
        if not isinstance(data, dict) or any(not isinstance(k, str) for k in data):
            raise HTTPException(400, "the configuration must be a YAML mapping of string keys (section: values)")
        if bad := _unknown_keys(data, Settings):
            raise HTTPException(400, f"unknown key(s): {', '.join(bad)} (known top-level keys: {', '.join(Settings.model_fields)})")
        try:
            Settings(**data)
        except ValidationError as err:
            raise HTTPException(400, str(err)) from None
        if st.jobs.is_busy() or st.work_lock.locked():  # a running job keeps the old settings/DB: the UI would show another world
            current = st.jobs.current()
            raise HTTPException(409, f"busy: {current.detail if current else 'the scheduler is ticking'}; save the configuration when it is done")
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.yaml, encoding="utf-8")
        os.environ[CONFIG_ENV] = str(path)
        old_db = st.db.path.resolve()
        st.settings = config.get_settings(reload=True)
        if st.settings.db_path.resolve() != old_db:
            st.db = DB(st.settings.db_path)
        _safe(lambda: setup_logging(st.settings.logs_dir))
        st.db.log("settings.saved", str(path))
        log.info("settings saved to %s and reloaded", path)
        return {"ok": True, "path": str(path)}

    # ---- media ---------------------------------------------------------------------------------------------------------------------
    @app.api_route("/media/{video_id}/clips/{filename}", methods=["GET", "HEAD"])
    def media(video_id: str, filename: str) -> FileResponse:
        root = st.settings.workspace_dir.resolve()
        base = (root / video_id / "clips").resolve()
        target = (base / filename).resolve()
        if base.parent.parent != root or target.parent != base or not target.is_file():
            raise HTTPException(404, "no such media file")
        media_type = MEDIA_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type)
