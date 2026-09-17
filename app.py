#!/usr/bin/env python3
"""
Balcony Projector - everything server side.

One file: the mpv wrapper, the player, the projector (PJLink), the evening
scheduler, and the Flask routes the phone page talks to.

Rules that matter here:
  * Nothing is ever drawn on the screen except the video. Errors go to the
    phone page, as the `problems` list in /api/status.
  * Plain Python + Flask only, so `apt install python3-flask mpv` is enough.
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
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".mpg", ".mpeg",
             ".ts", ".m2ts", ".wmv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT

DEFAULT_CONFIG = {
    "port": 8080,
    "pin": "",
    "media_dir": "~/media",
    "playlists": {
        "halloween": {"label": "Halloween", "mode": "loop"},
        "campaign": {"label": "Campaign", "mode": "loop"},
        "movies": {"label": "Movies", "mode": "once"},
    },
    "volume": 80,
    "audio_device": "auto",
    "image_seconds": 12,
    "mpv_video_args": ["--vo=gpu", "--gpu-context=drm", "--hwdec=auto-safe"],
    "mpv_extra_args": [],
    "schedule": {
        "enabled": False,
        "start": "19:00",
        "end": "23:00",
        "playlist": "halloween",
        "projector_power": True,
    },
    "projector": {
        "enabled": False,
        "host": "192.168.50.2",
        "port": 4352,
        "password": "",
        "input": "31",
        "warmup_seconds": 60,
    },
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


def _natural_key(name):
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", name)]


def _parse_hhmm(text):
    """'19:30' -> (19, 30). Raises ValueError with a plain message."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not match:
        raise ValueError("Times need to look like 19:30 (hours:minutes, 24-hour clock).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Times need to look like 19:30 (hours:minutes, 24-hour clock).")
    return hour, minute


def _safe_filename(name):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^A-Za-z0-9 ._()\-]+", "_", name)
    return name.strip(". ")


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
        return os.path.expanduser(self.data["media_dir"])


