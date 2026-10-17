"""
Prism — the routing brain (Groq)
────────────────────────────────
Takes the user's raw query + their profile + the agents they enabled, and asks
Groq to split the task into a self-contained prompt (or several) for each
pipeline stage — marking stages "needed": false when they don't apply.

Generalised from the original 4-stage version to the full six categories, and
tailored by the user's "what do you do" profile.
"""
from __future__ import annotations
import json
import re
import requests

from . import agents as A
from . import config as C
from . import skills as SK
from . import ui

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ── surviving Groq ────────────────────────────────────────────────────────────
# Hosted open models get retired on a few weeks' notice, and every install
# carries the same default. A single hardcoded model name means the day Groq
# drops it, every customer stops being able to plan anything, at the same hour,
# with "model_not_found" in a dialog box.
#
# So the model is a LIST, tried in order. The first one that answers is written
# back into the config, so the fallback costs one wasted request once and
# nothing afterwards. Ordered by capability: routing is the most demanding
# thing Prism asks a model to do, and a weaker one produces worse plans rather
# than visible errors.
# Verified against the live API on 18 Aug 2026 — every entry answered, and
# every entry returned parseable JSON for a routing-shaped prompt. Do not add a
# model to this tuple without checking BOTH: three of the four that used to be
# here were dead, and the survivors are not interchangeable.
#
#   llama-3.3-70b-versatile   404, retired
#   llama-3.1-8b-instant      404, retired
#   gemma2-9b-it              400, decommissioned
#
# So the chain had one working model left and it was third, meaning every plan
# paid for two failed round trips first, and one more retirement would have
# stopped every customer planning anything.
#
# qwen/qwen3.6-27b is deliberately ABSENT although it answers. It is a
# reasoning model that writes a <think> block into `content` — 1531 characters
# of it for a prompt asking only for JSON — so routing would get a plan it
# cannot parse. Answering is not the same as being usable.
#
# Note the reasoning models here put their working-out in a separate
# `reasoning` field and can return EMPTY `content` if max_tokens runs out
# first. Give them room; a short cap reads as "the model returned nothing".
MODEL_FALLBACKS = (
    "openai/gpt-oss-120b",      # largest; routing is the most demanding call
    "openai/gpt-oss-20b",
    "groq/compound-mini",
)

# Set from a licence-server payload, so a retirement is a database row rather
# than a release every customer has to install. Empty until one arrives.
_SERVED_CHAIN: list[str] = []


def apply_model_chain(models) -> int:
    """Replace the server-published model chain. Returns how many are in use.

    Replace rather than merge: the payload is the whole intended list, so a
    model dropped from it must stop being tried. Rubbish is ignored rather
    than raising — a bad publish must not be able to stop Prism planning.
    """
    _SERVED_CHAIN.clear()
    for name in (models or []):
        if isinstance(name, str) and name.strip():
            _SERVED_CHAIN.append(name.strip())
    return len(_SERVED_CHAIN)

# HTTP statuses worth trying again rather than surfacing. 429 is Groq's rate
# limit, which a queue of tasks hits routinely on the free tier; 5xx is Groq
# having a moment. Anything else is a real answer and is reported.
_RETRY_STATUS = (429, 500, 502, 503, 504)


def model_chain(preferred: str = "") -> list[str]:
    """The models to try, the caller's choice first and never duplicated.

    A server-published chain, when there is one, comes before the built-in
    tuple but still after the caller's own preference — the customer's setting
    is theirs, and the payload exists to fix OUR stale list, not to override
    what somebody deliberately chose.
    """
    chain = [m for m in ([preferred] if preferred else []) if m]
    for source in (_SERVED_CHAIN, MODEL_FALLBACKS):
        chain += [m for m in source if m not in chain]
    return chain


def _remember_model(model: str, preferred: str) -> None:
    """Persist a fallback that worked, so the next run starts there.

    Written through config.load/save rather than the caller's dict: route() is
    handed a cfg the GUI keeps in memory across dialogs, and writing that back
    wholesale is how a stale copy erases someone's API key.
    """
    if not model or model == preferred:
        return
    if not preferred:
        # The caller named no model (the mail check passes ""), so nothing
        # "went missing": the chain simply started at its first entry. Saying
        # "no longer offers the saved model" and rewriting the config on every
        # one of a check's AI calls was noise, and a write each time.
        return
    try:
        from . import config as C
        saved = C.load()
        if saved.get("model") != model:
            saved["model"] = model
            C.save(saved)
        ui.warn(f"Groq no longer offers {preferred or 'the saved model'} — "
                f"switched to {model} and saved it.")
    except Exception:                                   # noqa: BLE001
        pass        # a config we cannot write must not fail the run


def _model_is_gone(status: int, body: dict) -> bool:
    """Did Groq refuse because THIS MODEL is unavailable, as opposed to
    because the key, the quota or the request was wrong?"""
    if status not in (400, 404):
        return False
    blob = json.dumps(body).lower()
    return ("model" in blob
            and any(w in blob for w in ("not found", "does not exist",
                                        "decommission", "deprecat",
                                        "no longer", "unavailable")))


def groq_chat(api_key: str, model: str, prompt: str, *, temperature: float = 0.3,
              timeout: int = 60, retries: int = 1, json_mode: bool = False) -> str:
    """One Groq completion, with the two failures a daily user actually hits.

    Rate limits are retried after the wait Groq asks for; a retired model falls
    through to the next in the chain and the working one is saved. Everything
    else raises with the server's own words, because those are usually
    actionable ("invalid api key") and paraphrasing them loses that.

    `json_mode` asks Groq to constrain the whole reply to a single JSON object
    (`response_format={"type": "json_object"}`). This is the point of the API
    path for machine-consumed stages: unlike a scraped browser answer, the
    model cannot wrap the JSON in prose, refuse the format as an "injection",
    or trail off — so a renderer/parser downstream gets valid JSON or a clean
    error, with a real completion signal instead of a 300s stability guess.
    The prompt must itself mention JSON somewhere or Groq rejects the request;
    Prism's machine-stage prompts already say "reply with only a JSON object".
    """
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    last = ""
    for candidate in model_chain(model):
        for attempt in range(retries + 1):
            try:
                payload = {"model": candidate,
                           "messages": [{"role": "user", "content": prompt}],
                           "temperature": temperature}
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = requests.post(
                    GROQ_URL, headers=headers, json=payload, timeout=timeout)
            except requests.RequestException as e:
                raise RuntimeError(
                    f"Couldn't reach Groq — check your internet connection. "
                    f"({e})") from e

            try:
                body = resp.json()
            except ValueError:
                body = {}

            if resp.status_code == 200 and "choices" in body:
                _remember_model(candidate, model)
                return body["choices"][0]["message"]["content"]

            if resp.status_code in _RETRY_STATUS and attempt < retries:
                # Groq names the wait in a header; honour it, but cap it —
                # nobody wants the window to sit there for two minutes.
                import time
                delay = 5
                try:
                    delay = min(int(float(resp.headers.get("retry-after", 5))), 20)
                except (TypeError, ValueError):
                    pass
                ui.warn(f"Groq is rate-limiting this key — waiting {delay}s "
                        f"and trying once more.")
                time.sleep(delay)
                continue

            if _model_is_gone(resp.status_code, body):
                ui.warn(f"Groq has retired {candidate} — trying the next model.")
                break               # next candidate in the chain

            if resp.status_code == 429:
                raise RuntimeError(
                    "Groq is rate-limiting your API key. Wait a minute and try "
                    "again, or raise your limits at console.groq.com.")
            if resp.status_code == 401:
                raise RuntimeError(
                    "Groq rejected your API key. Re-enter it in Setup → Groq "
                    "API key.")
            last = f"HTTP {resp.status_code}: {json.dumps(body)[:300]}"
            raise RuntimeError(f"Groq API error ({last})")

    raise RuntimeError(
        "None of the models Prism knows about are available on your Groq key. "
        "Check console.groq.com for the current model list, then set it in "
        f"Setup. Tried: {', '.join(model_chain(model))}")

