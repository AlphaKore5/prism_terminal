/**
 * Prism Motion Graphics Engine — Node subclasses (GSAP rebuild)
 * ─────────────────────────────────────────────────────────────
 * Paint is real DOM/CSS (unchanged from the Aug-28 pivot). What changed
 * with the GSAP rewrite: any primitive with its OWN internal content
 * animation (TextNode's 8 modes, ShapeArrowNode's draw-on) now builds real
 * GSAP tweens ONCE via registerContentAnimation(masterTl), instead of
 * computing a value every frame in an updateDOM(time) override — see
 * runtime.js's Node.registerContentAnimation for the hook shape, and
 * proxyTween()/window.proxyTween for the shared non-GSAP-native-property
 * helper (blur, clip-path, background-position-x, stroke-dashoffset).
 *
 * ShapeCircleNode and ShapeArrowNode previously carried unused Canvas2D
 * draw(ctx,time) methods, kept only as reference for this exact port —
 * both are real DOM/SVG now, and draw() is gone.
 */

// ── ShapeRectNode — unchanged, paint-only (no animation of its own) ───────────
class ShapeRectNode extends Node {
  constructor(props = {}) {
    super(props);
    this.width        = props.width        || 200;
    this.height       = props.height       || 100;
    this.radius       = props.radius       || 0;
    this.fill         = props.fill         || "#FFFFFF";
    this.stroke       = props.stroke       || null;
    this.strokeWidth  = props.stroke_width || 0;
    this.shadowColor  = props.shadow_color || null;
    this.shadowBlur   = props.shadow_blur  || 0;
    this.shadowOffsetY= props.shadow_offset_y || 0;
    this.isGlass      = props.is_glass     !== undefined ? props.is_glass : false;
    this.glowColor    = props.glow_color   || null;
    this.glowBlur     = props.glow_blur    || 0;
  }

  initDOM(box) {
    const w = this.width, h = this.height;
    box.style.transform = "";
    box.style.left   = `${-w * this.anchor[0]}px`;
    box.style.top    = `${-h * this.anchor[1]}px`;
    box.style.width  = `${w}px`;
    box.style.height = `${h}px`;
    const r = Math.min(this.radius, w / 2, h / 2);
    box.style.borderRadius = `${r}px`;

    if (this.isGlass) {
      box.style.backdropFilter = "blur(20px)";
      box.style.webkitBackdropFilter = "blur(20px)";
      box.style.background = this.fill || "rgba(13,18,38,0.55)";
      box.style.border = "1px solid rgba(255,255,255,0.10)";
      box.style.boxShadow =
        "0 20px 42px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06)";
      return;
    }

    if (this.fill && this.fill !== "none") box.style.background = this.fill;
    if (this.stroke && this.strokeWidth > 0) {
      box.style.boxSizing = "border-box";
      box.style.border = `${this.strokeWidth}px solid ${this.stroke}`;
    }
    const shadows = [];
    if (this.shadowColor && this.shadowBlur > 0) {
      shadows.push(`0 ${this.shadowOffsetY}px ${this.shadowBlur}px ${this.shadowColor}`);
    }
    if (this.glowColor && this.glowBlur > 0) {
      shadows.push(`0 0 ${this.glowBlur}px ${this.glowColor}`);
    }
    if (shadows.length) box.style.boxShadow = shadows.join(", ");
  }
}

// ── ShapeCircleNode — real DOM now (was dead Canvas2D; trivial to port) ──────
class ShapeCircleNode extends Node {
  constructor(props = {}) {
    super(props);
    this.radius      = props.radius      || 50;
    this.fill         = props.fill         || "#FFFFFF";
    this.stroke       = props.stroke       || null;
    this.strokeWidth  = props.stroke_width || 0;
    this.glowColor    = props.glow_color   || null;
    this.glowBlur     = props.glow_blur    || 0;
  }

  initDOM(box) {
    const d = this.radius * 2;
    box.style.transform = "";
    box.style.left   = `${-d * this.anchor[0]}px`;
    box.style.top    = `${-d * this.anchor[1]}px`;
    box.style.width  = `${d}px`;
    box.style.height = `${d}px`;
    box.style.borderRadius = "50%";
    if (this.fill && this.fill !== "none") box.style.background = this.fill;
    if (this.stroke && this.strokeWidth > 0) {
      box.style.boxSizing = "border-box";
      box.style.border = `${this.strokeWidth}px solid ${this.stroke}`;
    }
    if (this.glowColor && this.glowBlur > 0) {
      box.style.boxShadow = `0 0 ${this.glowBlur}px ${this.glowColor}`;
    }
  }
}

// ── ShapeArrowNode — real SVG now (was dead Canvas2D) ─────────────────────────
// The bezier control-point/point-sampling math below is the OLD engine's
// own draw() geometry, unchanged — pure math, never canvas-specific — just
// building an SVG path `d` string instead of ctx.lineTo calls.
class ShapeArrowNode extends Node {
  constructor(props = {}) {
    super(props);
    this.from         = props.from         || [0, 0];
    this.to           = props.to           || [100, 0];
    this.curved       = props.curved       || false;
    this.curveHeight  = props.curve_height || 40;
    this.color        = props.color        || "#38BDF8";
    this.strokeWidth  = props.stroke_width || 6;
    this.headSize     = props.head_size    || 22;
    this.glowBlur     = props.glow_blur    || 14;
    this.drawStart    = props.draw_start   || 0.0;
    this.drawDuration = props.draw_duration || 0.65;
    this.pulse        = props.pulse        || false;
    this.pulseColor   = props.pulse_color  || "#FFFFFF";
    this.pulseSpeed   = props.pulse_speed  || 0.9;
  }

  initDOM(box) {
    const [x1, y1] = this.from, [x2, y2] = this.to;
    const dx = x2 - x1, dy = y2 - y1;
    const dist = Math.hypot(dx, dy) || 1;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const nx = -dy / dist, ny = dx / dist;
    const cx = this.curved ? mx + nx * this.curveHeight : mx;
    const cy = this.curved ? my + ny * this.curveHeight : my;
    const tangentEnd = Math.atan2(y2 - cy, x2 - cx) * (180 / Math.PI);

    const minX = Math.min(x1, x2, cx) - this.headSize - this.glowBlur;
    const minY = Math.min(y1, y2, cy) - this.headSize - this.glowBlur;
    const maxX = Math.max(x1, x2, cx) + this.headSize + this.glowBlur;
    const maxY = Math.max(y1, y2, cy) + this.headSize + this.glowBlur;
    const w = maxX - minX, h = maxY - minY;

    box.style.transform = "";
    box.style.left = `${minX}px`; box.style.top = `${minY}px`;
    box.style.width = `${w}px`; box.style.height = `${h}px`;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.style.overflow = "visible";

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const d = this.curved
      ? `M ${x1 - minX} ${y1 - minY} Q ${cx - minX} ${cy - minY} ${x2 - minX} ${y2 - minY}`
      : `M ${x1 - minX} ${y1 - minY} L ${x2 - minX} ${y2 - minY}`;
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", this.color);
    path.setAttribute("stroke-width", String(this.strokeWidth));
    path.setAttribute("stroke-linecap", "round");
    if (this.glowBlur > 0) path.style.filter = `drop-shadow(0 0 ${this.glowBlur}px ${this.color})`;
    svg.appendChild(path);

    const head = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
    const hs = this.headSize;
    head.setAttribute("points", `0,0 ${-hs * 1.3},${-hs * 0.6} ${-hs * 0.85},0 ${-hs * 1.3},${hs * 0.6}`);
    head.setAttribute("fill", this.color);
    head.setAttribute("transform", `translate(${x2 - minX},${y2 - minY}) rotate(${tangentEnd})`);
    head.style.opacity = "0"; // fades in with the draw-on, see registerContentAnimation
    svg.appendChild(head);

    box.appendChild(svg);
    this._path = path;
    this._head = head;
    this._pathLength = path.getTotalLength();
    path.style.strokeDasharray = String(this._pathLength);
    path.style.strokeDashoffset = String(this._pathLength);
    this._strokeTarget = path; // lets a spec-authored strokeDashoffset tween (see TWEEN_CHANNELS) target this path directly too
  }

  registerContentAnimation(masterTl) {
    const at = this.drawStart, dur = Math.max(0.05, this.drawDuration);
    proxyTween(masterTl, v => { this._path.style.strokeDashoffset = String(v); },
      this._pathLength, 0, dur, "power2.out", at);
    masterTl.fromTo(this._head, { opacity: 0, scale: 0 }, { opacity: 1, scale: 1, duration: 0.3, ease: "back.out(2)" }, at + dur * 0.6);
    if (this.pulse) {
      // A traveling highlight once the line is fully drawn — a small dot
      // orbiting the path via GSAP's MotionPathPlugin isn't in core
      // gsap.min.js, so this samples the SAME bezier by hand (identical
      // geometry to initDOM's own d= computation) and drives x/y directly.
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("r", String(this.strokeWidth * 1.4));
      dot.setAttribute("fill", this.pulseColor);
      dot.style.filter = `drop-shadow(0 0 10px ${this.pulseColor})`;
      this._path.parentNode.appendChild(dot);
      const proxy = { t: 0 };
      const [x1, y1] = this.from, [x2, y2] = this.to;
      const dx = x2 - x1, dy = y2 - y1;
      const dist = Math.hypot(dx, dy) || 1;
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const nx = -dy / dist, ny = dx / dist;
      const cx = this.curved ? mx + nx * this.curveHeight : mx;
      const cy = this.curved ? my + ny * this.curveHeight : my;
      const place = (t) => {
        const it = 1 - t;
        const px = it * it * x1 + 2 * it * t * cx + t * t * x2;
        const py = it * it * y1 + 2 * it * t * cy + t * t * y2;
        const boxLeft = parseFloat(this._path.parentNode.parentNode.style.left || "0");
        dot.setAttribute("cx", String(px - boxLeft));
        dot.setAttribute("cy", String(py - parseFloat(this._path.parentNode.parentNode.style.top || "0")));
      };
      masterTl.fromTo(proxy, { t: 0 }, {
        t: 1, duration: 1 / this.pulseSpeed, ease: "none", repeat: -1,
        onUpdate: () => place(proxy.t),
      }, at + dur);
    }
  }
}

