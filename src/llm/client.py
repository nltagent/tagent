"""
Низкоуровневый клиент к любому OpenAI-совместимому Chat Completions
API (OpenRouter, clavis.to, Ollama и т.п. — все они следуют одному и
тому же контракту: POST {base_url}/chat/completions, Bearer-токен,
тело с messages). Один и тот же код работает для любого числа
провайдеров — какой именно base_url/api_key использовать на этот
конкретный вызов, решает llm/providers.py (несколько профилей,
переключаемых командой /setprovider, а не только то, что задано в
.env — см. этот модуль за подробностями).
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

_limiter = RateLimiter(
    max_per_minute=config.LLM_MAX_PER_MINUTE, min_interval=config.LLM_MIN_INTERVAL
)


class LLMError(RuntimeError):
    pass


def get_active_model() -> str:
    profile_name = providers.get_active_profile_name()
    default = providers.get_default_model_for_profile(profile_name)
    return get_setting(f"llm_model:{profile_name}", default)


def set_active_model(model_id: str) -> None:
    profile_name = providers.get_active_profile_name()
    set_setting(f"llm_model:{profile_name}", model_id)


def _provider_name() -> str:
    # Для тегов в usage_log — не влияет на сам запрос. Профиль из
    # /addprovider даёт имя напрямую; для "default" — угадываем по
    # base_url, как раньше.
    profile_name = providers.get_active_profile_name()
    if profile_name != providers.DEFAULT_PROFILE_NAME:
        return profile_name
    base_url, _ = providers.get_active_credentials()
    if "openrouter" in base_url:
        return "openrouter"
    if "clavis" in base_url:
        return "clavis"
    return "custom"


def chat_completion(messages: list[dict], max_tokens: int = 1000, temperature: float = 0.7) -> str:
    """Один вызов Chat Completions. Возвращает текст ответа ассистента.
    Уважает лимит запросов и одноразовый retry по 429/Retry-After."""
    base_url, api_key = providers.get_active_credentials()
    model = get_active_model()
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

    for attempt in range(2):
        _limiter.wait_if_needed()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt == 0:
                retry_after = float(e.headers.get("Retry-After", "5"))
                log.warning("LLM 429, жду %.1fс и пробую ещё раз", retry_after)
                time.sleep(retry_after)
                continue
            log.error("LLM HTTP %s: %s", e.code, body)
            raise LLMError(f"LLM API вернул ошибку {e.code}") from e
        except urllib.error.URLError as e:
            log.error("LLM network error: %s", e)
            raise LLMError("Не удалось связаться с LLM API") from e
    else:
        raise LLMError("LLM API: превышен лимит запросов (429) второй раз подряд")

    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        log.error("Неожиданный формат ответа LLM: %s", raw)
        raise LLMError("Неожиданный формат ответа LLM API") from e

    usage = raw.get("usage", {})
    log_usage(
        provider=_provider_name(),
        model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
    )

    return text
