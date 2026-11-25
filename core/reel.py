"""
Prism — programmatic reel renderer (/reel)
───────────────────────────────────────────
Makes a finished vertical reel locally: Pillow draws every frame, FFmpeg
encodes them. No browser, no Node, no Remotion licence, no per-render fee,
no watermark, no daily credit cap.

Why this exists rather than a generative video tool:

  · Generative models (Google Flow, Kling, Runway) return a few seconds of
    LANDSCAPE footage and cannot render readable text. Every caption, price
    and phone number comes out garbled. They make b-roll, not deliverables.
  · A reel is 9:16, has the client's exact brand colours, and is mostly
    typography and motion graphics — all of which are drawing operations,
    not generation problems.

So this module owns the ASSEMBLY step: the part that turns shots and words
into something a client can post. Generated footage can be composited in
later; the text, brand marks and layout are always drawn here, where they
are exact and repeatable.

Same split as core/boq.py: code produces the artefact deterministically, an
AI stage only decides what goes in it (the scene spec).
"""
from __future__ import annotations
import math
import os
import re
import shutil
import subprocess

W, H, FPS = 1080, 1920, 30
SAFE_X, SAFE_Y = 90, 130

# Minimum type sizes for a 1080-wide frame. A phone is watched at arm's
# length for under a second per scene, so anything below these reads as
# decoration whatever it says — when in doubt the rule is larger, not smaller.
T_HEADLINE, T_SUPPORT, T_LABEL = 84, 44, 32

# Ordered by preference. The rupee sign is the deciding factor, not looks:
# Arial ships no ₹ glyph on macOS or older Windows, so every price in an
# Indian reel came out as a .notdef box. Helvetica and San Francisco carry it.
_FONT_CANDIDATES = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}
# Characters the reel must be able to draw. ₹ is not optional for an Indian
# product, and a font that can't draw it is the wrong font however good it
# looks — a box where the price should be is worse than a different typeface.
_REQUIRED_GLYPHS = "₹"
_FONT_CACHE: dict = {}


class ReelError(Exception):
    pass


def _has_glyphs(font, chars: str) -> bool:
    """True when the font draws real glyphs for `chars`.

    Detected by rendering against a private-use codepoint nothing defines: if
    a character comes out byte-identical to that, it is the .notdef box.
    Deliberately dependency-free — fontTools isn't guaranteed in a packaged
    build, and this runs a handful of times per render.
    """
    try:
        notdef = bytes(font.getmask("\ue000"))
        return all(bytes(font.getmask(c)) != notdef for c in chars)
    except Exception:
        return True   # can't tell — don't reject a font over it


