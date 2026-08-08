"""
Разбор входящего update от Telegram и диспетчеризация команд.

Мультипользовательский слой: доступ к боту теперь не ограничен одним
OWNER_CHAT_ID. Роли — user / vip / creator (см. modules/users/service.py):
- создатель (config.OWNER_CHAT_ID) имеет полные права всегда;
- новый chat_id при первом обращении заводит заявку ('pending') и
  создатель получает уведомление с /approve <chat_id> user|vip и
  /deny <chat_id>;
- /status, /selftest, /selftest_all, /users, /approve, /deny, /block
  доступны ТОЛЬКО создателю (см. _OWNER_ONLY_COMMANDS) — трогают
  инфраструктуру сервера и/или управляют доступом других людей;
- все ключи (LLM-провайдеры, поиск, GitHub) и самопамять — личные на
  chat_id, без исключений (см. llm/providers.py, modules/search/
  service.py, modules/github/service.py, modules/memory/self_memory.py).

Шаг 4 добавил напоминания (modules.reminders) и мониторинг сервера
(modules.monitoring, /status) — сама доставка напоминаний и
периодический отчёт идут через scheduler.py по тику Railway Cron Job,
не отсюда.
"""
from typing import Callable
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import threading

from config import config
from telegram.api import (
    send_message,
    send_long_message,
    send_chat_action,
    split_text_into_chunks,
    MAX_MESSAGE_LENGTH,
)
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
from modules.diagnostics import service as diagnostics
from modules.users import service as users_service
from storage.db import usage_today_totals
from llm import orchestrator
from llm import models as llm_models
from llm import model_filter
from llm import providers as llm_providers
from llm.providers import ProviderError
from llm.client import get_active_model, set_active_model
from llm.models import ModelsError

log = get_logger(__name__)

CommandHandler = Callable[[int | str, str], None]


def _cmd_start(chat_id: int | str, _args: str) -> None:
    send_message(
        chat_id,
        "Привет! Я на связи. Список команд — /help.\n\n"
        "На любой другой текст отвечаю через LLM — при необходимости "
        "модель сама решает, когда нужно поискать в интернете.\n\n"
        "⚠️ У каждого пользователя свои ключи: перед началом работы "
        "настройте свой LLM-провайдер через /addprovider (см. /help).",
    )


