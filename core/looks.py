"""
Prism — art directions for Studio and Motion
─────────────────────────────────────────────
Every Studio reel made on 13 Nov 2026 came out as a serif headline over an
arch, and every default Motion film as type on a dim vignette. The prompts
said "there is no house style" and then gave one worked example (the arch,
with its CSS), and a model given one example returns that example. They also
said "make a different decision than the last brief", to a model that has
never seen the last brief.

So the look is no longer left to a reflex. This is a small library of art
directions that differ in the things a viewer actually sees — ground, type,
framing, layout, movement — and Prism picks one per film: the one the client
named, else the one it used longest ago (it remembers the last seven). The
direction decides STRUCTURE. The client's brand kit still decides colour,
typeface and mark, so one business looks like itself across every direction.
That is the two-axis split other tools use: template × brand kit.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List

LOOKS: Dict[str, Dict[str, Any]] = {
    "editorial": {
        "image": "natural-light editorial photograph, muted warm tones, real materials, shallow depth of field",
        "title": "Editorial",
        "words": ("editorial", "magazine", "elegant", "classic"),
        "ground": "warm paper (an off-white or cream), dark ink text; colour only as punctuation",
        "type": "a high-contrast serif for headlines (Fraunces, Playfair Display) with a quiet grotesk for labels (DM Sans)",
        "framing": "hairline rules and small spaced-caps labels, like a magazine spread; wide margins",
        "layout": "left-aligned columns on a clear baseline; one large headline, one short supporting line",
        "motion": "unhurried: lines rise a short distance and settle, rules draw on, soft cross-fades",
        "avoid": "glows, gradients, rounded cards, anything centred by default",
    },
    "poster": {
        "image": "bold graphic illustration, flat saturated colour shapes, high contrast, screen-print feel",
        "title": "Poster",
        "words": ("poster", "bold", "loud", "punchy", "street"),
        "ground": "full-bleed flat colour fields — the accent itself is a background, alternating with black or white",
        "type": "one heavy condensed face in capitals (Anton, Bebas Neue, Archivo Black), set huge — a headline fills the frame's width",
        "framing": "colour blocks and thick bars; no thin lines, no cards",
        "layout": "type edge to edge, stacked tight; one idea per scene at maximum size",
        "motion": "hard and fast: blocks wipe across, words slam in, cuts land on the beat",
        "avoid": "serifs, soft shadows, gradients, small text, empty space around a small headline, a bare top or bottom third",
    },
    "swiss": {
        "image": "clean minimal studio photograph on a pale ground, precise geometry, lots of calm space",
        "title": "Swiss grid",
        "words": ("swiss", "minimal", "minimalist", "clean", "grid", "corporate"),
        "ground": "white or very light grey, black text, the accent on one element per scene",
        "type": "a single neutral grotesk in two weights (Inter, Space Grotesk, Archivo); numbers set large",
        "framing": "a visible grid: thin rules, index numbers (01, 02), aligned edges",
        "layout": "strictly left-aligned to shared columns; asymmetric; the empty area is deliberate but sits BESIDE the content, never a bare top or bottom third",
        "motion": "precise: elements slide along the grid lines and stop cleanly, no bounce",
        "avoid": "serifs, decoration, centred stacks, rounded shapes, more than one accent",
    },
    "night": {
        "image": "cinematic low-key photograph, one warm light source, deep shadows, soft haze",
        "title": "Night",
        "words": ("night", "cinematic", "dark", "moody", "dramatic"),
        "ground": "near-black, with one soft source of light in the accent colour that the scenes share",
        "type": "a refined light-weight face for headlines (Cormorant Garamond, Sora light) with a small sans for labels",
        "framing": "light itself: a glow, a beam, a lit edge; almost no drawn shapes",
        "layout": "centred and sparse — one line in the light, the dark around it filling the frame evenly (content spread across the middle two thirds, never pushed to one end)",
        "motion": "slow camera pushes, light that drifts and swells, text that fades up out of the dark",
        "avoid": "flat white panels, many colours, busy layouts, fast cuts",
    },
    "kinetic": {
        "image": "high-contrast graphic photograph with strong diagonals and a sense of motion",
        "title": "Kinetic type",
        "words": ("kinetic", "typographic", "energetic", "fast", "hype"),
        "ground": "two colours only for the whole film (the accent and one neutral), swapping roles scene to scene",
        "type": "one strong sans in heavy and light weights (Archivo, Unbounded, Syne); the words ARE the picture",
        "framing": "none — repetition, scale and rotation of the type do the work",
        "layout": "words at very different sizes in one scene: one huge, others small; lines turned 90°, stacked or repeated",
        "motion": "words arrive one at a time on a steady beat, scale through the frame, swap, invert",
        "avoid": "pictures used as decoration, cards, rules, long sentences, gentle fades",
    },
    "collage": {
        "image": "handmade cut-paper and torn-paper illustration with visible paper texture",
        "title": "Collage",
        "words": ("collage", "scrapbook", "handmade", "craft", "crafty"),
        "ground": "a textured paper or kraft tone; pictures and cut shapes sit on it like pieces on a table",
        "type": "a chunky friendly sans for headlines (Bricolage Grotesque, Space Grotesk) with a handwritten accent (Caveat) for one word or note",
        "framing": "cut-out pieces with a thin white edge, a strip of tape, a torn label; tilts of 3° at most",
        "layout": "one large piece and a headline per scene, slightly off-centre, overlapping a little",
        "motion": "pieces drop or slide in and settle with a small overshoot; a note is written on",
        "avoid": "glass, glows, perfect symmetry, piles of many tilted pictures",
    },
    "soft": {
        "image": "soft pastel 3D-rendered illustration, rounded forms, gentle light",
        "title": "Soft",
        "words": ("soft", "friendly", "playful", "fun", "pastel", "cute"),
        "ground": "a pale tint of the accent colour, with white rounded cards on it",
        "type": "a rounded sans throughout (Nunito, Poppins, Quicksand), medium and bold",
        "framing": "large-radius cards, pills, circles and soft drop shadows; simple drawn icons",
        "layout": "centred and roomy; one card or one big rounded shape per scene carrying the message",
        "motion": "springy: things pop, bounce once and rest; gentle floating while they hold",
        "avoid": "serifs, sharp corners, black backgrounds, thin hairlines",
    },
    "luxe": {
        "image": "rich dark still life with metallic highlights, velvet and glass, a single jewel colour",
        "title": "Luxe",
        "words": ("luxe", "luxury", "premium", "royal", "opulent"),
        "ground": "one deep jewel tone (the deep brand colour, or a deep green, wine or navy) with a metallic or cream second colour",
        "type": "a display serif in widely spaced capitals (Cormorant, Cinzel, Marcellus) with a light sans for small lines",
        "framing": "fine double lines, a thin border inset from the frame, one small ornament — never a plain arch",
        "layout": "centred and symmetrical; room around small type comes from margins on all sides, with the content block in the middle of the frame, not at one end",
        "motion": "slow and exact: lines draw themselves, letters space out as they appear, a soft shimmer once",
        "avoid": "heavy weights, bright flat colours, crowding, speed",
    },
    # Offered only when the client names it: the engine's own guidance calls the
    # dark-tech look "NOT a neutral default".
    "tech": {
        "image": "dark interface-style render with fine grid lines and one glowing accent",
        "title": "Tech",
        "words": ("tech", "techy", "futuristic", "cyber", "terminal"),
        "ground": "deep charcoal with a faint grid; one saturated accent",
        "type": "a geometric sans for headlines (Space Grotesk, Sora) with a monospace for labels and figures (JetBrains Mono)",
        "framing": "outlined panels, corner ticks, thin connecting lines, small data labels",
        "layout": "interface-like: panels on a grid, a figure or diagram as the hero",
        "motion": "things switch on: lines trace, numbers count, panels slide and lock",
        "avoid": "serifs, paper textures, warm pastel colours",
    },
}

# What the rotation draws from, in a fixed order.
ROTATION = ("editorial", "poster", "swiss", "night", "kinetic", "collage", "soft", "luxe")
# One fewer than the rotation: every direction is used before any comes round again.
REMEMBER = len(ROTATION) - 1


def image_style(name: str) -> str:
    """How this direction's pictures are made, for the image model's prompt."""
    return (LOOKS.get(name) or {}).get("image", "")


def named(request: str) -> str:
    """The direction the client asked for in words ("…in a bold poster style"), or ""."""
    low = (request or "").lower()
    for name, look in LOOKS.items():
        for w in look["words"]:
            if re.search(rf"\b{re.escape(w)}\b\s+(?:style|look|feel|design|direction|theme|vibe)", low) \
                    or re.search(rf"\b(?:style|look|feel|design|direction|theme|vibe)\s*[:\-]?\s*{re.escape(w)}\b", low):
                return name
    return ""


def pick(request: str, recent: List[str] | None = None) -> str:
    """Which direction this film takes: the one named in the request, else one
    not among the `recent` ones. The same request with the same history always
    gives the same answer, so a run can be reproduced."""
    asked = named(request)
    if asked:
        return asked
    recent = [r for r in (recent or []) if r in ROTATION][-REMEMBER:]
    free = [n for n in ROTATION if n not in recent] or list(ROTATION)
    seed = int(hashlib.sha256((request or "").strip().lower().encode("utf-8")).hexdigest(), 16)
    return free[seed % len(free)]


def text(name: str, fonts_given: bool = False) -> str:
    """The direction as the designer is told it. `fonts_given`: the client's own
    typefaces are stated elsewhere in the prompt and replace the direction's."""
    look = LOOKS.get(name)
    if not look:
        return ""
    colour = ("Where this prompt states the client's colours, build this ground from THEM: the "
              "direction says how colour is used, their palette says which colours. Otherwise the "
              "exact colours are yours, within that description.")
    return (
        f"\n\nART DIRECTION FOR THIS FILM — \"{look['title']}\". Prism chose it so that this film does "
        "not look like the last one; it is the brief, not a suggestion, and it overrides any example "
        "of a shape, layout or typeface given elsewhere in this prompt.\n"
        f"· Ground and colour: {look['ground']}. {colour}\n"
        + (f"· Type: {look['type']}.\n" if not fonts_given else
           "· Type: the client's own typefaces, set in this direction's manner.\n")
        + f"· Framing: {look['framing']}.\n"
        f"· Layout: {look['layout']}.\n"
        f"· Movement: {look['motion']}.\n"
        f"· Not in this direction: {look['avoid']}."
    )


def _path() -> str:
    from . import config as C
    return os.path.join(C.RUNS_DIR, "looks_recent.json")


def recent() -> List[str]:
    """The directions the last few films used, oldest first. [] when unknown."""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            got = json.load(f)
        return [g for g in got if isinstance(g, str)][-REMEMBER:] if isinstance(got, list) else []
    except Exception:                                           # noqa: BLE001
        return []


def remember(name: str) -> None:
    """Record that a film used `name`. Never raises: a film is not lost over a note."""
    if name not in LOOKS:
        return
    try:
        path = _path()
        kept = (recent() + [name])[-REMEMBER:]        # read before the file is opened for writing
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kept, f)
    except Exception:                                           # noqa: BLE001
        pass
