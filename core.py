"""
core.py — Ядро загрузчика (Separation of Concerns).

Отвечает за:
  - парсинг пользовательского ввода (несколько ссылок),
  - быстрое получение информации о видео (extract_info),
  - построение ydl_opts для yt-dlp,
  - парсинг кастомных аргументов CLI -> dict,
  - фоновые потоки InfoWorker / DownloadWorker,
  - прогресс через progress_hooks + postprocessor_hooks,
  - поиск FFmpeg.

GUI (main.py) НЕ должен импортировать yt_dlp напрямую.
Всё взаимодействие — через DownloadConfig, Workers и thread-safe callbacks.

Потокобезопасность: воркеры никогда не трогают GUI напрямую,
они только вызывают переданные callbacks. GUI решает, как
маршалить это в главный поток (через queue.Queue + after()).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import yt_dlp
from yt_dlp.utils import DownloadError


# ---------------------------------------------------------------------------
# Типы колбэков (все вызываются ИЗ фонового потока!)
# ---------------------------------------------------------------------------
LogCallback = Callable[[str], None]
ProgressCallback = Callable[[Dict[str, Any]], None]
StatusCallback = Callable[[str, str], None]  # (task_id/url, status_text)
InfoCallback = Callable[[Dict[str, Any]], None]  # успешный результат fetch
ErrorCallback = Callable[[str, str], None]  # (url, error_text)


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------
@dataclass
class DownloadConfig:
    """Снапшот всех настроек скачивания. Создаётся GUI перед стартом."""

    output_dir: str = "downloads"
    # best | 2160p | 1440p | 1080p | 720p | 480p | 360p | audio_mp3 | audio_m4a | audio_opus
    format_choice: str = "best"
    merge_format: str = "mp4"  # mp4 | mkv | webm

    download_subs: bool = False
    sub_langs: str = "ru,en"  # через запятую, например "ru,en" или "all"
    sub_auto: bool = True  # брать auto-generated, если нет авторских

    embed_metadata: bool = True
    embed_thumbnail: bool = True

    sponsorblock: bool = False
    sponsorblock_categories: List[str] = field(
        default_factory=lambda: [
            "sponsor",
            "intro",
            "outro",
            "selfpromo",
            "interaction",
            "preview",
        ]
    )

    playlist_mode: bool = False  # False = только одно видео (noplaylist=True)
    custom_args: str = ""  # сырая строка CLI-аргументов yt-dlp
    ffmpeg_location: Optional[str] = None  # путь к папке с ffmpeg.exe или к самому exe


@dataclass
class VideoInfo:
    """Нормализованный результат extract_info для отображения в GUI."""

    url: str
    title: str = ""
    uploader: str = ""
    duration: int = 0
    duration_str: str = ""
    is_playlist: bool = False
    playlist_count: int = 0
    webpage_url: str = ""
    extractor: str = ""


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
_URL_SPLIT_RE = re.compile(r"[\s,;|]+")

def parse_input_urls(raw_text: str) -> List[str]:
    """Разбить ввод пользователя на список URL.

    Поддерживает: новые строки, запятые, пробелы, точку с запятой.
    Пустые строки отбрасываются, дубликаты сохраняют порядок.
    """
    if not raw_text or not raw_text.strip():
        return []
    parts = _URL_SPLIT_RE.split(raw_text.strip())
    seen: set[str] = set()
    urls: List[str] = []
    for p in parts:
        u = p.strip().strip('"').strip("'")
        if not u:
            continue
        # Базовая валидация: должен быть похож на URL
        if "://" not in u and not u.startswith("www."):
            continue
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def format_duration(seconds: Any) -> str:
    """Секунды -> H:MM:SS / M:SS. Возвращает '—' если неизвестно."""
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    if s <= 0:
        # 0 может означать стрим; показываем как есть
        return "0:00" if s == 0 else "—"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def app_base_dir() -> str:
    """Папка программы: рядом с .exe во frozen-режиме, иначе папка скрипта."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg(custom_path: Optional[str] = None) -> Optional[str]:
    """Найти FFmpeg. Возвращает путь или None.

    Порядок:
      1. custom_path (файл или папка из настроек),
      2. ffmpeg.exe рядом с программой (.exe или .py) и в текущей папке,
      3. системный PATH (shutil.which).
    """
    if custom_path:
        custom_path = custom_path.strip().strip('"')
        if os.path.isfile(custom_path):
            return custom_path
        # Может быть указана папка — ищем ffmpeg(.exe) внутри
        candidate = os.path.join(custom_path, "ffmpeg.exe")
        if os.path.isfile(candidate):
            return candidate
        candidate2 = os.path.join(custom_path, "ffmpeg")
        if os.path.isfile(candidate2):
            return candidate2
    # Рядом с программой (важно для PyInstaller-сборки) и в cwd
    for folder in (app_base_dir(), os.getcwd()):
        for name in ("ffmpeg.exe", "ffmpeg"):
            local = os.path.join(folder, name)
            if os.path.isfile(local):
                return local
    # В PATH
    found = shutil.which("ffmpeg")
    return found


