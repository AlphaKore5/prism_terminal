"""
Prism Motion Graphics — sound that is generated from the timeline
──────────────────────────────────────────────────────────────────
The reference film this was compared against (11-12 Nov 2026) has a quiet bed
and about 2.8 sound events a second — a tick for every typed character, a
click for every press, a whoosh for every camera move, a chime for "done" —
and four of its seven strong visual events had an audio onset within 0.06 s
(1-2 frames). Prism's films had no audio at all.

That precision is not something to ask a model for. Every one of those moments
is already a number in the resolved spec — the time a character appears, the
time a cursor lands, a camera track's start — so the cues are read from the
spec, and the sounds are synthesised from them with numpy: deterministic (the
same spec makes the same file, byte for byte), no model call, no new
dependency, and in sync by construction.

    cues(resolved)            -> [{"t", "kind", "gain"}]
    synthesize(cues, seconds) -> float32 samples (mono, 44.1 kHz)
    build(resolved, path)     -> path of a 16-bit WAV, or None if nothing to play
"""
from __future__ import annotations

import wave
from typing import Any

SR = 44100
MAX_TICKS = 160          # a very long typed paragraph does not become a machine gun
_MIN_WHOOSH_GAP = 0.6


def _walk(nodes):
    for node in nodes or []:
        if isinstance(node, dict):
            yield node
            yield from _walk(node.get("children"))


def cues(resolved: dict[str, Any]) -> list[dict[str, Any]]:
    """Every moment worth a sound, with its time in seconds and a gain."""
    out: list[dict[str, Any]] = []

    def add(t, kind, gain=1.0):
        if t is not None and t >= 0:
            out.append({"t": round(float(t), 4), "kind": kind, "gain": gain})

    scenes = resolved.get("scenes", []) or []
    for si, sc in enumerate(scenes):
        start = float(sc.get("start", 0.0) or 0.0)
        if si > 0:
            add(start, "whoosh_soft", 0.7)
        for node in _walk(sc.get("nodes")):
            kind = node.get("type")
            if node.get("hit"):                      # the answer landing: a low hit as the node enters
                enter = (node.get("animation") or {}).get("enter") or {}
                add(enter.get("time", start), "hit", 1.0)
            if kind in ("text", "ui_input"):
                text = (("".join(str(r.get("text", "")) for r in node["runs"]) if node.get("runs") else str(node.get("content", "")))
                        if kind == "text" else str(node.get("text", "")))
                if (kind == "ui_input" and text) or (kind == "text" and node.get("type_on")):
                    rate = float(node.get("type_rate") or (14 if kind == "ui_input" else 16))
                    t0 = float(node.get("reveal_start") or 0.0)
                    typed = [i for i, ch in enumerate(text) if not ch.isspace()][:MAX_TICKS]
                    for i in typed:
                        add(t0 + i / max(1.0, rate), "tick", 0.8)
                if kind == "ui_input" and node.get("send_at") is not None:
                    add(node["send_at"], "click", 1.0)
            elif kind == "cursor":
                for seg in node.get("path") or []:
                    if isinstance(seg, dict) and seg.get("click"):
                        add(float(seg.get("at", 0.0)) + float(seg.get("duration", 0.7) or 0.7), "click", 1.0)
            elif kind in ("ui_pill", "pill"):
                enter = (node.get("animation") or {}).get("enter") or {}
                if enter.get("time") is not None:
                    add(enter["time"], "chime" if node.get("check") else "pop", 0.9 if node.get("check") else 0.6)
            elif kind == "ui_checklist":
                for it in node.get("items") or []:
                    if isinstance(it, dict):
                        add(it.get("at"), "pop", 0.5)
            elif kind == "ui_swatches":
                t0 = float(node.get("at", 0.0) or 0.0)
                step = float(node.get("stagger", 0.25) if node.get("stagger") is not None else 0.25)
                for i in range(len(node.get("groups") or [])):
                    add(t0 + i * step, "pop", 0.6)

    # Camera moves that a viewer would feel: a whoosh as each one starts.
    tracks = ((resolved.get("camera") or {}).get("tracks")) or []
    prev = None
    for tr in sorted((t for t in tracks if isinstance(t, dict)), key=lambda t: float(t.get("time", 0) or 0)):
        pos = tr.get("position") or [0, 0]
        z = float(tr.get("zoom", 1.0) or 1.0)
        dur = float(tr.get("duration", 0.0) or 0.0)
        if prev is not None and dur >= 0.3:
            dz = abs(z - prev[2]) / max(prev[2], 0.01)
            dp = ((pos[0] - prev[0]) ** 2 + (pos[1] - prev[1]) ** 2) ** 0.5
            if dz >= 0.08 or dp >= 120:
                add(float(tr.get("time", 0.0)), "whoosh", min(1.0, 0.5 + dz))
        prev = (pos[0], pos[1], z)

    out.sort(key=lambda c: (c["t"], c["kind"]))
    kept, last_whoosh = [], -9.0
    for c in out:
        if c["kind"].startswith("whoosh"):
            if c["t"] - last_whoosh < _MIN_WHOOSH_GAP:
                continue
            last_whoosh = c["t"]
        kept.append(c)
    return kept


