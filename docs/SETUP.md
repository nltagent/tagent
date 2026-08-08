# Развёртывание

Этот документ покрывает все способы запустить проект: локально для
проверки, в Codespaces для полноценной отладки (бот + SearxNG вместе),
и в проде на Railway (три сервиса: бот, SearxNG, Cron Job).

Полный список переменных окружения — в `.env.example`, здесь только
то, что нужно понять концептуально, не построчный разбор.

## Локальный запуск (без Railway, только проверить, что сервер стартует)

```bash
cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID, PUBLIC_URL
export $(cat .env | grep -v '^#' | xargs)
cd src && python main.py
```

Проверить, что жив:
```bash
curl http://localhost:8080/health
```

Локально Telegram не сможет достучаться до вашего компьютера напрямую
(нет реального публичного URL) — полноценная проверка вебхука
делается уже после деплоя. Для чисто локальной отладки логики можно
вручную дёрнуть `/webhook` через curl, подставив свой
`TELEGRAM_WEBHOOK_SECRET` (см. ниже — можно не задавать вручную) и
тело апдейта в формате Telegram Bot API.

## Минимальный набор переменных

По-настоящему обязательны только четыре: `TELEGRAM_BOT_TOKEN`,
`OWNER_CHAT_ID`, `PUBLIC_URL` — без них бот не может стартовать и
представиться Telegram. Если какой-то из них не хватает, контейнер
сразу упадёт при старте с понятным сообщением вида `Не задана
обязательная переменная окружения: OWNER_CHAT_ID` — это видно в логах
деплоя, а не тонет где-то в рантайме.

Всё остальное — необязательно:
- **`TELEGRAM_WEBHOOK_SECRET`, `CRON_SECRET`** — если не заданы, бот
  сам сгенерирует случайное значение при первом старте и сохранит
  в SQLite (переживает рестарты, пока жив `DB_PATH`/Volume). Текущий
  `CRON_SECRET` можно посмотреть командой `/cronsecret` (только
  создатель) — он нужен, чтобы вписать его в переменные окружения
  отдельного Cron Job сервиса.
- **`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`** — это только профиль
  `default` для владельца бота, короткий путь для тех, кто предпочитает
  один ключ через переменные окружения. Можно вообще не задавать и
  сразу после `/start` выполнить `/addprovider имя url ключ` прямо в
  Telegram — тогда ключ никогда не попадает в переменные окружения
  хостинга вовсе.
- Регистрация вебхука у Telegram теперь происходит автоматически при
  каждом старте контейнера (см. `main.py: _register_webhook()`) — за
  счёт `PUBLIC_URL`, который уже обязателен. Ручной запуск
  `scripts/set_webhook.py` остаётся, но нужен только для отладки —
  например, если хочется перерегистрировать вебхук, не передеплоивая
  контейнер целиком.

## Деплой бота на Railway

1. Создайте новый проект на Railway, подключите этот репозиторий.
2. **Важно:** Dockerfile лежит в `docker/Dockerfile`, а не в корне.
   В настройках сервиса (Settings → Build) укажите:
   - Dockerfile Path: `docker/Dockerfile`
   - Build Context: корень репозитория (`.`)

   Dockerfile ссылается на `requirements.txt` и `src/` от корня
   контекста, так что билд-контекст должен быть корнем репо, а не
   `docker/`.
3. Settings → Networking → Generate Domain — получите публичный URL
   вида `https://<name>.up.railway.app`.
4. Variables — задайте обязательные переменные: `TELEGRAM_BOT_TOKEN`,
   `OWNER_CHAT_ID`, `PUBLIC_URL` (тот самый домен из шага 3). `PORT`
   Railway подставит сам. Остальное — по желанию (см. выше).
5. Settings → Volumes — примонтируйте том на `/data` (путь из
   `DB_PATH`), иначе заметки/история/напоминания будут слетать при
   каждом передеплое — а заодно и авто-сгенерированные
   `TELEGRAM_WEBHOOK_SECRET`/`CRON_SECRET`, если их не задать явно.
