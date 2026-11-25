"""
Prism — reel renderer, HTML/CSS edition
───────────────────────────────────────
The agent designs the video. This module only guarantees it is a video.

core/reel.py draws every pixel in Pillow, which is why every reel it makes
shares one look: the layouts, the background, the palette structure and the
motion are all Python. Two clients get the same film with different words.

This renderer takes the opposite position, and it is the same one Claude
Design and Remotion take: the design surface is a browser. The writing stage
sends CSS and markup it wrote itself — its own background, type, colour,
composition and motion — and the only things fixed here are the frame size,
the seeking, the encoding, and a legibility check that runs before anything
is written to disk.

How the seeking works (the part that makes a web page filmable):

  · the page declares ordinary CSS @keyframes animations
  · nothing is allowed to run in real time — every animation is PAUSED and
    its currentTime is set explicitly for each frame
  · so frame 240 is identical whether it was reached in sequence, out of
    order, or a week later. A recording would not be; a seek is.

That is why this is a renderer and not a screen capture.

Licence note: no Remotion code is used or required. The technique — a paused
browser timeline, screenshotted per frame, piped to FFmpeg — is not anyone's
property, and this implementation is ours.
"""
from __future__ import annotations
import json
import math
import os
import re
import shutil
import subprocess

W, H = 1080, 1920
SAFE_X, SAFE_Y = 90, 130
DEFAULT_FPS = 30

# Where essential copy may sit — the ONE box every prompt in this module
# states. The design and scene prompts used to give two different boxes in
# the same message — "x=130..950 and y=180..1580" in one paragraph, "90px/
# 130px margins" a few paragraphs on — and a model handed two sets of
# numbers lays out to one of them, which cost needless layout-correction
# turns.
#
# Deliberately NOT derived from SAFE_X/SAFE_Y. Those are the harness's frame
# margins for backgrounds and shapes. Copy needs more room, above all at the
# bottom: a vertical reel is watched under the platform's own caption, name
# and buttons, and a headline placed down to y=1790 sits beneath them. The
# 180..1580 band is the one reels were laid out to before this change.
COPY_X, COPY_TOP, COPY_BOTTOM = 130, 180, 1580
SAFE_AREA = f"x={COPY_X}..{W - COPY_X} and y={COPY_TOP}..{COPY_BOTTOM}"

# Minimum type sizes for a 1080-wide frame, unchanged from the Pillow
# renderer: a phone is watched at arm's length for under a second a scene.
# The agent may design anything it likes above these.
T_HEADLINE, T_SUPPORT, T_LABEL = 84, 44, 32

# A menu of named curves for the motion doctrine in design_instructions()/
# scene_instructions() — deliberately a menu, not a mandate; see the note by
# `cut` in design_instructions() about prohibitions making these reels
# blander. Measured on a real reel: showing only ONE worked example of
# `both` fill-mode everywhere in this prompt anchored the model to that
# exact curve for 88% of every animation in the whole reel. A fixed
# illustration is not a neutral example, it is training data. ease_pick()
# rotates which curve illustrates the syntax so consecutive scenes are never
# shown the same one twice running; the full menu is still offered every
# time so the model has somewhere to reach beyond whichever one it was shown.
EASE_MENU = [
    ("settle", "cubic-bezier(.16,1,.3,1)",
     "a confident, unhurried arrival — the default for a headline or a "
     "panel that owns the frame"),
    ("snap", "cubic-bezier(.34,1.56,.64,1)",
     "quick with a touch of overshoot — counters, chips, small labels, "
     "anything that should feel tapped into place"),
    ("firm", "cubic-bezier(.22,1,.36,1)",
     "weighted and fast, no overshoot — a second or third element "
     "following a `settle` so the scene does not read as one move repeated"),
    ("glide", "cubic-bezier(.37,0,.63,1)",
     "symmetric — for motion still running when the scene hands over: a "
     "drift, a slow scale, a counter"),
    ("exit", "cubic-bezier(.64,0,.78,0)",
     "accelerating — for anything leaving rather than arriving; entrances "
     "and exits should not share a curve"),
]


def ease_pick(idx: int) -> tuple[str, str]:
    """Rotate EASE_MENU by scene index — never the same worked-example
    curve two scenes running (see EASE_MENU's note)."""
    name, curve, _ = EASE_MENU[idx % len(EASE_MENU)]
    return name, curve


def _ease_menu_text() -> str:
    return "; ".join(f"`{name}` ({curve}) — {why}"
                      for name, curve, why in EASE_MENU)


class ReelError(Exception):
    pass


def ffmpeg_path() -> str:
    """Same resolver as reel.py — see core/ffmpeg.py.

    These two were separate copies of `shutil.which("ffmpeg")`, which meant
    two places to fix and, predictably, only one of them getting fixed.
    """
    from . import ffmpeg as ffmpeg_tool
    found = ffmpeg_tool.locate()
    if not found:
        raise ReelError(ffmpeg_tool.MISSING)
    return found


def available() -> tuple[bool, str]:
    """(ready, why not). Checked before a run so the failure is a sentence,
    not a stack trace in the middle of a pipeline."""
    try:
        ffmpeg_path()
    except ReelError as e:
        return False, str(e)
    # Checks the browser BINARY, not just that `import playwright` works —
    # a packaged build has the Python package either way, and answering
    # "ready" on the strength of that is what let a render start and then
    # die on Playwright's own "Executable doesn't exist at …".
    from . import browser
    return browser.available()


# ── the harness ─────────────────────────────────────────────────────────────
# Everything the page can rely on, and everything it is not allowed to break.

EMPTY_BAND = 0.33           # a scene may leave at most this share of the frame bare at the top or the bottom
CAMERA_PUSH = 0.08          # how far the camera travels in one scene (8% of the frame)

_HARNESS_CSS = f"""
*, *::before, *::after {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0;
  width: {W}px; height: {H}px;
  overflow: hidden;
  background: var(--bg, #ffffff);
  -webkit-font-smoothing: antialiased;
  text-rendering: geometricPrecision;
}}
#stage {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; }}
/* A scene is a full-frame layer. Only the ones in play are painted, so an
   off-screen scene can never leak a stray pixel into the frame. */
#stage > .scene {{ position: absolute; inset: 0; visibility: hidden; }}
#stage > .scene.on {{ visibility: visible; }}
#stage > .scene.on .scene, #stage > .scene.on * {{ visibility: visible; }}
/* THE CAMERA. Every scene is pushed in (or pulled back) slowly for its whole
   length, by the harness, whatever the design wrote. A film whose designer put
   every entrance in the first second and then held still for four was measured
   at 82% still frames (13 Nov 2026, "poster" look, target 15-45%): the model's
   own "slow drift" was 18px over six seconds, below what the eye or the
   measurement sees. `scale` is the individual property, so it composes with
   the cut library's `transform` instead of fighting it. --q runs 0->1 (push in)
   or 1->0 (pull back) and is set by the seeker; the layout check switches the
   camera off (`nocam`) because it asks where things are at rest. */
#stage > .scene.on {{ scale: calc(1 + var(--q, 0) * {CAMERA_PUSH}); }}
#stage.nocam > .scene {{ scale: none !important; }}
/* The safe area is advisory for the designer and enforced by the checker. */
:root {{ --safe-x: {SAFE_X}px; --safe-y: {SAFE_Y}px; --W: {W}px; --H: {H}px; }}
.safe {{ position: absolute; inset: var(--safe-y) var(--safe-x); }}
/* Default cut, overridable: whatever the design says about .leaving and
   .entering wins, because these are the weakest possible rules. */
#stage > .scene.leaving {{ opacity: calc(1 - var(--x, 0)); }}
#stage > .scene.entering {{ opacity: var(--e, 1); }}
img, svg, video {{ max-width: 100%; }}

/* ── the cut library ──────────────────────────────────────────────────────
   Named transitions the design can ask for by class instead of inventing
   motion from nothing. Adapted from HyperFrames' transition catalogue
   (github.com/heygen-com/hyperframes, Apache 2.0) — see NOTICE.

   Translated, not copied. Theirs are GSAP timelines; these are pure
   functions of --x and --e, which the seeker sets afresh on every frame.
   That difference matters more than it looks: an @keyframes transition is
   placed on the SCENE's clock and has to have its duration matched to the
   cut by hand, while these are correct at any frame by construction and
   cannot drift out of the cut window.

   --x runs 0->1 as a scene leaves, --e runs 0->1 as one arrives. The eased
   forms below are smoothstep (3t^2 - 2t^3) — close enough to power3.inOut
   for a half-second cut, and expressible in plain calc. */
.scene.leaving  {{ --ease: calc(var(--x, 0) * var(--x, 0) * (3 - 2 * var(--x, 0))); }}
.scene.entering {{ --ease: calc(var(--e, 1) * var(--e, 1) * (3 - 2 * var(--e, 1))); }}

/* PUSH — the workhorse. Both scenes travel together, the new one pushing the
   old off. Neutral forward progress; use it for ordinary beats. */
.cut-push.leaving  {{ transform: translateX(calc(var(--ease) * -100%)); opacity: 1; }}
.cut-push.entering {{ transform: translateX(calc((1 - var(--ease)) * 100%)); opacity: 1; }}
.cut-push-up.leaving  {{ transform: translateY(calc(var(--ease) * -100%)); opacity: 1; }}
.cut-push-up.entering {{ transform: translateY(calc((1 - var(--ease)) * 100%)); opacity: 1; }}

/* SQUEEZE — the old frame compresses to its left edge, the new one opens out
   from its right. Mechanical and precise; suits industrial subjects. */
.cut-squeeze.leaving {{
  transform: scaleX(calc(1 - var(--ease))); transform-origin: left center;
  opacity: 1; }}
.cut-squeeze.entering {{
  transform: scaleX(var(--ease)); transform-origin: right center;
  opacity: 1; }}

/* ZOOM THROUGH — the old frame rushes past the camera and blurs out while the
   new one rises from behind it. Reserve it: it reads as pushing deeper into
   the same thought, so spending it on an ordinary beat wastes it. */
.cut-zoom.leaving {{
  transform: scale(calc(1 + var(--ease) * 1.5));
  filter: blur(calc(var(--ease) * 8px));
  opacity: calc(1 - var(--ease)); }}
.cut-zoom.entering {{
  transform: scale(calc(0.5 + var(--ease) * 0.5));
  filter: blur(calc((1 - var(--ease)) * 8px));
  opacity: var(--ease); }}
"""

