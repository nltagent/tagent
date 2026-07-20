"""
Разбор входящего update от Telegram и диспетчеризация команд.

На этом шаге подключены LLM (llm.orchestrator) и поиск
(modules.search) — обычный текст теперь идёт в модель, а не эхо.
Заметки и память агента — как на предыдущем шаге.
"""
from typing import Callable

from config import config
from telegram.api import send_message
from core.logger import get_logger
from modules.notes import service as notes
from modules.memory import self_memory
from modules.memory import history as dialog_history
from modules.search import service as search_service
from modules.search.service import SearchError
from storage.db import usage_today_totals
from llm import orchestrator

log = get_logger(__name__)

CommandHandler = Callable[[int | str, str], None]


def _cmd_start(chat_id: int | str, _args: str) -> None:
    send_message(
        chat_id,
        "Привет! Я на связи.\n\n"
        "Доступные команды:\n"
        "/note <текст> — сохранить заметку\n"
        "/notes — показать все заметки\n"
        "/delnote <id> — удалить заметку\n"
        "/remember <ключ>=<значение> — запомнить факт о себе/тебе надолго\n"
        "/memory — показать, что я помню\n"
        "/forget <ключ> — забыть факт\n"
        "/history — показать последние сообщения диалога\n"
        "/search <запрос> — прямой поиск в интернете (без LLM)\n"
        "/usage — сколько токенов/запросов к LLM ушло сегодня\n\n"
        "На любой другой текст отвечаю через LLM — при необходимости "
        "модель сама решает, когда нужно поискать в интернете.",
    )


def _cmd_note(chat_id: int | str, args: str) -> None:
    if not args.strip():
        send_message(chat_id, "Использование: /note текст заметки")
        return
    note_id = notes.add_note(chat_id, args.strip())
    send_message(chat_id, f"Заметка #{note_id} сохранена.")


def _cmd_notes(chat_id: int | str, _args: str) -> None:
    items = notes.list_notes(chat_id)
    if not items:
        send_message(chat_id, "Заметок пока нет.")
        return
    lines = [f"#{n['id']} ({n['created_at']}): {n['content']}" for n in items]
    send_message(chat_id, "\n".join(lines))


def _cmd_delnote(chat_id: int | str, args: str) -> None:
    if not args.strip().isdigit():
        send_message(chat_id, "Использование: /delnote <id>")
        return
    ok = notes.delete_note(chat_id, int(args.strip()))
    send_message(chat_id, "Удалено." if ok else "Заметка с таким id не найдена.")


def _cmd_remember(chat_id: int | str, args: str) -> None:
    if "=" not in args:
        send_message(chat_id, "Использование: /remember ключ=значение")
        return
    key, _, value = args.partition("=")
    if not key.strip() or not value.strip():
        send_message(chat_id, "И ключ, и значение должны быть непустыми.")
        return
    self_memory.remember(key, value)
    send_message(chat_id, f"Запомнил: {key.strip()} = {value.strip()}")


def _cmd_memory(chat_id: int | str, _args: str) -> None:
    facts = self_memory.recall_all()
    if not facts:
        send_message(chat_id, "Пока ничего не запомнил.")
        return
    lines = [f"- {k}: {v}" for k, v in facts.items()]
    send_message(chat_id, "Помню:\n" + "\n".join(lines))


def _cmd_forget(chat_id: int | str, args: str) -> None:
    key = args.strip()
    if not key:
        send_message(chat_id, "Использование: /forget ключ")
        return
    ok = self_memory.forget(key)
    send_message(chat_id, "Забыл." if ok else "Такого факта не помню.")


def _cmd_history(chat_id: int | str, _args: str) -> None:
    items = dialog_history.get_all_messages(chat_id, limit=20)
    if not items:
        send_message(chat_id, "История пуста.")
        return
    items.reverse()
    lines = []
    for m in items:
        tag = " [архив]" if m["archived"] else ""
        who = "Я" if m["role"] == "assistant" else "Ты"
        lines.append(f"{who}{tag}: {m['content']}")
    send_message(chat_id, "\n".join(lines))


def _cmd_search(chat_id: int | str, args: str) -> None:
    """Прямой поиск, в обход LLM — быстрый способ проверить, что
    Keenable вообще отвечает, и получить сырые результаты."""
    query = args.strip()
    if not query:
        send_message(chat_id, "Использование: /search запрос")
        return
    try:
        results = search_service.search(query)
    except SearchError as e:
        send_message(chat_id, f"Поиск не удался: {e}")
        return
    send_message(chat_id, search_service.format_for_llm(query, results))


def _cmd_usage(chat_id: int | str, _args: str) -> None:
    totals = usage_today_totals()
    send_message(
        chat_id,
        f"Сегодня: {totals['requests']} запросов к LLM, "
        f"{totals['tokens']} токенов суммарно.",
    )


# Реестр команд вида "/command аргументы". Пополняется по мере
# добавления модулей — каждый новый модуль просто регистрирует
# сюда свои обработчики, не трогая остальной код.
COMMANDS: dict[str, CommandHandler] = {
    "/start": _cmd_start,
    "/note": _cmd_note,
    "/notes": _cmd_notes,
    "/delnote": _cmd_delnote,
    "/remember": _cmd_remember,
    "/memory": _cmd_memory,
    "/forget": _cmd_forget,
    "/history": _cmd_history,
    "/search": _cmd_search,
    "/usage": _cmd_usage,
}


def _is_owner(chat_id: int | str) -> bool:
    return str(chat_id) == str(config.OWNER_CHAT_ID)


def _default_handler(chat_id: int | str, text: str) -> None:
    """Обычный текст — реальный диалог с LLM. orchestrator сам
    записывает историю, при необходимости запускает поиск, парсит
    теги памяти и запускает компакцию, когда пора."""
    reply = orchestrator.get_reply(chat_id, text)
    send_message(chat_id, reply)


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
