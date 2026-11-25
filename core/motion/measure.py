"""
Prism Motion Graphics — measuring a scene in the real runtime
──────────────────────────────────────────────────────────────
core.motion.inspect estimates where a node will be from its spec: a text's
width as characters × font size × 0.55, nothing about the camera. That is
the right first check (no browser, instant) and the wrong last one: the
study of 11 Nov 2026 (finding M4) found a phone number the estimate put at
80..1119 px that Chromium laid out at −763..928, and a "push" shot that
zooms 1.3× moves every edge the estimate never looked at.

This lays the scene out in the runtime that will render it, seeks its
settled frame, and reads each text/image node's box in VIEWPORT
coordinates — the stage already carries the camera, so off-frame and
platform-band faults come out exact with no arithmetic of our own. It is
the same move core.reel_web.inspect makes for Studio.

inspect_all() is what the storyboard stage hands core.motion.generate
.build_spec as its `check`: the estimate's faults, with its off-frame and
safe-area guesses replaced by measured ones whenever a browser is here.
Without a browser it is the estimate alone, as before.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

from . import inspect as _inspect
from .camera import safe_area

# Fault wording is shared with inspect.py on purpose: generate._REPAIR_MOVES
# keys on "runs off the edge" and "platform's own UI" to tell the model what
# to change on the second round.
# Where in each scene the storyboard stage looks. Early enough to catch type
# that starts off, late enough to catch type that drifts, with the middle.
CHECK_MOMENTS = (0.35, 0.75, 0.95)

_REPLACES = ("runs off the edge", "platform's own UI", "bottom band")

_MEASURE_JS = """
(ids) => {
  const want = new Set(ids);
  const eff = (el) => {
    let o = 1;
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden') return 0;
      o *= parseFloat(cs.opacity || '1');
    }
    return o;
  };
  const out = {};
  for (const host of document.querySelectorAll('[data-motion-id]')) {
    const id = host.dataset.motionId;
    if (!want.has(id)) continue;
    const box = host.firstElementChild;
    if (!box) continue;
    const r = box.getBoundingClientRect();
    out[id] = {l: r.left, t: r.top, r: r.right, b: r.bottom, o: eff(box)};
  }
  return out;
}
"""


def _measurable(scenes: list[dict]) -> dict[str, str]:
    """{node id: type} for every text/image node, including the children
    of depth layers, panels and groups — the same walk inspect makes."""
    found: dict[str, str] = {}

    def visit(node: dict) -> None:
        kind = str(node.get("type", ""))
        if kind in ("text", "image") and node.get("id"):
            found[str(node["id"])] = kind
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                visit(child)

    for sc in scenes:
        for node in sc.get("nodes", []) or []:
            if isinstance(node, dict):
                visit(node)
    return found


def inspect_rendered(spec: dict[str, Any], at: float = 0.75,
                     moments: Any = None) -> list[str] | None:
    """Faults read off the browser's own layout at `at` of each scene's
    length — or, given `moments` (fractions of each scene), at all of them,
    reporting a fault only when it persists at two or more. One moment
    cannot see text that is still entering or that drifts out later (the
    model-written film of 12 Nov 2026 passed at 75 % and was cut off the
    rest of the time); two-of-three keeps a node that is merely sliding in
    from off-screen from being reported. None when there is no browser to ask
    — the caller keeps the estimate — and [] when everything measured sits
    inside the frame and the platform's safe area."""
    from .render import _playwright_available
    if not _playwright_available():
        return None
    from .resolver import resolve_motion_spec
    from .schema import validate_motion_spec
    resolved = resolve_motion_spec(validate_motion_spec(spec))
    project = resolved["project"]
    width, height = int(project["width"]), int(project["height"])
    fps = int(project.get("fps", 30) or 30)
    kinds = _measurable(resolved.get("scenes", []))
    if not kinds:
        return []
    zone = safe_area(width, height)

    from playwright.sync_api import sync_playwright
    from .. import browser as _browser
    index = pathlib.Path(os.path.dirname(__file__), "runtime", "index.html")
    times = tuple(moments) if moments else (at,)
    need = 1 if len(times) == 1 else 2
    seen: dict = {}
    with sync_playwright() as pw:
        chromium = _browser.launch_chromium(
            pw, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        try:
            page = chromium.new_page(viewport={"width": width, "height": height})
            page.goto(index.as_uri(), wait_until="domcontentloaded")
            page.wait_for_function("typeof window.__loadSpec === 'function'", timeout=15_000)
            page.evaluate("spec => window.__loadSpec(spec)", resolved)
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(() => true)", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            for si, sc in enumerate(resolved.get("scenes", [])):
                start = float(sc.get("start", 0.0) or 0.0)
                dur = float(sc.get("duration", 0.0) or 0.0)
                for frac in times:
                    moment = start + dur * frac
                    page.evaluate(f"window.__seek({int(round(moment * fps))})")
                    boxes = page.evaluate(_MEASURE_JS, list(kinds))
                    for nid, m in boxes.items():
                        if m["o"] < 0.05 or m["r"] - m["l"] <= 0:
                            continue  # not on screen at this moment
                        kind = kinds[nid]
                        where = (f'{m["l"]:.0f},{m["t"]:.0f} to {m["r"]:.0f},{m["b"]:.0f} '
                                 f"at {moment:.1f}s, measured in the browser with the camera applied")
                        if m["l"] < 0 or m["t"] < 0 or m["r"] > width or m["b"] > height:
                            cat = "edge"
                            msg = (f'node "{nid}" ({kind}) runs off the edge of the {width}x{height} '
                                   f"frame as drawn — its box is {where}; pull it inside, and "
                                   "check its anchor against its position")
                        elif zone.get("vertical") and m["t"] < zone["top"]:
                            cat = "top"
                            msg = (f'node "{nid}" ({kind}) sits inside the top {zone["top"]}px of the '
                                   f"frame where the platform's own UI covers it — its box is {where}; "
                                   f'keep text and logos below y={zone["top"]}')
                        elif zone.get("vertical") and m["b"] > zone["bottom"]:
                            cat = "bottom"
                            msg = (f'node "{nid}" ({kind}) reaches into the bottom band of the frame '
                                   f"where the platform's own UI (captions and buttons) covers it — "
                                   f'its box is {where}; keep text and logos above y={zone["bottom"]}')
                        else:
                            continue
                        key = (si, nid, cat)
                        hit = seen.setdefault(key, [0, msg])
                        hit[0] += 1
                        hit[1] = msg
        finally:
            chromium.close()
    return [msg for count, msg in seen.values() if count >= need]


def inspect_all(spec: dict[str, Any]) -> list[str]:
    """The estimate's faults with the geometry ones measured for real when
    a browser is here. Same signature as inspect.inspect, so build_spec's
    `check` takes it unchanged."""
    estimated = _inspect.inspect(spec)
    try:
        measured = inspect_rendered(spec, moments=CHECK_MOMENTS)
    except Exception:  # noqa: BLE001 — a browser hiccup must not fail the scene; keep the estimate
        measured = None
    if measured is None:
        return estimated
    kept = [f for f in estimated if not any(key in f for key in _REPLACES)]
    return kept + measured


# ── fitting text into the frame, deterministically ───────────────────────────
#
# The model-written film of 12 Nov 2026 shipped a headline 1,470 px wide in a
# 1,080 px frame and a 1,378 px phone number. The checker named both; the
# repair loop gave up after two rounds and kept the "cleaner" scene. Asking a
# model to do arithmetic it has already got wrong twice is the wrong tool —
# the engine knows exactly how wide each line is, and can make it fit.

_FIT_JS = """
(idsPerScene) => {
  const rt = window.__runtime, out = [];
  rt.sceneWindows.forEach((sw, i) => {
    sw.el.style.display = "";             // layout needs the scene shown
    const found = {};
    const want = new Set(idsPerScene[i] || []);
    for (const host of sw.el.children) {
      const id = host.dataset.motionId;
      if (!want.has(id)) continue;
      const box = host.firstElementChild;  // offsetWidth ignores transforms: the laid-out width
      found[id] = {w: box.offsetWidth, h: box.offsetHeight};
    }
    out.push(found);
  });
  return out;
}
"""


def _has_text(node: dict) -> bool:
    if node.get("type") == "text":
        return True
    return any(isinstance(c, dict) and _has_text(c) for c in node.get("children", []) or [])


def _layout_nodes(scene: dict) -> list[dict]:
    """The top-level nodes whose box is the copy a viewer reads: text, pills,
    prompt boxes, and any panel that holds text."""
    out = []
    for node in scene.get("nodes", []) or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        if node.get("type") in ("text", "ui_pill", "pill", "ui_input") or (
                node.get("type") in ("glass_panel", "glass", "group") and _has_text(node)):
            out.append(node)
    return out


def content_boxes(spec: dict[str, Any]) -> list | None:
    """Per scene, the box (left, top, right, bottom) its copy occupies in
    AUTHORED space: each node's authored position and anchor with the size
    the browser lays it out at (camera off, animation ignored). Animated
    rectangles are the wrong thing to measure — a node still sliding in sits
    outside the frame for a moment and is not where the scene settles. None
    entries for scenes with no copy; None overall without a browser. This is
    what core.motion.camera.keep_in_view keeps in frame."""
    import copy
    from .render import _playwright_available
    from .resolver import resolve_motion_spec
    from .schema import validate_motion_spec
    if not _playwright_available():
        return None
    bare = validate_motion_spec(copy.deepcopy(spec))
    bare.pop("camera", None)
    bare.pop("_content_boxes", None)
    for sc in bare["scenes"]:
        sc.pop("shot", None)
    resolved = resolve_motion_spec(bare)
    project = resolved["project"]
    width, height = int(project["width"]), int(project["height"])
    per_scene = [_layout_nodes(sc) for sc in bare["scenes"]]
    ids = [[str(n["id"]) for n in nodes] for nodes in per_scene]
    boxes: list = [None] * len(per_scene)
    if not any(ids):
        return boxes
    from playwright.sync_api import sync_playwright
    from .. import browser as _browser
    index = pathlib.Path(os.path.dirname(__file__), "runtime", "index.html")
    with sync_playwright() as pw:
        chromium = _browser.launch_chromium(
            pw, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        try:
            page = chromium.new_page(viewport={"width": width, "height": height})
            page.goto(index.as_uri(), wait_until="domcontentloaded")
            page.wait_for_function("typeof window.__loadSpec === 'function'", timeout=15_000)
            page.evaluate("s => window.__loadSpec(s)", resolved)
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(() => true)", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            sizes = page.evaluate(_FIT_JS, ids)
        finally:
            chromium.close()
    for si, nodes in enumerate(per_scene):
        l = t = float("inf")
        r = b = float("-inf")
        for node in nodes:
            m = (sizes[si] or {}).get(str(node["id"]))
            if not m or m["w"] <= 0:
                continue
            sx, sy = (node.get("scale") or [1, 1])[:2]
            w, h = m["w"] * float(sx or 1), m["h"] * float(sy or 1)
            ax, ay = (node.get("anchor") or [0.5, 0.5])[:2]
            x, y = (node.get("position") or [width / 2, height / 2])[:2]
            left, top = float(x) - float(ax) * w, float(y) - float(ay) * h
            l, t, r, b = min(l, left), min(t, top), max(r, left + w), max(b, top + h)
        if r > l and b > t:
            boxes[si] = [round(l, 1), round(t, 1), round(r, 1), round(b, 1)]
    return boxes


def fit_to_frame(spec: dict[str, Any], margin: float = 0.06, min_scale: float = 0.4,
                 rounds: int = 3) -> tuple[dict[str, Any], list[str]]:
    """Make every top-level text node fit inside the frame's side margins.

    Measures each text node's laid-out width in the real runtime with the
    camera switched OFF (a node wider than the frame under a deliberate 3x
    push is the author's choice; one wider than the frame at 1x is a
    mistake). A node wider than the space between the margins has its
    font_size scaled down to fit (never below `min_scale` of itself); a
    node whose box then still sits outside a margin is nudged inward.

    Returns (spec, changes) — the validated spec with the fixes applied,
    and one plain sentence per change. Without a browser, or when nothing
    needs fixing, the spec comes back unchanged with no changes. Children of
    depth layers and panels are not touched: their positions are relative to
    the parent and the parent's own box decides where they land.
    """
    import copy
    from .render import _playwright_available
    from .resolver import resolve_motion_spec
    from .schema import validate_motion_spec
    spec = validate_motion_spec(copy.deepcopy(spec))
    if not _playwright_available():
        return spec, []
    project = spec["project"]
    width, height = int(project["width"]), int(project["height"])
    lo, hi = width * margin, width * (1.0 - margin)
    changes: list[str] = []

    from playwright.sync_api import sync_playwright
    from .. import browser as _browser
    index = pathlib.Path(os.path.dirname(__file__), "runtime", "index.html")

    def ids_per_scene(s):
        return [[str(n["id"]) for n in sc.get("nodes", []) or []
                 if isinstance(n, dict) and n.get("type") == "text" and n.get("id")]
                for sc in s["scenes"]]

    for _ in range(max(1, rounds)):
        bare = copy.deepcopy(spec)
        bare.pop("camera", None)
        for sc in bare["scenes"]:
            sc.pop("shot", None)
        resolved = resolve_motion_spec(bare)
        ids = ids_per_scene(spec)
        if not any(ids):
            break
        with sync_playwright() as pw:
            chromium = _browser.launch_chromium(
                pw, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            try:
                page = chromium.new_page(viewport={"width": width, "height": height})
                page.goto(index.as_uri(), wait_until="domcontentloaded")
                page.wait_for_function("typeof window.__loadSpec === 'function'", timeout=15_000)
                page.evaluate("s => window.__loadSpec(s)", resolved)
                _browser.warm_fonts(page)
                try:
                    page.wait_for_function("document.fonts.ready.then(() => true)", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                measured = page.evaluate(_FIT_JS, ids)
            finally:
                chromium.close()

        changed = False
        for si, sc in enumerate(spec["scenes"]):
            for node in sc.get("nodes", []) or []:
                if not (isinstance(node, dict) and node.get("type") == "text"):
                    continue
                m = (measured[si] or {}).get(str(node.get("id")))
                if not m or m["w"] <= 0:
                    continue
                scale_x = float((node.get("scale") or [1, 1])[0] or 1)
                w = m["w"] * scale_x
                anchor_x = float((node.get("anchor") or [0.5, 0.5])[0])
                x = float((node.get("position") or [width / 2, 0])[0])
                nid = node.get("id")
                if w > (hi - lo) + 1:
                    old = float(node.get("font_size") or 48)
                    factor = max(min_scale, (hi - lo) / w * 0.985)
                    new = max(12, int(round(old * factor)))
                    if new < old:
                        node["font_size"] = new
                        w *= new / old
                        changes.append(f'"{nid}" was {m["w"] * scale_x:.0f}px wide in a {width}px '
                                       f"frame; font_size {old:g} → {new}")
                        changed = True
                left = x - anchor_x * w
                right = left + w
                if left < lo - 0.5:
                    x += lo - left
                elif right > hi + 0.5:
                    x -= right - hi
                if abs(x - float(node["position"][0])) > 0.5:
                    changes.append(f'"{nid}" moved from x={node["position"][0]:g} to x={x:.0f} to sit inside the margins')
                    node["position"] = [round(x, 1), node["position"][1]]
                    changed = True
        if not changed:
            break
    boxes = content_boxes(spec)
    if boxes is not None:
        spec["_content_boxes"] = boxes
    return spec, changes
