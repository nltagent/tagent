# telegram-agent

## Шаг 1: скелет

Минимальный работающий каркас: вебхук от Telegram → эхо-ответ.
Цель этого шага — проверить всю цепочку целиком (Telegram → Railway
→ контейнер → обратно в Telegram), прежде чем добавлять модули.

## Шаг 2: SQLite — заметки, память агента, история диалога

Добавлено:
- **Заметки** (`modules/notes`) — `/note`, `/notes`, `/delnote <id>`.
- **Самопамять агента** (`modules/memory/self_memory.py`) — факты вида
  "меня зовут Джарвис", которые видны модели в любом диалоге:
  `/remember ключ=значение`, `/memory`, `/forget ключ`. Плюс утилита
  `extract_remember_tags()` — парсит теги `[REMEMBER: key=value]` из
  ответов LLM (будет использована на шаге с LLM-модулем).
- **История диалога** (`modules/memory/history.py`) — каждое
  сообщение пишется в SQLite навсегда; `/history` показывает
  последние. Отдельно ведётся "живая" (неархивированная) часть,
  которая пойдёт в контекст LLM.
- **Компактор** (`modules/memory/compactor.py`) — когда "живая"
  история превышает токен-бюджет (`HISTORY_TOKEN_BUDGET`, по
  умолчанию 3000), самая старая часть (кроме последних
  `HISTORY_KEEP_LAST` сообщений) сворачивается в summary через
  callback `summarize_fn`. Сама функция суммаризации подключится на
  шаге с LLM — здесь только логика "когда и что сжимать", проверено
  тестами на синтетических данных.

Все таблицы создаются автоматически при первом запуске
(`storage/db.py`), файл БД — по пути `DB_PATH` (по умолчанию
`/data/agent.db` — не забудьте примонтировать Railway Volume на
`/data`, см. переменную ниже).

Пока эхо-ответ на обычный текст остаётся как есть (записывается в
историю, но реального LLM ещё нет) — это шаг 3.

## Шаг 3: LLM + поиск

Добавлено:
- **`llm/client.py`** — низкоуровневый клиент к любому OpenAI-совместимому
  Chat Completions API (OpenRouter, clavis.to и т.п. — один и тот же
  код, меняются только `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`).
  Уважает лимит запросов (`LLM_MAX_PER_MINUTE`, `LLM_MIN_INTERVAL`),
  один retry по 429/Retry-After, логирует потребление токенов в
  `usage_log` (команда `/usage`).
- **`llm/orchestrator.py`** — обычный текст теперь идёт в модель, а не
  эхо. Собирает system-prompt (базовые инструкции + факты из
  `self_memory`), summary + активную историю диалога, вызывает LLM.
  Если модель решает, что нужен интернет — она отвечает тегом
  `[SEARCH: запрос]`; система сама выполняет поиск через Keenable,
  подставляет результаты и делает второй вызов за финальным ответом.
  Теги `[REMEMBER: ключ=значение]` в ответе модели вырезаются и
  сохраняются в `self_memory` автоматически.
- **`modules/search/service.py`** — поиск через Keenable с лимитом
  `KEENABLE_MIN_INTERVAL` (по умолчанию 1 запрос / 0.5 сек). Команда
  `/search запрос` — прямой поиск в обход LLM, для проверки/быстрых
  справок.
- Компактор (шаг 2) теперь подключён к реальной суммаризации —
  `llm.orchestrator.summarize_history` вызывается автоматически, когда
  история превышает токен-бюджет.

**Важно — одно место, требующее проверки перед первым реальным запуском:**
`LLM_BASE_URL` для clavis.to — я не нашёл подтверждённого публичного
адреса их API, уточните в личном кабинете/документации и впишите в
`.env`. Для OpenRouter значение по умолчанию (`https://openrouter.ai/api/v1`)
верно.

Обе переменные (`LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`) обязательны
— без них приложение не запустится (упадёт с понятной ошибкой
конфигурации при старте, а не тихо где-то в середине работы).

## Поиск: два провайдера, переключение без передеплоя

`modules/search/service.py` — тонкий диспетчер, который прячет от
остального кода, какой провайдер сейчас активен:
- **`keenable`** (`modules/search/providers/keenable.py`) — платный,
  но простой в настройке. ВАЖНО: вопреки формулировкам в документации
  Keenable про "keyless"-режим, реальный REST-эндпоинт требует
  заголовок `X-API-Key` на **каждый** запрос — подтверждено вручную
  curl'ом (без ключа ответ `"Missing API key"`). Получите ключ на
  https://keenable.ai/console.
- **`searxng`** (`modules/search/providers/searxng.py`) — свой
  self-hosted инстанс SearxNG, полностью бесплатно. Обязательно
  включите `json` в `search.formats` в `settings.yml` вашего
  инстанса, иначе получите 403.

