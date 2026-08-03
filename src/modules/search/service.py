"""
Единая точка входа для поиска — прячет от остального кода (router,
llm.orchestrator), какой именно провайдер сейчас активен У КОНКРЕТНОГО
пользователя и какой у него личный ключ/адрес. Добавить третий
провайдер — значит написать modules/search/providers/новый.py с
функцией search(query, max_results, **kwargs) -> list[dict] в общем
формате {"title","url","snippet"} и добавить одну строку в _PROVIDERS.

Мультипользовательский слой: провайдер и ключ/адрес — личные на
chat_id (команды /setsearch и /setsearchkey). Создатель (config.OWNER_
CHAT_ID) — единственный, для кого действует запасной вариант: если
он не задал себе личный ключ через /setsearchkey, используется ключ
из .env (KEENABLE_API_KEY/SEARXNG_BASE_URL) — это его собственный
ключ, настроенный при деплое. Для всех остальных пользователей такого
отката нет: без /setsearchkey поиск вернёт понятную ошибку с
подсказкой, что настроить.

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
from modules.users import service as users_service
from storage.db import get_setting, set_setting

log = get_logger(__name__)

_PROVIDERS = {
    "keenable": keenable,
    "searxng": searxng,
}

# Какой именно credential-kwarg передавать каждому провайдеру при
# вызове .search(), и какое имя у настройки .env для отката создателя.
_CREDENTIAL_KWARG = {"keenable": "api_key", "searxng": "base_url"}
_OWNER_FALLBACK = {"keenable": lambda: config.KEENABLE_API_KEY, "searxng": lambda: config.SEARXNG_BASE_URL}


def available_providers() -> list[str]:
    return list(_PROVIDERS)


def _provider_key(chat_id: int | str) -> str:
    return f"search_provider:{chat_id}"


def _search_key_key(chat_id: int | str, provider: str) -> str:
    return f"search_key:{provider}:{chat_id}"


def get_active_provider_name(chat_id: int | str) -> str:
    return get_setting(_provider_key(chat_id), config.SEARCH_PROVIDER)


def set_active_provider(chat_id: int | str, name: str) -> None:
    name = name.strip().lower()
    if name not in _PROVIDERS:
        raise SearchError(
            f"Неизвестный провайдер: {name}. Доступны: {', '.join(_PROVIDERS)}"
        )
    set_setting(_provider_key(chat_id), name)


def get_search_key(chat_id: int | str, provider: str) -> str:
    """Личный ключ/адрес ЭТОГО пользователя для данного провайдера
    (пустая строка, если не задан). Для создателя, если он ничего не
    задавал сам, отдаёт значение из .env — это его собственный ключ."""
    provider = provider.strip().lower()
    stored = get_setting(_search_key_key(chat_id, provider))
    if stored:
        return stored
    if users_service.is_owner(chat_id) and provider in _OWNER_FALLBACK:
        return _OWNER_FALLBACK[provider]()
    return ""


def set_search_key(chat_id: int | str, provider: str, value: str) -> None:
    provider = provider.strip().lower()
    if provider not in _PROVIDERS:
        raise SearchError(
            f"Неизвестный провайдер: {provider}. Доступны: {', '.join(_PROVIDERS)}"
        )
    set_setting(_search_key_key(chat_id, provider), value.strip())


def search(chat_id: int | str, query: str, max_results: int = 5, **kwargs) -> list[dict]:
    provider_name = get_active_provider_name(chat_id)
    provider = _PROVIDERS[provider_name]
    credential_kwarg = _CREDENTIAL_KWARG[provider_name]
    credential_value = get_search_key(chat_id, provider_name)
    call_kwargs = {credential_kwarg: credential_value, **kwargs}
    try:
        return provider.search(query, max_results=max_results, **call_kwargs)
    except SearchConfigError:
        raise  # не задан ключ/URL — повторная попытка тут не поможет
    except SearchError as e:
        log.warning(
            "Поиск не удался (%s, chat_id=%s) — жду %.1fс и пробую ещё раз (возможно, "
            "провайдер только проснулся после сна): %s",
            provider_name, chat_id, config.SEARCH_RETRY_DELAY_SECONDS, e,
        )
        time.sleep(config.SEARCH_RETRY_DELAY_SECONDS)
        return provider.search(query, max_results=max_results, **call_kwargs)  # вторая неудача — уже по-настоящему


def format_for_llm(query: str, results: list[dict]) -> str:
    if not results:
        return f"По запросу «{query}» ничего не нашлось."
    lines = [f"Результаты поиска по запросу «{query}»:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}")
    return "\n".join(lines)
