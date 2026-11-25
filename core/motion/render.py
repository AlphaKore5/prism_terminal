"""
Prism Motion Graphics Deterministic Frame Renderer
──────────────────────────────────────────────────
Renders Motion Specifications to MP4 video by seeking through every frame
in a headless browser and piping raw JPEG frames into FFmpeg.

Runner priority:
  1. Playwright Chromium (installed via `playwright install chromium`)
  2. Electron (from prism-desktop node_modules or system PATH)
  3. System Chromium / Google Chrome
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Dict, Optional, Union

from .schema import validate_motion_spec, MotionValidationError
from .resolver import resolve_motion_spec


class MotionRenderError(Exception):
    pass


# ── Runner Discovery ──────────────────────────────────────────────────────────

def _find_electron_binary() -> Optional[str]:
    """Look for Electron binary in node_modules or system PATH."""
    candidates = [
        os.path.abspath(os.path.join(
            os.path.dirname(__file__),
            "../../../../../prism-desktop/node_modules/electron/dist/electron")),
        os.path.abspath(os.path.join(
            os.path.dirname(__file__),
            "../../../../prism-desktop/node_modules/electron/dist/electron")),
        os.path.abspath(os.path.join(
            os.path.dirname(__file__),
            "../../../prism-desktop/node_modules/electron/dist/electron")),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("electron")


def _playwright_available() -> bool:
    """Whether Playwright's Chromium is actually on this machine.

    This used to glob `~/.cache/ms-playwright` for `chromium-*/chrome-linux64
    /chrome` and friends — Linux paths, and the OS cache directory. Both are
    wrong everywhere that matters: Windows and macOS lay the browser out
    differently, and a frozen build carries its copy INSIDE the playwright
    package (PLAYWRIGHT_BROWSERS_PATH=0, set in prism_gui/core_bridge.py),
    not in the cache directory at all. So every packaged build answered "no
    headless browser available" with the browser shipped right beside it.
    core.browser asks Playwright itself instead.
    """
    from .. import browser
    return browser.chromium_path() is not None


# Asset references are resolved by resolver.resolve_motion_spec() into data
# URIs before the browser receives the scene graph. This is intentionally a
# rendering-time operation (rather than a file URL) so attached artwork works
# from source, frozen Windows/macOS/Linux bundles, and saved re-renders.
_DISABLED_PENDING_ASSET_FIX = False



def is_available() -> tuple[bool, str]:
    """Check if FFmpeg and a headless browser runner are available."""
    if _DISABLED_PENDING_ASSET_FIX:
        return False, ("Motion Graphics isn't available in this release yet "
                       "— it's still being finished.")
    try:
        from core import ffmpeg
        ff = ffmpeg.locate()
        if not ff or not os.path.exists(ff):
            return False, "FFmpeg binary is required to render motion graphics."
    except Exception as e:
        return False, f"FFmpeg error: {e}"

    # The same two paths render() itself has — and nothing else. A bare
    # `node` on PATH used to count, but render() never uses node on its
    # own: it needs Playwright's Chromium or an Electron binary, and a
    # machine with only node passed here and then failed mid-run with
    # "No headless browser available" after the storyboard had already
    # been written (study of 11 Nov 2026, finding M5).
    if _playwright_available():
        return True, ""
    if _find_electron_binary():
        return True, ""
    return False, (
        "No headless browser available. Install Playwright: "
        "pip install playwright && playwright install chromium"
    )


# ── Playwright Python Renderer ────────────────────────────────────────────────

def _render_via_playwright(
    resolved_spec: Dict[str, Any],
    ff: str,
    output_path: str,
    fps: int,
    total_frames: int,
    crf: int,
    preset: str,
    on_progress: Optional[Callable[[int, int], None]],
) -> str:
    """Render every frame using the Playwright Python API (headless Chromium).

    Each frame is:
      1. Evaluated in the browser via `window.__seek(frame)`
      2. Captured as a JPEG page screenshot
      3. Written to FFmpeg stdin in MJPEG stream format
    """
    from playwright.sync_api import sync_playwright
    from .. import browser as _browser

    runtime_dir = os.path.join(os.path.dirname(__file__), "runtime")
    index_html  = pathlib.Path(runtime_dir) / "index.html"
    if not index_html.exists():
        raise MotionRenderError(f"Runtime index.html not found: {index_html}")

    width  = resolved_spec["project"]["width"]
    height = resolved_spec["project"]["height"]
    spec_json = json.dumps(resolved_spec)

    ffmpeg_cmd = [
        ff, "-y", "-loglevel", "error",
        "-f", "image2pipe",
        "-c:v", "mjpeg",
        "-framerate", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-preset", preset,
        "-movflags", "+faststart",
        output_path,
    ]

    ff_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        with sync_playwright() as pw:
            browser = _browser.launch_chromium(
                pw,
                args=["--no-sandbox", "--disable-gpu", "--disable-software-rasterizer",
                      "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
            )
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(index_html.as_uri(), wait_until="domcontentloaded")

            # Wait for the runtime's own bootstrap (index.html's inline
            # script) to have run, then hand it the spec through the same
            # window.__loadSpec/__seek/__pendingImages API the Electron
            # path already used — previously this function reconstructed
            # its own separate MotionRuntime instance instead of using it,
            # a second bootstrap path that could silently drift from this
            # one.
            page.wait_for_function("typeof window.__loadSpec === 'function'",
                                   timeout=15_000)
            page.evaluate("spec => window.__loadSpec(spec)", resolved_spec)

            # A web font that never arrives must not stop the render —
            # index.html's Google Fonts link uses display=block, so
            # without this wait, frame 0 (and likely several after it)
            # can screenshot genuinely blank text. Mirrors core.reel_web's
            # own harness.
            _browser.warm_fonts(page)
            try:
                page.wait_for_function(
                    "document.fonts.ready.then(() => true)", timeout=8000)
            except Exception:
                pass

            # Every <img> loads asynchronously — wait for each one to
            # settle (loaded OR errored, a broken asset must not hang
            # forever) before seeking frame 0. Without this, a
            # slow-loading brand asset can still be showing its loading
            # placeholder on frames where it should already be visible.
            try:
                page.wait_for_function(
                    "window.__pendingImages() === 0", timeout=15_000)
            except Exception:
                pass  # best-effort — a genuinely stuck image renders as its placeholder, not a crash

            # Seek frame-by-frame and capture. A full page screenshot, not
            # a canvas readback — the scene graph is real DOM now, there's
            # no single element to read pixels from the way there was
            # with a <canvas>. This also drops the base64 JS<->Python
            # round-trip the old canvas.toDataURL() capture needed.
            for frame_idx in range(total_frames):
                page.evaluate(f"window.__seek({frame_idx})")
                try:
                    ff_proc.stdin.write(page.screenshot(type="jpeg", quality=95))
                except BrokenPipeError:
                    err = ff_proc.stderr.read().decode("utf-8", "ignore")
                    raise MotionRenderError(f"FFmpeg stopped early: {err[:400]}")

                if on_progress:
                    on_progress(frame_idx + 1, total_frames)

            browser.close()

        # communicate() unconditionally flushes self.stdin if it isn't None,
        # even with no input to send — that raises "flush of closed file" on
        # a pipe we already closed ourselves. Clearing the attribute (not
        # just closing the file) is what makes communicate() skip it.
        ff_proc.stdin.close()
        ff_proc.stdin = None
        _, ff_err = ff_proc.communicate()

        if ff_proc.returncode != 0:
            raise MotionRenderError(
                f"FFmpeg encoding failed: {ff_err.decode(errors='replace')}"
            )

    except Exception:
        try:
            ff_proc.stdin.close()
        except Exception:
            pass
        ff_proc.wait()
        raise

    return output_path


# ── Electron Runner (fallback) ────────────────────────────────────────────────

def _render_via_electron(
    resolved_spec: Dict[str, Any],
    electron_bin: str,
    ff: str,
    output_path: str,
    fps: int,
    total_frames: int,
    crf: int,
    preset: str,
    on_progress: Optional[Callable[[int, int], None]],
) -> str:
    runtime_dir   = os.path.join(os.path.dirname(__file__), "runtime")
    runner_script = os.path.join(runtime_dir, "render_runner.js")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(resolved_spec, tf, indent=2)
        spec_temp_path = tf.name

    ffmpeg_cmd = [
        ff, "-y",
        "-f", "image2pipe",
        "-c:v", "mjpeg",
        "-framerate", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-preset", preset,
        "-movflags", "+faststart",
        output_path,
    ]

    runner_cmd = [
        electron_bin,
        runner_script,
        spec_temp_path,
        "--no-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
    ]

    try:
        ff_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        runner_proc = subprocess.Popen(
            runner_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        frames_rendered = 0
        chunk_size = 65536
        jpeg_soi   = b"\xff\xd8"

        while True:
            chunk = runner_proc.stdout.read(chunk_size)
            if not chunk:
                break
            ff_proc.stdin.write(chunk)
            soi_count = chunk.count(jpeg_soi)
            if soi_count > 0:
                frames_rendered += soi_count
                if on_progress:
                    on_progress(min(total_frames, frames_rendered), total_frames)

        runner_proc.stdout.close()
        runner_proc.wait()
        _, ff_err = ff_proc.communicate()

        if ff_proc.returncode != 0:
            raise MotionRenderError(
                f"FFmpeg encoding failed: {ff_err.decode(errors='replace')}"
            )

    finally:
        if os.path.exists(spec_temp_path):
            try:
                os.remove(spec_temp_path)
            except OSError:
                pass

    return output_path


# ── Public API ─────────────────────────────────────────────────────────────────

def audio_faults(spec: Dict[str, Any]) -> list:
    """The spec's production audio files that are not on disk."""
    audio = spec.get("audio") if isinstance(spec, dict) else None
    if not isinstance(audio, dict):
        return []
    faults = []
    for key in ("music", "voiceover"):
        path = audio.get(key)
        if isinstance(path, str) and path and not os.path.isfile(path):
            faults.append(f"audio: the {key} file is not on this machine ({path})")
    return faults


