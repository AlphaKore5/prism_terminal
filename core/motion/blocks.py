"""
Prism Motion Graphics — a library of tested scenes, for any business
─────────────────────────────────────────────────────────────────────
Every Motion film so far was written scene by scene from a blank page: one
model turn per scene, each choosing positions, sizes and timings, then checked
and sent back. It is slow (a 7-scene film is ten turns), costs the most, and the
result varies with the model's mood: the 13 Nov 2026 Christmas film never rested,
invented a sender and left a third of a page empty. HeyGen's HyperFrames ships
about 450 ready-made blocks for the same reason.

A block is one scene whose layout, type scale, timing and hold are fixed in code
and tested in the real runtime at 9:16, 1:1 and 16:9. The model's part is to
choose the order and write the words: a plan of {"block", "seconds", "slots"}.
`compose_plan()` builds the whole spec in one pass with no further turns. Every
block ends on a hold (no drifting), sizes its type from the slot's width budget so
a line cannot spill, marks one focus, and carries a node into the next scene
(`continuity_key`) so the cut is not a reset. Stdlib only.
"""
from __future__ import annotations

import json
import re
import textwrap
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_PALETTE = {"bg_a": "#F6EFE2", "bg_b": "#EDE3D2", "ink": "#1C1713", "accent": "#C8161D", "accent2": "#14412F"}
DEFAULT_FONTS = {"display_font": "Fraunces", "body_font": "DM Sans",
                 "google_fonts_url": "https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,600;0,9..144,800;1,9..144,700"
                                     "&family=DM+Sans:wght@400;500;700&display=block"}
DISPLAY = "var(--motion-display-font)"
BODY = "var(--motion-body-font)"
CHAR_W = 0.58          # average glyph width in em, deliberately wide so a line cannot spill
LINE_H = 1.16
TRANSITIONS = ("blur_swoosh", "morph", "light_leak", "zoom")


PAPER, GOLD = "#F5EFE4", "#C9A96A"


def _fonts(display: str, dspec: str, body: str, bspec: str, note: str = "") -> Dict[str, str]:
    fam = [f"{display}:{dspec}"] + ([f"{body}:{bspec}"] if body != display else []) + ([f"{note}:wght@700"] if note else [])
    out = {"display_font": display, "body_font": body,
           "google_fonts_url": "https://fonts.googleapis.com/css2?" + "&".join("family=" + f.replace(" ", "+") for f in fam) + "&display=block"}
    if note:
        out["note_font"] = note
    return out


# ── art directions ────────────────────────────────────────────────────────────
# core.looks names eight directions in words, for a model. This is the same eight as numbers
# the blocks read, so one plan comes out as eight different films. `grounds` are the pages the
# scenes cycle through; `head`/`soft`/`mark`/`emph` are colours in order of preference, and the
# first that reads against the scene's ground is used (see Ctx.paint). "" is the library's own
# look: the plan's palette and motif, a centred serif on paper.
_BASE: Dict[str, Any] = {
    "palette": {}, "grounds": [("bg_a", "bg_b")], "head": ["accent"], "soft": ["accent2"], "mark": ["accent"], "emph": ["accent"],
    "fonts": DEFAULT_FONTS, "weight": 800, "char_w": CHAR_W, "scale": 1.0, "caps": False, "track": 0.0, "align": "center",
    "margin": 0.09, "share": 0.8, "motif": None, "card": None, "glow": False, "rise": 36, "ease": "expo.out", "pace": 1.0,
    "mode": None, "transitions": TRANSITIONS}
