"""
"Самопамять" агента — факты вида "меня зовут Джарвис", которые
должны быть видны модели в любом диалоге ЭТОГО пользователя, а не
только в том, где их сообщили. Хранится отдельно от истории
переписки (см. history.py).

Раньше самопамять была общей на весь бот (один агент — один
пользователь). В мультипользовательской версии она стала личной:
все функции принимают chat_id первым параметром, и факты одного
пользователя не видны другому (см. storage/db.py:_migrate_agent_memory
для переноса старых, ещё общих, данных на OWNER_CHAT_ID).

Формат тега, которым модель сама помечает, что хочет что-то
запомнить (парсится из ответа LLM на шаге с LLM-модулем):
    [REMEMBER: key=value]
Можно несколько тегов в одном ответе, каждый на новой строке или
подряд. Тег вырезается из текста перед отправкой пользователю.
"""
import re

from storage.db import execute, query, now_iso

_REMEMBER_RE = re.compile(r"\[REMEMBER:\s*([^\]=]+?)\s*=\s*([^\]]+?)\s*\]")


def remember(chat_id: int | str, key: str, value: str) -> None:
    execute(
        """
        INSERT INTO agent_memory (chat_id, key, value, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at
        """,
        (str(chat_id), key.strip(), value.strip(), now_iso()),
    )


def forget(chat_id: int | str, key: str) -> bool:
    rows = query(
        "SELECT key FROM agent_memory WHERE chat_id = ? AND key = ?",
        (str(chat_id), key.strip()),
    )
    if not rows:
        return False
    execute(
        "DELETE FROM agent_memory WHERE chat_id = ? AND key = ?",
        (str(chat_id), key.strip()),
    )
    return True


def recall_all(chat_id: int | str) -> dict[str, str]:
    rows = query(
        "SELECT key, value FROM agent_memory WHERE chat_id = ? ORDER BY key",
        (str(chat_id),),
    )
    return {r["key"]: r["value"] for r in rows}


def as_prompt_block(chat_id: int | str) -> str:
    """Готовый текстовый блок для вставки в system-prompt. Пустая
    строка, если у ЭТОГО chat_id ничего не сохранено — тогда в промпт
    ничего не добавляется."""
    facts = recall_all(chat_id)
    if not facts:
        return ""
    lines = "\n".join(f"- {k}: {v}" for k, v in facts.items())
    return (
        "Вот факты, которые ты попросил(а) запомнить о себе или "
        "пользователе в предыдущих диалогах:\n" + lines
    )


def extract_remember_tags(chat_id: int | str, text: str) -> tuple[str, dict[str, str]]:
    """Достаёт все теги [REMEMBER: key=value] из текста ответа модели,
    сохраняет их в agent_memory ЭТОГО chat_id и возвращает
    (очищенный_текст, факты). Используется на шаге с LLM-модулем при
    обработке каждого ответа."""
    found: dict[str, str] = {}
    for key, value in _REMEMBER_RE.findall(text):
        found[key.strip()] = value.strip()
        remember(chat_id, key, value)
    cleaned = _REMEMBER_RE.sub("", text).strip()
    return cleaned, found