# ---------------------------------------------------------------------------
# Кастомные аргументы CLI -> ydl_opts
# ---------------------------------------------------------------------------
def parse_custom_args(arg_str: str) -> tuple[Dict[str, Any], List[str]]:
    """Преобразовать строку вида '--merge-output-format mkv --embed-chapters'
    в dict для YoutubeDL. Возвращает (opts, warnings).

    Известные флаги маппятся явно, неизвестные --foo-bar маппятся
    дженериком: --foo-bar value => {'foo_bar': value}, --flag => {'flag': True}.
    """
    opts: Dict[str, Any] = {}
    warnings: List[str] = []
    if not arg_str or not arg_str.strip():
        return opts, warnings

    try:
        # posix=False корректнее разбирает кавычки в Windows-cmd стиле
        tokens = shlex.split(arg_str, posix=True)
    except ValueError as e:
        return {}, [f"Не удалось разобрать кастомные аргументы: {e}"]

    i = 0
    # Явная таблица частых опций
    # (--cookies* обрабатываются отдельными ветками ниже, т.к. требуют
    #  спец-маппинга: cookiefile / кортеж cookiesfrombrowser)
    takes_value = {
        "--merge-output-format", "--merge_output_format",
        "--playlist-items", "--playlist_items",
        "--concurrent-fragments", "--concurrent_fragments",
        "--retries", "--fragment-retries", "--fragment_retries",
        "--proxy",
        "--user-agent", "--user_agent", "--referer", "--sleep-interval",
        "--max-sleep-interval", "--rate-limit", "--rate_limit",
        "--sponsorblock-remove", "--sponsorblock-mark",
        "--match-filter", "--match_filter", "--format-sort", "--format_sort",
        "--remux-video", "--recode-video", "--sub-langs", "--sub_langs",
    }

    def _set(key: str, value: Any) -> None:
        # Нормализация имён: CLI --rate-limit -> yt-dlp 'ratelimit' и т.п.
        aliases = {
            "rate_limit": "ratelimit",
            "sub_langs": "subtitleslangs",
            "subtitles_langs": "subtitleslangs",
            "remux_video": "remuxvideo",
            "recode_video": "recodevideo",
            "user_agent": "user_agent",  # yt-dlp понимает user_agent
            "match_filter": "match_filter",
            "format_sort": "format_sort",
        }
        opts[aliases.get(key, key)] = value

    def _set_browser_spec(spec: str) -> None:
        """BROWSER[+KEYRING][:PROFILE][::CONTAINER] -> кортеж для YoutubeDL.

        Важно: CLI-парсинг строки в кортеж делает __main__ yt-dlp, а не
        YoutubeDL — при работе через API кортеж нужно собрать самим,
        иначе load_cookies упадёт с CookieLoadError. Грамматика — 1-в-1
        как в yt_dlp/__init__.py.
        """
        m = re.fullmatch(
            r"(?P<name>[^+:]+)"
            r"(?:\s*\+\s*(?P<keyring>[^:]+))?"
            r"(?:\s*:\s*(?!:)(?P<profile>.+?))?"
            r"(?:\s*::\s*(?P<container>.+))?",
            spec.strip(),
        )
        if m is None:
            warnings.append(f"--cookies-from-browser: неверный формат: {spec!r} "
                            f"(нужно BROWSER[:PROFILE])")
            return
        name = m.group("name").strip().lower()
        keyring = m.group("keyring")
        profile = m.group("profile")
        container = m.group("container")
        if keyring is not None:
            keyring = keyring.strip().upper()
        if profile is not None:
            profile = profile.strip() or None
        if container is not None:
            container = container.strip() or None
        if name not in ("chrome", "chromium", "brave", "edge", "firefox",
                        "opera", "safari", "vivaldi", "whale"):
            # Синхронизировано с yt_dlp.cookies.SUPPORTED_BROWSERS.
            # Яндекс Браузера там нет — для него только метод «Файл cookies.txt».
            warnings.append(f"--cookies-from-browser: браузер {name!r} не поддерживается yt-dlp")
        _set("cookiesfrombrowser", (name, profile, keyring, container))

    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            warnings.append(f"Игнорирую токен без '-': {tok}")
            i += 1
            continue
        # Нормализация ключа: --foo-bar -> foo_bar
        key = tok.lstrip("-").replace("-", "_")

        # --- Явные маппинги ---
        if tok in ("--merge-output-format", "--merge_output_format"):
            val = tokens[i + 1] if i + 1 < len(tokens) else None
            if val is None:
                warnings.append(f"{tok}: нет значения")
                i += 1
            else:
                _set("merge_output_format", val)
                i += 2
        elif tok in ("--embed-chapters", "--embed_chapters"):
            _set("embedchapters", True)
            i += 1
        elif tok in ("--no-embed-chapters",):
            _set("embedchapters", False)
            i += 1
        elif tok in ("--embed-subs", "--embed-subtitles", "--embed_subs"):
            _set("embedsubs", True)
            i += 1
        elif tok in ("--write-subs", "--write_subs", "--write-sub"):
            _set("writesubtitles", True)
            i += 1
        elif tok in ("--yes-playlist", "--yes_playlist"):
            _set("noplaylist", False)
            i += 1
        elif tok in ("--no-playlist", "--no_playlist"):
            _set("noplaylist", True)
            i += 1
        elif tok in ("--geo-bypass",):
            _set("geo_bypass", True)
            i += 1
        elif tok in ("--no-geo-bypass",):
            _set("geo_bypass", False)
            i += 1
        elif tok in ("--embed-thumbnail", "--embed_thumbnail"):
            _set("embedthumbnail", True)
            _set("writethumbnail", True)
            i += 1
        elif tok in ("--embed-metadata", "--embed_metadata"):
            _set("embedmetadata", True)
            _set("addmetadata", True)
            i += 1
        elif tok in ("--cookies", "--cookiefile"):
            # CLI --cookies FILE -> YoutubeDL 'cookiefile' ('cookies' движок проигнорирует!)
            val = tokens[i + 1] if i + 1 < len(tokens) else None
            if val is None or val.startswith("-"):
                warnings.append(f"{tok}: нет значения")
                i += 1
            else:
                _set("cookiefile", val)
                i += 2
        elif tok in ("--cookies-from-browser", "--cookies_from_browser"):
            val = tokens[i + 1] if i + 1 < len(tokens) else None
            if val is None or val.startswith("-"):
                warnings.append(f"{tok}: нет значения (нужно BROWSER[:PROFILE])")
                i += 1
            else:
                _set_browser_spec(val)
                i += 2
        elif tok in takes_value:
            val = tokens[i + 1] if i + 1 < len(tokens) else None
            if val is None or val.startswith("-"):
                warnings.append(f"{tok}: нет значения")
                i += 1
            else:
                # Числовые опции приводим к int
                if key in ("concurrent_fragments", "retries", "fragment_retries",
                           "sleep_interval", "max_sleep_interval"):
                    try:
                        val_int = int(val)
                        _set(key, val_int)
                    except ValueError:
                        _set(key, val)
                elif key in ("sponsorblock_remove", "sponsorblock_mark"):
                    _set("sponsorblock_remove" if "remove" in tok else "sponsorblock_mark",
                         [c.strip() for c in val.split(",") if c.strip()])
                else:
                    _set(key, val)
                i += 2
        elif tok.startswith("--no-"):
            # --no-xxx => xxx = False
            _set(key[3:], False)
            i += 1
        else:
            # Дженерик: если следующий токен — значение, забираем его
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("-"):
                _set(key, nxt)
                i += 2
            else:
                _set(key, True)
                i += 1
    return opts, warnings


