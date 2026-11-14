"""SQLite persistence for ContentForge.

Connect-per-call style (mirrors AutoClip's autopost_db): every helper opens
its own connection so threads never share a sqlite handle. WAL mode keeps the
UI reads cheap while the upload worker writes.
"""
import datetime
import json
import sqlite3
import threading

from . import paths

_INIT_LOCK = threading.Lock()
_INIT_DONE = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    key TEXT PRIMARY KEY,               -- 'tiktok:default'
    platform TEXT NOT NULL,
    label TEXT,
    profile_key TEXT UNIQUE,
    account_name TEXT,
    connected INTEGER DEFAULT 0,
    channel_url TEXT,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    kind TEXT,                          -- audio | video | image | text
    title TEXT,
    path TEXT,
    engine TEXT,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    asset_id TEXT,
    account_key TEXT,
    status TEXT DEFAULT 'queued',       -- queued|running|done|failed|cancelled
    schedule_at TEXT,                   -- ISO UTC
    visibility TEXT DEFAULT 'public',
    caption_json TEXT DEFAULT '{}',
    progress INTEGER DEFAULT 0,
    public_url TEXT,
    platform_post_id TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    event TEXT,
    detail_json TEXT DEFAULT '{}',
    at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, schedule_at);
CREATE INDEX IF NOT EXISTS idx_history_job ON history(job_id);
"""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    init_db()  # cheap after first call (guard flag)
    conn = sqlite3.connect(str(paths.db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        # open a raw connection — connect() itself ensures-init, so going
        # through it here would deadlock on _INIT_LOCK
        conn = sqlite3.connect(str(paths.db_path()), timeout=30)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _INIT_DONE = True


def insert(table: str, row: dict) -> None:
    conn = connect()
    try:
        cols = ", ".join(row.keys())
        marks = ", ".join(["?"] * len(row))
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                     list(row.values()))
        conn.commit()
    finally:
        conn.close()


def upsert(table: str, row: dict, conflict_col: str = "key") -> None:
    conn = connect()
    try:
        cols = list(row.keys())
        marks = ", ".join(["?"] * len(cols))
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != conflict_col)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks}) "
            f"ON CONFLICT({conflict_col}) DO UPDATE SET {updates}",
            [row[c] for c in cols])
        conn.commit()
    finally:
        conn.close()


def update(table: str, key: str, vals: dict, key_col: str = "key") -> None:
    conn = connect()
    try:
        sets = ", ".join(f"{c} = ?" for c in vals)
        conn.execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?",
                     list(vals.values()) + [key])
        conn.commit()
    finally:
        conn.close()


def rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def one(sql: str, params: tuple = ()) -> dict | None:
    r = rows(sql, params)
    return r[0] if r else None


def log_event(job_id: str | None, event: str, detail: dict | None = None) -> None:
    insert("history", {
        "job_id": job_id,
        "event": event,
        "detail_json": json.dumps(detail or {}, ensure_ascii=False),
        "at": now_iso(),
    })
