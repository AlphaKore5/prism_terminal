"""
Prism Motion Graphics Semantic Resolver
────────────────────────────────────────
Translates domain-specific targets, UI element IDs, and auto-framing targets
into unified scene-graph coordinates and animations.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import continuity as _continuity
from . import camera as _camera

# Same curated set schema.py validates transition_in against. Rotation
# (hash of the scene's own id, not Math.random/an incrementing counter) so
# a scene missing transition_in still gets a real one deterministically —
# every cut gets a genuine transition by default, matching the reference
# this engine targets (the Meridian benchmark has zero hard cuts), the same
# "don't let an unset value silently mean the flattest option" fix already
# applied to easing fallback in schema.py/runtime.js.
_TRANSITION_ROTATION = ["push", "blur_swoosh", "light_leak", "push_up", "squeeze", "zoom"]


def _hash_seed(seed: str) -> int:
    h = 0
    for ch in str(seed):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _desugar_ui_mockup(node: Dict[str, Any]) -> Dict[str, Any]:
    """A domain_ui_mockup node was never a real runtime primitive — it's
    always been composable from shape_rect (the window chrome) + shape_rect/
    text children (its `elements`), so it's expanded here, before the spec
    ever reaches the runtime, rather than teaching runtime.js a type it
    doesn't need. Keeps the real primitive count small while the schema/
    prompt vocabulary the model sees stays rich.
    """
    width = float(node.get("width", 860))
    height = float(node.get("height", 520))
    title = node.get("title", "")
    chrome: Dict[str, Any] = {
        "id": f"{node.get('id', 'ui')}_chrome",
        "type": "shape_rect",
        "position": list(node.get("position", [0, 0])),
        "width": width, "height": height, "radius": 18,
        "is_glass": True,
        "z_index": node.get("z_index", 0),
        "anchor": list(node.get("anchor", [0.5, 0.5])),
        "animation": node.get("animation"),
        "children": [],
    }
    if title:
        chrome["children"].append({
            "id": f"{node.get('id', 'ui')}_title",
            "type": "text", "content": title, "font_size": 22, "font_weight": 600,
            "fill": "rgba(255,255,255,0.85)",
            "position": [-width / 2 + 24, -height / 2 + 30], "anchor": [0, 0.5],
        })
    for i, el in enumerate(node.get("elements", []) or []):
        el_pos = el.get("position", [0, 0])
        if el.get("type") == "stat_card":
            chrome["children"].append({
                "id": f"{node.get('id', 'ui')}_el{i}_card",
                "type": "shape_rect", "position": list(el_pos), "width": 200, "height": 90,
                "radius": 12, "fill": "rgba(255,255,255,0.06)",
            })
            chrome["children"].append({
                "id": f"{node.get('id', 'ui')}_el{i}_val",
                "type": "text", "content": str(el.get("value", "")), "font_size": 28,
                "font_weight": 700, "fill": el.get("color", "#FFFFFF"), "position": list(el_pos),
            })
        else:  # badge / generic label
            chrome["children"].append({
                "id": f"{node.get('id', 'ui')}_el{i}",
                "type": "text", "content": str(el.get("label", "")), "font_size": 18,
                "font_weight": 600, "fill": el.get("color", "#FFFFFF"), "position": list(el_pos),
            })
    return chrome


def _resolve_asset_srcs(node: Dict[str, Any], uris: Dict[str, str]) -> None:
    """Swap an `image` node's `src: "asset:<name>"` for the real data: URI,
    in place, recursively. Naturally idempotent — once resolved, `src` no
    longer starts with `asset:`, so re-running this (e.g. re-rendering an
    already-resolved saved spec) is a no-op, unlike the time-offset step
    below, which needs an explicit guard because it's additive rather than
    a plain replace.
    """
    src = node.get("src")
    if isinstance(src, str) and src.startswith("asset:"):
        name = src[len("asset:"):]
        if name in uris:
            node["src"] = uris[name]
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _resolve_asset_srcs(child, uris)


def _absolute_scale_tweens(node: Dict[str, Any]) -> None:
    """Read a scale tween written as ABSOLUTE values as absolute.

    The runtime adds a scale tween's from/to to the node's own scale (the
    catalogue says so: "from -0.3" means 0.3 smaller than where it settles).
    A model asked for "a pop down to 1x" writes scale 1.08 -> 1.0, and the
    runtime drew the node at 2.08x -> 2.0x: a model-written headline was
    twice its size and cut off at both edges (found 12 Nov 2026, by reading the
    node's host transform, matrix(2, 0, 0, 2, ...)). Offsets people mean are
    small or negative; a scale tween whose two ends are both >= 0.4 is
    absolute and is converted to offsets from the node's scale. Idempotent: the
    converted values are below 0.4.
    """
    anim = node.get("animation")
    if isinstance(anim, dict):
        scale = node.get("scale") or [1.0, 1.0]
        for block in ("enter", "exit"):
            tweens = (anim.get(block) or {}).get("tweens") if isinstance(anim.get(block), dict) else None
            for tw in tweens or []:
                if not isinstance(tw, dict) or tw.get("channel") not in ("scale", "scaleX", "scaleY"):
                    continue
                try:
                    frm, to = float(tw["from"]), float(tw["to"])
                except (KeyError, TypeError, ValueError):
                    continue
                if frm >= 0.4 and to >= 0.4:
                    base = float(scale[1] if tw["channel"] == "scaleY" and len(scale) > 1 else scale[0])
                    tw["from"], tw["to"] = round(frm - base, 4), round(to - base, 4)
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            _absolute_scale_tweens(child)


def _offset_node_times(node: Dict[str, Any], offset: float) -> None:
    """Shift a node's (and its children's) authored animation times by
    `offset`, in place. Scenes are written scene-local (a node's `enter`/
    `exit`/track keyframes count from 0 at that scene's own start,
    matching how core.reel_web's per-scene prompts work) — this is what
    turns that into the single global timeline runtime.js actually plays.
    """
    _absolute_scale_tweens(node)
    anim = node.get("animation")
    if isinstance(anim, dict):
        for block_name in ("enter", "exit"):
            block = anim.get(block_name)
            if isinstance(block, dict) and "time" in block:
                try:
                    block["time"] = float(block["time"]) + offset
                except (TypeError, ValueError):
                    pass
        tracks = anim.get("tracks")
        if isinstance(tracks, list):
            for track in tracks:
                keyframes = track.get("keyframes") if isinstance(track, dict) else None
                if isinstance(keyframes, list):
                    for kf in keyframes:
                        if isinstance(kf, dict) and "time" in kf:
                            try:
                                kf["time"] = float(kf["time"]) + offset
                            except (TypeError, ValueError):
                                pass
    # A primitive's OWN content animation (a text mode's reveal, an
    # arrow's or tree's draw-on, a particle field's phases) is registered
    # on the same global timeline by registerContentAnimation() and was
    # never offset — so every scene after the first showed its text
    # already revealed and its lines already drawn. These are scene-local
    # in the spec like enter/exit, and shift here the same way.
    node_type = node.get("type")
    if node_type in _CONTENT_TIME_TYPES:
        for key in ("reveal_start", "draw_start"):
            if key in node or key in _CONTENT_TIME_TYPES[node_type]:
                try:
                    node[key] = float(node.get(key, 0.0) or 0.0) + offset
                except (TypeError, ValueError):
                    node[key] = offset
    # A cursor's moves and a prompt box's send moment are scene-local too.
    if node_type == "cursor" and isinstance(node.get("path"), list):
        for seg in node["path"]:
            if isinstance(seg, dict):
                try:
                    seg["at"] = float(seg.get("at", 0.0) or 0.0) + offset
                except (TypeError, ValueError):
                    seg["at"] = offset
    if node_type == "ui_checklist" and isinstance(node.get("items"), list):
        for it in node["items"]:
            if isinstance(it, dict):
                try:
                    it["at"] = float(it.get("at", 0.0) or 0.0) + offset
                except (TypeError, ValueError):
                    it["at"] = offset
    if node_type == "ui_window":
        try:
            node["play_at"] = float(node.get("play_at", 0.0) or 0.0) + offset
        except (TypeError, ValueError):
            node["play_at"] = offset
    if node_type == "ui_swatches":
        try:
            node["at"] = float(node.get("at", 0.0) or 0.0) + offset
        except (TypeError, ValueError):
            node["at"] = offset
    if node_type == "ui_input" and node.get("send_at") is not None:
        try:
            node["send_at"] = float(node["send_at"]) + offset
        except (TypeError, ValueError):
            node.pop("send_at", None)
    phases = node.get("phases")
    if node_type == "particle_field" and isinstance(phases, list):
        for phase in phases:
            if isinstance(phase, dict):
                try:
                    phase["at"] = float(phase.get("at", 0.0) or 0.0) + offset
                except (TypeError, ValueError):
                    phase["at"] = offset
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _offset_node_times(child, offset)


# Which content-time keys each primitive reads (runtime/primitives.js and
# runtime/domains/*.js), and so which must be written even when absent —
# an absent reveal_start means "at this scene's start", not "at t=0".
_CONTENT_TIME_TYPES = {
    "text": ("reveal_start",),
    "ui_input": ("reveal_start",),
    "domain_chart": ("reveal_start",), "chart": ("reveal_start",),
    "domain_diagram": ("reveal_start",), "diagram": ("reveal_start",), "workflow": ("reveal_start",),
    "shape_arrow": ("draw_start",), "arrow": ("draw_start",),
    "spline_tree": ("draw_start",),
    "particle_field": (),
}


def _continuity_manifest(scenes: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Index visual subjects that continue across scene boundaries.

    Node IDs remain unique for Studio edits; ``continuity_key`` pairs the
    scene-local instances that represent one subject over time.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}

    def visit(node: Dict[str, Any], scene_id: str) -> None:
        key = node.get("continuity_key")
        if isinstance(key, str) and key:
            groups.setdefault(key, []).append({
                "scene": scene_id,
                "node": str(node.get("id", "")),
                "position": list(node.get("position", [0, 0])),
                "scale": list(node.get("scale", [1, 1])),
            })
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                visit(child, scene_id)

    for scene in scenes:
        scene_id = str(scene.get("id", ""))
        for node in scene.get("nodes", []) or []:
            if isinstance(node, dict):
                visit(node, scene_id)
    return groups


_BACKDROP_KEYS = ("spotlight", "vignette_strength", "grain_opacity", "grid", "particles")


def _auto_look(spec: Dict[str, Any]) -> None:
    """A light background gets the flat "paper" look unless the author chose
    one or set any backdrop value themselves.

    The runtime's only look used to be a dark-tech one — a cyan spotlight
    fading to navy, a scrolling grid, cyan particles, a 0.58 vignette — and it
    was painted over every film whatever its background, so a light page came
    out a blue-grey wash (study of 11-12 Nov 2026). Nobody asks for a navy
    spotlight on white; a light background is the signal.
    """
    project = spec.get("project")
    if not isinstance(project, dict):
        return
    visual = spec.get("visual") if isinstance(spec.get("visual"), dict) else {}
    if project.get("look") or visual.get("look") or any(k in visual for k in _BACKDROP_KEYS):
        return
    bg = project.get("background")
    if not isinstance(bg, str):
        return
    from .inspect import _luma
    lum = _luma(bg)
    if lum is not None and lum >= 180:
        spec["project"] = dict(project, look="paper")


def resolve_motion_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Preprocesses and enriches a validated Motion Specification.
    - Resolves semantic targets in camera tracks
    - Computes bounding envelopes for auto-framing
    - Lays scenes out sequentially on one global timeline: each scene's
      `start` is recomputed as a running total of the scenes before it
      (schema.py defaults every scene's `start` to 0.0, which is only ever
      right for the first one), and every node's authored, scene-local
      animation times are shifted to match — the renderer has no per-scene
      clock of its own, it plays one continuous timeline and uses
      `start`/`duration` only to decide which scene's nodes are visible
      when.
    - Swaps every `image` node's `src: "asset:<name>"` for the real file,
      inlined as a data: URI (same reasoning as core.reel_web's
      _asset_uris(): the runtime has no base URL to resolve a file:// path
      or bare filename against, and inlining means a saved spec is the
      whole motion graphic — re-render it later with no AI or filesystem
      dependency).
    - Compiles the scenes' shots into one continuous camera curve (see
      core.motion.camera) when any scene names one.
    - Emits `_continuity`, a scene/node manifest grouped by each validated
      node's `continuity_key`, for Studio selection and follow-up edits.
    - Compiles continuity bridges (see core.motion.continuity) and records
      the result on `_continuity_compiled` (threads, bridges, errors,
      warnings).
    """
    # A copy, not the caller's dict. This function shifts every scene-local
    # time by its scene's start, IN PLACE, and a second pass over the same nodes
    # shifts them again: a caller that measured a film (rhythm.probe,
    # critic.settled_frames) and then rendered the same spec drew every scene
    # after the first late, and the result was blank frames (12 Nov 2026). The
    # `_scene_times_resolved` flag below protects a RESOLVED spec; this protects
    # the one the caller is still holding.
    import copy as _copy
    spec = _copy.deepcopy(spec)
    scenes = spec.get("scenes", [])
    camera = spec.get("camera", {})
    _auto_look(spec)

    assets_table = spec.get("_assets")
    if assets_table:
        from .. import reel_web as _web
        uris = _web._asset_uris(assets_table)
        if uris:
            for scene in scenes:
                for node in scene.get("nodes", []) or []:
                    if isinstance(node, dict):
                        _resolve_asset_srcs(node, uris)

    # Guarded the same way camera focus_target resolution already is below
    # (`if target and "position" not in track`) — a saved spec is meant to
    # re-render identically forever, including a second pass through THIS
    # function on an already-resolved spec (e.g. render() calling it fresh
    # from a .json file that build_spec() already resolved once). Without
    # the guard, node times shift by each scene's start a second time and
    # drift further every re-render.
    if not spec.get("_scene_times_resolved"):
        cursor = 0.0
        for scene in scenes:
            try:
                duration = float(scene.get("duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            scene["start"] = cursor
            # domain_ui_mockup was never a real primitive runtime.js knows
            # how to paint — expand it into shape_rect + text children here,
            # once, before node times get offset below (so its children's
            # own animation, if any, is offset along with everything else).
            nodes = scene.get("nodes", []) or []
            for i, node in enumerate(nodes):
                if isinstance(node, dict) and node.get("type") in ("domain_ui_mockup", "domain_ui", "ui_mockup"):
                    nodes[i] = _desugar_ui_mockup(node)
            for node in nodes:
                if isinstance(node, dict):
                    _offset_node_times(node, cursor)
            cursor += duration
        spec["_scene_times_resolved"] = True

    # Continuity compiler (core.motion.continuity): every subject that
    # carries a `continuity_key` across a cut gets a pose-matched bridge
    # written onto its two instances, and that cut becomes a "morph". Runs
    # once, after scene starts are final and before transitions are
    # stamped (it decides some of them). What could not be bridged is kept
    # on `_continuity_compiled["errors"]` for render.py to refuse.
    if not spec.get("_continuity_resolved"):
        spec["_continuity_compiled"] = _continuity.compile(spec)
        spec["_continuity_resolved"] = True

    # Cross-scene transitions: each scene (after the first) that names a
    # transition_in — or, absent one, gets a deterministic default so every
    # cut is a real transition rather than silently defaulting to a hard
    # one — gets a computed overlap window with the scene before it. This
    # does NOT move where a scene's own content is timed (still `scene.
    # start`, offset above); it only widens each scene's VISIBILITY window
    # so both scenes coexist during the cut, mirroring core.reel_web.py's
    # own _plan() (same overlap cap: a third of either neighbor's own
    # duration, so a short scene is never swallowed by its own transition).
    if not spec.get("_transitions_resolved"):
        for i in range(1, len(scenes)):
            this_scene, prev_scene = scenes[i], scenes[i - 1]
            try:
                prev_dur = float(prev_scene.get("duration", 0.0) or 0.0)
                this_dur = float(this_scene.get("duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            overlap = _continuity.handoff_overlap(prev_dur, this_dur)
            if overlap <= 0.02:
                continue  # a scene too short to safely overlap keeps a hard cut
            t_in = this_scene.get("transition_in")
            if not t_in:
                t_in = _TRANSITION_ROTATION[_hash_seed(this_scene.get("id", i)) % len(_TRANSITION_ROTATION)]
                this_scene["transition_in"] = t_in
            cut_at = this_scene["start"]
            this_scene["transitionInStart"] = round(cut_at - overlap, 3)
            prev_scene["transitionOutEnd"] = round(cut_at + overlap * 0.4, 3)
            this_scene["_transitionOverlap"] = round(overlap, 3)
        spec["_transitions_resolved"] = True

    node_registry: Dict[str, Dict[str, Any]] = {}

    def _centre(node: Dict[str, Any]) -> Tuple[float, float]:
        """Where a node visually IS, for camera targets: an arrow's
        midpoint, a tree's hub, otherwise its position. (The Alphakore run
        panned to the top-left corner for eleven seconds because a shot
        targeted an arrow, whose defaulted position is the origin.)"""
        pos = node.get("position", [0, 0])
        try:
            x, y = float(pos[0]), float(pos[1])
        except (TypeError, ValueError, IndexError):
            x, y = 0.0, 0.0
        frm, to = node.get("from"), node.get("to")
        if node.get("type") in ("shape_arrow", "arrow") and isinstance(frm, (list, tuple)) \
                and isinstance(to, (list, tuple)) and len(frm) == 2 and len(to) == 2:
            try:
                return (x + (float(frm[0]) + float(to[0])) / 2, y + (float(frm[1]) + float(to[1])) / 2)
            except (TypeError, ValueError):
                pass
        hub = node.get("hub")
        if node.get("type") == "spline_tree" and isinstance(hub, (list, tuple)) and len(hub) == 2:
            try:
                return (x + float(hub[0]), y + float(hub[1]))
            except (TypeError, ValueError):
                pass
        return (x, y)

    def _index(nodes: List[Dict[str, Any]], parent_pos: Tuple[float, float] = (0.0, 0.0)):
        for node in nodes:
            nid = node.get("id", "")
            pos = node.get("position", [0, 0])
            abs_x = parent_pos[0] + float(pos[0])
            abs_y = parent_pos[1] + float(pos[1])
            cx, cy = _centre(node)
            node_registry[nid] = {
                "node": node,
                "world_pos": (parent_pos[0] + cx, parent_pos[1] + cy),
                "targetable": not node.get("_no_position"),
            }
            if "children" in node and isinstance(node["children"], list):
                _index(node["children"], (abs_x, abs_y))

    for scene in scenes:
        _index(scene.get("nodes", []))

    # Expose the continuity graph to Studio and follow-up prompting. It is
    # metadata only; rendering still uses the validated node graph and tracks.
    spec["_continuity"] = _continuity_manifest(scenes)

    # Shots → one continuous camera curve (core.motion.camera). Only when
    # a scene names a shot; an explicitly authored camera is left alone.
    # The authored tracks are kept on `_authored_tracks` (their first entry
    # seeds the opening state) so the decision is visible in a saved spec.
    if not spec.get("_camera_resolved"):
        world = {nid: entry["world_pos"] for nid, entry in node_registry.items()
                 if entry.get("targetable", True)}
        authored = camera.get("tracks") if isinstance(camera, dict) else None
        width = int((spec.get("project") or {}).get("width", 1080) or 1080)
        height = int((spec.get("project") or {}).get("height", 1920) or 1920)
        compiled = _camera.compile_tracks(
            scenes, width, height, world,
            authored if isinstance(authored, list) else None,
            content_boxes=spec.get("_content_boxes"))
        if compiled is not None:
            if not isinstance(camera, dict):
                camera = {}
            if isinstance(authored, list) and authored:
                camera["_authored_tracks"] = authored
            camera["tracks"] = compiled
            spec["camera"] = camera
        spec["_camera_resolved"] = True

    if camera and "tracks" in camera and isinstance(camera["tracks"], list):
        for track in camera["tracks"]:
            target = track.get("focus_target")
            if target and "position" not in track:
                if target in node_registry:
                    wx, wy = node_registry[target]["world_pos"]
                    track["position"] = [round(wx, 2), round(wy, 2)]

    return spec
