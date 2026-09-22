#!/usr/bin/env python3
"""
render.py - 3D illusion loops for the balcony screen, rendered by Blender.

    blender -b -P render.py -- --template snow_depth --out ~/Desktop/snow.mp4 [--seconds 20]
                                [--venue venue.json] [--text "VOTE NOV 3"] [--wordmark logo.png]
                                [--figure ghost.mp4] [--frame neon|metal|arch|none] [--accent "#f2a33a"]

Templates: snow_depth, ghost_float, spider_lunge, text_emerge, particle_assemble_3d, light_tunnel.

The camera is placed where a person on the sidewalk stands (see venue.json) and
looks at the screen through a lens shift, so anything drawn in front of the screen
plane really does look as if it is coming out toward that person. The screen plane
is z = 0; +z is toward the street. Everything is on black, loops seamlessly (the
last frame equals the first), and comes out as H.264 MP4 1280x800 at 30 fps, which
is exactly what the Pi plays.

Deterministic: every animation is a function of the frame number.
"""

import argparse
import json
import math
import os
import random
import sys

import bpy
from mathutils import Vector

W, H, FPS = 1280, 800, 30
FT = 0.3048          # scene units are metres; the venue file is in feet


# --------------------------------------------------------------------------
# venue and camera
# --------------------------------------------------------------------------

def load_venue(path):
    venue = {"picture_width_ft": 7.5, "picture_height_ft": 7.5 * 10 / 16, "picture_bottom_ft": 11.0,
             "viewer_distance_ft": 45.0, "viewer_eye_ft": 5.2, "viewer_offset_ft": 0.0, "frame": "neon", "accent": "#f2a33a"}
    if path and os.path.isfile(path):
        with open(path) as f:
            venue.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    return venue


def setup_camera(scene, venue):
    """A camera at the viewer's spot, shifted so its view exactly covers the picture."""
    pw, ph = venue["picture_width_ft"] * FT, venue["picture_height_ft"] * FT
    d = venue["viewer_distance_ft"] * FT
    dy = (venue["picture_bottom_ft"] + venue["picture_height_ft"] / 2 - venue["viewer_eye_ft"]) * FT
    dx = -venue["viewer_offset_ft"] * FT
    cam_data = bpy.data.cameras.new("ViewerCam")
    cam = bpy.data.objects.new("ViewerCam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = Vector((-dx, -dy, d))               # the picture's centre is the origin; +z toward the street
    cam.rotation_euler = (0, 0, 0)                     # looking down -z at the screen plane
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.angle = 2 * math.atan((pw / 2) / d)
    cam_data.shift_x = dx / pw
    cam_data.shift_y = dy / pw                         # shift is in fractions of the sensor width
    cam_data.clip_start = 0.05
    cam_data.clip_end = 200
    scene.camera = cam
    return cam, pw, ph


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def hex_rgb(text):
    text = text.lstrip("#")
    return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def emission_material(name, color, strength=4.0, alpha_from_image=None):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    emit = nodes.new("ShaderNodeEmission")
    emit.inputs["Strength"].default_value = strength
    if alpha_from_image is not None:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = alpha_from_image
        tex.extension = "CLIP"
        links.new(tex.outputs["Color"], emit.inputs["Color"])
        # black in the picture is see-through: use its brightness as alpha
        bw = nodes.new("ShaderNodeRGBToBW")
        links.new(tex.outputs["Color"], bw.inputs["Color"])
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].position = 0.06
        ramp.color_ramp.elements[1].position = 0.35
        links.new(bw.outputs["Val"], ramp.inputs["Fac"])
        mix = nodes.new("ShaderNodeMixShader")
        trans = nodes.new("ShaderNodeBsdfTransparent")
        links.new(ramp.outputs["Color"], mix.inputs["Fac"])
        links.new(trans.outputs["BSDF"], mix.inputs[1])
        links.new(emit.outputs["Emission"], mix.inputs[2])
        links.new(mix.outputs["Shader"], out.inputs["Surface"])
        for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND"), ("shadow_method", "NONE")):
            if hasattr(mat, attr):
                try:
                    setattr(mat, attr, value)
                except TypeError:
                    pass
    else:
        emit.inputs["Color"].default_value = (*color, 1.0)
        links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


