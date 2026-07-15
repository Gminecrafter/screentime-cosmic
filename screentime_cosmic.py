#!/usr/bin/env python3
"""ScreenTime for COSMIC - GUI (reader).

This is now a *pure reader*. The actual time tracking + the periodic reminder
run in a separate headless daemon (``screentime_tracker.py``) that autostarts
at login and writes ~/.screentime_cosmic_data.json. This window just displays
that file and lets you change settings; it does not need to be open for time to
be counted.

The daemon writes a ``status`` block (current focused app + a heartbeat), which
we read to show the live green dot and an honest "tracker running / not running"
line.
"""

import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import date, timedelta
from tkinter import ttk

import screentime_core as core
from screentime_core import fmt, get_weekly_data, load_data, update_data

# How often the window re-reads the data file from disk.
REFRESH_INTERVAL_MS = 1000
# If the daemon's heartbeat is older than this, treat it as not running.
DAEMON_STALE_S = 20

UI_SCALE = core.DEFAULT_UI_SCALE


# Theme Definitions (Catppuccin palette)
THEMES = {
    "Mocha": {
        "bg": "#1e1e2e",
        "fg": "#cdd6f4",
        "accent": "#a6e3a1",
        "surface": "#313244",
        "surface_light": "#45475a",
        "selected": "#585b70",
        "warn": "#f38ba8",
    },
    "Macchiato": {
        "bg": "#24273a",
        "fg": "#cad3f5",
        "accent": "#a6e3a1",
        "surface": "#494d64",
        "surface_light": "#5b6078",
        "selected": "#6e738b",
        "warn": "#ed8796",
    },
    "Latte": {
        "bg": "#eff1f5",
        "fg": "#4c4f69",
        "accent": "#40a02b",
        "surface": "#ccd0da",
        "surface_light": "#bcc0cc",
        "selected": "#acb0d0",
        "warn": "#d20f39",
    },
}


