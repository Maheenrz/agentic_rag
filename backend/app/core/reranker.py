# app/core/reranker.py
import os
import threading

from flashrank import Ranker, RerankRequest
from langchain_core.documents import Document

from app.config import RERANK_MODEL, logger

# FLASHRANK_CACHE lets the Docker build download the model once, into the image.
#
# The Ranker is constructed lazily (on first actual rerank call) rather
# than at import time. Constructing it loads an ONNX model from disk --
# real I/O that, the first time, also means downloading it -- and this
# module gets imported transitively by anything that imports
# app.core.graph, including code with nothing to do with reranking (e.g.
# tests/test_guardrails.py, which exercises guardrail functions in
# isolation and never reaches the rerank step). Eager construction meant
# any such import would fail outright if the model wasn't already cached,
# regardless of whether that code path ever used reranking. Lazy
# construction does not change production behavior: a real server's
# first chat request reaches rerank_node almost immediately, so the
# model still loads right away in practice, and the Docker build still
# warms FLASHRANK_CACHE ahead of time so that first load is fast. If the
# model genuinely cannot be loaded, the error still surfaces -- loudly,
# via the log line below and a raised exception -- the first time
# reranking is actually attempted; nothing here silently falls back to
# skipping reranking.
_ranker: Ranker | None = None
_ranker_lock = threading.Lock()


def _get_ranker() -> Ranker:
    global _ranker
    if _ranker is None:
        with _ranker_lock:
            if _ranker is None:  # re-check: another thread may have won the race
                cache_dir = os.getenv("FLASHRANK_CACHE", "/tmp")
                try:
                    _ranker = Ranker(model_name=RERANK_MODEL, cache_dir=cache_dir)
                except Exception:
                    logger.exception(
                        "FlashRank reranker model '%s' could not be loaded from "
                        "cache_dir=%s. Reranking is unavailable until this is "
                        "fixed: either run with network access once so FlashRank "
                        "can download it, or pre-populate cache_dir (set "
                        "FLASHRANK_CACHE) with the model files, e.g. as part of "
                        "the Docker build.",
                        RERANK_MODEL, cache_dir,
                    )
                    raise
    return _ranker


def rerank_documents(query: str, documents: list[Document], top_n: int) -> list[Document]:
    if not documents:
        return []

    passages = [
        {"id": i, "text": doc.page_content, "meta": {"index": i}}
        for i, doc in enumerate(documents)
    ]
    request = RerankRequest(query=query, passages=passages)
    results = _get_ranker().rerank(request)  # sorted best-first by score

    return [documents[result["meta"]["index"]] for result in results[:top_n]]