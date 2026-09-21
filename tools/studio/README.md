# Balcony Studio (laptop side)

Everything creative happens here, in a browser. The Pi only plays files.

- `slides/index.html` is the whole studio. Open it from the Pi at
  `http://balcony.local:8080/studio/`, or double-click the file. "Make a show"
  renders a template show to MP4 in the browser (WebCodecs H.264) and sends it
  to the Pi. "Send a video" converts any video into the Pi's native format on
  the laptop and sends it.
- `fonts/` holds Anton (headlines) and Inter (second lines, footer), both under
  the SIL Open Font License.
- `vendor/mp4-muxer.js` writes the MP4 container (MIT, by Vanilagy).

Needs Chrome, Edge or Safari 16.4+. Everything stays on the laptop and the Pi;
nothing is uploaded elsewhere.

Not built yet: the Blender 3D templates and the AI clip tool from the brief.
