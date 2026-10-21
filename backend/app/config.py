
"""
config.py
---------
Central place for environment variables, model choices, and logging setup.
Every other module imports from here instead of reading os.environ directly.
"""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def configure_logging() -> logging.Logger:
    logger = logging.getLogger("agentic_rag")

    if logger.handlers:
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
# LangSmith observability
# --------------------------------------------------------------------------
# Tracing is opt-in. LangChain integrations generally read these environment
# variables directly. Do not place the API key in source control.
LANGSMITH_TRACING = os.getenv(
    "LANGSMITH_TRACING", "false"
).lower() in ("true", "1", "yes")

LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY")
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "agentic-rag")

if LANGSMITH_TRACING and not LANGSMITH_API_KEY:
    logger.warning(
        "LANGSMITH_TRACING is enabled but LANGSMITH_API_KEY is missing; "
        "remote tracing may not work"
    )


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    logger.error("GROQ_API_KEY is not set. Add it to your .env file.")
    raise EnvironmentError("GROQ_API_KEY is not set")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)

FORCE_OFFLINE_EMBEDDINGS = (
    os.getenv("FORCE_OFFLINE_EMBEDDINGS", "false").lower() == "true"
)

SMALL_MODEL = "openai/gpt-oss-20b"
GENERATION_MODEL = "openai/gpt-oss-120b"


# --------------------------------------------------------------------------
# Chunking defaults
# --------------------------------------------------------------------------

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_TOP_K = 4

MEMORY_WINDOW_TURNS = 4
MAX_QUERY_REWRITES = 2


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store")
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "./app_data.db")

MEMORY_COLLECTION_NAME = "conversation_memory"
CACHE_COLLECTION_NAME = "query_cache"

CACHE_SIMILARITY_THRESHOLD = 0.93


# --------------------------------------------------------------------------
# JWT configuration
# --------------------------------------------------------------------------

JWT_SECRET_KEY = os.environ.get(
    "JWT_SECRET_KEY",
    "change-me-in-production",
)

_DEFAULT_JWT_SECRET = "change-me-in-production"
APP_ENV = os.getenv("APP_ENV", "dev")

if JWT_SECRET_KEY == _DEFAULT_JWT_SECRET:
    if APP_ENV == "production":
        raise EnvironmentError(
            "JWT_SECRET_KEY is the default placeholder; set a real secret "
            "(openssl rand -hex 32)"
        )
    logger.warning("Using the default JWT secret -- fine for local dev only")

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24


# --------------------------------------------------------------------------
# Reranking
# --------------------------------------------------------------------------

RERANK_MODEL = "ms-marco-MiniLM-L-12-v2"
RERANK_CANDIDATE_K = 15


# --------------------------------------------------------------------------
# Ingestion limits
# --------------------------------------------------------------------------

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_EXTRACTED_CHARS = int(
    os.getenv("MAX_EXTRACTED_CHARS", "5000000")
)
MAX_CHUNKS_PER_DOC = int(os.getenv("MAX_CHUNKS_PER_DOC", "6000"))
MAX_ZIP_UNCOMPRESSED_MB = int(
    os.getenv("MAX_ZIP_UNCOMPRESSED_MB", "150")
)
MAX_XLSX_ROWS_PER_SHEET = int(
    os.getenv("MAX_XLSX_ROWS_PER_SHEET", "20000")
)


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------

INJECTION_QUERY_ACTION = os.getenv("INJECTION_QUERY_ACTION", "drop")
MAX_QUESTION_CHARS = int(os.getenv("MAX_QUESTION_CHARS", "2000"))

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173",
    ).split(",")
    if origin.strip()
]

# Rate limits: (max_calls, window_seconds). These are per-process limits.
LOGIN_FAIL_LIMIT = (5, 15 * 60)
CHAT_LIMIT = (30, 60)
UPLOAD_LIMIT = (20, 60)
FEEDBACK_LIMIT = (60, 60)


# --------------------------------------------------------------------------
# Database configuration
# --------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
IS_PG = os.getenv("IS_PG", "True").lower() in ("true", "1", "yes")

SQLALCHEMY_URL = (
    DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    if DATABASE_URL and DATABASE_URL.startswith("postgresql://")
    else DATABASE_URL
)


# --------------------------------------------------------------------------
# Guest chat
# --------------------------------------------------------------------------

ENABLE_GUEST_CHAT = (
    os.getenv("ENABLE_GUEST_CHAT", "false").lower() == "true"
)

GUEST_UPLOAD_LIMIT = (3, 60 * 60)
GUEST_QUESTION_LIMIT = (20, 60 * 60)
GUEST_MAX_UPLOAD_MB = int(os.getenv("GUEST_MAX_UPLOAD_MB", "10"))
GUEST_SESSION_TTL_MINUTES = int(
    os.getenv("GUEST_SESSION_TTL_MINUTES", "30")
)

GUEST_COLLECTION_PREFIX = "guest_upload_"
