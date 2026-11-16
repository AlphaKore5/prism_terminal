"""BOQ lines are built in trade units (m, sqm), whatever unit the drawing is in.

The bug this guards: an mm drawing used to give lines like "186,400 mm", so a
rate typed per metre (1,350) priced the line at Rs 25,16,40,000 instead of
Rs 2,51,640, and every per-metre rate list was blocked by the unit gate.

Run from the engine root:  python -m pytest tests/test_boq_price.py
"""
import csv
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import boq_price as bp   # noqa: E402
from core import quoting           # noqa: E402


def _q(unit, lengths=None, areas=None, counts=None):
    return {"unit": unit, "unit_code": 0, "unit_confirmed": True,
            "lengths_by_layer": lengths or {}, "areas_by_layer": areas or {},
            "block_counts": counts or {}}


def _line(boq, name):
    return next(it for it in boq.items if it.source.endswith(":" + name))


class LinesAreInTradeUnits(unittest.TestCase):

    def test_mm_drawing_lengths_become_metres_and_areas_sqm(self):
        boq = bp.boq_from_measured(_q("millimeters", {"M-DUCT-SUPP": 186400.0},
                                      {"A-SLAB": 2_000_000.0}, {"PUMP": 3}))
        duct, slab, pump = (_line(boq, n) for n in ("M-DUCT-SUPP", "A-SLAB", "PUMP"))
        self.assertEqual((duct.unit, duct.quantity), ("m", Decimal("186.4")))
        self.assertEqual((slab.unit, slab.quantity), ("sqm", Decimal("2")))
        self.assertEqual((pump.unit, pump.quantity), ("nos", Decimal("3")))

    def test_cm_drawing_lengths_become_metres_and_areas_sqm(self):
        boq = bp.boq_from_measured(_q("centimeters", {"WALL": 18640.0},
                                      {"FLOOR": 20000.0}, {"DOOR": 2}))
        self.assertEqual((_line(boq, "WALL").unit, _line(boq, "WALL").quantity),
                         ("m", Decimal("186.4")))
        self.assertEqual((_line(boq, "FLOOR").unit, _line(boq, "FLOOR").quantity),
                         ("sqm", Decimal("2")))
        self.assertEqual(_line(boq, "DOOR").quantity, Decimal("2"))

    def test_metre_drawing_is_not_converted_again(self):
        boq = bp.boq_from_measured(_q("meters", {"WALL": 186.4}, {"FLOOR": 2.5}))
        self.assertEqual((_line(boq, "WALL").unit, _line(boq, "WALL").quantity),
                         ("m", Decimal("186.4")))
        self.assertEqual((_line(boq, "FLOOR").unit, _line(boq, "FLOOR").quantity),
                         ("sqm", Decimal("2.5")))

    def test_feet_and_unknown_units_are_left_as_drawn(self):
        ft = bp.boq_from_measured(_q("feet", {"WALL": 100.0}, {"FLOOR": 50.0}))
        self.assertEqual((_line(ft, "WALL").unit, _line(ft, "WALL").quantity),
                         ("ft", Decimal("100")))
        self.assertEqual(_line(ft, "FLOOR").unit, "sqft")
        unk = bp.boq_from_measured(_q("unspecified", {"WALL": 4000.0}))
        self.assertEqual((_line(unk, "WALL").unit, _line(unk, "WALL").quantity),
                         ("unit", Decimal("4000")))


class TypedRateArithmetic(unittest.TestCase):

    def test_rate_per_metre_on_an_mm_drawing_prices_the_metres(self):
        boq = bp.boq_from_measured(_q("millimeters", {"M-DUCT-SUPP": 186400.0}))
        boq.items[0].rate = quoting.to_decimal("1350")
        self.assertEqual(boq.items[0].amount, Decimal("251640.00"))
        self.assertEqual(boq.subtotal(), Decimal("251640.00"))
        self.assertEqual(boq.grand_total(), Decimal("296935.20"))   # + 18% GST

    def test_rate_per_sqm_on_an_mm_area(self):
        boq = bp.boq_from_measured(_q("millimeters", areas={"A-SLAB": 12_500_000.0}))
        boq.items[0].rate = quoting.to_decimal("950")
        self.assertEqual(boq.items[0].amount, Decimal("11875.00"))   # 12.5 sqm


