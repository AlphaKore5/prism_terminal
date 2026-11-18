"""
Prism -- the one door every AI call goes through
────────────────────────────────────────────────
Prism no longer asks a customer for an OpenRouter or Groq key. Every model call
(planner, BOQ write-up, drawing reading, STEP, quotation text, pictures, the
Motion critic) goes to Prism's licence server, /v1/ai/*, which holds the one
provider key, charges the customer's AI credits and makes the call. This
machine holds no provider secret.

The old entry points (router.groq_chat, openrouter.openrouter_chat,
openrouter.openrouter_image) keep their signatures and their callers; when the
seam is active they hand the same prompt to this module instead of to a
provider. Behaviour and prompts do not change; only the transport does.

When is the seam active?
  - the host app (the GUI) has registered an auth provider with
    `set_auth_provider()` -- the licence, the seat and the signed lease live
    in the GUI, not in this package; and
  - PRISM_AI_DIRECT is not set. PRISM_AI_DIRECT=1 is a TEMPORARY developer
    switch that puts the old direct path (the keys in ~/.prism) back.
The terminal CLI never registers a provider, so it keeps running on its own
keys exactly as before.

The `role` names the job, the server maps it to a model; model ids that
callers still pass are hints and are not sent (the server owns the choice).
Roles: planner, classify, writer, reasoner, vision, code, search, summary,
image, boq.agent_a, boq.agent_b, boq.tag_vision.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from typing import Callable, Iterator, Optional

import requests

# A value that stands in for "a provider key" so the many `if not api_key`
# guards in the app still pass. It is never sent anywhere and never saved.
AI_KEY = "prism-ai"
# The config fields a customer used to paste a provider key into.
KEY_FIELDS = ("api_key", "openrouter_api_key")

PATH_STATUS = "/v1/ai/status"
PATH_ESTIMATE = "/v1/ai/estimate"
PATH_CHAT = "/v1/ai/chat"
PATH_STREAM = "/v1/ai/chat/stream"
PATH_IMAGE = "/v1/ai/image"
PATH_RUN_COST = "/v1/ai/run-cost"
PATH_USAGE = "/v1/ai/usage"

_RETRY_STATUS = (500, 502, 503, 504)

_auth_provider: Optional[Callable[[], dict]] = None
_send: Optional[Callable] = None        # tests: fn(path, body, bearer) -> dict
_run = threading.local()


class AiError(RuntimeError):
    """The server refused an AI call, or could not be reached. `str(e)` is
    customer-facing copy; `code` is the server's error code (or one of
    "UNREACHABLE", "NOT_SET_UP", "NO_AUTH")."""

    def __init__(self, code: str, message: str, detail: dict | None = None,
                 status: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.status = status


# ── mode ─────────────────────────────────────────────────────────────────────

def direct_requested() -> bool:
    """TEMPORARY dev switch: PRISM_AI_DIRECT=1 puts the old direct path back."""
    return os.environ.get("PRISM_AI_DIRECT", "").strip().lower() in (
        "1", "true", "yes", "on")


def set_auth_provider(fn: Optional[Callable[[], dict]]) -> None:
    """The host app tells this package who the customer is. `fn()` returns
    {"base_url", "license_id", "device_fp", "app_version", "lease",
     "channel", "platform"}; it may raise AiError. None un-registers it."""
    global _auth_provider
    _auth_provider = fn


_listener: Optional[Callable[[dict], None]] = None


def set_usage_listener(fn: Optional[Callable[[dict], None]]) -> None:
    """The host app hears every answer's balance and charge (no prompt, no
    text), so a credits chip can stay current without asking again."""
    global _listener
    _listener = fn


def _heard(data: dict) -> None:
    if _listener is not None and isinstance(data, dict) and "balance_mc" in data:
        try:
            _listener({"balance_mc": data.get("balance_mc"),
                       "charged_mc": data.get("charged_mc", 0)})
        except Exception:                                   # noqa: BLE001
            pass            # a display must never break a call


def set_transport(fn: Optional[Callable]) -> None:
    """Tests only: answer every non-streaming call with fn(path, body, bearer)
    -> dict (or raise AiError). None restores the real HTTP call."""
    global _send
    _send = fn


def active() -> bool:
    """True when AI calls go through Prism's server."""
    return _auth_provider is not None and not direct_requested()


