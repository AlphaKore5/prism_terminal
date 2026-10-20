"""
Prism — Agent Registry & Category Map
─────────────────────────────────────
The single source of truth for every AI tool Prism can drive, and how the
six selectable categories map onto the sequential automation pipeline.

A tool lives ONCE in AGENT_REGISTRY. Its category is contextual — the same
entry (e.g. "Claude", "ChatGPT", "LAZYCOOK", "Kimi 2.6") is offered under
multiple categories because it is genuinely multi-domain. The stage it runs
under is decided by the user's selection + the router, not by the registry.
"""

# ── Pipeline order ────────────────────────────────────────────────────────────
# Stages run top-to-bottom. Each stage feeds its output forward as context to
# the next. A stage only runs if (a) the user assigned an agent to it AND
# (b) the router marks it "needed" for the current query.
PIPELINE_ORDER = [
    "research",       # ground the task in facts / papers / computation
    # Finding prospects is not research and must not have to fight it for the
    # same slot — exactly the reason audio was split out of media below. An
    # outreach run usually needs BOTH: research to work out which industries
    # to go after, THEN leads to pull the actual companies in them. Sharing
    # one stage forced a choice between a tool that reads the web and a tool
    # that reads a contact database, which are not substitutes.
    #
    # It sits AFTER research on purpose. Research narrows the segment; the
    # lead database then filters inside it. The other way round you are
    # picking companies before you know what you are looking for.
    "leads",          # real companies & verified contacts to approach
    "brains",         # strategy, reasoning, architecture
    "content",        # copy, docs, scripts
    # Narration must exist before the video is assembled. Keeping audio after
    # media produced a silent MP4 first and an orphaned ElevenLabs file later.
    # It also belongs before visual so it receives the script directly rather
    # than image-maker chatter as its handoff.
    "audio",          # voice-over, music, narration
    "visual",         # images
    "media",          # video
    "development",    # build & deploy apps, UIs, tools
    "presentation",   # slide decks & pitch presentations
    "summary",        # final synthesis (uses the 'brains' agent)
]

# ── Selectable categories (shown in onboarding & /agents) ─────────────────────
CATEGORIES = {
    "brains": {
        "label": "Orchestration & Brains",
        "emoji": "🧠",
        "color": "brains",
        "desc": "Strategy, factual grounding, deep reasoning & technical architecture",
        "agents": ["Perplexity", "ChatGPT", "Claude", "LAZYCOOK"],
    },
    "research": {
        "label": "Research & Academic",
        "emoji": "📚",
        "color": "research",
        "desc": "Peer-reviewed data, citations & symbolic mathematics",
        # Perplexity leads here: live web search with sources is the research
        # job most tasks actually need, and /boq's design-standards stage
        # picks whatever is set for this category.
        "agents": ["Perplexity", "Consensus", "WolframAlpha", "Semantic Scholar",
                   "NotebookLM", "LAZYCOOK"],
    },
    # Its own category, not a corner of research. A contact database and a
    # web-search model are different machines: one returns rows that were
    # verified, the other returns prose it composed. Making the user pick one
    # for both jobs meant an outreach run could have facts or prospects, never
    # both.
    "leads": {
        "label": "Leads & Prospecting",
        "emoji": "🎯",
        "color": "leads",
        "desc": "Real companies and named contacts with verified emails, to approach",
        # Apollo first and by a distance — it is the only one here that
        # returns checked contact records. The other two are the honest
        # fallback for someone with no Apollo account: they read the public
        # web, which finds fewer addresses and cannot confirm them.
        "agents": ["Apollo", "Perplexity", "LAZYCOOK", "ChatGPT"],
    },
    "content": {
        "label": "Content, Post & Docs",
        "emoji": "✍️",
        "color": "content",
        "desc": "Marketing copy, SEO, long-form & no-hallucination scripts",
        # Perplexity earns a place for the "no-hallucination" half of this
        # category — source-cited factual copy — not for creative marketing.
        "agents": ["Jasper", "Copy.ai", "Kimi 2.6", "Writesonic", "Claude",
                   "ChatGPT", "Perplexity", "LAZYCOOK"],
    },
    "visual": {
        "label": "Visual & Image",
        "emoji": "🎨",
        "color": "visual",
        "desc": "Professional image generation, editable posts & consistent characters",
        # Canva is first because most business visual work is a post or a
        # brochure the customer will want to tweak later, and it is the only
        # tool here that hands back something still editable.
        # Google Gemini sits second, not last: alternatives_for() offers the
        # first TWO untried tools in this order when one fails, so parked at
        # the end it would never actually be reached. This is what makes it
        # the real fallback for ChatGPT -- the tool that has hit its own
        # free-tier image limit twice in one afternoon on this account.
        "agents": ["Canva", "Google Gemini", "Leonardo.ai", "Adobe Firefly",
                   "Midjourney", "ChatGPT"],
    },
    "media": {
        "label": "Video & Reels",
        "emoji": "🎬",
        "color": "media",
        "desc": "Generated footage, reels from your own clips & AI avatars",
        "agents": ["Prism Studio", "Prism Reel", "Prism Motion", "Google Flow",
                   "Claude Design", "InVideo AI", "Runway", "Pika Labs", "HeyGen",
                   "NotebookLM"],
    },
    # Split out from video on purpose: a voice-over and a reel are different
    # jobs with different tools, and a run often needs BOTH — one stage each,
    # rather than forcing a single pick between ElevenLabs and Runway.
    "audio": {
        "label": "Voice & Audio",
        "emoji": "🔊",
        "color": "audio",
        "desc": "Voice-over, narration, music & audio explainers",
        "agents": ["ElevenLabs", "Suno", "NotebookLM"],
    },
    "development": {
        "label": "Web, App & Tools",
        "emoji": "🛠️",
        "color": "development",
        "desc": "Deployed apps, generative UI & tools straight from prompts",
        "agents": ["omma.build", "emergent.sh", "v0.dev", "Claude", "Kimi 2.6"],
    },
    "presentation": {
        "label": "Presentations & Decks",
        "emoji": "📊",
        "color": "presentation",
        "desc": "Slide decks, pitch presentations & narrative sites from a prompt",
        "agents": ["Gamma.app", "Canva", "Tome", "Claude", "Claude Design"],
    },
}


# The "summary" stage is not user-selectable; it reuses whichever agent the
# user picked for "brains" (falling back to content / research).
SUMMARY_FALLBACK_ORDER = ["brains", "content", "research"]

# ── Sensible generic web-UI selectors for tools we don't hand-tune ────────────
_GENERIC = {
    "textarea_selector": "textarea, div[contenteditable='true'][role='textbox'], div[contenteditable='true']",
    "response_selector": "[data-message-author-role='assistant'], [data-markdown-text-style='assistant-message'], .response, .message, .prose, .markdown",
    "submit_selector": "button[type='submit'], button[aria-label*='Send'], button[data-testid='send-button']",
}