def _cmd_help(chat_id: int | str, _args: str) -> None:
    is_owner = users_service.is_owner(chat_id)
    owner_block = (
        "\n👑 Создатель\n"
        "/users — список пользователей и заявок\n"
        "/approve <chat_id> user|vip — одобрить заявку\n"
        "/deny <chat_id> — отклонить заявку\n"
        "/block <chat_id> — заблокировать пользователя\n"
        "/cronsecret — показать CRON_SECRET, /cronreset — перегенерировать\n\n"
        if is_owner else ""
    )
    status_block = (
        "📊 Сервер\n"
        "/status — память/нагрузка/диск\n"
        "/selftest — живая самопроверка (быстрая, 5 базовых проверок)\n"
        "/selftest_all — полная самопроверка (заметки, диалоги, "
        "cron/напоминания, модели, GitHub round-trip)\n\n"
        if is_owner else ""
    )
    send_message(
        chat_id,
        "📝 Заметки\n"
        "/note <текст> — сохранить\n"
        "/notes [полностью] — показать (по умолчанию до "
        f"{MAX_MESSAGE_LENGTH} симв., полностью — все, в нескольких "
        "сообщениях)\n"
        "/delnote <id> — удалить\n\n"
        "🧠 Память и диалоги (личные, только ваши)\n"
        "/remember <ключ>=<значение> — запомнить факт о себе/тебе надолго\n"
        "/memory — что помню\n"
        "/forget <ключ> — забыть факт\n"
        "/history [полностью] — история текущего диалога (по умолчанию "
        "сжато через LLM, если не помещается в одно сообщение)\n"
        "/dialogs [все] — список диалогов\n"
        "/newdialog — начать новый\n"
        "/switchdialog <id> — переключиться\n"
        "/closedialog [id] — закрыть (по умолчанию текущий)\n\n"
        "🔍 Поиск\n"
        "/search <запрос> — прямой поиск (без LLM)\n"
        "/setsearch [keenable|searxng] — провайдер поиска\n"
        "/setsearchkey <keenable|searxng> <значение> — ваш личный "
        "ключ (keenable) или адрес инстанса (searxng)\n\n"
        "🤖 Модель (ключи — только свои, никаких общих/чужих)\n"
        "/models — бесплатные модели (умный анализ, с кэшем)\n"
        "/models_free — то же самое явной командой\n"
        "/models_all — вообще все модели с ценами\n"
        "/setmodel <id> — переключить модель\n"
        "/usage — ваш расход токенов за сегодня\n"
        "/addprovider имя url ключ [модель] — добавить свой ключ агрегатора\n"
        "/providers — список ваших профилей, /setprovider <имя> — переключить\n"
        "/delprovider <имя> — удалить профиль\n\n"
        "⏰ Напоминания\n"
        "/remind <когда> <текст> — например «через 10 минут ...»\n"
        "/reminders — активные\n"
        "/delremind <id> — удалить\n\n"
        f"{status_block}"
        "🐙 GitHub\n"
        "/setgithub <токен> [базовая_ветка] — ваш личный GitHub-токен\n"
        "/setgithubtest владелец/репо — тестовый репозиторий для последней "
        "проверки /selftest_all\n"
        "/pushcode owner/repo ветка путь/файл + код на след. строках — "
        "закоммитить готовый код\n"
        "/editcode owner/repo ветка + пути файлов + --- + инструкция — "
        "модель сама перепишет файл(ы) и запушит одним коммитом\n\n"
        f"{owner_block}"
        "На любой другой текст отвечаю через LLM.",
    )


def _cmd_note(chat_id: int | str, args: str) -> None:
    if not args.strip():
        send_message(chat_id, "Использование: /note текст заметки")
        return
    note_id = notes.add_note(chat_id, args.strip())
    send_message(chat_id, f"Заметка #{note_id} сохранена.")


def _cmd_notes(chat_id: int | str, args: str) -> None:
    items = notes.list_notes(chat_id)
    if not items:
        send_message(chat_id, "Заметок пока нет.")
        return
    lines = [f"#{n['id']} ({n['created_at']}): {n['content']}" for n in items]
    full_text = "\n".join(lines)

    want_full = args.strip().lower() in ("полностью", "все", "всё", "full")
    if want_full or len(full_text) <= MAX_MESSAGE_LENGTH:
        send_long_message(chat_id, full_text)
        return

    # По умолчанию — без обращения к LLM (это просто список данных,
    # не текст для пересказа): обрезаем по границе абзаца/предложения
    # и явно говорим, что показано не всё.
    truncated = split_text_into_chunks(full_text, MAX_MESSAGE_LENGTH)[0]
    send_message(
        chat_id,
        f"{truncated}\n\n… показаны не все заметки — /notes полностью для всех "
        f"({len(items)} шт.).",
    )


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
    self_memory.remember(chat_id, key, value)
    send_message(chat_id, f"Запомнил: {key.strip()} = {value.strip()}")


def _cmd_memory(chat_id: int | str, _args: str) -> None:
    facts = self_memory.recall_all(chat_id)
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
    ok = self_memory.forget(chat_id, key)
    send_message(chat_id, "Забыл." if ok else "Такого факта не помню.")


