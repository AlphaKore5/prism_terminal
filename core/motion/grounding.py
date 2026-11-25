"""
Prism Motion Graphics — words the client never gave
────────────────────────────────────────────────────
A Christmas film written on 13 Nov 2026 signed itself "from all of us at
Prism": the brief named no sender, so the model reached for the tool's own
name. A phone number, address or web link invented the same way would be
worse: it ships on a client's reel. This reads the text a scene shows and
reports anything that identifies a business or a way to reach one that is not
in the request, the script, the brand or the artwork list. Facts a program
can check, not taste. Stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|in|org|io|co|net|app|shop|store)(?:/\S*)?\b", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_.]{3,}")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d")
# "from all of us at Acme", "by Acme Studio", "from the team at Acme"
_SIGNER = re.compile(
    r"\b(?:from\s+(?:all\s+of\s+us|the\s+team|everyone|us)\s+at|from\s+the\s+team\s+at|by|presented\s+by|"
    r"with\s+love\s+from|brought\s+to\s+you\s+by)\s+((?:[A-Z][\w&.'-]*)(?:\s+[A-Z][\w&.'-]*){0,3})")
# The tool's own name is never the client's.
_TOOL_NAMES = ("prism",)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _walk(nodes: Iterable[Any]):
    stack = [n for n in nodes if isinstance(n, dict)]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in n.get("children") or [] if isinstance(c, dict))


def shown_text(scene: Dict[str, Any]) -> List[str]:
    """Every string a viewer can read in the scene."""
    out: List[str] = []
    for n in _walk(scene.get("nodes") or []):
        for key in ("content", "text", "label", "sub", "title", "caption", "value"):
            v = n.get(key)
            if isinstance(v, str) and v.strip():
                out.append(v)
        for item in n.get("items") or []:
            if isinstance(item, dict):
                out.extend(str(item[k]) for k in ("label", "sub", "text") if isinstance(item.get(k), str))
            elif isinstance(item, str):
                out.append(item)
        for run in n.get("runs") or []:
            if isinstance(run, dict) and isinstance(run.get("text"), str):
                out.append(run["text"])
    return out


def allowed_corpus(*sources: Any) -> str:
    """Lower-cased text of everything the client supplied (request, script,
    brand values, artwork names), flattened."""
    parts: List[str] = []

    def add(x: Any):
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                add(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                add(v)

    for s in sources:
        add(s)
    return "\n".join(parts).lower()


def grounding_faults(scene: Dict[str, Any], corpus: str) -> List[str]:
    """What the scene shows that identifies someone and is not in `corpus`
    (build it with allowed_corpus). Returns [] when `corpus` is empty: with
    nothing known about the client there is nothing to compare against."""
    if not corpus.strip():
        return []
    corpus_digits = _digits(corpus)
    out: List[str] = []
    seen: set = set()

    def say(msg: str):
        if msg not in seen:
            seen.add(msg)
            out.append(msg)

    for text in shown_text(scene):
        low = text.lower()
        for name in _TOOL_NAMES:
            if re.search(rf"\b{name}\b", low) and not re.search(rf"\b{name}\b", corpus):
                say(f'the scene says "{name.title()}", which is this tool\'s name, not the client\'s — the client '
                    "gave no such name; leave the sender out or say only what the brief says")
        for m in _SIGNER.finditer(text):
            who = m.group(1).strip()
            if who.lower() not in corpus:
                say(f'the scene signs off as "{who}", which the client never gave — remove the sender or use '
                    "only a name from the brief")
        for m in _EMAIL.finditer(text):
            if m.group(0).lower() not in corpus:
                say(f'the scene shows the email "{m.group(0)}", which the client never gave')
        for m in _URL.finditer(text):
            u = m.group(0).rstrip(".,)").lower()
            if "@" in u:
                continue
            if u not in corpus and u.removeprefix("https://").removeprefix("http://").removeprefix("www.") not in corpus:
                say(f'the scene shows the web address "{m.group(0)}", which the client never gave')
        for m in _HANDLE.finditer(text):
            if m.group(0).lower() not in corpus:
                say(f'the scene shows the handle "{m.group(0)}", which the client never gave')
        for m in _PHONE.finditer(text):
            d = _digits(m.group(0))
            if len(d) >= 8 and d not in corpus_digits:
                say(f'the scene shows the number "{m.group(0).strip()}", which the client never gave')
    return out
