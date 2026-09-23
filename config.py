"""
config.py
---------
Central place for environment variables, model choices, and logging setup.
Every other module imports from here instead of reading os.environ directly,
so all configuration lives in exactly one place.
"""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Production apps should never rely on print(). A configured logger gives us
# timestamps, log levels, and the ability to redirect output (e.g. to a file
# or a log aggregator) without touching application code.

def configure_logging() -> logging.Logger:
    logger = logging.getLogger("agentic_rag")

    if logger.handlers:
        # Avoid attaching duplicate handlers if this is called more than
        # once (Streamlit reruns the whole script on every interaction).
        return logger

    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


logger = configure_logging()


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    # Fail fast and loud at import time rather than deep inside a request.
    logger.error("GROQ_API_KEY is not set. Add it to your .env file.")
    raise EnvironmentError("GROQ_API_KEY is not set")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# When HuggingFace is unreachable (blocked network, rate limit with no way
# to authenticate), set this True to skip trying HF entirely and go
# straight to the offline fallback embedder -- avoids a slow hang/timeout
# on every attempt. Also toggleable live from the sidebar.
FORCE_OFFLINE_EMBEDDINGS = os.getenv("FORCE_OFFLINE_EMBEDDINGS", "false").lower() == "true"

# Cascading model routing: a small, fast, cheap model handles lightweight
# tasks (grading retrieved chunks, rewriting queries, summarizing history).
# The larger model is reserved for the final answer the user actually reads.
SMALL_MODEL = "openai/gpt-oss-20b"
GENERATION_MODEL = "openai/gpt-oss-120b"


# --------------------------------------------------------------------------
# Chunking defaults (overridable from the Streamlit sidebar)
# --------------------------------------------------------------------------
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_TOP_K = 4

# How many raw conversation turns to keep verbatim before compressing older
# ones into the running summary. One "turn" = one user message + one
# assistant reply.
MEMORY_WINDOW_TURNS = 4

# Max query-rewrite attempts inside the agentic loop before we give up and
# tell the user we couldn't find an answer, instead of looping forever.
MAX_QUERY_REWRITES = 2


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store")
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "./app_data.db")

# Global collection holding a summary of every finalized thread, searched
# when a question is asking to recall a PAST conversation rather than the
# current thread's documents.
MEMORY_COLLECTION_NAME = "conversation_memory"

# Semantic cache of (question -> answer) pairs, scoped per-thread (see
# rag_engine.py) so a cached answer from one thread's documents never gets
# served to a different thread with different documents.
CACHE_COLLECTION_NAME = "query_cache"

# Cosine similarity above which a past question counts as "the same
# question" for cache purposes. High on purpose -- this must only fire on
# near-duplicates, not just topically similar questions.
CACHE_SIMILARITY_THRESHOLD = 0.93