# Human field notes about the tools — written by the user from real experience
# (pros, cons, "use this one for X, avoid for Y"). If this file exists, its
# contents are injected into every routing prompt so Groq routes with the
# user's judgement, not just the generic specialty strings.
_NOTES_MAX_CHARS = 14000   # fits tool_notes.md + pros_cons.txt merged, with headroom to grow


def _tool_notes() -> str:
    """Merge EVERY notes file found (~/.prism/ takes precedence, then the app
    folder) — first-match-wins silently shadowed pros_cons.txt whenever
    tool_notes.md existed, dropping half the user's guidance."""
    from . import config as C
    import os
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parts = []
    for folder in (C.CONFIG_DIR, app_dir):
        for fname in ("tool_notes.md", "tool_notes.txt", "pros_cons.txt"):
            path = os.path.join(folder, fname)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read().strip()
                if text:
                    parts.append(text)
            except Exception:
                continue
    return "\n\n".join(parts)[:_NOTES_MAX_CHARS]



#: A tool's own heading in the notes file: "Perplexity:", "Kimi 2.6:". The
#: sub-headings inside one ("Pros:", "Cons:", "My recommendation:") match the
#: same shape, so they are listed out rather than guessed at.
_NOTE_TOOL_HEAD = re.compile(r"^([A-Za-z][\w .+/&-]{0,30}?)\s*:\s*$")
_NOTE_SUBHEADS = ("pros", "cons", "myrecommendation", "recommendation",
                  "note", "notes", "mytake", "usefor", "avoidfor")


def _squash(name: str) -> str:
    return "".join((name or "").lower().split())


def _notes_for(agents: dict) -> str:
    """The field notes for the tools ACTUALLY IN THIS PLAN, plus the general
    routing sections.

    The whole file used to go to the planner — 6,343 characters on the
    owner's machine, most of it about tools the plan does not use and
    advice ("use Runway for generated video") the planner cannot act on,
    since it chooses steps and not tools. It was also introduced as
    outranking the rules, which is how a note about one tool ended up
    competing with the rule that decides whether a step runs at all.

    Notes for a tool that is in the plan are worth their space: they say how
    that tool behaves in practice. Everything else is budget spent on
    confusion, so it is left out.
    """
    raw = _tool_notes()
    if not raw:
        return ""
    wanted = {_squash(n) for n in agents.values() if n}
    out: list[str] = []
    keep = True
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("=="):
            keep = True                 # a general routing note, not a tool
        else:
            head = _NOTE_TOOL_HEAD.match(line)
            if head and not line[:1].isspace():
                name = _squash(head.group(1))
                if name not in _NOTE_SUBHEADS:
                    keep = any(name.startswith(w) or w.startswith(name)
                               for w in wanted if w)
        if keep:
            out.append(line)
    return "\n".join(out).strip()[:_NOTES_MAX_CHARS]

# ── Deterministic make-stage guardrail ────────────────────────────────────────
# If the query clearly asks to BUILD an artefact, we force the matching make-stage
# on even when the model skipped it — so "design a PPT" can never come back as a
# plan-only answer. Only stages the user actually configured an agent for.
_BUILD_VERBS = [
    "make", "build", "create", "design", "generate", "produce", "develop",
    "compose", "draw", "render", "write", "code", "craft", "prepare",
    "put together", "come up with", "whip up", "mock up", "prototype",
]

_ARTEFACT_STAGES = {
    "presentation": ["ppt", "powerpoint", "power point", "slide", "slides", "deck",
                     "slidedeck", "presentation", "keynote", "pitch deck"],
    "visual": ["logo", "image", "illustration", "artwork", "icon", "poster",
               "banner", "graphic", "picture", "photo", "drawing", "wallpaper",
               "thumbnail", "sticker", "mockup"],
    "media": ["video", "animation", "animate", "voiceover", "voice over",
              "voice-over", "narration", "music", "song", "jingle", "soundtrack",
              "audio", "podcast", "tts", "avatar", "reel", "short video",
              "short-form"],
    # NOTE: no bare "prototype" here — it's usually the SUBJECT of a task
    # ("pitch the prototype"), not a request to build software. And no bare
    # "api" either, for exactly the same reason: "make me documentation for
    # this API" is a writing job, and a bare "api" sent it to a tool that
    # builds and deploys software instead.
    "development": ["web app", "webapp", "website", "web site", "web page",
                    "webpage", "landing page", "mobile app", "app", "ui component",
                    "dashboard", "frontend", "front-end", "backend", "back-end",
                    "build an api", "rest api", "api server", "api endpoint",
                    "graphql", "web tool", "saas", "platform"],
    # "document" and "report" were both missing, while "api" above was
    # present — so "write a report on X" forced nothing and "documentation
    # for this API" forced an app build. These are the words people
    # actually use when they want something written.
    "content": ["article", "essay", "blog post", "blog", "whitepaper",
                "white paper", "newsletter", "ebook", "e-book", "screenplay",
                "script", "story", "novel", "documentation", "manuscript",
                "specification", "spec", "technical spec", "requirements document",
                "prd", "srs", "design document", "design doc",
                "document", "report", "memo", "case study", "one-pager",
                "onepager", "manual", "user guide", "proposal", "brochure",
                "datasheet", "data sheet", "sop", "policy"],
}

_MAKE_LABEL = {
    "presentation": "slide deck / presentation",
    "visual": "image",
    "media": "video / audio asset",
    "development": "web app / tool",
    "content": "written piece",
}


def _mentions(text_lc: str, terms: list[str]) -> bool:
    """Every list this checks (_BUILD_VERBS, _ARTEFACT_STAGES) is written in
    the singular/base form — "image", "generate" — but real requests say
    "images" or "it generates images" as often as not. \\b alone treats the
    plural 's' as a boundary violation and misses those entirely, which is
    how "also use Images ... in the reel too" fails to trigger the visual
    guardrail even though it says exactly what it means."""
    for t in terms:
        if " " in t or "-" in t:
            if t in text_lc:
                return True
        elif re.search(r"\b" + re.escape(t) + r"s?\b", text_lc):
            return True
    return False


def apply_make_guardrail(query: str, routing: dict, agents: dict) -> list[str]:
    """Force make-stages the user clearly asked for. Returns the stages forced on.
    Mutates `routing` in place."""
    q = query.lower()
    if not _mentions(q, _BUILD_VERBS):
        return []
    forced = []
    for stage, terms in _ARTEFACT_STAGES.items():
        if not agents.get(stage):
            continue                      # user has no tool for this stage
        if not _mentions(q, terms):
            continue
        data = routing.get(stage) or {}
        if data.get("needed") and data.get("questions"):
            continue                      # already on — nothing to force
        label = _MAKE_LABEL.get(stage, stage)
        qs = data.get("questions") or [
            f"Create the requested {label} for this task, using any earlier "
            f"pipeline output as your brief. Original request: {query.strip()}"
        ]
        routing[stage] = {"needed": True, "questions": qs}
        forced.append(stage)

    # If we forced a make-stage and the user has a brains agent that isn't running,
    # turn on brains too so the deck/app/etc. is planned before it's built.
    if forced and agents.get("brains"):
        b = routing.get("brains") or {}
        if not (b.get("needed") and b.get("questions")):
            routing["brains"] = {"needed": True, "questions": [
                f"Plan and outline the following before it gets built: {query.strip()}"
            ]}
            forced.insert(0, "brains")
    return forced


