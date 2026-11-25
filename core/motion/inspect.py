"""
Prism Motion Graphics — per-scene verification
────────────────────────────────────────────────
Checks a SPECIFIC claim a scene's own spec makes against its resolved
geometry — not a generic style rule. This is the MoVer lesson, not the
motion_faults() one: core.reel_web's motion_faults() (reverted — see
motion-plan.md) checked "does one animation dominate" and got gamed by
scattering weak animations to dodge the number. Nothing here checks
whether a scene is *good*; each check answers a factual yes/no that a
model can't cheaply fake — a node is either inside the frame or it isn't,
an entrance either moves the value or it doesn't.

Pure Python, no browser. core.reel_web.inspect() has to launch Chromium
because its ground truth (real HTML/CSS layout) only exists once the page
renders. Motion's ground truth — position, size, animation timing — is
already sitting in the spec before a single frame is drawn, which is
exactly the advantage a from-scratch scene graph has over filming a page.
"""
from __future__ import annotations

from typing import Any

from .camera import safe_area


def _node_bounds(node: dict, canvas_w: int, canvas_h: int) -> tuple[float, float, float, float] | None:
    """Approximate (left, top, right, bottom) for one node's AUTHORED
    (pre-animation, settled) position — good enough to catch "this is
    nowhere near the frame", not meant to be pixel-exact. Returns None for
    a node with no usable position.
    """
    pos = node.get("position")
    if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
        return None
    try:
        x, y = float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return None
    anchor = node.get("anchor") or [0.5, 0.5]
    try:
        ax, ay = float(anchor[0]), float(anchor[1])
    except (TypeError, ValueError, IndexError):
        ax, ay = 0.5, 0.5

    node_type = node.get("type", "")
    fill = node.get("fill")
    if node.get("layer") == "background" or (
            node_type in ("shape_circle", "circle") and (fill is None or str(fill).strip().lower() == "none")):
        # A background wash or sky is meant to bleed past the frame, and a
        # stroke-only ring is a decoration that does the same (the
        # reference's great arc) — neither has a box worth clipping.
        return None
    if node_type in ("light_field", "depth_layer", "particle_field", "spline_tree"):
        # A light field is meant to bleed past the frame; a depth layer is
        # a positioning group with no paint of its own. Neither has a box
        # worth checking — their children are checked on their own.
        return None
    if node_type == "text":
        content = str(node.get("content", ""))
        font_size = float(node.get("font_size", 48) or 48)
        # A multi-line headline ("\n" — every real spec uses it, see
        # generate.py's catalogue) is genuinely taller and narrower per
        # line than the whole string treated as one line — measuring it as
        # one line UNDERSTATES its real height (a 4-line headline's true
        # extent is ~4x taller than this used to compute), which is
        # exactly backwards for a check meant to catch real collisions.
        lines = content.split("\n") or [content]
        line_height = float(node.get("line_height", 1.25) or 1.25)
        w = max(font_size, max((len(ln) for ln in lines), default=0) * font_size * 0.55)
        h = max(font_size * 1.3, len(lines) * font_size * line_height)
    elif node_type == "ui_pill":
        fs = float(node.get("font_size", 28) or 28)
        lead = fs * 0.85 if (node.get("check") or node.get("dot")) else 0.0
        w = len(str(node.get("label", ""))) * fs * 0.56 + fs * 1.9 + lead
        h = fs * 2.0
    elif node_type == "ui_checklist":
        fs = float(node.get("font_size", 34) or 34)
        w = float(node.get("width", 760) or 760)
        rows = max(1, len(node.get("items") or []))
        gap = float(node.get("row_gap", fs * 0.9) or fs * 0.9)
        h = rows * (fs * 1.2 + fs * 0.74 + fs * 0.3) + (rows - 1) * gap
    elif node_type == "ui_window":
        w = float(node.get("width", 900) or 900)
        h = float(node.get("height", 620) or 620)
    elif node_type == "ui_swatches":
        d = float(node.get("dot_size", 30) or 30)
        groups = node.get("groups") or []
        w = sum(len(g) * d + max(0, len(g) - 1) * d * 0.3 + d * 1.2 for g in groups if isinstance(g, list)) + max(0, len(groups) - 1) * d * 0.7
        h = d * 1.9
    elif node_type == "cursor":
        size = float(node.get("size", 44) or 44)
        label = str(node.get("label", ""))
        # The tip is the position; the pill hangs to its lower right.
        left, top = x, y
        return (left, top, left + size * 0.55 + len(label) * size * 0.2 + size * 0.6,
                top + size * (1.0 if not label else 1.15))
    elif node_type == "shape_arrow":
        frm = node.get("from") or [x, y]
        to = node.get("to") or [x, y]
        try:
            xs = [float(frm[0]), float(to[0])]
            ys = [float(frm[1]), float(to[1])]
        except (TypeError, ValueError, IndexError):
            xs, ys = [x], [y]
        return (min(xs), min(ys), max(xs), max(ys))
    else:
        # shape_rect / domain_chart / domain_ui_mockup / domain_diagram all
        # carry explicit width/height in the spec (defaults match
        # runtime/primitives.js's own JS defaults, so a check here means
        # the same thing the renderer would actually draw).
        dw, dh = (640, 400) if node_type == "glass_panel" else (200, 200)
        if node_type == "icon":
            dw = dh = float(node.get("size", 96) or 96)
        elif node_type == "orb":
            dw = dh = 2 * float(node.get("radius", 120) or 120)
        elif node_type in ("shape_circle", "circle"):
            dw = dh = 2 * float(node.get("radius", 50) or 50)
        w = float(node.get("width", dw) or dw)
        h = float(node.get("height", dh) or dh)

    left = x - w * ax
    top = y - h * ay
    return (left, top, left + w, top + h)