// ── TextNode — Full Kinetic Typography Toolkit (GSAP rebuild) ────────────────
class TextNode extends Node {
  constructor(props = {}) {
    super(props);
    this.content        = String(props.content || "");
    this.fontFamily     = props.font_family    || "var(--motion-body-font, Inter, -apple-system, sans-serif)";
    this.fontSize       = props.font_size      || 48;
    this.fontWeight     = props.font_weight    || 700;
    this.fill           = props.fill           || "#FFFFFF";
    // Lines inside a left-anchored block read left, a right-anchored one
    // right — unless the spec says otherwise. Centred lines in a block
    // whose anchor is its left edge look like a mistake, not a choice.
    this.align          = props.align
      || (this.anchor[0] < 0.34 ? "left" : this.anchor[0] > 0.66 ? "right" : "center");
    this.lineHeight     = props.line_height    || 1.25;
    this.mode           = props.mode           || "standard";
    // `runs`: [{text, fill?, gradient?, weight?}] — one line (or several, split
    // on "\n") whose parts differ in colour/weight. `type_on` reveals it one
    // character at a time with a caret that follows, at `type_rate` per second.
    this.runs           = Array.isArray(props.runs) && props.runs.length ? props.runs : null;
    this.typeOn         = !!props.type_on;
    this.typeRate       = props.type_rate      || 16;
    this.caret          = props.caret !== undefined ? !!props.caret : true;
    this.revealStart    = props.reveal_start   || 0.0;
    this.revealDuration = props.reveal_duration || 0.75;
    this.gradient       = props.gradient       || null;
    this.shadow         = props.text_shadow    || null;
    this.letterSpacing  = props.letter_spacing || 0;

    this.staggerDelay   = props.stagger_delay  || 0.075;
    this.shimmerColor   = props.shimmer_color  || "rgba(255,255,255,0.55)";
    this.shimmerWidth   = props.shimmer_width  || 0.35;
    this.splitGap       = props.split_gap      || 40;
    this.blurAmount     = props.blur_amount    || 24;
    this.prefix = props.prefix || "";
    this.suffix = props.suffix || "";
    this.locale = props.locale || undefined; // e.g. "en-IN" for ₹1,00,000-style grouping, matching the Meridian reference's own toLocaleString("en-IN")
  }

  _applyBaseTextStyle(el) {
    el.style.fontFamily = this.fontFamily;
    el.style.fontSize = `${this.fontSize}px`;
    el.style.fontWeight = String(this.fontWeight);
    el.style.lineHeight = String(this.lineHeight);
    el.style.whiteSpace = "pre";
    if (this.letterSpacing) el.style.letterSpacing = `${this.letterSpacing}px`;
    if (this.gradient && this.gradient.stops) {
      const stops = this.gradient.stops.slice().sort((a, b) => a.pos - b.pos)
        .map(s => `${s.color} ${Math.round(s.pos * 100)}%`).join(", ");
      el.style.backgroundImage = `linear-gradient(90deg, ${stops})`;
      el.style.webkitBackgroundClip = "text";
      el.style.backgroundClip = "text";
      el.style.color = "transparent";
    } else {
      el.style.color = this.fill;
    }
    if (this.shadow) {
      const c = this.shadow.color !== undefined ? this.shadow.color : "rgba(0,0,0,0.5)";
      const b = this.shadow.blur !== undefined ? this.shadow.blur : 12;
      const oy = this.shadow.offsetY !== undefined ? this.shadow.offsetY : 4;
      el.style.textShadow = `0 ${oy}px ${b}px ${c}`;
    }
  }

  initDOM(box) {
    // The anchor, honoured the way every other primitive honours it. A
    // percentage translate is measured against the box's OWN laid-out
    // size, so [0.5, 0.5] is the old translate(-50%,-50%) exactly and
    // [0, 0] puts the text's top-left corner on `position` — which is
    // what generate.py's catalogue promises the model and what
    // inspect._node_bounds measures. Before this, text was the one node
    // type that ignored its anchor: a headline the model left-anchored
    // at x=80 was centred ON x=80 and lost its first half off the frame
    // (the 11 Nov 2026 study, finding M1).
    box.style.transform = `translate(${-this.anchor[0] * 100}%, ${-this.anchor[1] * 100}%)`;
    box.style.textAlign = this.align;
    if (this.runs) { this._initRuns(box); return; }
    this._lines = this.content ? String(this.content).split(/\r?\n/) : [];
    this._lineEls = [];
    this._lineState = [];
    for (const line of this._lines) {
      const el = document.createElement("div");
      this._applyBaseTextStyle(el);
      box.appendChild(el);
      this._lineEls.push(el);
      this._lineState.push(this._initLineMode(el, line));
    }
  }

  // Builds this line's STATIC per-mode DOM once — unchanged in spirit from
  // the previous engine; only what drives the numbers changed.
  _initLineMode(el, line) {
    switch (this.mode) {
      case "word_stagger": {
        const words = line.split(" ");
        const spans = words.map((w, i) => {
          const s = document.createElement("span");
          s.style.display = "inline-block";
          s.style.whiteSpace = "pre";
          s.style.opacity = "0";
          s.textContent = w + (i < words.length - 1 ? " " : "");
          el.appendChild(s);
          return s;
        });
        return { spans };
      }
      case "char_cascade": {
        const spans = line.split("").map(c => {
          const s = document.createElement("span");
          s.style.display = "inline-block";
          s.style.whiteSpace = "pre";
          s.style.opacity = "0";
          s.textContent = c;
          el.appendChild(s);
          return s;
        });
        return { spans };
      }
      case "split_slide": {
        el.style.position = "relative";
        el.textContent = "";
        // An in-flow, invisible copy keeps the line's own size: the two
        // halves below are absolutely positioned and contribute nothing to
        // layout, so without this the line — and the whole text box — is
        // 0x0 and the halves are clipped to nothing. Every split_slide
        // caption in the third Alphakore run (10 Sep 2026) vanished this way.
        const ghost = document.createElement("span");
        ghost.style.visibility = "hidden";
        ghost.style.whiteSpace = "pre";
        ghost.textContent = line;
        el.appendChild(ghost);
        const mk = (side) => {
          const s = document.createElement("span");
          s.style.position = "absolute";
          s.style.left = "0"; s.style.top = "0"; s.style.width = "100%";
          s.style.whiteSpace = "pre";
          s.style.clipPath = side === "left" ? "inset(0 50% 0 0)" : "inset(0 0 0 50%)";
          s.textContent = line;
          el.appendChild(s);
          return s;
        };
        return { left: mk("left"), right: mk("right") };
      }
      case "shimmer_sweep": {
        el.style.position = "relative";
        el.textContent = "";
        const base = document.createElement("span");
        base.style.whiteSpace = "pre";
        base.style.opacity = "0";
        base.textContent = line;
        el.appendChild(base);

        const shimmer = document.createElement("span");
        shimmer.style.position = "absolute";
        shimmer.style.left = "0"; shimmer.style.top = "0";
        shimmer.style.whiteSpace = "pre";
        shimmer.textContent = line;
        shimmer.style.backgroundRepeat = "no-repeat";
        shimmer.style.webkitBackgroundClip = "text";
        shimmer.style.backgroundClip = "text";
        shimmer.style.color = "transparent";
        shimmer.style.opacity = "0";
        shimmer.style.backgroundImage =
          `linear-gradient(90deg, rgba(255,255,255,0) 0%, ${this.shimmerColor} 40%, ` +
          `${this.shimmerColor} 60%, rgba(255,255,255,0) 100%)`;
        shimmer.style.backgroundSize = "250% 100%";
        el.appendChild(shimmer);
        return { base, shimmer };
      }
      case "typewriter": {
        el.textContent = "";
        const textSpan = document.createElement("span");
        textSpan.style.whiteSpace = "pre";
        const cursor = document.createElement("span");
        cursor.style.display = "inline-block";
        cursor.style.width = "3px";
        cursor.style.height = `${this.fontSize * 0.85}px`;
        cursor.style.verticalAlign = "-0.1em";
        cursor.style.marginLeft = "4px";
        cursor.style.background = "currentColor";
        el.appendChild(textSpan); el.appendChild(cursor);
        return { textSpan, cursor };
      }
      case "masked_reveal":
        el.textContent = line;
        el.style.clipPath = "inset(100% 0 0 0)";
        return {};
      case "blur_pop":
        el.textContent = line;
        el.style.opacity = "0";
        return {};
      case "counter_tick":
        el.textContent = this.prefix + "0" + this.suffix;
        return {};
      case "standard":
      default:
        el.textContent = line;
        return {};
    }
  }

  _initRuns(box) {
    const lines = [[]];
    for (const r of this.runs) {
      String(r.text || "").split(/\r?\n/).forEach((part, k) => {
        if (k > 0) lines.push([]);
        if (part) lines[lines.length - 1].push({ ...r, text: part });
      });
    }
    this._lineEls = []; this._lines = []; this._lineState = [];
    this._runChars = []; this._runCarets = [];
    const caretColour = this.fill && !this.gradient ? this.fill : "#111111";
    for (const line of lines) {
      const el = document.createElement("div");
      this._applyBaseTextStyle(el);
      for (const r of line) {
        const span = document.createElement("span");
        span.style.whiteSpace = "pre";
        if (r.weight) span.style.fontWeight = String(r.weight);
        if (r.font_family) span.style.fontFamily = r.font_family;
        if (r.gradient && r.gradient.stops) {
          span.style.backgroundImage = _gradientCss(r.gradient);
          span.style.webkitBackgroundClip = "text"; span.style.backgroundClip = "text";
          span.style.color = "transparent";
        } else if (r.fill) {
          span.style.color = r.fill;
        }
        if (this.typeOn) {
          for (const ch of Array.from(r.text)) {
            const c = document.createElement("span");
            c.textContent = ch; c.style.whiteSpace = "pre"; c.style.visibility = "hidden";
            span.appendChild(c); this._runChars.push(c);
            if (this.caret) { const k = _caretEl(this.fontSize, r.caret_fill || caretColour); span.appendChild(k); this._runCarets.push(k); }
            else this._runCarets.push(null);
          }
        } else {
          span.textContent = r.text;
        }
        el.appendChild(span);
      }
      box.appendChild(el);
      this._lineEls.push(el); this._lines.push(line.map(r => r.text).join("")); this._lineState.push({});
    }
  }

