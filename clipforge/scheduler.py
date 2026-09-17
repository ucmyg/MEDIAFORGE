"""Scheduler (Phase 3): one-shot `tick` (cron / Task Scheduler) and the `daemon` loop around it.

A tick (1) processes queued videos through the pipeline, (2) posts every *scheduled* entry that is due (a per-clip
"post this clip on this platform at this time", see schedule_post) and (3) posts at most ONE ready clip per platform
when a schedule slot is due. A slot (schedule.times, local wall-clock) is due once it has opened today and nothing was posted
on that platform since it opened; missed slots collapse into one post (the daemon never "catches up" a backlog).
Posting is further gated by schedule.min_gap_h since the platform's last post, by Publisher.limits() (per_day and
the YouTube quota budget) and by a non-interactive Publisher.credentials_ok() probe (unusable credentials are a
platform skip, never a clip failure). Failures back off exponentially per (clip, platform) and give up after
schedule.max_attempts; the platform itself cools down for the same window (kv `sched.<platform>.cooldown_until`) so a
systemic failure costs one attempt per window instead of one clip per tick. A permanently failed post never blocks
the clip on other platforms.

Time handling: everything is an aware datetime. `now` is local (datetime.now().astimezone()); DB timestamps are UTC
ISO strings (db.utcnow()) parsed with datetime.fromisoformat, which keeps them comparable with `now`.

Every attempt lands in the posts table (ensure_post / claim_post / mark_posted / mark_post_failed), the SQLite log
table and the rotating log file. `publish_clip` is shared with `clipforge publish --now` so both paths record
identically; the atomic claim (`uploading`) is what keeps the daemon and a concurrent `publish --now` from uploading
the same clip twice.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Sequence

from . import pipeline
from .config import Settings
from .db import CLAIM_STALE_S, DB, Clip, Scheduled
from .log import console, get_logger
from .metadata import ClipMeta, read_meta
from .publish import PLATFORMS, get_publisher
from .publish.base import AuthError, NotConfirmed, Publisher, PublishFatal

log = get_logger(__name__)

TIME_FORMAT = "%H:%M"  # schedule.times entries, local wall-clock
DRY_RUN_POST_ID = ""  # post id recorded in TickResult.posted for a dry run (nothing was sent)
LOG_ACTIONS = {"schedule": ("schedule.posted", "schedule.failed"), "publish": ("publish.posted", "publish.failed")}
COOLDOWN_KEY = "sched.{platform}.cooldown_until"  # kv: platform-level backoff after any failure (UTC ISO)
CLAIM_STALE = timedelta(seconds=CLAIM_STALE_S)  # an `uploading` claim older than this belongs to a dead process and may be taken over
PUBLISHABLE = ("rendered", "ready", "posted")  # never rejected/failed/candidate: re-checked under the claim
SCHEDULABLE = ("ready", "posted")  # a scheduled entry needs an approved clip (posted = approved and out on another platform)
SCHEDULED_GRACE_H = 12.0  # a scheduled entry this late (the scheduler was not running) is marked missed instead of posted
SCHEDULE_PAST_SLACK_S = 60  # "now" typed a moment ago still counts as the future
WHEN_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")  # local wall-clock inputs


class InProgress(RuntimeError):
    """Another clipforge process holds the claim on this (clip, platform); nothing was attempted or recorded."""


@dataclass
class TickResult:
    processed: list[str] = field(default_factory=list)  # video ids the pipeline touched
    posted: list[tuple[str, str, str]] = field(default_factory=list)  # (clip_id, platform, post_id)
    skipped: list[str] = field(default_factory=list)  # human-readable reasons a platform did not post
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        verb = "would post" if self.dry_run else "posted"
        what = ", ".join(f"{clip} -> {platform}" + (f" {post_id}" if post_id else "") for clip, platform, post_id in self.posted)
        posted = f"{verb} {len(self.posted)}" + (f" ({what})" if what else "")
        return f"tick: processed {len(self.processed)} video(s), {posted}, skipped {len(self.skipped)}, errors {len(self.errors)}"


# ---- pure helpers ----------------------------------------------------------------------------------------------------
def parse_times(values: Sequence[str]) -> list[time]:
    """'HH:MM' strings -> sorted, de-duplicated times. ValueError names the offending entry."""
    out: set[time] = set()
    for raw in values:
        try:
            out.add(datetime.strptime(str(raw).strip(), TIME_FORMAT).time())
        except ValueError:
            raise ValueError(f"schedule.times: invalid entry {raw!r} (expected HH:MM, e.g. '09:00')") from None
    return sorted(out)


def slots_for_day(times: Sequence[time], day: date, tz: tzinfo | None) -> list[datetime]:
    """The schedule times placed on `day` as aware datetimes in `tz`, ascending."""
    return sorted(datetime.combine(day, t, tzinfo=tz) for t in times)


def due_slot(now: datetime, times: Sequence[time], last_posted_at: datetime | None) -> datetime | None:
    """The latest slot of `now`'s day that has opened with nothing posted since it, or None."""
    due = None
    for slot in slots_for_day(times, now.date(), now.tzinfo):
        if slot <= now and (last_posted_at is None or last_posted_at < slot):
            due = slot
    return due


