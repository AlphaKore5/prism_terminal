"""Bill of Materials — the parts list to FABRICATE a thing, from its drawing.

Sibling of core.boq, and it shares boq's whole measurement backbone: the DWG is
converted and measured by boq.ensure_dxf/measure (real geometry, never an AI),
and the same measured dict is handed here. The difference is entirely in what
the numbers MEAN and how they are written up:

    BOQ  →  quantities of construction WORK, grouped by trade, for a QS to price
    BOM  →  the PARTS to make a fabricated product, with material, grade and spec

A fabrication GA drawing (a crusher, a conveyor, an over-band magnetic
separator) is a manufactured assembly, so the measured data reads as parts, not
trades:

  · block INSERT counts        →  discrete part quantities (4 idler rollers,
                                   24 M-16 bolts, 2 bushes …)
  · layer/block names          →  part identities (DRIVE_SHAFT, ANGLE_CLEAT …)
  · polyline lengths per layer →  cut-to-length stock (channels, flats, angles,
                                   plate edges) as running length / pieces
  · closed areas per layer     →  plate area for a weight take-off

This module holds only the two PROMPT builders that reframe that data as a BOM;
measurement, DWG→DXF conversion and the CSV all stay in core.boq and are reused
unchanged, so the two add-ons can never disagree about the numbers.
"""
from __future__ import annotations

from . import skills as _SK


# Shared with core.boq.formatting_prompt — a stated "basis" does not stop a
# model inventing a precise-but-impossible spec, and one impossible figure
# discredits the whole document to the fabricator who reads it.
_PLAUSIBILITY_RULE = (
    " DO NOT FABRICATE COMPONENT SPECIFICATIONS. For a bought-out item "
    "(bearing, motor, fastener, standard section) describe it by type and duty "
    "and leave the exact model/size to the supplier's selection — or cite a "
    "genuine standard designation (an ISMC 100 channel, an M16 bolt, a 6205 "
    "bearing). Never invent a precise-looking figure. Any dimension, grade or "
    "rating you DO state must be physically plausible for that part (a mushroom "
    "E-stop head is ~40 mm across, not 250 mm; an M16 bolt is not 100 mm across "
    "flats). When unsure, give the type and mark the exact size/grade 'to "
    "detail drawing / supplier spec (TBC)' rather than guess."
)


def standards_prompt(user_request: str, project_context: str = "",
                     measured_text: str = "") -> str:
    """RESEARCH stage for a BOM: the material grades, standard stock sizes and
    fastener/bearing conventions a works engineer looks up before raising a
    parts list — the manufacturing analogue of boq.standards_prompt, and
    deliberately NOT about site/ELV norms.

    `measured_text`, when the drawing was measured, is the component list. It is
    passed to TARGET the research at the parts actually present, not to be
    quoted back."""
    where = f" Project context: {project_context}." if project_context.strip() else ""
    measured_block = (
        "\n\nThe drawing HAS already been measured (a later stage owns the "
        "actual counts and lengths). Use the component / layer names below ONLY "
        "to decide WHICH materials, standard sizes and fastener/bearing specs to "
        "research — for these specific parts. Do NOT quote the quantities back "
        "or write the parts list:\n"
        f"{measured_text.strip()}"
    ) if measured_text.strip() else ""
    return (
        "You are the RESEARCH stage of a Bill-of-MATERIALS pipeline for a "
        "fabricated / manufactured assembly. Your ONLY task is to set out the "
        "CURRENT STANDARD MATERIALS and SIZING CONVENTIONS a works/design "
        f"engineer would apply when specifying this build: {user_request}.{where}"
        "\n\nDo NOT write a parts list, do not invent quantities, and do not "
        "describe this specific machine — give the general manufacturing "
        "standards a later stage applies to the real measured parts."
        f"{measured_block}"
        "\n\nCover, with SPECIFIC designations wherever they exist:"
        "\n  · structural steel grades and STANDARD SECTION sizes (e.g. IS 2062 "
        "E250 for MS; ISMC / ISA / ISMB / ISLB channel, angle and beam "
        "designations and their standard stock lengths; common plate "
        "thicknesses)"
        "\n  · shaft / bush / pin materials (e.g. EN8, EN19, EN24; case-hardened "
        "vs through-hardened conventions) and standard round-bar sizes"
        "\n  · fasteners to standard (IS 1367 property classes 4.6 / 8.8 / 10.9, "
        "standard metric bolt/nut/washer sizes, when HT or hot-dip galvanised is "
        "specified) — a BOM lists these as discrete counts"
        "\n  · bearings, seals, couplings, drive components by their standard "
        "designation series where one genuinely applies"
        "\n  · surface treatment / paint and welding standards (electrode class, "
        "e.g. AWS/IS), and typical weld/finish allowances"
        "\n  · the applicable Indian codes by name/number (IS/BIS) where they "
        "genuinely apply, plus common Indian market stock availability if it "
        "differs from the written standard"
        "\n\nFormat it as a short, dense checklist of stated rules — each line "
        "usable as a spec a later stage can cite verbatim. Flag anything that is "
        "genuinely a judgement call rather than a standard."
        + _SK.addendum("bom.standards")
    )


