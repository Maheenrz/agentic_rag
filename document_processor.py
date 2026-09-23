"""
document_processor.py
----------------------
Three persistent Chroma-backed stores, all sharing one on-disk client
(CHROMA_PERSIST_DIR):

1. Per-thread document store  ("thread_<id>_docs")
   Uploaded files for one chat thread. Append-only: uploading a second
   file adds to the same collection instead of replacing it, which is
   what gives multi-document support without any extra plumbing.

2. Conversation memory store  ("conversation_memory", global)
   One entry per finalized thread (its summary), searched when a question
   is asking to recall a PAST conversation rather than the current
   thread's documents.

3. Semantic cache store  ("query_cache", scoped per-thread)
   (question -> answer) pairs. Scoped by thread_id at both write and read
   time, because the same question text can have a different correct
   answer depending on which documents are loaded in a given thread.
"""

import io
import json
import os
import time
from functools import lru_cache
from typing import Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from sklearn.feature_extraction.text import HashingVectorizer

from config import (
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


class LocalHashingEmbeddings:
    """
    Offline fallback embedding function used ONLY when the HuggingFace Hub
    is unreachable or rate-limiting us. Makes no network call at all --
    hashes tokens into a fixed-size vector space. Meaningfully less
    accurate than a real sentence-transformer model (no semantic
    understanding, just weighted token overlap), so it's a temporary
    degradation, not a permanent swap. Implements the same
    embed_documents/embed_query interface Chroma expects.
    """

    def __init__(self, n_features: int = 384):
        self._vectorizer = HashingVectorizer(n_features=n_features, norm="l2")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._vectorizer.transform(texts).toarray().tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._vectorizer.transform([text]).toarray()[0].tolist()


@lru_cache(maxsize=2)
def get_embedding_model(force_offline: bool = False):
    """
    Load the sentence-embedding model once per process and cache it
    (separately per force_offline value). Skips HuggingFace entirely when
    force_offline is True or FORCE_OFFLINE_EMBEDDINGS is set.
    """
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
# Loading & chunking (unchanged from before)
# --------------------------------------------------------------------------
def load_uploaded_documents(uploaded_files) -> list[Document]:
    documents: list[Document] = []

    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        file_bytes = uploaded_file.getvalue()
        extension = os.path.splitext(file_name)[1].lower()

        logger.info("Processing uploaded file: %s (%s)", file_name, extension)

        if extension == ".pdf":
            reader = PdfReader(io.BytesIO(file_bytes))
            pages_with_text = 0
            for page_number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    pages_with_text += 1
                    documents.append(Document(
                        page_content=text,
                        metadata={"source": file_name, "page": page_number},
                    ))
            logger.info("%s: extracted text from %d/%d pages", file_name, pages_with_text, len(reader.pages))

        elif extension == ".txt":
            text = file_bytes.decode("utf-8")
            if text.strip():
                documents.append(Document(
                    page_content=text,
                    metadata={"source": file_name, "page": 1, "file_type": "text"},
                ))
        else:
            logger.warning("Rejected unsupported file type: %s", extension)
            raise ValueError(f"Unsupported file type: {extension}")

    if not documents:
        logger.error("No extractable text found in any uploaded file")
        raise ValueError("No documents found in the uploaded files")

    return documents


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


def format_context(documents: list[Document]) -> str:
    blocks = []
    for index, doc in enumerate(documents, start=1):
        source = doc.metadata.get("source", "unknown")
        page = doc.metadata.get("page", "?")
        blocks.append(f"[Source {index}: {source}, Page {page}]\n{doc.page_content}")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# 1. Per-thread document store (persistent, append-only -> multi-doc)
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
    """Embeds and appends chunks to this thread's persistent collection."""
    vector_store = get_thread_vector_store(thread_id, force_offline=force_offline)
    vector_store.add_documents(documents=chunks)
    logger.info("Thread %s: indexed %d chunks (append)", thread_id, len(chunks))
    return vector_store


def thread_document_count(thread_id: str, force_offline: bool = False) -> int:
    """How many chunks are already indexed for this thread (0 if none yet)."""
    try:
        store = get_thread_vector_store(thread_id, force_offline=force_offline)
        return store._collection.count()
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 2. Conversation memory store (global, cross-thread recall)
# --------------------------------------------------------------------------
def get_memory_store(force_offline: bool = False) -> Chroma:
    return Chroma(
        collection_name=MEMORY_COLLECTION_NAME,
        embedding_function=get_embedding_model(force_offline=force_offline),
        persist_directory=CHROMA_PERSIST_DIR,
    )


def save_thread_summary(
    thread_id: str, title: str, summary: str, force_offline: bool = False
) -> None:
    """Called when a thread is finalized (New chat clicked, or app closed)."""
    if not summary.strip():
        return
    store = get_memory_store(force_offline=force_offline)
    store.add_documents([Document(
        page_content=summary,
        metadata={"thread_id": thread_id, "title": title, "timestamp": time.time()},
    )])
    logger.info("Saved thread summary to conversation memory: %s (%s)", thread_id, title)


def search_memory(query: str, k: int = 4, force_offline: bool = False) -> list[Document]:
    """Searched across ALL threads on purpose -- this is cross-session recall."""
    store = get_memory_store(force_offline=force_offline)
    if store._collection.count() == 0:
        return []
    return store.similarity_search(query, k=k)


# --------------------------------------------------------------------------
# 3. Semantic cache (thread-scoped: same question, same doc set only)
# --------------------------------------------------------------------------
def get_cache_store(force_offline: bool = False) -> Chroma:
    return Chroma(
        collection_name=CACHE_COLLECTION_NAME,
        embedding_function=get_embedding_model(force_offline=force_offline),
        persist_directory=CHROMA_PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
    )


def check_cache(question: str, thread_id: str, force_offline: bool = False) -> Optional[dict]:
    """
    Returns a cached {answer, sources, similarity} dict if a near-duplicate
    question was already answered IN THIS THREAD, else None. Scoped by
    thread_id so an answer from one document set never leaks into a
    different thread's different documents.
    """
    store = get_cache_store(force_offline=force_offline)
    if store._collection.count() == 0:
        return None

    try:
        results = store.similarity_search_with_score(
            question, k=1, filter={"thread_id": thread_id}
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
    question: str, answer: str, sources: list[dict], thread_id: str, force_offline: bool = False
) -> None:
    store = get_cache_store(force_offline=force_offline)
    store.add_documents([Document(
        page_content=question,
        metadata={
            "thread_id": thread_id,
            "answer": answer,
            "sources_json": json.dumps(sources),
            "timestamp": time.time(),
        },
    )])