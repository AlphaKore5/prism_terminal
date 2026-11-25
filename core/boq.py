"""
Prism — Bill of Quantities from a CAD drawing (/boq)
─────────────────────────────────────────────────────
No AI ever measures the drawing. A DWG/DXF is real vector geometry — lines,
polylines, arcs, blocks — and an LLM asked to eyeball a rasterized picture of
it can only guess plausible-looking numbers, which is worthless (or worse,
dangerous) for a document a client will price work against. So this module
does the measuring itself, deterministically, with a CAD library that reads
the actual entity coordinates: lengths from LINE/LWPOLYLINE/POLYLINE/ARC,
areas from closed polylines and HATCH boundaries, counts from INSERT (block)
references. The output is a plain quantities CSV — auditable, re-openable in
Excel, and never silently trusted. An AI stage only ever sees THAT structured
data afterward, to write it up as a professionally formatted BOQ document
(item numbers, descriptions, grouping by trade) — formatting is exactly what
it's good at; measuring is exactly what it isn't.

Real drawings keep teaching this module conventions it didn't know about —
read HVAC_DRAWING_RULES.md (same folder) before changing anything that
walks LINE/LWPOLYLINE/POLYLINE geometry, and add to it, in its own shape,
the next time a real client file finds another one.

.dwg is a proprietary binary format ezdxf cannot read directly. It must be
converted to .dxf (an open, documented format) FIRST, with one of:

  • Prism's own bundled `dwg2dxf` (GNU LibreDWG) — on an Apple Silicon Mac or
    64-bit Windows, nothing to install: _ensure_bundled_dwg2dxf() copies it
    into ~/.prism/tools/dwg2dxf/ the first time a .dwg is opened, and
    find_dwg_converter() finds it there on every run after. See
    core/vendor/dwg2dxf/README.md for exactly what's bundled, where it came
    from, and a real failure mode found while verifying it (read that before
    touching the validation in dwg_to_dxf() below — it is load-bearing).
    Coverage of newer/exotic DWG features is not perfect — good enough for
    ordinary 2D architectural drawings, which is the common case here.

  • GNU LibreDWG, installed by hand, on platforms Prism has no bundled copy
    for (an Intel Mac, Linux, 32-bit Windows):
        macOS   →  brew install libredwg          (gives the `dwg2dxf` CLI)
        Linux   →  build LibreDWG from source (many current Debian/Ubuntu
                    repositories do not package the `dwg2dxf` utility)

  • ODA File Converter (free, more complete, GUI-first but scriptable),
    always honoured first if it's already installed — more reliable than
    dwg2dxf (full DWG spec, not a reverse-engineered reader) and worth
    recommending to a customer who hits real conversion problems:
        https://www.opendesign.com/guestfiles/oda_file_converter
    On Windows it installs under %ProgramFiles%\\ODA\\ODAFileConverter*\\ and
    does NOT add itself to PATH — find_dwg_converter() globs that location, so
    no PATH surgery is needed there. On Linux, put `ODAFileConverter` on PATH.

ensure_dxf() detects whichever is available (bundled dwg2dxf counts); if
none is, it raises a clear error naming the install options rather than
failing obscurely.

KNOWN LIMITATIONS (disclosed, not hidden):
  • Curved polyline segments (bulges) are measured exactly for LWPOLYLINE;
    legacy POLYLINE segments are treated as straight (a documented
    approximation — rare in modern exports).
  • HATCH boundaries that are true splines/ellipses are measured via their
    control/fit points, not exact curve integration — fine for the polygonal
    boundaries architectural floor hatches almost always use.
  • Numbers are only as good as the source drawing's layer/block discipline.
    A drawing with everything dumped on layer "0" with no named blocks will
    still parse, but the grouping will be useless — this tool cannot invent
    structure the drawing never had.
"""
from __future__ import annotations
import csv
import math
import re
import os
import platform
import shutil
import subprocess
import sys
import tempfile

from . import skills as _SK

# An explicit path, for anyone who has a reason — same escape hatch
# core/ffmpeg.py offers as PRISM_FFMPEG.
ENV_CONVERTER = "PRISM_DWG_CONVERTER"

# DXF $INSUNITS header codes worth naming — the common ones. Anything else is
# reported as "unspecified (code N)" rather than guessed.
_UNIT_NAMES = {
    0: "unspecified", 1: "inches", 2: "feet", 3: "miles",
    4: "millimeters", 5: "centimeters", 6: "meters", 8: "US survey feet",
}


class BoqError(Exception):
    pass


# The first sentence of the "no converter" message. The GUI looks for it to open
# its own "install the converter" window instead of a plain message box.
NO_CONVERTER_TEXT = ("Prism needs a free helper program to open a DWG drawing, "
                     "and this computer does not have it yet.")


class NoConverterError(BoqError):
    """A .dwg was given and no DWG→DXF converter is installed or chosen."""


# ── DWG → DXF ──────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))


