"""
То, что вызывается на каждый тик Railway Cron Job (см. main.py,
эндпоинт /internal/cron). Два независимых дела за один тик:

1. Доставить все просроченные напоминания (нужно дёргать часто —
   раз в 5-15 минут — чтобы напоминания приходили вовремя).
2. Прислать отчёт о состоянии сервера, но не чаще, чем раз в
   MONITORING_REPORT_INTERVAL_HOURS — большинство тиков его пропустит,
   отчёт не должен приходить с той же частотой, что проверка
   напоминаний.
"""
from datetime import datetime, timezone

from config import config
from telegram.api import send_message
from modules.reminders import service as reminders_service
from modules.monitoring import reporter as monitoring_reporter
from storage.db import get_setting, set_setting
from core.logger import get_logger

log = get_logger(__name__)

_LAST_REPORT_KEY = "last_monitoring_report_at"


def run_tick() -> dict:
    now = datetime.now(timezone.utc)
    delivered = _deliver_due_reminders(now)
    reported = _maybe_send_monitoring_report(now)
    return {"reminders_delivered": delivered, "monitoring_report_sent": reported}


def _deliver_due_reminders(now: datetime) -> int:
    due = reminders_service.get_due(now)
    for r in due:
        try:
            send_message(r["chat_id"], f"⏰ Напоминание: {r['message']}")
        except Exception:
            log.exception("Не удалось отправить напоминание id=%s", r["id"])
            continue  # не помечаем доставленным — попробуем на следующем тике
        reminders_service.mark_delivered(r["id"])
    return len(due)


def _maybe_send_monitoring_report(now: datetime) -> bool:
    last_str = get_setting(_LAST_REPORT_KEY)
    if last_str:
        last = datetime.fromisoformat(last_str)
        elapsed_hours = (now - last).total_seconds() / 3600
        if elapsed_hours < config.MONITORING_REPORT_INTERVAL_HOURS:
            return False

    report = monitoring_reporter.build_report()
    send_message(config.OWNER_CHAT_ID, report)
    set_setting(_LAST_REPORT_KEY, now.isoformat())
    return True
