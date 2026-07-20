"""
Тонкая обёртка над Telegram Bot API. Только urllib из стандартной
библиотеки — никаких requests/aiohttp/python-telegram-bot.
"""
import json
import urllib.request
import urllib.error

from config import config
from core.logger import get_logger

log = get_logger(__name__)

API_BASE = f"https://api.telegram.org/bot{config.BOT_TOKEN}"


def _call(method: str, payload: dict, timeout: int = 10) -> dict:
    """Низкоуровневый вызов метода Telegram Bot API."""
    url = f"{API_BASE}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("Telegram API HTTP %s at %s: %s", e.code, method, body)
        raise
    except urllib.error.URLError as e:
        log.error("Telegram API network error at %s: %s", method, e)
        raise


def send_message(chat_id: int | str, text: str, **extra) -> dict:
    """Отправить текстовое сообщение. extra прокидывается как есть
    (например parse_mode='HTML', reply_markup=...)."""
    payload = {"chat_id": chat_id, "text": text, **extra}
    return _call("sendMessage", payload)


def set_webhook(url: str, secret_token: str) -> dict:
    """Зарегистрировать вебхук в Telegram. Вызывается один раз при
    настройке (см. scripts/set_webhook.py), не на каждом старте."""
    payload = {
        "url": url,
        "secret_token": secret_token,
        "allowed_updates": ["message"],
    }
    return _call("setWebhook", payload)


def delete_webhook() -> dict:
    """Убрать вебхук — полезно при локальной отладке через polling
    (не используется в проде, но пригодится для тестов)."""
    return _call("deleteWebhook", {})