# ── makers: tools whose deliverable is a THING, not text ─────────────────────
# A chat model answers with words, and Prism's stage prompts are written for
# that: a deliverable spec, a text format, "do not create files". Handed to a
# tool that BUILDS -- Canva, Gamma, Runway -- those same words are a wrong
# instruction, and the tool obeys them: the 2026-09-10 run asked Canva for
# "the final deck in plain-text slide format ready for import into Gamma.app
# ... do NOT generate actual PPT files", and Canva typed the outline back.
#
# `makes` says what the tool produces. The router names it when it writes
# the stage prompt (router._maker_rule), and the engine opens the stage with
# a brief that says "build it here" and sets aside any text-format ask below
# (automation._maker_brief). Empty for a chat tool.
_MAKES = {
    "Canva": "an editable Canva design — a presentation, a social post or a "
             "brochure — built in the customer's own Canva account",
    "Gamma.app": "a finished Gamma presentation, built in Gamma",
    "Tome": "a finished Tome presentation, built in Tome",
    "Leonardo.ai": "generated images",
    "Adobe Firefly": "generated images",
    "Midjourney": "generated images",
    "Google Gemini": "generated images",
    "Runway": "a generated video",
    "Pika Labs": "a generated video",
    "Google Flow": "a generated video",
    "NotebookLM": "a Video Overview (or an Audio Overview) rendered in "
                  "NotebookLM from the sources it is given",
    "InVideo AI": "a finished promo video cut from the uploaded footage",
    "HeyGen": "an AI-avatar video",
    "ElevenLabs": "a voice-over audio file",
    "Suno": "a music track",
    "omma.build": "a working, deployed app",
    "emergent.sh": "a working, deployed app",
    "v0.dev": "a working UI, deployed",
    "Claude Design": "a designed artifact",
}


def is_maker(cfg: dict) -> bool:
    return bool((cfg or {}).get("makes"))


# How generous the caps are. wait_time is a CEILING, not a sleep: _smart_wait
# returns the moment the answer settles, so a bigger number costs a fast tool
# nothing and only buys headroom for a slow one. The old floor of 120s was
# routinely hit by reasoning models and by any tool that renders a document,
# and hitting it means the run walks away from an answer that was seconds out.
WAIT_FLOOR = 300          # 5 min for even the quickest tool
WAIT_MULTIPLIER = 2.0     # slow tools scale from their registered estimate
WAIT_CEILING = 1800       # 30 min — past this something is genuinely stuck


# Appended verbatim to ChatGPT's visual/presentation prompts. Two jobs: make
# it actually reach for the connected Canva app, and make it hand back the
# share LINK — an editable design nobody can open is worth less than a flat
# image. The "if you cannot" clause matters: without it, a ChatGPT with no
# Canva app connected spends its reply explaining that it can't, instead of
# producing the picture that was asked for.
# Asked for an editable design — but the picture is still made properly first.
#
# This used to say "build it in Canva RATHER THAN a flat image", and the
# comment below has always admitted what that costs: Canva composes a template
# where DALL-E renders the scene that was described. So the customer had to
# choose between a good picture and an editable one.
#
# They do not. Generate the image at full quality here, and a SECOND prompt in
# the same chat (_CANVA_FOLLOWUP, sent by automation._make_editable) hands the
# finished artwork to the Canva app to be made editable. Best picture and an
# editable file, because the two steps stopped competing for one prompt.
_CANVA_SUFFIX = (
    "DELIVERY FORMAT — generate the image yourself, directly, at the highest "
    "quality you can. Do NOT route this through Canva or any other design "
    "tool yet, even if one is connected: a template-built layout is not what "
    "is being asked for. Return the generated image in your reply. I will ask "
    "you to make it editable in a follow-up message once I have seen it."
)

# The second half, sent into the SAME conversation once the artwork exists.
#
# Addressed to the Canva app with "@canva" because that is how ChatGPT routes
# a message to a connected app, and by this point the design it should act on
# is the image directly above it in the thread.
_CANVA_FOLLOWUP = (
    "@canva can you make this design editable — import the image above into a "
    "new Canva design in my account, at the same size, keeping the artwork as "
    "the background so I can edit the text and layout on top of it.\n\n"
    "Then reply with the Canva design URL on its own line, prefixed exactly "
    "with 'CANVA LINK: '. The link is the deliverable — without it I cannot "
    "open what you made. If the Canva app is not connected to this account, "
    "do not explain that and do not ask me to connect it: reply with only "
    "'CANVA LINK: none' and nothing else."
)

# The other half of the switch, and the reason it has to be said out loud.
#
# Letting Canva COMPOSE an image costs picture quality. It builds a template —
# stock layouts, its own type, its own arrangement — where DALL·E renders the
# scene that was actually described. For "da Vinci sipping Wagh Bakri chai"
# that is the whole job, and a template cannot do it.
#
# Which is why the editable path no longer asks Canva to compose anything: it
# generates first and converts second. The two suffixes are now identical in
# what they ask for, and differ only in what happens next — kept as separate
# constants because they answer different questions, and collapsing them would
# make the next person think the distinction had been dropped.
#
# Left to itself, a ChatGPT with the Canva app connected reaches for it far
# too readily — so when the user has not asked for an editable design, saying
# nothing is not enough. It has to be told not to.
_NO_CANVA_SUFFIX = (
    "DELIVERY FORMAT — generate the image yourself, directly, at the highest "
    "quality you can. Do NOT route this through the Canva app or any other "
    "design tool, even if one is connected to this account: a template-built "
    "layout is not what is being asked for here. Return the generated image "
    "in your reply."
)

# What counts as "I want this in Canva". Matched against the user's own words,
# never against the router's rewrite of them — the point is that the CUSTOMER
# asked, and the router paraphrasing a brief must not be able to opt them in.
CANVA_TRIGGERS = (
    "canva", "editable", "editable design", "edit it later", "edit later",
    "so i can edit", "so we can edit", "so they can edit", "client can edit",
    "template", "edit afterwards",
)


def wants_canva(text: str) -> bool:
    """Did the user actually ask for an editable design?

    Deliberately a plain substring check on the user's own task text. The
    alternative — letting the model decide — is what produced the complaint
    this exists to fix: every post came back as a Canva template because
    ChatGPT will always take that route if it is offered one.
    """
    lowered = (text or "").lower()
    return any(trigger in lowered for trigger in CANVA_TRIGGERS)