# Pausing every animation and setting its time by hand is the whole trick.
# Nothing on the page is permitted to depend on wall-clock time.
_HARNESS_JS = """
window.__SCENES__ = %s;
window.__ready = false;

window.__seek = function (t) {
  const S = window.__SCENES__;
  for (let i = 0; i < S.length; i++) {
    const s = S[i];
    const el = document.getElementById('s' + i);
    if (!el) continue;
    const on = t >= s.start && t < s.start + s.dur;
    el.classList.toggle('on', on);
    el.classList.remove('leaving', 'entering');
    if (!on) continue;

    const local = t - s.start;
    el.style.setProperty('--p', (local / s.dur).toFixed(5));
    el.style.setProperty('--ms', Math.round(local));
    el.style.setProperty('--q', (i %%%% 2 ? 1 - local / s.dur : local / s.dur).toFixed(5));

    // Overlap windows: the outgoing scene is 'leaving' with --x running 0->1,
    // the incoming one is 'entering' with --e running 0->1.
    if (s.outFrom != null && local >= s.outFrom) {
      el.classList.add('leaving');
      el.style.setProperty('--x',
        Math.min(1, (local - s.outFrom) / Math.max(1, s.outLen)).toFixed(5));
    }
    if (s.inLen && local < s.inLen) {
      el.classList.add('entering');
      el.style.setProperty('--e', Math.min(1, local / s.inLen).toFixed(5));
    }

    // Deterministic frames: pause every animation and place it by hand.
    //
    // Two clocks, and conflating them was a real bug. Everything INSIDE the
    // scene runs on scene time — an element that rises 200ms in should be
    // 200ms into its rise. But an animation on the scene ELEMENT while it is
    // leaving is the transition, and the transition starts at outFrom, not at
    // the top of the scene.
    //
    // Given scene time, a 900ms exit animation seeked to 2500ms is long past
    // its end, so `.leaving{animation:slideOut 900ms both}` snapped straight
    // to its final frame the instant the class landed. Every exit in every
    // reel was a hard pop, and it looked like a design problem.
    let own = [];
    try { own = el.getAnimations(); } catch (e) {}
    const ownSet = new Set(own);
    const leaving = s.outFrom != null && local >= s.outFrom;
    for (const a of own) {
      try {
        a.pause();
        a.currentTime = leaving ? Math.max(0, local - s.outFrom) : local;
      } catch (e) {}
    }
    let anims = [];
    try { anims = el.getAnimations({ subtree: true }); } catch (e) {}
    for (const a of anims) {
      if (ownSet.has(a)) continue;      // already placed on the other clock
      try { a.pause(); a.currentTime = local; } catch (e) {}
    }
  }
  return true;
};

// Top and bottom of what a viewer reads as the scene's content: leaf text,
// pictures, and shapes with a fill or border of their own. A ground that covers
// most of the frame is the background, not content.
window.__extent = function (scene) {
  let top = Infinity, bottom = -Infinity;
  for (const el of scene.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.05) continue;
    const tag = el.tagName.toLowerCase();
    const isText = !el.children.length && (el.textContent || '').trim();
    const isPic = tag === 'img' || tag === 'svg' || tag === 'picture' || tag === 'video';
    const filled = cs.backgroundColor !== 'rgba(0, 0, 0, 0)' || cs.borderTopStyle !== 'none';
    if (!isText && !isPic && !filled) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.width * r.height > 0.8 * __W__ * __H__) continue;
    top = Math.min(top, r.top); bottom = Math.max(bottom, r.bottom);
  }
  return top === Infinity ? null : { top: Math.max(0, top), bottom: Math.min(__H__, bottom) };
};

// Legibility check, run on the page rather than guessed at from Python: the
// browser is the only thing that knows where the text actually landed.
window.__check = function () {
  const out = [];
  const seen = new Set();
  const scenes = document.querySelectorAll('.scene.on');
  for (const scene of scenes) {
    let visibleContent = 0;
    const texts = [];
    for (const el of scene.querySelectorAll('*')) {
      const txt = (el.textContent || '').trim();
      if (!txt || el.children.length) continue;      // leaf text nodes only
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
      // Faded out by an ancestor is faded out: a label inside a panel that
      // has not arrived yet is not on screen, whatever its own opacity says.
      let faded = false;
      for (let p = el.parentElement; p && p !== scene; p = p.parentElement) {
        const pc = getComputedStyle(p);
        if (pc.display === 'none' || pc.visibility === 'hidden' ||
            parseFloat(pc.opacity) < 0.05) { faded = true; break; }
      }
      if (faded) continue;
      visibleContent++;
      const label = txt.slice(0, 34);
      const key = label + '|';
      texts.push({ el: el, r: r, label: label });
      if (r.left < 20 || r.right > %d - 20 || r.top < 20 || r.bottom > %d - 20) {
        if (!seen.has(key + 'box')) {
          seen.add(key + 'box');
          out.push('"' + label + '" is outside the frame');
        }
      }
      const fs = parseFloat(cs.fontSize);
      if (fs < %d && !seen.has(key + 'sz')) {
        seen.add(key + 'sz');
        out.push('"' + label + '" is ' + Math.round(fs) +
                 'px — under the %dpx minimum for video');
      }

      // A box can be inside the frame and still be unusable: a later opaque
      // panel, image or decorative layer may be painted on top of it. Sample
      // a 3x3 grid; two samples landing on a sibling painted above the text
      // is a panel across it, not a hairline. Five samples and "all five"
      // was the old rule — the two white blocks that hid half a page
      // counter on the 2026-09-07 reel covered two of five and passed.
      // Text over text is left to the overlap pass below, which names both.
      const pts = [];
      for (const fx of [.15, .5, .85]) {
        for (const fy of [.25, .5, .75]) {
          pts.push([r.left + r.width * fx, r.top + r.height * fy]);
        }
      }
      let covered = 0, by = null;
      for (const [x, y] of pts) {
        const top = document.elementFromPoint(x, y);
        if (!top || top === el || el.contains(top) || top.contains(el)) continue;
        if (!top.children.length && (top.textContent || '').trim()) continue;
        const tc = getComputedStyle(top);
        if (tc.visibility !== 'hidden' && parseFloat(tc.opacity) >= .08) {
          covered++;
          by = by || top;
        }
      }
      if (covered >= 2 && !seen.has(key + 'occluded')) {
        seen.add(key + 'occluded');
        const cls = (by.className && typeof by.className === 'string')
          ? ' class="' + by.className.split(' ')[0] + '"' : '';
        out.push('"' + label + '" is ' +
                 (covered === pts.length ? 'covered' : 'partly covered') +
                 ' by a higher layer (<' + by.tagName.toLowerCase() + cls +
                 '>) — nothing may sit across copy; move it, or raise the text z-index');
      }
    }

    // Two texts printed over each other. The commonest fault on a real reel
    // and, until now, the one this check could not see at all: each text
    // was inside the frame, large enough, and (mostly) on top of whatever
    // was under it. A running header colliding with a date, a footer under
    // a caption, a vertical label through a headline — every one passed.
    for (let i = 0; i < texts.length; i++) {
      for (let j = i + 1; j < texts.length; j++) {
        const a = texts[i].r, b = texts[j].r;
        const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ix <= 3 || iy <= 3) continue;
        const small = Math.min(a.width * a.height, b.width * b.height);
        if (ix * iy < 0.06 * small) continue;     // a kiss between neighbours
        const k2 = 'ovl|' + texts[i].label + '|' + texts[j].label;
        if (seen.has(k2)) continue;
        seen.add(k2);
        out.push('"' + texts[i].label + '" and "' + texts[j].label +
                 '" overlap — two texts printed over each other; give each its own space');
      }
    }

    // Images are checked too: a logo half off the frame is exactly as broken
    // as a headline half off it, and only the browser knows where it landed.
    for (const el of scene.querySelectorAll('img, svg, picture')) {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
      visibleContent++;
      if (el.tagName.toLowerCase() === 'img' && el.complete && el.naturalWidth === 0) {
        const key = 'broken|' + (el.getAttribute('alt') || 'image');
        if (!seen.has(key)) {
          seen.add(key);
          out.push('an image failed to load — replace the missing asset or remove the image slot');
        }
      }
      const frameW = %d, frameH = %d;
      // A moving picture -- a slow zoom, a pan -- legitimately overshoots its
      // own box mid-animation; the whole point of the CSS trick that makes
      // that safe is a clipping wrapper (overflow:hidden) that masks the
      // overshoot, so nothing the viewer ever actually sees leaves the
      // frame. Checked against that wrapper's OWN box instead, which does
      // not move with the animation: an image is only flagged when no such
      // wrapper contains it, or the wrapper itself sits off frame -- a real
      // mistake, not a settling entrance animation ("an image (img,
      // 719x1937) runs off the frame" on a scene whose .s1-photo wrapper,
      // overflow:hidden, sat exactly at 367,0,713,1920 -- the owner's run
      // of 15 Sep 2026, after the mid-animation scale(1.00868) on the img
      // inside it was measured directly instead).
      let bound = r;
      for (let p = el.parentElement; p && p !== scene; p = p.parentElement) {
        const pcs = getComputedStyle(p);
        if (pcs.overflow === 'hidden' || pcs.overflow === 'clip' ||
            pcs.overflowX === 'hidden' || pcs.overflowX === 'clip' ||
            pcs.overflowY === 'hidden' || pcs.overflowY === 'clip') {
          bound = p.getBoundingClientRect();
          break;
        }
      }
      if (bound.left < -2 || bound.right > frameW + 2 ||
          bound.top < -2 || bound.bottom > frameH + 2) {
        const what = el.getAttribute('alt') || el.tagName.toLowerCase();
        const key = 'img|' + what + Math.round(r.left);
        if (!seen.has(key)) {
          seen.add(key);
          out.push('an image (' + what + ', ' + Math.round(r.width) + 'x' +
                   Math.round(r.height) + ') runs off the frame');
        }
      }
      const clip = cs.clipPath || '';
      if (clip && clip.includes('polygon') && clip !== 'none') {
        const key = 'polygon_clip|' + (el.getAttribute('alt') || el.tagName);
        if (!seen.has(key)) {
          seen.add(key);
          out.push('product or artwork has a diagonal polygon clip-path (' + clip.slice(0, 40) + ') which slices off the artwork; use clean rectangular cards, rounded corners, arches, or unclipped containers');
        }
      }
    }

    // CSS animations are the motion contract. A missing fill mode causes a
    // visible snap at scene boundaries when the renderer seeks frame by
    // frame; transitions and JS timers are not filmable either.
    for (const el of scene.querySelectorAll('*')) {
      const cs = getComputedStyle(el);
      if (cs.visibility !== 'hidden' && parseFloat(cs.opacity) >= .05 &&
          (cs.backgroundColor !== 'rgba(0, 0, 0, 0)' ||
           cs.borderTopStyle !== 'none' || cs.borderRightStyle !== 'none' ||
           cs.borderBottomStyle !== 'none' || cs.borderLeftStyle !== 'none')) {
        visibleContent++;
      }
      const names = cs.animationName.split(',').map(x => x.trim());
      const fills = cs.animationFillMode.split(',').map(x => x.trim());
      if (names.some((name, i) => name !== 'none' &&
          fills[i %%%% fills.length] !== 'both')) {
        const key = 'anim|' + cs.animationName;
        if (!seen.has(key)) {
          seen.add(key);
          out.push('animation ' + cs.animationName + ' is missing fill-mode: both — motion may snap');
        }
      }
    }
    // Where the content sits. Both live films of 13 Nov 2026 packed every scene
    // into the middle or top of the frame and left a third of it bare (soft:
    // cards ending 64 percent down; poster: a whole red field with nothing in its upper
    // half), the "empty lower half" the owner had already complained of. Only
    // the browser knows where the content landed, so it is measured here:
    // text, pictures and filled shapes, not full-bleed grounds.
    const ext = window.__extent(scene);
    if (ext) {
      const gapTop = ext.top / __H__, gapBottom = (__H__ - ext.bottom) / __H__;
      const limit = __GAP__;
      const key = 'gap|' + (scene.id || 'scene');
      if ((gapTop > limit || gapBottom > limit) && !seen.has(key)) {
        seen.add(key);
        const bare = [];
        if (gapTop > limit) bare.push('top ' + Math.round(gapTop * 100) + ' percent');
        if (gapBottom > limit) bare.push('bottom ' + Math.round(gapBottom * 100) + ' percent');
        out.push('scene ' + (scene.id || '') + ' leaves the ' + bare.join(' and the ') +
                 ' of the frame empty — its content fills only the band between ' +
                 Math.round(gapTop * 100) + ' and ' + Math.round((1 - gapBottom) * 100) +
                 ' percent of the height. Spread it over the height: set the main element larger, or move the group toward the middle');
      }
    }
    if (visibleContent === 0) {
      const key = 'empty|' + (scene.id || 'scene');
      if (!seen.has(key)) {
        seen.add(key);
        out.push('scene ' + (scene.id || '') + ' has no visible content');
      }
    }
  }
  const d = document.documentElement;
  if (d.scrollWidth > %d || d.scrollHeight > %d) {
    out.push('the page is bigger than the frame (' + d.scrollWidth + 'x' +
             d.scrollHeight + ') — something is overflowing');
  }
  return out;
};
window.__check = (function (f) {
  return function () {
    const st = document.getElementById('stage');
    st.classList.add('nocam');
    try { return f(); } finally { st.classList.remove('nocam'); }
  };
})(window.__check);
""" % ("%s", W, H, T_LABEL, T_LABEL, W, H, W, H)
_HARNESS_JS = (_HARNESS_JS.replace("__W__", str(W)).replace("__H__", str(H))
               .replace("__GAP__", str(EMPTY_BAND)))


def _plan(spec: dict, fps: int):
    """Scene windows in milliseconds, with cuts overlapping their neighbours.

    Returns (scenes_for_js, total_frames). Overlap is capped at a third of
    either neighbour so a short scene is never swallowed by its own cut.
    """
    scenes = spec.get("scenes") or []
    if not scenes:
        raise ReelError("The spec has no scenes.")
    durs = []
    for sc in scenes:
        try:
            secs = float(sc.get("seconds", 4))
        except (TypeError, ValueError):
            secs = 4.0
        durs.append(max(1.5, min(12.0, secs)) * 1000.0)

    cut_ms = float(spec.get("design", {}).get("cut_ms", 420))
    cut_ms = max(0.0, min(1200.0, cut_ms))

    # Each gap is one number shared by the two scenes either side of it: the
    # scene before uses it to leave, the scene after uses it to arrive.
    gaps = [min(cut_ms, durs[i] / 3, durs[i + 1] / 3) if i + 1 < len(durs)
            else 0.0
            for i in range(len(durs))]

    out, t = [], 0.0
    for i, dur in enumerate(durs):
        # inLen was `gaps[i]` — the overlap with the NEXT scene rather than
        # the previous one. Off by one, and mostly invisible because every gap
        # is usually the same cut_ms. Not invisible on the LAST scene, which
        # has no next and so got inLen 0: the final scene of every reel
        # arrived with no transition at all. That scene is the endcard, so the
        # brand moment was the one hard pop in the film.
        #
        # The first scene keeps a window of its own. There is nothing behind
        # it to hand over from, but opening on a move rather than a jump is
        # worth having and costs nothing.
        entering = cut_ms if i == 0 else gaps[i - 1]
        out.append({"start": round(t), "dur": round(dur),
                    "outFrom": round(dur - gaps[i]) if gaps[i] else None,
                    "outLen": round(gaps[i]), "inLen": round(entering)})
        t += dur - gaps[i]
    total_ms = out[-1]["start"] + out[-1]["dur"]
    return out, int(round(total_ms / 1000.0 * fps))


def _asset_uris(table: dict, scene_index: int | None = None) -> dict:
    """Every asset as a data: URI.

    Inlined rather than linked because the page is loaded with set_content and
    has no base URL — a file:// path or a bare filename simply does not
    resolve, and the image silently fails to appear. Inlining also means the
    saved design JSON is the whole reel: re-render it next year and the logo
    is still there.
    """
    import base64
    out = {}
    for name, a in (table or {}).items():
        # A model occasionally returns a storyboard/contact sheet when asked
        # for separate artwork. Never let that composite become a full-frame
        # image in the final video; the design prompt receives the rejection
        # and can fall back to type/CSS or request individual art.
        if isinstance(a, dict) and a.get("composite"):
            # A board is reference material, not a production image. Guessed
            # 4+3 crops selected by scene index were unrelated to the copy.
            # Deliberate single-image crops must be collected as new assets.
            continue
        else:
            path = a.get("path") if isinstance(a, dict) else a
        # Specs written before the contact-sheet metadata existed are still
        # safe: inspect the source file lazily when they are re-rendered.
        if isinstance(a, dict) and path:
            try:
                from .assets import looks_like_contact_sheet
                if looks_like_contact_sheet(path):
                    continue
            except Exception:
                pass
        try:
            if not path or os.path.getsize(path) > 6_000_000:
                continue
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        ext = os.path.splitext(path)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/png")
        out[name] = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return out


def _place_assets(text: str, uris: dict) -> str:
    """Swap asset:name for the real thing. Longest name first, so asset:art1
    can never eat the front of asset:art10."""
    for name in sorted(uris, key=len, reverse=True):
        text = text.replace(f"asset:{name}", uris[name])
    return text


def missing_assets(spec: dict) -> list[str]:
    """Asset names the design asks for that were never made."""
    import re
    # The same resolver as preview/export: a stale path or rejected board is
    # not available merely because its name is present in the manifest.
    have = set(_asset_uris(spec.get("_assets") or {})) - _blocked_assets(spec.get("design") or {})
    used = set()
    blobs = [(spec.get("design") or {}).get("css", "")]
    for sc in (spec.get("scenes") or []):
        blobs += [sc.get("html", ""), sc.get("css", "")]
    for blob in blobs:
        used.update(re.findall(r"asset:([A-Za-z0-9_-]+)", str(blob)))
    return sorted(used - have)


def _blocked_assets(design: dict) -> set[str]:
    """Honor the visual review in the saved project, not only in prose."""
    flags = design.get("asset_flags") or []
    if not isinstance(flags, list):
        return set()
    return {str(flag.get("asset") or flag.get("name") or "").removeprefix("asset:")
            for flag in flags if isinstance(flag, dict)
            and str(flag.get("status", "")).lower() in
            {"unusable", "reference-only", "reference_only"}}


def _asset_names(listing: str) -> list[str]:
    """The names on an asset list, in order. Only lines that START with a
    name count, so the NO_ARTWORK instruction — which mentions
    `asset:anything` mid-sentence to forbid it — yields none."""
    return list(dict.fromkeys(
        match.group(1)
        for line in (listing or "").splitlines()
        if not re.search(r"REJECTED|REFERENCE[- ]ONLY|UNUSABLE", line, re.I)
        for match in [re.match(r"\s*asset:([A-Za-z0-9_-]+)", line)] if match))


def planned_assets(row: dict, listing: str) -> list[str]:
    """The artwork a storyboard row assigned to its scene — by name, and only
    names that actually exist on the list. A row may write `assets`,
    `artwork` or `asset`, as a list or a string; a model is not a schema."""
    have = set(_asset_names(listing))
    if not have:
        return []
    raw = None
    for key in ("assets", "artwork", "asset", "images", "image"):
        if key in (row or {}):
            raw = row[key]
            break
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.findall(r"[A-Za-z0-9_-]+", raw)
    out = []
    for name in raw if isinstance(raw, (list, tuple)) else []:
        name = str(name).strip()
        if name.lower().startswith("asset:"):
            name = name[6:]
        if name in have and name not in out:
            out.append(name)
    return out


def missing_planned(scene: dict, planned: list[str]) -> list[str]:
    """Planned artwork the scene did not place, as faults the writer can act
    on — phrased like the layout faults so the two travel in one list."""
    blob = str((scene or {}).get("html", "")) + str((scene or {}).get("css", ""))
    return [f"asset:{n} was planned for this scene in your storyboard and "
            f"does not appear in it — place it (<img src='asset:{n}' alt=''> "
            f"or background-image: url(asset:{n}))"
            for n in planned if f"asset:{n}" not in blob]


def ensure_accent_applied(spec: dict) -> dict:
    """Ensure the client's brand accent colour is actively used in the design.

    If brand accent is specified but not referenced in the design CSS or scene
    markup, injects targeted CSS rules applying var(--accent) to prominent focal
    elements (e.g. .kicker, .accent, .highlight, h1 em, borders, badges).
    """
    brand = spec.get("brand") or {}
    accent = str(brand.get("accent", "")).strip()
    if not accent:
        return spec
    blob = (str((spec.get("design") or {}).get("css", "")) + " " +
            " ".join(str(sc.get("html", "")) + " " + str(sc.get("css", ""))
                     for sc in spec.get("scenes") or [])
            ).lower()
    if "var(--accent" in blob.replace(" ", "") or accent.lower() in blob:
        return spec

    design = spec.setdefault("design", {})
    existing_css = str(design.get("css") or "")
    accent_rules = (
        f"\n/* Auto-applied client brand accent ({accent}) */\n"
        f":root {{ --accent: {accent}; }}\n"
        f".kicker, .accent, .highlight, [data-prism-id*='accent'], "
        f"h1 em, h2 em, .badge {{ color: var(--accent) !important; }}\n"
        f".accent-bg, .badge-accent, .pill-accent {{ background-color: var(--accent) !important; }}\n"
        f".accent-border {{ border-color: var(--accent) !important; }}\n"
    )
    design["css"] = (existing_css + "\n" + accent_rules).strip()
    return spec


def brand_faults(spec: dict) -> list[str]:
    """Did the design actually use the client's colours?

    Passing --accent into the page guarantees it is available, not that it is
    used — and an art director handed a green company will cheerfully return
    a blue reel. Measuring the colours and then not checking they survived
    was the gap between "Prism matches your brand" and Prism actually doing
    it.
    """
    brand = spec.get("brand") or {}
    accent = str(brand.get("accent", "")).strip().lower()
    if not accent:
        return []
    blob = (str((spec.get("design") or {}).get("css", "")) + " " +
            " ".join(str(sc.get("html", "")) + " " + str(sc.get("css", ""))
                     for sc in spec.get("scenes") or [])
            ).lower()
    if "var(--accent" in blob.replace(" ", "") or accent in blob:
        return []
    return [f"the client's accent colour {accent} appears nowhere in the "
            "design — use var(--accent) for the element the eye goes to first "
            "in each scene, or the reel is not in their colours"]



