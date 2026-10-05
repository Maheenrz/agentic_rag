"""
org_store.py
-------------
The company layer: orgs, groups, collections, who-can-read-what (ACL),
document records, and a tiny settings table. SQLite, same file as the
other stores (SQLITE_DB_PATH). This file decides WHO may see WHICH
collection. It never touches Chroma -- document_processor.py does that,
and only ever with the collection ids this file approved.

Access rules (v0):
- admin of an org: can read every collection in that org, and manage everything.
- member: can read a collection if it is visibility='org', OR they are
  listed in its ACL (directly or via a group), OR it is their personal collection.
- write (upload/delete documents): admin, or the owner of a personal collection.
- Nothing ever crosses orgs: every query below filters on org_id.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from app.config import SQLITE_DB_PATH, logger


@contextmanager
def _connect():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orgs (
                org_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE (org_id, name)
            );
            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY (group_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS collections (
                collection_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'restricted',   -- 'org' | 'restricted'
                owner_user_id TEXT,                               -- set only for personal collections
                data_version INTEGER NOT NULL DEFAULT 0,          -- bumped on every doc change
                created_at REAL NOT NULL, UNIQUE (org_id, name)
            );
            CREATE TABLE IF NOT EXISTS collection_acl (
                collection_id TEXT NOT NULL, principal_type TEXT NOT NULL,   -- 'user' | 'group'
                principal_id TEXT NOT NULL, PRIMARY KEY (collection_id, principal_type, principal_id)
            );
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, org_id TEXT NOT NULL,
                filename TEXT NOT NULL, version INTEGER NOT NULL,
                status TEXT NOT NULL,                             -- 'active' | 'superseded' | 'deleted'
                content_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
                uploaded_by TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_docs_coll ON documents (collection_id, status);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            -- which collections a chat thread has drawn answers from; used to purge recall
            -- summaries when someone loses access to one of them
            CREATE TABLE IF NOT EXISTS thread_collections (
                thread_id TEXT NOT NULL, collection_id TEXT NOT NULL, PRIMARY KEY (thread_id, collection_id)
            );
            CREATE TABLE IF NOT EXISTS thread_documents (
            thread_id TEXT NOT NULL, doc_id TEXT NOT NULL, PRIMARY KEY (thread_id, doc_id)
            );
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        if "scan_json" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN scan_json TEXT NOT NULL DEFAULT '[]'")


_init_db()


# ---------------------------------------------------------------- orgs
def create_org(name: str) -> str:
    org_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute("INSERT INTO orgs (org_id, name, created_at) VALUES (?, ?, ?)", (org_id, name, time.time()))
    logger.info("Created org: %s (%s)", name, org_id)
    return org_id


def get_org(org_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT org_id, name FROM orgs WHERE org_id = ?", (org_id,)).fetchone()
    return dict(row) if row else None


# -------------------------------------------------------------- groups
def create_group(org_id: str, name: str) -> str:
    group_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (group_id, org_id, name.strip(), time.time()),
        )
    return group_id


def group_in_org(group_id: str, org_id: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ? AND org_id = ?", (group_id, org_id)
        ).fetchone() is not None


def add_group_member(group_id: str, user_id: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)", (group_id, user_id))


def remove_group_member(group_id: str, user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))


def delete_group(group_id: str) -> None:
    """Removes the group, its memberships, and every ACL row that granted
    access to it (otherwise those collections keep a dangling grant)."""
    with _connect() as conn:
        conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM collection_acl WHERE principal_type = 'group' AND principal_id = ?", (group_id,))
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))


def list_groups(org_id: str) -> list[dict]:
    with _connect() as conn:
        groups = conn.execute("SELECT group_id, name FROM groups WHERE org_id = ? ORDER BY name", (org_id,)).fetchall()
        out = []
        for g in groups:
            members = conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (g["group_id"],)).fetchall()
            out.append({"group_id": g["group_id"], "name": g["name"], "member_ids": [m["user_id"] for m in members]})
    return out


# --------------------------------------------------------- collections
def create_collection(
    org_id: str, name: str, description: str = "", visibility: str = "restricted",
    owner_user_id: Optional[str] = None,
) -> str:
    if visibility not in ("org", "restricted"):
        raise ValueError("visibility must be 'org' or 'restricted'")
    collection_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            """INSERT INTO collections (collection_id, org_id, name, description, visibility, owner_user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (collection_id, org_id, name.strip(), description, visibility, owner_user_id, time.time()),
        )
    logger.info("Created collection %s (%s) in org %s", name, visibility, org_id)
    return collection_id


