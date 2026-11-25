"""
Prism Motion Graphics — camera grammar and safe areas
──────────────────────────────────────────────────────
Two things the model used to have to get right by hand, now compiled:

1. **One global camera curve.** A scene names a *shot* — what the camera
   does for that beat (hold, reveal, push, pull, orbit, parallax, macro,
   resolve) and what it looks at — and `compile_tracks()` turns the
   sequence into a single `camera.tracks` list the runtime walks
   continuously. Each track only states where the camera ENDS, so every
   shot starts wherever the previous one left the camera; the model can
   no longer reset the camera to the same centre and zoom at every cut,
   because it never writes camera values per scene at all.

2. **Safe areas for 9:16.** Where platform chrome (username, caption,
   buttons) covers a vertical frame, and the title/action zones that stay
   clear. `inspect.py` checks text and images against them.

Stdlib only, no browser: the runtime's Camera.evaluate() is a plain
piecewise walk over these tracks, so what is computed here is what plays.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The shot vocabulary. Keep this the single source: schema.py validates
# against it, generate.py's catalogue lists it, and compile_tracks() knows
# what each one does to the camera.
SHOT_INTENTS = (
    "hold", "reveal", "push", "pull", "orbit", "parallax", "macro", "resolve",
)

# Zoom factors a shot applies relative to where the camera IS (push/pull/
# orbit/parallax) or absolute (reveal/macro/resolve). Modest on purpose:
# a 9:16 frame is small, and a 1.15x push already reads as "moving in".
_PUSH_FACTOR = 1.15
_PULL_FACTOR = 1.0 / 1.15
_MACRO_ZOOM = 1.6
_ORBIT_TILT_DEG = 3.0
_PARALLAX_DRIFT_PX = 60.0
# 12, not 3: the reference film of 11 Nov 2026 pushes about 8x into one word,
# and text, shapes and UI are vector — they stay crisp. A raster `image` does
# not, so an author zooming past ~3x onto a photo should aim at its subject.
ZOOM_MIN, ZOOM_MAX = 0.5, 12.0


def safe_area(width: int, height: int) -> Dict[str, Any]:
    """The rectangle content may occupy and the zones inside it.

    Vertical (height > width): the top band hides under status/UI chrome
    and the bottom band under captions, buttons and the progress bar —
    proportions match the common short-video overlays. Landscape and
    square frames only reserve an ordinary margin.
    """
    w, h = float(width), float(height)
    if h > w:
        top, bottom, side = 0.11 * h, 0.80 * h, 0.06 * w
        return {
            "vertical": True,
            "left": round(side), "top": round(top),
            "right": round(w - side), "bottom": round(bottom),
            "title": {"top": round(top), "bottom": round(0.42 * h)},
            "action": {"top": round(0.42 * h), "bottom": round(bottom)},
        }
    margin_x, margin_y = 0.05 * w, 0.06 * h
    return {
        "vertical": False,
        "left": round(margin_x), "top": round(margin_y),
        "right": round(w - margin_x), "bottom": round(h - margin_y),
        "title": {"top": round(margin_y), "bottom": round(0.45 * h)},
        "action": {"top": round(0.45 * h), "bottom": round(h - margin_y)},
    }


def _num(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def normalize_shot(shot: Any) -> Optional[Dict[str, Any]]:
    """A scene's authored shot, coerced to {intent, target?, zoom?, easing?}
    or None when unusable. A bare string is the intent; a target is a node
    id or an [x, y]; zoom is clamped like camera tracks are."""
    if isinstance(shot, str):
        shot = {"intent": shot}
    if not isinstance(shot, dict):
        return None
    intent = str(shot.get("intent", "")).strip().lower()
    if intent not in SHOT_INTENTS:
        return None
    out: Dict[str, Any] = {"intent": intent}
    target = shot.get("target")
    if isinstance(target, str) and target.strip():
        out["target"] = target.strip()
    elif isinstance(target, (list, tuple)) and len(target) == 2:
        out["target"] = [_num(target[0], 0.0), _num(target[1], 0.0)]
    if shot.get("zoom") is not None:
        out["zoom"] = max(ZOOM_MIN, min(ZOOM_MAX, _num(shot["zoom"], 1.0)))
    if shot.get("duration") is not None:
        # how long the move takes, in seconds; clamped to the scene later.
        # Without it a move spans the scene, which is right for a drift and
        # wrong for a camera that must settle before the copy arrives. A
        # value that is not a number is dropped, not defaulted.
        dur = _num(shot["duration"], float("nan"))
        if dur == dur:
            out["duration"] = max(0.05, dur)
    if shot.get("delay") is not None:
        # seconds into the scene before the move starts; the camera holds
        # the previous shot's end state until then. The reference's
        # channel scene holds on the icons, then pans down to the hub.
        delay = _num(shot["delay"], float("nan"))
        if delay == delay:
            out["delay"] = max(0.0, delay)
    easing = shot.get("easing")
    if isinstance(easing, str) and easing.strip():
        out["easing"] = easing.strip()
    return out


def node_centre(node: Dict[str, Any]) -> Tuple[float, float]:
    """Where a node visually IS, in its parent's space, for camera targets
    and camera-view checks: an arrow's midpoint, a tree's hub, otherwise
    its position. (The Alphakore run of 10 Sep 2026 panned to the
    top-left corner for eleven seconds because a shot targeted an arrow,
    whose defaulted position is the origin.)"""
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


def _resolve_target(target: Any, world_pos: Dict[str, Tuple[float, float]],
                    fallback: Tuple[float, float]) -> Tuple[float, float]:
    if isinstance(target, str):
        hit = world_pos.get(target)
        return (float(hit[0]), float(hit[1])) if hit else fallback
    if isinstance(target, (list, tuple)) and len(target) == 2:
        return (_num(target[0], fallback[0]), _num(target[1], fallback[1]))
    return fallback


def keep_in_view(pos: Tuple[float, float], zoom: float, box: Sequence[float],
                 width: int, height: int, margin: Optional[float] = None
                 ) -> Tuple[Tuple[float, float], float]:
    """The camera state nearest the wanted one from which `box` (left, top,
    right, bottom, in authored space) is entirely visible INSIDE the safe
    area (see safe_area(): a vertical frame keeps its top 11 % and bottom
    20 % clear for platform chrome). `margin`, when given, replaces the safe
    area with a plain margin (a fraction of each dimension) on every side.

    Zoom is capped at the most the box allows (never below 1.0 — a box wider
    than the frame at 1x is the text-fit pass's business, not a reason to
    pull out); the centre is clamped into the range that keeps the box in
    view, or put on the box's own centre when no such range exists."""
    l, t, r, b = (float(v) for v in box)
    bw, bh = max(0.0, r - l), max(0.0, b - t)
    if bw <= 0 or bh <= 0:
        return pos, zoom
    if margin is not None:
        ml = mr = width * margin
        mt = mb = height * margin
    else:
        zone = safe_area(width, height)
        ml, mr = float(zone["left"]), float(width - zone["right"])
        mt, mb = float(zone["top"]), float(height - zone["bottom"])
    zmax = max(1.0, min(width / (bw + ml + mr), height / (bh + mt + mb)))
    z = min(zoom, zmax)
    hw, hh = width / (2.0 * z), height / (2.0 * z)

    def axis(c, lo_edge, hi_edge, half, m_lo, m_hi):
        # the view spans [c-half, c+half]; it must contain [lo_edge-m_lo, hi_edge+m_hi]
        low, high = hi_edge + m_hi - half, lo_edge - m_lo + half
        if low > high:
            return (lo_edge - m_lo + hi_edge + m_hi) / 2.0
        return min(max(c, low), high)

    return (axis(pos[0], l, r, hw, ml, mr), axis(pos[1], t, b, hh, mt, mb)), z


