"""
run_eval.py  --  one-command evaluation for the Agentic RAG project.

Put this file, eval_set.json and EN-Ethical_Hacking.pdf in the SAME folder
as graph.py / config.py / document_processor.py / reranker.py, then:

    python run_eval.py --stage retrieval          # no LLM calls, ~20-40s after index exists
    python run_eval.py --stage grader
    python run_eval.py --stage faith
    python run_eval.py --stage cache
    python run_eval.py --stage recall
    python run_eval.py --stage e2e --tag rerank_on
    python run_eval.py --stage e2e --tag rerank_off --no-rerank
    python run_eval.py                            # everything

It calls your code directly (no server, no login). It uses its OWN databases
(the Postgres in EVAL_DATABASE_URL) so your real app data is never touched.
Results go to results/<timestamp>_<tag>/ (summary.json + one CSV per stage).

Speed notes:
- PyMuPDF (fitz) is used instead of pypdf -- roughly 10-50x faster on PDFs
  with a bad xref table like EN-Ethical_Hacking.pdf.
- Both ML models are preloaded in parallel background threads on startup.
- The eval index lives in EVAL_DATABASE_URL; the PDF is only re-indexed if that collection is empty.
"""

import argparse
import csv
import json
import os
import random
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime

import re
from app.rag.parsers import parse_file
from app.rag.document_processor import add_org_chunks, build_org_retriever, get_org_store, _get_pg_connection

EVAL_ORG, EVAL_CID = "eval_org", "eval_handbook"


# Evals must NEVER run against your dev/prod Supabase: they write thousands of chunks and
# create/delete collections. Point EVAL_DATABASE_URL at a throwaway Postgres with pgvector, e.g.
#   docker run -d --name evaldb -e POSTGRES_PASSWORD=test -p 5433:5432 pgvector/pgvector:pg16
#   export EVAL_DATABASE_URL="postgresql://postgres:test@localhost:5433/postgres"
_eval_db = os.environ.get("EVAL_DATABASE_URL")
if not _eval_db:
    sys.exit("Set EVAL_DATABASE_URL to a throwaway Postgres (see the comment above). Refusing to touch DATABASE_URL.")
os.environ["DATABASE_URL"] = _eval_db
os.environ["IS_PG"] = "true"

# ---------------------------------------------------------------------------
# Parallel model preload -- kicks off BEFORE the heavy imports so downloads
# happen while everything else is still loading. This is the single biggest
# first-run speedup: FlashRank + MiniLM download concurrently, not serially.
# ---------------------------------------------------------------------------
_preload_errors: list = []


def _preload_flashrank():
    try:
        from flashrank import Ranker

        t0 = time.time()
        Ranker(model_name="ms-marco-MiniLM-L-12-v2")
        print(f"[preload] flashrank ready in {time.time() - t0:.1f}s")
    except Exception as exc:  # noqa: BLE001
        _preload_errors.append(f"flashrank: {exc}")
        print(f"[preload] flashrank unavailable: {exc}")


def _preload_minilm():
    try:
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        print(f"[preload] minilm ready in {time.time() - t0:.1f}s")
    except Exception as exc:  # noqa: BLE001
        _preload_errors.append(f"minilm: {exc}")
        print(f"[preload] minilm unavailable: {exc}")


_preload_threads = [
    threading.Thread(target=_preload_flashrank, daemon=True),
    threading.Thread(target=_preload_minilm, daemon=True),
]
for _t in _preload_threads:
    _t.start()

# Now the real imports -- they'll overlap with the downloads above.
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage

try:
    import fitz  # PyMuPDF

    _PDF_BACKEND = "pymupdf"
except ImportError:
    from pypdf import PdfReader  # type: ignore

    _PDF_BACKEND = "pypdf"

from app.core import graph as graph_module
from app.config import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, RERANK_CANDIDATE_K
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

