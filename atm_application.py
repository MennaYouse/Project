"""
NOVA ATM | Digital Banking  (local demo application)

Improvements over the first version:
  * Dark / Light theme (saved in the database, shortcut: Ctrl+T)
  * One shared SQLite connection (WAL mode) instead of opening a new one per action
  * Money stored as integer piastres + Decimal parsing  -> no floating point errors
  * Atomic withdrawals (no race conditions, balance can never go negative)
  * Sidebar is built once; only the page body is swapped when navigating
  * Non-blocking toast notifications instead of popup message boxes
  * Brute-force protection on login (temporary lockout) + no username enumeration timing
  * Overview statistics, quick-amount buttons, filterable & scrollable transactions
  * Automatic migration of the old database (float balances -> integer piastres)
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import tkinter as tk
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from tkinter import font as tkfont
from tkinter import messagebox, ttk

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_NAME = "ATM_System.db"
CURRENCY = "EGP"
PBKDF2_ITERATIONS = 200_000
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 30
MAX_AMOUNT = Decimal("1000000")          # max per single transaction
TRANSACTIONS_LIMIT = 500                 # rows loaded in the Transactions page
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.]{3,30}$")
FONT_CANDIDATES = ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "DejaVu Sans", "Arial")

# A fake hash used to keep login timing the same for unknown usernames.
DUMMY_HASH = "00" * 16 + ":" + "00" * 32

THEMES = {
    "dark": {
        "BG": "#0B1220", "PANEL": "#121C2E", "PANEL_LIGHT": "#1B2940",
        "TEXT": "#F4F7FB", "MUTED": "#9AAAC0", "BORDER": "#2A3A52",
        "ACCENT": "#38D6C4", "ACCENT_HOVER": "#70E8DA", "ACCENT_DARK": "#183F45",
        "ON_ACCENT": "#06101C", "HOVER": "#263852",
        "RED": "#FF7272", "GREEN": "#54D6A0",
        "SHADOW": "#000000", "SHADOW_A": 0.6,
    },
    "light": {
        "BG": "#F1F5F9", "PANEL": "#FFFFFF", "PANEL_LIGHT": "#E8EEF5",
        "TEXT": "#0F172A", "MUTED": "#64748B", "BORDER": "#CBD5E1",
        "ACCENT": "#0E9F8E", "ACCENT_HOVER": "#0B8577", "ACCENT_DARK": "#D5F1EC",
        "ON_ACCENT": "#FFFFFF", "HOVER": "#D8E1EC",
        "RED": "#DC2626", "GREEN": "#059669",
        "SHADOW": "#0F172A", "SHADOW_A": 0.18,
    },
}


# ---------------------------------------------------------------------------
# Helpers: passwords, money, dates
# ---------------------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored_hash):
    try:
        salt_hex, hash_hex = stored_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return hmac.compare_digest(candidate.hex(), hash_hex)
    except (ValueError, TypeError, AttributeError):
        return False


def parse_amount(raw):
    """Return a valid Decimal amount or raise ValueError with a friendly message."""
    text = raw.replace(",", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("Enter a valid number, for example 250 or 250.50.") from None
    if not value.is_finite():
        raise ValueError("Enter a valid number, for example 250 or 250.50.")
    if value <= 0:
        raise ValueError("Amount must be greater than zero.")
    if value.as_tuple().exponent < -2:
        raise ValueError("Use no more than two decimal places.")
    if value > MAX_AMOUNT:
        raise ValueError(f"The maximum per transaction is {CURRENCY} {MAX_AMOUNT:,.2f}.")
    return value


def to_cents(amount):
    return int((amount * 100).to_integral_value())


def fmt(cents):
    return f"{Decimal(int(cents)) / 100:,.2f}"


def format_date(value):
    """Database stores UTC; show it in the computer's local time."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%d %b %Y  %I:%M %p")
    except (ValueError, TypeError):
        return str(value)


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

class InsufficientFunds(Exception):
    pass


