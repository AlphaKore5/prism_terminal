"""
Prism — OpenRouter API calls ("Prism Agent", BOQ-CHANGES-TODO.md item 30)
───────────────────────────────────────────────────────────────────────
The BOQ add-on's write-up stage has always run by driving a real tool in a
browser (Claude, ChatGPT, Grok, ...). Item 30b adds a second path: a plain
API call through OpenRouter, for the two agents BOQ now also offers,
"Prism Agent Claude" and "Prism Agent Deep" (addons/boq/agents.py).

Mirrors router.py's own groq_chat() on purpose — same retry-on-429/5xx
shape, same rule that a failure raises with the server's own words rather
than a paraphrase, so the two API paths behave the same way to whatever
reads their exceptions.
"""
from __future__ import annotations

import base64
import json
import time

import requests

from . import ai_client as _ai

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# A text model's answer and a generated picture are different jobs on
# OpenRouter's own API, not one endpoint with a flag -- confirmed against
# OpenRouter's own docs (2026-11-07): /chat/completions returns prose in
# choices[0].message.content; /images returns the picture itself, base64,
# in data[0].b64_json. core.image_gen (FLUX 3 Klein, item: "replace the
# ChatGPT image stage") is the one caller of IMAGES_URL.
IMAGES_URL = "https://openrouter.ai/api/v1/images"
_RETRY_STATUS = (429, 500, 502, 503, 504)


def _content_blocks(prompt: str, attachments: list[dict] | None) -> str | list[dict]:
    """A plain string when there is nothing to show the model (the common
    case, and every call before item 32) — or OpenRouter's own multimodal
    content-array shape once there's a rendered drawing to go with the
    text. Each item in `attachments` is cad_render.prepare_for_ai()'s own
    shape: {"kind": "image"|"pdf", "mime", "b64", "name"}.

    Image -> an `image_url` block with a data: URI, OpenAI's (and so
    OpenRouter's) documented shape for a vision model. PDF -> a `file`
    block with `file_data`, OpenRouter's own documented shape for a real
    document a compatible model (Claude) reads natively, not a picture of
    it. NOT verified against a live call yet — built from OpenRouter's
    published API shape, the same honesty this file already asks of
    everything it has not run for real (BOQ-CHANGES-TODO.md item 32's own
    "still open" note)."""
    if not attachments:
        return prompt
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    for att in attachments:
        uri = f"data:{att['mime']};base64,{att['b64']}"
        if att["kind"] == "pdf":
            blocks.append({"type": "file",
                           "file": {"filename": att.get("name", "drawing.pdf"),
                                    "file_data": uri}})
        else:
            blocks.append({"type": "image_url", "image_url": {"url": uri}})
    return blocks


