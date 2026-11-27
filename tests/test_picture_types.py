"""A picture is declared as what its bytes are. FLUX answers with JPEG; Prism
saved it as .png and sent data:image/png, which Anthropic refuses with HTTP 400
(27 Nov 2026, a real run).

Run from the engine root:  HOME=$(mktemp -d) python -m pytest tests/test_picture_types.py
"""
import base64
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                                  # noqa: E402

from core import ai_client, config as C, files, image_gen, server_session  # noqa: E402


def picture(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 80), (30, 30, 30)).save(buf, fmt)
    return buf.getvalue()


FORMATS = [("JPEG", "image/jpeg", ".jpg"), ("PNG", "image/png", ".png"),
           ("WEBP", "image/webp", ".webp")]


@pytest.mark.parametrize("fmt,mime,ext", FORMATS)
def test_the_type_is_read_from_the_bytes(tmp_path, fmt, mime, ext):
    wrong = tmp_path / ("wrong.png" if ext != ".png" else "wrong.jpg")
    wrong.write_bytes(picture(fmt))
    att = files.attach(str(wrong))
    assert att["mime"] == mime
    block = server_session._attachment_blocks([att])
    assert ai_client._content("look", block)[1]["image_url"]["url"].startswith(
        f"data:{mime};base64,")


def test_a_wrong_label_is_corrected_when_attached():
    att = {"kind": "image", "mime": "image/png",
           "b64": base64.b64encode(picture("JPEG")).decode()}
    url = ai_client._content("look", [att])[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,/9j/")


@pytest.mark.parametrize("fmt,mime,ext", FORMATS)
def test_a_drawn_picture_is_saved_with_its_real_ending(tmp_path, monkeypatch, fmt, mime, ext):
    monkeypatch.setattr(C, "RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(image_gen, "build_prompts", lambda *a, **k: ["a hall at dusk"])
    monkeypatch.setattr(image_gen, "build_single_prompt", lambda *a, **k: "a hall")
    monkeypatch.setattr(image_gen._openrouter, "openrouter_image", lambda *a, **k: picture(fmt))
    cfg = {"openrouter_api_key": "k"}
    paths, note = image_gen.generate_artwork("a reel", [], cfg, film=True)
    assert [os.path.splitext(p)[1] for p in paths] == [ext], note
    one, note = image_gen.generate("a post", [], cfg)
    assert os.path.splitext(one)[1] == ext, note
    assert files.attach(one)["mime"] == mime


def test_a_logo_placed_on_a_jpeg_leaves_one_true_png(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(image_gen, "build_single_prompt", lambda *a, **k: "a hall")
    monkeypatch.setattr(image_gen._openrouter, "openrouter_image", lambda *a, **k: picture("JPEG"))
    logo = tmp_path / "logo.png"
    mark = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    mark.paste((200, 0, 0, 255), (5, 5, 30, 15))
    mark.save(logo)
    monkeypatch.setattr(image_gen, "_own_logo", lambda attachments: str(logo))
    out, note = image_gen.generate("a post", [], {"openrouter_api_key": "k"}, [str(logo)])
    assert out.endswith(".png") and "logo placed" in note, note
    assert files.attach(out)["mime"] == "image/png"
    assert os.listdir(tmp_path / "runs") == [os.path.basename(out)]