LOOKS: Dict[str, Dict[str, Any]] = {name: {**_BASE, **d} for name, d in {
    "": {},
    "editorial": {      # a magazine page: ink serif set left on paper, hairlines, colour only as punctuation
        "palette": {"bg_a": "#F7F2E8", "bg_b": "#EFE7D8", "ink": "#1B1714", "accent": "#B4432B", "accent2": "#5A5148"},
        "head": ["ink"], "soft": ["accent", "accent2"], "fonts": _fonts("Playfair Display", "ital,wght@0,700;0,800;1,700", "DM Sans", "wght@400;500;700"),
        "weight": 700, "char_w": 0.56, "align": "left", "margin": 0.1, "share": 0.66, "motif": "rules", "rise": 18, "ease": "power3.out",
        "pace": 1.2, "mode": "masked_reveal", "transitions": ("blur_swoosh", "morph")},
    "poster": {         # the accent IS the page: one condensed face in capitals, as wide as the frame, thick bars, hard cuts
        "palette": {"ink": "#111111", "accent": "#E4002B", "accent2": "#111111"},
        "grounds": [("accent", "accent"), ("dark", "dark"), ("light", "light")], "head": ["light", "accent"], "soft": ["ink"],
        "mark": ["dark", "accent"], "emph": ["dark", "accent"], "fonts": _fonts("Anton", "wght@400", "Archivo", "wght@500;700"),
        "weight": 400, "char_w": 0.5, "scale": 1.6, "caps": True, "margin": 0.055, "share": 0.74, "motif": "bars", "rise": 110,
        "pace": 0.6, "transitions": ("push", "zoom", "push_up", "squeeze")},
    "swiss": {          # a grid: one grotesk set left on white, an index number, the accent on one element
        "palette": {"bg_a": "#FFFFFF", "bg_b": "#F3F3F1", "ink": "#111111", "accent": "#E63312", "accent2": "#111111"},
        "head": ["ink"], "soft": ["ink"], "fonts": _fonts("Space Grotesk", "wght@500;700", "Inter", "wght@400;500;700"),
        "weight": 700, "char_w": 0.6, "align": "left", "margin": 0.1, "share": 0.6, "motif": "grid", "rise": 0, "ease": "power4.out",
        "pace": 0.85, "transitions": ("push", "push_up")},
    "night": {          # near-black with one light in the accent colour; a light serif fades up out of the dark
        "palette": {"bg_a": "#0A0B10", "bg_b": "#101119", "ink": "#101119", "accent": "#E0A458", "accent2": "#8C7B6B"},
        "head": ["ink"], "soft": ["accent"], "fonts": _fonts("Cormorant Garamond", "ital,wght@0,500;0,600;1,500", "Inter", "wght@300;400;500"),
        "weight": 500, "char_w": 0.5, "scale": 1.1, "glow": True, "rise": 10, "ease": "power2.out", "pace": 1.3, "mode": "blur_pop",
        "transitions": ("light_leak", "blur_swoosh")},
    "kinetic": {        # two colours swapping roles; a heavy sans in capitals set left, the words arriving on a beat
        "palette": {"ink": "#111111", "accent": "#2B2BFF", "accent2": "#111111"},
        "grounds": [("accent", "accent"), ("light", "light")], "head": ["accent", "light"], "soft": ["ink"], "mark": ["ink"],
        "emph": ["accent", "dark", "light"], "fonts": _fonts("Archivo", "wght@500;900", "Archivo", ""),
        "weight": 900, "char_w": 0.8, "scale": 1.35, "caps": True, "align": "left", "margin": 0.07, "rise": 70, "ease": "back.out",
        "pace": 0.7, "mode": "char_cascade", "transitions": ("zoom", "squeeze", "push")},
    "collage": {        # kraft paper with one cut white piece carrying the words, tilted, held by a strip of tape
        "palette": {"bg_a": "#DCCBAA", "bg_b": "#CFBB96", "ink": "#2A2118", "accent": "#D9481C", "accent2": "#1F5C4D"},
        "head": ["accent2", "ink"], "soft": ["accent"], "fonts": _fonts("Bricolage Grotesque", "wght@700;800", "DM Sans", "wght@400;500;700", "Caveat"),
        "char_w": 0.56, "margin": 0.14, "share": 0.62, "card": {"fill": "#FFFDF8", "radius": 6, "tilt": -1.5, "tape": True},
        "rise": 44, "ease": "back.out", "transitions": ("push_up", "squeeze", "morph")},
    "soft": {           # a pale tint of the accent with one white rounded card; a rounded sans; things pop and rest
        "palette": {"ink": "#2B2640", "accent": "#7C5CFF", "accent2": "#3E3470"},
        "grounds": [("tint", "tint2")], "fonts": _fonts("Nunito", "wght@600;900", "Nunito", ""), "weight": 900, "char_w": 0.6,
        "margin": 0.14, "share": 0.62, "card": {"fill": "#FFFFFF", "radius": 56, "tilt": 0, "tape": False}, "rise": 30,
        "ease": "back.out", "mode": "word_stagger", "transitions": ("squeeze", "zoom", "morph")},
    "luxe": {           # the deep colour as the page, a thin double border inset from the frame, spaced capitals, slow
        "palette": {"ink": "#14110F", "accent": GOLD, "accent2": "#12372A"},
        "grounds": [("accent2", "deep2")], "head": ["ink"], "soft": ["accent", GOLD], "mark": ["accent", GOLD], "emph": ["accent", GOLD],
        "fonts": _fonts("Cinzel", "wght@500;600", "Jost", "wght@300;400;500"), "weight": 500, "char_w": 0.84, "scale": 0.85,
        "caps": True, "track": 0.08, "share": 0.6, "motif": "inset", "rise": 8, "ease": "power2.out", "pace": 1.3,
        "mode": "shimmer_sweep", "transitions": ("blur_swoosh", "light_leak")},
}.items()}


def _hex(v: Any) -> Optional[str]:
    """#RRGGBB out of a colour a plan or a brand kit gave, or None."""
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", str(v or "").strip())
    if not m:
        return None
    h = m.group(1)
    return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h).upper()


