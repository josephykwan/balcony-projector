#!/usr/bin/env python3
"""
make_clip.py - ask an AI video service for a black-background clip, prepare it,
preview it, and (if you say yes) send it to the Pi.

    python3 make_clip.py "a translucent ghost drifting slowly" --playlist halloween
                         [--provider runway|kling|firefly] [--image start.png] [--seconds 5]
                         [--out clip.mp4] [--yes]

Keys come from the environment, never from this file:
    RUNWAY_API_KEY      https://dev.runwayml.com  (paid plan needed for commercial use)
    KLING_API_KEY       not wired yet
    FIREFLY_API_KEY     not wired yet; Adobe Firefly is the one to use for campaign material

Prompts are rewritten into the pattern that works on the mesh: single figure,
solid black background, centred, slow drifting motion, no camera movement,
seamless loop. Prompts naming trademarked characters are refused.

Written against the Runway API documentation (Gen-3/Gen-4 image-to-video,
version header 2024-11-06). It has not been run against a live key yet, so the
first real run may need a small fix; the error messages say what came back.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKED = ["mickey", "minnie", "disney", "pixar", "marvel", "batman", "superman", "spider-man", "spiderman", "pokemon",
           "pikachu", "star wars", "darth", "yoda", "harry potter", "mario", "sonic", "elsa", "frozen", "peppa", "paw patrol",
           "hello kitty", "snoopy", "garfield", "simpsons", "shrek", "minions", "barbie", "lego", "grinch", "charlie brown"]
PATTERN = ("{prompt}. Single figure, solid pure black background, centred in frame, slow drifting motion, "
           "no camera movement, seamless loop, high contrast, no text.")


def refuse_trademarks(prompt):
    low = prompt.lower()
    for word in BLOCKED:
        if word in low:
            sys.exit("Not that one: \"%s\" is a trademarked character. Describe your own figure instead (a ghost, a witch, a cat...)." % word)


def http(method, url, headers, body=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        sys.exit("%s %s failed: HTTP %d %s" % (method, url, exc.code, detail))


def download(url, path):
    with urllib.request.urlopen(url, timeout=600) as res, open(path, "wb") as f:
        while True:
            chunk = res.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


# --------------------------------------------------------------------------
# providers: each returns the path of a downloaded video
# --------------------------------------------------------------------------

def image_data_uri(path):
    import base64
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    with open(path, "rb") as f:
        return "data:image/%s;base64,%s" % ("jpeg" if ext == "jpg" else ext, base64.b64encode(f.read()).decode())


def black_start_image():
    """Runway's image-to-video wants a starting picture; a black one keeps the background black."""
    path = os.path.join(HERE, ".black-1280x800.png")
    if not os.path.isfile(path):
        import struct
        import zlib
        w, h = 1280, 800
        raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def provider_runway(prompt, seconds, image, out):
    key = os.environ.get("RUNWAY_API_KEY")
    if not key:
        sys.exit("Set RUNWAY_API_KEY first (from https://dev.runwayml.com). Example: export RUNWAY_API_KEY=key_...")
    base = "https://api.dev.runwayml.com/v1"
    headers = {"Authorization": "Bearer " + key, "X-Runway-Version": "2024-11-06"}
    body = {"model": "gen4_turbo", "promptImage": image_data_uri(image or black_start_image()),
            "promptText": prompt, "ratio": "1280:720", "duration": 10 if seconds > 5 else 5}
    task = http("POST", base + "/image_to_video", headers, body)
    task_id = task.get("id")
    if not task_id:
        sys.exit("Runway didn't return a task id: %s" % json.dumps(task)[:300])
    print("Runway is working on it (task %s)..." % task_id)
    for _ in range(120):
        time.sleep(5)
        status = http("GET", base + "/tasks/" + task_id, headers)
        state = status.get("status")
        if state == "SUCCEEDED":
            urls = status.get("output") or []
            if not urls:
                sys.exit("Runway finished but sent no video: %s" % json.dumps(status)[:300])
            download(urls[0], out)
            return out
        if state in ("FAILED", "CANCELLED"):
            sys.exit("Runway %s: %s" % (state.lower(), status.get("failure", status.get("failureCode", "no reason given"))))
        print("  ...", state.lower() if state else "waiting")
    sys.exit("Gave up waiting for Runway after ten minutes.")


def provider_kling(prompt, seconds, image, out):
    sys.exit("Kling isn't wired up yet. When you have an API key, add a provider_kling() like provider_runway() "
             "and it slots in here.")


def provider_firefly(prompt, seconds, image, out):
    sys.exit("Adobe Firefly isn't wired up yet. It is the right choice for campaign material (licensed training data); "
             "add a provider_firefly() when you have the credentials.")


PROVIDERS = {"runway": provider_runway, "kling": provider_kling, "firefly": provider_firefly}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--provider", default="runway", choices=sorted(PROVIDERS))
    parser.add_argument("--image", default="", help="optional starting picture (black background)")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--out", default="")
    parser.add_argument("--yes", action="store_true", help="send without asking")
    parser.add_argument("--pi", default=os.environ.get("BALCONY_PI", "http://balcony.local:8080"))
    args = parser.parse_args()

    refuse_trademarks(args.prompt)
    prompt = PATTERN.format(prompt=args.prompt.strip().rstrip("."))
    stem = re.sub(r"[^A-Za-z0-9]+", " ", args.prompt).strip()[:40] or "clip"
    raw = os.path.abspath(args.out or (stem + " (raw).mp4"))
    print("Asking %s for: %s" % (args.provider, prompt))
    PROVIDERS[args.provider](prompt, args.seconds, args.image, raw)
    print("Downloaded", raw)

    prepared = os.path.splitext(raw)[0].replace(" (raw)", "") + ".mp4"
    res = subprocess.run([sys.executable, os.path.join(HERE, "prepare.py"), raw, "-o", prepared])
    if res.returncode != 0:
        sys.exit("prepare.py did not accept the clip.")
    if sys.platform == "darwin":
        subprocess.run(["open", prepared])       # QuickTime preview
    if not args.yes:
        answer = input("Send \"%s\" to the Pi's %s playlist? [y/N] " % (os.path.basename(prepared), args.playlist)).strip().lower()
        if answer not in ("y", "yes"):
            print("Not sent. It is saved at", prepared)
            return
    subprocess.run([sys.executable, os.path.join(HERE, "send.py"), prepared, "--playlist", args.playlist, "--pi", args.pi], check=False)


if __name__ == "__main__":
    main()
