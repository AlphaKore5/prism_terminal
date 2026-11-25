"""
Prism — the frame a client asked for
─────────────────────────────────────
"A 16:9 launch video", "a square post", "a vertical reel". Motion planned every
film for 1080x1920 whatever was asked, and its prompts said so in several
places. This reads the shape from the request; None means the client named no
shape and the default (vertical) stands. Stdlib only.
"""
from __future__ import annotations

import re

# (label, width, height)
FRAMES = {
    "16:9": ("landscape 16:9", 1920, 1080),
    "9:16": ("vertical 9:16", 1080, 1920),
    "1:1": ("square 1:1", 1080, 1080),
    "4:5": ("portrait 4:5", 1080, 1350),
}

_RATIO = re.compile(r"(?<![\d:])(16\s*:\s*9|9\s*:\s*16|1\s*:\s*1|4\s*:\s*5)(?![\d:])")
_WORDS = (
    ("16:9", re.compile(r"\b(landscape|widescreen|youtube|desktop|horizontal|16x9)\b", re.I)),
    ("1:1", re.compile(r"\b(square|instagram post|1x1)\b", re.I)),
    ("4:5", re.compile(r"\b(4x5|feed post|portrait post)\b", re.I)),
    ("9:16", re.compile(r"\b(vertical|reel|reels|story|stories|tiktok|shorts|9x16|portrait)\b", re.I)),
)


def requested_frame(text: str) -> tuple[str, int, int] | None:
    """(label, width, height) the request names, or None. An explicit ratio
    ("16:9") beats a word; among words, the first one in the order above
    that appears."""
    text = text or ""
    m = _RATIO.search(text)
    if m:
        return FRAMES[re.sub(r"\s+", "", m.group(1))]
    for key, rx in _WORDS:
        if rx.search(text):
            return FRAMES[key]
    return None
