"""
run_eval.py — one-command evaluation for the Agentic RAG project.

Run from backend/evaluation:
    PYTHONPATH=.. python run_eval.py --stage retrieval
    PYTHONPATH=.. python run_eval.py --stage grader
    PYTHONPATH=.. python run_eval.py --stage faith
    PYTHONPATH=.. python run_eval.py --stage cache
    PYTHONPATH=.. python run_eval.py --stage recall
    PYTHONPATH=.. python run_eval.py --stage e2e --tag rerank_on
    PYTHONPATH=.. python run_eval.py --stage e2e --tag rerank_off --no-rerank
    PYTHONPATH=.. python run_eval.py

EVAL_DATABASE_URL must point to an isolated evaluation database.
Results are written to results/<timestamp>_<tag>/.
"""

# IMPORTANT: standard-library imports and evaluation DB configuration must
# happen before importing ANY app modules. app.config/db.py cache settings
# when imported, so setting DATABASE_URL afterward is too late.

import os
import sys
import hashlib

_eval_db = os.environ.get("EVAL_DATABASE_URL")
if not _eval_db:
    sys.exit(
        "ERROR: Set EVAL_DATABASE_URL to a dedicated evaluation database. "
        "Refusing to run without an explicit evaluation database."
    )

os.environ["DATABASE_URL"] = _eval_db
os.environ["IS_PG"] = "true"

import argparse
import csv
import json
import random
import re
import statistics
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit, unquote

# Application imports only after DATABASE_URL has been set.
from app.rag.parsers import parse_file
from app.rag.document_processor import (
    add_org_chunks,
    build_org_retriever,
    get_org_store,
    _get_pg_connection,
)

EVAL_ORG, EVAL_CID = "eval_org", "eval_handbook"

# ---------------------------------------------------------------------------
# Parallel model preload
# ---------------------------------------------------------------------------

_preload_errors = []


def _preload_flashrank():
    try:
        from flashrank import Ranker

        t0 = time.time()
        Ranker(model_name="ms-marco-MiniLM-L-12-v2")
        print(f"[preload] flashrank ready in {time.time() - t0:.1f}s")
    except Exception as exc:
        _preload_errors.append(f"flashrank: {exc}")
        print(f"[preload] flashrank unavailable: {exc}")


def _preload_minilm():
    try:
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        print(f"[preload] minilm ready in {time.time() - t0:.1f}s")
    except Exception as exc:
        _preload_errors.append(f"minilm: {exc}")
        print(f"[preload] minilm unavailable: {exc}")


_preload_threads = [
    threading.Thread(target=_preload_flashrank, daemon=True),
    threading.Thread(target=_preload_minilm, daemon=True),
]

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage

try:
    import fitz

    _PDF_BACKEND = "pymupdf"
except ImportError:
    from pypdf import PdfReader  # noqa: F401

    _PDF_BACKEND = "pypdf"

from app.core import graph as graph_module
from app.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    RERANK_CANDIDATE_K,
)
from app.rag.document_processor import (
    check_cache,
    chunk_documents,
    format_context,
    get_cache_store,
    get_embedding_mode_label,
    get_thread_vector_store,
    save_thread_summary,
    write_cache,
)
from app.core.graph import (
    AgentState,
    NO_ANSWER_MESSAGE,
    _FAITHFULNESS_PROMPT,
    _GRADER_PROMPT,
    compiled_graph,
    generation_llm,
    small_llm,
    summarize_thread_for_storage,
)
from app.core.reranker import rerank_documents

EVAL_THREAD = "eval_shared"
RUN_ID = uuid.uuid4().hex[:8]

