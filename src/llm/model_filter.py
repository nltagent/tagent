"""
"Умное" определение бесплатных моделей — вместо жёсткого парсинга под
конкретного провайдера (у OpenRouter цена лежит в pricing.prompt/
pricing.completion, но у других провайдеров, например clavis.to,
формат может быть другим и не задокументирован публично — уже была
история с Keenable, когда угаданный формат оказался неверным).

Вместо ещё одной догадки — отдаём модели компактные данные о цене
(llm.models.list_price_hints — id + любые поля, похожие на цену по
названию) и просим её саму разобраться, что бесплатно. Один
дополнительный вызов LLM, но результат кэшируется (settings, TTL —
FREE_MODELS_CACHE_HOURS), так что на каждый /models не тратится новый
запрос.

При сбое анализа — откат на простую эвристику llm.models.list_free_models().
"""
import json
from datetime import datetime, timezone

from config import config
from core.logger import get_logger
from llm.client import chat_completion, LLMError
from llm import models as llm_models
from storage.db import get_setting, set_setting

log = get_logger(__name__)

_CACHE_KEY = "free_models_cache"

_ANALYZE_SYSTEM = (
    "Тебе даны данные о моделях от API-провайдера в формате JSON: у "
    "каждой записи есть id и произвольные поля, как-то связанные с "
    "ценой (могут называться pricing, price, cost, credit, tier и "
    "т.п., в разных форматах и единицах — разберись по смыслу, у "
    "какой модели реальная стоимость использования равна нулю). "
    "Верни СПИСОК id бесплатных моделей, по одному id на строке, без "
    "нумерации, кавычек и какого-либо ещё текста. Если бесплатных "
    "моделей нет вовсе — верни пустой ответ."
)


def _read_cache() -> list[dict] | None:
    raw = get_setting(_CACHE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    cached_at = datetime.fromisoformat(data["cached_at"])
    age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
    if age_hours > config.FREE_MODELS_CACHE_HOURS:
        return None
    return data["models"]


def _write_cache(models: list[dict]) -> None:
    payload = json.dumps(
        {"cached_at": datetime.now(timezone.utc).isoformat(), "models": models},
        ensure_ascii=False,
    )
    set_setting(_CACHE_KEY, payload)


def classify_free_models(force_refresh: bool = False) -> list[dict]:
    """Возвращает [{"id", "name"}, ...] — модели, которые LLM сочла
    бесплатными по анализу сырых данных о цене. Результат кэшируется
    на FREE_MODELS_CACHE_HOURS часов; force_refresh=True игнорирует кэш."""
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    try:
        raw = llm_models._fetch_raw()  # один HTTP-запрос на весь анализ
    except llm_models.ModelsError:
        raise  # это ошибка сети/API, а не анализа — пусть вызывающий код её обработает

    if not raw:
        return []

    hints = [
        {
            "id": m.get("id", ""),
            **{k: v for k, v in m.items() if any(w in k.lower() for w in llm_models._PRICE_HINT_WORDS)},
        }
        for m in raw
    ]
    id_to_name = {m.get("id", ""): m.get("name", m.get("id", "")) for m in raw}

    try:
        reply = chat_completion(
            [
                {"role": "system", "content": _ANALYZE_SYSTEM},
                {"role": "user", "content": json.dumps(hints, ensure_ascii=False)},
            ],
            max_tokens=2000,
            temperature=0,
        )
        free_ids = {line.strip() for line in reply.splitlines() if line.strip()}
    except LLMError:
        log.exception("Не удалось проанализировать модели через LLM — использую эвристику")
        fallback = [
            {"id": m.get("id", ""), "name": id_to_name[m.get("id", "")]}
            for m in raw
            if llm_models._is_free(m) is True
        ]
        _write_cache(fallback)
        return fallback

    result = [{"id": mid, "name": id_to_name.get(mid, mid)} for mid in id_to_name if mid in free_ids]
    _write_cache(result)
    return result