class State:
    DEFAULTS = {
        "mode": "off",          # off | loop | once
        "playlist": None,
        "file": None,
        "volume": None,
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
# Media folders
# --------------------------------------------------------------------------

class Media:
    def __init__(self, cfg):
        self.cfg = cfg

    def playlists(self):
        """Configured playlists first, then any other folder under media_dir."""
        root = self.cfg.media_dir
        out = []
        seen = set()
        for name, spec in self.cfg.data["playlists"].items():
            seen.add(name)
            out.append(self._describe(name, spec.get("label", name.title()),
                                      spec.get("mode", "loop"), configured=True))
        if os.path.isdir(root):
            for name in sorted(os.listdir(root), key=_natural_key):
                if name in seen or name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(root, name)):
                    out.append(self._describe(name, name.replace("_", " ").title(),
                                              "loop", configured=False))
        return out

    def playlist(self, name):
        for item in self.playlists():
            if item["name"] == name:
                return item
        return None

    def _describe(self, name, label, mode, configured):
        folder = os.path.join(self.cfg.media_dir, name)
        files = []
        if os.path.isdir(folder):
            for entry in sorted(os.listdir(folder), key=_natural_key):
                ext = os.path.splitext(entry)[1].lower()
                if entry.startswith(".") or ext not in MEDIA_EXT:
                    continue
                path = os.path.join(folder, entry)
                if not os.path.isfile(path):
                    continue
                files.append({
                    "name": entry,
                    "kind": "image" if ext in IMAGE_EXT else "video",
                    "bytes": os.path.getsize(path),
                })
        return {
            "name": name,
            "label": label,
            "mode": mode,
            "dir": folder,
            "exists": os.path.isdir(folder),
            "configured": configured,
            "files": files,
        }


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
    def __init__(self, cfg, state, media):
        self.cfg = cfg
        self.state = state
        self.media = media
        self.lock = threading.RLock()
        self.mpv = None
        self.socket_path = os.environ.get(
            "BALCONY_MPV_SOCKET", os.path.join(BASE_DIR, "mpv.sock"))
        self.shutting_down = False
        self.gave_up = False
        self.restarts = deque(maxlen=20)
        self.ignore_idle = True          # true until we load something
        self.last_error = ""             # plain language, for the phone
        self.file_errors = {}            # basename -> what went wrong
        self.started_at = None

    # ---- lifecycle -------------------------------------------------------

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
        args += list(self.cfg.data.get("mpv_video_args", []))
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
        if self.state.get("mode") == "loop" and self.state.get("playlist"):
            self.play(self.state.get("playlist"))

    def _watchdog(self):
        while not self.shutting_down:
            time.sleep(3)
            with self.lock:
                if self.shutting_down or self.gave_up:
                    continue
                if self.mpv and self.mpv.alive():
                    continue
                recent = [t for t in self.restarts if time.time() - t < 120]
                if len(recent) >= 4:
                    self.gave_up = True
                    self.last_error = ("The video player keeps crashing. Tap "
                                       "'Restart the player' to try again. "
                                       "If it keeps happening, check the log.")
                    log.error("mpv crashed %d times in two minutes, giving up", len(recent))
                    continue
                log.warning("mpv is not running, restarting it")
                try:
                    if self.mpv:
                        self.mpv.kill()
                    self._launch()
                except MPVError as exc:
                    self.last_error = "Couldn't start the video player: %s" % exc
                    log.error(self.last_error)
                    continue
            if self.state.get("mode") == "loop" and self.state.get("playlist"):
                try:
                    self.play(self.state.get("playlist"))
                except (ValueError, MPVError) as exc:
                    self.last_error = str(exc)
            elif self.state.get("mode") == "once":
                self.state.update(mode="off", file=None)

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
                    self.state.update(mode="off", file=None)
                elif mode == "loop":
                    label = self.state.get("playlist")
                    self.last_error = ("Nothing in '%s' would play, so the screen went dark. "
                                       "The files may be in a format the Pi can't play; "
                                       "see the log for details." % label)
                    log.error(self.last_error)
                    self.state.update(mode="off")
                self.ignore_idle = True

    # ---- controls --------------------------------------------------------

    def _need_mpv(self):
        if self.gave_up:
            raise MPVError(self.last_error or "The video player has stopped.")
        if not (self.mpv and self.mpv.alive()):
            raise MPVError("The video player isn't running yet. Wait a few seconds and try again.")

    def play(self, playlist, filename=None):
        info = self.media.playlist(playlist)
        if info is None:
            raise ValueError("There is no playlist called '%s'." % playlist)
        if not info["exists"]:
            raise ValueError("The folder %s doesn't exist yet. Create it and copy videos into it."
                             % info["dir"])
        names = [f["name"] for f in info["files"]]
        if not names:
            raise ValueError("The %s folder is empty. Copy videos into %s first."
                             % (info["label"], info["dir"]))
        if filename:
            if filename not in names:
                raise ValueError("'%s' isn't in the %s folder any more." % (filename, info["label"]))
            names = [filename]
            mode = "once"
        else:
            mode = info["mode"]
            if mode == "once":
                raise ValueError("Pick one movie from the %s list." % info["label"])
        paths = [os.path.join(info["dir"], n) for n in names]

        with self.lock:
            self._need_mpv()
            self.ignore_idle = True
            self.mpv.set("loop-playlist", "inf" if mode == "loop" else "no")
            self.mpv.command("loadfile", paths[0], "replace")
            for path in paths[1:]:
                self.mpv.command("loadfile", path, "append")
            self.mpv.set("pause", False)
            self.ignore_idle = False
            self.last_error = ""
            self.state.update(mode=mode, playlist=playlist,
                              file=filename if mode == "once" else None)
        log.info("Playing %s (%s, %d files)", playlist, mode, len(paths))

    def stop(self):
        with self.lock:
            self.ignore_idle = True
            self.state.update(mode="off", file=None)
            if self.mpv and self.mpv.alive():
                self.mpv.command("stop")
        log.info("Stopped, screen is dark")

    def set_pause(self, paused):
        with self.lock:
            self._need_mpv()
            self.mpv.set("pause", bool(paused))

    def skip(self, direction):
        with self.lock:
            self._need_mpv()
            self.mpv.command("playlist-next" if direction > 0 else "playlist-prev", "weak")

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
        }
        if not running or self.state.get("mode") == "off":
            return out
        m = self.mpv
        path = m.get("path")
        out["file"] = os.path.basename(path) if path else None
        out["paused"] = bool(m.get("pause", False))
        out["position"] = m.get("time-pos")
        out["duration"] = m.get("duration")
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

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.busy = ""             # plain-language note while switching
        self.cached = {"power": "unknown", "reachable": False, "lamp_hours": None,
                       "error": "", "checked": None}
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
        except OSError as exc:
            result.update(reachable=False, power="unknown", error=self._unreachable(exc))
        with self.lock:
            self.cached = result

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
        out.update(enabled=self.enabled, host=pcfg.get("host"), busy=self.busy)
        if not self.enabled:
            out.update(power="unknown", reachable=False, error="")
        return out


# --------------------------------------------------------------------------
# Evening schedule
# --------------------------------------------------------------------------

class Scheduler:
    def __init__(self, cfg, state, player, projector):
        self.cfg = cfg
        self.state = state
        self.player = player
        self.projector = projector
        self.stop_running = False
        self.note = ""

    def window(self, now=None):
        """Return (window_id, start_dt, end_dt) for the window containing `now`,
        or (None, next_start, next_end) if we're outside one."""
        sched = self.cfg.data["schedule"]
        now = now or datetime.now()
        sh, sm = _parse_hhmm(sched.get("start", "19:00"))
        eh, em = _parse_hhmm(sched.get("end", "23:00"))
        start_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_today = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        overnight = end_today <= start_today

        candidates = []
        for day_offset in (-1, 0, 1):
            start = start_today + timedelta(days=day_offset)
            end = end_today + timedelta(days=day_offset + (1 if overnight else 0))
            candidates.append((start, end))
        for start, end in candidates:
            if start <= now < end:
                return start.strftime("%Y-%m-%d"), start, end
        for start, end in candidates:
            if start > now:
                return None, start, end
        return None, None, None

    def run_forever(self):
        while not self.stop_running:
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
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
        log.info("Schedule: evening starts")
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
        if self.state.get("mode") == "off":
            try:
                self.player.play(sched.get("playlist", "halloween"))
                self.note = ""
            except (ValueError, MPVError) as exc:
                self.player.last_error = "Tonight's show didn't start: %s" % exc
                self.note = ""
                log.error(self.player.last_error)
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
        try:
            window_id, start, end = self.window()
            sched.update(
                active=window_id is not None,
                next_start=start.isoformat(timespec="minutes") if start else None,
                next_end=end.isoformat(timespec="minutes") if end else None,
            )
        except ValueError as exc:
            sched.update(active=False, next_start=None, next_end=None, error=str(exc))
        sched["note"] = self.note
        return sched


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

def create_app(cfg, state, media, player, projector, scheduler, ring):
    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3
    app.config["JSON_SORT_KEYS"] = False

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
        if not hmac.compare_digest(given.encode(), pin.encode()):
            return jsonify({"ok": False, "pin_required": True,
                            "error": "This remote needs the PIN."}), 401
        return None

    @app.route("/")
    def index():
        return render_template("index.html")

    def problems():
        out = []
        root = cfg.media_dir
        if not os.path.isdir(root):
            out.append("The media folder %s doesn't exist. Create it, with folders inside "
                       "named halloween, campaign and movies." % root)
        else:
            for pl in media.playlists():
                if not pl["configured"]:
                    continue
                if not pl["exists"]:
                    out.append("There's no '%s' folder in %s yet. Create it and copy videos into it."
                               % (pl["name"], root))
                elif not pl["files"]:
                    out.append("The %s folder is empty. Copy videos into %s."
                               % (pl["label"], pl["dir"]))
        if player.gave_up:
            out.append(player.last_error)
        elif not (player.mpv and player.mpv.alive()):
            out.append("The video player isn't running. It restarts by itself within a few seconds.")
        elif player.last_error:
            out.append(player.last_error)
        for name, why in list(player.file_errors.items())[-5:]:
            out.append("Couldn't play %s (%s). Try converting it to an MP4." % (name, why))
        pst = projector.status()
        if pst["enabled"] and pst.get("error"):
            out.append(pst["error"])
        sst = scheduler.status()
        if sst.get("error"):
            out.append("The schedule times are wrong: %s" % sst["error"])
        if sst.get("note"):
            out.append(sst["note"])
        try:
            free = shutil.disk_usage(root if os.path.isdir(root) else BASE_DIR).free
            if free < 1024 ** 3:
                out.append("The Pi is almost out of space (%d MB left). Delete some videos."
                           % (free // 1024 ** 2))
        except OSError:
            pass
        return out

    @app.route("/api/status")
    def api_status():
        return jsonify({
            "ok": True,
            "now": datetime.now().isoformat(timespec="seconds"),
            "player": player.status(),
            "projector": projector.status(),
            "schedule": scheduler.status(),
            "problems": problems(),
            "pin_set": bool(cfg.data.get("pin")),
        })

    @app.route("/api/media")
    def api_media():
        return jsonify({"ok": True, "media_dir": cfg.media_dir, "playlists": media.playlists()})

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

    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        return _pause(True)

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        return _pause(False)

    def _pause(paused):
        try:
            player.set_pause(paused)
        except MPVError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "player": player.status()})

    @app.route("/api/next", methods=["POST"])
    def api_next():
        return _skip(1)

    @app.route("/api/previous", methods=["POST"])
    def api_previous():
        return _skip(-1)

    def _skip(direction):
        try:
            player.skip(direction)
        except MPVError as exc:
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
        want = str(body.get("power", "")).lower()
        if want not in ("on", "off"):
            return fail("Say whether the projector should be on or off.")
        try:
            message = projector.power(want == "on")
        except ValueError as exc:
            return fail(str(exc))
        return jsonify({"ok": True, "message": message, "projector": projector.status()})

    @app.route("/api/settings", methods=["GET"])
    def api_settings_get():
        return jsonify({
            "ok": True,
            "schedule": cfg.data["schedule"],
            "projector": {k: v for k, v in cfg.data["projector"].items() if k != "password"},
            "projector_password_set": bool(cfg.data["projector"].get("password")),
            "audio_device": cfg.data.get("audio_device", "auto"),
            "audio_devices": player.audio_devices(),
            "image_seconds": cfg.data.get("image_seconds", 12),
            "media_dir": cfg.media_dir,
            "pin_set": bool(cfg.data.get("pin")),
            "playlists": [{"name": n, "label": s.get("label", n), "mode": s.get("mode", "loop")}
                          for n, s in cfg.data["playlists"].items()],
        })

    @app.route("/api/settings", methods=["POST"])
    def api_settings_post():
        body = request.get_json(silent=True) or {}
        try:
            if "schedule" in body:
                sched = body["schedule"] or {}
                new = dict(cfg.data["schedule"])
                if "start" in sched:
                    _parse_hhmm(sched["start"])
                    new["start"] = sched["start"].strip()
                if "end" in sched:
                    _parse_hhmm(sched["end"])
                    new["end"] = sched["end"].strip()
                if "playlist" in sched:
                    if media.playlist(str(sched["playlist"])) is None:
                        raise ValueError("There is no playlist called '%s'." % sched["playlist"])
                    new["playlist"] = str(sched["playlist"])
                for key in ("enabled", "projector_power"):
                    if key in sched:
                        new[key] = bool(sched[key])
                if new["start"] == new["end"]:
                    raise ValueError("The start and end times can't be the same.")
                cfg.data["schedule"] = new
            if "projector" in body:
                proj = body["projector"] or {}
                new = dict(cfg.data["projector"])
                if "enabled" in proj:
                    new["enabled"] = bool(proj["enabled"])
                if "host" in proj:
                    host = str(proj["host"]).strip()
                    if not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
                        raise ValueError("The projector address should look like 192.168.50.2.")
                    new["host"] = host
                if "password" in proj:
                    new["password"] = str(proj["password"])
                if "input" in proj:
                    new["input"] = str(proj["input"]).strip()
                cfg.data["projector"] = new
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

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        playlist = str(request.form.get("playlist", ""))
        info = media.playlist(playlist)
        if info is None:
            return fail("Pick which playlist the file belongs to.")
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return fail("Choose a video or picture to upload.")
        name = _safe_filename(upload.filename)
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in MEDIA_EXT:
            return fail("That kind of file can't be played. Use MP4, MOV, MKV, JPG or PNG.")
        os.makedirs(info["dir"], exist_ok=True)
        target = os.path.join(info["dir"], name)
        try:
            upload.save(target + ".part")
            os.replace(target + ".part", target)
        except OSError as exc:
            return fail("Couldn't save the file: %s" % exc)
        log.info("Uploaded %s to %s", name, playlist)
        return jsonify({"ok": True, "name": name, "playlist": playlist})

    @app.route("/api/delete", methods=["POST"])
    def api_delete():
        body = request.get_json(silent=True) or {}
        info = media.playlist(str(body.get("playlist", "")))
        name = _safe_filename(str(body.get("file", "")))
        if info is None or not name:
            return fail("Say which file to remove.")
        target = os.path.join(info["dir"], name)
        if not os.path.isfile(target):
            return fail("That file is already gone.")
        try:
            os.remove(target)
        except OSError as exc:
            return fail("Couldn't remove the file: %s" % exc)
        player.file_errors.pop(name, None)
        log.info("Removed %s from %s", name, info["name"])
        return jsonify({"ok": True})

    @app.route("/api/log")
    def api_log():
        return jsonify({"ok": True, "app": list(ring.lines), "mpv": player.recent_mpv_output()})

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
    media = Media(cfg)
    player = Player(cfg, state, media)
    projector = Projector(cfg)
    scheduler = Scheduler(cfg, state, player, projector)

    try:
        player.start()
    except MPVError as exc:
        # Keep serving the phone page so the error is visible somewhere.
        player.last_error = "Couldn't start the video player: %s" % exc
        log.error(player.last_error)

    threading.Thread(target=projector.poll_forever, daemon=True).start()
    threading.Thread(target=scheduler.run_forever, daemon=True).start()

    app = create_app(cfg, state, media, player, projector, scheduler, ring)

    def on_signal(signum, _frame):
        log.info("Shutting down (signal %d)", signum)
        player.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    port = args.port or int(cfg.data.get("port", 8080))
    log.info("Phone remote at http://%s:%d/", socket.gethostname(), port)
    app.run(host=args.host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
