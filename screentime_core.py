#!/usr/bin/env python3
"""Shared core for ScreenTime (COSMIC).

This module is deliberately *tkinter-free* so it can be imported by both the
headless tracker daemon (``screentime_tracker.py``) and the reader GUI
(``screentime_cosmic.py``).

It owns:
  - the on-disk data format + safe, concurrent-friendly read/modify/write
    (``update_data`` takes an inter-process file lock and re-reads before
    writing, so the daemon's history writes and the GUI's settings writes can
    never clobber each other);
  - the Wayland ``WaylandTracker`` that talks to the COSMIC compositor;
  - small formatting/aggregation helpers used by the GUI;
  - the desktop notification helper.

Data file schema
----------------
{
  "history":  { "YYYY-MM-DD": { "AppName": <int seconds>, ... }, ... },
  "settings": { "theme", "font", "ui_scale", "notif_interval" },
  "status":   { "current_app", "tracking", "pid", "heartbeat" }   # written by daemon
}
"""

import contextlib
import fcntl
import json
import os
import shutil
import struct
import subprocess
import threading
import time

from pywayland.client import Display

from cosmic_protocol.ext_foreign_toplevel_list_v1.ext_foreign_toplevel_list_v1 import (
    ExtForeignToplevelListV1,
)
from cosmic_protocol.cosmic_toplevel_info_unstable_v1.zcosmic_toplevel_info_v1 import (
    ZcosmicToplevelInfoV1,
)
from cosmic_protocol.cosmic_toplevel_info_unstable_v1.zcosmic_toplevel_handle_v1 import (
    ZcosmicToplevelHandleV1,
)

# Idle detection uses the standard ext-idle-notifier protocol (COSMIC supports
# it). get_idle_notification honours idle inhibitors, so a playing video keeps
# the session 'active' and we keep counting while you watch — we only pause on
# genuine no-input idle. These ship with pywayland.
from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
from pywayland.protocol.wayland import WlSeat

# The data file location can be overridden (handy for tests) without touching
# the user's real history.
DATA_FILE = os.environ.get("SCREENTIME_DATA_FILE") or os.path.expanduser(
    "~/.screentime_cosmic_data.json"
)
LOCK_FILE = DATA_FILE + ".lock"

ACTIVATED = ZcosmicToplevelHandleV1.state.activated.value  # = 2

# A poll gap larger than this (seconds) means the machine was likely asleep /
# suspended, so we don't credit the missing time to any app.
SLEEP_GAP_THRESHOLD = 10

# Defaults used when a fresh data file / missing settings key is encountered.
DEFAULT_UI_SCALE = 2.0
DEFAULT_NOTIF_INTERVAL = 30  # minutes
DEFAULT_IDLE_ENABLED = True
DEFAULT_IDLE_TIMEOUT = 5  # minutes of no keyboard/mouse input before pausing

# Cap version we bind so behaviour matches the code paths below (v2 of
# zcosmic_toplevel_info uses get_cosmic_toplevel + done).
COSMIC_TOPLEVEL_INFO_VERSION = 2
FOREIGN_TOPLEVEL_LIST_VERSION = 1

# notify-send icon (best effort).
_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")


def _default_settings():
    return {
        "theme": "Mocha",
        "font": "JetBrains Mono",
        "ui_scale": DEFAULT_UI_SCALE,
        "notif_interval": DEFAULT_NOTIF_INTERVAL,
        "idle_enabled": DEFAULT_IDLE_ENABLED,
        "idle_timeout": DEFAULT_IDLE_TIMEOUT,
    }


def coerce_interval(value, default=DEFAULT_NOTIF_INTERVAL):
    """Return a valid positive-integer minute interval, or ``default``."""
    try:
        value = int(value)
    except (ValueError, TypeError):
        return default
    return value if value >= 1 else default


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #


def load_data():
    """Read and normalise the data file. Never locks (a plain read is safe
    because writes are atomic via os.replace). Returns a dict that always has
    ``history`` and ``settings`` keys; any ``status`` block is preserved."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable data file - start fresh rather than crashing.
            # The old file is left in place so the user can recover it manually.
            data = {}

        if not isinstance(data, dict):
            data = {}

        # Migration: Old schema had "date" and "apps" at root.
        if "date" in data and "history" not in data:
            old_date = data["date"]
            old_apps = data.get("apps", {})
            data = {"history": {old_date: old_apps}, "settings": _default_settings()}

        # Ensure settings exist and have all expected keys.
        base_settings = _default_settings()
        existing = data.get("settings", {})
        if isinstance(existing, dict):
            base_settings.update({k: v for k, v in existing.items() if v is not None})
        data["settings"] = base_settings

        # Ensure history is a dict of date -> {app: seconds}.
        if not isinstance(data.get("history"), dict):
            data["history"] = {}
        else:
            # Prune any day entry that isn't a dict of app->seconds (corrupted
            # by a crash, manual edit, etc.) so get_daily_total etc. never blow
            # up with AttributeError on .values().
            data["history"] = {
                d: v for d, v in data["history"].items()
                if isinstance(v, dict)
            }

        return data

    return {"history": {}, "settings": _default_settings()}


def _write_data(data):
    """Atomic write: temp file in the same dir, then rename. Prevents a
    half-written file (crash / power loss mid-save) from corrupting history."""
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    except OSError:
        # Saving is best-effort; never crash the caller because disk failed.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# Kept for backwards compatibility / full-dict writes.
save_data = _write_data


@contextlib.contextmanager
def _file_lock():
    """Exclusive inter-process lock around a read/modify/write of the data
    file. Both the daemon (history) and the GUI (settings) go through this so
    their writes serialise instead of racing and clobbering each other."""
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def update_data(mutator):
    """Load the freshest data under an exclusive lock, apply ``mutator(data)``
    in place, write it back atomically, and return the resulting dict.

    This is the ONLY safe way for the daemon and the GUI to modify the shared
    file concurrently: because we re-read inside the lock, the daemon's history
    increments never overwrite the GUI's settings changes and vice-versa."""
    with _file_lock():
        data = load_data()
        mutator(data)
        _write_data(data)
        return data


# --------------------------------------------------------------------------- #
#  Aggregation / formatting helpers (used by the GUI)
# --------------------------------------------------------------------------- #


def parse_states(states):
    """Parse a Wayland ``wl_array`` of toplevel states into a set of ints.

    pywayland hands us the array as raw ``bytes`` (one little-endian uint32 per
    state). Doing ``ACTIVATED in states`` directly would do a *byte substring*
    search, not an element search - so e.g. a state value of 0x0201 contains
    byte 0x02 and would falsely read as "activated". We must unpack the uint32s
    instead.
    """
    if states is None:
        return set()
    if isinstance(states, (bytes, bytearray)):
        n = len(states) // 4
        if n * 4 != len(states):
            return set()
        if n == 0:
            return set()
        return set(struct.unpack_from("<%dI" % n, states))
    try:
        return set(int(s) for s in states)
    except TypeError:
        return set()


def get_daily_total(data, target_date):
    apps = data.get("history", {}).get(target_date, {})
    return sum(apps.values())


def get_weekly_data(data, end_date):
    """Returns a list of (date_str, total_seconds) for the 7 days ending on
    end_date, oldest first."""
    from datetime import timedelta

    results = []
    current = end_date
    for _ in range(7):
        date_str = str(current)
        results.append((date_str, get_daily_total(data, date_str)))
        current -= timedelta(days=1)
    return results[::-1]


def fmt(seconds) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --------------------------------------------------------------------------- #
#  Notifications
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
#  Session lock state
# --------------------------------------------------------------------------- #

_LOGINCTL = shutil.which("loginctl")
_SESSION_ID = os.environ.get("XDG_SESSION_ID") or ""
_PACTL = shutil.which("pactl")
_PLAYERCTL = shutil.which("playerctl")
# Always-on / dummy audio streams that don't mean "the user is watching
# something" (e.g. the speech-dispatcher accessibility stream).
_AUDIO_IGNORE = ("speech-dispatcher",)