def _mix(a: str, b: str, t: float) -> str:
    """`a` moved the share `t` of the way to `b`."""
    return "#" + "".join("%02X" % round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (1, 3, 5))


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colours (1 to 21)."""
    def lum(c):
        ch = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, bl = (v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    lo, hi = sorted((lum(a), lum(b)))
    return (hi + 0.05) / (lo + 0.05)


class Ctx:
    """The canvas, art direction and palette every block builds against."""

    def __init__(self, width: int, height: int, palette: Dict[str, str], look: str = "", brand: Optional[Dict[str, Any]] = None):
        self.w, self.h = width, height
        self.L = LOOKS.get(look) or LOOKS[""]
        given = {k: _hex(v) for k, v in (palette or {}).items() if _hex(v)}
        if self.L is not LOOKS[""]:                           # a direction owns its ground; the plan keeps only its colours
            given = {k: v for k, v in given.items() if k in ("accent", "accent2")}
        mark = {k: _hex((brand or {}).get(b)) for k, b in (("accent", "accent"), ("accent2", "deep")) if _hex((brand or {}).get(b))}
        self.base = {**DEFAULT_PALETTE, **self.L["palette"], **given, **mark}
        acc, deep = self.base["accent"], self.base["accent2"]
        self.base.update(light="#F4F1EA", dark="#111111", tint=_mix(acc, "#FFFFFF", 0.84), tint2=_mix(acc, "#FFFFFF", 0.74),
                         deep2=_mix(deep, "#000000", 0.35))
        self.m = round(min(width, height) * self.L["margin"])  # outer margin
        self.cx, self.cy = width / 2, height * 0.47          # optical centre, a little above true centre
        self.x, self.anchor = (self.m, (0, 0.5)) if self.L["align"] == "left" else (self.cx, (0.5, 0.5))
        self.share = self.L["share"]                          # how much of the frame's height the text may use
        self.motif = "none"
        self.logo: Optional[Dict[str, Any]] = None            # {"src", "w", "h"}: the client's mark, for the close
        self.k = 1.0                                          # shrink factor, set by compose_plan when a block is too tall
        self.u = (width * height) ** 0.5 / 1.33              # a portrait-equivalent width, so small type scales with the frame
        self.paint(0)

    def usable(self) -> float:
        return self.w - 2 * self.m

    def paint(self, index: int) -> None:
        """Set `p`, the colours of scene `index`: its ground, and for each role the first of the
        direction's choices that reads against what the type sits on (the card, else the page).
        A brand colour that would vanish on this ground gives way to a neutral, never to a guess."""
        tok = lambda c: self.base.get(c, c)                   # noqa: E731
        a, b = (tok(c) for c in self.L["grounds"][index % len(self.L["grounds"])])
        on = self.L["card"]["fill"] if self.L["card"] else a

        def pick(names, ground=on, need=3.0):
            neutral = max((self.base["ink"], PAPER), key=lambda c: _contrast(c, ground))
            return next((tok(n) for n in list(names) + ["ink"] if _contrast(tok(n), ground) >= need), neutral)
        self.p = {"bg_a": a, "bg_b": b, "on": on, "ink": pick([], need=4.5), "head": pick(self.L["head"]), "soft": pick(self.L["soft"]),
                  "mark": pick(self.L["mark"]), "emph": pick(self.L["emph"]), "line": pick(self.L["mark"], a, 2.0),
                  "accent": self.base["accent"], "accent2": self.base["accent2"]}


MOTIFS = ("band", "frame", "dots", "none")       # what a plan may ask for; a direction brings its own


def _motif_nodes(ctx: "Ctx", motif: str, index: int) -> List[Dict[str, Any]]:
    """Colour and structure around the type, so a page is not only words on cream. They arrive
    with the scene, never move afterwards, and stay clear of the text area (the content is
    fitted to the space they leave, see Ctx.share)."""
    acc, deep = ctx.p["accent"], ctx.p["accent2"]
    w, h = ctx.w, ctx.h

    def rect(nid, x, y, rw, rh, fill, at, tween, radius=0):
        return {"id": nid, "type": "shape_rect", "position": [round(x), round(y)], "width": round(rw), "height": round(rh),
                "radius": radius, "fill": fill, "animation": _enter(at, 0.7, tween)}
    wipe = {"channel": "clipInset", "from": 0, "to": 100, "easing": "expo.out"}
    fade = {"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"}
    line, top = ctx.p["line"], h * (0.125 if h > w else 0.085)   # below where a vertical platform puts its own chrome
    if motif == "rules":                                    # hairlines at head and foot, one short heavy tick
        return [rect("rule_t", w / 2, top, ctx.usable(), 3, ctx.p["ink"], 0.0, wipe),
                rect("rule_b", w / 2, h - top, ctx.usable(), 3, ctx.p["ink"], 0.15, wipe),
                rect("tick", ctx.m + ctx.u * 0.06, top + 9, ctx.u * 0.12, 14, line, 0.3, wipe)]
    if motif == "bars":                                     # thick bars that slam in from above and below
        bh = round(h * 0.04)
        return [rect("bar_t", w / 2, bh / 2, w * 1.2, bh, line, 0.0, {"channel": "y", "from": -bh * 2, "to": 0, "easing": "expo.out"}),
                rect("bar_b", w / 2, h - bh / 2, w * 1.2, bh, line, 0.05, {"channel": "y", "from": bh * 2, "to": 0, "easing": "expo.out"})]
    if motif == "grid":                                     # an index number on a full-width hairline, the accent once
        fs = max(22, round(ctx.u * 0.03))
        return [{**_text("index", f"{index + 1:02d}", ctx, ctx.m, top + fs * 1.1, fs, weight=500, fill=ctx.p["ink"], font=BODY,
                         anchor=(0, 0.5)), "animation": _enter(0.0, 0.4, fade)},
                rect("grid_rule", w / 2, top, ctx.usable(), 2, ctx.p["ink"], 0.0, wipe),
                rect("grid_mark", w - ctx.m - fs / 2, top + fs * 1.1, fs, fs, line, 0.2, fade)]
    if motif == "inset":                                    # a thin double border inset from the frame, one small ornament
        i = round(max(w, h) * 0.045)
        edge = lambda nid, d, sw, at: {"id": nid, "type": "shape_rect", "position": [round(w / 2), round(h / 2)],   # noqa: E731
                                       "width": w - 2 * (i + d), "height": h - 2 * (i + d), "radius": 0, "fill": "none",
                                       "stroke": line, "stroke_width": sw, "animation": _enter(at, 1.2, fade)}
        return [edge("inset_a", 0, 3, 0.0), edge("inset_b", 14, 1, 0.3),
                {**rect("ornament", w / 2, i + 7, 20, 20, line, 0.6, fade), "rotation": 45}]
    if motif == "band":
        bh = round(h * 0.1)
        fill = acc if index % 2 == 0 else deep
        return [rect("band", w / 2, h - bh / 2, w, bh, fill, 0.0, {"channel": "y", "from": bh, "to": 0, "easing": "expo.out"}),
                rect("band_rule", w / 2, h - bh - 5, w, 10, deep if fill == acc else acc, 0.15, wipe)]
    if motif == "frame":
        i, t = round(max(w, h) * 0.045), 8                  # far enough in to survive the camera's push (up to about 8 %)
        return [rect("frame_t", w / 2, i, w - 2 * i, t, acc, 0.0, wipe), rect("frame_b", w / 2, h - i, w - 2 * i, t, acc, 0.1, wipe),
                rect("frame_l", i, h / 2, t, h - 2 * i, acc, 0.05, wipe), rect("frame_r", w - i, h / 2, t, h - 2 * i, acc, 0.15, wipe)]
    if motif == "dots":
        r, y = round(min(w, h) * 0.012), round(h * 0.075)
        colours = [acc, deep, acc, deep, acc]
        return [rect(f"dot_{k}", w / 2 + (k - 2) * r * 4.2, y, r * 2, r * 2, colours[k], 0.05 + 0.08 * k,
                     {"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"}, radius=r) for k in range(5)]
    return []


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def fit(value: Any, chars: int, lines: int = 1) -> str:
    """`value` wrapped to at most `lines` lines of at most `chars` characters,
    trimmed with an ellipsis if it still does not fit."""
    text = _clean(value)
    if not text:
        return ""
    rows = textwrap.wrap(text, width=max(4, chars), break_long_words=True)
    if len(rows) > lines:
        rest = " ".join(rows[lines - 1:])
        rows = rows[:lines - 1] + [rest[:max(1, chars - 1)].rstrip() + "…"]
    return "\n".join(rows)


def _size(ctx: Ctx, text: str, max_px: float, min_px: float = 28, width: Optional[float] = None, body: bool = False) -> int:
    """The largest font size (≤ max_px) at which the longest line of `text` fits `width`. Display type
    is measured with the direction's own face (its glyph width, capitals, tracking) and scale."""
    width = width or ctx.usable()
    longest = max((len(r) for r in text.split("\n")), default=1) or 1
    cw, scale = (CHAR_W, 1.0) if body else (ctx.L["char_w"], ctx.L["scale"])
    return int(max(min_px * ctx.k, min(max_px * scale * ctx.k, width / (longest * cw))))


