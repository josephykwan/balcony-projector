# Balcony Projector

A Raspberry Pi that loops videos on a see-through screen hung on the balcony,
facing the street. You run it from your phone: pick a show, go dark, set the
evening schedule, and turn the projector on and off.

Nothing but the video is ever drawn on the screen. If something goes wrong, the
phone page tells you what to do.

## What you need

- Raspberry Pi 4 with Raspberry Pi OS Lite (64-bit). No desktop.
- The Pi's HDMI 0 port (the one next to the power jack) into the projector's HDMI 1.
- Videos on the Pi, in folders under `~/media`:
  - `halloween` loops all evening
  - `campaign` loops all evening
  - `movies` play once, then the screen goes dark
- Optional: an Ethernet cable from the Pi to the projector's LAN port, so the
  remote can turn the projector on and off.

## Install

On the Pi, in this folder:

```bash
sudo ./install.sh --quiet-boot --projector-link --timezone America/Chicago
```

- `--quiet-boot` hides the boot text and login prompt so the street only ever
  sees black or video. Reboot once afterwards.
- `--projector-link` gives the Pi's Ethernet port the address `192.168.50.1`.
  Set the projector's LAN address to `192.168.50.2` in its own menu.
- `--timezone` sets the Pi's clock so the evening schedule fires at the right time.

When it finishes it prints the address for your phone, normally
`http://balcony.local:8080/`. Add it to your phone's home screen.

To remove everything except your videos:

```bash
sudo ./uninstall.sh
```

## Using the phone page

**On the screen now.** What is playing, with Pause, Next and Go dark. Go dark
stops playback and leaves the screen black; the projector stays on.

**Shows.** Tap Halloween or Campaign to loop everything in that folder. Movies
are listed one by one; tap one to play it once.

**Projector.** Turn on / Turn off, once projector control is on in Settings.
Turning on takes about a minute; the remote waits, then switches to HDMI 1.
Turning off lets the projector cool down on its own fan. Never cut its power
with a smart plug.

**Evening schedule.** Pick a start time, an end time and a show. Every evening
the Pi turns the projector on, starts the show, and at the end goes dark and
turns the projector off. Times after midnight work (for example 19:00 to 01:00).
If you start something yourself during the window, the schedule leaves it alone.

**Add or remove videos.** Upload from the phone, or copy files straight into
the folders on the Pi (for example with `scp` or a USB stick). MP4 with H.264
video plays best. Pictures (JPG, PNG) work too and show for 12 seconds each;
change that in Settings.

**Settings.** Projector address and password, which HDMI input to select, which
audio output to use, how long pictures show, and a PIN.

**PIN.** Anyone on your Wi-Fi can open the page. Set a 4 to 8 digit PIN in
Settings and the page asks for it once per phone.

## If something goes wrong

The yellow "Needs attention" box at the top of the phone page lists problems in
plain words, such as an empty folder, a file that will not play, or a projector
that is not answering. Under Settings there is "Restart the player" and "Show the
log".

If the page itself will not open:

```bash
sudo systemctl status balcony-projector
journalctl -u balcony-projector -f
```

Things that only show up on the real hardware, and where to change them:

- **No picture at all.** `mpv_video_args` in `config.json`. The defaults are
  `--vo=gpu --gpu-context=drm --hwdec=auto-safe`. Try `--drm-connector=HDMI-A-1`
  if the Pi picks the wrong HDMI port, or drop `--hwdec` if video stutters.
- **No sound.** Pick the HDMI output under Settings, or set `audio_device` in
  `config.json` (run `mpv --audio-device=help` to see the names).
- **Wrong resolution.** The projector is 1280x800. mpv scales to whatever the
  screen reports; if it looks wrong, force the mode with `--drm-mode=1280x800`
  in `mpv_video_args`.

## Files

- `app.py` runs everything: the player, the projector control, the schedule and
  the phone page. `python3 app.py` starts it by hand.
- `templates/index.html` is the phone page.
- `config.json` holds the settings. Edit it and restart the service, or use the
  phone page.
- `state.json` remembers what was playing, so it resumes after a power cut.
- `tests/smoke_test.py` runs the whole thing against a fake projector and real
  mpv with no display, and checks every button. `tests/test_scheduler.py` checks
  the schedule maths.

## Rules for what goes on the screen

The street can see it, including children and drivers. No text overlays, no
sudden flashes, slow transitions. The player never draws menus, error messages
or progress bars on the screen; all of that goes to the phone.
