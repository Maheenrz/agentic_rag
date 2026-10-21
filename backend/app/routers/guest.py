"""
routers/guest.py
----------------
Temporary "upload your own doc, ask about it, throw it away" mode.
No auth, no persistence: guests never see org collections, and their
uploads are deleted when the modal closes or after GUEST_SESSION_TTL_MINUTES.
"""
import asyncio
import json
import threading
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse

from app.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    ENABLE_GUEST_CHAT,
    GUEST_COLLECTION_PREFIX,
    GUEST_MAX_UPLOAD_MB,
    GUEST_SESSION_TTL_MINUTES,
    RERANK_CANDIDATE_K,
    logger,
)
from app.core.graph import compiled_graph
from app.deps import (
    _ip,
    guest_question_limiter,
    guest_upload_limiter,
    throttle,
)
from app.guardrails import guardrail_policy
from app.guardrails.security import POLICY_BLOCK_MESSAGE, check_user_input
from app.models import GuestChatRequest
from app.rag.document_processor import (
    add_org_chunks,
    build_org_retriever,
    chunk_documents,
    delete_collection_chunks,
    get_embedding_mode_label,
)
from app.rag.parsers import parse_file
from app.stores import audit_store, org_store

router = APIRouter(prefix="/chat/guest", tags=["guest"])

STREAM_NODES = {"generate", "regenerate"}
_guest_collections: dict[str, datetime] = {}
_guest_lock = threading.Lock()


def _guest_sweep() -> None:
    now = datetime.now(timezone.utc)

    with _guest_lock:
        expired = [
            cid
            for cid, exp in _guest_collections.items()
            if exp <= now
        ]
        for cid in expired:
            _guest_collections.pop(cid, None)

    for cid in expired:
        try:
            delete_collection_chunks(cid)
            logger.info("guest cleanup: deleted chunks for %s", cid)
        except Exception:
            logger.exception("guest cleanup failed for %s", cid)


def _guest_collection_valid(cid: str) -> bool:
    with _guest_lock:
        exp = _guest_collections.get(cid)
        if not exp or exp <= datetime.now(timezone.utc):
            return False

    return True


def _check_embeddings() -> None:
    try:
        org_store.assert_embedding_mode(get_embedding_mode_label())
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def guest_upload(request: Request, files: list[UploadFile] = File(...)):
    if not ENABLE_GUEST_CHAT:
        raise HTTPException(status_code=403, detail="Guest chat is disabled")

    ip = _ip(request)
    throttle(guest_upload_limiter, ip)
    _guest_sweep()

    limit_bytes = GUEST_MAX_UPLOAD_MB * 1024 * 1024
    docs: list = []

    for f in files:
        data = f.file.read(limit_bytes + 1)

        if len(data) > limit_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"{f.filename} is larger than "
                    f"{GUEST_MAX_UPLOAD_MB} MB"
                ),
            )

        try:
            parsed, _notes = parse_file(f.filename or "upload.txt", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        for d in parsed:
            d.metadata.update(source=f.filename or "upload.txt")

        docs.extend(parsed)

    if not docs:
        raise HTTPException(
            status_code=400,
            detail="No text could be extracted from those files",
        )

    chunks = chunk_documents(
        docs,
        DEFAULT_CHUNK_SIZE,
        DEFAULT_CHUNK_OVERLAP,
    )

    if not chunks:
        raise HTTPException(status_code=400, detail="Nothing to index")

    cid = f"{GUEST_COLLECTION_PREFIX}{_uuid.uuid4().hex}"
    doc_id = _uuid.uuid4().hex

    for chunk in chunks:
        chunk.metadata.update(
            org_id="guest",
            collection_id=cid,
            doc_id=doc_id,
        )

    try:
        add_org_chunks(chunks)
    except Exception:
        try:
            delete_collection_chunks(cid)
        except Exception:
            logger.exception(
                "Failed to clean up partially indexed guest collection %s",
                cid,
            )
        raise

    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=GUEST_SESSION_TTL_MINUTES
    )

    with _guest_lock:
        _guest_collections[cid] = expires_at

    logger.info(
        "guest upload: %d chunks -> %s (expires %s)",
        len(chunks),
        cid,
        expires_at.isoformat(),
    )

    return {
        "collection_id": cid,
        "chunks": len(chunks),
        "expires_at": expires_at.isoformat(),
        "ttl_minutes": GUEST_SESSION_TTL_MINUTES,
    }


