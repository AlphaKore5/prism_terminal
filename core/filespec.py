"""
Structured files -- the model returns JSON, Prism builds the file
─────────────────────────────────────────────────────────────────
A spreadsheet written as Markdown is a guess: the columns are whatever the
model felt like, numbers arrive as "1,200.50" strings, Hindi and Gujarati text
is mangled by whichever tool opens the result. Claude does it the other way
round: the model calls a tool, `create_file`, with STRUCTURED data (rows and
typed columns, document blocks) and the host turns that into a real file.

This module is that host side, for Prism. Nothing here talks to the network:

  * `validate(spec)`  strict check of the JSON contract below; raises
                      FileSpecError whose text is exactly what is sent back to
                      the model for a repair turn.
  * `render(spec, out_dir)`  XLSX / CSV / DOCX / PDF / MD / TXT, locally.
  * `run_file_tool(ai_chat, ...)`  the tool-call loop: ask, validate, render;
                      on a rejection send the exact error back, up to a cap.
  * `convert(path, ext)`  "make it a CSV / Excel / PDF / Word" from the spec
                      stored next to the file -- no AI call.

The server needs no change: /v1/ai/chat already has `json_mode`; tool calling is
a JSON contract at this level, carried by core/ai_client.py like every other
AI call.

THE CONTRACT (version 1)
────────────────────────
The model's whole reply is ONE JSON object:

    {"tool": "create_file",
     "message": "one short sentence for the person",
     "arguments": {"files": [ <file>, ... ]}}

or, when it cannot make the file (it needs something the person has not given):

    {"tool": "none", "message": "what is missing, in the person's language"}

<file> for a spreadsheet (type "xlsx" or "csv"):

    {"name": "Quotation", "type": "xlsx",
     "sheets": [{"name": "Items",
                 "columns": [{"key": "item", "title": "Item", "type": "text"},
                             {"key": "qty",  "title": "Qty",  "type": "number"},
                             {"key": "rate", "title": "Rate", "type": "currency", "symbol": "₹"},
                             {"key": "amt",  "title": "Amount", "type": "currency", "symbol": "₹"}],
                 "rows": [["Cable tray", 10, 450.5, "=B2*C2"],
                          ["Clamp", 40, 12, "=B3*C3"]],
                 "widths": [28, 8, 12, 14],
                 "freeze_header": true}]}

  column type: text | number | currency | date | percent.
    number/currency/percent cells hold real JSON numbers (no "1,200", no "Rs",
    no "%"); percent is a fraction (0.18 means 18%); date is "YYYY-MM-DD".
    A cell string starting with "=" is a FORMULA: allowed ONLY in an xlsx
    file, ONLY in a number/currency/percent column, built from a short list of
    safe functions (SUM, AVERAGE, MIN, MAX, COUNT, COUNTA, ROUND, ROUNDUP,
    ROUNDDOWN, IF, ABS, PRODUCT, SUMPRODUCT, MEDIAN, AND, OR).
    Row 1 of the sheet is the header, so the first data row is Excel row 2.
  A row is a list (same order as columns) or an object keyed by column key.

<file> for a document (type "docx", "pdf", "md" or "txt"):

    {"name": "Site visit report", "type": "docx",
     "blocks": [{"type": "heading", "level": 1, "text": "Site visit"},
                {"type": "paragraph", "text": "We inspected ..."},
                {"type": "bullets",  "items": ["Cable tray done", "Clamps pending"]},
                {"type": "numbered", "items": ["Fix", "Test"]},
                {"type": "table", "columns": ["Item", "Status"],
                 "rows": [["Tray", "Done"], ["Clamp", "Pending"]]},
                {"type": "code", "text": "make all"}]}

Any file may carry the other shape too (a docx may have "sheets": its sheets
print as tables; an xlsx built from "blocks" gets one sheet per table).

Limits and names are in the constants below. File names are plain names: no
folders, no "..", no characters Windows refuses, no reserved device names
(CON, NUL, COM1...), at most 80 characters; a missing or doubled extension is
repaired and a repeated name gets " (2)".

Next to every file Prism writes `<file>.spec.json` (the validated spec for that
file), so a later conversion starts from the data, not from a re-parsed
document.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable

CONTRACT_VERSION = 1

# ── limits ───────────────────────────────────────────────────────────────────
MAX_SPEC_CHARS = 1_500_000
MAX_FILES = 6
MAX_SHEETS = 12
MAX_ROWS = 5_000
MAX_COLS = 40
MAX_BLOCKS = 400
MAX_ITEMS = 200
MAX_CELL_CHARS = 5_000
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 80
MAX_PROBLEMS = 8                      # shown to the model per rejection

FILE_TYPES = ("xlsx", "csv", "docx", "pdf", "md", "txt")
SHEET_TYPES = ("xlsx", "csv")
COLUMN_TYPES = ("text", "number", "currency", "date", "percent")
NUMERIC = ("number", "currency", "percent")
BLOCK_TYPES = ("heading", "paragraph", "bullets", "numbered", "table", "code")

_FORMULA_FUNCS = frozenset({
    "SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "ROUND", "ROUNDUP",
    "ROUNDDOWN", "IF", "ABS", "PRODUCT", "SUMPRODUCT", "MEDIAN", "AND", "OR"})
_FORMULA_OK = re.compile(r"^=[A-Za-z0-9 .,:()+\-*/^%<>=$\"'!_]{1,300}$")
_FORMULA_FN = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)])
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\\x00-\x1f]")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXT_OF_TYPE = {t: "." + t for t in FILE_TYPES}


class FileSpecError(ValueError):
    """The spec broke the contract. `problems` are written for the model to
    fix; `str(e)` is all of them."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        shown = self.problems[:MAX_PROBLEMS]
        more = len(self.problems) - len(shown)
        text = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
        super().__init__(text)


