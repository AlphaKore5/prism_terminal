# HVAC drawing rules — what `core.boq.measure()` has learned the hard way

This file exists because there will be more than one of these. A client
hands over a real HVAC drawing, Prism's offline measurement gets a number
wrong, and the fix has to be a **general rule about how HVAC drawings are
actually drawn** — taught into the measuring code itself — not a patch for
that one file. This file is where that rule gets written down, every time,
so the next person (or the next AI session) touching `boq.py` reads what
real drawings have already taught it, instead of relearning it from
scratch, or silently regressing it.

## The process, every time this happens again

1. **Get the client's real test file, and a trustworthy ground truth for
   it.** Not Prism's own output compared against itself — an independent
   reading (a second measurement, a qualified person, or in a pinch an AI
   given the actual drawing directly, the way rule 1 below was confirmed).
2. **Run it through `core.boq.measure()` and compare, line by line.** Don't
   stop at "the totals look different" — find the exact item, the exact
   layer, the exact wrong number.
3. **Open the raw DXF and look at the actual entity coordinates** (ezdxf,
   directly — `msp.query(...)`, print every start/end point) for whatever
   layer is wrong. Don't guess the cause from the symptom; read the
   geometry that produced it.
4. **Name the general drawing CONVENTION responsible**, not the specific
   file. "This duct is drawn as two parallel lines" is a convention; "this
   one file has a wrong number" is not. The fix only has value if it
   generalises to every other drawing using the same convention.
5. **Fix `core/boq.py` itself** so the convention is handled correctly,
   never an AI-side patch, never a one-off special case keyed to a
   filename or a specific layer name — this module's whole reason to exist
   is deterministic, auditable measurement; an AI guess is exactly the
   thing it's built to avoid needing.
6. **Write the rule here**, in the same shape rule 1 below uses: what the
   convention is, how to recognise it, what broke, what the fix actually
   does, and — importantly — what it does NOT handle yet (the next false
   positive or false negative is usually hiding in the "not yet" list).
7. **Add a regression test built from the real geometry**, not kept as the
   client's actual file in the repo — reconstruct the coordinates as a
   synthetic ezdxf fixture (see `tests/test_double_line_ducts.py`) so the
   exact bug can never come back silently, without needing to keep a
   client's file around.
8. **Re-run every rule's tests together, not just the new one.** A fix for
   rule N can easily reintroduce a false positive rule N-1 already closed
   — rule 1's own build caught exactly this against a room's walls before
   it ever shipped (see rule 1, "what it does NOT handle").

If a client ever sends several files and several bugs turn up at once:
work them one at a time, through this same process, in full, before moving
to the next — don't batch fixes together without a rule and a test each,
or the next regression has no paper trail pointing at which fix broke it.

---

## Rule 1 — a duct/pipe/tray is very often drawn as its two edges, not one centreline

**Found:** 22 Oct 2026, a real client HVAC test file (`HVAC_BOQ_TEST`,
outside this repo). Prism measured the supply duct at 78.82 m. The real
route is 38.80 m — confirmed by giving an AI the actual drawing directly
and having it measure independently, which is itself notable: the only
reason that number came out right is that the drawing left Prism's
offline, AI-never-sees-it path. That gap is what rule 1 closes.

**The convention:** professional HVAC (and often pipe/cable-tray) drawings
usually show a duct run as **two parallel lines, the duct's real width
apart**, not a single centreline — because the drawing is meant to be read
to scale. A short perpendicular line at each open end closes the
rectangle visually. This is normal, correct, everyday drafting practice,
not an unusual file.

**What broke:** `core.boq.measure()` summed the length of *every* LINE on
a layer, with no concept of "these two lines are the same run's two
edges." It counted both edges as if they were two separate ducts — almost
exactly double the real length, plus the four short closing stubs pushing
it a little further still.

**The fix — `_collapse_duct_pairs()` in `core/boq.py`:** for each layer,
before summing lengths, look for pairs of LINE segments that are:
  - roughly **parallel** (their directions nearly collinear, either sense),
  - roughly the **same length** (within 8%),
  - a **small, constant perpendicular offset apart** — specifically
    *less than half the length of the longer one*. This ratio is the one
    number in the whole rule most likely to need revisiting; see below.

A matched pair contributes **one** length (the average of the two) to the
layer's total, not two. A short line left over that is roughly
perpendicular to a matched pair and about as long as that pair's own
offset is treated as the closing stub and dropped entirely — it is not
route length. Anything that doesn't match stays exactly as it always
measured: an ordinary single-line (centreline/schematic) run is untouched.

