#!/usr/bin/env python3
"""
End-to-end smoke test: real mpv (--vo=null), real ffmpeg, a fake PJLink
projector, a fake ntfy server, and every API endpoint.

    python3 tests/smoke_test.py

Makes a throwaway media folder with tiny pictures and clips, starts app.py on a
spare port with PIN 1234, and walks the whole thing: conversion, hologram mode,
the disclaimer strip, stitched shows, schedule, projector, alerts, edits.
"""

import http.server
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from fake_pjlink import FakeProjector  # noqa: E402

PIN = "1234"
failures = []


def check(name, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print("%s %s%s" % (mark, name, (" -- " + str(detail)[:600]) if detail and not condition else ""), flush=True)
    if not condition:
        failures.append(name)


def png_bytes(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def tiny_png(path, rgb, w=64, h=40):
    with open(path, "wb") as f:
        f.write(png_bytes(w, h, rgb))


def ffmpeg_clip(path, seconds=2, strobe=False):
    src = ("nullsrc=s=64x40:r=15,geq=lum='255*mod(floor(T*3),2)':cb=128:cr=128" if strobe
           else "color=c=0x202020:s=64x40:r=15")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, "-t", str(seconds),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", path]
    return subprocess.run(cmd).returncode == 0


def luma_stats(path, crop=None):
    """Mean brightness of a file (0-255), optionally of a cropped region."""
    vf = (("crop=%s," % crop) if crop else "") + "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-"
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-f", "null", "-"],
                         capture_output=True, text=True).stdout
    vals = [float(l.split("YAVG=")[1]) for l in out.splitlines() if "YAVG=" in l]
    return sum(vals) / len(vals) if vals else None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeNtfy(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        FakeNtfy.received.append((self.headers.get("Title", ""), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def multipart(fields, files):
    boundary = "----balconyboundary"
    out = b""
    for k, v in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    for k, (fname, data, ctype) in files.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n" % (boundary, k, fname, ctype)).encode() + data + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode()
    return out, "multipart/form-data; boundary=" + boundary


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
            with urllib.request.urlopen(req, data=data, timeout=30) as res:
                payload = res.read()
                if res.headers.get("Content-Type", "").startswith("image/"):
                    return res.status, {"bytes": len(payload)}
                return res.status, json.loads(payload.decode())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode())
            except ValueError:
                return exc.code, {}

    def wait(self, predicate, seconds, what, path="/api/status"):
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            _, last = self.call(path)
            try:
                if predicate(last):
                    return last
            except (KeyError, TypeError, IndexError):
                pass
            time.sleep(0.5)
        print("     timed out waiting for %s; last: %s" % (what, json.dumps(last)[:500]), flush=True)
        return last

    def files(self, playlist):
        _, m = self.call("/api/media")
        for pl in m["playlists"]:
            if pl["name"] == playlist:
                return pl
        return None


def main():
    for tool in ("mpv", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print("%s is not installed; cannot run the smoke test." % tool)
            return 2

    work = tempfile.mkdtemp(prefix="balcony-test-")
    media = os.path.join(work, "media")
    for name in ("halloween", "campaign", "movies"):
        os.makedirs(os.path.join(media, name))
    tiny_png(os.path.join(media, "halloween", "ghost 1.png"), (10, 10, 10))
    tiny_png(os.path.join(media, "halloween", "ghost 2.png"), (20, 20, 20))
    tiny_png(os.path.join(media, "halloween", "ghost 10.png"), (30, 30, 30))
    tiny_png(os.path.join(media, "campaign", "logo.png"), (250, 250, 250))       # white: hologram auto
    ffmpeg_clip(os.path.join(media, "movies", "short.mp4"), seconds=2)
    tiny_png(os.path.join(media, "movies", "poster.png"), (40, 0, 0))
    with open(os.path.join(media, "halloween", "notes.txt"), "w") as f:
        f.write("not media\n")
    # make the files look settled so the first scan picks them up
    old = time.time() - 60
    for folder in ("halloween", "campaign", "movies"):
        for entry in os.listdir(os.path.join(media, folder)):
            os.utime(os.path.join(media, folder, entry), (old, old))

    projector = FakeProjector(password="secret", warm_seconds=1.5).start()
    ntfy = http.server.HTTPServer(("127.0.0.1", 0), FakeNtfy)
    threading.Thread(target=ntfy.serve_forever, daemon=True).start()
    ntfy_url = "http://127.0.0.1:%d/balcony" % ntfy.server_address[1]

    port = free_port()
    config = {
        "port": port,
        "pin": PIN,
        "media_dir": media,
        "image_seconds": 2,
        "mpv_video_args": ["--vo=null", "--ao=null"],
        "processing": {"enabled": True, "crossfade_seconds": 0.5, "render_shows": True},
        "projector": {"enabled": True, "host": "127.0.0.1", "port": projector.port,
                      "password": "secret", "input": "31", "warmup_seconds": 5},
        "alerts": {"enabled": True, "ntfy_url": ntfy_url},
        "seasons": [],
        "tls": {"enabled": False},
    }
    config_path = os.path.join(work, "config.json")
    state_path = os.path.join(work, "state.json")
    with open(config_path, "w") as f:
        json.dump(config, f)

    env = dict(os.environ, BALCONY_MPV_SOCKET=os.path.join(work, "mpv.sock"), PYTHONUNBUFFERED="1",
               BALCONY_SCAN_SECONDS="2", BALCONY_SETTLE_SECONDS="1")
    log_file = open(os.path.join(work, "app.log"), "w")

    def launch():
        return subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py"), "--config", config_path,
                                 "--state", state_path, "--host", "127.0.0.1"],
                                stdout=log_file, stderr=subprocess.STDOUT, env=env)

    proc = launch()
    api = Client("http://127.0.0.1:%d" % port)

    def wait_up():
        for _ in range(150):
            try:
                st, _ = api.call("/api/status")
                if st == 200:
                    return
            except (urllib.error.URLError, ConnectionError, socket.timeout):
                pass
            time.sleep(0.2)
        raise SystemExit("app.py never came up; see " + log_file.name)

    def restart_app():
        nonlocal proc
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = launch()
        wait_up()

    def all_ready(playlist, count=None):
        def pred(m):
            pl = [p for p in m["playlists"] if p["name"] == playlist][0]
            if count is not None and len(pl["files"]) != count:
                return False
            return bool(pl["files"]) and all(f["status"] == "ready" for f in pl["files"])
        return pred

    try:
        wait_up()
        print("app up on port %d, work dir %s" % (port, work), flush=True)

        # --- PIN --------------------------------------------------------
        st, body = api.call("/api/status", pin="")
        check("status without PIN is refused", st == 401 and body.get("pin_required"), body)
        st, body = api.call("/api/status", pin="0000")
        check("wrong PIN is refused", st == 401, body)
        st, body = api.call("/api/status")
        check("status with PIN", st == 200 and body["ok"], body)
        check("player is running", body["player"]["player_running"], body["player"])
        check("screen starts dark", body["player"]["mode"] == "off")
        check("health reported", body["health"]["disk_free_gb"] is not None, body["health"])
        check("index page serves", urllib.request.urlopen(api.base + "/").status == 200)

        # --- conversion -------------------------------------------------
        m = api.wait(all_ready("halloween", 3), 240, "halloween conversion", path="/api/media")
        hall = [p for p in m["playlists"] if p["name"] == "halloween"][0]
        names = [f["name"] for f in hall["files"]]
        check("pictures became clips, in natural order", names == ["ghost 1.mp4", "ghost 2.mp4", "ghost 10.mp4"], names)
        first = hall["files"][0]
        check("clip has duration, thumbnail, and remembers its source",
              first["duration"] and abs(first["duration"] - 2) < 0.6 and first["thumb"] and first["source"] == "ghost 1.png"
              and first["from_image"], first)
        check("originals kept", os.path.exists(os.path.join(media, "halloween", ".originals", "ghost 1.png")))
        st, _ = api.call("/api/thumb/halloween/ghost%201.mp4")
        check("thumbnail served", st == 200)
        m = api.wait(all_ready("movies", 2), 120, "movies conversion", path="/api/media")
        mov = [p for p in m["playlists"] if p["name"] == "movies"][0]
        short = [f for f in mov["files"] if f["name"] == "short.mp4"][0]
        check("small H.264 mp4 kept as is", short["status"] == "ready" and short["source"] == "short.mp4"
              and not os.path.exists(os.path.join(media, "movies", ".originals", "short.mp4")), short)
        check("movies are play-once", mov["mode"] == "once")

        # --- political playlist: waiting for the strip ------------------------
        m = api.wait(lambda m: [p for p in m["playlists"] if p["name"] == "campaign"][0]["files"][0]["status"] == "waiting",
                     60, "campaign to wait for the strip", path="/api/media")
        camp = [p for p in m["playlists"] if p["name"] == "campaign"][0]
        check("campaign file waits for the paid-for line", camp["files"][0]["status"] == "waiting", camp["files"])
        _, s = api.call("/api/status")
        check("problem explains the paid-for line", any("paid for by" in p for p in s["problems"]), s["problems"])
        st, body = api.call("/api/play", {"playlist": "campaign"})
        check("campaign won't play without the strip", st == 400, body)
        st, body = api.call("/api/play", {"playlist": "campaign", "file": "logo.png"})
        check("nor a single campaign file", st == 400, body)

        raw, ctype = multipart({"playlist": "campaign", "text": "Political advertising paid for by Test Committee"},
                               {"image": ("campaign.png", png_bytes(1280, 90, (200, 200, 200)), "image/png")})
        st, body = api.call("/api/disclaimer", raw=raw, content_type=ctype)
        check("disclaimer strip saved", st == 200, body)
        m = api.wait(all_ready("campaign", 1), 120, "campaign conversion with strip", path="/api/media")
        camp = [p for p in m["playlists"] if p["name"] == "campaign"][0]
        logo = camp["files"][0]
        check("white logo got hologram invert automatically", logo["hologram"] == "invert" and logo["hologram_auto"], logo)
        out = os.path.join(media, "campaign", logo["name"])
        top = luma_stats(out, crop="1280:700:0:0")
        strip = luma_stats(out, crop="1280:80:0:720")
        check("inverted logo is dark on the screen", top is not None and top < 60, top)
        # the clip fades in and out, so the strip averages about half its brightness
        check("disclaimer strip is burned into the bottom", strip is not None and strip > 60 and strip > (top or 0) + 40, (top, strip))
        raw, ctype = multipart({"playlist": "campaign", "text": "Political advertising paid for by Other Committee"},
                               {"image": ("campaign.png", png_bytes(1280, 90, (120, 120, 120)), "image/png")})
        api.call("/api/disclaimer", raw=raw, content_type=ctype)
        m = api.wait(lambda m: [p for p in m["playlists"] if p["name"] == "campaign"][0]["files"][0]["status"] != "ready",
                     30, "campaign to re-queue after strip change", path="/api/media")
        m = api.wait(all_ready("campaign", 1), 120, "campaign re-render", path="/api/media")
        strip2 = luma_stats(os.path.join(media, "campaign", logo["name"]), crop="1280:80:0:720")
        check("changing the strip re-renders campaign files", strip2 is not None and strip2 < strip, (strip, strip2))

        # --- stitched show ----------------------------------------------------
        m = api.wait(lambda m: [p for p in m["playlists"] if p["name"] == "halloween"][0]["show"]["ready"],
                     180, "halloween show render", path="/api/media")
        hall = [p for p in m["playlists"] if p["name"] == "halloween"][0]
        check("halloween show stitched", hall["show"] and hall["show"]["ready"] and hall["show"]["segments"] == 3, hall["show"])
        st, body = api.call("/api/play", {"playlist": "halloween"})
        check("play halloween", st == 200 and body["ok"], body)
        s = api.wait(lambda s: s["player"]["file"] is not None, 10, "halloween to load")
        check("plays the stitched show", s["player"]["show"] and s["player"]["playlist_count"] == 3
              and s["player"]["file"] == "ghost 1.mp4", s["player"])
        st, body = api.call("/api/next", {})
        time.sleep(0.7)
        _, s = api.call("/api/status")
        check("next jumps to the second segment", st == 200 and s["player"]["playlist_pos"] == 1, s["player"])
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
        time.sleep(7)
        _, s = api.call("/api/status")
        check("show keeps looping past its end", s["player"]["mode"] == "loop" and s["player"]["file"], s["player"])

        # --- shuffle ----------------------------------------------------------
        st, body = api.call("/api/shuffle", {"shuffle": True})
        check("shuffle on", st == 200 and body["player"]["shuffle"], body)
        s = api.wait(lambda s: s["player"]["file"] is not None, 10, "shuffled playlist to load")
        check("shuffle plays the plain playlist, not the stitched show",
              s["player"]["mode"] == "loop" and not s["player"]["show"] and s["player"]["playlist_count"] == 3, s["player"])
        st, body = api.call("/api/shuffle", {"shuffle": False})
        s = api.wait(lambda s: s["player"]["show"], 10, "stitched show after shuffle off")
        check("shuffle off goes back to the stitched show", st == 200 and s["player"]["show"], s["player"])

        # --- loop one file, preview, playlists ------------------------------------
        st, body = api.call("/api/play", {"playlist": "halloween", "file": "ghost 2.mp4", "loop": True})
        check("loop a single file", st == 200 and body["player"]["single"] and body["player"]["mode"] == "loop", body)
        time.sleep(3.5)
        _, s = api.call("/api/status")
        check("single file keeps looping past its end", s["player"]["mode"] == "loop" and s["player"]["file"] == "ghost 2.mp4", s["player"])
        st, body = api.call("/api/preview.jpg")
        check("live preview served", st == 200 and body.get("bytes", 0) > 1000, body)
        restart_app()
        s = api.wait(lambda s: s["player"]["mode"] == "loop" and s["player"]["file"] == "ghost 2.mp4", 15, "single loop to resume")
        check("single-file loop resumes after a restart", s["player"]["single"], s["player"])
        st, body = api.call("/api/playlist", {"action": "create", "label": "Winter Lights", "mode": "loop"})
        check("create a playlist", st == 200 and body["name"] == "winter-lights" and os.path.isdir(os.path.join(media, "winter-lights")), body)
        st, body = api.call("/api/playlist", {"action": "create", "label": "Winter Lights"})
        check("duplicate playlist rejected", st == 400, body)
        st, body = api.call("/api/playlist", {"action": "rename", "name": "winter-lights", "label": "Winter"})
        check("rename a playlist", st == 200 and any(p["name"] == "winter-lights" and p["label"] == "Winter" for p in body["playlists"]), body)
        st, body = api.call("/api/playlist", {"action": "delete", "name": "winter-lights"})
        check("delete an empty playlist", st == 200 and not os.path.isdir(os.path.join(media, "winter-lights")), body)
        st, body = api.call("/api/playlist", {"action": "delete", "name": "halloween"})
        check("deleting a full playlist needs confirmation", st == 400, body)
        m = api.wait(lambda m: all(f["thumb"] for p in m["playlists"] if p["name"] == "movies" for f in p["files"]), 60, "mpv thumbnails", path="/api/media")
        check("thumbnails made by mpv", all(f["thumb"] for p in m["playlists"] if p["name"] == "movies" for f in p["files"]), m["playlists"][2]["files"])
        api.call("/api/play", {"playlist": "halloween"})
        s = api.wait(lambda s: s["player"]["show"], 10, "back to the stitched show")

        # --- playlist edits ---------------------------------------------------
        st, body = api.call("/api/item", {"playlist": "halloween", "file": "ghost 2.mp4", "enabled": False})
        check("switch a file off", st == 200 and not [f for f in body["playlist"]["files"] if f["name"] == "ghost 2.mp4"][0]["enabled"], body)
        m = api.wait(lambda m: [p for p in m["playlists"] if p["name"] == "halloween"][0]["show"]["ready"]
                     and [p for p in m["playlists"] if p["name"] == "halloween"][0]["show"]["segments"] == 2,
                     180, "show re-render with two clips", path="/api/media")
        hall = [p for p in m["playlists"] if p["name"] == "halloween"][0]
        check("show re-stitched without the disabled file", hall["show"]["segments"] == 2, hall["show"])
        st, body = api.call("/api/order", {"playlist": "halloween", "files": ["ghost 10.mp4", "ghost 1.mp4", "ghost 2.mp4"]})
        check("reorder", st == 200 and [f["name"] for f in body["playlist"]["files"]][0] == "ghost 10.mp4", body)
        st, body = api.call("/api/item", {"playlist": "halloween", "file": "ghost 1.mp4", "dwell": 3})
        check("dwell change accepted", st == 200, body)
        m = api.wait(lambda m: [f for f in [p for p in m["playlists"] if p["name"] == "halloween"][0]["files"]
                                if f["name"] == "ghost 1.mp4"][0]["status"] == "ready" and
                     abs([f for f in [p for p in m["playlists"] if p["name"] == "halloween"][0]["files"]
                          if f["name"] == "ghost 1.mp4"][0]["duration"] - 3) < 0.6,
                     120, "dwell re-render", path="/api/media")
        g1 = [f for f in [p for p in m["playlists"] if p["name"] == "halloween"][0]["files"] if f["name"] == "ghost 1.mp4"][0]
        check("picture re-made at the new length", abs((g1["duration"] or 0) - 3) < 0.6, g1)
        st, body = api.call("/api/item", {"playlist": "halloween", "file": "ghost 1.mp4", "hologram": "knockout"})
        check("hologram mode change accepted", st == 200, body)
        m = api.wait(lambda m: [f for f in [p for p in m["playlists"] if p["name"] == "halloween"][0]["files"]
                                if f["name"] == "ghost 1.mp4"][0]["status"] == "ready", 120, "knockout render", path="/api/media")
        st, body = api.call("/api/item", {"playlist": "halloween", "file": "ghost 1.mp4", "hologram": "sideways"})
        check("bad hologram mode rejected", st == 400, body)

        # --- flash check ----------------------------------------------------
        strobe = os.path.join(work, "strobe.mp4")
        ffmpeg_clip(strobe, seconds=4, strobe=True)
        with open(strobe, "rb") as f:
            raw, ctype = multipart({"playlist": "halloween"}, {"file": ("strobe.mp4", f.read(), "video/mp4")})
        st, body = api.call("/api/upload", raw=raw, content_type=ctype)
        check("upload a video", st == 200 and body["name"] == "strobe.mp4" and body["processing"], body)
        m = api.wait(lambda m: [f for f in [p for p in m["playlists"] if p["name"] == "halloween"][0]["files"]
                                if f["name"] == "strobe.mp4"][0]["status"] == "ready", 120, "strobe conversion", path="/api/media")
        sf = [f for f in [p for p in m["playlists"] if p["name"] == "halloween"][0]["files"] if f["name"] == "strobe.mp4"][0]
        check("flashing clip is flagged", sf["flash_warning"] and sf["flash"] > 3, sf)
        _, s = api.call("/api/status")
        check("flash warning in problems", any("brightness jumps" in p for p in s["problems"]), s["problems"])
        st, body = api.call("/api/delete", {"playlist": "halloween", "file": "strobe.mp4"})
        check("delete a file", st == 200 and not os.path.exists(os.path.join(media, "halloween", "strobe.mp4")), body)
        st, body = api.call("/api/delete", {"playlist": "halloween", "file": "strobe.mp4"})
        check("deleting twice is a plain error", st == 400, body)

        # --- watchdog ---------------------------------------------------
        pids = subprocess.run(["pgrep", "-f", "input-ipc-server=" + env["BALCONY_MPV_SOCKET"]],
                              capture_output=True, text=True).stdout.split()
        check("found the mpv process", len(pids) >= 1, pids)
        for pid in pids:
            os.kill(int(pid), signal.SIGKILL)
        s = api.wait(lambda s: s["player"]["player_running"] and s["player"]["mode"] == "loop"
                     and s["player"]["file"], 25, "watchdog restart")
        check("watchdog restarted mpv and resumed the loop",
              s["player"]["player_running"] and s["player"]["mode"] == "loop" and s["player"]["file"], s["player"])

        # --- resume after reboot ------------------------------------------
        restart_app()
        s = api.wait(lambda s: s["player"]["mode"] == "loop" and s["player"]["file"], 15, "resume after restart")
        check("loop resumes after the app restarts", s["player"]["mode"] == "loop" and s["player"]["file"], s["player"])

        # --- play once, then dark ---------------------------------------
        st, body = api.call("/api/play", {"playlist": "movies"})
        check("movies needs a file", st == 400, body)
        st, body = api.call("/api/play", {"playlist": "movies", "file": "poster.mp4"})
        check("play one movie", st == 200, body)
        _, s = api.call("/api/status")
        check("mode is once", s["player"]["mode"] == "once" and s["player"]["file"] == "poster.mp4", s["player"])
        s = api.wait(lambda s: s["player"]["mode"] == "off", 15, "movie to finish")
        check("screen goes dark after the movie", s["player"]["mode"] == "off", s["player"])
        st, body = api.call("/api/play", {"playlist": "movies", "file": "short.mp4"})
        s = api.wait(lambda s: s["player"]["duration"], 10, "mp4 to load")
        check("mp4 plays with a duration", st == 200 and s["player"]["duration"], s["player"])
        s = api.wait(lambda s: s["player"]["mode"] == "off", 15, "mp4 to finish")
        check("dark after the mp4", s["player"]["mode"] == "off", s["player"])
        st, body = api.call("/api/play", {"playlist": "nope"})
        check("unknown playlist rejected", st == 400 and "nope" in body["error"], body)
        st, body = api.call("/api/play", {"playlist": "movies", "file": "../../etc/passwd"})
        check("path traversal rejected", st == 400, body)

        # --- stop, mute when dark ---------------------------------------------
        api.call("/api/settings", {"projector": {"mute_when_dark": True}})
        api.call("/api/play", {"playlist": "campaign"})
        st, body = api.call("/api/stop", {})
        _, s = api.call("/api/status")
        check("go dark", st == 200 and s["player"]["mode"] == "off", s["player"])

        # --- projector --------------------------------------------------
        s = api.wait(lambda s: s["projector"]["reachable"], 10, "projector poll")
        check("fake projector reachable with password", s["projector"]["reachable"] and s["projector"]["power"] == "off", s["projector"])
        check("lamp hours read", s["projector"]["lamp_hours"] == 1234, s["projector"])
        st, body = api.call("/api/projector", {"power": "on"})
        check("projector on accepted", st == 200 and "warm" in body["message"], body)
        s = api.wait(lambda s: s["projector"]["power"] == "on" and not s["projector"]["busy"], 30, "projector warm")
        check("projector reports on", s["projector"]["power"] == "on", s["projector"])
        check("HDMI 1 selected after warm-up", "%1INPT 31" in projector.commands, projector.commands)
        st, body = api.call("/api/projector", {"mute": True})
        check("blank the picture", st == 200 and projector.muted, body)
        st, body = api.call("/api/projector", {"mute": False})
        check("unblank the picture", st == 200 and not projector.muted, body)
        st, body = api.call("/api/projector", {"input": "32"})
        check("switch input", st == 200 and projector.input == "32", body)
        st, body = api.call("/api/projector", {"input": "99x"})
        check("bad input rejected", st == 400, body)
        api.call("/api/play", {"playlist": "halloween"})
        time.sleep(1)
        api.call("/api/stop", {})
        s = api.wait(lambda s: s["projector"]["muted"], 10, "mute when dark")
        check("projector blanked when the screen went dark", projector.muted, projector.muted)
        api.call("/api/play", {"playlist": "halloween"})
        s = api.wait(lambda s: not s["projector"]["muted"], 10, "unmute on play")
        check("projector unblanked on play", not projector.muted)
        api.call("/api/stop", {})
        api.call("/api/settings", {"projector": {"mute_when_dark": False}})
        projector.erst = "002000"
        s = api.wait(lambda s: s["projector"]["errors"], 45, "projector error flags")
        check("projector fault shows as a plain problem", any("temperature" in p for p in s["problems"]), s["problems"])
        for _ in range(20):      # the alert goes out on a background thread
            if any("projector fault" in t.lower() for t, _ in FakeNtfy.received):
                break
            time.sleep(0.25)
        check("projector fault sent an alert", any("projector fault" in t.lower() for t, _ in FakeNtfy.received), FakeNtfy.received)
        projector.erst = "000000"
        st, body = api.call("/api/projector", {"power": "sideways"})
        check("bad projector request rejected", st == 400, body)
        st, body = api.call("/api/projector", {"power": "off"})
        check("projector off accepted", st == 200, body)
        s = api.wait(lambda s: s["projector"]["power"] == "off" and not s["projector"]["busy"], 30, "projector cool")
        check("projector reports off", s["projector"]["power"] == "off", s["projector"])

        # --- alerts ---------------------------------------------------------
        st, body = api.call("/api/test_alert", {})
        check("test alert sent", st == 200 and any("Test message" in b for _, b in FakeNtfy.received), (body, FakeNtfy.received))

        # --- settings, schedule, seasons, sunset, dim -------------------------
        st, body = api.call("/api/settings")
        check("settings read", st == 200 and body["projector_password_set"] and "password" not in body["projector"], body)
        check("campaign marked political with its strip", any(p["name"] == "campaign" and p["political"] and p["disclaimer_image"]
                                                             for p in body["playlists"]), body["playlists"])
        st, body = api.call("/api/settings", {"schedule": {"start": "25:00"}})
        check("bad time rejected", st == 400, body)
        st, body = api.call("/api/settings", {"schedule": {"start": "19:00", "end": "19:00"}})
        check("same start and end rejected", st == 400, body)
        st, body = api.call("/api/settings", {"schedule": {"playlist": "nope"}})
        check("unknown schedule playlist rejected", st == 400, body)
        st, body = api.call("/api/settings", {"schedule": {"enabled": True, "start": "sunset", "start_offset": 15,
                                                            "end": "23:15", "playlist": "halloween", "projector_power": False},
                                              "location": {"lat": 32.78, "lon": -96.80, "name": "Dallas"},
                                              "seasons": [{"name": "Halloween", "from": "10-01", "to": "10-31", "playlist": "halloween"}]})
        check("sunset schedule saved", st == 200 and body["schedule"]["start"] == "sunset" and body["seasons"][0]["from"] == "10-01", body)
        _, s = api.call("/api/status")
        check("status shows sunset and next start", s["schedule"]["sunset_today"] and s["schedule"]["next_start"], s["schedule"])
        today = datetime.now().date()
        st, body = api.call("/api/settings", {"seasons": [{"name": "Now", "from": (today - timedelta(days=1)).isoformat(),
                                                            "to": (today + timedelta(days=1)).isoformat(), "playlist": "campaign"}]})
        _, s = api.call("/api/status")
        check("season picks tonight's playlist", s["schedule"]["tonight_playlist"] == "campaign" and s["schedule"]["tonight_season"] == "Now", s["schedule"])
        st, body = api.call("/api/settings", {"seasons": [{"name": "Bad", "from": "10-01", "to": "2026-10-31", "playlist": "halloween"}]})
        check("mixed season dates rejected", st == 400, body)
        st, body = api.call("/api/settings", {"seasons": [{"name": "Bad", "from": "10-01", "to": "10-31", "playlist": "nope"}]})
        check("season with unknown playlist rejected", st == 400, body)
        with open(config_path) as f:
            saved = json.load(f)
        check("config.json written", saved["schedule"]["end"] == "23:15" and saved["location"]["name"] == "Dallas", saved)
        st, body = api.call("/api/settings", {"image_seconds": 1})
        check("silly picture time rejected", st == 400, body)
        st, body = api.call("/api/settings", {"pin": "12ab"})
        check("bad PIN rejected", st == 400, body)

        # dim now
        now = datetime.now()
        st, body = api.call("/api/settings", {"dim": {"enabled": True, "start": (now - timedelta(minutes=5)).strftime("%H:%M"),
                                                       "end": (now + timedelta(minutes=30)).strftime("%H:%M"), "level": 50}})
        s = api.wait(lambda s: s["player"]["dim_level"] == 50, 30, "dim to apply")
        check("quiet hours dim applied", s["player"]["dim_level"] == 50, s["player"])
        api.call("/api/settings", {"dim": {"enabled": False}})
        s = api.wait(lambda s: s["player"]["dim_level"] == 100, 30, "dim to clear")
        check("dim cleared", s["player"]["dim_level"] == 100, s["player"])

        # schedule fires: window starts now, season says campaign
        start = now.strftime("%H:%M")
        end = (now + timedelta(hours=1)).strftime("%H:%M")
        st, body = api.call("/api/settings", {"schedule": {"enabled": True, "start": start, "start_offset": 0, "end": end,
                                                            "end_offset": 0, "playlist": "halloween", "projector_power": True},
                                              "seasons": [{"name": "Now", "from": (today - timedelta(days=1)).isoformat(),
                                                           "to": (today + timedelta(days=1)).isoformat(), "playlist": "campaign"}]})
        check("schedule window set to now", st == 200, body)
        s = api.wait(lambda s: s["player"]["mode"] == "loop" and s["player"]["playlist"] == "campaign", 60,
                     "schedule to start the show")
        check("schedule started tonight's season show", s["player"]["mode"] == "loop" and s["player"]["playlist"] == "campaign", s["player"])
        check("schedule turned the projector on", projector.power in ("1", "3"), projector.power)
        past_end = (now - timedelta(hours=1)).strftime("%H:%M")
        past_start = (now - timedelta(hours=2)).strftime("%H:%M")
        api.call("/api/settings", {"schedule": {"start": past_start, "end": past_end}})
        s = api.wait(lambda s: s["player"]["mode"] == "off", 60, "schedule to end the show")
        check("schedule ended the show", s["player"]["mode"] == "off", s["player"])
        s = api.wait(lambda s: s["projector"]["power"] in ("off", "cooling"), 30, "projector off after show")
        check("schedule turned the projector off", s["projector"]["power"] in ("off", "cooling"), s["projector"])
        api.call("/api/settings", {"schedule": {"enabled": False}, "seasons": []})

        # --- uploads: pictures and slides ---------------------------------------
        raw, ctype = multipart({"playlist": "campaign", "dwell": "4", "slide": "1"},
                               {"file": ("VOTE NOV 3.png", png_bytes(1280, 800, (0, 0, 0)), "image/png")})
        st, body = api.call("/api/upload", raw=raw, content_type=ctype)
        check("slide upload accepted", st == 200 and body["name"] == "VOTE NOV 3.png", body)
        m = api.wait(all_ready("campaign", 2), 120, "slide conversion", path="/api/media")
        slide = [f for f in [p for p in m["playlists"] if p["name"] == "campaign"][0]["files"] if f["source"] == "VOTE NOV 3.png"]
        check("slide became a 4 second clip with the strip", slide and slide[0]["name"] == "VOTE NOV 3.mp4"
              and abs(slide[0]["duration"] - 4) < 0.6 and slide[0]["hologram"] == "off", slide)
        raw, ctype = multipart({"playlist": "campaign"}, {"file": ("evil.sh", b"#!/bin/sh\n", "text/plain")})
        st, body = api.call("/api/upload", raw=raw, content_type=ctype)
        check("unplayable upload rejected", st == 400, body)
        raw, ctype = multipart({"playlist": "campaign", "dwell": "1"}, {"file": ("x.png", png_bytes(8, 8, (0, 0, 0)), "image/png")})
        st, body = api.call("/api/upload", raw=raw, content_type=ctype)
        check("silly dwell rejected", st == 400, body)

        # --- problems list, log, restart --------------------------------------
        shutil.rmtree(os.path.join(media, "movies"))
        _, s = api.call("/api/status")
        check("missing folder shows up as a problem", any("movies" in p for p in s["problems"]), s["problems"])
        os.makedirs(os.path.join(media, "movies"))
        _, s = api.call("/api/status")
        check("empty folder shows up as a problem", any("Movies folder is empty" in p for p in s["problems"]), s["problems"])
        st, body = api.call("/api/log")
        check("log endpoint", st == 200 and isinstance(body["app"], list) and body["app"] and "processing" in body, body)
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

        # --- no ffmpeg: files play as they are -----------------------------------
        api.call("/api/settings", {"processing": {"enabled": False}})
        raw, ctype = multipart({"playlist": "halloween"}, {"file": ("plain.png", png_bytes(64, 40, (5, 5, 5)), "image/png")})
        st, body = api.call("/api/upload", raw=raw, content_type=ctype)
        check("upload with conversion off", st == 200 and not body["processing"], body)
        pl = api.files("halloween")
        plain = [f for f in pl["files"] if f["name"] == "plain.png"]
        check("unconverted picture listed and playable", plain and plain[0]["status"] == "unprocessed" and plain[0]["kind"] == "image", plain)

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        ntfy.shutdown()
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