def compile_tracks(scenes: Sequence[Dict[str, Any]], width: int, height: int,
                   world_pos: Dict[str, Tuple[float, float]],
                   authored: Optional[List[Dict[str, Any]]] = None,
                   content_boxes: Optional[Sequence[Optional[Sequence[float]]]] = None
                   ) -> Optional[List[Dict[str, Any]]]:
    """One continuous `camera.tracks` list from the scenes' shots.

    Returns None when no scene names a shot, so an explicitly authored
    camera keeps working exactly as before. `world_pos` maps node ids to
    absolute positions (resolver.py's registry) for node targets.

    The first track is a zero-length set at t=0 — the camera's opening
    state, taken from the first authored track if there was one — and
    every later track states only its destination, at the scene's own
    start, over most of the scene, so the walk from one to the next is
    continuous by construction. A `hold` writes a track too (its
    destination is where the camera already is), so a later seek never
    finds a gap it has to guess across.
    """
    if not any(isinstance(s.get("shot"), dict) for s in scenes):
        return None
    centre = (width / 2.0, height / 2.0)
    pos = centre
    zoom, rot = 1.0, 0.0
    if authored:
        first = authored[0]
        if isinstance(first, dict):
            if isinstance(first.get("position"), (list, tuple)) and len(first["position"]) == 2:
                pos = (_num(first["position"][0], pos[0]), _num(first["position"][1], pos[1]))
            zoom = max(ZOOM_MIN, min(ZOOM_MAX, _num(first.get("zoom"), 1.0)))
            rot = _num(first.get("rotation"), 0.0)
    tracks: List[Dict[str, Any]] = [{
        "time": 0.0, "duration": 0.0,
        "position": [round(pos[0], 2), round(pos[1], 2)],
        "zoom": round(zoom, 4), "rotation": round(rot, 3), "_shot": "open",
    }]
    orbit_sign = 1.0
    for scene_index, scene in enumerate(scenes):
        shot = scene.get("shot")
        if not isinstance(shot, dict):
            # A scene that names no shot must not inherit a corner: the
            # second Alphakore run pushed onto a tree at x=790 in scene 2,
            # then wrote no shot for scenes 3-7, and every caption after
            # 3.5 s was clipped at the left edge. Missing means "settle":
            # ease back to the centre at 1.0 over the scene.
            start = _num(scene.get("start"), 0.0)
            seconds = max(0.0, _num(scene.get("duration"), 0.0))
            if abs(pos[0] - centre[0]) < 1e-6 and abs(pos[1] - centre[1]) < 1e-6 \
                    and abs(zoom - 1.0) < 1e-6 and abs(rot) < 1e-6:
                continue
            pos, zoom, rot = centre, 1.0, 0.0
            tracks.append({
                "time": round(start, 3), "duration": round(min(seconds, max(0.4, seconds * 0.7)), 3),
                "position": [round(pos[0], 2), round(pos[1], 2)],
                "zoom": 1.0, "rotation": 0.0, "easing": "power2.inOut", "_shot": "settle",
            })
            continue
        start = _num(scene.get("start"), 0.0)
        seconds = max(0.0, _num(scene.get("duration"), 0.0))
        intent = shot.get("intent", "hold")
        target = _resolve_target(shot.get("target"), world_pos, pos)
        easing = shot.get("easing")
        duration = seconds
        if intent == "hold":
            new_pos, new_zoom, new_rot = pos, zoom, rot
            easing = easing or "none"
        elif intent == "reveal":
            new_pos = target
            new_zoom = _num(shot.get("zoom"), 1.0)
            new_rot = rot
            duration = seconds * 0.9
            easing = easing or "power3.out"
        elif intent == "push":
            new_pos = target
            new_zoom = zoom * _num(shot.get("zoom"), _PUSH_FACTOR) if "zoom" not in shot else _num(shot["zoom"], zoom)
            new_rot = rot
            easing = easing or "power2.inOut"
        elif intent == "pull":
            new_pos = target
            new_zoom = zoom * _PULL_FACTOR if "zoom" not in shot else _num(shot["zoom"], zoom)
            new_rot = rot
            easing = easing or "power2.inOut"
        elif intent == "orbit":
            new_pos = target
            new_zoom = _num(shot.get("zoom"), zoom)
            new_rot = rot + orbit_sign * _ORBIT_TILT_DEG
            orbit_sign = -orbit_sign
            easing = easing or "sine.inOut"
        elif intent == "parallax":
            if "target" in shot:
                new_pos = target
            else:
                new_pos = (pos[0] + _PARALLAX_DRIFT_PX * orbit_sign, pos[1])
                orbit_sign = -orbit_sign
            new_zoom = _num(shot.get("zoom"), zoom)
            new_rot = rot
            easing = easing or "none"
        elif intent == "macro":
            new_pos = target
            new_zoom = _num(shot.get("zoom"), _MACRO_ZOOM)
            new_rot = rot
            duration = seconds * 0.8
            easing = easing or "power2.inOut"
        else:  # resolve
            new_pos = target if "target" in shot else centre
            new_zoom = _num(shot.get("zoom"), 1.0)
            new_rot = 0.0
            duration = seconds * 0.85
            easing = easing or "power2.inOut"
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, new_zoom))
        # The scene's copy must still be in frame where the camera settles.
        # Shots compound — each `push` multiplies the zoom the last one left —
        # and a `hold` inherits it, so a model-written film ended on a 1.75x
        # camera filming a last scene it had composed for 1x: the name was
        # off the top and the phone number off the left (12 Nov 2026).
        # `crop: true` on the shot means the close-up is deliberate.
        if (content_boxes is not None and scene_index < len(content_boxes)
                and content_boxes[scene_index] and not shot.get("crop")):
            new_pos, new_zoom = keep_in_view(new_pos, new_zoom, content_boxes[scene_index],
                                             width, height)
        delay = 0.0
        if shot.get("delay") is not None:
            delay = min(max(0.0, seconds - 0.05), _num(shot["delay"], 0.0))
        if shot.get("duration") is not None:
            duration = min(seconds, _num(shot["duration"], duration))
        duration = min(duration, max(0.0, seconds - delay))
        tracks.append({
            "time": round(start + delay, 3), "duration": round(max(0.0, duration), 3),
            "position": [round(new_pos[0], 2), round(new_pos[1], 2)],
            "zoom": round(new_zoom, 4), "rotation": round(new_rot, 3),
            "easing": easing, "_shot": intent,
        })
        pos, zoom, rot = new_pos, new_zoom, new_rot
    return tracks


