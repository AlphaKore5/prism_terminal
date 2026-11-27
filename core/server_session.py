"""
Prism -- a chat that runs on Prism's server instead of a website
────────────────────────────────────────────────────────────────
automation.run() used to type every AI step into a consumer site (ChatGPT,
Claude, Perplexity, ...) through a Chrome it drove. In server mode
(core.ai_client.active()) it hands the same code a ServerSession where the
Selenium driver used to be. Every browser helper that would have touched the
page checks for one and answers "nothing to do" (see automation._server_aware),
so the prompt building, hand-off, Studio/Motion scene-by-scene conversations
and file contract all run unchanged, and only the transport differs: one
ai_client.chat() call per prompt.

Which stages exist without a website behind them is decided here too:
`unsupported_reason` is the one place that says "this is not part of your
Prism plan", and `role_for` is the one place that maps a stage to a server
role (core.ai_client roles; the server owns role -> model).
"""
from __future__ import annotations

import base64
import json
import os

# Stage (category or custom label) -> server role.
_STAGE_ROLE = {
    "research": "search",
    "leads": "search",
    "brains": "reasoner",
    "content": "writer",
    "development": "code",
    "summary": "summary",
    "design": "code",
    "storyboard": "code",
}
_DEFAULT_ROLE = "writer"

# Tools that existed only as a website with nothing equivalent on the server.
# Dropped on purpose (the owner's call); a plan that still names one keeps the
# step on screen, marked as left out, instead of opening a browser.
_NO_SERVER_EQUIVALENT = frozenset({
    "Canva", "Midjourney", "Runway", "HeyGen", "ElevenLabs", "Suno",
    "NotebookLM", "Gamma.app", "Tome", "Claude Design", "Google Flow",
    "InVideo AI", "Pika Labs", "Apollo",
})
# Whole categories with no server role: voice and slide decks.
_NO_SERVER_STAGES = frozenset({"audio", "presentation"})

# A picture step is one thing on the server: role `image`. Every site that
# only drew pictures is answered by the local "Prism Image" agent instead.
IMAGE_AGENT = "Prism Image"
_IMAGE_STAGES = frozenset({"visual", "artwork"})

_MAX_HISTORY_CHARS = 60000
_MAX_ATTACH_BYTES = 8 * 1024 * 1024
_MAX_ATTACHMENTS = 4


def not_in_plan(what: str) -> str:
    return (f"{what} is not part of your Prism plan, so this step was left "
            "out. Nothing was opened and nothing was charged.")


def unsupported_reason(stage: str, agent_name: str, agent_cfg: dict) -> str:
    """'' when the step can run on the server, else the sentence to show."""
    cfg = agent_cfg or {}
    if cfg.get("local"):
        return ""
    if (agent_name in _NO_SERVER_EQUIVALENT or stage in _NO_SERVER_STAGES
            or cfg.get("runner") in ("canva", "elevenlabs")
            or cfg.get("search_tool")):
        return not_in_plan(agent_name or "This tool")
    if stage in _IMAGE_STAGES:
        return ""                       # redirected, see redirect_agent
    if stage == "media":
        return not_in_plan(agent_name or "This video tool")
    return ""


def redirect_agent(stage: str, agent_name: str, agent_cfg: dict) -> str:
    """The agent that really runs a step in server mode ('' = unchanged)."""
    if (stage in _IMAGE_STAGES and not (agent_cfg or {}).get("local")
            and agent_name not in _NO_SERVER_EQUIVALENT):
        return IMAGE_AGENT
    return ""


def role_for(stage: str, agent_name: str = "", *, feeder: bool = False,
             has_images: bool = False) -> str:
    if feeder:
        return "code"
    if agent_name == "Perplexity":
        return "search"
    role = _STAGE_ROLE.get(stage, _DEFAULT_ROLE)
    if has_images and role in ("reasoner", "writer"):
        return "vision"
    return role


def _attachment_blocks(attachments) -> list[dict]:
    out = []
    for a in attachments or []:
        if len(out) >= _MAX_ATTACHMENTS:
            break
        if not isinstance(a, dict) or a.get("kind") not in ("image", "pdf"):
            continue
        path = a.get("path") or ""
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > _MAX_ATTACH_BYTES:
                continue
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            continue
        out.append({"kind": a["kind"], "mime": a.get("mime") or "image/png",
                    "b64": b64, "name": a.get("name") or os.path.basename(path)})
    return out


