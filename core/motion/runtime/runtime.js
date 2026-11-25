/**
 * Prism Motion Graphics Engine — Core Runtime (GSAP rebuild)
 * ─────────────────────────────────────────────────────────────
 * Real DOM/CSS paint (unchanged from the Aug-28 pivot) driven by ONE
 * paused, seekable `gsap.timeline()` instead of a hand-rolled per-frame
 * evaluator. resolver.py already flattens every node's enter/exit onto a
 * single global clock (each block's `time` is absolute, not scene-local)
 * — that's what makes "one master timeline, one .seek() call" the right
 * shape here, instead of Meridian's own per-scene-file timelines: Prism's
 * spec is already one unified document by the time it reaches this file.
 *
 * Matrix2D/Camera below are unchanged pure math (camera view matrix only,
 * never per-node — node transforms are handled by nested CSS transform
 * inheritance, the same way the DOM pivot always relied on it). Only
 * Node's animation machinery changed: GSAP owns every tween and every
 * seek; nothing here computes an eased value by hand.
 */

// ── 1. 2D Affine Transformation Matrix (camera view matrix only) ───────────
class Matrix2D {
  constructor(a = 1, b = 0, c = 0, d = 1, tx = 0, ty = 0) {
    this.a = a; this.b = b; this.c = c; this.d = d; this.tx = tx; this.ty = ty;
  }
  static identity() { return new Matrix2D(1, 0, 0, 1, 0, 0); }
  static translation(tx, ty) { return new Matrix2D(1, 0, 0, 1, tx, ty); }
  static rotation(rad) {
    const cos = Math.cos(rad), sin = Math.sin(rad);
    return new Matrix2D(cos, sin, -sin, cos, 0, 0);
  }
  static scaling(sx, sy) { return new Matrix2D(sx, 0, 0, sy, 0, 0); }
  multiply(m) {
    return new Matrix2D(
      this.a * m.a + this.c * m.b, this.b * m.a + this.d * m.b,
      this.a * m.c + this.c * m.d, this.b * m.c + this.d * m.d,
      this.a * m.tx + this.c * m.ty + this.tx, this.b * m.tx + this.d * m.ty + this.ty
    );
  }
  toCSSMatrix() { return `matrix(${this.a},${this.b},${this.c},${this.d},${this.tx},${this.ty})`; }
}

// Shared by every primitive for a CSS property GSAP can't safely tween as
// a raw string (filter:blur, clip-path, background-position-x, and any
// primitive-internal SVG stroke-dashoffset — see the channel design notes
// below Node). GSAP only ever tweens the plain number in `proxy.v`; the
// actual style write happens in `apply`, called on every onUpdate. This is
// the ONE place in the whole runtime any CSS value is computed by hand
// rather than left to GSAP — deliberately, so behavior never depends on
// unverified same-shape CSS string interpolation.
// `immediate` (default true) writes the from-value right away, which is
// what an ENTER wants (the node rests hidden until it enters). An EXIT
// passes false: its from-value is the settled state the enter already
// produced, and writing it at creation would overwrite the enter's
// hidden state — the node would show at full opacity before it entered.
function proxyTween(masterTl, apply, from, to, duration, ease, at, delay, immediate) {
  const proxy = { v: from };
  if (immediate !== false) apply(from);
  masterTl.fromTo(proxy, { v: from }, { v: to, duration, ease: ease || "power2.out", immediateRender: immediate !== false, onUpdate: () => apply(proxy.v) }, at + (delay || 0));
}
window.proxyTween = proxyTween;

