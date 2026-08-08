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

Мультипользовательский слой: таблица users хранит роли (pending /
user / vip / blocked / denied) для всех chat_id, кроме создателя
(OWNER_CHAT_ID) — тот проверяется напрямую по конфигу, отдельной
записи не требует (см. modules/users/service.py). agent_memory и
usage_log были общими на весь бот — теперь у каждого chat_id своя
самопамять и свой учёт расхода токенов; при обновлении с
однопользовательской версии старые данные переносятся на
OWNER_CHAT_ID (см. _migrate_agent_memory/_migrate_usage_log).
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

-- Самопамять теперь личная на каждый chat_id (раньше была общей на
-- весь бот) — см. _migrate_agent_memory для переноса старых данных.
CREATE TABLE IF NOT EXISTS agent_memory (
    chat_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    conversation_id INTEGER NOT NULL DEFAULT 0,
    role        TEXT NOT NULL,       -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    tokens_est  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, archived, id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, archived, id);

-- Один диалог (ветка переписки) на запись. Summary теперь живёт прямо
-- здесь, а не в отдельной conversation_meta (шаг 7: ветки диалогов —
-- summary и история теперь привязаны к conversation_id, а не к chat_id
-- напрямую, так что у одного chat_id может быть несколько диалогов).
CREATE TABLE IF NOT EXISTS conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             TEXT NOT NULL,
    title               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'closed'
    summary             TEXT NOT NULL DEFAULT '',
    summary_updated_at  TEXT,
    created_at          TEXT NOT NULL,
    last_active_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_chat ON conversations (chat_id, status);

-- Учёт расхода токенов теперь тоже персональный (chat_id) — раньше
-- usage_log был общим на весь бот, см. _migrate_usage_log.
CREATE TABLE IF NOT EXISTS usage_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    chat_id             TEXT,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_chat ON usage_log (chat_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    message     TEXT NOT NULL,
    due_at      TEXT NOT NULL,   -- ISO 8601, UTC
    created_at  TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (delivered, due_at);

-- Роли пользователей бота. Создатель (config.OWNER_CHAT_ID) сюда НЕ
-- записывается — у него полные права по определению, проверяется
-- напрямую по конфигу (modules/users/service.py:is_owner). Роли:
-- 'pending' (ждёт решения создателя), 'user'/'vip' (одобрен),
-- 'blocked' (забанен), 'denied' (заявку отклонили).
CREATE TABLE IF NOT EXISTS users (
    chat_id       TEXT PRIMARY KEY,
    role          TEXT NOT NULL DEFAULT 'pending',
    requested_at  TEXT NOT NULL,
    approved_by   TEXT,
    approved_at   TEXT
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
        # Важно: миграции колонок/таблиц ДО executescript(SCHEMA) — у
        # некоторых таблиц ниже есть индексы/ограничения по колонкам,
        # которых в старой базе могло не быть, а CREATE TABLE IF NOT
        # EXISTS не добавляет колонки в уже существующую таблицу.
        _add_conversation_id_column_if_missing(_conn)
        _migrate_agent_memory(_conn)
        _migrate_usage_log(_conn)
        _conn.executescript(SCHEMA)
        _conn.commit()
        _backfill_conversations(_conn)
        log.info("SQLite открыта: %s", config.DB_PATH)
    return _conn


def _add_conversation_id_column_if_missing(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchall()}
    if "messages" not in tables:
        return  # таблицы ещё нет вовсе — её создаст SCHEMA уже с нужной колонкой
    cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "conversation_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN conversation_id INTEGER")
        conn.commit()


def _migrate_agent_memory(conn: sqlite3.Connection) -> None:
    """Раньше agent_memory была общей на весь бот (PRIMARY KEY просто
    key). Теперь у каждого chat_id своя самопамять (PRIMARY KEY
    (chat_id, key)) — старые факты переносятся на OWNER_CHAT_ID, чтобы
    ничего не потерялось. На свежих базах таблицы либо ещё нет (её
    создаст SCHEMA уже в новом виде), либо она уже мигрирована —
    в обоих случаях эта функция ничего не делает."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_memory'"
    ).fetchall()}
    if "agent_memory" not in tables:
        return
    cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
    if "chat_id" in cols:
        return  # уже новой формы

    conn.execute("ALTER TABLE agent_memory RENAME TO agent_memory_old")
    conn.execute(
        """
        CREATE TABLE agent_memory (
            chat_id     TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (chat_id, key)
        )
        """
    )
    old_rows = conn.execute("SELECT key, value, updated_at FROM agent_memory_old").fetchall()
    for row in old_rows:
        conn.execute(
            "INSERT INTO agent_memory (chat_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (str(config.OWNER_CHAT_ID), row[0], row[1], row[2]),
        )
    conn.execute("DROP TABLE agent_memory_old")
    conn.commit()
    if old_rows:
        log.info(
            "Мигрирована agent_memory: %d факт(ов) перенесено на OWNER_CHAT_ID",
            len(old_rows),
        )


def _migrate_usage_log(conn: sqlite3.Connection) -> None:
    """usage_log раньше не знал про chat_id (был общим на весь бот).
    Добавляем колонку и относим все старые записи без chat_id на
    OWNER_CHAT_ID — это единственный, кто мог их насчитать в
    однопользовательской версии."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_log'"
    ).fetchall()}
    if "usage_log" not in tables:
        return
    cols = [row[1] for row in conn.execute("PRAGMA table_info(usage_log)").fetchall()]
    if "chat_id" not in cols:
        conn.execute("ALTER TABLE usage_log ADD COLUMN chat_id TEXT")
        conn.commit()

    cur = conn.execute(
        "UPDATE usage_log SET chat_id = ? WHERE chat_id IS NULL",
        (str(config.OWNER_CHAT_ID),),
    )
    if cur.rowcount:
        conn.commit()
        log.info(
            "Мигрирована usage_log: %d запись(ей) отнесено на OWNER_CHAT_ID",
            cur.rowcount,
        )