# ── what a Reel turn costs ──────────────────────────────────────────────────
# Each switch can be turned off in ~/.prism/config.json, for example
# "ai_savings": {"low_effort": false}; "ai_savings": false turns all off.
SAVINGS = {
    # A scene or repair turn is sent the brief, the look with the storyboard and
    # the neighbouring scene, not every earlier turn.
    "trim_history": True,
    # The part of a design conversation that repeats is marked for caching.
    "prompt_cache": True,
    # An output limit per kind of turn (never above the server's own 8,000).
    "turn_caps": True,
    # Less hidden thinking on routine turns. The art direction, the script and
    # the person's follow-up changes keep the model's default.
    "low_effort": True,
    # A scene whose call failed is asked for again with its own prompt.
    "resend_failed_scene": True,
}
_SWITCHES = tuple(SAVINGS)
# How much of the neighbouring scene a trimmed turn carries: half of this from
# its markup, half from its styles. The scene being fixed always goes whole.
SAVINGS["neighbour_chars"] = 8000
# Hidden thinking counts against the limit, hence the room.
TURN_CAPS = {"design": 8000, "scene": 8000, "repair": 8000, "followup": 8000,
             "script": 6000, "brief": 4000, "briefs": 6000}
_LOW_EFFORT_TURNS = ("scene", "repair", "brief", "briefs")
# Shorter stable text than this is not worth a cache write (providers need
# about a thousand tokens).
_MIN_CACHE_CHARS = 4000
# Under the server's input limit (120,000 characters).
_MAX_TRIMMED_CHARS = 110000
_HISTORY_INTRO = "This is a continuing conversation. Earlier turns:\n\n"


def savings_of(cfg) -> dict:
    got = (cfg or {}).get("ai_savings")
    if got is False:
        return dict.fromkeys(_SWITCHES, False)
    return {**SAVINGS, **(got if isinstance(got, dict) else {})}


def turn_options(savings: dict, kind: str) -> dict:
    """The extra arguments for one Reel turn of this kind ({} for none)."""
    out = {}
    if savings.get("turn_caps") and kind in TURN_CAPS:
        out["max_tokens"] = TURN_CAPS[kind]
    if savings.get("low_effort") and kind in _LOW_EFFORT_TURNS:
        out["effort"] = "low"
    return out


def aim_turn(ask, kind: str, scenes=(), idx: int = 0, *, current=None,
             last: bool = False) -> None:
    """Tell a server conversation what the next `ask` is: its kind, the scene
    before scene `idx`, the scene being fixed (`current`) and whether the
    exchange just before it is needed. An `ask` that cannot be told (a browser
    tab, a test stub) is left alone."""
    hook = getattr(ask, "aim", None)
    if hook is None:
        return
    shown = []
    if 0 < idx <= len(scenes):
        shown.append((f"THE SCENE BEFORE THIS ONE (scene {idx}), for continuity",
                      scenes[idx - 1], False))
    if current is not None:
        shown.append((f"SCENE {idx + 1} AS IT STANDS NOW", current, True))
    hook(kind, shown, last)


def _shown_scene(scene: dict, whole: bool, cap: int) -> str:
    """One scene as a trimmed turn carries it."""
    half = max(0, int(cap)) // 2
    cut = {k: v[:half] for k, v in scene.items()
           if not whole and isinstance(v, str) and len(v) > half}
    return (("(long, so only the opening of it is shown)\n" if cut else "")
            + "```json\n"
            + json.dumps({**scene, **cut}, ensure_ascii=False) + "\n```")


def replay_prompt(turns: list[tuple[str, str]], prompt: str) -> str:
    """`prompt` with the earlier turns of the conversation replayed in front
    of it, newest kept, the whole capped at _MAX_HISTORY_CHARS."""
    if not turns:
        return prompt
    parts, used = [], len(prompt)
    for who, text in reversed(turns):
        used += len(text) + 12
        if used > _MAX_HISTORY_CHARS:
            break
        parts.append(f"{who}: {text}")
    history = "\n\n".join(reversed(parts))
    return (_HISTORY_INTRO
            + f"{history}\n\nNow the user writes:\n\n{prompt}")


# Roles whose answer is long and structured, and the model asked once more
# when the first one sends back nothing. The server allows this model for
# these roles (ai_seed advanced_roles; STEP pins it the same way).
_RETRY_ON_EMPTY = ("code", "writer", "reasoner", "vision")
_RETRY_MODEL = "anthropic/claude-sonnet-5.5"


