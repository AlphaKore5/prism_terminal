"""
Prism — the default "visual" stage: FLUX 3 Klein, called directly through
OpenRouter, replacing ChatGPT as the picture-generation step most runs used
to drive in a browser.

Runs INSIDE Prism — no tab, no DALL-E account, no upload — the same shape
core.automation's other local renderers (Prism Studio/Reel/Motion) already
use: registered in core.agents.AGENT_REGISTRY as "Prism Image" with
local="flux_image", dispatched by core.automation._run_local(), and first
in CATEGORIES["visual"]["agents"] so a fresh install's default visual tool
is this API call, not a browser tab (roles.default_agents() picks the
first name in a category's own list).

The owner's own words: "replace that particular step where usually chatgpt
is responsible for the generation of images... with image generation
through API key and we are already using openrouter API key, and the model
for this particular purpose we would be choosing: FLUX 3 Klein... made
that thing as the default and first choice."
"""
from __future__ import annotations

import datetime as _dt
import os
import time

from . import ai_client as _ai
from . import openrouter as _openrouter

# OpenRouter's own id for the model (confirmed live against
# openrouter.ai/black-forest-labs/flux-3-image-klein, 2026-11-07) — the
# Images API, POST /api/v1/images, not /chat/completions: this model's
# deliverable IS the picture, not prose describing one.
MODEL = "black-forest-labs/flux-3-image-klein"

# The text model that writes FLUX's own prompts (see build_prompts below) —
# same default as STEP's Ask, Claude Haiku 5.5, a real in-house OpenRouter
# call, not a browser tab, so the whole "artwork" stage stays local end to
# end when Prism Image is the chosen tool.
BRIEF_MODEL = "anthropic/claude-haiku-5.5"

# core.reel_web.imagery_instructions' own asset list (kept in sync by hand —
# the two are read by different audiences: that one tells a browser tool
# what to do turn by turn, this one is folded into ONE meta-prompt a text
# model turns into N complete, self-contained picture briefs).
_ASSET_ROLES = (
    "the PRIMARY HERO SUBJECT of the business — its main equipment, "
    "flagship product, or core offering",
    "PROCESS / ACTION — their craft, manufacturing, engineering or "
    "workflow, happening",
    "SECONDARY DETAIL / TEXTURE / MATERIAL — one close-up component or "
    "raw material that proves domain mastery",
    "TRUST / BADGE / METRIC — an icon or stylised badge standing for "
    "quality, speed or precision",
    "OUTCOME / FINISH — the finished product or the dramatic closing "
    "visual",
)
_WORDMARK_ROLE = ("a wordmark/emblem for the business — its name spelled "
                  "out EXACTLY as given, in a style matching its industry")


# Film mode. Until 13 Nov 2026 a reel's pictures were six subjects on flat white,
# cut out and set on cards — which is what a slide deck looks like. A film needs
# whole pictures: full-frame vertical plates the scenes can sit on and move over,
# in the film's own art direction and palette, plus two cut-out subjects.
_SCENE_ROLES = (
    "SCENE PLATE 1 — the opening place or moment this film is about, wide and atmospheric",
    "SCENE PLATE 2 — a closer, more intimate view: hands, materials or a detail in use",
    "SCENE PLATE 3 — the feeling the film ends on: warm, resolved, a place to rest the eye",
)
_FILM_ROLES = _SCENE_ROLES + _ASSET_ROLES[:2]
_PLATE_RULES = (
    "A full-frame vertical picture with real depth — foreground, middle, background — "
    "lit with intent and filling the whole frame, not an object on a plain ground. "
    "Keep one calm, uncluttered area (a third of the frame, top or bottom) where type "
    "can sit. No text, letters, numbers or logos anywhere. If people appear, show them "
    "from behind or at a distance, never a close-up face.")


def _film_style(look: str, brand: dict | None) -> str:
    """The film's picture style: its art direction's, and its brand colours."""
    from . import looks as _looks
    style = _looks.image_style(look)
    accent = (brand or {}).get("accent")
    bits = []
    if style:
        bits.append(f"Picture style: {style}.")
    if accent:
        bits.append(f"Let {accent} be the dominant accent colour in the scene, with neutral tones around it.")
    return " ".join(bits)


