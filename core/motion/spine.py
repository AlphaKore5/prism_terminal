"""
Prism Motion Graphics — the authored spine
───────────────────────────────────────────
Authored structure, model-written content.

Two generated Alphakore reels through the story contract (10 Sep 2026)
carried their script in order and passed every gate, and still scored 46
against the reference with a motion correlation near zero: the writer's
scenes entered and sat. Asked for "one continuous transformation", a model
produces a logo and captions. So the transformation is no longer asked
for; it is given. The reference film's grammar is written down here as a
sequence of BEATS, each with the primitives and phases it requires and the
numbers that make them visible, the storyboard turn maps the script's lines
onto those beats, every scene prompt carries its beat's recipe, and a scene
that lacks its beat's transformation is sent back with the recipe.

The recipes' numbers are the authored recreation's (core.motion.fixtures
.inspo_fixture), which scores 84 against the reference — the same
primitives, phases, sizes and counts, with the brand, copy and colours left
to the model. Stdlib only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

BEATS: List[Dict[str, Any]] = [
    {"name": "gather", "seconds": 2.6,
     "subject": "scattered tiles gather into a low band — the signal finding its shape",
     "requires": {"particle_field": {"count": 100, "size": 22, "phases": ["scatter", "band"]},
                  "light_field": 1},
     "recipe": ('a particle_field (continuity_key "signal") with count ≥ 100, size ≥ 22 and '
                'phases [{"at": 0, "layout": {"kind": "scatter", "box": [20, 110, 1060, 700]}}, '
                '{"at": 0.3, "layout": {"kind": "band", "y": 855, "rows": 6, "cell": 44}, '
                '"duration": 0.9, "stagger": 0.8}] — the tiles MUST travel from the scatter to the '
                'band inside this scene; a faint grey light_field haze (intensity ≤ 0.1) behind it; '
                'the caption in the lower third')},
    {"name": "channel", "seconds": 3.3,
     "subject": "the band rises, three small glass cards name what it is for, then it bursts and streams into a column",
     "requires": {"particle_field": {"phases": ["band", "scatter", "column"]}, "glass_panel": 3},
     "recipe": ('the same particle_field (continuity_key "signal") with phases [{"at": 0, "layout": '
                '{"kind": "band", "y": 855, "rows": 6, "cell": 44}}, {"at": 1.6, "layout": {"kind": "band", '
                '"y": 490, "rows": 6, "cell": 44}, "duration": 0.8, "stagger": 0.3}, {"at": 2.6, "layout": '
                '{"kind": "scatter", "box": [30, 250, 1050, 1050], "cluster": 1, "cell": 44}, "duration": 0.5, '
                '"stagger": 0.15, "easing": "power3.out"}, {"at": 3.05, "layout": {"kind": "column", "x": 400, '
                '"spread": 60, "y0": -100, "y1": 1500}, "duration": 0.45, "stagger": 0.2}]; THREE small '
                'glass_panel cards (about 260x76, tinted, each with one short text child taken from the '
                "script or the brand's services) entering at 2.15-2.35 s in a row under the risen band at "
                'y ≈ 700 and exiting at 2.6 s; a light_field glow that brightens with the burst')},
    {"name": "converge", "seconds": 3.0,
     "subject": "channel icons appear on a diagonal and lines converge on a hub below; the column empties",
     "requires": {"icon": 3, "spline_tree": 1, "particle_field": {"phases": ["column", "hidden"]}},
     "recipe": ('the particle_field (continuity_key "signal") with phases [{"at": 0, "layout": {"kind": '
                '"column", "x": 400, "spread": 60, "y0": -100, "y1": 1500}}, {"at": 0, "layout": {"kind": '
                '"hidden"}, "duration": 0.3, "stagger": 0.15}]; at least THREE icon nodes (chat, phone, mail, '
                'whatsapp — the phone largest, size ≈ 240, at [532, 700]; the others 80-96 on a diagonal '
                'around it) entering at 0.1-0.8 s and exiting at 2.55 s; a spline_tree with "hub": [540, '
                '1350] and one leaf per icon, "draw_start": 1.9, "draw_duration": 0.7; the shot: '
                '{"intent": "parallax", "target": [540, 1480], "zoom": 1.08, "delay": 1.55, "duration": 0.55} '
                '— the camera pans DOWN at the end to reveal a dark orb (continuity_key "hub", radius 80, at '
                '[540, 1517], entering at 1.6 s) hanging below the phone')},
    {"name": "hub", "seconds": 3.15,
     "subject": "the dark orb hangs from a thin line over a spline tree among three or four soft bokeh lights; one small glass card at the right",
     "requires": {"orb": 1, "light_field": 3, "spline_tree": 1},
     "recipe": ('authored under the panned camera: the shot {"intent": "push", "target": [540, 1761], "zoom": '
                '1.18, "duration": 1.0}; the orb (continuity_key "hub", radius 80) at [540, 1494]; a 2 px '
                'shape_rect hang line from [540, 947] (anchor [0.5, 0]) 467 tall down to the orb; a small '
                'violet light_field (size 190, intensity 0.8) at the top of the line [540, 1259] entering at '
                '1.3 s; THREE OR FOUR bokeh light_fields (orange, purple, blue, gold; sizes 150-320; '
                'intensity 0.4-0.6) at [498, 1736], [197, 1382], [800, 1490], [1000, 1693] entering in turn; '
                'a spline_tree with "hub": [540, 1670] and leaves toward the corners; one glass_panel card '
                '(290x130, a title and one line from the script) at [875, 1363]; NO logo text here')},
    {"name": "sweep", "seconds": 4.4,
     "subject": "a bright sky sweeps in from the right and the copy arrives as chat bubbles",
     "requires": {"bright_backdrop": 1, "bubbles": 2},
     "recipe": ('the shot {"intent": "reveal", "target": [540, 960], "zoom": 1.0, "duration": 0.45}; a '
                'shape_circle "sky" at [540, 200] radius 1900, layer background, "fill": "radial-gradient('
                'circle at 50% 50%, #8fb4d6 0%, #86accf 12%, #7aa2cb 30%, #6f96c2 55%, #6288b8 80%, #5479ab '
                '100%)", entering at 0 over 0.42 s with x from 2400 to 0 and scale from -0.3 (it MUST be '
                'bright by 0.5 s); a white light_field (size 1300, intensity ≈ 0.2) at [300, 1000]; TWO OR '
                'THREE bubbles — shape_rect with radius ≈ 80 and a dark text child of 58-66 px carrying the '
                "script's line(s) — a white one 940x200 at [470, 746] sliding in from the right at 0.35 s, a "
                'blue (#2563FF) one 700x130 at [900, 870] at 1.0 s with white text, a white one 860x140 at '
                '[330, 1020] at 1.7 s; all three slide out to the left at 3.6-3.8 s; NO continuity_key on '
                'this scene\'s nodes (the sweep is the subject)')},
    {"name": "cards", "seconds": 3.5,
     "subject": "small glass cards on a spline tree over the bright sky, which fades toward the next beat",
     "requires": {"glass_panel": 2, "spline_tree": 1, "bright_backdrop": 1},
     "recipe": ('"transition_in": "morph"; a shape_rect "sky_2" 1400x2200 at [540, 960], layer background, '
                '"fill": "linear-gradient(115deg, #3d5c94 0%, #6b90c2 25%, #8fb2d8 42%, #98b9dc 58%, '
                '#6f95c6 78%, #4a6ba4 100%)", with an exit at 1.1 s over 1.0 s to opacity 0.6; a spline_tree '
                'with "hub": [540, 250] and leaves at the cards; TWO glass_panel cards (540x143 at [490, 718] '
                'and 460x100 at [450, 1060], tint #0d1119, each with a small title label above it and one or '
                "two lines of the script inside) entering at 0.5 and 1.0 s; the lower card carries "
                'continuity_key "signal"')},
    {"name": "knowledge", "seconds": 2.85,
     "subject": "one glass card with an icon over a glowing tree of lines — the knowledge base",
     "requires": {"glass_panel": 1, "spline_tree": 1},
     "recipe": ('the shot {"intent": "hold"}; a faint grey light_field haze (intensity ≈ 0.08) and a blue '
                'light_field glow (intensity ≈ 0.12) at [545, 1150]; ONE glass_panel card 560x160 at '
                '[461, 828] (continuity_key "signal") sliding in from the right (x from 320) at 0.15 s with '
                'an icon child, a title and two lines from the script; a spline_tree with "hub": [540, 250] '
                'converging on the card, stroke rgba(140,200,255,0.95), "draw_start": 0.3; three small icons '
                'with labels below')},
    {"name": "arc", "seconds": 1.75,
     "subject": "a great ring settles at the lower right with a short fact inside it",
     "requires": {"ring": 1, "text": 1},
     "recipe": ('the shot {"intent": "pull"}; a shape_circle at [564, 634] radius 359, "fill": '
                '"rgba(255,255,255,0.03)", "stroke": "rgba(255,255,255,0.55)", stroke_width 3, '
                'continuity_key "signal", entering over 1.2 s with a slow rotation wiggle; one short text '
                '(a number or two words from the script, 40 px) at [540, 743]')},
    {"name": "orb", "seconds": 2.1,
     "subject": "a lit orb fills the centre and changes colour",
     "requires": {"orb": 1},
     "recipe": ('the shot {"intent": "macro", "target": [540, 941], "zoom": 1.2}; a faint ring shape_circle '
                'radius 420 at [540, 848]; an orb (continuity_key "signal") radius 140 at [540, 848] with a '
                'brand-coloured core, a light rim, "glow_color" in the accent and "glow_blur" ≈ 44, an icon '
                'in it; a second orb at the same place in the second accent colour fading in at 1.0 s')},
    {"name": "resolve", "seconds": 2.5,
     "subject": "the orb becomes the logo; the name, the tagline and a call-to-action pill settle",
     "requires": {"logo": 1, "text": 2},
     "recipe": ('the shot {"intent": "resolve", "duration": 0.8}; the brand mark — an image with "src" '
                '"asset:<logo>" if one is listed, else the brand name as 104 px text — at [540, 760] '
                '(continuity_key "signal") entering at 0.55 s with a small scale pop; the tagline 33 px at '
                '[540, 903] at 0.8 s; a white shape_rect pill 330x72 radius 36 at [540, 1051] with dark '
                'text inside (the CTA line from the script) at 0.95 s; nothing else')},
]

_NAMES = [b["name"] for b in BEATS]
_CORE = ("gather", "channel", "converge", "hub", "sweep", "cards", "resolve")
_DROP_ORDER = ("arc", "orb", "knowledge", "cards", "hub", "channel", "converge", "sweep")


def beat(name: str) -> Optional[Dict[str, Any]]:
    for b in BEATS:
        if b["name"] == name:
            return b
    return None


def beats_for(n_scenes: int) -> List[str]:
    """The beat names for a piece of `n_scenes` scenes, in order. Ten
    scenes get the whole reference; fewer drop beats from the end of
    _DROP_ORDER first (the ring and the orb go before the hub); more
    repeat the sweep/cards pair. gather is always first, resolve last."""
    n = max(2, int(n_scenes))
    names = list(_NAMES)
    drop = list(_DROP_ORDER)
    while len(names) > n and drop:
        victim = drop.pop(0)
        if victim in names:
            names.remove(victim)
    while len(names) > n:
        names.pop(-2)
    extra = ["sweep", "cards"]
    k = 0
    while len(names) < n:
        names.insert(len(names) - 1, extra[k % 2])
        k += 1
    return names


def storyboard_text(n_scenes: int) -> str:
    """The beat list for the storyboard turn."""
    lines = []
    for i, name in enumerate(beats_for(n_scenes), 1):
        b = beat(name)
        lines.append(f'  {i}. "{name}" ({b["seconds"]:g}s) — {b["subject"]}')
    return ("THE SPINE — the piece follows these beats, in this order, one scene "
            "each; a script line belongs to the beat whose row it lands on:\n"
            + "\n".join(lines)
            + '\n\nEach storyboard row carries its "beat" name. Do not invent a '
              "different structure; put the brand, the copy and the colours into "
              "this one.")


# The recipes are a reference film's, colours included: a sky-blue gradient, a
# blue chat bubble, a blue-white tree, bokeh in "orange, purple, blue, gold". The
# writer copies the hex it is given, so a film in a dark red "night" direction
# came out with a flat #3d5c94 blue scene in the middle (13 Nov 2026, round 3):
# the art direction and the client's colours lost to the example. A recipe is
# therefore given in the film's own palette.
_SKY_RADIAL = ("radial-gradient(circle at 50% 50%, #8fb4d6 0%, #86accf 12%, #7aa2cb 30%, #6f96c2 55%, "
               "#6288b8 80%, #5479ab 100%)")
_SKY_LINEAR = ("linear-gradient(115deg, #3d5c94 0%, #6b90c2 25%, #8fb2d8 42%, #98b9dc 58%, "
               "#6f95c6 78%, #4a6ba4 100%)")


def _recolour(text: str, palette: Optional[Dict[str, Any]]) -> str:
    """`text` with the reference's blues swapped for the film's palette (bg_a, bg_b, accent,
    accent2). Unchanged when the palette does not name those four."""
    p = palette if isinstance(palette, dict) else {}
    bg_a, bg_b, accent, accent2 = (str(p.get(k) or "") for k in ("bg_a", "bg_b", "accent", "accent2"))
    if not (bg_a and bg_b and accent):
        return text
    accent2 = accent2 or accent
    for old, new in (
            (_SKY_RADIAL, f"radial-gradient(circle at 50% 50%, {bg_b} 0%, {bg_a} 100%)"),
            (_SKY_LINEAR, f"linear-gradient(115deg, {bg_a} 0%, {bg_b} 50%, {bg_a} 100%)"),
            ("#2563FF", accent),
            ("rgba(140,200,255,0.95)", accent),
            ("a small violet light_field", f"a small {accent2} light_field"),
            ("(orange, purple, blue, gold; ", f"({accent}, {accent2} and tints of the ground; "),
            ("a faint grey light_field haze", f"a faint {bg_b} light_field haze"),
            ("a blue light_field glow", f"a {accent2} light_field glow"),
            ("a bright sky sweeps in", "a field of the ground colour sweeps in"),
            ("over the bright sky", "over the ground colour"),
            ("it MUST be bright by 0.5 s", "it MUST be fully in by 0.5 s")):
        text = text.replace(old, new)
    return text


def recipe_text(name: str, palette: Optional[Dict[str, Any]] = None) -> str:
    b = beat(name)
    if not b:
        return ""
    return _recolour(
        f'THIS SCENE\'S BEAT: "{name}" — {b["subject"]}.\nBUILD IT LIKE THIS (the numbers '
        f"are the reference's, in 1080x1920; keep them unless the copy needs a nudge): "
        f"{b['recipe']}.\nA scene without this beat's primitives and phases is sent back.", palette)


# ── the check ────────────────────────────────────────────────────────────────

def _walk(scene: Dict[str, Any]):
    stack = [n for n in scene.get("nodes") or [] if isinstance(n, dict)]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in n.get("children") or [] if isinstance(c, dict))


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_bright_backdrop(node: Dict[str, Any]) -> bool:
    t = node.get("type")
    fill = str(node.get("fill") or "")
    if t in ("shape_circle", "shape_rect", "circle", "rect") and "gradient(" in fill:
        return _num(node.get("radius"), 0) >= 600 or (_num(node.get("width"), 0) >= 900
                                                       and _num(node.get("height"), 0) >= 900)
    if t == "light_field":
        return _num(node.get("intensity"), 0.6) >= 0.5 and _num(node.get("width"), 900) >= 1200
    return False


def _is_bubble(node: Dict[str, Any]) -> bool:
    if node.get("type") not in ("shape_rect", "glass_panel", "rect"):
        return False
    return any(c.get("type") == "text" and str(c.get("content") or "").strip()
               for c in node.get("children") or [] if isinstance(c, dict))


def beat_faults(scene: Dict[str, Any], name: str) -> List[str]:
    """What the scene lacks of its beat's requirements — concrete, checkable
    facts (a node type present or not, a phase kind present or not, a
    count or size under the floor), never taste."""
    b = beat(name)
    if not b:
        return []
    nodes = list(_walk(scene))
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for n in nodes:
        by_type.setdefault(str(n.get("type")), []).append(n)
    out: List[str] = []
    for key, need in (b.get("requires") or {}).items():
        if key == "particle_field":
            fields = by_type.get("particle_field", [])
            if not fields:
                out.append(f'beat "{name}" needs a particle_field and the scene has none')
                continue
            f = fields[0]
            if "count" in need and _num(f.get("count"), 0) < need["count"]:
                out.append(f'beat "{name}": the particle_field has count {_num(f.get("count"), 0):g}, '
                           f'it needs at least {need["count"]}')
            if "size" in need and _num(f.get("size"), 14) < need["size"]:
                out.append(f'beat "{name}": the particle_field has size {_num(f.get("size"), 14):g}, '
                           f'it needs at least {need["size"]}')
            kinds = []
            for ph in f.get("phases") or []:
                lay = ph.get("layout") if isinstance(ph, dict) else None
                kinds.append(str(lay.get("kind")) if isinstance(lay, dict) else str(ph))
            # a "points" layout is a scatter or a band with its positions
            # given (the composer uses the reference's own tile positions)
            given = kinds.count("points")
            for kind in need.get("phases", []):
                if kind in kinds:
                    continue
                if kind in ("scatter", "band") and given > 0:
                    given -= 1
                    continue
                out.append(f'beat "{name}": the particle_field has no "{kind}" phase — its phases '
                           f"are {kinds or 'missing'}; the tiles must move through "
                           f"{need['phases']} in this scene")
        elif key == "bright_backdrop":
            if not any(_is_bright_backdrop(n) for n in nodes):
                out.append(f'beat "{name}" needs a bright backdrop — a large gradient-filled '
                           "shape_circle/shape_rect or a big light_field with intensity ≥ 0.5 — "
                           "and the scene has none")
        elif key == "bubbles":
            have = sum(1 for n in nodes if _is_bubble(n))
            if have < need:
                out.append(f'beat "{name}" needs {need} chat bubbles (a shape_rect or glass_panel '
                           f"with a text child) and the scene has {have}")
        elif key == "ring":
            have = [n for n in by_type.get("shape_circle", []) if _num(n.get("radius"), 0) >= 250]
            if not have:
                out.append(f'beat "{name}" needs a large shape_circle ring (radius ≥ 250) and the scene has none')
        elif key == "logo":
            have = by_type.get("image", []) + [n for n in by_type.get("text", [])
                                                if _num(n.get("font_size"), 0) >= 80]
            if not have:
                out.append(f'beat "{name}" needs the brand mark (an image node, or the name as ≥ 80 px text)')
        else:
            have = len(by_type.get(key, []))
            if have < int(need):
                out.append(f'beat "{name}" needs {need} {key} node(s) and the scene has {have}')
    return out
