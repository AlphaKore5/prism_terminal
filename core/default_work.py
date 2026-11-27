"""
Prism -- "Prism default work": the person's own task, done in their own browser
───────────────────────────────────────────────────────────────────────────────
A task typed on Home runs as a chain of steps: look things up, think it
through, write it up, build it... Those steps are worked in the person's own
Chrome, signed in to their own AI accounts, so they cost no Prism AI credits.

What stays on Prism's server (core/server_session.py), unchanged:
  - the planner,
  - pictures and video, including the steps that only feed a video (the
    script a renderer films, the art direction, the storyboard),
  - every add-on.

One switch per run: automation.run(default_work=True). Only the Home task and
its follow-ups pass it; an add-on never does, so an add-on can never open a
browser by accident. The host app turns the feature on with enable().
PRISM_DEFAULT_WORK=server turns it off again, which gives the all-server
behaviour back without a new build.

This module is the one place that answers "does this step run in the browser,
and which tools may be picked for it". The engine, the planner's estimate and
the screens all ask here.
"""
from __future__ import annotations

import contextlib
import os
import threading

from . import ai_client

#: Step -> the tools offered for it, the first being the default (owner,
#: 27 Nov 2026). Pictures and video are not here: they did not move.
CHOICES: dict[str, tuple[str, ...]] = {
    "research": ("ChatGPT", "Grok", "Claude"),
    "leads": ("ChatGPT", "Apollo"),
    "brains": ("Claude", "ChatGPT"),
    "content": ("ChatGPT", "Claude", "Kimi 2.6"),
    "development": ("Claude", "v0.dev", "emergent.sh", "Kimi 2.6"),
    "presentation": ("Claude", "Gamma.app", "Canva"),
    "audio": ("ElevenLabs", "Sarvam"),
}

#: "Read the files" is not a step anyone plans (the engine adds it when files
#: are attached), so its tool is its own setting, cfg["analysis_agent"].
FILE_READERS = ("ChatGPT", "Claude")

#: Every step that runs in the browser. "summary" uses the brains tool and
#: "followup" is a reply in the conversation.
STAGES = frozenset(CHOICES) | {"analysis", "summary", "followup"}

#: Steps filled with the default tool when the person picked none, besides the
#: ones server mode already fills. Leads and voice stay empty until picked:
#: a filled voice step would add a voice-over to videos nobody asked one for.
FILLED = ("development", "presentation")

_VIDEO_RENDERERS = frozenset({"reel", "reel_web", "motion"})
_SCRIPT_STAGES = ("content", "brains")
_OFF_WORDS = ("server", "off", "0", "false", "no")

_enabled = False
_run = threading.local()


def enable(on: bool = True) -> None:
    """The host app offers browser-worked default work (it has the screens to
    pick the tools and to sign in to them)."""
    global _enabled
    _enabled = bool(on)


def switched_off() -> bool:
    """PRISM_DEFAULT_WORK=server: keep every step on Prism's server."""
    return os.environ.get("PRISM_DEFAULT_WORK", "").strip().lower() in _OFF_WORDS


def enabled() -> bool:
    """True when AI runs through Prism's server AND the person's own task is
    worked in their browser. (With PRISM_AI_DIRECT the old path runs
    everything in the browser and none of this applies.)"""
    return _enabled and ai_client.active() and not switched_off()


@contextlib.contextmanager
def running(on: bool):
    """Mark this thread as being inside a browser-worked default-work run."""
    before = getattr(_run, "on", False)
    _run.on = bool(on)
    try:
        yield
    finally:
        _run.on = before


def in_browser_run() -> bool:
    return bool(getattr(_run, "on", False))


def _entry(agent: str) -> dict:
    from . import agents as A
    return A.AGENT_REGISTRY.get(agent) or {}


def pick(stage: str, agent: str) -> str:
    """The tool a browser step really uses: the one asked for when it is on
    offer, else the step's default. Steps without a list, and Prism's own
    local tools, are returned unchanged."""
    offered = CHOICES.get(stage)
    if not offered or agent in offered or _entry(agent).get("local"):
        return agent
    return offered[0]


def picks(agents: dict | None) -> dict:
    """The saved stage -> tool map with every browser step held to its list.
    An older config naming a tool no longer offered gets the default."""
    return {stage: pick(stage, name)
            for stage, name in (agents or {}).items() if name}


def file_reader(cfg: dict | None) -> str:
    name = (cfg or {}).get("analysis_agent")
    return name if name in FILE_READERS else FILE_READERS[0]


def video_script_steps(steps) -> set[int]:
    """Indexes of the writing steps a video renderer films from. They are part
    of making the video, so they stay on Prism's server."""
    out: set[int] = set()
    for r, step in enumerate(steps):
        if _entry(step[1]).get("local") not in _VIDEO_RENDERERS:
            continue
        writer = next((i for i in range(r - 1, -1, -1)
                       if steps[i][0] in _SCRIPT_STAGES), None)
        if writer is not None:
            out.add(writer)
    return out


def step_in_browser(stage: str, agent: str, *, video_script: bool = False) -> bool:
    """Does this step run in the browser (in a default-work run)?"""
    return (stage in STAGES and not video_script
            and not _entry(agent).get("local"))


def places(steps) -> list[str]:
    """"browser" or "server" for each (stage, tool, ...) of a plan."""
    scripts = video_script_steps(steps)
    return ["browser" if step_in_browser(s[0], s[1], video_script=i in scripts)
            else "server" for i, s in enumerate(steps)]


def sign_in_picks(agents: dict | None, cfg: dict | None = None) -> dict:
    """Stage -> tool for the tools that need a sign-in: the browser steps'
    picks and the file reader, each tool once."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    wanted = picks(agents)
    wanted["analysis"] = file_reader(cfg)
    for stage, name in wanted.items():
        if (stage not in STAGES or name in seen
                or not step_in_browser(stage, name)):
            continue
        seen.add(name)
        out[stage] = name
    return out
