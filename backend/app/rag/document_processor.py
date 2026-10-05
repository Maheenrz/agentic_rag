"""
document_processor.py
----------------------
Chroma-backed stores plus the retriever used for organisation documents.

Organisation document retrieval is HYBRID by default:
  1. vector search (Chroma, MiniLM embeddings) finds chunks with similar meaning
  2. BM25 keyword search (rank_bm25) finds chunks with the exact words asked about
  3. the two lists are merged with Reciprocal Rank Fusion (RRF)

Both searches use the same org_id + collection_id filter, so the keyword side
can never see a chunk the vector side could not see.

Set HYBRID_SEARCH=false in .env to fall back to vector-only search.
"""

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import HashingVectorizer

from app.config import (
    CACHE_COLLECTION_NAME,
    CACHE_SIMILARITY_THRESHOLD,
    CHROMA_PERSIST_DIR,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    EMBEDDING_MODEL,
    FORCE_OFFLINE_EMBEDDINGS,
    MEMORY_COLLECTION_NAME,
    logger,
)
from app.stores.org_store import access_scope

# --------------------------------------------------------------------------
# Hybrid search settings
# --------------------------------------------------------------------------
HYBRID_SEARCH = os.getenv("HYBRID_SEARCH", "true").lower() == "true"
RRF_K = 60                  # standard constant for Reciprocal Rank Fusion
BM25_CACHE_SIZE = 32        # how many org/collection keyword indexes to keep in memory
_TOKEN_RE = re.compile(r"\w+")
_BM25_CACHE: "OrderedDict[str, tuple]" = OrderedDict()


class LocalHashingEmbeddings:
    """
    Offline fallback embedding function used ONLY when the HuggingFace Hub
    is unreachable. Hashes tokens into a fixed-size vector space. Less accurate
    than MiniLM, but makes no network call.
    """

    def __init__(self, n_features: int = 384):
        self._vectorizer = HashingVectorizer(n_features=n_features, norm="l2")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._vectorizer.transform([text]).toarray()[0].tolist()


@lru_cache(maxsize=2)
def get_embedding_model(force_offline: bool = False):
    """Load the sentence-embedding model once per process and cache it."""
    if force_offline or FORCE_OFFLINE_EMBEDDINGS:
        logger.info("Offline mode requested -- skipping HuggingFace, using LocalHashingEmbeddings")
        return LocalHashingEmbeddings()

    logger.info("Loading embedding model: %s", EMBEDDING_MODEL)
    try:
        return HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    except Exception as exc:
        logger.warning(
            "HuggingFace embedding model unavailable (%s). Falling back to "
            "offline LocalHashingEmbeddings.", exc,
        )
        return LocalHashingEmbeddings()


def get_embedding_mode_label(force_offline: bool = False) -> str:
    model = get_embedding_model(force_offline=force_offline)
    if isinstance(model, LocalHashingEmbeddings):
        return "Offline Fallback"
    return "HuggingFace MiniLM-L6"


# --------------------------------------------------------------------------
# Chunking and formatting
# --------------------------------------------------------------------------
def chunk_documents(
    documents: list[Document],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, add_start_index=True,
    )
    chunks = splitter.split_documents(documents)
    logger.info(
        "Split %d documents into %d chunks (size=%d, overlap=%d)",
        len(documents), len(chunks), chunk_size, chunk_overlap,
    )
    return chunks


def location_label(metadata: dict) -> str:
    """'handbook.pdf, Page 7' or 'policy.docx, Section "Leave Policy"'."""
    source = metadata.get("source", "unknown")
    section = metadata.get("section")
    if section:
        return f'{source}, {section}' if section.startswith("Sheet:") else f'{source}, Section "{section}"'
    return f"{source}, Page {metadata.get('page', '?')}"


def format_context(documents: list[Document]) -> str:
    blocks = []
    for index, doc in enumerate(documents, start=1):
        blocks.append(f"[Source {index}: {location_label(doc.metadata)}]\n{doc.page_content}")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# 1. Per-thread document store (persistent, append-only)
# --------------------------------------------------------------------------
def get_thread_vector_store(thread_id: str, force_offline: bool = False) -> Chroma:
    return Chroma(
        collection_name=f"thread_{thread_id}_docs",
        embedding_function=get_embedding_model(force_offline=force_offline),
        persist_directory=CHROMA_PERSIST_DIR,
    )


def add_documents_to_thread(
    thread_id: str, chunks: list[Document], force_offline: bool = False
) -> Chroma:
    vector_store = get_thread_vector_store(thread_id, force_offline=force_offline)
    vector_store.add_documents(documents=chunks)
    logger.info("Thread %s: indexed %d chunks (append)", thread_id, len(chunks))
    return vector_store


def delete_thread_collection(thread_id: str, force_offline: bool = False) -> None:
    store = get_thread_vector_store(thread_id, force_offline=force_offline)
    store.delete_collection()


