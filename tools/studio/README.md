# Balcony Studio (laptop side)

Everything creative happens here. The Pi only plays files.

## The browser studio

`slides/index.html`, served by the Pi at `https://balcony.local:8443/studio/`
(or double-click the file). "Make a show" renders a template show to MP4 in the
browser and sends it to the Pi. "Send a video" converts any video into the Pi's
native format on the laptop and sends it. Needs Chrome, Edge or Safari 16.4+.

## Command-line tools (need Python 3 and ffmpeg: `brew install ffmpeg`)

- `prepare.py IN [-o OUT.mp4]` turns any video or picture into the Pi's format:
  1280x800, 30 fps, H.264, black letterbox, near-black made black, the end
  blended into the start, over-bright frames dimmed, plus a poster JPEG. It
  refuses to write anything that fails the checks.
- `send.py FILE --playlist NAME` uploads a prepared file to the Pi and starts
  the playlist. `--pi http://...` and `--pin` if needed.
- `make_clip.py "prompt" --playlist NAME` asks an AI video service for a
  black-background clip, prepares it, opens a preview, and sends it when you say
  yes. Set `RUNWAY_API_KEY` first. Kling and Firefly have places to plug in.
  Prompts naming trademarked characters are refused.

## 3D illusion renders (need Blender: `brew install --cask blender`)

`render3d/render.py` builds a scene in Blender with the camera placed where a
person stands on the sidewalk (see `render3d/venue.json`: picture size and
height, viewer distance and eye height). Anything in front of the screen plane
then really looks as if it comes out toward that person.

    /Applications/Blender.app/Contents/MacOS/Blender -b -P render3d/render.py -- \
        --template text_emerge --text "VOTE NOV 3" --seconds 12 --out ~/Desktop/vote.mp4
    python3 send.py ~/Desktop/vote.mp4 --playlist campaign

Templates: `snow_depth` (three layers of snow, the nearest in front of the
frame), `ghost_float` (a black-background figure, `--figure ghost.png` or `.mp4`,
drifting in and out through the frame), `spider_lunge` (down on a thread, wait,
lunge out past the frame, retreat), `text_emerge` (3D letters push forward,
hold, recede; `--wordmark logo.png` for a logo), `particle_assemble_3d` (the
words assemble from dots in depth), `light_tunnel` (rings receding behind the
frame). `--frame neon|metal|arch|none`, `--accent "#f2a33a"`, `--samples 8` for
a faster draft. Renders are deterministic and loop seamlessly; the output is the
Pi's native format, no further preparation needed. About 5 to 20 frames a
second on a laptop, so a 12-second loop takes a minute or two.

Fonts: Anton and Inter (SIL Open Font License) in `fonts/`. `vendor/mp4-muxer.js`
writes MP4 in the browser (MIT).