def _mux_production_audio(resolved_spec: Dict[str, Any], video_path: str) -> str:
    """Attach the spec's production music/voice to the finished film, in
    place. No audio named means the silent file is the deliverable."""
    audio = resolved_spec.get("audio") if isinstance(resolved_spec, dict) else None
    audio = audio if isinstance(audio, dict) else {}
    root, ext = os.path.splitext(video_path)
    music, voice, volume = audio.get("music"), audio.get("voiceover"), float(audio.get("music_volume", 0.25))
    sfx_path = None
    # project.sound = "ui": the film's own sound, generated from its timeline
    # (core.motion.sfx) — a tick per typed character, a click per press, a whoosh
    # per camera move. Music the author supplied is theirs and wins; with a voice-
    # over the effects sit under it like any background track.
    if str((resolved_spec.get("project") or {}).get("sound", "")).lower() in ("ui", "sfx") and not music:
        from . import sfx as _sfx
        sfx_path = _sfx.build(resolved_spec, root + ".sfx.wav")
        if sfx_path:
            music, volume = sfx_path, (0.8 if voice else 1.0)
    if not (music or voice):
        return video_path
    from .audio import mux_audio_and_video
    try:
        muxed = mux_audio_and_video(video_path, voice, root + ".audio" + ext, bgm_path=music, bgm_volume=volume)
        if muxed != video_path and os.path.isfile(muxed):
            os.replace(muxed, video_path)
    finally:
        if sfx_path and os.path.isfile(sfx_path):
            os.remove(sfx_path)
    return video_path