  registerContentAnimation(masterTl) {
    if (this.runs) {
      if (this._runChars && this._runChars.length)
        _typeOn(masterTl, this._runChars, this._runCarets, this.revealStart, this.typeRate, this.caret);
      return;
    }
    for (let i = 0; i < this._lineEls.length; i++) {
      this._registerLineMode(this._lineEls[i], this._lines[i], this._lineState[i], i, masterTl);
    }
  }

  _registerLineMode(el, line, state, lineIdx, masterTl) {
    switch (this.mode) {
      case "word_stagger": {
        const wDur = 0.48;
        state.spans.forEach((s, i) => {
          const at = this.revealStart + i * this.staggerDelay;
          masterTl.fromTo(s, { opacity: 0, y: 30, scale: 0.78 },
            { opacity: 1, y: 0, scale: 1, duration: wDur, ease: "back.out(1.7)" }, at);
        });
        return;
      }
      case "char_cascade": {
        const cDur = 0.38;
        state.spans.forEach((s, i) => {
          const at = this.revealStart + i * (this.staggerDelay * 0.55);
          masterTl.fromTo(s, { opacity: 0, y: -80 },
            { opacity: 1, y: 0, duration: cDur, ease: "bounce.out" }, at);
        });
        return;
      }
      case "split_slide": {
        const off = this.splitGap + 200;
        masterTl.fromTo(state.left, { x: -off }, { x: 0, duration: this.revealDuration, ease: "expo.out" }, this.revealStart);
        masterTl.fromTo(state.right, { x: off }, { x: 0, duration: this.revealDuration, ease: "expo.out" }, this.revealStart);
        return;
      }
      case "shimmer_sweep": {
        const fadeDur = this.revealDuration * 0.5;
        masterTl.fromTo(state.base, { opacity: 0 }, { opacity: 1, duration: fadeDur, ease: "power2.out" }, this.revealStart);
        const sweepAt = this.revealStart + fadeDur;
        masterTl.set(state.shimmer, { opacity: 1 }, sweepAt);
        proxyTween(masterTl, v => { state.shimmer.style.backgroundPositionX = `${v}%`; },
          100, -100, this.revealDuration * 0.6, "power2.inOut", sweepAt);
        return;
      }
      case "typewriter": {
        const rate = Math.max(1, line.length) / Math.max(0.1, this.revealDuration);
        proxyTween(masterTl, v => { state.textSpan.textContent = line.substring(0, Math.floor(v)); },
          0, line.length, Math.max(0.1, this.revealDuration), "none", this.revealStart);
        // Cursor blink is its own repeat:-1 tween so a seek anywhere still
        // shows the correct phase — never a live/wall-clock blink.
        masterTl.fromTo(state.cursor, { opacity: 1 }, { opacity: 0, duration: 0.5, ease: "steps(1)", repeat: -1, yoyo: true }, this.revealStart);
        return;
      }
      case "masked_reveal":
        proxyTween(masterTl, v => { el.style.clipPath = `inset(${Math.max(0, 100 - v)}% 0 0 0)`; },
          0, 100, this.revealDuration, "power2.out", this.revealStart);
        return;
      case "blur_pop":
        masterTl.fromTo(el, { opacity: 0, scale: 0.7 }, { opacity: 1, scale: 1, duration: this.revealDuration, ease: "back.out(1.7)" }, this.revealStart);
        proxyTween(masterTl, v => { el.style.filter = `blur(${Math.max(0, v)}px)`; },
          this.blurAmount, 0, this.revealDuration, "back.out(1.7)", this.revealStart);
        return;
      case "counter_tick": {
        const target = parseFloat(line) || 0;
        const decimals = line.includes(".") ? line.split(".")[1].length : 0;
        proxyTween(masterTl, v => {
          const num = decimals > 0 ? v.toFixed(decimals) : Math.floor(v).toLocaleString(this.locale);
          el.textContent = this.prefix + num + this.suffix;
        }, 0, target, this.revealDuration, "power2.out", this.revealStart);
        return;
      }
      case "standard":
      default:
        return; // static — set once in _initLineMode
    }
  }
}

// ── ImageNode — unchanged, paint/load-state only ──────────────────────────────
class ImageNode extends Node {
  constructor(props = {}) {
    super(props);
    this.src    = props.src    || null;
    this.width  = props.width  || 200;
    this.height = props.height || 200;
    this.radius = props.radius || 12;
  }

  initDOM(box) {
    const w = this.width, h = this.height;
    box.style.transform = "";
    box.style.left   = `${-w * this.anchor[0]}px`;
    box.style.top    = `${-h * this.anchor[1]}px`;
    box.style.width  = `${w}px`;
    box.style.height = `${h}px`;
    const r = Math.min(this.radius, w / 2, h / 2);
    box.style.borderRadius = `${r}px`;
    box.style.overflow = "hidden";
    box.style.background = "rgba(15,23,42,0.75)";
    box.style.backdropFilter = "blur(20px)";
    box.style.webkitBackdropFilter = "blur(20px)";

    const img = document.createElement("img");
    img.style.width = "100%";
    img.style.height = "100%";
    // "fill" (stretch to the box, ignoring aspect ratio) faithfully matched
    // the old engine's drawImage(img,x,y,w,h) — but a live test surfaced
    // the real cost: a brand asset (a wordmark/logo, naturally wide) forced
    // into a box whose aspect ratio doesn't match gets smeared into
    // illegible mush. "contain" never distorts — worst case is letterbox
    // padding inside the box, which reads as an intentional frame, not a
    // rendering defect. A real, confirmed-by-evidence behavior change, not
    // a port choice.
    img.style.objectFit = "contain";
    img.style.display = "block";
    img.addEventListener("load", () => {
      box.style.background = "";
      box.style.backdropFilter = "";
      box.style.webkitBackdropFilter = "";
    });
    img.addEventListener("error", () => { img.style.display = "none"; });
    if (this.src) img.src = this.src;
    box.appendChild(img);
    this._img = img;
  }
}