# Handed to the stage BEFORE Apollo, in place of the usual prose handoff.
#
# Apollo is not a chat box. It is a filter screen backed by an API that
# rejects any single field longer than 200 characters — feeding it the normal
# pipeline brief produced exactly that:
#     Value too long: 'Context from the previous pipeline stage (RESEA…'
#
# So the previous stage is told, literally, that its reader is a database and
# what shape the answer has to be. Fixed field names, one per line, because
# _run_apollo() parses them back out and turns them into Apollo's own search
# URL — free prose here means no filters get set at all.
_APOLLO_HANDOFF = (
    "\n\nSTRICT PIPELINE RULES — YOUR READER IS A DATABASE, NOT A CHATBOT:\n"
    "1. Perform ONLY the task above. Do not produce anything else.\n"
    "2. Your answer is passed to Apollo.io, a B2B contact database. Apollo "
    "does not read paragraphs — it takes search FILTERS, and it rejects any "
    "single value longer than 200 characters. Prose is discarded.\n"
    "3. Do NOT try to name individual companies or people. Finding those is "
    "Apollo's job, and a guessed company name returns nothing. Your job is "
    "to decide WHAT KIND of company and WHICH job titles to search for.\n"
    "4. End your answer with EXACTLY this block, as the last thing you write "
    "— one field per line, plain comma-separated values, no bullets, no "
    "bold, no commentary, nothing after it:\n\n"
    "HANDOFF FOR APOLLO\n"
    "TITLES: 2-6 job titles of the person worth reaching, e.g. Founder, CEO, "
    "Managing Director, Head of Operations\n"
    "INDUSTRIES: 2-6 industry keywords describing the company, e.g. "
    "manufacturing, industrial automation\n"
    "LOCATIONS: cities, states or countries, e.g. Gujarat, Maharashtra, India\n"
    "HEADCOUNT: one or more of 1-10, 11-20, 21-50, 51-100, 101-200, 201-500, "
    "501-1000, 1001-2000, 2001-5000, 5001-10000, 10001+\n"
    "KEYWORDS: up to 12 plain words further describing the company\n\n"
    "5. Every one of those lines must be under 150 characters. Write 'any' "
    "for a field you genuinely cannot narrow — never leave one out.\n"
    "6. Company SIZE words like 'small cap' and 'mid cap' describe listed "
    "shares and mean nothing to Apollo. Translate them into the HEADCOUNT "
    "ranges above."
)


# ── the plain name of each step ──────────────────────────────────────────────
#
# What a person sees a step called: in the chat header ("Prism · <job> ·
# Write it up"), in the line above an earlier step's hand-off, and in the
# floor prompt for a step nobody wrote one for. CATEGORIES has the working
# labels ("Orchestration & Brains"); these are the words.
STEP_NAMES = {
    "brains": "Think it through", "research": "Look things up",
    "leads": "Find the people", "content": "Write it up",
    "visual": "Make the images", "media": "Make the video",
    "audio": "Record the voice", "development": "Build the tool",
    "presentation": "Build the slides", "design": "Design the reel",
    "artwork": "Make the artwork", "script": "Write the script",
    "analysis": "Read the files", "summary": "Sum it up",
    "motion_plan": "Plan the motion", "format": "Format it",
}


# ── how each tool thinks ─────────────────────────────────────────────────────
#
# Every tool used to get the same message. That is wrong in both directions:
# Claude puts a long answer in a side panel and will build a real .docx if
# asked to; ChatGPT will route an image through Canva given half a chance and
# answers shorter than it could; Perplexity runs one pass and forgets the
# conversation. None of that is visible in a CSS selector, and all of it
# changes what a good prompt looks like.
#
# So a tool carries a PROFILE, and Prism composes against it:
#
#   produces    what this tool can actually hand back (core/contract.py holds
#               a step to `text` rather than failing it for a file the tool
#               was never going to produce)
#   file_hint   one clause added to "give me the file" for this tool
#   avoid       one short line appended when this tool's known drift matters
#               for the kind of step being run
#
# Deliberately data, not code, and deliberately small — three fields that
# change the message, not a second personality sheet for the model to read.
# The licence payload can replace any of them (licensing/payload.py), so
# "Claude moved its download button" or "ask Gemini for files like this now"
# is a row in the admin console and reaches every customer on their next
# check-in, instead of a release.
_PROFILES: dict[str, dict] = {
    "ChatGPT": {
        "produces": ("text", "image", "file", "data"),
        "file_hint": "Use your own tools to build it and give me the "
                     "download link in this chat.",
        "avoid": "Do not put this in a canvas — answer in the chat itself. "
                 "Do not generate images or call image tools unless this specific step explicitly asks for images.",
    },
    "Claude": {
        "produces": ("text", "file", "data"),
        "file_hint": "Build it as a real document with your own tools, then "
                     "leave the download in the chat.",
        "avoid": "Write it in the chat, not in a side panel or an artifact — "
                 "Prism reads the conversation, not the panel.",
    },
    "Claude Design": {
        "produces": ("text", "file", "image"),
        "file_hint": "Build the design here and leave the export in the chat.",
        "avoid": "Do not generate images or call image tools unless this specific step explicitly asks for images.",
    },
    "Perplexity": {
        "produces": ("text",),
        "file_hint": "",
        # Its answers are short and it does not carry a brief between turns,
        # so everything it needs has to be in the one message.
        "avoid": "",
    },
    "LAZYCOOK": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Grok": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "DeepSeek": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Gemini": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Kimi 2.6": {
        "produces": ("text", "file", "data"),
        "file_hint": "",
        "avoid": "Answer in English.",
    },
    "Canva": {
        "produces": ("image", "file"),
        "file_hint": "Build it in Canva; the design itself is what I need.",
        "avoid": "",
    },
    "Gamma.app": {
        "produces": ("file",),
        "file_hint": "Build the deck in Gamma; the deck itself is what I need.",
        "avoid": "",
    },
    "Tome": {"produces": ("file",), "file_hint": "", "avoid": ""},
    "Midjourney": {"produces": ("image",), "file_hint": "", "avoid": ""},
    "Leonardo.ai": {"produces": ("image",), "file_hint": "", "avoid": ""},
    "Adobe Firefly": {"produces": ("image",), "file_hint": "", "avoid": ""},
    "Google Gemini": {"produces": ("image",), "file_hint": "", "avoid": ""},
    "Runway": {"produces": ("video",), "file_hint": "", "avoid": ""},
    "Pika Labs": {"produces": ("video",), "file_hint": "", "avoid": ""},
    "Google Flow": {"produces": ("video",), "file_hint": "", "avoid": ""},
    "InVideo AI": {"produces": ("video",), "file_hint": "", "avoid": ""},
    "HeyGen": {"produces": ("video",), "file_hint": "", "avoid": ""},
    "ElevenLabs": {"produces": ("file",), "file_hint": "", "avoid": ""},
    "Suno": {"produces": ("file",), "file_hint": "", "avoid": ""},
    "NotebookLM": {"produces": ("text", "file", "video"), "file_hint": "",
                   "avoid": ""},
    "Apollo": {"produces": ("links",), "file_hint": "", "avoid": ""},
    "omma.build": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "emergent.sh": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "v0.dev": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Jasper": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Copy.ai": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Writesonic": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Consensus": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "WolframAlpha": {"produces": ("text",), "file_hint": "", "avoid": ""},
    "Semantic Scholar": {"produces": ("text",), "file_hint": "", "avoid": ""},
}


