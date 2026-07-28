"""
Отказоустойчивый вызов LLM. Если активная модель/провайдер прямо
сейчас недоступны (модель временно отключена у провайдера, 5xx и
т.п. — НЕ ошибки конфигурации, ключ уже проверен на этапе
/addprovider), пробуем по очереди:
  1. другие бесплатные модели ТОГО ЖЕ провайдера;
  2. другие настроенные профили провайдеров (по порядку /providers) —
     и их бесплатные модели.

Каждый профиль в системе по определению уже имеет свой ключ (его
нельзя добавить через /addprovider без api_key) — поэтому здесь не
нужно отдельно проверять "есть ли смысл пробовать этот провайдер":
раз профиль существует, значит ключ для него уже есть.

Важно: попытки НЕ меняют /setmodel или /setprovider пользователя —
chat_completion() здесь вызывается с явными profile_name/model
(см. llm/client.py), а не через глобальные "активные" настройки. Если
что-то из перебора сработало — это разовая подмена только для этого
конкретного ответа; в следующий раз бот снова начнёт с вашей
настроенной по умолчанию модели (вдруг она к тому моменту уже
починилась) и запомнит новый fallback только если снова понадобится.
"""
from core.logger import get_logger
from llm.client import chat_completion, get_model_for, LLMError
from llm import providers
from llm import model_filter

log = get_logger(__name__)


class AllProvidersFailedError(LLMError):
    pass


class FallbackResult:
    def __init__(self, text: str, profile_name: str, model: str, used_fallback: bool):
        self.text = text
        self.profile_name = profile_name
        self.model = model
        self.used_fallback = used_fallback


def resilient_chat_completion(
    messages: list[dict], max_tokens: int = 1000, temperature: float = 0.7
) -> FallbackResult:
    """Пробует активную модель, затем бесплатные модели того же
    профиля, затем другие профили (и их бесплатные модели), пока
    что-то не ответит. Бросает AllProvidersFailedError, если совсем
    ничего не сработало."""
    original_profile = providers.get_active_profile_name()
    profile_order = [original_profile] + [
        p for p in providers.list_profile_names() if p != original_profile
    ]

    tried: list[str] = []
    is_first_attempt = True

    for profile_name in profile_order:
        candidates = [get_model_for(profile_name)]
        try:
            free_models = model_filter.classify_free_models()
            # classify_free_models всегда смотрит на АКТИВНЫЙ профиль —
            # если это не тот профиль, что мы сейчас перебираем,
            # пропускаем (иначе список моделей будет не от того
            # провайдера). Переключать активный профиль ради этого не
            # хотим — оставляем как есть, добавим бесплатные модели
            # только для того профиля, который сейчас активен.
            if profile_name == providers.get_active_profile_name():
                candidates += [m["id"] for m in free_models if m["id"] not in candidates]
        except Exception:
            log.warning("Не удалось получить список бесплатных моделей для %s", profile_name)

        for model_id in candidates:
            try:
                text = chat_completion(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    profile_name=profile_name,
                    model=model_id,
                )
                return FallbackResult(
                    text=text,
                    profile_name=profile_name,
                    model=model_id,
                    used_fallback=not is_first_attempt,
                )
            except LLMError as e:
                tried.append(f"{profile_name}/{model_id}")
                log.warning("Модель %s/%s не ответила: %s", profile_name, model_id, e)
            is_first_attempt = False

    raise AllProvidersFailedError(
        f"Ни одна модель не ответила. Пробовал: {', '.join(tried)}"
    )


def resilient_chat_completion_text(
    messages: list[dict], max_tokens: int = 1000, temperature: float = 0.7
) -> str:
    """Как resilient_chat_completion, но возвращает только текст — тот
    же контракт, что у llm.client.chat_completion, чтобы можно было
    подставить сюда без переделки вызывающего кода (см.
    llm/orchestrator.py). Если сработал fallback — это видно в логах
    (профиль/модель, которые реально ответили), в сам ответ
    пользователю ничего не добавляется."""
    result = resilient_chat_completion(messages, max_tokens=max_tokens, temperature=temperature)
    if result.used_fallback:
        log.warning(
            "Ответ получен через fallback: профиль=%s модель=%s "
            "(активная модель/провайдер были недоступны)",
            result.profile_name, result.model,
        )
    return result.text
