"""
org_store.py
The company layer: orgs, groups, collections, ACLs, document records, and settings.
"""

import hashlib
import json
import time
import uuid
from typing import Optional
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import logger
from app.db import connect, run_script, table_columns, IS_PG, engine



def _init_db() -> None:
    run_script("""
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
        visibility TEXT NOT NULL DEFAULT 'restricted',
        owner_user_id TEXT,
        data_version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, UNIQUE (org_id, name)
    );
    CREATE TABLE IF NOT EXISTS collection_acl (
        collection_id TEXT NOT NULL, principal_type TEXT NOT NULL,
        principal_id TEXT NOT NULL, PRIMARY KEY (collection_id, principal_type, principal_id)
    );
    CREATE TABLE IF NOT EXISTS documents (
        doc_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, org_id TEXT NOT NULL,
        filename TEXT NOT NULL, version INTEGER NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
        uploaded_by TEXT NOT NULL, created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_docs_coll ON documents (collection_id, status);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS thread_collections (
        thread_id TEXT NOT NULL, collection_id TEXT NOT NULL, PRIMARY KEY (thread_id, collection_id)
    );
    CREATE TABLE IF NOT EXISTS thread_documents (
        thread_id TEXT NOT NULL, doc_id TEXT NOT NULL, PRIMARY KEY (thread_id, doc_id)
    );
    """)
    cols = table_columns("documents")
    if "scan_json" not in cols and engine:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE documents ADD COLUMN scan_json TEXT NOT NULL DEFAULT '[]'"))

_init_db()


# ------------------------------------------------------------------ orgs

def create_org(name: str) -> str:
    org_id = uuid.uuid4().hex
    with connect() as session:
        session.execute(
            text("INSERT INTO orgs (org_id, name, created_at) VALUES (:org_id, :name, :created_at)"),
            {"org_id": org_id, "name": name, "created_at": time.time()},
        )
    logger.info("Created org: %s (%s)", name, org_id)
    return org_id


def get_org(org_id: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT org_id, name FROM orgs WHERE org_id = :org_id"),
            {"org_id": org_id},
        ).mappings().fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------- groups

def create_group(org_id: str, name: str) -> str:
    group_id = uuid.uuid4().hex
    with connect() as session:
        session.execute(
            text("INSERT INTO groups (group_id, org_id, name, created_at) VALUES (:group_id, :org_id, :name, :created_at)"),
            {"group_id": group_id, "org_id": org_id, "name": name.strip(), "created_at": time.time()},
        )
    return group_id


def group_in_org(group_id: str, org_id: str) -> bool:
    with connect() as session:
        return session.execute(
            text("SELECT 1 FROM groups WHERE group_id = :group_id AND org_id = :org_id"),
            {"group_id": group_id, "org_id": org_id},
        ).fetchone() is not None


def add_group_member(group_id: str, user_id: str) -> None:
    with connect() as session:
        if IS_PG:
            session.execute(
                text("INSERT INTO group_members (group_id, user_id) VALUES (:group_id, :user_id) ON CONFLICT DO NOTHING"),
                {"group_id": group_id, "user_id": user_id},
            )
        else:
            session.execute(
                text("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (:group_id, :user_id)"),
                {"group_id": group_id, "user_id": user_id},
            )


def remove_group_member(group_id: str, user_id: str) -> None:
    with connect() as session:
        session.execute(
            text("DELETE FROM group_members WHERE group_id = :group_id AND user_id = :user_id"),
            {"group_id": group_id, "user_id": user_id},
        )


