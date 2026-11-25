/**
 * Prism Motion Graphics Engine — Visual Effects Pipeline
 * ───────────────────────────────────────────────────────
 * Fully parameterized 2D effects renderer. No hardcoded themes.
 * The AI agent freely specifies every visual parameter (colors, spotlight,
 * particles, grain, vignette) in the motion spec's `visual` block.
 * This module executes exactly what the AI designed.
 */

class MotionEffects {
  constructor(width = 1080, height = 1920) {
    this.w = width;
    this.h = height;
    this._particles = null;
    this._particleConfig = null;
    this._noiseCanvas = this._generateNoiseCanvas(512, 512);
  }

  // ── Noise Texture ──────────────────────────────────────────────────────────
  _generateNoiseCanvas(w, h) {
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const ctx = c.getContext("2d");
    const imgData = ctx.createImageData(w, h);
    const buf = new Uint32Array(imgData.data.buffer);
    for (let i = 0; i < buf.length; i++) {
      const v = Math.floor(Math.random() * 255);
      buf[i] = (20 << 24) | (v << 16) | (v << 8) | v;
    }
    ctx.putImageData(imgData, 0, 0);
    return c;
  }

  // ── Particle Pool ──────────────────────────────────────────────────────────
  _initParticles(config = {}) {
    const count   = config.count        || 28;
    const minR    = config.min_radius   || 1.2;
    const maxR    = config.max_radius   || 4.0;
    const speedY  = config.speed_y      || [-8, -22];
    const minAlpha = config.min_alpha   || 0.10;
    const maxAlpha = config.max_alpha   || 0.40;

    const list = [];
    for (let i = 0; i < count; i++) {
      list.push({
        x:        Math.random() * this.w,
        y:        Math.random() * this.h,
        z:        0.2 + Math.random() * 0.8,
        radius:   minR + Math.random() * (maxR - minR),
        baseAlpha: minAlpha + Math.random() * (maxAlpha - minAlpha),
        speedX:   (Math.random() - 0.5) * 16,
        speedY:   speedY[0] + Math.random() * (speedY[1] - speedY[0]),
        phase:    Math.random() * Math.PI * 2
      });
    }
    return list;
  }

  _getParticles(config) {
    const key = JSON.stringify(config);
    if (this._particleConfig !== key) {
      this._particleConfig = key;
      this._particles = this._initParticles(config);
    }
    return this._particles;
  }

  // ── 1. Parameterized Studio Backdrop ──────────────────────────────────────
  /**
   * Draws the full cinematic studio backdrop using the AI-specified `visual` block.
   * @param {CanvasRenderingContext2D} ctx
   * @param {object} visual  - free-form visual config from AI motion spec
   * @param {object} camera  - current camera state
   * @param {number} time    - current animation time in seconds
   */
  drawStudioBackdrop(ctx, visual = {}, camera, time) {
    const bg = visual.background || "#07091A";

    // Base fill
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, this.w, this.h);

    // Primary volumetric spotlight — AI controls color, position, and radius
    const spot = visual.spotlight || {};
    const spotPosX = (spot.position ? spot.position[0] : 0.5) * this.w;
    const spotPosY = (spot.position ? spot.position[1] : 0.40) * this.h;
    const spotColor  = spot.color     || "rgba(56, 189, 248, 0.14)";
    const spotColor2 = spot.secondary || "rgba(30, 27, 75, 0.22)";
    const radiusFactor = spot.radius_factor || 0.78;

    // Subtle camera tracking on the spotlight (adds life to hold frames)
    const camLagX = camera ? (camera.x - this.w / 2) * 0.2 : 0;
    const camLagY = camera ? (camera.y - this.h / 2) * 0.2 : 0;
    const sx = spotPosX + camLagX;
    const sy = spotPosY + camLagY;
    const sr = Math.max(this.w, this.h) * radiusFactor;

