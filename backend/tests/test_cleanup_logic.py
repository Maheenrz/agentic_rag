"""
tests/test_cleanup_logic.py
----------------------------
Unit tests for the durable cleanup orchestration in app.services.cleanup
and app.services.cleanup_worker.

Like tests/test_ingest_lifecycle.py, these are mocked unit tests: every
call into cleanup_store (the real job queue) and document_processor/
org_store (the real vector store and relational DB) is replaced, so no
Postgres connection is made. They cover the ORCHESTRATION logic -- what
gets called, in what order, and what a job's resulting status is -- not
the SQL itself. The real-Postgres counterpart is
tests/test_cleanup_store.py, and tests/test_app.py covers the
end-to-end HTTP behavior.

Run from the backend directory:
    python -m pytest -q tests/test_cleanup_logic.py
"""
import asyncio

import pytest

from app.services import cleanup
from app.services import cleanup_worker


DOC_JOB = {
    "job_id": "job-doc-1",
    "job_type": "document",
    "target_id": "doc-1",
    "collection_id": "col-1",
    "org_id": "org-1",
    "attempts": 1,
    "max_attempts": 8,
}

COLLECTION_JOB = {
    "job_id": "job-col-1",
    "job_type": "collection",
    "target_id": "col-1",
    "collection_id": None,
    "org_id": "org-1",
    "attempts": 1,
    "max_attempts": 8,
}


# --------------------------------------------------------------------------
# _process_document_job
# --------------------------------------------------------------------------

def test_process_document_job_succeeds_when_verification_is_clean(monkeypatch):
    calls = []

    monkeypatch.setattr(cleanup, "delete_doc_chunks", lambda doc_id: calls.append(("delete", doc_id)) or 3)
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [{"user_id": "u1"}, {"user_id": "u2"}])
    monkeypatch.setattr(
        cleanup, "purge_summaries_for_document",
        lambda doc_id, coll_id, user_ids: calls.append(("purge_summaries", doc_id, set(user_ids))) or 2,
    )
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_document", lambda doc_id: ["t1", "t2"])
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: calls.append(("purge_cache", set(thread_ids))))
    monkeypatch.setattr(cleanup, "count_doc_chunks", lambda doc_id: 0)
    monkeypatch.setattr(cleanup, "remaining_summaries_for_document", lambda doc_id, coll_id, user_ids: 0)

    cleanup.process_job(DOC_JOB)  # must not raise

    assert ("delete", "doc-1") in calls
    assert ("purge_summaries", "doc-1", {"u1", "u2"}) in calls
    assert ("purge_cache", {"t1", "t2"}) in calls


def test_process_document_job_raises_when_chunks_remain(monkeypatch):
    monkeypatch.setattr(cleanup, "delete_doc_chunks", lambda doc_id: 3)
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [])
    monkeypatch.setattr(cleanup, "purge_summaries_for_document", lambda *a, **k: 0)
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_document", lambda doc_id: [])
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: None)
    monkeypatch.setattr(cleanup, "count_doc_chunks", lambda doc_id: 2)  # still there!
    monkeypatch.setattr(cleanup, "remaining_summaries_for_document", lambda *a, **k: 0)

    with pytest.raises(cleanup.CleanupVerificationError):
        cleanup.process_job(DOC_JOB)


def test_process_document_job_raises_when_summaries_remain(monkeypatch):
    monkeypatch.setattr(cleanup, "delete_doc_chunks", lambda doc_id: 0)
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [])
    monkeypatch.setattr(cleanup, "purge_summaries_for_document", lambda *a, **k: 0)
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_document", lambda doc_id: [])
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: None)
    monkeypatch.setattr(cleanup, "count_doc_chunks", lambda doc_id: 0)
    monkeypatch.setattr(cleanup, "remaining_summaries_for_document", lambda *a, **k: 1)  # still there!

    with pytest.raises(cleanup.CleanupVerificationError):
        cleanup.process_job(DOC_JOB)