def is_server_key(key) -> bool:
    return key == AI_KEY


def set_run_id(run_id: str) -> None:
    """Tag this thread's following calls with the app's run id, so the server
    can total a run's cost (History badges)."""
    _run.id = "".join(c for c in (run_id or "") if c.isalnum() or c in "_.:-")[:40]


def _run_id() -> str:
    return getattr(_run, "id", "")


# ── the wire ─────────────────────────────────────────────────────────────────

_OFFLINE = ("Couldn't reach Prism's server -- check your internet connection "
            "and try again. Nothing was charged.")
_OUTDATED = ("AI isn't switched on at Prism's server yet. Nothing was charged -- "
             "ask Alphakore.")


def _auth() -> dict:
    if _auth_provider is None:
        raise AiError("NO_AUTH", "Prism's AI needs the Prism app and a licence.")
    return _auth_provider()


def _headers(a: dict, bearer: bool) -> dict:
    h = {"X-Prism-Version": str(a.get("app_version", "")),
         "X-Prism-Platform": str(a.get("platform", "")),
         "X-Prism-Channel": str(a.get("channel", "prod")),
         "Content-Type": "application/json"}
    if bearer:
        h["Authorization"] = "Bearer " + str(a.get("lease", ""))
    return h


def _body(a: dict, extra: dict) -> dict:
    out = {"license_id": a["license_id"], "device_fp": a["device_fp"],
           "app_version": str(a.get("app_version", "")), "api": 1}
    out.update(extra)
    return out


def _raise_for(status: int, data) -> None:
    err = (data or {}).get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("code"):
        raise AiError(str(err["code"]), str(err.get("message") or ""),
                      err.get("detail") if isinstance(err.get("detail"), dict) else {},
                      status)
    if status == 404:
        raise AiError("NOT_SET_UP", _OUTDATED, status=status)
    raise AiError(f"HTTP_{status}",
                  "Prism's server had a problem. Nothing was charged -- "
                  "please try again in a moment.", status=status)


def _post(path: str, extra: dict, *, bearer: bool, timeout: int,
          retries: int = 1) -> dict:
    a = _auth()
    body = _body(a, extra)
    if _send is not None:
        return _send(path, body, a.get("lease", "") if bearer else "")
    url = str(a["base_url"]).rstrip("/") + path
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=body, headers=_headers(a, bearer),
                                 timeout=timeout)
        except requests.RequestException as e:
            last = e
            if attempt < retries:
                time.sleep(2)
                continue
            raise AiError("UNREACHABLE", _OFFLINE) from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code < 400:
            return data
        if resp.status_code in _RETRY_STATUS and attempt < retries:
            time.sleep(2)
            continue
        _raise_for(resp.status_code, data)
    raise AiError("UNREACHABLE", _OFFLINE) from last


# ── building a request ───────────────────────────────────────────────────────

def _content(prompt: str, attachments: list[dict] | None):
    """The same message content OpenRouter takes: a string, or text plus image
    / PDF parts (the shapes core.openrouter._content_blocks has always built)."""
    if not attachments:
        return prompt
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    for att in attachments:
        uri = f"data:{att['mime']};base64,{att['b64']}"
        if att["kind"] == "pdf":
            blocks.append({"type": "file", "file": {
                "filename": att.get("name", "drawing.pdf"), "file_data": uri}})
        else:
            blocks.append({"type": "image_url", "image_url": {"url": uri}})
    return blocks


def _ident(text: str, default: str, limit: int) -> str:
    keep = "".join(c for c in (text or "").lower() if c.isalnum() or c in "_.:-")
    return (keep or default)[:limit]


def _addon(text: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_"
                   for c in (text or "core").lower())[:24] or "core"


