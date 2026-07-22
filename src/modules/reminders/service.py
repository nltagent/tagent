"""
Напоминания — хранение и выборка "просроченных" (due) для доставки.
Сама доставка (отправка в Telegram) не здесь, а в scheduler.py —
этот модуль только про данные.
"""
from datetime import datetime

from storage.db import execute, query, now_iso


def add_reminder(chat_id: int | str, message: str, due_at_utc: datetime) -> int:
    return execute(
        """
        INSERT INTO reminders (chat_id, message, due_at, created_at, delivered)
        VALUES (?, ?, ?, ?, 0)
        """,
        (str(chat_id), message, due_at_utc.isoformat(), now_iso()),
    )


def list_pending(chat_id: int | str) -> list[dict]:
    rows = query(
        """
        SELECT id, message, due_at FROM reminders
        WHERE chat_id = ? AND delivered = 0
        ORDER BY due_at ASC
        """,
        (str(chat_id),),
    )
    return [dict(r) for r in rows]


def delete_reminder(chat_id: int | str, reminder_id: int) -> bool:
    existing = query(
        "SELECT id FROM reminders WHERE id = ? AND chat_id = ? AND delivered = 0",
        (reminder_id, str(chat_id)),
    )
    if not existing:
        return False
    execute("DELETE FROM reminders WHERE id = ? AND chat_id = ?", (reminder_id, str(chat_id)))
    return True


def get_due(now_utc: datetime) -> list[dict]:
    """Все недоставленные напоминания (по всем chat_id), время которых
    уже настало — используется scheduler.py на каждом тике cron."""
    rows = query(
        """
        SELECT id, chat_id, message, due_at FROM reminders
        WHERE delivered = 0 AND due_at <= ?
        ORDER BY due_at ASC
        """,
        (now_utc.isoformat(),),
    )
    return [dict(r) for r in rows]


def mark_delivered(reminder_id: int) -> None:
    execute("UPDATE reminders SET delivered = 1 WHERE id = ?", (reminder_id,))
