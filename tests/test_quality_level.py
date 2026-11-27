"""The quality pin and the quality meta of an answer (ai_client).

Run from the engine root:  python -m pytest tests/test_quality_level.py
No network: the transport is a fake of the server's /v1/ai/* answers.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ai_client as ai   # noqa: E402


@pytest.fixture
def server():
    sent, heard = [], []
    answer = {"text": "ok", "charged_mc": 5, "balance_mc": 100,
              "model": {"key": "m", "label": "Claude Sonnet"},
              "level_used": "balanced", "stepped_down": True, "bar_pct": 62}

    def send(path, body, bearer):
        sent.append((path, body))
        if path == ai.PATH_IMAGE:
            return {"images": ["aGk="], "charged_mc": 1, "balance_mc": 99,
                    "level_used": "best", "stepped_down": False, "bar_pct": 62,
                    "model": "flux"}
        return dict(answer)

    ai.set_auth_provider(lambda: {"base_url": "x", "license_id": "L",
                                  "device_fp": "d", "app_version": "1", "lease": "t"})
    ai.set_transport(send)
    ai.set_usage_listener(heard.append)
    ai.set_level(None)
    yield sent, heard, answer
    ai.set_level(None)
    ai.set_meta_hook(None)
    ai.set_transport(None)
    ai.set_auth_provider(None)
    ai.set_usage_listener(None)


def test_unset_quality_sends_no_level(server):
    sent, _, _ = server
    ai.chat("writer", "hi")
    assert "level" not in sent[0][1]


@pytest.mark.parametrize("pin", ["fast", "balanced", "best", " Best "])
def test_a_pin_is_sent_on_chat_stream_and_image(server, pin):
    sent, _, _ = server
    ai.set_level(pin)
    ai.chat("writer", "hi")
    "".join(ai.stream("writer", "hi"))
    ai.image("a cat")
    assert [b["level"] for _, b in sent] == [pin.strip().lower()] * 3


@pytest.mark.parametrize("pin", ["auto", "", None, "ultra"])
def test_auto_or_nonsense_means_unset(server, pin):
    sent, _, _ = server
    ai.set_level("fast")
    ai.set_level(pin)
    assert ai.get_level() is None
    ai.chat("writer", "hi")
    assert "level" not in sent[0][1]


def test_the_listener_hears_model_level_stepdown_and_bar(server):
    _, heard, _ = server
    ai.chat("writer", "hi")
    note = heard[0]
    assert (note["model"], note["level_used"], note["stepped_down"],
            note["bar_pct"]) == ("Claude Sonnet", "balanced", True, 62.0)


def test_an_older_server_with_no_meta_is_not_an_error(server):
    _, heard, answer = server
    for k in ("level_used", "stepped_down", "bar_pct"):
        answer.pop(k)
    ai.chat("writer", "hi")
    assert heard[0]["level_used"] == "" and heard[0]["stepped_down"] is False
    assert heard[0]["bar_pct"] is None


def test_the_meta_hook_hears_each_answer_and_can_be_cleared(server):
    got = []
    ai.set_meta_hook(got.append)
    ai.chat("writer", "hi")
    ai.set_meta_hook(None)
    ai.chat("writer", "hi")
    assert len(got) == 1 and got[0]["level_used"] == "balanced"


def test_the_summary_line():
    metas = [{"model": "A", "level_used": "best", "stepped_down": False, "bar_pct": 10.0},
             {"model": "B", "level_used": "balanced", "stepped_down": True, "bar_pct": 61.0},
             {"model": "C", "level_used": "balanced", "stepped_down": True, "bar_pct": 63.0}]
    s = ai.summarise_quality(metas)
    assert s["level_used"] == "balanced" and s["model"] == "C"
    assert s["stepped_down"] is True and s["bar_pct"] == 63.0
    assert ai.quality_line(s) == "Made with Balanced quality (C)"
    assert ai.summarise_quality([]) == {}


def test_model_id_is_the_fallback_and_the_label_wins():
    assert ai.meta_of({"model": {"key": "k", "label": "Claude Haiku"}, "model_id": "k",
                       "level_used": "fast"})["model"] == "Claude Haiku"
    assert ai.meta_of({"model_id": "anthropic/x", "level_used": "fast"})["model"] == "anthropic/x"


def test_a_full_bar_is_said_plainly_with_the_choices(server):
    ai.set_transport(None)
    import pytest as _p
    with _p.raises(ai.AiError) as e:
        ai._raise_for(403, {"error": {"code": "AI_NOT_AVAILABLE", "message": "no",
                                      "detail": {"reason": "bar_full", "offer_topup": True,
                                                 "bar_pct": 100, "week_resets_at": 1796083200}}})
    assert e.value.code == "AI_NOT_AVAILABLE" and e.value.detail["reason"] == "bar_full"
    assert "top-up" in str(e.value) and "simple Reel" in str(e.value) and "resets" in str(e.value)
