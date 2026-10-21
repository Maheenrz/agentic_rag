"""
tests/test_cleanup_store.py
-----------------------------
Integration tests for app.stores.cleanup_store against a real Postgres
database -- the job-queue mechanics (safe concurrent claiming via
FOR UPDATE SKIP LOCKED, the partial unique index, backoff scheduling,
abandoned-job recovery) are exactly the kind of thing a mock can't
meaningfully verify, since the whole point is real database behavior.

Use a dedicated, throwaway database, never dev or prod and never the
server's default "postgres" database -- same one test_app.py uses, and
the same reasoning: this file reads/writes cleanup_jobs rows freely and
never truncates the table, so don't point it at anything precious:
    export TEST_DATABASE_URL="postgresql://postgres:test@localhost:5433/agentic_rag_test"
    python -m pytest -q tests/test_cleanup_store.py
"""
import os
import time
import uuid

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if not TEST_DB:
    pytest.exit("Set TEST_DATABASE_URL to a throwaway Postgres database (see the README).", returncode=2)

os.environ.update({
    "DATABASE_URL": TEST_DB,
    "IS_PG": "true",
    "GROQ_API_KEY": "test-key",
    "JWT_SECRET_KEY": "test-secret-not-for-production-0123456789abcdef",
    "APP_ENV": "dev",
    "FORCE_OFFLINE_EMBEDDINGS": "true",
})

from app.db import init_db  # noqa: E402
init_db()

from app.stores import cleanup_store  # noqa: E402


def _id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------
# enqueue: idempotent, reuses the active job for a target
# --------------------------------------------------------------------------

def test_enqueue_is_idempotent_for_a_pending_job():
    org_id, target_id = _id("org"), _id("doc")

    job_id_1 = cleanup_store.enqueue_job("document", target_id, org_id)
    job_id_2 = cleanup_store.enqueue_job("document", target_id, org_id)

    assert job_id_1 == job_id_2

    job = cleanup_store.get_job(job_id_1)
    assert job["status"] == "pending"
    assert job["attempts"] == 0


def test_enqueue_resets_a_failed_job_to_pending():
    org_id, target_id = _id("org"), _id("doc")

    job_id = cleanup_store.enqueue_job("document", target_id, org_id)
    cleanup_store.claim_job_by_id(job_id, "w1")
    job = cleanup_store.get_job(job_id)
    cleanup_store.record_attempt_failure(job_id, job["max_attempts"], job["max_attempts"], "boom")
    assert cleanup_store.get_job(job_id)["status"] == "failed"

    reused_id = cleanup_store.enqueue_job("document", target_id, org_id)

    assert reused_id == job_id
    assert cleanup_store.get_job(job_id)["status"] == "pending"


def test_enqueue_gives_a_fresh_job_once_the_previous_one_completed():
    org_id, target_id = _id("org"), _id("doc")

    job_id_1 = cleanup_store.enqueue_job("document", target_id, org_id)
    cleanup_store.claim_job_by_id(job_id_1, "w1")
    cleanup_store.mark_completed(job_id_1)

    job_id_2 = cleanup_store.enqueue_job("document", target_id, org_id)

    assert job_id_2 != job_id_1
    assert cleanup_store.get_job(job_id_2)["status"] == "pending"


# --------------------------------------------------------------------------
# enqueue_and_claim: the atomic inline fast-path (never visibly 'pending')
# --------------------------------------------------------------------------

def test_enqueue_and_claim_creates_and_claims_in_one_step():
    org_id, target_id = _id("org"), _id("doc")

    job_id, job = cleanup_store.enqueue_and_claim("document", target_id, org_id, "inline-1")

    assert job is not None
    assert job["job_id"] == job_id
    assert job["attempts"] == 1
    stored = cleanup_store.get_job(job_id)
    assert stored["status"] == "in_progress"
    assert stored["claimed_by"] == "inline-1"


def test_enqueue_and_claim_never_leaves_the_job_visibly_pending_to_a_racing_claimer():
    """The whole point of enqueue_and_claim: claim_next_job (what the
    background worker uses) must never be able to grab a job this call
    just created, because it's never 'pending' at any point another
    connection could observe it."""
    org_id, target_id = _id("org"), _id("doc")

    job_id, job = cleanup_store.enqueue_and_claim("document", target_id, org_id, "inline-1")
    assert job is not None

    raced = cleanup_store.claim_next_job("racing-worker")
    assert raced is None or raced["job_id"] != job_id


