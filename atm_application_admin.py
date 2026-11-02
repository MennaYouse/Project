"""NOVA ATM - separate admin portal. Uses the existing ATM_System.db.
Customer-facing ATM code is not modified by this file.
"""
import hashlib
import os
import hmac
import secrets
import sqlite3
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from decimal import Decimal, InvalidOperation

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "ATM_System.db")
ITERATIONS = 200_000
REFRESH_MS = 5000

THEMES = {
    "dark": {"BG":"#0B1220","PANEL":"#121C2E","PANEL_LIGHT":"#1B2940","TEXT":"#F4F7FB","MUTED":"#9AAAC0","BORDER":"#2A3A52","ACCENT":"#38D6C4","ACCENT_HOVER":"#70E8DA","ACCENT_DARK":"#183F45","ON_ACCENT":"#06101C","HOVER":"#263852","RED":"#FF7272","GREEN":"#54D6A0","SHADOW":"#000000","SHADOW_A":0.45},
    "light": {"BG":"#F1F5F9","PANEL":"#FFFFFF","PANEL_LIGHT":"#E8EEF5","TEXT":"#0F172A","MUTED":"#64748B","BORDER":"#CBD5E1","ACCENT":"#0E9F8E","ACCENT_HOVER":"#0B8577","ACCENT_DARK":"#D5F1EC","ON_ACCENT":"#FFFFFF","HOVER":"#D8E1EC","RED":"#DC2626","GREEN":"#059669","SHADOW":"#0F172A","SHADOW_A":0.14}
}


def blend(widget, base, over, alpha):
    """Blend two Tk color values to create a subtle shadow without transparency."""
    try:
        br, bg, bb = widget.winfo_rgb(base)
        or_, og, ob = widget.winfo_rgb(over)
        r = round((br / 257) + ((or_ / 257) - (br / 257)) * alpha)
        g = round((bg / 257) + ((og / 257) - (bg / 257)) * alpha)
        b = round((bb / 257) + ((ob / 257) - (bb / 257)) * alpha)
        return f"#{r:02x}{g:02x}{b:02x}"
    except (tk.TclError, TypeError, ValueError):
        # Safe fallback in case a theme color is invalid.
        return base


def connect_db():
    con = sqlite3.connect(DB_NAME, timeout=10)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, saved):
    try:
        salt_hex, digest_hex = saved.split(":")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), ITERATIONS).hex()
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError, AttributeError):
        return False


def money(cents):
    return f"EGP {Decimal(int(cents)) / 100:,.2f}"


