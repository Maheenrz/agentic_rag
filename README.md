
<div align="center">

# Private Docs Assistant

### A secure, citation-first RAG assistant for organisational knowledge.

Ask questions across private documents. Get grounded answers with source references and supporting quotes — or a clear abstention when the documents do not contain the answer.

<br />

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi)
![React](https://img.shields.io/badge/Frontend-React-61DAFB?logo=react)
![Vite](https://img.shields.io/badge/Build-Vite-646CFF?logo=vite)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL%20%2B%20pgvector-4169E1?logo=postgresql)
![LangGraph](https://img.shields.io/badge/Orchestration-LangGraph-1C3C3C)

</div>

---

## Overview

Private Docs Assistant is a multi-user document question-answering application built around one principle: **answers should be traceable to the documents that support them.**

Organisations can upload policies, handbooks, reports, and spreadsheets into access-controlled collections. Staff can ask questions in natural language and receive an answer with source references and an exact supporting passage. If the available material does not support an answer, the assistant is designed to say so instead of filling the gap with a guess.

The project combines hybrid retrieval, reranking, a graph-based answer workflow, document-level access control, and layered input/output guardrails. It also includes a guest experience for asking a small number of questions about a visitor's own uploaded document.

> **Project status:** The project has Vercel and Azure deployment configuration. This source audit does not verify the currently deployed revision or live cloud health. Historical local evaluation results are reported below with their original scope.

## Highlights

- **Grounded answers** — responses are generated from retrieved document context and include source references and supporting quotes.
- **Hybrid search** — semantic vector retrieval and BM25 keyword retrieval are combined with Reciprocal Rank Fusion (RRF).
- **Reranking** — FlashRank reranks the candidate passages before answer generation.
- **Multi-tenant access control** — organisations, users, groups, and document collections have server-enforced access rules.
- **Conversation memory** — recent turns remain in the active context; older conversation history can be summarised and recalled.
- **Security guardrails** — upload validation, prompt-injection scanning, PII policies, query checks, retrieved-chunk filtering, and output sanitisation.
- **Guest document chat** — visitors can upload their own document and ask a limited number of questions without accessing organisational collections.
- **Admin workspace** — manage users, groups, collections, feedback, audit events, and organisation guardrail settings.
- **Answer feedback** — users can rate responses, while administrators can review feedback within the application's access rules.

## Product capabilities

| Area | Capabilities |
|---|---|
| Documents | PDF, DOCX, TXT, Markdown, HTML, and XLSX ingestion |
| Retrieval | MiniLM embeddings, BM25, RRF fusion, and FlashRank reranking |
| Answers | Context-grounded generation, faithfulness checking, source references, and quoted passages |
| Identity | Password hashing, HttpOnly-cookie JWT authentication, organisation membership, and role-aware routes |
| Administration | User and group management, collection controls, audit log, feedback review, and guardrail settings |
| Guest mode | Temporary personal document collection, streamed answers, question limits, and expiry cleanup |
| Persistence | PostgreSQL, pgvector, SQLAlchemy, and LangGraph PostgreSQL checkpoints |

## Architecture

```mermaid
flowchart TD
    U[React + Vite client] -->|HTTP / streamed chat| API[FastAPI application]
    API --> AUTH[Authentication and access checks]
    AUTH --> ORG[Organisation and collection scope]
    ORG --> FLOW[LangGraph answer workflow]

    FLOW --> GATE[Input and policy guardrails]
    GATE --> RET[Hybrid retrieval]
    RET --> VEC[Vector search · pgvector]
    RET --> BM[Keyword search · BM25]
    VEC --> FUSE[Reciprocal Rank Fusion]
    BM --> FUSE
    FUSE --> RERANK[FlashRank reranking]
    RERANK --> FILTER[Retrieved-content safety filter]
    FILTER --> GRADE[Relevance grading and query rewrite]
    GRADE --> GEN[LLM answer generation]
    GEN --> FAITH[Faithfulness check]
    FAITH --> OUT[Output guard and source assembly]
    OUT --> API

    API <--> DB[(PostgreSQL + pgvector)]
    FLOW <--> MEM[Conversation memory and checkpoints]
```

### Request lifecycle

1. **Authenticate and scope** — validate the caller and determine which organisation collections they are allowed to search.
2. **Apply input controls** — enforce rate limits, question-length limits, prompt-extraction checks, organisation topic policies, and configured PII handling.
3. **Retrieve context** — search permitted collections using vector similarity and BM25, then combine rankings with RRF.
4. **Rerank and filter** — rerank candidate passages and remove passages that trigger high-severity prompt-injection rules.
5. **Check relevance** — grade retrieved passages and, when needed, rewrite the query for another retrieval attempt (up to two rewrites).
6. **Generate and verify** — generate an answer from the selected context, run a faithfulness check, and allow one stricter regeneration before abstaining.
7. **Sanitise and cite** — apply output checks and attach source references and supporting quoted text.
8. **Update conversation context** — retain recent turns and maintain summaries for older conversation history.

Quoted passages are selected from retrieved source text rather than invented by the language model.

## Technology stack

| Layer | Tools |
|---|---|
| Frontend | React 18, Vite, React Router, Tailwind CSS, Lucide React |
| API | Python, FastAPI, Uvicorn |
| Agent workflow | LangGraph, LangChain |
| Language models | Groq — `openai/gpt-oss-20b` for smaller tasks and `openai/gpt-oss-120b` for answer generation |
| Embeddings | Sentence Transformers — `sentence-transformers/all-MiniLM-L6-v2` |
| Retrieval | pgvector, BM25 (`rank-bm25`), Reciprocal Rank Fusion |
| Reranking | FlashRank — `ms-marco-MiniLM-L-12-v2` |
| Persistence | PostgreSQL, pgvector, SQLAlchemy, LangGraph PostgreSQL checkpointing |
| Document parsing | PyMuPDF, pypdf, python-docx, openpyxl, Beautiful Soup |
| Testing and evaluation | pytest, custom retrieval/e2e evaluation runners, rule-based red-team suite |
| Containerisation | Docker, CPU-only PyTorch image |

## Security and data boundaries

Security is implemented in several stages rather than relying on the prompt alone.

### Document ingestion

- File-extension allow-list and file-signature (magic-byte) checks.
- Upload-size, extracted-text, archive-expansion, and spreadsheet-row limits.
- Hidden HTML content stripping.
- Per-section scanning for prompt-injection patterns, suspicious hidden Unicode, and secret-like values.
- High-severity findings can reject an upload; permitted overrides are restricted and audited.

### Question and retrieval time

- Login, chat, upload, and feedback rate limits.
- Checks for oversized questions and attempts to extract hidden instructions.
- Organisation-level blocked-topic, question-length, and PII handling policies.
- Configurable PII handling modes (allow, mask, or block) for supported patterns such as email addresses, payment-card numbers, CNICs, and SSNs.
- Retrieved passages that match high-severity injection rules are removed before generation.
- Collection access is checked on the server. The client cannot widen its own retrieval permissions.

### Answer and data lifecycle

- Retrieved document text is treated as untrusted content.
- Faithfulness verification with a stricter regeneration attempt before abstention.
- Output checks for remote images, unsupported links, secret-like values, canary leakage, and configured PII rules.
- Collection access failures use a not-found response to avoid exposing inaccessible collection identifiers.
- Document deletion and access revocation trigger related chunk, cache, and conversation-summary cleanup paths.
- Audit events cover important authentication, upload, access, policy, and security actions. Flagged-question entries store rule/reason metadata rather than raw question text.

**Security note:** These are application-level safeguards, not a claim of formal security certification. Pattern-based detection has limits; see [Known limitations](#known-limitations).

## Evaluation

Evaluation was run locally against a dedicated evaluation database using a 578-page ethical-hacking reference PDF. The evaluation set contains **50 answerable questions and 6 unanswerable questions**. The figures below are the recorded results from the October 21, 2026 runs; they are not a guarantee of performance on other document collections or workloads.

### Retrieval and answer quality

| Measurement | Result | Evaluation scope |
|---|---:|---|
| Candidate retrieval Hit@15 | **100%** | 50 answerable questions |
| Vector retrieval Hit@4 | 96% | 50 answerable questions |
| Reranked Hit@4 | **100%** | 50 answerable questions |
| Vector Recall@4 → reranked Recall@4 | 95% → **100%** | 50 answerable questions |
| MRR before → after reranking | 0.798 → **0.922** | 50 answerable questions |
| Answer judged correct | **96%** | 50 answerable questions |
| Gold page present in cited sources | **98%** | 50 answerable questions |
| Correctly refused unanswerable questions | **83.3%** | 6 unanswerable questions |
| Average end-to-end latency | 11.9 s | Same 50-answerable / 6-unanswerable run |

### Additional component checks

| Component check | Result | Scope |
|---|---:|---|
| Relevance grader overall accuracy | 97.7% | 50 positive and 82 negative examples |
| Faithfulness check caught unsupported answers | 7/7 | Small targeted set |
| Faithfulness check incorrectly flagged supported answers | 0/7 | Small targeted set |
| Prompt-injection cases obeyed with full guardrails | 1/20 | One run of 20 authored cases |
| Prompt-injection cases obeyed with guardrails disabled | 9/20 | Same 20-case comparison |

The red-team cases are hand-authored, and the injection comparison was run once. These results are useful engineering signals, not independent certification or a statistically broad security benchmark.

### Test and build status

**Previously recorded local verification (November 3, 2026; not reproduced in the November 4 source audit):**

| Check | Result |
|---|---:|
| Full backend pytest suite | **98 passed, 4 xfailed** |
| Frontend production build | **Passed** — 1,943 modules transformed |

The four `xfail` tests are marked as expected failures in the test suite. The run also emitted two deprecation warnings (Python's `crypt` module via Passlib and a deprecated Starlette status constant); neither caused a test failure. A later audit environment could not reproduce the full run because the throwaway PostgreSQL test database and several Python/Node dependencies were unavailable. See the release checklist before treating these checks as current.

## Local development

### Prerequisites

- Python 3.11 recommended for the backend container; local tests have also been run with Python 3.12.
- Node.js and npm.
- PostgreSQL with the `pgvector` extension.
- A Groq API key.

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd Agentic_rag
```

### 2. Configure the backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `backend/.env` file with values for your local environment:

```dotenv
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=postgresql://postgres:your_local_password@localhost:5432/agentic_rag
IS_PG=True
JWT_SECRET_KEY=replace_with_a_long_random_secret
APP_ENV=dev
CORS_ORIGINS=http://localhost:5173
```

Create the PostgreSQL database and enable pgvector before starting the API:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The application initialises the pgvector extension and LangGraph checkpoint tables when `python -m app.db` runs. The database user needs permission to create the extension if it is not already enabled.

Start the backend from the `backend` directory:

```bash
python -m app.db
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API documentation is available at `http://localhost:8000/docs` while the server is running. The health endpoint is `http://localhost:8000/health`.

### 3. Configure and run the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

The frontend calls the backend through `/api`. Vite proxies `/api` to `http://localhost:8000` during local development. `frontend/vercel.json` rewrites `/api/*` to the configured Azure Container App, keeping browser requests first-party so the HttpOnly cookie works with `SameSite=Lax`. If the backend URL changes, update the rewrite destination and redeploy the frontend.

### 4. Optional guest chat

Guest chat is disabled by default. To enable it for a local run, add this to `backend/.env`:

```dotenv
ENABLE_GUEST_CHAT=true
GUEST_MAX_UPLOAD_MB=10
GUEST_SESSION_TTL_MINUTES=30
```

Guest sessions use temporary collections, have configured upload/question limits, and expire after the configured TTL. A timed cleanup path removes expired guest collections.

## Running tests

Use a **dedicated throwaway PostgreSQL database** for tests. Do not point test variables at a production or personal database.

```bash
cd backend
source venv/bin/activate
export TEST_DATABASE_URL="postgresql://postgres:your_test_password@127.0.0.1:5433/agentic_rag_test"
python -m pytest -q
```

The database-backed tests read `TEST_DATABASE_URL` and configure the application to use that test database. Keep the test database separate from `DATABASE_URL` and never use production credentials for test runs.

## Running evaluations

The evaluation runner requires a separate, dedicated database through `EVAL_DATABASE_URL`. It includes runtime checks intended to prevent accidental use of an unsuitable database.

```bash
cd backend
export EVAL_DATABASE_URL="postgresql://postgres:your_eval_password@127.0.0.1:5434/agentic_rag_eval"
python evaluation/run_eval.py --help
```

Review the runner's options before launching a full evaluation. Full end-to-end runs call hosted language models and may incur API usage charges. Keep evaluation credentials separate from both development and production databases.

## Docker

The backend Dockerfile is in `backend/Dockerfile`. Build with the backend directory as the build context so `backend/.dockerignore` is applied:

```bash
docker build -t private-docs-assistant-api ./backend
```

The image uses CPU-only PyTorch and downloads the MiniLM embedding model and FlashRank model at build time so they are available when the container starts. The container initialises the database and launches Uvicorn on the port supplied by the hosting environment (default `8000`).

Configure secrets and database connection settings through the hosting platform's environment-variable settings. Do not copy `.env` into an image or commit it to source control.

## Repository layout

```text
.
├── backend/
│   ├── app/
│   │   ├── core/          # LangGraph workflow and model integration
│   │   ├── guardrails/    # Input, ingestion, and output safety checks
│   │   ├── rag/           # Parsing, chunking, embedding, retrieval
│   │   ├── routers/       # FastAPI endpoints
│   │   └── stores/        # Persistence and data-access layer
│   ├── evaluation/        # Evaluation datasets, runners, and results
│   ├── tests/             # Backend and integration tests
│   ├── Dockerfile
│   └── requirements.txt
└── frontend/
    ├── public/
    └── src/
        ├── components/
        ├── context/
        ├── lib/
        └── pages/
```

## Known limitations and next steps

- Prompt-injection and PII detection include pattern-based rules; they cannot identify every paraphrase, entity, or obfuscated attack.
- The evaluation set is based on one reference PDF. Results may differ on other formats, domains, languages, and document structures.
- The latest end-to-end evaluation still hallucinated on one unanswerable case (`Q26`); abstention is not perfect.
- The faithfulness component check is a small targeted set, not a broad independent benchmark.
- Rate limits are in-memory and therefore apply per application process; distributed deployments need a shared rate-limit store.
- Guest-session registry state is in memory, so multi-instance deployments and process restarts need durable/shared lifecycle coordination.
- BM25 indexing is in memory and is best suited to the current project scale, not an unbounded corpus.
- Authentication uses an HttpOnly cookie; browser JavaScript does not read or store the JWT. Theme preference is stored locally in the browser.
- Load, concurrency, and large-corpus performance have not been comprehensively benchmarked.

## Roadmap

- [x] Configure the API and frontend deployment targets; verify the deployed revision and production smoke tests after each release.
- [ ] Add shared rate limiting and durable guest-session lifecycle state for multi-instance hosting.
- [ ] Expand evaluation coverage across DOCX, XLSX, multilingual documents, and larger corpora.
- [ ] Add repeated and independently authored prompt-injection evaluations.
- [ ] Review token storage and session-hardening options for production.
- [ ] Add load and concurrency benchmarks.

## Design goals

This project explores how to make retrieval-augmented generation more useful in a realistic organisational setting: not only retrieving relevant passages, but also respecting access boundaries, exposing evidence, handling missing information, and measuring failure modes.

The emphasis is on **traceability, access control, defensive design, and honest evaluation** rather than treating a fluent answer as a correct answer.

---

<div align="center">

**Private Docs Assistant** · Built with React, FastAPI, LangGraph, and PostgreSQL.

</div>
