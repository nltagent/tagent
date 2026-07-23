"""
Полный набор тестов проекта — покрывает шаги 1-6. Только stdlib
(unittest + unittest.mock) — без pytest и прочих зависимостей, в духе
всего остального проекта.

Запуск (из корня репозитория):
    cd src && python -m unittest ../tests/test_all.py -v
или
    PYTHONPATH=src python -m unittest discover -s ../tests -v
Проще всего:
    cd tests && python run_tests.sh   (см. соседний скрипт-обёртку)

Все внешние вызовы (Telegram, LLM-провайдер, поиск, GitHub) —
подменяются моками. Реальные ключи/сеть не нужны и не используются.
Каждый тест работает с отдельной временной SQLite-базой — тесты не
видят данные друг друга и не портят вашу настоящую базу.
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ── Настройка окружения ДО импорта чего-либо из src/ ──
# Config читает переменные окружения в момент импорта модуля (см.
# src/config.py) — поэтому это должно случиться раньше любого импорта.
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, os.path.abspath(SRC_DIR))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("OWNER_CHAT_ID", "1")
os.environ.setdefault("LLM_API_KEY", "test-llm-key")
os.environ.setdefault("LLM_BASE_URL", "https://openrouter.ai/api/v1")
os.environ.setdefault("LLM_MODEL", "test/default-model")
os.environ.setdefault("CRON_SECRET", "test-cron-secret")
os.environ.setdefault("USER_TIMEZONE", "Europe/Amsterdam")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("DB_PATH", "/tmp/telegram-agent-tests-placeholder.db")

from config import config  # noqa: E402
import storage.db as db  # noqa: E402


class FakeResponse(io.BytesIO):
    """Имитация ответа urllib.request.urlopen (поддерживает `with`)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url: str, code: int, body: dict | bytes = b"{}") -> urllib.error.HTTPError:
    payload = json.dumps(body).encode() if isinstance(body, dict) else body
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(payload))