def backoff_delay(attempts: int, base: int, cap: int) -> int:
    """Seconds to wait before retry number `attempts + 1`: base * 2**attempts, capped."""
    return int(min(base * 2 ** max(attempts, 0), cap))


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    return now.astimezone() if now.tzinfo is None else now


def _parse_ts(value: str, tz: tzinfo | None) -> datetime:
    """DB ISO timestamp (UTC) -> aware datetime shown in `tz` (so reasons print local wall-clock)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _hm(moment: datetime | None) -> str:
    return moment.strftime(TIME_FORMAT) if moment else "never"


# ---- tick ------------------------------------------------------------------------------------------------------------
def tick(
    settings: Settings,
    db: DB,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    publishers: dict[str, Publisher] | None = None,
    platforms: list[str] | None = None,
) -> TickResult:
    """One scheduler pass: process the queue, then post what is due (at most one clip per platform).

    `publishers` injects Publisher instances by platform (tests); `platforms` restricts/orders the platforms and, when
    given explicitly, bypasses the is_configured() filter applied to the default list.
    """
    result = TickResult(dry_run=dry_run)
    if dry_run:  # "report what would be posted": the (possibly very long) pipeline step is reported, not run
        pending = [v.id for v in db.list_videos() if v.status not in pipeline.TERMINAL_STATUSES]
        if pending:
            result.skipped.append(f"queue: would process {len(pending)} video(s) ({', '.join(pending)})")
    else:
        _process_queue(settings, db, result)
    now = _aware(now)  # after the pipeline step so slot / min-gap / backoff use the current time
    try:
        times = parse_times(settings.schedule.times)
    except ValueError as err:
        result.errors.append(str(err))
        log.error("%s", err)
        return _finish(db, result)
    injected = publishers or {}
    names = platforms or list(injected) or list(PLATFORMS)
    active = _active_publishers(settings, db, names, injected, check_configured=platforms is None, result=result)
    _run_scheduled(settings, db, active, now, result)
    for publisher in active.values():
        try:
            _tick_platform(settings, db, publisher, times, now, list(active), result)
        except Exception as err:  # one publisher's crash (e.g. a missing tz database) must not stop the others
            result.errors.append(f"{publisher.name}: tick failed: {type(err).__name__}: {err}")
            log.error("%s: tick failed: %s: %s", publisher.name, type(err).__name__, err)
    return _finish(db, result)


def _finish(db: DB, result: TickResult) -> TickResult:
    """Log the tick summary; an idle tick only reaches the log file (a 60 s heartbeat is noise on the console and in SQLite)."""
    acted = bool(result.processed or result.posted or result.errors)
    (log.info if acted else log.debug)("%s", result.summary())
    for reason in result.skipped:
        log.debug("skipped: %s", reason)
    if acted:
        db.log("schedule.tick", result.summary() + ("; errors: " + " | ".join(result.errors) if result.errors else ""), level="error" if result.errors else "info")
    return result


def _process_queue(settings: Settings, db: DB, result: TickResult) -> None:
    try:
        reports = pipeline.process_queue(settings, db)
    except Exception as err:  # the pipeline reports per-video failures itself; this is a crash outside it
        result.errors.append(f"queue processing failed: {type(err).__name__}: {err}")
        log.error("queue processing failed: %s: %s", type(err).__name__, err)
        return
    result.processed.extend(r.video_id for r in reports)
    result.errors.extend(f"video {r.video_id}: {r.error}" for r in reports if r.status == "failed" and r.error)


def _active_publishers(
    settings: Settings, db: DB, names: Sequence[str], injected: dict[str, Publisher], *, check_configured: bool, result: TickResult
) -> dict[str, Publisher]:
    """Publishers the tick may post with, in order; unknown platforms become errors, unconfigured ones skip reasons."""
    active: dict[str, Publisher] = {}
    for name in names:
        try:
            publisher = injected.get(name) or get_publisher(name, settings, db)
        except (ValueError, ImportError) as err:
            result.errors.append(f"{name}: {err}")
            log.error("%s: %s", name, err)
            continue
        if check_configured and not publisher.is_configured():
            result.skipped.append(f"{name}: not configured (run `clipforge auth {name}` or set platforms.{name} in clipforge.yaml)")
            continue
        active[name] = publisher
    return active


def _tick_platform(settings: Settings, db: DB, publisher: Publisher, times: list[time], now: datetime, active: list[str], result: TickResult) -> None:
    name = publisher.name
    last_iso = db.latest_activity(name, _iso_utc(now - CLAIM_STALE))  # completed posts and uploads in flight
    last = _parse_ts(last_iso, now.tzinfo) if last_iso else None
    slot = due_slot(now, times, last)
    if slot is None:
        result.skipped.append(f"{name}: {_no_slot_reason(now, times, last)}")
        return
    gap = timedelta(hours=settings.schedule.min_gap_h)
    if last is not None and now - last < gap:
        result.skipped.append(f"{name}: slot {_hm(slot)} due but the last post at {_hm(last)} is less than {settings.schedule.min_gap_h:g} h ago")
        return
    limits = publisher.limits()
    if limits.exhausted:
        note = f"; {limits.note}" if limits.note else ""
        result.skipped.append(f"{name}: slot {_hm(slot)} due but the daily allowance is exhausted ({limits.posted_today}/{limits.per_day} posted today{note})")
        return
    cooldown = cooldown_until(db, name, now.tzinfo)
    if cooldown is not None and cooldown > now:
        result.skipped.append(f"{name}: slot {_hm(slot)} due but {name} is backing off until {_hm(cooldown)} after a failure")
        return
    if not publisher.credentials_ok():
        result.skipped.append(f"{name}: slot {_hm(slot)} due but no usable credentials; run `clipforge auth {name}`")
        log.error("%s: no usable credentials; run `clipforge auth %s`", name, name)
        return
    clip, waiting = _pick_clip(db, name, now)
    if clip is None:
        result.skipped.append(f"{name}: slot {_hm(slot)} due but no ready clip to post" + (f" ({'; '.join(waiting)})" if waiting else ""))
        return
    if result.dry_run:
        result.posted.append((clip.id, name, DRY_RUN_POST_ID))
        log.info("dry run: would post %s to %s for the %s slot", clip.id, name, _hm(slot))
        return
    try:
        post_id = publish_clip(settings, db, publisher, clip, active, now=now)
    except InProgress as err:
        result.skipped.append(f"{name}: slot {_hm(slot)} due but {err}")
        return
    except Exception as err:
        result.errors.append(_failure_message(settings, db, clip, name, err, now))
        return
    result.posted.append((clip.id, name, post_id))


def cooldown_until(db: DB, platform: str, tz: tzinfo | None) -> datetime | None:
    """End of the platform's backoff window set by the last failure (None when there is none)."""
    raw = db.kv_get(COOLDOWN_KEY.format(platform=platform))
    return _parse_ts(raw, tz) if raw else None


