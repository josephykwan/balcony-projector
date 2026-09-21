#!/usr/bin/env python3
"""
library.py - the media folders, what we know about each file, and the background
ffmpeg jobs that turn whatever people upload into clips that loop cleanly on the
balcony screen.

Per playlist folder:
    ~/media/halloween/               the playable files
    ~/media/halloween/playlist.json  order, dwell, hologram mode, processing status
    ~/media/halloween/.originals/    what was uploaded, before we touched it
    ~/media/halloween/.thumbs/       small pictures for the phone
    ~/media/halloween/.show/         the whole playlist stitched into one file
    ~/media/halloween/.work/         half-finished renders
    ~/media/.disclaimers/<name>.png  the "paid for by" strip for political playlists

ffmpeg is optional. Without it files play as they are, and the phone says so.
Nothing in here ever draws on the screen.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

log = logging.getLogger("balcony.library")

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".mpg", ".mpeg",
             ".ts", ".m2ts", ".wmv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT

TARGET_W, TARGET_H, TARGET_FPS = 1280, 800, 30
DISCLAIMER_H = 90            # the strip along the bottom of political assets
PIPELINE_VERSION = 1         # bump to re-process everything
META_NAME = "playlist.json"
SETTLE_SECONDS = float(os.environ.get("BALCONY_SETTLE_SECONDS", "5"))   # a file must sit still this long before we touch it
HOLOGRAM_MODES = ("off", "invert", "knockout")


def natural_key(name):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def safe_filename(name):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^A-Za-z0-9 ._()\-]+", "_", name)
    return name.strip(". ")


def fingerprint(path):
    st = os.stat(path)
    return "%d-%d" % (st.st_size, int(st.st_mtime))


def _fq(text):
    """Quote a value for use inside an ffmpeg filter graph."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:") + "'"


class LibraryError(Exception):
    pass


# --------------------------------------------------------------------------
# Library: folders and playlist.json
# --------------------------------------------------------------------------

