"""
Низкоуровневый клиент к любому OpenAI-совместимому Chat Completions
API (OpenRouter, clavis.to, Ollama и т.п. — все они следуют одному и
тому же контракту: POST {base_url}/chat/completions, Bearer-токен,
тело с messages). Один и тот же код работает для любого числа
провайдеров — какой именно base_url/api_key использовать на этот
конкретный вызов, решает llm/providers.py (несколько ЛИЧНЫХ профилей
на chat_id, переключаемых командой /setprovider).

chat_completion() всегда требует chat_id первым параметром — все
ключи, лимиты и учёт токенов персональные. По умолчанию использует
АКТИВНЫЙ профиль/модель ЭТОГО пользователя, но принимает
необязательные profile_name/model — это нужно llm/fallback.py, чтобы
пробовать другие профили/модели ТОГО ЖЕ пользователя без изменения
его настроек (/setmodel, /setprovider) ради одной попытки.

Лимитер запросов — свой на каждую пару (chat_id, профиль), не общий:
во-первых, у каждого пользователя свой ключ и свои лимиты у
провайдера, во-вторых, исчерпанный лимит одного профиля не должен
мешать попытке fallback на другой.
"""
import json
import time
import urllib.request
import urllib.error

from config import config
from core.logger import get_logger
from core.rate_limiter import RateLimiter
from llm import providers
from llm.providers import ProviderError
from storage.db import log_usage, get_setting, set_setting

log = get_logger(__name__)

_limiters: dict[tuple, RateLimiter] = {}


def _get_limiter(chat_id: int | str, profile_name: str) -> RateLimiter:
    key = (str(chat_id), profile_name)
    if key not in _limiters:
        _limiters[key] = RateLimiter(
            max_per_minute=config.LLM_MAX_PER_MINUTE, min_interval=config.LLM_MIN_INTERVAL
        )
    return _limiters[key]


class LLMError(RuntimeError):
    pass


def get_model_for(chat_id: int | str, profile_name: str) -> str:
    default = providers.get_default_model_for_profile(chat_id, profile_name)
    return get_setting(f"llm_model:{chat_id}:{profile_name}", default)


def get_active_model(chat_id: int | str) -> str:
    profile_name = providers.get_active_profile_name(chat_id)
    if not profile_name:
        return "(не настроено — см. /addprovider)"
    return get_model_for(chat_id, profile_name)


def set_active_model(chat_id: int | str, model_id: str) -> None:
    profile_name = providers.get_active_profile_name(chat_id)
    if not profile_name:
        raise ProviderError(
            "У вас пока не настроен ни один LLM-провайдер — сначала "
            "добавьте профиль: /addprovider имя url ключ"
        )
    set_setting(f"llm_model:{chat_id}:{profile_name}", model_id)


def _usage_provider_tag(profile_name: str, base_url: str) -> str:
    # Для тегов в usage_log — не влияет на сам запрос. Профиль из
    # /addprovider даёт имя напрямую; для "default" (только у
    # создателя) — угадываем по base_url, как раньше.
    if profile_name != providers.DEFAULT_PROFILE_NAME:
        return profile_name
    if "openrouter" in base_url:
        return "openrouter"
    if "clavis" in base_url:
        return "clavis"
    return "custom"


def chat_completion(
    chat_id: int | str,
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.7,
    *,
    profile_name: str | None = None,
    model: str | None = None,
) -> str:
    """Один вызов Chat Completions от имени chat_id. Возвращает текст
    ответа ассистента. Уважает лимит запросов (свой на каждую пару
    chat_id+профиль) и одноразовый retry по 429/Retry-After.

    По умолчанию — активный профиль/модель ЭТОГО пользователя. Явные
    profile_name/model (см. llm/fallback.py) позволяют попробовать
    ДРУГОЙ профиль/модель того же пользователя разово, не трогая
    /setmodel и /setprovider."""
    profile_name = profile_name or providers.get_active_profile_name(chat_id)
    if not profile_name:
        raise LLMError(
            "У вас пока не настроен ни один LLM-провайдер — добавьте "
            "свой: /addprovider имя url ключ"
        )
    try:
        base_url, api_key = providers.get_credentials_for(chat_id, profile_name)
    except ProviderError as e:
        raise LLMError(str(e)) from e

    model = model or get_model_for(chat_id, profile_name)
    if not model:
        raise LLMError(
            f"Для профиля «{profile_name}» не задана модель — укажите: /setmodel <id>"
        )

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    limiter = _get_limiter(chat_id, profile_name)

    for attempt in range(2):
        limiter.wait_if_needed()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt == 0:
                retry_after = float(e.headers.get("Retry-After", "5"))
                log.warning(
                    "LLM 429 (chat_id=%s, %s/%s), жду %.1fс и пробую ещё раз",
                    chat_id, profile_name, model, retry_after,
                )
                time.sleep(retry_after)
                continue
            log.error("LLM HTTP %s (chat_id=%s, %s/%s): %s", e.code, chat_id, profile_name, model, body)
            raise LLMError(f"LLM API вернул ошибку {e.code}") from e
        except urllib.error.URLError as e:
            log.error("LLM network error (chat_id=%s, %s/%s): %s", chat_id, profile_name, model, e)
            raise LLMError("Не удалось связаться с LLM API") from e
    else:
        raise LLMError("LLM API: превышен лимит запросов (429) второй раз подряд")

    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        log.error("Неожиданный формат ответа LLM (chat_id=%s, %s/%s): %s", chat_id, profile_name, model, raw)
        raise LLMError("Неожиданный формат ответа LLM API") from e

    usage = raw.get("usage", {})
    log_usage(
        chat_id,
        provider=_usage_provider_tag(profile_name, base_url),
        model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
    )

    return text
