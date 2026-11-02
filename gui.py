"""
=============================================================
  VisaBot — Dedicated Desktop Slot Monitor (Client Edition)
  ─────────────────────────────────────────────────────────
  Role: Purely Slot Monitoring & Remote Dispatch.
  All account management and booking execution is handled
  by the central GoDaddy Server Hub.
=============================================================
"""

import os
import sys
import json
import time
import threading
import subprocess
import requests
from datetime import datetime
from pathlib import Path

import socket
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

# Ensure project root is on Python path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

# ─── Color Palette & Styling (Warm Cream / Ivory Luxury) ───────────────────
BG           = "#F7F3EA"   # Warm cream - main background
SURFACE      = "#FFFDF8"   # Off-white/ivory cards and panels
SURFACE_CARD = "#F0EAE1"   # Warm ivory ribbon / inner cards
ACCENT       = "#B8860B"   # Classic golden/dark gold
ACCENT_HOVER = "#946F09"   # Darker gold on hover
SUCCESS      = "#2E7D32"   # Deep green
DANGER       = "#C62828"   # Deep red
INFO         = "#1976D2"   # Deep blue
WARNING      = "#C27A00"   # Warm amber
TEXT         = "#2F2A24"   # Dark warm gray/brown
TEXT_MUTED   = "#756F67"   # Muted warm gray
ENTRY_BG     = "#FFFDF8"   # Input background
ENTRY_FG     = "#2F2A24"   # Input text
BORDER       = "#D8D0C2"   # Warm gray/beige border

ALL_CITIES = ["CHENNAI", "MUMBAI", "HYDERABAD", "DELHI", "KOLKATA"]

CONFIG_FILE = PROJECT_ROOT / "monitor_config.json"


