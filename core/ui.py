"""
Prism — terminal UI helpers (rich-powered, Claude-Code-flavoured)
────────────────────────────────────────────────────────────────
All colour, banners, panels and tables live here so the rest of the app stays
free of formatting noise. Degrades to plain print() if `rich` is missing.
"""
from __future__ import annotations

try:
    from rich.console import Console
    from rich.theme import Theme
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.align import Align
    from rich.rule import Rule
    _RICH = True
except Exception:  # pragma: no cover
    _RICH = False

# ── Prism brand palette (mirrors the desktop dashboard) ───────────────────────
_THEME = {
    "pink": "#EF4B77",
    "orange": "#FF8A4B",
    "teal": "#4CD9B4",
    "blue": "#4C9AFF",
    "dim": "#777777",
    "brand": "bold #EF4B77",
    "brains": "#EF4B77",
    "research": "#4C9AFF",
    "leads": "#4BE08A",
    "content": "#FF8A4B",
    "visual": "#4CD9B4",
    "media": "#B47BFF",
    "audio": "#7BE0FF",
    "development": "#FFD24B",
    "presentation": "#8C9EFF",
    "ok": "bold #4CD9B4",
    "warn": "bold #FF8A4B",
    "err": "bold #EF4B77",
}

console = Console(theme=Theme(_THEME)) if _RICH else None

# ── the sink ─────────────────────────────────────────────────────────────────
# Everything the engine has to say about a run — which tool it is on, what it
# is waiting for, that it read the brand colours off the artwork, that it could
# not prepare a logo — is written through this module. In the terminal that
# lands on screen. In the desktop app it landed on a stdout that a windowed
# build does not have, so a GUI user saw a spinner and nothing else for minutes
# at a time while the CLI user running the identical code saw the whole story.
#
# A sink is a callable taking (level, text) with the markup already stripped.
# set_sink(None) puts it back. The console output is unaffected either way, so
# nothing about the terminal changes.
_sink = None


def set_sink(fn) -> None:
    """Mirror every message to `fn(level, text)` as well as the console."""
    global _sink
    _sink = fn


def _strip_markup(msg: str) -> str:
    """'[bold]Claude[/bold] is thinking' → 'Claude is thinking'.

    A sink is feeding a Qt label, not a terminal — rich's tags would show up
    verbatim. Uses rich's own parser when it is available so the two can never
    disagree about what counts as a tag.
    """
    if _RICH:
        try:
            from rich.text import Text as _Text
            return _Text.from_markup(msg).plain
        except Exception:
            pass
    # No rich -- every packaged build, because rich is in this engine's
    # requirements and not the app's. A bracket literal() escaped is text,
    # not a tag: keep it, and take the escape off.
    import re
    return _unescape(re.sub(r"(?<!\\)\[/?[a-zA-Z0-9 #_.-]*\]", "", msg))


def _unescape(msg: str) -> str:
    """'\\[slide-deck]' -> '[slide-deck]': literal()'s escape, taken off."""
    return msg.replace("\\[", "[")


_rewriter = None


def set_rewriter(fn) -> None:
    """Pass every message through `fn(text) -> text` before it is shown or
    handed to the sink (the GUI uses it to drop browser-era words while AI
    runs on Prism's server). None removes it."""
    global _rewriter
    _rewriter = fn


def _rewrite(msg):
    if _rewriter is None:
        return msg
    try:
        return _rewriter(str(msg))
    except Exception:
        return msg


def _tell(level: str, msg: str) -> None:
    """Hand one message to the sink, if there is one. Never raises: a broken
    listener must not take down the run it is only watching."""
    if _sink is None:
        return
    try:
        _sink(level, _strip_markup(str(msg)).strip())
    except Exception:
        pass


def _plain(msg: str):
    print(_unescape(msg))   # literal()'s escape is for the sink, not the reader


def rule(label: str = "", style: str = "pink"):
    _tell("rule", label)
    if _RICH:
        console.print(Rule(label, style=style))
    else:
        _plain(f"── {label} " + "─" * max(0, 40 - len(label)))


def say(msg: str, style: str = "", _level: str = "info"):
    # ok/warn/err/info all route through here, and each passes its own level
    # so a listener can tell a warning from a progress note without parsing
    # the emoji back out of the text.
    msg = _rewrite(msg)
    _tell(_level, msg)
    if _RICH:
        console.print(msg, style=style)
    else:
        _plain(msg)


