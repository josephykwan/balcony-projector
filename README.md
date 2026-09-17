# Balcony Projector

A Raspberry Pi that loops videos on a see-through screen hung on the balcony,
facing the street. You run it from your phone: pick a show, go dark, set the
evening schedule, make a slide, and turn the projector on and off.

Nothing but the video is ever drawn on the screen. If something goes wrong, the
phone page tells you what to do.

![The phone page](docs/screenshots/home.png)

More: [manage videos](docs/screenshots/manage-videos.png),
[make a slide](docs/screenshots/make-a-slide.png), [settings](docs/screenshots/settings.png).

## What you need

- Raspberry Pi 4 with Raspberry Pi OS Lite (64-bit). No desktop.
- The Pi's HDMI 0 port (the one next to the power jack) into the projector's HDMI 1.
- Videos on the Pi, in folders under `~/media`:
  - `halloween` loops all evening
  - `campaign` loops all evening and is treated as political advertising (see below)
  - `movies` play once, then the screen goes dark
- Optional: an Ethernet cable from the Pi to the projector's LAN port, so the
  remote can turn the projector on and off.

## Install

On the Pi, in this folder:

```bash
sudo ./install.sh --quiet-boot --projector-link --timezone America/Chicago
```

Or, once the project is on GitHub, in one line (edit the OWNER in `get.sh` first):

```bash
curl -fsSL https://raw.githubusercontent.com/josephykwan/balcony-projector/main/get.sh | sudo bash
```

- `--quiet-boot` hides the boot text and login prompt so the street only ever
  sees black or video. Reboot once afterwards.
- `--projector-link` gives the Pi's Ethernet port the address `192.168.50.1`.
  Set the projector's LAN address to `192.168.50.2` in its own menu.
- `--timezone` sets the Pi's clock so the evening schedule fires at the right time.

It installs `mpv`, `python3-flask` and `ffmpeg`. When it finishes it prints the
address for your phone, normally `http://balcony.local:8080/`. Add it to your
phone's home screen.

To remove everything except your videos:

```bash
sudo ./uninstall.sh
```

## Using the phone page

**On the screen now.** What is playing, with Pause, Next and Go dark. Go dark
stops playback and leaves the screen black. If you turn on "blank the projector
while the screen is dark" in Settings, the projector's own shutter closes too,
which cuts the faint glow a projector puts on a dark screen.

**Shows.** Tap Halloween or Campaign to loop everything in that folder. A
"seamless" badge means the folder has been stitched into one file (see below).
Movies are listed one by one; tap one to play it once.

**Projector.** Turn on / Turn off, blank / show picture, and the input picker,
once projector control is on in Settings. Turning on takes about a minute; the
remote waits, then switches to HDMI 1. Turning off lets the projector cool down
on its own fan. Never cut its power with a smart plug. Lamp hours and any fault
the projector reports show up here and in the "Needs attention" box.

**Evening schedule.** Start at sunset (plus or minus some minutes) or at a set
time; end at a set time or at sunrise. Every evening the Pi turns the projector
on, starts the show, and at the end goes dark and turns the projector off.
Seasons are date ranges that pick the show: `10-01` to `10-31` for Halloween
every year, `2026-09-16` to `2026-11-03` for the campaign this year. The first
matching season wins; otherwise the default show plays. If you start something
yourself during the window, the schedule leaves it alone.

**Manage videos.** Upload from the phone, or copy files straight into the
folders on the Pi. Each file has: switch on/off, move up/down, seconds on
screen (for pictures), hologram mode, Redo, Remove. Files copied in by hand are
picked up within about half a minute.

**Make a slide.** Type a few big words, pick a colour, see exactly what the
screen will show, tap Add. The slide drifts slowly so nothing sits still.

**Settings.** Projector connection, the campaign "paid for by" line, sound
output, conversion options, quiet-hours dimming, location for sunset, phone
alerts, and a PIN.

**PIN.** Anyone on your Wi-Fi can open the page. Set a 4 to 8 digit PIN in
Settings and the page asks for it once per phone.