def _call_fields(role: str, addon: str, feature: str, artifact_ref: str) -> dict:
    return {"idem_key": uuid.uuid4().hex, "role": role,
            "addon": _addon(addon),
            "feature": _ident(feature, role, 40),
            "run_id": _run_id(),
            "artifact_ref": _ident(artifact_ref, "", 64)}


def _chat_body(role, prompt, attachments, *, addon, feature, artifact_ref,
               json_mode, temperature, max_tokens, system) -> dict:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": _content(prompt, attachments)})
    body = _call_fields(role, addon, feature, artifact_ref)
    body["messages"] = msgs
    if json_mode:
        body["json_mode"] = True
    if temperature is not None:
        body["temperature"] = max(0.0, min(1.2, float(temperature)))
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    return body


def _fill_usage(sink: dict | None, data: dict) -> None:
    if sink is None:
        return
    u = data.get("usage") or {}
    tin, tout = int(u.get("in") or 0), int(u.get("out") or 0)
    sink.update({"prompt_tokens": tin, "completion_tokens": tout,
                 "total_tokens": tin + tout,
                 "credits_mc": int(data.get("charged_mc") or 0)})


# ── public calls ─────────────────────────────────────────────────────────────

def chat(role: str, prompt: str, *, addon: str = "core", feature: str = "",
         attachments: list[dict] | None = None, json_mode: bool = False,
         temperature: float | None = None, max_tokens: int | None = None,
         system: str = "", timeout: int = 180, retries: int = 1,
         usage_out: dict | None = None, usage_sink: dict | None = None,
         artifact_ref: str = "", model_hint: str = "") -> str:
    """One answer from the server. `model_hint` is accepted for the callers
    that still name a model and is deliberately not sent."""
    body = _chat_body(role, prompt, attachments, addon=addon,
                      feature=feature or role, artifact_ref=artifact_ref,
                      json_mode=json_mode, temperature=temperature,
                      max_tokens=max_tokens, system=system)
    data = _post(PATH_CHAT, body, bearer=True, timeout=timeout, retries=retries)
    _heard(data)
    text = data.get("text") or ""
    for sink in (usage_out, usage_sink):
        _fill_usage(sink, data)
    if usage_out is not None:
        usage_out["finish_reason"] = data.get("finish") or "stop"
        usage_out["answer_from_reasoning"] = False
    if usage_sink is not None:
        usage_sink.setdefault("cost", 0)
    if not text:
        raise AiError("EMPTY", "The AI sent back an empty answer. Nothing was "
                               "charged -- please try again.")
    return text


def stream(role: str, prompt: str, *, addon: str = "core", feature: str = "",
           attachments: list[dict] | None = None, json_mode: bool = False,
           temperature: float | None = None, max_tokens: int | None = None,
           system: str = "", timeout: int = 180, artifact_ref: str = "",
           usage_out: dict | None = None, model_hint: str = "") -> Iterator[str]:
    """The same call as chat(), yielding text as it is written. Errors before
    the first byte are raised as AiError; an error event mid-way is raised
    after the text already yielded."""
    a = _auth()
    body = _body(a, _chat_body(role, prompt, attachments, addon=addon,
                               feature=feature or role, artifact_ref=artifact_ref,
                               json_mode=json_mode, temperature=temperature,
                               max_tokens=max_tokens, system=system))
    if _send is not None:                       # tests: one JSON answer, in pieces
        data = _send(PATH_STREAM, body, a.get("lease", ""))
        for piece in data.get("deltas") or [data.get("text", "")]:
            yield piece
        _fill_usage(usage_out, data)
        return
    url = str(a["base_url"]).rstrip("/") + PATH_STREAM
    try:
        resp = requests.post(url, json=body, headers=_headers(a, True),
                             timeout=timeout, stream=True)
    except requests.RequestException as e:
        raise AiError("UNREACHABLE", _OFFLINE) from e
    with resp:
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            _raise_for(resp.status_code, data)
        event = ""
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw.startswith("event:"):
                    event = raw[6:].strip()
                elif raw.startswith("data:"):
                    try:
                        data = json.loads(raw[5:].strip() or "{}")
                    except ValueError:
                        continue
                    if event == "delta":
                        yield data.get("t", "")
                    elif event == "done":
                        _heard(data)
                        _fill_usage(usage_out, data)
                        return
                    elif event == "error":
                        raise AiError(str(data.get("code") or "PROVIDER_ERROR"),
                                      str(data.get("message") or ""), data)
        except requests.RequestException as e:
            raise AiError("UNREACHABLE", _OFFLINE) from e


_ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4")


def _server_aspect(ratio: str) -> str:
    """The server takes five shapes. A caller that asks for another (the 4:5
    post) gets the nearest of the five."""
    if ratio in _ASPECTS:
        return ratio
    try:
        w, h = (float(x) for x in ratio.split(":"))
        want = w / h
    except (ValueError, ZeroDivisionError):
        return "1:1"
    return min(_ASPECTS, key=lambda r: abs(
        (float(r.split(":")[0]) / float(r.split(":")[1])) - want))


def image(prompt: str, *, role: str = "image", addon: str = "core",
          feature: str = "image", aspect_ratio: str = "1:1",
          resolution: str = "1K", timeout: int = 180, retries: int = 1,
          artifact_ref: str = "", model_hint: str = "") -> bytes:
    """One picture, as raw bytes ready to write to a .png."""
    body = _call_fields(role, addon, feature, artifact_ref)
    body.update({"prompt": prompt, "aspect_ratio": _server_aspect(aspect_ratio),
                 "resolution": resolution, "n": 1})
    data = _post(PATH_IMAGE, body, bearer=True, timeout=timeout, retries=retries)
    _heard(data)
    imgs = data.get("images") or []
    first = imgs[0] if imgs else ""
    if isinstance(first, dict):
        first = first.get("b64_json") or first.get("b64") or ""
    if not first:
        raise AiError("EMPTY", "The AI answered but sent no picture. Nothing "
                               "was charged -- please try again.")
    return base64.b64decode(first)


def estimate(role: str, *, approx_in_chars: int = 0, max_tokens: int | None = None,
             images: int = 0, stream: bool = False, addon: str = "core",
             feature: str = "estimate", timeout: int = 20) -> dict:
    """What a call would hold at most and typically cost (credits in mc).
    Never charges."""
    extra = {"role": role, "addon": _addon(addon),
             "feature": _ident(feature, "estimate", 40),
             "approx_in_chars": int(approx_in_chars), "images": int(images),
             "stream": bool(stream)}
    if max_tokens is not None:
        extra["max_tokens"] = int(max_tokens)
    return _post(PATH_ESTIMATE, extra, bearer=False, timeout=timeout, retries=0)


def status(timeout: int = 15) -> dict:
    """Is AI ready for this seat, and the balance (see /v1/ai/status)."""
    return _post(PATH_STATUS, {}, bearer=False, timeout=timeout, retries=0)


def run_cost(run_ids: list[str], timeout: int = 15) -> dict:
    return _post(PATH_RUN_COST, {"run_ids": list(run_ids)}, bearer=False,
                 timeout=timeout, retries=0)


def usage(limit: int = 50, before: str = "", timeout: int = 20) -> dict:
    """What this licence spent: balance, this cycle, today, by add-on, by day
    and a page of recent calls (GET /v1/ai/usage; `next` is the next `before`).

    A 404 means an older server without the route: it raises AiError
    NOT_SET_UP (status 404), which the caller turns into "this session only".
    Blocking: call it from a worker thread, never from the UI thread."""
    a = _auth()
    q = {"license_id": a["license_id"], "device_fp": a["device_fp"],
         "limit": int(limit)}
    if before:
        q["before"] = str(before)
    if _send is not None:
        return _send(PATH_USAGE, q, a.get("lease", ""))
    url = str(a["base_url"]).rstrip("/") + PATH_USAGE
    try:
        resp = requests.get(url, params=q, headers=_headers(a, True),
                            timeout=timeout)
    except requests.RequestException as e:
        raise AiError("UNREACHABLE", _OFFLINE) from e
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code < 400:
        return data
    _raise_for(resp.status_code, data)


def plain(e: BaseException) -> str:
    """Words for a person. AiError messages are already written as copy."""
    return str(e)