def _cmd_history(chat_id: int | str, args: str) -> None:
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
    full_text = "\n".join(lines)

    want_full = args.strip().lower() in ("полностью", "все", "всё", "full")
    if want_full:
        send_long_message(chat_id, full_text)
        return

    if len(full_text) <= MAX_MESSAGE_LENGTH:
        send_message(chat_id, full_text)
        return

    # По умолчанию — сжимаем через LLM (это связный текст переписки,
    # тут, в отличие от /notes, есть смысл пересказывать, а не резать
    # вслепую) и явно говорим, что это сокращённая версия.
    compressed = orchestrator.compress_text(chat_id, full_text, MAX_MESSAGE_LENGTH - 100)
    send_message(
        chat_id,
        f"{compressed}\n\n… история сокращена — /history полностью для оригинала.",
    )


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
        results = search_service.search(chat_id, query)
    except SearchError as e:
        send_message(chat_id, f"Поиск не удался: {e}")
        return
    send_message(chat_id, search_service.format_for_llm(query, results))


def _cmd_setsearch(chat_id: int | str, args: str) -> None:
    name = args.strip()
    if not name:
        current = search_service.get_active_provider_name(chat_id)
        available = ", ".join(search_service.available_providers())
        send_message(
            chat_id,
            f"Текущий провайдер поиска: {current}\nДоступны: {available}\n"
            "Использование: /setsearch <имя>",
        )
        return
    try:
        search_service.set_active_provider(chat_id, name)
    except SearchError as e:
        send_message(chat_id, str(e))
        return
    send_message(chat_id, f"Провайдер поиска переключён на: {name}")


def _cmd_setsearchkey(chat_id: int | str, args: str) -> None:
    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        send_message(
            chat_id,
            "Использование: /setsearchkey <провайдер> <значение>\n"
            "keenable — ваш личный API-ключ (получить на "
            "https://keenable.ai/console)\n"
            "searxng — адрес вашего инстанса, без /search на конце\n\n"
            "⚠️ Значение будет храниться в базе бота, и это сообщение "
            "останется в истории чата с Telegram — при желании удалите "
            "его там вручную после.",
        )
        return
    provider, value = parts
    try:
        search_service.set_search_key(chat_id, provider, value)
    except SearchError as e:
        send_message(chat_id, str(e))
        return
    send_message(chat_id, f"Ключ/адрес для «{provider.strip().lower()}» сохранён.")


def _cmd_usage(chat_id: int | str, _args: str) -> None:
    totals = usage_today_totals(chat_id)
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


def _cmd_cronsecret(chat_id: int | str, _args: str) -> None:
    source = "переменная окружения CRON_SECRET" if os.environ.get("CRON_SECRET") else "сгенерирован ботом автоматически"
    send_message(
        chat_id,
        f"Текущий CRON_SECRET ({source}):\n"
        f"`{config.CRON_SECRET}`\n\n"
        "Впишите его в переменные окружения отдельного Cron Job сервиса "
        "(заголовок X-Cron-Secret при вызове /internal/cron). Если "
        "перегенерировать нужно — удалите значение из БД: /cronreset.",
    )


def _cmd_cronreset(chat_id: int | str, _args: str) -> None:
    from storage.db import set_setting
    import secrets as _secrets
    new_secret = _secrets.token_urlsafe(32)
    set_setting("cron_secret", new_secret)
    config.CRON_SECRET = new_secret
    send_message(
        chat_id,
        f"CRON_SECRET перегенерирован:\n`{new_secret}`\n\n"
        "Не забудьте обновить его в Cron Job сервисе — старое значение "
        "перестало работать немедленно.",
    )


def _cmd_selftest(chat_id: int | str, _args: str) -> None:
    send_message(
        chat_id,
        "Запускаю самопроверку — реальные вызовы к LLM и поиску, "
        "может занять несколько секунд...",
    )
    send_message(chat_id, diagnostics.run_selftest())


def _cmd_selftest_all(chat_id: int | str, _args: str) -> None:
    send_message(
        chat_id,
        "Запускаю полную самопроверку — пройдусь почти по всем функциям "
        "реальными вызовами, это может занять до минуты...",
    )
    send_long_message(chat_id, diagnostics.run_selftest_all())