def _dwg2dxf_platform_tag() -> str | None:
    """Vendor subfolder name (under core/vendor/dwg2dxf/) for this machine,
    or None if Prism carries no bundled dwg2dxf for it — those machines fall
    through to PATH / a manual install exactly as before this existed. See
    core/vendor/dwg2dxf/README.md for what's bundled and why only these two."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    # Windows on ARM runs the x64 build through its own emulation (Windows 11),
    # and LibreDWG publishes no ARM build, so ARM64 uses the same files.
    if system == "Windows" and machine in ("amd64", "x86_64", "arm64", "aarch64"):
        return "win64"
    return None


#: Why Prism's own converter could not be set up on this machine, or "". It was swallowed silently
#: before, so a person was told "no converter found" with no hint that one was bundled and failed
#: to install (antivirus, a read-only folder, a build without the files).
BUNDLED_PROBLEM = ""


def _ensure_bundled_dwg2dxf() -> None:
    """Install Prism's own bundled dwg2dxf into ~/.prism/tools/dwg2dxf/<tag>/
    the first time it's needed, so find_dwg_converter()'s existing tools-dir
    scan (below) picks it up with no other change to this file. A cheap
    no-op once it's there. Never raises — a failed install just means the
    tools-dir glob finds nothing new and the search falls through to PATH /
    an installed converter / "please install one", same as before this
    function existed."""
    global BUNDLED_PROBLEM
    BUNDLED_PROBLEM = ""
    tag = _dwg2dxf_platform_tag()
    if not tag:
        BUNDLED_PROBLEM = (f"Prism has no built-in converter for this computer "
                           f"({platform.system()} {platform.machine()}).")
        return
    exe_name = "dwg2dxf.exe" if tag.startswith("win") else "dwg2dxf"
    src_dir = os.path.join(_HERE, "vendor", "dwg2dxf", tag)
    src_exe = os.path.join(src_dir, exe_name)
    if not os.path.isfile(src_exe):
        BUNDLED_PROBLEM = "Prism's built-in converter is missing from this copy of Prism."
        return  # this checkout/build doesn't carry a vendor copy

    try:
        from . import ffmpeg as _ffmpeg
        dst_dir = os.path.join(_ffmpeg.tools_dir(), "dwg2dxf", tag)
    except Exception:
        return
    dst_exe = os.path.join(dst_dir, exe_name)

    # Already installed, same size as the one we ship: nothing to do. Not a
    # hash check — this runs on every find_dwg_converter() call, and a
    # cross-platform stat is cheap where re-hashing a bundled DLL set is not.
    if (os.path.isfile(dst_exe)
            and os.path.getsize(dst_exe) == os.path.getsize(src_exe)):
        return
    try:
        os.makedirs(dst_dir, exist_ok=True)
        for name in os.listdir(src_dir):
            if name == "README.md":
                continue
            shutil.copy2(os.path.join(src_dir, name), os.path.join(dst_dir, name))
        os.chmod(dst_exe, 0o755)  # PyInstaller's own datas don't promise the +x bit survives
    except OSError as e:
        # Not fatal, but say why: security software removing or locking the file shows up here.
        BUNDLED_PROBLEM = (f"Prism's built-in converter could not be set up in {dst_dir} ({e}). "
                           "Security software may be blocking it.")


def _installed_converter_paths() -> list[str]:
    """Where a DWG converter actually lands when somebody installs one.

    PATH alone was the whole search, and PATH is exactly where neither of
    these puts itself:

      · **ODA File Converter on Windows** installs to `C:\\Program Files\\ODA\\
        ODAFileConverter <version>\\ODAFileConverter.exe` — a versioned folder,
        and the installer adds nothing to PATH. So a customer who followed
        Prism's own instructions, installed it, and restarted, was still told
        "No DWG→DXF converter found on this machine".
      · **LibreDWG from Homebrew on macOS** puts `dwg2dxf` in /opt/homebrew/bin
        (or /usr/local/bin on Intel). That is on PATH in Terminal and NOT in
        the environment a Finder-launched .app inherits — so it worked when a
        developer ran Prism from a shell and failed for every customer who
        double-clicked it.

    Both are cases of "it IS installed and Prism cannot see it", which is
    worse than not having it: the person has already done the work.
    """
    import glob
    system = platform.system()
    found: list[str] = []

    if system == "Windows":
        roots = [os.environ.get(var) for var in (
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA")]
        # The usual folders even when the environment does not name them.
        roots += ["C:\\Program Files", "C:\\Program Files (x86)"]
        seen_roots: set = set()
        for root in roots:
            if not root or root.lower() in seen_roots:
                continue
            seen_roots.add(root.lower())
            # The folder carries the version, so it has to be matched
            # rather than named: ODAFileConverter 25.4.0, 26.2.0, and so on.
            #
            # Sorted by _oda_version_key, NOT reverse-lexicographically as
            # this first shipped — that ordering put "ODAFileConverter 9.0"
            # above "25.4.0", because it compares "9" against "2" one
            # character at a time. It passed its test by luck (25 vs 26 both
            # start with a 2). mtime breaks a tie between two installs of the
            # same version.
            found += sorted(
                glob.glob(os.path.join(root, "ODA", "ODAFileConverter*",
                                       "ODAFileConverter.exe")),
                key=lambda hit: (_oda_version_key(hit),
                                 os.path.getmtime(hit)
                                 if os.path.exists(hit) else 0),
                reverse=True)          # newest version first
            found.append(os.path.join(root, "ODA", "ODAFileConverter.exe"))
    elif system == "Darwin":
        found += [
            "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
            os.path.expanduser("~/Applications/ODAFileConverter.app/Contents/"
                               "MacOS/ODAFileConverter"),
            *sorted(glob.glob("/Applications/ODAFileConverter*.app/Contents/"
                              "MacOS/ODAFileConverter")),
            "/opt/homebrew/bin/dwg2dxf",     # Apple Silicon Homebrew
            "/usr/local/bin/dwg2dxf",        # Intel Homebrew
        ]
    else:
        found += [
            "/usr/bin/ODAFileConverter",
            "/usr/local/bin/ODAFileConverter",
            # the .deb/.rpm unpack into /opt; an AppImage sits wherever it was saved
            *sorted(glob.glob("/opt/ODAFileConverter*/ODAFileConverter")),
            *sorted(glob.glob(os.path.expanduser(
                "~/Applications/ODAFileConverter*.AppImage"))),
            *sorted(glob.glob(os.path.expanduser(
                "~/Downloads/ODAFileConverter*.AppImage"))),
            *sorted(glob.glob(os.path.expanduser("~/ODAFileConverter*.AppImage"))),
            "/usr/bin/dwg2dxf",
            "/usr/local/bin/dwg2dxf",
        ]
    return found



def _oda_version_key(exe_path: str) -> tuple[int, int, int]:
    """Pull a (major, minor, patch) version out of ODA's install-folder name
    (e.g. '…\\ODAFileConverter 26.7.0\\…' or '…\\ODAFileConverter_title 25.6.0\\…',
    and older 2-part or version-less folders). The version-less default folder
    'ODAFileConverter' has no digits, so it sorts as (0, 0, 0) and loses to any
    explicitly versioned install. Tolerates 1-, 2- or 3-component numbers so a
    'ODAFileConverter 25.6' folder still ranks above a version-less one."""
    import re
    # Both separators, not os.path's. This is a WINDOWS path being parsed,
    # and os.path.dirname on POSIX does not treat "\\" as a separator — so
    # the whole path came back as one basename, no digits matched, and every
    # install scored (0, 0, 0). Harmless on Windows, where it worked; fatal
    # to testing it anywhere else, which is where the suite runs.
    folder = exe_path.replace("\\", "/").rstrip("/").rsplit("/", 2)
    folder = folder[-2] if len(folder) >= 2 else ""
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", folder)
    if not m:
        return (0, 0, 0)
    return tuple(int(g) if g else 0 for g in m.groups())



def find_dwg_converter() -> str | None:
    """The DWG→DXF converter this machine should use, or None.

    ODA File Converter is preferred over dwg2dxf at every stage below — it
    reads the full DWG spec; dwg2dxf is a reverse-engineered reader that can
    fail on newer/exotic DWG features (see core/vendor/dwg2dxf/README.md for
    a real example). So each stage checks ODA's names before dwg2dxf's name,
    and Prism's own bundled dwg2dxf — installed on first use, see
    _ensure_bundled_dwg2dxf — is tried dead last, only once every other
    possibility (an explicit override, anything already on this machine) has
    come up empty. A customer who installed ODA themselves always gets it;
    Prism's bundled copy exists for the machines that have nothing at all.

    The places, in the order core/ffmpeg.py already established for FFmpeg:
    an explicit override, Prism's own tools directory, PATH, the places an
    installer actually puts these (_installed_converter_paths), and finally
    Prism's own bundled copy.

    Prism's own tools folder is searched BEFORE PATH on purpose: a binary
    somebody placed there deliberately should beat whatever PATH happens to
    hold. Searched recursively, because the normal shape of a hand-placed
    converter is an unzipped folder, not a loose executable.
    """
    override = os.environ.get(ENV_CONVERTER, "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    # ~/.prism/tools/** — the same directory FFmpeg is fetched into, so a
    # converter dropped there (by a user, or by _ensure_bundled_dwg2dxf
    # below on an earlier call) is found with no PATH change, on any OS.
    # Reached before PATH, and cheap: a small tree, walked once. ODA's own
    # names come first in the glob so a user-dropped ODA always beats a
    # user-dropped (or Prism-installed) dwg2dxf sitting in the same folder.
    try:
        from . import ffmpeg as _ffmpeg
        tools = _ffmpeg.tools_dir()
    except Exception:
        tools = ""
    if tools and os.path.isdir(tools):
        import glob
        for name in ("ODAFileConverter.exe", "ODAFileConverter",
                     "dwg2dxf.exe", "dwg2dxf"):
            for hit in sorted(glob.glob(os.path.join(tools, "**", name),
                                        recursive=True)):
                if os.path.isfile(hit):
                    return hit

    # Anything actually on PATH — an ODAFileConverter a user deliberately
    # PATH-added, or `dwg2dxf` on macOS/Linux. shutil.which() honours
    # PATHEXT on Windows, so the bare name already covers the .exe. ODA's
    # names checked first, same reasoning as the glob above.
    for name in ("ODAFileConverter", "ODAFileConverter.exe", "dwg2dxf"):
        found = shutil.which(name)
        if found:
            return found

    # Where an installer actually puts these. ODA's Windows installer adds
    # nothing to PATH, and Homebrew's bin is absent from the environment a
    # Finder-launched .app inherits — both are "installed, and invisible".
    for path in _installed_converter_paths():
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # Last resort: Prism's own bundled dwg2dxf (Apple Silicon Mac / 64-bit
    # Windows only — see _dwg2dxf_platform_tag). Installed here, not at the
    # top of this function, specifically so it never pre-empts a real ODA
    # install found above.
    _ensure_bundled_dwg2dxf()
    if tools and os.path.isdir(tools):
        import glob
        for name in ("dwg2dxf.exe", "dwg2dxf"):
            for hit in sorted(glob.glob(os.path.join(tools, "dwg2dxf", "**", name),
                                        recursive=True)):
                if os.path.isfile(hit):
                    return hit
    return None

def _read_dxf(path: str):
    """Open a DXF, tolerating the kind of minor structural errors a
    third-party writer (this app's own dwg2dxf fallback included) can leave
    behind. Tries the strict reader first — it keeps every bit of metadata,
    including $INSUNITS. Only if that fails does it fall back to ezdxf's
    lenient recovery reader, which repairs what it can and reports what it
    couldn't. Returns (doc, notes); raises BoqError if even recovery can't
    make sense of the file. Confirmed necessary against a real client DWG
    whose full-mode conversion produced one malformed numeric tag — enough
    to make the strict reader reject the entire file outright."""
    try:
        import ezdxf
    except ModuleNotFoundError as e:
        if e.name != "ezdxf":
            raise
        raise BoqError(
            "DXF measurement needs the `ezdxf` Python package. Install it into "
            f"the Python that started Prism:\n  {sys.executable} -m pip install ezdxf"
        ) from e
    try:
        return ezdxf.readfile(path), []
    except Exception:
        pass
    try:
        import ezdxf.recover as recover
        doc, auditor = recover.readfile(path)
    except Exception as e:
        raise BoqError(f"Couldn't read that DXF, even with error recovery: {e}")
    notes = []
    if auditor.errors:
        notes.append(
            f"The DXF needed error recovery ({len(auditor.errors)} structural "
            "issue(s)) — some entities may be missing or altered. Treat these "
            "quantities as a strong starting point, not a final figure.")
    return doc, notes


def _run_dwg2dxf(converter: str, dwg_path: str, out_path: str, minimal: bool):
    cmd = [converter] + (["-m"] if minimal else []) + ["-y", dwg_path, "-o", out_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def dwg_to_dxf(dwg_path: str, out_dir: str | None = None) -> tuple[str, list[str]]:
    """Convert a .dwg to .dxf with whatever converter is installed locally.
    Returns (dxf_path, notes) — notes flags anything the caller should know
    before trusting the result (e.g. a fallback that drops unit metadata).
    Raises BoqError with install instructions for both options if neither is
    on PATH, or if conversion genuinely fails — never fails silently.

    A converter reporting success is NOT taken on faith — the result is
    actually opened with ezdxf before being accepted, because a real client
    file surfaced a case where dwg2dxf exited 0 and wrote a file that still
    turned out to have one malformed tag full-mode conversion can't avoid on
    that source. If a full conversion can't actually be read, a minimal
    (-m) reconversion is tried before giving up — see _read_dxf/-m's own
    docstring for what each fallback costs."""
    converter = find_dwg_converter()
    if not converter:
        # Named per OS, because the previous text offered `brew install` and
        # "on Linux…" and nothing else — so the one platform with no route at
        # all was Windows, which is most of the customers this add-on is sold
        # to. ODA File Converter is free, has a Windows installer, and is the
        # answer on all three.
        raise NoConverterError(
            NO_CONVERTER_TEXT + "\n"
            + (BUNDLED_PROBLEM + "\n" if BUNDLED_PROBLEM else "")
            + "Get it here (free): https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "Or save the drawing as a DXF file and attach that instead.")
    out_dir = out_dir or tempfile.mkdtemp(prefix="prism_boq_")
    name = os.path.splitext(os.path.basename(dwg_path))[0]
    out_path = os.path.join(out_dir, f"{name}.dxf")
    notes: list[str] = []

    if os.path.basename(converter).lower().startswith("dwg2dxf"):
        result = _run_dwg2dxf(converter, dwg_path, out_path, minimal=False)
        readable = False
        first_err = ""
        if result.returncode == 0 and os.path.exists(out_path):
            try:
                _read_dxf(out_path)   # validate — don't just trust the exit code
                readable = True
            except BoqError as e:
                first_err = str(e)
        else:
            first_err = (result.stderr or result.stdout or "").strip()[:300]

        if not readable:
            # A full conversion pulls table-style/material sections that can
            # make libredwg abort outright, or emit a tag ezdxf can't parse,
            # on some real modern DWGs (both confirmed against an actual
            # client file). -m (minimal: $ACADVER/HANDSEED/ENTITIES only)
            # skips those sections and often succeeds where full conversion
            # can't — but it can also drop header metadata, including
            # $INSUNITS, so the result's units may be unconfirmed. We only
            # need entity geometry for a takeoff, so retry with it, but
            # surface the tradeoff rather than hiding it.
            result = _run_dwg2dxf(converter, dwg_path, out_path, minimal=True)
            if result.returncode != 0 or not os.path.exists(out_path):
                raise BoqError(
                    f"DWG→DXF conversion failed even in minimal mode: "
                    f"{(result.stderr or result.stdout or '').strip()[:400]}\n"
                    f"(full-mode attempt also failed: {first_err})"
                )
            try:
                _read_dxf(out_path)
            except BoqError as e:
                raise BoqError(
                    f"Neither full nor minimal conversion produced a file "
                    f"ezdxf could read. Full mode: {first_err}. Minimal mode: {e}"
                )
            notes.append(
                "Full conversion couldn't be read, so a minimal fallback was "
                "used — geometry came through, but the drawing's unit may "
                "not have been recorded. VERIFY the actual unit (mm/m/ft) "
                "against the source drawing before trusting any quantity below."
            )
    else:
        # ODA File Converter's CLI takes (in_dir, out_dir, ver, type, recurse,
        # audit, [filter]) — it converts a whole folder, not a single file, so
        # point it at the source's directory and filter to just this file.
        in_dir = os.path.dirname(os.path.abspath(dwg_path))
        cmd = [converter, in_dir, out_dir, "ACAD2018", "DXF", "0", "1",
               os.path.basename(dwg_path)]
        # ODA's converter is a Qt GUI binary (CLI and GUI are one .exe), so a
        # plain launch flashes a window on every run. Best-effort suppression,
        # Windows-only: STARTUPINFO/SW_HIDE hides the window, CREATE_NO_WINDOW
        # any console. Both are advisory for a GUI app that calls show() itself,
        # so a brief flash may still slip through — but neither can break the
        # conversion, and on macOS/Linux this stays the old call unchanged
        # (startupinfo=None, creationflags=0).
        run_kwargs: dict = {}
        # ODA is a Qt program with its own Qt. Prism is a Qt program too, and its
        # Qt settings (QT_QPA_PLATFORM, QT_PLUGIN_PATH, QML2_IMPORT_PATH...) are
        # inherited by anything it launches. Handed those, ODA looks for its
        # platform plugin in Prism's Qt and dies ("Could not find the Qt platform
        # plugin", exit 134 -- seen here on Linux with QT_QPA_PLATFORM=offscreen).
        # So ODA gets the environment without any Qt variable.
        run_kwargs["env"] = {k: v for k, v in os.environ.items()
                             if not k.upper().startswith("QT_") and k.upper() != "QML2_IMPORT_PATH"}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            run_kwargs["startupinfo"] = si
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=180, **run_kwargs)
        except Exception as e:
            raise BoqError(f"DWG→DXF conversion failed to run: {e}")
        # ODA's return code is unreliable across versions (it can exit non-zero
        # on success and, worse, zero after writing an empty .dxf), so trust the
        # file, not the code: it must exist, be non-empty, and actually open in
        # ezdxf — the same bar the dwg2dxf branch above holds its output to.
        # This is the branch every Windows run takes; it must not be the weaker.
        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            raise BoqError(
                f"DWG→DXF conversion produced no usable .dxf (exit "
                f"{result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:400]}"
            )
        try:
            _read_dxf(out_path)
        except BoqError as e:
            raise BoqError(f"The converter wrote a .dxf that couldn't be read: {e}")

    if not os.path.exists(out_path):
        raise BoqError("Converter ran but produced no .dxf — check the source file opens in AutoCAD.")
    return out_path, notes


def ensure_dxf(path: str) -> tuple[str, list[str]]:
    """Return (dxf_path, notes) — converting a .dwg first if that's what was
    given. notes is always a list (empty for a plain .dxf, which needed no
    conversion and so has no fallback caveats)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dxf":
        return path, []
    if ext == ".dwg":
        return dwg_to_dxf(path)
    raise BoqError(f"Not a CAD drawing Prism can measure: {path} (need .dwg or .dxf)")


# ── geometry ─────────────────────────────────────────────────────────────

def _dist(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _bulge_arc_length(a, b, bulge: float) -> float:
    """Arc length of one LWPOLYLINE segment with a bulge (curved segment).
    bulge = tan(included_angle / 4); this is the standard DXF bulge formula."""
    if not bulge:
        return _dist(a, b)
    chord = _dist(a, b)
    if chord == 0:
        return 0.0
    theta = 4 * math.atan(abs(bulge))
    if theta <= 0:
        return chord
    half = theta / 2
    sin_half = math.sin(half)
    if sin_half == 0:
        return chord
    radius = chord / (2 * sin_half)
    return radius * theta


def _polygon_area(points: list[tuple[float, float]]) -> float:
    """Shoelace formula. `points` need not repeat the first point at the end."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = points[i][0], points[i][1]
        x2, y2 = points[(i + 1) % n][0], points[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _lwpolyline_length(e) -> float:
    pts = [(p[0], p[1]) for p in e.get_points("xyb")]  # (x, y, bulge)
    if len(pts) < 2:
        return 0.0
    bulges = [p[2] if len(p) > 2 else 0.0 for p in e.get_points("xyb")]
    total = 0.0
    n = len(pts)
    segments = n if e.closed else n - 1
    for i in range(segments):
        a, b = pts[i], pts[(i + 1) % n]
        total += _bulge_arc_length(a, b, bulges[i])
    return total


def _lwpolyline_area(e) -> float:
    if not e.closed:
        return 0.0
    pts = [(p[0], p[1]) for p in e.get_points("xy")]
    return _polygon_area(pts)


def _legacy_polyline_length(e) -> float:
    # Straight-segment approximation — see module docstring's known limitations.
    pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
    if len(pts) < 2:
        return 0.0
    n = len(pts)
    segments = n if e.is_closed else n - 1
    return sum(_dist(pts[i], pts[(i + 1) % n]) for i in range(segments))


def _legacy_polyline_area(e) -> float:
    if not e.is_closed:
        return 0.0
    pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
    return _polygon_area(pts)


def _arc_length(e) -> float:
    radius = e.dxf.radius
    start, end = e.dxf.start_angle % 360, e.dxf.end_angle % 360
    span = end - start
    if span <= 0:
        span += 360
    return radius * math.radians(span)


def _hatch_area(e) -> float:
    """Sum external boundary loop areas, subtract internal (hole) loops.
    Non-polyline edges (arcs/splines/ellipses) are approximated via their
    control/fit vertices — see module docstring."""
    total = 0.0
    for path in e.paths:
        pts = []
        if hasattr(path, "vertices") and path.vertices:
            pts = [(v[0], v[1]) for v in path.vertices]
        elif hasattr(path, "edges"):
            for edge in path.edges:
                if hasattr(edge, "start"):
                    pts.append((edge.start[0], edge.start[1]))
        if len(pts) < 3:
            continue
        area = _polygon_area(pts)
        is_external = bool(getattr(path, "path_type_flags", 1) & 1)  # bit 0 = EXTERNAL
        total += area if is_external else -area
    return abs(total)


# ── double-line ductwork/pipework ───────────────────────────────────────────
# HVAC_DRAWING_RULES.md rule 1, found 22 Oct 2026 on a real HVAC test file: a
# duct (also a pipe, a cable tray — anything with real width worth drawing to
# scale) is very often drawn as its TWO EDGES, not one centreline, in ordinary
# professional CAD practice. Counting every LINE on the layer, oblivious to
# this, silently double-counted a run's length — summed BOTH edges as if they
# were two separate runs. Caught comparing Prism's own 78.82 m against an
# AI's own direct read of the same drawing, 38.80 m: a client's real file, not
# a corner case built to test with.

def _unit_dir(a, b, length: float | None = None) -> tuple[float, float]:
    length = _dist(a, b) if length is None else length
    if length <= 0:
        return (0.0, 0.0)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _duct_runs(segments: list) -> list[dict]:
    """One layer's straight LINE segments, resolved into real runs —
    {"a", "b", "length", "offset", "kind"} each, `kind` one of:

      "pair"       a matched double-line edge pair (rule 1): near-parallel,
                   near-equal-length, a small constant distance apart —
                   counted once, at the average of its two edges.
      "transition" a tapering reducer between two different-width pairs
                   (rule 2, added 23 Oct 2026, found on the same real file
                   rule 1 came from: a reducer is drawn as two short lines
                   that CONVERGE, not run parallel, so rule 1's own pairing
                   correctly leaves them unmatched — but counting both
                   diagonals in full double-counts the transition by about
                   its own length. Detected the same way a pair's own
                   offset is validated: both ends sit a plausible duct-width
                   apart (20-2000 mm), just a DIFFERENT width at each end —
                   that difference is what makes it a transition and not a
                   pair — and the transition itself is no longer than the
                   widest confirmed pair on the layer, so an unrelated long
                   diagonal line elsewhere can never be mistaken for one.
      "single"     no match — an ordinary single-line (centreline/
                   schematic) run, counted at its own full length, exactly
                   as before rule 1 ever existed.
      "bridged"    two collinear, same-width "pair" runs with a small gap
                   between them, joined into one continuous run (rule 2's
                   gap-bridging, revisited with real topology — see below).

    A short perpendicular stub that closes a pair's open end (not route
    length, just the drawing closing the rectangle) is dropped entirely —
    it appears in none of the kinds above.

    Two SEPARATE same-length parallel runs (e.g. a flow pipe and a return
    pipe) are safe by construction as long as the drawing keeps them on
    separate layers, which is the normal convention this whole module
    already relies on elsewhere — this function only ever compares lines
    within ONE layer's own list, never across layers."""
    n = len(segments)
    lengths = [_dist(a, b) for a, b in segments]
    dirs = [_unit_dir(a, b, length) for (a, b), length in zip(segments, lengths)]

    used = [False] * n
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        if used[i] or lengths[i] <= 0:
            continue
        best = None
        for j in range(i + 1, n):
            if used[j] or lengths[j] <= 0:
                continue
            # roughly parallel, either sense (cross product of unit vectors)
            cross = abs(dirs[i][0] * dirs[j][1] - dirs[i][1] * dirs[j][0])
            if cross > 0.05:
                continue
            # roughly the same length -- the two edges of one run
            if abs(lengths[i] - lengths[j]) > 0.08 * max(lengths[i], lengths[j]):
                continue
            # a small, constant perpendicular offset: the run's own width,
            # not an unrelated line that merely happens to run alongside it.
            # 0.5 (not a looser ratio) is deliberate: a real room's opposite
            # walls can be a similar length and a similar distance apart too
            # (caught live -- a 24 m / 14 m room scored 0.58 and was nearly
            # read as one 24 m duct) -- a genuine duct/pipe/tray run is a
            # thin strip, never close to as wide as it is long.
            nrm = (-dirs[i][1], dirs[i][0])
            off = abs((segments[j][0][0] - segments[i][0][0]) * nrm[0]
                      + (segments[j][0][1] - segments[i][0][1]) * nrm[1])
            if not (1.0 < off < 0.5 * max(lengths[i], lengths[j])):
                continue
            if best is None or off < best[1]:
                best = (j, off)
        if best:
            j, off = best
            used[i] = used[j] = True
            pairs.append((i, j, off))

    runs: list[dict] = []
    for i, j, off in pairs:
        a, b = segments[i]
        runs.append({"a": a, "b": b, "length": (lengths[i] + lengths[j]) / 2,
                    "offset": off, "kind": "pair", "seg_idxs": (i, j)})

    for k in range(n):
        if used[k] or lengths[k] <= 0:
            continue
        is_cap = False
        for i, _j, off in pairs:
            # roughly perpendicular to that pair's own direction, and about
            # as long as that pair's offset -- the stub closing its open end
            cross = abs(dirs[k][0] * dirs[i][1] - dirs[k][1] * dirs[i][0])
            if cross > 0.8 and abs(lengths[k] - off) < 0.25 * max(off, 1.0):
                is_cap = True
                break
        if is_cap:
            used[k] = True

    # rule 2: a tapering transition's two converging sides, counted once.
    leftover = [k for k in range(n) if not used[k] and lengths[k] > 0]
    widest_pair = max((r["length"] for r in runs), default=0.0)
    for a_i in range(len(leftover)):
        i = leftover[a_i]
        if used[i]:
            continue
        for b_i in range(a_i + 1, len(leftover)):
            j = leftover[b_i]
            if used[j]:
                continue
            cross = abs(dirs[i][0] * dirs[j][1] - dirs[i][1] * dirs[j][0])
            if cross <= 0.05:
                continue                      # parallel -- rule 1 already owns this
            if abs(lengths[i] - lengths[j]) > 0.08 * max(lengths[i], lengths[j]):
                continue
            start_gap = _dist(segments[i][0], segments[j][0])
            end_gap = _dist(segments[i][1], segments[j][1])
            if not (20.0 <= start_gap <= 2000.0 and 20.0 <= end_gap <= 2000.0):
                continue
            if widest_pair and max(lengths[i], lengths[j]) > widest_pair:
                continue                      # longer than any confirmed duct width -- not a transition
            used[i] = used[j] = True
            runs.append({"a": segments[i][0], "b": segments[j][1],
                        "length": (lengths[i] + lengths[j]) / 2, "offset": None,
                        "kind": "transition"})
            break

    for k in range(n):
        if used[k] or lengths[k] <= 0:
            continue
        a, b = segments[k]
        runs.append({"a": a, "b": b, "length": lengths[k], "offset": None, "kind": "single"})
        used[k] = True

    # rule 2 revisited, 25 Oct 2026: bridge a small gap between two
    # collinear, same-width "pair" runs into one continuous run — UNLESS
    # some OTHER segment on this same layer has an endpoint landing inside
    # that gap, which means a branch ties into the trunk exactly there (a
    # tee/cross junction, not a break in an otherwise continuous run).
    #
    # The original version of this idea bridged every such gap and was
    # rejected before shipping: on a real file, a trunk that takes
    # branches off BOTH sides at the same point reads as one continuous
    # run straight through its own body, silently adding the trunk's own
    # width (400-800mm per occurrence there) as if it were missing duct.
    # This version adds the real topology check that was missing — found
    # by reproducing that exact file (`hvac_tender_complex.dxf`,
    # M-DUCT-SUPPLY): every real gap in it has a crossing branch's
    # endpoint landing exactly inside the gap (confirmed at (13275, 16400)
    # and (13725, 15600), where two branches cross the trunk from opposite
    # sides at the same point) — so this check correctly leaves all of
    # them un-bridged, same as before this rule existed. A gap with
    # nothing tied into it — a trunk split into two LINE entities for no
    # topological reason — is bridged.
    def _axis(r):
        (ax, ay), (bx, by) = r["a"], r["b"]
        if abs(ay - by) < 1.0:
            return ("H", round(ay, 1))
        if abs(ax - bx) < 1.0:
            return ("V", round(ax, 1))
        return None

    bridged_away: set[int] = set()
    pair_idxs = [idx for idx, r in enumerate(runs) if r["kind"] == "pair"]
    for a_i in range(len(pair_idxs)):
        i_idx = pair_idxs[a_i]
        if i_idx in bridged_away:
            continue
        ri = runs[i_idx]
        axi = _axis(ri)
        if not axi:
            continue
        for b_i in range(len(pair_idxs)):
            j_idx = pair_idxs[b_i]
            if j_idx == i_idx or j_idx in bridged_away:
                continue
            rj = runs[j_idx]
            if _axis(rj) != axi:
                continue
            if abs(ri["offset"] - rj["offset"]) > 5.0:
                continue
            dim = 0 if axi[0] == "H" else 1
            a1, b1 = sorted((ri["a"][dim], ri["b"][dim]))
            a2, b2 = sorted((rj["a"][dim], rj["b"][dim]))
            if b1 >= a2:
                continue                       # rj is not to the "right" of ri
            gap = a2 - b1
            if not (1.0 < gap < 2000.0):
                continue
            y0 = axi[1]
            band = max(ri["offset"], rj["offset"], 5.0)
            # ri/rj's OWN boundary endpoints sit exactly at the gap's own
            # edges (b1, a2) -- excluded by raw segment index, not by
            # coordinate, because a genuine crossing branch can legitimately
            # land exactly on one of those same coordinates (confirmed on
            # the real file: the branch ties in exactly at the trunk pair's
            # own endpoint, not a little inside it).
            own = set(ri.get("seg_idxs", ())) | set(rj.get("seg_idxs", ()))
            branched = False
            for s_idx, (sa, sb) in enumerate(segments):
                if s_idx in own:
                    continue
                for pt in (sa, sb):
                    if (b1 - 1.0 <= pt[dim] <= a2 + 1.0
                            and abs(pt[1 - dim] - y0) < band):
                        branched = True
                        break
                if branched:
                    break
            if branched:
                continue                       # a real junction -- do not bridge
            pts = sorted((ri["a"], ri["b"], rj["a"], rj["b"]),
                        key=lambda p: p[dim])
            runs[i_idx] = {"a": pts[0], "b": pts[-1],
                          "length": ri["length"] + gap + rj["length"],
                          "offset": (ri["offset"] + rj["offset"]) / 2,
                          "kind": "bridged", "seg_idxs": tuple(own)}
            bridged_away.add(j_idx)
            ri = runs[i_idx]
            break

    if bridged_away:
        runs = [r for idx, r in enumerate(runs) if idx not in bridged_away]

    return runs


def _collapse_duct_pairs(segments: list) -> tuple[float, int, int, int]:
    """Thin wrapper over `_duct_runs()`: the route length of one layer's
    straight LINE segments, plus how many genuine pairs, tapering
    transitions and bridged branch-take-off gaps were found -- the caller
    turns each into a visible note, never a silent correction: these are
    geometric inferences, not measurements, and the module's whole promise
    is that nothing here is silently trusted.

    Returns (total_length, pairs_found, transitions_found, bridged_found)."""
    runs = _duct_runs(segments)
    total = sum(r["length"] for r in runs)
    pairs_found = sum(1 for r in runs if r["kind"] == "pair")
    transitions_found = sum(1 for r in runs if r["kind"] == "transition")
    bridged_found = sum(1 for r in runs if r["kind"] == "bridged")
    return total, pairs_found, transitions_found, bridged_found


# ── rule 3: duct/pipe sizes from the drawing's own TEXT labels ─────────────
# Found testing the duct-pairing fix above against an independent reading of
# the same real file: the fix gets the ROUTE length right, but a route
# length alone cannot be priced -- GI ductwork is billed in m² of sheet per
# size and gauge, not metres of centreline, and the size (e.g. "800x400")
# is written right there on the drawing as plain text, which this module
# never read at all. `_DUCT_SIZE_RE` / `_PIPE_SIZE_RE` find a size in any
# TEXT/MTEXT string; each label is matched to the nearest run by point-to-
# segment distance, then propagated to every other run in the same
# straight-line "chain" (same orientation, same line, same width) -- so a
# trunk broken into several LINE entities by branch take-offs still gets
# the one size label meant for the whole run, instead of the single
# nearest segment guessing wrong when two different labels are roughly
# equidistant from a segment in the middle.
_DUCT_SIZE_RE = re.compile(r"(\d{2,4})\s*[xX×]\s*(\d{2,4})")
_PIPE_SIZE_RE = re.compile(r"\bDN\s*(\d{1,4})\b", re.IGNORECASE)
_SIZE_LABEL_MAX_MM = 3000.0           # a label further than this from every
                                       # run on the layer is not describing it


def _duct_size_labels(texts: list[tuple[tuple[float, float], str]]
                      ) -> list[tuple[tuple[float, float], str, str]]:
    """(position, size string, "duct"|"pipe") for every TEXT/MTEXT entity
    that names a duct size ("800x400", spacing/case tolerant) or a pipe
    size ("DN80")."""
    out = []
    for pos, content in texts:
        m = _DUCT_SIZE_RE.search(content)
        if m:
            out.append((pos, f"{m.group(1)}x{m.group(2)}", "duct"))
            continue
        m = _PIPE_SIZE_RE.search(content)
        if m:
            out.append((pos, f"DN{m.group(1)}", "pipe"))
    return out


def _point_segment_distance(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return _dist(p, (ax + t * dx, ay + t * dy))


def _chain_key(run: dict, tol: float = 5.0):
    """Two runs share a key when they sit on the same straight line, the
    same duct/pipe width apart -- the signal used to propagate one size
    label across several LINE entities a branch take-off has split one
    run into. A diagonal (a transition) is never chained -- it belongs to
    neither side's label."""
    (ax, ay), (bx, by) = run["a"], run["b"]
    off = round((run["offset"] or 0.0) / tol) * tol
    if abs(ay - by) <= tol:
        return ("h", round(ay / tol) * tol, off)
    if abs(ax - bx) <= tol:
        return ("v", round(ax / tol) * tol, off)
    return ("d", id(run))


def _duct_sizes_for_layer(layer: str, segments: list,
                          labels: list[tuple[tuple[float, float], str, str]],
                          expected_kind: str
                          ) -> list[dict]:
    """One entry per distinct size found on this layer: {"layer", "size",
    "kind", "length"} (length in the drawing's own raw unit, same as
    lengths_by_layer). A run with no label close enough to trust is left
    out -- silence, not a guessed size.

    `expected_kind` ("duct" or "pipe", from the layer's own name) restricts
    matching to labels of that same kind only -- caught testing against a
    real file: a nearby "DN32" pipe label was the closest text to a duct
    run and got attached to it, producing a nonsensical "duct, DN32" entry.
    A duct layer is sized from duct-pattern text, a pipe layer from
    pipe-pattern text, never the other one just because it was closer."""
    same_kind = [l for l in labels if l[2] == expected_kind]
    if not same_kind:
        return []
    runs = [r for r in _duct_runs(segments) if r["kind"] in ("pair", "single", "bridged")]
    if not runs:
        return []
    chains: dict = {}
    for r in runs:
        chains.setdefault(_chain_key(r), []).append(r)

    out = []
    for members in chains.values():
        best = None
        for r in members:
            for pos, size, kind in same_kind:
                d = _point_segment_distance(pos, r["a"], r["b"])
                if best is None or d < best[0]:
                    best = (d, size, kind)
        if best is None or best[0] > _SIZE_LABEL_MAX_MM:
            continue
        _, size, kind = best
        out.append({"layer": layer, "size": size, "kind": kind,
                    "length": sum(r["length"] for r in members)})
    return out


_MM_PER_DRAWING_UNIT = {
    "millimeters": 1.0, "centimeters": 10.0, "meters": 1000.0,
    "inches": 25.4, "feet": 304.8,
}


def _duct_size_area(entry: dict, unit: str) -> dict:
    """Adds length_m and, for a duct (not a pipe), area_m2 -- sheet area,
    perimeter x length, the number a GI ductwork rate actually prices
    against. Only when the drawing's unit is one `measure()` can convert
    with confidence; left out otherwise rather than guessed."""
    to_mm = _MM_PER_DRAWING_UNIT.get(unit)
    if not to_mm:
        return entry
    length_m = entry["length"] * to_mm / 1000.0
    entry["length_m"] = round(length_m, 2)
    if entry["kind"] == "duct":
        m = _DUCT_SIZE_RE.search(entry["size"])
        if m:
            w, h = float(m.group(1)), float(m.group(2))
            perimeter_m = 2.0 * (w + h) / 1000.0
            entry["area_m2"] = round(perimeter_m * length_m, 2)
    return entry


# ── rule 4: a corner in a single-line run is an elbow ──────────────────────
# Safe by construction, unlike the branch-take-off gap this module
# deliberately does NOT bridge (see HVAC_DRAWING_RULES.md): it only ever
# looks at two single-line segments that already share an exact endpoint --
# a hard topological fact read straight off the coordinates, never a fuzzy
# proximity guess -- so it carries none of that one's false-positive risk
# of merging two unrelated runs that happen to sit nearby.

def _elbow_count(segments: list) -> int:
    singles = [r for r in _duct_runs(segments) if r["kind"] == "single"]
    seen: set = set()
    for i in range(len(singles)):
        ai, bi = singles[i]["a"], singles[i]["b"]
        di = _unit_dir(ai, bi)
        for j in range(i + 1, len(singles)):
            aj, bj = singles[j]["a"], singles[j]["b"]
            dj = _unit_dir(aj, bj)
            cross = abs(di[0] * dj[1] - di[1] * dj[0])
            if cross <= 0.3:                  # near-parallel -- a straight run, not a turn
                continue
            for p in (ai, bi):
                for q in (aj, bj):
                    if _dist(p, q) < 1.0:
                        seen.add((round(p[0]), round(p[1])))
    return len(seen)


# ── measurement ────────────────────────────────────────────────────────────

def measure(dxf_path: str, hvac: bool = False) -> dict:
    """Walk every entity in every layout's modelspace-equivalent geometry and
    return real, measured quantities — never estimated, never AI-guessed:

        {"unit": "meters", "unit_code": 6,
         "lengths_by_layer": {"WALLS": 142.7, ...},   # drawing units
         "areas_by_layer":   {"ROOM-HATCH": 88.4, ...},
         "block_counts":     {"DOOR-900": 12, "WINDOW-1200": 8, ...},
         "text_entities":    [((12.0, 4.5), "CAM-01"), ...],  # (x, y), content
         "layers": ["0", "WALLS", ...], "entity_count": 431}

    `hvac=True` additionally applies HVAC_DRAWING_RULES.md's rules (today:
    rule 1, _collapse_duct_pairs) — gated deliberately, the owner's own
    instruction (22 Oct 2026): these rules are trade-specific, verified
    against HVAC drawings specifically, and must not quietly start
    reading an electrical or plumbing drawing's lines differently just
    because they happen to look paired. The caller decides `hvac` from
    whether the person's own request actually says "HVAC" — see
    addons/boq/dialog.py:_measure().

    `text_entities` (added for BOQ-CHANGES-TODO.md item 65): every real
    TEXT/MTEXT this same walk already reads, position + content — this
    function has always collected it internally (rule 3 reads duct/pipe
    sizes out of it when hvac=True) but never handed it back, which is
    the only reason addons/boq/drawing_reader.py grew its own, SEPARATE
    ezdxf walk just to read a DXF's own text: the same file was being
    parsed twice, by two different modules, for two different slices of
    the same entities. Returning it here instead means every caller --
    the GUI's own auto-measure on attach, and the AI-recheck step, ELV's
    own drawing reading -- shares this ONE walk's answer, not three
    independent ones that can each see something slightly different.
    Additive only: every existing key keeps its exact previous meaning
    and value; no existing caller reads or is affected by this one.
    """
    doc, read_notes = _read_dxf(dxf_path)

    unit_code = doc.header.get("$INSUNITS", 0)
    unit = _UNIT_NAMES.get(unit_code, f"unspecified (code {unit_code})")

    lengths: dict[str, float] = {}
    areas: dict[str, float] = {}
    blocks: dict[str, int] = {}
    block_layers: dict[str, set] = {}
    seen_layers: set[str] = set()
    entity_count = 0
    # Raw LINE endpoints per layer, held back from `lengths` until every
    # LINE on that layer is in hand -- a double-line duct/pipe/tray run
    # (_collapse_duct_pairs) can only be told apart from a genuine single
    # line by comparing it against its layer's OTHER lines, never one at a
    # time as each is read off the drawing.
    line_segments: dict[str, list] = {}
    # For the site facts (_site_facts): where each block sits and at what
    # scale, and each layer's extent, piece count and whether it closes.
    block_pos: dict[str, list] = {}
    block_scales: dict[str, list] = {}
    layer_geom: dict[str, dict] = {}
    # Every TEXT/MTEXT on the drawing, position + content -- rule 3 reads
    # duct/pipe sizes out of these when hvac=True; always collected (cheap)
    # so the gate lives in one place, where it's used, not here too.
    text_entities: list[tuple[tuple[float, float], str]] = []

    def extend(layer: str, pts, closed: bool):
        g = layer_geom.setdefault(layer, {"minx": 1e300, "miny": 1e300,
                                          "maxx": -1e300, "maxy": -1e300,
                                          "pieces": 0, "closed": 0})
        for x, y in pts:
            if x < g["minx"]: g["minx"] = x
            if y < g["miny"]: g["miny"] = y
            if x > g["maxx"]: g["maxx"] = x
            if y > g["maxy"]: g["maxy"] = y
        g["pieces"] += 1
        g["closed"] += 1 if closed else 0

    def add(d: dict, key: str, value: float):
        if value:
            d[key] = d.get(key, 0.0) + value

    msp = doc.modelspace()
    for e in msp:
        entity_count += 1
        t = e.dxftype()
        layer = e.dxf.layer
        seen_layers.add(layer)
        try:
            if t == "LINE":
                a, b = (e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)
                line_segments.setdefault(layer, []).append((a, b))
                extend(layer, (a, b), False)
            elif t == "LWPOLYLINE":
                add(lengths, layer, _lwpolyline_length(e))
                add(areas, layer, _lwpolyline_area(e))
                extend(layer, [(v[0], v[1]) for v in e.get_points("xy")],
                       bool(e.closed))
            elif t == "POLYLINE":
                add(lengths, layer, _legacy_polyline_length(e))
                add(areas, layer, _legacy_polyline_area(e))
            elif t == "ARC":
                add(lengths, layer, _arc_length(e))
            elif t == "HATCH":
                add(areas, layer, _hatch_area(e))
            elif t in ("TEXT", "MTEXT"):
                content = e.dxf.text if t == "TEXT" else e.text
                if content:
                    text_entities.append(
                        ((float(e.dxf.insert.x), float(e.dxf.insert.y)), str(content)))
            elif t == "INSERT":
                name = e.dxf.name
                if not name.startswith("*"):   # anonymous/system blocks
                    blocks[name] = blocks.get(name, 0) + 1
                    # The LAYER a block sits on is usually the draughtsman's
                    # own plain-English name for it ("EP" on layer "Electric
                    # Pole"), so the drawing carries its own legend — no
                    # screenshot or hand-typed mapping needed, and it beats
                    # both: in a real client file this proved the user's
                    # hand-written legend wrong (they had HP = high tension
                    # pole; HP actually sits on "Hand Pump", HT on "High
                    # Tension Pole").
                    block_layers.setdefault(name, set()).add(layer)
                    pos = block_pos.setdefault(name, [])
                    if len(pos) < 500:
                        pos.append((float(e.dxf.insert.x), float(e.dxf.insert.y)))
                    block_scales.setdefault(name, []).append(float(e.dxf.xscale))
        except Exception:
            # One malformed entity (a stray null vertex, a degenerate arc)
            # must not sink the whole takeoff — skip it, keep going.
            continue

    duct_sizes: list[dict] = []
    size_labels = _duct_size_labels(text_entities) if hvac else []
    for layer, segments in line_segments.items():
        # Rule 3 only ever looks at a layer that names itself duct or pipe
        # work ("M-DUCT-SUPPLY", "M-PIPE-CHW-S") -- caught testing against
        # the real file: proximity alone wrongly matched A-WALL (the
        # architectural perimeter, which naturally runs right past the
        # ductwork's own size labels) to "800x400 SA". A layer's own name
        # is the one signal this whole module already trusts elsewhere
        # (block_layers' legend -- see measure()'s own comment on INSERT).
        expected_kind = ("duct" if "duct" in layer.lower()
                         else "pipe" if "pipe" in layer.lower() else "")
        if hvac:
            total, pair_count, transition_count, bridged_count = _collapse_duct_pairs(segments)
        else:
            # The plain, original sum -- every other trade's drawings keep
            # measuring exactly as they always did; HVAC_DRAWING_RULES.md's
            # rules only ever apply when `hvac` says so.
            total = sum(_dist(a, b) for a, b in segments)
            pair_count = transition_count = bridged_count = 0
        add(lengths, layer, total)
        if pair_count:
            read_notes.append(
                f"{layer}: read {pair_count} pair(s) of lines as one run's "
                "two edges, not two separate runs (a duct/pipe/tray drawn "
                "to its real width) — verify before pricing.")
        if transition_count:
            read_notes.append(
                f"{layer}: read {transition_count} tapering transition(s) "
                "(e.g. a reducer) as one length each, not both converging "
                "sides — HVAC_DRAWING_RULES.md rule 2, verify before pricing.")
        if bridged_count:
            read_notes.append(
                f"{layer}: bridged {bridged_count} gap(s) between collinear "
                "same-width duct lines into one continuous run — no branch "
                "ties in at that point, so read as the trunk continuing "
                "through, not a break; HVAC_DRAWING_RULES.md rule 2, verify "
                "before pricing.")
        if hvac and expected_kind:
            for entry in _duct_sizes_for_layer(layer, segments, size_labels, expected_kind):
                duct_sizes.append(_duct_size_area(entry, unit))
            elbows = _elbow_count(segments)
            if elbows:
                read_notes.append(
                    f"{layer}: {elbows} corner(s) found in the single-line "
                    "run(s) — read as elbow fittings, not route length; "
                    "HVAC_DRAWING_RULES.md rule 4.")
    if hvac and size_labels and not duct_sizes:
        read_notes.append(
            "Duct/pipe size text was found on the drawing but none of it "
            "sat close enough to a measured run to trust — sizes left out "
            "rather than guessed; HVAC_DRAWING_RULES.md rule 3.")

    try:
        site = _site_facts(unit_code, lengths, layer_geom, block_pos, block_scales)
    except Exception as exc:                                # noqa: BLE001
        site = {"error": str(exc)}          # facts are a bonus, never a blocker

    return {
        "unit": unit, "unit_code": unit_code, "unit_confirmed": unit_code != 0,
        "notes": read_notes,
        "site": site,
        "lengths_by_layer": dict(sorted(lengths.items())),
        "areas_by_layer": dict(sorted(areas.items())),
        "block_counts": dict(sorted(blocks.items())),
        "block_layers": {k: sorted(v) for k, v in sorted(block_layers.items())},
        # HVAC_DRAWING_RULES.md rule 3: one entry per distinct duct/pipe
        # size actually found and trusted, empty when hvac=False or no
        # size text was readable. Additive -- lengths_by_layer is
        # untouched and stays the single per-layer total it always was.
        "duct_sizes": duct_sizes,
        # item 65: the raw TEXT/MTEXT this walk already collected (see this
        # function's own docstring) -- every caller's one shared source for
        # a DXF's own real text, instead of each re-parsing the file itself.
        "text_entities": text_entities,
        # From the entities actually walked, NOT doc.layers.entries — a
        # minimal-mode DWG→DXF conversion can leave the formal layer TABLE
        # nearly empty (confirmed: 2 table entries vs. 64 real layer names
        # in an actual client file) while every entity still correctly
        # reports its own layer. The table is not a reliable source here.
        "layers": sorted(seen_layers),
        "entity_count": entity_count,
    }


# ── what the coordinates say ─────────────────────────────────────────────────
# Three things a person reading the drawing would settle in a minute, and
# that a table of totals cannot: what unit the drawing is in when the file
# does not say, whether two similar-looking layers are one feature or two,
# and which structures are nowhere near the others. All three came up on
# the same site survey on 2026-09-10 -- the unit was flagged "unconfirmed"
# in every BOQ written from it, one estimator dropped half the perimeter as
# a duplicate layer, and six of seven pump houses sat kilometres away on a
# pipeline and were counted as if they were on the campus network. Every
# one of those is answerable from entity coordinates, locally.

_SIZE_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*(MM|M|MTR|CM|FT)\b", re.IGNORECASE)
REMOTE_M = 1000.0            # a block this far from the rest is another site


def _median(values):
    vals = sorted(values)
    return vals[len(vals) // 2] if vals else 0.0


def _site_facts(unit_code: int, lengths: dict, layer_geom: dict,
                block_pos: dict, block_scales: dict) -> dict:
    """Unit inference, look-alike layers, and remote structures -- from the
    coordinates. Pure arithmetic over what measure() already walked."""
    import difflib
    out: dict = {"unit_inferred": "", "unit_evidence": [], "lookalikes": [],
                 "remote_blocks": [], "distances": {}, "extent": None}

    points = [p for pts in block_pos.values() for p in pts]
    if points:
        med = _median([abs(x) for x, _ in points] + [abs(y) for _, y in points])
        if 100_000 <= med <= 20_000_000:
            out["unit_evidence"].append(
                "coordinates sit on a projected survey grid (eastings and "
                "northings in the hundreds of thousands), which is metres")
    for name, scales in block_scales.items():
        m = _SIZE_IN_NAME.search(name)
        if not m or not scales:
            continue
        named = m.group(2).upper()
        scale = _median(scales)
        # "About 0.001", not exactly: a 7 m gate block drawn 6,125 mm wide
        # is placed at 0.00114 to come out at 7 m -- the scale is the
        # draughtsman's fit, the order of magnitude is the fact.
        if named in ("M", "MTR") and 0.0005 <= scale <= 0.002:
            out["unit_evidence"].append(
                f"block {name} is inserted at scale {scale:.4g} — drawn in "
                f"millimetres, placed in metres")
        elif named in ("M", "MTR") and abs(scale - 1.0) < 1e-6:
            out["unit_evidence"].append(
                f"block {name} is inserted at scale 1 and is named in metres")
        elif named == "MM" and abs(scale - 1.0) < 1e-6:
            out["unit_evidence"].append(
                f"block {name} is inserted at scale 1 and is named in "
                f"millimetres — the drawing unit is millimetres")
    if unit_code == 0 and out["unit_evidence"]:
        says_mm = any("is millimetres" in ev for ev in out["unit_evidence"])
        out["unit_inferred"] = "millimeters" if says_mm else "meters"

    names = [n for n in lengths if n in layer_geom and layer_geom[n]["pieces"]]

    def norm(n):
        return re.sub(r"[^a-z0-9]", "", n.lower())

    def box(g):
        return (g["minx"], g["miny"], g["maxx"], g["maxy"])

    def iou(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / ua if ua > 0 else 0.0

    # Only pairs a person would also pause on: names that read as the same
    # word (a misspelt or re-spaced duplicate), or footprints that all but
    # coincide. Overlap alone is not enough -- a road, its centre line and
    # its drain share an extent and are three different things.
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            similar = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= 0.8
            ga, gb = layer_geom[a], layer_geom[b]
            overlap = iou(box(ga), box(gb))
            if not similar and overlap < 0.9:
                continue
            ux = (min(ga["minx"], gb["minx"]), min(ga["miny"], gb["miny"]),
                  max(ga["maxx"], gb["maxx"]), max(ga["maxy"], gb["maxy"]))
            box_perim = 2 * ((ux[2] - ux[0]) + (ux[3] - ux[1]))
            combined = lengths[a] + lengths[b]
            closed = ga["closed"] + gb["closed"]
            if closed == 0 and box_perim and 0.85 <= combined / box_perim <= 1.15:
                verdict = ("together they form one closed run around the same "
                           "footprint — one feature drawn on two layers; count "
                           "both, once each")
            elif overlap >= 0.8 and abs(lengths[a] - lengths[b]) <= 0.15 * max(lengths[a], lengths[b]):
                verdict = ("same extent and near-identical length — probably "
                           "the same feature drawn twice; count once")
            elif similar:
                verdict = ("overlapping extents — check which is current before "
                           "counting either")
            else:
                continue            # merely co-located, not look-alike
            out["lookalikes"].append({
                "layers": [a, b], "lengths": [round(lengths[a], 2), round(lengths[b], 2)],
                "combined": round(combined, 2), "box_perimeter": round(box_perim, 2),
                "verdict": verdict})

    if points:
        cx, cy = _median([x for x, _ in points]), _median([y for _, y in points])
        near = [p for p in points if math.hypot(p[0] - cx, p[1] - cy) <= REMOTE_M]
        if near:
            xs = [p[0] for p in near]; ys = [p[1] for p in near]
            out["extent"] = {"width": round(max(xs) - min(xs), 1),
                             "height": round(max(ys) - min(ys), 1)}
        for name, pts in block_pos.items():
            ds = sorted(round(math.hypot(x - cx, y - cy)) for x, y in pts)
            out["distances"][name] = ds[:60]
            far = [d for d in ds if d > REMOTE_M]
            if far:
                out["remote_blocks"].append({
                    "name": name, "count": len(far), "of": len(ds),
                    "distances_m": far[:10]})
    return out


def site_facts_text(q: dict) -> list[str]:
    """The coordinate facts, as lines for the summary and the screen."""
    site = q.get("site") or {}
    if not site or site.get("error"):
        return []
    lines = []
    if site.get("unit_inferred") and not q.get("unit_confirmed", True):
        lines.append(f"UNIT INFERRED FROM THE COORDINATES: {site['unit_inferred'].upper()} — "
                     + "; ".join(site["unit_evidence"]) + ". Treat the figures "
                     "below as that unit unless the source drawing says otherwise.")
    for lk in site.get("lookalikes") or []:
        a, b = lk["layers"]
        lines.append(f"LOOK-ALIKE LAYERS: '{a}' ({lk['lengths'][0]:.2f}) and '{b}' "
                     f"({lk['lengths'][1]:.2f}) — {lk['verdict']} (combined "
                     f"{lk['combined']:.2f}; their footprint's perimeter is "
                     f"{lk['box_perimeter']:.2f}).")
    ext = site.get("extent")
    if ext:
        lines.append(f"SITE EXTENT (main cluster of blocks): about {ext['width']:.0f} x "
                     f"{ext['height']:.0f} drawing units.")
    for rb in site.get("remote_blocks") or []:
        lines.append(f"REMOTE: {rb['count']} of {rb['of']} '{rb['name']}' blocks sit "
                     f"{', '.join(str(d) for d in rb['distances_m'])} units from the "
                     f"main site — another location; do not put them on the site's "
                     f"cable network without saying so.")
    return lines


def apply_known_unit(q: dict, unit_name: str) -> dict:
    """The user knows the real unit even though the file didn't say so (or
    conversion dropped it) — take their word for it instead of flagging
    every quantity as unconfirmed. Mutates and returns q."""
    q["unit"] = unit_name.strip().lower()
    q["unit_confirmed"] = True
    return q


def filter_by_keywords(q: dict, keywords: list[str]) -> dict:
    """Keep only layers/blocks whose name contains any of `keywords`
    (case-insensitive substring match) — a real, deterministic scope filter,
    not a hope that the formatting agent will ignore irrelevant trades on
    its own. Confirmed necessary: without this, a mixed civil+CCTV drawing's
    full measured dump left the formatting agent to guess which layers
    belonged to the requested scope, and it guessed wrong (defaulted to
    civil). Returns a new dict; the original is untouched."""
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return q

    def matches(name: str) -> bool:
        low = name.lower()
        return any(k in low for k in kws)

    out = dict(q)
    out["lengths_by_layer"] = {k: v for k, v in q["lengths_by_layer"].items() if matches(k)}
    out["areas_by_layer"] = {k: v for k, v in q["areas_by_layer"].items() if matches(k)}
    out["block_counts"] = {k: v for k, v in q["block_counts"].items() if matches(k)}
    out["scope_keywords"] = kws
    out["scope_total_layers"] = len(q["layers"])
    return out


# ── output: the raw, auditable quantities CSV ──────────────────────────────

def write_quantities_csv(q: dict, path: str) -> None:
    """The measured numbers, exactly as computed — before any AI touches
    them. Openable in Excel, and the thing to sanity-check the eventual
    formatted BOQ against."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "name", "quantity", "unit"])
        for layer, length in q["lengths_by_layer"].items():
            w.writerow(["length", layer, f"{length:.2f}", q["unit"]])
        for layer, area in q["areas_by_layer"].items():
            w.writerow(["area", layer, f"{area:.2f}", f"square {q['unit']}"])
        for name, count in q["block_counts"].items():
            w.writerow(["count", name, count, "nos"])
        # HVAC_DRAWING_RULES.md rule 3: one row per distinct duct/pipe size
        # actually found and trusted, with its own sheet area when it's a
        # duct -- the number a GI ductwork rate prices against, not just
        # the plain route length above.
        for entry in (q.get("duct_sizes") or []):
            name = f"{entry['layer']} — {entry['size']}"
            if entry["kind"] == "duct" and "area_m2" in entry:
                w.writerow(["duct size", name, f"{entry['area_m2']:.2f}", "sq m"])
            elif "length_m" in entry:
                w.writerow(["duct size", name, f"{entry['length_m']:.2f}", "m"])
        # A rule (HVAC_DRAWING_RULES.md rule 1, today) inferred rather than
        # purely measured a number -- stays in the file this re-opens as,
        # not only the live app's own screen, so it survives being emailed
        # or filed away.
        for text in (q.get("notes") or []):
            w.writerow(["note", "", text, ""])


def summary_text(q: dict) -> str:
    """Plain-text quantities summary — small enough to embed directly in an
    AI prompt (no file upload needed), and what the terminal prints too."""
    lines = [f"Drawing units: {q['unit']}  ·  {q['entity_count']} entities  ·  "
             f"{len(q['layers'])} layer(s)"]
    if not q.get("unit_confirmed", q["unit_code"] != 0):
        lines.append(
            "⚠ UNIT NOT CONFIRMED — this drawing's $INSUNITS wasn't set or "
            "wasn't preserved through conversion. Every number below is in "
            "whatever raw drawing units were used (could be mm, m, or ft) — "
            "confirm the real unit against the source drawing before this "
            "goes anywhere near a client or a rate.")
    if "scope_keywords" in q:
        shown = len(q["lengths_by_layer"]) + len(q["areas_by_layer"]) + len(q["block_counts"])
        # No literal '[...]' here — this text is also shown in a Rich-markup
        # terminal panel, which parses square brackets as style tags and
        # silently swallows whatever's inside them.
        lines.append(
            f'SCOPE FILTER applied: matching "{", ".join(q["scope_keywords"])}" — '
            f"showing items from a subset of the drawing's {q['scope_total_layers']} "
            "layer(s), NOT the whole file. Everything below is in scope; nothing "
            "outside it should appear in the BOQ.")
        if shown == 0:
            lines.append(
                "⚠ NOTHING MATCHED that scope filter — check the keywords against "
                "the actual layer/block names in this drawing (see full layer list "
                "below) before assuming the scope truly has zero quantity.")
    for note in q.get("notes") or []:
        lines.append(f"⚠ {note}")
    lines += site_facts_text(q)
    lines.append("")
    if q["lengths_by_layer"]:
        lines.append("LENGTHS BY LAYER:")
        lines += [f"  {layer}: {length:.2f} {q['unit']}"
                  for layer, length in q["lengths_by_layer"].items()]
        lines.append("")
    if q.get("duct_sizes"):
        # HVAC_DRAWING_RULES.md rule 3: real sizes read off the drawing's
        # own TEXT, per duct/pipe run -- use these directly; the length
        # above is the same run's plain total, this is the size split.
        # Several branch take-offs on one layer can share one size, each
        # its own short "chain" -- aggregated here (layer, size, kind) so
        # the writer sees one line per size, not a dozen near-duplicates.
        agg: dict[tuple[str, str, str], dict] = {}
        for entry in q["duct_sizes"]:
            key = (entry["layer"], entry["size"], entry["kind"])
            bucket = agg.setdefault(key, {"length_m": 0.0, "area_m2": 0.0})
            bucket["length_m"] += entry.get("length_m", entry["length"])
            bucket["area_m2"] += entry.get("area_m2", 0.0)
        lines.append("DUCT/PIPE SIZES (read from the drawing's own size "
                     "text — use these real sizes directly in the BOQ, do "
                     "not mark them 'TBC — to HVAC schedule'):")
        for (layer, size, kind), bucket in agg.items():
            if kind == "duct" and bucket["area_m2"]:
                lines.append(f"  {layer} {size}: {bucket['length_m']:.2f} m "
                             f"run, {bucket['area_m2']:.2f} m² sheet area")
            else:
                lines.append(f"  {layer} {size}: {bucket['length_m']:.2f} m")
        lines.append("")
    if q["areas_by_layer"]:
        lines.append("AREAS BY LAYER:")
        lines += [f"  {layer}: {area:.2f} square {q['unit']}"
                  for layer, area in q["areas_by_layer"].items()]
        lines.append("")
    if q["block_counts"]:
        lines.append("BLOCK COUNTS (poles, fixtures, doors — whatever was drawn "
                     "as a block). The layer each block sits on is the "
                     "draughtsman's own name for it, so it doubles as the "
                     "drawing's built-in legend:")
        for name, count in q["block_counts"].items():
            lyrs = (q.get("block_layers") or {}).get(name) or []
            # Parentheses, not brackets — this string is also rendered in a
            # Rich terminal panel, which eats "[...]" as a style tag.
            hint = f"  (drawn on layer: {', '.join(lyrs)})" if lyrs else ""
            lines.append(f"  {name}: {count}{hint}")
    if q.get("text_entities"):
        # item 65: real TEXT/MTEXT, not a block and not a measured line --
        # schedule notes, device tags, legend text. Was read internally by
        # this same walk all along (rule 3's own duct-size matching) but
        # never shown here; every caller of summary_text() now gets it for
        # free, no new consent, no AI, regardless of industry.
        from collections import Counter
        texts = Counter(content for _, content in q["text_entities"])
        lines.append("")
        lines.append("TEXT ON THE DRAWING (every real TEXT/MTEXT entity read "
                     "directly off the drawing, not estimated — schedule "
                     "notes, tags, labels not already counted as a block "
                     "above):")
        for content, count in texts.items():
            suffix = f"  (×{count})" if count > 1 else ""
            lines.append(f"  {content}{suffix}")
    if not (q["lengths_by_layer"] or q["areas_by_layer"] or q["block_counts"]):
        lines.append("No measurable LINE/POLYLINE/HATCH/INSERT geometry found — "
                     "the drawing may use 3D solids or an unsupported entity type.")
    # Always list every layer name, not only when a scope filter is on. A
    # layer can exist with NO measurable geometry — "Electric Pole" and
    # "Hand Pump" in a real client drawing hold only block inserts, which
    # are counted above under their BLOCK name (EP, HP), never their layer.
    # Without this list a reader can't tell "that trade isn't in the file"
    # from "that trade is here but drawn as blocks", which is exactly the
    # distinction the interpretation stage is asked to report on.
    total = q.get("scope_total_layers", len(q["layers"]))
    if q["layers"]:
        lines.append("")
        lines.append(f"(All {total} layer names in the source drawing, for "
                     f"reference — some hold no measurable geometry: "
                     f"{', '.join(q['layers'])})")
    return "\n".join(lines)


_TEMPLATE_EXTS = (".docx", ".doc", ".xlsx", ".xls", ".csv", ".ods", ".pdf")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")

# A PDF with at least this much extractable text MIGHT be a real document
# (a sample BOQ, a spec) rather than a scanned/vector drawing with no text
# layer -- "might", because raw text alone isn't enough, see
# _looks_like_boq_document below. Not zero: a scan can carry a stray OCR
# layer or a title-block watermark and still have nothing a sample-BOQ read
# would use.
# Found live, 2 Nov 2026: a hand-sketched SS locker fabrication drawing
# (970x450x2000mm, qty 40, dimensions all clearly labelled -- nothing to
# guess) was filed here as a "sample BOQ" purely because it was a .pdf, with
# zero extractable text and a title that said "drawing" -- the writer stage
# correctly refused to invent a BOQ from a document it couldn't read, and
# "Make with drawing" (addons/boq/dialog.py's own explicit, consent-gated
# path for exactly this case) never turned on, because cad_path was never
# set for anything but .dwg/.dxf.
_PDF_TEXT_MIN_CHARS = 40

# Found live the very next day, 3 Nov 2026, on a DIFFERENT real client file:
# a real, proper CAD-exported (not scanned) 10-page PDF of pharma trolleys
# and storage racks -- real vector text, 2,842 characters total, comfortably
# over _PDF_TEXT_MIN_CHARS above, so the fix above filed it as a "sample
# BOQ" too, for the opposite reason: not too little text, just the WRONG
# KIND -- dimension numbers and one-line material/capacity callouts
# ("900\n1850…", "Material - SS", "Weight Capacity - 30 Kg", "16 SWG Top"),
# none of it reading anything like a BOQ document. "Has text" was never the
# real question; "reads like a BOQ" is. A real BOQ -- any trade, any firm's
# own template, confirmed against every sample BOQ excerpt read in this
# codebase's own session history -- names its own column set and section
# shape somewhere in its text; a drawing's extracted text, scanned or real
# vector, does not, no matter how many characters it has. Two distinct hits
# required, not one: a drawing's own title block can genuinely say "section"
# or "qty" once in passing without being a BOQ.
_BOQ_DOCUMENT_SIGNALS = (
    "bill of quantities", "bill of quantity", "description", "quantity",
    "qty", "rate", "amount", "preamble", "sr.no", "sr no", "sr. no",
    "item no", "unit rate", "provisional sum", "to confirm",
)


def _looks_like_boq_document(text: str) -> bool:
    """True if `text` (already lowercased by the caller) reads like an
    actual BOQ/estimate document -- at least two distinct signal terms,
    not merely some text existing at all. See _BOQ_DOCUMENT_SIGNALS."""
    return sum(1 for s in _BOQ_DOCUMENT_SIGNALS if s in text) >= 2


def classify_inputs(attachments: list[dict]):
    """Sort what the user attached by the ROLE it plays in a BOQ run:
    (cad, templates, images, notes). A screenshot of the drawing's legend
    sheet is not a template and must not be described to the AI as one —
    they carry completely different instructions.

    A .pdf is ambiguous by extension alone -- a sample BOQ and a scanned/
    exported drawing are both routinely delivered as PDF, and a real
    (non-scanned) drawing export can carry plenty of extracted text that
    still is not a BOQ document (dimension labels, material callouts) --
    see _looks_like_boq_document. Disambiguated by content, using the text
    CB.files.attach() already extracted (no extra I/O here): read as a BOQ
    document, it's a template; anything else -- too little text, or real
    text that doesn't read as one -- is treated as a drawing with no vector
    geometry to measure (cad), same bucket .dwg/.dxf go in, so "Make with
    drawing" sees it and the automatic .dwg/.dxf measurement step is what
    skips it (see addons/boq/dialog.py's _absorb(), which only measures a
    .dwg/.dxf cad_path -- there is no deterministic measurement for a PDF,
    only the AI actually looking at it, by explicit choice)."""
    cad, templates, images, notes = [], [], [], []
    for a in attachments:
        ext = os.path.splitext(a.get("path", ""))[1].lower()
        if ext in (".dwg", ".dxf"):
            cad.append(a)
        elif ext == ".pdf":
            text = (a.get("text") or "").strip()
            if len(text) >= _PDF_TEXT_MIN_CHARS and _looks_like_boq_document(text.lower()):
                templates.append(a)
            else:
                cad.append(a)
        elif ext in _IMAGE_EXTS:
            images.append(a)
        elif ext in _TEMPLATE_EXTS:
            templates.append(a)
        else:
            notes.append(a)
    return cad, templates, images, notes


def roles_text(cad, templates, images, notes) -> str:
    """One line per attached file saying what it IS — so the reading agent
    doesn't have to guess whether a .docx is a template or a spec."""
    lines = []
    for a in cad:
        lines.append(f"  · {a['name']} — the CAD drawing itself (source of the measured numbers below)")
    for a in images:
        lines.append(f"  · {a['name']} — a screenshot/image of the drawing "
                     "(read any legend, key, title block, notes or symbols visible in it)")
    for a in templates:
        lines.append(f"  · {a['name']} — an example BOQ showing the firm's usual style")
    for a in notes:
        lines.append(f"  · {a['name']} — background/requirements written by the user")
    return "\n".join(lines)


def standards_prompt(user_request: str, project_context: str = "",
                     measured_text: str = "") -> str:
    """The RESEARCH stage: the design norms a quantity surveyor would look up
    before estimating a trade that hasn't been drawn yet.

    This is the one part of a BOQ run that genuinely needs live web search
    and needs no CAD file at all — which makes it the natural first stage of
    a real pipeline rather than piling everything onto the writer. Camera
    spacing, max cable runs, containment conventions and the applicable
    standards are exactly what turns a derived quantity from a guess into a
    defensible assumption.

    `measured_text`, when the drawing was measured, is the list of components
    actually present. It is passed NOT for the researcher to quote back, but
    to TARGET the research: norms for the real parts (a magnetic separator,
    idler rollers, specific bolt grades) beat generic ones. Without it the
    research is a guess at the trade from the request text alone."""
    where = f" Project context: {project_context}." if project_context.strip() else ""
    # The component/layer names the drawing actually contains, so the norms
    # looked up are for THESE parts. Names carry the signal (what to research);
    # the researcher is told plainly not to echo the quantities or write a BOQ.
    measured_block = (
        "\n\nThe drawing HAS already been measured (a later stage owns the "
        "actual numbers). Use the component / layer names below ONLY to decide "
        "WHICH standards, standard sizes, material grades and rate bases to "
        "research — for these specific parts, not generic ones. Do NOT quote "
        "these quantities back, describe the site, or write a BOQ:\n"
        f"{measured_text.strip()}"
    ) if measured_text.strip() else ""
    return (
        "You are the RESEARCH stage of a Bill-of-Quantities pipeline. Your "
        "ONLY task is to set out the CURRENT STANDARD DESIGN NORMS a "
        "quantity surveyor or services estimator would apply when sizing "
        f"and estimating this work: {user_request}.{where}"
        "\n\nDo NOT write a BOQ, do not invent site quantities, and do not "
        "describe this specific site. Give the general engineering rules of "
        "thumb and standards that a later stage will apply to real measured "
        "site dimensions."
        f"{measured_block}"
        "\n\nCover, with SPECIFIC NUMBERS wherever they exist:"
        "\n  · typical spacing / coverage per device (e.g. metres between "
        "perimeter cameras, effective IR range, lux and lens guidance)"
        "\n  · cable types and their limits (e.g. max copper run before a "
        "repeater/fibre is required, when armoured or outdoor-rated is "
        "mandatory, indicative fibre core counts for a backbone)"
        "\n  · containment and civil allowances (conduit sizing, trench "
        "depth for buried duct, slack/service-loop percentage to add)"
        "\n  · termination, power and redundancy conventions (PoE budget, "
        "field switch/junction points per run, UPS backup expectations)"
        "\n  · the applicable standards or codes by name/number where they "
        "genuinely apply (Indian IS/NBC, TIA/ISO cabling, IEC), plus common "
        "Indian market practice if it differs from the written standard"
        "\n\nFormat it as a short, dense checklist of stated rules — each "
        "line usable as a design assumption a later stage can cite verbatim. "
        "Flag anything that is genuinely a judgement call rather than a norm."
        + _SK.addendum("boq.standards")
    )


def interpretation_prompt(user_request: str, quantities_text: str,
                          files_text: str, legend_hint: str = "") -> str:
    """The FIRST stage of a /boq run: work out what the drawing's codes mean
    and which of them the user actually wants, instead of making the user
    type `scope:` and `legend:` by hand. Deliberately forbidden from writing
    the BOQ or inventing quantities — it only interprets, and the stage
    after it does the writing."""
    hint = (f"\n\nThe user also supplied this partial legend — treat it as "
            f"authoritative and extend it:\n{legend_hint}") if legend_hint.strip() else ""
    return (
        "You are the INTERPRETATION stage of a Bill-of-Quantities pipeline. "
        "Your ONLY job is to work out what the drawing's layer/block codes "
        "MEAN and WHICH of them the user's request actually covers. Do NOT "
        "write the BOQ — the next AI does that. Do NOT invent, estimate or "
        "recalculate any quantity: the numbers below were measured directly "
        "from the drawing's geometry and are the only real ones that exist."
        f"\n\nWHAT THE USER ASKED FOR:\n{user_request}"
        f"\n\nFILES ATTACHED TO THIS MESSAGE:\n{files_text}{hint}"
        "\n\nRead the attached image(s) carefully — a drawing's legend/key, "
        "title block, scale and notes are usually printed on the sheet, and "
        "that is normally where abbreviations like EP / HT / BOR are "
        "explained."
        "\n\nIf an example BOQ is attached, it is your PRIMARY guide to scope: "
        "the trades and item types it bills for are what this firm actually "
        "quotes, so treat those as the scope of this BOQ unless the user's "
        "request above clearly says otherwise (the user's own words always "
        "win). It also shows you the item granularity and description style "
        "they expect."
        "\n\nReply with EXACTLY these four sections and nothing else:"
        "\n\n1. LEGEND — every layer/block code appearing in the measured "
        "data below that you can identify, as `CODE = plain-English meaning`. "
        "Say 'unidentified' for the ones you genuinely cannot work out; do "
        "not guess a meaning that isn't supported by the drawing or files."
        "\n\n2. IN SCOPE — the layer/block names (copied EXACTLY as spelled "
        "in the measured data) that belong in the BOQ the user asked for, "
        "each with a one-line reason. Leave out anything that is drafting "
        "furniture rather than built work (grids, title blocks, dimensions, "
        "text, revision marks, coordinates, level marks)."
        "\n\n3. PROJECT FACTS — client/project name, location, drawing "
        "scale, units, revision/date — only if you can actually see them in "
        "the attached files. Write 'not stated' otherwise."
        "\n\n4. GAPS — anything the user asked for that the measured data "
        "does NOT contain (e.g. they asked for cabling but no cable/conduit "
        "geometry exists in this drawing). Be explicit and specific: this "
        "warning is more useful to the user than a padded BOQ."
        + _SK.addendum("boq.interpret")
        + f"\n\nMEASURED QUANTITIES (ground truth, every layer in the drawing):\n{quantities_text}"
    )


def formatting_prompt(quantities_text: str, project_context: str = "",
                      has_template: bool = False, legend: str = "",
                      scoped: bool = False, brief_text: str = "",
                      allow_derived: bool = True, has_cad: bool = True,
                      standards_text: str = "", industry_note: str = "",
                      derivation_example: str = "") -> str:
    """`allow_derived` is the difference between a takeoff and an estimate.

    `industry_note` and `derivation_example` keep this function itself
    industry-agnostic (found live, 31 Oct 2026: the owner's own standing
    rule is that a trade-specific write-up rule must live apart and fire
    only when that trade is named -- the HVAC rules this function used to
    carry inline, gated on a bare `hvac: bool`, were already breaking that
    rule, just one `if` away from leaking into every other trade's own
    BOQ). The CALLER supplies both, already resolved for whatever industry
    it is writing for (addons/boq/industries.py owns that text on the app
    side -- this engine-side function never hardcodes a single industry's
    wording). Empty strings reproduce this function's old ELV/general
    default behaviour exactly, so every existing caller that passes
    neither keeps working unchanged.

    A survey/civil drawing legitimately contains no CCTV, cabling or fibre
    geometry — nobody has designed those yet. Forbidding un-measured line
    items outright (the original rule here) meant the writer could only
    refuse, which is useless when the whole job is "quote ELV works for this
    site". So derivation IS allowed — camera counts from boundary length,
    cable runs from road/perimeter lengths — but only with the design basis
    stated per section and a warning that it is a design-stage estimate, not
    a measured takeoff. Measured and derived must never be presented as the
    same kind of number."""
    context = f" Project context: {project_context}." if project_context.strip() else ""
    if has_template:
        structure = (
            "A SAMPLE BOQ IS ATTACHED. It is the firm's own document and it "
            "defines what you are making: read it first, and produce the "
            "same kind of document — its sections and trade breakdown, its "
            "column set, its numbering, the level of detail per line, how "
            "descriptions are worded, its units, its boilerplate (headers, "
            "notes, disclaimers). Where the sample makes a design decision "
            "for a comparable item (a spacing, an allowance, a cable "
            "specification, a containment size), use the same one here "
            "and say the sample is the basis. Adapt the CONTENT to what "
            "this drawing's measured data actually contains — do not copy "
            "the sample's rows or quantities — but keep its shape and "
            "conventions rather than inventing your own. Every quantity you "
            "present must trace back to the measured data below, and "
            "nothing measured should be silently dropped."
        )
    else:
        structure = (
            "Group items sensibly by trade/category (civil, doors & "
            "windows, finishes, etc. — infer from the layer/block names "
            "given). For each item include: item no., description, unit, "
            "and quantity (copied exactly from below)."
        )
    scope_rule = (
        " A SCOPE FILTER was already applied below — every item shown is in "
        "scope. Do not add items from other trades, and do not comment on "
        "trades that were filtered out."
    ) if scoped else ""
    # The brief is EMBEDDED, not referred to. Relying on the pipeline relay
    # to put it "above" produced a run where the writer was told to follow a
    # brief that was empty (a failed scrape) and another where it arrived
    # garbled. If there's a brief, it's in this string or it doesn't exist.
    brief_block = (
        "\n\nINTERPRETATION BRIEF from the reading stage — use its legend for "
        "descriptions and its scope list to decide what appears:\n"
        f"{brief_text.strip()}"
    ) if brief_text.strip() else ""
    # The drawing itself is NOT attached -- the same rule Gerber and STEP
    # keep, and the customer's drawing is their product. What the writer
    # has is the measured summary below: every layer name, every block name
    # with its count, the lengths and areas per layer. That is enough to
    # say what the drawing is and to write the BOQ; anything spatial it
    # cannot know from a table it must say it does not know, not invent.
    cad_note = (
        "\n\nABOUT THE DRAWING. It was measured on the customer's own machine "
        "and is NOT attached — you will not receive the file, so do not ask "
        "for it and do not try to open one. Do NOT build, install or download "
        "any CAD tool or converter, and do NOT try to convert a DWG — that is "
        "never the task here. The MEASURED QUANTITIES below are "
        "the authoritative numbers for the BOQ: they were read from the "
        "drawing's real geometry, layer by layer and block by block. Then:\n"
        "  (a) From the layer and block names and the figures, state briefly "
        "what the drawing IS (site survey, services layout, floor plan…) and "
        "what it contains.\n"
        "  (b) Build every line item from those figures. Where a quantity "
        "must be derived (a count from a length, a rate from a norm), say "
        "so on that line.\n"
        "  (c) Where the layout itself would decide something — where a "
        "camera goes, how a cable is routed — and the figures do not say, "
        "write the item with the quantity the figures support and flag the "
        "placement as to be confirmed on the drawing. Never invent geometry "
        "you have not been given.\n"
        "  (d) Work like the estimator who wrote the best of these: open "
        "with a short reference table of the measured figures you used "
        "(feature, layer or block, value, unit); size storage, power and "
        "switching from the device counts with the arithmetic shown; where "
        "the drawing already has poles, walls or structures, use them as "
        "mounting or routing points before adding new ones; and keep every "
        "assumed average (a cable drop, a spacing, a trench depth) "
        "plausible for the site's actual size and stated on the line.\n"
        "Do not skip this and jump to formatting."
    ) if has_cad else ""
    # The caller's own per-industry write-up note -- see this function's
    # docstring. Used to be a block of HVAC text hardcoded right here,
    # gated on a bare `hvac: bool`; that text now lives on the app side
    # (addons/boq/industries.py), the one place per-industry BOQ rules are
    # supposed to live. Gating is unchanged: a trade-specific note only
    # makes sense once there is real measured geometry (has_cad) to apply
    # it to.
    industry_note = industry_note if has_cad else ""
    if not has_cad:
        # Spec mode: no drawing exists at all. Everything is derived from the
        # stated requirement, so the rules are about being explicit and
        # honest per line rather than about separating measured from derived.
        derive_rule = (
            " THERE IS NO DRAWING for this job — the requirement above is your "
            "only definition of scope, so EVERY quantity you give is derived, "
            "not measured. Work as an experienced estimator quoting from a "
            "specification: break the deliverable into its real sub-assemblies "
            "and components, and for each line state the quantity, the unit, "
            "and the basis for it (a standard size, a rule of thumb, a stated "
            "assumption, or a count that follows directly from the spec). "
            "Rules: (a) open with a prominent warning box stating this is a "
            "PROVISIONAL, SPECIFICATION-BASED ESTIMATE — no drawing was "
            "available, quantities are derived from standard design practice "
            "for this size/duty and must be verified against approved "
            "drawings before ordering or tendering; (b) put the governing "
            "specification you worked to (capacity, size, rating, duty) in a "
            "short table near the top, so a reader can see exactly what was "
            "assumed; (c) where a quantity genuinely cannot be pinned down "
            "without a drawing, say so on that line rather than inventing a "
            "precise-looking figure; (d) separate bought-out items from "
            "fabricated/made items — the bought-outs are far more certain and "
            "the reader should be able to tell them apart at a glance."
        )
    elif allow_derived:
        # Found live, 31 Oct 2026: this whole paragraph used to be written
        # in ELV's own vocabulary (camera spacing, boundary and road
        # lengths, gate counts) -- every other trade's own BOQ run,
        # HVAC included, received that same ELV framing and worked
        # example whether or not any of it applied. The arithmetic
        # example is now the caller's own `derivation_example` (empty
        # falls back to the original ELV one, unchanged for a trade with
        # no override yet); the framing sentence itself is now
        # trade-agnostic rather than naming ELV's own items by name.
        example = derivation_example or "'boundary 1091 m ÷ 40 m camera spacing = 28 nos'"
        derive_rule = (
            " IMPORTANT — how to handle an item the drawing's own measured "
            "geometry does not cover: a system, accessory or whole trade "
            "the design hasn't detailed yet, or that this drawing doesn't "
            "show at all. Do NOT refuse the job on that basis, and do NOT "
            "silently pretend those items were measured. Instead DERIVE "
            "them as a competent estimator for this trade would: work the "
            "quantities out from the site's own real measured geometry, "
            "combined with explicit, stated design assumptions. Rules for "
            "derived items: (a) open the document with a prominent warning "
            "box stating this is a PROVISIONAL DESIGN-STAGE ESTIMATE, not a "
            "measured take-off, and that it must be validated by a site "
            "survey and detailed design before tendering; (b) under each "
            f"section heading, state the design basis in one or two lines "
            f"— the actual arithmetic, e.g. {example}; (c) never present a "
            "derived quantity as a measured one. Measured items, where the "
            "user asked for them, stay exactly as measured."
        )
    else:
        derive_rule = (
            " Do NOT add line items that have no corresponding measurement "
            "below — if the user asked for a trade the drawing does not "
            "contain, say so plainly instead of inventing quantities."
        )
    standards_block = (
        "\n\nDESIGN STANDARDS BRIEF from the research stage — when you derive "
        "a quantity, apply THESE norms and cite the specific rule you used "
        "(e.g. 'at 40 m spacing per the brief'). Prefer them over your own "
        "recollection; if one is missing for something you need, say which "
        "assumption you had to make instead:\n"
        f"{standards_text.strip()}"
    ) if standards_text.strip() else ""
    legend_block = (
        f"\n\nLAYER/BLOCK LEGEND (what these codes mean — use it to write real "
        f"descriptions instead of repeating the raw code):\n{legend}"
    ) if legend.strip() else ""
    # item 50 (4 Nov 2026) — see addons/boq/dialog.py's own copy of this
    # comment for the real-job comparison that found it: "add 100mm in
    # length and breadth for scrap" was read by the writer as a sheet-panel
    # instruction only ("length and breadth" of a panel) — every tube/rod/
    # flat-bar CUT LENGTH kept its bare spec figure, with the writer's own
    # notes saying tubes got no allowance at all. A second, independent run
    # of the identical PDF and instruction applied the allowance to every
    # cut piece, tube and rod included — a difference in how one ambiguous
    # instruction was read, not a measurement error either run made. Scoped
    # to project_context (the person's own words), the same text this
    # function already embeds as `context` above, never applied unasked.
    scrap_block = (
        "\n\nSCRAP/WASTAGE ALLOWANCE. The project context above asks for a "
        "cutting allowance. Apply it to EVERY raw-material cut piece, not "
        "sheet panels alone: the cut length of every tube, pipe, flat bar "
        "and rod section, AND the length and width of every sheet/plate "
        "panel. Show both the net (as-designed) and gross (with the "
        "allowance added) figure on each line, and state the allowance "
        "used once, plainly, near the top."
    ) if re.search(r"\bscrap\b|\bwastage\b|\bwaste\b",
                   project_context or "", re.I) else ""
    ground_truth = (
        " The quantities listed at the end were measured directly from the "
        "CAD drawing's geometry — treat them as ground truth and do not "
        "recalculate or contradict them."
    ) if has_cad else ""
    step2 = "\n\n"
    # Stating a "basis" is not enough on its own: the writer will still invent a
    # precise-but-wrong spec (a real run produced a "250 mm emergency-stop
    # button" and "100 mm proximity sensor"). One physically-impossible figure
    # makes an estimator distrust the whole document, so fabricated specifics
    # are forbidden outright — describe the type, cite a genuine standard, or
    # mark it TBC. Applies in every mode (measured, derived, spec-only).
    plausibility_rule = (
        " DO NOT FABRICATE COMPONENT SPECIFICATIONS. For a catalogue/bought-out "
        "item, describe it by type and duty (e.g. 'emergency-stop pushbutton, "
        "mushroom head, IP66, panel-mount') and leave the exact model, size and "
        "rating to the supplier's selection — or cite a genuine standard value. "
        "Never invent a precise-looking figure. Any dimension or rating you DO "
        "state must be physically plausible for that component (a mushroom "
        "E-stop head is ~40 mm across, not 250 mm; an inductive proximity "
        "sensor is an M12–M30 barrel, not 100 mm). When unsure, give the type "
        "and mark the exact size/rating 'to supplier spec (TBC)' rather than "
        "guess — one impossible number discredits the entire BOQ."
    )
    instructions = (
        f"Your task is: produce a professional Bill of Quantities (BOQ) "
        f"document.{context}{ground_truth}{cad_note}{industry_note}"
        f"{step2}{derive_rule}{scope_rule} {structure}{plausibility_rule} "
        "Leave Rate/Amount columns blank for the quantity surveyor to fill "
        "in. Present it as clean tables. Note prominently at the top that "
        "rates are not included and quantities should be independently "
        "verified before tendering."
    )
    # Plain concatenation, not str.format() on the whole thing — a layer or
    # block name from the drawing could itself contain '{'/'}' and must not
    # be misread as a format field.
    tail = ("\n\nMEASURED QUANTITIES:\n" + quantities_text) if quantities_text.strip() else ""
    # House doctrine for a BOQ, when a skill claims this job — see
    # core/skills.py. Empty when none does, so the prompt is unchanged.
    return (instructions + standards_block + brief_block + legend_block
            + scrap_block + _SK.addendum("boq.format") + tail)