def _shot(ctx: Ctx, intent: str, zoom: float, seconds: float, share: float = 0.7) -> Dict[str, Any]:
    """The camera's move: it eases out and settles in the first `share` of the scene, so the last stretch
    is a true hold. A move that eased in and out across 80 % of the scene never rested: at 16:9 only one
    stretch of the film was still for a full second (longest 1.1 s)."""
    zoom = zoom + (0.02 if ctx.h > ctx.w and zoom > 1.0 else 0.0)   # the same push moves fewer pixels in a narrow frame
    return {"intent": intent, "target": [round(ctx.cx), round(ctx.cy)], "zoom": zoom, "duration": round(seconds * share, 2),
            "easing": "power2.out"}


def _enter(t: float, dur: float, *tweens: Dict[str, Any]) -> Dict[str, Any]:
    return {"enter": {"time": round(t, 2), "duration": round(dur, 2), "tweens": list(tweens)}}


def _rise(dist: float = 36, ease: str = "expo.out") -> List[Dict[str, Any]]:
    return [{"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"}] + (
        [{"channel": "y", "from": dist, "to": 0, "easing": ease}] if dist else [])      # a look that does not rise only fades


def _text(nid: str, content: str, ctx: Ctx, x: float, y: float, size: int, *, weight: int = 700, fill: Optional[str] = None,
          font: str = DISPLAY, anchor=None, at: float = 0.0, dur: float = 0.7, mode: Optional[str] = None,
          focus: bool = False, hit: bool = False, key: Optional[str] = None, runs: Optional[list] = None) -> Dict[str, Any]:
    if font == DISPLAY:                                     # the direction's own weight, case and tracking
        weight = ctx.L["weight"]
        if ctx.L["caps"]:
            content, runs = content.upper(), runs and [dict(r, text=r["text"].upper()) for r in runs]
    n: Dict[str, Any] = {"id": nid, "type": "text", "content": content, "text": content,
                         "position": [round(x), round(y)], "anchor": list(anchor or ctx.anchor), "font_size": int(size),
                         "font_weight": weight, "font_family": font, "fill": fill or ctx.p["ink"],
                         "animation": _enter(at, dur, *_rise(ctx.L["rise"], ctx.L["ease"]))}
    if font == DISPLAY and ctx.L["track"]:
        n["letter_spacing"] = round(size * ctx.L["track"], 1)
    if mode:
        n["mode"], n["reveal_start"], n["reveal_duration"] = mode, round(at, 2), round(dur, 2)
    if focus:
        n["focus"] = True
    if hit:
        n["hit"] = True
    if key:
        n["continuity_key"] = key
    if runs:
        n["runs"] = runs
    return n


def _page(ctx: Ctx, gradient: bool = True) -> Dict[str, Any]:
    fill = f"linear-gradient(180deg, {ctx.p['bg_a']} 0%, {ctx.p['bg_b']} 100%)" if gradient else ctx.p["bg_a"]
    if ctx.L["glow"]:                                       # one soft light in the accent colour, in the same place in every scene
        fill = (f"radial-gradient(ellipse 85% 48% at 50% 45%, {_mix(ctx.p['bg_a'], ctx.p['accent'], 0.5)} 0%, "
                f"{_mix(ctx.p['bg_a'], ctx.p['accent'], 0.16)} 45%, transparent 100%), " + fill)
    return {"id": "bg", "type": "shape_rect", "position": [round(ctx.w / 2), round(ctx.h / 2)], "width": ctx.w, "height": ctx.h,
            "radius": 0, "fill": fill}


def _kicker(ctx: Ctx, text: str, y: float, size: int, at: float) -> Dict[str, Any]:
    """The small label above a headline: spaced capitals in the body face (the direction's note face if it has one)."""
    note = ctx.L["fonts"].get("note_font")
    n = _text("kicker", text, ctx, ctx.x, y, size, weight=700, fill=ctx.p["soft"], font=f"'{note}', cursive" if note else BODY, at=at)
    if ctx.L is not LOOKS[""] and not note:
        n["letter_spacing"] = round(size * 0.14, 1)
    return n


def _rule(ctx: Ctx, nid: str, x: float, y: float, w: float, at: float, fill: Optional[str] = None, h: int = 6) -> Dict[str, Any]:
    return {"id": nid, "type": "shape_rect", "position": [round(x), round(y)], "width": round(w), "height": h, "radius": 0,
            "fill": fill or ctx.p["mark"],
            "animation": _enter(at, 0.55, {"channel": "clipInset", "from": 0, "to": 100, "easing": "expo.out"})}


