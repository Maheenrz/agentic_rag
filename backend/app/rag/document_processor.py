"""
document_processor.py

PGVector-backed stores and retrieval for organisation documents.

Hybrid retrieval combines vector search and BM25 keyword search using
Reciprocal Rank Fusion (RRF). Both paths are restricted to active documents
in the user's authorised organisation collections.
"""

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from langchain_postgres.vectorstores import PGVector
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import HashingVectorizer

from app.stores.org_store import access_scope, active_document_ids
from app.config import (
    CACHE_COLLECTION_NAME,
    CACHE_SIMILARITY_THRESHOLD,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    EMBEDDING_MODEL,
    FORCE_OFFLINE_EMBEDDINGS,
    MEMORY_COLLECTION_NAME,
    DATABASE_URL,
    logger,
)
from app.db import engine


HYBRID_SEARCH = os.getenv("HYBRID_SEARCH", "true").lower() == "true"
RRF_K = 60
BM25_CACHE_SIZE = 32
_TOKEN_RE = re.compile(r"\w+")
_BM25_CACHE: OrderedDict[str, tuple] = OrderedDict()


class LocalHashingEmbeddings:
    """Low-quality offline embeddings, used only when explicitly configured."""

    def __init__(self, n_features: int = 384):
        self._vectorizer = HashingVectorizer(
            n_features=n_features,
            norm="l2",
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._vectorizer.transform([text]).toarray()[0].tolist()


@lru_cache(maxsize=2)
def get_embedding_model(force_offline: bool = False):
    """Load and cache the embedding model; do not silently downgrade quality."""
    if force_offline or FORCE_OFFLINE_EMBEDDINGS:
        logger.warning(
            "Offline hashing embeddings requested; retrieval quality is lower."
        )
        return LocalHashingEmbeddings()

    logger.info("Loading embedding model: %s", EMBEDDING_MODEL)
    try:
        return HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    except Exception as exc:
        raise RuntimeError(
            f"Embedding model '{EMBEDDING_MODEL}' failed to load: {exc}. "
            "Fix the model path or network; do not fall back silently."
        ) from exc


@lru_cache(maxsize=256)
def _pgvector(collection_name: str, force_offline: bool = False) -> PGVector:
    """Reuse vector-store objects and the application's shared SQLAlchemy engine."""
    if engine is None:
        raise RuntimeError(
            "Database engine is unavailable. Check DATABASE_URL and SQLALCHEMY_URL."
        )

    return PGVector(
        embeddings=get_embedding_model(force_offline=force_offline),
        collection_name=collection_name,
        connection=engine,
        use_jsonb=True,
        create_extension=False,
    )


def get_embedding_mode_label(force_offline: bool = False) -> str:
    model = get_embedding_model(force_offline=force_offline)
    if isinstance(model, LocalHashingEmbeddings):
        return "Offline Fallback"
    return "HuggingFace MiniLM-L6"


def _get_pg_connection():
    """Acquire a direct psycopg connection for metadata operations."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg.connect(
        DATABASE_URL,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=None,
    )


# --------------------------------------------------------------------------
# Chunking and formatting
# --------------------------------------------------------------------------

def chunk_documents(
    documents: list[Document],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Document]:
    if chunk_size <= 0:
        raise ValueError("Chunk size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("Chunk overlap must be smaller than chunk size")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_start_index=True,
    )
    chunks = splitter.split_documents(documents)

    logger.info(
        "Split %d documents into %d chunks (size=%d, overlap=%d)",
        len(documents),
        len(chunks),
        chunk_size,
        chunk_overlap,
    )
    return chunks


def location_label(metadata: dict) -> str:
    """Return a user-friendly source location."""
    source = metadata.get("source", "unknown")
    section = metadata.get("section")

    if section:
        if section.startswith("Sheet:"):
            return f"{source}, {section}"
        return f'{source}, Section "{section}"'

    return f"{source}, Page {metadata.get('page', '?')}"


def format_context(documents: list[Document]) -> str:
    blocks = []
    for index, doc in enumerate(documents, start=1):
        blocks.append(
            f"[Source {index}: {location_label(doc.metadata)}]\n"
            f"{doc.page_content}"
        )
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# 1. Per-thread document store
# --------------------------------------------------------------------------

def get_thread_vector_store(
    thread_id: str,
    force_offline: bool = False,
) -> PGVector:
    return _pgvector(f"thread_{thread_id}_docs", force_offline)


def add_documents_to_thread(
    thread_id: str,
    chunks: list[Document],
    force_offline: bool = False,
) -> PGVector:
    vector_store = get_thread_vector_store(
        thread_id,
        force_offline=force_offline,
    )
    vector_store.add_documents(documents=chunks)

    logger.info(
        "Thread %s: indexed %d chunks (append)",
        thread_id,
        len(chunks),
    )
    return vector_store


def delete_thread_collection(
    thread_id: str,
    force_offline: bool = False,
) -> None:
    store = get_thread_vector_store(
        thread_id,
        force_offline=force_offline,
    )
    store.delete_collection()
    _pgvector.cache_clear()


def thread_document_count(
    thread_id: str,
    force_offline: bool = False,
) -> int:
    try:
        collection_name = f"thread_{thread_id}_docs"

        with _get_pg_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(e.id) AS count
                    FROM langchain_pg_embedding e
                    JOIN langchain_pg_collection c
                      ON e.collection_id = c.uuid
                    WHERE c.name = %s
                    """,
                    (collection_name,),
                )
                result = cur.fetchone()
                return result["count"] if result else 0

    except Exception:
        logger.exception("Could not count thread documents")
        return 0