_RESEARCH_TERMS = [
    "research", "deep research", "look up", "investigate", "investigation",
    "sources", "citations", "fact-finding", "background analysis", "market study",
    "web search", "find out", "gather information", "look into",
    # broader research intent — phrases that clearly mean "go and find facts"
    "find information", "find data", "find sources", "find me facts",
    "data on", "information about", "facts about",
    "market research", "competitive analysis", "competitive landscape",
    "trends in", "industry report", "latest news", "recent news",
    "background on", "context on", "learn about",
]


def apply_research_guardrail(query: str, routing: dict, agents: dict) -> bool:
    """Ensure research stage is enabled when the task explicitly asks for research
    and the user has a research agent configured."""
    if not agents.get("research"):
        return False
    data = routing.get("research") or {}
    if data.get("needed") and data.get("questions"):
        return False
    q = query.lower()
    if _mentions(q, _RESEARCH_TERMS):
        routing["research"] = {
            "needed": True,
            "questions": [
                f"Research and gather facts, sources, data, and background on: {query.strip()}"
            ]
        }
        return True
    return False


def apply_research_reasoning_guardrail(routing: dict, agents: dict) -> bool:
    """A genuine research pass is followed by Prism's reasoning pass.

    Research collects sources and current facts; ``brains`` is where those
    findings are weighed into a recommendation. The router occasionally chose
    only Research, leaving a research task as a source dump even when the
    person had configured a Think-it-through agent. This is intentionally
    keyed to the routed research stage, not keyword matching, so an ordinary
    task that merely says "research" does not grow a step the planner rejected.
    """
    research = routing.get("research") or {}
    if not (isinstance(research, dict) and research.get("needed")
            and research.get("questions") and agents.get("brains")):
        return False
    brains = routing.get("brains") or {}
    if isinstance(brains, dict) and brains.get("needed") and brains.get("questions"):
        return False
    routing["brains"] = {"needed": True, "questions": [
        "Think through the research findings and turn them into the key "
        "decisions, implications and evidence-based recommendation."
    ]}
    return True


def apply_script_guardrail(routing: dict, agents: dict) -> bool:
    """A reel/video/deck needs WORDS — script, narration, captions, slide copy.
    That's the CONTENT agent's job. If MEDIA or PRESENTATION is about to
    produce the deliverable and the user configured a CONTENT agent that the
    model skipped, force CONTENT on between the plan and the make-stage.
    Mutates `routing`; returns True if content was forced."""
    if not agents.get("content"):
        return False
    making = any((routing.get(s) or {}).get("needed") and (routing.get(s) or {}).get("questions")
                 for s in ("media", "presentation"))
    c = routing.get("content") or {}
    if not making or (c.get("needed") and c.get("questions")):
        return False
    routing["content"] = {"needed": True, "questions": [
        "Your ONLY task is: using the plan from the previous stage, write the "
        "COMPLETE script for the deliverable — narration / voiceover lines, "
        "on-screen text, captions, scene-by-scene wording, and every exact word "
        "that will appear or be spoken. Do NOT produce the video, reel or deck "
        "itself — output the words only; a later stage builds it."
    ]}
    return True


# Prism Reel's whole house style is icons and typography, drawn in code —
# real photographs never appear in a frame, only their pixels sampled once for
# an accent colour. A brief that needs actual photography ON SCREEN (a grid
# mockup, a before/after comparison, real product or lifestyle shots) is
# asking for something that renderer structurally cannot draw. Prism Studio
# can: it places images by a real `asset:name` reference. Terms are about
# photography APPEARING in the video, not about "make an image" as a
# separate deliverable — that is _ARTEFACT_STAGES["visual"]'s job, a
# different question with a different answer.
_PHOTO_REEL_TERMS = [
    "instagram grid", "grid mockup", "mockup", "before/after",
    "before and after", "lifestyle photo", "lifestyle shot",
    "product photo", "product shot", "stock photo", "real photo",
    "photograph", "photoshoot", "phone mockup",
]


def apply_studio_guardrail(query: str, routing: dict, agents: dict) -> str:
    """Swap Prism Reel for Prism Studio when the brief clearly needs real
    photography drawn INTO the reel, not just brand colour sampled off one.

    Deterministic for the same reason apply_make_guardrail is: this is a
    structural mismatch between what was asked for and what the configured
    tool can physically draw, not a judgment call worth leaving to whichever
    way an LLM router happens to read the brief that day.

    A per-run swap only — mutates `routing`, not `agents` or the user's saved
    config, so the next run still defaults back to whatever they configured.
    Returns the message to log, or "" if nothing changed."""
    if agents.get("media") != "Prism Reel":
        return ""                          # nothing to swap, or already Studio
    if "Prism Studio" not in A.CATEGORIES.get("media", {}).get("agents", []):
        return ""
    m = routing.get("media") or {}
    if not (m.get("needed") and m.get("questions")):
        return ""
    if not _mentions(query.lower(), _PHOTO_REEL_TERMS):
        return ""
    routing["media"]["agent_override"] = "Prism Studio"
    return ("this brief needs real photography in the reel, which Prism "
            "Reel's code-drawn house style can't display — using Prism "
            "Studio instead for this run")


# Trigger words for the guardrail below — every way people write "BOQ" or
# "BOM" in a chat message, forgiving of the slash and the missing space.
_BOQ_FILE_TERMS = [
    "boq", "bom", "bill of quantities", "bill of quantity",
    "bill of materials", "bill of material",
]

# Stages this guardrail turns off outright. "research" is deliberately not
# here: whether it runs is the planner's own call (or the person's, if they
# asked for it), and forcing it off would undo a "and research the company"
# request the same breath asked for the BOQ. Everything else answers a
# question nobody asked -- there is one document to write, from one file.
_BOQ_FILE_OFF = ("brains", "leads", "visual", "summary", "development",
                 "presentation", "media", "audio", "design", "artwork")


def apply_boq_file_guardrail(query: str, routing: dict, agents: dict,
                             attachments: list | None = None) -> bool:
    """A BOQ or BOM asked for against an attached file is one document step,
    not a plan.

    The owner's report (11 Sep 2026): the same request -- a drawing
    attached, "make a BOQ/BOM for this" -- typed to Claude by hand got the
    document in a fraction of the tokens a routed Prism run spent, because
    the routed run also turned on Think-it-through and Sum-it-up, neither
    of which the request had any use for. Deterministic for the same reason
    apply_make_guardrail is: whether a BOQ needs a planning pass and a
    round-up is not a judgement call worth leaving to however an LLM router
    reads the brief that day -- it never does, so it is never asked for.

    Mutates `routing`: forces every stage in `_BOQ_FILE_OFF` off, and -- when
    a content agent is configured -- turns CONTENT on as the one document
    step, kind "file", briefed with the attached file's name and the format
    asked for (`contract.wanted_ext`, default .docx). RESEARCH is left
    exactly as the planner decided; see `_BOQ_FILE_OFF`'s docstring note.
    Returns whether anything was changed.
    """
    if not attachments:
        return False
    if not _mentions(query.lower(), _BOQ_FILE_TERMS):
        return False
    changed = False
    for stage in _BOQ_FILE_OFF:
        data = routing.get(stage)
        if isinstance(data, dict) and data.get("needed"):
            data["needed"] = False
            data["questions"] = []
            changed = True
    if agents.get("content"):
        from . import contract as _contract
        names = ", ".join(str(a.get("name") or "") for a in attachments
                          if isinstance(a, dict) and a.get("name"))
        ext = _contract.wanted_ext(query) or ".docx"
        line = (f"For this step: make the BOQ/BOM for the attached "
                f"{names or 'file'} as a {ext} file.")
        c = routing.get("content") or {}
        if not (c.get("needed") and c.get("questions")):
            changed = True
        routing["content"] = {"needed": True, "kind": "file",
                              "questions": [line]}
    return changed


