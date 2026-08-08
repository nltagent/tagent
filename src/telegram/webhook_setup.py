"""
Регистрация вебхука у Telegram — POST /bot<token>/setWebhook. Вынесено
в отдельный модуль, чтобы одним и тем же кодом пользовались:
  - main.py — вызывает автоматически при каждом старте контейнера
    (значит вручную scripts/set_webhook.py гонять после деплоя больше
    не обязательно: PUBLIC_URL не поменялся — вызов идемпотентный и
    просто подтвердит текущую регистрацию; поменялся — перерегистрирует);
  - scripts/set_webhook.py — для ручного запуска/отладки при желании.
"""
import json
import urllib.request
import urllib.error

from core.logger import get_logger

log = get_logger(__name__)


class WebhookSetupError(RuntimeError):
    pass


def register_webhook(
    *, bot_token: str, public_url: str, webhook_secret: str, webhook_path: str
) -> dict:
    """Сообщает Telegram, куда слать апдейты. Идемпотентно — можно
    звать при каждом старте контейнера без побочных эффектов, если
    адрес не изменился. Бросает WebhookSetupError, если Telegram
    ответил ok: false или сеть недоступна — старт бота НЕ должен из-за
    этого падать целиком (см. main.py), это несмертельная ошибка:
    сервер поднимется и переживёт временную недоступность api.telegram.org."""
    url = f"{public_url.rstrip('/')}{webhook_path}"
    api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload = json.dumps(
        {"url": url, "secret_token": webhook_secret, "allowed_updates": ["message"]}
    ).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        raise WebhookSetupError(f"Не удалось связаться с Telegram API: {e}") from e

    if not result.get("ok"):
        raise WebhookSetupError(f"Telegram отклонил setWebhook: {result}")
    return result
