"""
Разовый скрипт — прогнать один раз после каждого деплоя (или при
смене публичного URL Railway), чтобы сообщить Telegram, куда слать
апдейты. Не часть постоянно работающего сервиса.

Запуск (локально, с теми же переменными окружения, что в Railway):
    PUBLIC_URL=https://<your-app>.up.railway.app \
    TELEGRAM_BOT_TOKEN=... \
    TELEGRAM_WEBHOOK_SECRET=... \
    python scripts/set_webhook.py
"""
import os
import sys
import json
import urllib.request

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_PATH = os.environ.get("TELEGRAM_WEBHOOK_PATH", "/webhook")

url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"

payload = json.dumps({
    "url": url,
    "secret_token": WEBHOOK_SECRET,
    "allowed_updates": ["message"],
}).encode("utf-8")

req = urllib.request.Request(
    api_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
)

with urllib.request.urlopen(req, timeout=10) as resp:
    result = json.loads(resp.read().decode("utf-8"))

print(json.dumps(result, ensure_ascii=False, indent=2))
if not result.get("ok"):
    sys.exit(1)