def _prompt_from(prior_text) -> str:
    """The brief the stage before 'visual' wrote, turned into one image
    prompt. `prior_text` is either a single string or the earlier stages'
    outputs, newest first — the same shape every other local renderer in
    core.automation takes — so the first non-empty one is what this was
    asked to draw."""
    texts = [prior_text] if isinstance(prior_text, str) else list(prior_text or [])
    for t in texts:
        if t and str(t).strip():
            return str(t).strip()
    return ""


def _context_from(prior_text, limit: int = 2500) -> str:
    """Every earlier stage's own output, newest first, joined — not just
    the single most recent one.

    Found while answering the owner's own question (2026-11-08): the
    "artwork" stage usually runs AFTER both "research" (what the company
    actually does, their colours, their industry) and "content" (the
    script/caption) have already run — both already hold real information
    this was about to ask a text model to re-derive or, worse, silently
    lose. _prompt_from (used by the plain single-image 'visual' path,
    where the most recent stage's own text genuinely IS the literal
    brief) stops at the first non-empty entry on purpose; artwork's own
    brief-writer needs everything upstream actually found, not just
    whichever stage happened to run last."""
    texts = [prior_text] if isinstance(prior_text, str) else list(prior_text or [])
    seen, parts, total = set(), [], 0
    for t in texts:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        parts.append(t)
        total += len(t)
        if total >= limit:
            break
    return "\n\n".join(parts)[:limit]


def _own_logo(attachments) -> str:
    """The client's own logo, if one was attached — core.assets' own
    extraction, never a redraw. Returns a clean cut-out path, or "" when
    nothing attached looks like a mark (a photo is left alone; see
    core.assets' own docstring on why a photo is never treated as a logo)."""
    try:
        from . import assets as _assets
    except Exception:                                         # noqa: BLE001
        return ""
    for a in (attachments or []):
        path = a.get("path") if isinstance(a, dict) else str(a)
        if not path or not str(path).lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp")):
            continue
        try:
            prepared = _assets.prepare(path)
        except Exception:                                     # noqa: BLE001
            continue
        if prepared.get("alpha"):
            return prepared["path"]
    return ""


def _composite_logo(image_path: str, logo_path: str) -> str:
    """Paste the client's REAL logo onto the generated picture — never ask
    FLUX to draw one. core.assets' own docstring says why: "A model asked
    to 'make the logo without a background' redraws the logo, and a
    redrawn logo is the wrong logo." Placed bottom-right, a fixed
    proportion of the frame, the usual spot for a brand mark on a post."""
    from PIL import Image
    with Image.open(image_path).convert("RGBA") as base, \
            Image.open(logo_path).convert("RGBA") as logo:
        margin = round(base.width * 0.04)
        target_w = round(base.width * 0.22)
        scale = target_w / max(logo.width, 1)
        logo = logo.resize((target_w, max(1, round(logo.height * scale))),
                           Image.LANCZOS)
        pos = (base.width - logo.width - margin,
              base.height - logo.height - margin)
        base.alpha_composite(logo, dest=pos)
        base.convert("RGB").save(image_path, "PNG")
    return image_path


