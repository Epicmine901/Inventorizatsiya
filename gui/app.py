from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from time import monotonic, strftime
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_DIR = Path(__file__).resolve().parent.parent
MAIN_DIR = PROJECT_DIR / "main"
MAIN_ENV = MAIN_DIR / ".env"
SENDER_ENV = PROJECT_DIR / "sender" / ".env"
SERVER_SCRIPT = MAIN_DIR / "main.py"
BUILD_SCRIPT = PROJECT_DIR / "build.py"
ANSI_SGR_PATTERN = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_COLORS = (
    "#000000",
    "#800000",
    "#008000",
    "#808000",
    "#000080",
    "#800080",
    "#008080",
    "#c0c0c0",
    "#808080",
    "#ff0000",
    "#00ff00",
    "#ffff00",
    "#0000ff",
    "#ff00ff",
    "#00ffff",
    "#ffffff",
)

COLORS = {
    "background": "#f2f5f9",
    "surface": "#ffffff",
    "surface_light": "#e8eef4",
    "border": "#dce4ec",
    "text": "#1b2b40",
    "muted": "#718096",
    "blue": "#147d8c",
    "green": "#16845f",
    "red": "#c44752",
    "yellow": "#b7791f",
    "sidebar": "#14263a",
    "sidebar_muted": "#a4b4c5",
    "input": "#ffffff",
    "log_background": "#111c2b",
    "log_text": "#dce6f0",
}

INVENTORY_GROUP_LABELS = {
    "Network Hardware": "Сетевое оборудование",
    "Motherboard": "Материнская плата",
    "RAM": "Оперативная память",
    "Storages": "Накопители",
    "CPU": "Процессор",
    "GPUs": "Видеокарты",
    "Monitors": "Мониторы",
    "OS": "Операционная система",
    "ActiveDirectory": "Домен и пользователь",
    "mouse_keyboard": "Клавиатура и мышь",
}
INVENTORY_FIELD_LABELS = {
    "name": "Название",
    "price": "Цена",
    "fio": "Ответственный",
    "location": "Местоположение",
    "additional": "Дополнительно",
    "in_stock": "На складе",
    "SysInfo": "Сведения об устройстве",
    "manufacturer": "Производитель",
    "model": "Модель",
    "type": "Тип",
    "ip": "IP-адрес",
    "mac": "MAC-адрес",
    "capacity_gb": "Объём, ГБ",
    "speed_mhz": "Скорость, МГц",
    "slot": "Слот",
    "serial": "Серийный номер",
    "uuid": "UUID",
    "capacity_decimal": "Ёмкость",
    "capacity_binary": "Ёмкость (двоичная)",
    "capacity_bytes": "Ёмкость, байт",
    "interface": "Интерфейс",
    "cores": "Ядра",
    "threads": "Потоки",
    "clock_speed": "Тактовая частота",
    "socket": "Сокет",
    "processor_id": "ID процессора",
    "vram": "Видеопамять",
    "driver_version": "Версия драйвера",
    "status": "Состояние",
    "version": "Версия",
    "build": "Сборка",
    "architecture": "Архитектура",
    "install_date": "Дата установки",
    "registered_user": "Зарегистрированный пользователь",
    "is_in_domain": "В домене",
    "domain_or_workgroup_name": "Домен или рабочая группа",
    "current_user": "Текущий пользователь",
    "primary_owner": "Владелец",
    "keyboards": "Клавиатуры",
    "mice": "Мыши",
    "description": "Описание",
}
INVENTORY_GROUP_ORDER = tuple(INVENTORY_GROUP_LABELS)
INVENTORY_SUMMARY_FIELDS = {
    "Network Hardware": ("name", "ip", "mac", "type"),
    "Motherboard": ("manufacturer", "model", "name"),
    "RAM": ("manufacturer", "model", "capacity_gb", "speed_mhz"),
    "Storages": ("manufacturer", "model", "type", "capacity_decimal"),
    "CPU": ("model", "cores", "threads", "clock_speed"),
    "GPUs": ("model", "vram", "manufacturer", "status"),
    "Monitors": ("manufacturer", "model"),
    "OS": ("name", "version", "architecture", "build"),
    "ActiveDirectory": (
        "is_in_domain",
        "domain_or_workgroup_name",
        "current_user",
        "primary_owner",
    ),
    "mouse_keyboard": ("keyboards", "mice"),
}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if "=" not in stripped_line:
            raise ValueError(f"Некорректная строка {line_number} в {path.name}.")
        key, value = stripped_line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def update_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    updated_keys = set()
    updated_lines = []

    for line in lines:
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            updated_lines.append(f"{key}={updates[key]}")
            updated_keys.add(key)
        else:
            updated_lines.append(line)

    updated_lines.extend(
        f"{key}={value}" for key, value in updates.items() if key not in updated_keys
    )
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        temporary_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


