"""
Prism — the client's brand kit
───────────────────────────────
Until 13 Nov 2026 "the brand" a film was built to was two hex colours, read
off whatever was attached to that one run, and Prism Motion was not told even
those. A business is recognised by more than a colour: its name as it writes
it, its line, its typefaces, its mark, how it speaks, how to reach it. Tools
that make on-brand video (Canva's Brand Kit, Higgsfield's brand kits,
HyperFrames' DESIGN.md) all keep that once and apply it to every film; so
does this.

The kit is one dict, kept in config under "brand_kit" so it outlives a run:

    name, tagline            how the business writes itself
    accent, deep             #RRGGBB — typed colours win over measured ones
    display_font, body_font  Google Fonts family names
    tone                     a few words: "warm, plain-spoken"
    contact                  lines that may be shown: phone, site, handle

Every field is optional and nothing is invented to fill a gap. Stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

TEXT_FIELDS = ("name", "tagline", "display_font", "body_font", "tone")
COLOUR_FIELDS = ("accent", "deep")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean(raw: Any) -> Dict[str, Any]:
    """A kit with only well-formed fields left in it; {} for anything else."""
    if not isinstance(raw, dict):
        return {}
    kit: Dict[str, Any] = {}
    for k in TEXT_FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            kit[k] = " ".join(v.split())[:120]
    for k in COLOUR_FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and _HEX.match(v.strip()):
            kit[k] = v.strip().upper()
    contact = raw.get("contact")
    if isinstance(contact, str):
        contact = [contact]
    if isinstance(contact, (list, tuple)):
        lines = [" ".join(c.split())[:120] for c in contact if isinstance(c, str) and c.strip()]
        if lines:
            kit["contact"] = lines[:4]
    return kit


def from_config(cfg: Any) -> Dict[str, Any]:
    return clean(cfg.get("brand_kit")) if isinstance(cfg, dict) else {}


def colours(kit: Dict[str, Any], measured: Dict[str, Any] | None = None) -> Dict[str, str]:
    """The accent/deep pair a film is built to: what the client typed, else
    what was measured off their artwork. A typed accent with no typed deep
    gets a deep derived from it, never the measured one from another colour."""
    out = {k: v for k, v in (measured or {}).items() if k in COLOUR_FIELDS and isinstance(v, str) and v}
    if kit.get("accent"):
        out["accent"] = kit["accent"]
        if kit.get("deep"):
            out["deep"] = kit["deep"]
        else:
            r, g, b = (int(kit["accent"][i:i + 2], 16) for i in (1, 3, 5))
            out["deep"] = "#%02X%02X%02X" % (int(r * .72), int(g * .72), int(b * .72))
    return out


def block(kit: Dict[str, Any], has_logo: bool = False) -> str:
    """The identity as the designer is told it, or "" when nothing is known.
    Colours are stated by the callers' own colour notes, not repeated here."""
    lines: List[str] = []
    if kit.get("name"):
        lines.append(f'· Name, written exactly like this wherever it appears: "{kit["name"]}".')
    if kit.get("tagline"):
        lines.append(f'· Their line: "{kit["tagline"]}". Use it as written or not at all; do not write another.')
    fonts = [kit[k] for k in ("display_font", "body_font") if kit.get(k)]
    if fonts:
        lines.append("· Their typefaces (Google Fonts): "
                     + (f'{kit["display_font"]} for headlines' if kit.get("display_font") else "")
                     + (" and " if len(fonts) == 2 else "")
                     + (f'{kit["body_font"]} for everything else' if kit.get("body_font") else "")
                     + ". These replace any typeface an art direction suggests.")
    if kit.get("tone"):
        lines.append(f'· How they speak: {kit["tone"]}.')
    if kit.get("contact"):
        lines.append("· The only contact details that may be shown, exactly as written: "
                     + " · ".join(kit["contact"]) + ".")
    if has_logo:
        lines.append("· Their real mark is in the artwork list as `logo`. Show it whole, on a ground it reads against, "
                     "small in the opening scene and large in the closing one — never only at the end; "
                     "never redraw it from shapes or text.")
    if not lines:
        return ""
    return ("\n\nTHE CLIENT'S IDENTITY — given, not yours to change. A viewer who knows this business "
            "must recognise it in the film:\n" + "\n".join(lines))


def corpus(kit: Dict[str, Any]) -> str:
    """What the kit supplies that a scene may then show (see motion.grounding)."""
    return "\n".join([kit.get("name", ""), kit.get("tagline", "")] + list(kit.get("contact") or [])).strip()


def missing_logo(spec_text: str, has_logo: bool) -> str:
    """The fault when the client's mark was supplied and no scene places it."""
    if has_logo and not re.search(r"asset:logo\b|\"(?:asset|src|name)\"\s*:\s*\"logo\"", spec_text or ""):
        return ("the client's own mark is in the artwork list as `logo` and no scene shows it — "
                "the closing scene must place it")
    return ""
