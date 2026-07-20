"""
Разбор входящего update от Telegram и диспетчеризация команд.

На этом шаге (skeleton) поддерживается только /start и эхо-ответ на
любой текст — это нужно, чтобы проверить всю цепочку целиком:
Telegram -> Railway -> наш контейнер -> обратно в Telegram.

Дальше, по мере добавления модулей (заметки, напоминания, поиск,
LLM), сюда будут добавляться новые записи в COMMANDS и новый
дефолтный обработчик текста (сейчас — echo, потом — вызов LLM).
"""
from typing import Callable

from config import config
from telegram.api import send_message
from core.logger import get_logger

log = get_logger(__name__)

CommandHandler = Callable[[int | str, str], None]


def _cmd_start(chat_id: int | str, _args: str) -> None:
    send_message(
        chat_id,
        "Привет! Я на связи. Пока умею только повторять за тобой — "
        "остальные модули (заметки, напоминания, поиск) добавятся позже.",
    )


# Реестр команд вида "/command аргументы". Пополняется по мере
# добавления модулей — каждый новый модуль просто регистрирует
# сюда свои обработчики, не трогая остальной код.
COMMANDS: dict[str, CommandHandler] = {
    "/start": _cmd_start,
}


def _is_owner(chat_id: int | str) -> bool:
    return str(chat_id) == str(config.OWNER_CHAT_ID)


def _default_handler(chat_id: int | str, text: str) -> None:
    """Пока просто эхо. На шаге с LLM-модулем здесь будет вызов
    модели с учётом истории диалога и памяти агента."""
    send_message(chat_id, f"Эхо: {text}")


def handle_update(update: dict) -> None:
    """Точка входа для любого входящего update от Telegram."""
    message = update.get("message")
    if not message:
        # Игнорируем всё, кроме обычных сообщений, на этом шаге
        # (edited_message, callback_query и т.д. добавим при необходимости).
        return

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if chat_id is None:
        return

    if not _is_owner(chat_id):
        log.warning("Отклонено сообщение от постороннего chat_id=%s", chat_id)
        # Намеренно не отвечаем чужим chat_id вообще — чтобы не
        # подтверждать существование бота случайным пользователям.
        return

    if not text:
        return

    command, _, args = text.partition(" ")
    handler = COMMANDS.get(command, _default_handler)
    try:
        handler(chat_id, text if handler is _default_handler else args)
    except Exception:
        log.exception("Ошибка при обработке сообщения")
        send_message(chat_id, "Что-то пошло не так при обработке запроса.")
