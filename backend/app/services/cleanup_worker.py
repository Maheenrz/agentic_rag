"""
services/cleanup_worker.py
---------------------------
Background worker for durable document/collection cleanup.

Most jobs are actually finished inline, right after the delete request
(see app.services.cleanup.try_process_now) -- this worker exists for
everything that *doesn't* finish inline: a transient Postgres hiccup, an
API restart between the enqueue and the inline attempt, or a job that
needed backoff before its next try.

It's a single asyncio background task per process (started from
FastAPI's lifespan in app.main), not a second service: Postgres already
makes claiming jobs safe across multiple processes via
cleanup_store.claim_next_job's FOR UPDATE SKIP LOCKED, so running
several API instances in production is fine -- they just share the same
job queue.

Blocking DB/network calls (claim_next_job, process_job, ...) are plain
synchronous functions, run off the event loop via asyncio.to_thread so
one slow cleanup attempt doesn't stall request handling.
"""
import asyncio
import os
import random
import uuid

from app.config import logger
from app.stores import cleanup_store
from app.services.cleanup import run_claimed_job

POLL_INTERVAL_SECONDS = 5.0
IDLE_POLL_JITTER_SECONDS = 2.0
ABANDONED_SWEEP_INTERVAL_SECONDS = 120.0
MAX_CONCURRENT_JOBS = 3


class CleanupWorker:
    """One background poll loop. Call start() during app startup and
    stop() during shutdown (see app.main's lifespan)."""

    def __init__(self) -> None:
        self.worker_id = f"worker:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self._last_sweep = 0.0

    def start(self) -> None:
        if self._task is not None:
            return  # already running -- never start a second competing loop
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="cleanup-worker")
        logger.info("Cleanup worker started (%s)", self.worker_id)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=30)
        except asyncio.TimeoutError:
            logger.warning("Cleanup worker did not stop within 30s; cancelling")
            self._task.cancel()
        self._task = None
        logger.info("Cleanup worker stopped (%s)", self.worker_id)

    async def _run(self) -> None:
        # Recover anything left 'in_progress' by a previous process (a
        # crash, a kill -9, a deploy) before polling for new work.
        try:
            await asyncio.to_thread(cleanup_store.recover_abandoned_jobs)
        except Exception:
            logger.exception("Cleanup worker: startup recovery sweep failed")

        while not self._stopping.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("Cleanup worker: unexpected error in poll loop")

            if self._stopping.is_set():
                break

            # Always the same idle-poll interval, with a little jitter so
            # many instances don't all wake in lockstep -- deliberately NOT
            # shortened just because the last poll found work (see the
            # module docstring: this was a real bug). A zero/short delay
            # after a successful claim turns this into a self-sustaining
            # tight loop the moment it ever wins one claim race: it then
            # keeps re-polling fast enough to systematically out-race the
            # inline fast path (app.services.cleanup.request_*_deletion)
            # for every job enqueued afterward, for as long as the process
            # runs -- defeating the whole point of trying inline first.
            # The fix for genuine throughput during a real backlog is a
            # structural one (see cleanup_store.enqueue_and_claim, which
            # makes the inline attempt atomic so it can never be raced at
            # all), not a shorter sleep here.
            delay = POLL_INTERVAL_SECONDS + random.uniform(0, IDLE_POLL_JITTER_SECONDS)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> bool:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._last_sweep > ABANDONED_SWEEP_INTERVAL_SECONDS:
            self._last_sweep = now
            try:
                await asyncio.to_thread(cleanup_store.recover_abandoned_jobs)
            except Exception:
                logger.exception("Cleanup worker: periodic recovery sweep failed")

        job = await asyncio.to_thread(cleanup_store.claim_next_job, self.worker_id)
        if job is None:
            return False

        async with self._semaphore:
            await asyncio.to_thread(self._run_one, job)
        return True

    @staticmethod
    def _run_one(job: dict) -> None:
        # One failing job must never take down the worker loop or block
        # other jobs -- run_claimed_job already catches and records any
        # processing error, so a raise here would only be something truly
        # unexpected (e.g. a bug in run_claimed_job itself). Guard anyway:
        # the poll loop's own try/except would otherwise swallow it
        # silently along with everything else.
        try:
            run_claimed_job(job)
        except Exception:
            logger.exception(
                "Cleanup worker: job %s raised outside run_claimed_job's "
                "own error handling -- this indicates a bug",
                job.get("job_id"),
            )


# A single process-wide instance, matching how the rest of the app treats
# singletons (e.g. the module-level stores). app.main starts/stops this
# one instance; nothing else should construct a CleanupWorker.
worker = CleanupWorker()
