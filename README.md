# HIP availability watcher

Воркер опрашивает API [https://api.hip.hosting/hiplet/rpc/new-options](https://api.hip.hosting/hiplet/rpc/new-options) и следит за регионами, которые были в состоянии `sold out`.

Когда регион снова становится доступен для покупки, воркер отправляет сообщение в Telegram с названием локации и краткой сводкой по доступным конфигурациям.

Первый успешный запуск только сохраняет базовое состояние в `data/state.json` и ничего не шлет в Telegram. Это нужно, чтобы не получить пачку уведомлений по уже доступным регионам.

## Переменные окружения

Обязательные:

- `TELEGRAM_BOT_TOKEN` - токен Telegram-бота.
- `TELEGRAM_CHAT_ID` - chat id, куда отправлять уведомления.

Необязательные:

- `HIP_OPTIONS_API_URL` - URL API для опроса. По умолчанию `https://api.hip.hosting/hiplet/rpc/new-options`.
- `HIP_URL` - ссылка на сайт HIP, которая добавляется в уведомление. По умолчанию `https://hip.hosting/ru`.
- `ORDER_URL` - ссылка, которую добавлять в уведомление. По умолчанию `https://my.hip.hosting/hiplets/new`.
- `CHECK_INTERVAL_SECONDS` - интервал опроса в секундах. По умолчанию `300`.
- `REQUEST_TIMEOUT_SECONDS` - HTTP timeout в секундах. По умолчанию `20`.
- `STATE_PATH` - путь к JSON-файлу состояния. По умолчанию `data/state.json`.
- `WATCHED_REGION_SLUGS` - список slug через запятую, если нужно следить не за всеми регионами, а только за частью, например `fi-1,pl-1,se-1`.
- `RUN_ONCE` - если `true`, делает одну проверку и завершает процесс.

## Локальный запуск через uv

```bash
cp .env.example .env
uv run python main.py
```

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

Состояние хранится в директории `./data`, смонтированной в контейнер как volume.

## Полезные команды

Одна проверка без бесконечного цикла:

```bash
RUN_ONCE=true uv run python main.py
```

Одна проверка в контейнере:

```bash
docker compose run --rm -e RUN_ONCE=true hip-watcher
```

## Примечание по регионам

Сайт отдает данные по slug региона, например:

- `fi-1` - Финляндия
- `pl-1` - Польша
- `se-1` - Швеция
- `fr-1` - Франция
- `nl-1` - Нидерланды