def structural_faults(spec: dict) -> list[str]:
    """Checks that do not require a browser: timing, identity and content."""
    faults = []
    scenes = spec.get("scenes") or []
    ids = set()
    for i, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict) or not str(scene.get("html", "")).strip():
            faults.append(f"scene {i} has no HTML content")
            continue
        sid = str(scene.get("studio_id", f"scene-{i}"))
        if sid in ids:
            faults.append(f"scene {i} reuses studio id {sid} — selection and edits are ambiguous")
        ids.add(sid)
        try:
            seconds = float(scene.get("seconds", 4))
        except (TypeError, ValueError):
            faults.append(f"scene {i} has an invalid duration")
            continue
        if not 1.5 <= seconds <= 12:
            faults.append(f"scene {i} duration {seconds:g}s is outside the supported 1.5–12s range")
    return faults


def _drop_missing(text: str) -> str:
    """Remove what points at artwork that does not exist.

    Asked-for-but-absent assets are stripped rather than left in place,
    because an unresolved src renders as a broken-image glyph and an
    unresolved url() renders as a blank box — both of which end up in the
    finished video. A missing picture should leave no trace, not a hole with
    an icon in it.
    """
    import re
    if "asset:" not in text:
        return text
    # Whole elements whose source is missing.
    text = re.sub(r"<(img|image|picture|video)\b[^>]*\basset:[^>]*?>", "",
                  text, flags=re.I)
    # A missing background is simply no background.
    text = re.sub(r"url\(\s*['\"]?asset:[A-Za-z0-9_-]+['\"]?\s*\)", "none",
                  text, flags=re.I)
    # Anything left is an attribute we do not know; empty it.
    text = re.sub(r"asset:[A-Za-z0-9_-]+", "", text)
    return text


def strip_citations(text: str) -> str:
    """Remove AI citation markers and footnote tokens like
    :contentReference[oaicite:0]{index=0}, [oaicite:0], 【4:0†source】."""
    import re
    if not text:
        return ""
    text = re.sub(r":?contentReference\[[^\]]*\](\{[^\}]*\})?", "", text)
    text = re.sub(r"\[oaicite:[^\]]*\]", "", text)
    text = re.sub(r"【[^】]*】", "", text)
    return text


# ── per-scene CSS, confined to its own scene ────────────────────────────────
# Scenes are written one at a time now, in separate replies, and a model
# naming things in reply four cannot see what it called them in reply two.
# Left alone they collide: everybody writes `.title`, everybody writes
# `@keyframes rise`, and whichever scene loses the cascade silently inherits
# another scene's type size or another scene's motion.
#
# Scoping is what makes writing a scene at a time safe, and it is worth more
# than the collisions it prevents: because the scene cannot reach outside
# itself, the prompt does not have to ask it to be careful. It may call
# things whatever it likes.

_SCENE_CLASSES = {"scene", "leaving", "entering", "on"}


def _top_level_rules(css: str) -> list[tuple[str, str | None]]:
    """Split CSS into (prelude, body) pairs at brace depth zero.

    Written by hand rather than with a regex because the things that break a
    regex here are all common in real stylesheets: braces inside strings,
    `url(data:…{…})`, comments, and nested at-rules. `body` is None for a
    statement at-rule such as `@import …;`.
    """
    rules: list[tuple[str, str | None]] = []
    buf: list[str] = []
    prelude = ""
    depth = 0
    quote = None
    i, n = 0, len(css)
    while i < n:
        c = css[i]
        if quote:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(css[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and css[i + 1] == "*":
            end = css.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        # An unquoted url() is a single token to CSS, and its contents are not
        # CSS at all — a stray brace or semicolon in a path or a data: URI
        # would otherwise be read as structure and cut the stylesheet in half.
        if (c == "u" or c == "U") and css[i:i + 4].lower() == "url(":
            end = css.find(")", i + 4)
            end = n if end < 0 else end + 1
            buf.append(css[i:end])
            i = end
            continue
        if c == "{":
            if depth == 0:
                prelude = "".join(buf).strip()
                buf = []
            else:
                buf.append(c)
            depth += 1
        elif c == "}":
            depth -= 1
            if depth <= 0:
                depth = 0
                rules.append((prelude, "".join(buf)))
                buf, prelude = [], ""
            else:
                buf.append(c)
        elif c == ";" and depth == 0:
            stmt = "".join(buf).strip()
            if stmt:
                rules.append((stmt + ";", None))
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        # An unclosed rule. Kept rather than dropped: half a scene styled is
        # better than none, and the browser is forgiving about the missing
        # brace in a way this parser does not need to be.
        rules.append((prelude, tail) if prelude else (tail, None))
    return rules


def _scope_selector(sel: str, root: str) -> str:
    """One selector, confined to the scene element `root` (`#s3`).

    Three cases, and the middle one is the one that matters. `.leaving` means
    the scene element ITSELF, not something inside it — a scene writing its
    own custom cut is styling its own layer. Prefixing it as a descendant
    would silently disable every hand-written transition.
    """
    sel = sel.strip()
    if not sel:
        return ""
    if sel.startswith(root):
        return sel                            # already ours
    if sel in ("html", "body", ":root", "*, *::before, *::after"):
        return root
    head = re.split(r"[\s>+~]", sel, maxsplit=1)[0]
    classes = re.findall(r"\.([\w-]+)", head)
    if classes and set(classes) <= _SCENE_CLASSES and \
            re.fullmatch(r"(?:\.[\w-]+)+", head):
        return root + sel                     # #s3.leaving, #s3.scene.entering
    return f"{root} {sel}"


_AT_NESTED = ("@media", "@supports", "@container", "@layer", "@scope")
_AT_VERBATIM = ("@font-face", "@import", "@charset", "@namespace",
                "@property", "@counter-style", "@font-feature-values")


def _scope(css: str, root: str, prefix: str, renames: dict) -> str:
    out = []
    for prelude, body in _top_level_rules(css):
        if body is None:
            out.append(prelude)               # @import …; — global by nature
            continue
        low = prelude.lower()
        if low.startswith("@keyframes") or low.startswith("@-webkit-keyframes"):
            m = re.match(r"(@(?:-webkit-)?keyframes\s+)(.+)$", prelude,
                         flags=re.I | re.S)
            if m:
                name = m.group(2).strip().strip("'\"")
                renames[name] = f"{prefix}{name}"
                prelude = m.group(1) + renames[name]
            out.append(f"{prelude}{{{body}}}")
            continue
        if low.startswith(_AT_VERBATIM):
            out.append(f"{prelude}{{{body}}}")
            continue
        if low.startswith(_AT_NESTED):
            out.append(f"{prelude}{{{_scope(body, root, prefix, renames)}}}")
            continue
        if prelude.startswith("@"):
            out.append(f"{prelude}{{{body}}}")
            continue
        scoped = ", ".join(p for p in
                           (_scope_selector(s, root) for s in prelude.split(","))
                           if p)
        out.append(f"{scoped or root}{{{body}}}")
    return "".join(out)


def sanitize_scene_css(css: str) -> str:
    """Neutralize destructive polygon clip-paths on images and cards.
    A model told to make an 'editorial crop' sometimes writes
    clip-path: polygon(...) which slices diagonally through products and text.
    """
    css = str(css or "").strip()
    if not css:
        return ""
    return re.sub(r"clip-path\s*:\s*polygon\([^)]+\)\s*;?", "/* clip-path sanitized */", css, flags=re.I)


def scope_css(css: str, idx: int) -> str:
    """A scene's own stylesheet, unable to reach any other scene."""
    css = sanitize_scene_css(str(css or "").strip())
    if not css:
        return ""
    root, prefix = f"#s{idx}", f"s{idx}-"
    renames: dict[str, str] = {}
    out = _scope(css, root, prefix, renames) or ""
    if renames:
        # The keyframes were renamed, so every reference to them has to move
        # too. Restricted to animation declarations on purpose: a scene may
        # well have a CLASS called `rise` alongside its `@keyframes rise`, and
        # renaming that would break the markup.
        def fix(m):
            value = m.group(2)
            for old, new in renames.items():
                value = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", new,
                               value)
            return m.group(1) + value

        out = re.sub(r"(animation(?:-name)?\s*:\s*)([^;{}]*)", fix, out,
                     flags=re.I)
    return out


def font_urls(families) -> list[str]:
    """Google Fonts stylesheet URLs for the design's `google_fonts` list.

    The design is asked for "family with only the required weights" and writes
    that as "Anton:400" or "Inter:400,700" — the old v1 spelling. Pasted into a
    css2 URL as written, Google answers HTTP 400, no face is registered and the
    whole film is set in the fallback sans (found 13 Nov 2026 on the first live
    "poster" film: Anton chosen, Arial filmed). So the weights are rewritten to
    css2's `:wght@400;700`, and the bare family is always asked for as well: a
    weight the family does not have fails the weighted request, and the film
    must still get the family.
    """
    urls = []
    for family in (families or [])[:3]:
        name, _, tail = str(family).strip().partition(":")
        if not tail:                          # "Inter 400 700"
            got = re.match(r"^(.*?\D)[\s,]+(\d{3}(?:[\s,;]+\d{3})*)$", name)
            if got:
                name, tail = got.group(1), got.group(2)
        name = re.sub(r"\s+", "+", name.strip().strip("'\""))
        if not name:
            continue
        base = f"https://fonts.googleapis.com/css2?family={name}"
        urls.append(base + "&display=block")
        tail = tail.strip().replace(" ", "")
        if "@" in tail:                       # already css2: wght@400;700, ital,wght@0,400;1,400
            urls.append(f"{base}:{tail}&display=block")
        else:
            weights = sorted({int(w) for w in re.findall(r"\d{3}", tail)})
            if weights and weights != [400]:
                urls.append(f"{base}:wght@{';'.join(map(str, weights))}&display=block")
    return urls


def build_html(spec: dict, fps: int = DEFAULT_FPS) -> str:
    """The whole reel as one self-contained page.

    The design's CSS is placed AFTER the harness so the designer can override
    anything except the frame itself, and the scene markup is inserted as
    written — this is the part that is meant to differ from client to client.
    """
    # Every page built from a spec carries the same durable element ids, so
    # a Studio edit saved against the editor page finds its layer on the
    # render page too — including a spec that predates parse_spec() stamping
    # them, or one built by hand in a test. Idempotent, so this is cheap.
    from . import reel_edit
    reel_edit.ensure_stable_ids(spec)
    design = spec.get("design") or {}
    scenes = spec.get("scenes") or []
    asset_table = {name: value for name, value in (spec.get("_assets") or {}).items()
                   if name not in _blocked_assets(design)}
    plan, _ = _plan(spec, fps)

    fonts = "".join(f'<link rel="stylesheet" href="{u}">'
                    for u in font_urls(design.get("google_fonts")))

    brand = spec.get("brand") or {}
    root_vars = ";".join(f"--{k}:{v}" for k, v in brand.items()
                         if isinstance(v, str) and v.strip())

    body, scene_css = [], []
    for i, sc in enumerate(scenes):
        # Resolve only usable assets; boards remain references, never tiles
        # picked implicitly by scene number.
        uris = _asset_uris(asset_table, scene_index=i)
        html = strip_citations(_drop_missing(_place_assets(sc.get("html") or "", uris)))
        # A scene may name the cut it wants ("push", "squeeze", "zoom") and
        # get it from the library in the harness. Sanitised rather than
        # trusted: this string becomes a class attribute, and a design is
        # written by a language model reading a customer's own words.
        cut = re.sub(r"[^a-z0-9-]", "", str(sc.get("cut", "")).strip().lower())
        klass = f"scene cut-{cut}" if cut else "scene"
        studio_id = re.sub(r"[^A-Za-z0-9_-]", "", str(
            sc.get("studio_id", f"scene-{i + 1}"))) or f"scene-{i + 1}"
        body.append(f'<section class="{klass}" id="s{i}" '
                    f'data-type="{sc.get("type", "")}" '
                    f'data-prism-scene="{studio_id}">{html}</section>')
        # A scene written on its own turn brings its own stylesheet. Scoped
        # here rather than trusted to be careful — see scope_css.
        own = scope_css(_drop_missing(_place_assets(sc.get("css") or "", uris)),
                        i)
        if own:
            scene_css.append(own)

    design_uris = _asset_uris(asset_table)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"{fonts}"
        f"<style>{_HARNESS_CSS}</style>"
        f"<style>:root{{{root_vars}}}</style>"
        f"<style>{_drop_missing(_place_assets(sanitize_scene_css(design.get('css', '')), design_uris))}</style>"
        f"<style>{''.join(scene_css)}</style>"
        # A scene's own CSS commonly includes `.scene{position:relative}`.
        # Because scoping intentionally allows that selector to target the
        # scene root, it can override the harness's absolute stage layer and
        # make later scenes flow below the 1920px frame. Keep the film layers
        # pinned after all designer CSS while leaving inner composition rules
        # untouched.
        "<style>#stage > .scene{position:absolute!important;inset:0!important;"
        "width:1080px!important;height:1920px!important;overflow:hidden!important;}"
        "</style>"
        "</head><body>"
        f"<div id='stage'>{''.join(body)}</div>"
        f"<script>{_HARNESS_JS % json.dumps(plan)}</script>"
        "</body></html>"
    )


def _seek_page(page, milliseconds: float) -> None:
    """Wait for paint after a discontinuous animation seek.

    SVG stroke rasterization can lag behind computed animation styles by a
    frame. Two animation frames allow style invalidation and paint to settle
    without advancing the paused scene clock or relying on a fixed sleep.
    """
    page.evaluate("""async t => {
        window.__seek(t);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""", milliseconds)


def render(spec: dict, out_path: str, on_progress=None,
           check: bool = True, *, capture_quality: int = 95,
           crf: int = 19) -> str:
    """Draw the reel in a browser and encode it.

    JPEG frames are piped straight into FFmpeg — no temp directory of a
    thousand images, and no re-encode of anything.
    """
    ok, why = available()
    if not ok:
        raise ReelError(why)
    # Before the browser starts, not at minute four of the encode. See
    # ffmpeg.check_space — KNOWN_ISSUES #12.
    from . import ffmpeg as ffmpeg_tool
    no_room = ffmpeg_tool.check_space(out_path)
    if no_room:
        raise ReelError(no_room)
    from playwright.sync_api import sync_playwright

    fps = int(spec.get("fps", DEFAULT_FPS))
    # The owner's own hand fixes, made in the browser editor. Applied by the
    # SAME script the editor runs, so the film cannot differ from what they
    # saw when they pressed Save & render. A scene length set there is a
    # plan-time fact and goes into the spec before the windows are cut.
    edits = []
    filmed = spec
    if spec.get("edits"):
        from . import reel_edit
        reel_edit.ensure_stable_ids(spec)
        edits = reel_edit.clean_edits(spec["edits"])
        filmed = reel_edit.with_timing(spec, edits)
    plan, total = _plan(filmed, fps)
    html = build_html(filmed, fps)
    if edits:
        html = reel_edit.apply_edits(html, edits)
    exe = ffmpeg_path()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    # JPEG, not PNG, and it is not a close call. Measured on a real design:
    # seeking the page costs 1 ms a frame, PNG-encoding it costs 303, JPEG at
    # quality 95 costs 41. PNG was ~85% of the total render time, spent
    # losslessly compressing a frame that is about to be thrown through H.264
    # anyway — where the difference is invisible.
    capture_quality = max(1, min(100, int(capture_quality)))
    crf = max(0, min(51, int(crf)))
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    faults: list[str] = []
    from . import browser as _browser
    with sync_playwright() as p:
        browser = _browser.launch_chromium(p, args=[
            "--force-color-profile=srgb",
            "--font-render-hinting=none",
            "--disable-lcd-text",
            "--hide-scrollbars",
        ])
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        try:
            page.set_content(html, wait_until="load")
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(()=>true)",
                                       timeout=8000)
            except Exception:
                pass   # a web font that never arrives must not stop the render

            if check:
                # Look at a settled frame of every scene BEFORE encoding
                # anything: a reel that fails the check is worth catching in
                # seconds rather than after a minute of rendering.
                filmed = ensure_accent_applied(filmed)
                faults.extend(structural_faults(filmed))
                bfaults = brand_faults(filmed)
                if bfaults:
                    filmed = ensure_accent_applied(filmed)
                    bfaults = brand_faults(filmed)
                faults.extend(bfaults)
                for name in missing_assets(filmed):
                    faults.append(f'asset:{name} is referenced but unavailable')
                for s in plan:
                    _seek_page(page, s["start"] + s["dur"] * 0.75)
                    for fault in page.evaluate("() => window.__check()") or []:
                        if fault not in faults:
                            faults.append(fault)

                # Fatal faults are those that make rendering impossible (missing HTML, invalid timing).
                # Brand accent and minor styling faults are recorded in spec["_faults"] so they can be
                # reviewed and refined, but do not prevent the MP4 from being written.
                fatal_faults = [f for f in faults if "has no HTML content" in f or "duration" in f
                                or "outside the supported" in f]
                if fatal_faults:
                    preview = "; ".join(fatal_faults[:8])
                    more = f" (+{len(fatal_faults) - 8} more)" if len(fatal_faults) > 8 else ""
                    raise ReelError(
                        "Render preflight failed; no MP4 was written: "
                        + preview + more)

            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            try:
                for f in range(total):
                    _seek_page(page, f * 1000.0 / fps)
                    proc.stdin.write(page.screenshot(type="jpeg", quality=capture_quality))
                    if on_progress and (f + 1) % fps == 0:
                        on_progress(f + 1, total)
                proc.stdin.close()
            except BrokenPipeError:
                err = proc.stderr.read().decode("utf-8", "ignore")
                raise ReelError(f"FFmpeg stopped early: {err[:400]}")
            code = proc.wait()
            if code != 0:
                err = proc.stderr.read().decode("utf-8", "ignore")
                proc.stderr.close()
                raise ReelError(f"FFmpeg failed (exit {code}): {err[:400]}")
            proc.stderr.close()
        finally:
            browser.close()

    spec["_faults"] = faults
    return out_path


