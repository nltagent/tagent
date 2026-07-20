"""
Общий лимитер частоты запросов — используется и для LLM, и для
поиска (Keenable), просто с разными параметрами. Два механизма сразу:
минимальный интервал между вызовами (чтобы не бомбить API подряд) и
скользящее окно "не больше N вызовов в минуту" (чтобы не упереться в
дневной/минутный лимит провайдера).
"""
import collections
import time

from core.logger import get_logger

log = get_logger(__name__)


class RateLimiter:
    def __init__(self, max_per_minute: int | None = None, min_interval: float = 0.0):
        self.max_per_minute = max_per_minute
        self.min_interval = min_interval
        self._calls: collections.deque = collections.deque()
        self._last_call = 0.0

    def wait_if_needed(self) -> None:
        # Минимальный интервал между двумя последовательными вызовами.
        now = time.monotonic()
        gap = now - self._last_call
        if self.min_interval and gap < self.min_interval:
            sleep_for = self.min_interval - gap
            log.debug("Rate limiter: жду %.2fс (min_interval)", sleep_for)
            time.sleep(sleep_for)

        # Скользящее окно в 60 секунд.
        if self.max_per_minute:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_minute:
                sleep_for = 60 - (now - self._calls[0])
                if sleep_for > 0:
                    log.info("Rate limiter: жду %.1fс (лимит в минуту)", sleep_for)
                    time.sleep(sleep_for)

        self._calls.append(time.monotonic())
        self._last_call = time.monotonic()