def openrouter_chat(api_key: str, model: str, prompt: str, *,
                     attachments: list[dict] | None = None,
                     temperature: float = 0.3, timeout: int = 180,
                     retries: int = 1, max_tokens: int | None = None,
                     extra: dict | None = None,
                     usage_out: dict | None = None,
                     usage_sink: dict | None = None,
                     role: str = "writer", addon: str = "core",
                     feature: str = "") -> str:
    """One OpenRouter completion. `model` is an OpenRouter model id, e.g.
    "anthropic/claude-sonnet-5.5" — addons/boq/agents.py owns the mapping
    from a Prism Agent's display name to the model id actually sent here.

    `attachments`: rendered drawings from addons/boq/cad_render.py's own
    prepare_for_ai(), to actually show the model (item 32) — never the
    raw DXF/DWG bytes, and only ever sent after the SAME consent gate
    (addons/boq/dialog.py's _drawing_consent()) a browser agent's own
    drawing upload already goes through; nothing here decides whether a
    drawing may be sent at all.

    `max_tokens`: left unset keeps every existing caller's behaviour
    (no cap) — found live testing a vision model on a drawing tile
    (hvac_takeoff's own AI tag-reading, item 2): with no cap at all, a
    model can loop repeating the same lines for minutes before stopping
    itself, which a short reading task never needs. `extra`: merged
    straight into the request body (e.g. {"reasoning": {"enabled":
    False}}) — found on the SAME call: a "thinking" model spent its
    whole token budget on its own chain-of-thought and returned an
    empty final answer (finish_reason "length") until reasoning was
    turned off. Neither parameter is read by any caller before this
    one asked for it, so every existing call keeps behaving exactly as
    it always has.

    `usage_out`: when given, filled in place with whatever `usage` object
    OpenRouter's own response carried (`prompt_tokens`/`completion_tokens`/
    `total_tokens`) — a BOQ counter (addons/boq/history.py) sums these
    across runs for the token-usage figure it shows. A mutated-in-place
    dict rather than a second return value, so this stays a plain string
    return for the many existing callers that already match on that.

    When the AI seam is active (core.ai_client) the call goes to Prism's
    licence server under `role`; `api_key` and `model` are not used (the model
    is a hint), and `extra` (provider-side switches) is not sent."""
    if _ai.active():
        return _ai.chat(role, prompt, addon=addon, feature=feature or role,
                        attachments=attachments, temperature=temperature,
                        json_mode=bool((extra or {}).get("response_format")),
                        max_tokens=max_tokens, timeout=timeout, retries=retries,
                        usage_out=usage_out, usage_sink=usage_sink,
                        model_hint=model)
    if not api_key:
        raise RuntimeError(
            "No OpenRouter key set. Add one in Settings → AI tools.")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter's own docs ask for these on every call — identifies
        # the app in OpenRouter's own dashboards; a request with neither
        # still works, just shows up as anonymous there.
        "HTTP-Referer": "https://prism.app",
        "X-Title": "Prism",
    }
    content = _content_blocks(prompt, attachments)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if extra:
        payload.update(extra)
    if usage_sink is not None:
        # `usage_sink` is filled with OpenRouter's own usage block (tokens and
        # `cost` in dollars) so a caller can keep a running spend and stop at a cap.
        payload["usage"] = {"include": True}
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload,
                                 timeout=timeout)
        except requests.RequestException as e:
            raise RuntimeError(
                f"Couldn't reach OpenRouter — check your internet connection. "
                f"({e})") from e

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.status_code == 200 and body.get("choices"):
            if usage_sink is not None:
                usage_sink.update(body.get("usage") or {})
            msg = body["choices"][0]["message"]
            if usage_out is not None:
                usage_out.update(body.get("usage") or {})
                # Whether the answer is whole: "length" means the model hit
                # its output limit and the text stops mid-way; a null
                # `content` means the string returned below is the model's
                # own working notes, not an answer. A caller that saves the
                # result as a document reads these before trusting it.
                usage_out["finish_reason"] = body["choices"][0].get("finish_reason")
                usage_out["answer_from_reasoning"] = not msg.get("content")
            # A "thinking" model can spend its whole token budget on its
            # own reasoning and return a null `content` (finish_reason
            # "length") -- fall back to the reasoning text itself rather
            # than hand every caller a None where they expect a string.
            return msg.get("content") or msg.get("reasoning") or ""

        if resp.status_code in _RETRY_STATUS and attempt < retries:
            delay = 5
            try:
                delay = min(int(float(resp.headers.get("retry-after", 5))), 20)
            except (TypeError, ValueError):
                pass
            time.sleep(delay)
            continue

        if resp.status_code == 429:
            raise RuntimeError(
                "OpenRouter is rate-limiting your key. Wait a minute and "
                "try again, or raise your limits at openrouter.ai.")
        if resp.status_code == 401:
            raise RuntimeError(
                "OpenRouter rejected your API key. Re-enter it in "
                "Settings → AI tools.")
        err = None
        if isinstance(body, dict):
            err = (body.get("error") or {}).get("message")
        last = err or f"HTTP {resp.status_code}: {json.dumps(body)[:300]}"
        raise RuntimeError(f"OpenRouter API error ({last})")

    raise RuntimeError(f"OpenRouter did not answer. ({last})")