def ok(msg: str):
    say(f"✅  {msg}", "ok", _level="ok")


def warn(msg: str):
    say(f"⚠️  {msg}", "warn", _level="warn")


def err(msg: str):
    say(f"❌  {msg}", "err", _level="err")


def info(msg: str):
    say(f"[dim]{msg}[/dim]" if _RICH else msg, _level="info")


def literal(text) -> str:
    """Text that must print exactly as written.

    rich reads anything in square brackets as a style tag and drops it --
    on the console, and in the GUI's log too, because _strip_markup parses
    with rich. A skill fault reads "[slide-deck] Placeholder text left in:
    [client name]", and printed raw both the skill's name and the
    placeholder it is complaining about vanished. Wrap any text that came
    from somewhere else -- a checker, a model, a file -- in this before it
    goes into an f-string for say/info/warn.
    """
    s = str(text)
    if _RICH:
        try:
            from rich.markup import escape as _escape
            return _escape(s)
        except Exception:
            pass
    # Without rich, _strip_markup's fallback drops "[slide-deck]" just as
    # rich would, so escape it either way; the fallback and _plain undo it.
    return s.replace("[", "\\[")


def banner():
    """The big Prism splash."""
    art = r"""
    ██████╗ ██████╗ ██╗███████╗███╗   ███╗
    ██╔══██╗██╔══██╗██║██╔════╝████╗ ████║
    ██████╔╝██████╔╝██║███████╗██╔████╔██║
    ██╔═══╝ ██╔══██╗██║╚════██║██║╚██╔╝██║
    ██║     ██║  ██║██║███████║██║ ╚═╝ ██║
    ╚═╝     ╚═╝  ╚═╝╚═╝╚══════╝╚═╝     ╚═╝"""
    if _RICH:
        text = Text(art, style="bold #EF4B77")
        console.print(text)
        console.print(
            Align.center(
                Text("◈  one prompt → many specialist AIs, in sequence  ◈",
                     style="italic #4CD9B4")
            )
        )
        console.print()
    else:
        _plain(art)
        _plain("  one prompt -> many specialist AIs, in sequence")


def panel(body: str, title: str = "", style: str = "pink"):
    _tell("panel", f"{title}\n{body}" if title else body)
    if _RICH:
        console.print(Panel(body, title=title, border_style=style, padding=(1, 2)))
    else:
        if title:
            _plain(f"=== {title} ===")
        _plain(body)


def status_header(cfg: dict, active: dict):
    """Compact one-glance status shown at the top of the REPL."""
    from . import agents as A
    if _RICH:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim", justify="right")
        t.add_column()
        who = cfg.get("profile") or "—"
        t.add_row("you", who if len(who) < 70 else who[:67] + "…")
        key = cfg.get("api_key", "")
        t.add_row("groq key", (key[:7] + "…" + key[-4:]) if len(key) > 12 else "not set")
        cv = cfg.get("chrome_version", "")
        t.add_row("chrome", f"pinned v{cv}" if cv else "auto-detect")
        if active:
            for cat, name in active.items():
                meta = A.CATEGORIES.get(cat, {})
                emoji = meta.get("emoji", "•")
                color = meta.get("color", "pink")
                t.add_row(f"{emoji} {cat}", f"[{color}]{name}[/{color}]")
        else:
            t.add_row("agents", "[warn]none selected[/warn]")
        console.print(Panel(t, title="Prism", border_style="pink", padding=(1, 2)))
    else:
        _plain(f"Prism — {cfg.get('profile','')}")
        for cat, name in active.items():
            _plain(f"  {cat}: {name}")


def catalog_table():
    """The full tool catalogue, grouped by category (like the source CSV)."""
    from . import agents as A
    if not _RICH:
        for cat, meta in A.CATEGORIES.items():
            _plain(f"\n{meta['emoji']} {meta['label']}")
            for a in meta["agents"]:
                cfg = A.AGENT_REGISTRY[a]
                _plain(f"   {a:<16} {cfg['cost']:<12} {cfg['avg']}")
        return
    for cat, meta in A.CATEGORIES.items():
        t = Table(
            title=f"{meta['emoji']}  {meta['label']}  —  [dim]{meta['desc']}[/dim]",
            title_justify="left", border_style=meta["color"], expand=True,
            title_style=f"bold {meta['color']}",
        )
        t.add_column("Tool", style="bold", no_wrap=True)
        t.add_column("What it's picked for")
        t.add_column("Cost", no_wrap=True)
        t.add_column("Speed", no_wrap=True)
        for a in meta["agents"]:
            c = A.AGENT_REGISTRY[a]
            t.add_row(a, c["specialty"], c["cost"], c["avg"])
        console.print(t)
        console.print()