    ctx.save();
    // "none" = no spotlight at all. Not a transparent colour: a canvas gradient
    // interpolates in non-premultiplied RGBA, so fading from transparent BLACK
    // to the background passes through dark grey and draws a vignette ring.
    if (!(spotColor === "none" && spotColor2 === "none")) {
      const clear = `rgba(${_hexToRgb(bg)}, 0)`;
      const grad = ctx.createRadialGradient(sx, sy, 36, sx, sy, sr);
      grad.addColorStop(0,    spotColor  === "none" ? clear : spotColor);
      grad.addColorStop(0.30, spotColor2 === "none" ? clear : spotColor2);
      grad.addColorStop(0.72, `rgba(${_hexToRgb(bg)}, 0.88)`);
      grad.addColorStop(1,    `rgba(${_hexToRgb(bg)}, 1.0)`);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, this.w, this.h);
    }

    // Secondary accent spotlight (optional — AI can provide)
    if (spot.accent) {
      const ax = (spot.accent_position ? spot.accent_position[0] : 0.85) * this.w;
      const ay = (spot.accent_position ? spot.accent_position[1] : 0.15) * this.h;
      const ag = ctx.createRadialGradient(ax, ay, 20, ax, ay, sr * 0.55);
      ag.addColorStop(0,   spot.accent);
      ag.addColorStop(0.5, "rgba(0,0,0,0)");
      ctx.fillStyle = ag;
      ctx.fillRect(0, 0, this.w, this.h);
    }

    // Optional animated grid (blueprint / technical aesthetic)
    const grid = visual.grid || {};
    if (grid.enabled !== false) {
      const gColor = grid.color || "rgba(255,255,255,0.022)";
      const gSize  = grid.size  || 80;
      const gSpeed = grid.scroll_speed || 5;
      const ox = (time * gSpeed) % gSize;
      const oy = (time * (gSpeed * 0.65)) % gSize;

      ctx.strokeStyle = gColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = -gSize + ox; x < this.w + gSize; x += gSize) {
        ctx.moveTo(x, 0); ctx.lineTo(x, this.h);
      }
      for (let y = -gSize + oy; y < this.h + gSize; y += gSize) {
        ctx.moveTo(0, y); ctx.lineTo(this.w, y);
      }
      ctx.stroke();
    }

    ctx.restore();
  }

  // ── 2. AI-Parameterized Ambient Particles ─────────────────────────────────
  drawAmbientParticles(ctx, visual = {}, camera, time) {
    const pc = visual.particles || {};
    if (pc.count === 0 || pc.enabled === false) return;

    const particles = this._getParticles(pc);
    const color    = pc.color     || "#38BDF8";
    const glowBlur = pc.glow_blur || 10;

    ctx.save();
    ctx.globalCompositeOperation = "screen";

    for (const p of particles) {
      const curY = ((p.y + p.speedY * time) % (this.h + 100));
      const y    = curY < -50 ? this.h + 50 : curY;
      const x    = (p.x + p.speedX * time + Math.sin(time + p.phase) * 18) % this.w;

      const camShiftX = camera ? (camera.x - this.w / 2) * (1 - p.z) * 0.12 : 0;
      const camShiftY = camera ? (camera.y - this.h / 2) * (1 - p.z) * 0.12 : 0;

      const alpha = p.baseAlpha * (0.55 + 0.45 * Math.sin(time * 1.8 + p.phase));

      ctx.beginPath();
      ctx.arc(x - camShiftX, y - camShiftY, p.radius * p.z, 0, Math.PI * 2);
      ctx.fillStyle = _colorWithAlpha(color, alpha);
      ctx.shadowColor = color;
      ctx.shadowBlur  = glowBlur;
      ctx.fill();
    }
    ctx.restore();
  }

  // ── 3. Glassmorphism Specular Surface ─────────────────────────────────────
  /**
   * Draws a high-quality glassmorphism card with multi-tier elevation shadows
   * and a dual-tone specular 1px linear border.
   * AI controls: fill, border color, shadow depth, radius, sheen opacity.
   */
  static drawGlassCard(ctx, x, y, w, h, radius = 24, config = {}) {
    const anchorX = config.anchorX !== undefined ? config.anchorX : 0.5;
    const anchorY = config.anchorY !== undefined ? config.anchorY : 0.5;
    const rx = x - w * anchorX;
    const ry = y - h * anchorY;
    const r  = Math.min(radius, w / 2, h / 2);

    ctx.save();

    // Tier 1: Deep diffuse elevation shadow
    ctx.shadowColor   = config.shadow_color  || "rgba(0,0,0,0.60)";
    ctx.shadowBlur    = config.shadow_blur   || 42;
    ctx.shadowOffsetY = config.shadow_offset || 20;

    ctx.beginPath();
    ctx.roundRect(rx, ry, w, h, r);
    ctx.fillStyle = config.fill || "rgba(13, 18, 38, 0.90)";
    ctx.fill();
    ctx.shadowColor = "transparent";

    // Tier 2: Contact shadow (sharp, close)
    ctx.shadowColor   = "rgba(0,0,0,0.35)";
    ctx.shadowBlur    = 8;
    ctx.shadowOffsetY = 4;
    ctx.fill();
    ctx.shadowColor = "transparent";

    // Linear specular sheen (top-left to bottom-right)
    const sheen = ctx.createLinearGradient(rx, ry, rx + w * 0.5, ry + h);
    sheen.addColorStop(0,   `rgba(255,255,255,${config.sheen_opacity || 0.085})`);
    sheen.addColorStop(0.4, `rgba(255,255,255,${(config.sheen_opacity || 0.085) * 0.18})`);
    sheen.addColorStop(1,   "rgba(0,0,0,0.18)");
    ctx.fillStyle = sheen;
    ctx.fill();

    // 1.5px dual-tone specular border
    const borderTop    = config.border_top    || "rgba(255,255,255,0.30)";
    const borderBottom = config.border_bottom || "rgba(255,255,255,0.02)";
    const bGrad = ctx.createLinearGradient(rx, ry, rx, ry + h);
    bGrad.addColorStop(0,    borderTop);
    bGrad.addColorStop(0.45, "rgba(255,255,255,0.07)");
    bGrad.addColorStop(1,    borderBottom);
    ctx.strokeStyle = bGrad;
    ctx.lineWidth   = 1.5;
    ctx.stroke();

    ctx.restore();
  }

  // ── 4. Neon Glow Stroke ───────────────────────────────────────────────────
  static drawNeonStroke(ctx, path2D_or_fn, color = "#38BDF8", width = 4, glowRadius = 18) {
    ctx.save();
    ctx.globalCompositeOperation = "screen";
    ctx.strokeStyle = color;
    ctx.lineWidth   = width + glowRadius * 0.5;
    ctx.shadowColor = color;
    ctx.shadowBlur  = glowRadius;
    ctx.lineCap  = "round";
    ctx.lineJoin = "round";
    if (typeof path2D_or_fn === "function") path2D_or_fn(ctx);
    else ctx.stroke(path2D_or_fn);

    // Bright inner stroke
    ctx.lineWidth   = width;
    ctx.shadowBlur  = glowRadius * 0.4;
    ctx.strokeStyle = _lighten(color, 0.55);
    if (typeof path2D_or_fn === "function") path2D_or_fn(ctx);
    else ctx.stroke(path2D_or_fn);

    ctx.restore();
  }

  // ── 5. Post-Processing: Film Grain & Vignette ─────────────────────────────
  /**
   * @param {CanvasRenderingContext2D} ctx
   * @param {object} visual  - AI visual config
   */
  applyPostProcessing(ctx, visual = {}) {
    ctx.save();

    // Film grain overlay
    const grainOpacity = visual.grain_opacity !== undefined ? visual.grain_opacity : 0.05;
    if (grainOpacity > 0.001 && this._noiseCanvas) {
      ctx.globalCompositeOperation = "overlay";
      ctx.globalAlpha = grainOpacity;
      const pat = ctx.createPattern(this._noiseCanvas, "repeat");
      ctx.fillStyle = pat;
      ctx.fillRect(0, 0, this.w, this.h);
    }

    // Radial corner vignette
    const vigStrength = visual.vignette_strength !== undefined ? visual.vignette_strength : 0.58;
    if (vigStrength > 0.001) {
      ctx.globalCompositeOperation = "multiply";
      ctx.globalAlpha = 1.0;
      const vigGrad = ctx.createRadialGradient(
        this.w / 2, this.h / 2, this.w * 0.32,
        this.w / 2, this.h / 2, this.w * 0.88
      );
      vigGrad.addColorStop(0,    "rgba(255,255,255,1.0)");
      vigGrad.addColorStop(0.62, "rgba(210,218,235,0.97)");
      vigGrad.addColorStop(1,    `rgba(8,10,18,${vigStrength})`);
      ctx.fillStyle = vigGrad;
      ctx.fillRect(0, 0, this.w, this.h);
    }

    ctx.restore();
  }
}

