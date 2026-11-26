"""
Prism — skills: the doctrine layer
──────────────────────────────────
A skill is one folder that says what a good result looks like for one job,
and — where a program can tell — checks the result against that.

    skills/<key>/
        SKILL.md      frontmatter (what it is for) + the doctrine itself
        checks.py     optional: faults(text, context) -> list[str]
        examples/     optional: golden outputs the doctrine points at
        references/   optional: long material for people; never typed

Why this exists
───────────────
Prism already knew a great deal about what a good BOQ, a good cold email
or a good reel looks like. That knowledge was spread over eighteen modules
as f-strings, so changing a rule of thumb meant a release, a customer
could not add their own, and the router was asked to *improvise* a role,
a deliverable spec and a quality bar from scratch on every run. A skill
is the same knowledge as a file: shipped with the app, overridable by the
customer, listed to the router so it can pick rather than invent, and —
the half that makes it more than a prompt — paired with a check the run
can act on.

Two ways in
───────────
  · ROUTED. The router sees every skill's `description` under the stages
    it applies to (`stages:` in the frontmatter) and puts the fitting keys
    in that stage's "skills" list. `assign()` validates what it chose and
    falls back to `triggers:` matched against the person's own words, the
    same way the make-stage guardrail backs up the router. A skill marked
    `only_when_asked: true` is kept from the router's pick only when one of
    its triggers appears in those words. automation.run then types
    `block()` into the stage prompt after the person's words.
  · FEATURE. An add-on that builds its own prompt (BOQ, drafting, Reel …)
    appends `addendum("<feature>")` — the skills whose `features:` list
    names that job. Nothing is injected when no skill claims the job, so
    every existing prompt is byte-identical until a skill folder says
    otherwise.

Overrides, and what may not be overridden
─────────────────────────────────────────
  ~/.prism/skills/<key>/SKILL.md   replaces the shipped doctrine
  ~/.prism/skills/<key>/notes.md   is appended to it ("field notes")
  ~/.prism/skills/<new-key>/...    adds a skill of the customer's own
checks.py is loaded ONLY from the shipped folder. A skill served from the
licence server or written by hand may change what a tool is told; it may
not run code on the customer's machine.

Budgets
───────
The browser tools take one blob of text and a long prompt slows typing and
crowds out the person's own request, so over "browser" transport a body is
cut at `budget:` characters, at a paragraph boundary. Direct API calls
("api") get the whole body. Authors put the important rules first.
"""
from __future__ import annotations

import importlib.util
import os
import re
import threading
from dataclasses import dataclass

from . import config as C
from . import ui

_HERE = os.path.dirname(os.path.abspath(__file__))
# Source checkout: prism_terminal/skills. Frozen: <bundle>/skills — the spec
# copies the folder to the bundle root, next to core/, so the same relative
# walk finds it. The second candidate covers the generic engine-data copy.
SHIPPED_DIRS = (
    os.path.join(os.path.dirname(_HERE), "skills"),
    os.path.join(os.path.dirname(_HERE), "prism_terminal", "skills"),
)

TRANSPORTS = ("browser", "api")
DEFAULT_BUDGET = 2600      # characters of one skill over browser transport
TOTAL_BUDGET = 6000        # all skills on one stage, browser transport
MAX_PER_STAGE = 2          # the router may attach at most this many
SKILL_FILE = "SKILL.md"
NOTES_FILE = "notes.md"
CHECKS_FILE = "checks.py"

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


def shipped_dir() -> str:
    for d in SHIPPED_DIRS:
        if os.path.isdir(d):
            return d
    return SHIPPED_DIRS[0]


def user_dir() -> str:
    return os.path.join(C.CONFIG_DIR, "skills")


@dataclass
class Skill:
    key: str
    title: str
    description: str
    stages: tuple
    features: tuple
    triggers: tuple
    transport: tuple
    budget: int
    version: int
    body: str
    path: str
    overridden: bool = False
    notes: str = ""
    checks_path: str = ""
    # True: a format skill the router may attach only when the person's OWN
    # words ask for that kind of deliverable (a trigger appears in them).
    # None means the frontmatter did not say, so an override inherits the
    # shipped value; see assign().
    only_when_asked: bool | None = None

    @property
    def has_checks(self) -> bool:
        return bool(self.checks_path)

    def for_stage(self, stage: str) -> bool:
        return _base(stage) in self.stages

    def for_feature(self, feature: str) -> bool:
        return feature in self.features


# ── frontmatter ───────────────────────────────────────────────────────────────
# A deliberately small reader: `key: value`, `key: [a, b]`, and a YAML-style
# block list. PyYAML is not an engine dependency and a skill file needs
# nothing more than this.

def _split_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    last_key = ""
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip("\n")
        stripped = line.strip()
        if stripped.startswith("- ") and last_key:
            meta.setdefault(last_key, [])
            if isinstance(meta[last_key], list):
                meta[last_key].append(_unquote(stripped[2:]))
        elif ":" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()
            last_key = key
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                meta[key] = ([_unquote(v) for v in inner.split(",") if v.strip()]
                             if inner else [])
            elif value == "":
                meta[key] = []
            else:
                meta[key] = _unquote(value)
        i += 1
    # No closing fence: treat everything as body rather than lose it.
    return {}, text


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _as_tuple(value) -> tuple:
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value or []:
        v = str(v).strip().lower()
        if v:
            out.append(v)
    return tuple(out)


def _as_int(value, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _as_flag(value) -> bool | None:
    """A frontmatter yes/no. None when the key is absent or unreadable, so
    an override that says nothing keeps what the shipped skill said."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return None


def _as_list(value) -> list:
    """Whatever the planner put in "skills", as a list.

    It is a language model's JSON: a bare string, a number, an object or
    null all turn up. `value or []` is not enough -- a non-empty scalar
    survives it and then iterates into a TypeError in the middle of a run.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _base(stage: str) -> str:
    """'content 2' (a duplicated plan step) → 'content'."""
    return (stage or "").strip().lower().split(" ")[0]


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _parse(key: str, folder: str, shipped_checks: str = "",
           overridden: bool = False) -> Skill | None:
    text = _read(os.path.join(folder, SKILL_FILE))
    if not text.strip():
        return None
    meta, body = _split_frontmatter(text)
    transport = _as_tuple(meta.get("transport")) or TRANSPORTS
    transport = tuple(t for t in transport if t in TRANSPORTS) or TRANSPORTS
    return Skill(
        key=key,
        title=str(meta.get("title") or meta.get("name") or key).strip(),
        description=" ".join(str(meta.get("description") or "").split()),
        stages=_as_tuple(meta.get("stages")),
        features=_as_tuple(meta.get("features")),
        triggers=_as_tuple(meta.get("triggers")),
        transport=transport,
        budget=_as_int(meta.get("budget"), DEFAULT_BUDGET),
        version=_as_int(meta.get("version"), 1),
        body=body.strip(),
        path=folder,
        overridden=overridden,
        checks_path=shipped_checks,
        only_when_asked=_as_flag(meta.get("only_when_asked")),
    )


# ── the catalogue ─────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: dict = {"sig": None, "skills": {}}


def _folders(root: str, holding=(SKILL_FILE,)) -> list[tuple[str, str]]:
    """(key, folder) for every folder under `root` holding any of `holding`.

    The user's folders qualify on notes.md alone: adding a line of your own
    to a shipped skill must not mean copying its whole SKILL.md first, and
    requiring one is how "put a notes.md beside it" silently did nothing.
    """
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        folder = os.path.join(root, name)
        if not (os.path.isdir(folder) and _KEY_RE.match(name)):
            continue
        if any(os.path.isfile(os.path.join(folder, f)) for f in holding):
            out.append((name, folder))
    return out


def _signature() -> tuple:
    sig = []
    for root, holding in ((shipped_dir(), (SKILL_FILE,)),
                          (user_dir(), (SKILL_FILE, NOTES_FILE))):
        for key, folder in _folders(root, holding):
            for fname in (SKILL_FILE, NOTES_FILE, CHECKS_FILE):
                p = os.path.join(folder, fname)
                try:
                    sig.append((p, os.stat(p).st_mtime_ns))
                except OSError:
                    continue
    return tuple(sig)


def reload() -> dict:
    """Read every skill folder again. Called by catalog() when a file
    changed, and by tests."""
    skills: dict = {}
    for key, folder in _folders(shipped_dir()):
        checks = os.path.join(folder, CHECKS_FILE)
        s = _parse(key, folder, checks if os.path.isfile(checks) else "")
        if s:
            skills[key] = s
    for key, folder in _folders(user_dir(), (SKILL_FILE, NOTES_FILE)):
        base = skills.get(key)
        # checks.py is read from the SHIPPED folder or nowhere. A skill file
        # may change what a tool is told; it may never run code here.
        override = _parse(key, folder, base.checks_path if base else "",
                          overridden=True)
        if override:
            # A customer's SKILL.md may leave the frontmatter out and just
            # write doctrine: the shipped routing then stays in force.
            if base:
                override.stages = override.stages or base.stages
                override.features = override.features or base.features
                override.triggers = override.triggers or base.triggers
                override.description = override.description or base.description
                if override.only_when_asked is None:
                    override.only_when_asked = base.only_when_asked
                override.title = (base.title if override.title == key
                                  else override.title)
            skills[key] = override
        elif base is None:
            continue          # notes.md for a skill that does not exist
        notes = _read(os.path.join(folder, NOTES_FILE)).strip()
        if notes:
            skills[key].notes = notes
    with _lock:
        _cache["sig"] = _signature()
        _cache["skills"] = skills
    return skills


def catalog() -> dict:
    with _lock:
        cached = _cache["skills"]
        sig = _cache["sig"]
    if sig is not None and sig == _signature():
        return cached
    return reload()


def get(key: str) -> Skill | None:
    return catalog().get((key or "").strip().lower())


def for_stage(stage: str) -> list:
    return [s for s in catalog().values() if s.for_stage(stage)]


def for_feature(feature: str) -> list:
    feature = (feature or "").strip().lower()
    return [s for s in catalog().values() if s.for_feature(feature)]


# ── the router's view ─────────────────────────────────────────────────────────

def stage_line(stage: str) -> str:
    """One indented line naming the skills a stage may use, by key only,
    for the router's stage table. Empty when the stage has none.

    Keys, not descriptions. A skill on four stages used to print its
    description four times, into a planner prompt the customer already
    calls over-engineered; each description now appears once, in
    catalogue_text(), inside the SKILLS rule.
    """
    found = for_stage(stage)
    if not found:
        return ""
    return "    SKILLS: " + ", ".join(s.key for s in found)


def catalogue_text(stages) -> str:
    """Every skill that applies to any of `stages`, once each, with the one
    sentence the planner decides by. '' when none apply."""
    seen, lines = set(), []
    for stage in stages:
        for s in for_stage(stage):
            if s.key not in seen:
                seen.add(s.key)
                lines.append(f"    {s.key} — {s.description}")
    return "\n".join(lines)


def any_for_stages(stages) -> bool:
    return any(for_stage(s) for s in stages)


def _trigger_score(skill: Skill, query_lc: str) -> tuple:
    """How well this skill matches the person's words:
    (-position, longest phrase at that position, hits).

    EARLIEST MENTION WINS, and only then the number of matching words.
    Ranking on the count first looked right and is not: it rewards
    whichever skill happens to list more synonyms, which is an accident of
    how its triggers were written, not a signal about this request. On
    "a pitch deck for the bank, and a one-pager pdf brochure" the document
    skill scored three hits to the deck's two and took the presentation
    stage -- for a request that opens by naming a deck. What someone asks
    for first is the deliverable; what follows is usually the aside.

    At the SAME position the longer phrase wins: "document the api" is the
    documentation skill's "document the", not the document skill's bare
    "document", and without this the tie fell to alphabetical order.
    """
    hits, first, longest = 0, len(query_lc) + 1, 0   # `hits` is the truth test; see assign()
    for t in skill.triggers:
        m = re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])",
                      query_lc)
        if not m:
            continue
        hits += 1
        if m.start() < first:
            first, longest = m.start(), len(t)
        elif m.start() == first:
            longest = max(longest, len(t))
    return (-first, longest, hits)


def assign(query: str, routing: dict, stages) -> dict:
    """Settle `routing[stage]["skills"]` for every stage in `stages`.

    Keeps what the router chose when it names real skills that apply to
    that stage; otherwise — for a stage that is going to run — falls back
    to the skills whose `triggers:` appear in the person's own words. The
    person's words, not the router's paraphrase: the same rule as
    automation._resolve_suffix, for the same reason. Returns {stage: keys}
    for the stages that ended up with any.
    """
    chosen: dict = {}
    q = (query or "").lower()
    for stage in stages:
        data = (routing or {}).get(stage)
        if not isinstance(data, dict):
            continue
        avail = {s.key: s for s in for_stage(stage)}
        raw = _as_list(data.get("skills"))
        keys = []
        for k in raw or []:
            k = str(k).strip().lower() if isinstance(k, (str, int)) else ""
            if k in avail and k not in keys:
                if avail[k].only_when_asked and _trigger_score(avail[k], q)[2] == 0:
                    # A format skill (a PDF or Word document's rulebook, say)
                    # the planner named although the person never asked for
                    # that format. The planner writes from a brief a model
                    # expanded from the request, and two of the owner's three
                    # reel tasks of 21 Sep 2026 got a quote-and-price document
                    # rulebook typed into the storyboard step because that
                    # brief invented "a storyboard PDF". The person's own
                    # words decide, as they do for the fallback.
                    ui.info(f"📘  {stage}: skill {k} left off — nothing in "
                            "the request asks for that kind of document")
                    continue
                keys.append(k)
        if not keys and data.get("needed"):
            # ONE, not two. A planner that names two skills has reasoned
            # about whether they sit together; a trigger match has not.
            # "a pitch deck, and a one-pager pdf brochure" hits both the
            # deck and the document skill, and typing both at one stage
            # asks for a deck laid out in pages -- then faults it for not
            # being one, and spends the stage's single re-ask on a
            # contradiction Prism created itself.
            scored = [(_trigger_score(s, q), s.key) for s in avail.values()]
            # Filter on the HIT COUNT, never on the score tuple. Its first
            # element is -position, and a skill whose trigger opens the
            # request scores 0 there -- falsy, so "research the cement
            # market" and "storyboard a 20s reel" both matched and were
            # then silently dropped for matching too well.
            matched = [x for x in scored if x[0][2] > 0]
            keys = [max(matched)[1]] if matched else []
        keys = keys[:MAX_PER_STAGE]
        data["skills"] = keys
        if keys:
            chosen[stage] = keys
    return chosen


