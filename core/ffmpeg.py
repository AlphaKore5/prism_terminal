"""
Prism — finding FFmpeg, and fetching it when it is not there
────────────────────────────────────────────────────────────
FFmpeg does the video encoding for Reel and Studio. It is a separate program
Prism runs, not a library it links against.

The problem this module exists for: **Windows machines do not have it.** macOS
developers have it from Homebrew and never notice; the first Windows customer
opens Reel and is told to go and install a codec package. That is the end of
the trial.

Four places are looked in, in this order:

  1. `PRISM_FFMPEG` — an explicit path, for anyone who has a reason.
  2. **the copy that ships with Prism** (the `imageio-ffmpeg` package).
  3. a copy Prism downloaded earlier, in ~/.prism/tools.
  4. whatever is on PATH.

The shipped copy comes before the system one deliberately. Every customer then
encodes with the same build, so a video that came out right here cannot come
out wrong on their machine because their distribution shipped FFmpeg 4.2. A
system install is still honoured — it is just the fallback, not the default.

────────────────────────────────────────────────────────────────────────────
Downloading it
────────────────────────────────────────────────────────────────────────────
Only needed when Prism is run from source without the package installed, or on
a platform we have no wheel for. It fetches **the same wheel we bundle**, from
PyPI, and checks it against the SHA-256 that PyPI itself publishes for that
file — so there is no hash pinned in this repository to go stale, and nothing
is executed before it has been verified.

An unverified binary download would be a supply-chain hole in a product that
is sold on keeping a company's data on its own machines. The digest check is
not optional and there is no flag to skip it.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import zipfile

ENV_OVERRIDE = "PRISM_FFMPEG"
PACKAGE = "imageio-ffmpeg"
PYPI_JSON = "https://pypi.org/pypi/imageio-ffmpeg/json"

# Read in 256 KB pieces: big enough that hashing 30 MB is not a syscall storm,
# small enough that a progress bar moves and a cancel is noticed.
CHUNK = 262_144


class FFmpegError(Exception):
    """Couldn't find or fetch it — always with what to do next in the text."""


# ── where things live ─────────────────────────────────────────────────────────

def tools_dir() -> str:
    """~/.prism/tools — Prism's own bin directory.

    Not the workspace: a shared company folder may be on a network drive or a
    read-only mount, and a downloaded executable belongs to the machine rather
    than to the team.
    """
    path = os.path.join(os.path.expanduser("~"), ".prism", "tools")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _exe_name() -> str:
    return "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"


def _usable(path: str | None) -> str | None:
    if not path:
        return None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


# ── the four places ───────────────────────────────────────────────────────────

def from_env() -> str | None:
    return _usable(os.environ.get(ENV_OVERRIDE, "").strip() or None)