class StarterAndRateListGate(unittest.TestCase):

    def test_per_metre_rate_list_now_applies_to_an_mm_line(self):
        rates = [quoting.RateItem(code="DU-01", description="Supply duct M-DUCT-SUPP",
                                  unit="m", rate=Decimal("1350"))]
        boq = bp.boq_from_measured(_q("millimeters", {"M-DUCT-SUPP": 186400.0}),
                                   rate_items=rates)
        it = boq.items[0]
        self.assertEqual((it.rate, it.amount, it.remark),
                         (Decimal("1350"), Decimal("251640.00"), ""))

    def test_starter_rates_apply_to_converted_lines(self):
        q = _q("millimeters", {"PVC soil waste pipe 110 mm with fittings": 12000.0},
               {"Vitrified tile flooring 600x600": 4_000_000.0})
        boq = bp.boq_from_measured(q, rate_items=bp.starter_rate_items())
        pipe = _line(boq, "PVC soil waste pipe 110 mm with fittings")
        tile = _line(boq, "Vitrified tile flooring 600x600")
        self.assertEqual((pipe.unit, pipe.quantity, pipe.rate), ("m", Decimal("12"), Decimal("430")))
        self.assertEqual(pipe.amount, Decimal("5160.00"))
        self.assertEqual((tile.unit, tile.quantity, tile.rate), ("sqm", Decimal("4"), Decimal("950")))
        self.assertEqual(tile.amount, Decimal("3800.00"))

    def test_gate_still_blocks_a_real_mismatch(self):
        # A per-nos rate must not be applied to a length, converted or not.
        q = _q("millimeters", {"Wall-hung EWC with cistern seat fittings": 5000.0})
        boq = bp.boq_from_measured(q, rate_items=bp.starter_rate_items())
        it = boq.items[0]
        self.assertEqual((it.unit, it.rate), ("m", Decimal("0")))
        self.assertIn("per nos", it.remark)
        self.assertIn("measured in m", it.remark)


class ExportsAgree(unittest.TestCase):
    """Every export shows the same unit, quantity and amount as the line."""

    def setUp(self):
        self.boq = bp.boq_from_measured(_q("millimeters", {"M-DUCT-SUPP": 186400.0}))
        self.boq.items[0].rate = Decimal("1350")
        self.tmp = tempfile.mkdtemp()

    def test_csv(self):
        path = bp.write_boq_csv(self.boq, os.path.join(self.tmp, "b.csv"))
        with open(path, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1][4:], ["m", "186.40", "1350.00", "251640.00"])

    def test_html(self):
        html = bp.boq_html(self.boq)
        self.assertIn('<td>m</td><td class="num">186.4</td>', html)
        self.assertIn("2,51,640.00", html)
        self.assertNotIn("186400", html.replace(",", ""))

    @unittest.skipUnless(bp.HAVE_DOCX, "python-docx not installed")
    def test_docx(self):
        import docx
        path = bp.write_boq_docx(self.boq, os.path.join(self.tmp, "b.docx"))
        cells = [c.text for t in docx.Document(path).tables
                 for r in t.rows for c in r.cells]
        self.assertIn("186.4", cells)
        self.assertIn("m", cells)
        self.assertTrue(any("2,51,640.00" in c for c in cells), cells)

    @unittest.skipUnless(bp.HAVE_XLSX, "openpyxl not installed")
    def test_xlsx(self):
        import openpyxl
        path = bp.write_boq_xlsx(self.boq, os.path.join(self.tmp, "b.xlsx"))
        ws = openpyxl.load_workbook(path)["Bill of Quantities"]
        row = next(r for r in ws.iter_rows(values_only=True) if r[1] == "M-DUCT-SUPP")
        self.assertEqual((row[2], row[3], row[4]), ("m", 186.4, 1350.0))


if __name__ == "__main__":
    unittest.main()
