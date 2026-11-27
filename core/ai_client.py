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

from . import files

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

# The quality a customer may pin. Unset means "Auto": the server starts on the
# best quality the plan allows and steps down as the weekly bar fills. The
# server clamps a pin to the plan's top quality, and locked roles (boq.*,
# step.*, anything numeric) never step down whatever is sent.
LEVELS = ("fast", "balanced", "best")

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


def _heard(data: dict, body: dict | None = None) -> None:
    if _listener is not None and isinstance(data, dict) and "balance_mc" in data:
        body = body or {}
        model = data.get("model")
        meta = meta_of(data)
        try:
            _listener({"balance_mc": data.get("balance_mc"),
                       "charged_mc": data.get("charged_mc", 0),
                       # What the call was (no prompt, no text), so the host
                       # can keep a this-session list when the server has no
                       # usage route.
                       "addon": body.get("addon", ""),
                       "feature": body.get("feature", ""),
                       "role": body.get("role", ""),
                       "model_label": (model.get("label", "")
                                       if isinstance(model, dict) else ""),
                       **meta,
                       "run_id": body.get("run_id", ""),
                       "ts": int(time.time())})
        except Exception:                                   # noqa: BLE001
            pass            # a display must never break a call
    hook = getattr(_meta_hook, "fn", None)
    if hook is not None and isinstance(data, dict):
        meta = meta_of(data)
        if meta.get("level_used"):      # an older server names no level
            try:
                hook(meta)
            except Exception:                               # noqa: BLE001
                pass


_level: Optional[str] = None
_meta_hook = threading.local()         # this thread's run: fn(meta) per answer


def set_level(level: Optional[str]) -> None:
    """The customer's pinned quality: "fast", "balanced" or "best". None (or
    anything else, such as "auto") means unset: the server decides, and no
    `level` is sent."""
    global _level
    level = (level or "").strip().lower()
    _level = level if level in LEVELS else None


def get_level() -> Optional[str]:
    return _level


def _model_name(model) -> str:
    """The model's name for a person: the server sends a {key, label} object
    (or a bare string)."""
    if isinstance(model, dict):
        return str(model.get("label") or model.get("key") or "")
    return str(model or "")


def meta_of(data: dict) -> dict:
    """{"model", "level_used", "stepped_down", "bar_pct"} from one answer;
    {} when the server did not say (an older server)."""
    if not isinstance(data, dict):
        return {}
    level = str(data.get("level_used") or "").strip().lower()
    if level not in LEVELS:
        level = ""
    # The friendly name comes from the `model` object; `model_id` (the same
    # string as model.key) is the fallback.
    model = _model_name(data.get("model")) or str(data.get("model_id") or "")
    bar = data.get("bar_pct")
    try:
        bar = max(0.0, min(100.0, float(bar))) if bar is not None else None
    except (TypeError, ValueError):
        bar = None
    if not (level or model or bar is not None or data.get("stepped_down")):
        return {}
    return {"model": model, "level_used": level,
            "stepped_down": bool(data.get("stepped_down")), "bar_pct": bar}


def set_meta_hook(fn: Optional[Callable[[dict], None]]) -> None:
    """This thread hears the meta of every answer it gets (a run's events and
    its summary line). None clears it."""
    _meta_hook.fn = fn


_LEVEL_WORDS = {"best": "Best", "balanced": "Balanced", "fast": "Fast"}


def level_word(level: str) -> str:
    return _LEVEL_WORDS.get(str(level or "").lower(), "")


def summarise_quality(metas: list[dict]) -> dict:
    """One run's quality from the meta of each of its answers: the level most
    of its calls ran at, the model of the latest call at that level, whether
    any call stepped down, and the weekly bar at the end. {} when no answer
    said (an older server, or a run with no AI call)."""
    metas = [m for m in metas if isinstance(m, dict) and m]
    if not metas:
        return {}
    levels = [m["level_used"] for m in metas if m.get("level_used")]
    head = max(set(levels), key=levels.count) if levels else ""
    model = next((m.get("model") for m in reversed(metas)
                  if m.get("model") and m.get("level_used", "") == head), "")
    bars = [m["bar_pct"] for m in metas if m.get("bar_pct") is not None]
    return {"level_used": head, "model": model,
            "stepped_down": any(m.get("stepped_down") for m in metas),
            "bar_pct": bars[-1] if bars else None, "calls": len(metas)}


def quality_line(summary: dict) -> str:
    """"Made with Balanced quality (model-name)": what the run summary says."""
    word = level_word(summary.get("level_used", ""))
    model = summary.get("model") or ""
    if not word:
        return f"Made with {model}" if model else ""
    return (f"Made with {word} quality ({model})" if model
            else f"Made with {word} quality")


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


# A film run (Reel or Motion): every call this thread makes while it lasts is
# tagged with that add-on name, whatever the caller said (the script, the
# briefs, the pictures, the design turns), so the server's Reel / Motion shares
# and its one-Reel-at-a-time rule see the whole job. `feature` is left alone.
FILMS = ("reel", "motion")