# --------------------------------------------------------------------------
# 2. Conversation memory store
# --------------------------------------------------------------------------

def get_memory_store(force_offline: bool = False) -> PGVector:
    return _pgvector(MEMORY_COLLECTION_NAME, force_offline)


def save_thread_summary(
    thread_id: str,
    title: str,
    summary: str,
    user_id: str,
    force_offline: bool = False,
    collection_ids: Optional[list[str]] = None,
    doc_ids: Optional[list[str]] = None,
) -> None:
    """Save a summary with provenance so later deletion/access changes can purge it."""
    if not summary.strip():
        return

    store = get_memory_store(force_offline=force_offline)
    store.add_documents(
        [
            Document(
                page_content=summary,
                metadata={
                    "thread_id": thread_id,
                    "title": title,
                    "user_id": user_id,
                    "timestamp": time.time(),
                    "collection_ids": ",".join(sorted(collection_ids or [])),
                    "doc_ids": ",".join(sorted(doc_ids or [])),
                },
            )
        ],
        ids=[thread_id],
    )

    logger.info(
        "Saved thread summary to conversation memory: %s (%s)",
        thread_id,
        title,
    )


def search_memory(
    query: str,
    user_id: str,
    k: int = 4,
    force_offline: bool = False,
) -> list[Document]:
    store = get_memory_store(force_offline=force_offline)
    return store.similarity_search(
        query,
        k=k,
        filter={"user_id": user_id},
    )


# --------------------------------------------------------------------------
# 3. Semantic cache
# --------------------------------------------------------------------------

def get_cache_store(force_offline: bool = False) -> PGVector:
    return _pgvector(CACHE_COLLECTION_NAME, force_offline)