def get_collection(collection_id: str, org_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT collection_id, org_id, name, description, visibility, owner_user_id, data_version "
            "FROM collections WHERE collection_id = ? AND org_id = ?",
            (collection_id, org_id),
        ).fetchone()
    return dict(row) if row else None


_READABLE_SQL = """
SELECT c.collection_id FROM collections c
WHERE c.org_id = :org AND (
    c.visibility = 'org'
    OR c.owner_user_id = :uid
    OR EXISTS (SELECT 1 FROM collection_acl a
               WHERE a.collection_id = c.collection_id
                 AND a.principal_type = 'user' AND a.principal_id = :uid)
    OR EXISTS (SELECT 1 FROM collection_acl a
               JOIN group_members gm ON gm.group_id = a.principal_id
               WHERE a.collection_id = c.collection_id
                 AND a.principal_type = 'group' AND gm.user_id = :uid)
)
"""


def accessible_collection_ids(user: dict) -> list[str]:
    """The ONLY place that decides what a user may read. `user` is the row
    from users_store (needs user_id, org_id, role)."""
    with _connect() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                "SELECT collection_id FROM collections WHERE org_id = ? "
                "AND (owner_user_id IS NULL OR owner_user_id = ?)",
                (user["org_id"], user["user_id"]),
            ).fetchall()
        else:
            rows = conn.execute(_READABLE_SQL, {"org": user["org_id"], "uid": user["user_id"]}).fetchall()
    return [r["collection_id"] for r in rows]


def list_collections_for(user: dict) -> list[dict]:
    ids = accessible_collection_ids(user)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT collection_id, name, description, visibility, owner_user_id FROM collections "
            f"WHERE collection_id IN ({marks}) ORDER BY name", ids,
        ).fetchall()
    return [dict(r) for r in rows]


def can_read(user: dict, collection_id: str) -> bool:
    return collection_id in accessible_collection_ids(user)


def can_write(user: dict, collection: dict) -> bool:
    if collection["org_id"] != user["org_id"]:
        return False
    if collection["owner_user_id"]:                      # personal collection: only its owner
        return collection["owner_user_id"] == user["user_id"]
    return user["role"] == "admin"


def get_or_create_personal_collection(user: dict) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT collection_id FROM collections WHERE org_id = ? AND owner_user_id = ?",
            (user["org_id"], user["user_id"]),
        ).fetchone()
    if row:
        return get_collection(row["collection_id"], user["org_id"])
    cid = create_collection(
        user["org_id"], f"personal:{user['user_id']}", "Your private documents",
        visibility="restricted", owner_user_id=user["user_id"],
    )
    return get_collection(cid, user["org_id"])


def set_collection_acl(collection_id: str, user_ids: list[str], group_ids: list[str]) -> None:
    """Replaces the whole ACL. Caller must already have checked that every
    id belongs to the same org as the collection."""
    with _connect() as conn:
        conn.execute("DELETE FROM collection_acl WHERE collection_id = ?", (collection_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO collection_acl VALUES (?, 'user', ?)", [(collection_id, u) for u in user_ids]
        )
        conn.executemany(
            "INSERT OR IGNORE INTO collection_acl VALUES (?, 'group', ?)", [(collection_id, g) for g in group_ids]
        )


def get_collection_acl(collection_id: str) -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT principal_type, principal_id FROM collection_acl WHERE collection_id = ?", (collection_id,)
        ).fetchall()
    return {
        "user_ids": [r["principal_id"] for r in rows if r["principal_type"] == "user"],
        "group_ids": [r["principal_id"] for r in rows if r["principal_type"] == "group"],
    }


def set_visibility(collection_id: str, visibility: str) -> None:
    if visibility not in ("org", "restricted"):
        raise ValueError("visibility must be 'org' or 'restricted'")
    with _connect() as conn:
        conn.execute("UPDATE collections SET visibility = ? WHERE collection_id = ?", (visibility, collection_id))


