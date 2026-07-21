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
    SEARXNG_MIN_INTERVAL: float = float(os.environ.get("SEARXNG_MIN_INTERVAL", "0"))


config = Config()
