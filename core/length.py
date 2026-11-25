"""
Prism — the length a client asked for
──────────────────────────────────────
"A 25-second reel", "30s", "about a minute": the number was in the request
and nothing read it. Reel planned to its own 20-35 s band and Motion to its
profile's, so a 25 s ask came back as 21.4 s and 15 s (study of 11 Nov
2026, finding M6). This reads it once; the prompt and the lint both use it.
Stdlib only.
"""
from __future__ import annotations

import re

_SECONDS = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:-|–)?\s*(?:s|sec|secs|second|seconds)\b", re.I)
_MINUTES = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*(?:-|–)?\s*(?:min|mins|minute|minutes)\b", re.I)
_A_MINUTE = re.compile(r"\b(?:a|one)[ -]minute\b", re.I)
_HALF_MINUTE = re.compile(r"\bhalf[ -](?:a[ -])?minute\b", re.I)


def requested_seconds(text: str) -> float | None:
    """The length in seconds the request names, or None when it names none.
    Only a length between 5 s and 3 min counts — a "2024" or a "500 units"
    is not one, and neither is a "2-second" transition inside a longer ask
    (the first match wins; a request names its length early)."""
    text = text or ""
    if _HALF_MINUTE.search(text):
        return 30.0
    if _A_MINUTE.search(text):
        return 60.0
    for rx, scale in ((_MINUTES, 60.0), (_SECONDS, 1.0)):
        for m in rx.finditer(text):
            secs = float(m.group(1)) * scale
            if 5.0 <= secs <= 180.0:
                return secs
    return None


def bounds(requested: float, slack: float = 0.15) -> tuple[float, float]:
    """The band a plan must land in: the ask ± 15 %."""
    return requested * (1.0 - slack), requested * (1.0 + slack)
