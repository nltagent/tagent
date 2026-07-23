"""
Склеивает воедино: историю диалога (modules.memory), самопамять
агента, поиск (modules.search) и низкоуровневый LLM-клиент.

get_reply() — то, что вызывает router.py на любое обычное сообщение.
summarize_history() — то, что compactor.py вызывает, когда пора
сжимать старую часть истории (см. modules/memory/compactor.py).
"""
import re

from config import config
from core.logger import get_logger
from llm import prompts
from llm.client import chat_completion, LLMError
from modules.memory import self_memory, history as dialog_history, compactor
from modules.conversations import service as conversations
from modules.search import service as search_service
from modules.search.service import SearchError

log = get_logger(__name__)

_SEARCH_RE = re.compile(r"\[SEARCH:\s*(.+?)\]")


def _messages_for_llm(conversation_id: int) -> list[dict]:
    summary, active = compactor.build_context(conversation_id)
    system_content = prompts.build_system_prompt()
    if summary:
        system_content += f"\n\nКраткая сводка более ранней части этого диалога:\n{summary}"

    messages = [{"role": "system", "content": system_content}]
    for m in active:
        # В БД роли уже 'user'/'assistant' — ровно то, что нужно API.
        messages.append({"role": m["role"], "content": m["content"]})
    return messages


def get_reply(chat_id: int | str, user_text: str) -> str:
    conversation_id = conversations.get_active_conversation_id(chat_id)
    conversations.touch(conversation_id)
    conversations.maybe_set_title(conversation_id, user_text)

    dialog_history.record_message(chat_id, conversation_id, "user", user_text)

    messages = _messages_for_llm(conversation_id)
    try:
        raw_reply = chat_completion(messages)
    except LLMError as e:
        log.exception("Ошибка вызова LLM")
        return f"Не получилось получить ответ от модели ({e})."

    search_match = _SEARCH_RE.search(raw_reply)
    if search_match:
        query = search_match.group(1).strip()
        log.info("Модель запросила поиск: %s", query)
        try:
            results = search_service.search(query)
            results_text = search_service.format_for_llm(query, results)
        except SearchError as e:
            results_text = f"Поиск не удался: {e}"

        messages.append({"role": "assistant", "content": raw_reply})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Система: результаты поиска по твоему запросу.\n\n"
                    f"{results_text}\n\n"
                    "Дай окончательный ответ пользователю на основе этих "
                    "результатов (без служебных тегов)."
                ),
            }
        )
        try:
            raw_reply = chat_completion(messages)
        except LLMError as e:
            log.exception("Ошибка второго вызова LLM после поиска")
            return f"Нашёл информацию, но не смог сформулировать ответ ({e})."

    cleaned_reply, _facts = self_memory.extract_remember_tags(raw_reply)

    dialog_history.record_message(chat_id, conversation_id, "assistant", cleaned_reply)
    compactor.maybe_compact(conversation_id, summarize_history)

    return cleaned_reply


def summarize_history(old_summary: str, messages_to_archive: list[dict]) -> str:
    """Callback для compactor.py — сжимает старую часть истории в
    короткую сводку одним отдельным вызовом LLM."""
    transcript = "\n".join(
        f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}"
        for m in messages_to_archive
    )
    user_content = (
        f"Предыдущая сводка:\n{old_summary or '(пока пусто)'}\n\n"
        f"Новый кусок переписки для включения в сводку:\n{transcript}"
    )
    messages = [
        {"role": "system", "content": prompts.SUMMARIZE_INSTRUCTIONS},
        {"role": "user", "content": user_content},
    ]
    try:
        return chat_completion(messages, max_tokens=400, temperature=0.3)
    except LLMError:
        log.exception("Не удалось сжать историю — компакция будет отложена")
        return None