def _fmt_model_list(items: list[dict], header: str) -> str:
    lines = [f"🆓 {m['id']}" for m in items[:50]]
    more = f" (показаны первые {len(lines)} из {len(items)})" if len(items) > len(lines) else ""
    return f"{header}{more}:\n" + "\n".join(lines) + "\n\nВыбрать: /setmodel <id>"


def _cmd_models_free(chat_id: int | str, args: str) -> None:
    """Бесплатные модели вашего активного провайдера — "умный" анализ
    через LLM (llm/model_filter.py), с кэшем на
    config.FREE_MODELS_CACHE_HOURS часов. Работает независимо от того,
    как именно провайдер оформляет цену в ответе /models."""
    force_refresh = args.strip().lower() in ("обновить", "refresh")
    try:
        items = model_filter.classify_free_models(chat_id, force_refresh=force_refresh)
    except (ModelsError, ProviderError) as e:
        send_message(chat_id, str(e))
        return
    if not items:
        send_message(chat_id, "Бесплатных моделей не нашёл (или анализ не смог определить).")
        return
    send_message(chat_id, _fmt_model_list(items, "Бесплатные модели"))


def _cmd_models_all(chat_id: int | str, _args: str) -> None:
    try:
        items = llm_models.list_models(chat_id)
    except (ModelsError, ProviderError) as e:
        send_message(chat_id, str(e))
        return
    if not items:
        send_message(chat_id, "Список пуст.")
        return
    lines = []
    for m in items[:50]:
        mark = "🆓" if m["free"] else ("💰" if m["free"] is False else "❔")
        lines.append(f"{mark} {m['id']}")
    more = f" (показаны первые {len(lines)} из {len(items)})" if len(items) > len(lines) else ""
    send_message(chat_id, f"Все модели{more}:\n" + "\n".join(lines) + "\n\nВыбрать: /setmodel <id>")


def _cmd_setmodel(chat_id: int | str, args: str) -> None:
    model_id = args.strip()
    if not model_id:
        send_message(
            chat_id,
            f"Текущая модель: {get_active_model(chat_id)}\n"
            "Использование: /setmodel <id>\nСписок доступных: /models",
        )
        return
    try:
        known_ids = {m["id"] for m in llm_models.list_models(chat_id)}
        if model_id not in known_ids:
            send_message(
                chat_id,
                f"⚠️ Не нашёл «{model_id}» в списке моделей провайдера — "
                "всё равно переключаю, но проверьте /models на опечатки.",
            )
    except (ModelsError, ProviderError):
        pass  # не смогли свериться со списком — не блокируем переключение
    try:
        set_active_model(chat_id, model_id)
    except ProviderError as e:
        send_message(chat_id, str(e))
        return
    send_message(chat_id, f"Модель переключена на: {model_id}")


def _cmd_addprovider(chat_id: int | str, args: str) -> None:
    parts = args.split(maxsplit=3)
    if len(parts) < 3:
        send_message(
            chat_id,
            "Использование: /addprovider имя base_url api_key [модель_по_умолчанию]\n"
            "Пример: /addprovider clavis https://api.clavis.to/v1 sk-xxxxx\n\n"
            "⚠️ Ключ будет храниться в базе бота (тот же уровень защиты, "
            "что и .env), и это сообщение с ключом останется в истории "
            "чата с Telegram — при желании удалите его там вручную после.\n\n"
            "Это ВАШ личный ключ — используется только в ваших запросах, "
            "другие пользователи бота его не видят и им не пользуются.",
        )
        return
    name, base_url, api_key = parts[0], parts[1], parts[2]
    default_model = parts[3] if len(parts) > 3 else ""
    try:
        llm_providers.add_profile(chat_id, name, base_url, api_key, default_model)
    except ProviderError as e:
        send_message(chat_id, str(e))
        return
    send_message(
        chat_id,
        f"Профиль «{name}» добавлен. Переключиться: /setprovider {name}",
    )


