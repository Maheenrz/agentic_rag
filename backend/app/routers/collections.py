
"""
routers/collections.py
----------------------
Collections and their documents:
- list / create / read ACL / set ACL
- delete shared collections (admin only)
- list documents / upload documents / delete a document

Uploads are scanned for prompt-injection findings. HIGH-risk files are
rejected unless an admin explicitly overrides the scan.
"""

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from app.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    MAX_UPLOAD_MB,
    logger,
)
from app.deps import (
    _ip,
    access_snapshot,
    collection_or_404,
    get_current_user,
    purge_after_access_loss,
    require_admin,
    throttle,
    upload_limiter,
)
from app.models import AclRequest, CollectionRequest
from app.rag.document_processor import get_embedding_mode_label
from app.rag.ingest import SuspiciousDocument, ingest_file
from app.services import cleanup as cleanup_service
from app.stores import audit_store, cleanup_store, org_store
from app.stores.users_store import list_org_users

router = APIRouter(prefix="/collections", tags=["collections"])

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
_OK_STATUSES = ("added", "new_version", "unchanged")


# ------------------------------------------------------------------ list/create

@router.get("")
def my_collections(user: dict = Depends(get_current_user)):
    return org_store.list_collections_for(user)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_collection(
    body: CollectionRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    try:
        cid = org_store.create_collection(
            admin["org_id"],
            body.name,
            body.description,
            body.visibility,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # Avoid reporting every database error as a duplicate-name error.
        # Log the real exception on the server, but keep the response generic.
        logger.exception("Failed to create collection")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create collection",
        ) from exc

    audit_store.log(
        "collection_created",
        admin,
        target=cid,
        ip=_ip(request),
        detail={"name": body.name, "visibility": body.visibility},
    )
    return {"collection_id": cid}


# ------------------------------------------------------------- delete collection

@router.delete("/{collection_id}", status_code=status.HTTP_200_OK)
def delete_collection(
    collection_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """
    Delete a shared collection.

    The collection is marked 'deleting' immediately (excluded from
    listings, ACL, and uploads from this point on) and a durable cleanup
    job is persisted before this returns. Vectors, summaries, and
    relational records are removed -- and that removal verified -- by
    app.services.cleanup, either inline in this request (the common,
    fast case) or by the background worker if that doesn't finish here.

    Calling this again on a collection that's already 'deleting' (job
    failed, API restarted mid-cleanup, client retried) safely retries the
    same cleanup job rather than starting a duplicate.

    Personal collections are deliberately excluded from this endpoint.
    """
    # include_deleting=True: a collection already mid-deletion must still
    # be found here so a retried DELETE request can resume its cleanup,
    # not 404 just because it dropped out of normal (active-only) reads.
    coll = org_store.get_collection(
        collection_id, admin["org_id"], include_deleting=True
    )

    if not coll:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )

    if coll.get("owner_user_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Personal collections cannot be deleted through this endpoint",
        )

    try:
        result = cleanup_service.request_collection_deletion(
            collection_id, admin["org_id"],
        )
    except Exception as exc:
        # mark_collection_deleting / enqueue_job failed -- the collection
        # was NOT durably marked for deletion. Do not claim it was queued.
        logger.exception(
            "Could not queue cleanup for collection %s", collection_id
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Could not start collection deletion because the cleanup "
                "job could not be saved. Please retry."
            ),
        ) from exc

    if result is None:
        # Finished (and the row removed) between our lookup above and now --
        # a concurrent retry already completed it.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )

    cleanup_status = result["cleanup_status"]

    audit_store.log(
        "collection_deleted",
        admin,
        target=collection_id,
        ip=_ip(request),
        detail={
            "name": result["name"],
            "job_id": result["job_id"],
            "cleanup_status": cleanup_status,
        },
    )

    return {
        "message": "Collection deleted",
        "collection_id": collection_id,
        "cleanup_status": cleanup_status,
        "job_id": result["job_id"],
    }


# ------------------------------------------------------------------- ACL

@router.get("/{collection_id}/acl")
def get_acl(
    collection_id: str,
    admin: dict = Depends(require_admin),
):
    coll = org_store.get_collection(collection_id, admin["org_id"])

    if not coll:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )

    return {
        "visibility": coll["visibility"],
        **org_store.get_collection_acl(collection_id),
    }