# ---------------------------------------------------------------------------
# Построение ydl_opts
# ---------------------------------------------------------------------------
_RESOLUTION_HEIGHT = {
    "2160p": 2160, "4k": 2160,
    "1440p": 1440, "2k": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

def _format_string(choice: str) -> str:
    """Выбор пользователя -> format-селектор yt-dlp."""
    c = (choice or "best").strip().lower()
    if c in ("best", "лучшее"):
        return "bv*+ba/b"
    if c in _RESOLUTION_HEIGHT:
        h = _RESOLUTION_HEIGHT[c]
        # Лучшее видео не выше H + лучшее аудио, иначе одиночный файл
        return f"bv*[height<={h}]+ba/b[height<={h}]/b"
    if c.startswith("audio"):
        return "ba/b"
    return "bv*+ba/b"


class _QuietLogger:
    """Логгер yt-dlp, который перенаправляет сообщения в GUI-лог.

    Важно: методы вызываются из фонового потока — колбэк обязан
    быть thread-safe (например, класть в queue.Queue).
    Ошибки логгера никогда не должны ронять загрузку.
    """

    def __init__(self, log_cb: Optional[LogCallback],
                 collect_errors: Optional[List[str]] = None):
        self._log = log_cb
        # Сюда складываем строки ошибок — нужно, чтобы при ignoreerrors=True
        # не потерять НАСТОЯЩУЮ причину (иначе GUI покажет только «result is None»).
        self._collect_errors = collect_errors

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            # Чистим ANSI-цвета, которые yt-dlp иногда добавляет
            clean = re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).strip()
            if clean:
                self._log(clean)
        except Exception:
            pass

    def debug(self, msg: str) -> None:
        # debug очень шумный — пропускаем служебные строки
        if isinstance(msg, str) and msg.startswith("[debug] "):
            return
        self._emit(msg)

    def warning(self, msg: str) -> None:
        self._emit(f"[warn] {msg}")

    def error(self, msg: str) -> None:
        if self._collect_errors is not None:
            try:
                clean = re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).strip()
                # Режем FAQ-простыни, но оставляем суть (первые ~400 символов)
                self._collect_errors.append(clean[:400])
            except Exception:
                pass
        self._emit(f"[ошибка] {msg}")