def delete_group(group_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM group_members WHERE group_id = :group_id"), {"group_id": group_id})
        session.execute(
            text("DELETE FROM collection_acl WHERE principal_type = 'group' AND principal_id = :group_id"),
            {"group_id": group_id},
        )
        session.execute(text("DELETE FROM groups WHERE group_id = :group_id"), {"group_id": group_id})


def list_groups(org_id: str) -> list[dict]:
    with connect() as session:
        groups = session.execute(
            text("SELECT group_id, name FROM groups WHERE org_id = :org_id ORDER BY name"),
            {"org_id": org_id},
        ).mappings().fetchall()
        out = []
        for g in groups:
            members = session.execute(
                text("SELECT user_id FROM group_members WHERE group_id = :group_id"),
                {"group_id": g["group_id"]},
            ).mappings().fetchall()
            out.append({"group_id": g["group_id"], "name": g["name"], "member_ids": [m["user_id"] for m in members]})
        return out


# ---------------------------------------------------------- collections

def create_collection(
    org_id: str, name: str, description: str = "", visibility: str = "restricted",
    owner_user_id: Optional[str] = None,
) -> str:
    if visibility not in ("org", "restricted"):
        raise ValueError("visibility must be 'org' or 'restricted'")
    collection_id = uuid.uuid4().hex
    with connect() as session:
        session.execute(
            text("""INSERT INTO collections (collection_id, org_id, name, description, visibility, owner_user_id, created_at)
            VALUES (:collection_id, :org_id, :name, :description, :visibility, :owner_user_id, :created_at)"""),
            {
                "collection_id": collection_id, "org_id": org_id, "name": name.strip(),
                "description": description, "visibility": visibility,
                "owner_user_id": owner_user_id, "created_at": time.time(),
            },
        )
    logger.info("Created collection %s (%s) in org %s", name, visibility, org_id)
    return collection_id


def get_collection(collection_id: str, org_id: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT collection_id, org_id, name, description, visibility, owner_user_id, data_version "
                 "FROM collections WHERE collection_id = :collection_id AND org_id = :org_id"),
            {"collection_id": collection_id, "org_id": org_id},
        ).mappings().fetchone()
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
    with connect() as session:
        if user["role"] == "admin":
            rows = session.execute(
                text("SELECT collection_id FROM collections WHERE org_id = :org_id "
                     "AND (owner_user_id IS NULL OR owner_user_id = :user_id)"),
                {"org_id": user["org_id"], "user_id": user["user_id"]},
            ).mappings().fetchall()
        else:
            rows = session.execute(text(_READABLE_SQL), {"org": user["org_id"], "uid": user["user_id"]}).mappings().fetchall()
        return [r["collection_id"] for r in rows]


def list_collections_for(user: dict) -> list[dict]:
    ids = accessible_collection_ids(user)
    if not ids:
        return []

    bind_keys = [f":id_{i}" for i in range(len(ids))]
    sql = f"SELECT collection_id, name, description, visibility, owner_user_id FROM collections WHERE collection_id IN ({','.join(bind_keys)}) ORDER BY name"
    params = {f"id_{i}": val for i, val in enumerate(ids)}

    with connect() as session:
        rows = session.execute(text(sql), params).mappings().fetchall()
    return [dict(r) for r in rows]


def can_read(user: dict, collection_id: str) -> bool:
    return collection_id in accessible_collection_ids(user)


def can_write(user: dict, collection: dict) -> bool:
    if collection["org_id"] != user["org_id"]:
        return False
    if collection["owner_user_id"]:
        return collection["owner_user_id"] == user["user_id"]
    return user["role"] == "admin"


def get_or_create_personal_collection(user: dict) -> dict:
    with connect() as session:
        row = session.execute(
            text("SELECT collection_id FROM collections WHERE org_id = :org_id AND owner_user_id = :user_id"),
            {"org_id": user["org_id"], "user_id": user["user_id"]},
        ).mappings().fetchone()
        if row:
            return get_collection(row["collection_id"], user["org_id"])
    cid = create_collection(
        user["org_id"], f"personal:{user['user_id']}", "Your private documents",
        visibility="restricted", owner_user_id=user["user_id"],
    )
    return get_collection(cid, user["org_id"])


def set_collection_acl(collection_id: str, user_ids: list[str], group_ids: list[str]) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM collection_acl WHERE collection_id = :collection_id"), {"collection_id": collection_id})

        conflict_clause = "ON CONFLICT DO NOTHING" if IS_PG else ""
        ignore_clause = "" if IS_PG else "OR IGNORE"

        for u in user_ids:
            session.execute(
                text(f"INSERT {ignore_clause} INTO collection_acl VALUES (:cid, 'user', :uid) {conflict_clause}"),
                {"cid": collection_id, "uid": u},
            )
        for g in group_ids:
            session.execute(
                text(f"INSERT {ignore_clause} INTO collection_acl VALUES (:cid, 'group', :gid) {conflict_clause}"),
                {"cid": collection_id, "gid": g},
            )


