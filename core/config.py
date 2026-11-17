"""
Prism — persistent configuration
────────────────────────────────
Everything the user sets during onboarding lives in ~/.prism/config.json so it
survives across runs and is independent of the current working directory.
Once written, Prism never asks for these again (unless the user edits them).
"""
import os
import json

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".prism")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUNS_DIR = os.path.join(CONFIG_DIR, "runs")
# Where a generated file goes to be found again. RUNS_DIR is hidden on macOS
# and easy to walk past on Windows/Linux, and anything only harvested
# mid-pipeline used to land in the OS temp directory — gone the moment Prism,
# or the OS, cleans up. A rendered video, a generated image, a written
# document: all of it belongs somewhere a customer looks without being told
# where — same idea as gerber_dialog.py's own "Prism Gerber" folder on the
# Desktop, generalised past just Gerber's CSVs.


def _desktop_dir() -> str:
    """The folder the person sees as their Desktop.

    `~/Desktop` is right on Linux and macOS. On Windows it is right only
    until OneDrive's "Known Folder Move" redirects the Desktop to
    `%USERPROFILE%\\OneDrive\\Desktop` -- the default on a fresh Windows 11
    sign-in -- after which `C:\\Users\\x\\Desktop` may not exist at all.
    Prism then created its own, invisible one and every "saved to the
    Desktop" landed where nobody looks. The shell keeps the real answer in
    the registry; ask it, and fall back to the plain path.
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
            raw, _ = winreg.QueryValueEx(key, "Desktop")
            path = os.path.expandvars(raw)
            if path and os.path.isdir(path):
                return path
        except Exception:                                   # noqa: BLE001
            pass
    return os.path.join(home, "Desktop")


ARTIFACTS_DIR = os.path.join(_desktop_dir(), "Prism Artifacts")

# Where artifacts go when the Desktop folder cannot be written. macOS asks
# the person's permission the first time an app touches ~/Desktop, and a
# packaged Prism that was told "Don't Allow" got a PermissionError on every
# save -- swallowed as best-effort, so the run said "saved" in the log and
# the Artifacts screen stayed empty. The home folder itself needs no
# permission and is still somewhere a person can find.
FALLBACK_ARTIFACTS_DIR = os.path.join(os.path.expanduser("~"), "Prism Artifacts")
_root_cache: dict = {"for": None, "root": None}


def artifacts_root() -> str:
    """The Artifacts folder that can actually be written on this machine:
    ARTIFACTS_DIR when it can be created and written, otherwise
    FALLBACK_ARTIFACTS_DIR -- decided once per process and said once."""
    wanted = ARTIFACTS_DIR
    if _root_cache["for"] == wanted and _root_cache["root"]:
        return _root_cache["root"]
    root = wanted
    try:
        os.makedirs(wanted, exist_ok=True)
        probe = os.path.join(wanted, ".prism-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        root = FALLBACK_ARTIFACTS_DIR
        os.makedirs(root, exist_ok=True)
        try:
            from . import ui
            ui.warn(f"couldn't write to {wanted} ({e}); generated files go "
                    f"to {root} instead")
        except Exception:                                   # noqa: BLE001
            pass
    _root_cache.update({"for": wanted, "root": root})
    return root

DEFAULT = {
    "api_key": "",        # Groq key (gsk_...)
    "profile": "",        # free-text "what do you do" — steers routing
    "agents": {},         # {category: agent_name} — only categories the user enabled
    "chrome_version": "", # pinned Chrome major version; "" = auto-detect
    "onboarded": False,
    "model": "llama-3.3-70b-versatile",
    # Where /step puts a model's files. "" = ~/Desktop/Prism Step. The GUI
    # asks the person where their STEP work should live and stores it here;
    # the terminal sets it with /step-folder. See core.stepfile.output_dir.
    "step_out_dir": "",
}


def _through_server(cfg: dict) -> dict:
    """When AI runs through Prism's server (core.ai_client), the two provider
    key fields read as the stand-in value, so "is a key set?" checks pass and
    the real keys on disk are never handed to anything. save() puts the real
    values back, so this never reaches the file."""
    from . import ai_client
    if ai_client.active():
        cfg = dict(cfg)
        for k in ai_client.KEY_FIELDS:
            cfg[k] = ai_client.AI_KEY
    return cfg


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _through_server({**DEFAULT, **data})
        except Exception:
            # An unreadable config used to fall through to DEFAULT in silence,
            # which looks exactly like a fresh install: the user is asked to
            # onboard again and concludes Prism "forgot" their key. Keep the
            # bad file — it still contains their API key and agent choices,
            # recoverable by hand — and leave a trail saying so.
            _quarantine(CONFIG_PATH)
    return _through_server(dict(DEFAULT))


def _quarantine(path: str) -> str:
    """Move a file that failed to parse aside, and say where it went."""
    import time
    kept = f"{path}.corrupt-{int(time.time())}"
    try:
        os.replace(path, kept)
    except OSError:
        return ""
    print(f"⚠️  {os.path.basename(path)} could not be read and was kept as "
          f"{kept}\n    Starting from defaults — your settings are still in "
          f"that file if you need them back.")
    return kept


def save(cfg: dict) -> None:
    """Write the config so that an interrupted write cannot destroy it.

    The old version truncated config.json and then wrote into it. A crash,
    a full disk or a pulled power cable anywhere in between left a half-written
    file, load() silently discarded it, and the user's API key, profile and
    agent choices were gone — from a routine settings save. Writing a complete
    temporary file first and swapping it in with os.replace (atomic on POSIX
    and on Windows) means the config is only ever the old one or the new one.
    """
    # STALE-COPY GUARD. The GUI keeps a cfg dict in memory and hands copies to
    # dialogs that live a while; a dialog saving its OWN older copy back would
    # blank fields the user set meanwhile — most painfully the Groq key, the
    # onboarded flag and the agent map, which reads as "why do I have to set
    # Prism up AGAIN every launch". So before writing, any of those three this
    # cfg would clear but that are still populated on disk are kept from disk.
    # A real new value still overrides; only an accidental blanking is refused.
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                _disk = json.load(_f)
            cfg = dict(cfg)
            for _k in ("api_key", "onboarded", "agents"):
                if not cfg.get(_k) and _disk.get(_k):
                    cfg[_k] = _disk[_k]
    except Exception:
        pass        # an unreadable disk copy must not stop a legitimate save
    # The stand-in key (see _through_server) is never written: whatever the
    # file held for that field stays, and a field it never held stays absent.
    from . import ai_client as _ai
    if any(cfg.get(_k) == _ai.AI_KEY for _k in _ai.KEY_FIELDS):
        cfg = dict(cfg)
        _held = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                _held = json.load(_f)
        except Exception:
            pass
        for _k in _ai.KEY_FIELDS:
            if cfg.get(_k) == _ai.AI_KEY:
                if _held.get(_k):
                    cfg[_k] = _held[_k]
                else:
                    cfg.pop(_k, None)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = f"{CONFIG_PATH}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            # The rename is atomic, but only orders against data that has
            # actually reached the disk — without this the swap can land while
            # the contents are still buffered, and a power loss then leaves an
            # empty file where the config used to be.
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)  # key is sensitive — owner-only, before it lands
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("api_key")) and bool(cfg.get("onboarded"))


def active_agents(cfg: dict) -> dict:
    """Categories the user actually assigned an agent to."""
    return {k: v for k, v in (cfg.get("agents") or {}).items() if v}


def save_run(record: dict, runs_dir: str = "", path: str = "") -> str:
    """Persist one query's routing + responses to <runs_dir>/run_<ts>.json.

    `runs_dir` defaults to ~/.prism/runs, which is where the CLI has always
    written and still does. The GUI passes a per-member folder instead when
    the copy belongs to a company team, so one person's history does not land
    in another's — see prism_gui/workspace.py. If `path` is specified, updates
    that exact file in-place so follow-ups belong to the same task history.
    """
    import time
    runs_dir = runs_dir or RUNS_DIR
    os.makedirs(runs_dir, exist_ok=True)
    if not path:
        path = os.path.join(runs_dir, f"run_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    return path


def _clean_name(text: str, limit: int = 60) -> str:
    """Strip a string down to something safe as a Windows file or folder
    name: no reserved characters, no unprintables, collapsed whitespace,
    trimmed trailing dots/spaces (Windows rejects both)."""
    illegal = '<>:"/\\|?*'   # reserved on Windows; harmless to strip elsewhere
    clean = "".join(c for c in (text or "").strip()
                    if c not in illegal and c.isprintable())
    return " ".join(clean.split())[:limit].strip(" .")


_STOP_TAIL = {"a", "an", "the", "for", "and", "of", "to", "with", "in", "on",
              "at", "by", "from", "or", "this", "that", "my", "our", "me", "us",
              "like", "but", "as", "is", "are", "be", "so", "which", "into",
              "than", "about", "using", "via"}


def fallback_title(text: str, words: int = 6) -> str:
    """A short name for a run when no planner wrote one: the first few words
    of the request. A cut is not allowed to land on a joining word — it
    runs on (a little) until a real word ends the phrase, and any joining
    word still left dangling is trimmed. "make me a poster of a spring"
    stays whole; "can we make a platform like github but more…" becomes
    "can we make a platform like github". Not pretty; a folder can be
    found by it."""
    import re
    parts = re.sub(r"[^\w\s'&/-]", " ", text or "").split()
    n = min(len(parts), words)
    while 0 < n < len(parts) and n < words + 3 and parts[n - 1].lower() in _STOP_TAIL:
        n += 1
    out = parts[:n]
    while len(out) > 1 and out[-1].lower() in _STOP_TAIL:
        out.pop()
    return " ".join(out).strip() or "Task"


def tidy_title(title: str, limit: int = 60) -> str:
    """A model's answer, made fit for a folder name and a chat title: one
    line, no wrapping quotes, no trailing punctuation, bounded."""
    t = str(title or "").strip().split("\n")[0]        # first line only
    t = " ".join(t.split())
    t = t.strip().strip("\"'“”‘’`").rstrip(".:;,!").strip()
    return t[:limit].strip(" .-")


def _artifact_stem(prompt: str, kind: str, dated: bool = True) -> str:
    """A filename a person can actually read in Finder/Explorer without
    opening it. Inside a run folder it is the run's title and what kind of
    thing this is — "Instagram reel · brand guide — Artwork.png" — so a
    file dragged out of its folder still says what it is. Loose at the top
    level (no task given) it carries the prompt and the time as before."""
    import datetime
    label = (kind or "artifact").replace("_", " ").capitalize()
    if not dated:
        title = _run.get("title") or _clean_name(prompt, limit=40)
        return f"{title} — {label}" if title else label
    clean = _clean_name(prompt)
    when = datetime.datetime.now().strftime("%Y-%m-%d %I-%M %p")
    return " - ".join(p for p in (clean, label, when) if p)


# ── one folder per RUN ──────────────────────────────────────────────────────
# Prism Artifacts/<Task>/<2026-09-07 16-18>/. Grouped under the task so
# related runs sit together, one folder per run so they never merge: filed
# by the task's TEXT alone, every "Use again", every re-film and every
# follow-up of "make a reel for instgram for this brand" landed in one
# folder — twelve files from four runs on 2026-09-07 — and the follow-up
# dialog then attached all twelve to the next change.
ABOUT_FILE = "About this run.txt"
RUN_STAMP = "%Y-%m-%d %H-%M"
_run: dict = {"task": None, "dir": None, "title": None}


def reset_run() -> None:
    """Clear any current run directory or state so the next run starts clean."""
    _run.update(task=None, dir=None, title=None, continued=False)


def begin_run(task: str, title: str = "") -> str:
    """Open a fresh folder for one run of `task` and make it current, so
    every save_artifact(task=…) until the next begin_run lands in it.
    Returns the folder. An empty task means the loose top level.

    `title` names the folder — the planner's short name for the job
    ("Instagram reel · brand guide"), not the request verbatim with its
    typos and its forty words. Without one the request is trimmed to a
    few words; the full request is always in the About file.
    """
    import datetime
    task = (task or "").strip()
    if not task:
        _run.update(task=None, dir=None, title=None, continued=False)
        return artifacts_root()
    title = tidy_title(title) or fallback_title(task)
    parent = os.path.join(artifacts_root(), _clean_name(title, limit=80) or "Task")
    stamp = datetime.datetime.now().strftime(RUN_STAMP)
    folder, n = os.path.join(parent, stamp), 1
    while os.path.exists(folder):
        n += 1
        folder = os.path.join(parent, f"{stamp} ({n})")
    os.makedirs(folder, exist_ok=True)
    try:
        with open(os.path.join(folder, ABOUT_FILE), "w", encoding="utf-8") as f:
            f.write(f"{title}\n\nThe request, as typed:\n  {task}\n\n"
                    f"Started {datetime.datetime.now():%A %d %B %Y, %H:%M}\n"
                    "Everything Prism produced for this run is in this folder.\n")
    except OSError:
        pass
    _run.update(task=task, dir=folder, title=title, continued=False)
    return folder


def current_run_title() -> str:
    return _run.get("title") or ""


def current_run_dir() -> str:
    """The folder the current run's artifacts go to, or "" before any run."""
    folder = _run.get("dir") or ""
    return folder if folder and os.path.isdir(folder) else ""


