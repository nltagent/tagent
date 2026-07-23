"""
Сжатие (компакция) истории диалога. Сама суммаризация требует вызова
LLM, поэтому summarize_fn передаётся снаружи как callback:

    from llm.orchestrator import summarize_history
    compactor.maybe_compact(conversation_id, summarize_history)

Здесь же — только логика "когда" и "что" сжимать, без привязки к
конкретному провайдеру модели.
"""
from typing import Callable, Optional

from config import config
from modules.memory import history
from core.logger import get_logger

log = get_logger(__name__)

# summarize_fn(old_summary, messages_to_archive) -> новая сводка,
# либо None, если суммаризация не удалась (тогда компакция откладывается).
SummarizeFn = Callable[[str, list[dict]], Optional[str]]


def build_context(conversation_id: int) -> tuple[str, list[dict]]:
    """То, что нужно подставить в промпт: (summary_старой_части,
    список_живых_сообщений). Используется на шаге с LLM."""
    return history.get_summary(conversation_id), history.get_active_messages(conversation_id)


def maybe_compact(conversation_id: int, summarize_fn: SummarizeFn) -> bool:
    """Проверяет, не превышена ли квота токенов на "живую" историю,
    и если да — сворачивает самую старую часть (кроме последних
    HISTORY_KEEP_LAST сообщений) в summary через summarize_fn.

    Возвращает True, если компакция произошла."""
    total = history.active_tokens_total(conversation_id)
    if total <= config.HISTORY_TOKEN_BUDGET:
        return False

    active = history.get_active_messages(conversation_id)
    if len(active) <= config.HISTORY_KEEP_LAST:
        # Нечего архивировать — вся история и так короче "хвоста",
        # который мы обязуемся хранить целиком. Ждём, пока накопится.
        return False

    to_archive = active[: len(active) - config.HISTORY_KEEP_LAST]
    old_summary = history.get_summary(conversation_id)

    log.info(
        "Компакция conversation_id=%s: %d токенов, архивирую %d сообщений",
        conversation_id, total, len(to_archive),
    )

    new_summary = summarize_fn(old_summary, to_archive)
    if new_summary is None:
        # summarize_fn сигнализирует неудачу через None (например,
        # LLM была недоступна) — не архивируем, чтобы не потерять эти
        # сообщения из контекста впустую. Попробуем на следующей проверке.
        log.warning("Компакция conversation_id=%s отложена: summarize_fn вернул None", conversation_id)
        return False
    history.set_summary(conversation_id, new_summary)
    history.archive_messages([m["id"] for m in to_archive])
    return True