// ── Material primitives (Phase 2 of the inspo benchmark) ─────────────────────
// A colour the spec wrote as #rrggbb (or #rgb) becomes rgba() at `alpha`;
// anything else (an rgba()/hsla()/named colour) is returned as written, so
// an author who already chose their own alpha keeps it.
function _withAlpha(color, alpha) {
  if (typeof color !== "string") return color;
  const m = color.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (!m) return color;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
  const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, alpha)).toFixed(3)})`;
}
function _unit(v, dflt) { const n = Number(v); return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : dflt; }

// GlassPanelNode — the one reusable glass material: backdrop blur +
// transmission (how much of the scene shows through the tint), an edge
// light (border + top inset highlight), an inner shadow for thickness, a
// diagonal specular, and a contrast guard (a gentle scrim in the text's
// favour, so copy on the panel stays legible over a bright light_field
// as well as over a dark wash). Children mount on the host and so sit
// ABOVE the panel's own layers — the panel is the surface, its children
// are the content on it.
class GlassPanelNode extends Node {
  constructor(props = {}) {
    super(props);
    this.width        = props.width        || 640;
    this.height       = props.height       || 400;
    this.radius       = props.radius       !== undefined ? props.radius : 28;
    this.tint         = props.tint         || props.fill || "#1A2140";
    this.blur         = Number.isFinite(Number(props.blur)) ? Math.max(0, Math.min(40, Number(props.blur))) : 18;
    this.transmission = _unit(props.transmission, 0.55);
    this.borderLight  = _unit(props.border_light, 0.6);
    this.innerShadow  = _unit(props.inner_shadow, 0.4);
    this.specular     = _unit(props.specular, 0.5);
    this.lightAngle   = Number.isFinite(Number(props.light_angle)) ? Number(props.light_angle) : 135;
    const guard = props.contrast_guard;
    this.contrastGuard = guard === false || guard === "none" ? null : (guard === "light" ? "light" : "dark");
  }

  initDOM(box) {
    const w = this.width, h = this.height;
    box.style.transform = "";
    box.style.left   = `${-w * this.anchor[0]}px`;
    box.style.top    = `${-h * this.anchor[1]}px`;
    box.style.width  = `${w}px`;
    box.style.height = `${h}px`;
    const r = Math.min(this.radius, w / 2, h / 2);
    box.style.borderRadius = `${r}px`;
    box.style.overflow = "hidden";
    box.style.boxSizing = "border-box";

    const saturate = (1 + 0.5 * (1 - this.transmission)).toFixed(2);
    box.style.backdropFilter = `blur(${this.blur}px) saturate(${saturate})`;
    box.style.webkitBackdropFilter = `blur(${this.blur}px) saturate(${saturate})`;
    box.style.background = _withAlpha(this.tint, (1 - this.transmission) * 0.85);
    box.style.border = `1px solid rgba(255,255,255,${(0.08 + 0.32 * this.borderLight).toFixed(3)})`;
    box.style.boxShadow = [
      `inset 0 1px 0 rgba(255,255,255,${(0.10 + 0.45 * this.borderLight).toFixed(3)})`,
      `inset 0 -1px 0 rgba(0,0,0,${(0.35 * this.innerShadow).toFixed(3)})`,
      `inset 0 0 ${Math.round(28 * this.innerShadow)}px rgba(0,0,0,${(0.28 * this.innerShadow).toFixed(3)})`,
      `0 24px 48px rgba(0,0,0,0.35)`,
    ].join(", ");

    if (this.contrastGuard) {
      const scrim = document.createElement("div");
      const dark = this.contrastGuard === "dark";
      scrim.style.cssText = `position:absolute;inset:0;pointer-events:none;border-radius:inherit;` +
        (dark
          ? "background:linear-gradient(180deg, rgba(6,9,20,0.14) 0%, rgba(6,9,20,0.30) 100%);"
          : "background:linear-gradient(180deg, rgba(255,255,255,0.22) 0%, rgba(255,255,255,0.34) 100%);");
      box.appendChild(scrim);
    }
    if (this.specular > 0) {
      const spec = document.createElement("div");
      spec.style.cssText = `position:absolute;inset:0;pointer-events:none;border-radius:inherit;` +
        `background:linear-gradient(${this.lightAngle}deg, rgba(255,255,255,${(0.34 * this.specular).toFixed(3)}) 0%, ` +
        `rgba(255,255,255,0) 36%, rgba(255,255,255,0) 68%, rgba(255,255,255,${(0.09 * this.specular).toFixed(3)}) 100%);`;
      box.appendChild(spec);
    }
  }
}

// LightFieldNode — a soft, drifting light behind the subject: two radial
// lobes, screen-blended, blurred, each drifting on its own seeded phase so
// the field never reads as one mechanical pulse. Meant as the background
// layer of a shot in place of a flat wash.
class LightFieldNode extends Node {
  constructor(props = {}) {
    super(props);
    this.blendMode = props.blend_mode || "screen";
    this.width     = props.width  || 900;
    this.height    = props.height || 900;
    this.color     = props.color  || "rgba(120,160,255,0.55)";
    this.color2    = props.color2 || this.color;
    this.intensity = _unit(props.intensity, 0.6);
    this.spread    = Number.isFinite(Number(props.spread)) ? Math.max(0.2, Math.min(2.0, Number(props.spread))) : 1.0;
    this.drift     = Number.isFinite(Number(props.drift)) ? Math.max(0, Math.min(200, Number(props.drift))) : 40;
    this._lobes = [];
  }

  initDOM(box) {
    const w = this.width, h = this.height;
    box.style.transform = "";
    box.style.left   = `${-w * this.anchor[0]}px`;
    box.style.top    = `${-h * this.anchor[1]}px`;
    box.style.width  = `${w}px`;
    box.style.height = `${h}px`;
    box.style.overflow = "visible";
    box.style.pointerEvents = "none";
    // No CSS blur() on the box. The lobes' own radial gradients already fade to
    // transparent, so the filter softened nothing a viewer can see (mean pixel
    // difference 0.25 of 255 on a real film) while costing about a third of every
    // frame's render time on the software compositor (585 -> 385 ms, 12 Nov 2026).
    const stop = Math.round(58 * this.spread);
    const lobe = (color, size, left, top, opacity) => {
      const el = document.createElement("div");
      el.style.cssText = `position:absolute;width:${size}px;height:${size}px;left:${left}px;top:${top}px;` +
        `border-radius:50%;opacity:${opacity.toFixed(3)};` +
        `background:radial-gradient(circle at 50% 50%, ${color} 0%, rgba(0,0,0,0) ${stop}%);`;
      box.appendChild(el);
      this._lobes.push(el);
      return el;
    };
    lobe(this.color, Math.max(w, h), (w - Math.max(w, h)) / 2, (h - Math.max(w, h)) / 2, this.intensity);
    const small = Math.max(w, h) * 0.62;
    lobe(this.color2, small, w * 0.55 - small / 2, h * 0.38 - small / 2, this.intensity * 0.8);
  }

  registerContentAnimation(masterTl) {
    if (!this.drift || !this._lobes.length) return;
    const seed = _hashSeed(this.id);
    const at = this.animation && this.animation.enter ? (this.animation.enter.time || 0) : 0;
    const d = this.drift;
    this._lobes.forEach((el, i) => {
      const period = 5.5 + ((seed >> (i * 3)) % 7) * 0.35;
      const phase = ((seed >> (i * 5)) % 1000) / 1000 * period;
      const sx = i === 0 ? 1 : -1;
      masterTl.fromTo(el, { x: -d * sx, y: -d * 0.45 }, {
        x: d * sx, y: d * 0.45, duration: period, ease: "sine.inOut", repeat: -1, yoyo: true,
      }, Math.max(0, at - phase));
    });
  }
}

// DepthLayerNode — a group that moves with the camera by its depth, the
// hierarchy that gives a shot depth without repeating cards. depth 1 is
// the subject plane (identical to a plain group); < 1 sits further back
// (drifts and zooms less than the camera), > 1 nearer (more). The runtime
// calls applyParallax() on every seek, after the camera is evaluated.
class DepthLayerNode extends Node {
  constructor(props = {}) {
    super(props);
    const d = Number(props.depth);
    this.depth = Number.isFinite(d) ? Math.max(0.2, Math.min(2.5, d)) : 1.0;
    this._parallax = null;
  }

  mount(parentEl) {
    super.mount(parentEl);
    const wrap = document.createElement("div");
    wrap.style.position = "absolute";
    wrap.style.left = "0";
    wrap.style.top = "0";
    wrap.style.transformOrigin = "0 0";
    const sorted = [...this.children].sort((a, b) => a.zIndex - b.zIndex);
    for (const child of sorted) if (child._host) wrap.appendChild(child._host);
    this._host.appendChild(wrap);
    this._parallax = wrap;
  }

  // World point p at depth d appears where the camera at depth d (its pan
  // and zoom scaled by d) would put it. The stage already applies the full
  // camera, so this pre-transforms p so the stage lands it there.
  applyParallax(camera, w, h) {
    if (!this._parallax) return;
    const d = this.depth;
    if (Math.abs(d - 1) < 1e-6) { this._parallax.style.transform = ""; return; }
    const cx = w / 2, cy = h / 2;
    const zoom = Math.max(0.05, camera.zoom || 1);
    const zoomD = 1 + (zoom - 1) * d;
    const k = zoomD / zoom;
    const camDX = cx + (camera.x - cx) * d;
    const camDY = cy + (camera.y - cy) * d;
    // conjugate by the layer's own resting translation (usually 0,0)
    const px = this.position[0], py = this.position[1];
    const tx = camera.x - k * camDX + (k - 1) * px;
    const ty = camera.y - k * camDY + (k - 1) * py;
    this._parallax.style.transform = `matrix(${k.toFixed(5)},0,0,${k.toFixed(5)},${tx.toFixed(2)},${ty.toFixed(2)})`;
  }
}

// ── Reference-film primitives (the inspo benchmark) ──────────────────────────
// A small library of SVG paths (24x24 boxes) for IconNode / OrbNode.
const ICON_PATHS = {
  chat: "M4 4h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
  phone: "M6.6 2.5a1.6 1.6 0 0 1 1.6.2l2.4 2.1c.6.5.7 1.4.2 2l-1.5 1.8a13 13 0 0 0 6.1 6.1l1.8-1.5c.6-.5 1.5-.4 2 .2l2.1 2.4c.4.5.5 1.2.2 1.7l-1 1.7a2.6 2.6 0 0 1-2.6 1.3C11.1 19.9 4.1 12.9 3.5 6.1A2.6 2.6 0 0 1 4.8 3.5z",
  mail: "M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm1.5 2.2v.9l7.5 5 7.5-5v-.9L12 12.1z",
  whatsapp: "M12 2.5a9.5 9.5 0 0 0-8.2 14.3L2.5 21.5l4.9-1.3A9.5 9.5 0 1 0 12 2.5zm0 2a7.5 7.5 0 1 1-3.9 13.9l-.4-.2-2.4.6.7-2.3-.3-.4A7.5 7.5 0 0 1 12 4.5zm-3 3.6c-.3 0-.7.1-1 .5-.3.4-1 1-1 2.4s1 2.8 1.2 3c.2.2 2 3.2 5 4.4 2.5 1 3 .8 3.5.7.5-.1 1.7-.7 2-1.4.2-.7.2-1.3.1-1.4l-.5-.3-2-1c-.3-.1-.5-.1-.7.1l-.9 1.1c-.2.2-.3.2-.6.1a6.4 6.4 0 0 1-3.2-2.8c-.2-.4.2-.5.5-1.1l.3-.5-.1-.5-.9-2.1c-.2-.6-.5-.5-.7-.5z",
  book: "M4 3h7a3 3 0 0 1 3 3v15a2 2 0 0 0-2-2H4zM20 3h-7a3 3 0 0 0-3 3v15a2 2 0 0 1 2-2h8z",
  dial: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 3.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zM8 10.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zm8 0a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zM12 14.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3z",
  spark: "M12 2l2.2 6.3L20.5 10l-6.3 2.2L12 18.5l-2.2-6.3L3.5 10l6.3-1.7z",
  check: "M4 12.5l5 5L20 6.5l-2-2-9 9-3-3z",
};
function _iconSvg(name, size, color) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size); svg.setAttribute("height", size);
  svg.style.display = "block";
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON_PATHS[name] || ICON_PATHS.spark);
  path.setAttribute("fill", color);
  svg.appendChild(path);
  return svg;
}

// IconNode — a glyph from the library, optionally on a dark glass tile
// (the reference's chat / phone / mail tiles).
class IconNode extends Node {
  constructor(props = {}) {
    super(props);
    this.name  = props.name  || "spark";
    this.size  = props.size  || 96;
    this.color = props.color || "#FFFFFF";
    this.tile  = props.tile !== undefined ? props.tile : true;
    this.tileColor = props.tile_color || "rgba(20,22,30,0.82)";
    this.radius = props.radius !== undefined ? props.radius : Math.round(this.size * 0.26);
    this.glyph = props.glyph || 0.46; // glyph size as a fraction of the tile
  }
  initDOM(box) {
    const s = this.size;
    box.style.transform = "";
    box.style.left = `${-s * this.anchor[0]}px`; box.style.top = `${-s * this.anchor[1]}px`;
    box.style.width = `${s}px`; box.style.height = `${s}px`;
    box.style.display = "flex"; box.style.alignItems = "center"; box.style.justifyContent = "center";
    if (this.tile) {
      box.style.borderRadius = `${this.radius}px`;
      box.style.background = this.tileColor;
      box.style.border = "1px solid rgba(255,255,255,0.14)";
      box.style.boxShadow = "inset 0 1px 0 rgba(255,255,255,0.10), 0 18px 40px rgba(0,0,0,0.45)";
      box.style.backdropFilter = "blur(14px)"; box.style.webkitBackdropFilter = "blur(14px)";
    }
    box.appendChild(_iconSvg(this.name, Math.round(s * this.glyph), this.color));
  }
}

// OrbNode — a sphere: a lit core gradient, a rim, a specular, an outer
// glow, and an optional large faint ring around it (the reference's
// dark hub sphere and its glowing green/blue orb).
class OrbNode extends Node {
  constructor(props = {}) {
    super(props);
    this.radius = props.radius || 120;
    this.core   = props.core   || "#0c1018";
    this.tint   = props.tint   || "rgba(255,255,255,0.10)";
    this.rim    = props.rim    || "rgba(255,255,255,0.22)";
    this.glowColor = props.glow_color || null;
    this.glowBlur  = props.glow_blur  || 0;
    this.highlight = props.highlight !== undefined ? props.highlight : 0.55;
    this.ringRadius = props.ring_radius || 0;
    this.ringColor  = props.ring_color  || "rgba(255,255,255,0.12)";
    this.icon = props.icon || null;
    this.iconColor = props.icon_color || "#FFFFFF";
  }
  initDOM(box) {
    const d = this.radius * 2;
    box.style.transform = "";
    box.style.left = `${-d * this.anchor[0]}px`; box.style.top = `${-d * this.anchor[1]}px`;
    box.style.width = `${d}px`; box.style.height = `${d}px`;
    box.style.overflow = "visible";
    if (this.ringRadius > 0) {
      const ring = document.createElement("div");
      const rd = this.ringRadius * 2;
      ring.style.cssText = `position:absolute;left:${d / 2 - this.ringRadius}px;top:${d / 2 - this.ringRadius}px;width:${rd}px;height:${rd}px;border-radius:50%;border:1px solid ${this.ringColor};pointer-events:none;`;
      box.appendChild(ring);
    }
    const sphere = document.createElement("div");
    sphere.style.cssText = `position:absolute;inset:0;border-radius:50%;` +
      `background:radial-gradient(circle at 32% 28%, ${this.tint} 0%, rgba(255,255,255,0) 42%), ` +
      `radial-gradient(circle at 50% 55%, ${this.core} 55%, ${_withAlpha(this.core, 0.85)} 100%);` +
      `box-shadow: inset 0 0 ${Math.round(d * 0.12)}px rgba(0,0,0,0.55), inset 0 -${Math.round(d * 0.04)}px ${Math.round(d * 0.1)}px rgba(0,0,0,0.35), inset 0 1px 0 ${this.rim}` +
      (this.glowColor && this.glowBlur > 0 ? `, 0 0 ${this.glowBlur}px ${this.glowColor}, 0 0 ${Math.round(this.glowBlur * 2.2)}px ${_withAlpha(this.glowColor, 0.35)}` : "") + ";";
    box.appendChild(sphere);
    if (this.highlight > 0) {
      const spec = document.createElement("div");
      spec.style.cssText = `position:absolute;left:${d * 0.2}px;top:${d * 0.12}px;width:${d * 0.34}px;height:${d * 0.2}px;border-radius:50%;` +
        `background:radial-gradient(ellipse at 50% 50%, rgba(255,255,255,${(0.5 * this.highlight).toFixed(3)}) 0%, rgba(255,255,255,0) 70%);pointer-events:none;`;
      box.appendChild(spec);
    }
    if (this.icon) {
      const holder = document.createElement("div");
      holder.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;";
      holder.appendChild(_iconSvg(this.icon, Math.round(d * 0.22), this.iconColor));
      box.appendChild(holder);
    }
  }
}

// SplineTreeNode — thin curved lines from one hub to many leaves, drawn
// on with a stagger (the reference's icon-to-hub tree and its knowledge
// graph). Coordinates are relative to the node's own position.
class SplineTreeNode extends Node {
  constructor(props = {}) {
    super(props);
    this.hub = props.hub || [0, 0];
    this.leaves = Array.isArray(props.leaves) ? props.leaves : [];
    this.color = props.color || "rgba(255,255,255,0.55)";
    this.strokeWidth = props.stroke_width || 1.5;
    this.glowBlur = props.glow_blur || 0;
    this.bend = props.bend !== undefined ? props.bend : 0.5;
    this.drawStart = props.draw_start || 0;
    this.drawDuration = props.draw_duration || 0.9;
    this.stagger = props.stagger !== undefined ? props.stagger : 0.08;
    this.dots = props.dots !== undefined ? props.dots : true;
    this._paths = [];
  }
  initDOM(box) {
    const pts = [this.hub].concat(this.leaves);
    const minX = Math.min(...pts.map(p => p[0])) - 20, minY = Math.min(...pts.map(p => p[1])) - 20;
    const maxX = Math.max(...pts.map(p => p[0])) + 20, maxY = Math.max(...pts.map(p => p[1])) + 20;
    const w = maxX - minX, h = maxY - minY;
    box.style.transform = ""; box.style.left = `${minX}px`; box.style.top = `${minY}px`;
    box.style.width = `${w}px`; box.style.height = `${h}px`;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", w); svg.setAttribute("height", h); svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.style.overflow = "visible";
    const [hx, hy] = [this.hub[0] - minX, this.hub[1] - minY];
    for (const leaf of this.leaves) {
      const lx = leaf[0] - minX, ly = leaf[1] - minY;
      const dy = (ly - hy) * this.bend;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M ${hx} ${hy} C ${hx} ${hy + dy}, ${lx} ${ly - dy}, ${lx} ${ly}`);
      path.setAttribute("fill", "none"); path.setAttribute("stroke", this.color);
      path.setAttribute("stroke-width", String(this.strokeWidth)); path.setAttribute("stroke-linecap", "round");
      if (this.glowBlur > 0) path.style.filter = `drop-shadow(0 0 ${this.glowBlur}px ${this.color})`;
      svg.appendChild(path);
      const len = path.getTotalLength();
      path.style.strokeDasharray = String(len); path.style.strokeDashoffset = String(len);
      let dot = null;
      if (this.dots) {
        dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("cx", lx); dot.setAttribute("cy", ly); dot.setAttribute("r", String(this.strokeWidth * 1.6));
        dot.setAttribute("fill", this.color); dot.style.opacity = "0";
        svg.appendChild(dot);
      }
      this._paths.push({ path, len, dot });
    }
    box.appendChild(svg);
    this._strokeTarget = this._paths.length ? this._paths[0].path : null;
  }
  registerContentAnimation(masterTl) {
    const dur = Math.max(0.05, this.drawDuration);
    this._paths.forEach((p, i) => {
      const at = this.drawStart + i * this.stagger;
      proxyTween(masterTl, v => { p.path.style.strokeDashoffset = String(v); }, p.len, 0, dur, "power2.out", at);
      if (p.dot) masterTl.fromTo(p.dot, { opacity: 0 }, { opacity: 1, duration: 0.25 }, at + dur * 0.8);
    });
  }
}