**What it does NOT handle yet (read before changing the threshold):**
  - **Caught before shipping, not after:** a 24 m × 14 m room's opposite
    walls are a similar length and a similar distance apart too — the
    offset/length ratio came out at 0.58, and the first version of this
    fix (threshold 0.6) read the whole room as one 24 m duct. Tightened to
    0.5 specifically because no genuine duct/pipe edge pair in the real
    test file exceeded 0.44 — there is real margin here, but not a lot,
    and a sufficiently short, stubby duct transition piece could still
    fall on the wrong side of 0.5 and simply stay un-collapsed (the
    original, smaller kind of error — a partial overcount, not a silent
    wrong halving). See `tests/test_double_line_ducts.py::
    ARoomsOppositeWallsAreNeverReadAsOneDuct` for the exact case that
    caught this.
  - **Two separate same-length parallel runs on the SAME layer** (e.g. a
    flow and a return line both left un-separated onto one layer, instead
    of the two different layers this drawing correctly used) would still
    be wrongly paired — the function only ever compares lines within one
    layer, which is safe as long as the drawing itself keeps services on
    separate layers the way this one did. If a client file is ever found
    that mixes two genuinely separate runs onto one layer, that is the
    next rule to write here, not a reason to loosen this one.
  - **LWPOLYLINE double-lines are not covered.** This fix only collapses
    plain LINE entities. A duct drawn as two LWPOLYLINE edges (common for
    a curved or multi-segment run) would still double-count. If a client
    file turns up one, extend `_collapse_duct_pairs` (or a sibling
    function) to work on LWPOLYLINE segments the same way, rather than
    writing a second, divergent algorithm.
  - **A converging/tapering transition (a reducer)** is correctly left
    un-paired (its two sides are not parallel) — rule 2, below, now counts
    it once rather than twice.
  - **The visible audit trail was a plain-text note, no stronger signal.**
    23 Oct 2026: the BOQ add-on's measured-quantities panel now also
    tints every row green/yellow/red by how much it should be trusted,
    with a dedicated Trust column (a solid dot, not just a pale
    background) — see `addons/boq/measured.py:severity_for()`. The plain-
    text note in `q["notes"]` is still the only thing that reaches the
    exported CSV, which still matters once the live screen is closed.

---

## Rule 2 — a tapering transition (a reducer) counts once, not twice

**Found:** 23 Oct 2026, re-verifying rule 1's own fix against a second,
independent reading of the same real file. Rule 1 correctly brought the
supply duct down from the original bug's 78.82 m to 38.32 m — but the true
route is 38.80 m, and the gap was not noise: it was the reducer.

**The convention:** a reducer/transition between two different duct widths
is drawn as two short lines that **converge** (not parallel) — correctly
left un-paired by rule 1, since rule 1 only ever matches parallel pairs.
But the original code then summed both of those converging lines in full,
which double-counts the transition by about its own length: two diagonal
edges describing ONE short piece of duct, not two.

**The fix — `_duct_runs()` in `core/boq.py`** (rule 1's own pairing logic,
refactored to also produce this): among the segments rule 1 leaves
unpaired, look for two that are:
  - roughly the **same length** (within 8%, same test rule 1 uses),
  - genuinely **converging**, not parallel (the complement of rule 1's own
    parallel test — if they were parallel, rule 1 would already own them),
  - **both ends a plausible duct width apart** (20–2000 mm) — one gap at
    the start, a *different* gap at the end, which is exactly what makes
    it a transition and not a pair,
  - **no longer than the widest confirmed pair already found on the
    layer** — the one check standing between this and mistaking an
    unrelated long diagonal duct run elsewhere on the same layer for a
    transition.

A matched transition contributes its own average length **once**, the same
as a matched pair does — not both diagonals summed.

**What it does NOT handle yet:**
  - **The transition's TRUE centreline length** (the straight-line
    distance between the two duct axes, which for a short reducer is
    usually a little shorter than either diagonal edge) is not computed —
    the average of the two edges is used instead, which is close but not
    exact. A small, known, accepted imprecision for this one fitting.
  - **Branch take-off gaps are now bridged, but only with real topology
    behind it (25 Oct 2026).** The first version of this idea bridged
    every gap between two collinear, same-width paired runs and was
    rejected before shipping (see git history / BOQ-CHANGES-TODO.md item
    14 for the original write-up): it read a trunk that takes off
    branches from BOTH sides at the same point (very common) as one
    continuous run straight through the trunk's own body, silently adding
    the trunk's own width as if it were missing duct.

    **The real topology check that was missing:** a gap is bridged only
    when NOTHING else on the same layer has an endpoint landing inside
    it. When something does — a branch tying in, confirmed exactly on
    `hvac_tender_complex.dxf`'s M-DUCT-SUPPLY trunk, where two branches
    cross from opposite sides at the same point, x=13275-13725 — that is
    a real tee/cross junction, and the gap is the fitting's own width,
    not missing straight duct; left un-bridged, same as before this rule
    existed. A gap with nothing tied into it — a trunk split into two
    LINE entities for no topological reason — IS bridged. Verified
    against the real file both ways: its own 3 real junction gaps all
    stay un-bridged (143,862.5mm total, unchanged from before this rule);
    a synthetic gap with nothing crossing it correctly bridges.
    `_duct_runs()`'s new "bridged" kind, `_collapse_duct_pairs()`'s 4th
    return value. Tests:
    `tests/test_double_line_ducts.py::
    ABranchTakeOffGapIsBridgedOnlyWhenNoBranchTiesIn`.