def claim_is_live(post, now: datetime | None = None) -> bool:
    """True when `post` is `uploading` under a claim younger than CLAIM_STALE (another process is posting it right now)."""
    if post.status != "uploading" or not post.claimed_at:
        return False
    now = _aware(now)
    return _parse_ts(post.claimed_at, now.tzinfo) > now - CLAIM_STALE


def _no_slot_reason(now: datetime, times: list[time], last: datetime | None) -> str:
    if not times:
        return "schedule.times is empty"
    slots = slots_for_day(times, now.date(), now.tzinfo)
    opened = [s for s in slots if s <= now]
    upcoming = [s for s in slots if s > now]
    next_note = f"; next slot {_hm(upcoming[0])}" if upcoming else "; no more slots today"
    if not opened:
        return f"before the first slot ({_hm(slots[0])})"
    return f"already posted for the {_hm(opened[-1])} slot (at {_hm(last)}){next_note}"


def _pick_clip(db: DB, platform: str, now: datetime) -> tuple[Clip | None, list[str]]:
    """Oldest ready-or-posted clip not yet posted on `platform`, not permanently failed there, not waiting for a retry
    and not being posted by another process right now.

    `posted` clips are included because a clip flips to `posted` once every *active* platform has it (e.g.
    `publish --now --to youtube`, or while tiktok was unconfigured); the is_posted check keeps out the real ones.
    Clips with a pending scheduled entry on `platform` are reserved for their own time. Also returns notes about clips that were passed over because they wait for a backoff or failed permanently.
    """
    notes: list[str] = []
    reserved = db.reserved_clips(platform)  # a pending scheduled entry owns its clip until its time
    for clip in sorted(db.list_clips(status=["ready", "posted"]), key=lambda c: c.created_at):
        if db.is_posted(clip.id, platform) or clip.id in reserved:
            continue
        post = db.get_post(clip.id, platform)
        if post is None:
            return clip, notes
        if claim_is_live(post, now):
            notes.append(f"{clip.id} is being posted by another process")
            continue
        if post.status == "failed":
            notes.append(f"{clip.id} failed permanently after {post.attempts} attempt(s)")
            continue
        if post.attempts > 0 and post.next_attempt_at and _parse_ts(post.next_attempt_at, now.tzinfo) > now:
            notes.append(f"{clip.id} retries after {_hm(_parse_ts(post.next_attempt_at, now.tzinfo))}")
            continue
        return clip, notes
    return None, notes