class InventoryManager:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, str, object]] = queue.Queue()
        self.control_process: subprocess.Popen[str] | None = None
        self.control_token = secrets.token_urlsafe(32)
        self.control_port = 0
        self.control_url = ""
        self.control_ready = False
        self.control_stop = threading.Event()
        self.api_status: dict[str, object] = {
            "server_running": False,
            "server_url": "",
            "tunnel_running": False,
            "tunnel_url": "",
        }
        self.api_event_id = 0
        self.build_process: subprocess.Popen[str] | None = None
        self.local_url = ""
        self.global_url = ""
        self._ansi_tags: dict[tuple[object, ...], str] = {}
        self.build_running = False
        self.inventory_loading = False
        self.access_key_rotating = False
        self._configure_window()
        self._build_ui()
        self._start_control_api()
        self.root.after(100, self._process_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_window(self) -> None:
        self.root.title("Inventarizatsiya | Control panel")
        self.root.geometry("1180x800")
        self.root.minsize(940, 680)
        self.root.configure(bg=COLORS["background"])
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            ".",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Segoe UI", 10),
        )
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Card.TFrame", background=COLORS["surface"])
        style.configure(
            "Settings.TEntry",
            fieldbackground=COLORS["input"],
            foreground=COLORS["text"],
            insertcolor=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=(10, 8),
        )
        style.map(
            "Settings.TEntry",
            bordercolor=[("focus", COLORS["blue"])],
            lightcolor=[("focus", COLORS["blue"])],
            darkcolor=[("focus", COLORS["blue"])],
        )
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"])
        style.configure("Card.TLabel", background=COLORS["surface"], foreground=COLORS["text"])
        style.configure(
            "Muted.Card.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
        )
        style.configure(
            "Link.Card.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["blue"],
            font=("Segoe UI", 10, "underline"),
        )
        style.configure(
            "Online.Card.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["green"],
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "Title.TLabel",
            font=("Segoe UI Semibold", 25),
            background=COLORS["background"],
            foreground=COLORS["text"],
        )
        style.configure(
            "Subtitle.TLabel",
            font=("Segoe UI", 10),
            foreground=COLORS["muted"],
            background=COLORS["background"],
        )
        style.configure(
            "Section.TLabel",
            font=("Segoe UI Semibold", 13),
            background=COLORS["background"],
            foreground=COLORS["text"],
        )
        style.configure(
            "TButton",
            padding=(14, 11),
            background=COLORS["surface"],
            foreground=COLORS["text"],
            borderwidth=1,
            bordercolor=COLORS["border"],
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "TButton",
            background=[("active", COLORS["surface_light"]), ("disabled", "#e8edf2")],
            foreground=[("disabled", "#a0acb8")],
        )
        style.configure(
            "Primary.TButton",
            background=COLORS["blue"],
            foreground="#ffffff",
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#106a77"), ("disabled", "#9bbfc4")],
            foreground=[("disabled", "#ffffff")],
        )
        style.configure(
            "Danger.TButton",
            background=COLORS["red"],
            foreground="#ffffff",
            borderwidth=0,
        )
        style.map("Danger.TButton", background=[("active", "#a83b45")])

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["background"])
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg=COLORS["sidebar"], width=218)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=COLORS["sidebar"], padx=20, pady=24)
        brand.pack(fill="x")
        tk.Label(
            brand,
            text="И",
            bg=COLORS["blue"],
            fg="#ffffff",
            font=("Segoe UI Semibold", 18),
            width=2,
            height=1,
        ).pack(side="left", padx=(0, 11))
        brand_copy = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_copy.pack(side="left")
        tk.Label(
            brand_copy,
            text="ИНВЕНТАРИЗАЦИЯ",
            bg=COLORS["sidebar"],
            fg="#ffffff",
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w")
        tk.Label(
            brand_copy,
            text="ПАНЕЛЬ УПРАВЛЕНИЯ",
            bg=COLORS["sidebar"],
            fg=COLORS["sidebar_muted"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 0))

        tk.Frame(sidebar, bg="#30445a", height=1).pack(fill="x", padx=18, pady=(2, 20))
        tk.Label(
            sidebar,
            text="РАБОЧЕЕ ПРОСТРАНСТВО",
            bg=COLORS["sidebar"],
            fg=COLORS["sidebar_muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=22, pady=(0, 9))
        self.nav_buttons: dict[str, tk.Button] = {}
        for page_key, label in (("overview", "Обзор"), ("inventory", "Таблица")):
            button = tk.Button(
                sidebar,
                text=f"┃  {label}" if page_key == "overview" else f"   {label}",
                command=lambda key=page_key: self._show_page(key),
                anchor="w",
                relief="flat",
                borderwidth=0,
                padx=12,
                pady=10,
                bg="#20374d" if page_key == "overview" else COLORS["sidebar"],
                fg="#ffffff" if page_key == "overview" else COLORS["sidebar_muted"],
                activebackground="#20374d",
                activeforeground="#ffffff",
                font=("Segoe UI Semibold", 10),
                cursor="hand2",
            )
            button.pack(fill="x", padx=12, pady=(0, 4))
            self.nav_buttons[page_key] = button
        tk.Label(
            sidebar,
            text="Сервер  ·  Туннель  ·  Сборка",
            bg=COLORS["sidebar"],
            fg=COLORS["sidebar_muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=24, pady=(13, 0))

        sidebar_footer = tk.Frame(sidebar, bg=COLORS["sidebar"], padx=20, pady=18)
        sidebar_footer.pack(side="bottom", fill="x")
        tk.Frame(sidebar_footer, bg="#30445a", height=1).pack(fill="x", pady=(0, 12))
        tk.Label(
            sidebar_footer,
            text="ЛОКАЛЬНАЯ ПАНЕЛЬ УПРАВЛЕНИЯ",
            bg=COLORS["sidebar"],
            fg=COLORS["sidebar_muted"],
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        content = tk.Frame(shell, bg=COLORS["background"], padx=28, pady=24)
        content.pack(side="left", fill="both", expand=True)
        self.page_container = tk.Frame(content, bg=COLORS["background"])
        self.page_container.pack(fill="both", expand=True)
        self.page_container.columnconfigure(0, weight=1)
        self.page_container.rowconfigure(0, weight=1)
        self.pages: dict[str, tk.Frame] = {}
        overview_page = tk.Frame(self.page_container, bg=COLORS["background"])
        overview_page.grid(row=0, column=0, sticky="nsew")
        overview_page.columnconfigure(0, weight=1)
        overview_page.rowconfigure(4, weight=1)
        inventory_page = tk.Frame(self.page_container, bg=COLORS["background"])
        inventory_page.grid(row=0, column=0, sticky="nsew")
        inventory_page.grid_remove()
        self.pages["overview"] = overview_page
        self.pages["inventory"] = inventory_page
        self.active_page = "overview"

        header = tk.Frame(overview_page, bg=COLORS["background"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 23))
        title_area = tk.Frame(header, bg=COLORS["background"])
        title_area.pack(side="left")
        ttk.Label(title_area, text="Обзор", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_area,
            text="Управление сервером инвентаризации и доступом",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Button(header, text="⚙  Настройки", command=self._open_settings).pack(
            side="right", anchor="center", pady=(5, 0)
        )

        services_heading = tk.Frame(overview_page, bg=COLORS["background"])
        services_heading.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(services_heading, text="Сервисы", style="Section.TLabel").pack(side="left")
        tk.Label(
            services_heading,
            text="ТЕКУЩЕЕ СОСТОЯНИЕ",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(side="right", pady=(4, 0))
        self._build_status_cards(overview_page)
        self._build_actions(overview_page)
        self._build_log_panel(overview_page)
        self._build_inventory_page(inventory_page)
        self._log("Готово. Выберите действие.")

    def _show_page(self, page_key: str) -> None:
        page = self.pages[page_key]
        if page_key == self.active_page:
            if page_key == "inventory":
                self._load_inventory()
            return

        self.pages[self.active_page].grid_remove()
        page.grid()
        self.active_page = page_key
        for key, button in self.nav_buttons.items():
            selected = key == page_key
            button.configure(
                text=f"┃  {('Обзор' if key == 'overview' else 'Таблица')}"
                if selected
                else f"   {('Обзор' if key == 'overview' else 'Таблица')}",
                bg="#20374d" if selected else COLORS["sidebar"],
                fg="#ffffff" if selected else COLORS["sidebar_muted"],
            )
        if page_key == "inventory":
            self._load_inventory()

    def _build_inventory_page(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        header = tk.Frame(parent, bg=COLORS["background"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        title_area = tk.Frame(header, bg=COLORS["background"])
        title_area.pack(side="left")
        ttk.Label(title_area, text="Таблица инвентаризации", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            title_area,
            text="Записи из выбранной базы данных",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        header_actions = tk.Frame(header, bg=COLORS["background"])
        header_actions.pack(side="right")
        ttk.Label(
            header_actions,
            text="Версия таблицы",
            style="Subtitle.TLabel",
        ).pack(anchor="center")
        self.inventory_version = tk.IntVar(value=1)
        self.inventory_version_slider = tk.Scale(
            header_actions,
            from_=1,
            to=2,
            resolution=1,
            orient="horizontal",
            variable=self.inventory_version,
            showvalue=True,
            tickinterval=1,
            length=145,
            sliderlength=22,
            width=12,
            bd=0,
            highlightthickness=0,
            bg=COLORS["background"],
            fg=COLORS["text"],
            troughcolor=COLORS["border"],
            activebackground=COLORS["blue"],
            command=self._set_inventory_version,
        )
        self.inventory_version_slider.pack()
        ttk.Label(
            header_actions,
            text="V1: основная  ·  V2: характеристики",
            style="Subtitle.TLabel",
        ).pack(anchor="center")
        ttk.Button(
            header_actions,
            text="⟳  Обновить",
            command=self._load_inventory,
        ).pack(anchor="e", pady=(2, 0))
        self.access_key_button = ttk.Button(
            header_actions,
            text="🔑  Обновить ключ доступа",
            command=self._refresh_mobile_access_key,
        )
        self.access_key_button.pack(anchor="e", pady=(6, 0))

        toolbar = tk.Frame(
            parent,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=14,
            pady=11,
        )
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(toolbar, text="Записи", style="Card.TLabel").pack(side="left")
        self.inventory_count = ttk.Label(
            toolbar,
            text="Загрузка…",
            style="Muted.Card.TLabel",
        )
        self.inventory_count.pack(side="left", padx=(10, 0))
        ttk.Label(
            toolbar,
            text="Подробности устройства: двойной щелчок по строке",
            style="Muted.Card.TLabel",
        ).pack(side="right")

        table_frame = tk.Frame(
            parent,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.inventory_table = ttk.Treeview(
            table_frame,
            columns=(),
            show="headings",
            selectmode="browse",
        )
        self.inventory_table.tag_configure("odd", background="#f5f8fb")
        self.inventory_table.tag_configure("even", background="#ffffff")
        self.inventory_table.bind("<Double-1>", self._show_inventory_details)
        self.inventory_table_scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.inventory_table.yview,
        )
        self.inventory_table_horizontal_scrollbar = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.inventory_table.xview,
        )
        self.inventory_table.configure(
            yscrollcommand=self.inventory_table_scrollbar.set,
            xscrollcommand=self.inventory_table_horizontal_scrollbar.set,
        )
        self.inventory_table.grid(row=0, column=0, sticky="nsew")
        self.inventory_table_scrollbar.grid(row=0, column=1, sticky="ns")
        self.inventory_table_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

        self.inventory_v2_canvas = tk.Canvas(
            table_frame,
            bg=COLORS["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.inventory_v2_scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.inventory_v2_canvas.yview,
        )
        self.inventory_v2_horizontal_scrollbar = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.inventory_v2_canvas.xview,
        )
        self.inventory_v2_canvas.configure(
            yscrollcommand=self.inventory_v2_scrollbar.set,
            xscrollcommand=self.inventory_v2_horizontal_scrollbar.set,
        )
        self.inventory_v2_canvas.bind(
            "<Configure>",
            self._update_inventory_canvas_scrollregion,
        )
        self.inventory_v2_canvas.bind(
            "<MouseWheel>",
            self._scroll_inventory_canvas,
        )
        self.inventory_v2_group_keys: list[str] = []
        self.inventory_v2_column_widths: list[int] = []
        self.inventory_records: dict[str, dict[str, object]] = {}
        self._configure_inventory_columns()

    def _update_inventory_canvas_scrollregion(self, _: object = None) -> None:
        self.inventory_v2_canvas.configure(
            scrollregion=self.inventory_v2_canvas.bbox("all")
        )

    def _scroll_inventory_canvas(self, event: tk.Event) -> str:
        self.inventory_v2_canvas.yview_scroll(
            -int(event.delta / 120),
            "units",
        )
        return "break"

    def _configure_inventory_columns(self) -> None:
        if self.inventory_version.get() == 1:
            columns = ("id", "received_at", "name", "fio", "location", "in_stock")
            headings = (
                ("id", "№", 60, "center"),
                ("received_at", "Дата получения", 160, "w"),
                ("name", "Устройство", 190, "w"),
                ("fio", "Ответственный", 180, "w"),
                ("location", "Местоположение", 180, "w"),
                ("in_stock", "На складе", 110, "center"),
            )
            self.inventory_table.configure(columns=columns, displaycolumns=columns)
            for key, label, width, anchor in headings:
                self.inventory_table.heading(key, text=label)
                self.inventory_table.column(
                    key,
                    width=width,
                    minwidth=55,
                    anchor=anchor,
                    stretch=key != "id",
                )
            self.inventory_table.grid()
            self.inventory_v2_canvas.grid_remove()
            self.inventory_v2_scrollbar.grid_remove()
            self.inventory_v2_horizontal_scrollbar.grid_remove()
            self.inventory_v2_group_keys = []
            self.inventory_v2_column_widths = []
        else:
            sysinfo_keys = {
                key
                for record in self.inventory_records.values()
                for key in self._inventory_sysinfo(record)
            }
            ordered_keys = [
                key for key in INVENTORY_GROUP_ORDER if key in sysinfo_keys
            ]
            ordered_keys.extend(sorted(sysinfo_keys - set(ordered_keys)))
            self.inventory_v2_group_keys = ordered_keys
            self.inventory_v2_column_widths = [68, *(220 for _ in ordered_keys)]
            self.inventory_table.grid_remove()
            self.inventory_table_scrollbar.grid_remove()
            self.inventory_table_horizontal_scrollbar.grid_remove()
            self.inventory_v2_canvas.grid(row=0, column=0, sticky="nsew")
            self.inventory_v2_scrollbar.grid(row=0, column=1, sticky="ns")
            self.inventory_v2_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

    def _render_inventory_v2(self) -> None:
        canvas = self.inventory_v2_canvas
        canvas.delete("inventory_v2")

        columns = [
            ("№", 68),
            *(
                (
                    INVENTORY_GROUP_LABELS.get(
                        key,
                        self._humanize_inventory_key(key),
                    ),
                    220,
                )
                for key in self.inventory_v2_group_keys
            ),
        ]
        column_positions = [0]
        for width in self.inventory_v2_column_widths:
            column_positions.append(column_positions[-1] + width)
        total_width = column_positions[-1]
        header_height = 58
        for column, (label, width) in enumerate(columns):
            x1 = column_positions[column]
            x2 = column_positions[column + 1]
            canvas.create_rectangle(
                x1,
                0,
                x2,
                header_height,
                fill=COLORS["sidebar"],
                outline=COLORS["border"],
                tags=("inventory_v2",),
            )
            canvas.create_text(
                x1 + 12,
                11,
                text=label,
                fill="#ffffff",
                font=("Segoe UI Semibold", 9),
                anchor="nw",
                width=width - 24,
                tags=("inventory_v2",),
            )

        row_y = header_height
        for row_index, (row_id, record) in enumerate(self.inventory_records.items(), 1):
            row_tag = f"inventory_v2_row_{row_index}"
            row_background = "#ffffff" if row_index % 2 else "#f2f6fa"
            sysinfo = self._inventory_sysinfo(record)
            cell_values = [row_id]
            cell_values.extend(
                self._format_inventory_group_cell(key, sysinfo.get(key))
                for key in self.inventory_v2_group_keys
            )
            background_ids: list[int] = []
            text_ids: list[int] = []
            for column, text in enumerate(cell_values):
                x1 = column_positions[column]
                x2 = column_positions[column + 1]
                background_ids.append(
                    canvas.create_rectangle(
                        x1,
                        row_y,
                        x2,
                        row_y + 1,
                        fill="#e7f1f4" if column == 0 else row_background,
                        outline=COLORS["border"],
                        tags=("inventory_v2", row_tag),
                    )
                )
                text_ids.append(
                    canvas.create_text(
                        x1 + 12,
                        row_y + 11,
                        anchor="nw",
                        width=self.inventory_v2_column_widths[column] - 24,
                        text=text,
                        fill=(
                            COLORS["blue"]
                            if column == 0
                            else COLORS["text"]
                            if text != "Нет данных"
                            else COLORS["muted"]
                        ),
                        font=(
                            ("Segoe UI Semibold", 10)
                            if column == 0
                            else ("Segoe UI", 9)
                        ),
                        tags=("inventory_v2", row_tag),
                    )
                )
            canvas.update_idletasks()
            content_bottoms = [
                bounds[3]
                for item_id in text_ids
                if (bounds := canvas.bbox(item_id)) is not None
            ]
            row_height = max(
                48,
                max(content_bottoms, default=row_y + 24) - row_y + 12,
            )
            for item_id, column in zip(background_ids, range(len(cell_values))):
                canvas.coords(
                    item_id,
                    column_positions[column],
                    row_y,
                    column_positions[column + 1],
                    row_y + row_height,
                )
            canvas.tag_bind(
                row_tag,
                "<Double-Button-1>",
                lambda event, selected=row_id: self._show_inventory_record_details(
                    selected
                ),
            )
            row_y += row_height

        canvas.configure(scrollregion=(0, 0, total_width, row_y))

    def _format_inventory_cell(
        self,
        value: object,
        indent: int = 0,
        preferred_fields: tuple[str, ...] | None = None,
    ) -> str:
        if value is None or value == "":
            return "Нет данных"
        prefix = "  " * indent
        if isinstance(value, dict):
            lines = []
            items = list(value.items())
            if preferred_fields:
                preferred = [
                    (key, value[key])
                    for key in preferred_fields
                    if key in value
                ]
                if preferred:
                    items = preferred
            for key, item in items:
                if item is None or item == "":
                    continue
                label = self._humanize_inventory_key(str(key))
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}{label}:")
                    lines.append(
                        self._format_inventory_cell(
                            item,
                            indent + 1,
                            preferred_fields,
                        )
                    )
                else:
                    lines.append(
                        f"{prefix}{label}: {self._format_inventory_value(item)}"
                    )
            return "\n".join(lines) if lines else "Нет данных"
        if isinstance(value, list):
            items = [
                self._format_inventory_cell(item, indent, preferred_fields)
                for item in value
                if item is not None and item != ""
            ]
            return "\n\n".join(items) if items else "Нет данных"
        return f"{prefix}{self._format_inventory_value(value)}"

    def _format_inventory_group_cell(self, key: str, value: object) -> str:
        if key == "mouse_keyboard" and isinstance(value, dict):
            summaries = []
            for device_key, label in (
                ("keyboards", "Клавиатуры"),
                ("mice", "Мыши"),
            ):
                devices = value.get(device_key)
                if isinstance(devices, list):
                    summaries.append(f"{label}: {len(devices)}")
            return "\n".join(summaries) if summaries else "Нет данных"
        return self._format_inventory_cell(
            value,
            preferred_fields=INVENTORY_SUMMARY_FIELDS.get(key),
        )

    def _set_inventory_version(self, value: str) -> None:
        version = int(float(value))
        self.inventory_version.set(version)
        self._configure_inventory_columns()
        self._display_inventory_rows()

    def _inventory_sysinfo(self, record: dict[str, object]) -> dict[str, object]:
        payload = record.get("_payload")
        if not isinstance(payload, dict):
            return {}
        sysinfo = payload.get("SysInfo")
        return sysinfo if isinstance(sysinfo, dict) else {}

    @staticmethod
    def _humanize_inventory_key(key: str) -> str:
        return INVENTORY_GROUP_LABELS.get(
            key,
            INVENTORY_FIELD_LABELS.get(
                key,
                key.replace("_", " ").strip().capitalize(),
            ),
        )

    def _format_inventory_value(self, value: object) -> str:
        if value is None:
            return "Не указано"
        if isinstance(value, bool):
            return "Да" if value else "Нет"
        if isinstance(value, dict):
            return " · ".join(
                f"{self._humanize_inventory_key(str(key))}: "
                f"{self._format_inventory_value(item)}"
                for key, item in value.items()
                if item is not None and item != ""
            ) or "Нет данных"
        if isinstance(value, list):
            return " • ".join(
                self._format_inventory_value(item)
                for item in value
                if item is not None and item != ""
            ) or "Нет данных"
        return str(value)

    def _format_inventory_details(
        self,
        value: object,
        indent: int = 0,
        label: str | None = None,
    ) -> list[str]:
        prefix = "  " * indent
        if isinstance(value, dict):
            heading = (
                f"{prefix}{label}"
                if label and label.endswith(".")
                else f"{prefix}{label}:"
                if label
                else ""
            )
            lines = [heading]
            child_indent = indent + 1 if label else indent
            for key, child in value.items():
                lines.extend(
                    self._format_inventory_details(
                        child,
                        child_indent,
                        self._humanize_inventory_key(str(key)),
                    )
                )
            return [line for line in lines if line]
        if isinstance(value, list):
            lines = [f"{prefix}{label}" if label else ""]
            child_indent = indent + 1 if label else indent
            for index, child in enumerate(value, 1):
                lines.extend(
                    self._format_inventory_details(
                        child,
                        child_indent,
                        f"{index}.",
                    )
                )
            return [line for line in lines if line]
        formatted_value = self._format_inventory_value(value)
        return [f"{prefix}{label}: {formatted_value}" if label else f"{prefix}{formatted_value}"]

    def _load_inventory(self) -> None:
        if self.inventory_loading:
            return
        if not self.control_ready:
            self.inventory_count.configure(text="Локальный API ещё не готов")
            self._log("Не удалось загрузить таблицу: локальный API ещё не готов.", "error")
            return

        self.inventory_loading = True
        self.inventory_count.configure(text="Загрузка…")

        def load_records() -> None:
            try:
                response = self._api_request("GET", "/api/inventory?limit=500")
                records = response.get("records")
                if not isinstance(records, list) or any(
                    not isinstance(record, dict) for record in records
                ):
                    raise ValueError("Локальный API вернул некорректный список записей.")
                self.events.put(("inventory_records", "", records))
            except (RuntimeError, OSError, ValueError) as error:
                self.events.put(("inventory_error", "", str(error)))

        threading.Thread(target=load_records, daemon=True).start()

    def _refresh_mobile_access_key(self) -> None:
        if self.access_key_rotating:
            return
        if not self.control_ready:
            messagebox.showerror(
                "Локальный API недоступен",
                "Дождитесь запуска локального API управления и повторите попытку.",
                parent=self.root,
            )
            return
        if not self.api_status.get("server_running"):
            messagebox.showwarning(
                "Сначала запустите сервер",
                "Для создания файла доступа сначала запустите сервер инвентаризации.",
                parent=self.root,
            )
            return

        server_url = (
            self.global_url
            if self.api_status.get("tunnel_running") and self.global_url
            else self.local_url
        )
        if not server_url:
            messagebox.showerror(
                "Адрес сервера недоступен",
                "Не удалось определить HTTPS-адрес сервера.",
                parent=self.root,
            )
            return

        export_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить переносимый файл доступа",
            initialdir=str(PROJECT_DIR),
            initialfile="inventory_access.json",
            defaultextension=".json",
            filetypes=(("Файл доступа JSON", "*.json"),),
        )
        if not export_path:
            return

        try:
            ca_certificate = (MAIN_DIR / "rootCA.pem").read_text(encoding="utf-8")
        except OSError as error:
            messagebox.showerror(
                "Не удалось прочитать сертификат",
                f"Файл сертификата сервера недоступен: {error}",
                parent=self.root,
            )
            return

        access_key = secrets.token_urlsafe(32)
        access_config = {
            "server_url": server_url,
            "access_key": access_key,
            "ca_certificate": ca_certificate,
        }
        config_text = json.dumps(access_config, ensure_ascii=False, indent=2) + "\n"
        key_hash = hashlib.sha256(access_key.encode("utf-8")).hexdigest()
        target_path = Path(export_path)
        self.access_key_rotating = True
        self.access_key_button.configure(state="disabled")

        def rotate_key() -> None:
            temporary_path = target_path.with_name(
                f"{target_path.name}.{secrets.token_hex(6)}.tmp"
            )
            previous_content: bytes | None = None
            had_previous_file = target_path.exists()
            try:
                if had_previous_file:
                    previous_content = target_path.read_bytes()
                temporary_path.write_text(config_text, encoding="utf-8")
                os.replace(temporary_path, target_path)
                try:
                    self._api_request(
                        "POST",
                        "/api/access-key/rotate",
                        timeout=10,
                        data={"key_hash": key_hash},
                    )
                except (RuntimeError, OSError, ValueError):
                    if previous_content is None:
                        target_path.unlink(missing_ok=True)
                    else:
                        restore_path = target_path.with_name(
                            f"{target_path.name}.{secrets.token_hex(6)}.restore"
                        )
                        restore_path.write_bytes(previous_content)
                        os.replace(restore_path, target_path)
                    raise
            except (OSError, RuntimeError, ValueError) as error:
                temporary_path.unlink(missing_ok=True)
                self.events.put(("access_key_error", "", str(error)))
                return
            self.events.put(("access_key_done", "", str(target_path)))

        threading.Thread(target=rotate_key, daemon=True).start()

    def _display_inventory(self, records: list[dict[str, object]]) -> None:
        self.inventory_records = {}
        for record in records:
            row_id = str(record.get("id", ""))
            raw_payload = record.get("payload", "{}")
            try:
                payload = json.loads(str(raw_payload))
            except (TypeError, ValueError) as error:
                self._log(
                    f"Запись №{row_id}: не удалось прочитать JSON: {error}",
                    "error",
                )
                payload = {}
            if not isinstance(payload, dict):
                self._log(f"Запись №{row_id}: JSON должен содержать объект.", "error")
                payload = {}
            self.inventory_records[row_id] = {**record, "_payload": payload}
        self._configure_inventory_columns()
        self._display_inventory_rows()

    def _display_inventory_rows(self) -> None:
        for item in self.inventory_table.get_children():
            self.inventory_table.delete(item)
        if self.inventory_version.get() == 2:
            self._render_inventory_v2()
            self.inventory_count.configure(
                text=f"Всего показано: {len(self.inventory_records)}"
            )
            return
        for index, (row_id, record) in enumerate(self.inventory_records.items()):
            values = (
                row_id,
                str(record.get("received_at", "")),
                str(record.get("name", "")),
                str(record.get("fio", "")),
                str(record.get("location", "")),
                "Да" if bool(record.get("in_stock")) else "Нет",
            )
            self.inventory_table.insert(
                "",
                "end",
                iid=row_id,
                values=values,
                tags=("even" if index % 2 == 0 else "odd",),
            )
        self.inventory_count.configure(
            text=f"Всего показано: {len(self.inventory_records)}"
        )

    def _show_inventory_details(self, _: object) -> None:
        selection = self.inventory_table.selection()
        if not selection:
            return
        self._show_inventory_record_details(selection[0])

    def _show_inventory_record_details(self, row_id: str) -> None:
        record = self.inventory_records.get(row_id)
        if record is None:
            return
        payload = record.get("_payload")
        if not isinstance(payload, dict):
            messagebox.showerror(
                "Ошибка записи",
                "Подробные данные этой записи повреждены.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Устройство: {record.get('name', '')}")
        dialog.configure(bg=COLORS["background"])
        dialog.geometry("760x600")
        dialog.minsize(520, 360)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text=f"Подробности записи №{record.get('id', '')}",
            style="Section.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(18, 10))
        details = tk.Text(
            dialog,
            bg=COLORS["log_background"],
            fg=COLORS["log_text"],
            insertbackground=COLORS["log_text"],
            relief="flat",
            font=("Cascadia Mono", 9),
            wrap="none",
            padx=14,
            pady=12,
        )
        details.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
        details.insert(
            "1.0",
            "\n".join(self._format_inventory_details(payload)),
        )
        details.configure(state="disabled")

    def _build_status_cards(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=COLORS["background"])
        row.grid(row=2, column=0, sticky="ew", pady=(0, 20))
        row.columnconfigure((0, 1), weight=1, uniform="status")
        self.status_labels: dict[str, ttk.Label] = {}
        self.status_details: dict[str, ttk.Label] = {}
        for column, (key, title, detail, icon) in enumerate(
            (
                ("server", "Локальный сервер", "Сервер инвентаризации", "01"),
                ("tunnel", "Публичная ссылка", "Cloudflare Tunnel", "02"),
            )
        ):
            card = tk.Frame(
                row,
                bg=COLORS["surface"],
                highlightbackground=COLORS["border"],
                highlightthickness=1,
                padx=18,
                pady=16,
            )
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0, 8) if column == 0 else (8, 0),
            )
            card_header = tk.Frame(card, bg=COLORS["surface"])
            card_header.pack(fill="x")
            tk.Label(
                card_header,
                text=icon,
                bg="#e6f2f3",
                fg=COLORS["blue"],
                font=("Segoe UI Semibold", 9),
                padx=8,
                pady=5,
            ).pack(side="left", padx=(0, 10))
            ttk.Label(card_header, text=title, style="Card.TLabel").pack(
                side="left", anchor="center"
            )
            label = ttk.Label(
                card,
                text="●  Не запущен",
                style="Muted.Card.TLabel",
            )
            label.pack(anchor="w", pady=(14, 5))
            self.status_labels[key] = label
            detail_label = ttk.Label(
                card,
                text=detail,
                style="Muted.Card.TLabel",
                cursor="hand2",
                takefocus=True,
            )
            detail_label.pack(anchor="w")
            detail_label.bind(
                "<Button-1>",
                lambda _, status_key=key: self._open_status_link(status_key),
            )
            detail_label.bind(
                "<Return>",
                lambda _, status_key=key: self._open_status_link(status_key),
            )
            self.status_details[key] = detail_label

    def _build_actions(self, parent: tk.Frame) -> None:
        section = tk.Frame(parent, bg=COLORS["background"])
        section.grid(row=3, column=0, sticky="ew", pady=(0, 19))
        section.columnconfigure(0, weight=1)
        top = tk.Frame(section, bg=COLORS["background"])
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Быстрые действия", style="Section.TLabel").pack(side="left")
        tk.Label(
            top,
            text="ЗАПУСК И СБОРКА",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(side="right", pady=(4, 0))
        row = tk.Frame(
            section,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=14,
            pady=13,
        )
        row.pack(fill="x")
        self.action_buttons: dict[str, ttk.Button] = {}
        actions = (
            ("server", "▶  Запустить сервер", self._toggle_server),
            ("tunnel", "↗  Запустить Global", self._toggle_tunnel),
        )
        for column, (key, label, command) in enumerate(actions):
            button = ttk.Button(
                row,
                text=label,
                command=command,
                style="Primary.TButton",
            )
            button.grid(
                row=0,
                column=column,
                sticky="w",
                padx=(0, 9),
            )
            self.action_buttons[key] = button
        ttk.Button(
            row,
            text="⟳  Пересобрать EXE",
            command=self._build_exe,
            style="TButton",
        ).grid(row=0, column=2, sticky="w")

    def _build_log_panel(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=COLORS["background"])
        panel.grid(row=4, column=0, sticky="nsew")
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)
        header = tk.Frame(panel, bg=COLORS["background"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        ttk.Label(header, text="Журнал активности", style="Section.TLabel").pack(side="left")
        ttk.Button(header, text="Очистить журнал", command=self._clear_log).pack(side="right")

        log_frame = tk.Frame(
            panel,
            bg=COLORS["log_background"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        log_frame.grid(row=1, column=0, sticky="nsew")
        self.log_text = tk.Text(
            log_frame,
            bg=COLORS["log_background"],
            fg=COLORS["log_text"],
            insertbackground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            font=("Cascadia Mono", 9),
            wrap="word",
            padx=16,
            pady=14,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.log_text.tag_configure("time", foreground="#8495a8")
        self.log_text.tag_configure("error", foreground="#ff8b96")
        self.log_text.tag_configure("success", foreground="#69d6aa")

    def _log(self, message: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        timestamp = strftime("%H:%M:%S")
        self.log_text.insert("end", f"{timestamp}  ", "time")
        foreground: str | None = None
        background: str | None = None
        bold = False
        dim = False
        italic = False
        underline = False
        position = 0

        def insert_segment(segment: str) -> None:
            if not segment:
                return
            style = (foreground, background, bold, dim, italic, underline)
            if style == (None, None, False, False, False, False):
                self.log_text.insert("end", segment, tag)
                return
            style_tag = self._ansi_tags.get(style)
            if style_tag is None:
                style_tag = f"ansi_{len(self._ansi_tags)}"
                self._ansi_tags[style] = style_tag
                options: dict[str, object] = {}
                if foreground is not None:
                    options["foreground"] = foreground
                elif dim:
                    options["foreground"] = "#94a3b8"
                if background is not None:
                    options["background"] = background
                if bold or dim or italic:
                    options["font"] = (
                        "Cascadia Mono",
                        9,
                        *(name for enabled, name in (
                            (bold, "bold"),
                            (dim, "normal"),
                            (italic, "italic"),
                        ) if enabled),
                    )
                if underline:
                    options["underline"] = True
                self.log_text.tag_configure(style_tag, **options)
            self.log_text.insert("end", segment, style_tag)

        for match in ANSI_SGR_PATTERN.finditer(message):
            insert_segment(message[position : match.start()])
            codes = [int(code) if code else 0 for code in match.group(1).split(";")]
            code_index = 0
            while code_index < len(codes):
                code = codes[code_index]
                if code == 0:
                    foreground = background = None
                    bold = dim = italic = underline = False
                elif code == 1:
                    bold = True
                elif code == 2:
                    dim = True
                elif code == 3:
                    italic = True
                elif code == 4:
                    underline = True
                elif code == 22:
                    bold = dim = False
                elif code == 23:
                    italic = False
                elif code == 24:
                    underline = False
                elif code == 39:
                    foreground = None
                elif code == 49:
                    background = None
                elif code in range(30, 38) or code in range(90, 98):
                    color_index = code - 30 if code < 90 else code - 90 + 8
                    foreground = ANSI_COLORS[color_index]
                elif code in range(40, 48) or code in range(100, 108):
                    color_index = code - 40 if code < 100 else code - 100 + 8
                    background = ANSI_COLORS[color_index]
                elif code in (38, 48) and code_index + 1 < len(codes):
                    target = "foreground" if code == 38 else "background"
                    color_mode = codes[code_index + 1]
                    if color_mode == 2 and code_index + 4 < len(codes):
                        components = codes[code_index + 2 : code_index + 5]
                        code_index += 4
                        if all(0 <= component <= 255 for component in components):
                            color = "#{:02x}{:02x}{:02x}".format(*components)
                            if target == "foreground":
                                foreground = color
                            else:
                                background = color
                    elif color_mode == 5 and code_index + 2 < len(codes):
                        color_index = codes[code_index + 2]
                        code_index += 2
                        if 0 <= color_index <= 255:
                            if color_index < 16:
                                color = ANSI_COLORS[color_index]
                            elif color_index < 232:
                                color_index -= 16
                                levels = (0, 95, 135, 175, 215, 255)
                                color = "#{:02x}{:02x}{:02x}".format(
                                    levels[color_index // 36],
                                    levels[(color_index // 6) % 6],
                                    levels[color_index % 6],
                                )
                            else:
                                gray = 8 + (color_index - 232) * 10
                                color = f"#{gray:02x}{gray:02x}{gray:02x}"
                            if target == "foreground":
                                foreground = color
                            else:
                                background = color
                code_index += 1
            position = match.end()
        insert_segment(message[position:])
        self.log_text.insert("end", "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_status_link(self, key: str) -> None:
        url = self.local_url if key == "server" else self.global_url
        if not url:
            return
        try:
            opened = webbrowser.open_new_tab(url)
        except (OSError, webbrowser.Error) as error:
            self._log(f"Не удалось открыть ссылку: {error}", "error")
            return
        if not opened:
            self._log("Не удалось открыть ссылку в браузере.", "error")

    def _start_control_api(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            self.control_port = listener.getsockname()[1]
        self.control_url = f"http://127.0.0.1:{self.control_port}"
        environment = os.environ.copy()
        environment["INVENTORY_GUI_API_TOKEN"] = self.control_token
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-u",
                    str(SERVER_SCRIPT),
                    "--gui-api",
                    "--port",
                    str(self.control_port),
                ],
                cwd=PROJECT_DIR,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            self._log(f"Не удалось запустить локальный API: {error}", "error")
            return

        self.control_process = process
        threading.Thread(
            target=self._read_control_api_output,
            args=(process,),
            daemon=True,
        ).start()
        threading.Thread(target=self._poll_control_api, daemon=True).start()

    def _read_control_api_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if line:
                self.events.put(("log", "api", line))

    def _api_request(
        self,
        method: str,
        endpoint: str,
        timeout: float = 2,
        data: dict[str, object] | None = None,
    ) -> dict[str, object]:
        headers = {"X-Inventory-Gui-Token": self.control_token}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        request = Request(
            f"{self.control_url}{endpoint}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = (
                    body.get("detail", error.reason)
                    if isinstance(body, dict)
                    else error.reason
                )
            except (UnicodeDecodeError, ValueError):
                detail = error.reason
            raise RuntimeError(f"API {error.code}: {detail}") from error
        except URLError as error:
            raise RuntimeError(f"Локальный API недоступен: {error.reason}") from error
        if not isinstance(result, dict):
            raise ValueError("Локальный API вернул некорректный ответ.")
        return result

    def _poll_control_api(self) -> None:
        process = self.control_process
        if process is None:
            return
        ready_reported = False
        startup_deadline = monotonic() + 15
        while not self.control_stop.is_set() and process.poll() is None:
            try:
                status = self._api_request("GET", "/api/status")
                events = self._api_request(
                    "GET", f"/api/events?after={self.api_event_id}"
                ).get("events", [])
                if not isinstance(events, list):
                    raise ValueError("Локальный API вернул некорректный список журнала.")
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    event_id = event.get("id")
                    source = event.get("source")
                    message = event.get("message")
                    if isinstance(event_id, int):
                        self.api_event_id = max(self.api_event_id, event_id)
                    if isinstance(source, str) and isinstance(message, str):
                        self.events.put(("log", source, message))
                self.events.put(("api_status", "", status))
                if not ready_reported:
                    self.events.put(("api_ready", "", ""))
                    ready_reported = True
            except (RuntimeError, OSError, ValueError) as error:
                if ready_reported:
                    self.events.put(("api_error", "", str(error)))
                    ready_reported = False
                elif monotonic() >= startup_deadline:
                    self.events.put(("api_error", "", str(error)))
                    return
                if self.control_stop.wait(0.25):
                    return
                continue
            if self.control_stop.wait(0.5):
                return
        if not self.control_stop.is_set() and process.poll() is not None:
            self.events.put(
                ("api_error", "", f"Локальный API завершился (код {process.returncode}).")
            )

    def _request_control(self, endpoint: str, label: str) -> None:
        if not self.control_ready:
            self._log("Локальный API ещё не готов.", "error")
            return

        def send_request() -> None:
            try:
                self._api_request("POST", endpoint, timeout=10)
            except (RuntimeError, OSError, ValueError) as error:
                self.events.put(("log", "api", f"{label}: {error}"))

        threading.Thread(target=send_request, daemon=True).start()

    def _toggle_server(self) -> None:
        endpoint = (
            "/api/server/stop"
            if self.api_status.get("server_running")
            else "/api/server/start"
        )
        self._request_control(endpoint, "Не удалось управлять сервером")

    def _toggle_tunnel(self) -> None:
        if self.api_status.get("tunnel_running"):
            self._request_control("/api/tunnel/stop", "Не удалось остановить Global")
            return
        if not self.api_status.get("server_running"):
            messagebox.showwarning(
                "Сначала запустите сервер",
                "Для публичной ссылки сначала запустите локальный сервер.",
                parent=self.root,
            )
            return
        self._request_control("/api/tunnel/start", "Не удалось запустить Global")

    def _process_events(self) -> None:
        while True:
            try:
                event, _, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                line = str(value)
                tag = "error" if re.search(r"\b(error|failed|ошибка)\b", line, re.I) else ""
                self._log(line, tag)
            elif event == "api_status" and isinstance(value, dict):
                self._apply_api_status(value)
            elif event == "api_ready":
                if not self.control_ready:
                    self._log("Локальный API управления запущен.", "success")
                self.control_ready = True
            elif event == "api_error":
                self.control_ready = False
                self._log(str(value), "error")
            elif event == "inventory_records" and isinstance(value, list):
                self.inventory_loading = False
                if self.active_page == "inventory":
                    self._display_inventory(value)
            elif event == "inventory_error":
                self.inventory_loading = False
                self.inventory_count.configure(text="Не удалось загрузить записи")
                self._log(f"Не удалось загрузить таблицу: {value}", "error")
                if self.active_page == "inventory":
                    messagebox.showerror(
                        "Ошибка базы данных",
                        str(value),
                        parent=self.root,
                    )
            elif event == "access_key_done":
                self.access_key_rotating = False
                self.access_key_button.configure(state="normal")
                messagebox.showinfo(
                    "Ключ доступа обновлён",
                    "Файл сохранён:\n"
                    f"{value}\n\n"
                    "Передайте этот файл вместе с inventory_mobile.py. "
                    "В Termux запустите python inventory_mobile.py; "
                    "если JSON лежит в другом месте, укажите его путь аргументом. "
                    "Терминал оставьте запущенным, пока пользуетесь таблицей. "
                    "Обновление ключа отзывает прежние копии.",
                    parent=self.root,
                )
            elif event == "access_key_error":
                self.access_key_rotating = False
                self.access_key_button.configure(state="normal")
                messagebox.showerror(
                    "Не удалось обновить ключ доступа",
                    str(value),
                    parent=self.root,
                )
            elif event == "build_done":
                self.build_running = False
                self.build_process = None
                if int(value) == 0:
                    self._log("Сборка EXE успешно завершена.", "success")
                else:
                    self._log(f"Сборка завершилась с кодом {value}.", "error")
        self.root.after(100, self._process_events)

    def _apply_api_status(self, status: dict[str, object]) -> None:
        tunnel_was_running = bool(self.api_status.get("tunnel_running"))
        self.api_status = status
        tunnel_is_running = bool(status.get("tunnel_running"))
        self._set_status("server", bool(status.get("server_running")))
        self._set_status("tunnel", tunnel_is_running)

        server_url = str(status.get("server_url", ""))
        if server_url != self.local_url:
            self.local_url = server_url
            self.status_details["server"].configure(
                text=server_url or "Сервер инвентаризации",
                style="Link.Card.TLabel" if server_url else "Muted.Card.TLabel",
            )

        tunnel_url = str(status.get("tunnel_url", ""))
        if tunnel_url != self.global_url or (
            tunnel_was_running and not tunnel_is_running
        ):
            self.global_url = tunnel_url
            self.status_details["tunnel"].configure(
                text=tunnel_url or "Cloudflare Tunnel",
                style="Link.Card.TLabel" if tunnel_url else "Muted.Card.TLabel",
            )
            try:
                update_env(SENDER_ENV, {"INVENTORY_GLOBAL_URL": tunnel_url})
            except (OSError, ValueError) as error:
                self._log(f"Не удалось сохранить Global URL: {error}", "error")
            else:
                if tunnel_url:
                    self._log("Публичный адрес сохранён для следующей сборки.", "success")

    def _set_status(self, key: str, running: bool) -> None:
        label = self.status_labels[key]
        label.configure(
            text="●  Работает" if running else "●  Остановлен",
            style="Online.Card.TLabel" if running else "Muted.Card.TLabel",
        )
        button = self.action_buttons[key]
        button.configure(
            text=(
                "■  Остановить сервер"
                if running and key == "server"
                else "■  Остановить Global"
                if running
                else "▶  Запустить сервер"
                if key == "server"
                else "↗  Запустить Global"
            ),
            style="Danger.TButton" if running else "Primary.TButton",
        )

    def _build_exe(self) -> None:
        if self.build_running:
            self._log("Сборка уже выполняется.")
            return
        try:
            server_config = read_env(MAIN_ENV)
            lan_ip = ipaddress.ip_address(server_config["LAN_IP"])
            port = int(server_config["PORT"])
            if not 1 <= port <= 65535:
                raise ValueError("PORT должен быть в диапазоне 1–65535.")
        except (OSError, KeyError, ValueError) as error:
            messagebox.showerror("Ошибка настроек", str(error), parent=self.root)
            self._log(f"Не удалось подготовить настройки сборки: {error}", "error")
            return

        updates = {
            "INVENTORY_LOCAL_URL": f"https://{lan_ip}:{port}",
            "INVENTORY_GLOBAL_URL": self.global_url,
        }
        try:
            update_env(SENDER_ENV, updates)
        except OSError as error:
            self._log(f"Не удалось обновить sender/.env: {error}", "error")
            messagebox.showerror("Ошибка настроек", str(error), parent=self.root)
            return

        if not BUILD_SCRIPT.is_file():
            self._log("Не найден build.py.", "error")
            return
        self.build_running = True
        self._log(
            "sender/.env обновлён адресом из main/.env. Начинаю сборку…",
            "success",
        )
        threading.Thread(
            target=self._run_build,
            daemon=True,
        ).start()

    def _run_build(self) -> None:
        try:
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", str(BUILD_SCRIPT)],
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.build_process = process
            if process.stdout is not None:
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        self.events.put(("log", "build", line))
            return_code = process.wait()
            self.events.put(("build_done", "build", return_code))
        except OSError as error:
            self.events.put(("log", "build", f"Ошибка запуска сборки: {error}"))
            self.events.put(("build_done", "build", 1))

    def _open_settings(self) -> None:
        try:
            values = read_env(MAIN_ENV)
        except (OSError, ValueError) as error:
            messagebox.showerror("Ошибка настроек", str(error), parent=self.root)
            return

        settings_colors = getattr(self, "settings_colors", COLORS)
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройки приложения")
        dialog.configure(bg=settings_colors["background"])
        dialog.resizable(True, True)
        dialog.minsize(900, 570)
        dialog.transient(self.root)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Настройки",
            font=("Segoe UI Semibold", 20),
        ).pack(anchor="w")
        ttk.Label(
            body,
            text="Параметры SQL-подключения и доверенных устройств",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 18))

        columns = ttk.Frame(body)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure((0, 1), weight=1, uniform="settings")
        columns.rowconfigure(0, weight=1)

        sql_card = tk.Frame(
            columns,
            bg=settings_colors["surface"],
            highlightbackground=settings_colors["border"],
            highlightthickness=1,
            padx=18,
            pady=16,
        )
        sql_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ip_card = tk.Frame(
            columns,
            bg=settings_colors["surface"],
            highlightbackground=settings_colors["border"],
            highlightthickness=1,
            padx=18,
            pady=16,
        )
        ip_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        ttk.Label(
            sql_card,
            text="Подключение SQL",
            font=("Segoe UI Semibold", 14),
            style="Card.TLabel",
        ).pack(anchor="w", pady=(0, 14))
        ttk.Label(
            sql_card,
            text="Режим хранения",
            style="Muted.Card.TLabel",
        ).pack(anchor="w")
        stored_mode = values.get(
            "db_mode",
            "server" if values.get("SQL_ADDRESS") else "local",
        ).strip().lower()
        mode_var = tk.IntVar(value=1 if stored_mode == "server" else 0)
        mode_selector = tk.Frame(sql_card, bg=settings_colors["surface"])
        mode_selector.pack(fill="x", pady=(4, 12))
        ttk.Label(
            mode_selector,
            text="Local",
            style="Muted.Card.TLabel",
        ).pack(side="left")
        mode_slider = tk.Scale(
            mode_selector,
            from_=0,
            to=1,
            resolution=1,
            orient="horizontal",
            variable=mode_var,
            showvalue=False,
            length=150,
            sliderlength=28,
            width=12,
            bd=0,
            highlightthickness=0,
            bg=settings_colors["surface"],
            fg=settings_colors["text"],
            troughcolor=settings_colors["border"],
            activebackground=settings_colors["blue"],
            command=lambda _: update_database_mode(),
        )
        mode_slider.pack(side="left", padx=10)
        ttk.Label(
            mode_selector,
            text="Server",
            style="Muted.Card.TLabel",
        ).pack(side="left")

        local_database_fields = tk.Frame(sql_card, bg=settings_colors["surface"])
        ttk.Label(
            local_database_fields,
            text="Файл локальной базы (SQLite)",
            style="Muted.Card.TLabel",
        ).pack(anchor="w")
        local_path_row = ttk.Frame(local_database_fields)
        local_path_row.pack(fill="x", pady=(5, 10))
        db_path = ttk.Entry(
            local_path_row,
            style="Settings.TEntry",
        )
        db_path.insert(
            0,
            values.get("db_path", str(MAIN_DIR / "inventory.sqlite3")),
        )
        db_path.pack(side="left", fill="x", expand=True)

        def choose_database_file() -> None:
            selected_path = filedialog.asksaveasfilename(
                parent=dialog,
                title="Выберите файл SQLite",
                defaultextension=".sqlite3",
                initialdir=str(MAIN_DIR),
                initialfile="inventory.sqlite3",
                filetypes=(
                    ("SQLite database", "*.sqlite3 *.sqlite *.db"),
                    ("All files", "*.*"),
                ),
            )
            if selected_path:
                db_path.delete(0, "end")
                db_path.insert(0, selected_path)

        ttk.Button(
            local_path_row,
            text="Обзор…",
            command=choose_database_file,
        ).pack(side="left", padx=(8, 0))

        server_database_fields = tk.Frame(sql_card, bg=settings_colors["surface"])
        ttk.Label(
            server_database_fields,
            text="Адрес PostgreSQL (например, postgresql://server:5432/inventory)",
            style="Muted.Card.TLabel",
        ).pack(anchor="w")
        db_url = ttk.Entry(
            server_database_fields,
            width=48,
            style="Settings.TEntry",
        )
        db_url.insert(0, values.get("db_url", values.get("SQL_ADDRESS", "")))
        db_url.pack(fill="x", pady=(5, 12))

        authentication_fields = tk.Frame(
            server_database_fields,
            bg=settings_colors["surface"],
        )
        ttk.Label(authentication_fields, text="Admin", style="Muted.Card.TLabel").pack(
            anchor="w"
        )
        db_admin = ttk.Entry(
            authentication_fields,
            width=48,
            style="Settings.TEntry",
        )
        db_admin.insert(0, values.get("db_admin", values.get("SQL_ADMIN", "")))
        db_admin.pack(fill="x", pady=(5, 10))
        ttk.Label(
            authentication_fields,
            text="Password",
            style="Muted.Card.TLabel",
        ).pack(anchor="w")
        db_pass = ttk.Entry(
            authentication_fields,
            width=48,
            show="*",
            style="Settings.TEntry",
        )
        db_pass.insert(0, values.get("db_pass", values.get("SQL_PASSWORD", "")))
        db_pass.pack(fill="x", pady=(5, 12))
        authentication_visible = False

        def toggle_authentication() -> None:
            nonlocal authentication_visible
            authentication_visible = not authentication_visible
            if authentication_visible:
                authentication_fields.pack(fill="x", pady=(2, 0))
                authentication_button.configure(text="Скрыть Authentication")
            else:
                authentication_fields.pack_forget()
                authentication_button.configure(text="Authentication")

        authentication_button = ttk.Button(
            server_database_fields,
            text="Authentication",
            command=toggle_authentication,
        )
        authentication_button.pack(anchor="w", pady=(0, 8))

        def update_database_mode() -> None:
            if mode_var.get() == 0:
                server_database_fields.pack_forget()
                local_database_fields.pack(fill="x")
            else:
                local_database_fields.pack_forget()
                server_database_fields.pack(fill="x")

        update_database_mode()

        ttk.Label(
            ip_card,
            text="Доверенные IP",
            font=("Segoe UI Semibold", 14),
            style="Card.TLabel",
        ).pack(anchor="w", pady=(0, 5))
        ttk.Label(
            ip_card,
            text="Эти устройства увидят кнопку остановки сервера. Один адрес на строку.",
            style="Muted.Card.TLabel",
            wraplength=390,
        ).pack(anchor="w", pady=(0, 12))
        text = tk.Text(
            ip_card,
            width=42,
            height=12,
            bg=settings_colors.get("input", "#0b1220"),
            fg=settings_colors["text"],
            insertbackground=settings_colors["text"],
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=settings_colors["border"],
            highlightcolor=settings_colors["blue"],
            font=("Cascadia Mono", 10),
            padx=12,
            pady=12,
        )
        current_ips = values.get("TRUSTED_STOP_IPS", "").replace(",", "\n")
        text.insert("1.0", current_ips)
        text.pack(fill="both", expand=True)
        ttk.Label(
            ip_card,
            text="Пример: 192.168.1.42",
            style="Muted.Card.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(18, 0))

        def save_settings() -> None:
            addresses = []
            for raw_address in text.get("1.0", "end").splitlines():
                address = raw_address.strip()
                if not address:
                    continue
                try:
                    normalized_address = str(ipaddress.ip_address(address))
                except ValueError:
                    messagebox.showerror(
                        "Неверный IP",
                        f"Проверьте адрес: {address}",
                        parent=dialog,
                    )
                    return
                if normalized_address not in addresses:
                    addresses.append(normalized_address)
            try:
                database_mode = "server" if mode_var.get() == 1 else "local"
                database_path = db_path.get().strip()
                database_url = db_url.get().strip()
                if database_mode == "local" and not database_path:
                    messagebox.showerror(
                        "Не выбран файл базы",
                        "Укажите файл SQLite для локального режима.",
                        parent=dialog,
                    )
                    return
                if database_mode == "server" and not database_url:
                    messagebox.showerror(
                        "Не указан адрес базы",
                        "Укажите адрес PostgreSQL для серверного режима.",
                        parent=dialog,
                    )
                    return
                update_env(
                    MAIN_ENV,
                    {
                        "TRUSTED_STOP_IPS": ",".join(addresses),
                        "db_mode": database_mode,
                        "db_path": database_path,
                        "db_url": database_url,
                        "db_admin": db_admin.get().strip(),
                        "db_pass": db_pass.get(),
                    },
                )
            except OSError as error:
                messagebox.showerror("Не удалось сохранить", str(error), parent=dialog)
                return
            self._log(
                "Настройки сохранены. Перезапустите сервер для применения базы данных.",
                "success",
            )
            dialog.destroy()

        ttk.Button(
            buttons,
            text="Сохранить",
            style="Primary.TButton",
            command=save_settings,
        ).pack(side="right")
        ttk.Button(
            buttons,
            text="Отмена",
            command=dialog.destroy,
        ).pack(side="right", padx=(0, 8))
        dialog.update_idletasks()
        window_width = 980
        window_height = 660
        position_x = self.root.winfo_rootx() + (
            self.root.winfo_width() - window_width
        ) // 2
        position_y = self.root.winfo_rooty() + (
            self.root.winfo_height() - window_height
        ) // 2
        dialog.geometry(
            f"{window_width}x{window_height}+{max(0, position_x)}+{max(0, position_y)}"
        )

    def _close(self) -> None:
        active_services = bool(
            self.api_status.get("server_running")
            or self.api_status.get("tunnel_running")
        )
        build_active = (
            self.build_process is not None and self.build_process.poll() is None
        )
        if (active_services or build_active) and not messagebox.askyesno(
            "Завершить работу",
            "Остановить запущенные процессы и закрыть приложение?",
            parent=self.root,
        ):
            return

        self.control_stop.set()
        if self.control_process is not None and self.control_process.poll() is None:
            try:
                self._api_request("POST", "/api/shutdown")
                self.control_process.wait(timeout=8)
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError):
                if self.control_process.poll() is None:
                    self.control_process.terminate()
                    self.control_process.wait(timeout=2)
        if build_active and self.build_process is not None:
            self.build_process.terminate()
        if self.global_url:
            try:
                update_env(SENDER_ENV, {"INVENTORY_GLOBAL_URL": ""})
            except (OSError, ValueError) as error:
                messagebox.showerror(
                    "Не удалось очистить публичный адрес",
                    str(error),
                    parent=self.root,
                )
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    InventoryManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()
