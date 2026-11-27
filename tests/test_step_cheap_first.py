"""STEP cheap-first: local facts, a cheap model checked by validators, one
escalation. Real STEP files built with cadquery (known answers); a fake AI
client stands in for the server, so nothing is paid for and no network is
used. HOME is a sandbox: nothing here touches the real ~/.prism.

Run from the engine root with the GUI venv:
  .venv/bin/python -m pytest tests/test_step_cheap_first.py
"""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import step_cheap as sc   # noqa: E402
from core import stepfile as SF     # noqa: E402

cq = pytest.importorskip("cadquery")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    plate = (cq.Workplane("XY").box(100, 50, 3).faces(">Z").workplane()
             .pushPoints([(-30, 0), (0, 0), (30, 0)]).hole(5)
             .faces(">Z").workplane().pushPoints([(-40, 15)]).hole(8))
    asm = cq.Assembly()
    asm.add(plate, name="PLATE")
    asm.add(cq.Workplane("XY").box(20, 20, 10).translate((0, 0, 20)),
            name="BLOCK")
    path = str(d / "fx.step")
    asm.export(path)
    return SF.analyse(path, "metal")


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return str(tmp_path / "prism")


class Fake:
    """Records calls; replies come from a queue of dicts / strings."""
    def __init__(self, *replies, credits_mc=(2000, 40000)):
        self.replies, self.calls, self.mc = list(replies), [], credits_mc

    def __call__(self, role, prompt, **kw):
        self.calls.append(role)
        kw["usage_sink"]["credits_mc"] = (
            self.mc[0] if role == sc.ROLE_FAST else self.mc[1])
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


def ask(report, base, q, client=None, **kw):
    return sc.ask(report, q, licence_id="lic-1", client=client, base=base, **kw)


# ── local answers: known answers, zero AI calls ─────────────────────────────

LOCAL = [
    ("how many holes are there", "4 hole(s)"),
    ("How many holes of 5 mm?", "3 hole(s) of Ø5"),
    ("what size is H1", "H1 is a Ø8 mm hole in PLATE"),
    ("what are the overall dimensions", "100.00 x 50.00 x 26.50"),
    ("how big is BLOCK", "20.00 x 20.00 x 10.00"),
    ("what is the volume of PLATE", "14.67 cm3"),
    ("list the part names", "PLATE, BLOCK"),
    ("how many parts", "2 part"),
    ("what units", "millimetres"),
    ("what is the material mode", "metal"),
    ("how thick is the BLOCK", "5.00 mm"),
    ("what does the plate weigh", "g"),
]


@pytest.mark.parametrize("q,expect", LOCAL)
def test_local_answers_cost_nothing(report, base, q, expect):
    fake = Fake()
    r = ask(report, base, q, fake)
    assert r["path"] == sc.PATH_LOCAL and fake.calls == []
    assert expect in r["text"] and r["credits"] == 0
    # local work is free: never counted against the AI-file cap
    assert sc._load_usage(base) == {}


def test_local_edit_is_free_and_valid(report, base):
    fake = Fake()
    r = ask(report, base, "enlarge H2 to 6 mm", fake)
    ch = r["plan"]["changes"][0]
    assert r["path"] == sc.PATH_LOCAL and not fake.calls
    assert (ch["op"], ch["hole"], ch["dia_mm"], ch["new_dia_mm"]) == \
        ("enlarge_hole", "H2", 5.0, 6.0)


def test_shrinking_a_hole_is_not_done_locally(report, base):
    r = ask(report, base, "enlarge H1 to 7 mm",
            Fake({"answer": "A hole can only be enlarged.", "claims": [],
                  "changes": [], "advice": []}))
    assert r["path"] == sc.PATH_CHEAP and r["plan"]["changes"] == []


# ── cheap model passes the validators ───────────────────────────────────────

GOOD = {"answer": "Enlarge H2 from Ø5 to Ø6 mm; PLATE has 4 holes.",
        "claims": [{"type": "part", "name": "PLATE"},
                   {"type": "hole_count", "part": "PLATE", "value": 4},
                   {"type": "hole_tag", "tag": "H2", "dia_mm": 5}],
        "changes": [{"op": "enlarge_hole", "part": "PLATE", "dia_mm": 5,
                     "new_dia_mm": 6, "hole": "H2", "why": "easier bolt"}],
        "advice": ["Keep Ø8 as is."]}


def test_cheap_pass_accepted(report, base):
    fake = Fake(GOOD)
    r = ask(report, base, "how could this be easier to assemble", fake)
    assert r["path"] == sc.PATH_CHEAP and fake.calls == [sc.ROLE_FAST]
    assert r["verified"] and r["credits"] == 2.0
    assert r["plan"]["changes"][0]["new_dia_mm"] == 6.0


def _bad(**over):
    d = json.loads(json.dumps(GOOD))
    d.update(over)
    return d