// ── Color Utilities ──────────────────────────────────────────────────────────
function _hexToRgb(hex) {
  const h = hex.replace("#", "");
  const n = h.length === 3
    ? h.split("").map(c => parseInt(c + c, 16))
    : [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
  return `${n[0]},${n[1]},${n[2]}`;
}

function _colorWithAlpha(color, alpha) {
  if (color.startsWith("rgba")) {
    return color.replace(/[\d.]+\)$/, `${alpha.toFixed(3)})`);
  }
  if (color.startsWith("#")) {
    return `rgba(${_hexToRgb(color)},${alpha.toFixed(3)})`;
  }
  return color;
}

function _lighten(hex, amount = 0.3) {
  try {
    const parts = _hexToRgb(hex).split(",").map(Number);
    const r = Math.min(255, Math.round(parts[0] + (255 - parts[0]) * amount));
    const g = Math.min(255, Math.round(parts[1] + (255 - parts[1]) * amount));
    const b = Math.min(255, Math.round(parts[2] + (255 - parts[2]) * amount));
    return `rgb(${r},${g},${b})`;
  } catch { return hex; }
}

window.MotionEffects   = MotionEffects;
window._hexToRgb       = _hexToRgb;
window._colorWithAlpha = _colorWithAlpha;
window._lighten        = _lighten;
