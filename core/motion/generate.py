"""
Prism Motion Graphics — scene-at-a-time generation
────────────────────────────────────────────────────
Turn one names the project, the camera's overall intent, and a storyboard
row per scene — not the scenes themselves. Every scene's actual nodes are
then asked for on their own turn, in the same conversation, and checked
before the next is asked for. This is the same shape as
core.reel_web.build_spec(), for the same two reasons: a model asked to
write a whole motion graphic in one reply spreads its budget thin across
every scene (measured on Studio: 278 chars/scene one-shot vs. 2,339
chars/scene one-turn-per-scene — see CHANGES.md "Round 6"), and a fault
raised while the model is still "on" that scene lands; raised later
against a reply it has moved past, it does not.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .. import reel_web as _web
from .schema import MotionValidationError

SCENE_EXPECT = '"nodes"'

_PALETTE_GUIDANCE = """Choose a PALETTE and a TYPE PAIRING for this specific brief — not from a
fixed industry lookup. "Financial" does not mean navy, "SaaS" does not mean
cyan-on-obsidian — those are reflexes, and every video that reaches for the
same reflex for the same kind of request looks like it came from a template
library, no matter how different the copy is.

Decide instead from what the brief is actually asking to FEEL like: is it
warm/editorial and trustworthy (cream or ivory background, a dark ink text
colour, one warm metal or earth accent, a serif display face), cool/precise
and technical (deep charcoal or ink background, one saturated accent,
condensed sans display face), bright/energetic and consumer-facing (a light
or mid-tone ground, a bold saturated accent, a heavy display weight), or
something else the brief itself suggests — pick the mood, then commit to a
small NAMED set for the whole piece:
  project.palette: { bg_a, bg_b, ink, accent, accent2 } — bg_a/bg_b are the
    two backgrounds scenes alternate between (see rule 7 below), ink is the
    text colour that reads on whichever background is light, accent is the
    one colour used sparingly and consistently as the piece's signature.
  project.type: { display_font, body_font } — a real two-font pairing (a
    display/serif or condensed face for headlines and numbers, a plain
    sans for body copy and labels) picked for the SAME mood as the
    palette, not the same font doing both jobs.