// ParticleFieldNode — the reference's signal: many small coloured tiles
// that hold one layout and travel to the next at each phase. Every tile's
// place in every layout is a pure function of (seed, index), so the same
// field declared in two scenes with the same layout sits on the same
// pixels — the continuity the morph needs.
const _FIELD_PALETTE = ["#5b7cff", "#ff6bb5", "#b58cff", "#e8f0ff", "#2dd4bf", "#8fb3ff"];
function _rand(seed, i, salt) {
  // XOR on a JS number yields a SIGNED 32-bit int, so every step is
  // forced back to unsigned — a negative here once threw half of a
  // particle field off the top-left of the frame.
  let h = (_hashSeed(seed) ^ Math.imul(i + 1, 2654435761) ^ Math.imul(salt + 7, 40503)) >>> 0;
  h = (h ^ (h >>> 13)) >>> 0; h = Math.imul(h, 0x5bd1e995) >>> 0; h = (h ^ (h >>> 15)) >>> 0;
  return (h % 100000) / 100000;
}
function _layoutPoint(layout, i, count, seed) {
  if (typeof layout === "string") layout = { kind: layout };
  if (!layout || typeof layout !== "object") layout = {};
  const kind = layout.kind || "scatter";
  const r = k => _rand(seed, i, k);
  if (kind === "band") {
    const rows = layout.rows || 6, cell = layout.cell || 26, x0 = layout.x0 !== undefined ? layout.x0 : 40;
    const x1 = layout.x1 !== undefined ? layout.x1 : 1040, y = layout.y !== undefined ? layout.y : 900;
    const cols = Math.max(1, Math.floor((x1 - x0) / cell));
    const idx = i % (rows * cols);
    return [x0 + (idx % cols) * cell + (r(1) - 0.5) * 3, y + Math.floor(idx / cols) * cell - (rows * cell) / 2 + (r(2) - 0.5) * 3];
  }
  if (kind === "column") {
    const x = layout.x !== undefined ? layout.x : 540, spread = layout.spread || 90;
    const y0 = layout.y0 !== undefined ? layout.y0 : -200, y1 = layout.y1 !== undefined ? layout.y1 : 2100;
    return [x + (r(3) - 0.5) * 2 * spread * (0.4 + 0.6 * r(6)), y0 + (y1 - y0) * ((i / count + r(4) * 0.05) % 1)];
  }
  if (kind === "ring") {
    const c = layout.center || [540, 960], rad = layout.radius || 200, t = (i / count) * Math.PI * 2 + r(5) * 0.4;
    const rr = rad * (0.75 + 0.5 * r(6));
    return [c[0] + Math.cos(t) * rr, c[1] + Math.sin(t) * rr];
  }
  if (kind === "points") {
    const pts = layout.points || [[540, 960]], p = pts[i % pts.length], jitter = layout.jitter || 40;
    return [p[0] + (r(7) - 0.5) * 2 * jitter, p[1] + (r(8) - 0.5) * 2 * jitter];
  }
  if (kind === "hidden") {
    return [540, 960];
  }
  // scatter: small clusters of adjacent cells (the reference's tiles sit
  // in twos and threes, never evenly sprinkled), `cluster` tiles per group
  const box = layout.box || [40, 200, 1040, 1700];
  const per = Math.max(1, layout.cluster || 3), cell = layout.cell || 26;
  const g = Math.floor(i / per), k = i % per;
  const gx = box[0] + (box[2] - box[0]) * _rand(seed, g, 9);
  const gy = box[1] + (box[3] - box[1]) * _rand(seed, g, 10);
  return [gx + (k % 2) * cell + (Math.floor(k / 2) ? cell * (_rand(seed, g, 14) > 0.5 ? 1 : -1) : 0),
          gy + Math.floor(k / 2) * cell * (k >= 2 && _rand(seed, g, 15) > 0.5 ? 1 : 0)];
}
class ParticleFieldNode extends Node {
  constructor(props = {}) {
    super(props);
    this.count = Math.max(1, Math.min(600, props.count || 120));
    this.size = props.size || 14;
    this.palette = Array.isArray(props.palette) && props.palette.length ? props.palette : _FIELD_PALETTE;
    this.seed = props.seed !== undefined ? props.seed : this.id;
    // schema.py normalises these too; the runtime stays safe on its own so a
    // saved spec from before that normalisation still plays.
    const raw = Array.isArray(props.phases) && props.phases.length ? props.phases : [{ at: 0, layout: { kind: "scatter" } }];
    this.phases = raw.map((ph, k) => {
      if (typeof ph === "string") return { at: k * 1.2, layout: { kind: ph } };
      if (!ph || typeof ph !== "object") return { at: k * 1.2, layout: { kind: "scatter" } };
      const out = Object.assign({}, ph);
      if (typeof out.layout === "string") out.layout = { kind: out.layout };
      if (!out.layout || typeof out.layout !== "object") out.layout = { kind: out.kind || "scatter" };
      return out;
    });
    this.flicker = props.flicker !== undefined ? props.flicker : 0.35;
    this.blend = props.blend_mode || "normal";
    this._tiles = [];
  }
  initDOM(box) {
    box.style.transform = ""; box.style.left = "0"; box.style.top = "0";
    box.style.width = "0"; box.style.height = "0"; box.style.overflow = "visible";
    const first = this.phases[0].layout;
    const hidden = first && first.kind === "hidden";
    for (let i = 0; i < this.count; i++) {
      const el = document.createElement("i");
      const s = this.size * (0.88 + 0.24 * _rand(this.seed, i, 11));
      const depth = _rand(this.seed, i, 12);
      const colour = this.palette[Math.floor(_rand(this.seed, i, 13) * this.palette.length) % this.palette.length];
      const [x, y] = _layoutPoint(first, i, this.count, this.seed);
      // `alpha` is the tile's resting opacity whatever the first layout —
      // a field that starts hidden reveals to it, not to zero.
      const alpha = 0.55 + 0.45 * depth;
      el.style.cssText = `position:absolute;left:${-s / 2}px;top:${-s / 2}px;width:${s}px;height:${s}px;border-radius:2px;` +
        `background:${colour};opacity:${hidden ? 0 : alpha.toFixed(3)};mix-blend-mode:${this.blend};will-change:transform;`;
      box.appendChild(el);
      this._tiles.push({ el, x, y, depth, alpha, hiddenAtStart: hidden });
      gsap.set(el, { x, y });
    }
  }
  registerContentAnimation(masterTl) {
    const total = this.count;
    for (let k = 1; k < this.phases.length; k++) {
      const ph = this.phases[k], layout = ph.layout || {};
      const dur = Math.max(0.05, ph.duration || 1.0), ease = ph.easing || "power2.inOut";
      const hidden = layout.kind === "hidden";
      this._tiles.forEach((t, i) => {
        const [x, y] = _layoutPoint(layout, i, total, this.seed);
        const at = (ph.at || 0) + _rand(this.seed, i, 20 + k) * (ph.stagger || 0.35);
        masterTl.to(t.el, { x, y, duration: dur, ease }, at);
        const wasHidden = (this.phases[k - 1].layout || {}).kind === "hidden";
        if (hidden || wasHidden) masterTl.to(t.el, { opacity: hidden ? 0 : t.alpha, duration: dur * 0.6, ease: "power2.out" }, at);
      });
    }
    if (this.flicker > 0) {
      // The flicker starts only once the field is visible, or it would
      // reveal a hidden-at-start field on its own schedule.
      let visibleFrom = 0;
      for (let k = 0; k < this.phases.length; k++) {
        const lay = this.phases[k].layout || {};
        if (lay.kind !== "hidden") { visibleFrom = (this.phases[k].at || 0) + (k ? (this.phases[k].duration || 1.0) + (this.phases[k].stagger || 0.35) : 0); break; }
      }
      this._tiles.forEach((t, i) => {
        const period = 0.9 + _rand(this.seed, i, 30) * 1.6;
        masterTl.to(t.el, { opacity: Math.max(0.05, t.alpha * (1 - this.flicker)), duration: period, ease: "sine.inOut", repeat: -1, yoyo: true },
          visibleFrom + _rand(this.seed, i, 31) * period);
      });
    }
  }
}

