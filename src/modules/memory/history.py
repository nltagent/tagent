"""
История переписки. Всё пишется в messages навсегда (можно
пролистать всю историю по команде), но в промпт для LLM попадает
только "живая" (archived=0) часть плюс summary свёрнутой части —
см. compactor.py.

Шаг 7 (ветки диалогов): всё это теперь привязано к conversation_id,
а не к chat_id напрямую — у одного chat_id может быть несколько
диалогов (modules/conversations/service.py), у каждого своя история
и своя summary. chat_id всё ещё пишется в строку — просто для
удобства прямых запросов к базе, выборки идут по conversation_id.
"""
from storage.db import execute, query, query_one, now_iso


def estimate_tokens(text: str) -> int:
    """Грубая оценка без токенизатора: ~4 символа на токен с запасом.
    Точность не нужна — это только для бюджетирования контекста."""
    return max(1, len(text) // 4)


def record_message(chat_id: int | str, conversation_id: int, role: str, content: str) -> int:
    return execute(
        """
        INSERT INTO messages (chat_id, conversation_id, role, content, tokens_est, created_at, archived)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (str(chat_id), conversation_id, role, content, estimate_tokens(content), now_iso()),
    )


def get_active_messages(conversation_id: int) -> list[dict]:
    """Неархивированная история диалога, по возрастанию времени — то,
    что пойдёт в промпт вместе с summary."""
    rows = query(
        """
        SELECT id, role, content, tokens_est, created_at FROM messages
        WHERE conversation_id = ? AND archived = 0
        ORDER BY id ASC
        """,
        (conversation_id,),
    )
    return [dict(r) for r in rows]


def get_all_messages(conversation_id: int, limit: int = 100) -> list[dict]:
    """Вся история диалога (включая архивную) для команды просмотра —
    новые сначала."""
    rows = query(
        """
        SELECT id, role, content, created_at, archived FROM messages
        WHERE conversation_id = ? ORDER BY id DESC LIMIT ?
        """,
        (conversation_id, limit),
    )
    return [dict(r) for r in rows]


def active_tokens_total(conversation_id: int) -> int:
    row = query_one(
        "SELECT COALESCE(SUM(tokens_est), 0) AS total FROM messages "
        "WHERE conversation_id = ? AND archived = 0",
        (conversation_id,),
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


def get_summary(conversation_id: int) -> str:
    row = query_one("SELECT summary FROM conversations WHERE id = ?", (conversation_id,))
    return row["summary"] if row else ""


def set_summary(conversation_id: int, summary: str) -> None:
    execute(
        "UPDATE conversations SET summary = ?, summary_updated_at = ? WHERE id = ?",
        (summary, now_iso(), conversation_id),
    )