def _cmd_providers(chat_id: int | str, _args: str) -> None:
    active = llm_providers.get_active_profile_name(chat_id)
    names = llm_providers.list_profile_names(chat_id)
    if not names:
        send_message(
            chat_id,
            "У вас пока нет ни одного профиля — добавьте: "
            "/addprovider имя url ключ",
        )
        return
    lines = [f"{'➤ ' if n == active else '  '}{n}" for n in names]
    send_message(
        chat_id,
        "Ваши профили LLM-провайдеров:\n" + "\n".join(lines) +
        "\n\n/setprovider <имя>, /addprovider, /delprovider <имя>",
    )


def _cmd_setprovider(chat_id: int | str, args: str) -> None:
    name = args.strip()
    if not name:
        send_message(chat_id, f"Текущий профиль: {llm_providers.get_active_profile_name(chat_id) or '(не задан)'}\n"
                     "Использование: /setprovider <имя> (см. /providers)")
        return
    try:
        llm_providers.set_active_profile(chat_id, name)
    except ProviderError as e:
        send_message(chat_id, str(e))
        return
    send_message(chat_id, f"Профиль переключён на: {name} (модель: {get_active_model(chat_id)})")


def _cmd_delprovider(chat_id: int | str, args: str) -> None:
    name = args.strip()
    if not name:
        send_message(chat_id, "Использование: /delprovider <имя>")
        return
    if llm_providers.remove_profile(chat_id, name):
        send_message(chat_id, f"Профиль «{name}» удалён.")
    else:
        send_message(chat_id, f"Профиль «{name}» не найден (нельзя удалить «default»).")