def _single_brief_prompt(query: str, context: str, skill_text: str = "") -> str:
    """What the text model is asked to turn ONE request (fresh, or a
    follow-up's "use this logo" and nothing else) into ONE complete
    picture prompt — the single-image sibling of _brief_meta_prompt.

    Rewritten (2026-11-08, the owner's own ask): the old version only
    asked for "a scene" — no typographic hierarchy, no instruction to
    actually USE a company's own brand colours/tone when a research stage
    already found them in `context`, and no reserved space for the real
    logo generate() pastes on top afterward. A finished social post is a
    LAYOUT, not just a subject — so this asks for one explicitly, the
    same way a real designer's brief would.

    Extended the same day, same ask, then corrected a step further: a
    real post can carry all kinds of extra information a phone number, a
    website, an offer, a brand tagline, OR SOMETHING ELSE ENTIRELY — a
    venue, a date, a hashtag, a price, a testimonial. Every client's ask
    is different, so this does not hand the model a fixed checklist to
    fill in; it asks the model to find whatever concrete pieces the
    REQUEST OR CONTEXT ACTUALLY CONTAIN and place each sensibly, with the
    common cases named only as worked examples of where things usually
    go, never as the only things allowed to appear. Same "never invent a
    fact you weren't given" rule as everywhere else here.

    `skill_text` (2026-11-08): house doctrine for a SPECIFIC kind of post
    — a particular festival, a product launch, a sale — when
    core.skills.py has one that matched this run's own request (see
    core.automation's "visual" stage dispatch). Typed in as its own
    section, ahead of the general design rules below: a named skill's
    doctrine is more specific to this exact job than the general rules
    are, the same priority core.skills.py's own typed-to-a-browser-agent
    intro line already asks for ("where it conflicts with generic
    advice it wins")."""
    today = _dt.date.today().strftime("%d %B %Y")
    return (
        f"Today's date is {today}. You are writing ONE picture prompt for "
        "a model that will never see this message, cannot search the web, "
        "and knows only what you put in its own prompt — nothing may be "
        "left for it to assume or look up.\n\n"
        f"THE REQUEST:\n{query}\n\n"
        + (f"ALREADY KNOWN (earlier context from this conversation — use "
           f"it, do not invent beyond it; if this is a follow-up, the "
           f"actual subject usually lives here, not in the request "
           f"itself):\n{context[:1500]}\n\n" if context.strip() else "") +
        (f"{skill_text.rstrip()}\n\n" if skill_text.strip() else "") +
        "If the request names a date, festival or occasion, write out "
        "what it actually is and its real visual motifs — colours, "
        "objects, mood — from your own knowledge. If it names a place or "
        "an industry, describe the visual character that actually "
        "implies. Never invent a specific real fact you were not given — "
        "but if the context above names the company's own brand colours, "
        "a visual style, or a tone of voice, you MUST build the prompt "
        "around THOSE, not a generic palette of your own choosing: a "
        "company-oriented post looks like that company's, not like any "
        "festive post.\n\n"
        "This is a finished social-media post, not a bare illustration — "
        "write it as a real design brief, covering every one of these in "
        "the one prompt you write:\n"
        "  • LAYOUT: one clear visual hierarchy — a short line for the "
        "business name near the top, ONE large, elegant greeting/headline "
        "as the dominant element, and one short supporting tagline "
        "beneath it in smaller text. Say which typeface FEEL fits the "
        "brand (e.g. elegant serif, clean modern sans). This main group "
        "(name + greeting + tagline) stays to two short lines below the "
        "name, so the words stay legible and correctly spelled.\n"
        "  • EVERYTHING ELSE THE CLIENT ACTUALLY GAVE: read the request "
        "and the context above for every OTHER concrete piece of "
        "information offered beyond the core greeting — this varies by "
        "client and by job, so look for whatever is really there rather "
        "than expecting a fixed list. Give each one its own small, "
        "clearly separate text element — never folded into the greeting "
        "— placed where that KIND of information usually reads best, and "
        "skip entirely anything not actually given (never invent one, "
        "never leave a placeholder for one that's missing):\n"
        "      – a brand tagline/slogan (the company's general motto, "
        "distinct from today's own occasion line) → directly under the "
        "business name, smaller and lighter than the name;\n"
        "      – a promotion, offer, discount or call-to-action → a "
        "small distinct badge or ribbon in the TOP-LEFT corner, clearly "
        "apart from the main greeting;\n"
        "      – contact-style details — a phone number, a website, an "
        "address, a date or venue for an event — → one small fine-print "
        "line along the BOTTOM-LEFT, never the bottom-right (reserved, "
        "see below);\n"
        "      – anything else concrete the client gave that doesn't fit "
        "those (a hashtag, a price, a short quote or testimonial, a "
        "named person, a deadline) → use the same judgement: its own "
        "small element, in whichever of the spots above reads most "
        "naturally for that kind of detail, never crowding the main "
        "greeting or the reserved corner.\n"
        "  • COMPOSITION: a calm, uncluttered background (one deep colour "
        "or a soft gradient, matching the brand/theme) with the festive "
        "or thematic motifs placed so they support the text rather than "
        "fight it — generous negative space, nothing crammed.\n"
        "  • RESERVED CORNER: explicitly say that the bottom-right corner "
        "must stay visually clear and uncluttered — a real logo is pasted "
        "there afterward, over whatever is drawn, so anything busy there "
        "gets covered or clashes.\n"
        "  • TONE: premium and professional, fitting a real business post, "
        "never cartoonish or cluttered like generic stock clip-art.\n\n"
        "Answer with ONE complete, self-contained image prompt and "
        "nothing else — no numbering, no headers, no commentary before "
        "or after."
    )