def amount_to_cents(raw):
    try:
        value = Decimal(raw.strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ValueError("Enter a valid amount, e.g. 250 or 250.50.") from None
    if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2:
        raise ValueError("Amount must be positive and have at most 2 decimal places.")
    if value > Decimal("1000000"):
        raise ValueError("Maximum amount per operation is EGP 1,000,000.")
    return int(value * 100)


def setup_admins():
    # Never silently create/use a separate empty database: it makes the dashboard
    # appear blank while the customer app's real database remains elsewhere.
    if not os.path.isfile(DB_NAME):
        raise FileNotFoundError(
            f"Could not find ATM_System.db next to admin_page.py.\n\n"
            f"Put admin_page.py in the same folder as the customer app's ATM_System.db.\n\n"
            f"Expected location: {DB_NAME}"
        )
    with connect_db() as con:
        existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = {"Accounts", "Transactions"} - existing
        if missing:
            raise RuntimeError(
                "The ATM_System.db beside admin_page.py is not the customer app database "
                f"(missing tables: {', '.join(sorted(missing))}).\n\n"
                "Move admin_page.py next to the correct ATM_System.db and run it again."
            )
        con.execute("""CREATE TABLE IF NOT EXISTS Admins (
            AdminID INTEGER PRIMARY KEY AUTOINCREMENT,
            Username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            PasswordHash TEXT NOT NULL,
            MustChangePassword INTEGER NOT NULL DEFAULT 1)""")
        cols = {r[1] for r in con.execute("PRAGMA table_info(Admins)").fetchall()}
        if "MustChangePassword" not in cols:
            # Existing demo/admin accounts must change their password on next sign-in.
            con.execute("ALTER TABLE Admins ADD COLUMN MustChangePassword INTEGER NOT NULL DEFAULT 1")
        if not con.execute("SELECT 1 FROM Admins LIMIT 1").fetchone():
            con.execute("INSERT INTO Admins (Username,PasswordHash,MustChangePassword) VALUES (?,?,1)",
                        ("admin", hash_password("Admin@12345")))
        # Track which admin carried out admin-side deposits/withdrawals.
        tx_cols = {r[1] for r in con.execute("PRAGMA table_info(Transactions)").fetchall()}
        if "PerformedBy" not in tx_cols:
            con.execute("ALTER TABLE Transactions ADD COLUMN PerformedBy TEXT")


def rounded_polygon(canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = max(2, min(radius, int((y2-y1)/2), int((x2-x1)/2)))
    points = [x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
              x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
              x1,y2, x1,y2-radius, x1,y1+radius, x1,y1]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class RoundedButton(tk.Canvas):
    """Canvas button styled to match the rounded, softly shadowed NOVA controls."""
    def __init__(self, parent, text, command, *, bg, hover, fg, font,
                 shadow="#000000", shadow_alpha=0.0, radius=12, padx=18, pady=10,
                 width=None, height=None, anchor="center"):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command = text, command
        self.bg_color, self.hover_color, self.fg_color = bg, hover, fg
        self.font, self.radius, self.padx, self.pady, self.anchor = font, radius, padx, pady, anchor
        self.shadow, self.shadow_alpha = shadow, shadow_alpha
        self.hovering = False; self.pressed = False
        self.configure(width=width or max(90, len(text)*8+padx*2), height=height or 44)
        self.bind("<Configure>", lambda _e: self.draw())
        self.bind("<Enter>", self._enter); self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press); self.bind("<ButtonRelease-1>", self._release)
        self.draw()

    def set_style(self, *, bg=None, hover=None, fg=None, text=None):
        if bg is not None: self.bg_color = bg
        if hover is not None: self.hover_color = hover
        if fg is not None: self.fg_color = fg
        if text is not None: self.text = text
        self.draw()

    def draw(self):
        if not self.winfo_exists(): return
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8: return
        self.delete("all")
        bottom = h-5 if self.shadow_alpha else h-1
        if self.shadow_alpha:
            # Subtle offset shadow; keep it inside the canvas to avoid clipping.
            rounded_polygon(self, 2, 4, w-2, bottom+3, self.radius, fill=self.shadow, outline="")
        fill = self.hover_color if (self.hovering or self.pressed) else self.bg_color
        rounded_polygon(self, 2, 1, w-2, bottom, self.radius, fill=fill, outline="")
        y = (bottom+1)/2 + (1 if self.pressed else 0)
        self.create_text(self.padx if self.anchor == "w" else w/2, y,
                         text=self.text, fill=self.fg_color, font=self.font,
                         anchor="w" if self.anchor == "w" else "center")

    def _enter(self, _e): self.hovering = True; self.draw()
    def _leave(self, _e): self.hovering = False; self.pressed = False; self.draw()
    def _press(self, _e): self.pressed = True; self.draw()
    def _release(self, e):
        was_pressed = self.pressed; self.pressed = False; self.draw()
        if was_pressed and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            if self.command: self.command()


class EntryField(tk.Frame):
    """Rounded input with a soft shadow and a drawn eye / crossed-eye toggle."""
    def __init__(self, parent, app, *, password=False, width=None):
        self.app = app
        super().__init__(parent, bg=parent.cget("bg"), bd=0, highlightthickness=0)
        self.password = password
        self.hidden = password
        self.canvas = tk.Canvas(self, height=48, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", expand=True)
        self.inner = tk.Frame(self.canvas, bg=app.t["PANEL_LIGHT"], bd=0)
        self.entry = tk.Entry(self.inner, bg=app.t["PANEL_LIGHT"], fg=app.t["TEXT"],
            insertbackground=app.t["TEXT"], selectbackground=app.t["ACCENT_DARK"],
            selectforeground=app.t["TEXT"], relief="flat", bd=0, font=("Segoe UI", 11),
            show="•" if password else "")
        self.entry.pack(side="left", fill="both", expand=True, padx=(12, 5), ipady=9)
        self.eye_canvas = None
        if password:
            self.eye_canvas = tk.Canvas(self.inner, width=34, height=34,
                bg=app.t["PANEL_LIGHT"], highlightthickness=0, bd=0, cursor="hand2")
            self.eye_canvas.pack(side="right", padx=(0, 5), pady=2)
            self.eye_canvas.bind("<Button-1>", self.toggle_eye)
            self.eye_canvas.bind("<Enter>", lambda _e: self.draw_eye(app.t["ACCENT"]))
            self.eye_canvas.bind("<Leave>", lambda _e: self.draw_eye(app.t["MUTED"]))
            self.draw_eye(app.t["MUTED"])
        self.window_id = self.canvas.create_window(5, 5, anchor="nw", window=self.inner)
        self.canvas.bind("<Configure>", self.draw_field)
        if width: self.configure(width=width)
        self.draw_field()

    def draw_field(self, _event=None):
        if not self.winfo_exists(): return
        w = max(self.canvas.winfo_width(), 80)
        h = max(self.canvas.winfo_height(), 42)
        self.canvas.delete("field")
        t = self.app.t
        # subtle shadow below the rounded input
        rounded_polygon(self.canvas, 2, 4, w-2, h-1, 13,
                        fill=blend(self.canvas, self.master.cget("bg"), t["SHADOW"], t["SHADOW_A"]), outline="", tags="field")
        rounded_polygon(self.canvas, 2, 1, w-2, h-4, 13,
                        fill=t["PANEL_LIGHT"], outline=t["BORDER"], width=1, tags="field")
        self.canvas.tag_lower("field")
        self.canvas.coords(self.window_id, 5, 4)
        self.canvas.itemconfigure(self.window_id, width=max(40, w-10), height=max(30, h-10))

    def draw_eye(self, color=None):
        if not self.eye_canvas: return
        c = self.eye_canvas
        color = color or self.app.t["MUTED"]
        c.delete("all")
        # almond-shaped eye, pupil, and a slash when the password is currently visible
        c.create_polygon(5,17, 9,11, 15,8, 21,11, 27,17, 21,23, 15,26, 9,23,
                         smooth=True, fill="", outline=color, width=1.8)
        c.create_oval(12,14,18,20, outline=color, width=1.7)
        c.create_oval(14,16,16,18, fill=color, outline=color)
        if not self.hidden:
            c.create_line(7,27,27,7, fill=color, width=1.8, capstyle="round")

    def toggle_eye(self, _event=None):
        self.hidden = not self.hidden
        self.entry.configure(show="•" if self.hidden else "")
        self.draw_eye(self.app.t["ACCENT"])
        self.entry.focus_set()

    def get(self): return self.entry.get()
    def delete(self, *args): return self.entry.delete(*args)
    def insert(self, *args): return self.entry.insert(*args)
    def focus_set(self): return self.entry.focus_set()
    def bind(self, sequence=None, func=None, add=None): return self.entry.bind(sequence, func, add)
    def configure_theme(self):
        t = self.app.t
        self.configure(bg=self.master.cget("bg"))
        self.canvas.configure(bg=self.master.cget("bg"))
        self.inner.configure(bg=t["PANEL_LIGHT"])
        self.entry.configure(bg=t["PANEL_LIGHT"], fg=t["TEXT"], insertbackground=t["TEXT"],
                             selectbackground=t["ACCENT_DARK"], selectforeground=t["TEXT"])
        if self.password:
            self.eye_canvas.configure(bg=t["PANEL_LIGHT"])
            self.draw_eye(t["MUTED"])
        self.draw_field()


class AdminApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            setup_admins()
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            messagebox.showerror("ATM database problem", str(exc), parent=self)
            self.destroy()
            return
        self.title("NOVA ATM | Admin Portal")
        self.geometry("1180x760"); self.minsize(960, 640)
        self.theme_name = "dark"; self.t = THEMES[self.theme_name]
        self.admin = None; self.page = "Overview"; self._refresh_job = None
        self.style = ttk.Style(self); self.style.theme_use("clam"); self.apply_style()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.show_login()

    def apply_style(self):
        t = self.t; self.configure(bg=t["BG"])
        self.style.configure("Treeview", background=t["PANEL"], foreground=t["TEXT"],
            fieldbackground=t["PANEL"], rowheight=34, borderwidth=0, font=("Segoe UI", 10))
        self.style.configure("Treeview.Heading", background=t["PANEL_LIGHT"],
            foreground=t["MUTED"], relief="flat", font=("Segoe UI", 9, "bold"), padding=8)
        self.style.map("Treeview", background=[("selected", t["ACCENT_DARK"])], foreground=[("selected", t["TEXT"])])
        self.style.configure("Vertical.TScrollbar", background=t["PANEL_LIGHT"], troughcolor=t["BG"], bordercolor=t["BG"], arrowcolor=t["TEXT"])

    def close_app(self):
        if self._refresh_job:
            try: self.after_cancel(self._refresh_job)
            except tk.TclError: pass
        self.destroy()

    def clear(self):
        if self._refresh_job:
            try: self.after_cancel(self._refresh_job)
            except tk.TclError: pass
            self._refresh_job = None
        for child in self.winfo_children(): child.destroy()

    def label(self, parent, text, size=11, color=None, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color or self.t["TEXT"],
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, command, primary=False, *, icon=False, width=None, anchor="center"):
        t = self.t
        bg = t["ACCENT"] if primary else t["PANEL_LIGHT"]
        hover = t["ACCENT_HOVER"] if primary else t["HOVER"]
        fg = t["ON_ACCENT"] if primary else t["TEXT"]
        return RoundedButton(parent, text, command, bg=bg, hover=hover, fg=fg,
            font=("Segoe UI", 10, "bold"), shadow=t["SHADOW"],
            shadow_alpha=t["SHADOW_A"], radius=13, padx=15, pady=9,
            width=width or (44 if icon else None), height=44 if icon else 46,
            anchor=anchor)

    def entry(self, parent, password=False, width=None):
        return EntryField(parent, self, password=password, width=width)

    def theme_icon(self): return "☀" if self.theme_name == "dark" else "☾"

    def icon_button(self, parent):
        return self.button(parent, self.theme_icon(), self.toggle_theme, icon=True)

    def show_login(self):
        self.clear(); t = self.t
        outer = tk.Frame(self, bg=t["BG"]); outer.pack(fill="both", expand=True, padx=48, pady=30)
        top = tk.Frame(outer, bg=t["BG"]); top.pack(fill="x")
        self.icon_button(top).pack(side="right")
        card = tk.Frame(outer, bg=t["PANEL"], padx=42, pady=34,
                        highlightthickness=1, highlightbackground=t["BORDER"])
        card.pack(fill="both", expand=True, padx=100, pady=(18, 10))
        self.label(card, "NOVA", 30, t["ACCENT"], True).pack(anchor="w")
        self.label(card, "ADMIN CONTROL CENTER", 10, t["MUTED"], True).pack(anchor="w", pady=(2, 25))
        self.label(card, "Administrator sign in", 23, bold=True).pack(anchor="w")
        self.label(card, "Admin access is separate from customer accounts.", 11, t["MUTED"]).pack(anchor="w", pady=(6, 20))
        self.label(card, "Username", 10, t["MUTED"], True).pack(anchor="w", pady=(0, 5))
        self.username = self.entry(card); self.username.pack(fill="x")
        self.label(card, "Password", 10, t["MUTED"], True).pack(anchor="w", pady=(15, 5))
        self.password = self.entry(card, password=True); self.password.pack(fill="x")
        self.button(card, "Sign in as admin  →", self.login, True).pack(anchor="w", pady=(22, 10))
        self.label(card, "First login: admin  |  Admin@12345 (you must change it)", 9, t["MUTED"]).pack(anchor="w")
        bottom = tk.Frame(card, bg=t["PANEL"]); bottom.pack(fill="x", pady=(18, 0))
        self.button(bottom, "Open customer app…", self.open_customer_app).pack(side="left")
        self.label(bottom, "This shortcut is only in the admin portal.", 9, t["MUTED"]).pack(side="left", padx=12)
        self.password.bind("<Return>", lambda _e: self.login())
        self.username.bind("<Return>", lambda _e: self.password.focus_set())
        self.username.focus_set()

    def open_customer_app(self):
        path = filedialog.askopenfilename(title="Select your existing NOVA customer app Python file",
            filetypes=[("Python files", "*.py"), ("All files", "*.*")], parent=self)
        if not path: return
        try: subprocess.Popen([sys.executable, path], cwd=__import__("os").path.dirname(path))
        except OSError as exc: messagebox.showerror("Could not open app", str(exc), parent=self)

    def login(self):
        username, password = self.username.get().strip(), self.password.get()
        if not username or not password:
            messagebox.showwarning("Missing information", "Enter username and password.", parent=self); return
        with connect_db() as con:
            row = con.execute("SELECT Username, PasswordHash, MustChangePassword FROM Admins WHERE Username=? COLLATE NOCASE", (username,)).fetchone()
        if not row or not verify_password(password, row[1]):
            messagebox.showerror("Sign in failed", "Incorrect admin username or password.", parent=self); return
        self.admin = row[0]
        if row[2]:
            self.force_password_change()
            return
        self.show_dashboard("Overview")

    def force_password_change(self):
        # Mandatory screen before dashboard; the demo password cannot be used to continue.
        self.clear(); t = self.t
        shell = tk.Frame(self, bg=t["BG"]); shell.pack(fill="both", expand=True, padx=80, pady=45)
        card = tk.Frame(shell, bg=t["PANEL"], padx=38, pady=32,
                        highlightthickness=1, highlightbackground=t["BORDER"])
        card.pack(fill="both", expand=True, padx=90)
        self.label(card, "Security step required", 24, t["ACCENT"], True).pack(anchor="w")
        self.label(card, "For your protection, change the temporary password before continuing.", 11, t["MUTED"], wraplength=620, justify="left").pack(anchor="w", pady=(8,22))
        self.label(card, "New password (at least 8 characters)", 10, t["MUTED"], True).pack(anchor="w", pady=(0,5))
        new = self.entry(card, password=True); new.pack(fill="x")
        self.label(card, "Confirm new password", 10, t["MUTED"], True).pack(anchor="w", pady=(14,5))
        confirm = self.entry(card, password=True); confirm.pack(fill="x")
        def save():
            a, b = new.get(), confirm.get()
            if len(a) < 8:
                messagebox.showwarning("Weak password", "Use at least 8 characters.", parent=self); return
            if a != b:
                messagebox.showwarning("Passwords do not match", "Enter the same new password in both fields.", parent=self); return
            with connect_db() as con:
                con.execute("UPDATE Admins SET PasswordHash=?, MustChangePassword=0 WHERE Username=?", (hash_password(a), self.admin))
            messagebox.showinfo("Password changed", "Your password was updated. You can now use the admin dashboard.", parent=self)
            self.show_dashboard("Overview")
        self.button(card, "Save new password  →", save, True).pack(anchor="w", pady=(20,0))
        new.bind("<Return>", lambda _e: confirm.focus_set()); confirm.bind("<Return>", lambda _e: save())
        new.focus_set()

    def toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.t = THEMES[self.theme_name]; self.apply_style()
        # Rebuild only on deliberate theme change, never on the timed data refresh.
        if self.admin:
            with connect_db() as con:
                row = con.execute("SELECT MustChangePassword FROM Admins WHERE Username=?", (self.admin,)).fetchone()
            if row and row[0]: self.force_password_change()
            else: self.show_dashboard(self.page)
        else: self.show_login()

    def show_dashboard(self, page="Overview"):
        self.page = page; self.clear(); t = self.t
        shell = tk.Frame(self, bg=t["BG"]); shell.pack(fill="both", expand=True)
        sidebar = tk.Frame(shell, bg=t["PANEL"], width=225); sidebar.pack(side="left", fill="y"); sidebar.pack_propagate(False)
        self.label(sidebar, "NOVA", 22, t["ACCENT"], True).pack(anchor="w", padx=20, pady=(24,2))
        self.label(sidebar, "ADMIN CONTROL CENTER", 8, t["MUTED"], True).pack(anchor="w", padx=21, pady=(0,25))
        for item in ("Overview", "Customers", "Transactions", "Admin settings"):
            active = item == page
            self.button(sidebar, ("●  " if active else "   ") + item,
                lambda p=item: self.show_dashboard(p), primary=active, anchor="w").pack(fill="x", padx=12, pady=4)
        self.button(sidebar, "↪  Admin logout", self.logout).pack(side="bottom", fill="x", padx=12, pady=6)
        self.icon_button(sidebar).pack(side="bottom", anchor="w", padx=12, pady=(4,8))
        content = tk.Frame(shell, bg=t["BG"]); content.pack(side="left", fill="both", expand=True, padx=26, pady=22)
        head = tk.Frame(content, bg=t["BG"]); head.pack(fill="x", pady=(0,18))
        self.label(head, "Admin / " + page, 22, bold=True).pack(side="left")
        self.label(head, "Signed in: " + self.admin, 10, t["MUTED"]).pack(side="right", pady=8)
        if page == "Overview": self.overview(content)
        elif page == "Customers": self.customers(content)
        elif page == "Transactions": self.transactions(content)
        else: self.admin_settings(content)
        self.schedule_refresh()

    def schedule_refresh(self):
        if self._refresh_job:
            try: self.after_cancel(self._refresh_job)
            except tk.TclError: pass
        self._refresh_job = self.after(REFRESH_MS, self.refresh_data)

    def refresh_data(self):
        """Refresh table/card contents in place; never destroy widgets while typing/selecting."""
        self._refresh_job = None
        if not self.admin or not self.winfo_exists(): return
        try:
            if self.page == "Overview": self.load_overview_data()
            elif self.page == "Customers": self.load_customers(self.customer_table, self.customer_search.get())
            elif self.page == "Transactions": self.load_transactions(self.transaction_table, self.transaction_search.get())
        except sqlite3.Error as exc:
            messagebox.showerror(
                "Database refresh failed",
                "Could not read the ATM database. Make sure admin_page.py is beside the same ATM_System.db used by the customer app.\n\n"
                f"Details: {exc}", parent=self
            )
            return
        except (tk.TclError, AttributeError):
            pass
        self.schedule_refresh()

    def overview(self, parent):
        """Bank-wide dashboard with live charts, summary totals and recent activity."""
        self.metric_values = {}
        cards = tk.Frame(parent, bg=self.t["BG"]); cards.pack(fill="x")
        metrics = (
            ("deposits", "TOTAL DEPOSITS"),
            ("withdrawals", "TOTAL WITHDRAWALS"),
            ("count", "CUSTOMER ACCOUNTS"),
            ("balance", "TOTAL CUSTOMER BALANCES"),
        )
        for key, title in metrics:
            c = tk.Frame(cards, bg=self.t["PANEL"], padx=12, pady=10,
                         highlightthickness=1, highlightbackground=self.t["BORDER"])
            c.pack(side="left", fill="both", expand=True, padx=(0, 8))
            self.label(c, title, 8, self.t["MUTED"], True).pack(anchor="w")
            val = self.label(c, "Loading…", 13, self.t["ACCENT"], True)
            val.pack(anchor="w", pady=(7, 0)); self.metric_values[key] = val

        # A compact 2x2 chart grid. Native Tk Canvas is used so no extra package is needed.
        chart_grid = tk.Frame(parent, bg=self.t["BG"])
        chart_grid.pack(fill="x", pady=(10, 0))
        self.chart_canvases = {}
        chart_specs = [
            ("money", "Deposits vs withdrawals (EGP)"),
            ("types", "Transaction split"),
            ("monthly", "Monthly activity (EGP)"),
            ("customers", "Top 5 customer balances (EGP)"),
        ]
        for idx, (key, title) in enumerate(chart_specs):
            card = tk.Frame(chart_grid, bg=self.t["PANEL"], padx=8, pady=6,
                            highlightthickness=1, highlightbackground=self.t["BORDER"])
            card.grid(row=idx // 2, column=idx % 2, sticky="nsew",
                      padx=(0 if idx % 2 == 0 else 5, 5 if idx % 2 == 0 else 0), pady=4)
            self.label(card, title, 9, self.t["TEXT"], True).pack(anchor="w", padx=4, pady=(0, 2))
            chart = tk.Canvas(card, height=118, bg=self.t["PANEL"], highlightthickness=0, bd=0)
            chart.pack(fill="x", expand=True)
            self.chart_canvases[key] = chart
        chart_grid.grid_columnconfigure(0, weight=1, uniform="charts")
        chart_grid.grid_columnconfigure(1, weight=1, uniform="charts")

        # Counts remain visible as concise labels beneath the charts.
        stats = tk.Frame(parent, bg=self.t["BG"]); stats.pack(fill="x", pady=(3, 0))
        self.extra_stats_label = self.label(stats, "Loading transaction counts…", 9, self.t["MUTED"])
        self.extra_stats_label.pack(anchor="w")
        self.overview_error_label = self.label(parent, "", 9, self.t["RED"], wraplength=850, justify="left")
        self.overview_error_label.pack(anchor="w", pady=(2, 0))
        self.label(parent, "Latest activity", 12, bold=True).pack(anchor="w", pady=(5, 4))
        self.overview_table = self.make_table(parent,
            (("username", "CUSTOMER USERNAME", 125), ("type", "TYPE", 75),
             ("amount", "AMOUNT (EGP)", 105), ("date", "DATE", 145),
             ("balance", "BALANCE AFTER (EGP)", 135), ("performed_by", "DONE BY (ADMIN)", 120)),
            height=4)
        self.load_overview_data()

    def _chart_base(self, canvas, title=None):
        canvas.delete("all")
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 260)
        h = max(canvas.winfo_height(), 110)
        return w, h

    def _draw_money_chart(self, deposits, withdrawals):
        c = self.chart_canvases["money"]; w, h = self._chart_base(c)
        t = self.t; base_y = h - 25; top_y = 12
        maxv = max(int(deposits), int(withdrawals), 1)
        c.create_line(28, base_y, w-8, base_y, fill=t["BORDER"])
        bar_w = min(54, max(28, (w-100)//4))
        for x, value, label, color in ((w*0.32, deposits, "Deposits", t["ACCENT"]),
                                       (w*0.68, withdrawals, "Withdrawals", t["RED"])):
            bh = max(2, (base_y-top_y) * int(value) / maxv)
            c.create_rectangle(x-bar_w/2, base_y-bh, x+bar_w/2, base_y,
                               fill=color, outline="")
            c.create_text(x, max(7, base_y-bh-8), text=f"{int(value)/100:,.0f}",
                          fill=t["TEXT"], font=("Segoe UI", 8, "bold"))
            c.create_text(x, h-11, text=label, fill=t["MUTED"], font=("Segoe UI", 8))

    def _draw_type_chart(self, deposits_count, withdrawals_count):
        c = self.chart_canvases["types"]; w, h = self._chart_base(c)
        t = self.t; total = int(deposits_count) + int(withdrawals_count)
        cx, cy, r = min(62, w*0.22), h/2, min(43, h*0.39)
        if total == 0:
            c.create_oval(cx-r, cy-r, cx+r, cy+r, outline=t["BORDER"], width=12)
            c.create_text(cx, cy, text="0", fill=t["TEXT"], font=("Segoe UI", 11, "bold"))
        else:
            dep_angle = 360 * int(deposits_count) / total
            c.create_arc(cx-r, cy-r, cx+r, cy+r, start=90, extent=-dep_angle,
                         style="arc", outline=t["ACCENT"], width=12)
            c.create_arc(cx-r, cy-r, cx+r, cy+r, start=90-dep_angle, extent=-(360-dep_angle),
                         style="arc", outline=t["RED"], width=12)
            c.create_text(cx, cy, text=str(total), fill=t["TEXT"], font=("Segoe UI", 11, "bold"))
        lx = min(w*0.48, 150)
        c.create_rectangle(lx, cy-22, lx+9, cy-13, fill=t["ACCENT"], outline="")
        c.create_text(lx+15, cy-17, anchor="w", text=f"Deposits: {int(deposits_count):,}", fill=t["TEXT"], font=("Segoe UI", 8))
        c.create_rectangle(lx, cy+2, lx+9, cy+11, fill=t["RED"], outline="")
        c.create_text(lx+15, cy+7, anchor="w", text=f"Withdrawals: {int(withdrawals_count):,}", fill=t["TEXT"], font=("Segoe UI", 8))

    def _draw_monthly_chart(self, monthly):
        c = self.chart_canvases["monthly"]; w, h = self._chart_base(c)
        t = self.t; left, right, top, bottom = 30, w-10, 10, h-24
        c.create_line(left, bottom, right, bottom, fill=t["BORDER"])
        if not monthly:
            c.create_text(w/2, h/2, text="No transaction history yet", fill=t["MUTED"], font=("Segoe UI", 8))
            return
        maxv = max([max(int(d), int(v)) for _, d, v in monthly] + [1])
        usable_w = max(1, right-left); usable_h = max(1, bottom-top)
        for series_key, series_label, color in ((1, "Dep", t["ACCENT"]), (2, "Wdr", t["RED"])):
            points = []
            for i, row in enumerate(monthly):
                x = left + (usable_w * i / max(1, len(monthly)-1))
                y = bottom - (usable_h * int(row[series_key]) / maxv)
                points.extend([x, y])
            if len(points) >= 4: c.create_line(*points, fill=color, width=2, smooth=True)
            elif len(points) == 2: c.create_oval(points[0]-3, points[1]-3, points[0]+3, points[1]+3, fill=color, outline="")
        for i, row in enumerate(monthly):
            x = left + (usable_w * i / max(1, len(monthly)-1))
            c.create_text(x, h-10, text=row[0][-2:], fill=t["MUTED"], font=("Segoe UI", 7))
        c.create_text(right-5, top+2, anchor="ne", text="— Deposits   — Withdrawals", fill=t["MUTED"], font=("Segoe UI", 7))

    def _draw_customer_chart(self, customers):
        c = self.chart_canvases["customers"]; w, h = self._chart_base(c)
        t = self.t; rows = list(customers); top = 5; row_h = max(17, (h-8)//5)
        maxv = max([int(r[1]) for r in rows] + [1])
        if not rows:
            c.create_text(w/2, h/2, text="No customer accounts yet", fill=t["MUTED"], font=("Segoe UI", 8))
            return
        for i, (username, balance) in enumerate(rows[:5]):
            y = top + i*row_h
            name = username if len(username) <= 13 else username[:12] + "…"
            c.create_text(2, y+6, text=name, anchor="w", fill=t["TEXT"], font=("Segoe UI", 7))
            x0 = min(94, w*0.30); x1 = w-50
            bw = max(1, (x1-x0) * int(balance) / maxv)
            c.create_rectangle(x0, y, x0+bw, y+11, fill=t["ACCENT"], outline="")
            c.create_text(w-2, y+6, text=f"{int(balance)/100:,.0f}", anchor="e", fill=t["MUTED"], font=("Segoe UI", 7))

    def load_overview_data(self):
        try:
            with connect_db() as con:
                deposits, withdrawals, deposit_count, withdraw_count, tx_count = con.execute("""
                    SELECT
                        COALESCE(SUM(CASE WHEN TransactionType='Deposit' THEN Amount ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN TransactionType='Withdraw' THEN Amount ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN TransactionType='Deposit' THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN TransactionType='Withdraw' THEN 1 ELSE 0 END), 0),
                        COUNT(*) FROM Transactions
                """).fetchone()
                count, balance, funded = con.execute("""
                    SELECT COUNT(*), COALESCE(SUM(Balance), 0),
                           COALESCE(SUM(CASE WHEN Balance > 0 THEN 1 ELSE 0 END), 0)
                    FROM Accounts
                """).fetchone()
                rows = con.execute("""
                    SELECT a.Username, t.TransactionType, t.Amount, t.TransactionDate,
                           t.BalanceAfter, COALESCE(NULLIF(t.PerformedBy, ''), 'Customer / ATM')
                    FROM Transactions t JOIN Accounts a ON a.AccountID=t.AccountID
                    ORDER BY t.TransactionID DESC LIMIT 8
                """).fetchall()
                monthly = con.execute("""
                    SELECT substr(TransactionDate,1,7) AS month,
                           COALESCE(SUM(CASE WHEN TransactionType='Deposit' THEN Amount ELSE 0 END),0),
                           COALESCE(SUM(CASE WHEN TransactionType='Withdraw' THEN Amount ELSE 0 END),0)
                    FROM Transactions
                    WHERE TransactionDate IS NOT NULL
                    GROUP BY substr(TransactionDate,1,7) ORDER BY month DESC LIMIT 6
                """).fetchall()
                top_customers = con.execute("""
                    SELECT Username, Balance FROM Accounts ORDER BY Balance DESC LIMIT 5
                """).fetchall()
            monthly = list(reversed(monthly))
            values = {"deposits": money(deposits), "withdrawals": money(withdrawals),
                      "count": str(count), "balance": money(balance)}
            for key, value in values.items(): self.metric_values[key].configure(text=value)
            self.extra_stats_label.configure(text=(
                f"Transactions: {tx_count:,}   •   Deposit operations: {deposit_count:,}   •   "
                f"Withdrawal operations: {withdraw_count:,}   •   Customers with balance: {funded:,}"
            ))
            self._draw_money_chart(deposits, withdrawals)
            self._draw_type_chart(deposit_count, withdraw_count)
            self._draw_monthly_chart(monthly)
            self._draw_customer_chart(top_customers)
            self.overview_error_label.configure(text="")
            self.fill_table(self.overview_table, [
                (r[0], r[1], f"{int(r[2])/100:,.2f}", r[3], f"{int(r[4])/100:,.2f}", r[5])
                for r in rows
            ])
        except (sqlite3.Error, ValueError, TypeError, tk.TclError) as exc:
            if hasattr(self, "overview_error_label") and self.overview_error_label.winfo_exists():
                self.overview_error_label.configure(text=f"Could not load dashboard charts from {DB_NAME}. Error: {exc}")
            raise

    def customers(self, parent):
        toolbar = tk.Frame(parent, bg=self.t["BG"]); toolbar.pack(fill="x", pady=(0,12))
        self.label(toolbar, "Search customers", 10, self.t["MUTED"]).pack(side="left", padx=(0,8))
        self.customer_search = tk.StringVar()
        self.customer_search_field = self.entry(toolbar, width=300)
        self.customer_search_field.pack(side="left", fill="x", expand=True)
        self.customer_search_field.entry.configure(textvariable=self.customer_search)
        self.customer_search_field.entry.bind("<KeyRelease>", lambda _e: self.load_customers(self.customer_table, self.customer_search.get()))
        self.button(toolbar, "Search", lambda: self.load_customers(self.customer_table, self.customer_search.get())).pack(side="left", padx=7)
        self.button(toolbar, "Show all", self.show_all_customers).pack(side="left")
        self.customer_table = self.make_table(parent, (("id","ID",55),("name","FULL NAME",205),("username","USERNAME",145),("balance","BALANCE (EGP)",140),("created","CREATED AT",175)), height=14)
        actions = tk.Frame(parent, bg=self.t["BG"]); actions.pack(fill="x", pady=(12,0))
        self.button(actions, "Customer details / history", lambda: self.customer_details(self.customer_table)).pack(side="left", padx=(0,8))
        self.button(actions, "Deposit", lambda: self.admin_transaction(self.customer_table, "Deposit"), True).pack(side="left", padx=4)
        self.button(actions, "Withdraw", lambda: self.admin_transaction(self.customer_table, "Withdraw")).pack(side="left", padx=4)
        self.load_customers(self.customer_table, "")

    def show_all_customers(self):
        self.customer_search.set(""); self.load_customers(self.customer_table, "")

    def load_customers(self, table, query):
        if not table.winfo_exists(): return
        with connect_db() as con:
            rows = con.execute("SELECT AccountID,FullName,Username,Balance,CreatedAt FROM Accounts WHERE FullName LIKE ? OR Username LIKE ? ORDER BY AccountID DESC", (f"%{query}%", f"%{query}%")).fetchall()
        self.fill_table(table, [(r[0],r[1],r[2],f"{int(r[3])/100:,.2f}",r[4]) for r in rows])

    def selected_account(self, table):
        selected = table.selection()
        if not selected:
            messagebox.showwarning("Select customer", "Select a customer row first.", parent=self); return None
        return int(table.item(selected[0], "values")[0])

    def customer_details(self, table):
        account_id = self.selected_account(table)
        if account_id is None: return
        with connect_db() as con:
            account = con.execute("SELECT AccountID,FullName,Username,Balance,CreatedAt FROM Accounts WHERE AccountID=?", (account_id,)).fetchone()
            txs = con.execute("SELECT TransactionType,Amount,TransactionDate,BalanceAfter FROM Transactions WHERE AccountID=? ORDER BY TransactionID DESC LIMIT 500", (account_id,)).fetchall()
        if not account:
            messagebox.showerror("Not found", "This customer account no longer exists.", parent=self); return
        win = tk.Toplevel(self); win.title("NOVA | Customer details and history"); win.geometry("900x570"); win.minsize(700,420); win.configure(bg=self.t["BG"])
        self.label(win, f"{account[1]}  |  @{account[2]}  |  ID #{account[0]}", 17, self.t["ACCENT"], True).pack(anchor="w", padx=20, pady=(18,5))
        self.label(win, f"Current balance: {money(account[3])}     Created: {account[4]}", 11, self.t["MUTED"]).pack(anchor="w", padx=20, pady=(0,14))
        self.make_table(win, (("type","TYPE",140),("amount","AMOUNT (EGP)",150),("date","DATE",220),("balance","BALANCE AFTER (EGP)",180)), height=18,
                        initial=[(r[0],f"{int(r[1])/100:,.2f}",r[2],f"{int(r[3])/100:,.2f}") for r in txs])

    def admin_transaction(self, table, kind):
        account_id = self.selected_account(table)
        if account_id is None: return
        raw = simpledialog.askstring(kind, f"Enter {kind.lower()} amount in EGP:", parent=self)
        if raw is None: return
        try: cents = amount_to_cents(raw)
        except ValueError as exc: messagebox.showerror("Invalid amount", str(exc), parent=self); return
        try:
            with connect_db() as con:
                con.execute("BEGIN IMMEDIATE")
                if kind == "Deposit": cur = con.execute("UPDATE Accounts SET Balance=Balance+? WHERE AccountID=?", (cents, account_id))
                else: cur = con.execute("UPDATE Accounts SET Balance=Balance-? WHERE AccountID=? AND Balance>=?", (cents, account_id, cents))
                if cur.rowcount == 0:
                    exists = con.execute("SELECT 1 FROM Accounts WHERE AccountID=?", (account_id,)).fetchone()
                    con.rollback()
                    messagebox.showerror("Transaction failed", "Insufficient balance." if exists else "Customer account not found.", parent=self); return
                balance = con.execute("SELECT Balance FROM Accounts WHERE AccountID=?", (account_id,)).fetchone()[0]
                con.execute("INSERT INTO Transactions (AccountID,TransactionType,Amount,BalanceAfter,PerformedBy) VALUES (?,?,?,?,?)", (account_id, kind, cents, balance, self.admin))
                con.commit()
        except sqlite3.Error as exc:
            messagebox.showerror("Database error", str(exc), parent=self); return
        messagebox.showinfo("Success", f"{kind} completed.\nAmount: {money(cents)}\nNew balance: {money(balance)}", parent=self)
        self.load_customers(self.customer_table, self.customer_search.get())

    def transactions(self, parent):
        bar = tk.Frame(parent, bg=self.t["BG"]); bar.pack(fill="x", pady=(0,12))
        self.label(bar, "Search username or transaction type", 10, self.t["MUTED"]).pack(side="left", padx=(0,8))
        self.transaction_search = tk.StringVar()
        field = self.entry(bar, width=300); field.pack(side="left", fill="x", expand=True); field.entry.configure(textvariable=self.transaction_search)
        field.entry.bind("<KeyRelease>", lambda _e: self.load_transactions(self.transaction_table, self.transaction_search.get()))
        self.button(bar, "Search", lambda: self.load_transactions(self.transaction_table, self.transaction_search.get())).pack(side="left", padx=7)
        self.button(bar, "Show all", self.show_all_transactions).pack(side="left")
        self.transaction_table = self.make_table(parent, (("username","CUSTOMER USERNAME",150),("type","TYPE",100),("amount","AMOUNT (EGP)",125),("date","DATE",170),("balance","BALANCE AFTER (EGP)",165),("performed_by","DONE BY (ADMIN)",145)), height=18)
        self.load_transactions(self.transaction_table, "")

    def show_all_transactions(self):
        self.transaction_search.set(""); self.load_transactions(self.transaction_table, "")

    def load_transactions(self, table, query):
        if not table.winfo_exists(): return
        with connect_db() as con:
            rows = con.execute("SELECT a.Username,t.TransactionType,t.Amount,t.TransactionDate,t.BalanceAfter,COALESCE(t.PerformedBy,'Customer / ATM') FROM Transactions t JOIN Accounts a ON a.AccountID=t.AccountID WHERE a.Username LIKE ? OR a.FullName LIKE ? OR t.TransactionType LIKE ? OR COALESCE(t.PerformedBy,'') LIKE ? ORDER BY t.TransactionID DESC LIMIT 1000", (f"%{query}%",f"%{query}%",f"%{query}%",f"%{query}%")).fetchall()
        self.fill_table(table, [(r[0],r[1],f"{int(r[2])/100:,.2f}",r[3],f"{int(r[4])/100:,.2f}",r[5]) for r in rows])

    def admin_settings(self, parent):
        t = self.t
        add = tk.Frame(parent, bg=t["PANEL"], padx=22, pady=20, highlightthickness=1, highlightbackground=t["BORDER"]); add.pack(fill="x", pady=(0,14))
        self.label(add, "Create another admin account", 15, t["ACCENT"], True).pack(anchor="w", pady=(0,12))
        row = tk.Frame(add, bg=t["PANEL"]); row.pack(fill="x")
        self.label(row, "Username", 10, t["MUTED"]).pack(side="left", padx=(0,6))
        user = self.entry(row, width=190); user.pack(side="left", fill="x", expand=True, padx=(0,12))
        self.label(row, "Temporary password", 10, t["MUTED"]).pack(side="left", padx=(0,6))
        pw = self.entry(row, password=True, width=190); pw.pack(side="left", fill="x", expand=True, padx=(0,12))
        self.button(row, "Create admin", lambda: self.create_admin(user.get(), pw.get()), True).pack(side="left")
        change = tk.Frame(parent, bg=t["PANEL"], padx=22, pady=20, highlightthickness=1, highlightbackground=t["BORDER"]); change.pack(fill="x")
        self.label(change, "Change your password", 15, t["ACCENT"], True).pack(anchor="w", pady=(0,12))
        self.label(change, "Current password", 10, t["MUTED"]).pack(anchor="w")
        old = self.entry(change, password=True); old.pack(fill="x", pady=(3,10))
        self.label(change, "New password (at least 8 characters)", 10, t["MUTED"]).pack(anchor="w")
        new = self.entry(change, password=True); new.pack(fill="x", pady=(3,12))
        self.button(change, "Update password", lambda: self.change_password(old.get(), new.get()), True).pack(anchor="w")
        self.label(parent, "New admin accounts must change their temporary password at first sign-in.", 10, t["MUTED"]).pack(anchor="w", pady=14)

    def create_admin(self, username, password):
        username = username.strip()
        if len(username) < 3 or any(ch.isspace() for ch in username) or len(password) < 8:
            messagebox.showwarning("Check details", "Username must be at least 3 characters with no spaces; password must be at least 8 characters.", parent=self); return
        try:
            with connect_db() as con:
                con.execute("INSERT INTO Admins (Username,PasswordHash,MustChangePassword) VALUES (?,?,1)", (username,hash_password(password)))
        except sqlite3.IntegrityError:
            messagebox.showerror("Username unavailable", "That admin username already exists.", parent=self); return
        messagebox.showinfo("Admin created", f"Admin '{username}' created. They must change the temporary password on first login.", parent=self)

    def change_password(self, old, new):
        if len(new) < 8:
            messagebox.showwarning("Weak password", "New password must be at least 8 characters.", parent=self); return
        with connect_db() as con:
            row = con.execute("SELECT PasswordHash FROM Admins WHERE Username=?", (self.admin,)).fetchone()
            if not row or not verify_password(old, row[0]):
                messagebox.showerror("Incorrect password", "Current password is incorrect.", parent=self); return
            con.execute("UPDATE Admins SET PasswordHash=?, MustChangePassword=0 WHERE Username=?", (hash_password(new), self.admin))
        messagebox.showinfo("Success", "Your admin password has been updated.", parent=self)

    def make_table(self, parent, columns, height=12, initial=None):
        frame = tk.Frame(parent, bg=self.t["BG"]); frame.pack(fill="both", expand=True)
        names = tuple(col[0] for col in columns)
        table = ttk.Treeview(frame, columns=names, show="headings", height=height, selectmode="browse")
        for col, heading, width in columns:
            table.heading(col, text=heading)
            table.column(col, width=width, anchor="w", stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview); table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        if initial is not None: self.fill_table(table, initial)
        return table

    def fill_table(self, table, rows):
        if not table.winfo_exists(): return
        # Keep the selected row selected when the same transaction/customer is refreshed.
        selected_values = None
        selected = table.selection()
        if selected: selected_values = table.item(selected[0], "values")
        for item in table.get_children(): table.delete(item)
        selected_item = None
        for row in rows:
            iid = table.insert("", "end", values=row)
            if selected_values is not None and tuple(str(v) for v in row) == tuple(str(v) for v in selected_values):
                selected_item = iid
        if selected_item:
            table.selection_set(selected_item); table.focus(selected_item)

    def logout(self):
        self.admin = None; self.show_login()


if __name__ == "__main__":
    AdminApp().mainloop()