def _cmd_setgithub(chat_id: int | str, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts or not parts[0].strip():
        send_message(
            chat_id,
            "Использование: /setgithub <токен> [базовая_ветка]\n"
            "Нужен fine-grained персональный токен GitHub с правами "
            "Contents: Read and write на нужный репозиторий "
            "(https://github.com/settings/personal-access-tokens).\n\n"
            "⚠️ Токен будет храниться в базе бота, и это сообщение "
            "останется в истории чата с Telegram — при желании удалите "
            "его там вручную после. Это ВАШ личный токен, другие "
            "пользователи бота им не пользуются.",
        )
        return
    token = parts[0].strip()
    base_branch = parts[1].strip() if len(parts) > 1 else ""
    github_service.set_credentials(chat_id, token, base_branch)
    suffix = f" Базовая ветка: {base_branch}" if base_branch else ""
    send_message(chat_id, f"GitHub-токен сохранён.{suffix}")


def _cmd_setgithubtest(chat_id: int | str, args: str) -> None:
    repo = args.strip()
    if not repo:
        current = github_service.get_test_repo_for(chat_id)
        send_message(
            chat_id,
            "Использование: /setgithubtest владелец/репозиторий\n"
            "Отдельный репозиторий для /selftest_all (последняя "
            "проверка — реальные ветка+коммит+удаление, чтобы не "
            "трогать рабочие репозитории). Заведите пустой репозиторий "
            "специально под это и укажите его здесь, например:\n"
            "/setgithubtest ваш-логин/selftest-repo\n\n"
            f"Сейчас настроено: {current or '(не задано)'}",
        )
        return
    if "/" not in repo:
        send_message(chat_id, "Формат должен быть владелец/репозиторий, например octocat/hello-world.")
        return
    github_service.set_test_repo(chat_id, repo)
    send_message(chat_id, f"Тестовый репозиторий сохранён: {repo}")


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
            chat_id, repo, branch, path, content, message=f"Add/update {path} via Telegram bot"
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
        result = github_editor.edit_files(chat_id, repo, branch, paths, instruction)
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


def _fmt_user_row(u: dict) -> str:
    approved = f", одобрил {u['approved_by']} в {u['approved_at']}" if u.get("approved_by") else ""
    return f"{u['chat_id']} — {u['role']} (заявка: {u['requested_at']}{approved})"


def _cmd_users(chat_id: int | str, _args: str) -> None:
    items = users_service.list_users()
    lines = [f"➤ {config.OWNER_CHAT_ID} — creator (создатель)"]
    if items:
        lines.extend(_fmt_user_row(u) for u in items)
    else:
        lines.append("(заявок и пользователей пока нет)")
    send_message(
        chat_id,
        "👥 Пользователи:\n" + "\n".join(lines) +
        "\n\n/approve <chat_id> user|vip, /deny <chat_id>, /block <chat_id>",
    )


def _cmd_approve(chat_id: int | str, args: str) -> None:
    parts = args.split()
    if len(parts) != 2:
        send_message(chat_id, "Использование: /approve <chat_id> user|vip")
        return
    target_chat_id, role = parts
    if users_service.is_owner(target_chat_id):
        send_message(chat_id, "Это и так создатель бота — одобрять не нужно.")
        return
    if not users_service.approve(target_chat_id, role, chat_id):
        send_message(chat_id, "Роль должна быть user или vip.")
        return
    send_message(chat_id, f"Одобрено: {target_chat_id} -> {role.strip().lower()}")
    try:
        send_message(
            target_chat_id,
            f"✅ Ваша заявка на доступ одобрена (роль: {role.strip().lower()}). "
            "Можете пользоваться ботом — начните с /addprovider, чтобы "
            "добавить свой LLM-ключ, затем /help.",
        )
    except Exception:
        log.exception("Не удалось уведомить %s об одобрении заявки", target_chat_id)


def _cmd_deny(chat_id: int | str, args: str) -> None:
    target_chat_id = args.strip()
    if not target_chat_id:
        send_message(chat_id, "Использование: /deny <chat_id>")
        return
    if users_service.is_owner(target_chat_id):
        send_message(chat_id, "Нельзя отклонить создателя бота.")
        return
    if users_service.deny(target_chat_id):
        send_message(chat_id, f"Отклонено: {target_chat_id}")
    else:
        send_message(chat_id, "Такой chat_id не найден (нет заявки).")


def _cmd_block(chat_id: int | str, args: str) -> None:
    target_chat_id = args.strip()
    if not target_chat_id:
        send_message(chat_id, "Использование: /block <chat_id>")
        return
    if users_service.block(target_chat_id):
        send_message(chat_id, f"Заблокировано: {target_chat_id}")
    else:
        send_message(chat_id, "Создателя бота заблокировать нельзя.")


# Реестр команд вида "/command аргументы". Пополняется по мере
# добавления модулей — каждый новый модуль просто регистрирует
# сюда свои обработчики, не трогая остальной код.
COMMANDS: dict[str, CommandHandler] = {
    "/start": _cmd_start,
    "/help": _cmd_help,
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
    "/setsearchkey": _cmd_setsearchkey,
    "/usage": _cmd_usage,
    "/remind": _cmd_remind,
    "/reminders": _cmd_reminders,
    "/delremind": _cmd_delremind,
    "/status": _cmd_status,
    "/cronsecret": _cmd_cronsecret,
    "/cronreset": _cmd_cronreset,
    "/selftest": _cmd_selftest,
    "/selftest_all": _cmd_selftest_all,
    "/models": _cmd_models_free,
    "/models_free": _cmd_models_free,
    "/models_all": _cmd_models_all,
    "/setmodel": _cmd_setmodel,
    "/addprovider": _cmd_addprovider,
    "/providers": _cmd_providers,
    "/setprovider": _cmd_setprovider,
    "/delprovider": _cmd_delprovider,
    "/setgithub": _cmd_setgithub,
    "/setgithubtest": _cmd_setgithubtest,
    "/pushcode": _cmd_pushcode,
    "/editcode": _cmd_editcode,
    "/users": _cmd_users,
    "/approve": _cmd_approve,
    "/deny": _cmd_deny,
    "/block": _cmd_block,
}

# Команды, трогающие инфраструктуру сервера (тратят реальные деньги
# на вызовы) или управляющие доступом других людей — доступны ТОЛЬКО
# создателю, даже одобренным user/vip.
_OWNER_ONLY_COMMANDS = {
    "/status", "/selftest", "/selftest_all",
    "/users", "/approve", "/deny", "/block",
    "/cronsecret", "/cronreset",
}


def _with_typing_indicator(chat_id: int | str, fn, *args, **kwargs):
    """Показывает "печатает..." в Telegram, пока выполняется долгая
    операция (LLM + возможный поиск может занять несколько секунд).
    Telegram сам гасит индикатор через ~5с, поэтому повторяем в
    фоновом потоке, пока основная функция не вернёт результат."""
    stop_event = threading.Event()

    def _keep_typing():
        while not stop_event.is_set():
            send_chat_action(chat_id, "typing")
            stop_event.wait(4)

    thread = threading.Thread(target=_keep_typing, daemon=True)
    thread.start()
    try:
        return fn(*args, **kwargs)
    finally:
        stop_event.set()
        thread.join(timeout=1)


def _default_handler(chat_id: int | str, text: str) -> None:
    """Обычный текст — реальный диалог с LLM. orchestrator сам
    записывает историю, при необходимости запускает поиск, парсит
    теги памяти и запускает компакцию, когда пора."""
    reply = _with_typing_indicator(chat_id, orchestrator.get_reply, chat_id, text)
    send_long_message(chat_id, reply)


def _notify_owner_of_new_request(chat_id: int | str) -> None:
    try:
        send_message(
            config.OWNER_CHAT_ID,
            f"👤 Новая заявка на доступ к боту: chat_id={chat_id}\n"
            f"/approve {chat_id} user — одобрить как user\n"
            f"/approve {chat_id} vip — одобрить как vip\n"
            f"/deny {chat_id} — отклонить",
        )
    except Exception:
        log.exception("Не удалось уведомить создателя о новой заявке chat_id=%s", chat_id)


def _handle_access(chat_id: int | str) -> bool:
    """Проверяет роль chat_id и обрабатывает всё, что НЕ является
    полноценным допуском к боту (заводит заявку, уведомляет
    создателя, отвечает по статусу заявки). Возвращает True, если
    можно продолжать обычную обработку команды/сообщения."""
    role = users_service.get_role(chat_id)

    if role in users_service.AUTHORIZED_ROLES:
        return True

    if role == users_service.ROLE_BLOCKED:
        log.warning("Отклонено сообщение от заблокированного chat_id=%s", chat_id)
        send_message(chat_id, "🚫 Доступ к боту заблокирован.")
        return False

    if role == users_service.ROLE_DENIED:
        send_message(
            chat_id,
            "Заявка на доступ была отклонена создателем бота. Если "
            "считаете, что это ошибка — свяжитесь с ним напрямую.",
        )
        return False

    if role == users_service.ROLE_PENDING:
        send_message(chat_id, "⏳ Заявка на доступ уже отправлена — ждите решения создателя бота.")
        return False

    # role is None — впервые видим этот chat_id.
    users_service.request_access(chat_id)
    send_message(
        chat_id,
        "📨 Заявка на доступ к боту отправлена создателю. Ждите решения — "
        "как только вас одобрят, придёт уведомление.",
    )
    _notify_owner_of_new_request(chat_id)
    return False


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

    if not text:
        return

    if not _handle_access(chat_id):
        return

    command, _, args = text.partition(" ")

    if command in _OWNER_ONLY_COMMANDS and not users_service.is_owner(chat_id):
        send_message(chat_id, "Эта команда доступна только создателю бота.")
        return

    handler = COMMANDS.get(command, _default_handler)
    try:
        handler(chat_id, text if handler is _default_handler else args)
    except Exception:
        log.exception("Ошибка при обработке сообщения")
        send_message(chat_id, "Что-то пошло не так при обработке запроса.")
