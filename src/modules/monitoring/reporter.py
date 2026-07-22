"""
Собирает читаемый отчёт о состоянии сервера из modules.monitoring.system_stats.
Используется и командой /status (по требованию), и scheduler.py
(периодически, через Railway Cron Job).
"""
import os
from datetime import datetime, timezone

from config import config
from modules.monitoring import system_stats


def build_report() -> str:
    mem = system_stats.read_meminfo()
    load1, load5, load15 = system_stats.read_loadavg()
    disk_path = os.path.dirname(config.DB_PATH) or "/"
    disk = system_stats.read_disk_usage(disk_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"📊 Состояние сервера ({now})\n\n"
        f"Память: {mem['used_mb']:.0f} / {mem['total_mb']:.0f} МБ "
        f"({mem['percent_used']:.0f}%)\n"
        f"Нагрузка (load average 1/5/15 мин): "
        f"{load1:.2f} / {load5:.2f} / {load15:.2f}\n"
        f"Диск ({disk['path']}): {disk['used_gb']:.2f} / {disk['total_gb']:.2f} ГБ "
        f"({disk['percent_used']:.0f}%)"
    )