JUDGE_PROMPT = """You are an independent answer-quality evaluator.

Evaluate the candidate answer against the reference answer. Judge semantic
correctness, not exact wording. A candidate is CORRECT if it answers the
question accurately and includes the essential reference information.
A candidate is PARTIAL if it is partly correct but omits an important point.
A candidate is INCORRECT if it is wrong, contradicts the reference, or fails
to answer the question.

Question: {q}
Reference answer: {gold}
Candidate answer: {ans}

Respond with exactly one word: CORRECT, PARTIAL or INCORRECT.
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def pct(x, n):
    return round(100.0 * x / n, 1) if n else 0.0


def first_rank(pages, gold):
    for i, page in enumerate(pages, start=1):
        if page in gold:
            return i
    return None


def llm_call(llm, prompt, sleep):
    err = None
    for attempt in range(3):
        try:
            response = llm.invoke([HumanMessage(content=prompt)])
            text = response.content
            if not isinstance(text, str):
                text = str(text)
            time.sleep(sleep)
            return text.strip(), None
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            time.sleep(3 * (attempt + 1))
    return None, err


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# PDF extraction helpers
# ---------------------------------------------------------------------------

def _extract_pages(pdf_path, pdf_backend):
    if pdf_backend == "pymupdf":
        print(f"[index] reading {pdf_path} with PyMuPDF ...")
        t0 = time.time()
        try:
            doc = fitz.open(pdf_path)
        except Exception as exc:
            print(f"[index] PyMuPDF failed ({exc}); falling back to pypdf")
            return _extract_pages_pypdf(pdf_path)

        pages = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text() or ""
            if text.strip():
                pages.append(
                    Document(
                        page_content=text,
                        metadata={
                            "source": os.path.basename(pdf_path),
                            "page": i,
                        },
                    )
                )
        doc.close()
        print(f"[index] extracted {len(pages)} pages in {time.time() - t0:.1f}s")
        return pages

    return _extract_pages_pypdf(pdf_path)


def _extract_pages_pypdf(pdf_path):
    from pypdf import PdfReader

    print(f"[index] reading {pdf_path} with pypdf ...")
    t0 = time.time()
    reader = PdfReader(pdf_path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": os.path.basename(pdf_path),
                        "page": i,
                    },
                )
            )
    print(f"[index] extracted {len(pages)} pages in {time.time() - t0:.1f}s")
    return pages


# ---------------------------------------------------------------------------
# Evaluation database access
# ---------------------------------------------------------------------------

def _eval_rows(sql, params=()):
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def verify_eval_database():
    """Verify the actual connection is the explicitly configured eval DB."""
    parsed = urlsplit(_eval_db)
    expected_db = unquote(parsed.path.rsplit("/", 1)[-1])
    if not expected_db:
        raise RuntimeError("EVAL_DATABASE_URL must include a database name.")

    db_info_rows = _eval_rows(
        "SELECT current_database() AS database_name, "
        "inet_server_addr()::text AS server_addr, inet_server_port() AS server_port"
    )
    db_info = db_info_rows[0]
    actual_db = db_info["database_name"]
    if actual_db != expected_db:
        raise RuntimeError(
            f"Database safety check failed: connected to {actual_db!r}, "
            f"but EVAL_DATABASE_URL specifies {expected_db!r}. Evaluation aborted."
        )

    table_rows = _eval_rows(
        "SELECT to_regclass('public.langchain_pg_embedding') AS table_name"
    )
    table_exists = table_rows[0]["table_name"] is not None
    print(
        f"[db] verified evaluation database={actual_db!r}, "
        f"server={db_info['server_addr']}:{db_info['server_port']}"
    )
    if not table_exists:
        print("[db] PGVector table does not exist yet; a new evaluation index may be created.")
    return table_exists



def ensure_eval_registry(pdf_path):
    """Ensure indexed evaluation chunks have active registry records."""
    with open(pdf_path, "rb") as f:
        content_hash = hashlib.sha256(f.read()).hexdigest()

    rows = _eval_rows(
        """
        SELECT
            cmetadata->>'doc_id' AS doc_id,
            COALESCE(cmetadata->>'source', %s) AS filename,
            COUNT(*) AS chunk_count
        FROM langchain_pg_embedding
        WHERE cmetadata->>'org_id' = %s
          AND cmetadata->>'collection_id' = %s
          AND cmetadata->>'doc_id' IS NOT NULL
        GROUP BY cmetadata->>'doc_id', cmetadata->>'source'
        """,
        (os.path.basename(pdf_path), EVAL_ORG, EVAL_CID),
    )

    if not rows:
        raise RuntimeError("No evaluation chunks found to register.")

    expected_filename = os.path.basename(pdf_path)
    indexed_filenames = {row["filename"] for row in rows}
    if indexed_filenames != {expected_filename}:
        raise RuntimeError(
            "Evaluation index source mismatch: expected only "
            f"{expected_filename!r}, found {sorted(indexed_filenames)!r}. "
            "Refusing to activate/register unrelated chunks. Use the correct PDF "
            "or rebuild the isolated evaluation index."
        )

    # If a registry row already has a content hash, do not silently overwrite a
    # mismatch. Legacy vector-only indexes may have no registry row/hash; those
    # are registered with a warning because the old chunks cannot prove their
    # source bytes retroactively.
    existing = _eval_rows(
        "SELECT doc_id, content_hash, filename FROM documents "
        "WHERE doc_id = ANY(%s)",
        ([row["doc_id"] for row in rows],),
    )
    existing_by_id = {r["doc_id"]: r for r in existing}
    for row in rows:
        old = existing_by_id.get(row["doc_id"])
        if old and old.get("content_hash") and old["content_hash"] != content_hash:
            raise RuntimeError(
                f"Indexed document {row['doc_id']} has a different source PDF hash. "
                "Refusing to relabel stale chunks as the current PDF. Rebuild the "
                "evaluation index in the isolated evaluation database."
            )
    if any(row["doc_id"] not in existing_by_id for row in rows):
        print(
            "[registry] WARNING: legacy vector chunks lack registry hashes; "
            "source filename matches, but their original PDF bytes cannot be "
            "cryptographically verified. The current PDF hash will be recorded."
        )

    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orgs (org_id, name, created_at)
                VALUES (%s, %s, EXTRACT(EPOCH FROM NOW()))
                ON CONFLICT (org_id) DO NOTHING
                """,
                (EVAL_ORG, "Evaluation Organization"),
            )

            cur.execute(
                """
                INSERT INTO collections (
                    collection_id, org_id, name, description,
                    visibility, created_at, status
                )
                VALUES (
                    %s, %s, %s, %s, 'org',
                    EXTRACT(EPOCH FROM NOW()), 'active'
                )
                ON CONFLICT (collection_id) DO UPDATE
                SET org_id = EXCLUDED.org_id,
                    status = 'active'
                """,
                (
                    EVAL_CID,
                    EVAL_ORG,
                    "Evaluation Handbook",
                    "Isolated RAG evaluation collection",
                ),
            )

            for row in rows:
                cur.execute(
                    """
                    INSERT INTO documents (
                        doc_id, collection_id, org_id, filename,
                        version, status, content_hash, chunk_count,
                        uploaded_by, created_at, scan_json
                    )
                    VALUES (
                        %s, %s, %s, %s, 1, 'active', %s, %s,
                        'evaluation', EXTRACT(EPOCH FROM NOW()), '[]'
                    )
                    ON CONFLICT (doc_id) DO UPDATE
                    SET collection_id = EXCLUDED.collection_id,
                        org_id = EXCLUDED.org_id,
                        filename = EXCLUDED.filename,
                        status = 'active',
                        content_hash = EXCLUDED.content_hash,
                        chunk_count = EXCLUDED.chunk_count
                    """,
                    (
                        row["doc_id"],
                        EVAL_CID,
                        EVAL_ORG,
                        row["filename"],
                        content_hash,
                        row["chunk_count"],
                    ),
                )

        conn.commit()

    print(f"[registry] ensured active registry records for {len(rows)} document(s)")



