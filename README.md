# telegram-agent — шаг 1: скелет

Минимальный работающий каркас: вебхук от Telegram → эхо-ответ.
Цель этого шага — проверить всю цепочку целиком (Telegram → Railway
→ контейнер → обратно в Telegram), прежде чем добавлять модули.

## Локальный запуск (без Railway, для проверки, что сервер вообще стартует)

```bash
cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, OWNER_CHAT_ID
export $(cat .env | grep -v '^#' | xargs)
cd src && python main.py
```

Проверить, что жив:
```bash
curl http://localhost:8080/health
```

Локально Telegram не сможет достучаться до вашего компьютера напрямую
(нет публичного URL) — полноценная проверка вебхука делается уже
после деплоя на Railway. Для чисто локальной отладки логики можно
вручную дёрнуть `/webhook` через curl, подставив свой `TELEGRAM_WEBHOOK_SECRET`
и тело апдейта в формате Telegram Bot API.

## Деплой на Railway

1. Создайте новый проект на Railway, подключите этот репозиторий (или
   загрузите как есть).
2. **Важно:** Dockerfile лежит в `docker/Dockerfile`, а не в корне.
   В настройках сервиса (Settings → Build) укажите:
   - Dockerfile Path: `docker/Dockerfile`
   - Build Context: корень репозитория (`.`)

   Dockerfile ссылается на `requirements.txt` и `src/` от корня контекста,
   так что билд-контекст должен быть корнем репо, а не `docker/`.
3. Settings → Networking → Generate Domain — получите публичный URL
   вида `https://<name>.up.railway.app`.
4. Variables — задайте `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
   `OWNER_CHAT_ID` (см. `.env.example`). `PORT` Railway подставит сам.
5. После первого успешного деплоя — зарегистрируйте вебхук (с любой
   машины с интернетом, включая свой ноутбук):
   ```bash
   PUBLIC_URL=https://<name>.up.railway.app \
   TELEGRAM_BOT_TOKEN=... \
   TELEGRAM_WEBHOOK_SECRET=... \
   python scripts/set_webhook.py
   ```
   Ответ должен содержать `"ok": true`.
6. Напишите боту `/start` — должен ответить приветствием, а на любой
   другой текст — эхом.

## Что дальше

Это только скелет: заметки, память диалога, поиск, LLM, напоминания
и мониторинг сервера будут добавляться отдельными модулями на
следующих шагах, без изменения этой базовой части.