def rhythm_probe(spec: dict, sample_fps: int = 10, width: int = 192) -> dict | None:
    """The reel's rhythm (core.motion.rhythm): the page played at low resolution,
    `sample_fps` frames a second, and measured. None when there is no browser.
    The same numbers motion.rhythm.probe gives a Motion film and
    rhythm.probe_video gives any finished file."""
    ok, _why = available()
    if not ok:
        return None
    import numpy as np
    from io import BytesIO
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from . import browser as _browser
    from .motion import rhythm as _rhythm
    fps = int(spec.get("fps", DEFAULT_FPS))
    _plan_rows, total = _plan(spec, fps)
    html = build_html(spec, fps)
    frames = []
    with sync_playwright() as p:
        browser = _browser.launch_chromium(p, args=["--hide-scrollbars", "--font-render-hinting=none"])
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=min(1.0, width / W))
        try:
            page.set_content(html, wait_until="load")
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(()=>true)", timeout=8000)
            except Exception:
                pass
            for n in range(int(total / fps * sample_fps)):
                _seek_page(page, n * 1000.0 / sample_fps)
                frames.append(np.asarray(Image.open(BytesIO(page.screenshot(type="png"))).convert("L"), dtype=np.float32))
        finally:
            browser.close()
    return _rhythm.profile_from_frames(frames, sample_fps)


def settled_frames(spec: dict, at: float = 0.8, width: int = 540) -> list[tuple[int, bytes]]:
    """One PNG per scene, `at` of the way through it, `width` wide, for the vision
    critic (core.motion.critic). [] when there is no browser."""
    ok, _why = available()
    if not ok:
        return []
    from playwright.sync_api import sync_playwright
    from . import browser as _browser
    fps = int(spec.get("fps", DEFAULT_FPS))
    plan, _total = _plan(spec, fps)
    html = build_html(spec, fps)
    out: list[tuple[int, bytes]] = []
    with sync_playwright() as p:
        browser = _browser.launch_chromium(p, args=["--hide-scrollbars", "--font-render-hinting=none"])
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=min(1.0, width / W))
        try:
            page.set_content(html, wait_until="load")
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(()=>true)", timeout=8000)
            except Exception:
                pass
            for i, s in enumerate(plan[:12]):
                _seek_page(page, s["start"] + s["dur"] * at)
                out.append((i, page.screenshot(type="png")))
        finally:
            browser.close()
    return out


def _repair_rhythm(scenes: list[dict], design: dict, ask, say, measure, check=None, assets_table=None) -> None:
    """Film-level pass the per-scene layout check cannot make: the assembled reel
    is played at low resolution and its rhythm measured. A reel that is mostly
    still (or never rests) sends its worst scenes back once each with the
    measured numbers; the fix is kept only if the whole reel lands clearly nearer
    the 15-45 % still-frame band. In place; never raises."""
    from .motion import rhythm as _rhythm

    def spec_of(rows):
        out = {"design": design, "scenes": rows}
        if assets_table:
            out["_assets"] = assets_table
        return out

    try:
        profile = measure(spec_of(scenes))
    except Exception as e:                              # noqa: BLE001
        say(f"rhythm: could not be measured ({e})")
        return
    what = _rhythm.kind(profile)
    if not what:
        return
    durations = [float(s.get("seconds") or 0.0) for s in scenes]
    for idx in _rhythm.targets(profile, durations, what):
        activity = _rhythm.scene_activity(profile, durations)[idx]
        say(f"rhythm: {profile['static_pct']:.0f}% of frames are still — sending scene {idx + 1} back")
        fixed = parse_scene(ask(
            _rhythm.repair_text(idx + 1, what, profile, activity, medium="studio")
            + f"\n\nSend the corrected scene {idx + 1}: ONLY the JSON object, same keys, in a ```json fenced block.",
            SCENE_EXPECT) or "")
        if not fixed:
            say(f"   scene {idx + 1} never came back — keeping the first and stopping the rhythm pass")
            break
        fixed.setdefault("seconds", scenes[idx].get("seconds"))
        fixed.setdefault("cut", scenes[idx].get("cut", ""))
        fixed.setdefault("type", scenes[idx].get("type", ""))
        trial = list(scenes)
        trial[idx] = fixed
        try:
            after = measure(spec_of(trial))
            worse_alone = bool(check) and len(check({"design": design, "scenes": [fixed], "_assets": assets_table or {}})) \
                > len(check({"design": design, "scenes": [scenes[idx]], "_assets": assets_table or {}}))
        except Exception:                               # noqa: BLE001
            after, worse_alone = None, True
        if _rhythm.better(profile, after) and not worse_alone:
            scenes[idx] = fixed
            say(f"   scene {idx + 1} fixed — {profile['static_pct']:.0f}% still became {after['static_pct']:.0f}%")
            profile = after
            if not _rhythm.kind(profile):
                break
        else:
            say(f"   scene {idx + 1}'s correction was no better — keeping the first")


def inspect(spec: dict, at: float = 0.75) -> list[str]:
    """Run the legibility check without encoding — used to reject a design
    and ask for a fix before a minute of rendering is spent on it."""
    ok, why = available()
    if not ok:
        raise ReelError(why)
    from playwright.sync_api import sync_playwright
    from . import browser as _browser
    fps = int(spec.get("fps", DEFAULT_FPS))
    edits = []
    if spec.get("edits"):
        from . import reel_edit
        reel_edit.ensure_stable_ids(spec)
        edits = reel_edit.clean_edits(spec["edits"])
        spec = reel_edit.with_timing(spec, edits)
    plan, _ = _plan(spec, fps)
    html = build_html(spec, fps)
    if edits:
        html = reel_edit.apply_edits(html, edits)
    faults: list[str] = []
    with sync_playwright() as p:
        browser = _browser.launch_chromium(p, args=["--hide-scrollbars",
                                                    "--font-render-hinting=none"])
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        try:
            page.set_content(html, wait_until="load")
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(()=>true)",
                                       timeout=8000)
            except Exception:
                pass
            for s in plan:
                _seek_page(page, s["start"] + s["dur"] * at)
                for fault in page.evaluate("() => window.__check()") or []:
                    if fault not in faults:
                        faults.append(fault)
        finally:
            browser.close()

    # Reported even though the renderer strips them: the design asked for a
    # picture that was never made, and the scene it planned around it is now
    # emptier than intended. That is worth one correction.
    for name in missing_assets(spec):
        have = ", ".join((spec.get("_assets") or {}).keys()) or "none at all"
        faults.insert(0, f'the design uses "asset:{name}", which does not '
                          f'exist — the artwork available is: {have}')
    faults[:0] = structural_faults(spec)
    faults[:0] = brand_faults(spec)
    return faults


# ── what the two agent passes are asked for ─────────────────────────────────
# Split deliberately. One mind writing the words AND the art direction in a
# single reply does neither well — it produces a design that describes itself
# ("clean data card", "logo reveal") instead of one that exists. The first
# pass may not mention appearance at all; the second may not change a word.

def script_instructions() -> str:
    # Appended to a router-written brief the same way spec_instructions() is
    # in reel.py. It used to open "THIS OVERRIDES EVERY OTHER FORMATTING
    # INSTRUCTION YOU HAVE BEEN GIVEN" — necessary while the planner wrote a
    # deliverable spec of its own for every step, and a fair description of
    # what the message had become: three sets of format rules from three
    # authors, with the tool left to work out which one meant it. The
    # planner no longer writes format rules at all (router.build_prompt), so
    # there is nothing left to override and the shouting can go.
    return (
        "Context, honestly stated: you are one stage of Prism, a desktop "
        "automation the client's own team launched and is watching; your "
        "reply is parsed by a program on their machine, which is why the "
        "format below is strict.\n\n"
        "OUTPUT FORMAT. Your reply is read by a program, not by a person. "
        "Reply with ONLY a JSON object, wrapped in a ```json fenced code "
        "block and nothing else — no prose before it, none after. The fence "
        "keeps the chat window from reformatting what you wrote. "
        + IN_CHAT_RULE + "\n\n"
        "You are writing the SCRIPT for a short vertical brand reel. Words "
        "and running order only. You do not decide how it looks: a separate "
        "art-direction pass does that, and anything you say about colour, "
        "type, layout or motion will be thrown away.\n\n"
        # The shape, shown with an obviously made-up company. This used to be
        # a realistic sample (a named seed company, real-looking figures),
        # and Claude read it as a smuggled instruction — "the tail end of your
        # prompt demands a JSON schema about <that company>" — and refused,
        # so no JSON came back and the renderer had nothing to build. A
        # placeholder company with placeholder numbers cannot be mistaken
        # for a second brief.
        "SHAPE (an illustration only — the company, wording and numbers are "
        "placeholders; every value must come from THIS task's material):\n"
        '{\n'
        '  "scenes": [\n'
        '    {"role": "hook",    "seconds": 4.5,\n'
        '     "kicker": "<short label>", "headline": "<one-line hook>",\n'
        '     "support": ""},\n'
        '    {"role": "figure",  "seconds": 5,\n'
        '     "kicker": "<what the number is>", "headline": "<the number>",\n'
        '     "support": "<one line of context>",\n'
        '     "note": "<source or disclaimer, if any>"},\n'
        '    {"role": "list",    "seconds": 5.5,\n'
        '     "kicker": "<what these are>", "headline": "",\n'
        '     "items": ["<item 1>", "<item 2>"]},\n'
        '    {"role": "series",  "seconds": 6,\n'
        '     "kicker": "<what is measured>", "headline": "",\n'
        '     "points": [{"label": "<year 1>", "value": 100},\n'
        '                {"label": "<year 2>", "value": 140}],\n'
        '     "unit_prefix": "", "unit_suffix": "",\n'
        '     "note": ""},\n'
        '    {"role": "endcard", "seconds": 4,\n'
        '     "headline": "<Example Company Name>",\n'
        '     "support": "<tagline>",\n'
        '     "contact": "<website or phone>"}\n'
        '  ]\n'
        '}\n\n'
        "ROLES: hook · figure (one number) · series (numbers over time) · "
        "list (things or people named) · statement · brand · endcard (always "
        "last). Use only the roles the story needs; repeat any of them.\n\n"
        "RULES\n"
        "· 4 to 7 scenes, 20 to 35 seconds in total. A viewer reads three "
        "words in under two seconds.\n"
        "· headline under 60 characters, support under 100, kicker under 22, "
        "at most 4 items, at most 6 points.\n"
        "· A number that moves over time goes in 'points' as real numbers. "
        "Never write a series into a sentence.\n"
        "· Every disclaimer, source or asterisk goes in 'note'. An asterisk "
        "with no note points at nothing.\n"
        "· No invented figures, no unsupported claims, no marketing waffle.\n"
        "· Say nothing about how it should look. Not one word."
    )


# The design prompt is built before the imagery stage has run, so the asset
# list cannot be known yet. This stands in its place and is substituted the
# moment before the prompt is typed.
ASSET_TOKEN = "{{ASSETS}}"
# Same reason: when no logo is attached the palette comes from the research
# stage, which has not run when the design prompt is built.
BRAND_TOKEN = "{{BRAND}}"

MAX_GENERATED = 6


def brand_block(brand: dict | None) -> str:
    """How the client's colours are put to the art director."""
    brand = brand or {}
    if not brand:
        return ("\n\nNo brand colours could be established for this client, "
                "so the palette is yours — choose one that suits their trade "
                "and stay disciplined with it.")
    accent = brand.get("accent", "")
    return (
        "\n\nTHE CLIENT'S COLOURS — read off the client's own website by "
        "this pipeline's research step, before your turn (you did not "
        "measure them; treat them as given inputs): "
        + ", ".join(f"{k} {v}" for k, v in brand.items()) +
        ".\nThese are already set as CSS variables (--accent, --deep, --ink, "
        "--bg) and they are the ONE part of this design that is not yours to "
        "choose. Whatever palette you build, the accent colour must be "
        f"theirs: use var(--accent){f' ({accent})' if accent else ''} for the "
        "element the eye goes to first in each scene — the kicker, the rule, "
        "the figure, the active state. A reel in a colour the client does not "
        "own is not their reel, however good it looks, and the design is "
        "checked for this before it is filmed.")


def research_addendum() -> str:
    """Bolted onto whatever the research stage was already asked.

    The research agent is already on the client's website — asking it for the
    palette while it is there costs nothing, where having Prism open the site
    again to measure it would cost a whole extra page load per run.

    The format is rigid on purpose. 'a deep corporate blue' cannot be used;
    '#1B3A5C' can, and a model looking at the page can read one off it.
    """
    return (
        "\n\nONE EXTRA THING, SEPARATE FROM THE TASK ABOVE — THE BRAND'S "
        "COLOURS.\n"
        "Open the company's OWN official website (not a directory listing, "
        "not a news article, not a social profile) and look at it. Note the "
        "two colours the site is actually built from: the one the eye goes to "
        "— buttons, links, the logo, the highlights — and the darker tone "
        "behind headers or headings.\n"
        "Whatever else your answer contains, end it with exactly these two "
        "lines, on their own, with nothing after them:\n"
        "BRAND_ACCENT: #RRGGBB\n"
        "BRAND_DEEP: #RRGGBB\n"
        "Hex codes only. 'A deep corporate blue' is unusable — it has to be "
        "the six characters. Read them off the site rather than recalling or "
        "estimating them. If you cannot reach the official site, write NONE "
        "after both labels; a guessed colour is worse than none, because it "
        "will be used as if it were theirs."
    )


def read_brand(texts) -> dict:
    """Pull BRAND_ACCENT / BRAND_DEEP out of the research answer."""
    import re
    blob = "\n".join(t for t in (texts if isinstance(texts, (list, tuple))
                                 else [texts]) if t)
    out = {}
    for key, field in (("accent", "BRAND_ACCENT"), ("deep", "BRAND_DEEP")):
        # Six digits, or the three-digit shorthand a model sometimes writes.
        m = re.search(field + r"\s*:?\s*\**\s*#?([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b",
                      blob)
        if not m:
            continue
        hexed = m.group(1)
        if len(hexed) == 3:
            hexed = "".join(c * 2 for c in hexed)
        out[key] = "#" + hexed.upper()
    # A pair that is really one colour twice is worse than one colour: it
    # tells the design there is a dark tone to work with when there isn't.
    if out.get("accent") and out.get("accent") == out.get("deep"):
        del out["deep"]
    return out


PICTURE_LINE = re.compile(
    r"^\s*PICTURE\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$", re.M)