function _hashSeed(seed) {
  let h = 0;
  const s = String(seed || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

// ── 2. Virtual Camera System — unchanged math, GSAP supplies the ease ──────
class Camera {
  constructor(viewportWidth = 1080, viewportHeight = 1920) {
    this.w = viewportWidth; this.h = viewportHeight;
    this.x = viewportWidth / 2; this.y = viewportHeight / 2;
    this.zoom = 1.0; this.rotation = 0.0; this.tracks = []; this.time = 0;
  }
  setTracks(tracks) {
    this.tracks = [...(tracks || [])].sort((a, b) => (a.time || 0) - (b.time || 0));
  }
  evaluate(time) {
    this.time = time;
    if (!this.tracks.length) return;
    let curX = this.w / 2, curY = this.h / 2, curZoom = 1.0, curRot = 0.0;
    for (let i = 0; i < this.tracks.length; i++) {
      const tr = this.tracks[i];
      const startTime = tr.time || 0.0;
      const duration = tr.duration || 0.0;
      // GSAP owns easing: gsap.parseEase(name) returns a plain (p)=>p
      // function for a named/parenthesized ease string, the same object
      // this project already validates against SUPPORTED_EASINGS in
      // schema.py — one ease authority for tweens AND this procedural
      // camera walk, not two.
      const easeFn = gsap.parseEase(tr.easing || "power2.inOut");

      if (time <= startTime && i === 0) {
        if (tr.position) { curX = tr.position[0]; curY = tr.position[1]; }
        if (tr.zoom !== undefined) curZoom = tr.zoom;
        if (tr.rotation !== undefined) curRot = tr.rotation * (Math.PI / 180);
        break;
      }
      if (time >= startTime + duration) {
        if (tr.position) { curX = tr.position[0]; curY = tr.position[1]; }
        if (tr.zoom !== undefined) curZoom = tr.zoom;
        if (tr.rotation !== undefined) curRot = tr.rotation * (Math.PI / 180);
      } else if (time > startTime) {
        const p = easeFn((time - startTime) / Math.max(0.001, duration));
        if (tr.position) {
          curX = curX + (tr.position[0] - curX) * p;
          curY = curY + (tr.position[1] - curY) * p;
        }
        if (tr.zoom !== undefined) curZoom = curZoom + (tr.zoom - curZoom) * p;
        if (tr.rotation !== undefined) {
          const targetRot = tr.rotation * (Math.PI / 180);
          curRot = curRot + (targetRot - curRot) * p;
        }
        break;
      }
    }
    this.x = curX; this.y = curY; this.zoom = curZoom; this.rotation = curRot;
  }
  getViewMatrix() {
    const driftX = Math.sin((this.time || 0) * 0.8) * 3.5 * (1 / Math.max(0.5, this.zoom));
    const driftY = Math.cos((this.time || 0) * 0.6) * 2.5 * (1 / Math.max(0.5, this.zoom));
    const tCenter = Matrix2D.translation(this.w / 2, this.h / 2);
    const sZoom = Matrix2D.scaling(this.zoom, this.zoom);
    const rRot = Matrix2D.rotation(-this.rotation);
    const tCam = Matrix2D.translation(-(this.x + driftX), -(this.y + driftY));
    return tCenter.multiply(sZoom).multiply(rRot).multiply(tCam);
  }
}

// ── 3. Channel → target-element mapping ─────────────────────────────────────
// Transform/opacity channels are direct GSAP CSS properties on `host`
// (GSAP's CSSPlugin composes x/y/scale/rotation/skew into one transform
// automatically — no hand-built transform string, unlike the old engine).
// Paint-level channels (blur/clipInset/backgroundPositionX/strokeDashoffset)
// go on `box` (or a primitive-declared SVG target for strokeDashoffset) via
// a numeric dummy proxy + onUpdate — deliberately NOT GSAP's string-to-
// string CSS interpolation, so behavior never depends on unverified
// same-shape-string-matching; a plain number tweened by GSAP is the one
// thing GSAP is unconditionally correct about.
const HOST_CHANNELS = new Set(["x", "y", "scale", "scaleX", "scaleY", "rotation", "skewX", "skewY", "opacity"]);
const BOX_PROXY_CHANNELS = new Set(["blur", "clipInset", "backgroundPositionX", "strokeDashoffset"]);

function _baseValueFor(node, channel) {
  switch (channel) {
    case "x": return node.position[0];
    case "y": return node.position[1];
    case "scale": return node.scale[0];
    case "scaleX": return node.scale[0];
    case "scaleY": return node.scale[1];
    case "rotation": return node.rotation;
    case "skewX": return 0;
    case "skewY": return 0;
    case "opacity": return node.opacity;
    default: return 0;
  }
}

// Writes one proxy-tweened value onto its real CSS target. `target` is
// `box` for blur/clipInset/backgroundPositionX; for strokeDashoffset it's
// whatever SVG element the primitive registered via node._strokeTarget in
// its own initDOM (falls back to `box` if the primitive never set one).
function _applyBoxChannel(channel, target, value) {
  switch (channel) {
    case "blur": target.style.filter = `blur(${Math.max(0, value)}px)`; return;
    case "clipInset": target.style.clipPath = `inset(${Math.max(0, 100 - value)}% 0 0 0)`; return;
    case "backgroundPositionX": target.style.backgroundPositionX = `${value}%`; return;
    case "strokeDashoffset": target.style.strokeDashoffset = String(value); return;
  }
}

// ── 4. Scene Graph Node ──────────────────────────────────────────────────────
class Node {
  constructor(props = {}) {
    this.id = props.id || `node_${Math.random().toString(36).substr(2, 9)}`;
    this.type = props.type || "group";
    this.position = props.position ? [...props.position] : [0, 0];
    this.scale = props.scale ? [...props.scale] : [1, 1];
    this.rotation = props.rotation || 0;
    this.anchor = (Array.isArray(props.anchor) && props.anchor.length === 2
      && typeof props.anchor[0] === "number" && typeof props.anchor[1] === "number")
      ? [...props.anchor] : [0.5, 0.5];
    this.opacity = props.opacity !== undefined ? props.opacity : 1.0;
    this.zIndex = props.z_index || 0;
    this.visible = props.visible !== undefined ? props.visible : true;
    this.blendMode = props.blend_mode || "source-over";
    this.animation = props.animation || null;
    this.children = [];
    this.parent = null;

    this._host = null;
    this._box = null;
    this._strokeTarget = null; // primitives with an internal SVG path set this in initDOM
  }

  addChild(child) { child.parent = this; this.children.push(child); return child; }

  // ── DOM mounting — once per node, not per frame ──────────────────────────
  // Same two-element shape as before: a zero-size *host* (carries the
  // transform, real children attach here so nested CSS transforms compose
  // for free) and a *box* inside it (sized, anchor-offset, carries paint).
  mount(parentEl) {
    const host = document.createElement("div");
    host.style.position = "absolute";
    host.style.left = "0";
    host.style.top = "0";
    host.style.transformOrigin = "0 0";
    host.style.zIndex = String(this.zIndex);
    host.style.mixBlendMode = this.blendMode === "source-over" ? "normal" : this.blendMode;
    if (!this.visible) host.style.display = "none";
    host.dataset.motionId = this.id; // the studio's selection handle (studio.js)

    const box = document.createElement("div");
    box.style.position = "absolute";
    box.style.left = "50%";
    box.style.top = "50%";
    box.style.transform = "translate(-50%,-50%)";
    host.appendChild(box);

    this._host = host;
    this._box = box;
    parentEl.appendChild(host);

    this.initDOM(box);

    const sorted = [...this.children].sort((a, b) => a.zIndex - b.zIndex);
    for (const child of sorted) child.mount(host);
  }

  // Subclass hook — build this node's STATIC inner DOM once. Base "group"
  // node has no paint of its own.
  initDOM(box) {}

  // Establishes host's resting transform/opacity (gsap.set — no tween, no
  // seek needed to see it) and registers every enter/exit tween on the
  // master timeline at its already-global absolute time. Called once per
  // node after every node in the spec is mounted, so children exist
  // before any tween touches them.
  registerAnimation(masterTl) {
    const host = this._host, box = this._box;

    gsap.set(host, {
      x: this.position[0], y: this.position[1],
      rotation: this.rotation, scaleX: this.scale[0], scaleY: this.scale[1],
      opacity: this.opacity,
    });

    if (this.animation) {
      for (const blockName of ["enter", "exit"]) {
        const block = this.animation[blockName];
        if (!block || !Array.isArray(block.tweens)) continue;
        const blockStart = block.time || 0;
        const duration = block.duration || 0.6;
        for (const tw of block.tweens) {
          const ease = tw.easing || "power2.out";
          const at = blockStart + (tw.delay || 0);
          // A fromTo renders its from-value at creation (immediateRender).
          // For an enter that is the point: the node rests hidden/offset
          // until its enter begins. For an exit, created AFTER the enter,
          // it would overwrite that hidden state with the settled one and
          // the node would flash at full opacity before entering — so an
          // exit never renders early.
          const immediate = blockName === "enter";
          if (HOST_CHANNELS.has(tw.channel)) {
            const base = _baseValueFor(this, tw.channel);
            const fromV = tw.channel === "opacity" ? tw.from : base + tw.from;
            const toV = tw.channel === "opacity" ? tw.to : base + tw.to;
            masterTl.fromTo(host, { [tw.channel]: fromV }, { [tw.channel]: toV, duration, ease, immediateRender: immediate }, at);
          } else if (BOX_PROXY_CHANNELS.has(tw.channel)) {
            const target = tw.channel === "strokeDashoffset" && this._strokeTarget ? this._strokeTarget : box;
            proxyTween(masterTl, v => _applyBoxChannel(tw.channel, target, v), tw.from, tw.to, duration, ease, at, 0, immediate);
          }
        }
      }

      // Seeded sine-ish wiggle for the WHOLE scene window a node is
      // present, expressed as a native GSAP repeat/yoyo oscillation
      // instead of hand-computed Math.sin() per frame — deterministic
      // (seeded phase via a start-time offset, no Math.random()), and one
      // fewer thing this file evaluates procedurally.
      const w = this.animation.secondary_motion;
      if (w && w.property && HOST_CHANNELS.has(w.property)) {
        const freq = Math.max(0.05, w.freq || 1.0);
        const amount = w.amount || 0;
        const base = _baseValueFor(this, w.property);
        const seed = _hashSeed(w.seed !== undefined ? w.seed : this.id);
        const phaseOffset = ((seed % 1000) / 1000) * (1 / freq); // deterministic start-phase, no Math.random
        const halfPeriod = 1 / (freq * 2);
        const windowStart = (this.animation.enter ? this.animation.enter.time : 0) - phaseOffset;
        const windowEnd = this.animation.exit ? this.animation.exit.time : 1e6;
        // repeat:-1 never truly "ends" — a seek at any time, including
        // past the node's own exit, still computes a valid value, so an
        // unbounded repeat is harmless on its own. Not yet verified: which
        // tween wins if secondary_motion and an exit block both target the
        // SAME channel on the SAME node (a real but narrow authoring
        // collision — the layer doctrine's usual split, background wiggles
        // / foreground exits, avoids it in practice). Flagged to check
        // against the first real multi-scene render rather than guessed at.
        masterTl.fromTo(host, { [w.property]: base - amount }, {
          [w.property]: base + amount, duration: halfPeriod, ease: "sine.inOut",
          repeat: -1, yoyo: true,
        }, Math.max(0, windowStart));
      }

      // `follow` (child lag behind a parent's recent motion) is not yet
      // ported to the GSAP timeline model — it needs reading a PARENT's
      // position at a past time, which one shared paused timeline can't
      // do without a second evaluation pass. Deferred: not used by any
      // primitive built so far, off-by-default in the old engine too.
    }

    // Subclass hook — a primitive's OWN internal content animation (text's
    // per-word/char stagger, a chart's line-draw, an SVG mark's stroke
    // draw-in) is a separate concern from the node's own enter/exit above,
    // which only ever moves/fades the node as one rigid unit.
    this.registerContentAnimation(masterTl);

    for (const child of this.children) child.registerAnimation(masterTl);
  }

  registerContentAnimation(masterTl) {}

  // Per-scene visibility only — content positioning/opacity is entirely
  // GSAP's job now via the master timeline seek, not written here.
  setVisible(on) {
    if (this._host) this._host.style.display = on ? "" : "none";
  }
}

// ── 5. Motion Graphics Runtime Host ──────────────────────────────────────────
class MotionRuntime {
  constructor(backdropCanvas, vignetteEl, grainEl, stageEl) {
    this.backdropCanvas = backdropCanvas;
    this.backdropCtx = backdropCanvas.getContext("2d", { alpha: false, desynchronized: true });
    this.vignetteEl = vignetteEl;
    this.grainEl = grainEl;
    this.stageEl = stageEl;
    this.width = 1080; this.height = 1920; this.fps = 30; this.duration = 10.0;
    this.background = "#090D16";
    this.camera = new Camera(this.width, this.height);
    this.effects = window.MotionEffects ? new window.MotionEffects(this.width, this.height) : null;
    this.rootNodes = [];
    this.sceneWindows = [];
    this.depthLayers = [];
    this.masterTl = null;
    this.spec = null;
  }

  loadSpec(spec) {
    this.spec = spec;
    const p = spec.project || {};
    this.width = p.width || 1080; this.height = p.height || 1920;
    this.fps = p.fps || 30; this.duration = p.duration || 10.0;
    this.background = p.background || "#090D16";

    this.backdropCanvas.width = this.width;
    this.backdropCanvas.height = this.height;

    const px = `${this.width}px`, pyh = `${this.height}px`;
    document.documentElement.style.width = px;
    document.documentElement.style.height = pyh;
    document.body.style.width = px;
    document.body.style.height = pyh;
    for (const el of [this.backdropCanvas, this.stageEl, this.vignetteEl, this.grainEl]) {
      el.style.width = px; el.style.height = pyh;
    }

    this.camera = new Camera(this.width, this.height);
    this.effects = window.MotionEffects ? new window.MotionEffects(this.width, this.height) : null;
    if (spec.camera && spec.camera.tracks) this.camera.setTracks(spec.camera.tracks);

    this._look = (p && p.look) || (spec.visual && spec.visual.look) || "tech";
    this._setupPostProcessing(this._resolveLook(spec.visual || {}));
    this._setupTheme(p);

    this.stageEl.innerHTML = "";
    this.rootNodes = [];
    this.sceneWindows = [];
    this.depthLayers = [];
    this.masterTl = gsap.timeline({ paused: true });

    const scenes = spec.scenes || [];
    for (const scene of scenes) {
      const sceneStart = scene.start || 0;
      const sceneEnd = sceneStart + (scene.duration || 0);
      const sceneEl = document.createElement("div");
      sceneEl.className = "scene";
      sceneEl.style.position = "absolute";
      sceneEl.style.inset = "0";
      sceneEl.style.display = "none";
      this.stageEl.appendChild(sceneEl);

      const sceneRoots = [];
      for (const nodeData of scene.nodes || []) {
        const node = createNodeFromSpec(nodeData);
        if (node) { sceneRoots.push(node); this.rootNodes.push(node); this._collectDepthLayers(node); }
      }
      sceneRoots.sort((a, b) => a.zIndex - b.zIndex);
      for (const root of sceneRoots) root.mount(sceneEl);
      for (const root of sceneRoots) root.registerAnimation(this.masterTl);

      // Visibility window extends into the overlap Prism's transition
      // library owns (scene.transitionInStart/transitionOutEnd, stamped
      // by resolve_motion_spec() — absent for a hard cut, in which case
      // this is just [start, end)) so a scene's own content is present
      // for the FULL duration a cross-cut transition needs it visible.
      const winStart = scene.transitionInStart !== undefined ? scene.transitionInStart : sceneStart;
      const winEnd = scene.transitionOutEnd !== undefined ? scene.transitionOutEnd : sceneEnd;
      this.sceneWindows.push({ el: sceneEl, start: winStart, end: winEnd });
    }

    if (window.MotionTransitions) {
      window.MotionTransitions.registerAll(this.masterTl, scenes, this.sceneWindows, this.width, this.height, this.stageEl);
    }
  }

  // Depth layers (primitives.js DepthLayerNode) anywhere in the tree —
  // each gets its parallax transform refreshed on every seek, after the
  // camera is evaluated, since it depends on the camera's current state.
  _collectDepthLayers(node) {
    if (window.DepthLayerNode && node instanceof window.DepthLayerNode) this.depthLayers.push(node);
    for (const child of node.children) this._collectDepthLayers(child);
  }

  _setupTheme(project) {
    const type = project.type || {};
    if (type.google_fonts_url) {
      const existing = document.getElementById("motion-google-fonts");
      if (existing) existing.remove();
      const link = document.createElement("link");
      link.id = "motion-google-fonts";
      link.rel = "stylesheet";
      link.href = type.google_fonts_url;
      document.head.appendChild(link);
    }
    document.documentElement.style.setProperty("--motion-display-font", type.display_font ? `"${type.display_font}"` : "inherit");
    document.documentElement.style.setProperty("--motion-body-font", type.body_font ? `"${type.body_font}"` : "inherit");
    const palette = project.palette || {};
    for (const key of ["bg_a", "bg_b", "ink", "accent", "accent2"]) {
      if (palette[key]) document.documentElement.style.setProperty(`--motion-${key}`, palette[key]);
    }
  }

  // A look is a set of defaults for everything the backdrop paints on its own.
  // Until 12 Nov 2026 there was only one, a dark-tech one (a cyan spotlight
  // fading to navy, a scrolling grid, cyan particles, a 0.58 vignette, film
  // grain), applied to EVERY film whatever its background — on a light page
  // it turned the whole frame a blue-grey wash. "tech" is that look, kept
  // exactly; "paper" and "ink" are flat. Explicit `visual` keys still win.
  _resolveLook(visual) {
    const none = "none";
    const LOOKS = {
      paper: { spotlight: { color: none, secondary: none }, grid: { enabled: false },
               particles: { enabled: false }, vignette_strength: 0, grain_opacity: 0 },
      ink:   { spotlight: { color: "rgba(255,255,255,0.05)", secondary: "rgba(255,255,255,0.02)" },
               grid: { enabled: false }, particles: { enabled: false },
               vignette_strength: 0.22, grain_opacity: 0.03 },
    };
    const preset = LOOKS[this._look];
    return preset ? Object.assign({}, preset, visual) : visual;
  }

  _setupPostProcessing(visual) {
    const vigStrength = visual.vignette_strength !== undefined ? visual.vignette_strength : 0.58;
    if (vigStrength > 0.001) {
      this.vignetteEl.style.background =
        `radial-gradient(circle at 50% 50%, rgba(255,255,255,1.0) 0%, ` +
        `rgba(210,218,235,0.97) 62%, rgba(8,10,18,${vigStrength}) 100%)`;
      this.vignetteEl.style.display = "block";
    } else {
      this.vignetteEl.style.display = "none";
    }
    const grainOpacity = visual.grain_opacity !== undefined ? visual.grain_opacity : 0.05;
    if (grainOpacity > 0.001 && this.effects && this.effects._noiseCanvas) {
      this.grainEl.style.backgroundImage = `url(${this.effects._noiseCanvas.toDataURL()})`;
      this.grainEl.style.backgroundRepeat = "repeat";
      this.grainEl.style.opacity = String(grainOpacity);
      this.grainEl.style.display = "block";
    } else {
      this.grainEl.style.display = "none";
    }
  }

  pendingImageCount() {
    const imgs = this.stageEl.querySelectorAll("img");
    let pending = 0;
    for (const img of imgs) if (!img.complete) pending++;
    return pending;
  }

  seek(frame) {
    const time = frame / this.fps;
    this.camera.evaluate(time);
    this.masterTl.seek(time, false); // false: don't fire onComplete/onStart callbacks, only onUpdate — a re-seek must never re-trigger one-shot side effects

    for (const sw of this.sceneWindows) {
      const on = time >= sw.start && time < sw.end;
      sw.el.style.display = on ? "" : "none";
    }

    this.stageEl.style.transform = this.camera.getViewMatrix().toCSSMatrix();
    for (const layer of this.depthLayers || []) layer.applyParallax(this.camera, this.width, this.height);

    const visual = Object.assign({ background: this.background },
      this._resolveLook((this.spec && this.spec.visual) ? this.spec.visual : {}));
    const bctx = this.backdropCtx;
    bctx.setTransform(1, 0, 0, 1, 0, 0);
    if (this.effects) {
      this.effects.drawStudioBackdrop(bctx, visual, this.camera, time);
      this.effects.drawAmbientParticles(bctx, visual, this.camera, time);
    } else {
      bctx.fillStyle = visual.background || this.background;
      bctx.fillRect(0, 0, this.width, this.height);
    }
  }
}

window.Matrix2D = Matrix2D;
window.Camera = Camera;
window.Node = Node;
window.MotionRuntime = MotionRuntime;
window._hashSeed = _hashSeed;