def metal_material(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (0.75, 0.75, 0.78, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.35
    return mat


def add_driver(obj, path, expression, index=-1):
    """Animate a property with an expression of `frame` (deterministic, loops if periodic)."""
    fcurve = obj.driver_add(path, index) if index >= 0 else obj.driver_add(path)
    fcurve.driver.type = "SCRIPTED"
    fcurve.driver.expression = expression
    return fcurve


def sphere(name, radius, location, material):
    bpy.ops.mesh.primitive_ico_sphere_add(radius=radius, subdivisions=2, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.data.materials.append(material)
    try:
        bpy.ops.object.shade_smooth()
    except RuntimeError:
        pass
    return obj


def keyframe(obj, path, frame, value, index=-1):
    """Insert a keyframe. New keys use the smooth interpolation set in clear_scene()."""
    if index >= 0:
        getattr(obj, path)[index] = value
    else:
        setattr(obj, path, value)
    obj.keyframe_insert(data_path=path, index=index, frame=frame)


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        edit = bpy.context.preferences.edit
        edit.keyframe_new_interpolation_type = "SINE"      # eased motion, nothing sudden
        edit.keyframe_new_handle_type = "AUTO_CLAMPED"
    except (AttributeError, TypeError):
        pass
    scene = bpy.context.scene
    try:
        scene.view_settings.view_transform = "Standard"     # keep the accent colours as chosen, not AgX-washed
        scene.view_settings.look = "None"
    except TypeError:
        pass
    scene.render.resolution_x, scene.render.resolution_y = W, H
    scene.render.resolution_percentage = 100
    scene.render.fps = FPS
    scene.render.film_transparent = False
    world = bpy.data.worlds.new("Black")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    bg.inputs["Color"].default_value = (0, 0, 0, 1)
    bg.inputs["Strength"].default_value = 0
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = engine
            break
        except TypeError:
            continue
    eevee = getattr(scene, "eevee", None)
    if eevee is not None:
        for attr, value in (("use_bloom", True), ("bloom_intensity", 0.12), ("taa_render_samples", 24)):
            if hasattr(eevee, attr):
                setattr(eevee, attr, value)
    return scene


def find_ffmpeg():
    import shutil
    for candidate in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def output_frames(scene, folder, frames):
    """Blender writes PNG frames; ffmpeg turns them into the Pi's H.264 MP4 afterwards."""
    scene.frame_start, scene.frame_end = 0, frames      # frame `frames` == frame 0: seamless loop
    r = scene.render
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.compression = 30
    r.filepath = os.path.join(folder, "frame_")


def encode_mp4(folder, out):
    import subprocess
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("ffmpeg is needed to make the MP4 (frames are in %s). On a Mac: brew install ffmpeg" % folder)
    cmd = [ffmpeg, "-y", "-v", "error", "-framerate", str(FPS), "-i", os.path.join(folder, "frame_%04d.png"),
           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-movflags", "+faststart", out]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit("ffmpeg failed: " + res.stderr.strip())


# --------------------------------------------------------------------------
# the window frame
# --------------------------------------------------------------------------

def build_frame(kind, pw, ph, accent):
    if not kind or kind == "none":
        return
    inset = 0.08 * pw / 2
    hw, hh = pw / 2 - inset, ph / 2 - inset
    if kind == "arch":
        # a stone arch: a bevelled curve, dim so it never reads as a bright block
        curve = bpy.data.curves.new("Arch", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.045 * pw / 2
        curve.bevel_resolution = 4
        spline = curve.splines.new("POLY")
        pts = [(-hw, -hh)] + [(-hw, -hh + (hh * 1.4) * t) for t in (0.5, 1.0)]
        for i in range(0, 19):
            a = math.pi - i * math.pi / 18
            pts.append((math.cos(a) * hw, hh * 0.4 + math.sin(a) * hh * 0.6))
        pts += [(hw, -hh)]
        spline.points.add(len(pts) - 1)
        for p, (x, y) in zip(spline.points, pts):
            p.co = (x, y, 0, 1)
        obj = bpy.data.objects.new("Arch", curve)
        bpy.context.scene.collection.objects.link(obj)
        obj.data.materials.append(emission_material("Stone", (0.55, 0.5, 0.42), 0.9))
        return
    thick = (0.005 if kind == "neon" else 0.014) * pw
    mat = emission_material("FrameNeon", hex_rgb(accent), 1.0) if kind == "neon" else metal_material("FrameMetal")
    for name, loc, scale in (("FrameTop", (0, hh, 0), (hw + thick, thick, thick)), ("FrameBottom", (0, -hh, 0), (hw + thick, thick, thick)),
                             ("FrameLeft", (-hw, 0, 0), (thick, hh, thick)), ("FrameRight", (hw, 0, 0), (thick, hh, thick))):
        bpy.ops.mesh.primitive_cube_add(location=loc)
        bar = bpy.context.active_object
        bar.name = name
        bar.scale = scale
        bar.data.materials.append(mat)
    if kind == "metal":
        light = bpy.data.lights.new("FrameLight", "SUN")
        light.energy = 3
        lo = bpy.data.objects.new("FrameLight", light)
        bpy.context.scene.collection.objects.link(lo)
        lo.rotation_euler = (math.radians(50), 0, math.radians(30))


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

def t_snow_depth(scene, args, pw, ph, frames):
    """Three layers of snow at different depths; the nearest passes in front of the frame."""
    rnd = random.Random(7)
    white = emission_material("Snow", (1, 1, 1), 1.0)
    layers = ((-2.0 * pw, 60, 0.012), (-0.6 * pw, 70, 0.018), (0.5 * pw, 50, 0.026))   # z (depth), count, size
    for li, (z, count, size) in enumerate(layers):
        spread = pw * (1.6 + max(0, -z) / pw)          # farther layers cover more so the view stays full
        for i in range(count):
            x0, y0 = rnd.uniform(-spread / 2, spread / 2), rnd.uniform(-ph, ph)
            s = sphere("Flake%d_%d" % (li, i), size * pw / 2, (x0, y0, z), white)
            speed = (0.12 + 0.08 * li) * ph            # metres per second, nearer falls faster
            period = frames                              # whole loops per render, so it wraps cleanly
            cycles = max(1, round(speed * (frames / FPS) / (2 * ph)))
            s["x0"], s["y0"], s["phase"] = x0, y0, rnd.uniform(0, 6.28)
            add_driver(s, "location", "%.4f - ((frame / %d) * %d * %.4f + %.4f) %% %.4f" % (ph, period, cycles, 2 * ph, ph - y0, 2 * ph), 1)
            add_driver(s, "location", "%.4f + %.4f * sin(2*pi*frame/%d * %d + %.3f)" % (x0, 0.05 * pw, period, max(1, cycles), s["phase"]), 0)


def t_light_tunnel(scene, args, pw, ph, frames):
    """Receding rings of light behind the frame, rushing gently toward the viewer."""
    mat = emission_material("Ring", hex_rgb(args.accent), 0.8)
    n = 12
    depth = 6 * pw
    for i in range(n):
        bpy.ops.mesh.primitive_torus_add(major_radius=pw * 0.55, minor_radius=pw * 0.004, major_segments=64, location=(0, 0, 0))
        ring = bpy.context.active_object
        ring.name = "Ring%d" % i
        ring.data.materials.append(mat)
        # each ring moves from far to the frame plane, then wraps; whole cycles per loop
        add_driver(ring, "location", "-%.4f + ((frame / %d) * %.4f + %.4f) %% %.4f" % (depth, frames, depth, i * depth / n, depth), 2)
        add_driver(ring, "rotation_euler", "2*pi*frame/%d * %d + %.3f" % (frames, 1, i * 0.4), 2)


def _text_or_wordmark(args, pw, ph, name="Wordmark"):
    """Extruded 3D text, or a plane carrying the wordmark image."""
    if args.wordmark and os.path.isfile(args.wordmark):
        img = bpy.data.images.load(os.path.abspath(args.wordmark))
        ratio = img.size[1] / max(1, img.size[0])
        w = pw * 0.7
        bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
        obj = bpy.context.active_object
        obj.name = name
        obj.scale = (w, w * ratio, 1)
        obj.data.materials.append(emission_material("WordmarkMat", (1, 1, 1), 1.0, alpha_from_image=img))
        return obj
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = args.text or "VOTE NOV 3"
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.extrude = 0.06 * pw / 2
    curve.bevel_depth = 0.006 * pw / 2
    curve.size = pw * 0.19
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(emission_material("TextMat", (1, 1, 1), 1.0))
    # shrink long lines to fit
    bpy.context.view_layer.update()
    width = obj.dimensions.x
    if width > pw * 0.86:
        curve.size *= pw * 0.86 / width
    return obj


def t_text_emerge(scene, args, pw, ph, frames):
    """The words push forward out of the frame, hold, then recede. A light sweeps across."""
    obj = _text_or_wordmark(args, pw, ph)
    back, front = -0.8 * pw, 0.45 * pw
    hold_in, hold_out = int(frames * 0.3), int(frames * 0.7)
    keyframe(obj, "location", 0, back, 2)
    keyframe(obj, "location", hold_in, front, 2)
    keyframe(obj, "location", hold_out, front, 2)
    keyframe(obj, "location", frames, back, 2)
    keyframe(obj, "rotation_euler", 0, math.radians(-8), 0)
    keyframe(obj, "rotation_euler", hold_in, 0, 0)
    keyframe(obj, "rotation_euler", hold_out, 0, 0)
    keyframe(obj, "rotation_euler", frames, math.radians(-8), 0)
    light = bpy.data.lights.new("Sweep", "SPOT")
    light.energy = 400
    light.spot_size = math.radians(40)
    lo = bpy.data.objects.new("Sweep", light)
    scene.collection.objects.link(lo)
    lo.location = (-pw, 0, 1.2 * pw)
    lo.rotation_euler = (0, 0, 0)
    add_driver(lo, "location", "-%.3f + %.3f * ((frame / %d) %% 1.0)" % (pw, 2 * pw, frames), 0)


def t_particle_assemble_3d(scene, args, pw, ph, frames):
    """The wordmark assembles from dots scattered in depth, holds, then scatters again."""
    target = _text_or_wordmark(args, pw, ph, "Target")
    bpy.context.view_layer.update()
    # sample points spread over the letters' front faces, weighted by area
    rnd = random.Random(11)
    dep = bpy.context.evaluated_depsgraph_get()
    mesh = target.evaluated_get(dep).to_mesh()
    mesh.calc_loop_triangles()
    tris = []
    for tri in mesh.loop_triangles:
        a, b, c = (target.matrix_world @ mesh.vertices[i].co for i in tri.vertices)
        if tri.normal.z < 0.5 and not target.type == "MESH":
            continue                      # only the front face of extruded text
        tris.append((a, b, c, tri.area))
    total = sum(t[3] for t in tris) or 1.0
    verts = []
    for _ in range(1100):
        pick = rnd.uniform(0, total)
        acc = 0.0
        for a, b, c, area in tris:
            acc += area
            if acc >= pick:
                r1, r2 = math.sqrt(rnd.random()), rnd.random()
                verts.append(a * (1 - r1) + b * (r1 * (1 - r2)) + c * (r1 * r2))
                break
    target.evaluated_get(dep).to_mesh_clear()
    target.hide_render = True
    mat = emission_material("Dot", hex_rgb(args.accent), 1.0)
    t_in, t_hold = int(frames * 0.35), int(frames * 0.65)
    for i, v in enumerate(verts):
        start = Vector((rnd.uniform(-pw, pw), rnd.uniform(-ph, ph), rnd.uniform(-1.5 * pw, 0.6 * pw)))
        s = sphere("Dot%d" % i, 0.0035 * pw, start, mat)
        delay = int(rnd.uniform(0, frames * 0.08))
        keyframe(s, "location", 0, tuple(start))
        keyframe(s, "location", min(t_in, delay + int(frames * 0.25)), tuple(v))
        keyframe(s, "location", t_hold, tuple(v))
        keyframe(s, "location", frames, tuple(start))


def t_ghost_float(scene, args, pw, ph, frames):
    """A figure on a black-background clip drifts in and out of the frame, with depth."""
    figure = args.figure
    if figure and not os.path.isfile(figure):
        print("ghost_float: figure file not found:", figure)
    if figure and os.path.isfile(figure):
        img = bpy.data.images.load(os.path.abspath(figure))
        print("ghost_float: figure %s, %dx%d, %s" % (os.path.basename(figure), img.size[0], img.size[1], img.source))
        ratio = img.size[1] / max(1, img.size[0]) if img.size[0] else 1.0
        if img.source == "MOVIE" or figure.lower().endswith((".mp4", ".mov", ".webm")):
            img.source = "MOVIE"
    else:
        img = None
    h = ph * 0.9
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
    plane = bpy.context.active_object
    plane.name = "Figure"
    plane.scale = ((h / ratio) if img else h * 0.6, h, 1)
    if img:
        mat = emission_material("FigureMat", (1, 1, 1), 1.0, alpha_from_image=img)
        tex = [n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE"][0]
        if img.source == "MOVIE":
            tex.image_user.use_cyclic = True
            tex.image_user.use_auto_refresh = True
            tex.image_user.frame_duration = img.frame_duration
    else:
        mat = emission_material("FigureMat", (0.9, 0.95, 1.0), 0.9)
    plane.data.materials.append(mat)
    # a slow figure-eight through the frame plane: behind, then out in front of the frame
    add_driver(plane, "location", "%.3f * sin(2*pi*frame/%d)" % (0.35 * pw, frames), 0)
    add_driver(plane, "location", "%.3f * sin(4*pi*frame/%d + 1.0)" % (0.08 * ph, frames), 1)
    add_driver(plane, "location", "%.3f * sin(2*pi*frame/%d + 1.57)" % (0.5 * pw, frames), 2)
    add_driver(plane, "rotation_euler", "0.12 * sin(2*pi*frame/%d)" % frames, 1)


def t_spider_lunge(scene, args, pw, ph, frames):
    """A spider comes down on a thread, waits, lunges out past the frame, and climbs back."""
    body_mat = emission_material("Spider", (0.16, 0.16, 0.19), 1.0)
    eye_mat = emission_material("Eyes", (1.0, 0.15, 0.08), 1.0)
    root = bpy.data.objects.new("SpiderRoot", None)
    scene.collection.objects.link(root)
    r = 0.06 * pw
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=(0, 0, 0))
    body = bpy.context.active_object
    body.name = "Body"
    body.scale = (1, 1.3, 0.8)
    body.data.materials.append(body_mat)
    body.parent = root
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r * 0.55, location=(0, r * 1.35, 0))
    head = bpy.context.active_object
    head.name = "Head"
    head.data.materials.append(body_mat)
    head.parent = root
    for i, (ex, ey) in enumerate(((-0.25, 1.75), (0.25, 1.75), (-0.12, 1.85), (0.12, 1.85))):
        bpy.ops.mesh.primitive_uv_sphere_add(radius=r * 0.09, location=(ex * r, ey * r, r * 0.35))
        eye = bpy.context.active_object
        eye.name = "Eye%d" % i
        eye.data.materials.append(eye_mat)
        eye.parent = root
    for side in (-1, 1):
        for i in range(4):
            ang = math.radians(35 + i * 30) * side
            bpy.ops.mesh.primitive_cylinder_add(radius=r * 0.06, depth=r * 2.6, location=(side * r * 1.5, (1.5 - i) * r * 0.45, 0))
            leg = bpy.context.active_object
            leg.name = "Leg%d%d" % (i, 1 if side > 0 else 0)
            leg.rotation_euler = (math.radians(20), ang, 0)
            leg.data.materials.append(body_mat)
            leg.parent = root
            # legs twitch a little while it waits
            add_driver(leg, "rotation_euler", "%.3f + 0.08 * sin(2*pi*frame/%d * 6 + %d)" % (ang, frames, i), 1)
    # the thread: a thin cylinder from the top of the picture down to the spider
    bpy.ops.mesh.primitive_cylinder_add(radius=0.0015 * pw, depth=1, location=(0, 0, 0))
    thread = bpy.data.objects.get(bpy.context.active_object.name)
    thread.name = "Thread"
    thread.data.materials.append(emission_material("Thread", (0.7, 0.7, 0.7), 1.0))
    # motion: down (0-25%), wait (25-45%), lunge forward (45-55%), hold (55-62%), retreat (62-75%), climb (75-100%)
    top = ph * 0.75
    hang = ph * 0.1
    seq = [(0.0, top, -0.3 * pw), (0.25, hang, -0.3 * pw), (0.45, hang, -0.3 * pw), (0.55, hang - ph * 0.15, 0.55 * pw),
           (0.62, hang - ph * 0.15, 0.55 * pw), (0.75, hang, -0.3 * pw), (1.0, top, -0.3 * pw)]
    for t, y, z in seq:
        f = int(t * frames)
        keyframe(root, "location", f, y, 1)
        keyframe(root, "location", f, z, 2)
    # the thread follows: its top stays at the picture's top edge, its bottom at the spider
    add_driver(thread, "location", "SPY/2 + %.4f/2" % (ph / 2 * 1.02), 1)
    var = thread.animation_data.drivers[0].driver.variables.new()
    var.name = "SPY"
    var.type = "TRANSFORMS"
    var.targets[0].id = root
    var.targets[0].transform_type = "LOC_Y"
    add_driver(thread, "scale", "%.4f/2 - SPZ" % (ph * 1.02), 2)
    var2 = thread.animation_data.drivers[1].driver.variables.new()
    var2.name = "SPZ"
    var2.type = "TRANSFORMS"
    var2.targets[0].id = root
    var2.targets[0].transform_type = "LOC_Y"
    add_driver(thread, "location", "SPZ2", 2)
    var3 = thread.animation_data.drivers[2].driver.variables.new()
    var3.name = "SPZ2"
    var3.type = "TRANSFORMS"
    var3.targets[0].id = root
    var3.targets[0].transform_type = "LOC_Z"


TEMPLATES = {
    "snow_depth": t_snow_depth,
    "ghost_float": t_ghost_float,
    "spider_lunge": t_spider_lunge,
    "text_emerge": t_text_emerge,
    "particle_assemble_3d": t_particle_assemble_3d,
    "light_tunnel": t_light_tunnel,
}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Render a 3D illusion loop for the balcony screen")
    parser.add_argument("--template", required=True, choices=sorted(TEMPLATES))
    parser.add_argument("--out", required=True, help="output .mp4")
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--venue", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "venue.json"))
    parser.add_argument("--text", default="")
    parser.add_argument("--wordmark", default="")
    parser.add_argument("--figure", default="", help="black-background image or video for ghost_float")
    parser.add_argument("--frame", default=None, choices=["neon", "metal", "arch", "none"])
    parser.add_argument("--accent", default=None)
    parser.add_argument("--samples", type=int, default=0, help="render samples (lower = faster)")
    args = parser.parse_args(argv)

    venue = load_venue(args.venue)
    frame_kind = args.frame if args.frame is not None else venue.get("frame", "neon")
    args.accent = args.accent or venue.get("accent", "#f2a33a")
    frames = max(30, int(round(args.seconds * FPS)))

    scene = clear_scene()
    if args.samples and hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
        scene.eevee.taa_render_samples = args.samples
    cam, pw, ph = setup_camera(scene, venue)
    build_frame(frame_kind, pw, ph, args.accent)
    TEMPLATES[args.template](scene, args, pw, ph, frames)
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    import shutil
    import tempfile
    frames_dir = tempfile.mkdtemp(prefix="balcony-frames-")
    output_frames(scene, frames_dir, frames)
    print("Rendering %s: %d frames at %dx%d" % (args.template, frames + 1, W, H))
    bpy.ops.render.render(animation=True)
    encode_mp4(frames_dir, out)
    shutil.rmtree(frames_dir, ignore_errors=True)
    print("Done:", out)


if __name__ == "__main__":
    main()
