#!/usr/bin/env python3
"""
Prism — terminal
────────────────
One prompt → many specialist AIs, in sequence. A modern, Claude-Code-style REPL
that replaces the old Google-Drive watcher. Type a task; Groq splits it across
the agents you chose; Prism drives your logged-in Chrome to run each one and
hands the output forward.

    python3 prism.py            # interactive REPL
    python3 prism.py "task…"    # run one task and exit
    python3 prism.py --dry "…"  # show the routing plan only
    python3 prism.py --config   # re-run setup
"""
import json
import os
import shutil
import time

try:
    # Not decoration. Without readline, input() reads the terminal through
    # C stdio, which slurps a WHOLE BUFFER: line one of a pasted command
    # comes back, line two hides inside libc where select()-based draining
    # cannot see it — and leaks out at the next Y/n prompt, where the first
    # stray 'n' answers No. Watched happen twice on real /step-ask runs.
    # With readline active, input() reads exactly one line from the tty
    # itself; whatever remains stays IN the tty buffer, where the drains
    # already catch it. Arrow-key editing and history are the side bonus.
    import readline  # noqa: F401
except ImportError:                                     # pragma: no cover
    pass
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as C          # noqa: E402
from core import ui                   # noqa: E402
from core import router               # noqa: E402
from core import agents as A          # noqa: E402
from core import onboarding           # noqa: E402


# ── query execution ───────────────────────────────────────────────────────────

def run_query(cfg: dict, query: str, dry: bool, attachments: list | None = None,
              confirm: bool = True) -> None:
    if not query.strip():
        ui.warn("Empty task — nothing to route.")
        return
    attachments = attachments or []
    ui.rule("Routing", "teal")
    if attachments:
        ui.info(f"📎  {len(attachments)} file(s) attached to this task.")
    ui.info("🧠  asking Groq to split your task…")
    routing = None
    for attempt in (1, 2):
        try:
            routing = router.route(query, cfg, attachments)
            break
        except json.JSONDecodeError:
            # Raw parser noise ("Expecting ',' delimiter: line 7 column 13")
            # is not an answer a person can act on. A garbled reply from a
            # language model is usually a one-off — ask once more, and if it
            # repeats say what to actually do about it.
            if attempt == 1:
                ui.warn("Groq's plan came back garbled — asking once more…")
                continue
            ui.err("Groq couldn't produce a readable plan for this task. "
                   "Try rewording it in one sentence, or run the matching "
                   "command directly (/help lists them).")
            return
        except Exception as e:
            ui.err(str(e))
            return
    if routing is None:
        return

    ui.routing_plan(routing, C.active_agents(cfg))
    _show_prompt_upgrade(query, routing, C.active_agents(cfg))
    if attachments:
        ui.info("📎  plus an ANALYSIS stage first (not shown above): ChatGPT reads "
                "the attached file(s) and briefs the pipeline.")

    if dry:
        ui.info("\nDry run — no browser opened. This is exactly what would execute.")
        C.save_run({"query": query, "dry": True, "routing": routing,
                    "attachments": [a["name"] for a in attachments]})
        return

    run_agents = _apply_named_tools(routing, C.active_agents(cfg))
    run_agents = _offer_agent_alternatives(routing, run_agents)

    # confirm before touching the browser (skipped for remote prompts — the
    # sender isn't at the keyboard to answer)
    if confirm:
        from core.onboarding import _ask_confirm
        _flush_stdin_noise()
        if not _ask_confirm("\nRun this plan against your logged-in browser now?", default=True):
            ui.info("Cancelled. (Tip: prefix with '/dry ' to preview without running.)")
            return

    try:
        from core import automation
    except Exception as e:
        ui.err(f"Automation deps not available ({e}). Install requirements.txt.")
        return

    cfg_for_run = cfg
    if run_agents != C.active_agents(cfg):
        cfg_for_run = dict(cfg)
        cfg_for_run["agents"] = run_agents

    try:
        responses, links = automation.run(routing, cfg_for_run, attachments=attachments, query=query)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        # Driver setup (Chrome missing, driver download, unsupported OS) can
        # raise before the per-stage error handling — never kill the REPL.
        ui.err(f"run failed before completing: {e}")
        return

    ui.rule("Results", "pink")
    if links:
        for stage, url in links.items():
            resp = responses.get(stage) or []
            ui.say(f"[bold]{stage.upper()}[/bold]  →  {url}")
            if resp:
                ui.info(f"   {resp[0][:280]}…")
    else:
        ui.warn("No agent produced output. Are you logged in to each tool in Chrome?")

    path = C.save_run({"query": query, "routing": routing,
                       "responses": responses, "links": links,
                       "attachments": [a["name"] for a in attachments]})
    ui.ok(f"Run saved → {path}")


def _esc(text: str) -> str:
    """Escape rich markup in untrusted text (LLM output, transcripts, paths) —
    a stray '[anything]' would otherwise crash the styled display."""
    try:
        from rich.markup import escape
        return escape(text)
    except Exception:
        return text


def _show_prompt_upgrade(query: str, routing: dict, agents: dict):
    """Show the full transformation chain — raw words → task brief → the
    engineered prompt each AI actually receives — so the difference between
    what the user gave and what Prism built from it is always visible."""
    from core import agents as A
    stages = [(s, d) for s in A.PIPELINE_ORDER
              for d in [routing.get(s)]
              if d and d.get("needed") and d.get("questions")]
    if not stages:
        return
    parts = [f'[bold]1 · You said:[/bold]  [dim]"{_esc(query.strip())}"[/dim]\n']
    brief = (routing.get("_brief") or "").strip()
    if brief:
        parts.append(f"[bold]2 · Prism expanded it into this task brief:[/bold]\n{_esc(brief)}\n")
    step = 3 if brief else 2
    parts.append(f"[bold]{step} · …and engineered each AI's prompt from it:[/bold]\n")
    for stage, data in stages:
        agent = agents.get(stage) or A.summary_agent_name(agents) or ""
        for q in data["questions"]:
            parts.append(f"[bold]{stage.upper()}[/bold] [dim]({_esc(agent)}) gets:[/dim]\n{_esc(q)}\n")
    ui.panel("\n".join(parts).strip(), title="your words → engineered prompts", style="teal")


def _apply_named_tools(routing: dict, agents: dict) -> dict:
    """The user can directly ORDER a specific tool ("using NotebookLM…",
    "notebook lm video generation") — router.detect_named_tools() catches
    this deterministically, no LLM judgement call needed. Force it in for any
    stage that's actually running this task; no guessing, no picker, just a
    one-line notice. Returns the agent mapping to use for THIS run — the
    user's saved defaults (/agents) are never touched."""
    named = routing.get("_named_tools") or {}
    if not named:
        return agents
    run_agents = dict(agents)
    for stage, tool in named.items():
        data = routing.get(stage) or {}
        if not (data.get("needed") and data.get("questions")):
            continue   # this stage isn't even running this task
        if run_agents.get(stage) == tool:
            continue   # already the default — nothing to announce
        run_agents[stage] = tool
        ui.info(f"🗣️  you asked for [bold]{_esc(tool)}[/bold] — using it for "
                f"{stage.upper()} this run.")
    return run_agents


def _offer_agent_alternatives(routing: dict, agents: dict) -> dict:
    """Only for stages where the router found a clearly better-suited tool:
    show that category's FULL tool list as a numbered table — your current
    pick marked ●, the router's suggestion starred ★ — and let you switch to
    ANY of them, or skip (Enter). Stages with no suggestion stay silent, no
    prompt at all. The user's saved defaults (/agents) are never touched —
    this only affects the run about to execute."""
    seen = {}
    for s in routing.get("_suggestions") or []:
        stage = s.get("stage")
        if stage and s.get("suggested"):
            seen[stage] = s   # last one wins if the router somehow doubled up
    if not seen:
        return agents
    run_agents = dict(agents)
    for stage, s in seen.items():
        current, suggested, reason = s["current"], s["suggested"], s.get("reason", "")
        if run_agents.get(stage) != current:
            continue   # default already differs from what this suggestion assumed — skip
        names = A.CATEGORIES.get(stage, {}).get("agents", [])
        if not names:
            continue
        ui.agent_pick_table(stage, current, suggested)
        ui.info(f"   why {_esc(suggested)}: {_esc(reason)}")
        ans = _prompt(f"   keep {current}? [Enter]  or type a # to switch (this task only) ").strip()
        if not ans:
            continue
        if ans.isdigit() and 1 <= int(ans) <= len(names):
            pick = names[int(ans) - 1]
            if pick != current:
                run_agents[stage] = pick
                ui.ok(f"   {stage} → {pick} for this run.")
        else:
            ui.warn("   not a valid number — keeping your default.")
    return run_agents


# ── slash commands ────────────────────────────────────────────────────────────

HELP = """
[bold]Commands[/bold]
  [teal]/help[/teal]        show this help
  [teal]/status[/teal]      current profile, key & agents
  [teal]/catalog[/teal]     the full tool catalogue
  [teal]/agents[/teal]      re-pick one agent per category
  [teal]/profile[/teal]     change what-you-do
  [teal]/key[/teal]         change your Groq API key
  [teal]/chrome[/teal]      set/edit your pinned Chrome version (or auto-detect)
  [teal]/login[/teal]       re-open your tools in Chrome to sign in
  [teal]/config[/teal]      re-run the whole setup wizard
  [teal]/attach <path…>[/teal]  attach file(s) of any type to your next task
  [teal]/find <description>[/teal]  locate & attach a file described in plain words
                ("the brochure in the prism folder of python program in documents")
  [teal]/files[/teal]       list currently attached files
  [teal]/detach[/teal]      clear all attached files
  [teal]/runs[/teal]        list saved runs
  [teal]/dry <task>[/teal]  preview routing without opening a browser
  [teal]/remote[/teal]      host a local website that sends prompts to this terminal
  [teal]/remote <code>[/teal]  pair a 4-digit code & listen (local bridge or relay)
  [teal]/remote url <link>[/teal]  set a hosted relay → pair with anyone over the internet
  [teal]/remote stop[/teal] shut the local bridge down
  [teal]/email setup[/teal]  configure your sending account (SMTP, one time)
  [teal]/email <goal>[/teal]  draft an email from attached files & send it — recipients
                from an attached CSV, addresses written in the prompt, or (if
                none given) a research agent finds who matches, e.g. "find the
                best-suited agencies in Vadodara and email them" — you always
                see who was found and get a customizable preview before send
  [teal]/boq <path> <context>[/teal]  Bill of Quantities from a .dwg/.dxf drawing —
                lengths/areas/counts are MEASURED from the real geometry (no
                AI ever touches the drawing), saved as an auditable CSV, then
                an agent formats them into a professional BOQ document —
                /attach a template alongside the drawing to match its exact
                columns/structure instead of a generic layout
  [teal]/step [metal|plastic] <file.step>[/teal]  measure a 3D CAD model:
                every part's size, wall/sheet thickness, every hole, volume
                and weight — from the real geometry, offline. Writes an Excel
                dimension sheet and a DIMENSIONED DRAWING SHEET — front, top
                and side views of every part with the sizes in mm, a hole
                table, notes and a title block — drawn by Prism itself, no
                AI. Every file named after the model, in a folder per model.
                No AI ever sees the STEP file. /s works too.
  [teal]/step-folder [path|default][/teal]  where those folders go
                (~/Desktop/Prism Step unless you say otherwise).
  [teal]/step-auto [metal|plastic] <file.step>[/teal]  the same offline
                measurement and sheet, then ChatGPT ALSO draws a
                styled version FROM THE MEASURED NUMBERS — the STEP file
                still never leaves this machine. Optional: Prism's own sheet
                already carries every dimension.
  [teal]/step-ask [metal|plastic] <file.step> <question>[/teal]  ask about
                the part: Groq suggests from the measured figures, an agent
                reviews them into an exact change plan, and Prism applies
                the plan to a COPY of the model locally and re-measures it.
                The STEP file never leaves this machine at any stage.
  [teal]/gerber <folder|zip|rar> [what to write][/teal]  read a PCB job and
                measure what a fab asks for — board size, min track width, min
                track spacing, min drill, hole count — from the real geometry.
                No AI ever sees a Gerber; the design never leaves this machine.
                Cross-checked against the job's own CAM report where one
                exists, and saved as an auditable CSV. Add an instruction to
                have an agent write the reply FROM THE NUMBERS ONLY
  [teal]/reel <what it's about>[/teal]  a finished vertical reel, rendered on this
                machine — no account, no upload, no watermark. /attach the
                client's logo or artwork and their real colours are measured
                from it. Needs FFmpeg; Prism Studio (which art-directs the
                reel instead of using the house template) also needs
                `playwright install chromium`
  [teal]/skills[/teal]      the house doctrine Prism applies to each kind of job, and
                where to add your own
  [teal]/exit[/teal]        quit

Anything else you type is treated as a task and routed to your agents.
Attached files ride along with your next task (and stay until you /detach).
"""


