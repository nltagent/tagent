"""
Пользовательские заметки — независимо от памяти диалога модели
(см. modules/memory). Это просто список текстов, которые пользователь
явно попросил сохранить.
"""
from storage.db import execute, query, now_iso


def add_note(chat_id: int | str, content: str) -> int:
    return execute(
        "INSERT INTO notes (chat_id, content, created_at) VALUES (?, ?, ?)",
        (str(chat_id), content, now_iso()),
    )


def list_notes(chat_id: int | str) -> list[dict]:
    rows = query(
        "SELECT id, content, created_at FROM notes WHERE chat_id = ? ORDER BY id DESC",
        (str(chat_id),),
    )
    return [dict(r) for r in rows]


def delete_note(chat_id: int | str, note_id: int) -> bool:
    # Проверяем chat_id, чтобы нельзя было удалить чужую заметку по id.
    existing = query(
        "SELECT id FROM notes WHERE id = ? AND chat_id = ?", (note_id, str(chat_id))
    )
    if not existing:
        return False
    execute("DELETE FROM notes WHERE id = ? AND chat_id = ?", (note_id, str(chat_id)))
    return True
