"""
Разбор входящего update от Telegram и диспетчеризация команд.

Шаг 4 добавил напоминания (modules.reminders) и мониторинг сервера
(modules.monitoring, /status) — сама доставка напоминаний и
периодический отчёт идут через scheduler.py по тику Railway Cron Job,
не отсюда.
"""
from typing import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from config import config
from telegram.api import send_message
from core.logger import get_logger
from modules.notes import service as notes
from modules.memory import self_memory
from modules.memory import history as dialog_history
from modules.conversations import service as conversations
from modules.search import service as search_service
from modules.search.service import SearchError
from modules.reminders import service as reminders_service
from modules.reminders.timeparse import parse_when, TimeParseError
from modules.monitoring import reporter as monitoring_reporter
from modules.github import service as github_service
from modules.github.service import GitHubError
from modules.github import editor as github_editor
from modules.github.editor import EditError
from storage.db import usage_today_totals
from llm import orchestrator
from llm import models as llm_models
from llm.client import get_active_model, set_active_model
from llm.models import ModelsError

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
        "/dialogs [все] — список диалогов, /newdialog — начать новый\n"
        "/switchdialog <id> — переключиться на диалог\n"
        "/closedialog [id] — закрыть диалог (по умолчанию текущий)\n"
        "/search <запрос> — прямой поиск в интернете (без LLM)\n"
        "/setsearch <keenable|searxng> — переключить провайдера поиска\n"
        "/usage — сколько токенов/запросов к LLM ушло сегодня\n"
        "/remind <когда> <текст> — напоминание, например «через 10 минут ...»\n"
        "/reminders — активные напоминания\n"
        "/delremind <id> — удалить напоминание\n"
        "/status — состояние сервера (память/нагрузка/диск)\n"
        "/models [все] — бесплатные (или все) модели провайдера\n"
        "/setmodel <id> — переключить модель для ответов\n"
        "/pushcode owner/repo ветка путь/файл + код на след. строках — "
        "закоммитить готовый код в отдельную ветку на GitHub\n"
        "/editcode owner/repo ветка + пути файлов + --- + инструкция — "
        "модель сама перепишет файл(ы) по запросу и запушит одним коммитом\n\n"
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
    conversation_id = conversations.get_active_conversation_id(chat_id)
    items = dialog_history.get_all_messages(conversation_id, limit=20)
    if not items:
        send_message(chat_id, "История текущего диалога пуста.")
        return
    items.reverse()
    lines = []
    for m in items:
        tag = " [архив]" if m["archived"] else ""
        who = "Я" if m["role"] == "assistant" else "Ты"
        lines.append(f"{who}{tag}: {m['content']}")
    send_message(chat_id, "\n".join(lines))


def _fmt_conversation(c: dict, active_id: int | None = None) -> str:
    mark = "➤ " if active_id is not None and c["id"] == active_id else "  "
    title = c["title"] or "(без названия)"
    closed = " [закрыт]" if c.get("status") == "closed" else ""
    return f"{mark}#{c['id']} {title}{closed} — {c['last_active_at']}"


def _cmd_dialogs(chat_id: int | str, args: str) -> None:
    include_closed = args.strip().lower() in ("все", "всё", "all")
    items = conversations.list_conversations(chat_id, include_closed=include_closed)
    if not items:
        send_message(chat_id, "Диалогов пока нет — начните писать, и он появится сам.")
        return
    active_id = conversations.get_active_conversation_id(chat_id)
    lines = [_fmt_conversation(c, active_id) for c in items]
    hint = "\n\n/switchdialog <id>, /newdialog, /closedialog <id>"
    send_message(chat_id, "\n".join(lines) + hint)


def _cmd_newdialog(chat_id: int | str, _args: str) -> None:
    conversation_id = conversations.create_conversation(chat_id)
    send_message(chat_id, f"Начал новый диалог #{conversation_id}.")


def _cmd_switchdialog(chat_id: int | str, args: str) -> None:
    if not args.strip().isdigit():
        send_message(chat_id, "Использование: /switchdialog <id> (см. /dialogs)")
        return
    conversation_id = int(args.strip())
    if conversations.switch_conversation(chat_id, conversation_id):
        conv = conversations.get_conversation(conversation_id)
        send_message(chat_id, f"Переключился на диалог #{conversation_id} ({conv['title'] or 'без названия'}).")
    else:
        send_message(chat_id, "Не нашёл такой активный диалог (см. /dialogs).")


