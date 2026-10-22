"""
Prism — pricing a measured take-off into a tender-ready Bill of Quantities
──────────────────────────────────────────────────────────────────────────
`core.boq` measures a drawing and hands back real, auditable quantities. This
module is the half that turns those quantities into the document a contractor
actually tenders with: a priced BOQ with rates, amounts, section sub-totals,
contingency, GST and a grand total — exported to Excel with LIVE formulas, so
the estimator opens it and keeps working, not a dead picture of a table.

The hard rule the whole BOQ add-on lives by holds here without exception:
**no number of record is ever produced by an AI.**

  · Quantities come from measured geometry (core.boq) — never a model.
  · Rates come from a rate library the user brings (their own price list, or a
    DSR/state-SOR export) or types in — matched to items by the same
    deterministic fuzzy matcher `core.quoting` uses for quotations, which
    RETURNS CANDIDATES and a confidence flag, never a silent decision. A match
    that isn't confident leaves the rate blank and the item flagged, because a
    wrong rate on a tender is worse than an obvious gap.
  · Every amount, sub-total, tax and total is arithmetic — Decimal here, and a
    live `=D*E` / `=SUM()` formula in the sheet, so the buyer can audit and
    edit it in Excel.

The Indian specifics this market requires (researched, not assumed): units per
IS 1200 (cum / sqm / m / kg / nos), an Abstract of Cost that carries each
section forward, contingency and GST as their own visible lines (works-contract
GST, CGST+SGST intra-state vs IGST inter-state), and the grand total spelled
out in the Indian numbering system (lakh/crore). Rate analysis (material +
labour + plant + O&P build-up), a digitised DSR/SOR library, RA bills and a
frozen baseline are the roadmap beyond this file — the model here is shaped so
they slot in without a rewrite.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from decimal import Decimal

from . import quoting
from .boq import BoqError   # one user-facing error type for the whole add-on

try:                                                    # pragma: no cover
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAVE_XLSX = True
except Exception:                                       # pragma: no cover
    HAVE_XLSX = False

try:                                                    # pragma: no cover
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    HAVE_DOCX = True
except Exception:                                       # pragma: no cover
    HAVE_DOCX = False


# ── units ────────────────────────────────────────────────────────────────────
# core.boq reports lengths/areas in the drawing's own unit word ("meters",
# "millimeters", …). A BOQ wants the trade's short unit. Only these convert;
# a count is always "nos". The user can override any unit in the pricing grid,
# so this is a sensible default, not a decree.

_LENGTH_UNIT = {"meters": "m", "millimeters": "mm", "centimeters": "cm",
                "feet": "ft", "inches": "in", "miles": "mile"}
_AREA_UNIT = {"meters": "sqm", "millimeters": "sqmm", "centimeters": "sqcm",
              "feet": "sqft", "inches": "sqin"}


def _length_unit(drawing_unit: str) -> str:
    return _LENGTH_UNIT.get((drawing_unit or "").strip().lower(), "unit")


def _area_unit(drawing_unit: str) -> str:
    return _AREA_UNIT.get((drawing_unit or "").strip().lower(), "sq-unit")


# ── the priced document model ─────────────────────────────────────────────────

@dataclass
class BoqItem:
    """One priced line. `amount` is never stored — it is qty × rate, computed
    on demand here and written as a live formula into the sheet, so the two can
    never silently disagree."""
    description: str
    unit: str = "nos"
    quantity: Decimal = Decimal(0)
    rate: Decimal = Decimal(0)
    code: str = ""
    section: str = "Measured Works"
    # Where this line came from, kept for the audit trail: "measured:length:WALLS",
    # "measured:count:DOOR-900", "manual", "derived". Never shown as a rate/qty
    # source the user can't trace.
    source: str = ""
    is_derived: bool = False
    remark: str = ""

    def __post_init__(self):
        self.quantity = quoting.to_decimal(self.quantity)
        self.rate = quoting.to_decimal(self.rate)

    @property
    def amount(self) -> Decimal:
        return quoting.rupees(self.quantity * self.rate)

    @property
    def priced(self) -> bool:
        return self.rate > 0


@dataclass
class Boq:
    """A whole priced BOQ. `contingency_pct`/`gst_pct` are percentages (e.g.
    Decimal('18')). GST follows the works-contract rule: intra-state splits into
    CGST+SGST, inter-state is a single IGST — the split is presentational, the
    total tax is the same."""
    title: str = "Bill of Quantities"
    items: list[BoqItem] = field(default_factory=list)
    client: str = ""
    project: str = ""
    location: str = ""
    date: str = ""
    revision: str = ""
    contingency_pct: Decimal = Decimal(0)
    gst_pct: Decimal = Decimal(18)
    interstate: bool = False
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.contingency_pct = quoting.to_decimal(self.contingency_pct)
        self.gst_pct = quoting.to_decimal(self.gst_pct)

    # -- grouping & roll-up (the Decimal truth; the sheet mirrors it in formulas) --

    def sections(self) -> list[tuple[str, list[BoqItem]]]:
        """Items grouped by section, in first-seen order — the order the user
        arranged them, never re-sorted under them."""
        order: list[str] = []
        buckets: dict[str, list[BoqItem]] = {}
        for it in self.items:
            name = it.section or "Measured Works"
            if name not in buckets:
                buckets[name] = []
                order.append(name)
            buckets[name].append(it)
        return [(name, buckets[name]) for name in order]

    def subtotal(self) -> Decimal:
        return quoting.rupees(sum((it.amount for it in self.items), Decimal(0)))

    def contingency_amount(self) -> Decimal:
        return quoting.rupees(self.subtotal()
                              * quoting.to_decimal(self.contingency_pct) / 100)

    def pre_tax_total(self) -> Decimal:
        return quoting.rupees(self.subtotal() + self.contingency_amount())

    def gst_amount(self) -> Decimal:
        return quoting.rupees(self.pre_tax_total()
                              * quoting.to_decimal(self.gst_pct) / 100)

    def grand_total(self) -> Decimal:
        return quoting.rupees(self.pre_tax_total() + self.gst_amount())

    def unpriced(self) -> list[BoqItem]:
        """Lines with no rate — the ones that must NOT go out silently. The grid
        highlights these and the sheet counts them in a note."""
        return [it for it in self.items if not it.priced]


# ── measured quantities → a starting BOQ ──────────────────────────────────────

def boq_from_measured(q: dict, mapping: dict | None = None,
                      rate_items: list | None = None,
                      title: str = "Bill of Quantities") -> Boq:
    """Turn a `core.boq.measure()` result into a first-draft priced BOQ.

    `mapping` (optional) overrides per layer/block name — any of
    {code, description, unit, section, rate}. `rate_items` (optional) is a rate
    library (`quoting.RateItem` list): where a line has no explicit rate, a
    CONFIDENT fuzzy match against the library fills the rate/code, and anything
    less than confident is left blank rather than guessed. This is a draft the
    user then edits in the grid — every field here is a default, not a verdict.
    """
    mapping = mapping or {}
    unit = q.get("unit", "")
    items: list[BoqItem] = []

    def build(name: str, quantity, default_unit: str, kind: str):
        ov = mapping.get(name, {}) or {}
        it = BoqItem(
            description=ov.get("description") or name,
            unit=ov.get("unit") or default_unit,
            quantity=quoting.to_decimal(quantity),
            rate=quoting.to_decimal(ov.get("rate", 0)),
            code=ov.get("code", ""),
            section=ov.get("section") or "Measured Works",
            source=f"measured:{kind}:{name}",
        )
        if not it.priced and rate_items:
            _apply_confident_rate(it, rate_items)
        items.append(it)

    for name, length in (q.get("lengths_by_layer") or {}).items():
        build(name, length, _length_unit(unit), "length")
    for name, area in (q.get("areas_by_layer") or {}).items():
        build(name, area, _area_unit(unit), "area")
    for name, count in (q.get("block_counts") or {}).items():
        build(name, count, "nos", "count")

    return Boq(title=title, items=items)


_UNIT_ALIASES = {"rm": "m", "rmt": "m", "rft": "ft", "sqmt": "sqm",
                 "sqmts": "sqm", "sm": "sqm", "sft": "sqft", "no": "nos",
                 "nr": "nos", "number": "nos", "each": "nos", "ea": "nos"}


def _units_match(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        u = (u or "").strip().lower().replace(".", "").replace(" ", "")
        return _UNIT_ALIASES.get(u, u)
    return norm(a) == norm(b)


def _apply_confident_rate(item: BoqItem, rate_items: list) -> quoting.Match | None:
    """Fill an item's rate from the library ONLY when the match is confident
    AND the units agree. Returns the winning Match (for the UI to show its
    reason), or None.

    The unit gate is the guardrail: a confident *text* match on "brickwork" can
    still carry a per-cum rate, and our measured quantity is a length in metres.
    Applying it would price a wall's running length as though it were volume —
    a silent, dangerous wrong number, precisely what this add-on refuses to
    produce. So a matched rate whose unit differs is NOT applied; the item is
    left unpriced and the mismatch is noted, for the user to reconcile (enter a
    per-unit rate, or supply the dimension that converts the quantity). A unit
    the user has deliberately overridden is trusted — this only guards the
    automatic suggestion. Section/code are organisation, not numbers, so a
    confident match may still adopt them."""
    matches = quoting.match_item(f"{item.code} {item.description}".strip(), rate_items)
    if not matches or not quoting.is_confident(matches):
        return None
    best = matches[0].item
    if item.section in ("", "Measured Works") and best.group:
        item.section = best.group
    if not item.code and best.code:
        item.code = best.code
    if _units_match(item.unit, best.unit):
        item.rate = best.rate_for(item.quantity)
    else:
        item.remark = (f"library rate is per {best.unit}, but this is measured "
                       f"in {item.unit} — needs a quantity conversion first")
    return matches[0]


def suggest_rates(items: list[BoqItem], rate_items: list) -> dict[int, quoting.Match]:
    """Advisory pass for the grid: best library candidate per line, keyed by the
    line's index. The caller shows the suggestion + its reason and lets the user
    accept it — a fuzzy match on something that becomes a price is never applied
    silently. Confident matches can be pre-filled; the rest are hints."""
    out: dict[int, quoting.Match] = {}
    for i, it in enumerate(items):
        matches = quoting.match_item(f"{it.code} {it.description}".strip(), rate_items)
        if matches:
            out[i] = matches[0]
    return out


# ── amount in words (Indian numbering) ────────────────────────────────────────

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _below_thousand(n: int) -> str:
    if n < 100:
        return _two(n)
    rest = n % 100
    return (_ONES[n // 100] + " hundred"
            + (" " + _two(rest) if rest else "")).strip()


def _indian_words(n: int) -> str:
    """Non-negative integer → words, Indian system (thousand/lakh/crore)."""
    if n == 0:
        return "zero"
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    parts = []
    if crore:
        parts.append(_indian_words(crore) + " crore")   # recurse: crore can be large
    if lakh:
        parts.append(_two(lakh) + " lakh")
    if thousand:
        parts.append(_two(thousand) + " thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(value) -> str:
    """"Rupees one lakh twenty three thousand four hundred and fifty and 60
    paise only" — the words line every Indian tender/estimate carries. Paise
    are stated only when non-zero."""
    value = quoting.rupees(value)
    whole = int(value)
    paise = int((value - whole) * 100)
    words = "Rupees " + _indian_words(whole)
    if paise:
        words += f" and {_two(paise)} paise"
    return (words + " only").replace("  ", " ").strip()


# ── a starter rate library (indicative — verify before tendering) ─────────────
# So the tool prices something useful the first time, before the user attaches
# their own price list or a DSR/SOR export. These are common Indian building
# items at IS 1200 units and INDICATIVE 2023-ish rates — deliberately round,
# deliberately flagged. They exist to seed the fuzzy matcher, not to be trusted
# as a real schedule of rates.

_STARTER: list[tuple[str, str, str, str, str]] = [
    # (code, description, unit, rate, group)
    ("EW-01", "Earthwork excavation in ordinary soil up to 1.5 m depth", "cum", "185", "Earthwork"),
    ("EW-02", "Earth filling with available soil, watered & compacted in layers", "cum", "125", "Earthwork"),
    ("EW-03", "Sand filling in plinth / under floors", "cum", "1450", "Earthwork"),
    ("PC-01", "Plain cement concrete 1:4:8 in foundation / bed", "cum", "5200", "Concrete"),
    ("RC-01", "RCC M20 in foundation & plinth (excl. steel & formwork)", "cum", "6800", "Concrete"),
    ("RC-02", "RCC M25 in superstructure (excl. steel & formwork)", "cum", "7600", "Concrete"),
    ("ST-01", "Reinforcement steel Fe500 — cut, bent, placed & tied", "kg", "72", "Steel"),
    ("FW-01", "Formwork / centering & shuttering to foundations & footings", "sqm", "240", "Formwork"),
    ("FW-02", "Formwork to columns, beams & suspended slabs", "sqm", "330", "Formwork"),
    ("BW-01", "Brickwork in CM 1:6, 230 mm thick, in superstructure", "cum", "6300", "Masonry"),
    ("BW-02", "Half-brick (115 mm) partition wall in CM 1:4", "sqm", "720", "Masonry"),
    ("BB-01", "AAC block masonry 200 mm in thin-bed mortar", "cum", "5200", "Masonry"),
    ("PL-01", "12 mm internal cement plaster in CM 1:6", "sqm", "245", "Plaster"),
    ("PL-02", "20 mm external cement plaster in CM 1:4 (double coat)", "sqm", "320", "Plaster"),
    ("FL-01", "Vitrified tile flooring 600x600 over cement mortar bed", "sqm", "950", "Flooring"),
    ("FL-02", "Kota stone flooring, polished, over mortar bed", "sqm", "760", "Flooring"),
    ("FL-03", "IPS / cement concrete flooring, finished smooth", "sqm", "430", "Flooring"),
    ("WP-01", "Waterproofing to terrace — brickbat coba with slope", "sqm", "650", "Waterproofing"),
    ("PT-01", "Acrylic emulsion paint — 2 coats over primer (internal)", "sqm", "95", "Painting"),
    ("PT-02", "Exterior weatherproof paint — 2 coats over primer", "sqm", "135", "Painting"),
    ("DR-01", "Flush door shutter 35 mm with hardwood frame & fittings", "sqm", "2300", "Doors & Windows"),
    ("WN-01", "Aluminium sliding window with glazing & mesh", "sqm", "3300", "Doors & Windows"),
    ("WN-02", "MS grill / safety bar to windows", "kg", "125", "Doors & Windows"),
    ("PB-01", "CPVC water-supply piping — per point complete", "nos", "1250", "Plumbing"),
    ("PB-02", "PVC soil / waste pipe 110 mm with fittings", "m", "430", "Plumbing"),
    ("SN-01", "Wall-hung EWC with cistern, seat & fittings", "nos", "8800", "Sanitary"),
    ("SN-02", "Wash basin with CP fittings & waste", "nos", "3600", "Sanitary"),
    ("EL-01", "Light / fan point wiring in concealed conduit", "nos", "875", "Electrical"),
    ("EL-02", "Power socket point (6/16 A) wiring in conduit", "nos", "1250", "Electrical"),
    ("RF-01", "Weathering course / screed laid to slope on roof", "sqm", "285", "Roofing"),
    ("MS-01", "Anti-termite soil treatment (pre-construction)", "sqm", "48", "Miscellaneous"),
    ("MS-02", "Providing & fixing MS railing to staircase", "kg", "185", "Miscellaneous"),
]


def starter_rate_items() -> list:
    """The bundled indicative rate list, as `quoting.RateItem`s — the same type
    a user's uploaded price list loads into, so both feed the matcher identically."""
    return [quoting.RateItem(code=c, description=d, unit=u,
                             rate=quoting.to_decimal(r), group=g)
            for c, d, u, r, g in _STARTER]