def formatting_prompt(quantities_text: str, project_context: str = "",
                      has_template: bool = False, legend: str = "",
                      scoped: bool = False, brief_text: str = "",
                      allow_derived: bool = True, has_cad: bool = True,
                      standards_text: str = "", industry_note: str = "",
                      derivation_example: str = "") -> str:
    """Turn the measured geometry into a Bill of Materials. Same signature as
    boq.formatting_prompt so the dialog can dispatch on mode with no other
    change; the CONTENT reframes the numbers as parts, not trades."""
    context = f" Project context: {project_context}." if project_context.strip() else ""
    if has_template:
        structure = (
            "A parts-list / BOM TEMPLATE is attached — use it as a reference for "
            "the firm's style: its column set, its part-numbering, its "
            "description tone and its sub-assembly breakdown. Adapt the "
            "structure to what THIS drawing's measured parts actually are; do "
            "not copy its literal rows. Every quantity you present must trace "
            "back to the measured data below, and nothing measured should be "
            "silently dropped."
        )
    else:
        structure = (
            "Lay the BOM out as a table with these columns: Item No.; Part / "
            "Description; Qty; Unit (No. / m / kg); Material & Grade; Size / "
            "Spec; Basis / Remarks; then blank Rate and Amount columns for "
            "costing later. GROUP the rows under sub-assemblies (e.g. drive "
            "unit, magnet/body, frame & supports, idler set, fasteners & "
            "hardware) inferred from the layer/block names — a flat list of a "
            "hundred parts is not a BOM a fabricator can issue to the shop."
        )
    scope_rule = (
        " A SCOPE FILTER was already applied below — every part shown is in "
        "scope. Do not add parts from outside it."
    ) if scoped else ""
    brief_block = (
        "\n\nINTERPRETATION BRIEF from the reading stage — use its legend for "
        "part descriptions and its scope list to decide what appears:\n"
        f"{brief_text.strip()}"
    ) if brief_text.strip() else ""
    # The drawing itself is NOT attached -- the same rule Gerber, STEP and
    # BOQ keep. The writer has the measured summary: every block name with
    # its count, every layer's lengths and areas.
    cad_note = (
        "\n\nABOUT THE DRAWING. It was measured on the customer's own machine "
        "and is NOT attached — you will not receive the file, so do not ask "
        "for it and do not try to open one. The MEASURED QUANTITIES below are "
        "the authoritative numbers: every block/INSERT name with its COUNT "
        "and layer (your part quantities), every layer's polyline lengths "
        "(cut-to-length stock) and closed-polyline/hatch areas (plate). Then:\n"
        "  (a) From the block and layer names, state briefly what the "
        "assembly IS and its main sub-assemblies.\n"
        "  (b) Map the figures to parts: repeated blocks are discrete parts "
        "(give the count); per-layer lengths are sections to be cut "
        "(channels, angles, flats — give running length and, where a "
        "standard stock length is known, the number of lengths); plate "
        "areas support a weight take-off.\n"
        "  (c) Anything the figures do not settle — a part's grade, a fit, a "
        "finish — is flagged as to be confirmed, never invented.\n"
        "Do not skip this and jump to formatting."
    ) if has_cad else ""
    if not has_cad:
        derive_rule = (
            " THERE IS NO DRAWING for this build — the requirement above is your "
            "only definition, so EVERY quantity is derived, not measured. Work "
            "as an experienced works engineer raising a parts list from a "
            "specification: break the product into its real sub-assemblies and "
            "components, and for each line give the quantity, unit, material/"
            "grade and the basis (a standard size, a rule of thumb, a stated "
            "assumption, or a count that follows from the spec). Rules: (a) open "
            "with a prominent warning box — this is a PROVISIONAL, "
            "SPECIFICATION-BASED parts list, no drawing was available, and it "
            "must be verified against detail/manufacturing drawings before "
            "cutting, ordering or costing; (b) put the governing specification "
            "you worked to (capacity, size, duty) in a short table near the "
            "top; (c) where a part genuinely cannot be pinned down without a "
            "drawing, say so on that line rather than inventing a precise "
            "figure; (d) separate BOUGHT-OUT parts (bearings, motors, "
            "fasteners, standard sections) from FABRICATED parts (custom "
            "plates, brackets, weldments) — the bought-outs are far more "
            "certain and the reader must tell them apart at a glance."
        )
    elif allow_derived:
        derive_rule = (
            " A fabrication GA often shows the built geometry but not every "
            "bought-out fitting (bearings, seals, drive motor, some fasteners). "
            "Do NOT refuse on that basis and do NOT pretend those were measured. "
            "DERIVE them as a works engineer would — from the parts that ARE "
            "measured plus explicit, stated assumptions (a shaft implies "
            "bearings and a coupling; a bolted joint implies a bolt/nut/washer "
            "set of a stated grade) — and mark every derived line clearly with "
            "its basis. Keep measured parts exactly as measured; never present a "
            "derived quantity as a measured one."
        )
    else:
        derive_rule = (
            " Do NOT add parts that have no corresponding measurement below — if "
            "the request implies parts the drawing does not contain, say so "
            "plainly instead of inventing quantities."
        )
    standards_block = (
        "\n\nMATERIALS & STANDARDS BRIEF from the research stage — when you set "
        "a material, grade, standard size or fastener spec, apply THESE and cite "
        "the one you used. Prefer them over your own recollection; if one is "
        "missing, say which assumption you had to make instead:\n"
        f"{standards_text.strip()}"
    ) if standards_text.strip() else ""
    legend_block = (
        "\n\nLAYER/BLOCK LEGEND (what these codes mean — use it to write real "
        f"part descriptions instead of repeating the raw code):\n{legend}"
    ) if legend.strip() else ""
    ground_truth = (
        " The quantities listed at the end were measured directly from the "
        "drawing's geometry — treat them as ground truth and do not recalculate "
        "or contradict them."
    ) if has_cad else ""
    step2 = "\n\nSTEP 2 — BUILD THE BILL OF MATERIALS." if has_cad else "\n\n"
    instructions = (
        f"Your task is: produce a professional BILL OF MATERIALS (BOM) — the "
        f"parts list needed to fabricate and assemble this item.{context}"
        f"{ground_truth}{cad_note}{step2}{derive_rule}{scope_rule} {structure}"
        f"{_PLAUSIBILITY_RULE} Leave Rate/Amount blank for costing later. "
        "Present it as clean tables. Note prominently at the top that this is a "
        "parts take-off to be verified against detail drawings before cutting "
        "or ordering, and that no prices are included."
    )
    tail = ("\n\nMEASURED QUANTITIES:\n" + quantities_text) if quantities_text.strip() else ""
    # House doctrine for a parts list, when a skill claims this job — see
    # core/skills.py. Empty when none does, so the prompt is unchanged.
    return (instructions + standards_block + brief_block + legend_block
            + _SK.addendum("bom.format") + tail)