def apply_reel_imagery_guardrail(query: str, routing: dict,
                                 agents: dict) -> str:
    """Does this reel get generated pictures? Decided once, here.

    It used to answer that question by FORCING a `visual` step into the plan
    with a prompt of its own. That was the right instinct and the wrong
    mechanism: the engine inserts its own image step ahead of a local
    renderer anyway (automation.run), so a run ended up with two image
    stages — the forced one, briefed "for the Prism Studio reel" even when
    the renderer was Prism Motion, and the engine's. The first picture the
    generic stage produced then took the "logo" slot in the asset table.

    So this now sets a flag the engine reads instead of adding a step, and
    the half that was always worth keeping — honouring "no images", "type
    only" — is what it is really for. Returns "on", "off" or "".
    """
    entry = A.AGENT_REGISTRY.get(agents.get("media") or "") or {}
    if not entry.get("local"):
        return ""                      # not a renderer Prism drives itself
    m = routing.get("media") or {}
    if not (m.get("needed") and m.get("questions")):
        return ""
    q = query.lower()
    explicit_type_only = (
        "without image" in q or "no image" in q or "no artwork" in q or
        "type only" in q or "typography only" in q or "text only" in q or
        "type and colour only" in q or "type and color only" in q)
    routing["_reel_imagery"] = not explicit_type_only
    return "off" if explicit_type_only else "on"


# One-line description of what each stage is FOR, injected into the prompt only
# for the stages the user actually enabled.
_STAGE_HELP = {
    # Two sentences used to be glued together here with no space —
    # "…NOT for simple asks.For research purpose where you'll need the
    # factual evidences … along with writing something about that
    # information in depth" — and the second half told the planner that
    # research should also do the writing, contradicting the first half and
    # CONTENT's own line. One statement now, and it says the same thing
    # whichever way it is read.
    "research": "facts from the live web the model would not already know — "
                "current prices, named companies, recent events, papers, "
                "citations, scraping a real site. NOT for analysing material "
                "the person has already given you, and NOT for a simple ask.",
    "leads": "finding WHO to approach — real companies, named decision-makers and their "
             "contact email addresses. Turn this on for anything shaped like 'find "
             "customers / prospects / leads / companies in <place or industry>', or an "
             "outreach task that names no recipients. It answers WHO, never WHAT: it "
             "does not write the email, that is content or brains. Independent of "
             "research — a run may need research to decide which industry to target "
             "and leads to pull the companies in it, so turning BOTH on is normal.",
    "brains": "the DEFAULT workhorse — analysis, reasoning, strategy, "
              "architecture, planning, AND short written outputs like briefs, "
              "plans and explanations. Small tasks usually need ONLY this step.",
    "content": "ONLY when the deliverable is a SUBSTANTIAL written piece (full article, essay, long-form "
               "copy, script, documentation). Short text, answers and briefs belong to brains, not here.",
    "visual": "generating images, art, character designs, logos, illustrations.",
    "media": "generating VIDEO only — footage, animation, reels, AI avatars. "
             "Voice-over and music are a SEPARATE stage (audio), so a reel that "
             "needs narration should turn BOTH on, not one.",
    "audio": "generating AUDIO only — voice-over, narration, dubbing, music or an "
             "audio explainer. Turn this on alongside media when a video needs a "
             "voice, or alone when the deliverable is sound.",
    "development": "building/deploying a web app, website, UI, or software tool from a spec.",
    "presentation": "building an actual slide deck / PowerPoint / pitch presentation or narrative site.",
    "summary": "synthesising ALL earlier stage outputs into one clean final answer.",
}


def _stage_lines(agents: dict, premium: list | None = None) -> str:
    # One tool often carries several steps, and its description used to be
    # repeated in full for each of them — ChatGPT's 437-character line five
    # times over on the owner's configuration, 2,185 characters of the
    # planner's budget saying the same thing. Said once, referred to after.
    said: dict[str, str] = {}
    premium = premium or []
    lines = []
    for stage in A.PIPELINE_ORDER:
        if stage == "summary":
            name = A.summary_agent_name(agents)
            if not name:
                continue
        else:
            name = agents.get(stage)
            if not name:
                continue
        if name in said:
            spec = f"as described under {said[name]} above"
        else:
            spec = A.specialty_for(stage, name)
            said[name] = stage.upper()
        star = "  ⭐ PREMIUM (the user pays for this tool)" if name in premium else ""
        # The inner quotes are single on purpose: reusing double quotes inside a
        # double-quoted f-string is PEP 701 syntax and only parses on Python
        # 3.12+. Prism supports 3.10+, and on anything older this is a
        # SyntaxError raised at IMPORT time — so the whole engine, and the GUI
        # that imports it through core_bridge, failed to start at all.
        makes = (A.AGENT_REGISTRY.get(name) or {}).get("makes", "")
        maker = (f"\n    MAKES: {makes}. Its stage's deliverable is the thing "
                 f"itself — brief it to BUILD that, never to write text about it."
                 if makes else "")
        # The skills this step may use, by key; each is described once, in
        # the "skills" rule under WHAT TO WRITE (core/skills.py).
        skill_line = SK.stage_line(stage)
        lines.append(f"- {stage.upper()} → {name}: {spec}{star}{maker}\n"
                     f"    USE FOR: {_STAGE_HELP.get(stage, '')}"
                     + (f"\n{skill_line}" if skill_line else ""))
    return "\n".join(lines)


def _contract_kind(stage: str) -> str:
    from . import contract as _contract
    return _contract.for_stage(stage)


def _schema_stub(agents: dict) -> str:
    parts = []
    for stage in A.PIPELINE_ORDER:
        if stage == "summary":
            if not A.summary_agent_name(agents):
                continue
        elif not agents.get(stage):
            continue
        # The example shows each step's OWN default kind. It showed "text"
        # on every line, and a model that copies the example turns "Make the
        # images" into a text step — no image line, no image check.
        kind = _contract_kind(stage)
        skilled = ', "skills": []' if SK.for_stage(stage) else ""
        parts.append(f'  "{stage}": {{ "needed": false, "kind": "{kind}", '
                     f'"questions": ["..."]{skilled} }}')
    return "{\n" + ",\n".join(parts) + "\n}"


def _attached_line(attachments) -> str:
    """What is really attached, in a line the brief step cannot misread. The
    brief was inventing attachments ("incorporate the attached cultural study
    excerpts") from a request that only MENTIONED a study."""
    names = [str(a.get("name") or "").strip() for a in (attachments or [])
             if isinstance(a, dict)]
    names = [n for n in names if n]
    if names:
        return "Files the user attached: " + ", ".join(names) + ".\n"
    return "Files the user attached: none.\n"


