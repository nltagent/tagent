"""
Несколько профилей LLM-провайдеров одновременно — чтобы можно было
держать ключи сразу от нескольких агрегаторов (OpenRouter, clavis.to,
Ollama и т.п.) и переключаться командой, не трогая .env и не
передеплоивая контейнер.

Профиль "default" — то, что задано в .env (LLM_BASE_URL/LLM_API_KEY/
LLM_MODEL). Он НЕ хранится в SQLite повторно — незачем дублировать
секрет, который и так уже есть в переменных окружения. Остальные
профили (добавленные через /addprovider) хранятся в settings как
JSON.

Важно про безопасность: ключи дополнительных профилей хранятся в том
же SQLite-файле на Railway Volume, что и всё остальное — то есть с
тем же уровнем защиты, что и .env (доступ к volume = доступ к ключам,
не больше и не меньше). Если это неприемлемо для чувствительного
ключа — не добавляйте его через /addprovider, используйте отдельный
Railway-сервис с его собственными переменными окружения.

Активная модель хранится ОТДЕЛЬНО на каждый профиль (id модели одного
провайдера обычно бессмысленен для другого) — ключ settings вида
"llm_model:<имя_профиля>".
"""
import json

from config import config
from storage.db import get_setting, set_setting

_PROFILES_KEY = "llm_provider_profiles"
_ACTIVE_KEY = "llm_active_provider"

DEFAULT_PROFILE_NAME = "default"


class ProviderError(RuntimeError):
    pass


def _load_profiles() -> dict:
    raw = get_setting(_PROFILES_KEY)
    return json.loads(raw) if raw else {}


def _save_profiles(profiles: dict) -> None:
    set_setting(_PROFILES_KEY, json.dumps(profiles, ensure_ascii=False))


def add_profile(name: str, base_url: str, api_key: str, default_model: str = "") -> None:
    name = name.strip().lower()
    if not name or not base_url.strip() or not api_key.strip():
        raise ProviderError("Нужны непустые имя, base_url и api_key.")
    if name == DEFAULT_PROFILE_NAME:
        raise ProviderError(
            f"Имя «{DEFAULT_PROFILE_NAME}» зарезервировано под профиль из .env "
            "(LLM_BASE_URL/LLM_API_KEY) — выберите другое имя."
        )
    profiles = _load_profiles()
    profiles[name] = {
        "base_url": base_url.strip().rstrip("/"),
        "api_key": api_key.strip(),
        "default_model": default_model.strip(),
    }
    _save_profiles(profiles)


def remove_profile(name: str) -> bool:
    name = name.strip().lower()
    profiles = _load_profiles()
    if name not in profiles:
        return False
    del profiles[name]
    _save_profiles(profiles)
    if get_setting(_ACTIVE_KEY) == name:
        set_setting(_ACTIVE_KEY, DEFAULT_PROFILE_NAME)
    return True


def list_profile_names() -> list[str]:
    return [DEFAULT_PROFILE_NAME] + list(_load_profiles().keys())


def get_active_profile_name() -> str:
    return get_setting(_ACTIVE_KEY, DEFAULT_PROFILE_NAME)


def set_active_profile(name: str) -> None:
    name = name.strip().lower()
    if name != DEFAULT_PROFILE_NAME and name not in _load_profiles():
        raise ProviderError(
            f"Профиль «{name}» не найден. Доступны: {', '.join(list_profile_names())}"
        )
    set_setting(_ACTIVE_KEY, name)


def get_credentials_for(name: str) -> tuple[str, str]:
    """Возвращает (base_url, api_key) КОНКРЕТНОГО профиля по имени —
    не обязательно активного. Нужно для отказоустойчивого вызова
    (llm/fallback.py), который перебирает профили, не переключая
    активный (чтобы не менять настройку пользователя ради одной
    попытки)."""
    if name == DEFAULT_PROFILE_NAME:
        return config.LLM_BASE_URL, config.LLM_API_KEY
    profile = _load_profiles().get(name)
    if not profile:
        return config.LLM_BASE_URL, config.LLM_API_KEY
    return profile["base_url"], profile["api_key"]


def get_active_credentials() -> tuple[str, str]:
    """Возвращает (base_url, api_key) активного профиля."""
    return get_credentials_for(get_active_profile_name())


def get_default_model_for_profile(name: str) -> str:
    if name == DEFAULT_PROFILE_NAME:
        return config.LLM_MODEL
    return _load_profiles().get(name, {}).get("default_model", "")
