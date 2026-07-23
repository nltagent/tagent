"""
Склеивает воедино: историю диалога (modules.memory), самопамять
агента, поиск (modules.search) и низкоуровневый LLM-клиент.

get_reply() — то, что вызывает router.py на любое обычное сообщение.
summarize_history() — то, что compactor.py вызывает, когда пора
сжимать старую часть истории (см. modules/memory/compactor.py).

Шаг 8: модель может запросить НЕСКОЛЬКО поисков за один ответ
([SEARCH: ...] строк подряд, не больше config.SEARCH_MAX_QUERIES_PER_TURN).
Каждый черновой запрос дочищается отдельным лёгким вызовом LLM
(refine_query) перед тем, как реально идти в поисковик — это надёжнее,
чем полагаться на то, что основная (часто бесплатная/слабая) модель
сама сразу напишет запрос в подходящем "поисковом" стиле. Что реально
искалось — видно в самом ответе строкой "🔍 Искал: ...", а не только
в логах контейнера.
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


def refine_query(draft_query: str) -> str:
    """Один лёгкий (короткий, дешёвый) вызов LLM, который переписывает
    черновой поисковый запрос модели в компактный "поисковый" вид.
    При сбое — просто используем черновой запрос как есть, не роняя
    весь ответ из-за этого."""
    try:
        refined = chat_completion(
            [
                {"role": "system", "content": prompts.QUERY_REFINE_INSTRUCTIONS},
                {"role": "user", "content": f"Черновой запрос: {draft_query}"},
            ],
            max_tokens=60,
            temperature=0.2,
        )
    except LLMError:
        log.exception("Не удалось уточнить поисковый запрос — использую черновой как есть")
        return draft_query
    refined = refined.strip().strip('"').strip("«»").strip()
    return refined or draft_query


def _format_search_note(queries: list[str]) -> str:
    if len(queries) == 1:
        return f"🔍 Искал: {queries[0]}"
    return "🔍 Искал:\n" + "\n".join(f"— {q}" for q in queries)


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

    draft_queries = _SEARCH_RE.findall(raw_reply)[: config.SEARCH_MAX_QUERIES_PER_TURN]
    search_note = ""

    if draft_queries:
        refined_queries = []
        result_blocks = []
        for draft in draft_queries:
            refined = refine_query(draft.strip())
            refined_queries.append(refined)
            log.info("Поиск: черновой запрос %r -> уточнённый %r", draft.strip(), refined)
            try:
                results = search_service.search(refined)
                result_blocks.append(search_service.format_for_llm(refined, results))
            except SearchError as e:
                result_blocks.append(f"Поиск по «{refined}» не удался: {e}")

        combined_results = "\n\n".join(result_blocks)
        messages.append({"role": "assistant", "content": raw_reply})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Система: результаты поиска по твоим запросам.\n\n"
                    f"{combined_results}\n\n"
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

        search_note = _format_search_note(refined_queries)

    cleaned_reply, _facts = self_memory.extract_remember_tags(raw_reply)

    dialog_history.record_message(chat_id, conversation_id, "assistant", cleaned_reply)
    compactor.maybe_compact(conversation_id, summarize_history)

    if search_note:
        return f"{search_note}\n\n{cleaned_reply}"
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
