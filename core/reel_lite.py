"""A plain reel built from the script and the pictures alone, with no AI call.

Used when the art-direction step failed or was left out (no credits, a
provider error, the person chose the simpler version): the script is
already written and the pictures are already drawn, and the renderer needs no
AI, so the client still gets a film. It is deliberately simple: one picture,
a dark band, a kicker, a headline, a support line. No art direction.

The spec it returns is the same shape reel_web.render() takes from the design
step, plus `_lite: True` so the run can say what it is.
"""
from __future__ import annotations

import os
from html import escape

_CUTS = ("push", "zoom", "push-up")
_DEFAULT_BRAND = {"accent": "#2F6F4E", "deep": "#14213D", "ink": "#14213D",
                  "bg": "#F5EFE4"}

CSS = """
.lt{position:absolute;inset:0;font-family:'Helvetica Neue',Arial,'DejaVu Sans',sans-serif}
.lt .bg{position:absolute;inset:0;background-size:cover;background-position:center}
.lt .scrim{position:absolute;inset:0;background:linear-gradient(to top,rgba(10,16,40,.92) 0%,rgba(10,16,40,.7) 38%,rgba(10,16,40,0) 68%)}
.lt .txt{position:absolute;left:var(--safe-x);right:var(--safe-x);bottom:var(--safe-y);display:flex;flex-direction:column;gap:28px}
.lt .txt>*{animation:lt-in 800ms cubic-bezier(.16,1,.3,1) both}
.lt .txt>:nth-child(2){animation-delay:140ms}.lt .txt>:nth-child(3){animation-delay:280ms}
.lt .k{align-self:flex-start;background:var(--accent);color:#fff;font-size:36px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;padding:12px 24px;border-radius:8px}
.lt .h{font-size:104px;line-height:1.04;font-weight:800;color:#fff;letter-spacing:-.01em}
.lt .s{font-size:50px;line-height:1.3;color:rgba(255,255,255,.88)}
.lt .l{margin:0;padding:0 0 0 1em;font-size:50px;line-height:1.3;color:rgba(255,255,255,.88)}
.lt.paper{background:var(--bg)}
.lt.paper .hero{position:absolute;left:var(--safe-x);right:var(--safe-x);top:var(--safe-y);height:900px;background-size:contain;background-repeat:no-repeat;background-position:center}
.lt.paper .h{color:var(--ink)}.lt.paper .s,.lt.paper .l{color:var(--ink);opacity:.8}
.lt.end,.lt.solid{background:var(--deep)}
.lt.end .scrim{background:rgba(10,16,40,.82)}
.lt.end .txt{top:0;bottom:0;justify-content:center;align-items:center;text-align:center}
.lt.end .h{font-size:150px}
.lt.end .c{font-size:56px;font-weight:700;color:var(--accent);background:#fff;padding:16px 36px;border-radius:999px}
@keyframes lt-in{from{opacity:0;transform:translateY(44px)}to{opacity:1;transform:none}}
"""


def _pictures(paths) -> dict:
    """{name: {path, w, h}} for the pictures that open, in the order given."""
    table = {}
    try:
        from PIL import Image
    except Exception:
        return table
    for p in paths or []:
        p = str(p or "")
        if not os.path.isfile(p):
            continue
        try:
            with Image.open(p) as im:
                w, h = im.size
        except Exception:
            continue
        table[f"p{len(table) + 1}"] = {"path": p, "w": w, "h": h}
    return table


def _seconds(scene: dict) -> float:
    try:
        s = float(scene.get("seconds", 4))
    except (TypeError, ValueError):
        s = 4.0
    return min(12.0, max(1.5, s))


def _text(scene: dict, key: str) -> str:
    return escape(str(scene.get(key) or "").strip())


def build_lite_spec(sources, picture_paths, brand=None):
    """The simple reel for the first of `sources` that reads as a script, or
    None when none does (or the script has no scene with any words)."""
    from . import reel_web as web
    if isinstance(sources, str):
        sources = [sources]
    script = []
    for text in sources or []:
        try:
            got = web.read_script(text)
        except Exception:
            got = []
        got = [s for s in got if any(str(s.get(k) or "").strip()
                                     for k in ("kicker", "headline", "support"))]
        if got:
            script = got
            break
    if not script:
        return None

    table = _pictures(picture_paths)
    names = list(table)
    if not brand:
        try:
            from . import reel as _pillow
            brand = _pillow.sample_brand([v["path"] for v in table.values()])
        except Exception:
            brand = {}
    brand = {**_DEFAULT_BRAND, **{k: v for k, v in (brand or {}).items() if v}}

    def full_bleed(name):
        t = table[name]
        return t["w"] >= 500 and t["h"] / t["w"] >= 1.3

    scenes = []
    for i, sc in enumerate(script):
        role = str(sc.get("role") or "")
        name = names[i % len(names)] if names else None
        kicker = _text(sc, "kicker")
        kick = f"<div class='k'>{kicker}</div>" if kicker else ""
        head = f"<div class='h'>{_text(sc, 'headline')}</div>"
        sup = _text(sc, "support")
        sup = f"<div class='s'>{sup}</div>" if sup else ""
        items = sc.get("items")
        if isinstance(items, list) and items:
            sup += ("<ul class='l'>" + "".join(
                f"<li>{escape(str(x))}</li>" for x in items[:4]) + "</ul>")
        if role == "endcard":
            contact = _text(sc, "contact")
            contact = f"<div class='c'>{contact}</div>" if contact else ""
            bg = ""
            if name and full_bleed(name):
                bg = (f"<div class='bg' style=\"background-image:url('asset:{name}')\">"
                      "</div><div class='scrim'></div>")
            body = (f"<div class='lt end'>{bg}<div class='txt'>{head}{sup}{contact}"
                    "</div></div>")
        elif name and full_bleed(name):
            body = (f"<div class='lt'><div class='bg' style=\"background-image:"
                    f"url('asset:{name}')\"></div><div class='scrim'></div>"
                    f"<div class='txt'>{kick}{head}{sup}</div></div>")
        elif name:
            body = (f"<div class='lt paper'><div class='hero' style=\"background-image:"
                    f"url('asset:{name}')\"></div><div class='txt'>{kick}{head}{sup}"
                    "</div></div>")
        else:
            body = f"<div class='lt solid'><div class='txt'>{kick}{head}{sup}</div></div>"
        scenes.append({"seconds": _seconds(sc), "cut": _CUTS[i % len(_CUTS)],
                       "css": CSS, "html": body, "type": role})
    return {"design": {"name": "lite", "css": ""}, "scenes": scenes,
            "brand": brand, "_assets": table, "_lite": True}
