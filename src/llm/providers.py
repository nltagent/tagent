"""
Несколько профилей LLM-провайдеров одновременно — чтобы можно было
держать ключи сразу от нескольких агрегаторов (OpenRouter, clavis.to,
Ollama и т.п.) и переключаться командой, не трогая .env и не
передеплоивая контейнер.

Мультипользовательский слой: ВСЕ профили теперь личные на chat_id —
каждый пользователь обязан использовать только свои ключи, без
исключений. Профиль "default" (то, что задано в .env —
LLM_BASE_URL/LLM_API_KEY/LLM_MODEL) доступен ТОЛЬКО создателю
(config.OWNER_CHAT_ID) — это его собственный ключ, настроенный при
деплое. Остальные пользователи "default" вообще не видят в списке
профилей и не могут его выбрать — им нужно добавить хотя бы один
свой профиль через /addprovider, иначе любой вызов LLM вернёт
понятную ошибку с подсказкой.

Активная модель хранится ОТДЕЛЬНО на каждый (chat_id, профиль) — id
модели одного провайдера обычно бессмысленен для другого — ключ
settings вида "llm_model:<chat_id>:<имя_профиля>" (см. llm/client.py).
"""
import json

from config import config
from modules.users import service as users_service
from storage.db import get_setting, set_setting

DEFAULT_PROFILE_NAME = "default"


class ProviderError(RuntimeError):
    pass


def _profiles_key(chat_id: int | str) -> str:
    return f"llm_provider_profiles:{chat_id}"


def _active_key(chat_id: int | str) -> str:
    return f"llm_active_provider:{chat_id}"


def _load_profiles(chat_id: int | str) -> dict:
    raw = get_setting(_profiles_key(chat_id))
    return json.loads(raw) if raw else {}


def _save_profiles(chat_id: int | str, profiles: dict) -> None:
    set_setting(_profiles_key(chat_id), json.dumps(profiles, ensure_ascii=False))


def add_profile(
    chat_id: int | str, name: str, base_url: str, api_key: str, default_model: str = ""
) -> None:
    name = name.strip().lower()
    if not name or not base_url.strip() or not api_key.strip():
        raise ProviderError("Нужны непустые имя, base_url и api_key.")
    if name == DEFAULT_PROFILE_NAME:
        raise ProviderError(
            f"Имя «{DEFAULT_PROFILE_NAME}» зарезервировано — выберите другое имя."
        )
    profiles = _load_profiles(chat_id)
    profiles[name] = {
        "base_url": base_url.strip().rstrip("/"),
        "api_key": api_key.strip(),
        "default_model": default_model.strip(),
    }
    _save_profiles(chat_id, profiles)


def remove_profile(chat_id: int | str, name: str) -> bool:
    name = name.strip().lower()
    profiles = _load_profiles(chat_id)
    if name not in profiles:
        return False
    del profiles[name]
    _save_profiles(chat_id, profiles)
    if get_setting(_active_key(chat_id)) == name:
        set_setting(_active_key(chat_id), _fallback_profile(chat_id))
    return True


def _fallback_profile(chat_id: int | str) -> str:
    """Профиль, на который переключаемся, когда активный удалён (или
    ещё не выбирался). Для создателя — "default" (его ключ из .env);
    для всех остальных "default" недоступен ни при каких условиях —
    им остаётся первый из собственных добавленных профилей, либо
    пустая строка, если профилей вообще нет (тогда любой вызов LLM
    вернёт понятную ошибку с просьбой /addprovider)."""
    if users_service.is_owner(chat_id):
        return DEFAULT_PROFILE_NAME
    own = list(_load_profiles(chat_id).keys())
    return own[0] if own else ""


def list_profile_names(chat_id: int | str) -> list[str]:
    own = list(_load_profiles(chat_id).keys())
    if users_service.is_owner(chat_id):
        return [DEFAULT_PROFILE_NAME] + own
    return own


def get_active_profile_name(chat_id: int | str) -> str:
    """Может вернуть пустую строку, если у пользователя ещё нет ни
    одного профиля (не-создатель, ни разу не вызывавший /addprovider) —
    вызывающий код (llm/client.py) обязан это учитывать."""
    stored = get_setting(_active_key(chat_id))
    if stored is not None and stored in list_profile_names(chat_id):
        return stored
    return _fallback_profile(chat_id)


def set_active_profile(chat_id: int | str, name: str) -> None:
    name = name.strip().lower()
    available = list_profile_names(chat_id)
    if name not in available:
        hint = ", ".join(available) if available else "(пока нет ни одного — см. /addprovider)"
        raise ProviderError(f"Профиль «{name}» не найден. Доступны: {hint}")
    set_setting(_active_key(chat_id), name)


def get_credentials_for(chat_id: int | str, name: str) -> tuple[str, str]:
    """Возвращает (base_url, api_key) КОНКРЕТНОГО профиля ЭТОГО
    пользователя по имени — не обязательно активного (нужно для
    llm/fallback.py, который перебирает профили, не переключая
    активный). "default" отдаётся ТОЛЬКО создателю — у всех
    остальных пользователей ключа из .env нет и быть не должно."""
    if name == DEFAULT_PROFILE_NAME:
        if not users_service.is_owner(chat_id):
            raise ProviderError(
                "Профиль «default» доступен только создателю бота — "
                "добавьте свой профиль: /addprovider имя url ключ"
            )
        return config.LLM_BASE_URL, config.LLM_API_KEY
    profile = _load_profiles(chat_id).get(name)
    if not profile:
        raise ProviderError(
            f"Профиль «{name}» не настроен для вас — добавьте его: "
            "/addprovider имя url ключ"
        )
    return profile["base_url"], profile["api_key"]


def get_active_credentials(chat_id: int | str) -> tuple[str, str]:
    """Возвращает (base_url, api_key) активного профиля ЭТОГО
    пользователя. Бросает ProviderError с понятной подсказкой, если
    профилей вообще нет."""
    active = get_active_profile_name(chat_id)
    if not active:
        raise ProviderError(
            "У вас пока не настроен ни один LLM-провайдер — добавьте "
            "свой: /addprovider имя url ключ"
        )
    return get_credentials_for(chat_id, active)


def get_default_model_for_profile(chat_id: int | str, name: str) -> str:
    if name == DEFAULT_PROFILE_NAME and users_service.is_owner(chat_id):
        return config.LLM_MODEL
    return _load_profiles(chat_id).get(name, {}).get("default_model", "")