def enrich_query(query: str, profile: str, api_key: str, model: str,
                 attachments: list | None = None) -> str:
    """Pre-pass: expand the user's raw request into a professional task brief.
    This is what separates a human's one-liner from an engineered prompt — the
    router then writes every stage prompt FROM this brief. Returns "" on any
    failure so routing still works without it.

    The brief sharpens the request and never enlarges it. Told to fill in the
    deliverable, the constraints and "things the user didn't say", the model
    invented them: on 21 Sep 2026 the briefs for plain reel requests asked for
    ".docx (script) + .pdf (storyboard)", a ₹12,000 budget and "the attached
    cultural study excerpts" -- none of it asked for, nothing attached -- and
    the planner then wrote a PDF step and attached a document rulebook to it."""
    profile_line = f'The user describes themselves as: "{profile}".\n' if profile else ""
    attach_line = _attached_line(attachments)
    prompt = f"""You are a senior prompt engineer. Expand the raw request below into a crisp
professional TASK BRIEF that a downstream AI pipeline will use to write prompts.
Do NOT answer or perform the task itself.

THE BRIEF SHARPENS THE REQUEST; IT NEVER ENLARGES IT.
- Every deliverable, file type, tool, platform, length, budget and deadline in
  the brief must come from the raw request. Where the request does not say,
  write "not specified" -- never fill the gap with a guess. A file type nobody
  named is "not specified", not ".pdf".
- Do not add a second deliverable beside the one asked for: no extra script
  file, storyboard PDF, report, deck or spreadsheet.
- A file is attached only if it is listed below. Mentioning a study, a report
  or a source in the request does not make it an attachment.

{attach_line}{profile_line}Cover, in at most 220 words, as plain bullet lines:
- GOAL: the outcome the user actually wants (read intent, not just words)
- DELIVERABLE & FORMAT: the artefact(s) the user asked for and the structure/sections it needs
- AUDIENCE & TONE
- SCOPE: explicitly IN and explicitly OUT (respect words like "only" / "don't")
- CONSTRAINTS & GIVENS: only what the request states -- languages, budget, sources, attached material -- or "none stated"
- QUALITY BAR: 2-3 criteria a professional result must meet, judged from the result itself
- IMPLICIT NEEDS: what a professional would put INSIDE the requested deliverable that the user didn't spell out -- never extra files, formats, tools or work products

Raw request:
{query}

Return ONLY the brief as plain text bullets — no preamble, no commentary."""
    # Enrichment is a nicety — routing works without it — so a failure here
    # returns empty rather than raising and taking the plan down with it.
    try:
        return groq_chat(api_key, model, prompt, timeout=45).strip()
    except Exception:                                   # noqa: BLE001
        return ""


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def detect_named_tools(query: str) -> dict:
    """If the user explicitly NAMES one of Prism's own tools in their query
    ("using NotebookLM", "notebook lm video generation", "via Claude Design")
    that's a direct order, not a maybe — the suggestion mechanism's LLM
    judgement call shouldn't be the only path to honouring it. Fuzzy-matched
    (spacing/case-insensitive) so "notebook lm" still matches "NotebookLM".
    Returns {stage: tool_name} for every category that tool belongs to.
    Short/common names (<4 normalized chars) are skipped to avoid false hits."""
    q_norm = _norm_name(query)
    out = {}
    for name in A.AGENT_REGISTRY:
        n_norm = _norm_name(name)
        if len(n_norm) < 4 or n_norm not in q_norm:
            continue
        for stage, cat in A.CATEGORIES.items():
            if name in cat["agents"]:
                out.setdefault(stage, name)
    return out


def suggest_alternatives(query: str, brief: str, routing: dict, agents: dict,
                         api_key: str, model: str) -> list[dict]:
    """For each stage the router marked needed, check whether one of the
    OTHER tools listed under that stage's category (ones the user did NOT
    default to) would clearly suit THIS specific task better — e.g. NotebookLM
    over Perplexity when the task is really about grounding in the user's own
    uploaded documents. Deliberately conservative: returns [] unless there's a
    strong, concrete reason, so most runs get no suggestion at all. Fails
    soft — any error just means no suggestions this time."""
    candidates = []
    for stage in A.PIPELINE_ORDER:
        data = routing.get(stage)
        if not data or not data.get("needed"):
            continue
        current = A.summary_agent_name(agents) if stage == "summary" else agents.get(stage)
        if not current or current not in A.AGENT_REGISTRY:
            continue
        cat = A.CATEGORIES.get(stage)
        if not cat:
            continue
        alts = [n for n in cat["agents"] if n != current and n in A.AGENT_REGISTRY]
        if not alts:
            continue
        alt_lines = "\n".join(f"  - {n}: {A.AGENT_REGISTRY[n]['specialty']}" for n in alts)
        candidates.append(
            f"STAGE: {stage}\nCurrently assigned: {current} "
            f"({A.AGENT_REGISTRY[current]['specialty']})\n"
            f"Other tools available for this stage:\n{alt_lines}"
        )
    if not candidates:
        return []

    notes = _tool_notes()
    notes_block = (
        f"\nThe user's OWN field notes on these tools, written from real hands-on\n"
        f"experience — these are FACTS about the tools, not generic marketing text.\n"
        f"Weigh them more heavily than the one-line specialty strings above, and\n"
        f"apply any routing rule in them that matches this task:\n{notes}\n"
        if notes else ""
    )

    prompt = f"""You are auditing an AI-tool routing plan before it runs.
TASK BRIEF:
{brief or query}
{notes_block}
For EACH stage below, decide if one of the "other tools available" would
CLEARLY perform this SPECIFIC task better than the currently assigned tool —
not marginally, only for a strong, concrete reason (e.g. the task is really
about grounding in the user's OWN uploaded documents, which fits NotebookLM
far better than a general web-search tool). If the current pick is perfectly
fine, suggest nothing for that stage — most stages should get NO suggestion.

{chr(10).join(candidates)}

Return ONLY a JSON array (empty if nothing stands out):
[{{"stage": "...", "current": "...", "suggested": "...", "reason": "one sentence"}}]"""
    try:
        text = groq_chat(api_key, model, prompt, temperature=0, timeout=45,
                         retries=0)
        s, e = text.find("["), text.rfind("]") + 1
        data = json.loads(text[s:e]) if s != -1 and e > s else []
        out = []
        for item in data:
            stage, suggested = item.get("stage"), item.get("suggested")
            if (stage in routing and suggested in A.AGENT_REGISTRY
                    and suggested != item.get("current")):
                out.append({
                    "stage": stage,
                    "current": item.get("current") or agents.get(stage, ""),
                    "suggested": suggested,
                    "reason": (item.get("reason") or "").strip(),
                })
        return out
    except Exception:
        return []



# Tools that run their own multi-pass research loop. Prism's house style —
# "Your ONLY task is", an exhaustive deliverable spec, a fixed section list —
# reads to these as an instruction to stop after one pass, and they come back
# thinner than the tool they were chosen over. They need the goal and the
# quality bar, not the procedure.
#
# Read off the registry rather than listed here, so marking a new tool
# self-directing is one flag in agents.py and nothing else.
def _self_directing_names() -> list[str]:
    return sorted(name for name, cfg in A.AGENT_REGISTRY.items()
                  if cfg.get("prompt_style") == "natural")


def _self_directing_rule(agents: dict) -> str:
    """The carve-out, included only when such a tool is actually in the plan."""
    names = [n for n in _self_directing_names() if n in set(agents.values())]
    if not names:
        return ""
    listed = " and ".join(names)
    return (
        f"- SELF-DIRECTING TOOLS ({listed}). {listed} runs its own multi-pass\n"
        f"  research loop and scrapes the live web itself. Give its step the\n"
        f"  subject, the audience and what a good answer must cover — then let\n"
        f"  it choose how to get there. Do NOT prescribe the steps, the section\n"
        f"  list or the word count: over-specifying makes {listed} skip the\n"
        f"  analyse and optimise passes that are the reason it was picked over\n"
        f"  a plain search tool.\n")


def _maker_names(agents: dict) -> list[str]:
    return sorted(n for n in set(agents.values())
                  if A.is_maker(A.AGENT_REGISTRY.get(n) or {}))