# ── validation ───────────────────────────────────────────────────────────────

def parse_json(text: str):
    """The model's reply as a Python value. Tolerates a ```json fence and chatter
    around the object; raises FileSpecError("not valid JSON ...") otherwise."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except ValueError as first:
        a, b = s.find("{"), s.rfind("}")
        if 0 <= a < b:
            try:
                return json.loads(s[a:b + 1])
            except ValueError:
                pass
        raise FileSpecError(
            f"the reply was not valid JSON ({first}); reply with the one JSON "
            "object only, no text around it") from None


def safe_name(name, type_: str, used: set | None = None) -> str:
    """A file name that is safe on Windows, macOS and Linux, WITHOUT extension.
    Raises FileSpecError for names that are an attempt rather than a name."""
    raw = str(name if name is not None else "").strip()
    if not raw:
        raise FileSpecError("a file has no name")
    if _BAD_NAME_CHARS.search(raw) or ".." in raw:
        raise FileSpecError(
            f"file name {raw[:40]!r} has a folder, a '..' or a character "
            "Windows does not allow; use a plain name such as \"Quotation\"")
    ext = _EXT_OF_TYPE.get(type_, "")
    stem = raw
    if ext and stem.lower().endswith(ext):
        stem = stem[:-len(ext)]
    stem = stem.strip(" .")
    if not stem:
        raise FileSpecError(f"file name {raw[:40]!r} is empty once cleaned")
    if len(stem) > MAX_NAME_CHARS:
        raise FileSpecError(
            f"file name is {len(stem)} characters; keep it under "
            f"{MAX_NAME_CHARS}")
    if stem.split(".")[0].upper() in _RESERVED:
        raise FileSpecError(
            f"file name {stem!r} is a reserved Windows device name; pick another")
    if used is not None:
        base, n = stem, 1
        while (stem + ext).lower() in used:
            n += 1
            stem = f"{base} ({n})"
        used.add((stem + ext).lower())
    return stem


def _check_formula(f: str, type_: str, ctype: str, where: str, bad: list):
    if type_ != "xlsx":
        bad.append(f"{where}: a formula is only allowed in an xlsx file")
    elif not _FORMULA_OK.match(f) or "__" in f:
        bad.append(f"{where}: formula {f[:40]!r} has characters or length "
                   "that are not allowed")
    else:
        for fn in _FORMULA_FN.findall(f):
            if fn.upper() not in _FORMULA_FUNCS:
                bad.append(f"{where}: formula uses {fn.upper()}, which is not "
                           "allowed (use "
                           + ", ".join(sorted(_FORMULA_FUNCS)) + ")")
                break


def _check_cell(v, col: dict, type_: str, where: str, bad: list):
    ctype = col["type"]
    if v is None:
        return
    if (isinstance(v, str) and v.startswith("=") and len(v) > 1
            and ctype in NUMERIC):
        _check_formula(v, type_, ctype, where, bad)
        return
    if ctype == "text":
        if isinstance(v, (dict, list)):
            bad.append(f"{where}: a text cell must be a string, not a list or object")
        elif len(str(v)) > MAX_CELL_CHARS:
            bad.append(f"{where}: text is longer than {MAX_CELL_CHARS} characters")
    elif ctype in NUMERIC:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            bad.append(
                f"{where}: column {col['title']!r} is {ctype} so the cell must be "
                f"a JSON number (no quotes, no thousands commas, no currency or "
                f"% sign), got {str(v)[:30]!r}")
        elif isinstance(v, float) and not math.isfinite(v):
            bad.append(f"{where}: number is not finite")
    elif ctype == "date":
        if not (isinstance(v, str) and _DATE.match(v)):
            bad.append(f"{where}: date must be \"YYYY-MM-DD\", got {str(v)[:30]!r}")
        else:
            try:
                _dt.date.fromisoformat(v)
            except ValueError:
                bad.append(f"{where}: {v!r} is not a real date")


def _sheet_name_problem(name) -> str:
    if not isinstance(name, str) or not name.strip():
        return "a sheet has no name"
    if len(name) > 31:
        return f"sheet name {name[:20]!r}... is longer than 31 characters"
    if _BAD_SHEET_CHARS.search(name):
        return f"sheet name {name!r} has a character Excel refuses ( [ ] : * ? / \\ )"
    if name != name.strip() or name.startswith("'") or name.endswith("'"):
        return f"sheet name {name!r} may not start or end with a space or apostrophe"
    if name.lower() == "history":
        return "sheet name 'History' is reserved by Excel"
    return ""


def _validate_sheet(sh, i: int, type_: str, fpath: str, bad: list, names: set):
    w = f"{fpath}.sheets[{i}]"
    if not isinstance(sh, dict):
        bad.append(f"{w} must be an object")
        return None
    problem = _sheet_name_problem(sh.get("name"))
    if problem:
        bad.append(f"{w}: {problem}")
    elif sh["name"].lower() in names:
        bad.append(f"{w}: sheet name {sh['name']!r} is used twice (names are "
                   "compared ignoring case)")
    else:
        names.add(sh["name"].lower())
    cols = sh.get("columns")
    if not isinstance(cols, list) or not cols:
        bad.append(f"{w}.columns must be a non-empty list")
        return None
    if len(cols) > MAX_COLS:
        bad.append(f"{w}.columns has {len(cols)}; the limit is {MAX_COLS}")
        return None
    keys: set = set()
    good_cols = []
    for j, c in enumerate(cols):
        cw = f"{w}.columns[{j}]"
        if not isinstance(c, dict):
            bad.append(f"{cw} must be an object with key, title, type")
            continue
        key, title, ctype = c.get("key"), c.get("title"), c.get("type", "text")
        if not isinstance(key, str) or not key.strip() or len(key) > 40:
            bad.append(f"{cw}.key must be a short non-empty string")
        elif key in keys:
            bad.append(f"{cw}.key {key!r} is used twice")
        else:
            keys.add(key)
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            bad.append(f"{cw}.title must be a non-empty string under 100 characters")
        if ctype not in COLUMN_TYPES:
            bad.append(f"{cw}.type {str(ctype)[:20]!r} must be one of "
                       + ", ".join(COLUMN_TYPES))
            ctype = "text"
        sym = c.get("symbol", "")
        if sym is not None and (not isinstance(sym, str) or len(sym) > 4):
            bad.append(f"{cw}.symbol must be a short string such as \"₹\"")
        good_cols.append({"key": key, "title": title if isinstance(title, str) else "",
                          "type": ctype, "symbol": sym if isinstance(sym, str) else ""})
    rows = sh.get("rows", [])
    if not isinstance(rows, list):
        bad.append(f"{w}.rows must be a list")
        return None
    if len(rows) > MAX_ROWS:
        bad.append(f"{w}.rows has {len(rows)}; the limit is {MAX_ROWS}")
        return None
    norm_rows = []
    for r, row in enumerate(rows):
        rw = f"{w}.rows[{r}]"
        if isinstance(row, dict):
            row = [row.get(c["key"]) for c in good_cols]
        if not isinstance(row, list):
            bad.append(f"{rw} must be a list (or an object keyed by column key)")
            continue
        if len(row) > len(good_cols):
            bad.append(f"{rw} has {len(row)} cells but there are only "
                       f"{len(good_cols)} columns")
            continue
        row = list(row) + [None] * (len(good_cols) - len(row))
        for k, v in enumerate(row):
            if len(bad) > 40:
                break
            _check_cell(v, good_cols[k], type_,
                        f"{rw}[{k}] (Excel row {r + 2}, column {good_cols[k]['title']!r})",
                        bad)
        norm_rows.append(row)
    widths = sh.get("widths")
    if widths is not None:
        if (not isinstance(widths, list) or len(widths) > len(good_cols)
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           and 3 <= x <= 100 for x in widths)):
            bad.append(f"{w}.widths must be a list of at most one number (3-100) "
                       "per column")
            widths = None
    fh = sh.get("freeze_header", True)
    if not isinstance(fh, bool):
        bad.append(f"{w}.freeze_header must be true or false")
        fh = True
    return {"name": sh.get("name"), "columns": good_cols, "rows": norm_rows,
            "widths": list(widths) if widths else [], "freeze_header": fh}


def _validate_block(b, i: int, fpath: str, bad: list):
    w = f"{fpath}.blocks[{i}]"
    if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
        bad.append(f"{w}.type must be one of " + ", ".join(BLOCK_TYPES))
        return None
    t = b["type"]
    if t in ("heading", "paragraph", "code"):
        text = b.get("text")
        if not isinstance(text, str) or not text.strip():
            bad.append(f"{w}.text must be a non-empty string")
            return None
        if len(text) > MAX_TEXT_CHARS:
            bad.append(f"{w}.text is longer than {MAX_TEXT_CHARS} characters")
            return None
        out = {"type": t, "text": text}
        if t == "heading":
            lvl = b.get("level", 1)
            if isinstance(lvl, bool) or lvl not in (1, 2, 3):
                bad.append(f"{w}.level must be 1, 2 or 3")
                lvl = 1
            out["level"] = lvl
        return out
    if t in ("bullets", "numbered"):
        items = b.get("items")
        if (not isinstance(items, list) or not items or len(items) > MAX_ITEMS
                or not all(isinstance(x, (str, int, float)) and not isinstance(x, bool)
                           for x in items)):
            bad.append(f"{w}.items must be a list of 1-{MAX_ITEMS} strings")
            return None
        return {"type": t, "items": [str(x) for x in items]}
    cols = b.get("columns")
    rows = b.get("rows")
    if (not isinstance(cols, list) or not cols or len(cols) > MAX_COLS):
        bad.append(f"{w}.columns must be a list of column titles")
        return None
    titles = []
    for c in cols:
        titles.append(str(c.get("title", "")) if isinstance(c, dict) else str(c))
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        bad.append(f"{w}.rows must be a list of at most {MAX_ROWS} rows")
        return None
    out_rows = []
    for r, row in enumerate(rows):
        if not isinstance(row, list) or len(row) > len(titles):
            bad.append(f"{w}.rows[{r}] must be a list of at most {len(titles)} cells")
            return None
        out_rows.append(["" if v is None else str(v) for v in row]
                        + [""] * (len(titles) - len(row)))
    return {"type": "table", "columns": titles, "rows": out_rows}


def validate(spec, stored: bool = False) -> dict:
    """The spec, checked and normalised (new dict; the input is not changed).
    Raises FileSpecError listing what to fix. `stored` is for a spec Prism
    itself wrote earlier: a csv converted from an xlsx keeps its formulas."""
    if not isinstance(spec, dict):
        raise FileSpecError("the reply must be a JSON object")
    if "arguments" in spec and isinstance(spec["arguments"], dict):
        spec = spec["arguments"]
    files = spec.get("files")
    if not isinstance(files, list) or not files:
        raise FileSpecError('"files" must be a non-empty list')
    if len(files) > MAX_FILES:
        raise FileSpecError(f'"files" has {len(files)}; the limit is {MAX_FILES}')
    try:
        if len(json.dumps(spec, ensure_ascii=False)) > MAX_SPEC_CHARS:
            raise FileSpecError("the spec is too large; send fewer rows")
    except (TypeError, ValueError):
        raise FileSpecError("the spec contains something that is not JSON") from None
    bad: list[str] = []
    used: set = set()
    out_files = []
    for fi, f in enumerate(files):
        fp = f"files[{fi}]"
        if not isinstance(f, dict):
            bad.append(f"{fp} must be an object")
            continue
        type_ = str(f.get("type", "")).lower().lstrip(".")
        if type_ not in FILE_TYPES:
            bad.append(f"{fp}.type {str(f.get('type'))[:20]!r} must be one of "
                       + ", ".join(FILE_TYPES))
            continue
        try:
            name = safe_name(f.get("name"), type_, used)
        except FileSpecError as e:
            bad.append(f"{fp}: {e}")
            continue
        entry: dict = {"name": name, "type": type_}
        sheets, blocks = f.get("sheets"), f.get("blocks")
        if type_ in SHEET_TYPES and not sheets and not blocks:
            bad.append(f"{fp}: a {type_} file needs \"sheets\"")
            continue
        if type_ not in SHEET_TYPES and not blocks and not sheets:
            bad.append(f"{fp}: a {type_} file needs \"blocks\"")
            continue
        if sheets:
            if not isinstance(sheets, list) or len(sheets) > MAX_SHEETS:
                bad.append(f"{fp}.sheets must be a list of at most {MAX_SHEETS}")
                continue
            names: set = set()
            ok = [_validate_sheet(s, i, "xlsx" if stored else type_, fp, bad, names)
                  for i, s in enumerate(sheets)]
            entry["sheets"] = [s for s in ok if s]
        if blocks:
            if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
                bad.append(f"{fp}.blocks must be a list of at most {MAX_BLOCKS}")
                continue
            ok = [_validate_block(b, i, fp, bad) for i, b in enumerate(blocks)]
            entry["blocks"] = [b for b in ok if b]
        out_files.append(entry)
    if bad:
        raise FileSpecError(bad)
    return {"version": CONTRACT_VERSION, "files": out_files}


# ── rendering ────────────────────────────────────────────────────────────────

def _blocks_of(entry: dict) -> list[dict]:
    """A file's content as document blocks (sheets become headed tables)."""
    out = list(entry.get("blocks") or [])
    for sh in entry.get("sheets") or []:
        if len(entry.get("sheets") or []) > 1 or out:
            out.append({"type": "heading", "level": 2, "text": sh["name"]})
        out.append({"type": "table", "columns": [c["title"] for c in sh["columns"]],
                    "rows": [[_cell_text(v, c) for v, c in zip(r, sh["columns"])]
                             for r in _computed_rows(sh)]})
    return out