def cmd_status(cfg):
    ui.status_header(cfg, C.active_agents(cfg))
    premium = cfg.get("premium") or []
    if premium:
        ui.info(f"⭐  premium plans: {', '.join(premium)} (routed the bulk of the work)")


def cmd_agents(cfg):
    cfg["agents"] = onboarding.collect_agents(cfg.get("agents", {}))
    cfg["premium"] = onboarding.collect_premium(cfg["agents"], cfg.get("premium"))
    C.save(cfg)
    ui.ok("Agents updated.")
    cmd_status(cfg)


def cmd_profile(cfg):
    cfg["profile"] = onboarding.collect_profile(cfg.get("profile", ""))
    C.save(cfg)
    ui.ok("Profile updated.")


def cmd_key(cfg):
    cfg["api_key"] = onboarding.collect_key(cfg.get("api_key", ""))
    C.save(cfg)
    ui.ok("Key updated.")


def cmd_chrome(cfg):
    cfg["chrome_version"] = onboarding.collect_chrome(cfg.get("chrome_version", ""))
    C.save(cfg)
    pinned = cfg["chrome_version"]
    ui.ok(f"Chrome version set to {'v' + pinned if pinned else 'auto-detect'}.")


def cmd_login(cfg):
    onboarding.login_step(C.active_agents(cfg))


def cmd_config(cfg):
    new = onboarding.run(cfg)
    fr = new.pop("_firstrun", "skip")
    C.save(new)
    _handle_firstrun(new, fr)
    return new


def _attach_folder_interactive(dirpath: str, attachments: list) -> None:
    """Folder flow (typed, described or spoken): ALWAYS show what's inside,
    then let the user pick specific files or take everything."""
    from core import files as F
    listing = sorted(
        os.path.join(dirpath, f) for f in os.listdir(dirpath)
        if not f.startswith(".") and os.path.isfile(os.path.join(dirpath, f)))
    if not listing:
        ui.warn(f"Folder {_esc(dirpath)} has no attachable files.")
        return
    ui.say(f"  Files in {_esc(os.path.basename(dirpath) or dirpath)}:")
    for i, f in enumerate(listing, 1):
        ui.say(f"   {i}. {_esc(os.path.basename(f))}")
    ui.warn(f"Prism attaches at most {F.MAX_DIR_FILES} files per folder — and on "
            "ChatGPT's FREE plan only a handful of uploads per day get through, "
            "so fewer, well-chosen files give better results on free tiers.")
    ans = _prompt("attach which? (Enter/'a' = all, or numbers like 1 3 5) ").strip().lower()
    if ans in ("", "a", "all", "y", "yes"):
        chosen = listing[:F.MAX_DIR_FILES]
    else:
        nums = [int(t) for t in ans.replace(",", " ").split() if t.isdigit()]
        chosen = [listing[n - 1] for n in nums if 1 <= n <= len(listing)]
        if not chosen:
            ui.info("cancelled.")
            return
        chosen = chosen[:F.MAX_DIR_FILES]
    added = []
    for fp in chosen:
        try:
            added.append(F.attach(fp))
        except Exception as e:
            ui.err(f"Could not attach {os.path.basename(fp)}: {e}")
    attachments.extend(added)
    if added:
        ui.ok(f"Attached {len(added)} file(s): " + ", ".join(_esc(a["name"]) for a in added))


def cmd_find(arg, cfg, attachments):
    """Resolve a spoken/casual description of a file's location and attach it."""
    from core import pathfinder as PF, files as F
    if not arg:
        ui.warn('Usage: /find <where the file is, in your own words>\n'
                'e.g.  /find the brochure pdf in the prism ai flow folder '
                'of python program in documents')
        return
    ui.info("🔎  interpreting the description…")
    res = PF.find(arg, cfg)
    hints = _esc(" → ".join(res["folders"]) or "?")
    files = res["files"]
    if not files and res["dir"]:
        # Folder located, no specific filename — confirm it's the RIGHT
        # folder before listing contents (fuzzy matching can pick a
        # same-named sibling, or mishear a spoken folder name entirely).
        ui.ok(f"Folder found: {_esc(res['dir'])}   (heard: {hints})")
        ans = _prompt("Is this the right folder? (Y/n) ").strip().lower()
        if ans in ("n", "no"):
            corrected = _prompt("type the correct folder name/path: ").strip()
            if not corrected:
                ui.info("cancelled.")
                return
            cp = os.path.abspath(os.path.expanduser(corrected))
            if os.path.isdir(cp):
                _attach_folder_interactive(cp, attachments)
            else:
                cmd_find(corrected, cfg, attachments)
            return
        _attach_folder_interactive(res["dir"], attachments)
        return
    if not files:
        ui.err(f"Couldn't locate anything for: {hints}"
               + (f" / {_esc(res['filename'])}" if res["filename"] else ""))
        return
    if len(files) == 1:
        choice = files[0]
        # A lone fuzzy match can still be the WRONG file — the underlying LLM
        # parse isn't perfectly deterministic even at temperature 0, so the
        # SAME description can resolve differently between runs. ALWAYS
        # confirm before a guess rides silently into every AI tool.
        ans = _prompt(f"Match found: {choice} — attach it? (Y/n) ").strip().lower()
        if ans in ("n", "no"):
            corrected = _prompt("type the correct file name/path (Enter to cancel): ").strip()
            if not corrected:
                ui.info("cancelled.")
                return
            cp = os.path.abspath(os.path.expanduser(corrected))
            if os.path.isfile(cp):
                choice = cp
            else:
                return cmd_find(corrected, cfg, attachments)
    else:
        ui.say("  Matches:")
        for i, f in enumerate(files, 1):
            ui.say(f"   {i}. {_esc(f)}")
        picked = _prompt("attach which? (number, Enter to cancel)").strip()
        if not picked.isdigit() or not (1 <= int(picked) <= len(files)):
            ui.info("cancelled.")
            return
        choice = files[int(picked) - 1]
    try:
        att = F.attach(choice)
    except Exception as e:
        ui.err(f"Could not attach {choice}: {e}")
        return
    attachments.append(att)
    note = "text extracted" if att["text"] else "will upload as-is"
    ui.ok(f"Attached {att['name']} ({att['kind']}, {note})")


def cmd_attach(arg, attachments, cfg=None):
    import shlex
    from core import files as F
    try:
        paths = shlex.split(arg)
    except ValueError:
        paths = arg.split()
    if not paths:
        ui.warn("Usage: /attach <path> [more paths…]   (quote paths with spaces)")
        return
    # An unquoted path with spaces ("/attach my project notes.pdf") is one
    # real file, not a description — attach it directly.
    whole = os.path.expanduser(arg.strip().strip('"').strip("'"))
    if len(paths) > 1 and os.path.isfile(whole):
        paths = [whole]
    # A multi-word argument where nothing exists on disk reads like a spoken
    # description ("the report in my documents folder") — hand it to /find.
    elif cfg is not None and len(paths) >= 3 and not any(
            os.path.exists(os.path.expanduser(p)) for p in paths):
        ui.info("that doesn't look like a path — trying to find it from your description")
        return cmd_find(arg, cfg, attachments)
    for p in paths:
        p_exp = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p_exp):
            # A folder: show contents, let the user pick files or take all.
            _attach_folder_interactive(p_exp, attachments)
            continue
        try:
            att = F.attach(p)
        except FileNotFoundError:
            ui.err(f"Not a file: {p}")
            continue
        except Exception as e:
            ui.err(f"Could not attach {p}: {e}")
            continue
        attachments.append(att)
        note = "text extracted" if att["text"] else "will upload as-is"
        ui.ok(f"Attached {att['name']} ({att['kind']}, {note})")


def cmd_files(attachments):
    from core import files as F
    if not attachments:
        ui.info("No files attached. Use /attach <path> to add some.")
        return
    for a in attachments:
        ui.say("  • " + F.describe(a))


def cmd_detach(attachments):
    n = len(attachments)
    attachments.clear()
    ui.ok(f"Cleared {n} attachment(s).")


def cmd_skills() -> None:
    """What doctrine Prism has, where it applies, and how to change it."""
    from core import skills as SK
    ui.rule("Skills")
    ui.say(SK.describe())
    ui.say("")
    ui.info(f"shipped with Prism:  {SK.shipped_dir()}")
    ui.info(f"yours:               {SK.user_dir()}")
    ui.say("")
    ui.say("Put a SKILL.md in a folder of your own under the second path to "
           "add a skill, or a notes.md beside a shipped one to add your own "
           "notes to it. The format is in the README beside the shipped "
           "skills.")


def cmd_runs(cfg):
    import glob
    import json
    files = sorted(glob.glob(os.path.join(C.RUNS_DIR, "run_*.json")), reverse=True)
    if not files:
        ui.info("No runs yet.")
        return
    for fp in files[:15]:
        try:
            with open(fp) as f:
                d = json.load(f)
            tag = "[dim](dry)[/dim]" if d.get("dry") else ""
            ui.say(f"  {os.path.basename(fp)}  {tag}  {d.get('query','')[:70]}")
        except Exception:
            continue


def cmd_remote(cfg, arg: str):
    import time
    from core import remote

    arg = arg.strip()
    if arg in ("stop", "off"):
        if remote.is_running():
            remote.stop()
            ui.ok("Remote bridge stopped.")
        else:
            ui.info("Remote bridge isn't running.")
        return

    if not arg:
        try:
            link = remote.start()
        except Exception as e:
            ui.err(f"Could not start the remote bridge: {e}")
            return
        ui.panel(
            f"Remote bridge is live at  [bold]{link}[/bold]\n\n"
            "1. Open that link on any device on the same Wi-Fi\n"
            "2. The page shows a 4-digit code\n"
            "3. Back here, type  [bold]/remote <that code>[/bold]  to pair & listen",
            title="📡  Prism Remote", style="blue",
        )
        return

    if arg.startswith("url"):
        link = arg[3:].strip()
        if link:
            cfg["remote_relay"] = link.rstrip("/")
            C.save(cfg)
            ui.ok(f"Relay set to {cfg['remote_relay']} — /remote <code> now pairs over the internet.")
        else:
            current = cfg.get("remote_relay", "")
            ui.info(f"Current relay: {current or 'none (local Wi-Fi bridge only)'}\n"
                    "Set one with: /remote url https://your-relay.example.com")
        return

    if not (arg.isdigit() and len(arg) == 4):
        ui.warn("Usage: /remote            start the local bridge (shows the URL)\n"
                "       /remote <4-digit>  pair a code (local bridge, or relay if set)\n"
                "       /remote url <link> set a hosted relay for internet-wide pairing\n"
                "       /remote stop       shut the local bridge down")
        return

    # ── local bridge mode ──────────────────────────────────────────────────
    if remote.is_running():
        if not remote.pair(arg):
            ui.err(f"No local session with code {arg}. Reload the page and use the code it displays.")
            return
        ui.ok(f"Paired with local remote session {arg}.")
        ui.info("Listening for prompts from the website — press Ctrl-C to leave remote mode.")
        try:
            while True:
                item = remote.next_prompt(arg)
                if item is None:
                    time.sleep(1)
                    continue
                pid, prompt = item
                ui.rule("REMOTE TASK", "blue")
                ui.say(f"[bold]›[/bold] {prompt}")
                try:
                    run_query(cfg, prompt, dry=False, confirm=False)
                    remote.set_status(pid, "done")
                except Exception as e:
                    remote.set_status(pid, f"error: {str(e)[:80]}")
                    ui.err(f"remote task failed: {e}")
        except KeyboardInterrupt:
            ui.info("\nLeft remote mode — the bridge is still up (pair again with "
                    "/remote <code>, or /remote stop to shut it down).")
        return

    # ── hosted relay mode (works from anywhere, e.g. a friend's idea) ──────
    base = cfg.get("remote_relay", "")
    if not base:
        ui.err("Nothing to pair against: the local bridge isn't running and no relay is set.\n"
               "  Same Wi-Fi:  /remote          (starts the local bridge)\n"
               "  Internet:    /remote url <link-to-your-hosted-relay>, then /remote <code>")
        return
    token = remote.relay_pair(base, arg)
    if not token:
        ui.err(f"Relay at {base} has no session with code {arg} (codes expire "
               "after 15 min unpaired — ask them to reload the page).")
        return
    ui.ok(f"Paired with code {arg} on {base}.")
    ui.info("Listening for prompts from the relay — press Ctrl-C to stop.")
    try:
        while True:
            try:
                item = remote.relay_next(base, arg, token)
            except Exception:
                time.sleep(5)   # network blip — back off and retry
                continue
            if item is None:
                time.sleep(2)
                continue
            pid, prompt = item
            ui.rule("REMOTE TASK", "blue")
            ui.say(f"[bold]›[/bold] {prompt}")
            try:
                run_query(cfg, prompt, dry=False, confirm=False)
                remote.relay_set_status(base, pid, token, "done")
            except Exception as e:
                remote.relay_set_status(base, pid, token, f"error: {str(e)[:80]}")
                ui.err(f"remote task failed: {e}")
    except KeyboardInterrupt:
        ui.info("\nLeft remote mode.")