6. Деплой — при старте бот сам зарегистрирует вебхук через
   `PUBLIC_URL`, дополнительный шаг не нужен. Если хочется убедиться
   вручную (или перерегистрировать после смены домена):
   ```bash
   PUBLIC_URL=https://<name>.up.railway.app \
   TELEGRAM_BOT_TOKEN=... \
   TELEGRAM_WEBHOOK_SECRET=... \
   python scripts/set_webhook.py
   ```
   Ответ должен содержать `"ok": true`.
7. Напишите боту `/start`, затем `/addprovider имя url ключ`, если не
   задавали `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` — список команд:
   `/help`.
8. (Необязательно) Меню команд рядом с полем ввода в Telegram:
   ```bash
   TELEGRAM_BOT_TOKEN=... python scripts/set_commands.py
   ```
   Разово, при первой настройке или когда меняете список команд в
   `scripts/set_commands.py` — не на каждый деплой.

## Деплой SearxNG как отдельного сервиса на Railway

SearxNG — независимый контейнер, не часть образа бота. Это **второй**
сервис в том же Railway-проекте:

1. `searxng/Dockerfile` и `searxng/settings.yml` уже в репозитории.
   Откройте `searxng/settings.yml` и замените `secret_key` на
   случайную строку: `openssl rand -hex 32`.
2. Railway → New → Empty Service (в том же проекте) → Settings →
   Source: подключите тот же репозиторий → Build: Dockerfile Path =
   `searxng/Dockerfile`, Build Context = корень репо.
3. Settings → Networking → задайте Target Port = `8080` (SearxNG
   слушает этот порт по умолчанию). Публичный домен генерировать не
   обязательно — бот будет достукиваться по приватной сети.
4. После деплоя внутренний адрес будет
   `http://<имя-сервиса>.railway.internal:8080` (именно `http`, не
   `https` — трафик внутри приватной сети Railway).
5. В переменных сервиса **бота** (не SearxNG!) задайте:
   ```
   SEARXNG_BASE_URL=http://<имя-сервиса>.railway.internal:8080
   ```
   и передеплойте/перезапустите бота.
6. Проверьте: `/setsearch searxng`, затем `/search тест`.

Быстрая проверка самого SearxNG в отдельности, до подключения бота
(с публичным доменом, если временно его включили):
```bash
curl -s "https://<публичный-домен-searxng>/search?q=test&format=json" | head -50
```
Если вернулся HTML вместо JSON — значит `settings.yml` не применился
(проверьте, что `COPY settings.yml` действительно попал в образ —
пересоберите сервис) или формат всё ещё не включён.

## Настройка Railway Cron Job (напоминания + мониторинг)

Нужен **третий** сервис в том же Railway-проекте (бот и SearxNG — уже
два):

1. Узнайте текущий `CRON_SECRET`: напишите боту `/cronsecret` (только
   создатель) — если переменную `CRON_SECRET` не задавали явно, бот
   сам сгенерировал и сохранил её при первом старте, команда покажет
   актуальное значение. Хотите задать вручную вместо авто-генерации —
   впишите свой `openssl rand -hex 32` в переменную `CRON_SECRET`
   **сервиса бота** и передеплойте бота.
2. Railway → New → **Cron Job** (не Empty Service — именно тип Cron
   Job, это отдельный вариант в меню создания сервиса).
3. Command:
   ```bash
   curl -sf -X POST http://<имя-сервиса-бота>.railway.internal:<PORT>/internal/cron \
     -H "X-Cron-Secret: <тот же CRON_SECRET>"
   ```
   `<PORT>` — тот же порт, что слушает бот (посмотрите в переменных
   сервиса бота).
4. Schedule: например `*/10 * * * *` (раз в 10 минут — компромисс
   между своевременностью напоминаний и частотой пробуждения
   контейнера бота из Serverless-сна). Минимальная частота у Railway
   Cron — раз в 5 минут.
5. Проверить вручную: `/remind через 1 минуту тест`, подождать тик
   cron — должно прийти сообщение.

## Проверка бота + SearxNG вместе в Codespaces (без Railway)

`docker-compose.yml` в корне репозитория поднимает бота и SearxNG
вместе, в одной docker-сети, где они видят друг друга по имени
сервиса (`http://searxng:8080`) — Railway-домены тут ни при чём, это
отдельный, локальный способ проверки перед реальным деплоем.