def _sheets_of(entry: dict) -> list[dict]:
    """A file's content as sheets (each table block becomes a text sheet).
    Raises FileSpecError when there is no table to make a sheet from."""
    if entry.get("sheets"):
        return entry["sheets"]
    sheets, names = [], set()
    for b in entry.get("blocks") or []:
        if b["type"] != "table":
            continue
        base = f"Table {len(sheets) + 1}"
        sheets.append({
            "name": base, "freeze_header": True, "widths": [],
            "columns": [{"key": f"c{i}", "title": t or f"Column {i + 1}",
                         "type": "text", "symbol": ""}
                        for i, t in enumerate(b["columns"])],
            "rows": [list(r) for r in b["rows"]]})
        names.add(base.lower())
    if not sheets:
        raise FileSpecError("this document has no table, so it cannot become a "
                            "spreadsheet")
    return sheets



# ── formulas, worked out for the formats that cannot hold one ────────────────

_CELL = re.compile(r"\$?([A-Z]{1,2})\$?(\d{1,5})(?::\$?([A-Z]{1,2})\$?(\d{1,5}))?")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _computed_rows(sheet: dict) -> list[list]:
    """The sheet's rows with every formula the small evaluator understands
    replaced by its value (PDF, Word, text and the preview cannot hold a
    formula). A formula it cannot work out stays as written. The stored spec
    and the xlsx/csv outputs are never touched."""
    rows = [list(r) for r in sheet["rows"]]
    memo: dict = {}

    def cell(ci: int, ri: int, depth: int):
        if ri < 0 or ci < 0 or ri >= len(rows) or ci >= len(rows[ri]):
            return 0
        key = (ci, ri)
        if key in memo:
            return memo[key]
        v = rows[ri][ci]
        if isinstance(v, str) and v.startswith("="):
            v = evaluate(v, depth + 1)
        memo[key] = 0 if v is None or isinstance(v, str) else v
        return memo[key]

    def evaluate(f: str, depth: int = 0):
        if depth > 8:
            raise ValueError("deep")
        expr = f[1:]

        def ref(m):
            c1, r1 = _col_index(m.group(1)), int(m.group(2)) - 2
            if m.group(3):
                c2, r2 = _col_index(m.group(3)), int(m.group(4)) - 2
                vals = [cell(c, r, depth) for r in range(min(r1, r2), max(r1, r2) + 1)
                        for c in range(min(c1, c2), max(c1, c2) + 1)]
                return "[" + ",".join(repr(x) for x in vals) + "]"
            return repr(cell(c1, r1, depth))
        if "!" in expr or '"' in expr or "^" in expr or "__" in expr:
            raise ValueError("unsupported")
        expr = _CELL.sub(ref, expr)
        expr = expr.replace("<>", "!=")
        expr = re.sub(r"(?<![<>!=])=(?!=)", "==", expr)
        expr = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", expr)
        expr = re.sub(r"\b(AND|OR|IF)\b", lambda m: m.group(1).lower() + "_", expr)
        env = {"__builtins__": {}, "SUM": _sum, "AVERAGE": _avg, "MIN": _min,
               "MAX": _max, "COUNT": _count, "COUNTA": _count, "ROUND": _round,
               "ABS": abs, "PRODUCT": _prod, "MEDIAN": _median,
               "if_": lambda c, a, b=0: a if c else b,
               "and_": lambda *a: all(a), "or_": lambda *a: any(a)}
        if re.search(r"[A-Za-z_]+(?!\s*\()", re.sub(r"[A-Za-z_]+\s*\(", "", expr)):
            raise ValueError("unsupported")
        return eval(expr, env)                      # noqa: S307 -- see below

    for ri, row in enumerate(rows):
        for ci, v in enumerate(row):
            if isinstance(v, str) and v.startswith("="):
                try:
                    out = evaluate(v)
                    if isinstance(out, float) and not math.isfinite(out):
                        raise ValueError("nan")
                    if isinstance(out, (int, float)) and not isinstance(out, bool):
                        rows[ri][ci] = round(out, 10) if isinstance(out, float) else out
                except Exception:                    # noqa: BLE001
                    pass
    return rows