def _maker_rule(agents: dict) -> str:
    """For a tool that builds the thing, the prompt is a brief to build it.

    Included only when such a tool is in the plan. Without this the planner
    writes every stage the same way -- a text deliverable spec with a
    "do not create files" non-goal -- and a deck-building tool obeys it and
    types the outline back (Canva, 2026-09-10).
    """
    names = _maker_names(agents)
    if not names:
        return ""
    listed = " and ".join(names)
    what = "; ".join(f"{n} makes {A.AGENT_REGISTRY[n]['makes']}" for n in names)
    return (
        f"- MAKER TOOLS ({listed}). {what}. For such a stage the deliverable is\n"
        f"  the THING, built inside that tool, and the prompt is a brief to build\n"
        f"  it -- written the way a senior person briefs a designer:\n"
        f"    • Name the tool actually assigned to the stage, never another one.\n"
        f"    • Say what to build (a 7-slide deck; one square post), what goes on\n"
        f"      it (the headings and bullets from the previous stage, verbatim, in\n"
        f"      order), the look (palette, tone, audience), and the size/count.\n"
        f"    • Never ask for \"plain text\", a \"text format\", a description,\n"
        f"      an outline, or an export file, and never say \"do not generate\n"
        f"      files\" -- the tool would obey and hand back words.\n"
        f"    • For AUDIO/VOICE tools (e.g. ElevenLabs, Suno): supply only the clean\n"
        f"      spoken voiceover narration lines to synthesize. Never include visual\n"
        f"      directions, sound-effect tags, JSON keys, metadata, or conversational handoffs.\n"
        f"    • DELIVERABLE SPEC = the built thing; QUALITY BAR = about the thing\n"
        f"      (every slide present, headings exact, readable at a glance).\n")


def _skills_rule(agents: dict) -> str:
    """How to use the SKILLS keys named in the step list above.

    A skill is house standards for a kind of deliverable, written by hand and
    checked by Prism, which types its text into the step's message itself.
    So the planner only NAMES one: in keeping with the rest of this prompt,
    the brief stays about the task and carries no rules (core/skills.py).
    """
    stages = list(agents) + ["summary"]
    if not SK.any_for_stages(stages):
        return ""
    return (
        "- \"skills\" — house standards for a kind of deliverable. The steps "
        "above name the ones each step may use; this is what each is for:\n"
        + SK.catalogue_text(stages) + "\n"
        "  Where one fits what a step is making, put its key in that step's "
        "\"skills\" list (at most two, the closest first); leave it empty "
        "when none fits. Prism types the skill's standards into that step's "
        "message itself, so the brief stays about this task and does not "
        "restate them.\n")


def build_prompt(query: str, profile: str, agents: dict, attachments: list | None = None,
                 premium: list | None = None, brief: str = "") -> str:
    profile_line = (
        f"The user describes themselves / their work as: \"{profile}\".\n"
        "Tailor every prompt to that context.\n\n" if profile else ""
    )
    from . import files as F
    attach_line = F.routing_note(attachments or [])
    notes = _notes_for(agents)
    notes_block = (
        "═══ FIELD NOTES on the tools in this plan (written by the user, "
        "from hands-on use) ═══\n"
        "How these tools behave in practice. Where a note says something the "
        "descriptions above do not, believe the note. It does not decide "
        "which steps run — the rules below do.\n"
        f"{notes}\n\n" if notes else ""
    )
    premium = premium or []
    enabled_premium = sorted({n for n in agents.values() if n in premium})
    premium_rule = (
        f"- PREMIUM PREFERENCE: the user PAYS for {', '.join(enabled_premium)}. When a "
        "piece of work could reasonably be carried by more than one enabled stage, give "
        "the bulk of it to the premium tool's stage — paid plans mean higher limits and "
        "better output. This only breaks ties: never violate the DELIVERABLE RULE or "
        "SCOPE LOCK, and never enable a stage the task doesn't need just because its "
        "tool is premium.\n" if enabled_premium else ""
    )
    self_directing_block = _self_directing_rule(agents)
    maker_block = _maker_rule(agents)
    skills_block = _skills_rule(agents)
    brief_block = (
        "\n═══ TASK BRIEF (auto-expanded from the raw request by a prompt-"
        "engineering pass; mine it for context, deliverable specs, quality "
        "criteria and non-goals when writing each stage prompt. It restates "
        "the request and never adds to it: where it names a deliverable, "
        "file type, budget or attachment that the person's own words do "
        "not, ignore that part) ═══\n"
        f"{brief}\n" if brief else ""
    )
    return f"""You are the planner of Prism — a desktop app that runs real AI
tools in a browser, one step after another, and collects what they produce.

{profile_line}{attach_line}These steps are available, in this order. Each one is carried by the tool
named, and each receives the previous step's answer as its context:

{_stage_lines(agents, premium)}

{notes_block}═══ WHICH STEPS RUN ═══
{premium_rule}- MAKE-STEPS. If the person asks for something to be MADE, the step that
  makes it MUST run — a thinking step only describes it. Map the thing to
  its step:
    image / logo / art / poster ............ VISUAL
    video / reel / animation ............... MEDIA
    voice-over / narration / music ......... AUDIO
    slide deck / presentation .............. PRESENTATION
    web app / website / tool ............... DEVELOPMENT
    document / report / article / script ... CONTENT
  Pair BRAINS with the make-step when the thing needs thinking about first.
- WORDS FIRST. A reel, video or deck needs a script, captions or slide copy,
  and writing those is CONTENT's job, not BRAINS'.
- RESEARCH is for facts from the live web the model would not already know —
  real prices, named companies, recent events, citations. Not for material
  the person has already given you, and not for a simple request.
- ONE STEP IS OFTEN ENOUGH. A question, an analysis or a short piece of
  writing is BRAINS alone. Steps that are not needed make a run slower, not
  better — but never drop a make-step to keep the plan short.
- SUMMARY only when three or more steps ran and their answers need pulling
  together.
- Set "needed": false and "questions": [] for a step that does not apply.
  Never invent a step that is not listed above.

═══ WHAT TO WRITE FOR EACH STEP ═══
- "kind" — what must EXIST when the step ends. Pick exactly one:
    text    an answer written in the chat
    file    a document, deck or spreadsheet the person can download
    image   generated picture(s)
    video   a finished video file
    data    a reply a program will read
    links   companies, contacts or rows
  If the person asked for a .docx, a PDF, a spreadsheet, a deck "as a file"
  or anything to download, that step's kind is "file", never "text".
- "questions" — ONE line of at most 25 words, in plain language, saying
  what this step does towards the person's request ("For this step: …").
  The tool is sent the person's own words, their files and the earlier
  step's hand-off beside it, so the line only has to say which part of the
  job is this step's. Nothing else.
- Do NOT write a role to play ("act as a senior…"), a section list, a word
  count, a quality bar, non-goals, formatting rules, background facts, or
  anything about handing over to the next step. Prism adds what it needs
  itself, and a second set of instructions in the same message is what
  makes a tool answer ABOUT the instructions instead of doing the work.
- Never assert something the step will not actually have. Only write a fact
  into a brief if the person gave it or an earlier step will really produce
  it — a brief that says "you have been given the brand colours" when nobody
  found any is worse than one that asks for them.
{self_directing_block}{maker_block}{skills_block}{brief_block}
The person's own request — authoritative on scope, and it wins over the
brief wherever the two differ:
{query}

Return ONLY this JSON (no markdown, no commentary), using exactly these keys:
{_schema_stub(agents)}"""



