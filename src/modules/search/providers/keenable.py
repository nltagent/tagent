"""
Keenable — https://docs.keenable.ai (спецификация подтверждена по
их официальному OpenAPI: https://docs.keenable.ai/api-reference/openapi.json).

ВАЖНО (по вашей проверке curl'ом): несмотря на то, что документация
называет Keenable "keyless", реальный REST-эндпоинт /v1/search требует
заголовок X-API-Key при каждом запросе — без него отвечает
"Missing API key". Похоже, keyless-режим работает только через их
CLI/MCP-обвязку, а не через голый REST API. Получите ключ на
https://keenable.ai/console и укажите в KEENABLE_API_KEY.

Лимиты (https://docs.keenable.ai/rate-limits.md): с ключом — 10
запросов/сек без часового лимита.
"""
import json
import urllib.request
import urllib.error

from config import config
from core.logger import get_logger
from core.rate_limiter import RateLimiter
from modules.search.errors import SearchError, SearchConfigError

log = get_logger(__name__)

_limiter = RateLimiter(min_interval=config.KEENABLE_MIN_INTERVAL)


def _parse_response(raw: dict) -> list[dict]:
    results = []
    for item in raw.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description") or item.get("snippet", ""),
            }
        )
    return results


def search(query: str, max_results: int = 5, **filters) -> list[dict]:
    if not config.KEENABLE_API_KEY:
        raise SearchConfigError(
            "KEENABLE_API_KEY не задан — вопреки документации, реальный "
            "REST API Keenable требует ключ на каждый запрос. Получите "
            "его на https://keenable.ai/console."
        )

    _limiter.wait_if_needed()

    body = {"query": query, **{k: v for k, v in filters.items() if v is not None}}
    data = json.dumps(body).encode("utf-8")

    url = f"{config.KEENABLE_BASE_URL}/v1/search"
    headers = {"Content-Type": "application/json", "X-API-Key": config.KEENABLE_API_KEY}

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        log.error("Keenable HTTP %s: %s", e.code, body_text)
        raise SearchError(f"Keenable вернул ошибку {e.code}: {body_text[:200]}") from e
    except urllib.error.URLError as e:
        log.error("Keenable network error: %s", e)
        raise SearchError("Не удалось связаться с Keenable") from e

    try:
        return _parse_response(raw)[:max_results]
    except Exception:
        log.exception("Не удалось разобрать ответ Keenable, сырой ответ: %s", raw)
        raise SearchError("Неожиданный формат ответа Keenable — см. логи")
