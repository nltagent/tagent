"""
Self-hosted SearxNG (https://github.com/searxng/searxng) — бесплатная
метапоисковая система, работает через её собственный JSON API
(GET /search?q=...&format=json), без сторонних оберток вроде
MCP-searxng — нам не нужен MCP-протокол, только сам HTTP-эндпоинт,
который MCP-обёртки тоже вызывают под капотом.

ВАЖНО про сам SearxNG (не про наш код): JSON-формат по умолчанию
выключен на большинстве инстансов. В settings.yml вашего инстанса
должно быть:
    search:
      formats:
        - html
        - json
Если формат не включён — SearxNG отвечает 403 Forbidden.
"""
import json
import urllib.request
import urllib.parse
import urllib.error

from config import config
from core.logger import get_logger
from core.rate_limiter import RateLimiter
from modules.search.errors import SearchError

log = get_logger(__name__)

_limiter = RateLimiter(min_interval=config.SEARXNG_MIN_INTERVAL)


def _parse_response(raw: dict) -> list[dict]:
    results = []
    for item in raw.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
        )
    return results


def search(query: str, max_results: int = 5, **_ignored) -> list[dict]:
    if not config.SEARXNG_BASE_URL:
        raise SearchError(
            "SEARXNG_BASE_URL не задан — укажите адрес вашего "
            "self-hosted инстанса SearxNG (например http://localhost:8080)."
        )

    _limiter.wait_if_needed()

    # SearxNG не поддерживает ограничение числа результатов на своей
    # стороне — обрезаем на нашей после получения ответа.
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{config.SEARXNG_BASE_URL.rstrip('/')}/search?{params}"

    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "Mozilla/5.0 (compatible; telegram-agent/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        log.error("SearxNG HTTP %s: %s", e.code, body_text)
        if e.code == 403:
            raise SearchError(
                "SearxNG вернул 403 — вероятно, JSON-формат не включён "
                "в settings.yml вашего инстанса (search.formats: [html, json])."
            ) from e
        raise SearchError(f"SearxNG вернул ошибку {e.code}") from e
    except urllib.error.URLError as e:
        log.error("SearxNG network error: %s", e)
        raise SearchError("Не удалось связаться с вашим SearxNG-инстансом") from e

    try:
        return _parse_response(raw)[:max_results]
    except Exception:
        log.exception("Не удалось разобрать ответ SearxNG, сырой ответ: %s", raw)
        raise SearchError("Неожиданный формат ответа SearxNG — см. логи")