def continue_run(folder: str, task: str = "", title: str = "") -> str:
    """Anchor future artifact writes to an existing run folder instead of
    opening a brand-new timestamped folder. Used for follow-ups and edits so
    all artifacts of a task remain in one unified directory.
    """
    if not folder or not os.path.isdir(folder):
        return begin_run(task, title)
    task_name = task or _run.get("task") or os.path.basename(os.path.dirname(folder)) or "Task"
    run_title = title or _run.get("title") or task_name
    _run.update(task=task_name, dir=folder, title=run_title, continued=True)
    return folder


def artifact_task_dir(task: str) -> str:
    """The folder `save_artifact()` writes into for this `task`: the current
    run's, when the task is the one begin_run() opened — otherwise a run is
    begun for it here, so a caller that never announced its run (an add-on
    dialog nobody has updated yet) still gets a folder of its own rather
    than everything it ever made in one pile.

    Exposed on its own (not only through save_artifact) for a caller whose
    deliverable is a whole directory tree — Gerber's cleaned-copy output,
    which is a folder of layers plus a report and preview images, not a
    single file `shutil.copy2` can land in one call.

    Empty `task` returns ARTIFACTS_DIR itself: the flat top level, so a
    caller passing nothing keeps working unchanged.
    """
    if not task:
        return artifacts_root()
    if (_run.get("continued") or _run.get("task") == task) and current_run_dir():
        return _run["dir"]
    return begin_run(task)


