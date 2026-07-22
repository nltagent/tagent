"""
Разбор времени напоминания из начала строки, без сторонних библиотек
(dateutil и т.п.) — несколько простых форматов на русском.

Поддерживается (регистр не важен):
    через 10 минут <текст>
    через 2 часа <текст>
    через 3 дня <текст>
    завтра в 9:00 <текст>
    сегодня в 18:30 <текст>
    18:30 <текст>                (сегодня, если время ещё не прошло, иначе завтра)
    2026-07-22 09:00 <текст>

Часовой пояс — config.USER_TIMEZONE (stdlib zoneinfo, с учётом DST).
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import config


class TimeParseError(ValueError):
    pass


_REL_UNITS = {
    "минута": "minutes", "минуту": "minutes", "минуты": "minutes", "минут": "minutes", "мин": "minutes",
    "час": "hours", "часа": "hours", "часов": "hours",
    "день": "days", "дня": "days", "дней": "days",
}

_REL_RE = re.compile(r"^через\s+(\d+)\s+(\S+)\s*", re.IGNORECASE)
_DAYWORD_RE = re.compile(r"^(завтра|сегодня)\s+в\s+(\d{1,2}):(\d{2})\s*", re.IGNORECASE)
_ABSOLUTE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*")
_TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*")


def _tz() -> ZoneInfo:
    return ZoneInfo(config.USER_TIMEZONE)


def parse_when(text: str) -> tuple[datetime, str]:
    """Возвращает (due_at_utc, остаток_строки_с_текстом_напоминания).
    Бросает TimeParseError, если в начале строки не распознан формат."""
    text = text.strip()
    tz = _tz()
    now_local = datetime.now(tz)

    m = _REL_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit_word = m.group(2).lower()
        unit = _REL_UNITS.get(unit_word)
        if unit is None:
            raise TimeParseError(
                f"Не понял единицу времени «{unit_word}». Используйте "
                "минут(у/ы), час(ов/а) или день/дня/дней."
            )
        due_local = now_local + timedelta(**{unit: amount})
        return due_local.astimezone(timezone.utc), text[m.end():]

    m = _DAYWORD_RE.match(text)
    if m:
        day_word, hh, mm = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        base = now_local.date()
        if day_word == "завтра":
            base += timedelta(days=1)
        due_local = datetime(base.year, base.month, base.day, hh, mm, tzinfo=tz)
        return due_local.astimezone(timezone.utc), text[m.end():]

    m = _ABSOLUTE_RE.match(text)
    if m:
        y, mo, d, hh, mm = (int(g) for g in m.groups())
        try:
            due_local = datetime(y, mo, d, hh, mm, tzinfo=tz)
        except ValueError as e:
            raise TimeParseError(f"Некорректная дата/время: {e}") from e
        return due_local.astimezone(timezone.utc), text[m.end():]

    m = _TIME_ONLY_RE.match(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise TimeParseError("Некорректное время.")
        due_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if due_local <= now_local:
            due_local += timedelta(days=1)
        return due_local.astimezone(timezone.utc), text[m.end():]

    raise TimeParseError(
        "Не понял, когда напомнить. Примеры: «через 10 минут ...», "
        "«завтра в 9:00 ...», «18:30 ...», «2026-07-22 09:00 ...»."
    )