import copy as _copy  # noqa: E402  (a pristine copy for apply_profiles to reset to)

#: The profiles this build ships with, untouched by any payload.
_BUILTIN_PROFILES: dict[str, dict] = _copy.deepcopy(_PROFILES)


def profile_for(name: str) -> dict:
    """What is known about how `name` likes to be asked. {} when nothing is.

    Read through resolve_agent() in normal use; exposed on its own for
    diagnostics and for the plan screen, which says what a step will produce
    before it runs.
    """
    return dict(_PROFILES.get(name) or {})


def apply_profiles(mapping: dict[str, dict] | None) -> int:
    """Replace the published tool profiles. Returns how many tools have one.

    Same shape and the same reasoning as apply_overrides(): the payload is
    the whole intended state, so a tool dropped from it goes back to its
    built-in profile rather than keeping one nobody can see. Rubbish is
    ignored — a bad publish must never be able to stop Prism asking.
    """
    known = ("produces", "file_hint", "avoid")
    # Reset first, as apply_overrides() does. Merging on top of whatever was
    # applied last meant an unpublished profile — an empty payload, or a
    # tool dropped from it — stayed in force until Prism was restarted,
    # which is the opposite of what this docstring promises.
    _PROFILES.clear()
    _PROFILES.update(_copy.deepcopy(_BUILTIN_PROFILES))
    for name, prof in (mapping or {}).items():
        if not isinstance(name, str) or not isinstance(prof, dict):
            continue
        clean = {}
        for key in known:
            value = prof.get(key)
            if key == "produces" and isinstance(value, (list, tuple)):
                picked = tuple(str(v) for v in value if isinstance(v, str))
                if picked:
                    clean[key] = picked
            elif key != "produces" and isinstance(value, str):
                clean[key] = value
        if clean:
            _PROFILES[name] = {**(_PROFILES.get(name) or {}), **clean}
    return len(_PROFILES)


def _agent(url, specialty, cost, avg, wait, **overrides):
    base = {
        "url": url,
        "specialty": specialty,
        "cost": cost,
        "avg": avg,
        "wait_time": int(min(max(wait * WAIT_MULTIPLIER, WAIT_FLOOR), WAIT_CEILING)),
        **_GENERIC,
    }
    base.update(overrides)
    return base


