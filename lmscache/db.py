"""Tiny SQLite layer. One connection, one lock; the data set is a few hundred rows."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS models   (id TEXT PRIMARY KEY, meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machines (name TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents  (machine TEXT NOT NULL, model_id TEXT NOT NULL, state TEXT NOT NULL,
                                     set_at REAL NOT NULL, PRIMARY KEY (machine, model_id));
CREATE TABLE IF NOT EXISTS reports  (machine TEXT PRIMARY KEY, data TEXT NOT NULL, at REAL NOT NULL);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = sqlite3.connect(str(config.DB_FILE), check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        c = conn()
        c.execute(sql, params)
        c.commit()


def query(sql: str, params: tuple = ()) -> list[tuple]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def get_json(table: str, key_col: str, key: str) -> dict | None:
    rows = query(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,))
    if not rows:
        return None
    # the JSON payload is always the second column
    return json.loads(rows[0][1])


def put_json(table: str, key_col: str, key: str, payload_col: str, payload: Any, extra: dict | None = None) -> None:
    extra = extra or {}
    cols = [key_col, payload_col, *extra.keys()]
    vals = [key, json.dumps(payload), *extra.values()]
    placeholders = ",".join("?" for _ in cols)
    execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})", tuple(vals))


def all_json(table: str, key_col: str, payload_col: str) -> dict[str, Any]:
    return {k: json.loads(v) for k, v in query(f"SELECT {key_col}, {payload_col} FROM {table}")}
