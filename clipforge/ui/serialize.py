"""DB dataclasses, metadata and limits -> the JSON dicts of /api/state (see the API contract in server.py)."""
from __future__ import annotations

import os

import re
from pathlib import Path
from typing import Any

from ..db import Clip, Post, Video
from ..metadata import ClipMeta, read_meta
from ..publish import PLATFORMS
from ..publish.base import Limits
from ..publish.youtube import SHORTS_URL
from ..log import get_logger

log = get_logger(__name__)

MEDIA_PREFIX = "/media"
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def post_url(platform: str, post_id: str | None) -> str | None:
    """Where to look at a post: a URL post id as-is, a bare YouTube video id as its Shorts URL (what the CLI prints), else None."""
    pid = (post_id or "").strip()
    if pid.startswith(("http://", "https://")):
        return pid
    if platform == "youtube" and YOUTUBE_ID_RE.match(pid):
        return SHORTS_URL.format(video_id=pid)
    return None


def media_url(clip: Clip) -> str | None:
    """'/media/<video_id>/clips/<file>' when the clip's rendered file exists, else None."""
    if not clip.path:
        return None
    path = Path(clip.path)
    if not path.is_file():
        return None
    return f"{MEDIA_PREFIX}/{clip.video_id}/clips/{path.name}"


META_CACHE_MAX = 5000
_meta_cache: dict[str, tuple[int, int, ClipMeta | None]] = {}  # meta_path -> (mtime_ns, size, parsed)


def forget_meta(meta_path: str | None) -> None:
    """Drop a cached metadata file (called after the UI rewrites it, so a same-second rewrite is never served stale)."""
    if meta_path:
        _meta_cache.pop(str(meta_path), None)


def load_meta(clip: Clip) -> ClipMeta | None:
    """The clip's metadata file, None when missing or unreadable (never raises).

    Cached per path by (mtime_ns, size): the UI polls /api/state every 2 s and re-reading every clip's JSON was the
    dominant cost with a few hundred clips. A rewrite changes mtime/size (and the meta route also calls forget_meta).
    """
    if not clip.meta_path:
        return None
    key = str(clip.meta_path)
    try:
        stat = os.stat(key)
    except OSError:
        _meta_cache.pop(key, None)
        return None
    cached = _meta_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        meta: ClipMeta | None = read_meta(key)
    except Exception as err:  # JSON errors, a stale shape (TypeError)
        log.debug("meta %s unreadable: %s: %s", key, type(err).__name__, err)
        meta = None
    if len(_meta_cache) >= META_CACHE_MAX:
        _meta_cache.clear()
    _meta_cache[key] = (stat.st_mtime_ns, stat.st_size, meta)
    return meta


def meta_dict(meta: ClipMeta | None) -> dict[str, Any] | None:
    if meta is None:
        return None
    return {
        "title": meta.title,
        "description": meta.description,
        "hashtags": {p: list(meta.hashtags.get(p, [])) for p in PLATFORMS},
        "topics": list(meta.topics),
    }


def post_dict(post: Post | None) -> dict[str, Any] | None:
    if post is None:
        return None
    return {
        "status": post.status,
        "post_id": post.post_id,
        "url": post_url(post.platform, post.post_id),
        "error": post.error,
        "attempts": post.attempts,
        "next_attempt_at": post.next_attempt_at,
        "posted_at": post.posted_at,
    }


def video_dict(video: Video, clip_count: int) -> dict[str, Any]:
    return {
        "id": video.id,
        "kind": video.kind,
        "source": video.source,
        "title": video.title,
        "status": video.status,
        "duration": video.duration,
        "error": video.error,
        "options": dict(video.options),
        "created_at": video.created_at,
        "updated_at": video.updated_at,
        "clip_count": clip_count,
    }


def clip_dict(clip: Clip, posts: dict[str, Post | None], meta: ClipMeta | None) -> dict[str, Any]:
    return {
        "id": clip.id,
        "video_id": clip.video_id,
        "idx": clip.idx,
        "start": clip.start,
        "end": clip.end,
        "duration": clip.duration,
        "score": clip.score,
        "hook": clip.hook,
        "status": clip.status,
        "error": clip.error,
        "path": clip.path,
        "media_url": media_url(clip),
        "meta": meta_dict(meta),
        "posts": {p: post_dict(posts.get(p)) for p in PLATFORMS},
    }


def limits_dict(limits: Limits | None) -> dict[str, Any] | None:
    if limits is None:
        return None
    return {
        "per_day": limits.per_day,
        "posted_today": limits.posted_today,
        "remaining": limits.remaining,
        "note": limits.note,
        "quota_used": limits.quota_used,
        "quota_total": limits.quota_total,
    }