Переключение:
- `SEARCH_PROVIDER` в `.env` — что использовать при старте контейнера;
- команда `/setsearch <keenable|searxng>` — переключает на лету, без
  передеплоя, сохраняется в SQLite (таблица `settings`) и переживает
  рестарт. `/setsearch` без аргумента показывает текущий провайдер.

Добавить третий провайдер (например другой платный API) — написать
`modules/search/providers/новый.py` с функцией
`search(query, max_results, **kwargs) -> list[dict]` в общем формате
`{"title", "url", "snippet"}` и добавить одну строку в `_PROVIDERS`
внутри `service.py`. Остального кода (router, llm.orchestrator) это
не касается.

## Деплой SearxNG как отдельного сервиса на Railway

SearxNG — это отдельный, независимый контейнер, не часть образа бота.
Нужен ещё один Railway-сервис в том же проекте:

1. `searxng/Dockerfile` и `searxng/settings.yml` уже в репозитории.
   Откройте `searxng/settings.yml` и замените `secret_key` на
   случайную строку: `openssl rand -hex 32`.
2. Railway → New → Empty Service (в том же проекте, где уже крутится
   бот) → Settings → Source: подключите тот же репозиторий → Build:
   Dockerfile Path = `searxng/Dockerfile`, Build Context = корень репо.
3. Settings → Networking → задайте Target Port = `8080` (SearxNG
   слушает этот порт по умолчанию). Публичный домен генерировать не
   обязательно — бот будет достукиваться по приватной сети.
4. После деплоя узнайте имя сервиса (по умолчанию совпадает с именем,
   которое вы дали при создании, например `searxng`) — внутренний
   адрес будет `http://<имя-сервиса>.railway.internal:8080` (именно
   `http`, не `https` — трафик внутри приватной сети Railway).
5. В переменных сервиса **бота** (не SearxNG!) задайте:
   ```
   SEARXNG_BASE_URL=http://<имя-сервиса>.railway.internal:8080
   ```
   и передеплойте бота (или просто перезапустите — Railway подхватит
   новую переменную).
6. Проверьте: `/setsearch searxng`, затем `/search тест`.

Быстрая проверка самого SearxNG в отдельности, до подключения бота
(с публичным доменом, если временно его включили):
```bash
curl -s "https://<публичный-домен-searxng>/search?q=test&format=json" | head -50
```
Если вернулся HTML вместо JSON — значит `settings.yml` не применился
(проверьте, что `COPY settings.yml` действительно попал в образ —
пересоберите сервис) или формат всё ещё не включён.

## Проверка обоих контейнеров в Codespaces (без Railway)

Для этого — `docker-compose.yml` в корне репозитория: поднимает бота
и SearxNG вместе, в одной docker-сети, где они видят друг друга по
имени сервиса (`http://searxng:8080`) — Railway-домены тут ни при чём,
это отдельный, локальный способ проверки.

1. Заполните `.env` реальными значениями (`TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_WEBHOOK_SECRET`, `OWNER_CHAT_ID`, `LLM_API_KEY`,
   `LLM_BASE_URL`, `LLM_MODEL`). `SEARXNG_BASE_URL` можно не трогать —
   compose сам подставит `http://searxng:8080`.
2. В терминале Codespaces:
   ```bash
   docker compose up --build
   ```
   Соберутся оба образа, поднимутся два контейнера в одной сети.

   Если получите `Bind for 0.0.0.0:XXXX failed: port is already allocated` —
   значит остались контейнеры от предыдущего запуска. Почистить:
   ```bash
   docker compose down
   # если не помогло — найти виновника и убрать вручную:
   docker ps -a --format "table {{.ID}}\t{{.Names}}\t{{.Ports}}"
   docker rm -f <ID>
   ```
3. **Проверить SearxNG напрямую**, в обход бота (в отдельном
   терминале Codespaces, порт 18081 проброшен наружу через compose):
   ```bash
   curl -s "http://localhost:18081/search?q=test&format=json" | head -50
   ```
   Должен вернуться JSON. Если HTML или 403 — проблема в самом
   SearxNG (см. пункт выше), к боту это отношения не имеет.
