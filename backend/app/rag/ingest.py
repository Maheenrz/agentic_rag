
"""
ingest.py
----------
Document lifecycle for a collection: upload and new version.

Lifecycle:
  1. Parse and scan the uploaded content.
  2. Chunk the content and enforce size limits.
  3. Register a staged document record.
  4. Index the chunks in the vector store.
  5. Atomically publish the new version.
  6. Best-effort cleanup of superseded vectors and summaries.

An identical upload is a no-op. A failed replacement must not supersede
the previous active document.
"""

import hashlib
import os
import time
import uuid

from app.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNKS_PER_DOC,
    logger,
)
from app.guardrails.security import HIGH, scan_text
from app.rag.document_processor import (
    add_org_chunks,
    chunk_documents,
    delete_doc_chunks,
    location_label,
)
from app.rag.parsers import parse_file
from app.services import cleanup as cleanup_service
from app.stores import org_store

_MAX_FINDINGS = 50


class SuspiciousDocument(Exception):
    """Raised when a file contains high-severity injection patterns."""

    def __init__(self, filename: str, findings: list[dict]):
        super().__init__(
            f"{filename} contains content that looks like a prompt-injection attempt"
        )
        self.filename = filename
        self.findings = findings


def can_override(user: dict, collection: dict) -> bool:
    """Return whether this user may override a high-severity upload finding."""
    return (
        user.get("role") == "admin"
        or collection.get("owner_user_id") == user.get("user_id")
    )


def _cleanup_unpublished_document(doc_id: str) -> None:
    """Best-effort cleanup after indexing or publication fails."""
    try:
        delete_doc_chunks(doc_id)
    except Exception:
        logger.exception(
            "Failed to clean unpublished vector rows doc_id=%s",
            doc_id,
        )

    try:
        org_store.set_document_status(doc_id, "failed")
    except Exception:
        # Registration may have failed before the document row existed.
        logger.debug(
            "Could not mark unpublished document failed doc_id=%s",
            doc_id,
            exc_info=True,
        )


