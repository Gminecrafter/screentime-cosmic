# ScreenTime for COSMIC

A lightweight screen time tracker built for the [COSMIC](https://system76.com/cosmic) desktop environment on Linux. Tracks which apps you use and for how long, with a clean GUI and periodic notifications.

This project was built with the help of AI models: **GLM 5.2**, **Sonnet 5**, and **Gemma 4 31B**.

## Features

- **Background tracking** — runs headless as a daemon; time is counted even when the window is closed
- **Wayland-native** — talks directly to COSMIC via `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1` protocols; no X11 required
- **Idle detection** — pauses when there's no keyboard/mouse input (uses `ext_idle_notifier_v1`)
- **Media awareness** — keeps counting while a video or music is playing (via MPRIS / PulseAudio)
- **Session lock awareness** — stops counting when the screen is locked
- **Notifications** — periodic "screen time today" reminders (configurable interval)
- **Weekly insights** — bar chart of daily usage + top apps (weekly and all-time)
- **Theme support** — Catppuccin Mocha, Macchiato, and Latte
- **Single instance** — prevents double-counting if autostart fires twice

## Architecture

The app is split into two processes:

| Process | File | Role |
|---------|------|------|
| **Daemon** | `screentime_tracker.py` | Headless background tracker. Connects to the COSMIC compositor, credits elapsed time to the focused app, writes data to a shared JSON file, fires notifications. |
| **GUI** | `screentime_cosmic.py` | Pure reader. Displays the data the daemon writes and lets you change settings. tkinter-based. |
| **Shared core** | `screentime_core.py` | Data persistence, Wayland tracking, formatting helpers. Deliberately tkinter-free so both processes can import it. |

Data is stored in `~/.screentime_cosmic_data.json`. Writes from the daemon (history) and the GUI (settings) are serialised via an inter-process file lock so they never clobber each other.

## Requirements

- Python 3.8+
- Linux with the COSMIC desktop environment (or any Wayland compositor that supports the ext-foreign-toplevel-list protocol)
- `pywayland` and `libwayland`

## Installation

```bash
git clone https://github.com/Gminecrafter/screentime-cosmic.git
cd screentime-cosmic
pip install -r requirements.txt
```

## Usage

**Start the background tracker** (recommended via COSMIC autostart):

```bash
./start_screentime.sh
```

**Open the GUI** (can be launched independently; the daemon self-heals):

```bash
./start_gui.sh
```

Or run the files directly:

```bash
# Daemon (headless)
python3 screentime_tracker.py

# GUI window
python3 screentime_cosmic.py
```

### Autostart (COSMIC)

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

## License

MIT
