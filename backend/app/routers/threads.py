"""
routers/threads.py
------------------
Everything a logged-in user does with their own chats:
  - create / list / rename / delete a thread
  - get messages / rate a message / clear a rating
  - chat (blocking) and chat/stream
  - upload files to the user's PERSONAL collection
  - finalize a thread (writes a recall summary)
"""
import json
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status,
)
from fastapi.responses import StreamingResponse

from app.config import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, RERANK_CANDIDATE_K
from app.core.graph import compiled_graph, small_llm, summarize_thread_for_storage
from app.deps import (
    _ip, chat_limiter, feedback_limiter, get_current_user, require_owned_thread,
    throttle, upload_limiter,
)
from app.guardrails import guardrail_policy
from app.guardrails.guard_llm import classify_input
from app.guardrails.pii import apply_policy as apply_pii_policy
from app.guardrails.security import POLICY_BLOCK_MESSAGE, check_user_input
from app.models import ChatRequest, FeedbackRequest, RenameRequest
from app.rag.document_processor import (
    build_org_retriever, delete_thread_cache, delete_thread_summary,
    get_embedding_mode_label, save_thread_summary,
)
from app.stores import audit_store, feedback_store, org_store
from app.stores.session_store import (
    add_message, create_thread, delete_thread, get_message, get_messages,
    list_threads, rename_thread,
)
from app.core.graph import delete_thread_state

import re as _re

router = APIRouter(prefix="/threads", tags=["threads"])

STREAM_NODES = {"generate", "regenerate"}
_RATINGS = {"up": 1, "down": -1}

TOPIC_BLOCK_MESSAGE = "This assistant isn't set up to answer questions about that topic."
PII_QUESTION_BLOCK_MESSAGE = (
    "Please don't include personal or financial identifiers in your question. "
    "Remove them and ask again."
)


# ------------------------------------------------------------- thread CRUD
@router.post("")
def new_thread(user: dict = Depends(get_current_user)):
    return {"thread_id": create_thread(user["user_id"])}


@router.get("")
def my_threads(user: dict = Depends(get_current_user)):
    return list_threads(user["user_id"])


@router.get("/{thread_id}/messages")
def thread_messages(thread_id: str, user: dict = Depends(get_current_user)):
    require_owned_thread(thread_id, user["user_id"])
    messages = get_messages(thread_id)
    mine = feedback_store.ratings_for_thread(thread_id, user["user_id"])
    for msg in messages:
        if msg["role"] == "assistant":
            msg["feedback"] = mine.get(msg["id"])
    return messages


@router.patch("/{thread_id}")
def rename(thread_id: str, body: RenameRequest, user: dict = Depends(get_current_user)):
    require_owned_thread(thread_id, user["user_id"])
    rename_thread(thread_id, body.title)
    return {"message": "Renamed"}


@router.delete("/{thread_id}")
def remove_thread(thread_id: str, user: dict = Depends(get_current_user)):
    require_owned_thread(thread_id, user["user_id"])
    delete_thread(thread_id)
    org_store.delete_thread_collections(thread_id)
    feedback_store.delete_thread_feedback(thread_id)

    for cleanup in (delete_thread_cache, delete_thread_summary, delete_thread_state):
        try:
            cleanup(thread_id)
        except Exception:
            from app.config import logger
            logger.exception(
                "Cleanup %s failed for thread %s",
                cleanup.__name__,
                thread_id,
            )

    return {"message": "Deleted"}


# ------------------------------------------------------------- personal upload
@router.post("/{thread_id}/upload")
def upload_to_personal(
    thread_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    force_offline: bool = Form(False),  # ignored; kept for old frontend
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    chunk_overlap: int = Form(DEFAULT_CHUNK_OVERLAP),
    allow_flagged: bool = Form(False),
    user: dict = Depends(get_current_user),
):
    """Sidebar upload. Files land in the user's PERSONAL collection, not this thread."""
    require_owned_thread(thread_id, user["user_id"])
    throttle(upload_limiter, user["user_id"])

    try:
        org_store.assert_embedding_mode(get_embedding_mode_label())
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    # Personal collections belong to the caller, so admin override doesn't apply here:
    # they own the docs, they own the consequences. (See ingest.can_override.)
    coll = org_store.get_or_create_personal_collection(user)

    from app.routers.collections import _ingest_many  # avoid duplicating the loop

    return _ingest_many(
        user,
        coll,
        files,
        chunk_size,
        chunk_overlap,
        bool(allow_flagged),
        _ip(request),
    )