class Library:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.processor = None       # set by Processor

    @property
    def media_dir(self):
        return os.path.abspath(os.path.expanduser(self.cfg.data["media_dir"]))

    def folder(self, name):
        return os.path.join(self.media_dir, name)

    # ---- playlist specs ----------------------------------------------------

    def specs(self):
        """Configured playlists first, then any other folder under media_dir."""
        out, seen = [], set()
        for name, spec in self.cfg.data["playlists"].items():
            seen.add(name)
            out.append(self._spec(name, spec, True))
        root = self.media_dir
        if os.path.isdir(root):
            for name in sorted(os.listdir(root), key=natural_key):
                if name in seen or name.startswith(".") or not os.path.isdir(os.path.join(root, name)):
                    continue
                out.append(self._spec(name, {"label": name.replace("_", " ").title()}, False))
        return out

    def _spec(self, name, spec, configured):
        return {
            "name": name,
            "label": spec.get("label") or name.title(),
            "mode": spec.get("mode", "loop"),
            "political": bool(spec.get("political")),
            "disclaimer": spec.get("disclaimer") or "",
            "configured": configured,
            "dir": self.folder(name),
        }

    def spec(self, name):
        for s in self.specs():
            if s["name"] == name:
                return s
        return None

    # ---- metadata ----------------------------------------------------------

    def meta(self, name):
        data = {"items": {}, "show": None}
        try:
            with open(os.path.join(self.folder(name), META_NAME)) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            pass
        if not isinstance(data.get("items"), dict):
            data["items"] = {}
        return data

    def save_meta(self, name, meta):
        folder = self.folder(name)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, META_NAME)
        with open(path + ".tmp", "w") as f:
            json.dump(meta, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)

    def disk_files(self, name):
        folder = self.folder(name)
        out = []
        if not os.path.isdir(folder):
            return out
        for entry in os.listdir(folder):
            ext = os.path.splitext(entry)[1].lower()
            if entry.startswith(".") or ext not in MEDIA_EXT:
                continue
            if os.path.isfile(os.path.join(folder, entry)):
                out.append(entry)
        return out

    def thumb_path(self, name, key):
        return os.path.join(self.folder(name), ".thumbs", os.path.splitext(key)[0] + ".jpg")

    def disclaimer_image(self, name):
        path = os.path.join(self.media_dir, ".disclaimers", name + ".png")
        return path if os.path.isfile(path) else None

    def disclaimer_fingerprint(self, name):
        """A hash of the strip's contents, so a new strip always triggers a re-render."""
        path = self.disclaimer_image(name)
        if not path:
            return ""
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:16]

    # ---- describing playlists for the phone --------------------------------

    def playlists(self):
        return [self.describe(s) for s in self.specs()]

    def playlist(self, name):
        s = self.spec(name)
        return self.describe(s) if s else None

    def describe(self, spec):
        name, folder = spec["name"], spec["dir"]
        exists = os.path.isdir(folder)
        files = []
        with self.lock:
            meta = self.meta(name) if exists else {"items": {}, "show": None}
            items = meta["items"]
            names = self.disk_files(name)
            warn_rate = float(self.cfg.data["processing"].get("flash_warn_per_minute", 3))

            def sort_key(n):
                return (items.get(n, {}).get("order", 10 ** 6), natural_key(n))

            for n in sorted(names, key=sort_key):
                it = items.get(n, {})
                ext = os.path.splitext(n)[1].lower()
                status = it.get("status", "unprocessed")
                flash = float(it.get("flash") or 0.0)
                files.append({
                    "name": n,
                    "label": os.path.splitext(it.get("source") or n)[0],
                    "kind": "image" if ext in IMAGE_EXT else "video",
                    "bytes": os.path.getsize(os.path.join(folder, n)),
                    "status": status,
                    "duration": it.get("duration"),
                    "dwell": int(it.get("dwell") or self.cfg.data.get("image_seconds", 12)),
                    "hologram": it.get("hologram", "off"),
                    "hologram_auto": bool(it.get("hologram_auto")),
                    "enabled": bool(it.get("enabled", True)),
                    "flash": round(flash, 1),
                    "flash_warning": flash > warn_rate,
                    "thumb": os.path.isfile(self.thumb_path(name, n)),
                    "error": it.get("error", ""),
                    "source": it.get("source") or n,
                    "from_image": bool(it.get("from_image")),
                })
            show = self._show_info(name, meta)
        out = dict(spec)
        out.update(exists=exists, files=files, show=show)
        return out

    def _show_info(self, name, meta):
        show = meta.get("show")
        if not show:
            return None
        path = os.path.join(self.folder(name), show.get("file", ""))
        return {
            "ready": os.path.isfile(path) and show.get("hash") == self.show_hash(name, meta),
            "duration": show.get("duration"),
            "segments": len(show.get("segments") or []),
            "rendered": show.get("rendered"),
        }

    def show_hash(self, name, meta=None):
        meta = meta or self.meta(name)
        items = meta["items"]
        parts = []
        for key in sorted(items, key=lambda k: (items[k].get("order", 10 ** 6), natural_key(k))):
            it = items[key]
            if it.get("enabled", True) and it.get("status") == "ready" and it.get("duration"):
                parts.append([key, it.get("fp"), it.get("order")])
        if len(parts) < 2:
            return ""
        blob = json.dumps([parts, float(self.cfg.data["processing"].get("crossfade_seconds", 2)),
                           PIPELINE_VERSION])
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    # ---- what to play ------------------------------------------------------

    def playable(self, name):
        """Enabled files, in order, as absolute paths. Files waiting for a
        disclaimer strip are left out on purpose."""
        pl = self.playlist(name)
        if pl is None:
            return []
        return [os.path.join(pl["dir"], f["name"]) for f in pl["files"]
                if f["enabled"] and f["status"] != "waiting"]

    def show_for(self, name):
        """The stitched show file, if it is up to date: {path, segments, duration}."""
        with self.lock:
            meta = self.meta(name)
            show = meta.get("show")
            if not show:
                return None
            path = os.path.join(self.folder(name), show.get("file", ""))
            if not os.path.isfile(path) or show.get("hash") != self.show_hash(name, meta):
                return None
            return {"path": path, "segments": show.get("segments") or [],
                    "duration": show.get("duration")}

    # ---- edits from the phone ---------------------------------------------

    def set_item(self, name, key, changes):
        with self.lock:
            meta = self.meta(name)
            it = meta["items"].get(key)
            if it is None:
                if key not in self.disk_files(name):
                    raise LibraryError("That file isn't in the %s folder any more." % name)
                it = meta["items"][key] = self._new_item(meta, key)
            requeue = False
            if "enabled" in changes:
                it["enabled"] = bool(changes["enabled"])
            if "dwell" in changes:
                dwell = int(changes["dwell"])
                if not 2 <= dwell <= 600:
                    raise LibraryError("Picture time should be between 2 and 600 seconds.")
                if dwell != it.get("dwell"):
                    it["dwell"] = dwell
                    requeue = bool(it.get("from_image")) or os.path.splitext(key)[1].lower() in IMAGE_EXT
            if "hologram" in changes:
                mode = str(changes["hologram"])
                if mode not in HOLOGRAM_MODES:
                    raise LibraryError("Hologram mode should be off, invert or knockout.")
                if mode != it.get("hologram", "off"):
                    it["hologram"] = mode
                    it["hologram_auto"] = False
                    it["hologram_auto_checked"] = True
                    requeue = True
            if "order" in changes:
                it["order"] = int(changes["order"])
            self.save_meta(name, meta)
        if (requeue or changes.get("reprocess")) and self.processor:
            self.processor.request(name, key)
        return it

    def reorder(self, name, keys):
        with self.lock:
            meta = self.meta(name)
            on_disk = set(self.disk_files(name))
            position = 0
            for key in keys:
                if key in on_disk:
                    meta["items"].setdefault(key, self._new_item(meta, key))["order"] = position
                    position += 1
            for key in sorted(on_disk - set(keys), key=natural_key):
                meta["items"].setdefault(key, self._new_item(meta, key))["order"] = position
                position += 1
            self.save_meta(name, meta)

    def remove(self, name, key):
        folder = self.folder(name)
        target = os.path.join(folder, key)
        if not os.path.isfile(target):
            raise LibraryError("That file is already gone.")
        with self.lock:
            meta = self.meta(name)
            it = meta["items"].pop(key, {})
            os.remove(target)
            for extra in (it.get("original"), os.path.relpath(self.thumb_path(name, key), folder)):
                if extra:
                    try:
                        os.remove(os.path.join(folder, extra))
                    except OSError:
                        pass
            self.save_meta(name, meta)

    def add_file(self, name, filename, stream, dwell=None, source_kind="upload", replace=False):
        """Save an upload into the folder. Returns the file name used.
        With replace=True a file of the same name is overwritten (a show re-sent
        from the studio) instead of getting a "(2)" name."""
        spec = self.spec(name)
        if spec is None:
            # a new playlist name (the studio's Holiday template, say): make its folder
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", name or ""):
                raise LibraryError("Pick which playlist the file belongs to.")
            try:
                os.makedirs(self.folder(name), exist_ok=True)
            except OSError as exc:
                raise LibraryError("Couldn't make a folder for '%s': %s" % (name, exc))
            spec = self.spec(name)
        base = safe_filename(filename)
        ext = os.path.splitext(base)[1].lower()
        if not base or ext not in MEDIA_EXT:
            raise LibraryError("That kind of file can't be played. Use MP4, MOV, MKV, JPG or PNG.")
        os.makedirs(spec["dir"], exist_ok=True)
        stem = os.path.splitext(base)[0]
        with self.lock:
            meta = self.meta(name)
            taken = set(self.disk_files(name)) | set(meta["items"])
            taken |= {os.path.splitext(k)[0] + ".mp4" for k in taken}

            def clashes(c):
                return c in taken or os.path.splitext(c)[0] + ".mp4" in taken

            candidate, counter = base, 2
            if replace:
                taken.discard(base)
                taken.discard(os.path.splitext(base)[0] + ".mp4")
            while clashes(candidate):
                candidate = "%s (%d)%s" % (stem, counter, ext)
                counter += 1
            target = os.path.join(spec["dir"], candidate)
            stream.save(target + ".part")
            os.replace(target + ".part", target)
            it = meta["items"].get(candidate) if replace else None
            if it is None:
                it = self._new_item(meta, candidate)
            else:
                it.update(status="queued" if (self.processor and self.processor.enabled()) else "unprocessed", error="")
                it.pop("rendered_with", None)
            it["source_kind"] = source_kind
            if dwell:
                it["dwell"] = int(dwell)
            if source_kind == "slide":
                it["hologram_auto_checked"] = True    # slides are already black
            meta["items"][candidate] = it
            self.save_meta(name, meta)
        if self.processor:
            self.processor.request(name, candidate)
        return candidate

    def _new_item(self, meta, key):
        orders = [it.get("order", -1) for it in meta["items"].values()]
        return {
            "status": "queued" if (self.processor and self.processor.enabled()) else "unprocessed",
            "enabled": True,
            "order": (max(orders) + 1) if orders else 0,
            "dwell": int(self.cfg.data.get("image_seconds", 12)),
            "hologram": "off",
            "added": datetime.now().isoformat(timespec="seconds"),
        }

    def set_disclaimer(self, name, text, png_bytes):
        spec = self.spec(name)
        if spec is None:
            raise LibraryError("There is no playlist called '%s'." % name)
        folder = os.path.join(self.media_dir, ".disclaimers")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ".png")
        if png_bytes:
            if not png_bytes.startswith(b"\x89PNG"):
                raise LibraryError("The disclaimer strip should be a PNG picture.")
            with open(path + ".tmp", "wb") as f:
                f.write(png_bytes)
            os.replace(path + ".tmp", path)
        pcfg = self.cfg.data["playlists"].setdefault(name, {"label": spec["label"], "mode": spec["mode"]})
        pcfg["disclaimer"] = text
        self.cfg.save()


