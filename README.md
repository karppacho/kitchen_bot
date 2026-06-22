# Kitchen Bot — MVP

AI-ассистент R&D кухни. Telegram-бот, который читает Google Sheets и помогает
с расчётами UC, маржи, поиском блюд и ингредиентов.

## Стек

- Python 3.11+
- aiogram 3.x — Telegram-бот
- OpenAI SDK (поверх polza.ai) — LLM
- gspread — Google Sheets
- pydantic — валидация данных

## Запуск — по шагам

### Шаг 1. Установить зависимости

В первый раз, в корне проекта:

```bash
# Создаём venv и ставим зависимости
python -m venv .venv
source .venv/bin/activate     # Linux/Mac
# .venv\Scripts\activate       # Windows

pip install -e .
```

Или через `uv` (быстрее, если установлен):

```bash
uv sync
```

### Шаг 2. Сделать Service Account для Google Sheets

Это разовая операция, нужна для того, чтобы Python-скрипт мог читать таблицу
без браузера и пароля.

1. Зайти на [console.cloud.google.com](https://console.cloud.google.com/).
2. Создать новый проект (любое имя, например `kitchen-bot`).
3. В меню слева: **APIs & Services → Library** — найти и **включить**:
   - Google Sheets API
   - Google Drive API
4. **APIs & Services → Credentials → Create Credentials → Service Account**:
   - Имя — любое, например `kitchen-bot-reader`.
   - Роль — не назначать (skip).
5. После создания — кликнуть на созданный аккаунт → вкладка **Keys → Add Key → Create new key → JSON**. Скачается `*.json` файл.
6. Положить файл в корень проекта и переименовать в `google_credentials.json`.

### Шаг 3. Расшарить таблицу для Service Account

В скачанном JSON есть поле `"client_email"` — что-то вроде
`kitchen-bot-reader@project-id.iam.gserviceaccount.com`.

Открыть твою Google-таблицу, нажать **Поделиться**, вставить туда этот email
с правами **«Читатель»** (Viewer). Без этого шага бот не увидит таблицу.

### Шаг 4. Заполнить `.env`

Скопировать шаблон:

```bash
cp .env.example .env
```

И заполнить значения:

- `GOOGLE_SHEETS_ID` — из URL таблицы. Например, для URL
  `https://docs.google.com/spreadsheets/d/1AbCdEf.../edit`
  это `1AbCdEf...`
- `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` — путь к `google_credentials.json` (обычно `./google_credentials.json`).
- `POLZA_API_KEY` — твой ключ polza.ai.
- `TELEGRAM_BOT_TOKEN` — из BotFather.
- `TELEGRAM_ALLOWED_USER_IDS` — через запятую: твой ID и ID друга. Узнать
  свой ID можно через [@userinfobot](https://t.me/userinfobot).

### Шаг 5. Проверка №1 — подключение к Google Sheets и калькулятор

**Это первая обязательная проверка**, чтобы убедиться, что данные читаются и
расчёт корректный. Запуск:

```bash
python -m scripts.test_calc
```

Ожидаемый вывод — таблица со всеми блюдами и их UC. Сравни цифры с тем, что
сейчас у шефа в Excel. **Если расхождение больше 1-2 рублей — стоп, разбираемся.**

Если получил ошибку про `google_credentials.json` или `403 Forbidden` —
вернись к шагу 3, проверь, что Service Account добавлен в таблицу.

### Шаг 6. Проверка №2 — LLM в REPL

```bash
python -m scripts.test_llm
```

Появится приглашение `>`. Пиши запросы, например:

- `UC чизбургера`
- `какие у нас роллы`
- `у какого блюда лучшая маржа`
- `где используется котлета`

Если бот корректно вызывает функции и отвечает осмысленно — переходим к
последнему шагу. Если что-то странно — это **самое подходящее место для отладки**,
потому что цикл «изменил промпт → проверил» здесь занимает 5 секунд.

### Шаг 7. Запуск Telegram-бота

```bash
python -m src.bot.main
```

Дальше идёшь в Telegram, открываешь своего бота (по имени, которое дал у
BotFather), нажимаешь `/start`.

Если бот молчит — посмотри логи в `logs/bot_*.log`.

## Структура проекта

```
src/
├── config.py              # настройки из .env
├── data/
│   ├── models.py          # типы данных (pydantic)
│   └── sheets.py          # чтение Google Sheets
├── calc/
│   └── costs.py           # калькулятор UC
├── llm/
│   ├── client.py          # обёртка polza.ai + tool calling
│   ├── tools.py           # описания функций
│   └── prompts/
│       └── system.md      # системный промпт
└── bot/
    └── main.py            # Telegram-бот

scripts/
├── test_calc.py           # тест калькулятора
└── test_llm.py            # REPL для LLM
```

## Что делать, если что-то не работает

**`ImportError: cannot import name 'Settings'`** — не активировался venv или не
поставлены зависимости. См. шаг 1.

**`FileNotFoundError: google_credentials.json`** — нет файла или путь в `.env`
неправильный.

**`APIError: 403 Forbidden`** — Service Account не добавлен в таблицу. Шаг 3.

**`SpreadsheetNotFound`** — неверный `GOOGLE_SHEETS_ID`. Проверь URL.

**Бот не отвечает в Telegram** — проверь, что твой `user_id` есть в
`TELEGRAM_ALLOWED_USER_IDS`.

**LLM отвечает «не нашёл блюдо»** — возможно, неверный путь к JSON или
кеш данных не обновился. Команда `/refresh` в Telegram.

## Дальше

Это MVP — он показывает, что концепция работает. Следующие шаги:

- Добавить функцию замены ингредиента (`simulate_replacement`).
- Добавить создание новых блюд через LLM (`create_dish`).
- Подключить генерацию ТТК в `.docx` (Этап 6).
- Деплой на Railway/Fly чтобы работал 24/7.
- Голосовой ввод через Whisper.