def thread_document_count(thread_id: str, force_offline: bool = False) -> int:
    try:
        store = get_thread_vector_store(thread_id, force_offline=force_offline)
        return store._collection.count()
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 2. Conversation memory store (global, filtered per user)
# --------------------------------------------------------------------------
def get_memory_store(force_offline: bool = False) -> Chroma:
    return Chroma(
        collection_name=MEMORY_COLLECTION_NAME,
        embedding_function=get_embedding_model(force_offline=force_offline),
        persist_directory=CHROMA_PERSIST_DIR,
    )


def save_thread_summary(
    thread_id: str, title: str, summary: str, user_id: str, force_offline: bool = False,
    collection_ids: Optional[list[str]] = None, doc_ids: Optional[list[str]] = None,
) -> None:
    """Called when a thread is finalized. Records which collections and
    documents the chat drew on, so later revocations or deletions can purge it."""
    if not summary.strip():
        return
    store = get_memory_store(force_offline=force_offline)
    store.add_documents([Document(
        page_content=summary,
        metadata={
            "thread_id": thread_id,
            "title": title,
            "user_id": user_id,
            "timestamp": time.time(),
            "collection_ids": ",".join(sorted(collection_ids or [])),
            "doc_ids": ",".join(sorted(doc_ids or [])),
        },
    )], ids=[thread_id])
    logger.info("Saved thread summary to conversation memory: %s (%s)", thread_id, title)


def search_memory(query: str, user_id: str, k: int = 4, force_offline: bool = False) -> list[Document]:
    store = get_memory_store(force_offline=force_offline)
    if store._collection.count() == 0:
        return []
    return store.similarity_search(query, k=k, filter={"user_id": user_id})


# --------------------------------------------------------------------------
# 3. Semantic cache (scoped per thread and access scope)
# --------------------------------------------------------------------------
def get_cache_store(force_offline: bool = False) -> Chroma:
    return Chroma(
        collection_name=CACHE_COLLECTION_NAME,
        embedding_function=get_embedding_model(force_offline=force_offline),
        persist_directory=CHROMA_PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
    )


def check_cache(question: str, thread_id: str, force_offline: bool = False, scope="") -> Optional[dict]:
    """Returns a cached {answer, sources, similarity} if a near-duplicate question
    was already answered in this thread with the same scope, else None."""
    store = get_cache_store(force_offline=force_offline)
    if store._collection.count() == 0:
        return None

    try:
        results = store.similarity_search_with_score(
            question, k=1, filter={"$and": [{"thread_id": thread_id}, {"scope": scope}]},
        )
    except Exception:
        logger.exception("Cache lookup failed")
        return None

    if not results:
        return None

    doc, distance = results[0]
    similarity = 1 - distance  # cosine space: distance 0 = identical

    if similarity >= CACHE_SIMILARITY_THRESHOLD:
        logger.info("Cache hit (similarity=%.3f, thread=%s)", similarity, thread_id)
        return {
            "answer": doc.metadata.get("answer", ""),
            "sources": json.loads(doc.metadata.get("sources_json", "[]")),
            "similarity": round(similarity, 3),
        }
    return None


def write_cache(
    question: str, answer: str, sources: list[dict], thread_id: str, force_offline: bool = False, scope="",
) -> None:
    store = get_cache_store(force_offline=force_offline)
    store.add_documents([Document(
        page_content=question,
        metadata={
            "thread_id": thread_id,
            "scope": scope,
            "answer": answer,
            "sources_json": json.dumps(sources),
            "timestamp": time.time(),
        },
    )])


# --------------------------------------------------------------------------
# Org document store (shared collections)
# --------------------------------------------------------------------------
ORG_COLLECTION_NAME = "org_docs"


def get_org_store() -> Chroma:
    # No force_offline argument on purpose: the server decides the embedder.
    return Chroma(
        collection_name=ORG_COLLECTION_NAME,
        embedding_function=get_embedding_model(),
        persist_directory=CHROMA_PERSIST_DIR,
    )


def add_org_chunks(chunks: list[Document], batch_size: int = 512) -> None:
    store = get_org_store()
    for i in range(0, len(chunks), batch_size):
        store.add_documents(chunks[i:i + batch_size])


# --------------------------------------------------------------------------
# Hybrid retrieval: vector search + BM25 keyword search, merged with RRF
# --------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _chunk_key(doc: Document) -> str:
    """Stable identity for a chunk, so the same chunk found by both searches is merged."""
    raw = f"{doc.metadata.get('doc_id', '')}|{doc.page_content}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_corpus(org_id: str, collection_ids: list[str]) -> list[Document]:
    """Every chunk the user may read, fetched through the SAME filter the vector search uses.
    Superseded and deleted documents are not here: their chunks were removed from Chroma."""
    flt = {"$and": [{"org_id": org_id}, {"collection_id": {"$in": collection_ids}}]}
    got = get_org_store()._collection.get(where=flt, include=["documents", "metadatas"])
    return [
        Document(page_content=text, metadata=meta or {})
        for text, meta in zip(got.get("documents", []), got.get("metadatas", []))
    ]