# --------------------------------------------------------------------------
# Processor: the ffmpeg jobs
# --------------------------------------------------------------------------

class Processor:
    def __init__(self, library, cfg):
        self.lib = library
        library.processor = self
        self.cfg = cfg
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self.available = bool(self.ffmpeg and self.ffprobe)
        self.encoder = None
        self.queue = deque()
        self.qlock = threading.Lock()
        self.wake = threading.Event()
        self.current = None
        self.recent = deque(maxlen=12)
        self.stopping = False
        self.proc = None
        self.last_scan = 0

    # ---- lifecycle ---------------------------------------------------------

    def enabled(self):
        return self.available and bool(self.cfg.data["processing"].get("enabled", True))

    def start(self):
        if self.available:
            self.encoder = self._pick_encoder()
            log.info("ffmpeg found, encoding with %s", self.encoder)
        else:
            log.warning("ffmpeg not found; files will play as they are")
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._scan_loop, daemon=True).start()

    def stop(self):
        self.stopping = True
        self.wake.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _pick_encoder(self):
        want = self.cfg.data["processing"].get("encoder", "auto")
        try:
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                 text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        have = set(re.findall(r"^\s*V\S*\s+(\S+)", out, re.M))
        if want != "auto":
            return want if want in have else "libx264"
        if "h264_v4l2m2m" in have and sys.platform.startswith("linux") and os.path.exists("/dev/video11"):
            return "h264_v4l2m2m"
        return "libx264"

    def _encoder_args(self):
        if self.encoder == "h264_v4l2m2m":
            return ["-c:v", "h264_v4l2m2m", "-b:v", "6M", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-level", "4.1"]

    def status(self):
        with self.qlock:
            queued = len(self.queue)
        cur = dict(self.current) if self.current else None
        return {"available": self.available, "enabled": self.enabled(), "encoder": self.encoder,
                "current": cur, "queued": queued, "recent": list(self.recent)}

    # ---- queue ------------------------------------------------------------

    def request(self, playlist, key):
        if not self.enabled():
            return
        with self.qlock:
            job = ("normalize", playlist, key)
            if job in self.queue or (self.current and (self.current["kind"], self.current["playlist"],
                                                        self.current.get("file")) == job):
                return
            # keep normalizing ahead of show renders
            shows = [j for j in self.queue if j[0] == "show"]
            others = [j for j in self.queue if j[0] != "show"]
            self.queue = deque(others + [job] + shows)
        with self.lib.lock:
            meta = self.lib.meta(playlist)
            it = meta["items"].get(key)
            if it is not None and it.get("status") != "processing":
                it["status"] = "queued"
                it["error"] = ""
                self.lib.save_meta(playlist, meta)
        self.wake.set()

    def request_show(self, playlist):
        with self.qlock:
            job = ("show", playlist, None)
            if job not in self.queue and not (self.current and self.current["kind"] == "show"
                                              and self.current["playlist"] == playlist):
                self.queue.append(job)
        self.wake.set()

    def pending_for(self, playlist):
        with self.qlock:
            if any(j[1] == playlist and j[0] == "normalize" for j in self.queue):
                return True
        cur = self.current
        return bool(cur and cur["playlist"] == playlist and cur["kind"] == "normalize")

    def _worker(self):
        while not self.stopping:
            with self.qlock:
                job = self.queue.popleft() if self.queue else None
            if job is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            kind, playlist, key = job
            self.current = {"kind": kind, "playlist": playlist, "file": key, "progress": 0.0,
                            "started": time.time()}
            try:
                if kind == "normalize":
                    self._normalize(playlist, key)
                elif kind == "show":
                    self._render_show(playlist)
            except Exception as exc:  # keep the worker alive whatever happens
                log.exception("Job %s failed", job)
                self._note("%s: %s" % (key or playlist, exc))
            finally:
                self.current = None

    def _note(self, text):
        self.recent.appendleft("%s  %s" % (datetime.now().strftime("%H:%M"), text))

    # ---- scanning ---------------------------------------------------------

    def _scan_loop(self):
        interval = float(os.environ.get("BALCONY_SCAN_SECONDS", "20"))
        time.sleep(2)
        while not self.stopping:
            try:
                self.scan()
            except Exception:
                log.exception("Scan failed")
            time.sleep(interval)

    def scan(self):
        """Notice new, replaced or changed files, and stale show renders."""
        if not self.enabled():
            return
        for spec in self.lib.specs():
            name = spec["name"]
            if not os.path.isdir(spec["dir"]):
                continue
            wanted_disclaimer = self.lib.disclaimer_fingerprint(name) if spec["political"] else ""
            to_queue = []
            with self.lib.lock:
                meta = self.lib.meta(name)
                items = meta["items"]
                on_disk = self.lib.disk_files(name)
                changed = False
                for key in list(items):
                    if key not in on_disk and items[key].get("status") != "processing":
                        del items[key]
                        changed = True
                for key in sorted(on_disk, key=natural_key):
                    path = os.path.join(spec["dir"], key)
                    try:
                        fp = fingerprint(path)
                        settled = time.time() - os.stat(path).st_mtime > SETTLE_SECONDS
                    except OSError:
                        continue
                    it = items.get(key)
                    if it is None:
                        if not settled:
                            continue
                        items[key] = self.lib._new_item(meta, key)
                        items[key]["fp"] = fp
                        changed = True
                        to_queue.append(key)
                        continue
                    status = it.get("status")
                    if status == "processing":
                        continue
                    if it.get("fp") and it["fp"] != fp and settled:
                        it.update(fp=fp, status="queued", error="")
                        changed = True
                        to_queue.append(key)
                        continue
                    if status in ("queued", "unprocessed") and settled:
                        it["fp"] = it.get("fp") or fp
                        to_queue.append(key)
                        continue
                    if status == "waiting":
                        if wanted_disclaimer:          # the strip exists now
                            it["status"] = "queued"
                            changed = True
                            to_queue.append(key)
                        continue
                    if status == "ready":
                        done = it.get("rendered_with") or {}
                        want = self._wanted(it, spec, wanted_disclaimer)
                        if done != want:
                            it["status"] = "queued"
                            changed = True
                            to_queue.append(key)
                if changed:
                    self.lib.save_meta(name, meta)
            for key in to_queue:
                self.request(name, key)
            if (spec["mode"] == "loop" and self.cfg.data["processing"].get("render_shows", True)
                    and not to_queue and not self.pending_for(name)):
                with self.lib.lock:
                    meta = self.lib.meta(name)
                    want = self.lib.show_hash(name, meta)
                    show = meta.get("show") or {}
                    stale = bool(want) and (show.get("hash") != want or not os.path.isfile(
                        os.path.join(spec["dir"], show.get("file", ""))))
                if stale:
                    self.request_show(name)

    def _wanted(self, it, spec, disclaimer_fp):
        return {
            "pipeline": PIPELINE_VERSION,
            "hologram": it.get("hologram", "off"),
            "dwell": int(it.get("dwell") or 0) if it.get("from_image") else 0,
            "disclaimer": disclaimer_fp,
        }

    # ---- ffmpeg helpers ----------------------------------------------------

    def _run(self, args, total_seconds=None, timeout=6 * 3600):
        """Run ffmpeg, tracking progress. Raises LibraryError with a plain message."""
        cmd = [self.ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-progress", "pipe:1", "-nostats"] + args
        if shutil.which("nice"):
            cmd = ["nice", "-n", "10"] + cmd
        errors = deque(maxlen=12)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, errors="replace")
        proc = self.proc

        def drain():
            for line in proc.stderr:
                line = line.strip()
                if line:
                    errors.append(line)

        threading.Thread(target=drain, daemon=True).start()
        started = time.time()
        for line in proc.stdout:
            if time.time() - started > timeout:
                proc.kill()
                raise LibraryError("ffmpeg took too long and was stopped.")
            if total_seconds and line.startswith("out_time_us=") and self.current:
                try:
                    done = int(line.split("=", 1)[1]) / 1e6
                    self.current["progress"] = max(0.0, min(0.99, done / total_seconds))
                except ValueError:
                    pass
        proc.wait()
        self.proc = None
        if proc.returncode != 0:
            raise LibraryError("ffmpeg couldn't convert this file (%s)."
                               % (errors[-1] if errors else "no details"))

    def probe(self, path):
        try:
            out = subprocess.run([self.ffprobe, "-v", "error", "-print_format", "json",
                                  "-show_streams", "-show_format", path],
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LibraryError("Couldn't read this file: %s" % exc)
        if out.returncode != 0:
            raise LibraryError("This doesn't look like a video or picture ffmpeg can read.")
        data = json.loads(out.stdout or "{}")
        video = None
        audio = False
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and video is None:
                video = s
            elif s.get("codec_type") == "audio":
                audio = True
        if video is None:
            raise LibraryError("There is no picture in this file.")
        fps = 0.0
        try:
            num, den = video.get("avg_frame_rate", "0/1").split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            pass
        duration = 0.0
        for src in (data.get("format", {}), video):
            try:
                duration = float(src.get("duration") or 0.0)
            except ValueError:
                duration = 0.0
            if duration:
                break
        return {"codec": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"),
                "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                "fps": fps, "duration": duration, "audio": audio}

    def _luma_series(self, path, seconds=None, fps=15, first_only=False):
        vf = "scale=96:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-"
        if not first_only:
            vf = "fps=%d," % fps + vf
        cmd = [self.ffmpeg, "-v", "error", "-nostdin"]
        if seconds:
            cmd += ["-t", str(seconds)]
        cmd += ["-i", path, "-an", "-vf", vf]
        if first_only:
            cmd += ["-frames:v", "1"]
        cmd += ["-f", "null", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        values = []
        for line in out.splitlines():
            if "YAVG=" in line:
                try:
                    values.append(float(line.split("YAVG=", 1)[1]))
                except ValueError:
                    pass
        return values

    def mean_luma(self, path):
        values = self._luma_series(path, first_only=True)
        return values[0] if values else 0.0

    def flash_rate(self, path, duration):
        """Sudden brightness jumps per minute, over the first ten minutes."""
        values = self._luma_series(path, seconds=min(duration or 600, 600), fps=15)
        if len(values) < 2:
            return 0.0
        jumps = sum(1 for a, b in zip(values, values[1:]) if abs(a - b) > 48)
        minutes = len(values) / 15.0 / 60.0
        return jumps / minutes if minutes else 0.0

    def _canonical(self, info, ext):
        return (info["codec"] == "h264" and info["pix_fmt"] == "yuv420p"
                and 0 < info["width"] <= 1920 and 0 < info["height"] <= 1200
                and 0 < info["fps"] <= 30.5 and ext in (".mp4", ".m4v", ".mov", ".mkv"))

    # ---- filters ----------------------------------------------------------

    def _hologram_chain(self, mode, width, height, fps, label_in, label_out, image):
        """Filter graph text that turns a white-background asset into one that
        vanishes on the mesh. Returns (graph, extra_inputs)."""
        if mode == "invert":
            return "[%s]negate[%s]" % (label_in, label_out)
        if mode == "knockout":
            src = ("color=c=black:s=%dx%d:r=%s" % (width, height, "1" if image else str(int(fps or 30))))
            return ("%s[bg];[%s]format=rgba,colorkey=0xFFFFFF:0.3:0.1[fg];[bg][fg]overlay=shortest=1:format=auto,format=rgb24[%s]"
                    % (src, label_in, label_out))
        return "[%s]null[%s]" % (label_in, label_out)

    def _fit(self):
        return ("scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,pad=%d:%d:-1:-1:color=black,setsar=1"
                % (TARGET_W, TARGET_H, TARGET_W, TARGET_H))

    # ---- the jobs ---------------------------------------------------------

    def _normalize(self, name, key):
        spec = self.lib.spec(name)
        if spec is None:
            return
        folder = spec["dir"]
        path = os.path.join(folder, key)
        with self.lib.lock:
            meta = self.lib.meta(name)
            it = meta["items"].get(key)
            if it is None or not os.path.isfile(path):
                return
            it["status"] = "processing"
            it["error"] = ""
            self.lib.save_meta(name, meta)

        try:
            new_key, it = self._normalize_inner(spec, key, it)
            with self.lib.lock:
                meta = self.lib.meta(name)
                live = meta["items"].pop(key, {})
                live.update(it)
                meta["items"][new_key] = live
                self.lib.save_meta(name, meta)
            self._note("%s ready" % new_key)
        except LibraryError as exc:
            with self.lib.lock:
                meta = self.lib.meta(name)
                if key in meta["items"]:
                    meta["items"][key].update(status="failed", error=str(exc))
                    self.lib.save_meta(name, meta)
            self._note("%s failed: %s" % (key, exc))
            log.error("Could not process %s/%s: %s", name, key, exc)

    def _normalize_inner(self, spec, key, it):
        name, folder = spec["name"], spec["dir"]
        path = os.path.join(folder, key)
        ext = os.path.splitext(key)[1].lower()
        is_image = ext in IMAGE_EXT
        original_rel = it.get("original")
        src = os.path.join(folder, original_rel) if original_rel and os.path.isfile(
            os.path.join(folder, original_rel)) else path
        src_ext = os.path.splitext(src)[1].lower()
        from_image = src_ext in IMAGE_EXT
        info = self.probe(src)

        if from_image and not it.get("hologram_auto_checked"):
            it["hologram_auto_checked"] = True
            if self.mean_luma(src) > 170 and it.get("hologram", "off") == "off":
                it["hologram"] = "invert"
                it["hologram_auto"] = True
        disclaimer = self.lib.disclaimer_image(name) if spec["political"] else None
        if spec["political"] and not disclaimer:
            it.update(status="waiting", error="Set the 'paid for by' line in Settings first; "
                                              "campaign files don't play without it.")
            return key, it
        it["from_image"] = from_image
        dwell = int(it.get("dwell") or self.cfg.data.get("image_seconds", 12))
        hologram = it.get("hologram", "off")
        needs = from_image or hologram != "off" or bool(disclaimer) or not self._canonical(info, src_ext)

        if not needs:
            if src != path:
                shutil.copy2(src, path)
                try:
                    os.remove(src)
                except OSError:
                    pass
                it.pop("original", None)
            it.update(status="ready", duration=info["duration"], width=info["width"],
                      height=info["height"], fp=fingerprint(path),
                      rendered_with=self._wanted(it, spec, ""))
            self._finish(spec, key, path, info["duration"], it)
            return key, it

        stem = os.path.splitext(it.get("source") or key)[0]
        out_key = key if (ext == ".mp4" and not from_image) else stem + ".mp4"
        if out_key != key and os.path.exists(os.path.join(folder, out_key)):
            counter = 2
            while os.path.exists(os.path.join(folder, "%s (%d).mp4" % (stem, counter))):
                counter += 1
            out_key = "%s (%d).mp4" % (stem, counter)
        work = os.path.join(folder, ".work")
        os.makedirs(work, exist_ok=True)
        tmp = os.path.join(work, out_key)

        if from_image:
            self._encode_image(src, tmp, info, dwell, hologram, disclaimer)
            total = dwell
        else:
            self._encode_video(src, tmp, info, hologram, disclaimer)
            total = info["duration"]

        if src == path:
            originals = os.path.join(folder, ".originals")
            os.makedirs(originals, exist_ok=True)
            keep = os.path.join(originals, key)
            os.replace(path, keep)
            it["original"] = os.path.relpath(keep, folder)
            it.setdefault("source", key)
        os.replace(tmp, os.path.join(folder, out_key))
        out_path = os.path.join(folder, out_key)
        out_info = self.probe(out_path)
        it.update(status="ready", duration=out_info["duration"] or total, width=out_info["width"],
                  height=out_info["height"], fp=fingerprint(out_path),
                  rendered_with=self._wanted(it, spec, self.lib.disclaimer_fingerprint(name) if disclaimer else ""))
        self._finish(spec, out_key, out_path, it["duration"], it)
        return out_key, it

    def _finish(self, spec, key, path, duration, it):
        thumb = self.lib.thumb_path(spec["name"], key)
        os.makedirs(os.path.dirname(thumb), exist_ok=True)
        try:
            ss = max(0.0, min(1.0, (duration or 0) * 0.1))
            subprocess.run([self.ffmpeg, "-y", "-v", "error", "-nostdin", "-ss", "%.2f" % ss, "-i", path,
                            "-frames:v", "1", "-vf", "scale=320:-2", thumb],
                           capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            it["flash"] = round(self.flash_rate(path, duration), 2)
        except Exception:
            it["flash"] = 0.0

    def _encode_image(self, src, out, info, dwell, hologram, disclaimer):
        frames = max(2, int(dwell * TARGET_FPS))
        step = 0.2 / frames
        big_w, big_h = TARGET_W * 3, TARGET_H * 3
        graph = self._hologram_chain(hologram, info["width"], info["height"], 1, "0:v", "h", True)
        graph += (";[h]scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,"
                  "pad=%d:%d:-1:-1:color=black,zoompan=z='min(zoom+%.6f,1.2)':d=%d:"
                  "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=%dx%d:fps=%d"
                  % (big_w, big_h, big_w, big_h, step, frames, TARGET_W, TARGET_H, TARGET_FPS))
        inputs = ["-i", src]
        if disclaimer:
            inputs += ["-i", disclaimer]
            graph += ("[z];[1:v]scale=%d:%d[d];[z][d]overlay=0:%d:eof_action=repeat"
                      % (TARGET_W, DISCLAIMER_H, TARGET_H - DISCLAIMER_H))
        graph += (",fade=t=in:st=0:d=1,fade=t=out:st=%.2f:d=1,format=yuv420p[v]" % max(0.0, dwell - 1))
        args = inputs + ["-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames),
                         "-r", str(TARGET_FPS), "-an"] + self._encoder_args() + \
            ["-movflags", "+faststart", "-f", "mp4", out]
        self._run(args, total_seconds=dwell)

    def _encode_video(self, src, out, info, hologram, disclaimer):
        graph = self._hologram_chain(hologram, info["width"], info["height"], info["fps"], "0:v", "h", False)
        graph += ";[h]" + self._fit() + ",fps=%d" % TARGET_FPS
        inputs = ["-i", src]
        if disclaimer:
            inputs += ["-i", disclaimer]
            graph += ("[z];[1:v]scale=%d:%d[d];[z][d]overlay=0:%d:eof_action=repeat"
                      % (TARGET_W, DISCLAIMER_H, TARGET_H - DISCLAIMER_H))
        graph += ",format=yuv420p[v]"
        args = inputs + ["-filter_complex", graph, "-map", "[v]"]
        if info["audio"]:
            args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        else:
            args += ["-an"]
        args += self._encoder_args() + ["-movflags", "+faststart", "-f", "mp4", out]
        self._run(args, total_seconds=info["duration"])

    def _render_show(self, name):
        spec = self.lib.spec(name)
        if spec is None:
            return
        with self.lib.lock:
            meta = self.lib.meta(name)
            want = self.lib.show_hash(name, meta)
            items = meta["items"]
            order = sorted(items, key=lambda k: (items[k].get("order", 10 ** 6), natural_key(k)))
            clips = [(k, items[k]) for k in order
                     if items[k].get("enabled", True) and items[k].get("status") == "ready"
                     and items[k].get("duration")]
        if not want or len(clips) < 2:
            return
        folder = spec["dir"]
        fade = float(self.cfg.data["processing"].get("crossfade_seconds", 2))
        shortest = min(it["duration"] for _, it in clips)
        fade = max(0.3, min(fade, shortest / 2 - 0.1))
        paths = [os.path.join(folder, k) for k, _ in clips]
        durations = [float(it["duration"]) for _, it in clips]
        # the first clip is appended again so the loop point falls inside a crossfade
        seq_paths = paths + [paths[0]]
        seq_durs = durations + [durations[0]]
        n = len(seq_paths)
        has_audio = []
        for p in paths:
            try:
                has_audio.append(self.probe(p)["audio"])
            except LibraryError:
                has_audio.append(False)
        has_audio.append(has_audio[0])
        any_audio = any(has_audio)

        inputs = []
        for p in seq_paths:
            inputs += ["-i", p]
        parts = []
        for i in range(n):
            parts.append("[%d:v]%s,fps=%d,format=yuv420p[v%d]" % (i, self._fit(), TARGET_FPS, i))
        prev = "v0"
        offsets = []
        offset = 0.0
        for i in range(1, n):
            offset += seq_durs[i - 1] - fade
            offsets.append(offset)
            parts.append("[%s][v%d]xfade=transition=fade:duration=%.3f:offset=%.3f[x%d]"
                         % (prev, i, fade, offset, i))
            prev = "x%d" % i
        total = sum(seq_durs) - (n - 1) * fade
        end = total - seq_durs[-1] + fade
        parts.append("[%s]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[vout]" % (prev, fade, end))

        silence_index = n
        if any_audio:
            aprev = None
            for i in range(n):
                if has_audio[i]:
                    parts.append("[%d:a:0]aformat=sample_rates=48000:channel_layouts=stereo[a%d]" % (i, i))
                else:
                    inputs += ["-f", "lavfi", "-t", "%.3f" % seq_durs[i], "-i", "anullsrc=r=48000:cl=stereo"]
                    parts.append("[%d:a]aformat=sample_rates=48000:channel_layouts=stereo[a%d]"
                                 % (silence_index, i))
                    silence_index += 1
                if aprev is None:
                    aprev = "a0"
                else:
                    parts.append("[%s][a%d]acrossfade=d=%.3f:c1=tri:c2=tri[ax%d]" % (aprev, i, fade, i))
                    aprev = "ax%d" % i
            parts.append("[%s]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[aout]" % (aprev, fade, end))

        work = os.path.join(folder, ".work")
        show_dir = os.path.join(folder, ".show")
        os.makedirs(work, exist_ok=True)
        os.makedirs(show_dir, exist_ok=True)
        tmp = os.path.join(work, "show.mp4")
        args = inputs + ["-filter_complex", ";".join(parts), "-map", "[vout]"]
        # a keyframe where each clip begins, so Next/Previous can jump there cleanly
        keyframes = ",".join("%.3f" % o for o in offsets[:len(clips) - 1]) or "0"
        args += ["-force_key_frames", keyframes]
        if any_audio:
            args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
        else:
            args += ["-an"]
        args += self._encoder_args() + ["-movflags", "+faststart", "-f", "mp4", tmp]
        self._run(args, total_seconds=end - fade)

        final = os.path.join(show_dir, "show.mp4")
        os.replace(tmp, final)
        segments = [{"name": clips[0][0], "start": 0.0}]
        for i in range(1, len(clips)):
            segments.append({"name": clips[i][0], "start": round(offsets[i - 1], 3)})
        with self.lib.lock:
            meta = self.lib.meta(name)
            meta["show"] = {"hash": want, "file": os.path.relpath(final, folder),
                            "duration": round(end - fade, 3), "segments": segments,
                            "rendered": datetime.now().isoformat(timespec="seconds")}
            self.lib.save_meta(name, meta)
        self._note("%s show rendered (%d clips)" % (spec["label"], len(clips)))
        log.info("Rendered %s show: %d clips, %.1f s", name, len(clips), end - fade)