def bundled() -> str | None:
    """The copy that ships inside Prism, via the imageio-ffmpeg package."""
    try:
        import imageio_ffmpeg
    except Exception:
        return None
    try:
        return _usable(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        # The package is present but its binary is not — a partial install, or
        # a frozen build that bundled the Python and forgot the executable.
        return None


def downloaded() -> str | None:
    return _usable(os.path.join(tools_dir(), _exe_name()))


def on_path() -> str | None:
    return _usable(shutil.which("ffmpeg"))


def locate() -> str | None:
    """The FFmpeg this machine should use, or None."""
    for finder in (from_env, bundled, downloaded, on_path):
        found = finder()
        if found:
            return found
    return None


def is_available() -> bool:
    return locate() is not None


def describe() -> str:
    """Which one is being used, for the self-test and diagnostics."""
    for name, finder in (("set by PRISM_FFMPEG", from_env),
                         ("bundled with Prism", bundled),
                         ("downloaded by Prism", downloaded),
                         ("installed on this computer", on_path)):
        found = finder()
        if found:
            return f"{found} ({name})"
    return "not found"


# A minute of 1080p H.264 out of this pipeline lands around 30-60 MB, and
# FFmpeg needs working room on top of what it writes. 500 MB is comfortably
# more than any reel Prism makes and small enough that it never refuses a
# machine that could actually have finished.
RENDER_HEADROOM_MB = 500


def free_mb(path: str) -> int | None:
    """Free space on the volume `path` will be written to, or None.

    None when it cannot be determined — a filesystem that does not report it,
    a path whose parents do not exist yet. The caller treats that as "carry
    on": refusing to render because a disk-space probe failed would turn a
    diagnostic into an outage.
    """
    import shutil as _shutil
    probe = os.path.dirname(os.path.abspath(path)) or "."
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return int(_shutil.disk_usage(probe).free / 1e6)
    except OSError:
        return None


def check_space(out_path: str, need_mb: int = RENDER_HEADROOM_MB) -> str:
    """"" if there is room to render, else a sentence saying there is not.

    Checked BEFORE a render rather than discovered during one. A disk that
    fills mid-encode gives FFmpeg's own "No space left on device" at minute
    four of a seven-minute job, leaves a truncated .mp4 that looks like a
    file, and costs the whole render — where the same fact stated up front
    costs nothing and names the fix. KNOWN_ISSUES #12.
    """
    free = free_mb(out_path)
    if free is None or free >= need_mb:
        return ""
    return (f"Not enough free space to make the video — about {free} MB left, "
            f"and Prism needs around {need_mb} MB to work in. Free some space "
            "and try again; nothing else has changed.")


MISSING = (
    "Prism needs FFmpeg to turn the frames into a video. It is a free, "
    "standard program — Prism can fetch it for you, which takes about a "
    "minute and around 30 MB."
)


# ── choosing the right wheel ──────────────────────────────────────────────────

def platform_tag() -> str:
    """The wheel tag for this machine, as PyPI names it."""
    machine = platform.machine().lower()
    if sys.platform.startswith("win"):
        # Windows on ARM runs the x64 FFmpeg through its own emulation; the
        # 32-bit "win32" wheel is the wrong answer there (and Python 3.12
        # reports ARM64 even for an x64 Prism running under emulation).
        return "win_amd64" if machine in ("amd64", "x86_64", "arm64", "aarch64") else "win32"
    if sys.platform == "darwin":
        return "macosx_11_0_arm64" if machine in ("arm64", "aarch64") else "macosx_10_9_x86_64"
    if machine in ("aarch64", "arm64"):
        return "manylinux2014_aarch64"
    return "manylinux2014_x86_64"


def _fetch_json(url: str, timeout: int = 30) -> dict:
    import requests
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def wheel_for(tag: str = "", index: dict | None = None) -> tuple[str, str, str]:
    """(url, sha256, filename) of the wheel for this machine.

    The digest comes from PyPI's own index rather than from a constant in this
    file. Nothing to update when imageio-ffmpeg releases, and no window in
    which a pinned hash and the current release disagree.
    """
    tag = tag or platform_tag()
    try:
        index = index or _fetch_json(PYPI_JSON)
    except Exception as e:
        raise FFmpegError(
            "Prism couldn't reach the download site to get FFmpeg. Check this "
            "computer's internet connection and try again."
        ) from e

    version = (index.get("info") or {}).get("version", "")
    files = (index.get("releases") or {}).get(version, [])
    for entry in files:
        name = entry.get("filename", "")
        if name.endswith(".whl") and tag in name:
            digest = (entry.get("digests") or {}).get("sha256", "")
            if not digest:
                continue
            return entry.get("url", ""), digest, name
    raise FFmpegError(
        f"There is no ready-made FFmpeg for this computer "
        f"({platform.system()} {platform.machine()}). Install it the usual "
        f"way for this system and Prism will find it.")


# ── fetching it ───────────────────────────────────────────────────────────────

def download(on_progress=None, *, timeout: int = 60) -> str:
    """Fetch, verify and install FFmpeg. Returns the path to the executable.

    `on_progress(done_bytes, total_bytes)` is called as it goes — a 30 MB
    download on an Indian office connection is a minute of silence otherwise,
    and silence in front of a customer reads as a hang.
    """
    import requests

    url, expected, filename = wheel_for()
    digest = hashlib.sha256()
    done = 0

    handle, temp_wheel = tempfile.mkstemp(suffix=".whl", dir=tools_dir())
    os.close(handle)
    try:
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                with open(temp_wheel, "wb") as f:
                    for chunk in response.iter_content(CHUNK):
                        if not chunk:
                            continue
                        f.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
        except FFmpegError:
            raise
        except Exception as e:
            raise FFmpegError(
                "The FFmpeg download didn't finish. Check this computer's "
                "internet connection and try again — nothing was changed."
            ) from e

        # Verified BEFORE anything is unpacked, let alone run. A truncated
        # download and a tampered one look identical at this point, and both
        # must stop here.
        if digest.hexdigest() != expected:
            raise FFmpegError(
                "The FFmpeg download arrived damaged, so Prism has thrown it "
                "away rather than use it. Try again — this is usually a "
                "network glitch.")

        return _install_from_wheel(temp_wheel)
    finally:
        try:
            os.unlink(temp_wheel)
        except OSError:
            pass


def _install_from_wheel(wheel_path: str) -> str:
    """Pull the executable out of the verified wheel and put it in place."""
    target = os.path.join(tools_dir(), _exe_name())
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            member = _binary_member(archive)
            temp_exe = target + ".part"
            with archive.open(member) as source, open(temp_exe, "wb") as out:
                shutil.copyfileobj(source, out, CHUNK)
            # Executable for the owner, readable by all — same as a system
            # install, and it has to be set before the swap so there is never
            # a moment where the final path exists but cannot be run.
            os.chmod(temp_exe, os.stat(temp_exe).st_mode | stat.S_IXUSR
                     | stat.S_IXGRP | stat.S_IXOTH)
            os.replace(temp_exe, target)
            _save_licence(archive)
    except FFmpegError:
        raise
    except Exception as e:
        raise FFmpegError(
            f"Prism couldn't unpack FFmpeg after downloading it. There may "
            f"not be enough free disk space. ({e})") from e
    return target


def _binary_member(archive: zipfile.ZipFile) -> str:
    """The one file in the wheel that is the executable.

    Matched by folder and prefix rather than by an exact name, because the
    name carries the version and the architecture and changes every release.
    Anything with a path separator beyond the expected folder is refused —
    a zip is allowed to name '../../bin/sh' and this is where that would land.
    """
    for name in archive.namelist():
        parts = name.split("/")
        if (len(parts) == 3 and parts[0] == "imageio_ffmpeg"
                and parts[1] == "binaries" and parts[2].startswith("ffmpeg-")):
            return name
    raise FFmpegError(
        "The FFmpeg download did not contain the program Prism expected. "
        "Install FFmpeg the usual way for this computer instead.")


def _save_licence(archive: zipfile.ZipFile) -> None:
    """Keep FFmpeg's licence next to the binary.

    Prism runs FFmpeg as a separate program rather than linking to it, which
    is the clean case — but shipping somebody a copy of a program means
    shipping its licence with it, and it costs one file to be correct.
    """
    for name in archive.namelist():
        if name.endswith("dist-info/LICENSE"):
            try:
                with archive.open(name) as source, \
                        open(os.path.join(tools_dir(), "ffmpeg-LICENSE.txt"),
                             "wb") as out:
                    shutil.copyfileobj(source, out)
            except OSError:
                pass
            return


def ensure(on_progress=None) -> str:
    """The path to FFmpeg, downloading it first if this machine has none."""
    found = locate()
    if found:
        return found
    return download(on_progress)
