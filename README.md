# ⬇️ YtDoWnLoAdEr

Современное десктопное приложение под **Windows** для скачивания видео и аудио с YouTube (и сотен других сайтов) на движке [yt-dlp](https://github.com/yt-dlp/yt-dlp). Функциональный аналог Android-приложения **YTDLnis**.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![GUI](https://img.shields.io/badge/GUI-CustomTkinter-green)
![Engine](https://img.shields.io/badge/engine-yt--dlp-red)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

## ✨ Возможности

- 🔗 Одиночные видео, плейлисты и **несколько ссылок** (с новой строки / через запятую)
- 🔍 Фонове получение информации (название, автор, длительность) без зависания интерфейса
- 🎬 «Лучшее видео + лучшее аудио» со слиянием через FFmpeg, выбор разрешения (4K → 360p)
- 🎵 Режим «только аудио» (MP3 / M4A / OPUS)
- 💬 Субтитры (авторские / авто, выбор языков, встраивание в файл)
- 🏷 Встраивание метаданных и обложки
- ✂️ SponsorBlock — вырезание спонсорских интеграций
- 🧩 Поле кастомных аргументов CLI yt-dlp
- 📊 Прогресс-бар: проценты, скорость, ETA (через `progress_hooks`)
- 🧵 Скачивание строго в фоновом потоке, ошибки — в лог без крашей

## 🚀 Быстрый старт (без установки Python)

1. Скачай последний `YtDoWnLoAdEr.exe` из [Releases](../../releases).
2. Положи `ffmpeg.exe` рядом с `.exe` (нужен для 1080p+, 4K и MP3) — см. ниже.
3. Запусти, вставь ссылку, жми **СКАЧАТЬ**.

## 🛠 Запуск из исходников

```powershell
# 1. Python 3.10+
python --version

# 2. Зависимости
pip install -r requirements.txt

# 3. Запуск
python main.py
```

## 🎞 FFmpeg (важно!)

Без FFmpeg не работают: слияние видео+аудио (всё ≥1080p), конвертация в MP3, встраивание обложки.

Вариант А — рядом с программой (рекомендуется для `.exe`):
скачай `ffmpeg.exe` ([gyan.dev](https://www.gyan.dev/ffmpeg/builds/) → release essentials)
и положи в папку с `main.py` / `YtDoWnLoAdEr.exe`.

Вариант Б — в систему: `winget install Gyan.FFmpeg` (приложение найдёт его в `PATH`).

Путь можно указать вручную в настройках — он сохраняется в `config.json`.

## 📦 Сборка .exe

```powershell
pip install pyinstaller
.\build.ps1        # результат: dist\YtDoWnLoAdEr\YtDoWnLoAdEr.exe
.\build.ps1 -OneFile  # один файл dist\YtDoWnLoAdEr.exe (запускается дольше)
```

## 📂 Структура проекта

```
main.py   # GUI (CustomTkinter): виджеты, очередь сообщений, config.json
core.py   # Ядро: yt-dlp как модуль, InfoWorker / DownloadWorker, прогресс-хуки
```

Архитектура: GUI-поток только рисует и раз в 100 мс разбирает `queue.Queue`.
Воркеры никогда не трогают виджеты напрямую — только кладут события в очередь.

## ⚙️ Настройки

Хранятся в `config.json` рядом с программой: папка сохранения, формат,
субтитры, SponsorBlock, путь к FFmpeg, тема. В репозиторий не коммитится.

## 📄 Лицензия

MIT — см. [LICENSE](LICENSE). Движок yt-dlp распространяется под Unlicense.