def _overlap_faults(scene: dict, canvas_w: int, canvas_h: int) -> list[str]:
    """Do any two text/image nodes' AUTHORED (settled) bounds collide by
    more than a third of the smaller one's area?

    Scoped to text-vs-image and image-vs-image on purpose — shape_rect is
    routinely and correctly used as an intentional backdrop/badge/panel
    directly BEHIND other content (a highlight pill behind a label, a
    glass card behind a photo), so including it would flag legitimate
    layering as a fault. Text piled on top of a photo, or two photos
    sharing the same region, has no such legitimate reading — that combo
    only happens when nothing accounted for what else was already there.
    A factual, checkable geometry question (do these boxes intersect),
    not a taste judgment — same category as the existing off-frame check,
    not the reverted diversity-dominance one.
    """
    nodes = [n for n in (scene.get("nodes") or [])
             if isinstance(n, dict) and n.get("type") in ("text", "image")]
    boxes = []
    for n in nodes:
        b = _node_bounds(n, canvas_w, canvas_h)
        if b:
            boxes.append((n.get("id", "?"), n.get("type"), b))

    faults: list[str] = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            id_a, type_a, (l1, t1, r1, b1) = boxes[i]
            id_b, type_b, (l2, t2, r2, b2) = boxes[j]
            ix = max(0.0, min(r1, r2) - max(l1, l2))
            iy = max(0.0, min(b1, b2) - max(t1, t2))
            if ix <= 0 or iy <= 0:
                continue
            inter = ix * iy
            area_a = max(1.0, (r1 - l1) * (b1 - t1))
            area_b = max(1.0, (r2 - l2) * (b2 - t2))
            smaller = min(area_a, area_b)
            if inter / smaller > 0.33:
                faults.append(
                    f'node "{id_a}" ({type_a}) and node "{id_b}" ({type_b}) '
                    f"overlap by {int(inter / smaller * 100)}% of the smaller "
                    "one's area — move or resize one of them so they don't "
                    "share the same region")
    return faults