def read_pictures(texts) -> dict[str, str]:
    """What the imagery stage said about the pictures the CLIENT attached.

    The customer types "make a reel, here are two screenshots" and nothing
    else — which is the whole point of the product — so nobody ever tells the
    art director that one of them is a home screen with an Add folder button
    and the other is a plan with a tool beside every step. The design stage
    then places them as decoration, because decoration is all it knows they
    are.

    The imagery stage is already ChatGPT, already in the pipeline whenever
    artwork exists, and can see. Asking it while it is there costs nothing.
    """
    blob = "\n".join(t for t in (texts if isinstance(texts, (list, tuple))
                                 else [texts]) if t)
    out = {}
    for name, said in PICTURE_LINE.findall(blob):
        said = said.strip().strip("*").strip()
        # Two ways this comes back empty and both must be caught. NONE is what
        # the stage was TOLD to write when it cannot see a file; the example
        # echoed back is what a model writes when it is padding. A wrong
        # description is worse than none, because the art director will build
        # a scene around it.
        if not said or said.upper().startswith("NONE"):
            continue
        if said.lower().startswith(("what it shows", "what is in it", "<", "…",
                                    "...")):
            continue
        out[name] = said[:400]
    return out


def describe_pictures(listing: str, notes: dict) -> str:
    """Fold those descriptions into the asset list the design stage reads."""
    if not notes or not listing.strip():
        return listing
    out = []
    for line in listing.split("\n"):
        m = re.match(r"\s*asset:([A-Za-z0-9_-]+)\b", line)
        said = notes.get(m.group(1)) if m else None
        out.append(f"{line}\n      ↳ {said}" if said else line)
    return "\n".join(out)


def imagery_instructions(request: str, has_own_artwork: bool = False,
                         research: str = "", attached=()) -> str:
    """The stage that MAKES the pictures when the client supplied none.

    Most jobs arrive with nothing attached — a company name and a sentence.
    Research has already found out who they are by this point, so the tool
    that can both search and draw is asked for a small, specific set of
    images rather than a mood board.
    """
    return (
        "You are producing the ARTWORK for a short vertical brand reel. Do "
        f"this in two steps.\n\n"
        "STEP 1 — SEARCH. Look the company up on the web before drawing "
        "anything: what they actually sell, what their existing branding "
        "looks like, their colours, the physical things their work involves. "
        "Do not guess from the name.\n\n"
        f"STEP 2 — GENERATE EXACTLY {MAX_GENERATED} SEPARATE, REUSABLE "
        "REEL ASSETS.\n"
        f"  · Make {MAX_GENERATED} DIFFERENT IMAGE FILES. Call the image "
        f"tool {MAX_GENERATED} separate times, once per asset — do not ask it "
        "for a set of images in one call.\n"
        "  · These files will be placed INSIDE a later video scene. They are "
        "raw visual ingredients, NOT finished reel frames. Do not make a "
        "storyboard, contact sheet, carousel, poster, presentation slide, "
        "multi-panel image, scene-number badge, timestamp, CTA, caption, "
        "headline, or a full-screen social-media design.\n"
        "  · Never combine multiple proposed scenes into one bitmap. One "
        "image with several panels, phones, cards, or alternatives is a "
        "failed result and is discarded.\n"
        "  · DO NOT make a collage, collection spread, or multi-product group. "
        "Every single image file must feature EXACTLY ONE individual subject in isolation.\n"
        "  · Each file contains ONE clear subject only, with generous empty "
        "space around it so the video can crop, animate, and place live text "
        "beside it.\n\n"
        f"WHAT THE {MAX_GENERATED} ASSETS ARE:\n"
        + ("  1. NO logo — the client's real mark was supplied and is already "
           "in hand, so anything you draw would be a lookalike and would be "
           f"thrown away. Make {MAX_GENERATED} SUBJECT images instead.\n"
           if has_own_artwork else
           "  1. A wordmark or emblem for the company, in the spirit of what "
           "your search found — their colours, their industry. The company "
           "name must be spelled EXACTLY as it is written above; check it "
           "character by character before you finish.\n")
        + "  2. The PRIMARY HERO SUBJECT of their business — the main equipment, "
        "flagship product, or core offering.\n"
        "  3. PROCESS / ACTION — a crisp, clear representation of their craft, "
        "manufacturing, engineering, or workflow in action.\n"
        "  4. SECONDARY DETAIL / TEXTURE / MATERIAL — a specific close-up, component, "
        "or raw material that proves domain mastery.\n"
        "  5. TRUST / BADGE / METRIC — an iconography graphic, certification mark, "
        "or stylized badge representing quality, speed, or precision.\n"
        "  6. OUTCOME / FINISH — the finished product, happy result, or dramatic "
        "closing visual.\n\n"
        "EVERY IMAGE MUST:\n"
        "  · have a TRANSPARENT background — a PNG with alpha, the subject "
        "cut out and nothing behind it. No white card, no scene, no desk, no "
        "drop shadow, no rounded panel. The reel places these on its own "
        "background, so anything behind the subject shows up as a rectangle "
        "stuck in the middle of the frame. If transparency is genuinely not "
        "possible, use ONE flat solid colour and nothing else — that can be "
        "removed afterwards; a gradient or a scene cannot.\n"
        "  · contain no people and no faces.\n"
        "  · be square or portrait, high-resolution, and leave the subject "
        "uncropped at the edges.\n"
        "  · contain no baked-in copy at all. The only exception is image 1 "
        "when asked for a wordmark: it may contain the company name, and "
        "nothing else.\n\n"
        f"WHAT THE REEL IS ABOUT:\n{request}\n\n"
        + (f"WHAT RESEARCH ALREADY FOUND:\n{research[:1500]}\n\n"
           if research.strip() else "")
        + "When the images are done, reply with one short line per image "
        "saying what it is — nothing else. The images themselves are "
        "collected from this page automatically; do not describe how they "
        "should be used."

        # Asked here because this stage can SEE and is already running. The
        # customer's own words are usually "make a reel, here are two
        # screenshots" — so without this nobody ever tells the art director
        # what is in them, and it places them as decoration because
        # decoration is all it knows they are.
        + (("\n\nONE MORE THING, AND IT IS SEPARATE FROM THE IMAGES YOU JUST "
            "MADE.\nThe files attached to this message are the CLIENT'S OWN "
            "pictures. Look at each one and, after your lines above, add "
            "exactly one line per attached file in this form, and nothing "
            "after them:\n"
            + "\n".join(f"PICTURE {n}: <what is in it, in a few words> — "
                        f"<the ONE part of it worth cropping to and why>"
                        for n in attached) +
            "\n\nThey are in that order, top to bottom, as attached. Say what "
            "is actually visible — a screen, a machine, a product, a "
            "certificate — and name the single region that carries the point, "
            "because the frame is tall and narrow and the whole picture will "
            "not fit into it legibly. If a file is not a picture at all, or "
            "you cannot see it, write NONE after its label rather than "
            "guessing.") if attached else "")
    )


def design_instructions(brand: dict | None = None, request: str = "",
                        assets: str = "", look: str = "", kit: dict | None = None) -> str:
    """The art-direction pass — the LOOK and the STORYBOARD, no scenes yet.

    This used to ask for the whole reel in one reply, and that single fact was
    what made every reel look like a slide deck. Measured against a reel built
    by hand with a coding agent: 20,300 characters of markup and motion per
    scene there, 278 characters per scene here. Not a taste gap — a budget
    one. A model asked for seven scenes in one JSON object spreads a few
    thousand characters across all of them, and 278 characters holds a
    headline and a subhead. That IS a slide; there is nothing in it to move.

    So the design stage is a conversation now. This prompt is turn one: the
    palette, the type, the shared stylesheet, and a storyboard giving every
    scene a distinct job. Each scene is then written on its own turn by
    scene_instructions(), with the whole reply to spend on one scene.
    """
    # None means "not known yet" — the research stage supplies it at run time
    # and the token is substituted the moment before this prompt is typed.
    swatch = BRAND_TOKEN if brand is None else brand_block(brand)
    # `look`: the art direction Prism chose for this film (core.looks). With one
    # set, the worked examples below that every reel was copying — the arch and
    # its CSS — are taken out, and the direction is stated after the colours.
    # `kit`: the client's identity beyond colour (core.brandkit).
    from . import brandkit as _brandkit, looks as _looks
    kit = kit or {}
    direction = _looks.text(look, fonts_given=bool(kit.get("display_font") or kit.get("body_font")))
    framing_example = ("" if direction else
                       "(for example: classic architectural arches, soft rounded editorial cards, "
                       "or a measured rule/grid system) ")
    card_shapes = ("in the shapes this film's art direction names" if direction else
                   "clean rounded corners (`border-radius: 20px-36px`) or tall arches "
                   "(`border-radius: 400px 400px 0 0`)")
    swatch += direction + _brandkit.block(kit)       # the mark has its own rules in the artwork section below
    return (
        # Fences are now REQUIRED, having been forbidden here since this was
        # written. Diagnosed from a saved failure: asked for bare JSON, the
        # chat window renders it as prose, and prose is markdown — so every
        # asterisk in the stylesheet is eaten as an emphasis marker (`*{box-
        # sizing:border-box}` arrives as ` {box-sizing:border-box}`) and a long
        # @import URL gets soft-wrapped into a real newline inside a JSON
        # string, which is an unescaped control character and unparseable.
        #
        # Inside a code block markdown processes nothing and wrapping is
        # visual only, so both corruptions simply do not happen. The parser
        # has always skipped fences — its own docstring says scrapes carry
        # them — so forbidding them bought nothing and cost whole designs.
        "Context, honestly stated: you are one stage of Prism, a desktop "
        "automation the client's own team launched and is watching; your "
        "reply is parsed by a program on their machine, which is why the "
        "format below is strict.\n\n"
        "OUTPUT FORMAT. Reply with ONLY a JSON object, wrapped in a ```json "
        "fenced code block and nothing else — no prose before it, none "
        "after. The fence matters: "
        "outside one, the chat window eats the asterisks in your CSS and "
        "breaks long URLs across lines, and the design is lost. "
        + IN_CHAT_RULE + "\n\n"
        "You are the ART DIRECTOR for a 1080x1920 vertical reel. The script "
        "above is final: use its words exactly, in its order, with its "
        "timings. Everything about how it LOOKS and MOVES is yours — "
        "background, palette, typography, composition, motion, the lot. "
        "There is no house style and no template to follow."
        "\n\nQUALITY BAR — design this as a short film, not a stack of social cards. "
        "The first 1.5 seconds need one unmistakable focal point; use one "
        "dominant composition per scene, a clear foreground/midground/background "
        "hierarchy, and generous safe margins. "
        "ONE HERO FOCAL POINT PER SCENE: Never pile multiple product photos into one scene with conflicting rotations, "
        "tilted overlaps, or cluttered collage piles. A vertical mobile reel lasts 3-4 seconds per scene — the viewer can "
        "only absorb ONE hero product focal point at a time. Make the featured product large, proud, and spacious "
        "(occupying 40-60% of the visual weight). If a scene has bullet points or features, place them in a clean editorial "
        "column with clear spacing, never overlapping or crowding the product. "
        "BALANCED VERTICAL COMPOSITION: Distribute the composition with intentional balance across the 1080x1920 canvas. "
        "Avoid clustering elements in one corner. Every scene's content must run from within the top third of the 1920px "
        "frame to within the bottom third: no more than ~630px bare at the top or at the bottom (the layout check measures "
        "this and sends the scene back) — size the main element up or place the group in the middle, do not float a small "
        "card in a blank frame. "
        "Never print giant random numbers (like '200 g') that overpower the product. "
        "Labels and descriptions must accurately describe the featured artwork on screen. "
        "Do not default to split-screen, a narrow image strip, or a picture beside an oversized headline. Keep "
        "all copy live HTML/CSS: generated images are visual ingredients only, "
        "never a finished poster or text-bearing storyboard."
        "\nSHAPE & EDITORIAL FRAMING — geometric elements must feel authored, not like "
        "placeholders. Define one cohesive framing system for the whole reel "
        f"{framing_example}and reuse it with variation. Every "
        "shape needs a job in the story or hierarchy. Use optical alignment, "
        "a small stroke scale (1px/2px/4px), intentional proportions, and "
        "controlled opacity. "
        "CRITICAL PRODUCT FRAMING: Never use arbitrary diagonal polygon clip-paths (`clip-path: polygon(...)`) "
        "on product photography or artwork. Diagonal polygon cuts slice through the client's products, candles, jars, and labels. "
        "Products must always remain intact, complete, and fully visible. Transparent cut-outs should float cleanly with natural "
        f"breathing room. Photographic cards should use {card_shapes}. "
        "Avoid random floating shapes, decorative clutter, and stray measure lines that do not connect to the subject. "
        "Avoid random circles, generic pills, arbitrary "
        "solid rectangles, decorative counters and corner marks, equal-weight "
        "decorations, and shapes that merely fill empty space. Geometry is "
        "optional: a single well-spaced phrase with a purposeful reveal can "
        "carry a beat. When used, prefer one hero construction plus two quiet "
        "support details per scene; keep the headline as the visual priority. "
        "Carry one meaningful visual idea through the reel and let its "
        "transformation explain the next beat, rather than a new ring, panel "
        "or graphic system for every scene."
        "\nCOMPOSITION SYSTEM — use a consistent 12-column grid and an 8px rhythm. "
        "Align type, rules, and shape edges to shared guides; vary the span and "
        "offset, not the underlying grid. THE SAFE AREA: keep essential copy "
        f"inside {SAFE_AREA}. "
        "Use at most three typographic roles (display, support, micro-label) and "
        "no more than two families. Let the space between elements carry as much "
        "weight as the geometric mark."
        f"{swatch}"
        f"{(chr(10) + chr(10) + 'What the client asked for: ' + request) if request else ''}"
        "\n\nYou are writing a real web page. It is rendered in Chromium at "
        "1080x1920 and filmed frame by frame, so ordinary CSS is what you "
        "have — gradients, grid, flexbox, clip-path, masks, filters, SVG, "
        "pseudo-elements, web fonts.\n\n"
        + (("ARTWORK ON HAND — this is the complete list:\n"
            + assets +
            "\n\nUse them by name, exactly like a URL: "
            '<img src="asset:logo" alt=""> or '
            "background-image: url(asset:logo).\n"
            "· VISUAL REFERENCE REVIEW: inspect each attached image in the "
            "conversation before choosing a scene. For every listed asset, "
            "decide which scene uses it and record that name in the storyboard "
            "row's `assets` list. If an image is unreadable, irrelevant, the "
            "wrong orientation, or cannot be used safely, do not force it into "
            "a frame: add a `design.asset_flags` entry with its asset name, status "
            "(`limited`, `reference-only` or `unusable`) and a short reason. Never invent a "
            "replacement filename.\n"
            "· THOSE NAMES ARE THE ONLY ONES THAT EXIST. Referring to any "
            "other — asset:art2 when only asset:art1 is listed, asset:photo, "
            "asset:bg — leaves a hole in the frame. Count the list above and "
            "use only what is on it.\n"
            "· Every scene does not need a picture. A scene with none must "
            "still look finished: fill it with the background, type and "
            "colour rather than leaving a gap where an image would have been. "
            "An empty rectangle reads as a bug; deliberate space does not.\n"
            "· Put the logo where a logo belongs — the endcard at least — and "
            "size it in CSS rather than trusting its pixel dimensions.\n"
            "· Never redraw or approximate a mark you have been given.\n\n")
           if assets else
           "NO ARTWORK IS AVAILABLE — not one image. Build the entire reel "
           "from type, colour, shape and motion, and make that look "
           "deliberate. Do not reference asset:anything; every such reference "
           "resolves to nothing and leaves a hole. Do not draw a logo out of "
           "CSS boxes either — set the company name in good type instead.\n\n")
        + "THIS REPLY IS THE LOOK AND THE PLAN. NOT THE SCENES.\n"
        "You will be asked for the scenes one at a time, straight after this, "
        "in this same conversation — a whole reply for each one. So do not "
        "write any scene markup now, and do not compress the plan: the "
        "storyboard below is the brief you will be building from, and it is "
        "worth being specific in.\n\n"
        '{\n'
        '  "design": {\n'
        '    "name": "one line describing the look you chose",\n'
        '    "google_fonts": ["chosen family with only the required weights"],\n'
        '    "direction": "reference observations, one visual idea, type roles, '
        'shape purpose, motion rhythm and what to leave out",\n'
        '    "cut_ms": 500,\n'
        '    "css": "…the SHARED stylesheet: :root variables, the background, '
        'the type scale, and any helper class more than one scene will use…"\n'
        '  },\n'
        '  "storyboard": [\n'
        '    {"scene": 1, "job": "what this scene is FOR in the argument",\n'
        '     "look": "what is on screen and how it is composed — where the '
        'eye lands, what is big, what is edge-to-edge, what is only a rule or '
        'a field of colour",\n'
        '     "motion": "what moves, in what order, from where — and what '
        'stays still so the moving thing reads",\n'
        '     "shape_language": "the recurring geometric vocabulary and why it belongs here",\n'
        '     "cut": "push",\n'
        '     "assets": ["logo"]}\n'
        '  ]\n'
        '}\n\n'
        "ONE STORYBOARD ROW PER SCENE IN THE SCRIPT, in the script's order. "
        "Give each one a DIFFERENT job and a different composition. Seven "
        "scenes that are all a centred headline over the same background is "
        "the failure this stage exists to prevent — vary the scale, the "
        "alignment, the crop, what the frame is mostly made of.\n\n"
        # The pictures are planned here, by name, because a scene written on
        # its own turn cannot see what the others did with them: on the
        # 2026-09-07 reel one generated image reached the film as a strip of
        # confetti and nobody — not the model, not the check — knew a
        # picture had gone missing, because nothing had said where it went.
        + (("THE ASSET PLAN: `assets` on a row names the artwork that scene "
            "carries, by the names on the list above and nothing else. "
            "SEQUENTIAL ARTWORK COVERAGE: When you have multiple production artworks (art1, art2, art3, etc.), "
            "each scene must feature a DIFFERENT artwork across the reel (e.g. Scene 1 -> art1, Scene 2 -> art2, "
            "Scene 3 -> art3, etc.). Do NOT repeat the same artwork across multiple scenes while leaving other artworks unused! "
            "Every available production artwork must be showcased across the reel. "
            "Assets flagged REFERENCE ONLY or unusable must not be placed into scenes. "
            "Use a valid supplied logo on the last row when available. "
            "The scene prompts that follow hold you to this, row by row.\n\n")
           if _asset_names(assets) else "")
        # From here to the end is the rulebook for every scene. build_spec()
        # asks for the scenes in this same tab straight after this reply, so
        # scene_instructions() names these sections instead of restating
        # them; it used to restate every one of them in its own words on
        # every turn (see the note there).
        + "EVERYTHING FROM HERE DOWN APPLIES TO EVERY SCENE YOU WRITE LATER IN "
        "THIS CONVERSATION. The scene prompts point back to these sections by "
        "name rather than repeat them, so hold on to them.\n\n"
        "HOW MOTION WORKS — read this, it is the one unusual part:\n"
        "· Write ordinary CSS @keyframes and animation declarations. The "
        "renderer PAUSES the page and sets each animation's time by hand for "
        "every frame, so the result is identical on every render.\n"
        "· Every animation MUST use `both` fill-mode "
        f"(`animation: rise 900ms {EASE_MENU[0][1]} both`) or it will "
        "snap when the frame is seeked.\n"
        "· Animation time restarts at 0 for each scene. Stagger with "
        "`animation-delay` — things that arrive together read as one block, "
        "things that arrive 80-120ms apart read as choreography.\n"
        "· Choose a coherent motion rhythm. Reuse easing for related elements; "
        "change it only when the action or emphasis calls for it. Available "
        f"curves: {_ease_menu_text()}.\n"
        "· Choreograph in phases: a short anticipation, the primary move, a "
        "restrained settle, then a readable hold. Do not animate every object "
        "at once. Finish essential copy entrances in the first 30 percent of "
        "the scene and hold the complete message for at least one second "
        "before it leaves. Stillness is valid; a slow secondary motion is "
        "optional, and a headline never moves merely to keep pixels moving.\n"
        "· Never use transitions, JavaScript, `:hover`, or anything that "
        "depends on real time — none of it will be filmed.\n"
        "· Each scene element gets `--p` (0→1 through the scene) and `--ms` "
        "if you prefer to drive something off progress directly.\n"
        "· Cuts: the outgoing scene has class `leaving` with `--x` 0→1, the "
        "incoming one `entering` with `--e` 0→1. Style them however you like "
        "— a fade is only the default.\n"

        # A menu, deliberately, rather than another rule. An earlier attempt at
        # improving these reels added six prohibitions to this prompt and made
        # the output blander: told what NOT to do, a model plays safe, and safe
        # is what generic is made of. Named moves that already work leave it
        # free to choose instead of invent.
        "· Or name one and skip the work: put `\"cut\": \"push\"` on a scene "
        "and it gets that transition, already built and already correct at "
        "every frame. `push` — both frames travel together, the new one "
        "pushing the old out; the neutral choice for an ordinary beat. "
        "`push-up` — the same thing vertically. `squeeze` — the old frame "
        "compresses to its left edge while the new one opens out from its "
        "right; mechanical and precise, good for industrial subjects. `zoom` "
        "— the old frame rushes past the camera and blurs while the new one "
        "rises from behind it; it reads as pushing deeper into the same "
        "thought, so spend it once, not on every cut. Mix them with your own "
        "`.leaving` / `.entering` rules freely.\n\n"
        "WHAT THE RENDERER GUARANTEES, SO YOU DO NOT HAVE TO:\n"
        "the frame size, the seeking, the cuts' timing, and the encode. "
        "`.scene` is already a full-frame absolutely-positioned layer, and "
        "`--safe-x`/`--safe-y` are the frame margins for backgrounds and "
        "shapes; essential copy stays inside the safe area given above.\n\n"
        "THE LAYER CONTRACT — obey this in every scene:\n"
        "· Add depth only when it clarifies the subject. A strong typographic "
        "frame does not need a background illustration or a running label.\n"
        "· When layers are needed, establish an explicit stack: background "
        "z-index 0; textures and decorations 10; image assets 20; cards and "
        "colour panels 30; all headlines, body copy and required script words "
        "50; tiny labels, counters and brand marks 60. Give positioned "
        "elements an explicit z-index instead of relying on DOM order.\n"
        "· Required copy must be the top readable layer: required words are "
        "always above decorative shapes and image assets. A panel may sit "
        "behind copy, never across it. If copy sits over an image, give it a "
        "solid or translucent backing with enough contrast to read at arm's "
        "length.\n"
        "· Do not let a circle, rule, crop, image, card or entering scene "
        "cross a headline or support line unless the overlap is deliberate, "
        "brief, and the text remains fully readable.\n"
        "· Keep the primary headline and support in a dedicated content layer "
        "with its own z-index. Check the settled frame, not only the entrance "
        "frame: animation must never leave copy behind an opaque sibling.\n\n"
        "WHAT WILL BE REJECTED — the page is measured before it is filmed:\n"
        "· any text whose box falls outside the 1080x1920 frame\n"
        "· any two texts whose boxes overlap, and any text with a panel, "
        "rule, shape or picture painted across it — a running header must "
        "leave room for its own date, a footer for its own caption\n"
        f"· any text rendered under {T_LABEL}px; headlines want "
        f"{T_HEADLINE}px+, supporting text {T_SUPPORT}px+. A phone is watched "
        "at arm's length for under a second a scene.\n"
        "· anything that makes the page scroll\n\n"
        "MAKE IT SPECIFIC TO THIS CLIENT. A seed company at dusk and a "
        "cardiology clinic should not come out looking like the same film. "
        "Choose a background that means something — a deep field gradient, "
        "paper, a colour block, a fine rule system — and typography with a "
        "point of view. Do not default to white with a grid.\n"

        # What actually separated a scene that worked from one that did not,
        # on a reel this stage was rebuilt for. Both had the same words. The
        # good one put the CUSTOMER'S OWN MATERIAL on screen — the real
        # filenames out of a Gerber folder, the seven real drill sizes, one
        # dot for each of 238 holes — and the flat one described the same
        # facts in a sentence. Deliberately given across trades so it does
        # not read as advice about circuit boards. It lived in every scene
        # prompt; it is a rule for every scene, so it is said here, once.
        "BUILD EACH SCENE FROM THE CUSTOMER'S OWN MATERIAL, not from "
        "adjectives. The things they actually handle are the strongest thing "
        "you can put on screen: the part numbers, the file names, the sizes, "
        "the grades, the machines, the varieties, the test names, the routes. "
        "A fabricator's real drill sizes set as chips; a seed company's actual "
        "variety names in a column; a workshop's tolerances beside the part "
        "they hold. If a scene names a count, consider DRAWING that many "
        "things rather than only printing the number."
    )