def load_local_config():
    defaults = {
        "server_url": os.getenv("SERVER_URL", "http://localhost:5000"),
        "api_key": os.getenv("SERVER_API_KEY", "visabot-secure-api-key-2026"),
        "poll_interval_min": int(os.getenv("MONITOR_INTERVAL_MIN", "45")),
        "poll_interval_max": int(os.getenv("MONITOR_INTERVAL_MAX", "75")),
        "laptop_role": os.getenv("LAPTOP_ROLE", "ALL_IN_ONE"),
        "auto_sync_on_start": True
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_local_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


class SlotMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VisaWin - You dream. We monitor. You move.")
        self.configure(bg=BG)
        self.geometry("1100x750")
        self.minsize(980, 650)

        # Set VisaWin Logo Icon (Toolbar & Window Decoration)
        ico_path = PROJECT_ROOT / "images" / "bot_logo.ico"
        png_path = PROJECT_ROOT / "images" / "bot_logo.png"
        if not png_path.exists():
            png_path = PROJECT_ROOT / "assets" / "bot_logo.png"

        if ico_path.exists():
            try:
                self.iconbitmap(str(ico_path))
            except Exception:
                pass

        if png_path.exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(str(png_path)).resize((32, 32), Image.Resampling.LANCZOS)
                self.icon_img = ImageTk.PhotoImage(img)
                self.iconphoto(False, self.icon_img)
            except Exception:
                pass


        self.config_data = load_local_config()
        self.server_online = False
        self.local_server_proc = None
        self.monitoring_proc = None
        self.is_monitoring = False
        self.countdown_seconds = 0
        self.countdown_active = False

        self.search_matches = []
        self.search_current_idx = -1

        self.synced_targets = {
            "ofcCities": ALL_CITIES,
            "consularCities": ALL_CITIES,
            "minDate": "2026-08-19",
            "maxDate": "2027-12-31",
            "total_active_accounts": 0
        }

        self.city_cards = {}  # {city: {"date_lbl": ..., "status_lbl": ..., "time_lbl": ...}}

        self._configure_styles()
        self._build_ui()

        # Auto-launch local Server Hub if targeting localhost
        self._start_local_server_if_needed()

        # Protocol for clean exit
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Initial Server Health Check in background (after brief pause for server startup)
        self.after(1200, self._check_server_connection)

        self.last_poll_time = ""
        # Background tailer to keep City Slot Matrix live-synced from orchestrator logs
        self._start_log_tailer()

        # Dynamic Fleet Status Badges auto-refresher (every 3.5s)
        self.after(2000, self._refresh_fleet_status_badges)

    def _configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Card.TFrame", background=SURFACE_CARD)

        # Labels
        style.configure("Header.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI", 13, "bold"))
        style.configure("Sub.TLabel", background=SURFACE, foreground=TEXT_MUTED, font=("Segoe UI", 9))
        style.configure("Gold.TLabel", background=SURFACE, foreground=ACCENT, font=("Segoe UI", 11, "bold"))

    def _build_ui(self):
        # ── 1. Top Header & Server Connection Bar ─────────────────────
        top_frame = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        top_frame.pack(fill=tk.X, padx=15, pady=(15, 8))

        top_inner = tk.Frame(top_frame, bg=SURFACE)
        top_inner.pack(fill=tk.X, padx=16, pady=12)

        # Title & Branding
        brand_box = tk.Frame(top_inner, bg=SURFACE)
        brand_box.pack(side=tk.LEFT)

        png_path = PROJECT_ROOT / "images" / "bot_logo.png"
        if not png_path.exists():
            png_path = PROJECT_ROOT / "assets" / "bot_logo.png"
        self.header_logo_img = None
        if png_path.exists():
            try:
                from PIL import Image, ImageTk
                l_img = Image.open(str(png_path)).resize((44, 44), Image.Resampling.LANCZOS)
                self.header_logo_img = ImageTk.PhotoImage(l_img)
                tk.Label(brand_box, image=self.header_logo_img, bg=SURFACE).pack(side=tk.LEFT, padx=(0, 10))
            except Exception:
                tk.Label(brand_box, text="⚡", font=("Segoe UI", 18), bg=SURFACE, fg=ACCENT).pack(side=tk.LEFT, padx=(0, 8))
        else:
            tk.Label(brand_box, text="⚡", font=("Segoe UI", 18), bg=SURFACE, fg=ACCENT).pack(side=tk.LEFT, padx=(0, 8))

        title_box = tk.Frame(brand_box, bg=SURFACE)
        title_box.pack(side=tk.LEFT)
        tk.Label(title_box, text="VisaWin", font=("Segoe UI", 16, "bold"), bg=SURFACE, fg=TEXT).pack(anchor="w")
        tk.Label(title_box, text="You dream. We monitor. You move.", font=("Segoe UI", 9, "bold"), bg=SURFACE, fg=ACCENT).pack(anchor="w")


        # Server Settings & Sync Capsule
        server_box = tk.Frame(top_inner, bg=SURFACE)
        server_box.pack(side=tk.RIGHT, fill=tk.Y)

        # Server URL Capsule
        url_capsule = tk.Frame(server_box, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1, padx=6, pady=3)
        url_capsule.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(url_capsule, text="🌐 URL:", font=("Segoe UI", 8, "bold"), bg=SURFACE_CARD, fg=TEXT).pack(side=tk.LEFT, padx=(0, 4))
        self.ent_server_url = tk.Entry(
            url_capsule,
            font=("Segoe UI", 9),
            width=22,
            bg=ENTRY_BG,
            fg=ENTRY_FG,
            insertbackground=ENTRY_FG,
            relief="flat"
        )
        self.ent_server_url.insert(0, self.config_data.get("server_url", "http://localhost:5000"))
        self.ent_server_url.pack(side=tk.LEFT)

        # API Key Capsule with Mask & Visibility Toggle
        key_capsule = tk.Frame(server_box, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1, padx=6, pady=3)
        key_capsule.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(key_capsule, text="🔑 Key:", font=("Segoe UI", 8, "bold"), bg=SURFACE_CARD, fg=TEXT).pack(side=tk.LEFT, padx=(0, 4))
        self.ent_api_key = tk.Entry(
            key_capsule,
            font=("Segoe UI", 9),
            width=16,
            bg=ENTRY_BG,
            fg=ENTRY_FG,
            insertbackground=ENTRY_FG,
            relief="flat",
            show="●"
        )
        self.ent_api_key.insert(0, self.config_data.get("api_key", "visabot-secure-api-key-2026"))
        self.ent_api_key.pack(side=tk.LEFT, padx=(0, 2))

        self.btn_toggle_key = tk.Button(
            key_capsule,
            text="👁",
            font=("Segoe UI", 7),
            bg=SURFACE_CARD,
            fg=TEXT_MUTED,
            activebackground=BORDER,
            relief="flat",
            padx=2,
            pady=0,
            cursor="hand2",
            command=self._toggle_api_key_visibility
        )
        self.btn_toggle_key.pack(side=tk.LEFT)

        self.btn_sync = tk.Button(
            server_box,
            text="🔄 Sync",
            font=("Segoe UI", 9, "bold"),
            bg=SURFACE_CARD,
            fg=TEXT,
            activebackground=BORDER,
            activeforeground=TEXT,
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._sync_targets_from_server
        )
        self.btn_sync.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_open_web = tk.Button(
            server_box,
            text="🌐 Web Hub",
            font=("Segoe UI", 9, "bold"),
            bg="#EAE2D4",
            fg=ACCENT,
            activebackground=ACCENT,
            activeforeground="#FFFFFF",
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._open_web_dashboard
        )
        self.btn_open_web.pack(side=tk.LEFT, padx=(0, 8))

        self.lbl_server_badge = tk.Label(
            server_box,
            text="● Connecting...",
            font=("Segoe UI", 9, "bold"),
            bg=SURFACE_CARD,
            fg=WARNING,
            padx=8,
            pady=4,
            highlightbackground=BORDER,
            highlightthickness=1
        )
        self.lbl_server_badge.pack(side=tk.LEFT)

        # ── 2. Sync Info & Targets Ribbon ────────────────────────────
        ribbon_frame = tk.Frame(self, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1)
        ribbon_frame.pack(fill=tk.X, padx=15, pady=6)

        ribbon_inner = tk.Frame(ribbon_frame, bg=SURFACE_CARD)
        ribbon_inner.pack(fill=tk.X, padx=16, pady=8)

        # Active Targets Display
        self.lbl_targets_info = tk.Label(
            ribbon_inner,
            text="🎯 Active Targets: Syncing with server...",
            font=("Segoe UI", 10),
            bg=SURFACE_CARD,
            fg=ACCENT
        )
        self.lbl_targets_info.pack(side=tk.LEFT)

        self.lbl_accounts_count = tk.Label(
            ribbon_inner,
            text="👥 Server Clients: --",
            font=("Segoe UI", 10),
            bg=SURFACE_CARD,
            fg=TEXT_MUTED
        )
        self.lbl_accounts_count.pack(side=tk.RIGHT)

        # ── 2b. Fleet Radar & Hot-Swap Live Status Bar ──────────────────
        fleet_frame = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        fleet_frame.pack(fill=tk.X, padx=15, pady=(0, 6))

        fleet_inner = tk.Frame(fleet_frame, bg=SURFACE, padx=12, pady=7)
        fleet_inner.pack(fill=tk.X)

        mode_box = tk.Frame(fleet_inner, bg=SURFACE)
        mode_box.pack(side=tk.LEFT)

        self.lbl_radar_mode = tk.Label(
            mode_box,
            text="📡 FLEET RADAR: ALL 5 CITIES",
            font=("Segoe UI", 8, "bold"),
            bg="#E8F5E9",
            fg="#2E7D32",
            padx=8,
            pady=3,
            highlightbackground="#A5D6A7",
            highlightthickness=1
        )
        self.lbl_radar_mode.pack(side=tk.LEFT, padx=(0, 8))

        self.lbl_hotswap_mode = tk.Label(
            mode_box,
            text="🔄 AUTO HOT-SWAP: ARMED",
            font=("Segoe UI", 8, "bold"),
            bg="#FFF8E1",
            fg="#B8860B",
            padx=8,
            pady=3,
            highlightbackground="#FFE082",
            highlightthickness=1
        )
        self.lbl_hotswap_mode.pack(side=tk.LEFT, padx=(0, 10))

        self.fleet_badges_frame = tk.Frame(fleet_inner, bg=SURFACE)
        self.fleet_badges_frame.pack(side=tk.RIGHT)

        # ── 3. Live City Slot Matrix ─────────────────────────────────
        matrix_frame = tk.Frame(self, bg=BG)
        matrix_frame.pack(fill=tk.X, padx=15, pady=8)

        # Grid of 5 cities
        for idx, city in enumerate(ALL_CITIES):
            card = tk.Frame(matrix_frame, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
            card.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(0 if idx == 0 else 6, 0))

            card_inner = tk.Frame(card, bg=SURFACE, padx=12, pady=10)
            card_inner.pack(fill=tk.BOTH, expand=True)

            # City Header
            head_box = tk.Frame(card_inner, bg=SURFACE)
            head_box.pack(fill=tk.X)
            tk.Label(head_box, text=city, font=("Segoe UI", 11, "bold"), bg=SURFACE, fg=TEXT).pack(side=tk.LEFT)
            status_dot = tk.Label(head_box, text="● IDLE", font=("Segoe UI", 8, "bold"), bg=SURFACE, fg=TEXT_MUTED)
            status_dot.pack(side=tk.RIGHT)

            # Date Display
            date_lbl = tk.Label(card_inner, text="No Slot Yet", font=("Segoe UI", 13, "bold"), bg=SURFACE, fg=TEXT_MUTED)
            date_lbl.pack(anchor="w", pady=(8, 4))

            # Last Checked Time
            time_lbl = tk.Label(card_inner, text="Last check: --", font=("Segoe UI", 8), bg=SURFACE, fg=TEXT_MUTED)
            time_lbl.pack(anchor="w")

            self.city_cards[city] = {
                "card": card,
                "status_lbl": status_dot,
                "date_lbl": date_lbl,
                "time_lbl": time_lbl
            }

        # ── 4. Monitoring Controls Bar ──────────────────────────────
        controls_frame = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        controls_frame.pack(fill=tk.X, padx=15, pady=8)

        controls_inner = tk.Frame(controls_frame, bg=SURFACE, padx=16, pady=12)
        controls_inner.pack(fill=tk.X)

        # Action Buttons (Uniform height & sizing)
        self.btn_start = tk.Button(
            controls_inner,
            text="▶ START MONITORING",
            font=("Segoe UI", 9, "bold"),
            bg=ACCENT,
            fg="#FFFFFF",
            activebackground=ACCENT_HOVER,
            activeforeground="#FFFFFF",
            relief="flat",
            padx=14,
            pady=4,
            cursor="hand2",
            command=self._start_monitoring
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = tk.Button(
            controls_inner,
            text="⏹ STOP",
            font=("Segoe UI", 9, "bold"),
            bg="#FDE8E8",
            fg=DANGER,
            activebackground=DANGER,
            activeforeground="#FFFFFF",
            relief="flat",
            padx=14,
            pady=4,
            state=tk.DISABLED,
            cursor="hand2",
            command=self._on_stop_clicked
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_manual_check = tk.Button(
            controls_inner,
            text="⚡ CHECK NOW",
            font=("Segoe UI", 9, "bold"),
            bg=SURFACE_CARD,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            padx=14,
            pady=4,
            cursor="hand2",
            command=self._manual_check_slot
        )
        self.btn_manual_check.pack(side=tk.LEFT, padx=(0, 10))

        # Mode selector capsule
        mode_capsule = tk.Frame(controls_inner, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1, padx=6, pady=3)
        mode_capsule.pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(mode_capsule, text="⚙️ Mode:", font=("Segoe UI", 9, "bold"), bg=SURFACE_CARD, fg=TEXT).pack(side=tk.LEFT, padx=(0, 4))
        self.var_laptop_role = tk.StringVar(value=self.config_data.get("laptop_role", "ALL_IN_ONE"))
        self.cmb_role = ttk.Combobox(
            mode_capsule,
            textvariable=self.var_laptop_role,
            values=["ALL_IN_ONE", "POLLING", "BOOKING"],
            state="readonly",
            width=12,
            font=("Segoe UI", 9, "bold")
        )
        self.cmb_role.pack(side=tk.LEFT)

        # Status & Countdown Card (matching button height)
        status_box = tk.Frame(controls_inner, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=4)
        status_box.pack(side=tk.RIGHT)
        self.lbl_countdown = tk.Label(status_box, text="● Status: Stopped", font=("Segoe UI", 9, "bold"), bg=SURFACE_CARD, fg=TEXT_MUTED)
        self.lbl_countdown.pack()


        # ── 5. Live Console Logs ─────────────────────────────────────
        log_frame = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=(4, 15))

        log_header = tk.Frame(log_frame, bg=SURFACE, padx=14, pady=8)
        log_header.pack(fill=tk.X)
        tk.Label(log_header, text="📡 Live Monitoring Log", font=("Segoe UI", 10, "bold"), bg=SURFACE, fg=TEXT).pack(side=tk.LEFT)

        # Right tools container: Search bar, Auto-Scroll checkbox, Clear Log button
        log_tools = tk.Frame(log_header, bg=SURFACE)
        log_tools.pack(side=tk.RIGHT)

        # Search Bar Capsule
        search_frame = tk.Frame(log_tools, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1, padx=4, pady=3)
        search_frame.pack(side=tk.LEFT, padx=(0, 10))

        tk.Label(search_frame, text="🔍", font=("Segoe UI", 8), bg=SURFACE_CARD, fg=TEXT_MUTED).pack(side=tk.LEFT, padx=(2, 2))

        self.ent_search = tk.Entry(
            search_frame,
            font=("Segoe UI", 9),
            bg=ENTRY_BG,
            fg=ENTRY_FG,
            relief="flat",
            width=28,
            insertbackground=ENTRY_FG
        )
        self.ent_search.pack(side=tk.LEFT, padx=(2, 4))
        self.ent_search.bind("<KeyRelease>", self._on_search_key)
        self.ent_search.bind("<Return>", lambda e: self._search_next())
        self.ent_search.bind("<Shift-Return>", lambda e: self._search_prev())
        self.ent_search.bind("<Escape>", lambda e: self._clear_search())

        self.lbl_search_count = tk.Label(
            search_frame,
            text="0/0",
            font=("Consolas", 8, "bold"),
            bg="#E7E0D2",
            fg=TEXT,
            padx=6,
            pady=1,
            width=5
        )
        self.lbl_search_count.pack(side=tk.LEFT, padx=(2, 3))

        self.btn_search_prev = tk.Button(
            search_frame,
            text="▲",
            font=("Segoe UI", 7, "bold"),
            bg=SURFACE,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            width=2,
            cursor="hand2",
            command=self._search_prev
        )
        self.btn_search_prev.pack(side=tk.LEFT, padx=1)

        self.btn_search_next = tk.Button(
            search_frame,
            text="▼",
            font=("Segoe UI", 7, "bold"),
            bg=SURFACE,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            width=2,
            cursor="hand2",
            command=self._search_next
        )
        self.btn_search_next.pack(side=tk.LEFT, padx=1)

        self.btn_search_clear = tk.Button(
            search_frame,
            text="✕",
            font=("Segoe UI", 7, "bold"),
            bg=SURFACE_CARD,
            fg=TEXT_MUTED,
            activebackground=BORDER,
            relief="flat",
            width=2,
            cursor="hand2",
            command=self._clear_search
        )
        self.btn_search_clear.pack(side=tk.LEFT, padx=(1, 2))

        # Auto-Scroll Capsule
        scroll_box = tk.Frame(log_tools, bg=SURFACE_CARD, highlightbackground=BORDER, highlightthickness=1, padx=6, pady=2)
        scroll_box.pack(side=tk.LEFT, padx=(0, 10))
        self.var_autoscroll = tk.BooleanVar(value=True)
        self.chk_autoscroll = tk.Checkbutton(
            scroll_box,
            text="Auto-Scroll",
            variable=self.var_autoscroll,
            font=("Segoe UI", 9, "bold"),
            bg=SURFACE_CARD,
            fg=TEXT,
            selectcolor=SURFACE,
            activebackground=SURFACE_CARD,
            activeforeground=TEXT,
            cursor="hand2",
            relief="flat",
            bd=0
        )
        self.chk_autoscroll.pack(side=tk.LEFT)

        # Clear Log button (active red theme, not disabled looking)
        btn_clear = tk.Button(
            log_tools,
            text="🗑 Clear Log",
            font=("Segoe UI", 8, "bold"),
            bg="#FFEBEE",
            fg="#C62828",
            activebackground="#C62828",
            activeforeground="#FFFFFF",
            highlightbackground="#FFCDD2",
            highlightthickness=1,
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._on_clear_log_clicked
        )
        btn_clear.pack(side=tk.LEFT)


        self.txt_log = scrolledtext.ScrolledText(
            log_frame,
            font=("Consolas", 10),
            bg=ENTRY_BG,
            fg="#000000",
            insertbackground="#000000",
            relief="flat",
            padx=12,
            pady=10
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # Log color tags (Black normal, Red for error, Green for slot found & booking trigger)
        self.txt_log.tag_config("MATCH", foreground=SUCCESS, font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("DISPATCH", foreground=SUCCESS, font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("POLL", foreground="#000000")
        self.txt_log.tag_config("ERROR", foreground=DANGER, font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("WARN", foreground="#000000")
        self.txt_log.tag_config("SWAP", foreground="#B8860B", font=("Consolas", 10, "bold"))

        # Task 4 & Task 5 Color Tags
        self.txt_log.tag_config("SESSION_BLUE", foreground="#0D47A1", font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("TRIGGER_ORANGE", foreground="#E65100", font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("BOOKING_FAILED_RED", foreground="#B71C1C", font=("Consolas", 10, "bold"))
        self.txt_log.tag_config("BOOKING_SUCCESS_GREEN", foreground="#1B5E20", font=("Consolas", 10, "bold"))

        # Search highlight tags
        self.txt_log.tag_config("SEARCH_ALL", background="#FFE082", foreground="#000000")
        self.txt_log.tag_config("SEARCH_CURRENT", background="#E65100", foreground="#FFFFFF")
        self.txt_log.tag_raise("SEARCH_ALL")
        self.txt_log.tag_raise("SEARCH_CURRENT")

    # ─── Server Sync & Health Logic ──────────────────────────────

    def _get_server_url(self):
        return self.ent_server_url.get().strip().rstrip("/")

    def _get_api_key(self):
        return self.ent_api_key.get().strip()

    def _toggle_api_key_visibility(self):
        if self.ent_api_key.cget("show") == "":
            self.ent_api_key.config(show="●")
            self.btn_toggle_key.config(text="👁")
        else:
            self.ent_api_key.config(show="")
            self.btn_toggle_key.config(text="🙈")

    def _open_web_dashboard(self):
        url = self._get_server_url()
        self._log(f"[BROWSER] Opening Web Hub in separate window: {url}", "DISPATCH")
        try:
            # Open in a dedicated separate Chrome or Edge window (not just a new tab)
            opened = False
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            ]
            for browser_exe in candidates:
                if os.path.exists(browser_exe):
                    subprocess.Popen([browser_exe, "--new-window", url])
                    opened = True
                    break
            if not opened:
                webbrowser.open_new(url)
        except Exception as e:
            self._log(f"[BROWSER] Failed to open separate window: {e}", "WARN")
            try:
                webbrowser.open_new(url)
            except Exception:
                pass

    def _start_local_server_if_needed(self):
        url = self.config_data.get("server_url", "http://localhost:5000")
        if "localhost" in url or "127.0.0.1" in url:
            port = 5000
            try:
                if ":" in url.split("//")[-1]:
                    port = int(url.split("//")[-1].split(":")[1].split("/")[0])
            except Exception:
                port = 5000

            if not is_port_in_use(port):
                server_script = PROJECT_ROOT / "server" / "app.py"
                if server_script.exists():
                    try:
                        env = os.environ.copy()
                        env["PORT"] = str(port)
                        env["PYTHONUNBUFFERED"] = "1"
                        self.local_server_proc = subprocess.Popen(
                            [sys.executable, str(server_script)],
                            cwd=str(PROJECT_ROOT / "server"),
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1
                        )
                        self._log(f"[LOCAL SERVER] Auto-started Local Server Hub on http://localhost:{port} in background.", "DISPATCH")
                        threading.Thread(target=self._stream_server_output, daemon=True).start()
                    except Exception as e:
                        self._log(f"[LOCAL SERVER] Failed to start local server: {e}", "WARN")
            else:
                self._log(f"[LOCAL SERVER] Local Server Hub is already active on port {port}.", "POLL")

    def _stream_server_output(self):
        if not self.local_server_proc or not self.local_server_proc.stdout:
            return
        try:
            for line in iter(self.local_server_proc.stdout.readline, ''):
                if line:
                    clean = line.strip()
                    if "Running on" in clean or "Restarting" in clean or "ERROR" in clean:
                        self.after(0, lambda c=clean: self._log(f"[SERVER LOG] {c}", "POLL"))
        except Exception:
            pass

    def _check_server_connection(self):
        def _task():
            url = self._get_server_url()
            key = self._get_api_key()
            try:
                r = requests.get(f"{url}/api/health", headers={"X-API-Key": key}, timeout=4)
                if r.status_code == 200:
                    self.server_online = True
                    self.after(0, lambda: self._update_server_badge(True))
                    self.after(0, self._sync_targets_from_server)
                else:
                    self.server_online = False
                    self.after(0, lambda: self._update_server_badge(False))
            except Exception:
                self.server_online = False
                self.after(0, lambda: self._update_server_badge(False))

        threading.Thread(target=_task, daemon=True).start()

    def _update_server_badge(self, online):
        if online:
            self.lbl_server_badge.config(text="● Server Online", fg=SUCCESS)
            self._log(f"[SERVER] Connected to GoDaddy Hub: {self._get_server_url()}", "DISPATCH")
        else:
            self.lbl_server_badge.config(text="● Server Offline", fg=DANGER)
            self._log(f"[SERVER] ⚠️ Cannot reach GoDaddy Hub at {self._get_server_url()}. Check URL and API Key.", "WARN")

    def _sync_targets_from_server(self):
        def _task():
            url = self._get_server_url()
            key = self._get_api_key()
            try:
                # 1. Fetch Targets
                r = requests.get(f"{url}/api/targets", headers={"X-API-Key": key}, timeout=5)
                if r.status_code == 200:
                    data = r.json().get("targets", {})
                    self.synced_targets = data
                    self.server_online = True
                else:
                    self.after(0, lambda: self._log(f"[SYNC] Server targets status {r.status_code}", "ERROR"))

                # 2. Fetch Accounts & Synchronize directly to root accounts.json
                synced_accs = []
                try:
                    r_acc = requests.get(f"{url}/api/accounts", headers={"X-API-Key": key}, timeout=5)
                    if r_acc.status_code == 200:
                        synced_accs = r_acc.json().get("accounts", [])
                        root_acc_file = PROJECT_ROOT / "accounts.json"
                        with open(root_acc_file, "w", encoding="utf-8") as f:
                            json.dump(synced_accs, f, indent=2)
                except Exception as ex_acc:
                    self.after(0, lambda: self._log(f"[SYNC] Accounts file sync warning: {ex_acc}", "WARN"))

                # 3. Clean stale queue entries
                try:
                    requests.post(f"{url}/api/queue/reset", headers={"X-API-Key": key}, timeout=3)
                except Exception:
                    pass

                self.after(0, lambda: self._apply_synced_targets(synced_accs))
            except Exception as e:
                self.after(0, lambda: self._log(f"[SYNC] Failed to fetch server data: {e}", "ERROR"))

        threading.Thread(target=_task, daemon=True).start()

    def _apply_synced_targets(self, accounts=None):
        ofc_cities = self.synced_targets.get("ofcCities", ALL_CITIES)
        consular_cities = self.synced_targets.get("consularCities", [])
        min_date = self.synced_targets.get("minDate", "Earliest")
        max_date = self.synced_targets.get("maxDate", "Latest")

        acc_list = accounts if accounts is not None else []
        if not acc_list:
            try:
                with open(PROJECT_ROOT / "accounts.json", "r", encoding="utf-8") as f:
                    acc_list = json.load(f)
            except Exception:
                pass

        acc_count = len(acc_list)
        enabled_accs = [a for a in acc_list if a.get("enabled", True) not in (False, 0, "0", "false", "no", "off")]
        active_count = len(enabled_accs)
        active_names = [a.get("customer_name") or a.get("username", "") for a in enabled_accs]
        short_names = ", ".join(active_names[:3]) + ("..." if len(active_names) > 3 else "")

        consular_str = f" | Consular: {', '.join(consular_cities)}" if consular_cities else ""
        self.lbl_targets_info.config(
            text=f"🎯 Target OFC: {', '.join(ofc_cities)}{consular_str} | 📅 Window: {min_date} ➔ {max_date}"
        )
        self.lbl_accounts_count.config(
            text=f"👥 {active_count}/{acc_count} Active Accounts (Status ON): {short_names}" if active_names else f"👥 {active_count}/{acc_count} Active Accounts (Status ON)"
        )
        self._log(f"[SYNC] ✅ Synced {acc_count} accounts ({active_count} Active/Status ON): {', '.join(active_names)}", "MATCH")
        self._log(f"[SYNC] Targets: OFC: {', '.join(ofc_cities)}, Consular: {', '.join(consular_cities) or 'All'}, Window: {min_date} to {max_date}", "POLL")

    def _refresh_fleet_status_badges(self):
        """Update live Fleet Radar and Hot-Swap role badges for all enabled accounts."""
        try:
            acc_file = PROJECT_ROOT / "accounts.json"
            pstate_file = PROJECT_ROOT / "src" / "polling_state.json"

            if not acc_file.exists():
                return

            with open(acc_file, "r", encoding="utf-8") as f:
                accounts = json.load(f)

            pstate = {}
            if pstate_file.exists():
                try:
                    with open(pstate_file, "r", encoding="utf-8") as f:
                        pstate = json.load(f)
                except Exception:
                    pass

            quota_cds = pstate.get("account_quota_cooldowns", {})
            active_poller = pstate.get("active_poller")
            now = time.time()

            if hasattr(self, "fleet_badges_frame") and self.fleet_badges_frame.winfo_exists():
                for child in self.fleet_badges_frame.winfo_children():
                    child.destroy()

                # Display ONLY the single Active Poller / Active Polling badge
                current_turn = pstate.get("current_turn") or pstate.get("last_poller")
                active_user = active_poller or current_turn

                # Map username to Customer Name
                acc_map = {
                    a.get("username", ""): (a.get("customer_name") or a.get("username", ""))
                    for a in accounts
                }

                if active_user:
                    cname = acc_map.get(active_user, active_user)
                    quota_until = float(quota_cds.get(active_user, 0))

                    if active_poller:
                        text = f"⚡ Active Poller: {cname} (Scanning)"
                        bg_color = "#BBDEFB"
                        fg_color = "#0D47A1"
                        border_color = "#42A5F5"
                    elif now < quota_until:
                        rem_m = max(1, int((quota_until - now) / 60))
                        text = f"⏳ Active Poller: {cname} (Resting {rem_m}m - Auto Switching)"
                        bg_color = "#FFF8E1"
                        fg_color = "#B8860B"
                        border_color = "#FFE082"
                    else:
                        text = f"⚡ Active Poller: {cname}"
                        bg_color = "#E3F2FD"
                        fg_color = "#1565C0"
                        border_color = "#90CAF9"
                else:
                    text = "⚡ Active Poller: Standby"
                    bg_color = "#F5F5F5"
                    fg_color = "#616161"
                    border_color = "#E0E0E0"

                badge = tk.Label(
                    self.fleet_badges_frame,
                    text=text,
                    font=("Segoe UI", 9, "bold"),
                    bg=bg_color,
                    fg=fg_color,
                    padx=10,
                    pady=3,
                    highlightbackground=border_color,
                    highlightthickness=1
                )
                badge.pack(side=tk.RIGHT, padx=3)

        except Exception:
            pass
        finally:
            self.after(3500, self._refresh_fleet_status_badges)

    # ─── Monitoring Process Control ───────────────────────────────

    def _start_monitoring(self):
        if self.is_monitoring:
            return

        # Save config
        self.config_data["server_url"] = self._get_server_url()
        self.config_data["api_key"] = self._get_api_key()
        try:
            if hasattr(self, "var_laptop_role"):
                self.config_data["laptop_role"] = self.var_laptop_role.get().strip()
        except Exception:
            pass
        save_local_config(self.config_data)

        # Reset / clean queue on server
        try:
            requests.post(f"{self._get_server_url()}/api/queue/reset", headers={"X-API-Key": self._get_api_key()}, timeout=3)
        except Exception:
            pass

        self.is_monitoring = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.lbl_countdown.config(text="Status: Monitoring Active", fg=SUCCESS)

        selected_role = self.var_laptop_role.get().strip() if hasattr(self, "var_laptop_role") else "ALL_IN_ONE"
        self._log("=============================================================", "DISPATCH")
        self._log(f"🚀 Starting Slot Monitor (Mode: {selected_role})...", "DISPATCH")
        self._log(f"   Remote Webhook URL: {self._get_server_url()}/api/trigger", "DISPATCH")
        self._log("=============================================================", "DISPATCH")

        # Launch orchestrator
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["LAPTOP_ROLE"] = selected_role
        env["SERVER_API_KEY"] = self._get_api_key()
        env["API_KEY"] = self._get_api_key()
        env["SERVER_URL"] = self._get_server_url()
        env["REMOTE_TRIGGER_URL"] = f"{self._get_server_url()}/api/trigger"
        env["FROM_GUI"] = "1"
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = str(PROJECT_ROOT)

        # Clear log files so bot starts fresh without disk/memory load
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        for lf_name in ["orchestrator.log", "browser.log", "polling.log"]:
            try:
                (log_dir / lf_name).write_text("", encoding="utf-8")
            except Exception:
                pass

        script = PROJECT_ROOT / "src" / "orchestrator.py"
        cmd = [sys.executable, str(script)]

        try:
            self.monitoring_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(PROJECT_ROOT),
                env=env
            )
            threading.Thread(target=self._read_monitor_output, daemon=True).start()
        except Exception as e:
            self._log(f"[ERROR] Failed to start monitor process: {e}", "ERROR")
            self._stop_monitoring()

    def _start_log_tailer(self):
        """Continuously tail orchestrator.log in background so Slot UI cards always stay live-updated."""
        log_file = PROJECT_ROOT / "logs" / "orchestrator.log"
        threading.Thread(target=self._tail_log_file, args=(log_file,), daemon=True).start()

    def _tail_log_file(self, log_path: Path):
        import time
        while not log_path.exists():
            time.sleep(1)

        # Pre-scan last ~50KB to populate the latest slot counts and timestamps immediately on start
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 50000), os.SEEK_SET)
                for line in f:
                    self.after(0, self._process_log_line, line.rstrip(), False)
        except Exception:
            pass

        # Follow file indefinitely for new entries
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        show_log = (self.monitoring_proc is None)
                        self.after(0, self._process_log_line, line.rstrip(), show_log)
                    else:
                        time.sleep(0.5)
        except Exception:
            pass

    def _read_monitor_output(self):
        try:
            for line in iter(self.monitoring_proc.stdout.readline, ''):
                if line:
                    text = line.rstrip()
                    self.after(0, self._process_log_line, text, True)
        except Exception:
            pass
        self.after(0, self._on_monitor_exit)

    def _process_log_line(self, line, show_in_log=True):
        import re
        # Strip ANSI color codes if present in raw log stream
        clean_text = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        if not clean_text:
            return

        # 1. Capture cycle polling timestamp e.g. [SLOT MONITOR] ... | 01:15:58 PM
        # or [13:14:53] / 22 Oct 26, 01:15:58 PM
        time_match = re.search(r"\|\s*(\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?)", clean_text, re.IGNORECASE)
        if time_match:
            self.last_poll_time = time_match.group(1).strip()
        elif "PM" in clean_text or "AM" in clean_text:
            time_match2 = re.search(r"(\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM))", clean_text, re.IGNORECASE)
            if time_match2:
                self.last_poll_time = time_match2.group(1).strip()

        check_time = getattr(self, "last_poll_time", "") or datetime.now().strftime("%I:%M:%S %p")

        # 2. Check for slot matches (SLOT_MATCHED / Qualifying OFC date found)
        if "SLOT_MATCHED" in clean_text or "Qualifying OFC date found" in clean_text or "OFC Appointment Available" in clean_text:
            if show_in_log:
                self._log(f"🚨 {clean_text}", "MATCH")
            for city in ALL_CITIES:
                if city in clean_text.upper():
                    self._update_city_card(city, "SLOT FOUND! 🟢", is_match=True, check_time=check_time)
            return

        # 3. Check for Per-City availability response lines:
        # e.g.:
        #   - CHENNAI     : 0 slots available
        #   - NEW DELHI   : 3 slots available
        avail_match = re.search(
            r"-\s*(CHENNAI|MUMBAI|HYDERABAD|KOLKATA|NEW DELHI|DELHI)\s*:\s*(\d+)\s*(?:slot|date)s?\s*(?:available|\(s\))?",
            clean_text, re.IGNORECASE
        )
        if avail_match:
            raw_city = avail_match.group(1).upper()
            city = "DELHI" if "DELHI" in raw_city else raw_city
            count = int(avail_match.group(2))
            if count > 0:
                slot_str = f"{count} slots" if count > 1 else f"{count} slot"
                self._update_city_card(city, slot_str, is_match=True, check_time=check_time)
            else:
                self._update_city_card(city, "0 slots", is_match=False, check_time=check_time)
            if show_in_log:
                self._log(clean_text)
            return

        # 3b. Check for city error lines e.g. - CHENNAI : ERROR (...)
        avail_err_match = re.search(
            r"-\s*(CHENNAI|MUMBAI|HYDERABAD|KOLKATA|NEW DELHI|DELHI)\s*:\s*ERROR",
            clean_text, re.IGNORECASE
        )
        if avail_err_match:
            raw_city = avail_err_match.group(1).upper()
            city = "DELHI" if "DELHI" in raw_city else raw_city
            self._update_city_card(city, "Error", is_match=False, check_time=check_time, is_error=True)
            if show_in_log:
                self._log(clean_text, "ERROR")
            return

        # 4. Check for Summary / Polling Result line:
        # e.g. Summary: CHENNAI=0d, HYDERABAD=0d, KOLKATA=0d, MUMBAI=0d, NEW DELHI=0d
        # or [MONITOR] Poll #3 'Agarwal' -> CHENNAI=0d, HYDERABAD=0d, KOLKATA=0d, MUMBAI=0d, NEW DELHI=0d
        if "SUMMARY:" in clean_text.upper() or "[MONITOR]" in clean_text.upper() or "POLLING-RESULT" in clean_text.upper():
            summary_matches = list(re.finditer(
                r"(CHENNAI|HYDERABAD|KOLKATA|MUMBAI|NEW DELHI|DELHI)\s*=\s*(\d+|ERR)d?",
                clean_text, re.IGNORECASE
            ))
            if summary_matches:
                for m in summary_matches:
                    raw_city = m.group(1).upper()
                    city = "DELHI" if "DELHI" in raw_city else raw_city
                    val = m.group(2).upper()
                    if val == "ERR":
                        self._update_city_card(city, "Error", is_match=False, check_time=check_time, is_error=True)
                    else:
                        count = int(val)
                        if count > 0:
                            slot_str = f"{count} slots" if count > 1 else f"{count} slot"
                            self._update_city_card(city, slot_str, is_match=True, check_time=check_time)
                        else:
                            self._update_city_card(city, "0 slots", is_match=False, check_time=check_time)
            if show_in_log:
                self._log(clean_text, "POLL")
            return

        if not show_in_log:
            return

        # 5. Logging triggers & formatting
        clean_upper = clean_text.upper()

        # Task 4: Session Radar -> Dark Blue
        if "[SESSION RADAR]" in clean_upper or "SESSION RADAR" in clean_upper:
            self._log(clean_text, "SESSION_BLUE")
            return

        # Task 5: OFC & Consular Booking Success -> Dark Green
        if any(k in clean_upper for k in [
            "[BOOKING SUCCESS]", "[OFC BOOKING SUCCESS]", "[CONSULAR BOOKING SUCCESS]",
            "OFC APPOINTMENT SECURED", "OFC BOOKED SUCCESSFULLY",
            "CONSULAR APPOINTMENT RESCHEDULED", "COMPLETE BOOKING!"
        ]):
            self._log(clean_text, "BOOKING_SUCCESS_GREEN")
            return

        # Task 5: OFC & Consular Booking Failed -> Dark Red
        if any(k in clean_upper for k in [
            "[OFC BOOKING FAILED", "[CONSULAR BOOKING FAILED",
            "OFC BOOKING REJECTED", "CONSULAR BOOKING REJECTED",
            "BOOKING REJECTED BY SERVER", "EXHAUSTED ALL SLOTS",
            "EXHAUSTED ALL DATES", "SERVER REJECTED TOKEN"
        ]):
            self._log(clean_text, "BOOKING_FAILED_RED")
            return

        # Task 5: OFC & Consular Trigger / Dispatch -> Deep Orange
        if any(k in clean_upper for k in [
            "[CROSS-ACCOUNT TRIGGER]", "[SCOUT DISPATCH]", "[OFC BOOKING HIT]",
            "[OFC TRIGGER]", "[CONSULAR TRIGGER]", "FIRING OFC BOOKING",
            "FIRING CONSULAR BOOKING", "EXECUTING LIVE APPOINTMENT",
            "ENQUEUED", "REMOTE TRIGGER SENT", "DISPATCHED TO TARGET"
        ]):
            self._log(clean_text, "TRIGGER_ORANGE")
            return

        if "HOT-SWAP" in clean_upper or "ROLE-SWAP" in clean_upper or "ACCOUNT-REST" in clean_upper or "ACCOUNT QUOTA" in clean_upper:
            self._log(f"🔄 {clean_text}", "SWAP")
            self._refresh_fleet_status_badges()
        elif "REMOTE TRIGGER SENT" in clean_upper or "DISPATCHED" in clean_upper or "TRIGGER SENT" in clean_upper or "TRIGGERING REMOTE" in clean_upper:
            self._log(f"⚡ {clean_text}", "DISPATCH")
        elif any(k in clean_upper for k in ["ERROR", "EXCEPTION", "RATE LIMIT", "HTTP 429", "BAN-TYPE", "FAILED", "CRASH", "[ALERT]"]):
            self._log(clean_text, "ERROR")
        else:
            self._log(clean_text)

    def _update_city_card(self, city, date_text, is_match=False, check_time=None, is_error=False):
        if city in self.city_cards:
            card = self.city_cards[city]
            if is_match:
                card["status_lbl"].config(text="● AVAILABLE", fg=SUCCESS)
                card["date_lbl"].config(text=date_text, fg=SUCCESS)
                card["card"].config(highlightbackground=SUCCESS, highlightthickness=2)
            elif is_error:
                card["status_lbl"].config(text="● ERROR", fg=WARNING)
                card["date_lbl"].config(text=date_text, fg=WARNING)
                card["card"].config(highlightbackground=BORDER, highlightthickness=1)
            else:
                card["status_lbl"].config(text="● CHECKED", fg=INFO)
                card["date_lbl"].config(text=date_text, fg=TEXT_MUTED)
                card["card"].config(highlightbackground=BORDER, highlightthickness=1)

            if check_time:
                card["time_lbl"].config(text=f"Last check: {check_time}")

    def _manual_check_slot(self):
        """Simulate or manually send a slot test trigger to the server."""
        if not self.server_online:
            messagebox.showwarning("Server Offline", "Cannot send trigger: GoDaddy Server Hub is currently offline.")
            return

        def _task():
            url = f"{self._get_server_url()}/api/trigger"
            key = self._get_api_key()
            payload = {
                "city": "CHENNAI",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "time": "09:00",
                "slot_type": "OFC",
                "source": "manual_test"
            }
            try:
                r = requests.post(url, headers={"X-API-Key": key}, json=payload, timeout=5)
                res = r.json()
                self.after(0, lambda: self._log(f"[TEST TRIGGER] Server response: {res.get('message')}", "DISPATCH"))
                self.after(0, lambda: messagebox.showinfo("Trigger Sent", f"Slot dispatched to GoDaddy Server!\n\nMatched Accounts: {res.get('data', {}).get('matched_count', 0)}"))
            except Exception as e:
                self.after(0, lambda: self._log(f"[TEST TRIGGER] Error: {e}", "ERROR"))

        threading.Thread(target=_task, daemon=True).start()

    def _on_stop_clicked(self):
        """Prompt confirmation before stopping monitoring."""
        if not self.is_monitoring:
            return
        confirm = messagebox.askyesno(
            "Stop Monitoring",
            "Do you want to stop monitoring?",
            parent=self
        )
        if confirm:
            self._stop_monitoring()

    def _stop_monitoring(self):

        if self.monitoring_proc:
            self._log("[MONITOR] Stopping monitor processes...", "WARN")
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.monitoring_proc.pid)], capture_output=True)
            except Exception:
                pass
            self.monitoring_proc = None

        self._on_monitor_exit()

    def _on_monitor_exit(self):
        self.is_monitoring = False
        self.monitoring_proc = None
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.lbl_countdown.config(text="Status: Stopped", fg=TEXT_MUTED)
        self._log("[MONITOR] Slot Monitor stopped.", "WARN")

    # ─── Console Search & Log Navigation ─────────────────────────

    def _on_search_key(self, event=None):
        if event and event.keysym in ("Return", "Up", "Down", "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        query = self.ent_search.get().strip()
        self._highlight_search(query)

    def _highlight_search(self, query):
        self.txt_log.tag_remove("SEARCH_ALL", "1.0", tk.END)
        self.txt_log.tag_remove("SEARCH_CURRENT", "1.0", tk.END)
        self.search_matches = []
        self.search_current_idx = -1

        if not query:
            self.lbl_search_count.config(text="0/0")
            return

        idx = "1.0"
        query_len = len(query)
        while True:
            idx = self.txt_log.search(query, idx, stopindex=tk.END, nocase=True)
            if not idx:
                break
            end_idx = f"{idx}+{query_len}c"
            self.search_matches.append((idx, end_idx))
            self.txt_log.tag_add("SEARCH_ALL", idx, end_idx)
            idx = end_idx

        total = len(self.search_matches)
        if total > 0:
            self.search_current_idx = 0
            self._update_search_selection(scroll=True)
        else:
            self.lbl_search_count.config(text="0/0")

    def _update_search_selection(self, scroll=True):
        self.txt_log.tag_remove("SEARCH_CURRENT", "1.0", tk.END)
        total = len(self.search_matches)
        if total == 0 or self.search_current_idx < 0:
            self.lbl_search_count.config(text="0/0")
            return

        self.search_current_idx = self.search_current_idx % total
        start, end = self.search_matches[self.search_current_idx]
        self.txt_log.tag_add("SEARCH_CURRENT", start, end)
        self.lbl_search_count.config(text=f"{self.search_current_idx + 1}/{total}")
        if scroll:
            self.txt_log.see(start)

    def _search_next(self):
        if not self.search_matches:
            query = self.ent_search.get().strip()
            if query:
                self._highlight_search(query)
            return
        self.search_current_idx = (self.search_current_idx + 1) % len(self.search_matches)
        self._update_search_selection(scroll=True)

    def _search_prev(self):
        if not self.search_matches:
            query = self.ent_search.get().strip()
            if query:
                self._highlight_search(query)
            return
        self.search_current_idx = (self.search_current_idx - 1) % len(self.search_matches)
        self._update_search_selection(scroll=True)

    def _clear_search(self):
        self.ent_search.delete(0, tk.END)
        self.txt_log.tag_remove("SEARCH_ALL", "1.0", tk.END)
        self.txt_log.tag_remove("SEARCH_CURRENT", "1.0", tk.END)
        self.search_matches = []
        self.search_current_idx = -1
        self.lbl_search_count.config(text="0/0")

    # ─── Console Logging Helpers ─────────────────────────────────

    def _log(self, text, tag=None):
        timestamp = datetime.now().strftime("[%H:%M:%S] ")
        self.txt_log.config(state=tk.NORMAL)
        if tag:
            self.txt_log.insert(tk.END, timestamp + text + "\n", tag)
        else:
            self.txt_log.insert(tk.END, timestamp + text + "\n")

        # If user searched for something and new line has matches, re-highlight
        query = self.ent_search.get().strip() if hasattr(self, "ent_search") else ""
        if query and query.lower() in (timestamp + text).lower():
            self._highlight_search(query)

        # Auto-scroll if enabled
        if not hasattr(self, "var_autoscroll") or self.var_autoscroll.get():
            self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def _on_clear_log_clicked(self):
        """Prompt confirmation before clearing all logs."""
        confirm = messagebox.askyesno(
            "Clear Logs",
            "Do you want to clear all logs?",
            parent=self
        )
        if confirm:
            self._clear_log()

    def _clear_log(self):

        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete(1.0, tk.END)
        self.txt_log.config(state=tk.DISABLED)
        self._clear_search()

    def _on_closing(self):
        if self.monitoring_proc:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.monitoring_proc.pid)], capture_output=True)
            except Exception:
                pass
        if self.local_server_proc:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.local_server_proc.pid)], capture_output=True)
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = SlotMonitorApp()
    app.mainloop()
