"""
Живая самопроверка — не путать с tests/test_all.py. Там всё нарочно
подменено моками (чтобы тесты работали без ключей и не трогали
реальные сервисы). Здесь — наоборот: минимальные, но НАСТОЯЩИЕ вызовы
к каждому настроенному сервису прямо на текущем сервере — LLM,
поиск, GitHub, база, мониторинг — чтобы проверить, что всё реально
подключено и работает именно сейчас, с текущими ключами и
провайдерами. Отдельный репозиторий не нужен — это просто ещё один
модуль в том же проекте, вызывается командой /selftest.

Каждая проверка стоит немного реальных денег/лимитов (один вызов LLM,
один поисковый запрос) — специально не автоматизировано по расписанию,
запускается только вручную командой.
"""
import time

from core.logger import get_logger

log = get_logger(__name__)


class _Skip(Exception):
    """Проверка сознательно пропущена (например, GITHUB_TOKEN не задан) —
    это не сбой, поэтому в отчёте помечается отдельным значком, не ❌."""


def _run_check(name: str, fn) -> str:
    start = time.monotonic()
    try:
        detail = fn()
        elapsed = time.monotonic() - start
        suffix = f" — {detail}" if detail else ""
        return f"✅ {name}: OK ({elapsed:.1f}с){suffix}"
    except _Skip as e:
        return f"⏭️ {name}: пропущено ({e})"
    except Exception as e:
        elapsed = time.monotonic() - start
        return f"❌ {name}: {e} ({elapsed:.1f}с)"


def _check_db() -> str | None:
    from storage.db import set_setting, get_setting

    set_setting("_selftest_ping", "ok")
    if get_setting("_selftest_ping") != "ok":
        raise RuntimeError("запись прошла, но чтение вернуло не то, что записали")
    return None


def _check_llm() -> str:
    from llm.client import chat_completion, get_active_model

    reply = chat_completion(
        [{"role": "user", "content": "Ответь одним словом: тест"}], max_tokens=10
    )
    return f"модель {get_active_model()}, ответ: {reply.strip()[:40]!r}"


def _check_search() -> str:
    from modules.search import service as search_service

    provider = search_service.get_active_provider_name()
    results = search_service.search("test", max_results=1)
    return f"провайдер {provider}, результатов: {len(results)}"


def _check_github() -> str:
    from config import config

    if not config.GITHUB_TOKEN:
        raise _Skip("GITHUB_TOKEN не задан")
    from modules.github.service import _request  # тот же клиент, что и у /pushcode

    data = _request("GET", "/rate_limit")
    remaining = data.get("rate", {}).get("remaining", "?")
    return f"токен рабочий, осталось запросов к GitHub API: {remaining}"


def _check_monitoring() -> None:
    from modules.monitoring import reporter

    reporter.build_report()  # само не бросит — если бросит, тест это и покажет
    return None


def _check_cron_health() -> str:
    """Ключевая диагностика для "напоминание не сработало": показывает,
    когда Railway Cron Job последний раз реально достучался до
    /internal/cron (по отметке last_monitoring_report_at, которую
    выставляет только scheduler.run_tick), и сколько просроченных,
    но недоставленных напоминаний висит прямо сейчас. Если тик был
    давно (или ни разу) и/или просроченных > 0 — Cron Job либо не
    настроен, либо не достучивается (неверный URL/секрет/расписание)."""
    from datetime import datetime, timezone
    from storage.db import get_setting
    from modules.reminders import service as reminders_service

    now = datetime.now(timezone.utc)
    last_report = get_setting("last_monitoring_report_at")
    overdue = reminders_service.get_due(now)

    if last_report is None:
        tick_info = "ни разу не отмечался (Cron Job ещё не срабатывал с последнего передеплоя базы)"
    else:
        last_dt = datetime.fromisoformat(last_report)
        age_hours = (now - last_dt).total_seconds() / 3600
        tick_info = f"последний тик {age_hours:.1f}ч назад"

    if overdue:
        raise RuntimeError(
            f"{len(overdue)} просроченных напоминаний НЕ доставлены "
            f"({tick_info}) — похоже, Railway Cron Job не срабатывает. "
            f"Проверьте в Railway: расписание, URL сервиса, "
            f"X-Cron-Secret совпадает с CRON_SECRET бота."
        )
    return f"просроченных недоставленных: 0, {tick_info}"


