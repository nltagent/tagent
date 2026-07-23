"""
Ветки диалогов. Один chat_id может иметь несколько диалогов
(conversations) — у каждого своя история и summary (см.
modules/memory/history.py, которая теперь оперирует conversation_id,
а не chat_id напрямую). Какой диалог сейчас активен — хранится в
таблице settings под ключом f"active_conversation:{chat_id}"
(переиспользует storage.db.get_setting/set_setting из шага 3).

Самопамять агента (modules/memory/self_memory.py) в это не входит —
она остаётся общей для всех диалогов, как и раньше.
"""
from storage.db import execute, query, query_one, get_setting, set_setting, now_iso

_ACTIVE_KEY_PREFIX = "active_conversation:"


def _active_key(chat_id: int | str) -> str:
    return f"{_ACTIVE_KEY_PREFIX}{chat_id}"


def create_conversation(chat_id: int | str, title: str = "") -> int:
    now = now_iso()
    conversation_id = execute(
        """
        INSERT INTO conversations (chat_id, title, status, summary, created_at, last_active_at)
        VALUES (?, ?, 'active', '', ?, ?)
        """,
        (str(chat_id), title, now, now),
    )
    set_setting(_active_key(chat_id), str(conversation_id))
    return conversation_id


def get_conversation(conversation_id: int) -> dict | None:
    row = query_one(
        "SELECT id, chat_id, title, status, created_at, last_active_at FROM conversations WHERE id = ?",
        (conversation_id,),
    )
    return dict(row) if row else None


def list_conversations(chat_id: int | str, include_closed: bool = False) -> list[dict]:
    if include_closed:
        rows = query(
            """
            SELECT id, title, status, created_at, last_active_at FROM conversations
            WHERE chat_id = ? ORDER BY last_active_at DESC
            """,
            (str(chat_id),),
        )
    else:
        rows = query(
            """
            SELECT id, title, status, created_at, last_active_at FROM conversations
            WHERE chat_id = ? AND status = 'active' ORDER BY last_active_at DESC
            """,
            (str(chat_id),),
        )
    return [dict(r) for r in rows]


def get_active_conversation_id(chat_id: int | str) -> int:
    """Возвращает id текущего активного диалога, создавая новый, если
    его ещё нет или сохранённый оказался закрыт (например, закрыли из
    другого клиента прямо во время работы)."""
    stored = get_setting(_active_key(chat_id))
    if stored is not None:
        conv = get_conversation(int(stored))
        if conv and conv["status"] == "active" and str(conv["chat_id"]) == str(chat_id):
            return conv["id"]
    return create_conversation(chat_id)


def switch_conversation(chat_id: int | str, conversation_id: int) -> bool:
    conv = get_conversation(conversation_id)
    if not conv or str(conv["chat_id"]) != str(chat_id):
        return False
    if conv["status"] != "active":
        return False
    set_setting(_active_key(chat_id), str(conversation_id))
    return True


def close_conversation(chat_id: int | str, conversation_id: int) -> bool:
    conv = get_conversation(conversation_id)
    if not conv or str(conv["chat_id"]) != str(chat_id) or conv["status"] != "active":
        return False

    execute("UPDATE conversations SET status = 'closed' WHERE id = ?", (conversation_id,))

    was_active = get_setting(_active_key(chat_id)) == str(conversation_id)
    if was_active:
        # Закрыли именно текущий диалог — сразу заводим новый, чтобы
        # следующее сообщение не осталось без активного диалога.
        create_conversation(chat_id)
    return True


def touch(conversation_id: int) -> None:
    execute(
        "UPDATE conversations SET last_active_at = ? WHERE id = ?",
        (now_iso(), conversation_id),
    )


def maybe_set_title(conversation_id: int, text: str, max_len: int = 40) -> None:
    """Если у диалога ещё нет названия — берём его из первого
    сообщения (обрезая), чтобы список /dialogs был осмысленным без
    лишнего вызова LLM специально под заголовок."""
    conv = query_one("SELECT title FROM conversations WHERE id = ?", (conversation_id,))
    if conv is None or conv["title"]:
        return
    title = text.strip().replace("\n", " ")[:max_len]
    execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