def test_process_document_job_calls_cache_purge_with_doc_threads(monkeypatch):
    """Cache cleanup for a document is scoped to threads that actually used
    it (via thread_ids_for_document), not swept across every thread. Note:
    purge_cache_for_threads() itself is responsible for never raising (see
    its docstring in document_processor.py) -- _process_document_job does
    not wrap this call in its own try/except, so that contract lives and
    is tested at the document_processor level, not here."""
    monkeypatch.setattr(cleanup, "delete_doc_chunks", lambda doc_id: 0)
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [])
    monkeypatch.setattr(cleanup, "purge_summaries_for_document", lambda *a, **k: 0)
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_document", lambda doc_id: ["t1", "t2"])
    monkeypatch.setattr(cleanup, "count_doc_chunks", lambda doc_id: 0)
    monkeypatch.setattr(cleanup, "remaining_summaries_for_document", lambda *a, **k: 0)

    seen = []
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: seen.append(set(thread_ids)))

    cleanup.process_job(DOC_JOB)

    assert seen == [{"t1", "t2"}]


# --------------------------------------------------------------------------
# _process_collection_job
# --------------------------------------------------------------------------

def test_process_collection_job_is_a_noop_when_already_gone(monkeypatch):
    monkeypatch.setattr(cleanup.org_store, "get_collection", lambda cid, org_id, include_deleting=False: None)

    def boom(*a, **k):
        raise AssertionError("delete_collection_chunks must not be called when the collection is already gone")

    monkeypatch.setattr(cleanup, "delete_collection_chunks", boom)

    cleanup.process_job(COLLECTION_JOB)  # must not raise, must not touch storage


def test_process_collection_job_finalizes_only_after_verification(monkeypatch):
    calls = []

    monkeypatch.setattr(
        cleanup.org_store, "get_collection",
        lambda cid, org_id, include_deleting=False: {
            "collection_id": "col-1", "org_id": "org-1", "owner_user_id": None, "status": "deleting",
        },
    )
    monkeypatch.setattr(cleanup, "delete_collection_chunks", lambda cid: 5)
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [{"user_id": "u1"}])
    monkeypatch.setattr(
        cleanup, "purge_user_summaries",
        lambda uid, lost: calls.append(("purge", uid, tuple(lost))) or 1,
    )
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_collection", lambda cid: [])
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: None)
    monkeypatch.setattr(cleanup, "count_collection_chunks", lambda cid: 0)
    monkeypatch.setattr(cleanup, "remaining_summaries_for_collection", lambda cid, uids: 0)
    monkeypatch.setattr(
        cleanup.org_store, "finalize_collection_deletion",
        lambda cid, org_id: calls.append(("finalize", cid, org_id)) or True,
    )

    cleanup.process_job(COLLECTION_JOB)

    assert ("purge", "u1", ("col-1",)) in calls
    assert ("finalize", "col-1", "org-1") in calls


def test_process_collection_job_does_not_finalize_when_verification_fails(monkeypatch):
    monkeypatch.setattr(
        cleanup.org_store, "get_collection",
        lambda cid, org_id, include_deleting=False: {
            "collection_id": "col-1", "org_id": "org-1", "owner_user_id": None, "status": "deleting",
        },
    )
    monkeypatch.setattr(cleanup, "delete_collection_chunks", lambda cid: 0)
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [])
    monkeypatch.setattr(cleanup, "purge_user_summaries", lambda uid, lost: 0)
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_collection", lambda cid: [])
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: None)
    monkeypatch.setattr(cleanup, "count_collection_chunks", lambda cid: 1)  # still there!
    monkeypatch.setattr(cleanup, "remaining_summaries_for_collection", lambda cid, uids: 0)

    def boom(*a, **k):
        raise AssertionError("finalize_collection_deletion must not run before verification passes")

    monkeypatch.setattr(cleanup.org_store, "finalize_collection_deletion", boom)

    with pytest.raises(cleanup.CleanupVerificationError):
        cleanup.process_job(COLLECTION_JOB)


