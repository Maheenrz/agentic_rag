"""
Diagnostic: does your vector search lose results when a user's allowed documents are a SMALL slice of a BIG index?

Why it matters: every chat searches ONE shared Chroma collection ("org_docs") with a metadata filter
(org + the collections this user may read). While testing the injection eval I saw Chroma return FEWER chunks than
exist (2 of 4) once the shared collection held a few hundred vectors. That would silently hurt answers in a real
multi-tenant deployment, and your 30-question eval cannot see it (one PDF = no selective filter).

Run (uses your REAL embedding model, in a throw-away folder; takes about a minute):
    python3 -m tests.check_filtered_recall
    python3 -m tests.check_filtered_recall --collections 200      # bigger index
    python3 -m tests.check_filtered_recall --offline              # hashing embedder, no model download
Reading the result: "complete" = every query returned all 4 chunks that exist for that user. Anything under 100% means
documents the user is allowed to see were not even looked at.
"""
import argparse, atexit, os, shutil, sys, tempfile, uuid

ap = argparse.ArgumentParser()
ap.add_argument("--collections", type=int, default=60)
ap.add_argument("--docs-per-collection", type=int, default=4)
ap.add_argument("--offline", action="store_true")
a = ap.parse_args()
_TMP = tempfile.mkdtemp(prefix="ragrecall_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.update(GROQ_API_KEY=os.environ.get("GROQ_API_KEY", "x"), CHROMA_PERSIST_DIR=_TMP + "/chroma", SQLITE_DB_PATH=_TMP + "/t.db")
if a.offline:
    os.environ["FORCE_OFFLINE_EMBEDDINGS"] = "true"

from langchain_core.documents import Document
from app.rag import document_processor as D

TOPICS = ["leave policy", "refund window", "vpn setup", "expense approval", "onboarding laptop", "dress code",
          "password reset", "office hours", "travel booking", "security training", "payroll dates", "parking rules"]
org = "diag_org"
print(f"embedder: {D.get_embedding_mode_label()}   collections: {a.collections} x {a.docs_per_collection} chunks "
      f"= {a.collections * a.docs_per_collection} vectors in ONE index")
cids = []
for i in range(a.collections):
    cid = f"diag_{i}_{uuid.uuid4().hex[:6]}"
    cids.append((cid, TOPICS[i % len(TOPICS)]))
    D.add_org_chunks([Document(page_content=f"{TOPICS[(i + j) % len(TOPICS)]} document number {i}-{j}: details about {TOPICS[(i + j) % len(TOPICS)]} at the company.",
                               metadata={"source": f"d{i}_{j}.txt", "page": 1, "org_id": org, "collection_id": cid, "doc_id": uuid.uuid4().hex})
                      for j in range(a.docs_per_collection)])

complete, lost = 0, []
for cid, topic in cids:
    got = D.build_org_retriever(org, [cid], k=15).invoke(f"what is the {topic}?")
    if len(got) >= a.docs_per_collection:
        complete += 1
    else:
        lost.append(len(got))
pct = 100 * complete / len(cids)
print(f"\nqueries returning ALL {a.docs_per_collection} chunks the user may read: {complete}/{len(cids)} = {pct:.0f}%")
if lost:
    print(f"incomplete queries returned only: {sorted(set(lost))} chunks")
print("VERDICT:", "OK at this size" if pct == 100 else
      "PROBLEM: filtered search is losing allowed chunks. Fix = one Chroma collection per org (or per collection), "
      "and/or exact keyword search (SQLite FTS5) that is not approximate. Tell me and I will implement it.")