def render(
    spec: Union[str, Dict[str, Any]],
    output_path: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    fps: Optional[int] = None,
    crf: int = 19,
    preset: str = "medium",
) -> str:
    """Render a Motion Specification to an MP4 video.

    Uses Playwright (preferred) or Electron (fallback) as the headless renderer.
    """
    # Studio edits saved on the spec (core.motion.studio) are folded in
    # first — the same function the editor's preview goes through, so the
    # film cannot differ from what the editor showed.
    if isinstance(spec, str):
        import json as _json
        spec = _json.loads(spec)
    if isinstance(spec, dict) and spec.get("_motion_edits"):
        from .studio import apply_edits as _apply_edits
        spec = _apply_edits(spec, spec.get("_motion_edits"))
    valid_spec    = validate_motion_spec(spec)
    resolved_spec = resolve_motion_spec(valid_spec)

    # A handoff the continuity compiler could not bridge keeps its ordinary
    # transition and is reported on the resolved spec (`_continuity_compiled
    # ["errors"]`) for Studio, the review sheet and the promotion gate. Only
    # a caller that asks for strictness (`_strict_continuity`: the gate and
    # its tests) has the render refused — a production run's finished plan
    # is never thrown away over a key the writer forgot.
    broken = (resolved_spec.get("_continuity_compiled") or {}).get("errors") or []
    if broken and resolved_spec.get("_strict_continuity"):
        raise MotionValidationError(
            "continuity broken — " + " | ".join(e["message"] for e in broken[:4]))

    # Production audio named by the spec must exist before a minute of
    # frames is spent — a missing track is a spec problem, not FFmpeg's.
    for problem in audio_faults(resolved_spec):
        raise MotionValidationError(problem)

    p            = resolved_spec["project"]
    width        = p["width"]
    height       = p["height"]
    fps          = fps or p["fps"]
    duration     = p["duration"]
    total_frames = int(round(duration * fps))

    from core import ffmpeg as _ffmpeg
    ff = _ffmpeg.locate()
    if not ff or not os.path.exists(ff):
        raise MotionRenderError("FFmpeg executable not found.")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # ── Playwright path (preferred) ───────────────────────────────────────────
    if _playwright_available():
        result = _render_via_playwright(
            resolved_spec, ff, output_path, fps, total_frames, crf, preset, on_progress
        )
        if on_progress:
            on_progress(total_frames, total_frames)
        return _mux_production_audio(resolved_spec, result)

    # ── Electron fallback ─────────────────────────────────────────────────────
    electron_bin = _find_electron_binary()
    if electron_bin:
        result = _render_via_electron(
            resolved_spec, electron_bin, ff, output_path,
            fps, total_frames, crf, preset, on_progress
        )
        if on_progress:
            on_progress(total_frames, total_frames)
        return _mux_production_audio(resolved_spec, result)

    raise MotionRenderError(
        "No headless browser available. "
        "Run: pip install playwright && playwright install chromium"
    )