def media_playing():
    """Best-effort: True if the user is actively watching/listening to
    something with SOUND — an MPRIS player that's Playing, or a real (non-corked,
    non-dummy) audio stream. Lets us keep counting while a video/music plays
    even with no keyboard/mouse input. A muted video produces no audio, so it
    can't be detected this way. Returns False on any uncertainty."""
    # MPRIS: any player actively Playing?
    if _PLAYERCTL:
        try:
            r = subprocess.run([_PLAYERCTL, "-a", "status"],
                               capture_output=True, text=True, timeout=2)
            if any(ln.strip() == "Playing" for ln in r.stdout.splitlines()):
                return True
        except Exception:
            pass
    # Audio: any running (non-corked) sink-input from a real app?
    if _PACTL:
        try:
            r = subprocess.run([_PACTL, "list", "sink-inputs"],
                               capture_output=True, text=True, timeout=2)
            for block in r.stdout.split("Sink Input #")[1:]:
                if "Corked: yes" in block:
                    continue
                low = block.lower()
                if any(ig in low for ig in _AUDIO_IGNORE):
                    continue
                return True
        except Exception:
            pass
    return False


def session_locked():
    """Best-effort: True if the logind graphical session is locked
    (``LockedHint=yes``). Returns False whenever we can't tell — uncertainty
    must never block time tracking. Used so we stop crediting time while the
    screen is locked (the compositor keeps the last window 'activated' across a
    lock, so focus state alone can't tell us the user stepped away)."""
    if not (_LOGINCTL and _SESSION_ID):
        return False
    try:
        r = subprocess.run(
            [_LOGINCTL, "show-session", _SESSION_ID, "-p", "LockedHint", "--value"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return False
    return r.stdout.strip() == "yes"


def send_notification(message, title="ScreenTime"):
    """Fire a desktop notification. Robust to the minimal environment that
    COSMIC autostart gives us: notify-send needs DBUS_SESSION_BUS_ADDRESS to
    reach the daemon, which is sometimes missing, so we fall back to the
    standard session-bus path."""
    env = os.environ.copy()
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and env.get("XDG_RUNTIME_DIR"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"

    args = ["notify-send", "-a", title]
    if os.path.exists(_ICON):
        args += ["-i", _ICON]
    args += [title, message]

    try:
        subprocess.Popen(
            args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        # Last resort: try the subprocess one more time without the icon arg
        # so a missing icon file doesn't block the notification.
        try:
            subprocess.Popen(
                ["notify-send", "-a", title, title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Wayland tracker
# --------------------------------------------------------------------------- #


class WaylandTracker:
    """Tracks which app is currently focused by talking directly to the
    COSMIC compositor over two Wayland protocols:

      - ext_foreign_toplevel_list_v1 : per-window app_id / title / lifecycle
      - zcosmic_toplevel_info_v1      : COSMIC-specific, gives `activated` state

    Design / fool-proofing notes
    ---------------------------
    - The compositor connection lives ENTIRELY on the tracker thread. No
      libwayland object is ever touched from another thread; callers only read
      `get_current_app()` / `is_tracking()` through a lock.
    - Focus is NOT cached. Each toplevel stores its own `activated` flag as the
      compositor reports it; `current_app` is computed lazily by scanning the
      toplevel set on read. This avoids the old drift bugs where a mirrored
      `current_app` got stuck on a stale app after focus moved.
    - State arrays are unpacked as uint32 lists (NOT byte-substring matched,
      which produced phantom focus on values containing the 0x02 byte).
    - Every dispatcher callback is wrapped in try/except and returns 0, so one
      malformed event can never kill the dispatcher (which would silently halt
      all tracking).
    - A single connect loop retries until BOTH globals are bound. The
      compositor can be reachable on the socket before it advertises the
      toplevel globals (classic autostart race); we detect that and retry
      instead of sitting on a dead connection forever.
    - `done`/`finished`/both `closed` events are all handled so we never
      reference a toplevel the compositor has already torn down.
    - On a real compositor death (socket EOF), `select` returns ready,
      `roundtrip()` raises, and we reconnect after a short backoff.
    """

    # Backoff between (re)connect attempts.
    RECONNECT_BACKOFF_S = 2
    # How long `select` waits for an event before looping back to check the
    # stop flag and retry.
    DISPATCH_TIMEOUT_S = 2

    def __init__(self):
        # toplevel handle -> {"app_id", "title", "activated", "cosmic_handle"}
        self._toplevels = {}
        self.lock = threading.Lock()
        self.running = True
        self._stop_event = threading.Event()
        # Set by request_reconnect() to force a fresh compositor connection.
        self._reconnect_requested = threading.Event()

        # All pywayland objects below live ONLY on the tracker thread.
        self._display = None
        self._toplevel_info_proxy = None
        self._toplevel_list_proxy = None

        # Idle detection (ext-idle-notifier). _desired_idle_ms is set from the
        # daemon thread (under self.lock); everything else is tracker-thread.
        # 0 = idle detection disabled.
        self._idle_notifier_proxy = None
        self._seat_proxy = None
        self._idle_notification = None
        self._desired_idle_ms = 0
        self._active_idle_ms = None  # what's applied on the current connection
        self._idle = False

        # Connection-state flags, only mutated on the tracker thread (read by
        # callers under self.lock where it matters).
        self._bound_toplevel_list = False
        self._bound_toplevel_info = False
        # Bumped on each successful connect, so callers can show a live status.
        self._connect_generation = 0

    # ---------- public API (any thread) ----------

    def get_current_app(self):
        """Name of the focused app, or 'Unknown' if nothing is focused / we
        aren't tracking yet. Computed fresh on every call from toplevel state
        so it can never drift from the compositor's truth."""
        with self.lock:
            for info in self._toplevels.values():
                if info.get("activated"):
                    return self._identify(info)
            return "Unknown"

    def is_connected(self):
        """True iff both Wayland globals are bound (compositor reachable),
        regardless of whether any toplevel windows exist."""
        with self.lock:
            return self._bound_toplevel_list and self._bound_toplevel_info

    def is_tracking(self):
        """True iff we are connected, both protocols are bound, AND we have
        seen at least one live toplevel."""
        with self.lock:
            return (
                self._bound_toplevel_list
                and self._bound_toplevel_info
                and bool(self._toplevels)
            )

    def set_idle_timeout_ms(self, ms):
        """Configure idle detection: pause after `ms` of no keyboard/mouse
        input. 0 (or falsy) disables it. Safe to call from any thread; the
        actual (re)configuration happens on the tracker thread."""
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            ms = 0
        with self.lock:
            self._desired_idle_ms = ms if ms > 0 else 0

    def is_idle(self):
        """True iff idle detection is on AND the compositor reports the user
        has been idle (no input) past the configured timeout."""
        with self.lock:
            return self._idle

    def stop(self):
        self.running = False
        self._stop_event.set()

    def request_reconnect(self):
        """Ask the tracker thread to drop and rebuild its compositor connection.

        Needed because pywayland's dispatcher can silently wedge after heavy
        window churn (it raises 'Cannot find display' inside the cffi callback,
        drops the event, and stops delivering further toplevel/focus updates —
        but roundtrip() never raises, so we can't detect it by connection error
        alone). A fresh connection re-enumerates all toplevels and resyncs."""
        self._reconnect_requested.set()

    # ---------- connection (tracker thread) ----------

    def _connect(self):
        """Connect, bind both protocols, and return True only once both globals
        are bound. Returns False (and cleans up) if the compositor hasn't
        advertised the toplevel globals yet — caller retries."""
        self._display = Display()
        self._display.connect()
        registry = self._display.get_registry()
        registry.dispatcher["global"] = self._on_global
        # Drive a couple of roundtrips: the first may bind only one of the two
        # globals if the compositor advertises them in separate batches.
        for _ in range(3):
            self._display.roundtrip()
            if self._bound_toplevel_list and self._bound_toplevel_info:
                break
        if not (self._bound_toplevel_list and self._bound_toplevel_info):
            # Globals not ready yet — drop and let the caller retry.
            self._hard_reset()
            return False
        # One more roundtrip to receive the initial toplevel list that the
        # compositor sends after processing our bind requests. Without this,
        # the initial toplevels sit in the socket buffer but select() never
        # signals them, leaving _toplevels empty and is_tracking() stuck on
        # False.
        self._display.roundtrip()
        self._connect_generation += 1
        return True

    def _on_global(self, registry, id_, interface, version):
        if interface == "ext_foreign_toplevel_list_v1":
            v = min(version, FOREIGN_TOPLEVEL_LIST_VERSION)
            proxy = registry.bind(id_, ExtForeignToplevelListV1, v)
            proxy.dispatcher["toplevel"] = self._on_toplevel
            # Compositor is tearing down the toplevel list (restart/shutdown).
            # Trigger an immediate reconnect instead of waiting for the next
            # roundtrip to fail with a dead-connection error.
            proxy.dispatcher["finished"] = lambda *_: self.request_reconnect()
            self._toplevel_list_proxy = proxy
            with self.lock:
                self._bound_toplevel_list = True
        elif interface == "zcosmic_toplevel_info_v1":
            v = min(version, COSMIC_TOPLEVEL_INFO_VERSION)
            proxy = registry.bind(id_, ZcosmicToplevelInfoV1, v)
            proxy.dispatcher["finished"] = lambda *_: self.request_reconnect()
            self._toplevel_info_proxy = proxy
            with self.lock:
                self._bound_toplevel_info = True
        elif interface == "ext_idle_notifier_v1":
            # Optional: if absent we simply never pause on idle.
            self._idle_notifier_proxy = registry.bind(id_, ExtIdleNotifierV1, 1)
        elif interface == "wl_seat":
            # Needed as the input source for the idle notification. We don't
            # use any seat events, so binding v1 is enough.
            self._seat_proxy = registry.bind(id_, WlSeat, 1)
        return 0

    # ---------- toplevel lifecycle (tracker thread) ----------

    def _on_toplevel(self, toplevel_list, handle):
        """A new window appeared. Track it and wire up its event dispatchers."""
        info = {"app_id": "", "title": "", "activated": False, "cosmic_handle": None}
        with self.lock:
            self._toplevels[handle] = info

        handle.dispatcher["app_id"] = lambda h, app_id: self._safe(self._set_app_id, info, app_id)
        handle.dispatcher["title"] = lambda h, title: self._safe(self._set_title, info, title)
        handle.dispatcher["closed"] = lambda h: self._safe(self._handle_closed, handle)
        handle.dispatcher["done"] = lambda h: 0  # batch boundary; state already stored

        # Bind the COSMIC-specific handle for focus (activated) state.
        if self._toplevel_info_proxy is not None:
            try:
                cosmic_handle = self._toplevel_info_proxy.get_cosmic_toplevel(handle)
                info["cosmic_handle"] = cosmic_handle
                cosmic_handle.dispatcher["state"] = lambda h, states: self._safe(self._on_state, info, states)
                cosmic_handle.dispatcher["done"] = lambda h: 0
                cosmic_handle.dispatcher["closed"] = lambda h: self._safe(self._handle_closed, handle)
            except Exception:
                # If the COSMIC info bind fails we can still track app_id/title
                # from the foreign toplevel list; we just won't get focus state.
                pass
        return 0

    def _safe(self, fn, *args):
        """Run a dispatcher callback, swallowing any error so a single bad
        event can never kill the dispatcher. Always returns 0 for CFFI."""
        try:
            fn(*args)
        except Exception:
            pass
        return 0

    def _set_app_id(self, info, app_id):
        with self.lock:
            info["app_id"] = app_id or ""
        return 0

    def _set_title(self, info, title):
        with self.lock:
            info["title"] = title or ""
        return 0

    def _on_state(self, info, states):
        # parse_states unpacks the wl_array as uint32 values. The old code did
        # `ACTIVATED in states`, a byte-substring search that produced false
        # positives (any state value containing the byte 0x02 looked activated).
        is_activated = ACTIVATED in parse_states(states)
        with self.lock:
            info["activated"] = is_activated
        return 0

    def _handle_closed(self, handle):
        # Window gone — drop our record. current_app is computed lazily, so we
        # don't need to recompute it here.
        with self.lock:
            self._toplevels.pop(handle, None)
        return 0

    # ---------- idle detection (tracker thread) ----------

    def _set_idle(self, value):
        with self.lock:
            self._idle = bool(value)
        return 0

    def _sync_idle_notification(self):
        """(Re)build the idle notification so it matches the desired timeout.
        Tracker-thread only. Cheap no-op when nothing changed."""
        with self.lock:
            desired = self._desired_idle_ms
        if desired == self._active_idle_ms and (
            desired == 0 or self._idle_notification is not None
        ):
            return

        # Tear down any existing notification (timeout changed / disabled).
        if self._idle_notification is not None:
            try:
                self._idle_notification.destroy()
            except Exception:
                pass
            self._idle_notification = None

        # Build a new one if enabled and the globals are available. The
        # compositor sends `idled` immediately if we're already past the
        # timeout, and `resumed` on the next input.
        if (desired > 0 and self._idle_notifier_proxy is not None
                and self._seat_proxy is not None):
            try:
                notif = self._idle_notifier_proxy.get_idle_notification(
                    desired, self._seat_proxy
                )
                notif.dispatcher["idled"] = lambda *_: self._safe(self._set_idle, True)
                notif.dispatcher["resumed"] = lambda *_: self._safe(self._set_idle, False)
                self._idle_notification = notif
            except Exception:
                self._idle_notification = None

        self._active_idle_ms = desired
        if self._idle_notification is None:
            self._set_idle(False)

    # ---------- main loop (tracker thread) ----------

    def run(self):
        while self.running and not self._stop_event.is_set():
            try:
                if self._display is None:
                    if not self._connect():
                        # Globals not ready yet (autostart race). Back off and
                        # retry.
                        self._sleep_or_stop(self.RECONNECT_BACKOFF_S)
                    continue

                # Caller asked for a fresh connection (recovering from a wedged
                # dispatcher). Drop everything; the next loop reconnects.
                if self._reconnect_requested.is_set():
                    self._reconnect_requested.clear()
                    self._hard_reset()
                    continue

                # Apply any pending idle-timeout change (create/replace/destroy
                # the idle notification). Cheap no-op when unchanged.
                self._sync_idle_notification()

                # Watch the fd with a short timeout so we wake up to check the
                # stop flag even when the compositor is quiet.
                import select as _select

                fd = self._display.get_fd()
                ready, _, _ = _select.select([fd], [], [], self.DISPATCH_TIMEOUT_S)

                if ready:
                    # roundtrip() both READS events from the fd AND dispatches
                    # them. pywayland's dispatch(block=False) only drains an
                    # already-read queue, so it would never pick up freshly
                    # arrived toplevel events and tracking would silently never
                    # start. roundtrip also forces a compositor reply, so a dead
                    # connection raises here and we reconnect.
                    self._display.roundtrip()
                try:
                    self._display.flush()
                except Exception:
                    pass
            except Exception:
                # Broken connection / silent death: drop everything and try to
                # reconnect after a short backoff. Handles compositor restarts
                # and wake-from-suspend races where the socket was closed.
                self._hard_reset()
                self._sleep_or_stop(self.RECONNECT_BACKOFF_S)

    def _hard_reset(self):
        with self.lock:
            self._toplevels.clear()
            self._idle = False
        try:
            if self._display is not None:
                self._display.disconnect()
        except Exception:
            pass
        self._display = None
        self._toplevel_info_proxy = None
        self._toplevel_list_proxy = None
        self._bound_toplevel_list = False
        self._bound_toplevel_info = False
        # Drop idle objects too; the notification is rebuilt after reconnect.
        self._idle_notifier_proxy = None
        self._seat_proxy = None
        self._idle_notification = None
        self._active_idle_ms = None

    def _sleep_or_stop(self, seconds):
        """Sleep up to `seconds`, but return early if stop() is requested."""
        self._stop_event.wait(seconds)

    # ---------- app naming ----------

    def _identify(self, info):
        app_id = info.get("app_id", "") or ""
        title = info.get("title", "") or ""

        combined = (app_id + " " + title).lower()
        if "firefox" in combined:
            return "Firefox"
        if "chrome" in combined or "chromium" in combined:
            return "Chrome"

        raw_id = app_id or title or "Unknown"
        return self._clean_name(raw_id)

    def _clean_name(self, name):
        if not name or name == "Unknown":
            return "Unknown"
        if name.upper() == "EOG":
            return "Image Viewer"
        if "." in name:
            name = name.split(".")[-1]
        return name.strip().capitalize()