def _maker() -> str:
    """Who drew it, in words a customer should see: in server mode the picture
    comes from Prism's server, not from a named model."""
    from . import ai_client
    return "Prism AI" if ai_client.active() else "FLUX 3 Klein"


def _reel_turn(cfg: dict, kind: str) -> dict:
    """A Reel's picture briefs on Prism's server get the turn's output limit
    and effort. Any other picture is asked exactly as before."""
    if not (_ai.active() and _ai.current_film() == "reel"):
        return {}
    from . import server_session as _ss
    return _ss.turn_options(_ss.savings_of(cfg), kind)


def build_single_prompt(query: str, context: str, cfg: dict,
                        skill_text: str = "") -> str:
    """ONE complete FLUX prompt, written by Claude Haiku 5.5 from the
    request plus whatever earlier stages (or an earlier run, on a
    follow-up) already established. `skill_text`: house doctrine for a
    specific kind of post (see _single_brief_prompt's own docstring).
    Returns "" on any failure — the caller falls back to the raw request
    rather than guessing further — except an ai_client.AiError, which is
    raised so the caller can show the server's own reason."""
    api_key = (cfg.get("openrouter_api_key") or "").strip()
    if not api_key:
        return ""
    try:
        text = _openrouter.openrouter_chat(
            api_key, BRIEF_MODEL,
            _single_brief_prompt(query, context, skill_text),
            temperature=0.5, timeout=60,
            role="writer", addon="image", feature="brief",
            **_reel_turn(cfg, "brief"))
    except _ai.AiError:
        # A refusal from Prism's server (out of credits, AI paused, rate
        # limit, timeout) is not "no key": let the caller say what it was.
        raise
    except Exception:                                         # noqa: BLE001
        return ""
    return (text or "").strip()


def generate(query: str, prior_text, cfg: dict, attachments=None, *,
            aspect_ratio: str = "1:1", resolution: str = "1K",
            skill_text: str = "") -> tuple[str, str]:
    """Draw one image with FLUX 3 Klein. Returns (path, message) — path
    empty on failure, the same (out, note) shape _run_local's other
    renderers return, so a failure degrades the same way a scraped stage
    does rather than taking the run down with it.

    Reported live, 2026-11-08, TWO bugs in one sitting:

    1. Attached a real logo, asked for a post, and the logo FLUX drew
       into the picture did not match it — this function never looked at
       `attachments` at all, so the only "logo" FLUX could make was its
       own guess from text. The real one is now cut out (core.assets, the
       same tool a client's flat-card logo already goes through
       elsewhere) and pasted on top of whatever FLUX draws, never asked
       of FLUX itself.

    2. Filled in real content on a post, then followed up with "use this
       logo" — and the new picture was random, because the OLD version of
       this function only ever grabbed `prior_text`'s single most recent
       entry verbatim (here, "use this logo" itself, or this stage's own
       earlier terse note — never the actual subject). `query` is now the
       run's real ask (the follow-up's own text on a follow-up), and
       `_context_from(prior_text)` is EVERY earlier stage's output, not
       just the latest — the same fix already made for generate_artwork.
       Claude Haiku 5.5 turns the two into one coherent prompt first, the
       same reasoning pass FLUX itself cannot do, so a terse follow-up ask
       does not overwrite the post it was refining."""
    api_key = (cfg.get("openrouter_api_key") or "").strip()
    if not api_key:
        return "", ("FLUX 3 Klein needs an OpenRouter key — add one in "
                    "Settings → AI tools.")
    context = _context_from(prior_text)
    base = (query or "").strip() or _prompt_from(prior_text)
    if not base:
        return "", "Nothing was written for FLUX 3 Klein to draw from."
    try:
        prompt = build_single_prompt(base, context, cfg, skill_text) or base
    except _ai.AiError as e:
        # Out of credits / AI paused / rate-limited: say so, rather than
        # quietly drawing from the raw words and hiding the cause.
        return "", str(e)
    logo_path = _own_logo(attachments)
    if logo_path:
        prompt += (" Do not draw any logo, wordmark, brand mark, or "
                   "company name anywhere in the image — leave the "
                   "composition clean; a real logo is placed on top of "
                   "this afterward, not drawn by you.")
    try:
        data = _openrouter.openrouter_image(
            api_key, MODEL, prompt, aspect_ratio=aspect_ratio,
            resolution=resolution, addon="image")
    except Exception as e:                                  # noqa: BLE001
        return "", str(e)

    from . import config as _config
    os.makedirs(_config.RUNS_DIR, exist_ok=True)
    out = os.path.join(_config.RUNS_DIR, f"flux_{int(time.time())}.png")
    with open(out, "wb") as f:
        f.write(data)
    # The actual prompt, not just a filename — a later follow-up's own
    # "earlier result" reference (main_window._followup_prompt) reads
    # responses[stage] back; a bare filename told it nothing about what
    # the picture actually showed, which is bug #2 above in a nutshell.
    note = f"{_maker()} drew: {prompt[:280]}"
    if logo_path:
        try:
            _composite_logo(out, logo_path)
            note += " (your own logo placed on top)"
        except Exception as e:                               # noqa: BLE001
            note += f" (could not place your logo on it: {e})"
    return out, note


