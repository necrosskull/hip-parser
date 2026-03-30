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

## CI/CD в GitHub (автодеплой при коммите в `main`)

В репозитории добавлен workflow [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).

Триггеры:

- push в ветку `main`
- ручной запуск через `workflow_dispatch`

Что делает деплой:

1. Подключается к серверу по SSH.
2. Переходит в директорию проекта.
3. Обновляет код до `origin/main`.
4. Выполняет `docker compose up -d --build --remove-orphans`.

### Что настроить в GitHub

В `Settings -> Secrets and variables -> Actions` добавьте secrets:

- `SSH_HOST` — IP/домен сервера.
- `SSH_PORT` — SSH-порт (обычно `22`).
- `SSH_USER` — пользователь на сервере.
- `SSH_PRIVATE_KEY` — приватный SSH-ключ (которым GitHub Actions подключится на сервер).
- `DEPLOY_PATH` — абсолютный путь к директории проекта на сервере, например `/opt/hip-parser`.
- `ENV_FILE_B64` — содержимое `.env` в base64 (workflow декодирует его на сервере в файл `.env` перед запуском `docker compose`).

Как подготовить `ENV_FILE_B64`:

Linux/macOS:

```bash
base64 -w 0 .env
```

PowerShell (Windows):

```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content .env -Raw)))
```

### Что настроить на сервере

1. Установить Docker и Docker Compose plugin.
2. Создать директорию проекта и клонировать репозиторий:

```bash
sudo mkdir -p /opt/hip-parser
sudo chown -R $USER:$USER /opt/hip-parser
git clone https://github.com/necrosskull/hip-parser /opt/hip-parser
cd /opt/hip-parser
```

3. `.env` вручную создавать не обязательно: workflow сам создаёт его из `ENV_FILE_B64` при каждом деплое.

Если нужно проверить запуск до первого деплоя, можно временно сделать вручную:

```bash
cp .env.example .env
nano .env
```

4. Создать директорию для состояния (если отсутствует):

```bash
mkdir -p data
```

5. Проверить первый запуск вручную:

```bash
docker compose up -d --build
docker compose logs -f --tail=100 hip-watcher
```

6. Добавить публичный ключ для `SSH_PRIVATE_KEY` в `~/.ssh/authorized_keys` пользователя `SSH_USER`.

После этого каждый commit/push в `main` будет автоматически деплоить новую версию на сервер.

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