def _stack(ctx: Ctx, rows: List[Tuple[str, int]], gap: float) -> List[float]:
    """Centre y of each row of a vertical stack of (text, size), centred on the optical centre."""
    heights = [(t.count("\n") + 1) * s * LINE_H for t, s in rows]
    total = sum(heights) + gap * (len(rows) - 1)
    y, out = ctx.cy - total / 2, []
    for h in heights:
        out.append(y + h / 2)
        y += h + gap
    return out


# ── the blocks: each returns (nodes, shot) ────────────────────────────────────
# slots: {name: {"chars", "lines", "what"}}; optional ones may be empty.

def _title(ctx: Ctx, s: Dict[str, str], seconds: float):
    """The name arrives large on a quiet page; one line says what it is; a hold."""
    head = fit(s.get("headline"), 14, 2)
    kicker = fit(s.get("kicker"), 28, 1).upper()
    sub = fit(s.get("line"), 34, 2)
    size_h = _size(ctx, head, ctx.h * 0.1 if ctx.h >= ctx.w else ctx.h * 0.18, 56)
    size_k, size_s = max(24, round(ctx.u * 0.034)), max(30, round(ctx.u * 0.05))
    rows = [(t, z) for t, z in ((kicker, size_k), (head, size_h), (sub, size_s)) if t]
    ys = _stack(ctx, rows, ctx.h * 0.025)
    nodes = [_page(ctx)]
    it = iter(ys)
    if kicker:
        nodes.append(_kicker(ctx, kicker, next(it), size_k, 0.15))
    nodes.append(_text("headline", head, ctx, ctx.x, next(it), size_h, weight=800, fill=ctx.p["head"], at=0.35, dur=0.9,
                       focus=True, key="carry", mode=ctx.L["mode"]))
    if sub:
        nodes.append(_text("line", sub, ctx, ctx.x, next(it), size_s, weight=500, fill=ctx.p["ink"], font=BODY, at=min(1.1, seconds * 0.35)))
    return nodes, _shot(ctx, "push", 1.06, seconds, 0.7)


def _statement(ctx: Ctx, s: Dict[str, str], seconds: float):
    """One sentence, large, with the key word in the accent colour; a hold."""
    text = fit(s.get("text"), 18, 4)
    kicker = fit(s.get("kicker"), 30, 1).upper()
    emph = _clean(s.get("emphasis"))
    size = _size(ctx, text, ctx.h * 0.085 if ctx.h >= ctx.w else ctx.h * 0.17, 48)
    size_k = max(24, round(ctx.u * 0.034))
    rows = [(t, z) for t, z in ((kicker, size_k), (text, size)) if t]
    ys = _stack(ctx, rows, ctx.h * 0.03)
    nodes = [_page(ctx)]
    it = iter(ys)
    if kicker:
        nodes.append(_kicker(ctx, kicker, next(it), size_k, 0.1))
    runs = None
    if emph and emph.lower() in text.lower():
        i = text.lower().index(emph.lower())
        runs = [r for r in ({"text": text[:i], "fill": ctx.p["ink"]}, {"text": text[i:i + len(emph)], "fill": ctx.p["emph"]},
                            {"text": text[i + len(emph):], "fill": ctx.p["ink"]}) if r["text"]]
    nodes.append(_text("headline", text, ctx, ctx.x, next(it), size, weight=800, at=0.3, dur=0.9, focus=True, key="carry",
                       mode="word_stagger", runs=runs))
    return nodes, _shot(ctx, "push", 1.03, seconds, 0.7)


def _list(ctx: Ctx, s: Dict[str, Any], seconds: float):
    """A heading and three or four items arriving one after another, each faster; a hold."""
    head = fit(s.get("heading"), 26, 2)
    items = [fit(i, 30, 1) for i in (s.get("items") or []) if _clean(i)][:4] or [""]
    size_i = _size(ctx, "\n".join(items), ctx.h * 0.05 if ctx.h >= ctx.w else ctx.h * 0.095, 30, width=ctx.usable() * 0.92, body=True)
    cap_h = ctx.h * 0.058 if ctx.h >= ctx.w else ctx.h * 0.11
    size_h = min(max(size_i + 6, _size(ctx, head, cap_h, 36)), _size(ctx, head, cap_h, 20))   # a heading in the body's size must still fit
    gap = size_i * 1.15
    rows = [(head, size_h)] + [(i, size_i) for i in items]
    total = (head.count("\n") + 1) * size_h * LINE_H + len(items) * (size_i * LINE_H + gap)
    x0 = ctx.m
    y = ctx.cy - total / 2
    nodes = [_page(ctx)]
    hh = (head.count("\n") + 1) * size_h * LINE_H
    nodes.append(_text("headline", head, ctx, x0, y + hh / 2, size_h, weight=800, fill=ctx.p["soft"], anchor=(0, 0.5), at=0.1,
                       focus=True, key="carry"))
    y += hh + gap * 0.6
    nodes.append(_rule(ctx, "rule", x0 + ctx.usable() / 2, y - gap * 0.3, ctx.usable(), 0.4))
    t = 0.7
    for k, item in enumerate(items):
        yy = y + size_i * LINE_H / 2
        nodes.append({"id": f"dot_{k}", "type": "shape_rect", "position": [round(x0 + size_i * 0.3), round(yy)], "width": round(size_i * 0.5),
                      "height": round(size_i * 0.5), "radius": round(size_i * 0.25), "fill": ctx.p["mark"],
                      "animation": _enter(t, 0.4, {"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"})})
        nodes.append(_text(f"item_{k}", item, ctx, x0 + size_i * 1.1, yy, size_i, weight=600, fill=ctx.p["ink"], font=BODY,
                           anchor=(0, 0.5), at=t, dur=0.55))
        y += size_i * LINE_H + gap
        t += max(0.25, 0.6 - 0.1 * k)           # each row arrives faster than the last
    return nodes, _shot(ctx, "reveal", 1.04, seconds, 0.7)


