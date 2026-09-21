#!/usr/bin/env python3
"""
Balcony Projector - everything server side.

The mpv wrapper, the player, the projector (PJLink), the evening scheduler,
Pi health, push alerts, and the Flask routes the phone page talks to. The media
folders and the ffmpeg jobs live in library.py; sunset maths in solar.py.

Rules that matter here:
  * Nothing is ever drawn on the screen except the video. Errors go to the
    phone page, as the `problems` list in /api/status.
  * Plain Python + Flask only, so `apt install python3-flask mpv` is enough.
    ffmpeg is optional and makes everything smoother.
  * Settings people change live in config.json; runtime state in state.json.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

import library
import solar
from library import Library, LibraryError, Processor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "port": 8080,
    "pin": "",
    "media_dir": "~/media",
    "playlists": {
        "halloween": {"label": "Halloween", "mode": "loop"},
        "campaign": {"label": "Campaign", "mode": "loop", "political": True, "disclaimer": ""},
        "movies": {"label": "Movies", "mode": "once"},
    },
    "volume": 80,
    "resume_on_boot": True,
    "audio_device": "auto",
    "image_seconds": 12,
    "mpv_video_args": ["--vo=gpu", "--gpu-context=drm", "--hwdec=auto-safe"],
    "mpv_extra_args": [],
    "location": {"lat": 32.78, "lon": -96.80, "name": "Dallas"},
    "schedule": {
        "enabled": False,
        "start": "sunset",          # "sunset", "sunrise" or "HH:MM"
        "start_offset": 15,         # minutes, may be negative
        "end": "23:00",
        "end_offset": 0,
        "playlist": "halloween",
        "projector_power": True,
    },
    "seasons": [
        {"name": "Halloween", "from": "10-01", "to": "10-31", "playlist": "halloween"},
        {"name": "Campaign", "from": "2026-09-16", "to": "2026-11-03", "playlist": "campaign"},
    ],
    "dim": {"enabled": False, "start": "22:00", "end": "23:59", "level": 60},
    # Off by default: the Pi is a player. The laptop "studio" prepares files
    # (see CLAUDE.md). Turn this on only for a Pi with ffmpeg and spare time.
    "processing": {"enabled": False, "encoder": "auto", "crossfade_seconds": 2,
                   "render_shows": True, "flash_warn_per_minute": 3},
    "projector": {
        "enabled": False,
        "host": "192.168.50.2",
        "port": 4352,
        "password": "",
        "input": "31",
        "warmup_seconds": 60,
        "mute_when_dark": False,
        "lamp_warn_hours": 3500,
    },
    "alerts": {"enabled": False, "ntfy_url": ""},
    # HTTPS copy of the same site on a second port. Browsers only allow the
    # studio's video encoder on a secure page, so the studio is opened there.
    "tls": {"enabled": True, "port": 8443},
}

log = logging.getLogger("balcony")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _deep_merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _parse_hhmm(text):
    """'19:30' -> (19, 30). Raises ValueError with a plain message."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not match:
        raise ValueError("Times need to look like 19:30 (hours:minutes, 24-hour clock).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Times need to look like 19:30 (hours:minutes, 24-hour clock).")
    return hour, minute


def _in_window(start, end, now=None):
    """Is `now` inside the daily window start..end (HH:MM strings)? Handles midnight."""
    now = now or datetime.now()
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    s = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    e = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if e <= s:
        return now >= s or now < e
    return s <= now < e


class RingLogHandler(logging.Handler):
    """Keeps the last few hundred log lines so the phone can show them."""

    def __init__(self, size=300):
        super().__init__()
        self.lines = deque(maxlen=size)

    def emit(self, record):
        self.lines.append(self.format(record))


# --------------------------------------------------------------------------
# Config and state files
# --------------------------------------------------------------------------

class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(path):
            with open(path) as f:
                _deep_merge(self.data, json.load(f))

    def save(self):
        with self.lock:
            _atomic_write(self.path, json.dumps(self.data, indent=2) + "\n")

    @property
    def media_dir(self):
        return os.path.abspath(os.path.expanduser(self.data["media_dir"]))


class State:
    DEFAULTS = {
        "mode": "off",          # off | loop | once
        "playlist": None,
        "file": None,
        "volume": None,
        "shuffle": False,
        "last_window_id": None,  # which scheduled evening we last acted on
    }

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(self.DEFAULTS)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError) as exc:
                log.warning("Could not read %s, starting fresh: %s", path, exc)

    def get(self, key):
        return self.data.get(key)

    def update(self, **fields):
        with self.lock:
            self.data.update(fields)
            self.data["updated"] = datetime.now().isoformat(timespec="seconds")
            try:
                _atomic_write(self.path, json.dumps(self.data, indent=2) + "\n")
            except OSError as exc:
                log.warning("Could not write %s: %s", self.path, exc)


# --------------------------------------------------------------------------
# mpv over its JSON IPC socket
# --------------------------------------------------------------------------

class MPVError(Exception):
    pass


