"""
main.py — Графический интерфейс (CustomTkinter).

Архитектура:
  GUI-поток (mainloop) только рисует и читает очередь сообщений.
  InfoWorker / DownloadWorker (core.py) работают в фоне и кладут
  события в queue.Queue. Раз в 100 мс _poll_queue() переносит их в GUI.
  Прямого доступа из потоков к виджетам НЕТ — только через очередь.

Запуск:
  pip install -r requirements.txt
  python main.py
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog
from typing import Any, Dict, List, Optional

import customtkinter as ctk

from core import (
    DownloadConfig,
    DownloadWorker,
    InfoWorker,
    check_youtube_login,
    find_ffmpeg,
    parse_input_urls,
)

# ---------------------------------------------------------------------------
# Константы UI
# ---------------------------------------------------------------------------
APP_TITLE = "YT Downloader — yt-dlp GUI"
APP_VERSION = "1.0.6"


def app_dir() -> str:
    """Папка для config.json и загрузок по умолчанию.

    Во frozen-режиме (PyInstaller .exe) __file__ указывает во временную
    папку _MEI*, поэтому берём папку рядом с .exe.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_downloads_dir() -> str:
    return os.path.join(app_dir(), "downloads")


CONFIG_FILE = os.path.join(app_dir(), "config.json")

DISPLAY_TO_CORE = {
    "⭐ Лучшее видео + аудио": "best",
    "4K (2160p)": "2160p",
    "2K (1440p)": "1440p",
    "Full HD (1080p)": "1080p",
    "HD (720p)": "720p",
    "480p": "480p",
    "360p": "360p",
    "🎵 Только аудио — MP3": "audio_mp3",
    "🎵 Только аудио — M4A": "audio_m4a",
    "🎵 Только аудио — OPUS": "audio_opus",
}
CORE_TO_DISPLAY = {v: k for k, v in DISPLAY_TO_CORE.items()}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------
def human_size(n: Any) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_speed(bps: Any) -> str:
    try:
        bps = float(bps or 0)
    except (TypeError, ValueError):
        return "—"
    if bps <= 0:
        return "—"
    return f"{human_size(bps)}/с"