def _entrance_faults(node: dict, scene_duration: float) -> list[str]:
    """Does this node's declared enter/exit actually do what it claims?
    Every check here is about the DECLARATION being internally consistent
    with itself and the scene's own duration — not a taste judgment.
    """
    faults: list[str] = []
    nid = node.get("id", "?")
    anim = node.get("animation")
    if not isinstance(anim, dict):
        return faults

    enter = anim.get("enter")
    if isinstance(enter, dict):
        try:
            e_time = float(enter.get("time", 0.0) or 0.0)
            e_dur = float(enter.get("duration", 0.6) or 0.6)
        except (TypeError, ValueError):
            e_time, e_dur = 0.0, 0.6
        if e_time >= scene_duration:
            faults.append(f'node "{nid}" enters at {e_time:g}s but the '
                          f"scene is only {scene_duration:g}s — it never appears")
        elif e_dur <= 0.05:
            faults.append(f'node "{nid}" enters over {e_dur:g}s — that is '
                          "instant, not an entrance; give it real duration")
        # Per-tween checks — the closed "type" enum (slide_up/fade_in/...)
        # this used to key off is gone; the same two facts are checked
        # against whichever channels the enter actually declares, since a
        # blur-focus or clip reveal can have exactly the same "technically
        # present but too small to read" and "fades to nothing" failure
        # modes a slide/fade could.
        for tw in (enter.get("tweens") or []):
            if not isinstance(tw, dict):
                continue
            channel = tw.get("channel")
            try:
                frm = float(tw.get("from", 0.0))
                to = float(tw.get("to", 0.0))
            except (TypeError, ValueError):
                continue
            if channel in ("x", "y") and abs(to - frm) < 5:
                faults.append(f'node "{nid}"\'s enter {channel} tween moves '
                              f"only {abs(to - frm):g}px — too small to read as motion")
            if channel == "opacity" and to <= 0.05:
                faults.append(f'node "{nid}" enters to opacity {to:g} — '
                              "effectively invisible")

    exit_ = anim.get("exit")
    if isinstance(exit_, dict) and "time" in exit_:
        try:
            x_time = float(exit_["time"])
            x_dur = float(exit_.get("duration", 0.6) or 0.6)
        except (TypeError, ValueError):
            x_time, x_dur = None, 0.6
        if x_time is not None:
            if x_time >= scene_duration:
                faults.append(f'node "{nid}" exits at {x_time:g}s but the '
                              f"scene ends at {scene_duration:g}s — the exit "
                              "never plays")
            elif x_time + x_dur > scene_duration + 0.05:
                faults.append(f'node "{nid}"\'s exit runs past the scene\'s '
                              f"own end ({x_time + x_dur:g}s vs "
                              f"{scene_duration:g}s) — it will be cut off "
                              "mid-motion")
    return faults


def _luma(colour: str):
    """Approximate luminance 0-255 of a #hex or rgb()/rgba() colour, or None."""
    import re
    c = colour.strip().lower()
    m = re.match(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", c)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", c)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    return None


def _material_faults(scene: dict, canvas_w: int, canvas_h: int) -> list[str]:
    """Under the layer doctrine only: a flat-coloured shape_rect wider AND
    taller than half the frame is a wash or a card built from the wrong
    primitive — the catalogue's own words — so it is named, with the
    primitive that should carry it. A gradient fill (a sky) and a glass
    rect are not flagged; neither are pills, bubbles or hairlines, which
    never reach half the frame both ways. A checkable fact about one node,
    not a taste judgement about the composition.
    """
    nodes = [n for n in scene.get("nodes", []) or [] if isinstance(n, dict)]
    if not any(n.get("layer") for n in nodes):
        return []
    faults: list[str] = []

    def visit(node: dict) -> None:
        if node.get("type") == "particle_field":
            try:
                size = float(node.get("size", 14) or 14)
            except (TypeError, ValueError):
                size = 14.0
            if size < 10:
                faults.append(
                    f'node "{node.get("id", "?")}" is a particle_field with "size" {size:g} — '
                    "dust, not tiles; the reference's tiles are 28-40 px, use 24 or more")
        if node.get("type") == "light_field":
            colour = str(node.get("color") or "")
            if _luma(colour) is not None and _luma(colour) < 60:
                faults.append(
                    f'node "{node.get("id", "?")}" is a light_field whose "color" {colour} is '
                    "nearly black — a light must be a bright colour (it is screen-blended "
                    "over the background), e.g. rgba(124,156,255,0.6)")
        if node.get("type") == "glass_panel" and not (node.get("children") or []):
            faults.append(
                f'node "{node.get("id", "?")}" is an empty glass_panel — a placeholder '
                "frame with nothing in it; put the beat's copy inside it as children, "
                "or remove it")
        if node.get("type") == "shape_rect" and not node.get("is_glass"):
            fill = str(node.get("fill") or "")
            try:
                w = float(node.get("width", 200) or 200)
                h = float(node.get("height", 100) or 100)
            except (TypeError, ValueError):
                w, h = 200.0, 100.0
            if "gradient(" not in fill and w >= canvas_w * 0.5 and h >= canvas_h * 0.5:
                what = ("background" if node.get("layer") == "background"
                        else "card")
                faults.append(
                    f'node "{node.get("id", "?")}" is a flat shape_rect '
                    f"{w:.0f}x{h:.0f} covering most of the {canvas_w}x{canvas_h} "
                    f"frame — the wrong primitive for a {what}: use a "
                    + ("light_field, or give the rect a gradient fill"
                       if what == "background" else "glass_panel"))
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                visit(child)

    for node in nodes:
        visit(node)
    return faults