def _check_notes_roundtrip() -> str:
    """Создаёт/читает/удаляет заметку под служебным chat_id — не
    трогает ваши настоящие заметки (они у другого chat_id)."""
    from modules.notes import service as notes_service

    test_chat_id = "_selftest"
    note_id = notes_service.add_note(test_chat_id, "тестовая заметка от /selftest_all")
    items = notes_service.list_notes(test_chat_id)
    found = any(n["id"] == note_id for n in items)
    notes_service.delete_note(test_chat_id, note_id)
    if not found:
        raise RuntimeError("заметка не нашлась сразу после создания")
    return "создание/чтение/удаление работают"


def _check_dialogs_roundtrip() -> str:
    """То же самое для веток диалогов — под служебным chat_id."""
    from modules.conversations import service as conversations

    test_chat_id = "_selftest"
    conv_id = conversations.create_conversation(test_chat_id, title="selftest")
    switched = conversations.switch_conversation(test_chat_id, conv_id)
    closed = conversations.close_conversation(test_chat_id, conv_id)
    if not switched or not closed:
        raise RuntimeError("создание/переключение/закрытие диалога не сработали как ожидалось")
    return f"создание/переключение/закрытие работают (id={conv_id})"


def _check_models_list() -> str:
    from llm import models as llm_models

    items = llm_models.list_models()
    return f"получено моделей от провайдера: {len(items)}"


def _check_github_roundtrip() -> str:
    """В отличие от _check_github (только чтение /rate_limit), тут
    реальный цикл записи: создать ветку, закоммитить файл, убедиться,
    что всё прошло, и удалить ветку за собой. Требует ОТДЕЛЬНОГО
    тестового репозитория (GITHUB_TEST_REPO) — намеренно не трогает
    репозитории, которые вы не выделили специально под это."""
    import time as _time
    from config import config

    if not config.GITHUB_TOKEN:
        raise _Skip("GITHUB_TOKEN не задан")
    if not config.GITHUB_TEST_REPO:
        raise _Skip(
            "GITHUB_TEST_REPO не задан — укажите тестовый репозиторий "
            "(например ваш-логин/selftest-repo), чтобы включить эту проверку"
        )

    from modules.github import service as gh

    branch = f"selftest-{int(_time.time())}"
    result = gh.push_file_to_branch(
        config.GITHUB_TEST_REPO, branch, "selftest.txt",
        "ping from /selftest_all", "Selftest ping (auto-cleanup)",
    )
    # Убираем за собой — тестовый репозиторий не должен зарастать ветками.
    gh._request("DELETE", f"/repos/{config.GITHUB_TEST_REPO}/git/refs/heads/{branch}")
    return f"branch+commit+delete прошли ({result.get('file_html_url', '')})"


def run_selftest() -> str:
    checks = [
        ("База данных", _check_db),
        ("LLM", _check_llm),
        ("Поиск", _check_search),
        ("GitHub (токен)", _check_github),
        ("Мониторинг сервера", _check_monitoring),
    ]
    lines = [_run_check(name, fn) for name, fn in checks]
    return "🩺 Самопроверка (реальные вызовы, не моки):\n\n" + "\n".join(lines)


def run_selftest_all() -> str:
    """Расширенная версия — проходит по большинству функций бота, не
    только по базовому подключению. Отдельно, а не по умолчанию,
    потому что дороже (больше реальных вызовов) и дольше выполняется."""
    checks = [
        ("База данных", _check_db),
        ("LLM", _check_llm),
        ("Поиск", _check_search),
        ("GitHub (токен)", _check_github),
        ("Мониторинг сервера", _check_monitoring),
        ("Cron / напоминания", _check_cron_health),
        ("Заметки (round-trip)", _check_notes_roundtrip),
        ("Диалоги (round-trip)", _check_dialogs_roundtrip),
        ("Список моделей", _check_models_list),
        ("GitHub (round-trip на тестовом репо)", _check_github_roundtrip),
    ]
    lines = [_run_check(name, fn) for name, fn in checks]
    return "🩺 Полная самопроверка (реальные вызовы, не моки):\n\n" + "\n".join(lines)
