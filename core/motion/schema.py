"""
Prism Motion Graphics Schema & Validator
────────────────────────────────────────
Defines and validates structured Motion Specifications (Motion JSON).
Decouples AI high-level intent from low-level frame rendering.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from .camera import normalize_shot


class MotionValidationError(Exception):
    """Raised when a motion spec violates schema or structural rules."""
    pass


# GSAP's own core ease vocabulary (gsap.min.js, no plugins) — replaces the
# old hand-rolled easeInCubic/etc. names now that runtime.js hands these
# strings straight to gsap.to()'s own `ease` option instead of interpreting
# them itself. A base name optionally followed by GSAP's own parenthesized
# config, e.g. "back.out(1.7)" or "elastic.out(1,0.3)" — see _valid_easing.
SUPPORTED_EASINGS = {
    "none",
    "power1.in", "power1.out", "power1.inOut",
    "power2.in", "power2.out", "power2.inOut",
    "power3.in", "power3.out", "power3.inOut",
    "power4.in", "power4.out", "power4.inOut",
    "back.in", "back.out", "back.inOut",
    "elastic.in", "elastic.out", "elastic.inOut",
    "bounce.in", "bounce.out", "bounce.inOut",
    "circ.in", "circ.out", "circ.inOut",
    "expo.in", "expo.out", "expo.inOut",
    "sine.in", "sine.out", "sine.inOut",
}

_EASE_RE = re.compile(r"^([a-zA-Z0-9]+(?:\.[a-zA-Z]+)?)(\([0-9.,\s-]*\))?$")


def _valid_easing(value: Any) -> bool:
    """True for a known GSAP ease base name, with or without GSAP's own
    parenthesized config (back.out(1.7), elastic.out(1,0.3)) — the paren
    group is only shape-checked (digits/commas/dot/minus), GSAP itself
    validates the actual parameter values at tween time.
    """
    if not isinstance(value, str):
        return False
    m = _EASE_RE.match(value.strip())
    return bool(m) and m.group(1) in SUPPORTED_EASINGS


# The animatable surface every primitive shares — replaces the old closed
# enter/exit "type" enum (fade_in/pop_in/slide_up/slide_down) with channels
# a tween can target on ANY node, not just the four archetypes runtime.js
# used to hand-branch on. Each maps to a real CSS-expressible property in
# runtime.js's tween builder (opacity, transform components, filter:blur,
# clip-path inset, background-position-x for shimmer sweeps, and
# stroke-dashoffset for SVG draw-ins shared by charts/arrows/the brand mark).
TWEEN_CHANNELS = {
    "opacity", "x", "y", "scale", "scaleX", "scaleY", "rotation",
    "skewX", "skewY", "blur", "clipInset", "backgroundPositionX",
    "strokeDashoffset",
}

# Named, curated cross-scene transitions — ported from core.reel_web's own
# `.cut-*` library (push/push_up/squeeze/zoom, themselves adapted from
# HyperFrames under Apache 2.0, see prism_gui/NOTICE) plus two new ones
# translated from the Meridian HyperFrames reference (blur_swoosh,
# light_leak). A curated set, not free-text, for the same reason the
# easing fallback rotation is curated rather than open: a bounded, varied
# menu beats an unconstrained surface for consistency across scenes.
# `morph` is the one the continuity compiler (continuity.py) stamps onto
# any cut a `continuity_key` crosses: a plain container crossfade, so the
# pose-matched subject bridge is the only thing that moves.
TRANSITION_NAMES = {
    "push", "push_up", "squeeze", "zoom", "blur_swoosh", "light_leak",
    "morph",
}

# Keyed by the keyword with every space/hyphen/underscore stripped and
# lowercased, so "top-left", "top_left", "Top Left" and "topleft" all match
# the same entry — an LLM asked for an anchor is about as likely to write
# any of those forms as the others.
# A node's "layer" is an optional semantic label — background / midground /
# foreground / accent / finish — that, if given, sets a sensible z_index
# band automatically so the model can say WHAT something is instead of
# picking an arbitrary stacking number. Entirely opt-in: a node with no
# "layer" behaves exactly as before (z_index still defaults to 0). This
# only exists to back the "brand_launch" skeleton in generate.py — every
# other request path never sets it and is unaffected.
_LAYER_Z_DEFAULT = {
    "background": 0,
    "midground": 10,
    "foreground": 20,
    "accent": 30,
    "finish": 40,
}

# Numeric material/depth props: (min, max, default-or-None). A value out of
# range is clamped, not rejected (same tolerance as camera zoom); a
# default is only written where Python-side tooling must agree with the
# runtime (depth, so parallax planning and inspect see the same number).
_MATERIAL_PROPS = {
    "glass_panel": {"blur": (0.0, 40.0, None), "transmission": (0.0, 1.0, None),
                    "border_light": (0.0, 1.0, None), "inner_shadow": (0.0, 1.0, None),
                    "specular": (0.0, 1.0, None), "light_angle": (-360.0, 360.0, None)},
    "light_field": {"intensity": (0.0, 1.0, None), "spread": (0.2, 2.0, None),
                    "drift": (0.0, 200.0, None)},
    "depth_layer": {"depth": (0.2, 2.5, 1.0)},
}


def _clamp_material_props(node: dict) -> None:
    table = _MATERIAL_PROPS.get(node.get("type"))
    if not table:
        return
    for key, (lo, hi, default) in table.items():
        if key in node:
            try:
                node[key] = max(lo, min(hi, float(node[key])))
            except (TypeError, ValueError):
                if default is None:
                    del node[key]
                else:
                    node[key] = default
        elif default is not None:
            node[key] = default


_ANCHOR_KEYWORDS = {
    "center": (0.5, 0.5), "middle": (0.5, 0.5),
    "top": (0.5, 0.0), "topcenter": (0.5, 0.0), "topmiddle": (0.5, 0.0),
    "bottom": (0.5, 1.0), "bottomcenter": (0.5, 1.0), "bottommiddle": (0.5, 1.0),
    "left": (0.0, 0.5), "centerleft": (0.0, 0.5), "middleleft": (0.0, 0.5),
    "right": (1.0, 0.5), "centerright": (1.0, 0.5), "middleright": (1.0, 0.5),
    "topleft": (0.0, 0.0), "lefttop": (0.0, 0.0),
    "topright": (1.0, 0.0), "righttop": (1.0, 0.0),
    "bottomleft": (0.0, 1.0), "leftbottom": (0.0, 1.0),
    "bottomright": (1.0, 1.0), "rightbottom": (1.0, 1.0),
}


def _normalize_anchor(value: Any) -> list[float]:
    """A node's anchor, coerced to a real [x, y] pair.

    Accepts what schema.py always accepted (a 2-number list/tuple) and,
    now, common keyword strings a CSS-literate model reaches for instead
    ("center", "top-left", ...) — mapped to the equivalent fraction rather
    than just rejected, since that's what was actually meant. Anything
    else (missing, malformed, an unrecognized word) falls back to
    [0.5, 0.5] instead of passing a broken value through: see
    _validate_node's comment for exactly how destructive that is
    downstream (NaN positions from spreading a string in JS).
    """
    if isinstance(value, str):
        key = value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        if key in _ANCHOR_KEYWORDS:
            x, y = _ANCHOR_KEYWORDS[key]
            return [x, y]
        return [0.5, 0.5]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            pass
    return [0.5, 0.5]


def _validate_tween(tween: Any) -> Optional[dict]:
    """One {channel, from, to, easing, delay} entry inside an enter/exit
    block's "tweens" list. Returns None (drop silently) if the channel is
    missing/unrecognized or from/to aren't numbers — one bad tween must
    not invalidate its siblings, same tolerant-but-structural philosophy
    as the rest of this file.
    """
    if not isinstance(tween, dict):
        return None
    channel = tween.get("channel")
    if channel not in TWEEN_CHANNELS:
        return None
    try:
        frm = float(tween["from"])
        to = float(tween["to"])
    except (KeyError, TypeError, ValueError):
        return None
    out: dict[str, Any] = {"channel": channel, "from": frm, "to": to}
    easing = tween.get("easing")
    if easing and _valid_easing(easing):
        out["easing"] = easing
    if tween.get("delay") is not None:
        try:
            out["delay"] = max(0.0, float(tween["delay"]))
        except (TypeError, ValueError):
            pass
    return out


def _validate_animation_block(block: Any) -> Optional[dict]:
    """One enter/exit block: {time, duration, tweens: [...]}. A block with
    no valid tweens left after filtering is dropped entirely — an enter/
    exit that ends up animating nothing is not a real block, same as
    before when a malformed "type" fell through.
    """
    if not isinstance(block, dict):
        return None
    tweens_in = block.get("tweens")
    if not isinstance(tweens_in, list):
        return None
    tweens = [t for t in (_validate_tween(t) for t in tweens_in) if t]
    if not tweens:
        return None
    out: dict[str, Any] = {"tweens": tweens}
    try:
        out["time"] = float(block.get("time", 0.0))
    except (TypeError, ValueError):
        out["time"] = 0.0
    try:
        out["duration"] = max(0.05, float(block.get("duration", 0.6)))
    except (TypeError, ValueError):
        out["duration"] = 0.6
    return out


_ANIM_BLOCK_KEYS = ("enter", "exit", "secondary_motion", "follow")


def _block_from_from_to(block: dict) -> dict:
    """A block a writer shaped as {at, duration, from: {channel: v},
    to: {channel: v}} (a common Framer/GSAP habit) becomes the {time,
    duration, tweens: [...]} shape the runtime plays. A channel present on
    one side only holds its value — no motion, but no invented one."""
    out = dict(block)
    if "at" in out and "time" not in out:
        out["time"] = out.pop("at")
    frm, to = out.get("from"), out.get("to")
    if isinstance(frm, dict) or isinstance(to, dict):
        frm = frm if isinstance(frm, dict) else {}
        to = to if isinstance(to, dict) else {}
        ease = out.get("easing") or out.get("ease")
        tweens = []
        for ch in list(frm) + [c for c in to if c not in frm]:
            a, b = frm.get(ch, to.get(ch)), to.get(ch, frm.get(ch))
            tw = {"channel": ch, "from": a, "to": b}
            if ease:
                tw["easing"] = ease
            tweens.append(tw)
        out["tweens"] = tweens
        out.pop("from", None)
        out.pop("to", None)
        out.pop("ease", None)
    return out


def normalize_aliases(node: dict) -> None:
    """The writer's near-misses become the keys the runtime reads, in
    place. Every rule here was seen in a real generated reel (10 Sep 2026,
    the Alphakore run went blank for its last three scenes on exactly
    these): x/y for position, text for content, source for src, stroke
    for an arrow's color, glow/ring on an orb, animation blocks at the
    node's top level, from/to dicts for tweens, and a shape_group made of
    parts. Tolerance that DROPS a value silently is what let that reel
    render empty; tolerance that maps it is what this is."""
    if "position" not in node:
        pos = None
        if isinstance(node.get("x"), (int, float)) or isinstance(node.get("y"), (int, float)):
            pos = [node.get("x", 0), node.get("y", 0)]
        elif isinstance(node.get("pos"), (list, tuple)):
            pos = list(node["pos"])
        if pos is not None:
            node["position"] = pos
    elif isinstance(node.get("position"), dict):
        d = node["position"]
        node["position"] = [d.get("x", 0), d.get("y", 0)]
    node.pop("x", None)
    node.pop("y", None)
    node.pop("pos", None)

    node_type = node.get("type")
    if node_type == "text" and "content" not in node:
        for alt in ("text", "label", "value", "string"):
            if isinstance(node.get(alt), (str, int, float)):
                node["content"] = str(node.pop(alt))
                break
    if node_type == "text" and isinstance(node.get("runs"), list):
        # Coloured parts of one line. Keep only well-formed runs, and let the
        # joined words stand as `content` so every check that reads content
        # (caption match, bounds, Studio) sees what is on screen.
        runs = [r for r in node["runs"] if isinstance(r, dict) and str(r.get("text", "")) != ""]
        if runs:
            node["runs"] = runs
            if not isinstance(node.get("content"), str) or not node.get("content"):
                node["content"] = "".join(str(r["text"]) for r in runs)
        else:
            node.pop("runs", None)
    if node_type == "text" and isinstance(node.get("content"), str):
        # Markdown escapes travel from the copy stage into the scene
        # ("www\\.alphakore\\.com" reached the screen with its backslashes).
        node["content"] = re.sub(r"\\([.\-_*#()\[\]!])", r"\1", node["content"])
    if node_type == "image" and "src" not in node:
        for alt in ("source", "url", "asset", "path", "image"):
            if isinstance(node.get(alt), str):
                node["src"] = node.pop(alt)
                break
    if node_type in ("shape_arrow", "arrow", "spline_tree") and "color" not in node:
        for alt in ("stroke", "fill", "colour"):
            if isinstance(node.get(alt), str):
                node["color"] = node[alt]
                break
    if node_type == "orb":
        if "glow_color" not in node and isinstance(node.get("glow"), str):
            node["glow_color"] = node.pop("glow")
        ring = node.get("ring")
        if "ring_radius" not in node and isinstance(ring, (int, float)):
            node["ring_radius"] = node.pop("ring")
        elif "ring_color" not in node and isinstance(ring, str):
            node["ring_color"] = node.pop("ring")
    if node_type in ("shape_group", "shapes") and isinstance(node.get("parts"), list):
        node["type"] = "group"
        parts = [dict(pt) for pt in node.pop("parts") if isinstance(pt, dict)]
        for i, pt in enumerate(parts):
            pt.setdefault("id", f"{node.get('id', 'group')}_part{i}")
            for key in ("stroke", "stroke_width", "color"):
                if key not in pt and key in node:
                    pt[key] = node[key]
        node["children"] = parts + [c for c in (node.get("children") or []) if isinstance(c, dict)]

    if node_type == "particle_field":
        # Phases written as bare kinds ("ring", "scatter"), as {kind: ...}
        # without a "layout", or with a layout that is itself a string —
        # the Alphakore story-contract run (10 Sep 2026) crashed the
        # renderer on the first frame with ["ring", "scatter"].
        phases = node.get("phases")
        if isinstance(phases, str):
            phases = [phases]
        if isinstance(phases, list):
            fixed = []
            for k, ph in enumerate(phases):
                if isinstance(ph, str):
                    ph = {"at": round(k * 1.2, 2), "layout": {"kind": ph}}
                if not isinstance(ph, dict):
                    continue
                ph = dict(ph)
                lay = ph.get("layout")
                if isinstance(lay, str):
                    ph["layout"] = {"kind": lay}
                elif not isinstance(lay, dict):
                    kind = ph.pop("kind", None)
                    ph["layout"] = ({"kind": kind, **{k2: v for k2, v in ph.items()
                                                     if k2 in ("box", "y", "rows", "cell", "x0", "x1", "x",
                                                               "spread", "y0", "y1", "center", "centre",
                                                               "radius", "points", "jitter", "cluster")}}
                                    if isinstance(kind, str) else {"kind": "scatter"})
                ph.setdefault("at", round(k * 1.2, 2))
                fixed.append(ph)
            node["phases"] = fixed

    anim = node.get("animation")
    if not isinstance(anim, dict):
        anim = {}
    for key in _ANIM_BLOCK_KEYS:
        if key in node and key not in anim and isinstance(node[key], dict):
            anim[key] = node.pop(key)
        else:
            node.pop(key, None)
    for key in ("enter", "exit"):
        if isinstance(anim.get(key), dict):
            anim[key] = _block_from_from_to(anim[key])
    if anim:
        node["animation"] = anim


def validate_motion_spec(data: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Validate raw JSON or dict against the Prism Motion Graphics Schema.
    Returns a cleaned, normalized specification dict or raises MotionValidationError.

    Design principle: be maximally tolerant. Unknown fields are preserved as-is
    so the AI can freely express visual parameters. Only structural invariants are enforced.
    """
    if isinstance(data, str):
        try:
            spec = json.loads(data)
        except json.JSONDecodeError as e:
            raise MotionValidationError(f"Invalid JSON string: {e}") from e
    elif isinstance(data, dict):
        spec = dict(data)
    else:
        raise MotionValidationError(f"Expected dict or JSON string, got {type(data).__name__}")

    # ── Visual block: fully open passthrough ─────────────────────────────────
    # The AI specifies any visual parameters it wants. We never restrict this.
    visual = spec.get("visual")
    if visual is not None and not isinstance(visual, dict):
        spec["visual"] = {}  # safe reset if AI sent a non-dict by mistake

    # ── 1. Project metadata ───────────────────────────────────────────────────
    project = spec.get("project")
    if not isinstance(project, dict):
        project = {}
        spec["project"] = project

    # If visual.background is set, let it populate project.background too
    if visual and isinstance(visual, dict) and "background" in visual:
        project.setdefault("background", visual["background"])

    project.setdefault("width", 1080)
    project.setdefault("height", 1920)
    project.setdefault("fps", 30)
    project.setdefault("duration", 10.0)
    project.setdefault("background", "#090D16")

    # A small NAMED palette (not just background+one accent) and a real
    # display/body font pairing — threaded through every scene so a whole
    # generation shares one identity instead of each scene picking its own
    # colors/font fresh. Both are open string values (any hex, any Google
    # Font name) — the model's actual choice is steered by doctrine in
    # generate.py's prompt, not constrained here; this layer only enforces
    # the SHAPE (right keys, string values) so downstream CSS generation
    # never chokes on a non-string.
    palette = project.get("palette")
    if not isinstance(palette, dict):
        palette = {}
    for key in ("bg_a", "bg_b", "ink", "accent", "accent2"):
        if key in palette and not isinstance(palette[key], str):
            del palette[key]
    project["palette"] = palette

    type_cfg = project.get("type")
    if not isinstance(type_cfg, dict):
        type_cfg = {}
    for key in ("display_font", "body_font", "google_fonts_url"):
        if key in type_cfg and not isinstance(type_cfg[key], str):
            del type_cfg[key]
    project["type"] = type_cfg

    width    = int(project["width"])
    height   = int(project["height"])
    fps      = int(project["fps"])
    duration = float(project["duration"])

    if width < 320 or width > 3840:
        raise MotionValidationError(f"Invalid width {width}. Must be between 320 and 3840.")
    if height < 320 or height > 3840:
        raise MotionValidationError(f"Invalid height {height}. Must be between 320 and 3840.")
    if fps < 10 or fps > 120:
        raise MotionValidationError(f"Invalid fps {fps}. Must be between 10 and 120.")
    if duration <= 0.5 or duration > 300.0:
        raise MotionValidationError(f"Invalid duration {duration}s. Must be between 0.5s and 300s.")

    # ── 1b. Production audio ─────────────────────────────────────────────────
    # Files to mux at export (render.py) and the tempo the timing grid
    # (beats.py) lays beats on. Nothing here generates sound; a temporary
    # score never enters the design contract. Paths are checked at render.
    audio = spec.get("audio")
    if audio is not None and not isinstance(audio, dict):
        del spec["audio"]
    elif isinstance(audio, dict):
        clean: Dict[str, Any] = {}
        for key in ("music", "voiceover"):
            if isinstance(audio.get(key), str) and audio[key].strip():
                clean[key] = audio[key].strip()
        for key, lo, hi in (("bpm", 40.0, 240.0), ("offset", -60.0, 60.0),
                            ("music_volume", 0.0, 1.0)):
            if audio.get(key) is not None:
                try:
                    clean[key] = max(lo, min(hi, float(audio[key])))
                except (TypeError, ValueError):
                    pass
        if clean:
            spec["audio"] = clean
        else:
            del spec["audio"]

    # ── 2. Camera validation ──────────────────────────────────────────────────
    camera = spec.get("camera")
    if camera is not None and not isinstance(camera, dict):
        raise MotionValidationError("camera must be an object if provided.")
    if camera:
        tracks = camera.get("tracks", [])
        if not isinstance(tracks, list):
            raise MotionValidationError("camera.tracks must be a list.")
        for i, track in enumerate(tracks):
            if not isinstance(track, dict):
                raise MotionValidationError(f"camera.tracks[{i}] must be an object.")
            track.setdefault("time", 0.0)
            if "zoom" in track:
                try:
                    z = float(track["zoom"])
                    track["zoom"] = max(0.1, min(12.0, z))  # clamp silently
                except (ValueError, TypeError):
                    track["zoom"] = 1.0  # safe default
            if "easing" in track:
                if not _valid_easing(track["easing"]):
                    track["easing"] = "power2.inOut"  # silent fallback, GSAP-native
            if "position" in track:
                pos = track["position"]
                if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
                    raise MotionValidationError(f"camera track position must be [x, y], got {pos}")

    # ── 3. Scenes / Nodes ─────────────────────────────────────────────────────
    scenes = spec.get("scenes")
    if scenes is None:
        if "nodes" in spec and isinstance(spec["nodes"], list):
            spec["scenes"] = [{
                "id": "scene_0",
                "start": 0.0,
                "duration": duration,
                "nodes": spec["nodes"]
            }]
            scenes = spec["scenes"]
        else:
            raise MotionValidationError("Motion specification must contain a 'scenes' list or 'nodes' list.")

    if not isinstance(scenes, list) or not scenes:
        raise MotionValidationError("'scenes' must be a non-empty list.")

    node_ids: set[str] = set()

    def _validate_node(node: Any, path: str):
        if not isinstance(node, dict):
            raise MotionValidationError(f"{path} must be an object.")
        node_id = str(node.get("id") or f"node_{len(node_ids)}")
        node["id"] = node_id
        if node_id in node_ids:
            node_id = f"{node_id}_{len(node_ids)}"
            node["id"] = node_id
        node_ids.add(node_id)

        # Editor-facing IDs stay unique. A continuity key separately pairs
        # scene-local instances of one visual subject (particles -> core ->
        # orb) so the cinematic compiler and Studio can preserve its motion.
        if "continuity_key" in node:
            key = node.get("continuity_key")
            if isinstance(key, str):
                key = key.strip()
            if isinstance(key, str) and key and len(key) <= 80:
                node["continuity_key"] = key
            else:
                del node["continuity_key"]

        node["type"] = str(node.get("type", "group"))
        normalize_aliases(node)
        node_type = node["type"]

        _clamp_material_props(node)

        # A node the writer gave no position at all (and that has no
        # from/to or hub of its own) is drawn at the origin and must never
        # be a camera target — resolver.py reads this flag.
        if "position" not in node and not any(k in node for k in ("from", "to", "hub")):
            node["_no_position"] = True
        node.setdefault("position", [0, 0])
        node.setdefault("scale",    [1.0, 1.0])
        node.setdefault("rotation", 0.0)
        node.setdefault("opacity",  1.0)
        # Layer-based z_index default has to run BEFORE the plain
        # setdefault below, since setdefault only fills a missing key —
        # whichever runs first wins. An unrecognized "layer" string is
        # dropped rather than guessed at, same as an invalid easing name.
        layer = node.get("layer")
        if isinstance(layer, str) and layer in _LAYER_Z_DEFAULT:
            node.setdefault("z_index", _LAYER_Z_DEFAULT[layer])
        elif "layer" in node:
            del node["layer"]
        node.setdefault("z_index",  0)
        # setdefault alone isn't enough for anchor — it only fills a MISSING
        # key, and a wrong-TYPE one (present, just broken) sails through
        # untouched. Measured on a real generated reel: every image node
        # used anchor: "center" (a bare string, not [0.5, 0.5]) — a
        # reasonable guess for someone used to CSS-style keyword anchors,
        # and genuinely destructive downstream: runtime.js's Node
        # constructor does `[...props.anchor]`, and spreading a STRING
        # produces an array of its individual CHARACTERS ('c','e','n',...),
        # so anchor[0]/[1] become non-numeric and every position multiply
        # against them is NaN — the node silently never appears anywhere.
        node["anchor"] = _normalize_anchor(node.get("anchor"))

        # animation.enter / animation.exit are now a composable list of
        # {channel, from, to, easing, delay} tweens rather than a closed
        # "type" enum (fade_in/pop_in/slide_up/slide_down) — see
        # TWEEN_CHANNELS above. A block that fails validation entirely
        # (missing/malformed) is dropped, not coerced to a default type,
        # since there's no longer a "type" to fall back to.
        anim = node.get("animation")
        if isinstance(anim, dict):
            for block_name in ("enter", "exit"):
                validated = _validate_animation_block(anim.get(block_name))
                if validated:
                    anim[block_name] = validated
                elif block_name in anim:
                    del anim[block_name]

            secondary = anim.get("secondary_motion")
            if isinstance(secondary, dict):
                for numeric_key in ("freq", "amount"):
                    if numeric_key in secondary:
                        try:
                            secondary[numeric_key] = float(secondary[numeric_key])
                        except (TypeError, ValueError):
                            del secondary[numeric_key]

            follow = anim.get("follow")
            if isinstance(follow, dict):
                for numeric_key in ("lag", "damping"):
                    if numeric_key in follow:
                        try:
                            follow[numeric_key] = float(follow[numeric_key])
                        except (TypeError, ValueError):
                            del follow[numeric_key]

        if "children" in node:
            if not isinstance(node["children"], list):
                raise MotionValidationError(f"{path}.children must be a list.")
            for c_idx, child in enumerate(node["children"]):
                _validate_node(child, f"{path}.children[{c_idx}]")

    # A colour written as a reference into the project palette
    # ("project.palette.accent", "palette.ink", "$accent") is the palette's
    # value; an unknown reference is dropped so CSS never sees it.
    palette_values = {str(k): v for k, v in (project.get("palette") or {}).items()
                      if isinstance(v, str)}

    def _colour_ref(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        key = value.strip()
        for prefix in ("project.palette.", "palette.", "$", "var(--motion-", "{{palette.", "{palette."):
            if key.lower().startswith(prefix):
                name = key[len(prefix):].strip("}) ")
                return palette_values.get(name, None)
        return value

    def _resolve_colour_refs(node: dict) -> None:
        for key in ("fill", "color", "colour", "tint", "stroke", "glow_color", "rim", "core", "shadow_color"):
            if key in node:
                got = _colour_ref(node[key])
                if got is None:
                    del node[key]
                else:
                    node[key] = got
        pal = node.get("palette")
        if isinstance(pal, list):
            node["palette"] = [c for c in (_colour_ref(v) for v in pal) if isinstance(c, str)] or None
            if node["palette"] is None:
                del node["palette"]
        for child in node.get("children") or []:
            if isinstance(child, dict):
                _resolve_colour_refs(child)

    for s_idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise MotionValidationError(f"scenes[{s_idx}] must be an object.")
        for node in scene.get("nodes") or []:
            if isinstance(node, dict):
                _resolve_colour_refs(node)
        scene.setdefault("id", f"scene_{s_idx}")
        scene.setdefault("start", 0.0)
        scene.setdefault("duration", duration)
        # Which named transition (see TRANSITION_NAMES) plays as this scene
        # cuts in from the PREVIOUS one — absent/invalid means resolver.py
        # picks one via the same seeded-rotation approach the easing
        # fallback already uses, rather than defaulting every unset scene
        # to the identical transition. Meaningless (and dropped) on scene 0,
        # which has nothing before it to cut in from.
        t_in = scene.get("transition_in")
        if s_idx == 0 or t_in not in TRANSITION_NAMES:
            scene.pop("transition_in", None)
        # The easing of the continuity bridge across the cut INTO this
        # scene (Studio's handoff handle) — dropped unless it is a GSAP ease.
        if "handoff_easing" in scene:
            if s_idx == 0 or not _valid_easing(scene.get("handoff_easing")):
                del scene["handoff_easing"]
            else:
                scene["handoff_easing"] = str(scene["handoff_easing"]).strip()
        # The scene's camera intent (see camera.py) — a bare intent string
        # or {intent, target, zoom, easing}. Unusable means dropped, so the
        # compiled camera curve simply carries the previous shot through.
        if "shot" in scene:
            shot = normalize_shot(scene.get("shot"))
            if shot is None:
                del scene["shot"]
            else:
                if "easing" in shot and not _valid_easing(shot["easing"]):
                    del shot["easing"]
                scene["shot"] = shot
        nodes = scene.get("nodes", [])
        if not isinstance(nodes, list):
            raise MotionValidationError(f"scenes[{s_idx}].nodes must be a list.")
        for n_idx, node in enumerate(nodes):
            _validate_node(node, f"scenes[{s_idx}].nodes[{n_idx}]")

    return spec



@dataclass
class MotionProject:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    duration: float = 10.0
    background: str = "#090D16"
    scenes: List[Dict[str, Any]] = field(default_factory=list)
    camera: Optional[Dict[str, Any]] = None
    assets: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MotionProject":
        valid = validate_motion_spec(data)
        p = valid["project"]
        return cls(
            width=p["width"],
            height=p["height"],
            fps=p["fps"],
            duration=p["duration"],
            background=p["background"],
            scenes=valid.get("scenes", []),
            camera=valid.get("camera"),
            assets=valid.get("assets", []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": {
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "duration": self.duration,
                "background": self.background,
            },
            "scenes": self.scenes,
            "camera": self.camera,
            "assets": self.assets,
        }
