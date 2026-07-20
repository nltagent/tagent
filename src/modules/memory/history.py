"""
История переписки. Всё пишется в messages навсегда (можно
пролистать всю историю по команде), но в промпт для LLM попадает
только "живая" (archived=0) часть плюс summary свёрнутой части —
см. compactor.py.
"""
from storage.db import execute, query, query_one, now_iso


def estimate_tokens(text: str) -> int:
    """Грубая оценка без токенизатора: ~4 символа на токен с запасом.
    Точность не нужна — это только для бюджетирования контекста."""
    return max(1, len(text) // 4)


def record_message(chat_id: int | str, role: str, content: str) -> int:
    return execute(
        """
        INSERT INTO messages (chat_id, role, content, tokens_est, created_at, archived)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (str(chat_id), role, content, estimate_tokens(content), now_iso()),
    )


def get_active_messages(chat_id: int | str) -> list[dict]:
    """Неархивированная история, по возрастанию времени — то, что
    пойдёт в промпт вместе с summary."""
    rows = query(
        """
        SELECT id, role, content, tokens_est, created_at FROM messages
        WHERE chat_id = ? AND archived = 0
        ORDER BY id ASC
        """,
        (str(chat_id),),
    )
    return [dict(r) for r in rows]


def get_all_messages(chat_id: int | str, limit: int = 100) -> list[dict]:
    """Вся история (включая архивную) для команды просмотра прошлых
    диалогов — новые сначала."""
    rows = query(
        """
        SELECT id, role, content, created_at, archived FROM messages
        WHERE chat_id = ? ORDER BY id DESC LIMIT ?
        """,
        (str(chat_id), limit),
    )
    return [dict(r) for r in rows]


def active_tokens_total(chat_id: int | str) -> int:
    row = query_one(
        "SELECT COALESCE(SUM(tokens_est), 0) AS total FROM messages "
        "WHERE chat_id = ? AND archived = 0",
        (str(chat_id),),
    )
    return row["total"] if row else 0


def archive_messages(message_ids: list[int]) -> None:
    if not message_ids:
        return
    placeholders = ",".join("?" for _ in message_ids)
    execute(
        f"UPDATE messages SET archived = 1 WHERE id IN ({placeholders})",
        tuple(message_ids),
    )


def get_summary(chat_id: int | str) -> str:
    row = query_one(
        "SELECT summary FROM conversation_meta WHERE chat_id = ?", (str(chat_id),)
    )
    return row["summary"] if row else ""


def set_summary(chat_id: int | str, summary: str) -> None:
    execute(
        """
        INSERT INTO conversation_meta (chat_id, summary, summary_updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET summary = excluded.summary,
                                            summary_updated_at = excluded.summary_updated_at
        """,
        (str(chat_id), summary, now_iso()),
    )
