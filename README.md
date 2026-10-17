# AI Report Bot

Telegram-бот, который делает из сырых данных аккуратные PDF-документы с помощью Claude Haiku 5.5.

| Режим | Что присылает пользователь | Что получает |
|---|---|---|
| `/report` | транскрибацию диалога с клиентом (текстом или `.txt`) | PDF «Отчёт по клиентской консультации» + краткая сводка в чате |
| `/product` | ссылку на товар | PDF «Карточка товара» с фото, ценой, описанием и характеристиками |

Если сайт блокирует scraping (403/429, капча, требуется JavaScript, разметка не распознана), бот не падает,
а предлагает заполнить карточку вручную: название → описание → цена и валюта → характеристики → фото.

## Как это работает

```
/report:  текст → Claude (строгий JSON) → Pydantic → data/json/*.json → Jinja2 → WeasyPrint → reports/*.pdf → Telegram
/product: URL → requests + BeautifulSoup ─┬─ данные есть ───────────────┐
                                          └─ блокировка → ручной ввод ──┴→ Claude → Pydantic → PDF → Telegram
```

- Ответ модели валидируется Pydantic. Если JSON невалиден, бот повторяет запрос до 2 раз
  с уточняющим системным сообщением; если не помогло — понятная ошибка пользователю, детали в лог.
- Фото товара скачивается заранее и встраивается в PDF как data URI. Если скачать не вышло,
  в PDF будет аккуратный placeholder «Фото товара не предоставлено».
- Бот не ходит по ссылкам на `localhost` и внутренние сети (защита от SSRF), а WeasyPrint
  не загружает внешние ресурсы при вёрстке.

## Структура

```text
ai_report_bot/
├── bot.py                     # Telegram-хендлеры и ConversationHandler
├── config.py                  # пути и переменные окружения
├── utils/
│   ├── ai_processor.py        # Anthropic Messages API, промпты, повторы при невалидном JSON
│   ├── scraper.py             # scraping товара, распознавание блокировок, загрузка фото
│   ├── pdf_generator.py       # Jinja2 → HTML → WeasyPrint → PDF
│   └── validators.py          # Pydantic-модели, разбор JSON, сохранение файлов
├── templates/                 # HTML-шаблоны A4
├── data/json/                 # сохранённые JSON-ответы (в git не попадают)
├── reports/                   # готовые PDF (в git не попадают)
├── logs/bot.log               # лог с ротацией (создаётся при запуске)
├── examples/example_dialog.txt
└── tests/
```

## 1. Создайте Telegram-бота через BotFather

1. Откройте в Telegram [@BotFather](https://t.me/BotFather) и отправьте `/newbot`.
2. Введите отображаемое имя бота, например `AI Report Bot`.
3. Введите username, который заканчивается на `bot`, например `my_ai_report_bot`.
4. BotFather пришлёт токен вида `123456789:AAH...` — это `TELEGRAM_BOT_TOKEN`. Никому его не показывайте.

Меню команд (`/report`, `/product`, `/cancel`, `/help`) бот зарегистрирует сам при запуске.

## 2. Получите ключ Anthropic

1. Зайдите в [Anthropic Console](https://console.anthropic.com/) и войдите в аккаунт.
2. В разделе **Billing** пополните баланс (без него API отвечает ошибкой).
3. В разделе **API Keys** нажмите **Create Key** и скопируйте ключ `sk-ant-...` — это `ANTHROPIC_API_KEY`.
   Ключ показывается один раз.

## 3. Установите системные библиотеки для WeasyPrint

WeasyPrint использует библиотеку Pango, её нужно поставить отдельно от Python-пакетов:

- **Windows:** установите [MSYS2](https://www.msys2.org/), в его терминале выполните
  `pacman -S mingw-w64-x86_64-pango`, затем добавьте `C:\msys64\mingw64\bin` в `PATH`
  (или задайте `WEASYPRINT_DLL_DIRECTORIES=C:\msys64\mingw64\bin`).
- **macOS:** `brew install pango`
- **Ubuntu/Debian:** `sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`

Проверка: `python -c "import weasyprint; print(weasyprint.__version__)"` не должна выдавать ошибку про `libgobject`/`pango`.
Подробности — в [документации WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html).

## 4. Установите зависимости и заполните `.env`

Нужен Python 3.11 или новее.

```bash
cd ai_report_bot
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # Windows: copy .env.example .env
```

Откройте `.env` и впишите свои значения:

```env
TELEGRAM_BOT_TOKEN=123456789:AAH...
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-5-5
LOG_LEVEL=INFO
```

`.env` указан в `.gitignore` и в репозиторий не попадает.

## 5. Запустите бота

```bash
python bot.py
```

В консоли появится строка `Бот @<username> запущен и ждёт сообщений`. Остановить — `Ctrl+C`.
Логи дублируются в `logs/bot.log`.

## 6. Проверьте оба сценария

**Отчёт по диалогу**

1. Отправьте боту `/report`.
2. Пришлите содержимое `examples/example_dialog.txt` текстом или прикрепите сам файл.
3. Бот напишет «Анализирую диалог…», затем пришлёт PDF и краткую сводку.
4. JSON ответа модели появится в `data/json/report_*.json`, PDF — в `reports/report_*.pdf`.

**Карточка товара — автоматический режим**

1. Отправьте `/product`.
2. Пришлите ссылку на страницу товара, например
   `https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html` (учебный сайт для scraping).
3. Бот пришлёт PDF-карточку с фото, ценой и описанием.

**Карточка товара — ручной режим (fallback)**

1. Отправьте `/product` и ссылку на сайт с антибот-защитой (крупные маркетплейсы обычно отвечают 403).
2. Бот сообщит «Не удалось автоматически получить данные с сайта. Давайте заполним карточку вручную.»
3. Ответьте на вопросы: название, описание, цена (`1299 EUR`), характеристики (каждая с новой строки),
   ссылка на фото — или «пропустить» (можно также прислать фото прямо в чат).
4. Если фото пропущено или не скачалось, PDF создаётся с placeholder-блоком.

`/cancel` в любой момент прерывает сценарий, `/help` показывает команды.

## Тесты

```bash
pytest
```

Тесты не ходят в сеть и не тратят токены: проверяются валидация JSON, генерация PDF,
разбор HTML товара и логика повторов запроса к Claude (на фейковом клиенте).

## Ограничения MVP

- Scraping без браузера: сайты, которые рисуют карточку JavaScript-ом, уходят в ручной режим.
- Состояние диалогов хранится в памяти процесса: после перезапуска бота незаконченный сценарий нужно начать заново.
- Баз данных, Docker и очередей нет — это локальный MVP.
