#!/usr/bin/env python3
"""ScreenTime background tracker daemon (COSMIC).

Headless. No window, no terminal. This is the process that autostarts at login
and does the ACTUAL time tracking + the periodic reminder. The GUI
(screentime_cosmic.py) is now a pure reader of the file this daemon writes.

Responsibilities
----------------
- Run the WaylandTracker (its own thread) to know the focused app.
- Once per second, credit the *elapsed* wall time to the focused app for today
  (skipping large gaps that mean the machine slept).
- Flush accumulated seconds to the JSON file every few seconds, merging into
  whatever is on disk so we never clobber the GUI's settings writes.
- Write a `status` heartbeat block so the GUI can show live focus + know the
  daemon is alive.
- Fire the "screen time today" notification every `notif_interval` minutes,
  picking up interval changes the GUI makes at runtime.
- Enforce a single instance (flock) so double-autostart can't double-count.

Nothing is ever printed to stdout/stderr in normal operation; a fatal
traceback is appended to ~/.screentime_cosmic.log for post-mortem only.
"""

import fcntl
import os
import signal
import sys
import threading
import time
import traceback
from datetime import date

import screentime_core as core
from screentime_core import (
    WaylandTracker,
    coerce_interval,
    fmt,
    load_data,
    send_notification,
    media_playing,
    session_locked,
    update_data,
    SLEEP_GAP_THRESHOLD,
    DEFAULT_NOTIF_INTERVAL,
    DEFAULT_IDLE_TIMEOUT,
)

# How often we flush accumulated time + heartbeat to disk. Small enough that
# the GUI's "today" total and green-dot feel live; large enough to be cheap.
FLUSH_INTERVAL_S = 5

# If the tracker reports "Unknown" (nothing focused) for this many seconds while
# it otherwise looks connected, assume pywayland's dispatcher has silently
# wedged (it does this after heavy window churn) and force a fresh connection.
# Rate-limited so we never reconnect in a tight loop.
UNKNOWN_WEDGE_S = 4
RECONNECT_MIN_INTERVAL_S = 20

# How often to re-check logind's lock state (a loginctl call). This is only a
# secondary net: on COSMIC the lock surface deactivates the focused window, so
# counting already stops via get_current_app()->"Unknown". LockedHint helps on
# desktops that keep a window 'activated' while locked, so we poll it slowly.
LOCK_POLL_S = 15

# How often to re-check media playback while idle (pactl/playerctl calls). Only
# polled when input-idle, so it's cheap.
MEDIA_POLL_S = 4

# Consider the daemon's heartbeat fresh to readers for a bit over 2 flushes.
SINGLETON_LOCK = core.DATA_FILE + ".daemon.lock"
LOG_FILE = os.path.expanduser("~/.screentime_cosmic.log")


