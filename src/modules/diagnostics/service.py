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


def run_selftest() -> str:
    checks = [
        ("База данных", _check_db),
        ("LLM", _check_llm),
        ("Поиск", _check_search),
        ("GitHub", _check_github),
        ("Мониторинг сервера", _check_monitoring),
    ]
    lines = [_run_check(name, fn) for name, fn in checks]
    return "🩺 Самопроверка (реальные вызовы, не моки):\n\n" + "\n".join(lines)