def keys_for(routing: dict, stage: str) -> list:
    """The skill keys settled for a stage, tolerant of a duplicated plan
    step's label ('content 2') and of anything that is not a list."""
    data = (routing or {}).get(stage)
    if not isinstance(data, dict):
        data = (routing or {}).get(_base(stage))
    if not isinstance(data, dict):
        return []
    raw = _as_list(data.get("skills"))
    out = []
    for k in raw or []:
        k = str(k).strip().lower() if isinstance(k, (str, int)) else ""
        if k and get(k) and k not in out:
            out.append(k)
    return out


# ── what gets typed ───────────────────────────────────────────────────────────

_INTRO = (
    "PRISM SKILL — house doctrine for this step. This is part of the "
    "instructions, not background: follow it, and where it conflicts with "
    "generic advice it wins. Your answer is checked against the checklist "
    "in it before it is accepted, so fix anything that fails before you "
    "send.\n\n"
)


_MAKER_INTRO = (
    "PRISM SKILL — house standards for what you are building. Build the "
    "thing itself, here, in this tool; the standards below describe what "
    "makes it good, and they are not a request to write it out as text.\n\n"
)

# Sections a maker is not given. "Shape" is the layout of a TEXT answer
# ("write every slide as SLIDE 3 — …") and "Non-goals" tells a text stage to
# leave the building to a later step. Typed to Canva, Gamma, v0 or a video
# tool, both say the opposite of automation._maker_brief -- do not build it,
# type it out -- which is the failure the 2026-09-11 prompt audit traced as
# "Prism tells the tool not to make the file".
_MAKER_DROPS = ("shape", "non-goals", "non goals", "example")