// ── Interface primitives — what a product film is made of ────────────────────
// Added after the 11 Nov 2026 study against a polished product film
// (artifacts/reference-motion-so/plan.html): its story is a prompt box
// that types, labelled cursors that travel and click, status pills, and a
// line whose last two words are a different colour. None of that could be
// drawn from rects and text without the model hand-building every glyph.

// Types `spans` on one at a time, `rate` characters a second from `start`.
// Visibility, not opacity: a gradient run is painted with background-clip
// on its parent, and a child with its own opacity leaves that clip.
// `carets[i]` is the bar shown after character i; the last one keeps
// blinking. Everything is a timeline tween, so a seek is deterministic.
function _typeOn(masterTl, spans, carets, start, rate, blink) {
  const step = 1 / Math.max(1, rate);
  const n = spans.length;
  for (let i = 0; i < n; i++) {
    const t = start + i * step;
    masterTl.set(spans[i], { visibility: "visible" }, t);
    const c = carets[i];
    if (!c) continue;
    if (i < n - 1) {
      masterTl.set(c, { visibility: "visible" }, t);
      masterTl.set(c, { visibility: "hidden" }, start + (i + 1) * step);
    } else {
      masterTl.set(c, { visibility: "visible" }, t);
      if (blink) {
        masterTl.fromTo(c, { opacity: 1 }, { opacity: 0, duration: 0.5, ease: "steps(1)",
          repeat: -1, yoyo: true, immediateRender: false }, t);
      }
    }
  }
}

function _caretEl(size, colour) {
  const holder = document.createElement("span");
  holder.style.cssText = "display:inline-block;width:0;position:relative;visibility:hidden;";
  const bar = document.createElement("span");
  // The holder is a zero-height inline-block, so it sits ON the baseline; the
  // bar hangs from the baseline UP (bottom:, not top:) — from a little below
  // it, past the cap height. Anchoring it with top: put it under the text.
  bar.style.cssText = `position:absolute;left:${Math.round(size * 0.06)}px;bottom:${-Math.round(size * 0.18)}px;`
    + `width:${Math.max(2, Math.round(size * 0.07))}px;height:${Math.round(size * 0.95)}px;background:${colour};`;
  holder.appendChild(bar);
  return holder;
}

function _gradientCss(g) {
  const stops = g.stops.slice().sort((a, b) => a.pos - b.pos)
    .map(s => `${s.color} ${Math.round(s.pos * 100)}%`).join(", ");
  return `linear-gradient(90deg, ${stops})`;
}