---

## Rule 3 — a duct/pipe's size lives in the drawing's own TEXT, not its geometry

**Found:** 23 Oct 2026, the same day, from two independent directions at
once: the owner's own follow-up question ("is the CSV enough to make an
HVAC BOQ?") and a second AI review of the same file, both landing on the
identical gap. A route length alone cannot be priced — GI ductwork is
billed in **m² of sheet per size and gauge**, not metres of centreline —
and the size ("800x400", "DN80") is written right on the drawing as plain
TEXT, which `measure()` never read at all.

**The convention:** a duct/pipe's size is annotated as a nearby TEXT or
MTEXT entity — "800x400 SA", "600x300 RA", "CHWS DN80" — usually on its
own annotation layer (M-TEXT here), not the duct's own layer, and usually
positioned near one point along a run rather than repeated at every
segment a branch take-off happens to split that run into.

**The fix — `_duct_size_labels()` / `_duct_sizes_for_layer()` in
`core/boq.py`:** `_DUCT_SIZE_RE` / `_PIPE_SIZE_RE` find a size in any
TEXT/MTEXT string on the drawing. Each layer's runs (from `_duct_runs()`)
are grouped into "chains" — runs on the same straight line, the same
duct/pipe width apart, regardless of the gap between them (a branch
take-off splits one physical run into several LINE entities, and the
label is only ever near one of them). Each CHAIN, not each individual
run, is matched to its nearest label by point-to-segment distance — taking
the chain's single best (closest) candidate across all its members, which
is what correctly resolves a segment in the middle of a long trunk that
would, on its own, sit roughly equidistant between two different labels
and guess the wrong one; its unambiguous neighbour's own close match
settles it for the whole chain. A duct's area is `perimeter × length`
(`_duct_size_area()`), computed in m² when the drawing's unit is
confirmed, left out otherwise rather than guessed. **Scoped to layers that
name themselves duct or pipe work** (`"duct" in layer.lower()` /
`"pipe" in ...`) — caught live, before shipping: proximity alone wrongly
matched A-WALL, the architectural perimeter (which naturally runs right
past the ductwork's own size labels), to "800x400 SA". The layer's own
name is the one signal this whole module already trusts elsewhere (see
`measure()`'s own comment on `block_layers`).

Output lands in a new, additive `q["duct_sizes"]` list — `lengths_by_layer`
is completely untouched, still the one simple per-layer total it always
was. Each entry: `{"layer", "size", "kind" ("duct"|"pipe"), "length"
(drawing's own unit), "length_m", "area_m2" (ducts only)}`. Also written
as extra rows in the exported quantities CSV.

**What it does NOT handle yet:**
  - **An unusually named duct/pipe layer** (not containing "duct" or
    "pipe") gets no sizes at all, by design — silence over a wrong guess.
    If a client file uses a different convention, extend the name check,
    don't loosen the proximity matching.
  - **Two genuinely different-sized runs that happen to share the exact
    same line and width** (same y/x, same offset, by coincidence) would be
    chained together and given one label — a rare, specific edge case, not
    seen in the real file, worth a test the day it actually turns up.
  - **A transition's own size** (the reducer itself, e.g. "800x400 to
    600x400") is not labelled or sized — only "pair" and "single" kind
    runs get chained; a "transition" run is left out of `duct_sizes`
    entirely.

---

## Rule 4 — a corner in a single-line run is an elbow fitting

**Found:** 23 Oct 2026, the same file: the return duct (drawn single-line,
not double) turns one corner, which a real BOQ has to list as a fitting,
not silently fold into the route length.

**The fix — `_elbow_count()` in `core/boq.py`:** among a layer's "single"
kind runs (rule 1/2 already resolved — never a pair, never a transition),
count how many pairs of them **share an exact endpoint** (within 1 mm) with
a clear change of direction. Deliberately the opposite kind of check from
rule 2's rejected gap-bridging: this only ever looks at two segments that
already share a real, exact coordinate — a hard topological fact read
straight off the geometry, not a proximity guess — so it carries none of
that one's false-positive risk of merging two unrelated runs that happen
to sit nearby. Reported as a plain note, e.g. "M-DUCT-RETURN: 1 corner(s)
found... — read as elbow fittings."

**What it does NOT handle yet:**
  - **Tee/cross junctions** (a branch take-off meeting a trunk) are not
    counted at all — the same topological-ambiguity problem that sunk rule
    2's gap-bridging idea applies here too (which branch belongs to which
    side of the trunk is not a question proximity alone can answer
    safely). Left for a future rule that reasons about real topology.
  - **A bend inside a PAIRED (double-line) run** is not detected — only
    single-line runs are checked, since a pair's own two edges already
    describe its direction, and generalising this safely to paired runs
    needs its own test, not an assumption it works the same way.