@router.put("/{collection_id}/acl")
def set_acl(
    collection_id: str,
    body: AclRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    coll = org_store.get_collection(collection_id, admin["org_id"])

    if not coll or coll["owner_user_id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )

    org_user_ids = {
        user["user_id"]
        for user in list_org_users(admin["org_id"])
    }

    if not set(body.user_ids) <= org_user_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown user id",
        )

    if not all(
        org_store.group_in_org(group_id, admin["org_id"])
        for group_id in body.group_ids
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown group id",
        )

    before = access_snapshot(admin["org_id"])
    org_store.set_collection_acl(
        collection_id,
        body.user_ids,
        body.group_ids,
    )

    if body.visibility:
        try:
            org_store.set_visibility(collection_id, body.visibility)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    purged = purge_after_access_loss(admin["org_id"], before)

    audit_store.log(
        "acl_changed",
        admin,
        target=collection_id,
        ip=_ip(request),
        detail={
            "user_ids": body.user_ids,
            "group_ids": body.group_ids,
            "visibility": body.visibility,
            "summaries_purged": purged,
        },
    )

    return {"message": "Access updated"}


# ------------------------------------------------------------- documents

def _read_upload(f: UploadFile) -> bytes:
    data = f.file.read(MAX_UPLOAD_BYTES + 1)

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"{f.filename} is larger than "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
            ),
        )

    return data


def _check_embeddings() -> None:
    try:
        org_store.assert_embedding_mode(get_embedding_mode_label())
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _ingest_many(
    user: dict,
    coll: dict,
    files: list[UploadFile],
    chunk_size: int,
    chunk_overlap: int,
    allow_flagged: bool,
    ip: str,
):
    """Return per-file results; fail if no file was ingested successfully."""
    results = []

    for f in files:
        name = f.filename or "untitled"

        try:
            data = _read_upload(f)
            res = ingest_file(
                user,
                coll,
                name,
                data,
                chunk_size,
                chunk_overlap,
                allow_flagged=allow_flagged,
            )
            results.append(res)

            if res["status"] != "unchanged":
                audit_store.log(
                    "document_uploaded",
                    user,
                    target=res.get("doc_id", ""),
                    ip=ip,
                    detail={
                        "collection_id": coll["collection_id"],
                        "filename": res["filename"],
                        "version": res["version"],
                        "findings": len(res.get("findings", [])),
                        "notes": res.get("notes", []),
                        "parse_ms": res.get("parse_ms"),
                        "index_ms": res.get("index_ms"),
                        "cleanup_pending": res.get("cleanup_pending", False),
                    },
                )

            if res.get("overridden"):
                audit_store.log(
                    "upload_security_override",
                    user,
                    target=res["doc_id"],
                    ip=ip,
                    detail={
                        "filename": res["filename"],
                        "findings": res["findings"],
                    },
                )

        except SuspiciousDocument as exc:
            audit_store.log(
                "upload_blocked_suspicious",
                user,
                ip=ip,
                detail={
                    "collection_id": coll["collection_id"],
                    "filename": exc.filename,
                    "findings": exc.findings,
                },
            )
            results.append({
                "filename": exc.filename,
                "status": "rejected_suspicious",
                "error": str(exc),
                "findings": exc.findings,
            })

        except HTTPException as exc:
            results.append({
                "filename": name,
                "status": "error",
                "error": exc.detail,
            })

        except ValueError as exc:
            results.append({
                "filename": name,
                "status": "error",
                "error": str(exc),
            })

        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    if not any(result["status"] in _OK_STATUSES for result in results):
        code = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if any(
                result["status"] == "rejected_suspicious"
                for result in results
            )
            else status.HTTP_400_BAD_REQUEST
        )

        first_error = (
            results[0].get("error", "No files were ingested")
            if results
            else "No files were provided"
        )

        return JSONResponse(
            status_code=code,
            content={"detail": first_error, "results": results},
        )

    return {"results": results}


@router.get("/{collection_id}/documents")
def collection_documents(
    collection_id: str,
    user: dict = Depends(get_current_user),
):
    collection_or_404(collection_id, user)
    return org_store.list_documents(collection_id)


@router.post("/{collection_id}/documents")
def upload_to_collection(
    collection_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    chunk_overlap: int = Form(DEFAULT_CHUNK_OVERLAP),
    allow_flagged: bool = Form(False),
    user: dict = Depends(get_current_user),
):
    coll = collection_or_404(collection_id, user)

    if not org_store.can_write(user, coll):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can't upload to this collection",
        )

    # Only admins may override the injection scan.
    allow_flagged = bool(allow_flagged) and user.get("role") == "admin"

    throttle(upload_limiter, user["user_id"])
    _check_embeddings()

    return _ingest_many(
        user,
        coll,
        files,
        chunk_size,
        chunk_overlap,
        allow_flagged,
        _ip(request),
    )