4. **Проверить бота целиком**, включая обращение к SearxNG изнутри
   его контейнера — так же, как раньше отлаживали вебхук, curl'ом
   прямо на `/webhook` (реальный вызов до Telegram по-настоящему
   регистрировать не нужно — это симулирует то, что прислал бы
   Telegram, а `sendMessage` внутри бота реально уйдёт в Telegram API,
   так что ответ придёт вам в чат по-настоящему):
   ```bash
   curl -s -X POST http://localhost:18080/webhook \
     -H "X-Telegram-Bot-Api-Secret-Token: <ваш TELEGRAM_WEBHOOK_SECRET>" \
     -d '{"message":{"chat":{"id":<ваш OWNER_CHAT_ID>},"text":"/setsearch searxng"}}'

   curl -s -X POST http://localhost:18080/webhook \
     -H "X-Telegram-Bot-Api-Secret-Token: <ваш TELEGRAM_WEBHOOK_SECRET>" \
     -d '{"message":{"chat":{"id":<ваш OWNER_CHAT_ID>},"text":"/search тест"}}'
   ```
   Если всё настроено верно — в вашем Telegram-чате с ботом появится
   реальный ответ с результатами поиска.
5. Логи обоих контейнеров видно прямо в терминале, где выполнили
   `docker compose up` (либо `docker compose logs -f bot` /
   `docker compose logs -f searxng` в отдельном терминале).
6. Остановить: `Ctrl+C`, затем при необходимости `docker compose down`
   (данные в `bot-data`-volume переживут остановку, `down -v` их
   сотрёт).

Когда всё проверено в Codespaces — на Railway разворачиваете как и
раньше: бот и SearxNG отдельными сервисами, связь через
`*.railway.internal` (см. предыдущий раздел). `docker-compose.yml`
на Railway не используется и не нужен.

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

## Шаг 4: напоминания + мониторинг сервера (Railway Cron Job)

Добавлено:
- **Напоминания** (`modules/reminders`) — `/remind <когда> <текст>`,
  `/reminders`, `/delremind <id>`. Разбор времени —
  `modules/reminders/timeparse.py`, свой лёгкий парсер под несколько
  русских форматов (без сторонних библиотек вроде dateutil):
  `через N минут/часов/дней`, `завтра в HH:MM`, `сегодня в HH:MM`,
  `HH:MM`, `ГГГГ-ММ-ДД ЧЧ:ММ`. Часовой пояс — `USER_TIMEZONE`
  (stdlib `zoneinfo`, с учётом перехода на летнее/зимнее время).
- **Мониторинг** (`modules/monitoring`) — память/нагрузка/диск через
  `/proc/meminfo`, `os.getloadavg()`, `shutil.disk_usage()` — без
  psutil. Команда `/status` — отчёт по требованию.
- **`scheduler.py`** — то, что выполняется на каждый тик Railway Cron
  Job: доставляет просроченные напоминания (нужно проверять часто) и
  не чаще раза в `MONITORING_REPORT_INTERVAL_HOURS` шлёт отчёт о
  сервере (большинство тиков его пропустят — не нужно так часто).
- **`/internal/cron`** в `main.py` — эндпоинт, который дёргает Railway
  Cron Job. Отдельный секрет `CRON_SECRET` (не тот же, что у вебхука
  Telegram).
- Единственная новая зависимость — `tzdata` (requirements.txt):
  stdlib `zoneinfo` нужна IANA tz база, которой может не быть в
  `python:3.12-slim`; `tzdata` — официальный pure-Python пакет именно
  для этого случая, без скомпилированных зависимостей.

### Настройка Railway Cron Job

Нужен третий сервис в том же Railway-проекте (бот и SearxNG — уже два):

1. Сгенерируйте секрет: `openssl rand -hex 32` → впишите в переменную
   `CRON_SECRET` **сервиса бота** (и передеплойте бота).
2. Railway → New → **Cron Job** (не Empty Service — именно тип Cron
   Job, это отдельный вариант в меню создания сервиса).
3. Command:
   ```bash
   curl -sf -X POST http://<имя-сервиса-бота>.railway.internal:<PORT>/internal/cron \
     -H "X-Cron-Secret: <тот же CRON_SECRET>"
   ```
   `<PORT>` — тот же порт, что слушает бот (`config.PORT`, обычно то,
   что подставляет сам Railway — посмотрите в переменных сервиса бота).
4. Schedule: например `*/10 * * * *` (раз в 10 минут — компромисс
   между своевременностью напоминаний и частотой пробуждения
   контейнера бота из Serverless-сна). Минимальная частота у Railway
   Cron — раз в 5 минут.
5. Проверить вручную: `/remind через 1 минуту тест`, подождать тик
   cron — должно прийти сообщение.

Локально/в Codespaces тот же самый curl можно погонять руками (с
поправкой на `localhost:18080` вместо Railway-домена, и заголовком
`X-Cron-Secret` вместо `X-Telegram-Bot-Api-Secret-Token`), не дожидаясь
реального расписания:
```bash
curl -s -X POST http://localhost:18080/internal/cron \
  -H "X-Cron-Secret: <ваш CRON_SECRET>"
```