def _backfill_conversations(conn: sqlite3.Connection) -> None:
    """Шаг 7 (ветки диалогов): в старых базах (шаги 1-6) summary лежала
    в conversation_meta по chat_id, а сообщения не были привязаны ни к
    какому диалогу. Здесь — одноразовый перенос: на каждый chat_id, у
    которого есть сообщения без conversation_id, заводим один
    "Перенесённый диалог", переносим туда summary и все сообщения, и
    делаем его активным. На свежих базах переносить нечего — блок
    просто ничего не найдёт и завершится сразу."""
    orphaned_chat_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE conversation_id IS NULL"
        ).fetchall()
    ]
    if not orphaned_chat_ids:
        return

    try:
        old_summaries = {
            row["chat_id"]: row["summary"]
            for row in conn.execute("SELECT chat_id, summary FROM conversation_meta").fetchall()
        }
    except sqlite3.OperationalError:
        old_summaries = {}  # старой таблицы почему-то нет — не страшно

    now = now_iso()
    for chat_id in orphaned_chat_ids:
        summary = old_summaries.get(chat_id, "")
        cur = conn.execute(
            """
            INSERT INTO conversations
                (chat_id, title, status, summary, summary_updated_at, created_at, last_active_at)
            VALUES (?, 'Перенесённый диалог', 'active', ?, ?, ?, ?)
            """,
            (chat_id, summary, now if summary else None, now, now),
        )
        new_conv_id = cur.lastrowid
        conn.execute(
            "UPDATE messages SET conversation_id = ? WHERE chat_id = ? AND conversation_id IS NULL",
            (new_conv_id, chat_id),
        )
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (f"active_conversation:{chat_id}", str(new_conv_id), now),
        )
        log.info("Мигрирован chat_id=%s в conversation_id=%s", chat_id, new_conv_id)

    conn.commit()


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


def log_usage(
    chat_id: int | str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> None:
    execute(
        """
        INSERT INTO usage_log (created_at, chat_id, provider, model, prompt_tokens, completion_tokens, total_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), str(chat_id), provider, model, prompt_tokens, completion_tokens, total_tokens),
    )


def usage_today_totals(chat_id: int | str) -> dict:
    """Суммы за текущие UTC-сутки ДЛЯ ЭТОГО chat_id — используется
    командой /usage (учёт расхода токенов персональный на пользователя)."""
    row = query_one(
        """
        SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
        FROM usage_log
        WHERE date(created_at) = date('now') AND chat_id = ?
        """,
        (str(chat_id),),
    )
    return {"requests": row["requests"], "tokens": row["tokens"]} if row else {"requests": 0, "tokens": 0}


def get_setting(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def get_or_create_secret(key: str, generator=None) -> str:
    """Как get_setting(), но если значения ещё нет — генерирует
    случайную строку (secrets.token_urlsafe), СРАЗУ сохраняет её в
    settings и возвращает. Нужно для секретов, которые не обязаны
    приходить из окружения (TELEGRAM_WEBHOOK_SECRET, CRON_SECRET):
    бот сам придумывает их при первом старте, дальше просто читает
    то же значение из базы при каждом следующем запуске контейнера.
    generator — необязательная фабрика строки, по умолчанию
    secrets.token_urlsafe(32)."""
    existing = get_setting(key)
    if existing:
        return existing
    import secrets as _secrets
    value = generator() if generator else _secrets.token_urlsafe(32)
    set_setting(key, value)
    return value


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                        updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )
