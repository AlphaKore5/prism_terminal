"""STEP questions: local code first, a cheap model second, Sonnet last.

Why this exists: a STEP file used to cost two calls on the `reasoner` role
(about 114 credits). Most of what people ask is a fact the measuring code
already knows, and most of the rest is an edit suggestion a cheap model can
make if every claim it makes is re-checked against the measured numbers.

The order, for one question about one measured file (`ask()`):

  1. LOCAL    A fact (size, volume, hole count, part names, units, material
              fields) or a plain edit ("enlarge H3 to 8 mm") is answered by
              code from the report. No AI call, no credits, not counted
              against the AI-file cap.
  2. CHEAP    Anything else goes to role `step.fast` once (the person's
              Haiku pick, or no pick), asked for strict JSON. The answer is then run through local validators that
              re-check every claim, every named part, hole tag and
              diameter, and every number in the text against the report.
  3. ESCALATE Only when validation fails: one call on role `reasoner`
              (Sonnet), told which checks failed, validated the same way.
              Never a second escalation. If that also fails, its answer is
              shown under a "could not be verified" warning and no change is
              offered; an honest empty plan beats an invented edit.
  Sonnet pick: step 2 is skipped; one validated call on the strong model.

A picture the person agreed to send goes with every AI call. A follow-up
sends the recent earlier exchanges, and older ones when it refers back
(history_block).

A per-licence daily and monthly cap on AI-assisted FILES (not questions)
limits the worst case; local-only work is free and never counted, and nor
is a call that fails. Every
answer appends one line to step_ai_log.jsonl (which path ran, how many
credits) so the saving is measurable.

The STEP file itself is never sent; only the measured text (and, with the
person's consent, Prism's own drawing of the parts), as before.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from typing import Callable

from . import stepfile as SF

ROLE_FAST = "step.fast"
ROLE_ESCALATE = "reasoner"
ROLE_PICTURE = "vision"          # the strong call when a picture goes with it

# The person's pick in STEP. Sent with the call, as before; the server checks
# it against its allow-list. "" (no pick) leaves the choice to the server.
MODEL_FAST = "anthropic/claude-haiku-5.5"
MODEL_STRONG = "anthropic/claude-sonnet-5.5"

# Earlier chat sent with a follow-up, in characters (about 4 to a token).
HISTORY_CHARS = 3000             # the recent exchanges, newest kept first
HISTORY_OLDER_CHARS = 1500       # extra, only when the question refers back
HISTORY_ANSWER_CHARS = 600       # one answer is cut to this
HISTORY_QUESTION_CHARS = 300

# Config constants. The server must enforce the same caps; these stop an
# honest client early and give the customer a plain message.
STEP_AI_FILES_PER_MONTH = 300
STEP_AI_FILES_PER_DAY = 40

PATH_LOCAL, PATH_CHEAP, PATH_ESCALATED = "local", "cheap", "escalated"
PATH_UNVERIFIED, PATH_CAPPED = "unverified", "capped"
PATH_STRONG = "strong"           # the person picked the strong model

UNVERIFIED_MESSAGE = (
    "Prism could not verify an AI answer against the measurements of this "
    "file, so it is not shown and no change is proposed. Sizes, hole counts "
    "and part names can still be asked directly.")
# Goes above a strong-model answer that failed the checks: it was billed, so
# it is shown. UNVERIFIED_MESSAGE is for when there is no answer to show.
UNVERIFIED_WARNING = (
    "Could not be verified: this answer did not pass Prism's checks against "
    "the measurements of this file. Check every figure before using it. No "
    "change is proposed.")
WARNING_MARK = "\u26a0 "           # starts every such answer

CAP_MESSAGE_DAY = ("You have reached today's limit of AI-assisted STEP files. "
                   "Measurements, sizes, hole counts and simple edits still "
                   "work and are free. AI suggestions are available again "
                   "tomorrow.")
CAP_MESSAGE_MONTH = ("You have reached this month's limit of AI-assisted STEP "
                     "files. Measurements, sizes, hole counts and simple "
                     "edits still work and are free. AI suggestions are "
                     "available again next month.")

_TOL = 0.06          # mm / cm3 slack when comparing a claim to a measurement


# ── home, cap and log ────────────────────────────────────────────────────────

def _dir(base: str = "") -> str:
    d = base or os.path.join(os.path.expanduser("~"), ".prism")
    os.makedirs(d, exist_ok=True)
    return d


def _lic(licence_id: str) -> str:
    return hashlib.sha256((licence_id or "none").encode()).hexdigest()[:12]


def file_key(report: dict) -> str:
    sig = report.get("file", "") + "|" + "|".join(
        f"{p['name']}:{p['size_mm']}:{p['volume_cm3']}"
        for p in report.get("parts", []))
    return hashlib.sha1(sig.encode()).hexdigest()[:16]


def _now(now) -> _dt.datetime:
    return now or _dt.datetime.now()


def _usage_path(base: str) -> str:
    return os.path.join(_dir(base), "step_ai_usage.json")


def _load_usage(base: str) -> dict:
    try:
        with open(_usage_path(base), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cap_status(licence_id: str, key: str, *, base: str = "", now=None) -> str:
    """"" when a new AI call for this file may start, else the plain message.
    A file already counted this month is free to keep asking about."""
    n = _now(now)
    rec = _load_usage(base).get(_lic(licence_id), {})
    month = rec.get("months", {}).get(n.strftime("%Y-%m"), [])
    day = rec.get("days", {}).get(n.strftime("%Y-%m-%d"), [])
    if key in month:
        return ""
    if len(day) >= STEP_AI_FILES_PER_DAY:
        return CAP_MESSAGE_DAY
    if len(month) >= STEP_AI_FILES_PER_MONTH:
        return CAP_MESSAGE_MONTH
    return ""


def count_file(licence_id: str, key: str, *, base: str = "", now=None) -> None:
    n = _now(now)
    data = _load_usage(base)
    rec = data.setdefault(_lic(licence_id), {})
    for field, label in (("months", n.strftime("%Y-%m")),
                         ("days", n.strftime("%Y-%m-%d"))):
        bucket = rec.setdefault(field, {})
        keep = sorted(bucket)[-3:]              # forget old periods
        for old in [k for k in bucket if k not in keep]:
            del bucket[old]
        lst = bucket.setdefault(label, [])
        if key not in lst:
            lst.append(key)
    with open(_usage_path(base), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _log(base: str, licence_id: str, report: dict, path: str, calls: int,
         credits: float, now) -> None:
    line = {"ts": _now(now).isoformat(timespec="seconds"),
            "licence": _lic(licence_id), "file_key": file_key(report),
            "file": report.get("file", ""), "path": path, "calls": calls,
            "credits": round(credits, 3)}
    with open(os.path.join(_dir(base), "step_ai_log.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def summarize_log(base: str = "") -> dict:
    """{path: {"answers": n, "credits": total, "avg": credits per answer}}"""
    out: dict[str, dict] = {}
    try:
        with open(os.path.join(_dir(base), "step_ai_log.jsonl"),
                  encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except OSError:
        rows = []
    for r in rows:
        s = out.setdefault(r["path"], {"answers": 0, "credits": 0.0})
        s["answers"] += 1
        s["credits"] += r["credits"]
    for s in out.values():
        s["avg"] = round(s["credits"] / s["answers"], 3)
    return out


# ── what the report knows ────────────────────────────────────────────────────

def hole_table(report: dict) -> list[dict]:
    """[{part, tag, dia_mm, count}] with the SAME tags the drawing and the
    prompt use. Parts with no taggable holes fall back to per-diameter rows
    with an empty tag."""
    shapes = dict(report.get("_shapes") or [])
    rows: list[dict] = []
    for part in report["parts"]:
        flat = part.get("flat")
        shape = shapes.get(part["name"])
        if flat and flat.get("holes"):
            tags, counts = SF._flat_named_holes(flat)
            seen: set[str] = set()
            for tag, h in zip(tags, flat["holes"]):
                if tag.startswith("G"):
                    if tag in seen:
                        continue
                    seen.add(tag)
                    rows.append({"part": part["name"], "tag": tag,
                                 "dia_mm": h["dia_mm"],
                                 "count": counts[h["dia_mm"]]})
                else:
                    rows.append({"part": part["name"], "tag": tag,
                                 "dia_mm": h["dia_mm"], "count": 1})
            continue
        named = SF._named_holes(shape) if shape is not None else []
        if named:
            rows += [{"part": part["name"], "tag": h["tag"],
                      "dia_mm": h["dia_mm"], "count": 1} for h in named]
        else:
            rows += [{"part": part["name"], "tag": "", "dia_mm": h["dia_mm"],
                      "count": h["count"]} for h in part["holes"]]
    return rows


def _dia_ok(a: float, b: float) -> bool:
    return abs(a - b) <= _TOL


def _part_named(report: dict, text: str) -> dict | None:
    t = (text or "").strip()
    if not t:
        return None
    for p in report["parts"]:
        if SF._part_name_matches(t, p["name"]):
            return p
    return None


def _part_total_holes(part: dict) -> int:
    return sum(h["count"] for h in part["holes"])


# ── 1. local answers ─────────────────────────────────────────────────────────

_OPEN_WORDS = re.compile(
    r"\b(should|why|improve|reduce|lighter|cheaper|cost|suggest|recommend|"
    r"advice|review|change|optimi[sz]e|better|best|makeable|manufactur|"
    r"strength|stronger|thinner|thicker|could|would|can i|how can|how do|"
    r"enlarge|increase|widen|bigger|resize|scale|shrink|smaller)\b")
_NUM = r"(\d+(?:\.\d+)?)"


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _dims(sz) -> str:
    return " x ".join(f"{v:.2f}" for v in sz) + " mm"


def _mentioned_part(report: dict, q: str) -> dict | None:
    for p in report["parts"]:
        if p["name"].casefold() in q:
            return p
    return None


def local_edit(report: dict, question: str) -> dict | None:
    """A plain, fully-specified edit becomes a plan with no AI call."""
    q = question.lower()
    m = re.search(r"\b(?:enlarge|increase|widen|bigger|make|change|resize)\b"
                  r".*?\b(h\d+)\b.*?(?:to|=)?\s*(?:ø|dia\w*|diameter)?\s*"
                  + _NUM + r"\s*(?:mm)?\b", q)
    if m:
        tag, new = m.group(1).upper(), float(m.group(2))
        for r in hole_table(report):
            if r["tag"] == tag:
                if new <= r["dia_mm"]:
                    return None
                return {"changes": [SF._valid_change({
                    "op": "enlarge_hole", "part": r["part"],
                    "dia_mm": r["dia_mm"], "new_dia_mm": new, "hole": tag,
                    "why": "Requested by the customer."})], "advice": []}
        return None
    m = re.search(r"\bscale\b.*?(?:by|to|x)\s*" + _NUM + r"\s*(%|x)?", q)
    if m:
        f = float(m.group(1))
        f = f / 100 if m.group(2) == "%" else f
        ch = SF._valid_change({"op": "scale", "part": "all", "factor": f,
                               "why": "Requested by the customer."})
        return {"changes": [ch], "advice": []} if ch else None
    return None


def local_answer(report: dict, question: str) -> str | None:
    """A sentence from the measured numbers, or None when this is not a
    plain factual question the report can answer completely."""
    q = " ".join(question.lower().replace("ø", " dia ").split())
    if not q or _OPEN_WORDS.search(q):
        return None
    parts = report["parts"]
    part = _mentioned_part(report, q)
    scope = [part] if part else parts
    where = f" in {part['name']}" if part else ""
    tag = re.search(r"\bh(\d+)\b", q)
    if tag:
        t = f"H{tag.group(1)}"
        for r in hole_table(report):
            if r["tag"] == t:
                return f"{t} is a Ø{r['dia_mm']:g} mm hole in {r['part']}."
        return None
    if re.search(r"\bhow many parts|number of parts|part count", q):
        return f"{len(parts)} part(s)."
    if re.search(r"\b(part names?|name of the parts|which parts|list.*parts|"
                 r"what parts)\b", q):
        return "Parts: " + ", ".join(p["name"] for p in parts) + "."
    if re.search(r"\bholes?\b", q) and re.search(
            r"\bhow many|count|number of|total\b", q):
        dm = re.search(r"dia\w*\s*" + _NUM + r"|" + _NUM + r"\s*mm", q)
        if dm:
            d = float(dm.group(1) or dm.group(2))
            n = sum(h["count"] for p in scope for h in p["holes"]
                    if _dia_ok(h["dia_mm"], d))
            return f"{n} hole(s) of Ø{d:g} mm{where}."
        n = sum(_part_total_holes(p) for p in scope)
        sizes = sorted({h["dia_mm"] for p in scope for h in p["holes"]})
        extra = (" Sizes: " + ", ".join(f"Ø{s:g}" for s in sizes) + " mm."
                 if sizes else "")
        return f"{n} hole(s){where}.{extra}"
    if re.search(r"\bholes?\b", q) and re.search(
            r"\b(size|sizes|diameter|diameters|dia)\b", q):
        rows = [f"Ø{h['dia_mm']:g} x {h['count']}"
                for p in scope for h in p["holes"]]
        return ("Holes" + where + ": " + " · ".join(rows) + ".") if rows \
            else f"No holes{where}."
    if re.search(r"\bunits?\b|\bmm or inch|\binch\b", q):
        return "All sizes are in millimetres (mm); volumes in cm3."
    if re.search(r"\bmaterial|\bmould?ing|\bmetal or plastic|\bplastic\b|"
                 r"\bsheet metal\b", q) and not re.search(r"\bweigh", q):
        return (f"This file was measured as {report['mode']} "
                f"({'sheet metal' if report['mode'] == 'metal' else 'moulded plastic'}); "
                "that is the mode chosen when it was opened, not read from "
                "the file.")
    if re.search(r"\bweigh|\bmass\b", q):
        return "\n".join(f"{p['name']}: {SF._weights(p['volume_cm3'], report['mode'])}"
                         for p in scope)
    if re.search(r"\bvolume\b", q):
        return "\n".join(f"{p['name']}: {p['volume_cm3']:.2f} cm3"
                         for p in scope)
    if re.search(r"\bthick|\bgauge\b|\bwall\b", q):
        return "\n".join(f"{p['name']}: about {p['thickness_mm']:.2f} mm"
                         for p in scope)
    if re.search(r"\bbound|\bdimension|\bsize\b|\boverall|\bbox\b|\blength|"
                 r"\bwidth|\bheight|\bhow (big|large|long|wide|tall)", q):
        if part:
            return f"{part['name']}: {_dims(part['size_mm'])}."
        lines = [f"Assembly overall: {_dims(report['overall_mm'])}."]
        lines += [f"{p['name']}: {_dims(p['size_mm'])}." for p in parts]
        return "\n".join(lines)
    return None


# A follow-up that leans on what was said before: code cannot know what "that
# one" is, so it is not answered locally.
_POINTS_BACK = re.compile(
    r"\b(other|another|that|those|these|same|previous(?:ly)?|earlier|"
    r"you (?:said|suggested|mentioned|proposed)|(?:first|last) one)\b")
# Wording that asks for older turns than the recent ones.
_REFERS_BACK = re.compile(
    r"\b(other|another|that|those|these|same|previous(?:ly)?|earlier|before|"
    r"again|originally|original|first|at the start|beginning|you (?:said|"
    r"suggested|mentioned|proposed|told)|we (?:said|discussed|talked)|"
    r"go back|back to)\b")
_FIRST = re.compile(r"\b(first|at the start|beginning|original(?:ly)?)\b")
_PLAIN_WORDS = frozenset(
    "about again also back before could does earlier first from have hole "
    "holes just like make mentioned more other part parts previous said same "
    "should some suggested than that them then there these they this those "
    "what when where which will with would your".split())


def _local(report: dict, question: str, history=None) -> tuple | None:
    """(text, plan) when code alone answers, else None."""
    q = question.lower()
    edit = local_edit(report, question)
    if edit and edit["changes"] and edit["changes"][0]:
        c = edit["changes"][0]
        txt = (f"Planned: " + (f"enlarge {c['hole']} from Ø{c['dia_mm']:g} to "
               f"Ø{c['new_dia_mm']:g} mm on {c['part']}" if c["op"] ==
               "enlarge_hole" else f"scale by {c['factor']:g}") + ".")
        if not _check_change(report, c):
            return txt, edit
    if (history and _POINTS_BACK.search(q)
            and not re.search(r"\bh\d+\b", q)):
        return None
    local = local_answer(report, question)
    if local:
        return local, {"changes": [], "advice": []}
    return None


def answers_locally(report: dict, question: str, history=None) -> bool:
    """True when asking this would send nothing and cost nothing."""
    return _local(report, question, history) is not None


# ── earlier chat ─────────────────────────────────────────────────────────────

def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+(?:\.\d+)?", text.lower())
            if (len(w) >= 4 or re.fullmatch(r"[hg]\d+|\d+(?:\.\d+)?", w))
            and w not in _PLAIN_WORDS}


def history_block(history, question: str) -> str:
    """The earlier exchanges ([{"q", "a"}], oldest first) to send with a
    follow-up. Recent ones, newest kept first, up to HISTORY_CHARS; when the
    question refers back (or names a hole only an older turn spoke of), up to
    HISTORY_OLDER_CHARS more from before that window, best matches first."""
    turns = [f"Customer: {_cut(h.get('q'), HISTORY_QUESTION_CHARS)}\n"
             f"Answer: {_cut(h.get('a'), HISTORY_ANSWER_CHARS)}"
             for h in history or []]
    if not turns:
        return ""
    keep, used, i = set(), 0, len(turns) - 1
    while i >= 0 and used + len(turns[i]) <= HISTORY_CHARS:
        keep.add(i)
        used += len(turns[i])
        i -= 1
    older = list(range(i + 1))
    q = question.lower()
    tags = set(re.findall(r"\bh\d+\b", q))
    if older and (_REFERS_BACK.search(q) or any(
            tags & _terms(turns[j]) for j in older)):
        want = _terms(q)

        def score(j):
            n = len(want & _terms(turns[j]))
            return n + (100 if j == 0 and _FIRST.search(q) else 0)

        used = 0
        # best match first; with nothing to match, the newest of the older
        for j in sorted(older, key=lambda j: (score(j), j), reverse=True):
            if used + len(turns[j]) <= HISTORY_OLDER_CHARS:
                keep.add(j)
                used += len(turns[j])
    out, last = [], -1
    for j in sorted(keep):
        if j != last + 1:
            out.append("(some earlier turns left out)")
        out.append(turns[j])
        last = j
    return "\n".join(out)


# ── 2. cheap pass: prompt, parse, validators ─────────────────────────────────

SCHEMA = (
    '{"answer": "<short plain text>",\n'
    ' "claims": [ {"type": "part", "name": "..."},\n'
    '             {"type": "hole_dia", "part": "...", "dia_mm": 0},\n'
    '             {"type": "hole_count", "part": "<name or all>", '
    '"dia_mm": 0, "value": 0},\n'
    '             {"type": "hole_tag", "tag": "H1", "dia_mm": 0},\n'
    '             {"type": "size", "part": "<name or all>", '
    '"size_mm": [0, 0, 0]},\n'
    '             {"type": "volume", "part": "...", "cm3": 0} ],\n'
    ' "changes": [ {"op": "enlarge_hole", "part": "...", "dia_mm": 0, '
    '"new_dia_mm": 0, "hole": "H1", "why": "..."},\n'
    '              {"op": "scale", "part": "...", "factor": 1, "why": "..."} ],\n'
    ' "advice": [ "..." ]}')


def fast_prompt(report: dict, question: str, failures: list[str] | None = None,
                earlier: str = "", picture: bool = False) -> str:
    o = report["overall_mm"]
    p = (f"You advise a {report['mode']} moulding shop on a customer's part. "
         "The CAD model is confidential; only the offline measurements below "
         "are known. Answer ONLY from them and reply with ONE JSON object, "
         "nothing else, exactly in this shape:\n" + SCHEMA + "\n\n"
         "Rules: every part name, hole tag (H1..) and diameter you use must "
         "appear below. List each factual statement you make in `claims` so "
         "it can be re-checked. `changes` may only use the two operations "
         "shown; a hole can only be enlarged. Give no change you are not "
         "sure of: an empty list is correct when none is justified. Keep "
         "`answer` and `advice` short and plain; do not mention rule "
         "numbers.\n\n"
         f"Job: {report['file']} · assembly overall "
         f"{o[0]:.2f} x {o[1]:.2f} x {o[2]:.2f} mm · "
         f"{len(report['parts'])} part(s)\n{SF._part_lines(report)}\n\n"
         + ("A drawing of these parts, made from the same measurements, is "
            "attached. Use it to see the layout; take every figure from the "
            "measurements.\n\n" if picture else "")
         + ("Earlier in this chat (context for what the customer means; the "
            f"measurements above are the truth):\n{earlier}\n\n"
            if earlier else "")
         + f"The customer asks: {question}\n")
    if failures:
        p += ("\nA first draft was REJECTED by automatic checks against the "
              "measurements:\n- " + "\n- ".join(failures[:8]) +
              "\nRe-answer correctly; use only measured facts.\n")
    return p


def parse_reply(text: str) -> dict | None:
    s, e = (text or "").find("{"), (text or "").rfind("}") + 1
    if s == -1 or e <= s:
        return None
    try:
        raw = json.loads(text[s:e])
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("answer"), str):
        return None
    return raw


def _numbers(text: str) -> list[float]:
    text = re.sub(r"(?m)^\s*\d+[.)]\s", "", text)       # list numbering
    text = re.sub(r"\bH\d+\b|\bG\d+\b", " ", text)       # hole tags
    text = re.sub(r"\d+(?:\.\d+)?\s*%", " ", text)       # percentages
    return [float(x) for x in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text)]


def _known_numbers(report: dict) -> set[float]:
    s: set[float] = set()
    for v in report["overall_mm"]:
        s.add(round(v, 2))
    s.update({float(len(report["parts"])), float(
        sum(_part_total_holes(p) for p in report["parts"]))})
    for p in report["parts"]:
        s.update(round(v, 2) for v in p["size_mm"])
        s.update({p["thickness_mm"], p["volume_cm3"], p["area_cm2"]})
        s.update(float(h["count"]) for h in p["holes"])
        s.update(h["dia_mm"] for h in p["holes"])
        s.add(float(_part_total_holes(p)))
        for chunk in re.findall(r"\d+(?:\.\d+)?", p["name"]):
            s.add(float(chunk))
    s.update(r["dia_mm"] for r in hole_table(report))
    return s


def _near(v: float, pool) -> bool:
    return any(abs(v - k) <= _TOL for k in pool)


def _check_change(report: dict, ch: dict) -> list[str]:
    bad = SF._valid_change(ch)
    if not bad:
        return [f"change {ch.get('op')!r} is malformed or out of range"]
    errs: list[str] = []
    part = bad["part"]
    targets = (report["parts"] if part == "all"
               else [p for p in [_part_named(report, part)] if p])
    if not targets:
        return [f"part {part!r} does not exist"]
    if bad["op"] == "enlarge_hole":
        rows = [r for r in hole_table(report) if r["part"] in
                {t["name"] for t in targets}]
        if bad["hole"]:
            row = next((r for r in rows if r["tag"] == bad["hole"]), None)
            if not row:
                errs.append(f"hole {bad['hole']} does not exist on {part}")
            elif not _dia_ok(row["dia_mm"], bad["dia_mm"]):
                errs.append(f"{bad['hole']} is Ø{row['dia_mm']:g}, not "
                            f"Ø{bad['dia_mm']:g}")
        elif not any(_dia_ok(h["dia_mm"], bad["dia_mm"])
                     for t in targets for h in t["holes"]):
            errs.append(f"no Ø{bad['dia_mm']:g} hole on {part}")
        # the bigger hole must still fit inside the part
        for t in targets:
            if bad["new_dia_mm"] >= 0.8 * min(t["size_mm"][:2]):
                errs.append(f"Ø{bad['new_dia_mm']:g} will not fit in {t['name']}")
    return errs


def validate(report: dict, reply: dict, history=None) -> list[str]:
    """Every reason NOT to trust the reply; [] means it checks out. Numbers
    already given in earlier answers (`history`) may be repeated; claims and
    changes are still checked against the measurements only."""
    errs: list[str] = []
    holes = hole_table(report)
    for c in reply.get("claims") or []:
        if not isinstance(c, dict):
            errs.append("a claim is not an object")
            continue
        t = c.get("type")
        try:
            if t == "part":
                if not _part_named(report, str(c.get("name", ""))):
                    errs.append(f"part {c.get('name')!r} does not exist")
            elif t == "hole_dia":
                p = _part_named(report, str(c.get("part", "")))
                if not p or not any(_dia_ok(h["dia_mm"], float(c["dia_mm"]))
                                    for h in p["holes"]):
                    errs.append(f"no Ø{c.get('dia_mm')} hole on {c.get('part')!r}")
            elif t == "hole_count":
                name = str(c.get("part", "all"))
                sc = (report["parts"] if name in ("all", "") else
                      [p for p in [_part_named(report, name)] if p])
                if not sc:
                    errs.append(f"part {name!r} does not exist")
                    continue
                if c.get("dia_mm") not in (None, 0, ""):
                    n = sum(h["count"] for p in sc for h in p["holes"]
                            if _dia_ok(h["dia_mm"], float(c["dia_mm"])))
                else:
                    n = sum(_part_total_holes(p) for p in sc)
                if int(c["value"]) != n:
                    errs.append(f"claimed {c['value']} holes, measured {n}")
            elif t == "hole_tag":
                tag = str(c.get("tag", "")).upper()
                if not any(r["tag"] == tag and _dia_ok(
                        r["dia_mm"], float(c["dia_mm"])) for r in holes):
                    errs.append(f"{tag} is not a Ø{c.get('dia_mm')} hole")
            elif t == "size":
                name = str(c.get("part", "all"))
                want = sorted((float(v) for v in c["size_mm"]), reverse=True)
                have = (report["overall_mm"] if name in ("all", "") else
                        (_part_named(report, name) or {}).get("size_mm"))
                if not have or len(want) != 3 or any(
                        abs(a - b) > _TOL for a, b in zip(want, have)):
                    errs.append(f"size {c['size_mm']} does not match "
                                f"{name!r}: {have}")
            elif t == "volume":
                p = _part_named(report, str(c.get("part", "")))
                if not p or abs(float(c["cm3"]) - p["volume_cm3"]) > _TOL:
                    errs.append(f"volume {c.get('cm3')} cm3 is wrong for "
                                f"{c.get('part')!r}")
            else:
                errs.append(f"unknown claim type {t!r}")
        except (KeyError, TypeError, ValueError):
            errs.append(f"claim {t!r} is incomplete")
    for ch in reply.get("changes") or []:
        errs += _check_change(report, ch if isinstance(ch, dict) else {})
    pool = _known_numbers(report)
    for h in history or []:
        if not str(h.get("a", "")).startswith(WARNING_MARK):
            pool.update(_numbers(str(h.get("a", ""))))
    for ch in reply.get("changes") or []:
        if isinstance(ch, dict):
            for k in ("dia_mm", "new_dia_mm", "factor"):
                try:
                    pool.add(float(ch[k]))
                except (KeyError, TypeError, ValueError):
                    pass
    text = " ".join([str(reply.get("answer", ""))] +
                    [str(a) for a in reply.get("advice") or []] +
                    [str(c.get("why", "")) for c in reply.get("changes") or []
                     if isinstance(c, dict)])
    for n in _numbers(text):
        if not _near(n, pool):
            errs.append(f"the text states {_fmt(n)}, which is not a "
                        "measured figure or part of a change")
    return errs


def _plan_of(reply: dict) -> dict:
    ch = [c for c in map(SF._valid_change, reply.get("changes") or []) if c]
    adv = [str(a).strip() for a in reply.get("advice") or [] if str(a).strip()]
    return {"changes": ch, "advice": adv[:12]}


# ── the one entry point ──────────────────────────────────────────────────────

def _default_client() -> Callable:
    from . import ai_client
    return ai_client.chat


def _call(client, role: str, prompt: str, model: str = "",
          attachments: list | None = None) -> tuple[dict | None, float, str]:
    sink: dict = {}
    extra: dict = {}
    if model:
        extra["model"] = model
    if attachments:
        extra["attachments"] = attachments
    text = client(role, prompt, addon="step", feature="step.ask",
                  json_mode=True, temperature=0.0, max_tokens=1500,
                  usage_sink=sink, timeout=90, **extra)
    return parse_reply(text), sink.get("credits_mc", 0) / 1000.0, text


def _shown_answer(reply: dict | None, raw: str) -> str:
    """The words of a reply that failed the checks, "" when it has none a
    person could read (nothing, or broken JSON)."""
    if reply:
        lines = [reply["answer"].strip()] + [
            f"- {a}" for a in _plan_of(reply)["advice"]]
        return "\n".join(x for x in lines if x).strip()
    raw = (raw or "").strip()
    return "" if raw.startswith(("{", "[", "```")) else raw


def ask(report: dict, question: str, *, licence_id: str = "", client=None,
        base: str = "", now=None, model: str = "",
        attachments: list | None = None, history: list | None = None) -> dict:
    """Result: {text, plan, path, credits, calls, verified, failures, capped}.
    `client(role, prompt, **kw) -> str` defaults to ai_client.chat; tests
    pass a fake. `base` is the folder holding the cap and log files.

    `model`: the person's pick. MODEL_STRONG goes straight to the strong
    model, once. Anything else takes the cheap pass, then one escalation to
    the strong model only when validation fails. `attachments`: pictures sent
    with every AI call made here (never for a local answer). `history`:
    earlier exchanges [{"q", "a"}], oldest first, for a follow-up.

    A file counts against the cap only once an answer comes back: a call
    that raises leaves the count as it was."""
    def done(text, plan, path, credits=0.0, calls=0, verified=True,
             failures=None):
        _log(base, licence_id, report, path, calls, credits, now)
        return {"text": text, "plan": plan, "path": path, "credits": credits,
                "calls": calls, "verified": verified,
                "failures": failures or [], "capped": path == PATH_CAPPED}

    local = _local(report, question, history)
    if local:
        return done(local[0], local[1], PATH_LOCAL)

    key = file_key(report)
    msg = cap_status(licence_id, key, base=base, now=now)
    if msg:
        return done(msg, {"changes": [], "advice": []}, PATH_CAPPED)

    client = client or _default_client()
    earlier = history_block(history, question)
    strong = model == MODEL_STRONG
    strong_role = ROLE_PICTURE if attachments else ROLE_ESCALATE
    steps = [(strong_role, model, PATH_STRONG)] if strong else [
        (ROLE_FAST, model, PATH_CHEAP),
        (strong_role, MODEL_STRONG if model else "", PATH_ESCALATED)]
    credits, calls, failures, first = 0.0, 0, [], []
    for role, use, path in steps:
        reply, c, raw = _call(client, role, fast_prompt(
            report, question, failures, earlier, bool(attachments)),
            use, attachments)
        credits, calls = credits + c, calls + 1
        failures = (validate(report, reply, history) if reply
                    else ["the reply was not valid JSON in the required shape"])
        if not failures:
            count_file(licence_id, key, base=base, now=now)
            return done(reply["answer"], _plan_of(reply), path, credits, calls,
                        failures=first)
        first = first or failures
    # Nothing checked out. The strong model's answer was paid for, so it is
    # shown under a warning; its changes are never offered for building.
    count_file(licence_id, key, base=base, now=now)
    answer = _shown_answer(reply, raw)
    text = (f"{WARNING_MARK}{UNVERIFIED_WARNING}\n\n{answer}" if answer
            else UNVERIFIED_MESSAGE)
    result = done(text, {"changes": [], "advice": []}, PATH_UNVERIFIED,
                  credits, calls, verified=False, failures=failures)
    result["answer"] = answer
    return result
