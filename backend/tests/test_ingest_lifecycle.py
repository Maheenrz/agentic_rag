
"""Regression tests for document ingestion, publication, and retrieval safety.

Run from the backend directory:
    python -m pytest -q tests/test_ingest_lifecycle.py

These are mocked unit tests. They do not replace a PostgreSQL integration test
for the real publication transaction.
"""

import hashlib

import pytest
from langchain_core.documents import Document

from app.rag import document_processor as processor
from app.rag import ingest


USER = {"user_id": "user-1", "role": "admin", "org_id": "org-1"}
COLLECTION = {
    "collection_id": "collection-1",
    "org_id": "org-1",
    "owner_user_id": None,
}
CONTENT = b"Company policy: employees receive annual leave."


def make_doc(doc_id, text, org_id="org-1", collection_id="collection-1"):
    return Document(
        page_content=text,
        metadata={
            "doc_id": doc_id,
            "org_id": org_id,
            "collection_id": collection_id,
            "source": "policy.txt",
        },
    )


def configure_ingest(
    monkeypatch,
    *,
    existing=None,
    index_error=None,
    publish_error=None,
    cleanup_error=None,
):
    """Replace external dependencies and record the lifecycle call order."""
    events = []
    state = {
        "old_status": "active" if existing else None,
        "new_status": None,
        "new_doc_id": None,
    }

    monkeypatch.setattr(
        ingest.org_store,
        "find_active_document",
        lambda collection_id, filename: existing,
    )

    monkeypatch.setattr(
        ingest,
        "parse_file",
        lambda filename, content: (
            [Document(page_content="Company policy text.", metadata={"source": filename})],
            [],
        ),
    )
    monkeypatch.setattr(ingest, "scan_text", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        ingest,
        "chunk_documents",
        lambda *args, **kwargs: [
            Document(page_content="Company policy text.", metadata={})
        ],
    )

    def register_document(*args, **kwargs):
        events.append("register")
        state["new_doc_id"] = kwargs["doc_id"]
        state["new_status"] = kwargs.get("status", "staged")
        assert state["new_status"] == "staged"
        return kwargs["doc_id"]

    monkeypatch.setattr(
        ingest.org_store, "register_document", register_document
    )

    def add_chunks(chunks):
        events.append("index")
        assert state["new_status"] == "staged", (
            "Vectors must not be written before the document is staged."
        )
        if index_error:
            raise index_error

    monkeypatch.setattr(ingest, "add_org_chunks", add_chunks)

    def publish(doc_id, collection_id):
        events.append("publish")
        assert doc_id == state["new_doc_id"]
        assert state["new_status"] == "staged"
        if publish_error:
            raise publish_error

        state["new_status"] = "active"
        if existing:
            state["old_status"] = "superseded"
            return [existing["doc_id"]], existing["version"] + 1
        return [], 1

    monkeypatch.setattr(
        ingest.org_store, "publish_document_version", publish
    )

    def delete_chunks(doc_id):
        events.append(f"delete:{doc_id}")
        if cleanup_error:
            raise cleanup_error

    monkeypatch.setattr(ingest, "delete_doc_chunks", delete_chunks)

    def set_status(doc_id, status):
        events.append(f"status:{status}")
        if doc_id == state["new_doc_id"]:
            state["new_status"] = status

    monkeypatch.setattr(
        ingest.org_store, "set_document_status", set_status
    )
    monkeypatch.setattr(ingest.org_store, "bump_version", lambda cid: None)

    def request_document_deletion(doc_id, collection_id, org_id):
            events.append(f"cleanup:{doc_id}")
            if cleanup_error:
                raise cleanup_error
            return {"cleanup_status": "completed"}

    monkeypatch.setattr(
        ingest.cleanup_service,
        "request_document_deletion",
        request_document_deletion,
    )


    return events, state




def test_identical_upload_is_unchanged_and_does_not_index(monkeypatch):
    existing = {
        "doc_id": "old-doc",
        "version": 1,
        "content_hash": hashlib.sha256(CONTENT).hexdigest(),
    }
    events, _ = configure_ingest(monkeypatch, existing=existing)

    result = ingest.ingest_file(
        USER, COLLECTION, "policy.txt", CONTENT
    )

    assert result["status"] == "unchanged"
    assert result["version"] == 1
    assert "index" not in events
    assert "register" not in events
    assert "publish" not in events


def test_new_document_is_staged_before_index_and_published_after(monkeypatch):
    events, state = configure_ingest(monkeypatch)

    result = ingest.ingest_file(
        USER, COLLECTION, "policy.txt", CONTENT
    )

    assert events.index("register") < events.index("index")
    assert events.index("index") < events.index("publish")
    assert state["new_status"] == "active"
    assert result["status"] == "added"
    assert result["version"] == 1