# ── The full registry ─────────────────────────────────────────────────────────
# No count in this heading on purpose: it went stale every time a tool landed.
AGENT_REGISTRY = {
    # ── Orchestration / Brains ────────────────────────────────────────────────
    "Perplexity": _agent(
        "https://www.perplexity.ai",
        "real-time factual grounding, source verification, citations, current events",
        "Freemium", "5–15s", 120,
        textarea_selector="div[contenteditable='true']#ask-input, textarea",
        response_selector=".prose, .break-words",
        submit_selector="button[aria-label='Submit']",
    ),
    "ChatGPT": _agent(
        "https://chatgpt.com",
        "general intelligence, multimodal reasoning, brainstorming & DALL·E 3 "
        "visuals — a rendered, illustrated image of whatever scene is "
        "described, which is what most posts and creatives need. It can also "
        "build an EDITABLE Canva design instead, but ONLY mention that when "
        "the user's own words ask to edit or reuse the file afterwards; "
        "routing an ordinary image request through Canva returns a stock "
        "template and loses the picture they asked for",
        "Freemium", "5–20s", 120,
        # The busiest tool in the registry, on the thinnest settle time of
        # any of them (_GENERIC has none; every other agent that sets its
        # own is 8-14s). A "New chat" click swaps in a fresh composer that
        # is present in the DOM before React has finished wiring it up --
        # typing lands (raw DOM text, which is all _text_landed can check),
        # the click or Enter that follows does nothing because the
        # framework's OWN state never saw it, and the run reports "would
        # not accept the prompt" on a page that looks, to the eye, ready.
        page_wait=10,
        # A chain: ChatGPT dropped the old id="prompt-textarea" (Oct 2026) for
        # a bare ProseMirror contenteditable labelled "Ask ChatGPT", which made
        # every run time out on "waiting for the message box". Keep the id for
        # buckets that still have it; the rest are the editor's own markers.
        textarea_selector=(
            "#prompt-textarea, "
            "form div.ProseMirror[contenteditable='true'], "
            "div[contenteditable='true'][aria-label='Ask ChatGPT']"),
        # A CHAIN, not one selector. OpenAI rolls the conversation DOM out in
        # buckets, so two customers on the same day can see different markup:
        # the long-standing div[data-message-author-role] and a newer
        # li[data-message-role] form. A single matcher means _capture() finds
        # nothing, and because _looks_like_signin() gives up on bodies over
        # 4000 chars, a real conversation page never reaches the marker check —
        # so the customer is told they are signed out and sent round the login
        # loop instead of being told the page changed. This was the narrowest
        # matcher in the file, on the default tool: narrower than the generic
        # fallback at DEFAULTS that an unconfigured agent gets. A selector list
        # is an OR, so extra alternatives cannot break a bucket that still
        # matches the old form.
        response_selector=(
            "[data-message-author-role='assistant'], "
            "[data-message-role='assistant'], "
            "[data-markdown-text-style='assistant-message'], "
            ".markdown.prose"),
        submit_selector="button[data-testid='send-button']",
        # A switch, not an instruction. Which way it falls is decided by the
        # user's own wording at run time — see wants_canva() and the resolver
        # in automation.py.
        #
        # It was unconditional at first, and that was wrong: every post came
        # back as a flat Canva template, including the ones that needed a
        # rendered illustration. Both branches are spelled out because
        # silence is not neutral here — a ChatGPT with the Canva app
        # connected will reach for it unless told otherwise.
        stage_suffix={
            "visual": {"when": CANVA_TRIGGERS,
                       "asked": _CANVA_SUFFIX, "otherwise": _NO_CANVA_SUFFIX},
            "presentation": {"when": CANVA_TRIGGERS,
                             "asked": _CANVA_SUFFIX,
                             "otherwise": _NO_CANVA_SUFFIX},
        },
        # An empty, finished reply -- a new assistant turn with no text and
        # no Stop button -- ends the wait early instead of running out the
        # cap, and is regenerated once (automation._smart_wait,
        # _regenerate_once). Read off the live page on 2026-09-10: the turn
        # is a <section data-turn="assistant">; the per-message toolbar's
        # regenerate control is hover-only but present in the DOM.
        busy_selector="button[data-testid='stop-button']",
        turn_selector=("section[data-turn='assistant'], "
                       "[data-content-search-unit-key$=':assistant']"),
        regenerate_selector=("button[aria-label='Regenerate'], "
                             "button[aria-label*='Try again' i], "
                             "button[aria-label*='Retry' i], "
                             "button[data-testid*='regenerate' i]"),
    ),
    "Claude": _agent(
        "https://claude.ai",
        "advanced coding, complex documentation, UI artifacts & long-form reasoning",
        "Freemium", "10–30s", 300,
        # See ChatGPT's page_wait comment above -- same fix, same reasoning.
        # Claude's textarea_selector is also the broadest, most generic one
        # in the registry (any contenteditable div), which a page is more
        # likely to satisfy with something present-but-not-yet-live sooner
        # after navigation than a narrower selector would.
        page_wait=10,
        textarea_selector="div[contenteditable='true']",
        # .font-claude-response is the newer class; the old one is kept ahead
        # of it so nothing changes for a customer still served the old markup.
        response_selector=(".font-claude-message, .font-claude-response, "
                           ".prose, [data-is-streaming='false']"),
        submit_selector="button[aria-label='Send Message']",
    ),
    # Read live on 20 Oct 2026 by attaching Claude in Chrome to the owner's
    # own signed-in grok.com (same pattern as Google Gemini's note below):
    # asked "What is 2+2?" then a 300-word bridge-engineering question to
    # catch the busy state mid-stream. The composer is a tiptap/ProseMirror
    # contenteditable, same framework ChatGPT uses, but grok.com tags its
    # own turns and controls with data-testid rather than leaning on classes
    # or ARIA alone — [data-testid="assistant-message"] held the answer text
    # ONLY, the per-turn toolbar (Copy/Like/Dislike/Regenerate) sits beside
    # it as a sibling, not nested inside, so response_selector never picks
    # up a button's own label text. First use: a one-time, non-dismissible
    # age-confirmation modal blocked the very first send on this account —
    # not part of the chat DOM itself, so no selector here handles it; it
    # does not reappear once answered.
    "Grok": _agent(
        "https://grok.com",
        "general-purpose reasoning and drafting with real-time awareness "
        "via X (Twitter); a fast, free-tier-friendly alternative when "
        "ChatGPT or Claude are rate-limited or unavailable",
        "Freemium", "5–15s", 120,
        textarea_selector="div[contenteditable='true'][aria-label='Ask Grok anything']",
        response_selector="[data-testid='assistant-message']",
        submit_selector="button[data-testid='chat-submit']",
        busy_selector="button[aria-label='Stop model response']",
        regenerate_selector="button[aria-label='Regenerate']",
    ),
    # Read live on 20 Oct 2026, same session and method as Grok above, once
    # the owner had signed into chat.deepseek.com themselves (an account
    # login is one thing this assistant will never do on someone's behalf,
    # so Grok's research had to stop at the sign-in wall until this). No
    # data-testid attributes anywhere on this app, unlike grok.com — its own
    # design-system class names (ds-button, ds-markdown, …) are the stable
    # thing instead, so those are what the three selectors below key off.
    # Two things this did NOT come away with, stated rather than guessed at:
    #   - busy_selector: the one circular send button toggles state, but
    #     NOT in any way distinguishable by class or by its SVG content —
    #     it reads ds-button--disabled whether the box is simply empty OR a
    #     reply is actively streaming (checked repeatedly, incl. with "Deep
    #     thinking" on for a long reasoning answer to widen the window).
    #     Left unset; _GENERIC has no default for it either, so this falls
    #     back to the engine's own text-stopped-growing polling, same as
    #     Perplexity, Kimi 2.6 and most of this file already do.
    #   - regenerate_selector: no distinct regenerate affordance was found
    #     near a finished answer in the time spent looking. Left unset.
    # A second, DIFFERENT element also matches ".ds-button--circle" once a
    # conversation is tall enough to need one — a floating "scroll to the
    # newest message" control that sits ABOVE the composer, not inside it.
    # submit_selector excludes it explicitly (:not(.ds-button--floating)):
    # without that, a long conversation would start clicking a scroll
    # button instead of ever sending the next message.
    "DeepSeek": _agent(
        "https://chat.deepseek.com",
        "a large, free-tier-friendly reasoning model (optional 'Deep "
        "thinking' mode) with its own web search ('Smart Search'); a "
        "capable alternative when ChatGPT or Claude are unavailable",
        "Freemium", "10–30s", 180,
        textarea_selector="textarea[placeholder='Message DeepSeek']",
        response_selector=".ds-markdown",
        submit_selector="div.ds-button--circle:not(.ds-button--floating)",
    ),
    # A SEPARATE entry from "Google Gemini" above on purpose, the same way
    # "Claude" and "Claude Design" are two entries for one company's two
    # different jobs. "Google Gemini" is permanently tagged in _MAKES as an
    # image maker ("Google Gemini": "generated images") — that tag is
    # applied to AGENT_REGISTRY["Google Gemini"] by name, unconditionally,
    # at the bottom of this file (the "for _name, _what in _MAKES.items()"
    # loop), so every stage that entry is EVER assigned to gets an
    # image-generation brief, not a "write this document" one. Reusing it
    # for a BOQ write-up would have asked the writer stage to draw a
    # picture of a bill of quantities. This entry points at the exact same
    # product (gemini.google.com) for the TEXT side of it instead, with no
    # "makes" tag, so a stage assigned here gets an ordinary text brief.
    #
    # Read live on 20 Oct 2026, same session/method as Grok and DeepSeek
    # above: "Write a short 3-sentence summary of a bill of quantities for
    # a brick wall measuring 12 meters", then a 300-word bridge-engineering
    # question to catch the busy state. Gemini's composer is a Quill.js
    # editor (class="ql-editor", the same library many unrelated sites
    # use) inside a <rich-textarea> custom element — the aria-label is what
    # actually identifies it, not the Quill class, which is generic.
    # Answers render inside Gemini's own <message-content> custom element,
    # confirmed to hold the answer text ONLY (not the Copy/Edit/Good
    # response toolbar below it). No regenerate_selector: a "Show more
    # options" menu might hold one, but opening and reading a dropdown
    # wasn't tried this pass -- left unset rather than guessed, same
    # reasoning as DeepSeek's gaps above.
    "Gemini": _agent(
        "https://gemini.google.com/app",
        "Google's general-purpose assistant for text and reasoning (not "
        "the image generator — see 'Google Gemini' for that); broad web "
        "knowledge and a large context window, useful when ChatGPT or "
        "Claude are unavailable or rate-limited",
        "Freemium", "10–20s", 120,
        textarea_selector="div[contenteditable='true'][aria-label='Enter a prompt for Gemini']",
        response_selector="message-content",
        submit_selector="button[aria-label='Send message']",
        busy_selector="button[aria-label='Stop response']",
    ),
    # LAZYCOOK runs its own Generate → Analyze → Optimize → Validate loop and
    # does its own web scraping. That loop is the entire reason to route work
    # here rather than to Perplexity — and Prism's standard pipeline rules
    # switch it off. "Perform ONLY the task above, nothing more" reads to it as
    # "skip the analyse and optimise passes", so it answers in one shot and
    # comes back weaker than the tool it was chosen over.
    #
    # Asked the way a person would ask, it goes and does the work. Hence
    # prompt_style — see _resolve_handoff() in automation.py.
    "LAZYCOOK": _agent(
        "https://thelazycook.in",
        "4-stage automation (Generate → Analyze → Optimize → Validate) with "
        "its own live web scraping and no-hallucination checking. Give it a "
        "SUBJECT and let it work — it researches, cross-checks and refines by "
        "itself, and over-specifying the steps makes it worse, not better",
        "Free/Low", "20–45s", 400,
        prompt_style="natural",
    ),

    # ── Research & Academic ───────────────────────────────────────────────────
    # Apollo is a filter-and-table app, not a chat box: there is no assistant
    # bubble to scrape, so the response selector targets the results grid and
    # the stage after it is expected to turn rows into a recipients CSV.
    #
    # It is driven by URL rather than by typing, which is what `search_tool`
    # switches on (see automation._run_apollo). Apollo encodes every filter
    # into its own address bar, so setting them there sets exactly the same
    # state as clicking through the left rail — and needs no selector for the
    # filter widgets, which are the part of the page most likely to churn.
    # The selectors below are still used, but only for the results grid and
    # for the typed-search fallback when the previous stage gave us nothing
    # parseable.
    #
    # Apollo does have ONE place that takes prose: the "Use Apollo AI to find
    # the right prospects" box on an empty People page. Text entered there is
    # handed to Apollo's AI Assistant, which turns it into filters and applies
    # them to the table (verified with Playwright, Sep 2026: a 74-character
    # prompt became Titles + Company keywords + Location and 8 rows, and the
    # box kept a 4,000-character paste with no maxlength). It is NOT the
    # toolbar "Search people" box beside the filter button — that one is a
    # plain keyword search and matches nobody when given a sentence, which is
    # where the fallback used to type. Each prompt spends one assistant chat
    # (the free plan shows "N CHATS LEFT" in the panel), so the URL route
    # stays first whenever the previous stage handed over a filter block.
    "Apollo": _agent(
        "https://app.apollo.io/#/people",
        "B2B lead database — real companies and named decision-makers with "
        "VERIFIED work email addresses, filtered by industry, job title, "
        "headcount, technology and location. Use this for any 'find "
        "prospects / leads / companies to email' task: it returns contact "
        "data that was checked, instead of a language model's best guess at "
        "an address. Needs the user's own logged-in Apollo account",
        "Freemium", "20–40s", 300,
        page_wait=14,
        input_wait=45,
        search_tool="apollo",
        # The whole point of the handoff spec: keep every value Apollo is
        # given comfortably inside the limit its API enforces.
        max_query_chars=180,
        handoff_spec=_APOLLO_HANDOFF,
        # The prose box. Attribute-based on purpose: Apollo's zp_* class
        # names are hashed per deploy, the role and the rotating
        # "Example: …" placeholder are not. It only renders while no search
        # is on screen; the "Reset filters" button (or the bare People
        # route) brings it back. NOT "Search with AI" — that button opens a
        # chat about the current search and spends a chat doing it.
        ai_prompt_selector="input[role='combobox'][placeholder^='Example:']",
        ai_prompt_reset="Reset filters",
        # Measured capacity, not a guess — see the comment above the entry.
        ai_prompt_max_chars=4000,
        # The assistant "brews" for 30–60s before the filters land.
        ai_prompt_wait=150,
        # Keyword box — last resort only, and only ever given a few words.
        textarea_selector=("input[data-element='finder-toolbar-search-input'], "
                           "input[placeholder*='Search people'], "
                           "input[type='search'], input[type='text']"),
        response_selector=("[data-cy='people-table'], [role='table'], table, "
                           "[data-cy-loaded='true'], .zp_tFLCQ"),
        submit_selector=("button[type='submit'], button[aria-label*='Search'], "
                         "button[aria-label*='Apply']"),
    ),
    "Consensus": _agent(
        "https://consensus.app/search",
        "evidence-based answers extracted from 200M+ peer-reviewed research papers",
        "Freemium", "10–20s", 45,
    ),
    "WolframAlpha": _agent(
        "https://www.wolframalpha.com",
        "computational knowledge for physics, chemistry, and hard mathematics",
        "Free/Paid", "2–5s", 25,
        textarea_selector="input[type='text'], textarea",
        response_selector=".output, section, img",
        submit_selector="button[type='submit'], input[type='submit']",
    ),
    "Semantic Scholar": _agent(
        "https://www.semanticscholar.org",
        "AI-driven literature mapping and academic discovery",
        "Free", "5–15s", 35,
        textarea_selector="input[type='search'], input[name='q'], textarea",
    ),
    "NotebookLM": _agent(
        # notebooklm.google.com redirects here now; the page calls itself
        # "Gemini Notebook" (read live, 13 Sep 2026 — see automation.py's
        # NotebookLM notes).
        "https://notebook.google.com",
        "grounding AI in your own uploaded documents for faithful synthesis; "
        "handles LARGE volumes of source material (many long docs/videos/notes "
        "at once) and turns them into explainer output via its built-in Video "
        "Overview (a long-form Explainer or a Short) and Audio Overview "
        "(podcast-style) generators — the best fit for 'explain everything "
        "we have' and 'make a video/podcast from all this' requests",
        "Free", "10–20s", 45,
        textarea_selector="textarea[aria-label='Query box']",
        # No <input type=file> anywhere on the page: its "Upload files" is
        # the OS picker. "" tells _upload_files not to look; the runner
        # pastes every readable attachment in as a source instead.
        upload_selector="",
        # A Video Overview is a multi-minute render.
        generate_wait=1500,
    ),

    # ── Content, Post & Documentation ─────────────────────────────────────────
    "Jasper": _agent(
        "https://app.jasper.ai",
        "enterprise-grade marketing copy and consistent brand voice",
        "Paid/Trial", "5–15s", 35,
    ),
    "Copy.ai": _agent(
        "https://app.copy.ai",
        "high-conversion sales copy and go-to-market assets",
        "Freemium", "5–10s", 30,
    ),
    "Kimi 2.6": _agent(
        "https://www.kimi.ai",
        "massive context window for analysing 100+ page documents; multilingual",
        "Free/Low", "15–40s", 500,
        textarea_selector="div[contenteditable='true'], textarea",
        response_selector=".chat-message, .markdown",
    ),
    "Writesonic": _agent(
        "https://app.writesonic.com",
        "SEO-optimised long-form articles and landing-page copy",
        "Freemium", "10–20s", 45,
    ),

    # ── Visual & Image ────────────────────────────────────────────────────────
    "Leonardo.ai": _agent(
        "https://app.leonardo.ai",
        "fine-tuned model control and consistent character generation",
        "Freemium", "15–30s", 70,
    ),
    "Adobe Firefly": _agent(
        "https://firefly.adobe.com",
        "commercial-safe imagery with advanced generative fill",
        "Freemium", "10–20s", 50,
    ),
    "Midjourney": _agent(
        "https://www.midjourney.com/imagine",
        "the gold standard for cinematic photorealism (web alpha)",
        "Paid", "30–60s", 100,
    ),
    "Google Gemini": _agent(
        # Read live on 15 Sep 2026 by attaching Playwright to Prism's own
        # signed-in Chrome (the pattern this file's NotebookLM notes use):
        # a plain chat prompt -- "Generate an image: a stainless steel shoe
        # rack product photo, industrial style, teal accent lighting,
        # vertical 9:16 composition" -- produced a clean 572x1024 image in
        # about 15 seconds, with a working "Download full size image"
        # control once it settled. No custom selectors needed: the
        # composer is a plain contenteditable div the generic textarea
        # selector already matches, and the image is a plain large <img>
        # the generic harvest (_harvest_images) already finds by shape.
        "https://gemini.google.com/app",
        "Google's Nano Banana model (Gemini 2.5 Flash Image), free and "
        "fast — a clean product or scene photo in seconds, generated "
        "straight in the chat",
        "Free", "10–20s", 45,
        # The 15 Sep check above only ever tried a TEXT prompt — no
        # reference photos — which is exactly why the attachment path
        # went unverified: a real run with 6 reference images came back
        # "Google Gemini has no file-upload field on this page" on every
        # attempt. Confirmed live end-to-end (22 Sep 2026, driving the
        # real page directly): a freshly loaded page has ZERO
        # <input type='file'> elements anywhere on it — not hidden, not
        # late, none — until the button beside the composer, aria-label
        # "Upload & tools", is clicked. Clicking it alone is enough: it
        # mounts a hidden <input class="hidden-file-input" type="file">
        # (no need to also click the "Upload files" item in the menu it
        # opens, which is the one path that hands off to the native OS
        # file picker Selenium cannot drive), and assigning a file to it
        # directly produced a real attachment chip and enabled the send
        # button — despite the input's own `accept` list being document
        # types only (.pdf, .docx, …) with no image extension in it at
        # all; that attribute is evidently just a picker-dialog hint here,
        # not an enforced filter, so it does not need matching.
        upload_trigger_selectors=(
            "button[aria-label='Upload & tools']",
            "button[aria-label*='Upload & tools' i]",
        ),
    ),
    # Sits in BOTH visual and presentation: the same Magic Studio prompt makes
    # a post or a deck, and the reason to pick it is the same either way.
    #
    # What makes Canva different from every other tool in this registry: the
    # result is not something Prism scrapes out and hands over, it is a real
    # editable design left in the customer's own Canva account. So the LINK is
    # the deliverable, not the text — a run that captures no prose here has
    # still succeeded, and CompletionDialog already treats a URL-only step
    # that way.
    "Canva": _agent(
        # Canva AI, the chat -- not the /magic-design/ marketing page, which
        # has no composer at all (the "prompt would not go into Canva's
        # message box" run of 2026-09-10). Driven by automation._run_canva:
        # promo dialog dismissed, prompt into the one labelled textarea,
        # Submit, then the outline's "Generate design" pressed for the deck.
        "https://www.canva.com/ai",
        "social posts, brochures, decks and brand assets that stay EDITABLE — "
        "the design lands in the customer's own Canva account as a real file, "
        "so a price, caption, photo or logo can be changed afterwards without "
        "re-running anything. Choose this over a flat image generator whenever "
        "the client will want to tweak the result themselves, or needs it in "
        "their own brand kit. Output is the Canva design link",
        "Freemium", "30–60s", 400,
        page_wait=12,
        input_wait=45,
        runner="canva",
        textarea_selector="textarea[aria-label]",
        submit_selector="button[aria-label='Submit']",
        # Canva's class names are generated; the reply is ordinary
        # paragraphs, and _capture keeps the long ones and drops our echo.
        response_selector="main p, p",
    ),

    # ── Video & Audio ─────────────────────────────────────────────────────────
    "Runway": _agent(
        "https://app.runwayml.com",
        "high-end cinematic video generation and motion control (Gen-3)",
        "Freemium", "1–3 min", 240,
    ),
    "InVideo AI": _agent(
        "https://ai.invideo.io",
        "prompt-driven assembly of REAL uploaded footage into finished promo "
        "reels — branded intro/outro, captions, music, animated overlays, "
        "platform-specific cuts (Instagram/LinkedIn/pitch); unlike Runway it "
        "edits your actual clips together instead of generating new ones",
        "Freemium", "2–5 min", 300,
    ),
    "Pika Labs": _agent(
        "https://pika.art",
        "stylised animation and precise regional video editing",
        "Freemium", "1–2 min", 180,
    ),
    "HeyGen": _agent(
        "https://app.heygen.com",
        "AI avatars for professional pitch videos and presentations",
        "Freemium", "5–15 min", 360,
    ),
    "ElevenLabs": _agent(
        "https://elevenlabs.io/app/speech-synthesis",
        "industry-leading emotive voice cloning and text-to-speech",
        "Freemium", "5–15s", 45,
        runner="elevenlabs",
        textarea_selector=("textarea, [contenteditable='true'][role='textbox'], "
                           "[contenteditable='true']"),
    ),
    # Runs INSIDE Prism — no browser, no account, no upload. Marked local so
    # automation.py renders it here instead of driving a Chrome tab.
    "Prism Studio": _agent(
        "local://reel_web",
        "the reel is DESIGNED, not templated: one pass writes the script, a "
        "second art-directs it — its own background, typography, palette and "
        "motion as real CSS — and Prism films the page frame by frame. Two "
        "clients never get the same-looking film. No watermark, no credits",
        "Included", "40–90s", 60,
        local="reel_web",
    ),
    "Prism Reel": _agent(
        "local://reel",
        "the fixed house style, drawn in code: always 9:16, text always "
        "legible, brand colours measured from the client's artwork. Faster "
        "and never surprising, but every reel shares one look",
        "Included", "15–30s", 60,
        local="reel",
    ),
    "Prism Motion": _agent(
        "local://motion",
        "a real scene graph and camera, not a filmed web page: charts, "
        "diagrams, UI mockups and a real camera move, drawn frame by frame. "
        "One turn per scene, same as Studio. Best for explaining something —"
        " an architecture, a process, a number — rather than a brand story",
        "Included", "40–90s", 60,
        local="motion",
    ),
    "Google Flow": _agent(
        "https://labs.google/fx/tools/flow",
        "Veo-powered video on a free daily credit allowance — the cheapest way "
        "to make short reels. CANNOT render readable on-screen text, so any "
        "caption, price or phone number must be added afterwards, never asked "
        "for in the prompt",
        "Free tier", "1–3 min", 300,
    ),
    "Suno": _agent(
        "https://suno.com/create",
        "full-scale music and jingle generation for content",
        "Freemium", "30–60s", 100,
    ),

    # ── Web, App & Presentation Development ────────────────────────────────────
    "omma.build": _agent(
        "https://omma.build",
        "rapid full-stack application generation from prompts",
        "Freemium", "30–90s", 600,
        response_selector=".output, .code, .message",
    ),
    "emergent.sh": _agent(
        "https://emergent.sh",
        "agentic deployment of code into live environments",
        "Freemium", "1–3 min", 1200,
        response_selector=".message, .response",
    ),
    "v0.dev": _agent(
        "https://v0.dev",
        "generative UI components using Tailwind and shadcn/ui",
        "Freemium", "10–30s", 120,
    ),
    "Gamma.app": _agent(
        "https://gamma.app/create/generate",
        "one-click transformation of text into professional decks / sites",
        "Freemium", "20–40s", 400,
        # Gamma is a slow-loading SPA — give it extra time before hunting
        # for the prompt box, and accept plain <input> fields too.
        page_wait=12,
        input_wait=40,
        textarea_selector="textarea, div[contenteditable='true'], input[type='text']",
    ),
    "Tome": _agent(
        "https://tome.app",
        "AI-driven storytelling for pitch decks and narratives",
        "Freemium", "15–30s", 70,
    ),
    "Claude Design": _agent(
        "https://claude.ai/design",
        "Claude's design surface — slide decks, video edit designs & polished visual assets",
        "Freemium", "1–3 min", 300,
        # Same claude.ai frontend as the chat — reuse its hand-tuned selectors,
        # with extra load time since the design surface renders a canvas.
        page_wait=8,
        textarea_selector="div[contenteditable='true']",
        # .font-claude-response is the newer class; the old one is kept ahead
        # of it so nothing changes for a customer still served the old markup.
        response_selector=(".font-claude-message, .font-claude-response, "
                           ".prose, [data-is-streaming='false']"),
        submit_selector="button[aria-label='Send Message']",
    ),
}