LIES = [
    ("wrong hole count",
     _bad(claims=[{"type": "hole_count", "part": "PLATE", "value": 9}])),
    ("invented part",
     _bad(claims=[{"type": "part", "name": "BRACKET"}])),
    ("wrong hole tag diameter",
     _bad(claims=[{"type": "hole_tag", "tag": "H2", "dia_mm": 8}])),
    ("change on a hole that does not exist",
     _bad(changes=[{"op": "enlarge_hole", "part": "PLATE", "dia_mm": 12,
                    "new_dia_mm": 14, "why": "x"}])),
    ("change on a tag that does not exist",
     _bad(changes=[{"op": "enlarge_hole", "part": "PLATE", "dia_mm": 5,
                    "new_dia_mm": 6, "hole": "H9", "why": "x"}])),
    ("new hole will not fit in the part",
     _bad(changes=[{"op": "enlarge_hole", "part": "BLOCK", "dia_mm": 5,
                    "new_dia_mm": 19, "why": "x"}])),
    ("invented number in the text",
     _bad(answer="The plate is 120 mm long.")),
    ("wrong overall size",
     _bad(claims=[{"type": "size", "part": "all",
                   "size_mm": [100, 50, 30]}])),
    ("wrong volume",
     _bad(claims=[{"type": "volume", "part": "PLATE", "cm3": 20.0}])),
]


@pytest.mark.parametrize("name,lie", LIES, ids=[n for n, _ in LIES])
def test_validator_catches_a_lying_cheap_model(report, base, name, lie):
    assert sc.validate(report, lie), name
    fake = Fake(lie, GOOD)
    r = ask(report, base, "how could this be easier to assemble", fake)
    assert fake.calls == [sc.ROLE_FAST, sc.ROLE_ESCALATE]
    assert r["path"] == sc.PATH_ESCALATED and r["verified"]
    assert r["failures"] and r["credits"] == 42.0


def test_good_reply_has_no_failures(report):
    assert sc.validate(report, GOOD) == []


def test_bad_json_escalates(report, base):
    fake = Fake("sorry, here is prose", GOOD)
    r = ask(report, base, "how could this be easier to assemble", fake)
    assert r["path"] == sc.PATH_ESCALATED


def test_escalates_at_most_once_and_drops_unsafe_parts(report, base):
    lie = _bad(claims=[{"type": "hole_count", "part": "PLATE", "value": 9}])
    fake = Fake(lie, lie, GOOD)
    r = ask(report, base, "how could this be easier to assemble", fake)
    assert len(fake.calls) == 2                  # never a third call
    assert r["path"] == sc.PATH_UNVERIFIED and not r["verified"]
    assert r["plan"] == {"changes": [], "advice": []}


# ── caps ─────────────────────────────────────────────────────────────────────

def _distinct(report, n):
    r = dict(report)
    r["file"] = f"f{n}.step"
    return r


def test_daily_cap_blocks_new_files_not_old_ones(report, base, monkeypatch):
    monkeypatch.setattr(sc, "STEP_AI_FILES_PER_DAY", 2)
    now = dt.datetime(2026, 11, 27, 10)
    q = "how could this be easier to assemble"
    for n in range(2):
        r = ask(_distinct(report, n), base, q, Fake(GOOD), now=now)
        assert r["path"] == sc.PATH_CHEAP
    fake = Fake(GOOD)
    r = ask(_distinct(report, 9), base, q, fake, now=now)
    assert r["capped"] and not fake.calls and "today's limit" in r["text"]
    # a file already counted keeps working; local answers still work
    assert ask(_distinct(report, 0), base, q, Fake(GOOD), now=now)["path"] == sc.PATH_CHEAP
    assert ask(_distinct(report, 9), base, "how many holes", Fake(),
               now=now)["path"] == sc.PATH_LOCAL
    # next day is open again
    nxt = now + dt.timedelta(days=1)
    assert ask(_distinct(report, 9), base, q, Fake(GOOD), now=nxt)["path"] == sc.PATH_CHEAP


def test_monthly_cap_and_default(report, base, monkeypatch):
    assert sc.STEP_AI_FILES_PER_MONTH == 300
    monkeypatch.setattr(sc, "STEP_AI_FILES_PER_MONTH", 1)
    q = "how could this be easier to assemble"
    ask(_distinct(report, 0), base, q, Fake(GOOD), now=dt.datetime(2026, 11, 1))
    r = ask(_distinct(report, 1), base, q, Fake(GOOD),
            now=dt.datetime(2026, 11, 20))
    assert r["capped"] and "this month's limit" in r["text"]
    r = ask(_distinct(report, 1), base, q, Fake(GOOD),
            now=dt.datetime(2026, 12, 2))
    assert r["path"] == sc.PATH_CHEAP


def test_caps_are_per_licence(report, base, monkeypatch):
    monkeypatch.setattr(sc, "STEP_AI_FILES_PER_MONTH", 1)
    q = "how could this be easier to assemble"
    sc.ask(report, q, licence_id="a", client=Fake(GOOD), base=base)
    r = sc.ask(_distinct(report, 1), q, licence_id="b", client=Fake(GOOD),
               base=base)
    assert r["path"] == sc.PATH_CHEAP


# ── log ──────────────────────────────────────────────────────────────────────

def test_every_answer_is_logged_by_path(report, base):
    q = "how could this be easier to assemble"
    ask(report, base, "how many holes", Fake())
    ask(report, base, q, Fake(GOOD))
    ask(_distinct(report, 1), base, q, Fake(_bad(answer="It is 999 mm."), GOOD))
    s = sc.summarize_log(base)
    assert s["local"]["avg"] == 0
    assert s["cheap"]["avg"] == 2.0 and s["escalated"]["avg"] == 42.0
    assert "licence" in open(os.path.join(base, "step_ai_log.jsonl")).readline()