def _keyword_index(org_id: str, collection_ids: list[str]):
    """Returns (documents, BM25 index) for this user's readable chunks.
    Cached per access scope. The scope includes each collection's data_version,
    which bumps on every upload, replacement, or deletion, so the index rebuilds
    automatically when documents change."""
    scope = f"{org_id}:{access_scope(collection_ids)}"
    cached = _BM25_CACHE.get(scope)
    if cached is not None:
        _BM25_CACHE.move_to_end(scope)
        return cached

    docs = _load_corpus(org_id, collection_ids)
    tokenised = [_tokenize(d.page_content) for d in docs]
    index = BM25Okapi(tokenised) if any(tokenised) else None

    _BM25_CACHE[scope] = (docs, index)
    if len(_BM25_CACHE) > BM25_CACHE_SIZE:
        _BM25_CACHE.popitem(last=False)
    return docs, index


def _reciprocal_rank_fusion(ranked_lists: list[list[Document]], k: int) -> list[Document]:
    """Each chunk scores 1/(RRF_K + rank) in every list it appears in.
    Chunks ranked well by both searches end up on top."""
    scores: dict[str, float] = {}
    first_seen: dict[str, Document] = {}
    for ranking in ranked_lists:
        for rank, doc in enumerate(ranking, start=1):
            key = _chunk_key(doc)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            first_seen.setdefault(key, doc)
    best = sorted(scores, key=scores.get, reverse=True)[:k]
    return [first_seen[key] for key in best]


class HybridOrgRetriever:
    """Has the same .invoke(query) method as a LangChain retriever, so graph.py's
    retrieve_node works without changes."""

    def __init__(self, org_id: str, collection_ids: list[str], k: int):
        self.org_id = org_id
        self.collection_ids = list(collection_ids)
        self.k = k
        self.flt = {"$and": [{"org_id": org_id}, {"collection_id": {"$in": self.collection_ids}}]}

    def invoke(self, query: str) -> list[Document]:
        vector_hits = get_org_store().similarity_search(query, k=self.k, filter=self.flt)
        if not HYBRID_SEARCH:
            return vector_hits

        docs, index = _keyword_index(self.org_id, self.collection_ids)
        if index is None:
            return vector_hits

        scores = index.get_scores(_tokenize(query))
        ranked = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        keyword_hits = [docs[i] for i in ranked[: self.k] if scores[i] > 0]

        merged = _reciprocal_rank_fusion([vector_hits, keyword_hits], self.k)
        logger.info(
            "Hybrid search: %d vector hits, %d keyword hits, %d after fusion",
            len(vector_hits), len(keyword_hits), len(merged),
        )
        return merged


def build_org_retriever(org_id: str, collection_ids: list[str], k: int) -> HybridOrgRetriever:
    return HybridOrgRetriever(org_id, collection_ids, k)


# --------------------------------------------------------------------------
# Deletion and cache cleanup
# --------------------------------------------------------------------------
def delete_doc_chunks(doc_id: str) -> None:
    get_org_store()._collection.delete(where={"doc_id": doc_id})


def delete_collection_chunks(collection_id: str) -> None:
    """Bulk-removes every chunk belonging to a collection in one call."""
    get_org_store()._collection.delete(where={"collection_id": collection_id})


def delete_thread_cache(thread_id: str) -> None:
    get_cache_store()._collection.delete(where={"thread_id": thread_id})


def delete_thread_summary(thread_id: str) -> None:
    get_memory_store()._collection.delete(ids=[thread_id])


def purge_summaries_for_document(doc_id: str, collection_id: str, user_ids: list[str]) -> int:
    """Called when a document is deleted. Removes recall summaries built from it.
    Summaries saved before document tracking existed have no doc_ids field, so
    they are removed if they touched the same collection (we can't prove they're safe)."""
    store = get_memory_store()
    doomed = []
    for uid in user_ids:
        got = store._collection.get(where={"user_id": uid}, include=["metadatas"])
        for summary_id, meta in zip(got.get("ids", []), got.get("metadatas", [])):
            meta = meta or {}
            if "doc_ids" in meta:
                if doc_id in {d for d in meta["doc_ids"].split(",") if d}:
                    doomed.append(summary_id)
            elif collection_id in {c for c in meta.get("collection_ids", "").split(",") if c}:
                doomed.append(summary_id)
    if doomed:
        store._collection.delete(ids=doomed)
        logger.info("Purged %d recall summaries built from document %s", len(doomed), doc_id)
    return len(doomed)


def purge_user_summaries(user_id: str, lost_collection_ids) -> int:
    """Called when a user LOSES access to collections. Deletes that user's recall
    summaries built from any of those collections. Summaries with no recorded
    collections are purged too, because we can't prove they're safe."""
    lost = set(lost_collection_ids)
    if not lost:
        return 0
    store = get_memory_store()
    got = store._collection.get(where={"user_id": user_id}, include=["metadatas"])
    doomed = []
    for doc_id, meta in zip(got.get("ids", []), got.get("metadatas", [])):
        recorded = (meta or {}).get("collection_ids")
        if recorded is None or lost & {c for c in recorded.split(",") if c}:
            doomed.append(doc_id)
    if doomed:
        store._collection.delete(ids=doomed)
        logger.info("Purged %d recall summaries for user %s after access loss", len(doomed), user_id)
    return len(doomed)