"""The names the design step is given for the pictures are the names the film is
rendered with.

27 Nov 2026, a real run: the design step was told logo=picture 1, art1..art4 =
pictures 2..5, but the renderer named the same files again without knowing Prism
had drawn them, put a cut-out in the logo slot and shifted every other name, so
scenes showed the wrong picture and text landed on photographs.

Run from the engine root:  HOME=$(mktemp -d) python -m pytest tests/test_reel_picture_names.py
"""
import base64
import io
import os
import re

from PIL import Image, ImageDraw

from test_reel_cost import AGENTS, ROUTING, run, server, text_of   # noqa: F401
from core import ai_client as ai


def _picture(i: int, fmt: str = "JPEG") -> str:
    """Pictures 1-3 are whole scene plates; 4 onward a small subject on white,
    which Prism cuts out (the file the renderer then took for the logo)."""
    if i <= 3:
        im = Image.effect_noise((320, 560), 40 + i * 10).convert("RGB")
    else:
        im = Image.new("RGB", (400, 500), (255, 255, 255))
        ImageDraw.Draw(im).ellipse((170, 220, 210 + i, 260 + i), fill=(180, 140, 40))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode()


def test_the_film_is_rendered_with_the_names_the_design_step_saw(server, monkeypatch):
    drawn = []

    def transport(path, body, bearer):
        if path == ai.PATH_IMAGE:
            drawn.append(body)
            return {"images": [_picture(len(drawn))], "charged_mc": 1}
        return server(path, body, bearer)

    ai.set_transport(transport)
    monkeypatch.setitem(AGENTS, "artwork", "Prism Image")
    import time
    clock = iter(range(1_800_000_000, 1_800_001_000))
    monkeypatch.setattr(time, "time", lambda: next(clock))     # one file name per picture
    from core import assets
    shown = {}
    real = assets.manifest

    def manifest(table):
        if table:
            shown.update({k: os.path.basename(v["source"]) for k, v in table.items()})
        return real(table)

    monkeypatch.setattr(assets, "manifest", manifest)
    routing = dict(ROUTING, artwork={"needed": True, "questions": ["Draw the pictures"]})
    run(routing=routing)

    assert len(drawn) >= 4 and shown, (len(drawn), shown)
    rendered = {k: os.path.basename(v["source"])
                for k, v in server.rendered[-1]["_assets"].items()}
    assert rendered == shown