STARTER_RATES_NOTE = ("Rates shown are INDICATIVE starter values — verify every "
                      "rate against your own quotation or the applicable DSR / "
                      "state SOR before tendering.")


# ── the deliverable: a priced Excel BOQ with live formulas ────────────────────

_MONEY_FMT = "#,##,##0.00"        # Excel renders this in the Indian grouping
_QTY_FMT = "#,##0.00"

_INK = "1F2A44"                   # header ink
_HEAD_FILL = "1F2A44"
_SECTION_FILL = "E8EDF5"
_TOTAL_FILL = "F3E9D2"
_GRAND_FILL = "D9E7D6"
_UNPRICED_FILL = "FBE4E4"
_GRID = "C7CDD6"

_COLS = ["Item", "Description", "Unit", "Quantity", "Rate (₹)", "Amount (₹)"]
_WIDTHS = [7, 58, 9, 13, 14, 16]


def _thin(ws, cell):
    side = Side(style="thin", color=_GRID)
    cell.border = Border(left=side, right=side, top=side, bottom=side)


def write_boq_xlsx(boq: Boq, path: str) -> str:
    """Write `boq` to a tender-ready .xlsx with LIVE formulas and return `path`.

    Two sheets: the itemised **Bill of Quantities** (sections, sub-totals,
    contingency, GST, grand total, amount in words) and an **Abstract of Cost**
    that carries each section forward — its figures reference the BOQ sheet's
    own sub-total cells, so editing a rate re-flows the abstract too. Every
    Amount is `=Qty*Rate`, every sub-total a `=SUM`, every tax a formula: the
    estimator can audit and adjust it in Excel, which a flattened value table
    could never allow."""
    if not HAVE_XLSX:
        raise BoqError("Writing an Excel BOQ needs the openpyxl package.")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bill of Quantities"
    for i, w in enumerate(_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    r = 1
    # -- title & project meta ------------------------------------------------
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    tcell = ws.cell(r, 1, boq.title.upper())
    tcell.font = Font(bold=True, size=15, color=_INK)
    tcell.alignment = Alignment(horizontal="center")
    r += 1
    meta_pairs = [("Project", boq.project), ("Client", boq.client),
                  ("Location", boq.location), ("Date", boq.date),
                  ("Revision", boq.revision)]
    meta = "   ·   ".join(f"{k}: {v}" for k, v in meta_pairs if v)
    if meta:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        mcell = ws.cell(r, 1, meta)
        mcell.font = Font(size=9, color="55607A")
        mcell.alignment = Alignment(horizontal="center")
        r += 1
    r += 1

    # -- column header -------------------------------------------------------
    header_row = r
    for i, name in enumerate(_COLS, 1):
        c = ws.cell(r, i, name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=_HEAD_FILL)
        c.alignment = Alignment(horizontal="center" if i != 2 else "left",
                                vertical="center", wrap_text=True)
        _thin(ws, c)
    ws.freeze_panes = ws.cell(r + 1, 1)
    r += 1

    # -- sections & items ----------------------------------------------------
    subtotal_cells: list[str] = []          # for the grand sub-total & abstract
    section_rows: list[tuple[str, str]] = []  # (section name, subtotal cell)
    for section, items in boq.sections():
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        sc = ws.cell(r, 1, section)
        sc.font = Font(bold=True, color=_INK)
        sc.fill = PatternFill("solid", fgColor=_SECTION_FILL)
        sc.alignment = Alignment(horizontal="left")
        r += 1

        first_item_row = r
        for n, it in enumerate(items, 1):
            ws.cell(r, 1, n).alignment = Alignment(horizontal="center")
            desc = it.description + (f"  [{it.code}]" if it.code else "")
            if it.is_derived:
                desc += "  (estimated — verify)"
            ws.cell(r, 2, desc).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(r, 3, it.unit).alignment = Alignment(horizontal="center")
            qy = ws.cell(r, 4, float(it.quantity)); qy.number_format = _QTY_FMT
            rt = ws.cell(r, 5, float(it.rate) if it.priced else None)
            rt.number_format = _MONEY_FMT
            am = ws.cell(r, 6, f"=D{r}*E{r}"); am.number_format = _MONEY_FMT
            if not it.priced:
                for col in (1, 2, 3, 4, 5, 6):
                    ws.cell(r, col).fill = PatternFill("solid", fgColor=_UNPRICED_FILL)
            for col in range(1, 7):
                _thin(ws, ws.cell(r, col))
            r += 1
        last_item_row = r - 1

        # section sub-total
        stc = ws.cell(r, 2, f"Sub-total — {section}")
        stc.font = Font(bold=True)
        stc.alignment = Alignment(horizontal="right")
        amt = ws.cell(r, 6, f"=SUM(F{first_item_row}:F{last_item_row})"
                      if items else 0)
        amt.number_format = _MONEY_FMT
        amt.font = Font(bold=True)
        for col in range(1, 7):
            cell = ws.cell(r, col)
            cell.fill = PatternFill("solid", fgColor=_SECTION_FILL)
            _thin(ws, cell)
        subtotal_cells.append(f"F{r}")
        section_rows.append((section, f"F{r}"))
        r += 1

    r += 1
    # -- roll-up: sub-total → contingency → GST → grand total ----------------
    def totals_row(label: str, formula: str, fill: str, bold=True):
        nonlocal r
        lc = ws.cell(r, 2, label)
        lc.font = Font(bold=bold)
        lc.alignment = Alignment(horizontal="right")
        vc = ws.cell(r, 6, formula)
        vc.number_format = _MONEY_FMT
        vc.font = Font(bold=bold)
        for col in range(2, 7):
            cell = ws.cell(r, col)
            cell.fill = PatternFill("solid", fgColor=fill)
            _thin(ws, cell)
        here = f"F{r}"
        r += 1
        return here

    works_sum = "+".join(subtotal_cells) if subtotal_cells else "0"
    sub_cell = totals_row("Sub-total (all works)", f"={works_sum}", _TOTAL_FILL)
    cont_pct = quoting.to_decimal(boq.contingency_pct)
    gst_pct = quoting.to_decimal(boq.gst_pct)
    cont_cell = totals_row(
        f"Add: Contingency @ {_pct(cont_pct)}%",
        f"=ROUND({sub_cell}*{cont_pct / 100},2)", _TOTAL_FILL)
    pretax_cell = totals_row("Total before GST",
                             f"={sub_cell}+{cont_cell}", _TOTAL_FILL)
    if boq.interstate:
        igst = totals_row(f"Add: IGST @ {_pct(gst_pct)}%",
                          f"=ROUND({pretax_cell}*{gst_pct / 100},2)", _TOTAL_FILL)
        gst_terms = igst
    else:
        half = gst_pct / 2
        cgst = totals_row(f"Add: CGST @ {_pct(half)}%",
                          f"=ROUND({pretax_cell}*{half / 100},2)", _TOTAL_FILL)
        sgst = totals_row(f"Add: SGST @ {_pct(half)}%",
                          f"=ROUND({pretax_cell}*{half / 100},2)", _TOTAL_FILL)
        gst_terms = f"{cgst}+{sgst}"
    grand = totals_row("GRAND TOTAL",
                       f"={pretax_cell}+{gst_terms}", _GRAND_FILL)

    # amount in words (of the model's grand total — the sheet total matches it)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    wcell = ws.cell(r, 1, amount_in_words(boq.grand_total()))
    wcell.font = Font(bold=True, italic=True, color=_INK)
    wcell.alignment = Alignment(horizontal="left", wrap_text=True)
    r += 2

    # -- notes / disclaimers -------------------------------------------------
    notes = list(boq.notes)
    unpriced = boq.unpriced()
    if unpriced:
        notes.insert(0, f"⚠ {len(unpriced)} item(s) have NO rate yet (shaded red) "
                        "— fill every rate before this BOQ is tendered.")
    notes.append("Quantities are measured from the drawing geometry; rates are to "
                 "be verified against your quotation or the applicable DSR / state "
                 "SOR. Amounts, sub-totals and taxes are live formulas — edit any "
                 "rate and the totals re-calculate.")
    for note in notes:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        nc = ws.cell(r, 1, note)
        nc.font = Font(size=9, color="7A2E2E" if note.startswith("⚠") else "55607A")
        nc.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 28
        r += 1

    # -- Abstract of Cost sheet (references the BOQ sheet's own sub-totals) ---
    _write_abstract(wb, boq, section_rows, sub_cell, cont_cell,
                    pretax_cell, grand)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    wb.save(path)
    return path


def _write_abstract(wb, boq: Boq, section_rows, sub_cell, cont_cell,
                    pretax_cell, grand_cell) -> None:
    """The one-page summary: each section carried forward, then the same
    roll-up. Values are formulas pointing back at the BOQ sheet, so the two
    pages can never drift."""
    ws = wb.create_sheet("Abstract of Cost")
    src = "'Bill of Quantities'"
    for i, w in enumerate([7, 52, 18], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    r = 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    t = ws.cell(r, 1, "ABSTRACT OF COST")
    t.font = Font(bold=True, size=14, color=_INK)
    t.alignment = Alignment(horizontal="center")
    r += 2
    for i, name in enumerate(["Item", "Description", "Amount (₹)"], 1):
        c = ws.cell(r, i, name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=_HEAD_FILL)
        c.alignment = Alignment(horizontal="center" if i != 2 else "left")
        _thin(ws, c)
    r += 1
    for n, (section, cell) in enumerate(section_rows, 1):
        ws.cell(r, 1, n).alignment = Alignment(horizontal="center")
        ws.cell(r, 2, section)
        a = ws.cell(r, 3, f"={src}!{cell}"); a.number_format = _MONEY_FMT
        for col in range(1, 4):
            _thin(ws, ws.cell(r, col))
        r += 1

    def summary_row(label, formula, fill, bold=True):
        nonlocal r
        lc = ws.cell(r, 2, label); lc.font = Font(bold=bold)
        lc.alignment = Alignment(horizontal="right")
        vc = ws.cell(r, 3, formula); vc.number_format = _MONEY_FMT
        vc.font = Font(bold=bold)
        for col in range(2, 4):
            ws.cell(r, col).fill = PatternFill("solid", fgColor=fill)
            _thin(ws, ws.cell(r, col))
        r += 1

    summary_row("Sub-total (all works)", f"={src}!{sub_cell}", _TOTAL_FILL)
    summary_row(f"Contingency @ {_pct(boq.contingency_pct)}%",
                f"={src}!{cont_cell}", _TOTAL_FILL)
    summary_row("Total before GST", f"={src}!{pretax_cell}", _TOTAL_FILL)
    # GST = grand − pre-tax, correct whether it was split CGST+SGST or a single
    # IGST on the BOQ sheet — no need to know which split was used.
    summary_row(f"GST @ {_pct(boq.gst_pct)}%",
                f"={src}!{grand_cell}-{src}!{pretax_cell}", _TOTAL_FILL)
    summary_row("GRAND TOTAL", f"={src}!{grand_cell}", _GRAND_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    wcell = ws.cell(r, 1, amount_in_words(boq.grand_total()))
    wcell.font = Font(bold=True, italic=True, color=_INK)
    wcell.alignment = Alignment(wrap_text=True)


def _pct(value) -> str:
    """A percentage without trailing-zero noise: 18, 7.5, 1.5 — not 18.00."""
    value = quoting.to_decimal(value)
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value.normalize():f}"


# ── a flat CSV, for re-opening / diffing ─────────────────────────────────────

def write_boq_csv(boq: Boq, path: str) -> str:
    """The priced lines as a plain CSV — re-openable anywhere, and the thing to
    diff two revisions against. Totals are written as computed values."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Section", "Item", "Code", "Description", "Unit",
                    "Quantity", "Rate", "Amount"])
        for section, items in boq.sections():
            for n, it in enumerate(items, 1):
                w.writerow([section, n, it.code, it.description, it.unit,
                            f"{it.quantity:.2f}", f"{it.rate:.2f}",
                            f"{it.amount:.2f}"])
        w.writerow([])
        w.writerow(["", "", "", "", "", "", "Sub-total", f"{boq.subtotal():.2f}"])
        w.writerow(["", "", "", "", "", "",
                    f"Contingency {_pct(boq.contingency_pct)}%",
                    f"{boq.contingency_amount():.2f}"])
        w.writerow(["", "", "", "", "", "",
                    f"GST {_pct(boq.gst_pct)}%", f"{boq.gst_amount():.2f}"])
        w.writerow(["", "", "", "", "", "", "Grand total",
                    f"{boq.grand_total():.2f}"])
    return path


# ── the deliverable, part one-and-a-half: a tender-ready Word document ─────────
# The owner's own words: "BOQ in any format gets filled in with the number
# from the pricing list the user will attach, that's it" — xlsx/csv/pdf
# already did; Word did not. Same Boq, same Decimal arithmetic the other
# three writers use (subtotal()/contingency_amount()/pre_tax_total()/
# gst_amount()/grand_total()) -- nothing here decides a rate or an amount,
# the same promise core/boq_price.py's own module docstring makes "without
# exception". Word has no formula cells, so unlike the xlsx sheet this is a
# flat, once-computed table -- editing a rate here does not re-flow the
# totals the way it does in Excel; the Excel export is still what an
# estimator should open to adjust and recalculate.

def write_boq_docx(boq: Boq, path: str) -> str:
    """Write `boq` to a tender-ready .docx and return `path`."""
    if not HAVE_DOCX:
        raise BoqError("Writing a Word BOQ needs the python-docx package.")

    doc = docx.Document()
    heading = doc.add_heading(boq.title.upper(), level=1)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    meta_pairs = [("Project", boq.project), ("Client", boq.client),
                  ("Location", boq.location), ("Date", boq.date),
                  ("Revision", boq.revision)]
    meta = "   ·   ".join(f"{k}: {v}" for k, v in meta_pairs if v)
    if meta:
        p = doc.add_paragraph(meta)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for cell, name in zip(table.rows[0].cells, _COLS):
        cell.text = name
        if cell.paragraphs[0].runs:
            cell.paragraphs[0].runs[0].font.bold = True

    def add_row(values, *, bold=False):
        cells = table.add_row().cells
        for cell, value in zip(cells, values):
            cell.text = str(value)
            if bold and cell.paragraphs[0].runs:
                cell.paragraphs[0].runs[0].font.bold = True
        return cells

    for section, items in boq.sections():
        header = table.add_row().cells
        header[0].merge(header[-1])
        header[0].text = section
        if header[0].paragraphs[0].runs:
            header[0].paragraphs[0].runs[0].font.bold = True
        for n, it in enumerate(items, 1):
            desc = it.description + (f"  [{it.code}]" if it.code else "")
            if it.is_derived:
                desc += "  (estimated — verify)"
            add_row([n, desc, it.unit, _qty_str(it.quantity),
                    (_inr(it.rate) if it.priced else ""), _inr(it.amount)])
        section_total = sum((it.amount for it in items), Decimal(0))
        add_row(["", f"Sub-total — {section}", "", "", "", _inr(section_total)],
                bold=True)

    add_row(["", "Sub-total (all works)", "", "", "", _inr(boq.subtotal())],
            bold=True)
    add_row(["", f"Add: Contingency @ {_pct(boq.contingency_pct)}%", "", "", "",
            _inr(boq.contingency_amount())], bold=True)
    add_row(["", "Total before GST", "", "", "", _inr(boq.pre_tax_total())],
            bold=True)
    if boq.interstate:
        add_row(["", f"Add: IGST @ {_pct(boq.gst_pct)}%", "", "", "",
                _inr(boq.gst_amount())], bold=True)
    else:
        # cgst/sgst split for display only, same arithmetic boq_html()
        # already uses -- sgst is gst_amount() minus cgst, not computed
        # independently, so the two halves always sum to the exact total.
        half_pct = quoting.to_decimal(boq.gst_pct) / 2
        cgst = quoting.rupees(boq.pre_tax_total() * half_pct / 100)
        sgst = boq.gst_amount() - cgst
        add_row(["", f"Add: CGST @ {_pct(half_pct)}%", "", "", "", _inr(cgst)],
                bold=True)
        add_row(["", f"Add: SGST @ {_pct(half_pct)}%", "", "", "", _inr(sgst)],
                bold=True)
    add_row(["", "GRAND TOTAL", "", "", "", _inr(boq.grand_total())], bold=True)

    words = doc.add_paragraph(amount_in_words(boq.grand_total()))
    if words.runs:
        words.runs[0].font.bold = True
        words.runs[0].font.italic = True

    notes = list(boq.notes)
    unpriced = boq.unpriced()
    if unpriced:
        notes.insert(0, f"⚠ {len(unpriced)} item(s) have NO rate yet — fill "
                     "every rate before this BOQ is tendered.")
    notes.append("Quantities are measured from the drawing geometry; rates "
                 "are to be verified against your quotation or the "
                 "applicable DSR / state SOR. This Word copy is a snapshot — "
                 "edit a rate and re-export rather than typing over a total.")
    for note in notes:
        doc.add_paragraph(note)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    doc.save(path)
    return path


# ── the deliverable, part two: a government-format tender PDF ──────────────────
# Schedule A (Abstract of Cost) + Schedule B (item-wise BOQ), rendered from the
# SAME Boq model to HTML and printed by the browser engine Prism already ships
# for Studio (core.browser → Chromium's page.pdf) — no new dependency. This is
# the shape a state-PWD / municipal tender or a bank-loan file expects: rupees
# in words, sections carried to an abstract, a signature block.

def _inr(value) -> str:
    """Indian-grouped rupee string with no symbol: 1,23,45,678.90."""
    value = quoting.rupees(value)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) <= 3:
        grouped = whole
    else:
        last3, rest, parts = whole[-3:], whole[:-3], []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return f"{sign}{grouped}.{frac}"


def _qty_str(value) -> str:
    """A quantity without trailing-zero noise: 108, 54, 142.7."""
    v = quoting.to_decimal(value)
    if v == v.to_integral_value():
        return str(int(v))
    return ("%f" % v).rstrip("0").rstrip(".")


_PDF_CSS = """
* { box-sizing: border-box; }
body { font-family: "Segoe UI", Arial, sans-serif; color:#1b1b1b; background:#fff;
       font-size:11px; margin:0; }
html { background:#fff; }
h1 { font-size:17px; text-align:center; margin:0 0 2px; letter-spacing:.4px; }
.sub { text-align:center; color:#555; font-size:10px; margin-bottom:10px; }
table.meta { border-collapse:collapse; margin:0 auto 12px; font-size:10.5px; }
table.meta td { padding:1px 8px; }
table.meta .k { color:#555; }
.sched { font-size:13px; font-weight:700; margin:16px 0 6px; color:#243b53;
         border-bottom:2px solid #243b53; padding-bottom:3px; }
table.boq { width:100%; border-collapse:collapse; font-size:10.5px; }
table.boq thead { display:table-header-group; }
table.boq th { background:#243b53; color:#fff; padding:5px 6px; text-align:left; font-weight:600; }
table.boq td { padding:4px 6px; border-bottom:1px solid #dbe0e7; vertical-align:top; }
.num { text-align:right; white-space:nowrap; }
tr.sec td { background:#eaeff5; font-weight:700; color:#243b53; }
tr.subrow td { background:#f4f7fb; font-weight:700; }
tr.total td { font-weight:700; }
tr.grand td { background:#e4efe1; font-weight:700; font-size:11.5px; }
tr.unpriced td { color:#b00020; }
.words { margin:8px 2px; font-style:italic; font-weight:600; }
.note { color:#6a6a6a; font-size:9.5px; margin-top:8px; }
.warn { color:#b00020; font-weight:600; font-size:10px; margin-top:6px; }
.break { break-before:page; }
table.sign { width:100%; margin-top:40px; border-collapse:collapse; }
table.sign td { width:50%; border-top:1px solid #444; padding-top:6px; font-size:10px;
                color:#333; text-align:center; }
"""

_PDF_FOOTER = (
    '<div style="font-size:8px;color:#777;width:100%;text-align:center;">'
    'Bill of Quantities &nbsp;&middot;&nbsp; Page <span class="pageNumber"></span> '
    'of <span class="totalPages"></span></div>')


def _rollup_rows(boq: Boq, cols: int) -> str:
    """Sub-total → contingency → GST → grand-total, as table rows that fit a
    3-column (abstract) or 6-column (detailed) table."""
    def row(label, amount, cls):
        mid = f'<td colspan="4">{label}</td>' if cols == 6 else f'<td>{label}</td>'
        return f'<tr class="{cls}"><td></td>{mid}<td class="num">{_inr(amount)}</td></tr>'

    out = row("Sub-total (all works)", boq.subtotal(), "subrow")
    out += row(f"Add: Contingency @ {_pct(boq.contingency_pct)}%",
               boq.contingency_amount(), "total")
    out += row("Total before GST", boq.pre_tax_total(), "total")
    if boq.interstate:
        out += row(f"Add: IGST @ {_pct(boq.gst_pct)}%", boq.gst_amount(), "total")
    else:
        half = quoting.to_decimal(boq.gst_pct) / 2
        cgst = quoting.rupees(boq.pre_tax_total() * half / 100)
        out += row(f"Add: CGST @ {_pct(half)}%", cgst, "total")
        out += row(f"Add: SGST @ {_pct(half)}%", boq.gst_amount() - cgst, "total")
    out += row("GRAND TOTAL", boq.grand_total(), "grand")
    return out


def boq_html(boq: Boq) -> str:
    """The whole Schedule A + Schedule B document as one self-contained HTML
    string (inline CSS, no external assets) — ready for the browser to print."""
    import html as H
    esc = H.escape
    sections = boq.sections()

    meta = "".join(
        f'<tr><td class="k">{esc(k)}</td><td>{esc(str(v))}</td></tr>'
        for k, v in (("Project", boq.project), ("Client", boq.client),
                     ("Location", boq.location), ("Date", boq.date),
                     ("Revision", boq.revision)) if v)

    a_rows = ""
    for n, (name, items) in enumerate(sections, 1):
        amt = quoting.rupees(sum((it.amount for it in items), Decimal(0)))
        a_rows += (f'<tr><td>{n}</td><td>{esc(name)}</td>'
                   f'<td class="num">{_inr(amt)}</td></tr>')
    a_rows += _rollup_rows(boq, cols=3)

    b_rows = ""
    for name, items in sections:
        b_rows += f'<tr class="sec"><td colspan="6">{esc(name)}</td></tr>'
        for n, it in enumerate(items, 1):
            desc = esc(it.description)
            if it.code:
                desc += f' <span style="color:#888">[{esc(it.code)}]</span>'
            if it.is_derived:
                desc += ' <span style="color:#b06a00">(estimated)</span>'
            if it.priced:
                rate, amount, cls = _inr(it.rate), _inr(it.amount), ""
            else:
                rate, amount, cls = "&mdash;", "&mdash;", ' class="unpriced"'
            b_rows += (f'<tr{cls}><td>{n}</td><td>{desc}</td><td>{esc(it.unit)}</td>'
                       f'<td class="num">{_qty_str(it.quantity)}</td>'
                       f'<td class="num">{rate}</td><td class="num">{amount}</td></tr>')
        sub = quoting.rupees(sum((it.amount for it in items), Decimal(0)))
        b_rows += (f'<tr class="subrow"><td></td><td colspan="4">Sub-total &mdash; '
                   f'{esc(name)}</td><td class="num">{_inr(sub)}</td></tr>')
    b_rows += _rollup_rows(boq, cols=6)

    unpriced = boq.unpriced()
    warn = (f'<div class="warn">{len(unpriced)} item(s) are unpriced (shown as '
            '&mdash;) and excluded from the totals — price them before this goes '
            'to tender.</div>') if unpriced else ""

    words = esc(amount_in_words(boq.grand_total()))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>{_PDF_CSS}</style></head><body>
<h1>{esc(boq.title).upper()}</h1>
<div class="sub">{esc(boq.project or "")}</div>
<table class="meta">{meta}</table>

<div class="sched">Schedule A &mdash; Abstract of Cost</div>
<table class="boq"><thead><tr><th style="width:8%">Item</th><th>Description</th>
<th class="num" style="width:24%">Amount (&#8377;)</th></tr></thead><tbody>{a_rows}</tbody></table>
<div class="words">{words}</div>

<div class="sched break">Schedule B &mdash; Detailed Bill of Quantities</div>
<table class="boq"><thead><tr><th style="width:6%">Item</th><th>Description</th>
<th style="width:7%">Unit</th><th class="num" style="width:9%">Qty</th>
<th class="num" style="width:13%">Rate (&#8377;)</th>
<th class="num" style="width:15%">Amount (&#8377;)</th></tr></thead><tbody>{b_rows}</tbody></table>
<div class="words">{words}</div>
{warn}
<div class="note">Quantities are measured from the drawing geometry; rates are
to be verified against the applicable DSR / state SOR or your own quotation.
This is a computed estimate — check every figure before tendering.</div>
<table class="sign"><tr><td>Contractor / Tenderer</td>
<td>Prepared &amp; Checked by</td></tr></table>
</body></html>"""


def write_boq_pdf(boq: Boq, path: str) -> str:
    """Render the Schedule A + B document to a PDF at `path` via the bundled
    Chromium (Playwright's page.pdf) and return `path`. Raises BoqError with a
    plain message if the browser engine isn't available — the Excel export
    still works without it."""
    html_str = boq_html(boq)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                              # noqa: BLE001
        raise BoqError(
            "Exporting a tender PDF needs the bundled browser engine "
            "(Playwright/Chromium), which isn't available here. The priced "
            "Excel export works without it.") from e
    from . import browser
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    try:
        with sync_playwright() as pw:
            b = browser.launch_chromium(
                pw, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            try:
                page = b.new_page()
                page.set_content(html_str, wait_until="load")
                page.pdf(path=path, format="A4", print_background=True,
                         margin={"top": "14mm", "bottom": "16mm",
                                 "left": "12mm", "right": "12mm"},
                         display_header_footer=True,
                         header_template="<span></span>",
                         footer_template=_PDF_FOOTER)
            finally:
                b.close()
    except BoqError:
        raise
    except Exception as e:                              # noqa: BLE001
        first = (str(e).strip().splitlines() or [""])[0]
        raise BoqError(f"Couldn't render the tender PDF: {first[:200]}") from e
    return path
