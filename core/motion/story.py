"""
Prism Motion Graphics — one story grammar, for any business
────────────────────────────────────────────────────────────
`spine.py` is a single film — a SaaS ad's signal, hub and chat bubbles —
written down as seven mandatory beats with pixel coordinates, and every client
got that film. This is the grammar underneath a whole family of films, taken
from the reference the 11-12 Nov 2026 study measured (a product film that
alternates short bursts with long holds):

    hook     the name arrives on a quiet page; the camera does the work
    act      someone does the thing this business is for, typed or pressed
    promise  what happens for them, as work in progress, then one sentence
    proof    the result on screen, then a variation
    payoff   one decisive change, a "done", the name and one way to reach them

Each part says WHAT it needs and in what ENERGY (hold, build, burst) but not
where anything goes: the model supplies the business — the customer's action,
three things done for them, the demo object — and lays it out for the frame it
was given. `beat_faults()` checks facts (a prompt box or a cursor is present, a
checklist has rows) the way spine.beat_faults does, never taste. Stdlib only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# share of the film's length, energy, what the part is, what it requires
BEATS: List[Dict[str, Any]] = [
    {"name": "hook", "share": 0.18, "energy": "build, then hold",
     "subject": "the name arrives on a quiet page and the camera does the work",
     "recipe": ("a stranger must know within two seconds WHAT THIS IS FOR. So: the name AND, under "
                "or beside it, ONE plain line saying what the business does and for whom, with a "
                "concrete claim in it (a number, a speed, a promise: \"Precision parts, quoted the "
                "same day\"). If the artwork list names a hero image of what the business makes, "
                "show it — large — here; the viewer should SEE the product. The name arrives typed "
                "or revealed on a quiet page; one slow camera push toward the focus. End on a HOLD "
                "of about a second, nothing drifting, so it is read."),
     "requires": {"text": 2}},
    {"name": "act", "share": 0.20, "energy": "build",
     "subject": "someone does the thing this business is for, typed or pressed",
     "recipe": ("the customer's action, shown as interface: a ui_input whose text types itself "
                "with the request in the customer's own words (\"Send us the drawing\", \"Book a "
                "table for four\"), and a cursor that travels to the send button and clicks it at "
                "send_at. Keep the box large and the page empty around it. A moment of stillness "
                "while it types is the point. Build tension: the request is specific, so the viewer "
                "wonders how it will be answered."),
     "requires": {"any": ["ui_input", "cursor"]}},
    {"name": "promise", "share": 0.20, "energy": "build, then hold",
     "subject": "what happens for them, as work in progress, then one sentence",
     "recipe": ("a ui_checklist of three rows — the three things done for them, each with a short "
                "label, a quieter sub-line, and a staggered \"at\" so they arrive one by one — then "
                "a single sentence in large type that says what they get. The rows arrive FASTER "
                "each time (their \"at\" gaps shrink) so it feels like momentum; then HOLD on the "
                "sentence for 1.5 seconds or more."),
     "requires": {"ui_checklist": 2}},
    {"name": "proof", "share": 0.27, "energy": "build, then burst",
     "subject": "the result on screen, then a variation",
     "recipe": ("a ui_window that rises into view holding THE DEMO OBJECT for this business — what "
                "the result actually looks like: a part drawing and a quote, a menu card, a "
                "dashboard tile, a listing card (use a supplied image if the artwork list names "
                "one, otherwise build it from text, numbers and shapes inside the window's "
                "children). Then offer a variation: ui_swatches popping in above it and the "
                "window changing palette."),
     "requires": {"ui_window": 1}},
    {"name": "payoff", "share": 0.15, "energy": "burst, then hold",
     "subject": "one decisive change, a done, the name and one way to reach them",
     "recipe": ("the answer LANDS: a ui_pill with check true (\"Quote ready\", \"Table booked\") "
                "arriving centred with \"hit\": true (a sound hit) and a scale pop, the one biggest "
                "motion of the film; then the name large and ONE contact line — a phone number or "
                "a web address, not both — that fits the frame with room. End on a HOLD."),
     "requires": {"ui_pill_check": 1, "hit": 1}},
]

_NAMES = [b["name"] for b in BEATS]


def beat(name: str) -> Optional[Dict[str, Any]]:
    for b in BEATS:
        if b["name"] == name:
            return b
    return None


def storyboard_text(total_seconds: float = 22.0) -> str:
    """The beat list for the storyboard turn, with each part's share of the length."""
    lines = []
    for i, b in enumerate(BEATS, 1):
        lines.append(f'  {i}. "{b["name"]}" (~{b["share"] * total_seconds:.1f}s, {b["energy"]}) — {b["subject"]}')
    return ("THE STORY — every film is these five parts, in this order, ONE SCENE EACH; the business, "
            "the customer's words and the demo object are yours:\n" + "\n".join(lines)
            + '\n\nEach storyboard row carries its "beat" name. REST IS PART OF THE DESIGN: a film that '
              "is always moving reads as noise. Each part ends on a hold; one part spends a decisive "
              "burst. Do not add decoration to fill a hold.")