def _layer_faults(scene: dict) -> list[str]:
    """Only fires for scenes actually using the "layer" doctrine (the
    brand_launch skeleton in generate.py) — a plain freeform scene has no
    node with a "layer" key and this returns nothing, exactly as before.
    Checks the two REQUIRED facts that doctrine promises, not taste: a
    background layer must have ongoing motion (else the scene is a held
    slide with a moving foreground on top, not a shot), and a foreground
    layer must actually enter AND exit (else it's not the scene's subject,
    it's decoration).
    """
    nodes = [n for n in scene.get("nodes", []) or [] if isinstance(n, dict)]
    if not any(n.get("layer") for n in nodes):
        return []
    faults: list[str] = []

    backgrounds = [n for n in nodes if n.get("layer") == "background"]
    if backgrounds and not any(
            isinstance(n.get("animation"), dict)
            and isinstance(n["animation"].get("secondary_motion"), dict)
            for n in backgrounds):
        faults.append(
            'the "background" layer node has no "secondary_motion" — it '
            "will sit still once it's in, which is what makes a scene "
            "read as a held slide instead of a shot")

    # A foreground node either LEAVES (a real exit) or keeps some life in
    # it while it holds (secondary_motion) — both read as a directed shot.
    # Only "enters once, then is completely inert for the rest of the
    # scene" is the actual fault: that's the held-slide problem. A closing
    # scene intentionally settling on a logo is the former case, not this.
    def _lives_or_leaves(n: dict) -> bool:
        anim = n.get("animation")
        if not isinstance(anim, dict) or not isinstance(anim.get("enter"), dict):
            return False
        return isinstance(anim.get("exit"), dict) or isinstance(
            anim.get("secondary_motion"), dict)

    foregrounds = [n for n in nodes if n.get("layer") == "foreground"]
    if foregrounds and not any(_lives_or_leaves(n) for n in foregrounds):
        faults.append(
            'no "foreground" layer node has a real "enter" paired with '
            'either an "exit" or "secondary_motion" — it appears once '
            "and then sits completely still until the cut")
    return faults


def _safe_area_faults(scene: dict, canvas_w: int, canvas_h: int) -> list[str]:
    """Text and images that sit where a vertical platform's own chrome
    (username, caption, buttons, progress bar) covers the frame. A
    factual geometry check against core.motion.camera.safe_area(); a
    landscape or square project has no such bands and returns nothing.
    Only top-level nodes and the children of a depth_layer/glass_panel
    are walked — that is where headlines and logos live.
    """
    zone = safe_area(canvas_w, canvas_h)
    if not zone.get("vertical"):
        return []
    faults: list[str] = []

    def visit(node: dict, offset: tuple[float, float]) -> None:
        if node.get("type") in ("text", "image"):
            shifted = dict(node)
            pos = node.get("position") or [0, 0]
            try:
                shifted["position"] = [float(pos[0]) + offset[0], float(pos[1]) + offset[1]]
            except (TypeError, ValueError, IndexError):
                return
            b = _node_bounds(shifted, canvas_w, canvas_h)
            if not b:
                return
            _, top, _, bottom = b
            if bottom <= 0 or top >= canvas_h:
                return  # entirely outside: the off-frame check owns that
            if top < zone["top"]:
                faults.append(
                    f'node "{node.get("id", "?")}" ({node.get("type")}) reaches '
                    f"y={top:.0f}, inside the top {zone['top']}px of a "
                    f"{canvas_w}x{canvas_h} frame where the platform's own UI "
                    f"covers it — keep text and logos below y={zone['top']}")
            elif bottom > zone["bottom"]:
                faults.append(
                    f'node "{node.get("id", "?")}" ({node.get("type")}) reaches '
                    f"y={bottom:.0f}, inside the bottom band of a "
                    f"{canvas_w}x{canvas_h} frame where captions and buttons "
                    f"cover it — keep text and logos above y={zone['bottom']}")
        if node.get("type") in ("depth_layer", "glass_panel", "group"):
            pos = node.get("position") or [0, 0]
            try:
                inner = (offset[0] + float(pos[0]), offset[1] + float(pos[1]))
            except (TypeError, ValueError, IndexError):
                return
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    visit(child, inner)

    for node in scene.get("nodes", []) or []:
        if isinstance(node, dict):
            visit(node, (0.0, 0.0))
    return faults