def save_artifact(src_path: str, prompt: str, kind: str = "artifact",
                  link: str = "", task: str = "", name: str = "") -> str:
    """Copy a file an agent generated into the one folder a customer will
    actually look in again — see ARTIFACTS_DIR above for why this exists.

    A copy, not a move: whatever already has `src_path` (a pipeline stage,
    the render worker) keeps working with the path it knows, and losing this
    copy afterwards (a full disk, a permissions slip) never breaks the
    caller's own view of what it produced.

    `link`, when given, is the live tab URL of the AI conversation that made
    this — real for a ChatGPT/Claude stage, empty for a local render (Reel,
    Motion) that was never a chat at all. Written as a tiny sidecar next to
    the artifact rather than baked into the filename, since a URL is
    something to open, not something to read at a glance in Finder/Explorer.

    `task`, when given, is the run/job this artifact belongs to (the same
    string already used to title it in History and in its own filename) —
    see `artifact_task_dir()`. Everything from one task then lands in one
    subfolder instead of loose at the top level.
    """
    dest_dir = artifact_task_dir(task)
    _, ext = os.path.splitext(src_path)
    stem = _artifact_stem(prompt, kind, dated=not task)
    # `name` is the filename the TOOL gave the file ("Quotation - JK
    # Cement.xlsx"). Kept after the run's own label, so the file still says
    # which run it came from and also what the customer asked it to be
    # called — the name they will look for.
    if name:
        clean = os.path.splitext(os.path.basename(name))[0].strip()
        if clean:
            stem = f"{stem} — {clean[:80]}"
    dest = os.path.join(dest_dir, f"{stem}{ext}")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(dest_dir, f"{stem} {n}{ext}" if task
                            else f"{stem}_{n}{ext}")
    import shutil
    shutil.copy2(src_path, dest)
    if ext.lower() == ".mp4":
        src_json = os.path.splitext(src_path)[0] + ".json"
        if os.path.isfile(src_json):
            try:
                dest_json = os.path.splitext(dest)[0] + ".json"
                shutil.copy2(src_json, dest_json)
            except OSError:
                pass
    if link:
        try:
            with open(dest + ".link.txt", "w", encoding="utf-8") as f:
                f.write(link.strip() + "\n")
        except OSError:
            pass   # the artifact itself is already safely saved either way
    return dest
