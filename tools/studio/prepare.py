#!/usr/bin/env python3
"""
prepare.py - turn any video or picture into exactly what the Pi plays.

    python3 prepare.py IN [-o OUT.mp4] [--seconds N] [--no-loop] [--keep-dark] [--max-bright 0.35]

Output: H.264 MP4, 1280x800, 30 fps, yuv420p, letterboxed on black, near-black
made pure black (invisible on the mesh), the tail crossfaded into the head so it
loops without a jump, and a poster JPEG next to it. Refuses to write anything
that fails the checks. Needs ffmpeg and ffprobe on the laptop.

The browser studio does the same job without a terminal; this is the command
line version, used by the 3D renders and the AI clip tool.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

W, H, FPS = 1280, 800, 30
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("ffprobe can't read %s" % path)
    data = json.loads(out.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        sys.exit("No picture in %s" % path)
    duration = float(data.get("format", {}).get("duration") or 0)
    audio = any(s["codec_type"] == "audio" for s in data["streams"])
    return video, duration, audio


def mean_luma(path, seconds=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-an", "-vf", "fps=5,scale=64:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    vals = [float(l.split("YAVG=")[1]) for l in out.splitlines() if "YAVG=" in l]
    return (max(vals) / 255.0, sum(vals) / len(vals) / 255.0) if vals else (0.0, 0.0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("src")
    parser.add_argument("-o", "--out")
    parser.add_argument("--seconds", type=float, default=0, help="for pictures: how long the clip lasts (default 12)")
    parser.add_argument("--no-loop", action="store_true", help="don't blend the end into the start")
    parser.add_argument("--keep-dark", action="store_true", help="leave near-black pixels alone")
    parser.add_argument("--max-bright", type=float, default=0.35, help="dim frames brighter than this (mean luminance)")
    args = parser.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("%s is not installed. On a Mac: brew install ffmpeg" % tool)
    src = os.path.abspath(args.src)
    out = os.path.abspath(args.out or os.path.splitext(src)[0] + " (balcony).mp4")
    is_image = src.lower().endswith(IMAGE_EXT)
    video, duration, audio = probe(src)
    if is_image:
        duration = args.seconds or 12.0

    fit = "scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,pad=%d:%d:-1:-1:color=black,setsar=1" % (W, H, W, H)
    black = "" if args.keep_dark else ",lutyuv=y='if(lt(val,28),16,val)'"      # 16 is video black; below 28 is treated as black
    fade = 1.0 if not args.no_loop and duration > 3 else 0
    tmp = out + ".part.mp4"
    if is_image:
        frames = int(duration * FPS)
        graph = ("[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:-1:-1:color=black,"
                 "zoompan=z='min(zoom+0.0006,1.15)':d=%d:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=%dx%d:fps=%d%s,"
                 "fade=t=in:st=0:d=1,fade=t=out:st=%.2f:d=1,format=yuv420p[v]" % (W * 3, H * 3, W * 3, H * 3, frames, W, H, FPS, black, duration - 1))
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames)]
    elif fade:
        # play the clip, then crossfade its tail into a second copy's head, and cut at the join:
        # the result starts and ends on the same frame
        base = "%s,fps=%d%s,format=yuv420p" % (fit, FPS, black)
        graph = ("[0:v]%s[a];[1:v]%s[b];[a][b]xfade=transition=fade:duration=%.2f:offset=%.3f[x];"
                 "[x]trim=start=%.2f:end=%.3f,setpts=PTS-STARTPTS[v]" % (base, base, fade, duration - fade, fade, duration))
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-i", src, "-filter_complex", graph, "-map", "[v]"]
        if audio:
            cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-t", "%.3f" % (duration - fade)]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", "%s,fps=%d%s,format=yuv420p" % (fit, FPS, black)]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
    if not audio or is_image:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-movflags", "+faststart", tmp]
    print("Converting", os.path.basename(src), "->", os.path.basename(out))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("ffmpeg failed: " + res.stderr.strip().splitlines()[-1])

    peak, mean = mean_luma(tmp, 600)
    if peak > args.max_bright:
        k = args.max_bright / peak
        print("Brightest frame is %.0f%% lit; dimming to %.0f%% so it doesn't look like a bare bulb on the mesh." % (peak * 100, args.max_bright * 100))
        dimmed = out + ".dim.mp4"
        res = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", tmp, "-vf", "lutyuv=y='clip(val*%.3f,16,235)'" % k,
                              "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", dimmed],
                             capture_output=True, text=True)
        if res.returncode != 0:
            sys.exit("ffmpeg failed while dimming: " + res.stderr.strip().splitlines()[-1])
        os.replace(dimmed, tmp)

    # checks before we hand it over
    v, d, _ = probe(tmp)
    if (v.get("codec_name"), int(v["width"]), int(v["height"]), v.get("pix_fmt")) != ("h264", W, H, "yuv420p"):
        os.remove(tmp)
        sys.exit("Output isn't in the Pi's format (got %s %sx%s %s); not written." % (v.get("codec_name"), v.get("width"), v.get("height"), v.get("pix_fmt")))
    os.replace(tmp, out)
    poster = os.path.splitext(out)[0] + ".jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % min(1.0, d * 0.1), "-i", out, "-frames:v", "1", "-vf", "scale=320:-2", poster],
                   capture_output=True)
    print("Ready: %s (%.1f s, brightest frame %.0f%% lit)" % (out, d, min(peak, args.max_bright) * 100))
    return out


if __name__ == "__main__":
    main()