def ingest_file(
    user: dict,
    collection: dict,
    filename: str,
    content: bytes,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    allow_flagged: bool = False,
) -> dict:
    """Parse, scan, stage, index, and publish a document version."""
    filename = os.path.basename(filename) or "untitled"

    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
        raise ValueError("Chunk size must be an integer")
    if chunk_size < 100 or chunk_size > 10000:
        raise ValueError("Chunk size must be between 100 and 10,000 characters")

    if not isinstance(chunk_overlap, int) or isinstance(chunk_overlap, bool):
        raise ValueError("Chunk overlap must be an integer")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError(
            "Chunk overlap must be non-negative and smaller than chunk size"
        )

    if not isinstance(content, bytes):
        raise ValueError("Uploaded file content must be bytes")
    if not content:
        raise ValueError("Uploaded file is empty")

    collection_id = collection["collection_id"]
    org_id = collection["org_id"]
    user_id = user["user_id"]
    digest = hashlib.sha256(content).hexdigest()

    # An identical active document does not need to be indexed again.
    existing = org_store.find_active_document(collection_id, filename)
    if existing and existing["content_hash"] == digest:
        return {
            "filename": filename,
            "status": "unchanged",
            "version": existing["version"],
        }

    # Parse the file and reject uploads without usable text.
    parse_started = time.perf_counter()
    documents, notes = parse_file(filename, content)
    parse_ms = round((time.perf_counter() - parse_started) * 1000, 2)

    if not documents or not any(
        getattr(document, "page_content", "").strip()
        for document in documents
    ):
        raise ValueError("No readable text could be extracted from this document")

    # Scan extracted text before chunking or indexing.
    findings = []
    for document in documents:
        remaining = _MAX_FINDINGS - len(findings)
        if remaining <= 0:
            break

        section_findings = scan_text(
            document.page_content,
            location=location_label(document.metadata),
        )
        findings.extend(section_findings[:remaining])

    scan = [finding.to_dict() for finding in findings]
    has_high = any(finding.severity == HIGH for finding in findings)
    overridden = False

    if has_high:
        if not (allow_flagged and can_override(user, collection)):
            raise SuspiciousDocument(filename, scan)
        overridden = True

    # Create chunks and enforce the per-document limit.
    chunks = chunk_documents(
        documents,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    if not chunks:
        raise ValueError("No text chunks could be created from this document")

    if len(chunks) > MAX_CHUNKS_PER_DOC:
        raise ValueError(
            f"Document is too large ({len(chunks):,} chunks; "
            f"limit {MAX_CHUNKS_PER_DOC:,})"
        )

    # This is a provisional version number. The publication transaction in
    # org_store is responsible for assigning the authoritative version.
    version = existing["version"] + 1 if existing else 1
    doc_id = uuid.uuid4().hex

    for chunk in chunks:
        chunk.metadata.update(
            {
                "org_id": org_id,
                "collection_id": collection_id,
                "doc_id": doc_id,
                "version": version,
                "source": filename,
            }
        )

    # Register the staged row BEFORE writing vectors. Retrieval must exclude
    # staged documents by checking active document metadata.
    try:
        org_store.register_document(
            collection_id,
            org_id,
            filename,
            version,
            digest,
            len(chunks),
            user_id,
            doc_id=doc_id,
            scan=scan,
            status="staged",
        )
    except Exception:
        # No vectors have been written yet. Do not change the existing active
        # document if the new metadata row cannot be registered.
        logger.exception(
            "RAG staged registration failed collection=%s filename=%s doc_id=%s",
            collection_id,
            filename,
            doc_id,
        )
        raise

    # Index only after registration succeeds. If indexing fails, the new row
    # is marked failed and the previous active version remains untouched.
    index_started = time.perf_counter()
    try:
        add_org_chunks(chunks)
    except Exception:
        _cleanup_unpublished_document(doc_id)
        logger.exception(
            "RAG vector write failed collection=%s filename=%s doc_id=%s",
            collection_id,
            filename,
            doc_id,
        )
        raise

    index_ms = round((time.perf_counter() - index_started) * 1000, 2)

    # Publish only after indexing succeeds. The store must atomically mark
    # the previous version superseded, activate this row, and bump data_version.
    try:
        superseded_ids, published_version = org_store.publish_document_version(
            doc_id,
            collection_id,
        )
        version = published_version
    except Exception:
        _cleanup_unpublished_document(doc_id)
        logger.exception(
            "RAG metadata publication failed collection=%s filename=%s doc_id=%s",
            collection_id,
            filename,
            doc_id,
        )
        raise

    # The new version is active now (superseded_ids are already excluded
    # from retrieval by status alone). Cleaning up their old vectors and
    # summaries is equivalent to deleting them -- the same job_type that
    # an explicit document delete uses -- so it gets the same durable
    # queuing, retry-with-backoff, and verification instead of a one-shot
    # best-effort attempt that silently leaves orphaned vectors behind if
    # it fails and nothing ever retries it.
    cleanup_ok = True

    for old_doc_id in superseded_ids:
        try:
            result = cleanup_service.request_document_deletion(
                old_doc_id, collection_id, org_id,
            )
            if result["cleanup_status"] != "completed":
                cleanup_ok = False
        except Exception:
            cleanup_ok = False
            logger.exception(
                "Replacement published but cleanup could not be queued "
                "for superseded old_doc_id=%s new_doc_id=%s",
                old_doc_id,
                doc_id,
            )

    logger.info(
        "RAG ingest complete collection=%s filename=%s version=%d chunks=%d "
        "findings=%d parse_ms=%s index_ms=%s cleanup_pending=%s",
        collection_id,
        filename,
        version,
        len(chunks),
        len(scan),
        parse_ms,
        index_ms,
        not cleanup_ok,
    )

    return {
        "filename": filename,
        "status": "new_version" if existing else "added",
        "version": version,
        "chunks": len(chunks),
        "doc_id": doc_id,
        "notes": notes,
        "findings": scan,
        "overridden": overridden,
        "cleanup_pending": not cleanup_ok,
        "parse_ms": parse_ms,
        "index_ms": index_ms,
    }