# ── the "artwork" stage: several images, each self-contained ────────────────
#
# FLUX cannot browse and knows nothing past its own training — asked for
# "a Dhanteras post for this company in Vadodara" with no other context, it
# has no way to find out what either of those actually means. A browser tool
# (ChatGPT, old default) could search the web itself, mid-conversation; a
# pure image API never can. So the step that used to be "one smart tool does
# research AND draws" splits in two here: a TEXT model (which, unlike the
# image model, may genuinely know what Dhanteras is, or can read whatever a
# real research stage already found earlier in this run) writes N complete,
# self-contained prompts FIRST — and only THEN does FLUX draw, from a prompt
# with nothing missing, never from a bare request it cannot complete itself.

def _brief_meta_prompt(query: str, context: str, n: int,
                       has_own_artwork: bool, film: str = "") -> str:
    """What the text model is asked, to turn one request into N prompts a
    picture-only model can draw with nothing else to go on."""
    today = _dt.date.today().strftime("%d %B %Y")
    if film:
        roles = list(_FILM_ROLES)
    else:
        roles = ([] if has_own_artwork else [_WORDMARK_ROLE]) + list(_ASSET_ROLES)
    roles = roles[:n]
    numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(roles, 1))
    return (
        f"Today's date is {today}. You are writing picture prompts for a "
        "model that will never see this message, cannot search the web, "
        "and knows only what you put in its own prompt — nothing may be "
        "left for it to assume or look up.\n\n"
        f"THE REQUEST:\n{query}\n\n"
        + (f"ALREADY KNOWN (earlier research or context from this run — "
           f"use it, do not invent beyond it):\n{context[:1500]}\n\n"
           if context.strip() else "") +
        f"Write exactly {len(roles)} separate prompts, one per line, "
        f"covering these roles in order:\n{numbered}\n\n"
        "If the request names a date, festival or occasion (e.g. "
        "Dhanteras, Diwali, a company anniversary), write out in the "
        "prompt itself what it actually is and its real visual motifs — "
        "colours, objects, mood — from your own knowledge, so the picture "
        "model never has to recognise the word itself. If it names a "
        "place or an industry, describe the visual character that "
        "actually implies. Never invent a specific real fact — an "
        "address, a person, a logo — that was not given to you above.\n\n"
        + (f"THE FILM'S LOOK — every prompt carries this: {film}\n"
           f"For the SCENE PLATES: {_PLATE_RULES}\n"
           "For the other prompts (cut-out subjects): ONE flat solid plain background colour "
           "and nothing else behind the subject, exactly one clear subject, no text.\n\n"
           if film else "") +
        ("" if film else
        "Every single prompt must ALSO say, verbatim in substance: ONE "
        "flat solid plain background colour and nothing else behind the "
        "subject — no scene, no gradient, no shadow, no texture; exactly "
        "one clear subject with generous empty space around it; no "
        "people, no faces; no on-image text or lettering (except prompt "
        "1, the wordmark, which may show the business name and nothing "
        "else).\n\n") +
        f"Answer with exactly {len(roles)} lines and nothing else — no "
        "numbering, no headers, no blank lines between them, no "
        "commentary before or after."
    )


