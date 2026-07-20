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


config = Config()