class ScreenTimeApp:
    def __init__(self, root):
        self.root = root
        self.data = load_data()
        self._data_mtime = None
        self._last_daemon_spawn = 0.0
        self._spawned_procs = []  # daemon spawns we started, reaped on refresh

        # Guarantee the background tracker is running BEFORE we build the UI, so
        # opening the window is never "paused": if the daemon isn't alive (no
        # autostart, a crash, etc.) we start it ourselves. Its single-instance
        # lock makes this a no-op when one is already running.
        self.ensure_daemon_running()

        # State
        self.current_view = "Today"
        self.theme_name = self.data["settings"].get("theme", "Mocha")
        self.font_name = self.data["settings"].get("font", "JetBrains Mono")
        self.insights_end_date = date.today()

        # Setup Window
        self.root.title("ScreenTime")
        # Thinner but taller: the Insights view stacks a graph + two app
        # columns, so it needs vertical room far more than width.
        self.root.geometry("780x1120")
        self.root.minsize(760, 1040)

        # Window icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
        try:
            if os.path.exists(icon_path):
                self._icon_img = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

        self.apply_global_styles()

        # Main layout container
        self.main_container = tk.Frame(root, bg=self.get_color("bg"))
        self.main_container.pack(fill="both", expand=True)

        # View frames
        self.views = {}
        self.setup_today_view()
        self.setup_insights_view()
        self.setup_settings_view()

        # Bottom Navigation
        self.setup_bottom_nav()

        # Initial view
        self.switch_view("Today")

        # Start the read/refresh loop (no tracking happens here anymore).
        self.refresh_ui()

    # ---------- data ----------

    def _reload_if_changed(self):
        """Re-read the data file if it changed on disk (the daemon writes it).
        Returns True if we reloaded."""
        try:
            mtime = os.path.getmtime(core.DATA_FILE)
        except OSError:
            return False
        if self._data_mtime == mtime:
            return False
        try:
            self.data = load_data()
        except Exception:
            return False
        self._data_mtime = mtime
        return True

    def _status(self):
        st = self.data.get("status")
        return st if isinstance(st, dict) else {}

    def _daemon_alive(self):
        hb = self._status().get("heartbeat")
        try:
            return hb is not None and (time.time() - float(hb)) < DAEMON_STALE_S
        except (TypeError, ValueError):
            return False

    def _current_app(self):
        if self._daemon_alive():
            return self._status().get("current_app") or "Unknown"
        return "Unknown"

    def _tracker_connected(self):
        """True if the daemon's tracker has bound both Wayland globals
        (compositor reachable). False means it's still connecting or the
        compositor isn't available."""
        return bool(self._status().get("connected"))

    def ensure_daemon_running(self):
        """Start the background tracker if it isn't alive. Safe to call as often
        as we like: the daemon takes a single-instance flock, so a redundant
        launch just exits immediately. This is what makes opening the window
        self-healing instead of showing a dead 'paused' state."""
        if self._daemon_alive():
            return
        now = time.time()
        # Throttle: the daemon needs a few seconds to bind the compositor and
        # write its first heartbeat, so don't re-spawn on every 1s refresh.
        if now - self._last_daemon_spawn < 8:
            return
        self._last_daemon_spawn = now
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            script = os.path.join(here, "screentime_tracker.py")
            env = os.environ.copy()
            env["PYTHONPATH"] = here + os.pathsep + env.get("PYTHONPATH", "")
            if not env.get("DBUS_SESSION_BUS_ADDRESS") and env.get("XDG_RUNTIME_DIR"):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
            # start_new_session=True detaches the daemon into its own session so
            # it keeps tracking after this window is closed.
            p = subprocess.Popen(
                [sys.executable, script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            # Keep a handle so refresh_ui can reap it. A spawn that loses the
            # single-instance race exits at once; without reaping it'd linger
            # as a zombie child of this window.
            self._spawned_procs.append(p)
        except Exception:
            pass

    def _persist_settings(self):
        """Write the current theme/font/interval into the shared file WITHOUT
        clobbering the daemon's history/status (merge under lock)."""
        theme = self.theme_name
        font = self.font_name
        interval = core.coerce_interval(self.notif_var.get()) if hasattr(self, "notif_var") else None

        idle_enabled = None
        if hasattr(self, "idle_enabled_var"):
            idle_enabled = bool(self.idle_enabled_var.get())
        idle_timeout = None
        if hasattr(self, "idle_timeout_var"):
            try:
                idle_timeout = core.coerce_interval(
                    self.idle_timeout_var.get(), default=core.DEFAULT_IDLE_TIMEOUT
                )
            except Exception:
                idle_timeout = None

        def mut(data):
            s = data.setdefault("settings", {})
            s["theme"] = theme
            s["font"] = font
            if interval is not None:
                s["notif_interval"] = interval
            if idle_enabled is not None:
                s["idle_enabled"] = idle_enabled
            if idle_timeout is not None:
                s["idle_timeout"] = idle_timeout

        try:
            self.data = update_data(mut)
            try:
                self._data_mtime = os.path.getmtime(core.DATA_FILE)
            except OSError:
                pass
        except Exception:
            pass

    # ---------- settings handlers ----------

    def update_notif_interval(self, event=None):
        # Validate what the user typed/selected and persist it. The daemon
        # re-reads this value and reschedules its reminder automatically; the
        # GUI no longer fires notifications itself.
        try:
            interval = int(self.notif_var.get())
        except (ValueError, AttributeError, TypeError):
            return  # not a number yet; let them keep typing
        if interval < 1:
            return
        self._persist_settings()

    def update_idle_settings(self, event=None):
        # Toggle / retype the idle-pause timeout. The daemon re-reads these and
        # reconfigures the compositor idle notification automatically. Reflect
        # the enabled state on the timeout box's usability.
        try:
            minutes = int(self.idle_timeout_var.get())
        except (ValueError, AttributeError, TypeError):
            return  # mid-typing; wait for a valid number
        if minutes < 1:
            return
        if hasattr(self, "idle_spin"):
            self.idle_spin.config(
                state="normal" if self.idle_enabled_var.get() else "disabled"
            )
        self._persist_settings()

    def get_color(self, key):
        return THEMES[self.theme_name].get(key, "#000000")

    def apply_global_styles(self):
        self.root.configure(bg=self.get_color("bg"))

        style = ttk.Style()
        style.theme_use("clam")

        # Treeview
        style.configure(
            "Treeview",
            background=self.get_color("surface"),
            foreground=self.get_color("fg"),
            fieldbackground=self.get_color("surface"),
            rowheight=36,
            font=(self.font_name, 12)
        )
        style.configure(
            "Treeview.Heading",
            background=self.get_color("surface_light"),
            foreground=self.get_color("fg"),
            font=(self.font_name, 12, "bold")
        )
        style.map("Treeview", background=[("selected", self.get_color("selected"))])

    def setup_bottom_nav(self):
        # If nav_frame exists, destroy it first
        if hasattr(self, 'nav_frame'):
            self.nav_frame.destroy()

        self.nav_frame = tk.Frame(self.root, bg=self.get_color("surface_light"), height=60)
        self.nav_frame.pack(side="bottom", fill="x")

        btn_opts = {
            "font": (self.font_name, 12, "bold"),
            "bg": self.get_color("surface_light"),
            "fg": self.get_color("fg"),
            "activebackground": self.get_color("selected"),
            "activeforeground": self.get_color("fg"),
            "bd": 0,
            "padx": 20,
            "pady": 10,
            "cursor": "hand2"
        }

        # We use a dictionary to hold buttons to potentially update colors later
        self.nav_buttons = {}
        for view in ["Settings", "Today", "Insights"]:
            btn = tk.Button(
                self.nav_frame, text=view,
                command=lambda v=view: self.switch_view(v),
                **btn_opts
            )
            btn.pack(side="left", expand=True, fill="both")
            self.nav_buttons[view] = btn

    def switch_view(self, view_name):
        self.current_view = view_name
        # Hide all views
        for frame in self.views.values():
            frame.pack_forget()

        # Show target view
        self.views[view_name].pack(fill="both", expand=True)

        # Special updates for views
        if view_name == "Insights":
            self.update_insights_view()

        # Update nav button colors
        for name, btn in self.nav_buttons.items():
            if name == view_name:
                btn.configure(bg=self.get_color("selected"))
            else:
                btn.configure(bg=self.get_color("surface_light"))

    def setup_today_view(self):
        frame = self.views.get("Today")
        if frame is None:
            frame = tk.Frame(self.main_container, bg=self.get_color("bg"))
            self.views["Today"] = frame
        else:
            for child in frame.winfo_children():
                child.destroy()

        self.today_header = tk.Label(
            frame, text="📊 Screen Time Today",
            font=(self.font_name, 20, "bold"),
            bg=self.get_color("bg"), fg=self.get_color("fg")
        )
        self.today_header.pack(pady=(20, 5))

        self.total_label = tk.Label(
            frame, text="Total: 0s",
            font=(self.font_name, 14),
            bg=self.get_color("bg"), fg=self.get_color("accent")
        )
        self.total_label.pack(pady=(0, 15))

        self.tree = ttk.Treeview(frame, columns=("time",), show="tree headings", height=14)
        self.tree.heading("#0", text="App")
        self.tree.heading("time", text="Time")
        self.tree.column("#0", width=420)
        self.tree.column("time", width=200, anchor="center")
        self.tree.pack(padx=25, pady=10, fill="both", expand=True)

        self.status = tk.Label(
            frame, text="⏳ Connecting to tracker…",
            font=(self.font_name, 11),
            bg=self.get_color("bg"), fg=self.get_color("fg")
        )
        self.status.pack(pady=15)

    def setup_insights_view(self):
        frame = self.views.get("Insights")
        if frame is None:
            frame = tk.Frame(self.main_container, bg=self.get_color("bg"))
            self.views["Insights"] = frame
        else:
            for child in frame.winfo_children():
                child.destroy()

        # Header
        header = tk.Label(
            frame, text="📈 Usage Insights",
            font=(self.font_name, 20, "bold"),
            bg=self.get_color("bg"), fg=self.get_color("fg")
        )
        header.pack(pady=(20, 10))

        # Week Selector
        selector_frame = tk.Frame(frame, bg=self.get_color("bg"))
        selector_frame.pack(pady=5, fill="x", padx=20)

        # Top row: prev / date range / next, spread across the full width
        nav_row = tk.Frame(selector_frame, bg=self.get_color("bg"))
        nav_row.pack(fill="x")

        self.prev_week_btn = tk.Button(
            nav_row, text="◀ Previous Week",
            command=lambda: self.shift_week(-7),
            font=(self.font_name, 10),
            bg=self.get_color("surface"), fg=self.get_color("fg"),
            activebackground=self.get_color("selected"),
            bd=0, padx=10, pady=5, cursor="hand2"
        )
        self.prev_week_btn.pack(side="left")

        self.date_range_label = tk.Label(
            nav_row, text="",
            font=(self.font_name, 11),
            bg=self.get_color("bg"), fg=self.get_color("fg")
        )
        self.date_range_label.pack(side="left", expand=True)

        self.next_week_btn = tk.Button(
            nav_row, text="Next Week ▶",
            command=lambda: self.shift_week(7),
            font=(self.font_name, 10),
            bg=self.get_color("surface"), fg=self.get_color("fg"),
            activebackground=self.get_color("selected"),
            bd=0, padx=10, pady=5, cursor="hand2"
        )
        self.next_week_btn.pack(side="right")

        # Bottom row: "This Week" jump button, centered on its own line
        self.today_btn = tk.Button(
            selector_frame, text="📍 This Week",
            command=self.go_to_current_week,
            font=(self.font_name, 9),
            bg=self.get_color("surface_light"), fg=self.get_color("fg"),
            activebackground=self.get_color("selected"),
            bd=0, padx=12, pady=4, cursor="hand2"
        )
        self.today_btn.pack(pady=(8, 0))

        # Graph
        self.graph_canvas = tk.Canvas(
            frame, width=750, height=350,
            bg=self.get_color("bg"), highlightthickness=0
        )
        self.graph_canvas.pack(pady=20)

        # Stats
        self.stats_label = tk.Label(
            frame, text="",
            font=(self.font_name, 12),
            bg=self.get_color("bg"), fg=self.get_color("accent")
        )
        self.stats_label.pack(pady=5)

        # Top Apps
        top_apps_frame = tk.Frame(frame, bg=self.get_color("bg"))
        top_apps_frame.pack(pady=20, padx=40, fill="both", expand=True)

        # Column 1: Weekly
        weekly_frame = tk.Frame(top_apps_frame, bg=self.get_color("bg"))
        weekly_frame.pack(side="left", expand=True)
        tk.Label(weekly_frame, text="Top Apps (Week)", font=(self.font_name, 12, "bold"),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(pady=(0, 10))
        self.weekly_top_list = tk.Label(weekly_frame, text="", justify="left",
                                        font=(self.font_name, 11), bg=self.get_color("bg"), fg=self.get_color("fg"))
        self.weekly_top_list.pack()

        # Column 2: All-time
        alltime_frame = tk.Frame(top_apps_frame, bg=self.get_color("bg"))
        alltime_frame.pack(side="left", expand=True)
        tk.Label(alltime_frame, text="Top Apps (All-Time)", font=(self.font_name, 12, "bold"),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(pady=(0, 10))
        self.alltime_top_list = tk.Label(alltime_frame, text="", justify="left",
                                         font=(self.font_name, 11), bg=self.get_color("bg"), fg=self.get_color("fg"))
        self.alltime_top_list.pack()

    def shift_week(self, days):
        # Don't allow navigating into the future — a week that hasn't happened
        # yet just renders as empty bars, which reads like a bug.
        new_end = self.insights_end_date + timedelta(days=days)
        if new_end > date.today():
            new_end = date.today()
        self.insights_end_date = new_end
        self.update_insights_view()

    def go_to_current_week(self):
        self.insights_end_date = date.today()
        self.update_insights_view()

    def update_insights_view(self):
        # 1. Update date range label
        start_date = self.insights_end_date - timedelta(days=6)
        self.date_range_label.config(
            text=f"{start_date} to {self.insights_end_date}"
        )

        # 2. Get data
        weekly_data = get_weekly_data(self.data, self.insights_end_date)
        totals = [total for _, total in weekly_data]
        dates = [d for d, _ in weekly_data]

        # 3. Render Graph
        self.render_insights_graph(dates, totals)

        # 4. Calculate Stats
        if totals:
            avg = sum(totals) / 7
            mx = max(totals)
            self.stats_label.config(text=f"Average: {fmt(int(avg))} | Max: {fmt(mx)}")
        else:
            self.stats_label.config(text="No data for this week")

        # 5. Top Apps (Weekly)
        weekly_apps = {}
        for d, _ in weekly_data:
            day_apps = self.data["history"].get(d, {})
            for app, sec in day_apps.items():
                weekly_apps[app] = weekly_apps.get(app, 0) + sec

        top_weekly = sorted(weekly_apps.items(), key=lambda x: x[1], reverse=True)[:5]
        weekly_text = "\n".join([f"{i+1}. {app}: {fmt(sec)}" for i, (app, sec) in enumerate(top_weekly)])
        self.weekly_top_list.config(text=weekly_text if weekly_text else "No data")

        # 6. Top Apps (All-time)
        all_apps = {}
        for day_apps in self.data["history"].values():
            for app, sec in day_apps.items():
                all_apps[app] = all_apps.get(app, 0) + sec

        top_all = sorted(all_apps.items(), key=lambda x: x[1], reverse=True)[:5]
        alltime_text = "\n".join([f"{i+1}. {app}: {fmt(sec)}" for i, (app, sec) in enumerate(top_all)])
        self.alltime_top_list.config(text=alltime_text if alltime_text else "No data")

    def render_insights_graph(self, dates, totals):
        self.graph_canvas.delete("all")

        if not totals:
            return

        # Canvas setup
        width = self.graph_canvas.winfo_width() if self.graph_canvas.winfo_width() > 1 else 750
        height = self.graph_canvas.winfo_height() if self.graph_canvas.winfo_height() > 1 else 350
        padding = 50
        graph_width = width - 2 * padding
        graph_height = height - 2 * padding

        max_val = max(totals) if max(totals) > 0 else 1
        bar_width = graph_width / 7

        for i, (date_str, total) in enumerate(zip(dates, totals)):
            # Calculate bar height
            bar_h = (total / max_val) * graph_height

            # X position
            x0 = padding + i * bar_width + 10
            x1 = x0 + bar_width - 20
            # Y position (bottom up)
            y0 = height - padding - bar_h
            y1 = height - padding

            # Draw bar
            self.graph_canvas.create_rectangle(
                x0, y0, x1, y1,
                fill=self.get_color("accent"), outline=self.get_color("accent")
            )

            # Draw date label (bottom)
            date_short = date_str[-5:] # MM-DD
            self.graph_canvas.create_text(
                (x0 + x1) / 2, height - padding + 20,
                text=date_short, fill=self.get_color("fg"),
                font=(self.font_name, 10)
            )

            # Draw time label (top)
            hours = total // 3600
            mins = (total % 3600) // 60
            time_str = f"{hours:02d}:{mins:02d}"

            self.graph_canvas.create_text(
                (x0 + x1) / 2, y0 - 20,
                text=time_str, fill=self.get_color("fg"),
                font=(self.font_name, 11, "bold")
            )

    def setup_settings_view(self):
        # If the settings frame already exists, destroy its contents so we can
        # rebuild cleanly (used when theme/font changes). We keep the frame
        # itself so switch_view's pack state isn't disturbed.
        frame = self.views.get("Settings")
        if frame is None:
            frame = tk.Frame(self.main_container, bg=self.get_color("bg"))
            self.views["Settings"] = frame
        else:
            for child in frame.winfo_children():
                child.destroy()

        header = tk.Label(
            frame, text="⚙️ Settings",
            font=(self.font_name, 20, "bold"),
            bg=self.get_color("bg"), fg=self.get_color("fg")
        )
        header.pack(pady=(20, 20))

        # Theme Selection
        theme_frame = tk.Frame(frame, bg=self.get_color("bg"))
        theme_frame.pack(pady=10, padx=40, fill="x")

        tk.Label(theme_frame, text="App Theme", font=(self.font_name, 12, "bold"),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(side="left")

        self.theme_var = tk.StringVar(value=self.theme_name)
        theme_menu = ttk.Combobox(
            theme_frame, textvariable=self.theme_var,
            values=list(THEMES.keys()), state="readonly",
            font=(self.font_name, 11)
        )
        theme_menu.pack(side="right")
        theme_menu.bind("<<ComboboxSelected>>", self.update_theme)

        # Font Selection
        font_frame = tk.Frame(frame, bg=self.get_color("bg"))
        font_frame.pack(pady=10, padx=40, fill="x")

        tk.Label(font_frame, text="App Font", font=(self.font_name, 12, "bold"),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(side="left")

        # Common system fonts as fallbacks
        available_fonts = ["JetBrains Mono", "Monospace", "DejaVu Sans Mono", "Arial", "Helvetica"]
        self.font_var = tk.StringVar(value=self.font_name)
        font_menu = ttk.Combobox(
            font_frame, textvariable=self.font_var,
            values=available_fonts, state="readonly",
            font=(self.font_name, 11)
        )
        font_menu.pack(side="right")
        font_menu.bind("<<ComboboxSelected>>", self.update_font)

        # Notification interval (custom, in minutes)
        notif_frame = tk.Frame(frame, bg=self.get_color("bg"))
        notif_frame.pack(pady=10, padx=40, fill="x")

        tk.Label(notif_frame, text="Notification Interval (min)",
                 font=(self.font_name, 12, "bold"),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(side="left")

        current_interval = core.coerce_interval(self.data["settings"].get("notif_interval"))
        self.notif_var = tk.StringVar(value=str(current_interval))

        # Spinbox lets the user type any positive integer (custom interval)
        # instead of picking from a fixed list. `from_=1/to=10080` covers
        # 1 minute up to a week; wrap=False keeps typing sane.
        self.notif_spin = tk.Spinbox(
            notif_frame, from_=1, to=10080, increment=5,
            textvariable=self.notif_var, width=6,
            font=(self.font_name, 11),
            command=self.update_notif_interval,
        )
        self.notif_spin.pack(side="right")
        # Validate on manual typing + focus-out so a typed custom value is
        # applied even without pressing the arrows.
        self.notif_spin.bind("<FocusOut>", self.update_notif_interval)
        self.notif_spin.bind("<Return>", self.update_notif_interval)

        # Idle auto-pause: stop the timer when there's no keyboard/mouse input.
        idle_settings = self.data["settings"]
        idle_frame = tk.Frame(frame, bg=self.get_color("bg"))
        idle_frame.pack(pady=10, padx=40, fill="x")

        tk.Label(idle_frame, text="Pause when idle",
                 font=(self.font_name, 12, "bold"),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(side="left")

        self.idle_enabled_var = tk.BooleanVar(
            value=bool(idle_settings.get("idle_enabled", True))
        )
        idle_check = tk.Checkbutton(
            idle_frame, variable=self.idle_enabled_var,
            command=self.update_idle_settings,
            bg=self.get_color("bg"), fg=self.get_color("fg"),
            activebackground=self.get_color("bg"),
            selectcolor=self.get_color("surface"),
            bd=0, highlightthickness=0, cursor="hand2",
        )
        idle_check.pack(side="right")

        # Idle timeout in minutes (freely typeable).
        idle_to_frame = tk.Frame(frame, bg=self.get_color("bg"))
        idle_to_frame.pack(pady=(0, 10), padx=40, fill="x")

        tk.Label(idle_to_frame, text="Idle timeout (min)",
                 font=(self.font_name, 11),
                 bg=self.get_color("bg"), fg=self.get_color("fg")).pack(side="left")

        current_idle = core.coerce_interval(
            idle_settings.get("idle_timeout"), default=core.DEFAULT_IDLE_TIMEOUT
        )
        self.idle_timeout_var = tk.StringVar(value=str(current_idle))
        self.idle_spin = tk.Spinbox(
            idle_to_frame, from_=1, to=1440, increment=1,
            textvariable=self.idle_timeout_var, width=6,
            font=(self.font_name, 11),
            command=self.update_idle_settings,
            state="normal" if self.idle_enabled_var.get() else "disabled",
        )
        self.idle_spin.pack(side="right")
        self.idle_spin.bind("<FocusOut>", self.update_idle_settings)
        self.idle_spin.bind("<Return>", self.update_idle_settings)

        # A note that tracking is independent of this window.
        tk.Label(
            frame,
            text="Time is tracked in the background even when this window is closed.",
            font=(self.font_name, 9),
            bg=self.get_color("bg"), fg=self.get_color("fg"), wraplength=700,
        ).pack(pady=(24, 0))

    def update_theme(self, event=None):
        self.theme_name = self.theme_var.get()
        self._persist_settings()
        self.apply_global_styles()
        self.rebuild_views()

    def update_font(self, event=None):
        self.font_name = self.font_var.get()
        self._persist_settings()
        self.apply_global_styles()
        self.rebuild_views()

    def rebuild_views(self):
        """Recreate all view frames in place and re-show the current view."""
        # Update nav button colors
        for name, btn in self.nav_buttons.items():
            btn.configure(
                bg=self.get_color("selected") if name == self.current_view
                else self.get_color("surface_light"),
                fg=self.get_color("fg"),
                activebackground=self.get_color("selected"),
                activeforeground=self.get_color("fg"),
            )
        self.nav_frame.configure(bg=self.get_color("surface_light"))

        self.setup_today_view()
        self.setup_insights_view()
        self.setup_settings_view()
        # Re-show the current view (rebuild may have created a fresh frame).
        self.switch_view(self.current_view)

    def refresh_ui(self):
        # The reschedule must be unconditional: if a widget call raises (e.g.
        # mid theme/font rebuild) we still keep refreshing next second.
        try:
            # Reap any finished daemon spawns so they don't linger as zombies.
            if self._spawned_procs:
                self._spawned_procs = [p for p in self._spawned_procs if p.poll() is None]

            # Self-heal: if the tracker died while we're open, bring it back.
            self.ensure_daemon_running()

            reloaded = self._reload_if_changed()

            # Keep the on-screen theme/font in sync if another process (or a
            # previous session) changed settings on disk.
            if reloaded:
                if self.current_view == "Insights":
                    self.update_insights_view()

            # Today view
            if hasattr(self, "tree") and self.tree.winfo_exists():
                self.tree.delete(*self.tree.get_children())

                today = str(date.today())
                day_apps = self.data.get("history", {}).get(today, {})

                sorted_apps = sorted(day_apps.items(), key=lambda x: x[1], reverse=True)
                total = sum(day_apps.values())
                if hasattr(self, "total_label") and self.total_label.winfo_exists():
                    self.total_label.config(text=f"Total: {fmt(total)}")

                current = self._current_app()
                for app, seconds in sorted_apps:
                    if app == "Unknown":
                        continue
                    label = f"🟢 {app}" if app == current and current != "Unknown" else app
                    self.tree.insert("", "end", text=label, values=(fmt(seconds),))

            # Status line: honest about whether the background daemon is running.
            if hasattr(self, "status") and self.status.winfo_exists():
                st = self._status()
                if not self._daemon_alive():
                    # ensure_daemon_running() has just (re)launched it; it needs
                    # a few seconds to bind the compositor and heartbeat.
                    self.status.config(
                        text="⏳ Starting background tracker…",
                        fg=self.get_color("fg"),
                    )
                elif not self._tracker_connected():
                    # Daemon is alive but the tracker hasn't bound both
                    # Wayland globals yet — either still connecting or the
                    # compositor hasn't advertised them.
                    self.status.config(
                        text="⏳ Connecting to COSMIC compositor…",
                        fg=self.get_color("fg"),
                    )
                elif st.get("locked"):
                    self.status.config(
                        text="🔒 Screen locked — tracking paused",
                        fg=self.get_color("fg"),
                    )
                elif st.get("idle"):
                    self.status.config(
                        text="😴 Idle — tracking paused (no input)",
                        fg=self.get_color("fg"),
                    )
                elif st.get("current_app") in (None, "", "Unknown"):
                    # Connected but no window is focused — on COSMIC this is
                    # what a locked or idle screen looks like, or simply no
                    # apps are open. We are connected, just not crediting.
                    self.status.config(
                        text="🟢 Connected — no active window",
                        fg=self.get_color("fg"),
                    )
                else:
                    self.status.config(
                        text="🟢 Tracking via COSMIC protocol (background)",
                        fg=self.get_color("accent"),
                    )
        except Exception:
            pass
        finally:
            self.root.after(REFRESH_INTERVAL_MS, self.refresh_ui)

    def on_close(self):
        # Nothing to flush — tracking lives in the daemon. Just close the window.
        self.root.destroy()


if __name__ == "__main__":
    # className fixes the "Tk" label in COSMIC's dock/window-switcher
    root = tk.Tk(className="ScreenTime")

    _settings = load_data().get("settings", {})
    try:
        _scale = float(_settings.get("ui_scale", UI_SCALE)) or UI_SCALE
    except (TypeError, ValueError):
        _scale = UI_SCALE
    root.tk.call("tk", "scaling", _scale)  # fixes tiny UI on high-res screens

    app = ScreenTimeApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