def test_process_collection_job_includes_departed_owner_in_summary_purge(monkeypatch):
    """Regression: a personal collection's owner can already be removed
    from the users table (app.routers.org.org_delete_user deletes the user
    right after queuing this job). list_org_users() alone would then never
    include them, silently leaving their own summaries un-purged forever."""
    calls = []

    monkeypatch.setattr(
        cleanup.org_store, "get_collection",
        lambda cid, org_id, include_deleting=False: {
            "collection_id": "col-1", "org_id": "org-1", "owner_user_id": "departed-user", "status": "deleting",
        },
    )
    monkeypatch.setattr(cleanup, "delete_collection_chunks", lambda cid: 0)
    # The departed user is already gone from the org's user list.
    monkeypatch.setattr(cleanup, "list_org_users", lambda org_id: [{"user_id": "still-here"}])
    monkeypatch.setattr(
        cleanup, "purge_user_summaries",
        lambda uid, lost: calls.append(uid) or 0,
    )
    monkeypatch.setattr(cleanup.org_store, "thread_ids_for_collection", lambda cid: [])
    monkeypatch.setattr(cleanup, "purge_cache_for_threads", lambda thread_ids: None)
    monkeypatch.setattr(cleanup, "count_collection_chunks", lambda cid: 0)
    monkeypatch.setattr(cleanup, "remaining_summaries_for_collection", lambda cid, uids: 0)
    monkeypatch.setattr(cleanup.org_store, "finalize_collection_deletion", lambda cid, org_id: True)

    cleanup.process_job(COLLECTION_JOB)

    assert "departed-user" in calls, (
        "the personal collection owner must be purged even though they're "
        "no longer in list_org_users()"
    )
    assert "still-here" in calls


# --------------------------------------------------------------------------
# run_claimed_job / try_process_now
# --------------------------------------------------------------------------

def test_run_claimed_job_marks_completed_on_success(monkeypatch):
    completed = []
    monkeypatch.setattr(cleanup, "process_job", lambda job: None)
    monkeypatch.setattr(cleanup.cleanup_store, "mark_completed", lambda job_id: completed.append(job_id))

    status = cleanup.run_claimed_job(DOC_JOB)

    assert status == "completed"
    assert completed == ["job-doc-1"]


def test_run_claimed_job_records_failure_without_marking_completed(monkeypatch):
    recorded = []

    def failing_process(job):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(cleanup, "process_job", failing_process)
    monkeypatch.setattr(
        cleanup.cleanup_store, "record_attempt_failure",
        lambda job_id, attempts, max_attempts, error: recorded.append((job_id, attempts, max_attempts, error)) or "pending",
    )

    def boom(*a, **k):
        raise AssertionError("mark_completed must not be called on failure")

    monkeypatch.setattr(cleanup.cleanup_store, "mark_completed", boom)

    status = cleanup.run_claimed_job(DOC_JOB)

    assert status == "pending"
    assert recorded == [("job-doc-1", 1, 8, "storage is down")]


def test_try_process_now_returns_none_when_job_already_claimed_elsewhere(monkeypatch):
    monkeypatch.setattr(cleanup.cleanup_store, "claim_job_by_id", lambda job_id, worker_id: None)

    assert cleanup.try_process_now("job-doc-1") is None


def test_try_process_now_runs_the_claimed_job(monkeypatch):
    monkeypatch.setattr(cleanup.cleanup_store, "claim_job_by_id", lambda job_id, worker_id: DOC_JOB)
    monkeypatch.setattr(cleanup, "run_claimed_job", lambda job: "completed")

    assert cleanup.try_process_now("job-doc-1") == "completed"


# --------------------------------------------------------------------------
# request_document_deletion / request_collection_deletion
# --------------------------------------------------------------------------

def test_request_document_deletion_reports_pending_when_already_in_progress(monkeypatch):
    """enqueue_and_claim returns (job_id, None) when the job already
    exists and is 'in_progress' under someone else -- see its docstring.
    request_document_deletion must report that as 'pending', not try to
    run a job it doesn't actually hold."""
    monkeypatch.setattr(
        cleanup.cleanup_store, "enqueue_and_claim",
        lambda **kw: ("job-doc-1", None),
    )

    def boom(job):
        raise AssertionError("run_claimed_job must not run a job we were not given")

    monkeypatch.setattr(cleanup, "run_claimed_job", boom)

    result = cleanup.request_document_deletion("doc-1", "col-1", "org-1")

    assert result == {"job_id": "job-doc-1", "cleanup_status": "pending"}