def _flat(args):
    for a in args:
        if isinstance(a, list):
            yield from a
        else:
            yield a


def _sum(*a):
    return sum(_flat(a))


def _avg(*a):
    v = list(_flat(a))
    return sum(v) / len(v)


def _min(*a):
    return min(_flat(a))


def _max(*a):
    return max(_flat(a))


def _count(*a):
    return len([x for x in _flat(a) if isinstance(x, (int, float))])


def _round(x, n=0):
    return round(x, int(n))


def _prod(*a):
    out = 1
    for x in _flat(a):
        out *= x
    return out


def _median(*a):
    v = sorted(_flat(a))
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def _fmt_number(v, ctype: str) -> str:
    if ctype == "percent":
        s = f"{v * 100:.4f}".rstrip("0").rstrip(".")
        return s + "%"
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)


def _cell_text(v, col: dict) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if col["type"] in NUMERIC and isinstance(v, (int, float)) and not isinstance(v, bool):
        if col["type"] == "percent":
            return _fmt_number(v, "percent")
        if col["type"] == "currency":
            return col.get("symbol", "") + f"{v:,.2f}"
        return f"{int(v):,}" if v == int(v) else f"{v:,.2f}"
    return str(v)


def csv_safe(text: str) -> str:
    """A TEXT cell that a spreadsheet would run as a formula gets a leading
    apostrophe (OWASP's rule): = + - @ and a leading tab or CR."""
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def render_csv(sheet: dict) -> bytes:
    """One sheet as UTF-8 WITH a BOM (Excel then reads Hindi/Gujarati right)."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    cols = sheet["columns"]
    w.writerow([csv_safe(c["title"]) for c in cols])
    for row in sheet["rows"]:
        out = []
        for v, c in zip(row, cols):
            if v is None:
                out.append("")
            elif isinstance(v, str) and c["type"] in NUMERIC and v.startswith("="):
                out.append(v)              # a validated formula: Excel computes it
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(_fmt_number(v, c["type"]))
            else:
                out.append(csv_safe(str(v)))
        w.writerow(out)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _unique_path(out_dir: str, stem: str, ext: str) -> str:
    path = os.path.join(out_dir, stem + ext)
    n = 1
    while os.path.exists(path) or os.path.exists(path + ".spec.json"):
        n += 1
        path = os.path.join(out_dir, f"{stem} ({n}){ext}")
    return path


def _write_xlsx(entry: dict, path: str):
    from openpyxl import Workbook                       # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
    from openpyxl.utils import get_column_letter        # noqa: PLC0415
    wb = Workbook()
    wb.remove(wb.active)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2B2B2B")
    for sh in _sheets_of(entry):
        ws = wb.create_sheet(sh["name"])
        cols = sh["columns"]
        ws.append([c["title"] for c in cols])
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r, row in enumerate(sh["rows"], start=2):
            for k, (v, c) in enumerate(zip(row, cols), start=1):
                cell = ws.cell(row=r, column=k)
                if v is None:
                    continue
                if isinstance(v, str) and v.startswith("=") and c["type"] in NUMERIC:
                    cell.value = v                       # stays a formula
                elif c["type"] == "date" and isinstance(v, str):
                    cell.value = _dt.date.fromisoformat(v)
                    cell.number_format = "yyyy-mm-dd"
                elif c["type"] in NUMERIC and isinstance(v, (int, float)):
                    cell.value = v
                else:
                    cell.value = str(v)
                    cell.data_type = "s"                 # "=x" in a text column is text
                if c["type"] == "currency":
                    cell.number_format = (f'"{c["symbol"]}"' if c["symbol"] else "") + "#,##0.00"
                elif c["type"] == "percent":
                    cell.number_format = "0.0%"
                elif c["type"] == "number" and isinstance(v, float) and v != int(v):
                    cell.number_format = "#,##0.00"
                elif c["type"] == "number":
                    cell.number_format = "#,##0.##" if isinstance(v, float) else "#,##0"
        for k, c in enumerate(cols):
            if k < len(sh["widths"]):
                width = sh["widths"][k]
            else:
                longest = max([len(c["title"])] + [
                    len(_cell_text(r[k], c)) for r in sh["rows"][:300]
                    if k < len(r) and r[k] is not None])
                width = min(60, max(8, longest + 2))
            ws.column_dimensions[get_column_letter(k + 1)].width = width
        if sh["freeze_header"]:
            ws.freeze_panes = "A2"
        if sh["rows"]:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(sh['rows']) + 1}"
    wb.save(path)


def to_markdown(entry: dict) -> str:
    out = []
    for b in _blocks_of(entry):
        t = b["type"]
        if t == "heading":
            out.append("#" * b["level"] + " " + b["text"])
        elif t == "paragraph":
            out.append(b["text"])
        elif t == "bullets":
            out.append("\n".join(f"- {x}" for x in b["items"]))
        elif t == "numbered":
            out.append("\n".join(f"{i}. {x}" for i, x in enumerate(b["items"], 1)))
        elif t == "code":
            out.append("```\n" + b["text"] + "\n```")
        else:
            esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
            lines = ["| " + " | ".join(esc(c) for c in b["columns"]) + " |",
                     "|" + "|".join(" --- " for _ in b["columns"]) + "|"]
            lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in b["rows"]]
            out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


def to_text(entry: dict) -> str:
    out = []
    for b in _blocks_of(entry):
        t = b["type"]
        if t == "heading":
            out.append(b["text"].upper() if b["level"] == 1 else b["text"])
        elif t in ("paragraph", "code"):
            out.append(b["text"])
        elif t == "bullets":
            out.append("\n".join(f"- {x}" for x in b["items"]))
        elif t == "numbered":
            out.append("\n".join(f"{i}. {x}" for i, x in enumerate(b["items"], 1)))
        else:
            out.append("\n".join(["\t".join(b["columns"])]
                                 + ["\t".join(r) for r in b["rows"]]))
    return "\n\n".join(out) + "\n"


def _write_docx(entry: dict, path: str):
    from docx import Document                           # noqa: PLC0415
    from docx.shared import Pt                          # noqa: PLC0415
    doc = Document()
    for b in _blocks_of(entry):
        t = b["type"]
        if t == "heading":
            doc.add_heading(b["text"], level=b["level"])
        elif t == "paragraph":
            doc.add_paragraph(b["text"])
        elif t == "bullets":
            for x in b["items"]:
                doc.add_paragraph(x, style="List Bullet")
        elif t == "numbered":
            for x in b["items"]:
                doc.add_paragraph(x, style="List Number")
        elif t == "code":
            run = doc.add_paragraph().add_run(b["text"])
            run.font.name = "Courier New"
            run.font.size = Pt(9)
        else:
            table = doc.add_table(rows=1, cols=len(b["columns"]))
            table.style = "Table Grid"
            for i, c in enumerate(b["columns"]):
                cell = table.rows[0].cells[i]
                cell.text = c
                for r in cell.paragraphs[0].runs:
                    r.bold = True
            for row in b["rows"]:
                cells = table.add_row().cells
                for i, c in enumerate(row):
                    cells[i].text = c
    doc.save(path)


def _render_file(entry: dict, out_dir: str) -> str:
    """Write one validated file entry. Returns the path (a .zip for a
    several-sheet CSV)."""
    type_, stem = entry["type"], entry["name"]
    if type_ == "csv":
        sheets = _sheets_of(entry)
        if len(sheets) == 1:
            path = _unique_path(out_dir, stem, ".csv")
            with open(path, "wb") as f:
                f.write(render_csv(sheets[0]))
            return path
        path = _unique_path(out_dir, stem, ".zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for sh in sheets:
                member = safe_name(f"{stem} - {sh['name']}", "csv")
                z.writestr(member + ".csv", render_csv(sh))
        return path
    path = _unique_path(out_dir, stem, _EXT_OF_TYPE[type_])
    if type_ == "xlsx":
        _write_xlsx(entry, path)
    elif type_ == "docx":
        _write_docx(entry, path)
    elif type_ == "pdf":
        from . import contract                          # noqa: PLC0415
        contract._write_pdf(to_markdown(entry), path)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_markdown(entry) if type_ == "md" else to_text(entry))
    return path


def _record(path: str, entry: dict) -> dict:
    spec_path = path + ".spec.json"
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump({"version": CONTRACT_VERSION, "files": [entry]}, f,
                  ensure_ascii=False, indent=1)
    return {"path": path, "name": os.path.basename(path),
            "type": entry["type"], "kind": "document", "url": "",
            "size": os.path.getsize(path), "spec": spec_path,
            "_site_name": os.path.basename(path)}


def render(spec: dict, out_dir: str) -> list[dict]:
    """Validate and write every file of `spec` into `out_dir`. Returns one
    record per file: {"path","name","type","kind","size","spec",...}. A
    renderer failure raises FileSpecError (so it can be sent back to the model
    like any other rejection)."""
    spec = validate(spec)
    os.makedirs(out_dir, exist_ok=True)
    made: list[dict] = []
    for entry in spec["files"]:
        try:
            path = _render_file(entry, out_dir)
        except FileSpecError:
            raise
        except ImportError as e:
            raise FileSpecError(f"this computer cannot write .{entry['type']} "
                                f"files ({e.name or e})") from e
        except Exception as e:                           # noqa: BLE001
            raise FileSpecError(f"{entry['name']} could not be built: "
                                f"{type(e).__name__}: {e}") from e
        made.append(_record(path, entry))
    return made


# ── conversion from the stored spec, no AI ───────────────────────────────────

def spec_path_for(path: str) -> str:
    return path + ".spec.json"


def has_spec(path: str) -> bool:
    return os.path.isfile(spec_path_for(path))


def load_entry(path: str) -> dict:
    """The stored file entry for `path`; FileSpecError when there is none."""
    sp = spec_path_for(path)
    try:
        with open(sp, encoding="utf-8") as f:
            data = json.load(f)
        return validate(data, stored=True)["files"][0]
    except OSError:
        raise FileSpecError("Prism has no stored data for this file, so it "
                            "cannot convert it exactly") from None
    except (ValueError, KeyError, IndexError):
        raise FileSpecError("the stored data for this file is damaged") from None


def convertible_to(path: str) -> list[str]:
    """Extensions this file can be turned into from its stored spec, [] when
    there is no spec. A document with no table is not offered csv/xlsx."""
    try:
        entry = load_entry(path)
    except FileSpecError:
        return []
    out = [".pdf", ".docx"]
    try:
        _sheets_of(entry)
        out = [".csv", ".xlsx"] + out
    except FileSpecError:
        pass
    own = os.path.splitext(path)[1].lower()
    return [e for e in out if e != own]


def convert(path: str, ext: str, out_dir: str = "") -> dict:
    """Turn a file Prism made into another format, locally, from its spec.
    Returns the new record. Raises FileSpecError with a plain reason."""
    ext = ext.lower()
    if not ext.startswith("."):
        ext = "." + ext
    type_ = ext[1:]
    if type_ not in FILE_TYPES:
        raise FileSpecError(f"{ext} is not a format Prism can make")
    entry = dict(load_entry(path))
    entry["type"] = type_
    out_dir = out_dir or os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    if type_ in SHEET_TYPES:
        entry = dict(entry, sheets=_sheets_of(entry))
        entry.pop("blocks", None)
    made = _render_file(entry, out_dir)
    return _record(made, entry)


def spec_from_markdown_table(text: str, name: str, type_: str) -> dict | None:
    """The first-class use of a table the person's own text already holds:
    every Markdown table becomes a sheet (or a table block). None when there is
    no table. The old Markdown-table path, now producing a spec."""
    tables, cur = [], []
    for line in (text or "").splitlines():
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells) and cells:
                continue
            cur.append(cells)
        elif cur:
            tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    tables = [t for t in tables if len(t) >= 2]
    if not tables:
        return None
    if type_ in SHEET_TYPES:
        sheets = []
        for i, t in enumerate(tables[:MAX_SHEETS], 1):
            width = len(t[0])
            sheets.append({
                "name": f"Table {i}",
                "columns": [{"key": f"c{k}", "title": h or f"Column {k + 1}",
                             "type": "text"} for k, h in enumerate(t[0])],
                "rows": [(r + [""] * width)[:width] for r in t[1:]]})
        return {"files": [{"name": name, "type": type_, "sheets": sheets}]}
    blocks = [{"type": "table", "columns": t[0],
               "rows": [(r + [""] * len(t[0]))[:len(t[0])] for r in t[1:]]}
              for t in tables]
    return {"files": [{"name": name, "type": type_, "blocks": blocks}]}


# ── reading a file back for the reader's preview ─────────────────────────────

def preview(path: str, max_rows: int = 20, max_cols: int = 8) -> dict:
    """{"sheet","columns":[titles],"rows":[[str]],"rows_total","cols_total"}
    for the first sheet of a spreadsheet file, from its stored spec when there
    is one, else read from the file itself. {} when it cannot be read."""
    try:
        sheet = None
        if has_spec(path):
            try:
                sheet = _sheets_of(load_entry(path))[0]
            except FileSpecError:
                sheet = None
        name = ""
        if sheet is not None:
            cols = sheet["columns"]
            rows = [[_cell_text(v, c) for v, c in zip(r, cols)]
                    for r in _computed_rows(sheet)]
            titles, name = [c["title"] for c in cols], sheet["name"]
        else:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".csv":
                with open(path, newline="", encoding="utf-8-sig") as f:
                    data = [r for _, r in zip(range(5001), csv.reader(f))]
            elif ext == ".xlsx":
                from openpyxl import load_workbook      # noqa: PLC0415
                wb = load_workbook(path, read_only=True)
                ws = wb.worksheets[0]
                data = [["" if v is None else str(v) for v in r]
                        for _, r in zip(range(5001), ws.iter_rows(values_only=True))]
                name = ws.title
                wb.close()
            else:
                return {}
            if not data:
                return {}
            titles, rows = data[0], data[1:]
        return {"sheet": name, "columns": titles[:max_cols],
                "rows": [[str(x) for x in r[:max_cols]] for r in rows[:max_rows]],
                "rows_total": len(rows), "cols_total": len(titles)}
    except Exception:                                    # noqa: BLE001
        return {}


# ── the tool-call loop ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the file maker inside Prism. You make files by calling ONE tool, create_file, and your whole reply is a single JSON object, nothing else (no markdown fence, no text around it).

{"tool": "create_file",
 "message": "one short sentence for the person, in their language",
 "arguments": {"files": [ ... ]}}

If you truly cannot make the file because something essential is missing, reply {"tool": "none", "message": "what you need, in the person's language"} instead. Never invent data the person did not give; leave a cell null.

A SPREADSHEET file (type "xlsx" or "csv"):
{"name": "Quotation", "type": "xlsx",
 "sheets": [{"name": "Items",
   "columns": [{"key": "item", "title": "Item", "type": "text"},
               {"key": "qty", "title": "Qty", "type": "number"},
               {"key": "rate", "title": "Rate", "type": "currency", "symbol": "Rs"},
               {"key": "amt", "title": "Amount", "type": "currency", "symbol": "Rs"}],
   "rows": [["Cable tray", 10, 450.5, "=B2*C2"], ["Clamp", 40, 12, "=B3*C3"]],
   "widths": [28, 8, 12, 14], "freeze_header": true}]}
- column type is text, number, currency, date or percent.
- number, currency and percent cells are real JSON numbers: no quotes, no thousands commas, no currency symbol, no % sign. A percent is a fraction (0.18 is 18%). A date is "YYYY-MM-DD". Text is plain text, never Markdown.
- Row 1 is the header, so the first data row is Excel row 2. A formula is a string starting with "=", allowed ONLY in an xlsx file and ONLY in a number, currency or percent column; use only SUM, AVERAGE, MIN, MAX, COUNT, COUNTA, ROUND, ROUNDUP, ROUNDDOWN, IF, ABS, PRODUCT, SUMPRODUCT, MEDIAN, AND, OR. In a csv file write plain values, no formulas.
- Sheet names: at most 31 characters, none of [ ] : * ? / \\ , all different.
- Keep the person's own language and script (Hindi, Gujarati, ...) in the cells exactly.

A DOCUMENT file (type "docx", "pdf", "md" or "txt"):
{"name": "Site visit report", "type": "docx",
 "blocks": [{"type": "heading", "level": 1, "text": "Site visit"},
            {"type": "paragraph", "text": "..."},
            {"type": "bullets", "items": ["...", "..."]},
            {"type": "numbered", "items": ["...", "..."]},
            {"type": "table", "columns": ["Item", "Status"], "rows": [["Tray", "Done"]]},
            {"type": "code", "text": "..."}]}

File names are plain names (no folders, no ".."), at most 80 characters; the extension is added for you. Make the file the person asked for, with the format they named; if they named none, use xlsx for a table of data and docx for writing."""


