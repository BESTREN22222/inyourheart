"""Local SQLite storage for INYOURHEART."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paths

ROOT = paths.BASE
DB_PATH = paths.DATA / "inyourheart.db"
OLD_DB_PATH = paths.DATA / "echo-open.db"
TZ = timezone.utc


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and OLD_DB_PATH.exists():
        OLD_DB_PATH.replace(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'direct',
            uid TEXT,
            screen_name TEXT,
            token_enc TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_since_id INTEGER NOT NULL DEFAULT 0,
            last_run_at TEXT,
            last_status TEXT,
            last_new INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            pages INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'running',
            fetched INTEGER NOT NULL DEFAULT 0,
            saved INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "balance" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN balance TEXT DEFAULT ''")
    conn.commit()


# ---- DPAPI (Windows) -------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect(text: str) -> str:
    if not text:
        return ""
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in, _buffer = _blob(text.encode("utf-8"))
        blob_out = _Blob()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptProtectData failed")
        try:
            raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return "dpapi:" + base64.b64encode(raw).decode("ascii")
    except Exception:
        # Non-Windows fallback; still never written to logs.
        return "plain:" + base64.b64encode(text.encode("utf-8")).decode("ascii")


def unprotect(value: str) -> str:
    if not value:
        return ""
    if value.startswith("plain:"):
        return base64.b64decode(value[6:]).decode("utf-8")
    if not value.startswith("dpapi:"):
        return value
    try:
        raw = base64.b64decode(value[6:])
        blob_in, _buffer = _blob(raw)
        blob_out = _Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptUnprotectData failed")
        try:
            data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return data.decode("utf-8")
    except Exception:
        return ""


# ---- helpers ---------------------------------------------------------------

def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


# ---- accounts --------------------------------------------------------------

def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, mode, uid, screen_name, balance, created_at FROM accounts ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["token"] = unprotect(data.pop("token_enc", "") or "")
    return data


def add_account(conn: sqlite3.Connection, name: str, mode: str, uid: str,
                screen_name: str, token: str = "", balance: str = "") -> int:
    cursor = conn.execute(
        "INSERT INTO accounts (name, mode, uid, screen_name, token_enc, balance, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, mode, uid, screen_name, protect(token), str(balance or ""), now()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def set_account_balance(conn: sqlite3.Connection, account_id: int, balance: str) -> None:
    conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (str(balance or ""), account_id))
    conn.commit()


def delete_account(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()


# ---- query history ---------------------------------------------------------

def add_query(conn: sqlite3.Connection, account_id: int, source: str,
              params: dict[str, Any], pages: int) -> int:
    cursor = conn.execute(
        "INSERT INTO queries (account_id, source, params, pages, status, started_at) "
        "VALUES (?, ?, ?, ?, 'running', ?)",
        (account_id, source, json.dumps(params, ensure_ascii=False), int(pages), now()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_query(conn: sqlite3.Connection, query_id: int, status: str,
                 pages: int, fetched: int, saved: int, error: str = "") -> None:
    conn.execute(
        "UPDATE queries SET status = ?, pages = ?, fetched = ?, saved = ?, error = ?, finished_at = ? "
        "WHERE id = ?",
        (status, int(pages), int(fetched), int(saved), error[:400], now(), int(query_id)),
    )
    conn.commit()


def list_queries(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT q.*, a.name AS account_name, a.screen_name "
        "FROM queries q LEFT JOIN accounts a ON a.id = q.account_id "
        "ORDER BY q.id DESC LIMIT ?",
        (max(1, min(200, int(limit))),),
    ).fetchall()
    items = []
    for row in rows:
        data = dict(row)
        try:
            data["params"] = json.loads(data.get("params") or "{}")
        except (TypeError, ValueError):
            data["params"] = {}
        items.append(data)
    return items


# ---- comments --------------------------------------------------------------

def save_comments(conn: sqlite3.Connection, account_id: int, source: str,
                  comments: list[dict[str, Any]]) -> int:
    added = 0
    for item in comments:
        try:
            comment_id = int(item.get("id") or item.get("idstr") or 0)
        except (TypeError, ValueError):
            continue
        if not comment_id:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO comments (id, account_id, source, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (comment_id, account_id, source, json.dumps(item, ensure_ascii=False), now()),
        )
        added += cursor.rowcount
    conn.commit()
    return added


def list_comments(conn: sqlite3.Connection, account_id: int, source: str = "",
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
    where = "WHERE account_id = ?"
    params: list[Any] = [account_id]
    if source:
        where += " AND source = ?"
        params.append(source)
    total = conn.execute(f"SELECT COUNT(*) FROM comments {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, source, payload, created_at FROM comments {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    items = []
    for row in rows:
        item = json.loads(row["payload"])
        item["_source"] = row["source"]
        items.append(item)
    return {"total": total, "items": items}


def comment_bounds(conn: sqlite3.Connection, account_id: int, source: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT MIN(id) AS oldest, MAX(id) AS newest FROM comments "
        "WHERE account_id = ? AND source = ? AND id > 0",
        (account_id, source),
    ).fetchone()
    return {"oldest": int(row["oldest"] or 0), "newest": int(row["newest"] or 0)}


# ---- schedules -------------------------------------------------------------

def list_schedules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.*, a.name AS account_name, a.screen_name, a.mode "
        "FROM schedules s JOIN accounts a ON a.id = s.account_id ORDER BY s.id"
    ).fetchall()
    return [dict(row) for row in rows]


def add_schedule(conn: sqlite3.Connection, account_id: int, source: str,
                 params: dict[str, Any], interval_minutes: int) -> int:
    cursor = conn.execute(
        "INSERT INTO schedules (account_id, source, params, interval_minutes) VALUES (?, ?, ?, ?)",
        (account_id, source, json.dumps(params, ensure_ascii=False), max(1, interval_minutes)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_schedule(conn: sqlite3.Connection, schedule_id: int) -> None:
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    conn.commit()


def set_schedule_enabled(conn: sqlite3.Connection, schedule_id: int, enabled: bool) -> None:
    conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, schedule_id))
    conn.commit()


def update_schedule(conn: sqlite3.Connection, schedule_id: int, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(f"UPDATE schedules SET {columns} WHERE id = ?", list(fields.values()) + [schedule_id])
    conn.commit()