def check_cache(
    question: str,
    thread_id: str,
    force_offline: bool = False,
    scope: str = "",
) -> Optional[dict]:
    """Return a similar cached answer scoped to this thread and access scope."""
    store = get_cache_store(force_offline=force_offline)

    try:
        results = store.similarity_search_with_score(
            question,
            k=1,
            filter={"thread_id": thread_id, "scope": scope},
        )
    except Exception:
        logger.exception("Cache lookup failed")
        return None

    if not results:
        return None

    doc, distance = results[0]
    similarity = 1 - distance

    if similarity >= CACHE_SIMILARITY_THRESHOLD:
        logger.info(
            "Cache hit (similarity=%.3f, thread=%s)",
            similarity,
            thread_id,
        )
        try:
            cached_sources = json.loads(
                doc.metadata.get("sources_json", "[]")
            )
        except (TypeError, json.JSONDecodeError):
            logger.warning("Ignoring malformed cached sources for thread %s", thread_id)
            return None

        return {
            "answer": doc.metadata.get("answer", ""),
            "sources": cached_sources,
            "similarity": round(similarity, 3),
        }

    return None


def write_cache(
    question: str,
    answer: str,
    sources: list[dict],
    thread_id: str,
    force_offline: bool = False,
    scope: str = "",
) -> None:
    store = get_cache_store(force_offline=force_offline)
    store.add_documents(
        [
            Document(
                page_content=question,
                metadata={
                    "thread_id": thread_id,
                    "scope": scope,
                    "answer": answer,
                    "sources_json": json.dumps(sources),
                    "timestamp": time.time(),
                },
            )
        ]
    )


# --------------------------------------------------------------------------
# 4. Organisation document store
# --------------------------------------------------------------------------

ORG_COLLECTION_NAME = "org_docs"


def get_org_store() -> PGVector:
    return _pgvector(ORG_COLLECTION_NAME)


def add_org_chunks(chunks: list[Document], batch_size: int = 512) -> None:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    if not chunks:
        return

    store = get_org_store()

    for start in range(0, len(chunks), batch_size):
        store.add_documents(chunks[start : start + batch_size])