// Cursor — a labelled pointer. `position` is where the pointer's TIP starts;
// `path` is a list of moves {at, to:[x,y], duration, easing, click}. A click
// draws a ring and presses the arrow. Times are scene-local like every other
// block (the resolver shifts them).
class CursorNode extends Node {
  constructor(props = {}) {
    super(props);
    this.label = props.label || "";
    this.color = props.color || "#C96442";
    this.size  = props.size  || 44;
    this.path  = Array.isArray(props.path) ? props.path : [];
  }
  initDOM(box) {
    const s = this.size;
    box.style.transform = "";
    box.style.left = "0px"; box.style.top = "0px"; box.style.width = "0px"; box.style.height = "0px";
    const ring = document.createElement("div");
    ring.style.cssText = `position:absolute;left:${-s * 0.6}px;top:${-s * 0.6}px;width:${s * 1.2}px;height:${s * 1.2}px;`
      + `border-radius:50%;border:${Math.max(2, Math.round(s * 0.06))}px solid ${this.color};opacity:0;`;
    box.appendChild(ring);
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", String(s)); svg.setAttribute("height", String(s));
    svg.style.cssText = "position:absolute;left:0;top:0;overflow:visible;filter:drop-shadow(0 2px 3px rgba(0,0,0,0.25));";
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", "M1 1 L1 19 L5.6 14.8 L8.8 22 L12.2 20.5 L9 13.4 L15.2 13.4 Z");
    p.setAttribute("fill", this.color); p.setAttribute("stroke", "#FFFFFF");
    p.setAttribute("stroke-width", "1.5"); p.setAttribute("stroke-linejoin", "round");
    svg.appendChild(p); box.appendChild(svg);
    if (this.label) {
      const tag = document.createElement("div");
      tag.textContent = this.label;
      tag.style.cssText = `position:absolute;left:${Math.round(s * 0.5)}px;top:${Math.round(s * 0.62)}px;`
        + `background:${this.color};color:#FFFFFF;font:600 ${Math.round(s * 0.34)}px/1 var(--motion-body-font, Inter, sans-serif);`
        + `padding:${Math.round(s * 0.11)}px ${Math.round(s * 0.3)}px;border-radius:999px;white-space:nowrap;`
        + "box-shadow:0 2px 6px rgba(0,0,0,0.18);";
      box.appendChild(tag);
    }
    this._ring = ring; this._arrow = svg;
  }
  registerContentAnimation(masterTl) {
    let x = this.position[0], y = this.position[1];
    for (const seg of this.path) {
      const to = Array.isArray(seg.to) && seg.to.length === 2 ? seg.to : [x, y];
      const dur = Math.max(0.05, seg.duration || 0.7), at = seg.at || 0;
      masterTl.fromTo(this._host, { x, y }, { x: to[0], y: to[1], duration: dur,
        ease: seg.easing || "power3.inOut", immediateRender: false }, at);
      x = to[0]; y = to[1];
      if (seg.click) {
        const t = at + dur;
        masterTl.fromTo(this._ring, { opacity: 0.7, scale: 0.3 }, { opacity: 0, scale: 1.6,
          duration: 0.42, ease: "power2.out", immediateRender: false }, t);
        masterTl.fromTo(this._arrow, { scale: 1 }, { scale: 0.82, duration: 0.07, ease: "power1.inOut",
          yoyo: true, repeat: 1, transformOrigin: "0 0", immediateRender: false }, t);
      }
    }
  }
}

// UIPill — a status chip: an optional coloured dot or a green check, then a label.
class UIPillNode extends Node {
  constructor(props = {}) {
    super(props);
    this.label    = String(props.label || "");
    this.dot      = props.dot || null;
    this.check    = !!props.check;
    this.fontSize = props.font_size || 28;
    this.fill     = props.fill || "#FFFFFF";
    this.ink      = props.ink  || "#111111";
    this.border   = props.border || "rgba(0,0,0,0.07)";
  }
  initDOM(box) {
    const fs = this.fontSize;
    box.style.transform = `translate(${-this.anchor[0] * 100}%, ${-this.anchor[1] * 100}%)`;
    box.style.display = "inline-flex"; box.style.alignItems = "center";
    box.style.gap = `${Math.round(fs * 0.45)}px`;
    box.style.padding = `${Math.round(fs * 0.5)}px ${Math.round(fs * 0.95)}px`;
    box.style.borderRadius = "999px"; box.style.background = this.fill;
    box.style.border = `1px solid ${this.border}`;
    box.style.boxShadow = "0 1px 2px rgba(0,0,0,0.06), 0 10px 28px rgba(0,0,0,0.09)";
    box.style.whiteSpace = "nowrap"; box.style.color = this.ink;
    box.style.font = `500 ${fs}px/1 var(--motion-body-font, Inter, -apple-system, sans-serif)`;
    if (this.check) {
      const NS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", String(Math.round(fs * 0.95)));
      svg.setAttribute("height", String(Math.round(fs * 0.95)));
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", "M4 12.5 L9.5 18 L20 6.5"); p.setAttribute("fill", "none");
      p.setAttribute("stroke", "#16A34A"); p.setAttribute("stroke-width", "3.2");
      p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
      svg.appendChild(p); box.appendChild(svg);
    } else if (this.dot) {
      const d = document.createElement("span");
      d.style.cssText = `display:inline-block;width:${Math.round(fs * 0.4)}px;height:${Math.round(fs * 0.4)}px;`
        + `border-radius:50%;background:${this.dot};`;
      box.appendChild(d);
    }
    const label = document.createElement("span"); label.textContent = this.label; box.appendChild(label);
  }
}

// UIInput — a prompt box: placeholder, text that types itself, a send button
// that goes from grey to ink when the message is sent.
class UIInputNode extends Node {
  constructor(props = {}) {
    super(props);
    this.width  = props.width  || 900;
    this.height = props.height || 220;
    this.text = String(props.text || "");
    this.placeholder = String(props.placeholder || "");
    this.fontSize = props.font_size || Math.round(this.height * 0.17);
    this.fill = props.fill || "#FFFFFF";
    this.ink = props.ink || "#111111";
    this.revealStart = props.reveal_start || 0;
    this.typeRate = props.type_rate || 14;
    this.sendAt = props.send_at !== undefined ? props.send_at : null;
  }
  initDOM(box) {
    const w = this.width, h = this.height, fs = this.fontSize;
    box.style.transform = "";
    box.style.left = `${-w * this.anchor[0]}px`; box.style.top = `${-h * this.anchor[1]}px`;
    box.style.width = `${w}px`; box.style.height = `${h}px`;
    box.style.background = this.fill; box.style.borderRadius = `${Math.round(h * 0.13)}px`;
    box.style.border = "1px solid rgba(0,0,0,0.07)";
    box.style.boxShadow = "0 1px 2px rgba(0,0,0,0.05), 0 18px 48px rgba(0,0,0,0.08)";
    box.style.font = `400 ${fs}px/1.3 var(--motion-body-font, Inter, -apple-system, sans-serif)`;
    const pad = Math.round(h * 0.14);
    const ph = document.createElement("div");
    ph.textContent = this.placeholder;
    ph.style.cssText = `position:absolute;left:${pad}px;top:${pad}px;color:#9CA3AF;white-space:pre;`;
    box.appendChild(ph);
    const typed = document.createElement("div");
    typed.style.cssText = `position:absolute;left:${pad}px;top:${pad}px;right:${pad}px;color:${this.ink};white-space:pre-wrap;`;
    this._spans = []; this._carets = [];
    for (const ch of Array.from(this.text)) {
      const s = document.createElement("span"); s.textContent = ch; s.style.visibility = "hidden"; s.style.whiteSpace = "pre";
      typed.appendChild(s); this._spans.push(s);
      const c = _caretEl(fs, this.ink); typed.appendChild(c); this._carets.push(c);
    }
    box.appendChild(typed);
    const d = Math.round(h * 0.3);
    const btn = document.createElement("div");
    btn.style.cssText = `position:absolute;right:${pad}px;bottom:${pad}px;width:${d}px;height:${d}px;border-radius:50%;`
      + "background:#A1A1AA;display:flex;align-items:center;justify-content:center;";
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", String(Math.round(d * 0.5)));
    svg.setAttribute("height", String(Math.round(d * 0.5)));
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", "M12 19 V5 M5.5 11.5 L12 5 L18.5 11.5"); p.setAttribute("fill", "none");
    p.setAttribute("stroke", "#FFFFFF"); p.setAttribute("stroke-width", "2.6");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    svg.appendChild(p); btn.appendChild(svg); box.appendChild(btn);
    this._ph = ph; this._btn = btn;
  }
  registerContentAnimation(masterTl) {
    if (this._spans.length) {
      masterTl.set(this._ph, { visibility: "hidden" }, this.revealStart);
      _typeOn(masterTl, this._spans, this._carets, this.revealStart, this.typeRate, true);
    }
    if (this.sendAt !== null) {
      masterTl.fromTo(this._btn, { backgroundColor: "#A1A1AA" }, { backgroundColor: this.ink,
        duration: 0.18, ease: "power1.out", immediateRender: false }, this.sendAt);
    }
  }
}

// UIChecklist — rows of work in progress: a coloured dot, a label, a quieter
// sub-line and a thin underline that fills while the row "works". Each item
// is {label, sub?, color?, at, fill?} (at: scene-local seconds it appears).
class UIChecklistNode extends Node {
  constructor(props = {}) {
    super(props);
    this.items = Array.isArray(props.items) ? props.items : [];
    this.width = props.width || 760;
    this.fontSize = props.font_size || 34;
    this.ink = props.ink || "#111111";
    this.gap = props.row_gap !== undefined ? props.row_gap : Math.round(this.fontSize * 0.9);
  }
  initDOM(box) {
    const fs = this.fontSize;
    box.style.transform = `translate(${-this.anchor[0] * 100}%, ${-this.anchor[1] * 100}%)`;
    box.style.width = `${this.width}px`;
    box.style.display = "flex"; box.style.flexDirection = "column"; box.style.gap = `${this.gap}px`;
    box.style.font = `600 ${fs}px/1.2 var(--motion-body-font, Inter, -apple-system, sans-serif)`;
    box.style.color = this.ink;
    this._rows = [];
    const colours = ["#E0533C", "#3B82F6", "#D946A8", "#10B981", "#F59E0B"];
    this.items.forEach((it, i) => {
      const colour = it.color || colours[i % colours.length];
      const row = document.createElement("div");
      row.style.cssText = "position:relative;display:flex;align-items:flex-start;gap:" + Math.round(fs * 0.5) + "px;opacity:0;padding-bottom:" + Math.round(fs * 0.3) + "px;";
      const dot = document.createElement("span");
      dot.style.cssText = `flex:none;margin-top:${Math.round(fs * 0.32)}px;width:${Math.round(fs * 0.3)}px;height:${Math.round(fs * 0.3)}px;border-radius:50%;background:${colour};`;
      const col = document.createElement("div");
      const label = document.createElement("div"); label.textContent = String(it.label || "");
      col.appendChild(label);
      if (it.sub) {
        const sub = document.createElement("div"); sub.textContent = String(it.sub);
        sub.style.cssText = `font-weight:400;font-size:${Math.round(fs * 0.62)}px;color:#6B7280;margin-top:${Math.round(fs * 0.12)}px;`;
        col.appendChild(sub);
      }
      const line = document.createElement("div");
      line.style.cssText = `position:absolute;left:${Math.round(fs * 0.8)}px;right:0;bottom:0;height:${Math.max(2, Math.round(fs * 0.07))}px;background:${colour};transform-origin:0 50%;transform:scaleX(0);opacity:0.85;`;
      row.appendChild(dot); row.appendChild(col); row.appendChild(line); box.appendChild(row);
      this._rows.push({ row, line, at: it.at || 0, fill: it.fill || 1.6 });
    });
  }
  registerContentAnimation(masterTl) {
    for (const r of this._rows) {
      masterTl.fromTo(r.row, { opacity: 0, y: 14 }, { opacity: 1, y: 0, duration: 0.4, ease: "power2.out", immediateRender: false }, r.at);
      masterTl.fromTo(r.line, { scaleX: 0 }, { scaleX: 1, duration: r.fill, ease: "power1.inOut", immediateRender: false }, r.at + 0.2);
    }
  }
}