@dataclass
class ToolResult:
    ok: bool = False
    files: list = field(default_factory=list)       # records from render()
    message: str = ""                                # for the person, plain
    turns: int = 0
    error: str = ""                                  # the last rejection
    fallback: bool = False                           # the Markdown-table path
    specs: list = field(default_factory=list)


def has_markdown_table(text: str) -> bool:
    lines = [l.strip() for l in (text or "").splitlines()]
    for i in range(len(lines) - 1):
        if (lines[i].startswith("|") and lines[i].endswith("|")
                and re.match(r"^\|[\s:|-]+\|$", lines[i + 1])
                and "-" in lines[i + 1]):
            return True
    return False


_NO_FILE = ("Prism could not make that file this time, so nothing was saved. "
            "Nothing wrong with your files; please try again, or say a little "
            "more about the columns you want.")


def _first_turn(task_text: str, context: str, ext_hint: str) -> str:
    parts = ["Make the file for this request by calling create_file.",
             "REQUEST:\n" + (task_text or "").strip()[:20_000]]
    if ext_hint:
        parts.append(f"The person asked for a {ext_hint} file.")
    if (context or "").strip():
        parts.append("MATERIAL TO USE (already written or attached):\n"
                     + context.strip()[:60_000])
    return "\n\n".join(parts)


