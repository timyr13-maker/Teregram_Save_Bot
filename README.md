# 🎬 Telegram Save Bot

Telegram бот для скачивания видео и музыки из YouTube, Instagram, TikTok и 1000+ других сайтов.

## ✨ Возможности

- 🎬 **Видео** — отправь ссылку, получи видео (макс. 1080p)
- 🎵 **Музыка** — команда `/music` + ссылка → MP3 (320kbps)
- 📐 **Умный выбор качества** — если видео 4K, скачивается в 1080p; если 720p — в 720p
- 🌐 **1000+ сайтов** — YouTube, Instagram, TikTok, Twitter/X, VK, Rutube и другие

## 🚀 Быстрый старт

### 1. Получи токен бота

Напиши [@BotFather](https://t.me/BotFather) в Telegram и создай нового бота.

### 2. Настрой окружение

```bash
cp .env.example .env
nano .env  # Вставь свой BOT_TOKEN
```

### 3. Запусти через Docker

```bash
docker compose up -d
```

### 4. Или запусти без Docker

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

> ⚠️ Для работы без Docker нужен [ffmpeg](https://ffmpeg.org/download.html) в системе.

## 📋 Команды бота

| Команда   | Описание                           |
|-----------|-------------------------------------|
| `/start`  | Приветствие и информация            |
| `/help`   | Список команд                       |
| `/music`  | Режим скачивания музыки (MP3)       |
| `/cancel` | Отменить режим музыки               |

## ⚙️ Конфигурация

### Секреты (`.env`)

| Переменная  | Описание               |
|-------------|------------------------|
| `BOT_TOKEN` | Telegram Bot API токен |

### Настройки (`config.py`)

| Константа          | Значение | Описание                        |
|--------------------|----------|---------------------------------|
| `MAX_VIDEO_HEIGHT` | `1080`   | Макс. высота видео (px)         |
| `MAX_FILE_SIZE_MB` | `50`     | Макс. размер файла для Telegram |

## 🏗️ Структура проекта

```
Telegram_Save_Bot/
├── bot.py              # Основной файл бота
├── config.py           # Конфигурация
├── requirements.txt    # Python зависимости
├── Dockerfile          # Docker образ
├── docker-compose.yml  # Docker Compose
├── .env.example        # Пример конфигурации
├── .env                # Секреты (не в git!)
├── .gitignore
├── .dockerignore
└── README.md
```

## 🔒 Безопасность

- Токен бота хранится в `.env` файле, который **не попадает в git** (`.gitignore`)
- `.env` **не запекается в Docker образ** (`.dockerignore`), а монтируется через `env_file` в `docker-compose.yml`
- Скачанные файлы автоматически удаляются после отправки

## 📝 Лицензия

MIT