class IsolatedDBTestCase(unittest.TestCase):
    """Каждый тест получает свою временную SQLite-базу — состояние не
    утекает между тестами и не трогает настоящий DB_PATH. Заодно
    сохраняем и восстанавливаем config.* — некоторые тесты специально
    подкручивают лимиты/ключи (KEENABLE_API_KEY, HISTORY_TOKEN_BUDGET
    и т.п.), это не должно просачиваться в другие тесты."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tgagent-test-")
        self._config_snapshot = dict(vars(config))
        config.DB_PATH = os.path.join(self.tmpdir, "test.db")
        db._conn = None  # сбрасываем закэшированное соединение на старый файл

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        config.__dict__.clear()
        config.__dict__.update(self._config_snapshot)


# ────────────────────────── Заметки ──────────────────────────

class TestNotes(IsolatedDBTestCase):
    def test_add_list_delete(self):
        from modules.notes import service as notes

        note_id = notes.add_note(1, "купить молоко")
        self.assertEqual(notes.list_notes(1), [
            {"id": note_id, "content": "купить молоко", "created_at": notes.list_notes(1)[0]["created_at"]}
        ])
        self.assertTrue(notes.delete_note(1, note_id))
        self.assertEqual(notes.list_notes(1), [])
        # Повторное удаление того же id — уже нечего удалять
        self.assertFalse(notes.delete_note(1, note_id))

    def test_notes_are_scoped_by_chat(self):
        from modules.notes import service as notes

        notes.add_note(1, "заметка пользователя 1")
        notes.add_note(2, "заметка пользователя 2")
        self.assertEqual(len(notes.list_notes(1)), 1)
        self.assertEqual(len(notes.list_notes(2)), 1)
        # Нельзя удалить чужую заметку по id
        other_id = notes.list_notes(2)[0]["id"]
        self.assertFalse(notes.delete_note(1, other_id))


# ────────────────────────── Память агента ──────────────────────────

class TestSelfMemory(IsolatedDBTestCase):
    def test_remember_forget_recall(self):
        from modules.memory import self_memory

        self_memory.remember("name", "Джарвис")
        self.assertEqual(self_memory.recall_all(), {"name": "Джарвис"})
        self.assertIn("Джарвис", self_memory.as_prompt_block())

        self.assertTrue(self_memory.forget("name"))
        self.assertEqual(self_memory.recall_all(), {})
        self.assertFalse(self_memory.forget("name"))  # уже нечего забывать
        self.assertEqual(self_memory.as_prompt_block(), "")

    def test_extract_remember_tags(self):
        from modules.memory import self_memory

        text = "Ок! [REMEMBER: name=Джарвис] Дальше текст. [REMEMBER: mood=бодрый]"
        cleaned, facts = self_memory.extract_remember_tags(text)
        self.assertEqual(facts, {"name": "Джарвис", "mood": "бодрый"})
        self.assertNotIn("REMEMBER", cleaned)
        self.assertEqual(self_memory.recall_all(), {"name": "Джарвис", "mood": "бодрый"})


# ────────────────────────── История диалога + компактор ──────────────────────────

class TestHistoryAndCompactor(IsolatedDBTestCase):
    def test_record_and_read_history(self):
        from modules.memory import history
        from modules.conversations import service as conversations

        conv_id = conversations.create_conversation(1)
        history.record_message(1, conv_id, "user", "привет")
        history.record_message(1, conv_id, "assistant", "привет!")
        active = history.get_active_messages(conv_id)
        self.assertEqual([m["role"] for m in active], ["user", "assistant"])
        self.assertEqual(history.active_tokens_total(conv_id), sum(m["tokens_est"] for m in active))

    def test_compactor_archives_old_messages(self):
        from modules.memory import history, compactor
        from modules.conversations import service as conversations

        config.HISTORY_KEEP_LAST = 2
        config.HISTORY_TOKEN_BUDGET = 20
        conv_id = conversations.create_conversation(1)

        for i in range(10):
            history.record_message(1, conv_id, "user", f"сообщение номер {i} " * 3)
            history.record_message(1, conv_id, "assistant", f"ответ номер {i} " * 3)

        def fake_summarize(old_summary, messages_to_archive):
            return f"сжато {len(messages_to_archive)} сообщений"

        changed = compactor.maybe_compact(conv_id, fake_summarize)
        self.assertTrue(changed)
        self.assertLessEqual(len(history.get_active_messages(conv_id)), config.HISTORY_KEEP_LAST + 2)
        self.assertIn("сжато", history.get_summary(conv_id))

    def test_compactor_skips_when_summarize_fails(self):
        from modules.memory import history, compactor
        from modules.conversations import service as conversations

        config.HISTORY_KEEP_LAST = 1
        config.HISTORY_TOKEN_BUDGET = 5
        conv_id = conversations.create_conversation(1)
        history.record_message(1, conv_id, "user", "какое-то длинное сообщение для превышения бюджета")
        history.record_message(1, conv_id, "assistant", "и ещё один длинный ответ для верности")

        before = len(history.get_active_messages(conv_id))
        changed = compactor.maybe_compact(conv_id, lambda old, msgs: None)  # None = неудача
        self.assertFalse(changed)
        self.assertEqual(len(history.get_active_messages(conv_id)), before)  # ничего не потеряно


# ────────────────────────── Напоминания + парсер времени ──────────────────────────

class TestTimeParse(unittest.TestCase):
    def test_relative_and_absolute_formats(self):
        from modules.reminders.timeparse import parse_when, TimeParseError

        for text in [
            "через 10 минут купить молоко",
            "через 2 часа позвонить",
            "через 1 день сделать бэкап",
            "завтра в 9:00 встреча с врачом",
            "18:30 зарядка",
            "2026-08-01 09:00 день рождения",
        ]:
            due_utc, message = parse_when(text)
            self.assertIsInstance(due_utc, datetime)
            self.assertTrue(due_utc.tzinfo is not None)
            self.assertTrue(len(message) > 0)

        with self.assertRaises(TimeParseError):
            parse_when("какая-то ерунда без времени")


class TestReminders(IsolatedDBTestCase):
    def test_add_list_delete_and_due(self):
        from modules.reminders import service as reminders

        now = datetime.now(timezone.utc)
        past_id = reminders.add_reminder(1, "просрочено", now - timedelta(minutes=5))
        future_id = reminders.add_reminder(1, "в будущем", now + timedelta(hours=1))

        pending = reminders.list_pending(1)
        self.assertEqual(len(pending), 2)

        due = reminders.get_due(now)
        self.assertEqual([r["id"] for r in due], [past_id])

        reminders.mark_delivered(past_id)
        self.assertEqual(reminders.get_due(now), [])
        self.assertEqual(len(reminders.list_pending(1)), 1)

        self.assertTrue(reminders.delete_reminder(1, future_id))
        self.assertEqual(reminders.list_pending(1), [])


# ────────────────────────── Мониторинг ──────────────────────────

class TestMonitoring(IsolatedDBTestCase):
    def test_build_report_runs_and_has_expected_sections(self):
        from modules.monitoring import reporter

        report = reporter.build_report()
        self.assertIn("Память", report)
        self.assertIn("Нагрузка", report)
        self.assertIn("Диск", report)


# ────────────────────────── Планировщик (cron-тик) ──────────────────────────

class TestScheduler(IsolatedDBTestCase):
    def test_run_tick_delivers_reminders_and_throttles_report(self):
        import scheduler
        from modules.reminders import service as reminders

        sent = []
        patcher = mock.patch.object(
            scheduler, "send_message",
            lambda chat_id, text, **kw: sent.append((chat_id, text)) or {"ok": True},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        config.MONITORING_REPORT_INTERVAL_HOURS = 1
        now = datetime.now(timezone.utc)
        reminders.add_reminder(1, "просроченное", now - timedelta(minutes=1))

        result1 = scheduler.run_tick()
        self.assertEqual(result1["reminders_delivered"], 1)
        self.assertTrue(result1["monitoring_report_sent"])  # первый раз всегда шлём

        result2 = scheduler.run_tick()
        self.assertEqual(result2["reminders_delivered"], 0)
        self.assertFalse(result2["monitoring_report_sent"])  # рано ещё

        self.assertEqual(len(sent), 2)  # напоминание + один отчёт
        self.assertTrue(any("Напоминание" in t for _, t in sent))
        self.assertTrue(any("Состояние сервера" in t for _, t in sent))


# ────────────────────────── Поиск: диспетчер провайдеров ──────────────────────────

class TestSearchProviders(IsolatedDBTestCase):
    def test_keenable_without_key_raises_clear_error(self):
        from modules.search import service as search_service
        from modules.search.errors import SearchError

        config.KEENABLE_API_KEY = ""
        search_service.set_active_provider("keenable")
        with self.assertRaises(SearchError):
            search_service.search("тест")

    def test_searxng_without_url_raises_clear_error(self):
        from modules.search import service as search_service
        from modules.search.errors import SearchError

        config.SEARXNG_BASE_URL = ""
        search_service.set_active_provider("searxng")
        with self.assertRaises(SearchError):
            search_service.search("тест")

    def test_unknown_provider_rejected(self):
        from modules.search import service as search_service
        from modules.search.errors import SearchError

        with self.assertRaises(SearchError):
            search_service.set_active_provider("bing")

    def test_switch_persists_and_searxng_call_shape(self):
        from modules.search import service as search_service

        config.SEARXNG_BASE_URL = "http://localhost:8080"
        search_service.set_active_provider("searxng")
        self.assertEqual(search_service.get_active_provider_name(), "searxng")

        captured = {}

        def fake_urlopen(req, timeout=15):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            payload = {"results": [{"title": "Погода", "url": "https://x.example", "content": "18°C"}]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            results = search_service.search("погода")

        self.assertEqual(results, [{"title": "Погода", "url": "https://x.example", "snippet": "18°C"}])
        self.assertEqual(captured["method"], "GET")
        self.assertIn("format=json", captured["url"])


# ────────────────────────── LLM: клиент, модели ──────────────────────────

class TestLLMClientAndModels(IsolatedDBTestCase):
    def test_chat_completion_sends_expected_request_and_logs_usage(self):
        from llm.client import chat_completion, get_active_model

        captured = {}

        def fake_urlopen(req, timeout=60):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            payload = {
                "choices": [{"message": {"content": "Привет! Чем помочь?"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            reply = chat_completion([{"role": "user", "content": "привет"}])

        self.assertEqual(reply, "Привет! Чем помочь?")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["body"]["model"], get_active_model())
        self.assertIn("Bearer", captured["auth"])

        totals = db.usage_today_totals()
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(totals["tokens"], 15)

    def test_retries_once_on_429_then_succeeds(self):
        from llm.client import chat_completion

        attempts = {"n": 0}

        def fake_urlopen(req, timeout=60):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise http_error(req.full_url, 429, {"error": "rate limited"})
            payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch("time.sleep", lambda s: None):
            reply = chat_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(reply, "ok")
        self.assertEqual(attempts["n"], 2)

    def test_models_listing_and_free_filter(self):
        import llm.models as llm_models

        def fake_urlopen(req, timeout=15):
            payload = {
                "data": [
                    {"id": "meta-llama/llama-3.3-70b:free", "name": "Llama free",
                     "pricing": {"prompt": "0", "completion": "0"}},
                    {"id": "openai/gpt-4o", "name": "GPT-4o",
                     "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
                    {"id": "some/unknown-pricing", "name": "Unknown"},
                ]
            }
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            all_models = llm_models.list_models()
            free_models = llm_models.list_free_models()

        self.assertEqual(len(all_models), 3)
        self.assertEqual([m["id"] for m in free_models], ["meta-llama/llama-3.3-70b:free"])
        unknown = [m for m in all_models if m["id"] == "some/unknown-pricing"][0]
        self.assertIsNone(unknown["free"])

    def test_set_and_get_active_model_persists(self):
        from llm.client import get_active_model, set_active_model

        default = get_active_model()
        self.assertEqual(default, config.LLM_MODEL)
        set_active_model("some/other-model")
        self.assertEqual(get_active_model(), "some/other-model")


# ────────────────────────── LLM: оркестратор (REMEMBER + SEARCH теги) ──────────────────────────

class TestOrchestrator(IsolatedDBTestCase):
    def test_plain_reply_is_recorded_in_history(self):
        import llm.orchestrator as orchestrator
        from modules.memory import history
        from modules.conversations import service as conversations

        with mock.patch.object(
            orchestrator, "chat_completion", lambda messages, **kw: "Привет! Всё хорошо."
        ):
            reply = orchestrator.get_reply(1, "как дела?")

        self.assertEqual(reply, "Привет! Всё хорошо.")
        conv_id = conversations.get_active_conversation_id(1)
        roles = [m["role"] for m in history.get_active_messages(conv_id)]
        self.assertEqual(roles, ["user", "assistant"])

    def test_remember_tag_is_stripped_and_stored(self):
        import llm.orchestrator as orchestrator
        from modules.memory import self_memory

        with mock.patch.object(
            orchestrator, "chat_completion",
            lambda messages, **kw: "Ок! [REMEMBER: name=Джарвис]",
        ):
            reply = orchestrator.get_reply(1, "тебя зовут Джарвис")

        self.assertNotIn("REMEMBER", reply)
        self.assertEqual(self_memory.recall_all().get("name"), "Джарвис")

    def test_search_tag_triggers_second_call_with_results(self):
        import llm.orchestrator as orchestrator

        calls = {"n": 0}

        def fake_chat_completion(messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return "[SEARCH: погода в Амстердаме]"
            # На втором вызове в истории должны быть результаты поиска
            self.assertTrue(any("результаты поиска" in m["content"].lower() for m in messages))
            return "Сейчас в Амстердаме облачно, 18°C."

        def fake_search(query, max_results=5, **kw):
            return [{"title": "Погода", "url": "https://x.example", "snippet": "18°C, облачно"}]

        with mock.patch.object(orchestrator, "chat_completion", fake_chat_completion), \
             mock.patch.object(orchestrator.search_service, "search", fake_search):
            reply = orchestrator.get_reply(1, "какая погода в Амстердаме?")

        self.assertIn("18", reply)
        self.assertEqual(calls["n"], 2)


# ────────────────────────── GitHub: сервис (Contents API + Git Data API) ──────────────────────────

class TestGitHubService(unittest.TestCase):
    def test_push_file_to_branch_creates_branch_and_commits(self):
        from modules.github import service as gh

        calls = []

        def fake_urlopen(req, timeout=20):
            method, url = req.get_method(), req.full_url
            calls.append((method, url))
            if "/git/ref/heads/new-feature" in url:
                raise http_error(url, 404)
            if "/git/ref/heads/main" in url:
                return FakeResponse(json.dumps({"object": {"sha": "base-sha"}}).encode())
            if "/git/refs" in url and method == "POST":
                return FakeResponse(json.dumps({"ref": "refs/heads/new-feature"}).encode())
            if "/contents/" in url and method == "GET":
                raise http_error(url, 404)
            if "/contents/" in url and method == "PUT":
                return FakeResponse(json.dumps(
                    {"content": {"html_url": "https://github.com/o/r/blob/new-feature/f.py"}}
                ).encode())
            raise AssertionError(f"Неожиданный запрос: {method} {url}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = gh.push_file_to_branch("o/r", "new-feature", "f.py", "print(1)\n", "msg")

        self.assertTrue(result["created_branch"])
        self.assertIn("new-feature", result["branch_url"])
        methods = [m for m, _ in calls]
        self.assertEqual(methods, ["GET", "GET", "POST", "GET", "PUT"])

    def test_push_files_to_branch_atomic_multi_file(self):
        from modules.github import service as gh

        blob_calls = {"n": 0}

        def fake_urlopen(req, timeout=20):
            method, url = req.get_method(), req.full_url
            if "/git/ref/heads/multi" in url:
                return FakeResponse(json.dumps({"object": {"sha": "head-sha"}}).encode())
            if "/git/commits/head-sha" in url:
                return FakeResponse(json.dumps({"tree": {"sha": "base-tree"}}).encode())
            if url.endswith("/git/blobs"):
                blob_calls["n"] += 1
                return FakeResponse(json.dumps({"sha": f"blob-{blob_calls['n']}"}).encode())
            if url.endswith("/git/trees"):
                body = json.loads(req.data.decode())
                self.assertEqual(len(body["tree"]), 2)  # оба файла в одном дереве
                return FakeResponse(json.dumps({"sha": "new-tree"}).encode())
            if url.endswith("/git/commits") and method == "POST":
                return FakeResponse(json.dumps({"sha": "new-commit"}).encode())
            if "/git/refs/heads/multi" in url and method == "PATCH":
                return FakeResponse(json.dumps({}).encode())
            raise AssertionError(f"Неожиданный запрос: {method} {url}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = gh.push_files_to_branch(
                "o/r", "multi", {"a.py": "print('a')", "b.py": "print('b')"}, "msg"
            )

        self.assertEqual(blob_calls["n"], 2)  # ровно два blob'а — по одному на файл
        self.assertIn("new-commit", result["commit_url"])


class TestGitHubEditor(unittest.TestCase):
    def test_edit_files_success(self):
        import modules.github.editor as editor

        state = {"branch_created": False}

        def fake_urlopen(req, timeout=20):
            method, url = req.get_method(), req.full_url
            if "/git/ref/heads/feature-edit" in url:
                if state["branch_created"]:
                    return FakeResponse(json.dumps({"object": {"sha": "main-sha"}}).encode())
                raise http_error(url, 404)
            if "/git/ref/heads/main" in url:
                return FakeResponse(json.dumps({"object": {"sha": "main-sha"}}).encode())
            if "/contents/src/app.py" in url and method == "GET":
                content_b64 = base64.b64encode(b"print('old')\n").decode()
                return FakeResponse(json.dumps({"encoding": "base64", "content": content_b64}).encode())
            if "/git/refs" in url and method == "POST":
                state["branch_created"] = True
                return FakeResponse(json.dumps({"ref": "refs/heads/feature-edit"}).encode())
            if "/git/commits/main-sha" in url:
                return FakeResponse(json.dumps({"tree": {"sha": "tree-sha"}}).encode())
            if url.endswith("/git/blobs"):
                return FakeResponse(json.dumps({"sha": "blob-sha"}).encode())
            if url.endswith("/git/trees"):
                return FakeResponse(json.dumps({"sha": "new-tree"}).encode())
            if url.endswith("/git/commits") and method == "POST":
                return FakeResponse(json.dumps({"sha": "new-commit"}).encode())
            if "/git/refs/heads/feature-edit" in url and method == "PATCH":
                return FakeResponse(json.dumps({}).encode())
            raise AssertionError(f"Неожиданный запрос: {method} {url}")

        edit_fn = mock.patch.object(
            editor, "chat_completion",
            lambda messages, **kw: "===FILE: src/app.py===\nprint('new')\n===END===",
        )
        edit_fn.start()
        self.addCleanup(edit_fn.stop)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = editor.edit_files("o/r", "feature-edit", ["src/app.py"], "почини баг")

        self.assertEqual(result["files"], ["src/app.py"])
        self.assertTrue(result["created_branch"])

    def test_edit_files_raises_when_model_misses_a_file(self):
        import modules.github.editor as editor

        def fake_urlopen(req, timeout=20):
            method, url = req.get_method(), req.full_url
            if "/git/ref/heads/" in url:
                return FakeResponse(json.dumps({"object": {"sha": "sha1"}}).encode())
            if "/contents/" in url:
                raise http_error(url, 404)
            raise AssertionError(f"Неожиданный запрос: {method} {url}")

        edit_fn = mock.patch.object(
            editor, "chat_completion",
            lambda messages, **kw: "===FILE: другой/файл.py===\nсодержимое\n===END===",
        )
        edit_fn.start()
        self.addCleanup(edit_fn.stop)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(editor.EditError):
                editor.edit_files("o/r", "branch", ["src/app.py"], "запрос")


# ────────────────────────── Ветки диалогов (шаг 7) ──────────────────────────

class TestConversations(IsolatedDBTestCase):
    def test_lazy_create_and_active_tracking(self):
        from modules.conversations import service as conversations

        conv_id = conversations.get_active_conversation_id(1)
        self.assertIsInstance(conv_id, int)
        # Повторный вызов возвращает тот же самый активный диалог
        self.assertEqual(conversations.get_active_conversation_id(1), conv_id)

    def test_new_switch_and_list(self):
        from modules.conversations import service as conversations

        first_id = conversations.get_active_conversation_id(1)
        second_id = conversations.create_conversation(1)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(conversations.get_active_conversation_id(1), second_id)

        self.assertTrue(conversations.switch_conversation(1, first_id))
        self.assertEqual(conversations.get_active_conversation_id(1), first_id)

        items = conversations.list_conversations(1)
        self.assertEqual({c["id"] for c in items}, {first_id, second_id})

    def test_switch_rejects_foreign_or_closed(self):
        from modules.conversations import service as conversations

        conv_id = conversations.create_conversation(1)
        # Чужой chat_id не может переключиться на этот диалог
        self.assertFalse(conversations.switch_conversation(2, conv_id))
        # Несуществующий id
        self.assertFalse(conversations.switch_conversation(1, 99999))

    def test_close_active_auto_creates_new(self):
        from modules.conversations import service as conversations

        conv_id = conversations.get_active_conversation_id(1)
        self.assertTrue(conversations.close_conversation(1, conv_id))
        new_active = conversations.get_active_conversation_id(1)
        self.assertNotEqual(new_active, conv_id)

        closed = conversations.get_conversation(conv_id)
        self.assertEqual(closed["status"], "closed")
        # Закрытый диалог не должен попадать в дефолтный (активный) список
        self.assertNotIn(conv_id, [c["id"] for c in conversations.list_conversations(1)])
        self.assertIn(conv_id, [c["id"] for c in conversations.list_conversations(1, include_closed=True)])

    def test_close_non_active_does_not_touch_active(self):
        from modules.conversations import service as conversations

        active_id = conversations.get_active_conversation_id(1)
        other_id = conversations.create_conversation(1)
        conversations.switch_conversation(1, active_id)  # активный снова первый

        self.assertTrue(conversations.close_conversation(1, other_id))
        self.assertEqual(conversations.get_active_conversation_id(1), active_id)

    def test_maybe_set_title_only_when_empty(self):
        from modules.conversations import service as conversations

        conv_id = conversations.create_conversation(1)
        conversations.maybe_set_title(conv_id, "Привет, это первое сообщение диалога")
        conv = conversations.get_conversation(conv_id)
        self.assertTrue(conv["title"].startswith("Привет"))

        conversations.maybe_set_title(conv_id, "Второе сообщение не должно менять заголовок")
        conv_again = conversations.get_conversation(conv_id)
        self.assertEqual(conv["title"], conv_again["title"])

    def test_history_and_summary_are_isolated_per_conversation(self):
        from modules.conversations import service as conversations
        from modules.memory import history

        conv_a = conversations.create_conversation(1)
        conv_b = conversations.create_conversation(1)

        history.record_message(1, conv_a, "user", "сообщение в диалоге A")
        history.record_message(1, conv_b, "user", "сообщение в диалоге B")
        history.set_summary(conv_a, "сводка A")

        self.assertEqual(len(history.get_active_messages(conv_a)), 1)
        self.assertEqual(len(history.get_active_messages(conv_b)), 1)
        self.assertEqual(history.get_active_messages(conv_a)[0]["content"], "сообщение в диалоге A")
        self.assertEqual(history.get_summary(conv_a), "сводка A")
        self.assertEqual(history.get_summary(conv_b), "")  # не перепуталось


class TestMigrationFromOldSchema(unittest.TestCase):
    """Отдельно от IsolatedDBTestCase — тут нам как раз нужно вручную
    создать файл БД в СТАРОЙ схеме (до conversation_id) перед первым
    открытием через storage.db, а не получать пустую новую."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tgagent-migration-")
        self.db_path = os.path.join(self.tmpdir, "old.db")
        self._config_snapshot = dict(vars(config))
        config.DB_PATH = self.db_path
        db._conn = None

        import sqlite3 as sqlite3_module
        conn = sqlite3_module.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tokens_est INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE conversation_meta (
                chat_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                summary_updated_at TEXT
            );
            CREATE TABLE settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, tokens_est, created_at, archived) "
            "VALUES ('42', 'user', 'старое сообщение', 5, '2026-01-01T00:00:00+00:00', 0)"
        )
        conn.execute(
            "INSERT INTO conversation_meta (chat_id, summary, summary_updated_at) "
            "VALUES ('42', 'старая сводка', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        config.__dict__.clear()
        config.__dict__.update(self._config_snapshot)

    def test_old_data_is_preserved_and_becomes_active_conversation(self):
        from modules.conversations import service as conversations
        from modules.memory import history

        db._get_conn()  # триггерит миграцию при первом открытии

        active_id = conversations.get_active_conversation_id(42)
        messages = history.get_all_messages(active_id, limit=10)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "старое сообщение")
        self.assertEqual(history.get_summary(active_id), "старая сводка")