class ServerSession:
    """Stands where the Selenium driver stood. One conversation per stage."""

    is_server_session = True
    current_url = ""

    def __init__(self, addon: str = "core", cfg: dict | None = None):
        self.addon = addon
        self.savings = savings_of(cfg)
        self.begin("", "", "writer")

    def begin(self, stage: str, agent_name: str, role: str, feature: str = ""):
        self.stage, self.agent = stage, agent_name
        self.role = role
        self.feature = feature or stage or role
        self.turns: list[tuple[str, str]] = []
        self.captures: list[str] = []
        self.pending: list[dict] = []
        self.last_error = ""
        self.last_error_code = ""       # the AiError code behind last_error
        self.turn_kind = ""             # Reel turn kind when nothing aims one
        self.cache_head = False         # a design conversation: cache its start
        self.look_text = ""             # the look and storyboard, kept short
        self._aim = None

    def add_files(self, attachments) -> int:
        """Pictures and PDFs travel with the first prompt. Other files are
        already pasted into it as text (files.context_block)."""
        self.pending = _attachment_blocks(attachments)
        if self.pending and self.role in ("reasoner", "writer"):
            self.role = "vision"
        return 0                        # 0: also paste the text, as for any tool

    def look(self, text: str) -> None:
        self.look_text = text

    def aim(self, kind: str, shown=(), last: bool = False) -> None:
        """What the next send() is. See aim_turn()."""
        self._aim = (kind, list(shown), last)

    def _prompt(self, prompt: str, aim) -> tuple[str, str]:
        """(what to send, its stable start to mark for caching or '')."""
        cache = self.savings["prompt_cache"] and (aim or self.cache_head)
        if aim and self.savings["trim_history"] and self.turns:
            _kind, shown, last = aim
            head = [self.turns[0]]
            kept = 1                    # turns the head stands for
            if self.look_text:
                head.append(("ASSISTANT", self.look_text))
                kept = 2
            start = _HISTORY_INTRO + "\n\n".join(f"{w}: {t}" for w, t in head)
            tail = (self.turns[max(kept, len(self.turns) - 2):] if last else [])
            while True:
                parts = [start, "(Only the earlier turns this step needs are "
                                "shown.)"]
                parts += [f"{label}:\n" + _shown_scene(
                              scene, whole, self.savings["neighbour_chars"])
                          for label, scene, whole in shown]
                parts += [f"{w}: {t}" for w, t in tail]
                full = ("\n\n".join(parts)
                        + f"\n\nNow the user writes:\n\n{prompt}")
                if len(full) <= _MAX_TRIMMED_CHARS or len(shown) < 2:
                    break
                shown = shown[1:]       # the neighbour goes before the target
            cache = cache and kept == 2
        else:
            full = replay_prompt(self.turns, prompt)
            start = (_HISTORY_INTRO + "\n\n".join(
                f"{w}: {t}" for w, t in self.turns[:2])
                if len(self.turns) >= 2 else "")
        if not (cache and len(start) >= _MIN_CACHE_CHARS
                and full.startswith(start)):
            start = ""
        return full, start

    def send(self, prompt: str) -> str:
        """One turn. Raises ai_client.AiError with customer-facing words.

        An EMPTY answer (nothing charged) is asked once more of a different
        model: 19 Nov 2026 a reel's design stage ('code' role) came back empty
        twice after 78 s and the whole video was lost. A long art-direction
        brief can leave a reasoning model no room for the answer itself."""
        from . import ai_client
        aim, self._aim = self._aim, None
        full, prefix = self._prompt(prompt, aim)
        extra = turn_options(self.savings, aim[0] if aim else self.turn_kind)
        if prefix:
            extra["cache_prefix"] = prefix
        self.last_error_code = ""
        try:
            try:
                text = ai_client.chat(
                    self.role, full, addon=self.addon,
                    feature=self.feature, attachments=self.pending or None,
                    **extra)
            except ai_client.AiError as e:
                # Never ask again for an answer the server already charged for
                # (26 Nov 2026: 255.378 credits taken twice for two empty
                # answers). Only a call that cost nothing is retried.
                if (e.code != "EMPTY" or self.role not in _RETRY_ON_EMPTY
                        or e.detail.get("charged_mc")):
                    raise
                text = ai_client.chat(
                    self.role, full, addon=self.addon, feature=self.feature,
                    attachments=self.pending or None, model=_RETRY_MODEL,
                    **extra)
        except ai_client.AiError as e:
            self.last_error_code = e.code or "AI_ERROR"
            raise
        self.pending = []
        self.turns.append(("USER", prompt))
        self.turns.append(("ASSISTANT", text))
        self.captures.append(text)
        return text


def is_session(driver) -> bool:
    return isinstance(driver, ServerSession)
