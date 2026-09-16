"""Scheduler loop for the web UI: a thread that calls scheduler.tick every schedule.tick_s seconds.

Started/stopped from POST /api/scheduler. Waiting happens on a per-thread Event so a stop request returns at once
(a tick in flight finishes first; the next one never starts). While that last tick is still running the loop reports
`stopping` (not `running`), and start() refuses to spawn a second thread until it has exited. A crashing tick is
recorded in `last_summary` and the SQLite log and the loop goes on, like `clipforge daemon`. Settings and DB are
fetched through `context()` on every pass so a config saved in the UI (new tick_s, new db path) is picked up without
a restart.
"""
from __future__ import annotations

import threading
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Callable, Sequence

from .. import scheduler as S
from ..config import Settings
from ..db import DB, utcnow
from ..log import get_logger

log = get_logger(__name__)

MIN_WAIT_S = 1.0  # floor for the wait between ticks (schedule.tick_s is an int >= 0 in config)
Context = Callable[[], tuple[Settings, DB]]


def parse_db_ts(value: str, tz: tzinfo | None) -> datetime:
    """DB ISO timestamp (UTC) -> aware datetime in `tz`."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def next_slot_hm(times: Sequence[time], now: datetime, last_posted_at: datetime | None) -> str | None:
    """'HH:MM' of the slot the scheduler acts on next: the due one (opened today, nothing posted since), else the first
    slot still ahead today, else the first slot tomorrow. None when the schedule has no times."""
    if not times:
        return None
    due = S.due_slot(now, times, last_posted_at)
    if due is not None:
        return due.strftime(S.TIME_FORMAT)
    upcoming = [slot for slot in S.slots_for_day(times, now.date(), now.tzinfo) if slot > now]
    if upcoming:
        return upcoming[0].strftime(S.TIME_FORMAT)
    return S.slots_for_day(times, now.date() + timedelta(days=1), now.tzinfo)[0].strftime(S.TIME_FORMAT)


class SchedulerLoop:
    """Owns the scheduler thread. `lock` serialises ticks with pipeline jobs so two ffmpeg pools never run at once."""

    def __init__(self, context: Context, lock: threading.Lock | None = None):
        self._context = context
        self._lock = lock or threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self.last_tick: str | None = None  # UTC ISO of the last completed tick
        self.last_summary: str | None = None
        self.ticks = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._stop is not None and not self._stop.is_set()

    @property
    def stopping(self) -> bool:
        """stop() was called but the thread is still inside its last tick (a pipeline pass can take minutes)."""
        return self._thread is not None and self._thread.is_alive() and self._stop is not None and self._stop.is_set()

    def start(self) -> bool:
        """Start the loop; False when it already runs or its last tick is still finishing (start again once it has)."""
        if self.running or self.stopping:
            return False
        stop = threading.Event()
        thread = threading.Thread(target=self._loop, args=(stop,), name="clipforge-scheduler", daemon=True)
        self._stop, self._thread = stop, thread
        thread.start()
        log.info("scheduler loop started")
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the loop to end and wait up to `timeout` for the thread; False when nothing was running or a stop is
        already pending. A thread still inside its tick after the wait is kept (see `stopping`), never abandoned."""
        stop, thread = self._stop, self._thread
        if stop is None or thread is None or not thread.is_alive():
            self._stop, self._thread = None, None
            return False
        if stop.is_set():
            return False
        stop.set()
        thread.join(timeout)
        if thread.is_alive():
            log.info("scheduler loop stopping after %d tick(s); the tick in progress finishes first", self.ticks)
        else:
            self._stop, self._thread = None, None
            log.info("scheduler loop stopped after %d tick(s)", self.ticks)
        return True

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            settings, db = self._context()
            try:
                with self._lock:
                    if stop.is_set():
                        break
                    result = S.tick(settings, db)
                self.last_summary = result.summary()
            except Exception as err:  # keep the loop alive; the next tick may well succeed
                self.last_summary = f"tick crashed: {type(err).__name__}: {err}"
                log.error("%s", self.last_summary)
                try:
                    db.log("schedule.tick", self.last_summary, level="error")
                except Exception:  # a broken DB must not kill the loop either
                    log.debug("could not record the crash in the DB log")
            self.last_tick = utcnow()
            self.ticks += 1
            stop.wait(max(MIN_WAIT_S, float(settings.schedule.tick_s)))
