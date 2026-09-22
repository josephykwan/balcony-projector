#!/usr/bin/env python3
"""
send.py - put a prepared video on the Pi and start it.

    python3 send.py FILE.mp4 --playlist halloween [--pi http://balcony.local:8080] [--pin 1234] [--no-play]

Only accepts files already in the Pi's format (H.264 MP4 1280x800); run
prepare.py first for anything else. Uses the Pi's own upload endpoint, so it
works from any laptop on the Wi-Fi with nothing but Python.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid


def check_format(path):
    if not shutil.which("ffprobe"):
        return True          # can't check; the Pi will still play it if it is right
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return out == "h264,1280,800"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--pi", default=os.environ.get("BALCONY_PI", "http://balcony.local:8080"))
    parser.add_argument("--pin", default=os.environ.get("BALCONY_PIN", ""))
    parser.add_argument("--no-play", action="store_true", help="upload without starting the playlist")
    parser.add_argument("--force", action="store_true", help="send even if the format check fails")
    args = parser.parse_args()
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        sys.exit("No such file: " + path)
    if not args.force and not check_format(path):
        sys.exit("%s is not in the Pi's format. Run prepare.py on it first (or --force)." % os.path.basename(path))

    boundary = "----balcony" + uuid.uuid4().hex
    fields = {"playlist": args.playlist, "replace": "1"}
    if not args.no_play:
        fields["play"] = "1"
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    with open(path, "rb") as f:
        data = f.read()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: video/mp4\r\n\r\n"
             % (boundary, os.path.basename(path))).encode() + data + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(args.pi.rstrip("/") + "/api/upload", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    if args.pin:
        req.add_header("X-Pin", args.pin)
    print("Sending %s (%.1f MB) to %s, playlist %s..." % (os.path.basename(path), len(data) / 1e6, args.pi, args.playlist))
    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            reply = json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            reply = json.loads(exc.read().decode())
        except ValueError:
            reply = {}
        sys.exit("The Pi said no: " + reply.get("error", "HTTP %d" % exc.code))
    except urllib.error.URLError as exc:
        sys.exit("Can't reach the Pi at %s (%s). Is it on and on the same Wi-Fi?" % (args.pi, exc.reason))
    print("Done. %s" % ("The Pi is playing it now." if reply.get("playing") else "It is in the %s folder." % args.playlist))


if __name__ == "__main__":
    main()