def _cmd_closedialog(chat_id: int | str, args: str) -> None:
    conversation_id = (
        int(args.strip()) if args.strip().isdigit()
        else conversations.get_active_conversation_id(chat_id)
    )
    if conversations.close_conversation(chat_id, conversation_id):
        send_message(chat_id, f"Диалог #{conversation_id} закрыт.")
    else:
        send_message(chat_id, "Не нашёл такой активный диалог для закрытия (см. /dialogs).")


def _cmd_search(chat_id: int | str, args: str) -> None:
    """Прямой поиск, в обход LLM — быстрый способ проверить, что
    активный провайдер вообще отвечает, и получить сырые результаты."""
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


def _cmd_setsearch(chat_id: int | str, args: str) -> None:
    name = args.strip()
    if not name:
        current = search_service.get_active_provider_name()
        available = ", ".join(search_service.available_providers())
        send_message(
            chat_id,
            f"Текущий провайдер поиска: {current}\nДоступны: {available}\n"
            "Использование: /setsearch <имя>",
        )
        return
    try:
        search_service.set_active_provider(name)
    except SearchError as e:
        send_message(chat_id, str(e))
        return
    send_message(chat_id, f"Провайдер поиска переключён на: {name}")


def _cmd_usage(chat_id: int | str, _args: str) -> None:
    totals = usage_today_totals()
    send_message(
        chat_id,
        f"Сегодня: {totals['requests']} запросов к LLM, "
        f"{totals['tokens']} токенов суммарно.",
    )


def _cmd_remind(chat_id: int | str, args: str) -> None:
    if not args.strip():
        send_message(
            chat_id,
            "Использование: /remind <когда> <текст>\n"
            "Примеры:\n"
            "/remind через 10 минут купить молоко\n"
            "/remind завтра в 9:00 позвонить врачу\n"
            "/remind 18:30 сделать зарядку\n"
            "/remind 2026-07-22 09:00 встреча",
        )
        return
    try:
        due_utc, message = parse_when(args)
    except TimeParseError as e:
        send_message(chat_id, str(e))
        return
    message = message.strip()
    if not message:
        send_message(chat_id, "Не хватает текста напоминания после времени.")
        return
    reminder_id = reminders_service.add_reminder(chat_id, message, due_utc)
    due_local = due_utc.astimezone(ZoneInfo(config.USER_TIMEZONE))
    send_message(
        chat_id,
        f"Напоминание #{reminder_id} на {due_local.strftime('%Y-%m-%d %H:%M')}: {message}",
    )


def _cmd_reminders(chat_id: int | str, _args: str) -> None:
    items = reminders_service.list_pending(chat_id)
    if not items:
        send_message(chat_id, "Активных напоминаний нет.")
        return
    tz = ZoneInfo(config.USER_TIMEZONE)
    lines = []
    for r in items:
        due_local = datetime.fromisoformat(r["due_at"]).astimezone(tz)
        lines.append(f"#{r['id']} {due_local.strftime('%Y-%m-%d %H:%M')}: {r['message']}")
    send_message(chat_id, "\n".join(lines))


def _cmd_delremind(chat_id: int | str, args: str) -> None:
    if not args.strip().isdigit():
        send_message(chat_id, "Использование: /delremind <id>")
        return
    ok = reminders_service.delete_reminder(chat_id, int(args.strip()))
    send_message(
        chat_id, "Удалено." if ok else "Напоминание с таким id не найдено (или уже сработало)."
    )


def _cmd_status(chat_id: int | str, _args: str) -> None:
    send_message(chat_id, monitoring_reporter.build_report())


def _cmd_models(chat_id: int | str, args: str) -> None:
    show_all = args.strip().lower() in ("все", "всё", "all")
    try:
        items = llm_models.list_models() if show_all else llm_models.list_free_models()
    except ModelsError as e:
        send_message(chat_id, str(e))
        return
    if not items:
        send_message(
            chat_id,
            "Список пуст." if show_all else
            "Бесплатных моделей не нашёл (или провайдер не публикует цены — "
            "попробуйте /models все).",
        )
        return
    lines = []
    for m in items[:50]:  # не заваливать чат, если моделей сотни
        mark = "🆓" if m["free"] else ("💰" if m["free"] is False else "❔")
        lines.append(f"{mark} {m['id']}")
    header = "Все модели" if show_all else "Бесплатные модели"
    more = f" (показаны первые {len(lines)} из {len(items)})" if len(items) > len(lines) else ""
    send_message(
        chat_id,
        f"{header}{more}:\n" + "\n".join(lines) + "\n\nВыбрать: /setmodel <id>",
    )


