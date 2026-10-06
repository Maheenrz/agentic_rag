"""
db.py
-----
One place for database connections.

  connect()           SQLAlchemy connection used by the store files (text SQL).
  get_db_connection() raw psycopg connection for pgvector/cleanup SQL.
  init_db()           one-time setup: pgvector extension + LangGraph checkpoint tables.
"""
import psycopg
from psycopg.rows import dict_row
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import DATABASE_URL, SQLALCHEMY_URL, IS_PG, logger

Base = declarative_base()

if SQLALCHEMY_URL:
    # prepare_threshold=None: never use server-side prepared statements. Supabase poolers
    # (and any PgBouncer in transaction mode) break them with "prepared statement does not exist".
    engine = create_engine(
        SQLALCHEMY_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        connect_args={"prepare_threshold": None},
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
else:
    engine = None
    SessionLocal = None


def connect():
    """Use as:  with connect() as session: session.execute(text(...))

    engine.begin() opens a transaction that COMMITS when the block ends normally
    and ROLLS BACK if the block raises. The previous engine.connect() never
    committed, so every INSERT/UPDATE/DELETE made through it was lost."""
    if not engine:
        raise ValueError("DATABASE_URL environment variable is missing.")
    return engine.begin()


def get_db_connection():
    """Returns a direct psycopg connection for raw SQL queries."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing. Check your .env file.")
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, prepare_threshold=None)


def run_script(sql_script: str) -> None:
    """Executes a raw SQL script or string using the SQLAlchemy engine."""
    if not engine:
        raise ValueError("DATABASE_URL environment variable is missing.")
    with engine.begin() as conn:
        conn.execute(text(sql_script))


def table_columns(table_name: str) -> list[str]:
    """Returns a list of column names for a given table name."""
    if not engine:
        return []
    inspector = inspect(engine)
    if inspector.has_table(table_name):
        return [col["name"] for col in inspector.get_columns(table_name)]
    return []


def init_db():
    """Creates the pgvector extension and the LangGraph checkpoint tables.
    Safe to run on every start: everything uses IF NOT EXISTS / setup() is idempotent."""
    if not IS_PG or not DATABASE_URL:
        logger.warning("Postgres is disabled or DATABASE_URL is not set. Skipping initialization.")
        return

    logger.info("Initializing database...")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            logger.info("Enabled pgvector extension.")

    from langgraph.checkpoint.postgres import PostgresSaver
    with get_db_connection() as conn:
        PostgresSaver(conn).setup()
        logger.info("LangGraph checkpoint tables are ready.")

    logger.info("Database initialization complete.")


if __name__ == "__main__":
    init_db()