# Selector overrides delivered by the licence server, applied on top of the
# registry at lookup time. Empty in a fresh process and populated by
# licensing.payload once a verified payload has been read.
#
# Held apart from AGENT_REGISTRY rather than merged into it, on purpose: the
# built-in values must stay reachable and unmodified, so that clearing the
# overrides restores the shipped behaviour exactly. A payload that turns out to
# be wrong is then one publish away from being undone, and a customer who never
# reaches the server is never worse off than the build they installed.
_OVERRIDES: dict[str, dict] = {}


def apply_overrides(mapping: dict[str, dict] | None) -> int:
    """Replace the override set. Returns how many agents are now overridden.

    Replace, not merge: the payload is the whole intended state, so an agent
    dropped from it must revert to its built-in values rather than keep an
    override nobody can see any more.
    """
    _OVERRIDES.clear()
    for name, over in (mapping or {}).items():
        if isinstance(name, str) and isinstance(over, dict) and over:
            _OVERRIDES[name] = dict(over)
    return len(_OVERRIDES)


def overrides() -> dict[str, dict]:
    """What is currently overridden — for diagnostics and the self-test."""
    return {name: dict(over) for name, over in _OVERRIDES.items()}


def resolve_agent(stage: str, name: str) -> dict | None:
    """Return the registry entry for an agent (name is category-independent).

    The one place every stage passes through, which is why server-published
    overrides are applied here rather than at each call site.
    """
    if not name:
        return None
    entry = AGENT_REGISTRY.get(name)
    if entry is None:
        return None
    over = _OVERRIDES.get(name)
    prof = _PROFILES.get(name)
    if not over and not prof:
        return entry
    # A copy, so neither an override nor a profile mutates the shipped
    # registry. The profile rides along here rather than being looked up at
    # each call site, so everything that asks for a tool — the contract, the
    # prompt composer, the plan screen — sees the same answer about what
    # that tool can do (see _PROFILES).
    return {**entry, **(prof or {}), **(over or {})}


