"""On Prism's server a follow-up starts a new conversation, so it must carry the
filmed scene code and shared stylesheet; a browser design chat already has them.

Run from the engine root:  HOME=$(mktemp -d) python -m pytest tests/test_followup_sees_the_code.py
"""
import json

from test_reel_cost import run, server, text_of                       # noqa: F401
from core import ai_client as ai, automation, config as C, reel_web


def test_a_server_follow_up_is_sent_the_scene_code_and_shared_css(server):
    run()
    spec = server.rendered[-1]
    sent = []

    def transport(path, body, bearer):
        sent.append(text_of(body))
        return {"text": json.dumps({"scenes": [{"scene": 2, "css": ".z{}",
                                                "html": "<div>calmer</div>"}]}),
                "charged_mc": 1}

    ai.set_transport(transport)
    automation.studio_followup(C.load(), spec, "Claude", "", "Make scene 2 calmer")
    first = sent[0]
    assert spec["design"]["css"] in first
    for scene in spec["scenes"]:
        assert scene["html"] in first and scene["css"] in first
    assert server.rendered[-1]["scenes"][0]["html"] == spec["scenes"][0]["html"]


def test_a_browser_design_chat_is_not_sent_the_code_again():
    spec = {"scenes": [{"html": "<div>UNIQUEMARK</div>", "css": ".z{}"}], "design": {"css": ":root{}"}}
    text = reel_web.followup_instructions("calmer", spec)
    assert "THE CODE AS FILMED" not in text and ".z{}" not in text


def test_a_very_long_reel_is_cut_at_a_whole_scene():
    spec = {"scenes": [{"html": "a" * 50_000, "css": ""}, {"html": "b" * 50_000, "css": ""}], "design": {}}
    text = reel_web.followup_instructions("calmer", spec, with_code=True)
    assert "a" * 50_000 in text and "b" * 1000 not in text and "Scenes 2 onward" in text