def recipe_text(name: str) -> str:
    b = beat(name)
    if not b:
        return ""
    return (f'THIS SCENE\'S PART: "{name}" ({b["energy"]}) — {b["subject"]}.\nBUILD IT LIKE THIS: '
            f"{b['recipe']}\nA scene without what this part needs is sent back.")


def _walk(scene: Dict[str, Any]):
    stack = [n for n in scene.get("nodes") or [] if isinstance(n, dict)]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in n.get("children") or [] if isinstance(c, dict))


def beat_faults(scene: Dict[str, Any], name: str) -> List[str]:
    """What the scene lacks of its part's requirements — facts, not taste."""
    b = beat(name)
    if not b:
        return []
    nodes = list(_walk(scene))
    types = [str(n.get("type")) for n in nodes]
    out: List[str] = []
    req = b["requires"]
    if req.get("text") and types.count("text") < req["text"]:
        out.append(f'part "{name}" needs {req["text"]} text lines — the name AND one plain line saying what '
                   f'the business does and for whom — and the scene has {types.count("text")}')
    if req.get("any") and not any(t in types for t in req["any"]):
        out.append(f'part "{name}" needs one of {", ".join(req["any"])} — the customer\'s action shown as '
                   "interface — and the scene has none")
    if req.get("ui_checklist"):
        lists = [n for n in nodes if n.get("type") == "ui_checklist"]
        rows = max((len(n.get("items") or []) for n in lists), default=0)
        if rows < req["ui_checklist"]:
            out.append(f'part "{name}" needs a ui_checklist with at least {req["ui_checklist"]} rows; '
                       f"the scene has {'none' if not lists else str(rows) + ' row(s)'}")
    if req.get("ui_window") and "ui_window" not in types:
        out.append(f'part "{name}" needs a ui_window holding the demo object and the scene has none')
    if req.get("ui_pill_check") and not any(n.get("type") in ("ui_pill", "pill") and n.get("check") for n in nodes):
        out.append(f'part "{name}" needs a ui_pill with check true (the "done") and the scene has none')
    if req.get("hit") and not any(n.get("hit") for n in nodes):
        out.append(f'part "{name}" needs the answer to land with a sound: set "hit": true on the done pill')
    return [f"{f} — {recipe_text(name).split('BUILD IT LIKE THIS: ')[-1][:520]}" for f in out]


# ── what the eye does, what carries over, how it is shot ─────────────────────
#
# The first model-written story film (12 Nov 2026) had every part and still had
# nowhere for the eye to land, nothing carried from scene to scene, a generic
# camera, and an opening a stranger could not read. These check facts about
# those, so they can be sent back like a missing prompt box.

_BACKGROUND_TYPES = ("light_field", "depth_layer", "particle_field", "spline_tree")


