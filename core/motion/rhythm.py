"""
Prism Motion Graphics — the film's rhythm, measured
────────────────────────────────────────────────────
A film that never rests reads as noise and one that never moves reads as a
slide. The polished reference this was compared against (11-12 Nov 2026)
holds still for about a third of its frames, in a few long stretches, and
spends its motion in short bursts; Prism's model-written films moved
continuously (12 % still frames, no hold over 0.7 s) and its Reel never moved.

This renders the resolved spec at low resolution, 10 frames a second,
through the same runtime that will draw it, and measures what a viewer feels:
the share of still frames, the longest holds, how much of the motion falls in
the busiest tenth of the seconds, and the mean brightness. Deterministic, no
model call. `faults()` turns the numbers into sentences a writer can act on.

Method and thresholds are those used on the reference at 10 fps and 192 px
wide: a frame is "still" when it differs from the last by under 0.3 grey
levels on average.
"""
from __future__ import annotations

import os
import pathlib
from io import BytesIO
from typing import Any

STILL = 0.3          # mean grey-level change below which a frame is still
SAMPLE_FPS = 10
ANALYSIS_WIDTH = 192


def probe(spec: dict[str, Any], fps: int = SAMPLE_FPS) -> dict[str, Any] | None:
    """The rhythm profile of `spec`, or None when there is no browser."""
    from .render import _playwright_available
    if not _playwright_available():
        return None
    import numpy as np
    from PIL import Image
    from .resolver import resolve_motion_spec
    from .schema import validate_motion_spec
    resolved = resolve_motion_spec(validate_motion_spec(spec))
    project = resolved["project"]
    width, height = int(project["width"]), int(project["height"])
    src_fps = int(project.get("fps", 30) or 30)
    duration = float(project.get("duration") or sum(float(s.get("duration", 0) or 0) for s in resolved["scenes"]))
    scale = min(1.0, ANALYSIS_WIDTH / width)

    from playwright.sync_api import sync_playwright
    from .. import browser as _browser
    index = pathlib.Path(os.path.dirname(__file__), "runtime", "index.html")
    frames = []
    with sync_playwright() as pw:
        chromium = _browser.launch_chromium(
            pw, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        try:
            page = chromium.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
            page.goto(index.as_uri(), wait_until="domcontentloaded")
            page.wait_for_function("typeof window.__loadSpec === 'function'", timeout=15_000)
            page.evaluate("s => window.__loadSpec(s)", resolved)
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(() => true)", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            # Let the first paint settle before sampling: an unsettled frame 0 reads
            # as motion (a replay of frame 0 differed by 0.66 grey levels, 13 Nov 2026).
            page.evaluate("""async () => { window.__seek(0);
                await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); }""")
            n = int(duration * fps)
            for i in range(n):
                page.evaluate(f"window.__seek({int(round(i / fps * src_fps))})")
                im = Image.open(BytesIO(page.screenshot(type="png"))).convert("L")
                frames.append(np.asarray(im, dtype=np.float32))
        finally:
            chromium.close()
    return profile_from_frames(frames, fps)


def profile_from_frames(frames, fps: int = SAMPLE_FPS) -> dict[str, Any]:
    """The rhythm profile of a list of grayscale frames (2-D float arrays)
    sampled at `fps`. The one place the numbers are made, so a film measured from
    its spec (probe) and one measured from its video file (probe_video) are
    measured identically."""
    import numpy as np
    if len(frames) < 3:
        return {"seconds": 0.0, "static_pct": 0.0, "per_second": [], "holds": [], "top10_share": 0.0,
                "luminance": [0.0, 0.0], "mean_motion": 0.0, "longest_hold": 0.0, "holds_ge_1s": 0}
    diffs = np.abs(np.diff(np.stack(frames), axis=0)).mean(axis=(1, 2))
    per_second = [float(diffs[i * fps:(i + 1) * fps].mean()) for i in range(len(diffs) // fps)]
    holds, run = [], 0
    for d in diffs:
        if d < STILL:
            run += 1
        else:
            if run:
                holds.append(run / fps)
            run = 0
    if run:
        holds.append(run / fps)
    ordered = sorted(per_second, reverse=True)
    top = max(1, len(ordered) // 10)
    total = sum(ordered) or 1.0
    lum = [float(f.mean()) for f in frames]
    return {
        "seconds": round(len(frames) / fps, 2),
        "static_pct": round(float((diffs < STILL).mean() * 100), 1),
        "mean_motion": round(float(diffs.mean()), 2),
        "per_second": [round(v, 2) for v in per_second],
        "holds": [round(h, 1) for h in holds],
        "longest_hold": round(max(holds), 1) if holds else 0.0,
        "holds_ge_1s": sum(1 for h in holds if h >= 1.0),
        "top10_share": round(sum(ordered[:top]) / total * 100, 0),
        "luminance": [round(min(lum), 0), round(max(lum), 0)],
    }


def probe_video(path: str, fps: int = SAMPLE_FPS) -> dict[str, Any] | None:
    """The rhythm profile of a finished video FILE — a Motion film, a Studio reel,
    a Reel, a reference — with FFmpeg only, no browser. Same maths, same
    thresholds. None when FFmpeg cannot read it."""
    import re
    import subprocess
    import numpy as np
    from .. import ffmpeg
    ff = ffmpeg.locate()
    if not ff or not os.path.exists(path):
        return None
    # The size comes from FFmpeg's own banner, not ffprobe: the copy Prism ships
    # (imageio-ffmpeg) has no ffprobe next to it, and customers' machines do not
    # have one on PATH either, so asking ffprobe made this return None for
    # everyone except developers who happened to have it installed.
    try:
        banner = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True,
                                errors="replace").stderr
        m = re.search(r"Video:.*?[,\s](\d{2,5})x(\d{2,5})[,\s\[]", banner)
        w0, h0 = int(m.group(1)), int(m.group(2))
    except Exception:  # noqa: BLE001
        return None
    w = ANALYSIS_WIDTH
    h = max(2, round(w * h0 / w0) // 2 * 2)
    raw = subprocess.run([ff, "-v", "error", "-i", path, "-vf", f"fps={fps},scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (w * h)
    if n < 3:
        return None
    arr = np.frombuffer(raw[:n * w * h], np.uint8).reshape(n, h, w).astype(np.float32)
    return profile_from_frames(list(arr), fps)


def faults(profile: dict[str, Any] | None, static_range: tuple[float, float] = (15.0, 45.0)) -> list[str]:
    """What is wrong with the rhythm, as sentences. [] when it is fine or when
    there is no profile. Ranges are wide on purpose: the reference sits at
    28-33 % still frames; anything from 15 to 45 % is a film that breathes."""
    if not profile or profile.get("seconds", 0) < 4:
        return []
    out: list[str] = []
    lo, hi = static_range
    still = profile["static_pct"]
    if still < lo:
        out.append(f"The film never rests: {still:.0f}% of frames are still (it should be about "
                   f"{lo:.0f}-{hi:.0f}%) and its longest hold is {profile['longest_hold']:g}s. Give "
                   "one or two scenes a real hold of 1.5s or more — nothing drifting, nothing "
                   "entering — right after the line that must be read.")
    elif still > hi:
        # the longest run of consecutive quiet seconds, so the writer knows WHERE
        best, run_start, best_span = 0, None, None
        for i, v in enumerate(list(profile["per_second"]) + [99.0]):
            if v < 0.5 and run_start is None:
                run_start = i
            elif v >= 0.5 and run_start is not None:
                if i - run_start > best:
                    best, best_span = i - run_start, (run_start, i)
                run_start = None
        span = f" (the quiet stretch is seconds {best_span[0]}-{best_span[1]})" if best_span else ""
        out.append(f"The film is mostly still: {still:.0f}% of frames do not change (it should be "
                   f"about {lo:.0f}-{hi:.0f}%){span}. Put a camera move, a cursor, a typed line or "
                   "an entering element in the quiet stretch.")
    if profile["holds_ge_1s"] == 0 and still >= lo:
        out.append("There is no hold of a second or more: nothing is ever given time to be read.")
    if profile["top10_share"] < 20 and still >= lo:
        out.append(f"The motion is spread evenly (only {profile['top10_share']:.0f}% of it falls in "
                   "the busiest tenth of the seconds; a film with a payoff has 30-45%). Hold, then "
                   "spend one decisive change.")
    return out


# ── repair: which scenes to send back, and what to tell the writer ───────────

def kind(profile: dict[str, Any] | None, static_range: tuple[float, float] = (15.0, 45.0)) -> str | None:
    """"never_rests", "mostly_still" or None (fine, or nothing measured)."""
    if not profile or profile.get("seconds", 0) < 4:
        return None
    lo, hi = static_range
    if profile["static_pct"] < lo:
        return "never_rests"
    if profile["static_pct"] > hi:
        return "mostly_still"
    return None


def distance(profile: dict[str, Any] | None, static_range: tuple[float, float] = (15.0, 45.0)) -> float:
    """How far the still-frame share is outside the band (0 inside it)."""
    if not profile:
        return 0.0
    lo, hi = static_range
    s = profile["static_pct"]
    return max(0.0, lo - s, s - hi)


def better(before: dict[str, Any] | None, after: dict[str, Any] | None,
           static_range: tuple[float, float] = (15.0, 45.0), margin: float = 2.0) -> bool:
    """A repaired film must land clearly nearer the band: by at least `margin`
    points of still-frame share. Same rule the per-scene repair uses, a "fix"
    that does not move the number is not a fix."""
    if not after:
        return False
    return distance(after, static_range) <= distance(before, static_range) - margin


def scene_activity(profile: dict[str, Any], durations: list[float]) -> list[float]:
    """Mean motion (grey levels) per scene: the per-second series averaged over
    each scene's own seconds."""
    per = profile.get("per_second") or []
    out, t = [], 0.0
    for d in durations:
        lo, hi = int(t), max(int(t) + 1, int(round(t + d)))
        seg = per[lo:hi]
        out.append(round(sum(seg) / len(seg), 2) if seg else 0.0)
        t += d
    return out


def targets(profile: dict[str, Any], durations: list[float], what: str, limit: int = 2) -> list[int]:
    """The scenes to send back. A film that never rests: its busiest scenes, but
    never the last (the payoff keeps its burst). A film that never moves: its
    quietest, but never the first (the opening may be a deliberate hold)."""
    act = scene_activity(profile, durations)
    idx = list(range(len(act)))
    if what == "never_rests":
        idx = [i for i in idx if i != len(act) - 1] or idx
        idx.sort(key=lambda i: -act[i])
    else:
        idx = [i for i in idx if i != 0] or idx
        idx.sort(key=lambda i: act[i])
    return sorted(idx[:limit])


def repair_text(scene_no: int, what: str, profile: dict[str, Any], activity: float,
                static_range: tuple[float, float] = (15.0, 45.0), medium: str = "motion") -> str:
    lo, hi = static_range
    head = (f"The finished film was measured. {profile['static_pct']:.0f}% of its frames are still "
            f"(a film that breathes is {lo:.0f}-{hi:.0f}%) and its longest hold is {profile['longest_hold']:g}s. "
            f"Scene {scene_no} moves at {activity:g} grey levels a second on average. ")
    if what == "never_rests":
        body = ("Nothing in the film ever settles, so nothing is given time to be read. Rewrite this scene "
                "so it has a real hold: its elements arrive in the first third, then NOTHING moves for at "
                "least 1.5 seconds. Remove looping secondary_motion and drift, and particle fields that keep "
                "moving after the arrival. Do not fill the hold with decoration.")
    elif medium == "studio":
        body = ("The reel is mostly still. Rewrite this scene so something is always changing in it: a slow "
                "zoom or drift on the photo or the main shape across the whole scene, lines that arrive one "
                "after another (staggered animation-delay), a stroke that draws itself, a number that "
                "counts. Give each change a clear start and end, keep the words, and keep every change "
                "inside the frame.")
    else:
        body = ("The film is mostly still. Rewrite this scene so something is always changing in it: a camera "
                "shot (push toward the focus or pull to reveal), staggered entrances, a typed line or a "
                "travelling cursor. Give the change a clear start and end.")
    return head + body