def agent_pick_table(stage: str, current: str, suggested: str | None = None):
    """One category's full tool list as a numbered picker — current pick
    marked green (●), the router's suggested alternative starred (★). Used
    right before letting the user type a number to switch, or skip."""
    from . import agents as A
    meta = A.CATEGORIES.get(stage, {})
    names = meta.get("agents", [])
    if not _RICH:
        for i, n in enumerate(names, 1):
            tag = " (current)" if n == current else (" (suggested)" if n == suggested else "")
            _plain(f"  {i}. {n}{tag}")
        return
    color = meta.get("color", "teal")
    t = Table(
        title=f"{meta.get('emoji','')}  {stage.upper()}  —  [dim]{meta.get('desc','')}[/dim]",
        title_justify="left", border_style=color, expand=True,
    )
    t.add_column("#", justify="right", no_wrap=True, width=3)
    t.add_column("Tool", no_wrap=True)
    t.add_column("What it's picked for")
    t.add_column("Cost", no_wrap=True)
    for i, n in enumerate(names, 1):
        c = A.AGENT_REGISTRY.get(n, {})
        if n == current:
            label = f"[ok]●[/ok] {n}"
        elif n == suggested:
            label = f"[bold {color}]★[/bold {color}] {n}"
        else:
            label = n
        t.add_row(str(i), label, c.get("specialty", ""), c.get("cost", ""))
    console.print(t)
    console.print("   [ok]●[/ok] your current default    [bold]★[/bold] router's suggestion",
                  style="dim")


def pipeline_plan(stages: list[tuple[str, str, str]], title: str = "pipeline"):
    """Render an EXPLICIT stage list — (stage, agent, what it does) — the way
    routing_plan renders the router's own choices.

    routing_plan can only draw the fixed PIPELINE_ORDER categories, so a
    purpose-built pipeline (/boq's measure → interpret → write, and the
    task-shaped pipelines in FUTURE_PLAN Phase 6) had no way to show the user
    which tools were about to run. Same table, arbitrary stages."""
    if _RICH:
        t = Table(title=f"🧠  {title}", border_style="teal",
                  title_style="bold teal", expand=True)
        t.add_column("#", justify="right", style="dim")
        t.add_column("Stage", style="bold")
        t.add_column("Agent")
        t.add_column("What it does")
        for i, (stage, agent, purpose) in enumerate(stages, 1):
            t.add_row(str(i), stage, agent or "[dim]—[/dim]", purpose)
        console.print(t)
    else:
        _plain(f"-- {title} --")
        for i, (stage, agent, purpose) in enumerate(stages, 1):
            _plain(f"{i}. {stage}: {agent} — {purpose}")


def routing_plan(routing: dict, agents: dict):
    """Render the router's plan as a table before execution."""
    from . import agents as A
    if _RICH:
        t = Table(title="🧠  Groq routing plan", border_style="teal",
                  title_style="bold teal", expand=True)
        t.add_column("Stage", style="bold")
        t.add_column("Agent")
        t.add_column("Run?", justify="center")
        t.add_column("Prompts")
        for stage in A.PIPELINE_ORDER:
            data = routing.get(stage)
            if not data:
                continue
            name = agents.get(stage) or (A.summary_agent_name(agents) if stage == "summary" else None)
            needed = data.get("needed", False) and bool(data.get("questions"))
            mark = "[ok]● yes[/ok]" if needed else "[dim]○ skip[/dim]"
            qs = data.get("questions", [])
            preview = "\n".join(f"• {q[:80]}" for q in qs) if qs else "[dim]—[/dim]"
            t.add_row(stage, name or "[dim]—[/dim]", mark, preview)
        console.print(t)
    else:
        for stage in A.PIPELINE_ORDER:
            data = routing.get(stage)
            if data:
                _plain(f"{stage}: needed={data.get('needed')} qs={len(data.get('questions',[]))}")