def _discover_recipients(cfg, goal: str, source_files: list | None = None,
                         query: str = "") -> list:
    """No address given — could be one named org ('email sarvam about…') or
    a whole category ('find the best-suited agencies in Vadodara and email
    them…'). Either way: a research agent searches the web for who actually
    matches, a second pass structures the findings into a strict CSV (its
    own agent, or the same one again — cycling back is fine), and the result
    is written to a real CSV on disk and shown to the user before anything
    downstream drafts or sends a single email.

    Never guesses silently: discovery only runs after a yes, the found list
    is shown and confirmed on its own, and the eventual send still goes
    through the normal preview/edit/confirm in cmd_email."""
    import time
    from core.onboarding import _ask_confirm
    try:
        from core import automation, mailer
    except Exception as e:
        ui.err(f"Automation deps not available ({e}).")
        return []

    agents = C.active_agents(cfg)
    # "leads" is the stage built for this, so it is asked first. After that,
    # a lead database wins the slot from whichever category it is set in —
    # verified contact records beat a chat model recalling company names, and
    # this is the only place that knows the run is outreach rather than
    # ordinary research. Web-search agents are the last resort, not the plan.
    _FINDERS = ("leads", "research", "brains")
    finder = next((s for s in _FINDERS if agents.get(s)
                   and mailer.prefers_lead_database(agents[s])), None)
    if not finder:
        finder = next((s for s in _FINDERS if agents.get(s)), None)
    if not finder:
        return []
    structurer = next((s for s in ("brains", "content", "research")
                       if agents.get(s) and s != finder), finder)
    _flush_stdin_noise()
    how = ("look up who actually matches this in its verified contact database"
           if mailer.prefers_lead_database(agents[finder])
           else "search the web for who actually matches this")
    if not _ask_confirm(
            f"No recipients given — have {agents[finder]} {how}, so Prism can "
            f"draft AND send them an email — \"{goal}\"?", default=True):
        return []

    # A lead database takes FILTERS, not paragraphs — so the filters are
    # decided here, by Groq, before Apollo ever opens. The block it writes
    # is what _run_apollo parses straight into Apollo's own search URL; if
    # Groq is unavailable the old prose prompt still works through the
    # typed-search fallback.
    filter_block = ""
    if mailer.prefers_lead_database(agents[finder]):
        try:
            from core import router as _router
            filter_block = _router.groq_chat(
                cfg.get("api_key", ""),
                cfg.get("model", "llama-3.3-70b-versatile"),
                mailer.apollo_filter_prompt(goal), temperature=0, timeout=45)
            if "HANDOFF FOR APOLLO" not in filter_block.upper():
                filter_block = ""
            else:
                shown = "; ".join(
                    ln.strip() for ln in filter_block.splitlines()
                    if ":" in ln)[:200]
                ui.info(f"🎯  filters decided locally first — {shown}")
        except Exception:
            filter_block = ""

    research_q, structure_q = mailer.discovery_prompts(goal, agents[finder],
                                                       filter_block)
    custom_stages = [
        ("research", agents[finder], [research_q]),
        ("structure", agents[structurer], [structure_q]),
    ]
    try:
        responses, links = automation.run(
            {"structure": {"expect": "email"}}, cfg,
            attachments=source_files or [], query=query or goal,
            custom_stages=custom_stages)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        ui.err(f"discovery failed: {e}")
        return []

    csv_text = "\n".join(responses.get("structure") or [])
    found = mailer.parse_structured_csv_text(csv_text)
    if not found:
        ui.warn("No candidate came back with a confirmed email — give "
                "recipients explicitly instead (a CSV, or addresses typed "
                "right into the prompt).")
        if links.get("research"):
            ui.info(f"Raw research is here if you want to check it yourself: "
                    f"{links['research']}")
        return []

    os.makedirs(C.RUNS_DIR, exist_ok=True)
    path = os.path.join(C.RUNS_DIR, f"discovered_{int(time.time())}.csv")
    mailer.write_recipients_csv(found, path)
    ui.say(f"  Found {len(found)} candidate(s) with a confirmed email → saved to {path}")
    for i, r in enumerate(found, 1):
        site = f"  · {_esc(r['website'])}" if r["website"] else ""
        ui.say(f"   {i}. {_esc(r['name'] or '(no name)')} — {_esc(r['email'])}{site}")

    _flush_stdin_noise()
    if not _ask_confirm(f"Draft an email and prepare to send it to all "
                        f"{len(found)}?", default=True):
        ui.info(f"Not sending. The list is saved — /attach {path} and run "
                "/email again whenever you're ready.")
        return []
    return found


def cmd_email(cfg, arg: str, attachments: list):
    """/email — mail-merge through the pipeline.

    /email setup                     configure your sending account (once)
    /email <what the email is for>   analyse attachments → draft → send to CSV
    """
    from core import mailer
    from core.onboarding import _ask_text, _ask_confirm

    arg = arg.strip()

    # ── one-time account setup ──────────────────────────────────────────────
    if arg == "setup" or (arg and not mailer.is_configured(cfg)):
        ui.rule("Email account", "orange")
        ui.panel(
            "Prism sends through YOUR account via SMTP — nothing is stored\n"
            "anywhere but ~/.prism/config.json (chmod 600).\n\n"
            "Gmail users: this needs an [bold]app password[/bold], not your real one —\n"
            "create one at  [bold]myaccount.google.com/apppasswords[/bold]",
            title="✉️  Setup", style="orange",
        )
        address = _ask_text("Your email address:",
                            default=(cfg.get("email") or {}).get("address", ""))
        if not address or "@" not in address:
            ui.err("That doesn't look like an email address — aborting setup.")
            return
        password = _ask_text("App password:", secret=True)
        if not password:
            ui.err("No password entered — aborting setup.")
            return
        known = mailer.smtp_for(address)
        if known:
            host, port = known
        else:
            host = _ask_text("SMTP host (e.g. smtp.yourcompany.com):")
            port = _ask_text("SMTP port (465 = SSL, 587 = STARTTLS):", default="587")
        cfg["email"] = {"address": address.strip(),
                        "password": mailer.clean_password(password),
                        "host": host, "port": int(port)}
        C.save(cfg)
        ui.ok(f"Email account saved ({address} via {host}).")
        if arg == "setup":
            return

    if not arg:
        ui.warn("Usage: /email setup              configure your sending account\n"
                "       /email <goal of the email> draft from attachments & send to the CSV\n"
                "First /attach a recipients CSV + the source file (brochure, doc…).")
        return

    # ── recipients: from an attached CSV and/or typed right in the prompt ───
    csvs, source_files = mailer.split_attachments(attachments)
    inline, arg = mailer.recipients_from_text(arg)   # strip addresses from the goal
    recipients = list(inline)
    for a in csvs:
        recipients += mailer.parse_recipients(a["path"])
    seen = set()
    recipients = [r for r in recipients
                  if not (r["email"] in seen or seen.add(r["email"]))]
    if not recipients:
        # No address anywhere — could be one named org ("email sarvam about a
        # partnership") or a whole category ("find the best-suited agencies
        # in Vadodara and email them"). Either way, offer to go find who
        # actually matches before giving up.
        recipients = _discover_recipients(cfg, arg, source_files,
                                          query=f"write an email: {arg}")
    if not recipients:
        ui.err("No recipients. /attach a CSV with addresses, write one in the "
               "prompt (/email tell them about X — a@x.com), or say yes to "
               "the email search.")
        return
    if not arg.strip():
        ui.err("The prompt only contained addresses — also say what the email is about.")
        return
    src = " + ".join(filter(None, [
        f"{len(inline)} from the prompt" if inline else "",
        f"{len(recipients) - len(inline)} from {', '.join(a['name'] for a in csvs)}" if csvs else "",
    ]))
    ui.ok(f"{len(recipients)} recipient(s): {src} "
          "(parsed locally — addresses are never shown to any AI).")

    # ── fixed plan: ChatGPT analyses the source files (injected by
    #    automation.run, like every attachment run), then the draft stage ────
    agents = C.active_agents(cfg)
    avail = [s for s in ("research", "brains", "content") if agents.get(s)]
    if not avail:
        ui.err("No research/brains/content agent configured — run /agents first.")
        return
    draft_stage = avail[-1]
    routing = {draft_stage: {"needed": True,
                             "reason": "write the email draft — and ONLY the draft",
                             # the wait isn't over until the draft marker shows
                             "expect": "SUBJECT:",
                             "questions": [mailer.draft_question(arg)]}}

    ui.routing_plan(routing, agents)
    _flush_stdin_noise()
    if not _ask_confirm("\nRun this plan against your logged-in browser now?", default=True):
        ui.info("Cancelled.")
        return

    try:
        from core import automation
    except Exception as e:
        ui.err(f"Automation deps not available ({e}). Install requirements.txt.")
        return
    responses, links = automation.run(routing, cfg, attachments=source_files,
                                      query=f"write an email: {arg}")

    draft_texts = [t for t in (responses.get(draft_stage) or [])
                   if not mailer.is_prompt_echo(t)]
    draft = mailer.parse_draft(draft_texts[0] if draft_texts else "")
    if not draft:
        ui.err("Couldn't find a 'SUBJECT: … / BODY: …' draft in the response.")
        if links.get(draft_stage):
            ui.info(f"Read it yourself and send manually: {links[draft_stage]}")
        return
    subject, body = draft

    # ── preview, customize, confirm, send ───────────────────────────────────
    # Never leaves this loop except via an explicit "Send now" or "Cancel" —
    # "Edit" comes straight back here so the peek always reflects the latest
    # version before anything actually goes out.
    from core.onboarding import _ask_select
    while True:
        preview = body if len(body) <= 700 else body[:700] + "…"
        ui.panel(
            f"[bold]Subject:[/bold] {subject}\n\n{preview}\n\n"
            f"[bold]To:[/bold] {len(recipients)} recipient(s) — "
            f"{', '.join(r['email'] for r in recipients[:5])}"
            f"{', …' if len(recipients) > 5 else ''}\n"
            f"[bold]Attachments:[/bold] "
            f"{', '.join(f['name'] for f in source_files) or 'none'}\n"
            f"[bold]From:[/bold] {cfg['email']['address']}",
            title="✉️  Ready to send", style="teal",
        )
        _flush_stdin_noise()
        choice = _ask_select(
            f"Send this to all {len(recipients)} recipients now?",
            ["Cancel — don't send", "Edit subject/body", "Send now"],
            default="Cancel — don't send")
        if choice == "Send now":
            break
        if choice == "Edit subject/body":
            new_subject = _ask_text("Subject:", default=subject)
            new_body = _ask_text("Body:", default=body, multiline=True)
            subject = new_subject.strip() or subject
            body = new_body.strip() or body
            continue
        ui.info("Not sent. The draft is saved in this run's file (/runs).")
        C.save_run({"query": f"/email {arg}", "routing": routing, "responses": responses,
                    "links": links, "email": {"subject": subject, "sent": [],
                                              "recipients": len(recipients), "confirmed": False}})
        return

    # The same pace and per-run cap the GUI window uses (prism_gui's
    # email_config.send_policy writes cfg["email"]["send"]); a config without
    # the block sends as it always has. The daily cap is counted off the
    # GUI's sent log and is not applied here.
    pace = (cfg.get("email") or {}).get("send") or {}
    try:
        gap = max(0.5, float(pace.get("gap_seconds", mailer.SEND_DELAY)))
        jitter = max(0.0, float(pace.get("jitter_seconds", 0) or 0))
        per_run = max(0, int(pace.get("max_per_run", 0) or 0))
    except (TypeError, ValueError):
        gap, jitter, per_run = mailer.SEND_DELAY, 0.0, 0
    sent, failed = mailer.send_bulk(cfg, recipients, subject, body, source_files,
                                    delay=gap, jitter=jitter, limit=per_run)
    if sent:
        ui.ok(f"Sent to {len(sent)}/{len(recipients)} recipient(s).")
    if failed:
        ui.err(f"{len(failed)} failed: " + "; ".join(f"{e} ({msg[:60]})" for e, msg in failed[:5]))
        ui.info("Gmail rejecting logins? Use an app password: myaccount.google.com/apppasswords")
    path = C.save_run({"query": f"/email {arg}", "routing": routing, "responses": responses,
                       "links": links, "email": {"subject": subject, "sent": sent,
                                                 "failed": failed, "recipients": len(recipients)}})
    ui.ok(f"Run saved → {path}")


