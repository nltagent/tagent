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
"""
from config import config
from modules.search.errors import SearchError
from modules.search.providers import keenable, searxng
from storage.db import get_setting, set_setting

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
    return provider.search(query, max_results=max_results, **kwargs)


def format_for_llm(query: str, results: list[dict]) -> str:
    if not results:
        return f"По запросу «{query}» ничего не нашлось."
    lines = [f"Результаты поиска по запросу «{query}»:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}")
    return "\n".join(lines)
