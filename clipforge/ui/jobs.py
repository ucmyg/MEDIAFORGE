"""Background jobs for the web UI: one worker thread per lane, jobs of a lane run one after another.

Two lanes. "work" (pipeline runs, publishing, scheduler ticks) is sequential on purpose: a pipeline run spawns an
ffmpeg process pool, and two of them at once would only fight for the CPU. "auth" carries the YouTube sign-in, which
does nothing but wait for a browser redirect and must never hold up the work lane (nor be held up by a render).
A job function receives its Job and may update `progress` (a free-text line the UI shows while polling /api/state);
anything it raises becomes status `failed` with the error text, never a dead worker.
"""
from __future__ import annotations

import copy
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..db import utcnow
from ..log import get_logger

log = get_logger(__name__)

HISTORY = 50  # jobs kept for /api/state (finished ones beyond this are dropped, oldest first)
STATUSES = ("queued", "running", "done", "failed")
ACTIVE = ("queued", "running")
WORK_LANE = "work"
AUTH_LANE = "auth"

JobFn = Callable[["Job"], Any]


class JobFailed(Exception):
    """Raised by a job function to fail its job with a plain message (no exception type prefix in `error`)."""


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def _targets(target: str | Iterable[str] | None) -> frozenset[str]:
    if target is None:
        return frozenset()
    if isinstance(target, str):
        return frozenset({target})
    return frozenset(str(t) for t in target if t)


@dataclass
class Job:
    id: str
    kind: str  # run | publish | auth | tick
    detail: str  # what was asked, e.g. "run l01c0857ff69"
    status: str = "queued"
    progress: str = ""  # free text updated by the job function
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: Any = None  # JSON-serialisable, set by the job function
    created_at: str = field(default_factory=utcnow)
    targets: frozenset[str] = frozenset()  # what the job works on (video ids, "*" for the queue); not part of the API shape
    lane: str = WORK_LANE  # which worker thread runs it; not part of the API shape
    seq: int = 0  # submission order (created_at has second resolution)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "progress": self.progress,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": copy.deepcopy(self.result),  # snapshot: a running publish job appends to its result list
        }


class JobRunner:
    """FIFO of jobs per lane, each lane run by a single daemon thread (started lazily on the first submit)."""

    def __init__(self, history: int = HISTORY):
        self.history = history
        self._queues: dict[str, queue.Queue[Job | None]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._fns: dict[str, JobFn] = {}
        self._seq = 0

    # ---- public API ----------------------------------------------------------------------------------------------------
    def submit(self, kind: str, fn: JobFn, detail: str, *, target: str | Iterable[str] | None = None, lane: str = WORK_LANE) -> Job:
        """Queue `fn` on `lane`; `target` names the video id(s) (or "*" for the queue) the job works on, for active_targets()."""
        job = Job(id=short_id(), kind=kind, detail=detail, targets=_targets(target), lane=lane)
        with self._lock:
            self._seq += 1
            job.seq = self._seq
            self._fns[job.id] = fn
            self._jobs[job.id] = job
            self._prune()
            q = self._ensure_lane(lane)
        q.put(job)
        log.info("job %s queued: %s %s", job.id, kind, detail)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """Newest first (by creation), at most `history` entries."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.seq, reverse=True)[: self.history]

    def is_busy(self) -> bool:
        with self._lock:
            return any(j.status in ACTIVE for j in self._jobs.values())

    def current(self, lane: str | None = None) -> Job | None:
        """The running job (of `lane` when given); None when nothing runs there."""
        with self._lock:
            return next((j for j in self._jobs.values() if j.status == "running" and (lane is None or j.lane == lane)), None)

    def active_targets(self) -> set[str]:
        with self._lock:
            return {t for j in self._jobs.values() if j.status in ACTIVE for t in j.targets}

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no job is queued or running; False on timeout."""
        with self._idle:
            return self._idle.wait_for(lambda: not any(j.status in ACTIVE for j in self._jobs.values()), timeout)

    def stop(self, timeout: float = 2.0) -> None:
        """Ask every worker to exit once its current job is finished (used at server shutdown)."""
        with self._lock:
            lanes = list(self._threads.items())
        for lane, thread in lanes:
            self._queues[lane].put(None)
        for _lane, thread in lanes:
            thread.join(timeout)

    # ---- worker ------------------------------------------------------------------------------------------------------
    def _ensure_lane(self, lane: str) -> queue.Queue[Job | None]:
        """The lane's queue, starting its thread when missing or dead (caller holds the lock)."""
        q = self._queues.get(lane)
        if q is None:
            q = self._queues[lane] = queue.Queue()
        thread = self._threads.get(lane)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=self._loop, args=(q,), name=f"clipforge-jobs-{lane}", daemon=True)
            self._threads[lane] = thread
            thread.start()
        return q

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.status not in ACTIVE]
        excess = len(self._jobs) - self.history
        for job in sorted(finished, key=lambda j: j.seq)[: max(0, excess)]:
            del self._jobs[job.id]
            self._fns.pop(job.id, None)

    def _loop(self, q: queue.Queue[Job | None]) -> None:
        while True:
            job = q.get()
            if job is None:
                return
            self._run(job)

    def _run(self, job: Job) -> None:
        with self._lock:
            fn = self._fns.pop(job.id, None)
            job.status, job.started_at = "running", utcnow()
        try:
            if fn is None:
                raise RuntimeError("job function missing")
            fn(job)
        except Exception as err:  # the job failed; the worker lives on
            with self._lock:
                job.status, job.error = "failed", str(err) if isinstance(err, JobFailed) else f"{type(err).__name__}: {err}"
            log.error("job %s (%s %s) failed: %s", job.id, job.kind, job.detail, job.error)
        else:
            with self._lock:
                job.status = "done"
            log.info("job %s (%s %s) done", job.id, job.kind, job.detail)
        finally:
            with self._idle:
                job.finished_at = utcnow()
                self._idle.notify_all()