def _log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def acquire_singleton():
    """Take an exclusive, non-blocking lock so only one daemon runs. Returns
    the open file object (which MUST be kept alive for the process lifetime) or
    None if another instance already holds it."""
    f = open(SINGLETON_LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
    except OSError:
        pass
    return f


class Daemon:
    def __init__(self):
        self.tracker = WaylandTracker()
        self.stop_event = threading.Event()
        # pending[date_str][app] = float seconds not yet flushed to disk.
        # Sub-second remainders are carried across flushes so no time is lost.
        self.pending = {}
        self.notif_interval = DEFAULT_NOTIF_INTERVAL
        # Wedge-detection state (see UNKNOWN_WEDGE_S).
        self._unknown_since = 0.0
        self._last_reconnect = 0.0
        # Cached session-lock state (refreshed every LOCK_POLL_S).
        self._locked = False
        # Cached media-playing state + the effective idle-pause decision.
        self._media_playing = False
        self._idle_paused = False
        # Whether we've already tried a reconnect for the current Unknown run.
        self._reconnect_tried = False

    # -- lifecycle --

    def request_stop(self, *_):
        self.stop_event.set()

    @staticmethod
    def _idle_ms_from_settings(s):
        """Idle-pause timeout in ms from settings, or 0 when disabled."""
        if not bool(s.get("idle_enabled", True)):
            return 0
        mins = coerce_interval(s.get("idle_timeout"), default=DEFAULT_IDLE_TIMEOUT)
        return mins * 60 * 1000

    def run(self):
        # Seed interval + idle settings from disk so they're honoured from the
        # very first tick, not just after the first flush.
        try:
            s = load_data().get("settings", {})
            self.notif_interval = coerce_interval(s.get("notif_interval"))
            self.tracker.set_idle_timeout_ms(self._idle_ms_from_settings(s))
        except Exception:
            self.notif_interval = DEFAULT_NOTIF_INTERVAL

        t = threading.Thread(target=self.tracker.run, daemon=True)
        t.start()

        last_wall = time.time()
        last_flush = 0.0  # monotonic; 0 forces an immediate first flush
        last_lock_check = 0.0  # monotonic; 0 forces an immediate first check
        last_media_check = 0.0
        # Reminders are scheduled on the monotonic clock so an NTP/wall-clock
        # step can't make one fire early or hang forever.
        next_notif = time.monotonic() + self.notif_interval * 60

        while not self.stop_event.is_set():
            # Interruptible ~1s tick.
            self.stop_event.wait(1.0)
            if self.stop_event.is_set():
                break

            now = time.time()
            elapsed = now - last_wall
            last_wall = now

            app = self.tracker.get_current_app()

            mono = time.monotonic()
            # Refresh the lock state periodically (cheap loginctl call).
            if mono - last_lock_check >= LOCK_POLL_S:
                self._locked = session_locked()
                last_lock_check = mono

            # Decide whether idle should pause counting. The user is idle when
            # there's been no keyboard/mouse input past the timeout — but we
            # keep counting if media is actively playing (a video/music with
            # sound), since that's active use even without input. Media is only
            # polled while idle, so it's cheap.
            idle = self.tracker.is_idle()
            if idle:
                if mono - last_media_check >= MEDIA_POLL_S:
                    self._media_playing = media_playing()
                    last_media_check = mono
            else:
                self._media_playing = False
                last_media_check = 0.0  # re-check promptly next idle stretch
            self._idle_paused = idle and not self._media_playing

            # Credit elapsed time to the focused app, unless: the gap looks
            # like a suspend/resume or backwards clock step; the screen is
            # locked; or we're idle-with-no-media.
            if (not self._locked and not self._idle_paused
                    and 0 < elapsed < SLEEP_GAP_THRESHOLD
                    and app and app != "Unknown"):
                today = str(date.today())
                day = self.pending.setdefault(today, {})
                day[app] = day.get(app, 0.0) + elapsed

            # Self-heal a wedged compositor connection. pywayland can silently
            # stop delivering focus events after heavy window churn, leaving us
            # stuck reporting "Unknown" while a window is clearly focused (this
            # was the "stops counting when I focus a window" bug). If that
            # persists while we otherwise look connected, force a reconnect,
            # which re-enumerates every toplevel and resyncs focus.
            if app == "Unknown" and self.tracker.is_tracking():
                if self._unknown_since == 0.0:
                    self._unknown_since = now
                    self._reconnect_tried = False
                elif (not self._reconnect_tried
                        and now - self._unknown_since >= UNKNOWN_WEDGE_S
                        and now - self._last_reconnect >= RECONNECT_MIN_INTERVAL_S):
                    # ONE reconnect per Unknown run. If a window really is
                    # focused but the dispatcher wedged, this recovers it. If
                    # it's a genuine no-focus state (screen locked / idle — on
                    # COSMIC the lock surface deactivates the window), the
                    # reconnect won't help, so we must not keep reconnecting
                    # every few seconds for the whole time the user is away.
                    self.tracker.request_reconnect()
                    self._last_reconnect = now
                    self._reconnect_tried = True
            else:
                self._unknown_since = 0.0
                self._reconnect_tried = False

            # Flush + refresh the interval from disk (the GUI may have changed
            # it). If it changed, reschedule the next reminder relative to now.
            if mono - last_flush >= FLUSH_INTERVAL_S:
                new_interval = self.flush()
                last_flush = mono
                if new_interval != self.notif_interval:
                    self.notif_interval = new_interval
                    next_notif = mono + new_interval * 60

            # Periodic reminder.
            if time.monotonic() >= next_notif:
                self.fire_notification()
                next_notif = time.monotonic() + self.notif_interval * 60

        # Clean shutdown: final flush + drop the compositor connection.
        self.flush()
        self.tracker.stop()

    # -- persistence --

    def flush(self):
        """Merge accumulated whole seconds into the on-disk history, write a
        fresh status heartbeat, and return the current notif_interval from
        disk. Sub-second remainders stay in ``self.pending`` for next time."""
        heartbeat = time.time()
        pid = os.getpid()
        current_app = self.tracker.get_current_app()
        tracking = self.tracker.is_tracking()
        connected = self.tracker.is_connected()
        idle_paused = self._idle_paused
        media = self._media_playing
        seen = {}

        def mut(data):
            hist = data.setdefault("history", {})
            for day, apps in self.pending.items():
                day_map = hist.setdefault(day, {})
                for app, secs in apps.items():
                    whole = int(secs)
                    if whole:
                        day_map[app] = int(day_map.get(app, 0)) + whole
            data["status"] = {
                "current_app": current_app,
                "tracking": tracking,
                "connected": connected,
                "locked": self._locked,
                "idle": idle_paused,
                "media": media,
                "pid": pid,
                "heartbeat": heartbeat,
            }
            s = data.get("settings", {})
            seen["interval"] = coerce_interval(s.get("notif_interval"))
            seen["idle_ms"] = self._idle_ms_from_settings(s)

        try:
            update_data(mut)
        except Exception as e:  # never let a disk hiccup kill the daemon
            _log(f"flush failed: {e!r}")
            return self.notif_interval

        # Pick up any idle-setting change the GUI made (enable/disable or a new
        # timeout). Cheap + idempotent; the tracker only rebuilds on a change.
        self.tracker.set_idle_timeout_ms(seen.get("idle_ms", 0))

        # Carry only the sub-second remainder forward; drop fully-flushed keys
        # and any day that isn't today so ``pending`` can't grow without bound.
        today = str(date.today())
        for day in list(self.pending):
            apps = self.pending[day]
            if day != today:
                del self.pending[day]
                continue
            for app in list(apps):
                apps[app] -= int(apps[app])  # keep fraction, discard flushed whole part
                if apps[app] <= 1e-6:
                    del apps[app]

        return seen.get("interval", self.notif_interval)

    # -- notifications --

    def fire_notification(self):
        try:
            data = load_data()
            today = str(date.today())
            total = sum(int(v) for v in data.get("history", {}).get(today, {}).values())
            send_notification(f"Total screen time today: {fmt(total)}")
        except Exception as e:
            _log(f"notification failed: {e!r}")


def main():
    lock = acquire_singleton()
    if lock is None:
        # Another daemon already owns tracking; exit quietly so autostart
        # firing twice (or a manual launch) can't double-count.
        return 0

    daemon = Daemon()
    signal.signal(signal.SIGTERM, daemon.request_stop)
    signal.signal(signal.SIGINT, daemon.request_stop)
    daemon.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        _log("fatal:\n" + traceback.format_exc())
        raise
