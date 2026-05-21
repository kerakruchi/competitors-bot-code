# 🤖 Competitors News Monitor Bot

Telegram-бот для автоматического мониторинга новостей конкурентов. Следит за сайтами и LinkedIn-страницами, классифицирует публикации с помощью AI и присылает ежедневные обновления.

## Возможности

- **Автоматический поиск фида** — RSS/Atom или HTML-скрапинг, включая JS-сайты через Playwright
- **AI-классификация** — GPT-4o-mini определяет категорию и пишет краткое резюме на русском
- **Гибкое расписание** — каждый пользователь настраивает своё время уведомлений
- **Режим дайджеста** — все новости за день в одном сообщении вместо потока
- **Избранное** — сохранение статей кнопкой прямо в чате
- **Фильтрация по категориям** — ивенты, продукты, кейсы, другое
- **Поиск по кешу** — поиск по всем сохранённым статьям
- **LinkedIn-поддержка** — скрапинг страниц компаний через Playwright

## Команды

| Команда | Описание |
|---|---|
| `/add <url>` | Добавить сайт или LinkedIn-страницу |
| `/remove <domain>` | Удалить источник |
| `/pause <domain>` | Приостановить мониторинг |
| `/resume <domain>` | Возобновить мониторинг |
| `/list` | Список всех источников |
| `/check` | Проверить прямо сейчас |
| `/search <запрос>` | Поиск по кешу статей |
| `/schedule HH:MM` | Установить время уведомлений |
| `/digest` | Включить/выключить режим дайджеста |
| `/favourites` | Показать избранное |
| `/event` | Новости про ивенты |
| `/product` | Новости про продукты |
| `/cases` | Кейсы и внедрения |
| `/other` | Прочие новости |

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/kerakruchi/competitors-bot-code
cd competitors-bot-code
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt
```

Для поддержки JS-сайтов и LinkedIn (опционально):

```bash
pip install playwright && playwright install chromium
```

### 3. Заполнить переменные окружения

Открыть файл `.env` и вставить токены:

```
TELEGRAM_BOT_TOKEN=<токен от @BotFather>
OPENAI_API_KEY=<ключ от OpenAI>
```

Получить токены:
- **Telegram** — [@BotFather](https://t.me/BotFather) → `/newbot`
- **OpenAI** — [platform.openai.com/api-keys](https://platform.openai.com/api-keys)

> Если `OPENAI_API_KEY` не указан — бот продолжит работу, классификация будет по ключевым словам без AI-резюме.

### 4. Запустить

```bash
python3 bot.py
```

## Настройка расписания

По умолчанию уведомления приходят в **06:00 по Москве**. Изменить для своего аккаунта:

```
/schedule 09:00
```

Дополнительные параметры через переменные окружения в `.env`:

```
SCHEDULE_HOUR=6
SCHEDULE_MINUTE=0
SCHEDULE_TZ=Europe/Moscow
```

## Как работает мониторинг

1. При добавлении источника бот кеширует все текущие статьи — они не отправляются
2. Каждые 5 минут бот проверяет, наступило ли запланированное время у каждого пользователя
3. При обнаружении новых статей — классифицирует через GPT-4o-mini и отправляет
4. Для HTML-сайтов без RSS — подтягивает точные даты публикаций из JSON-LD каждой страницы

## Стек

- [python-telegram-bot](https://python-telegram-bot.org/) — Telegram Bot API
- [feedparser](https://feedparser.readthedocs.io/) — парсинг RSS/Atom
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML-скрапинг
- [OpenAI API](https://platform.openai.com/) — классификация и резюме
- [Playwright](https://playwright.dev/python/) — JS-сайты и LinkedIn (опционально)
- SQLite — локальное хранилище

## Деплой на Heroku

```bash
heroku create
heroku config:set TELEGRAM_BOT_TOKEN=...
heroku config:set OPENAI_API_KEY=...
git push heroku main
```

`Procfile` уже настроен для запуска как worker-процесс.