def _number(ctx: Ctx, s: Dict[str, str], seconds: float):
    """One big figure, what it measures, and a closing line; a hold."""
    figure = fit(s.get("figure"), 9, 1)
    label = fit(s.get("label"), 26, 2)
    line = fit(s.get("line"), 34, 2)
    size_f = _size(ctx, figure, ctx.h * 0.16 if ctx.h >= ctx.w else ctx.h * 0.3, 90)
    size_l, size_s = max(32, round(ctx.u * 0.055)), max(28, round(ctx.u * 0.042))
    rows = [(t, z) for t, z in ((figure, size_f), (label, size_l), (line, size_s)) if t]
    ys = iter(_stack(ctx, rows, ctx.h * 0.025))
    nodes = [_page(ctx)]
    nodes.append(_text("headline", figure, ctx, ctx.x, next(ys), size_f, weight=800, fill=ctx.p["emph"], at=0.2, dur=0.9, focus=True,
                       key="carry", mode=ctx.L["mode"]))
    if label:
        nodes.append(_text("label", label, ctx, ctx.x, next(ys), size_l, weight=700, fill=ctx.p["ink"], font=BODY, at=0.7))
    if line:
        nodes.append(_text("line", line, ctx, ctx.x, next(ys), size_s, weight=500, fill=ctx.p["soft"], font=BODY, at=1.0))
    return nodes, _shot(ctx, "push", 1.05, seconds, 0.7)


def _close(ctx: Ctx, s: Dict[str, str], seconds: float):
    """The client's mark if there is one, the answer landing (a pill with a check and a sound), the name,
    one way to reach them; a hold."""
    name = fit(s.get("name"), 16, 2)
    done = fit(s.get("done"), 24, 1)
    contact = fit(s.get("contact"), 30, 1)
    size_n = _size(ctx, name, ctx.h * 0.09 if ctx.h >= ctx.w else ctx.h * 0.16, 56)
    size_c = max(32, round(ctx.u * 0.052))
    pill_fs = max(26, round(ctx.u * 0.038))
    nodes = [_page(ctx)]
    lh = lw = pad = 0.0
    if ctx.logo:
        # Whole and undistorted: a box of the mark's own proportions. A mark nobody has looked at may be
        # dark or pale, so on anything but a light ground it gets a light plate of its own.
        lh = min(ctx.w, ctx.h) * 0.26 * ctx.k
        ratio = (ctx.logo.get("w") or 1) / (ctx.logo.get("h") or 1)
        lw = min(ctx.usable() * 0.7, lh * ratio)
        lh = lw / ratio                                             # a mark held to the width keeps its shape
        pad = lh * 0.12 if _contrast(ctx.p["on"], "#FFFFFF") > 1.35 else 0.0
    top = ctx.cy - (size_n * LINE_H * (name.count("\n") + 1) + size_c * 2.2 + pill_fs * 2.6 + (lh + 2 * pad + pill_fs if lh else 0)) / 2
    y = top
    if lh:
        x = ctx.x + (0.5 - ctx.anchor[0]) * (lw + 2 * pad)      # the centre of the mark, wherever the type is anchored
        fade = lambda: _enter(0.1, 0.7, {"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"})   # noqa: E731  one dict per node: the resolver offsets each node's times in place
        if pad:
            nodes.append({"id": "logo_plate", "type": "shape_rect", "position": [round(x), round(y + pad + lh / 2)],
                          "width": round(lw + 2 * pad), "height": round(lh + 2 * pad),
                          "radius": round(min(ctx.L["card"]["radius"] if ctx.L["card"] else 0, lh * 0.2)), "fill": "#FFFFFF", "animation": fade()})
        nodes.append({"id": "logo", "type": "image", "src": ctx.logo["src"], "position": [round(x), round(y + pad + lh / 2)],
                      "width": round(lw), "height": round(lh), "radius": 0, "anchor": [0.5, 0.5], "animation": fade()})
        y += lh + 2 * pad + pill_fs
    if done:
        nodes.append({"id": "done", "type": "ui_pill", "label": done, "check": True, "hit": True, "font_size": pill_fs,
                      "position": [round(ctx.x), round(y + pill_fs * 1.2)], "anchor": list(ctx.anchor),
                      "animation": _enter(0.3, 0.5, {"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"},
                                          {"channel": "y", "from": 24, "to": 0, "easing": "back.out"})})
    y += pill_fs * 2.6
    hh = (name.count("\n") + 1) * size_n * LINE_H
    nodes.append(_text("headline", name, ctx, ctx.x, y + hh / 2, size_n, weight=800, fill=ctx.p["head"], at=0.9, dur=0.9,
                       focus=True, key="carry", mode=ctx.L["mode"]))
    y += hh + size_c * 0.8
    if contact:
        nodes.append(_text("contact", contact, ctx, ctx.x, y + size_c / 2, size_c, weight=600, fill=ctx.p["ink"], font=BODY, at=1.6))
    return nodes, _shot(ctx, "resolve", 1.0, seconds, 0.5)