@router.delete("/{collection_id}/documents/{doc_id}")
def delete_document(
    collection_id: str,
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    coll = collection_or_404(collection_id, user)

    if not org_store.can_write(user, coll):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can't delete from this collection",
        )

    doc = org_store.get_document(doc_id, collection_id)

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    # Retire first so retrieval excludes the document immediately, even
    # though physical cleanup (below) may still be pending. If it was
    # already retired -- a previous attempt, a restart mid-cleanup, or the
    # client simply retrying -- skip straight to (re)queuing cleanup.
    if doc["status"] != "deleted":
        retired = org_store.retire_document(
            doc_id,
            collection_id,
            status="deleted",
        )
        if not retired:
            # Another request retired it concurrently; re-read and proceed
            # exactly as if we had retired it ourselves.
            doc = org_store.get_document(doc_id, collection_id)
            if not doc or doc["status"] != "deleted":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found",
                )

    try:
        result = cleanup_service.request_document_deletion(
            doc_id, collection_id, user["org_id"],
        )
    except Exception as exc:
        # The document IS retired (excluded from retrieval already), but we
        # could not persist a durable job for the physical cleanup. Say so
        # plainly rather than claiming cleanup was queued when it wasn't.
        logger.exception(
            "Could not queue cleanup for deleted document %s", doc_id
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The document is excluded from retrieval, but the cleanup "
                "job could not be saved. Please retry the deletion."
            ),
        ) from exc

    cleanup_status = result["cleanup_status"]

    audit_store.log(
        "document_deleted",
        user,
        target=doc_id,
        ip=_ip(request),
        detail={
            "collection_id": collection_id,
            "filename": doc["filename"],
            "job_id": result["job_id"],
            "cleanup_status": cleanup_status,
        },
    )

    return {
        "message": "Deleted",
        "cleanup_status": cleanup_status,
        "cleanup_pending": cleanup_status != "completed",
        "job_id": result["job_id"],
    }


@router.get("/{collection_id}/documents/{doc_id}/cleanup-status")
def document_cleanup_status(
    collection_id: str,
    doc_id: str,
    user: dict = Depends(get_current_user),
):
    """
    Status of a document's cleanup job, so an admin can check on a deleted
    document even after it has disappeared from the normal document list.
    Membership in the collection is enough to check -- this does not
    require write access, since checking status isn't a mutation.
    """
    collection_or_404(collection_id, user)

    job = cleanup_store.get_active_job_for_target("document", doc_id)
    if not job:
        # No non-completed job: either nothing was ever deleted here, or
        # the most recent deletion's cleanup already completed.
        return {"doc_id": doc_id, "cleanup_status": "completed", "job": None}

    return {
        "doc_id": doc_id,
        "cleanup_status": job["status"],
        "job": job,
    }


@router.get("/{collection_id}/cleanup-jobs")
def collection_cleanup_jobs(
    collection_id: str,
    admin: dict = Depends(require_admin),
):
    """
    Every cleanup job touching this collection (its own deletion, and any
    of its documents' deletions), newest first. Lets an admin see pending
    or failed cleanup even after deleted documents have disappeared from
    the normal document list, and is how the UI finds jobs to retry.
    """
    return cleanup_store.list_jobs_for_org(
        admin["org_id"], collection_id=collection_id,
    )


@router.post("/{collection_id}/cleanup-jobs/{job_id}/retry")
def retry_cleanup_job(
    collection_id: str,
    job_id: str,
    admin: dict = Depends(require_admin),
):
    """
    Manually nudge a 'failed' (retries exhausted) or stalled cleanup job
    back to pending. The background worker picks it up on its next poll;
    this also makes one inline attempt for an immediate result.
    """
    job = cleanup_store.get_job(job_id)
    if not job or job["org_id"] != admin["org_id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cleanup job not found",
        )

    if job["status"] == "completed":
        return {"job_id": job_id, "cleanup_status": "completed"}

    requeued = cleanup_store.requeue_job(job_id, admin["org_id"])
    if not requeued:
        # In progress right now (a worker already has it) -- nothing to do.
        return {"job_id": job_id, "cleanup_status": job["status"]}

    cleanup_status = cleanup_service.try_process_now(job_id) or "pending"
    return {"job_id": job_id, "cleanup_status": cleanup_status}