# ------------------------------------------------------------- finalize
@router.post("/{thread_id}/finalize")
def finalize_thread(thread_id: str, user: dict = Depends(get_current_user)):
    require_owned_thread(thread_id, user["user_id"])
    snap = compiled_graph.get_state({"configurable": {"thread_id": thread_id}})
    values = snap.values or {}

    title, summary = summarize_thread_for_storage(
        values.get("raw_turns", []),
        values.get("running_summary", ""),
    )
    if not summary:
        return {"saved": False}

    save_thread_summary(
        thread_id,
        title,
        summary,
        user["user_id"],
        collection_ids=org_store.get_thread_collections(thread_id),
        doc_ids=org_store.get_thread_documents(thread_id),
    )
    return {"saved": True, "title": title}


# ------------------------------------------------------------- chat (blocking)
def _prepare_run(thread_id: str, user: dict, body: ChatRequest, policy: dict):
    allowed = org_store.accessible_collection_ids(user)

    if body.collection_ids:
        wanted = set(body.collection_ids)
        allowed = [c for c in allowed if c in wanted]

    retriever = None
    if org_store.has_active_documents(allowed):
        try:
            org_store.assert_embedding_mode(get_embedding_mode_label())
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            )

        retriever = build_org_retriever(
            user["org_id"],
            allowed,
            k=RERANK_CANDIDATE_K,
        )

    graph_input = {
        "question": body.question,
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

    # LangSmith uses these top-level fields to organize and filter runs.
    # Do not put questions, prompts, or document contents in metadata.
    config = {
        "run_name": "rag.chat",
        "tags": ["agentic-rag", "chat", "authenticated"],
        "metadata": {
            "pipeline": "rag",
            "actor_type": "authenticated",
        },
        "configurable": {
            "thread_id": thread_id,
            "user_id": user["user_id"],
            "retriever": retriever,
            "top_k": body.top_k,
            "force_offline": False,
            "guardrails": policy,
            "scope": (
                org_store.access_scope(allowed)
                + ":"
                + guardrail_policy.fingerprint(policy)
            ),
        },
    }

    return graph_input, config


def _policy_gate(
    user: dict,
    body: ChatRequest,
    ip: str,
    policy: dict,
) -> Optional[tuple[str, str]]:
    throttle(chat_limiter, user["user_id"])

    allowed, reason, flags = check_user_input(
        body.question,
        max_chars=policy["max_question_chars"],
        block_extraction=policy["block_prompt_extraction"],
    )

    if flags:
        audit_store.log(
            "question_flagged",
            user,
            ip=ip,
            detail={
                "rules": [f.rule for f in flags],
                "question": body.question[:200],
            },
        )

    if not allowed:
        audit_store.log(
            "question_blocked",
            user,
            ip=ip,
            detail={
                "reason": reason,
                "question": body.question[:200],
            },
        )
        return POLICY_BLOCK_MESSAGE, reason

    lowered = body.question.lower()
    for topic in policy["blocked_topics"]:
        if _re.search(r"(?<!\w)" + _re.escape(topic) + r"(?!\w)", lowered):
            audit_store.log(
                "question_blocked",
                user,
                ip=ip,
                detail={"reason": "blocked topic", "topic": topic},
            )
            return TOPIC_BLOCK_MESSAGE, f"blocked topic: {topic}"

    masked_q, masked, blocked = apply_pii_policy(body.question, policy["pii"])
    if blocked:
        audit_store.log(
            "question_blocked",
            user,
            ip=ip,
            detail={"reason": "pii", "kinds": blocked},
        )
        return PII_QUESTION_BLOCK_MESSAGE, "pii: " + ", ".join(blocked)

    if masked:
        body.question = masked_q
        audit_store.log(
            "pii_masked_in_question",
            user,
            ip=ip,
            detail={"kinds": masked},
        )

    if flags and policy["llm_second_opinion"]:
        verdict = classify_input(body.question, small_llm)
        audit_store.log(
            "llm_guard_verdict",
            user,
            ip=ip,
            detail={
                "verdict": verdict,
                "rules": [f.rule for f in flags],
            },
        )
        if verdict == "ATTACK":
            return POLICY_BLOCK_MESSAGE, "llm guard: attack"

    return None


def _after_chat(thread_id: str, user: dict, state: dict, ip: str) -> None:
    org_store.record_thread_collections(
        thread_id,
        [s.get("collection_id") for s in state.get("sources", [])],
    )
    org_store.record_thread_documents(
        thread_id,
        [s.get("doc_id") for s in state.get("sources", [])],
    )

    for event in state.get("security_events", []):
        audit_store.log(
            "security_event",
            user,
            target=thread_id,
            ip=ip,
            detail=event,
        )


@router.post("/{thread_id}/chat")
def chat(
    thread_id: str,
    body: ChatRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    require_owned_thread(thread_id, user["user_id"])
    ip = _ip(request)
    policy = guardrail_policy.get_policy(user["org_id"])

    blocked = _policy_gate(user, body, ip, policy)
    if blocked:
        add_message(thread_id, "user", body.question)
        trace = [f"Policy gate: blocked ({blocked[1]})"]
        message_id = add_message(thread_id, "assistant", blocked[0], trace, [])
        return {
            "answer": blocked[0],
            "sources": [],
            "trace": trace,
            "message_id": message_id,
        }

    add_message(thread_id, "user", body.question)
    graph_input, config = _prepare_run(thread_id, user, body, policy)
    final_state = compiled_graph.invoke(graph_input, config=config)

    answer = final_state.get("answer", "")
    sources = final_state.get("sources", [])
    trace = final_state.get("trace", [])

    message_id = add_message(thread_id, "assistant", answer, trace, sources)
    _after_chat(thread_id, user, final_state, ip)

    return {
        "answer": answer,
        "sources": sources,
        "trace": trace,
        "message_id": message_id,
    }


# ------------------------------------------------------------- chat (streaming)
@router.post("/{thread_id}/chat/stream")
def chat_stream(
    thread_id: str,
    body: ChatRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    require_owned_thread(thread_id, user["user_id"])
    ip = _ip(request)
    policy = guardrail_policy.get_policy(user["org_id"])

    blocked = _policy_gate(user, body, ip, policy)
    if blocked:
        add_message(thread_id, "user", body.question)
        trace = [f"Policy gate: blocked ({blocked[1]})"]
        message_id = add_message(thread_id, "assistant", blocked[0], trace, [])
        payload = json.dumps({
            "type": "final",
            "answer": blocked[0],
            "sources": [],
            "trace": trace,
            "message_id": message_id,
        }) + "\n"

        return StreamingResponse(
            iter([payload]),
            media_type="application/x-ndjson",
        )

    add_message(thread_id, "user", body.question)
    graph_input, config = _prepare_run(thread_id, user, body, policy)

    def emit(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    def event_stream():
        state: dict = {}

        try:
            for mode, payload in compiled_graph.stream(
                graph_input,
                config=config,
                stream_mode=["updates", "messages"],
            ):
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

            answer = state.get("answer", "")
            sources = state.get("sources", [])
            trace = state.get("trace", [])

            message_id = add_message(
                thread_id, "assistant", answer, trace, sources
            )
            _after_chat(thread_id, user, state, ip)

            # Always replace streamed text with the final graph answer.
            yield emit({
                "type": "final",
                "answer": answer,
                "sources": sources,
                "trace": trace,
                "message_id": message_id,
            })

        except Exception:
            from app.config import logger
            logger.exception("Streaming chat failed")
            yield emit({
                "type": "error",
                "detail": "Streaming chat failed. Please try again.",
            })

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )


# ------------------------------------------------------------- feedback
@router.put("/{thread_id}/messages/{message_id}/feedback")
def rate_message(
    thread_id: str,
    message_id: int,
    body: FeedbackRequest,
    user: dict = Depends(get_current_user),
):
    require_owned_thread(thread_id, user["user_id"])

    if body.reason and body.reason not in feedback_store.REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"reason must be one of: {', '.join(feedback_store.REASONS)}",
        )

    msg = get_message(thread_id, message_id)
    if not msg or msg["role"] != "assistant":
        raise HTTPException(status_code=404, detail="Answer not found")

    throttle(feedback_limiter, user["user_id"])
    feedback_store.set_feedback(
        message_id,
        thread_id,
        user["user_id"],
        user["org_id"],
        _RATINGS[body.rating],
        body.reason,
        body.comment.strip()[:1000],
    )
    return {"message": "Thanks for the feedback"}


@router.delete("/{thread_id}/messages/{message_id}/feedback")
def clear_rating(
    thread_id: str,
    message_id: int,
    user: dict = Depends(get_current_user),
):
    require_owned_thread(thread_id, user["user_id"])

    if not feedback_store.remove_feedback(message_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="No rating to remove")

    return {"message": "Removed"}