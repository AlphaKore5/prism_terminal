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
    un-paired (its two sides are not parallel), so its own edge length is
    counted in full on each side rather than a true centreline length —
    a small, known, accepted imprecision for that one kind of fitting,
    not something this rule tries to solve.
  - **The visible audit trail is a plain-text note, not a UI flag.** Right
    now `measure()` appends a note like "M-DUCT-SUPPLY: read 10 pair(s) of
    lines as one run's two edges... — verify before pricing" to the notes
    list the BOQ dialog already surfaces. Nothing routes this to a more
    prominent "this quantity was inferred, check it" indicator in the
    BOQ add-on's own screen yet — worth doing once more than one of these
    rules exists and a person needs to see at a glance which numbers were
    geometry straight from the drawing and which were inferred.
