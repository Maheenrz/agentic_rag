"""
cleanup_store.py
-----------------
Durable job queue for document/collection vector cleanup, backed by the
same Postgres database as everything else (no Redis/Celery: this project
doesn't need a second moving part for a queue this small).

A cleanup job survives API restarts because it's a row, written before the
HTTP handler returns. Workers claim a job with a single atomic
UPDATE ... WHERE job_id = (SELECT ... FOR UPDATE SKIP LOCKED), so running
more than one worker process is safe -- they never grab the same job.
Failed attempts get bounded exponential backoff up to a retry limit, after
which a job is marked 'failed' for manual attention instead of retrying
forever. Jobs left 'in_progress' by a worker that crashed or was killed
mid-run are recovered back to 'pending' after a staleness window.

Lifecycle: pending -> in_progress -> completed
                 ^            |
                 |            v
                 +-------- pending (retry) or failed (retries exhausted)
"""
import time
import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import logger
from app.db import connect, run_script, table_columns, engine


JOB_TYPES = {"document", "collection"}

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 30 * 60
STALE_IN_PROGRESS_SECONDS = 10 * 60  # a claim held this long is abandoned


def _init_db() -> None:
    # Timestamp columns are DOUBLE PRECISION (float8), matching Python's
    # time.time(), NOT REAL (float4). REAL only has ~6-7 significant
    # decimal digits; a Unix epoch timestamp already needs ~10 just for
    # whole seconds, so storing one in a REAL column silently rounds it
    # to the nearest ~6-sig-fig value -- e.g. 1792554910.6 round-trips as
    # 1792550000.0, a ~4910 second error. That's harmless for a
    # once-created, rarely-compared created_at elsewhere in this project,
    # but next_attempt_at here is compared against a fresh time.time()
    # within a 30-second initial backoff window, so this precision loss
    # was enough to make a job that was never actually due look claimable
    # (or, as observed, make a job that should still be due look overdue).
    run_script(f"""
    CREATE TABLE IF NOT EXISTS cleanup_jobs (
        job_id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        collection_id TEXT,
        org_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT {MAX_ATTEMPTS},
        next_attempt_at DOUBLE PRECISION NOT NULL,
        last_error TEXT,
        claimed_by TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        started_at DOUBLE PRECISION,
        completed_at DOUBLE PRECISION
    );
    """)

    # Execute each DDL statement separately; run_script wraps one SQLAlchemy
    # text() execution and should not be relied on to execute multiple SQL
    # commands in a single string across PostgreSQL drivers.
    run_script("CREATE INDEX IF NOT EXISTS idx_cleanup_jobs_claim ON cleanup_jobs (status, next_attempt_at)")
    run_script("CREATE INDEX IF NOT EXISTS idx_cleanup_jobs_target ON cleanup_jobs (job_type, target_id)")
    run_script("CREATE INDEX IF NOT EXISTS idx_cleanup_jobs_org ON cleanup_jobs (org_id, status)")

    # Partial unique index: at most one non-completed job per target, so
    # repeated delete requests (retries, double-clicks) reuse the same job
    # instead of piling up duplicates. A target can get a fresh job again
    # once its previous one is 'completed'.
    run_script("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_jobs_active_target
            ON cleanup_jobs (job_type, target_id)
            WHERE status != 'completed';
    """)

    # Migration for jobs tables created before claimed_by existed.
    cols = table_columns("cleanup_jobs")
    if "claimed_by" not in cols and engine:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE cleanup_jobs ADD COLUMN claimed_by TEXT"))

    # Migration for tables created before the timestamp columns used
    # DOUBLE PRECISION. Execute each ALTER separately: SQLAlchemy's
    # ``text()`` execution with psycopg does not reliably accept a
    # multi-command SQL string. Widening preserves rows, although precision
    # already lost while values were stored as REAL cannot be recovered.
    if engine:
        with engine.begin() as conn:
            for column in (
                "next_attempt_at",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
            ):
                conn.execute(text(
                    f"ALTER TABLE cleanup_jobs ALTER COLUMN {column} TYPE DOUBLE PRECISION"
                ))


_init_db()


def _row_to_job(row) -> dict:
    return dict(row)


# --------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------

def enqueue_job(
    job_type: str,
    target_id: str,
    org_id: str,
    collection_id: Optional[str] = None,
) -> str:
    """
    Create (or reuse) a durable cleanup job for this target.

    Idempotent and safe to call on every delete request, including
    retries: if a job for this (job_type, target_id) is already pending
    or in progress, its id is returned unchanged -- no duplicate work is
    queued. If the previous job failed (retries exhausted), it's reset to
    pending so a fresh delete request gives it another chance. A target
    whose previous job already completed gets a brand new row, since
    cleanup is being asked for again (e.g. a legacy record, or a document
    that was deleted a second time after somehow reappearing).

    Raises on database errors so the caller can tell the client that
    queuing failed, rather than claiming work was queued when it wasn't.
    """
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unsupported cleanup job type: {job_type}")

    now = time.time()

    with connect() as session:
        existing = session.execute(
            text("""
                SELECT job_id, status FROM cleanup_jobs
                WHERE job_type = :job_type AND target_id = :target_id
                  AND status != 'completed'
                FOR UPDATE
            """),
            {"job_type": job_type, "target_id": target_id},
        ).mappings().fetchone()

        if existing:
            if existing["status"] == "failed":
                session.execute(
                    text("""
                        UPDATE cleanup_jobs
                        SET status = 'pending', next_attempt_at = :now,
                            updated_at = :now
                        WHERE job_id = :job_id
                    """),
                    {"job_id": existing["job_id"], "now": now},
                )
            return existing["job_id"]

        job_id = uuid.uuid4().hex
        insert_params = {
            "job_id": job_id,
            "job_type": job_type,
            "target_id": target_id,
            "collection_id": collection_id,
            "org_id": org_id,
            "max_attempts": MAX_ATTEMPTS,
            "now": now,
        }

        try:
            # A savepoint, not the whole enqueue transaction: if a concurrent
            # request won the race and already inserted the active job for
            # this target (the FOR UPDATE above found nothing because that
            # row didn't exist yet when we checked), the partial unique
            # index rejects our insert. Roll back just this insert and
            # fetch whichever job actually won, instead of failing the
            # request or leaving two competing jobs for the same target.
            with session.begin_nested():
                session.execute(
                    text("""
                        INSERT INTO cleanup_jobs (
                            job_id, job_type, target_id, collection_id, org_id,
                            status, attempts, max_attempts, next_attempt_at,
                            created_at, updated_at
                        ) VALUES (
                            :job_id, :job_type, :target_id, :collection_id, :org_id,
                            'pending', 0, :max_attempts, :now, :now, :now
                        )
                    """),
                    insert_params,
                )
        except IntegrityError:
            winner = session.execute(
                text("""
                    SELECT job_id FROM cleanup_jobs
                    WHERE job_type = :job_type AND target_id = :target_id
                      AND status != 'completed'
                """),
                {"job_type": job_type, "target_id": target_id},
            ).mappings().fetchone()
            if not winner:
                # Extremely unlikely (the conflicting job would have to have
                # completed in the instant between the conflict and this
                # re-check); fall back to a fresh id rather than erroring.
                raise
            return winner["job_id"]

    logger.info(
        "Cleanup job queued job_id=%s type=%s target=%s org=%s",
        job_id, job_type, target_id, org_id,
    )
    return job_id


def enqueue_and_claim(
    job_type: str,
    target_id: str,
    org_id: str,
    worker_id: str,
    collection_id: Optional[str] = None,
):
    """
    Create (or reuse) a job for this target AND claim it for immediate
    processing, atomically, in one transaction.

    This is what the request handlers (app.services.cleanup.
    request_document_deletion / request_collection_deletion) use for
    their inline fast-path attempt, instead of enqueue_job() followed by
    a separate claim_job_by_id() call. The two-step version leaves a real
    (if usually brief) window where the job sits visibly 'pending' and a
    concurrent background worker's claim_next_job() can legitimately grab
    it first -- not a bug in claim_next_job (claiming the oldest pending
    job really is its job), just a race the two-step version exposes
    itself to. Doing the create/reuse and the claim as one atomic
    transaction closes that window entirely: the job is never visible to
    any other connection in a claimable 'pending' state at all, so the
    background worker cannot win a race for a job this call is about to
    process itself.

    Returns (job_id, claimed_job). claimed_job is None when an existing job
    is already 'in_progress', or when it is pending but its retry time has
    not arrived. In either case the caller must not process it inline.
    """
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unsupported cleanup job type: {job_type}")

    now = time.time()

    with connect() as session:
        existing = session.execute(
            text("""
                SELECT job_id, status, next_attempt_at FROM cleanup_jobs
                WHERE job_type = :job_type AND target_id = :target_id
                  AND status != 'completed'
                FOR UPDATE
            """),
            {"job_type": job_type, "target_id": target_id},
        ).mappings().fetchone()

        if existing:
            job_id = existing["job_id"]
            if existing["status"] == "in_progress":
                return job_id, None

            if existing["status"] == "pending" and existing["next_attempt_at"] > now:
                # Do not let the inline fast path bypass retry backoff.
                return job_id, None

            if existing["status"] == "failed":
                # An explicit new delete request is allowed to retry an
                # exhausted job; make it due before claiming it below.
                session.execute(
                    text("""
                        UPDATE cleanup_jobs
                        SET status = 'pending', next_attempt_at = :now,
                            updated_at = :now, last_error = NULL
                        WHERE job_id = :job_id
                    """),
                    {"job_id": job_id, "now": now},
                )

            # A due pending job (or explicitly reactivated failed job) can
            # be claimed while we still hold its row lock.
            row = session.execute(
                text("""
                    UPDATE cleanup_jobs
                    SET status = 'in_progress', started_at = :now, updated_at = :now,
                        claimed_by = :worker_id, attempts = attempts + 1
                    WHERE job_id = :job_id
                    RETURNING job_id, job_type, target_id, collection_id, org_id,
                              attempts, max_attempts, created_at
                """),
                {"job_id": job_id, "now": now, "worker_id": worker_id},
            ).mappings().fetchone()
            return job_id, _row_to_job(row)

        # No existing job for this target: insert one that is already
        # claimed, directly -- it is never 'pending' at any point another
        # connection could observe.
        job_id = uuid.uuid4().hex
        insert_params = {
            "job_id": job_id,
            "job_type": job_type,
            "target_id": target_id,
            "collection_id": collection_id,
            "org_id": org_id,
            "max_attempts": MAX_ATTEMPTS,
            "now": now,
            "worker_id": worker_id,
        }

        try:
            with session.begin_nested():
                row = session.execute(
                    text("""
                        INSERT INTO cleanup_jobs (
                            job_id, job_type, target_id, collection_id, org_id,
                            status, attempts, max_attempts, next_attempt_at,
                            created_at, updated_at, started_at, claimed_by
                        ) VALUES (
                            :job_id, :job_type, :target_id, :collection_id, :org_id,
                            'in_progress', 1, :max_attempts, :now,
                            :now, :now, :now, :worker_id
                        )
                        RETURNING job_id, job_type, target_id, collection_id, org_id,
                                  attempts, max_attempts, created_at
                    """),
                    insert_params,
                ).mappings().fetchone()
        except IntegrityError:
            # Lost a race to create the active job for this target (another
            # concurrent request's enqueue_and_claim/enqueue_job got there
            # microseconds first). We did not claim it -- report it as
            # pending/in-progress rather than erroring.
            winner = session.execute(
                text("""
                    SELECT job_id FROM cleanup_jobs
                    WHERE job_type = :job_type AND target_id = :target_id
                      AND status != 'completed'
                """),
                {"job_type": job_type, "target_id": target_id},
            ).mappings().fetchone()
            if not winner:
                raise
            return winner["job_id"], None

    logger.info(
        "Cleanup job queued and claimed inline job_id=%s type=%s target=%s org=%s",
        job_id, job_type, target_id, org_id,
    )
    return job_id, _row_to_job(row)


# --------------------------------------------------------------------------
# Claiming and completion (used by the worker)
# --------------------------------------------------------------------------

def claim_next_job(worker_id: str) -> Optional[dict]:
    """
    Atomically claim the oldest due pending job, if any.

    FOR UPDATE SKIP LOCKED means concurrent workers calling this at the
    same time each get a different job (or none), never the same one --
    the standard safe multi-worker queue pattern. This is a single SQL
    statement, so it's atomic on its own; no separate transaction needed.
    """
    now = time.time()
    with connect() as session:
        row = session.execute(
            text("""
                UPDATE cleanup_jobs
                SET status = 'in_progress',
                    started_at = :now,
                    updated_at = :now,
                    claimed_by = :worker_id,
                    attempts = attempts + 1
                WHERE job_id = (
                    SELECT job_id FROM cleanup_jobs
                    WHERE status = 'pending' AND next_attempt_at <= :now
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING job_id, job_type, target_id, collection_id, org_id,
                          attempts, max_attempts, created_at
            """),
            {"now": now, "worker_id": worker_id},
        ).mappings().fetchone()

    return _row_to_job(row) if row else None


def claim_job_by_id(job_id: str, worker_id: str) -> Optional[dict]:
    """
    Atomically claim one specific pending job, if it's still pending.

    Used by app.services.cleanup.try_process_now -- the manual "retry
    this job now" path (app.routers.collections.retry_cleanup_job) -- to
    reclaim an existing job by id. The inline fast-path taken right after
    a delete request uses enqueue_and_claim() instead, which creates and
    claims a job in one atomic step so it's never visibly 'pending' at
    all; this function is for claiming a job that already exists and
    already is 'pending'. Safe against a concurrent background worker
    also trying to claim the same job: the UPDATE's WHERE clause
    (status = 'pending') means only one caller ever sees a matching row
    -- Postgres row locking serializes the two
    attempts, and the loser simply matches zero rows and gets None back,
    rather than both processing the same job at once.
    """
    now = time.time()
    with connect() as session:
        row = session.execute(
            text("""
                UPDATE cleanup_jobs
                SET status = 'in_progress',
                    started_at = :now,
                    updated_at = :now,
                    claimed_by = :worker_id,
                    attempts = attempts + 1
                WHERE job_id = :job_id
                  AND status = 'pending'
                  AND next_attempt_at <= :now
                RETURNING job_id, job_type, target_id, collection_id, org_id,
                          attempts, max_attempts, created_at
            """),
            {"now": now, "worker_id": worker_id, "job_id": job_id},
        ).mappings().fetchone()

    return _row_to_job(row) if row else None


def mark_completed(job_id: str) -> None:
    now = time.time()
    with connect() as session:
        session.execute(
            text("""
                UPDATE cleanup_jobs
                SET status = 'completed', completed_at = :now, updated_at = :now,
                    last_error = NULL
                WHERE job_id = :job_id
            """),
            {"job_id": job_id, "now": now},
        )
    logger.info("Cleanup job completed and verified job_id=%s", job_id)


def record_attempt_failure(
    job_id: str,
    attempts: int,
    max_attempts: int,
    error: str,
) -> str:
    """
    Record a failed (or unverified) attempt and schedule the next one with
    bounded exponential backoff, or give up and mark the job 'failed' once
    max_attempts is reached. Returns the status the job was set to.
    """
    now = time.time()

    if attempts >= max_attempts:
        new_status = "failed"
        next_attempt_at = now
    else:
        new_status = "pending"
        backoff = min(
            MAX_BACKOFF_SECONDS,
            BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)),
        )
        next_attempt_at = now + backoff

    with connect() as session:
        session.execute(
            text("""
                UPDATE cleanup_jobs
                SET status = :status, next_attempt_at = :next_attempt_at,
                    updated_at = :now, last_error = :error
                WHERE job_id = :job_id
            """),
            {
                "status": new_status,
                "next_attempt_at": next_attempt_at,
                "now": now,
                "error": (error or "")[:2000],
                "job_id": job_id,
            },
        )

    log_fn = logger.error if new_status == "failed" else logger.warning
    log_fn(
        "Cleanup job attempt failed job_id=%s attempts=%d/%d new_status=%s error=%s",
        job_id, attempts, max_attempts, new_status, error,
    )
    return new_status


def recover_abandoned_jobs(
    stale_after_seconds: float = STALE_IN_PROGRESS_SECONDS,
) -> int:
    """
    Requeue jobs stuck 'in_progress' past the staleness window -- the
    worker that claimed them died (process killed, API restarted) before
    finishing. Called on worker startup and on a periodic sweep so cleanup
    always resumes automatically rather than hanging forever on a crashed
    attempt. Does not consume a retry attempt itself (the next real
    attempt, when claimed, increments attempts as usual).
    """
    cutoff = time.time() - stale_after_seconds
    now = time.time()

    with connect() as session:
        result = session.execute(
            text("""
                UPDATE cleanup_jobs
                SET status = 'pending', next_attempt_at = :now, updated_at = :now,
                    last_error = CASE
                        WHEN last_error IS NULL OR last_error = ''
                            THEN 'recovered: abandoned by a worker that did not finish'
                        ELSE last_error || '; recovered: abandoned by a worker that did not finish'
                    END
                WHERE status = 'in_progress' AND started_at < :cutoff
            """),
            {"now": now, "cutoff": cutoff},
        )
        count = result.rowcount

    if count:
        logger.warning("Recovered %d abandoned cleanup job(s)", count)
    return count


# --------------------------------------------------------------------------
# Reads (status endpoints, tests)
# --------------------------------------------------------------------------

def get_job(job_id: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT * FROM cleanup_jobs WHERE job_id = :job_id"),
            {"job_id": job_id},
        ).mappings().fetchone()
    return dict(row) if row else None


def get_active_job_for_target(job_type: str, target_id: str) -> Optional[dict]:
    """The current non-completed job for a target, if any."""
    with connect() as session:
        row = session.execute(
            text("""
                SELECT * FROM cleanup_jobs
                WHERE job_type = :job_type AND target_id = :target_id
                  AND status != 'completed'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"job_type": job_type, "target_id": target_id},
        ).mappings().fetchone()
    return dict(row) if row else None


