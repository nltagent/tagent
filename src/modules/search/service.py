"""
Единая точка входа для поиска — прячет от остального кода (router,
llm.orchestrator), какой именно провайдер сейчас активен. Добавить
третий провайдер — значит написать modules/search/providers/новый.py
с функцией search(query, max_results, **kwargs) -> list[dict] в общем
формате {"title","url","snippet"} и добавить одну строку в _PROVIDERS.

Переключение провайдера:
- SEARCH_PROVIDER в .env — что использовать по умолчанию при старте;
- команда /setsearch <имя> — переключает на лету, сохраняется в
  settings (таблица в SQLite) и переживает рестарт контейнера, пока
  явно не переключат обратно.

Retry: если поиск падает с ошибкой — типичная причина для self-hosted
SearxNG на Railway — контейнер успел заснуть (Serverless) и первый
запрос попадает на "холодный старт", не дождавшись ответа. Один раз
повторяем после паузы (SEARCH_RETRY_DELAY_SECONDS) — если и это не
помогло, значит проблема настоящая, и ошибка уходит наверх как есть.
"""
import time

from config import config
from core.logger import get_logger
from modules.search.errors import SearchError, SearchConfigError
from modules.search.providers import keenable, searxng
from storage.db import get_setting, set_setting

log = get_logger(__name__)

_PROVIDERS = {
    "keenable": keenable,
    "searxng": searxng,
}


def available_providers() -> list[str]:
    return list(_PROVIDERS)


def get_active_provider_name() -> str:
    return get_setting("search_provider", config.SEARCH_PROVIDER)


def set_active_provider(name: str) -> None:
    name = name.strip().lower()
    if name not in _PROVIDERS:
        raise SearchError(
            f"Неизвестный провайдер: {name}. Доступны: {', '.join(_PROVIDERS)}"
        )
    set_setting("search_provider", name)


def search(query: str, max_results: int = 5, **kwargs) -> list[dict]:
    provider = _PROVIDERS[get_active_provider_name()]
    try:
        return provider.search(query, max_results=max_results, **kwargs)
    except SearchConfigError:
        raise  # не задан ключ/URL — повторная попытка тут не поможет
    except SearchError as e:
        log.warning(
            "Поиск не удался (%s) — жду %.1fс и пробую ещё раз (возможно, "
            "провайдер только проснулся после сна): %s",
            get_active_provider_name(), config.SEARCH_RETRY_DELAY_SECONDS, e,
        )
        time.sleep(config.SEARCH_RETRY_DELAY_SECONDS)
        return provider.search(query, max_results=max_results, **kwargs)  # вторая неудача — уже по-настоящему


def format_for_llm(query: str, results: list[dict]) -> str:
    if not results:
        return f"По запросу «{query}» ничего не нашлось."
    lines = [f"Результаты поиска по запросу «{query}»:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}")
    return "\n".join(lines)