class TestRouterCommands(IsolatedDBTestCase):
    def setUp(self):
        super().setUp()
        import telegram.router as router
        self.router = router
        self.sent = []
        patcher = mock.patch.object(
            router, "send_message",
            lambda chat_id, text, **kw: self.sent.append((chat_id, text)) or {"ok": True},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upd(self, text, chat_id=1):
        return {"message": {"chat": {"id": chat_id}, "text": text}}

    def test_stranger_is_ignored(self):
        self.router.handle_update(self._upd("привет", chat_id=999))
        self.assertEqual(self.sent, [])

    def test_start_and_notes_flow(self):
        self.router.handle_update(self._upd("/start"))
        self.router.handle_update(self._upd("/note купить хлеб"))
        self.router.handle_update(self._upd("/notes"))
        self.assertTrue(any("купить хлеб" in t for _, t in self.sent))

    def test_reminders_flow(self):
        self.router.handle_update(self._upd("/remind через 5 минут проверить почту"))
        self.router.handle_update(self._upd("/reminders"))
        self.assertTrue(any("проверить почту" in t for _, t in self.sent))

    def test_status_command(self):
        self.router.handle_update(self._upd("/status"))
        self.assertTrue(any("Память" in t for _, t in self.sent))

    def test_dialog_commands(self):
        with mock.patch.object(
            self.router.orchestrator, "chat_completion", lambda messages, **kw: "ответ"
        ):
            self.router.handle_update(self._upd("первое сообщение"))
        self.router.handle_update(self._upd("/dialogs"))
        self.assertTrue(any("#1" in t for _, t in self.sent))

        self.router.handle_update(self._upd("/newdialog"))
        self.assertTrue(any("Начал новый диалог #2" in t for _, t in self.sent))

        self.router.handle_update(self._upd("/switchdialog 1"))
        self.assertTrue(any("Переключился на диалог #1" in t for _, t in self.sent))

        self.router.handle_update(self._upd("/closedialog"))
        self.assertTrue(any("Диалог #1 закрыт" in t for _, t in self.sent))

        self.router.handle_update(self._upd("/switchdialog abc"))
        self.assertTrue(any("Использование" in t for _, t in self.sent))

    def test_setsearch_and_setmodel(self):
        self.router.handle_update(self._upd("/setsearch"))
        self.router.handle_update(self._upd("/setsearch searxng"))
        self.assertTrue(any("searxng" in t for _, t in self.sent))

        def fake_urlopen(req, timeout=15):
            payload = {"data": [{"id": "a/b:free", "pricing": {"prompt": "0", "completion": "0"}}]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.router.handle_update(self._upd("/setmodel a/b:free"))
        self.assertTrue(any("a/b:free" in t for _, t in self.sent))

    def test_default_text_goes_through_llm(self):
        import llm.orchestrator as orchestrator
        with mock.patch.object(
            orchestrator, "chat_completion", lambda messages, **kw: "Простой ответ модели."
        ):
            self.router.handle_update(self._upd("привет, бот"))
        self.assertTrue(any("Простой ответ модели" in t for _, t in self.sent))

    def test_pushcode_bad_format_gives_usage(self):
        self.router.handle_update(self._upd("/pushcode без переноса строки"))
        self.assertTrue(any("Использование" in t for _, t in self.sent))


# ────────────────────────── Точка входа ──────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
