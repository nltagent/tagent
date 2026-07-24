"""
Список моделей у текущего провайдера. Подтверждено по документации
OpenRouter: GET https://openrouter.ai/api/v1/models возвращает
{"data": [{"id", "name", "pricing": {"prompt", "completion"}, ...}]}
— бесплатные вычисляются по pricing.prompt == "0" (плюс подстраховка
на суффикс ":free" в id). Для clavis.to и любых других провайдеров, у
которых цена может лежать в другом поле или формате, простой эвристики
может не хватать — см. llm/model_filter.py, где сырые данные (в том
числе любые поля, похожие на цену) отдаются на анализ самой LLM.

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

_PRICE_HINT_WORDS = ("price", "pricing", "cost", "credit", "free", "tier", "plan")


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


def _fetch_raw() -> list[dict]:
    """Сырые записи моделей ровно как их вернул провайдер (список
    словарей из поля "data") — используется и list_models(), и
    llm/model_filter.py для более умного анализа."""
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

    return [m for m in raw.get("data", []) if m.get("id")]


def list_models() -> list[dict]:
    """Возвращает [{"id", "name", "free": True|False|None}, ...]."""
    return [
        {"id": m.get("id", ""), "name": m.get("name", m.get("id", "")), "free": _is_free(m)}
        for m in _fetch_raw()
    ]


def list_free_models() -> list[dict]:
    """Простая эвристика (pricing.prompt/completion == '0' или суффикс
    ':free'). Для более надёжного варианта, не завязанного на схему
    конкретного провайдера, см. llm/model_filter.classify_free_models()."""
    return [m for m in list_models() if m["free"] is True]


def list_price_hints() -> list[dict]:
    """Компактная версия сырых данных: id модели плюс только те поля,
    которые похожи на цену/тариф по названию (не только "pricing" —
    у разных провайдеров это может называться иначе). Не тащим все
    остальные поля (описание, контекстное окно и т.п.) — они моделям
    для этой конкретной задачи не нужны и только раздувают токены."""
    hints = []
    for m in _fetch_raw():
        entry = {"id": m.get("id", "")}
        for key, value in m.items():
            if any(word in key.lower() for word in _PRICE_HINT_WORDS):
                entry[key] = value
        hints.append(entry)
    return hints
