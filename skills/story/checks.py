"""Faults a program can see in a narrative piece.

Almost all of storytelling is taste, and a checker has no taste. What it
can see is the two failures that are not taste at all: the opening that
announces the industry instead of starting, and the piece with no
checkable specifics in it — which is what a fabricated story looks like
from the outside.
"""
from __future__ import annotations

import re

DEAD_OPENERS = re.compile(
    r"^\W*(?:in (?:today'?s?|the)\s+(?:fast[- ]paced|modern|competitive|"
    r"digital)\b|in an era\b|since the dawn\b|founded in \d{4}\b|"
    r"have you ever\b|imagine (?:a|if|that)\b|it is (?:no|a) secret\b|"
    r"picture this\b|in the world of\b|when it comes to\b)", re.IGNORECASE)
BANNED = re.compile(r"\bjourney\b|\bpassion(?:ate)?\b|\bgame[- ]chang\w+|"
                    r"\btransformative\b|\bsynerg\w+|\bleverag\w+|"
                    r"\bcutting[- ]edge\b|\bworld[- ]class\b|\bunlock\w*\s+"
                    r"(?:the\s+)?potential|\bempower\w*\b|\bthrive[ds]?\b",
                    re.IGNORECASE)
MORAL = re.compile(r"and that is why\b|which is why we\b|the lesson\b|"
                   r"this story shows\b|goes to show\b|at the end of the day\b|"
                   r"teaches us\b|reminds us\b", re.IGNORECASE)
SPEECH = re.compile(r"[\"“][^\"“”]{8,}[\"”]|\bsaid\b|\btold\b|\basked\b",
                    re.IGNORECASE)
# Something a reader could in principle check. A year is not the tail of a
# resolution ('1080x1920'); '%' is followed by a space, not a word boundary;
# a plan's own numbers (seconds, px, bpm, fps, frames, 16:00) are checkable.
SPECIFIC = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:kg|tonnes?|tons?|mm|cm|km|m|hours?|hrs?|"
    r"days?|weeks?|months?|years?|crore|lakh|units?|pieces?|nos\.?|"
    r"seconds?|secs?|s|ms|px|bpm|fps|frames?)\b|"
    r"\b\d[\d,]*(?:\.\d+)?\s*%(?!\w)|\b\d{1,2}:\d{2}\b|"
    r"(?:₹|\$|€|£|\bRs\.?)\s?[\d,]+|"
    r"(?<![x×\d])(?<![x×] )\b(?:19|20)\d{2}\b(?!\s*[x×]\s*\d)|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b\d{1,2}\s*(?:am|pm)\b", re.IGNORECASE)
CTA = re.compile(r"\bcontact us\b|\bget in touch\b|\blearn more\b|"
                 r"\bvisit our\b|\bbook a (?:demo|call)\b|\bsign up\b",
                 re.IGNORECASE)

MIN_SPECIFICS = 3
MIN_WORDS = 120


def faults(text: str, context: dict) -> list:
    text = (text or "").strip()
    words = text.split()
    if len(words) < MIN_WORDS:
        return [f"At {len(words)} words this is too short to be a story. It "
                "needs someone who wants something, something in the way, a "
                "turn, and what changed."]

    out = []
    # The opening line, past any heading.
    first = ""
    for line in text.splitlines():
        s = line.strip().lstrip("#*_ ").strip()
        if len(s.split()) >= 4:
            first = s
            break
    if DEAD_OPENERS.search(first):
        out.append("The opening is a stock phrase, not a moment: "
                   f"'{first[:70]}…'. Start inside something happening — a "
                   "scene, a number, a line someone said.")

    specifics = SPECIFIC.findall(text)
    if len(specifics) < MIN_SPECIFICS:
        out.append(f"Only {len(specifics)} checkable specific(s) in the whole "
                   "piece. A story is carried by details someone could "
                   "verify — a figure, a date, a day, a part, a place. Add "
                   "at least three real ones, or mark clearly where the "
                   "person must supply them.")

    if not SPEECH.search(text):
        out.append("Nobody speaks. Quote at least one real line — it is what "
                   "makes the piece read as true rather than composed.")

    banned = sorted({m.group(0).strip().lower() for m in BANNED.finditer(text)})
    if banned:
        out.append("Stock business words used: " + ", ".join(banned[:6])
                   + ". Replace each with what actually happened.")

    moral = sorted({m.group(0).strip().lower() for m in MORAL.finditer(text)})
    if moral:
        out.append("The piece explains its own point (" + ", ".join(moral[:3])
                   + "). Cut the moral; if the point needs stating, the story "
                     "has not done it.")

    if CTA.search(text) and not context.get("wants_cta"):
        out.append("A call to action was added. A story ends on what changed, "
                   "not on an invitation, unless one was asked for.")
    return out