BLOCKS: Dict[str, Dict[str, Any]] = {
    "title": {"build": _title, "min": 2.6, "use": "an opening: the name, big, and one plain line saying what it is and for whom",
              "slots": {"headline": "the name or greeting, up to 14 characters per line, 2 lines (REQUIRED)",
                        "kicker": "a short label above it, up to 28 characters (optional)",
                        "line": "one plain line under it, up to 34 characters per line, 2 lines (optional)"}},
    "statement": {"build": _statement, "min": 2.6, "use": "one sentence the viewer must read",
                  "slots": {"text": "the sentence, up to 18 characters per line, 4 lines (REQUIRED)",
                            "kicker": "a short label above it (optional)",
                            "emphasis": "the word or phrase in it to colour in the accent (optional, must appear in the sentence)"}},
    "list": {"build": _list, "min": 3.4, "use": "three or four things, arriving one by one",
             "slots": {"heading": "what the list is, up to 26 characters per line, 2 lines (REQUIRED)",
                       "items": "3 or 4 short items, up to 30 characters each (REQUIRED)"}},
    "number": {"build": _number, "min": 2.8, "use": "one figure that proves the point",
               "slots": {"figure": "the figure, up to 9 characters, e.g. 22 years, 4.9★, 24/7 (REQUIRED)",
                         "label": "what it measures, up to 26 characters per line, 2 lines",
                         "line": "a closing line (optional)"}},
    "close": {"build": _close, "min": 3.0, "use": "the ending: a done, the name, ONE way to reach them",
              "slots": {"name": "the name, up to 16 characters per line, 2 lines (REQUIRED)",
                        "done": "a short confirmation for the pill, up to 24 characters, e.g. Order received (optional)",
                        "contact": "ONE contact line (phone OR web address), only if the client gave it (optional)"}},
}


# ── the plan: what the model writes ───────────────────────────────────────────

def blocks_text() -> str:
    lines = []
    for name, b in BLOCKS.items():
        slots = "; ".join(f'"{k}": {v}' for k, v in b["slots"].items())
        lines.append(f'  - "{name}" ({b["min"]:g}s or more) — {b["use"]}. Slots: {slots}')
    return "\n".join(lines)


def plan_instructions(request: str, brand: Optional[dict] = None, total_seconds: Optional[float] = None) -> str:
    brand_note = ""
    if brand:
        brand_note = (f"\nThe brand colours were measured from the client's own artwork: accent {brand.get('accent')}, "
                      f"deep {brand.get('deep')}. Use them as palette.accent and palette.accent2.")
    length = (f"\nThe client asked for {total_seconds:g} seconds; the seconds must add up to about that."
              if total_seconds else "\nNo length was given: plan 15 to 22 seconds, since a reel is read at a glance and "
                                    "each block needs its time.")
    return (
        "You are Prism's Senior Motion Designer. You do not draw this film: you CHOOSE from a library of finished, "
        "tested scenes and write their words.\n\nTHE BLOCKS:\n" + blocks_text() + "\n\n"
        "Rules:\n"
        "1. Four to six blocks in order. Open with \"title\" and end with \"close\". Use each of the others only where "
        "it serves this film; never the same block three times.\n"
        "2. Every slot is a width budget; write to it. Words only, no coordinates and no styling.\n"
        "3. NEVER INVENT WHO IT IS FROM: use only the names, phone numbers, web addresses, emails and handles the "
        "request gives. If it names no business or sender, leave the name slot to the occasion (\"Merry Christmas\") and "
        "leave contact empty. Never sign a film with Prism.\n"
        "4. Plain, concrete words in the client's language. A number or a promise beats an adjective.\n"
        "5. Pick a palette that fits the subject: bg_a, bg_b (a quiet light page), ink (near black), accent (the "
        "hero colour), accent2 (a deep supporting colour). Light backgrounds only.\n"
        '6. Choose project.motif to suit the subject: "band" (a colour band at the foot of every page; the default), "frame" '
        '(a thin border drawn around every page, formal and festive), "dots" (a row of dots above the type, light and playful) or "none".\n'
        + brand_note + length + "\n\nThe request:\n" + request.strip()[:4000] +
        "\n\nAnswer with ONLY this JSON in a ```json fenced block:\n"
        '{"project": {"motif": "band", "palette": {"bg_a": "#F6EFE2", "bg_b": "#EDE3D2", "ink": "#1C1713", "accent": "#C8161D", '
        '"accent2": "#14412F"}},\n "plan": [{"block": "title", "seconds": 3.4, "slots": {"headline": "…", "line": "…"}}, '
        '{"block": "close", "seconds": 4, "slots": {"name": "…"}}]}')


