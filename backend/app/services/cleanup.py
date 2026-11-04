"""
services/cleanup.py
--------------------
Durable document and collection deletion.

This is the one place that knows how to clean up a document or a
collection. It's used two ways:

  1. Inline, once, right after an HTTP delete request -- so the common
     case (storage is healthy) gets an immediate "deleted and verified"
     response instead of always waiting for the background worker's next
     poll.
  2. By the background worker (app.services.cleanup_worker), which keeps
     retrying with backoff until cleanup succeeds, the retry budget is
     exhausted, or the process is told to stop.

Both paths claim a job with cleanup_store.claim_job_by_id /
claim_next_job before touching storage, so at most one of them is ever
actually running the cleanup for a given job at a time -- if the inline
attempt and a worker race to claim the same freshly-queued job, exactly
one wins the claim and the other is a safe no-op.

Every storage mutation here (delete_doc_chunks, delete_collection_chunks,
purge_summaries_for_document, purge_user_summaries,
finalize_collection_deletion) is itself idempotent, so re-running this
module's functions after a partial failure is always safe.
"""
import os
import time
import uuid
from typing import Optional

from app.config import logger
from app.stores import cleanup_store, org_store
from app.stores.users_store import list_org_users
from app.rag.document_processor import (
    count_collection_chunks,
    count_doc_chunks,
    delete_collection_chunks,
    delete_doc_chunks,
    purge_cache_for_threads,
    purge_summaries_for_document,
    purge_user_summaries,
    remaining_summaries_for_collection,
    remaining_summaries_for_document,
)

# Stable-ish per-process id for diagnostics (which process/thread claimed a
# job). Not used for correctness -- claiming is safe regardless.
_WORKER_ID = f"api:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class CleanupVerificationError(RuntimeError):
    """Raised when cleanup ran without a storage error, but a verification
    read afterward still finds data that should be gone. Treated the same
    as any other failed attempt: the job is retried, never marked
    complete on the strength of 'the delete statement didn't raise'."""


# --------------------------------------------------------------------------
# Job processing (shared by the inline path and the background worker)
# --------------------------------------------------------------------------

def _process_document_job(job: dict) -> None:
    doc_id = job["target_id"]
    collection_id = job.get("collection_id") or ""
    org_id = job["org_id"]

    deleted_rows = delete_doc_chunks(doc_id)

    try:
        org_user_ids = [u["user_id"] for u in list_org_users(org_id)]
    except Exception as exc:
        raise RuntimeError(f"could not list org users for summary cleanup: {exc}") from exc

    summaries_purged = purge_summaries_for_document(doc_id, collection_id, org_user_ids)

    # Cache cleanup is hygiene on top of an already-correct cache (see
    # purge_cache_for_threads docstring) -- best-effort, never fails the job.
    purge_cache_for_threads(org_store.thread_ids_for_document(doc_id))

    remaining_chunks = count_doc_chunks(doc_id)
    remaining_summaries = remaining_summaries_for_document(doc_id, collection_id, org_user_ids)

    if remaining_chunks or remaining_summaries:
        raise CleanupVerificationError(
            f"doc {doc_id}: {remaining_chunks} chunk(s) and "
            f"{remaining_summaries} summary(ies) still present after cleanup"
        )

    logger.info(
        "Document cleanup verified doc_id=%s deleted_chunks=%d summaries_purged=%d",
        doc_id, deleted_rows, summaries_purged,
    )


def _process_collection_job(job: dict) -> None:
    collection_id = job["target_id"]
    org_id = job["org_id"]

    # If the collection row is already gone, a previous run already
    # finished (deleted vectors, summaries, AND the relational rows) but
    # presumably crashed before marking the job completed. Nothing left
    # to do -- this is success, not an error.
    collection = org_store.get_collection(collection_id, org_id, include_deleting=True)
    if not collection:
        logger.info(
            "Collection %s already fully removed; treating job as complete",
            collection_id,
        )
        return

    deleted_rows = delete_collection_chunks(collection_id)

    try:
        org_user_ids = {u["user_id"] for u in list_org_users(org_id)}
    except Exception as exc:
        raise RuntimeError(f"could not list org users for summary cleanup: {exc}") from exc

    # A personal collection's owner may already be gone from the users
    # table by the time this runs (org.py deletes the user row right
    # after queuing this job, without waiting for cleanup to finish).
    # Their own summaries are still keyed by their user_id in the memory
    # store regardless, so include it explicitly -- list_org_users() alone
    # would silently skip a departed user and leave their summaries
    # referencing this collection un-purged forever.
    owner_id = collection.get("owner_user_id")
    if owner_id:
        org_user_ids.add(owner_id)

    summaries_purged = sum(
        purge_user_summaries(uid, [collection_id]) for uid in org_user_ids
    )

    purge_cache_for_threads(org_store.thread_ids_for_collection(collection_id))

    remaining_chunks = count_collection_chunks(collection_id)
    remaining_summaries = remaining_summaries_for_collection(collection_id, org_user_ids)

    if remaining_chunks or remaining_summaries:
        raise CleanupVerificationError(
            f"collection {collection_id}: {remaining_chunks} chunk(s) and "
            f"{remaining_summaries} summary(ies) still present after cleanup"
        )

    # Only now -- cleanup verified -- remove the relational rows. A crash
    # between here and mark_completed() just means the next attempt finds
    # the collection row already gone (handled above) and finishes cleanly.
    org_store.finalize_collection_deletion(collection_id, org_id)

    logger.info(
        "Collection cleanup verified collection_id=%s deleted_chunks=%d summaries_purged=%d",
        collection_id, deleted_rows, summaries_purged,
    )