def _parse_boq_directives(text: str):
    """Pull optional scope:/unit:/legend: directives out of a /boq context
    string — pipe-separated from each other and from the plain description,
    e.g. 'scope:cctv,cable,fiber,ep,ht | unit:meters | legend:EP=electric
    pole; HP=high tension pole | site development for a client township'.
    `measured-only` (a bare word, no colon) forbids derived/design-stage
    items, so a trade the drawing doesn't contain is reported as missing
    instead of estimated.
    Returns (clean_context, scope_keywords, unit_override, legend, allow_derived)."""
    scope: list[str] = []
    unit_override = ""
    legend = ""
    allow_derived = True
    rest: list[str] = []
    for part in text.split("|"):
        p = part.strip()
        low = p.lower()
        if low.startswith("scope:"):
            scope = [k.strip() for k in p[len("scope:"):].split(",") if k.strip()]
        elif low.startswith("unit:"):
            unit_override = p[len("unit:"):].strip()
        elif low.startswith("legend:"):
            legend = p[len("legend:"):].strip()
        elif low in ("measured-only", "measured only", "no-derive"):
            allow_derived = False
        elif p:
            rest.append(p)
    return " ".join(rest).strip(), scope, unit_override, legend, allow_derived


def cmd_boq(cfg, arg: str, attachments: list):
    """/boq <path to .dwg/.dxf> [project context] — real quantity takeoff.

    Lengths/areas/counts are measured directly from the drawing's geometry
    (core.boq — no AI ever sees the drawing itself), saved to an auditable
    CSV, and only THEN handed to an agent to write up as a professional BOQ —
    matching an attached template's exact structure/columns if one is given
    (anything /attach-ed alongside the drawing that isn't itself .dwg/.dxf).

    Optional pipe-separated directives in the context text:
      scope:cctv,cable,fiber   only include layers/blocks matching these —
                               a hard, deterministic filter (not a hope the
                               agent ignores unrelated trades on its own)
      unit:meters              you already know the real unit; skip the
                               "unit not confirmed" caveat entirely
      legend:EP=electric pole; HP=high tension pole
                               translate cryptic layer/block codes into real
                               descriptions instead of leaving them as TBC
    """
    from core import boq

    arg = arg.strip()
    tokens = arg.split(" ", 1) if arg else []
    cad_attachments, templates, images, note_files = boq.classify_inputs(attachments)

    # A drawing is OPTIONAL. With one, quantities are measured and the run is
    # a takeoff. Without one — a spec-only enquiry, "quote the materials to
    # build a 36x24 jaw crusher" — there is nothing to measure, so the same
    # pipeline runs in SPEC MODE and every quantity is derived from the
    # stated requirement plus the design-standards brief, clearly labelled.
    path = ""
    if tokens and os.path.exists(tokens[0]) and \
            tokens[0].lower().endswith((".dwg", ".dxf")):
        path, context = tokens[0], (tokens[1] if len(tokens) > 1 else "")
    elif cad_attachments:
        path, context = cad_attachments[0]["path"], arg
    else:
        context = arg
    context, scope_keywords, unit_override, legend, allow_derived = \
        _parse_boq_directives(context)

    if not path and not context:
        ui.warn("Say what the BOQ is for, or give a drawing.\n"
                "  with a drawing:  /boq <path to .dwg/.dxf> <context>\n"
                "                   (or /attach it, then /boq <context>)\n"
                "  without one:     /boq materials to build one 36x24 single-toggle "
                "jaw crusher, 100 TPH\n"
                "Optional: scope:kw1,kw2 | unit:meters | legend:CODE=meaning | measured-only")
        return

    support_files = templates + images + note_files
    if support_files:
        for label, group in (("sample BOQ", templates), ("image/screenshot", images),
                             ("notes", note_files)):
            if group:
                ui.info(f"📎  {label}: {', '.join(g['name'] for g in group)}")

    q, summary, csv_path = None, "", ""
    if path:
        ui.info(f"📐  measuring real geometry from {os.path.basename(path)} — "
                "no AI touches the drawing itself, only the numbers this produces.")
        try:
            dxf_path, notes = boq.ensure_dxf(path)
            q = boq.measure(dxf_path)
        except boq.BoqError as e:
            ui.err(str(e))
            return
        for note in notes:
            ui.warn(note)

        if unit_override:
            boq.apply_known_unit(q, unit_override)
            ui.info(f"📏  using your stated unit: {unit_override}")
        if scope_keywords:
            q = boq.filter_by_keywords(q, scope_keywords)
            ui.info(f"🔎  scope filter applied: {', '.join(scope_keywords)}")

        summary = boq.summary_text(q)
        ui.panel(summary, title="📐  Measured quantities (real, not AI-guessed)", style="teal")

        import time
        os.makedirs(C.RUNS_DIR, exist_ok=True)
        csv_path = os.path.join(C.RUNS_DIR, f"boq_quantities_{int(time.time())}.csv")
        boq.write_quantities_csv(q, csv_path)
        ui.ok(f"Raw quantities saved → {csv_path} (cross-check the formatted BOQ against this)")

        if not (q["lengths_by_layer"] or q["areas_by_layer"] or q["block_counts"]):
            ui.warn("Nothing measurable came out of this drawing (no LINE/POLYLINE/HATCH/"
                    "INSERT geometry) — a formatted BOQ would have nothing real to show, "
                    "so stopping here. It may use 3D solids or an unsupported entity type.")
            return
    else:
        # Spec mode. Say plainly that nothing is measured, so the output is
        # never mistaken for a takeoff.
        ui.warn("No drawing given — running in SPEC MODE. Every quantity will be "
                "DERIVED from your stated requirement and standard design practice, "
                "not measured. The document will say so.")
        if not allow_derived:
            ui.warn("`measured-only` ignored: with no drawing there is nothing to "
                    "measure, so derivation is the only way to produce anything.")
            allow_derived = True

    agents = C.active_agents(cfg)
    writer = next((s for s in ("content", "brains") if agents.get(s)), None)
    if not writer:
        ui.err("No content/brains agent configured — run /agents first.")
        return

    # ── build the pipeline ──────────────────────────────────────────────
    # Each stage exists because it can do something the others cannot, and
    # each is given only files it can actually read. Handing ChatGPT the
    # .dwg was the original failure — it can't parse a 13MB binary CAD file,
    # so it returned nothing and the writer followed a brief that never
    # existed. Splitting the work also keeps any one prompt from becoming
    # the bulky catch-all that degrades the result.
    from core import files as F
    cad_file = F.attach(path) if path else None
    user_request = context or "Produce a Bill of Quantities from the attached drawing."

    researcher = agents.get("research") or agents.get("brains")
    interpreter = "ChatGPT" if images else None

    plan: list[tuple[str, str, str]] = []
    if path:
        plan.append(("measure", "Prism (local)",
                     f"DONE — {os.path.basename(path)} parsed with ezdxf; real "
                     "lengths, areas & block counts, no AI involved"))
    if allow_derived and researcher:
        plan.append(("standards", researcher,
                     "look up the design norms and standard specs this quote "
                     "must follow — no files needed"))
    if interpreter:
        plan.append(("interpret", interpreter,
                     "read the screenshot + sample BOQ for the legend, scope "
                     "and house style (never the .dwg — it can't parse one)"))
    plan.append(("write", agents[writer],
                 ("open the .dwg itself, then write the BOQ from the measured "
                  "data + the briefs above, in the sample's style") if path else
                 ("derive every quantity from your stated requirement + the "
                  "standards brief, in the sample's style — labelled as an estimate")))
    ui.pipeline_plan(plan, title="BOQ pipeline" if path else "BOQ pipeline (spec mode — nothing measured)")

    if not _ask_yes_no("Run this pipeline?", default=True):
        ui.info("Not written." + (" The raw quantities CSV above is still yours to use."
                                  if csv_path else ""))
        return

    try:
        from core import automation
    except Exception as e:
        ui.err(f"Automation deps not available ({e}). Install requirements.txt.")
        return

    links: dict = {}
    standards_text, brief_text = "", ""

    def _run_stage(label, agent, prompt, files, query):
        """One stage = one automation.run. Separate calls (not one chained
        run) because each stage needs a DIFFERENT file set, and the briefs
        are threaded explicitly below rather than relying on the relay —
        which is what produced an empty and then a garbled handoff."""
        r, l = automation.run({}, cfg, attachments=files, chatgpt_analysis=False,
                              custom_stages=[(label, agent, [prompt])], query=query)
        links.update(l)
        got = [t for t in (r.get(label) or []) if t.strip()]
        return got[0] if got else ""

    if allow_derived and researcher:
        standards_text = _run_stage(
            "standards", researcher,
            boq.standards_prompt(user_request, project_context=context),
            [],   # deliberately no files: this stage is pure web research
            "design standards for a BOQ trade")
        if standards_text:
            head = standards_text[:900] + ("…" if len(standards_text) > 900 else "")
            ui.panel(head, title=f"📐  {researcher}'s design-standards brief", style="teal")
        else:
            ui.warn(f"{researcher} returned nothing — {agents[writer]} will fall "
                    "back on its own knowledge of the norms.")

    if interpreter:
        brief_text = _run_stage(
            "interpret", interpreter,
            boq.interpretation_prompt(user_request, summary,
                                      boq.roles_text([], templates, images, note_files),
                                      legend_hint=legend),
            images + templates + note_files,   # NOT the .dwg
            "read a drawing screenshot for BOQ legend and scope")
        if brief_text:
            head = brief_text[:900] + ("…" if len(brief_text) > 900 else "")
            ui.panel(head, title=f"🔍  {interpreter}'s legend & scope brief", style="orange")
        else:
            ui.warn(f"{interpreter} returned nothing — {agents[writer]} will work "
                    "from the measured data alone.")

    format_q = boq.formatting_prompt(summary, project_context=context,
                                     has_template=bool(templates),
                                     legend=legend, scoped=bool(scope_keywords),
                                     brief_text=brief_text,
                                     standards_text=standards_text,
                                     allow_derived=allow_derived,
                                     has_cad=bool(path))
    # The writer DOES get the raw CAD file when there is one — Claude can open
    # a .dwg and read its layers directly, which is exactly why it belongs
    # here and not on ChatGPT's stage.
    # Never the drawing: the writer gets the measured figures in its
    # prompt and the templates/notes as files (same rule as /gerber).
    write_files = templates + note_files
    responses, l2 = automation.run(
        {}, cfg, attachments=write_files, chatgpt_analysis=False,
        custom_stages=[("format", agents[writer], [format_q])],
        query=(f"Bill of Quantities from a CAD drawing{(' — ' + context) if context else ''}"
               if path else f"Bill of Quantities from a stated requirement — {context}"))
    links.update(l2)

    if links.get("interpret"):
        ui.info(f"{interpreter} tab: {links['interpret']}")

    texts = responses.get("format") or []
    if not texts:
        ui.err("No response came back — the raw quantities CSV is still saved above.")
        if links.get("format"):
            ui.info(f"Link: {links['format']}")
        return

    preview = texts[0] if len(texts[0]) <= 2000 else texts[0][:2000] + "…"
    ui.panel(preview, title="📄  Draft BOQ", style="teal")
    if links.get("format"):
        ui.info(f"Full response: {links['format']}")
    saved = C.save_run({"query": f"/boq {arg}", "responses": responses, "links": links,
                       "boq": {"quantities_csv": csv_path, "source": path}})
    ui.ok(f"Run saved → {saved}")


