"""What a Reel costs on Prism's server: every call tagged as the film it belongs
to, a scene turn carrying only what it needs, the repeated start marked for the
prompt cache, limits per turn, a failed scene asked again on its own, and a
switch for each.

Run from the engine root:  HOME=$(mktemp -d) python -m pytest tests/test_reel_cost.py
No network: the transport is a fake of the server's /v1/ai/* answers.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ai_client as ai, automation, config as C, reel_web   # noqa: E402
from core import server_session as SS                                 # noqa: E402

N = 4
QUERY = "make an Instagram reel about our crane hire service for builders"
SCRIPT = {"scenes": [{"role": "statement", "seconds": 4, "headline": f"Headline {i}"}
                     for i in range(N)]}
LOOK = {"design": {"direction": "calm and exact",
                   "css": ":root{--ink:#111}" + ".k{letter-spacing:.2em}" * 150},
        "storyboard": [{"job": f"job {i}", "cut": "push"} for i in range(N)]}
ROUTING = {"content": {"needed": True, "questions": ["Write the script for the reel"]},
           "media": {"needed": True, "kind": "video", "questions": ["Make the reel"]}}
AGENTS = {"content": "Claude", "media": "Prism Studio", "visual": "Prism Image"}


def scene_reply(n, note="", pad=1500):
    return "```json\n" + json.dumps({
        "seconds": 4, "cut": "push", "css": f".s{n}{{color:red}}" + "/*c*/" * pad,
        "html": f"<div class='s{n}'>Headline {n - 1} {note}MARK{n}</div>"}) + "\n```"


def text_of(body):
    c = body["messages"][-1]["content"]
    return c if isinstance(c, str) else "".join(
        b["text"] for b in c if b.get("type") == "text")


class Fake:
    def __init__(self):
        self.calls, self.fail, self.bad = [], {}, set()

    def __call__(self, path, body, bearer):
        if path == ai.PATH_IMAGE:
            self.calls.append(("image", "", body))
            return {"images": ["iVBORw0KGgo="], "charged_mc": 1}
        text = text_of(body)
        ask = text.split("Now the user writes:\n\n")[-1]
        scene = re.search(r"THIS IS SCENE (\d+) OF", ask)
        if body["feature"] in ("brief", "briefs"):
            kind, out = "briefs", "a picture\n" * 6
        elif scene:
            n = int(scene.group(1))
            kind = f"scene{n}"
            self.calls.append((kind, text, body))
            if self.fail.get(n):
                self.fail[n] -= 1
                raise ai.AiError("PROVIDER_ERROR", "cut off", {"kind": "cut_off"})
            return {"text": "just prose" if n in self.bad else scene_reply(n),
                    "charged_mc": 1}
        elif "was laid out at" in ask:
            kind, out = "fix", scene_reply(int(re.search(r"Scene (\d+) was", ask).group(1)), "FIXED ")
        elif "again as JSON only" in ask:
            kind, out = "retry", scene_reply(int(re.search(r"scene (\d+) again", ask).group(1)))
        elif "ART DIRECTOR" in ask:
            kind, out = "design", "```json\n" + json.dumps(LOOK) + "\n```"
        else:
            kind, out = "script", "```json\n" + json.dumps(SCRIPT) + "\n```"
        self.calls.append((kind, text, body))
        return {"text": out, "charged_mc": 1}

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def server(monkeypatch, tmp_path):
    fake = Fake()
    ai.set_auth_provider(lambda: {"base_url": "http://127.0.0.1:9", "license_id": "L",
                                  "device_fp": "d" * 16, "lease": "t"})
    ai.set_transport(fake)
    ai.set_run_id("")
    monkeypatch.setattr(C, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(reel_web, "available", lambda: (True, ""))
    monkeypatch.setattr(reel_web, "rhythm_probe", lambda *a, **k: None)
    fake.checked, fake.rendered = [], []

    def inspect(spec, *a, **k):
        html = spec["scenes"][0]["html"]
        fake.checked.append(html)
        # Scene 3 is laid out wrong until it has been fixed.
        return ["the headline is off the frame"] if "MARK3" in html and "FIXED" not in html else []

    def render(spec, out, **k):
        fake.rendered.append(spec)
        with open(out, "wb") as f:
            f.write(b"0" * 64)
        return out

    monkeypatch.setattr(reel_web, "inspect", inspect)
    monkeypatch.setattr(reel_web, "render", render)
    yield fake
    ai.set_transport(None)
    ai.set_auth_provider(None)
    ai.set_run_id("")


def run(savings=None, routing=ROUTING, **kw):
    cfg = C.load()
    cfg["agents"] = dict(AGENTS)
    if savings is not None:
        cfg["ai_savings"] = savings
    return automation.run(routing, cfg, query=QUERY, chatgpt_analysis=False, **kw)


# ── tags ────────────────────────────────────────────────────────────────────

def test_every_call_of_a_reel_is_tagged_reel_with_one_run_id(server):
    run()
    kinds = {c[0] for c in server.calls}
    assert {"script", "briefs", "image", "design", "scene1", "fix"} <= kinds
    assert {c[2]["addon"] for c in server.calls} == {"reel"}
    ids = {c[2]["run_id"] for c in server.calls}
    assert len(ids) == 1 and ids.pop().startswith("r")
    assert ai.film_state() == ("", ""), "the tag and the made-up run id are put back"
    ai.chat("writer", "hi")
    assert server.calls[-1][2]["addon"] == "core"


def test_the_apps_own_run_id_is_kept(server):
    ai.set_run_id("r-from-the-app")
    run()
    assert {c[2]["run_id"] for c in server.calls} == {"r-from-the-app"}
    assert ai.film_state() == ("", "r-from-the-app")


def test_a_run_that_makes_no_film_keeps_its_tags(server):
    run(routing={"content": {"needed": True, "questions": ["Write a letter"]}})
    assert {c[2]["addon"] for c in server.calls} == {"core"}
    assert "max_tokens" not in server.calls[0][2] and "effort" not in server.calls[0][2]


@pytest.mark.parametrize("film", ["reel", "motion"])
def test_a_part_of_a_film_says_so(server, film):
    cfg = C.load()
    automation.run({}, cfg, custom_stages=[("refine", "Claude", ["Change scene 2"])],
                   chatgpt_analysis=False, query="x", film=film)
    assert [c[2]["addon"] for c in server.calls] == [film]
    assert server.calls[0][2]["run_id"].startswith(film[0])


def test_a_motion_design_run_is_tagged_motion(server):
    cfg = C.load()
    automation.run({}, cfg, custom_stages=[("storyboard", "Claude", ["Plan it"])],
                   motion_design_stage="storyboard", chatgpt_analysis=False, query="x")
    assert server.calls and {c[2]["addon"] for c in server.calls} == {"motion"}


def test_a_follow_up_edit_is_tagged_reel(server):
    run()
    spec = server.rendered[-1]
    server.calls.clear()
    server_reply = {"scenes": [{"scene": 2, "css": ".z{}", "html": "<div>warmer</div>"}]}
    ai.set_transport(lambda p, b, t: server.calls.append(("follow", text_of(b), b))
                     or {"text": json.dumps(server_reply), "charged_mc": 1})
    automation.studio_followup(C.load(), spec, "Claude", "", "make scene 2 warmer")
    body = server.calls[0][2]
    assert body["addon"] == "reel" and body["run_id"] and body["max_tokens"] == 8000
    assert "effort" not in body, "the person's own change keeps full thinking"
    assert server.rendered[-1]["scenes"][1]["html"] == "<div>warmer</div>"
    assert ai.film_state() == ("", "")


# ── what a turn carries ─────────────────────────────────────────────────────

def test_a_scene_turn_carries_the_brief_the_look_and_only_its_neighbour(server):
    run()
    text = server.of("scene4")[0][1]
    assert "ART DIRECTOR" in text and '"storyboard"' in text and "--ink:#111" in text
    assert "MARK3" in text and "MARK1" not in text and "MARK2" not in text
    assert text.count("THIS IS SCENE") == 1, "earlier scene requests are not replayed"
    assert "THE SCENE BEFORE THIS ONE (scene 3)" in text


def test_a_long_neighbour_is_shortened_but_the_scene_being_fixed_is_whole(server):
    run({"neighbour_chars": 200})
    assert "only the opening of it is shown" in server.of("scene2")[0][1]
    fix = server.of("fix")[0][1]
    target = fix.split("SCENE 3 AS IT STANDS NOW:")[1].split("Now the user writes")[0]
    assert "MARK3" in target and "/*c*/" * 1500 in target
    assert "only the opening" not in target


def test_the_reel_is_assembled_from_the_replies_and_the_check_still_runs(server):
    responses, links = run()
    spec = server.rendered[-1]
    assert [re.search(r"MARK\d", s["html"]).group() for s in spec["scenes"]] == [
        "MARK1", "MARK2", "MARK3", "MARK4"]
    assert "FIXED" in spec["scenes"][2]["html"], "the layout fix was kept"
    assert spec["design"]["css"].startswith(":root{--ink:#111}")
    assert any("FIXED" in h for h in server.checked) and "media" in links


def test_a_prose_reply_is_asked_again_with_the_request_it_failed(server):
    server.bad = {2}
    run()
    retry = server.of("retry")[0][1]
    assert "THIS IS SCENE 2 OF" in retry and "just prose" in retry
    assert "MARK2" in server.rendered[-1]["scenes"][1]["html"]


def test_only_the_failed_scene_is_asked_again_in_full(server):
    server.fail = {2: 1}
    run()
    assert [len(server.of(f"scene{n}")) for n in range(1, N + 1)] == [1, 2, 1, 1]
    assert len(server.of("design")) == 1 and not server.of("retry")
    assert "MARK2" in server.rendered[-1]["scenes"][1]["html"]


def test_a_scene_that_keeps_failing_costs_one_plain_scene_not_the_reel(server):
    server.fail = {2: 5}
    run()
    assert len(server.of("scene2")) == 2 and len(server.of("design")) == 1
    scenes = server.rendered[-1]["scenes"]
    assert len(scenes) == N and "class='fb'" in scenes[1]["html"]


# ── cache, limits, effort ───────────────────────────────────────────────────

def marked(body):
    c = body["messages"][-1]["content"]
    return [] if isinstance(c, str) else [b for b in c if b.get("cache_control")]


def test_the_same_start_is_marked_for_the_cache_on_every_scene_turn(server):
    run()
    starts = []
    for n in range(1, N + 1):
        body = server.of(f"scene{n}")[0][2]
        (block,) = marked(body)
        assert block["cache_control"] == {"type": "ephemeral"}
        assert body["messages"][-1]["content"][0] is block
        starts.append(block["text"])
    assert len(set(starts)) == 1 and marked(server.of("fix")[0][2])[0]["text"] == starts[0]
    assert not marked(server.of("design")[0][2]) and not marked(server.of("script")[0][2])


def test_limits_and_effort_by_kind_of_turn(server):
    run()
    def asked(kind):
        b = server.of(kind)[0][2]
        return b.get("max_tokens"), b.get("effort")
    assert asked("script") == (6000, None)
    assert asked("briefs") == (6000, "low")
    assert asked("design") == (8000, None)
    assert asked("scene1") == (8000, "low")
    assert asked("fix") == (8000, "low")


def test_a_lower_server_limit_is_obeyed(server):
    sent = []

    def send(path, body, bearer):
        sent.append(dict(body))
        if "max_tokens" in body:
            raise ai.AiError("REQUEST_TOO_LARGE", "too big", {"limit": "max_output_tokens", "max": 4000})
        return {"text": "ok"}

    ai.set_transport(send)
    assert ai.chat("code", "x", max_tokens=8000) == "ok"
    assert len(sent) == 2 and "max_tokens" not in sent[1]


# ── the switches ────────────────────────────────────────────────────────────

def test_all_off_sends_what_was_sent_before(server):
    server.fail = {2: 1}
    run(False)
    for _kind, _text, body in server.calls:
        assert not {"max_tokens", "effort"} & set(body)
        if "messages" in body:
            assert not marked(body)
    last = server.of("scene4")[0][1]
    assert "MARK1" in last and "MARK2" in last and "MARK3" in last
    assert "Send scene 2 again" in last and server.of("retry")
    assert {c[2]["addon"] for c in server.calls} == {"reel"}, "tags are not a saving"


@pytest.mark.parametrize("switch", ["trim_history", "prompt_cache", "turn_caps", "low_effort"])
def test_one_switch_off_leaves_the_others_on(server, switch):
    run({switch: False})
    text, body = server.of("scene4")[0][1:]
    assert ("MARK1" in text) is (switch == "trim_history")
    assert bool(marked(body)) is (switch != "prompt_cache")
    assert ("max_tokens" in body) is (switch != "turn_caps")
    assert ("effort" in body) is (switch != "low_effort")


def test_the_marked_text_is_the_same_text(server):
    run({"prompt_cache": False})
    plain = [server.of(f"scene{n}")[0][1] for n in range(2, N + 1)]
    server.calls.clear()
    run()
    # (the brief names a different look each run, so compare what follows it)
    after = lambda t: t.split("(Only the earlier turns")[1]
    assert [after(server.of(f"scene{n}")[0][1]) for n in range(2, N + 1)] == [
        after(t) for t in plain]


def test_a_browser_tab_is_told_nothing():
    def ask(prompt, expect=""):
        return ""
    SS.aim_turn(ask, "scene", [{}], 1)      # no `aim` on it: nothing happens
    assert not hasattr(ask, "aim")
