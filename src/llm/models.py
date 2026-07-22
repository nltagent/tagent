"""
Список моделей у текущего провайдера. Подтверждено по документации
OpenRouter: GET https://openrouter.ai/api/v1/models возвращает
{"data": [{"id", "name", "pricing": {"prompt", "completion"}, ...}]}
— бесплатные вычисляются по pricing.prompt == "0" (плюс подстраховка
на суффикс ":free" в id). У clavis.to тот же путь /v1/models — один
и тот же код работает для обоих, раз оба следуют этому виду.

Если у провайдера нет поля pricing вовсе — считаем принадлежность к
бесплатным неизвестной (free=None), а не молча приравниваем к платным.
"""
import json
import urllib.request
import urllib.error

from config import config
from core.logger import get_logger
from core.rate_limiter import RateLimiter

log = get_logger(__name__)

# Отдельный, лёгкий лимитер — команда вызывается редко (руками), не
# стоит делить его с лимитером chat_completion.
_limiter = RateLimiter(min_interval=1.0)


class ModelsError(RuntimeError):
    pass


def _is_free(model: dict) -> bool | None:
    pricing = model.get("pricing")
    model_id = model.get("id", "")
    if model_id.endswith(":free"):
        return True
    if not pricing:
        return None
    prompt_price = str(pricing.get("prompt", ""))
    completion_price = str(pricing.get("completion", ""))
    if prompt_price == "" or completion_price == "":
        return None
    return prompt_price == "0" and completion_price == "0"


def list_models() -> list[dict]:
    """Возвращает [{"id", "name", "free": True|False|None}, ...]."""
    url = f"{config.LLM_BASE_URL.rstrip('/')}/models"
    headers = {}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"

    _limiter.wait_if_needed()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("Models list HTTP %s: %s", e.code, body)
        raise ModelsError(f"Не удалось получить список моделей ({e.code})") from e
    except urllib.error.URLError as e:
        log.error("Models list network error: %s", e)
        raise ModelsError("Не удалось связаться с провайдером за списком моделей") from e

    items = raw.get("data", [])
    return [
        {
            "id": m.get("id", ""),
            "name": m.get("name", m.get("id", "")),
            "free": _is_free(m),
        }
        for m in items
        if m.get("id")
    ]


def list_free_models() -> list[dict]:
    return [m for m in list_models() if m["free"] is True]