# --------------------------------------------------------------------------
# 5. Hybrid retrieval
# --------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _chunk_key(doc: Document) -> str:
    """Stable identity for deduplicating chunks found by both retrieval methods."""
    raw = f"{doc.metadata.get('doc_id', '')}|{doc.page_content}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_corpus(
    org_id: str,
    collection_ids: list[str],
) -> list[Document]:
    """
    Load only chunks belonging to active document rows.

    The SQL join is a second safety barrier: stale vector rows are not eligible
    for BM25 merely because asynchronous vector cleanup failed.
    """
    if not collection_ids:
        return []

    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.document, e.cmetadata
                FROM langchain_pg_embedding e
                JOIN langchain_pg_collection c
                  ON e.collection_id = c.uuid
                JOIN documents d
                  ON d.doc_id = e.cmetadata->>'doc_id'
                WHERE c.name = %s
                  AND e.cmetadata->>'org_id' = %s
                  AND e.cmetadata->>'collection_id' = ANY(%s)
                  AND d.org_id = %s
                  AND d.collection_id = ANY(%s)
                  AND d.status = 'active'
                """,
                (
                    ORG_COLLECTION_NAME,
                    org_id,
                    collection_ids,
                    org_id,
                    collection_ids,
                ),
            )
            rows = cur.fetchall()

    return [
        Document(
            page_content=row["document"],
            metadata=row["cmetadata"] or {},
        )
        for row in rows
    ]


def _keyword_index(
    org_id: str,
    collection_ids: list[str],
):
    """Cache a BM25 index by collection data versions and organisation."""
    scope = f"{org_id}:{access_scope(collection_ids)}"
    cached = _BM25_CACHE.get(scope)

    if cached is not None:
        _BM25_CACHE.move_to_end(scope)
        return cached

    docs = _load_corpus(org_id, collection_ids)
    tokenised = [_tokenize(doc.page_content) for doc in docs]
    index = BM25Okapi(tokenised) if any(tokenised) else None

    result = (docs, index)
    _BM25_CACHE[scope] = result

    if len(_BM25_CACHE) > BM25_CACHE_SIZE:
        _BM25_CACHE.popitem(last=False)

    return result


def _reciprocal_rank_fusion(
    ranked_lists: list[list[Document]],
    k: int,
) -> list[Document]:
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
    """LangChain-compatible retriever restricted to active, authorised documents."""

    def __init__(
        self,
        org_id: str,
        collection_ids: list[str],
        k: int,
    ):
        self.org_id = org_id
        # Deduplicate without changing the caller's collection ordering.
        self.collection_ids = list(dict.fromkeys(collection_ids))
        self.k = max(1, int(k))

    def invoke(self, query: str) -> list[Document]:
        if not self.collection_ids:
            return []

        started = time.perf_counter()

        # If this database query fails, propagate the error rather than
        # falling back to an unrestricted vector search.
        active_ids = set(
            active_document_ids(
                self.collection_ids,
                org_id=self.org_id,
            )
        )

        if not active_ids:
            logger.info(
                "RAG retrieval: no active documents org=%s collections=%d",
                self.org_id,
                len(self.collection_ids),
            )
            return []

        vector_filter = {
            "org_id": self.org_id,
            "collection_id": {"$in": self.collection_ids},
            "doc_id": {"$in": sorted(active_ids)},
        }

        vector_started = time.perf_counter()
        vector_hits = get_org_store().similarity_search(
            query,
            k=self.k,
            filter=vector_filter,
        )
        vector_ms = round(
            (time.perf_counter() - vector_started) * 1000,
            2,
        )

        # Recheck after vector search. This narrows the race window in which
        # a concurrent deletion or replacement could retire a document while
        # the vector query is running.
        current_active_ids = set(
            active_document_ids(
                self.collection_ids,
                org_id=self.org_id,
            )
        )

        vector_hits = [
            doc for doc in vector_hits
            if doc.metadata.get("doc_id") in current_active_ids
            and doc.metadata.get("org_id") == self.org_id
            and doc.metadata.get("collection_id") in self.collection_ids
        ]

        if not vector_hits:
            logger.info(
                "Vector search returned no active-document hits org=%s",
                self.org_id,
            )

        if not HYBRID_SEARCH:
            logger.info(
                "Vector retrieval complete hits=%d latency_ms=%s",
                len(vector_hits),
                vector_ms,
            )
            return vector_hits

        try:
            docs, index = _keyword_index(
                self.org_id,
                self.collection_ids,
            )
        except Exception:
            # Vector hits remain restricted to currently active IDs.
            logger.exception(
                "BM25 corpus/index load failed; using filtered vector hits"
            )
            return vector_hits

        # A cached BM25 index is keyed by collection data_version. Filter its
        # documents again in case a lifecycle transition happened concurrently.
        docs = [
            doc for doc in docs
            if doc.metadata.get("doc_id") in current_active_ids
            and doc.metadata.get("org_id") == self.org_id
            and doc.metadata.get("collection_id") in self.collection_ids
        ]

        if not docs:
            return vector_hits

        # Rebuild the small in-memory index if stale entries were filtered.
        tokenised = [_tokenize(doc.page_content) for doc in docs]
        index = BM25Okapi(tokenised) if any(tokenised) else None

        if index is None:
            return vector_hits

        scores = index.get_scores(_tokenize(query))
        ranked = sorted(
            range(len(docs)),
            key=lambda i: scores[i],
            reverse=True,
        )
        keyword_hits = [
            docs[i]
            for i in ranked[: self.k]
            if scores[i] > 0
        ]

        merged = _reciprocal_rank_fusion(
            [vector_hits, keyword_hits],
            self.k,
        )

        logger.info(
            "Hybrid retrieval complete vector_hits=%d keyword_hits=%d "
            "merged_hits=%d vector_ms=%s total_ms=%s",
            len(vector_hits),
            len(keyword_hits),
            len(merged),
            vector_ms,
            round((time.perf_counter() - started) * 1000, 2),
        )
        return merged


def build_org_retriever(
    org_id: str,
    collection_ids: list[str],
    k: int,
) -> HybridOrgRetriever:
    return HybridOrgRetriever(org_id, collection_ids, k)


# --------------------------------------------------------------------------
# 6. Deletion and cache cleanup
# --------------------------------------------------------------------------

def delete_doc_chunks(doc_id: str) -> int:
    """Remove every vector chunk for a document. Idempotent: deleting a
    document with no remaining chunks (already cleaned up, or it never
    had any) is a no-op, not an error. Returns the number of rows removed
    so callers can log/verify what actually happened."""
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM langchain_pg_embedding e
                USING langchain_pg_collection c
                WHERE e.collection_id = c.uuid
                  AND c.name = %s
                  AND e.cmetadata->>'doc_id' = %s
                """,
                (ORG_COLLECTION_NAME, doc_id),
            )
            return cur.rowcount