def ensure_index(pdf_path):
    # Fail before indexing if the active connection is not the eval DB.
    table_exists = verify_eval_database()

    n = 0
    if table_exists:
        rows = _eval_rows(
            """
            SELECT COUNT(*) AS n
            FROM langchain_pg_embedding
            WHERE cmetadata->>'collection_id' = %s
            """,
            (EVAL_CID,),
        )
        n = rows[0]["n"]

    if n:
        print(f"[index] reusing eval index ({n} chunks)")
    else:
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(
                f"Evaluation PDF not found: {pdf_path}"
            )

        print(f"[index] no existing chunks for {EVAL_CID}; indexing PDF")

        with open(pdf_path, "rb") as f:
            docs, _ = parse_file(os.path.basename(pdf_path), f.read())

        chunks = chunk_documents(
            docs,
            DEFAULT_CHUNK_SIZE,
            DEFAULT_CHUNK_OVERLAP,
        )

        if not chunks:
            raise RuntimeError(
                f"PDF parsing/chunking produced zero chunks: {pdf_path}"
            )

        doc_id = uuid.uuid4().hex
        for chunk in chunks:
            chunk.metadata.update(
                org_id=EVAL_ORG,
                collection_id=EVAL_CID,
                doc_id=doc_id,
                source=os.path.basename(pdf_path),
            )

        add_org_chunks(chunks)
        print(f"[index] submitted {len(chunks)} chunks for indexing")

        # Verify that the index was actually written to the eval collection.
        rows = _eval_rows(
            """
            SELECT COUNT(*) AS n
            FROM langchain_pg_embedding
            WHERE cmetadata->>'collection_id' = %s
            """,
            (EVAL_CID,),
        )
        n_after = rows[0]["n"]
        print(f"[index] verified stored chunks: {n_after}")

        if n_after == 0:
            raise RuntimeError(
                "Indexing returned without an error, but the evaluation "
                "collection still contains zero chunks. Check add_org_chunks "
                "and the metadata written to langchain_pg_embedding."
            )

    ensure_eval_registry(pdf_path)
    # Verify the DB again immediately before building the retriever.
    verify_eval_database()

    return build_org_retriever(
        EVAL_ORG,
        [EVAL_CID],
        k=RERANK_CANDIDATE_K,
    )

def make_retriever(retriever):
    return retriever


def chunks_for_pages(_unused, pages):
    if not pages:
        return []

    rows = _eval_rows(
        """
        SELECT e.document, e.cmetadata
        FROM langchain_pg_embedding e
        WHERE e.cmetadata->>'collection_id' = %s
          AND (e.cmetadata->>'page')::int = ANY(%s)
        """,
        (EVAL_CID, list(pages)),
    )
    return [
        Document(page_content=row["document"], metadata=row["cmetadata"])
        for row in rows
    ]


def _eval_pages():
    rows = _eval_rows(
        """
        SELECT DISTINCT (cmetadata->>'page')::int AS p
        FROM langchain_pg_embedding
        WHERE cmetadata->>'collection_id' = %s
          AND cmetadata->>'page' IS NOT NULL
        """,
        (EVAL_CID,),
    )
    return sorted(row["p"] for row in rows if row["p"] is not None)


