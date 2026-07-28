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


# ────────────────────────── Разбивка длинных сообщений ──────────────────────────

class TestMessageChunking(unittest.TestCase):
    def test_short_text_is_single_chunk(self):
        from telegram.api import split_text_into_chunks

        self.assertEqual(split_text_into_chunks("привет", max_length=100), ["привет"])
        self.assertEqual(split_text_into_chunks("", max_length=100), [])

    def test_splits_on_paragraph_boundary_first(self):
        from telegram.api import split_text_into_chunks

        text = ("Абзац раз. " * 3 + "\n\n") * 2 + "Абзац раз. " * 3
        chunks = split_text_into_chunks(text.strip(), max_length=60)
        self.assertTrue(all(len(c) <= 60 for c in chunks))
        self.assertGreater(len(chunks), 1)

    def test_falls_back_to_sentence_boundary(self):
        from telegram.api import split_text_into_chunks

        text = "Предложение номер один. " * 6 + "Предложение номер два. " * 6
        chunks = split_text_into_chunks(text, max_length=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        # Ни одно предложение не должно быть разорвано посередине
        for c in chunks:
            self.assertFalse(c.startswith(" "))

    def test_hard_split_when_no_boundaries_at_all(self):
        from telegram.api import split_text_into_chunks

        text = "a" * 250
        chunks = split_text_into_chunks(text, max_length=100)
        self.assertEqual(sum(len(c) for c in chunks), 250)
        self.assertTrue(all(len(c) <= 100 for c in chunks))

    def test_exact_boundary_length(self):
        from telegram.api import split_text_into_chunks

        text = "x" * 100
        self.assertEqual(split_text_into_chunks(text, max_length=100), [text])

    def test_send_long_message_sends_one_call_per_chunk(self):
        from telegram.api import send_long_message

        sent = []
        with mock.patch("telegram.api.send_message", lambda chat_id, text, **kw: sent.append(text)):
            send_long_message(1, "короткий текст", max_length=100)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], "короткий текст")  # без префикса [1/1], раз чанк один

        sent.clear()
        long_text = "Предложение. " * 30
        with mock.patch("telegram.api.send_message", lambda chat_id, text, **kw: sent.append(text)):
            send_long_message(1, long_text, max_length=50)
        self.assertGreater(len(sent), 1)
        self.assertTrue(sent[0].startswith("[1/"))


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

    def test_retries_once_on_transient_failure_then_succeeds(self):
        from modules.search import service as search_service
        from modules.search.errors import SearchError

        config.SEARCH_RETRY_DELAY_SECONDS = 0.01  # не ждать реальные 2.5с в тестах
        attempts = {"n": 0}

        def flaky(query, max_results=5, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise SearchError("временный сбой, например холодный старт")
            return [{"title": "T", "url": "https://x", "snippet": "s"}]

        search_service.set_active_provider("keenable")
        with mock.patch.object(search_service._PROVIDERS["keenable"], "search", flaky):
            results = search_service.search("тест")

        self.assertEqual(attempts["n"], 2)
        self.assertEqual(results[0]["title"], "T")

    def test_config_errors_are_not_retried(self):
        from modules.search import service as search_service
        from modules.search.errors import SearchConfigError

        attempts = {"n": 0}

        def always_config_error(query, max_results=5, **kw):
            attempts["n"] += 1
            raise SearchConfigError("ключ не задан")

        search_service.set_active_provider("keenable")
        with mock.patch.object(search_service._PROVIDERS["keenable"], "search", always_config_error):
            with self.assertRaises(SearchConfigError):
                search_service.search("тест")

        self.assertEqual(attempts["n"], 1)  # без повторной попытки


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

    def test_price_hints_capture_nonstandard_fields(self):
        import llm.models as llm_models

        def fake_urlopen(req, timeout=15):
            payload = {
                "data": [
                    {"id": "clavis/a", "name": "A", "cost_rub_per_1k": 0, "description": "irrelevant"},
                    {"id": "clavis/b", "name": "B", "cost_rub_per_1k": 1.5},
                ]
            }
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            hints = llm_models.list_price_hints()

        self.assertEqual(len(hints), 2)
        self.assertIn("cost_rub_per_1k", hints[0])
        self.assertNotIn("description", hints[0])  # только id + ценовые поля


# ────────────────────────── "Умное" определение бесплатных моделей ──────────────────────────

class TestModelFilter(IsolatedDBTestCase):
    def test_classifies_nonstandard_pricing_field_via_llm(self):
        import llm.model_filter as model_filter

        def fake_urlopen(req, timeout=15):
            payload = {
                "data": [
                    {"id": "clavis/free-one", "name": "Free one", "cost_rub_per_1k": 0},
                    {"id": "clavis/paid-one", "name": "Paid one", "cost_rub_per_1k": 2},
                ]
            }
            return FakeResponse(json.dumps(payload).encode())

        def fake_chat_completion(messages, **kw):
            payload = json.loads(messages[1]["content"])
            self.assertIn("cost_rub_per_1k", payload[0])
            return "clavis/free-one"

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(model_filter, "chat_completion", fake_chat_completion):
            result = model_filter.classify_free_models()

        self.assertEqual([m["id"] for m in result], ["clavis/free-one"])

    def test_result_is_cached_until_ttl_or_force_refresh(self):
        import llm.model_filter as model_filter

        fetch_calls = {"n": 0}

        def fake_urlopen(req, timeout=15):
            fetch_calls["n"] += 1
            payload = {"data": [{"id": "a/free:free", "name": "A"}]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(model_filter, "chat_completion", lambda messages, **kw: "a/free:free"):
            model_filter.classify_free_models()
            model_filter.classify_free_models()  # из кэша, без новых вызовов
            self.assertEqual(fetch_calls["n"], 1)

            model_filter.classify_free_models(force_refresh=True)
            self.assertEqual(fetch_calls["n"], 2)

    def test_falls_back_to_heuristic_when_llm_fails(self):
        import llm.model_filter as model_filter
        from llm.client import LLMError

        def fake_urlopen(req, timeout=15):
            payload = {
                "data": [
                    {"id": "a/free:free", "name": "A"},
                    {"id": "b/paid", "name": "B", "pricing": {"prompt": "0.1", "completion": "0.2"}},
                ]
            }
            return FakeResponse(json.dumps(payload).encode())

        def failing_chat_completion(messages, **kw):
            raise LLMError("модель недоступна")

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(model_filter, "chat_completion", failing_chat_completion):
            result = model_filter.classify_free_models()

        self.assertEqual([m["id"] for m in result], ["a/free:free"])  # эвристика по :free


# ────────────────────────── Системный промпт: дата/время ──────────────────────────

class TestPrompts(unittest.TestCase):
    def test_system_prompt_includes_current_datetime(self):
        import llm.prompts as prompts
        from datetime import datetime

        prompt = prompts.build_system_prompt()
        self.assertIn("Текущие дата и время", prompt)
        self.assertIn(str(datetime.now().year), prompt)
        self.assertIn(config.USER_TIMEZONE, prompt)

    def test_system_prompt_includes_self_identity(self):
        import llm.prompts as prompts
        from llm.client import get_active_model

        prompt = prompts.build_system_prompt()
        self.assertIn(get_active_model(), prompt)
        self.assertIn(config.LLM_BASE_URL, prompt)
        self.assertIn("личного Telegram-агента", prompt)


# ────────────────────────── Несколько профилей LLM-провайдеров ──────────────────────────

# ────────────────────────── Отказоустойчивость LLM (fallback) ──────────────────────────

class TestFallback(IsolatedDBTestCase):
    def test_falls_back_to_free_model_same_provider(self):
        from llm import fallback, model_filter
        from llm.client import LLMError

        def fake_chat_completion(messages, max_tokens=1000, temperature=0.7,
                                  profile_name=None, model=None):
            if model == config.LLM_MODEL:
                raise LLMError("модель временно недоступна")
            if model == "or/free-backup":
                return "ответ от резерва"
            raise LLMError("неожиданная модель")

        with mock.patch.object(
            model_filter, "classify_free_models",
            lambda force_refresh=False: [{"id": "or/free-backup", "name": "Backup"}],
        ), mock.patch.object(fallback, "chat_completion", fake_chat_completion):
            result = fallback.resilient_chat_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(result.text, "ответ от резерва")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.model, "or/free-backup")

    def test_falls_back_to_another_provider_profile(self):
        from llm import fallback, providers, model_filter
        from llm.client import LLMError

        providers.add_profile("clavis", "https://api.clavis.to/v1", "sk-1", "clavis/m")

        def fake_chat_completion(messages, max_tokens=1000, temperature=0.7,
                                  profile_name=None, model=None):
            if profile_name == "default":
                raise LLMError("весь провайдер лежит")
            return "ответ от clavis"

        with mock.patch.object(model_filter, "classify_free_models", lambda force_refresh=False: []), \
             mock.patch.object(fallback, "chat_completion", fake_chat_completion):
            result = fallback.resilient_chat_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(result.profile_name, "clavis")
        self.assertTrue(result.used_fallback)

    def test_no_fallback_needed_when_active_model_works(self):
        from llm import fallback

        with mock.patch.object(fallback, "chat_completion", lambda messages, **kw: "ok сразу"):
            result = fallback.resilient_chat_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(result.text, "ok сразу")
        self.assertFalse(result.used_fallback)

    def test_raises_when_everything_fails(self):
        from llm import fallback, providers
        from llm.client import LLMError

        providers.add_profile("clavis", "https://api.clavis.to/v1", "sk-1", "clavis/m")

        def always_fails(messages, **kw):
            raise LLMError("недоступно")

        with mock.patch.object(fallback, "chat_completion", always_fails):
            with self.assertRaises(fallback.AllProvidersFailedError):
                fallback.resilient_chat_completion([{"role": "user", "content": "hi"}])

    def test_orchestrator_uses_fallback_transparently(self):
        """orchestrator.chat_completion теперь = resilient-обёртка, но
        вызывающий код (get_reply) не заметил разницы — сигнатура и
        возврат (строка) те же самые."""
        import llm.orchestrator as orchestrator
        from llm.client import LLMError

        def fake_low_level(messages, max_tokens=1000, temperature=0.7,
                           profile_name=None, model=None):
            if model == config.LLM_MODEL:
                raise LLMError("недоступна")
            return "ответ от резерва через оркестратор"

        with mock.patch(
            "llm.model_filter.classify_free_models",
            lambda force_refresh=False: [{"id": "or/free-backup", "name": "Backup"}],
        ), mock.patch("llm.fallback.chat_completion", fake_low_level):
            reply = orchestrator.get_reply(1, "привет")

        self.assertEqual(reply, "ответ от резерва через оркестратор")


class TestProviders(IsolatedDBTestCase):
    def test_default_profile_uses_config(self):
        from llm import providers

        self.assertEqual(providers.list_profile_names(), ["default"])
        self.assertEqual(providers.get_active_profile_name(), "default")
        self.assertEqual(
            providers.get_active_credentials(), (config.LLM_BASE_URL, config.LLM_API_KEY)
        )

    def test_add_switch_and_remove_profile(self):
        from llm import providers

        providers.add_profile("clavis", "https://api.clavis.to/v1", "sk-123", "clavis/model-x")
        self.assertIn("clavis", providers.list_profile_names())

        providers.set_active_profile("clavis")
        self.assertEqual(providers.get_active_profile_name(), "clavis")
        self.assertEqual(
            providers.get_active_credentials(), ("https://api.clavis.to/v1", "sk-123")
        )

        self.assertTrue(providers.remove_profile("clavis"))
        # Удалили активный профиль -> откат на default
        self.assertEqual(providers.get_active_profile_name(), "default")

    def test_cannot_overwrite_or_switch_to_unknown(self):
        from llm import providers
        from llm.providers import ProviderError

        with self.assertRaises(ProviderError):
            providers.add_profile("default", "https://x", "key")
        with self.assertRaises(ProviderError):
            providers.set_active_profile("does-not-exist")

    def test_model_is_remembered_per_profile(self):
        from llm import providers
        from llm.client import get_active_model, set_active_model

        default_model = get_active_model()
        providers.add_profile("clavis", "https://api.clavis.to/v1", "sk-123", "clavis/default")
        providers.set_active_profile("clavis")
        self.assertEqual(get_active_model(), "clavis/default")

        set_active_model("clavis/other")
        self.assertEqual(get_active_model(), "clavis/other")

        providers.set_active_profile("default")
        self.assertEqual(get_active_model(), default_model)  # не перепуталось с clavis

        providers.set_active_profile("clavis")
        self.assertEqual(get_active_model(), "clavis/other")  # не потерялось

    def test_chat_completion_uses_active_profile_credentials(self):
        from llm import providers
        from llm.client import chat_completion

        providers.add_profile("clavis", "https://api.clavis.to/v1", "sk-clavis", "clavis/m")
        providers.set_active_profile("clavis")

        captured = {}

        def fake_urlopen(req, timeout=60):
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            chat_completion([{"role": "user", "content": "hi"}])

        self.assertTrue(captured["url"].startswith("https://api.clavis.to/v1"))
        self.assertEqual(captured["auth"], "Bearer sk-clavis")

    def test_router_provider_commands(self):
        import telegram.router as router
        sent = []
        with mock.patch.object(
            router, "send_message", lambda chat_id, text, **kw: sent.append(text)
        ):
            router.handle_update({"message": {"chat": {"id": 1}, "text": "/addprovider"}})
            router.handle_update({
                "message": {"chat": {"id": 1},
                            "text": "/addprovider clavis https://api.clavis.to/v1 sk-1 clavis/m"}
            })
            router.handle_update({"message": {"chat": {"id": 1}, "text": "/providers"}})
            router.handle_update({"message": {"chat": {"id": 1}, "text": "/setprovider clavis"}})
            router.handle_update({"message": {"chat": {"id": 1}, "text": "/setprovider nope"}})
            router.handle_update({"message": {"chat": {"id": 1}, "text": "/delprovider clavis"}})

        self.assertTrue(any("Использование" in t for t in sent))
        self.assertTrue(any("добавлен" in t for t in sent))
        self.assertTrue(any("➤ default" in t for t in sent))
        self.assertTrue(any("переключён на: clavis" in t for t in sent))
        self.assertTrue(any("не найден" in t for t in sent))
        self.assertTrue(any("удалён" in t for t in sent))


# ────────────────────────── Живая самопроверка ──────────────────────────

class TestDiagnostics(IsolatedDBTestCase):
    def test_selftest_reports_ok_for_each_working_check(self):
        import modules.diagnostics.service as diagnostics

        config.GITHUB_TOKEN = ""  # тестовое окружение обычно задаёт токен глобально — тут проверяем путь "не настроено"
        with mock.patch("llm.client.chat_completion", lambda messages, **kw: "тест"), \
             mock.patch("modules.search.service.search", lambda q, max_results=5, **kw: [{"title": "T"}]), \
             mock.patch("modules.search.service.get_active_provider_name", lambda: "keenable"):
            report = diagnostics.run_selftest()

        self.assertIn("✅ База данных", report)
        self.assertIn("✅ LLM", report)
        self.assertIn("✅ Поиск", report)
        self.assertIn("⏭️ GitHub", report)  # токен не задан
        self.assertIn("✅ Мониторинг сервера", report)

    def test_selftest_github_check_when_configured(self):
        import modules.diagnostics.service as diagnostics

        config.GITHUB_TOKEN = "ghp_test"
        with mock.patch("llm.client.chat_completion", lambda messages, **kw: "тест"), \
             mock.patch("modules.search.service.search", lambda q, max_results=5, **kw: []), \
             mock.patch("modules.search.service.get_active_provider_name", lambda: "keenable"), \
             mock.patch("modules.github.service._request", lambda method, path: {"rate": {"remaining": 4999}}):
            report = diagnostics.run_selftest()

        self.assertIn("✅ GitHub", report)
        self.assertIn("4999", report)

    def test_selftest_reports_failure_clearly(self):
        import modules.diagnostics.service as diagnostics
        from llm.client import LLMError

        def failing_llm(messages, **kw):
            raise LLMError("недоступна")

        with mock.patch("llm.client.chat_completion", failing_llm):
            report = diagnostics.run_selftest()

        self.assertIn("❌ LLM", report)
        self.assertIn("недоступна", report)

    def test_cron_health_ok_when_no_overdue_reminders(self):
        import modules.diagnostics.service as diagnostics

        result = diagnostics._check_cron_health()
        self.assertIn("просроченных недоставленных: 0", result)

    def test_cron_health_flags_stuck_overdue_reminder(self):
        import modules.diagnostics.service as diagnostics
        from modules.reminders import service as reminders_service

        now = datetime.now(timezone.utc)
        reminders_service.add_reminder(1, "застрявшее напоминание", now - timedelta(days=2))

        with self.assertRaises(RuntimeError) as ctx:
            diagnostics._check_cron_health()
        self.assertIn("Cron Job", str(ctx.exception))

    def test_notes_and_dialogs_roundtrip_checks(self):
        import modules.diagnostics.service as diagnostics
        from modules.notes import service as notes_service
        from modules.conversations import service as conversations

        result = diagnostics._check_notes_roundtrip()
        self.assertIn("работают", result)
        self.assertEqual(notes_service.list_notes("_selftest"), [])  # убрано за собой

        result2 = diagnostics._check_dialogs_roundtrip()
        self.assertIn("работают", result2)

    def test_github_roundtrip_skipped_without_test_repo(self):
        import modules.diagnostics.service as diagnostics

        config.GITHUB_TOKEN = "ghp_test"
        config.GITHUB_TEST_REPO = ""
        with self.assertRaises(diagnostics._Skip):
            diagnostics._check_github_roundtrip()

    def test_github_roundtrip_creates_and_cleans_up_branch(self):
        import modules.diagnostics.service as diagnostics

        config.GITHUB_TOKEN = "ghp_test"
        config.GITHUB_TEST_REPO = "owner/test-repo"
        calls = []

        def fake_urlopen(req, timeout=20):
            method, url = req.get_method(), req.full_url
            calls.append((method, url))
            if "/git/ref/heads/selftest-" in url:
                raise http_error(url, 404)
            if "/git/ref/heads/main" in url:
                return FakeResponse(json.dumps({"object": {"sha": "s"}}).encode())
            if "/git/refs" in url and method == "POST":
                return FakeResponse(json.dumps({"ref": "refs/heads/x"}).encode())
            if "/contents/" in url and method == "GET":
                raise http_error(url, 404)
            if "/contents/" in url and method == "PUT":
                return FakeResponse(json.dumps({"content": {"html_url": "https://x"}}).encode())
            if "/git/refs/heads/selftest-" in url and method == "DELETE":
                return FakeResponse(b"{}")
            raise AssertionError(f"{method} {url}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = diagnostics._check_github_roundtrip()

        self.assertIn("branch+commit+delete", result)
        self.assertEqual(calls[-1][0], "DELETE")  # ветка убрана за собой

    def test_run_selftest_all_includes_expanded_checks(self):
        import modules.diagnostics.service as diagnostics

        config.GITHUB_TOKEN = ""
        with mock.patch("llm.client.chat_completion", lambda messages, **kw: "тест"), \
             mock.patch("modules.search.service.search", lambda q, max_results=5, **kw: []), \
             mock.patch("modules.search.service.get_active_provider_name", lambda: "keenable"), \
             mock.patch("llm.models.list_models", lambda: [{"id": "a", "name": "A", "free": True}]):
            report = diagnostics.run_selftest_all()

        self.assertIn("Cron / напоминания", report)
        self.assertIn("Заметки (round-trip)", report)
        self.assertIn("Диалоги (round-trip)", report)
        self.assertIn("Список моделей", report)
        self.assertIn("получено моделей от провайдера: 1", report)


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

    def test_search_tag_triggers_refine_then_second_call_with_results(self):
        import llm.orchestrator as orchestrator

        calls = {"main": 0, "refine": 0}

        def fake_chat_completion(messages, **kw):
            if "Ты помогаешь сформулировать" in messages[0]["content"]:
                calls["refine"] += 1
                return "amsterdam weather today"
            calls["main"] += 1
            if calls["main"] == 1:
                return "[SEARCH: погода в Амстердаме]"
            # На финальном вызове в истории должны быть результаты поиска
            # именно по УТОЧНЁННОМУ запросу, а не по черновому.
            self.assertTrue(any("amsterdam weather today" in m["content"] for m in messages))
            return "Сейчас в Амстердаме облачно, 18°C."

        def fake_search(query, max_results=5, **kw):
            self.assertEqual(query, "amsterdam weather today")  # именно уточнённый
            return [{"title": "Погода", "url": "https://x.example", "snippet": "18°C, облачно"}]

        with mock.patch.object(orchestrator, "chat_completion", fake_chat_completion), \
             mock.patch.object(orchestrator.search_service, "search", fake_search):
            reply = orchestrator.get_reply(1, "какая погода в Амстердаме?")

        self.assertIn("18", reply)
        self.assertIn("🔍 Искал: amsterdam weather today", reply)
        self.assertEqual(calls["main"], 2)
        self.assertEqual(calls["refine"], 1)

    def test_multiple_search_queries_in_one_turn(self):
        import llm.orchestrator as orchestrator

        searched_queries = []

        def fake_chat_completion(messages, **kw):
            if "Ты помогаешь сформулировать" in messages[0]["content"]:
                draft = messages[1]["content"].replace("Черновой запрос: ", "")
                return f"{draft} (refined)"
            last = messages[-1]["content"]
            if "результаты поиска" in last.lower():
                self.assertIn("погода Амстердам (refined)", last)
                self.assertIn("курс евро (refined)", last)
                return "Вот оба ответа сразу."
            return "[SEARCH: погода Амстердам][SEARCH: курс евро]"

        def fake_search(query, max_results=5, **kw):
            searched_queries.append(query)
            return [{"title": "T", "url": "https://x", "snippet": query}]

        with mock.patch.object(orchestrator, "chat_completion", fake_chat_completion), \
             mock.patch.object(orchestrator.search_service, "search", fake_search):
            reply = orchestrator.get_reply(1, "погода и курс?")

        self.assertEqual(searched_queries, ["погода Амстердам (refined)", "курс евро (refined)"])
        self.assertIn("🔍 Искал:", reply)
        self.assertIn("погода Амстердам (refined)", reply)
        self.assertIn("курс евро (refined)", reply)

    def test_search_queries_are_capped_per_turn(self):
        import llm.orchestrator as orchestrator

        config.SEARCH_MAX_QUERIES_PER_TURN = 2
        refine_calls = {"n": 0}

        def fake_chat_completion(messages, **kw):
            if "Ты помогаешь сформулировать" in messages[0]["content"]:
                refine_calls["n"] += 1
                return f"refined-{refine_calls['n']}"
            last = messages[-1]["content"]
            if "результаты поиска" in last.lower():
                return "Готово."
            return "[SEARCH: a][SEARCH: b][SEARCH: c][SEARCH: d][SEARCH: e]"

        def fake_search(query, max_results=5, **kw):
            return [{"title": "T", "url": "https://x", "snippet": "s"}]

        with mock.patch.object(orchestrator, "chat_completion", fake_chat_completion), \
             mock.patch.object(orchestrator.search_service, "search", fake_search):
            orchestrator.get_reply(1, "запрос с кучей вопросов")

        self.assertEqual(refine_calls["n"], 2)  # не 5


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

        # send_long_message/send_chat_action живут в telegram.api и
        # вызывают СВОИ внутренние send_message/HTTP-запросы — патч
        # router.send_message их не перехватывает (это другой
        # модульный биндинг), поэтому патчим отдельно.
        def fake_send_long_message(chat_id, text, max_length=None, **kw):
            from telegram.api import split_text_into_chunks, MAX_MESSAGE_LENGTH
            for chunk in split_text_into_chunks(text, max_length or MAX_MESSAGE_LENGTH):
                self.sent.append((chat_id, chunk))

        patcher2 = mock.patch.object(router, "send_long_message", fake_send_long_message)
        patcher2.start()
        self.addCleanup(patcher2.stop)

        patcher3 = mock.patch.object(router, "send_chat_action", lambda chat_id, action="typing": None)
        patcher3.start()
        self.addCleanup(patcher3.stop)

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

    def test_notes_truncated_by_default_and_full_on_request(self):
        for i in range(80):
            self.router.handle_update(self._upd(f"/note заметка номер {i} с некоторым текстом для объёма"))
        self.sent.clear()

        self.router.handle_update(self._upd("/notes"))
        # По умолчанию — один (обрезанный) ответ с пометкой, что не всё показано
        self.assertEqual(len(self.sent), 1)
        self.assertIn("показаны не все заметки", self.sent[0][1])
        self.assertLessEqual(len(self.sent[0][1]), 3700)

        self.sent.clear()
        self.router.handle_update(self._upd("/notes полностью"))
        # Полностью — может быть несколько сообщений, но все заметки должны быть видны
        joined = "\n".join(t for _, t in self.sent)
        self.assertIn("заметка номер 0 ", joined)
        self.assertIn("заметка номер 79 ", joined)

    def test_history_compressed_by_default_and_full_on_request(self):
        long_filler = "текст для объёма " * 15  # ~250 символов на сообщение
        with mock.patch.object(
            self.router.orchestrator, "chat_completion", lambda messages, **kw: "ответ на сообщение " + long_filler
        ):
            for i in range(15):
                self.router.handle_update(self._upd(f"вопрос номер {i} {long_filler}"))
        self.sent.clear()

        with mock.patch.object(
            self.router.orchestrator, "compress_text", lambda text, max_length=3600: "СЖАТАЯ ИСТОРИЯ"
        ):
            self.router.handle_update(self._upd("/history"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("СЖАТАЯ ИСТОРИЯ", self.sent[0][1])
        self.assertIn("история сокращена", self.sent[0][1])

        self.sent.clear()
        self.router.handle_update(self._upd("/history полностью"))
        joined = "\n".join(t for _, t in self.sent)
        self.assertIn("вопрос номер 14", joined)  # последнее сообщение точно видно

    def test_help_lists_grouped_commands(self):
        self.router.handle_update(self._upd("/help"))
        text = self.sent[-1][1]
        self.assertIn("/note", text)
        self.assertIn("/remind", text)
        self.assertIn("/pushcode", text)
        self.assertIn("/models_all", text)
        # /start сам по себе должен остаться коротким и не дублировать /help
        self.sent.clear()
        self.router.handle_update(self._upd("/start"))
        start_text = self.sent[-1][1]
        self.assertLess(len(start_text), len(text))

    def test_models_all_and_models_free_commands(self):
        def fake_urlopen(req, timeout=15):
            payload = {"data": [
                {"id": "a/free:free", "name": "A"},
                {"id": "b/paid", "name": "B", "pricing": {"prompt": "0.1", "completion": "0.2"}},
            ]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(self.router.model_filter, "chat_completion", lambda m, **kw: "a/free:free"):
            self.router.handle_update(self._upd("/models_all"))
            self.router.handle_update(self._upd("/models_free"))
            self.router.handle_update(self._upd("/models"))

        all_text, free_text, models_text = (t for _, t in self.sent[-3:])
        self.assertIn("b/paid", all_text)
        self.assertNotIn("b/paid", free_text)
        self.assertIn("a/free:free", free_text)
        self.assertEqual(free_text, models_text)  # /models — алиас /models_free

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