def build_prompts(query: str, context: str, cfg: dict, *, n: int = 6,
                  has_own_artwork: bool = False, film: str = "") -> list[str]:
    """N complete, self-contained FLUX prompts, written by a real text
    model (Claude Haiku 5.5) that can reason about dates, festivals and
    whatever research already ran earlier in this pipeline — the research
    FLUX itself cannot do. Returns [] on any failure; the caller decides
    what that means rather than this function guessing."""
    api_key = (cfg.get("openrouter_api_key") or "").strip()
    if not api_key:
        return []
    try:
        text = _openrouter.openrouter_chat(
            api_key, BRIEF_MODEL,
            _brief_meta_prompt(query, context, n, has_own_artwork, film),
            temperature=0.5, timeout=60,
            role="writer", addon="image", feature="briefs",
            **_reel_turn(cfg, "briefs"))
    except _ai.AiError:
        raise       # the server's own reason, shown by generate_artwork
    except Exception:                                         # noqa: BLE001
        return []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[:n]


def generate_artwork(query: str, prior_text, cfg: dict, *,
                     has_own_artwork: bool = False,
                     n: int = 6, look: str = "", brand: dict | None = None,
                     film: bool = False) -> tuple[list[str], str]:
    """The "artwork" stage's own local path, end to end: write N
    self-contained prompts, draw each on a flat background, then cut that
    background out with Prism's own existing cutout tool (core.assets) —
    the same deliverable ChatGPT's artwork stage gave (several real
    transparent subject PNGs), reached without a browser.

    Returns ([paths], message) — paths empty only when nothing at all
    could be made; one failed asset among several does not sink the rest."""
    context = _context_from(prior_text)
    style = (_film_style(look, brand) or "Picture style: natural, considered.") if film else ""
    if film:
        n = len(_FILM_ROLES)
    try:
        prompts = build_prompts(query, context, cfg, n=n,
                                has_own_artwork=has_own_artwork, film=style)
    except _ai.AiError as e:
        return [], str(e)
    if not prompts:
        if not _ai.active() and not (cfg.get("openrouter_api_key") or "").strip():
            return [], ("FLUX 3 Klein needs an OpenRouter key to write the "
                        "image briefs — add one in Settings → AI tools.")
        return [], f"{_maker()} did not write any image briefs. Try again."
    api_key = (cfg.get("openrouter_api_key") or "").strip()

    from . import assets as _assets
    from . import config as _config
    os.makedirs(_config.RUNS_DIR, exist_ok=True)

    out_paths = []
    why = ""
    for i, prompt in enumerate(prompts, 1):
        plate = film and i <= len(_SCENE_ROLES)
        full = prompt if plate else (
            prompt + " Background: one flat solid plain white colour "
            "(#FFFFFF), absolutely nothing else behind the subject.")
        try:
            data = _openrouter.openrouter_image(
                api_key, MODEL, full, aspect_ratio="9:16" if plate else "4:5",
                resolution="1K", addon="image")
        except Exception as e:                                # noqa: BLE001
            # A refusal from Prism's server (out of credits, AI paused) is
            # worth saying; one picture failing among six is not.
            if isinstance(e, _ai.AiError) and not why:
                why = str(e)
            continue
        raw = os.path.join(_config.RUNS_DIR,
                           f"flux_{int(time.time())}_{i}.png")
        with open(raw, "wb") as f:
            f.write(data)
        # A scene plate is kept whole; only a cut-out subject has its ground removed.
        out_paths.append(raw if plate else _assets.prepare(raw, _config.RUNS_DIR)["path"])

    if not out_paths:
        return [], why or f"{_maker()} could not draw any of the images."
    return out_paths, f"{_maker()} drew {len(out_paths)} image(s)."
