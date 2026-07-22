"""
Сырые метрики контейнера. Специально без psutil — всё то же самое
можно взять из /proc и стандартной библиотеки:
- память: /proc/meminfo (внутри контейнера показывает лимиты cgroup,
  что и нужно — реальные ограничения, выданные Railway/Docker);
- нагрузка: os.getloadavg() (обёртка над тем же /proc/loadavg);
- диск: shutil.disk_usage() (обёртка над statvfs).
"""
import os
import shutil


def read_meminfo() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            parts = rest.strip().split()
            if parts:
                info[key] = int(parts[0])  # значение в кБ

    total_kb = info.get("MemTotal", 0)
    # MemAvailable точнее MemFree (учитывает вытесняемый кэш), но есть
    # не во всех ядрах — на этот случай запасной вариант.
    available_kb = info.get("MemAvailable", info.get("MemFree", 0))
    used_kb = max(total_kb - available_kb, 0)
    percent = (used_kb / total_kb * 100) if total_kb else 0.0

    return {
        "total_mb": total_kb / 1024,
        "used_mb": used_kb / 1024,
        "available_mb": available_kb / 1024,
        "percent_used": percent,
    }


def read_loadavg() -> tuple[float, float, float]:
    return os.getloadavg()


def read_disk_usage(path: str = "/") -> dict:
    total, used, free = shutil.disk_usage(path)
    gb = 1024 ** 3
    return {
        "path": path,
        "total_gb": total / gb,
        "used_gb": used / gb,
        "free_gb": free / gb,
        "percent_used": (used / total * 100) if total else 0.0,
    }
