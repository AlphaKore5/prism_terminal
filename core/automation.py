"""
Prism — browser automation
───────────────────────────
Drives the user's logged-in Chrome (via undetected-chromedriver) through each
needed pipeline stage: opens the tool, types the prompt(s), waits, scrapes the
response, and passes it forward as context to the next stage.

Ported from the original prism_new.py, generalised to N categories and decoupled
from Google Drive. Selenium/uc are imported lazily so the REPL and dry-runs work
even on machines where they aren't installed yet.
"""
from __future__ import annotations
import os
import re
import time
import shutil
import tempfile
import subprocess
import platform
import webbrowser
from . import agents as A
from . import config as C
from . import ui

def _chrome_binaries() -> list[str]:
    """Where Chrome might be, on this machine specifically.

    This was a flat list with two hardcoded Windows paths, both under
    C:\\Program Files. Chrome's installer defaults to a PER-USER install
    whenever it is run without administrator rights — which is the normal
    case in an office — and that lands in %LOCALAPPDATA%\\Google\\Chrome\\
    Application instead. Neither hardcoded path exists on such a machine.

    The result was a split personality that took a while to make sense of:
    RUNS worked, because undetected-chromedriver has its own finder and does
    check LOCALAPPDATA, while LOGIN TABS said "Chrome not found — opening in
    your default browser instead. Logins there will NOT carry into Prism's
    runs." So the customer signed in, in the wrong browser, was told it would
    not count, and then every tool in every run met a sign-in wall. Reported
    as "the agents don't open".

    Same four environment variables uc's own find_chrome_executable() uses,
    for the same reason, plus PROGRAMW6432 — which is where a 32-bit process
    has to look to see the real 64-bit Program Files.
    """
    if platform.system() == "Windows":
        roots = [os.environ.get(v) for v in
                 ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA",
                  "PROGRAMW6432")]
        found, seen = [], set()
        for root in roots:
            if not root:
                continue
            path = os.path.join(root, "Google", "Chrome", "Application",
                                "chrome.exe")
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                found.append(path)
        return found
    if platform.system() == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/"
                               "MacOS/Google Chrome"),
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/snap/bin/google-chrome",
    ]


# Resolved at import: these are installation locations, not something that
# moves while Prism is running.
_CHROME_BINARIES = _chrome_binaries()

# Windows opens a console window for every subprocess unless told not to, and
# in a frozen GUI build that is a black rectangle flashing over the app. Zero
# on every other platform, where the flag does not exist.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Cap on how much of the previous stage's output is forwarded. The tail is
# kept (not the head) because that's where the HANDOFF summary lives.
# How much of the previous step's answer travels to the next tool. Since
# Round 31 (owner, 11 Sep 2026) that is the HANDOFF block -- at most
# _MAX_HANDOFF_LINES lines -- and this is only the ceiling under it, or the
# tail of an answer that wrote no such block. It was 8,000 characters of the
# whole previous answer, which is how a Perplexity research report became
# most of the message Claude was sent and ran the chat out of tokens.
_MAX_FORWARD_CHARS = 1200
_MAX_HANDOFF_LINES = 4

#: The one sentence that closes the last step's message. When a file, a
#: picture or a video is what the step hands back, contract.deliverable_line
#: already says "give me the finished result as a file", so the closing
#: shrinks to the half that line does not say.
_FINAL_CLOSING = "\n\nGive me the finished result — no questions back."
_FINAL_CLOSING_AFTER_DELIVERABLE = "\n\nNo questions back."

# The live browser, kept across runs. A slow producer (deck/video/app builder)
# often keeps rendering its result server-side after Prism gives up waiting on
# it, and the user still needs to go look at that tab — so nothing in this
# module ever calls driver.quit(). The same window is reused for the next
# command; a fresh one is only launched if the user has since closed it.
_active_driver = None


def _get_driver(cfg: dict):
    """Return (driver, fresh) — reusing the still-open browser from a
    previous run when possible, so results left on screen aren't disturbed."""
    global _active_driver
    if _active_driver is not None:
        try:
            _active_driver.window_handles  # cheap liveness probe
            return _active_driver, False
        except Exception:
            _active_driver = None
    _active_driver = _setup_chrome_driver(parse_chrome_version(cfg.get("chrome_version")))
    return _active_driver, True


def _driver_has_live_tab(driver) -> bool:
    """Return whether Chrome kept at least one WebDriver-controlled tab.

    A successful ``uc.Chrome(...)`` constructor only proves Chrome started.
    It does *not* prove the initial renderer survived: on some Linux Chrome
    builds the process starts, reports a session, then drops its sole tab a
    moment later.  Detect that at launch so the normal one-time driver retry
    can recover before a customer waits on an empty Content card.

    Tiny test doubles intentionally expose no ``window_handles`` property;
    they stand in for a successful driver and remain valid here.
    """
    if not hasattr(driver, "window_handles"):
        return True
    try:
        return bool(driver.window_handles)
    except Exception:
        return False


def _ensure_active_window(driver) -> bool:
    """Ensure driver is pointing to a live window handle.

    If driver.current_window_handle raises NoSuchWindowException or is not in
    driver.window_handles, switch to the latest available handle in
    driver.window_handles. If no handles exist, try creating a new window.
    Returns True if an active window handle is established, False otherwise.
    """
    if not hasattr(driver, "window_handles"):
        return True
    try:
        handles = list(driver.window_handles)
    except Exception:
        return False
    if not handles:
        try:
            if hasattr(driver, "switch_to") and hasattr(driver.switch_to, "new_window"):
                driver.switch_to.new_window("tab")
                return bool(driver.window_handles)
        except Exception:
            return False
        return False

    try:
        cur = driver.current_window_handle
        if cur in handles:
            return True
    except Exception:
        pass

    for h in reversed(handles):
        try:
            driver.switch_to.window(h)
            return True
        except Exception:
            continue
    return False


def _open_tab(driver, agent_name: str = "") -> bool:
    """Put the next stage in a NEW tab, leaving every finished one on screen.

    Each stage gets its own tab so the whole run stays readable afterwards —
    Claude's answer still there when ChatGPT's arrives, and so on. That is the
    point of never calling quit(), and it only works if this actually gets a
    new tab every time.

    It was `execute_script("window.open('')")`, then `sleep(1)`, then
    `window_handles[-1]`, and that failed two ways — both silently, and both
    ending in the same place: no new tab, so `driver.get()` navigated the tab
    the PREVIOUS agent's answer was sitting in, and that answer was gone.

      · window.open() from injected script is not a user gesture, so Chrome's
        popup blocker is entitled to refuse it, and on a real profile with
        stricter settings it does.
      · Even when allowed, one second is a guess. If the handle had not
        registered yet, handles[-1] was still the OLD tab.

    switch_to.new_window() is the WebDriver command for this. It goes through
    the driver rather than the page, so the popup blocker has no say, and it
    returns once the tab exists rather than after a hopeful sleep.

    Returns whether a new tab was actually obtained. Falling back to the old
    trick, and then to reusing the current tab, is deliberate: losing an
    earlier answer from the screen is bad, but refusing to run the stage at
    all would be worse.
    """
    _ensure_active_window(driver)
    before = set()
    try:
        before = set(driver.window_handles)
    except Exception:
        pass
    try:
        driver.switch_to.new_window("tab")
        current_handles = set(driver.window_handles)
        new = current_handles - before
        if new:
            driver.switch_to.window(new.pop())
            return True
        elif driver.window_handles:
            driver.switch_to.window(driver.window_handles[-1])
            return True
    except Exception:
        pass

    try:                                # older drivers, or a refusal above
        _ensure_active_window(driver)
        driver.execute_script("window.open('');")
        for _ in range(20):             # up to ~4s, checked rather than assumed
            time.sleep(0.2)
            new = set(driver.window_handles) - before
            if new:
                driver.switch_to.window(new.pop())
                return True
    except Exception:
        pass

    _ensure_active_window(driver)
    ui.warn(f"   ⚠️   couldn't open a new tab for {agent_name or 'this step'} — "
            f"reusing the current one, so the previous answer will be replaced. "
            f"Its text is still saved in this run.")
    return False


def shutdown() -> None:
    """Close Prism's browser, if one is open.

    Nothing in a normal run calls this — the tabs are left up on purpose, since
    a slow tool often finishes in its tab after Prism stops watching. But when
    the APP itself is quitting there is no one left to read them, and leaving
    the driver running strands a headless Chrome and its profile lock, so the
    next launch fails with "profile appears to be in use".
    """
    global _active_driver
    if _active_driver is None:
        return
    try:
        _active_driver.quit()
    except Exception:
        pass
    finally:
        _active_driver = None
        # uc's quit() can leave Chrome renderer/GPU children behind on Linux.
        # They keep Prism's profile locked and make the next controlled tab
        # close as soon as Chrome hands its command line to that old process.
        _release_profile()


def _bmp_safe(text: str) -> str:
    """ChromeDriver's send_keys only accepts Basic-Multilingual-Plane characters
    (<= U+FFFF). Drop anything above it (emoji, etc.) so typing never crashes
    with 'ChromeDriver only supports characters in the BMP'."""
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


def parse_chrome_version(raw) -> int | None:
    """Accept '147', '147.0.7727.139', 147 → 147. Blank/invalid → None."""
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip().split(".")[0])
    except (ValueError, IndexError):
        return None


def _windows_chrome_version() -> int | None:
    """The installed Chrome major version on Windows.

    `chrome.exe --version` cannot be used here. Chrome ships as a GUI-subsystem
    binary on Windows, so it has no console to print to: the command exits
    silently with empty stdout rather than failing, and the caller is left
    parsing an empty string. Ask Windows itself instead — the registry first,
    since Chrome writes its own version to BLBeacon on every update, then the
    executable's file version as a fallback for installs that lack the key.

    Getting None back here is not cosmetic: it disables the pinned-version
    guard in _setup_chrome_driver, which is what lets a stale pin drive the
    wrong chromedriver until Chrome dies a second after launch.
    """
    try:
        out = subprocess.check_output(
            ["reg", "query", r"HKCU\Software\Google\Chrome\BLBeacon",
             "/v", "version"],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=_NO_WINDOW)
        # "    version    REG_SZ    147.0.7727.139"
        return int(out.strip().split()[-1].split(".")[0])
    except Exception:
        pass
    for path in _CHROME_BINARIES:
        if not os.path.exists(path):
            continue
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-Item '{path}').VersionInfo.ProductVersion"],
                text=True, stderr=subprocess.DEVNULL, timeout=20,
                creationflags=_NO_WINDOW)
            return int(out.strip().split(".")[0])
        except Exception:
            continue
    return None


def detect_chrome_version() -> int | None:
    """Return the installed Chrome major version, or None if it can't be found."""
    if platform.system() == "Windows":
        return _windows_chrome_version()
    for path in _CHROME_BINARIES:
        if not os.path.exists(path):
            continue
        try:
            out = subprocess.check_output([path, "--version"], text=True)
            # e.g. "Google Chrome 147.0.7727.139"
            return int(out.strip().split()[2].split(".")[0])
        except Exception:
            continue
    return None


# Prism's own browser profile. It lives beside the config (NOT in /tmp, which
# the OS clears on reboot) and it PERSISTS: every login you complete inside the
# automated window — including the ones a tool forces mid-run — is still there
# next time. It used to be wiped and re-cloned on every launch, which meant any
# session Prism itself established was thrown away, and a tool that had logged
# you out once stayed logged out forever.
PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".prism", "chrome_profile")

# Caches are re-created on demand and are the bulk of a Chrome profile —
# copying them makes seeding take minutes and adds nothing. Logins live in
# Cookies / Login Data / Local Storage / IndexedDB, which are all kept.
_PROFILE_SKIP = shutil.ignore_patterns(
    "Singleton*", "*.lock", "Cache", "Cache*", "Code Cache", "GPUCache",
    "ShaderCache", "GrShaderCache", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "Service Worker", "Application Cache", "Media Cache",
    "component_crx_cache", "extensions_crx_cache", "optimization_guide*",
    "segmentation_platform", "Crashpad", "blob_storage",
)


def user_chrome_dir() -> str:
    """Where the real Chrome keeps its profiles on this OS."""
    system = platform.system()
    if system == "Linux":
        return os.path.expanduser("~/.config/google-chrome")
    if system == "Windows":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google",
                            "Chrome", "User Data")
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    ui.err("prism yet doesnt support your OS")
    raise RuntimeError(f"Unsupported operating system: {system}")


def profile_is_seeded() -> bool:
    default = os.path.join(PROFILE_DIR, "Default")
    return any(os.path.exists(os.path.join(default, f))
               for f in ("Cookies", "Preferences", "Login Data"))


def seed_profile(force: bool = False) -> bool:
    """Copy the real Chrome profile into Prism's, once. Returns True if it
    copied. Call with force=True to refresh from the real browser — that's the
    fix for 'I logged into the tool in my normal Chrome but Prism still asks'."""
    if profile_is_seeded() and not force:
        return False
    src = user_chrome_dir()
    src_default = os.path.join(src, "Default")
    if not os.path.exists(src_default):
        os.makedirs(os.path.join(PROFILE_DIR, "Default"), exist_ok=True)
        ui.warn("No Chrome profile found to copy — starting a blank one. "
                "Sign in to your tools in the window Prism opens.")
        return False
    # A running Chrome hasn't flushed its newest cookies to disk, so a copy
    # taken now can be missing the login the user just completed.
    if os.path.exists(os.path.join(src, "SingletonLock")):
        ui.warn("Chrome is running — close it for the most reliable copy of "
                "your logins.")
    ui.info("   🧬  copying your Chrome logins into Prism's profile (once)…")
    if force and os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        shutil.copytree(src_default, os.path.join(PROFILE_DIR, "Default"),
                        dirs_exist_ok=True, ignore=_PROFILE_SKIP)
    except shutil.Error as exc:
        # Windows: a running Chrome holds some files open (Network\Cookies
        # first among them) and copytree raises only AFTER copying everything
        # else. That copy is a working profile — the login tabs about to open
        # are where the person signs in anyway — so keep it. Raising here
        # failed the whole Login tabs press, silently, on the first click of a
        # fresh Windows install (09-Oct-2026).
        skipped = exc.args[0] if exc.args and isinstance(exc.args[0], list) else []
        ui.warn(f"Copied your Chrome logins except {len(skipped)} file(s) Chrome "
                "had open — sign in again in the tabs Prism opens.")
    local_state = os.path.join(src, "Local State")
    if os.path.exists(local_state):
        try:
            shutil.copy2(local_state, os.path.join(PROFILE_DIR, "Local State"))
        except OSError:
            pass                # Chrome writes a fresh one on first start
    return True


def _clear_profile_locks():
    """A run that was killed (or a crash) leaves SingletonLock behind, and the
    next launch then fails with 'profile appears to be in use'. The profile is
    Prism's alone, so a leftover lock is always stale.

    POSIX only, in effect — Windows Chrome does not use these files at all,
    it guards a profile with a named kernel object. _release_profile() is the
    half that works there.
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _chrome_pids_using_profile() -> list[int] | None:
    """PIDs of every Chrome process currently holding Prism's OWN profile.

    None — not [] — when the probe itself could not run. "I looked and found
    nothing" and "I could not look" lead to opposite next steps, and
    collapsing them is how a fix becomes a silent no-op: on Windows this
    shells out to PowerShell, and if that fails for any reason (execution
    policy, WMI disabled, no powershell.exe on PATH) an empty list would
    read as "the profile is free", _release_profile would do nothing, and
    the customer would get the original "cannot connect to chrome" back with
    no clue why. The caller says so out loud instead.

    Two conditions, and BOTH are load-bearing, because the result of this is
    fed to a kill:

      · the command line carries `--user-data-dir=<Prism's profile>`, which
        nothing but Prism ever passes. The user's everyday Chrome cannot
        match it, and must not — killing that would take their real work
        with it.
      · the program being run is Chrome itself.

    The second one was missing at first and it mattered immediately: `ps`
    reports full command lines, so ANY process whose arguments merely
    mention the path matched — a shell running a script that names it, an
    editor with the folder open. Caught by this function killing the very
    shell that was testing it.
    """
    marker = "--user-data-dir=" + PROFILE_DIR
    try:
        if platform.system() == "Windows":
            # Win32_Process, not tasklist: the command line is the only place
            # the profile appears, and tasklist does not report it. The
            # profile path goes through the ENVIRONMENT rather than into the
            # script text, so a path containing a quote or a bracket can
            # neither break the command nor be read as a wildcard.
            #
            # NOT ONE DOUBLE QUOTE in the script, which is why the obvious
            # `-Filter "Name='chrome.exe'"` is a Where-Object test instead.
            # Python builds a Windows command line with list2cmdline, which
            # escapes an embedded " as \" — and powershell's -Command does
            # not unescape it the way a C program's argv would, so the filter
            # arrives as a parse error. That fails the whole probe, and a
            # failed probe on Windows is precisely the case this function
            # exists to handle. Verified by printing list2cmdline's output.
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { "
                 "$_.Name -eq 'chrome.exe' -and $_.CommandLine -and "
                 "$_.CommandLine.Contains($env:PRISM_PROFILE_ARG) } "
                 "| ForEach-Object { $_.ProcessId }"],
                text=True, stderr=subprocess.DEVNULL, timeout=30,
                creationflags=_NO_WINDOW,
                env={**os.environ, "PRISM_PROFILE_ARG": marker})
        else:
            raw = subprocess.check_output(["ps", "-Ao", "pid=,args="],
                                          text=True, stderr=subprocess.DEVNULL,
                                          timeout=15)
            out = "\n".join(_posix_chrome_pids(raw, marker))
    except Exception:
        return None                    # could not look — not "nothing there"
    pids = []
    for token in out.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def _posix_chrome_pids(ps_output: str, marker: str) -> list[str]:
    """The `ps -Ao pid=,args=` lines that are a Chrome on this profile."""
    out = []
    for line in ps_output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or marker not in parts[1]:
            continue
        # The program is everything before its first `--flag`, not the first
        # whitespace-separated token. That token was `/Applications/Google`
        # for a Mac's `/Applications/Google Chrome.app/Contents/MacOS/Google
        # Chrome`, whose basename is "Google" -- so no Mac Chrome ever
        # matched, and _release_profile() was a silent no-op on every Mac.
        # The Chrome left running by Login tabs (closing its windows does
        # not quit a Mac app), and the one uc leaves behind when its driver
        # fails to start, were never closed; the next launch handed itself
        # to them and chromedriver said "cannot connect to chrome". Found on
        # a client's M2, 10 Sep 2026, where it also defeated the errno-86
        # retry: the retry's Chrome handed off to the first attempt's orphan.
        program = os.path.basename(parts[1].split(" --", 1)[0].strip()).lower()
        if "chrome" in program or "chromium" in program:
            out.append(parts[0])
    return out


def _release_profile() -> int:
    """Close any Chrome still holding Prism's profile. Returns how many.

    Chrome allows exactly one browser process per user-data-dir. A second
    launch does not fail and does not get its own browser — it hands its
    command line to the instance already there and EXITS. undetected-
    chromedriver launches Chrome itself with --remote-debugging-port and then
    points chromedriver at that port, so when the launch it just made exits
    instead of running, chromedriver reports:

        session not created: cannot connect to chrome at 127.0.0.1:53695

    which is the error a customer sent from Windows, and which Prism showed
    them as a Chrome/driver VERSION mismatch — sending them to update a
    browser that was working perfectly.

    Two ordinary things leave a Chrome on the profile: Login tabs (see
    open_login_tabs — it launches a plain Chrome on this same profile, and
    people leave that window open), and a Prism that was killed or crashed
    while a run was going, which never reaches shutdown(). On POSIX the
    stale SingletonLock at least made this visible; on Windows there is no
    lock file to find, and it simply repeats every run until a reboot.

    Only reached when there is no live driver to reuse (see _get_driver), so
    a Chrome found here is one Prism can no longer talk to — closing it
    costs nothing and is the only way to get a browser at all.
    """
    pids = _chrome_pids_using_profile()
    if pids is None:
        # Said out loud, because the next thing the customer may see is the
        # "cannot connect to chrome" this exists to prevent — and then the
        # question is whether the guard ran and found nothing, or never ran.
        ui.warn("   couldn't check for a leftover Chrome on Prism's profile. "
                "If the browser fails to start, close every Chrome window and "
                "try again.")
        return 0
    if not pids:
        return 0
    ui.info(f"   🧹  closing {len(pids)} leftover Chrome process(es) still "
            f"holding Prism's browser profile")

    # Ask first, insist second. A hard kill costs the very thing this profile
    # exists for: Chrome keeps new cookies in memory and writes them out on a
    # clean shutdown, so force-killing a window somebody has just signed in
    # through throws that sign-in away — and the next run walks into the
    # login wall the profile was supposed to prevent.
    _signal_pids(pids, force=False)
    left = pids
    for _ in range(24):                 # up to ~6s to close gracefully
        time.sleep(0.25)
        left = _chrome_pids_using_profile()
        if not left:
            return len(pids)

    _signal_pids(left, force=True)
    for _ in range(12):
        time.sleep(0.25)
        if not _chrome_pids_using_profile():
            break
    return len(pids)


def _signal_pids(pids: list[int], force: bool) -> None:
    """Close (or, forced, kill) each process. Never raises."""
    import signal as _signal
    for pid in pids:
        try:
            if platform.system() == "Windows":
                # /T takes the child processes with it — Chrome is a process
                # tree, and the browser process alone leaving would strand
                # its renderers on the profile.
                subprocess.run(["taskkill", "/PID", str(pid), "/T"]
                               + (["/F"] if force else []),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15,
                               creationflags=_NO_WINDOW)
            else:
                os.kill(pid, _signal.SIGKILL if force else _signal.SIGTERM)
        except Exception:
            continue


# Preferences is a settings file. Anything past this is not settings.
_PREFS_SANE = 8_000_000
_PREFS_HOPELESS = 150_000_000
# Keys that grow without bound under automation and hold nothing worth
# keeping. DevTools state is appended to on every CDP session — which is
# every Prism run — and nothing ever prunes it.
_PREFS_JUNK = ("devtools", "media")


def _prune_preferences() -> None:
    """Stop Prism poisoning its own browser profile.

    Chrome parses Preferences at startup and CHECK-fails on a big enough one:
    the browser dies about a second after launch with a SIGTRAP in
    CrBrowserMain and a crash report that says nothing about why. Seen in the
    wild at 2.2 GB, of which 2.06 GB was the 'devtools' key alone, grown a
    little at a time over months of automated runs.

    Deliberately does NOT parse a hopeless file — a 2 GB JSON costs several
    gigabytes of RAM to load, and this runs on the way to launching a browser.
    Past that size the file is quarantined instead; Chrome writes a fresh one,
    and logins are unaffected because they live in Cookies and Login Data,
    not here.
    """
    import json
    path = os.path.join(PROFILE_DIR, "Default", "Preferences")
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < _PREFS_SANE:
        return

    if size > _PREFS_HOPELESS:
        spare = f"{path}.oversized-{int(time.time())}"
        try:
            os.replace(path, spare)
            ui.warn(f"Chrome's settings file had grown to {size/1e6:.0f} MB — "
                    "big enough to crash the browser on launch. Set aside; "
                    "your logins are untouched.")
        except OSError:
            pass
        return

    try:
        with open(path) as f:
            prefs = json.load(f)
    except Exception:
        return
    freed = 0
    for key in _PREFS_JUNK:
        if key in prefs:
            freed += len(json.dumps(prefs[key]))
            del prefs[key]
    if not freed:
        return
    try:
        tmp = path + ".pruned"
        with open(tmp, "w") as f:
            json.dump(prefs, f, separators=(",", ":"))
        os.replace(tmp, path)
        ui.info(f"   🧹  trimmed {freed/1e6:.0f} MB of accumulated DevTools "
                "state from Chrome's settings")
    except OSError:
        pass


def _ensure_session_restore() -> None:
    """Keep session-only login cookies alive across a full Prism restart.

    Some tools (Kimi included) sign you in with a cookie that has no
    Expires/Max-Age — a "session cookie". Chrome deletes those the moment the
    browser process ends, UNLESS the profile's startup setting is "Continue
    where you left off" rather than the default "Open the New Tab page". The
    browser stays open across runs within one Prism session (nothing here
    calls driver.quit() until the app itself quits — see shutdown()), so this
    only bites after a full restart: everything else in the profile persisted
    fine, but a tool using a session cookie silently needs a fresh login.
    """
    import json
    path = os.path.join(PROFILE_DIR, "Default", "Preferences")
    try:
        with open(path) as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        prefs = {}
    if prefs.get("session", {}).get("restore_on_startup") == 1:
        return
    prefs.setdefault("session", {})["restore_on_startup"] = 1
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".restore-pref"
        with open(tmp, "w") as f:
            json.dump(prefs, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def _reset_to_blank_tab(driver) -> None:
    """Collapse a restored session back down to the single blank tab the
    pipeline expects a fresh launch to have.

    _ensure_session_restore() sets "Continue where you left off" so
    session-only login cookies survive a restart — but that setting also
    makes Chrome reopen every tab left over from the last time Prism quit,
    and stage one relies on a freshly launched browser opening on exactly
    one blank tab (see `first_tab` in run())."""
    try:
        handles = list(driver.window_handles)
    except Exception:
        return
    if len(handles) <= 1:
        if handles:
            try:
                driver.switch_to.window(handles[0])
                driver.get("about:blank")
            except Exception:
                pass
        return

    keep_handle = handles[0]
    for h in handles[1:]:
        try:
            driver.switch_to.window(h)
            driver.close()
        except Exception:
            pass

    try:
        remaining = list(driver.window_handles)
        target_handle = keep_handle if keep_handle in remaining else (remaining[0] if remaining else None)
        if target_handle:
            driver.switch_to.window(target_handle)
            driver.get("about:blank")
        elif hasattr(driver, "switch_to") and hasattr(driver.switch_to, "new_window"):
            driver.switch_to.new_window("tab")
            driver.get("about:blank")
    except Exception:
        pass


def _uc_cache_dir() -> str:
    """Where undetected-chromedriver keeps the driver it patched last time.

    Mirrors Patcher.data_path in undetected_chromedriver/patcher.py. This was
    hardcoded to the macOS path, which silently made the staleness check in
    _setup_chrome_driver a no-op on Windows and Linux — the two platforms where
    it matters most, since a Chrome auto-update there leaves a version-behind
    driver sitting in this directory and uc reuses it forever.
    """
    system = platform.system()
    if system == "Windows":
        d = "~/appdata/roaming/undetected_chromedriver"
    elif system == "Linux":
        d = "~/.local/share/undetected_chromedriver"
    elif system == "Darwin":
        d = "~/Library/Application Support/undetected_chromedriver"
    else:
        d = "~/.undetected_chromedriver"
    return os.path.abspath(os.path.expanduser(d))


# Mach-O CPU types, from <mach/machine.h>. Read directly rather than by
# shelling out to `file`: a GUI app launched from the Finder has a minimal
# PATH, `file` can be missing or answer in another language, and the
# previous check swallowed every failure -- which is how an Intel driver
# copied over from an old Mac by Migration Assistant survived the cleanup
# on an M2 with no Rosetta, and Prism died with a bare "[Errno 86] Bad CPU
# type in executable".
_MACHO_X86_64 = 0x01000007
_MACHO_ARM64 = 0x0100000C


def _macho_arches(path: str) -> set:
    """The CPU architectures a Mach-O file carries: {"x86_64"}, {"arm64"},
    both for a universal binary, or an empty set for anything unreadable
    or not Mach-O at all."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return set()
    return _macho_arches_bytes(head)


def _macho_arches_bytes(head: bytes) -> set:
    import struct
    names = {_MACHO_X86_64: "x86_64", _MACHO_ARM64: "arm64"}
    if len(head) < 8:
        return set()
    magic = head[:4]
    out = set()
    if magic == b"\xca\xfe\xba\xbe":                      # fat / universal
        n = struct.unpack(">I", head[4:8])[0]
        for i in range(min(n, 8)):
            off = 8 + i * 20
            if off + 4 > len(head):
                break
            cputype = struct.unpack(">I", head[off:off + 4])[0]
            if cputype in names:
                out.add(names[cputype])
        return out
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):   # thin, little-endian
        cputype = struct.unpack("<I", head[4:8])[0]
    elif magic in (b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):  # thin, big-endian
        cputype = struct.unpack(">I", head[4:8])[0]
    else:
        return set()
    if cputype in names:
        out.add(names[cputype])
    return out


def _driver_wrong_arch(path: str) -> str:
    """Why this cached driver cannot run on this Mac, or "" when it can.
    Apple Silicon only: an Intel driver there needs Rosetta, which a new
    Mac does not have, and this is the one case a leftover file kills the
    first run outright."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return ""
    arches = _macho_arches(path)
    if arches and "arm64" not in arches:
        return "built for Intel Macs (" + "/".join(sorted(arches)) + ")"
    return ""


def _purge_uc_cache(why: str) -> None:
    """Remove every cached driver so undetected-chromedriver downloads the
    one for THIS machine. Loud on failure -- a silent pass here is exactly
    what let the wrong driver survive before."""
    d = _uc_cache_dir()
    if not os.path.isdir(d):
        return
    stuck = []
    for f in os.listdir(d):
        fp = os.path.join(d, f)
        try:
            if os.path.isdir(fp):
                shutil.rmtree(fp)
            else:
                os.remove(fp)
        except OSError as e:
            stuck.append(f"{fp}: {e}")
    if stuck:
        # A folder the user cannot write into (Migration Assistant leaves
        # those): move the whole thing aside instead -- that only needs the
        # parent to be writable, which ~/Library/Application Support is.
        aside = f"{d}.intel-{int(time.time())}"
        try:
            os.rename(d, aside)
            ui.info(f"   ♻️   moved the old driver folder aside → {aside}")
        except OSError as e:
            ui.warn("   couldn't remove the cached browser driver: "
                    + "; ".join(stuck) + f"; nor move it aside: {e}")
            return
    ui.info(f"   ♻️   replacing the cached driver — {why}")


_INTEL_DRIVER_HELP = (
    "The browser driver that was run is built for Intel Macs, and this Mac "
    "has no Rosetta to run it.\n\n"
    "  1. Check this Mac is online and press Start again: Prism fetches its "
    "own Apple silicon driver into ~/.prism/chromedriver and uses that.\n"
    "  2. If it comes back, run this in Terminal and try once more:\n"
    "     rm -rf ~/Library/Application\\ Support/undetected_chromedriver\n"
    "  3. Still stuck? Settings → Export diagnostics and send the file."
)

_NO_ARM64_DRIVER_HELP = (
    "Prism needs its Apple silicon browser driver on this Mac, and couldn't "
    "fetch it.\n\n"
    "  1. Check this Mac is online and press Start again. It is a one-time "
    "download of about 10 MB from Google's Chrome for Testing site; after "
    "that it is kept in ~/.prism/chromedriver.\n"
    "  2. On a company network, ask IT to allow googlechromelabs.github.io "
    "and storage.googleapis.com.\n"
    "  3. Still stuck? Settings → Export diagnostics and send the file."
)

_MAC_QUIT_CHROME = (
    "On a Mac, closing Chrome's windows leaves it running: quit it with ⌘Q "
    "(Chrome menu → Quit Google Chrome), then try again.")


def _is_apple_silicon() -> bool:
    """Darwin on an arm64 process. False for the same Mac running an x86_64
    Python under Rosetta -- there an Intel driver is the right one."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _rosetta_installed() -> bool:
    """Can this Apple silicon Mac run an Intel binary at all?

    A new Mac ships without Rosetta; macOS offers to install it the first
    time an Intel app is opened from the Finder, and never for a binary a
    program executes -- that just fails with errno 86. Which is why every
    Mac at Alphakore ran the Intel driver fine (Rosetta had been pulled in
    by something else long ago) and the client's M2 did not. Two files that
    exist once it is installed, then a real exec as the tie-breaker."""
    if not _is_apple_silicon():
        return False
    for path in ("/Library/Apple/usr/share/rosetta/rosetta",
                 "/Library/Apple/usr/libexec/oah/libRosettaRuntime"):
        if os.path.exists(path):
            return True
    try:
        return subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:                                       # noqa: BLE001
        return False


def _refuse_intel_fallback(own_driver: str) -> None:
    """Stop before launching a Chrome nothing will drive.

    undetected-chromedriver 3.5.5 knows three driver builds -- win32,
    linux64 and mac-x64 (patcher.py, _set_platform_name) -- so on ANY Mac
    it downloads the Intel driver, and on Apple silicon without Rosetta
    that is a certain errno 86. Letting it try anyway costs the customer a
    Chrome window that opens and sits there, and a message about updating
    Chrome. When Prism's own arm64 driver is not available, say the one
    true thing instead: it needs to be fetched once, and this Mac is not
    reaching the feed."""
    if own_driver or not _is_apple_silicon() or _rosetta_installed():
        return
    raise RuntimeError("Prism couldn't start Chrome.\n\n" + _NO_ARM64_DRIVER_HELP)

# ── Prism's own driver on Apple silicon ─────────────────────────────────────
# undetected-chromedriver decides which chromedriver to fetch from
# platform.machine() and unlinks + re-downloads on EVERY launch; when the
# unlink is refused it silently reuses whatever is there. On a client's M2
# that left an Intel driver (migrated from an old Mac, in a folder the
# user could not delete) being executed run after run, with no Rosetta.
# So on Darwin/arm64 Prism fetches the mac-arm64 driver itself, from the
# same Chrome-for-Testing feed uc uses, into its own folder, checks the
# Mach-O header, and hands uc that path. uc's own cache no longer matters.
_PRISM_DRIVER_DIR = os.path.join(C.CONFIG_DIR, "chromedriver")
_CFT_LATEST = "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_{}"
_CFT_ZIP = ("https://storage.googleapis.com/chrome-for-testing-public/{}/"
            "mac-arm64/chromedriver-mac-arm64.zip")


def _trusted_ssl_context():
    """An SSL context that verifies against certifi's own certificate
    bundle, not Python's default lookup.

    A frozen build's OpenSSL has its default cert PATH compiled in at
    build time -- the machine that ran PyInstaller, never the customer's.
    `ssl.get_default_verify_paths()` on a client Mac with no Python of its
    own points at a file that is not there, so the plain default context
    finds nothing to verify against and every HTTPS request fails with
    "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer
    certificate" -- reproduced by hand (SSL_CERT_FILE/DIR pointed at a
    path that does not exist, the same shape a frozen app's baked-in path
    takes on a different machine) against this file's own chromedriver
    fetch, 16 Sep 2026. certifi ships its own cacert.pem as a package
    file, found the same way on every machine regardless of what built
    the app, which is the whole reason it is bundled (PyInstaller's own
    hook does that automatically once certifi is imported anywhere) --
    nothing here previously told Python's ssl module to use it instead of
    its own guess. Falls back to the plain default context if certifi is
    somehow unavailable, so this can only make a request MORE likely to
    verify, never less."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                       # noqa: BLE001
        return ssl.create_default_context()


def _http_get(url: str, timeout: float = 60.0) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Prism"})
    with urllib.request.urlopen(                              # noqa: S310
            req, timeout=timeout, context=_trusted_ssl_context()) as r:
        return r.read()


def _own_driver_path(tag: str) -> str:
    return os.path.join(_PRISM_DRIVER_DIR, tag, "chromedriver")


def _own_driver_ok(path: str) -> bool:
    return os.path.isfile(path) and "arm64" in _macho_arches(path)


def _fetch_arm64_driver(tag: str) -> str:
    """Download the mac-arm64 chromedriver the Chrome-for-Testing feed names
    for `tag` ("152" or "STABLE") into Prism's folder. Raises on any failure;
    the caller decides what to try next."""
    import io
    import zipfile
    path = _own_driver_path(tag)
    version = _http_get(_CFT_LATEST.format(tag), timeout=30).decode().strip()
    data = _http_get(_CFT_ZIP.format(version))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next(n for n in z.namelist()
                      if n.endswith("/chromedriver") or n == "chromedriver")
        blob = z.read(member)
    if "arm64" not in _macho_arches_bytes(blob):
        raise RuntimeError("the downloaded driver is not an arm64 build")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".part"
    with open(tmp_path, "wb") as f:
        f.write(blob)
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, path)
    ui.info(f"   ⬇️   fetched the Apple silicon chromedriver {version}")
    return path


def _cached_own_drivers() -> list[str]:
    """Every arm64 driver already in Prism's folder, newest major first
    (STABLE, having no number, sorts last)."""
    try:
        tags = os.listdir(_PRISM_DRIVER_DIR)
    except OSError:
        return []
    found = [t for t in tags if _own_driver_ok(_own_driver_path(t))]
    found.sort(key=lambda t: (not t.isdigit(), -int(t) if t.isdigit() else 0))
    return [_own_driver_path(t) for t in found]


def _apple_silicon_driver(version_main=None) -> str:
    """Path to an arm64 chromedriver for this Chrome, downloaded once per
    Chrome major into ~/.prism/chromedriver/<major>/. "" anywhere but
    Darwin/arm64, and "" (with a warning) when nothing can be had.

    In order: the exact major already on disk; the exact major from the
    feed; STABLE on disk; STABLE from the feed; any arm64 driver already on
    disk. The last three exist for the two days after a Chrome auto-update
    when `LATEST_RELEASE_<new major>` is still a 404, and for a Mac that is
    offline on a later run -- a driver one major behind usually still
    drives the browser, and a wrong-version error names the real problem,
    where a missing driver used to hand the launch to uc's Intel build."""
    if not _is_apple_silicon():
        return ""
    tags = ([str(int(version_main))] if version_main else []) + ["STABLE"]
    errors = []
    for tag in tags:
        path = _own_driver_path(tag)
        if _own_driver_ok(path):
            return path
        try:
            return _fetch_arm64_driver(tag)
        except Exception as e:                              # noqa: BLE001
            errors.append(f"{tag}: {e}")
    cached = _cached_own_drivers()
    if cached:
        ui.warn(f"   couldn't fetch the Apple silicon chromedriver ("
                f"{'; '.join(errors)}); using the one already here: {cached[0]}")
        return cached[0]
    ui.warn(f"   couldn't fetch the Apple silicon chromedriver ({'; '.join(errors)})")
    return ""


def driver_report() -> list[str]:
    """What support needs to know about the browser driver on this machine,
    for Export diagnostics: the CPU, Rosetta, where Chrome is, every driver
    file with the CPU it was built for, and whether a Chrome is sitting on
    Prism's profile right now. Never raises."""
    lines = [f"CPU               {platform.machine()} ({platform.system()})"]
    if platform.system() == "Darwin":
        lines.append(f"Apple silicon     {'yes' if _is_apple_silicon() else 'no'}")
        if _is_apple_silicon():
            lines.append(f"Rosetta           "
                         f"{'installed' if _rosetta_installed() else 'not installed'}")
    chrome = next((c for c in _CHROME_BINARIES if os.path.exists(c)), None)
    version = detect_chrome_version() if chrome else None
    lines.append(f"Chrome            {chrome or 'not found'}"
                 + (f" (v{version})" if version else ""))
    for label, folder in (("uc driver cache", _uc_cache_dir()),
                          ("Prism drivers", _PRISM_DRIVER_DIR)):
        lines.append(f"{label:<17} {folder}"
                     + ("" if os.path.isdir(folder) else " (absent)"))
        try:
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    if "chromedriver" not in name.lower():
                        continue
                    path = os.path.join(root, name)
                    arch = "/".join(sorted(_macho_arches(path))) or "not Mach-O"
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    lines.append(f"  {os.path.relpath(path, folder):<28} "
                                 f"{arch:<14} {size} bytes")
        except OSError as e:
            lines.append(f"  (couldn't list: {e})")
    pids = _chrome_pids_using_profile()
    lines.append("Chrome on profile "
                 + ("couldn't check" if pids is None
                    else ", ".join(map(str, pids)) if pids else "none"))
    return lines


def _is_bad_arch_error(e: BaseException) -> bool:
    """[Errno 86] Bad CPU type in executable, however Selenium wrapped it."""
    if getattr(e, "errno", None) == 86:
        return True
    low = str(e).lower()
    return "errno 86" in low or "bad cpu type" in low


def _setup_chrome_driver(version_main=None, reseed: bool = False):
    """Launch undetected-chromedriver against Prism's own persistent profile,
    seeded from the user's real Chrome the first time so their logins carry
    over."""
    import undetected_chromedriver as uc

    seed_profile(force=reseed)
    if not reseed and profile_is_seeded():
        ui.info("   🍪  reusing Prism's browser profile (logins persist "
                "between runs)")
    _clear_profile_locks()
    _release_profile()
    _prune_preferences()
    _ensure_session_restore()
    tmp = PROFILE_DIR

    # Built fresh per attempt: uc raises "you cannot reuse the ChromeOptions
    # object" on a second Chrome() with the same instance, because it appends
    # to the argument list rather than replacing it — so a retry needs its own.
    def _options():
        o = uc.ChromeOptions()
        o.page_load_strategy = "eager"
        o.add_argument("--profile-directory=Default")
        o.add_argument("--disable-blink-features=AutomationControlled")
        o.add_argument("--remote-allow-origins=*")
        if platform.system() == "Linux":
            # A handful of recent Linux Chrome builds lose the only WebDriver
            # tab while bringing up GPU compositing or under AppArmor / user
            # namespace restrictions. The browser window can remain visible,
            # which makes it look like Prism failed to open the agent link.
            # These keep normal visible Chrome while using the stable software
            # compositor and sandbox bypass for automated tabs.
            o.add_argument("--disable-gpu")
            o.add_argument("--disable-dev-shm-usage")
            o.add_argument("--no-sandbox")
            o.add_argument("--disable-setuid-sandbox")
        return o

    # Match the driver to Chrome. A pin exists to work around DETECTION
    # failing, not to override what is actually installed — and Chrome
    # auto-updates, so a pin set months ago goes stale silently. Driving
    # Chrome 150 with a 149 driver does not fail politely: Chrome trips an
    # internal check and dies about a second after launch, with a crash
    # report and nothing in Prism's output to explain it.
    detected = detect_chrome_version()
    if version_main and detected and version_main != detected:
        ui.warn(f"your pinned Chrome version is v{version_main} but v{detected} "
                f"is installed — using v{detected}. Run /chrome to update or "
                "clear the pin.")
        version_main = detected
    elif version_main is None:
        version_main = detected
    if version_main:
        ui.info(f"   🌐  targeting Chrome v{version_main}")

    # Drop a cached chromedriver that cannot drive this Chrome: the wrong
    # architecture, or a version behind the browser. undetected-chromedriver
    # reuses whatever it patched last time, which is exactly how a stale one
    # survives a Chrome update.
    uc_cache = _uc_cache_dir()
    if os.path.exists(uc_cache):
        for f in [x for x in os.listdir(uc_cache) if "chromedriver" in x.lower()]:
            fp = os.path.join(uc_cache, f)
            if not os.path.isfile(fp):
                continue
            # Apple Silicon only (_driver_wrong_arch answers "" elsewhere):
            # an Intel driver -- left by Rosetta, or carried over from an
            # old Mac -- cannot run on a Mac without Rosetta. Read from the
            # Mach-O header, not from `file`, so it cannot fail quietly.
            drop = _driver_wrong_arch(fp) or None
            if drop is None and version_main:
                try:
                    # chromedriver, unlike chrome, is a console binary on
                    # Windows and does print its version.
                    r = subprocess.run([fp, "--version"], capture_output=True,
                                       text=True, timeout=10,
                                       creationflags=_NO_WINDOW)
                    have = int(r.stdout.strip().split()[1].split(".")[0])
                    if have != version_main:
                        drop = f"built for Chrome v{have}, not v{version_main}"
                except Exception:
                    pass
            if drop:
                try:
                    os.remove(fp)
                    ui.info(f"   ♻️   replacing the cached driver — {drop}")
                except OSError as e:
                    # Windows refuses to unlink a running executable. Say so
                    # and let uc try it and report the real failure.
                    ui.warn(f"   couldn't replace the cached driver ({drop}): {e}")

    # On Apple silicon the driver is Prism's own arm64 download; uc only
    # patches it. Empty everywhere else -- and when the download failed,
    # which on a Mac without Rosetta is a stop, not a fallback.
    own_driver = _apple_silicon_driver(version_main)
    _refuse_intel_fallback(own_driver)

    # Prism's own finder knows about ~/Applications; uc's checks
    # /Applications only. So a per-user Chrome install (no admin rights)
    # opened Login tabs fine and then failed every run with "could not
    # determine browser executable". Tell uc where the browser is.
    chrome = next((c for c in _CHROME_BINARIES if os.path.exists(c)), None)

    def _kwargs(driver_path):
        kw = {}
        if driver_path:
            kw["driver_executable_path"] = driver_path
        if chrome:
            kw["browser_executable_path"] = chrome
        return kw

    try:
        drv = uc.Chrome(options=_options(), user_data_dir=tmp,
                        version_main=version_main, **_kwargs(own_driver))
        try:
            drv.set_page_load_timeout(35)
        except Exception:
            pass
        _reset_to_blank_tab(drv)
        # Give Chrome a beat to finish creating its first renderer.  Without
        # this, a launch whose only tab immediately disappears looks healthy
        # here and fails much later at ``driver.get(...)`` in Content.
        time.sleep(1)
        if not _driver_has_live_tab(drv):
            raise RuntimeError("Chrome opened but closed its controlled tab")
        return drv
    except Exception as e:
        # One retry, because the two most common failures here are both
        # recoverable and neither is a broken installation:
        #
        #   · A Chrome appeared on the profile between _release_profile()
        #     above and this launch — Login tabs opened in another window,
        #     say. Clear it and the second attempt gets its own browser.
        #   · The driver for THIS major version cannot be had. Chrome
        #     auto-updates roughly monthly and Chrome-for-Testing publishes
        #     the matching chromedriver a little behind it, so for a day or
        #     two `version_main=N` resolves to nothing (uc raises a bare
        #     KeyError with the version number in it, and nothing else).
        #     Unpinned, uc takes the latest stable driver instead, which
        #     drives the new Chrome in every case the pin was going to fail.
        _release_profile()
        bad_arch = _is_bad_arch_error(e)
        if bad_arch:
            # The driver that was just executed is for the other CPU. Throw
            # uc's cache away, and Prism's own copy if that is what ran, so
            # the retry starts from a fresh download.
            _purge_uc_cache("it was built for another kind of Mac")
            if own_driver:
                try:
                    os.remove(own_driver)
                except OSError:
                    pass
            own_driver = _apple_silicon_driver(version_main)
            _refuse_intel_fallback(own_driver)
        elif version_main is not None:
            ui.warn(f"   couldn't start Chrome for v{version_main} — retrying "
                    "with whatever driver is current")
        try:
            drv = uc.Chrome(options=_options(), user_data_dir=tmp,
                            version_main=version_main if bad_arch else None,
                            **_kwargs(own_driver))
            try:
                drv.set_page_load_timeout(35)
            except Exception:
                pass
            _reset_to_blank_tab(drv)
            time.sleep(1)
            if not _driver_has_live_tab(drv):
                raise RuntimeError("Chrome opened but closed its controlled tab")
            return drv
        except Exception as retry_error:
            e = retry_error   # the newer, and after a version drop the truer, one

        # The raw Selenium traceback tells a business owner nothing, and this
        # is the first thing that happens when they press Start the work.
        #
        # Two lines of it, not one: Selenium puts the failure on line one
        # ("session not created: cannot connect to chrome at 127.0.0.1:53695")
        # and WHY on line two ("from chrome not reachable"), and keeping only
        # the first is what made every one of these look identical in the
        # screenshots customers send.
        lines = [ln.strip() for ln in str(e).strip().splitlines() if ln.strip()]
        detail = " — ".join(lines[:2])[:240]
        low = detail.lower()

        # Three different failures used to be reported as one, and the one
        # they were all reported as — "Chrome updated and the driver hasn't
        # caught up" — sent people to update a browser that was fine.
        if _is_bad_arch_error(e):
            why = _INTEL_DRIVER_HELP
        elif "could not determine browser executable" in low:
            why = ("Prism drives Google Chrome, and it isn't installed here.\n\n"
                   "  1. Install Google Chrome from google.com/chrome.\n"
                   "  2. Start it once, then try again.\n\n"
                   "Chrome specifically — Prism cannot use Edge, Safari or "
                   "Firefox.")
        elif "cannot connect to chrome" in low or "chrome not reachable" in low:
            why = ("Chrome started and then closed again before Prism could "
                   "reach it.\n\n"
                   "  1. Close every Chrome window — including any Prism "
                   "opened for signing in — and try again.\n"
                   + (f"     {_MAC_QUIT_CHROME}\n"
                      if platform.system() == "Darwin" else "")
                   + "  2. If it keeps happening, restart the computer; that "
                   "clears it for certain.\n"
                   "  3. Some antivirus and 'endpoint protection' tools block "
                   "one program from starting another. If you have one, allow "
                   "Prism through it.")
        else:
            why = ("This is almost always a version mismatch — Chrome updated "
                   "itself and the matching driver isn't available yet.\n\n"
                   "  1. Update Chrome (Chrome menu → About Google Chrome) and "
                   "try again.\n"
                   "  2. If you have pinned a version in Settings → Chrome, "
                   "clear that box so Prism detects it automatically.")

        raise RuntimeError(
            "Prism couldn't start Chrome.\n\n" + why + "\n\n"
            + (f"Technical detail: {detail}" if detail else "")) from e


def _needed_stages(routing: dict, agents: dict):
    """Yield (stage, agent_name, questions) for every stage that should run."""
    for stage in A.PIPELINE_ORDER:
        data = routing.get(stage)
        if not data or not data.get("needed", False):
            continue
        questions = [q for q in data.get("questions", []) if q and q.strip()]
        if not questions:
            continue
        if stage == "summary":
            name = A.summary_agent_name(agents)
        else:
            # A guardrail (e.g. router.apply_studio_guardrail) may have
            # picked a different tool for THIS run only — a structural
            # mismatch between the brief and the configured tool, not
            # something worth changing the user's saved setting over. Their
            # own config still wins whenever no override was set.
            name = data.get("agent_override") or agents.get(stage)
        if not name:
            continue
        yield stage, name, questions


def _reel_imagery_on(routing: dict, cfg: dict) -> bool:
    """Does this reel get generated pictures?

    Three voices, strongest last: Prism's default (yes), the customer's own
    setting, and what the person asked for in this request — "type only" and
    "no images" are honoured, and router.apply_reel_imagery_guardrail is
    where that reading happens. Kept as one function so the Studio and
    Motion paths cannot answer it differently, which is how one of them
    ended up with no image step at all.
    """
    # The router can only turn the pictures OFF (the person said "type
    # only"). Its "on" is the default restated, and letting it win overrode
    # a customer's own reel_imagery: false.
    if (routing or {}).get("_reel_imagery") is False:
        return False
    return bool(cfg.get("reel_imagery", True))


# Buttons a chat composer puts on a staged attachment chip. Generic on
# purpose: the tools rename their classes weekly, but a remove control keeps
# an accessible name, and "remove" / "delete" / "dismiss" is what it says.
_STAGED_REMOVE_JS = """
const sel = arguments[0];
const box = (() => {
  const ta = sel ? document.querySelector(sel) : null;
  let el = ta;
  while (el && el !== document.body) {
    if (el.querySelector && el.querySelector("input[type='file']")) return el;
    el = el.parentElement;
  }
  return ta ? (ta.closest('form') || ta.parentElement) : null;
})();
if (!box) return -1;
const pat = /(remove|delete|dismiss|clear)\\b/i;
const send = /(send|submit)/i;
const hits = [];
for (const b of box.querySelectorAll('button, [role="button"]')) {
  const name = (b.getAttribute('aria-label') || b.getAttribute('title') ||
                b.getAttribute('data-testid') || '').trim();
  if (!name || send.test(name) || !pat.test(name)) continue;
  if (b.offsetParent === null && !b.getClientRects().length) continue;
  hits.push(b);
}
for (const b of hits) { try { b.click(); } catch (e) {} }
return hits.length;
"""


def _clear_staged_attachments(driver, agent_cfg, agent_name: str = "") -> int:
    """Take off whatever the composer already holds before this stage's own
    files go up. Returns how many chips were removed (-1: no composer found).

    The owner's run of 11 Sep 2026: one PDF attached in Prism, and Claude's
    message went out with that PDF AND a deck outline from a reel run days
    earlier. Nothing in Prism sent the deck -- the content step only ever
    uploads the person's own files -- but claude.ai keeps an unsent draft,
    attachments included, and shows it again on the next new chat. A run
    that staged a file and never sent (a stop, a crash, a timeout) leaves
    that chip behind for the next run to send along. So: look for the
    remove control on every chip inside the composer and press it, before
    the upload, every time. A tool with no chips answers 0 and nothing
    happens; a tool with no composer answers -1 and nothing happens.
    """
    try:
        n = driver.execute_script(_STAGED_REMOVE_JS,
                                  agent_cfg.get("textarea_selector") or "")
    except Exception:                                   # noqa: BLE001
        return -1
    try:
        n = int(n)
    except (TypeError, ValueError):
        return -1
    if n > 0:
        ui.warn(f"   🧹  removed {n} attachment(s) that {agent_name or 'the tool'} "
                "still had staged from an earlier run — only this task's files go up")
    return n


def _dispatch_upload_events(driver, input_el) -> None:
    """Dispatch native and synthetic input/change events to inform React/Vue
    listeners that files have been attached to a hidden file input."""
    try:
        driver.execute_script("""
            const el = arguments[0];
            if (!el) return;
            el.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
            el.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
        """, input_el)
    except Exception:
        pass


def _candidate_inputs(driver, inputs):
    """Sort file input elements to prioritize composer attachments over avatar/profile inputs."""
    def _score(inp):
        score = 0
        try:
            if inp.get_attribute("multiple"):
                score += 10
            accept = (inp.get_attribute("accept") or "").lower()
            if accept:
                score += 5
            name_id = ((inp.get_attribute("name") or "") +
                       (inp.get_attribute("id") or "") +
                       (inp.get_attribute("class") or "")).lower()
            if any(term in name_id for term in ("avatar", "profile", "icon", "user_image")):
                score -= 20
            in_composer = driver.execute_script("""
                const el = arguments[0];
                if (!el) return false;
                return !!el.closest("form, [role='presentation'], [class*='composer'], [class*='chat']");
            """, inp)
            if in_composer:
                score += 15
        except Exception:
            pass
        return score
    return sorted(inputs, key=_score, reverse=True)


def _verify_page_attachments(driver, basenames: list[str], timeout: float = 6.0) -> tuple[int, list[str], str]:
    """Poll the page DOM to verify that uploaded files actually landed in the web UI.

    Checks:
    1. Attachment chips/cards/previews in or near the composer.
    2. Presence of uploaded file basenames in the page text/DOM.
    3. Active upload progress bars/spinners (indicating ingestion has begun).
    4. Rejection/error alerts (e.g. 'unsupported file', 'file too large').

    Returns (verified_count, verified_names, error_detail).
    """
    if not basenames:
        return 0, [], ""
    end_time = time.time() + timeout
    last_res = {"matched_names": [], "chips_count": 0, "has_busy": False, "error_msg": ""}

    js_check = """
        const names = arguments[0] || [];
        const res = {
            matched_names: [],
            chips_count: 0,
            has_busy: false,
            error_msg: ""
        };
        // 1. Check for visible error alerts/messages
        const alertSels = "[role='alert'], [class*='error'], [class*='alert'], [data-testid*='error'], .text-red-500, .toast-error";
        for (const el of document.querySelectorAll(alertSels)) {
            if (el.offsetParent !== null) {
                const txt = (el.innerText || el.textContent || '').trim();
                if (/upload|file|attachment|size|large|unsupported|type|failed|error/i.test(txt)) {
                    res.error_msg = txt.slice(0, 160);
                    break;
                }
            }
        }
        // 2. Check for active upload progressbars / spinners
        const busySels = "[role='progressbar'], progress, .animate-spin, [aria-busy='true'], [data-testid*='progress'], [class*='progress'], [class*='spinner']";
        for (const el of document.querySelectorAll(busySels)) {
            if (el.offsetParent !== null) {
                res.has_busy = true;
                break;
            }
        }
        // 3. Check for attachment chips/cards
        const chipSels = "[data-testid*='attachment'], [data-testid*='file'], [aria-label*='Remove' i], [aria-label*='Delete' i], [aria-label*='remove' i], .attachment-item, [class*='attachment'], [class*='FileCard'], [class*='FileThumbnail'], [class*='file-tile'], [class*='file_thumbnail']";
        const chips = Array.from(document.querySelectorAll(chipSels)).filter(el => el.offsetParent !== null);
        res.chips_count = chips.length;

        // 4. Check for file basenames in the visible page text or attributes
        const bodyText = (document.body && (document.body.innerText || document.body.textContent)) || "";
        for (const name of names) {
            if (bodyText.includes(name)) {
                res.matched_names.push(name);
            } else {
                const esc = (window.CSS && CSS.escape) ? CSS.escape(name) : name.replace(/["\\\\]/g, '\\\\$&');
                const hasAttr = !!document.querySelector(`[title*="${esc}"], [aria-label*="${esc}"]`);
                if (hasAttr) {
                    res.matched_names.push(name);
                }
            }
        }
        return res;
    """
    executed_dom_check = False
    # A real upload lands progressively -- the browser reads and thumbnails
    # each file in turn, so a 6-file batch can still be two chips deep half
    # a second in. Breaking on the FIRST non-empty poll (the old behaviour)
    # reported whatever partial count happened to be visible at that one
    # instant as final -- a real run of six photos came back "only 2 of 6
    # attachment(s) reached ChatGPT" when the other four would have shown up
    # within another second or two of the SAME upload, not a separate
    # failure. Now: keep the BEST count any poll has seen, and only stop
    # once it has held steady (no growth, nothing still marked busy) for a
    # short grace window, or every file is accounted for, or time runs out.
    STABLE_GRACE = 1.2
    best = {"matched_names": [], "chips_count": 0, "has_busy": False, "error_msg": ""}
    best_signal = 0
    stable_since = None
    while time.time() < end_time:
        try:
            data = driver.execute_script(js_check, basenames)
        except Exception:
            data = None
        if isinstance(data, dict):
            executed_dom_check = True
            matched = data.get("matched_names") or []
            chips_cnt = data.get("chips_count", 0) or 0
            has_busy = bool(data.get("has_busy"))
            signal = max(len(matched), chips_cnt,
                        len(basenames) if has_busy else 0)
            if signal > best_signal:
                best_signal = signal
                best = data
                stable_since = time.time()
            elif (signal == best_signal and best_signal > 0 and not has_busy
                  and stable_since is not None
                  and time.time() - stable_since >= STABLE_GRACE):
                break
            if data.get("error_msg") and not best.get("error_msg"):
                best["error_msg"] = data["error_msg"]
            if best_signal >= len(basenames):
                break
        time.sleep(0.5)

    if not executed_dom_check:
        return len(basenames), list(basenames), ""

    matched = best.get("matched_names", [])
    chips_cnt = best.get("chips_count", 0)
    has_busy = best.get("has_busy", False)
    err = best.get("error_msg", "")

    if matched:
        count = len(matched)
    elif chips_cnt > 0:
        count = min(chips_cnt, len(basenames))
    elif has_busy:
        count = len(basenames)
    else:
        count = 0

    return count, matched, err


def _upload_files(driver, agent_cfg, attachments, agent_name: str = ""):
    """Push any attached files into the tool's <input type='file'>, if present.

    Returns the number of files that actually reached the page. A caller that
    ignores the return value still gets the failure through the usual channel
    — ui.warn() — because a customer's drawing or PO silently never reaching
    the agent is exactly the kind of thing that must not fail quietly: the
    pipeline would otherwise carry on as if the attachment had gone up, and
    nobody finds out until the response makes no sense.
    """
    if not attachments:
        return 0
    from selenium.webdriver.common.by import By
    from . import files as F

    who = agent_name or "this tool"
    sel = agent_cfg.get("upload_selector", "input[type='file']")
    if not sel:
        # The tool takes its files another way — NotebookLM pastes them in
        # as sources itself (_nb_sources) — and says so with "" here, so
        # this is not the "answer blind" warning below.
        return 0

    # Some composers never mount <input type='file'> at all until an attach
    # button is used first — confirmed live on Google Gemini (22 Sep 2026,
    # by reading its own DOM): a freshly loaded page has ZERO file inputs
    # anywhere on it, not a hidden or late one, until its own composer '+'
    # is clicked. No amount of waiting for `sel` below ever finds one on a
    # page like that; a real run reported "Google Gemini has no
    # file-upload field on this page" for exactly this reason, on every
    # attempt, not an occasional late-render miss the WebDriverWait
    # already covers. Silent and best-effort: a tool that already has its
    # input present is unaffected, and a stale trigger selector just does
    # nothing rather than breaking the tools that never needed one.
    for trig_sel in agent_cfg.get("upload_trigger_selectors", ()):
        try:
            trig = driver.find_elements(By.CSS_SELECTOR, trig_sel)
            if trig and trig[0].is_displayed():
                trig[0].click()
                time.sleep(0.6)
                break
        except Exception:
            continue

    # WAIT for it, don't just look once. run() navigates to the tool and
    # sleeps `page_wait` (4 seconds by default) before getting here, and four
    # seconds is not how long chatgpt.com or claude.ai take to render their
    # composer on a cold profile — which is every Prism profile, since it
    # keeps its own. The typing path has always waited properly for
    # `textarea_selector` (WebDriverWait, input_wait, 15-30s); the upload path
    # looked exactly once and, finding nothing, reported the tool as having no
    # upload field at all and carried on without the customer's drawing. Same
    # page, same moment, two different answers — the difference was only that
    # one of them waited.
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    try:
        WebDriverWait(driver, agent_cfg.get("upload_wait", 20)).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
    except Exception:
        pass        # fall through to the same message as before
    inputs = driver.find_elements(By.CSS_SELECTOR, sel)
    if not inputs:
        ui.warn(f"   ⚠️   {who} has no file-upload field on this page — "
                f"{len(attachments)} attachment(s) were NOT sent; it will "
                "answer blind to them")
        return 0
    paths = F.upload_paths(attachments)
    # ChromeDriver rejects the WHOLE send when any one path is missing
    # ("invalid argument: File not found"), so one stale entry took every
    # other attachment down with it. Files harvested from an earlier stage
    # live in the system temp directory, which Windows and macOS both clean
    # underneath a long run — so this is a normal thing to meet, not a
    # corrupt state, and the right answer is to send the rest and say which
    # one went.
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        paths = [p for p in paths if os.path.isfile(p)]
        ui.warn(f"   ⚠️   {len(missing)} attachment(s) are no longer on disk "
                f"and can't be sent to {who}: "
                + ", ".join(os.path.basename(p) for p in missing[:3])
                + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""))
    if not paths:
        return 0

    sorted_inputs = _candidate_inputs(driver, inputs)
    target = sorted_inputs[0]
    basenames = [os.path.basename(p) for p in paths]
    verified_count = 0
    verified_names: list[str] = []
    error_detail = ""
    # Each file's own preview takes a moment to render, several MB of real
    # photos more than a moment — a fixed 5s window undercounted a 6-file
    # batch of camera photos that was still legitimately rendering when the
    # window closed. Scales with how many files there actually are to wait
    # for; the stability check above still returns early once they are all
    # in, so this only matters when the upload is genuinely still working.
    verify_timeout = min(24.0, 5.0 + 1.5 * max(0, len(paths) - 1))

    # Attempt bulk upload
    try:
        target.send_keys("\n".join(paths))
        _dispatch_upload_events(driver, target)
        verified_count, verified_names, error_detail = _verify_page_attachments(
            driver, basenames, timeout=verify_timeout)
    except Exception as e:
        bulk_reason = str(e).strip().splitlines()[0][:120] if str(e).strip() else "no detail"

    # Fall back to one-at-a-time or alternative candidate inputs if bulk produced 0 verified
    if verified_count == 0 and not error_detail:
        for p in paths:
            sent = False
            for inp in sorted_inputs:
                try:
                    inp.send_keys(p)
                    _dispatch_upload_events(driver, inp)
                    sent = True
                    break
                except Exception:
                    pass
        if sent:
            verified_count, verified_names, error_detail = _verify_page_attachments(
                driver, basenames, timeout=max(4.0, verify_timeout - 1.0))

    # Verification outcome check
    if verified_count == 0:
        if error_detail:
            ui.warn(f"   ⚠️   0 of {len(paths)} attachment(s) reached {who} — "
                    f"page reported: {error_detail}")
        else:
            ui.warn(f"   ⚠️   0 of {len(paths)} attachment(s) reached {who} — "
                    "the page did not accept or display the attached files; "
                    "it will answer blind to them")
        return 0

    uploaded = verified_count
    ui.info(f"   📎  verified {uploaded} file(s) attached to {who}")
    if uploaded < len(paths):
        ui.warn(f"   ⚠️   only {uploaded} of {len(paths)} attachment(s) reached {who} — "
                "the rest never appeared on the page")

    # A brief settle pause so the page composer registers the attached file chips
    time.sleep(2)
    return uploaded


def _fast_type(driver, element, text: str) -> bool:
    """Insert the whole prompt at once via JavaScript instead of per-character
    send_keys (which crawls at ~20 chars/sec over the WebDriver wire — minutes
    for a long context). Handles <textarea>/<input> through the native value
    setter (so React/Vue notice the change) and contenteditable editors through
    execCommand('insertText'), which fires real input events.
    Returns False if the text didn't land, so the caller can fall back."""
    try:
        element.click()
    except Exception:
        pass
    try:
        return bool(driver.execute_script(
            """
            const el = arguments[0], text = arguments[1];
            el.focus();
            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                const proto = el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
                el.dispatchEvent(new Event('input',  {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return el.value === text;
            }
            document.execCommand('selectAll', false, null);
            const ok = document.execCommand('insertText', false, text);
            return ok && (el.innerText || el.textContent || '').trim().length > 0;
            """,
            element, text,
        )) and _text_landed(_composer_text(driver, element), text)
    except Exception:
        return False


def _composer_text(driver, element) -> str:
    """What is ACTUALLY in the box right now."""
    try:
        return driver.execute_script(
            "const el = arguments[0];"
            "return (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')"
            " ? (el.value || '') : (el.innerText || el.textContent || '');",
            element) or ""
    except Exception:
        return ""


def _text_landed(actual: str, wanted: str) -> bool:
    """Did the prompt reach the box — not "is the box non-empty".

    That distinction is the bug this exists for. _fast_type's contenteditable
    branch returned `ok && innerText.trim().length > 0`, so ANY text in the
    editor counted as success: a partial insert, the previous prompt still
    sitting there, or text that execCommand put somewhere else entirely
    (execCommand acts on the document's selection, which after a file upload
    is not reliably inside the composer). The textarea branch had always
    checked `el.value === text` properly; only the editor every major tool
    actually uses was taken on trust.

    Whitespace-normalised and not exact, because a rich editor legitimately
    reflows blank lines. The bulk of the text and its opening have to be
    there, which no near-miss above passes.
    """
    tidy = lambda s: " ".join((s or "").split())
    a, w = tidy(actual), tidy(wanted)
    if not w:
        return True
    return len(a) >= int(len(w) * 0.9) and w[:60] in a


def _prompt_was_sent(driver, element, wanted: str, timeout: int = 12) -> bool:
    """Did the prompt actually go, or is it still sitting in the box?

    Every chat tool empties its composer when it accepts a message, so the
    prompt still being there after the submit is the signal that nothing
    happened. Worth checking because the submit had no check at all:
    `submitted = True` was set because `btn.click()` did not raise, and when
    the composer is empty ChatGPT has no send button to click — so the
    element_to_be_clickable wait timed out, the code fell through to ENTER on
    an empty box, that did nothing, and Prism went on to wait 300 seconds for
    a reply to a prompt it had never sent.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _text_landed(_composer_text(driver, element), wanted):
            return True
        time.sleep(0.5)
    return False


def _submit_prompt(driver, agent_cfg: dict, agent_name: str, prompt: str,
                   *, has_attachments: bool = False) -> None:
    """Put a prompt in an agent composer and prove the site accepted it.

    Keeping this operation together matters for recovery: a ChromeDriver
    failure before this function returns means Prism has not established that
    the tool received the prompt, so it is safe for the caller to reopen the
    tab and make one fresh attempt.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    operation = "waiting for the message box"
    try:
        textarea = WebDriverWait(driver, agent_cfg.get("input_wait", 15)).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, agent_cfg["textarea_selector"]))
        )
        try:
            textarea.clear()
        except Exception:
            pass

        operation = "putting the prompt in the message box"
        if not _fast_type(driver, textarea, prompt):
            # JS insertion did not take on this site — fall back to
            # per-keystroke typing (slow but universal).
            lines = prompt.split("\n")
            for i, line in enumerate(lines):
                if line:
                    textarea.send_keys(line)
                if i < len(lines) - 1:
                    textarea.send_keys(Keys.SHIFT, Keys.ENTER)
        time.sleep(1)

        # Uploading or starting a fresh chat may re-render the composer.  Do
        # not let a detached element turn an empty-looking composer into a
        # false successful send.
        operation = "checking that the prompt landed"
        if not _text_landed(_composer_text(driver, textarea), prompt):
            textarea = driver.find_element(By.CSS_SELECTOR,
                                           agent_cfg["textarea_selector"])
            _fast_type(driver, textarea, prompt)
            time.sleep(1)
        if not _text_landed(_composer_text(driver, textarea), prompt):
            raise RuntimeError(
                f"the prompt would not go into {agent_name}'s message box — "
                "nothing was sent")

        # A new-chat composer can become visible before React has wired it to
        # the send action.  Give the page a short, explicit settle after text
        # is proven present.
        time.sleep(1.5)

        operation = "submitting the prompt"
        selectors = [s.strip() for s in
                     agent_cfg.get("submit_selector", "").split(",")
                     if s.strip()]
        for fallback_sel in ("button[data-testid='send-button']",
                             "button[aria-label*='Send']",
                             "button[type='submit']"):
            if fallback_sel not in selectors:
                selectors.append(fallback_sel)

        deadline = time.time() + (35 if has_attachments else 12)
        sent = False
        while time.time() < deadline:
            for selector in selectors:
                try:
                    for button in driver.find_elements(By.CSS_SELECTOR, selector):
                        disabled = driver.execute_script(
                            "return arguments[0].disabled || "
                            "arguments[0].getAttribute('aria-disabled') === 'true';",
                            button)
                        if not disabled and button.is_displayed():
                            try:
                                button.click()
                            except Exception:
                                driver.execute_script("arguments[0].click();", button)
                            if _prompt_was_sent(driver, textarea, prompt, timeout=2):
                                sent = True
                                break
                    if sent:
                        break
                except Exception:
                    pass
            if sent:
                break
            try:
                textarea.send_keys(Keys.ENTER)
                if _prompt_was_sent(driver, textarea, prompt, timeout=2):
                    sent = True
                    break
            except Exception:
                pass
            time.sleep(1)

        operation = "confirming that the prompt was accepted"
        if not sent and not _prompt_was_sent(driver, textarea, prompt, timeout=6):
            raise RuntimeError(
                f"{agent_name} would not accept the prompt — it is still "
                "sitting in the message box")
    except Exception as error:
        # Selenium's native errors otherwise say only "Message:" and an
        # address-only stack trace.  The operation turns that into a useful
        # diagnostic without hiding the original exception as __cause__.
        raise RuntimeError(f"{agent_name}: {operation}: {error}") from error
def _within(selector_list: str, descendant: str) -> str:
    """"<list> img" done properly — `<a> img, <b> img`, not `<a>, <b> img`.

    A response_selector is a selector LIST: ChatGPT's is three alternatives,
    Claude's four, because those sites serve different markup to different
    accounts on the same day. `f"{sel} img"` reads as "everything in the list
    EXCEPT the last one, plus images inside the last one" — the descendant
    combinator binds tighter than the comma. Written out, ChatGPT's came to:

        [data-message-author-role='assistant'],      ← the message DIV itself
        [data-message-role='assistant'],             ← ditto
        .markdown.prose img                          ← only this branch scoped

    So the harvesters got message CONTAINERS back, not images or links. Both
    then treated a <div> as the thing they were looking for: `get_attribute
    ("src")` and `get_attribute("href")` are None on a div, so every one was
    skipped — and because the list HAD matched something, neither fell back
    to searching the whole page. The net effect was that a generated image
    and a generated .docx were invisible to Prism on the two most-used tools
    in the registry, every single time.

    Splitting is comma-aware of brackets, parens and quotes: `[data-x='a,b']`
    and `:is(.a, .b)` are both legal in a selector and both contain a comma
    that does not end an alternative.
    """
    parts, current, depth, quote = [], [], 0, ""
    for char in selector_list:
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return ", ".join(f"{p.strip()} {descendant}" for p in parts if p.strip())


def _harvest_images(driver, agent_cfg, stage: str,
                    exclude_files: list | None = None) -> list[dict]:
    """A generated image can't travel in a text handoff. Pull every real image
    out of the response area — fetched through the page's own session so
    auth-gated CDN links work — and return attachment records that later
    stages can re-upload. Falls back to screenshotting the rendered element."""
    import base64
    import hashlib
    from selenium.webdriver.common.by import By

    # Compute fingerprints of existing user attachments and previous files so
    # user-uploaded input pictures on the page are NEVER mistaken for
    # newly-generated artwork.
    exclude_hashes = set()
    exclude_sizes = set()
    exclude_stems = set()
    for item in (exclude_files or []):
        p = item.get("path") if isinstance(item, dict) else item
        name = item.get("name") if isinstance(item, dict) else ""
        if p and os.path.exists(p):
            try:
                sz = os.path.getsize(p)
                exclude_sizes.add(sz)
                with open(p, "rb") as ef:
                    exclude_hashes.add(hashlib.md5(ef.read()).hexdigest())
            except Exception:
                pass
        for cand in (p, name):
            if cand:
                base = os.path.basename(str(cand))
                stem = os.path.splitext(base)[0].lower().strip()
                if len(stem) >= 3:
                    exclude_stems.add(stem)

    # Inside the reply first — that is where a generated image usually sits and
    # the ordering there is the order it was asked for. But ChatGPT's image UI
    # opens a canvas pane BESIDE the conversation, putting the pictures outside
    # the message element entirely, so fall back to the whole page rather than
    # reporting that a stage produced nothing while three images are on screen.
    sel = agent_cfg.get("response_selector", "")
    imgs = []
    try:
        if sel:
            imgs = driver.find_elements(By.CSS_SELECTOR, _within(sel, "img"))
        if not imgs:
            imgs = driver.find_elements(By.CSS_SELECTOR, "img")
    except Exception:
        return []

    out, seen = [], set()
    try:
        driver.set_script_timeout(20)
    except Exception:
        pass
    for img in imgs:
        try:
            # Strictly verify this image is a genuine generated assistant image,
            # NOT a user attachment, prompt thumbnail, web search citation, or avatar.
            is_valid_generated = driver.execute_script(r"""
                const img = arguments[0];
                if (!img) return false;
                const src = img.currentSrc || img.src || '';
                if (!src) return false;
                // Exclude user-turn prompts, attachment chips, composers
                if (img.closest(
                    '[data-message-author-role="user"], [data-message-role="user"], ' +
                    '.user-message, form, [data-testid*="composer"], [data-testid*="attachment"], ' +
                    '[data-testid*="user-message"], [data-testid*="user-turn"], [class*="user-turn"]'
                )) return false;

                // Immediate pass: OpenAI DALL-E CDN URLs are ALWAYS generated assistant images
                const isOpenAiDalle = /oaidalleapiprodscus\.blob\.core\.windows\.net|files\.oaiusercontent\.com|chatgpt\.com\/backend-api\/files/i.test(src);
                if (isOpenAiDalle) return true;

                const turn = img.closest('article, section[data-testid^="conversation-turn-"], [data-testid^="conversation-turn-"], [class*="conversation-turn"]');
                if (turn && turn.querySelector('[data-message-author-role="user"], [data-message-role="user"]')) {
                    return false;
                }
                // Exclude web search citations, source cards, search result thumbnails
                if (img.closest(
                    'a[href^="http://"], a[href^="https://"]:not([href*="chatgpt.com"]):not([href*="openai.com"]), ' +
                    '[data-testid*="source"], [data-testid*="citation"], [class*="citation"], ' +
                    '[class*="source-card"], [class*="attribution"], [class*="search-result"], ' +
                    '[aria-label*="source" i], [aria-label*="citation" i], details, [class*="searched"]'
                )) return false;
                // Exclude UI avatars, navbars, and toolbar icon buttons
                if (img.closest(
                    '[class*="avatar"], [data-testid*="avatar"], nav, header, footer, ' +
                    'button[aria-label*="copy" i], button[aria-label*="thumb" i], button[aria-label*="feedback" i]'
                )) return false;
                const alt = (img.getAttribute('alt') || '').toLowerCase();
                if (alt.includes('avatar') || alt === 'user' || alt === 'chatgpt') return false;
                const rect = img.getBoundingClientRect();
                if (rect.width > 0 && rect.width < 120 && rect.height > 0 && rect.height < 120) return false;
                return true;
            """, img)
            if not is_valid_generated:
                continue

            alt = (img.get_attribute("alt") or "").lower()
            title = (img.get_attribute("title") or "").lower()
            aria = (img.get_attribute("aria-label") or "").lower()
            if any(stem in alt or stem in title or stem in aria for stem in exclude_stems):
                continue

            src = img.get_attribute("src") or ""
            w = driver.execute_script("return arguments[0].naturalWidth || 0", img)
            h = driver.execute_script("return arguments[0].naturalHeight || 0", img)
        except Exception:
            continue
        # Icons, avatars and citation thumbnails are small — real generated
        # images aren't.
        if not src or src in seen or w < 256 or h < 256:
            continue
        seen.add(src)
        raw, mime = None, "image/png"
        try:
            data = driver.execute_async_script(
                """
                const src = arguments[0], done = arguments[arguments.length - 1];
                fetch(src, {credentials: 'include'})
                    .then(r => r.blob())
                    .then(b => { const fr = new FileReader();
                                 fr.onloadend = () => done(fr.result);
                                 fr.readAsDataURL(b); })
                    .catch(() => done(null));
                """, src)
            if data and data.startswith("data:"):
                header, b64 = data.split(",", 1)
                raw = base64.b64decode(b64)
                mime = header[5:].split(";")[0] or "image/png"
        except Exception:
            raw = None
        if not raw:
            try:
                raw = img.screenshot_as_png   # rendered pixels — always works
            except Exception:
                continue
        if not mime.startswith("image/"):
            continue

        # Check binary fingerprint: if this image matches an uploaded user file,
        # skip it immediately.
        if raw:
            raw_hash = hashlib.md5(raw).hexdigest()
            if raw_hash in exclude_hashes or len(raw) in exclude_sizes:
                continue

        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
        path = os.path.join(tempfile.gettempdir(),
                            f"prism_{stage}_img{len(out) + 1}{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        out.append({"path": path, "name": os.path.basename(path), "size": len(raw),
                    "mime": mime, "kind": "image", "text": None,
                    "truncated": False,
                    # Marked so a later stage can tell a picture a model drew
                    # from a file the client actually owns — they are not
                    # interchangeable when one of them is a logo.
                    "_generated": True})
        if len(out) >= 8:
            break
    return out


# A link that plainly points at a real file — a document, deck, spreadsheet,
# archive or source file — rather than an ordinary navigational link. Kept
# narrow and shape-based on purpose, the same way _harvest_images only takes
# an <img> sized like a real picture: a chat reply that merely TALKS about a
# file must not count, only a link that plainly IS one.
_HARVESTABLE_EXTS = (
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv",
    ".odt", ".odp", ".ods", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".html", ".css",
    ".java", ".c", ".cpp", ".h", ".go", ".rb", ".php", ".sh", ".sql",
    ".ipynb", ".md", ".txt", ".rtf",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac",
)

# A blob: download button (ChatGPT/Claude canvas export) hands back an
# extension-less href and usually no `download` filename, so the deliverable
# used to land as "..._file1" with NO suffix — the OS typed it as a generic
# "File", Prism couldn't preview it, and it read as "the PDF wasn't taken" even
# though the bytes were right there. The real type is recoverable two ways: the
# data: URL FileReader returns names the MIME type, and failing that the bytes
# start with a recognisable magic number. This map covers the office/doc types
# `mimetypes.guess_extension` gets wrong or refuses cross-platform.
_MIME_EXT = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def _guess_ext(mime: str, raw: bytes) -> str:
    """Best-effort file extension when the download link gave none: trust the
    MIME type the data: URL declared, fall back to a magic-number sniff, else
    empty (caller keeps whatever it had)."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime and mime != "application/octet-stream":
        hit = _MIME_EXT.get(mime)
        if hit:
            return hit
        if mime.startswith("audio/mpeg") or mime == "audio/mp3":
            return ".mp3"
        if mime.startswith("audio/wav") or mime == "audio/x-wav":
            return ".wav"
        if mime.startswith("audio/ogg"):
            return ".ogg"
        if mime.startswith("audio/mp4") or mime in ("audio/x-m4a", "audio/aac"):
            return ".m4a"
        import mimetypes
        guess = mimetypes.guess_extension(mime)
        if guess:
            return ".jpg" if guess == ".jpe" else guess
    # Content sniff — a blob download often reports octet-stream regardless.
    if raw[:5] == b"%PDF-":
        return ".pdf"
    if raw[:2] == b"PK":
        return ".zip"          # also docx/pptx/xlsx, but .zip at least opens
    if raw[:4] == b"%!PS":
        return ".ps"
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return ".wav"
    if raw[:4] == b"OggS":
        return ".ogg"
    if raw[4:8] == b"ftyp":
        return ".m4a"
    return ""


def _download_attr(anchor) -> str | None:
    """The anchor's `download` ATTRIBUTE — None when it doesn't have one.

    `get_attribute("download")` cannot answer this. Selenium's getAttribute
    returns the DOM PROPERTY in preference to the attribute, and
    HTMLAnchorElement.download is a DOMString that reads back as "" on every
    anchor ever written, attribute or no attribute. So the obvious
    `download_attr is not None` test below was true for every <a href> on the
    page, and _harvest_files stopped being a filter at all: it fetched the
    first four links in DOM order — sidebar entries, "Help", the tool's own
    logo link — saved them extension-less (there is no extension to take from
    a nav link), and hit its cap of 4 long before reaching the .docx the
    stage had actually produced. That is the "documents and images aren't
    coming back from the agents" report, and both halves of it: the real file
    was never fetched, and what did get saved had no usable extension.

    get_dom_attribute() is the one that reads the attribute itself, and it
    keeps the distinction the caller needs: None when there is no attribute,
    "" for a bare `<a href="/api/file/9" download>` — which IS a deliverable,
    it just carries no filename to take an extension from. Guarded with
    getattr because it arrived in Selenium 4 and the engine's pin is only
    >=4.0.0; on that old path "" has to be read as absent, since the property
    fallback cannot tell the two apart and treating it as present is the very
    bug this exists to fix.
    """
    getter = getattr(anchor, "get_dom_attribute", None)
    try:
        if getter:
            return getter("download")
        return anchor.get_attribute("download") or None
    except Exception:
        return None


def _anchor_text(anchor) -> str:
    try:
        return (getattr(anchor, "text", "") or "").strip()
    except Exception:
        return ""


#: The "Document·DOCX" line on a generated-file card. Claude labels an
#: artifact by its TYPE, not by a filename — "Lazycook design brief" on one
#: line, "Document·DOCX" on the next, "Download" on the third — so nothing
#: in the card ends in ".docx" and every extension-based test walked past a
#: document the tool had plainly just built (run of 2026-09-08 15:07).
_CARD_TYPE_RE = re.compile(
    r"^(?:(?P<word>document|spreadsheet|presentation|code|text|file|audio|voice|speech)\s*)?"
    r"(?P<sep>[·•|]\s*)?"
    r"(?P<ext>docx?|pdf|pptx?|xlsx?|csv|md|txt|zip|json|py|ipynb|mp3|wav|m4a|ogg|flac|aac)\s*$", re.I)

#: Labels on a card that are controls, not the document's name.
_CARD_CONTROLS = ("download", "copy", "open", "preview", "publish", "share")


def _card_filename(text: str) -> str:
    """The filename a generated-file CARD implies, from its type label.

    Deliberately conservative: a bare "PDF" on a line is not enough, since a
    reply can say that about anything. The type must be introduced — by the
    word ("Document PDF") or by a separator ("· PDF") — which is how every
    card that has been seen actually writes it.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    ext, type_at = "", -1
    for i, line in enumerate(lines):
        m = _CARD_TYPE_RE.match(line)
        if m and (m.group("word") or m.group("sep")):
            ext, type_at = "." + m.group("ext").lower(), i
            break
    if not ext:
        return ""
    name = ""
    for line in reversed(lines[:type_at]):
        if line.lower() in _CARD_CONTROLS:
            continue
        name = line
        break
    return _safe_filename(name or "document")[:80] + ext


def _filename_in_text(text: str) -> str:
    """The filename an anchor SHOWS, when its href gives nothing away.

    ChatGPT's generated-file chip is `<a href="…/backend-api/…/content?id=
    file-…&sig=…">report.docx</a>`: an extension-less signed URL with no
    `download` attribute, and the only place the file's name and type
    appear is the link text. The old candidate test (extension in the
    href, a `download` attribute, or a blob: URL) let every one of those
    through as "an ordinary navigational link" -- so the document a tool
    had plainly produced was skipped, the click fallback found no button
    labelled "Download" (the chip is labelled with the filename), and the
    step was reported as "Prism couldn't read the response"."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= 160:
        low = text.lower()
        if any(low.endswith(ext) for ext in _HARVESTABLE_EXTS):
            return text
        for token in text.split():
            t = token.strip("()[],;:\"'")
            if any(t.lower().endswith(ext) for ext in _HARVESTABLE_EXTS) and len(t) > len(
                    os.path.splitext(t)[1]):
                return t
    # No extension anywhere — but a card that names its type is still a file.
    return _card_filename(text)


def _safe_filename(name: str) -> str:
    """A site's suggested filename, made safe for this machine."""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip(" .")
    return name[:120]


def _harvest_files(driver, agent_cfg, stage: str, ignore_names=(),
                   click_fallback: bool = True) -> list[dict]:
    """Non-image sibling of _harvest_images. A code, presentation or format
    stage's real deliverable is a DOCUMENT, DECK, CODE file or ARCHIVE, not a
    picture — that can't travel in a text handoff either, only the file
    itself can, and it joins the same pipeline_files list _harvest_images
    already feeds so a later producer stage receives it the same way.

    _harvest_images finds its candidates by shape — an <img> sized like a
    real picture rather than an icon or avatar. There is no equivalent
    universal signal for a document, so the candidate test is different (a
    link whose href plainly points at a file: a real extension, an explicit
    `download` attribute, or a `blob:` URL — how Code-Interpreter-style
    "Download" buttons usually work), but everything around that test is the
    SAME mechanism reused rather than reinvented: search the response area
    first, fall back to the whole page (a tool's download UI can sit outside
    the message bubble the same way ChatGPT's image canvas does), fetch the
    bytes through the page's OWN session so an auth-gated link still
    resolves, and hand back an attachment record built the normal way
    (core.files.attach, the same function a user's own upload goes through).

    Unlike an image, there is no "screenshot the element" fallback when the
    fetch fails — a rendered pixel grid can stand in for a picture; nothing
    can stand in for a PDF. A candidate that can't be fetched is skipped.

    For the "audio" stage specifically, an <a href> download link is often
    not how the result appears at all — ElevenLabs/Suno-style players
    typically hand back an <audio> element instead, with no anchor anywhere.
    Unverified against either tool's real DOM (no live account to test
    against), but harmless to try: `currentSrc` is read via JS because that
    reflects what the player actually loaded, not just a static `src`
    attribute a dynamic player may never set.
    """
    import base64
    from selenium.webdriver.common.by import By
    from . import files as F

    sel = agent_cfg.get("response_selector", "")
    candidates: list[tuple[str, str | None, str]] = []
    try:
        links = (driver.find_elements(By.CSS_SELECTOR, _within(sel, "a[href]"))
                 if sel else [])
        if not links:
            links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        for a in links:
            try:
                candidates.append((a.get_attribute("href") or "",
                                   _download_attr(a), _anchor_text(a)))
            except Exception:
                continue
    except Exception:
        return []

    if stage == "audio":
        try:
            srcs = driver.execute_script("""
                const urls = [];
                for (const el of document.querySelectorAll('audio, audio source')) {
                    const s = el.currentSrc || el.src;
                    if (s && (s.startsWith('blob:') || s.includes('.mp3') || s.includes('.wav') || s.includes('.m4a'))) {
                        urls.push(s);
                    }
                }
                for (const a of document.querySelectorAll('a[href$=".mp3"], a[href$=".wav"], a[href$=".m4a"], a[href*=".mp3?"], a[download*=".mp3"]')) {
                    if (a.href) urls.push(a.href);
                }
                return [...new Set(urls)];
            """) or []
            candidates.extend((src, None, "") for src in srcs)
        except Exception:
            pass

    # ChatGPT's newer file card: a container (not itself an <a>) that shows
    # a filename and type label ("Delta_Report... PDF").  The download link
    # may be an <a> INSIDE the card, or the card itself may be clickable
    # with no anchor.  This pass looks for cards, digs out any nested <a>,
    # and adds them to the candidate list so they go through the same
    # extension/fetch logic.  If no <a> is found inside, the card's own
    # text is used so _filename_in_text and _card_filename can still pick
    # up the type label and flag it for the click-to-download fallback.
    try:
        _card_js = """
            const fileCardRe = /\\.(?:docx?|pdf|pptx?|xlsx?|csv|md|txt|zip|json|py|ipynb|mp3|wav|m4a|ogg|flac|aac)(?:\\s|\\n)*(?:document|spreadsheet|presentation|file|audio|voice)?\\s*$/im;
            const sels = [
                "[data-testid*='artifact' i]", "[class*='artifact' i]",
                "[data-testid*='file' i]", "[data-testid*='download' i]",
                "[class*='file-card' i]", "[class*='fileCard' i]",
            ];
            const byAttr = document.querySelectorAll(sels.join(', '));
            // Also sweep the assistant turn for any short element whose
            // text names a file type — the broadest net for an unknown card
            // shape, scoped to assistant turns so sidebar and nav are excluded.
            // ChatGPT's current DOM does not put a message-role attribute on
            // every assistant turn.  Its turn has an `agent-turn` child
            // instead.  Scope this scan to the LAST assistant reply: files
            // from an older conversation turn must never satisfy the current
            // stage (or be clicked/downloaded in its place).
            const scopes = [
                ...document.querySelectorAll("[data-message-author-role='assistant'], "
                    + "[data-message-role='assistant'], .agent-turn, [data-content-search-unit-key$=':assistant'], "
                    + "section[data-testid^='conversation-turn-']:has(.agent-turn)")
            ];
            const scope = scopes.at(-1) || document;
            const respEls = scope.querySelectorAll('*');
            const byText = [...respEls].filter(el => {
                const t = (el.innerText || '').trim();
                return t.length >= 10 && t.length < 300
                    && el.querySelectorAll('p, li, ol, ul, h1, h2, h3').length <= 1
                    && fileCardRe.test(t);
            });
            const all = new Set([...byAttr, ...byText]);
            const result = [];
            for (const card of all) {
                // Prefer an <a> inside the card — its href is fetchable.
                const a = card.matches('a[href]') ? card : card.querySelector('a[href]');
                if (a) {
                    result.push([a.href || '', a.getAttribute('download'), (a.textContent || '').trim()]);
                } else {
                    // No anchor: report the card's text so _card_filename
                    // can extract the type label and _filename_in_text can
                    // flag it.  The href is empty so the fetch will be
                    // skipped — the click fallback will handle it.
                    result.push(['', null, (card.innerText || '').trim()]);
                }
            }
            return result;
        """
        card_links = driver.execute_script(_card_js) or []
        seen_hrefs = {c[0] for c in candidates if c[0]}
        for href, dl, text in card_links:
            if href and href not in seen_hrefs:
                candidates.append((href, dl, text))
                seen_hrefs.add(href)
            elif not href and text:
                # No anchor — the card's text still feeds _filename_in_text
                # and _card_filename so the click fallback knows a card-
                # shaped deliverable exists.
                candidates.append(("", None, text))
    except Exception:
        pass

    # The customer's own attachments come back as chips in THEIR turn of the
    # conversation, with the same download anchors a generated file has.
    # Harvesting the whole page (the fallback when the reply itself holds no
    # link) would otherwise re-save the drawing they uploaded as something
    # the tool made.
    ignore_lower = {os.path.basename(n).lower() for n in (ignore_names or ()) if n}

    out, seen = [], set()
    try:
        driver.set_script_timeout(20)
    except Exception:
        pass
    for href, download_attr, text in candidates:
        if not href or href in seen:
            continue
        clean = href.split("?")[0].split("#")[0]
        href_ext = os.path.splitext(clean)[1].lower()
        # The `download` attribute is the site's own suggested filename and,
        # when present, a more reliable source for the real extension than
        # the URL — a download endpoint's href is often extension-less. The
        # link's visible text is the next best thing (see _filename_in_text).
        text_name = _filename_in_text(text)
        site_name = _safe_filename(os.path.basename(download_attr or text_name or ""))
        ext = os.path.splitext(site_name)[1].lower() or href_ext
        if not (href.startswith("blob:") or download_attr is not None
                or href_ext in _HARVESTABLE_EXTS or text_name):
            continue   # an ordinary navigational link, not a deliverable
        if site_name and site_name.lower() in ignore_lower:
            continue   # the customer's own upload, shown back to them
        seen.add(href)

        raw = None
        try:
            data = driver.execute_async_script(
                """
                const src = arguments[0], done = arguments[arguments.length - 1];
                fetch(src, {credentials: 'include'})
                    .then(r => r.blob())
                    .then(b => { const fr = new FileReader();
                                 fr.onloadend = () => done(fr.result);
                                 fr.readAsDataURL(b); })
                    .catch(() => done(null));
                """, href)
            if data and data.startswith("data:"):
                header, b64 = data.split(",", 1)
                raw = base64.b64decode(b64)
                if not ext and raw:
                    # header looks like "data:application/pdf;base64" — the part
                    # after "data:" is the MIME type the browser attached.
                    mime = header[5:] if header.startswith("data:") else ""
                    ext = _guess_ext(mime, raw)
        except Exception:
            raw = None
        if not raw:
            continue
        # Never treat HTML text or web pages as a harvested deliverable
        if raw.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<?xml", b"<head", b"<body")) or "text/html" in (mime if 'mime' in locals() else ""):
            continue

        if stage in ("audio", "voice", "speech"):
            is_real_audio = (
                raw.startswith((b"ID3", b"RIFF", b"OggS", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
                or b"ftyp" in raw[:16]
                or any(m in (mime if 'mime' in locals() else "") for m in ("audio/", "mpeg", "mp3", "wav", "ogg"))
            )
            if not is_real_audio:
                continue

        # Keep the name the tool gave the file: "Quotation - JK Cement.xlsx"
        # is what the customer asked for and what they will look for in
        # Artifacts; "content_file1.xlsx" is what they used to get.
        if stage in ("audio", "voice", "speech") and not ext:
            ext = ".mp3"
        if site_name:
            stem, site_ext = os.path.splitext(site_name)
            name = stem + (site_ext or ext)
        else:
            name = f"{stage}_file{len(out) + 1}{ext}"
        path = os.path.join(tempfile.gettempdir(), f"prism_{stage}_{name}")
        try:
            with open(path, "wb") as f:
                f.write(raw)
            att = F.attach(path)
        except Exception:
            continue
        if stage in ("audio", "voice", "speech"):
            att["kind"] = "audio"
        if att["kind"] == "image":
            continue   # _harvest_images already owns real pictures
        # Marked so a later stage can tell a file a model produced from one
        # the client actually supplied — same convention _harvest_images uses.
        att["_generated"] = True
        att["_site_name"] = site_name
        out.append(att)
        if len(out) >= 4:
            break

    # Nothing fetchable — try the click-to-download path. Claude's file skill
    # and ChatGPT's canvas serve a generated file through a Download BUTTON
    # that runs JS to save it, with no <a href> whose bytes we can fetch; the
    # reply says "Download the … PDF" and the artifact folder stayed empty.
    # Only when the caller has a reason to believe a file exists: this path
    # clicks the first download-ish control it finds and then waits up to
    # 45 s, which is the wrong thing to do on a plain-text step.
    if not out and click_fallback:
        out = _harvest_via_download(driver, agent_cfg, stage)
    return out


# Stages whose deliverable is usually a file. Every OTHER stage is looked at
# too (see _harvest_stage_files), but these get the patient wait.
_FILE_STAGES = ("brains", "development", "presentation", "format", "content", "write", "audio")

# A reply that says a file is coming, even when no link is on the page yet.
_FILE_HINT_RE = re.compile(
    r"\b(?:download|attached|attachment|here(?:'s| is) (?:the|your) (?:file|"
    r"document|report|spreadsheet|deck|presentation|workbook|pdf))\b"
    r"|\.(?:" + "|".join(re.escape(ext.lstrip("."))
                              for ext in _HARVESTABLE_EXTS) + r")\b", re.I)


def _harvest_stage_files(driver, agent_cfg, stage: str, texts,
                         attachments=None) -> list[dict]:
    """Every file a stage produced, on ANY stage.

    Harvesting used to run only on the six _FILE_STAGES. A tool asked on a
    research or summary step for "an Excel of the results" produced it, and
    Prism walked past: the file was never saved, and when the reply was the
    file alone the step read as "Prism couldn't read the response". Now the
    file stages keep their patient wait, and every other stage gets a free
    look -- one probe of the page, no sleep -- and a real wait only when the
    page already shows a file link or the reply's own words say one is
    coming. A plain-text step costs nothing extra.

    (History: briefly cap=8/grace=4 here -- to catch an agent that appends
    the file card after its text with no filename anywhere in it -- which
    caught more, unhinted, late-appearing cards, at the cost of that same
    short wait on EVERY plain-text stage of every run. Reverted 22-09-2026:
    a real, felt trade-off, not a bug, but the latency lost.)"""
    ignore = set()
    for a in attachments or []:
        if isinstance(a, dict):
            ignore.add(os.path.basename(a.get("name") or a.get("path") or ""))
    ignore.discard("")
    if stage in _FILE_STAGES:
        ui.info("   ⏳  waiting for the file to finish rendering (up to 60s)…")
        _wait_for_files(driver, stage=stage)
        return _harvest_files(driver, agent_cfg, stage, ignore_names=ignore,
                              click_fallback=True)
    # Some agents append the file card after their text without mentioning a
    # filename. Give the page a short chance to expose it on every stage.
    n = _wait_for_files(driver, stage=stage, cap=0, grace=0)
    hinted = any(_FILE_HINT_RE.search(t or "") for t in (texts or []))
    if not n and not hinted:
        return []
    if not n:
        ui.info("   ⏳  the reply mentions a file — giving it a moment to appear…")
        n = _wait_for_files(driver, stage=stage, cap=18, grace=6)
    return _harvest_files(driver, agent_cfg, stage, ignore_names=ignore,
                          click_fallback=bool(n) or hinted)


def _files_as_reply(items: list[dict]) -> str:
    """What a step's result reads as when the tool answered with a file and
    no prose. Kept as the stage's text so the run records a result, the
    card shows one, and the next stage's context names the file it is also
    being handed."""
    from . import files as F
    lines = ["The tool answered with a file rather than text:"]
    for it in items:
        name = it.get("_site_name") or it.get("name") or "file"
        kind = it.get("kind") or "file"
        try:
            size = F._human_size(int(it.get("size") or 0))
        except Exception:                                   # noqa: BLE001
            size = ""
        lines.append(f"- {name} ({kind}{', ' + size if size else ''})")
    lines.append("The file is saved in Prism Artifacts and passed on to the "
                 "next step as an attachment.")
    return "\n".join(lines)


def _doc_stem(text: str, stage: str) -> str:
    """A filename for a document Prism had to write itself — taken from the
    answer's own first heading, so the file in Artifacts is called what the
    document calls itself rather than "content 3"."""
    for line in (text or "").splitlines():
        s = line.strip().lstrip("#").strip(" *_")
        if not s:
            continue
        if 3 <= len(s) <= 80 and not s.endswith((".", ":", "!", "?")):
            return _safe_filename(s)
        break
    return _safe_filename(stage or "document")


def _meet_contract(driver, agent_cfg: dict, stage: str, kind: str,
                   texts: list, made: list, *, query: str = "",
                   task: str = "", attachments=None, should_stop=None) -> dict:
    """Hold a finished step to what it owed, and try ONCE to put it right.

    This is the half of core/contract.py that touches the page. A step whose
    kind is `file` and which came back as chat text is not finished: the tool
    still has everything it just wrote, so one short follow-up in the same
    chat ("give me that as a downloadable .docx, built here") is both the
    cheapest and the likeliest fix — far better than failing the run or
    starting the step again somewhere else.

    When even that comes back as prose, the words are not thrown away: Prism
    writes the document itself from the step's own text, so the person still
    ends up with a file to open and is told plainly who made it.

    Returns {"files": [...harvest-shaped items...], "texts": [...], "note":
    str, "missing": str}. Never raises: a step that produced a real answer
    must not be failed by the machinery that was trying to improve it.
    """
    from . import contract as _c
    files = [m for m in made if (m.get("kind") or "file") != "image"]
    images = [m for m in made if (m.get("kind") or "") == "image"]
    out: dict = {"files": [], "texts": [], "note": "", "missing": ""}
    short = _c.missing(kind, texts=texts, files=len(files), images=len(images))
    if not short:
        return out

    ext = _c.wanted_ext(query)
    ask = _c.reask(kind, ext=ext, query=query, task=task)
    halted = should_stop or (lambda: False)
    if (ask and driver is not None and agent_cfg.get("textarea_selector")
            and not halted()):
        ui.warn(f"   {short} — asking once more for it")
        try:
            # The tool's own budget, not the follow-up minimum: this is a
            # one-line correction, and Stop/Skip are polled throughout.
            more = _reask(driver, agent_cfg, ask, should_stop=halted,
                          wait=int(agent_cfg.get("wait_time", 60) or 60))
        except Exception as e:                              # noqa: BLE001
            ui.warn(f"   couldn't ask again ({e})")
            more = []
        if halted():
            out["missing"] = short
            return out
        try:
            if kind == "image":
                if _wait_for_images(driver, agent_cfg, 1, cap=180, grace=20,
                                    should_stop=should_stop):
                    out["files"] = _harvest_images(
                        driver, agent_cfg, stage,
                        exclude_files=attachments)
            else:
                _wait_for_files(driver, stage=stage, cap=45, grace=12)
                ignore = tuple(
                    os.path.basename(a.get("name") or a.get("path") or "")
                    for a in (attachments or []) if isinstance(a, dict))
                out["files"] = _harvest_files(
                    driver, agent_cfg, stage,
                    ignore_names=tuple(n for n in ignore if n),
                    click_fallback=True)
        except Exception as e:                              # noqa: BLE001
            ui.warn(f"   couldn't collect it ({e})")
        if out["files"]:
            ui.ok(f"   ✅  got it on the second ask — "
                  f"{len(out['files'])} file(s)")
            return out
        out["texts"] = [t for t in more if (t or "").strip()]

    if kind != "file":
        out["missing"] = short
        return out

    # Last resort, and the reason a document request can no longer end with
    # nothing to open: write the file out of the words the tool did write.
    #
    # Prefer the original turn over the corrective turn.  The correction is
    # only meant to make the site attach a file; it is not a new writing
    # brief.  Some agents answer that correction with a longer stock message
    # such as "I don't see anything earlier in this conversation…".  Choosing
    # the longest reply across both turns then made Prism faithfully save that
    # error as the customer's document and discard the real answer it had
    # already captured.  The original turn is the source material for this
    # fallback; use a corrective turn only when the original was empty.
    best = max((t for t in texts if (t or "").strip()), key=len, default="")
    if not best:
        best = max((t for t in out["texts"] if (t or "").strip()),
                   key=len, default="")
    if not best:
        out["missing"] = short
        return out
    import tempfile
    try:
        folder = tempfile.mkdtemp(prefix="prism-doc-")
        stem = _doc_stem(best, stage)
        path, note = _c.document_from_text(best, folder, stem, ext)
    except Exception as e:                                  # noqa: BLE001
        ui.warn(f"   couldn't write the document ({e})")
        out["missing"] = short
        return out
    if not path:
        out["missing"] = short
        return out
    out["files"] = [{"path": path, "name": os.path.basename(path),
                     "kind": "document", "url": "",
                     "size": os.path.getsize(path),
                     "_site_name": os.path.basename(path)}]
    out["note"] = note
    ui.ok(f"   📝  {note} — {os.path.basename(path)}")
    return out


def _file_summaries(items: list[dict]) -> list[dict]:
    """The part of a harvested record the GUI can show: no text dump."""
    return [{"name": it.get("_site_name") or it.get("name") or "",
             "kind": it.get("kind") or "file",
             "size": int(it.get("size") or 0),
             "saved": it.get("_saved") or "",
             "path": it.get("path") or ""} for it in items or []]


def _click_download_control(driver, agent_cfg: dict) -> bool:
    """Click whatever serves a generated file: an explicit download anchor, a
    download-labelled icon button, or visible 'Download' text — searched inside
    the reply first, then the whole page. Returns whether something was
    clicked. Never raises."""
    from selenium.webdriver.common.by import By
    # New ChatGPT file cards use an icon-only `Download file` button and no
    # fetchable anchor.  A file card's filename button OPENS a preview; it is
    # not a download control.  Only the explicitly-labelled button is safe.
    # Keep this check scoped to the latest assistant turn so an older result
    # cannot be downloaded for the current stage.
    try:
        clicked = bool(driver.execute_script(r"""
            const scopes = [...document.querySelectorAll(
                "[data-message-author-role='assistant'], "
                + "[data-message-role='assistant'], .agent-turn, [data-content-search-unit-key$=':assistant'], "
                + "section[data-testid^='conversation-turn-']:has(.agent-turn)")];
            const scope = scopes.at(-1);
            if (!scope) return false;
            const button = [...scope.querySelectorAll(
                "button[aria-label*='download' i], a[download], "
                + "a[aria-label*='download' i]")]
                .find(el => el.getClientRects().length && !el.disabled);
            if (!button) return false;
            button.scrollIntoView({block: 'center'});
            button.click();
            return true;
        """))
        if clicked:
            return True
    except Exception:
        pass
    # ChatGPT must never fall through to generic card/text clicks: its file
    # card is a preview opener, while its neighbouring icon button is the
    # actual download.  Waiting for that explicit button is safer than
    # opening a preview and incorrectly asking the model for the same file.
    if "chatgpt.com" in str(agent_cfg.get("url") or agent_cfg.get("start_url") or ""):
        return False
    # _within, not f"{scope}…". A response_selector is a selector LIST, and a
    # descendant combinator binds tighter than the comma — so `f"{scope}
    # a[download]"` meant "every branch except the last, PLUS a[download]
    # inside the last one". Verified in a real Chrome against ChatGPT's
    # registered selector with a genuine <a download> in the reply: it
    # matched the message DIV, this function clicked that div, returned True,
    # and _harvest_via_download then waited 45s for a download that was never
    # going to start. The feature could not work on either of the two
    # most-used tools in the registry.
    sel = agent_cfg.get("response_selector", "")
    css_list = [_within(sel, "a[download]"),
                _within(sel, "[aria-label*='download' i]"),
                _within(sel, "[data-testid*='download' i]"),
                "a[download]", "[aria-label*='download' i]"]
    for css in css_list:
        if not css:
            continue
        try:
            els = driver.find_elements(By.CSS_SELECTOR, css.strip())
        except Exception:
            continue
        for el in els:
            try:
                # A container is not a control. Without this, one slip in a
                # selector is worth 45 seconds of waiting on a clicked <div>
                # — which is exactly what the bug above cost.
                role = (el.get_attribute("role") or "").lower()
                if el.tag_name.lower() not in ("a", "button") and role != "button":
                    continue
                if el.is_displayed() and el.is_enabled():
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el)
                    el.click()
                    return True
            except Exception:
                continue
    # Last resort before generic text search: look for an explicitly-labelled
    # download control inside a file card.  Never click the card itself: on
    # current ChatGPT it opens a preview rather than downloading the file.
    try:
        card_js = """
            const typeRe = /(^|[·•|\\s])(docx?|pdf|pptx?|xlsx?|csv|md|txt|zip|json|py|ipynb)\\s*$/im;
            const sels = [
                "[data-testid*='artifact' i]", "[class*='artifact' i]",
                "[data-testid*='file' i]", "[data-testid*='download' i]",
                "[class*='file-card' i]", "[class*='fileCard' i]",
            ];
            const byAttr = [...document.querySelectorAll(sels.join(', '))]
                .filter(el => typeRe.test((el.innerText || '').trim()));
            const respEls = document.querySelectorAll(
                "[data-message-author-role='assistant'] *, "
                + "[data-message-role='assistant'] *, "
                + "[data-content-search-unit-key$=':assistant'] *");
            const byText = [...respEls].filter(el => {
                const t = (el.innerText || '').trim();
                return t.length >= 10 && t.length < 300
                    && el.querySelectorAll('p, li, ol, ul, h1, h2, h3').length <= 1
                    && typeRe.test(t);
            });
            // Return the outermost unique cards.
            const all = [...new Set([...byAttr, ...byText])];
            return all.length;
        """
        n = int(driver.execute_script(card_js) or 0)
    except Exception:
        n = 0
    if n:
        # Re-query (the count was JS-only) and click a labelled download
        # control inside the card, never the card itself.
        try:
            card_sels = (
                "[data-testid*='artifact' i], [class*='artifact' i], "
                "[data-testid*='file' i], [data-testid*='download' i], "
                "[class*='file-card' i], [class*='fileCard' i]")
            els = driver.find_elements(By.CSS_SELECTOR, card_sels)
            for card in els:
                try:
                    txt = (card.text or "").strip()
                    if len(txt) < 10 or len(txt) > 300:
                        continue
                    if not _CARD_TYPE_RE.search(txt):
                        continue
                    if not (card.is_displayed()):
                        continue
                    # Prefer a download button or link INSIDE the card.
                    inner = None
                    for inner_sel in ("a[download]",
                                      "[aria-label*='download' i]",
                                      "[data-testid*='download' i]"):
                        try:
                            inner = card.find_element(By.CSS_SELECTOR,
                                                       inner_sel)
                            if inner.is_displayed():
                                break
                            inner = None
                        except Exception:
                            inner = None
                    if inner is None:
                        continue
                    target = inner
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});",
                        target)
                    target.click()
                    return True
                except Exception:
                    continue
        except Exception:
            pass
    try:
        dl_btn = driver.execute_script("""
            const btns = [...document.querySelectorAll("button, a, [role='button']")];
            return btns.find(b => {
                const label = ((b.getAttribute('aria-label') || '') + ' ' +
                               (b.innerText || '') + ' ' +
                               (b.getAttribute('data-testid') || '') + ' ' +
                               (b.getAttribute('title') || '')).toLowerCase();
                return label.includes('download') && b.offsetParent !== null;
            });
        """)
        if dl_btn:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", dl_btn)
            dl_btn.click()
            return True
    except Exception:
        pass
    # Last resort: a visible link/button whose text is literally "Download …".
    return _click_by_text(driver, ["download"], timeout=4)


def _current_chatgpt_file_card(driver) -> bool:
    """Whether the newest ChatGPT reply already contains a generated file.

    This is deliberately a *guard*, not a click target.  The filename button
    opens ChatGPT's preview pane; only its sibling ``Download file`` button
    may be clicked by `_click_download_control`.
    """
    try:
        return bool(driver.execute_script(r"""
            const scopes = [...document.querySelectorAll(
                "[data-message-author-role='assistant'], "
                + "[data-message-role='assistant'], .agent-turn, [data-content-search-unit-key$=':assistant'], "
                + "section[data-testid^='conversation-turn-']:has(.agent-turn)")];
            const scope = scopes.at(-1);
            if (!scope) return false;
            if (scope.querySelector("button[aria-label*='download' i], a[aria-label*='download' i]")) return true;
            const fileCardRe = /\.(?:docx?|pdf|pptx?|xlsx?|csv|md|txt|zip|json|py|ipynb)(?:\s|\n)*(?:document|spreadsheet|presentation|file)?\s*$/im;
            return [...scope.querySelectorAll('button, [role="button"]')]
                .some(el => fileCardRe.test((el.innerText || '').trim()));
        """))
    except Exception:
        return False


def _capture_download(driver, stage: str, click, *, wait: float = 45,
                      menu_words=("pdf", "download", "mp3", "audio", "voice")) -> list[dict]:
    """Capture a file that is only reachable by CLICKING something — the
    sibling of _harvest_files' fetch path, for tools that hand the file over
    through JS rather than a fetchable link. Point Chrome's download at a
    scratch folder we own (so our file is distinguishable from the user's own
    ~/Downloads traffic), run `click`, and harvest whatever lands within
    `wait` seconds. When nothing lands and nothing is in flight after a few
    seconds, one menu item named in `menu_words` is tried — the first click
    may have opened a "Download as…" menu.

    Best-effort and never raises: if the download can't be redirected, or
    nothing lands, or a plain link navigates instead of downloading, it gives
    back an empty list and restores the tab, leaving the run unharmed.
    """
    from . import files as F

    folder = os.path.join(tempfile.gettempdir(),
                          f"prism_dl_{stage}_{os.getpid()}")
    try:
        os.makedirs(folder, exist_ok=True)
        driver.execute_cdp_cmd("Page.setDownloadBehavior",
                               {"behavior": "allow", "downloadPath": folder})
    except Exception:
        return []

    origin = ""
    try:
        origin = driver.current_url
    except Exception:
        pass
    before = set(os.listdir(folder))
    # ChatGPT creates the card before it mounts its icon-only `Download
    # file` button.  Do not mistake that short gap for a missing deliverable
    # and send the model a duplicate corrective prompt.  Wait briefly for an
    # explicit control; `click` never opens a file card.
    clicked = False
    click_deadline = time.time() + min(20, wait)
    while time.time() < click_deadline:
        if click():
            clicked = True
            break
        time.sleep(1)
    if not clicked:
        _restore_downloads(driver, folder)
        return []

    # Wait for a COMPLETE file — Chrome writes *.crdownload while in flight, and
    # a big PDF from a code tool can take a while to serialise and stream.
    start, tried_menu, target = time.time(), False, None
    while time.time() - start < wait:
        time.sleep(1)
        now = set(os.listdir(folder)) - before
        fresh = [f for f in now
                 if not f.endswith((".crdownload", ".tmp", ".part"))]
        inflight = any(f.endswith((".crdownload", ".tmp", ".part")) for f in now)
        if fresh:
            # Newest, not alphabetically first: two files landing meant the
            # wrong one was picked, and "a.pdf" beating "report.pdf" is not a
            # rule anybody intended.
            cand = max((os.path.join(folder, f) for f in fresh),
                       key=lambda f: os.path.getmtime(f)
                       if os.path.exists(f) else 0)
            s1 = os.path.getsize(cand) if os.path.exists(cand) else -1
            time.sleep(1)
            s2 = os.path.getsize(cand) if os.path.exists(cand) else -2
            if s1 == s2 and s1 > 0:          # size settled → done writing
                target = cand
                break
        elif not inflight and not tried_menu and time.time() - start > 6:
            # Nothing landed and nothing is downloading — the first click may
            # have opened a "Download as…" menu instead. Try a menu item once.
            tried_menu = True
            _click_by_text(driver, list(menu_words), timeout=2)

    # A plain link (no Content-Disposition) navigates instead of downloading —
    # put the chat tab back so the saved stage URL and any later step are intact.
    try:
        if origin and driver.current_url != origin:
            driver.get(origin)
    except Exception:
        pass

    if not target:
        _restore_downloads(driver, folder)
        return []
    try:
        att = F.attach(target)
    except Exception:
        _restore_downloads(driver, folder)
        return []
    # The file is copied out by _save_artifacts / re-uploaded from its temp
    # path, so the scratch folder itself is not kept.
    _restore_downloads(driver, folder, keep=target)
    if att.get("kind") == "image":
        return []          # _harvest_images already owns real pictures
    if stage in ("audio", "voice", "speech"):
        att["kind"] = "audio"
    att["_generated"] = True
    return [att]


def _harvest_via_download(driver, agent_cfg, stage: str) -> list[dict]:
    """The generic click-to-download harvest: whatever download control the
    reply or the page shows (_click_download_control), captured."""
    return _capture_download(driver, stage,
                             lambda: _click_download_control(driver, agent_cfg))


def _restore_downloads(driver, folder: str, keep: str = "") -> None:
    """Give Chrome its download folder back, and stop littering temp.

    Page.setDownloadBehavior is a SESSION setting and was never reset, so
    after one harvest every download in that browser went to a temp folder
    for the rest of the run — including the user's own, since Prism keeps
    its browser open on purpose and people use it. Silent, and it survives
    long after the stage that caused it.
    """
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior",
                               {"behavior": "default"})
    except Exception:
        pass
    if keep:
        return              # the harvested file still lives in there
    try:
        shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        pass


def _save_artifacts(items: list[dict], query: str, stage: str,
                    link: str = "") -> None:
    """Copy what a stage just generated out of the temp directory `items`
    were harvested into, and into the one folder a customer can still find
    once Prism is closed — see config.ARTIFACTS_DIR.

    `link` is the live tab this stage's tool answered in — passed straight
    through to config.save_artifact() so the saved file keeps a way back to
    the actual AI conversation that produced it, not just its output.

    Best-effort and silent per item: a full disk or a permissions slip here
    must not cost the run its actual result, which the harvested temp file
    (and, for a producer stage with a next step, `pipeline_files`) already
    holds regardless of whether this copy succeeds.
    """
    from . import config
    for item in items:
        path = item.get("path")
        if not path:
            continue
        try:
            # `query` names the whole run, not just this stage — passing it
            # as `task` too means every stage's output (images, docs, video)
            # from one New Task lands in the same Artifacts subfolder.
            saved = config.save_artifact(path, query, kind=stage, link=link,
                                         task=query,
                                         name=item.get("_site_name") or "")
        except Exception as e:                          # noqa: BLE001
            # Silent for a long time, and that silence is what made "Studio
            # makes artwork on Linux but not on my Mac" undiagnosable: the
            # picture was made and harvested, the copy to the Desktop was
            # refused (macOS folder permission), and nothing said so.
            ui.warn(f"   couldn't save {os.path.basename(path)} to Prism "
                    f"Artifacts: {e} — the file is still at {path}")
            continue
        item["_saved"] = saved
        ui.info(f"   💾  saved to {saved}")


# Phrases that only ever appear in what Prism types, never in a tool's answer.
# Every stage prompt ends with the pipeline rules, and /email's draft prompt
# carries the SUBJECT/BODY template — an element containing either is the user
# turn, not the reply.
_PROMPT_ECHO_MARKERS = (
    "strict pipeline rules:",
    "your only task is:",
    "reply with nothing except",
    "reply with only a json object describing the reel",
    "<one subject line>",
    "<the full email body>",
    "your output will be passed directly to",
)

#: Lines of Prism's own composed message that a tool might reasonably QUOTE
#: in a real answer — a hand-off that repeats "what the person actually asked
#: for" is doing its job. One of these alone is not enough to call a capture
#: Prism's echo and throw it away; the whole message carries several. (The
#: first version counted any one of them, and would have discarded exactly
#: that hand-off.)
_PROMPT_ECHO_SOFT = (
    "from the earlier step (",
    "at the end add a section headed 'handoff for",
    "give me the finished result",
)


def _is_prompt_echo(text: str) -> bool:
    low = (text or "").lower()
    if any(m in low for m in _PROMPT_ECHO_MARKERS):
        return True
    # Every first message opens "Prism · <job> · <step>" (_chat_header), and
    # no tool's answer does.
    if "prism · " in low[:200]:
        return True
    return sum(m in low for m in _PROMPT_ECHO_SOFT) >= 2


def _safe_url(driver, exclude=()) -> str:
    """The current tab's URL, for the paths where the stage blew up. Never
    raises (the session itself may be what died) and never returns a link that
    isn't this stage's: a blank tab, or a page already credited to an earlier
    stage, means we failed before we ever got to the tool."""
    try:
        url = (driver.current_url or "").strip()
    except Exception:
        return ""
    if not url or url.startswith(("about:", "data:", "chrome:")):
        return ""
    return "" if url in exclude else url


def _sleep_interruptibly(seconds: float, should_stop=None) -> bool:
    """time.sleep, but it notices a cancel. Returns True if we were stopped.

    The waits in here are long by design (a tool can take five minutes), so a
    plain sleep makes a Stop button feel broken — the user presses it and
    nothing happens until the current poll interval ends. Sleeping in short
    slices keeps cancellation under a second without polling the page harder.
    """
    if should_stop is None:
        time.sleep(seconds)
        return False
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_stop():
            return True
        time.sleep(min(0.25, max(0.0, deadline - time.time())))
    return should_stop()


def _smart_wait(driver, agent_cfg, cap: int, poll: int = 1,
                stable_for: float = 2.5, min_wait: int = 3,
                expect: str = "", should_stop=None) -> tuple[int, bool]:
    """Wait for the agent to finish generating — but no longer than needed.
    Polls the response selector and returns once the total response text has
    stopped growing for `stable_for` seconds (after having grown at least
    once). `cap` is the hard maximum (the old fixed sleep), so a selector
    that never matches degrades to the previous behaviour, not a hang.

    Returns (seconds_waited, settled). settled is False when the cap ran out
    with the answer still growing — the tool has NOT failed, we just stopped
    watching, and it will keep working in its tab. Callers use this to say so
    and to hand the user the link instead of claiming the scrape missed.

    `expect` is a marker the finished answer must contain (e.g. "SUBJECT:" for
    an email draft). Tools routinely pause mid-answer — thinking, rendering a
    tool call, streaming in bursts — and a pause longer than `stable_for` reads
    exactly like being finished. When the marker is set, a lull that doesn't
    contain it is treated as the tool still working."""
    from selenium.webdriver.common.by import By
    sel = agent_cfg.get("response_selector", "")
    start = time.time()
    baseline = last_len = None
    last_change = start
    grown = False
    settled = False
    # An answer that is EMPTY and FINISHED. ChatGPT sometimes puts up a new
    # assistant turn with nothing in it -- the toolbar, no text, no stop
    # button -- and this loop, watching for text to grow, waited the whole
    # cap for text that was never coming (300s on the 2026-09-10 run). A
    # tool that says which element means "still generating" (busy_selector)
    # and which means "a reply" (turn_selector) lets the wait end as soon
    # as a new turn has sat idle and empty for a while; the caller then
    # regenerates once (_regenerate_once) or fails the stage NOW, so the
    # fallback tool gets it minutes earlier.
    busy_sel = agent_cfg.get("busy_selector", "")
    turn_sel = agent_cfg.get("turn_selector", "")
    turns0 = None
    empty_since = None

    def _count(css: str) -> int:
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, css))
        except Exception:
            return 0

    def has_marker() -> bool:
        if not expect:
            return True
        try:
            return any(expect.lower() in (el.text or "").lower()
                       for el in driver.find_elements(By.CSS_SELECTOR, sel))
        except Exception:
            return False

    def still_generating() -> bool:
        # The tool's own busy selector AND the generic "Stop" button, not one
        # or the other: ChatGPT's composer changes its stop button's markup
        # without notice, and a selector that silently matches nothing read as
        # "finished" while it was still working through its tool calls (the
        # 2026-10-05 run moved on to Claude after 5s).
        if busy_sel:
            try:
                if any(el.is_displayed() for el in
                       driver.find_elements(By.CSS_SELECTOR, busy_sel)):
                    return True
            except Exception:
                pass
        try:
            return bool(driver.execute_script("""
                return [...document.querySelectorAll('button')].some(button => {
                    if (!button.getClientRects().length) return false;
                    const labels = [button.innerText, button.getAttribute('aria-label'),
                                    button.getAttribute('title'),
                                    button.getAttribute('data-testid')];
                    return labels.some(label =>
                        /^(stop|stop generating|stop response|stop streaming|cancel response|stop-button)$/i
                            .test((label || '').trim()));
                });
            """))
        except Exception:
            return False

    while time.time() - start < cap:
        # Stopping here returns settled=False, which the caller already treats
        # as "we stopped watching, the tool didn't fail" — exactly the truth
        # after a cancel. It re-checks should_stop before scraping.
        if _sleep_interruptibly(poll, should_stop):
            break
        try:
            total = sum(len(el.text) for el in
                        driver.find_elements(By.CSS_SELECTOR, sel))
        except Exception:
            continue
        if baseline is None:
            # First reading — whatever is already on the page (our own typed
            # prompt, old chat turns) doesn't count as generation.
            baseline = last_len = total
            if turn_sel:
                turns0 = _count(turn_sel)
            continue
        img_present = False
        try:
            img_present = bool(driver.execute_script("""
                const dalle = document.querySelector('img[src*="blob.core.windows.net"], img[src*="files.oaiusercontent.com"], img[src*="backend-api/files"], [data-testid*="dalle"] img, [data-testid*="image-pane"] img, img[alt*="Generated image" i]');
                return Boolean(dalle && ((dalle.naturalWidth || 0) >= 256 || (dalle.width || 0) >= 256));
            """))
        except Exception:
            pass
        if total != last_len:
            grown = grown or total > baseline
            last_len = total
            last_change = time.time()
        elif ((grown or (img_present and time.time() - start >= 15)
               # A reply already complete before the first reading never
               # "grows"; a tool idle for 8s with text on the page and no
               # stop button is finished all the same -- but only when that
               # text can be proved to be THIS reply: the one and only
               # assistant turn on the page (a fresh chat). With earlier
               # turns on screen, old text and a reply that has not started
               # look identical, and settling would hand back the previous
               # answer as the new one (a follow-up, a Studio scene turn).
               or (total > 0 and time.time() - last_change >= 8
                   and time.time() - start >= 20
                   and bool(turn_sel) and _count(turn_sel) == 1))
              and time.time() - start >= min_wait
              and time.time() - last_change >= stable_for
              and has_marker() and not still_generating()):
            settled = True
            break
        if (turn_sel and busy_sel and not grown
                and time.time() - start >= EMPTY_TURN_AFTER):
            # A reply turn exists, nothing is generating, and no text has
            # arrived: an empty answer. Give it a moment to be a slow start
            # rather than a blank, then stop waiting.
            if _count(turn_sel) > (turns0 or 0) and not _count(busy_sel):
                empty_since = empty_since or time.time()
                if time.time() - empty_since >= EMPTY_TURN_PATIENCE:
                    ui.warn("   the tool finished with an empty answer — "
                            "not waiting out the cap")
                    settled = True
                    break
            else:
                empty_since = None
    return int(time.time() - start), settled


# How long an empty, finished reply turn is given before it is called empty:
# seconds into the wait before the check starts, and how long the turn has to
# stay idle and blank. Short on purpose -- a genuine answer starts streaming
# text within seconds of the turn appearing.
EMPTY_TURN_AFTER = 20
EMPTY_TURN_PATIENCE = 15


def _regenerate_once(driver, agent_cfg: dict) -> bool:
    """Press the tool's own "regenerate" on an empty answer, once.

    An empty ChatGPT turn is usually a hiccup, not a refusal: the same
    prompt regenerated a moment later answers normally. One press, then the
    ordinary wait; if it is empty again the stage fails and the fallback
    tool takes it. Best-effort: the control is hover-only in the DOM and is
    clicked through the page rather than the mouse. Returns whether
    anything was clicked."""
    sel = agent_cfg.get("regenerate_selector", "")
    turn_sel = agent_cfg.get("turn_selector", "")
    if not sel:
        return False
    js = """
        const sel = arguments[0], turnSel = arguments[1];
        let scope = document;
        if (turnSel) {
            const turns = document.querySelectorAll(turnSel);
            if (turns.length) scope = turns[turns.length - 1];
        }
        let btn = scope.querySelector(sel) || document.querySelector(sel);
        if (!btn) return false;
        btn.click();
        return true;
    """
    try:
        clicked = bool(driver.execute_script(js, sel, turn_sel))
    except Exception:
        clicked = False
    if clicked:
        ui.info("   🔁  empty answer — asked the tool to regenerate once")
    return clicked


def _wait_for_images(driver, agent_cfg, want: int, cap: int = 240,
                     grace: int | None = None, should_stop=None,
                     baseline_count: int | None = None) -> int:
    """Wait for generated images to actually appear, then stop growing.

    _smart_wait watches TEXT, and during image generation the text is finished
    long before the pictures are — the model says "here are three images" and
    then renders for another minute. Waiting on text alone scrapes the page
    while every canvas is still empty, which looks exactly like a stage that
    produced nothing.

    `grace`, when set, gives up early if nothing has appeared at all within
    that many seconds — for a stage where a picture is possible but not
    guaranteed, most turns never produce one, and waiting out the full `cap`
    for an image that was never coming would stall every plain-text turn on
    that stage.
    """
    sel = agent_cfg.get("response_selector", "")
    # The WHOLE page, not just the reply element. ChatGPT's image UI opens a
    # canvas pane beside the conversation, so the pictures live outside
    # [data-message-author-role='assistant'] — the harvester was looking in
    # the one place they are not, and reported that none had appeared while
    # three sat on screen.
    # Three readings, not one count. ChatGPT renders an image PROGRESSIVELY:
    # a blurred preview <img> appears within seconds and is then replaced,
    # in place, by the finished picture a minute or two later. Counting
    # images alone saw "1, still 1, still 1" and called it done at 20s —
    # which is how a /step-auto sheet came back as a smear. So the signature
    # (sizes and sources) has to sit still too, and while the page itself
    # says it is still creating the image, nothing is done regardless.
    js = r"""
        function isGeneratedAssistantImage(img) {
            if (!img) return false;
            const src = img.currentSrc || img.src || '';
            if (!src) return false;
            // Exclude user-turn prompts, attachment chips, composers
            if (img.closest(
                '[data-message-author-role="user"], [data-message-role="user"], ' +
                '.user-message, form, [data-testid*="composer"], [data-testid*="attachment"], ' +
                '[data-testid*="user-message"], [data-testid*="user-turn"], [class*="user-turn"]'
            )) {
                return false;
            }

            // Immediate pass: OpenAI DALL-E CDN URLs are ALWAYS generated assistant images
            const isOpenAiDalle = /oaidalleapiprodscus\.blob\.core\.windows\.net|files\.oaiusercontent\.com|chatgpt\.com\/backend-api\/files/i.test(src);
            if (isOpenAiDalle) return true;

            const turn = img.closest('article, section[data-testid^="conversation-turn-"], [data-testid^="conversation-turn-"], [class*="conversation-turn"]');
            if (turn && turn.querySelector('[data-message-author-role="user"], [data-message-role="user"]')) {
                return false;
            }
            // Exclude web search citations, source cards, search result thumbnails
            if (img.closest(
                'a[href^="http://"], a[href^="https://"]:not([href*="chatgpt.com"]):not([href*="openai.com"]), ' +
                '[data-testid*="source"], [data-testid*="citation"], [class*="citation"], ' +
                '[class*="source-card"], [class*="attribution"], [class*="search-result"], ' +
                '[aria-label*="source" i], [aria-label*="citation" i], details, [class*="searched"]'
            )) {
                return false;
            }
            // Exclude UI avatars, navbars, and toolbar icon buttons
            if (img.closest(
                '[class*="avatar"], [data-testid*="avatar"], nav, header, footer, ' +
                'button[aria-label*="copy" i], button[aria-label*="thumb" i], button[aria-label*="feedback" i]'
            )) {
                return false;
            }
            const alt = (img.getAttribute('alt') || '').toLowerCase();
            if (alt.includes('avatar') || alt === 'user' || alt === 'chatgpt') return false;
            return true;
        }

        let n = 0, px = 0, sig = '';
        const seenSrc = new Set();
        for (const img of document.querySelectorAll('img')) {
          if (!isGeneratedAssistantImage(img)) continue;
          const src = img.currentSrc || img.src || '';
          if (!src || seenSrc.has(src)) continue;
          const rect = img.getBoundingClientRect();
          if (rect.width > 0 && rect.width < 120 && rect.height > 0 && rect.height < 120) {
            continue;
          }
          if ((img.naturalWidth || 0) >= 256 && (img.naturalHeight || 0) >= 256) {
            seenSrc.add(src);
            n++;
            px += img.naturalWidth * img.naturalHeight;
            sig += src.slice(-48) + ';';
          }
        }
        const stopBtn = Boolean(document.querySelector('button[aria-label*="Stop" i], button[data-testid*="stop" i]'));
        const spinning = Boolean(document.querySelector('[class*="animate-spin"], [class*="animate-pulse"], [data-testid*="generating"]'));
        const textBusy = /creating image|generating image|rendering image|image is being (?:created|generated)|creating your image/i
                       .test(document.body.innerText || '');
        const busy = stopBtn || spinning || textBusy;
        return [n, px + ':' + sig, busy];
    """
    # Capture the pre-generation page after Send but before the first poll:
    # uploaded client images are already on that page and must not be counted
    # as generated artwork. Subsequent image-tool turns pass their known
    # count, so each one waits for a genuinely new asset.
    if baseline_count is None:
        try:
            baseline_n, _baseline_sig, _baseline_busy = driver.execute_script(js, sel)
            baseline_n = int(baseline_n or 0)
        except Exception:
            baseline_n = 0
    else:
        baseline_n = max(0, int(baseline_count))
    start, last, last_sig, steady = time.time(), 0, "", 0
    while time.time() - start < cap:
        # Four seconds a poll, and stop/skip checked every poll: this loop
        # never polled should_stop at all, and a stage stuck "rendering" an
        # image that was never coming held the run for the whole cap with no
        # way out. That is the loop "Skip this step" exists to break.
        if _sleep_interruptibly(4, should_stop):
            break
        try:
            n, sig, busy = driver.execute_script(js, sel)
            n = int(n or 0)
        except Exception:
            continue
        new_n = max(0, n - baseline_n)
        # A generator can replace a preview in place. Its count stays the
        # same, but its source signature changes; that is still evidence the
        # new image is progressing, never a reason to idle until the cap.
        if new_n > last or (last and sig != last_sig):
            last, last_sig, steady = max(last, new_n), sig, 0
            ui.info(f"   🖼️   {last} new image(s) so far…")
        elif last:
            steady += 4
            # Images that have been sitting there unchanged and not busy are finished!
            if (steady >= 12 and not busy) or (last >= want and not busy and steady >= 4):
                break
        elif (n > 0 or new_n > 0) and not busy and (time.time() - start >= 8):
            # If the image was already generated on the page (or before this loop), accept it
            last = max(last, new_n if new_n > 0 else n)
            ui.info(f"   🖼️   {last} completed image(s) ready")
            break
        elif grace is not None and not busy and time.time() - start >= grace:
            break   # nothing ever appeared — not this turn's kind of reply
    return last


def _chatgpt_is_busy(driver) -> bool:
    """True if ChatGPT is actively generating an image or answer."""
    try:
        return bool(driver.execute_script("""
            const stopBtn = document.querySelector('button[aria-label*="Stop"], button[data-testid*="stop"]');
            const spinning = document.querySelector('[class*="animate-spin"], [class*="animate-pulse"]');
            const busy = /creating image|generating image|rendering image|creating your image/i.test(document.body.innerText || '');
            return Boolean(stopBtn || spinning || busy);
        """))
    except Exception:
        return False


def _wait_for_files(driver, cap: int = 60, grace: int = 12,
                    stage: str = "") -> int:
    """_wait_for_images's twin for document/deck/code deliverables.

    A Code-Interpreter-style "Download" link often finishes rendering AFTER
    the reply text settles — the model says "here's your file" a few seconds
    before the button that serves it exists. Most development/presentation/
    format turns never produce a separate download link at all (the code or
    outline written into the chat IS the whole reply), so `grace` gives up
    fast when nothing shows, rather than taxing every plain-text turn on
    these stages the way an unconditional wait would.

    `stage="audio"` also counts <audio> elements — see _harvest_files' own
    audio-specific note on why a synth/music player is unlikely to expose a
    plain download anchor the way a document tool does.
    """
    import json as _json
    exts_sel = ", ".join(f"a[href$='{ext}']" for ext in _HARVESTABLE_EXTS)
    audio_sel = ", audio" if stage == "audio" else ""
    # Two kinds of anchor: one whose href or attributes say "file", and one
    # whose visible TEXT is a filename — ChatGPT's generated-file chip, an
    # extension-less signed URL labelled "report.docx" (_filename_in_text).
    js = (
        "const exts = " + _json.dumps(list(_HARVESTABLE_EXTS)) + ";"
        "const byHref = document.querySelectorAll(\"a[href^='blob:'], a[download], "
        + exts_sel + audio_sel + "\");"
        "const byText = [...document.querySelectorAll('a[href]')].filter(a => {"
        "  const t = (a.textContent || '').trim().toLowerCase();"
        "  return t.length < 160 && exts.some(e => t.endsWith(e)); });"
        # A third shape, and the one that cost a customer their document: a
        # CARD with no anchor and no extension anywhere, labelled by type —
        # "Document·DOCX" over a Download button. Matched on the artifact
        # containers the page itself names, so this stays cheap and does not
        # sweep every div on a long conversation.
        #
        # Relaxed since 1.5.4: the `/download/i` requirement dropped a
        # ChatGPT file card whose text is "Delta_Report... PDF" with no
        # "Download" word anywhere.  A card whose innerText names a known
        # file type is counted whether or not it also says "download".
        #
        # A fourth shape: ChatGPT's newer file card.  It is not marked with
        # "artifact" in any attribute — it sits inside the assistant turn as
        # a standalone block.  Detected by a broader sweep of the reply area
        # for any element whose text is SHORT and ends with a bare file-type
        # word (PDF, DOCX …) on a line of its own.  The length cap (< 300
        # chars) and the requirement for a type-word ON ITS OWN LINE keep it
        # from matching a long prose paragraph that merely mentions "PDF".
        + r"const typeRe = /(^|[·•|\s])(docx?|pdf|pptx?|xlsx?|csv|md|txt|zip"
        r"|json|py|ipynb|mp3|wav|m4a|ogg|flac|aac)\s*$/im;"
        + r"const fileCardRe = /\.(?:docx?|pdf|pptx?|xlsx?|csv|md|txt|zip|json|py|ipynb|mp3|wav|m4a|ogg|flac|aac)(?:\s|\n)*(?:document|spreadsheet|presentation|file|audio|voice)?\s*$/im;"
        "const cards = [...document.querySelectorAll(\"[data-testid*='artifact' i], "
        "[class*='artifact' i]\")].filter(el => "
        "(typeRe.test(((el.innerText || '').trim())) || fileCardRe.test(((el.innerText || '').trim()))));"
        # ChatGPT file cards: short blocks whose text ends with a type label.
        "const chatCards = [...document.querySelectorAll("
        "\"[data-testid*='file' i], [data-testid*='download' i], "
        "[class*='file-card' i], [class*='fileCard' i]\")].filter(el => {"
        "  const t = (el.innerText || '').trim();"
        "  return t.length < 300 && (typeRe.test(t) || fileCardRe.test(t)); });"
        # Broadest fallback: any element inside the response area whose text
        # is SHORT, contains a known extension as a standalone word, and is
        # NOT a full paragraph.  This catches cards whose DOM attributes
        # carry no predictable marker at all — only their visible text
        # distinguishes them from prose.
        "const assistantScopes = [...document.querySelectorAll("
        "\"[data-message-author-role='assistant'], "
        "[data-message-role='assistant'], .agent-turn, [data-content-search-unit-key$=':assistant'], "
        "section[data-testid^='conversation-turn-']:has(.agent-turn)\")];"
        "const assistantScope = assistantScopes.at(-1) || document;"
        "const respEls = assistantScope.querySelectorAll('*');"
        "const inlineCards = [...respEls].filter(el => {"
        "  const t = (el.innerText || '').trim();"
        "  if (t.length < 10 || t.length > 300) return false;"
        "  if (el.querySelectorAll('p, li, ol, ul, h1, h2, h3').length > 1) return false;"
        "  return typeRe.test(t) || fileCardRe.test(t); });"
        # ChatGPT's Sep 2026 file card is filename + "Document", with an
        # icon-only button labelled "Download file". It has neither a URL nor
        # an extension in its type line, so the visible control is the honest
        # signal that a deliverable exists. Scoped to the current assistant
        # turn so old downloadable files cannot trigger a correction.
        "const downloadControls = assistantScope.querySelectorAll("
        "\"button[aria-label*='download' i], a[aria-label*='download' i]\");"
        "return new Set([...byHref, ...byText, ...cards, ...chatCards, ...inlineCards, ...downloadControls]).size;")

    def probe() -> int:
        try:
            return int(driver.execute_script(js) or 0)
        except Exception:
            return -1

    # Look first, sleep after: a caller with cap=0 gets one honest answer
    # and pays nothing for it (see _harvest_stage_files).
    start, steady = time.time(), 0
    last = max(probe(), 0)
    if last:
        ui.info(f"   📎  {last} file link(s) so far…")
    while time.time() - start < cap:
        time.sleep(1)
        n = probe()
        if n < 0:
            continue
        if n > last:
            last, steady = n, 0
            ui.info(f"   📎  {n} file link(s) so far…")
        elif last:
            steady += 1
            # A link that's been sitting there for 4s is all there is.
            if steady >= 4:
                break
        elif time.time() - start >= grace:
            break   # nothing ever appeared — not this turn's kind of reply
    return last


#: Tags that are actually a control rather than something wrapped around one.
_CONTROL_TAGS = ("button", "a")


def _innermost_control(elements) -> object | None:
    """Of the elements whose text matched, the one that IS the control.

    This is the whole fix for a document that a tool built and Prism then
    dropped. `contains(normalize-space(.), 'download')` matches every
    ANCESTOR of the real button as well — the card, the message, the whole
    conversation column — and document order puts the outermost of those
    first. So Prism clicked a <div>, Selenium reported a successful click,
    _harvest_via_download waited its full 45 seconds for a download that was
    never going to start, and the DOCX Claude had just built was lost with
    no error anywhere (run of 2026-09-08 15:07).

    Two keys, in order: a real control beats a container, and among equals
    the SHORTEST text wins — the button says "Download", the card around it
    says the document's whole name and type as well.
    """
    best, best_key = None, None
    for el in elements:
        try:
            if not (el.is_displayed() and el.is_enabled()):
                continue
            tag = (el.tag_name or "").lower()
            role = (el.get_attribute("role") or "").lower()
            text = (el.text or "").strip()
        except Exception:                                   # noqa: BLE001
            continue        # a node that went stale mid-scan is not a control
        key = (0 if (tag in _CONTROL_TAGS or role == "button") else 1, len(text))
        if best_key is None or key < best_key:
            best, best_key = el, key
    return best


def _click_by_text(driver, texts: list[str], timeout: int = 10) -> bool:
    """Best-effort: click the visible element whose text matches one of
    `texts` (case-insensitive, substring). NotebookLM's UI doesn't expose
    stable ids/classes the way ChatGPT/Claude do, so matching on visible
    button/label TEXT is the more durable anchor here.

    Polls rather than using element_to_be_clickable: that helper returns the
    FIRST match in document order, which for a text match is the outermost
    container — see _innermost_control. Returns False (never raises) if
    nothing matched within `timeout`.
    """
    from selenium.webdriver.common.by import By
    parts = []
    for t in texts:
        tl = t.lower()
        parts.append(
            "//*[self::button or self::a or self::span or self::div or self::li]"
            "[contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
            f"'{tl}')]"
        )
    xpath = " | ".join(parts)
    deadline = time.time() + max(timeout, 0)
    while True:
        try:
            found = driver.find_elements(By.XPATH, xpath)
        except Exception:                                   # noqa: BLE001
            found = []
        el = _innermost_control(found)
        if el is not None:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", el)
            except Exception:                               # noqa: BLE001
                pass
            try:
                el.click()
                return True
            except Exception:                               # noqa: BLE001
                pass        # covered/animating — try again on the next poll
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


# ── how firmly to talk to a tool ──────────────────────────────────────────────
# Most agents are chat models being used as pipeline components, and they need
# telling: do only this, hand over in this shape, do not ask me questions back.
# That block of STRICT PIPELINE RULES is what keeps a ten-stage run coherent.
#
# It is also actively harmful to a tool that does its own multi-pass work.
# LAZYCOOK runs Generate → Analyze → Optimize → Validate and scrapes the web
# itself; "perform ONLY the task above — nothing more" reads to it as an
# instruction to skip those passes, and it answers in one shot. The tool then
# underperforms the Perplexity it was picked over, which looks like Prism
# choosing badly rather than Prism asking badly.
#
# So an agent can declare prompt_style="natural" and get asked the way a person
# would ask. The pipeline still needs a handoff, so it is still requested — as
# a request, at the end, in a sentence rather than as rule 3 of 4.
def _is_natural(agent_cfg: dict) -> bool:
    return (agent_cfg or {}).get("prompt_style") == "natural"


def _is_maker(agent_cfg: dict) -> bool:
    """A tool whose deliverable is a thing it builds, not text -- see
    agents._MAKES."""
    return bool((agent_cfg or {}).get("makes"))


# Stages whose deliverable is a built thing, whichever tool runs them. The
# router's own description of presentation is "building an actual slide deck /
# PowerPoint". Given a deck to make on 2026-09-11, Claude -- not a registered
# maker -- built the .pptx in its sandbox for 522 seconds and left 431
# characters of build log in the chat. A skill's text layout typed there
# argues with the file, and a checker reading the build log faults it.
_BUILDING_STAGES = ("presentation",)


def _builds_deliverable(agent_cfg: dict, stage: str) -> bool:
    """True when this stage's deliverable is something the tool builds:
    a registered maker, or a building stage on any tool."""
    return (_is_maker(agent_cfg)
            or (stage or "").strip().lower().split(" ")[0] in _BUILDING_STAGES)


def _maker_brief(agent_name: str, agent_cfg: dict) -> str:
    """The opening of a maker's stage prompt: what it is for, in plain words.

    Written as a person would brief a designer, and put FIRST so it frames
    everything after it. The one thing it must do is set aside any
    text-shaped instruction further down: the stage prompt is written by
    the router for a chat tool, and "deliver the deck in plain-text format,
    do NOT generate files" reached Canva verbatim on 2026-09-10 -- so Canva
    typed the outline back instead of building the deck. Saying so up
    front, in the tool's own terms, is what stops that.
    """
    makes = (agent_cfg or {}).get("makes", "")
    if not makes:
        return ""
    if (agent_name == "ElevenLabs" or (agent_cfg or {}).get("runner") == "elevenlabs"
            or "voice" in makes.lower() or "audio" in makes.lower()):
        return (
            f"You are {agent_name}. Create the voice-over audio for this deliverable. "
            "Speak ONLY the dialogue and spoken narration lines below — do NOT speak "
            "any instructions, scene labels, visual descriptions, or JSON syntax.\n\n"
        )
    return (
        f"You are {agent_name}, and what I need from you is {makes}. Build "
        f"it here, in this tool — the thing itself is the deliverable of "
        f"this step, and it is what gets collected from this page. Use the "
        f"content below exactly as written: the headings, the order, the "
        f"wording. If anything below asks for plain text, a text-format "
        f"version, a description or an outline instead of the thing, or "
        f"says not to create files — that was written for a chat tool and "
        f"does not apply to you: build it. When it is built, say so in one "
        f"line.\n\n"
    )


def _maker_handoff() -> str:
    """The closing rule for a maker: nothing but the thing, and one line."""
    return ("\n\nWhat you build is what is collected. Do not add a handoff "
            "section, a summary, an explanation or a follow-up question — "
            "build it, then one line saying it is done.")


# How much of the user's own request travels into every stage prompt. Generous
# on purpose: this is the one piece of text nothing else can reconstruct, and
# truncating the sentence that says what the product DOES is exactly the
# failure this exists to prevent.
_MAX_INTENT_CHARS = 2500


# Selenium's wordings for "there is no browser to talk to any more". Every one
# of these means the session is dead, not that this particular step went wrong.
_BROWSER_GONE = (
    "no such window",
    "target window already closed",
    "web view not found",
    "invalid session id",
    "session deleted because of page crash",
    "disconnected: not connected to devtools",
    "chrome not reachable",
    "browser has closed",
    "read timed out",
    "read timeout",
    "connection refused",
    "connection reset",
    "max retries exceeded",
)


def motion_script(all_responses: dict) -> str:
    """The copy stage's script from a run's responses so far, or "". The
    content stage answers under its category label ("content"); a run may
    also carry an explicit script stage. Newest text wins."""
    if not isinstance(all_responses, dict):
        return ""
    for key in ("script", "content", "copy"):
        texts = all_responses.get(key)
        if isinstance(texts, list):
            joined = "\n".join(str(t) for t in texts if str(t).strip())
            if joined.strip():
                return joined
    return ""


def motion_turn_one(query: str, skeleton: str, all_responses: dict) -> str:
    """The motion writer's rebuilt first prompt — the storyboard turn with
    the script threaded in — or "" when no script was written, in which
    case the planned prompt stands."""
    script = motion_script(all_responses)
    if not script:
        return ""
    from .motion import generate as _motion_gen
    return _motion_gen.storyboard_instructions(
        query, skeleton=skeleton or "cinematic_glass", script=script)


def _keep_failed_spec(sources) -> str:
    """Write the reply that would not parse to disk, and say where.

    A renderer failure is the one place where the evidence is both essential
    and momentary: the text lives in a local variable, the run moves on, and
    all anybody is left with is "No JSON found in the agent's reply" against a
    ChatGPT tab that visibly contains JSON. Those two facts cannot both be
    investigated from a log line.

    Best-effort in every direction — a full disk must not turn a bad design
    into a crash.
    """
    try:
        from . import config
        folder = os.path.join(os.path.dirname(config.CONFIG_PATH), "logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"design-that-would-not-parse-{int(time.time())}.txt")
        with open(path, "w", encoding="utf-8") as f:
            for i, text in enumerate(sources, 1):
                f.write(f"───── candidate {i} ─────\n{text}\n\n")
        ui.warn(f"   saved what came back to {path}")
        return path
    except Exception:                                    # noqa: BLE001
        return ""


def _blank_native_webdriver_error(error: object) -> bool:
    """Whether ChromeDriver returned its address-only, message-less failure.

    The affected Linux build returns "Message:" followed only by native stack
    addresses. It can wrap that error in a RuntimeError, so inspect the
    exception chain rather than trusting the outer message.
    """
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).replace("\r", "")
        before, marker, _stack = message.partition("Stacktrace:")
        if marker and before.strip().lower() in ("", "message:"):
            return True
        current = (getattr(current, "__cause__", None) or
                   getattr(current, "__context__", None))
    return False


def _browser_is_gone(error: object) -> bool:
    """True when the failure is the browser itself, not the step.

    Matched on the message rather than the exception class because
    undetected_chromedriver re-raises through several of Selenium's types and
    the wording is the only thing common to all of them. A native blank
    ChromeDriver error is also a dead/unusable session in practice.
    """
    return (_blank_native_webdriver_error(error) or
            any(marker in str(error).lower() for marker in _BROWSER_GONE))


# What a step is called to a person — the plan screen's words, so the chat a
# step opens is titled the way the timeline names it.
# The plain name of each step, as the chat header and the one-line prompts
# say it. Lives in agents.py so the router can use it without importing
# this module.
STEP_NAMES = A.STEP_NAMES


def _chat_header(title: str, stage: str) -> str:
    """The first line of the first message a stage sends: `Prism · <job> ·
    <step>`. ChatGPT and Claude name a conversation after its opening, and
    every Prism chat used to open with "Your ONLY task is: Act as a senior
    …" — so a customer's sidebar read "Senior Creative Director Task" forty
    times, and nothing said which job or which step any of them was."""
    step = STEP_NAMES.get(stage) or STEP_NAMES.get(stage.split(" ")[0]) \
        or stage.replace("_", " ").capitalize()
    title = (title or "").strip()
    return f"Prism · {title} · {step}\n\n" if title else f"Prism · {step}\n\n"


def _intent_block(query: str) -> str:
    """The user's own words, verbatim, at the top of every stage prompt --
    and since Round 31 (owner, 11 Sep 2026) nothing else around them.

    The failure the block itself fixes, in full, because it cost a whole
    reel: Prism expanded a request into a professional brief and the router
    wrote each stage prompt FROM that brief. A customer asked for a reel
    about "Consiz, a mouse with a middle button that summarises whatever you
    have selected"; the brief said "showcase the mouse, demonstrate its
    features", and Claude wrote an excellent script about a generic mouse.
    Nothing downstream can recover from that, so the raw request rides
    along, first.

    What was taken away: the banner "WHAT THE PERSON ACTUALLY ASKED FOR",
    the dashes, and a paragraph explaining that summaries lose things and
    the words above win. The owner put the same request to Claude by hand as
    one line with the PDF attached and got the BOQ in a fraction of the
    tokens; the Prism message for the same file exhausted the chat. The
    tools do not need to be told whose words these are or to rank them --
    they need the words. So: the words.
    """
    text = (query or "").strip()
    if not text:
        return ""
    if len(text) > _MAX_INTENT_CHARS:
        text = text[:_MAX_INTENT_CHARS].rstrip() + " […]"
    return text + "\n\n"


_HANDOFF_HEADING = re.compile(
    r"^\W*HANDOFF FOR [A-Z0-9][A-Z0-9 .&'()\-]*\W*$", re.IGNORECASE | re.MULTILINE)


def _handoff_of(texts, *, lines: int | None = _MAX_HANDOFF_LINES) -> str:
    """What travels from one step to the next: the lines under the previous
    answer's 'HANDOFF FOR <tool>' heading, at most `lines` of them.

    The whole previous answer used to go (its last 8,000 characters), on the
    theory that a summary loses things. It does -- and a research report
    pasted whole into a writing step lost the run instead: the owner's BOQ
    request of 10 Sep 2026 sent Claude a message that was 90% Perplexity's
    report and the chat ran out of tokens before the document existed. The
    person still gets every step's full answer in the run's output; the
    NEXT TOOL gets the hand-off it was asked to write, which is the part the
    earlier tool judged worth carrying.

    Last heading wins (a tool that restates its block puts the considered
    one at the end). `lines=None` keeps every line under the heading -- for
    a tool with its own filter block (Apollo), whose fields must all
    arrive. An answer with no heading at all falls back to its tail, capped
    hard, so a tool that ignored the request still hands something on.
    """
    text = "\n\n".join(t for t in (texts or []) if t and t.strip()).strip()
    if not text:
        return ""
    hits = list(_HANDOFF_HEADING.finditer(text))
    if hits:
        body = text[hits[-1].end():].strip()
        kept = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
        if lines is not None:
            kept = kept[:lines]
        body = "\n".join(kept)
        return body[:_MAX_FORWARD_CHARS].rstrip()
    if len(text) > _MAX_FORWARD_CHARS:
        text = "[…] " + text[-_MAX_FORWARD_CHARS:].lstrip()
    return text


def _context_header(agent_cfg: dict, prev_stage: str) -> str:
    """The line that introduces the previous stage's output."""
    if _is_natural(agent_cfg) or _is_maker(agent_cfg):
        return ("Here's what I've got so far on this — use whatever is useful "
                "and ignore the rest:\n\n")
    step = STEP_NAMES.get(prev_stage) or prev_stage.replace("_", " ")
    return f"From the earlier step ({step}):\n"


def _context_footer(agent_cfg: dict) -> str:
    if _is_natural(agent_cfg) or _is_maker(agent_cfg):
        return "\n\nWith that in mind:\n\n"
    return "\n\n"


def _natural_handoff(nxt_agent: str, final: bool) -> str:
    """A request, not a rule sheet -- and since Round 31, a short one.

    Deliberately says nothing about performing only the task, nothing about
    the reader being a machine, and nothing about not asking questions.
    Those three are what suppress a self-directing tool. What it keeps is
    the one thing the pipeline cannot work without: a hand-off at the end
    that the next tool reads on its own -- and it is now told how long that
    is, because only those lines travel (_handoff_of).
    """
    if final:
        return ("\n\nGo as deep as it needs and give me the finished piece, "
                "not an outline — and don't end by asking what I'd like next.")
    return (
        "\n\nGo as deep as it needs. At the end add a short section headed "
        f"'HANDOFF FOR {nxt_agent.upper()}': at most {_MAX_HANDOFF_LINES} "
        f"lines with the facts, figures and decisions {nxt_agent} needs — "
        "I'm passing only those lines on.")


def _resolve_suffix(agent_cfg: dict, stage: str, query: str) -> str:
    """The literal text appended to this agent's prompt for this stage.

    A registry entry is either a plain string — always appended — or a switch:

        {"when": ("canva", "editable", …),
         "asked":     "…build it as an editable Canva design…",
         "otherwise": "…generate the image yourself, do not use Canva…"}

    which is resolved against THE USER'S OWN WORDS, not against the router's
    rewrite of them. That distinction is the whole point. ChatGPT with the
    Canva app connected will route every image through it given the chance,
    and the result is a stock template where an illustration was wanted — so
    the editable-design path has to be something the customer asked for out
    loud, and a router paraphrasing a brief must not be able to opt them in.

    Both branches are spelled out because silence is not neutral: leaving the
    prompt quiet is what let ChatGPT reach for Canva unprompted.
    """
    entry = (agent_cfg.get("stage_suffix") or {}).get(stage, "")
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return ""
    triggers = entry.get("when") or ()
    lowered = (query or "").lower()
    asked = any(str(t).lower() in lowered for t in triggers)
    return entry.get("asked" if asked else "otherwise", "") or ""


# ── Apollo: a filter screen, not a chat box ───────────────────────────────────
#
# Every other tool in the registry takes one blob of text. Apollo takes a set
# of FIELDS, and its API refuses any single field over 200 characters — which
# is how a normal pipeline brief ended a run with:
#     Value too long: 'Context from the previous pipeline stage (RESEA…'
#
# Two halves to the fix. agents._APOLLO_HANDOFF makes the previous stage write
# a fixed block of filter lines instead of prose; everything below parses that
# block and puts the values into Apollo's own search URL.
#
# Why the URL and not the filter widgets: Apollo mirrors its entire search
# state into the address bar, so navigating to a built URL sets the same
# filters as clicking through the left rail — without touching a single one
# of the class names that rail is built from. The results grid is the only
# part of the DOM this still depends on.
#
# The parameter names are Apollo's own, read off the address bar of a search
# built by hand in the UI. If Apollo renames one, that filter silently stops
# applying (the search still runs, just wider) — so a run that comes back
# with obviously unfiltered results means it is time to build the search in
# Apollo once and compare its URL against _APOLLO_PARAMS.
_APOLLO_PARAMS = {
    "TITLES":     "personTitles[]",
    "LOCATIONS":  "personLocations[]",
    "INDUSTRIES": "qOrganizationKeywordTags[]",
}

# Apollo expresses headcount as a "low,high" pair. The handoff spec asks for
# these exact labels so the mapping can be a lookup rather than a parser.
_APOLLO_HEADCOUNT = {
    "1-10": "1,10", "11-20": "11,20", "21-50": "21,50", "51-100": "51,100",
    "101-200": "101,200", "201-500": "201,500", "501-1000": "501,1000",
    "1001-2000": "1001,2000", "2001-5000": "2001,5000",
    "5001-10000": "5001,10000", "10001+": "10001,1000000",
}

_APOLLO_FIELDS = ("TITLES", "INDUSTRIES", "LOCATIONS", "HEADCOUNT", "KEYWORDS")


def _apollo_filters(text: str, cap: int = 180) -> dict[str, list[str]]:
    """Pull the 'HANDOFF FOR APOLLO' block out of the previous stage's answer.

    Tolerant on purpose: the block is looked for anywhere in the text, the
    field names are matched case-insensitively, and markdown bullets or bold
    the model added anyway are stripped off. A field it could not narrow comes
    back as "any", which is treated as absent rather than searched for
    literally — searching Apollo for the job title "any" returns nothing.

    Every value is truncated to `cap`, because the whole point of this path is
    that Apollo rejects long ones. Truncation is the last line of defence: the
    prompt already asked for short values, but a prompt is a request and this
    is a guarantee.
    """
    import re

    out: dict[str, list[str]] = {}
    for field in _APOLLO_FIELDS:
        # Last occurrence wins — models often restate the block, and the final
        # one is the considered answer rather than an example being echoed.
        hits = re.findall(rf"^\W*{field}\s*:\s*(.+)$", text,
                          re.IGNORECASE | re.MULTILINE)
        if not hits:
            continue
        raw = hits[-1].strip().strip("*_`").strip()
        values = []
        for part in raw.split(","):
            v = part.strip().strip("*_`[]").strip()
            # "any"/"n/a"/"none" all mean "the model declined to narrow this".
            if not v or v.lower() in ("any", "n/a", "na", "none", "all", "-"):
                continue
            values.append(v[:cap])
        if values:
            out[field] = values[:8]   # a 40-title search is not a search
    return out


def _apollo_url(base: str, filters: dict[str, list[str]]) -> str:
    """Turn parsed filters into an Apollo people-search URL.

    Apollo is a hash-routed SPA, so the query string lives after '#/people'.
    The '[]' in its repeated parameters is left literal rather than
    percent-encoded — that is how Apollo writes its own links.
    """
    from urllib.parse import quote

    parts = ["page=1",
             # The reason to use Apollo at all rather than ask a model to
             # guess addresses. Without it the table fills with rows whose
             # email column is a locked placeholder.
             "contactEmailStatusV2[]=verified"]

    for field, param in _APOLLO_PARAMS.items():
        for value in filters.get(field, []):
            parts.append(f"{param}={quote(value)}")
    if filters.get("INDUSTRIES"):
        # Tells Apollo which company fields the industry keywords above should
        # be matched against; without it the keyword filter is ignored.
        parts.append("includedOrganizationKeywordFields[]=tags")
        parts.append("includedOrganizationKeywordFields[]=name")
    for band in filters.get("HEADCOUNT", []):
        pair = _APOLLO_HEADCOUNT.get(band.replace(" ", ""))
        if pair:
            parts.append(f"organizationNumEmployeesRanges[]={quote(pair)}")
    if filters.get("KEYWORDS"):
        parts.append("qKeywords=" + quote(" ".join(filters["KEYWORDS"])))

    root = base.split("?")[0] or "https://app.apollo.io/#/people"
    return f"{root}?" + "&".join(parts)


def _apollo_fallback_query(brief: str, limit: int = 60) -> str:
    """A handful of plain words for Apollo's keyword box. Apollo's search is
    not a chat prompt — a sentence typed into it matches nobody, while a few
    words return a wide but real table — so the fallback takes the first
    substantial line and keeps at most six words of it."""
    import re as _re

    line = next((ln.strip() for ln in brief.splitlines()
                 if len(ln.strip()) > 20
                 and not ln.strip().startswith("#")), brief.strip())
    terms = _re.findall(r"[A-Za-z][A-Za-z&-]{2,}", line)
    return " ".join(terms[:6])[:limit] or line[:limit]


def _apollo_ai_text(brief: str, cap: int = 4000) -> str:
    """What goes into Apollo's AI box: the brief's words, on one line, trimmed
    to what the box was measured to keep.

    One line because the box is an <input>, not a textarea: a newline typed
    into it is either dropped (the JS path) or pressed as Enter (send_keys),
    and Enter submits — the second half of a two-line brief would arrive as
    a separate, chat-spending prompt. Prose is otherwise left alone; this is
    the one surface in Apollo that reads sentences. The cut lands on a word
    boundary so the assistant never reads half a word as the request."""
    text = " ".join((brief or "").split())
    if len(text) <= cap:
        return text
    head = text[:cap]
    sp = head.rfind(" ")
    return (head[:sp] if sp > cap // 2 else head).rstrip()


# The People page shows its match count as a "Total" tile. It is the earliest
# reliable sign that the assistant has applied filters: the number drops from
# the whole database (hundreds of millions) to the matched few, seconds before
# the grid finishes drawing. Read as text, so a redesign of the tile only
# costs the early exit, not the search.
_APOLLO_TOTAL_JS = """
const m = document.body.innerText.match(/Total\\s*\\n?\\s*([0-9][0-9.,]*[KM]?)/);
return m ? m[1] : '';
"""

# Anything under this is a filtered search; the unfiltered tile reads in the
# hundreds of millions, and a tile still loading reads 0.
_APOLLO_FILTERED_BELOW = 5_000_000


def _apollo_total_count(text: str):
    """'246.4M' -> 246400000, '1.2K' -> 1200, '8' -> 8, '' -> None."""
    t = (text or "").strip().replace(",", "")
    if not t:
        return None
    mult = {"K": 1_000, "M": 1_000_000}.get(t[-1].upper(), 1)
    if mult != 1:
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None
_APOLLO_CHATS_JS = """
const m = document.body.innerText.match(/(\\d+)\\s+CHATS?\\s+LEFT/i);
return m ? m[1] : '';
"""


def _apollo_ai_box(driver, agent_cfg: dict):
    """The "Use Apollo AI to find the right prospects" field, or None.

    Apollo only draws it while NO search is on screen — and a Prism session
    restores its last tab, so the People page usually comes back with the
    previous run's keywords still applied and the box gone. Two things bring
    it back, both checked on the live app: the "Reset filters" button in the
    empty-results area, and the bare People route. Tried in that order.

    Not the "Search with AI" button. Despite the name it does not reveal the
    box: it opens an assistant chat ABOUT the current search ("My search
    returns 0 results…") and spends one of the account's chats doing so.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    sel = agent_cfg.get("ai_prompt_selector", "")
    if not sel:
        return None

    def _find():
        return [e for e in driver.find_elements(By.CSS_SELECTOR, sel)
                if e.is_displayed()]

    boxes = _find()
    if boxes:
        return boxes[0]

    reset = agent_cfg.get("ai_prompt_reset", "")
    if reset:
        try:
            btn = driver.find_element(
                By.XPATH, f"//button[contains(normalize-space(.), '{reset}')]")
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
                WebDriverWait(driver, 10).until(lambda d: _find())
                return _find()[0]
        except Exception:
            pass

    # No reset button (nothing was filtered, or the page is mid-load). A
    # hash change to the bare route clears the search state without a
    # reload; a full get of the same URL is the heavier second try.
    root = (agent_cfg.get("url") or "https://app.apollo.io/#/people").split("?")[0]
    frag = "#" + root.split("#", 1)[1] if "#" in root else "#/people"
    for step in ("hash", "get"):
        try:
            if step == "hash":
                driver.execute_script("location.hash = arguments[0]", frag)
            else:
                driver.get(root)
            WebDriverWait(driver, 12).until(lambda d: _find())
            return _find()[0]
        except Exception:
            continue
    return None


def _apollo_ai_prompt(driver, agent_cfg: dict, prompt: str) -> bool:
    """Put a prose prompt where Apollo actually reads prose, and wait for the
    filters it produces to land on the table.

    The box is the "Use Apollo AI to find the right prospects" field in the
    empty state of the People page. Enter hands the text to Apollo's AI
    Assistant, which opens as a side panel, thinks for half a minute or more,
    then applies Titles / keywords / Location to the grid — the same grid
    _capture() already reads. Returns False if the box could not be found or
    the text did not go in, so the caller can drop to the keyword search.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    box = _apollo_ai_box(driver, agent_cfg)
    if box is None:
        ui.warn("   Apollo's AI prompt box isn't on this page")
        return False

    try:
        chats = driver.execute_script(_APOLLO_CHATS_JS)
    except Exception:
        chats = ""
    if chats == "0":
        ui.warn("   this Apollo account has no AI chats left this month — "
                "the prompt box would be ignored")
        return False

    try:
        box.click()
        if not _fast_type(driver, box, prompt):
            box.clear()
            box.send_keys(prompt)
        box.send_keys(Keys.ENTER)
    except Exception as e:
        ui.warn(f"   couldn't hand the prompt to Apollo's AI box ({e})")
        return False
    ui.info(f"   🤖  prompt handed to Apollo AI ({len(prompt)} chars"
            + (f", {chats} chats left" if chats else "") + ") — it takes a "
            "minute to turn that into filters")

    # Done when the Total tile reads a filtered count or rows appear. The
    # box only exists on the empty People page, which shows no grid at all,
    # so either is a real signal; a tile that still says 0 is one that has
    # not loaded yet, not a filter that matched nobody.
    deadline = time.time() + int(agent_cfg.get("ai_prompt_wait", 150))
    rows_sel = agent_cfg.get("response_selector", "")
    while time.time() < deadline:
        time.sleep(3)
        try:
            n = _apollo_total_count(driver.execute_script(_APOLLO_TOTAL_JS))
            if n is not None and 0 < n < _APOLLO_FILTERED_BELOW:
                break
            if rows_sel and any(
                    e.is_displayed() and len(e.text) > 50
                    for e in driver.find_elements(By.CSS_SELECTOR, rows_sel)):
                break
        except Exception:
            continue
    else:
        ui.warn("   Apollo AI hadn't applied any filters when the wait ran "
                "out — reading whatever the table shows")
    return True


def _run_apollo(driver, agent_cfg: dict, stage: str, brief: str) -> list[str]:
    """Drive Apollo by URL from the filter block the previous stage wrote.

    With no parseable block, the brief goes into Apollo's AI prompt box —
    the one field on the page that reads prose — and the filters the
    assistant builds from it are what get captured. Only if that box cannot
    be used does the run drop to a few plain words in the keyword search,
    which still beats failing the stage and stays inside the 200-character
    limit that caused the original error either way.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    cap = int(agent_cfg.get("max_query_chars", 180))
    filters = _apollo_filters(brief, cap)
    prompted = False

    if filters:
        shown = "; ".join(f"{k.lower()}: {', '.join(v)}"
                          for k, v in filters.items())
        ui.info(f"   🎯  Apollo filters — {shown}")
        url = _apollo_url(agent_cfg.get("url", ""), filters)
        try:
            driver.get(url)
        except Exception as e:
            ui.warn(f"   couldn't open the filtered search ({e})")
    else:
        ui.info("   the previous stage sent no APOLLO filter block — handing "
                "the brief to Apollo's AI search instead")
        prompt = _apollo_ai_text(brief, int(agent_cfg.get("ai_prompt_max_chars",
                                                           4000)))
        prompted = _apollo_ai_prompt(driver, agent_cfg, prompt)
        if not prompted:
            # The AI box is gone or spent. Apollo's keyword box is not a chat
            # prompt: a sentence typed into it matches nobody, while a
            # handful of plain words returns a wide but real table — so this
            # last resort is a few words, never a line of prose.
            ui.warn("   falling back to a short keyword search")
            query = _bmp_safe(_apollo_fallback_query(brief))
            try:
                box = WebDriverWait(driver, agent_cfg.get("input_wait", 30)).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, agent_cfg["textarea_selector"])))
                box.clear()
                if not _fast_type(driver, box, query):
                    box.send_keys(query)
                box.send_keys(Keys.ENTER)
            except Exception as e:
                ui.err(f"   couldn't run the Apollo search: {e}")
                return []

    # The grid loads asynchronously well after the URL settles. The AI path
    # has already waited for the filters to land.
    if not prompted:
        try:
            WebDriverWait(driver, agent_cfg.get("input_wait", 45)).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, agent_cfg["response_selector"])))
        except Exception:
            pass
    time.sleep(4)

    rows = _capture(driver, agent_cfg)
    if not rows and filters and not prompted:
        # The block parsed but matched nobody. One assistant chat is a fair
        # price for a second opinion from something that reads the brief.
        ui.warn("   the filtered search matched nobody — trying the brief in "
                "Apollo's AI search")
        prompt = _apollo_ai_text(brief, int(agent_cfg.get("ai_prompt_max_chars",
                                                           4000)))
        if _apollo_ai_prompt(driver, agent_cfg, prompt):
            time.sleep(4)
            rows = _capture(driver, agent_cfg)
    if not rows:
        ui.warn("   Apollo returned no rows. Three things do this: the "
                "filters matched nobody, this Apollo account is signed out, "
                "or it is out of email credits for the month. Open Login "
                "tabs, check you can see the People table, and check your "
                "credit balance at the top of Apollo.")
    return rows


def _click_control(driver, labels, timeout: int = 10) -> str:
    """Click the CONTROL whose own text starts with one of `labels` -- a
    button, a role=button, or a link -- choosing the smallest match.

    _click_by_text matches any span or div that CONTAINS the words, which
    on a page like Canva AI's is the whole chat pane: the first live run
    "pressed Generate design" on a container div and nothing happened.

    A real WebDriver click first (it scrolls into view and fires the
    pointer events a React card listens for -- a JS .click() opened
    nothing on Canva's "View outline" card, the second live run), and the
    page's own click only if that is intercepted. Returns the label that
    was clicked, or "".
    """
    js = """
        const labels = arguments[0].map(l => l.toLowerCase());
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
        const els = Array.from(document.querySelectorAll("button, [role='button'], a"));
        let best = null, bestLabel = "";
        for (const el of els) {
            if (!vis(el)) continue;
            const t = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
            for (const l of labels) {
                // First line of the control's text, so "View outline" with a
                // chevron or a second line still matches -- and no literal
                // newline inside this JS string (that is what silently broke
                // the whole matcher on the fourth live run).
                const first = t.split(String.fromCharCode(10))[0].trim();
                const hit = first === l || t.startsWith(l + " ");
                if (hit && (!best || t.length < (best.innerText || '').trim().length)) {
                    best = el; bestLabel = l;
                }
            }
        }
        return best ? [best, bestLabel] : null;
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hit = driver.execute_script(js, list(labels))
        except Exception:
            hit = None
        if hit:
            el, label = hit
            try:
                el.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click()", el)
                except Exception:
                    return ""
            return label
        time.sleep(1)
    return ""


def _run_canva(driver, agent_cfg: dict, stage: str, prompt: str,
               should_stop=None) -> list[str]:
    """Canva AI is a chat that answers a deck request in two moves.

    Read off the live page with Playwright on 2026-09-10
    (devtools/canva_probe.py's sibling probes), because the generic path
    failed twice on the owner's run -- "the prompt would not go into
    Canva's message box" on the old /magic-design/ marketing page, and,
    once the box was found, no generate button was ever pressed:

      1. https://www.canva.com/ai opens with a promo dialog over the page
         (student discount, free trial) whose video swallows every click.
         Escape closes it. The composer is the one textarea with an
         aria-label ("Describe your idea, and I'll bring it to life"); the
         send control is button[aria-label='Submit'].
      2. The first reply is an OUTLINE, not a design -- "I'll draft a
         concise outline, then you can turn it into the full presentation"
         -- with a "View outline" card and, once that is open, a
         "Generate design" button at the foot of the outline pane. Pressing
         it is what builds the deck; the reply then carries a preview card
         and "Your N-slide presentation ... has been created".

    The thread URL is the link Prism keeps: the deck opens from its card
    there, and the design also lands in the customer's Canva Projects.
    Best-effort throughout, like the NotebookLM runner: every step fails
    soft with a message rather than hanging the run, and stop/skip/
    fallback are polled between the long waits.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    def halted() -> bool:
        return bool(should_stop and should_stop())

    try:
        if "canva.com/ai" not in (driver.current_url or ""):
            driver.get(agent_cfg.get("url") or "https://www.canva.com/ai")
        time.sleep(agent_cfg.get("page_wait", 10))
        # The promo dialog. Escape is what closed it on the live page; a
        # close button is tried as well in case the dialog changes.
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        _click_control(driver, ["not now", "maybe later"], timeout=2)
        time.sleep(1)

        box = WebDriverWait(driver, agent_cfg.get("input_wait", 30)).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, agent_cfg["textarea_selector"])))
        box.click()
        if not _fast_type(driver, box, _bmp_safe(prompt)):
            box.send_keys(_bmp_safe(prompt))
        time.sleep(1)
        if not _text_landed(_composer_text(driver, box), prompt):
            return ["Canva AI: the prompt would not go into the message box "
                    "— nothing was sent. Its tab is open if you want to "
                    "paste it by hand."]
        sent = False
        try:
            WebDriverWait(driver, 8).until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, agent_cfg.get("submit_selector", "")))).click()
            sent = True
        except Exception:
            pass
        if not sent:
            box.send_keys(Keys.ENTER)
        ui.info("   ✓  prompt sent to Canva AI — waiting for the outline")

        # Move one: the outline. Canva thinks for a while before the card
        # appears; poll for either button rather than sleeping a fixed time.
        # Canva AI sometimes asks a question first -- a brand/style picker
        # ("Choose an existing layout or select Style only", with Skip),
        # seen on the third live run once it had a brand in memory. Skip is
        # answered for it; the outline follows.
        found = ""
        deadline = time.time() + min(int(agent_cfg.get("wait_time", 400)), 300)
        while time.time() < deadline and not halted():
            found = _click_control(
                driver, ["generate design", "view outline", "skip"], timeout=3)
            if found == "skip":
                ui.info("   ⏭  skipped a Canva question (brand/style picker)")
                found = ""
                time.sleep(3)
                continue
            if found:
                break
        if halted():
            return []
        if not found:
            return ["Canva AI answered, but no outline or Generate design "
                    "button appeared — check the open tab."]
        if found == "view outline":
            # The card answers a click only once the reply has settled; the
            # fifth live run pressed it while "Updated memory" was still
            # landing and the pane never opened. Press, look for the button,
            # and press again for up to a minute.
            pressed = False
            until = time.time() + 60
            while time.time() < until and not halted():
                time.sleep(3)
                if _click_control(driver, ["generate design"], timeout=4):
                    pressed = True
                    break
                _click_control(driver, ["view outline"], timeout=2)
            if not pressed:
                return ["Canva AI drafted an outline but the Generate design "
                        "button was not found — open the tab and press it by "
                        "hand."]
        ui.info("   🎨  pressed Generate design — waiting for the deck")
        # The outline pane closes and the reply grows a preview card once
        # the press took. If the button is still there after a moment, the
        # click did not land -- press it once more before waiting.
        time.sleep(4)
        if _click_control(driver, ["generate design"], timeout=2):
            ui.info("   🎨  pressed Generate design again")

        # Move two: the deck. The reply grows a preview card and a closing
        # message; watch the text settle the way every other tool's is.
        _smart_wait(driver, agent_cfg, int(agent_cfg.get("wait_time", 400)),
                    expect="", should_stop=should_stop)
        texts = _capture(driver, agent_cfg)
        # Left on the thread on purpose: that is where the card that opens
        # the deck lives, and it is what the saved link points at.
        return texts or ["Canva AI generated the design — open the tab to "
                         "see it (the reply text could not be read)."]
    except Exception as e:
        return [f"Canva AI automation stopped early ({e}). Its tab is open — "
                "the design may still be there."]


# ── NotebookLM (Google's "Gemini Notebook") ──────────────────────────────────
#
# Read off the live page on 13 Sep 2026 by attaching Playwright to Prism's
# own signed-in Chrome (the run's Chrome keeps a debugging port open; the
# probes are devtools/nb_probe*.py's siblings in the session scratchpad).
# notebooklm.google.com now redirects to notebook.google.com and the page
# calls itself "Gemini Notebook". What is there:
#
#   home      button[aria-label='Create new notebook']
#   notebook  button[aria-label='Add source'] opens a dialog with "Upload
#             files" (the OS file picker -- there is NO <input type=file>
#             on the page, which is why the generic upload path reports
#             "no file-upload field"), "Websites", "Drive", "Copied text".
#             "Copied text" shows textarea[aria-label='Pasted text'] and an
#             "Insert" button that enables once there is text.
#             chat      textarea[aria-label='Query box'] + Submit
#             Studio    [role=button][aria-label='Video Overview'], 'Audio
#                       Overview', 'Slide Deck', … Each card carries a
#                       pencil (.option-icon) that opens "Customize …":
#                         Video: Format {Short, Explainer}, a focus box
#                               "What should the video focus on?",
#                               "Generate later" / "Generate now".
#                         Audio: Format {Deep Dive, Brief, Critique,
#                               Debate}, Length {Short, Default, Long},
#                               a focus box, the same two buttons.
#                       Clicking the card itself generates with defaults.
#
# The first version of this runner looked for "Copied text" without ever
# pressing "Add source", so on a fresh notebook it searched a dialog that
# was not open and gave up -- the owner's run of 13 Sep 2026 ("generate me
# a google notebook lm video") typed nothing into NotebookLM at all.
#
# What a finished Video/Audio Overview looks like in the Studio list was
# NOT seen (nothing was generated during the probe), so _nb_wait_generated
# and _nb_download are best effort and say so when they fail.

_NB_NOTEBOOK_URL = re.compile(r"/notebook/[0-9a-f-]{16,}")
_NB_SOURCE_MAX = 300_000          # characters per pasted source
_NB_SOURCES_MAX = 20              # sources per notebook, keeps the run bounded
_NB_LONG_WORDS = re.compile(
    r"\b(long[- ]?form|long|full|detailed|comprehensive|in[- ]depth|"
    r"documentary)\b", re.I)
_NB_SHORT_WORDS = re.compile(
    r"\b(short|shorts|reel|bite[- ]?sized|brief|quick|teaser|"
    r"\d{2}[- ]?sec(?:ond)?s?)\b", re.I)
_NB_AUDIO_WORDS = re.compile(
    r"\b(audio|podcast|voice[- ]?over|narration|narrated|episode)\b", re.I)
_NB_VIDEO_WORDS = re.compile(r"\b(video|reel|film|explainer)\b", re.I)


def _nb_wants(stage: str, line: str, query: str = "") -> str:
    """What to make in NotebookLM: "video", "audio" or "chat".

    The person's own words win over the step: the owner's run had
    NotebookLM on the AUDIO step ("create a clear voice-over audio file")
    for a request that said "generate me a google notebook lm video".
    """
    asked_video = bool(_NB_VIDEO_WORDS.search(query or ""))
    asked_audio = bool(_NB_AUDIO_WORDS.search(query or ""))
    if asked_video and not asked_audio:
        return "video"
    if asked_audio and not asked_video:
        return "audio"
    if stage == "media":
        return "video"
    if stage == "audio":
        return "audio"
    words = line or ""
    if _NB_VIDEO_WORDS.search(words):
        return "video"
    if _NB_AUDIO_WORDS.search(words):
        return "audio"
    return "chat"


def _nb_format(make: str, line: str, query: str = "") -> tuple[str, str]:
    """(format label, length label) for the Customize dialog. Long-form is
    the default the owner asked for: a video is an Explainer unless the
    words say short; an audio is a Deep Dive, Long when the words say so."""
    words = f"{query or ''} {line or ''}"
    short = bool(_NB_SHORT_WORDS.search(words)) and not _NB_LONG_WORDS.search(words)
    if make == "video":
        return ("Short" if short else "Explainer", "")
    if make == "audio":
        if short:
            return ("Deep Dive", "Short")
        return ("Deep Dive", "Long" if _NB_LONG_WORDS.search(words) else "")
    return ("", "")


def _nb_focus(line: str, query: str = "") -> str:
    """The Customize dialog's focus text: the person's words first, then
    the step's one line without its 'For this step:' prefix."""
    step = re.sub(r"^\s*for this step\s*:\s*", "", (line or "").strip(), flags=re.I)
    parts = [p for p in ((query or "").strip(), step) if p]
    return "\n".join(parts)[:1500]


def _nb_sources(prior, attachments) -> tuple[list[tuple[str, str]], list[str]]:
    """What goes into the notebook as sources: every earlier step's FULL
    answer (NotebookLM is a source tool -- it wants the material, not the
    four-line hand-off), then every attachment Prism could read text out
    of. Returns (sources, names of attachments with no text to give)."""
    out: list[tuple[str, str]] = []
    for stage, texts in prior or []:
        text = "\n\n".join(t for t in (texts or []) if t and t.strip()).strip()
        if text:
            out.append((f"Prism — {A.STEP_NAMES.get(stage, stage)}", text))
    unreadable: list[str] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        text = (att.get("text") or "").strip()
        name = att.get("name") or os.path.basename(att.get("path") or "") or "attachment"
        if text:
            out.append((name, text))
        else:
            unreadable.append(name)
    return out[:_NB_SOURCES_MAX], unreadable


def _nb_el(driver, css: str, timeout: float = 10, visible: bool = True):
    """The first element matching `css` (visible unless told otherwise),
    polled up to `timeout`; None when nothing shows up."""
    from selenium.webdriver.common.by import By
    deadline = time.time() + max(timeout, 0)
    while True:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css):
                if not visible or el.is_displayed():
                    return el
        except Exception:                               # noqa: BLE001
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _nb_click(driver, css: str = "", texts=(), timeout: float = 10) -> bool:
    """Click by selector first, by visible text second. Never raises."""
    el = _nb_el(driver, css, timeout) if css else None
    if el is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:                               # noqa: BLE001
            pass
        for attempt in (lambda: el.click(),
                        lambda: driver.execute_script("arguments[0].click();", el)):
            try:
                attempt()
                return True
            except Exception:                           # noqa: BLE001
                continue
    return _click_by_text(driver, list(texts), timeout=timeout) if texts else False


def _nb_click_xpath(driver, xpath: str, timeout: float = 10) -> bool:
    from selenium.webdriver.common.by import By
    deadline = time.time() + timeout
    while True:
        try:
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed() and el.is_enabled():
                    el.click()
                    return True
        except Exception:                               # noqa: BLE001
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _nb_dialog(driver, timeout: float = 1):
    return _nb_el(driver, "[role='dialog']", timeout)


def _nb_wait_dialog_closed(driver, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and _nb_dialog(driver, 0.2) is not None:
        time.sleep(0.5)


def _nb_new_notebook(driver, timeout: float = 30) -> bool:
    """A fresh notebook, so this run's sources never mix with old ones."""
    if not _nb_click(driver, "button[aria-label='Create new notebook']",
                     ("create new notebook", "create new"), timeout=15):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _NB_NOTEBOOK_URL.search(driver.current_url or ""):
                time.sleep(2)
                return True
        except Exception:                               # noqa: BLE001
            pass
        time.sleep(1)
    return False


def _nb_add_text_source(driver, title: str, text: str) -> bool:
    """Add sources → Copied text → paste → Insert. A fresh notebook may open
    the dialog on its own; an existing one needs the button pressed."""
    if _nb_el(driver, "[role='dialog'] textarea[aria-label='Pasted text']", 1) is None:
        if _nb_dialog(driver, 1) is None:
            if not _nb_click(driver, "button[aria-label='Add source']",
                             ("add source",), timeout=10):
                return False
        if not _nb_click(driver, "", ("copied text",), timeout=10):
            return False
    box = _nb_el(driver, "[role='dialog'] textarea[aria-label='Pasted text'], "
                         "[role='dialog'] textarea", 10)
    if box is None:
        return False
    body = f"{title}\n\n{text}"[:_NB_SOURCE_MAX]
    if not _fast_type(driver, box, body):
        try:
            box.send_keys(body)
        except Exception:                               # noqa: BLE001
            return False
    time.sleep(0.5)
    if not _nb_click_xpath(driver, "//*[@role='dialog']//button[normalize-space(.)='Insert']",
                           timeout=10):
        return False
    _nb_wait_dialog_closed(driver, 20)
    return True


def _nb_wait_sources(driver, cap: float = 120) -> None:
    """Sources are read in the background; a Studio request before that
    finishes is refused. Spinner-based, capped."""
    start = time.time()
    time.sleep(2)
    while time.time() - start < cap:
        try:
            busy = driver.execute_script(
                "return !!document.querySelector(\"[role='progressbar'], "
                "mat-progress-bar, mat-spinner, [aria-busy='true']\");")
        except Exception:                               # noqa: BLE001
            busy = False
        if not busy:
            return
        time.sleep(3)


_NB_STUDIO_JS = """
const h = [...document.querySelectorAll('h2')].find(e => /^\\s*Studio\\s*$/.test(e.innerText || ''));
if (!h) return null;
let p = h;
for (let i = 0; i < 6 && p.parentElement && p.parentElement !== document.body; i++) {
  p = p.parentElement;
  if ((p.innerText || '').length > 400) break;
}
const text = p.innerText || '';
const named = [...p.querySelectorAll("button[aria-label], [role='button'][aria-label], a[aria-label]")]
  .map(b => b.getAttribute('aria-label') || '');
return {
  generating: /generating|creating|in progress|this (?:may|can|will) take|loading/i.test(text),
  menus: named.filter(n => /more|option|menu/i.test(n)).length,
  downloads: named.filter(n => /download/i.test(n)).length,
  text: text.slice(0, 400),
};
"""

_NB_OPEN_MENU_JS = """
const h = [...document.querySelectorAll('h2')].find(e => /^\\s*Studio\\s*$/.test(e.innerText || ''));
if (!h) return false;
let p = h;
for (let i = 0; i < 6 && p.parentElement && p.parentElement !== document.body; i++) {
  p = p.parentElement;
  if ((p.innerText || '').length > 400) break;
}
const dl = [...p.querySelectorAll("button[aria-label], a[aria-label]")].filter(b => /download/i.test(b.getAttribute('aria-label') || ''));
if (dl.length) { dl[dl.length - 1].click(); return 'download'; }
const menus = [...p.querySelectorAll("button[aria-label], [role='button'][aria-label]")].filter(b => /more|option|menu/i.test(b.getAttribute('aria-label') || ''));
if (!menus.length) return false;
menus[menus.length - 1].click();
return 'menu';
"""


def _nb_studio_state(driver) -> dict:
    try:
        return driver.execute_script(_NB_STUDIO_JS) or {}
    except Exception:                                   # noqa: BLE001
        return {}


def _nb_generate(driver, make: str, fmt: str, length: str, focus: str) -> str:
    """Open the card's Customize dialog, set format/length/focus, press
    Generate now. "" on success, else what could not be found."""
    card = "Video Overview" if make == "video" else "Audio Overview"
    pencil = f"[role='button'][aria-label='{card}'] .option-icon"
    if not _nb_click(driver, pencil, (), timeout=15):
        # No pencil: the card itself generates with the defaults.
        if not _nb_click(driver, f"[role='button'][aria-label='{card}']",
                         (card.lower(),), timeout=10):
            return f"the Studio → {card} card couldn't be found"
        return ""
    if _nb_dialog(driver, 10) is None:
        return f"the Customize {card} dialog never opened"
    if fmt:
        _nb_click_xpath(driver, "//*[@role='dialog']//label[starts-with(normalize-space(.), "
                                f"'{fmt}')]", timeout=5)
    if length:
        _nb_click_xpath(driver, "//*[@role='dialog']//button[@role='radio']"
                                f"[normalize-space(.)='{length}']", timeout=5)
    if focus:
        box = _nb_el(driver, "[role='dialog'] textarea", 5)
        if box is not None and not _fast_type(driver, box, focus):
            try:
                box.send_keys(focus)
            except Exception:                           # noqa: BLE001
                pass
    if not _nb_click_xpath(driver, "//*[@role='dialog']//button[normalize-space(.)='Generate now']",
                           timeout=10):
        return "no 'Generate now' button in the Customize dialog"
    _nb_wait_dialog_closed(driver, 20)
    return ""


def _nb_wait_generated(driver, cap: float, halted, baseline_menus: int = 0,
                       poll: float = 10) -> bool:
    """Watch the Studio panel until the render is done: it says it is
    generating and then stops saying so, or a new menu/download control
    appears on the list. False on the cap or a Stop."""
    start, seen = time.time(), False
    while time.time() - start < cap:
        if halted():
            return False
        st = _nb_studio_state(driver)
        if st.get("generating"):
            seen = True
        elif seen or st.get("downloads") or st.get("menus", 0) > baseline_menus:
            return True
        time.sleep(poll)
    return False


def _nb_download(driver, stage: str, wait: float = 600) -> list[dict]:
    """The finished overview as a file: the newest Studio item's menu →
    Download (or its own Download control), captured into a scratch
    folder the way every click-to-download file is."""
    def click() -> bool:
        try:
            how = driver.execute_script(_NB_OPEN_MENU_JS)
        except Exception:                               # noqa: BLE001
            how = False
        if how == "download":
            return True
        if how == "menu":
            time.sleep(1)
            if _click_by_text(driver, ["download"], timeout=5):
                return True
        return _click_by_text(driver, ["download"], timeout=3)
    return _capture_download(driver, stage, click, wait=wait,
                             menu_words=("mp4", "m4a", "download"))


def _nb_chat(driver, agent_cfg: dict, prompt: str) -> list[str]:
    """Any other step: ask the notebook and read what it says."""
    from selenium.webdriver.common.keys import Keys
    box = _nb_el(driver, agent_cfg.get("textarea_selector")
                 or "textarea[aria-label='Query box'], textarea", 15)
    if box is None:
        return ["NotebookLM: the sources were added, but no chat box was "
                "found to ask the question — check the open tab."]
    try:
        before = driver.execute_script("return document.body.innerText || '';")
    except Exception:                                   # noqa: BLE001
        before = ""
    if not _fast_type(driver, box, prompt):
        box.send_keys(prompt)
    try:
        box.send_keys(Keys.ENTER)
    except Exception:                                   # noqa: BLE001
        _nb_click(driver, "button[type='submit'], button[aria-label='Submit']", ("submit",), 5)
    time.sleep(int(agent_cfg.get("wait_time", 45) or 45))
    try:
        after = driver.execute_script("return document.body.innerText || '';")
    except Exception:                                   # noqa: BLE001
        after = ""
    fresh = after[len(before):] if after.startswith(before) else after
    fresh = fresh.replace(prompt, "").strip()
    if len(fresh) > 50:
        return [fresh]
    return ["NotebookLM answered, but no response text could be read off the "
            "page — check the open tab."]


def _run_notebooklm(driver, agent_cfg: dict, stage: str, line: str, *,
                    sources=(), unreadable=(), query: str = "",
                    should_stop=None) -> tuple[list[str], list[dict]]:
    """Drive NotebookLM for one step. Returns (texts, files).

      1. a fresh notebook;
      2. every source pasted in as "Copied text" -- the earlier steps' full
         answers and the readable attachments (see _nb_sources);
      3. wait for the sources to be read;
      4. what the step wants (_nb_wants): a Video Overview or an Audio
         Overview through the card's Customize dialog, then wait for the
         render and download it; or, on an ordinary step, a question in
         the chat.
    """
    halted = (lambda: bool(should_stop and should_stop()))
    where = ""
    try:
        if not _nb_new_notebook(driver):
            return (["NotebookLM: couldn't create a notebook — the 'Create new "
                     "notebook' button was not found. Check the open tab."], [])
        where = driver.current_url
        added = 0
        for title, text in sources:
            if halted():
                return (["NotebookLM: stopped while adding sources."], [])
            if _nb_add_text_source(driver, title, text):
                added += 1
                ui.info(f"   📚  source {added}: {title[:60]}")
            else:
                ui.warn(f"   couldn't add source '{title[:60]}' — the Add "
                        "source → Copied text path did not go through")
                break
        if not added:
            return ([f"NotebookLM: no source could be added to the notebook "
                     f"({where}). Check the open tab — the notebook is there, empty."], [])
        if unreadable:
            ui.warn("   these attachments have no text Prism could paste in "
                    "and NotebookLM has no upload field on the page: "
                    + ", ".join(list(unreadable)[:4]))
        _nb_wait_sources(driver)

        make = _nb_wants(stage, line, query)
        if make == "chat":
            return (_nb_chat(driver, agent_cfg, line or query), [])

        fmt, length = _nb_format(make, line, query)
        focus = _nb_focus(line, query)
        baseline = int(_nb_studio_state(driver).get("menus", 0) or 0)
        why = _nb_generate(driver, make, fmt, length, focus)
        label = ("Video Overview" if make == "video" else "Audio Overview") + \
                (f" ({fmt}{', ' + length if length else ''})" if fmt else "")
        if why:
            return ([f"NotebookLM: {added} source(s) added to {where}, but the "
                     f"{label} could not be requested — {why}. Open the tab and "
                     "press Generate yourself."], [])
        cap = int(agent_cfg.get("generate_wait", 1500) or 1500)
        ui.info(f"   🎬  {label} requested — waiting up to {cap // 60} min for "
                "NotebookLM to render it")
        if not _nb_wait_generated(driver, cap, halted, baseline):
            return ([f"NotebookLM {label} requested from {added} source(s) — "
                     f"still rendering when Prism stopped waiting. It finishes "
                     f"in the notebook: {where}"], [])
        files = _nb_download(driver, stage)
        if files:
            ui.ok(f"   ✅  {label} downloaded: {files[0].get('name')}")
            return ([f"NotebookLM {label} generated from {added} source(s) and "
                     f"saved as {files[0].get('name')}. Notebook: {where}"], files)
        return ([f"NotebookLM {label} generated from {added} source(s) — it is "
                 f"in the notebook's Studio panel ({where}); Prism could not "
                 "find its Download control to save a copy."], [])
    except Exception as e:                              # noqa: BLE001
        return ([f"NotebookLM automation stopped early ({e}). Check the open "
                 f"tab{' — ' + where if where else ''}; the notebook may be "
                 "usable from there."], [])


# Words that only appear on a page asking you to identify yourself. Matched
# against the visible body text, lowercased. Kept deliberately specific: "sign
# in" alone also appears in the header of a page you ARE signed into, so every
# entry here has to be something a signed-in user would not be looking at.
_SIGNIN_MARKERS = (
    "sign in to continue", "log in to continue", "please sign in",
    "please log in", "sign in to your account", "create an account to",
    "you must be logged in", "session expired", "your session has expired",
    "continue with google", "sign in with google", "verify you are human",
    "are you a robot", "checking your browser",
)


# A tool that has run out for now. Kept apart from _SIGNIN_MARKERS because the
# two need opposite advice: signing in is something only the user can do,
# whereas an exhausted quota is something Prism can route around by itself.
# Telling somebody to "sign in again" when Claude has merely used up its free
# messages sends them off to fix a thing that is not broken.
_EXHAUSTED_MARKERS = (
    "you've reached your limit", "you have reached your limit",
    "message limit reached", "reached the limit", "usage limit",
    "out of free messages", "no free messages", "free messages remaining",
    "daily limit", "limit resets", "limit will reset",
    "upgrade to continue", "upgrade your plan to continue",
    "subscribe to continue", "too many requests", "rate limit",
    "quota exceeded", "out of credits", "no credits remaining",
    "plan limit", "capacity constraints", "currently at capacity",
)


def _looks_exhausted(driver) -> str:
    """"" if the tool is fine, else why it cannot answer right now.

    The commonest way a long run dies at the last step: the free tier on
    whichever tool drew the heaviest stage runs out, and Prism reports "no
    response scraped" — which reads as Prism being broken rather than as
    "Claude is finished for the next few hours, use something else".

    Only consulted when a stage produced nothing, so a false positive costs one
    wrong sentence and one unnecessary attempt on another tool — never a good
    answer.
    """
    try:
        body = (driver.find_element("tag name", "body").text or "").lower()
    except Exception:
        return ""
    if not body:
        return ""
    # No length ceiling here, unlike the sign-in check below. A quota notice
    # appears ON a full conversation page with every previous turn still on it,
    # so the "a real page is long" heuristic that makes the sign-in check safe
    # would miss every one of these.
    for marker in _EXHAUSTED_MARKERS:
        if marker in body:
            return (f"This tool has hit its usage limit for now — the page "
                    f"says \"{marker}\".")
    return ""


def _looks_signed_out(driver) -> str:
    """"" if the page looks usable, else what the user has to do about it.

    Not being signed in is the single most common reason a run comes back with
    nothing, and it was indistinguishable from every other empty result: the
    scrape found no reply, so the user was told "Prism couldn't read the
    response off the page" — which reads as Prism being broken rather than as
    "log in to Perplexity". Cheap to check and only consulted when a stage
    produced nothing, so a false positive costs a wrong sentence, not a run.
    """
    try:
        body = (driver.find_element("tag name", "body").text or "").lower()
    except Exception:
        return ""
    if not body or len(body) > 4000:
        # A real conversation page is long; a sign-in wall is short. This keeps
        # the check off pages that merely mention signing in somewhere.
        return ""
    for marker in _SIGNIN_MARKERS:
        if marker in body:
            if "robot" in marker or "human" in marker or "browser" in marker:
                return ("This tool is showing a human-verification check. Open "
                        "the tab, clear it once, and run again — Prism reuses "
                        "the same browser profile, so it usually only asks once.")
            return ("You're not signed in to this tool. Use “Login tabs” in the "
                    "sidebar, sign in once in the window Prism opens, then run "
                    "again — the login is remembered from then on.")
    return ""


def _looks_unreadable(driver, questions, timed_out: bool = False) -> str:
    """"" unless the page holds a real conversation we could not read.

    The third case, and the one that had no message of its own. A stage that
    captures nothing is one of four things: the tool ran out (_looks_exhausted),
    the page is a sign-in or robot wall (_looks_signed_out), the answer was
    still being written, or THE SITE CHANGED ITS MARKUP and our selector no
    longer matches. The last two both landed on "it returned nothing", which
    tells the customer only that Prism failed.

    Distinguished by our own prompt being on the page: if it is, we reached the
    tool, typed, and submitted successfully — so this is not a login problem and
    saying so sends the customer to fix something that is not broken.

    Note the length ceiling in _looks_signed_out is doing its job and is not the
    bug: it is what stops a long conversation page being reported as a sign-in
    wall. This function covers the case that ceiling deliberately lets through.
    """
    try:
        body = (driver.find_element("tag name", "body").text or "")
    except Exception:
        return ""
    if len(body) <= 4000:
        return ""                       # short page: the other two checks own it
    asked = [q for q in (questions or []) if q and len(q) > 40]
    if not any(q[:40].lower() in body.lower() for q in asked):
        return ""                       # cannot prove we got our prompt in
    if timed_out:
        return ("This tool was still writing its answer when Prism stopped "
                "waiting. The tab is still open and the answer will finish "
                "there — give it longer in Settings, or copy it from the tab.")
    return ("This tool answered, but Prism could not read the reply off the "
            "page — the site has most likely changed its layout. Your prompt "
            "went through, so the answer is in the open tab and can be copied "
            "from there. Please report it: this needs a Prism update, and it "
            "is not something signing in again will fix.")


def _match(driver, selector: str) -> list:
    from selenium.webdriver.common.by import By
    if not selector:
        return []
    try:
        return driver.find_elements(By.CSS_SELECTOR, selector)
    except Exception:
        return []                       # invalid selector, or the page moved


# The page's own furniture, scraped along with the answer: a "Claude
# responded:" caption, a "Thought for 9s" pill (twice, when the pane
# re-rendered). Left in, it rides the relay into the NEXT tool's prompt —
# where a fabricated-looking "Claude responded:" header is exactly what a
# model's prompt-injection defences are trained to refuse. One real run
# died that way: Claude read the relayed script as a scripted takeover and
# declined to produce the design at all. Fences and content are untouched.
_CAPTION_RE = re.compile(
    # ONE token (plus an optional version) before the verb: "Claude
    # responded:", "Kimi 2.6 said:". Never two words — "The customer said:"
    # is content, and an earlier, looser pattern ate it.
    r"^\s*[A-Z][\w.-]{1,15}(?:\s+\d[\w.]*)?\s+"
    r"(?:responded|replied|said|wrote|answered)\s*:\s*")
_THOUGHT_RE = re.compile(
    r"\b(?:Thought|Thinking|Reasoned|Reasoning)(?:\s+(?:for|about))?\s+\d+\s*"
    r"(?:s|sec|secs|seconds?|m|min|mins|minutes?)\b\.?", re.IGNORECASE)


def _clean_capture(text: str) -> str:
    t = _CAPTION_RE.sub("", text or "", count=1)
    t = _THOUGHT_RE.sub("", t)
    return t.strip()


def _capture(driver, agent_cfg: dict, keep: str = "") -> list[str]:
    """Everything on the page that reads as a reply, longest captures only.

    `keep` is a marker a caller is waiting for. A reply that carries it is
    kept however short it is: the length floor below exists to drop
    buttons and chips, and "CANVA LINK: none" is sixteen characters -- so
    the one answer _make_editable most needs to see was the one this used
    to throw away, and a disconnected Canva app read as a silent one.

    Falls back to the GENERIC selector when the hand-tuned one matches nothing.
    The tuned selectors are pinned to markup we do not own: these sites roll
    redesigns out in buckets, so the same version of Prism can be right for one
    customer and wrong for another on the same afternoon. A tuned selector that
    matches is always preferred — the generic one is broader and picks up more
    noise — but matching nothing at all is the one case where broader-and-noisy
    beats exact-and-empty, because empty is indistinguishable from failure.
    """
    elements = _match(driver, agent_cfg.get("response_selector", ""))
    if not elements:
        from . import agents as _agents
        generic = _agents._GENERIC["response_selector"]
        if generic != agent_cfg.get("response_selector", ""):
            elements = _match(driver, generic)
            if elements:
                ui.info("   🔁  tuned selector matched nothing; "
                        "read the reply with the generic one")
    texts = []
    for el in elements:
        try:
            t = el.text.strip()
        except Exception:
            continue
        wanted = bool(keep) and keep.lower() in t.lower()
        if (len(t) > 50 or wanted) and t not in texts:
            texts.append(t)
    # Response selectors often match a container AND pieces inside it
    # (sections, citation chips…). Keep only the fullest captures: drop any
    # text that is contained inside another element's text.
    texts = [t for t in texts if not any(t != u and t in u for u in texts)]
    # Several tools render OUR message with the same classes as the reply, so
    # the prompt comes back as a "response" — which then gets forwarded
    # downstream, or (for /email) parsed as a draft whose subject is the
    # template we typed.
    echoes = [t for t in texts if _is_prompt_echo(t)]
    if echoes:
        texts = [t for t in texts if t not in echoes]
        ui.info(f"   ↩️   ignored {len(echoes)} echo(es) of our own prompt")
    return [c for c in (_clean_capture(t) for t in texts) if c]


# Stages where "make it editable" means anything.
#
# Exactly the two the registry configures a Canva suffix for, and no more.
# This first shipped including "artwork" and "design" as well, which was
# wrong and showed up on the first real run: the Studio pipeline's design
# stage emits a JSON scene spec for Prism's own renderer, so the follow-up
# asked Canva to "import the image above" when the message above was a CSS
# blob. Canva answered "none", and the turn was pure waste on a conversation
# that had just been asked, twice, for strict JSON.
#
# Both of those stages are internal plumbing of the Studio pipeline. Neither
# is ever the thing a customer opens and edits.
_EDITABLE_STAGES = ("visual", "presentation")


def _make_editable(driver, agent_cfg: dict, stage: str, query: str,
                   responses: list,
                   machine_shaped: bool = False,
                   made_image: bool = False) -> tuple[list, str]:
    """Hand the image just generated to Canva, in the same conversation.

    Two prompts, not one. The first asked for the best picture the tool can
    draw; this one asks Canva to wrap that picture in something editable.

    Splitting it is the whole point. Asking for both in a single prompt makes
    Canva COMPOSE the image — a stock template where DALL-E would have
    rendered the scene — so the customer had to choose between a good picture
    and an editable one. Generate first, convert second, and they get both.

    Returns `(responses, canva_url)`. The Canva reply is appended to
    `responses` rather than substituted: the first answer holds the image,
    and dropping it to keep a link would lose the artwork the customer
    actually asked for. `canva_url` is the design's own URL, pulled out of
    that reply — the caller saves it as the artifact's link so "editable"
    means something a customer can actually click open, instead of pointing
    back at the ChatGPT tab where the link is just one more line of text.
    """
    if stage not in _EDITABLE_STAGES:
        return responses, ""
    if machine_shaped:
        # This stage's answer is parsed by Prism, not read by a person. It was
        # just told to reply with ONLY a JSON object; adding a chat turn after
        # that both wastes a round trip and leaves prose sitting where the
        # parser expects to find the spec.
        return responses, ""
    if not A.wants_canva(query):
        return responses, ""
    if not responses and not made_image:
        # Nothing was made, so there is nothing to convert. Asking anyway
        # would have Canva invent a design from the prompt alone, which is
        # exactly the template-instead-of-artwork failure this avoids.
        #
        # `made_image` is the half this check used to miss. ChatGPT answers
        # an image request with the picture and NO prose -- the assistant
        # turn holds an <img> and an "Edit" button -- so the text capture
        # is empty even though the artwork is right there. Seen on the
        # 2026-09-09 Playwright run: the picture rendered, the Canva step
        # was skipped as "nothing to make editable", and the customer who
        # had asked for something editable got a flat PNG.
        ui.warn("   nothing to make editable — skipping the Canva step")
        return responses, ""

    ui.info("   🎨  asking Canva to make it editable…")
    reply = _reask(driver, agent_cfg, A._CANVA_FOLLOWUP, expect="CANVA LINK:")
    text = "\n\n".join(t for t in reply if t and t.strip()).strip()
    if not text:
        ui.warn("   Canva didn't answer — keeping the image on its own")
        return responses, ""
    if "canva link: none" in text.lower():
        # Said plainly rather than swallowed: the customer asked for something
        # editable and is not getting it, and the reason is one they can fix.
        ui.warn("   the Canva app isn't connected to this ChatGPT account — "
                "connect it there and the design becomes editable next time")
        return responses, ""
    match = re.search(r"canva link:\s*(\S+)", text, re.IGNORECASE)
    canva_url = match.group(1).rstrip(").,") if match else ""
    ui.ok("   ✅  editable Canva design created")
    return responses + [text], canva_url


def _rescue_json_from_page(driver, marker: str) -> list[str]:
    """Code-shaped text anywhere on the page that carries `marker`, oldest
    first -- the reply's own code block, a Claude artifact panel, a ChatGPT
    canvas, an editor. _capture() reads the reply element; when a tool has
    moved a long JSON answer into a side panel the reply element holds a
    sentence and the JSON sits beside it. Prism's own prompt echoes are
    dropped, since the design prompt contains an example. Never raises."""
    js = """
        const marker = arguments[0];
        const sel = "pre, code, .cm-content, [data-testid*='artifact' i], "
                  + "[class*='artifact' i], [class*='canvas' i], "
                  + "[role='dialog'], textarea";
        const out = [], seen = new Set();
        for (const el of document.querySelectorAll(sel)) {
            const t = (el.innerText || el.value || el.textContent || '').trim();
            if (t.length < 40 || !t.includes(marker) || seen.has(t)) continue;
            seen.add(t); out.push(t);
        }
        return out.slice(-6);
    """
    try:
        found = driver.execute_script(js, marker) or []
    except Exception:
        return []
    return [t for t in found if isinstance(t, str) and not _is_prompt_echo(t)]


def _design_turn_text(driver, agent_cfg: dict, texts: list, web, ask) -> str:
    """The art-direction reply that actually parses, found wherever it is.

    Newest capture first (the reply is at the end of the chat; the prompt,
    with its example, is above it). Then the page's code panels -- an
    artifact or canvas the tool put the JSON in. Then one plain re-ask that
    names the problem, and both again. Falls back to the newest capture, so
    the caller's own error and its saved evidence are unchanged when nothing
    helps. Found on a client's Mac, 11 Sep 2026: "No JSON found in the
    agent's reply" with the design visibly on screen."""
    def parses(text: str) -> bool:
        try:
            web.parse_design(text)
            return True
        except Exception:                                   # noqa: BLE001
            return False

    for t in reversed([t for t in (texts or []) if str(t).strip()]):
        if parses(t):
            return t
    for marker in ('"storyboard"', '"design"'):
        for t in reversed(_rescue_json_from_page(driver, marker)):
            if parses(t):
                ui.info("   📋  the design was in a panel beside the chat, "
                        "not in the reply — read it from there")
                return t
    ui.warn("   the design did not arrive in the chat message — asking for "
            "it there, not in an artifact or canvas")
    got = ""
    try:
        got = ask(web.IN_CHAT_REASK, '"css"') or ""
    except Exception as e:                                  # noqa: BLE001
        ui.warn(f"   couldn't ask again: {e}")
    if got and parses(got):
        return got
    for marker in ('"storyboard"', '"design"'):
        for t in reversed(_rescue_json_from_page(driver, marker)):
            if parses(t):
                return t
    return texts[-1] if texts else got


def _reask(driver, agent_cfg: dict, prompt: str, expect: str = "",
           wait: int = 0, should_stop=None) -> list[str]:
    """Send one follow-up in the SAME tab and re-scrape.

    Used when a stage answered but not in the shape the next stage needs. The
    chat still holds everything it just wrote, so a one-line correction is far
    cheaper — and far likelier to work — than failing the run or starting the
    stage over.

    `wait` overrides the agent's own budget. A one-line correction settles in
    seconds; a whole reel scene is several thousand characters of markup and
    CSS and does not, and the default 60s cut them off mid-stylesheet."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    try:
        box = WebDriverWait(driver, agent_cfg.get("input_wait", 15)).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, agent_cfg["textarea_selector"])))
        if not _fast_type(driver, box, _bmp_safe(prompt)):
            box.send_keys(_bmp_safe(prompt).replace("\n", " "))
        time.sleep(1)
        sel = agent_cfg.get("submit_selector", "")
        clicked = False
        if sel:
            try:
                WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))).click()
                clicked = True
            except Exception:
                pass
        if not clicked:
            box.send_keys(Keys.ENTER)
        if wait >= 0:
            _smart_wait(driver, agent_cfg,
                        wait or agent_cfg.get("wait_time", 60), expect=expect,
                        should_stop=should_stop)
        return _capture(driver, agent_cfg, keep=expect)
    except Exception as e:
        ui.err(f"   follow-up failed: {e}")
        return []


# How long a follow-up may wait for a tool, at least. A follow-up redoes a
# whole deliverable — a document, a set of scenes — and the tool's everyday
# budget (ChatGPT's is 300s) was cutting those off mid-way. A ceiling, not a
# duration: the wait ends the moment the answer settles.
FOLLOWUP_MIN_WAIT = 480


def followup_wait(agent_cfg: dict) -> int:
    """The wait ceiling for a follow-up turn on this tool."""
    return max(int(agent_cfg.get("wait_time", 60) or 60), FOLLOWUP_MIN_WAIT)


def _adopt_assets(spec: dict, files, generated=None) -> str:
    """Add pictures to a filmed reel's artwork under new names — `new1`,
    `new2`… — so nothing already placed is renamed underneath it. Returns
    the manifest of what was added, for the design chat."""
    from . import assets as _assets
    try:
        table = _assets.collect(files, generated=generated)
    except Exception as e:                               # noqa: BLE001
        ui.warn(f"   couldn't prepare the new artwork ({e})")
        return ""
    if not table:
        return ""
    have = spec.setdefault("_assets", {})
    fresh, n = {}, 1
    for entry in table.values():
        while f"new{n}" in have:
            n += 1
        have[f"new{n}"] = {**entry, "kind": "art"}
        fresh[f"new{n}"] = have[f"new{n}"]
        n += 1
    ui.ok(f"✂️   {len(fresh)} new asset(s): " + ", ".join(fresh))
    return _assets.manifest(fresh)


def studio_followup(cfg: dict, spec: dict, agent_name: str, design_url: str,
                    change: str, attachments=None, on_event=None,
                    on_progress=None, images: str = "",
                    context: str = "", task: str = "",
                    title: str = "") -> tuple[str, str, str]:
    """A change to a filmed Studio reel, made where the reel was designed.

    Reopens the design conversation (its URL was saved with the run), asks
    for only the scenes that change, and re-films locally. Returns (mp4,
    spec path, note). The previous cut is left where it was; the new one
    gets its own stamp beside it, so History keeps both.

    A follow-up can need more than the design chat. `images` is a picture
    the owner asked for — made first, in the image tool, and adopted as
    `asset:new…` so the design chat can place it. `context` is what an
    earlier step of the same follow-up produced (a rewritten script), so
    the design chat changes the scenes it affects rather than guessing.

    Before this, a follow-up on a reel went through the same classifier as
    any other task and landed on the local renderer — a stage with a file
    for a link and no chat to resume — or on the writer, in a fresh tab that
    had never seen the design. Neither could change a scene.
    """
    import copy
    import json as _json
    import time as _time
    from . import reel_web as _web, config as C

    def emit(kind, payload):
        if on_event:
            on_event(kind, payload)

    agent_cfg = (A.resolve_agent("design", agent_name)
                 or A.resolve_agent("brains", agent_name))
    if not agent_cfg:
        raise RuntimeError(f"{agent_name} isn't in Prism's tool registry, so "
                           "the design conversation can't be reopened.")
    spec = copy.deepcopy(spec)
    cur_dir = C.current_run_dir()
    if cur_dir and os.path.isdir(cur_dir):
        C.continue_run(cur_dir, task=task or change,
                       title=title or C.current_run_title())
    else:
        C.begin_run(task or change,             # this follow-up's own folder
                    title=title or C.fallback_title(task or change))
    listing = ""
    if attachments:
        listing = _adopt_assets(spec, attachments)

    driver = None
    files = []
    if images.strip():
        # The picture first, in the tool that can draw — a fresh tab, since
        # this is a new job, not a continuation. Harvested off the page the
        # way the imagery stage's are, then adopted into the reel's artwork.
        maker = (cfg.get("agents") or {}).get("visual") or "ChatGPT"
        maker_cfg = A.resolve_agent("visual", maker)
        if not maker_cfg:
            ui.warn(f"   {maker} can't make pictures here — going on without")
        else:
            emit("stage_start", {"stage": "artwork", "agent": maker})
            ui.rule(f"ARTWORK  ·  {maker}  ·  follow-up",
                    style=A.CATEGORIES.get("visual", {}).get("color", "pink"))
            driver, _ = _get_driver(cfg)
            _open_tab(driver, maker)
            driver.get(maker_cfg["url"])
            _time.sleep(maker_cfg.get("page_wait", 4))
            import re
            m = re.search(r'\b([1-6])\s*(?:new\s*)?(?:images?|pictures?|artworks?|photos?|assets?)\b', images, re.I)
            word_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
            if m:
                want = int(m.group(1))
            else:
                m_word = re.search(r'\b(one|two|three|four|five|six)\s+(?:new\s*)?(?:images?|pictures?|artworks?|photos?|assets?)\b', images, re.I)
                if m_word:
                    want = word_map[m_word.group(1).lower()]
                elif any(p in images.lower() for p in ("images", "pictures", "artworks", "assets", "photos")):
                    want = min(4, max(2, len((spec.get("_scenes") or [])) - 1))
                else:
                    want = 1

            emit("waiting", {"stage": "artwork", "seconds": 75 * want})
            _reask(driver, maker_cfg,
                   _web.followup_imagery_instructions(images, spec),
                   wait=-1)
            ui.info(f"   ⏳  waiting for artwork asset 1 of {want} to finish rendering…")
            got = _wait_for_images(driver, maker_cfg, 1, cap=80, baseline_count=0)
            if got:
                ui.info(f"   🖼️   artwork asset 1 of {want} completed")
            else:
                ui.warn("   ⚠️  could not generate artwork asset 1")

            for num in range(2, want + 1):
                settle_deadline = _time.time() + 30
                while _time.time() < settle_deadline:
                    if not _chatgpt_is_busy(driver):
                        break
                    _time.sleep(2)

                before_dom = int(driver.execute_script(r"""
                    return document.querySelectorAll('img[src*="blob.core.windows.net"], img[src*="files.oaiusercontent.com"], img[src*="backend-api/files"]').length;
                """) or got)

                followup_prompt = (
                    f"Now generate artwork asset {num} of {want} for this reel using DALL-E. "
                    "Make it a different single clear subject with an isolated background and no text. "
                    "Do NOT search the web. Use the image tool now."
                )
                _reask(driver, maker_cfg, followup_prompt, wait=-1)
                ui.info(f"   ⏳  waiting for artwork asset {num} of {want} to finish rendering…")

                wait_start = _time.time()
                asset_finished = False
                per_asset_cap = 75
                while _time.time() - wait_start < per_asset_cap:
                    _time.sleep(3)
                    busy = _chatgpt_is_busy(driver)
                    try:
                        cur_dom = int(driver.execute_script(r"""
                            return document.querySelectorAll('img[src*="blob.core.windows.net"], img[src*="files.oaiusercontent.com"], img[src*="backend-api/files"]').length;
                        """) or 0)
                    except Exception:
                        cur_dom = before_dom

                    if cur_dom > before_dom and not busy:
                        asset_finished = True
                        break
                    if not busy and (_time.time() - wait_start) > 20 and cur_dom <= before_dom:
                        break

                if asset_finished:
                    got += 1
                    ui.info(f"   🖼️   artwork asset {got} of {want} completed")
                else:
                    ui.warn(f"   ⚠️  could not generate artwork asset {num} — proceeding with {got} image(s)")
                    break

            files = _harvest_images(
                driver, maker_cfg, "artwork",
                exclude_files=attachments or [])
            if want > 1 and len(files) > want:
                files = files[-want:]
            made = [f["path"] for f in files if f.get("path")]
            if made:
                listing = (listing + "\n" if listing else "") + \
                    _adopt_assets(spec, made, generated=set(made))
            else:
                ui.warn("   no picture came back — the design chat will be "
                        "told nothing new was made")
            try:
                url = driver.current_url
            except Exception:                            # noqa: BLE001
                url = maker_cfg["url"]
            emit("stage_done", {"stage": "artwork", "count": len(made),
                                "texts": [f"{len(made)} picture(s) made"]
                                if made else [],
                                "url": url, "timed_out": False})

    emit("stage_start", {"stage": "design", "agent": agent_name})
    ui.rule(f"DESIGN  ·  {agent_name}  ·  follow-up",
            style=A.CATEGORIES.get("design", {}).get("color", "pink"))
    if driver is None:
        driver, _ = _get_driver(cfg)
    _open_tab(driver, agent_name)
    driver.get(design_url)
    _time.sleep(agent_cfg.get("page_wait", 4))
    wait = followup_wait(agent_cfg)

    # Upload newly generated/attached artwork to the reopened design conversation
    if files:
        img_uploads = [f for f in files if f.get("path") and not str(f.get("path", "")).lower().endswith(
            (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))]
        if img_uploads:
            try:
                _upload_files(driver, agent_cfg, img_uploads, agent_name)
            except Exception:
                pass

    def ask(prompt, expect=_web.SCENE_EXPECT):
        emit("waiting", {"stage": "design", "seconds": wait})
        got = _reask(driver, agent_cfg, prompt, expect=expect, wait=wait)
        return got[-1] if got else ""

    new_spec, notes = _web.refine_spec(
        spec, change, ask, check=_web.inspect,
        log=lambda m: ui.info(f"   {m}"), new_assets=listing,
        context=context)
    try:
        url = driver.current_url or design_url
    except Exception:                                    # noqa: BLE001
        url = design_url
    ui.ok("   " + "; ".join(notes))
    emit("stage_done", {"stage": "design", "count": 1, "texts": notes,
                        "url": url, "timed_out": False})

    emit("stage_start", {"stage": "media", "agent": "Prism Studio"})
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(_time.time())
    out = os.path.join(C.RUNS_DIR, f"reel_{stamp}.mp4")
    spec_path = os.path.join(C.RUNS_DIR, f"reel_{stamp}.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        _json.dump(new_spec, f, indent=2)
    ui.info(f"   🎬  re-filming {len(new_spec['scenes'])} scenes…")
    # Follow-ups must pass the same browser preflight as first renders.
    try:
        _web.render(new_spec, out, on_progress=on_progress, check=True)
    except Exception as e:
        ui.warn(f"   re-film preflight issue ({e}) — auto-healing accent and retrying…")
        new_spec = _web.ensure_accent_applied(new_spec)
        _web.render(new_spec, out, on_progress=on_progress, check=True)

    # Audio handling: check attachments or existing spec for voice/audio
    voice_files = []
    from . import footage as _footage
    for item in (attachments or []):
        p = item.get("path", "") if isinstance(item, dict) else str(item)
        if p and os.path.isfile(p) and p.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
            if _footage.is_valid_audio(p):
                voice_files.append(p)
            else:
                ui.warn(f"   ⚠️  ignoring unreadable audio {os.path.basename(p)} (header corrupt or non-audio)")
    if not voice_files and spec.get("_audio") and os.path.isfile(spec["_audio"]):
        if _footage.is_valid_audio(spec["_audio"]):
            voice_files.append(spec["_audio"])

    if voice_files:
        try:
            from . import footage as _footage
            ui.info("   🔊  adding voice-over / audio to the re-filmed video…")
            _footage.mix_audio(out, voice_files[-1])
            new_spec["_audio"] = voice_files[-1]
            with open(spec_path, "w", encoding="utf-8") as f:
                _json.dump(new_spec, f, indent=2)
            notes.append("audio mixed")
        except Exception as e:
            ui.warn(f"   could not mix audio into re-filmed reel ({e})")

    note = f"reel re-filmed — {os.path.basename(out)} ({'; '.join(notes)})"
    emit("stage_done", {"stage": "media", "count": 1, "texts": [note],
                        "url": out, "timed_out": False})
    return out, spec_path, note


def _web_token() -> str:
    from . import reel_web
    return reel_web.ASSET_TOKEN


def _brand_from_images(files: list) -> dict:
    """The client's accent colour, sampled from whichever entries in `files`
    are images. {} when none are, or none of them yield a usable palette.
    Shared by the design conversation's early look (before scenes are
    written) and _run_studio()'s own render-time fallback, so the same rule
    decides both -- see the note above where the design-stage call is made.
    """
    from . import reel as _pillow
    imgs = [str(a.get("path", "")) for a in (files or [])
            if str(a.get("path", "")).lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
    return (_pillow.sample_brand(imgs) or {}) if imgs else {}


def _run_studio(prior_text, attachments, cfg: dict, brand: dict | None = None,
                studio: dict | None = None):
    """Film the page the art-direction stage wrote.

    `studio` is the conversation the design came from — {"design_url",
    "agent"} — saved into the spec as `_studio` so Studio's Refine box can
    reopen that exact chat later. ReelDialog writes the same key for its own
    reels; without it here, every reel made by the router opened in Studio
    with Refine dead ("This older reel has no saved Studio conversation").

    The design is not trusted, it is measured: the page is laid out in the
    browser and every piece of text checked for being inside the frame and
    big enough to read, before a single frame is encoded. A design that fails
    is reported with the exact strings that are wrong.
    """
    try:
        from . import reel_web as web
    except Exception as e:
        return "", f"The web renderer isn't available ({e})."
    ok, why = web.available()
    if not ok:
        return "", why

    sources = [prior_text] if isinstance(prior_text, str) else list(prior_text)
    spec, why_bad = None, None
    for text in sources:
        try:
            spec = web.parse_spec(text)
            break
        except Exception as e:
            why_bad = why_bad or e
    if spec is None:
        # Keep what actually came back. Without this the one piece of evidence
        # that could explain the failure is discarded at the moment it becomes
        # interesting, and "No JSON found in the agent's reply" is unanswerable
        # — the customer can see JSON on the ChatGPT page and Prism cannot,
        # and there is no way to tell which of them is looking at the truth.
        kept = _keep_failed_spec(sources)
        return "", (f"{why_bad or 'Nothing was written for the renderer.'} "
                    "The art-direction stage has to return the design JSON."
                    + (f" What came back was saved to {kept}" if kept else ""))

    imgs = [a["path"] for a in (attachments or [])
            if a.get("path", "").lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
    # Whatever the design was actually shown: measured off an attachment, or
    # read off the client's website by the research stage. The design's own
    # 'brand' key is not trusted over either — it is the one thing it does not
    # get to choose.
    if brand:
        spec["brand"] = dict(brand)
    elif imgs and not spec.get("brand"):
        sampled = _brand_from_images(attachments or [])
        if sampled:
            spec["brand"] = sampled

    # The design pass may have inspected the attached references and marked a
    # file as limited/unusable. Keep that decision visible in the run log and
    # in the saved spec; never silently substitute an invented asset name.
    flags = spec.get("asset_flags")
    if isinstance(flags, list):
        clean_flags = []
        for flag in flags[:32]:
            if not isinstance(flag, dict):
                continue
            name = str(flag.get("asset", flag.get("name", ""))).strip()[:32]
            state = str(flag.get("status", "limited")).strip().lower()
            reason = str(flag.get("reason", "")).strip()[:240]
            if name and state in ("limited", "unusable"):
                clean_flags.append({"asset": name, "status": state,
                                    "reason": reason})
                ui.warn(f"   ⚑  {name} flagged {state}"
                        + (f" — {reason}" if reason else ""))
        if clean_flags:
            spec["asset_flags"] = clean_flags
        else:
            spec.pop("asset_flags", None)

    # Rebuilt, not passed along: collect() names assets from the files in a
    # fixed order, so the table the design stage was shown and the table the
    # renderer resolves are the same one without either holding a reference.
    if attachments:
        try:
            from . import assets as _assets
            made = {a["path"] for a in attachments if a.get("_generated")}
            spec["_assets"] = {
                k: {kk: vv for kk, vv in v.items() if kk != "ink"}
                for k, v in _assets.collect(attachments,
                                            generated=made).items()}
        except Exception as e:
            ui.warn(f"   couldn't prepare the artwork ({e})")

    import json as _json
    import time as _time
    from . import config as C
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(_time.time())
    out = os.path.join(C.RUNS_DIR, f"reel_{stamp}.mp4")
    if studio and studio.get("design_url"):
        spec["_studio"] = {"design_url": str(studio.get("design_url", "")),
                           "agent": str(studio.get("agent", ""))}
    _json.dump(spec, open(os.path.join(C.RUNS_DIR, f"reel_{stamp}.json"), "w"),
               indent=2)

    name = (spec.get("design") or {}).get("name", "")
    if name:
        ui.info(f"   🎨  design: {name}")

    # If audio voice-over was generated in an earlier stage, scale scenes to match it
    voice_files = [
        a.get("path", "") for a in (attachments or [])
        if isinstance(a, dict) and (
            a.get("kind") == "audio" or
            str(a.get("path", "")).lower().endswith(
                (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))
        ) and os.path.isfile(a.get("path", ""))
    ]
    if voice_files:
        try:
            from . import footage as _footage
            voice_dur = float(_footage.probe(voice_files[-1]).get("duration") or 0)
            if voice_dur > 0 and spec.get("scenes"):
                planned_secs = sum(float(sc.get("seconds", 4) or 4) for sc in spec["scenes"])
                if planned_secs > 0 and voice_dur > planned_secs:
                    scale = voice_dur / planned_secs
                    for sc in spec["scenes"]:
                        cur = float(sc.get("seconds", 4) or 4)
                        sc["seconds"] = round(cur * scale, 2)
                    ui.info(f"   ⏱️   scaled scenes to match {voice_dur:.1f}s voice-over")
        except Exception:
            pass

    secs = sum(float(sc.get("seconds", 4) or 4) for sc in spec["scenes"])
    ui.info(f"   🎬  filming {len(spec['scenes'])} scenes, ~{secs:.0f}s, "
            "1080x1920 — in a browser, locally")
    try:
        web.render(spec, out)
    except Exception as e:
        try:
            ui.warn(f"   render preflight issue ({e}) — auto-healing accent and retrying…")
            spec = web.ensure_accent_applied(spec)
            web.render(spec, out)
        except Exception as retry_err:
            return "", f"Render failed: {retry_err}"
    for fault in (spec.get("_faults") or [])[:5]:
        ui.warn(f"   layout: {fault}")
    return out, f"reel filmed — {os.path.basename(out)}"


def _run_motion(prior_text, attachments, cfg: dict, brand: dict | None = None):
    """Render the motion graphic the storyboard stage built.

    Same shape as _run_studio(): the spec was already fully assembled —
    every scene written, checked and corrected, one browser turn at a time
    — by core.motion.generate.build_spec() inside the storyboard stage's
    own handling (see the motion_feeder block further down this file).
    This just finds that JSON among the completed stages and renders it.
    Unlike _run_studio()'s web.parse_spec(), a plain json.loads() is
    correct here rather than a corner-cutting shortcut: build_spec()
    already did the messy-LLM-reply repair work upstream and its own
    output (json.dumps() of a validated spec) is clean by construction.
    """
    import json as _json
    try:
        from . import motion as _motion_pkg
    except Exception as e:
        return "", f"The motion renderer isn't available ({e})."
    ok, why = _motion_pkg.is_available()
    if not ok:
        return "", why

    sources = [prior_text] if isinstance(prior_text, str) else list(prior_text)
    spec, why_bad = None, None
    for text in sources:
        try:
            candidate = _json.loads(text)
        except Exception as e:
            why_bad = why_bad or e
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("scenes"), list):
            spec = candidate
            break
    if spec is None:
        kept = _keep_failed_spec(sources)
        return "", (f"{why_bad or 'Nothing was written for the renderer.'} "
                    "The storyboard stage has to return the assembled JSON."
                    + (f" What came back was saved to {kept}" if kept else ""))

    import time as _time
    from . import config as C
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(_time.time())
    out = os.path.join(C.RUNS_DIR, f"motion_{stamp}.mp4")
    _json.dump(spec, open(os.path.join(C.RUNS_DIR, f"motion_{stamp}.json"), "w"),
               indent=2)

    # If audio voice-over was generated in an earlier stage, scale motion project duration
    voice_files = [
        a.get("path", "") for a in (attachments or [])
        if isinstance(a, dict) and (
            a.get("kind") == "audio" or
            str(a.get("path", "")).lower().endswith(
                (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))
        ) and os.path.isfile(a.get("path", ""))
    ]
    if voice_files:
        try:
            from . import footage as _footage
            voice_dur = float(_footage.probe(voice_files[-1]).get("duration") or 0)
            if voice_dur > 0 and spec.get("scenes"):
                if "project" not in spec:
                    spec["project"] = {}
                spec["project"]["duration"] = voice_dur
                planned_secs = sum(float(sc.get("duration", 4) or 4) for sc in spec["scenes"])
                if planned_secs > 0 and voice_dur > planned_secs:
                    scale = voice_dur / planned_secs
                    for sc in spec["scenes"]:
                        cur = float(sc.get("duration", 4) or 4)
                        sc["duration"] = round(cur * scale, 2)
                    ui.info(f"   ⏱️   scaled motion scenes to match {voice_dur:.1f}s voice-over")
        except Exception:
            pass

    dur = float((spec.get("project") or {}).get("duration", 8.0) or 8.0)
    ui.info(f"   🎬  rendering {len(spec.get('scenes', []))} scenes, "
            f"~{dur:.0f}s, 1080x1920 — locally")
    try:
        _motion_pkg.render(spec, out)
    except Exception as e:
        return "", f"Render failed: {e}"
    return out, f"motion graphic rendered — {os.path.basename(out)}"


def _studio_conversation(stages, stage: str, all_links: dict) -> dict:
    """The chat a local renderer's spec came from: the nearest earlier stage
    that left a real URL behind. That is the conversation Studio's Refine
    box reopens, so it is stored with the reel (see _run_studio)."""
    names = [s[0] for s in stages]
    if stage not in names:
        return {}
    for earlier in reversed(names[:names.index(stage)]):
        url = str(all_links.get(earlier) or "")
        if url.startswith("http"):
            agent = next((s[1] for s in stages if s[0] == earlier), "")
            return {"design_url": url, "agent": agent}
    return {}


def _footage_script(prior_text) -> list[dict]:
    """Extract simple on-screen copy without imposing a template schema."""
    import json as _json

    def instruction_like(value: str) -> bool:
        low = " ".join(value.lower().split())
        return low.startswith((
            "i want to ", "i need to ", "we want to ", "we need to ",
            "please create ", "please make ", "create a reel ",
            "create an instagram ", "make a reel ", "make an instagram ",
        ))

    sources = [prior_text] if isinstance(prior_text, str) else list(prior_text)
    for source in sources:
        text = str(source or "")
        decoder = _json.JSONDecoder()
        for start, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[start:])
            except (TypeError, ValueError):
                continue
            scenes = candidate.get("scenes") if isinstance(candidate, dict) else None
            if not isinstance(scenes, list):
                continue
            captions = []
            for scene in scenes:
                if not isinstance(scene, dict):
                    continue
                value = (scene.get("caption") or scene.get("on_screen_text")
                         or scene.get("subtitles") or scene.get("headline")
                         or scene.get("text"))
                if not value and isinstance(scene.get("lines"), list):
                    value = " ".join(str(v) for v in scene["lines"] if v)
                if value and not instruction_like(str(value)):
                    item = {"text": str(value),
                            "seconds": scene.get("seconds", 3)}
                    for k in ("kicker", "badge", "tag"):
                        if scene.get(k):
                            item["kicker"] = str(scene[k])
                            break
                    for k in ("highlight", "focus", "accent"):
                        if scene.get(k):
                            item["highlight"] = str(scene[k])
                            break
                    for k in ("style", "theme", "archetype"):
                        if scene.get(k):
                            item["style"] = str(scene[k]).lower().strip()
                            break
                    for k in ("position", "pos", "placement"):
                        if scene.get(k):
                            item["position"] = str(scene[k]).lower().strip()
                            break
                    for k in ("sub", "subline", "subtitle", "secondary"):
                        if scene.get(k):
                            item["sub"] = str(scene[k]).strip()
                            break
                    if scene.get("transition"):
                        item["transition"] = str(scene["transition"])
                    captions.append(item)
            if captions:
                return captions

    # Prose and labelled script fallback: when the writer did not format as JSON
    import re as _re
    for source in sources:
        text = str(source or "")
        labelled = _re.findall(
            r"(?im)^\s*(?:(?:scene\s*\d+[\s:\.\)]*)?(?:caption|on[- ]screen text|subtitle|text)\s*:\s*)[\"']?([^\n\"']+)[\"']?\s*$",
            text)
        found = []
        for line in labelled:
            clean = " ".join(line.strip().strip('"\'').split())
            if clean and not instruction_like(clean) and len(clean) > 2 and not clean.lower().startswith(("scene ", "voice", "audio", "duration")):
                found.append({"text": clean, "seconds": 3})
        if found:
            return found

        numbered = _re.findall(r"(?m)^\s*(?:\d+[\.\)]|scene\s+\d+[:\.\)])\s*[\"']?([^\n\"']+)[\"']?\s*$", text)
        for line in numbered:
            clean = " ".join(line.strip().strip('"\'').split())
            if ":" in clean:
                sub_parts = clean.split(":", 1)
                if any(k in sub_parts[0].lower() for k in ("caption", "text", "screen")):
                    clean = sub_parts[1].strip().strip('"\'')
                elif any(k in sub_parts[0].lower() for k in ("voice", "audio", "vo")):
                    continue
            if clean and not instruction_like(clean) and len(clean) > 2 and not clean.lower().startswith(("scene ", "duration", "visual", "audio")):
                found.append({"text": clean, "seconds": 3})
        if found:
            return found
    return []


def _voiceover_text(prior_text) -> str:
    """The spoken words only — never the pipeline prompt, metadata, or scene JSON."""
    import json as _json
    import re as _re

    def _clean_spoken_line(raw: str) -> str:
        s = str(raw or "").strip()
        s = s.strip('"\'`*')
        # Remove bracketed stage directions like [pause], [music fades], (smiling), (laughs)
        s = _re.sub(r"\[[^\]]*\]|\([^\)]*\)", "", s).strip()
        return " ".join(s.split())

    sources = [prior_text] if isinstance(prior_text, str) else list(prior_text)

    # 1. JSON parsing: scenes list or top-level script/voiceover fields
    for source in sources:
        text = str(source or "")
        decoder = _json.JSONDecoder()
        for start, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[start:])
            except (TypeError, ValueError):
                continue
            if not isinstance(candidate, dict):
                continue

            # Check top-level voiceover / narration / script fields
            for key in ("voiceover", "voice_over", "narration", "spoken", "dialogue"):
                val = candidate.get(key)
                if isinstance(val, str) and val.strip():
                    cleaned = _clean_spoken_line(val)
                    if cleaned:
                        return (cleaned if cleaned.endswith((".", "!", "?")) else cleaned + ".")[:5000]
                elif isinstance(val, list) and val:
                    parts = [_clean_spoken_line(v) for v in val if _clean_spoken_line(v)]
                    if parts:
                        return " ".join(
                            p if p.endswith((".", "!", "?")) else p + "." for p in parts
                        )[:5000]

            scenes = candidate.get("scenes")
            if not isinstance(scenes, list):
                continue
            spoken = []
            for scene in scenes:
                if not isinstance(scene, dict):
                    continue
                explicit = (scene.get("voiceover") or scene.get("voice_over")
                            or scene.get("narration") or scene.get("spoken")
                            or scene.get("dialogue") or scene.get("audio"))
                if explicit:
                    parts = ([explicit] if isinstance(explicit, str)
                             else explicit if isinstance(explicit, list) else [])
                else:
                    parts = [scene.get("headline"), scene.get("support")]
                    if not any(parts) and isinstance(scene.get("lines"), list):
                        parts = scene["lines"]
                for part in parts:
                    clean = _clean_spoken_line(part)
                    if clean and clean not in spoken:
                        spoken.append(clean)
            if spoken:
                return " ".join(
                    value if value.endswith((".", "!", "?")) else value + "."
                    for value in spoken)[:5000]

    # 2. Labelled prose / lines (VO:, Voiceover:, Narrator:, Speaker:, Dialogue:, etc.)
    # Handles Markdown bold **Voiceover:**, brackets [VO]:, Scene prefixes, etc.
    label_pat = _re.compile(
        r"(?im)^\s*[*\[_]*(?:scene\s*\d+[\s:\.\)-]*)?(?:voice[ -]?over|vo|narration|narrator|speaker(?:\s*\d+)?|voice|dialogue|audio)[*\]_]*\s*[:\-]\s*[*_~]*\s*(.+)$"
    )
    marked = []
    for source in sources:
        text = str(source or "")
        for value in label_pat.findall(text):
            clean = _clean_spoken_line(value)
            if clean and clean not in marked:
                marked.append(clean)
    if marked:
        return " ".join(
            v if v.endswith((".", "!", "?")) else v + "." for v in marked
        )[:5000]

    # 3. Markdown script / narration section block (e.g. ## Script, ### Voice-over Script)
    sec_pat = _re.compile(
        r"(?is)(?:^|\n)\s*(?:#{1,6}|\*\*)\s*(?:voice[ -]?over(?:\s+script)?|narration|script)\s*(?:\*\*)?\s*[:\-]?\s*\n+(.*?)(?=\n\s*(?:#{1,6}|\*\*|$))"
    )
    for source in sources:
        text = str(source or "")
        m = sec_pat.search(text)
        if m:
            block = m.group(1).strip()
            lines = []
            for raw_line in block.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                low = line.lower()
                if any(low.startswith(p) for p in (
                    "visual:", "video:", "camera:", "sfx:", "music:", "scene ",
                    "shot:", "duration:", "screen:", "text on screen:", "on-screen:"
                )):
                    continue
                clean = _clean_spoken_line(line)
                if clean and clean not in lines:
                    lines.append(clean)
            if lines:
                return " ".join(
                    v if v.endswith((".", "!", "?")) else v + "." for v in lines
                )[:5000]

    # 4. If an input source is ALREADY clean plain spoken text without pipeline framing
    for source in sources:
        text = str(source or "").strip()
        if not text:
            continue
        low = text.lower()
        if any(marker in low for marker in (
            "this is a follow-up", "prism ·", "your only task is", "handoff",
            "the person now wants", "earlier result is included", "create one finished",
            "return the generated audio", "give the full updated result", "record this voice"
        )):
            continue
        if text.startswith("{") and text.endswith("}"):
            continue
        if _re.search(r"(?m)^\s*#{1,6}\s+", text):
            continue
        lines = []
        for l in text.split("\n"):
            clean = _clean_spoken_line(l)
            if clean and not clean.startswith(("#", "-", "*")):
                lines.append(clean)
        if lines:
            return " ".join(
                v if v.endswith((".", "!", "?")) else v + "." for v in lines
            )[:5000]

    return ""



def _run_elevenlabs(driver, agent_cfg: dict, narration: str,
                    should_stop=None) -> list[str]:
    """Drive ElevenLabs' speech form, which is not a chat composer."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    narration = " ".join(str(narration or "").split()).strip()
    if not narration:
        raise RuntimeError(
            "the writing step supplied no spoken voice-over lines, so Prism "
            "did not send pipeline instructions to ElevenLabs")

    box = WebDriverWait(driver, agent_cfg.get("input_wait", 20)).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, agent_cfg["textarea_selector"])))
    try:
        box.click()
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
    except Exception:
        pass
    if not _fast_type(driver, box, narration):
        box.send_keys(narration)
    # Notify React that the textarea content has changed so internal state updates
    try:
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', { bubbles: true }));"
            "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
            box)
    except Exception:
        pass

    if not _text_landed(_composer_text(driver, box), narration):
        raise RuntimeError("the narration would not go into ElevenLabs' text field")

    signature_js = """
        const audio = [...document.querySelectorAll('audio')]
          .map(a => a.currentSrc || a.src || '').filter(Boolean);
        const downloads = [...document.querySelectorAll('a[href], button')]
          .filter(e => /download/i.test((e.innerText || '') + ' ' +
                                       (e.getAttribute('aria-label') || '')))
          .map(e => (e.href || '') + '|' + (e.innerText || '').trim());
        return JSON.stringify({audio, downloads});
    """
    try:
        before = driver.execute_script(signature_js) or ""
    except Exception:
        before = ""

    find_button_js = """
        const preferred = ['generate speech', 'generate voice', 'convert to speech',
                           'synthesize', 'generate', 'create speech', 'generate audio'];
        // 1. Try testids or submit buttons
        const testBtn = document.querySelector(
            'button[data-testid*="generate"], [data-testid*="speech-synthesis-generate"], [data-testid*="generate-button"], button[type="submit"]');
        if (testBtn && !testBtn.disabled && !testBtn.getAttribute('aria-disabled')) {
            return testBtn;
        }
        // 2. Search all buttons and role="button" elements
        const candidates = [...document.querySelectorAll('button, [role="button"]')];
        for (const pref of preferred) {
            for (const btn of candidates) {
                const text = ((btn.innerText || '') + ' ' + (btn.getAttribute('aria-label') || '') + ' ' + (btn.getAttribute('data-testid') || '')).toLowerCase();
                if (text.includes(pref) && !btn.disabled && !btn.getAttribute('aria-disabled')) {
                    return btn;
                }
            }
        }
        return null;
    """

    generate = None
    deadline_btn = time.time() + 15
    while time.time() < deadline_btn:
        if should_stop and should_stop():
            return []
        try:
            candidate = driver.execute_script(find_button_js)
            if candidate is not None:
                generate = candidate
                break
        except Exception:
            pass
        try:
            candidates = driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
            for wanted in ("generate speech", "generate voice", "convert to speech",
                           "synthesize", "generate", "create speech", "generate audio"):
                for b in candidates:
                    try:
                        lbl = " ".join(((b.text or "") + " " +
                                        (b.get_attribute("aria-label") or "") + " " +
                                        (b.get_attribute("data-testid") or "")).lower().split())
                        if b.is_displayed() and b.is_enabled() and wanted in lbl:
                            generate = b
                            break
                    except Exception:
                        continue
                if generate is not None:
                    break
            if generate is not None:
                break
        except Exception:
            pass
        time.sleep(1)

    if generate is None:
        raise RuntimeError(
            "Prism could not find ElevenLabs' Generate speech button; its "
            "page layout may have changed")

    try:
        generate.click()
    except Exception:
        driver.execute_script("arguments[0].click();", generate)
    ui.info(f"   🔊  sent {len(narration.split())} spoken words only — "
            "waiting for the audio…")
    deadline = time.time() + max(45, int(agent_cfg.get("wait_time", 90)))
    while time.time() < deadline:
        if should_stop and should_stop():
            return []
        time.sleep(2)
        try:
            after = driver.execute_script(signature_js) or ""
            if after and after != before and ("audio" in after or "download" in after.lower()):
                return [f"Voice-over generated from {len(narration.split())} spoken words."]
        except Exception:
            continue
    raise RuntimeError(
        "ElevenLabs accepted the narration, but no new audio player or "
        "download appeared before the wait ended")


def _run_footage(prior_text, attachments, cfg: dict, on_progress=None):
    """Cut attached client clips, then add planned captions and voice-over."""
    try:
        from . import footage
        from . import config as C
    except Exception as e:
        return "", f"The client-footage editor isn't available ({e})."

    def _path_of(item):
        if isinstance(item, dict):
            return str(item.get("path", "") or "")
        return str(item or "")

    videos = [_path_of(a) for a in (attachments or [])
              if footage.is_video(_path_of(a))]
    audio = [_path_of(a) for a in (attachments or [])
             if str(_path_of(a)).lower().endswith(footage.AUDIO_SUFFIXES)
             and os.path.isfile(_path_of(a))]
    if not audio and cfg.get("audio_path") and os.path.isfile(cfg["audio_path"]):
        audio = [cfg["audio_path"]]
    captions = _footage_script(prior_text)
    if not captions:
        return "", ("The writing step did not return usable reel captions. "
                    "Prism refused to put the task instruction itself on "
                    "the client video; run the caption step again.")
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    out = os.path.join(C.RUNS_DIR, f"client_reel_{int(time.time())}.mp4")
    ui.info(f"   🎬  editing {len(videos)} client clip(s), "
            f"{len(captions)} caption(s)"
            + (" + voice-over" if audio else ""))
    try:
        footage.render(videos, out, captions=captions,
                       audio_path=audio[-1] if audio else "",
                       on_progress=on_progress)
    except Exception as e:
        return "", f"Client-footage edit failed: {e}"
    details = [f"{len(videos)} clips", f"{len(captions)} dynamic motion captions"]
    if len(videos) > 1:
        details.append(f"{len(videos) - 1} transitions")
    details.append("voice-over replaces camera audio" if audio else "silent picture edit")
    return out, f"client footage edited — {os.path.basename(out)} ({', '.join(details)})"


def _run_local(kind: str, prior_text, attachments, cfg: dict, stage: str,
               brand: dict | None = None, studio: dict | None = None,
               on_progress=None):
    """Execute an agent that lives in Prism rather than in a browser.

    prior_text is either a single string or the earlier stages' outputs,
    newest first — each is tried in turn so one prose-heavy stage can't hide
    the spec written by another.

    Returns (output_path, message). On failure the path is empty and the
    message explains what went wrong — a local stage must degrade the same
    way a scraped one does, never take the run down with it.
    """
    if kind == "reel_web" and any(
            str(a.get("path", "")).lower().endswith(
                (".mov", ".mp4", ".m4v", ".webm"))
            for a in (attachments or [])):
        return _run_footage(prior_text, attachments, cfg,
                            on_progress=on_progress)
    if kind == "reel_web":
        return _run_studio(prior_text, attachments, cfg, brand, studio=studio)
    if kind == "motion":
        return _run_motion(prior_text, attachments, cfg, brand)
    if kind != "reel":
        return "", f"Unknown local agent {kind!r}."
    try:
        from . import reel
    except Exception as e:
        return "", f"The reel renderer isn't available ({e})."
    try:
        reel.ffmpeg_path()
    except Exception as e:
        return "", str(e)

    sources = [prior_text] if isinstance(prior_text, str) else list(prior_text)
    spec, why = reel.first_spec(sources)
    if spec is None:
        # The writing stage produced prose instead of a scene spec. Say so
        # plainly — the fix is a routing one, not something to paper over —
        # and KEEP what came back, because it is the only thing that can
        # explain the failure and it disappears with this local variable.
        kept = reel.keep_unparsed(sources)
        if kept:
            ui.warn(f"   saved what came back to {kept}")
        return "", (f"{why or 'Nothing was written for the renderer.'} The "
                    "stage before this one has to return the JSON scene spec "
                    "for the renderer to draw."
                    + (f" What came back was saved to {kept}" if kept else ""))

    # Brand colour comes from the client's own artwork when they attached
    # any — measured, not described.
    imgs = [a["path"] for a in (attachments or [])
            if a.get("path", "").lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
    if imgs and not spec.get("brand"):
        brand = reel.sample_brand(imgs)
        if brand:
            spec["brand"] = brand

    import json as _json
    import time as _time
    from . import config as C
    os.makedirs(C.RUNS_DIR, exist_ok=True)
    stamp = int(_time.time())
    out = os.path.join(C.RUNS_DIR, f"reel_{stamp}.mp4")
    if studio and studio.get("design_url"):
        spec["_studio"] = {"design_url": str(studio.get("design_url", "")),
                           "agent": str(studio.get("agent", ""))}
    _json.dump(spec, open(os.path.join(C.RUNS_DIR, f"reel_{stamp}.json"), "w"),
               indent=2)
    secs = sum(float(sc.get("seconds", 4)) for sc in spec["scenes"])
    ui.info(f"   🎬  drawing {len(spec['scenes'])} scenes, {secs:.0f}s, 1080x1920 "
            "— locally, no browser")
    if spec.get("_dropped"):
        ui.warn(f"   skipped {len(spec['_dropped'])} scene(s) this renderer "
                f"can't draw: {', '.join(spec['_dropped'][:4])}")
    try:
        reel.render(spec, out)
    except Exception as e:
        return "", f"Render failed: {e}"
    return out, f"reel rendered — {os.path.basename(out)}"


def run(routing: dict, cfg: dict, attachments=None, on_event=None,
        query: str = "", chatgpt_analysis: bool = True,
        custom_stages: list[tuple[str, str, list[str]]] | None = None,
        should_stop=None, failover: bool = True,
        reel_design_stage: str = "", pipeline_files_out: list | None = None,
        motion_design_stage: str = "", resume_urls: dict | None = None,
        skip_signal=None, skip_stages: list | None = None,
        min_wait: int = 0, image_stages=None, fallback_signal=None,
        motion_skeleton: str = "", stage_skills: dict | None = None,
        followup: bool = False, prior_responses: dict | None = None):
    """Execute the pipeline. Returns (responses, links).

    stage_skills: {stage label: [skill key]} for a caller that built its own
                 stages -- an add-on naming the doctrine its own prompt was
                 written against. A routed run needs none: the planner puts
                 the keys in the plan and they are read from there.
    fallback_signal: a threading.Event the screen sets for "Use fallback" —
                 stop waiting for the tool on the stage that is running and
                 hand that stage to the next tool in its category NOW, the
                 way the failover pass would after the cap ran out. For the
                 person who can see the tool has errored and does not want
                 to sit out a 600-second wait for Prism to notice. Cleared by
                 the engine per press, like skip_signal.

    image_stages: stage keys the caller PROMISES will produce a picture —
                 /step-auto's "visual" stage, the STEP dialog's Draft. Such a
                 stage gets the full image budget (minutes, like Reel's
                 artwork) instead of the short look a generic visual turn
                 gets, because on those a picture is possible, not certain.

    attachments: list of records from core.files.attach() — uploaded to each
                 tool and their extracted text prepended to the first prompt.
    query: the user's original task — gives the file-analysis stage its focus.
    chatgpt_analysis: when attachments exist, prepend a ChatGPT stage that
                 analyses the files first (skipped if the pipeline already
                 starts with ChatGPT, or when the caller routes its own
                 analysis, e.g. /email).
    custom_stages: an explicit, pre-built (stage_label, agent_name, questions)
                 list, used instead of deriving stages from `routing`. Unlike
                 `routing` — one agent per fixed PIPELINE_ORDER category — this
                 can name any agent any number of times in any order (e.g. a
                 research pass, a structuring pass, back to the same agent
                 again). Stage labels only need to be unique WITHIN this list
                 (they key the responses/links dicts and drive the next-stage
                 handoff); they don't need to match a real category name.
    reel_design_stage: the label of a stage that art-directs a reel, when the
                 caller built the stages itself. That stage's reply is turn one
                 of a conversation — the look and the storyboard — and the
                 scenes are then asked for one at a time in the same tab. A
                 routed run finds this stage on its own; only a caller passing
                 `custom_stages` has to name it.
    motion_design_stage: the same idea as reel_design_stage, for core.motion
                 instead of core.reel_web — that stage's reply is turn one
                 (project, camera, storyboard rows), and core.motion.generate
                 asks for each scene's nodes one at a time in the same tab.
                 Motion has no routed-run auto-detection (it isn't in
                 core.agents' catalogue), so every caller names this stage
                 itself — there is no "a routed run finds this on its own"
                 case the way there is for reel_design_stage.
    motion_skeleton: optional core.motion prompt profile. ``cinematic_glass``
                 keeps one visual subject moving through the film and adds
                 continuity metadata; ``brand_launch`` retains the legacy
                 four-role treatment. If omitted, Motion uses cinematic_glass.
    on_event(kind, payload) is an optional callback for live UI updates.
    should_stop() is an optional predicate polled between and during stages.

    skip_signal is an optional threading.Event the GUI sets when the user
    presses "Skip this step": the CURRENT stage stops waiting, whatever the
    tool had produced is kept, and the run moves on to the next stage. The
    engine clears the event when it acts on it, so one press skips one step
    — a leftover press can never eat the stage after it. This is the way
    out of a tool stuck generating (an image that never finishes, a site
    whose selectors have moved) without throwing away the whole run.
                 A routed run drives a browser for minutes at a time, so a
                 caller with a Stop button needs a way to be let go of that
                 isn't killing the process. When it returns True the run
                 finishes the current poll, emits "cancelled", and RETURNS what
                 has already landed rather than raising — a cancelled run still
                 has real output in it, and throwing that away punishes the
                 user for stopping. The browser is deliberately left open, same
                 as every other exit from here.
    failover: after the pipeline, hand any stage that produced NOTHING to a
                 different tool from the same category and try once more. On by
                 default, because the failure it covers — a free tier running
                 out at the last stage of a forty-minute run — otherwise costs
                 the whole run. Set False for the retry itself, so a category
                 where every tool is having a bad afternoon cannot recurse.
    pipeline_files_out: a caller-owned list that images/files generated during
                 this run are appended into, on top of being used internally.
                 Exists so a failover retry — which runs this whole function
                 again from scratch for one stage via `custom_stages` — can get
                 the images that ONE stage generated back out, when otherwise
                 they would be harvested into this function's own local
                 `pipeline_files` and vanish with it when the call returns.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from . import files as F

    from . import lang as L
    from . import skills as SK

    attachments = attachments or []
    source_footage = [a for a in attachments
                      if str(a.get("path", "")).lower().endswith(
                          (".mov", ".mp4", ".m4v", ".webm"))]
    browser_attachments = [a for a in attachments if a not in source_footage]

    # "Reply in Gujarati", if the user asked for it. Resolved once per run
    # rather than per prompt: it is the same sentence every time, and reading
    # it here keeps the per-stage code to one `if`.
    answer_language = L.directive(cfg.get("output_language") or "")
    if answer_language:
        ui.info(f"   🌐  tools will answer in "
                f"{L.NAMES.get(cfg['output_language'], '')}")

    def emit(kind, payload):
        if on_event:
            on_event(kind, payload)

    agents = {k: v for k, v in (cfg.get("agents") or {}).items() if v}
    stages = list(custom_stages) if custom_stages is not None else list(_needed_stages(routing, agents))
    if not stages:
        ui.warn("Router marked every stage as not-needed — nothing to run.")
        return {}, {}

    # ChatGPT is Prism's dedicated file analyst: whenever attachments ride
    # along, it reads them FIRST and hands a precise brief to the pipeline —
    # its file/vision handling is the most reliable of the web tools. If this
    # stage fails, `prior` stays empty and the next stage gets the raw files
    # re-supplied, so the run degrades gracefully to the old behaviour.
    if browser_attachments and chatgpt_analysis and not followup and stages[0][1] != "ChatGPT":
        names = ", ".join(a["name"] for a in browser_attachments)
        goal = f" for this task: {query}" if query.strip() else " for the user's task"
        q = (f"Your ONLY task is: analyse the attached file(s) ({names}) thoroughly — "
             "their content, structure, key facts, numbers, data and style — and "
             "produce a short, precise brief of everything the next AI needs to "
             f"use these files{goal}. Do NOT perform the task itself. Do not "
             "reproduce the file's rows or text back; where a profile of a "
             "spreadsheet is given below, trust its counts over your own.")
        stages.insert(0, ("analysis", "ChatGPT", [q]))
        ui.info("📎  attachments present — ChatGPT will analyse the files first")

    # The script writer needs to know what each source shot actually shows,
    # but uploading hundreds of megabytes of client MOVs to every browser tool
    # is slow and unnecessary. Give it labelled opening/action frames per
    # planned clip instead. The full-resolution footage never leaves the local
    # editor; ElevenLabs is explicitly excluded from attachments below.
    early_studio = any(
        (A.resolve_agent("", agent) or {}).get("local") == "reel_web"
        for _stage, agent, _questions in stages)
    if source_footage and early_studio:
        try:
            from . import footage as _footage_preview
            sheet = _footage_preview.storyboard_sheet(
                [item.get("path", "") for item in source_footage])
            if sheet:
                preview = F.attach(sheet)
                preview["_generated"] = True
                preview["name"] = "client-footage-storyboard.jpg"
                browser_attachments.append(preview)
                ui.info(f"🎬  prepared a {len(source_footage)}-shot visual "
                        "storyboard for the caption writer")
        except Exception as e:
            ui.warn(f"   couldn't prepare the footage storyboard ({e})")

    # A local renderer draws whatever the stage before it hands over, so that
    # stage has to hand over a scene spec rather than prose. Rather than
    # making the router understand renderers, the requirement is appended to
    # the last writing stage here — the only place that knows both which
    # agent got picked for video AND what runs before it.
    # Prism Studio designs the reel instead of filling in a template, so it
    # needs TWO writing passes before it: one for the words, one for the art
    # direction. Splitting them is the point — a single reply that has to do
    # both produces a design that describes itself instead of one that exists.
    # Studio owns its artwork batch: it needs the files collected and named
    # before the art director begins. Leaving the router's ordinary `visual`
    # stage in as well opened the same image tool twice (once for `visual`,
    # again for `artwork`) and looked like Prism was endlessly retyping the
    # job. This is one deliverable, not two. Keep the owner's visual toggle:
    # it is still consulted below before the one Studio artwork stage is
    # inserted.
    has_studio = any((A.resolve_agent("", an) or {}).get("local") == "reel_web"
                     for _, an, _ in stages)
    if has_studio:
        stages = [item for item in stages if item[0] != "visual"]

    studio_at = next((i for i, (_, an, _) in enumerate(stages)
                      if (A.resolve_agent("", an) or {}).get("local") == "reel_web"),
                     None)
    footage_writer = None
    if studio_at is not None and source_footage:
        # Source clips change Studio's job from generation to editing. Keep
        # the planned content and audio stages (their captions and ElevenLabs
        # file feed the edit), but do not append Studio's HTML scene prompts
        # or insert generated artwork. The local renderer detects the footage
        # below and performs the cut instead.
        ui.info(f"🎞️   {len(source_footage)} source video(s) attached — "
                "Studio will edit them using the planned text and audio")
        writer = next((i for i in range(studio_at - 1, -1, -1)
                       if stages[i][0] in ("content", "brains")), None)
        if writer is not None and stages[writer][2]:
            st, agent, prompts = stages[writer]
            prompts = list(prompts)
            prompts[-1] += (
                "\n\nCLIENT-FOOTAGE EDIT SCRIPT & DIRECTION: Reply with a concise JSON "
                "object containing a scenes array. Write exactly "
                f"{len(source_footage)} scenes, matching the supplied CLIP storyboard.\n\n"
                "STUDIO COPYWRITING & VOICEOVER STANDARDS (NO AI SLOP):\n"
                "• MANDATORY USER VALUE PROPOSITIONS & HOOKS: Thoroughly scan the user's prompt for unique claims, "
                "regional/geographic firsts, company locations, brand names, and distinct product specs "
                "(e.g. 'first machine ever in Gujarat', 'Manish Grinding Tools Vadodara', 'Rougher X Finish End Mill'). "
                "You MUST feature these exact unique angles prominently in the script (especially Scene 1 and Scene 5) "
                "— never drop real client facts in favor of generic filler.\n"
                "• KILL CORPORATE ROBOTIC SLOP & REPETITION: Absolutely forbidden words and clichés: "
                "'cutting-edge', 'game-changer', 'elevate', 'seamless', 'revolutionize', 'unparalleled', "
                "'delve', 'testament', 'beacon', 'symphony', 'masterpiece', 'in today\'s fast-paced world', 'journey of'. "
                "Never write generic brochure copy. Furthermore, DO NOT ECHO the same words across consecutive scenes "
                "(e.g. don't repeat 'control' in two scenes; don't repeat 'rough and finish' in three scenes). "
                "Each scene must advance the narrative progressively: Hook/Flex ➔ Tension/Friction ➔ Precision/Technique ➔ Tool Reveal ➔ Brand/Location.\n"
                "• SPOKEN VOICEOVER (narration): Write for an authentic human voice actor speaking with "
                "emotion, tension, and conviction. Use conversational breathing pauses (..., —, and short rhythmic clauses) "
                "so ElevenLabs speaks naturally. Hook the listener in Scene 1 with the client's biggest flex or tension.\n"
                "• DYNAMIC ON-SCREEN CAPTIONS: High-impact kinetic punchlines (2 to 5 words max!), "
                "NOT verbatim subtitles of the voiceover. Use stacked 2-line contrasting typography: "
                "'caption' (primary focal line, bold uppercase) and 'sub' (secondary contrasting line in Title Case / mixed case, e.g. "
                "'Rougher X Finish', 'to mirror finish', 'Vadodara, Gujarat').\n"
                "• MANDATORY STYLE ROTATION & POSITIONING: Rotate styles across scenes so no two consecutive scenes "
                "use the same style! Cycle between 'creator' (punchy 3D yellow extrusion), 'editorial' (luxe documentary serif), "
                "and 'neon' (cyber scan glow with beam). Vary 'position' across 'upper', 'center', and 'lower' for visual dynamism.\n\n"
                "SCHEMA PER SCENE:\n"
                "{\n"
                '  "seconds": <float>,\n'
                '  "caption": "<punchy 2-5 word focal hook>",\n'
                '  "sub": "<contrasting line in Title/mixed case>",\n'
                '  "voiceover": "<natural spoken human line with pauses ...>",\n'
                '  "kicker": "<optional micro eyebrow label>",\n'
                '  "highlight": "<focal word to ignite with color>",\n'
                '  "style": "creator" | "editorial" | "neon",\n'
                '  "position": "upper" | "center" | "lower"\n'
                "}\n\n"
                "The attached labelled client-footage storyboard is authoritative: scene 1 describes "
                "CLIP 1, scene 2 CLIP 2, and so on. Never claim an action or result that its corresponding "
                "OPEN/ACTION frames do not visibly support.")
            stages[writer] = (st, agent, prompts)
            footage_writer = writer
        studio_at = None
    design_feeder = None
    script_stage = ""
    research_stage = ""
    studio_brand: dict = {}
    design_assets: dict = {}
    # Stages whose reply is READ BY A PROGRAM, not handed to another chat.
    # The pipeline's normal rules demand a prose "HANDOFF FOR <next agent>"
    # section as the last thing in the answer, which flatly contradicts "reply
    # with only a JSON object" — and a model that spots the contradiction
    # refuses outright rather than picking one. Each entry here replaces those
    # rules for that stage.
    machine_stages: dict[int, str] = {}
    if footage_writer is not None:
        machine_stages[footage_writer] = (
            "\n\nSTRICT PIPELINE RULES:\n"
            "Your answer is parsed by Prism and then passed to the audio "
            "step. Return only the requested JSON object. Do not add "
            "markdown fences, commentary, a handoff, or a follow-up question.")
    if studio_at is not None:
        # The stage that actually WRITES the reel — content's job by
        # PIPELINE_ORDER's own description ("copy, docs, scripts"), brains
        # the fallback this function's own warning below already promises.
        # Not "whichever non-local stage sits closest to studio": visual
        # routinely sits between content and media/studio, and asking visual
        # — an image-direction stage, not a script one — to also carry the
        # script instructions left the actual script (which content wrote
        # correctly) stuck in prose with nothing to turn it into JSON.
        writer = next((i for i in range(studio_at - 1, -1, -1)
                       if stages[i][0] in ("content", "brains")),
                      None)
        if writer is None:
            ui.warn("Prism Studio has no writing stage before it — turn on "
                    "content or brains so something can write the reel.")
        else:
            from . import reel_web as _web
            from . import reel as _pillow
            st, an, qs = stages[writer]
            stages[writer] = (st, an, qs[:-1] + [
                qs[-1] + "\n\n" + _web.script_instructions()])
            brand, asset_list = {}, ""
            client_pics: list[str] = []   # asset names for THEIR files
            if attachments:
                imgs = [a["path"] for a in attachments
                        if a.get("path", "").lower().endswith(
                            (".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
                if imgs:
                    brand = _pillow.sample_brand(imgs) or {}
                    if brand:
                        ui.info(f"🎨  brand colours read from the artwork — "
                                f"accent {brand.get('accent')}, "
                                f"deep {brand.get('deep')}")
                    # Cut the client's own marks out of what they sent, so the
                    # art director can place the REAL logo rather than ask a
                    # model to draw one that looks nearly like it.
                    try:
                        from . import assets as _assets
                        table = _assets.collect(attachments)
                        asset_list = _assets.manifest(table)
                        client_pics = list(table)
                        if table:
                            ui.info(f"✂️   {len(table)} asset(s) prepared from "
                                    "the artwork: " + ", ".join(table))
                    except Exception as e:
                        ui.warn(f"couldn't prepare the artwork ({e}) — the "
                                "reel will be type and colour only")
            # No logo attached means no measured palette, and a reel in a
            # colour the client does not own is not their reel. The research
            # stage is already on their website — asking for the hex codes
            # while it is there costs nothing, where having Prism open the
            # site again to sample it would cost a whole extra page load.
            if not brand:
                # `ragent`, not `an`: reusing the writer's name here rebound it
                # and the plan banner then credited the script to whichever
                # tool happened to do the research.
                for i, (st, ragent, qs) in enumerate(stages):
                    if st == "research" and i < studio_at:
                        stages[i] = (st, ragent,
                                     qs[:-1] + [qs[-1] + _web.research_addendum()])
                        research_stage = st
                        ui.info(f"🎨  {ragent} will read the brand colours off "
                                "their website")
                        break

            # Most jobs arrive with NOTHING attached — a company name and a
            # sentence. So the reel's pictures are made here: the tool that
            # can search the web and draw looks the company up and produces a
            # few images, which are harvested off the page like any other
            # generated asset. Skipped only if the user turned it off.
            maker = agents.get("visual") or "ChatGPT"
            # `skip_stages` is what the plan screen left out. This stage is
            # inserted here, not planned there, so unticking "Make the
            # images" removed a row and changed nothing — the run still
            # opened the image tool and made three pictures (2026-09-07).
            # A step the owner switched off stays off.
            left_out = "visual" in (skip_stages or ())
            if left_out:
                ui.info("🖼️   Make the images was left out of the plan — no "
                        "pictures will be generated; the reel is built from "
                        "type and colour"
                        + (" and the artwork attached" if client_pics else "")
                        + ". This is an asset choice, not a layout check.")
            if (not left_out and _reel_imagery_on(routing, cfg)
                    and A.resolve_agent("visual", maker)):
                stages.insert(studio_at, ("artwork", maker, [
                    _web.imagery_instructions(query, bool(client_pics),
                                              attached=client_pics)]))
                machine_stages[studio_at] = (
                    "\n\nSTRICT PIPELINE RULES:\n"
                    "The images themselves are collected from this page "
                    "automatically — they ARE the deliverable. Produce them, "
                    "then write one short line per image saying what it is. "
                    "Do NOT add a handoff section, a summary, an explanation "
                    "or a follow-up question; nothing but the images and "
                    "those lines is read.")
                studio_at += 1
                ui.info(f"🖼️   {maker} will search the web and make up to "
                        f"{_web.MAX_GENERATED} images for it")

            # The studio pipeline follows the order:
            # write up -> image -> artwork -> audio if needed -> design -> render
            audio_idx = next((i for i, (st, _, _) in enumerate(stages) if st == "audio"), None)
            if audio_idx is not None:
                audio_entry = stages.pop(audio_idx)
                if audio_idx < studio_at:
                    studio_at -= 1
                stages.insert(studio_at, audio_entry)
                studio_at += 1

            # The art director is the same tool as the writer unless a
            # stronger one is switched on: this pass is the harder of the two.
            director = agents.get("brains") or agents.get("content") or an
            # The asset list cannot be known yet — the imagery stage has not
            # run. A token stands in and is substituted the moment before this
            # prompt is typed.
            studio_brand = dict(brand or {})
            stages.insert(studio_at, ("design", director,
                                      [_web.design_instructions(
                                          brand or None, query,
                                          _web.ASSET_TOKEN)]))
            studio_at += 1
            design_feeder = studio_at - 1
            script_stage = stages[writer][0]
            json_only = (
                "\n\nSTRICT PIPELINE RULES:\n"
                "Your answer is parsed by a program, not read by a person. "
                "Obey the OUTPUT FORMAT block above exactly: the whole reply "
                "is one JSON object and nothing else. Do NOT add a handoff "
                "section, a summary, an explanation or a follow-up question. "
                "If any earlier instruction asked for one, it does not apply "
                "here — this is the only formatting rule that counts.")
            # Both the writer and the art director answer in JSON, so both are
            # exempt from the handoff rules, not just the one nearest the
            # renderer. Missing the writer is what made it pad its JSON with
            # commentary; missing the art director made it refuse outright.
            machine_stages[writer] = json_only
            machine_stages[design_feeder] = json_only
            ui.info(f"🎬  {stages[studio_at][1]} films the page — {an} writes "
                    f"the script, {director} art-directs it")

    # A caller that built its own stages (the CLI's /studio) still gets the
    # scene-at-a-time conversation — it names the art-direction stage rather
    # than having it inferred from a renderer that isn't in its list.
    if design_feeder is None and reel_design_stage:
        design_feeder = next((i for i, (st, _, _) in enumerate(stages)
                              if st == reel_design_stage), None)
        if design_feeder:
            script_stage = script_stage or stages[design_feeder - 1][0]

    # Same idea, for core.motion. No routed-run auto-detection exists for
    # it (see motion_design_stage's docstring) — every caller names the
    # stage itself, unconditionally.
    motion_feeder = None
    if motion_design_stage:
        motion_feeder = next((i for i, (st, _, _) in enumerate(stages)
                              if st == motion_design_stage), None)

    local_reel_at = next((i for i, (_, an, _) in enumerate(stages)
                          if (A.resolve_agent("", an) or {}).get("local") == "reel"),
                         None)
    spec_feeder = None      # stage index that must answer in JSON, not prose
    if local_reel_at is not None:
        # Same fix as the Studio writer above, same reason: content is the
        # stage that writes the script (visual only ever describes imagery),
        # so search for content/brains specifically rather than accepting
        # whatever non-local stage happens to sit nearest to media.
        feeder = next((i for i in range(local_reel_at - 1, -1, -1)
                       if stages[i][0] in ("content", "brains")),
                      None)
        if feeder is None:
            ui.warn("Prism Reel has no writing stage before it — turn on content "
                    "or brains so something can write the script.")
        else:
            try:
                from . import reel as _reel
                st, an, qs = stages[feeder]
                stages[feeder] = (st, an, qs[:-1] + [
                    qs[-1] + "\n\n" + _reel.spec_instructions()])
                spec_feeder = feeder
                ui.info(f"🎬  {stages[local_reel_at][1]} renders locally — "
                        f"{an} will write the scene spec for it")
            except Exception:
                pass

    # A routed run that picked "Prism Motion" itself (typed on Home, not
    # opened from Motion's own dialog — that path names motion_design_stage
    # explicitly above and never reaches here, since it uses the real tool
    # name, not this display name).
    #
    # "Prism Motion" is a LOCAL renderer, same as "Prism Studio"/"Prism
    # Reel" — not a real chat tool. It can't hold the storyboard
    # conversation itself; the stage loop dispatches its slot straight to
    # _run_local() (a Python call, never a browser — see the "local agent
    # runs inside Prism" branch below), so rewriting THAT stage's own
    # prompt is a no-op nobody reads. This mirrors studio_at above instead:
    # a NEW stage is inserted on a real configured tool, which becomes
    # motion_feeder; the original "Prism Motion" stage is left in place to
    # do what it already does — get dispatched to _run_local("motion", …),
    # which searches the completed stages (including the new one) for the
    # assembled spec. Simpler than studio_at in one way: Motion doesn't
    # need a separate script-writing pass or an imagery stage first —
    # generate.storyboard_instructions() asks for the words and the plan
    # in one turn, and its "image" nodes reference the client's own
    # attachments by name rather than needing anything AI-generated.
    motion_at = next((i for i, (_, an, _) in enumerate(stages)
                      if (A.resolve_agent("", an) or {}).get("local") == "motion"),
                     None)
    if motion_at is not None and motion_feeder is None:
        planner = agents.get("brains") or agents.get("content")
        if not planner:
            ui.warn("Prism Motion needs a content or brains agent turned on "
                    "to plan the motion graphic.")
        else:
            from .motion import generate as _motion_gen
            from . import reel_web as _web_m
            # Motion makes its pictures the same way Studio does, and for
            # the same reason: most jobs arrive with a company name and a
            # sentence, nothing attached. This step used to exist only on
            # the Studio path, so a reel rendered by Motion could not
            # contain a generated picture at all however the plan was
            # written — the storyboard was told what assets existed, there
            # were none, and every scene came out as type and colour.
            # Inserted BEFORE the storyboard stage, so the images exist
            # when the scenes that place them are written.
            maker_m = agents.get("visual") or "ChatGPT"
            if ("visual" in (skip_stages or ())):
                ui.info("🖼️   Make the images was switched off — the motion "
                        "graphic is built from type and colour")
            elif _reel_imagery_on(routing, cfg) and A.resolve_agent("visual", maker_m):
                own = []
                try:
                    from . import assets as _assets_m
                    own = list(_assets_m.collect(attachments or []))
                except Exception:                          # noqa: BLE001
                    own = []
                stages.insert(motion_at, ("artwork", maker_m, [
                    _web_m.imagery_instructions(query, bool(own),
                                                attached=own)]))
                machine_stages[motion_at] = (
                    "\n\nThe images themselves are collected from this page "
                    "automatically — they ARE the deliverable. Produce them, "
                    "then write one short line per image saying what it is. "
                    "Nothing else is read.")
                motion_at += 1
                ui.info(f"🖼️   {maker_m} will make up to "
                        f"{_web_m.MAX_GENERATED} images for the motion graphic")
            stages.insert(motion_at, ("motion_plan", planner,
                                      [_motion_gen.storyboard_instructions(
                                          query,
                                          skeleton=motion_skeleton or
                                          "cinematic_glass")]))
            motion_at += 1
            motion_feeder = motion_at - 1
            machine_stages[motion_feeder] = (
                "\n\nSTRICT PIPELINE RULES:\n"
                "Your answer is parsed by a program, not read by a person. "
                "Obey the OUTPUT FORMAT block above exactly: the whole "
                "reply is one JSON object and nothing else. Do NOT add a "
                "handoff section, a summary, an explanation or a follow-up "
                "question. If any earlier instruction asked for one, it "
                "does not apply here — this is the only formatting rule "
                "that counts.")
            ui.info(f"🎬  {stages[motion_at][1]} renders locally — "
                    f"{planner} plans the motion graphic, one scene at a "
                    "time")

    # Before the browser, not after: launching Chrome is the single slowest
    # and most visible thing this function does, and a run cancelled while the
    # plan was still being assembled would otherwise still throw a window onto
    # the user's screen before noticing it had been called off.
    if should_stop and should_stop():
        if on_event:
            on_event("cancelled", {"stage": "", "done": 0})
        ui.warn("Stopped before anything ran.")
        return {}, {}

    # One artifact folder per run — a "Use again" of the same words, or a
    # follow-up, is a new run and gets a new folder — named by the job's
    # title, which the planner wrote (or the request's first words when it
    # did not). See config.begin_run.
    run_title = ((routing or {}).get("_title") or C.fallback_title(query))
    cur_dir = C.current_run_dir()
    if (followup or resume_urls) and cur_dir and os.path.isdir(cur_dir):
        C.continue_run(cur_dir, task=query, title=run_title)
    else:
        C.begin_run(query, title=run_title)
    driver, fresh = _get_driver(cfg)
    all_responses: dict[str, list[str]] = {
        k: list(v) if isinstance(v, list) else [str(v)]
        for k, v in (prior_responses or {}).items()
    }
    all_links: dict[str, str] = {}
    # images/docs/decks/code GENERATED by earlier stages. Caller-supplied when
    # given, and mutated in place (never rebound) from here down, so a caller
    # holding onto pipeline_files_out sees every harvest as it happens rather
    # than only whatever this function's own local name pointed to last.
    pipeline_files: list[dict] = (
        pipeline_files_out if pipeline_files_out is not None else [])
    # A freshly launched browser opens on one blank tab — reuse it for stage
    # one. A REUSED browser still has the previous run's result tabs open;
    # always open a new tab in that case so nothing gets navigated away.
    first_tab = fresh

    def stopped() -> bool:
        return bool(should_stop and should_stop())

    def skip_requested() -> bool:
        return bool(skip_signal is not None and skip_signal.is_set())

    def fallback_requested() -> bool:
        return bool(fallback_signal is not None and fallback_signal.is_set())

    def stage_halt() -> bool:
        # What the per-stage waits poll: a full Stop, a skip of the stage
        # that is waiting right now, or "use fallback" on it.
        return stopped() or skip_requested() or fallback_requested()

    # Stages that produced nothing, and why. Read by the failover pass after
    # the loop; see _retry_failed_stages().
    failures: dict[str, dict] = {}
    # Stages that DID produce something, but the cap ran out while the tool
    # was still writing — see the `timed_out` branch of _smart_wait's caller
    # below. Kept separate from `failures`: the answer is real and stays in
    # all_responses (never thrown away just for arriving at the deadline),
    # but "got something" and "got the finished answer" are not the same
    # claim, and this is what lets the run say so instead of quietly calling
    # a partial answer done.
    incomplete: dict[str, dict] = {}
    # Something worth telling the person about a step that otherwise went
    # fine — today only "Prism wrote this document itself from the tool's
    # text" (see _meet_contract). Carried on the stage_done event so the
    # card can say it, rather than living only in the log.
    stage_notes: dict[str, str] = {}

    # Local renderers (Prism Reel / Studio / Motion) held back because a
    # stage BEFORE them produced nothing and is queued for the failover
    # pass. Run after that pass — see the local branch in the loop below.
    deferred_locals: list[tuple[str, str, dict]] = []

    def _run_local_stage(stage: str, agent_name: str, agent_cfg: dict) -> None:
        """One local renderer stage, start to finish: a Python call inside
        Prism — no tab, no upload, no scrape. Consumes the earlier stages'
        text and files, produces a real file here, and reports like any
        other stage. Shared by the main loop and the deferred pass."""
        emit("stage_start", {"stage": stage, "agent": agent_name})
        ui.rule(f"{stage.upper()}  ·  {agent_name}",
                style=A.CATEGORIES.get(stage, {}).get("color", "pink"))
        # Newest stage first, each kept separate: the spec comes from the
        # stage right before this one, and merging every stage into one
        # blob only gives the parser more prose to trip over.
        prior_text = [t for ts in reversed(list(all_responses.values()))
                      for t in ts if t.strip()]
        # Images an earlier stage GENERATED count as artwork too — that is
        # the whole point of harvesting them. The client's own files come
        # first so their real mark wins the 'logo' slot over anything a
        # model drew.
        out, note = _run_local(agent_cfg["local"], prior_text,
                               (attachments or []) + pipeline_files,
                               cfg, stage, brand=studio_brand,
                               studio=_studio_conversation(
                                   stages, stage, all_links),
                               on_progress=lambda d, t: emit(
                                   "local_progress", {"stage": stage,
                                                      "done": d, "total": t}))
        if out:
            candidates = []
            for item in (pipeline_files or []):
                p = item.get("path", "") if isinstance(item, dict) else str(item)
                if ((isinstance(item, dict) and item.get("kind") == "audio") or
                        str(p).lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))):
                    if os.path.isfile(p):
                        candidates.append(p)
            for item in (attachments or []):
                p = item.get("path", "") if isinstance(item, dict) else str(item)
                if ((isinstance(item, dict) and item.get("kind") == "audio") or
                        str(p).lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))):
                    if os.path.isfile(p) and p not in candidates:
                        candidates.append(p)
            for sk in ("audio", "voice", "speech"):
                lp = str(all_links.get(sk, "") or "")
                if lp and os.path.isfile(lp) and lp.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
                    if lp not in candidates:
                        candidates.append(lp)
            voice_files = candidates
            # Studio/Reel/Motion render their picture locally. When an audio
            # stage ran before them, finish the deliverable here by muxing its
            # downloaded track into that MP4. Client-footage renders already
            # do this inside _run_footage because their duration planning uses
            # the voice track, so do not needlessly replace it a second time.
            if voice_files and "voice-over" not in note:
                try:
                    from . import footage as _footage
                    ui.info("   🔊  adding the generated voice-over to the video…")
                    _footage.mix_audio(out, voice_files[-1])
                    note += " · voice-over mixed"
                except Exception as e:
                    error = ("The video rendered, but Prism could not add the "
                             f"generated voice-over: {e}")
                    ui.err(error)
                    emit("stage_error", {"stage": stage, "error": error,
                                         "url": out})
                    return
            # Keep the internal reel_<timestamp> working name, but make
            # the customer-facing artifact describe the original request.
            try:
                from . import config as _config
                saved = _config.save_artifact(out, query, kind="reel",
                                             task=query)
                ui.info(f"   💾  saved to {saved}")
            except Exception:                           # noqa: BLE001
                pass       # rendering succeeded; copying is best-effort
            all_responses[stage] = [note]
            all_links[stage] = out
            delivered["video"] = delivered.get("video", 0) + 1
            ui.ok(note)
            ui.info(f"   📁  {out}")
            emit("stage_done", {"stage": stage, "count": 1, "texts": [note],
                                "url": out, "timed_out": False})
        else:
            ui.err(note)
            emit("stage_error", {"stage": stage, "error": note, "url": ""})

    def _hand_to_fallback(stage: str, agent_name: str, questions: list) -> None:
        """"Use fallback", pressed while `agent_name` was being waited on.

        The failover pass already knows how to give a stage to the next tool
        in its category; it just runs after the loop, once the cap has run
        out. This is the same pass, for this one stage, right now -- so the
        stages after it get its answer instead of running on thinner context.
        What the tool had on the page is deliberately not kept: the person
        pressed the button because they could see it was not going to answer.
        """
        fallback_signal.clear()
        ui.warn(f"   ↪  {stage}: handed to the fallback at your request — "
                f"not waiting for {agent_name}")
        try:
            all_links[stage] = driver.current_url
        except Exception:                                   # noqa: BLE001
            pass
        info = {"agent": agent_name, "questions": questions,
                "reason": f"You handed this step to the fallback instead of "
                          f"waiting for {agent_name}.",
                "exhausted": False}
        if not failover:
            # A nested retry run never fails over again (one level only, see
            # _retry_failed_stages). Report it and let the outer pass decide.
            failures[stage] = info
            emit("stage_error", {"stage": stage, "error": info["reason"],
                                 "url": all_links.get(stage, "")})
            return
        _retry_failed_stages(
            {stage: info}, cfg, all_responses, all_links,
            attachments=attachments, query=query, emit=emit,
            should_stop=should_stop, stages=None,
            pipeline_files=pipeline_files, brand=studio_brand,
            skip_signal=skip_signal, image_stages=image_stages,
            fallback_signal=fallback_signal)

    # What each step owes the person — see core/contract.py. Resolved HERE,
    # after every insertion above, so the stages the engine adds for itself
    # (analysis, artwork, design, motion_plan) are held to the same contract
    # as the ones the planner wrote, and so a plan that predates kinds still
    # has one for every step.
    from . import contract as _contract
    stage_kinds: dict[int, str] = {}
    _entries = []
    _read_by_program = {i for i in (spec_feeder, design_feeder, motion_feeder)
                        if i is not None} | set(machine_stages)
    for _i, (_st, _an, _qs) in enumerate(stages):
        _cfg = A.resolve_agent(_st, _an) or {}
        _planned = routing.get(_st)
        _k = _contract.for_stage(
            _st, _planned.get("kind", "") if isinstance(_planned, dict) else "",
            _cfg)
        # A step whose reply a program reads (the script feeding a renderer,
        # the design, the storyboard) never owes the person a file — asking
        # it for one would put a document request on top of "JSON only".
        # The artwork step is read by a program too, but its deliverable IS
        # the pictures, so it keeps its kind.
        if _i in _read_by_program and _st != "artwork":
            _k = "data"
        stage_kinds[_i] = _k
        _entries.append((_st, _k, _cfg))
    _file_at = _contract.file_step_index(_entries, query)
    if _file_at >= 0:
        stage_kinds[_file_at] = "file"
    owed = _contract.promised(
        [(_st, stage_kinds[_i]) for _i, (_st, _a, _q) in enumerate(stages)])
    delivered: dict[str, int] = {}

    def _open_agent_page(agent_name: str, agent_cfg: dict, stage: str) -> None:
        """Open a stage's agent page, relaunching once if Chrome loses its tab.

        A Chrome window may still be visible after its renderer has vanished;
        WebDriver then raises ``no such window`` on navigation.  Retrying here
        is safe because no upload or prompt has happened yet, and it prevents
        a transient browser failure from cancelling the whole client reel.
        """
        nonlocal driver, first_tab
        target = agent_cfg["url"]
        res_cand = (resume_urls or {}).get(stage)
        if res_cand:
            try:
                from urllib.parse import urlparse
                r_host = urlparse(res_cand).netloc.lower().split(":")[0]
                c_host = urlparse(agent_cfg.get("url", "")).netloc.lower().split(":")[0]
                r_domain = ".".join(r_host.split(".")[-2:]) if "." in r_host else r_host
                c_domain = ".".join(c_host.split(".")[-2:]) if "." in c_host else c_host
                if r_domain and c_domain and r_domain == c_domain:
                    target = res_cand
            except Exception:
                pass
        for attempt in range(2):
            try:
                _ensure_active_window(driver)
                if not first_tab:
                    _open_tab(driver, agent_name)
                first_tab = False
                _ensure_active_window(driver)
                try:
                    driver.get(target)
                except Exception as get_err:
                    err_str = str(get_err).lower()
                    is_socket_or_gone = _browser_is_gone(get_err)
                    if ("timeout" in err_str or "timed out" in err_str) and not is_socket_or_gone:
                        ui.warn(f"   Navigation to {target} timed out; stopping background requests and proceeding")
                        try:
                            driver.execute_script("window.stop();")
                        except Exception:
                            pass
                    elif is_socket_or_gone and _ensure_active_window(driver):
                        try:
                            driver.get(target)
                        except Exception as retry_err:
                            r_err_str = str(retry_err).lower()
                            if ("timeout" in r_err_str or "timed out" in r_err_str) and not _browser_is_gone(retry_err):
                                ui.warn(f"   Navigation to {target} timed out on retry; stopping background requests and proceeding")
                                try:
                                    driver.execute_script("window.stop();")
                                except Exception:
                                    pass
                            else:
                                raise retry_err
                    else:
                        raise get_err
                # ChatGPT redirects its bare home URL to the last chat the
                # browser happened to view.  A new Prism job must never type
                # into that unrelated conversation; only a saved follow-up
                # URL is allowed to resume one.  Click the product's own
                # New chat control after the redirect has settled.
                if (agent_name == "ChatGPT"
                        and not (resume_urls or {}).get(stage)):
                    try:
                        fresh_chat = bool(driver.execute_script("""
                            const controls = [...document.querySelectorAll(
                                "a, button, [role='button']")];
                            const newChat = controls.find(el => {
                                const label = ((el.getAttribute('aria-label') || '')
                                    + ' ' + (el.innerText || '')).trim();
                                return /^new chat$/i.test(label)
                                    || /^(start )?new chat/i.test(label);
                            });
                            if (!newChat || !newChat.getClientRects().length) return false;
                            newChat.click();
                            return true;
                        """))
                        if fresh_chat:
                            ui.info("   💬  started a fresh ChatGPT conversation")
                            time.sleep(1)
                    except Exception:
                        pass
                time.sleep(agent_cfg.get("page_wait", 4))
                if not _driver_has_live_tab(driver):
                    raise RuntimeError("browser has closed its controlled agent tab")
                return
            except Exception as error:
                last_error = error
                if attempt or not _browser_is_gone(error):
                    raise
                ui.warn(f"   Chrome lost its tab while opening {agent_name} — "
                        "relaunching it once before the prompt is sent")
                try:
                    if driver is not None:
                        driver.quit()
                except Exception:
                    pass
                _active_driver = None
                _release_profile()
                driver, first_tab = _get_driver(cfg)
        raise last_error  # pragma: no cover - the loop always returns or raises

    for stage_idx, (stage, agent_name, questions) in enumerate(stages):
        # Per stage, never carried: see the skill check below.
        skill_keys, skill_task_text = [], ""
        if stopped():
            ui.warn("Stopped at your request — keeping everything finished so far.")
            emit("cancelled", {"stage": stage, "done": len(all_responses)})
            break
        if skip_requested():
            # A skip pressed in the dying moments of the previous stage must
            # not eat this one.
            skip_signal.clear()
        if fallback_requested():
            fallback_signal.clear()          # same rule for "use fallback"

        if stage_idx == motion_feeder:
            # The story contract: the copy stage's script IS the story, so
            # the motion writer's first turn is rebuilt now, with the script
            # that did not exist when the stages were planned. Without this
            # the writer plans its own story (the Alphakore run, 10 Sep
            # 2026, invented a slogan per scene while the script sat unused
            # one stage earlier).
            rebuilt = motion_turn_one(query, motion_skeleton, all_responses)
            if rebuilt:
                questions = [rebuilt]

        agent_cfg = A.resolve_agent(stage, agent_name)
        if not agent_cfg:
            # This `continue` used to be invisible to anything but a terminal:
            # the GUI got no event, so the step simply never appeared and the
            # run looked like it had silently skipped part of the plan.
            ui.warn(f"No registry entry for {agent_name} — skipping {stage}.")
            emit("stage_skipped", {
                "stage": stage, "agent": agent_name,
                "reason": f"{agent_name} isn't in Prism's tool registry, so "
                          "this step was left out. Pick a different tool for "
                          "it in the plan."})
            continue

        # A browser stage with no prompt is the run that uploads the files,
        # asks nothing, and waits the whole cap for an answer to a question
        # nobody put — 322 seconds on the 2026-09-07 reel run, and the next
        # stage was doing the same when the owner pressed Stop.
        #
        # Only `custom_stages` can bring one here: _needed_stages() drops a
        # stage with no questions, but the GUI's plan editor hands its
        # ticked rows over as they are, and a row the router never planned
        # has no prompt behind it. The "never received the prompt" guard
        # further down cannot catch it either — it is set inside the
        # per-prompt loop, which has nothing to loop over. Skipped, not
        # failed: a failure is retried on another tool, and there is nothing
        # to send that tool. Local renderers are exempt — they read the
        # previous stage's output, not a prompt.
        if not agent_cfg.get("local") and not any(
                q and str(q).strip() for q in questions):
            reason = (f"{stage} has no prompt behind it, so there is nothing "
                      f"to ask {agent_name}. Open Prompt on that step and "
                      "write one, or leave the step out.")
            ui.warn(f"   {reason}")
            emit("stage_skipped", {"stage": stage, "agent": agent_name,
                                   "reason": reason})
            continue

        # A LOCAL agent runs inside Prism — no tab, no upload, no scrape. It
        # consumes the previous stages' text and files and produces a real
        # file here. If a stage before it produced NOTHING and failover is
        # about to retry that stage with another tool, rendering now would
        # build the video without the images it is waiting on and show the
        # customer "Make the video — FAILED" while "Make the images" is still
        # being retried underneath it. So it is held back and run after the
        # retry pass, with whatever that pass recovered. (Its card stays
        # queued meanwhile: no stage_start is emitted until it really runs.)
        if agent_cfg.get("local"):
            has_spec = any(
                ('"scenes"' in t or '"html"' in t)
                for ts in all_responses.values() for t in ts)
            feeder_st = stages[design_feeder][0] if design_feeder is not None else ""
            critical_missing = not has_spec and (feeder_st in failures or spec_feeder in failures)
            if failover and failures:
                if critical_missing:
                    deferred_locals.append((stage, agent_name, agent_cfg))
                    ui.warn(f"   ⏸  {stage}: holding until "
                            f"{', '.join(failures)} has been retried — it feeds "
                            "this step")
                    continue
            _run_local_stage(stage, agent_name, agent_cfg)
            continue

        emit("stage_start", {"stage": stage, "agent": agent_name})
        ui.rule(f"{stage.upper()}  ·  {agent_name}", style=A.CATEGORIES.get(stage, {}).get("color", "pink"))

        timed_out = False
        try:
            # A follow-up resumes its exact saved chat URL.  The helper also
            # recovers once if Chrome's visible window lost its controlled tab
            # before any prompt/upload could be duplicated.
            _open_agent_page(agent_name, agent_cfg, stage)

            # Only NON-EMPTY prior outputs count — a failed scrape must not
            # inject an empty "[STAGE]" block downstream.
            prior = [(s, t) for s, t in all_responses.items()
                     if t and any(x.strip() for x in t)]

            # The user's files go to EVERY stage, as files. The rule used to
            # be "the first stage analyses them and hands a brief forward;
            # later stages get the brief, not the file". That produced the
            # run the owner reported: the analysis stage happened to fail,
            # so Claude got the raw CSV and wrote the document; the next
            # time the analysis succeeded, Claude got a paragraph about the
            # CSV and no CSV, and the document was hollow. A brief is a
            # summary and summaries lose things; the writer that needs a
            # figure should be able to read it off the file. The prompt
            # never carries the data itself — see files.context_block(),
            # which points at the upload (or gives a spreadsheet's profile)
            # once the file has gone up.
            # "format" is /boq's custom-pipeline label; "artwork" is Prism
            # Studio's imagery stage. Both are producers for the second list
            # below — the files an EARLIER stage generated (a logo, a deck)
            # travel only to the stages that make the deliverable.
            producer = (stage in ("visual", "media", "development",
                                  "presentation", "format", "artwork", "design")
                        or stage_idx == design_feeder
                        or stage_idx == motion_feeder)
            # Source video is never sent to browser tools for a local edit.
            # The writing/audio stages need the brief and script, not hundreds
            # of megabytes of client footage; the local renderer receives the
            # originals through `attachments` in _run_local_stage above.
            include_attachment = (bool(browser_attachments)
                                  and agent_cfg.get("runner") != "elevenlabs")
            # Producers also receive files GENERATED by earlier stages
            # (e.g. the logo the visual stage just made) — those can't
            # travel in a text handoff at all. Audio files are kept for
            # local renderers and never uploaded to ChatGPT.
            gen_uploads = [
                f for f in (pipeline_files if producer else [])
                if not str(f.get("path", "")).lower().endswith(
                    (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))
            ]
            send_files = (browser_attachments if include_attachment else []) + gen_uploads
            went_up = 0
            # A chip left in the composer by an earlier run would go out
            # with this message -- see _clear_staged_attachments. Done on
            # every stage, files to send or not: a stale chip on a step
            # with no upload is still a stale chip.
            _clear_staged_attachments(driver, agent_cfg, agent_name)
            if send_files and agent_cfg.get("runner") != "elevenlabs" and agent_name != "ElevenLabs":
                went_up = _upload_files(driver, agent_cfg, send_files, agent_name)

            # Relay hand-off: forward ONLY the most recent stage's output.
            # Every agent is instructed (below) to fold the key findings of
            # everything before it into its own answer, so the latest output
            # already carries the whole chain — re-sending every older stage
            # would only bloat and slow down the prompt.
            # The person's own words first, before the attachments and before
            # the previous stage's handoff. Whatever else gets crowded out of
            # a long prompt, this must not be it.
            _kind = stage_kinds.get(stage_idx, "text")
            context = _intent_block(query)
            if _kind == "text" and any(w in (query or "").lower() for w in (
                    "image", "picture", "photo", "drawing", "visual", "artwork",
                    "video", "reel", "audio", "voice")):
                context += (
                    "NOTE ON MEDIA: The user's request asks for generated media "
                    "(images, audio, or video). However, media generation is "
                    "handled by dedicated later stages in this pipeline. For "
                    "THIS step, do NOT generate images or call image creation "
                    "tools — reply strictly with written text.\n\n"
                )
            if include_attachment:
                # Built per stage, not once: whether the files went UP to
                # this tool decides whether their text is pasted in as well.
                context += F.context_block(browser_attachments,
                                           uploaded=bool(went_up))
            if producer and gen_uploads:
                # Not always pictures any more — _harvest_files also lands
                # generated documents, decks, code and archives here, so the
                # wording has to fit whatever actually came through rather
                # than always saying "image".
                names = ", ".join(f["name"] for f in gen_uploads)
                kinds = {f.get("kind", "image") for f in gen_uploads}
                noun = "image file(s)" if kinds == {"image"} else "file(s)"
                context += (
                    f"An earlier pipeline stage GENERATED these {noun}, "
                    f"uploaded to this chat: {names}. Use them as assets in "
                    "what you produce — do not recreate them from scratch.\n\n"
                )
            # The art director is handed the script verbatim further down, so
            # the relay's "here is the previous stage" block adds nothing it
            # needs — and the stage immediately before it is the image maker,
            # whose text is chatter about the pictures. Forwarding that as the
            # brief is how a design ends up answering the wrong question.
            # Motion's storyboard stage is self-contained the same way and
            # gets the same treatment, whether or not it happens to have a
            # preceding stage.
            if stage_idx == design_feeder or stage_idx == motion_feeder:
                prior = []

            if prior:
                # Only the hand-off lines travel (Round 31). A tool with its
                # own filter block (Apollo) needs every line of it, so the
                # line cap is lifted for that one; the summary step is the
                # one reader that wants every earlier step, so it gets each
                # step's hand-off rather than the last step's alone.
                own_block = bool(agent_cfg.get("handoff_spec"))
                cap = None if own_block else _MAX_HANDOFF_LINES
                if stage == "summary" and len(prior) > 1:
                    blocks = []
                    for prev_stage, prev_texts in prior:
                        got = _handoff_of(prev_texts, lines=cap)
                        if got:
                            blocks.append(_context_header(agent_cfg, prev_stage)
                                          + got)
                    if blocks:
                        context += ("\n\n".join(blocks)
                                    + _context_footer(agent_cfg))
                else:
                    prev_stage, prev_texts = prior[-1]
                    prev_text = _handoff_of(prev_texts, lines=cap)
                    if prev_text:
                        context += (_context_header(agent_cfg, prev_stage)
                                    + prev_text
                                    + _context_footer(agent_cfg))

            # The stage feeding a LOCAL renderer is machine-read, so it gets
            # the final-stage rules even though a stage follows it. The normal
            # handoff rules below demand a prose "HANDOFF FOR …" section as the
            # LAST thing in the answer — flatly contradicting "reply with only
            # a JSON object", and a model resolving that contradiction writes
            # the handoff and drops the spec. That is exactly how a run ends up
            # with nothing to render.
            # Is this stage's answer read by a person, or parsed by something?
            # It decides whether the user's "write back in Gujarati" setting
            # applies further down: a JSON scene spec or an Apollo filter block
            # translated into another language parses as nothing at all.
            machine_shaped = (
                stage_idx in machine_stages
                or stage_idx == spec_feeder
                or (stage_idx + 1 < len(stages)
                    and A.AGENT_REGISTRY.get(stages[stage_idx + 1][1], {})
                         .get("handoff_spec")))

            if stage_idx in machine_stages:
                handoff = machine_stages[stage_idx]
            elif stage_idx == spec_feeder:
                handoff = (
                    "\n\nSTRICT PIPELINE RULES:\n"
                    "Your answer is consumed by a renderer, not by another "
                    "chat. Obey the OUTPUT FORMAT block above exactly: the "
                    "whole reply is one JSON object and nothing else. Do NOT "
                    "add a handoff section, a summary, an explanation or a "
                    "follow-up question — any of those and nothing can be "
                    "rendered."
                )
            elif machine_shaped and stage_idx + 1 < len(stages):
                # The next tool is not a chat box — it is a search screen with
                # its own idea of what an input looks like (Apollo's fields cap
                # at 200 characters and ignore prose). Such a tool ships the
                # exact handoff it can parse, and it replaces the generic prose
                # rules below wholesale rather than being appended to them:
                # asking for a prose summary AND a filter block gets the prose.
                handoff = A.AGENT_REGISTRY[stages[stage_idx + 1][1]]["handoff_spec"]
            elif stage_idx + 1 < len(stages):
                nxt_stage, nxt_agent, _ = stages[stage_idx + 1]
                # Said the way a senior colleague briefs another: what this
                # answer is for, who reads it next, and the one thing that
                # has to be there. The old numbered "STRICT PIPELINE RULES"
                # read as a rule sheet, and a rule sheet is what a chat model
                # answers thinly or not at all.
                # One short paragraph, not a numbered rule sheet. It was
                # four rules — "do only the task", "read the context",
                # "end with HANDOFF FOR X", "your reader is another AI" —
                # 753 characters of procedure wrapped around a task that
                # was often 200. On an image step it demanded a written
                # handoff from a step whose whole job is to return a
                # picture, and that is what came back instead of the
                # picture. Everything it still needs to say fits here.
                handoff = (
                    _natural_handoff(nxt_agent, final=False)
                    if _is_natural(agent_cfg) else
                    "\n\nAt the end add a section headed "
                    f"'HANDOFF FOR {nxt_agent.upper()}': at most "
                    f"{_MAX_HANDOFF_LINES} lines with the facts and decisions "
                    f"{nxt_agent} needs for the '{nxt_stage}' step — only "
                    "those lines go forward. No questions back.")
            else:
                handoff = _natural_handoff("", final=True) if _is_natural(agent_cfg) else _FINAL_CLOSING
            if _is_maker(agent_cfg) and stage_idx not in machine_stages \
                    and stage_idx != spec_feeder:
                # A maker is never asked for a handoff section: what it
                # builds is what the next stage gets (the harvest), and the
                # STRICT PIPELINE RULES above read to it as "answer in text".
                handoff = _maker_handoff()

            # ONE line saying what to hand back, written for this step's kind
            # and this tool — see core/contract.py. It goes FIRST, ahead of
            # the pipeline's own wording, because it is the thing the person
            # actually wants; and it is the only such line in the message,
            # which is the point. A machine-read stage already has its format
            # block and must not be given a second one.
            _kind = stage_kinds.get(stage_idx, "text")
            if _kind in ("file", "image", "video") and not machine_shaped:
                _line = _contract.deliverable_line(
                    _kind, agent_name, agent_cfg,
                    ext=_contract.wanted_ext(query))
                if _line:
                    if handoff == _FINAL_CLOSING:
                        handoff = _FINAL_CLOSING_AFTER_DELIVERABLE
                    handoff = "\n\n" + _line + handoff
            elif _kind == "text" and not machine_shaped:
                # This tool's own known drift, one line, and only where it
                # matters: a long answer that lands in a canvas or a side
                # panel is invisible to the scraper, which is how a reel
                # script written perfectly well by Claude became six scenes
                # with no words in them. Not said on a `file` step — there
                # the panel IS the deliverable.
                _drift = (agent_cfg.get("avoid") or "").strip()
                if "image" in agent_cfg.get("produces", ()):
                    image_guard = "Do not generate images or call image tools for this step — reply in written text only."
                    if image_guard.lower() not in _drift.lower():
                        _drift = f"{_drift} {image_guard}".strip() if _drift else image_guard
                if _drift:
                    handoff = "\n\n" + _drift + handoff

            # The user asked for answers in their own language. Appended last
            # so it is the final instruction the model reads, and skipped for
            # machine-shaped stages, where translating the output would break
            # whatever is about to parse it.
            if answer_language and not machine_shaped:
                handoff += "\n\n" + answer_language

            # (Removed the "answer in the chat, never in a document/artifact"
            # instruction: the engine now HARVESTS artifact/canvas files
            # (_harvest_files) and saves them to the task's Artifacts folder, so
            # a document IS captured and downloadable. Forcing chat-only was
            # actively stopping document stages — the BOQ especially — from
            # producing the very PDF the customer wants.)

            nb_files: list = []          # what NotebookLM downloaded itself
            # `got` is only ever counted inside the generic else-branch below
            # (the image-wait/artwork loop); every OTHER branch here — Apollo,
            # Canva, ElevenLabs, NotebookLM — skips straight past it, yet
            # `_make_editable(..., made_image=bool(got))` after this dispatch
            # reads it unconditionally. Defined here so every branch has it:
            # "cannot access local variable 'got'" on a NotebookLM voice-over
            # fallback, 17 Sep 2026.
            got: int = 0
            if agent_cfg.get("search_tool") == "apollo":
                # Deliberately does NOT get `context`. That blob opens with
                # "Context from the previous pipeline stage (RESEARCH) —" and
                # is thousands of characters long; handing it to Apollo is the
                # exact call that failed with "Value too long". What Apollo
                # needs out of the previous stage is the filter block, which
                # _run_apollo parses for itself — and the questions carry the
                # user's own wording as the fallback if that block is missing.
                stage_responses = _run_apollo(
                    driver, agent_cfg, stage,
                    _bmp_safe("\n".join(questions) + "\n" + context))
            elif agent_cfg.get("runner") == "canva":
                # Canva AI answers with an outline first and needs its
                # "Generate design" pressed before a deck exists -- see
                # _run_canva. Handed the same prompt a chat tool would get.
                canva_prompt = _bmp_safe(_maker_brief(agent_name, agent_cfg)
                                         + context + "\n\n".join(questions)
                                         + handoff)
                stage_responses = _run_canva(driver, agent_cfg, stage,
                                             canva_prompt, should_stop=stage_halt)
            elif agent_cfg.get("runner") == "elevenlabs":
                # ElevenLabs is a speech form, not a chat agent. It receives
                # only words that should be spoken — no task brief, attached
                # artwork, JSON, handoff rules or "produce an MP3" wrapper.
                voice_sources = [
                    text for _prior_stage, texts in reversed(prior)
                    for text in texts if str(text).strip()]
                narration = _voiceover_text(voice_sources)
                if not narration and all_responses:
                    narration = _voiceover_text([
                        t for ts in reversed(list(all_responses.values()))
                        for t in ts if str(t).strip()
                    ])
                if not narration and questions:
                    narration = _voiceover_text(questions)
                ui.info(f"   → narration only: {len(narration.split())} words")
                stage_responses = _run_elevenlabs(
                    driver, agent_cfg, narration, should_stop=stage_halt)
            elif agent_name == "NotebookLM":
                # NotebookLM is not a chat box — it's a "sources" notebook.
                # It is handed the earlier steps' FULL answers and the
                # readable attachments as sources (a source tool wants the
                # material, not the four-line hand-off), and the step's
                # line plus the person's words as the overview's focus.
                # See _run_notebooklm and the notes above it.
                nb_sources, nb_unreadable = _nb_sources(prior, send_files)
                stage_responses, nb_files = _run_notebooklm(
                    driver, agent_cfg, stage, _bmp_safe("\n\n".join(questions)),
                    sources=nb_sources, unreadable=nb_unreadable, query=query,
                    should_stop=stage_halt)
            else:
                if stage_idx == design_feeder:
                    # Two things this prompt could not know when the plan was
                    # made. The assets, because the imagery stage had not run
                    # yet — and the SCRIPT, because the relay only forwards
                    # the stage immediately before, which is now the image
                    # maker's chatter rather than the words of the reel.
                    from . import reel_web as _web
                    from . import assets as _assets
                    made = {f["path"] for f in pipeline_files}
                    table = {}
                    try:
                        table = _assets.collect(
                            (attachments or []) + pipeline_files,
                            generated=made)
                    except Exception as e:
                        ui.warn(f"   couldn't prepare the artwork ({e})")
                    design_assets = table
                    # manifest() answers for the empty case too, and must: a
                    # design stage told nothing about pictures designs around
                    # the ones it assumes are there. See assets.NO_ARTWORK.
                    listing = _assets.manifest(table)
                    # What the imagery stage saw in the client's own pictures.
                    # Without it the art director knows a file's size and
                    # nothing else, and places it as decoration — which is all
                    # it knows the picture is.
                    said = _web.read_pictures(all_responses.get("artwork") or [])
                    if said:
                        listing = _web.describe_pictures(listing, said)
                        for name, line in said.items():
                            ui.info(f"   👁   asset:{name} — {line[:70]}")
                    if table:
                        ui.info(f"   🖼️   {len(table)} asset(s) for the design: "
                                + ", ".join(table))
                    else:
                        ui.warn("   🎨  no artwork — the design stage is being "
                                "told to build this from type and colour alone")
                    if not studio_brand and research_stage:
                        studio_brand = _web.read_brand(
                            all_responses.get(research_stage) or [])
                        if studio_brand:
                            ui.ok("   🎨  brand colours from their website — "
                                  + ", ".join(f"{k} {v}" for k, v
                                              in studio_brand.items()))
                        else:
                            ui.info("   no brand colours came back — the "
                                    "design will choose its own")
                    if not studio_brand:
                        # The reason a reel could pass every per-scene brand
                        # check and still fail brand_faults() at render: the
                        # colour Prism actually films in comes from
                        # _run_studio()'s OWN fallback sample of
                        # attachments + pipeline_files — which, on an artwork
                        # stage ordered before design, includes pictures that
                        # do not exist until now. Sampled here too, from the
                        # SAME set (_brand_from_images), so the design
                        # conversation is told the real colour and the
                        # per-scene check (build_spec's brand argument, just
                        # below) can actually catch a scene that ignores it
                        # — instead of the colour being discovered for the
                        # first time after every scene is already written
                        # and filmed (the owner's run of 15 Sep 2026: accent
                        # #4ab50a, sampled from artwork the design
                        # conversation never saw).
                        studio_brand = _brand_from_images(
                            (attachments or []) + pipeline_files)
                        if studio_brand:
                            ui.info(f"   🎨  brand colours read from the "
                                    f"artwork — accent "
                                    f"{studio_brand.get('accent')}, deep "
                                    f"{studio_brand.get('deep')}")
                    questions = [q.replace(_web.BRAND_TOKEN,
                                           _web.brand_block(studio_brand))
                                 for q in questions]

                    script = "\n\n".join(
                        t for t in (all_responses.get(script_stage) or [])
                        if t.strip())
                    head = ((
                        "For context: you are one stage of Prism, a desktop "
                        "automation the client's own team launched and is "
                        "watching. The script below is the genuine output of "
                        "this pipeline's writing stage, relayed as it was "
                        "written.\n\n"
                        f"THE SCRIPT — final, use these words, this order "
                        f"and these timings:\n\n{script}\n\n")
                        if script else "")
                    questions = [head + q.replace(_web_token(), listing)
                                 for q in questions]

                # A tool's own capabilities are described to the ROUTER, which
                # then decides what to ask for — a nudge, not an instruction,
                # and router.py explicitly tells the model to weigh field notes
                # above those descriptions. So a capability that only exists
                # because the user connected something (ChatGPT + the Canva
                # app) needs saying literally, in the prompt, every time that
                # agent runs that stage. Keyed by stage so ChatGPT's brains
                # turn is not told to go and make a design.
                suffix = _resolve_suffix(agent_cfg, stage, query)
                if suffix:
                    questions = [q + "\n\n" + suffix for q in questions]

                # House standards for this kind of deliverable — see
                # core/skills.py. Typed after the task, next to the suffix.
                # By the step's kind (core/contract.py): a text step gets the
                # whole skill; a step that builds its deliverable -- a maker,
                # the presentation stage, a file, image or video step -- gets
                # the standards without the text layout, which would tell it
                # to write the thing out; a data or links step, or any step a
                # program reads, gets nothing, because a second author of
                # format rules makes the model drop the JSON.
                skill_keys = (list((stage_skills or {}).get(stage) or [])
                              or SK.keys_for(routing, stage))
                _skill_kind = stage_kinds.get(stage_idx, "text")
                _skill_builds = (_builds_deliverable(agent_cfg, stage)
                                 or _skill_kind in ("file", "image", "video"))
                skill_text = (SK.block(skill_keys, "browser", maker=_skill_builds)
                              if skill_keys and not machine_shaped
                              and _skill_kind not in ("data", "links") else "")
                # What the tool was shown for this task WITHOUT the skill's
                # own text: a checker counts a figure as invented only when it
                # appears nowhere in here.
                skill_task_text = context + "\n".join(questions)
                if skill_text:
                    ui.info(f"   📘  skill: {', '.join(skill_keys)}")
                    questions = [q + "\n\n" + skill_text.rstrip() for q in questions]

                # Set when a prompt provably never reached the tool, so the
                # stage does not then sit out its full wait_time cap watching
                # a page nobody asked anything.
                nothing_was_sent = False
                for idx, prompt in enumerate(questions, 1):
                    prompt_phase = "constructing the prompt"
                    try:
                        ui.info(f"   → prompt {idx}/{len(questions)}: {prompt[:80]}…")

                        if stage == "audio" or agent_name == "ElevenLabs" or agent_cfg.get("runner") == "elevenlabs":
                            sources = [t for ts in reversed(list(all_responses.values())) for t in ts if str(t).strip()]
                            if not sources and prior:
                                sources = [text for _st, texts in reversed(prior) for text in texts if str(text).strip()]
                            if not sources and questions:
                                sources = questions
                            spoken = _voiceover_text(sources)
                            if not spoken and prompt:
                                spoken = _voiceover_text([prompt])
                            if spoken:
                                full_prompt = f"Record this voice-over narration aloud. Return the generated audio file:\n\n{spoken}"
                            else:
                                full_prompt = prompt
                        else:
                            full_prompt = ((context + prompt) if (idx == 1 and context) else prompt) + handoff
                        if idx == 1:
                            # A maker hears what it is for before anything
                            # else -- see _maker_brief.
                            # Voice-over / audio stages must receive ONLY the spoken lines / script,
                            # not task titles, pipeline briefs, or metadata.
                            if stage != "audio" and agent_name != "ElevenLabs" and agent_cfg.get("runner") != "elevenlabs":
                                full_prompt = _maker_brief(agent_name, agent_cfg) + full_prompt
                                full_prompt = _chat_header(run_title, stage) + full_prompt
                        full_prompt = _bmp_safe(full_prompt)  # strip emoji ChromeDriver can't type
                        prompt_phase = "waiting for the message box"
                        textarea = WebDriverWait(
                            driver, agent_cfg.get("input_wait", 15)).until(
                                EC.presence_of_element_located(
                                    (By.CSS_SELECTOR,
                                     agent_cfg["textarea_selector"])))
                        try:
                            textarea.clear()
                        except Exception:
                            pass
                        prompt_phase = "putting the prompt in the message box"
                        if not _fast_type(driver, textarea, full_prompt):
                            # JS insertion didn't take on this site — fall back
                            # to per-keystroke typing (slow but universal).
                            lines = full_prompt.split("\n")
                            for i, line in enumerate(lines):
                                if line:
                                    textarea.send_keys(line)
                                if i < len(lines) - 1:
                                    textarea.send_keys(Keys.SHIFT, Keys.ENTER)
                        time.sleep(1)

                        # The prompt has to BE there before it can be sent, and
                        # neither route above proved it was. Re-finding the
                        # element matters: uploading a file re-renders the
                        # composer, so the node found before the upload can be
                        # detached by now — typing into it succeeds and goes
                        # nowhere, which is exactly the run that reported
                        # "uploaded 1 file(s) → prompt 1/1" over a ChatGPT tab
                        # whose box was empty.
                        if not _text_landed(_composer_text(driver, textarea),
                                            full_prompt):
                            textarea = driver.find_element(
                                By.CSS_SELECTOR, agent_cfg["textarea_selector"])
                            _fast_type(driver, textarea, full_prompt)
                            time.sleep(1)
                        if not _text_landed(_composer_text(driver, textarea),
                                            full_prompt):
                            raise RuntimeError(
                                f"the prompt would not go into {agent_name}'s "
                                "message box — nothing was sent")

                        # The DOM having the text is not proof the page's OWN
                        # framework has: on a composer that only just became
                        # present (a fresh "New chat", or straight off
                        # navigation) the text can land — _text_landed reads
                        # raw innerText/value, which is all it CAN read — a
                        # beat before React finishes wiring the control up,
                        # so the send button stays inert to a click that
                        # raises nothing and an Enter the framework never
                        # sees. A short settle here, only once real text is
                        # confirmed in the box, costs nothing next to the
                        # minutes this stage already waits for a reply, and
                        # is cheaper than the run reporting the prompt never
                        # sent at all (see agents.py's page_wait comments).
                        time.sleep(1.5)

                        prompt_phase = "submitting the prompt"
                        # Submit — try the button, fall back to Enter.
                        # When attachments or rich content are present, the page may keep the
                        # send button disabled for several seconds while uploading/indexing.
                        # Poll and retry submit with a generous window so we don't abort prematurely.
                        sel = agent_cfg.get("submit_selector", "")
                        has_attachments = bool(attachments or send_files)
                        submit_deadline = time.time() + (35 if has_attachments else 12)
                        sent = False

                        selectors = [s.strip() for s in sel.split(",") if s.strip()] if sel else []
                        for fallback_sel in ("button[data-testid='send-button']", "button[aria-label*='Send']", "button[type='submit']"):
                            if fallback_sel not in selectors:
                                selectors.append(fallback_sel)

                        while time.time() < submit_deadline:
                            for s in selectors:
                                try:
                                    btns = driver.find_elements(By.CSS_SELECTOR, s)
                                    for btn in btns:
                                        is_disabled = driver.execute_script(
                                            "return arguments[0].disabled || arguments[0].getAttribute('aria-disabled') === 'true';",
                                            btn)
                                        if not is_disabled and btn.is_displayed():
                                            try:
                                                btn.click()
                                            except Exception:
                                                driver.execute_script("arguments[0].click();", btn)
                                            if _prompt_was_sent(driver, textarea, full_prompt, timeout=2):
                                                sent = True
                                                break
                                    if sent:
                                        break
                                except Exception:
                                    pass
                            if sent:
                                break
                            try:
                                textarea.send_keys(Keys.ENTER)
                                if _prompt_was_sent(driver, textarea, full_prompt, timeout=2):
                                    sent = True
                                    break
                            except Exception:
                                pass
                            time.sleep(1)

                        if not sent:
                            if not _prompt_was_sent(driver, textarea, full_prompt, timeout=6):
                                raise RuntimeError(
                                    f"{agent_name} would not accept the prompt "
                                    "— it is still sitting in the message box")
                        ui.info(f"   ✓  prompt {idx}/{len(questions)} sent")

                        if idx < len(questions):
                            # Let this answer finish before sending the next
                            # prompt. should_stop was missing here, unlike the
                            # identical call below for the stage's final wait
                            # — Stop went unpolled for up to 120s per gap in a
                            # multi-prompt stage, which is where "Stop takes
                            # minutes" was coming from.
                            _smart_wait(driver, agent_cfg, 120,
                                       should_stop=stage_halt)
                    except Exception as e:
                        # The blank native ChromeDriver failure happens before
                        # the first ChatGPT composer is usable. No prompt has
                        # been submitted at this point, so one fresh tab is
                        # safe and avoids making the person restart a whole
                        # pipeline that has already completed research.
                        if (idx == 1 and prompt_phase != "submitting the prompt"
                                and _blank_native_webdriver_error(e)):
                            ui.warn(
                                "   ChromeDriver returned a blank error while "
                                f"{prompt_phase}; reopening {agent_name} and "
                                "retrying the prompt once")
                            try:
                                _open_agent_page(agent_name, agent_cfg, stage)
                                _submit_prompt(
                                    driver, agent_cfg, agent_name, full_prompt,
                                    has_attachments=bool(attachments or send_files))
                                ui.info(f"   ✓  prompt {idx}/{len(questions)} "
                                        "sent after browser recovery")
                                if idx < len(questions):
                                    _smart_wait(driver, agent_cfg, 120,
                                                should_stop=stage_halt)
                                continue
                            except Exception as recovery_error:
                                e = recovery_error
                        if _browser_is_gone(e):
                            raise e
                        ui.err(f"   prompt error while {prompt_phase}: {e}")
                        nothing_was_sent = True

                if nothing_was_sent and not all_responses.get(stage):
                    # Waiting the full cap here is how a stage that sent
                    # nothing still cost five minutes and then reported "no
                    # response scraped" — which reads as the tool being slow
                    # rather than as Prism never having asked.
                    note = (f"{agent_name} never received the prompt, so there "
                            "is nothing to wait for. Its tab is open if you "
                            "want to send it by hand.")
                    ui.err(f"   {note}")
                    all_links[stage] = _safe_url(driver, exclude=tuple(
                        all_links.values()))
                    emit("stage_error", {"stage": stage, "error": note,
                                         "url": all_links.get(stage, "")})
                    # Same shape every other failure uses, so
                    # _retry_failed_stages can pick this stage up like any
                    # other — a prompt that never landed is exactly the kind
                    # worth retrying.
                    failures[stage] = {"agent": agent_name,
                                       "questions": questions,
                                       "reason": note, "exhausted": False}
                    continue

                # A promised image is not a text answer. ChatGPT commonly
                # writes its tiny acknowledgement immediately, then draws in
                # a side pane; `_smart_wait` cannot see that pane and used to
                # consume the entire agent timer before `_wait_for_images`
                # was even allowed to look. Start the image watcher as soon
                # as the prompt has landed. Once its finished-picture check
                # returns, the deliverable is ready — do not make the owner
                # wait for an unrelated prose timer.
                promised = set(image_stages or ())
                got = 0
                image_is_deliverable = stage == "artwork" or stage in promised
                if image_is_deliverable:
                    from . import reel_web as _rw
                    want, image_cap = ((_rw.MAX_GENERATED, 300)
                                       if stage == "artwork" else (1, 420))
                    ui.info(f"   ⏳  watching for the pictures to finish "
                            f"rendering (up to {image_cap}s)…")
                    got = _wait_for_images(driver, agent_cfg, want,
                                           cap=image_cap, grace=None,
                                           should_stop=stage_halt)
                    # Image agents commonly honour the first tool call but
                    # stop there even when their prose instruction asks for a
                    # set. Make the remaining artwork calls explicit, in the
                    # Image agents commonly honour the first tool call but
                    # stop there even when their prose instruction asks for a
                    # set. If we already have 4+ strong images, that is plenty
                    # for a 4-6 scene reel. Only re-ask if we have fewer than 4.
                    min_needed = min(4, want)
                    if stage == "artwork" and got >= min_needed:
                        ui.ok(f"   ✓  {got} artwork images generated — ready for reel design")
                    elif stage == "artwork" and got < min_needed:
                        js_img_count = r"""
                            function isGen(img) {
                                if (!img) return false;
                                const src = img.currentSrc || img.src || '';
                                if (!src) return false;
                                if (img.closest(
                                    '[data-message-author-role="user"], [data-message-role="user"], ' +
                                    '.user-message, form, [data-testid*="composer"], [data-testid*="attachment"], ' +
                                    '[data-testid*="user-message"], [data-testid*="user-turn"], [class*="user-turn"]'
                                )) return false;

                                // Immediate pass: OpenAI DALL-E CDN URLs are ALWAYS generated assistant images
                                const isOpenAiDalle = /oaidalleapiprodscus\.blob\.core\.windows\.net|files\.oaiusercontent\.com|chatgpt\.com\/backend-api\/files/i.test(src);
                                if (isOpenAiDalle) return true;

                                const turn = img.closest('article, section[data-testid^="conversation-turn-"], [data-testid^="conversation-turn-"], [class*="conversation-turn"]');
                                if (turn && turn.querySelector('[data-message-author-role="user"], [data-message-role="user"]')) {
                                    return false;
                                }
                                if (img.closest(
                                    'a[href^="http://"], a[href^="https://"]:not([href*="chatgpt.com"]):not([href*="openai.com"]), ' +
                                    '[data-testid*="source"], [data-testid*="citation"], [class*="citation"], ' +
                                    '[class*="source-card"], [class*="attribution"], [class*="search-result"], ' +
                                    '[aria-label*="source" i], [aria-label*="citation" i], details, [class*="searched"]'
                                )) return false;
                                if (img.closest(
                                    '[class*="avatar"], [data-testid*="avatar"], nav, header, footer, ' +
                                    'button[aria-label*="copy" i], button[aria-label*="thumb" i], button[aria-label*="feedback" i]'
                                )) return false;
                                const alt = (img.getAttribute('alt') || '').toLowerCase();
                                if (alt.includes('avatar') || alt === 'user' || alt === 'chatgpt') return false;
                                const rect = img.getBoundingClientRect();
                                if (rect.width > 0 && rect.width < 120 && rect.height > 0 && rect.height < 120) return false;
                                return true;
                            }
                            let n = 0;
                            const seenSrc = new Set();
                            for (const img of document.querySelectorAll('img')) {
                                if (!isGen(img)) continue;
                                const src = img.currentSrc || img.src || '';
                                if (!src || seenSrc.has(src)) continue;
                                if ((img.naturalWidth || 0) >= 256 && (img.naturalHeight || 0) >= 256) {
                                    seenSrc.add(src);
                                    n++;
                                }
                            }
                            return n;
                        """
                        asset_roles = {
                            1: "a brand emblem or clean wordmark",
                            2: "the flagship hero product or main equipment",
                            3: "the production process or craft in action",
                            4: "a close-up texture, material, or fine detail",
                            5: "a certification badge or quality seal",
                            6: "the final finished product or outcome",
                        }
                        while got < min_needed and not stage_halt():
                            # Ensure any previous generation is completely finished before typing
                            settle_deadline = time.time() + 45
                            while time.time() < settle_deadline and not stage_halt():
                                if not _chatgpt_is_busy(driver):
                                    break
                                time.sleep(2)

                            try:
                                before_dom = int(driver.execute_script(js_img_count) or 0)
                            except Exception:
                                before_dom = 0

                            number = got + 1
                            subject_desc = asset_roles.get(number, "a relevant product ingredient or detail")
                            _reask(
                                driver, agent_cfg,
                                f"Do NOT search the web or browse websites. "
                                f"Directly generate an image of {subject_desc} for this reel using DALL-E now. "
                                "Make it a single clear subject with an isolated transparent background and no text.",
                                wait=-1, should_stop=stage_halt)

                            # Wait for this new asset to finish rendering (max 75s, not 300s)
                            time.sleep(4)
                            wait_start = time.time()
                            asset_finished = False
                            per_asset_cap = 75
                            while time.time() - wait_start < per_asset_cap and not stage_halt():
                                if _sleep_interruptibly(3, stage_halt):
                                    break
                                busy = _chatgpt_is_busy(driver)
                                try:
                                    cur_dom = int(driver.execute_script(js_img_count) or 0)
                                except Exception:
                                    cur_dom = before_dom

                                if cur_dom > before_dom and not busy:
                                    asset_finished = True
                                    break
                                # If ChatGPT settled without drawing after an honest wait, move on
                                if not busy and (time.time() - wait_start) > 20 and cur_dom <= before_dom:
                                    break

                            if asset_finished:
                                got += 1
                                ui.info(f"   🖼️   artwork asset {got} of {want} completed")
                            else:
                                ui.warn(f"   ⚠️  could not generate artwork asset {number} — proceeding with {got} image(s)")
                                break

                # `min_wait` raises the ceiling for a run that redoes whole
                # deliverables — a follow-up — where the tool's everyday
                # budget cut the answer off mid-way. A ceiling only: the
                # wait ends the moment the answer settles.
                wait = max(int(agent_cfg.get("wait_time", 60) or 60),
                           int(min_wait or 0))
                # A caller that knows what a finished answer looks like says
                # so (e.g. /email needs "SUBJECT:"), and a mid-answer pause
                # can no longer end the wait early.
                expect = (routing.get(stage) or {}).get("expect", "")
                if stage_idx == design_feeder:
                    expect = '"css"'
                elif stage_idx == motion_feeder:
                    expect = '"storyboard"'
                elif stage_idx == spec_feeder:
                    # A spec streams in over several seconds and pauses mid-way;
                    # without a marker a pause reads as "finished" and we scrape
                    # the preamble before the JSON has been written.
                    expect = '"scenes"'
                if got and image_is_deliverable:
                    took, settled = 0, True
                    ui.info("   ✓  finished image detected — continuing now")
                else:
                    ui.info(f"   ⏳  waiting up to {wait}s for {agent_name} to finish…")
                    emit("waiting", {"stage": stage, "seconds": wait})
                    took, settled = _smart_wait(driver, agent_cfg, wait,
                                                expect=expect, should_stop=stage_halt)
                if skip_requested() and not stopped():
                    # "Skip this step": keep whatever the tool had produced —
                    # exactly as a Stop would — and carry on with the NEXT
                    # stage instead of ending the run. This is the way past a
                    # tool stuck generating without losing everything else.
                    skip_signal.clear()
                    ui.warn("   ⤼  skipped at your request — keeping what "
                            "landed and moving to the next step")
                    try:
                        captured = _capture(driver, agent_cfg)
                        partial = [max(captured, key=len)] if captured else []
                    except Exception:
                        partial = []
                    all_links[stage] = driver.current_url
                    if partial:
                        all_responses[stage] = partial
                        emit("stage_done", {"stage": stage,
                                            "count": len(partial),
                                            "texts": partial,
                                            "url": driver.current_url,
                                            "timed_out": True})
                    emit("stage_skipped", {
                        "stage": stage, "agent": agent_name,
                        "reason": "You skipped this step. Whatever the tool "
                                  "had produced was kept; the run moved on "
                                  "to the next step."})
                    first_tab = False
                    continue
                if fallback_requested() and not stopped():
                    _hand_to_fallback(stage, agent_name, questions)
                    first_tab = False
                    continue
                if stopped():
                    # Scrape before leaving: the tool has been generating for
                    # however long the user waited before pressing Stop, and
                    # that partial answer is theirs. The link is kept too, so
                    # the tab they can still read is one click away.
                    ui.warn("Stopped at your request — keeping this step's "
                            "output so far.")
                    try:
                        captured = _capture(driver, agent_cfg)
                        partial = [max(captured, key=len)] if captured else []
                    except Exception:
                        partial = []
                    all_links[stage] = driver.current_url
                    if partial:
                        all_responses[stage] = partial
                    emit("stage_done", {"stage": stage, "count": len(partial),
                                        "texts": partial,
                                        "url": driver.current_url,
                                        "timed_out": True})
                    emit("cancelled", {"stage": stage, "done": len(all_responses)})
                    break
                if settled:
                    ui.info(f"   ✓  response settled after {took}s")
                else:
                    # The cap ran out, not the tool: it is still generating
                    # in its tab and will finish there. Whatever is on the
                    # page gets scraped anyway (a partial answer beats
                    # none), and the link below is the real deliverable.
                    timed_out = True
                    ui.warn(f"still generating after {took}s — scraping what "
                            f"is on the page and keeping the link")

                if (not got and not image_is_deliverable
                        and stage in ("visual", "media")):
                    # The images are the deliverable here, not the text, so
                    # this stage gets its own budget ON TOP of the agent's —
                    # long only where it needs to be, rather than making every
                    # ChatGPT stage in the pipeline wait like an image render.
                    from . import reel_web as _rw
                    if stage == "artwork":
                        # Reel's dedicated image-batch stage: a picture is
                        # always the point, so it gets the full budget.
                        want, cap, grace = _rw.MAX_GENERATED, 300, None
                    elif stage in promised:
                        # The caller said a picture IS the deliverable (a
                        # /step-auto drawing sheet). ChatGPT's image model
                        # takes one to three minutes and shows a blurred
                        # preview meanwhile; 60s with a 12s grace was giving
                        # up on it every time. Seven minutes is the cap, not
                        # the wait — the loop returns the moment the picture
                        # settles.
                        want, cap, grace = 1, 420, None
                    else:
                        # visual/media also cover plain non-image turns (a
                        # Studio design turn answers with a CSS/JSON spec,
                        # never a picture) — grace gives up fast when this
                        # turn was never going to render one, instead of
                        # stalling every text-only turn on these stages.
                        want, cap, grace = 1, 60, 12
                    ui.info(f"   ⏳  waiting for the pictures to finish "
                            f"rendering (up to {cap}s)…")
                    got = _wait_for_images(driver, agent_cfg, want,
                                           cap=cap, grace=grace,
                                           should_stop=stage_halt)
                    if got:
                        timed_out = False
                    elif stage == "artwork":
                        ui.warn("   no images appeared — the reel will be "
                                "type and colour only")

                if fallback_requested() and not stopped():
                    # Pressed while the pictures were being waited on.
                    _hand_to_fallback(stage, agent_name, questions)
                    first_tab = False
                    continue
                texts = _capture(driver, agent_cfg)
                if (not texts and not stage_halt()
                        and agent_cfg.get("regenerate_selector")
                        and _regenerate_once(driver, agent_cfg)):
                    # An empty turn is usually a hiccup. One regenerate, one
                    # more (shorter) wait, then the honest answer.
                    emit("retry", {"stage": stage,
                                   "reason": "empty answer — regenerated once"})
                    took2, settled2 = _smart_wait(
                        driver, agent_cfg, min(wait, 300), expect=expect,
                        should_stop=stage_halt)
                    took += took2
                    timed_out = timed_out and not settled2
                    texts = _capture(driver, agent_cfg)
                if not texts:
                    stage_responses = []
                elif len(questions) == 1:
                    # One prompt → one answer: the biggest surviving capture IS it.
                    stage_responses = [max(texts, key=len)]
                else:
                    stage_responses = texts[-len(questions):]

                # THE REST OF THE DESIGN CONVERSATION.
                #
                # What came back is turn one — the look and the storyboard.
                # Every scene is now asked for on its own turn, in this same
                # tab, and laid out in a real browser before the next one is
                # asked for. Two things this buys, and the first is the whole
                # reason for it:
                #
                #   · budget. Asked for a whole reel in one reply, a model
                #     spread a few thousand characters over seven scenes and
                #     each one came out at ~278 characters, which is a
                #     headline and a subhead. That is a slide, and no prompt
                #     about motion can fix it because there is nothing in it
                #     to move. A scene with a whole reply to itself gets 20x
                #     the room.
                #   · correction that lands. A fault used to come back as
                #     "scene 3's headline is off the frame" against a reply
                #     the model had long since moved past. Now it is simply
                #     "this one", while the scene is still the subject.
                if stage_idx == design_feeder and texts:
                    # A scene is several thousand characters of markup and
                    # CSS. The agent's ordinary budget is sized for an answer,
                    # not for that, and cutting one off mid-stylesheet costs
                    # the whole scene.
                    scene_wait = max(int(agent_cfg.get("wait_time", 60)), 180)

                    def _ask(prompt, expect=_web.SCENE_EXPECT):
                        got = _reask(driver, agent_cfg, prompt, expect=expect,
                                     wait=scene_wait)
                        return got[-1] if got else ""

                    script_txt = "\n".join(all_responses.get(script_stage) or [])
                    try:
                        first_reply = _design_turn_text(driver, agent_cfg, texts,
                                                        _web, _ask)
                        spec = _web.build_spec(
                            first_reply, _ask,
                            script=script_txt, assets=listing,
                            assets_table=design_assets,
                            check=_web.inspect, brand=studio_brand,
                            log=lambda m: ui.info(f"   {m}"),
                            should_stop=stage_halt,
                            on_scene=lambda i, n: emit(
                                "reel_scene", {"index": i, "total": n}))
                    except Exception as e:
                        # Turn one did not parse, so there is no design to
                        # build scenes against. Whatever came back is kept as
                        # it is: it may still be a whole single-reply design
                        # from a model that answered the old way, and the
                        # renderer will happily parse that.
                        ui.err(f"   {e}")
                        kept = _keep_failed_spec(texts)
                        if kept:
                            ui.info(f"   what came back was saved to {kept}")
                    else:
                        import json as _json
                        ui.ok(f"   {len(spec['scenes'])} scene(s) written and "
                              "laid out clean at 1080x1920")
                        # Told not to change a word is not the same as
                        # prevented. Flagged, not blocked: a design may
                        # legitimately split a headline across elements.
                        for line in _web.script_drift(spec, script_txt)[:4]:
                            ui.warn(f'   the design dropped or reworded: '
                                    f'"{line}"')
                        stage_responses = [_json.dumps(spec, ensure_ascii=False)]

                # Same idea as design_feeder just above, for core.motion:
                # turn one is the storyboard, every scene's nodes are then
                # asked for one at a time in the same tab. No script/assets
                # substitution here — core.motion.generate's prompts are
                # self-contained, unlike reel_web's ASSET_TOKEN/BRAND_TOKEN
                # placeholders — so there is nothing to fill in before this
                # runs, only after the reply comes back.
                if stage_idx == motion_feeder and texts:
                    from . import motion as _motion_pkg
                    from .motion import generate as _motion
                    from .motion import inspect as _motion_inspect
                    from . import assets as _assets

                    scene_wait = max(int(agent_cfg.get("wait_time", 60)), 180)

                    def _ask(prompt, expect=_motion.SCENE_EXPECT):
                        got = _reask(driver, agent_cfg, prompt, expect=expect,
                                     wait=scene_wait)
                        return got[-1] if got else ""

                    # Same extraction Studio's design_feeder uses — logo/
                    # brand marks cut out of whatever the user attached, so
                    # the model can place the REAL mark via an "image" node
                    # rather than approximate one out of shapes and text.
                    motion_assets_table = {}
                    try:
                        # The client's own files AND whatever earlier steps
                        # generated. Attachments only, until now — so the
                        # pictures an image step had just made and saved were
                        # never offered to the storyboard, and a Motion reel
                        # was type and colour however many images the run had
                        # produced.
                        _made = {f["path"] for f in pipeline_files}
                        motion_assets_table = _assets.collect(
                            (attachments or []) + pipeline_files,
                            generated=_made)
                        if motion_assets_table:
                            ui.info(f"   🖼️   {len(motion_assets_table)} "
                                    "asset(s) prepared from the artwork: "
                                    + ", ".join(motion_assets_table))
                    except Exception as e:
                        ui.warn(f"   couldn't prepare the artwork ({e})")
                    motion_assets_listing = _assets.manifest(motion_assets_table)

                    try:
                        spec = _motion.build_spec(
                            texts[-1], _ask,
                            assets=motion_assets_listing,
                            assets_table=motion_assets_table,
                            skeleton=motion_skeleton or "cinematic_glass",
                            check=_motion_inspect.inspect,
                            log=lambda m: ui.info(f"   {m}"),
                            should_stop=stage_halt,
                            on_scene=lambda i, n: emit(
                                "motion_scene", {"index": i, "total": n}))
                    except Exception as e:
                        # Turn one did not parse, so there is no storyboard
                        # to build scenes against. Kept as-is: it may still
                        # be a whole single-reply spec from a model that
                        # answered the old way, and the renderer will
                        # happily parse that.
                        ui.err(f"   {e}")
                        kept = _keep_failed_spec(texts)
                        if kept:
                            ui.info(f"   what came back was saved to {kept}")
                    else:
                        import json as _json
                        try:
                            validated = _motion_pkg.validate_motion_spec(spec)
                        except Exception as e:
                            ui.err(f"   storyboard assembled but did not "
                                   f"validate ({e})")
                        else:
                            ui.ok(f"   {len(validated['scenes'])} scene(s) "
                                  "written")
                            stage_responses = [_json.dumps(validated, ensure_ascii=False)]

                # This stage feeds a renderer, so "it answered" isn't enough —
                # it has to have answered in JSON. Prefer a capture that
                # actually parses (the spec is often shorter than the prose
                # around it), and if none does, ask once more in the same tab
                # before the run reaches the renderer with nothing to draw.
                #
                # `texts` can be empty here even though the tab is still very
                # much alive — a spec is long, and the base wait above is
                # sized for an ordinary reply, not one that is also rendering
                # a DALL-E image on the side. Gating this whole block on
                # `and texts` meant that exact case — nothing captured yet,
                # tool still typing — skipped the one retry that exists for
                # it and fell straight through to the renderer with nothing
                # to draw, instead of asking again in the tab that was right
                # there.
                if stage_idx == spec_feeder:
                    from . import reel as _reel
                    spec_texts = [t for t in texts if _reel.has_spec(t)] if texts else []
                    if spec_texts:
                        # LAST, not longest: the prompt we typed carries an
                        # example spec, so "biggest thing that parses" can be
                        # our own echo. Chat DOM order is chronological, so
                        # the newest capture is the reply.
                        stage_responses = [spec_texts[-1]]
                        # It parses — but does it render as something worth
                        # posting? The faults that matter (a series typed into
                        # a caption, an asterisk with no footnote, a scene
                        # describing a shot) are all legal JSON, so the only
                        # way to catch them is to look, and the only way to fix
                        # them is to say precisely what is wrong.
                        faults = _reel.lint_spec(
                            _reel.parse_spec(stage_responses[0]))
                        if faults:
                            ui.warn(f"the scene spec has {len(faults)} problem(s) "
                                    "— sending them back to be fixed")
                            for fault in faults[:6]:
                                ui.info(f"   · {fault}")
                            again = _reask(
                                driver, agent_cfg,
                                "Your JSON renders, but these are wrong:\n\n"
                                + "\n".join(f"{n}. {x}" for n, x
                                            in enumerate(faults[:10], 1))
                                + "\n\nSend the corrected scene spec: ONLY the "
                                  "JSON object, first character '{', last '}'.",
                                expect='"scenes"')
                            better = [t for t in again if _reel.has_spec(t)]
                            if better:
                                left = _reel.lint_spec(_reel.parse_spec(better[-1]))
                                # Keep the second attempt only if it is
                                # genuinely cleaner — a "fix" that trades six
                                # faults for seven is not a fix.
                                if len(left) < len(faults):
                                    stage_responses = [better[-1]]
                                    ui.ok(f"   fixed — {len(faults)} problem(s) "
                                          f"down to {len(left)}")
                                else:
                                    ui.info("   the second attempt was no "
                                            "better — keeping the first")
                    else:
                        if texts:
                            ui.warn(f"{agent_name} wrote about the reel instead "
                                    "of writing the spec — asking again for "
                                    "JSON only")
                        else:
                            ui.warn(f"{agent_name} was still writing when "
                                    "Prism stopped waiting — asking again for "
                                    "JSON only")
                        emit("retry", {"stage": stage, "reason": "no scene spec"})
                        again = _reask(
                            driver, agent_cfg,
                            "That reply cannot be rendered. Send the scene "
                            "spec itself now: reply with ONLY the JSON "
                            "object, wrapped in a ```json fenced code block "
                            "and nothing else — no preamble, no handoff. "
                            "Keep the fence: without it the chat window eats "
                            "the asterisks in your CSS and wraps long URLs "
                            "onto new lines, which is what broke the last "
                            "attempt.",
                            expect='"scenes"')
                        fixed = [t for t in again if _reel.has_spec(t)]
                        if fixed:
                            stage_responses = [fixed[-1]]
                            ui.ok("   got the scene spec on the second ask")
                        else:
                            ui.err("   still no scene spec — the renderer will "
                                   "have nothing to draw")

                    # Groq API fallback (browser-first, API-backstop). The
                    # browser is tried exactly as before; only if it STILL has
                    # no renderable spec here — the model refused the JSON
                    # contract, wrote prose, or was cut off — do we ask Groq for
                    # it. json_mode forces one JSON object and the call returns
                    # the moment it is done (a real completion signal, no 300s
                    # stability guess, no refusal), so a run reaches the renderer
                    # with something to draw instead of nothing. Browser output
                    # is always preferred; this never runs when it succeeded.
                    have_spec = bool(stage_responses) and _reel.has_spec(
                        stage_responses[-1])
                    if not have_spec and cfg.get("api_key"):
                        ui.info("   ⚡  no usable spec from the browser — asking "
                                "Groq (API) for the JSON")
                        emit("retry", {"stage": stage,
                                       "reason": "groq spec fallback"})
                        api_prompt = (
                            (context or "")
                            + "\n\n".join(questions)
                            + "\n\n" + _reel.spec_instructions()
                            + "\n\nReply with ONLY the JSON object — first "
                              "character '{', last '}'.")
                        try:
                            from . import router as _router
                            groq_got = _router.groq_chat(
                                cfg.get("api_key", ""), cfg.get("model", ""),
                                api_prompt, json_mode=True, timeout=90)
                            if _reel.has_spec(groq_got):
                                stage_responses = [groq_got]
                                ui.ok("   ✓  Groq produced a valid scene spec")
                            else:
                                ui.warn("   Groq's reply still wasn't a usable "
                                        "spec — nothing to render")
                        except Exception as e:
                            ui.warn(f"   Groq spec fallback failed: {e}")
            # The artwork exists and is good. NOW make it editable — a second
            # prompt in the same chat rather than a different first prompt.
            stage_responses, canva_url = _make_editable(
                driver, agent_cfg, stage, query, stage_responses,
                machine_shaped=machine_shaped, made_image=bool(got))

            if stage_responses:
                ui.info(f"   📥  captured {sum(len(t) for t in stage_responses)} chars")

            # The Canva design, when there is one, is the deliverable a
            # customer actually opens and edits — worth more as the saved
            # link than the ChatGPT tab it was requested from.
            all_links[stage] = canva_url or driver.current_url
            all_responses[stage] = stage_responses

            # Image-making stages: pull the generated images off the page so
            # later stages can actually use them (text handoffs can't) —
            # and, regardless of whether a later stage exists, so the file
            # itself survives. It used to only be harvested when there was a
            # next stage to hand it to (or a failover retry's
            # pipeline_files_out asked explicitly), which meant a plain
            # "generate me an image" task — one stage, no retry — never had
            # its output downloaded at all: only the tool's own hosted link
            # remained, gone the moment that browser tab or session closed.
            made_here: list = []
            if stage in ("visual", "media", "artwork"):
                made = _harvest_images(
                    driver, agent_cfg, stage,
                    exclude_files=(attachments or []) + (pipeline_files or []))
                if stage == "artwork":
                    # A reel wants a few strong images, not a contact sheet —
                    # and every extra one is another asset the art director
                    # has to find a place for.
                    from . import reel_web as _rw
                    made = made[:_rw.MAX_GENERATED]
                if made:
                    # In place, never rebound — see pipeline_files_out above.
                    # Keep any earlier audio files so voice-over is NEVER evicted by images
                    audio_in_pipeline = [
                        f for f in pipeline_files
                        if f.get("kind") == "audio" or str(f.get("path", "")).lower().endswith(
                            (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))
                    ]
                    if stage == "artwork":
                        # Newly generated artworks from the dedicated artwork stage are
                        # the production deliverables for the reel; replace visual files
                        # but keep the voice-over audio file intact.
                        pipeline_files[:] = audio_in_pipeline + made[-_rw.MAX_GENERATED:]
                    else:
                        other_pipeline = [f for f in pipeline_files if f not in audio_in_pipeline]
                        pipeline_files[:] = audio_in_pipeline + (other_pipeline + made)[-6:]
                    ui.info(f"   🖼️   harvested {len(made)} generated image(s) "
                            "for the next stages")
                    _save_artifacts(made, query, stage, link=all_links.get(stage, ""))
                    made_here = made
            # Same idea for a document, deck, sheet or code file — and on
            # EVERY other stage, not just the six producer stages it used to
            # be limited to: a tool asked on a research or summary step for
            # "an Excel of this" makes one, and Prism used to walk past it.
            # The producer stages keep their patient wait; the rest get a
            # free look and wait only when something says a file is coming
            # (_harvest_stage_files). "write" is Gerber's custom-pipeline
            # label for the same reason "format" is /boq's; "audio"
            # (ElevenLabs/Suno) is best-effort, unverified against a live
            # account; see _harvest_files and _wait_for_files.
            else:
                made_files = _harvest_stage_files(driver, agent_cfg, stage,
                                                  stage_responses, attachments)
                if made_files:
                    audio_in_pipeline = [
                        f for f in pipeline_files
                        if f.get("kind") == "audio" or str(f.get("path", "")).lower().endswith(
                            (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"))
                    ]
                    other_pipeline = [f for f in pipeline_files if f not in audio_in_pipeline]
                    pipeline_files[:] = audio_in_pipeline + (other_pipeline + made_files)[-6:]
                    ui.info(f"   📎  harvested {len(made_files)} generated "
                            "file(s) for the next stages")
                    _save_artifacts(made_files, query, stage, link=all_links.get(stage, ""))
                    made_here = made_files
                    if stage in ("audio", "voice", "speech") or not all_links.get(stage):
                        all_links[stage] = made_files[0].get("path", "")

            if nb_files:
                # NotebookLM saved its own overview (see _nb_download); it
                # travels and is kept the same way a harvested file is.
                pipeline_files[:] = (pipeline_files + nb_files)[-6:]
                _save_artifacts(nb_files, query, stage, link=all_links.get(stage, ""))
                made_here = list(made_here) + nb_files

            # Did this step produce what it owed? A `file` step that came
            # back as chat text is asked once for the file, and if it still
            # answers in prose the text is written out as a document — see
            # _meet_contract and core/contract.py. This is what stops "make
            # me a DOCX" ending with nine thousand characters in a chat
            # window and nothing on the disk.
            kind_here = stage_kinds.get(stage_idx, "text")
            short_of = ""
            # Only a CHAT can be asked again. Canva, Gamma and the other
            # makers, Apollo's search screen and NotebookLM have a prompt
            # box but no conversation: "please give me that as a file"
            # typed there starts a new generation, and the text scraped off
            # the page is not the design it built. Their deliverable is
            # what they made in their own page, and the link keeps it.
            chat_tool = not (_is_maker(agent_cfg)
                             or agent_cfg.get("search_tool")
                             or agent_cfg.get("runner")
                             or agent_name == "NotebookLM")
            # A ChatGPT file card means the deliverable is already there.
            # If the browser has not copied it to Prism yet, wait/report that
            # state rather than asking the same chat to create it again.
            # (The card itself opens a preview, so it is never a download
            # fallback; `_capture_download` waits for its explicit button.)
            card_waiting = (agent_name == "ChatGPT" and not made_here
                            and _current_chatgpt_file_card(driver))
            if card_waiting:
                stage_notes[stage] = (
                    "The generated file is ready in ChatGPT; Prism is waiting "
                    "for its download control instead of requesting it again.")
                short_of = "download pending"
            elif not stopped() and chat_tool and not machine_shaped:
                fixed = _meet_contract(
                    driver, agent_cfg, stage, kind_here, stage_responses,
                    made_here, query=query, attachments=attachments,
                    task="\n".join(questions), should_stop=stage_halt)
                if fixed["files"]:
                    _save_artifacts(fixed["files"], query, stage,
                                    link=all_links.get(stage, ""))
                    pipeline_files[:] = (pipeline_files + fixed["files"])[-6:]
                    made_here = list(made_here) + fixed["files"]
                if fixed["texts"] and not stage_responses:
                    stage_responses = fixed["texts"]
                    all_responses[stage] = stage_responses
                if fixed["note"]:
                    stage_notes[stage] = fixed["note"]
                short_of = fixed["missing"]

            # THE SKILL CHECK, for a text step whose deliverable is there.
            # A file, image or video step is judged by whether the thing
            # exists (the contract above), a program-read step by its own
            # parser, and a step whose deliverable the contract found missing
            # has already had its one re-ask. So a step is asked again at
            # most once. The faults that matter here -- a deck with no
            # speaker notes, a figure with no source, a 250 mm emergency stop
            # -- all read perfectly well, so the only way to catch them is to
            # look.
            if (skill_keys and stage_responses and not machine_shaped
                    and kind_here == "text" and chat_tool and not short_of
                    and not stopped()
                    and not _builds_deliverable(agent_cfg, stage)):
                faults = SK.check(skill_keys, stage_responses[-1],
                                  {"stage": stage, "query": query,
                                   "prompt": skill_task_text})
                if faults:
                    ui.warn(f"   the answer misses {len(faults)} point(s) of "
                            f"the {', '.join(skill_keys)} skill — sending "
                            "them back")
                    for fault in faults[:6]:
                        ui.info(f"   · {ui.literal(fault)}")
                    emit("retry", {"stage": stage,
                                   "reason": f"{len(faults)} skill fault(s)"})
                    again = _reask(driver, agent_cfg, SK.repair_prompt(faults),
                                   should_stop=stage_halt)
                    better = [x for x in again if x and x.strip()]
                    if better:
                        left = SK.check(skill_keys, better[-1],
                                        {"stage": stage, "query": query,
                                         "prompt": skill_task_text})
                        # Only if it is genuinely cleaner — a "fix" that
                        # trades six faults for seven is not a fix.
                        if len(left) < len(faults):
                            stage_responses = [better[-1]]
                            all_responses[stage] = stage_responses
                            ui.ok(f"   fixed — {len(faults)} down to "
                                  f"{len(left)}")
                        else:
                            ui.info("   the second attempt was no better — "
                                    "keeping the first")
            if made_here:
                if kind_here == "video":
                    # A browser video tool's output is harvested like a
                    # picture and tagged "image"; the step was owed a video.
                    delivered["video"] = delivered.get("video", 0) + 1
                for _item in made_here:
                    _k = "image" if (_item.get("kind") == "image") else "file"
                    delivered[_k] = delivered.get(_k, 0) + 1
            elif (not chat_tool and stage_responses
                    and kind_here in ("file", "image", "video")):
                # A maker answered and its design or video lives in its own
                # page; the saved link is the deliverable.
                delivered[kind_here] = delivered.get(kind_here, 0) + 1

            # A tool that answered WITH A FILE and no prose has answered.
            # This used to fall through to "it returned nothing": the card
            # said "Prism couldn't read the response off the page", the run
            # recorded a failure, and the DOCX sat in Artifacts unmentioned.
            if not stage_responses and made_here:
                stage_responses = [_files_as_reply(made_here)]
                all_responses[stage] = stage_responses
                ui.info("   📎  the reply is the file itself — kept as this "
                        "step's result")

            if stage_responses:
                ui.ok(f"captured {len(stage_responses)} response(s)")
                if short_of:
                    # Finished, but not with the thing that was asked for.
                    # Said out loud here and carried to the card: an image
                    # step that produced no image used to be reported as a
                    # completed step and only noticed when the reel came out
                    # blank.
                    ui.warn(f"   ⚠️   {short_of}")
                emit("stage_done", {"stage": stage, "count": len(stage_responses),
                                    "snippet": stage_responses[0][:200],
                                    "texts": stage_responses, "url": driver.current_url,
                                    "timed_out": timed_out,
                                    "note": stage_notes.get(stage, ""),
                                    "missing": short_of,
                                    "files": _file_summaries(made_here)})
                if timed_out:
                    # Real output, kept in all_responses above — but the cap,
                    # not the tool, ended this wait, so what got captured may
                    # be a sentence cut off mid-word rather than the finished
                    # answer. Flagged here rather than silently treated as a
                    # normal "done" stage; see the summary after the loop.
                    incomplete[stage] = {"agent": agent_name,
                                         "questions": questions}
            else:
                # Nothing came back. Before reporting that as a scrape miss,
                # ask the three far more likely questions: has this tool run
                # out, is this a sign-in wall, and — if neither, and our prompt
                # is visibly on the page — did the site change its markup?
                spent = _looks_exhausted(driver)
                blocked = (spent or _looks_signed_out(driver)
                           or _looks_unreadable(driver, questions, timed_out))
                if blocked:
                    ui.err(blocked)
                else:
                    ui.warn("no response scraped, but link saved")
                failures[stage] = {
                    "agent": agent_name, "questions": questions,
                    "reason": blocked or "it returned nothing",
                    "exhausted": bool(spent)}
                emit("stage_done", {"stage": stage, "count": 0, "texts": [],
                                    "url": driver.current_url,
                                    "timed_out": timed_out,
                                    "blocked": blocked,
                                    "exhausted": bool(spent)})
            ui.info(f"   🔗  {driver.current_url}")

        except Exception as ex:
            # The tab is still open on whatever the tool was doing, and for
            # the slow producers (decks, video, apps) that page IS the
            # deliverable — it keeps rendering server-side after we gave up.
            # So the link goes out with the error, not instead of it.
            url = _safe_url(driver, exclude=set(all_links.values()))
            if url:
                all_links[stage] = url
                ui.info(f"   🔗  {url}  (still open — the tool may finish there)")
            ui.err(f"stage {stage} failed: {ex}")
            emit("stage_error", {"stage": stage, "error": str(ex), "url": url})

            # A closed browser is not a failed STAGE, it is a failed RUN.
            #
            # Every stage after this one would open the same dead session,
            # raise the same error and take its own timeout getting there — so
            # a run stopped at step two reports five identical failures over
            # several minutes, and the customer has to work out which of them
            # was the real one. Worse, the failover pass would then try each
            # of them again on a second tool, through the same dead driver.
            #
            # Stop here and say so once, keeping everything already finished.
            if _browser_is_gone(ex):
                ui.err("   the browser window is gone — stopping here and "
                       "keeping everything finished so far")
                emit("browser_lost", {"stage": stage, "error": str(ex),
                                      "done": len(all_responses)})
                return all_responses, all_links

            failures[stage] = {"agent": agent_name, "questions": questions,
                               "reason": str(ex), "exhausted": False}

    if incomplete:
        # A transient ui.warn already fired for each of these the moment its
        # cap ran out (mid-run, easy to miss); this is the persistent version
        # — printed once, right where the CLI's "Results" summary and the
        # GUI's completion state read from next — so a partial answer is
        # never confused for a finished one just because it showed up on
        # time. The text itself is untouched: it is already sitting in
        # all_responses, kept rather than thrown away.
        ui.warn(f"{len(incomplete)} stage(s) may be incomplete — the tool's "
                "time ran out before its answer finished (its tab is still "
                "open and may finish there): "
                + ", ".join(f"{s} ({i['agent']})" for s, i in incomplete.items()))
        emit("run_incomplete", {"stages": incomplete})

    if failover and failures and not stopped():
        _retry_failed_stages(
            failures, cfg, all_responses, all_links,
            attachments=attachments, query=query, emit=emit,
            should_stop=should_stop, stages=stages,
            pipeline_files=pipeline_files, brand=studio_brand,
            skip_signal=skip_signal, image_stages=image_stages)

    # The renderers held back above, now that every retry has been tried.
    # Recovered or not, the render happens: with the pictures if they came,
    # honestly without them if they did not — but never before the retry
    # that could have supplied them.
    for stage, agent_name, agent_cfg in deferred_locals:
        if stopped():
            break
        ui.info(f"   ▶  {stage}: the retries are done — rendering now")
        _run_local_stage(stage, agent_name, agent_cfg)

    # Finally: did the run produce the things it was for? Every step can end
    # green and the folder still be empty — a picture step that answered in
    # prose, a document step whose file was never collected, a render that
    # was skipped because an earlier stage failed. Checked once, here,
    # against what the plan promised, so the last thing said about a run is
    # true rather than merely tidy.
    if owed and not stopped():
        # The failover pass runs a nested run with counts of its own, and a
        # render re-run after a recovery never passes through the loop above;
        # both leave their evidence here instead.
        for _item in pipeline_files:
            _k = "image" if _item.get("kind") == "image" else "file"
            delivered[_k] = max(delivered.get(_k, 0), 1)
        for _i, (_st, _a, _q) in enumerate(stages):
            _link = all_links.get(_st) or ""
            if (stage_kinds.get(_i) == "video" and _link
                    and os.path.isfile(_link)):
                delivered["video"] = max(delivered.get("video", 0), 1)
        gap = _contract.shortfall(owed, delivered)
        emit("run_shortfall", {"missing": gap, "delivered": dict(delivered)})
        if gap:
            ui.warn("this run was asked for "
                    + ", ".join(_contract.label(k) for k in owed)
                    + " — and did not produce "
                    + ", ".join(_contract.label(k) for k in gap))

    return all_responses, all_links


# ── when a tool cannot finish ────────────────────────────────────────────────

def _retry_failed_stages(failures: dict, cfg: dict, all_responses: dict,
                         all_links: dict, *, attachments, query, emit,
                         should_stop, stages=None, pipeline_files=None,
                         brand=None, skip_signal=None, image_stages=None,
                         fallback_signal=None) -> None:
    """Give each empty stage to a different tool.

    The failure this exists for: forty minutes into a run, the free tier on
    whichever tool drew the heaviest stage runs out, and the customer is handed
    a pipeline that did nine tenths of the work and produced nothing usable.
    Another tool in the same category can almost always finish it.

    **It runs after the pipeline, not inside it, and that is a real
    limitation.** A stage that failed in the MIDDLE has already handed nothing
    to the stages after it, so those ran on thinner context and are not re-run
    here. The last stage — which is where a quota runs out, because that is
    where the most has been asked — is fully recovered. Doing better means
    retrying inline, which means restructuring a 500-line loop whose index-keyed
    maps (machine_stages, spec_feeder, design_feeder) all shift if the stage
    list changes underneath it.

    One case of that limitation IS fixed here, though, because it doesn't need
    the restructuring: a LOCAL renderer (Prism Reel, Prism Studio) runs as one
    self-contained function call, not a browser tab holding its place in the
    stage list — so if `visual` failed and only came back through this retry,
    the renderer already ran without the images it was waiting on, and calling
    that same function again is safe. See `_rerender_local_after_recovery()`,
    called at the end of this function once every stage here has been tried.

    Never raises: this is a rescue attempt, and a rescue that takes down the
    results it was rescuing would be worse than not trying.

    "Skip this step" works in here too. It did not: the nested run() below
    was started without the skip flag, so a press during a retry — the one
    place a customer is most likely to press it, watching a second tool
    grind on the same stuck stage — did nothing at all. Now the flag is what
    the nested run's waits poll (as a stop, so it winds up quietly and keeps
    what landed), and a press abandons the remaining alternatives for that
    stage rather than trying the next tool anyway.
    """
    def skipped() -> bool:
        return bool(skip_signal is not None and skip_signal.is_set())

    def fallback_pressed() -> bool:
        # "Use fallback" during a retry means "not this one either -- the
        # next tool, now". The nested run's waits break on it like a stop.
        return bool(fallback_signal is not None and fallback_signal.is_set())

    def halt() -> bool:
        return bool(should_stop and should_stop()) or skipped() or fallback_pressed()

    def give_up(stage: str, info: dict, texts: list) -> None:
        skip_signal.clear()
        if texts:
            all_responses[stage] = texts
        ui.warn(f"   ⤼  {stage}: skipped at your request — "
                + ("keeping what landed" if texts else "moving on"))
        emit("stage_skipped", {
            "stage": stage, "agent": info.get("agent"),
            "reason": "You skipped this step while it was being retried. "
                      + ("Whatever the tool had produced was kept; "
                         if texts else "")
                      + "the run moved on."})

    recovered: set[str] = set()
    for stage, info in list(failures.items()):
        if should_stop and should_stop():
            return
        # A later pass may already have filled this in.
        if all_responses.get(stage):
            continue
        # If an upstream planning/research stage failed, but downstream stages
        # already completed the actual brief, script or design, retrying the
        # upstream step is redundant and would only stall the user's run.
        if stage in ("brains", "research") and any(
                all_responses.get(s) for s in ("content", "design", "script", "artwork")):
            continue
        if skipped():
            give_up(stage, info, [])
            continue

        tried = [info.get("agent")]
        for alternative in A.alternatives_for(stage, tried, cfg, query=query):
            if should_stop and should_stop():
                return
            if skipped():
                give_up(stage, info, [])
                break
            ui.rule(f"{stage.upper()}  ·  retrying with {alternative}",
                    style="yellow")
            ui.warn(f"   {info.get('agent')} couldn't finish: {info['reason']}")
            emit("stage_failover", {
                "stage": stage, "failed": info.get("agent"),
                "agent": alternative, "reason": info["reason"],
                "exhausted": info.get("exhausted", False)})
            # This stage's own harvested images, pulled back out of the
            # nested run() below rather than lost with its own local
            # pipeline_files when that call returns — see pipeline_files_out.
            recovered_files: list = []
            try:
                responses, links = run(
                    {}, cfg,
                    attachments=attachments,
                    custom_stages=[(stage, alternative, info["questions"])],
                    query=query,
                    # The file-analysis pre-stage would re-read every
                    # attachment before the one prompt we actually want.
                    chatgpt_analysis=False,
                    # A skip reaches the nested run as a stop: its waits
                    # break, it scrapes what landed and returns, and the
                    # check just below decides what that means.
                    should_stop=halt,
                    # One level only. Without this a category where every tool
                    # is having a bad afternoon retries itself forever.
                    failover=False,
                    pipeline_files_out=recovered_files,
                    image_stages=image_stages)
            except Exception as e:                       # noqa: BLE001
                ui.err(f"   {alternative} also failed: {e}")
                continue

            texts = responses.get(stage) or []
            if skipped():
                give_up(stage, info, texts)
                break
            if fallback_pressed():
                fallback_signal.clear()
                ui.warn(f"   ↪  {alternative} abandoned at your request — "
                        "trying the next tool")
                continue
            if texts:
                all_responses[stage] = texts
                if links.get(stage):
                    all_links[stage] = links[stage]
                if recovered_files and pipeline_files is not None:
                    pipeline_files[:] = (pipeline_files + recovered_files)[-6:]
                recovered.add(stage)
                ui.ok(f"   ✅  {alternative} finished the {stage} step")
                emit("stage_recovered", {
                    "stage": stage, "agent": alternative,
                    "failed": info.get("agent"),
                    "texts": texts, "url": links.get(stage, "")})
                break
            ui.warn(f"   {alternative} returned nothing either")
        else:
            emit("stage_unrecovered", {
                "stage": stage, "failed": info.get("agent"),
                "reason": info["reason"]})

    if recovered and stages:
        _rerender_local_after_recovery(recovered, stages, cfg, all_responses,
                                       all_links, attachments=attachments,
                                       pipeline_files=pipeline_files,
                                       brand=brand, emit=emit)


def _rerender_local_after_recovery(recovered: set, stages, cfg: dict,
                                   all_responses: dict, all_links: dict, *,
                                   attachments, pipeline_files, brand, emit):
    """Give a local renderer a second pass once a stage feeding it has been
    recovered by failover.

    A LOCAL stage (Prism Reel, Prism Studio) runs synchronously, in its own
    turn in the main loop — so if `visual` failed there, the renderer already
    ran, without the images `visual` was going to hand it. `_retry_failed_stages`
    may then go on to recover `visual` through a different tool, but that
    happens after the whole pipeline finished; nothing about a plain text/URL
    handoff makes a video that already rendered start using pictures that
    showed up afterwards. Calling the same renderer function again is cheap
    and safe — unlike the browser stages, it isn't holding a tab or an index
    into `stages` that a second pass could disturb — so it is what gets called
    here rather than left as the limitation the rest of this failover accepts.

    Walks `stages` in order rather than jumping straight to the renderer, so a
    LOCAL stage only gets redone when something that actually ran BEFORE it
    was among the ones just recovered — a renderer with no recovered stage
    upstream of it has nothing new to draw from and is left alone.
    """
    seen_recovered: set[str] = set()
    for stage, agent_name, _questions in stages:
        if stage in recovered:
            seen_recovered.add(stage)
            continue
        if not seen_recovered or stage not in all_links:
            continue                    # nothing upstream recovered, or this
                                         # stage never ran in the first place
        agent_cfg = A.resolve_agent(stage, agent_name)
        if not (agent_cfg and agent_cfg.get("local")):
            continue
        ui.rule(f"{stage.upper()}  ·  re-rendering", style="yellow")
        ui.info(f"   🔁  {', '.join(sorted(seen_recovered))} came back after "
                f"{stage} finished the first time — rendering it again with "
                "what showed up")
        prior_text = [t for ts in reversed(list(all_responses.values()))
                      for t in ts if t.strip()]
        try:
            out, note = _run_local(agent_cfg["local"], prior_text,
                                   (attachments or []) + (pipeline_files or []),
                                   cfg, stage, brand=brand,
                                   studio=_studio_conversation(
                                       stages, stage, all_links))
        except Exception as e:                            # noqa: BLE001
            ui.err(f"   re-render failed: {e}")
            continue
        if out:
            all_responses[stage] = [note]
            all_links[stage] = out
            ui.ok(f"   {note}")
            emit("stage_done", {"stage": stage, "count": 1, "texts": [note],
                                "url": out, "timed_out": False})
        else:
            ui.err(f"   re-render failed: {note}")


def open_login_tabs(urls: list[str]):
    """Open each tool's URL so the user can sign in before a real run.

    Crucially this opens PRISM's profile, not the everyday one. Runs use
    PROFILE_DIR, so a login done in the normal browser lands in a different
    cookie jar and the run still hits a sign-in wall — which is exactly the
    'it isn't staying logged in' complaint. Signing in here writes to the same
    profile the automation drives, and it persists."""
    seed_profile()
    _clear_profile_locks()
    chrome = next((c for c in _CHROME_BINARIES if os.path.exists(c)), None)
    if not chrome:
        ui.warn("Chrome not found — opening in your default browser instead. "
                "Logins there will NOT carry into Prism's runs.")
        for url in urls:
            webbrowser.open(url)
        return

    args = [chrome, f"--user-data-dir={PROFILE_DIR}", "--profile-directory=Default"]
    ui.info("   🔐  opening Prism's browser profile — sign in here and it "
            "sticks for every run")
    first = True
    for url in urls:
        subprocess.Popen(args + [url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Chrome may be cold-starting on the first URL — give its singleton
        # lock time to settle so the remaining tabs join the same instance
        # instead of racing it and getting dropped.
        time.sleep(3.5 if first else 0.5)
        first = False