def list_jobs_for_org(
    org_id: str,
    status: Optional[str] = None,
    collection_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """
    Jobs for an org, newest first -- used by the cleanup-status endpoint so
    an admin can see pending/failed cleanup even after the document or
    collection itself has disappeared from normal listings.
    """
    clauses = ["org_id = :org_id"]
    params: dict = {"org_id": org_id, "limit": limit}

    if status:
        clauses.append("status = :status")
        params["status"] = status
    if collection_id:
        clauses.append("(collection_id = :collection_id OR target_id = :collection_id)")
        params["collection_id"] = collection_id

    sql = (
        "SELECT * FROM cleanup_jobs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created_at DESC LIMIT :limit"
    )

    with connect() as session:
        rows = session.execute(text(sql), params).mappings().fetchall()
    return [dict(row) for row in rows]


def list_pending_due(limit: int = 50) -> list[dict]:
    """Diagnostic helper: pending jobs currently eligible to be claimed."""
    now = time.time()
    with connect() as session:
        rows = session.execute(
            text("""
                SELECT * FROM cleanup_jobs
                WHERE status = 'pending' AND next_attempt_at <= :now
                ORDER BY created_at
                LIMIT :limit
            """),
            {"now": now, "limit": limit},
        ).mappings().fetchall()
    return [dict(row) for row in rows]


def requeue_job(job_id: str, org_id: str) -> bool:
    """
    Manually force a job back to pending for immediate retry -- used by the
    admin 'retry' action on a job that's 'failed' (retries exhausted) or
    stuck. Scoped to org_id so one tenant can't poke another's job.
    """
    now = time.time()
    with connect() as session:
        result = session.execute(
            text("""
                UPDATE cleanup_jobs
                SET status = 'pending', next_attempt_at = :now, updated_at = :now
                WHERE job_id = :job_id AND org_id = :org_id
                  AND status IN ('failed', 'pending')
            """),
            {"job_id": job_id, "org_id": org_id, "now": now},
        )
        return result.rowcount == 1