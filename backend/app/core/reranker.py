# app/core/reranker.py
import os

from flashrank import Ranker, RerankRequest
from langchain_core.documents import Document

from app.config import RERANK_MODEL

# FLASHRANK_CACHE lets the Docker build download the model once, into the image.
_ranker = Ranker(model_name=RERANK_MODEL, cache_dir=os.getenv("FLASHRANK_CACHE", "/tmp"))


def rerank_documents(query: str, documents: list[Document], top_n: int) -> list[Document]:
    if not documents:
        return []

    passages = [
        {"id": i, "text": doc.page_content, "meta": {"index": i}}
        for i, doc in enumerate(documents)
    ]
    request = RerankRequest(query=query, passages=passages)
    results = _ranker.rerank(request)  # sorted best-first by score

    return [documents[result["meta"]["index"]] for result in results[:top_n]]