def _foreground(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [n for n in scene.get("nodes") or []
            if isinstance(n, dict) and n.get("type") not in _BACKGROUND_TYPES
            and n.get("layer") not in ("background", "finish")]


def _area(node: Dict[str, Any], width: int, height: int) -> float:
    from .inspect import _node_bounds
    b = _node_bounds(node, width, height)
    return max(0.0, (b[2] - b[0]) * (b[3] - b[1])) if b else 0.0


def focus_faults(scene: Dict[str, Any], row: Optional[Dict[str, Any]] = None,
                 canvas: tuple = (1080, 1920)) -> List[str]:
    """Exactly one node is the focus, it is clearly the biggest thing, and the
    shot goes to it."""
    nodes = list(_walk(scene))
    flagged = [n for n in nodes if n.get("focus")]
    said = str((row or {}).get("focus", "")).strip()
    out: List[str] = []
    if not flagged:
        return [f'the scene marks no focus — set "focus": true on the ONE node the eye must land on'
                + (f' ({said})' if said else "") + " — and make it the biggest thing on the page"]
    if len(flagged) > 1:
        return [f'{len(flagged)} nodes are marked focus ({", ".join(str(n.get("id")) for n in flagged)}); '
                "exactly one — a page with two focal points has none"]
    focus = flagged[0]
    fid = str(focus.get("id", ""))
    f_area = _area(focus, *canvas)
    others = [n for n in _foreground(scene) if n is not focus and not any(c is focus for c in _walk({"nodes": [n]}))]
    biggest = max(((_area(n, *canvas), n) for n in others), key=lambda t: t[0], default=(0.0, None))
    if biggest[1] is not None and f_area > 0 and f_area < 1.5 * biggest[0]:
        out.append(f'the focus "{fid}" is not clearly the biggest thing: "{biggest[1].get("id")}" is '
                   f"{biggest[0] / f_area:.1f}x its size on the page. Make the focus at least 1.5x the next "
                   "biggest thing, or make the other smaller, lighter or later")
    shot = scene.get("shot") if isinstance(scene.get("shot"), dict) else {}
    target = shot.get("target")
    if shot.get("intent") in ("push", "macro", "reveal") and isinstance(target, str) and target and target != fid:
        out.append(f'the shot goes toward "{target}" but the focus is "{fid}" — the camera should lead the eye to the focus')
    return out


def continuity_faults(scene: Dict[str, Any], prev_scene: Optional[Dict[str, Any]], is_last: bool = False) -> List[str]:
    """Something carries from the last scene into this one, and (unless this is
    the last) something is offered to the next."""
    keys = {str(n["continuity_key"]) for n in _walk(scene) if n.get("continuity_key")}
    out: List[str] = []
    if prev_scene is not None:
        prev = {str(n["continuity_key"]) for n in _walk(prev_scene) if n.get("continuity_key")}
        if prev and not (prev & keys):
            out.append(f'nothing carries over from the last scene: reuse its continuity_key ({", ".join(sorted(prev))}) '
                       "on a node here, at a pose within reach of where it left, so the cut is one continuous film")
    if not is_last and not keys:
        out.append('nothing is offered to the next scene: give the carried node (the name, the mark, the box, the '
                   'window) a stable "continuity_key"')
    return out


def direction_faults(scenes: List[Dict[str, Any]]) -> List[str]:
    """The camera is directed across the film, not repeated: several kinds of
    shot, no three alike in a row, and it does not end on a push."""
    intents = [str((s.get("shot") or {}).get("intent", "")) for s in scenes if isinstance(s, dict)]
    named = [i for i in intents if i]
    if len(named) < 3:
        return []
    out: List[str] = []
    if len(set(named)) < 3:
        out.append(f"the camera is one idea ({', '.join(sorted(set(named)))}): use at least three kinds of shot — "
                   "establish, push to the focus, pull to reveal, hold to read")
    for i in range(len(intents) - 2):
        if intents[i] and intents[i] == intents[i + 1] == intents[i + 2]:
            out.append(f'three scenes in a row use the "{intents[i]}" shot (scenes {i + 1}-{i + 3}); vary it')
            break
    if intents[-1] == "push":
        out.append('the film ends on a "push": end on a "resolve", "pull" or "hold" so the answer settles')
    return out