def alternatives_for(stage: str, tried: list | tuple = (),
                     cfg: dict | None = None, limit: int = 2,
                     query: str = "") -> list[str]:
    """Other tools that could do this stage, best first.

    Used when a tool cannot finish — most often a free tier running out
    part-way through a long run. The alternative has to be able to do the same
    JOB, so candidates come from the stage's own category rather than from
    whatever else the user happens to have configured: swapping a failed image
    stage onto a research tool would produce an answer, and the answer would be
    an essay about pictures.

    Ordering, and the reasoning for it:

      1. **Tools the user already picked somewhere.** They chose them, which
         almost always means they are signed in — and a signed-out alternative
         fails exactly as fast as the tool it is replacing.
      2. **The rest of the category, registry order.** That order is roughly
         "most capable first" and is a reasonable second guess.

    Local agents are excluded. They are Prism's own renderers, not web tools,
    and a stage that failed in a browser is not fixed by handing it to one.

    Canva is excluded from "visual"/"presentation" unless the user's own
    words actually asked for something editable (wants_canva) — the same
    signal that already keeps the PRIMARY tool from routing an ordinary
    image request through Canva (NO_CANVA_INSTRUCTION), just never
    reused here before. A real run asked for a photorealistic product
    composite from reference photos; ChatGPT hit its free-tier limit,
    and the fallback — going by catalogue order alone, "Canva first
    because most business visual work is a post or a brochure" — handed
    the job to a template builder that cannot use reference photos at
    all and was never going to produce what was asked for, on the very
    request this signal exists to keep away from Canva in the first
    place.
    """
    seen = {t for t in tried if t}
    if stage in ("visual", "presentation") and not wants_canva(query):
        seen.add("Canva")
    picked = set((cfg or {}).get("agents", {}).values())
    catalogue = CATEGORIES.get(stage, {}).get("agents", [])

    preferred, rest = [], []
    for name in catalogue:
        if name in seen:
            continue
        entry = AGENT_REGISTRY.get(name)
        if not entry or entry.get("local"):
            continue
        (preferred if name in picked else rest).append(name)
    return (preferred + rest)[:limit]


def summary_agent_name(agents: dict) -> str | None:
    """Which agent should run the final 'summary' stage, given user selections."""
    for cat in SUMMARY_FALLBACK_ORDER:
        if agents.get(cat):
            return agents[cat]
    return None


def specialty_for(stage: str, name: str) -> str:
    cfg = resolve_agent(stage, name)
    return cfg["specialty"] if cfg else "general-purpose AI"


# Applied last: the registry has to exist before a field can be written on it.
for _name, _what in _MAKES.items():
    if _name in AGENT_REGISTRY:
        AGENT_REGISTRY[_name]["makes"] = _what
del _name, _what
