"""
Тонкая обёртка над Telegram Bot API. Только urllib из стандартной
библиотеки — никаких requests/aiohttp/python-telegram-bot.
"""
import json
import re
import time
import urllib.request
import urllib.error

from config import config
from core.logger import get_logger

log = get_logger(__name__)

API_BASE = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

# Реальный лимит Telegram — 4096 символов на сообщение. Берём с
# запасом (не впритык), чтобы не зависеть от лишнего байта — эмодзи,
# завершающей строки-примечания и т.п.
MAX_MESSAGE_LENGTH = 3600


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


def send_chat_action(chat_id: int | str, action: str = "typing") -> None:
    """"Бот печатает..." — Telegram сам гасит индикатор через ~5 секунд
    или после следующего sendMessage, так что для долгих операций
    вызывающему коду стоит повторять раз в 4-5 секунд, если ожидание
    может быть дольше (см. telegram/router.py)."""
    try:
        _call("sendChatAction", {"chat_id": chat_id, "action": action})
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass  # чисто декоративная штука — не должна ронять основной ответ


_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")
_PARAGRAPH_RE = re.compile(r"\n{2,}")


def split_text_into_chunks(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Режет текст на части не длиннее max_length, стараясь резать по
    границе абзаца, потом по границе предложения, и только если
    отдельный "абзац"/"предложение" сам по себе длиннее лимита — режет
    жёстко посимвольно (иначе одно гигантское предложение без единого
    разделителя никогда бы не влезло)."""
    text = text.strip()
    if len(text) <= max_length:
        return [text] if text else []

    def _split_by(pattern_or_none, piece: str) -> list[str]:
        if pattern_or_none is None:
            return [piece[i:i + max_length] for i in range(0, len(piece), max_length)]
        parts = pattern_or_none.split(piece)
        return [p for p in parts if p]

    def _pack(pieces: list[str], separator: str, fallback_splitter) -> list[str]:
        chunks: list[str] = []
        current = ""
        for piece in pieces:
            candidate = f"{current}{separator}{piece}" if current else piece
            if len(candidate) <= max_length:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(piece) <= max_length:
                current = piece
            else:
                # Сам кусок больше лимита — дробим его дальше меньшими
                # разделителями, рекурсивно.
                sub_chunks = fallback_splitter(piece)
                chunks.extend(sub_chunks[:-1])
                current = sub_chunks[-1] if sub_chunks else ""
        if current:
            chunks.append(current)
        return chunks

    def _by_sentences(piece: str) -> list[str]:
        sentences = _split_by(_SENTENCE_END_RE, piece)
        return _pack(sentences, " ", lambda p: _split_by(None, p))

    paragraphs = _split_by(_PARAGRAPH_RE, text)
    return _pack(paragraphs, "\n\n", _by_sentences)


def send_long_message(chat_id: int | str, text: str, max_length: int = MAX_MESSAGE_LENGTH, **extra) -> None:
    """Как send_message, но если текст не помещается в одно сообщение —
    режет на несколько (по границе абзаца/предложения) и шлёт подряд."""
    chunks = split_text_into_chunks(text, max_length)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"[{i}/{total}] " if total > 1 else ""
        send_message(chat_id, f"{prefix}{chunk}", **extra)


def set_my_commands(commands: list[tuple[str, str]]) -> dict:
    """Меню команд рядом с полем ввода в Telegram-клиенте (кнопка "/").
    commands — [(имя_без_слэша, короткое_описание), ...]. Вызывается
    один раз при настройке (см. scripts/set_commands.py), не на каждом
    старте контейнера."""
    payload = {"commands": [{"command": name, "description": desc} for name, desc in commands]}
    return _call("setMyCommands", payload)


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