# Маркеры «YouTube требует вход» в текстах ошибок yt-dlp
_BOT_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "use --cookies",
    "--cookies-from-browser",
)

BOT_HINT = ("💡 Похоже, YouTube требует вход (защита от ботов). "
            "Включи 🔐 YouTube Login в настройках: «Из браузера» или «Файл cookies.txt».")


def friendly_dl_error(exc_text: str) -> str:
    """Добавить русскую подсказку про 🔐 Login к бот-ошибкам YouTube."""
    try:
        low = str(exc_text).lower()
    except Exception:
        return str(exc_text)
    if any(m in low for m in _BOT_MARKERS):
        return f"{exc_text}\n{BOT_HINT}"
    return str(exc_text)


def build_ydl_opts(
    cfg: DownloadConfig,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    postprocessor_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    log_cb: Optional[LogCallback] = None,
) -> Dict[str, Any]:
    """Собрать словарь опций YoutubeDL из DownloadConfig + custom_args."""
    os.makedirs(cfg.output_dir or "downloads", exist_ok=True)

    fmt = _format_string(cfg.format_choice)
    is_audio_only = cfg.format_choice.startswith("audio")

    # Кодек для аудио-режима
    audio_codec = "mp3"
    if cfg.format_choice == "audio_m4a":
        audio_codec = "m4a"
    elif cfg.format_choice == "audio_opus":
        audio_codec = "opus"

    outtmpl = os.path.join(cfg.output_dir or "downloads", "%(title)s [%(id)s].%(ext)s")

    ydl_opts: Dict[str, Any] = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": not cfg.playlist_mode,
        "yes_playlist": cfg.playlist_mode,
        "ignoreerrors": True,  # плейлист: пропускать недоступные, не падать
        "no_warnings": False,
        "quiet": True,      # весь вывод — через logger + hooks
        "noprogress": True,
        "socket_timeout": 15,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragments": 4,
        "windowsfilenames": True,  # безопасные имена для Windows
        "restrictfilenames": False,
        "logger": _QuietLogger(log_cb),
    }

    if progress_hook is not None:
        ydl_opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook is not None:
        ydl_opts["postprocessor_hooks"] = [postprocessor_hook]

    # --- FFmpeg ---
    ffmpeg_path = find_ffmpeg(cfg.ffmpeg_location)
    if ffmpeg_path:
        # yt-dlp ждёт либо папку, либо путь к exe
        if os.path.isfile(ffmpeg_path) and os.path.basename(ffmpeg_path).lower().startswith("ffmpeg"):
            ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path) or ffmpeg_path
            # Если передали полный путь к файлу — тоже ок, оставим как есть
            if os.path.isfile(ffmpeg_path):
                ydl_opts["ffmpeg_location"] = ffmpeg_path
        else:
            ydl_opts["ffmpeg_location"] = ffmpeg_path

    # --- Слияние / ремукс ---
    if not is_audio_only and cfg.merge_format in ("mp4", "mkv", "webm"):
        ydl_opts["merge_output_format"] = cfg.merge_format

    # --- Аудио-режим ---
    if is_audio_only:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_codec,
                "preferredquality": "0",  # лучшее качество VBR
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        if cfg.embed_thumbnail:
            ydl_opts["writethumbnail"] = True
            ydl_opts["embedthumbnail"] = True
    else:
        # --- Видео-режим: метаданные и обложка ---
        if cfg.embed_metadata:
            ydl_opts["addmetadata"] = True
            ydl_opts["embedmetadata"] = True
            ydl_opts["embedchapters"] = True
        if cfg.embed_thumbnail:
            ydl_opts["writethumbnail"] = True
            ydl_opts["embedthumbnail"] = True

    # --- Субтитры ---
    if cfg.download_subs:
        langs = [s.strip() for s in (cfg.sub_langs or "").split(",") if s.strip()]
        if not langs:
            langs = ["ru", "en"]
        ydl_opts["writesubtitles"] = True
        ydl_opts["writeautomaticsub"] = bool(cfg.sub_auto)
        ydl_opts["subtitleslangs"] = langs
        ydl_opts["subtitlesformat"] = "srt/best"
        ydl_opts["embedsubs"] = True  # встроить + оставить файл

    # --- SponsorBlock ---
    if cfg.sponsorblock:
        ydl_opts["sponsorblock_remove"] = list(cfg.sponsorblock_categories or ["sponsor"])

    # --- Кастомные аргументы (перебивают базовые) ---
    custom, warns = parse_custom_args(cfg.custom_args)
    for w in warns:
        if log_cb:
            try:
                log_cb(f"[warn] кастомные аргументы: {w}")
            except Exception:
                pass
    # noplaylist из чекбокса — главнее, чтобы GUI не врал; остальное перебиваем
    noplaylist_gui = ydl_opts.get("noplaylist")
    ydl_opts.update(custom)
    if "--no-playlist" not in (cfg.custom_args or "") and "--yes-playlist" not in (cfg.custom_args or ""):
        ydl_opts["noplaylist"] = noplaylist_gui

    return ydl_opts


