# Balcony Projector — Project Brief

A street-facing hologram display: a Raspberry Pi loops video onto a see-through
rear-projection screen on an apartment balcony, controlled from a phone. Content is
made on a laptop and sent to the Pi. This file is the single source of truth for
Claude Code; read it before touching anything.

First task: read this file, README.md, and app.py, then work through the "Order of
work" section at the bottom, starting with Phase 1.

## 1. Hardware and physical setup

- Screen: 5 × 9 ft gray holographic rear-projection mesh (Amazon B09C11GXSN), hung at
  the balcony railing. Black in the video is invisible on the mesh; anything bright
  appears to float in midair. This one fact drives every content decision.
- Projector: Optoma ML750ST (used). LED light source, 700 ANSI lumens, 20,000 hr, no
  cooldown needed. Native 1280×800 (16:10). Fixed 0.8 throw ratio. Auto vertical
  keystone. ONE HDMI port. No LAN/network port. Built-in USB media player (useful for
  testing files without the Pi).
- Placement: projector sits on an interior window sill (6 in deep) and projects about
  6 ft through the window glass to the screen at the railing. At 0.8 throw that gives a
  ~7.5 ft wide image. Rear projection mode is set in the projector's own menu so text
  reads correctly from the street.
- Power: projector is turned on and off by hand each night. No smart plug, no PJLink.
  The Pi stays powered 24/7 and keeps looping the last chosen playlist, so content is
  on screen the moment the projector warms up.
- Player: Raspberry Pi 4 Model B, 2 GB RAM, Raspberry Pi OS Lite 64-bit, console boot
  (no desktop), hostname `balcony`. Micro-HDMI (port next to the power jack) straight
  into the projector's HDMI. Official 5V 3A supply, case with heatsink/fan.
- Audience: people on the sidewalk across the street, roughly 30–60 ft away. Drivers
  pass by, so no flashing and no sudden motion.
- Location: Dallas, TX (America/Chicago). Rented apartment.

## 2. Architecture: two halves, strict split

The Pi is a player only. It loops the files in each playlist folder, switches
playlists when told, accepts uploads, resumes after reboot, and restarts mpv if it
dies. It never renders, re-encodes, generates thumbnails, or runs AI. Priorities:
instant playlist switching, zero blank frames at loop points, never draw anything on
screen but the video (no OSD, menus, or error text; errors go to the phone page).

The laptop is the studio. Everything creative happens in `tools/studio`: slide
builder, 3D illusion renders, AI clip pipeline, and a required "prepare" step that
converts any file to the Pi's native format before upload. The Pi must never receive
a file that isn't already in its format.

Native format for every file on the Pi: H.264 MP4, 1280×800, 30 fps, yuv420p, CRF
18–20, AAC or no audio, first and last frames identical (seamless loop).

## 3. What already exists

Built and tested in a sandbox (real mpv with `--vo=null`, real ffmpeg, fake PJLink
projector, fake ntfy server). Not yet on real hardware.

- `app.py` — Flask + mpv. Playlists (halloween loops, campaign loops, movies
  play-one-then-dark), mpv JSON IPC control, evening schedule (set times or sunset /
  sunrise with offsets, season date ranges), quiet-hours dimming, PJLink projector
  control (off by default; only for projectors with a LAN port), crash watchdog with
  stall detection, resume-on-boot, PIN lock, upload/delete, per-file order / on-off,
  shuffle, Pi health (temperature, throttling, disk), optional ntfy push alerts.
- `library.py` — the media folders and per-folder `playlist.json`, plus an ffmpeg
  conversion pipeline (normalize to native format, Ken Burns stills, hologram
  invert/knockout, campaign disclaimer strip, thumbnails, flash check, stitched
  crossfaded "show" per playlist). **Disabled by default** (`processing.enabled`
  false) because of the split in section 2; the installer no longer puts ffmpeg on
  the Pi. Phase 2 moves this logic into `tools/studio` "prepare"; reuse it rather than
  rewriting it. With it off, the Pi plays files exactly as uploaded.
- `solar.py` — sunrise/sunset maths, no dependencies.
- `templates/index.html` — the phone remote, single file, no build step. Projector
  card and studio-only controls (slide builder, hologram, dwell) are hidden unless
  the matching feature is on.
- `config.json`, `state.json`, `install.sh` (`--quiet-boot`, `--share`,
  `--projector-link`, `--timezone`), `uninstall.sh`, `get.sh` (one-line installer),
  `README.md`, `docs/screenshots/`.