def inspect(spec: dict[str, Any]) -> list[str]:
    """Check one scene (spec["scenes"] has exactly one, the shape
    core.motion.generate.build_spec() checks with) and report concrete,
    checkable faults. Empty list means nothing objective was found wrong
    — not a claim that the scene is good, only that it isn't broken.
    """
    project = spec.get("project") or {}
    try:
        canvas_w = int(project.get("width", 1080) or 1080)
        canvas_h = int(project.get("height", 1920) or 1920)
    except (TypeError, ValueError):
        canvas_w, canvas_h = 1080, 1920

    faults: list[str] = []
    for scene in spec.get("scenes", []):
        try:
            duration = float(scene.get("duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        faults.extend(_layer_faults(scene))
        faults.extend(_material_faults(scene, canvas_w, canvas_h))
        faults.extend(_overlap_faults(scene, canvas_w, canvas_h))
        faults.extend(_safe_area_faults(scene, canvas_w, canvas_h))
        for node in scene.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            nid = node.get("id", "?")
            if node.get("type") == "text" and not str(node.get("content") or "").strip():
                faults.append(f'node "{nid}" is a text node with no "content" — '
                              "it renders nothing; put the words in \"content\"")
            if node.get("type") == "image" and not str(node.get("src") or "").strip():
                faults.append(f'node "{nid}" is an image with no "src" — it renders '
                              'nothing; use "src": "asset:<name>" from the artwork list')
            if node.get("_no_position") and node.get("type") not in (
                    "group", "depth_layer", "particle_field", "light_field", "spline_tree",
                    "shape_arrow", "arrow"):
                # a field lays its tiles out itself, a tree has a hub, an
                # arrow has from/to, a layer only groups — the origin is
                # right for all of them
                faults.append(f'node "{nid}" has no "position" — it is drawn at the '
                              "top-left corner; give it a [x, y]")
            bounds = _node_bounds(node, canvas_w, canvas_h)
            if bounds:
                left, top, right, bottom = bounds
                fully_outside = (right <= 0 or bottom <= 0
                                  or left >= canvas_w or top >= canvas_h)
                if fully_outside:
                    faults.append(
                        f'node "{nid}" sits entirely outside the '
                        f"{canvas_w}x{canvas_h} frame (approx. bounds "
                        f"{left:.0f},{top:.0f} to {right:.0f},{bottom:.0f})")
                # A center-anchored node positioned near an edge — a common
                # real mistake: a headline meant to sit at a LEFT margin,
                # authored with anchor [0.5, 0.5] instead of [0, 0.5], runs
                # off the edge for anything wider than a couple of words.
                # Caught live on a real generated scene: "DISTRIBUTED
                # CRAWLER" at x=96 rendered as "...BUTED CRAWLER", clipped
                # at the left edge — entirely outside the frame never fires
                # for a PARTIAL overflow like that, so this is checked
                # separately, not folded into the condition above.
                # ...except a plain shape that is LARGER than the frame on
                # the axis it overflows: a ring or a rect that encloses the
                # canvas is a backdrop by construction, not clipped content
                # that a different anchor would have fitted.
                elif ((left < 0 or top < 0 or right > canvas_w or bottom > canvas_h)
                      and not (node.get("type") in ("shape_circle", "circle", "shape_rect")
                               and ((right - left >= canvas_w and (left < 0 or right > canvas_w))
                                    or (bottom - top >= canvas_h and (top < 0 or bottom > canvas_h))))):
                    faults.append(
                        f'node "{nid}" runs off the edge of the '
                        f"{canvas_w}x{canvas_h} frame (approx. bounds "
                        f"{left:.0f},{top:.0f} to {right:.0f},{bottom:.0f}) — "
                        "check its anchor against its position; a "
                        "center anchor near a margin clips wide content")
            if duration:
                faults.extend(_entrance_faults(node, duration))
    return faults