class MPV:
    """One mpv process, idle between playlists, talked to over a unix socket."""

    def __init__(self, socket_path, args):
        self.socket_path = socket_path
        self.args = args
        self.proc = None
        self.sock = None
        self.connected = False
        self.send_lock = threading.Lock()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.next_id = 1
        self.events = queue.Queue()
        self.stderr_lines = deque(maxlen=60)

    def start(self):
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        cmd = ["mpv", "--idle=yes", "--input-ipc-server=" + self.socket_path] + self.args
        log.info("Starting mpv: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, errors="replace")
        threading.Thread(target=self._read_stderr, daemon=True).start()

        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise MPVError("mpv exited right after starting: "
                               + (" / ".join(self.stderr_lines) or "no message"))
            if os.path.exists(self.socket_path):
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(self.socket_path)
                    break
                except OSError:
                    self.sock = None
            time.sleep(0.1)
        if self.sock is None:
            self.kill()
            raise MPVError("mpv started but never opened its control socket.")
        self.connected = True
        threading.Thread(target=self._read_socket, daemon=True).start()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and self.connected

    def _read_stderr(self):
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                self.stderr_lines.append(line)
                log.warning("mpv: %s", line)

    def _read_socket(self):
        try:
            for raw in self.sock.makefile("r"):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "request_id" in msg:
                    with self.pending_lock:
                        slot = self.pending.pop(msg["request_id"], None)
                    if slot:
                        slot["reply"] = msg
                        slot["event"].set()
                elif "event" in msg:
                    self.events.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.connected = False
            with self.pending_lock:
                for slot in self.pending.values():
                    slot["reply"] = {"error": "mpv went away"}
                    slot["event"].set()
                self.pending.clear()
            self.events.put({"event": "_disconnected"})

    def command(self, *args, timeout=5.0):
        if not self.connected:
            raise MPVError("The video player isn't running.")
        slot = {"event": threading.Event(), "reply": None}
        with self.send_lock:
            rid = self.next_id
            self.next_id += 1
            with self.pending_lock:
                self.pending[rid] = slot
            payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
            try:
                self.sock.sendall(payload.encode())
            except OSError as exc:
                with self.pending_lock:
                    self.pending.pop(rid, None)
                raise MPVError("Lost the connection to mpv: %s" % exc)
        if not slot["event"].wait(timeout):
            with self.pending_lock:
                self.pending.pop(rid, None)
            raise MPVError("mpv did not answer in time.")
        reply = slot["reply"]
        if reply.get("error") != "success":
            raise MPVError(reply.get("error", "unknown error"))
        return reply.get("data")

    def get(self, prop, default=None):
        try:
            return self.command("get_property", prop)
        except MPVError:
            return default

    def set(self, prop, value):
        self.command("set_property", prop, value)

    def quit(self):
        try:
            if self.connected:
                self.command("quit", timeout=2)
        except MPVError:
            pass
        self.kill()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.connected = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Player: playlists, watchdog, resume after reboot
# --------------------------------------------------------------------------

class Player:
    def __init__(self, cfg, state, lib, alerts):
        self.cfg = cfg
        self.state = state
        self.lib = lib
        self.alerts = alerts
        self.projector = None            # set later; used for mute-when-dark
        self.lock = threading.RLock()
        self.mpv = None
        # Unix socket paths have a short limit (about 100 characters), so keep
        # this out of the app folder, which may live somewhere deep.
        self.socket_path = os.environ.get(
            "BALCONY_MPV_SOCKET", "/tmp/balcony-mpv-%d.sock" % os.getuid())
        self.shutting_down = False
        self.gave_up = False
        self.retry_at = 0
        self.restarts = deque(maxlen=20)
        self.ignore_idle = True          # true until we load something
        self.last_error = ""             # plain language, for the phone
        self.file_errors = {}            # basename -> what went wrong
        self.started_at = None
        self.segments = []               # when playing a stitched show
        self.dim_level = 100
        self._stall_pos = None
        self._stall_since = time.time()

    # ---- lifecycle -------------------------------------------------------

    @staticmethod
    def _drm_hints():
        """On a Pi there are two /dev/dri cards; only one drives HDMI. Tell mpv
        which, and which HDMI port, unless config.json already does."""
        hints = []
        try:
            cards = {}
            for entry in os.listdir("/sys/class/drm"):
                if "-HDMI-A-" in entry and entry.startswith("card"):
                    card = entry.split("-")[0]
                    with open(os.path.join("/sys/class/drm", entry, "status")) as f:
                        status = f.read().strip()
                    cards.setdefault(card, []).append((entry.split("-", 1)[1], status))
            if cards:
                card = sorted(cards)[0]
                for c, ports in cards.items():
                    if any(st == "connected" for _, st in ports):
                        card = c
                hints.append("--drm-device=/dev/dri/" + card)
                ports = cards[card]
                connected = [name for name, st in ports if st == "connected"]
                hints.append("--drm-connector=" + (connected[0] if connected else sorted(ports)[0][0]))
        except OSError:
            pass
        return hints

    def _mpv_args(self):
        args = [
            "--fullscreen", "--force-window=yes", "--keep-open=no",
            "--osd-level=0", "--no-osc", "--no-osd-bar", "--cursor-autohide=always",
            "--no-input-default-bindings", "--input-vo-keyboard=no", "--no-input-cursor",
            "--terminal=yes", "--input-terminal=no", "--msg-level=all=error",
            "--prefetch-playlist=yes",
            "--image-display-duration=%d" % int(self.cfg.data.get("image_seconds", 12)),
            "--volume-max=100",
        ]
        video_args = list(self.cfg.data.get("mpv_video_args", []))
        if "--gpu-context=drm" in video_args or "--vo=drm" in video_args:
            for hint in self._drm_hints():
                key = hint.split("=")[0]
                if not any(a.startswith(key + "=") for a in video_args):
                    video_args.append(hint)
        args += video_args
        device = self.cfg.data.get("audio_device") or "auto"
        if device != "auto":
            args.append("--audio-device=" + device)
        args += list(self.cfg.data.get("mpv_extra_args", []))
        return args

    def start(self):
        """Start mpv and the background threads. Resumes a looping playlist."""
        threading.Thread(target=self._event_loop, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        with self.lock:
            self._launch()
        self._resume(boot=True)

    def _resume(self, boot=False):
        if boot and not self.cfg.data.get("resume_on_boot", True):
            self.state.update(mode="off", file=None)
            return
        if self.state.get("mode") == "loop" and self.state.get("playlist"):
            try:
                self.play(self.state.get("playlist"))
                log.info("Resumed playlist %s", self.state.get("playlist"))
            except (ValueError, MPVError) as exc:
                self.last_error = str(exc)
                self.state.update(mode="off")
        elif self.state.get("mode") == "once":
            self.state.update(mode="off", file=None)

    def _launch(self):
        self.ignore_idle = True
        if len(self.socket_path) > 100:
            raise MPVError("the mpv socket path %s is too long; set BALCONY_MPV_SOCKET "
                           "to something short" % self.socket_path)
        self.mpv = MPV(self.socket_path, self._mpv_args())
        self.mpv.start()
        self.started_at = time.time()
        self.restarts.append(time.time())
        volume = self.state.get("volume")
        if volume is None:
            volume = self.cfg.data.get("volume", 80)
        try:
            self.mpv.set("volume", int(volume))
        except MPVError as exc:
            log.warning("Could not set volume: %s", exc)
        self.apply_dim(force=True)

    def shutdown(self):
        self.shutting_down = True
        with self.lock:
            if self.mpv:
                self.mpv.quit()

    def restart(self):
        """Manual restart from the phone. Clears the crash-loop guard."""
        with self.lock:
            self.gave_up = False
            self.restarts.clear()
            self.last_error = ""
            if self.mpv:
                self.mpv.quit()
            self._launch()
        self._resume()

    def _watchdog(self):
        while not self.shutting_down:
            time.sleep(3)
            restarted = False
            with self.lock:
                if self.shutting_down:
                    continue
                if self.gave_up:
                    if time.time() >= self.retry_at:
                        log.info("Trying the video player again after the pause")
                        self.gave_up = False
                        self.restarts.clear()
                    else:
                        continue
                if self.mpv and self.mpv.alive():
                    if self._stalled():
                        log.error("Playback froze for 30 seconds, restarting mpv")
                        self.alerts.send("stall", "Balcony: playback froze",
                                         "The video froze and the player is being restarted.")
                        self.mpv.kill()
                    else:
                        continue
                recent = [t for t in self.restarts if time.time() - t < 120]
                if len(recent) >= 4:
                    self.gave_up = True
                    self.retry_at = time.time() + 120
                    self.last_error = ("The video player keeps crashing. It will try again in two "
                                       "minutes, or tap 'Restart the player' now. If the projector "
                                       "is off, that may be why.")
                    log.error("mpv crashed %d times in two minutes, pausing for two minutes", len(recent))
                    self.alerts.send("crashloop", "Balcony: player keeps crashing", self.last_error)
                    continue
                log.warning("mpv is not running, restarting it")
                try:
                    if self.mpv:
                        self.mpv.kill()
                    self._launch()
                    restarted = True
                except MPVError as exc:
                    self.last_error = "Couldn't start the video player: %s" % exc
                    log.error(self.last_error)
                    continue
            if restarted:
                if self.state.get("mode") == "loop" and self.state.get("playlist"):
                    try:
                        self.play(self.state.get("playlist"))
                    except (ValueError, MPVError) as exc:
                        self.last_error = str(exc)
                elif self.state.get("mode") == "once":
                    self.state.update(mode="off", file=None)

    def _stalled(self):
        """True when something should be playing but the clock hasn't moved for 30 s."""
        if self.state.get("mode") == "off":
            self._stall_pos, self._stall_since = None, time.time()
            return False
        if self.mpv.get("pause", False) or self.mpv.get("idle-active", False):
            self._stall_since = time.time()
            return False
        pos = self.mpv.get("time-pos")
        if pos is None or pos != self._stall_pos:
            self._stall_pos, self._stall_since = pos, time.time()
            return False
        return time.time() - self._stall_since > 30

    def _event_loop(self):
        while not self.shutting_down:
            if self.mpv is None:
                time.sleep(0.5)
                continue
            try:
                ev = self.mpv.events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._on_event(ev)
            except Exception:  # never let the event thread die
                log.exception("Error handling mpv event %r", ev)

    def _on_event(self, ev):
        name = ev.get("event")
        if name == "end-file" and ev.get("reason") == "error":
            path = self.mpv.get("path") or ""
            base = os.path.basename(path) if path else "a file"
            reason = ev.get("file_error", "unknown problem")
            self.file_errors[base] = reason
            log.error("Could not play %s: %s", base, reason)
        elif name == "file-loaded":
            path = self.mpv.get("path") or ""
            self.file_errors.pop(os.path.basename(path), None)
        elif name == "idle":
            if self.ignore_idle:
                return
            with self.lock:
                mode = self.state.get("mode")
                if mode == "once":
                    log.info("Movie finished, screen is dark")
                    self._went_dark()
                elif mode == "loop":
                    label = self.state.get("playlist")
                    self.last_error = ("Nothing in '%s' would play, so the screen went dark. "
                                       "The files may be in a format the Pi can't play; "
                                       "see the log for details." % label)
                    log.error(self.last_error)
                    self.alerts.send("idle", "Balcony: the show stopped", self.last_error)
                    self._went_dark()
                self.ignore_idle = True

    def _went_dark(self):
        self.state.update(mode="off", file=None)
        self.segments = []
        self._mute(True)

    def _mute(self, on):
        pcfg = self.cfg.data["projector"]
        if self.projector and pcfg.get("enabled") and pcfg.get("mute_when_dark"):
            threading.Thread(target=self.projector.mute_quietly, args=(on,), daemon=True).start()

    # ---- controls --------------------------------------------------------

    def _need_mpv(self):
        if self.gave_up:
            raise MPVError(self.last_error or "The video player has stopped.")
        if not (self.mpv and self.mpv.alive()):
            raise MPVError("The video player isn't running yet. Wait a few seconds and try again.")

    def play(self, playlist, filename=None):
        info = self.lib.playlist(playlist)
        if info is None:
            raise ValueError("There is no playlist called '%s'." % playlist)
        if not info["exists"]:
            raise ValueError("The folder %s doesn't exist yet. Create it and copy videos into it."
                             % info["dir"])
        segments = []
        loop_file = "no"
        if filename:
            match = [f for f in info["files"] if f["name"] == filename]
            if not match:
                raise ValueError("'%s' isn't in the %s folder any more." % (filename, info["label"]))
            if match[0]["status"] == "waiting":
                raise ValueError(match[0]["error"] or "That file is waiting for the 'paid for by' line.")
            paths = [os.path.join(info["dir"], filename)]
            mode = "once"
        else:
            mode = info["mode"]
            if mode == "once":
                raise ValueError("Pick one movie from the %s list." % info["label"])
            shuffle = bool(self.state.get("shuffle"))
            show = None
            if self.cfg.data["processing"].get("render_shows", True) and not shuffle:
                show = self.lib.show_for(playlist)
            if show:
                paths = [show["path"]]
                segments = show["segments"]
                loop_file = "inf"
            else:
                paths = self.lib.playable(playlist)
                if not paths:
                    if info["files"]:
                        raise ValueError("Everything in %s is switched off or still waiting. "
                                         "Turn a file on under Manage videos." % info["label"])
                    raise ValueError("The %s folder is empty. Copy videos into %s first."
                                     % (info["label"], info["dir"]))
                if len(paths) == 1:
                    loop_file = "inf"

        with self.lock:
            self._need_mpv()
            self.ignore_idle = True
            self.mpv.set("loop-file", loop_file)
            self.mpv.set("loop-playlist", "inf" if mode == "loop" else "no")
            self.mpv.command("loadfile", paths[0], "replace")
            for path in paths[1:]:
                self.mpv.command("loadfile", path, "append")
            if mode == "loop" and self.state.get("shuffle") and len(paths) > 1:
                self.mpv.command("playlist-shuffle")
            self.mpv.set("pause", False)
            self.ignore_idle = False
            self.last_error = ""
            self.segments = segments
            self.state.update(mode=mode, playlist=playlist,
                              file=filename if mode == "once" else None)
        self._mute(False)
        log.info("Playing %s (%s, %d file%s%s)", playlist, mode, len(paths),
                 "" if len(paths) == 1 else "s", ", stitched show" if segments else "")

    def stop(self):
        with self.lock:
            self.ignore_idle = True
            self.state.update(mode="off", file=None)
            self.segments = []
            if self.mpv and self.mpv.alive():
                self.mpv.command("stop")
        self._mute(True)
        log.info("Stopped, screen is dark")

    def set_pause(self, paused):
        with self.lock:
            self._need_mpv()
            self.mpv.set("pause", bool(paused))

    def skip(self, direction):
        with self.lock:
            self._need_mpv()
            if self.segments:
                # a seek sent before the file has finished loading is dropped by mpv
                for _ in range(20):
                    if self.mpv.get("duration") is not None:
                        break
                    time.sleep(0.1)
                pos = self.mpv.get("time-pos") or 0.0
                starts = [s["start"] for s in self.segments]
                if direction > 0:
                    later = [s for s in starts if s > pos + 0.5]
                    target = later[0] if later else 0.0
                else:
                    earlier = [s for s in starts if s < pos - 2.0]
                    target = earlier[-1] if earlier else starts[-1]
                self.mpv.command("seek", target, "absolute+exact")
            else:
                self.mpv.command("playlist-next" if direction > 0 else "playlist-prev", "weak")

    def set_shuffle(self, on):
        """Shuffle the order of a looping playlist. Reloads it if it is playing."""
        self.state.update(shuffle=bool(on))
        if self.state.get("mode") == "loop" and self.state.get("playlist"):
            self.play(self.state.get("playlist"))

    def set_volume(self, volume):
        volume = max(0, min(100, int(volume)))
        with self.lock:
            self.state.update(volume=volume)
            if self.mpv and self.mpv.alive():
                self.mpv.set("volume", volume)
        return volume

    def set_audio_device(self, device):
        with self.lock:
            if self.mpv and self.mpv.alive():
                self.mpv.set("audio-device", device or "auto")

    def set_image_seconds(self, seconds):
        with self.lock:
            if self.mpv and self.mpv.alive():
                self.mpv.set("image-display-duration", int(seconds))

    def audio_devices(self):
        with self.lock:
            if not (self.mpv and self.mpv.alive()):
                return []
            devices = self.mpv.get("audio-device-list") or []
        return [{"name": d.get("name"), "description": d.get("description")} for d in devices]

    def apply_dim(self, force=False):
        """Quiet-hours dimming: mpv's brightness goes negative to darken the picture."""
        dcfg = self.cfg.data.get("dim", {})
        level = 100
        try:
            if dcfg.get("enabled") and _in_window(dcfg.get("start", "22:00"), dcfg.get("end", "23:59")):
                level = max(10, min(100, int(dcfg.get("level", 60))))
        except ValueError:
            level = 100
        if level == self.dim_level and not force:
            return
        with self.lock:
            if self.mpv and self.mpv.alive():
                try:
                    self.mpv.set("brightness", -(100 - level))
                    self.dim_level = level
                except MPVError as exc:
                    log.warning("Could not set brightness: %s", exc)

    # ---- reporting -------------------------------------------------------

    def status(self):
        running = bool(self.mpv and self.mpv.alive())
        out = {
            "player_running": running,
            "mode": self.state.get("mode"),
            "playlist": self.state.get("playlist"),
            "file": None,
            "paused": False,
            "volume": self.state.get("volume") if self.state.get("volume") is not None
            else self.cfg.data.get("volume", 80),
            "position": None,
            "duration": None,
            "playlist_count": 0,
            "playlist_pos": None,
            "show": bool(self.segments),
            "shuffle": bool(self.state.get("shuffle")),
            "dim_level": self.dim_level,
        }
        if not running or self.state.get("mode") == "off":
            return out
        m = self.mpv
        path = m.get("path")
        out["paused"] = bool(m.get("pause", False))
        out["position"] = m.get("time-pos")
        out["duration"] = m.get("duration")
        if self.segments:
            pos = out["position"] or 0.0
            idx = 0
            for i, seg in enumerate(self.segments):
                if pos >= seg["start"]:
                    idx = i
            out["file"] = self.segments[idx]["name"] if (path and out["duration"]) else None
            out["playlist_count"] = len(self.segments)
            out["playlist_pos"] = idx
        else:
            out["file"] = os.path.basename(path) if path else None
            out["playlist_count"] = m.get("playlist-count", 0) or 0
            out["playlist_pos"] = m.get("playlist-pos")
        return out

    def recent_mpv_output(self):
        if self.mpv is None:
            return []
        return list(self.mpv.stderr_lines)


# --------------------------------------------------------------------------
# Projector over PJLink (class 1)
# --------------------------------------------------------------------------

class Projector:
    POWER_NAMES = {"0": "off", "1": "on", "2": "cooling", "3": "warming"}
    ERST_PARTS = ("fan", "lamp", "temperature", "cover", "filter", "other")

    def __init__(self, cfg, alerts):
        self.cfg = cfg
        self.alerts = alerts
        self.lock = threading.Lock()
        self.busy = ""             # plain-language note while switching
        self.cached = {"power": "unknown", "reachable": False, "lamp_hours": None,
                       "error": "", "errors": [], "muted": False, "checked": None}
        self.stop_polling = False

    @property
    def enabled(self):
        return bool(self.cfg.data["projector"].get("enabled"))

    # ---- protocol --------------------------------------------------------

    def _send(self, body):
        """Send one PJLink command like 'POWR 1' and return the response body."""
        pcfg = self.cfg.data["projector"]
        host, port = pcfg.get("host", "192.168.50.2"), int(pcfg.get("port", 4352))
        with socket.create_connection((host, port), timeout=4) as sock:
            sock.settimeout(6)
            greeting = self._read_line(sock)
            prefix = ""
            if greeting.startswith("PJLINK 1"):
                parts = greeting.split()
                seed = parts[2] if len(parts) > 2 else ""
                prefix = hashlib.md5((seed + pcfg.get("password", "")).encode()).hexdigest()
            elif not greeting.startswith("PJLINK 0"):
                raise OSError("unexpected greeting %r" % greeting)
            sock.sendall((prefix + "%1" + body + "\r").encode())
            reply = self._read_line(sock)
        if reply.upper().startswith("PJLINK ERRA"):
            raise OSError("The projector rejected the PJLink password.")
        if "=" not in reply:
            raise OSError("unexpected reply %r" % reply)
        return reply.split("=", 1)[1].strip()

    @staticmethod
    def _read_line(sock):
        buf = b""
        while not buf.endswith(b"\r"):
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace").strip()

    @staticmethod
    def _explain(code):
        return {
            "ERR1": "The projector doesn't understand that command.",
            "ERR2": "The projector rejected that setting.",
            "ERR3": "The projector is busy warming up or cooling down. Try again in a minute.",
            "ERR4": "The projector reported a fault. Check the projector itself.",
        }.get(code.upper(), "The projector answered: %s" % code)

    def _unreachable(self, exc):
        host = self.cfg.data["projector"].get("host")
        return ("Can't reach the projector at %s (%s). Check the Ethernet cable between "
                "the Pi and the projector, and that the projector is plugged in." % (host, exc))

    # ---- actions ---------------------------------------------------------

    def refresh(self):
        """Ask the projector how it is doing. Called from the poll thread."""
        if not self.enabled:
            return
        result = dict(self.cached)
        result["checked"] = datetime.now().isoformat(timespec="seconds")
        try:
            code = self._send("POWR ?")
            result["reachable"] = True
            result["power"] = self.POWER_NAMES.get(code, "unknown")
            result["error"] = "" if code in self.POWER_NAMES else self._explain(code)
            try:
                lamp = self._send("LAMP ?")
                result["lamp_hours"] = int(lamp.split()[0])
            except (OSError, ValueError, IndexError):
                pass
            try:
                result["errors"] = self._parse_erst(self._send("ERST ?"))
            except OSError:
                pass
            try:
                mute = self._send("AVMT ?")
                result["muted"] = mute in ("11", "21", "31")
            except OSError:
                pass
        except OSError as exc:
            result.update(reachable=False, power="unknown", error=self._unreachable(exc))
        with self.lock:
            self.cached = result
        for text in result.get("errors", []):
            if text.startswith("Fault"):
                self.alerts.send("projector-" + text[:20], "Balcony: projector fault", text)

    def _parse_erst(self, code):
        out = []
        for part, ch in zip(self.ERST_PARTS, code.strip()[:6]):
            if ch == "1":
                out.append("Warning from the projector's %s. Keep an eye on it." % part)
            elif ch == "2":
                out.append("Fault in the projector's %s. Check the projector." % part)
        return out

    def power(self, on):
        """Turn the projector on or off. Returns a plain-language message."""
        if not self.enabled:
            raise ValueError("Projector control is turned off in Settings.")
        try:
            code = self._send("POWR 1" if on else "POWR 0")
        except OSError as exc:
            msg = self._unreachable(exc)
            with self.lock:
                self.cached.update(reachable=False, error=msg)
            raise ValueError(msg)
        if code.upper() != "OK":
            raise ValueError(self._explain(code))
        threading.Thread(target=self._settle, args=(on,), daemon=True).start()
        return ("Turning the projector on. It takes about a minute to warm up."
                if on else "Turning the projector off. It will cool down for a minute or two.")

    def mute(self, on):
        if not self.enabled:
            raise ValueError("Projector control is turned off in Settings.")
        try:
            code = self._send("AVMT 31" if on else "AVMT 30")
        except OSError as exc:
            raise ValueError(self._unreachable(exc))
        if code.upper() != "OK":
            raise ValueError(self._explain(code))
        with self.lock:
            self.cached["muted"] = bool(on)
        return "Projector picture is blanked." if on else "Projector picture is back."

    def mute_quietly(self, on):
        try:
            self.mute(on)
        except ValueError as exc:
            log.warning("Could not %s the projector: %s", "blank" if on else "unblank", exc)

    def select_input(self, code):
        if not self.enabled:
            raise ValueError("Projector control is turned off in Settings.")
        try:
            reply = self._send("INPT " + str(code))
        except OSError as exc:
            raise ValueError(self._unreachable(exc))
        if reply.upper() != "OK":
            raise ValueError(self._explain(reply))
        return "Switched the projector input."

    def _settle(self, on):
        """Wait for warm-up or cool-down, then pick the HDMI input when turning on."""
        pcfg = self.cfg.data["projector"]
        self.busy = "Warming up..." if on else "Cooling down..."
        deadline = time.time() + int(pcfg.get("warmup_seconds", 60)) + 90
        target = "on" if on else "off"
        while time.time() < deadline:
            time.sleep(5)
            self.refresh()
            if self.cached["power"] == target:
                break
        if on and self.cached["power"] == "on" and pcfg.get("input"):
            try:
                self._send("INPT " + str(pcfg["input"]))
            except OSError as exc:
                log.warning("Could not select projector input: %s", exc)
        self.busy = ""

    def poll_forever(self):
        while not self.stop_polling:
            try:
                self.refresh()
            except Exception:
                log.exception("Projector poll failed")
            time.sleep(30)

    def status(self):
        with self.lock:
            out = dict(self.cached)
        pcfg = self.cfg.data["projector"]
        out.update(enabled=self.enabled, host=pcfg.get("host"), busy=self.busy,
                   lamp_warn_hours=int(pcfg.get("lamp_warn_hours", 3500)),
                   input=pcfg.get("input"), mute_when_dark=bool(pcfg.get("mute_when_dark")))
        if not self.enabled:
            out.update(power="unknown", reachable=False, error="", errors=[])
        return out


# --------------------------------------------------------------------------
# Alerts (ntfy) and Pi health
# --------------------------------------------------------------------------

class Alerts:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sent = {}
        self.lock = threading.Lock()
        self.last_result = ""

    def enabled(self):
        a = self.cfg.data.get("alerts", {})
        return bool(a.get("enabled") and a.get("ntfy_url"))

    def send(self, kind, title, message, min_gap=3600, force=False):
        if not self.enabled():
            return False
        with self.lock:
            last = self.sent.get(kind, 0)
            if not force and time.time() - last < min_gap:
                return False
            self.sent[kind] = time.time()
        threading.Thread(target=self._post, args=(title, message), daemon=True).start()
        return True

    def _post(self, title, message):
        url = self.cfg.data["alerts"]["ntfy_url"]
        req = urllib.request.Request(url, data=message.encode(), method="POST")
        req.add_header("Title", title)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                self.last_result = "sent (%d)" % res.status
        except Exception as exc:  # network trouble must never hurt playback
            self.last_result = "failed: %s" % exc
            log.warning("Alert not sent: %s", exc)


def pi_health(media_dir, started_at):
    out = {"cpu_temp": None, "throttled": [], "uptime_hours": None, "load": None,
           "disk_free_gb": None, "app_uptime_hours": round((time.time() - started_at) / 3600, 1)}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            out["cpu_temp"] = round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    if shutil.which("vcgencmd"):
        try:
            raw = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            bits = int(raw.split("=")[1], 16)
            names = {0x1: "The Pi's power supply is weak right now (under-voltage). Use the official power supply.",
                     0x4: "The Pi is slowing itself down because it is too hot.",
                     0x8: "The Pi is near its temperature limit.",
                     0x10000: "The power supply dipped at some point since boot.",
                     0x40000: "The Pi has been throttled by heat since boot."}
            out["throttled"] = [text for bit, text in names.items() if bits & bit]
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    try:
        with open("/proc/uptime") as f:
            out["uptime_hours"] = round(float(f.read().split()[0]) / 3600, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["load"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        target = media_dir if os.path.isdir(media_dir) else BASE_DIR
        out["disk_free_gb"] = round(shutil.disk_usage(target).free / 1024 ** 3, 1)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# Evening schedule
# --------------------------------------------------------------------------

class Scheduler:
    def __init__(self, cfg, state, player, projector, alerts):
        self.cfg = cfg
        self.state = state
        self.player = player
        self.projector = projector
        self.alerts = alerts
        self.stop_running = False
        self.note = ""

    # ---- times -------------------------------------------------------------

    def resolve(self, spec, offset_minutes, day):
        """A datetime for `spec` ('sunset', 'sunrise' or 'HH:MM') on `day`."""
        spec = str(spec or "").strip().lower()
        if spec in ("sunset", "sunrise"):
            loc = self.cfg.data.get("location", {})
            rise, sset = solar.sun_times(day, float(loc.get("lat", 32.78)), float(loc.get("lon", -96.80)))
            base = sset if spec == "sunset" else rise
            if base is None:
                raise ValueError("The sun doesn't set there on %s." % day)
        else:
            h, m = _parse_hhmm(spec)
            base = datetime(day.year, day.month, day.day, h, m)
        return base + timedelta(minutes=int(offset_minutes or 0))

    def window(self, now=None):
        """(window_id, start, end) for the window containing `now`, or
        (None, next_start, next_end) when outside one."""
        sched = self.cfg.data["schedule"]
        now = now or datetime.now()
        candidates = []
        for day_offset in (-1, 0, 1):
            day = (now + timedelta(days=day_offset)).date()
            start = self.resolve(sched.get("start", "19:00"), sched.get("start_offset", 0), day)
            end = self.resolve(sched.get("end", "23:00"), sched.get("end_offset", 0), day)
            if end <= start:
                end += timedelta(days=1)
            candidates.append((start, end))
        for start, end in candidates:
            if start <= now < end:
                return start.strftime("%Y-%m-%d"), start, end
        for start, end in candidates:
            if start > now:
                return None, start, end
        return None, None, None

    def tonight_playlist(self, day=None):
        """Which playlist the season calendar picks for `day`."""
        day = day or date.today()
        for season in self.cfg.data.get("seasons", []):
            if self._season_covers(season, day):
                return season.get("playlist"), season.get("name")
        return self.cfg.data["schedule"].get("playlist", "halloween"), None

    @staticmethod
    def _season_covers(season, day):
        try:
            start, end = str(season.get("from", "")), str(season.get("to", ""))
            if len(start) == 5 and len(end) == 5:            # MM-DD, every year
                s = date(day.year, int(start[:2]), int(start[3:]))
                e = date(day.year, int(end[:2]), int(end[3:]))
                if e < s:                                    # wraps the new year
                    return day >= s or day <= e
                return s <= day <= e
            return date.fromisoformat(start) <= day <= date.fromisoformat(end)
        except ValueError:
            return False

    # ---- running ---------------------------------------------------------

    def run_forever(self):
        while not self.stop_running:
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            try:
                self.player.apply_dim()
            except Exception:
                log.exception("Dimming failed")
            time.sleep(20)

    def tick(self):
        sched = self.cfg.data["schedule"]
        if not sched.get("enabled"):
            return
        window_id, _, _ = self.window()
        last = self.state.get("last_window_id")
        if window_id and window_id != last:
            self.state.update(last_window_id=window_id)
            threading.Thread(target=self._evening_start, daemon=True).start()
        elif window_id is None and last is not None:
            self.state.update(last_window_id=None)
            threading.Thread(target=self._evening_end, daemon=True).start()

    def _evening_start(self):
        sched = self.cfg.data["schedule"]
        playlist, season = self.tonight_playlist()
        log.info("Schedule: evening starts (%s%s)", playlist, ", " + season if season else "")
        self.note = "Starting tonight's show..."
        if sched.get("projector_power") and self.projector.enabled:
            try:
                self.projector.power(True)
                deadline = time.time() + int(self.cfg.data["projector"].get("warmup_seconds", 60)) + 60
                while time.time() < deadline and self.projector.status()["power"] != "on":
                    time.sleep(5)
            except ValueError as exc:
                log.error("Schedule: projector did not turn on: %s", exc)
                self.note = "Couldn't turn the projector on: %s" % exc
                self.alerts.send("sched-projector", "Balcony: projector didn't turn on", str(exc))
        if self.state.get("mode") == "off":
            try:
                self.player.play(playlist)
                self.note = ""
            except (ValueError, MPVError) as exc:
                self.player.last_error = "Tonight's show didn't start: %s" % exc
                self.note = ""
                log.error(self.player.last_error)
                self.alerts.send("sched-play", "Balcony: tonight's show didn't start", str(exc))
        else:
            log.info("Schedule: something is already playing, leaving it alone")
            self.note = ""

    def _evening_end(self):
        log.info("Schedule: evening ends")
        try:
            self.player.stop()
        except MPVError as exc:
            log.warning("Schedule: could not stop playback: %s", exc)
        if self.cfg.data["schedule"].get("projector_power") and self.projector.enabled:
            try:
                self.projector.power(False)
            except ValueError as exc:
                log.error("Schedule: projector did not turn off: %s", exc)

    def status(self):
        sched = dict(self.cfg.data["schedule"])
        sched["seasons"] = self.cfg.data.get("seasons", [])
        sched["location"] = self.cfg.data.get("location", {})
        playlist, season = self.tonight_playlist()
        sched.update(tonight_playlist=playlist, tonight_season=season)
        try:
            window_id, start, end = self.window()
            sched.update(
                active=window_id is not None,
                next_start=start.isoformat(timespec="minutes") if start else None,
                next_end=end.isoformat(timespec="minutes") if end else None,
            )
        except ValueError as exc:
            sched.update(active=False, next_start=None, next_end=None, error=str(exc))
        try:
            loc = self.cfg.data.get("location", {})
            rise, sset = solar.sun_times(date.today(), float(loc.get("lat", 32.78)), float(loc.get("lon", -96.80)))
            sched["sunrise_today"] = rise.isoformat(timespec="minutes") if rise else None
            sched["sunset_today"] = sset.isoformat(timespec="minutes") if sset else None
        except (ValueError, TypeError):
            sched["sunrise_today"] = sched["sunset_today"] = None
        sched["note"] = self.note
        return sched


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

def create_app(cfg, state, lib, processor, player, projector, scheduler, alerts, ring):
    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3
    app.config["JSON_SORT_KEYS"] = False
    app.config["TEMPLATES_AUTO_RELOAD"] = True     # edits to index.html show without a restart
    started_at = time.time()

    def fail(message, code=400):
        return jsonify({"ok": False, "error": message}), code

    @app.before_request
    def check_pin():
        if not request.path.startswith("/api/"):
            return None
        pin = str(cfg.data.get("pin") or "")
        if not pin:
            return None
        given = request.headers.get("X-Pin", "")
        if not given and request.path.startswith("/api/thumb/"):
            given = request.args.get("pin", "")
        if not hmac.compare_digest(given.encode(), pin.encode()):
            return jsonify({"ok": False, "pin_required": True,
                            "error": "This remote needs the PIN."}), 401
        return None

    @app.after_request
    def allow_studio(response):
        # The laptop studio page (a local file or localhost) talks to this API directly.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Pin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    @app.route("/api/<path:_any>", methods=["OPTIONS", "GET", "POST"])
    def api_preflight(_any):
        if request.method == "OPTIONS":
            return ("", 204)
        return fail("There's nothing at that address.", 404)

    @app.route("/")
    def index():
        return render_template("index.html")

    # The laptop studio is served from the Pi too, so any browser on the
    # Wi-Fi can open http://balcony.local:8080/studio/ with nothing to install.
    studio_dir = os.path.join(BASE_DIR, "tools", "studio")

    @app.route("/studio/")
    def studio_index():
        return send_from_directory(os.path.join(studio_dir, "slides"), "index.html")

    @app.route("/studio/<path:name>")
    def studio_file(name):
        return send_from_directory(studio_dir, name)

    def problems():
        out = []
        root = cfg.media_dir
        playlists = lib.playlists()
        if not os.path.isdir(root):
            out.append("The media folder %s doesn't exist. Create it, with folders inside "
                       "named halloween, campaign and movies." % root)
        else:
            for pl in playlists:
                if not pl["configured"]:
                    continue
                if not pl["exists"]:
                    out.append("There's no '%s' folder in %s yet. Create it and copy videos into it."
                               % (pl["name"], root))
                elif not pl["files"]:
                    out.append("The %s folder is empty. Copy videos into %s."
                               % (pl["label"], pl["dir"]))
        if cfg.data["processing"].get("enabled") and not processor.available:
            out.append("ffmpeg isn't installed, so uploads play exactly as they are: no smooth "
                       "loops, hologram mode or pictures with motion. On the Pi run: "
                       "sudo apt install ffmpeg")
        for pl in playlists:
            if (processor.enabled() and pl["political"] and pl["exists"]
                    and not pl["disclaimer"] and pl["files"]):
                out.append("%s files won't play until the 'paid for by' line is set under Settings. "
                           "Texas requires it on political advertising." % pl["label"])
            for f in pl["files"]:
                if f["status"] == "failed":
                    out.append("Couldn't convert %s in %s: %s" % (f["source"], pl["label"], f["error"]))
                elif f["flash_warning"] and f["enabled"]:
                    out.append("%s in %s has about %d sudden brightness jumps a minute. That can "
                               "bother drivers and people with photosensitivity; consider switching "
                               "it off under Manage videos." % (f["label"], pl["label"], round(f["flash"])))
        if player.gave_up:
            out.append(player.last_error)
        elif not (player.mpv and player.mpv.alive()):
            out.append("The video player isn't running. It restarts by itself within a few seconds.")
        elif player.last_error:
            out.append(player.last_error)
        for name, why in list(player.file_errors.items())[-5:]:
            out.append("Couldn't play %s (%s). Try converting it to an MP4." % (name, why))
        pst = projector.status()
        if pst["enabled"]:
            if pst.get("error"):
                out.append(pst["error"])
            out.extend(pst.get("errors", []))
            if pst.get("lamp_hours") is not None and pst["lamp_hours"] >= pst["lamp_warn_hours"]:
                out.append("The projector lamp has run %s hours. Order a spare (Hitachi DT01411) "
                           "before it fails." % format(pst["lamp_hours"], ","))
        sst = scheduler.status()
        if sst.get("error"):
            out.append("The schedule times are wrong: %s" % sst["error"])
        if sst.get("note"):
            out.append(sst["note"])
        health = pi_health(root, started_at)
        if health["cpu_temp"] is not None and health["cpu_temp"] >= 80:
            out.append("The Pi is running hot (%d C). Give it some air, or add a small fan." % health["cpu_temp"])
            alerts.send("hot", "Balcony: the Pi is running hot", "%d C" % health["cpu_temp"])
        out.extend(health["throttled"][:2])
        if health["disk_free_gb"] is not None and health["disk_free_gb"] < 1:
            out.append("The Pi is almost out of space (%.1f GB left). Delete some videos." % health["disk_free_gb"])
            alerts.send("disk", "Balcony: almost out of space", "%.1f GB left" % health["disk_free_gb"])
        return out

    @app.route("/api/status")
    def api_status():
        return jsonify({
            "ok": True,
            "now": datetime.now().isoformat(timespec="seconds"),
            "player": player.status(),
            "projector": projector.status(),
            "schedule": scheduler.status(),
            "processing": processor.status(),
            "health": pi_health(cfg.media_dir, started_at),
            "problems": problems(),
            "pin_set": bool(cfg.data.get("pin")),
        })

    @app.route("/api/media")
    def api_media():
        return jsonify({"ok": True, "media_dir": cfg.media_dir, "playlists": lib.playlists(),
                        "processing": processor.status()})

    @app.route("/api/thumb/<playlist>/<path:name>")
    def api_thumb(playlist, name):
        path = os.path.abspath(lib.thumb_path(playlist, library.safe_filename(name)))
        if not os.path.isfile(path):
            return fail("No picture yet.", 404)
        try:
            response = send_file(path, mimetype="image/jpeg", conditional=True)
        except OSError:
            return fail("No picture yet.", 404)
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.route("/api/play", methods=["POST"])
    def api_play():
        body = request.get_json(silent=True) or {}
        try:
            player.play(str(body.get("playlist", "")), body.get("file") or None)
        except (ValueError, MPVError) as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "player": player.status()})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        try:
            player.stop()
        except MPVError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "player": player.status()})

    def _pause(paused):
        try:
            player.set_pause(paused)
        except MPVError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "player": player.status()})

    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        return _pause(True)

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        return _pause(False)

    def _skip(direction):
        try:
            player.skip(direction)
        except MPVError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "player": player.status()})

    @app.route("/api/next", methods=["POST"])
    def api_next():
        return _skip(1)

    @app.route("/api/previous", methods=["POST"])
    def api_previous():
        return _skip(-1)

    @app.route("/api/shuffle", methods=["POST"])
    def api_shuffle():
        body = request.get_json(silent=True) or {}
        try:
            player.set_shuffle(bool(body.get("shuffle")))
        except (ValueError, MPVError) as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "player": player.status()})

    @app.route("/api/volume", methods=["POST"])
    def api_volume():
        body = request.get_json(silent=True) or {}
        try:
            volume = player.set_volume(body.get("volume"))
        except (TypeError, ValueError):
            return fail("Volume needs to be a number from 0 to 100.")
        except MPVError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "volume": volume})

    @app.route("/api/restart_player", methods=["POST"])
    def api_restart_player():
        try:
            player.restart()
        except (ValueError, MPVError) as exc:
            return fail("Couldn't restart the player: %s" % exc)
        return jsonify({"ok": True, "player": player.status()})

    @app.route("/api/projector", methods=["POST"])
    def api_projector():
        body = request.get_json(silent=True) or {}
        try:
            if "power" in body:
                want = str(body.get("power", "")).lower()
                if want not in ("on", "off"):
                    return fail("Say whether the projector should be on or off.")
                message = projector.power(want == "on")
            elif "mute" in body:
                message = projector.mute(bool(body["mute"]))
            elif "input" in body:
                code = str(body["input"]).strip()
                if not re.fullmatch(r"[1-5][1-9]", code):
                    return fail("Pick an input from the list.")
                message = projector.select_input(code)
            else:
                return fail("Say what the projector should do.")
        except ValueError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "message": message, "projector": projector.status()})

    @app.route("/api/settings", methods=["GET"])
    def api_settings_get():
        return jsonify({
            "ok": True,
            "schedule": cfg.data["schedule"],
            "seasons": cfg.data.get("seasons", []),
            "location": cfg.data.get("location", {}),
            "dim": cfg.data.get("dim", {}),
            "processing": cfg.data.get("processing", {}),
            "processing_available": processor.available,
            "processing_on": processor.enabled(),
            "resume_on_boot": bool(cfg.data.get("resume_on_boot", True)),
            "encoder": processor.encoder,
            "alerts": {"enabled": bool(cfg.data["alerts"].get("enabled")),
                       "ntfy_url": cfg.data["alerts"].get("ntfy_url", ""),
                       "last_result": alerts.last_result},
            "projector": {k: v for k, v in cfg.data["projector"].items() if k != "password"},
            "projector_password_set": bool(cfg.data["projector"].get("password")),
            "audio_device": cfg.data.get("audio_device", "auto"),
            "audio_devices": player.audio_devices(),
            "image_seconds": cfg.data.get("image_seconds", 12),
            "media_dir": cfg.media_dir,
            "pin_set": bool(cfg.data.get("pin")),
            "playlists": [{"name": s["name"], "label": s["label"], "mode": s["mode"],
                           "political": s["political"], "disclaimer": s["disclaimer"],
                           "disclaimer_image": bool(lib.disclaimer_image(s["name"]))}
                          for s in lib.specs()],
        })

    @app.route("/api/settings", methods=["POST"])
    def api_settings_post():
        body = request.get_json(silent=True) or {}
        try:
            if "schedule" in body:
                sched = body["schedule"] or {}
                new = dict(cfg.data["schedule"])
                for key in ("start", "end"):
                    if key in sched:
                        value = str(sched[key]).strip().lower()
                        if value not in ("sunset", "sunrise"):
                            _parse_hhmm(value)
                        new[key] = value
                for key in ("start_offset", "end_offset"):
                    if key in sched:
                        minutes = int(sched[key])
                        if not -180 <= minutes <= 180:
                            raise ValueError("Offsets should be within three hours.")
                        new[key] = minutes
                if "playlist" in sched:
                    if lib.spec(str(sched["playlist"])) is None:
                        raise ValueError("There is no playlist called '%s'." % sched["playlist"])
                    new["playlist"] = str(sched["playlist"])
                for key in ("enabled", "projector_power"):
                    if key in sched:
                        new[key] = bool(sched[key])
                same_offsets = new.get("start_offset", 0) == new.get("end_offset", 0)
                if new["start"] == new["end"] and (same_offsets or new["start"][:1].isdigit()):
                    raise ValueError("The start and end times can't be the same.")
                cfg.data["schedule"] = new
            if "seasons" in body:
                seasons = []
                for raw in body["seasons"] or []:
                    start, end = str(raw.get("from", "")).strip(), str(raw.get("to", "")).strip()
                    for value in (start, end):
                        if not re.fullmatch(r"(\d{4}-)?\d{2}-\d{2}", value):
                            raise ValueError("Season dates should look like 10-01 or 2026-10-01.")
                    if (len(start) == 5) != (len(end) == 5):
                        raise ValueError("Use the same kind of date for both ends of a season.")
                    if lib.spec(str(raw.get("playlist", ""))) is None:
                        raise ValueError("There is no playlist called '%s'." % raw.get("playlist"))
                    seasons.append({"name": str(raw.get("name", "")).strip()[:40] or "Season",
                                    "from": start, "to": end, "playlist": str(raw["playlist"])})
                cfg.data["seasons"] = seasons
            if "location" in body:
                loc = body["location"] or {}
                lat, lon = float(loc.get("lat")), float(loc.get("lon"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Latitude and longitude don't look right.")
                cfg.data["location"] = {"lat": lat, "lon": lon, "name": str(loc.get("name", ""))[:40]}
            if "dim" in body:
                dim = body["dim"] or {}
                new = dict(cfg.data.get("dim", {}))
                if "enabled" in dim:
                    new["enabled"] = bool(dim["enabled"])
                for key in ("start", "end"):
                    if key in dim:
                        _parse_hhmm(dim[key])
                        new[key] = str(dim[key]).strip()
                if "level" in dim:
                    level = int(dim["level"])
                    if not 10 <= level <= 100:
                        raise ValueError("Dim level should be between 10 and 100 percent.")
                    new["level"] = level
                cfg.data["dim"] = new
                player.apply_dim(force=True)
            if "alerts" in body:
                a = body["alerts"] or {}
                new = dict(cfg.data["alerts"])
                if "enabled" in a:
                    new["enabled"] = bool(a["enabled"])
                if "ntfy_url" in a:
                    url = str(a["ntfy_url"]).strip()
                    if url and not re.fullmatch(r"https?://[^\s]+", url):
                        raise ValueError("The alert address should start with https://")
                    new["ntfy_url"] = url
                cfg.data["alerts"] = new
            if "processing" in body:
                p = body["processing"] or {}
                new = dict(cfg.data["processing"])
                for key in ("enabled", "render_shows"):
                    if key in p:
                        new[key] = bool(p[key])
                if "crossfade_seconds" in p:
                    fade = float(p["crossfade_seconds"])
                    if not 0.3 <= fade <= 5:
                        raise ValueError("Crossfades should be between 0.3 and 5 seconds.")
                    new["crossfade_seconds"] = fade
                if "encoder" in p:
                    enc = str(p["encoder"]).strip()
                    if enc not in ("auto", "libx264", "h264_v4l2m2m"):
                        raise ValueError("Encoder should be auto, libx264 or h264_v4l2m2m.")
                    new["encoder"] = enc
                cfg.data["processing"] = new
            if "projector" in body:
                proj = body["projector"] or {}
                new = dict(cfg.data["projector"])
                for key in ("enabled", "mute_when_dark"):
                    if key in proj:
                        new[key] = bool(proj[key])
                if "host" in proj:
                    host = str(proj["host"]).strip()
                    if not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
                        raise ValueError("The projector address should look like 192.168.50.2.")
                    new["host"] = host
                if "password" in proj:
                    new["password"] = str(proj["password"])
                if "input" in proj:
                    new["input"] = str(proj["input"]).strip()
                if "lamp_warn_hours" in proj:
                    new["lamp_warn_hours"] = max(100, int(proj["lamp_warn_hours"]))
                cfg.data["projector"] = new
            if "playlists" in body:
                for raw in body["playlists"] or []:
                    spec = lib.spec(str(raw.get("name", "")))
                    if spec is None:
                        raise ValueError("There is no playlist called '%s'." % raw.get("name"))
                    pcfg = cfg.data["playlists"].setdefault(spec["name"], {"label": spec["label"],
                                                                            "mode": spec["mode"]})
                    if "political" in raw:
                        pcfg["political"] = bool(raw["political"])
                    if "label" in raw:
                        pcfg["label"] = str(raw["label"]).strip()[:40] or spec["label"]
            if "resume_on_boot" in body:
                cfg.data["resume_on_boot"] = bool(body["resume_on_boot"])
            if "audio_device" in body:
                device = str(body["audio_device"] or "auto").strip()
                cfg.data["audio_device"] = device
                player.set_audio_device(device)
            if "image_seconds" in body:
                seconds = int(body["image_seconds"])
                if not 2 <= seconds <= 600:
                    raise ValueError("Picture time should be between 2 and 600 seconds.")
                cfg.data["image_seconds"] = seconds
                player.set_image_seconds(seconds)
            if "pin" in body:
                pin = str(body["pin"] or "").strip()
                if pin and not re.fullmatch(r"\d{4,8}", pin):
                    raise ValueError("The PIN should be 4 to 8 digits, or blank to turn it off.")
                cfg.data["pin"] = pin
        except (TypeError, ValueError) as exc:
            return fail(str(exc) if str(exc) else "Those settings didn't make sense.")
        cfg.save()
        if "projector" in body and projector.enabled:
            threading.Thread(target=projector.refresh, daemon=True).start()
        return api_settings_get()

    @app.route("/api/test_alert", methods=["POST"])
    def api_test_alert():
        if not alerts.enabled():
            return fail("Turn alerts on and enter an ntfy address first.")
        alerts.send("test", "Balcony Projector", "Test message. Alerts are working.", force=True)
        time.sleep(1.5)
        return jsonify({"ok": True, "message": "Test sent: %s" % (alerts.last_result or "sending...")})

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        playlist = str(request.form.get("playlist", ""))
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return fail("Choose a video or picture to upload.")
        dwell = request.form.get("dwell")
        try:
            dwell = int(dwell) if dwell else None
            if dwell is not None and not 2 <= dwell <= 600:
                raise ValueError
        except ValueError:
            return fail("Picture time should be between 2 and 600 seconds.")
        kind = "slide" if request.form.get("slide") else "upload"
        replace = bool(request.form.get("replace"))
        try:
            name = lib.add_file(playlist, upload.filename, upload, dwell=dwell, source_kind=kind, replace=replace)
        except LibraryError as exc:
            return fail(str(exc))
        except OSError as exc:
            return fail("Couldn't save the file: %s" % exc)
        log.info("Added %s to %s (%s)", name, playlist, kind)
        played = False
        if request.form.get("play"):
            try:
                player.play(playlist)
                played = True
            except (ValueError, MPVError) as exc:
                log.warning("Uploaded but could not start %s: %s", playlist, exc)
        return jsonify({"ok": True, "name": name, "playlist": playlist,
                        "processing": processor.enabled(), "playing": played})

    @app.route("/api/disclaimer", methods=["POST"])
    def api_disclaimer():
        playlist = str(request.form.get("playlist", ""))
        text = str(request.form.get("text", "")).strip()[:200]
        image = request.files.get("image")
        data = image.read() if image else b""
        if text and not data and not lib.disclaimer_image(playlist):
            return fail("The phone needs to send the rendered strip along with the text.")
        try:
            lib.set_disclaimer(playlist, text, data)
        except LibraryError as exc:
            return fail(str(exc))
        log.info("Disclaimer for %s set to %r", playlist, text)
        return jsonify({"ok": True})

    @app.route("/api/delete", methods=["POST"])
    def api_delete():
        body = request.get_json(silent=True) or {}
        name = library.safe_filename(str(body.get("file", "")))
        try:
            if lib.spec(str(body.get("playlist", ""))) is None or not name:
                raise LibraryError("Say which file to remove.")
            lib.remove(str(body["playlist"]), name)
        except LibraryError as exc:
            return fail(str(exc))
        except OSError as exc:
            return fail("Couldn't remove the file: %s" % exc)
        player.file_errors.pop(name, None)
        log.info("Removed %s from %s", name, body["playlist"])
        return jsonify({"ok": True})

    @app.route("/api/item", methods=["POST"])
    def api_item():
        body = request.get_json(silent=True) or {}
        name = library.safe_filename(str(body.get("file", "")))
        playlist = str(body.get("playlist", ""))
        if lib.spec(playlist) is None or not name:
            return fail("Say which file to change.")
        changes = {k: body[k] for k in ("enabled", "dwell", "hologram", "reprocess") if k in body}
        try:
            lib.set_item(playlist, name, changes)
        except (LibraryError, ValueError, TypeError) as exc:
            return fail(str(exc) or "That change didn't make sense.")
        return jsonify({"ok": True, "playlist": lib.playlist(playlist)})

    @app.route("/api/order", methods=["POST"])
    def api_order():
        body = request.get_json(silent=True) or {}
        playlist = str(body.get("playlist", ""))
        names = [library.safe_filename(str(n)) for n in (body.get("files") or [])]
        if lib.spec(playlist) is None or not names:
            return fail("Say which playlist to reorder.")
        lib.reorder(playlist, names)
        return jsonify({"ok": True, "playlist": lib.playlist(playlist)})

    @app.route("/api/log")
    def api_log():
        return jsonify({"ok": True, "app": list(ring.lines), "mpv": player.recent_mpv_output(),
                        "processing": processor.status()})

    @app.errorhandler(404)
    def not_found(_):
        if request.path.startswith("/api/"):
            return fail("There's nothing at that address.", 404)
        return "Not found", 404

    @app.errorhandler(413)
    def too_big(_):
        return fail("That file is too big to upload this way. Copy it over the network instead.", 413)

    return app


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Balcony Projector server")
    parser.add_argument("--config", default=os.environ.get(
        "BALCONY_CONFIG", os.path.join(BASE_DIR, "config.json")))
    parser.add_argument("--state", default=os.environ.get(
        "BALCONY_STATE", os.path.join(BASE_DIR, "state.json")))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(argv)

    ring = RingLogHandler()
    ring.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger().addHandler(ring)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    cfg = Config(args.config)
    state = State(args.state)
    alerts = Alerts(cfg)
    lib = Library(cfg)
    processor = Processor(lib, cfg)
    player = Player(cfg, state, lib, alerts)
    projector = Projector(cfg, alerts)
    player.projector = projector
    scheduler = Scheduler(cfg, state, player, projector, alerts)

    processor.start()
    try:
        player.start()
    except MPVError as exc:
        # Keep serving the phone page so the error is visible somewhere.
        player.last_error = "Couldn't start the video player: %s" % exc
        log.error(player.last_error)

    threading.Thread(target=projector.poll_forever, daemon=True).start()
    threading.Thread(target=scheduler.run_forever, daemon=True).start()

    app = create_app(cfg, state, lib, processor, player, projector, scheduler, alerts, ring)

    def on_signal(signum, _frame):
        log.info("Shutting down (signal %d)", signum)
        processor.stop()
        player.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    port = args.port or int(cfg.data.get("port", 8080))
    log.info("Phone remote at http://%s:%d/", socket.gethostname(), port)
    tls = cfg.data.get("tls", {})
    if tls.get("enabled", True):
        cert = ensure_certificate(os.path.join(BASE_DIR, "tls"))
        if cert:
            tls_port = int(tls.get("port", 8443))
            log.info("Studio at https://%s:%d/studio/", socket.gethostname(), tls_port)
            threading.Thread(target=serve_https, args=(app, args.host, tls_port, cert), daemon=True).start()
    app.run(host=args.host, port=port, threaded=True, use_reloader=False)


def ensure_certificate(folder):
    """A self-signed certificate for the HTTPS port, made once with openssl.
    Browsers warn about it the first time; that is expected."""
    crt, key = os.path.join(folder, "balcony.crt"), os.path.join(folder, "balcony.key")
    if os.path.isfile(crt) and os.path.isfile(key):
        return crt, key
    if not shutil.which("openssl"):
        log.warning("openssl not found; no HTTPS port (the studio needs to be opened as a file instead)")
        return None
    os.makedirs(folder, exist_ok=True)
    host = socket.gethostname().split(".")[0] or "balcony"
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
           "-keyout", key, "-out", crt, "-subj", "/CN=%s.local" % host,
           "-addext", "subjectAltName=DNS:%s.local,DNS:%s" % (host, host)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        os.chmod(key, 0o600)
        log.info("Made a self-signed certificate for %s.local", host)
        return crt, key
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not make a certificate: %s", exc)
        return None


def serve_https(app, host, port, cert):
    from werkzeug.serving import make_server
    try:
        server = make_server(host, port, app, threaded=True, ssl_context=cert)
        server.serve_forever()
    except Exception as exc:  # the HTTP port keeps working regardless
        log.error("HTTPS port %d failed: %s", port, exc)


if __name__ == "__main__":
    main()