- `tests/smoke_test.py` walks every endpoint; `tests/test_scheduler.py` covers the
  schedule and sunset maths; `tests/fake_pjlink.py` is the fake projector.
- NOT tested: real HDMI output, audio device names, actual Pi performance.

## 4. Pi changes to make

1. Update the Hardware section of this file if anything above changes; keep README in
   sync with behavior.
2. `projector.enabled` defaults to `false`. The Projector section of the remote is
   hidden when disabled (done). README notes PJLink only applies to projectors with a
   LAN port.
3. Keep the evening schedule but reframe it in README as optional: useful for
   switching playlists automatically (Halloween loop at 6:30, dark at 11) even though
   the projector is powered by hand. `resume_on_boot` is on by default so a power blip
   never leaves a dark screen.
4. `install.sh` adds `video=HDMI-A-1:1280x800@60` to `cmdline.txt` so the Pi outputs
   the projector's native resolution. (done)
5. `install.sh --share` (optional) installs Samba so `~/media` appears as a network
   drive on a Mac or Windows laptop. (done)
6. README "Adding videos": note the web remote's upload button also works from a
   laptop browser. (done)
7. README "Content tips" section: pure black backgrounds only; large bright white
   text; avoid dim or muddy visuals; test at dusk and again at full dark from across
   the street. (done)
8. Remove nothing that works, add nothing heavy. Phone remote stays: playlist buttons,
   play/pause/next, volume, screen dark, upload, optional schedule, PIN.
9. Once on real hardware: confirm mpv args (`--vo=gpu --gpu-context=drm
   --hwdec=auto-safe`, fall back to `--vo=drm`), audio device name for HDMI,
   boot-to-console, and smooth playback of the AtmosFX files.

## 5. The studio (`tools/studio`, runs on the laptop)

### 5a. Prepare and send (build first; everything else depends on it)

- `prepare <file>`: converts any video or image to the native format above, forces
  near-black pixels to pure black, removes edge halos, crossfades the tail into the
  head for a seamless loop, writes a poster thumbnail. Refuses to output anything that
  fails the checks.
- A brightness check: mean frame luminance capped at 0.35 (a full-white frame looks
  like a bare bulb on the mesh). Warn or auto-dim.
- A mirrored preview viewer (rear projection) cropped to the screen's aspect.
- `send <file> --playlist <name>`: uploads prepared files only, over scp, to
  `balcony.local:~/media/<playlist>/`. A "Send to balcony" button in every studio tool.

### 5b. Slide builder (2D, campaign and announcements)

Web page on localhost with a phone-friendly UI. Slide model: headline, subline,
duration (default 6 s), text color (default white), accent color, logo PNG (auto
knockout white→transparent, invert option), effect, transition, `political` flag,
disclaimer text.

Effects, each an HTML5 canvas module rendered headless (Chromium + ffmpeg),
deterministic by seed: `particle_assemble`, `depth_drift`, `light_sweep`,
`reveal_behind`, `halo_pulse`, `trail_wipe`. Transitions: `fade`, `particle_burst`.
Every effect starts and ends on pure black or an identical frame.

A "show" is an ordered list of slides rendered to one loop. Re-render only changed
slides; keep source JSON so slides stay editable. Drag to reorder, live low-res
mirrored preview.

Seed show (six beats, ~30 s, text left as placeholders): wordmark (particle_assemble)
→ office (reveal_behind) → one message line (depth_drift) → "VOTE NOV 3" (trail_wipe)
→ website (halo_pulse) → wordmark outro (light_sweep, fade to black).

### 5c. 3D illusion pipeline (anamorphic, "Times Square" style)

Built on Blender, headless via `bpy`, rendering 1280×800 loops on black.

1. Calibration / venue profile. Inputs: screen size (5×9 ft), screen height off the
   ground, viewer spot (distance and height from the sidewalk across the street),
   optional photo from that spot. Sets up a Blender camera matched to the viewer so
   renders are correctly anamorphic for that position. Saved as a reusable profile.
2. Fake frame. An optional glowing "window" frame drawn slightly inside the screen
   edges. Objects that pass outside it appear to leap out of the screen. Styles: thin
   neon line, brushed metal, stone arch. Never near full-white.
