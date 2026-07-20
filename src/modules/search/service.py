"""
Обёртка над Keenable (поиск в интернете без обязательного API-ключа).

ВАЖНО — честно предупреждаю: у меня нет подтверждённой из первых рук
спецификации REST API Keenable (точный путь эндпоинта, имена полей
запроса/ответа). Ниже — правдоподобный вариант в духе большинства
поисковых REST API (GET с query-параметром q, Bearer-токен, JSON с
массивом результатов), но его нужно свериться с реальной
документацией/README проекта Keenable и поправить при необходимости
— именно поэтому вся работа с их HTTP-ответом изолирована в одной
функции _parse_response(), а не размазана по коду.
"""
import json
import urllib.request
import urllib.parse
import urllib.error

from config import config
from core.logger import get_logger
from core.rate_limiter import RateLimiter

log = get_logger(__name__)

_limiter = RateLimiter(min_interval=config.KEENABLE_MIN_INTERVAL)


class SearchError(RuntimeError):
    pass


def _parse_response(raw: dict) -> list[dict]:
    """Приводим ответ Keenable к единому виду: список
    {"title": ..., "url": ..., "snippet": ...}.
    ЕСЛИ реальный формат ответа Keenable отличается — правится только
    здесь."""
    items = raw.get("results") or raw.get("data") or []
    results = []
    for item in items:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", item.get("link", "")),
                "snippet": item.get("snippet", item.get("description", "")),
            }
        )
    return results


def search(query: str, max_results: int = 5) -> list[dict]:
    _limiter.wait_if_needed()

    params = urllib.parse.urlencode({"q": query, "limit": max_results})
    url = f"{config.KEENABLE_BASE_URL}/v1/search?{params}"

    headers = {}
    if config.KEENABLE_API_KEY:
        headers["Authorization"] = f"Bearer {config.KEENABLE_API_KEY}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("Keenable HTTP %s: %s", e.code, body)
        raise SearchError(f"Keenable вернул ошибку {e.code}") from e
    except urllib.error.URLError as e:
        log.error("Keenable network error: %s", e)
        raise SearchError("Не удалось связаться с Keenable") from e

    try:
        return _parse_response(raw)[:max_results]
    except Exception:
        log.exception("Не удалось разобрать ответ Keenable, сырой ответ: %s", raw)
        raise SearchError("Неожиданный формат ответа Keenable — см. логи")


def format_for_llm(query: str, results: list[dict]) -> str:
    if not results:
        return f"По запросу «{query}» Keenable ничего не нашёл."
    lines = [f"Результаты поиска по запросу «{query}»:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}")
    return "\n".join(lines)