def delete_collection_chunks(collection_id: str) -> int:
    """Remove every vector belonging to a collection. Idempotent; see
    delete_doc_chunks(). Returns the number of rows removed."""
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM langchain_pg_embedding e
                USING langchain_pg_collection c
                WHERE e.collection_id = c.uuid
                  AND c.name = %s
                  AND e.cmetadata->>'collection_id' = %s
                """,
                (ORG_COLLECTION_NAME, collection_id),
            )
            return cur.rowcount


def count_doc_chunks(doc_id: str) -> int:
    """Count remaining vector chunks for a document. Used to VERIFY cleanup
    after delete_doc_chunks() -- a deletion is only considered complete once
    this returns 0, not merely because the delete statement ran without
    raising. Scoped by the exact doc_id stored in chunk metadata, so it
    cannot match an unrelated document that happens to share a filename."""
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(e.id) AS count
                FROM langchain_pg_embedding e
                JOIN langchain_pg_collection c ON e.collection_id = c.uuid
                WHERE c.name = %s AND e.cmetadata->>'doc_id' = %s
                """,
                (ORG_COLLECTION_NAME, doc_id),
            )
            row = cur.fetchone()
            return row["count"] if row else 0


def count_collection_chunks(collection_id: str) -> int:
    """Count remaining vector chunks for a collection. Used to VERIFY
    delete_collection_chunks(); see count_doc_chunks()."""
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(e.id) AS count
                FROM langchain_pg_embedding e
                JOIN langchain_pg_collection c ON e.collection_id = c.uuid
                WHERE c.name = %s AND e.cmetadata->>'collection_id' = %s
                """,
                (ORG_COLLECTION_NAME, collection_id),
            )
            row = cur.fetchone()
            return row["count"] if row else 0


def delete_thread_cache(thread_id: str) -> None:
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM langchain_pg_embedding e
                USING langchain_pg_collection c
                WHERE e.collection_id = c.uuid
                  AND c.name = %s
                  AND e.cmetadata->>'thread_id' = %s
                """,
                (CACHE_COLLECTION_NAME, thread_id),
            )


def delete_thread_summary(thread_id: str) -> None:
    get_memory_store().delete(ids=[thread_id])


def purge_cache_for_threads(thread_ids: list[str]) -> int:
    """
    Best-effort cleanup of cached Q&A pairs for a set of threads.

    The semantic cache is already soft-invalidated for new requests: scope
    strings embed each collection's data_version (see
    app.stores.org_store.access_scope), which changes whenever a document
    is added, superseded, or deleted, so a stale cache row simply stops
    matching. This additionally removes the physical rows for threads that
    are known (via thread_documents/thread_collections) to have actually
    used the deleted document or collection, so no trace of the deleted
    content lingers in storage. Failures here are logged, not raised --
    this is hygiene on top of an already-correct cache, not something a
    cleanup job should fail or retry over.
    """
    purged = 0
    for thread_id in set(thread_ids):
        try:
            delete_thread_cache(thread_id)
            purged += 1
        except Exception:
            logger.exception(
                "Cache cleanup failed for thread %s", thread_id
            )
    return purged


