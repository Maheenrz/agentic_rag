"""
ingest.py
----------
Document lifecycle for a collection: upload, new version.

  same filename + same bytes   -> "unchanged" (nothing re-embedded)
  same filename + new bytes    -> new VERSION: new chunks in, old chunks out,
                                  old row kept as 'superseded'
  new filename                 -> version 1

Security at ingest (every upload goes through this, nothing else writes chunks):
  1. parsers.parse_file(): type allow-list, magic bytes, zip-bomb + size limits,
     hidden HTML text stripped
  2. scan_text() on every section: prompt-injection patterns, hidden Unicode,
     secrets. HIGH findings REJECT the upload (SuspiciousDocument) unless the
     uploader may override (an admin, or the owner of their own personal
     collection) AND passes allow_flagged=True -- the override is audited by main.py.
  3. Findings are stored with the document so admins can review them later.

Every chunk is stamped with org_id / collection_id / doc_id so retrieval can be
filtered server-side and a document can be deleted by doc_id alone.
"""

import hashlib
import os
import uuid

from app.stores import org_store
from app.config import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, MAX_CHUNKS_PER_DOC, logger
from app.rag.document_processor import add_org_chunks, chunk_documents, delete_doc_chunks, location_label
from app.rag.parsers import parse_file
from app.guardrails.security import HIGH, scan_text

_MAX_FINDINGS = 50


class SuspiciousDocument(Exception):
    """Raised when a file contains high-severity injection patterns."""
    def __init__(self, filename: str, findings: list[dict]):
        super().__init__(f"{filename} contains content that looks like a prompt-injection attempt")
        self.filename, self.findings = filename, findings


def can_override(user: dict, collection: dict) -> bool:
    return user["role"] == "admin" or collection.get("owner_user_id") == user["user_id"]


def ingest_file(
    user: dict, collection: dict, filename: str, content: bytes,
    chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    allow_flagged: bool = False,
) -> dict:
    filename = os.path.basename(filename) or "untitled"
    digest = hashlib.sha256(content).hexdigest()
    cid = collection["collection_id"]

    existing = org_store.find_active_document(cid, filename)
    if existing and existing["content_hash"] == digest:
        return {"filename": filename, "status": "unchanged", "version": existing["version"]}

    documents, notes = parse_file(filename, content)          # ValueError with a user-safe message

    findings = []
    for d in documents:
        findings += scan_text(d.page_content, location=location_label(d.metadata))
        if len(findings) >= _MAX_FINDINGS:
            findings = findings[:_MAX_FINDINGS]
            break
    scan = [f.to_dict() for f in findings]
    has_high = any(f.severity == HIGH for f in findings)
    overridden = False
    if has_high:
        if not (allow_flagged and can_override(user, collection)):
            raise SuspiciousDocument(filename, scan)
        overridden = True

    chunks = chunk_documents(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if len(chunks) > MAX_CHUNKS_PER_DOC:
        raise ValueError(f"Document is too large ({len(chunks):,} chunks; limit {MAX_CHUNKS_PER_DOC:,})")

    version = existing["version"] + 1 if existing else 1
    doc_id = uuid.uuid4().hex
    for chunk in chunks:
        chunk.metadata.update({
            "org_id": collection["org_id"], "collection_id": cid,
            "doc_id": doc_id, "version": version, "source": filename,
        })

    try:
        add_org_chunks(chunks)
    except Exception:
        delete_doc_chunks(doc_id)          # don't leave half-indexed chunks behind
        raise

    org_store.register_document(
        cid, collection["org_id"], filename, version, digest, len(chunks), user["user_id"], doc_id=doc_id, scan=scan
    )
    if existing:
        delete_doc_chunks(existing["doc_id"])
        org_store.set_document_status(existing["doc_id"], "superseded")
    org_store.bump_version(cid)            # invalidates cached answers for this collection

    logger.info("Ingested %s v%d into %s (%d chunks, %d findings)", filename, version, cid, len(chunks), len(scan))
    return {"filename": filename, "status": "new_version" if existing else "added",
            "version": version, "chunks": len(chunks), "doc_id": doc_id,
            "notes": notes, "findings": scan, "overridden": overridden}