def get_collection_acl(collection_id: str) -> dict:
    with connect() as session:
        rows = session.execute(
            text("SELECT principal_type, principal_id FROM collection_acl WHERE collection_id = :collection_id"),
            {"collection_id": collection_id},
        ).mappings().fetchall()
    return {
        "user_ids": [r["principal_id"] for r in rows if r["principal_type"] == "user"],
        "group_ids": [r["principal_id"] for r in rows if r["principal_type"] == "group"],
    }


def set_visibility(collection_id: str, visibility: str) -> None:
    if visibility not in ("org", "restricted"):
        raise ValueError("visibility must be 'org' or 'restricted'")
    with connect() as session:
        session.execute(
            text("UPDATE collections SET visibility = :visibility WHERE collection_id = :collection_id"),
            {"visibility": visibility, "collection_id": collection_id},
        )


def bump_version(collection_id: str) -> None:
    with connect() as session:
        session.execute(
            text("UPDATE collections SET data_version = data_version + 1 WHERE collection_id = :collection_id"),
            {"collection_id": collection_id},
        )


def access_scope(collection_ids: list[str]) -> str:
    if not collection_ids:
        return "none"
    bind_keys = [f":id_{i}" for i in range(len(collection_ids))]
    sql = f"SELECT collection_id, data_version FROM collections WHERE collection_id IN ({','.join(bind_keys)})"
    params = {f"id_{i}": val for i, val in enumerate(collection_ids)}

    with connect() as session:
        rows = session.execute(text(sql), params).mappings().fetchall()
    raw = "|".join(sorted(f"{r['collection_id']}:{r['data_version']}" for r in rows))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ------------------------------------------------------------ documents

def find_active_document(collection_id: str, filename: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT * FROM documents WHERE collection_id = :cid AND filename = :fname AND status = 'active'"),
            {"cid": collection_id, "fname": filename},
        ).mappings().fetchone()
        return dict(row) if row else None


def register_document(
    collection_id: str, org_id: str, filename: str, version: int, content_hash: str,
    chunk_count: int, uploaded_by: str, doc_id: Optional[str] = None, scan: Optional[list] = None,
) -> str:
    doc_id = doc_id or uuid.uuid4().hex
    with connect() as session:
        session.execute(
            text("""INSERT INTO documents (doc_id, collection_id, org_id, filename, version, status,
            content_hash, chunk_count, uploaded_by, created_at, scan_json)
            VALUES (:doc_id, :collection_id, :org_id, :filename, :version, 'active',
            :content_hash, :chunk_count, :uploaded_by, :created_at, :scan_json)"""),
            {
                "doc_id": doc_id, "collection_id": collection_id, "org_id": org_id,
                "filename": filename, "version": version, "content_hash": content_hash,
                "chunk_count": chunk_count, "uploaded_by": uploaded_by,
                "created_at": time.time(), "scan_json": json.dumps(scan or []),
            },
        )
    return doc_id


def set_document_status(doc_id: str, status: str) -> None:
    with connect() as session:
        session.execute(
            text("UPDATE documents SET status = :status WHERE doc_id = :doc_id"),
            {"status": status, "doc_id": doc_id},
        )


def get_document(doc_id: str, collection_id: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT * FROM documents WHERE doc_id = :doc_id AND collection_id = :collection_id"),
            {"doc_id": doc_id, "collection_id": collection_id},
        ).mappings().fetchone()
        return dict(row) if row else None


def has_active_documents(collection_ids: list[str]) -> bool:
    if not collection_ids:
        return False
    bind_keys = [f":id_{i}" for i in range(len(collection_ids))]
    sql = f"SELECT 1 FROM documents WHERE status = 'active' AND collection_id IN ({','.join(bind_keys)}) LIMIT 1"
    params = {f"id_{i}": val for i, val in enumerate(collection_ids)}

    with connect() as session:
        row = session.execute(text(sql), params).fetchone()
    return row is not None