def _summary_rows_for_users(user_ids: list[str]) -> list[dict]:
    if not user_ids:
        return []
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id, e.cmetadata
                FROM langchain_pg_embedding e
                JOIN langchain_pg_collection c
                  ON e.collection_id = c.uuid
                WHERE c.name = %s
                  AND e.cmetadata->>'user_id' = ANY(%s)
                """,
                (MEMORY_COLLECTION_NAME, user_ids),
            )
            return cur.fetchall()


def _summaries_depending_on_document(
    doc_id: str,
    collection_id: str,
    user_ids: list[str],
) -> list[str]:
    """
    Summary ids that depend on a document, by provenance.

    Summaries with recorded doc_ids are purged only if they actually cite
    this document. Older summaries without doc_ids (written before
    provenance tracking existed) are purged conservatively whenever they
    reference the affected collection at all, since there's no way to
    confirm whether this specific document contributed to them.
    """
    doomed = []
    for row in _summary_rows_for_users(user_ids):
        metadata = row["cmetadata"] or {}

        if "doc_ids" in metadata:
            recorded_doc_ids = {
                item for item in metadata["doc_ids"].split(",") if item
            }
            if doc_id in recorded_doc_ids:
                doomed.append(row["id"])
        else:
            recorded_collections = {
                item
                for item in metadata.get("collection_ids", "").split(",")
                if item
            }
            if collection_id in recorded_collections:
                doomed.append(row["id"])

    return doomed


def purge_summaries_for_document(
    doc_id: str,
    collection_id: str,
    user_ids: list[str],
) -> int:
    """Remove summaries known to depend on a document. See
    _summaries_depending_on_document() for the provenance rule."""
    doomed = _summaries_depending_on_document(doc_id, collection_id, user_ids)

    if doomed:
        get_memory_store().delete(ids=doomed)
        logger.info(
            "Purged %d recall summaries built from document %s",
            len(doomed),
            doc_id,
        )

    return len(doomed)


def remaining_summaries_for_document(
    doc_id: str,
    collection_id: str,
    user_ids: list[str],
) -> int:
    """VERIFY purge_summaries_for_document(): re-run the same provenance
    query and confirm nothing still matches. Used by the cleanup job
    processor before marking a document cleanup complete."""
    return len(_summaries_depending_on_document(doc_id, collection_id, user_ids))


def _summaries_referencing_collections(user_id: str, lost: set) -> list[str]:
    if not lost:
        return []

    doomed = []
    for row in _summary_rows_for_users([user_id]):
        metadata = row["cmetadata"] or {}
        recorded = metadata.get("collection_ids")

        if recorded is None:
            # Legacy summaries have no provenance, so fail closed.
            doomed.append(row["id"])
            continue

        recorded_ids = {item for item in recorded.split(",") if item}
        if lost & recorded_ids:
            doomed.append(row["id"])

    return doomed


def purge_user_summaries(
    user_id: str,
    lost_collection_ids,
) -> int:
    """Purge summaries that reference collections a user can no longer
    access -- used both when ACLs change and (via collection_id in the
    'lost' set) when a collection is deleted outright."""
    doomed = _summaries_referencing_collections(user_id, set(lost_collection_ids))

    if doomed:
        get_memory_store().delete(ids=doomed)
        logger.info(
            "Purged %d recall summaries for user %s after access loss",
            len(doomed),
            user_id,
        )

    return len(doomed)


def remaining_summaries_for_collection(
    collection_id: str,
    user_ids: list[str],
) -> int:
    """VERIFY purge_user_summaries() across every org user for one
    collection id. Used by the cleanup job processor before marking a
    collection cleanup complete."""
    lost = {collection_id}
    return sum(
        len(_summaries_referencing_collections(user_id, lost))
        for user_id in user_ids
    )