# ── the scene-at-a-time conversation ────────────────────────────────────────
# Turn one is design_instructions() above. Everything from here down runs the
# rest of it: one turn per scene, each with the whole reply to spend, each
# laid out and checked before the next is asked for.

SCENE_EXPECT = '"html"'


def _json_objects(text: str):
    """Every balanced {...} in a reply, parsed if it parses.

    The same repairs parse_spec makes, in the same order: chat windows linkify
    URLs inside CSS, and art directors write HTML attributes in double quotes
    inside a JSON string. Both lose an otherwise perfect reply over
    punctuation.
    """
    from . import reel as _pillow
    if not text or not text.strip():
        return
    for block in _pillow._blocks(_unlink_markdown(text), "{", "}"):
        for candidate in (block, _fix_markup_quotes(block),
                          _pillow._loosen(block),
                          _fix_markup_quotes(_pillow._loosen(block))):
            try:
                got = json.loads(candidate)
            except Exception:
                continue
            if isinstance(got, dict):
                yield got
                break


def read_script(script_text: str) -> list[dict]:
    """The script stage's scenes, as written. Empty if it cannot be read.

    Used for two things: how many scenes there are, and what words each one
    must carry. The script is final by the time this runs, so a scene prompt
    quotes it rather than inviting the art director to reinterpret it.
    """
    for got in _json_objects(script_text or ""):
        scenes = got.get("scenes")
        if isinstance(scenes, list) and scenes:
            return [s for s in scenes if isinstance(s, dict)]
    return []


# The sentence every JSON-returning Studio prompt carries. Found on a
# client's Mac (11 Sep 2026): the art-direction reply came back with no JSON
# in the chat at all -- the kind of reply Claude moves into an artifact panel
# and ChatGPT into a canvas by default when it is long and code-shaped. Prism
# reads the conversation text; a side panel is invisible to it, so the
# design was "lost" while sitting on screen. Nothing in the prompt had said
# not to do that.
IN_CHAT_RULE = (
    "Put it in this chat message itself: do NOT create an artifact, a "
    "canvas, a document or a downloadable file. The program reads only the "
    "conversation text, so anything placed in a side panel is never seen "
    "and the work is lost.")

IN_CHAT_REASK = (
    "Your design did not reach me: nothing in your chat message parses as "
    "JSON, which usually means it went into an artifact, canvas or file. "
    "Send the SAME design again as a ```json fenced code block IN THIS CHAT "
    "MESSAGE ITSELF, with the \"design\" and \"storyboard\" keys, nothing "
    "before or after it, and do not create an artifact or canvas.")


def parse_design(text: str) -> tuple[dict, list[dict]]:
    """Turn one's reply: (design, storyboard).

    A reply that carries scenes as well is not an error — some models answer
    the whole brief however it is put. The scenes are ignored, but their
    presence is a perfectly good storyboard if none was written.
    """
    design, board = {}, []
    for got in _json_objects(text):
        d = got.get("design")
        if isinstance(d, dict) and not design:
            design = dict(d)
            if "asset_flags" not in design and isinstance(got.get("asset_flags"), list):
                design["asset_flags"] = got["asset_flags"]
        rows = got.get("storyboard")
        if isinstance(rows, list) and not board:
            board = [r for r in rows if isinstance(r, dict)]
        if not board and isinstance(got.get("scenes"), list):
            board = [{"job": "", "look": "", "motion": "",
                      "cut": str(s.get("cut", ""))}
                     for s in got["scenes"] if isinstance(s, dict)]
        if design and board:
            break
    if not design:
        raise ReelError("The art-direction stage returned no design.")
    design.setdefault("css", "")
    return design, board


def parse_scene(text: str) -> dict | None:
    """One scene's reply. None if there is no markup in it."""
    for got in _json_objects(text):
        # A model sometimes wraps the scene in the shape it was shown first.
        if isinstance(got.get("scenes"), list) and got["scenes"]:
            inner = got["scenes"][0]
            if isinstance(inner, dict) and str(inner.get("html", "")).strip():
                got = inner
        if not str(got.get("html", "")).strip():
            continue
        out = {"html": str(got["html"])}
        if str(got.get("css", "")).strip():
            out["css"] = str(got["css"])
        for key in ("cut", "type"):
            if str(got.get(key, "")).strip():
                out[key] = str(got[key]).strip()
        try:
            out["seconds"] = float(got["seconds"])
        except (KeyError, TypeError, ValueError):
            pass
        return out
    return None


def _scene_count(text: str) -> int:
    """How many scenes with real markup a reply actually contained.

    parse_scene() above only ever keeps the first, silently — which means a
    model that drifts from "one scene per turn" and answers three at once
    gets no signal that the other two were thrown away. It just sees the
    conversation move on, and answers the same over-eager way next turn too.
    This is what lets the next prompt name the actual number instead of a
    vague "slow down" that a model drifting once has no reason to believe.
    """
    n = 0
    for got in _json_objects(text):
        rows = got.get("scenes")
        if isinstance(rows, list):
            n += sum(1 for s in rows
                     if isinstance(s, dict) and str(s.get("html", "")).strip())
        elif str(got.get("html", "")).strip():
            n += 1
    return n


def scene_instructions(idx: int, total: int, line: dict, script_scene: dict,
                       assets: str = "") -> str:
    """The prompt for ONE scene, sent in the same tab as the design.

    Short on purpose. The palette, the type scale, the storyboard and the
    rulebook every scene is written to — the layer contract, how motion
    works, the safe area, what is rejected, the asset manifest — are all
    further up this same conversation, typed straight before scene 1 is
    asked for. Repeating them would spend the budget this whole change
    exists to create.
    """
    line = line or {}
    script_scene = script_scene or {}
    words = []
    for key, label in (("kicker", "KICKER"), ("headline", "HEADLINE"),
                       ("support", "SUPPORT"), ("note", "NOTE"),
                       ("contact", "CONTACT")):
        val = str(script_scene.get(key, "")).strip()
        if val:
            words.append(f"  {label}: {val}")
    for key in ("items", "points"):
        val = script_scene.get(key)
        if isinstance(val, list) and val:
            words.append(f"  {key.upper()}: {json.dumps(val, ensure_ascii=False)}")

    try:
        seconds = float(script_scene.get("seconds", 4.5))
    except (TypeError, ValueError):
        seconds = 4.5
    cut = str(line.get("cut", "")).strip()
    role = str(script_scene.get("role", "")).strip()

    plan = []
    for key in ("job", "look", "motion", "shape_language"):
        val = str(line.get(key, "")).strip()
        if val:
            plan.append(f"  {key.upper()}: {val}")
    planned = planned_assets(line, assets)
    names = _asset_names(assets)

    # This prompt used to restate the design turn's rulebook in its own words
    # on every turn: HOW MOTION WORKS again as MOTION, THE LAYER CONTRACT
    # again as LAYER ORDER, WHAT WILL BE REJECTED again as a closing
    # paragraph, the shape and grid rules, the customer's-material rule, and
    # the whole asset manifest. With a four-picture list that was about 9,700
    # characters a scene, so a six-scene reel typed some 72,000 characters
    # into one chat tab, and in one place the two copies disagreed (two
    # different safe areas). build_spec() asks for every scene in the same
    # tab straight after the design turn, so the model has just read those
    # rules. They are said once there; this names them and carries only what
    # belongs to this scene — its number, its words, its storyboard row, its
    # artwork by name, the safe area, and the shape of the reply.
    return (
        f"THIS IS SCENE {idx + 1} OF {total}"
        + (f" — role: {role}" if role else "") + f", {seconds:g} seconds. "
        # Give each scene a full turn without rewarding DOM/CSS volume.
        # Element and movement quotas produced decorative clutter.
        "THIS WHOLE REPLY IS ONE SCENE: resolve it completely; there is no "
        "minimum element count or animation count.\n\n"
        + ("YOUR OWN STORYBOARD FOR IT:\n" + "\n".join(plan) + "\n\n"
           if plan else "")
        + ("THE WORDS, EXACTLY AS THE SCRIPT WROTE THEM — every one of these "
           "has to appear on screen:\n" + "\n".join(words) + "\n\n"
           if words else
           "This scene carries no text of its own — it is made of shape, "
           "colour and movement.\n\n")

        # Written one turn at a time, a scene cannot see the others, and on
        # the 2026-09-07 reel that showed: scenes 1-4 were numbered "/ 10"
        # (the source document's page count) and 5-6 "/ 06", and the last
        # two switched typeface family. The reel has one count and one look.
        + f"Same reel: any counter reads {idx + 1} / {total} (never a total "
          "from the source material), and a scene that switches typeface "
          "family, header or labels reads as a different film.\n\n"

        + ((("ARTWORK THIS SCENE CARRIES (your storyboard): "
             + ", ".join(f"asset:{n}" for n in planned)
             + ". Feature this artwork as the single hero focal point of this scene, "
             "fully visible and unclipped (NEVER use diagonal polygon clip-paths). "
             "Each must appear in this scene's markup or CSS, or the "
             "scene comes back.\n") if planned else "")
           # Named off the real list rather than hard-coded. The example used
           # to read `asset:logo` whether or not a logo existed, which is a
           # picture of a mark being dangled in front of a reel that has
           # none — and an unresolved reference leaves a hole in the frame.
           + "Artwork names that exist: "
           + ", ".join(f"asset:{n}" for n in names)
           + f" — <img src='asset:{names[0]}' alt=''> or "
             f"url(asset:{names[0]}); any other name leaves a hole.\n"
           # The last scene is where a logo belongs, and the last scene is
           # the one that knows it is last: a reel with a perfectly good
           # logo attached once ended on a mark drawn out of CSS boxes.
           + ("THE CLIENT'S OWN MARK IS AVAILABLE as `asset:logo` and this is "
              "the last scene: place it, sized in CSS, with room. Never "
              "redraw it or build one out of CSS shapes.\n"
              if "logo" in names and idx == total - 1 else
              "`asset:logo` is the client's mark; the last scene places it, "
              "so it does not need to appear in every scene.\n"
              if "logo" in names else "")
           + "\n" if names else "")

        + "Still in force from the design turn, not repeated here: THE LAYER "
          "CONTRACT, HOW MOTION WORKS (every animation ends in `both`, e.g. "
          f"`rise 800ms {ease_pick(idx)[1]} both`; time starts at 0 in this "
          "scene), SHAPE & EDITORIAL FRAMING (no diagonal polygon clip-paths), "
          "ONE HERO FOCAL POINT (no multi-product tilted collages), and WHAT WILL BE REJECTED.\n"
          f"THE SAFE AREA: essential copy inside {SAFE_AREA}.\n\n"

        + "REPLY WITH ONLY THIS JSON OBJECT, in a ```json fenced code block "
        "in this chat message itself (no artifact, canvas or file), "
        "nothing before or after it:\n"
        '{\n'
        f'  "seconds": {seconds:g},\n'
        + (f'  "cut": "{cut}",\n' if cut else
           '  "cut": "push",\n')
        + '  "css": "…this scene\'s rules and @keyframes…",\n'
        '  "html": "<div class=\'…\'>…</div>"\n'
        '}\n\n'
        "Your css is SCOPED TO THIS SCENE AUTOMATICALLY; no prefixes needed.\n"
        "HTML ATTRIBUTES MUST USE SINGLE QUOTES (<div class='wrap'>): an "
        "unescaped double quote inside the JSON string makes the reply "
        "unparseable, the most common way this step fails."
    )