def test_request_document_deletion_reports_completed_on_fast_path(monkeypatch):
    monkeypatch.setattr(
        cleanup.cleanup_store, "enqueue_and_claim",
        lambda **kw: ("job-doc-1", DOC_JOB),
    )
    monkeypatch.setattr(cleanup, "run_claimed_job", lambda job: "completed")

    result = cleanup.request_document_deletion("doc-1", "col-1", "org-1")

    assert result == {"job_id": "job-doc-1", "cleanup_status": "completed"}


def test_request_collection_deletion_returns_none_when_not_found(monkeypatch):
    monkeypatch.setattr(cleanup.org_store, "mark_collection_deleting", lambda cid, org_id: None)

    assert cleanup.request_collection_deletion("col-1", "org-1") is None


def test_request_collection_deletion_reports_pending_when_already_in_progress(monkeypatch):
    monkeypatch.setattr(
        cleanup.org_store, "mark_collection_deleting",
        lambda cid, org_id: {"name": "Policies", "owner_user_id": None, "status": "deleting"},
    )
    monkeypatch.setattr(
        cleanup.cleanup_store, "enqueue_and_claim",
        lambda **kw: ("job-col-1", None),
    )

    def boom(job):
        raise AssertionError("run_claimed_job must not run a job we were not given")

    monkeypatch.setattr(cleanup, "run_claimed_job", boom)

    result = cleanup.request_collection_deletion("col-1", "org-1")

    assert result == {
        "job_id": "job-col-1",
        "cleanup_status": "pending",
        "name": "Policies",
        "owner_user_id": None,
    }


def test_request_collection_deletion_enqueues_after_marking_deleting(monkeypatch):
    monkeypatch.setattr(
        cleanup.org_store, "mark_collection_deleting",
        lambda cid, org_id: {"name": "Policies", "owner_user_id": None, "status": "deleting"},
    )
    monkeypatch.setattr(
        cleanup.cleanup_store, "enqueue_and_claim",
        lambda **kw: ("job-col-1", COLLECTION_JOB),
    )
    monkeypatch.setattr(cleanup, "run_claimed_job", lambda job: "completed")

    result = cleanup.request_collection_deletion("col-1", "org-1")

    assert result == {
        "job_id": "job-col-1",
        "cleanup_status": "completed",
        "name": "Policies",
        "owner_user_id": None,
    }


# --------------------------------------------------------------------------
# Worker: one failing job must not stop the loop or block other jobs
# --------------------------------------------------------------------------

def test_worker_run_one_swallows_an_unexpected_exception(monkeypatch, caplog):
    """run_claimed_job already catches ordinary processing failures (see
    the tests above) -- this covers the extra guard in
    CleanupWorker._run_one for something unexpected raising past that."""
    monkeypatch.setattr(
        cleanup_worker, "run_claimed_job",
        lambda job: (_ for _ in ()).throw(RuntimeError("unexpected bug")),
    )

    cleanup_worker.CleanupWorker._run_one(DOC_JOB)  # must not raise


def test_worker_poll_loop_processes_second_job_after_first_fails(monkeypatch):
    """End-to-end (within this module) check of requirement #9: one
    failing job does not stop the worker from processing other jobs."""
    jobs = [DOC_JOB, COLLECTION_JOB, None]
    processed = []

    def fake_claim_next_job(worker_id):
        return jobs.pop(0)

    def fake_run_one(job):
        processed.append(job["job_id"])
        if job is DOC_JOB:
            raise RuntimeError("boom")  # simulate an unexpected failure

    monkeypatch.setattr(cleanup_worker.cleanup_store, "claim_next_job", fake_claim_next_job)
    monkeypatch.setattr(cleanup_worker.cleanup_store, "recover_abandoned_jobs", lambda *a, **k: 0)
    monkeypatch.setattr(cleanup_worker.CleanupWorker, "_run_one", staticmethod(fake_run_one))

    worker = cleanup_worker.CleanupWorker()

    async def drive():
        # Two polls: one claims+processes DOC_JOB (raises, swallowed by
        # _poll_once's own try/except in _run -- here we call _poll_once
        # directly, so we replicate that one layer of isolation).
        for _ in range(2):
            try:
                await worker._poll_once()
            except Exception:
                pass  # this is exactly what CleanupWorker._run's loop does

    asyncio.run(drive())

    assert processed == ["job-doc-1", "job-col-1"], (
        "the second job must still be processed after the first raised"
    )