def parse_plan(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """(project, plan) out of a reply; ({}, []) when there is none."""
    t = text or ""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    raw = m.group(1) if m else t[t.find("{"): t.rfind("}") + 1] if "{" in t else ""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    plan = []
    for row in data.get("plan") or []:
        if isinstance(row, dict) and row.get("block") in BLOCKS:
            try:
                sec = float(row.get("seconds") or BLOCKS[row["block"]]["min"])
            except (TypeError, ValueError):
                sec = BLOCKS[row["block"]]["min"]
            plan.append({"block": row["block"], "seconds": max(BLOCKS[row["block"]]["min"], min(9.0, sec)),
                         "slots": row.get("slots") if isinstance(row.get("slots"), dict) else {}})
    return (data.get("project") if isinstance(data.get("project"), dict) else {}), plan


def plan_faults(plan: List[Dict[str, Any]]) -> List[str]:
    """Facts about a plan, sent back to the writer: required slots, order, repetition."""
    out: List[str] = []
    if not plan:
        return ["the plan has no usable blocks: use only the block names listed, each with its slots"]
    if plan[0]["block"] != "title":
        out.append('the film must open with a "title" block')
    if plan[-1]["block"] != "close":
        out.append('the film must end with a "close" block')
    for i, row in enumerate(plan):
        slots = row["slots"]
        need = {"title": "headline", "statement": "text", "list": "heading", "number": "figure", "close": "name"}[row["block"]]
        if not _clean(slots.get(need)):
            out.append(f'block {i + 1} ("{row["block"]}") has no "{need}"')
        if row["block"] == "list" and len([x for x in (slots.get("items") or []) if _clean(x)]) < 3:
            out.append(f'block {i + 1} ("list") needs at least 3 items')
        if i >= 2 and plan[i - 2]["block"] == plan[i - 1]["block"] == row["block"]:
            out.append(f'three "{row["block"]}" blocks in a row (blocks {i - 1}-{i + 1}); vary them')
    return out


# ── the composer ──────────────────────────────────────────────────────────────

def _span(nodes: List[Dict[str, Any]]) -> Tuple[float, float]:
    """Top and bottom (px) of what a block draws, from where it puts each line; (0, 0) when it draws nothing."""
    lo, hi = float("inf"), float("-inf")
    for n in nodes:
        if n.get("type") == "text":
            half = (str(n.get("content", "")).count("\n") + 1) * n["font_size"] * LINE_H / 2
        elif n.get("type") == "ui_pill":
            half = n.get("font_size", 28) * 1.2
        elif n.get("id") in ("logo", "logo_plate"):
            half = n["height"] / 2
        else:
            continue
        lo, hi = min(lo, n["position"][1] - half), max(hi, n["position"][1] + half)
    return (lo, hi) if hi > lo else (0.0, 0.0)


def _build_fitted(ctx: Ctx, name: str, slots: Dict[str, Any], seconds: float, share: Optional[float] = None):
    """Build a block; if it is taller than `share` of the frame, rebuild it smaller (type scales together)."""
    base_u = ctx.u
    share = ctx.share if share is None else share
    ctx.k, ctx.u = 1.0, base_u
    for _ in range(3):
        nodes, shot = BLOCKS[name]["build"](ctx, slots, seconds)
        lo, hi = _span(nodes)
        if hi - lo <= ctx.h * share:
            break
        f = ctx.h * share / (hi - lo)
        ctx.k *= f
        ctx.u = base_u * ctx.k
    ctx.k, ctx.u = 1.0, base_u
    return nodes, shot


def _card(ctx: Ctx, lo: float, hi: float) -> List[Dict[str, Any]]:
    """The one piece a direction puts under the type: a card sized to what the block drew."""
    c, pad = ctx.L["card"], ctx.m * 0.5
    pop = [{"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"}, {"channel": "scale", "from": -0.06, "to": 0, "easing": "back.out"}]
    out = [{"id": "card", "type": "shape_rect", "position": [round(ctx.w / 2), round((lo + hi) / 2)], "width": round(ctx.usable() + 2 * pad),
            "height": round(hi - lo + 2 * pad), "radius": c["radius"], "fill": c["fill"], "rotation": c["tilt"],
            "shadow_color": "rgba(0,0,0,0.16)", "shadow_blur": 44, "shadow_offset_y": 16, "animation": _enter(0.0, 0.6, *pop)}]
    if c["tape"]:
        out.append({"id": "tape", "type": "shape_rect", "position": [round(ctx.w / 2), round(lo - pad)], "width": round(ctx.u * 0.24),
                    "height": round(ctx.u * 0.055), "radius": 2, "fill": ctx.p["line"], "rotation": 3, "animation": _enter(0.25, 0.4, *pop)})
    return out


def _logo_from(assets: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The client's mark in an asset table ({name: {"path", "w", "h", "kind"}}): the entry named or marked "logo"."""
    for name, a in (assets or {}).items():
        if name == "logo" or (isinstance(a, dict) and a.get("kind") == "logo"):
            a = a if isinstance(a, dict) else {}
            return {"src": f"asset:{name}", "w": a.get("w"), "h": a.get("h")}
    return None


def compose_plan(project: Dict[str, Any], plan: List[Dict[str, Any]], width: int = 1080, height: int = 1920, fps: int = 30,
                 look: str = "", brand: Optional[Dict[str, Any]] = None, assets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The whole film. `look` is one of core.looks' eight directions ("" = the library's own), `brand`
    is {"accent", "deep"} from the client's kit and wins over the direction's colours, `assets` is the
    asset table: its logo, if it has one, is placed in the close."""
    palette = project.get("palette") if isinstance(project.get("palette"), dict) else {}
    ctx = Ctx(width, height, palette, look, brand)
    L = ctx.L
    ctx.logo = _logo_from(assets)
    motif = str(project.get("motif") or "band").lower()
    ctx.motif = L["motif"] or ("none" if L is not LOOKS[""] else motif if motif in MOTIFS else "band")
    if ctx.motif == "band":
        ctx.share, ctx.cy = 0.72, height * 0.43
    elif ctx.motif == "frame":
        ctx.share = 0.7
    scenes, clock = [], 0.0
    for i, row in enumerate(plan):
        ctx.paint(i)
        nodes, shot = _build_fitted(ctx, row["block"], row["slots"], row["seconds"])
        nodes[1:1] = (_card(ctx, *_span(nodes)) if L["card"] else []) + _motif_nodes(ctx, ctx.motif, i)
        for n in nodes:                                       # the direction's tempo: hard and fast, or slow
            enter = (n.get("animation") or {}).get("enter")
            if enter and L["pace"] != 1.0:
                enter["time"], enter["duration"] = round(enter["time"] * L["pace"], 2), round(enter["duration"] * L["pace"], 2)
                if "reveal_start" in n:
                    n["reveal_start"], n["reveal_duration"] = enter["time"], enter["duration"]
        scene: Dict[str, Any] = {"id": f"scene_{i}", "duration": round(row["seconds"], 2), "nodes": nodes, "shot": shot}
        if i:
            scene["transition_in"] = L["transitions"][(i - 1) % len(L["transitions"])]
        scenes.append(scene)
        clock += row["seconds"]
    ctx.paint(0)
    spec = {"project": {"width": width, "height": height, "fps": fps, "duration": round(clock, 2), "background": ctx.p["bg_a"],
                        "look": "paper", "palette": {k: ctx.p[k] for k in ("bg_a", "bg_b", "ink", "accent", "accent2")},
                        "type": dict(L["fonts"]), "sound": "ui", "motif": ctx.motif},
            "scenes": scenes, "_motion_profile": "blocks", "_plan": plan, "_look": look if look in LOOKS else ""}
    if assets:
        spec["_assets"] = assets
    return spec
