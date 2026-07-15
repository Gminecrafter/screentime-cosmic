# ScreenTime — Screen Time Tracker for Linux (COSMIC / Wayland)

A lightweight, open-source **screen time tracker** for **Linux**, built specifically for the [COSMIC desktop](https://system76.com/cosmic) on **Pop!_OS** and other **Wayland**-based environments. Monitor your daily app usage, track how much time you spend in each application, and get periodic notifications about your screen time — all without X11.

If you've been looking for a **screen time app for Linux**, a **Wayland-native usage tracker**, or a **digital wellbeing tool for Pop!_OS and COSMIC**, this is it.

This project was built with the help of AI models: **GLM 5.2**, **Sonnet 5**, and **Gemma 4 31B**.

## Features

- **Background screen time tracking** — runs headless as a daemon; your usage is counted even when the window is closed
- **Wayland-native** — talks directly to the COSMIC compositor via `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1` protocols; no X11 required. Works on any Wayland compositor that supports the ext-foreign-toplevel-list protocol
- **App usage monitoring** — tracks which application is focused and for how long, with per-app daily breakdowns
- **Idle detection** — automatically pauses when there's no keyboard/mouse input (uses `ext_idle_notifier_v1`), so inactive time isn't counted against you
- **Media awareness** — keeps counting while a video or music is playing (via MPRIS / PulseAudio), so watching a movie doesn't look like idle time
- **Session lock awareness** — stops counting when the screen is locked
- **Screen time notifications** — periodic "screen time today" reminders with a configurable interval
- **Weekly insights** — bar chart of daily usage + top apps (weekly and all-time)
- **Catppuccin theme support** — Mocha, Macchiato, and Latte colour schemes
- **Single instance** — prevents double-counting if autostart fires twice
- **Digital wellbeing** — understand your computer usage patterns and manage your screen time

## Why This Exists

Most screen time trackers are built for macOS or Windows. Linux users — especially on newer Wayland-based desktops like COSMIC on Pop!_OS — don't have a good option. Existing tools either require X11, use `xdotool`/`xprop` (which don't work on Wayland), or are unmaintained. ScreenTime fills this gap with a clean, Wayland-native approach that talks directly to the compositor.

## Architecture

The app is split into two processes:

| Process | File | Role |
|---------|------|------|
| **Daemon** | `screentime_tracker.py` | Headless background screen time tracker. Connects to the COSMIC/Wayland compositor, credits elapsed time to the focused app, writes data to a shared JSON file, fires notifications. |
| **GUI** | `screentime_cosmic.py` | Pure reader window. Displays the screen time data the daemon writes and lets you change settings. tkinter-based. |
| **Shared core** | `screentime_core.py` | Data persistence, Wayland tracking, formatting helpers. Deliberately tkinter-free so both processes can import it. |

Data is stored in `~/.screentime_cosmic_data.json`. Writes from the daemon (history) and the GUI (settings) are serialised via an inter-process file lock so they never clobber each other.

## Requirements

- Python 3.8+
- Linux with the COSMIC desktop environment (Pop!_OS 24.04+) or any Wayland compositor that supports the `ext-foreign-toplevel-list` protocol
- `pywayland` and `libwayland`

## Installation

```bash
git clone https://github.com/Gminecrafter/screentime-cosmic.git
cd screentime-cosmic
pip install -r requirements.txt
```

## Usage

**Start the background screen time tracker** (recommended via COSMIC autostart):

```bash
./start_screentime.sh
```

**Open the usage GUI** (can be launched independently; the daemon self-heals):

```bash
./start_gui.sh
```

Or run the files directly:

```bash
# Daemon (headless screen time tracker)
python3 screentime_tracker.py

# GUI window (screen time viewer)
python3 screentime_cosmic.py
```

### Autostart (COSMIC / Pop!_OS)

Create a `.desktop` file in `~/.config/autostart/`:

```ini
[Desktop Entry]
Type=Application
Name=ScreenTime Tracker
Exec=/path/to/screentime/start_screentime.sh
Hidden=false
X-COSMIC-Autostart-Phase=Applications
```

## Configuration

All settings are managed through the GUI (Settings tab) and persist to the data file:

| Setting | Default | Description |
|---------|---------|-------------|
| Theme | Mocha | Catppuccin colour scheme |
| Font | JetBrains Mono | UI font |
| Notification interval | 30 min | How often to show "screen time today" |
| Pause when idle | enabled | Stop counting after N min of no input |
| Idle timeout | 5 min | Minutes of no input before pausing |

## Data Format

```json
{
  "history": {
    "2025-07-15": {
      "Firefox": 3600,
      "Code": 7200
    }
  },
  "settings": {
    "theme": "Mocha",
    "font": "JetBrains Mono",
    "notif_interval": 30,
    "idle_enabled": true,
    "idle_timeout": 5
  },
  "status": {
    "current_app": "Firefox",
    "tracking": true,
    "connected": true,
    "pid": 12345,
    "heartbeat": 1721059200.0
  }
}
```

## Related Searches

screen time linux, screentime linux, screen time pop os, screentime pop os, screen time wayland, screentime wayland, screen time tracker linux, app usage tracker linux, digital wellbeing linux, computer time tracker, screen usage monitor, COSMIC screen time, Pop!_OS screen time, wayland app tracker, linux usage monitor, screen time app, time tracking linux, focus tracker linux

## License

MIT
