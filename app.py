"""Multi-mailbox Tkinter manager for rootsh.com temporary mail."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import string
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable
from urllib.parse import urlsplit

from cf_browser import (
    CreativeFabricaBrowser,
    browser_display_name,
    browser_was_closed,
    debugger_address_is_live,
    existing_debugger_address,
    manual_browser_command,
)
from donut_browser import (
    DEFAULT_DONUT_API_URL,
    DonutBrowserClient,
    DonutBrowserSession,
)
from outlook_client import OutlookClient, OutlookCredentials, OutlookError, parse_outlook_import
from rootsh_client import InboxUpdate, Mailbox, Message, RootshClient, html_to_text
from secret_store import SecretStoreError, protect_secret, unprotect_secret
from virtual_browser import (
    VirtualBrowserError,
    VirtualBrowserPool,
    find_browser_executable,
)


BG = "#f3f6fb"
CARD = "#ffffff"
INK = "#172033"
MUTED = "#667085"
ACCENT = "#4f46e5"
SUCCESS = "#16a34a"
WARNING = "#d97706"
DANGER = "#dc2626"
BROWSER_CHOICES = ("Brave", "Chrome", "Donut", "VirtualBrowser")
ISOLATED_SESSION_MODE = "隔离会话"
REUSED_SESSION_MODE = "复用会话"
BROWSER_SESSION_MODE_CHOICES = (ISOLATED_SESSION_MODE, REUSED_SESSION_MODE)
LEGACY_BROWSER_SESSION_MODES = {
    "独立": ISOLATED_SESSION_MODE,
    "共享": REUSED_SESSION_MODE,
}


def normalize_browser_session_mode(value: object) -> str:
    """Return the current session-mode name while accepting saved legacy names."""
    selected = str(value).strip()
    selected = LEGACY_BROWSER_SESSION_MODES.get(selected, selected)
    return (
        selected
        if selected in BROWSER_SESSION_MODE_CHOICES
        else ISOLATED_SESSION_MODE
    )


@dataclass(slots=True)
class ManagedMailbox:
    key: str
    client: RootshClient | OutlookClient
    address: str
    local_part: str
    domain: str
    expires_at: float
    cursor: int = 0
    provider: str = "rootsh"
    points: int | None = None
    registered: bool = False
    donut_profile_id: str | None = None
    donut_debugger_address: str | None = None
    virtual_worker_id: str | None = None
    messages: dict[str, Message] = field(default_factory=dict)
    status: str = "等待收件"
    busy: bool = False
    next_poll_at: float = 0.0

    @property
    def remaining_seconds(self) -> int:
        if self.provider == "outlook":
            return 0
        return max(0, int(self.expires_at - time.monotonic()))

    @property
    def expired(self) -> bool:
        if self.provider == "outlook":
            return False
        return self.remaining_seconds <= 0

    @property
    def remaining_text(self) -> str:
        if self.provider == "outlook":
            return "长期"
        minutes, seconds = divmod(self.remaining_seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"


class TemporaryMailManagerApp:
    def __init__(self, root: tk.Tk, *, connect: bool = True) -> None:
        self.root = root
        self.root.title("临时邮箱管理器")
        self.root.geometry("1220x800")
        self.root.minsize(980, 650)
        self.root.configure(bg=BG)

        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="mailbox")
        self.mailboxes: dict[str, ManagedMailbox] = {}
        self.mailbox_browser_buttons: dict[
            str, tuple[ttk.Button, ttk.Button]
        ] = {}
        self.mailbox_background_buttons: dict[str, ttk.Button] = {}
        self.browser_drivers: dict[str, Any] = {}
        self.browser_workers: set[str] = set()
        self.browser_trackers: dict[str, threading.Thread] = {}
        self.reused_registration_queue: deque[str] = deque()
        self.reused_registration_paused_key: str | None = None
        self.registration_outcomes: dict[str, str] = {}
        self.registration_last_result = ""
        self.registration_last_kind = "info"
        self.shared_browser_driver: Any | None = None
        self.shared_browser_owner: str | None = None
        self.virtual_worker_owners: dict[str, str] = {}
        self.manual_browser_processes: dict[str, subprocess.Popen[Any]] = {}
        self.active_key: str | None = None
        self.closed = False
        self.tick_after_id: str | None = None
        self.creating = False
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        self.app_data_root = local_app_data / "TemporaryEmailPickup"
        self.browser_profile_root = self.app_data_root / "browser_profiles"
        self.settings_path = self.app_data_root / "settings.json"
        self.mailboxes_path = self.app_data_root / "mailboxes.json"
        self.settings = self._load_settings()
        self._window_normal_bounds: dict[str, tuple[int, int, int, int]] = {}
        self._restore_window_state(self.root, "main")
        self.root.bind(
            "<Configure>",
            lambda event: self._remember_window_bounds(event, self.root, "main"),
            add="+",
        )

        saved_browser = str(self.settings.get("browser", "Brave")).strip().title()
        if saved_browser not in BROWSER_CHOICES:
            saved_browser = "Brave"
        self.browser_var = tk.StringVar(value=saved_browser)
        raw_session_mode = self.settings.get(
            "browser_session_mode", ISOLATED_SESSION_MODE
        )
        saved_session_mode = normalize_browser_session_mode(raw_session_mode)
        if raw_session_mode != saved_session_mode:
            self.settings["browser_session_mode"] = saved_session_mode
            self._write_settings()
        self.browser_session_mode_var = tk.StringVar(value=saved_session_mode)
        self.domain_var = tk.StringVar(value="bccto.cc")
        self.address_var = tk.StringVar(value="请选择或新建一个邮箱")
        self.detail_var = tk.StringVar(
            value=(
                "所有邮箱依次复用同一个浏览器会话"
                if saved_session_mode == REUSED_SESSION_MODE
                else "每个邮箱均使用隔离会话"
            )
        )
        self.status_var = tk.StringVar(value="准备就绪")
        self.summary_var = tk.StringVar(value="0 个邮箱 · 0 封邮件")
        self.registration_feedback_var = tk.StringVar(value="后台注册：暂无任务")

        self._configure_styles()
        self._build_ui()
        if connect:
            self._restore_mailboxes()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._tick()
        self.root.after(150, self._restore_split_state)
        if connect:
            self._discover_domains()

    @staticmethod
    def _random_local() -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "mail" + "".join(secrets.choice(alphabet) for _ in range(8))

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("Address.TLabel", background=CARD, foreground=INK, font=("Consolas", 17, "bold"))
        style.configure("Info.TLabel", background=CARD, foreground=ACCENT, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 9))
        style.map("Accent.TButton", background=[("active", "#4338ca"), ("!disabled", ACCENT)], foreground=[("!disabled", "white")])
        style.configure("Danger.TButton", font=("Microsoft YaHei UI", 9), padding=(10, 7))
        style.map("Danger.TButton", foreground=[("!disabled", DANGER)])
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 7))
        style.configure("Browser.TButton", font=("Microsoft YaHei UI", 8, "bold"), padding=(2, 1))
        style.configure("Treeview", rowheight=31, font=("Microsoft YaHei UI", 9), background=CARD, fieldbackground=CARD)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=(7, 7))
        style.map("Treeview", background=[("selected", "#e0e7ff")], foreground=[("selected", INK)])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(26, 22, 26, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 16))
        title_block = ttk.Frame(header)
        title_block.pack(side="left")
        ttk.Label(title_block, text="临时邮箱管理器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_block, text="同时管理多个相互独立的十分钟邮箱", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        create_block = ttk.Frame(header)
        create_block.pack(side="right", anchor="s")
        ttk.Label(create_block, text="浏览器", style="Subtitle.TLabel").pack(side="left", padx=(0, 7))
        self.browser_box = ttk.Combobox(
            create_block,
            textvariable=self.browser_var,
            state="readonly",
            width=9,
            values=BROWSER_CHOICES,
        )
        self.browser_box.pack(side="left", padx=(0, 6))
        self.browser_box.bind("<<ComboboxSelected>>", self._browser_choice_changed)
        ttk.Label(create_block, text="会话策略", style="Subtitle.TLabel").pack(side="left", padx=(0, 5))
        self.browser_session_mode_box = ttk.Combobox(
            create_block,
            textvariable=self.browser_session_mode_var,
            state="readonly",
            width=8,
            values=BROWSER_SESSION_MODE_CHOICES,
        )
        self.browser_session_mode_box.pack(side="left", padx=(0, 12))
        self.browser_session_mode_box.bind(
            "<<ComboboxSelected>>", self._browser_session_mode_changed
        )
        self.donut_api_button = ttk.Button(
            create_block,
            text="Donut API",
            command=self._configure_donut_api,
        )
        self.donut_api_button.pack(side="left", padx=(0, 12))
        self.virtual_pool_button = ttk.Button(
            create_block, text="VB worker 池", command=self._configure_virtual_pool
        )
        self.virtual_pool_button.pack(side="left", padx=(0, 12))
        ttk.Label(create_block, text="邮箱域名", style="Subtitle.TLabel").pack(side="left", padx=(0, 7))
        self.domain_box = ttk.Combobox(create_block, textvariable=self.domain_var, state="readonly", width=16, values=("bccto.cc",), font=("Consolas", 10))
        self.domain_box.pack(side="left", padx=(0, 8))
        self.create_button = ttk.Button(create_block, text="＋ 新建随机邮箱", style="Accent.TButton", command=self.create_random_mailbox)
        self.create_button.pack(side="left")
        self.import_outlook_button = ttk.Button(create_block, text="导入 Outlook", command=self.show_outlook_import)
        self.import_outlook_button.pack(side="left", padx=(8, 0))

        self.main_pane = ttk.Panedwindow(outer, orient="horizontal")
        self.main_pane.pack(fill="both", expand=True)
        self.main_pane.bind("<ButtonRelease-1>", self._split_released)

        left = ttk.Frame(self.main_pane, style="Card.TFrame", padding=14)
        left_header = ttk.Frame(left, style="Card.TFrame")
        left_header.pack(fill="x", pady=(0, 10))
        ttk.Label(left_header, text="邮箱列表", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ttk.Label(left_header, textvariable=self.summary_var, style="Card.TLabel", foreground=MUTED).pack(side="right")

        self.mailbox_tree = ttk.Treeview(left, columns=("address", "remaining", "count", "points", "status", "background", "browser"), show="headings", selectmode="extended")
        self.mailbox_tree.heading("address", text="邮箱地址")
        self.mailbox_tree.heading("remaining", text="剩余")
        self.mailbox_tree.heading("count", text="邮件")
        self.mailbox_tree.heading("points", text="积分")
        self.mailbox_tree.heading("status", text="状态")
        self.mailbox_tree.heading("background", text="后台注册")
        self.mailbox_tree.heading("browser", text="浏览器打开")
        self.mailbox_tree.column("address", width=190, minwidth=140)
        self.mailbox_tree.column("remaining", width=62, minwidth=55, anchor="center")
        self.mailbox_tree.column("count", width=50, minwidth=46, anchor="center")
        self.mailbox_tree.column("points", width=72, minwidth=60, anchor="center")
        self.mailbox_tree.column("status", width=76, minwidth=60, anchor="center")
        self.mailbox_tree.column("background", width=82, minwidth=72, anchor="center")
        self.mailbox_tree.column("browser", width=132, minwidth=118, anchor="center")
        self.mailbox_scroll = ttk.Scrollbar(left, orient="vertical", command=self._mailbox_yview)
        self.mailbox_tree.configure(yscrollcommand=self._mailbox_scroll_set)
        self.mailbox_tree.pack(side="left", fill="both", expand=True)
        self.mailbox_scroll.pack(side="right", fill="y")
        self.mailbox_tree.bind("<<TreeviewSelect>>", self._mailbox_selected)
        self.mailbox_tree.bind("<Double-1>", self._mailbox_double_click)
        self.mailbox_tree.bind("<Configure>", lambda _event: self.root.after_idle(self._layout_browser_buttons))
        self.main_pane.add(left, weight=4)

        right = ttk.Frame(self.main_pane, padding=(14, 0, 0, 0))
        detail_card = ttk.Frame(right, style="Card.TFrame", padding=16)
        detail_card.pack(fill="x")
        info_row = ttk.Frame(detail_card, style="Card.TFrame")
        info_row.pack(fill="x")
        info_text = ttk.Frame(info_row, style="Card.TFrame")
        info_text.pack(side="left", fill="x", expand=True)
        ttk.Label(info_text, textvariable=self.address_var, style="Address.TLabel").pack(anchor="w")
        ttk.Label(info_text, textvariable=self.detail_var, style="Info.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(info_row, text="仅在验证码阶段自动取件", style="Card.TLabel", foreground=MUTED).pack(side="right")

        actions = ttk.Frame(detail_card, style="Card.TFrame")
        actions.pack(fill="x", pady=(15, 0))
        self.copy_button = ttk.Button(actions, text="复制邮箱", command=self.copy_selected)
        self.copy_button.pack(side="left")
        self.browser_button = ttk.Button(actions, text="自动打开浏览器", command=self.open_selected_browser)
        self.browser_button.pack(side="left", padx=(7, 0))
        self.renew_button = ttk.Button(actions, text="续期 10 分钟", command=self.renew_selected)
        self.renew_button.pack(side="left", padx=7)
        self.refresh_button = ttk.Button(actions, text="立即取件", command=self.refresh_selected)
        self.refresh_button.pack(side="left")
        self.download_all_button = ttk.Button(actions, text="下载全部", command=self.download_selected_mailbox)
        self.download_all_button.pack(side="left", padx=7)
        self.destroy_button = ttk.Button(actions, text="删除选中邮箱", style="Danger.TButton", command=self.destroy_selected_mailboxes)
        self.destroy_button.pack(side="right")

        inbox_header = ttk.Frame(right, padding=(0, 14, 0, 8))
        inbox_header.pack(fill="x")
        ttk.Label(inbox_header, text="当前邮箱的收件箱", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.delete_button = ttk.Button(inbox_header, text="删除选中邮件", command=self.delete_selected_messages)
        self.delete_button.pack(side="right")

        self.content_pane = ttk.Panedwindow(right, orient="vertical")
        self.content_pane.pack(fill="both", expand=True)
        self.content_pane.bind("<ButtonRelease-1>", self._split_released)
        inbox_card = ttk.Frame(self.content_pane, style="Card.TFrame", padding=1)
        self.message_tree = ttk.Treeview(inbox_card, columns=("sender", "subject", "size", "time"), show="headings", selectmode="extended")
        self.message_tree.heading("sender", text="发件人")
        self.message_tree.heading("subject", text="主题")
        self.message_tree.heading("size", text="大小")
        self.message_tree.heading("time", text="时间")
        self.message_tree.column("sender", width=220, minwidth=130)
        self.message_tree.column("subject", width=370, minwidth=180)
        self.message_tree.column("size", width=74, minwidth=55, anchor="center")
        self.message_tree.column("time", width=135, minwidth=105, anchor="center")
        message_scroll = ttk.Scrollbar(inbox_card, orient="vertical", command=self.message_tree.yview)
        self.message_tree.configure(yscrollcommand=message_scroll.set)
        self.message_tree.pack(side="left", fill="both", expand=True)
        message_scroll.pack(side="right", fill="y")
        self.message_tree.bind("<<TreeviewSelect>>", self._message_selected)
        self.message_tree.bind("<Double-1>", lambda _event: self.view_selected_message())
        self.content_pane.add(inbox_card, weight=3)

        preview_card = ttk.Frame(self.content_pane, style="Card.TFrame", padding=11)
        preview_header = ttk.Frame(preview_card, style="Card.TFrame")
        preview_header.pack(fill="x", pady=(0, 7))
        ttk.Label(preview_header, text="邮件预览", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.save_message_button = ttk.Button(preview_header, text="保存原始邮件", command=self.save_selected_message)
        self.save_message_button.pack(side="right")
        self.view_message_button = ttk.Button(preview_header, text="读取正文", command=self.view_selected_message)
        self.view_message_button.pack(side="right", padx=7)
        self.preview = tk.Text(preview_card, height=8, wrap="word", relief="flat", bg=CARD, fg=INK, padx=5, pady=5, font=("Microsoft YaHei UI", 10))
        self.preview.insert("1.0", "从左侧选择一个邮箱，邮件会显示在这里。")
        self.preview.configure(state="disabled")
        self.preview.pack(fill="both", expand=True)
        self.content_pane.add(preview_card, weight=2)
        self.main_pane.add(right, weight=6)

        registration_feedback = ttk.Frame(
            outer, style="Card.TFrame", padding=(12, 8)
        )
        registration_feedback.pack(fill="x", pady=(10, 0))
        ttk.Label(
            registration_feedback,
            text="后台注册状态",
            style="Card.TLabel",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.registration_feedback_label = ttk.Label(
            registration_feedback,
            textvariable=self.registration_feedback_var,
            style="Card.TLabel",
            foreground=MUTED,
        )
        self.registration_feedback_label.pack(side="left", padx=(12, 0))

        footer = ttk.Frame(outer, padding=(0, 10, 0, 0))
        footer.pack(fill="x")
        ttk.Label(footer, text="●", foreground=SUCCESS).pack(side="left")
        ttk.Label(footer, textvariable=self.status_var, style="Subtitle.TLabel").pack(side="left", padx=(5, 0))
        ttk.Label(footer, text="可切换隔离会话或复用会话", style="Subtitle.TLabel").pack(side="right")
        self._update_action_states()

    def _load_settings(self) -> dict[str, Any]:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _split_released(self, _event: tk.Event[Any] | None = None) -> None:
        self.root.after_idle(self._save_split_state)

    def _write_settings(self) -> bool:
        try:
            self.app_data_root.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.settings_path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _screen_rectangles(window: tk.Misc) -> list[tuple[int, int, int, int]]:
        """Return monitor work areas, including monitors left/above the primary one."""
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                class MonitorInfo(ctypes.Structure):
                    _fields_ = (
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    )

                rectangles: list[tuple[int, int, int, int]] = []
                callback_type = ctypes.WINFUNCTYPE(
                    wintypes.BOOL,
                    wintypes.HANDLE,
                    wintypes.HDC,
                    ctypes.POINTER(wintypes.RECT),
                    wintypes.LPARAM,
                )

                def collect_monitor(
                    monitor: int,
                    _device_context: int,
                    _monitor_rect: Any,
                    _data: int,
                ) -> bool:
                    info = MonitorInfo()
                    info.cbSize = ctypes.sizeof(MonitorInfo)
                    if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                        work = info.rcWork
                        rectangles.append(
                            (
                                int(work.left),
                                int(work.top),
                                int(work.right - work.left),
                                int(work.bottom - work.top),
                            )
                        )
                    return True

                callback = callback_type(collect_monitor)
                ctypes.windll.user32.EnumDisplayMonitors(0, 0, callback, 0)
                if rectangles:
                    return rectangles
            except (AttributeError, OSError, TypeError, ValueError):
                pass

        try:
            return [
                (
                    int(window.winfo_vrootx()),
                    int(window.winfo_vrooty()),
                    int(window.winfo_vrootwidth()),
                    int(window.winfo_vrootheight()),
                )
            ]
        except tk.TclError:
            return [(0, 0, 1220, 800)]

    @staticmethod
    def _fit_window_bounds(
        bounds: tuple[int, int, int, int],
        screens: list[tuple[int, int, int, int]],
        minimum_size: tuple[int, int],
    ) -> tuple[int, int, int, int]:
        x, y, width, height = bounds
        minimum_width, minimum_height = minimum_size
        usable_screens = [screen for screen in screens if screen[2] > 0 and screen[3] > 0]
        if not usable_screens:
            usable_screens = [(0, 0, max(width, minimum_width), max(height, minimum_height))]

        def overlap_area(screen: tuple[int, int, int, int]) -> int:
            sx, sy, sw, sh = screen
            overlap_width = max(0, min(x + width, sx + sw) - max(x, sx))
            overlap_height = max(0, min(y + height, sy + sh) - max(y, sy))
            return overlap_width * overlap_height

        target = max(usable_screens, key=overlap_area)
        if overlap_area(target) == 0:
            center_x = x + width / 2
            center_y = y + height / 2
            target = min(
                usable_screens,
                key=lambda screen: (
                    center_x - (screen[0] + screen[2] / 2)
                ) ** 2
                + (center_y - (screen[1] + screen[3] / 2)) ** 2,
            )

        screen_x, screen_y, screen_width, screen_height = target
        width = min(max(width, minimum_width), screen_width)
        height = min(max(height, minimum_height), screen_height)
        x = min(max(x, screen_x), screen_x + screen_width - width)
        y = min(max(y, screen_y), screen_y + screen_height - height)
        return x, y, width, height

    def _restore_window_state(self, window: tk.Toplevel | tk.Tk, key: str) -> bool:
        windows = self.settings.get("windows")
        saved = windows.get(key) if isinstance(windows, dict) else None
        if not isinstance(saved, dict):
            return False
        try:
            bounds = (
                int(saved["x"]),
                int(saved["y"]),
                int(saved["width"]),
                int(saved["height"]),
            )
            minimum_size = (int(window.minsize()[0]), int(window.minsize()[1]))
            fitted = self._fit_window_bounds(
                bounds,
                self._screen_rectangles(window),
                minimum_size,
            )
            x, y, width, height = fitted
            window.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self._window_normal_bounds[key] = fitted
            if bool(saved.get("maximized")):
                window.after_idle(lambda: window.state("zoomed"))
            return True
        except (KeyError, tk.TclError, TypeError, ValueError):
            return False

    def _remember_window_bounds(
        self,
        event: tk.Event[Any],
        window: tk.Toplevel | tk.Tk,
        key: str,
    ) -> None:
        if event.widget is not window:
            return
        try:
            if window.state() == "normal" and event.width > 1 and event.height > 1:
                self._window_normal_bounds[key] = (
                    int(window.winfo_x()),
                    int(window.winfo_y()),
                    int(event.width),
                    int(event.height),
                )
        except tk.TclError:
            return

    def _save_window_state(self, window: tk.Toplevel | tk.Tk, key: str) -> None:
        try:
            state = window.state()
            if state == "normal":
                bounds = (
                    int(window.winfo_x()),
                    int(window.winfo_y()),
                    int(window.winfo_width()),
                    int(window.winfo_height()),
                )
                self._window_normal_bounds[key] = bounds
            else:
                bounds = self._window_normal_bounds.get(key)
            if not bounds:
                return
            x, y, width, height = bounds
            windows = self.settings.setdefault("windows", {})
            if not isinstance(windows, dict):
                windows = {}
                self.settings["windows"] = windows
            windows[key] = {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "maximized": state == "zoomed",
            }
            self._write_settings()
        except (tk.TclError, TypeError, ValueError):
            return

    @staticmethod
    def _normalize_donut_api_url(value: str) -> str:
        candidate = value.strip().rstrip("/") or DEFAULT_DONUT_API_URL
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Donut API 地址必须是本机 HTTP 地址，例如 http://127.0.0.1:10108")
        try:
            port = parsed.port or 80
        except ValueError as exc:
            raise ValueError("Donut API 端口无效") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Donut API 端口无效")
        return candidate

    def _donut_api_url(self) -> str:
        configured = os.environ.get("DONUT_API_URL") or str(
            self.settings.get("donut_api_url") or DEFAULT_DONUT_API_URL
        )
        return self._normalize_donut_api_url(configured)

    def _donut_api_token(self) -> str:
        environment_token = os.environ.get("DONUT_API_KEY", "").strip()
        if environment_token:
            return environment_token
        protected = str(self.settings.get("donut_api_token") or "")
        if not protected:
            raise RuntimeError("尚未配置 Donut API Token，请点击顶部“Donut API”。")
        try:
            token = unprotect_secret(protected).strip()
        except SecretStoreError as exc:
            raise RuntimeError("Donut API Token 无法解密，请重新配置。") from exc
        if not token:
            raise RuntimeError("Donut API Token 为空，请重新配置。")
        return token

    def _new_donut_client(self) -> DonutBrowserClient:
        return DonutBrowserClient(
            api_url=self._donut_api_url(),
            token=self._donut_api_token(),
        )

    def _configure_donut_api(self) -> None:
        current_url = str(
            self.settings.get("donut_api_url") or DEFAULT_DONUT_API_URL
        )
        api_url = simpledialog.askstring(
            "Donut API 设置",
            "本地 API 地址：",
            initialvalue=current_url,
            parent=self.root,
        )
        if api_url is None:
            return
        try:
            normalized_url = self._normalize_donut_api_url(api_url)
        except ValueError as exc:
            messagebox.showerror("Donut API 地址无效", str(exc), parent=self.root)
            return

        has_saved_token = bool(self.settings.get("donut_api_token"))
        token = simpledialog.askstring(
            "Donut API 设置",
            (
                "粘贴“设置 → 集成 → 本地 API”中的 Token。\n"
                + ("留空可保留当前 Token。" if has_saved_token else "Token 不会以明文保存。")
            ),
            show="*",
            parent=self.root,
        )
        if token is None:
            return
        token = token.strip()
        if not token and not has_saved_token:
            messagebox.showerror("缺少 Donut API Token", "请输入 API Token。", parent=self.root)
            return
        try:
            if token:
                self.settings["donut_api_token"] = protect_secret(token)
            self.settings["donut_api_url"] = normalized_url
        except SecretStoreError as exc:
            messagebox.showerror("Donut API 设置保存失败", str(exc), parent=self.root)
            return
        if self._write_settings():
            self.status_var.set("Donut API 设置已安全保存")
        else:
            messagebox.showerror("Donut API 设置保存失败", "无法写入 settings.json。", parent=self.root)

    def _virtual_pool_size(self) -> int:
        try:
            return max(1, int(self.settings.get("virtual_browser_pool_size", 3)))
        except (TypeError, ValueError):
            return 3

    def _configure_virtual_pool(self) -> None:
        size = simpledialog.askinteger(
            "VirtualBrowser worker 池",
            "固定 worker 数量（按最小环境 ID 参与轮转）：",
            initialvalue=self._virtual_pool_size(),
            minvalue=1,
            maxvalue=200,
            parent=self.root,
        )
        if size is None:
            return
        self.settings["virtual_browser_pool_size"] = size
        if self._write_settings():
            self.status_var.set(f"VirtualBrowser worker 池已设置为 {size} 个")

    def _acquire_virtual_worker(self, mailbox: ManagedMailbox) -> tuple[Path, Path, str]:
        pool = VirtualBrowserPool(pool_size=self._virtual_pool_size())
        owned_worker_id = self.virtual_worker_owners.get(mailbox.key)
        if owned_worker_id:
            for worker in pool.workers():
                if worker.worker_id == owned_worker_id:
                    return find_browser_executable(), worker.profile_path, worker.worker_id
            self._release_virtual_worker(mailbox.key)
        usage = self.settings.get("virtual_browser_worker_usage", {})
        if not isinstance(usage, dict):
            usage = {}
        numeric_usage = {
            str(worker_id): float(value)
            for worker_id, value in usage.items()
            if isinstance(value, (int, float))
        }
        worker = pool.acquire(
            occupied_worker_ids=self.virtual_worker_owners.values(),
            last_used_at=numeric_usage,
        )
        self.virtual_worker_owners[mailbox.key] = worker.worker_id
        mailbox.virtual_worker_id = worker.worker_id
        numeric_usage[worker.worker_id] = time.time()
        self.settings["virtual_browser_worker_usage"] = numeric_usage
        self._write_settings()
        self._save_mailboxes()
        return find_browser_executable(), worker.profile_path, worker.worker_id

    def _acquire_shared_virtual_worker(self) -> tuple[Path, Path, str]:
        """Use one stable VirtualBrowser environment for the shared session."""
        workers = VirtualBrowserPool(pool_size=self._virtual_pool_size()).workers()
        if not workers:
            raise VirtualBrowserError("未找到可用的 VirtualBrowser worker 环境。")
        selected_id = str(self.settings.get("shared_virtual_browser_worker_id", ""))
        worker = next(
            (item for item in workers if item.worker_id == selected_id), workers[0]
        )
        if worker.worker_id != selected_id:
            self.settings["shared_virtual_browser_worker_id"] = worker.worker_id
            self._write_settings()
        return find_browser_executable(), worker.profile_path, worker.worker_id

    def _release_virtual_worker(self, key: str) -> None:
        self.virtual_worker_owners.pop(key, None)

    def _browser_choice_changed(self, _event: tk.Event[Any] | None = None) -> None:
        selected = self.browser_var.get()
        previous = str(self.settings.get("browser", "Brave")).strip().title()
        if previous not in BROWSER_CHOICES:
            previous = "Brave"
        if selected not in BROWSER_CHOICES:
            self.browser_var.set(previous)
            return
        if selected == previous:
            return
        if (
            self.browser_workers
            or self.reused_registration_queue
            or self.reused_registration_paused_key
        ):
            self.browser_var.set(previous)
            messagebox.showwarning(
                "无法切换浏览器",
                "浏览器任务正在运行或排队，请等待注册队列结束后再切换。",
                parent=self.root,
            )
            return

        live_keys: list[str] = []
        for key, driver in list(self.browser_drivers.items()):
            try:
                if driver.window_handles:
                    live_keys.append(key)
                else:
                    self.browser_drivers.pop(key, None)
            except Exception:
                self.browser_drivers.pop(key, None)
        if live_keys:
            self.browser_var.set(previous)
            messagebox.showwarning(
                "无法切换浏览器",
                "请先关闭本程序已打开的浏览器窗口，再切换全局浏览器。",
                parent=self.root,
            )
            return

        self.settings["browser"] = selected
        if self._write_settings():
            if (
                selected == "Donut"
                and not os.environ.get("DONUT_API_KEY")
                and not self.settings.get("donut_api_token")
            ):
                self.status_var.set("已切换为 Donut；请点击“Donut API”配置本地 Token")
            else:
                self.status_var.set(f"全局浏览器已切换为 {selected}")
        else:
            self.status_var.set(f"已选择 {selected}，但设置保存失败")

    def _browser_session_mode_changed(
        self, _event: tk.Event[Any] | None = None
    ) -> None:
        selected = self.browser_session_mode_var.get()
        previous = normalize_browser_session_mode(
            self.settings.get("browser_session_mode", ISOLATED_SESSION_MODE)
        )
        if selected not in BROWSER_SESSION_MODE_CHOICES:
            self.browser_session_mode_var.set(previous)
            return
        if selected == previous:
            return
        if (
            self.browser_workers
            or self.reused_registration_queue
            or self.reused_registration_paused_key
            or self._has_live_browser_drivers()
        ):
            self.browser_session_mode_var.set(previous)
            messagebox.showwarning(
                "无法切换浏览器会话",
                "请先关闭本程序已打开的浏览器窗口，并等待注册队列结束后再切换。",
                parent=self.root,
            )
            return
        self.settings["browser_session_mode"] = selected
        self.shared_browser_driver = None
        self.shared_browser_owner = None
        self._update_active_header()
        if self._write_settings():
            self.status_var.set(
                "已启用复用会话；登录或注册前会先退出当前账户。"
                if selected == REUSED_SESSION_MODE
                else "已启用隔离会话。"
            )
        else:
            self.status_var.set(f"已选择“{selected}”，但设置保存失败")

    def _has_live_browser_drivers(self) -> bool:
        for key, driver in list(self.browser_drivers.items()):
            try:
                if driver.window_handles:
                    return True
                self.browser_drivers.pop(key, None)
            except Exception:
                self.browser_drivers.pop(key, None)
        if self.shared_browser_driver is not None:
            try:
                if self.shared_browser_driver.window_handles:
                    return True
            except Exception:
                pass
            self.shared_browser_driver = None
            self.shared_browser_owner = None
        return False

    def _shared_browser_mode(self) -> bool:
        return self.browser_session_mode_var.get() == REUSED_SESSION_MODE

    def _save_split_state(self) -> None:
        if self.closed:
            return
        try:
            main_width = self.main_pane.winfo_width()
            content_height = self.content_pane.winfo_height()
            if main_width < 50 or content_height < 50:
                return
            main_ratio = self.main_pane.sashpos(0) / main_width
            content_ratio = self.content_pane.sashpos(0) / content_height
            self.settings["split"] = {
                "mailbox_detail_ratio": max(0.02, min(0.98, main_ratio)),
                "inbox_preview_ratio": max(0.02, min(0.98, content_ratio)),
            }
            self._write_settings()
        except (OSError, tk.TclError, TypeError, ValueError):
            return

    def _restore_split_state(self) -> None:
        split = self.settings.get("split")
        if not isinstance(split, dict):
            return
        try:
            main_ratio = float(split.get("mailbox_detail_ratio", 0.4))
            content_ratio = float(split.get("inbox_preview_ratio", 0.6))
            main_width = self.main_pane.winfo_width()
            content_height = self.content_pane.winfo_height()
            if main_width >= 50 and 0.02 <= main_ratio <= 0.98:
                self.main_pane.sashpos(0, int(main_width * main_ratio))
            if content_height >= 50 and 0.02 <= content_ratio <= 0.98:
                self.content_pane.sashpos(0, int(content_height * content_ratio))
        except (tk.TclError, TypeError, ValueError):
            return

    @staticmethod
    def _message_to_dict(message: Message) -> dict[str, str]:
        return {
            "sender_name": message.sender_name,
            "sender_address": message.sender_address,
            "subject": message.subject,
            "received_at": message.received_at,
            "message_id": message.message_id,
            "size": message.size,
        }

    def _save_mailboxes(self) -> None:
        if self.closed:
            return
        try:
            rows: list[dict[str, Any]] = []
            for mailbox in self.mailboxes.values():
                row: dict[str, Any] = {
                    "key": mailbox.key,
                    "provider": mailbox.provider,
                    "address": mailbox.address,
                    "local_part": mailbox.local_part,
                    "domain": mailbox.domain,
                    "expires_at": (
                        None
                        if mailbox.provider == "outlook"
                        else time.time() + mailbox.remaining_seconds
                    ),
                    "cursor": mailbox.cursor,
                    "points": mailbox.points,
                    "registered": mailbox.registered,
                    "donut_profile_id": mailbox.donut_profile_id,
                    "donut_debugger_address": mailbox.donut_debugger_address,
                    "virtual_worker_id": mailbox.virtual_worker_id,
                    "messages": [
                        self._message_to_dict(message)
                        for message in mailbox.messages.values()
                    ],
                }
                if mailbox.provider == "outlook":
                    credentials = mailbox.client.credentials
                    row["outlook_secret"] = protect_secret(credentials.to_secret_json())
                else:
                    row["cookies"] = mailbox.client.export_cookies()
                rows.append(row)
            payload = {
                "version": 3,
                "saved_at": time.time(),
                "mailboxes": rows,
            }
            self.app_data_root.mkdir(parents=True, exist_ok=True)
            temporary = self.mailboxes_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.mailboxes_path)
        except (OSError, TypeError, ValueError, SecretStoreError) as exc:
            if not self.closed:
                self.status_var.set(f"邮箱数据保存失败：{exc}")

    def _restore_mailboxes(self) -> None:
        try:
            payload = json.loads(self.mailboxes_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as exc:
            self.status_var.set(f"邮箱数据读取失败：{exc}")
            return
        rows = payload.get("mailboxes", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return

        restored = 0
        restore_failed = 0
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            try:
                key = str(raw["key"])
                address = str(raw["address"])
                local_part = str(raw["local_part"])
                domain = str(raw["domain"])
                provider = str(raw.get("provider") or "rootsh")
                if provider not in {"rootsh", "outlook"}:
                    continue
                expires_epoch = float(raw.get("expires_at") or 0)
                cursor = int(raw.get("cursor", 0))
                raw_points = raw.get("points")
                points = int(raw_points) if raw_points is not None else None
                registered = bool(raw.get("registered")) or points is not None
                donut_profile_id = str(raw.get("donut_profile_id") or "") or None
                donut_debugger_address = (
                    str(raw.get("donut_debugger_address") or "") or None
                )
                virtual_worker_id = str(raw.get("virtual_worker_id") or "") or None
                if not key or not address or "@" not in address or key in self.mailboxes:
                    continue
                if provider == "outlook":
                    secret = unprotect_secret(str(raw.get("outlook_secret") or ""))
                    credentials = OutlookCredentials.from_secret_json(address, secret)
                    client: RootshClient | OutlookClient = OutlookClient(credentials)
                else:
                    client = RootshClient()
                    cookies = raw.get("cookies", [])
                    if isinstance(cookies, list):
                        client.import_cookies(cookies)
                messages: dict[str, Message] = {}
                for item in raw.get("messages", []):
                    if not isinstance(item, dict):
                        continue
                    message = Message(
                        sender_name=str(item.get("sender_name") or ""),
                        sender_address=str(item.get("sender_address") or ""),
                        subject=str(item.get("subject") or "(无主题)"),
                        received_at=str(item.get("received_at") or ""),
                        message_id=str(item.get("message_id") or ""),
                        size=str(item.get("size") or ""),
                    )
                    if message.message_id:
                        messages[message.message_id] = message
                remaining = (
                    0.0 if provider == "outlook" else max(0.0, expires_epoch - time.time())
                )
                mailbox = ManagedMailbox(
                    key=key,
                    client=client,
                    address=address,
                    local_part=local_part,
                    domain=domain,
                    expires_at=time.monotonic() + remaining,
                    cursor=cursor,
                    provider=provider,
                    points=points,
                    registered=registered,
                    donut_profile_id=donut_profile_id,
                    donut_debugger_address=donut_debugger_address,
                    virtual_worker_id=virtual_worker_id,
                    messages=messages,
                    status=(
                        (
                            f"已注册 · {points:,} 积分"
                            if points is not None
                            else "已注册"
                        )
                        if registered
                        else (
                            "Outlook 已恢复"
                            if provider == "outlook"
                            else ("已恢复" if remaining else "已到期")
                        )
                    ),
                    next_poll_at=time.monotonic(),
                )
            except (KeyError, TypeError, ValueError, SecretStoreError, json.JSONDecodeError):
                restore_failed += 1
                continue
            self.mailboxes[key] = mailbox
            self.mailbox_tree.insert(
                "",
                "end",
                iid=key,
                values=self._mailbox_values(mailbox),
            )
            self._create_browser_cell_button(mailbox)
            restored += 1

        if restored:
            first_key = next(iter(self.mailboxes))
            self.mailbox_tree.selection_set(first_key)
            self.mailbox_tree.focus(first_key)
            self._activate_mailbox(first_key)
            self.status_var.set(
                f"已恢复 {restored} 个邮箱"
                + (f"，{restore_failed} 个凭据无法恢复" if restore_failed else "")
            )
        elif restore_failed:
            self.status_var.set(f"有 {restore_failed} 个邮箱凭据无法恢复")

    def _submit(
        self,
        task: Callable[[], Any],
        on_success: Callable[[Any], None],
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.closed:
            return
        future = self.executor.submit(task)

        def finished(done: Future[Any]) -> None:
            if not self.closed:
                self.root.after(0, lambda: self._finish_future(done, on_success, on_error))

        future.add_done_callback(finished)

    def _finish_future(
        self,
        future: Future[Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:
            if on_error:
                on_error(exc)
            else:
                self.status_var.set(f"操作失败：{exc}")
                messagebox.showerror("操作失败", str(exc), parent=self.root)
        else:
            on_success(result)

    def _discover_domains(self) -> None:
        def task() -> list[str]:
            client = RootshClient()
            try:
                return client.bootstrap()
            finally:
                client.close()

        self.status_var.set("正在读取可用域名…")
        self._submit(task, self._on_domains, on_error=lambda exc: self.status_var.set(f"域名读取失败，将使用 bccto.cc：{exc}"))

    def _on_domains(self, domains: list[str]) -> None:
        self.domain_box.configure(values=tuple(domains))
        if self.domain_var.get() not in domains:
            self.domain_var.set(domains[0])
        self.status_var.set("准备就绪，点击“新建随机邮箱”开始")

    def show_outlook_import(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("批量导入 Outlook 邮箱")
        dialog.geometry("780x430")
        dialog.minsize(640, 340)
        self._restore_window_state(dialog, "outlook_import")
        dialog.bind(
            "<Configure>",
            lambda event: self._remember_window_bounds(
                event, dialog, "outlook_import"
            ),
            add="+",
        )
        dialog.transient(self.root)
        dialog.grab_set()

        def close_dialog() -> None:
            self._save_window_state(dialog, "outlook_import")
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="每行一个账号，格式：邮箱----密码----客户端ID----刷新令牌",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="导入时会验证收件权限；密码和刷新令牌使用 Windows DPAPI 加密保存。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 10))

        editor_frame = ttk.Frame(frame)
        editor_frame.pack(fill="both", expand=True)
        editor = tk.Text(editor_frame, wrap="none", font=("Consolas", 9), undo=True)
        y_scroll = ttk.Scrollbar(editor_frame, orient="vertical", command=editor.yview)
        x_scroll = ttk.Scrollbar(editor_frame, orient="horizontal", command=editor.xview)
        editor.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        editor.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="取消", command=close_dialog).pack(side="right")

        def submit() -> None:
            source = editor.get("1.0", "end")
            try:
                accounts = parse_outlook_import(source)
            except OutlookError as exc:
                messagebox.showerror("导入格式错误", str(exc), parent=dialog)
                return
            existing = {mailbox.address.lower() for mailbox in self.mailboxes.values()}
            accounts = [item for item in accounts if item.address.lower() not in existing]
            if not accounts:
                messagebox.showinfo("无需导入", "这些邮箱已经在列表中。", parent=dialog)
                return
            close_dialog()
            self._import_outlook_accounts(accounts)

        ttk.Button(actions, text="验证并导入", style="Accent.TButton", command=submit).pack(side="right", padx=(0, 8))
        editor.focus_set()

    def _import_outlook_accounts(self, accounts: list[OutlookCredentials]) -> None:
        self.import_outlook_button.configure(state="disabled")
        self.status_var.set(f"正在验证 {len(accounts)} 个 Outlook 邮箱…")

        def task() -> tuple[
            list[tuple[OutlookClient, InboxUpdate]],
            list[tuple[str, str]],
        ]:
            imported: list[tuple[OutlookClient, InboxUpdate]] = []
            failed: list[tuple[str, str]] = []
            for credentials in accounts:
                client = OutlookClient(credentials)
                try:
                    client.validate()
                    update = InboxUpdate(0, credentials.address, ())
                except Exception as exc:
                    client.close()
                    failed.append((credentials.address, str(exc)))
                else:
                    imported.append((client, update))
            return imported, failed

        def success(
            result: tuple[
                list[tuple[OutlookClient, InboxUpdate]],
                list[tuple[str, str]],
            ]
        ) -> None:
            imported, failed = result
            self.import_outlook_button.configure(state="normal")
            first_key: str | None = None
            for client, update in imported:
                credentials = client.credentials
                key = uuid.uuid4().hex
                mailbox = ManagedMailbox(
                    key=key,
                    client=client,
                    address=credentials.address,
                    local_part=credentials.address.split("@", 1)[0],
                    domain=credentials.address.rsplit("@", 1)[-1],
                    expires_at=time.monotonic(),
                    cursor=update.cursor,
                    provider="outlook",
                    messages={message.message_id: message for message in update.messages},
                    status=f"Outlook {client.mode}",
                    next_poll_at=0.0,
                )
                self.mailboxes[key] = mailbox
                self.mailbox_tree.insert("", "end", iid=key, values=self._mailbox_values(mailbox))
                self._create_browser_cell_button(mailbox)
                first_key = first_key or key
            self._save_mailboxes()
            if first_key:
                self.mailbox_tree.selection_set(first_key)
                self.mailbox_tree.focus(first_key)
                self.mailbox_tree.see(first_key)
                self._activate_mailbox(first_key)
            self.status_var.set(f"已导入 {len(imported)} 个 Outlook 邮箱" + (f"，{len(failed)} 个失败" if failed else ""))
            if failed:
                details = "\n".join(f"{address}：{error}" for address, error in failed)
                messagebox.showwarning("部分 Outlook 邮箱导入失败", details, parent=self.root)

        def failure(exc: Exception) -> None:
            self.import_outlook_button.configure(state="normal")
            self.status_var.set(f"Outlook 导入失败：{exc}")
            messagebox.showerror("Outlook 导入失败", str(exc), parent=self.root)

        self._submit(task, success, on_error=failure)

    def create_random_mailbox(self) -> None:
        if self.creating:
            return
        self.creating = True
        self.create_button.configure(state="disabled")
        requested_domain = self.domain_var.get().strip() or "bccto.cc"
        local_part = self._random_local()
        self.status_var.set(f"正在创建 {local_part}@{requested_domain}…")

        def task() -> tuple[RootshClient, Mailbox, str, str]:
            client = RootshClient()
            try:
                domains = client.bootstrap()
                domain = requested_domain if requested_domain in domains else domains[0]
                mailbox = client.apply_mailbox(local_part, domain)
                return client, mailbox, local_part, domain
            except Exception:
                client.close()
                raise

        self._submit(task, self._on_mailbox_created, on_error=self._create_error)

    def _create_error(self, exc: Exception) -> None:
        self.creating = False
        self.status_var.set(f"邮箱创建失败：{exc}")
        messagebox.showerror("邮箱创建失败", str(exc), parent=self.root)
        self.root.after(2_500, lambda: self.create_button.configure(state="normal") if not self.closed else None)

    def _on_mailbox_created(self, result: tuple[RootshClient, Mailbox, str, str]) -> None:
        self.creating = False
        self.root.after(2_200, lambda: self.create_button.configure(state="normal") if not self.closed else None)
        client, mailbox, local_part, domain = result
        key = uuid.uuid4().hex
        managed = ManagedMailbox(
            key=key,
            client=client,
            address=mailbox.address,
            local_part=local_part,
            domain=domain,
            expires_at=time.monotonic() + mailbox.lifetime_seconds,
            next_poll_at=time.monotonic(),
        )
        self.mailboxes[key] = managed
        self.mailbox_tree.insert("", "end", iid=key, values=self._mailbox_values(managed))
        self._create_browser_cell_button(managed)
        self.mailbox_tree.selection_set(key)
        self.mailbox_tree.focus(key)
        self.mailbox_tree.see(key)
        self._activate_mailbox(key)
        self.status_var.set(f"已创建 {mailbox.address}")
        self._save_mailboxes()
        if mailbox.notice:
            messagebox.showinfo("邮箱提示", mailbox.notice, parent=self.root)

    def _mailbox_values(self, mailbox: ManagedMailbox) -> tuple[str, str, str, str, str, str, str]:
        status = mailbox.status
        if mailbox.expired and not mailbox.busy:
            status = "已到期"
        points = f"{mailbox.points:,}" if mailbox.points is not None else "—"
        return mailbox.address, mailbox.remaining_text, str(len(mailbox.messages)), points, status, "", ""

    def _tick(self) -> None:
        self._reap_manual_browser_processes()
        self._start_next_reused_registration()
        total_messages = 0
        total_points = 0
        for key, mailbox in list(self.mailboxes.items()):
            total_messages += len(mailbox.messages)
            total_points += mailbox.points or 0
            if self.mailbox_tree.exists(key):
                self.mailbox_tree.item(key, values=self._mailbox_values(mailbox))
        self.summary_var.set(f"{len(self.mailboxes)} 个邮箱 · {total_messages} 封邮件 · {total_points:,} 积分")
        self._update_active_header()
        self._layout_browser_buttons()
        if not self.closed:
            self.tick_after_id = self.root.after(1_000, self._tick)

    def _mailbox_selected(self, _event: tk.Event[Any] | None = None) -> None:
        selected = self.mailbox_tree.selection()
        focus = self.mailbox_tree.focus()
        active = focus if focus in selected else (selected[-1] if selected else None)
        self._activate_mailbox(active)

    def _mailbox_yview(self, *args: Any) -> None:
        self.mailbox_tree.yview(*args)
        self.root.after_idle(self._layout_browser_buttons)

    def _mailbox_scroll_set(self, first: str, last: str) -> None:
        self.mailbox_scroll.set(first, last)
        self.root.after_idle(self._layout_browser_buttons)

    def _create_browser_cell_button(self, mailbox: ManagedMailbox) -> None:
        background_button = ttk.Button(
            self.mailbox_tree,
            text="后台注册",
            style="Browser.TButton",
            command=lambda key=mailbox.key: self._background_register_key(key),
        )
        automatic_button = ttk.Button(
            self.mailbox_tree,
            text="自动打开",
            style="Browser.TButton",
            command=lambda key=mailbox.key: self._open_browser_key(key, manual=False),
        )
        manual_button = ttk.Button(
            self.mailbox_tree,
            text="手动打开",
            style="Browser.TButton",
            command=lambda key=mailbox.key: self._open_browser_key(key, manual=True),
        )
        self.mailbox_background_buttons[mailbox.key] = background_button
        self.mailbox_browser_buttons[mailbox.key] = (
            automatic_button,
            manual_button,
        )
        self.root.after_idle(self._layout_browser_buttons)

    def _layout_browser_buttons(self) -> None:
        if self.closed:
            return
        for key, button in list(self.mailbox_background_buttons.items()):
            if key not in self.mailboxes or not self.mailbox_tree.exists(key):
                button.destroy()
                self.mailbox_background_buttons.pop(key, None)
                continue
            if self.mailboxes[key].registered:
                button.place_forget()
                continue
            bounds = self.mailbox_tree.bbox(key, "background")
            if not bounds:
                button.place_forget()
                continue
            x, y, width, height = bounds
            queued_position = self._reused_registration_position(key)
            if key in self.browser_workers:
                button_text = "注册中"
            elif key == self.reused_registration_paused_key:
                button_text = "等待验证"
            elif queued_position is not None:
                button_text = f"排队中 {queued_position}"
            else:
                button_text = "后台注册"
            button.configure(
                state=(
                    "disabled"
                    if key in self.browser_workers
                    or key == self.reused_registration_paused_key
                    or queued_position is not None
                    else "normal"
                ),
                text=button_text,
            )
            button.place(x=x + 3, y=y + 3, width=max(58, width - 6), height=max(22, height - 6))
        for key, buttons in list(self.mailbox_browser_buttons.items()):
            if key not in self.mailboxes or not self.mailbox_tree.exists(key):
                for button in buttons:
                    button.destroy()
                self.mailbox_browser_buttons.pop(key, None)
                continue
            bounds = self.mailbox_tree.bbox(key, "browser")
            if not bounds:
                for button in buttons:
                    button.place_forget()
                continue
            x, y, width, height = bounds
            if self._shared_browser_mode() and self.reused_registration_paused_key:
                state = (
                    "normal"
                    if key == self.reused_registration_paused_key
                    and key not in self.browser_workers
                    else "disabled"
                )
            else:
                state = (
                    "disabled"
                    if key in self.browser_workers
                    or (
                        self._shared_browser_mode()
                        and bool(
                            self.browser_workers or self.reused_registration_queue
                        )
                    )
                    else "normal"
                )
            gap = 3
            available_width = max(104, width - 6)
            automatic_width = (available_width - gap) // 2
            manual_width = available_width - gap - automatic_width
            automatic_button, manual_button = buttons
            automatic_button.configure(state=state)
            manual_button.configure(state=state)
            automatic_button.place(
                x=x + 3,
                y=y + 3,
                width=automatic_width,
                height=max(22, height - 6),
            )
            manual_button.place(
                x=x + 3 + automatic_width + gap,
                y=y + 3,
                width=manual_width,
                height=max(22, height - 6),
            )

    def _background_register_key(self, key: str) -> None:
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        self.mailbox_tree.selection_add(key)
        self.mailbox_tree.focus(key)
        self._activate_mailbox(key)
        self.background_register(mailbox)

    def _open_browser_key(self, key: str, *, manual: bool = False) -> None:
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        self.mailbox_tree.selection_add(key)
        self.mailbox_tree.focus(key)
        self._activate_mailbox(key)
        if manual:
            self._open_manual_browser(mailbox)
        else:
            self.open_browser(mailbox)

    def _mailbox_double_click(self, event: tk.Event[Any]) -> None:
        key = self.mailbox_tree.identify_row(event.y)
        column = self.mailbox_tree.identify_column(event.x)
        if not key or key not in self.mailboxes:
            return
        self.mailbox_tree.selection_set(key)
        self._activate_mailbox(key)
        if column == "#6":
            self.background_register(self.mailboxes[key])
        elif column == "#7":
            self.open_browser(self.mailboxes[key])
        else:
            self.refresh_mailbox(self.mailboxes[key], quiet=False)

    def _activate_mailbox(self, key: str | None) -> None:
        self.active_key = key if key in self.mailboxes else None
        self._render_active_messages()
        self._update_active_header()
        self._update_action_states()

    def _active_mailbox(self) -> ManagedMailbox | None:
        return self.mailboxes.get(self.active_key or "")

    def _update_active_header(self) -> None:
        mailbox = self._active_mailbox()
        if not mailbox:
            self.address_var.set("请选择或新建一个邮箱")
            self.detail_var.set(
                "所有邮箱依次复用同一个浏览器会话"
                if self._shared_browser_mode()
                else "每个邮箱均使用隔离会话"
            )
            return
        self.address_var.set(mailbox.address)
        points = f"{mailbox.points:,} 积分" if mailbox.points is not None else "积分待更新"
        state = "已到期，可单独续期" if mailbox.expired else f"剩余 {mailbox.remaining_text} · {len(mailbox.messages)} 封邮件 · {points} · {mailbox.status}"
        self.detail_var.set(state)

    def _render_active_messages(self) -> None:
        for item in self.message_tree.get_children():
            self.message_tree.delete(item)
        mailbox = self._active_mailbox()
        if not mailbox:
            self._set_preview("从左侧选择一个邮箱，邮件会显示在这里。")
            return
        for message in mailbox.messages.values():
            self.message_tree.insert("", 0, iid=message.message_id, values=(message.sender, message.subject, message.size, message.received_at))
        self._set_preview("双击邮件或点击“读取正文”查看内容。" if mailbox.messages else "当前邮箱还没有邮件。")

    def _update_action_states(self) -> None:
        mailbox = self._active_mailbox()
        mailbox_state = "normal" if mailbox and not mailbox.busy else "disabled"
        for button in (self.copy_button, self.browser_button, self.refresh_button, self.download_all_button):
            button.configure(state=mailbox_state)
        self.renew_button.configure(
            state=(
                "normal"
                if mailbox and not mailbox.busy and mailbox.provider == "rootsh"
                else "disabled"
            )
        )
        selected_mailboxes = [self.mailboxes[key] for key in self.mailbox_tree.selection() if key in self.mailboxes]
        queued_keys = set(self.reused_registration_queue)
        can_destroy = bool(selected_mailboxes) and all(
            not item.busy
            and item.key not in self.browser_workers
            and item.key not in queued_keys
            and item.key != self.reused_registration_paused_key
            for item in selected_mailboxes
        )
        self.destroy_button.configure(
            state="normal" if can_destroy else "disabled",
            text=f"删除选中邮箱 ({len(selected_mailboxes)})" if len(selected_mailboxes) > 1 else "删除选中邮箱",
        )
        selected_messages = self.message_tree.selection()
        one_message = mailbox and not mailbox.busy and len(selected_messages) == 1
        any_messages = mailbox and not mailbox.busy and mailbox.provider == "rootsh" and bool(selected_messages)
        self.view_message_button.configure(state="normal" if one_message else "disabled")
        self.save_message_button.configure(state="normal" if one_message else "disabled")
        self.delete_button.configure(state="normal" if any_messages else "disabled")

    def copy_selected(self) -> None:
        mailbox = self._active_mailbox()
        if not mailbox:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(mailbox.address)
        self.status_var.set(f"已复制 {mailbox.address}")

    def _find_browser(self) -> Path:
        selected = self.browser_var.get()
        if selected == "Donut":
            raise RuntimeError("Donut 由本地 API 启动，不使用浏览器可执行文件路径。")
        if selected == "VirtualBrowser":
            return find_browser_executable()
        if selected == "Chrome":
            executable = "chrome.exe"
            vendor_parts = ("Google", "Chrome")
        else:
            selected = "Brave"
            executable = "brave.exe"
            vendor_parts = ("BraveSoftware", "Brave-Browser")
        candidates = [
            shutil.which(executable),
            os.path.join(
                os.environ.get("PROGRAMFILES", ""),
                *vendor_parts,
                "Application",
                executable,
            ),
            os.path.join(
                os.environ.get("PROGRAMFILES(X86)", ""),
                *vendor_parts,
                "Application",
                executable,
            ),
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                *vendor_parts,
                "Application",
                executable,
            ),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise FileNotFoundError(f"未找到所选的 {selected}，请先安装或切换全局浏览器。")

    def _browser_profile_path(self, key: str, browser_path: Path) -> Path:
        if browser_display_name(browser_path) == "Chrome":
            root = self.browser_profile_root
        else:
            slug = "".join(
                char
                for char in browser_path.stem.lower()
                if char.isalnum() or char in "-_"
            ) or "chromium"
            root = self.app_data_root / f"browser_profiles_{slug}"
        return root / ("shared" if self._shared_browser_mode() else key)

    def _remember_donut_session(
        self,
        key: str,
        profile_id: str,
        debugger_address: str | None = None,
    ) -> None:
        if self._shared_browser_mode():
            self.settings["shared_donut_profile_id"] = profile_id
            if debugger_address is not None:
                self.settings["shared_donut_debugger_address"] = debugger_address
            self._write_settings()
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        mailbox.donut_profile_id = profile_id
        if debugger_address is not None:
            mailbox.donut_debugger_address = debugger_address
        self._save_mailboxes()

    def _clear_donut_debugger(
        self,
        key: str,
        expected_address: str | None = None,
    ) -> None:
        if self._shared_browser_mode():
            if (
                expected_address is None
                or self.settings.get("shared_donut_debugger_address")
                == expected_address
            ):
                self.settings.pop("shared_donut_debugger_address", None)
                self._write_settings()
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        if (
            expected_address is None
            or mailbox.donut_debugger_address == expected_address
        ):
            mailbox.donut_debugger_address = None
            self._save_mailboxes()

    def open_selected_browser(self) -> None:
        mailbox = self._active_mailbox()
        if mailbox:
            self.open_browser(mailbox)

    def _reused_registration_position(self, key: str) -> int | None:
        try:
            return tuple(self.reused_registration_queue).index(key) + 1
        except ValueError:
            return None

    def _update_registration_feedback(self) -> None:
        counts = {
            kind: sum(1 for value in self.registration_outcomes.values() if value == kind)
            for kind in ("success", "failed", "manual", "unknown")
        }
        active_addresses = [
            self.mailboxes[key].address
            for key in self.browser_workers
            if key in self.mailboxes
            and self.registration_outcomes.get(key) == "running"
        ]
        if len(active_addresses) == 1:
            headline = f"正在处理 {active_addresses[0]}"
            kind = "info"
        elif active_addresses:
            headline = f"正在并行处理 {len(active_addresses)} 个账户"
            kind = "info"
        elif self.reused_registration_paused_key:
            mailbox = self.mailboxes.get(self.reused_registration_paused_key)
            headline = (
                (
                    f"{mailbox.address} 已注册，等待人工验证积分"
                    if mailbox.registered
                    else f"等待 {mailbox.address} 完成人工验证"
                )
                if mailbox
                else "注册队列正在等待人工验证"
            )
            kind = "manual"
        elif self.registration_last_result:
            headline = self.registration_last_result
            kind = self.registration_last_kind
        elif self.reused_registration_queue:
            headline = "等待开始后台注册"
            kind = "info"
        else:
            headline = "暂无任务"
            kind = "info"

        summary_parts = []
        if self.reused_registration_queue:
            summary_parts.append(f"排队 {len(self.reused_registration_queue)}")
        if counts["success"]:
            summary_parts.append(f"成功 {counts['success']}")
        if counts["failed"]:
            summary_parts.append(f"失败 {counts['failed']}")
        if counts["manual"]:
            summary_parts.append(f"待验证 {counts['manual']}")
        if counts["unknown"]:
            summary_parts.append(f"未确认 {counts['unknown']}")
        suffix = f" · {' / '.join(summary_parts)}" if summary_parts else ""
        self.registration_feedback_var.set(f"{headline}{suffix}")

        colors = {
            "success": SUCCESS,
            "failed": DANGER,
            "manual": WARNING,
            "unknown": WARNING,
            "info": ACCENT,
        }
        if hasattr(self, "registration_feedback_label"):
            self.registration_feedback_label.configure(
                foreground=colors.get(kind, MUTED)
            )

    def _record_registration_outcome(
        self,
        mailbox: ManagedMailbox,
        kind: str,
        result: str,
    ) -> None:
        self.registration_outcomes[mailbox.key] = kind
        self.registration_last_kind = kind
        compact_result = " ".join(result.split())
        self.registration_last_result = (
            compact_result
            if len(compact_result) <= 120
            else f"{compact_result[:117]}…"
        )
        self._update_registration_feedback()

    def _refresh_reused_registration_queue_statuses(self) -> None:
        for position, key in enumerate(self.reused_registration_queue, start=1):
            mailbox = self.mailboxes.get(key)
            if mailbox:
                mailbox.status = f"后台注册排队（第 {position} 位）"
        self._layout_browser_buttons()
        self._update_registration_feedback()

    def _reused_registration_window_is_open(self) -> bool:
        if self.shared_browser_driver is not None:
            try:
                if self.shared_browser_driver.window_handles:
                    return True
            except Exception:
                pass
            self.shared_browser_driver = None
            self.shared_browser_owner = None
        return any(
            process.poll() is None
            for process in self.manual_browser_processes.values()
        )

    def _start_next_reused_registration(self) -> None:
        if (
            self.closed
            or not self._shared_browser_mode()
            or self.browser_workers
            or self.reused_registration_paused_key
            or self._reused_registration_window_is_open()
        ):
            return
        while self.reused_registration_queue:
            key = self.reused_registration_queue.popleft()
            mailbox = self.mailboxes.get(key)
            self._refresh_reused_registration_queue_statuses()
            if not mailbox or mailbox.registered:
                continue
            self._start_background_register(mailbox)
            if key in self.browser_workers:
                return

    def background_register(self, mailbox: ManagedMailbox) -> None:
        """Queue or start a hidden login/registration task for one mailbox."""
        if mailbox.registered:
            self.status_var.set(f"{mailbox.address} 已完成注册")
            return
        if not self._shared_browser_mode():
            self._start_background_register(mailbox)
            return
        if mailbox.key in self.browser_workers:
            self.status_var.set(f"{mailbox.address} 的登录/注册任务正在运行")
            return
        queued_position = self._reused_registration_position(mailbox.key)
        if queued_position is not None:
            self.status_var.set(
                f"{mailbox.address} 已在后台注册队列第 {queued_position} 位"
            )
            return
        if mailbox.key == self.reused_registration_paused_key:
            self.status_var.set(
                f"{mailbox.address} 正在等待人工验证，请点击“自动打开”继续"
            )
            return
        self.reused_registration_queue.append(mailbox.key)
        self.registration_outcomes[mailbox.key] = "queued"
        position = len(self.reused_registration_queue)
        self._refresh_reused_registration_queue_statuses()
        self.status_var.set(
            f"已将 {mailbox.address} 加入后台注册队列（第 {position} 位）"
        )
        self._start_next_reused_registration()

    def _start_background_register(self, mailbox: ManagedMailbox) -> None:
        """Start one hidden registration worker and persist its Studio points."""
        if mailbox.key in self.browser_workers:
            self.status_var.set(f"{mailbox.address} 的登录/注册任务正在运行")
            return
        if self._shared_browser_mode() and self.shared_browser_driver is not None:
            self.status_var.set("复用会话窗口已打开，后台注册队列将在窗口关闭后继续。")
            return
        donut_client: DonutBrowserClient | None = None
        virtual_worker_id: str | None = None
        try:
            if self.browser_var.get() == "Donut":
                browser_path = None
                profile_path = None
                browser_name = "Donut"
                donut_client = self._new_donut_client()
            elif self.browser_var.get() == "VirtualBrowser":
                browser_path, profile_path, virtual_worker_id = (
                    self._acquire_shared_virtual_worker()
                    if self._shared_browser_mode()
                    else self._acquire_virtual_worker(mailbox)
                )
                browser_name = "VirtualBrowser"
            else:
                browser_path = self._find_browser()
                profile_path = self._browser_profile_path(mailbox.key, browser_path)
                profile_path.mkdir(parents=True, exist_ok=True)
                browser_name = browser_display_name(browser_path)
            mail_client = mailbox.client.clone()
        except Exception as exc:
            mailbox.status = "后台准备失败"
            self.status_var.set(f"后台注册准备失败：{exc}")
            self._record_registration_outcome(
                mailbox,
                "failed",
                f"注册失败：{mailbox.address}（准备失败：{exc}）",
            )
            return

        self.browser_workers.add(mailbox.key)
        self.registration_outcomes[mailbox.key] = "running"
        mailbox.status = "后台注册中"
        self.status_var.set(f"正在后台登录/注册 {mailbox.address}…")
        self._update_registration_feedback()
        self._update_action_states()
        self._layout_browser_buttons()
        outcome: dict[str, Any] = {
            "points": None,
            "manual": False,
            "registered": False,
        }

        def status(message: str) -> None:
            if "检测到 CAPTCHA" in message or "需要 CAPTCHA" in message:
                outcome["manual"] = True
            self.root.after(
                0,
                lambda text=message: self._background_browser_status(
                    mailbox.key, text
                ),
            )

        def points_updated(points: int) -> None:
            outcome["points"] = max(0, int(points))

        def authenticated() -> None:
            outcome["registered"] = True

        def worker() -> None:
            automation: CreativeFabricaBrowser | None = None
            donut_session: DonutBrowserSession | None = None
            error: Exception | None = None
            try:
                self._renew_expired_mailbox_for_browser(mailbox, mail_client, status)
                if donut_client is not None:
                    status(f"正在准备 {mailbox.address} 的 Donut 指纹配置…")
                    profile = donut_client.ensure_profile(
                        mailbox_key=(
                            "shared-creativefabrica"
                            if self._shared_browser_mode()
                            else mailbox.key
                        ),
                        address=(
                            "复用 Creative Fabrica 会话"
                            if self._shared_browser_mode()
                            else mailbox.address
                        ),
                        preferred_id=(
                            self.settings.get("shared_donut_profile_id")
                            if self._shared_browser_mode()
                            else mailbox.donut_profile_id
                        ),
                    )
                    self.root.after(
                        0,
                        lambda profile_id=profile.id: self._remember_donut_session(
                            mailbox.key, profile_id
                        ),
                    )
                    donut_session = donut_client.open_profile(
                        profile,
                        existing_debugger_address=(
                            self.settings.get("shared_donut_debugger_address")
                            if self._shared_browser_mode()
                            else mailbox.donut_debugger_address
                        ),
                        headless=False,
                    )
                    self.root.after(
                        0,
                        lambda session=donut_session: self._remember_donut_session(
                            mailbox.key,
                            session.profile_id,
                            session.debugger_address,
                        ),
                    )
                automation = CreativeFabricaBrowser(
                    address=mailbox.address,
                    profile_path=profile_path,
                    browser_path=browser_path,
                    mail_client=mail_client,
                    status=status,
                    points_updated=points_updated,
                    background=True,
                    authenticated=authenticated,
                    debugger_address=(
                        donut_session.debugger_address if donut_session else None
                    ),
                    browser_name=browser_name,
                    browser_version=(
                        donut_session.browser_version if donut_session else None
                    ),
                    launch_arguments=(
                        (f"--worker-id={virtual_worker_id}",)
                        if virtual_worker_id else ()
                    ),
                    reset_site_session=(
                        bool(virtual_worker_id) or self._shared_browser_mode()
                    ),
                    clear_site_data_on_logout=bool(virtual_worker_id),
                )
                automation.run()
            except Exception as exc:
                error = exc
            finally:
                if automation is not None and automation.driver is not None:
                    try:
                        automation.driver.quit()
                    except Exception:
                        pass
                if donut_client is not None:
                    if donut_session is not None:
                        try:
                            donut_client.kill_profile(donut_session.profile_id)
                        except Exception:
                            pass
                        self.root.after(
                            0,
                            lambda session=donut_session: self._clear_donut_debugger(
                                mailbox.key, session.debugger_address
                            ),
                        )
                    donut_client.close()
                mail_client.close()
                points = outcome["points"]
                manual = bool(outcome["manual"])
                registered = bool(outcome["registered"])
                self.root.after(
                    0,
                    lambda: self._finish_background_register(
                        mailbox.key, error, points, manual, registered, virtual_worker_id
                    ),
                )

        threading.Thread(
            target=worker,
            name=f"creative-fabrica-background-{mailbox.key}",
            daemon=True,
        ).start()

    def _background_browser_status(self, key: str, message: str) -> None:
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if mailbox:
            mailbox.status = (
                "需要手动验证"
                if "检测到 CAPTCHA" in message or "需要 CAPTCHA" in message
                else "后台注册中"
            )
        self.status_var.set(message)

    def _finish_background_register(
        self,
        key: str,
        error: Exception | None,
        points: int | None,
        manual: bool,
        registered: bool,
        virtual_worker_id: str | None = None,
    ) -> None:
        self.browser_workers.discard(key)
        if virtual_worker_id and not manual:
            self._release_virtual_worker(key)
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            self._start_next_reused_registration()
            return
        detail = ""
        if error is not None:
            detail = str(getattr(error, "msg", "") or error)
            if "Stacktrace:" in detail:
                detail = detail.split("Stacktrace:", 1)[0]
            detail = detail.removeprefix("Message:").strip() or "未知错误"

        registration_succeeded = registered or points is not None
        if registration_succeeded:
            mailbox.registered = True
            if points is not None:
                mailbox.points = points
                mailbox.status = f"注册成功 · {points:,} 积分"
                result = f"注册成功：{mailbox.address}（积分 {points:,}）"
            elif manual:
                mailbox.status = "注册成功 · 积分待人工验证"
                result = f"注册成功：{mailbox.address}（积分读取等待人工验证）"
            elif error is not None:
                mailbox.status = "注册成功 · 积分读取失败"
                result = f"注册成功：{mailbox.address}（积分读取失败：{detail}）"
            else:
                mailbox.status = "注册成功 · 积分未读取"
                result = f"注册成功：{mailbox.address}（积分未读取）"
            if manual and self._shared_browser_mode():
                self.reused_registration_paused_key = key
            self.status_var.set(result)
            self._record_registration_outcome(
                mailbox,
                "manual" if manual else "success",
                result,
            )
        elif manual:
            mailbox.status = "注册待验证"
            if self._shared_browser_mode():
                self.reused_registration_paused_key = key
            result = f"等待验证：{mailbox.address}，请点击“自动打开”继续"
            self.status_var.set(
                f"{result}；复用会话注册队列已暂停"
                if self._shared_browser_mode()
                else result
            )
            self._record_registration_outcome(mailbox, "manual", result)
        elif error is not None:
            mailbox.status = "注册失败"
            result = f"注册失败：{mailbox.address}（{detail}）"
            self.status_var.set(result)
            self._record_registration_outcome(mailbox, "failed", result)
        else:
            mailbox.status = "注册结果未确认"
            result = f"未确认注册结果：{mailbox.address}"
            self.status_var.set(result)
            self._record_registration_outcome(mailbox, "unknown", result)
        self._save_mailboxes()
        self._update_active_header()
        self._update_action_states()
        self._layout_browser_buttons()
        self._start_next_reused_registration()

    def open_browser(self, mailbox: ManagedMailbox) -> None:
        if mailbox.key in self.browser_workers:
            self.status_var.set(f"{mailbox.address} 的浏览器登录/注册流程正在运行")
            return
        if (
            self._shared_browser_mode()
            and self.reused_registration_paused_key
            and mailbox.key != self.reused_registration_paused_key
        ):
            paused = self.mailboxes.get(self.reused_registration_paused_key)
            address = paused.address if paused else "上一账户"
            self.status_var.set(
                f"注册队列正等待 {address} 完成人工验证，暂时不能切换其他账户。"
            )
            return
        if (
            self._shared_browser_mode()
            and self.reused_registration_queue
            and not self.reused_registration_paused_key
        ):
            self.status_var.set("复用会话注册队列正在运行，请等待队列结束。")
            return

        shared_driver = None
        switching_shared_account = False
        if self._shared_browser_mode():
            if self.browser_workers:
                self.status_var.set("复用会话正在处理另一个账号，请等待该流程结束。")
                return
            if self.shared_browser_driver is not None:
                try:
                    if self.shared_browser_driver.window_handles:
                        shared_driver = self.shared_browser_driver
                    else:
                        raise RuntimeError("复用会话窗口已关闭")
                except Exception:
                    self.shared_browser_driver = None
                    self.shared_browser_owner = None
            # A shared session always starts by logging out from the account
            # page. Do this even when the same mailbox was the last owner:
            # the running browser may have been changed outside this app.
            switching_shared_account = True

        existing = self.browser_drivers.get(mailbox.key)
        if existing is not None and shared_driver is None:
            try:
                existing.switch_to.window(existing.current_window_handle)
                self._refresh_existing_browser_points(mailbox, existing)
                return
            except Exception:
                self.browser_drivers.pop(mailbox.key, None)
                self._release_virtual_worker(mailbox.key)

        donut_client: DonutBrowserClient | None = None
        virtual_worker_id: str | None = None
        try:
            if shared_driver is not None:
                browser_path = None
                profile_path = None
                browser_name = "复用会话浏览器"
            elif self.browser_var.get() == "Donut":
                browser_path = None
                profile_path = None
                browser_name = "Donut"
                donut_client = self._new_donut_client()
            elif self.browser_var.get() == "VirtualBrowser":
                browser_path, profile_path, virtual_worker_id = (
                    self._acquire_shared_virtual_worker()
                    if self._shared_browser_mode()
                    else self._acquire_virtual_worker(mailbox)
                )
                browser_name = "VirtualBrowser"
            else:
                browser_path = self._find_browser()
                profile_path = self._browser_profile_path(mailbox.key, browser_path)
                profile_path.mkdir(parents=True, exist_ok=True)
                browser_name = browser_display_name(browser_path)
            mail_client = mailbox.client.clone()
        except Exception as exc:
            self.status_var.set(f"浏览器启动失败：{exc}")
            messagebox.showerror("无法打开浏览器", str(exc), parent=self.root)
            return

        self.browser_workers.add(mailbox.key)
        if self._shared_browser_mode():
            # Remember the owner before a CAPTCHA pause so reopening the same
            # account can resume it instead of logging out again.
            self.shared_browser_owner = mailbox.key
        mailbox.status = "浏览器启动中"
        self.root.clipboard_clear()
        self.root.clipboard_append(mailbox.address)
        self.status_var.set(
            f"正在为 {mailbox.address} 启动{'复用会话' if self._shared_browser_mode() else '隔离会话'} {browser_name}；邮箱地址已复制"
        )

        def status(message: str) -> None:
            self.root.after(0, lambda: self._browser_status(mailbox.key, message))

        def points_updated(points: int) -> None:
            self.root.after(0, lambda: self._update_mailbox_points(mailbox.key, points))

        def authenticated() -> None:
            self.root.after(0, lambda: self._mark_mailbox_registered(mailbox.key))

        def worker() -> None:
            driver = None
            error: Exception | None = None
            automation: CreativeFabricaBrowser | None = None
            donut_session: DonutBrowserSession | None = None
            try:
                self._renew_expired_mailbox_for_browser(mailbox, mail_client, status)
                if donut_client is not None:
                    status(f"正在准备 {mailbox.address} 的 Donut 指纹配置…")
                    profile = donut_client.ensure_profile(
                        mailbox_key=(
                            "shared-creativefabrica"
                            if self._shared_browser_mode()
                            else mailbox.key
                        ),
                        address=(
                            "复用 Creative Fabrica 会话"
                            if self._shared_browser_mode()
                            else mailbox.address
                        ),
                        preferred_id=(
                            self.settings.get("shared_donut_profile_id")
                            if self._shared_browser_mode()
                            else mailbox.donut_profile_id
                        ),
                    )
                    self.root.after(
                        0,
                        lambda profile_id=profile.id: self._remember_donut_session(
                            mailbox.key, profile_id
                        ),
                    )
                    donut_session = donut_client.open_profile(
                        profile,
                        existing_debugger_address=(
                            self.settings.get("shared_donut_debugger_address")
                            if self._shared_browser_mode()
                            else mailbox.donut_debugger_address
                        ),
                        headless=False,
                    )
                    self.root.after(
                        0,
                        lambda session=donut_session: self._remember_donut_session(
                            mailbox.key,
                            session.profile_id,
                            session.debugger_address,
                        ),
                    )
                automation = CreativeFabricaBrowser(
                    address=mailbox.address,
                    profile_path=profile_path,
                    browser_path=browser_path,
                    mail_client=mail_client,
                    status=status,
                    points_updated=points_updated,
                    authenticated=authenticated,
                    debugger_address=(
                        donut_session.debugger_address if donut_session else None
                    ),
                    browser_name=browser_name,
                    browser_version=(
                        donut_session.browser_version if donut_session else None
                    ),
                    launch_arguments=(
                        (f"--worker-id={virtual_worker_id}",)
                        if virtual_worker_id else ()
                    ),
                    reset_site_session=(
                        (
                            bool(virtual_worker_id)
                            and mailbox.status != "需要手动验证"
                        )
                        or switching_shared_account
                    ),
                    clear_site_data_on_logout=bool(virtual_worker_id),
                    driver=shared_driver,
                )
                driver = automation.run()
            except Exception as exc:
                if (
                    automation is not None
                    and automation.driver is not None
                    and automation.human_verification_pending
                    and not browser_was_closed(exc)
                ):
                    status(
                        f"CAPTCHA 人机验证仍在进行；请在 {browser_name} 窗口完成后再次点击“自动打开”"
                    )
                else:
                    error = exc
            finally:
                if donut_client is not None:
                    donut_client.close()
                if automation is not None and automation.human_verification_pending:
                    # Do not cache a driver whose login workflow stopped at a
                    # challenge. The browser is detached and the next click will
                    # reattach to the same profile and resume the full flow.
                    driver = None
                mail_client.close()
                self.root.after(
                    0,
                    lambda: self._finish_browser_worker(
                        mailbox.key, driver, error, virtual_worker_id
                    ),
                )

        threading.Thread(
            target=worker,
            name=f"creative-fabrica-{mailbox.key}",
            daemon=True,
        ).start()

    def _open_manual_browser(self, mailbox: ManagedMailbox) -> None:
        """Open a normal browser window without Selenium, input, or submission."""
        self._renew_expired_mailbox_for_browser(
            mailbox,
            None,
            lambda message: self.status_var.set(message),
        )
        existing = self.manual_browser_processes.get(mailbox.key)
        if existing is not None and existing.poll() is None:
            self.root.clipboard_clear()
            self.root.clipboard_append(mailbox.address)
            self.status_var.set(f"{mailbox.address} 的手动浏览器已打开；邮箱已复制。")
            return
        if existing is not None:
            self.manual_browser_processes.pop(mailbox.key, None)
            self._release_virtual_worker(mailbox.key)

        virtual_worker_id: str | None = None
        try:
            selected = self.browser_var.get()
            if selected == "Donut":
                raise RuntimeError(
                    "手动模式不能通过 Donut API 启动环境；请在 Donut 中手动打开配置后使用已复制的邮箱。"
                )
            if selected == "VirtualBrowser":
                browser_path, profile_path, virtual_worker_id = (
                    self._acquire_shared_virtual_worker()
                    if self._shared_browser_mode()
                    else self._acquire_virtual_worker(mailbox)
                )
            else:
                browser_path = self._find_browser()
                profile_path = self._browser_profile_path(mailbox.key, browser_path)
                profile_path.mkdir(parents=True, exist_ok=True)
            command = manual_browser_command(
                browser_path,
                profile_path,
                worker_id=virtual_worker_id,
            )
            process = subprocess.Popen(command, cwd=str(browser_path.parent))
        except (OSError, RuntimeError, VirtualBrowserError) as exc:
            if virtual_worker_id:
                self._release_virtual_worker(mailbox.key)
            detail = str(exc)
            self.status_var.set(f"手动浏览器启动失败：{detail}")
            messagebox.showerror("无法打开手动浏览器", detail, parent=self.root)
            return

        self.manual_browser_processes[mailbox.key] = process
        self.root.clipboard_clear()
        self.root.clipboard_append(mailbox.address)
        mailbox.status = "等待手动操作"
        if not virtual_worker_id:
            self._start_manual_browser_tracker(mailbox, profile_path, process)
        self.status_var.set(
            f"已打开 {mailbox.address} 的网站首页，邮箱已复制；请自行登录、注册和完成验证。"
        )
        self._update_active_header()
        self._layout_browser_buttons()

    def _reap_manual_browser_processes(self) -> None:
        for key, process in list(self.manual_browser_processes.items()):
            if process.poll() is None:
                continue
            self.manual_browser_processes.pop(key, None)
            self._release_virtual_worker(key)
            mailbox = self.mailboxes.get(key)
            if mailbox and mailbox.status == "等待手动操作":
                mailbox.status = "浏览器已关闭"

    def _make_tracking_automation(self, mailbox: ManagedMailbox) -> CreativeFabricaBrowser:
        try:
            browser_path = self._find_browser()
        except Exception:
            browser_path = None
        browser_name = browser_display_name(browser_path) if browser_path else "浏览器"
        return CreativeFabricaBrowser(
            address=mailbox.address,
            profile_path=None,
            browser_path=browser_path,
            mail_client=object(),
            status=lambda _message: None,
            points_updated=lambda _points: None,
            browser_name=browser_name,
        )

    @staticmethod
    def _observed_login_state(driver: Any, By, url: str) -> bool | None:
        """Return the login evidence on the current page, or None when unclear."""
        try:
            logout = driver.find_elements(By.CSS_SELECTOR, "a[href*='logout']")
        except Exception:
            logout = []
        if logout:
            return True
        lowered = url.lower()
        if "/login" in lowered or "/signup" in lowered:
            return False
        return None

    def _observe_site_login(self, key: str, state: bool) -> None:
        if state is not True:
            return
        self.root.after(0, lambda: self._apply_observed_login(key))

    def _apply_observed_login(self, key: str) -> None:
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox or mailbox.registered:
            return
        mailbox.registered = True
        if mailbox.status not in ("等待手动操作",):
            mailbox.status = "网站已登录"
        self._save_mailboxes()

    def _observe_studio_points(self, key: str, points: int) -> None:
        self.root.after(0, lambda: self._apply_observed_points(key, int(points)))

    def _apply_observed_points(self, key: str, points: int) -> None:
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        value = max(0, points)
        if value == mailbox.points and mailbox.status == f"网站已登录 · {value:,} 积分":
            return
        mailbox.points = value
        mailbox.registered = True
        if mailbox.status not in ("等待手动操作",):
            mailbox.status = f"网站已登录 · {value:,} 积分"
        self._save_mailboxes()

    def _track_observation_round(
        self,
        mailbox: ManagedMailbox,
        automation: CreativeFabricaBrowser,
        driver: Any,
        By,
    ) -> bool:
        """Read one page snapshot; return False when the browser window is gone."""
        try:
            url = driver.current_url or ""
        except Exception as exc:
            if browser_was_closed(exc):
                return False
            return True
        state = self._observed_login_state(driver, By, url)
        if state is not None:
            self._observe_site_login(mailbox.key, state)
        if "studio.creativefabrica.com" in url.lower():
            try:
                points = automation._read_studio_points(By)
            except Exception:
                points = None
            if points is not None:
                self._observe_studio_points(mailbox.key, points)
        return True

    def _mark_tracked_browser_closed(self, key: str) -> None:
        if self.closed:
            return
        self.browser_trackers.pop(key, None)
        self.browser_drivers.pop(key, None)
        self._release_virtual_worker(key)
        if self.shared_browser_owner == key:
            self.shared_browser_driver = None
            self.shared_browser_owner = None
        mailbox = self.mailboxes.get(key)
        if mailbox:
            mailbox.status = "浏览器已关闭"
            self._save_mailboxes()

    def _start_manual_browser_tracker(
        self,
        mailbox: ManagedMailbox,
        profile_path: Path,
        process: subprocess.Popen[Any],
    ) -> None:
        """Observe a manually opened window: URL, login state and Studio points."""
        key = mailbox.key
        if key in self.browser_trackers:
            return

        def track() -> None:
            driver = None
            automation: CreativeFabricaBrowser | None = None
            try:
                while not self.closed and key in self.mailboxes and process.poll() is None:
                    if key in self.browser_workers:
                        time.sleep(2)
                        continue
                    if driver is None:
                        debugger_address = existing_debugger_address(profile_path)
                        if not debugger_address or not debugger_address_is_live(
                            debugger_address
                        ):
                            time.sleep(1)
                            continue
                        automation = self._make_tracking_automation(mailbox)
                        automation.debugger_address = debugger_address
                        try:
                            webdriver, By, _WebDriverWait = automation._selenium_imports()
                            driver = automation._attach_to_debugger(webdriver)
                        except Exception:
                            driver = None
                            time.sleep(2)
                            continue
                    if not self._track_observation_round(
                        mailbox, automation, driver, By
                    ):
                        break
                    time.sleep(3)
            finally:
                # Only quit the attached session once the user's window is gone;
                # quitting an attached driver must never close the live window.
                if driver is not None and process.poll() is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                try:
                    self.root.after(
                        0,
                        lambda: self._mark_tracked_browser_closed(key),
                    )
                except tk.TclError:
                    pass

        thread = threading.Thread(target=track, name=f"track-manual-{key}", daemon=True)
        self.browser_trackers[key] = thread
        thread.start()

    def _start_auto_browser_tracker(self, mailbox: ManagedMailbox, driver: Any) -> None:
        """Keep observing an automation-owned window after its flow finished."""
        key = mailbox.key
        if key in self.browser_trackers or self.closed:
            return

        def track() -> None:
            automation = self._make_tracking_automation(mailbox)
            try:
                _webdriver, By, _WebDriverWait = automation._selenium_imports()
            except Exception:
                By = None
            try:
                while not self.closed and key in self.mailboxes:
                    if key in self.browser_workers:
                        time.sleep(2)
                        continue
                    if not self._track_observation_round(
                        mailbox, automation, driver, By
                    ):
                        break
                    time.sleep(3)
            finally:
                try:
                    self.root.after(
                        0,
                        lambda: self._mark_tracked_browser_closed(key),
                    )
                except tk.TclError:
                    pass

        thread = threading.Thread(target=track, name=f"track-auto-{key}", daemon=True)
        self.browser_trackers[key] = thread
        thread.start()

    def _refresh_existing_browser_points(self, mailbox: ManagedMailbox, driver: Any) -> None:
        if mailbox.key in self.browser_workers:
            return
        try:
            mail_client = mailbox.client.clone()
            if self.browser_var.get() == "Donut":
                browser_path = None
                profile_path = None
                browser_name = "Donut"
            else:
                browser_path = self._find_browser()
                profile_path = self._browser_profile_path(mailbox.key, browser_path)
                browser_name = browser_display_name(browser_path)
        except Exception as exc:
            self.status_var.set(f"积分更新准备失败：{exc}")
            return
        self.browser_workers.add(mailbox.key)
        mailbox.status = "更新积分中"
        self.status_var.set(f"正在重新读取 {mailbox.address} 的 Studio AI 积分…")

        def status(message: str) -> None:
            self.root.after(0, lambda: self._browser_status(mailbox.key, message))

        def points_updated(points: int) -> None:
            self.root.after(0, lambda: self._update_mailbox_points(mailbox.key, points))

        def worker() -> None:
            error: Exception | None = None
            try:
                automation = CreativeFabricaBrowser(
                    address=mailbox.address,
                    profile_path=profile_path,
                    browser_path=browser_path,
                    mail_client=mail_client,
                    status=status,
                    points_updated=points_updated,
                    browser_name=browser_name,
                )
                automation.refresh_points_only(driver)
            except Exception as exc:
                error = exc
            finally:
                mail_client.close()
                self.root.after(
                    0,
                    lambda: self._finish_browser_worker(mailbox.key, driver, error),
                )

        threading.Thread(
            target=worker,
            name=f"creative-fabrica-points-{mailbox.key}",
            daemon=True,
        ).start()

    def _browser_status(self, key: str, message: str) -> None:
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if mailbox:
            if "成功" in message or "已登录" in message:
                mailbox.status = "网站已登录"
            elif "人机验证已完成" in message:
                mailbox.status = "浏览器处理中"
            elif "CAPTCHA" in message or "人机验证" in message:
                mailbox.status = "等待 CAPTCHA"
            elif "验证码" in message:
                mailbox.status = "等待验证码"
            else:
                mailbox.status = "浏览器处理中"
        self.status_var.set(message)

    def _update_mailbox_points(self, key: str, points: int) -> None:
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        mailbox.points = max(0, int(points))
        mailbox.registered = True
        mailbox.status = f"注册成功 · {mailbox.points:,} 积分"
        if self.reused_registration_paused_key == key:
            self.reused_registration_paused_key = None
        self._record_registration_outcome(
            mailbox,
            "success",
            f"注册成功：{mailbox.address}（积分 {mailbox.points:,}）",
        )
        self._save_mailboxes()
        self._update_active_header()
        self._layout_browser_buttons()

    def _mark_mailbox_registered(self, key: str) -> None:
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if not mailbox:
            return
        if self.reused_registration_paused_key == key:
            self.reused_registration_paused_key = None
        if mailbox.registered:
            if self.registration_outcomes.get(key) == "manual":
                self._record_registration_outcome(
                    mailbox,
                    "success",
                    f"注册成功：{mailbox.address}（积分待更新）",
                )
            self._layout_browser_buttons()
            return
        mailbox.registered = True
        mailbox.status = "注册成功 · 积分待更新"
        self._record_registration_outcome(
            mailbox,
            "success",
            f"注册成功：{mailbox.address}（积分待更新）",
        )
        self._save_mailboxes()
        self._layout_browser_buttons()

    def _finish_browser_worker(
        self,
        key: str,
        driver: Any,
        error: Exception | None,
        virtual_worker_id: str | None = None,
    ) -> None:
        self.browser_workers.discard(key)
        if self.closed:
            return
        mailbox = self.mailboxes.get(key)
        if error is not None and browser_was_closed(error):
            self.browser_drivers.pop(key, None)
            if self.shared_browser_owner == key:
                self.shared_browser_driver = None
                self.shared_browser_owner = None
            if virtual_worker_id:
                self._release_virtual_worker(key)
            if mailbox:
                mailbox.status = "浏览器已关闭"
                mailbox.donut_debugger_address = None
                self._save_mailboxes()
            self.status_var.set(
                f"{mailbox.address if mailbox else '该邮箱'} 的浏览器已关闭"
            )
            self._update_active_header()
            self._update_action_states()
            self._layout_browser_buttons()
            self._start_next_reused_registration()
            return
        if driver is not None:
            self.browser_drivers[key] = driver
            if self._shared_browser_mode():
                self.shared_browser_driver = driver
                self.shared_browser_owner = key
                for other_key in list(self.browser_drivers):
                    if other_key != key:
                        self.browser_drivers.pop(other_key, None)
            if mailbox and mailbox.status == "浏览器处理中":
                mailbox.status = "浏览器已打开"
            if mailbox:
                self._start_auto_browser_tracker(mailbox, driver)
        if error is None:
            self._layout_browser_buttons()
            self._start_next_reused_registration()
            return
        if virtual_worker_id:
            self._release_virtual_worker(key)
        if mailbox:
            mailbox.status = "浏览器失败"
        detail = str(getattr(error, "msg", "") or error)
        if "Stacktrace:" in detail:
            detail = detail.split("Stacktrace:", 1)[0]
        detail = detail.removeprefix("Message:").strip()
        if not detail:
            detail = (
                "浏览器驱动未返回具体原因。程序已清理失效的浏览器连接，"
                "请再次点击“自动打开”；如果仍然失败，请先关闭该邮箱的浏览器窗口。"
            )
        if "user data directory is already in use" in detail.lower():
            detail = "该邮箱的浏览器配置正在被另一个窗口使用。请先关闭原来的浏览器窗口，再重试。"
        self.status_var.set(f"浏览器自动化失败：{detail}")
        messagebox.showerror("Creative Fabrica 自动化失败", detail, parent=self.root)
        self._layout_browser_buttons()
        self._start_next_reused_registration()

    def renew_selected(self) -> None:
        mailbox = self._active_mailbox()
        if mailbox:
            self.renew_mailbox(mailbox)

    def renew_mailbox(self, mailbox: ManagedMailbox) -> None:
        if mailbox.busy or mailbox.provider != "rootsh":
            return
        mailbox.busy = True
        mailbox.status = "续期中"
        self._update_action_states()
        self.status_var.set(f"正在续期 {mailbox.address}…")

        def success(result: Mailbox) -> None:
            if mailbox.key not in self.mailboxes:
                return
            self._apply_renewal_result(mailbox.key, result)
            self.status_var.set(f"{mailbox.address} 已续期 10 分钟")

        self._submit(
            lambda: mailbox.client.apply_mailbox(mailbox.local_part, mailbox.domain),
            success,
            on_error=lambda exc: self._mailbox_error(mailbox, exc),
        )

    def _apply_renewal_result(self, key: str, result: Mailbox) -> None:
        """Apply a successful mailbox renewal on the UI thread."""
        if self.closed or key not in self.mailboxes:
            return
        mailbox = self.mailboxes[key]
        mailbox.address = result.address
        mailbox.expires_at = time.monotonic() + result.lifetime_seconds
        mailbox.cursor = 0
        mailbox.messages.clear()
        mailbox.busy = False
        if mailbox.status != "等待手动操作":
            mailbox.status = "已续期"
        mailbox.next_poll_at = 0.0
        if self.active_key == key:
            self._render_active_messages()
        self._update_action_states()
        self._save_mailboxes()

    def _renew_expired_mailbox_for_browser(
        self,
        mailbox: ManagedMailbox,
        mail_client: RootshClient | None,
        status: Callable[[str], None],
    ) -> None:
        """Renew an expired rootsh mailbox in the background.

        Never blocks the browser launch: the renewal runs in its own thread and
        its result (including fresh cookies, synced back into ``mail_client``
        when one is given) is applied as soon as it arrives.
        """
        if mailbox.provider != "rootsh" or not mailbox.expired:
            return
        status(f"{mailbox.address} 已到期，正在自动续期…")

        def renew() -> None:
            try:
                result = mailbox.client.apply_mailbox(
                    mailbox.local_part, mailbox.domain
                )
            except Exception as exc:
                self.root.after(
                    0,
                    lambda: self.status_var.set(
                        f"{mailbox.address} 自动续期失败：{exc}；可稍后点击“续期 10 分钟”重试"
                    ),
                )
                return
            if mail_client is not None:
                try:
                    mail_client.import_cookies(mailbox.client.export_cookies())
                except Exception:
                    pass
            self.root.after(
                0,
                lambda renewed=result: self._apply_renewal_result(
                    mailbox.key, renewed
                ),
            )

        threading.Thread(
            target=renew,
            name=f"rootsh-renew-{mailbox.key}",
            daemon=True,
        ).start()

    def refresh_selected(self) -> None:
        mailbox = self._active_mailbox()
        if mailbox:
            self.refresh_mailbox(mailbox, quiet=False)

    def refresh_mailbox(self, mailbox: ManagedMailbox, *, quiet: bool) -> None:
        if mailbox.busy or mailbox.key not in self.mailboxes:
            return
        if mailbox.expired:
            mailbox.status = "已到期"
            if not quiet:
                self.status_var.set(f"{mailbox.address} 已到期，请先续期")
            return
        mailbox.busy = True
        mailbox.status = "取件中"
        mailbox.next_poll_at = 0.0
        self._update_action_states()
        if not quiet:
            self.status_var.set(f"正在检查 {mailbox.address}…")

        def success(update: InboxUpdate) -> None:
            if mailbox.key not in self.mailboxes:
                return
            mailbox.cursor = update.cursor
            added = 0
            for message in update.messages:
                if message.message_id not in mailbox.messages:
                    mailbox.messages[message.message_id] = message
                    added += 1
            mailbox.busy = False
            mailbox.status = f"新增 {added} 封" if added else "监听中"
            mailbox.next_poll_at = 0.0
            if self.active_key == mailbox.key and added:
                self._render_active_messages()
            self._update_action_states()
            if added:
                self.root.bell()
                self.status_var.set(f"{mailbox.address} 收到 {added} 封新邮件")
            elif not quiet:
                self.status_var.set(f"{mailbox.address} 暂无新邮件")
            self._save_mailboxes()

        self._submit(
            lambda: mailbox.client.get_mail(mailbox.address, mailbox.cursor),
            success,
            on_error=lambda exc: self._mailbox_error(mailbox, exc),
        )

    def _mailbox_error(self, mailbox: ManagedMailbox, exc: Exception) -> None:
        if mailbox.key in self.mailboxes:
            mailbox.busy = False
            mailbox.status = "操作失败"
            mailbox.next_poll_at = 0.0
        self._update_action_states()
        self.status_var.set(f"{mailbox.address}：{exc}")
        messagebox.showerror("邮箱操作失败", f"{mailbox.address}\n\n{exc}", parent=self.root)

    def destroy_selected_mailboxes(self) -> None:
        selected = [self.mailboxes[key] for key in self.mailbox_tree.selection() if key in self.mailboxes]
        if not selected or any(mailbox.busy for mailbox in selected):
            return
        description = selected[0].address if len(selected) == 1 else f"选中的 {len(selected)} 个邮箱"
        outlook_only = all(mailbox.provider == "outlook" for mailbox in selected)
        action_text = "从列表移除" if outlook_only else "销毁并删除"
        if not messagebox.askyesno(
            "删除邮箱",
            f"确定{action_text}{description}？\n对应邮件和列表项都会被移除。"
            + ("\n不会删除 Microsoft Outlook 账号或远程邮件。" if outlook_only else ""),
            parent=self.root,
        ):
            return
        for mailbox in selected:
            mailbox.busy = True
            mailbox.status = "销毁中"
        self._update_action_states()
        self.status_var.set(f"正在销毁 {len(selected)} 个邮箱…")

        def task() -> tuple[list[ManagedMailbox], list[tuple[ManagedMailbox, str]]]:
            destroyed: list[ManagedMailbox] = []
            failed: list[tuple[ManagedMailbox, str]] = []
            for mailbox in selected:
                if mailbox.provider == "outlook":
                    destroyed.append(mailbox)
                    continue
                try:
                    mailbox.client.destroy_mailbox()
                except Exception as exc:
                    failed.append((mailbox, str(exc)))
                else:
                    destroyed.append(mailbox)
            return destroyed, failed

        def success(result: tuple[list[ManagedMailbox], list[tuple[ManagedMailbox, str]]]) -> None:
            destroyed, failed = result
            for mailbox in destroyed:
                self._remove_mailbox(mailbox, select_fallback=False)
            for mailbox, _error in failed:
                mailbox.busy = False
                mailbox.status = "销毁失败"
            remaining = self.mailbox_tree.get_children()
            if remaining:
                next_key = remaining[0]
                self.mailbox_tree.selection_set(next_key)
                self.mailbox_tree.focus(next_key)
                self._activate_mailbox(next_key)
            else:
                self._activate_mailbox(None)
            self._update_action_states()
            self._layout_browser_buttons()
            self.status_var.set(f"已删除 {len(destroyed)} 个邮箱" + (f"，{len(failed)} 个失败" if failed else ""))
            if failed:
                details = "\n".join(f"{mailbox.address}：{error}" for mailbox, error in failed)
                messagebox.showwarning("部分邮箱删除失败", details, parent=self.root)

        self._submit(task, success)

    def _remove_mailbox(self, mailbox: ManagedMailbox, *, select_fallback: bool = True) -> None:
        if mailbox.key in self.reused_registration_queue:
            self.reused_registration_queue = deque(
                key
                for key in self.reused_registration_queue
                if key != mailbox.key
            )
            self._refresh_reused_registration_queue_statuses()
        if self.reused_registration_paused_key == mailbox.key:
            self.reused_registration_paused_key = None
        self.mailboxes.pop(mailbox.key, None)
        buttons = self.mailbox_browser_buttons.pop(mailbox.key, None)
        if buttons:
            for button in buttons:
                button.destroy()
        background_button = self.mailbox_background_buttons.pop(mailbox.key, None)
        if background_button:
            background_button.destroy()
        if self.mailbox_tree.exists(mailbox.key):
            self.mailbox_tree.delete(mailbox.key)
        mailbox.client.close()
        self.browser_drivers.pop(mailbox.key, None)
        self._save_mailboxes()
        self._start_next_reused_registration()
        if not select_fallback:
            return
        remaining = self.mailbox_tree.get_children()
        if remaining:
            next_key = remaining[0]
            self.mailbox_tree.selection_set(next_key)
            self._activate_mailbox(next_key)
        else:
            self._activate_mailbox(None)

    def _message_selected(self, _event: tk.Event[Any] | None = None) -> None:
        self._update_action_states()

    def view_selected_message(self) -> None:
        mailbox = self._active_mailbox()
        selected = self.message_tree.selection()
        if not mailbox or mailbox.busy or len(selected) != 1:
            return
        message_id = selected[0]
        message = mailbox.messages.get(message_id)
        mailbox.busy = True
        mailbox.status = "读取正文"
        self._update_action_states()

        def success(source: str) -> None:
            mailbox.busy = False
            mailbox.status = "监听中"
            heading = ""
            if message:
                heading = f"邮箱：{mailbox.address}\n发件人：{message.sender}\n主题：{message.subject}\n时间：{message.received_at}\n{'─' * 58}\n\n"
            self._set_preview(heading + (html_to_text(source) or "（邮件正文为空）"))
            self._update_action_states()
            self.status_var.set("邮件正文已读取")

        self._submit(
            lambda: mailbox.client.fetch_message_html(mailbox.address, message_id),
            success,
            on_error=lambda exc: self._mailbox_error(mailbox, exc),
        )

    def save_selected_message(self) -> None:
        mailbox = self._active_mailbox()
        selected = self.message_tree.selection()
        if not mailbox or mailbox.busy or len(selected) != 1:
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="保存原始邮件", defaultextension=".eml", filetypes=(("邮件文件", "*.eml"), ("所有文件", "*.*")))
        if not destination:
            return
        message_id = selected[0]
        mailbox.busy = True
        mailbox.status = "下载中"
        self._update_action_states()
        self._submit(
            lambda: mailbox.client.download_message(mailbox.address, message_id, Path(destination)),
            lambda _value: self._finish_file_operation(mailbox, f"邮件已保存到 {destination}"),
            on_error=lambda exc: self._mailbox_error(mailbox, exc),
        )

    def download_selected_mailbox(self) -> None:
        mailbox = self._active_mailbox()
        if not mailbox or mailbox.busy:
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="下载整个邮箱", defaultextension=".zip", filetypes=(("ZIP 压缩包", "*.zip"), ("所有文件", "*.*")))
        if not destination:
            return
        mailbox.busy = True
        mailbox.status = "下载中"
        self._update_action_states()
        self._submit(
            lambda: mailbox.client.download_mailbox(mailbox.address, Path(destination)),
            lambda _value: self._finish_file_operation(mailbox, f"邮箱已保存到 {destination}"),
            on_error=lambda exc: self._mailbox_error(mailbox, exc),
        )

    def _finish_file_operation(self, mailbox: ManagedMailbox, status: str) -> None:
        mailbox.busy = False
        mailbox.status = "监听中"
        self._update_action_states()
        self.status_var.set(status)

    def delete_selected_messages(self) -> None:
        mailbox = self._active_mailbox()
        selected = list(self.message_tree.selection())
        if not mailbox or mailbox.busy or not selected:
            return
        if not messagebox.askyesno("删除邮件", f"确定从 {mailbox.address} 删除选中的 {len(selected)} 封邮件？", parent=self.root):
            return
        mailbox.busy = True
        mailbox.status = "删除邮件"
        self._update_action_states()

        def success(deleted: tuple[str, ...]) -> None:
            for message_id in deleted:
                mailbox.messages.pop(message_id, None)
            mailbox.busy = False
            mailbox.status = "监听中"
            self._render_active_messages()
            self._update_action_states()
            self.status_var.set(f"已从 {mailbox.address} 删除 {len(deleted)} 封邮件")
            self._save_mailboxes()

        self._submit(
            lambda: mailbox.client.delete_messages(selected),
            success,
            on_error=lambda exc: self._mailbox_error(mailbox, exc),
        )

    def _set_preview(self, content: str) -> None:
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", content)
        self.preview.configure(state="disabled")
        self.preview.see("1.0")

    def close(self) -> None:
        self._save_window_state(self.root, "main")
        self._save_split_state()
        for key in self.reused_registration_queue:
            mailbox = self.mailboxes.get(key)
            if mailbox:
                mailbox.status = "后台注册已取消"
        if self.reused_registration_paused_key:
            mailbox = self.mailboxes.get(self.reused_registration_paused_key)
            if mailbox and not mailbox.registered:
                mailbox.status = "人工验证未完成"
        self.reused_registration_queue.clear()
        self.reused_registration_paused_key = None
        self._save_mailboxes()
        self.closed = True
        if self.tick_after_id:
            self.root.after_cancel(self.tick_after_id)
        for mailbox in self.mailboxes.values():
            mailbox.client.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def smoke_test() -> None:
    assert normalize_browser_session_mode("独立") == ISOLATED_SESSION_MODE
    assert normalize_browser_session_mode("共享") == REUSED_SESSION_MODE
    assert normalize_browser_session_mode("invalid") == ISOLATED_SESSION_MODE
    root = tk.Tk()
    root.withdraw()
    app = TemporaryMailManagerApp(root, connect=False)
    temporary_settings = tempfile.TemporaryDirectory()
    app.app_data_root = Path(temporary_settings.name)
    app.settings_path = app.app_data_root / "settings.json"
    app.mailboxes_path = app.app_data_root / "mailboxes.json"
    app.browser_profile_root = app.app_data_root / "browser_profiles"
    app.settings = {}
    app.browser_var.set("Chrome")
    app._browser_choice_changed()
    app.browser_session_mode_var.set(REUSED_SESSION_MODE)
    app._browser_session_mode_changed()
    assert app._browser_profile_path("smoke-one", Path("chrome.exe")).name == "shared"
    for key in ("smoke-one", "smoke-two"):
        mailbox = ManagedMailbox(
            key=key,
            client=RootshClient(),
            address=f"{key}@bccto.cc",
            local_part=key,
            domain="bccto.cc",
            expires_at=time.monotonic() + 600,
            points=5000 if key == "smoke-one" else 1250,
        )
        app.mailboxes[key] = mailbox
        app.mailbox_tree.insert("", "end", iid=key, values=app._mailbox_values(mailbox))
        app._create_browser_cell_button(mailbox)
    outlook_credentials = OutlookCredentials(
        "smoke@outlook.com",
        "smoke-password",
        "smoke-client-id",
        "smoke-refresh-token",
    )
    outlook_mailbox = ManagedMailbox(
        key="smoke-outlook",
        client=OutlookClient(outlook_credentials),
        address=outlook_credentials.address,
        local_part="smoke",
        domain="outlook.com",
        expires_at=time.monotonic(),
        provider="outlook",
        status="Outlook Graph",
    )
    app.mailboxes[outlook_mailbox.key] = outlook_mailbox
    app.mailbox_tree.insert(
        "",
        "end",
        iid=outlook_mailbox.key,
        values=app._mailbox_values(outlook_mailbox),
    )
    app._create_browser_cell_button(outlook_mailbox)
    app.mailbox_tree.selection_set("smoke-one", "smoke-two")
    root.attributes("-alpha", 0.0)
    root.deiconify()
    root.update()
    app.main_pane.sashpos(0, int(app.main_pane.winfo_width() * 0.36))
    app.content_pane.sashpos(0, int(app.content_pane.winfo_height() * 0.64))
    app._save_split_state()
    root.update_idletasks()
    assert str(app.mailbox_tree.cget("selectmode")) == "extended"
    assert len(app.mailbox_tree.selection()) == 2
    assert len(app.mailbox_browser_buttons) == 3
    assert {
        str(button.cget("text"))
        for button in app.mailbox_browser_buttons["smoke-one"]
    } == {"自动打开", "手动打开"}
    dispatched: list[tuple[str, str]] = []
    original_open_browser = app.open_browser
    original_open_manual_browser = app._open_manual_browser
    app.open_browser = lambda mailbox: dispatched.append(("automatic", mailbox.key))
    app._open_manual_browser = lambda mailbox: dispatched.append(("manual", mailbox.key))
    automatic_button, manual_button = app.mailbox_browser_buttons["smoke-one"]
    automatic_button.invoke()
    manual_button.invoke()
    app.open_browser = original_open_browser
    app._open_manual_browser = original_open_manual_browser
    assert dispatched == [("automatic", "smoke-one"), ("manual", "smoke-one")]
    assert len(app.mailbox_background_buttons) == 3
    saved = json.loads(app.settings_path.read_text(encoding="utf-8"))
    assert saved["browser"] == "Chrome"
    assert saved["browser_session_mode"] == REUSED_SESSION_MODE
    assert 0.30 < saved["split"]["mailbox_detail_ratio"] < 0.42
    assert 0.58 < saved["split"]["inbox_preview_ratio"] < 0.70
    app.close()

    persisted = json.loads(app.mailboxes_path.read_text(encoding="utf-8"))
    assert len(persisted["mailboxes"]) == 3
    assert persisted["mailboxes"][0]["address"].endswith("@bccto.cc")
    assert persisted["mailboxes"][0]["points"] == 5000
    outlook_row = next(item for item in persisted["mailboxes"] if item["provider"] == "outlook")
    assert "smoke-password" not in outlook_row["outlook_secret"]
    assert "smoke-refresh-token" not in outlook_row["outlook_secret"]

    restored_root = tk.Tk()
    restored_root.withdraw()
    restored_app = TemporaryMailManagerApp(restored_root, connect=False)
    restored_app.app_data_root = Path(temporary_settings.name)
    restored_app.settings_path = restored_app.app_data_root / "settings.json"
    restored_app.mailboxes_path = restored_app.app_data_root / "mailboxes.json"
    restored_app.browser_profile_root = restored_app.app_data_root / "browser_profiles"
    restored_app._restore_mailboxes()
    assert len(restored_app.mailboxes) == 3
    assert len(restored_app.mailbox_browser_buttons) == 3
    assert len(restored_app.mailbox_background_buttons) == 3
    assert restored_app.active_key in restored_app.mailboxes
    assert restored_app.mailboxes["smoke-one"].points == 5000
    assert restored_app.mailboxes["smoke-outlook"].provider == "outlook"
    assert restored_app.mailboxes["smoke-outlook"].client.credentials.refresh_token == "smoke-refresh-token"
    restored_app.close()
    temporary_settings.cleanup()
    print("Persistence, multi-select, background/browser buttons, and split-state UI smoke test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="rootsh.com 多临时邮箱管理器")
    parser.add_argument("--smoke-test", action="store_true", help="创建并关闭窗口，用于安装检查")
    args = parser.parse_args()
    if args.smoke_test:
        smoke_test()
        return
    root = tk.Tk()
    TemporaryMailManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