def fallback_scene(script_scene: dict, seconds: float = 4.0) -> dict:
    """A plain, legible scene, built from the script when a turn comes back
    with nothing usable.

    Deliberately modest — it exists so that one failed turn costs one dull
    scene rather than the whole reel. It uses the design's own variables, so
    it is at least in the right colours, and its @keyframes are scoped like
    any other scene's and cannot collide.
    """
    from html import escape
    ss = script_scene or {}
    try:
        seconds = float(ss.get("seconds", seconds))
    except (TypeError, ValueError):
        pass
    bits = []
    for key, cls in (("kicker", "fb-k"), ("headline", "fb-h"),
                     ("support", "fb-s"), ("note", "fb-n")):
        val = str(ss.get(key, "")).strip()
        if val:
            bits.append(f"<div class='{cls}'>{escape(val)}</div>")
    items = ss.get("items")
    if isinstance(items, list) and items:
        rows = "".join(f"<li>{escape(str(i))}</li>" for i in items[:4])
        bits.append(f"<ul class='fb-l'>{rows}</ul>")
    if not bits:
        bits.append("<div class='fb-h'>&nbsp;</div>")
    return {
        "seconds": max(1.5, seconds),
        "cut": "push",
        "css": (
            ".fb{position:absolute;inset:var(--safe-y) var(--safe-x);"
            "display:flex;flex-direction:column;justify-content:center;"
            "gap:30px}"
            ".fb>*{animation:fb-in 800ms cubic-bezier(.16,1,.3,1) both}"
            ".fb>*:nth-child(2){animation-delay:110ms}"
            ".fb>*:nth-child(3){animation-delay:220ms}"
            ".fb>*:nth-child(4){animation-delay:330ms}"
            ".fb-k{font-size:38px;letter-spacing:.18em;text-transform:uppercase;"
            "color:var(--accent,#7a7a7a)}"
            ".fb-h{font-size:96px;line-height:1.05;font-weight:800;"
            "color:var(--ink,#111)}"
            ".fb-s{font-size:50px;line-height:1.3;color:var(--ink,#333);"
            "opacity:.82}"
            ".fb-n{font-size:34px;color:var(--ink,#555);opacity:.6}"
            ".fb-l{list-style:none;padding:0;margin:0;font-size:56px;"
            "line-height:1.5;color:var(--ink,#111)}"
            "@keyframes fb-in{from{opacity:0;transform:translateY(44px)}"
            "to{opacity:1;transform:none}}"
        ),
        "type": str(ss.get("role", "")),
        "html": f"<div class='fb'>{''.join(bits)}</div>",
    }


def build_spec(first_reply: str, ask, script: str = "", assets: str = "",
               assets_table: dict | None = None, check=None, log=None,
               should_stop=None, on_scene=None, brand: dict | None = None,
               measure=None, critic=None) -> dict:
    """Run the rest of the design conversation and return the finished spec.

    `ask(prompt, expect) -> str` sends a follow-up in the tab the design stage
    is already sitting in and returns the reply. `check(spec) -> list[str]`
    lays a scene out in the browser and reports what is illegible. Both are
    injected rather than imported so this whole flow can be exercised without
    a browser, which is the only reason it has tests worth having.

    `on_scene(index, total)` fires before each scene is asked for. It exists
    for the desktop window: this loop takes minutes, and a dialog that says
    "writing the words…" for six of them is indistinguishable from one that
    has hung.

    Nothing here raises once turn one has parsed. A scene that will not come
    back is replaced by a plain one built from the script: a reel with one
    dull scene ships, and a reel with a hole in it does not.

    `brand` is the client's measured colours, the same dict render() is
    given later. Without it here, the only place the accent-colour rule was
    ever actually checked was the final render — after every scene had
    already been written AND filmed, minutes of work in, with no chance to
    correct it: "the client's accent colour #3a713a appears nowhere in the
    design" (the owner's run, 14 Sep 2026). The rule is stated in turn one
    (brand_block) and the prompt SAYS "the design is checked for this
    before it is filmed" — but nothing had ever actually run that check
    until the render gate. Checked here too, cumulatively, once the last
    scene is written (not every scene — the rule only needs the colour to
    appear somewhere in the reel, matching what render()'s own brand_faults
    requires), it goes through the same one-shot correction every other
    layout fault already gets. render()'s hard gate stays as the backstop:
    a correction that doesn't take still stops a flawed MP4 from shipping.
    """
    def say(msg):
        if log:
            log(msg)

    design, board = parse_design(first_reply)
    lines = read_script(script)
    total = len(lines) or len(board)
    if not total:
        raise ReelError("Neither the script nor the storyboard names any "
                        "scenes — there is nothing to build.")
    if len(board) < total:
        board = board + [{}] * (total - len(board))
    if len(lines) < total:
        lines = lines + [{}] * (total - len(lines))

    say(f"storyboard: {total} scene(s) — writing them one at a time")
    scenes: list[dict] = []
    # Set whenever a turn answers more scenes than it was asked for, and
    # prepended to the very next prompt so the drift gets named and corrected
    # instead of silently repeating — see _scene_count().
    overflow_notice = ""
    for i in range(total):
        if should_stop and should_stop():
            say("stopped — keeping the scenes written so far")
            break
        if on_scene:
            try:
                on_scene(i, total)
            except Exception:                        # noqa: BLE001
                pass          # a progress listener must never fail the run
        prompt = overflow_notice + scene_instructions(i, total, board[i],
                                                       lines[i], assets)
        overflow_notice = ""
        raw = ask(prompt, SCENE_EXPECT) or ""
        scene = parse_scene(raw)
        if scene is None:
            # One retry, and a blunter ask. Almost always prose where JSON was
            # wanted, which a second, shorter prompt reliably fixes.
            scene = parse_scene(ask(
                f"Send scene {i + 1} again as JSON only — first character "
                '\'{\', last \'}\', keys "seconds", "cut", "css", "html", '
                "wrapped in a ```json fenced block. Nothing before or after.",
                SCENE_EXPECT) or "")
        elif _scene_count(raw) > 1:
            n = _scene_count(raw)
            say(f"scene {i + 1} came back with {n} scenes in it — only the "
                "first was kept; telling it to slow down before the next ask")
            overflow_notice = (
                f"Before the next scene: that last reply answered {n} "
                f"scenes at once. Only scene {i + 1} was kept — the rest "
                "were discarded, not saved for later, so nothing from them "
                "will appear in the reel. From here on, answer EXACTLY ONE "
                "scene per reply, the one actually asked for.\n\n")
        if scene is None:
            say(f"scene {i + 1} never came back as JSON — using a plain one")
            scenes.append(fallback_scene(lines[i]))
            continue

        scene.setdefault("seconds", lines[i].get("seconds", 4.5))
        if not scene.get("cut") and board[i].get("cut"):
            scene["cut"] = str(board[i]["cut"])
        scene.setdefault("type", str(lines[i].get("role", "")))

        # Laid out NOW, while the conversation is still on this scene. The
        # old stage checked all seven at the end, so a fault came back as
        # "scene 3's headline is off the frame" against a reply the model had
        # long since moved on from. Asked here, it is simply "this one".
        #
        # The storyboard's asset plan is checked in the same breath, without
        # a browser: a picture the plan put in this scene has to be in it.
        planned = [name for name in planned_assets(board[i], assets)
                   if name not in _blocked_assets(design)]
        check_state = "not run"
        # Only worth judging once the reel is about to finish -- brand_faults
        # asks whether the colour appears ANYWHERE in the design, and an
        # early scene legitimately may not carry it (a full-bleed picture, a
        # kicker-only card). Checked against every scene written so far plus
        # the one being judged, so it reflects the reel as it will actually
        # ship, not just this one card.
        last_scene = (i == total - 1)

        def _faults(sc, _i=i):
            nonlocal check_state
            found = []
            if check:
                try:
                    found = list(check({"design": design, "scenes": [sc],
                                        "_assets": assets_table or {}}) or [])
                    check_state = "passed" if not found else f"{len(found)} unresolved problem(s)"
                except Exception as e:
                    check_state = "unavailable — export preflight still required"
                    say(f"couldn't lay scene {_i + 1} out ({e})")
            colour = (brand_faults({"design": design, "brand": brand,
                                    "scenes": scenes + [sc]})
                      if brand and last_scene else [])
            return missing_planned(sc, planned) + found + colour

        if check or planned or (brand and last_scene):
            faults = _faults(scene)
            original_check_state = check_state
            if faults:
                say(f"scene {i + 1} has {len(faults)} layout problem(s) — "
                    "sending them back")
                fixed = parse_scene(ask(
                    f"Scene {i + 1} was laid out at 1080x1920 and these are "
                    "wrong:\n\n"
                    + "\n".join(f"{n}. {x}" for n, x in enumerate(faults[:8], 1))
                    + "\n\nSend the corrected scene: ONLY the JSON object, "
                      "same keys, in a ```json fenced block.",
                    SCENE_EXPECT) or "")
                if fixed:
                    fixed.setdefault("seconds", scene["seconds"])
                    fixed.setdefault("cut", scene.get("cut", ""))
                    fixed.setdefault("type", scene.get("type", ""))
                    left = _faults(fixed)
                    # Kept only if genuinely cleaner. A "fix" that trades four
                    # faults for five is not a fix, and the first attempt at
                    # least had the composition the storyboard asked for.
                    if len(left) < len(faults) and not check_state.startswith("unavailable"):
                        scene = fixed
                        say(f"   fixed — {len(faults)} down to {len(left)}")
                    else:
                        check_state = original_check_state
                        say("   the correction was no better — keeping the "
                            "first")
        scenes.append(scene)
        status = f"; layout preflight {check_state}"
        say(f"scene {i + 1}/{total} written — {len(scene.get('html', ''))} "
            f"chars of markup, {len(scene.get('css', ''))} of CSS{status}")

    if not scenes:
        raise ReelError("No scenes were written.")
    if measure and len(scenes) > 1:
        _repair_rhythm(scenes, design, ask, say, measure, check, assets_table)
    if critic:
        from .motion import critic as _critic

        def _spec_of(rows):
            out = {"design": design, "scenes": rows}
            if assets_table:
                out["_assets"] = assets_table
            return out

        def _keep(old, new):
            new.setdefault("seconds", old.get("seconds"))
            new.setdefault("cut", old.get("cut", ""))
            new.setdefault("type", old.get("type", ""))

        _critic.repair(scenes, _spec_of, ask, parse_scene, _keep, say, critic, SCENE_EXPECT)
    spec = {"design": design, "scenes": scenes}
    if assets_table:
        spec["_assets"] = assets_table
    return spec