def film_state() -> tuple[str, str]:
    """(film, run id) of this thread, to hand back to restore_film()."""
    return current_film(), _run_id()


def current_film() -> str:
    return getattr(_run, "film", "")


def set_film(kind: str) -> None:
    """This thread's following calls belong to a "reel" or "motion" run. A run
    with no run id yet gets one. Anything else changes nothing."""
    if kind in FILMS:
        _run.film = kind
        if not _run_id():
            set_run_id(kind[0] + uuid.uuid4().hex[:16])


def restore_film(state: tuple[str, str]) -> None:
    _run.film, _run.id = state


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
    if (isinstance(err, dict) and err.get("code") == "AI_NOT_AVAILABLE"
            and isinstance(err.get("detail"), dict)
            and err["detail"].get("reason") == "bar_full"):
        # The weekly bar is full: say what can be done, not "not available".
        day = ""
        try:
            import datetime
            day = datetime.datetime.fromtimestamp(
                int(err["detail"].get("week_resets_at"))).strftime("%A")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        raise AiError(
            "AI_NOT_AVAILABLE",
            "You have used this week's AI allowance. "
            + (f"It resets {day}. " if day else "It resets when your week ends. ")
            + "Ask Alphakore if you need more before then, or make a simple "
              "Reel, which uses no AI. Nothing was charged.",
            err["detail"], status)
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
        mime = att["mime"]
        if att["kind"] != "pdf":            # declare what the bytes are, not what the name says
            try:
                mime = files.image_mime(base64.b64decode(att["b64"][:24])) or mime
            except ValueError:
                pass
        uri = f"data:{mime};base64,{att['b64']}"
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
    out = {"idem_key": uuid.uuid4().hex, "role": role,
           "addon": current_film() or _addon(addon),
           "feature": _ident(feature, role, 40),
           "run_id": _run_id(),
           "artifact_ref": _ident(artifact_ref, "", 64)}
    if _level:
        out["level"] = _level        # the customer's pin; unset = server decides
    return out


def _cached(content, prefix: str):
    """`content` with its opening `prefix` as a text part of its own, marked
    for the provider's prompt cache. The text sent is the same either way."""
    blocks = ([{"type": "text", "text": content}] if isinstance(content, str)
              else list(content))
    text = blocks[0].get("text", "")
    if not prefix or len(text) <= len(prefix) or not text.startswith(prefix):
        return content
    return [{"type": "text", "text": prefix,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": text[len(prefix):]}] + blocks[1:]


def _chat_body(role, prompt, attachments, *, addon, feature, artifact_ref,
               json_mode, temperature, max_tokens, system,
               cache_prefix: str = "", effort: str = "") -> dict:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    content = _content(prompt, attachments)
    if cache_prefix:
        content = _cached(content, cache_prefix)
    msgs.append({"role": "user", "content": content})
    body = _call_fields(role, addon, feature, artifact_ref)
    if effort:
        body["effort"] = effort      # a request: the server decides if it applies
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
         artifact_ref: str = "", model_hint: str = "", model: str = "",
         cache_prefix: str = "", effort: str = "") -> str:
    """One answer from the server. `model_hint` is accepted for the callers
    that still name a model and is deliberately not sent. `model` is the
    opposite: the person picked it (STEP's Haiku / Sonnet), so it is sent and
    the server checks it against its own allow-list."""
    body = _chat_body(role, prompt, attachments, addon=addon,
                      feature=feature or role, artifact_ref=artifact_ref,
                      json_mode=json_mode, temperature=temperature,
                      max_tokens=max_tokens, system=system,
                      cache_prefix=cache_prefix, effort=effort)
    if model:
        body["model"] = model
    try:
        data = _post(PATH_CHAT, body, bearer=True, timeout=timeout, retries=retries)
    except AiError as e:
        # The owner can set the server's output limit below ours. Refused
        # before anything ran: ask once more and let the server's limit stand.
        if not (max_tokens and e.code == "REQUEST_TOO_LARGE"
                and e.detail.get("limit") == "max_output_tokens"):
            raise
        body.pop("max_tokens", None)
        data = _post(PATH_CHAT, body, bearer=True, timeout=timeout, retries=retries)
    _heard(data, body)
    text = data.get("text") or ""
    for sink in (usage_out, usage_sink):
        _fill_usage(sink, data)
    if usage_out is not None:
        usage_out["finish_reason"] = data.get("finish") or "stop"
        usage_out["answer_from_reasoning"] = False
    if usage_sink is not None:
        usage_sink.setdefault("cost", 0)
    if not text:
        charged = int(data.get("charged_mc") or 0)
        if charged > 0:
            # The server only answers 200 with no text when the model ran out
            # of room (finish "length"), and it bills that call.
            raise AiError(
                "EMPTY",
                "The AI used up its whole answer budget and sent back no text. "
                f"This call was charged {charged / 1000:.1f} credits. Prism has "
                "not asked again, so nothing more was spent -- try a shorter "
                "request.",
                {"charged_mc": charged, "balance_mc": data.get("balance_mc"),
                 "finish": data.get("finish") or ""})
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
                        _heard(data, body)
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
    _heard(data, body)
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