JUDGE_PROMPT = """You are grading a candidate answer against a reference answer.

Question: {q}
Reference answer: {gold}
Candidate answer: {ans}

Does the candidate agree with the reference and cover its key points?
Respond with exactly one word: CORRECT, PARTIAL or INCORRECT."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def pct(x, n):
    return round(100.0 * x / n, 1) if n else 0.0


def first_rank(pages, gold):
    for i, p in enumerate(pages, start=1):
        if p in gold:
            return i
    return None


def llm_call(llm, prompt, sleep):
    err = None
    for attempt in range(3):
        try:
            text = llm.invoke([HumanMessage(content=prompt)]).content.strip()
            time.sleep(sleep)
            return text, None
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            time.sleep(3 * (attempt + 1))
    return None, err


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Fast PDF extraction -- PyMuPDF is dramatically faster than pypdf on
# malformed PDFs, and pulls text from all pages in one C-level pass.
# ---------------------------------------------------------------------------
def _extract_pages(pdf_path, pdf_backend):
    if pdf_backend == "pymupdf":
        print(f"[index] reading {pdf_path} with PyMuPDF ...")
        t0 = time.time()
        try:
            doc = fitz.open(pdf_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[index] PyMuPDF failed ({exc}) -- falling back to pypdf for this file")
            return _extract_pages_pypdf(pdf_path)
        pages = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text() or ""
            if text.strip():
                pages.append(Document(
                    page_content=text,
                    metadata={"source": os.path.basename(pdf_path), "page": i},
                ))
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
            pages.append(Document(
                page_content=text,
                metadata={"source": os.path.basename(pdf_path), "page": i},
            ))
    print(f"[index] extracted {len(pages)} pages in {time.time() - t0:.1f}s")
    return pages




def _eval_rows(sql, params=()):
    with _get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def ensure_index(pdf_path):
    try:
        n = _eval_rows("SELECT COUNT(*) AS n FROM langchain_pg_embedding WHERE cmetadata->>'collection_id' = %s",
                    (EVAL_CID,))[0]["n"]
    except Exception:
        n = 0
    if n:
        print(f"[index] reusing eval index ({n} chunks)")
    else:
        with open(pdf_path, "rb") as f:
            docs, _ = parse_file(os.path.basename(pdf_path), f.read())   # production parser
        chunks = chunk_documents(docs, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP)
        doc_id = uuid.uuid4().hex
        for c in chunks:
            c.metadata.update(org_id=EVAL_ORG, collection_id=EVAL_CID, doc_id=doc_id,
                              source=os.path.basename(pdf_path))
        add_org_chunks(chunks)
        print(f"[index] indexed {len(chunks)} chunks")
    return build_org_retriever(EVAL_ORG, [EVAL_CID], k=RERANK_CANDIDATE_K)   # the hybrid retriever


def make_retriever(retriever):
    return retriever                                   # ensure_index already returns it


def chunks_for_pages(_unused, pages):
    rows = _eval_rows(
        """SELECT e.document, e.cmetadata FROM langchain_pg_embedding e
           WHERE e.cmetadata->>'collection_id' = %s
             AND (e.cmetadata->>'page')::int = ANY(%s)""",
        (EVAL_CID, list(pages)))
    return [Document(page_content=r["document"], metadata=r["cmetadata"]) for r in rows]


def _eval_pages():
    return sorted(r["p"] for r in _eval_rows(
        "SELECT DISTINCT (cmetadata->>'page')::int AS p FROM langchain_pg_embedding "
        "WHERE cmetadata->>'collection_id' = %s", (EVAL_CID,)))



def run_graph(question, thread_id, user_id, retriever):
    gi = {
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
        gi["search_query"] = ""
    cfg = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "retriever": retriever,
            "top_k": 4,
            "force_offline": False,
        }
    }
    return compiled_graph.invoke(gi, config=cfg)


# ---------------------------------------------------------------------------
# Stage 1: retrieval + reranker (no LLM calls)
# ---------------------------------------------------------------------------
def stage_retrieval(store, qs, args):
    rows = []
    for q in qs:
        gold = set(q["gold_pages"])
        cands = store.invoke(q["question"])
        vec_pages = [d.metadata["page"] for d in cands]
        rr_pages = [
            d.metadata["page"]
            for d in rerank_documents(q["question"], cands, top_n=len(cands))
        ]

        def metrics(pages):
            r = first_rank(pages, gold)
            return {
                "hit4": int(any(p in gold for p in pages[:4])),
                "recall4": len(gold & set(pages[:4])) / len(gold),
                "hit15": int(any(p in gold for p in pages)),
                "rr": (1.0 / r) if r else 0.0,
                "rank": r or 0,
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
        "hit@15 (ceiling: reranker cannot fix a miss here)": pct(
            sum(x["hit15"] for x in rows), n
        ),
        "vector  Hit@4": pct(sum(x["vec_hit4"] for x in rows), n),
        "rerank  Hit@4": pct(sum(x["rerank_hit4"] for x in rows), n),
        "vector  Recall@4": round(
            100 * statistics.mean(x["vec_recall4"] for x in rows), 1
        ),
        "rerank  Recall@4": round(
            100 * statistics.mean(x["rerank_recall4"] for x in rows), 1
        ),
        "vector  MRR": round(statistics.mean(x["vec_mrr"] for x in rows), 3),
        "rerank  MRR": round(statistics.mean(x["rerank_mrr"] for x in rows), 3),
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
# Stage 2: grader in isolation
# ---------------------------------------------------------------------------
# def stage_grader(store, qs, args):
#     rng = random.Random(7)
#     all_pages = sorted(
#         {m["page"] for m in store.get(include=["metadatas"])["metadatas"]}
#     )
#     rows = []
#     for q in qs:
#         gold = q["gold_pages"]
#         gold_chunk = chunks_for_pages(store, gold[:1])
#         far = [p for p in all_pages if all(abs(p - g) >= 40 for g in gold)]
#         far_chunk = chunks_for_pages(store, [rng.choice(far)]) if far else []
#         cases = []
#         if gold_chunk:
#             cases.append(("gold", True, gold_chunk[0]))
#         for hn in q["hard_negative_pages"][:1]:
#             hc = chunks_for_pages(store, [hn])
#             if hc:
#                 cases.append(("hard_negative", False, hc[0]))
#         if far_chunk:
#             cases.append(("far_random", False, far_chunk[0]))
#         for kind, expected, doc in cases:
#             verdict, err = llm_call(
#                 small_llm,
#                 _GRADER_PROMPT.format(
#                     question=q["question"], passage=doc.page_content
#                 ),
#                 args.sleep,
#             )
#             said = (
#                 None
#                 if verdict is None
#                 else (
#                     "RELEVANT" in verdict.upper()
#                     and "IRRELEVANT" not in verdict.upper()
#                 )
#             )
#             rows.append(
#                 {
#                     "id": q["id"],
#                     "kind": kind,
#                     "page": doc.metadata["page"],
#                     "expected_relevant": expected,
#                     "grader_said_relevant": said,
#                     "correct": (said == expected) if said is not None else None,
#                     "error": err or "",
#                 }
#             )
#     ok = [r for r in rows if r["correct"] is not None]

#     def rate(kind, want):
#         sub = [r for r in ok if r["kind"] == kind]
#         return (
#             pct(sum(1 for r in sub if r["grader_said_relevant"] == want), len(sub)),
#             len(sub),
#         )

#     gold_acc, ng = rate("gold", True)
#     hn_rej, nh = rate("hard_negative", False)
#     far_rej, nf = rate("far_random", False)
#     summary = {
#         "gold chunks accepted (want high)": f"{gold_acc}%  (n={ng})",
#         "hard negatives rejected (want high)": f"{hn_rej}%  (n={nh})",
#         "far-random rejected (want ~100)": f"{far_rej}%  (n={nf})",
#         "overall accuracy": pct(sum(1 for r in ok if r["correct"]), len(ok)),
#         "failed LLM calls": len(rows) - len(ok),
#     }
#     return summary, rows







def stage_grader_batch(store, qs, args):
    rng = random.Random(7)
    all_pages = _eval_pages()
    rows = []
    for q in qs:
        gold = q["gold_pages"]
        gold_chunk = chunks_for_pages(store, gold[:1])
        if not gold_chunk:
            continue
        negs = []
        for hn in q["hard_negative_pages"][:2]:
            negs += chunks_for_pages(store, [hn])[:1]
        far = [p for p in all_pages if all(abs(p - g) >= 40 for g in gold)]
        if far:
            negs += chunks_for_pages(store, [rng.choice(far)])[:1]
        cands = [("gold", gold_chunk[0])] + [("neg", c) for c in negs]
        rng.shuffle(cands)
        passages = "\n\n".join(f"[Passage {i}]\n{d.page_content}" for i, (_, d) in enumerate(cands, 1))
        verdict, err = llm_call(small_llm, graph_module._BATCH_GRADER_PROMPT.format(
            question=q["question"], passages=passages), args.sleep)
        if verdict is None:
            continue
        keep = set() if "NONE" in verdict.upper() else {int(n) for n in re.findall(r"\d+", verdict)}
        for i, (kind, d) in enumerate(cands, 1):
            rows.append({"id": q["id"], "kind": kind, "page": d.metadata["page"],
                         "kept": i in keep, "correct": (i in keep) == (kind == "gold")})
    gold_r = [r for r in rows if r["kind"] == "gold"]
    neg_r = [r for r in rows if r["kind"] == "neg"]
    return {
        "gold kept (want high)": f"{pct(sum(r['kept'] for r in gold_r), len(gold_r))}%  (n={len(gold_r)})",
        "negatives rejected (want high)": f"{pct(sum(not r['kept'] for r in neg_r), len(neg_r))}%  (n={len(neg_r)})",
    }, rows

# ---------------------------------------------------------------------------
# Stage 3: faithfulness check in isolation
# ---------------------------------------------------------------------------
def stage_faith(store, tests, args):
    rows = []
    for t in tests:
        ctx = format_context(chunks_for_pages(store, t["context_pages"]))
        for kind, ans, want_unsupported in (
            ("good", t["good_answer"], False),
            ("bad", t["bad_answer"], True),
        ):
            verdict, err = llm_call(
                small_llm,
                _FAITHFULNESS_PROMPT.format(context=ctx, answer=ans),
                args.sleep,
            )
            said_unsupported = (
                None if verdict is None else ("UNSUPPORTED" in verdict.upper())
            )
            rows.append(
                {
                    "id": t["id"],
                    "kind": kind,
                    "bad_type": t["bad_type"] if kind == "bad" else "",
                    "expected_unsupported": want_unsupported,
                    "said_unsupported": said_unsupported,
                    "correct": (said_unsupported == want_unsupported)
                    if said_unsupported is not None
                    else None,
                    "error": err or "",
                }
            )
    ok = [r for r in rows if r["correct"] is not None]
    bad = [r for r in ok if r["kind"] == "bad"]
    good = [r for r in ok if r["kind"] == "good"]
    summary = {
        "bad answers caught (want high)": f"{pct(sum(r['correct'] for r in bad), len(bad))}%  (n={len(bad)})",
        "good answers wrongly flagged (want 0)": f"{pct(sum(1 for r in good if not r['correct']), len(good))}%  (n={len(good)})",
        "missed bad answers": [
            f"{r['id']} ({r['bad_type']})" for r in bad if not r["correct"]
        ],
        "failed LLM calls": len(rows) - len(ok),
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Stage 4: semantic cache
# ---------------------------------------------------------------------------
def raw_similarity(question, thread_id):
    store = get_cache_store()
    try:
        res = store.similarity_search_with_score(
            question, k=1, filter={"thread_id": thread_id}
        )
    except Exception:  # noqa: BLE001
        return None
    return round(1 - res[0][1], 3) if res else None


def stage_cache(tests):
    rows = []
    for t in tests:
        tid_a = f"evalcache_{RUN_ID}_{t['id']}_A"
        tid_b = f"evalcache_{RUN_ID}_{t['id']}_B"
        write_cache(t["setup_question"], "dummy cached answer", [], thread_id=tid_a)
        lookup_thread = tid_b if t["id"] == "C5" else tid_a
        sim = raw_similarity(t["test_question"], lookup_thread)
        hit = check_cache(t["test_question"], thread_id=lookup_thread) is not None
        exp = t["expect"]
        if exp == "HIT":
            verdict = "PASS" if hit else "FAIL"
        elif exp.startswith("MISS"):
            verdict = "PASS" if not hit else "FAIL (WRONG ANSWER WOULD BE SERVED)"
        else:
            verdict = "INFO"
        rows.append(
            {
                "id": t["id"],
                "setup": t["setup_question"],
                "test": t["test_question"],
                "expected": exp,
                "got": "HIT" if hit else "MISS",
                "similarity": sim,
                "verdict": verdict,
            }
        )
    summary = {
        r["id"]: f"{r['verdict']}  (got {r['got']}, similarity={r['similarity']})"
        for r in rows
    }
    return summary, rows


# ---------------------------------------------------------------------------
# Stage 5: cross-thread recall + user isolation
# ---------------------------------------------------------------------------
def stage_recall(store, qa_by_id, args):
    retriever = make_retriever(store)
    user_a, user_b = f"evaluser_a_{RUN_ID}", f"evaluser_b_{RUN_ID}"
    t1 = f"evalrecall_{RUN_ID}_t1"
    for qid in ("Q17", "Q18"):
        run_graph(qa_by_id[qid]["question"], t1, user_a, retriever)
    values = (
        compiled_graph.get_state({"configurable": {"thread_id": t1}}).values or {}
    )
    title, summary = summarize_thread_for_storage(
        values.get("raw_turns", []), values.get("running_summary", "")
    )
    save_thread_summary(t1, title, summary, user_a)

    ask = "What did we discuss in my previous chat?"
    r1 = run_graph(ask, f"evalrecall_{RUN_ID}_t2", user_a, retriever)
    r2 = run_graph(ask, f"evalrecall_{RUN_ID}_t3", user_b, retriever)
    a1, a2 = r1.get("answer", ""), r2.get("answer", "")
    kw = ("session", "hijack", "spoof")
    rows = [
        {
            "id": "R1",
            "user": "A (finalized thread exists)",
            "tool_used": r1.get("tool_used"),
            "answer": a1[:300],
            "verdict": "PASS"
            if r1.get("tool_used") == "Conversation Memory"
            and any(k in a1.lower() for k in kw)
            else "FAIL",
        },
        {
            "id": "R2/R3",
            "user": "B (no past threads)",
            "tool_used": r2.get("tool_used"),
            "answer": a2[:300],
            "verdict": "PASS"
            if not any(k in a2.lower() for k in kw)
            else "FAIL (LEAK ACROSS USERS)",
        },
    ]
    return {
        r["id"]: r["verdict"] for r in rows
    } | {"saved_title": title, "saved_summary": summary[:200]}, rows


# ---------------------------------------------------------------------------
# Stage 6: end-to-end through the real graph
# ---------------------------------------------------------------------------
def stage_e2e(store, qs, args):
    retriever = make_retriever(store)
    rows = []
    for i, q in enumerate(qs, start=1):
        print(f"  [{i}/{len(qs)}] {q['id']} {q['question'][:60]}")
        t0 = time.time()
        try:
            st = run_graph(
                q["question"], f"evale2e_{RUN_ID}_{q['id']}", "eval_user", retriever
            )
            err = ""
        except Exception as exc:  # noqa: BLE001
            st, err = {}, f"{type(exc).__name__}: {exc}"
        latency = round(time.time() - t0, 1)
        ans = st.get("answer", "")
        trace = st.get("trace", [])
        sources = st.get("sources", [])
        refused = "couldn't find that information" in ans.lower()
        row = {
            "id": q["id"],
            "category": q["category"],
            "answerable": q["answerable"],
            "question": q["question"],
            "answer": ans,
            "refused": refused,
            "latency_s": latency,
            "error": err,
            "rewrites": sum("Rewrote query" in t for t in trace),
            "regenerated": int(any("UNSUPPORTED" in t for t in trace)),
            "source_pages": [s.get("page") for s in sources],
            "citation_hit": int(
                any(s.get("page") in q["gold_pages"] for s in sources)
            )
            if q["answerable"]
            else "",
            "judge": "",
        }
        if q["answerable"] and ans and not err:
            v, _ = llm_call(
                generation_llm,
                JUDGE_PROMPT.format(
                    q=q["question"], gold=q["gold_answer"], ans=ans
                ),
                args.sleep,
            )
            row["judge"] = (v or "ERROR").split()[0].upper().strip(".")
        rows.append(row)
        time.sleep(args.sleep)

    ans_rows = [r for r in rows if r["answerable"]]
    un_rows = [r for r in rows if not r["answerable"]]
    summary = {
        "rerank": "OFF" if args.no_rerank else "ON",
        "answerable n": len(ans_rows),
        "judge CORRECT": pct(
            sum(r["judge"] == "CORRECT" for r in ans_rows), len(ans_rows)
        ),
        "judge PARTIAL": pct(
            sum(r["judge"] == "PARTIAL" for r in ans_rows), len(ans_rows)
        ),
        "judge INCORRECT": pct(
            sum(r["judge"] == "INCORRECT" for r in ans_rows), len(ans_rows)
        ),
        "wrongly refused (answerable but 'couldn't find')": sum(
            r["refused"] for r in ans_rows
        ),
        "gold page cited in sources": pct(
            sum(bool(r["citation_hit"]) for r in ans_rows), len(ans_rows)
        ),
        "unanswerable n": len(un_rows),
        "correctly refused (want 100)": pct(
            sum(r["refused"] for r in un_rows), len(un_rows)
        ),
        "HALLUCINATED on unanswerable": [
            r["id"] for r in un_rows if not r["refused"]
        ],
        "avg latency s": round(
            statistics.mean(r["latency_s"] for r in rows), 1
        )
        if rows
        else 0,
        "avg rewrites/question": round(
            statistics.mean(r["rewrites"] for r in rows), 2
        )
        if rows
        else 0,
        "regenerate rate %": pct(
            sum(r["regenerated"] for r in rows), len(rows)
        ),
        "errors": [r["id"] for r in rows if r["error"]],
    }
    return summary, rows


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        default="all",
        choices=[
            "all",
            "retrieval",
            "grader",
            "faith",
            "cache",
            "recall",
            "e2e",
        ],
    )
    ap.add_argument("--pdf", default="EN-Ethical_Hacking.pdf")
    ap.add_argument("--eval", default="eval_set.json")
    ap.add_argument("--tag", default="run")
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only first N answerable + N unanswerable questions (smoke test)",
    )
    ap.add_argument(
        "--no-rerank",
        action="store_true",
        help="e2e only: skip the reranker (keep top-k from vector order)",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="seconds between LLM calls (raise if you hit Groq 429s)",
    )
    ap.add_argument(
        "--allow-offline",
        action="store_true",
        help="allow the offline hashing embedder (results will be poor)",
    )
    args = ap.parse_args()

    # Wait for preload threads so we get clean logs and know if anything failed.
    print("[preload] waiting for models ...")
    t0 = time.time()
    for t in _preload_threads:
        t.join()
    print(f"[preload] all models ready in {time.time() - t0:.1f}s")
    if _preload_errors:
        print(f"[preload] warnings: {_preload_errors}")

    mode = get_embedding_mode_label()
    print(f"[env] embeddings: {mode}")
    if mode == "Offline Fallback" and not args.allow_offline:
        sys.exit(
            "Embeddings fell back to the offline hashing model - retrieval numbers would be meaningless. "
            "Fix HuggingFace access, or pass --allow-offline to run anyway."
        )

    if args.no_rerank:
        graph_module.rerank_documents = lambda query, documents, **kw: documents[
            : kw.get("top_n", 4)
        ]
        print("[env] reranker DISABLED for the graph (retrieval stage still compares both)")

    data = json.load(open(args.eval, encoding="utf-8"))
    qs = data["qa"]
    ans_q = [q for q in qs if q["answerable"]]
    un_q = [q for q in qs if not q["answerable"]]
    if args.limit:
        ans_q, un_q = ans_q[: args.limit], un_q[: args.limit]
    qa_by_id = {q["id"]: q for q in data["qa"]}

    store = ensure_index(args.pdf)
    out_dir = os.path.join(
        "results", f"{datetime.now():%Y%m%d_%H%M%S}_{args.tag}"
    )
    os.makedirs(out_dir, exist_ok=True)

    plan = {
        "retrieval": lambda: stage_retrieval(store, ans_q, args),
        "grader": lambda: stage_grader(store, ans_q, args),
        "faith": lambda: stage_faith(store, data["faithfulness_tests"], args),
        "cache": lambda: stage_cache(data["cache_tests"]),
        "recall": lambda: stage_recall(store, qa_by_id, args),
        "e2e": lambda: stage_e2e(store, ans_q + un_q, args),
    }
    stages = list(plan) if args.stage == "all" else [args.stage]

    results = {
        "meta": {
            "tag": args.tag,
            "time": datetime.now().isoformat(),
            "embeddings": mode,
            "pdf_backend": _PDF_BACKEND,
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
            "rerank_candidates": RERANK_CANDIDATE_K,
            "graph_rerank": "off" if args.no_rerank else "on",
            "n_answerable": len(ans_q),
            "n_unanswerable": len(un_q),
        }
    }
    for name in stages:
        print(f"\n=== stage: {name} ===")
        t0 = time.time()
        try:
            summary, rows = plan[name]()
        except Exception as exc:  # noqa: BLE001
            print(f"  stage {name} crashed: {type(exc).__name__}: {exc}")
            results[name] = {"crashed": f"{type(exc).__name__}: {exc}"}
            continue
        results[name] = summary
        write_csv(os.path.join(out_dir, f"{name}.csv"), rows)
        print(f"  ({time.time() - t0:.0f}s)")
        for k, v in summary.items():
            print(f"  {k}: {v}")

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved to {out_dir}/  (summary.json + one CSV per stage)")


if __name__ == "__main__":
    main()