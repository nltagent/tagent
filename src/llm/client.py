"""
Низкоуровневый клиент к любому OpenAI-совместимому Chat Completions
API (OpenRouter, clavis.to, Ollama и т.п. — все они следуют одному и
тому же контракту: POST {base_url}/chat/completions, Bearer-токен,
тело с messages). Один и тот же код работает для любого числа
провайдеров — какой именно base_url/api_key использовать на этот
конкретный вызов, решает llm/providers.py (несколько профилей,
переключаемых командой /setprovider, а не только то, что задано в
.env — см. этот модуль за подробностями).

chat_completion() по умолчанию использует АКТИВНЫЙ профиль/модель, но
принимает необязательные profile_name/model — это нужно
llm/fallback.py, чтобы пробовать другие профили/модели БЕЗ изменения
настроек пользователя (/setmodel, /setprovider) ради одной попытки.

Лимитер запросов — свой на каждый профиль (не общий), иначе
исчерпанный лимит одного провайдера мешал бы попытке fallback на
другой, у которого лимит вообще-то ещё есть.
"""
import json
import time
import urllib.request
import urllib.error

from config import config
from core.logger import get_logger
from core.rate_limiter import RateLimiter
from llm import providers
from storage.db import log_usage, get_setting, set_setting

log = get_logger(__name__)

_limiters: dict[str, RateLimiter] = {}


def _get_limiter(profile_name: str) -> RateLimiter:
    if profile_name not in _limiters:
        _limiters[profile_name] = RateLimiter(
            max_per_minute=config.LLM_MAX_PER_MINUTE, min_interval=config.LLM_MIN_INTERVAL
        )
    return _limiters[profile_name]


class LLMError(RuntimeError):
    pass


def get_model_for(profile_name: str) -> str:
    default = providers.get_default_model_for_profile(profile_name)
    return get_setting(f"llm_model:{profile_name}", default)


def get_active_model() -> str:
    return get_model_for(providers.get_active_profile_name())


def set_active_model(model_id: str) -> None:
    profile_name = providers.get_active_profile_name()
    set_setting(f"llm_model:{profile_name}", model_id)


def _usage_provider_tag(profile_name: str, base_url: str) -> str:
    # Для тегов в usage_log — не влияет на сам запрос. Профиль из
    # /addprovider даёт имя напрямую; для "default" — угадываем по
    # base_url, как раньше.
    if profile_name != providers.DEFAULT_PROFILE_NAME:
        return profile_name
    if "openrouter" in base_url:
        return "openrouter"
    if "clavis" in base_url:
        return "clavis"
    return "custom"


def chat_completion(
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.7,
    *,
    profile_name: str | None = None,
    model: str | None = None,
) -> str:
    """Один вызов Chat Completions. Возвращает текст ответа ассистента.
    Уважает лимит запросов (свой на каждый профиль) и одноразовый
    retry по 429/Retry-After.

    По умолчанию — активный профиль/модель. Явные profile_name/model
    (см. llm/fallback.py) позволяют попробовать ДРУГОЙ профиль/модель
    разово, не трогая /setmodel и /setprovider пользователя."""
    profile_name = profile_name or providers.get_active_profile_name()
    base_url, api_key = providers.get_credentials_for(profile_name)
    model = model or get_model_for(profile_name)

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
    limiter = _get_limiter(profile_name)

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
                log.warning("LLM 429 (%s/%s), жду %.1fс и пробую ещё раз", profile_name, model, retry_after)
                time.sleep(retry_after)
                continue
            log.error("LLM HTTP %s (%s/%s): %s", e.code, profile_name, model, body)
            raise LLMError(f"LLM API вернул ошибку {e.code}") from e
        except urllib.error.URLError as e:
            log.error("LLM network error (%s/%s): %s", profile_name, model, e)
            raise LLMError("Не удалось связаться с LLM API") from e
    else:
        raise LLMError("LLM API: превышен лимит запросов (429) второй раз подряд")

    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        log.error("Неожиданный формат ответа LLM (%s/%s): %s", profile_name, model, raw)
        raise LLMError("Неожиданный формат ответа LLM API") from e

    usage = raw.get("usage", {})
    log_usage(
        provider=_usage_provider_tag(profile_name, base_url),
        model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
    )

    return text