# ---------------------------------------------------------------------------
# Получение информации (быстрый двухэтапный парсинг)
# ---------------------------------------------------------------------------
def fetch_info_sync(url: str, playlist_mode: bool = False,
                    ffmpeg_location: Optional[str] = None) -> VideoInfo:
    """Блокирующий вызов. Выполнять ТОЛЬКО в фоновом потоке.

    Этап 1: extract_info(process=False) — быстрый, определяет тип.
    Этап 2: process_ie_result — догружает title/duration для видео.
    """
    base_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "noplaylist": not playlist_mode,
        "ignoreerrors": True,  # для плейлистов: пропускать битые entries
        "extract_flat": False,
    }
    if ffmpeg_location:
        fp = find_ffmpeg(ffmpeg_location)
        if fp:
            base_opts["ffmpeg_location"] = fp

    # Собираем НАСТОЯЩИЕ тексты ошибок: при ignoreerrors=True движок глотает
    # исключение и возвращает None — без коллектора GUI показал бы пустышку.
    err_lines: List[str] = []
    base_opts["logger"] = _QuietLogger(None, collect_errors=err_lines)

    def _fail(prefix: str) -> DownloadError:
        cause = err_lines[-1] if err_lines else ""
        text = f"{prefix}: {url}" + (f" — {cause}" if cause else "")
        return DownloadError(friendly_dl_error(text))

    with yt_dlp.YoutubeDL(base_opts) as ydl:
        # Этап 1 — лёгкий
        try:
            ie_result = ydl.extract_info(url, download=False, process=False)
        except DownloadError as e:
            # ignoreerrors=False у пользователя? Всё равно отдаём дружелюбный текст
            raise DownloadError(friendly_dl_error(str(e))) from e
        if ie_result is None:
            raise _fail("Не удалось получить информацию")

        # Плейлист?
        if ie_result.get("_type") == "playlist":
            entries = ie_result.get("entries")
            count: int
            try:
                if hasattr(entries, "__len__"):
                    count = len(entries)  # type: ignore[arg-type]
                else:
                    # entries может быть генератором — считаем аккуратно (до 5000)
                    count = sum(1 for _ in entries)  # type: ignore
            except Exception:
                count = ie_result.get("playlist_count") or 0
            return VideoInfo(
                url=url,
                title=ie_result.get("title") or "Плейлист",
                uploader=ie_result.get("uploader") or ie_result.get("channel") or "",
                duration=0,
                duration_str=f"треков: {count}" if count else "плейлист",
                is_playlist=True,
                playlist_count=int(count or 0),
                webpage_url=ie_result.get("webpage_url") or url,
                extractor=ie_result.get("extractor") or "",
            )

        # Этап 2 — полная обработка single video
        info = ydl.process_ie_result(ie_result, download=False)
        if info is None:
            raise _fail("Пустой результат")
        if info.get("_type") == "playlist":
            # Редкий кейс: раскрылся как плейлист на втором этапе
            entries = info.get("entries") or []
            try:
                count = len(list(entries)) if not hasattr(entries, "__len__") else len(entries)
            except Exception:
                count = 0
            return VideoInfo(
                url=url,
                title=info.get("title") or "Плейлист",
                is_playlist=True,
                playlist_count=int(count or 0),
                duration_str=f"треков: {count}",
                webpage_url=info.get("webpage_url") or url,
                extractor=info.get("extractor") or "",
            )
        dur = info.get("duration") or 0
        return VideoInfo(
            url=url,
            title=info.get("title") or url,
            uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
            duration=int(dur or 0),
            duration_str=format_duration(dur),
            is_playlist=False,
            playlist_count=0,
            webpage_url=info.get("webpage_url") or url,
            extractor=info.get("extractor") or "",
        )