@router.delete("/collection/{collection_id}")
def guest_release(collection_id: str, request: Request):
    with _guest_lock:
        known = collection_id in _guest_collections
        _guest_collections.pop(collection_id, None)

    if not known:
        return {"released": False}

    try:
        delete_collection_chunks(collection_id)
    except Exception:
        logger.exception(
            "guest release failed for %s",
            collection_id,
        )

    return {"released": True}


@router.post("/stream")
async def guest_chat_stream(body: GuestChatRequest, request: Request):
    if not ENABLE_GUEST_CHAT:
        raise HTTPException(status_code=403, detail="Guest chat is disabled")

    ip = _ip(request)
    throttle(guest_question_limiter, ip)

    if not _guest_collection_valid(body.collection_id):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This upload has expired. Please upload the document again.",
        )

    question = (body.question or "").strip()

    if not question:
        raise HTTPException(status_code=400, detail="Question is empty")

    if len(question) > 2000:
        raise HTTPException(status_code=400, detail="Question is too long")

    allowed, reason, _flags = check_user_input(
        question,
        max_chars=2000,
        block_extraction=True,
    )

    if not allowed:
        audit_store.log(
            "guest_question_blocked",
            ip=ip,
            detail={"reason": reason},
        )

        payload = json.dumps({
            "type": "final",
            "answer": POLICY_BLOCK_MESSAGE,
            "sources": [],
            "trace": [f"Policy gate: blocked ({reason})"],
        }) + "\n"

        return StreamingResponse(
            iter([payload]),
            media_type="application/x-ndjson",
        )

    _check_embeddings()

    retriever = build_org_retriever(
        "guest",
        [body.collection_id],
        k=RERANK_CANDIDATE_K,
    )

    graph_input = {
        "question": question,
        "search_query": "",
        "retrieved_docs": [],
        "relevant_docs": [],
        "rewrite_count": 0,
        "answer": "",
        "trace": [],
        "tool_used": "",
        "sources": [],
        "faithfulness_verdict": "",
        "regenerated": False,
        "security_events": [],
    }

    guest_thread_id = f"guest-{_uuid.uuid4().hex}"

    # These fields group guest runs separately in LangSmith.
    # Avoid adding raw questions, IP addresses, or document text to metadata.
    config = {
        "run_name": "rag.guest_chat",
        "tags": ["agentic-rag", "chat", "guest"],
        "metadata": {
            "pipeline": "rag",
            "actor_type": "guest",
        },
        "configurable": {
            "thread_id": guest_thread_id,
            "user_id": f"guest:{ip}",
            "retriever": retriever,
            "top_k": 4,
            "force_offline": False,
            "guardrails": guardrail_policy.get_policy(None),
            "scope": f"guest:{body.collection_id}",
        },
    }

    def emit(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    async def event_stream():
        state: dict = {}

        try:
            loop = asyncio.get_running_loop()
            gen = compiled_graph.stream(
                graph_input,
                config=config,
                stream_mode=["updates", "messages"],
            )

            def _next():
                try:
                    return next(gen)
                except StopIteration:
                    return None

            while True:
                if await request.is_disconnected():
                    logger.info(
                        "guest stream: client disconnected, aborting (thread=%s)",
                        guest_thread_id,
                    )
                    gen.close()
                    return

                item = await loop.run_in_executor(None, _next)

                if item is None:
                    break

                mode, payload = item

                if mode == "updates":
                    for _node, update in payload.items():
                        if not update:
                            continue

                        state.update(update)

                        if "trace" in update:
                            yield emit({
                                "type": "trace",
                                "trace": update["trace"],
                            })

                        if update.get("faithfulness_verdict") == "UNSUPPORTED":
                            yield emit({"type": "reset"})

                else:
                    chunk, meta = payload

                    if (
                        meta.get("langgraph_node") in STREAM_NODES
                        and isinstance(chunk.content, str)
                        and chunk.content
                    ):
                        yield emit({
                            "type": "token",
                            "text": chunk.content,
                        })

            yield emit({
                "type": "final",
                "answer": state.get("answer", ""),
                "sources": state.get("sources", []),
                "trace": state.get("trace", []),
                "message_id": None,
            })

        except Exception:
            logger.exception("Guest streaming chat failed")
            yield emit({
                "type": "error",
                "detail": "Guest streaming chat failed. Please try again.",
            })

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )