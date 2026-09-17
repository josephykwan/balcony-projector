#!/usr/bin/env python3
"""
End-to-end smoke test: real mpv (--vo=null), a fake PJLink projector, and every
API endpoint. Needs mpv on PATH and Flask importable.

    python3 tests/smoke_test.py

Makes a throwaway media folder with tiny pictures (and a tiny MP4 if ffmpeg is
around), starts app.py on a spare port with PIN 1234, and walks the API.
"""

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from fake_pjlink import FakeProjector  # noqa: E402

PIN = "1234"
failures = []


def check(name, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print("%s %s%s" % (mark, name, (" -- " + str(detail)) if detail and not condition else ""))
    if not condition:
        failures.append(name)


def tiny_png(path, rgb):
    w = h = 64
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def tiny_mp4(path):
    if not shutil.which("ffmpeg"):
        return False
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2:r=10",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", path]
    return subprocess.run(cmd).returncode == 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, path, body=None, pin=PIN, raw=None, content_type=None):
        req = urllib.request.Request(self.base + path)
        if pin:
            req.add_header("X-Pin", pin)
        data = None
        if raw is not None:
            data = raw
            req.add_header("Content-Type", content_type)
        elif body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=15) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode())
            except ValueError:
                return exc.code, {}

    def wait(self, predicate, seconds, what):
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            _, last = self.call("/api/status")
            if predicate(last):
                return last
            time.sleep(0.5)
        print("     timed out waiting for %s; last status: %s" % (what, json.dumps(last)[:400]))
        return last


def main():
    if not shutil.which("mpv"):
        print("mpv is not installed; cannot run the smoke test.")
        return 2

    work = tempfile.mkdtemp(prefix="balcony-test-")
    media = os.path.join(work, "media")
    for name in ("halloween", "campaign", "movies"):
        os.makedirs(os.path.join(media, name))
    tiny_png(os.path.join(media, "halloween", "ghost 1.png"), (10, 10, 10))
    tiny_png(os.path.join(media, "halloween", "ghost 2.png"), (20, 20, 20))
    tiny_png(os.path.join(media, "halloween", "ghost 10.png"), (30, 30, 30))
    tiny_png(os.path.join(media, "campaign", "slide.png"), (0, 0, 40))
    have_mp4 = tiny_mp4(os.path.join(media, "movies", "short.mp4"))
    tiny_png(os.path.join(media, "movies", "poster.png"), (40, 0, 0))
    with open(os.path.join(media, "halloween", "notes.txt"), "w") as f:
        f.write("not media\n")

    projector = FakeProjector(password="secret", warm_seconds=1.5).start()
    port = free_port()
    config = {
        "port": port,
        "pin": PIN,
        "media_dir": media,
        "image_seconds": 2,
        "mpv_video_args": ["--vo=null", "--ao=null"],
        "projector": {"enabled": True, "host": "127.0.0.1", "port": projector.port,
                      "password": "secret", "input": "31", "warmup_seconds": 5},
    }
    config_path = os.path.join(work, "config.json")
    state_path = os.path.join(work, "state.json")
    with open(config_path, "w") as f:
        json.dump(config, f)

    env = dict(os.environ, BALCONY_MPV_SOCKET=os.path.join(work, "mpv.sock"), PYTHONUNBUFFERED="1")
    log_file = open(os.path.join(work, "app.log"), "w")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py"), "--config", config_path,
                             "--state", state_path, "--host", "127.0.0.1"],
                            stdout=log_file, stderr=subprocess.STDOUT, env=env)
    api = Client("http://127.0.0.1:%d" % port)

    def restart_app():
        nonlocal proc
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py"), "--config", config_path,
                                 "--state", state_path, "--host", "127.0.0.1"],
                                stdout=log_file, stderr=subprocess.STDOUT, env=env)
        wait_up()

    def wait_up():
        for _ in range(100):
            try:
                st, _ = api.call("/api/status")
                if st == 200:
                    return
            except (urllib.error.URLError, ConnectionError, socket.timeout):
                pass
            time.sleep(0.2)
        raise SystemExit("app.py never came up; see " + log_file.name)

    try:
        wait_up()
        print("app up on port %d, work dir %s" % (port, work))

        # --- PIN --------------------------------------------------------
        st, body = api.call("/api/status", pin="")
        check("status without PIN is refused", st == 401 and body.get("pin_required"), body)
        st, body = api.call("/api/status", pin="0000")
        check("wrong PIN is refused", st == 401, body)
        st, body = api.call("/api/status")
        check("status with PIN", st == 200 and body["ok"], body)
        check("player is running", body["player"]["player_running"], body["player"])
        check("screen starts dark", body["player"]["mode"] == "off")
        check("index page serves", urllib.request.urlopen(api.base + "/").status == 200)

        # --- media ------------------------------------------------------
        st, body = api.call("/api/media")
        names = [p["name"] for p in body["playlists"]]
        check("three playlists listed", names == ["halloween", "campaign", "movies"], names)
        hall = body["playlists"][0]
        check("natural sort and text file skipped",
              [f["name"] for f in hall["files"]] == ["ghost 1.png", "ghost 2.png", "ghost 10.png"], hall["files"])
        movies = body["playlists"][2]
        check("movies are play-once", movies["mode"] == "once")

        # --- looping playlist ---------------------------------------------
        st, body = api.call("/api/play", {"playlist": "halloween"})
        check("play halloween", st == 200 and body["ok"], body)
        s = api.wait(lambda s: s["player"]["file"] is not None, 10, "halloween to load")
        check("halloween is looping", s["player"]["mode"] == "loop" and s["player"]["playlist_count"] == 3, s["player"])
        st, body = api.call("/api/next", {})
        check("next file", st == 200, body)
        time.sleep(0.5)
        _, s = api.call("/api/status")
        check("moved to second file", s["player"]["playlist_pos"] == 1, s["player"])
        st, body = api.call("/api/pause", {})
        _, s = api.call("/api/status")
        check("pause", st == 200 and s["player"]["paused"], s["player"])
        st, body = api.call("/api/resume", {})
        _, s = api.call("/api/status")
        check("resume", st == 200 and not s["player"]["paused"], s["player"])
        st, body = api.call("/api/volume", {"volume": 55})
        check("volume", st == 200 and body["volume"] == 55, body)
        st, body = api.call("/api/volume", {"volume": "loud"})
        check("bad volume rejected", st == 400, body)

        # loop survives past the playlist end (3 pictures x 2 s)
        time.sleep(7)
        _, s = api.call("/api/status")
        check("still looping after the last file", s["player"]["mode"] == "loop" and s["player"]["file"], s["player"])

        # --- watchdog ---------------------------------------------------
        pids = subprocess.run(["pgrep", "-f", "input-ipc-server=" + env["BALCONY_MPV_SOCKET"]],
                              capture_output=True, text=True).stdout.split()
        check("found the mpv process", len(pids) >= 1, pids)
        for pid in pids:
            os.kill(int(pid), signal.SIGKILL)
        s = api.wait(lambda s: s["player"]["player_running"] and s["player"]["mode"] == "loop"
                     and s["player"]["file"], 20, "watchdog restart")
        check("watchdog restarted mpv and resumed the loop",
              s["player"]["player_running"] and s["player"]["mode"] == "loop" and s["player"]["file"], s["player"])

        # --- resume after reboot ------------------------------------------
        restart_app()
        s = api.wait(lambda s: s["player"]["mode"] == "loop" and s["player"]["file"], 15, "resume after restart")
        check("loop resumes after the app restarts", s["player"]["mode"] == "loop" and s["player"]["file"], s["player"])

        # --- play once, then dark ---------------------------------------
        st, body = api.call("/api/play", {"playlist": "movies"})
        check("movies needs a file", st == 400, body)
        st, body = api.call("/api/play", {"playlist": "movies", "file": "poster.png"})
        check("play one movie", st == 200, body)
        _, s = api.call("/api/status")
        check("mode is once", s["player"]["mode"] == "once" and s["player"]["file"] == "poster.png", s["player"])
        s = api.wait(lambda s: s["player"]["mode"] == "off", 12, "movie to finish")
        check("screen goes dark after the movie", s["player"]["mode"] == "off", s["player"])
        if have_mp4:
            st, body = api.call("/api/play", {"playlist": "movies", "file": "short.mp4"})
            s = api.wait(lambda s: s["player"]["duration"], 10, "mp4 to load")
            check("mp4 plays with a duration", st == 200 and s["player"]["duration"], s["player"])
            s = api.wait(lambda s: s["player"]["mode"] == "off", 12, "mp4 to finish")
            check("dark after the mp4", s["player"]["mode"] == "off", s["player"])
        st, body = api.call("/api/play", {"playlist": "nope"})
        check("unknown playlist rejected", st == 400 and "nope" in body["error"], body)
        st, body = api.call("/api/play", {"playlist": "movies", "file": "../../etc/passwd"})
        check("path traversal rejected", st == 400, body)

        # --- stop -------------------------------------------------------
        api.call("/api/play", {"playlist": "campaign"})
        st, body = api.call("/api/stop", {})
        _, s = api.call("/api/status")
        check("go dark", st == 200 and s["player"]["mode"] == "off", s["player"])

        # --- projector --------------------------------------------------
        s = api.wait(lambda s: s["projector"]["reachable"], 10, "projector poll")
        check("fake projector reachable with password", s["projector"]["reachable"] and
              s["projector"]["power"] == "off", s["projector"])
        check("lamp hours read", s["projector"]["lamp_hours"] == 1234, s["projector"])
        st, body = api.call("/api/projector", {"power": "on"})
        check("projector on accepted", st == 200 and "warm" in body["message"], body)
        s = api.wait(lambda s: s["projector"]["power"] == "on" and not s["projector"]["busy"], 30, "projector warm")
        check("projector reports on", s["projector"]["power"] == "on", s["projector"])
        check("HDMI 1 selected after warm-up", "%1INPT 31" in projector.commands, projector.commands)
        st, body = api.call("/api/projector", {"power": "sideways"})
        check("bad projector request rejected", st == 400, body)
        st, body = api.call("/api/projector", {"power": "off"})
        check("projector off accepted", st == 200, body)
        s = api.wait(lambda s: s["projector"]["power"] == "off" and not s["projector"]["busy"], 30, "projector cool")
        check("projector reports off", s["projector"]["power"] == "off", s["projector"])

        # --- settings and schedule ----------------------------------------
        st, body = api.call("/api/settings")
        check("settings read", st == 200 and body["projector_password_set"] and "password" not in body["projector"], body)
        st, body = api.call("/api/settings", {"schedule": {"start": "25:00"}})
        check("bad time rejected", st == 400, body)
        st, body = api.call("/api/settings", {"schedule": {"start": "19:00", "end": "19:00"}})
        check("same start and end rejected", st == 400, body)
        st, body = api.call("/api/settings", {"schedule": {"playlist": "nope"}})
        check("unknown schedule playlist rejected", st == 400, body)
        st, body = api.call("/api/settings", {"schedule": {"enabled": True, "start": "18:30", "end": "23:15",
                                                            "playlist": "campaign", "projector_power": False},
                                              "image_seconds": 3, "audio_device": "auto"})
        check("schedule saved", st == 200 and body["schedule"]["start"] == "18:30"
              and body["schedule"]["playlist"] == "campaign" and body["image_seconds"] == 3, body)
        with open(config_path) as f:
            saved = json.load(f)
        check("config.json written", saved["schedule"]["end"] == "23:15" and saved["image_seconds"] == 3, saved)
        _, s = api.call("/api/status")
        check("status shows next start/end", s["schedule"]["next_start"] and s["schedule"]["next_end"], s["schedule"])
        st, body = api.call("/api/settings", {"image_seconds": 1})
        check("silly picture time rejected", st == 400, body)
        st, body = api.call("/api/settings", {"pin": "12ab"})
        check("bad PIN rejected", st == 400, body)

        # schedule fires: set a window that starts now
        now = datetime.now()
        start = now.strftime("%H:%M")
        end = (now.replace(hour=(now.hour + 1) % 24)).strftime("%H:%M")
        st, body = api.call("/api/settings", {"schedule": {"enabled": True, "start": start, "end": end,
                                                            "playlist": "halloween", "projector_power": True}})
        check("schedule window set to now", st == 200, body)
        s = api.wait(lambda s: s["player"]["mode"] == "loop" and s["player"]["playlist"] == "halloween", 45,
                     "schedule to start the show")
        check("schedule started the show", s["player"]["mode"] == "loop" and s["player"]["playlist"] == "halloween",
              s["player"])
        check("schedule turned the projector on", projector.power in ("1", "3"), projector.power)
        # window ends: move the end to a minute ago
        past_end = (now.replace(hour=(now.hour + 23) % 24)).strftime("%H:%M")
        past_start = (now.replace(hour=(now.hour + 22) % 24)).strftime("%H:%M")
        api.call("/api/settings", {"schedule": {"start": past_start, "end": past_end}})
        s = api.wait(lambda s: s["player"]["mode"] == "off", 45, "schedule to end the show")
        check("schedule ended the show", s["player"]["mode"] == "off", s["player"])
        s = api.wait(lambda s: s["projector"]["power"] in ("off", "cooling"), 30, "projector off after show")
        check("schedule turned the projector off", s["projector"]["power"] in ("off", "cooling"), s["projector"])
        api.call("/api/settings", {"schedule": {"enabled": False}})

        # --- upload and delete ----------------------------------------------
        png_path = os.path.join(work, "up.png")
        tiny_png(png_path, (1, 2, 3))
        boundary = "----balconyboundary"
        with open(png_path, "rb") as f:
            data = f.read()
        raw = (("--%s\r\nContent-Disposition: form-data; name=\"playlist\"\r\n\r\ncampaign\r\n"
                "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"new slide.png\"\r\n"
                "Content-Type: image/png\r\n\r\n") % (boundary, boundary)).encode() + data + \
            ("\r\n--%s--\r\n" % boundary).encode()
        st, body = api.call("/api/upload", raw=raw, content_type="multipart/form-data; boundary=" + boundary)
        check("upload a picture", st == 200 and body["name"] == "new slide.png", body)
        check("uploaded file on disk", os.path.exists(os.path.join(media, "campaign", "new slide.png")))
        bad = raw.replace(b'filename="new slide.png"', b'filename="evil.sh"')
        st, body = api.call("/api/upload", raw=bad, content_type="multipart/form-data; boundary=" + boundary)
        check("unplayable upload rejected", st == 400, body)
        st, body = api.call("/api/delete", {"playlist": "campaign", "file": "new slide.png"})
        check("delete a file", st == 200 and not os.path.exists(os.path.join(media, "campaign", "new slide.png")), body)
        st, body = api.call("/api/delete", {"playlist": "campaign", "file": "new slide.png"})
        check("deleting twice is a plain error", st == 400, body)

        # --- problems list, log, restart --------------------------------------
        shutil.rmtree(os.path.join(media, "movies"))
        _, s = api.call("/api/status")
        check("missing folder shows up as a problem", any("movies" in p for p in s["problems"]), s["problems"])
        os.makedirs(os.path.join(media, "movies"))
        _, s = api.call("/api/status")
        check("empty folder shows up as a problem", any("Movies folder is empty" in p for p in s["problems"]),
              s["problems"])
        st, body = api.call("/api/log")
        check("log endpoint", st == 200 and isinstance(body["app"], list) and body["app"], body)
        st, body = api.call("/api/restart_player", {})
        check("manual player restart", st == 200 and body["player"]["player_running"], body)
        st, body = api.call("/api/nothing")
        check("unknown API path is a plain 404", st == 404 and body.get("error"), body)

        # --- PIN change -------------------------------------------------------
        st, body = api.call("/api/settings", {"pin": ""})
        check("PIN removed", st == 200 and not body["pin_set"], body)
        st, body = api.call("/api/status", pin="")
        check("no PIN needed now", st == 200, body)
        st, body = api.call("/api/settings", {"pin": PIN}, pin="")
        check("PIN set again", st == 200 and body["pin_set"], body)

        # --- state file ------------------------------------------------------
        with open(state_path) as f:
            state = json.load(f)
        check("state.json has volume and mode", state["volume"] == 55 and state["mode"] == "off", state)

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        leftovers = subprocess.run(["pgrep", "-f", "input-ipc-server=" + env["BALCONY_MPV_SOCKET"]],
                                   capture_output=True, text=True).stdout.split()
        check("mpv exits with the app", not leftovers, leftovers)
        for pid in leftovers:
            os.kill(int(pid), signal.SIGKILL)

    print()
    if failures:
        print("%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        print("app log: %s" % log_file.name)
        return 1
    print("all checks passed")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