# ---- scheduled entries: "post this clip on this platform at this time" ----------------------------------------------
def parse_when(text: str, now: datetime | None = None) -> datetime:
    """'YYYY-MM-DD HH:MM' (local wall-clock, 'T' accepted) or any ISO 8601 with an offset -> aware datetime.
    ValueError names the problem; the past (beyond a minute of slack) is refused."""
    now = _aware(now)
    raw = str(text).strip()
    if not raw:
        raise ValueError("a date and time is required, e.g. '2026-09-20 18:00'")
    when: datetime | None = None
    for fmt in WHEN_FORMATS:
        try:
            when = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if when is None:
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"cannot read the time {raw!r}; use 'YYYY-MM-DD HH:MM' (local time)") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    if when < now - timedelta(seconds=SCHEDULE_PAST_SLACK_S):
        raise ValueError(f"{when.strftime('%Y-%m-%d %H:%M')} is in the past; pick a later time")
    return when


def schedule_post(settings: Settings, db: DB, clip_id: str, platform: str, when: datetime, *, now: datetime | None = None) -> Scheduled:
    """Queue clip_id for platform at `when`. ValueError explains a refusal: unknown platform/clip, clip not approved,
    already posted there, already scheduled there, the platform's per-day cap for that day is spoken for."""
    now = _aware(now)
    when = _aware(when)
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}; choose from {', '.join(PLATFORMS)}")
    clip = db.get_clip(clip_id)
    if clip is None:
        raise ValueError(f"unknown clip {clip_id!r}")
    if clip.status not in SCHEDULABLE:
        hint = "approve it in Review first" if clip.status == "rendered" else f"it is {clip.status}"
        raise ValueError(f"clip {clip_id} cannot be scheduled: {hint}")
    if db.is_posted(clip_id, platform):
        raise ValueError(f"clip {clip_id} is already posted on {platform}")
    if when < now - timedelta(seconds=SCHEDULE_PAST_SLACK_S):
        raise ValueError(f"{when.strftime('%Y-%m-%d %H:%M')} is in the past; pick a later time")
    per_day = _per_day(settings, platform)
    if per_day >= 0:
        local = when.astimezone(now.tzinfo)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_iso, end_iso = _iso_utc(day_start), _iso_utc(day_start + timedelta(days=1))
        taken = db.count_scheduled_between(platform, start_iso, end_iso)
        taken += sum(1 for p in db.posts_since(platform, start_iso) if p.posted_at and p.posted_at < end_iso)
        if taken >= per_day:
            raise ValueError(f"{platform} already has {taken} post(s) on {local.strftime('%Y-%m-%d')} (limit {per_day} per day); pick another day or raise platforms.{platform}.per_day")
    entry = db.add_scheduled(clip_id, platform, _iso_utc(when))
    db.log("schedule.add", f"{clip_id} -> {platform} at {when.astimezone(now.tzinfo).strftime('%Y-%m-%d %H:%M')} (#{entry.id})")
    log.info("%s: scheduled %s for %s", platform, clip_id, when.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M"))
    return entry


def cancel_scheduled(db: DB, entry_id: int) -> Scheduled:
    """Cancel a pending entry; ValueError when it is unknown or no longer pending."""
    entry = db.get_scheduled(entry_id)
    if entry is None:
        raise ValueError(f"no scheduled post #{entry_id}")
    if not db.cancel_scheduled(entry_id):
        raise ValueError(f"scheduled post #{entry_id} is {entry.status}, not pending; nothing to cancel")
    db.log("schedule.cancel", f"#{entry_id}: {entry.clip_id} -> {entry.platform} at {_parse_ts(entry.run_at, None).strftime('%Y-%m-%d %H:%M')} cancelled")
    refreshed = db.get_scheduled(entry_id)
    return refreshed if refreshed is not None else entry


def _run_scheduled(settings: Settings, db: DB, active: dict[str, Publisher], now: datetime, result: TickResult) -> None:
    """Post every due scheduled entry (earliest first). Retryable failures leave the entry pending (the posts row's
    backoff gates the next try); final ones, a rejected clip and an entry older than SCHEDULED_GRACE_H mark it failed."""
    for entry in db.due_scheduled(_iso_utc(now)):
        name = entry.platform
        label = f"scheduled #{entry.id} {entry.clip_id} -> {name}"
        try:
            _run_one_scheduled(settings, db, active, now, result, entry, label)
        except Exception as err:  # one entry's crash must not stop the others
            result.errors.append(f"{label}: {type(err).__name__}: {err}")
            log.error("%s: %s: %s", label, type(err).__name__, err)


def _run_one_scheduled(settings: Settings, db: DB, active: dict[str, Publisher], now: datetime, result: TickResult, entry: Scheduled, label: str) -> None:
    name = entry.platform
    run_at = _parse_ts(entry.run_at, now.tzinfo)
    clip = db.get_clip(entry.clip_id)
    if clip is None or clip.status not in PUBLISHABLE:
        _scheduled_failed(db, entry, f"clip is {'gone' if clip is None else clip.status}", result, label)
        return
    if db.is_posted(clip.id, name):  # posted by hand / `publish --now` in the meantime
        post = db.get_post(clip.id, name)
        db.set_scheduled(entry.id, "posted", post_id=post.post_id if post else None)
        result.skipped.append(f"{label}: already posted, entry closed")
        return
    if now - run_at > timedelta(hours=SCHEDULED_GRACE_H):
        _scheduled_failed(db, entry, f"missed: due {run_at.strftime('%Y-%m-%d %H:%M')}, the scheduler was not running (more than {SCHEDULED_GRACE_H:g} h ago)", result, label)
        return
    publisher = active.get(name)
    if publisher is None:
        result.skipped.append(f"{label}: due but {name} is not configured (run `clipforge auth {name}`); still pending")
        return
    post = db.get_post(clip.id, name)
    if post is not None:
        if claim_is_live(post, now):
            result.skipped.append(f"{label}: being posted by another process")
            return
        if post.status == "failed":
            _scheduled_failed(db, entry, f"gave up after {post.attempts} attempt(s): {post.error or 'unknown error'}", result, label)
            return
        if post.attempts > 0 and post.next_attempt_at and _parse_ts(post.next_attempt_at, now.tzinfo) > now:
            result.skipped.append(f"{label}: retries after {_hm(_parse_ts(post.next_attempt_at, now.tzinfo))}")
            return
    limits = publisher.limits()
    if limits.exhausted:
        _scheduled_failed(db, entry, f"daily allowance exhausted at the scheduled time ({limits.posted_today}/{limits.per_day} posted today)", result, label)
        return
    cooldown = cooldown_until(db, name, now.tzinfo)
    if cooldown is not None and cooldown > now:
        result.skipped.append(f"{label}: due but {name} is backing off until {_hm(cooldown)} after a failure")
        return
    if not publisher.credentials_ok():
        result.skipped.append(f"{label}: due but no usable credentials; run `clipforge auth {name}`")
        log.error("%s: no usable credentials; run `clipforge auth %s`", name, name)
        return
    if result.dry_run:
        result.posted.append((clip.id, name, DRY_RUN_POST_ID))
        log.info("dry run: would post %s to %s (scheduled for %s)", clip.id, name, _hm(run_at))
        return
    try:
        post_id = publish_clip(settings, db, publisher, clip, list(active), now=now, min_gap=False)
    except InProgress as err:
        result.skipped.append(f"{label}: {err}")
        return
    except Exception as err:
        result.errors.append(f"{label}: " + _failure_message(settings, db, clip, name, err, now))
        post = db.get_post(clip.id, name)
        if post is not None and post.status == "failed":
            db.set_scheduled(entry.id, "failed", error=f"gave up after {post.attempts} attempt(s): {err}")
        return
    db.set_scheduled(entry.id, "posted", post_id=post_id)
    db.log("schedule.posted", f"{label} {post_id} (scheduled for {run_at.strftime('%Y-%m-%d %H:%M')})")
    result.posted.append((clip.id, name, post_id))


def _scheduled_failed(db: DB, entry: Scheduled, reason: str, result: TickResult, label: str) -> None:
    db.set_scheduled(entry.id, "failed", error=reason)
    db.log("schedule.failed", f"{label}: {reason}", level="error")
    log.error("%s: %s", label, reason)
    result.errors.append(f"{label}: {reason}")


# ---- posting (shared with `clipforge publish --now`) ----------------------------------------------------------------
def publish_clip(
    settings: Settings, db: DB, publisher: Publisher, clip: Clip, active: Sequence[str], *, now: datetime | None = None, source: str = "schedule", min_gap: bool | None = None
) -> str:
    """Post one clip through `publisher`, recording the attempt either way; returns the post id or re-raises.

    An exhausted allowance raises PublishFatal before anything is recorded (nothing was attempted); a claim held by
    another process raises InProgress likewise. Success verifies the posted row (publishers mark it themselves; marked
    here otherwise) and flips the clip to `posted` once every platform in `active` has it. Failures get
    attempts/backoff/final recorded (see _record_failure) and are re-raised. A NotConfirmed decline and an interrupt
    (Ctrl-C, SIGTERM) record nothing: the claim is released (a row this call created is dropped again).
    `min_gap` enforces schedule.min_gap_h (default: only for the slot scheduler; an explicit time or `publish --now`
    is the user's own choice).
    """
    now = _aware(now)
    name = publisher.name
    if min_gap is None:
        min_gap = source == "schedule"
    publisher.ensure_allowance()
    row = db.ensure_post(clip.id, name)
    attempts_before = row.attempts
    verdict = db.reserve_and_claim(
        clip.id,
        name,
        _iso_utc(now),
        _iso_utc(now - CLAIM_STALE),
        day_start_iso=_day_start_iso(publisher, now),
        per_day=_per_day(settings, name, publisher),
        gap_before_iso=_iso_utc(now - timedelta(hours=settings.schedule.min_gap_h)) if min_gap else None,
    )
    if verdict == "busy":
        raise InProgress(f"{name}: clip {clip.id} is being posted by another clipforge process")
    if verdict == "per_day":
        raise InProgress(f"{name}: daily allowance reserved by another clipforge process ({_per_day(settings, name, publisher)} per day)")
    if verdict == "gap":
        raise InProgress(f"{name}: another clipforge process posted within the last {settings.schedule.min_gap_h:g} h")
    current = db.get_clip(clip.id)  # re-read under the claim: a rejection that landed after the job was queued wins
    if current is None or current.status not in PUBLISHABLE:
        _discard_attempt(db, row)
        raise PublishFatal(f"{name}: clip {clip.id} is {'gone' if current is None else current.status}; not publishing")
    try:
        post_id = publisher.publish(clip, _load_meta(clip))
    except NotConfirmed:  # a decline is not an attempt (manual.py contract): no attempts, no backoff, no row
        _discard_attempt(db, row)
        raise
    except Exception as err:
        _record_failure(settings, db, clip, name, err, now, attempts_before, source)
        raise
    except BaseException:  # KeyboardInterrupt / SystemExit mid-upload: leave the row pending for the next run
        db.release_claim(clip.id, name)
        raise
    _record_success(db, clip, name, post_id, active, source)
    return post_id


def _per_day(settings: Settings, platform: str, publisher: Publisher | None = None) -> int:
    """The per-day cap the reservation enforces: the publisher's own view of it (limits()), else the config."""
    if publisher is not None:
        try:
            return int(publisher.limits().per_day)
        except Exception:  # limits() failing must not block a post the publisher itself will re-check
            pass
    cfg = getattr(settings.platforms, platform, None)
    return int(getattr(cfg, "per_day", -1)) if cfg is not None else -1


def _day_start_iso(publisher: Publisher, now: datetime) -> str:
    """Today's midnight in the publisher's budget zone as UTC ISO (matches the stored posted_at / claimed_at format)."""
    fn = getattr(publisher, "day_start_iso", None)
    if fn is not None:
        return str(fn())
    return _iso_utc(now.replace(hour=0, minute=0, second=0, microsecond=0))


def _discard_attempt(db: DB, row) -> None:
    """Drop the row when it never recorded an attempt (it was created for this call), else just hand back the claim."""
    if row.attempts == 0 and db.delete_post(row.id):
        return
    db.release_claim(row.clip_id, row.platform)


def _load_meta(clip: Clip) -> ClipMeta:
    if not clip.meta_path or not Path(clip.meta_path).is_file():
        raise PublishFatal(f"clip {clip.id} has no metadata file ({clip.meta_path!r}); run `clipforge run` first")
    return read_meta(clip.meta_path)


def _record_success(db: DB, clip: Clip, platform: str, post_id: str, active: Sequence[str], source: str) -> None:
    if not db.is_posted(clip.id, platform):
        db.mark_posted(clip.id, platform, post_id)
    remaining = [p for p in active if not db.is_posted(clip.id, p)]
    if not remaining:
        db.set_clip_status(clip.id, "posted")
    db.kv_delete(COOLDOWN_KEY.format(platform=platform))  # the platform works again
    db.log(LOG_ACTIONS[source][0], f"{clip.id} -> {platform} {post_id}" + (f" (still ready for {', '.join(remaining)})" if remaining else " (clip status -> posted)"))
    log.info("%s: posted %s -> %s", platform, clip.id, post_id)


def _record_failure(settings: Settings, db: DB, clip: Clip, platform: str, err: BaseException, now: datetime, attempts_before: int, source: str) -> None:
    """Bump attempts (unless the publisher already did), schedule the retry with exponential backoff or mark it final.

    Real publishers record their own failure via mark_post_failed (attempts + 1, next_attempt_at None, final for
    PublishFatal); then only the retry time/status is written here so an attempt is never counted twice, and the
    publisher's verdict on finality (e.g. YouTube quota exhaustion = retry tomorrow) is respected.

    Whatever the verdict, the platform cools down until the retry time (also after a final failure, one window), so
    a systemic failure tries one clip per window instead of a new clip every tick. An AuthError is not the clip's
    fault: its row is left untouched (claim released) and only the platform cooldown and the logs record it.
    """
    sched = settings.schedule
    if isinstance(err, AuthError):
        db.release_claim(clip.id, platform)
        delay = backoff_delay(attempts_before, sched.backoff_base_s, sched.backoff_max_s)
        db.kv_set(COOLDOWN_KEY.format(platform=platform), _iso_utc(now + timedelta(seconds=delay)))
        db.log(LOG_ACTIONS[source][1], f"{clip.id} -> {platform} not attempted (credentials unusable, clip left as is): {err}", level="error")
        log.error("%s: %s not attempted, credentials unusable (run `clipforge auth %s`): %s", platform, clip.id, platform, err)
        return
    row = db.get_post(clip.id, platform)
    if row is not None and row.attempts > attempts_before:  # the publisher recorded this failure itself
        attempts = row.attempts
        final = row.status == "failed" or attempts >= sched.max_attempts
    else:
        row = None
        attempts = attempts_before + 1
        final = isinstance(err, PublishFatal) or attempts >= sched.max_attempts
    delay = backoff_delay(attempts - 1, sched.backoff_base_s, sched.backoff_max_s)
    retry_at = now + timedelta(seconds=delay)
    db.kv_set(COOLDOWN_KEY.format(platform=platform), _iso_utc(retry_at))
    if row is not None:
        _set_retry(db, row.id, None if final else _iso_utc(retry_at), final)
    else:
        db.mark_post_failed(clip.id, platform, f"{type(err).__name__}: {err}", None if final else _iso_utc(retry_at), final=final)
    verdict = "giving up" if final else f"retry after {_hm(retry_at)} ({delay}s backoff)"
    db.log(LOG_ACTIONS[source][1], f"{clip.id} -> {platform} attempt {attempts}/{sched.max_attempts} failed, {verdict}: {err}", level="error")
    log.error("%s: %s attempt %d/%d failed, %s: %s", platform, clip.id, attempts, sched.max_attempts, verdict, err)


def _set_retry(db: DB, post_row_id: int, next_attempt_at: str | None, final: bool) -> None:
    """Write only the retry time/status of a post row whose attempts the publisher already bumped (no DB setter for that)."""
    with db.connect() as c:
        c.execute("UPDATE posts SET status=?, next_attempt_at=? WHERE id=?", ("failed" if final else "pending", next_attempt_at, post_row_id))


def _failure_message(settings: Settings, db: DB, clip: Clip, platform: str, err: BaseException, now: datetime) -> str:
    post = db.get_post(clip.id, platform)
    if post is None or isinstance(err, AuthError):
        return f"{platform}: {clip.id} failed: {err}"
    if post.status == "failed":
        state = f"attempt {post.attempts}/{settings.schedule.max_attempts}, giving up"
    elif post.next_attempt_at:
        state = f"attempt {post.attempts}/{settings.schedule.max_attempts}, retry after {_hm(_parse_ts(post.next_attempt_at, now.tzinfo))}"
    else:
        state = f"attempt {post.attempts}/{settings.schedule.max_attempts}"
    return f"{platform}: {clip.id} failed ({state}): {err}"


# ---- daemon ----------------------------------------------------------------------------------------------------------
def daemon(settings: Settings, db: DB, *, ticks: int | None = None, sleep: Callable[[float], None] | None = None) -> int:
    """Tick every schedule.tick_s seconds until Ctrl-C (or `ticks` passes, for tests). A crashing tick is logged, not fatal.

    Returns the number of ticks run. `sleep` defaults to time.sleep (resolved at call time so tests can patch it).
    """
    sleep = sleep or _time.sleep
    _print_banner(settings, db)
    count = 0
    try:
        while ticks is None or count < ticks:
            count += 1
            try:
                console.print(tick(settings, db).summary(), highlight=False)
            except Exception as err:  # keep the loop alive; the next tick may well succeed
                log.error("tick crashed: %s: %s", type(err).__name__, err)
                db.log("schedule.tick", f"tick crashed: {type(err).__name__}: {err}", level="error")
            if ticks is not None and count >= ticks:
                break
            sleep(settings.schedule.tick_s)
    except KeyboardInterrupt:
        console.print("stopped")
    log.info("daemon stopped after %d tick(s)", count)
    return count


def configured_platforms(settings: Settings, db: DB) -> list[str]:
    """Platforms whose publisher reports credentials/config present (no network)."""
    names: list[str] = []
    for name in PLATFORMS:
        try:
            if get_publisher(name, settings, db).is_configured():
                names.append(name)
        except (ValueError, ImportError) as err:
            log.warning("%s: %s", name, err)
    return names


def _print_banner(settings: Settings, db: DB) -> None:
    sched = settings.schedule
    per_day = ", ".join(f"{name} {getattr(settings.platforms, name).per_day}/day" for name in PLATFORMS)
    configured = ", ".join(configured_platforms(settings, db)) or "none (run `clipforge auth <platform>`)"
    console.print(
        f"clipforge daemon: tick every {sched.tick_s}s; slots {', '.join(sched.times) or 'none'} (local); "
        f"min gap {sched.min_gap_h:g} h; limits {per_day}; configured: {configured}; Ctrl-C stops",
        highlight=False,
    )
    log.info("daemon started (tick %ss, slots %s, configured: %s)", sched.tick_s, ",".join(sched.times), configured)