class AccountNotFound(Exception):
    pass


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.isolation_level = None          # we control transactions manually
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._setup()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def _setup(self):
        c = self.conn
        existed = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Accounts'"
        ).fetchone() is not None

        c.execute("""
            CREATE TABLE IF NOT EXISTS Accounts (
                AccountID INTEGER PRIMARY KEY AUTOINCREMENT,
                FullName TEXT NOT NULL,
                Username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                PasswordHash TEXT NOT NULL,
                Balance INTEGER NOT NULL DEFAULT 0 CHECK (Balance >= 0),
                CreatedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS Transactions (
                TransactionID INTEGER PRIMARY KEY AUTOINCREMENT,
                AccountID INTEGER NOT NULL,
                TransactionType TEXT NOT NULL
                    CHECK (TransactionType IN ('Deposit', 'Withdraw')),
                Amount INTEGER NOT NULL CHECK (Amount > 0),
                TransactionDate TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                BalanceAfter INTEGER NOT NULL,
                FOREIGN KEY (AccountID) REFERENCES Accounts(AccountID)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_account
            ON Transactions (AccountID, TransactionID DESC)
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS Settings (
                Key TEXT PRIMARY KEY,
                Value TEXT NOT NULL
            )
        """)

        # Migration: version 0 stored money as float EGP, version 1 stores integer piastres.
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            if existed:
                with self.transaction() as tx:
                    tx.execute("UPDATE Accounts SET Balance = CAST(ROUND(Balance * 100) AS INTEGER)")
                    tx.execute("""
                        UPDATE Transactions
                        SET Amount = CAST(ROUND(Amount * 100) AS INTEGER),
                            BalanceAfter = CAST(ROUND(BalanceAfter * 100) AS INTEGER)
                    """)
            c.execute("PRAGMA user_version = 1")

    # ----- settings -----

    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT Value FROM Settings WHERE Key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO Settings (Key, Value) VALUES (?, ?)", (key, value))

    # ----- accounts -----

    def create_account(self, full_name, username, password_hash):
        with self.transaction() as c:
            taken = c.execute(
                "SELECT 1 FROM Accounts WHERE Username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if taken:
                raise sqlite3.IntegrityError("username already exists")
            c.execute(
                "INSERT INTO Accounts (FullName, Username, PasswordHash, Balance) VALUES (?, ?, ?, 0)",
                (full_name, username, password_hash),
            )

    def find_for_login(self, username):
        return self.conn.execute(
            "SELECT AccountID, FullName, Username, PasswordHash FROM Accounts "
            "WHERE Username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()

    def get_account(self, account_id):
        return self.conn.execute(
            "SELECT AccountID, FullName, Username, Balance FROM Accounts WHERE AccountID = ?",
            (account_id,),
        ).fetchone()

    # ----- money -----

    def apply_transaction(self, account_id, kind, cents):
        """Atomically change the balance and log the transaction. Returns the new balance."""
        with self.transaction() as c:
            if kind == "Deposit":
                cur = c.execute(
                    "UPDATE Accounts SET Balance = Balance + ? WHERE AccountID = ?",
                    (cents, account_id),
                )
            else:
                cur = c.execute(
                    "UPDATE Accounts SET Balance = Balance - ? WHERE AccountID = ? AND Balance >= ?",
                    (cents, account_id, cents),
                )
            if cur.rowcount == 0:
                exists = c.execute(
                    "SELECT 1 FROM Accounts WHERE AccountID = ?", (account_id,)
                ).fetchone()
                raise InsufficientFunds() if exists else AccountNotFound()
            balance = c.execute(
                "SELECT Balance FROM Accounts WHERE AccountID = ?", (account_id,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO Transactions (AccountID, TransactionType, Amount, BalanceAfter) "
                "VALUES (?, ?, ?, ?)",
                (account_id, kind, cents, balance),
            )
        return balance

    def fetch_transactions(self, account_id, kind=None, limit=None):
        query = (
            "SELECT TransactionType, Amount, TransactionDate, BalanceAfter "
            "FROM Transactions WHERE AccountID = ?"
        )
        params = [account_id]
        if kind:
            query += " AND TransactionType = ?"
            params.append(kind)
        query += " ORDER BY TransactionID DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(query, params).fetchall()

    def get_stats(self, account_id):
        """Return (total deposited, total withdrawn, number of transactions) in one query."""
        return self.conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN TransactionType = 'Deposit'  THEN Amount END), 0),
                COALESCE(SUM(CASE WHEN TransactionType = 'Withdraw' THEN Amount END), 0),
                COUNT(*)
            FROM Transactions WHERE AccountID = ?
        """, (account_id,)).fetchone()


# ---------------------------------------------------------------------------
# Custom widgets: rounded buttons with a soft shadow, rounded entry with eye icon
# ---------------------------------------------------------------------------

def _rgb(widget, color):
    r, g, b = widget.winfo_rgb(color)
    return r // 257, g // 257, b // 257


def blend(widget, base, over, alpha):
    """Return `over` painted on top of `base` with the given opacity (fake transparency)."""
    br, bg, bb = _rgb(widget, base)
    orr, og, ob = _rgb(widget, over)
    return "#%02x%02x%02x" % (round(br + (orr - br) * alpha),
                              round(bg + (og - bg) * alpha),
                              round(bb + (ob - bb) * alpha))


def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
              x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
              x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kwargs)


_FONT_CACHE = {}


def measure_font(font):
    key = tuple(font)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = tkfont.Font(font=font)
    return _FONT_CACHE[key]


class RoundedButton(tk.Canvas):
    """Canvas-based button with rounded corners and an optional soft shadow."""

    MARGIN = 3        # horizontal room for the shadow
    DEPTH = 4         # vertical room for the shadow

    def __init__(self, parent, text, command, *, bg, hover, fg, font,
                 shadow_base="#000000", shadow_alpha=0.0, radius=12,
                 padx=18, pady=11, anchor="center"):
        parent_bg = parent.cget("bg")
        super().__init__(parent, bg=parent_bg, highlightthickness=0, bd=0, cursor="hand2")
        self._text, self._command = text, command
        self._bg, self._hover, self._fg, self._font = bg, hover, fg, font
        self._radius, self._padx, self._anchor = radius, padx, anchor
        self._has_shadow = shadow_alpha > 0
        self._m = self.MARGIN if self._has_shadow else 0
        self._d = self.DEPTH if self._has_shadow else 0
        if self._has_shadow:
            self._shadow_near = blend(self, parent_bg, shadow_base, shadow_alpha)
            self._shadow_far = blend(self, parent_bg, shadow_base, shadow_alpha / 2)
        self._hovering = False
        self._pressed = False

        f = measure_font(font)
        width = f.measure(text) + 2 * padx + 2 * self._m
        height = f.metrics("linespace") + 2 * pady + self._d + (1 if self._has_shadow else 0)
        self.configure(width=width, height=height)

        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_style(self, **kwargs):
        for key in ("bg", "hover", "fg", "font", "text"):
            if key in kwargs:
                setattr(self, "_" + key, kwargs[key])
        self._draw()

    def _draw(self):
        if not self.winfo_exists():
            return
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8:
            return
        self.delete("all")
        m, d = self._m, self._d
        top = 1 if self._has_shadow else 0
        bottom = h - d
        r = max(min(self._radius, (bottom - top) // 2), 2)
        if self._has_shadow:
            round_rect(self, m - 1, top + 2, w - m + 1, bottom + 3, r + 1,
                       fill=self._shadow_far, outline="")
            round_rect(self, m, top + 1, w - m, bottom + 2, r,
                       fill=self._shadow_near, outline="")
        color = self._hover if (self._hovering or self._pressed) else self._bg
        round_rect(self, m, top, w - m, bottom, r, fill=color, outline="")
        y = (top + bottom) / 2 + (1 if self._pressed else 0)
        if self._anchor == "w":
            self.create_text(m + self._padx, y, text=self._text, fill=self._fg,
                             font=self._font, anchor="w")
        else:
            self.create_text(w / 2, y, text=self._text, fill=self._fg,
                             font=self._font, anchor="center")

    def _on_enter(self, _event):
        self._hovering = True
        self._draw()

    def _on_leave(self, _event):
        self._hovering = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event):
        self._pressed = True
        self._draw()

    def _on_release(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self._draw()
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if was_pressed and inside and self._command:
            self._command()


class RoundedEntry(tk.Canvas):
    """Rounded text field. With password=True an eye icon inside the field shows/hides the text."""

    ICON_ZONE = 52

    def __init__(self, parent, *, field_bg, border, focus_border, fg, muted, accent,
                 font, show=None, radius=12, height=46, password=False):
        parent_bg = parent.cget("bg")
        super().__init__(parent, width=200, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._field_bg, self._border, self._focus_border = field_bg, border, focus_border
        self._fg, self._muted, self._accent = fg, muted, accent
        self._radius, self._password = radius, password
        self._hidden = password
        self._focused = False
        self._eye_hover = False

        self.entry = tk.Entry(self, bg=field_bg, fg=fg, insertbackground=fg, relief="flat",
                              bd=0, highlightthickness=0, font=font,
                              show="•" if password else (show or ""))
        self._win = self.create_window(16, height / 2, window=self.entry, anchor="w")

        tk.Canvas.bind(self, "<Configure>", lambda _e: self._redraw())
        tk.Canvas.bind(self, "<Button-1>", self._on_click)
        tk.Canvas.bind(self, "<Motion>", self._on_motion)
        tk.Canvas.bind(self, "<Leave>", self._on_leave)
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))

    # ----- small Entry-like API -----

    def get(self):
        return self.entry.get()

    def set(self, value):
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def focus_set(self):
        self.entry.focus_set()

    def bind(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    # ----- drawing -----

    def _redraw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8:
            return
        self.delete("deco")
        border = self._focus_border if self._focused else self._border
        round_rect(self, 2, 2, w - 2, h - 2, self._radius, fill=self._field_bg,
                   outline=border, width=2 if self._focused else 1, tags="deco")
        right_pad = 16
        if self._password:
            self._draw_eye(w, h)
            right_pad = self.ICON_ZONE
        self.coords(self._win, 18, h / 2)
        self.itemconfigure(self._win, width=max(w - 18 - right_pad, 20), height=h - 20)
        self.tag_lower("deco")

    def _draw_eye(self, w, h):
        cx, cy = w - 28, h / 2
        if not self._hidden:
            color = self._accent
        else:
            color = self._fg if self._eye_hover else self._muted
        self.create_polygon([cx - 11, cy, cx - 5, cy - 6, cx + 5, cy - 6,
                             cx + 11, cy, cx + 5, cy + 6, cx - 5, cy + 6],
                            smooth=True, fill="", outline=color, width=2, tags="deco")
        self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=color, outline=color, tags="deco")
        if not self._hidden:   # slashed eye = "click to hide"
            self.create_line(cx - 10, cy + 9, cx + 10, cy - 9, fill=color, width=2,
                             capstyle="round", tags="deco")

    # ----- interaction -----

    def _over_eye(self, x):
        return self._password and x >= self.winfo_width() - self.ICON_ZONE

    def _set_focus(self, focused):
        self._focused = focused
        self._redraw()

    def _on_click(self, event):
        if self._over_eye(event.x):
            self._hidden = not self._hidden
            self.entry.config(show="•" if self._hidden else "")
            self.entry.focus_set()
            self.entry.icursor("end")
            self._redraw()
        else:
            self.entry.focus_set()

    def _on_motion(self, event):
        over = self._over_eye(event.x)
        self.configure(cursor="hand2" if over else "")
        if over != self._eye_hover:
            self._eye_hover = over
            self._redraw()

    def _on_leave(self, _event):
        self.configure(cursor="")
        if self._eye_hover:
            self._eye_hover = False
            self._redraw()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class ATMApp(tk.Tk):
    NAV_ITEMS = (("▦", "Overview"), ("＋", "Deposit"), ("−", "Withdraw"), ("≡", "Transactions"))
    QUICK_AMOUNTS = (100, 500, 1000, 5000)
    FILTERS = (("All", None), ("Deposits", "Deposit"), ("Withdrawals", "Withdraw"))

    def __init__(self):
        super().__init__()
        self.db = Database(DB_NAME)

        saved_theme = self.db.get_setting("theme", "dark")
        self.theme_name = saved_theme if saved_theme in THEMES else "dark"
        self.t = THEMES[self.theme_name]

        families = set(tkfont.families(self))
        self.ff = next((f for f in FONT_CANDIDATES if f in families), "Helvetica")

        self.title("NOVA ATM | Digital Banking")
        self.minsize(960, 660)
        self.center_window(1100, 740)

        self.account_id = None
        self.view = "auth"              # auth | register | app
        self.current_page = "Overview"
        self.tx_filter = None
        self.fields = {}                # name -> Entry widget of the current page
        self.failed_logins = {}         # username -> (failures, locked_until)
        self.nav_buttons = {}
        self.filter_buttons = {}
        self._toast = None
        self._toast_job = None

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.apply_theme()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-t>", lambda _e: self.toggle_theme())
        self.render()

    # ----- window / theme -----

    def center_window(self, width, height):
        x = max((self.winfo_screenwidth() - width) // 2, 0)
        y = max((self.winfo_screenheight() - height) // 2 - 20, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def font(self, size=11, bold=False):
        return (self.ff, size, "bold" if bold else "normal")

    def apply_theme(self):
        t, s = self.t, self.style
        self.configure(bg=t["BG"])
        s.configure("Treeview", background=t["PANEL"], foreground=t["TEXT"],
                    fieldbackground=t["PANEL"], rowheight=36, borderwidth=0,
                    font=self.font(10))
        s.configure("Treeview.Heading", background=t["PANEL_LIGHT"], foreground=t["MUTED"],
                    relief="flat", borderwidth=0, font=self.font(9, True), padding=(8, 8))
        s.map("Treeview", background=[("selected", t["ACCENT_DARK"])],
              foreground=[("selected", t["TEXT"])])
        s.map("Treeview.Heading", background=[("active", t["HOVER"])])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Vertical.TScrollbar", background=t["PANEL_LIGHT"], troughcolor=t["PANEL"],
                    bordercolor=t["PANEL"], lightcolor=t["PANEL_LIGHT"], darkcolor=t["PANEL_LIGHT"],
                    arrowcolor=t["MUTED"], relief="flat")
        s.map("Vertical.TScrollbar", background=[("active", t["HOVER"])])

    def toggle_theme(self):
        # Keep whatever the user already typed (username, amount, ...)
        saved = {k: e.get() for k, e in self.fields.items() if e.winfo_exists()}
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.t = THEMES[self.theme_name]
        self.db.set_setting("theme", self.theme_name)
        self.apply_theme()
        self.render()
        for key, value in saved.items():
            entry = self.fields.get(key)
            if entry is not None and value:
                entry.set(value)

    def theme_button_text(self):
        return "☀  Light mode" if self.theme_name == "dark" else "☾  Dark mode"

    def render(self):
        if self.view == "app" and self.account_id is not None:
            if self.build_shell():
                self.show_page(self.current_page)
        elif self.view == "register":
            self.show_register()
        else:
            self.show_auth()

    def clear_window(self):
        if self._toast_job is not None:
            self.after_cancel(self._toast_job)
            self._toast_job = None
        self._toast = None
        for child in self.winfo_children():
            child.destroy()
        self.fields = {}
        self.nav_buttons = {}

    # ----- widget factories -----

    def label(self, parent, text, size=11, color="TEXT", bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=self.t[color],
                        font=self.font(size, bold), **kwargs)

    def button(self, parent, text, command, primary=False, shadow=True, **options):
        t = self.t
        options.setdefault("bg", t["ACCENT"] if primary else t["PANEL_LIGHT"])
        options.setdefault("hover", t["ACCENT_HOVER"] if primary else t["HOVER"])
        options.setdefault("fg", t["ON_ACCENT"] if primary else t["TEXT"])
        options.setdefault("font", self.font(10, True))
        return RoundedButton(parent, text, command, shadow_base=t["SHADOW"],
                             shadow_alpha=t["SHADOW_A"] if shadow else 0.0, **options)

    def entry(self, parent, key=None, show=None, password=False):
        t = self.t
        widget = RoundedEntry(parent, field_bg=t["PANEL_LIGHT"], border=t["BORDER"],
                              focus_border=t["ACCENT"], fg=t["TEXT"], muted=t["MUTED"],
                              accent=t["ACCENT"], font=self.font(11), show=show,
                              password=password)
        if key:
            self.fields[key] = widget
        return widget

    def password_field(self, parent, key):
        """Password entry with the show/hide eye icon inside the field itself."""
        widget = self.entry(parent, key=key, password=True)
        widget.pack(fill="x")
        return widget

    # ----- toast notifications -----

    def toast(self, message, kind="info", duration=3200):
        colors = {"success": "GREEN", "error": "RED", "info": "ACCENT"}
        color = self.t[colors.get(kind, "ACCENT")]
        self._dismiss_toast()
        frame = tk.Frame(self, bg=self.t["PANEL"], highlightthickness=2, highlightbackground=color)
        tk.Label(frame, text=message, bg=self.t["PANEL"], fg=self.t["TEXT"],
                 font=self.font(10, True), padx=18, pady=12, wraplength=360,
                 justify="left").pack()
        frame.place(relx=1.0, rely=1.0, anchor="se", x=-24, y=-24)
        frame.lift()
        self._toast = frame
        self._toast_job = self.after(duration, self._dismiss_toast)

    def _dismiss_toast(self):
        if self._toast_job is not None:
            self.after_cancel(self._toast_job)
            self._toast_job = None
        if self._toast is not None:
            if self._toast.winfo_exists():
                self._toast.destroy()
            self._toast = None

    # ----- auth pages -----

    def show_auth(self):
        self.view = "auth"
        self.clear_window()
        t = self.t
        outer = tk.Frame(self, bg=t["BG"])
        outer.pack(fill="both", expand=True, padx=34, pady=28)

        left = tk.Frame(outer, bg=t["PANEL"], padx=34, pady=34,
                        highlightthickness=1, highlightbackground=t["BORDER"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 18))
        self.label(left, "NOVA", 32, "ACCENT", True).pack(anchor="w", pady=(20, 6))
        self.label(left, "YOUR MONEY. YOUR CONTROL.", 15, "TEXT", True).pack(anchor="w")
        tk.Frame(left, bg=t["ACCENT"], height=3, width=74).pack(anchor="w", pady=22)
        self.label(left, "A simpler way to manage your money.\n"
                         "Secure access, clear balances,\n"
                         "and a complete transaction history.",
                   13, "MUTED", justify="left").pack(anchor="w", pady=(0, 30))
        for text in ("Secure account access", "Fast deposits and withdrawals",
                     "Transaction history in one place"):
            row = tk.Frame(left, bg=t["PANEL"])
            row.pack(anchor="w", pady=7)
            self.label(row, "✓", 11, "ACCENT", True).pack(side="left", padx=(0, 10))
            self.label(row, text, 11).pack(side="left")
        self.label(left, f"LOCAL DEMO APPLICATION  •  {CURRENCY}", 9, "MUTED").pack(side="bottom", anchor="w")

        right = tk.Frame(outer, bg=t["BG"], padx=28, pady=10)
        right.pack(side="right", fill="both", expand=True)
        top = tk.Frame(right, bg=t["BG"])
        top.pack(fill="x")
        self.button(top, self.theme_button_text(), self.toggle_theme).pack(side="right")

        self.label(right, "Welcome back", 25, bold=True).pack(anchor="w", pady=(34, 6))
        self.label(right, "Log in to your account or create a new one.", 11, "MUTED").pack(anchor="w", pady=(0, 24))

        self.label(right, "Username", 10, "MUTED", True).pack(anchor="w", pady=(8, 6))
        username = self.entry(right, key="login_username")
        username.pack(fill="x")

        self.label(right, "Password", 10, "MUTED", True).pack(anchor="w", pady=(17, 6))
        password = self.password_field(right, "login_password")

        self.button(right, "Sign in  →", self.do_login, primary=True).pack(fill="x", pady=(24, 10))
        self.button(right, "Create an account", self.show_register).pack(fill="x")

        username.bind("<Return>", lambda _e: password.focus_set())
        password.bind("<Return>", lambda _e: self.do_login())
        username.focus_set()

    def show_register(self):
        self.view = "register"
        self.clear_window()
        t = self.t
        shell = tk.Frame(self, bg=t["BG"])
        shell.pack(fill="both", expand=True, padx=40, pady=28)

        top = tk.Frame(shell, bg=t["BG"])
        top.pack(fill="x")
        self.button(top, "←  Back to login", self.show_auth).pack(side="left")
        self.button(top, self.theme_button_text(), self.toggle_theme).pack(side="right")

        card = tk.Frame(shell, bg=t["PANEL"], padx=36, pady=28,
                        highlightthickness=1, highlightbackground=t["BORDER"])
        card.pack(fill="both", expand=True, pady=(18, 0))
        self.label(card, "Create your account", 24, bold=True).pack(anchor="w")
        self.label(card, "A few details to get started.", 11, "MUTED").pack(anchor="w", pady=(6, 18))

        form = tk.Frame(card, bg=t["PANEL"])
        form.pack(fill="x")
        for text, key, secret in (
            ("Full name", "reg_full_name", False),
            ("Username (letters, numbers, _ or .)", "reg_username", False),
            ("Password (at least 8 characters)", "reg_password", True),
            ("Confirm password", "reg_confirm", True),
        ):
            self.label(form, text, 10, "MUTED", True).pack(anchor="w", pady=(7, 5))
            if secret:
                self.password_field(form, key)
            else:
                self.entry(form, key=key).pack(fill="x")

        self.button(card, "Create account  →", self.do_register, primary=True).pack(fill="x", pady=(22, 0))
        self.fields["reg_confirm"].bind("<Return>", lambda _e: self.do_register())
        self.fields["reg_full_name"].focus_set()

    def do_register(self):
        full_name = " ".join(self.fields["reg_full_name"].get().split())
        username = self.fields["reg_username"].get().strip()
        password = self.fields["reg_password"].get()
        confirm = self.fields["reg_confirm"].get()

        if not (full_name and username and password and confirm):
            return self.toast("Please fill in all fields.", "error")
        if not USERNAME_PATTERN.match(username):
            return self.toast("Username must be 3-30 characters: letters, numbers, _ or .", "error")
        if len(password) < 8:
            return self.toast("Password must be at least 8 characters.", "error")
        if password != confirm:
            return self.toast("The passwords do not match.", "error")

        try:
            self.db.create_account(full_name, username, hash_password(password))
        except sqlite3.IntegrityError:
            return self.toast("That username is already taken. Choose a different one.", "error")
        except sqlite3.Error as error:
            return self.toast(f"Could not create the account: {error}", "error")

        self.show_auth()
        self.fields["login_username"].set(username)
        self.fields["login_password"].focus_set()
        self.toast("Account created. You can now sign in.", "success")

    def do_login(self):
        username = self.fields["login_username"].get().strip()
        password = self.fields["login_password"].get()
        if not username or not password:
            return self.toast("Enter your username and password.", "error")

        key = username.lower()
        failures, locked_until = self.failed_logins.get(key, (0, 0.0))
        remaining = locked_until - time.monotonic()
        if remaining > 0:
            return self.toast(f"Too many failed attempts. Try again in {int(remaining) + 1}s.", "error")

        account = self.db.find_for_login(username)
        # Always hash once, even for unknown usernames (keeps timing consistent).
        valid = verify_password(password, account[3] if account else DUMMY_HASH)

        if account and valid:
            self.failed_logins.pop(key, None)
            self.account_id = account[0]
            self.current_page = "Overview"
            self.view = "app"
            if self.build_shell():
                self.show_page("Overview")
            return

        failures += 1
        if failures >= MAX_LOGIN_ATTEMPTS:
            self.failed_logins[key] = (0, time.monotonic() + LOCKOUT_SECONDS)
            self.toast(f"Too many failed attempts. Locked for {LOCKOUT_SECONDS} seconds.", "error")
        else:
            self.failed_logins[key] = (failures, 0.0)
            self.toast("Incorrect username or password.", "error")

    # ----- main shell (built once, pages are swapped inside) -----

    def build_shell(self):
        account = self.db.get_account(self.account_id)
        if account is None:
            self.force_logout("Your account could not be found.")
            return False

        self.clear_window()
        t = self.t
        sidebar = tk.Frame(self, bg=t["PANEL"], width=230,
                           highlightthickness=1, highlightbackground=t["BORDER"])
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        self.label(sidebar, "NOVA", 24, "ACCENT", True).pack(anchor="w", padx=24, pady=(28, 2))
        self.label(sidebar, "DIGITAL BANKING", 8, "MUTED", True).pack(anchor="w", padx=26, pady=(0, 30))

        for symbol, name in self.NAV_ITEMS:
            btn = self.button(sidebar, f"{symbol}    {name}", lambda p=name: self.show_page(p),
                              shadow=False, anchor="w", padx=18, pady=12, font=self.font(11),
                              bg=t["PANEL"], hover=t["PANEL_LIGHT"], fg=t["MUTED"])
            btn.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[name] = btn

        # Bottom area (packed bottom-up)
        self.button(sidebar, "↪  Log out", self.logout).pack(side="bottom", fill="x", padx=16, pady=(6, 20))
        self.button(sidebar, self.theme_button_text(), self.toggle_theme).pack(side="bottom", fill="x", padx=16, pady=6)
        self.label(sidebar, f"LOCAL DEMO • {CURRENCY}", 8, "MUTED").pack(side="bottom", anchor="w", padx=24, pady=(0, 6))

        content = tk.Frame(self, bg=t["BG"])
        content.pack(side="left", fill="both", expand=True, padx=30, pady=26)
        header = tk.Frame(content, bg=t["BG"])
        header.pack(fill="x", pady=(0, 22))
        self.title_label = self.label(header, "", 23, bold=True)
        self.title_label.pack(side="left")
        self.label(header, f"Hello, {account[1]}", 10, "MUTED").pack(side="right", pady=8)

        self.body = tk.Frame(content, bg=t["BG"])
        self.body.pack(fill="both", expand=True)
        return True

    def _style_nav(self):
        t = self.t
        for name, btn in self.nav_buttons.items():
            active = name == self.current_page
            btn.set_style(bg=t["ACCENT_DARK"] if active else t["PANEL"],
                          hover=t["ACCENT_DARK"] if active else t["PANEL_LIGHT"],
                          fg=t["ACCENT"] if active else t["MUTED"],
                          font=self.font(11, active))

    def show_page(self, page):
        self.current_page = page
        for child in self.body.winfo_children():
            child.destroy()
        self.fields = {}
        self.filter_buttons = {}
        self.title_label.config(text=page)
        self._style_nav()

        if page == "Overview":
            self.render_overview(self.body)
        elif page in ("Deposit", "Withdraw"):
            self.render_money_page(self.body, page)
        elif page == "Transactions":
            self.render_transactions(self.body)

    # ----- pages -----

    def render_overview(self, parent):
        t = self.t
        account = self.db.get_account(self.account_id)
        if account is None:
            return self.force_logout("Your account could not be found.")
        deposited, withdrawn, count = self.db.get_stats(self.account_id)

        card = tk.Frame(parent, bg=t["PANEL"], padx=28, pady=24,
                        highlightthickness=1, highlightbackground=t["BORDER"])
        card.pack(fill="x")
        self.label(card, "TOTAL AVAILABLE BALANCE", 10, "MUTED", True).pack(anchor="w")
        self.label(card, f"{CURRENCY} {fmt(account[3])}", 32, bold=True).pack(anchor="w", pady=(8, 12))
        self.label(card, f"Account username: {account[2]}", 10, "MUTED").pack(anchor="w")

        stats = tk.Frame(parent, bg=t["BG"])
        stats.pack(fill="x", pady=(14, 0))
        items = (
            ("TOTAL DEPOSITED", f"{CURRENCY} {fmt(deposited)}", "GREEN"),
            ("TOTAL WITHDRAWN", f"{CURRENCY} {fmt(withdrawn)}", "RED"),
            ("TRANSACTIONS", f"{count:,}", "ACCENT"),
        )
        paddings = ((0, 7), (7, 7), (7, 0))
        for index, (title, value, color) in enumerate(items):
            stats.columnconfigure(index, weight=1, uniform="stat")
            box = tk.Frame(stats, bg=t["PANEL"], padx=20, pady=16,
                           highlightthickness=1, highlightbackground=t["BORDER"])
            box.grid(row=0, column=index, sticky="ew", padx=paddings[index])
            self.label(box, title, 9, "MUTED", True).pack(anchor="w")
            self.label(box, value, 16, color, True).pack(anchor="w", pady=(6, 0))

        actions = tk.Frame(parent, bg=t["BG"])
        actions.pack(fill="x", pady=16)
        self.button(actions, "＋  Deposit money", lambda: self.show_page("Deposit"),
                    primary=True).pack(side="left", padx=(0, 10))
        self.button(actions, "−  Withdraw money", lambda: self.show_page("Withdraw")).pack(side="left")

        self.label(parent, "Recent transactions", 15, bold=True).pack(anchor="w", pady=(6, 10))
        rows = self.db.fetch_transactions(self.account_id, limit=5)
        if rows:
            self.transaction_table(parent, rows, height=5)
        else:
            self.label(parent, "No transactions yet. Your activity will appear here.", 11, "MUTED").pack(anchor="w", pady=10)

    def render_money_page(self, parent, kind):
        t = self.t
        account = self.db.get_account(self.account_id)
        if account is None:
            return self.force_logout("Your account could not be found.")
        balance = account[3]

        subtitle = ("Add money to your account" if kind == "Deposit"
                    else "Withdraw from your available balance")
        card = tk.Frame(parent, bg=t["PANEL"], padx=30, pady=28,
                        highlightthickness=1, highlightbackground=t["BORDER"])
        card.pack(fill="x")
        self.label(card, subtitle, 12, "MUTED").pack(anchor="w")
        self.label(card, f"Available balance: {CURRENCY} {fmt(balance)}", 15, "ACCENT", True).pack(anchor="w", pady=(10, 22))

        self.label(card, f"Amount ({CURRENCY})", 10, "MUTED", True).pack(anchor="w", pady=(0, 7))
        amount_entry = self.entry(card, key="amount")
        amount_entry.pack(fill="x")
        amount_entry.bind("<Return>", lambda _e: self.submit_money(kind))
        amount_entry.focus_set()

        quick = tk.Frame(card, bg=t["PANEL"])
        quick.pack(fill="x", pady=(12, 0))
        for value in self.QUICK_AMOUNTS:
            self.button(quick, f"{value:,}", lambda v=value: self.set_amount(str(v))).pack(side="left", padx=(0, 8))
        if kind == "Withdraw" and balance > 0:
            self.button(quick, "Max", lambda: self.set_amount(f"{Decimal(int(balance)) / 100:.2f}")).pack(side="left")

        self.button(card, f"Confirm {kind.lower()}  →", lambda: self.submit_money(kind),
                    primary=True).pack(anchor="w", pady=(22, 0))

    def set_amount(self, text):
        entry = self.fields.get("amount")
        if entry is not None:
            entry.set(text)
            entry.focus_set()

    def submit_money(self, kind):
        entry = self.fields.get("amount")
        if entry is None:
            return
        try:
            amount = parse_amount(entry.get())
        except ValueError as error:
            return self.toast(str(error), "error")

        cents = to_cents(amount)
        try:
            self.db.apply_transaction(self.account_id, kind, cents)
        except InsufficientFunds:
            return self.toast("You don't have enough money for this withdrawal.", "error")
        except AccountNotFound:
            return self.force_logout("Your account could not be found.")
        except sqlite3.Error as error:
            return self.toast(f"Could not complete the {kind.lower()}: {error}", "error")

        self.show_page("Overview")
        verb = "deposited" if kind == "Deposit" else "withdrawn"
        self.toast(f"{CURRENCY} {fmt(cents)} {verb} successfully.", "success")

    def render_transactions(self, parent):
        t = self.t
        top = tk.Frame(parent, bg=t["BG"])
        top.pack(fill="x", pady=(0, 14))
        self.label(top, "Your deposits and withdrawals, newest first.", 11, "MUTED").pack(side="left")

        filters = tk.Frame(top, bg=t["BG"])
        filters.pack(side="right")
        for text, kind in self.FILTERS:
            btn = self.button(filters, text, lambda k=kind: self.set_filter(k),
                              shadow=False, padx=14, pady=7, radius=10)
            btn.pack(side="left", padx=(6, 0))
            self.filter_buttons[kind] = btn

        self.tx_container = tk.Frame(parent, bg=t["BG"])
        self.tx_container.pack(fill="both", expand=True)
        self.refresh_transactions()

    def set_filter(self, kind):
        self.tx_filter = kind
        self.refresh_transactions()

    def refresh_transactions(self):
        t = self.t
        for kind, btn in self.filter_buttons.items():
            active = kind == self.tx_filter
            btn.set_style(bg=t["ACCENT_DARK"] if active else t["PANEL_LIGHT"],
                          hover=t["ACCENT_DARK"] if active else t["HOVER"],
                          fg=t["ACCENT"] if active else t["MUTED"],
                          font=self.font(10, active))
        for child in self.tx_container.winfo_children():
            child.destroy()

        rows = self.db.fetch_transactions(self.account_id, self.tx_filter, TRANSACTIONS_LIMIT)
        if not rows:
            self.label(self.tx_container, "No transactions found.", 12, "MUTED").pack(anchor="w", pady=25)
            return
        self.transaction_table(self.tx_container, rows, height=14, scrollbar=True)
        if len(rows) == TRANSACTIONS_LIMIT:
            self.label(self.tx_container, f"Showing the latest {TRANSACTIONS_LIMIT} transactions.",
                       9, "MUTED").pack(anchor="w", pady=(8, 0))

    def transaction_table(self, parent, rows, height=8, scrollbar=False):
        t = self.t
        wrap = tk.Frame(parent, bg=t["PANEL"], highlightthickness=1, highlightbackground=t["BORDER"])
        wrap.pack(fill="both", expand=True)

        table = ttk.Treeview(wrap, columns=("type", "amount", "date", "balance"),
                             show="headings", height=height, selectmode="browse")
        for column, title, anchor, width in (
            ("type", "TYPE", "w", 110),
            ("amount", f"AMOUNT ({CURRENCY})", "e", 150),
            ("date", "DATE", "w", 200),
            ("balance", f"BALANCE AFTER ({CURRENCY})", "e", 190),
        ):
            table.heading(column, text=title, anchor=anchor)
            table.column(column, width=width, anchor=anchor, stretch=True)

        table.tag_configure("Deposit", foreground=t["GREEN"])
        table.tag_configure("Withdraw", foreground=t["RED"])
        for kind, amount, date, balance_after in rows:
            sign = "+" if kind == "Deposit" else "−"
            table.insert("", "end", tags=(kind,),
                         values=(kind, f"{sign}{fmt(amount)}", format_date(date), fmt(balance_after)))

        if scrollbar:
            bar = ttk.Scrollbar(wrap, orient="vertical", command=table.yview)
            table.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
        table.pack(side="left", fill="both", expand=True)
        return table

    # ----- session -----

    def logout(self):
        if messagebox.askyesno("Log out", "Are you sure you want to log out?"):
            self.force_logout()

    def force_logout(self, message=None):
        self.account_id = None
        self.current_page = "Overview"
        self.view = "auth"
        self.show_auth()
        if message:
            self.toast(message, "error")

    def on_close(self):
        self.db.close()
        self.destroy()


if __name__ == "__main__":
    ATMApp().mainloop()