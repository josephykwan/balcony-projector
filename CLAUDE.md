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

- `app.py` — server side: MPV, Player, Scheduler, Projector, alerts, health, Flask routes.
- `library.py` — media folders, per-folder `playlist.json` (order, dwell, hologram mode,
  status) and the background ffmpeg jobs: normalize uploads, Ken Burns stills, hologram
  knockout, campaign disclaimer overlay, thumbnails, flash check, stitched "show" renders.
- `solar.py` — sunrise/sunset maths, no dependencies.
- `templates/index.html` — the phone remote, single file, no build step. Slides and the
  disclaimer strip are drawn on the phone's canvas and uploaded as PNG, so the Pi never
  needs fonts or ffmpeg's drawtext.
- `config.json` — settings; `state.json` — runtime state written by the app.
- `install.sh` / `uninstall.sh` — Pi setup. `get.sh` — one-line installer.
- `README.md` — end-user guide; keep it in sync with behavior changes.
- `tests/` — `smoke_test.py` (real mpv + ffmpeg + fake PJLink, every endpoint),
  `test_scheduler.py`, `fake_pjlink.py`.

## Conventions

- Plain Python 3.11+, Flask, no other dependencies. Keep it apt-installable (`python3-flask`, `mpv`).
  `ffmpeg` is optional: without it files play as-is and the phone says so.
- Keep the code runnable on Python 3.9 too (no `match`, no `X | Y` types); that is what the
  Mac used for testing has.
- Everything the user sees on the phone is written in plain language, no jargon; errors say what to do.
- Store user-visible settings in `config.json`, not in code.
- Anything shown to the street must be safe for kids and drivers: no text overlays, no sudden flashes, slow transitions.

## Status

Tested in a sandbox with real mpv (`--vo=null`) and a fake PJLink server; all API endpoints pass. Not yet run on the actual Pi or projector. First job: get it running on the real hardware and fix audio device names, video output flags, and resolution.

## Roadmap

Rounds 1, 2, 3 and 5 below were built 2026-09-16 (see README for what each does).
Round 4 (motion-triggered reactions with a PIR sensor) was deliberately not built.

1. Seamless, unbreakable loop: ffmpeg job queue normalizing every file to H.264
   1280x800 30fps; stills become slow Ken Burns clips; whole playlists stitched into one
   crossfaded "show" file that loops without a gap; stall detection; flash check;
   quiet-hours dim. **Built.**
2. Hologram-aware content: per-file invert / white-knockout on forced black; thumbnails
   and mirrored preview; phone slide builder; locked "political advertising, paid for by"
   strip burned into everything in a political playlist. **Built.**
3. Smarter schedule and projector: sunset start, season calendar (date ranges to
   playlists), PJLink AV-mute and error flags, Pi temperature/throttling, ntfy push
   alerts. **Built.**
4. Motion-triggered reactions (PIR on GPIO, idle/reaction in one file with A-B loop and
   seek). **Not built, by choice.**
5. Playlist editor (order, enable, dwell per item) and packaging (one-line installer,
   screenshots). **Built.**

Still first on the list: real-hardware bring-up. Nothing here has run on the Pi or the
Hitachi yet; expect to adjust `mpv_video_args`, the audio device and the encoder choice.
