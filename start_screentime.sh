#!/bin/sh
# Autostart launcher for the ScreenTime *background tracker daemon*.
#
# This runs headless: no window, no terminal. It waits for the COSMIC
# compositor to actually advertise the toplevel-tracking globals — not just for
# the Wayland socket to exist — before starting the tracker, so counting begins
# from the very first second of the session instead of after a "waiting"
# period. (The daemon also retries internally, so this wait is a fast-path, not
# a hard requirement.)
set -e

PROJECT_DIR="/home/gabri/screentime"
SCRIPT="$PROJECT_DIR/screentime_tracker.py"

# Make sure the project's bundled protocol package and user-site deps are on
# the path even under the minimal environment COSMIC gives autostart entries.
export PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}"

# notify-send (and other D-Bus clients) need DBUS_SESSION_BUS_ADDRESS, which
# COSMIC's autostart environment sometimes omits. Fall back to the standard
# session bus path so the periodic notification actually fires.
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && [ -n "$XDG_RUNTIME_DIR" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi

runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
wd="${WAYLAND_DISPLAY:-wayland-1}"

# 1. Wait (up to ~30s) for the Wayland socket to exist at all.
for _ in $(seq 1 30); do
    [ -S "$runtime/$wd" ] && break
    sleep 1
done

# 2. Wait (up to ~60s) for the compositor to actually advertise the two
# toplevel-tracking globals. Idempotent and harmless if already ready.
python3 - "$runtime" "$wd" <<'PYEOF'
import sys, time
runtime, wd = sys.argv[1], sys.argv[2]
import os
os.environ["XDG_RUNTIME_DIR"] = runtime
os.environ["WAYLAND_DISPLAY"] = wd
sys.path.insert(0, "/home/gabri/screentime")
deadline = time.time() + 60
while time.time() < deadline:
    try:
        from pywayland.client import Display
        d = Display(); d.connect()
        reg = d.get_registry()
        seen = {"ext_foreign_toplevel_list_v1": False,
                "zcosmic_toplevel_info_v1": False}
        def on_global(registry, id_, interface, version, seen=seen):
            if interface in seen:
                seen[interface] = True
            return 0
        reg.dispatcher["global"] = on_global
        d.roundtrip()
        try: d.disconnect()
        except Exception: pass
        if all(seen.values()):
            sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
sys.exit(0)  # give up waiting but still launch; the daemon retries internally
PYEOF

# Hand off to the headless daemon (replaces this shell; no terminal window).
exec /usr/bin/python3 "$SCRIPT"