3. Templates (parameterized scenes, seamless loops on black):
   - `snow_depth`: three layers of snow at different depths and speeds, flakes
     drifting toward the viewer and passing in front of the frame.
   - `ghost_float`: an imported figure (black-background video on a plane, or a 3D
     model) drifting in and out of the frame with glow falloff for depth. Accepts
     AtmosFX-style clips as textures.
   - `spider_lunge`: a spider descends on a thread, pauses, lunges out past the frame
     toward the camera, retreats. Motion never faster than 1 s; small and infrequent.
   - `text_emerge`: campaign text as extruded 3D letters pushing forward out of the
     frame with a light sweep, hold, recede. Uses the wordmark PNG (auto knockout to
     white on black).
   - `particle_assemble_3d`: the wordmark assembles from particles in depth.
   - `light_tunnel`: receding corridor of light rings behind the frame, as a
     background for text slides.
4. Figure sources: accept AI-generated or stock black-background clips as
   ghost/creature layers, run through the prepare cleanup.

### 5d. AI clip pipeline

`make-clip "<prompt>" --playlist <name>`: calls a video-generation API (start with
Runway; structure so Kling or Adobe Firefly can be swapped in), downloads, runs
prepare, opens preview, uploads on confirmation. Prompt pattern that works: "single
figure, solid black background, centered, slow drifting motion, no camera movement,
seamless loop." Reject prompts naming trademarked characters. Use Firefly for anything
campaign-related; avoid Veo 3 (commercial-use restrictions at time of writing).

## 6. Content rules (enforced in the studio, documented in README)

- Black background always. White or light text is brightest; saturated brand colors
  lose a lot of light on the mesh, so use them for accents only.
- Letter height ≥ 1 in per 10 ft of viewing distance; for 60 ft that's 6 in minimum,
  aim for 10+. One idea per slide, 3–4 words.
- Slow transitions (1.5–2 s fades), nothing flashing faster than once per second, no
  sudden large motion toward the street.
- Loops start and end on identical frames.
- 700 lumens means bright, sparse, high-contrast visuals read well; big dim
  atmospheric effects don't.

## 7. Compliance (campaign content)

- Texas Election Code §255.001: political advertising with express advocacy must show
  "political advertising" (or a recognizable abbreviation such as "Pol. adv.") plus the
  full name of who paid for it, on the face of the ad. Any slide flagged `political`
  gets a persistent footer on every frame: "Pol. adv. paid for by ______". Cannot be
  blank. If the display is the user's own as a supporter, the payer is the user's name;
  if a campaign is behind it, its committee name.
- Use only the campaign's own logo, slogan, and published messaging; never invent
  quotes. Confirm logo use with the campaign.
- Dallas regulates illuminated/animated signs; keep the display on private property,
  avoid projecting into neighbors' windows or the roadway, keep brightness and motion
  modest. Treat as guidance, not legal advice.

## 8. Conventions

- Pi side: plain Python 3.11+, Flask, mpv; apt-installable only (`python3-flask`,
  `mpv`). No other dependencies on the Pi. Keep the code runnable on Python 3.9 too
  (no `match`, no `X | Y` types); that is what the Mac used for testing has.
- Studio side: Python + ffmpeg + Blender + headless Chromium (Playwright). Keep it a
  folder in this repo, not a second project.
- Everything the user sees is plain language; errors say what to do.
- Settings live in `config.json`, not in code.
- Testing on the Mac: no system Flask, so `python3 -m venv` + `pip install flask`;
  Homebrew `mpv` and `ffmpeg` (that ffmpeg has no drawtext, so text is rendered in a
  browser canvas, never with ffmpeg filters).

## 9. Order of work

Phase 1 — Pi on real hardware (deadline: Halloween season, so first). Walk the user
through flashing the SD card (Raspberry Pi Imager: OS Lite 64-bit, hostname `balcony`,
Wi-Fi, time zone America/Chicago, SSH on), copying the project, running
`install.sh --quiet-boot --share`, and checking `journalctl -u balcony-projector -f`
after boot. Apply the Pi changes in section 4. Get AtmosFX Hollusion files looping
smoothly and switching from the phone. Fix real-hardware issues (video output, audio
device, resolution). **Status 2026-09-21: section 4 code changes done; the hands-on
part is in progress with the user.**

Phase 2 — Studio prepare + send (5a). So every file reaching the Pi is safe.

Phase 3 — Slide builder (5b) with the seed campaign show. Then calibration, fake
frame, and `text_emerge` from the 3D pipeline (5c), since campaign content is next in
line.

Phase 4 — Halloween 3D templates: `snow_depth`, `ghost_float`, `spider_lunge`. Then
the AI clip pipeline (5d).

Later: motion-triggered idle→reaction clips (pre-primed in mpv so the switch has no
blank frame), GitHub packaging polish. (Sunset scheduling, date ranges and the health
card already exist on the Pi side.)