## What happens to the files you add

With `ffmpeg` installed (the installer adds it), every file goes through a
background conversion the first time it is seen. It is never shown until it is
done, and the original is kept in a hidden `.originals` folder.

- Videos become H.264 MP4 at 1280x800 (the projector's own size), 30 frames a
  second, letterboxed on black. Files that are already like that are left alone.
- Pictures become 12-second clips (you can change the seconds) with a slow drift
  and zoom, so nothing static burns into the projector, and they fade in and
  out. Mostly-white pictures get "hologram: invert" automatically.
- Each looping folder is also stitched into one seamless "show" file with
  two-second crossfades, and the loop point lands inside a crossfade. That is
  what plays when the badge says "seamless". It is re-made when files change,
  which takes a few minutes on the Pi; the plain playlist plays until then.
- Every file is checked for sudden brightness jumps. Anything strobing gets a
  "flashes" badge and a warning, because it can bother drivers and people with
  photosensitivity.

**Hologram mode.** The screen is a gray mesh: black disappears and bright things
seem to float. A black logo on a white background does the opposite of what you
want, so:

- **invert** flips it: white background becomes black (invisible), dark artwork
  becomes bright. Right for logos and text on white.
- **knockout** makes only the white areas black and keeps the other colours.
  Right for colour artwork on white.

Test on the real screen at night; what looks fine on a phone can wash out.

## Campaign slides and the law

Texas Election Code section 255.001 says political advertising must say that it
is political advertising and who paid for it, on the face of the ad. The
`campaign` playlist is marked political in `config.json`. Until you set the
"paid for by" line under Settings, nothing in that folder will play, and once
you set it the strip is burned into the bottom of every file in the folder,
including uploads. It cannot be turned off per file. The wording is yours to
get right; have the campaign's lawyer confirm it.

Two more things to keep in mind, which are not the software's job: Dallas
regulates illuminated and animated signs, and a bright projected image facing
the street may count. Keep it on your own property, do not point it into
neighbours' windows, use the quiet-hours dim late in the evening, and check with
Dallas Development Services before a campaign runs on it.

## Alerts

Install the free ntfy app on your phone, pick a topic name nobody would guess,
and enter `https://ntfy.sh/that-topic` under Settings. You will get a message
when the show stops unexpectedly, the player keeps crashing, the projector
reports a fault or will not turn on for the schedule, the Pi runs hot, or the
disk is nearly full. At most one message an hour per kind of problem.

## If something goes wrong

The yellow "Needs attention" box at the top of the phone page lists problems in
plain words. Under Settings there is "Restart the player" and "Show the log",
which includes recent conversions.

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
- **Conversions are slow.** The Pi's hardware encoder (`h264_v4l2m2m`) is used
  when ffmpeg offers it; otherwise software encoding, which is several times
  slower than real time. Settings shows which one is in use. Uploads of big
  movies are better copied in already as H.264 MP4, which is left untouched.

## Files

- `app.py` runs everything: the player, the projector control, the schedule,
  alerts and the phone page. `python3 app.py` starts it by hand.
- `library.py` looks after the media folders and the ffmpeg conversions.
- `solar.py` works out sunrise and sunset.
- `templates/index.html` is the phone page.
- `config.json` holds the settings. Edit it and restart the service, or use the
  phone page.
- `state.json` remembers what was playing, so it resumes after a power cut.
- Each media folder gets a `playlist.json` with the order and settings of its
  files, plus hidden `.originals`, `.thumbs`, `.show` and `.work` folders.
- `tests/smoke_test.py` runs the whole thing against real mpv and ffmpeg, a
  fake projector and a fake ntfy server, and checks every button.
  `tests/test_scheduler.py` checks the schedule and sunset maths.

## Rules for what goes on the screen

The street can see it, including children and drivers. No text overlays except
the legally required one, no sudden flashes, slow transitions. The player never
draws menus, error messages or progress bars on the screen; all of that goes to
the phone.