1. Заполните `.env` минимум `TELEGRAM_BOT_TOKEN`, `OWNER_CHAT_ID`,
   `PUBLIC_URL` (для локальной проверки подойдёт заглушка вида
   `http://localhost:8080` — просто чтобы конфиг загрузился, вебхук
   всё равно локально не зарегистрируется по-настоящему). Остальное
   (`TELEGRAM_WEBHOOK_SECRET`, `CRON_SECRET`, `LLM_API_KEY`/
   `LLM_BASE_URL`/`LLM_MODEL`) можно не трогать — см. «Минимальный
   набор переменных» выше. `SEARXNG_BASE_URL` тоже можно не трогать —
   compose сам подставит `http://searxng:8080`.
2. В терминале Codespaces:
   ```bash
   docker compose up --build
   ```
   Соберутся оба образа, поднимутся два контейнера в одной сети.

   Если получите `Bind for 0.0.0.0:XXXX failed: port is already allocated`
   — значит остались контейнеры от предыдущего запуска:
   ```bash
   docker compose down
   # если не помогло — найти виновника и убрать вручную:
   docker ps -a --format "table {{.ID}}\t{{.Names}}\t{{.Ports}}"
   docker rm -f <ID>
   ```
3. **Проверить SearxNG напрямую**, в обход бота (порт 18081
   проброшен наружу через compose):
   ```bash
   curl -s "http://localhost:18081/search?q=test&format=json" | head -50
   ```
   Должен вернуться JSON. Если HTML или 403 — проблема в самом
   SearxNG, к боту это отношения не имеет.
4. **Проверить бота целиком**, включая обращение к SearxNG изнутри
   его контейнера — curl'ом прямо на `/webhook` (реальный вебхук у
   Telegram регистрировать не нужно — это симулирует то, что прислал
   бы Telegram, а `sendMessage` внутри бота реально уйдёт в Telegram
   API, так что ответ придёт вам в чат по-настоящему):
   ```bash
   curl -s -X POST http://localhost:18080/webhook \
     -H "X-Telegram-Bot-Api-Secret-Token: <ваш TELEGRAM_WEBHOOK_SECRET>" \
     -d '{"message":{"chat":{"id":<ваш OWNER_CHAT_ID>},"text":"/setsearch searxng"}}'

   curl -s -X POST http://localhost:18080/webhook \
     -H "X-Telegram-Bot-Api-Secret-Token: <ваш TELEGRAM_WEBHOOK_SECRET>" \
     -d '{"message":{"chat":{"id":<ваш OWNER_CHAT_ID>},"text":"/search тест"}}'
   ```
5. Логи обоих контейнеров — в терминале, где выполнили
   `docker compose up` (либо `docker compose logs -f bot` /
   `docker compose logs -f searxng` в отдельном терминале).
6. Остановить: `Ctrl+C`, затем при необходимости `docker compose down`
   (данные в `bot-data`-volume переживут остановку, `down -v` их
   сотрёт).

Тот же приём (curl на `/internal/cron` с заголовком `X-Cron-Secret`
вместо `X-Telegram-Bot-Api-Secret-Token`) работает и для проверки
cron-эндпоинта локально, не дожидаясь реального расписания:
```bash
curl -s -X POST http://localhost:18080/internal/cron \
  -H "X-Cron-Secret: <ваш CRON_SECRET>"
```

Когда всё проверено в Codespaces — на Railway разворачивается так же,
как описано выше: три отдельных сервиса, связь через
`*.railway.internal`. `docker-compose.yml` на Railway не используется.

## Особые случаи, которые уже встречались

- **`Permission denied` при создании `/data`** — Dockerfile
  намеренно не переключается на непривилегированного пользователя
  (см. `docs/ARCHITECTURE.md`) именно из-за этого — Railway монтирует
  Volume во время запуска контейнера, обычно с правами root.
- **Keenable без ключа не работает**, хотя документация называет
  режим "keyless" — реальный REST API требует `X-API-Key` на каждый
  запрос. Получите ключ на https://keenable.ai/console.
- **SearxNG отвечает 403 или HTML вместо JSON** — не включён `json` в
  `search.formats` в `settings.yml` вашего инстанса.