def run_file_tool(ai_chat: Callable, task_text: str, attachments_context: str = "",
                  max_turns: int = 3, *, out_dir: str, role: str = "writer",
                  addon: str = "core", feature: str = "file_tool",
                  ext: str = "") -> ToolResult:
    """Ask the model to call create_file; validate and render what it sends;
    on a rejection send the exact reason back (same conversation, history
    replayed and capped like ServerSession) until `max_turns`.

    `ai_chat(role, prompt, *, system, json_mode, addon, feature)` is
    core.ai_client.chat in the app. Every turn goes through it with the same
    addon/feature, so the run id and the usage counters see each one.

    When every turn fails, the ONLY fallback is the old Markdown path, and only
    if the person's own text (request or material) already holds a Markdown
    table. Otherwise `message` says plainly that no file was made; a file is
    never faked. Never raises for a model/validation problem; an AiError from
    `ai_chat` (server refused, offline) propagates -- its words are already
    customer copy.
    """
    from .server_session import replay_prompt            # noqa: PLC0415
    ext_hint = (ext or "").lstrip(".")
    result = ToolResult()
    turns: list[tuple[str, str]] = []
    prompt = _first_turn(task_text, attachments_context, ext_hint)
    declined = ""
    for n in range(1, max(1, int(max_turns)) + 1):
        reply = ai_chat(role, replay_prompt(turns, prompt), system=SYSTEM_PROMPT,
                        json_mode=True, addon=addon, feature=feature)
        result.turns = n
        turns.append(("USER", prompt))
        turns.append(("ASSISTANT", reply))
        try:
            data = parse_json(reply)
            if isinstance(data, dict) and str(data.get("tool", "")).lower() == "none":
                declined = str(data.get("message") or "").strip()
                result.message = declined or _NO_FILE
                result.error = "declined"
                return result
            made = render(data, out_dir)
            if isinstance(data, dict):
                result.message = str(data.get("message") or "").strip()
            result.ok, result.files, result.error = True, made, ""
            return result
        except FileSpecError as e:
            result.error = str(e)
            prompt = ("Your last file spec was rejected: " + str(e)
                      + "\nFix every point and reply with the complete corrected "
                        "JSON object again (the same one tool call, nothing else).")
    # Out of turns.
    source = "\n\n".join(t for t in (task_text, attachments_context) if t)
    if has_markdown_table(source):
        type_ = (ext_hint or "xlsx").lower()
        type_ = type_ if type_ in FILE_TYPES else "xlsx"
        data = spec_from_markdown_table(source, "Table", type_)
        try:
            made = render(data, out_dir) if data else []
        except FileSpecError:
            made = []
        if made:
            result.ok, result.files, result.fallback = True, made, True
            result.message = ("The AI's file kept failing its checks, so Prism "
                              "made this one from the table already in your text.")
            return result
    result.message = _NO_FILE
    return result