# ---------------------------------------------------------------------------
# Graph runner
# ---------------------------------------------------------------------------

def run_graph(question, thread_id, user_id, retriever):
    initial_state = {
        "question": question,
        "retrieved_docs": [],
        "relevant_docs": [],
        "rewrite_count": 0,
        "answer": "",
        "trace": [],
        "tool_used": "",
        "sources": [],
        "faithfulness_verdict": "",
    }

    if "search_query" in AgentState.__annotations__:
        initial_state["search_query"] = ""

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "retriever": retriever,
            "top_k": 4,
            "force_offline": False,
        }
    }
    return compiled_graph.invoke(initial_state, config=config)


# ---------------------------------------------------------------------------
# Stage 1: retrieval + reranker (no LLM calls)
# ---------------------------------------------------------------------------

def stage_retrieval(store, qs, args):
    rows = []

    for q in qs:
        gold = set(q["gold_pages"])
        candidates = store.invoke(q["question"])

        vec_pages = [
            d.metadata.get("page")
            for d in candidates
            if d.metadata.get("page") is not None
        ]
        reranked = rerank_documents(
            q["question"],
            candidates,
            top_n=len(candidates),
        )
        rr_pages = [
            d.metadata.get("page")
            for d in reranked
            if d.metadata.get("page") is not None
        ]

        def metrics(pages):
            rank = first_rank(pages, gold)
            return {
                "hit4": int(any(p in gold for p in pages[:4])),
                "recall4": len(gold & set(pages[:4])) / len(gold) if gold else 0,
                "hit15": int(any(p in gold for p in pages)),
                "rr": 1.0 / rank if rank else 0.0,
                "rank": rank or 0,
            }

        v, r = metrics(vec_pages), metrics(rr_pages)
        rows.append(
            {
                "id": q["id"],
                "category": q["category"],
                "gold_pages": q["gold_pages"],
                "vec_top4_pages": vec_pages[:4],
                "rerank_top4_pages": rr_pages[:4],
                "vec_hit4": v["hit4"],
                "rerank_hit4": r["hit4"],
                "vec_recall4": round(v["recall4"], 2),
                "rerank_recall4": round(r["recall4"], 2),
                "hit15": v["hit15"],
                "vec_rank": v["rank"],
                "rerank_rank": r["rank"],
                "vec_mrr": round(v["rr"], 3),
                "rerank_mrr": round(r["rr"], 3),
            }
        )

    n = len(rows)
    summary = {
        "n": n,
        "hit@15 (retriever candidate ceiling)": pct(
            sum(x["hit15"] for x in rows), n
        ),
        "vector Hit@4": pct(sum(x["vec_hit4"] for x in rows), n),
        "rerank Hit@4": pct(sum(x["rerank_hit4"] for x in rows), n),
        "vector Recall@4": round(
            100 * statistics.mean(x["vec_recall4"] for x in rows), 1
        ) if rows else 0,
        "rerank Recall@4": round(
            100 * statistics.mean(x["rerank_recall4"] for x in rows), 1
        ) if rows else 0,
        "vector MRR": round(
            statistics.mean(x["vec_mrr"] for x in rows), 3
        ) if rows else 0,
        "rerank MRR": round(
            statistics.mean(x["rerank_mrr"] for x in rows), 3
        ) if rows else 0,
        "rerank_helped (miss->hit)": sum(
            1 for x in rows if not x["vec_hit4"] and x["rerank_hit4"]
        ),
        "rerank_hurt (hit->miss)": sum(
            1 for x in rows if x["vec_hit4"] and not x["rerank_hit4"]
        ),
        "still_missing_after_rerank": [
            x["id"] for x in rows if not x["rerank_hit4"]
        ],
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Stage 2: batch relevance grader
# ---------------------------------------------------------------------------

def stage_grader_batch(store, qs, args):
    rng = random.Random(7)
    all_pages = _eval_pages()
    rows = []

    for q in qs:
        gold = q["gold_pages"]
        gold_chunks = chunks_for_pages(store, gold[:1])
        if not gold_chunks:
            continue

        negatives = []
        for page in q.get("hard_negative_pages", [])[:2]:
            negatives.extend(chunks_for_pages(store, [page])[:1])

        far_pages = [
            p for p in all_pages
            if all(abs(p - g) >= 40 for g in gold)
        ]
        if far_pages:
            negatives.extend(
                chunks_for_pages(store, [rng.choice(far_pages)])[:1]
            )

        candidates = [("gold", gold_chunks[0])]
        candidates.extend(("neg", doc) for doc in negatives)
        rng.shuffle(candidates)

        passages = "\n\n".join(
            f"[Passage {i}]\n{doc.page_content}"
            for i, (_, doc) in enumerate(candidates, 1)
        )

        verdict, err = llm_call(
            small_llm,
            graph_module._BATCH_GRADER_PROMPT.format(
                question=q["question"],
                passages=passages,
            ),
            args.sleep,
        )
        if verdict is None:
            print(f"[grader] {q['id']} failed: {err}")
            continue

        # Parse passage numbers. Avoid treating the word NONE as a number.
        if "NONE" in verdict.upper():
            kept = set()
        else:
            kept = {int(n) for n in re.findall(r"\d+", verdict)}

        for i, (kind, doc) in enumerate(candidates, 1):
            rows.append(
                {
                    "id": q["id"],
                    "kind": kind,
                    "page": doc.metadata.get("page"),
                    "kept": i in kept,
                    "correct": (i in kept) == (kind == "gold"),
                    "error": "",
                }
            )

    gold_rows = [r for r in rows if r["kind"] == "gold"]
    negative_rows = [r for r in rows if r["kind"] == "neg"]

    summary = {
        "gold kept (want high)": (
            f"{pct(sum(r['kept'] for r in gold_rows), len(gold_rows))}% "
            f"(n={len(gold_rows)})"
        ),
        "negatives rejected (want high)": (
            f"{pct(sum(not r['kept'] for r in negative_rows), len(negative_rows))}% "
            f"(n={len(negative_rows)})"
        ),
        "overall accuracy": pct(
            sum(r["correct"] for r in rows), len(rows)
        ),
        "failed LLM calls": len(qs) - len({
            r["id"] for r in rows
        }),
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Stage 3: faithfulness check in isolation
# ---------------------------------------------------------------------------

def stage_faith(store, tests, args):
    rows = []

    for test in tests:
        context = format_context(
            chunks_for_pages(store, test["context_pages"])
        )

        for kind, answer, want_unsupported in (
            ("good", test["good_answer"], False),
            ("bad", test["bad_answer"], True),
        ):
            verdict, err = llm_call(
                small_llm,
                _FAITHFULNESS_PROMPT.format(
                    context=context,
                    answer=answer,
                ),
                args.sleep,
            )

            said_unsupported = (
                None if verdict is None
                else "UNSUPPORTED" in verdict.upper()
            )

            rows.append(
                {
                    "id": test["id"],
                    "kind": kind,
                    "bad_type": test.get("bad_type", "") if kind == "bad" else "",
                    "expected_unsupported": want_unsupported,
                    "said_unsupported": said_unsupported,
                    "correct": (
                        said_unsupported == want_unsupported
                        if said_unsupported is not None else None
                    ),
                    "error": err or "",
                }
            )

    valid = [r for r in rows if r["correct"] is not None]
    bad_rows = [r for r in valid if r["kind"] == "bad"]
    good_rows = [r for r in valid if r["kind"] == "good"]

    summary = {
        "bad answers caught (want high)": (
            f"{pct(sum(r['correct'] for r in bad_rows), len(bad_rows))}% "
            f"(n={len(bad_rows)})"
        ),
        "good answers wrongly flagged (want 0)": (
            f"{pct(sum(not r['correct'] for r in good_rows), len(good_rows))}% "
            f"(n={len(good_rows)})"
        ),
        "missed bad answers": [
            f"{r['id']} ({r['bad_type']})"
            for r in bad_rows if not r["correct"]
        ],
        "failed LLM calls": len(rows) - len(valid),
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Stage 4: semantic cache
# ---------------------------------------------------------------------------

def raw_similarity(question, thread_id):
    store = get_cache_store()
    try:
        result = store.similarity_search_with_score(
            question,
            k=1,
            filter={"thread_id": thread_id},
        )
    except Exception:
        return None
    return round(1 - result[0][1], 3) if result else None


def stage_cache(tests):
    rows = []

    for test in tests:
        tid_a = f"evalcache_{RUN_ID}_{test['id']}_A"
        tid_b = f"evalcache_{RUN_ID}_{test['id']}_B"

        write_cache(
            test["setup_question"],
            "dummy cached answer",
            [],
            thread_id=tid_a,
        )

        lookup_thread = tid_b if test["id"] == "C5" else tid_a
        similarity = raw_similarity(test["test_question"], lookup_thread)
        hit = check_cache(
            test["test_question"],
            thread_id=lookup_thread,
        ) is not None

        expected = test["expect"]
        if expected == "HIT":
            verdict = "PASS" if hit else "FAIL"
        elif expected.startswith("MISS"):
            verdict = (
                "PASS" if not hit
                else "FAIL (WRONG ANSWER WOULD BE SERVED)"
            )
        else:
            verdict = "INFO"

        rows.append(
            {
                "id": test["id"],
                "setup": test["setup_question"],
                "test": test["test_question"],
                "expected": expected,
                "got": "HIT" if hit else "MISS",
                "similarity": similarity,
                "verdict": verdict,
            }
        )

    summary = {
        row["id"]: (
            f"{row['verdict']} (got {row['got']}, "
            f"similarity={row['similarity']})"
        )
        for row in rows
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Stage 5: cross-thread recall + user isolation
# ---------------------------------------------------------------------------

def stage_recall(store, qa_by_id, args):
    retriever = make_retriever(store)
    user_a = f"evaluser_a_{RUN_ID}"
    user_b = f"evaluser_b_{RUN_ID}"
    thread_1 = f"evalrecall_{RUN_ID}_t1"

    for qid in ("Q17", "Q18"):
        if qid in qa_by_id:
            run_graph(
                qa_by_id[qid]["question"],
                thread_1,
                user_a,
                retriever,
            )

    values = (
        compiled_graph.get_state(
            {"configurable": {"thread_id": thread_1}}
        ).values or {}
    )

    title, summary_text = summarize_thread_for_storage(
        values.get("raw_turns", []),
        values.get("running_summary", ""),
    )
    save_thread_summary(thread_1, title, summary_text, user_a)

    ask = "What did we discuss in my previous chat?"
    result_a = run_graph(
        ask, f"evalrecall_{RUN_ID}_t2", user_a, retriever
    )
    result_b = run_graph(
        ask, f"evalrecall_{RUN_ID}_t3", user_b, retriever
    )

    answer_a = result_a.get("answer", "")
    answer_b = result_b.get("answer", "")
    keywords = ("session", "hijack", "spoof")

    rows = [
        {
            "id": "R1",
            "user": "A (finalized thread exists)",
            "tool_used": result_a.get("tool_used"),
            "answer": answer_a[:300],
            "verdict": (
                "PASS"
                if result_a.get("tool_used") == "Conversation Memory"
                and any(k in answer_a.lower() for k in keywords)
                else "FAIL"
            ),
        },
        {
            "id": "R2/R3",
            "user": "B (no past threads)",
            "tool_used": result_b.get("tool_used"),
            "answer": answer_b[:300],
            "verdict": (
                "PASS"
                if not any(k in answer_b.lower() for k in keywords)
                else "FAIL (LEAK ACROSS USERS)"
            ),
        },
    ]

    return (
        {row["id"]: row["verdict"] for row in rows}
        | {
            "saved_title": title,
            "saved_summary": summary_text[:200],
        },
        rows,
    )


# ---------------------------------------------------------------------------
# Stage 6: end-to-end graph evaluation
# ---------------------------------------------------------------------------

def stage_e2e(store, qs, args):
    retriever = make_retriever(store)
    rows = []

    # IMPORTANT: use small_llm as the evaluator rather than generation_llm.
    # Check app/core/graph.py to ensure these are configured as distinct models.
    judge_llm = small_llm

    for i, q in enumerate(qs, start=1):
        print(f"  [{i}/{len(qs)}] {q['id']} {q['question'][:60]}")
        t0 = time.time()

        try:
            state = run_graph(
                q["question"],
                f"evale2e_{RUN_ID}_{q['id']}",
                "eval_user",
                retriever,
            )
            err = ""
        except Exception as exc:
            state = {}
            err = f"{type(exc).__name__}: {exc}"

        latency = round(time.time() - t0, 1)
        answer = state.get("answer", "")
        trace = state.get("trace", [])
        sources = state.get("sources", [])

        refused = (
            "couldn't find that information" in answer.lower()
            or "could not find that information" in answer.lower()
        )

        row = {
            "id": q["id"],
            "category": q["category"],
            "answerable": q["answerable"],
            "question": q["question"],
            "answer": answer,
            "refused": refused,
            "latency_s": latency,
            "error": err,
            "rewrites": sum("Rewrote query" in str(t) for t in trace),
            "regenerated": int(
                any("UNSUPPORTED" in str(t) for t in trace)
            ),
            "source_pages": [s.get("page") for s in sources],
            "citation_hit": (
                int(any(s.get("page") in q["gold_pages"] for s in sources))
                if q["answerable"] else ""
            ),
            "judge": "",
            "judge_error": "",
        }

        if q["answerable"] and answer and not err:
            verdict, judge_error = llm_call(
                judge_llm,
                JUDGE_PROMPT.format(
                    q=q["question"],
                    gold=q["gold_answer"],
                    ans=answer,
                ),
                args.sleep,
            )
            row["judge"] = (
                (verdict or "ERROR").split()[0].upper().strip(".")
            )
            row["judge_error"] = judge_error or ""

        rows.append(row)
        time.sleep(args.sleep)

    answerable_rows = [r for r in rows if r["answerable"]]
    unanswerable_rows = [r for r in rows if not r["answerable"]]

    summary = {
        "rerank": "OFF" if args.no_rerank else "ON",
        "judge_model_source": "small_llm (verify distinct from generation_llm)",
        "answerable n": len(answerable_rows),
        "judge CORRECT": pct(
            sum(r["judge"] == "CORRECT" for r in answerable_rows),
            len(answerable_rows),
        ),
        "judge PARTIAL": pct(
            sum(r["judge"] == "PARTIAL" for r in answerable_rows),
            len(answerable_rows),
        ),
        "judge INCORRECT": pct(
            sum(r["judge"] == "INCORRECT" for r in answerable_rows),
            len(answerable_rows),
        ),
        "judge ERROR/empty": sum(
            r["judge"] not in ("CORRECT", "PARTIAL", "INCORRECT")
            for r in answerable_rows
        ),
        "wrongly refused (answerable)": sum(
            r["refused"] for r in answerable_rows
        ),
        "gold page cited in sources": pct(
            sum(bool(r["citation_hit"]) for r in answerable_rows),
            len(answerable_rows),
        ),
        "unanswerable n": len(unanswerable_rows),
        "correctly refused (want high)": pct(
            sum(r["refused"] for r in unanswerable_rows),
            len(unanswerable_rows),
        ),
        "HALLUCINATED on unanswerable": [
            r["id"] for r in unanswerable_rows if not r["refused"]
        ],
        "avg latency s": round(
            statistics.mean(r["latency_s"] for r in rows), 1
        ) if rows else 0,
        "avg rewrites/question": round(
            statistics.mean(r["rewrites"] for r in rows), 2
        ) if rows else 0,
        "regenerate rate %": pct(
            sum(r["regenerated"] for r in rows), len(rows)
        ),
        "errors": [r["id"] for r in rows if r["error"]],
        "judge_errors": [
            r["id"] for r in rows if r["judge_error"]
        ],
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Dataset validation
# ---------------------------------------------------------------------------

def validate_dataset(data, pdf_path):
    """Fail fast on malformed data, duplicate IDs, or PDF/page mismatch."""
    if not isinstance(data, dict) or not isinstance(data.get("qa"), list):
        raise ValueError("Dataset must be a JSON object with a top-level 'qa' list.")
    qa = data["qa"]
    ids = [q.get("id") for q in qa if isinstance(q, dict)]
    if len(ids) != len(qa):
        raise ValueError("Every qa entry must be a JSON object.")
    duplicates = sorted({qid for qid in ids if ids.count(qid) > 1})
    if duplicates:
        raise ValueError(f"Duplicate QA IDs: {duplicates}")

    required = ("id", "category", "question", "answerable", "gold_answer", "gold_pages")
    for i, q in enumerate(qa, 1):
        missing = [key for key in required if key not in q]
        if missing:
            raise ValueError(f"QA item #{i} ({q.get('id', '<no id>')}) missing fields: {missing}")
        if not isinstance(q["question"], str) or not q["question"].strip():
            raise ValueError(f"QA item {q['id']}: question must be non-empty text.")
        if not isinstance(q["answerable"], bool):
            raise ValueError(f"QA item {q['id']}: answerable must be true/false.")
        if not isinstance(q["gold_pages"], list) or any(
            not isinstance(page, int) or page < 1 for page in q["gold_pages"]
        ):
            raise ValueError(f"QA item {q['id']}: gold_pages must be a list of positive PDF page numbers.")
        if q["answerable"]:
            if not q["gold_pages"] or not str(q["gold_answer"]).strip():
                raise ValueError(f"QA item {q['id']}: answerable items require gold_answer and gold_pages.")
        elif q["gold_pages"]:
            raise ValueError(f"QA item {q['id']}: unanswerable items must have empty gold_pages.")

    answerable = [q for q in qa if q["answerable"]]
    unanswerable = [q for q in qa if not q["answerable"]]
    if len(answerable) != 50:
        raise ValueError(f"Expected exactly 50 answerable QA items; found {len(answerable)}.")
    if len(unanswerable) < 1:
        raise ValueError("Expected a separate unanswerable QA set, but none was found.")

    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"Evaluation PDF not found: {pdf_path}")
    with open(pdf_path, "rb") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()
    expected_hash = data.get("source_pdf_sha256")
    if expected_hash and actual_hash != expected_hash:
        raise ValueError(
            "The evaluation PDF hash does not match source_pdf_sha256 in the dataset. "
            "Use the exact PDF used to author the gold answers/pages, or regenerate "
            "and manually verify the dataset."
        )

    if _PDF_BACKEND == "pymupdf":
        pdf_doc = fitz.open(pdf_path)
        page_count = len(pdf_doc)
        pdf_doc.close()
    else:
        page_count = len(PdfReader(pdf_path).pages)
    for q in answerable:
        invalid = [page for page in q["gold_pages"] if page > page_count]
        if invalid:
            raise ValueError(f"QA item {q['id']}: gold pages {invalid} exceed PDF page count {page_count}.")

    print(
        f"[dataset] valid: {len(answerable)} answerable + {len(unanswerable)} "
        f"unanswerable; {len(qa)} total; PDF pages={page_count}; "
        f"PDF SHA256={actual_hash[:12]}..."
    )
    return actual_hash


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        default="all",
        choices=[
            "all", "retrieval", "grader", "faith",
            "cache", "recall", "e2e",
        ],
    )
    parser.add_argument("--pdf", default="EN-Ethical_Hacking.pdf")
    parser.add_argument("--eval", default="eval_set.json")
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Validate dataset/PDF and exit without connecting to the evaluation index.",
    )
    parser.add_argument("--tag", default="run")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Smoke test: first N answerable and first N unanswerable questions",
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="e2e only: disable reranking in the graph",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Seconds between LLM calls",
    )
    parser.add_argument(
        "--allow-offline",
        action="store_true",
        help="Allow offline hashing embeddings (retrieval scores will be poor)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.eval):
        sys.exit(f"Evaluation set not found: {args.eval}")
    with open(args.eval, encoding="utf-8") as f:
        data = json.load(f)
    try:
        pdf_hash = validate_dataset(data, args.pdf)
    except Exception as exc:
        sys.exit(f"DATASET VALIDATION FAILED: {type(exc).__name__}: {exc}")
    if args.validate_only:
        print("[dataset] validation-only mode complete; no database/index touched.")
        return

    print("[preload] starting model warm-up ...")
    for thread in _preload_threads:
        thread.start()
    t0 = time.time()
    for thread in _preload_threads:
        thread.join()
    print(f"[preload] all models ready in {time.time() - t0:.1f}s")
    if _preload_errors:
        print(f"[preload] warnings: {_preload_errors}")

    mode = get_embedding_mode_label()
    print(f"[env] embeddings: {mode}")
    if mode == "Offline Fallback" and not args.allow_offline:
        sys.exit(
            "Embeddings fell back to the offline hashing model; retrieval "
            "results would be meaningless. Fix the embedding setup or pass "
            "--allow-offline to run anyway."
        )

    if args.no_rerank:
        graph_module.rerank_documents = (
            lambda query, documents, **kwargs:
            documents[:kwargs.get("top_n", 4)]
        )
        print("[env] reranker disabled for the graph")

    qs = data["qa"]
    answerable_qs = [q for q in qs if q["answerable"]]
    unanswerable_qs = [q for q in qs if not q["answerable"]]

    if args.limit:
        answerable_qs = answerable_qs[:args.limit]
        unanswerable_qs = unanswerable_qs[:args.limit]

    qa_by_id = {q["id"]: q for q in qs}

    store = ensure_index(args.pdf)

    out_dir = os.path.join(
        "results",
        f"{datetime.now():%Y%m%d_%H%M%S}_{args.tag}",
    )
    os.makedirs(out_dir, exist_ok=True)

    # Fixed: the grader stage must call the function that actually exists.
    plan = {
        "retrieval": lambda: stage_retrieval(store, answerable_qs, args),
        "grader": lambda: stage_grader_batch(store, answerable_qs, args),
        "faith": lambda: stage_faith(
            store, data["faithfulness_tests"], args
        ),
        "cache": lambda: stage_cache(data["cache_tests"]),
        "recall": lambda: stage_recall(store, qa_by_id, args),
        "e2e": lambda: stage_e2e(
            store, answerable_qs + unanswerable_qs, args
        ),
    }

    stages = list(plan) if args.stage == "all" else [args.stage]

    results = {
        "meta": {
            "tag": args.tag,
            "time": datetime.now().isoformat(),
            "dataset_file": os.path.abspath(args.eval),
            "dataset_sha256": hashlib.sha256(open(args.eval, "rb").read()).hexdigest(),
            "source_pdf": os.path.abspath(args.pdf),
            "source_pdf_sha256": pdf_hash,
            "database": "evaluation database (verified at runtime)",
            "embeddings": mode,
            "pdf_backend": _PDF_BACKEND,
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
            "rerank_candidates": RERANK_CANDIDATE_K,
            "graph_rerank": "off" if args.no_rerank else "on",
            "n_answerable": len(answerable_qs),
            "n_unanswerable": len(unanswerable_qs),
        }
    }

    stage_errors = []
    for name in stages:
        print(f"\n=== stage: {name} ===")
        t0 = time.time()

        try:
            summary, rows = plan[name]()
        except Exception as exc:
            print(
                f"  stage {name} crashed: "
                f"{type(exc).__name__}: {exc}"
            )
            error_text = f"{type(exc).__name__}: {exc}"
            results[name] = {"crashed": error_text}
            stage_errors.append((name, error_text))
            continue

        results[name] = summary
        write_csv(os.path.join(out_dir, f"{name}.csv"), rows)
        print(f"  ({time.time() - t0:.0f}s)")
        for key, value in summary.items():
            print(f"  {key}: {value}")

    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nSaved to {out_dir}/ (summary.json + stage CSV files)")
    if stage_errors:
        print("\nERROR: one or more evaluation stages failed:")
        for name, error_text in stage_errors:
            print(f"  {name}: {error_text}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()