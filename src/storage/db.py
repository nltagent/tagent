"""
Единственная точка доступа к SQLite. Специально не даём другим
модулям открывать соединение самостоятельно — вся работа с БД идёт
через execute()/query() отсюда, с одним общим Lock.

Почему один Lock на всё, а не WAL/пул соединений: у нас один
процесс, один контейнер, нагрузка — единицы запросов в минуту от
одного владельца. Простой Lock полностью исключает "database is
locked" ошибки ценой почти нулевой задержки при таком трафике.
Если это когда-нибудь станет узким местом — заменить один этот
файл, остальной код не заметит разницы.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from config import config
from core.logger import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_memory (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    role        TEXT NOT NULL,       -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    tokens_est  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, archived, id);

CREATE TABLE IF NOT EXISTS conversation_meta (
    chat_id             TEXT PRIMARY KEY,
    summary             TEXT NOT NULL DEFAULT '',
    summary_updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
        log.info("SQLite открыта: %s", config.DB_PATH)
    return _conn


@contextmanager
def _cursor():
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def execute(sql: str, params: tuple = ()) -> int:
    """INSERT/UPDATE/DELETE. Возвращает lastrowid (для INSERT)."""
    with _cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def log_usage(provider: str, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
    execute(
        """
        INSERT INTO usage_log (created_at, provider, model, prompt_tokens, completion_tokens, total_tokens)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), provider, model, prompt_tokens, completion_tokens, total_tokens),
    )


def usage_today_totals() -> dict:
    """Суммы за текущие UTC-сутки — используется командой /usage."""
    row = query_one(
        """
        SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
        FROM usage_log
        WHERE date(created_at) = date('now')
        """
    )
    return {"requests": row["requests"], "tokens": row["tokens"]} if row else {"requests": 0, "tokens": 0}