def test_enqueue_and_claim_reports_in_progress_as_unclaimed():
    org_id, target_id = _id("org"), _id("doc")
    job_id, first = cleanup_store.enqueue_and_claim("document", target_id, org_id, "inline-1")
    assert first is not None

    same_job_id, second = cleanup_store.enqueue_and_claim("document", target_id, org_id, "inline-2")

    assert same_job_id == job_id
    assert second is None, "a job already in_progress must not be claimed a second time"


def test_enqueue_and_claim_reclaims_a_failed_job():
    org_id, target_id = _id("org"), _id("doc")
    job_id, job = cleanup_store.enqueue_and_claim("document", target_id, org_id, "inline-1")
    cleanup_store.record_attempt_failure(job_id, job["max_attempts"], job["max_attempts"], "boom")
    assert cleanup_store.get_job(job_id)["status"] == "failed"

    same_job_id, reclaimed = cleanup_store.enqueue_and_claim("document", target_id, org_id, "inline-2")

    assert same_job_id == job_id
    assert reclaimed is not None
    assert cleanup_store.get_job(job_id)["status"] == "in_progress"


# --------------------------------------------------------------------------
# claiming: concurrency-safe
# --------------------------------------------------------------------------

def _backdate_created_at(job_id, when):
    """Force a job to sort first in claim_next_job's ORDER BY created_at,
    regardless of whatever else is sitting in this shared, never-truncated
    database (see the module docstring) -- without touching any row but
    our own."""
    from sqlalchemy import text
    from app.db import connect
    with connect() as session:
        session.execute(
            text("UPDATE cleanup_jobs SET created_at = :t WHERE job_id = :jid"),
            {"t": when, "jid": job_id},
        )


def test_claim_next_job_does_not_return_the_same_job_twice():
    org_id = _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_id)
    # Guaranteed to be the oldest pending job in the table, so
    # claim_next_job's first call is deterministic even if other tests
    # (or other runs, since this database is never truncated) have left
    # pending jobs behind.
    _backdate_created_at(job_id, 1.0)

    first = cleanup_store.claim_next_job("worker-a")
    second = cleanup_store.claim_next_job("worker-b")

    assert first is not None
    assert first["job_id"] == job_id
    # Whatever (if anything) a second concurrent claim finds, it must not
    # be the job worker-a already has.
    assert second is None or second["job_id"] != job_id


def test_claim_job_by_id_is_exclusive():
    org_id = _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_id)

    first = cleanup_store.claim_job_by_id(job_id, "inline")
    second = cleanup_store.claim_job_by_id(job_id, "worker")

    assert first is not None and first["job_id"] == job_id
    assert second is None, "a job already claimed must not be claimable again"


def test_claim_next_job_ignores_jobs_not_yet_due():
    """claim_next_job and claim_job_by_id share the identical
    next_attempt_at <= now gate in their SQL -- checked here via
    claim_job_by_id, which is scoped to one job id and so is
    deterministic regardless of whatever else is pending in this shared,
    never-truncated database (see the module docstring). Asserting
    directly against claim_next_job here would be flaky: some other
    due job could legitimately be returned instead of None."""
    org_id = _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_id)

    # Push this job's next_attempt_at into the future, simulating a
    # backoff wait.
    job = cleanup_store.get_job(job_id)
    cleanup_store.claim_job_by_id(job_id, "w1")
    cleanup_store.record_attempt_failure(job_id, 1, job["max_attempts"], "transient")
    reloaded = cleanup_store.get_job(job_id)
    assert reloaded["status"] == "pending"
    assert reloaded["next_attempt_at"] > time.time(), "backoff should push next_attempt_at into the future"

    # A targeted claim must not bypass the backoff.
    assert cleanup_store.claim_job_by_id(job_id, "w2") is None


def test_enqueue_and_claim_does_not_bypass_retry_backoff():
    org_id, target_id = _id("org"), _id("doc")
    job_id = cleanup_store.enqueue_job("document", target_id, org_id)
    original = cleanup_store.get_job(job_id)

    assert cleanup_store.claim_job_by_id(job_id, "worker-1") is not None
    cleanup_store.record_attempt_failure(
        job_id, 1, original["max_attempts"], "temporary failure"
    )

    before = cleanup_store.get_job(job_id)
    assert before["status"] == "pending"
    assert before["next_attempt_at"] > time.time()

    returned_id, claimed = cleanup_store.enqueue_and_claim(
        "document", target_id, org_id, "inline-retry"
    )

    assert returned_id == job_id
    assert claimed is None, "inline claim must respect next_attempt_at backoff"
    after = cleanup_store.get_job(job_id)
    assert after["status"] == "pending"
    assert after["next_attempt_at"] == before["next_attempt_at"]