def openrouter_image(api_key: str, model: str, prompt: str, *,
                     aspect_ratio: str = "1:1", resolution: str = "1K",
                     timeout: int = 180, retries: int = 1,
                     addon: str = "core", feature: str = "image") -> bytes:
    """One OpenRouter image generation call -- the dedicated Images API
    (POST /api/v1/images), not /chat/completions: this model's deliverable
    IS the image, not a text reply describing one. `model` is an
    OpenRouter image-model id, e.g. "black-forest-labs/flux-3-image-klein".
    Returns the raw image bytes (decoded from the response's own base64),
    ready to write straight to a .png file.

    Same retry-on-429/5xx shape and the same rule that a failure raises
    with the server's own words, as openrouter_chat -- this is its sibling
    for a provider that answers with pixels instead of tokens.

    When the AI seam is active the picture is made by Prism's licence server
    (role "image"); `api_key` and `model` are not used."""
    if _ai.active():
        return _ai.image(prompt, addon=addon, feature=feature,
                         aspect_ratio=aspect_ratio, resolution=resolution,
                         timeout=timeout, retries=retries, model_hint=model)
    if not api_key:
        raise RuntimeError(
            "No OpenRouter key set. Add one in Settings → AI tools.")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://prism.app",
        "X-Title": "Prism",
    }
    payload = {"model": model, "prompt": prompt, "n": 1,
              "aspect_ratio": aspect_ratio, "resolution": resolution}
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(IMAGES_URL, headers=headers, json=payload,
                                 timeout=timeout)
        except requests.RequestException as e:
            raise RuntimeError(
                f"Couldn't reach OpenRouter — check your internet connection. "
                f"({e})") from e

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.status_code == 200:
            data = body.get("data") or []
            b64 = data[0].get("b64_json", "") if data else ""
            if not b64:
                raise RuntimeError(
                    "OpenRouter answered but sent no image data.")
            return base64.b64decode(b64)

        if resp.status_code in _RETRY_STATUS and attempt < retries:
            delay = 5
            try:
                delay = min(int(float(resp.headers.get("retry-after", 5))), 20)
            except (TypeError, ValueError):
                pass
            time.sleep(delay)
            continue

        if resp.status_code == 429:
            raise RuntimeError(
                "OpenRouter is rate-limiting your key. Wait a minute and "
                "try again, or raise your limits at openrouter.ai.")
        if resp.status_code == 401:
            raise RuntimeError(
                "OpenRouter rejected your API key. Re-enter it in "
                "Settings → AI tools.")
        err = None
        if isinstance(body, dict):
            err = (body.get("error") or {}).get("message")
        last = err or f"HTTP {resp.status_code}: {json.dumps(body)[:300]}"
        raise RuntimeError(f"OpenRouter API error ({last})")

    raise RuntimeError(f"OpenRouter did not answer. ({last})")


def verify_key(api_key: str, model: str = "") -> str:
    """Check an OpenRouter key against OpenRouter. Returns "" if it works,
    else why not — mirrors router.verify_key's own shape (same reasoning:
    ask this the moment the key is entered, not on the first real task)."""
    api_key = (api_key or "").strip()
    if not api_key:
        return "No key entered."
    if not api_key.startswith("sk-or-"):
        return ("An OpenRouter key starts with 'sk-or-' — this doesn't "
                "look like one.")
    # /models is public: it answers 200 to any key, even a made-up one, so it
    # proves nothing about the key. /key is the one that checks it (401 if bad).
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
    except Exception as e:                                  # noqa: BLE001
        return (f"Couldn't reach OpenRouter to check the key ({e}).\nIf "
                "you're behind a proxy or offline, the key may still be fine.")
    if resp.status_code == 401:
        return ("OpenRouter rejected this key. Make a new one at "
                "openrouter.ai/keys and paste it again — copying it twice "
                "by accident is the usual cause.")
    if resp.status_code == 429:
        return ("The key is valid, but OpenRouter is rate-limiting it "
                "right now. Writing may be slow until that clears.")
    if resp.status_code != 200:
        return (f"OpenRouter answered HTTP {resp.status_code} — the key "
                "may not be usable.")
    if model:
        try:
            listing = requests.get("https://openrouter.ai/api/v1/models", timeout=20)
            ids = {m.get("id") for m in listing.json().get("data", [])}
            if ids and model not in ids:
                return (f"The key works, but '{model}' isn't on OpenRouter's "
                        "current model list.")
        except Exception:                                   # noqa: BLE001
            pass
    return ""