def _cmd_setmodel(chat_id: int | str, args: str) -> None:
    model_id = args.strip()
    if not model_id:
        send_message(
            chat_id,
            f"Текущая модель: {get_active_model()}\n"
            "Использование: /setmodel <id>\nСписок доступных: /models",
        )
        return
    try:
        known_ids = {m["id"] for m in llm_models.list_models()}
        if model_id not in known_ids:
            send_message(
                chat_id,
                f"⚠️ Не нашёл «{model_id}» в списке моделей провайдера — "
                "всё равно переключаю, но проверьте /models на опечатки.",
            )
    except ModelsError:
        pass  # не смогли свериться со списком — не блокируем переключение
    set_active_model(model_id)
    send_message(chat_id, f"Модель переключена на: {model_id}")


def _cmd_pushcode(chat_id: int | str, args: str) -> None:
    if "\n" not in args:
        send_message(
            chat_id,
            "Использование (первая строка — параметры, дальше — код):\n"
            "/pushcode owner/repo имя-ветки путь/к/файлу.py\n"
            "<содержимое файла со следующей строки>",
        )
        return
    header, _, content = args.partition("\n")
    parts = header.split()
    if len(parts) != 3:
        send_message(chat_id, "Первая строка должна быть: owner/repo имя-ветки путь/к/файлу")
        return
    repo, branch, path = parts
    if not content.strip():
        send_message(chat_id, "Тело файла пустое — нечего коммитить.")
        return
    try:
        result = github_service.push_file_to_branch(
            repo, branch, path, content, message=f"Add/update {path} via Telegram bot"
        )
    except GitHubError as e:
        send_message(chat_id, f"Не получилось: {e}")
        return
    branch_note = "новая ветка" if result["created_branch"] else "ветка уже существовала"
    send_message(
        chat_id,
        f"Готово ({branch_note}).\nФайл: {result['file_html_url']}\n"
        f"Ветка: {result['branch_url']}",
    )


_EDITCODE_USAGE = (
    "Использование:\n"
    "/editcode owner/repo имя-ветки\n"
    "путь/к/файлу1.py\n"
    "путь/к/файлу2.py\n"
    "---\n"
    "Инструкция, что изменить (можно в несколько строк)"
)


def _parse_editcode(args: str) -> tuple[str, str, list[str], str]:
    lines = args.split("\n")
    header = lines[0].split()
    if len(header) != 2:
        raise ValueError("Первая строка должна быть: owner/repo имя-ветки")
    repo, branch = header

    paths = []
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        if lines[i].strip():
            paths.append(lines[i].strip())
        i += 1
    if i >= len(lines):
        raise ValueError("Не нашёл разделитель --- перед инструкцией")

    instruction = "\n".join(lines[i + 1:]).strip()
    if not paths:
        raise ValueError("Укажите хотя бы один путь к файлу")
    if not instruction:
        raise ValueError("Не хватает инструкции после ---")
    return repo, branch, paths, instruction


def _cmd_editcode(chat_id: int | str, args: str) -> None:
    if not args.strip():
        send_message(chat_id, _EDITCODE_USAGE)
        return
    try:
        repo, branch, paths, instruction = _parse_editcode(args)
    except ValueError as e:
        send_message(chat_id, f"{e}\n\n{_EDITCODE_USAGE}")
        return

    send_message(chat_id, f"Читаю {len(paths)} файл(ов) и прошу модель внести правки...")
    try:
        result = github_editor.edit_files(repo, branch, paths, instruction)
    except EditError as e:
        send_message(chat_id, f"Не получилось: {e}")
        return

    branch_note = "новая ветка" if result["created_branch"] else "ветка уже существовала"
    files_list = "\n".join(f"- {p}" for p in result["files"])
    send_message(
        chat_id,
        f"Готово ({branch_note}). Изменённые файлы:\n{files_list}\n\n"
        f"Коммит: {result['commit_url']}\nВетка: {result['branch_url']}",
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
    "/dialogs": _cmd_dialogs,
    "/newdialog": _cmd_newdialog,
    "/switchdialog": _cmd_switchdialog,
    "/closedialog": _cmd_closedialog,
    "/search": _cmd_search,
    "/setsearch": _cmd_setsearch,
    "/usage": _cmd_usage,
    "/remind": _cmd_remind,
    "/reminders": _cmd_reminders,
    "/delremind": _cmd_delremind,
    "/status": _cmd_status,
    "/models": _cmd_models,
    "/setmodel": _cmd_setmodel,
    "/pushcode": _cmd_pushcode,
    "/editcode": _cmd_editcode,
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
