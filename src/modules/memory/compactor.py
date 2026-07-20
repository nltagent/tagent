"""
Сжатие (компакция) истории диалога. Сама суммаризация требует вызова
LLM — на этом шаге (SQLite-слой) модуля LLM ещё нет, поэтому
summarize_fn передаётся снаружи как callback. На шаге с LLM это будет:

    from llm.client import summarize_history
    compactor.maybe_compact(chat_id, summarize_history)

Здесь же — только логика "когда" и "что" сжимать, без привязки к
конкретному провайдеру модели.
"""
from typing import Callable

from config import config
from modules.memory import history
from core.logger import get_logger

log = get_logger(__name__)

# summarize_fn(old_summary: str, messages_to_archive: list[dict]) -> new_summary: str
SummarizeFn = Callable[[str, list[dict]], str]


def build_context(chat_id: int | str) -> tuple[str, list[dict]]:
    """То, что нужно подставить в промпт: (summary_старой_части,
    список_живых_сообщений). Используется на шаге с LLM."""
    return history.get_summary(chat_id), history.get_active_messages(chat_id)


def maybe_compact(chat_id: int | str, summarize_fn: SummarizeFn) -> bool:
    """Проверяет, не превышена ли квота токенов на "живую" историю,
    и если да — сворачивает самую старую часть (кроме последних
    HISTORY_KEEP_LAST сообщений) в summary через summarize_fn.

    Возвращает True, если компакция произошла."""
    total = history.active_tokens_total(chat_id)
    if total <= config.HISTORY_TOKEN_BUDGET:
        return False

    active = history.get_active_messages(chat_id)
    if len(active) <= config.HISTORY_KEEP_LAST:
        # Нечего архивировать — вся история и так короче "хвоста",
        # который мы обязуемся хранить целиком. Ждём, пока накопится.
        return False

    to_archive = active[: len(active) - config.HISTORY_KEEP_LAST]
    old_summary = history.get_summary(chat_id)

    log.info(
        "Компакция chat_id=%s: %d токенов, архивирую %d сообщений",
        chat_id, total, len(to_archive),
    )

    new_summary = summarize_fn(old_summary, to_archive)
    history.set_summary(chat_id, new_summary)
    history.archive_messages([m["id"] for m in to_archive])
    return True