_PROCESSORS = {
    "document": _process_document_job,
    "collection": _process_collection_job,
}


def process_job(job: dict) -> None:
    """Run the cleanup for one claimed job. Raises on any failure
    (storage error or verification failure) -- callers decide what that
    means for the job's status."""
    processor = _PROCESSORS.get(job["job_type"])
    if processor is None:
        raise ValueError(f"Unknown cleanup job type: {job['job_type']}")
    processor(job)


def run_claimed_job(job: dict) -> str:
    """Process a claimed job and record the outcome. Returns the resulting
    status: 'completed', 'pending' (will retry), or 'failed' (retries
    exhausted)."""
    try:
        process_job(job)
    except Exception as exc:
        logger.exception(
            "Cleanup job attempt failed job_id=%s type=%s target=%s",
            job["job_id"], job["job_type"], job["target_id"],
        )
        return cleanup_store.record_attempt_failure(
            job["job_id"], job["attempts"], job["max_attempts"], str(exc),
        )

    cleanup_store.mark_completed(job["job_id"])
    return "completed"


def try_process_now(job_id: str) -> Optional[str]:
    """
    Attempt to claim and run a specific job right now, in this process.

    Used right after enqueueing, for a fast inline response. Returns the
    resulting status ('completed', 'pending', 'failed'), or None if the
    job could not be claimed here (already claimed by a concurrent worker
    or inline attempt, or no longer pending) -- in which case the
    background worker owns it and will finish it.
    """
    job = cleanup_store.claim_job_by_id(job_id, _WORKER_ID)
    if job is None:
        return None
    return run_claimed_job(job)


# --------------------------------------------------------------------------
# Request handlers (used by routers)
# --------------------------------------------------------------------------

def request_document_deletion(
    doc_id: str,
    collection_id: str,
    org_id: str,
) -> dict:
    """
    Durable document deletion.

    The document is retired (excluded from retrieval) by the caller
    BEFORE this is called -- see app.routers.collections.delete_document,
    which must do that regardless of whether queuing succeeds. This
    function only handles the durable, retryable physical cleanup: create
    (or reuse) the job AND claim it for this request in one atomic step
    (enqueue_and_claim), so the background worker can never race this
    call for the job it's about to process inline -- see
    cleanup_store.enqueue_and_claim's docstring.

    Returns {"job_id", "cleanup_status"} where cleanup_status is one of
    'completed', 'pending', or 'failed'.
    """
    job_id, job = cleanup_store.enqueue_and_claim(
        job_type="document",
        target_id=doc_id,
        org_id=org_id,
        worker_id=_WORKER_ID,
        collection_id=collection_id,
    )

    if job is None:
        # Already 'in_progress' under someone else (a duplicate concurrent
        # request, or a prior attempt the worker is mid-way through
        # retrying). It's durably queued either way.
        return {"job_id": job_id, "cleanup_status": "pending"}

    return {"job_id": job_id, "cleanup_status": run_claimed_job(job)}


def request_collection_deletion(
    collection_id: str,
    org_id: str,
) -> Optional[dict]:
    """
    Durable collection deletion.

    Marks the collection 'deleting' (excluded from listings/ACL/uploads
    immediately), then creates-and-claims a durable cleanup job in one
    atomic step (see request_document_deletion / enqueue_and_claim).
    Returns None if the collection doesn't exist in this org at all
    (never existed, or a previous attempt already finished and removed
    it) -- the caller should treat that as 404, except on a retry where
    "already gone" is actually the success case; callers distinguish
    these by checking cleanup_store history if needed.

    Returns {"job_id", "cleanup_status", "name", "owner_user_id"}.
    """
    collection = org_store.mark_collection_deleting(collection_id, org_id)
    if not collection:
        return None

    job_id, job = cleanup_store.enqueue_and_claim(
        job_type="collection",
        target_id=collection_id,
        org_id=org_id,
        worker_id=_WORKER_ID,
    )

    cleanup_status = "pending" if job is None else run_claimed_job(job)

    return {
        "job_id": job_id,
        "cleanup_status": cleanup_status,
        "name": collection["name"],
        "owner_user_id": collection["owner_user_id"],
    }