# ---------------------------------------------------------------------------
# Фоновые потоки
# ---------------------------------------------------------------------------
class InfoWorker(threading.Thread):
    """Фоново тянет информацию по списку URL. GUI обновляется через колбэки."""

    daemon = True

    def __init__(
        self,
        urls: List[str],
        playlist_mode: bool = False,
        ffmpeg_location: Optional[str] = None,
        on_info: Optional[InfoCallback] = None,
        on_error: Optional[ErrorCallback] = None,
        on_done: Optional[Callable[[], None]] = None,
    ):
        super().__init__(name="InfoWorker")
        self._urls = urls
        self._playlist_mode = playlist_mode
        self._ffmpeg_location = ffmpeg_location
        self._on_info = on_info
        self._on_error = on_error
        self._on_done = on_done

    def run(self) -> None:  # noqa: D102
        try:
            for url in self._urls:
                try:
                    vi = fetch_info_sync(url, self._playlist_mode, self._ffmpeg_location)
                    if self._on_info:
                        self._on_info({
                            "url": vi.url,
                            "title": vi.title,
                            "uploader": vi.uploader,
                            "duration_str": vi.duration_str,
                            "is_playlist": vi.is_playlist,
                            "playlist_count": vi.playlist_count,
                        })
                except Exception as e:  # noqa: BLE001 — обязаны не ронять поток
                    if self._on_error:
                        self._on_error(url, friendly_dl_error(f"{type(e).__name__}: {e}"))
        finally:
            if self._on_done:
                try:
                    self._on_done()
                except Exception:
                    pass