DO NOT use generic or safe designs. Make bold, specific, production-quality
decisions — and make a genuinely different decision than the last brief
that felt similar, the same way rule 4 below asks for genuinely different
easing choices scene to scene."""

_LOOK_GUIDANCE = """LOOK — set project.look, and let the background colour agree with it:
  paper — a flat light page (an off-white like #FAF9F6, hairline shadows). The
          default for products, services, explainers, anything friendly or
          premium. Colour is punctuation, not a field: mostly page, one or two
          accents. Type and interface (ui_input, ui_pill, cursor) carry it.
          Do NOT add light_field or glass_panel on a flat page: a glow is almost
          invisible there and each light_field makes the film render about
          four times slower.
  ink   — a flat dark page. For a serious, cinematic or luxury piece.
  tech  — the dark-tech backdrop: a cyan spotlight, a scrolling grid, drifting
          particles, a vignette. Only when the subject is genuinely technical
          and you want that atmosphere; it is NOT a neutral default and it
          turns a light background blue-grey.
A light background with no look is treated as paper."""


_NODE_CATALOGUE = """NODE TYPES (put these in "nodes"):
  text             — content, position, font_size, font_weight, fill, mode (see TEXT MODES).
                      A long headline may use "\\n" for a manual line break —
                      each line is centred and stacked automatically. Optional
                      "runs": [{"text","fill"|"gradient","weight"}] makes parts of
                      one line differ in colour or weight (a highlighted word);
                      "type_on": true with "type_rate" (characters/second) types
                      it out with a caret that follows. Set "anchor" [0,0] to
                      start from the left, [0.5,0.5] to centre.
  cursor           — position (the pointer tip), label (a role or name), color,
                      size, path: [{"at","to":[x,y],"duration","easing","click"}].
                      A labelled pointer that travels and clicks — for a film
                      about something being operated, not for decoration.
  ui_pill          — label, dot (a colour) or check (true = green tick), font_size,
                      fill, ink. A status chip: "Rendering", "Finished".
  ui_input         — width, height, placeholder, text (types itself from
                      reveal_start at type_rate), send_at (the send button turns
                      from grey to ink). A prompt box.
  ui_checklist     — items: [{"label","sub","color","at","fill"}], width, font_size.
                      Rows of work in progress — a coloured dot, a label, a quieter
                      sub-line, and an underline that fills from "at" over "fill" seconds.
  ui_window        — width, height, title, timecode, scrubber, progress, play_at,
                      play_for, content_fill, children. An application window with a
                      title bar and a scrubber; children sit inside it (positions
                      relative to its centre). The product's own output, on screen.
  ui_swatches      — groups: [[colour, ...], ...], at, stagger. A row of palette chips
                      that pop in one after another — a variation being offered.
  shape_rect       — position, width, height, radius, fill, is_glass (optional).
                      For hairlines, pills, badges and chat bubbles. It is the
                      WRONG primitive for a background or a card: a flat
                      rectangle wider and taller than half the frame is sent
                      back — a background is a light_field (or a gradient
                      fill), a card is a glass_panel.
  shape_arrow      — from, to, curved, color, stroke_width, draw_start, draw_duration
  domain_chart     — chart_type (bar|line|ring|area|sparkline|metric), data, accent_color
  domain_ui_mockup — position, width, height, title, elements, cursor_actions
  domain_diagram   — nodes (id/label/position/shape/color), edges (from/to/pulse)
  glass_panel      — position, width, height, radius, tint (a colour; a hex tint is
                      applied at the panel's transmission), blur (px, 0-40),
                      transmission 0-1 (how much of the scene shows through),
                      border_light 0-1 (edge highlight), inner_shadow 0-1,
                      specular 0-1 (a diagonal highlight), contrast_guard
                      "dark" | "light" | false (a scrim so the panel's text stays
                      readable over a bright light as well as a dark wash). Put
                      the panel's own text/images in its "children" (positions
                      relative to the panel's centre). ONE main panel per shot —
                      depth comes from depth_layer hierarchy, not repeated cards.
  light_field      — position, width, height, color, color2, intensity 0-1,
                      spread 0.2-2, drift (px). A soft drifting light behind the
                      subject (screen-blended). Use it as the background layer in
                      place of a flat wash; it may bleed past the frame.
  depth_layer      — depth (0.4 far … 1 = the subject plane … 1.6 near) and
                      "children". A group that moves with the camera by its depth:
                      further layers drift and zoom less, nearer ones more. Compose
                      a shot as far light_field → mid glass_panel → near accents,
                      each in its own depth_layer.
  particle_field   — count, size, palette, seed, flicker, and "phases": a list of
                      {"at", "layout", "duration", "stagger"}; the field holds the
                      first layout and travels to each next one at its "at" (scene-
                      local). Layouts: {"kind":"scatter","box":[x0,y0,x1,y1]},
                      {"kind":"band","y","rows","cell","x0","x1"}, {"kind":"column",
                      "x","spread","y0","y1"}, {"kind":"ring","center","radius"},
                      {"kind":"points","points":[[x,y],...],"jitter"}, {"kind":"hidden"}.
                      Many small tiles of light — a signal, data, a crowd of sources —
                      that gathers, streams and converges. Give it the continuity_key
                      when it IS the carried subject; declare it again in the next
                      scene starting from the layout this scene ended on.
  icon             — name (chat|phone|mail|whatsapp|book|dial|spark|check), size,
                      color, tile (true = on a dark glass tile), tile_color, radius.
  orb              — radius, core (colour), tint, rim, glow_color, glow_blur, highlight
                      0-1, icon (an icon name drawn at its centre), ring_radius +
                      ring_color (a faint great circle around it). A sphere — a dark
                      hub, a glowing product orb.
  spline_tree      — hub [x,y], leaves [[x,y],...] (relative to position), color,
                      stroke_width, glow_blur, bend 0-1, draw_start, draw_duration,
                      stagger, dots. Thin curved lines drawn on from one hub to many
                      leaves — channels converging, a knowledge graph.
  image            — position, width, height, radius (corner rounding), anchor;
                      "src" is `asset:<name>` for one of the client's own images
                      the scene prompt names (never write a real URL or invent a name) — a logo,
                      a product photo, a screenshot. Drawn clipped to a rounded
                      rect; SVG marks work the same way as photos.

  focus           — true on ONE node per scene: what the eye must land on first. It must
                    be the biggest thing on the page and the shot goes toward it.
  hit             — true: a low sound hit as this node enters. For the answer landing.
  continuity_key  — optional stable name for the visual subject that survives
                    across scene boundaries (for example "signal", "product",
                    or "orb"). Node ids remain unique per scene; reuse this
                    key when the same subject returns so Prism can track its
                    handoff and Studio can select the whole visual thread.

"anchor" (on any node) is ALWAYS a two-number [x, y] fraction of the node's
own box, e.g. [0.5, 0.5] for its centre, [0, 0] for its top-left corner —
never a keyword string like "center" or "top-left".

TEXT MODES (set on any text node via "mode"):
  word_stagger   — words pop in sequentially with spring overshoot (default, versatile)
  masked_reveal  — text rises from behind horizontal stencil clip (editorial, slow)
  shimmer_sweep  — fade in then light-beam sweeps across the headline (premium feel)
  char_cascade   — characters fall in with staggered bounce (energetic, consumer)
  split_slide    — text halves slide in from opposite sides (dramatic, brand)
  blur_pop       — blurs to sharp with scale pop (tech, SaaS)
  counter_tick   — numeric value ticks up from 0 to target (data, financial)
  typewriter     — characters appear left-to-right with cursor (developer, code)

ANIMATION, on any node via "animation" (all times are LOCAL to this scene, starting at 0):
  "enter": {"time":, "duration":, "tweens": [
              {"channel": "opacity", "from": 0, "to": 1, "easing": "power2.out"},
              {"channel": "y", "from": 20, "to": 0, "easing": "power2.out", "delay": 0.05},
              ...
           ]}
  "exit":  same shape as "enter" — give at least one node per scene a real
           exit so the scene doesn't just settle and hold until the cut.
  Put as many tweens in one enter/exit as the moment needs — this is how a
  blur-focus reveal, a directional slide, a scale-pop and a plain fade are
  all really just different CHANNEL COMBINATIONS of the same mechanism, not
  different fixed "types" to pick from. "from"/"to" for x/y are relative
  OFFSETS from the node's own resting position (20 means 20px away from
  where it settles, not an absolute coordinate); scale/scaleX/scaleY/
  rotation/skewX/skewY work the same way — a scale tween is an offset from
  the node's own scale, so a pop that settles at 1x is from 0.08 to 0 (it
  starts 1.08x), a grow-in is from -0.2 to 0; opacity is ABSOLUTE, 0 to 1;
  blur/clipInset/
  backgroundPositionX/strokeDashoffset are absolute values in their own
  units (blur in px, clipInset 0-100 as percent revealed, strokeDashoffset
  in the node's own path-length units — leave it to the runtime's own
  full-length value, tween TO 0 to fully draw a line/arrow/mark).
  TWEEN CHANNELS: opacity, x, y, scale, scaleX, scaleY, rotation, skewX,
    skewY, blur, clipInset, backgroundPositionX, strokeDashoffset.
  "secondary_motion": {"property": "<any channel above>", "freq":, "amount":,
                        "seed": "<anything stable>"} — a small deterministic
           wiggle on top of the main animation, running the WHOLE time this
           node is on screen (see the background layer's rule below).
  "follow": {"lag":, "damping":} — on a CHILD node, makes it trail the parent's
           recent motion instead of moving in rigid lockstep. (Not yet
           supported by the current runtime — avoid relying on it.)

SHOT, on a SCENE (next to "nodes") — what the camera does for this beat. The
shots of all scenes are compiled into ONE continuous camera curve for the
whole film; a shot only says where the camera ends up, so it always starts
wherever the previous shot left it (there is no per-scene camera reset):
  "shot": {"intent": "push", "target": "<node id>" or [x, y], "zoom": 1.2, "duration": 0.6}
  Optional "duration" (seconds the move takes; default most of the scene)
  and "delay" (seconds into the scene before it starts, holding the previous
  shot's end state — a scene that settles on its icons, then pans down to
  the hub). A node target puts that node at the frame's centre.
  ("duration" is optional: seconds the move takes; omit it and the move
  spans the scene, right for a drift, wrong when copy must land on a
  settled frame.)
  hold     — stay where the camera is.
  reveal   — settle out onto the target (an opening).
  push     — move in on the target (zoom ×1.15 unless "zoom" is given).
  pull     — ease back out.
  orbit    — a slow tilt around the target.
  parallax — a lateral drift, zoom unchanged (depth_layers separate).
  macro    — close on a detail (zoom 1.6 unless given).
  resolve  — return to the centre at 1.0 for the ending.

TRANSITIONS, on a SCENE (not a node) via "transition_in" — how this scene
cuts in from the one before it. Omit it and one is still picked for you
(never a silent hard cut), but naming one on purpose usually reads better:
  push        — both scenes travel together, new one pushing the old off. Neutral, use for an ordinary beat.
  push_up     — same as push, vertical instead of horizontal.
  squeeze     — the old scene compresses away, the new one opens out. Mechanical, precise — industrial/technical subjects.
  zoom        — the old scene rushes past and blurs, the new one rises from behind it. Reserve it — it reads as pushing deeper into the same thought.
  blur_swoosh — both scenes blur/skew past each other, directional. Editorial, motion-forward.
  light_leak  — a warm light wash bridges the cut. Editorial, warm, premium — good between two beats of the same argument rather than a hard scene change.
  morph       — a plain crossfade. Any cut where a node's "continuity_key" carries over from the previous scene becomes a morph automatically: both instances of that subject are tweened along one matched path between their two poses, so only the subject moves and everything else dissolves.

EASING VALUES (GSAP's own — the runtime hands these straight to the tween engine):
  power1.in/out/inOut, power2.in/out/inOut, power3.in/out/inOut, power4.in/out/inOut,
  back.in/out/inOut, elastic.in/out/inOut, bounce.in/out/inOut,
  circ.in/out/inOut, expo.in/out/inOut, sine.in/out/inOut, none

  Pick by what the thing is doing, not by habit — reusing one easing for
  everything that moves is the fastest way to look like a slide deck, no
  matter how many elements are on screen. `.out` curves for things
  ARRIVING, `.in` curves for things LEAVING, `sine.inOut`/`none` for
  motion still running when the scene hands over. Use at least two
  distinct easings in every scene."""


SCENE_ROLES = ("HOOK", "REVEAL", "PROOF", "SIGNOFF")

_LAYER_DOCTRINE = """LAYERS — give every node a "layer" (not just a z_index guess):
  background — full-bleed wash, glow or gradient behind everything else.
               MUST carry "secondary_motion" running for the WHOLE scene
               (not just enter/exit) — a slow drift, pulse or rotation.
               A background that just sits there once it's in is exactly
               what makes a scene read as a held slide, not a shot.
  midground  — a supporting shape or glass panel that gives the scene
               depth. Move it slower than the foreground (a smaller
               secondary_motion amount, or a longer enter duration) so it
               reads as further back, not flat with everything else.
  foreground — the hero of the scene: the headline, the product/logo
               image. MUST either really "exit" or carry its own
               "secondary_motion" while it holds — appearing once and then
               going completely still until the cut is the one thing this
               layer is not allowed to do. A closing scene settling on a
               logo is fine; settling DEAD is not — give it a slow
               breathe/drift even then.
  accent     — small floating detail(s): a badge, a stat, a short label.
               Fast, energetic motion — this is what makes a frame feel
               busy/alive without competing with the foreground.
  finish     — an overlay (vignette, radial darken at the edges) that sits
               on top of everything. Static — it's a treatment, not a
               character.

Not every layer needs a node every scene, but background and foreground
are required — a scene with no background layer has no depth, and a
scene with no foreground has no subject."""

_CINEMATIC_GLASS_DOCTRINE = """CONTINUOUS CINEMATIC PROFILE — this is a motion film, not a stack of unrelated slides.
  · Choose one recognisable visual spine (a signal, ribbon, orb, product card,
    line or light field) and carry it through the whole piece. Give its hero
    node a stable "continuity_key" and reuse that key whenever the subject
    returns in another scene.
  · The outgoing final pose and incoming first pose must agree: position,
    scale, rotation, dominant colour and opacity should feel like the same
    object crossing the handoff. Do not reset the subject to the centre at
    every cut.
  · Keep the subject alive during the transition. Use a directional
    blur_swoosh, light_leak or push when appropriate; avoid a full-scene fade
    to empty followed by a cold rebuild.
  · Give every scene a "shot" (see SHOT above) and let the compiled camera
    curve carry the film: reveal → push/orbit/parallax → resolve. Never
    reset the camera to the same zoom and centre at every scene.
  · Glass is layered depth: ONE main glass_panel (the material primitive, not
    a plain rect) carrying the copy, a light_field behind it, small accents in
    front — each in its own depth_layer (far ≈ 0.55, subject 1.0, near ≈ 1.45).
    Restrained border light and specular beat many identical cards. Preserve
    generous negative space and a clear typographic hierarchy.
  · Keep headlines and logos inside the safe area: on a vertical (9:16) frame nothing
    that must be read sits in the top 11% or the bottom 20%.
  · The reference grammar in one line: a particle_field of scattered tiles
    gathers into a band, streams into a column, converges on icons and a
    hub orb over a spline_tree; a light_field sweeps in for a conversation
    in bubbles; a knowledge tree with small glass cards; the Knowledge Base
    card over a glowing tree; a great arc; the glowing orb; the logo. Use
    those primitives for those jobs instead of rebuilding them from rects.
  · The final scene resolves the carried subject into the logo, answer or CTA;
    it should feel like a destination, not a new template."""


def _scene_role(idx: int) -> str:
    return SCENE_ROLES[idx] if 0 <= idx < len(SCENE_ROLES) else "SCENE"


_ROLE_BRIEF = {
    "HOOK": "The cold open. One bold claim or the brand/product name, "
            "nothing else competing for attention. Fast — this scene "
            "should feel like it's already moving when it appears.",
    "REVEAL": "The payoff. Whatever the hook promised, shown large — the "
              "product, the logo, the number. This is the scene the "
              "other three exist to set up and close out.",
    "PROOF": "One supporting detail that earns the claim — a benefit, a "
             "stat, a second angle. Calmer than the hook, still moving.",
    "SIGNOFF": "Logo lockup and/or a short tagline/CTA. The close — "
               "energy settles here, it does not spike.",
}


def _scene_handoff(scene: dict) -> dict | None:
    """What the LAST foreground node in a finished scene exits with — fed
    to the next scene's instructions so the cut continues a motion/colour
    instead of resetting cold. Returns None if there's nothing to hand
    off (no foreground node, or it never exits)."""
    best = None
    for node in scene.get("nodes", []) or []:
        if not isinstance(node, dict) or node.get("layer") != "foreground":
            continue
        anim = node.get("animation")
        exit_ = anim.get("exit") if isinstance(anim, dict) else None
        # A keyed subject is handed off even without an authored exit — the
        # continuity compiler writes its bridge — so the next scene learns
        # where to pick it up. An unkeyed foreground still needs an exit
        # to have anything worth continuing.
        if isinstance(exit_, dict) or node.get("continuity_key"):
            best = {
                "type": exit_.get("type", "fade_in") if isinstance(exit_, dict) else "morph",
                "fill": node.get("fill") or node.get("accent_color"),
                "continuity_key": node.get("continuity_key", ""),
                "node": str(node.get("id", "")),
                "position": list(node.get("position") or [0, 0]),
                "scale": list(node.get("scale") or [1, 1]),
            }
    return best


# The node type names alone, read off the catalogue, so the one-line list
# each scene prompt carries cannot drift from the catalogue turn one sent.
_NODE_TYPE_NAMES = tuple(re.findall(
    r"^  ([a-z_]+)\s+—", _NODE_CATALOGUE.split("\n\n", 1)[0], re.M))


def _scene_rules(skeleton: str | None) -> str:
    """The rules every scene is written to, numbered once for the whole
    conversation. _PALETTE_GUIDANCE points at rules 4 and 7 by number."""
    rules = (
        "SCENE RULES — every scene you write after this turn follows these:\n"
        "1. 3-7 nodes. Do not overcrowd.\n"
        "2. Vary the text mode — use at most 2 different modes.\n"
        '3. Give at least one node a real "exit", not only "enter".\n'
        "4. The most common way a scene ends up reading as a PowerPoint "
        "slide, however busy it is, is every element using the same "
        "easing curve. Treat that as the failure to design against.\n"
        "5. Never use generic emojis in text content.\n"
        "6. The brand's accent colour should recur, not repeat identically —\n"
        "   the same one or two colours framing every single scene the same\n"
        "   way (same white headline, same accent subtitle, same background)\n"
        "   reads as one template stamped four times, not four scenes of one\n"
        "   film. Let where and how the accent is used change: a highlighted\n"
        "   word instead of a whole line, a filled shape instead of an\n"
        "   outline, a background wash instead of just text — same brand,\n"
        "   different weight each time.\n"
        "7. Use the palette/type chosen in this turn BY NAME — a full-bleed "
        "background node filled with project.palette.bg_a or bg_b (alternate "
        "which one scene to scene rather than repeating the same one every "
        "time), text filled with project.palette.ink or accent, "
        'font_family "var(--motion-display-font)" for headlines/numbers and '
        '"var(--motion-body-font)" for body/labels — never invent a fresh '
        "hex or font mid-scene that ignores what this turn already chose.\n"
        "8. A scene may name a \"transition_in\" (see TRANSITIONS above) "
        "for how it cuts in from the one before it — pick one that fits "
        "the beat, or leave it unset and a real one is still chosen for "
        "you rather than a hard cut.\n"
        "8b. Only the artwork listed under ARTWORK YOU MAY USE exists. A "
        "screenshot, photo or device frame the list does not name must not "
        "be drawn as an empty placeholder — an empty glass_panel or an image "
        "with no src is sent back. Carry that beat's copy on a glass_panel "
        "with the text inside it instead.\n"
        "9. Before placing a text or image node, sketch its actual box — "
        "position ± roughly half its width/height — against every OTHER "
        "text/image node's box already placed in the same scene. Two photos, "
        "or a headline and a photo, sharing the same region reads as debris, "
        "not layout, no matter how good either looks alone. Give each one "
        "its own clear region of the frame (stack vertically, "
        "or split left/right) rather than centering everything on the "
        "same point. A shape_rect used as an intentional backdrop directly "
        "behind one specific node (a badge behind its own label, a card "
        "behind its own photo) is the one exception — that pairing is "
        "supposed to share a position.\n"
    )
    if skeleton == STORY_SKELETON:
        rules += (
            '10. FOCUS: mark exactly ONE node "focus": true — what the eye must land on first. Make it at '
            "least 1.5x the size on the page of anything else; everything else is quieter (smaller, lighter, "
            "or arriving later). The scene's shot goes toward it.\n"
            '11. CONTINUITY: give the node that carries the film (the name, a mark, the prompt box, the '
            'window) a stable "continuity_key", and reuse the SAME key on a node in the next scene at a '
            "pose within reach of where it left. Open each scene by continuing the last one's motion.\n"
            '12. SHOT: every scene names a "shot" next to "nodes" — push toward the focus, pull to reveal, '
            "hold to read — and the film uses at least three kinds.\n"
            "13. HYPE: the opening states a concrete claim; the build speeds up; the answer lands with "
            '"hit": true and the biggest motion of the film; then everything HOLDS. Never fill a hold with '
            "decoration.\n")
    if skeleton in ("brand_launch", "cinematic_glass"):
        rules += ('10. Give every node a "layer" (see LAYERS above) — '
                  "background and foreground are both required in every "
                  "scene.\n")
    if skeleton == "cinematic_glass":
        rules += (
            '11. Put a stable "continuity_key" on the hero/subject node; '
            "reuse the same key in later scenes when that subject returns.\n"
            "12. At least one camera or secondary motion must continue across "
            "the handoff; the incoming pose must visibly pick up the outgoing "
            "one.\n"
            "13. Build from the material primitives, not from rectangles: the "
            "background layer is a light_field (or a gradient-filled rect), the "
            "card that carries copy is a glass_panel, depth comes from "
            "depth_layer, gathered tiles are a particle_field, a lit sphere is "
            "an orb. A flat shape_rect covering more than half the frame is "
            "checked for and sent back.\n")
    rules += (
        f"READABLE: no text under {MIN_TEXT_PX} px (it is read on a phone) and the scene's largest artwork "
        f"at least {int(MIN_ART_SHARE * 100)}% of the frame's width, drawn in a box with the picture's own "
        "proportions. Scenes that break this are sent back.\n")
    rules += (
        "NEVER INVENT WHO IT IS FROM: use only the names, phone numbers, web addresses, emails and "
        "handles the client's request gives. If it names no sender or business, leave the sign-off "
        "out. Never sign a film with Prism (that is the tool, not the client). Scenes that show an "
        "invented identity or contact are sent back.\n")
    return rules


def _rulebook(skeleton: str | None) -> str:
    """The catalogue, the doctrines this profile uses and the scene rules,
    stated once, in turn one.

    scene_instructions() used to carry all of this itself: the catalogue
    and doctrines in full with scene 1 (19,800 characters with a four-
    picture asset list) and the rules again on every later scene (6,600
    each), in a tab where a run on 10 Sep 2026 had already grown the page
    until the kernel killed Chrome for memory. Every scene turn lands in
    this same tab straight after turn one, so the model has read it by
    then. Stated before the storyboard is planned, it also shapes the plan:
    a row can only name a node the model has read about.
    """
    parts = [_NODE_CATALOGUE]
    if skeleton in ("brand_launch", "cinematic_glass"):
        parts.append(_LAYER_DOCTRINE)
    if skeleton == "cinematic_glass":
        parts.append(_CINEMATIC_GLASS_DOCTRINE)
    parts.append(_scene_rules(skeleton))
    return ("THE RULEBOOK FOR EVERY SCENE. You plan the storyboard with it, "
            "and each scene you are asked for after this turn is written to "
            "it. The scene prompts name these sections instead of repeating "
            "them, so hold on to them.\n\n" + "\n\n".join(parts))


def storyboard_instructions(request: str, brand: dict | None = None,
                            skeleton: str | None = None, script: str = "",
                            look: str = "", kit: dict | None = None, has_logo: bool = False) -> str:
    """Turn one: the look, the camera's overall intent, a storyboard row
    per scene, and the rulebook every scene after it is written to (see
    _rulebook()). Mirrors core.reel_web.design_instructions()'s split.

    `brand`: colours already measured off the client's own artwork (see
    core.reel.sample_brand — the same pixel-level measurement Reel/Studio
    use, not re-implemented here). Unlike Reel's Pillow renderer, nothing
    here applies these automatically — Motion's palette is the model's own
    choice — so it's told to use them as ITS accent rather than inventing
    one, the same way Studio is told to.
    """
    if skeleton == SPINE_SKELETON:
        return copy_instructions(request, brand=brand, script=script)
    if skeleton == BLOCKS_SKELETON:
        from . import blocks as _blocks
        from ..length import requested_seconds as _req_secs
        return _blocks.plan_instructions(request, brand=brand, total_seconds=_req_secs(request))
    brand_note = ""
    if brand:
        brand_note = (
            f"\n\nThe brand colours have already been measured from the "
            f"client's own artwork: accent {brand.get('accent')}, deep "
            f"{brand.get('deep')}. Use these as the accent colour running "
            "through the piece rather than inventing your own — this is "
            "their actual brand, not a suggestion.")
    # `look`: the art direction Prism chose for this film (core.looks), so two
    # films in a row do not share one. `kit`: the client's identity beyond
    # colour (core.brandkit); `has_logo`: their mark is in the artwork list.
    from .. import brandkit as _brandkit, looks as _looks
    kit = kit or {}
    brand_note += (_looks.text(look, fonts_given=bool(kit.get("display_font") or kit.get("body_font")))
                   + _brandkit.block(kit, has_logo=has_logo))
    from ..aspect import requested_frame as _requested_frame
    # The cinematic profile's recipes are written in 1080x1920 coordinates, so it
    # keeps the vertical frame whatever was asked.
    _frame = None if skeleton in ("cinematic_glass", SPINE_SKELETON) else _requested_frame(request)
    _fw, _fh = (_frame[1], _frame[2]) if _frame else (1080, 1920)
    frame_note = ""
    if _frame:
        frame_note = (f"\n\nFRAME: the client asked for a {_frame[0]} film, {_fw}x{_fh}. Author every "
                      f"position for that frame — the x range is 0-{_fw}, the y range 0-{_fh} — and keep "
                      "copy inside its margins.")
    from . import spine as _spine
    spine_note = ""
    if skeleton == STORY_SKELETON:
        from . import story as _story
        from ..length import requested_seconds as _secs
        spine_note = "\n\n" + _story.storyboard_text(_secs(request) or 22.0)
    if skeleton == "cinematic_glass":
        n_rows = 7
        if script and script.strip():
            import re as _re
            found = len(_re.findall(r"(?im)^\s*scene\s*0?(\d+)\b", script))
            if found:
                n_rows = max(4, min(10, found))
        spine_note = "\n\n" + _spine.storyboard_text(n_rows)
    script_note = ""
    if script and script.strip():
        script_note = (
            "\n\nTHE SCRIPT — the copy stage already wrote what this piece "
            "says, beat by beat. It is the story; do not write another one:\n"
            + script.strip()[:6000]
            + "\n\nOne storyboard row per beat of that script, in its order. "
            'Each row\'s "caption" is that beat\'s on-screen line, VERBATIM — '
            "the scene will carry it as its main text and is checked "
            "against it. Rows without a caption are only allowed where the "
            "script itself has no words for that beat.")
    return (
        "You are Prism's Senior Visual Director and Motion Designer, "
        "planning a short vertical motion graphic.\n\n"
        f"WHAT THE CLIENT ASKED FOR:\n{request}"
        + brand_note + frame_note + script_note + spine_note + "\n\n"
        + _PALETTE_GUIDANCE + "\n\n"
        + _LOOK_GUIDANCE + "\n\n"
        + _rulebook(skeleton) + "\n\n"
        "This is turn one of a conversation. Right now, name the project "
        "settings, the camera's overall intent, and a STORYBOARD — one row "
        "per scene, words only, no nodes yet. Each scene's actual content "
        "is asked for on its own turn, right after this one, so it gets "
        "your full attention rather than a fraction of one reply split "
        "across everything.\n\n"
        "Reply with ONLY this JSON object, in a ```json fenced code block, "
        "nothing before or after it:\n"
        "{\n"
        f'  "project": {{"width": {_fw}, "height": {_fh}, "fps": 30, '
        '"duration": 8.0, "background": "<a hex>", "look": "paper|ink|tech",\n'
        '    "palette": {"bg_a": "...", "bg_b": "...", "ink": "...", '
        '"accent": "...", "accent2": "..."},\n'
        '    "type": {"display_font": "...", "body_font": "...", '
        '"google_fonts_url": "https://fonts.googleapis.com/css2?family=...&display=block"}\n'
        "  },\n"
        '  "camera": {"tracks": [\n'
        '    {"time": 0.0, "position": [540, 960], "zoom": 1.0},\n'
        '    {"time": 1.2, "position": [540, 900], "zoom": 1.35, '
        '"duration": 1.1, "easing": "power2.inOut"}\n'
        "  ]},\n"
        '  "storyboard": [\n'
        '    {"scene": 1, "seconds": 2.5,\n'
        '     "job": "what this scene is FOR in the argument",\n'
        f'     "beat": "{"hook" if skeleton == STORY_SKELETON else "gather"}",\n'
        '     "caption": "the on-screen words for this beat (the script\'s line, verbatim)",\n'
        '     "subject": "what the carried subject IS and what STATE it is in here",\n'
        + ('     "focus": "the ONE thing the eye must land on in this scene — the node you will mark focus: true",\n'
           '     "carry": "what carries into the next scene (a node with a continuity_key) and where it leaves from",\n'
           '     "shot": "hold|reveal|push|pull|orbit|parallax|macro|resolve — and why it fits this beat",\n'
           if skeleton == STORY_SKELETON else "")
        + '     "look": "what is on screen and how it is composed — what is '
        'big, what is a supporting label, what kind of node carries it",\n'
        '     "motion": "what moves, in what order, from where — and what '
        'is still moving when the scene hands over"}\n'
        "  ]\n"
        "}\n\n"
        + (_story_storyboard_close() if skeleton == STORY_SKELETON
           else _cinematic_glass_storyboard_close() if skeleton == "cinematic_glass"
           else _brand_launch_storyboard_close() if skeleton == "brand_launch"
           else
        "3-6 scenes, 6-15 seconds total. ONE STORYBOARD ROW PER SCENE. Give "
        "each one a different job and a different composition — several "
        "scenes that are all a centred headline over the same background "
        "is the failure this stage exists to prevent. `camera.tracks` is "
        "for the WHOLE graphic; each scene's \"shot\" (see SHOT above) "
        "carries on from it.")
        + _length_note(request)
    )


def _length_note(request: str) -> str:
    """The client's own length, when the request names one, stated after
    the profile's band so it wins. A 25 s ask used to come back as 15 s
    because only the band was ever read (study of 11 Nov 2026, M6)."""
    from ..length import requested_seconds
    secs = requested_seconds(request)
    if not secs:
        return ""
    return (f"\n\nLENGTH: the client asked for {secs:g} seconds. The rows' "
            f'"seconds" must add up to about {secs:g} — within 15% of it — '
            "whatever the band above says; scale every beat's seconds "
            "together rather than padding one scene.")


def _brand_launch_storyboard_close() -> str:
    roles = "\n".join(f'  {i + 1}. {r} — {_ROLE_BRIEF[r]}'
                       for i, r in enumerate(SCENE_ROLES))
    return (
        "EXACTLY 4 scenes, in this fixed order, 8-14 seconds total — do "
        "not add, drop, reorder or rename them:\n" + roles + "\n\n"
        '"job" for each row IS its role above, in your own words for this '
        "brand. `camera.tracks` is for the WHOLE graphic; each scene's "
        "\"shot\" (see SHOT above) carries on from it."
    )


STORY_SKELETON = "story"


def _has_art(assets_table) -> bool:
    return bool(assets_table) and any(str(k).startswith("art") for k in assets_table)


def _canvas(project) -> tuple:
    try:
        return int(project.get("width", 1080) or 1080), int(project.get("height", 1920) or 1920)
    except (AttributeError, TypeError, ValueError):
        return 1080, 1920


def _story_storyboard_close() -> str:
    return (
        "EXACTLY 5 scenes, one per part of THE STORY above, in order, 14-30 seconds total. Each "
        "row's `look` names the interface it uses (ui_input, cursor, ui_checklist, ui_window, "
        "ui_swatches, ui_pill) and what is on the page; its `motion` says what moves AND where it "
        "holds. `camera.tracks` is for the WHOLE film: one continuous canvas, a few slow moves, "
        "never a reset to the same zoom at every cut. Set project.look to paper unless the brand "
        "is dark.")


def _cinematic_glass_storyboard_close() -> str:
    return (
        "5-8 scenes, 16-30 seconds total. Design one continuous visual "
        "transformation rather than a sequence of poster frames: name the "
        "visual spine in the motion field, keep it recognisable, and state "
        "how each scene's final pose hands into the next scene's first pose. "
        "Use scene changes to reveal, transform or focus the same idea; "
        "reserve the final scene for a resolved logo/answer/CTA. `camera.tracks` "
        "is for the WHOLE graphic and should also feel continuous. In each "
        "row's `look`, name the material primitives that carry the scene "
        "(light_field, glass_panel, depth_layer, particle_field, orb, "
        "spline_tree, icon) — a scene described as rectangles and arrows "
        "will be built from rectangles and arrows."
    )


def scene_instructions(idx: int, total: int, row: dict, assets: str = "",
                       skeleton: str | None = None,
                       handoff: dict | None = None, palette: dict | None = None) -> str:
    """Ask for ONE scene's nodes. The rest of the conversation already
    knows the palette, the camera and the rulebook from turn one (see
    _rulebook()); this only needs the row.

    `skeleton="brand_launch"` pins this scene to its fixed HOOK/REVEAL/
    PROOF/SIGNOFF role. `skeleton="cinematic_glass"` adds explicit handoff
    guidance for the persistent visual spine. The layer doctrine, the
    cinematic profile and the continuity-key rules those profiles are held
    to were stated in storyboard_instructions() and are only named here.
    `handoff` is what core.motion.generate._scene_handoff() read off the
    PREVIOUS scene — how it exited — so this one can continue that motion
    or colour instead of cutting cold; None for the first scene.
    """
    job = str(row.get("job", "")).strip() or "carry the argument forward"
    caption = str(row.get("caption", "")).strip()
    subject = str(row.get("subject", "")).strip()
    beat_name = str(row.get("beat", "")).strip()
    look = str(row.get("look", "")).strip()
    motion = str(row.get("motion", "")).strip()
    try:
        seconds = float(row.get("seconds") or 3.0)
    except (TypeError, ValueError):
        seconds = 3.0
    role_header = ""
    if skeleton in ("brand_launch", "cinematic_glass", STORY_SKELETON):
        role = _scene_role(idx) if skeleton != STORY_SKELETON else ""
        role_header = ("PROFILE: CINEMATIC GLASS — one continuous visual spine.\n\n"
                       if skeleton == "cinematic_glass"
                       else "PROFILE: STORY — five parts of one continuous film.\n\n" if skeleton == STORY_SKELETON
                       else f"ROLE: {role} — {_ROLE_BRIEF[role]}\n\n")
        if handoff:
            role_header += (
                f'CONTINUING FROM THE LAST SCENE: it exited with a '
                f'"{handoff["type"]}"'
                + (f' in {handoff["fill"]}' if handoff.get("fill") else "")
                + (f' on continuity_key "{handoff["continuity_key"]}"'
                   if handoff.get("continuity_key") else "")
                + ". Open THIS scene picking that motion or colour up — "
                "reverse the exit direction, or carry the colour into "
                "this scene's foreground — rather than starting cold.\n"
                + ((f'That subject settled at position {handoff.get("position")} '
                    f'and scale {handoff.get("scale")}. Give this scene\'s node '
                    f'with continuity_key "{handoff["continuity_key"]}" a '
                    "settled position and scale within reach of that — the "
                    "cut will morph between the two poses; a pose across the "
                    "frame or many times larger cannot be bridged and will "
                    "be sent back.\n")
                   if handoff.get("continuity_key") else "")
                + "\n"
            )
    # The catalogue, the doctrines and the scene rules used to travel with
    # this prompt — in full with scene 1, the rules again on every turn —
    # and are stated once now, in turn one (see _rulebook() for the run that
    # made that matter). This names them and keeps what belongs to this
    # scene alone: its row, its role or profile, the handoff from the scene
    # before, the node names, its artwork, and the shape of the reply.
    named = ("NODE TYPES, TEXT MODES, ANIMATION, SHOT, TRANSITIONS, EASING "
             "VALUES"
             + (", LAYERS, the CINEMATIC GLASS profile (its safe area "
                "included)" if skeleton == "cinematic_glass"
                else ", LAYERS" if skeleton == "brand_launch" else "")
             + " and the SCENE RULES")
    assets = assets or ""
    names = _web._asset_names(assets)
    if not assets.strip():
        artwork = ""
    elif idx == 0:
        # Turn one is written before the artwork exists and takes no asset
        # list, so scene 1 is the first place the model reads it: in full
        # once, by name after that.
        artwork = f"ARTWORK YOU MAY USE:\n{assets}\n\n"
    elif names:
        artwork = ("ARTWORK YOU MAY USE (described in scene 1's message): "
                   + ", ".join(f"asset:{n}" for n in names)
                   + ". No other name exists.\n\n")
    else:
        artwork = ("THERE ARE NO IMAGES (see scene 1's message): write no "
                   "image node.\n\n")
    return (
        f"SCENE {idx + 1} of {total}.\n\n"
        + role_header
        + f"ITS JOB: {job}\n"
        + (f'ITS CAPTION: "{caption}" — this scene\'s main text node carries '
           "exactly these words (a manual line break is fine); a scene "
           "whose text says something else is sent back.\n" if caption else "")
        + (f"ITS SUBJECT: {subject}\n" if subject else "")
        + ((f'ITS FOCUS: {row.get("focus")} — mark this node "focus": true; it is the biggest thing here and '
            "the shot goes toward it.\n" if row.get("focus") else "")
           + (f'IT CARRIES: {row.get("carry")} — a node with a stable continuity_key, reused in the next scene.\n'
              if row.get("carry") else "")
           + (f'ITS SHOT: {row.get("shot")}\n' if row.get("shot") else "")
           if skeleton == STORY_SKELETON else "")
        + (("\n" + __import__("core.motion.spine", fromlist=["recipe_text"]).recipe_text(beat_name, palette) + "\n")
           if beat_name and skeleton == "cinematic_glass" else "")
        + (("\n" + __import__("core.motion.story", fromlist=["recipe_text"]).recipe_text(beat_name) + "\n")
           if beat_name and skeleton == STORY_SKELETON else "")
        + (f"THE LOOK: {look}\n" if look else "")
        + (f"THE MOTION: {motion}\n" if motion else "")
        + f"\nDuration: {seconds:g} seconds. All this scene's animation "
        "times count from 0 at this scene's own start.\n\n"
        + "Node types: " + ", ".join(_NODE_TYPE_NAMES) + ".\n"
        + named + " are as the storyboard turn above set them out and still "
        "apply; they are not repeated here.\n\n"
        + artwork
        + "Reply with ONLY this JSON object, in a ```json fenced code "
        "block, nothing before or after it:\n"
        '{\n  "shot": {"intent": "...", "target": "<node id>"},\n'
        '  "nodes": [ /* 3-7 node objects, as NODE TYPES describes */ ]\n}\n'
        '"transition_in" may sit next to "nodes" to name how this scene cuts '
        "in (one of the TRANSITIONS)."
    )


STORYBOARD_EXPECT = ("storyboard", "project")


def _words(text: str) -> list[str]:
    import re
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def caption_faults(scene: dict, row: dict) -> list[str]:
    """The one story check a program can make: when the storyboard row
    has a caption (the script's line for this beat), some text node in
    the scene must carry those words. Compared on lowercased word runs,
    so a manual line break or a stray comma does not fail it; a scene
    that says something else — or nothing — does."""
    caption = str(row.get("caption", "")).strip() if isinstance(row, dict) else ""
    if not caption:
        return []
    want = _words(caption)
    if not want:
        return []
    texts: list[str] = []

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(str(node.get("content") or ""))
            for child in node.get("children") or []:
                visit(child)

    for node in scene.get("nodes") or []:
        visit(node)
    joined = " ".join(_words(" ".join(texts)))
    if " ".join(want) in joined:
        return []
    hit = max((sum(1 for w in want if w in _words(t)) / len(want) for t in texts), default=0.0)
    return [f'the scene\'s caption is "{caption}" but ' +
            ("no text node carries it" if hit < 0.5 else
             "its text only partly matches it") +
            ' — put the caption, verbatim, in the scene\'s main "text" node '
            "(a manual line break is fine)"]


REPAIR_ROUNDS = 2

# What each fault family needs changed — added on the second round, when a
# bare list of faults came back with the same scene.
_REPAIR_MOVES = (
    ("sits completely still", 'give the foreground node an "exit" block (time near the '
                              'end, a real x/y/opacity change) or a "secondary_motion" '
                              "wiggle — do not only re-send its enter"),
    ("overlap by", "move one of the two nodes to its own region of the frame — "
                   "stack them vertically or split left/right, and shrink the larger"),
    ("names no \"shot\"", 'add "shot": {"intent": ..., "target": ...} next to "nodes"'),
    ("caption is", 'put the caption, word for word, in the scene\'s main "text" node'),
    ("runs off the edge", "pull the node inside the frame and check its anchor"),
    ("platform's own UI", "move it below the top band, above the bottom band"),
    ("no \"secondary_motion\"", 'add "secondary_motion" to the background node'),
    ("flat shape_rect", "replace it with a light_field (background) or a glass_panel (card)"),
    ("empty glass_panel", "put the copy inside it as children, or remove it"),
    ("no \"content\"", 'put the words in "content"'),
    ("beat \"", "build the primitives and phases the beat's recipe lists — the recipe is the scene"),
    ("no \"src\"", 'use "src": "asset:<name>" from the list, or remove the image'),
)


def repair_prompt(idx: int, faults: list[str], round_no: int) -> str:
    """The message a faulty scene is sent back with. Round one lists the
    faults; round two adds the concrete change each one needs, and says
    the last correction did not fix them."""
    head = (f"Scene {idx + 1} was checked and these are wrong:\n\n" if round_no == 0 else
            f"Scene {idx + 1} still has these problems after your correction — the "
            "last reply came back with the same faults, so change the nodes "
            "named here directly:\n\n")
    lines = []
    for n, fault in enumerate(faults[:8], 1):
        line = f"{n}. {fault}"
        if round_no > 0:
            for key, move in _REPAIR_MOVES:
                if key in fault:
                    line += f"\n   → {move}"
                    break
        lines.append(line)
    return (head + "\n".join(lines)
            + "\n\nSend the corrected scene: ONLY the JSON object, same shape, "
              "in a ```json fenced block."
            + (" Keep every node that was not named." if round_no > 0 else ""))


def beat_check(scene: dict, row: dict, skeleton: str | None, prev_scene: dict | None = None,
               is_last: bool = False, has_art: bool = False,
               canvas: tuple = (1080, 1920)) -> list[str]:
    """The scene against its storyboard row's beat (core.motion.spine)."""
    if skeleton == STORY_SKELETON and isinstance(row, dict):
        from . import story as _story
        name = str(row.get("beat", "")).strip()
        out = _story.beat_faults(scene, name)
        out += _story.focus_faults(scene, row, canvas)
        out += _story.continuity_faults(scene, prev_scene, is_last)
        if name == "hook" and has_art and not any(n.get("type") == "image" for n in _story._walk(scene)):
            out.append("the artwork list has a hero image of what the business makes: show it, large, in the "
                       "hook, so a stranger can SEE what this is for")
        return out
    if skeleton != "cinematic_glass" or not isinstance(row, dict):
        return []
    from . import spine as _spine
    return _spine.beat_faults(scene, str(row.get("beat", "")).strip())


MIN_TEXT_PX = 34            # on a phone a 1080-wide frame is ~390 pt: 34 px is ~12 pt
MIN_ART_SHARE = 0.30        # the scene's largest artwork, as a share of the frame's width


def legibility_faults(scene: dict, canvas: tuple = (1080, 1920), assets_table: dict | None = None) -> list[str]:
    """Two things a viewer cannot forgive, both seen in the 13 Nov 2026 films:
    type too small to read on a phone (a 26 px "Ember & Oak" under the logo, red
    on a red glow; 12-18 px captions on a Swiss page) and the client's own mark
    drawn as a thumbnail (a 719x757 logo put in a 420x260 box, so 247 px wide in
    a 1080 frame). Read from the node, nothing measured by eye."""
    from . import story as _story
    width = (canvas or (1080, 1920))[0]
    out: list[str] = []
    best = 0.0
    for node in _story._walk(scene):
        kind = node.get("type")
        if kind == "text" and str(node.get("content") or "").strip():
            try:
                size = float(node.get("font_size", 48) or 48)
            except (TypeError, ValueError):
                continue
            if size < MIN_TEXT_PX:
                out.append(f"text {str(node.get('content')).strip()[:24]!r} is {size:g} px: too small to read on "
                           f"a phone — make it at least {MIN_TEXT_PX} px (shorten the words rather than shrink them)")
        elif kind == "image":
            src = str(node.get("src") or "")
            art = (assets_table or {}).get(src[6:]) if src.startswith("asset:") else None
            try:
                w, h = float(node.get("width") or 0), float(node.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if art and w and h and art.get("w") and art.get("h"):
                # drawn "contain": the picture keeps its own shape inside the box
                best = max(best, min(w, h * float(art["w"]) / float(art["h"])))
    if best and best < MIN_ART_SHARE * width:
        out.append(f"the client's artwork is drawn only {best:.0f} px wide in a {width} px frame: make it at least "
                   f"{int(MIN_ART_SHARE * width)} px wide (give the box the picture's own proportions, then enlarge)")
    return out


def shot_faults(scene: dict, skeleton: str | None, has_camera: bool = False) -> list[str]:
    """Under the cinematic profile every scene names a shot; a scene
    without one holds the previous camera state, which is how the second
    Alphakore run (10 Sep 2026) held one push for its last five scenes.
    A storyboard that authored its own `camera.tracks` is exempt — the
    resolver plays that curve when no scene names a shot, and asking for
    shots on top of it cost the third run two wasted repair rounds a scene."""
    if skeleton not in ("cinematic_glass", STORY_SKELETON) or scene.get("shot") or has_camera:
        return []
    return ['the scene names no "shot" — add one (see SHOT: hold, reveal, push, '
            'pull, orbit, parallax, macro, resolve) so the camera keeps moving '
            "through the film"]


def parse_storyboard(text: str) -> tuple[dict, dict, list[dict]]:
    """Turn one's reply: (project, camera, storyboard rows).

    A reply that answers whole scenes anyway is not an error — its scene
    list is a perfectly good storyboard if none was written, the same
    accommodation core.reel_web.parse_design() makes.
    """
    project: dict = {}
    camera: dict = {}
    board: list[dict] = []
    for got in _web._json_objects(text):
        p = got.get("project")
        if isinstance(p, dict) and not project:
            project = p
        c = got.get("camera")
        if isinstance(c, dict) and not camera:
            camera = c
        rows = got.get("storyboard")
        if isinstance(rows, list) and not board:
            board = [r for r in rows if isinstance(r, dict)]
        if not board and isinstance(got.get("scenes"), list):
            # Kept as a last resort — but flagged, so build_spec() asks for
            # the real storyboard once before building on rows with no job,
            # no caption and no subject (the Alphakore run built seven
            # unconnected scenes on exactly such rows).
            board = [{"job": "", "look": "", "motion": "", "_from_scenes": True,
                      "seconds": s.get("duration")}
                     for s in got["scenes"] if isinstance(s, dict)]
        if project and board:
            break
    if not project:
        raise MotionValidationError(
            "The storyboard stage returned no project settings.")
    return project, camera, board


def _normalise_nodes(nodes: list) -> None:
    """The writer's near-misses mapped to the keys the renderer reads (schema
    .normalize_aliases), children included, BEFORE the scene is checked.

    The first live run of 13 Nov 2026 wrote "secondary_motion" beside
    "animation" instead of inside it, and "text" for "content", on every scene.
    The renderer accepts both, so nothing was wrong on screen; the checks read
    the raw reply and sent each scene back twice for a missing secondary_motion
    it already had. The model re-sent the same node both times: six wasted
    turns in three scenes. A check must read what will be played."""
    from .schema import normalize_aliases
    for node in nodes:
        if not isinstance(node, dict):
            continue
        try:
            normalize_aliases(node)
        except Exception:                                       # noqa: BLE001
            pass                  # validation reports a malformed node later
        if isinstance(node.get("children"), list):
            _normalise_nodes(node["children"])


def parse_scene(text: str) -> dict | None:
    """One scene's reply: {"nodes": [...]} plus, when the reply carried
    them, the scene-level "shot" and "transition_in" (schema.py validates
    both; anything else on the scene is the loop's to set). None if
    unusable."""
    for got in _web._json_objects(text):
        if isinstance(got.get("scenes"), list) and got["scenes"]:
            inner = got["scenes"][0]
            if isinstance(inner, dict) and isinstance(inner.get("nodes"), list):
                got = inner
        nodes = got.get("nodes")
        if isinstance(nodes, list) and nodes:
            scene = {"nodes": [n for n in nodes if isinstance(n, dict)]}
            _normalise_nodes(scene["nodes"])
            for key in ("shot", "transition_in"):
                if got.get(key) is not None:
                    scene[key] = got[key]
            return scene
    return None


def _scene_count(text: str) -> int:
    """How many scenes-with-nodes a reply actually contained — same
    over-eager-reply detector as core.reel_web._scene_count()."""
    n = 0
    for got in _web._json_objects(text):
        rows = got.get("scenes")
        if isinstance(rows, list):
            n += sum(1 for s in rows
                      if isinstance(s, dict) and isinstance(s.get("nodes"), list))
    return n or (1 if parse_scene(text) else 0)


def fallback_scene(row: dict, project: dict | None = None) -> dict:
    """A plain, code-authored scene for when this one's reply can't be
    recovered — mirrors core.reel_web.fallback_scene(): a graphic with one
    dull scene ships, one with a hole in it does not.

    It shows the row's CAPTION (the words meant for the screen), wrapped and
    sized to fit the frame, in the palette's ink when the page is light. The
    live run of 13 Nov 2026 showed what it did before: the row's "job" (a
    note to the designer) as one 64 px line 3,300 px wide, near-white on a
    light page. The job is used only when the row has no caption at all."""
    import textwrap
    project = project if isinstance(project, dict) else {}
    width, height = _canvas(project)
    words = str(row.get("caption", "")).strip() or str(row.get("job", "")).strip() or "…"
    lines = textwrap.wrap(words, 20) or [words]
    longest = max(len(ln) for ln in lines)
    size = int(max(36, min(width * 0.085, width * 0.84 / (max(longest, 1) * 0.55))))
    palette = project.get("palette") if isinstance(project.get("palette"), dict) else {}
    from .inspect import _luma
    page = _luma(str(project.get("background") or palette.get("bg_a") or ""))
    light = page is not None and page >= 140
    fill = str(palette.get("ink") or "#111111") if light else "#F8FAFC"
    if light and (_luma(fill) or 0) >= 140:
        fill = "#111111"
    return {
        "nodes": [{
            "type": "text", "content": "\n".join(lines), "position": [width // 2, height // 2],
            "font_size": size, "font_weight": 700, "fill": fill,
            "mode": "word_stagger",
            "animation": {"enter": {"type": "fade_in", "duration": 0.6,
                                     "easing": "easeOutCubic"}},
        }],
    }


def _pose_lines(scene: dict, label: str) -> list[str]:
    """One line per keyed node in `scene`, for a repair prompt."""
    out = []
    for node in scene.get("nodes", []) or []:
        if isinstance(node, dict) and node.get("continuity_key"):
            out.append(
                f'{label} carries continuity_key "{node["continuity_key"]}" on '
                f'node "{node.get("id", "")}" at position '
                f'{list(node.get("position") or [0, 0])}, scale '
                f'{list(node.get("scale") or [1, 1])}.')
    return out


def _repair_continuity(scenes: list[dict], project: dict, camera: dict,
                       ask: Callable[..., str], say, check=None) -> None:
    """Whole-piece check the per-scene `check` cannot do: every handoff
    is planned by core.motion.continuity and each scene that breaks the
    contract is sent back ONCE with the exact fault and the neighbouring
    poses it must meet. A returned scene is kept only if the piece as a
    whole has fewer continuity issues afterwards and the scene itself is
    no worse on the per-scene check — same "a fix must actually be a fix"
    rule build_spec() applies per scene.

    In place on `scenes`. Never raises: the resolver compiles whatever is
    left and render.py refuses only what still cannot be bridged.
    """
    from . import continuity as _continuity

    def probe(rows: list[dict]) -> list[dict]:
        rep = _continuity.report({"project": project, "camera": camera,
                                  "scenes": rows,
                                  "_motion_profile": "cinematic_glass"})
        return ([dict(e, severity="error") for e in rep["errors"]]
                + [dict(w, severity="warning") for w in rep["warnings"]])

    issues = probe(scenes)
    if not issues:
        return
    # Only what would stop the render goes back to the model. Warnings
    # (a scene without a keyed node, an opening subject the ending does
    # not resolve) are logged and left: each extra turn in the same tab is
    # minutes of the writer's time on an already long page, and a run
    # measured on 10 Sep 2026 lost its browser session after the seventh
    # such turn. At most three scenes are sent back, worst first.
    errors = [it for it in issues if it.get("severity", "error") == "error"]
    for it in issues:
        if it not in errors:
            say(f"continuity note: {it['message']}")
    if not errors:
        return
    by_scene: dict[int, list[str]] = {}
    for it in errors:
        by_scene.setdefault(int(it["scene_index"]), []).append(it["message"])
    say(f"continuity: {len(errors)} problem(s) across "
        f"{len(by_scene)} scene(s) — sending them back")
    order = sorted(by_scene, key=lambda i: -len(by_scene[i]))[:3]
    for idx in sorted(order):
        if idx < 0 or idx >= len(scenes):
            continue
        msgs = by_scene[idx]
        context = []
        if idx > 0:
            context += _pose_lines(scenes[idx - 1], f"The scene before (scene {idx})")
        if idx + 1 < len(scenes):
            context += _pose_lines(scenes[idx + 1], f"The scene after (scene {idx + 2})")
        fixed = parse_scene(ask(
            f"Scene {idx + 1} was checked against its neighbours and breaks "
            "the continuity contract:\n\n"
            + "\n".join(f"{n}. {x}" for n, x in enumerate(msgs[:6], 1))
            + ("\n\n" + "\n".join(context) if context else "")
            + f"\n\nSend the corrected scene {idx + 1}: ONLY the JSON object, "
              "same shape, in a ```json fenced block. Keep everything that "
              "was not mentioned.",
            SCENE_EXPECT) or "")
        if not fixed:
            # No JSON back usually means the tab is struggling; do not
            # spend more turns on it.
            say(f"   scene {idx + 1} never came back — keeping the first "
                "and stopping the continuity pass")
            break
        fixed["id"] = scenes[idx]["id"]
        fixed["duration"] = scenes[idx]["duration"]
        trial = list(scenes)
        trial[idx] = fixed
        after = probe(trial)
        worse_alone = False
        if check:
            try:
                before_n = len(check({"project": project, "camera": camera,
                                      "scenes": [scenes[idx]]}))
                after_n = len(check({"project": project, "camera": camera,
                                     "scenes": [fixed]}))
                worse_alone = after_n > before_n
            except Exception:
                worse_alone = False
        after_errors = [it for it in after if it.get("severity", "error") == "error"]
        if len(after_errors) < len(errors) and not worse_alone:
            scenes[idx] = fixed
            say(f"   scene {idx + 1} fixed — {len(errors)} down to {len(after_errors)}")
            errors = after_errors
        else:
            say(f"   scene {idx + 1}'s correction was no better — keeping the first")


def _repair_rhythm(scenes: list[dict], project: dict, camera: dict, ask: Callable[..., str],
                   say, check, measure) -> None:
    """Film-level pass the per-scene checks cannot make: the assembled film is
    played at low resolution and its rhythm measured (core.motion.rhythm). A film
    that never rests, or never moves, sends its worst scenes back once each with
    the measured numbers. A returned scene is kept only when the whole film then
    lands clearly nearer the 15-45 % still-frame band and the scene is no worse on
    the per-scene check. In place on `scenes`. Never raises: a missing browser or
    a model that does not answer leaves the film as written."""
    from . import rhythm as _rhythm

    def spec_of(rows):
        out = {"project": dict(project), "scenes": rows}
        if camera:
            out["camera"] = camera
        return out

    try:
        profile = measure(spec_of(scenes))
    except Exception as e:
        say(f"rhythm: could not be measured ({e})")
        return
    what = _rhythm.kind(profile)
    if not what:
        return
    durations = [float(s.get("duration") or 0.0) for s in scenes]
    for idx in _rhythm.targets(profile, durations, what):
        activity = _rhythm.scene_activity(profile, durations)[idx]
        say(f"rhythm: {profile['static_pct']:.0f}% of frames are still — "
            f"{'sending the busiest' if what == 'never_rests' else 'sending a quiet'} scene {idx + 1} back")
        fixed = parse_scene(ask(
            _rhythm.repair_text(idx + 1, what, profile, activity)
            + f"\n\nSend the corrected scene {idx + 1}: ONLY the JSON object, same shape, in a ```json fenced "
              "block. Keep its words, layout, continuity_key and everything not mentioned.",
            SCENE_EXPECT) or "")
        if not fixed:
            say(f"   scene {idx + 1} never came back — keeping the first and stopping the rhythm pass")
            break
        fixed["id"] = scenes[idx]["id"]
        fixed["duration"] = scenes[idx]["duration"]
        trial = list(scenes)
        trial[idx] = fixed
        try:
            after = measure(spec_of(trial))
            worse_alone = bool(check) and len(check({"project": project, "camera": camera, "scenes": [fixed]})) \
                > len(check({"project": project, "camera": camera, "scenes": [scenes[idx]]}))
        except Exception:
            after, worse_alone = None, True
        if _rhythm.better(profile, after) and not worse_alone:
            scenes[idx] = fixed
            say(f"   scene {idx + 1} fixed — {profile['static_pct']:.0f}% still became {after['static_pct']:.0f}%")
            profile = after
            if not _rhythm.kind(profile):
                break
        else:
            say(f"   scene {idx + 1}'s correction was no better — keeping the first")


def build_blocks(first_reply: str, ask: Callable[..., str], assets_table: dict | None = None, check=None,
                 log=None, on_scene=None, request: str = "", measure=None) -> dict:
    """The block path: parse the plan (sending it back once for each fact that is wrong
    with it), compose the film from the library in one pass, and return the spec. Never
    asks for a scene: the layout, type scale, timing and hold are the blocks' own."""
    from . import blocks as _blocks
    from . import grounding as _grounding

    def say(msg):
        if log:
            log(msg)

    project, plan = _blocks.parse_plan(first_reply)
    corpus = _grounding.allowed_corpus(request) if request else ""

    def faults_of(rows):
        out = _blocks.plan_faults(rows)
        for i, row in enumerate(rows):
            texts = []
            for value in row["slots"].values():
                texts.extend(value if isinstance(value, list) else [value])
            probe = {"nodes": [{"type": "text", "content": str(t)} for t in texts]}
            out += [f"block {i + 1}: {f}" for f in _grounding.grounding_faults(probe, corpus)]
        return out

    for round_no in range(2):
        faults = faults_of(plan)
        if not faults:
            break
        say(f"the plan has {len(faults)} problem(s) — sending them back")
        again = ask("The plan was checked and these are wrong:\n\n" + "\n".join(f"{n}. {x}" for n, x in enumerate(faults[:8], 1))
                    + '\n\nSend the corrected plan: ONLY the JSON object with "project" and "plan", in a ```json fenced block.',
                    '"plan"') or ""
        project2, plan2 = _blocks.parse_plan(again)
        if plan2 and len(faults_of(plan2)) < len(faults):
            project, plan = project2 or project, plan2
        else:
            say("   the correction was no better — keeping the first")
            break
    if not plan:
        raise MotionValidationError("The plan turn returned no usable blocks.")
    if on_scene:
        try:
            on_scene(0, 1)
        except Exception:                                    # noqa: BLE001
            pass
    spec = _blocks.compose_plan(project, plan)
    if assets_table:
        spec["_assets"] = assets_table
    say(f"{len(spec['scenes'])} scene(s) composed from the block library, {spec['project']['duration']:g}s")
    return spec


def _aim_shots_at_centres(scenes: list, canvas: tuple = (1080, 1920)) -> None:
    """A shot that names a node as its target is aimed at the node's `position`,
    which is its anchor point, not its middle (camera.compile_tracks). The Swiss
    look sets every node's anchor to [0, 0] (a grid is left-aligned), so a "push"
    on the closing logo aimed at its top-left corner and the finished film ended
    with the logo and the name cut off at the frame's right and bottom edges
    (13 Nov 2026, round 4: logo box at [72, 560], 720x400, drawn at x 768-1080+).
    The target becomes the centre of the node's box, as coordinates, for any
    top-level node whose anchor is not its centre. In place."""
    from .inspect import _node_bounds
    width, height = canvas
    for scene in scenes:
        shot = scene.get("shot") if isinstance(scene, dict) else None
        target = shot.get("target") if isinstance(shot, dict) else None
        if not (isinstance(target, str) and target):
            continue
        node = next((n for n in scene.get("nodes", []) if isinstance(n, dict) and n.get("id") == target), None)
        anchor = (node or {}).get("anchor")
        if not node or not isinstance(anchor, (list, tuple)) or tuple(anchor) == (0.5, 0.5):
            continue
        try:
            box = _node_bounds(node, width, height)
        except Exception:                                       # noqa: BLE001
            box = None
        if box:
            shot["target"] = [round((box[0] + box[2]) / 2, 1), round((box[1] + box[3]) / 2, 1)]


def _flat_ink_backdrop(spec: dict) -> None:
    """A film in the "ink" look is dark and flat; the runtime's ink preset
    paints a faint white spotlight, and the canvas gradient it is drawn with
    fades from that white to the page colour through grey. Measured 13 Nov 2026
    on a #0C0808 film: corners and edges rendered (42,40,41)-(53,50,54), a grey
    wash round a dark disc on every frame, and with the spotlight set to "none"
    the same frames were (10,7,7) everywhere. Until runtime/effects.js fades to
    a transparent copy of the page colour, an ink film says "none" itself. Any
    backdrop value the writer set is left alone."""
    project = spec.get("project") if isinstance(spec.get("project"), dict) else {}
    visual = spec.get("visual") if isinstance(spec.get("visual"), dict) else {}
    if project.get("look") != "ink" or "spotlight" in visual:
        return
    spec["visual"] = dict(visual, spotlight={"color": "none", "secondary": "none"})


BLOCKS_SKELETON = "blocks"
SPINE_SKELETON = "spine"
COPY_EXPECT = '"copy"'


def copy_instructions(request: str, brand: dict | None = None, script: str = "") -> str:
    """Turn one of the composed path: the model writes the WORDS for the
    reference's grammar — every slot the composer has, with its width —
    and the palette. No storyboard, no scenes: the structure is
    core.motion.compose's, and three generated reels (10-11 Sep 2026)
    showed a model does not build the transformation however it is asked."""
    from . import compose as _compose
    brand_note = ""
    if brand:
        brand_note = (
            f"\n\nThe brand colours were measured from the client's own artwork: "
            f"accent {brand.get('accent')}, deep {brand.get('deep')}. Use the accent "
            'as "accent" in the palette below.')
    script_note = ""
    if script and script.strip():
        script_note = (
            "\n\nTHE SCRIPT — the copy stage already wrote what this piece says. "
            "Take the words from it; do not write another story:\n"
            + script.strip()[:6000])
    return (
        "You are Prism's copywriter for a short vertical motion graphic. The "
        "film's structure is already designed and built — a signal of tiles "
        "gathers, streams into a hub, a bright sky sweeps in with a chat "
        "exchange, cards on a tree, a knowledge card, a ring with a fact, an "
        "orb, then the brand mark, tagline and call to action. Your job is "
        "ONLY the words that go into its slots, fitted to the widths given.\n\n"
        f"WHAT THE CLIENT ASKED FOR:\n{request}" + brand_note + script_note
        + "\n\nReply with ONLY this JSON object, in a ```json fenced code block, "
        "nothing before or after it:\n"
        "{\n"
        '  "project": {"palette": {"accent": "#hex — the brand accent", "accent2": "#hex — a second accent"}},\n'
        '  "copy": ' + _compose.copy_slots_text().replace("\n", "\n  ") + "\n"
        "}\n\n"
        "RULES: every slot filled, in the brand's own voice, taken from the "
        "script where the script has the line; character limits are hard "
        "(what does not fit is cut); plain text only — no markdown, no "
        "emoji; the three bubbles read as one real exchange with a customer "
        "(reply, question, answer); \"fact\" is a number or two words; the "
        '"captions" are optional one-liners for beats without their own words.'
    )


def parse_copy(text: str) -> tuple[dict, dict]:
    """The copy turn's reply: (project, copy). Either may be empty."""
    project: dict = {}
    copy: dict = {}
    for got in _web._json_objects(text):
        p = got.get("project")
        if isinstance(p, dict) and not project:
            project = p
        c = got.get("copy")
        if isinstance(c, dict) and not copy:
            copy = c
        elif not copy and isinstance(got.get("brand"), str):
            copy = got                                  # the slots at the top level
        if project and copy:
            break
    return project, copy


def _logo_from(assets_table: dict | None) -> dict | None:
    """The client's own mark from the asset table, for the resolve beat —
    a "logo" kind first, else the first transparent artwork."""
    if not assets_table:
        return None
    ranked = sorted(assets_table.items(),
                    key=lambda kv: (0 if kv[1].get("kind") == "logo" else 1,
                                    0 if kv[1].get("alpha") else 1))
    name, a = ranked[0]
    return {"src": f"asset:{name}", "w": a.get("w"), "h": a.get("h")}


def build_composed(first_reply: str, ask: Callable[..., str], assets_table: dict | None = None,
                   check=None, log=None, on_scene=None) -> dict:
    """The composed path: parse the copy (asking once more if none came),
    compose the reference's grammar around it, report what the inspector
    sees, and return the spec. Never asks for a scene."""
    from . import compose as _compose

    def say(msg):
        if log:
            log(msg)

    project, copy = parse_copy(first_reply)
    if not copy:
        say("turn one carried no copy — asking for the slots again")
        again = ask(
            'Send ONLY the JSON object with "project" (its "palette") and "copy" — '
            "every slot listed, fitted to its width — in a ```json fenced block.",
            COPY_EXPECT) or ""
        project2, copy = parse_copy(again)
        project = project or project2
    if not copy:
        raise MotionValidationError("The copy turn returned no words to compose with.")
    if on_scene:
        try:
            on_scene(0, 1)
        except Exception:                        # noqa: BLE001
            pass
    palette = project.get("palette") if isinstance(project.get("palette"), dict) else {}
    try:
        fps = int(float(project.get("fps") or 30))
    except (TypeError, ValueError):
        fps = 30
    spec = _compose.compose(copy, width=1080, height=1920, fps=max(12, min(60, fps)),
                            logo=_logo_from(assets_table), palette=palette)
    spec["_copy"] = copy
    if assets_table:
        spec["_assets"] = assets_table
    if check:
        try:
            faults = check(spec)
        except Exception as e:                   # noqa: BLE001
            faults = [f"couldn't inspect the composed piece ({e})"]
        if faults:
            say(f"composed {len(spec['scenes'])} scenes; the inspector notes "
                f"{len(faults)} thing(s): " + "; ".join(str(f) for f in faults[:4]))
        else:
            say(f"composed {len(spec['scenes'])} scenes — nothing to send back")
    return spec


def build_spec(first_reply: str, ask: Callable[..., str], assets: str = "",
               assets_table: dict | None = None,
               check=None, log=None, should_stop=None, on_scene=None,
               skeleton: str | None = None, request: str = "", measure=None, critic=None) -> dict:
    """Run the rest of the design conversation and return the finished spec.

    `measure(spec) -> rhythm profile | None`, when given, turns on the film-level
    rhythm repair (see _repair_rhythm). Injected for the same reason `check` is.

    `critic(spec) -> list[defect] | None`, when given, has a vision model look at
    the finished frames; serious defects send their scene back (core.motion.critic.repair).

    `request` is what the client asked for. When given, the words a scene
    shows are checked against it (core.motion.grounding): a sender, phone
    number, web address, email or handle the client never gave is sent back.

    `ask(prompt, expect) -> str` sends a follow-up in the tab turn one is
    already sitting in. `check(spec) -> list[str]` reports concrete faults
    for a one-scene spec. Both are injected rather than imported, so this
    can be exercised without a browser — see core.reel_web.build_spec()'s
    docstring, which this mirrors exactly. `on_scene(index, total)` fires
    before each scene is asked for, for the same reason: this loop takes
    minutes, and nothing here raises once turn one has parsed.

    `assets` is the text description (core.assets.manifest()'s output)
    telling the model what `asset:<name>` it may put in an "image" node's
    "src". `assets_table` is the real {name: {"path": ...}} table behind
    those names — stashed on the returned spec as `_assets` (same
    convention core.reel_web uses) so resolve_motion_spec() can swap each
    `asset:name` for the real file before anything tries to render it.

    `skeleton="brand_launch"` or `skeleton="cinematic_glass"` must match what storyboard_instructions() was
    called with for `first_reply` — it switches each scene's prompt to the
    layer doctrine and threads each finished scene's exit into the next
    scene's prompt as a handoff so consecutive cuts continue a motion instead
    of resetting. The cinematic profile also records `_motion_profile` on the
    assembled spec.
    """
    def say(msg):
        if log:
            log(msg)

    if skeleton == SPINE_SKELETON:
        return build_composed(first_reply, ask, assets_table=assets_table, check=check,
                              log=log, on_scene=on_scene)
    if skeleton == BLOCKS_SKELETON:
        return build_blocks(first_reply, ask, assets_table=assets_table, check=check, log=log,
                            on_scene=on_scene, request=request, measure=measure)

    from . import grounding as _grounding
    _corpus = _grounding.allowed_corpus(request, assets, list((assets_table or {}).keys())) if request else ""

    project, camera, board = parse_storyboard(first_reply)
    if board and all(r.get("_from_scenes") for r in board):
        # Turn one answered whole scenes instead of a storyboard. Those
        # scenes carry no job, caption or subject — building on them is
        # how a piece ends up with no connecting story. Ask once for the
        # storyboard itself; fall back to the scene list only if that
        # fails too.
        say("turn one answered scenes, not a storyboard — asking for the "
            "storyboard itself")
        again = ask(
            "That reply built the scenes. Before any scene is built, send "
            "the STORYBOARD only: the same JSON object with \"project\", "
            "\"camera\" and a \"storyboard\" list — one row per scene "
            "with \"seconds\", \"job\", \"caption\", \"subject\", "
            "\"look\" and \"motion\" — and NO \"scenes\" key. The "
            "scenes are asked for one at a time after this.",
            STORYBOARD_EXPECT) or ""
        try:
            project2, camera2, board2 = parse_storyboard(again)
        except MotionValidationError:
            project2, camera2, board2 = project, camera, []
        if board2 and not all(r.get("_from_scenes") for r in board2):
            project, camera, board = project2 or project, camera2 or camera, board2
        else:
            say("   no storyboard came back — building on the scene list")
    total = len(board)
    if not total:
        raise MotionValidationError(
            "The storyboard names no scenes — there is nothing to build.")
    if skeleton == "cinematic_glass":
        from . import spine as _spine
        names = _spine.beats_for(total)
        for i, row in enumerate(board):
            if not _spine.beat(str(row.get("beat", "")).strip()):
                row["beat"] = names[i]

    say(f"storyboard: {total} scene(s) — writing them one at a time")
    scenes: list[dict] = []
    overflow_notice = ""
    handoff: dict | None = None
    for i in range(total):
        if should_stop and should_stop():
            say("stopped — keeping the scenes written so far")
            break
        if on_scene:
            try:
                on_scene(i, total)
            except Exception:                        # noqa: BLE001
                pass          # a progress listener must never fail the run
        prompt = overflow_notice + scene_instructions(
            i, total, board[i], assets, skeleton=skeleton, handoff=handoff,
            palette=project.get("palette") if isinstance(project.get("palette"), dict) else None)
        overflow_notice = ""
        raw = ask(prompt, SCENE_EXPECT) or ""
        scene = parse_scene(raw)
        if scene is None:
            scene = parse_scene(ask(
                f"Send scene {i + 1} again as JSON only — first character "
                "'{', last '}', key \"nodes\", wrapped in a ```json fenced "
                "block. Nothing before or after.",
                SCENE_EXPECT) or "")
        elif _scene_count(raw) > 1:
            n = _scene_count(raw)
            say(f"scene {i + 1} came back with {n} scenes in it — only the "
                "first was kept; telling it to slow down before the next ask")
            overflow_notice = (
                f"Before the next scene: that last reply answered {n} "
                f"scenes at once. Only scene {i + 1} was kept — the rest "
                "were discarded, not saved for later, so nothing from them "
                "will appear in the motion graphic. From here on, answer "
                "EXACTLY ONE scene per reply, the one actually asked "
                "for.\n\n")
        if scene is None:
            say(f"scene {i + 1} never came back as JSON — using a plain one")
            scene = fallback_scene(board[i], project)

        try:
            seconds = float(board[i].get("seconds") or 3.0)
        except (TypeError, ValueError):
            seconds = 3.0
        scene["id"] = f"scene_{i}"
        scene["duration"] = seconds

        if check:
            try:
                faults = check({"project": project, "camera": camera,
                                 "scenes": [scene]})
            except Exception as e:
                say(f"couldn't check scene {i + 1} ({e})")
                faults = []
            faults = (caption_faults(scene, board[i])
                      + legibility_faults(scene, _canvas(project), assets_table)
                      + _grounding.grounding_faults(scene, _corpus)
                      + beat_check(scene, board[i], skeleton, scenes[-1] if scenes else None, i == total - 1,
                                   _has_art(assets_table), _canvas(project))
                      + shot_faults(scene, skeleton, bool((camera or {}).get("tracks"))) + faults)
            if faults:
                say(f"scene {i + 1} has {len(faults)} problem(s) — "
                    "sending them back")
            # Up to two rounds. The second names the change each fault
            # needs (the second Alphakore run's "held slide" faults came
            # back unchanged after a single, unguided round). A correction
            # is kept only if genuinely cleaner — same rule as reel_web: a
            # "fix" trading four faults for five is not a fix.
            for round_no in range(REPAIR_ROUNDS):
                if not faults:
                    break
                fixed = parse_scene(ask(
                    repair_prompt(i, faults, round_no), SCENE_EXPECT) or "")
                if not fixed:
                    break
                fixed["id"] = scene["id"]
                fixed["duration"] = scene["duration"]
                try:
                    left = check({"project": project, "camera": camera,
                                   "scenes": [fixed]})
                except Exception:
                    left = []
                left = (caption_faults(fixed, board[i])
                        + legibility_faults(fixed, _canvas(project), assets_table)
                        + _grounding.grounding_faults(fixed, _corpus)
                        + beat_check(fixed, board[i], skeleton, scenes[-1] if scenes else None, i == total - 1,
                                     _has_art(assets_table), _canvas(project))
                        + shot_faults(fixed, skeleton, bool((camera or {}).get("tracks"))) + left)
                if len(left) < len(faults):
                    scene = fixed
                    say(f"   fixed — {len(faults)} down to {len(left)}")
                    faults = left
                else:
                    say("   the correction was no better — keeping the first"
                        + (" and saying what to change" if round_no + 1 < REPAIR_ROUNDS else ""))
        scenes.append(scene)
        if skeleton in ("brand_launch", "cinematic_glass", STORY_SKELETON):
            handoff = _scene_handoff(scene) or handoff
        say(f"scene {i + 1}/{total} written — "
            f"{len(scene.get('nodes', []))} node(s)")

    if not scenes:
        raise MotionValidationError("No scenes were written.")
    if skeleton in ("cinematic_glass", STORY_SKELETON) and len(scenes) > 1:
        _repair_continuity(scenes, project, camera, ask, say, check)
    if skeleton == STORY_SKELETON:
        from . import story as _story
        for _f in _story.direction_faults(scenes):
            say(f"   direction: {_f}")
    if measure and len(scenes) > 1:
        _repair_rhythm(scenes, project, camera, ask, say, check, measure)
    if critic:
        from . import critic as _critic

        def _spec_of(rows):
            out = {"project": dict(project), "scenes": rows}
            if camera:
                out["camera"] = camera
            return out

        def _keep(old, new):
            new["id"], new["duration"] = old["id"], old["duration"]

        _critic.repair(scenes, _spec_of, ask, parse_scene, _keep, say, critic, SCENE_EXPECT)
    spec: dict[str, Any] = {"project": project, "scenes": scenes}
    _aim_shots_at_centres(scenes, _canvas(project))
    _flat_ink_backdrop(spec)
    if camera:
        spec["camera"] = camera
    if assets_table:
        spec["_assets"] = assets_table
    if skeleton:
        spec["_motion_profile"] = skeleton
    return spec