def _font(weight: str, size: int):
    from PIL import ImageFont
    key = (weight, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    fallback = None
    for path in _FONT_CANDIDATES[weight]:
        if not os.path.exists(path):
            continue
        try:
            # .ttc collections: index 1 is the bold face in Helvetica's.
            index = 1 if (path.endswith(".ttc") and weight == "bold") else 0
            f = ImageFont.truetype(path, size, index=index)
        except Exception:
            continue
        if _has_glyphs(f, _REQUIRED_GLYPHS):
            _FONT_CACHE[key] = f
            return f
        fallback = fallback or f
    _FONT_CACHE[key] = fallback or ImageFont.load_default()
    return _FONT_CACHE[key]


def ffmpeg_path() -> str:
    """Where FFmpeg is. See core/ffmpeg.py for the four places looked in.

    Was `shutil.which("ffmpeg")` here and again in reel_web.py — which finds
    nothing on a Windows machine, because Windows does not come with it and
    nobody installs it by accident. The first Windows customer met a codec
    install guide instead of a video.
    """
    from . import ffmpeg as ffmpeg_tool
    found = ffmpeg_tool.locate()
    if not found:
        raise ReelError(ffmpeg_tool.MISSING)
    return found


# ── easing & helpers ────────────────────────────────────────────────────────

def cubic_bezier(x1: float, y1: float, x2: float, y2: float):
    """A CSS `cubic-bezier(x1,y1,x2,y2)` curve as a plain function.

    Same four control points a designer or a web animation would give you, so
    a timing spec can be used as-is instead of being eyeballed. Solved with
    Newton-Raphson and a bisection fallback, which is how browsers do it.
    """
    def bez(a, b, t):
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t * t + t ** 3

    def slope(a, b, t):
        return (3 * a * (1 - 4 * t + 3 * t * t)
                + 3 * b * (2 * t - 3 * t * t) + 3 * t * t)

    def curve(t: float) -> float:
        t = max(0.0, min(1.0, t))
        if t in (0.0, 1.0) or (x1 == y1 and x2 == y2):
            return t
        u = t
        for _ in range(8):                       # Newton-Raphson
            err = bez(x1, x2, u) - t
            if abs(err) < 1e-6:
                return bez(y1, y2, u)
            dv = slope(x1, x2, u)
            if abs(dv) < 1e-6:
                break
            u -= err / dv
        lo, hi, u = 0.0, 1.0, t                  # bisection fallback
        for _ in range(20):
            v = bez(x1, x2, u)
            if abs(v - t) < 1e-6:
                break
            if v > t:
                hi = u
            else:
                lo = u
            u = (lo + hi) / 2
        return bez(y1, y2, u)
    return curve


# The curves themselves, not the library that ships them. An entrance starts
# fast and decelerates into place; an exit starts slow and accelerates away —
# things arrive with momentum and leave with gravity.
EASE_ENTER = cubic_bezier(0.16, 1.0, 0.3, 1.0)     # crisp, no overshoot
EASE_INOUT = cubic_bezier(0.45, 0.0, 0.55, 1.0)    # editorial, symmetric
EASE_POP = cubic_bezier(0.34, 1.56, 0.64, 1.0)     # settles past the target
EASE_EXIT = cubic_bezier(0.55, 0.0, 1.0, 0.45)     # accelerates away


def ease(t: float) -> float:
    """The default entrance curve. Everything that fades or rises into place
    uses this, so the whole reel shares one sense of timing."""
    return EASE_ENTER(t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def fade_in(frame: int, start: int, length: int = 14) -> float:
    return ease((frame - start) / max(1, length))


def rise(frame: int, start: int, dist: int = 40, length: int = 18) -> int:
    return int(dist * (1 - ease((frame - start) / max(1, length))))


def hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def blend(fg, bg, alpha: float):
    a = max(0.0, min(1.0, alpha))
    return tuple(int(round(f * a + b * (1 - a))) for f, b in zip(fg, bg))


# ── brand ───────────────────────────────────────────────────────────────────

DEFAULT_BRAND = {
    "accent": "#68C04F",
    "deep": "#4B8A5D",
    "ink": "#1A1A1A",
    "grey": "#6B6B6B",
    "line": "#D8E6DA",
    "bg": "#FFFFFF",
    "paper": "#FAFBFA",
}


class Brand:
    def __init__(self, **kw):
        d = dict(DEFAULT_BRAND)
        d.update({k: v for k, v in kw.items() if v})
        for k, v in d.items():
            setattr(self, k, hex_rgb(v))


# ── drawing primitives ──────────────────────────────────────────────────────

def blueprint_grid(draw, brand, opacity=0.9, offset=0):
    """Engineering paper: fine 40px grid, coarse 200px grid. Deliberately
    low-contrast — it is a background, not a sci-fi HUD."""
    fine = blend(brand.line, brand.bg, 0.55 * opacity)
    coarse = blend(brand.line, brand.bg, 0.95 * opacity)
    for x in range(0, W + 1, 40):
        draw.line([(x, 0), (x, H)], fill=fine, width=1)
    for y in range(-40 + offset % 40, H + 1, 40):
        draw.line([(0, y), (W, y)], fill=fine, width=1)
    for x in range(0, W + 1, 200):
        draw.line([(x, 0), (x, H)], fill=coarse, width=2)
    for y in range(-200 + offset % 200, H + 1, 200):
        draw.line([(0, y), (W, y)], fill=coarse, width=2)


def dotted_wave(draw, brand, x, y, rows=7, cols=12, cell=34, dot_max=15,
                progress=1.0, color=None):
    """The client's halftone mark, rebuilt parametrically.

    Read off the business card: a fan of dots, dense and saturated at the
    lower-left, dissolving up and to the right. Generated rather than traced
    so it can be scaled, animated and reused as transition, watermark and hub.
    """
    color = color or brand.deep
    for r in range(rows):
        for c in range(cols):
            sweep = c / max(1, cols - 1)
            riser = r / max(1, rows - 1)
            fall = max(0.0, 1 - (sweep * 1.25 + riser * 0.55))
            if fall <= 0.02:
                continue
            local = max(0.0, min(1.0, (progress - sweep * 0.55) * 2.4))
            if local <= 0:
                continue
            size = dot_max * (0.35 + fall * 0.65) * local
            cx = x + c * cell + cell / 2
            cy = y + r * cell + cell / 2 - sweep * cell * 1.6
            fill = blend(color, brand.bg, 0.18 + fall * 0.82)
            draw.ellipse([cx - size / 2, cy - size / 2,
                          cx + size / 2, cy + size / 2], fill=fill)


def text(draw, xy, s, font, fill, anchor="la", max_width=None, line_gap=10):
    """Draw text, wrapping to max_width. Returns the height used, so callers
    can stack blocks without hard-coding positions."""
    if not max_width:
        draw.text(xy, s, font=font, fill=fill, anchor=anchor)
        bb = draw.textbbox(xy, s, font=font, anchor=anchor)
        return bb[3] - bb[1]
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    x, y = xy
    used = 0
    for ln in lines:
        draw.text((x, y + used), ln, font=font, fill=fill, anchor=anchor)
        bb = draw.textbbox((x, y + used), ln, font=font, anchor=anchor)
        used += (bb[3] - bb[1]) + line_gap
    return used


# ── thin-line icons ─────────────────────────────────────────────────────────
# Outlined only, rounded joins, one stroke weight — equipment, not app icons.

def _icon_server(d, x, y, s, col, w):
    for i in range(3):
        top = y + s * (0.13 + i * 0.28)
        d.rounded_rectangle([x + s * .12, top, x + s * .88, top + s * .2],
                            radius=s * .05, outline=col, width=w)
        d.ellipse([x + s * .2, top + s * .075, x + s * .26, top + s * .135], fill=col)


def _icon_storage(d, x, y, s, col, w):
    d.ellipse([x + s * .14, y + s * .1, x + s * .86, y + s * .34], outline=col, width=w)
    d.arc([x + s * .14, y + s * .38, x + s * .86, y + s * .62], 0, 180, fill=col, width=w)
    d.arc([x + s * .14, y + s * .62, x + s * .86, y + s * .86], 0, 180, fill=col, width=w)
    d.line([x + s * .14, y + s * .22, x + s * .14, y + s * .74], fill=col, width=w)
    d.line([x + s * .86, y + s * .22, x + s * .86, y + s * .74], fill=col, width=w)


def _icon_network(d, x, y, s, col, w):
    d.rounded_rectangle([x + s * .35, y + s * .12, x + s * .65, y + s * .3],
                        radius=s * .04, outline=col, width=w)
    for cx in (.1, .58):
        d.rounded_rectangle([x + s * cx, y + s * .68, x + s * (cx + .32), y + s * .86],
                            radius=s * .04, outline=col, width=w)
    d.line([x + s * .5, y + s * .3, x + s * .5, y + s * .52], fill=col, width=w)
    d.line([x + s * .26, y + s * .52, x + s * .74, y + s * .52], fill=col, width=w)
    d.line([x + s * .26, y + s * .52, x + s * .26, y + s * .68], fill=col, width=w)
    d.line([x + s * .74, y + s * .52, x + s * .74, y + s * .68], fill=col, width=w)


def _icon_security(d, x, y, s, col, w):
    pts = [(x + s * .5, y + s * .1), (x + s * .85, y + s * .26),
           (x + s * .85, y + s * .55), (x + s * .5, y + s * .9),
           (x + s * .15, y + s * .55), (x + s * .15, y + s * .26)]
    d.polygon(pts, outline=col, width=w)
    d.line([x + s * .35, y + s * .5, x + s * .46, y + s * .62], fill=col, width=w)
    d.line([x + s * .46, y + s * .62, x + s * .68, y + s * .38], fill=col, width=w)


def _icon_support(d, x, y, s, col, w):
    d.arc([x + s * .16, y + s * .18, x + s * .84, y + s * .78], 180, 360, fill=col, width=w)
    d.rounded_rectangle([x + s * .1, y + s * .48, x + s * .3, y + s * .78],
                        radius=s * .07, outline=col, width=w)
    d.rounded_rectangle([x + s * .7, y + s * .48, x + s * .9, y + s * .78],
                        radius=s * .07, outline=col, width=w)
    d.arc([x + s * .5, y + s * .62, x + s * .9, y + s * .96], 0, 90, fill=col, width=w)


def _icon_cctv(d, x, y, s, col, w):
    d.polygon([(x + s * .1, y + s * .34), (x + s * .72, y + s * .16),
               (x + s * .82, y + s * .42), (x + s * .2, y + s * .6)],
              outline=col, width=w)
    d.line([x + s * .3, y + s * .58, x + s * .3, y + s * .76], fill=col, width=w)
    d.line([x + s * .3, y + s * .76, x + s * .46, y + s * .84], fill=col, width=w)
    d.ellipse([x + s * .62, y + s * .58, x + s * .86, y + s * .82], outline=col, width=w)


def _icon_desktop(d, x, y, s, col, w):
    d.rounded_rectangle([x + s * .12, y + s * .18, x + s * .88, y + s * .66],
                        radius=s * .05, outline=col, width=w)
    d.line([x + s * .5, y + s * .66, x + s * .5, y + s * .8], fill=col, width=w)
    d.line([x + s * .34, y + s * .82, x + s * .66, y + s * .82], fill=col, width=w)


def _icon_fiber(d, x, y, s, col, w):
    d.ellipse([x + s * .32, y + s * .32, x + s * .68, y + s * .68], outline=col, width=w)
    d.arc([x + s * .32, y + s * .32, x + s * .68, y + s * .68], 180, 360, fill=col, width=w)
    d.line([x + s * .08, y + s * .5, x + s * .32, y + s * .5], fill=col, width=w)
    d.line([x + s * .68, y + s * .5, x + s * .92, y + s * .5], fill=col, width=w)


# ── beyond IT ───────────────────────────────────────────────────────────────
# The eight above are computer-room equipment, and for a while they were all
# there was — so a seed company's reel showed a database, a server rack and a
# support headset, and a writing stage had to invent a "conceptual mapping"
# (storage = seed bank, support = farmer) to use them at all. These cover the
# businesses that are not IT.

def _qbez(p0, p1, p2, n=18):
    """Points along a quadratic Bézier. Pillow draws arcs and polygons but has
    no curve primitive, and organic shapes — a leaf, a petal — are curves."""
    return [(p0[0] * (1 - t) ** 2 + 2 * p1[0] * (1 - t) * t + p2[0] * t * t,
             p0[1] * (1 - t) ** 2 + 2 * p1[1] * (1 - t) * t + p2[1] * t * t)
            for t in (i / n for i in range(n + 1))]


def _leaf_at(d, base, tip, bulge, col, w, midrib=True):
    """One leaf blade: two curves meeting at base and tip."""
    bx, by = base
    tx, ty = tip
    mx, my = (bx + tx) / 2, (by + ty) / 2
    nx, ny = -(ty - by), (tx - bx)          # normal to the midrib
    length = math.hypot(nx, ny) or 1
    nx, ny = nx / length * bulge, ny / length * bulge
    d.line(_qbez(base, (mx + nx, my + ny), tip), fill=col, width=w, joint="curve")
    d.line(_qbez(base, (mx - nx, my - ny), tip), fill=col, width=w, joint="curve")
    if midrib:
        d.line([base, tip], fill=col, width=max(1, w - 1))


def _icon_leaf(d, x, y, s, col, w):
    _leaf_at(d, (x + s * .16, y + s * .88), (x + s * .86, y + s * .14),
             s * .26, col, w)


def _icon_sprout(d, x, y, s, col, w):
    d.line([(x + s * .5, y + s * .94), (x + s * .5, y + s * .44)],
           fill=col, width=w)
    _leaf_at(d, (x + s * .5, y + s * .58), (x + s * .96, y + s * .22),
             s * .15, col, max(1, w - 1), midrib=False)
    _leaf_at(d, (x + s * .5, y + s * .7), (x + s * .06, y + s * .36),
             s * .15, col, max(1, w - 1), midrib=False)
    d.line([(x + s * .3, y + s * .94), (x + s * .7, y + s * .94)],
           fill=col, width=w)


def _icon_grain(d, x, y, s, col, w):
    """An ear of wheat: a stalk with grains angled up off it."""
    d.line([(x + s * .5, y + s * .96), (x + s * .5, y + s * .2)],
           fill=col, width=w)
    for i in range(4):
        gy = y + s * (0.26 + i * 0.16)
        _leaf_at(d, (x + s * .5, gy + s * .1), (x + s * .84, gy - s * .06),
                 s * .05, col, max(1, w - 1), midrib=False)
        _leaf_at(d, (x + s * .5, gy + s * .1), (x + s * .16, gy - s * .06),
                 s * .05, col, max(1, w - 1), midrib=False)


def _icon_drop(d, x, y, s, col, w):
    d.polygon([(x + s * .5, y + s * .1), (x + s * .8, y + s * .52),
               (x + s * .5, y + s * .9), (x + s * .2, y + s * .52)],
              outline=col, width=w)
    d.arc([x + s * .2, y + s * .38, x + s * .8, y + s * .9], 0, 180, fill=col, width=w)


def _icon_lab(d, x, y, s, col, w):
    d.line([x + s * .36, y + s * .12, x + s * .36, y + s * .44], fill=col, width=w)
    d.line([x + s * .64, y + s * .12, x + s * .64, y + s * .44], fill=col, width=w)
    d.line([x + s * .28, y + s * .12, x + s * .72, y + s * .12], fill=col, width=w)
    d.polygon([(x + s * .36, y + s * .44), (x + s * .14, y + s * .88),
               (x + s * .86, y + s * .88), (x + s * .64, y + s * .44)],
              outline=col, width=w)
    d.line([x + s * .26, y + s * .68, x + s * .74, y + s * .68], fill=col, width=w)


def _icon_factory(d, x, y, s, col, w):
    d.polygon([(x + s * .1, y + s * .88), (x + s * .1, y + s * .46),
               (x + s * .4, y + s * .62), (x + s * .4, y + s * .46),
               (x + s * .7, y + s * .62), (x + s * .7, y + s * .2),
               (x + s * .9, y + s * .2), (x + s * .9, y + s * .88)],
              outline=col, width=w)
    d.line([x + s * .28, y + s * .74, x + s * .28, y + s * .82], fill=col, width=w)
    d.line([x + s * .55, y + s * .74, x + s * .55, y + s * .82], fill=col, width=w)


def _icon_truck(d, x, y, s, col, w):
    d.rounded_rectangle([x + s * .06, y + s * .3, x + s * .58, y + s * .68],
                        radius=s * .04, outline=col, width=w)
    d.polygon([(x + s * .58, y + s * .42), (x + s * .78, y + s * .42),
               (x + s * .94, y + s * .56), (x + s * .94, y + s * .68),
               (x + s * .58, y + s * .68)], outline=col, width=w)
    d.ellipse([x + s * .16, y + s * .66, x + s * .34, y + s * .84], outline=col, width=w)
    d.ellipse([x + s * .66, y + s * .66, x + s * .84, y + s * .84], outline=col, width=w)


def _icon_shop(d, x, y, s, col, w):
    d.polygon([(x + s * .08, y + s * .34), (x + s * .2, y + s * .14),
               (x + s * .8, y + s * .14), (x + s * .92, y + s * .34)],
              outline=col, width=w)
    d.rounded_rectangle([x + s * .14, y + s * .34, x + s * .86, y + s * .88],
                        radius=s * .04, outline=col, width=w)
    d.rounded_rectangle([x + s * .38, y + s * .56, x + s * .62, y + s * .88],
                        radius=s * .03, outline=col, width=w)


def _icon_package(d, x, y, s, col, w):
    d.polygon([(x + s * .5, y + s * .1), (x + s * .9, y + s * .32),
               (x + s * .9, y + s * .74), (x + s * .5, y + s * .94),
               (x + s * .1, y + s * .74), (x + s * .1, y + s * .32)],
              outline=col, width=w)
    d.line([x + s * .1, y + s * .32, x + s * .5, y + s * .52], fill=col, width=w)
    d.line([x + s * .9, y + s * .32, x + s * .5, y + s * .52], fill=col, width=w)
    d.line([x + s * .5, y + s * .52, x + s * .5, y + s * .94], fill=col, width=w)


def _icon_growth(d, x, y, s, col, w):
    d.line([x + s * .1, y + s * .88, x + s * .9, y + s * .88], fill=col, width=w)
    for i, h in enumerate((0.3, 0.46, 0.64)):
        bx = x + s * (0.18 + i * 0.26)
        d.rounded_rectangle([bx, y + s * (0.88 - h), bx + s * .16, y + s * .88],
                            radius=s * .03, outline=col, width=w)
    d.line([x + s * .2, y + s * .42, x + s * .86, y + s * .14], fill=col, width=w)
    d.line([x + s * .86, y + s * .14, x + s * .64, y + s * .16], fill=col, width=w)
    d.line([x + s * .86, y + s * .14, x + s * .84, y + s * .36], fill=col, width=w)


def _icon_award(d, x, y, s, col, w):
    d.ellipse([x + s * .26, y + s * .1, x + s * .74, y + s * .58], outline=col, width=w)
    d.line([x + s * .36, y + s * .54, x + s * .28, y + s * .92], fill=col, width=w)
    d.line([x + s * .28, y + s * .92, x + s * .5, y + s * .78], fill=col, width=w)
    d.line([x + s * .64, y + s * .54, x + s * .72, y + s * .92], fill=col, width=w)
    d.line([x + s * .72, y + s * .92, x + s * .5, y + s * .78], fill=col, width=w)


def _icon_pin(d, x, y, s, col, w):
    d.arc([x + s * .2, y + s * .1, x + s * .8, y + s * .7], 180, 360, fill=col, width=w)
    d.line([x + s * .2, y + s * .4, x + s * .5, y + s * .92], fill=col, width=w)
    d.line([x + s * .8, y + s * .4, x + s * .5, y + s * .92], fill=col, width=w)
    d.ellipse([x + s * .4, y + s * .3, x + s * .6, y + s * .5], outline=col, width=w)


def _icon_clock(d, x, y, s, col, w):
    d.ellipse([x + s * .1, y + s * .1, x + s * .9, y + s * .9], outline=col, width=w)
    d.line([x + s * .5, y + s * .3, x + s * .5, y + s * .52], fill=col, width=w)
    d.line([x + s * .5, y + s * .52, x + s * .68, y + s * .62], fill=col, width=w)


def _icon_tools(d, x, y, s, col, w):
    """A cog. A spanner was tried first and read as a key at 72px — at icon
    size the silhouette has to be unmistakable, not accurate."""
    cx, cy, r = x + s * .5, y + s * .5, s * .3
    for i in range(8):
        a = math.radians(i * 45)
        d.line([(cx + math.cos(a) * r * .95, cy + math.sin(a) * r * .95),
                (cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45)],
               fill=col, width=int(w * 1.9))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=w)
    d.ellipse([cx - r * .38, cy - r * .38, cx + r * .38, cy + r * .38],
              outline=col, width=w)


ICONS = {
    # computing / infrastructure
    "server": _icon_server, "storage": _icon_storage, "network": _icon_network,
    "security": _icon_security, "support": _icon_support, "cctv": _icon_cctv,
    "desktop": _icon_desktop, "fiber": _icon_fiber,
    # anything else
    "leaf": _icon_leaf, "sprout": _icon_sprout, "grain": _icon_grain,
    "drop": _icon_drop, "lab": _icon_lab, "factory": _icon_factory,
    "truck": _icon_truck, "shop": _icon_shop, "package": _icon_package,
    "growth": _icon_growth, "award": _icon_award, "pin": _icon_pin,
    "clock": _icon_clock, "tools": _icon_tools,
}


def icon(draw, name, x, y, size, color, width=None):
    fn = ICONS.get(name)
    if not fn:
        return
    fn(draw, x, y, size, color, width or max(3, int(size * 0.045)))


def lower_third(draw, brand, y, heading, caption, frame, start):
    """White block, green accent bar, black heading, grey caption — the
    title system the brief specifies. Rises into a reserved slot."""
    a = fade_in(frame, start)
    if a <= 0:
        return
    dy = rise(frame, start, 60)
    x0, x1 = SAFE_X, W - SAFE_X
    hf = _fit(draw, heading, "bold", T_HEADLINE, x1 - x0 - 130, 52)
    cf = _font("regular", T_SUPPORT)
    pad = 36
    tx = x0 + 26 + 8 + 28
    # Every offset below is MEASURED, never a constant. The heading is fitted,
    # so its height varies by scene — a hard-coded 74px gap was right for the
    # 58px heading this box used to have and overlapped the caption the moment
    # the heading grew.
    head_h = int(hf.size * 1.18)
    cap_w = x1 - tx - pad
    line_h = int(cf.size * 1.28)
    cap_h = 0
    if caption:
        words, lines, cur = caption.split(), [], ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if draw.textlength(t, font=cf) <= cap_w:
                cur = t
            else:
                lines.append(cur); cur = wd
        if cur:
            lines.append(cur)
        cap_h = len(lines) * line_h
    box_h = pad * 2 + head_h + (cap_h + 14 if caption else 0)
    top = y + dy
    bg = blend((255, 255, 255), brand.bg, a)
    draw.rounded_rectangle([x0, top, x1, top + box_h], radius=18, fill=bg)
    bar = blend(brand.accent, bg, a)
    draw.rounded_rectangle([x0 + 26, top + pad, x0 + 34, top + box_h - pad],
                           radius=4, fill=bar)
    draw.text((tx, top + pad - 4), heading, font=hf,
              fill=blend(brand.ink, bg, a))
    if caption:
        text(draw, (tx, top + pad + head_h + 14), caption, cf,
             blend(brand.grey, bg, a), max_width=cap_w, line_gap=line_h - cf.size)


# ── scenes ──────────────────────────────────────────────────────────────────
# Each takes (draw, brand, spec, frame_in_scene) and paints one frame.
# A scene is pure: same frame number always gives the same pixels, which is
# what makes a render reproducible and reviewable.

def scene_statement(d, b, s, f):
    """Big type, one idea per line, revealed line by line."""
    blueprint_grid(d, b, 0.5, offset=int(f * 0.4))
    lines = s.get("lines", [])
    y = H // 2 - (len(lines) * 118 + 90) // 2
    # Nothing here was measured against the frame, so anything longer than the
    # examples ran off the right edge and was CLIPPED — silently, because
    # Pillow draws past the canvas without complaining. A three-word line fits
    # and a sentence does not, and the sentence is what a model writes.
    #
    # Two different repairs on purpose. A line is one idea and must stay on
    # one line, so it SHRINKS to fit; the tail is a sentence, so it WRAPS,
    # exactly as the same field already does in scene_list.
    for i, ln in enumerate(lines):
        a = fade_in(f, i * 12)
        if a <= 0:
            continue
        # Floor at the video minimum rather than at a comfortable headline
        # size: a comfortable floor means the line stops shrinking and then
        # runs off the frame. This is the second line of defence, not the
        # first — lint_spec rejects a statement line over 24 characters and
        # sends it back before anything is drawn, and 24 is well clear of the
        # ~30 where the floor is reached. Past about forty characters even
        # this cannot save it, which is precisely why the check upstream
        # exists.
        d.text((SAFE_X, y + i * 118 + rise(f, i * 12)), ln,
               font=_fit(d, ln, "bold", 100, W - SAFE_X * 2, T_LABEL),
               fill=blend(b.ink, b.bg, a))
    tail = s.get("tail")
    if tail:
        a = fade_in(f, len(lines) * 12 + 10)
        if a > 0:
            text(d, (SAFE_X,
                     y + len(lines) * 118 + 40 + rise(f, len(lines) * 12 + 10)),
                 tail, _font("regular", T_SUPPORT), blend(b.grey, b.bg, a),
                 max_width=W - SAFE_X * 2, line_gap=10)


def scene_brand(d, b, s, f):
    """Logo reveal: the dotted mark draws itself, then the name, then the
    capability stack."""
    prog = ease((f - 4) / 34)
    dotted_wave(d, b, W // 2 - 210, 430, rows=7, cols=12, cell=36, dot_max=16,
                progress=prog)
    a = fade_in(f, 24)
    if a > 0:
        dy = rise(f, 24)
        d.text((W // 2, 790 + dy), s.get("name", ""), font=_font("bold", 92),
               fill=blend(b.ink, b.bg, a), anchor="ma")
        d.text((W // 2, 900 + dy), s.get("tagline", ""), font=_font("regular", T_SUPPORT),
               fill=blend(b.grey, b.bg, a), anchor="ma")
    for i, item in enumerate(s.get("stack", [])):
        aa = fade_in(f, 46 + i * 9)
        if aa <= 0:
            continue
        dy = rise(f, 46 + i * 9, 24)
        y = 1030 + i * 106 + dy
        icon(d, item["icon"], W // 2 - 250, y, 72, blend(b.deep, b.bg, aa))
        d.text((W // 2 - 150, y + 14), item["label"], font=_font("regular", 46),
               fill=blend(b.ink, b.bg, aa))


def scene_pillar(d, b, s, f):
    """One trade: a row of equipment cards, then the title block."""
    blueprint_grid(d, b, 0.65, offset=int(f * 0.3))
    icons = s.get("icons", [])
    accent = s.get("accent_index", 0)
    n = len(icons)
    card = 268
    gap = 30
    total = n * card + (n - 1) * gap
    x0 = (W - total) // 2
    top = 560
    for i, name in enumerate(icons):
        a = fade_in(f, 8 + i * 9)
        if a <= 0:
            continue
        dy = rise(f, 8 + i * 9, 26)
        x = x0 + i * (card + gap)
        col = b.accent if i == accent else b.line
        d.rounded_rectangle([x, top + dy, x + card, top + card + dy], radius=26,
                            fill=blend((255, 255, 255), b.bg, a),
                            outline=blend(col, b.bg, a), width=3)
        icon(d, name, x + card * .17, top + dy + card * .17, card * .66,
             blend(b.accent if i == accent else b.deep, b.bg, a))
    lower_third(d, b, top + card + 90, s.get("heading", ""), s.get("caption"), f, 26)


def scene_hub(d, b, s, f):
    """Everything converges on one mark — the single-window message.

    The centre MUST be a visible object: an earlier version radiated lines
    from an invisible point and read as random crossings, not architecture.
    """
    blueprint_grid(d, b, 0.55, offset=int(f * 0.25))
    nodes = s.get("nodes", [])
    hub = (W // 2, 900)
    box_w, box_h = 210, 180
    slots = [(SAFE_X + 30, 480), (W - SAFE_X - 30 - box_w, 480),
             (SAFE_X + 30, 1230), (W - SAFE_X - 30 - box_w, 1230)]
    for i, node in enumerate(nodes[:4]):
        sx, sy = slots[i]
        cx, cy = sx + box_w / 2, sy + box_h / 2
        p = ease((f - (18 + i * 8)) / 22)
        if p > 0:
            d.line([cx, cy, lerp(cx, hub[0], p), lerp(cy, hub[1], p)],
                   fill=b.accent, width=3)
    hs = lerp(0.6, 1.0, ease(f / 20))
    r = 112 * hs
    d.ellipse([hub[0] - r, hub[1] - r, hub[0] + r, hub[1] + r],
              fill=b.bg, outline=b.accent, width=3)
    dotted_wave(d, b, hub[0] - 78, hub[1] - 62, rows=5, cols=8, cell=20,
                dot_max=11, progress=1.0)
    for i, node in enumerate(nodes[:4]):
        a = fade_in(f, 24 + i * 8)
        if a <= 0:
            continue
        sx, sy = slots[i]
        dy = rise(f, 24 + i * 8, 18)
        d.rounded_rectangle([sx, sy + dy, sx + box_w, sy + box_h + dy], radius=22,
                            fill=blend((255, 255, 255), b.bg, a),
                            outline=blend(b.line, b.bg, a), width=3)
        icon(d, node["icon"], sx + box_w * .26, sy + dy + box_h * .18, box_h * .62,
             blend(b.deep, b.bg, a))
        d.text((sx + box_w / 2, sy + box_h + dy + 22), node["label"],
               font=_font("regular", 34), fill=blend(b.ink, b.bg, a), anchor="ma")
    lower_third(d, b, 1500, s.get("heading", ""), s.get("caption"), f, 62)


def scene_endcard(d, b, s, f):
    """Final frame: mark, name, tagline, one line of contact. Nothing else.

    Every position here used to be a hard-coded y, chosen for a two-line
    tagline with a contact under it. Anything else sat wherever those numbers
    left it, and even the case they were chosen for was not centred: measured
    on a real reel the whole block ran y=441 to y=1270, leaving 441px above it
    and 650px below. It read as bottom-heavy — a big dead zone under the
    brand, on the one frame a viewer looks at longest.

    The gaps between the parts are still fixed, because they are a designed
    rhythm. What is computed is where the block as a whole starts, from what
    is actually in it.
    """
    lines = s.get("tagline_lines", [])
    # The mark's ink reaches ~30px above its origin (dot_max is a radius).
    mark_top = 470 - 30
    if s.get("contact"):
        block_bottom = 1230 + 40
    elif lines:
        block_bottom = 1030 + max(0, len(lines) - 1) * 58 + 50
    else:
        block_bottom = 900 + 96
    shift = int((H - (block_bottom - mark_top)) / 2 - mark_top)

    prog = ease(f / 32)
    dotted_wave(d, b, W // 2 - 216, 470 + shift, rows=8, cols=13, cell=36,
                dot_max=17, progress=prog)
    a = fade_in(f, 22)
    if a > 0:
        dy = rise(f, 22)
        d.text((W // 2, 900 + shift + dy), s.get("name", ""),
               font=_fit(d, s.get("name", ""), "bold", 96, W - SAFE_X * 2,
                         T_SUPPORT),
               fill=blend(b.ink, b.bg, a), anchor="ma")
    a2 = fade_in(f, 38)
    if a2 > 0:
        dy = rise(f, 38)
        for i, ln in enumerate(lines):
            d.text((W // 2, 1030 + shift + i * 58 + dy), ln,
                   font=_fit(d, ln, "bold", 46, W - SAFE_X * 2, T_LABEL),
                   fill=blend(b.accent, b.bg, a2), anchor="ma")
    a3 = fade_in(f, 56)
    if a3 > 0 and s.get("contact"):
        d.text((W // 2, 1230 + shift + rise(f, 56)), s["contact"],
               font=_font("regular", T_LABEL + 2), fill=blend(b.grey, b.bg, a3),
               anchor="ma")


def _fit(d, s: str, weight: str, size: int, max_width: int, floor: int = 22):
    """Largest size at or below `size` that fits `max_width`. Type that runs
    off the frame is the one thing a renderer must never do."""
    while size > floor and d.textlength(s, font=_font(weight, size)) > max_width:
        size -= 3
    return _font(weight, size)


def _series(s: dict) -> list[tuple[str, float]]:
    """The numeric points of a trend scene, junk entries dropped."""
    out = []
    for p in s.get("points") or []:
        if not isinstance(p, dict):
            continue
        try:
            out.append((str(p.get("label", "")), float(p["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def scene_trend(d, b, s, f):
    """A real chart drawn from real numbers.

    This exists because the alternative is what actually happened: with no
    chart scene, a writing stage handed over a caption reading "Animated
    trend: ₹61,000 Cr → ₹88,000 Cr …" and the renderer drew that sentence —
    a description of a chart instead of a chart. Numbers are exactly what
    code should own, so the spec carries the series and this draws it.
    """
    blueprint_grid(d, b, 0.5, offset=int(f * 0.2))
    heading = s.get("heading", "")
    pts = _series(s)
    x0, x1 = SAFE_X + 30, W - SAFE_X - 30
    top, bottom = 720, 1240
    if s.get("subheading"):
        # Each point's value sits in a chip 70px ABOVE it, and the chip is
        # filled with the background so a climbing series cannot be read
        # through its own numbers. The highest point sits at `top` — so with
        # the chart starting at 720 that chip lands at 598-658, straight
        # through a subheading drawn at 610, and paints out however much of
        # it the chip covers. On a real reel it rendered as
        # "25 min s spent reading one folder": the chip had eaten "Minute".
        #
        # The chart moves rather than the subheading, because the subheading
        # belongs with the heading above it and the chart has the room.
        top = 800

    a = fade_in(f, 2)
    if a > 0 and heading:
        d.text((SAFE_X, 520 + rise(f, 2)), heading,
               font=_fit(d, heading, "bold", 72, W - SAFE_X * 2, 44),
               fill=blend(b.ink, b.bg, a))
    if s.get("subheading"):
        a2 = fade_in(f, 10)
        if a2 > 0:
            d.text((SAFE_X, 610 + rise(f, 10)), s["subheading"],
                   font=_font("regular", T_SUPPORT), fill=blend(b.grey, b.bg, a2))

    if len(pts) >= 2:
        vals = [v for _, v in pts]
        hi, lo = max(vals), min(vals)
        # Floor the axis below the smallest value so a gentle rise still reads
        # as a rise — but never below zero, which would overstate the climb.
        base = max(0.0, lo - (hi - lo) * 0.45) if hi > lo else 0.0
        span = (hi - base) or 1.0
        n = len(pts)
        step = (x1 - x0) / (n - 1)
        xy = [(x0 + i * step, bottom - (v - base) / span * (bottom - top))
              for i, (_, v) in enumerate(pts)]

        d.line([(x0, bottom), (x1, bottom)], fill=b.line, width=3)

        prog = ease((f - 8) / 54)
        walked = prog * (n - 1)
        drawn = [xy[0]]
        for i in range(1, n):
            if walked >= i:
                drawn.append(xy[i])
            elif walked > i - 1:
                t = walked - (i - 1)
                px, py = xy[i - 1]
                qx, qy = xy[i]
                drawn.append((lerp(px, qx, t), lerp(py, qy, t)))
                break
            else:
                break
        if len(drawn) > 1:
            d.polygon(drawn + [(drawn[-1][0], bottom), (x0, bottom)],
                      fill=blend(b.accent, b.bg, 0.13))
            d.line(drawn, fill=b.accent, width=9, joint="curve")

        prefix, suffix = s.get("value_prefix", ""), s.get("value_suffix", "")
        labels = [f"{prefix}{v:,.0f}{suffix}".replace(",", ",") for _, v in pts]
        # Every point labelled if the gaps allow it; otherwise the ends and the
        # peak, which is the whole story anyway.
        vf = _font("bold", 40)
        while (max(d.textlength(t, font=vf) for t in labels) > step - 18
               and vf.size > T_LABEL):
            vf = _font("bold", vf.size - 2)
        sparse = max(d.textlength(t, font=vf) for t in labels) > step - 18
        keep = {0, n - 1, vals.index(hi)} if sparse else set(range(n))

        for i, ((label, _), (px, py)) in enumerate(zip(pts, xy)):
            if walked < i:
                continue
            # Fade from when the line REACHES the point, not from how much
            # line is left: the final point's progress stops the instant it
            # arrives, so tying opacity to it left the last — and most
            # important — value permanently half-faded.
            aa = fade_in(f, 8 + i * 54 / (n - 1), 12)
            last = i == n - 1
            r = 15 if last else 10
            d.ellipse([px - r, py - r, px + r, py + r],
                      fill=blend(b.accent if last else b.deep, b.bg, aa))
            if i in keep:
                lf = _font("bold", vf.size + (8 if last else 0))
                lw = d.textlength(labels[i], font=lf)
                # Keep the label inside the frame AND off the line: it sits in
                # a chip of background so a rising series can't be read
                # through its own value.
                tx = min(max(px, SAFE_X + lw / 2), W - SAFE_X - lw / 2)
                # Clearance is set by the steepest the line can be between two
                # points, so the chip clears it on both sides rather than
                # nicking it where the series climbs.
                ty = py - 70
                d.rounded_rectangle(
                    [tx - lw / 2 - 10, ty - lf.size - 4, tx + lw / 2 + 10, ty + 8],
                    radius=10, fill=b.bg)
                d.text((tx, ty), labels[i], font=lf,
                       fill=blend(b.ink if not last else b.deep, b.bg, aa),
                       anchor="ms")
            if label:
                d.text((px, bottom + 22), label, font=_font("regular", T_LABEL + 2),
                       fill=blend(b.grey, b.bg, aa), anchor="ma")

    note = s.get("note")
    if note:
        an = fade_in(f, 62)
        if an > 0:
            text(d, (SAFE_X, 1380), note, _font("regular", T_LABEL),
                 blend(b.grey, b.bg, an), max_width=W - SAFE_X * 2, line_gap=6)


def scene_figure(d, b, s, f):
    """One number, what it means, and the small print underneath.

    The footnote is the point of this scene. A spec once carried an asterisked
    figure — "₹10–50 Cr (FY2023–24)*" — whose disclaimer lived in a key no
    scene rendered, so the asterisk pointed at nothing. Anything a figure is
    legally required to carry has to have somewhere to land.
    """
    blueprint_grid(d, b, 0.45, offset=int(f * 0.25))
    value = str(s.get("value", ""))
    a = fade_in(f, 6)
    if a > 0 and value:
        d.text((W // 2, 760 + rise(f, 6, 50)), value,
               font=_fit(d, value, "bold", 168, W - SAFE_X * 2, 64),
               fill=blend(b.deep, b.bg, a), anchor="ma")

    label = s.get("label")
    if label:
        a2 = fade_in(f, 22)
        if a2 > 0:
            text(d, (W // 2, 990 + rise(f, 22, 30)), label,
                 _font("regular", T_SUPPORT + 4), blend(b.ink, b.bg, a2), anchor="ma",
                 max_width=W - SAFE_X * 2 - 80, line_gap=12)

    note = s.get("note")
    if note:
        a3 = fade_in(f, 40)
        if a3 > 0:
            d.line([(W // 2 - 120, 1180), (W // 2 + 120, 1180)],
                   fill=blend(b.line, b.bg, a3), width=3)
            text(d, (W // 2, 1220), note, _font("regular", T_LABEL),
                 blend(b.grey, b.bg, a3), anchor="ma",
                 max_width=W - SAFE_X * 2 - 60, line_gap=8)


def scene_list(d, b, s, f):
    """A plain typographic list — the people thanked, the things included.

    No icons on purpose. A list of who a company owes its year to should not
    have to find a picture for "farmers"; the previous way out was borrowing
    an unrelated icon, which read as a mistake.
    """
    blueprint_grid(d, b, 0.5, offset=int(f * 0.3))
    heading = s.get("heading", "")
    a = fade_in(f, 2)
    if a > 0 and heading:
        text(d, (SAFE_X, 470 + rise(f, 2)), heading,
             _fit(d, heading, "bold", 78, W - SAFE_X * 2, 46),
             blend(b.ink, b.bg, a), max_width=W - SAFE_X * 2, line_gap=6)

    items = [str(i) for i in (s.get("items") or [])][:6]
    y = 760
    for i, item in enumerate(items):
        aa = fade_in(f, 18 + i * 10)
        if aa <= 0:
            continue
        dy = rise(f, 18 + i * 10, 30)
        d.rounded_rectangle([SAFE_X, y + i * 118 + dy + 18,
                             SAFE_X + 10, y + i * 118 + dy + 74],
                            radius=5, fill=blend(b.accent, b.bg, aa))
        d.text((SAFE_X + 44, y + i * 118 + dy), item,
               font=_fit(d, item, "regular", 62, W - SAFE_X * 2 - 60, 38),
               fill=blend(b.ink, b.bg, aa))

    tail = s.get("tail")
    if tail:
        at = fade_in(f, 18 + len(items) * 10 + 12)
        if at > 0:
            text(d, (SAFE_X, y + len(items) * 118 + 60), tail,
                 _font("regular", T_SUPPORT), blend(b.grey, b.bg, at),
                 max_width=W - SAFE_X * 2, line_gap=10)


SCENES = {
    "statement": scene_statement,
    "brand": scene_brand,
    "pillar": scene_pillar,
    "hub": scene_hub,
    "trend": scene_trend,
    "figure": scene_figure,
    "list": scene_list,
    "endcard": scene_endcard,
}


# ── the dotted-wave wipe between scenes ─────────────────────────────────────

def wipe(d, b, f, length=12):
    """Primary transition from the brief. Covers the cut so scene changes
    read as designed rather than as a hard jump."""
    if f >= length:
        return
    p = ease(f / length)
    x = int(W * p)
    d.rectangle([x, 0, W, H], fill=b.bg)
    dotted_wave(d, b, x - 260, H // 2 - 300, rows=14, cols=8, cell=44,
                dot_max=20, progress=1.0, color=b.deep)


# ── transitions ─────────────────────────────────────────────────────────────
# A cut where both scenes play at once, not an effect painted over the start
# of the new one. Costs real time — the reel is SHORTER than the sum of its
# scenes by exactly the frames the transitions overlap.

TRANSITION_FRAMES = 12


def _transition_kind(prev_type: str, next_type: str) -> str:
    """Which cut suits these two scenes.

    Chosen from the scene types rather than at random, so the same spec always
    renders the same video — and so a chart is never yanked onto the screen:
    data needs a beat to be read, movement fights that.
    """
    if next_type in ("trend", "figure"):
        # Never yank a chart or a headline number onto the screen — it needs a
        # beat to be read, and movement fights that.
        return "fade"
    if next_type in ("statement", "list"):
        # Two text scenes crossfading superimpose two headlines for the length
        # of the cut, which reads as a mistake. Slide them past each other.
        return "slide"
    return "dots"


def _mix(a, b, p: float, kind: str, brand):
    """One composited frame p (0→1) of the way through a cut."""
    from PIL import Image, ImageDraw
    if kind == "fade":
        return Image.blend(a, b, p)
    if kind == "slide":
        # The outgoing scene drifts up a fraction of the distance the incoming
        # one travels — parallax, so the two read as depth rather than a swap.
        out = Image.new("RGB", (W, H), brand.bg)
        out.paste(a, (0, -int(H * 0.28 * p)))
        out.paste(b, (0, int(H * (1 - p))))
        return out
    out = a.copy()
    x = int(W * p)
    if x > 0:
        out.paste(b.crop((0, 0, x, H)), (0, 0))
    dotted_wave(ImageDraw.Draw(out), brand, x - 260, H // 2 - 300, rows=14,
                cols=8, cell=44, dot_max=20, progress=1.0, color=brand.deep)
    return out


# ── render ──────────────────────────────────────────────────────────────────

def render(spec: dict, out_path: str, on_progress=None) -> str:
    """Draw every frame with Pillow and pipe raw RGB straight into FFmpeg.

    Piping avoids writing thousands of PNGs to disk — a 40 s reel is 1,200
    frames, and the temp-file version was slower and messier for no gain.
    """
    from PIL import Image, ImageDraw

    exe = ffmpeg_path()
    # Said before the first frame is drawn rather than found at minute four
    # of the encode, where it costs the whole render and leaves a truncated
    # .mp4 that looks like a file. KNOWN_ISSUES #12.
    from . import ffmpeg as ffmpeg_tool
    no_room = ffmpeg_tool.check_space(out_path)
    if no_room:
        raise ReelError(no_room)
    brand = Brand(**(spec.get("brand") or {}))
    fps = int(spec.get("fps", FPS))
    scenes = spec.get("scenes", [])
    if not scenes:
        raise ReelError("The spec has no scenes.")

    plan = []
    for sc in scenes:
        kind = sc.get("type")
        if kind not in SCENES:
            raise ReelError(f"Unknown scene type {kind!r}. "
                            f"Known: {', '.join(sorted(SCENES))}")
        plan.append((SCENES[kind], sc, int(round(float(sc.get("seconds", 4)) * fps))))

    # Each cut overlaps its two scenes, so the reel runs shorter than the sum
    # of the requested seconds — capped at half of either neighbour so a short
    # scene is never swallowed whole by its own transition.
    cuts = []
    for i in range(len(plan) - 1):
        n = min(TRANSITION_FRAMES, plan[i][2] // 2, plan[i + 1][2] // 2)
        cuts.append((n, _transition_kind(plan[i][1].get("type", ""),
                                         plan[i + 1][1].get("type", ""))))
    total = sum(n for _, _, n in plan) - sum(n for n, _ in cuts)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def paint(fn, sc, f):
        img = Image.new("RGB", (W, H), brand.bg)
        fn(ImageDraw.Draw(img), brand, sc, f)
        return img

    done = 0
    try:
        for i, (fn, sc, count) in enumerate(plan):
            # Frames at the head were already played inside the previous cut.
            head = cuts[i - 1][0] if i else 0
            tail, kind = cuts[i] if i < len(cuts) else (0, "")
            for f in range(head, count - tail):
                proc.stdin.write(paint(fn, sc, f).tobytes())
                done += 1
                if on_progress and done % fps == 0:
                    on_progress(done, total)
            if tail:
                nfn, nsc, _ = plan[i + 1]
                for k in range(tail):
                    p = EASE_INOUT((k + 1) / tail)
                    frame = _mix(paint(fn, sc, count - tail + k),
                                 paint(nfn, nsc, k), p, kind, brand)
                    proc.stdin.write(frame.tobytes())
                    done += 1
                    if on_progress and done % fps == 0:
                        on_progress(done, total)
        proc.stdin.close()
    except BrokenPipeError:
        err = proc.stderr.read().decode("utf-8", "ignore")
        raise ReelError(f"FFmpeg stopped early: {err[:400]}")
    code = proc.wait()
    if code != 0:
        err = proc.stderr.read().decode("utf-8", "ignore")
        raise ReelError(f"FFmpeg failed (exit {code}): {err[:400]}")
    return out_path


def spec_instructions(requested: float | None = None) -> str:
    """What an AI stage must produce. The agent decides the WORDS and the
    running order; this module decides the pixels — so the layout can never
    be broken by a model having an off day.

    `requested`: the length the client asked for, in seconds (see
    core.length) — the pacing line then names it instead of the house band.

    Appended, not sent alone: the router plans this stage's task before it
    knows a local renderer is downstream, so the brief it writes routinely
    specifies its own deliverable — a document, a table, sections of its
    own. Naming "a handoff, a summary or a plan" isn't broad enough to void
    that; a writer handed a fully-specified alternate deliverable and told
    only that handoffs/summaries/plans don't count will produce the
    deliverable anyway. The override has to say so explicitly, or a stage
    fed a coherent competing brief will reasonably follow it instead of a
    schema that shows up as an afterthought at the bottom.
    """
    return (
        "OUTPUT FORMAT:\n"
        "Your reply is read by a program, not a person — this step feeds a "
        "renderer on the person's own machine. Reply with ONLY a JSON "
        "object describing "
        "the reel — no preamble, no commentary, no handoff section, no "
        "document, no markdown fences. Do NOT describe the JSON you would "
        "write; write it. The first character of your reply must be '{' "
        "and the last must be '}'. Shape:\n"
        '{"fps": 30, "scenes": [ … ]}\n\n'
        "SCENE TYPES — pick only the ones the story needs, in any order, and "
        "repeat any of them. There is no required running order.\n"
        '  statement  {"type":"statement","seconds":4,'
        '"lines":["Up to 3 short lines"],"tail":"one quieter line"}\n'
        # Placeholders, not a realistic sample: a named market with real-
        # looking figures reads to a careful model as a second brief
        # smuggled in at the bottom, and it refuses (see reel_web's script
        # prompt, where exactly that happened).
        '  figure     {"type":"figure","seconds":5,"value":"<the number>",'
        '"label":"<what the number is>",'
        '"note":"<source or disclaimer, if any>"}\n'
        '  trend      {"type":"trend","seconds":6,"heading":"<what is measured>",'
        '"subheading":"optional second line","value_prefix":"",'
        '"value_suffix":"","points":[{"label":"<year 1>","value":100},'
        '{"label":"<year 2>","value":140}],"note":"optional small print"}\n'
        '  list       {"type":"list","seconds":5,"heading":"<what these are>",'
        '"items":["<item 1>","<item 2>","<item 3>"],"tail":"optional closing line"}\n'
        '  pillar     {"type":"pillar","seconds":4,"heading":"under 34 chars",'
        '"caption":"under 90 chars","icons":["exactly","three","icons"],'
        '"accent_index":1}\n'
        '  hub        {"type":"hub","seconds":5,"heading":"…","caption":"…",'
        '"nodes":[{"icon":"server","label":"…"}]}   (exactly 4 nodes)\n'
        '  brand      {"type":"brand","seconds":5,"name":"Company Name",'
        '"tagline":"…","stack":[{"icon":"server","label":"…"}]}\n'
        '  endcard    {"type":"endcard","seconds":4,"name":"Company Name",'
        '"tagline_lines":["two short","lines"],"contact":"www.example.com"}\n\n'
        "WHEN TO USE WHICH — pick by what you are saying, not by habit:\n"
        "  · an idea, a claim, an opening hook           → statement\n"
        "  · ONE number that is the point of the scene   → figure\n"
        "  · two or more numbers that move over time     → trend\n"
        "  · people, places or things being named        → list\n"
        "  · three pieces of equipment or capability     → pillar\n"
        "  · four things converging on one offer         → hub\n"
        "  · the company introducing itself              → brand\n"
        "  · the last scene, always                      → endcard\n\n"

        # The RATIO, and deliberately not a running order.
        #
        # Two real reels from this same prompt: the one that worked had ONE
        # text scene out of seven, the one that failed had four out of six.
        # Nothing else about them differed, so the ratio is the lesson.
        #
        # A worked example WAS given here — the good reel's actual order,
        # statement/brand/pillar/pillar/pillar/hub/endcard — and it was copied
        # straight onto an unrelated business the same day, which is a worse
        # failure than the one it fixed. One template for every client is what
        # this whole renderer is trying not to be. So the constraint is stated
        # as a count, and the shapes below are several, contradictory, and
        # chosen by what the story is.
        "HOW MUCH OF IT MAY BE WORDS:\n"
        "At most TWO text-only scenes ('statement', 'list') in the whole "
        "reel, never two in a row, and at least two scenes built from "
        "'figure', 'trend', 'pillar', 'hub' or 'brand'. This is checked "
        "before anything is drawn and sent back if it is wrong. A reel that "
        "is mostly sentences could have been a post — the renderer draws "
        "diagrams, and that is the only reason to make a video of it.\n"
        "If a point feels like it has to be a sentence, it is usually three "
        "pieces of kit ('pillar'), one number ('figure'), four things "
        "converging ('hub'), or a number that moved ('trend'). Say it that "
        "way instead.\n\n"

        "THE RUNNING ORDER IS YOURS, AND IT SHOULD NOT LOOK LIKE ANYONE "
        "ELSE'S. Build it from what this business actually has to say. Some "
        "shapes that fit different stories:\n"
        "  · a firm with three divisions  — statement, brand, pillar, "
        "pillar, pillar, hub, endcard\n"
        "  · a year of results            — figure, trend, statement, "
        "figure, endcard\n"
        "  · one product, done properly   — statement, figure, pillar, "
        "figure, brand, endcard\n"
        "  · a supplier list or a network — brand, list, hub, figure, "
        "endcard\n"
        "  · something measured           — figure, pillar, figure, trend, "
        "endcard\n"
        "These are examples of DIFFERENT, not a menu to pick from. A "
        "machining shop and a pathology lab should not come out as the same "
        "seven scenes with the words changed. Repeat a scene type when the "
        "story genuinely repeats — three divisions really is three pillars — "
        "and not otherwise.\n\n"

        "PACING — a viewer reads three words in under two seconds:\n"
        "  statement 3–6s · figure 4–6.5s · trend 5–8s · list 4–7s\n"
        "  pillar 3–6s · hub 4–7s · brand 4–6s · endcard 3–5s\n"
        + (f"  4 to 7 scenes, about {requested:g} seconds in total — the client "
           f"asked for {requested:g}; land within 15% of it. A reel is not a "
           "film.\n\n" if requested
           else "  4 to 7 scenes, 20–35 seconds in total. A reel is not a film.\n\n")
        +
        "COPY LIMITS — these are what keeps type large enough to read:\n"
        "  heading under 34 characters · caption under 90 · statement at most "
        "3 lines · list at most 4 items · pillar exactly 3 icons · hub exactly "
        "4 nodes\n\n"
        "RULES\n"
        "· NUMBERS BELONG IN A SCENE THAT DRAWS THEM. A series of figures goes "
        "in 'trend' as real numeric points; a single headline figure goes in "
        "'figure'. Never write a number series into a caption or a label — the "
        "renderer will print your sentence instead of charting it.\n"
        "· NEVER DESCRIBE MOTION, STYLE OR LAYOUT. Do not write 'animated', "
        "'fade in', 'clean data card', 'logo reveal' or any visual direction. "
        "The renderer already animates every scene; such words get drawn as "
        "literal text.\n"
        "· Any disclaimer, source or asterisk footnote goes in the scene's "
        "'note' — that is the only field small print renders from. An asterisk "
        "with no 'note' points at nothing.\n"
        "· Only these keys exist. Anything else you invent is discarded "
        "silently, so put the words where they are read.\n"
        "· icons (pillar, hub, brand only) must come from this list — "
        + ", ".join(sorted(ICONS)) + ". Pick by the client's ACTUAL trade: a "
        "seed company uses lab, factory, truck, sprout, grain, drop; a shop "
        "uses shop, package, truck, award; an IT firm uses server, network, "
        "cctv, fiber. Never borrow an icon for what it vaguely suggests — a "
        "'storage' cylinder does not mean a seed bank and a 'support' headset "
        "does not mean a farmer; both just look like the wrong industry. If "
        "nothing on the list honestly fits, use 'statement', 'figure', 'trend' "
        "or 'list', which need no icon at all.\n"
        "· Never write a scene containing people; this renderer draws "
        "typography, data and equipment only.\n\n"
        "BEFORE YOU REPLY, CHECK YOUR OWN JSON:\n"
        "  1. Is every number that appears more than once inside a 'trend' as "
        "numeric points, rather than written into a sentence?\n"
        "  2. Does any string describe motion, styling or a shot? Delete it.\n"
        "  3. Does every '*' have a 'note' in the same scene?\n"
        "  4. Is the last scene an 'endcard'?\n"
        "  5. Do the seconds add up to between 20 and 35?\n"
        "  6. Is the first character '{' and the last '}', with nothing else "
        "in your reply?"
    )


# ── brand extraction & spec parsing ─────────────────────────────────────────

def _is_green(rgb) -> bool:
    r, g, b = rgb
    return g > r + 18 and g > b + 18


def _leading_colour(image_paths: list[str]):
    """The artwork's leading saturated colour, any hue, as (r, g, b) — or None
    when it is all greys, whites and blacks. Colours are grouped into twelve
    hue families first, so a logo's one red beats a photograph's scatter of
    many different ones; the family is ranked like the green reading, by how
    much there is of it and how vivid it is."""
    try:
        from PIL import Image
    except Exception:
        return None
    import colorsys
    from collections import Counter
    family: dict[int, float] = {}
    members: dict[int, Counter] = {}
    exact: dict[tuple, Counter] = {}
    for p in image_paths:
        try:
            im = Image.open(p).convert("RGB")
        except Exception:
            continue
        im.thumbnail((320, 320))
        for px in im.getdata():
            r, g, b = px
            sat = max(px) - min(px)
            if sat <= 70 or max(px) < 70:
                continue                          # grey, white, black or mud
            h = int(colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 12) % 12
            family[h] = family.get(h, 0.0) + sat ** 1.6
            near = (r & 0xF8, g & 0xF8, b & 0xF8)   # anti-aliased edges vote with their neighbours
            members.setdefault(h, Counter())[near] += 1
            exact.setdefault(near, Counter())[px] += 1
    if not family:
        return None
    best = max(family, key=family.get)
    near, _ = max(members[best].items(), key=lambda kv: kv[1] * (max(kv[0]) - min(kv[0])) ** 1.6)
    return exact[near].most_common(1)[0][0]


def sample_brand(image_paths: list[str]) -> dict:
    """Read the client's palette off their own artwork — a business card, a
    logo, a brochure — instead of asking anyone to type hex codes.

    Deterministic on purpose. A model asked to "match the brand" eyeballs a
    colour and gets it close; this reads the actual pixels. Same principle as
    measuring a drawing rather than describing it.
    """
    try:
        from PIL import Image
    except Exception:
        return {}
    from collections import Counter

    accent_votes, deep_votes = Counter(), Counter()
    for p in image_paths:
        try:
            im = Image.open(p).convert("RGB")
        except Exception:
            continue
        w, h = im.size
        if max(w, h) > 900:                      # speed: colour survives resizing
            im = im.resize((w // 2, h // 2))
        for px in im.getdata():
            r, g, b = px
            if g < 60 or g <= r + 18 or g <= b + 18:
                continue                          # not a green
            sat = g - max(r, b)
            # Only a genuinely saturated green counts as the brand colour.
            # Faded halftone print samples as a washed grey-green and would
            # otherwise be mistaken for the second brand tone.
            if sat > 45:
                accent_votes[px] += 1
            elif sat > 28 and g < 170:
                deep_votes[px] += 1

    out = {}
    # The green reading above was written for the first client, whose brand is
    # green; a red, blue or orange brand came back with no colours at all. So
    # every hue is read too, and green keeps its own tuned reading only when
    # green really is the artwork's leading colour.
    lead = _leading_colour(image_paths)
    if lead and not (accent_votes and _is_green(lead)):
        r, g, b = lead
        return {"accent": "#%02X%02X%02X" % lead,
                "deep": "#%02X%02X%02X" % (int(r * .72), int(g * .72), int(b * .72))}
    if accent_votes:
        # Rank by frequency AND vividness, not frequency alone. A logo mark
        # covers more pixels than a heading, but the heading usually carries
        # the truer brand colour — weighting saturation finds it without
        # ignoring how much of the artwork the colour actually occupies.
        def score(item):
            (r, g, b), n = item
            return n * (g - max(r, b)) ** 1.6
        best = max(accent_votes.items(), key=score)[0]
        out["accent"] = "#%02X%02X%02X" % best
    # A single strong colour is enough; derive the muted partner from it.
    # Deriving beats sampling here — a printed card's second tone is usually
    # just the first one faded, and sampling that gives a muddy grey.
    if "accent" in out:
        r, g, b = hex_rgb(out["accent"])
        out["deep"] = "#%02X%02X%02X" % (int(r * .72), int(g * .72), int(b * .72))
    return out


# How long each scene type can hold the screen. A viewer reads three words in
# under two seconds and a five-point chart in about six; anything longer is
# dead air on a feed. Timing is a rendering decision, so these are enforced
# rather than requested — one spec arrived with a 12-second logo card.
SCENE_SECONDS = {
    "statement": (3.0, 6.0), "figure": (4.0, 6.5), "trend": (5.0, 8.0),
    "list": (4.0, 7.0), "pillar": (3.0, 6.0), "hub": (4.0, 7.0),
    "brand": (4.0, 6.0), "endcard": (3.0, 5.0),
}
REEL_SECONDS = (18.0, 40.0)

# Scenes that are words on a background, and scenes this renderer builds a
# PICTURE for — an icon triad, a four-node hub, a bar chart, a stat card.
#
# The distinction is not stylistic. `statement` and `list` set type and stop;
# everything in _DRAWN puts geometry on the screen that a viewer reads as
# information rather than as reading. `endcard` is in neither on purpose: it
# is the ending, and every reel has exactly one.
_TEXT_ONLY = ("statement", "list")
_DRAWN = ("figure", "trend", "pillar", "hub", "brand")

# Words that describe a video instead of being one. Every one of these has
# turned up in a real spec, drawn literally on screen: "Animated trend: …",
# "clean data card", "logo reveal with gentle leaf-inspired motion".
# Phrases rather than bare words wherever the bare word is somebody's real
# business. "camera", "footage", "zoom" and "transition" are all ordinary copy
# for the CCTV and IT firms this tool was built for — flagging those would
# correct a client's own words back at them.
_MOTION_WORDS = (
    "animate", "animation", "fade in", "fade-in", "b-roll", "broll",
    "montage", "voiceover", "voice-over", "logo reveal", "data card",
    "lower third", "slow motion", "drone shot", "soundtrack",
    "background music", "cut to", "on-screen text", "visual note",
    "camera pans", "camera angle", "on camera", "stock footage",
    "zoom in", "zoom out", "slow zoom", "transition to", "cinematic shot",
    "typography-led", "kinetic typography", "scene with", "visual treatment",
    "colour palette", "color palette", "premium motion", "subtle texture",
    "text overlay", "title card", "end card with", "brand colours applied",
)

_TEXT_KEYS = ("heading", "subheading", "caption", "label", "tail", "name",
              "tagline", "contact", "note", "value")


def _scene_text(sc: dict):
    """Every human-readable string in a scene, with the key it came from."""
    for k in _TEXT_KEYS:
        v = sc.get(k)
        if isinstance(v, str) and v.strip():
            yield k, v
    for k in ("lines", "items", "tagline_lines"):
        for v in sc.get(k) or []:
            if isinstance(v, str) and v.strip():
                yield k, v
    for k in ("stack", "nodes"):
        for item in sc.get(k) or []:
            if isinstance(item, dict) and isinstance(item.get("label"), str):
                yield k, item["label"]


def lint_spec(spec: dict, requested: float | None = None) -> list[str]:
    """What is wrong with this spec, in words the writer can act on.

    `requested`: the length the client asked for, in seconds; the total is
    then held to ±15 % of it instead of the house REEL_SECONDS band.

    Not a schema check — parse_spec already guarantees the shape. This catches
    the mistakes that render as something embarrassing: a data series typed
    into a caption, an asterisk with no footnote, a scene describing the
    animation it wishes it had. Each complaint is phrased as an instruction so
    it can be handed straight back to the agent that wrote it.
    """
    import re
    out = []
    scenes = spec.get("scenes") or []
    if len(scenes) < 3:
        out.append(f"The reel has only {len(scenes)} scene(s) — write 4 to 7.")
    elif len(scenes) > 8:
        out.append(f"{len(scenes)} scenes is too many — cut it to 7 at most.")

    total = sum(float(sc.get("_seconds_asked", sc.get("seconds", 4)) or 4)
                for sc in scenes)
    if requested:
        from .length import bounds
        lo, hi = bounds(requested)
        if total < lo:
            out.append(f"The reel is only {total:.0f}s long — the client asked "
                       f"for {requested:g}s; lengthen the scenes that carry the "
                       "most, or add one, to land within 15% of that.")
        elif total > hi:
            out.append(f"The reel runs {total:.0f}s — the client asked for "
                       f"{requested:g}s; cut to within 15% of that by dropping "
                       "a scene, not by shortening every one.")
    elif total < REEL_SECONDS[0]:
        out.append(f"The reel is only {total:.0f}s long — aim for 20 to 35s.")
    elif total > REEL_SECONDS[1]:
        out.append(f"The reel runs {total:.0f}s — too long for a feed. "
                   "Cut it to 35s at most by dropping scenes, not by "
                   "shortening every one.")

    if scenes and scenes[-1].get("type") != "endcard":
        out.append("The last scene should be an 'endcard' so the reel ends on "
                   "the company name, not mid-thought.")

    # THE MIX, which is what separates a reel from a wall of text.
    #
    # Measured on two real reels drawn by this same renderer from this same
    # prompt. The one the customer called the best they had: seven scenes,
    # ONE of them text-only — statement, brand, pillar, pillar, pillar, hub,
    # endcard. The one they called the worst: six scenes, FOUR of them
    # text-only — statement, statement, list, statement, pillar, endcard.
    #
    # Nothing else differed. The prompt already says "pick by what you are
    # saying, not by habit", and habit still won, because `statement` is the
    # easiest scene to write and the renderer will accept any number of them.
    # A model asked to describe a business writes prose unless something
    # counts.
    kinds = [sc.get("type", "") for sc in scenes]
    talkers = [k for k in kinds if k in _TEXT_ONLY]
    drawn = [k for k in kinds if k in _DRAWN]
    if len(talkers) > 2:
        out.append(
            f"{len(talkers)} of the {len(scenes)} scenes are text-only "
            f"({', '.join(talkers)}) — that is a wall of text, not a reel. "
            "Keep at most two and turn the rest into scenes this renderer "
            "DRAWS: 'figure' for a single number, 'trend' for numbers that "
            "move, 'pillar' for three capabilities, 'hub' for four things "
            "converging, 'brand' for what the company is made of.")
    if scenes and len(drawn) < 2:
        out.append(
            "Only "
            + (f"{len(drawn)} scene is" if len(drawn) == 1 else "no scenes are")
            + " drawn as a diagram. A reel that is all words could have been "
            "a post. Build at least two scenes from 'figure', 'trend', "
            "'pillar', 'hub' or 'brand'.")
    for i in range(1, len(kinds)):
        if kinds[i] == kinds[i - 1] == "statement":
            out.append(
                f"Scenes {i} and {i + 1} are both 'statement' — two text "
                "screens in a row read as one long screen. Put a drawn scene "
                "between them, or merge them.")
            break

    for i, sc in enumerate(scenes, 1):
        kind = sc.get("type", "")
        where = f"Scene {i} ({kind})"
        for key, val in _scene_text(sc):
            low = val.lower()
            for word in _MOTION_WORDS:
                if word in low:
                    out.append(f"{where}: '{key}' contains \"{word}\" — that is "
                               "a description of a video, and it will be drawn "
                               "as text. Delete it; the renderer handles motion.")
                    break
            # Two or more big numbers in one string is a series, and a series
            # belongs in a chart. A phone number is not a series: "+91 98765
            # 43210" is two five-digit groups, and this sent every endcard
            # with an Indian number back to be charted (study of 11 Nov
            # 2026, finding R1). The endcard's own 'contact' line and any
            # phone-shaped string are left alone.
            phone_like = re.search(r"\+?\d[\d\s\-()]{7,}\d", val) is not None
            if (key not in ("value", "contact") and not phone_like
                    and len(re.findall(r"\d[\d,]{2,}", val)) >= 2):
                out.append(f"{where}: '{key}' has a series of numbers in it. "
                           "Move them into a 'trend' scene as real numeric "
                           "points — the renderer charts those.")
            if "*" in val and not sc.get("note"):
                out.append(f"{where}: '{key}' has an asterisk but the scene has "
                           "no 'note', so the footnote it points at never "
                           "appears. Add the 'note'.")

        lo, hi = SCENE_SECONDS.get(kind, (3.0, 8.0))
        # The asked-for value, not the clamped one — the writer has to see
        # the range it missed, or it will keep missing it.
        try:
            secs = float(sc.get("_seconds_asked", sc.get("seconds", 4)))
        except (TypeError, ValueError):
            secs = 4.0
        if not lo <= secs <= hi:
            out.append(f"{where}: {secs:g}s is outside the {lo:g}–{hi:g}s this "
                       "scene type holds attention for.")

        if len(sc.get("heading", "")) > 34:
            out.append(f"{where}: the heading is {len(sc['heading'])} characters "
                       "— keep it under 34 so it stays large on a phone.")
        if len(sc.get("caption", "")) > 90:
            out.append(f"{where}: the caption is {len(sc['caption'])} characters "
                       "— keep it under 90.")
        if kind == "statement" and len(sc.get("lines") or []) > 3:
            out.append(f"{where}: more than 3 lines. Split it into two scenes.")
        if kind == "statement":
            # A statement line is set at 100px bold. Around 24 characters is
            # what that size holds across a 1080 frame; past it the line
            # shrinks, and a "headline" small enough to fit forty characters
            # is not a headline any more. The good reel's lines were single
            # words — "Servers." "Networks." "Security."
            for ln in (sc.get("lines") or []):
                if len(str(ln)) > 24:
                    out.append(
                        f'{where}: the line "{str(ln)[:30]}…" is '
                        f"{len(str(ln))} characters. A statement line is set "
                        "huge, so keep each one under 24 — one or two words. "
                        "Put the sentence in 'tail', which is smaller and "
                        "wraps.")
        if kind == "pillar" and len(sc.get("icons") or []) != 3:
            out.append(f"{where}: 'pillar' needs exactly 3 icons.")
        if kind == "hub" and len(sc.get("nodes") or []) != 4:
            out.append(f"{where}: 'hub' needs exactly 4 nodes.")
        if kind == "trend" and len(_series(sc)) < 2:
            out.append(f"{where}: a 'trend' needs at least 2 numeric points "
                       'like {"label":"2024","value":81000}.')
        if kind == "list" and len(sc.get("items") or []) > 6:
            out.append(f"{where}: more than 6 items — a viewer reads 4.")
    return out


def has_spec(text: str) -> bool:
    """Cheap check for 'this reply contains something renderable'. Used to
    decide whether a stage needs asking again — never raises."""
    try:
        parse_spec(text)
        return True
    except Exception:
        return False


def _blocks(text: str, open_ch: str, close_ch: str) -> list[str]:
    """Every balanced {...} (or [...]) run in the text, LAST one first.

    Brace counting, not find/rfind: a reply that says "₹{amount}" before the
    spec, or adds a note after it, used to make the naive outermost slice span
    prose and fail as JSON. String-aware so a brace inside a headline doesn't
    throw the depth off. Last block first because an agent that explains
    itself and THEN emits the spec is the common case.
    """
    out, depth, start = [], 0, -1
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch and depth:
            depth -= 1
            if depth == 0:
                out.append(text[start:i + 1])
    return list(reversed(out))


def _escape_control_chars(block: str) -> str:
    """Escape the raw newlines a chat window wrapped INTO a JSON string.

    Diagnosed from a saved failure, and it is not the model's fault. ChatGPT
    wrote a perfectly good stylesheet; its page then soft-wrapped the very long
    Google Fonts URL, and the scrape turned that visual wrap into a real
    newline sitting inside the string:

        "css":"@import url('https://…&display=swap
        ');*{box-sizing:border-box}…"

    JSON forbids an unescaped control character inside a string, so the whole
    design was thrown away over a line break that only ever existed on screen.

    Walks the text tracking whether it is inside a string, so newlines BETWEEN
    members — the ordinary pretty-printing that is perfectly legal — are left
    exactly as they are. Only the ones trapped inside a value are escaped.
    """
    out, in_string, escaped = [], False, False
    for ch in block:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = in_string
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        out.append(ch)
    return "".join(out)


def _loosen(block: str) -> str:
    """Last-resort repair of the JSON faults models actually make: trailing
    commas, // comments, and curly quotes used as delimiters. Only ever tried
    AFTER a strict parse has failed, so it can never corrupt valid JSON.

    Also repairs the faults the chat WINDOW makes, which is a different list
    and took a saved failure to find — see _escape_control_chars.
    """
    import re
    block = re.sub(r"(?m)^\s*//.*$", "", block)
    block = block.replace("“", '"').replace("”", '"')
    block = re.sub(r",\s*([}\]])", r"\1", block)
    return _escape_control_chars(block)


# A URL the chat UI turned into a clickable link, sitting inside the JSON.
#
# Anchored on the link TEXT starting with http, because that is the only thing
# these UIs linkify, and a looser pattern would eat ordinary brackets.
_LINKIFIED = re.compile(r"\[(https?://[^\]]*)\]\([^)\s]*\)")


def _unlink_markdown(block: str) -> str:
    """Undo the chat window's auto-linking of URLs inside a reply.

    Diagnosed on the web renderer and then found to apply here too, which is
    obvious in hindsight: it is the same chat window. A URL that runs up
    against the text after it has the anchor swallow that text as its link
    label, and the swallowed text brings its own unbalanced braces — so the
    balanced-brace scan below counts braces that were never really there and
    cuts the spec in the wrong place.

    Taking the link TEXT rather than the href is the repair. The text is what
    the model typed; the href is the browser's percent-encoded guess at where
    it might point, with `)` rewritten as %29.
    """
    return _LINKIFIED.sub(lambda m: m.group(1), block)


def keep_unparsed(sources, what: str = "scene-spec") -> str:
    """Write a reply that would not parse to disk, and say where.

    This is the one place where the evidence is both essential and momentary.
    The text lives in a local variable, the run moves on, and all anybody is
    left with is "No JSON scene spec found in the agent's reply" against a
    chat tab that visibly contains JSON. Those two facts cannot both be
    investigated from a log line, and a reel that failed this way could not be
    diagnosed at all — the reply was already gone by the time anyone asked.

    Best-effort in every direction: a full disk must not turn a bad spec into
    a crash.
    """
    try:
        import time as _time
        from . import config
        folder = os.path.join(os.path.dirname(config.CONFIG_PATH), "logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(
            folder, f"{what}-that-would-not-parse-{int(_time.time())}.txt")
        with open(path, "w", encoding="utf-8") as f:
            for i, text in enumerate(sources, 1):
                f.write(f"───── candidate {i} "
                        f"({len(str(text))} chars) ─────\n{text}\n\n")
        return path
    except Exception:                                    # noqa: BLE001
        return ""


def first_spec(texts) -> tuple[dict | None, Exception | None]:
    """The newest capture that is actually a spec, and why none was.

    NEWEST first, not first-on-the-page and not longest. Chat DOM order is
    chronological, so the reply is at the end — and the page also holds the
    prompt Prism typed, which carries an EXAMPLE spec inside it. Taking
    texts[0] read whatever happened to be at the top of the tab; taking the
    longest could pick Prism's own echo of its instructions.
    """
    why = None
    for text in reversed([t for t in (texts or []) if str(t).strip()]):
        try:
            return parse_spec(text), None
        except Exception as e:                           # noqa: BLE001
            why = why or e
    return None, why


def parse_spec(text: str) -> dict:
    """Pull the scene spec out of an agent's reply.

    Scrapes carry markdown fences, a preamble, sometimes a trailing note, and
    sometimes the concatenated output of several stages — so hunt for the
    outermost balanced block that actually looks like a spec rather than
    trusting the whole response to be clean JSON.
    """
    import json
    if not text or not text.strip():
        raise ReelError("The agent returned nothing to render.")

    # Before the brace scan, not as a repair candidate after it: a swallowed
    # link label carries its own unbalanced braces, so _blocks would already
    # be counting the wrong ones.
    text = _unlink_markdown(text)
    spec, bad_json = None, None
    for block in _blocks(text, "{", "}"):
        for candidate in (block, _loosen(block)):
            try:
                got = json.loads(candidate)
            except Exception as e:
                bad_json = bad_json or e
                continue
            if isinstance(got, dict) and isinstance(got.get("scenes"), list) \
                    and got["scenes"]:
                spec = got
                break
        if spec:
            break

    # Some replies drop the wrapper and hand over the scene list on its own.
    if spec is None:
        for block in _blocks(text, "[", "]"):
            for candidate in (block, _loosen(block)):
                try:
                    got = json.loads(candidate)
                except Exception:
                    continue
                if isinstance(got, list) and got and all(
                        isinstance(x, dict) and "type" in x for x in got):
                    spec = {"scenes": got}
                    break
            if spec:
                break

    if spec is None:
        if bad_json:
            raise ReelError(f"The scene spec isn't valid JSON: {bad_json}")
        raise ReelError("No JSON scene spec found in the agent's reply.")

    # Drop anything the renderer can't draw rather than failing the whole run.
    clean, dropped = [], []
    for sc in spec["scenes"]:
        if isinstance(sc, dict) and sc.get("type") in SCENES:
            clean.append(sc)
        else:
            dropped.append(str(sc.get("type") if isinstance(sc, dict) else sc)[:24])
    if not clean:
        raise ReelError("None of the scenes are types this renderer knows.")
    # Pacing is a rendering decision, like layout and colour — so it is
    # corrected here rather than left to whatever the writer typed. lint_spec
    # still reports the original value so the writer learns the range.
    for sc in clean:
        lo, hi = SCENE_SECONDS.get(sc.get("type", ""), (3.0, 8.0))
        try:
            asked = float(sc.get("seconds", 4))
        except (TypeError, ValueError):
            asked = lo
        sc["_seconds_asked"] = asked
        sc["seconds"] = max(lo, min(hi, asked))
    spec["scenes"] = clean
    spec["_dropped"] = dropped
    return spec


def build_prompt(request: str, brand: dict, has_refs: bool) -> str:
    """The agent's whole job: choose the words and the running order. It never
    touches layout, colour or timing — those are code."""
    brand_note = ""
    if brand:
        brand_note = (
            f"\n\nThe brand colours have already been read from the client's own "
            f"artwork: accent {brand.get('accent')}, deep {brand.get('deep')}. "
            "They are already applied — do not change them, and do not put a "
            "brand block in your JSON.")
    ref_note = (
        "\n\nReference images of the client's material are attached. Use them "
        "to understand who this company is and how they present themselves — "
        "the wording, the tone, what they actually sell."
        if has_refs else "")
    return (
        "You are writing the script for a short vertical brand reel. A local "
        "renderer will draw it — you decide the WORDS and the running order, "
        "nothing else. Layout, colour, typography, motion and timing are "
        "already handled and are not yours to specify."
        f"\n\nWHAT THE CLIENT ASKED FOR:\n{request}"
        f"{ref_note}{brand_note}"
        "\n\nWrite copy a business owner would be happy to post: short, "
        "concrete, about what they actually sell. No marketing waffle, no "
        "invented claims, no statistics you cannot support.\n\n"
        + spec_instructions())