def _for_maker(body: str) -> str:
    """The body without the sections a maker must not be given.

    Fence-aware: the documentation skill's Shape section SHOWS a document
    whose own headings are '## Before you start', inside a code fence. Read
    as real headings they reopened the cut and leaked half the layout.
    """
    out, keep, fenced = [], True, False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            m = re.match(r"^\s{0,3}##\s+(.+?)\s*$", line)
            if m:
                keep = not m.group(1).strip().lower().startswith(_MAKER_DROPS)
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def _trim(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    cut = text.rfind("\n\n", 0, budget)
    if cut < budget // 2:
        cut = text.rfind("\n", 0, budget)
    if cut < budget // 2:
        cut = budget
    return text[:cut].rstrip() + "\n[…]"


def block(keys, transport: str = "browser", maker: bool = False) -> str:
    """The text typed into a prompt for these skills, or '' when none
    apply over this transport. `maker` is True for a tool whose deliverable
    is the thing it builds (agents._MAKES); see _for_maker."""
    parts = []
    spent = 0
    for key in keys or []:
        s = get(key)
        if not s or transport not in s.transport:
            continue
        text = _for_maker(s.body) if maker else s.body
        if s.notes:
            text += "\n\nFIELD NOTES (written by the person running this):\n" + s.notes
        heading = f"═══ {s.title.upper()} ═══\n"
        if transport == "browser":
            # The heading and the blank line between skills are typed too,
            # so they come out of the budget. Counting only the bodies is
            # how a "6000 character" cap quietly types 6300.
            room = min(s.budget, TOTAL_BUDGET - spent - len(heading) - 2)
            text = _trim(text, room)
        if not text.strip():
            continue
        spent += len(text) + len(heading) + 2
        parts.append(heading + text)
    if not parts:
        return ""
    return (_MAKER_INTRO if maker else _INTRO) + "\n\n".join(parts) + "\n\n"


def addendum(feature: str, transport: str = "browser") -> str:
    """What a feature's own prompt builder appends: the skills claiming
    `feature`, or '' — so a prompt with no skill is exactly what it was."""
    keys = [s.key for s in for_feature(feature)]
    text = block(keys, transport)
    return ("\n\n" + text.rstrip("\n")) if text else ""


# ── the checks ────────────────────────────────────────────────────────────────

_checks_cache: dict = {}


def _checks_module(skill: Skill):
    path = skill.checks_path
    if not path or not os.path.isfile(path):
        return None
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    hit = _checks_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    spec = importlib.util.spec_from_file_location(
        f"prism_skill_checks_{skill.key.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)              # shipped code only
    _checks_cache[path] = (stamp, module)
    return module


# ── reading what Prism captured ───────────────────────────────────────────────
# A checker reads a reply as Prism captured it: the chat page as displayed,
# because automation._capture takes Selenium's element text. That is not
# markdown. Measured over the owner's 93 saved replies on 2026-09-11: no table
# pipes, no bold markers, list bullets and numbers gone, a heading as a bare
# short line, a table row as its cells joined by single spaces, and a
# Perplexity citation as a bare site name on its own line under the claim.
# The first checkers were written for markdown and misfired on nearly every
# real answer. These read the captured shape, and markdown as well, because a
# model sometimes types markdown where the chat does not render it.

def text_lines(text) -> list:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


_CHIP = re.compile(r"^(?:\+\d{1,2}|[a-z0-9][a-z0-9-]{1,40}"
                   r"(?:\.[a-z0-9-]{2,24})*(?:\s*\+\d{1,2})?)$")


def is_source_chip(line) -> bool:
    """'nhb.gov', 'shroomydelightsagrotech', '+1' -- the citation a
    Perplexity or ChatGPT reply shows on its own line under a claim."""
    s = (line or "").strip()
    return bool(s) and not s.isdigit() and bool(_CHIP.match(s))


_HEAD_MD = re.compile(r"^#{1,6}\s+\S")
_LABEL = re.compile(r"^[A-Z][A-Za-z0-9 /&()'-]{1,48}:$")


def is_heading(line) -> bool:
    """A line that reads as a heading once the chat has rendered it: short,
    capitalised, not a sentence. Lenient by design -- a table cell counted
    as a heading costs nothing, a real heading missed is a false fault."""
    s = (line or "").strip()
    if not s:
        return False
    if _HEAD_MD.match(s):
        return True
    s = re.sub(r"^\*\*(.+?)\*\*:?$", r"\1", s).strip()
    if _LABEL.match(s):
        return True
    if len(s) > 80 or len(s.split()) > 9:
        return False
    if s.endswith((".", ",", ";", "?", "!")) or is_source_chip(s):
        return False
    return (bool(re.match(r"^(?:\d{1,2}[.)]?\s+)?[A-Z]", s))
            and bool(re.search(r"[A-Za-z]{3}", s)))


def headings(text) -> list:
    return [line for line in text_lines(text) if is_heading(line)]


def has_section(text, pattern) -> bool:
    """Is there a heading matching `pattern` -- 'Executive Summary' for
    r'summar', '3. BIBLIOGRAPHY (Full Citations)' for r'bibliograph'."""
    rx = re.compile(pattern, re.IGNORECASE)
    return any(rx.search(h) for h in headings(text))


def section_lines(text, pattern) -> list:
    """The lines under the first heading matching `pattern`, up to the next
    heading. A short list item can read as a heading and end this early,
    which only makes a check more lenient, never less."""
    rx = re.compile(pattern, re.IGNORECASE)
    out, inside = [], False
    for line in text_lines(text):
        if is_heading(line):
            if inside:
                break
            inside = bool(rx.search(line))
            continue
        if inside:
            out.append(line)
    return out


def item_rows(text) -> list:
    """Rows of a parts list or a priced table: markdown pipe rows as
    'cell | cell', or captured rows that open with an item number and carry
    a figure ('8 UC 215 Bearing in 11,250.58'). A header row, which has no
    figure in it, is left out."""
    rows = []
    for line in text_lines(text):
        if line.startswith("|") and line.endswith("|") and line.count("|") >= 3:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if re.search(r"\d", line):
                rows.append(" | ".join(cells))
        elif re.match(r"^\d{1,3}[.)]?\s+\S", line) and re.search(r"\d", line[3:]):
            rows.append(line)
    return rows


_NUMBER = re.compile(r"(?<![A-Za-z])[₹$€£]?\d[\d,]*(?:\.\d+)?%?")


def table_like_lines(text, min_run: int = 2) -> list:
    """Consecutive short lines each carrying two or more figures -- how a
    rendered table without item numbers arrives."""
    out, run = [], []
    for line in text_lines(text):
        if (len(_NUMBER.findall(line)) >= 2 and len(line.split()) <= 16
                and not line.endswith(".")):
            run.append(line)
            continue
        if len(run) >= min_run:
            out.extend(run)
        run = []
    if len(run) >= min_run:
        out.extend(run)
    return out


_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{0,60}\b(?:placeholder|here|insert|tbd|todo|your|name|logo)\b"
    r"[^\]\n]{0,200}\]"
    r"|\[[A-Z][A-Za-z]*(?:[ _-][A-Za-z]+){0,3}\]"
    r"|\{\{?\s*[a-z_ ]{1,30}\s*\}?\}"
    r"|<(?:insert|your)[^>\n]{0,30}>"
    r"|\blorem ipsum\b|\bTBD\b|\bTODO\b|\bFIXME\b|\bX{3,}\b",
    re.IGNORECASE)


def placeholders(text) -> list:
    """Unfilled template text, as written: '[PRODUCT LOGO HERE]',
    '[IMAGE PLACEHOLDER — …]', '[Bank Name]', '{{company}}', 'TBD'."""
    seen = []
    for m in _PLACEHOLDER.finditer(text or ""):
        found = m.group(0).strip()
        if found not in seen:
            seen.append(found)
    return seen


_ARTIFACT = re.compile(
    r"\b(?:Document|Presentation|Spreadsheet|Code|Image|File|PDF|Word|Excel|"
    r"PowerPoint)\s*·\s*(?:DOCX|DOC|PPTX|PPT|XLSX|XLS|PDF|CSV|MD|HTML|PNG|JPE?G|"
    r"ZIP|TXT)\b", re.IGNORECASE)


_TOOL_RUN = re.compile(r"\b(?:ran|read|viewed|edited|created|wrote)\s+\d+\s+"
                       r"(?:files?|commands?|tools?)\b", re.IGNORECASE)


def is_artifact_reply(text) -> bool:
    """The tool built a file rather than writing the answer in the chat.

    Two shapes, both from the owner's own runs: the chat shows a file card
    ('Document·DOCX', 'Download'), or it shows only Claude's sandbox log --
    'Read 11 files, ran 11 commands, and 3 more tools' -- because the deck or
    document is a file and the chat holds a line or two of commentary.
    """
    t = text or ""
    if _ARTIFACT.search(t) and (len(t) < 3000 or "Download" in t):
        return True
    return len(t) < 2000 and bool(_TOOL_RUN.search(t))


def check(keys, text: str, context: dict | None = None) -> list:
    """Every fault the skills' checks find in `text`, prefixed with the
    skill's key. A check that raises is reported and skipped: a bug in a
    checker must never cost a run."""
    faults: list = []
    if is_artifact_reply(text):
        # The tool built a file and the chat shows its card. On the owner's
        # own document run Claude built the DOCX itself; judged as a text
        # document, its build log would have spent the re-ask asking Claude
        # to paste the document into the chat instead.
        return faults
    for key in keys or []:
        s = get(key)
        if not s or not s.has_checks:
            continue
        try:
            module = _checks_module(s)
            fn = getattr(module, "faults", None) if module else None
            if not callable(fn):
                continue
            got = fn(text or "", dict(context or {}))
        except Exception as err:                        # noqa: BLE001
            ui.warn(f"skill '{key}' check failed and was skipped: "
                    f"{ui.literal(err)}")
            continue
        for f in got or []:
            f = " ".join(str(f).split())
            if f:
                faults.append(f"[{key}] {f}")
    return faults


_SPECIFICS_FAULT = re.compile(r"Only \d+ checkable specific")
_NO_INVENTION = re.compile(
    r"awaiting brand copy|none supplied|(?:do|will|must|never) not invent|"
    r"nothing (?:is )?invented|to supply\]", re.IGNORECASE)


def repair_cannot_help(faults, *texts) -> bool:
    """True when the only fault is "too few checkable specifics" and the
    answer or the brief says nothing may be invented ("none supplied", "do not
    invent", "awaiting brand copy"). Such a repair cannot be satisfied: the
    writer is told to add facts the brief forbids it to make up, so it is not
    paid for (26 Nov 2026: a reel plan full of real numbers, no brand copy)."""
    faults = list(faults or [])
    return bool(faults) and all(_SPECIFICS_FAULT.search(f) for f in faults) \
        and any(_NO_INVENTION.search(t or "") for t in texts)


def repair_prompt(faults) -> str:
    listed = "\n".join(f"{n}. {f}" for n, f in enumerate(list(faults)[:10], 1))
    return (
        "Nearly there — Prism checked your answer against the skill's "
        "checklist and these are wrong:\n\n" + listed +
        "\n\nSend the corrected answer IN FULL — the whole deliverable again "
        "in the same format, not just the changed parts, and no commentary "
        "about the changes."
    )


# ── for people ────────────────────────────────────────────────────────────────

def describe() -> str:
    """A plain listing for /skills and the diagnostics screen."""
    rows = []
    for s in sorted(catalog().values(), key=lambda x: x.key):
        where = []
        if s.stages:
            where.append("stages: " + ", ".join(s.stages))
        if s.features:
            where.append("features: " + ", ".join(s.features))
        flags = []
        if s.has_checks:
            flags.append("checked")
        if s.overridden:
            flags.append("overridden")
        if s.notes:
            flags.append("notes")
        # Round brackets, never square: core.ui renders Rich markup, and
        # "[checked]" is a style tag to it -- the flags were swallowed
        # whole and /skills quietly stopped saying which skills have a
        # checker, which is the one thing that listing is for.
        rows.append(f"  {s.key:<24} {s.title}"
                    + (f"  ({', '.join(flags)})" if flags else "")
                    + f"\n{'':26}{'; '.join(where)}")
    if not rows:
        return f"  (no skills found under {shipped_dir()} or {user_dir()})"
    return "\n".join(rows)
