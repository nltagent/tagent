"""
Точка входа. Сервер на чистом http.server (stdlib) — без Flask/FastAPI.
Обрабатывает:
  POST <config.WEBHOOK_PATH>  — вебхук от Telegram
  POST /internal/cron          — тик Railway Cron Job (напоминания + мониторинг)
  GET  /health                 — проверка живости (Railway/ручная)

Почему так: минимум зависимостей = нечему сломаться при пересборке
образа. Вебхук (а не long-polling) выбран специально — это входящий
трафик, который не мешает Railway усыплять контейнер (Serverless),
в отличие от long-polling, который сам постоянно стучится наружу.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import config
from telegram.router import handle_update
from core.logger import get_logger
import scheduler

log = get_logger(__name__)

CRON_PATH = "/internal/cron"


class Handler(BaseHTTPRequestHandler):
    # Отключаем стандартный лог BaseHTTPRequestHandler в stderr на
    # каждый запрос — используем свой логгер вместо него.
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, b"ok")
        else:
            self._respond(404, b"not found")

    def do_POST(self):
        if self.path == config.WEBHOOK_PATH:
            self._handle_webhook()
        elif self.path == CRON_PATH:
            self._handle_cron()
        else:
            self._respond(404, b"not found")

    def _handle_webhook(self):
        # Проверяем секретный токен — так отсекаем любые запросы,
        # кроме реальных от Telegram (см. set_webhook.py).
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != config.WEBHOOK_SECRET:
            log.warning("Отклонён webhook-запрос с неверным secret token")
            self._respond(403, b"forbidden")
            return

        raw_body = self._read_body()

        # Отвечаем Telegram сразу 200 OK — до обработки. Telegram ждёт
        # быстрый ответ и иначе будет ретраить доставку. Сама отправка
        # ответа пользователю уйдёт отдельным вызовом sendMessage внутри
        # handle_update, а не через тело этого ответа.
        self._respond(200, b"ok")

        try:
            update = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            log.exception("Не удалось распарсить тело webhook-запроса")
            return

        handle_update(update)

    def _handle_cron(self):
        # Отдельный секрет — этот эндпоинт дёргает не Telegram, а
        # Railway Cron Job по приватной сети (см. README, шаг 4).
        secret = self.headers.get("X-Cron-Secret")
        if secret != config.CRON_SECRET:
            log.warning("Отклонён cron-запрос с неверным секретом")
            self._respond(403, b"forbidden")
            return

        self._read_body()  # тело не нужно, но дочитать сокет надо
        try:
            result = scheduler.run_tick()
            log.info("Cron-тик выполнен: %s", result)
            self._respond(200, json.dumps(result).encode("utf-8"))
        except Exception:
            log.exception("Ошибка при выполнении cron-тика")
            self._respond(500, b'{"error": "internal error"}')

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b"{}"

    def _respond(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    log.info("Слушаю на порту %s, webhook path=%s", config.PORT, config.WEBHOOK_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
