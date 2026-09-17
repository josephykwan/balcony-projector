# Balcony Projector

A Raspberry Pi player that loops video playlists on a see-through balcony screen facing the street, controlled from a phone web page. Built in a Claude chat session; this file carries over the context.

## Hardware

- Raspberry Pi 4 Model B, 2 GB RAM, Raspberry Pi OS Lite (64-bit), console boot (no desktop). Hostname `balcony`.
- Projector: Hitachi CP-BW301WN, short throw (0.4), 3LCD, 3,000 lumens, native 1280x800 (16:10), two HDMI inputs, LAN port. Lamp DT01411. Lamp life 4,000 hrs normal / 6,000 eco.
- Screen: 5 x 9 ft gray holographic rear-projection mesh (Amazon B09C11GXSN). Projector sits behind it on the balcony side; projector's Mirror setting set to horizontal flip so the street reads it correctly.
- Pi connects to projector HDMI 1 via micro-HDMI (Pi HDMI 0, the port next to the power jack). A streaming stick may live on HDMI 2 for Netflix.
- Owner is in Dallas, TX (America/Chicago). Renting an apartment; balcony faces the street.

## What it does

- Playlists in `~/media/<folder>`: `halloween` (loops, AtmosFX Hollusion files with no background), `campaign` (loops, political slides/videos), `movies` (pick one, play once, then dark).
- mpv plays full screen straight to the framebuffer (`--vo=gpu --gpu-context=drm`), controlled over its JSON IPC socket. Never draw anything on screen but the video: no OSD, no menus, no error text. Errors go to the phone page.
- Flask serves the phone remote (`templates/index.html`) and a JSON API. Optional PIN via `X-Pin` header.
- Evening schedule (start/end times) with optional PJLink power control of the projector over a direct Ethernet link (192.168.50.1 Pi, 192.168.50.2 projector). Uses the projector's own cooldown; never cut projector power with a smart plug.
- Watchdog restarts mpv if it dies; last looping playlist resumes after reboot.
- systemd service `balcony-projector`, installed by `install.sh` (`--quiet-boot` hides boot text).

## Files

- `app.py` — everything server side (MPV, Player, Scheduler, Projector, Flask routes).
- `templates/index.html` — the phone remote, single file, no build step.
- `config.json` — settings; `state.json` — runtime state written by the app.
- `install.sh` / `uninstall.sh` — Pi setup.
- `README.md` — end-user guide; keep it in sync with behavior changes.

## Conventions

- Plain Python 3.11+, Flask, no other dependencies. Keep it apt-installable (`python3-flask`, `mpv`).
- Everything the user sees on the phone is written in plain language, no jargon; errors say what to do.
- Store user-visible settings in `config.json`, not in code.
- Anything shown to the street must be safe for kids and drivers: no text overlays, no sudden flashes, slow transitions.

## Status

Tested in a sandbox with real mpv (`--vo=null`) and a fake PJLink server; all API endpoints pass. Not yet run on the actual Pi or projector. First job: get it running on the real hardware and fix audio device names, video output flags, and resolution.

## Roadmap (in order)

1. Real-hardware bring-up.
2. Sunset-based scheduling and a date calendar (Oct 1–31 Halloween, campaign through election day).
3. Auto re-encode uploads with ffmpeg to H.264 1080p.
4. Phone playlist editor: reorder, per-item duration, crossfades.
5. Simple text slide builder for campaign/announcement slides.
6. Health monitoring: Pi temperature, disk space, lamp-hour reminder, alert when playback stops.
7. Packaging: one-line installer, GitHub repo with screenshots.