def state_at(tracks: Sequence[Dict[str, Any]], time: float, width: int, height: int
             ) -> Dict[str, float]:
    """The camera's position/zoom at `time`, walking the tracks the way
    runtime.js's Camera.evaluate() does (linear in this Python mirror —
    the easing only changes WHEN a value is reached, not where it ends,
    and the review points are read at settled times)."""
    x, y, zoom = width / 2.0, height / 2.0, 1.0
    for tr in sorted(tracks or [], key=lambda t: _num(t.get("time"), 0.0)):
        start = _num(tr.get("time"), 0.0)
        dur = _num(tr.get("duration"), 0.0)
        pos = tr.get("position") if isinstance(tr.get("position"), (list, tuple)) else None
        tz = _num(tr.get("zoom"), zoom) if tr.get("zoom") is not None else zoom
        if time >= start + dur:
            if pos:
                x, y = _num(pos[0], x), _num(pos[1], y)
            zoom = tz
        elif time > start:
            p = (time - start) / max(0.001, dur)
            if pos:
                x, y = x + (_num(pos[0], x) - x) * p, y + (_num(pos[1], y) - y) * p
            zoom = zoom + (tz - zoom) * p
            break
        else:
            break
    return {"x": x, "y": y, "zoom": zoom}


def visible_rect(state: Dict[str, float], width: int, height: int) -> Tuple[float, float, float, float]:
    """(left, top, right, bottom) of the world the camera shows."""
    half_w, half_h = width / (2.0 * state["zoom"]), height / (2.0 * state["zoom"])
    return (state["x"] - half_w, state["y"] - half_h, state["x"] + half_w, state["y"] + half_h)


def is_continuous(tracks: Sequence[Dict[str, Any]]) -> bool:
    """True when no track starts before the previous one has finished —
    the property that makes Camera.evaluate()'s walk continuous."""
    end = -1.0
    for tr in sorted(tracks, key=lambda t: _num(t.get("time"), 0.0)):
        start = _num(tr.get("time"), 0.0)
        if start < end - 1e-6:
            return False
        end = start + _num(tr.get("duration"), 0.0)
    return True