def script_drift(spec: dict, script_text: str) -> list[str]:
    """Lines the script wrote that the design did not carry over.

    Two agents, and the second is told not to change a word — but told is not
    the same as prevented. Reported rather than blocked: sometimes the design
    legitimately splits a headline across elements, so this is a flag for a
    person, not a rule for a machine.
    """
    import re
    if not script_text.strip():
        return []
    try:
        script = {"scenes": read_script(script_text)}
    except Exception:
        return []
    if not script["scenes"]:
        return []

    page = " ".join(str(sc.get("html", "")) for sc in (spec.get("scenes") or []))
    page = re.sub(r"<[^>]+>", " ", page)
    page = re.sub(r"\s+", " ", page).lower()

    lost = []
    for sc in script["scenes"]:
        for key in ("headline", "support"):
            line = str(sc.get(key, "")).strip()
            if len(line) < 12:
                continue
            words = [w for w in re.findall(r"[a-z0-9₹%.]+", line.lower())
                     if len(w) > 3]
            if not words:
                continue
            hits = sum(1 for w in words if w in page)
            if hits < max(1, len(words) // 2):
                lost.append(line[:60])
    return lost


# ── the follow-up: a change to a filmed reel, in the chat that designed it ──
# The design conversation holds everything: the look, the storyboard, every
# scene as written. A change asked THERE costs one turn and comes back in the
# reel's own idiom; the same change asked in a fresh chat starts from nothing.
# So a follow-up reopens that tab (its URL is saved with the run) and asks for
# only the scenes that change — each complete, so it replaces its scene
# outright — then re-films locally. No AI touches the scenes it did not name.

def _scene_index_lines(spec: dict) -> str:
    """One line per scene, so the art director and the owner mean the same
    thing by "scene 3": the number, the role, and the words on it."""
    out = []
    for i, sc in enumerate(spec.get("scenes") or [], 1):
        words = re.sub(r"<[^>]+>", " ", str(sc.get("html", "")))
        words = " ".join(words.split())[:90]
        role = str(sc.get("type") or "").strip()
        out.append(f"  {i}. {(role + ' — ') if role else ''}{words}")
    return "\n".join(out)


_IMAGE_RULES = (
    "EVERY IMAGE MUST:\n"
    "  · have a TRANSPARENT background — a PNG with alpha, the subject cut "
    "out and nothing behind it. No white card, no scene, no desk, no drop "
    "shadow, no rounded panel. If transparency is genuinely not possible, "
    "use ONE flat solid colour and nothing else.\n"
    "  · contain one clear subject, with generous empty space around it.\n"
    "  · contain no people and no faces, and no baked-in words.\n"
    "  · be square or portrait, high-resolution, the subject uncropped.\n")


def followup_imagery_instructions(what: str, spec: dict) -> str:
    """The image tool's turn in a follow-up: the picture(s) the owner asked
    for, made as reel assets — the same rules the imagery stage works to,
    so what comes back drops into a scene like any other artwork."""
    have = ", ".join(f"asset:{n}" for n in (spec.get("_assets") or {}))
    return (
        "You are making ARTWORK for a short vertical brand reel that already "
        "exists and has been filmed. The owner watched it and asked for this "
        f"picture:\n\n  “{what.strip()}”\n\n"
        "Make it as SEPARATE image file(s) — one per picture asked for, at "
        "most 3 — calling the image tool once per picture. They will be "
        "placed INSIDE a scene by a later step: raw ingredients, not finished "
        "frames. No storyboard, no collage, no caption, no headline.\n\n"
        + _IMAGE_RULES
        + (f"\nThe reel already carries: {have}. Make what was asked for, "
           "not another version of those.\n" if have else "")
        + "\nWhen the images are done, reply with one short line per image "
        "saying what it is — nothing else. The images are collected from "
        "this page automatically."
    )


def followup_instructions(change: str, spec: dict, new_assets: str = "",
                          context: str = "") -> str:
    """What the design conversation is asked when the owner wants a change.

    `context` is what an earlier step of the same follow-up produced — a
    rewritten script, notes on the pictures just made — so the design chat
    changes the scenes it affects rather than guessing what moved."""
    total = len(spec.get("scenes") or [])
    have = ", ".join(f"asset:{n}" for n in (spec.get("_assets") or {}))
    brand = spec.get("brand") or {}
    faults = spec.get("_faults") or []
    return (
        "Continue the saved Studio project from this design conversation. "
        "Its previous export may have failed. The owner wants this change:\n\n"
        f"  “{change.strip()}”\n\n"
        + (("\nPREVIOUS RENDER FAULTS (must be resolved in this revision):\n· "
            + "\n· ".join(faults) + "\n\n") if faults else "")
        + (brand_block(brand) + "\n\n" if brand else "")
        + f"THE REEL AS FILMED — {total} scene(s):\n{_scene_index_lines(spec)}\n\n"
        + ("CURRENT ART DIRECTION (keep unless the change asks to replace it):\n"
           + str((spec.get("design") or {}).get("direction"))[:3000] + "\n\n"
           if (spec.get("design") or {}).get("direction") else "")
        + ((f"WHAT CHANGED UPSTREAM — a step before this one was redone for "
            f"this change, and this is its new output. Update the scenes "
            f"whose words or facts it changes; leave the rest as filmed:\n\n"
            f"{context.strip()[:6000]}\n\n")
           if context.strip() else "")
        + ((f"NEW ARTWORK made or attached for this change — use it by "
            f"name, exactly like the rest:\n{new_assets}\n\n"
            f"IMPORTANT: You MUST incorporate these new artwork asset(s) into the reel! "
            f"Update the scenes that need visual imagery by embedding <img class='hero' src='asset:<name>' alt=''> "
            f"or background-image: url(asset:<name>) with proper positioning and animation. Return the updated scenes in your JSON.\n\n")
           if new_assets.strip() else "")
        + (f"Artwork already in the reel: {have}.\n\n" if have else "")
        + "REPLY WITH ONLY THIS JSON OBJECT, in a ```json fenced block:\n"
        "{\n"
        '  "scenes": [\n'
        '    {"scene": 3, "seconds": 4, "cut": "push",\n'
        '     "css": "…this scene\'s COMPLETE rules and @keyframes…",\n'
        '     "html": "…this scene\'s COMPLETE markup…"}\n'
        "  ],\n"
        '  "design_css": "…only if the SHARED stylesheet changes — palette, '
        'type — and then the whole stylesheet, not a diff…",\n'
        '  "remove": []\n'
        "}\n\n"
        "RULES\n"
        "· Send only the scenes the change touches, but send each one "
        "COMPLETE — its full css and html — because it replaces the scene "
        "outright. Everything you do not send stays exactly as filmed.\n"
        "· A change to the whole reel (colours, typeface, the running "
        "header) is a change to `design_css`; send scenes as well only where "
        "their own markup must change.\n"
        f"· `scene` is the number from the list above, 1 to {total}. A number "
        f"past {total} appends a new scene at the end. `remove` lists scene "
        "numbers to drop.\n"
        f"· Same reel: the typefaces, header, footer and label scheme stay; a "
        f"counter reads n / {total}.\n"
        "· Artwork by name only — the names listed here and nothing else; a "
        "name that does not exist leaves a hole.\n"
        f"· No two texts may overlap and nothing may sit across copy; every "
        f"box inside the {W}x{H} frame and essential copy inside {SAFE_AREA}; "
        f"no text under {T_LABEL}px. The page is measured before it is filmed.\n"
        "· Preserve the existing visual system unless the owner explicitly "
        "asks for a whole-reel redesign. A follow-up should improve the named "
        "scene, not invent a new layout language for the film.\n"
        "· Shape refinement is intentional: reuse the reel's established "
        "geometric vocabulary, keep one hero construction and at most two "
        "supporting details in the changed scene, and remove generic filler "
        "circles, bars, rings, pills, or rectangles. Align edges and stroke "
        "weights to the existing grid.\n"
        "· HTML attributes in single quotes — the markup lives inside a JSON "
        "string.\n"
        "· No explanation, no description of the change — the JSON is the "
        "whole reply."
    )


def parse_followup(text: str, total: int) -> dict | None:
    """The change out of the reply: {"scenes": {index0: scene}, "remove":
    [index0…], "design_css": str|None}, or None if nothing usable came."""
    for got in _json_objects(text or ""):
        # A new storyboard is a plan, not an executable scene replacement.
        # Accepting its design.css alone silently left every old scene intact.
        if got.get("storyboard") and not got.get("scenes"):
            continue
        scenes, remove, design_css = {}, [], None
        rows = got.get("scenes")
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict) or not str(r.get("html", "")).strip():
                    continue
                try:
                    n = int(r.get("scene") if r.get("scene") is not None
                            else r.get("index", 0))
                except (TypeError, ValueError):
                    continue
                if n < 1:
                    continue
                sc = {"html": strip_citations(str(r["html"]))}
                if str(r.get("css", "")).strip():
                    sc["css"] = str(r["css"])
                for key in ("cut", "type"):
                    if str(r.get(key, "")).strip():
                        sc[key] = str(r[key]).strip()
                try:
                    sc["seconds"] = float(r["seconds"])
                except (KeyError, TypeError, ValueError):
                    pass
                scenes[n - 1] = sc
        rm = got.get("remove")
        if isinstance(rm, list):
            for x in rm:
                try:
                    n = int(x)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= total and n - 1 not in remove:
                    remove.append(n - 1)
            remove.sort()
        dc = got.get("design_css")
        if dc is None and isinstance(got.get("design"), dict):
            dc = got["design"].get("css")
        if isinstance(dc, str) and dc.strip():
            design_css = dc
        if scenes or remove or design_css:
            return {"scenes": scenes, "remove": remove, "design_css": design_css}
    return None


def apply_followup(spec: dict, patch: dict) -> dict:
    """The filmed spec with the change applied. A new dict; `spec` untouched.

    A hand edit (spec["edits"]) addresses an element by its position inside
    its scene, so it means nothing once that scene is rewritten — those are
    dropped. A removed scene shifts every later index, so edits from the
    first removal onward go too. Edits on untouched scenes stay.
    """
    import copy
    new = copy.deepcopy(spec)
    scenes = list(new.get("scenes") or [])
    changed = set()
    for idx in sorted(patch.get("scenes") or {}):
        sc = dict(patch["scenes"][idx])
        if idx < len(scenes):
            old = scenes[idx] if isinstance(scenes[idx], dict) else {}
            sc.setdefault("seconds", old.get("seconds", 4))
            sc.setdefault("cut", old.get("cut", ""))
            sc.setdefault("type", old.get("type", ""))
            scenes[idx] = sc
            changed.add(idx)
        else:
            sc.setdefault("seconds", 4.0)
            sc.setdefault("cut", "push")
            scenes.append(sc)
            changed.add(len(scenes) - 1)
    removed = sorted(i for i in (patch.get("remove") or []) if 0 <= i < len(scenes))
    for i in reversed(removed):
        del scenes[i]
    new["scenes"] = scenes
    if patch.get("design_css"):
        new.setdefault("design", {})["css"] = patch["design_css"]
    first_removed = removed[0] if removed else None
    kept = []
    for e in new.get("edits") or []:
        s = e.get("scene") if isinstance(e, dict) else None
        if not isinstance(s, int) or s in changed:
            continue
        if first_removed is not None and s >= first_removed:
            continue
        kept.append(e)
    if kept:
        new["edits"] = kept
    else:
        new.pop("edits", None)
    new.pop("_faults", None)
    ensure_accent_applied(new)
    return new


def refine_spec(spec: dict, change: str, ask, check=None, log=None,
                new_assets: str = "", context: str = "") -> tuple[dict, list[str]]:
    """Run the change through the design conversation and return (new spec,
    notes). `ask(prompt, expect) -> str` sends a turn in that tab; `check`
    is the same layout check build_spec uses. Raises ReelError when the
    reply holds nothing to film — nothing is changed in that case."""
    def say(msg):
        if log:
            log(msg)

    total = len(spec.get("scenes") or [])
    if not total:
        raise ReelError("There is no reel to change.")
    raw = ask(followup_instructions(change, spec, new_assets, context),
              SCENE_EXPECT) or ""
    patch = parse_followup(raw, total)
    if patch is None:
        raw = ask("A storyboard or description cannot be rendered. Send complete "
                  "HTML and CSS for each changed scene. Send the change again "
                  "as JSON only — first character '{', "
                  'last \'}\', keys "scenes" (each with "scene", "seconds", '
                  '"cut", "css", "html"), optional "design_css" and "remove", '
                  "in a ```json fenced block. Nothing before or after.",
                  SCENE_EXPECT) or ""
        patch = parse_followup(raw, total)
    if patch is None:
        raise ReelError("The art director did not answer with a change that "
                        "can be filmed — the reel was left as it was.")

    design = dict(spec.get("design") or {})
    if patch.get("design_css"):
        design["css"] = patch["design_css"]

    def faults_in(scene):
        if not check:
            return []
        try:
            return list(check({"design": design, "scenes": [scene],
                               "brand": spec.get("brand") or {},
                               "_assets": spec.get("_assets") or {}}) or [])
        except Exception as e:                           # noqa: BLE001
            say(f"couldn't lay the scene out ({e})")
            return []

    for idx in sorted(patch["scenes"]):
        faults = faults_in(patch["scenes"][idx])
        if not faults:
            continue
        say(f"scene {idx + 1} has {len(faults)} layout problem(s) — sending "
            "them back")
        fixed = parse_followup(ask(
            f"Scene {idx + 1} was laid out at 1080x1920 and these are wrong:"
            "\n\n" + "\n".join(f"{n}. {x}" for n, x in enumerate(faults[:8], 1))
            + f'\n\nSend the corrected scene {idx + 1}: ONLY the JSON object '
              '{"scenes": [{"scene": ' + str(idx + 1) + ', …}]}, same keys, '
              "in a ```json fenced block.",
            SCENE_EXPECT) or "", total)
        cand = ((fixed or {}).get("scenes") or {}).get(idx)
        if cand and len(faults_in(cand)) < len(faults):
            patch["scenes"][idx] = cand
            say("   fixed")
        else:
            say("   the correction was no better — keeping the first")

    what = []
    if patch["scenes"]:
        nums = ", ".join(str(i + 1) for i in sorted(patch["scenes"]))
        what.append(("scenes " if len(patch["scenes"]) > 1 else "scene ") + nums)
    if patch["remove"]:
        what.append("removed " + ", ".join(str(i + 1) for i in patch["remove"]))
    if patch.get("design_css"):
        what.append("the shared stylesheet")
    return apply_followup(spec, patch), ["changed " + "; ".join(what)]


def _fix_markup_quotes(block: str) -> str:
    """Escape the double quotes an art director left raw inside its markup.

    A design is JSON whose values are HTML, and the reply that comes back very
    often contains  "html": "<div class="content">…"  — valid HTML, invalid
    JSON, and the whole design lost over punctuation. The prompt asks for
    single quotes; this is what happens when it doesn't get them.

    Only quotes that look like HTML attributes are touched: `="` and the `"`
    that closes it. A quote that is genuinely ending a JSON string is followed
    by a comma, brace or colon, and is left alone.
    """
    import re
    return re.sub(r'=\\?"([^"\\]*?)\\?"(?=[\s>/])',
                  lambda m: "='" + m.group(1) + "'", block)


# A URL the chat UI turned into a clickable link, sitting inside the CSS.
#
# Anchored on the link TEXT starting with http, because that is the only thing
# these UIs linkify. CSS is full of brackets and parentheses — attribute
# selectors, url(), rgba() — and a looser pattern would happily eat them.
_LINKIFIED = re.compile(r"\[(https?://[^\]]*)\]\([^)\s]*\)")


def _unlink_markdown(block: str) -> str:
    """Undo the chat window's auto-linking of URLs inside the design.

    The failure, which looks like the model's fault and is not:

        "css": "@import url('[https://fonts.googleapis.com/…;*{box-sizing:
                border-box}html,body{…font-family:'DM](https://fonts.google…)
                 Sans',sans-serif}…"

    ChatGPT wrote perfectly good CSS. Its web UI then rendered the @import
    URL as a hyperlink, and because a stylesheet URL runs right up against the
    code after it, the anchor swallowed half the stylesheet as its link text.
    What Prism scraped off the page was markdown, not the CSS — so the JSON
    would not parse and the whole design was thrown away with "No JSON found
    in the agent's reply", which is the one message guaranteed to send someone
    looking in the wrong place.

    Taking the link TEXT rather than the href is the repair. The text is the
    literal characters the model typed; the href is the browser's percent-
    encoded guess at where they might point, and it has `)` rewritten to %29.
    """
    return _LINKIFIED.sub(lambda m: m.group(1), block)


def parse_spec(text: str) -> dict:
    """Pull the design/scene JSON out of an agent's reply — same balanced-brace
    scan as the Pillow renderer, since scrapes carry fences and preambles."""
    from . import reel as _pillow
    if not text or not text.strip():
        raise ReelError("The agent returned nothing to render.")
    # Before the brace scan, not merely as a repair candidate afterwards: the
    # swallowed link text carries its own unbalanced { and }, so _blocks would
    # be counting braces that were never really there and would cut the design
    # in the wrong place.
    text = _unlink_markdown(text)
    spec = None
    for block in _pillow._blocks(text, "{", "}"):
        for candidate in (block, _fix_markup_quotes(block),
                          _pillow._loosen(block),
                          _fix_markup_quotes(_pillow._loosen(block))):
            try:
                got = json.loads(candidate)
            except Exception:
                continue
            if isinstance(got, dict) and isinstance(got.get("scenes"), list) \
                    and got["scenes"]:
                spec = got
                break
        if spec:
            break
    if spec is None:
        raise ReelError("No JSON found in the agent's reply.")
    keep = [sc for sc in spec["scenes"]
            if isinstance(sc, dict) and str(sc.get("html", "")).strip()]
    if not keep:
        raise ReelError("The scenes carry no markup — nothing to render.")
    for sc in keep:
        sc["html"] = strip_citations(str(sc["html"]))
    spec["scenes"] = keep
    # Do this at authoring time, not only when the owner later opens Studio:
    # the JSON next to an MP4 is the permanent editable project source.
    from . import reel_edit
    reel_edit.ensure_stable_ids(spec)
    return spec


def still(spec: dict, at_frame: int, out_path: str) -> str:
    """One frame as a PNG. For looking at a design before committing to it."""
    from playwright.sync_api import sync_playwright
    from . import browser as _browser
    fps = int(spec.get("fps", DEFAULT_FPS))
    edits = []
    if spec.get("edits"):
        from . import reel_edit
        reel_edit.ensure_stable_ids(spec)
        edits = reel_edit.clean_edits(spec["edits"])
        spec = reel_edit.with_timing(spec, edits)
    html = build_html(spec, fps)
    if edits:
        html = reel_edit.apply_edits(html, edits)
    with sync_playwright() as p:
        browser = _browser.launch_chromium(p, args=["--hide-scrollbars",
                                                    "--font-render-hinting=none",
                                                    "--force-color-profile=srgb"])
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        try:
            page.set_content(html, wait_until="load")
            _browser.warm_fonts(page)
            try:
                page.wait_for_function("document.fonts.ready.then(()=>true)",
                                       timeout=8000)
            except Exception:
                pass
            _seek_page(page, at_frame * 1000.0 / fps)
            page.screenshot(path=out_path, type="png")
        finally:
            browser.close()
    return out_path