class DownloadWorker(threading.Thread):
    """Скачивает очередь URL один за другим. Не трогает GUI напрямую."""

    daemon = True

    def __init__(
        self,
        urls: List[str],
        config: DownloadConfig,
        on_log: Optional[LogCallback] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_status: Optional[StatusCallback] = None,
        on_file_done: Optional[Callable[[str, str], None]] = None,  # (url, filepath)
        on_all_done: Optional[Callable[[], None]] = None,
        on_error: Optional[ErrorCallback] = None,
    ):
        super().__init__(name="DownloadWorker")
        self._urls = urls
        self._config = config
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_status = on_status
        self._on_file_done = on_file_done
        self._on_all_done = on_all_done
        self._on_error_cb = on_error
        self._stop_flag = threading.Event()
        self._current_url = ""

    # -- управление --
    def stop(self) -> None:
        """Запросить остановку (проверяется между файлами + хуком прогресса)."""
        self._stop_flag.set()

    @property
    def stopped(self) -> bool:
        return self._stop_flag.is_set()

    # -- внутренние хуки yt-dlp (вызываются движком скачивания) --
    def _progress_hook(self, d: Dict[str, Any]) -> None:
        if self._stop_flag.is_set():
            raise DownloadError("Остановлено пользователем")
        if self._on_progress is None:
            return
        try:
            status = d.get("status")
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100.0) if total else 0.0
            payload = {
                "url": self._current_url,
                "hook_status": status,  # downloading | finished | error
                "downloaded": downloaded,
                "total": total,
                "percent": percent,
                "speed": d.get("speed") or 0,
                "eta": d.get("eta"),
                "filename": d.get("filename") or d.get("info_dict", {}).get("_filename", ""),
                "tmpfilename": d.get("tmpfilename", ""),
            }
            self._on_progress(payload)
        except DownloadError:
            raise
        except Exception:
            pass  # хук не должен ронять загрузку

    def _pp_hook(self, d: Dict[str, Any]) -> None:
        """Хук пост-обработки: ловим 'Merging', 'ExtractAudio', 'Embedding'. """
        if self._on_status is None:
            return
        try:
            pp_status = d.get("status")  # starting | finished
            pp_info = d.get("postprocessor") or d.get("info_dict", {}).get("postprocessor")
            # yt-dlp кладёт имя постпроцессора в d['postprocessor']
            name = str(d.get("postprocessor", ""))
            if pp_status == "started":
                if "Merger" in name:
                    self._on_status(self._current_url, "🔀 Слияние видео+аудио (FFmpeg)…")
                elif "ExtractAudio" in name:
                    self._on_status(self._current_url, "🎵 Конвертация аудио (FFmpeg)…")
                elif "Embed" in name or "Metadata" in name:
                    self._on_status(self._current_url, "🏷 Встраивание метаданных/обложки…")
                else:
                    self._on_status(self._current_url, f"⚙️ Обработка: {name or 'FFmpeg'}…")
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def _set_status(self, url: str, text: str) -> None:
        if self._on_status:
            try:
                self._on_status(url, text)
            except Exception:
                pass

    # -- главный цикл --
    def run(self) -> None:  # noqa: D102
        total = len(self._urls)
        for idx, url in enumerate(self._urls, start=1):
            if self._stop_flag.is_set():
                self._log("⏹ Загрузка остановлена пользователем.")
                break
            self._current_url = url
            self._set_status(url, f"⬇️ Скачивание {idx}/{total}…")
            self._log(f"─── [{idx}/{total}] {url}")

            try:
                ydl_opts = build_ydl_opts(
                    self._config,
                    progress_hook=self._progress_hook,
                    postprocessor_hook=self._pp_hook,
                    log_cb=self._log,
                )
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    retcode = ydl.download([url])

                if self._stop_flag.is_set():
                    self._set_status(url, "⏹ Остановлено")
                elif retcode != 0:
                    # ignoreerrors=True: движок не бросил исключение, но файл
                    # не создан (retcode=1). Без проверки показали бы «✅ Готово».
                    raise DownloadError(
                        "yt-dlp завершился с ошибкой (файл не создан). "
                        "Смотри строки [ошибка] выше в логе.")
                else:
                    self._set_status(url, "✅ Готово")
                    if self._on_file_done:
                        try:
                            self._on_file_done(url, "")
                        except Exception:
                            pass
                    self._log(f"✅ Готово: {url}")

            except DownloadError as e:
                msg = friendly_dl_error(str(e))
                if "Остановлено пользователем" in msg:
                    self._set_status(url, "⏹ Остановлено")
                    self._log("⏹ Остановлено пользователем.")
                    break
                self._set_status(url, f"❌ Ошибка: {msg[:160]}")
                self._log(f"❌ Ошибка загрузки {url}: {msg}")
                if self._on_error_cb:
                    try:
                        self._on_error_cb(url, msg)
                    except Exception:
                        pass
                continue  # идём к следующему URL, не роняем очередь
            except Exception as e:  # noqa: BLE001 — очередь обязана жить
                msg = friendly_dl_error(f"{type(e).__name__}: {e}")
                self._set_status(url, f"❌ {msg[:160]}")
                self._log(f"❌ Неожиданная ошибка {url}: {msg}")
                if self._on_error_cb:
                    try:
                        self._on_error_cb(url, msg)
                    except Exception:
                        pass
                continue

        if self._on_all_done:
            try:
                self._on_all_done()
            except Exception:
                pass