def bump_version(collection_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE collections SET data_version = data_version + 1 WHERE collection_id = ?", (collection_id,))


def access_scope(collection_ids: list[str]) -> str:
    """A fingerprint of 'which collections, at which data versions'. The
    semantic cache is keyed on it, so a cached answer stops matching the
    moment a doc is added/removed/replaced OR the user's access set changes.
    This replaces manual cache invalidation."""
    if not collection_ids:
        return "none"
    marks = ",".join("?" * len(collection_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT collection_id, data_version FROM collections WHERE collection_id IN ({marks})",
            collection_ids,
        ).fetchall()
    raw = "|".join(sorted(f"{r['collection_id']}:{r['data_version']}" for r in rows))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ----------------------------------------------------------- documents
def find_active_document(collection_id: str, filename: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE collection_id = ? AND filename = ? AND status = 'active'",
            (collection_id, filename),
        ).fetchone()
    return dict(row) if row else None


def register_document(
    collection_id: str, org_id: str, filename: str, version: int, content_hash: str,
    chunk_count: int, uploaded_by: str, doc_id: Optional[str] = None, scan: Optional[list] = None,
) -> str:
    doc_id = doc_id or uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            """INSERT INTO documents (doc_id, collection_id, org_id, filename, version, status,
                                      content_hash, chunk_count, uploaded_by, created_at, scan_json)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
            (doc_id, collection_id, org_id, filename, version, content_hash, chunk_count, uploaded_by,
             time.time(), json.dumps(scan or [])),
        )
    return doc_id


def set_document_status(doc_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE documents SET status = ? WHERE doc_id = ?", (status, doc_id))


def get_document(doc_id: str, collection_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE doc_id = ? AND collection_id = ?", (doc_id, collection_id)
        ).fetchone()
    return dict(row) if row else None


def has_active_documents(collection_ids: list[str]) -> bool:
    if not collection_ids:
        return False
    marks = ",".join("?" * len(collection_ids))
    with _connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM documents WHERE status = 'active' AND collection_id IN ({marks}) LIMIT 1",
            collection_ids,
        ).fetchone()
    return row is not None


def list_documents(collection_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, filename, version, status, chunk_count, created_at, scan_json FROM documents "
            "WHERE collection_id = ? AND status != 'deleted' ORDER BY filename, version DESC",
            (collection_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["findings"] = json.loads(d.pop("scan_json") or "[]")
        out.append(d)
    return out


# ------------------------------------------------------------ settings
def get_setting(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def assert_embedding_mode(current_label: str) -> None:
    """MiniLM and the offline hashing embedder are BOTH 384-d, so Chroma will
    happily mix them and give garbage retrieval with no error. Remember the
    mode the index was built with and refuse to run in a different one."""
    stored = get_setting("embedding_mode")
    if stored is None:
        set_setting("embedding_mode", current_label)
    elif stored != current_label:
        raise RuntimeError(
            f"Index was built with '{stored}' embeddings but the server is now running '{current_label}'. "
            "Fix HuggingFace access (or FORCE_OFFLINE_EMBEDDINGS) and restart, or re-index."
        )


def get_personal_collection(user: dict) -> Optional[dict]:
    """Like get_or_create_personal_collection, but returns None instead of
    creating one -- a user who never uploaded privately shouldn't get an
    empty collection conjured into existence just because they're being deleted."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT collection_id FROM collections WHERE org_id = ? AND owner_user_id = ?",
            (user["org_id"], user["user_id"]),
        ).fetchone()
    return get_collection(row["collection_id"], user["org_id"]) if row else None


def delete_collection_cascade(collection_id: str) -> None:
    """Removes a collection's document rows, its ACL rows, and the collection
    itself. Does NOT touch Chroma -- call document_processor.delete_collection_chunks()
    first, same ordering as delete_doc_chunks() elsewhere in the codebase."""
    with _connect() as conn:
        conn.execute("DELETE FROM documents WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM collection_acl WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM collections WHERE collection_id = ?", (collection_id,))


def remove_user_everywhere(user_id: str) -> None:
    """Strips a user out of every group and every collection ACL that names
    them directly. Call right before deleting the user row itself."""
    with _connect() as conn:
        conn.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM collection_acl WHERE principal_type = 'user' AND principal_id = ?", (user_id,))


# ------------------------------------------------ thread <-> collections used
def record_thread_collections(thread_id: str, collection_ids) -> None:
    ids = [c for c in set(collection_ids) if c]
    if not ids:
        return
    with _connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO thread_collections VALUES (?, ?)", [(thread_id, c) for c in ids])


def record_thread_documents(thread_id: str, doc_ids) -> None:
    ids = [d for d in set(doc_ids) if d]
    if not ids:
        return
    with _connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO thread_documents VALUES (?, ?)", [(thread_id, d) for d in ids])


def get_thread_documents(thread_id: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT doc_id FROM thread_documents WHERE thread_id = ?", (thread_id,)).fetchall()
    return sorted(r["doc_id"] for r in rows)



def get_thread_collections(thread_id: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT collection_id FROM thread_collections WHERE thread_id = ?", (thread_id,)).fetchall()
    return sorted(r["collection_id"] for r in rows)


def delete_thread_collections(thread_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM thread_collections WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM thread_documents WHERE thread_id = ?", (thread_id,))