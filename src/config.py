"""
Централизованная конфигурация. Всё берётся из переменных окружения —
никаких секретов в коде и в git. Смотри .env.example для полного списка.
"""
import os


class ConfigError(RuntimeError):
    """Не хватает обязательной переменной окружения."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Не задана обязательная переменная окружения: {name}")
    return value


class Config:
    # Токен бота, выданный @BotFather
    BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

    # Секрет, который Telegram будет присылать в заголовке
    # X-Telegram-Bot-Api-Secret-Token — так мы отличаем реальные запросы
    # от Telegram от любых случайных POST-запросов на наш публичный URL.
    WEBHOOK_SECRET: str = _require("TELEGRAM_WEBHOOK_SECRET")

    # chat_id владельца — единственного, кому бот будет отвечать на первом
    # этапе. Узнать свой chat_id можно, написав боту и посмотрев логи,
    # либо через @userinfobot.
    OWNER_CHAT_ID: str = _require("OWNER_CHAT_ID")

    # Railway сам прокидывает PORT — на него нужно слушать.
    PORT: int = int(os.environ.get("PORT", "8080"))

    # Путь, на который будет приходить вебхук. Не обязателен к изменению,
    # но пусть тоже будет секретным, а не просто "/webhook".
    WEBHOOK_PATH: str = os.environ.get("TELEGRAM_WEBHOOK_PATH", "/webhook")

    # Путь к файлу SQLite. На Railway сюда нужно примонтировать Volume
    # (Settings -> Volumes -> Mount Path = /data), иначе база будет
    # пропадать при каждом передеплое контейнера.
    DB_PATH: str = os.environ.get("DB_PATH", "/data/agent.db")

    # Сколько последних (неархивированных) сообщений диалога держим
    # в "живом" контексте, не считая порог по токенам.
    HISTORY_KEEP_LAST: int = int(os.environ.get("HISTORY_KEEP_LAST", "20"))

    # Грубый бюджет токенов на историю диалога (без system-prompt и
    # самого нового сообщения) — когда неархивированная история
    # превышает это значение, самая старая часть уходит в компакцию.
    HISTORY_TOKEN_BUDGET: int = int(os.environ.get("HISTORY_TOKEN_BUDGET", "3000"))

    # ── LLM (OpenRouter / clavis.to / любой OpenAI-совместимый шлюз) ──
    LLM_API_KEY: str = _require("LLM_API_KEY")

    # Полный базовый URL БЕЗ /chat/completions на конце, например:
    #   OpenRouter:  https://openrouter.ai/api/v1
    #   clavis.to:   уточните в личном кабинете/документации clavis.to —
    #                на момент написания этого кода у меня нет
    #                подтверждённого публичного адреса их API, поэтому
    #                значение по умолчанию не задаю намеренно.
    LLM_BASE_URL: str = _require("LLM_BASE_URL")

    # Конкретная модель, например "deepseek/deepseek-chat-v3-0324:free"
    # для OpenRouter. Указывается явно, без дефолта — списки доступных
    # моделей и их актуальные идентификаторы меняются слишком часто,
    # чтобы зашивать их в код.
    LLM_MODEL: str = _require("LLM_MODEL")

    # Лимиты запросов к LLM. Для OpenRouter :free-моделей — не больше
    # 20/мин было актуально на момент написания; для других
    # провайдеров/тарифов подберите под их документацию.
    LLM_MAX_PER_MINUTE: int = int(os.environ.get("LLM_MAX_PER_MINUTE", "20"))
    LLM_MIN_INTERVAL: float = float(os.environ.get("LLM_MIN_INTERVAL", "0"))

    # ── Поиск ──
    # Какой провайдер использовать по умолчанию при старте контейнера.
    # На лету можно переключить командой /setsearch — переопределяет
    # это значение и переживает рестарт (хранится в таблице settings).
    SEARCH_PROVIDER: str = os.environ.get("SEARCH_PROVIDER", "keenable")

    # Keenable (https://docs.keenable.ai). Вопреки маркетингу "keyless",
    # реальный REST-эндпоинт требует ключ на каждый запрос (проверено
    # вручную curl'ом) — получите его на https://keenable.ai/console.
    KEENABLE_API_KEY: str = os.environ.get("KEENABLE_API_KEY", "")
    KEENABLE_BASE_URL: str = os.environ.get("KEENABLE_BASE_URL", "https://api.keenable.ai")
    # Ваше стартовое ограничение: не чаще 1 запроса в 0.5 секунды.
    KEENABLE_MIN_INTERVAL: float = float(os.environ.get("KEENABLE_MIN_INTERVAL", "0.5"))

    # Self-hosted SearxNG — свой инстанс, полностью бесплатно. Укажите
    # адрес БЕЗ /search на конце, например http://localhost:8080 или
    # https://ваш-домен. Не забудьте включить json в search.formats
    # в settings.yml вашего инстанса (см. modules/search/providers/searxng.py).
    SEARXNG_BASE_URL: str = os.environ.get("SEARXNG_BASE_URL", "")
    # Небольшая задержка по умолчанию (не 0) — на шаге 8 модель может
    # запросить несколько поисков за один ответ, и не стоит бить по
    # вашему инстансу вообще без пауз между запросами подряд.
    SEARXNG_MIN_INTERVAL: float = float(os.environ.get("SEARXNG_MIN_INTERVAL", "0.3"))

    # Если поиск (любой провайдер) падает с ошибкой — например,
    # self-hosted SearxNG на Railway успел заснуть (Serverless) и не
    # ответил с первого раза — один раз повторяем запрос после паузы.
    # Частая причина именно "холодного старта" после сна контейнера.
    SEARCH_RETRY_DELAY_SECONDS: float = float(os.environ.get("SEARCH_RETRY_DELAY_SECONDS", "2.5"))

    # Максимум поисковых запросов, которые модель может запросить за
    # один свой ответ (шаг 8: несколько [SEARCH: ...] в одном ответе) —
    # защита от случайного "у меня 10 вопросов, ищу всё сразу".
    SEARCH_MAX_QUERIES_PER_TURN: int = int(os.environ.get("SEARCH_MAX_QUERIES_PER_TURN", "3"))

    # На сколько часов кэшировать результат "умного" определения
    # бесплатных моделей (llm/model_filter.py) — сам список моделей и
    # их цены меняются нечасто, не стоит на каждый /models тратить
    # дополнительный вызов LLM.
    FREE_MODELS_CACHE_HOURS: float = float(os.environ.get("FREE_MODELS_CACHE_HOURS", "12"))

    # ── Напоминания и мониторинг (шаг 4) ──
    # Часовой пояс для интерпретации "завтра в 9:00" и т.п. и для
    # отображения времени напоминаний пользователю. Используется
    # stdlib zoneinfo — полный список зон: IANA tz database.
    USER_TIMEZONE: str = os.environ.get("USER_TIMEZONE", "Europe/Amsterdam")

    # Секрет для внутреннего эндпоинта /internal/cron, который дёргает
    # Railway Cron Job (см. README) — не путать с TELEGRAM_WEBHOOK_SECRET,
    # это разные вызовы с разными источниками.
    CRON_SECRET: str = _require("CRON_SECRET")

    # Не чаще раза в столько часов слать отчёт о нагрузке сервера, даже
    # если cron дёргает эндпоинт чаще (нужно чаще ради своевременности
    # напоминаний, но отчёт о сервере так часто не нужен).
    MONITORING_REPORT_INTERVAL_HOURS: float = float(
        os.environ.get("MONITORING_REPORT_INTERVAL_HOURS", "6")
    )

    # ── GitHub (опционально) ──
    # Fine-grained персональный токен с правами Contents: Read and
    # write на нужный репозиторий. Пусто = навык выключен (команда
    # /pushcode вернёт понятную ошибку, а не упадёт).
    GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")

    # Базовая ветка, от которой создаются новые (если не указана явно
    # в команде) — обычно "main", но можно переопределить.
    GITHUB_BASE_BRANCH: str = os.environ.get("GITHUB_BASE_BRANCH", "main")


config = Config()
