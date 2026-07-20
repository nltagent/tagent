"""
"Самопамять" агента — факты вида "меня зовут Джарвис", которые
должны быть видны модели в любом диалоге, а не только в том, где их
сообщили. Хранится отдельно от истории переписки (см. history.py).

Формат тега, которым модель сама помечает, что хочет что-то
запомнить (парсится из ответа LLM на шаге с LLM-модулем):
    [REMEMBER: key=value]
Можно несколько тегов в одном ответе, каждый на новой строке или
подряд. Тег вырезается из текста перед отправкой пользователю.
"""
import re

from storage.db import execute, query, now_iso

_REMEMBER_RE = re.compile(r"\[REMEMBER:\s*([^\]=]+?)\s*=\s*([^\]]+?)\s*\]")


def remember(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO agent_memory (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                        updated_at = excluded.updated_at
        """,
        (key.strip(), value.strip(), now_iso()),
    )


def forget(key: str) -> bool:
    rows = query("SELECT key FROM agent_memory WHERE key = ?", (key.strip(),))
    if not rows:
        return False
    execute("DELETE FROM agent_memory WHERE key = ?", (key.strip(),))
    return True


def recall_all() -> dict[str, str]:
    rows = query("SELECT key, value FROM agent_memory ORDER BY key")
    return {r["key"]: r["value"] for r in rows}


def as_prompt_block() -> str:
    """Готовый текстовый блок для вставки в system-prompt. Пустая
    строка, если ничего не сохранено — тогда в промпт ничего не
    добавляется."""
    facts = recall_all()
    if not facts:
        return ""
    lines = "\n".join(f"- {k}: {v}" for k, v in facts.items())
    return (
        "Вот факты, которые ты попросил(а) запомнить о себе или "
        "пользователе в предыдущих диалогах:\n" + lines
    )


def extract_remember_tags(text: str) -> tuple[str, dict[str, str]]:
    """Достаёт все теги [REMEMBER: key=value] из текста ответа модели,
    сохраняет их в agent_memory и возвращает (очищенный_текст, факты).
    Используется на шаге с LLM-модулем при обработке каждого ответа."""
    found: dict[str, str] = {}
    for key, value in _REMEMBER_RE.findall(text):
        found[key.strip()] = value.strip()
        remember(key, value)
    cleaned = _REMEMBER_RE.sub("", text).strip()
    return cleaned, found