def verify_key(api_key: str, model: str = "") -> str:
    """Check a Groq key against Groq. Returns "" if it works, else why not.

    Both the CLI and the GUI only ever checked that a key starts with 'gsk_'
    and is long enough, which a typo, a revoked key and a key pasted with half
    a newline in it all pass. The first sign of trouble was then the first real
    task failing with a raw HTTP 401 body — minutes after setup, and worded for
    a developer. This is the same question asked at the moment the key is
    entered, when it is still obvious what to do about the answer.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        return "No key entered."
    if not api_key.startswith("gsk_"):
        return "A Groq key starts with 'gsk_' — this doesn't look like one."
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
    except Exception as e:
        return (f"Couldn't reach Groq to check the key ({e}).\nIf you're behind "
                "a proxy or offline, the key may still be fine.")
    if resp.status_code == 401:
        return ("Groq rejected this key. Make a new one at console.groq.com/keys "
                "and paste it again — copying it twice by accident is the usual "
                "cause.")
    if resp.status_code == 429:
        return ("The key is valid, but Groq is rate-limiting it right now. "
                "Routing may be slow until that clears.")
    if resp.status_code != 200:
        return f"Groq answered HTTP {resp.status_code} — the key may not be usable."
    if model:
        try:
            names = {m.get("id") for m in resp.json().get("data", [])}
            if names and model not in names:
                return (f"The key works, but '{model}' isn't available on it. "
                        "Prism will fall back to whatever Groq allows.")
        except Exception:
            pass
    return ""


def _escape_inner_quotes(block: str) -> str:
    """Escape a raw double quote a model left INSIDE a JSON string.

    The plan's prompts are about writing, so they quote things — `Act as a
    "senior" strategist` — and a model that has just been told to write
    engaging copy does not always remember that the quote has to be `\\"`
    once it is inside a JSON string. Walks the text tracking whether it is
    inside a string; a quote there that is not followed (after whitespace)
    by `,` `}` `]` or `:` cannot be the string's end, so it is escaped.
    """
    out, in_str, esc = [], False, False
    n = len(block)
    for i, ch in enumerate(block):
        if in_str:
            if esc:
                esc = False
                out.append(ch)
                continue
            if ch == "\\":
                esc = True
                out.append(ch)
                continue
            if ch == '"':
                j = i + 1
                while j < n and block[j] in " \t\r\n":
                    j += 1
                if j >= n or block[j] in ",}]:":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _parse_plan(text: str):
    """The routing dict out of a planner reply — (dict, None), or (None, why).

    Tries the reply as written, then the repairs a scraped browser reply
    already gets (comments, curly quotes, trailing commas, control
    characters in strings — core.reel._loosen), then the one fault a
    planner makes that those do not cover: a raw quote inside a string.
    """
    from . import reel as _reel
    text = text or ""
    blocks = _reel._blocks(text, "{", "}")
    s, e = text.find("{"), text.rfind("}") + 1
    if s != -1 and e > s and text[s:e] not in blocks:
        # An unbalanced quote throws the string-aware brace scan off; the
        # outermost slice is the plain-minded fallback.
        blocks.append(text[s:e])
    if not blocks:
        return None, "no JSON object in the reply"
    why = None
    for block in blocks:
        for candidate in (block, _reel._loosen(block),
                          _escape_inner_quotes(block),
                          _escape_inner_quotes(_reel._loosen(block))):
            try:
                got = json.loads(candidate)
            except Exception as err:                       # noqa: BLE001
                why = why or err
                continue
            if isinstance(got, dict):
                return got, None
    return None, why or "the reply was not a JSON object"


def title_for(query: str, brief: str, api_key: str, model: str) -> str:
    """A short name for the job — what a folder, a History row and a chat
    should be called. Six words at most, no verb, no quotes: "Instagram
    reel · brand guide" for "make a reel for instgram for this brand".
    Until now every one of those surfaces carried the request verbatim,
    typos and all, and every chat Prism opened was titled by its own
    opening line — "Senior Creative Director Task", forty times over.
    Never raises: a planner that cannot name the job gets the request's
    first words instead."""
    prompt = (
        "Name this job in at most six words, the way a folder or a chat "
        "thread would be named: what is being made, and for whom or about "
        "what. No verbs like 'create' or 'make', no quotes, no trailing "
        "punctuation, spell names correctly even if the request does not.\n\n"
        f"The request: {query}\n"
        + (f"\nThe brief: {brief[:600]}\n" if brief else "")
        + '\nReply with ONLY a JSON object: {"title": "..."}')
    try:
        out = groq_chat(api_key, model, prompt, temperature=0, timeout=20,
                        retries=0, json_mode=True)
        title = C.tidy_title(str(json.loads(out).get("title", "")))
    except Exception:                                    # noqa: BLE001
        title = ""
    return title or C.fallback_title(query)


def planned_steps(routing: dict, agents: dict) -> list[tuple[str, str]]:
    """(stage, tool) for every step the router's plan would run, in order --
    the same reading automation._needed_stages makes, before the person
    has touched anything."""
    out = []
    for stage in A.PIPELINE_ORDER:
        data = (routing or {}).get(stage) or {}
        if not data.get("needed"):
            continue
        if not [q for q in (data.get("questions") or []) if q and str(q).strip()]:
            continue
        name = (A.summary_agent_name(agents) if stage == "summary"
                else data.get("agent_override") or agents.get(stage))
        if name:
            out.append((stage, name))
    return out


def plan_changed(planned: list, confirmed: list) -> bool:
    """Did the person change the plan before pressing Start?

    Two lists of (stage, tool). Any difference in membership, order or tool
    counts -- the prompts were written for the plan as routed, and a prompt
    written for Gamma names Gamma even when the step now runs on Canva
    (the owner's deck run of 2026-09-10). A step with no prompt at all
    counts as changed too; it has nothing to send.
    """
    def key(steps):
        return [(str(s[0]), str(s[1])) for s in steps]
    if key(planned) != key(confirmed):
        return True
    return any(len(s) > 2 and not [q for q in (s[2] or []) if q and str(q).strip()]
               for s in confirmed)


def passthrough_prompt(query: str, stage: str, brief: str = "") -> str:
    """The floor under a step with no prompt: the person's own words,
    scoped to that step's job. Never a great prompt, always a real one."""
    job = A.STEP_NAMES.get(stage) or stage.replace("_", " ")
    job = job[0].lower() + job[1:]
    # `brief` (the 220-word engineered task brief) is deliberately not
    # carried: the person's words are the prompt now (Round 31).
    return f"For this step: {job.rstrip('.')} — {query.strip()}"


def brief_confirmed_plan(query: str, cfg: dict, steps: list,
                         routing: dict | None = None) -> list:
    """Write the prompts for the plan AS THE PERSON CONFIRMED IT.

    `steps` is the ordered [(stage, tool, draft questions)] the Plan screen
    is about to run. The router wrote its prompts before anyone looked at
    the plan; if a step was dropped, added, moved, or given a different tool,
    those prompts are wrong -- they name the old tool, hand off to a step
    that no longer follows, or do not exist. One Groq call rewrites them for
    exactly these steps, in this order, on these tools: the drafts are the
    substance to keep, the tools and the order are the truth.

    Returns the same list with the questions replaced. Never raises: on a
    failure the drafts stand, and a step with no draft gets the floor
    (passthrough_prompt), so the run always has something real to send.
    """
    steps = [(str(s[0]), str(s[1]), [q for q in (s[2] if len(s) > 2 else [])
                                     if q and str(q).strip()]) for s in steps]
    brief = ((routing or {}).get("_brief") or "").strip()
    api_key = cfg.get("api_key")
    model = cfg.get("model", "llama-3.3-70b-versatile")

    def floor(step):
        stage, tool, qs = step
        return (stage, tool, qs or [passthrough_prompt(query, stage, brief)])

    if not api_key or not steps:
        return [floor(s) for s in steps]

    profile = cfg.get("profile", "")
    profile_line = (f"The user describes themselves / their work as: "
                    f"\"{profile}\". Tailor every prompt to that context.\n\n"
                    if profile else "")
    lines = []
    for i, (stage, tool, qs) in enumerate(steps, 1):
        meta = A.CATEGORIES.get(stage) or {}
        entry = A.AGENT_REGISTRY.get(tool) or {}
        makes = entry.get("makes", "")
        draft = " ".join(" ".join(qs).split())[:1200] if qs else "(no draft — write it)"
        lines.append(
            f"STEP {i} — {stage.upper()} on {tool}"
            f"{' (last step)' if i == len(steps) else ''}\n"
            f"  what this tool is: {entry.get('specialty', 'general-purpose AI')}\n"
            + (f"  MAKES: {makes} — brief it to BUILD that, never to write "
               f"text about it\n" if makes else "")
            + f"  the step's job: {meta.get('desc', meta.get('label', stage))}\n"
            f"  draft prompt: {draft}")
    agents = {stage: tool for stage, tool, _ in steps}
    maker_block = _maker_rule(agents)
    brief_block = f"The task brief the drafts were written from:\n{brief}\n\n" if brief else ""
    prompt = f"""You are the routing brain of Prism — a multi-agent AI pipeline.
The person has looked at the plan and CONFIRMED these steps, in this order,
on these tools. Write the final prompt for every step, for this plan exactly.

{profile_line}{brief_block}User's raw request (authoritative on scope):
{query}

THE CONFIRMED PLAN:
{chr(10).join(lines)}

═══ RULES ═══
- One prompt per step, in the order above. A prompt is ONE line of at most
  25 words, in plain language, saying what this step does towards the
  person's request ("For this step: …"). The drafts say what each step is
  for — keep that; drop everything else in them.
- The TOOL and the ORDER above are the truth: a line that names a tool
  names the one the step actually runs on, never another.
- The tool is sent the person's own words, their files and the earlier
  step's hand-off beside the line, and Prism itself says what to hand back
  and how to hand over. So: no role to play, no section list, no word count,
  no quality bar, no non-goals, no formatting rules, no background facts,
  nothing about the next step or about being the last step.
{maker_block}

Return ONLY this JSON (no markdown, no commentary):
{{"steps": [{{"stage": "<stage>", "questions": ["<prompt>"]}}, ...]}}"""
    try:
        try:
            text = groq_chat(api_key, model, prompt, timeout=60, json_mode=True)
        except RuntimeError:
            text = groq_chat(api_key, model, prompt, timeout=60)
    except Exception as e:                                  # noqa: BLE001
        ui.warn(f"could not rewrite the prompts for the confirmed plan ({e}) "
                "— running with the drafts")
        return [floor(s) for s in steps]
    got, _why = _parse_plan(text)
    raw = got.get("steps") if isinstance(got, dict) else None
    if not isinstance(raw, list) or len(raw) != len(steps):
        ui.warn("the rewritten prompts did not match the plan — running with "
                "the drafts")
        return [floor(s) for s in steps]
    out = []
    for step, item in zip(steps, raw):
        qs = (item or {}).get("questions") if isinstance(item, dict) else None
        if isinstance(qs, str):
            qs = [qs]
        qs = [str(q).strip() for q in (qs or []) if q and str(q).strip()]
        out.append((step[0], step[1], qs) if qs else floor(step))
    ui.info("✍️  prompts written for the plan as you confirmed it")
    return out


def route(query: str, cfg: dict, attachments: list | None = None) -> dict:
    """Call Groq and return the routing dict (stage -> {questions, needed})."""
    agents = {k: v for k, v in (cfg.get("agents") or {}).items() if v}
    if not agents:
        raise ValueError("No agents configured. Run /agents to pick some first.")

    api_key = cfg.get("api_key")
    if not api_key:
        raise ValueError("No Groq API key configured. Run /key to add one.")

    # Pass 1 — enrichment: expand the raw ask into a professional task brief.
    model = cfg.get("model", "llama-3.3-70b-versatile")
    brief = ""
    try:
        brief = enrich_query(query, cfg.get("profile", ""), api_key, model,
                             attachments)
        if brief:
            ui.info("🪄  expanded your request into a professional task brief")
    except Exception:
        brief = ""  # routing still works without the brief

    # Pass 2 — routing: pick stages and write engineered prompts from the brief.
    prompt = build_prompt(query, cfg.get("profile", ""), agents, attachments,
                          premium=cfg.get("premium") or [], brief=brief)
    # The one call the whole plan depends on, so this is the one that gets a
    # retry and the full model chain.
    text = groq_chat(api_key, model, prompt, timeout=60)
    routing, why = _parse_plan(text)
    if routing is None:
        # "Expecting ',' delimiter: line 16 column 49" — twice in a row on
        # 2026-09-07, a raw quote inside a prompt string, and each time the
        # whole plan was thrown away behind a "Something went wrong" dialog
        # with the parser's words in it. The repairs in _parse_plan catch
        # most of that; what they cannot, one more ask with the error quoted
        # back does — in JSON mode, where Groq itself refuses to hand back
        # anything that does not parse.
        ui.warn(f"the plan came back as JSON that would not parse ({why}) "
                "— asking once more")
        again = (prompt + "\n\nYOUR PREVIOUS REPLY WAS NOT VALID JSON — the "
                 f"parser said: {why}. Reply again with ONLY the JSON "
                 "object: every double quote inside a string escaped as "
                 "\\\", no comments, no trailing commas, nothing before or "
                 "after it.")
        try:
            text = groq_chat(api_key, model, again, timeout=60, json_mode=True)
        except RuntimeError:
            text = groq_chat(api_key, model, again, timeout=60)
        routing, why = _parse_plan(text)
    if routing is None:
        raise RuntimeError(
            "Prism's planner wrote a plan it could not read back "
            f"({why}). This is usually momentary — press Make a plan again.")

    # Deterministic safety net: force make-stages the user clearly asked for.
    forced = apply_make_guardrail(query, routing, agents)
    if forced:
        pretty = ", ".join(f"{s} ({agents.get(s) or A.summary_agent_name(agents)})" for s in forced)
        ui.info(f"🛡️  guardrail enabled required stage(s): {pretty}")
    if apply_research_guardrail(query, routing, agents):
        ui.info(f"🛡️  guardrail enabled Look things up ({agents['research']}) for research task")
    if apply_research_reasoning_guardrail(routing, agents):
        ui.info(f"🛡️  guardrail enabled Think it through ({agents['brains']}) "
                "after research")
    if apply_script_guardrail(routing, agents):
        ui.info(f"🛡️  guardrail enabled content ({agents['content']}) — "
                "the reel/deck's script is a content job, not a brains job")
    studio_swap = apply_studio_guardrail(query, routing, agents)
    if studio_swap:
        ui.info(f"🛡️  guardrail: {studio_swap}")
    imagery = apply_reel_imagery_guardrail(query, routing, agents)
    if imagery == "on":
        ui.info("🖼️   the reel gets its own generated artwork — Prism makes "
                "the pictures for its scenes")
    elif imagery == "off":
        ui.info("🖼️   type and colour only, as asked — no pictures will be "
                "generated for the reel")
    # Before the skills pass, so a skill is only ever offered a stage that
    # is actually going to run.
    if apply_boq_file_guardrail(query, routing, agents, attachments):
        ui.info("🛡️  guardrail: a BOQ/BOM from an attached file is one "
                "document step, not a plan")
    # Surface the enrichment brief so the UI can show the full transformation
    # chain (raw words → brief → stage prompts). Consumers iterate
    # PIPELINE_ORDER, so this extra key is invisible to them.
    # Which skills each step actually runs with. The planner's pick is
    # validated against what exists for that step; where it chose none and
    # the step is running, the person's own words decide -- the same
    # deterministic backstop as the make-step guardrail above.
    picked = SK.assign(query, routing, list(A.PIPELINE_ORDER))
    if picked:
        for stage, keys in picked.items():
            ui.info(f"📘  {stage}: skill(s) {', '.join(keys)}")

    routing["_brief"] = brief
    # The job's name, for the run folder, History, Home and every chat's
    # title. Same "invisible to consumers" convention as _brief.
    try:
        routing["_title"] = title_for(query, brief, api_key, model)
    except Exception:                                    # noqa: BLE001
        routing["_title"] = C.fallback_title(query)
    try:
        routing["_named_tools"] = detect_named_tools(query)
    except Exception:
        routing["_named_tools"] = {}
    try:
        routing["_suggestions"] = suggest_alternatives(query, brief, routing, agents,
                                                        api_key, model)
    except Exception:
        routing["_suggestions"] = []
    return routing