def human_eta(secs: Any) -> str:
    try:
        if secs is None:
            return "—"
        s = int(secs)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        return "—"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1020x800")
        self.minsize(900, 700)

        # Межпоточная очередь: воркеры -> GUI
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.info_worker: InfoWorker | None = None
        self.dl_worker: DownloadWorker | None = None
        self.fetched: List[Dict[str, Any]] = []  # результаты парсинга
        self.task_widgets: Dict[str, Dict[str, Any]] = {}  # url -> {frame, status, bar...}

        self._build_widgets()
        self._load_config()
        self._refresh_ffmpeg_status()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ============================ построение ============================
    def _build_widgets(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # --- Шапка ---
        header = ctk.CTkFrame(self)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="⬇️ YT Downloader  (yt-dlp)",
                     font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=8)
        self.theme_menu = ctk.CTkOptionMenu(
            header, values=["Dark", "Light", "System"],
            width=110, command=self._on_theme_change)
        self.theme_menu.set("Dark")
        self.theme_menu.grid(row=0, column=1, padx=6, pady=8)
        self.ffmpeg_badge = ctk.CTkLabel(header, text="FFmpeg: …", font=ctk.CTkFont(size=12))
        self.ffmpeg_badge.grid(row=0, column=2, padx=12, pady=8)

        # --- Ввод ссылок ---
        url_box = ctk.CTkFrame(self)
        url_box.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        url_box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(url_box, text="Ссылки (каждая с новой строки или через запятую):",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        self.url_text = ctk.CTkTextbox(url_box, height=70)
        self.url_text.grid(row=1, column=0, sticky="ew", padx=12, pady=2)
        self.url_text.insert("1.0", "")
        
        # Контекстное меню для правой кнопки мыши
        self._create_context_menu()
        self.url_text.bind("<Button-3>", self._show_context_menu)

        btn_row = ctk.CTkFrame(url_box, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 8))
        ctk.CTkButton(btn_row, text="📋 Вставить", width=110,
                      command=self._on_paste).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row, text="🧹 Очистить", width=110, fg_color="gray",
                      command=lambda: self.url_text.delete("1.0", "end")).pack(side="left", padx=6)
        self.fetch_btn = ctk.CTkButton(btn_row, text="🔍 Получить информацию", width=200,
                                       command=self._on_fetch_info)
        self.fetch_btn.pack(side="left", padx=6)
        self.playlist_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(btn_row, text="Скачать плейлист целиком",
                        variable=self.playlist_var).pack(side="left", padx=12)
        self.info_label = ctk.CTkLabel(url_box, text="Вставьте ссылку и нажмите «Получить информацию».",
                                       font=ctk.CTkFont(size=12), wraplength=950, justify="left")
        self.info_label.grid(row=3, column=0, sticky="w", padx=12, pady=(0, 8))

        # --- Настройки (две колонки) ---
        settings = ctk.CTkFrame(self)
        settings.grid(row=2, column=0, sticky="ew", padx=12, pady=6)
        settings.grid_columnconfigure(0, weight=1)
        settings.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(settings)
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        left.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(left, text="🎬 Формат", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))
        ctk.CTkLabel(left, text="Качество:").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        self.format_menu = ctk.CTkOptionMenu(left, values=list(DISPLAY_TO_CORE.keys()), width=220)
        self.format_menu.set("⭐ Лучшее видео + аудио")
        self.format_menu.grid(row=1, column=1, sticky="ew", padx=10, pady=4)
        ctk.CTkLabel(left, text="Контейнер:").grid(row=2, column=0, sticky="w", padx=10, pady=4)
        self.merge_menu = ctk.CTkOptionMenu(left, values=["mp4", "mkv", "webm"], width=220)
        self.merge_menu.set("mp4")
        self.merge_menu.grid(row=2, column=1, sticky="ew", padx=10, pady=4)
        ctk.CTkLabel(left, text="Папка:").grid(row=3, column=0, sticky="w", padx=10, pady=4)
        folder_row = ctk.CTkFrame(left, fg_color="transparent")
        folder_row.grid(row=3, column=1, sticky="ew", padx=10, pady=4)
        folder_row.grid_columnconfigure(0, weight=1)
        self.output_entry = ctk.CTkEntry(folder_row)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.output_entry.insert(0, default_downloads_dir())
        ctk.CTkButton(folder_row, text="…", width=40,
                      command=self._on_browse_output).grid(row=0, column=1)
        ctk.CTkButton(left, text="📂 Открыть папку", command=self._on_open_folder).grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10))

        right = ctk.CTkFrame(settings)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        ctk.CTkLabel(right, text="⚙️ Опции", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 2))
        self.subs_var = tk.BooleanVar(value=False)
        self.auto_sub_var = tk.BooleanVar(value=True)
        self.meta_var = tk.BooleanVar(value=True)
        self.thumb_var = tk.BooleanVar(value=True)
        self.sponsor_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(right, text="Скачать субтитры", variable=self.subs_var).pack(
            anchor="w", padx=10, pady=2)
        sub_row = ctk.CTkFrame(right, fg_color="transparent")
        sub_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(sub_row, text="Языки:").pack(side="left")
        self.sub_langs_entry = ctk.CTkEntry(sub_row, width=140)
        self.sub_langs_entry.pack(side="left", padx=6)
        self.sub_langs_entry.insert(0, "ru,en")
        ctk.CTkCheckBox(sub_row, text="auto", variable=self.auto_sub_var).pack(side="left", padx=6)
        ctk.CTkCheckBox(right, text="Встроить метаданные", variable=self.meta_var).pack(
            anchor="w", padx=10, pady=2)
        ctk.CTkCheckBox(right, text="Встроить обложку", variable=self.thumb_var).pack(
            anchor="w", padx=10, pady=2)
        ctk.CTkCheckBox(right, text="✂️ SponsorBlock (вырезать рекламу)",
                        variable=self.sponsor_var).pack(anchor="w", padx=10, pady=2)
        
        # --- Аутентификация YouTube ---
        auth_frame = ctk.CTkFrame(right, fg_color="transparent")
        auth_frame.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(auth_frame, text="🔐 YouTube Login", font=ctk.CTkFont(weight="bold", size=11)).pack(
            anchor="w", padx=8, pady=(6, 4))
        
        self.auth_method_var = tk.StringVar(value="none")
        method_frame = ctk.CTkFrame(auth_frame, fg_color="transparent")
        method_frame.pack(fill="x", padx=8, pady=2)
        ctk.CTkRadioButton(method_frame, text="Нет", variable=self.auth_method_var, 
                          value="none", command=self._toggle_auth_fields).pack(anchor="w", padx=4)
        ctk.CTkRadioButton(method_frame, text="Из браузера", variable=self.auth_method_var,
                          value="browser", command=self._toggle_auth_fields).pack(anchor="w", padx=4)
        ctk.CTkRadioButton(method_frame, text="Файл cookies.txt", variable=self.auth_method_var,
                          value="file", command=self._toggle_auth_fields).pack(anchor="w", padx=4)
        
        self.browser_frame = ctk.CTkFrame(auth_frame, fg_color="transparent")
        self.browser_frame.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(self.browser_frame, text="Браузер:").pack(side="left", padx=(4, 6))
        self.browser_menu = ctk.CTkOptionMenu(self.browser_frame, values=[
            "chrome", "firefox", "edge", "brave", "chromium", "safari", "vivaldi", "opera"
        ], width=120)
        self.browser_menu.set("chrome")
        self.browser_menu.pack(side="left", padx=4)
        self.browser_profile_entry = ctk.CTkEntry(self.browser_frame, placeholder_text="Профиль (опционально)", width=150)
        self.browser_profile_entry.pack(side="left", padx=4)
        
        self.cookies_file_frame = ctk.CTkFrame(auth_frame, fg_color="transparent")
        self.cookies_file_frame.pack(fill="x", padx=8, pady=4)
        self.cookies_file_entry = ctk.CTkEntry(self.cookies_file_frame, placeholder_text="Путь к cookies.txt")
        self.cookies_file_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
        ctk.CTkButton(self.cookies_file_frame, text="…", width=30,
                      command=self._on_browse_cookies).pack(side="left", padx=4)
        
        self._toggle_auth_fields()  # Инициализация видимости
        
        ff_row = ctk.CTkFrame(right, fg_color="transparent")
        ff_row.pack(fill="x", padx=10, pady=(4, 10))
        ff_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ff_row, text="FFmpeg:").grid(row=0, column=0, sticky="w")
        self.ffmpeg_entry = ctk.CTkEntry(ff_row)
        self.ffmpeg_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(ff_row, text="…", width=40,
                      command=self._on_browse_ffmpeg).grid(row=1, column=1)

        # Кастомные аргументы — на всю ширину
        custom_frame = ctk.CTkFrame(self)
        custom_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=6)
        custom_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(custom_frame, text="🧩 Дополнительные аргументы yt-dlp (необязательно):",
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        self.custom_entry = ctk.CTkEntry(
            custom_frame, placeholder_text="например: --merge-output-format mkv --embed-chapters")
        self.custom_entry.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        ctrl = ctk.CTkFrame(custom_frame, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        ctrl.grid_columnconfigure(2, weight=1)
        self.download_btn = ctk.CTkButton(ctrl, text="⬇️ СКАЧАТЬ", width=200, height=36,
                                          font=ctk.CTkFont(size=15, weight="bold"),
                                          command=self._on_download)
        self.download_btn.grid(row=0, column=0, padx=(0, 8))
        self.stop_btn = ctk.CTkButton(ctrl, text="⏹ Стоп", width=110, height=36,
                                      fg_color="#C0392B", hover_color="#922B21",
                                      command=self._on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=8)
        self.status_label = ctk.CTkLabel(ctrl, text="Готов к работе", font=ctk.CTkFont(size=12))
        self.status_label.grid(row=0, column=2, sticky="w", padx=8)
        self.overall_bar = ctk.CTkProgressBar(ctrl, width=220)
        self.overall_bar.grid(row=0, column=3, padx=8)
        self.overall_bar.set(0)
        self.stats_label = ctk.CTkLabel(ctrl, text="0% • — • —", font=ctk.CTkFont(size=12))
        self.stats_label.grid(row=0, column=4, padx=8)

        # --- Низ: очередь + лог ---
        self.bottom_tabs = ctk.CTkTabview(self)
        bottom = self.bottom_tabs
        bottom.grid(row=4, column=0, sticky="nsew", padx=12, pady=(6, 12))
        bottom.add("📥 Очередь")
        bottom.add("📜 Лог")
        bottom.set("📥 Очередь")

        self.queue_frame = ctk.CTkScrollableFrame(bottom.tab("📥 Очередь"), height=180)
        self.queue_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.queue_frame.grid_columnconfigure(0, weight=1)

        self.log_text = ctk.CTkTextbox(bottom.tab("📜 Лог"), height=180)
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._log("Добро пожаловать! Вставьте ссылку и нажмите «Получить информацию» или сразу «СКАЧАТЬ».")

    # ============================ конфиг ============================
    def _collect_config(self) -> DownloadConfig:
        """Собрать текущие настройки GUI в DownloadConfig для передачи в ядро."""
        display = self.format_menu.get()
        core_fmt = DISPLAY_TO_CORE.get(display, "best")
        
        # Возвращаем НАСТОЯЩИЙ текст из поля custom_args, без добавления --cookies*
        # Аутентификация передаётся ТОЛЬКО через отдельные поля cookies_from_browser/cookies_file
        custom_args = self.custom_entry.get().strip()
        
        return DownloadConfig(
            output_dir=self.output_entry.get().strip() or default_downloads_dir(),
            format_choice=core_fmt,
            merge_format=self.merge_menu.get().strip() or "mp4",
            download_subs=bool(self.subs_var.get()),
            sub_langs=self.sub_langs_entry.get().strip() or "ru,en",
            sub_auto=bool(self.auto_sub_var.get()),
            embed_metadata=bool(self.meta_var.get()),
            embed_thumbnail=bool(self.thumb_var.get()),
            sponsorblock=bool(self.sponsor_var.get()),
            playlist_mode=bool(self.playlist_var.get()),
            custom_args=custom_args,
            ffmpeg_location=self.ffmpeg_entry.get().strip() or None,
            cookies_from_browser=self._get_cookies_from_browser(),
            cookies_file=self.cookies_file_entry.get().strip() if self.auth_method_var.get() == "file" else None,
        )

    def _get_cookies_from_browser(self) -> Optional[str]:
        """Вернуть строку cookies_from_browser если выбран метод браузера."""
        if self.auth_method_var.get() != "browser":
            return None
        browser = self.browser_menu.get()
        profile = self.browser_profile_entry.get().strip()
        return f"{browser}:{profile}" if profile else browser

    def _save_config(self) -> None:
        try:
            cfg = self._collect_config()
            data = {
                "output_dir": cfg.output_dir,
                "format_choice": cfg.format_choice,
                "merge_format": cfg.merge_format,
                "download_subs": cfg.download_subs,
                "sub_langs": cfg.sub_langs,
                "sub_auto": cfg.sub_auto,
                "embed_metadata": cfg.embed_metadata,
                "embed_thumbnail": cfg.embed_thumbnail,
                "sponsorblock": cfg.sponsorblock,
                "playlist_mode": cfg.playlist_mode,
                # Сырой текст поля, а НЕ cfg.custom_args: туда _collect_config
                # подмешивает сгенерированные --cookies*, иначе при каждой
                # загрузке они дублировались бы в поле после перезапуска.
                "custom_args": self.custom_entry.get().strip(),
                "ffmpeg_location": cfg.ffmpeg_location or "",
                "theme": self.theme_menu.get(),
                "auth_method": self.auth_method_var.get(),
                "browser": self.browser_menu.get(),
                "browser_profile": self.browser_profile_entry.get().strip(),
                "cookies_file": self.cookies_file_entry.get().strip(),
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_config(self) -> None:
        try:
            if not os.path.isfile(CONFIG_FILE):
                return
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, data.get("output_dir", default_downloads_dir()))
            self.format_menu.set(CORE_TO_DISPLAY.get(data.get("format_choice", "best"),
                                                     "⭐ Лучшее видео + аудио"))
            self.merge_menu.set(data.get("merge_format", "mp4"))
            self.subs_var.set(bool(data.get("download_subs", False)))
            self.sub_langs_entry.delete(0, "end")
            self.sub_langs_entry.insert(0, data.get("sub_langs", "ru,en"))
            self.auto_sub_var.set(bool(data.get("sub_auto", True)))
            self.meta_var.set(bool(data.get("embed_metadata", True)))
            self.thumb_var.set(bool(data.get("embed_thumbnail", True)))
            self.sponsor_var.set(bool(data.get("sponsorblock", False)))
            self.playlist_var.set(bool(data.get("playlist_mode", False)))
            self.custom_entry.delete(0, "end")
            self.custom_entry.insert(0, data.get("custom_args", ""))
            self.ffmpeg_entry.delete(0, "end")
            self.ffmpeg_entry.insert(0, data.get("ffmpeg_location", ""))
            
            # Загрузка настроек аутентификации
            self.auth_method_var.set(data.get("auth_method", "none"))
            self.browser_menu.set(data.get("browser", "chrome"))
            self.browser_profile_entry.delete(0, "end")
            self.browser_profile_entry.insert(0, data.get("browser_profile", ""))
            self.cookies_file_entry.delete(0, "end")
            self.cookies_file_entry.insert(0, data.get("cookies_file", ""))
            
            theme = data.get("theme", "Dark")
            if theme in ("Dark", "Light", "System"):
                self.theme_menu.set(theme)
                ctk.set_appearance_mode(theme)
            
            # Обновить видимость полей аутентификации
            self._toggle_auth_fields()
        except Exception:
            pass

    # ============================ хендлеры кнопок ============================
    def _on_theme_change(self, value: str) -> None:
        ctk.set_appearance_mode(value)

    def _create_context_menu(self) -> None:
        """Создание контекстного меню для текстового поля."""
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="📋 Вставить", command=self._on_paste)
        self.context_menu.add_command(label="✂️ Вырезать", command=self._on_cut)
        self.context_menu.add_command(label="📄 Копировать", command=self._on_copy)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🗑️ Удалить", command=self._on_delete)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="✅ Выделить всё", command=self._on_select_all)

    def _show_context_menu(self, event) -> None:
        """Показ контекстного меню по правой кнопке мыши."""
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _on_cut(self) -> None:
        """Вырезать выделенный текст."""
        try:
            text = self.url_text.get("sel.first", "sel.last")
            self.clipboard_clear()
            self.clipboard_append(text)
            self.url_text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass  # Нет выделения

    def _on_copy(self) -> None:
        """Копировать выделенный текст."""
        try:
            text = self.url_text.get("sel.first", "sel.last")
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass  # Нет выделения

    def _on_delete(self) -> None:
        """Удалить выделенный текст."""
        try:
            self.url_text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass  # Нет выделения

    def _on_select_all(self) -> None:
        """Выделить весь текст."""
        self.url_text.tag_add("sel", "1.0", "end")

    def _on_paste(self) -> None:
        try:
            text = self.clipboard_get()
        except Exception:
            self._log("⚠️ Буфер обмена пуст или недоступен.")
            return
        if text.strip():
            cur = self.url_text.get("1.0", "end").strip()
            sep = "\n" if cur else ""
            self.url_text.insert("end", sep + text.strip() + "\n")
            self._log("📋 Вставлено из буфера обмена.")
        else:
            self._log("⚠️ Буфер обмена пуст.")

    def _on_browse_output(self) -> None:
        d = filedialog.askdirectory(title="Выберите папку для сохранения")
        if d:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, d)

    def _on_open_folder(self) -> None:
        path = self.output_entry.get().strip() or default_downloads_dir()
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log(f"⚠️ Не удалось открыть папку: {e}")

    def _on_browse_ffmpeg(self) -> None:
        f = filedialog.askopenfilename(
            title="Укажите ffmpeg.exe",
            filetypes=[("ffmpeg", "ffmpeg*.exe"), ("Все файлы", "*.*")])
        if f:
            self.ffmpeg_entry.delete(0, "end")
            self.ffmpeg_entry.insert(0, f)
            self._refresh_ffmpeg_status()

    def _on_browse_cookies(self) -> None:
        f = filedialog.askopenfilename(
            title="Выберите файл cookies.txt",
            filetypes=[("Cookies", "*.txt"), ("Все файлы", "*.*")])
        if f:
            self.cookies_file_entry.delete(0, "end")
            self.cookies_file_entry.insert(0, f)

    def _toggle_auth_fields(self) -> None:
        """Показать/скрыть поля аутентификации в зависимости от выбранного метода."""
        method = self.auth_method_var.get()
        # Скрыть все сначала
        self.browser_frame.pack_forget()
        self.cookies_file_frame.pack_forget()
        
        if method == "browser":
            self.browser_frame.pack(fill="x", padx=8, pady=4)
        elif method == "file":
            self.cookies_file_frame.pack(fill="x", padx=8, pady=4)

    def _refresh_ffmpeg_status(self) -> None:
        found = find_ffmpeg(self.ffmpeg_entry.get().strip() or None)
        if found:
            self.ffmpeg_badge.configure(text=f"FFmpeg: ✅ {found[:60]}")
        else:
            self.ffmpeg_badge.configure(text="FFmpeg: ❌ не найден (слияние/MP3 не будет работать)")

    def _on_fetch_info(self) -> None:
        urls = parse_input_urls(self.url_text.get("1.0", "end"))
        if not urls:
            self._log("⚠️ Вставьте хотя бы одну ссылку.")
            return
        if self.info_worker and self.info_worker.is_alive():
            self._log("⏳ Получение информации уже идёт…")
            return
        self.fetched = []
        self.info_label.configure(text=f"🔎 Получение информации: {len(urls)} ссылок…")
        self.fetch_btn.configure(state="disabled")
        self._log(f"🔎 Получение информации: {len(urls)} ссылок…")
        
        # Собираем настройки аутентификации для fetch_info
        auth_method = self.auth_method_var.get()
        cookies_from_browser = None
        cookies_file = None
        
        if auth_method == "browser":
            browser = self.browser_menu.get()
            profile = self.browser_profile_entry.get().strip()
            cookies_from_browser = f"{browser}:{profile}" if profile else browser
        elif auth_method == "file":
            cookies_file = self.cookies_file_entry.get().strip()
            self._check_cookies_file(cookies_file)

        # Колбэки кладут события в очередь — GUI их разберёт в _poll_queue
        self.info_worker = InfoWorker(
            urls,
            playlist_mode=bool(self.playlist_var.get()),
            ffmpeg_location=self.ffmpeg_entry.get().strip() or None,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            on_info=lambda d: self.msg_queue.put(("info", d)),
            on_error=lambda u, e: self.msg_queue.put(("info_error", u, e)),
            on_done=lambda: self.msg_queue.put(("info_done",)),
        )
        self.info_worker.start()

    def _on_download(self) -> None:
        urls = parse_input_urls(self.url_text.get("1.0", "end"))
        if not urls:
            self._log("⚠️ Вставьте хотя бы одну ссылку.")
            return
        if self.dl_worker and self.dl_worker.is_alive():
            self._log("⏳ Загрузка уже идёт. Дождитесь завершения или нажмите «Стоп».")
            return
        cfg = self._collect_config()
        self._save_config()
        self._refresh_ffmpeg_status()
        if cfg.cookies_file:
            self._check_cookies_file(cfg.cookies_file)

        # Предупреждение про FFmpeg (не блокируем — yt-dlp сам сообщит в лог)
        if not find_ffmpeg(cfg.ffmpeg_location):
            if cfg.format_choice in ("best", "2160p", "1440p", "1080p", "720p") \
                    or cfg.format_choice.startswith("audio"):
                self._log("⚠️ FFmpeg не найден! Слияние 1080p+/MP3 может не получиться. "
                          "Скачайте ffmpeg.exe и укажите путь в настройках.")

        try:
            os.makedirs(cfg.output_dir, exist_ok=True)
        except Exception as e:
            self._log(f"❌ Не удалось создать папку {cfg.output_dir}: {e}")
            return

        # Создаём карточки очереди
        for url in urls:
            self._ensure_task_row(url)

        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.overall_bar.set(0)
        self._log(f"🚀 Старт загрузки: {len(urls)} ссылок | формат={cfg.format_choice} | "
                  f"плейлист={'да' if cfg.playlist_mode else 'нет'}")

        self.dl_worker = DownloadWorker(
            urls, cfg,
            on_log=lambda m: self.msg_queue.put(("log", m)),
            on_progress=lambda p: self.msg_queue.put(("progress", p)),
            on_status=lambda u, s: self.msg_queue.put(("status", u, s)),
            on_file_done=lambda u, fp: self.msg_queue.put(("file_done", u, fp)),
            on_all_done=lambda: self.msg_queue.put(("all_done",)),
            on_error=lambda u, e: self.msg_queue.put(("dl_error", u, e)),
        )
        self.dl_worker.start()

    def _on_stop(self) -> None:
        if self.dl_worker and self.dl_worker.is_alive():
            self.dl_worker.stop()
            self._log("⏹ Запрошена остановка… (текущий фрагмент докачается)")
        else:
            self._log("ℹ️ Нет активной загрузки.")

    def _on_close(self) -> None:
        try:
            self._save_config()
            if self.dl_worker and self.dl_worker.is_alive():
                self.dl_worker.stop()
        finally:
            self.destroy()

    # ============================ очередь задач (карточки) ============================
    def _ensure_task_row(self, url: str) -> None:
        if url in self.task_widgets:
            w = self.task_widgets[url]
            try:
                w["status"].configure(text="⏳ В очереди…")
                w["bar"].set(0)
            except Exception:
                pass
            return
        card = ctk.CTkFrame(self.queue_frame)
        card.pack(fill="x", padx=4, pady=4)
        card.grid_columnconfigure(0, weight=1)
        title = ctk.CTkLabel(card, text=url, font=ctk.CTkFont(size=12, weight="bold"),
                             wraplength=850, justify="left")
        title.grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))
        status = ctk.CTkLabel(card, text="⏳ В очереди…", font=ctk.CTkFont(size=12))
        status.grid(row=1, column=0, sticky="w", padx=10)
        bar = ctk.CTkProgressBar(card, width=860)
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 8))
        bar.set(0)
        self.task_widgets[url] = {"frame": card, "title": title, "status": status, "bar": bar}

    # ============================ лог ============================
    def _log(self, text: str) -> None:
        """Прямой лог из GUI-потока."""
        try:
            self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
            # Ограничиваем размер, чтобы не съесть память
            try:
                lines = int(self.log_text.index("end-1c").split(".")[0])
                if lines > 3000:
                    self.log_text.delete("1.0", f"{lines - 3000}.0")
            except Exception:
                pass
        except Exception:
            pass

    # ============================ мост потоков -> GUI ============================
    def _show_log_tab(self) -> None:
        """Переключить нижние вкладки на лог (только для ошибок)."""
        try:
            self.bottom_tabs.set("📜 Лог")
        except Exception:
            pass

    def _check_cookies_file(self, path: Optional[str]) -> None:
        """Предупредить в лог, если cookies.txt не содержит входа в YouTube.

        Не блокирует запуск: проверка эвристическая, последнее слово за движком.
        """
        try:
            ok, detail = check_youtube_login((path or "").strip() or None)
            if ok:
                self._log(f"🔐 Куки в порядке: {detail}.")
            else:
                self._log(f"⚠️ Проверка cookies.txt: {detail}. "
                          f"Без живого входа бот-чек YouTube не снимется.")
                self._show_log_tab()
        except Exception:
            pass

    def _poll_queue(self) -> None:
        """Выполняется в GUI-потоке каждые 100 мс. Единственное место,
        где фоновые события касаются виджетов."""
        try:
            while True:
                try:
                    msg = self.msg_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_msg(msg)
                except Exception as e:  # GUI не должен падать из-за одного сообщения
                    try:
                        self._log(f"⚠️ Ошибка обработки события: {type(e).__name__}: {e}")
                    except Exception:
                        pass
        finally:
            self.after(100, self._poll_queue)

    def _handle_msg(self, msg: tuple) -> None:
        kind = msg[0]

        if kind == "log":
            self._log(str(msg[1]))

        elif kind == "status":
            _, url, text = msg
            self.status_label.configure(text=f"{url[:60]}… — {text}" if len(url) > 60 else text)
            if url in self.task_widgets:
                self.task_widgets[url]["status"].configure(text=text)
            self._log(f"{text} [{url[:80]}]")

        elif kind == "progress":
            p: Dict[str, Any] = msg[1]
            url = p.get("url", "")
            hook_status = p.get("hook_status")
            percent = float(p.get("percent") or 0)
            frac = max(0.0, min(1.0, percent / 100.0))
            speed = human_speed(p.get("speed"))
            eta = human_eta(p.get("eta"))
            if url in self.task_widgets:
                self.task_widgets[url]["bar"].set(frac)
                if hook_status == "downloading":
                    total_s = human_size(p.get("total"))
                    dl_s = human_size(p.get("downloaded"))
                    self.task_widgets[url]["status"].configure(
                        text=f"⬇️ {percent:.1f}% • {dl_s}/{total_s} • {speed} • ETA {eta}")
            self.overall_bar.set(frac)
            self.stats_label.configure(text=f"{percent:.1f}% • {speed} • ETA {eta}")
            if hook_status == "finished":
                self.status_label.configure(text="🔀 Обработка/слияние (FFmpeg)…")
                if url in self.task_widgets:
                    self.task_widgets[url]["status"].configure(
                        text="🔀 Скачано, идёт обработка (слияние/конвертация)…")
                    self.task_widgets[url]["bar"].set(1.0)

        elif kind == "info":
            d: Dict[str, Any] = msg[1]
            self.fetched.append(d)
            if d.get("is_playlist"):
                line = f"📑 {d.get('title')} — треков: {d.get('playlist_count')}"
            else:
                author = f" — {d['uploader']}" if d.get("uploader") else ""
                line = f"🎬 {d.get('title')}{author} [{d.get('duration_str')}]"
            # Обновляем карточку и общий лейбл
            self._ensure_task_row(d["url"])
            try:
                self.task_widgets[d["url"]]["title"].configure(text=line)
                self.task_widgets[d["url"]]["status"].configure(text="ℹ️ Информация получена")
            except Exception:
                pass
            lines = [f"• {x.get('title', x.get('url'))}" for x in self.fetched[-8:]]
            self.info_label.configure(text="\n".join(lines))
            self._log(f"ℹ️ {line}")

        elif kind == "info_error":
            _, url, err = msg
            self._log(f"❌ Не удалось получить информацию [{url}]: {err}")
            self._ensure_task_row(url)
            try:
                self.task_widgets[url]["status"].configure(text=f"❌ Ошибка инфо: {err[:120]}")
            except Exception:
                pass
            self._show_log_tab()  # ошибка должна быть ВИДНА, а не прятаться во вкладке

        elif kind == "info_done":
            self.fetch_btn.configure(state="normal")
            if self.fetched:
                self.info_label.configure(
                    text=f"✅ Найдено: {len(self.fetched)}. Проверьте настройки и жмите «СКАЧАТЬ».")
            else:
                self.info_label.configure(text="⚠️ Ничего не найдено. Проверьте ссылки.")
            self._log("✅ Получение информации завершено.")

        elif kind == "file_done":
            _, url, _fp = msg
            if url in self.task_widgets:
                self.task_widgets[url]["bar"].set(1.0)
                self.task_widgets[url]["status"].configure(text="✅ Готово")

        elif kind == "dl_error":
            _, url, err = msg
            self._log(f"❌ Ошибка [{url}]: {err}")
            self._show_log_tab()

        elif kind == "all_done":
            self.download_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.status_label.configure(text="Готов к работе")
            self._log("🏁 Все загрузки завершены.")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    # Нужно для PyInstaller onefile-сборки на Windows
    import multiprocessing

    multiprocessing.freeze_support()
    main()
