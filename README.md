# Service API

---

### Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
if [ ! -f .env ]; then cp .env.example .env; else echo ".env уже существует, оставляем без изменений"; fi
```

---

### Генерация ключа шифрования для сессий Service
Ключ используется для шифрования cookies и данных авторизации Service перед сохранением в SQLite

- Не публиковать ключ
- Не менять его после первого входа пользователей
- Сохранить резервную копию
- Если ключ потерять, сохранённые сессии нельзя будет расшифровать, и пользователям придётся войти заново

---

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Запишите ключ в `.env` и запустите API:

```bash
set -a
source .env
set +a
uvicorn service_api.app:app --host 127.0.0.1 --port 8000
```

Проверка:

```bash
curl http://127.0.0.1:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok"}
```


## Railway Setup

---

### 1. Создать сервис

    1. Загрузите проект в GitHub.
    2. В Railway выберите `New Project` -> `Deploy from GitHub repo`.
    3. Выберите репозиторий. Railway использует `Dockerfile` и `railway.toml`.

---

### 2. Добавить переменные

В Railway откройте `Variables` и добавьте:

```env
    SERVICE_ENV=production
    SERVICE_DB_PATH=/app/data/service.sqlite3
    SERVICE_BASE_URL=https://service.example.com
    SERVICE_SESSION_ENCRYPTION_KEY=<стабильный-Fernet-ключ>
    SERVICE_HEADLESS=1
    SERVICE_SYNC_TIMEOUT_SECONDS=300
    SERVICE_AUTH_CHALLENGE_TTL_SECONDS=300
    SERVICE_AUTH_REQUESTS_PER_10_MINUTES=3
    SERVICE_AUTH_MAX_ATTEMPTS=5
    SERVICE_API_TOKEN_TTL_SECONDS=2592000
    SERVICE_TIMEZONE=Europe/Moscow
    CHAT_AMOUNT_LIMIT=20
    MESSAGE_AMOUNT_LIMIT=50
    SERVICE_SQLITE_TIMEOUT_SECONDS=30
```

### Описание переменных

| Переменная | Назначение |
| --- | --- |
| `SERVICE_ENV` | Режим запуска приложения. `production` включает production-проверки. |
| `SERVICE_DB_PATH` | Путь к SQLite-базе внутри контейнера. Каталог `/app/data` подключён к Volume. |
| `SERVICE_BASE_URL` | Базовый HTTPS-адрес внешнего сервиса для авторизации и синхронизации. |
| `SERVICE_SESSION_ENCRYPTION_KEY` | Секретный Fernet-ключ для шифрования сессий Service. |
| `SERVICE_HEADLESS` | Запуск браузера без графического окна. Значение `1` нужно для Railway. |
| `SERVICE_SYNC_TIMEOUT_SECONDS` | Максимальная длительность одной синхронизации в секундах. |
| `SERVICE_AUTH_CHALLENGE_TTL_SECONDS` | Срок действия запроса SMS-кода в секундах. |
| `SERVICE_AUTH_REQUESTS_PER_10_MINUTES` | Максимальное количество запросов SMS-кода за 10 минут для одного номера. `0` отключает ограничение. |
| `SERVICE_AUTH_MAX_ATTEMPTS` | Максимальное количество попыток ввода SMS-кода. |
| `SERVICE_API_TOKEN_TTL_SECONDS` | Срок жизни Bearer-токена в секундах. По умолчанию 30 дней. |
| `SERVICE_TIMEZONE` | Часовой пояс для преобразования дат сообщений. |
| `CHAT_AMOUNT_LIMIT` | Максимальное количество загружаемых чатов. Значение `0` загружает все чаты. |
| `MESSAGE_AMOUNT_LIMIT` | Максимальное количество сообщений в каждом чате. Значение `0` загружает все сообщения. |
| `SERVICE_SQLITE_TIMEOUT_SECONDS` | Максимальное время ожидания освобождения SQLite-блокировки в секундах. |


### 3. Подключение Volume

Добавьте Railway Volume и укажите путь монтирования:

```text
/app/data
```

Volume нужен для сохранения SQLite после деплоя. Используйте одну реплику и
один worker.

---

### 4. Проверка API

После деплоя Railway выдаст HTTPS-домен:

```bash
curl https://<railway-домен>/health
```

Приложение автоматически слушает `0.0.0.0:$PORT`. Текущая конфигурация
запускает один Uvicorn worker, потому что challenge входа хранится в памяти.

---

## Endpoints

| Метод | Endpoint | Назначение |
| --- | --- | --- |
| `GET` | `/health` | Проверка доступности |
| `POST` | `/auth/request-code` | Запросить SMS-код |
| `POST` | `/auth/verify-code` | Получить Bearer-токен |
| `GET` | `/me` | Текущий пользователь |
| `GET` | `/chats` | Список чатов |
| `GET` | `/chats/{chat_id}/messages` | Сообщения чата |
| `POST` | `/sync` | Запустить синхронизацию с лимитами из JSON-тела |
| `GET` | `/sync/status` | Статус синхронизации |
| `POST` | `/auth/logout` | Отозвать текущий токен |

Защищённые endpoints используют заголовок:

```http
Authorization: Bearer <access_token>
```

Параметр `limit` у `/chats` и `/chats/{chat_id}/messages` ограничивается
переменными `CHAT_AMOUNT_LIMIT` и `MESSAGE_AMOUNT_LIMIT`. Значение `0`
отключает ограничение, если `limit` не передан в запросе.

Для запуска синхронизации приложение может передать лимиты в JSON-теле:

```json
{
  "chat_amount_limit": 20,
  "message_amount_limit": 50
}
```

Оба поля необязательны. Если поле не передано, используется значение из
окружения. Значение `0` загружает все чаты или все сообщения в каждом чате.

## Коды ошибок

Ошибки API возвращаются в JSON-формате:

```json
{
  "detail": "Описание ошибки"
}
```

| HTTP-код | Значение | Где возникает |
| --- | --- | --- |
| `400` | Некорректный или просроченный challenge, неверный SMS-код | `/auth/verify-code` |
| `401` | Отсутствует, просрочен или недействителен Bearer-токен | Все защищённые endpoints |
| `404` | Чат или endpoint не найден | `/chats/{chat_id}/messages`, неизвестный путь |
| `405` | Использован неверный HTTP-метод | Любой endpoint |
| `409` | Синхронизация уже выполняется или требуется повторная авторизация в Service | `/sync` |
| `422` | Ошибка валидации входных данных: телефон, SMS-код, `limit`, `offset` или `sync_run_id` | Все endpoints с параметрами или JSON-телом |
| `429` | Превышен лимит запросов SMS-кода или попыток ввода | `/auth/request-code`, `/auth/verify-code` |
| `503` | Недоступен внешний сервис авторизации Service или не удалось запустить браузер | `/auth/request-code` |
| `500` | Непредвиденная ошибка сервера | Любой endpoint |

Для ошибок валидации FastAPI может вернуть список деталей:

```json
{
  "detail": [
    {
      "loc": ["body", "phone"],
      "msg": "String should have at least 10 characters",
      "type": "string_too_short"
    }
  ]
}
```

Код `409` от `/sync` может содержать не поле `detail`, а статус операции:

```json
{
  "status": "already_running"
}
```

---

## Структура

- `service_api/` — FastAPI, авторизация, база и синхронизация;
- `service_scraper/` — Scrapy spider;
- `Dockerfile` — production-образ с Chromium;
- `compose.yaml` — production-запуск на собственной VM;
- `railway.toml` — настройки сборки и health-check;
- `.env.example` — пример переменных окружения.