def _show_gerber(job, label: str = ""):
    """Print one measured job and save its auditable CSV."""
    from core import gerber as G
    import time

    ui.panel(G.files_text(job), title="📁  What is in this job", style="teal")
    ui.panel(G.answers_text(job),
             title="📐  The five numbers (measured, not guessed)", style="teal")
    ui.panel(G.summary_text(job), title="🔬  The workings behind them", style="pink")
    ui.panel(G.crosscheck_text(G.crosscheck(job)),
             title="✓  Checked against the job's own CAM report", style="teal")
    for w in job["warnings"]:
        ui.warn(w)

    os.makedirs(C.RUNS_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    stem = f"gerber_{safe}_{int(time.time())}" if safe else f"gerber_{int(time.time())}"
    csv_path = os.path.join(C.RUNS_DIR, f"{stem}.csv")
    G.write_report_csv(job, csv_path)
    ui.ok(f"Auditable figures saved → {csv_path}")



def _step_measured(cfg, arg: str, attachments: list):
    """The offline half both /step and /step-auto share: find the model,
    measure it, save the Excel sheet and Prism's own plain drawing render.
    Returns (report, out_dir, drawn) or None after explaining itself."""
    from core import stepfile as SF

    ok, why = SF.available()
    if not ok:
        ui.err(why)
        return None

    mode = "metal"
    tokens = arg.strip().split()
    if tokens and tokens[0].lower().lstrip("/") in SF.MODES:
        mode = tokens[0].lower().lstrip("/")
        tokens = tokens[1:]
    rest = " ".join(tokens).strip().strip('"').strip("'")

    target = ""
    if rest:
        cand = os.path.expanduser(rest)
        if os.path.exists(cand):
            target = cand
    if not target:
        for a in attachments:
            if a["path"].lower().endswith((".step", ".stp")):
                target = a["path"]
                break
    if not target:
        ui.warn("Point Prism at the model.\n"
                "  /step ~/Downloads/Assem1.STEP\n"
                "  /step plastic ~/Downloads/housing.stp\n"
                "  (or /attach the file, then /step)")
        return None

    ui.info(f"📐  measuring {os.path.basename(target)} ({mode} moulding) — "
            "the design never leaves this machine; no AI sees the STEP file.")
    try:
        report = SF.analyse(target, mode=mode)
    except SF.StepError as e:
        ui.err(str(e))
        return None

    ui.panel(SF.report_text(report), title="What the model measures",
             style="teal")

    # One folder per model, named after it, under wherever the person keeps
    # their STEP work (cfg["step_out_dir"], set with /step-folder or by the
    # GUI's own question) — and every file inside carries the model's name,
    # so ten jobs' sheets in one place are ten files a person can tell apart.
    out_dir = SF.output_dir(target, cfg.get("step_out_dir", ""))
    ui.info(f"   files for this model → {out_dir}")
    try:
        xlsx = SF.write_xlsx(report,
                             os.path.join(out_dir, SF.names(report)["xlsx"]))
        ui.ok(f"dimension sheet → {xlsx}")
    except SF.StepError as e:
        ui.warn(str(e))
    ui.info("   drawing the parts…")
    drawn = SF.render_sheet(report, out_dir)
    ui.ok(f"drawing sheet   → {drawn['png']}")
    for name, path in drawn.get("flats", {}).items():
        ui.ok(f"  {name} flat pattern → {path}")
    return report, out_dir, drawn


def cmd_step(cfg, arg: str, attachments: list):
    """/step [metal|plastic] <file.step> — measure a 3D CAD model.

    The estimator's first hour on a moulding inquiry, done offline: every
    part's formed size, thickness, holes, volume and weight, read from the
    real geometry by core.stepfile. The customer's STEP file never leaves
    this machine and no AI ever sees it — same rule as /gerber.
    """
    _step_measured(cfg, arg, attachments)


def cmd_step_folder(cfg, arg: str):
    """/step-folder [path] — where /step keeps a model's files.

    The terminal's answer to the question the GUI asks in a dialog: one
    place for all STEP work, with a folder per model inside it. No argument
    shows the current choice; `default` puts it back to ~/Desktop/Prism Step.
    """
    from core import stepfile as SF

    want = arg.strip().strip('"').strip("'")
    if not want:
        current = cfg.get("step_out_dir") or SF.DEFAULT_OUT_ROOT
        ui.info(f"STEP files go to {current}\n"
                "  /step-folder ~/Jobs/STEP     to change it\n"
                "  /step-folder default         to go back to the Desktop")
        return
    if want.lower() == "default":
        cfg["step_out_dir"] = ""
        C.save(cfg)
        ui.ok(f"STEP files go to {SF.DEFAULT_OUT_ROOT} again.")
        return
    path = os.path.abspath(os.path.expanduser(want))
    if os.path.exists(path) and not os.path.isdir(path):
        ui.err(f"{path} is a file, not a folder.")
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        ui.err(f"Couldn't create {path}: {e}")
        return
    cfg["step_out_dir"] = path
    C.save(cfg)
    ui.ok(f"STEP files will go to {path} — one folder per model, every file "
          "named after the model.")


def cmd_step_auto(cfg, arg: str, attachments: list):
    """/step-auto [metal|plastic] <file.step> — measure offline, then have
    the visual agent draw the professional dimension sheet.

    The split IS the security model: core.stepfile measures the customer's
    model on this machine, and the only things that ever reach an AI are
    the measured figures and Prism's own plain render of the parts. The
    STEP file itself is never uploaded — same rule as /gerber, and the
    prompt says so, so the agent cannot ask for it either.
    """
    from core import stepfile as SF

    measured = _step_measured(cfg, arg, attachments)
    if measured is None:
        return
    report, out_dir, drawn = measured

    # ChatGPT, and only ChatGPT: its image model is the one that draws a
    # dimension sheet well from a numbers-only brief, so the stage neither
    # picks whatever visual tool is configured nor falls over to another
    # tool if it stumbles (failover=False below) — a second, differently-
    # wrong sheet is not a rescue. Prism's own dimensioned sheet is already
    # on disk either way.
    artist = "ChatGPT"

    try:
        from core import automation
        from core import files as F
    except Exception as e:
        ui.err(f"Automation deps not available ({e}). "
               "The measured sheet above is still yours.")
        return

    # Only Prism's OWN render travels — never the customer's model. When the
    # PNG could not be rendered (no Playwright), the numbers in the prompt
    # carry the whole brief on their own.
    files = []
    if drawn.get("png"):
        files.append(F.attach(drawn["png"]))
    ui.info(f"🎨  asking {artist} to draw the dimensioned sheet — from the "
            "measured numbers only; the STEP file stays here.")

    made: list = []
    responses, links = automation.run(
        {}, cfg, attachments=files, chatgpt_analysis=False,
        custom_stages=[("visual", artist, [SF.auto_brief(report)])],
        query=f"draw a dimensioned fabrication sheet for {report['file']}",
        pipeline_files_out=made,
        # A picture IS the deliverable here — give the image model its full
        # budget instead of the short look a generic visual turn gets.
        image_stages={"visual"},
        failover=False)

    kept = []
    for rec in made:
        src = rec.get("path", "") if isinstance(rec, dict) else str(rec)
        if src and os.path.exists(src):
            dest = os.path.join(out_dir, SF.ai_sheet_name(
                report["stem"], len(kept) + 1, os.path.splitext(src)[1]))
            try:
                shutil.copyfile(src, dest)
                kept.append(dest)
            except OSError:
                pass
    for p in kept:
        ui.ok(f"AI drawing sheet → {p}")
    if not kept:
        texts = [t for t in (responses.get("visual") or []) if t.strip()]
        if texts:
            ui.warn("No image came back — what the agent said instead is in "
                    "the saved run.")
        else:
            ui.warn("The agent returned nothing. Prism's own measured sheet "
                    "above still stands.")
    if links.get("visual"):
        ui.info(f"Its tab: {links['visual']}")
    saved = C.save_run({"query": f"/step-auto {arg}".strip(),
                        "responses": responses, "links": links,
                        "step": {"out_dir": out_dir, "ai_sheets": kept}})
    ui.ok(f"Run saved → {saved}")


def cmd_step_ask(cfg, arg: str, attachments: list):
    """/step-ask [metal|plastic] <file.step> <question> — advice, then a
    reviewed change plan, then the changes applied to a COPY of the model.

    Three hands, one rule. Groq gets the measured numbers and the question
    and suggests; a browser agent reviews those suggestions against the
    same numbers and answers with a strict JSON plan of only the operations
    Prism can execute; Prism executes them HERE, in cadquery, and
    re-measures the result. The customer's STEP file never leaves this
    machine at any stage — the geometry work happens locally because Prism
    is the one actually holding the file.
    """
    from core import router
    from core import stepfile as SF

    tokens = arg.strip().split()
    mode = "plastic"
    if tokens and tokens[0].lower().lstrip("/") in SF.MODES:
        mode = tokens[0].lower().lstrip("/")
        tokens = tokens[1:]
    rest = " ".join(tokens)

    target, question = "", ""
    head = rest
    while head:
        cand = os.path.expanduser(head.strip().strip('"').strip("'"))
        if os.path.exists(cand):
            target = cand
            question = rest[len(head):].strip()
            break
        if " " not in head:
            break
        head = head.rsplit(" ", 1)[0]
    if not target:
        for a in attachments:
            if a["path"].lower().endswith((".step", ".stp")):
                target, question = a["path"], rest
                break
    if not target:
        ui.warn("Point Prism at the model and ask.\n"
                "  /step-ask plastic ~/Downloads/housing.stp how do I make "
                "this lighter?\n"
                "  (or /attach the file, then /step-ask <question>)")
        return
    question = question or ("Suggest improvements for reliable, economical "
                            f"{mode} moulding of this part.")

    measured = _step_measured(cfg, f"{mode} {target}", attachments)
    if measured is None:
        return
    report, out_dir, drawn = measured
    out = SF.names(report)

    api_key = cfg.get("api_key")
    if not api_key:
        ui.err("No Groq API key configured. Run /key to add one.")
        return
    ui.info("🧠  asking Groq — the measured numbers and your question only; "
            "the STEP file stays here.")
    try:
        suggestions = router.groq_chat(
            api_key, cfg.get("model", "llama-3.3-70b-versatile"),
            SF.ask_prompt(report, question), temperature=0.4,
            timeout=60).strip()
    except Exception as e:
        ui.err(f"Groq: {e}")
        return
    ui.panel(suggestions if len(suggestions) <= 2400
             else suggestions[:2400] + "…",
             title="💡  Suggested changes (from the measured figures)",
             style="teal")

    record = {"query": f"/step-ask {arg}".strip(),
              "responses": {"groq": [suggestions]}, "links": {},
              "step_ask": {"out_dir": out_dir, "question": question}}

    agents = C.active_agents(cfg)
    planner = next((agents[s] for s in ("brains", "content", "research")
                    if agents.get(s)), None)
    try:
        from core import automation
        from core import files as F
    except Exception as e:
        ui.warn(f"Automation deps not available ({e}) — the advice above "
                "is the answer; nothing will be changed.")
        planner = None
    if not planner:
        ui.ok(f"Run saved → {C.save_run(record)}")
        return

    if not _ask_yes_no(f"Have {planner} review this into an exact change "
                       "plan Prism can apply?", default=True):
        ui.ok(f"Run saved → {C.save_run(record)}")
        return

    files = [F.attach(drawn["png"])] if drawn.get("png") else []
    responses, links = automation.run(
        {}, cfg, attachments=files, chatgpt_analysis=False,
        custom_stages=[("plan", planner,
                        [SF.plan_prompt(report, question, suggestions)])],
        query=f"review and plan CAD changes for {report['file']}")
    record["responses"].update(responses)
    record["links"] = links

    texts = [t for t in (responses.get("plan") or []) if t.strip()]
    plan, why = SF.parse_plan(texts)
    if plan is None:
        ui.err(why)
        if links.get("plan"):
            ui.info(f"Its tab: {links['plan']}")
        ui.ok(f"Run saved → {C.save_run(record)}")
        return

    for a in plan["advice"]:
        ui.info(f"   • (for the designer) {a}")
    if not plan["changes"]:
        ui.info("Nothing in the plan is a change Prism can apply by itself "
                "— the advice above is the deliverable.")
        ui.ok(f"Run saved → {C.save_run(record)}")
        return
    for ch in plan["changes"]:
        what = (f"Ø{ch['dia_mm']:g} → Ø{ch['new_dia_mm']:g} on {ch['part']}"
                if ch["op"] == "enlarge_hole"
                else f"scale {ch['part']} x {ch['factor']:g}")
        ui.info(f"   ▸ {what}" + (f" — {ch['why']}" if ch["why"] else ""))

    # The approval page: every dimension before → after in one table, the
    # drawing image beside it — so the user confirms against what they can
    # SEE, not against terminal text. Nothing is built until they say Y.
    review = SF.review_html(report, plan, out_dir, question=question)
    ui.ok(f"review page → {review}")
    try:
        import webbrowser
        webbrowser.open("file://" + review)
    except Exception:
        pass

    if not _ask_yes_no(f"Reviewed the page — build {out['modified']} now? "
                       "(the original file is never touched)", default=True):
        ui.info("Not built. The review page stays as the record of the plan.")
        ui.ok(f"Run saved → {C.save_run(record)}")
        return
    out_path = os.path.join(out_dir, SF.modified_name(report))
    try:
        done = SF.apply_plan(target, plan, out_path)
    except SF.StepError as e:
        ui.err(str(e))
        ui.ok(f"Run saved → {C.save_run(record)}")
        return
    for line in done["log"]:
        (ui.warn if line.startswith("!") else ui.ok)(line)

    after = SF.analyse(out_path, mode=mode)
    ui.panel(SF.report_text(after),
             title="After the changes (re-measured, not assumed)",
             style="pink")
    try:
        SF.write_xlsx(after, os.path.join(out_dir, out["xlsx_after"]))
    except SF.StepError:
        pass
    # The visual half of the proof: the modified model drawn FROM THE
    # BUILT FILE, so the review page shows before and after side by side.
    # Its sheet is rendered into its own subfolder (the modified model has
    # the same stem, so its files would otherwise overwrite the originals),
    # and the PNG is copied up beside the review page under its after-name.
    try:
        drawn_after = SF.render_sheet(after,
                                      os.path.join(out_dir, out["after_dir"]))
        if drawn_after.get("png"):
            shutil.copyfile(drawn_after["png"],
                            os.path.join(out_dir, out["png_after"]))
    except Exception:                                   # noqa: BLE001
        pass
    # Same page, rewritten: the After column now holds the RE-MEASURED
    # figures from the built file, and the banner says so.
    review = SF.review_html(report, plan, out_dir, question=question,
                            after=after)
    ui.ok(f"before/after page → {review}")
    ui.ok(f"modified model → {out_path}")
    record["step_ask"]["modified"] = out_path
    record["step_ask"]["log"] = done["log"]
    ui.ok(f"Run saved → {C.save_run(record)}")


def cmd_gerber(cfg, arg: str, attachments: list):
    """/gerber <folder|zip|rar|files…> [what to write] — measure a PCB job.

    The five numbers a fab asks for — board size, minimum track width,
    minimum track spacing, minimum drill, hole count — measured from the
    real geometry by core.gerber. No AI ever sees a Gerber file: the design
    is the customer's intellectual property and it never leaves this machine.
    An agent is only offered the MEASURED NUMBERS afterward, to write the
    reply or the quotation.
    """
    from core import gerber as G

    arg = arg.strip()
    targets: list[str] = []
    context = ""
    if arg:
        # Everything up to the last path-like token is the job; the rest is
        # the covering instruction ("reply to them with a price").
        head = arg
        while head:
            cand = os.path.expanduser(head.strip().strip('"').strip("'"))
            if os.path.exists(cand):
                targets.append(cand)
                context = arg[len(head):].strip()
                break
            if " " not in head:
                break
            head = head.rsplit(" ", 1)[0]
        if not targets:
            context = arg
    if not targets:
        targets = [a["path"] for a in attachments
                   if os.path.splitext(a["path"])[1].lower() in
                   (".zip", ".rar") or os.path.isdir(a["path"])] or \
                  [a["path"] for a in attachments]
    if not targets:
        ui.warn("Point Prism at the job.\n"
                "  /gerber ~/Downloads/job_folder\n"
                "  /gerber ~/Downloads/gerbers.zip reply with our price\n"
                "  (or /attach the zip, then /gerber)")
        return

    try:
        paths = G.gather(targets)
    except G.GerberError as e:
        ui.err(str(e))
        return
    if not paths:
        ui.err("Nothing readable in there.")
        return

    # A folder can hold more than one board. Measuring five jobs as one
    # produces a single confident answer that describes none of them.
    groups = G.split_jobs(paths)
    if len(groups) > 1:
        ui.info(f"📦  {len(groups)} separate jobs in there — measuring each on "
                "its own:")
        for name, group in groups:
            ui.info(f"      {name}  ({len(group)} files)")

    ok = 0
    measured: list = []
    for name, group in groups:
        if len(groups) > 1:
            ui.rule(name, "teal")
        ui.info(f"📐  measuring {len(group)} file(s) — the design never leaves "
                "this machine; no AI sees a Gerber.")
        try:
            job = G.analyse(group, on_progress=lambda m: ui.info(f"   {m}"))
        except G.GerberError as e:
            ui.err(str(e))
            continue
        _show_gerber(job, name if len(groups) > 1 else "")
        measured.append((name, job))
        ok += 1
    if not ok:
        return
    # One sheet across every job — a row per board, in the column order and
    # the units the customer's own spreadsheet already uses.
    import time
    summary = os.path.join(C.RUNS_DIR, f"gerber_summary_{int(time.time())}.csv")
    G.write_summary_csv(measured, summary)
    ui.ok(f"One sheet for all {len(measured)} job(s) → {summary}")
    ui.info("   Opens in Excel. A row per board, plus layer identification "
            "for every file underneath.")

    if len(groups) > 1:
        ui.info("Each job above was measured separately. Add an instruction to "
                "write one of them up, or run /gerber on a single folder.")
        return

    if not context:
        ui.info("Add an instruction to have an agent write this up, e.g.\n"
                "  /gerber <path> reply to the customer with our price for 500 pieces")
        return

    brief = G.agent_brief(job, context)
    ui.info("🔒  Only the numbers above go to the agent. The Gerber files stay here.")
    run_query(cfg, brief, dry=False, attachments=[])


def cmd_reel(cfg, arg: str, attachments: list):
    """/reel <what the reel is about> — a finished vertical reel, rendered here.

    The agent writes the script; core.reel draws every frame and FFmpeg
    encodes it. Nothing is generated by a video model, so the output is
    always 9:16, the text is always legible, there is no watermark, no daily
    credit limit, and the same spec renders identically every time.
    """
    from core import reel

    request = arg.strip()
    if not request:
        ui.warn("Say what the reel is about.\n"
                "  /reel a reel describing what we sell at Raj Infotech\n"
                "Attach the client's logo, card or brochure first and the brand "
                "colours are read from it automatically.")
        return

    ok, why = reel_available()
    if not ok:
        ui.err(why)
        return

    # Brand colours come from the client's own artwork, measured — not from
    # an AI's impression of it, and not from the user typing hex codes.
    images = [a for a in attachments
              if a.get("kind") == "image" or
              a.get("path", "").lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    brand = reel.sample_brand([a["path"] for a in images]) if images else {}
    if brand:
        ui.ok(f"🎨  brand read from {', '.join(a['name'] for a in images)} — "
              f"accent {brand.get('accent')}, deep {brand.get('deep')}")
    else:
        ui.info("No artwork attached — using Prism's default palette. "
                "Attach a logo or card and the client's own colours are used.")

    agents = C.active_agents(cfg)
    writer = next((s for s in ("content", "brains", "media") if agents.get(s)), None)
    if not writer:
        ui.err("No content/brains agent configured — run /agents first.")
        return
    director = agents.get("brains") or agents[writer]

    # Two renderers, and the difference matters enough to say out loud: the
    # designed one gives every client a different-looking film, the template
    # one is faster and always identical. Designed is the default when the
    # browser engine is installed.
    from core import reel_web
    studio_ok, studio_why = reel_web.available()

    if studio_ok:
        ui.pipeline_plan([
            ("brand", "Prism (local)",
             "DONE — colours measured from the attached artwork, no AI involved"),
            ("script", agents[writer],
             "write the words and the running order — no design, no layout"),
            ("design", director,
             "art-direct it: background, type, palette and motion, as real CSS"),
            ("film", "Prism Studio (local)",
             "lay the page out at 1080x1920, check every line is inside the "
             "frame and big enough, then film it frame by frame"),
        ], title="Reel pipeline — designed")
    else:
        ui.warn(f"Designed reels need the browser engine — {studio_why}")
        ui.info("Falling back to the fixed house style for now.")
        ui.pipeline_plan([
            ("brand", "Prism (local)",
             "DONE — colours measured from the attached artwork, no AI involved"),
            ("script", agents[writer],
             "write the words and the running order — nothing else"),
            ("render", "Prism (local)",
             "draw every frame and encode with FFmpeg — 1080x1920, no watermark"),
        ], title="Reel pipeline — house style")

    if not _ask_yes_no("Run this?", default=True):
        ui.info("Cancelled.")
        return

    try:
        from core import automation
    except Exception as e:
        ui.err(f"Automation deps not available ({e}).")
        return

    if studio_ok:
        _reel_designed(cfg, request, brand, images, agents[writer], director)
        return

    prompt = reel.build_prompt(request, brand, bool(images))
    responses, links = automation.run(
        {}, cfg, attachments=images, chatgpt_analysis=False,
        custom_stages=[("script", agents[writer], [prompt])],
        query=f"write a reel script — {request}")

    texts = [t for t in (responses.get("script") or []) if t.strip()]
    if not texts:
        ui.err("The agent returned nothing.")
        if links.get("script"):
            ui.info(f"Its tab: {links['script']}")
        return

    # Newest capture that parses, not the first on the page — the tab is
    # reused between runs and also holds the prompt Prism typed, which carries
    # an example spec inside it.
    spec, why = reel.first_spec(texts)
    if spec is None:
        ui.err(str(why or "The agent returned nothing to render."))
        kept = reel.keep_unparsed(texts)
        if kept:
            ui.info(f"What came back was saved to {kept}")
        if links.get("script"):
            ui.info(f"Read what it said: {links['script']}")
        return

    if brand:
        spec["brand"] = brand
    if spec.get("_dropped"):
        ui.warn(f"skipped {len(spec['_dropped'])} scene(s) this renderer can't "
                f"draw: {', '.join(spec['_dropped'][:4])}")

    import time
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(time.time())
    out = os.path.join(C.RUNS_DIR, f"reel_{stamp}.mp4")
    spec_path = os.path.join(C.RUNS_DIR, f"reel_{stamp}.json")
    import json as _json
    _json.dump(spec, open(spec_path, "w"), indent=2)

    secs = sum(float(sc.get("seconds", 4)) for sc in spec["scenes"])
    ui.info(f"🎬  rendering {len(spec['scenes'])} scenes, {secs:.0f}s, 1080x1920…")
    try:
        reel.render(spec, out,
                    on_progress=lambda d, t: ui.info(f"   {d}/{t} frames")
                    if d % (t // 4 or 1) < 30 else None)
    except reel.ReelError as e:
        ui.err(str(e))
        return

    ui.ok(f"Reel ready → {out}")
    ui.info(f"   scene spec saved → {spec_path}  (edit it and re-render, no AI needed)")
    C.save_run({"query": f"/reel {request}", "responses": responses, "links": links,
                "reel": {"mp4": out, "spec": spec_path, "brand": brand}})


def _reel_designed(cfg, request, brand, images, writer_agent, director_agent):
    """/reel through Prism Studio: script, then art direction, then filmed.

    The split is on purpose. One reply asked for both the words and the look
    reliably produces a design that describes itself — "clean data card",
    "logo reveal" — rather than one that exists.

    Art direction is itself a conversation, not a reply: the look and a
    storyboard, then one turn per scene. Asked for every scene at once, a
    model spread a few thousand characters across all of them and each came
    out at about 278 characters, which is a headline and a subhead — a slide,
    with nothing in it to move. See reel_web.design_instructions().
    """
    import json as _json
    import time
    from core import automation, assets as _assets, reel_web

    # The client's own marks, cut out of what they sent. Never regenerated:
    # a model asked to redraw a logo produces a lookalike, and a lookalike on
    # a client's reel is worse than no logo at all.
    table = {}
    if images:
        try:
            table = _assets.collect(images)
            if table:
                ui.ok(f"✂️   {len(table)} asset(s) from the artwork: "
                      + ", ".join(table))
        except Exception as e:
            ui.warn(f"couldn't prepare the artwork ({e})")

    responses, links = automation.run(
        {}, cfg, attachments=images, chatgpt_analysis=False,
        custom_stages=[
            ("script", writer_agent,
             [f"Write the script for a short vertical brand reel.\n\n"
              f"WHAT THE CLIENT ASKED FOR:\n{request}\n\n"
              + reel_web.script_instructions()]),
            ("design", director_agent,
             [reel_web.design_instructions(brand, request,
                                           _assets.manifest(table))]),
        ],
        # The design stage is a conversation, not one reply: this turn is the
        # look and the storyboard, and the scenes are asked for one at a time
        # in the same tab. A routed run works this out from the renderer stage
        # in its plan; this one has no renderer stage, so it says so.
        reel_design_stage="design",
        query=f"design a reel — {request}")

    texts = [t for t in (responses.get("design") or []) if t.strip()]
    if not texts:
        ui.err("The art-direction stage returned nothing.")
        if links.get("design"):
            ui.info(f"Its tab: {links['design']}")
        return
    try:
        spec = reel_web.parse_spec(texts[-1])
    except reel_web.ReelError as e:
        ui.err(str(e))
        if links.get("design"):
            ui.info(f"Read what it said: {links['design']}")
        return
    if brand:
        spec["brand"] = brand
    if table:
        spec["_assets"] = table

    # Measured, not trusted: the page is laid out and every line checked
    # before a minute is spent filming it.
    ui.info("   📐  laying the design out at 1080x1920…")
    try:
        faults = reel_web.inspect(spec)
    except Exception as e:
        ui.warn(f"   couldn't check the layout ({e}) — filming anyway")
        faults = []
    for fault in faults[:6]:
        ui.warn(f"   layout: {fault}")

    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(time.time())
    out = os.path.join(C.RUNS_DIR, f"reel_{stamp}.mp4")
    spec_path = os.path.join(C.RUNS_DIR, f"reel_{stamp}.json")
    _json.dump(spec, open(spec_path, "w"), indent=2)

    name = (spec.get("design") or {}).get("name", "")
    if name:
        ui.ok(f"🎨  {name}")
    secs = sum(float(sc.get("seconds", 4) or 4) for sc in spec["scenes"])
    ui.info(f"🎬  filming {len(spec['scenes'])} scenes, ~{secs:.0f}s — this "
            "takes longer than the house style because every frame is a real "
            "browser paint.")
    try:
        reel_web.render(spec, out, check=False,
                        on_progress=lambda d, t: ui.info(f"   {d}/{t} frames")
                        if d % (t // 4 or 1) < 30 else None)
    except reel_web.ReelError as e:
        ui.err(str(e))
        return

    ui.ok(f"Reel ready → {out}")
    ui.info(f"   design saved → {spec_path}  (edit the CSS and re-render, "
            "no AI needed)")
    C.save_run({"query": f"/reel {request}", "responses": responses,
                "links": links,
                "reel": {"mp4": out, "spec": spec_path, "brand": brand,
                         "designed": True}})


def cmd_motion(cfg: dict, request: str, attachments: list | None = None):
    if not request.strip():
        ui.warn("Please specify what motion graphic you want to generate.")
        ui.info("Example: /motion Create an animated architecture diagram of our distributed crawler")
        return

    try:
        from core import motion
    except ImportError as e:
        ui.err(f"Motion Graphics Engine not installed: {e}")
        return

    ok, err = motion.is_available()
    if not ok:
        ui.err(f"Motion Graphics requirement missing: {err}")
        return

    from core.motion import generate as motion_generate
    from core import automation
    import json

    # Same measurement Reel/Studio use — a logo or business card among the
    # attachments gets the brand's real colours read off it, not guessed.
    brand: dict = {}
    if attachments:
        imgs = [a["path"] for a in attachments
                if a.get("path", "").lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
        if imgs:
            try:
                from core import reel as _pillow
                brand = _pillow.sample_brand(imgs) or {}
                if brand:
                    ui.info(f"🎨  brand colours read from the artwork — "
                            f"accent {brand.get('accent')}, "
                            f"deep {brand.get('deep')}")
            except Exception:
                pass

    prompt = motion_generate.storyboard_instructions(
        request, brand, skeleton="cinematic_glass")

    # ── Multi-agent fallback routing ───────────────────────────────────────────
    # Build a prioritized provider chain from the agents the user actually
    # configured, strongest first — the same "brains" > "content" preference
    # reel_web's director selection uses. "media" is excluded on purpose:
    # that category holds the local renderers (Prism Reel/Prism Studio),
    # which render video and cannot hold a text conversation. On empty
    # response or parse failure, cascade to the next provider.
    active = C.active_agents(cfg)
    preferred = cfg.get("motion_agent") or active.get("brains") or active.get("content")
    fallback_chain: list[str] = [preferred] if preferred else []
    for category, name in active.items():
        if category != "media" and name not in fallback_chain:
            fallback_chain.append(name)
    if not fallback_chain:
        ui.err("No agent is configured to write the motion plan — set one up first.")
        return

    spec = None
    used_agent = None
    last_error = ""

    for agent in fallback_chain:
        try:
            ui.rule(f"Visual Director ({agent})", "magenta")
            ui.info(f"Designing motion graphic: {request}")

            # motion_design_stage turns "storyboard" into the same
            # scene-at-a-time conversation reel_design_stage gives
            # core.reel_web: this one reply is turn one, and
            # core.motion.generate.build_spec() asks for every scene's
            # nodes on its own turn in the same tab before returning.
            # By the time run() returns, responses["storyboard"] already
            # holds the fully assembled, validated multi-scene spec — not
            # just turn one's raw reply.
            responses, links = automation.run(
                {}, cfg, attachments=attachments, chatgpt_analysis=False,
                custom_stages=[("storyboard", agent, [prompt])],
                motion_design_stage="storyboard",
                motion_skeleton="cinematic_glass",
                query=f"motion graphic — {request}")

            texts = [t for t in (responses.get("storyboard") or []) if t.strip()]
            if not texts:
                last_error = f"{agent}: returned empty response"
                ui.warn(f"   {last_error} — trying next provider…")
                continue

            spec = json.loads(texts[-1])
            used_agent = agent
            break

        except (ValueError, json.JSONDecodeError) as e:
            last_error = f"{agent}: could not parse motion spec — {e}"
            ui.warn(f"   {last_error} — trying next provider…")
            continue
        except Exception as e:
            last_error = f"{agent}: {e}"
            ui.warn(f"   {last_error} — trying next provider…")
            continue

    if spec is None:
        ui.err(f"All AI providers failed to produce a valid motion specification.")
        ui.err(f"Last error: {last_error}")
        return

    import time
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(time.time())
    out = os.path.join(C.RUNS_DIR, f"motion_{stamp}.mp4")
    spec_path = os.path.join(C.RUNS_DIR, f"motion_{stamp}.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)

    dur = float((spec.get("project") or {}).get("duration", 10.0))
    ui.info(f"rendering motion graphic ({dur:.1f}s, 1080x1920 @ 30fps) via {used_agent}…")

    _last_reported = [-1]
    def _progress(done: int, total: int):
        pct = int(done / total * 100) if total else 0
        bucket = pct // 10
        if bucket != _last_reported[0]:
            _last_reported[0] = bucket
            ui.info(f"   frame {done}/{total} ({pct}%)")

    try:
        motion.render(spec, out, on_progress=_progress)
    except Exception as e:
        ui.err(f"Motion rendering failed: {e}")
        return

    ui.ok(f"Motion Graphic ready → {out}")
    ui.info(f"   specification saved → {spec_path}")
    C.save_run({"query": f"/motion {request}", "responses": responses, "links": links,
                "motion": {"mp4": out, "spec": spec_path}})


def reel_available() -> tuple[bool, str]:
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return False, "Pillow is needed to draw the frames:  pip install pillow"
    try:
        from core import reel
        reel.ffmpeg_path()
    except Exception as e:
        return False, str(e)
    return True, ""


def _handle_firstrun(cfg, choice):
    if choice == "skip":
        return
    profile = cfg.get("profile", "").strip()
    sample = (f"Give me a quick starter task idea for someone whose work is: {profile}. "
              "Then outline how you'd approach it.") if profile else \
        "Write a short haiku about prisms and refracted light."
    ui.rule("First run", "orange")
    ui.info(f"Sample task: {sample}")
    run_query(cfg, sample, dry=(choice == "dry"))


# ── REPL ──────────────────────────────────────────────────────────────────────

def repl(cfg):
    ui.info("Type [bold]/help[/bold] for commands, or just describe a task. Ctrl-C to quit.\n")
    attachments = []          # ride along with the next task until /detach
    cmd_status(cfg)
    while True:
        try:
            raw, spoken = _get_input(cfg, attachments)
        except (EOFError, KeyboardInterrupt):
            ui.info("\nbye ◈")
            return
        if not raw.strip():
            continue
        line = raw.strip()

        # One command must never be able to end the session. Before this, an
        # unexpected error anywhere inside a command propagated out of the
        # loop and quit Prism outright, taking the attachment list and all the
        # context with it — the user's punishment for a bug being that they
        # had to set the whole task up again.
        try:
            cfg, keep_going = _dispatch(cfg, line, attachments)
            if not keep_going:
                return
        except KeyboardInterrupt:
            # Ctrl-C interrupts the COMMAND, not the session. Coming back to
            # the prompt with the attachments intact is the point of a REPL.
            ui.warn("\nStopped. Back at the prompt — your attachments are "
                    "still here.")
        except Exception as e:
            ui.err(f"That command hit an unexpected problem: {e}")
            ui.info("Nothing else was changed. Try again, or /help for the "
                    "list of commands.")


def _dispatch(cfg: dict, line: str, attachments: list) -> tuple[dict, bool]:
    """Run one command line.

    Returns (cfg, keep_going). cfg comes back because /config replaces it
    wholesale, and the REPL has to keep the new one.
    """
    if line in ("/exit", "/quit", "/q"):
        ui.info("bye ◈")
        return cfg, False
    elif line in ("/help", "/?", "help"):
        ui.panel(HELP.strip(), title="Prism", style="teal")
    elif line == "/status":
        cmd_status(cfg)
        if attachments:
            ui.info(f"📎  {len(attachments)} file(s) attached to your next task.")
    elif line == "/catalog":
        ui.catalog_table()
    elif line == "/agents":
        cmd_agents(cfg)
    elif line == "/profile":
        cmd_profile(cfg)
    elif line == "/key":
        cmd_key(cfg)
    elif line == "/chrome":
        cmd_chrome(cfg)
    elif line == "/login":
        cmd_login(cfg)
    elif line == "/config":
        cfg = cmd_config(cfg)
    elif line.startswith("/attach"):
        cmd_attach(line[len("/attach"):].strip(), attachments, cfg)
    elif line.startswith("/find"):
        cmd_find(line[len("/find"):].strip(), cfg, attachments)
    elif line == "/files":
        cmd_files(attachments)
    elif line == "/detach":
        cmd_detach(attachments)
    elif line == "/runs":
        cmd_runs(cfg)
    elif line == "/skills":
        cmd_skills()
    elif line.startswith("/remote"):
        cmd_remote(cfg, line[len("/remote"):].strip())
    elif line.startswith("/email"):
        cmd_email(cfg, line[len("/email"):].strip(), attachments)
    elif line.startswith("/boq"):
        cmd_boq(cfg, line[len("/boq"):].strip(), attachments)
    elif line.startswith("/step-folder") or line.startswith("/stepfolder"):
        cmd_step_folder(cfg, line.split(" ", 1)[1] if " " in line else "")
    elif line.startswith("/step-ask") or line.startswith("/stepask"):
        cmd_step_ask(cfg, line.split(" ", 1)[1] if " " in line else "",
                     attachments)
    elif line.startswith("/step-auto") or line.startswith("/stepauto"):
        cmd_step_auto(cfg, line.split(" ", 1)[1] if " " in line else "",
                      attachments)
    elif line.startswith("/step") or line == "/s" or line.startswith("/s "):
        cmd_step(cfg, line.split(" ", 1)[1] if " " in line else "", attachments)
    elif line.startswith("/gerber"):
        cmd_gerber(cfg, line[len("/gerber"):].strip(), attachments)
    elif line.startswith("/reel"):
        cmd_reel(cfg, line[len("/reel"):].strip(), attachments)
    elif line.startswith("/motion"):
        cmd_motion(cfg, line[len("/motion"):].strip(), attachments)
    elif line.startswith("/dry"):
        run_query(cfg, line[4:].strip(), dry=True, attachments=attachments)
    elif line.startswith("/"):
        ui.warn(f"Unknown command: {line}. Try /help.")
    else:
        run_query(cfg, line, dry=False, attachments=attachments)
    return cfg, True


def _drain_pending_lines() -> str:
    """Read any lines already sitting in the terminal's input buffer, without
    blocking. A hard-wrapped multi-line paste dumps several newline-separated
    chunks into the tty at once; a single input() call only returns the FIRST
    one, and the rest used to sit there silently and get read as the answer
    to whatever prompt came next (e.g. the Y/n run-confirmation) — corrupting
    the query AND auto-answering the confirm with no chance to respond.
    POSIX only (select on stdin); a no-op on Windows."""
    if os.name == "nt":
        return ""
    import select
    extra = []
    try:
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if not r:
                break
            line = sys.stdin.readline()
            if not line:
                break
            extra.append(line.strip())
    except Exception:
        pass
    return " ".join(x for x in extra if x)


def _flush_stdin_noise() -> None:
    """Discard (don't merge) any stray buffered input right before a Y/n-style
    prompt, so a leftover paste fragment can never silently answer it."""
    _drain_pending_lines()


def _strip_paste_markers(text: str) -> str:
    """Remove bracketed-paste escape sequences from assembled input.

    A terminal with bracketed paste on (macOS Terminal, iTerm, most Linux
    terminals) wraps every paste in ESC[200~ … ESC[201~. The speak/type gate
    reads its first character raw, so the marker's ESC arrived as "the first
    typed character": the line no longer started with "/", a pasted
    /step-ask command fell past the dispatcher into the task router, and the
    interpreter hijacked the file path out of it — while the prompt echoed
    an invisible escape code. Seen live; this strips the markers (and any
    stray ESC) wherever input is put together."""
    return (text.replace("\x1b[200~", "").replace("\x1b[201~", "")
            .replace("\x1b", ""))


def _ask_yes_no(msg: str, default: bool = True) -> bool:
    """A Y/n prompt that will NOT accept leftover buffered text as an answer.

    The plain-input fallback in onboarding._ask_confirm treats anything not
    starting with 'y' as "no" — so a stray line still sitting in the tty
    (the tail of a hard-wrapped paste, say) silently answers the question
    before the user's real keypress is ever read, and the run is cancelled
    with no explanation. Confirmed cause of "/boq said Y but printed 'Not
    formatted'": the confirm consumed the second line of a pasted command.

    Here anything that isn't clearly yes/no is REJECTED and re-asked, and
    the ignored text is shown, so a mis-read can never masquerade as a
    deliberate 'no'."""
    from core.onboarding import _ask_confirm as _q_confirm, _Q
    if _Q:
        # The widget reads the tty raw, so a paste remnant still sitting in
        # the buffer becomes its keystrokes — drop it first or a stray 'n'
        # answers the question before the user sees it.
        _drain_pending_lines()
        return _q_confirm(msg, default=default)
    for _ in range(3):
        _drain_pending_lines()                    # drop stale buffered input
        try:
            raw = _strip_paste_markers(
                input(f"{msg} [{'Y/n' if default else 'y/N'}] ")).strip()
        except EOFError:
            return default
        low = raw.lower()
        if not low:
            return default
        if low in ("y", "yes"):
            return True
        if low in ("n", "no"):
            return False
        ui.warn(f'ignoring unexpected input ("{_esc(raw[:60])}") — please answer y or n')
    ui.warn("no clear answer after 3 tries — assuming "
            f"{'yes' if default else 'no'}")
    return default


def _prompt(text: str) -> str:
    # Discard any stray buffered input BEFORE showing this prompt — without
    # this, a leftover keystroke from an EARLIER answer (or a paste's
    # overflow) can silently satisfy this NEW question before the user ever
    # sees it rendered. This was the actual cause of "the folder-correction
    # prompt didn't even show up" — it fired and returned instantly against
    # stale buffered text, not against a real keypress.
    #
    # Plain input(), deliberately NOT the questionary widget. A pasted
    # command with a newline in it (any copy of wrapped terminal text has
    # one) submits at the first newline in either reader — but the widget
    # runs the tty raw and swallows the REMAINING lines into its own
    # buffer, where the select()-based drain below cannot see them. They
    # then surfaced as keystrokes inside the NEXT widget: a /step-ask run
    # lost half its question AND had its Y/n confirm answered "No" by the
    # first stray 'n' in the leftovers. With canonical input() the
    # remainder stays in the tty buffer, and the drain merges it back into
    # one command — the whole paste survives.
    _drain_pending_lines()
    try:
        line = input(text)
    except EOFError:
        line = ""
    extra = _drain_pending_lines()
    return _strip_paste_markers(
        f"{line} {extra}".strip() if extra else line)


def _confirm_task(task: str) -> str:
    """Show the final task text before it goes to routing and let the user
    confirm or correct it — catches STT mishears and any over-eager cleanup,
    spoken or typed. Typing the fix directly (instead of 'n' first, then
    re-typing) also works. Empty string means the user gave up on this take."""
    ui.say(f'  📝  Prism will route: "{_esc(task)}"')
    ans = _prompt("correct? (Y/n, or type a correction) ").strip()
    low = ans.lower()
    if low in ("", "y", "yes"):
        return task
    if low in ("n", "no"):
        return _prompt("type the corrected task: ").strip()
    return ans   # they typed the fix directly instead of answering y/n first


def _maybe_extract_files(raw: str, cfg, attachments) -> str:
    """Typed queries can mention files/folders too ("grab the delta prototype
    from the desktop and …") — run them through the SAME interpreter used for
    speech so those get located & attached either way, not only when spoken.
    Slash-commands are left completely untouched so /attach, /find, /dry etc.
    still reach the REPL dispatcher verbatim. Only asks for confirmation when
    the interpreter actually changed something — a plain typed task that
    mentioned no files goes straight through, no extra prompt."""
    text = raw.strip()
    if not text or text.startswith("/"):
        return raw
    from core import voice
    intent = voice.interpret(text, cfg)
    if not intent.get("ok", True):
        return raw   # degraded — leave the typed text exactly as given
    for desc in intent["files"]:
        ui.info(f"📎  you mentioned a file: {_esc(desc)}")
        cmd_find(desc, cfg, attachments)
    if not intent["files"]:
        return raw   # nothing to extract — don't risk rewording a typed task
    task = intent["task"].strip()
    if not task:
        return ""   # only a file/folder op was requested — nothing left to route
    return _confirm_task(task)


def _get_input(cfg, attachments) -> tuple[str, bool]:
    """The speak/type gate. Returns (line, was_spoken).
    SPACE at the prompt records a voice take (SPACE again stops it); any other
    key (t, Enter, …) opens the normal typed prompt. Every take — spoken OR
    typed — goes through the interpreter: the transcript is polished
    (Wispr-Flow style), file/folder references are located & attached however
    they were phrased, and the remaining task is returned for routing.
    Without pyaudio or a real terminal it's typed-only, as before."""
    from core import voice
    if not voice.available():
        return _maybe_extract_files(_prompt("prism › "), cfg, attachments), False
    _drain_pending_lines()   # same stale-buffer risk as _prompt() — clear it first
    ch = voice.choose("prism ›  [space] speak · [t] type ")
    if ch != " ":
        # 't' / Enter open a fresh typed prompt. Any OTHER key is treated as
        # the FIRST CHARACTER of typed input — swallowing it would corrupt
        # fast typing and pastes ('/dry …' silently becoming 'dry …').
        if ch in ("t", "T", "\n", "\r"):
            return _maybe_extract_files(_prompt("prism › "), cfg, attachments), False
        try:
            sys.stdout.write(f"prism › {ch}")
            sys.stdout.flush()
            rest = input()
        except EOFError:
            return "", False
        extra = _drain_pending_lines()
        full = _strip_paste_markers(
            f"{ch}{rest} {extra}" if extra else ch + rest).strip()
        return _maybe_extract_files(full, cfg, attachments), False

    ui.info("🎤  recording — press SPACE again when you're done")
    try:
        text, lang = voice.record_and_transcribe(cfg)
    except Exception as e:
        ui.err(f"voice failed ({e}) — type instead")
        return _maybe_extract_files(_prompt("prism › "), cfg, attachments), False
    if not text:
        ui.warn("didn't catch anything — try again, or type")
        return "", False
    lang_note = f"  [dim]({_esc(lang)})[/dim]" if lang and lang != "english" else ""
    ui.info(f'🎤  heard: "{_esc(text)}"{lang_note}')

    intent = voice.interpret(text, cfg)
    if not intent.get("ok", True):
        ui.warn("interpreter unavailable — routing your words as-is; any file "
                "you mentioned was NOT auto-attached (use /find to attach it)")
    if intent["cleaned"] and intent["cleaned"] != text:
        ui.ok(f'✨  understood: "{_esc(intent["cleaned"])}"')
    for desc in intent["files"]:
        ui.info(f"📎  you mentioned a file: {_esc(desc)}")
        cmd_find(desc, cfg, attachments)
    task = intent["task"].strip()
    if not task:
        return "", False   # only file ops this take — back to the prompt
    task = _confirm_task(task)
    if not task:
        return "", False
    return task, True


# ── entry ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    cfg = C.load()

    if "--config" in args or "-c" in args:
        cfg = onboarding.run(cfg)
        fr = cfg.pop("_firstrun", "skip")
        C.save(cfg)
        _handle_firstrun(cfg, fr)
        return

    # First-time users always go through onboarding.
    if not C.is_configured(cfg):
        cfg = onboarding.run(cfg)
        fr = cfg.pop("_firstrun", "skip")
        C.save(cfg)
        _handle_firstrun(cfg, fr)
        # then drop into the REPL
        cfg = C.load()

    dry = "--dry" in args or "-d" in args

    # Collect --file <path> (repeatable) and strip them from the task words.
    from core import files as F
    attachments = []
    task_words = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--file", "-f") and i + 1 < len(args):
            try:
                attachments.append(F.attach(args[i + 1]))
            except Exception as e:
                ui.err(f"Could not attach {args[i + 1]}: {e}")
            i += 2
            continue
        if not a.startswith("-"):
            task_words.append(a)
        i += 1
    task = " ".join(task_words).strip()

    if task:
        run_query(cfg, task, dry=dry, attachments=attachments)
        return

    ui.banner()
    repl(cfg)


if __name__ == "__main__":
    main()