def list_documents(collection_id: str) -> list[dict]:
    with connect() as session:
        rows = session.execute(
            text("SELECT doc_id, filename, version, status, chunk_count, created_at, scan_json FROM documents "
                 "WHERE collection_id = :collection_id AND status != 'deleted' ORDER BY filename, version DESC"),
            {"collection_id": collection_id},
        ).mappings().fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["findings"] = json.loads(d.pop("scan_json") or "[]")
            out.append(d)
        return out


# ------------------------------------------------------------- settings

def get_setting(key: str) -> Optional[str]:
    with connect() as session:
        row = session.execute(text("SELECT value FROM settings WHERE key = :key"), {"key": key}).mappings().fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as session:
        if IS_PG:
            session.execute(
                text("INSERT INTO settings (key, value) VALUES (:key, :value) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                {"key": key, "value": value},
            )
        else:
            session.execute(
                text("INSERT OR REPLACE INTO settings (key, value) VALUES (:key, :value)"),
                {"key": key, "value": value},
            )


def assert_embedding_mode(current_label: str) -> None:
    stored = get_setting("embedding_mode")
    if stored is None:
        set_setting("embedding_mode", current_label)
    elif stored != current_label:
        raise RuntimeError(
            f"Index was built with '{stored}' embeddings but the server is now running '{current_label}'. "
            "Fix HuggingFace access (or FORCE_OFFLINE_EMBEDDINGS) and restart, or re-index."
        )


def get_personal_collection(user: dict) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT collection_id FROM collections WHERE org_id = :org_id AND owner_user_id = :user_id"),
            {"org_id": user["org_id"], "user_id": user["user_id"]},
        ).mappings().fetchone()
        return get_collection(row["collection_id"], user["org_id"]) if row else None


def delete_collection_cascade(collection_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM documents WHERE collection_id = :cid"), {"cid": collection_id})
        session.execute(text("DELETE FROM collection_acl WHERE collection_id = :cid"), {"cid": collection_id})
        session.execute(text("DELETE FROM collections WHERE collection_id = :cid"), {"cid": collection_id})


def remove_user_everywhere(user_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM group_members WHERE user_id = :uid"), {"uid": user_id})
        session.execute(
            text("DELETE FROM collection_acl WHERE principal_type = 'user' AND principal_id = :uid"),
            {"uid": user_id},
        )


# ------------------------------------------------- thread <-> collections

def record_thread_collections(thread_id: str, collection_ids) -> None:
    ids = [c for c in set(collection_ids) if c]
    if not ids:
        return

    conflict_clause = "ON CONFLICT DO NOTHING" if IS_PG else ""
    ignore_clause = "" if IS_PG else "OR IGNORE"

    with connect() as session:
        for cid in ids:
            session.execute(
                text(f"INSERT {ignore_clause} INTO thread_collections VALUES (:tid, :cid) {conflict_clause}"),
                {"tid": thread_id, "cid": cid},
            )


def record_thread_documents(thread_id: str, doc_ids) -> None:
    ids = [d for d in set(doc_ids) if d]
    if not ids:
        return

    conflict_clause = "ON CONFLICT DO NOTHING" if IS_PG else ""
    ignore_clause = "" if IS_PG else "OR IGNORE"

    with connect() as session:
        for did in ids:
            session.execute(
                text(f"INSERT {ignore_clause} INTO thread_documents VALUES (:tid, :did) {conflict_clause}"),
                {"tid": thread_id, "did": did},
            )


def get_thread_documents(thread_id: str) -> list[str]:
    with connect() as session:
        rows = session.execute(
            text("SELECT doc_id FROM thread_documents WHERE thread_id = :thread_id"),
            {"thread_id": thread_id},
        ).mappings().fetchall()
        return sorted(r["doc_id"] for r in rows)


def get_thread_collections(thread_id: str) -> list[str]:
    with connect() as session:
        rows = session.execute(
            text("SELECT collection_id FROM thread_collections WHERE thread_id = :thread_id"),
            {"thread_id": thread_id},
        ).mappings().fetchall()
        return sorted(r["collection_id"] for r in rows)


def delete_thread_collections(thread_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM thread_collections WHERE thread_id = :tid"), {"tid": thread_id})
        session.execute(text("DELETE FROM thread_documents WHERE thread_id = :tid"), {"tid": thread_id})