# --------------------------------------------------------------------------
# backoff and the retry limit
# --------------------------------------------------------------------------

def test_record_attempt_failure_backs_off_then_gives_up():
    org_id = _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_id)
    job = cleanup_store.get_job(job_id)
    max_attempts = job["max_attempts"]

    for attempt in range(1, max_attempts):
        cleanup_store.claim_job_by_id(job_id, "w1")
        status = cleanup_store.record_attempt_failure(job_id, attempt, max_attempts, f"attempt {attempt}")
        assert status == "pending", f"attempt {attempt} should still be retryable"
        # Force it due again so the test can proceed without waiting out
        # real backoff; this does not change attempts/max_attempts.
        cleanup_store.requeue_job(job_id, org_id)

    cleanup_store.claim_job_by_id(job_id, "w1")
    final_status = cleanup_store.record_attempt_failure(
        job_id, max_attempts, max_attempts, "final attempt"
    )

    assert final_status == "failed"
    job = cleanup_store.get_job(job_id)
    assert job["status"] == "failed"
    assert job["last_error"] == "final attempt"


# --------------------------------------------------------------------------
# abandoned-job recovery (simulated worker crash / API restart)
# --------------------------------------------------------------------------

def test_recover_abandoned_jobs_requeues_stale_in_progress_jobs():
    org_id = _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_id)
    cleanup_store.claim_job_by_id(job_id, "worker-that-died")
    assert cleanup_store.get_job(job_id)["status"] == "in_progress"

    # A worker that finished normally would call mark_completed or
    # record_attempt_failure; simulate a crash by just leaving it
    # 'in_progress' and backdating started_at past the staleness window.
    from sqlalchemy import text
    from app.db import connect
    with connect() as session:
        session.execute(
            text("UPDATE cleanup_jobs SET started_at = :t WHERE job_id = :jid"),
            {"t": time.time() - 3600, "jid": job_id},
        )

    recovered = cleanup_store.recover_abandoned_jobs(stale_after_seconds=600)

    assert recovered >= 1
    job = cleanup_store.get_job(job_id)
    assert job["status"] == "pending"
    assert "abandoned" in (job["last_error"] or "")


def test_recover_abandoned_jobs_leaves_recent_in_progress_jobs_alone():
    org_id = _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_id)
    cleanup_store.claim_job_by_id(job_id, "worker-still-running")

    cleanup_store.recover_abandoned_jobs(stale_after_seconds=600)

    assert cleanup_store.get_job(job_id)["status"] == "in_progress"


# --------------------------------------------------------------------------
# status reads used by the API, and tenant-scoped retry
# --------------------------------------------------------------------------

def test_list_jobs_for_org_filters_by_collection():
    org_id = _id("org")
    collection_id = _id("col")
    other_collection_id = _id("col")

    doc_job = cleanup_store.enqueue_job("document", _id("doc"), org_id, collection_id=collection_id)
    coll_job = cleanup_store.enqueue_job("collection", collection_id, org_id)
    other_job = cleanup_store.enqueue_job("document", _id("doc"), org_id, collection_id=other_collection_id)

    jobs = cleanup_store.list_jobs_for_org(org_id, collection_id=collection_id)
    job_ids = {job["job_id"] for job in jobs}

    assert doc_job in job_ids
    assert coll_job in job_ids
    assert other_job not in job_ids


def test_requeue_job_is_scoped_to_the_owning_org():
    org_a, org_b = _id("org"), _id("org")
    job_id = cleanup_store.enqueue_job("document", _id("doc"), org_a)
    cleanup_store.claim_job_by_id(job_id, "w1")
    job = cleanup_store.get_job(job_id)
    cleanup_store.record_attempt_failure(job_id, job["max_attempts"], job["max_attempts"], "boom")
    assert cleanup_store.get_job(job_id)["status"] == "failed"

    # A different org must not be able to requeue this job.
    assert cleanup_store.requeue_job(job_id, org_b) is False
    assert cleanup_store.get_job(job_id)["status"] == "failed"

    assert cleanup_store.requeue_job(job_id, org_a) is True
    assert cleanup_store.get_job(job_id)["status"] == "pending"


def test_get_active_job_for_target_ignores_completed_jobs():
    org_id, target_id = _id("org"), _id("doc")
    job_id = cleanup_store.enqueue_job("document", target_id, org_id)

    assert cleanup_store.get_active_job_for_target("document", target_id)["job_id"] == job_id

    cleanup_store.claim_job_by_id(job_id, "w1")
    cleanup_store.mark_completed(job_id)

    assert cleanup_store.get_active_job_for_target("document", target_id) is None