// UIWindow — an application window: title bar, a content area for its
// children (positions are relative to the window's centre, like a panel's),
// and a scrubber whose thumb travels from 0 to `progress` between `play_at`
// and `play_at + play_for`.
class UIWindowNode extends Node {
  constructor(props = {}) {
    super(props);
    this.width = props.width || 900; this.height = props.height || 620;
    this.title = String(props.title || "");
    this.timecode = String(props.timecode || "");
    this.fill = props.fill || "#FFFFFF";
    this.contentFill = props.content_fill || "#F3EFE6";
    this.scrubber = props.scrubber !== false;
    this.progress = props.progress !== undefined ? props.progress : 1;
    this.playAt = props.play_at || 0; this.playFor = props.play_for || 2;
  }
  initDOM(box) {
    const w = this.width, h = this.height, bar = Math.round(h * 0.075), foot = this.scrubber ? Math.round(h * 0.11) : 0;
    box.style.transform = "";
    box.style.left = `${-w * this.anchor[0]}px`; box.style.top = `${-h * this.anchor[1]}px`;
    box.style.width = `${w}px`; box.style.height = `${h}px`;
    box.style.background = this.fill; box.style.borderRadius = `${Math.round(h * 0.03)}px`;
    box.style.border = "1px solid rgba(0,0,0,0.07)";
    box.style.boxShadow = "0 2px 4px rgba(0,0,0,0.05), 0 28px 70px rgba(0,0,0,0.12)";
    box.style.overflow = "hidden";
    box.style.font = `500 ${Math.round(bar * 0.42)}px/1 var(--motion-body-font, Inter, -apple-system, sans-serif)`;
    const head = document.createElement("div");
    head.style.cssText = `position:absolute;left:0;top:0;right:0;height:${bar}px;display:flex;align-items:center;gap:${Math.round(bar * 0.2)}px;padding:0 ${Math.round(bar * 0.4)}px;color:#9CA3AF;`;
    for (let i = 0; i < 3; i++) {
      const d = document.createElement("span");
      d.style.cssText = `width:${Math.round(bar * 0.2)}px;height:${Math.round(bar * 0.2)}px;border-radius:50%;background:#D4D4D8;`;
      head.appendChild(d);
    }
    const t = document.createElement("span"); t.textContent = this.title; t.style.marginLeft = `${Math.round(bar * 0.2)}px`; head.appendChild(t);
    const content = document.createElement("div");
    content.style.cssText = `position:absolute;left:${Math.round(w * 0.02)}px;right:${Math.round(w * 0.02)}px;top:${bar}px;bottom:${foot + Math.round(h * 0.02)}px;background:${this.contentFill};border-radius:${Math.round(h * 0.02)}px;`;
    box.appendChild(head); box.appendChild(content);
    if (this.scrubber) {
      const trackW = w - 2 * Math.round(w * 0.04) - (this.timecode ? Math.round(bar * 1.6) : 0);
      const track = document.createElement("div");
      track.style.cssText = `position:absolute;left:${Math.round(w * 0.04)}px;bottom:${Math.round(foot * 0.45)}px;width:${trackW}px;height:2px;background:#E4E4E7;`;
      const thumb = document.createElement("div");
      const tw = Math.max(2, Math.round(bar * 0.06)), th = Math.round(foot * 0.5);
      thumb.style.cssText = `position:absolute;left:0;top:${-Math.round(th / 2) + 1}px;width:${tw}px;height:${th}px;background:#111111;`;
      track.appendChild(thumb); box.appendChild(track);
      if (this.timecode) {
        const tc = document.createElement("div"); tc.textContent = this.timecode;
        tc.style.cssText = `position:absolute;right:${Math.round(w * 0.04)}px;bottom:${Math.round(foot * 0.45) - 6}px;color:#9CA3AF;`;
        box.appendChild(tc);
      }
      this._thumb = thumb; this._trackW = trackW;
    }
  }
  registerContentAnimation(masterTl) {
    if (this._thumb) {
      masterTl.fromTo(this._thumb, { x: 0 }, { x: this._trackW * this.progress, duration: Math.max(0.05, this.playFor),
        ease: "none", immediateRender: false }, this.playAt);
    }
  }
}

// UISwatches — a row of palette chips, each a small card of coloured dots,
// popping in one after another from `at`.
class UISwatchesNode extends Node {
  constructor(props = {}) {
    super(props);
    this.groups = Array.isArray(props.groups) ? props.groups : [];
    this.dot = props.dot_size || 30; this.at = props.at || 0; this.stagger = props.stagger !== undefined ? props.stagger : 0.25;
  }
  initDOM(box) {
    const d = this.dot;
    box.style.transform = `translate(${-this.anchor[0] * 100}%, ${-this.anchor[1] * 100}%)`;
    box.style.display = "flex"; box.style.gap = `${Math.round(d * 0.7)}px`; box.style.whiteSpace = "nowrap";
    this._chips = [];
    for (const colours of this.groups) {
      const chip = document.createElement("div");
      chip.style.cssText = `display:flex;gap:${Math.round(d * 0.3)}px;padding:${Math.round(d * 0.45)}px ${Math.round(d * 0.6)}px;`
        + "background:#FFFFFF;border:1px solid rgba(0,0,0,0.07);border-radius:" + Math.round(d * 0.55) + "px;"
        + "box-shadow:0 1px 2px rgba(0,0,0,0.06),0 10px 26px rgba(0,0,0,0.09);opacity:0;";
      for (const c of (Array.isArray(colours) ? colours : [])) {
        const dot = document.createElement("span");
        dot.style.cssText = `width:${d}px;height:${d}px;border-radius:${Math.round(d * 0.3)}px;background:${c};`;
        chip.appendChild(dot);
      }
      box.appendChild(chip); this._chips.push(chip);
    }
  }
  registerContentAnimation(masterTl) {
    this._chips.forEach((chip, i) => {
      masterTl.fromTo(chip, { opacity: 0, scale: 0.8, y: 10 }, { opacity: 1, scale: 1, y: 0, duration: 0.45,
        ease: "back.out(1.6)", immediateRender: false }, this.at + i * this.stagger);
    });
  }
}

// ── Node Factory ──────────────────────────────────────────────────────────────
function createNodeFromSpec(nodeData) {
  if (!nodeData) return null;
  const type = nodeData.type;
  let node;
  if      (type === "shape_rect"  || type === "rect")        node = new ShapeRectNode(nodeData);
  else if (type === "shape_circle"|| type === "circle")      node = new ShapeCircleNode(nodeData);
  else if (type === "shape_arrow" || type === "arrow")       node = new ShapeArrowNode(nodeData);
  else if (type === "text")                                  node = new TextNode(nodeData);
  else if (type === "image")                                 node = new ImageNode(nodeData);
  else if (type === "glass_panel" || type === "glass")       node = new GlassPanelNode(nodeData);
  else if (type === "light_field")                           node = new LightFieldNode(nodeData);
  else if (type === "depth_layer")                           node = new DepthLayerNode(nodeData);
  else if (type === "icon")                                  node = new IconNode(nodeData);
  else if (type === "cursor")                                node = new CursorNode(nodeData);
  else if (type === "ui_pill" || type === "pill")            node = new UIPillNode(nodeData);
  else if (type === "ui_input")                              node = new UIInputNode(nodeData);
  else if (type === "ui_checklist")                          node = new UIChecklistNode(nodeData);
  else if (type === "ui_window")                             node = new UIWindowNode(nodeData);
  else if (type === "ui_swatches")                           node = new UISwatchesNode(nodeData);
  else if (type === "orb")                                   node = new OrbNode(nodeData);
  else if (type === "spline_tree")                           node = new SplineTreeNode(nodeData);
  else if (type === "particle_field")                        node = new ParticleFieldNode(nodeData);
  else if ((type === "domain_chart" || type === "chart") && window.DomainChartNode)
    node = new window.DomainChartNode(nodeData);
  else if ((type === "domain_diagram" || type === "diagram" || type === "workflow") && window.DomainDiagramNode)
    node = new window.DomainDiagramNode(nodeData);
  else
    node = new Node(nodeData);

  if (Array.isArray(nodeData.children)) {
    for (const childData of nodeData.children) {
      const child = createNodeFromSpec(childData);
      if (child) node.addChild(child);
    }
  }
  return node;
}

window.ShapeRectNode    = ShapeRectNode;
window.ShapeCircleNode  = ShapeCircleNode;
window.ShapeArrowNode   = ShapeArrowNode;
window.TextNode         = TextNode;
window.ImageNode        = ImageNode;
window.GlassPanelNode   = GlassPanelNode;
window.LightFieldNode   = LightFieldNode;
window.DepthLayerNode   = DepthLayerNode;
window.IconNode         = IconNode;
window.OrbNode          = OrbNode;
window.CursorNode       = CursorNode;
window.UIPillNode       = UIPillNode;
window.UIInputNode      = UIInputNode;
window.UIChecklistNode  = UIChecklistNode;
window.UIWindowNode     = UIWindowNode;
window.UISwatchesNode   = UISwatchesNode;
window.SplineTreeNode   = SplineTreeNode;
window.ParticleFieldNode = ParticleFieldNode;
window.createNodeFromSpec = createNodeFromSpec;