def test_index_failure_does_not_supersede_existing_active_version(monkeypatch):
    existing = {
        "doc_id": "old-doc",
        "version": 1,
        "content_hash": "different-old-hash",
    }
    events, state = configure_ingest(
        monkeypatch,
        existing=existing,
        index_error=RuntimeError("simulated vector write failure"),
    )

    with pytest.raises(RuntimeError, match="simulated vector write failure"):
        ingest.ingest_file(USER, COLLECTION, "policy.txt", CONTENT)

    assert events.index("register") < events.index("index")
    assert "publish" not in events
    assert state["old_status"] == "active"
    assert state["new_status"] in {"failed", "deleted"}
    assert f"delete:{state['new_doc_id']}" in events


def test_publication_failure_keeps_old_version_active(monkeypatch):
    existing = {
        "doc_id": "old-doc",
        "version": 1,
        "content_hash": "different-old-hash",
    }
    events, state = configure_ingest(
        monkeypatch,
        existing=existing,
        publish_error=RuntimeError("simulated publication failure"),
    )

    with pytest.raises(RuntimeError, match="simulated publication failure"):
        ingest.ingest_file(USER, COLLECTION, "policy.txt", CONTENT)

    assert events.index("register") < events.index("index")
    assert events.index("index") < events.index("publish")
    assert state["old_status"] == "active"
    assert state["new_status"] in {"failed", "deleted"}
    assert f"delete:{state['new_doc_id']}" in events
    assert "delete:old-doc" not in events


class FakeVectorStore:
    def __init__(self, hits):
        self.hits = hits
        self.last_filter = None

    def similarity_search(self, query, k, filter):
        self.last_filter = filter
        return self.hits[:k]


def test_vector_retrieval_excludes_superseded_and_deleted_chunks(monkeypatch):
    active = make_doc("active-doc", "Current leave policy.")
    superseded = make_doc("old-doc", "Old leave policy.")
    deleted = make_doc("deleted-doc", "Deleted confidential policy.")

    store = FakeVectorStore([superseded, deleted, active])
    monkeypatch.setattr(processor, "get_org_store", lambda: store)
    monkeypatch.setattr(
        processor, "active_document_ids", lambda ids, org_id=None: ["active-doc"]
    )
    monkeypatch.setattr(processor, "HYBRID_SEARCH", False)

    retriever = processor.build_org_retriever(
        "org-1", ["collection-1"], k=10
    )
    results = retriever.invoke("leave policy")

    assert results
    assert {doc.metadata["doc_id"] for doc in results} == {"active-doc"}
    assert store.last_filter["org_id"] == "org-1"
    assert store.last_filter["doc_id"] == {"$in": ["active-doc"]}


def test_hybrid_retrieval_does_not_return_inactive_keyword_hits(monkeypatch):
    active = make_doc("active-doc", "Current leave policy.")
    superseded = make_doc("old-doc", "Old leave policy.")
    deleted = make_doc("deleted-doc", "Deleted leave policy.")

    store = FakeVectorStore([active, superseded, deleted])
    monkeypatch.setattr(processor, "get_org_store", lambda: store)
    monkeypatch.setattr(
        processor, "active_document_ids", lambda ids, org_id=None: ["active-doc"]
    )
    monkeypatch.setattr(processor, "HYBRID_SEARCH", True)

    class FakeBM25:
        def get_scores(self, tokens):
            # Deliberately rank an inactive chunk highest.
            return [1.0, 100.0, 90.0]

    monkeypatch.setattr(
        processor,
        "_keyword_index",
        lambda *args, **kwargs: ([active, superseded, deleted], FakeBM25()),
    )

    retriever = processor.build_org_retriever(
        "org-1", ["collection-1"], k=10
    )
    results = retriever.invoke("leave policy")

    assert {doc.metadata["doc_id"] for doc in results} == {"active-doc"}


def test_replacement_queues_cleanup_for_superseded_document(monkeypatch):
    existing = {
        "doc_id": "old-doc",
        "version": 1,
        "content_hash": "different-old-hash",
    }
    events, state = configure_ingest(monkeypatch, existing=existing)

    result = ingest.ingest_file(
        USER, COLLECTION, "policy.txt", CONTENT
    )

    assert result["status"] == "new_version"
    assert state["new_status"] == "active"
    assert "cleanup:old-doc" in events
    assert events.index("publish") < events.index("cleanup:old-doc")
    assert result["cleanup_pending"] is False