# ── synthesis ────────────────────────────────────────────────────────────────

def _env(n, tau):
    import numpy as np
    return np.exp(-np.arange(n) / (SR * tau))


def _tick(rng, gain):
    import numpy as np
    n = int(0.014 * SR)
    x = rng.standard_normal(n).astype("float32")
    x = x - 0.93 * np.concatenate(([0.0], x[:-1]))          # brighten
    return (x * _env(n, 0.0025) * 0.30 * gain).astype("float32")


def _click(rng, gain):
    import numpy as np
    a = _tick(rng, gain * 1.1)
    n = int(0.03 * SR)
    thump = (np.sin(2 * np.pi * 170 * np.arange(n) / SR) * _env(n, 0.009) * 0.35 * gain).astype("float32")
    out = np.zeros(int(0.07 * SR), dtype="float32")
    out[:len(a)] += a
    out[:n] += thump
    b = _tick(rng, gain * 0.6)
    s = int(0.035 * SR)
    out[s:s + len(b)] += b
    return out


def _pop(rng, gain):
    import numpy as np
    n = int(0.12 * SR)
    t = np.arange(n) / SR
    f = 700 - 350 * np.minimum(1.0, t / 0.08)
    return (np.sin(2 * np.pi * np.cumsum(f) / SR) * _env(n, 0.03) * 0.30 * gain).astype("float32")


def _chime(rng, gain):
    import numpy as np
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    tone = np.sin(2 * np.pi * 880 * t) + 0.6 * np.sin(2 * np.pi * 1318.5 * t)
    attack = np.minimum(1.0, t / 0.004)
    return (tone * attack * _env(n, 0.3) * 0.22 * gain).astype("float32")


def _whoosh(rng, gain, seconds):
    import numpy as np
    n = int(seconds * SR)
    x = rng.standard_normal(n).astype("float32")
    u = np.linspace(0.0, 1.0, n, dtype="float32")
    cutoff = 300.0 + 3800.0 * np.sin(np.pi * u) ** 2          # the band sweeps up and back
    alpha = 1.0 - np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(n):                                          # one-pole low-pass, time-varying
        acc += alpha[i] * (x[i] - acc)
        y[i] = acc
    y = y - np.convolve(y, np.ones(400) / 400, mode="same")    # take the rumble out
    return (y * np.sin(np.pi * u) ** 2 * 0.9 * gain).astype("float32")


def _hit(rng, gain):
    import numpy as np
    n = int(0.55 * SR)
    t = np.arange(n) / SR
    f = 48.0 + 80.0 * np.exp(-t / 0.07)                    # a body that drops from ~128 Hz to ~48 Hz
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * _env(n, 0.14) * 0.62
    m = int(0.03 * SR)
    snap = rng.standard_normal(m).astype("float32") * _env(m, 0.006) * 0.35
    body[:m] += snap
    return (body * gain).astype("float32")


def _bed(seconds):
    import numpy as np
    n = int(seconds * SR)
    t = np.arange(n) / SR
    pad = sum(np.sin(2 * np.pi * f * t + p) for f, p in ((110.0, 0.0), (164.8, 1.3), (220.0, 2.1)))
    swell = 0.75 + 0.25 * np.sin(2 * np.pi * 0.07 * t)
    fade = np.minimum(1.0, np.minimum(t / 0.8, np.maximum(0.0, (seconds - t)) / 0.8))
    return (pad * swell * fade * 0.012).astype("float32")


def synthesize(cue_list: list[dict[str, Any]], seconds: float, bed: bool = True):
    import numpy as np
    n = int(seconds * SR) + SR // 2
    mix = _bed(seconds + 0.5)[:n] if bed else np.zeros(n, dtype="float32")
    if bed and len(mix) < n:
        mix = np.pad(mix, (0, n - len(mix)))
    rng = np.random.default_rng(20261112)
    for c in cue_list:
        kind, gain = c["kind"], float(c.get("gain", 1.0))
        if kind == "tick":
            clip = _tick(rng, gain)
        elif kind == "click":
            clip = _click(rng, gain)
        elif kind == "pop":
            clip = _pop(rng, gain)
        elif kind == "chime":
            clip = _chime(rng, gain)
        elif kind == "hit":
            clip = _hit(rng, gain)
        elif kind == "whoosh":
            clip = _whoosh(rng, gain, 0.7)
        elif kind == "whoosh_soft":
            clip = _whoosh(rng, gain, 0.4)
        else:
            continue
        s = int(c["t"] * SR)
        if s >= n:
            continue
        e = min(n, s + len(clip))
        mix[s:e] += clip[:e - s]
    return np.clip(mix, -0.9, 0.9)


def write_wav(samples, path: str) -> str:
    import numpy as np
    pcm = (np.asarray(samples) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def build(resolved: dict[str, Any], path: str, bed: bool = True) -> str | None:
    """Write the film's sound to `path`; None when there is nothing to play."""
    project = resolved.get("project") or {}
    seconds = float(project.get("duration") or sum(float(s.get("duration", 0) or 0) for s in resolved.get("scenes", [])))
    cue_list = cues(resolved)
    if not cue_list or seconds <= 0:
        return None
    return write_wav(synthesize(cue_list, seconds, bed=bed), path)
