"""
Prism — /step: measure a 3D CAD model the way /gerber measures a PCB job
─────────────────────────────────────────────────────────────────────────
A customer sends a .STEP file of the product — a sheet-metal enclosure, a
plastic part — and the fab's estimator opens it in CAD and reads the
numbers off by hand: overall size, each part's size, wall thickness, every
hole. This module does that reading offline, on this machine, with the
same rules as the Gerber add-on:

  · No AI ever sees the STEP file. The customer's design is their product;
    only the measured numbers leave this module.
  · Everything shown is measured from the real geometry (OpenCascade, via
    the cadquery package) — never guessed, never generated.
  · Two decimal places everywhere a figure is shown or written.

What comes out, per part and for the assembly:

  · the formed (as-modelled) size L x W x H — NOTE: for bent sheet metal
    this is the finished part, not the unfolded flat sheet; the report
    says so, because a fab drawing often quotes the flat size;
  · a wall/sheet thickness ESTIMATE (2V/A — exact for a plain sheet, a
    little under for a part full of holes; the report calls it an
    estimate);
  · every hole, grouped by diameter with a count of distinct positions
    (a slot's two rounded ends count as two positions);
  · volume, surface area, and weight at common material densities —
    CRC steel for metal moulding, ABS/PP/PC/Nylon for plastic.

Deliverables written beside the terminal output, every one of them named
after the customer's own file — for Assem1.STEP:

  · Assem1 - dimensions.xlsx           — one row per part in the drawing's
                                         own style ("101.00 x 93.00 x 71.00
                                         mm — 1 nos") plus a hole table,
                                         readable by the estimator's Excel;
  · Assem1 - drawing sheet.png         — everything: every part's flat
                                         pattern (or formed 3-view) and
                                         isometric, one page, drawn
                                         straight to PNG (Pillow — no
                                         browser, no intermediate HTML or
                                         SVG file);
  · Assem1 - <part> - flat pattern.png — one per part that has a flat
                                         pattern, that part alone, full
                                         page, no isometric column — the
                                         image a person actually cuts from.

The name is the whole point of the prefix: an estimator keeps ten jobs'
sheets in one folder, and "dimensions.xlsx" ten times over is ten files
nobody can tell apart. See names() and output_dir() — where the files go
is the caller's choice (the GUI asks the person), the default is a folder
per model under ~/Desktop/Prism Step.
"""
from __future__ import annotations

from . import skills as _SK

import datetime as _dt
import math as _math
import os
import re
from urllib.parse import quote as _quote

try:
    import cadquery as _cq
    import numpy as _np
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
    from OCP.BRep import BRep_Tool
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import (TCollection_AsciiString,
                                 TCollection_ExtendedString)
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    HAVE_CAD = True
except Exception:                                   # pragma: no cover
    HAVE_CAD = False

try:
    import openpyxl
    HAVE_XLSX = True
except Exception:                                   # pragma: no cover
    HAVE_XLSX = False

try:
    from PIL import Image as _PILImage, ImageDraw as _PILImageDraw
    from PIL import ImageFont as _PILImageFont
    HAVE_PIL = True
except Exception:                                   # pragma: no cover
    HAVE_PIL = False


class StepError(Exception):
    """A sentence for the user, not a stack trace."""


MODES = ("metal", "plastic")

# g/cm3. The estimator multiplies volume by one of these anyway; doing it
# here saves the calculator without pretending to know the exact alloy.
_DENSITY = {
    "metal": [("CRC steel", 7.85), ("aluminium", 2.70), ("SS 304", 8.00)],
    "plastic": [("ABS", 1.04), ("PP", 0.91), ("PC", 1.20), ("Nylon 6", 1.14)],
}

# The bend-allowance K-factor: how far into the sheet's thickness the
# neutral (unstretched) bend line sits, as a fraction of thickness from the
# inside face. 0.33-0.44 covers most sheet steel/aluminium at ordinary
# bend radii; 0.4133 is the IPC/general-purpose middle of that band, used
# only when the customer's own shop has not said otherwise -- every drawing
# states which one was used, so a default is never mistaken for a
# confirmed shop value.
_DEFAULT_K_FACTOR = 0.4133

_EXTS = (".step", ".stp")

# Where a model's files go when nobody has said otherwise. The GUI asks the
# person and passes their answer as `root` to output_dir(); the terminal
# reads cfg["step_out_dir"] and falls back to this.
# expanduser("~") then join, not expanduser("~/Desktop"): the latter keeps the
# forward slash on Windows (C:\\Users\\x/Desktop), which is a different string
# from the normalised path output_dir() hands back — the one red test on the
# Windows CI lane for the STEP round.
DEFAULT_OUT_ROOT = os.path.join(os.path.expanduser("~"), "Desktop", "Prism Step")


def available() -> tuple[bool, str]:
    if not HAVE_CAD:
        return False, ("The STEP add-on needs the cadquery package "
                       "(pip install cadquery).")
    return True, ""


# ── where the files go, and what they are called ────────────────────────────

def stem_of(path_or_name: str) -> str:
    """The customer's own file name without its extension, made safe to use
    as part of a file name: 'Assem1.STEP' -> 'Assem1', '~/x/housing v2.stp'
    -> 'housing v2'. Case and spaces are kept — this is the name the person
    knows the job by, and 'assem1' is not what they called it."""
    base = os.path.splitext(os.path.basename(path_or_name or ""))[0]
    illegal = '<>:"/\\|?*'          # reserved on Windows; harmless elsewhere
    clean = "".join(c for c in base.strip() if c not in illegal and c.isprintable())
    clean = " ".join(clean.split())[:60].strip(" .")
    return clean or "model"


def _stem(report: dict) -> str:
    return report.get("stem") or stem_of(report.get("file", ""))


def names(report_or_stem) -> dict[str, str]:
    """Every deliverable's file name, each carrying the model's own name.

    One place, so the terminal, the GUI and the review page all agree on
    what a file is called and none of them writes a bare "dimensions.xlsx"
    again. Readable words with ' - ' between them, the same shape as the
    Artifacts folder's names (core.config._artifact_stem)."""
    stem = (report_or_stem if isinstance(report_or_stem, str)
            else _stem(report_or_stem))
    return {
        "xlsx":       f"{stem} - dimensions.xlsx",
        "png":        f"{stem} - drawing sheet.png",
        "review":     f"{stem} - change review.html",
        "modified":   f"{stem} - modified.step",
        "xlsx_after": f"{stem} - dimensions after change.xlsx",
        "png_after":  f"{stem} - drawing sheet after change.png",
        "after_dir":  f"{stem} - after change",
        "conversation": f"{stem} - conversation.json",
    }


def modified_name(report_or_stem) -> str:
    """The modified file's REAL name, stamped with when it was built.

    names()['modified'] is a fixed, un-stamped base — the same literal
    string on every Edit of the same model, in a FRESH numbered sibling
    folder each time (output_dir()). That left the file itself with no
    way to tell one build from another once it left its folder, or once
    two builds' folders sat side by side: both held a file named
    identically "<stem> - modified.step". This is what the worker that
    actually writes the file asks for instead — never names() itself,
    which is called more than once per build (the pre-build confirm
    message, the actual write) and would stamp a different moment each
    time if it carried the clock itself."""
    stem = (report_or_stem if isinstance(report_or_stem, str)
            else _stem(report_or_stem))
    return f"{stem} - modified {_dt.datetime.now():%Y-%m-%d %H%M%S}.step"


def flat_image_name(stem: str, part_name: str, index: int) -> str:
    """The standalone cutting image for one part's flat pattern only, no
    isometric column: 'Assem1 - top - flat pattern.png'."""
    safe = re.sub(r"[^\w.-]", "_", part_name or "") or f"part{index}"
    return f"{stem} - {safe} - flat pattern.png"


def ai_sheet_name(stem: str, n: int, ext: str = ".png") -> str:
    """What /step-auto's returned drawing is saved as."""
    return f"{stem} - AI drawing sheet {n}{ext or '.png'}"


def output_dir(target: str, root: str = "") -> str:
    """The folder one model's files go into: <root>/<stem>.

    `root` is where the person said their STEP work should live (the GUI
    asks; the terminal has /step-folder); empty means DEFAULT_OUT_ROOT. A
    folder that already holds files gets a numbered sibling — 'Assem1 (2)',
    'Assem1 (3)' — the way Finder and Explorer do it, so measuring the same
    model twice never silently overwrites the first sheet. Nothing is
    created here; the writers make the folder when they write."""
    root = os.path.abspath(os.path.expanduser(root or DEFAULT_OUT_ROOT))
    stem = stem_of(target)
    first = os.path.join(root, stem)
    if not os.path.isdir(first) or not os.listdir(first):
        return first
    n = 2
    while True:
        cand = os.path.join(root, f"{stem} ({n})")
        if not os.path.isdir(cand) or not os.listdir(cand):
            return cand
        n += 1


# ── reading the file, names kept ─────────────────────────────────────────────

def _name_of(label) -> str:
    n = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), n):
        return TCollection_AsciiString(n.Get()).ToCString()
    return ""


def load_parts(path: str) -> list[tuple[str, object]]:
    """[(part name, cadquery Shape)] with assembly placement applied.

    Through the XCAF reader rather than the plain importer, because the
    plain importer throws the part names away — and "top: 101.00 x 93.00"
    is worth a great deal more than "part 2: 101.00 x 93.00".
    """
    doc = TDocStd_Document(TCollection_ExtendedString("prism-step"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise StepError(f"Could not read {os.path.basename(path)} — is it a "
                        "STEP file?")
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    free = TDF_LabelSequence()
    tool.GetFreeShapes(free)

    parts: list[tuple[str, object]] = []
    for i in range(1, free.Length() + 1):
        root = free.Value(i)
        comps = TDF_LabelSequence()
        if tool.GetComponents_s(root, comps) and comps.Length():
            for j in range(1, comps.Length() + 1):
                comp = comps.Value(j)
                name = _name_of(comp)
                ref = TDF_Label()
                if tool.GetReferredShape_s(comp, ref):
                    name = _name_of(ref) or name
                parts.append((name or f"part {len(parts) + 1}",
                              _cq.Shape.cast(tool.GetShape_s(comp))))
        else:
            parts.append((_name_of(root) or f"part {len(parts) + 1}",
                          _cq.Shape.cast(tool.GetShape_s(root))))
    if not parts:
        raise StepError("The file holds no solid parts.")
    return parts


# ── measuring ────────────────────────────────────────────────────────────────

def _holes_of(shape) -> list[dict]:
    """Cylindrical faces grouped by diameter: [{dia_mm, positions}].

    Positions are distinct cylinder axes, so a hole counted from both its
    halves stays one hole. A slot's two rounded ends are two positions —
    honest, and the report says a slot reads that way.
    """
    groups: dict[float, set] = {}
    for f in shape.Faces():
        if f.geomType() != "CYLINDER":
            continue
        ad = BRepAdaptor_Surface(TopoDS.Face_s(f.wrapped))
        cyl = ad.Cylinder()
        dia = round(2 * cyl.Radius(), 2)
        p = cyl.Axis().Location()
        key = (round(p.X(), 1), round(p.Y(), 1), round(p.Z(), 1))
        groups.setdefault(dia, set()).add(key)
    return [{"dia_mm": dia, "count": len(axes)}
            for dia, axes in sorted(groups.items())]


# ── flat pattern (unfolding a formed sheet-metal part) ──────────────────────
#
# A formed bend, as OCCT models it, is a cylindrical strip between two flat
# faces -- two of them, actually: an inner and an outer surface, the
# thickness apart, because the material has thickness. OCCT also leaves a
# small cylindrical fillet at almost every hole's edge, which is ALSO a
# partial cylinder and would otherwise be indistinguishable from a real
# bend. The one reliable difference, found by reading a real customer
# STEP file's faces directly rather than guessing: a bend's cylindrical
# face is as long as the panel it folds (tens or hundreds of mm); a hole's
# edge fillet is only ever as long as the sheet is thick (under 1 mm on
# the sample job). `_bend_candidates` uses exactly that.
#
# Unfolding itself: walk the bend graph from the largest flat face (kept
# fixed), and at each bend rotate the NEXT flat face about the bend's own
# axis by the bend's own measured angle, so its normal ends up parallel to
# the fixed face's -- exact rigid rotation, not an approximation. The
# rotation alone leaves the two faces' edges the FORMED distance apart
# (going around the bend radius); the flat pattern needs them the FLAT
# distance apart instead, so that gap is measured after the rotation and
# corrected to the real bend-allowance figure
# (angle x (radius + K x thickness)) along the same direction the rotation
# already put them in. A part whose bends do not resolve this way --
# anything beyond straight folds between flat panels -- is left unfolded,
# not guessed at.

def _cyl_info(f) -> dict:
    """A cylindrical face's radius, angular span, axial length and axis
    line -- everything both hole detection and bend detection read off a
    CYLINDER face, in one place so the two methods cannot disagree."""
    ad = BRepAdaptor_Surface(TopoDS.Face_s(f.wrapped))
    cyl = ad.Cylinder()
    umin, umax, vmin, vmax = BRepTools.UVBounds_s(TopoDS.Face_s(f.wrapped))
    axis = cyl.Axis()
    loc, d = axis.Location(), axis.Direction()
    return {
        "face": f, "radius": cyl.Radius(), "span": umax - umin,
        "vlen": vmax - vmin,
        "axis_pt": _np.array([loc.X(), loc.Y(), loc.Z()]),
        "axis_dir": _np.array([d.X(), d.Y(), d.Z()]),
    }


def _bend_candidates(all_faces: list, thickness_mm: float) -> list[dict]:
    """Cylindrical faces long enough to be a structural bend -- see the
    note above this section for why axial length, not angular span, is
    the discriminator that actually holds up on real geometry.

    Takes the shape's faces as a list the caller already fetched, not the
    shape itself -- cadquery's `.Faces()` hands back a fresh wrapper
    object on every call, so an `id()` taken here and one taken from a
    separate `.Faces()` call elsewhere never compare equal even for the
    same underlying face. Every caller in this module shares one list."""
    min_len = max(5.0, 4 * thickness_mm)
    out = []
    for f in all_faces:
        if f.geomType() != "CYLINDER":
            continue
        info = _cyl_info(f)
        if info["vlen"] < min_len:
            continue
        if info["span"] > _math.radians(179):
            continue          # a full cylinder is a hole or a boss
        out.append(info)
    return out


def _seam_edges(face, vlen: float) -> list:
    """A bend face's two LONG edges -- the seams to the flat panel on
    each side -- as opposed to its two short curved ends."""
    return [e for e in face.Edges() if e.Length() > 0.8 * vlen]


def _same_axis_line(a: dict, b: dict, tol: float = 0.5) -> bool:
    """True when two cylindrical faces share one axis line -- the inner
    and outer surfaces of the SAME bend, not two different bends."""
    d1, d2 = a["axis_dir"], b["axis_dir"]
    if _np.linalg.norm(_np.cross(d1, d2)) > 1e-3:
        return False
    p = a["axis_pt"] - b["axis_pt"]
    return float(_np.linalg.norm(_np.cross(p, d2))) < tol


def _group_bends(all_faces: list, thickness_mm: float) -> list[dict]:
    """One entry per real bend line: its inner radius and angle (the
    smaller of the inner/outer cylindrical face pair OCCT models it
    with), and the two flat faces it connects, each with the edge that
    seams to it. A bend whose seam edge touches anything other than
    exactly one other flat face is skipped -- a face the flat pattern
    will not show, honestly, rather than a guess."""
    candidates = _bend_candidates(all_faces, thickness_mm)
    used = [False] * len(candidates)
    groups = []
    for i, c in enumerate(candidates):
        if used[i]:
            continue
        cluster = [c]
        used[i] = True
        for j in range(i + 1, len(candidates)):
            if not used[j] and _same_axis_line(c, candidates[j]):
                cluster.append(candidates[j])
                used[j] = True
        inner = min(cluster, key=lambda x: x["radius"])
        seams = _seam_edges(inner["face"], inner["vlen"])
        if len(seams) != 2:
            continue
        sides = []
        ok = True
        for e in seams:
            touching = [f for f in all_faces
                       if f.geomType() == "PLANE"
                       and not f.wrapped.IsSame(inner["face"].wrapped)
                       and any(e2.wrapped.IsSame(e.wrapped) for e2 in f.Edges())]
            if len(touching) != 1:
                ok = False
                break
            sides.append((touching[0], e))
        if not ok:
            continue
        axis_dir = inner["axis_dir"]
        groups.append({
            "radius_mm": inner["radius"], "angle_rad": inner["span"],
            "axis_pt": inner["axis_pt"],
            "axis_dir": axis_dir / _np.linalg.norm(axis_dir),
            "sides": sides,
        })
    return groups


def _bend_allowance(radius_mm: float, angle_rad: float, thickness_mm: float,
                    k_factor: float) -> float:
    """The flat length a bend uses -- the neutral axis' arc length, not
    the formed part's own (larger) outer radius or (smaller) inner one."""
    return angle_rad * (radius_mm + k_factor * thickness_mm)


def _face_normal(face) -> "_np.ndarray":
    n = face.normalAt()
    return _np.array([n.x, n.y, n.z])


def _rotation_matrix(axis_dir: "_np.ndarray", angle: float) -> "_np.ndarray":
    """Rodrigues' formula: the 3x3 rotation by `angle` about the unit
    vector `axis_dir`, through the origin."""
    ax = axis_dir / _np.linalg.norm(axis_dir)
    K = _np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    return _np.eye(3) + _math.sin(angle) * K + (1 - _math.cos(angle)) * (K @ K)


def _align_rotation(a: "_np.ndarray", b: "_np.ndarray") -> "_np.ndarray":
    """The rotation that carries unit vector `a` onto unit vector `b`."""
    a = a / _np.linalg.norm(a)
    b = b / _np.linalg.norm(b)
    v = _np.cross(a, b)
    s = float(_np.linalg.norm(v))
    c = float(_np.dot(a, b))
    if s < 1e-9:
        if c > 0:
            return _np.eye(3)
        perp = _np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else _np.array([0.0, 1.0, 0.0])
        axis = _np.cross(a, perp)
        return _rotation_matrix(axis / _np.linalg.norm(axis), _math.pi)
    K = _np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return _np.eye(3) + K + K @ K * ((1 - c) / (s * s))


class _Xf:
    """A rigid transform, p -> R @ p + t. Small enough to be a tuple, kept
    as a class so `.apply`/`.apply_vec` read as what they do at each call
    site instead of a matrix multiply that has to be checked every time."""
    __slots__ = ("R", "t")

    def __init__(self, R=None, t=None):
        self.R = R if R is not None else _np.eye(3)
        self.t = t if t is not None else _np.zeros(3)

    def apply(self, p):
        return self.R @ p + self.t

    def apply_vec(self, v):
        return self.R @ v

    @staticmethod
    def compose(outer: "_Xf", inner: "_Xf") -> "_Xf":
        """`outer` applied after `inner`."""
        return _Xf(outer.R @ inner.R, outer.R @ inner.t + outer.t)


def _rotate_about_axis(axis_pt, axis_dir, angle: float) -> _Xf:
    """A rigid transform: rotate by `angle` about the LINE through
    `axis_pt` in direction `axis_dir`, both already in the frame this
    transform will be applied in."""
    R = _rotation_matrix(axis_dir, angle)
    return _Xf(R, axis_pt - R @ axis_pt)


def _unfold_across(bend: dict, f, xf_f: _Xf, other,
                   thickness_mm: float, k_factor: float) -> _Xf | None:
    """`other`'s transform, given `f` is already correctly placed: rotate
    `other` about the bend's own axis (carried into `f`'s frame) so its
    normal matches `f`'s, then push it out along the axis-to-seam radial
    direction by the real bend-allowance distance.

    That push is exact, not measured-and-corrected: `e_here` and
    `e_there` are the bend's own two boundary edges, both at the SAME
    radius from the SAME axis (they belong to one cylindrical face), so
    rotating `other` by the bend's own measured angle always lands
    `e_there` exactly ON `e_here` -- a zero gap every time, by
    construction, whatever the bend allowance should be. Measuring "the
    gap" there and correcting toward the target — an earlier version of
    this function did exactly that — reads back a real distance and a
    real direction, but the distance is always zero and the direction is
    just floating-point noise, so the correction it computed was zero
    too, silently, for every bend on every part. The fix does not need a
    measurement: the direction to push `other` out along is simply away
    from the axis, along the line from the axis to the (coincident) seam
    point -- exact, and it does not depend on measuring a gap that never
    existed to measure."""
    (fa, ea), (fb, eb) = bend["sides"]
    if fa.wrapped.IsSame(f.wrapped):
        e_here = ea
    elif fb.wrapped.IsSame(f.wrapped):
        e_here = eb
    else:
        return None

    axis_pt_t = xf_f.apply(bend["axis_pt"])
    axis_dir_t = xf_f.apply_vec(bend["axis_dir"])
    axis_dir_t = axis_dir_t / _np.linalg.norm(axis_dir_t)

    n_other_t = xf_f.apply_vec(_face_normal(other))
    n_f_t = xf_f.apply_vec(_face_normal(f))
    cos_a = float(_np.clip(_np.dot(n_other_t, n_f_t), -1.0, 1.0))
    sin_a = float(_np.dot(_np.cross(n_other_t, n_f_t), axis_dir_t))
    angle = _math.atan2(sin_a, cos_a)

    xf_raw = _Xf.compose(_rotate_about_axis(axis_pt_t, axis_dir_t, angle), xf_f)

    p_here = xf_f.apply(_np.array([e_here.Center().x, e_here.Center().y,
                                   e_here.Center().z]))
    # The direction to push `other` further along: from the seam, toward
    # where `other`'s own body actually sits (its centre, carried through
    # the same rotation), with the along-the-fold component removed so
    # what is left is purely "how far into the panel, across the fold".
    #
    # An earlier version measured from the bend's cylinder AXIS instead of
    # from `other` itself, on the assumption the axis sits in f's own
    # plane. It does not: the bend's cylindrical face is tangent to f's
    # plane, so the axis sits one bend-radius OFF it, along f's normal —
    # and on this geometry that off-plane offset was the axis-to-seam
    # vector's ONLY real content, so removing the fold-line component left
    # nothing (or, before that, left the correction pointing off the flat
    # plane in Z instead of across it) — either way, no in-plane push, and
    # a bigger K-factor changed nothing _unfold()'s own flat_size_mm could
    # see. `other`'s own centre has no such degeneracy: it is never on the
    # fold line itself.
    p_other = xf_raw.apply(_np.array([other.Center().x, other.Center().y,
                                      other.Center().z]))
    radial = p_other - p_here
    radial = radial - _np.dot(radial, axis_dir_t) * axis_dir_t
    radial = radial - _np.dot(radial, n_f_t) * n_f_t
    r_len = float(_np.linalg.norm(radial))
    if r_len < 1e-9:
        return _Xf(xf_raw.R, xf_raw.t)
    target = _bend_allowance(bend["radius_mm"], bend["angle_rad"],
                             thickness_mm, k_factor)
    correction = target * (radial / r_len)
    return _Xf(xf_raw.R, xf_raw.t + correction)


def _flatten_faces(bends: list[dict], planar_faces: list,
                   thickness_mm: float, k_factor: float
                   ) -> tuple[dict, list[dict]]:
    """BFS from the largest flat face, placing every face the bend graph
    reaches from it. Returns ({id(face.wrapped): _Xf}, [bend, ...] the
    faces this reached were placed by)."""
    if not planar_faces:
        return {}, []
    face_by_id = {id(f.wrapped): f for f in planar_faces}
    adjacency: dict = {}
    nodes: dict = {}
    for bend in bends:
        (fa, _ea), (fb, _eb) = bend["sides"]
        ida, idb = id(fa.wrapped), id(fb.wrapped)
        if ida not in face_by_id or idb not in face_by_id:
            continue
        adjacency.setdefault(ida, []).append((bend, fb))
        adjacency.setdefault(idb, []).append((bend, fa))
        nodes[ida], nodes[idb] = fa, fb
    if not nodes:
        return {}, []

    # A solid sheet has TWO faces per panel -- the surfaces on either side
    # of the material, thickness apart, same area, opposite normals. Only
    # one of the two is ever the one a bend's cylindrical face actually
    # touches; picking root from every planar face (rather than only the
    # ones the bend graph can reach) risked picking the untouched twin and
    # finding the whole graph unreachable from it.
    root = max(nodes.values(), key=lambda f: f.Area())
    R0 = _align_rotation(_face_normal(root), _np.array([0.0, 0.0, 1.0]))
    c = _np.array([root.Center().x, root.Center().y, root.Center().z])
    xf = {id(root.wrapped): _Xf(R0, c - R0 @ c)}
    placed_bends: list[dict] = []
    queue = [root]
    while queue:
        f = queue.pop(0)
        fid = id(f.wrapped)
        for bend, other in adjacency.get(fid, ()):
            oid = id(other.wrapped)
            if oid in xf:
                continue
            xf_other = _unfold_across(bend, f, xf[fid], other,
                                      thickness_mm, k_factor)
            if xf_other is None:
                continue
            xf[oid] = xf_other
            placed_bends.append(bend)
            queue.append(other)
    return xf, placed_bends


def _wire_points(wire) -> list:
    """A wire's vertices in connected order -- BRepTools_WireExplorer,
    not a plain face/edge iteration, because those do not promise the
    order an outline needs to draw correctly."""
    pts = []
    exp = BRepTools_WireExplorer(wire.wrapped)
    while exp.More():
        v = exp.CurrentVertex()
        p = BRep_Tool.Pnt_s(v)
        pts.append(_np.array([p.X(), p.Y(), p.Z()]))
        exp.Next()
    return pts


def _holes_detail(shape, thickness_mm: float) -> list[dict]:
    """Every hole as its own record -- diameter and 3D axis point -- the
    per-instance version of _holes_of's grouped counts, for placing each
    one on the flat pattern rather than just counting them.

    Not the same face test as _holes_of: that function counts every
    cylindrical face, which on a real formed part includes the bend
    faces themselves (a bend's inner/outer radius, doubled, IS what
    shows up as an extra "hole" diameter in its table -- a pre-existing
    quirk, out of scope here). A hole drawn on the flat pattern excludes
    anything long enough to be a bend by the same test _bend_candidates
    uses, so a fold line is never drawn as a circle."""
    min_len = max(5.0, 4 * thickness_mm)
    seen = set()
    out = []
    for f in shape.Faces():
        if f.geomType() != "CYLINDER":
            continue
        ad = BRepAdaptor_Surface(TopoDS.Face_s(f.wrapped))
        cyl = ad.Cylinder()
        _umin, _umax, vmin, vmax = BRepTools.UVBounds_s(TopoDS.Face_s(f.wrapped))
        if (vmax - vmin) >= min_len:
            continue                        # a bend, not a hole
        p = cyl.Axis().Location()
        key = (round(p.X(), 1), round(p.Y(), 1), round(p.Z(), 1))
        if key in seen:
            continue
        seen.add(key)
        out.append({"dia_mm": round(2 * cyl.Radius(), 2),
                    "axis_pt": _np.array([p.X(), p.Y(), p.Z()])})
    return out


def _hole_face_of(axis_pt, placed_faces: list, thickness_mm: float):
    """Which flat face a hole belongs to: nearest first by perpendicular
    distance to the plane -- exact for a thin sheet, where a through-hole's
    axis is only ever a fraction of a millimetre off either face it passes
    through -- but on a part with several small flanges, more than one
    plane can pass close to the same point (two tabs bent to a similar
    angle, offset by only a thickness or two). Among the planes that are
    genuinely close, the tie-breaker is which one the point is actually
    NEAR IN-PLANE, by distance to that face's own centre -- a coplanar
    face on the far side of the part loses to the one the hole is really
    sitting on."""
    near = []
    for f in placed_faces:
        c = _np.array([f.Center().x, f.Center().y, f.Center().z])
        n = _face_normal(f)
        d = abs(float(_np.dot(axis_pt - c, n)))
        near.append((d, f, c))
    if not near:
        return None
    near.sort(key=lambda t: t[0])
    closest_d = near[0][0]
    candidates = [(f, c) for d, f, c in near if d < closest_d + 2 * thickness_mm]
    return min(candidates, key=lambda fc: float(_np.linalg.norm(axis_pt - fc[1])))[0]


def _is_trivial_leftover(face, placed_faces: list, thickness_mm: float) -> bool:
    """True when a face the bend graph never reached is not actually
    missing information -- verified against a real part (Assem1's "side")
    where every one of these turned out to be one of two things: the
    material's own edge-thickness wall (a sliver no wider than the sheet
    is thick, already implied by the step it walls -- the step itself IS
    in the placed face's own outline, this is just its side) or the
    untouched BACK of an already-placed panel (same shape, one thickness
    away, facing the opposite way -- a solid sheet has two faces per
    panel and only one of them is ever what a bend's cylindrical face
    touches). Both are real faces in the model; neither is a feature a
    "faces not shown" warning should be alarming anyone about."""
    bb = face.BoundingBox()
    dims = sorted(d for d in (bb.xlen, bb.ylen, bb.zlen) if d > 1e-6)
    if dims and dims[0] <= 1.5 * thickness_mm:
        return True
    if face.Area() <= max(9.0, thickness_mm * 10):
        return True
    n = _face_normal(face)
    c = _np.array([face.Center().x, face.Center().y, face.Center().z])
    for pf in placed_faces:
        if abs(pf.Area() - face.Area()) > max(1.0, 0.02 * pf.Area()):
            continue
        if _np.dot(n, _face_normal(pf)) > -0.9:
            continue
        pc = _np.array([pf.Center().x, pf.Center().y, pf.Center().z])
        gap = abs(float(_np.dot(c - pc, n)))
        if 0.2 * thickness_mm <= gap <= 4.0 * thickness_mm:
            return True
    return False


def unfold(shape, thickness_mm: float, k_factor: float = 0.0) -> dict | None:
    """The flat pattern of a formed sheet-metal part: every flat panel
    rotated back about its real bend lines into one plane, holes carried
    along at their true flat positions, each bend's flat length from the
    bend-allowance formula -- never the panel's own curved (formed)
    geometry, which is the finished part, not what a cutting list quotes.

    None when this part is not a simple bent sheet -- anything beyond
    straight folds between flat panels is left unmeasured here rather
    than approximated, the same rule _project() already follows for a
    view that cannot be drawn.
    """
    k = k_factor if k_factor > 0 else _DEFAULT_K_FACTOR
    try:
        all_faces = list(shape.Faces())     # fetched once -- see _bend_candidates
        planar = [f for f in all_faces if f.geomType() == "PLANE"]
        if len(planar) < 2:
            return None
        bends = _group_bends(all_faces, thickness_mm)
        if not bends:
            return None
        xf, placed = _flatten_faces(bends, planar, thickness_mm, k)
        if len(xf) < 2 or not placed:
            return None

        placed_faces = [f for f in planar if id(f.wrapped) in xf]
        panels = []
        cutouts = []
        all_xy = []
        for f in placed_faces:
            xfm = xf[id(f.wrapped)]
            outline = [xfm.apply(p) for p in _wire_points(f.outerWire())]
            xy = [(round(float(q[0]), 2), round(float(q[1]), 2)) for q in outline]
            panels.append(xy)
            all_xy += xy
            # A face's OUTER wire is its panel boundary; anything cut out of
            # the middle of it -- a window, a slot -- is one of its INNER
            # wires, and was never looked at here at all: a real window on
            # a real customer part (46mm square, confirmed against the
            # geometry directly) was simply invisible on the flat pattern.
            # A round hole's own inner wire is degenerate under this test
            # (its two-vertex seam collapses to ~zero width in at least one
            # direction) and is already drawn, correctly, as a circle from
            # its cylindrical face — only a wire with real extent in BOTH
            # directions is a shape this loop needs to draw for itself.
            for wire in f.innerWires():
                pts = [xfm.apply(p) for p in _wire_points(wire)]
                if len(pts) < 3:
                    continue
                cxs = [float(p[0]) for p in pts]
                cys = [float(p[1]) for p in pts]
                if max(cxs) - min(cxs) < 1.0 or max(cys) - min(cys) < 1.0:
                    continue
                cutouts.append([(round(float(p[0]), 2), round(float(p[1]), 2))
                                for p in pts])
        if not all_xy:
            return None
        xs = [p[0] for p in all_xy]
        ys = [p[1] for p in all_xy]
        x0, y0 = min(xs), min(ys)
        panels = [[(round(x - x0, 2), round(y - y0, 2)) for x, y in poly]
                  for poly in panels]
        cutouts = [[(round(x - x0, 2), round(y - y0, 2)) for x, y in poly]
                  for poly in cutouts]
        flat_w, flat_h = round(max(xs) - x0, 2), round(max(ys) - y0, 2)

        holes = []
        for h in _holes_detail(shape, thickness_mm):
            face = _hole_face_of(h["axis_pt"], placed_faces, thickness_mm)
            if face is None:
                continue
            q = xf[id(face.wrapped)].apply(h["axis_pt"])
            holes.append({"dia_mm": h["dia_mm"],
                          "x": round(float(q[0]) - x0, 2),
                          "y": round(float(q[1]) - y0, 2)})

        bend_lines = []
        for bend in placed:
            (fa, ea), (fb, eb) = bend["sides"]
            face, edge = (fa, ea) if id(fa.wrapped) in xf else (fb, eb)
            xfm = xf[id(face.wrapped)]
            p1 = xfm.apply(_np.array([edge.Center().x, edge.Center().y,
                                      edge.Center().z]))
            d = xfm.apply_vec(bend["axis_dir"])
            half = bend.get("vlen", 20.0) / 2 or 20.0
            a = p1 - d * half
            b = p1 + d * half
            bend_lines.append({
                "x1": round(float(a[0]) - x0, 2), "y1": round(float(a[1]) - y0, 2),
                "x2": round(float(b[0]) - x0, 2), "y2": round(float(b[1]) - y0, 2),
                "angle_deg": round(_math.degrees(bend["angle_rad"]), 1),
                "radius_mm": round(bend["radius_mm"], 2),
            })

        leftover = [f for f in planar if id(f.wrapped) not in xf]
        hidden_meaningful = sum(
            1 for f in leftover
            if not _is_trivial_leftover(f, placed_faces, thickness_mm))

        return {
            "flat_size_mm": (flat_w, flat_h),
            "panels": panels,
            "cutouts": cutouts,
            "holes": holes,
            "bend_lines": bend_lines,
            "k_factor": round(k, 4),
            "faces_unfolded": len(xf), "faces_total": len(planar),
            "faces_hidden_meaningful": hidden_meaningful,
        }
    except Exception:                                   # noqa: BLE001
        return None


def solve_k_factor(report: dict, part_name: str, target_mm: float,
                    axis: str = "w") -> float | None:
    """The K-factor that reproduces a flat size the shop already knows is
    correct — from a past job, from experience, from a fabricator's own
    reference sheet — instead of trusting Prism's default (or a guess)
    outright.

    Bend allowance is exactly linear in k (_bend_allowance is
    angle*radius + angle*k*thickness), and every placed point's flat
    position is a straight sum of bend-allowance terms along its own
    path back to the root panel — so the whole flat size is linear in k
    too. Two real unfolds pin that line down exactly; no search, no
    iteration.

    `report` is the dict analyse() returned for this job (it carries the
    loaded shapes under "_shapes" and each part's measured thickness).
    `axis` is "w" for the flat pattern's width, "h" for its height —
    whichever one the shop's own known figure is for. None when the part
    is not found, did not unfold, or the solved k falls outside (0, 1) —
    a target that could not really have come from this part's own
    geometry, most likely the wrong figure or the wrong part."""
    shapes = dict(report.get("_shapes") or [])
    shape = shapes.get(part_name)
    if shape is None:
        return None
    thickness = next((p["thickness_mm"] for p in report["parts"]
                      if p["name"] == part_name), None)
    if not thickness:
        return None
    idx = 0 if axis == "w" else 1
    r0 = unfold(shape, thickness, 0.1)
    r1 = unfold(shape, thickness, 0.9)
    if r0 is None or r1 is None:
        return None
    s0, s1 = r0["flat_size_mm"][idx], r1["flat_size_mm"][idx]
    if abs(s1 - s0) < 1e-6:
        return None
    k = 0.1 + (target_mm - s0) * (0.9 - 0.1) / (s1 - s0)
    if not (0.0 < k < 1.0):
        return None
    return round(k, 4)


def _measure(name: str, shape, mode: str = "metal", k_factor: float = 0.0) -> dict:
    bb = shape.BoundingBox()
    dims = sorted((bb.xlen, bb.ylen, bb.zlen), reverse=True)
    volume = shape.Volume()             # mm3
    area = shape.Area()                 # mm2
    thickness = round(2 * volume / area, 2) if area else 0.0
    return {
        "name": name,
        "size_mm": tuple(round(d, 2) for d in dims),
        # Exact for a plain sheet; slightly under for a part full of holes.
        "thickness_mm": thickness,
        "volume_cm3": round(volume / 1000.0, 2),
        "area_cm2": round(area / 100.0, 2),
        "holes": _holes_of(shape),
        # None for plastic (there is no flat pattern to a moulded part), and
        # None for a metal part whose bends did not resolve to simple folds
        # between flat panels — see unfold()'s own docstring for why that is
        # left unmeasured rather than approximated.
        "flat": unfold(shape, thickness, k_factor) if mode == "metal" else None,
    }


def analyse(path: str, mode: str = "metal", k_factor: float = 0.0) -> dict:
    """Measure every part of a STEP file. Offline; nothing leaves here.

    `k_factor` is the bend-allowance K-factor a flat pattern is unfolded
    with — the customer's own shop value when they have one, Prism's
    default (_DEFAULT_K_FACTOR) otherwise. Always stated in the report,
    so a default is never mistaken for a confirmed shop figure.
    """
    ok, why = available()
    if not ok:
        raise StepError(why)
    if mode not in MODES:
        raise StepError(f"Mode must be one of {MODES}, not {mode!r}.")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise StepError(f"No such file: {path}")
    if not path.lower().endswith(_EXTS):
        raise StepError("That is not a .step/.stp file.")

    named = load_parts(path)
    parts = [_measure(name, shape, mode, k_factor) for name, shape in named]
    parts.sort(key=lambda p: -p["volume_cm3"])

    compound = _cq.Compound.makeCompound([s for _, s in named])
    bb = compound.BoundingBox()
    overall = tuple(round(d, 2)
                    for d in sorted((bb.xlen, bb.ylen, bb.zlen), reverse=True))

    warnings = [
        "Sizes are the FORMED part as modelled — for bent sheet metal "
        "this is the finished part, not the unfolded flat sheet a "
        "cutting list quotes.",
        "Thickness is an estimate (2 x volume / surface); a part full "
        "of holes reads a little under its nominal sheet.",
        "A slot's two rounded ends count as two hole positions.",
    ]
    if mode == "metal":
        used_k = k_factor if k_factor > 0 else _DEFAULT_K_FACTOR
        source = "as entered" if k_factor > 0 else "Prism's default — confirm with the shop before quoting"
        warnings.append(f"Flat pattern bend allowance uses K-factor "
                        f"{used_k:.4f} ({source}).")
        unresolved = [p["name"] for p in parts if p["flat"] is None]
        if unresolved:
            warnings.append(
                "No flat pattern for " + ", ".join(unresolved) + " — its "
                "bends did not resolve to simple folds between flat panels; "
                "the size shown for it is the formed part only.")

    return {
        "file": os.path.basename(path),
        "path": path,
        # The name every deliverable is prefixed with — see names().
        "stem": stem_of(path),
        "mode": mode,
        "parts": parts,
        "overall_mm": overall,
        "k_factor": round(k_factor if k_factor > 0 else _DEFAULT_K_FACTOR, 4),
        "_shapes": named,       # for the renderer; stripped before saving
        "warnings": warnings,
    }


# ── words and numbers out ────────────────────────────────────────────────────

def _weights(volume_cm3: float, mode: str) -> str:
    return " · ".join(f"{name} {volume_cm3 * dens:.2f} g"
                      for name, dens in _DENSITY[mode])


def _caption(part: dict, mode: str) -> str:
    L, W, H = part["size_mm"]
    stock = (f"t≈{part['thickness_mm']:.2f} mm sheet" if mode == "metal"
             else f"wall≈{part['thickness_mm']:.2f} mm")
    return f"{L:.2f} x {W:.2f} x {H:.2f} mm · {stock} — 1 nos"


def report_text(report: dict) -> str:
    lines = [f"STEP MEASUREMENT — {report['file']}   "
             f"({report['mode']} moulding)",
             "",
             f"  Assembly overall    {report['overall_mm'][0]:.2f} x "
             f"{report['overall_mm'][1]:.2f} x {report['overall_mm'][2]:.2f} mm",
             f"  Parts               {len(report['parts'])}",
             ""]
    for i, part in enumerate(report["parts"], 1):
        lines.append(f"  {i}) {part['name']}  —  {_caption(part, report['mode'])}")
        lines.append(f"     volume {part['volume_cm3']:.2f} cm3 · "
                     f"weight {_weights(part['volume_cm3'], report['mode'])}")
        if part["holes"]:
            lines.append("     holes  " + " · ".join(
                f"Ø{h['dia_mm']:g} x {h['count']}" for h in part["holes"]))
        lines.append("")
    for w in report["warnings"]:
        lines.append(f"  ! {w}")
    lines.append("")
    lines.append("  Measured offline from the geometry itself — the STEP "
                 "file never leaves this machine and no AI ever sees it.")
    return "\n".join(lines)


def write_xlsx(report: dict, path: str) -> str:
    """The estimator's sheet: one row per part, then every hole."""
    if not HAVE_XLSX:
        raise StepError("Writing Excel needs the openpyxl package.")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Parts"
    bold = openpyxl.styles.Font(bold=True)

    ws.append([f"{report['file']} — {report['mode']} moulding — "
               f"measured {_dt.date.today().strftime('%d-%m-%Y')}"])
    ws["A1"].font = bold
    ws.append([])
    header = ["Sr", "Part", "L (mm)", "W (mm)", "H (mm)",
              "Thickness est (mm)", "Volume (cm3)",
              "Weight (g)", "Holes", "Line for the drawing",
              # Appended, not inserted -- every existing column keeps its
              # letter, so a customer's own formula referencing column C or
              # H does not silently start pointing at the wrong figure.
              "Flat L (mm)", "Flat W (mm)"]
    ws.append(header)
    for cell in ws[ws.max_row]:
        cell.font = bold
    first = _DENSITY[report["mode"]][0]
    for i, part in enumerate(report["parts"], 1):
        L, W, H = part["size_mm"]
        holes = " · ".join(f"Ø{h['dia_mm']:g} x {h['count']}"
                           for h in part["holes"]) or "—"
        flat = part.get("flat")
        flat_l, flat_w = flat["flat_size_mm"] if flat else ("", "")
        ws.append([i, part["name"], L, W, H, part["thickness_mm"],
                   part["volume_cm3"],
                   round(part["volume_cm3"] * first[1], 2),
                   holes, f"{i}) {_caption(part, report['mode'])}",
                   flat_l, flat_w])
    ws.append([])
    ws.append(["Assembly overall",
               f"{report['overall_mm'][0]:.2f} x {report['overall_mm'][1]:.2f}"
               f" x {report['overall_mm'][2]:.2f} mm"])
    ws.append([f"Weight column uses {first[0]} at {first[1]} g/cm3."])
    if report["mode"] == "metal":
        ws.append([f"Flat L/W use K-factor {report.get('k_factor', _DEFAULT_K_FACTOR):.4f} "
                   "— blank where the part's bends did not resolve to "
                   "simple folds (see the warnings below)."])
    for w in report["warnings"]:
        ws.append([f"! {w}"])
    for col, width in zip("ABCDEFGHIJKL",
                          (4, 14, 10, 10, 10, 16, 12, 12, 40, 44, 12, 12)):
        ws.column_dimensions[col].width = width

    holes_ws = wb.create_sheet("Holes")
    holes_ws.append(["Part", "Diameter (mm)", "Positions"])
    for cell in holes_ws[1]:
        cell.font = bold
    for part in report["parts"]:
        for h in part["holes"]:
            holes_ws.append([part["name"], h["dia_mm"], h["count"]])
    for col, width in zip("ABC", (14, 14, 10)):
        holes_ws.column_dimensions[col].width = width

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    wb.save(path)
    return path


# ── the dimensioned drawing sheet — drawn by Prism, no AI ──────────────────
#
# What the fab's estimator actually hands round is a dimension sheet: three
# orthographic views of each part with the overall sizes on dimension lines,
# a hole table, notes and a title block. /step-auto used to ask an image
# model for that from the measured numbers, which took minutes and came back
# looking right while quietly mis-stating a figure. Every line below is
# projected from the real geometry (OpenCascade hidden-line removal), every
# number is the measured one to two decimals, and it takes a second.

# The three standard views. `n` is the direction the camera looks FROM (the
# convention cadquery's exporter uses), `x` the axis that reads left-to-right
# on paper — fixed explicitly so Z is always up in the front and side views
# and Y is up in the top view, instead of whatever OpenCascade picks.
_VIEWS = (
    ("FRONT VIEW", (0, -1, 0), (1, 0, 0), ("x", "z")),
    ("TOP VIEW",   (0, 0, 1),  (1, 0, 0), ("x", "y")),
    ("SIDE VIEW",  (1, 0, 0),  (0, 1, 0), ("y", "z")),
)


def _project(shape, n, x=None) -> dict | None:
    """One orthographic view of a shape: {"visible": [svg path d…],
    "hidden": [...], "bb": (xmin, xmax, ymin, ymax)} in model millimetres,
    hidden lines removed the way a drawing office does it. None when the
    projection fails — a view that cannot be drawn must not sink the sheet."""
    try:
        from cadquery.occ_impl.exporters.svg import getPaths
        from cadquery.occ_impl.shapes import TOLERANCE, Shape, Compound
        from OCP.BRepLib import BRepLib
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        from OCP.HLRAlgo import HLRAlgo_Projector
        from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

        hlr = HLRBRep_Algo()
        hlr.Add(shape.wrapped)
        cs = (gp_Ax2(gp_Pnt(), gp_Dir(*n), gp_Dir(*x)) if x
              else gp_Ax2(gp_Pnt(), gp_Dir(*n)))
        hlr.Projector(HLRAlgo_Projector(cs))
        hlr.Update()
        hlr.Hide()
        hs = HLRBRep_HLRToShape(hlr)
        visible = [c for c in (hs.VCompound(), hs.Rg1LineVCompound(),
                               hs.OutLineVCompound()) if not c.IsNull()]
        hidden = [c for c in (hs.HCompound(), hs.OutLineHCompound())
                  if not c.IsNull()]
        for el in visible + hidden:
            BRepLib.BuildCurves3d_s(el, TOLERANCE)
        visible = [Shape(c) for c in visible]
        hidden = [Shape(c) for c in hidden]
        hidden_paths, visible_paths = getPaths(visible, hidden)
        if not visible_paths and not hidden_paths:
            return None
        bb = Compound.makeCompound(hidden + visible).BoundingBox()
        return {"visible": visible_paths, "hidden": hidden_paths,
                "bb": (bb.xmin, bb.xmax, bb.ymin, bb.ymax)}
    except Exception:                               # noqa: BLE001
        return None


def _extents(shape) -> dict:
    bb = shape.BoundingBox()
    return {"x": bb.xlen, "y": bb.ylen, "z": bb.zlen}


def _svg_path_points(d: str) -> list:
    """An SVG path's `d` string as a list of polylines (one per subpath).

    cadquery's own getPaths() (used by _project) has already flattened
    every curve to short line segments before handing the path back --
    checked directly against this file's own real sample, every path it
    produces uses only M (move) and L (line), never a curve command --
    so a plain M/L reader is enough; nothing here approximates an arc,
    it only ever has to read straight ones cadquery already drew."""
    subpaths: list = []
    cur: list | None = None
    for m in re.finditer(r"([ML])\s*(-?[\d.]+)[,\s]+(-?[\d.]+)", d):
        cmd, x, y = m.group(1), float(m.group(2)), float(m.group(3))
        if cmd == "M" or cur is None:
            cur = [(x, y)]
            subpaths.append(cur)
        else:
            cur.append((x, y))
    return subpaths


class _Sheet:
    """An SVG page being drawn, in pixels. Small helpers so the layout code
    reads as a drawing, not as string concatenation."""

    W = 1240                    # A4 portrait at ~150 dpi; the height grows
    M = 44                      # page margin
    FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"

    def __init__(self):
        self.items: list[str] = []
        self.y = self.M         # the next free row

    def text(self, x, y, s, size=13, anchor="start", weight="normal",
             fill="#111", rotate=None):
        import html as _html
        tr = f' transform="rotate({rotate} {x} {y})"' if rotate else ""
        self.items.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
            f'text-anchor="{anchor}" font-weight="{weight}" fill="{fill}"'
            f'{tr}>{_html.escape(str(s))}</text>')

    def line(self, x1, y1, x2, y2, w=1, color="#111", dash=""):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{w}"{d}/>')

    def rect(self, x, y, w, h, stroke="#111", fill="none", sw=1):
        self.items.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'stroke="{stroke}" fill="{fill}" stroke-width="{sw}"/>')

    def polygon(self, points, fill="none", stroke="#111", sw=1):
        """A closed outline from (x, y) pixel points — a panel edge, a
        cutout. Its own method (not a raw items.append) so the same call
        works against _Canvas's raster backend too."""
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points) + " Z"
        self.items.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}" '
                          f'stroke-width="{sw}"/>')

    def circle(self, cx, cy, r, fill="none", stroke="#111", sw=1):
        self.items.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}"/>')

    def arrow(self, x, y, dx, dy):
        """A filled arrowhead at (x, y) pointing along (dx, dy)."""
        import math
        L, Wd = 9.0, 3.2
        ang = math.atan2(dy, dx)
        bx, by = x - L * math.cos(ang), y - L * math.sin(ang)
        px, py = -math.sin(ang) * Wd, math.cos(ang) * Wd
        self.items.append(
            f'<polygon points="{x:.1f},{y:.1f} {bx + px:.1f},{by + py:.1f} '
            f'{bx - px:.1f},{by - py:.1f}" fill="#111"/>')

    def view(self, proj: dict, x0, y0, scale, w_px, h_px):
        """Draw a projection into the box at (x0, y0) of w_px x h_px, model
        units scaled by `scale`, centred. Returns the drawn extents
        (left, top, right, bottom) in px for the dimension lines."""
        xmin, xmax, ymin, ymax = proj["bb"]
        dw, dh = (xmax - xmin) * scale, (ymax - ymin) * scale
        left = x0 + (w_px - dw) / 2
        top = y0 + (h_px - dh) / 2
        # SVG y runs down the page; model y runs up. Flip about the box.
        tx = left - xmin * scale
        ty = top + ymax * scale
        g = (f'<g transform="translate({tx:.2f},{ty:.2f}) '
             f'scale({scale:.4f},{-scale:.4f})" fill="none" '
             f'vector-effect="non-scaling-stroke">')
        sw = 1.1 / scale
        g += (f'<g stroke="#8a8f94" stroke-width="{sw:.3f}" '
              f'stroke-dasharray="{3 / scale:.3f},{2 / scale:.3f}">'
              + "".join(f'<path d="{d}"/>' for d in proj["hidden"]) + "</g>")
        g += (f'<g stroke="#111" stroke-width="{sw:.3f}">'
              + "".join(f'<path d="{d}"/>' for d in proj["visible"]) + "</g>")
        g += "</g>"
        self.items.append(g)
        return left, top, left + dw, top + dh

    # Below this many pixels a figure no longer fits between its own
    # arrowheads; the arrows go outside and the figure beside them, the way
    # a draughtsman dimensions a sheet edge.
    NARROW = 46

    def dim_h(self, left, right, y, value):
        """A horizontal dimension under a view: extension ticks, a line with
        arrowheads, the figure above it. `value` in mm, two decimals."""
        self.line(left, y - 12, left, y + 4, w=0.8)
        self.line(right, y - 12, right, y + 4, w=0.8)
        label = f"{value:.2f}"
        if right - left >= self.NARROW:
            self.line(left, y, right, y, w=0.9)
            self.arrow(left, y, -1, 0)
            self.arrow(right, y, 1, 0)
            self.text((left + right) / 2, y - 5, label, size=13,
                      anchor="middle")
        else:
            self.line(left - 16, y, right + 16, y, w=0.9)
            self.arrow(left, y, 1, 0)
            self.arrow(right, y, -1, 0)
            self.text(right + 22, y + 4, label, size=13, anchor="start")

    def dim_v(self, top, bottom, x, value):
        """A vertical dimension beside a view, the figure reading upward."""
        self.line(x - 12, top, x + 4, top, w=0.8)
        self.line(x - 12, bottom, x + 4, bottom, w=0.8)
        label = f"{value:.2f}"
        if bottom - top >= self.NARROW:
            self.line(x, top, x, bottom, w=0.9)
            self.arrow(x, top, 0, -1)
            self.arrow(x, bottom, 0, 1)
            self.text(x + 14, (top + bottom) / 2 + 4, label, size=13,
                      anchor="middle", rotate=-90)
        else:
            self.line(x, top - 16, x, bottom + 16, w=0.9)
            self.arrow(x, top, 0, 1)
            self.arrow(x, bottom, 0, -1)
            self.text(x, bottom + 32, label, size=13, anchor="middle")

    def svg(self, height) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.W}" '
                f'height="{height:.0f}" viewBox="0 0 {self.W} {height:.0f}" '
                f'font-family="{self.FONT}"><rect width="100%" height="100%" '
                'fill="#fff"/>' + "".join(self.items) + "</svg>")


def _step_font_path(bold: bool = False) -> str:
    """Prism's own bundled body font — the same asset footage.py already
    relies on for Reel's own PIL-drawn text, so no new font dependency is
    introduced. Empty when even that is missing (a stripped-down checkout);
    _Canvas falls back to Pillow's own built-in font rather than fail."""
    name = "Barlow-Bold.ttf" if bold else "Barlow-Regular.ttf"
    bundled = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "assets", "fonts", name))
    return bundled if os.path.isfile(bundled) else ""


class _Canvas:
    """The same page _Sheet lays out, rasterised straight to a PIL image --
    no browser, no SVG renderer, nothing beyond the Pillow dependency Reel
    already requires. Every public method matches _Sheet's own signature,
    so _draw_flat_pattern, _draw_part, _title_block and _lay_out_sheet run
    against either backend unmodified — draw calls are only recorded here
    (as _Sheet records SVG strings) and actually rasterised in image(),
    once the final page height is known."""

    W = _Sheet.W
    M = _Sheet.M
    NARROW = _Sheet.NARROW
    SCALE = 2                   # supersample, then downsize -- crisper
                                # text and lines than drawing at 1:1 would.

    def __init__(self):
        self.y = self.M
        self._ops: list = []
        self._fonts: dict = {}

    # Borrowed unbound: both only ever call self.line/self.arrow/self.text,
    # which this class implements with matching signatures.
    dim_h = _Sheet.dim_h
    dim_v = _Sheet.dim_v

    def text(self, x, y, s, size=13, anchor="start", weight="normal",
             fill="#111", rotate=None):
        self._ops.append(("text", x, y, str(s), size, anchor,
                          weight == "bold", fill, rotate))

    def line(self, x1, y1, x2, y2, w=1, color="#111", dash=""):
        self._ops.append(("line", x1, y1, x2, y2, w, color, dash))

    def rect(self, x, y, w, h, stroke="#111", fill="none", sw=1):
        self._ops.append(("rect", x, y, w, h, stroke, fill, sw))

    def polygon(self, points, fill="none", stroke="#111", sw=1):
        self._ops.append(("polygon", list(points), fill, stroke, sw))

    def circle(self, cx, cy, r, fill="none", stroke="#111", sw=1):
        self._ops.append(("circle", cx, cy, r, fill, stroke, sw))

    def arrow(self, x, y, dx, dy):
        L, Wd = 9.0, 3.2
        ang = _math.atan2(dy, dx)
        bx, by = x - L * _math.cos(ang), y - L * _math.sin(ang)
        px, py = -_math.sin(ang) * Wd, _math.cos(ang) * Wd
        self._ops.append(("polygon", [(x, y), (bx + px, by + py),
                                      (bx - px, by - py)], "#111", "#111", 0))

    def view(self, proj: dict, x0, y0, scale, w_px, h_px):
        """Same contract as _Sheet.view: draws the projection, returns its
        drawn extents. getPaths()'s own path strings are read back into
        point lists by _svg_path_points -- see that function for why a
        plain M/L reader is enough here."""
        xmin, xmax, ymin, ymax = proj["bb"]
        dw, dh = (xmax - xmin) * scale, (ymax - ymin) * scale
        left = x0 + (w_px - dw) / 2
        top = y0 + (h_px - dh) / 2
        tx = left - xmin * scale
        ty = top + ymax * scale
        for d, color, dash in ((p, "#8a8f94", (3, 2)) for p in proj["hidden"]):
            for sub in _svg_path_points(d):
                pts = [(tx + px * scale, ty - py * scale) for px, py in sub]
                self._ops.append(("polyline", pts, color, 1.1, dash))
        for d in proj["visible"]:
            for sub in _svg_path_points(d):
                pts = [(tx + px * scale, ty - py * scale) for px, py in sub]
                self._ops.append(("polyline", pts, "#111", 1.1, None))
        return left, top, left + dw, top + dh

    def _font(self, size, bold):
        key = (round(size), bold)
        if key in self._fonts:
            return self._fonts[key]
        path = _step_font_path(bold)
        px = max(1, round(size * self.SCALE))
        f = (_PILImageFont.truetype(path, px) if path
             else _PILImageFont.load_default(size=px))
        self._fonts[key] = f
        return f

    @staticmethod
    def _dashed(draw, pts, color, width, dash):
        """A polyline with an SVG-style (on, off) dasharray -- PIL has no
        native dashed stroke, so each segment is walked and only the 'on'
        portion of every dash cycle is actually drawn."""
        on, off = dash
        cycle = on + off
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            seg = _math.hypot(x2 - x1, y2 - y1)
            if seg < 1e-6:
                continue
            ux, uy = (x2 - x1) / seg, (y2 - y1) / seg
            d = 0.0
            while d < seg:
                d2 = min(d + on, seg)
                draw.line([(x1 + ux * d, y1 + uy * d),
                          (x1 + ux * d2, y1 + uy * d2)],
                         fill=color, width=width)
                d += cycle

    def image(self, height):
        """Rasterises every recorded op onto a white page of exactly
        (W, height) — supersampled then downsized, the way Reel's own
        title cards are drawn, for text and thin lines that hold up when
        someone actually zooms into the cutting drawing."""
        s = self.SCALE
        h_px = max(int(round(height)) + 20, 60) * s
        img = _PILImage.new("RGB", (self.W * s, h_px), "white")
        draw = _PILImageDraw.Draw(img)

        def col(c):
            return c if c != "none" else None

        for op in self._ops:
            kind = op[0]
            if kind == "text":
                _, x, y, text, size, anchor, bold, fill, rotate = op
                font = self._font(size, bold)
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                ascent, _descent = font.getmetrics()
                px, py = x * s, y * s - ascent
                if anchor == "middle":
                    px -= tw / 2
                elif anchor == "end":
                    px -= tw
                if rotate:
                    # The only rotated text this file ever draws is
                    # dim_v's vertical figure, anchor="middle" at
                    # rotate=-90 -- so (x, y) is already that text's own
                    # centre once turned vertical, and pasting the
                    # rotated layer centred on it is exact, not a
                    # general-angle approximation.
                    #
                    # textbbox's own top-left (bbox[0], bbox[1]) is not
                    # (0, 0) -- a font's internal ascent/leading offsets
                    # it -- so drawing flush at (4, 4) without correcting
                    # for that clipped the glyphs against this tightly
                    # sized layer; drawing at (4 - bbox[0], 4 - bbox[1])
                    # lands the actual ink there instead.
                    layer = _PILImage.new("RGBA",
                                          (int(tw) + 8, int(th) + 8), (0, 0, 0, 0))
                    ld = _PILImageDraw.Draw(layer)
                    ld.text((4 - bbox[0], 4 - bbox[1]), text, font=font,
                           fill=col(fill))
                    rotated = layer.rotate(-90, expand=True,
                                           resample=_PILImage.BICUBIC)
                    img.paste(rotated,
                             (int(x * s - rotated.width / 2),
                              int(y * s - rotated.height / 2)), rotated)
                else:
                    draw.text((px, py), text, font=font, fill=col(fill))
            elif kind == "line":
                _, x1, y1, x2, y2, w, color, dash = op
                width = max(1, round(w * s))
                p1, p2 = (x1 * s, y1 * s), (x2 * s, y2 * s)
                if dash:
                    parts = [float(v) for v in str(dash).split(",")]
                    on = parts[0] * s
                    off = (parts[1] if len(parts) > 1 else parts[0]) * s
                    self._dashed(draw, [p1, p2], color, width, (on, off))
                else:
                    draw.line([p1, p2], fill=color, width=width)
            elif kind == "rect":
                _, x, y, w, h, stroke, fill, sw = op
                draw.rectangle([x * s, y * s, (x + w) * s, (y + h) * s],
                               outline=stroke, fill=col(fill),
                               width=max(1, round(sw * s)))
            elif kind == "polygon":
                _, points, fill, stroke, sw = op
                pts = [(px * s, py * s) for px, py in points]
                draw.polygon(pts, outline=stroke, fill=col(fill))
            elif kind == "circle":
                _, cx, cy, r, fill, stroke, sw = op
                bb = [(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s]
                draw.ellipse(bb, outline=stroke, fill=col(fill),
                            width=max(1, round(sw * s)))
            elif kind == "polyline":
                _, pts, color, w, dash = op
                spts = [(px * s, py * s) for px, py in pts]
                width = max(1, round(w * s))
                if dash:
                    self._dashed(draw, spts, color, width,
                                (dash[0] * s, dash[1] * s))
                elif len(spts) >= 2:
                    draw.line(spts, fill=color, width=width, joint="curve")
        if s != 1:
            img = img.resize((self.W, h_px // s), _PILImage.LANCZOS)
        return img


def _draw_part(sh: _Sheet, index: int, part: dict, shape, mode: str) -> None:
    """One part's band: caption and isometric on the left, front over top in
    the middle, side over the hole table on the right — the layout of the
    hand-made sheet, and of the sheet the image model used to draw."""
    M, W = sh.M, sh.W
    y0 = sh.y
    ext = _extents(shape)
    col1, col2, col3 = M, 400, 830
    vw, vh = 350, 230                 # each view's box
    # One scale for all three views of a part, so the front's width and the
    # top's width are the same line — and never larger than the box allows.
    pad = 1.0
    scale = min(vw / (ext["x"] + pad), vw / (ext["y"] + pad),
                vh / (ext["y"] + pad), vh / (ext["z"] + pad),
                6.0)                  # never blow a tiny part up past 6 px/mm
    scale = max(scale, 0.05)

    # ── caption + isometric ───────────────────────────────────────────
    sh.text(col1, y0 + 22, f"{index}) {part['name'].upper()} — 1 NOS",
            size=19, weight="bold")
    stock = (f"t≈{part['thickness_mm']:.2f} mm SHEET" if mode == "metal"
             else f"wall≈{part['thickness_mm']:.2f} mm")
    sh.text(col1 + 10, y0 + 44, stock, size=13, fill="#333")
    iso = _project(shape, (1, -1.2, 1))
    if iso:
        xmin, xmax, ymin, ymax = iso["bb"]
        s_iso = min(300 / max(xmax - xmin, 1e-6), 240 / max(ymax - ymin, 1e-6),
                    scale * 0.85)
        sh.view(iso, col1, y0 + 60, s_iso, 320, 250)

    # ── front over top ────────────────────────────────────────────────
    y_front = y0 + 30
    y_top = y_front + vh + 70
    y_side = y_front
    boxes = {"FRONT VIEW": (col2, y_front), "TOP VIEW": (col2, y_top),
             "SIDE VIEW": (col3, y_side)}
    bottom_used = y_top + vh + 40
    for title, n, xdir, (ax_w, ax_h) in _VIEWS:
        bx, by = boxes[title]
        sh.text(bx + vw / 2, by - 8, title, size=13, anchor="middle",
                weight="bold", fill="#222")
        proj = _project(shape, n, xdir)
        if not proj:
            sh.text(bx + vw / 2, by + vh / 2, "(view could not be drawn)",
                    size=12, anchor="middle", fill="#8a9098")
            continue
        left, top, right, bottom = sh.view(proj, bx, by, scale, vw, vh)
        sh.dim_h(left, right, bottom + 24, ext[ax_w])
        sh.dim_v(top, bottom, right + 26, ext[ax_h])

    # ── hole table under the side view ────────────────────────────────
    ty = y_side + vh + 70
    tx = col3 + 40
    rows = part["holes"]
    sh.text(tx, ty, f"HOLES ({part['name'].upper()})", size=13, weight="bold")
    if rows:
        for k, h in enumerate(rows):
            sh.text(tx, ty + 22 + k * 19, f"Ø{h['dia_mm']:g} x {h['count']}",
                    size=13)
        table_h = 30 + len(rows) * 19
    else:
        sh.text(tx, ty + 22, "no holes", size=13, fill="#555")
        table_h = 50
    sh.rect(tx - 12, ty - 18, 220, table_h, stroke="#777")
    # weight line, under the caption side
    sh.text(col1 + 10, y0 + 330, f"volume {part['volume_cm3']:.2f} cm3 · "
            f"{_weights(part['volume_cm3'], mode)}", size=11.5, fill="#444")

    band_bottom = max(bottom_used, ty + table_h + 10, y0 + 345)
    sh.line(M, band_bottom, W - M, band_bottom, w=1.2, color="#222")
    sh.y = band_bottom + 22


def _cluster(values: list[float], tol: float = 1.0) -> list[float]:
    """Nearby numbers merged into one representative value each — so ten
    holes down one row read as one dimension line, the way a draughtsman
    would, not ten near-duplicate ones stacked on each other."""
    out: list[float] = []
    for v in sorted(values):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


_CHAIN_MAX_STOPS = 9      # 0, up to 7 interior stops, and the far edge


def _chain_points(values: list[float], span: float, min_gap: float = 0.5
                  ) -> list[float] | None:
    """The stops a dimension CHAIN runs between: 0, every clustered
    feature position, and the far edge — so what gets drawn is the GAP
    between one feature and the next (26.5 -> 90.5 -> 50 -> 3.5, summing
    to the whole), the way the reference sheet dimensions it, not each
    feature's bare distance from a datum that a reader has to subtract by
    hand to find the distance between two holes.

    Both edges (0 and `span`) always survive thinning, even when the
    nearest real feature sits within `min_gap` of one — dropping the
    edge itself, which an earlier version did, silently shortened the
    whole chain (it stopped 2 mm short of the part's own measured
    height, on a real job, because the last vent hole happened to sit
    just inside that tolerance of the top edge). When a feature is too
    close to an edge to get its own segment, the FEATURE is the one that
    gives way, folded into the edge segment instead.

    None when there would be more stops than a chain can show without
    the labels running into each other regardless of clustering — a
    ventilation grille's slot row, not a handful of mounting holes; the
    caller falls back to leaving it undimensioned rather than drawing an
    unreadable comb of digits."""
    pts = sorted(set(round(v, 2) for v in values))
    pts = [p for p in pts if min_gap < p < span - min_gap]
    thinned = [0.0]
    for p in pts:
        if p - thinned[-1] > min_gap:
            thinned.append(p)
    span = round(span, 2)
    if thinned[-1] >= span - min_gap:
        thinned[-1] = span
    else:
        thinned.append(span)
    if len(thinned) > _CHAIN_MAX_STOPS:
        return None
    return thinned


def _label_box(x: float, y: float, text: str, size: float,
              anchor: str) -> tuple:
    """A red callout's approximate footprint in page pixels — not exact
    font metrics (those are a drawing-time concern, resolved separately
    by each backend), just consistent enough to catch the real collision
    this exists for: two different features' labels landing in the same
    few pixels on a real generated job (a hole's diameter and a small
    cutout's size, both placed 'beside their own shape' with no idea the
    other one was there)."""
    w = 0.58 * size * len(text)
    h = size * 1.15
    if anchor == "middle":
        x -= w / 2
    return (x, y - h * 0.82, x + w, y + h * 0.18)


def _boxes_overlap(a: tuple, b: tuple, pad: float = 1.5) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0]
               or a[3] + pad < b[1] or b[3] + pad < a[1])


def _place_label(anchor_pt: tuple, default_pt: tuple, text: str,
                 size: float, anchor_mode: str, placed: list) -> tuple:
    """Where a callout actually lands: flush at its own feature's default
    spot when nothing else claims that ground, pushed further out along
    a leader line -- first straight along the same direction, then
    fanned out to other compass directions -- the first time it would
    collide with a label already placed. `placed` collects every label's
    box as it is resolved, so each new one avoids every earlier one, not
    just its own immediate neighbour. Returns (x, y, needed_a_leader)."""
    ax, ay = anchor_pt
    dx0, dy0 = default_pt[0] - ax, default_pt[1] - ay
    dist = max(_math.hypot(dx0, dy0), 1.0)
    ux, uy = dx0 / dist, dy0 / dist
    tried = [(ax + ux * dist * m, ay + uy * dist * m)
             for m in (1.0, 2.2, 3.6, 5.2, 7.0)]
    for deg in (45, -45, 90, -90, 135, -135, 180):
        rad = _math.radians(deg)
        rux = ux * _math.cos(rad) - uy * _math.sin(rad)
        ruy = ux * _math.sin(rad) + uy * _math.cos(rad)
        tried.append((ax + rux * dist * 3.2, ay + ruy * dist * 3.2))
    for i, (cx, cy) in enumerate(tried):
        box = _label_box(cx, cy, text, size, anchor_mode)
        if not any(_boxes_overlap(box, b) for b in placed):
            placed.append(box)
            return cx, cy, i > 0
    # Nothing tried was clear -- every real feature still gets a number
    # rather than being silently dropped; the furthest straight-line try
    # is the least crowded of the ones already checked.
    cx, cy = tried[4]
    placed.append(_label_box(cx, cy, text, size, anchor_mode))
    return cx, cy, True


def _draw_flat_pattern(sh: _Sheet, index: int, part: dict, shape,
                       with_isometric: bool = True) -> None:
    """The section a shop-floor worker actually reads: the flat sheet a
    cutting list quotes from, on its own — not folded into a table of
    figures they have to cross-reference. Every hole carries its own
    diameter right beside it, and its position is dimensioned from the
    panel's own edges, the way the customer's own reference sheet does
    it — because the person cutting this is not assumed to be someone
    who already knows to go look a code up in a legend.

    `with_isometric=False` drops the isometric column and gives the flat
    pattern the whole page width instead — the per-part cutting image
    _render_flat_image builds, where a 3D reference view is not the
    point and the extra width means a bigger, clearer drawing."""
    flat = part["flat"]
    M, W = sh.M, sh.W
    y0 = sh.y
    fw, fh = flat["flat_size_mm"]

    sh.text(M, y0 + 22, f"{index}) {part['name'].upper()}", size=19,
            weight="bold")
    sh.text(M + 10, y0 + 44,
            f"{fw:.2f} x {fh:.2f} x {part['thickness_mm']:.2f} mm crc — 1 nos",
            size=13, fill="#333")
    y0 += 56

    # ── two clearly separated sections, side by side (or one, full width
    # and full height, when there is no isometric column to share the
    # page with — a standalone cutting image gets the whole page, not
    # just the corner the combined sheet's layout would give it) ───────
    split = (W - M - 340) if with_isometric else (W - M)
    box_h_avail = 340 if with_isometric else 560
    if with_isometric:
        sh.text((M + split) / 2, y0 + 14, "FLAT PATTERN", size=14,
                anchor="middle", weight="bold", fill="#222")
        sh.text((split + 20 + W - M) / 2, y0 + 14, "ISOMETRIC", size=14,
                anchor="middle", weight="bold", fill="#222")
        sh.line(split + 10, y0, split + 10, y0 + 420, w=1, color="#ccc")

    # Reserve a dimensioning margin on the top and left of the outline for
    # the position CHAIN — one row/column, the width a dim_h/dim_v span
    # needs, not the several staggered rows an earlier version needed to
    # keep individual absolute-position labels from overlapping. A chain
    # only ever has one label per gap, in one line, so it needs less room
    # than that did, not more — even though it says more.
    # The panel's own outline carries real dimensioned geometry too -- a
    # stepped or notched corner, not just where holes sit -- so its own
    # vertices join the same chain a hole position would, the way the
    # reference sheet dimensions a step in the edge exactly like it
    # dimensions a hole: a gap on the same line, not a shape left to be
    # read off the drawing by eye.
    outline_x = [x for poly in flat["panels"] for x, _y in poly]
    outline_y = [y for poly in flat["panels"] for _x, y in poly]
    pos_x = _cluster([h["x"] for h in flat["holes"]] + outline_x)
    pos_y = _cluster([h["y"] for h in flat["holes"]] + outline_y)
    top_margin = 34 if pos_x else 0
    left_margin = 46 if pos_y else 0

    box_w, box_h = split - M - left_margin - 10, box_h_avail - top_margin
    scale = min(box_w / max(fw, 1), box_h / max(fh, 1), 6.0)
    scale = max(scale, 0.05)
    x0 = M + left_margin
    y_top = y0 + 34 + top_margin
    left, top, right, bottom = x0, y_top, x0 + fw * scale, y_top + fh * scale

    def X(x):
        return left + x * scale

    def Y(y):
        return bottom - y * scale             # model y up, svg y down

    for poly in flat["panels"]:
        sh.polygon([(X(x), Y(y)) for x, y in poly], sw=1.2)
    # A window cut into a panel, or a slot -- anything that is not a plain
    # round hole -- drawn as its own real outline, filled white so it
    # reads as an opening rather than a second panel sitting on top, with
    # its own width x height labelled the way the reference sheet marks
    # its 46 x 46 square: a size on the drawing, not just a shape.
    #
    # One label per DISTINCT size, the same rule the hole diameters
    # already follow, and for the same reason: a ventilation grille is
    # dozens of identically-sized rectangular cutouts, and a label on
    # every one of them is the same overlapping-digits mess a tick on
    # every hole was, just with cutouts instead of holes making it.
    #
    # Every label placed anywhere on this drawing -- a cutout's size, a
    # hole's diameter -- shares this one obstacle list, so a hole's Ø
    # and a nearby cutout's size (different features, no idea about each
    # other) cannot both land on the same few pixels, and neither can
    # land on top of a THIRD feature's own drawn shape -- both really
    # happened on a real generated job: a hole and a cutout a few mm
    # apart each labelled "beside its own shape" with no view of the
    # other, and separately a cutout's label spilling onto a different
    # nearby hole's circle. _place_label pushes whichever label loses
    # its default spot out along a thin leader line instead, the way a
    # crowded real drawing points a callout at its feature from open
    # space rather than cramming the number into occupied ground.
    #
    # Two passes, not one: every shape is drawn and registered as an
    # obstacle FIRST, so that when cutout and hole labels are placed
    # second, each one already knows about every shape on the page --
    # not just the ones drawn earlier in a single combined pass, which
    # is what let a cutout's label miss a hole that was only drawn
    # (and only became an obstacle) afterwards.
    label_boxes: list = []
    labelled_cutout_sizes: set[tuple] = set()
    cutouts_to_label = []
    for poly in flat.get("cutouts", []):
        pts = [(X(x), Y(y)) for x, y in poly]
        sh.polygon(pts, fill="#fff")
        pxs, pys = [p[0] for p in pts], [p[1] for p in pts]
        label_boxes.append((min(pxs), min(pys), max(pxs), max(pys)))
        cxs = [p[0] for p in poly]
        cys = [p[1] for p in poly]
        cw, ch = max(cxs) - min(cxs), max(cys) - min(cys)
        key = (round(cw, 1), round(ch, 1))
        if key in labelled_cutout_sizes:
            continue
        labelled_cutout_sizes.add(key)
        cutouts_to_label.append((cw, ch, cxs, cys))
    for bl in flat["bend_lines"]:
        sh.line(X(bl["x1"]), Y(bl["y1"]), X(bl["x2"]), Y(bl["y2"]),
                w=0.8, color="#b33", dash="4,3")

    labelled_dia: set[float] = set()
    holes_to_label = []
    for h in flat["holes"]:
        r = max(h["dia_mm"] / 2 * scale, 1.3)
        cx, cy = X(h["x"]), Y(h["y"])
        sh.circle(cx, cy, r)
        label_boxes.append((cx - r, cy - r, cx + r, cy + r))
        if h["dia_mm"] not in labelled_dia:
            labelled_dia.add(h["dia_mm"])
            holes_to_label.append((cx, cy, r, h["dia_mm"]))

    # A window cut into a panel, or a slot -- anything that is not a
    # plain round hole -- gets its own width x height labelled the way
    # the reference sheet marks its 46 x 46 square: a size on the
    # drawing, not just a shape.
    for cw, ch, cxs, cys in cutouts_to_label:
        text = f"{cw:.2f} x {ch:.2f}"
        if cw * scale > 60 and ch * scale > 20:
            # Room for the label inside the opening itself -- never
            # crowded enough in practice to need a leader, but still
            # registered so a later label does not land on top of it.
            x_mid, y_mid = (min(cxs) + max(cxs)) / 2, (min(cys) + max(cys)) / 2
            xm, ym = X(x_mid), Y(y_mid)
            sh.text(xm, ym, text, size=9.5, anchor="middle", fill="#a33")
            label_boxes.append(_label_box(xm, ym, text, 9.5, "middle"))
        else:
            # Too small to hold its own label without the text spilling
            # out past the shape (a corner relief a few mm across) — put
            # it beside the cutout instead, the same convention a small
            # hole's own diameter label already uses.
            anchor_pt = (X(max(cxs)), Y(min(cys)))
            lx, ly, leadered = _place_label(
                anchor_pt, (anchor_pt[0] + 3, anchor_pt[1] - 2), text, 9.5,
                "start", label_boxes)
            if leadered:
                sh.line(anchor_pt[0], anchor_pt[1], lx, ly, w=0.6,
                       color="#a33")
            sh.text(lx, ly, text, size=9.5, fill="#a33")

    # Every hole's own diameter, once per DISTINCT diameter present, so a
    # repeated size is not re-labelled at every instance and crowded off
    # the page.
    for cx, cy, r, dia in holes_to_label:
        text = f"Ø{dia:g}"
        lx, ly, leadered = _place_label(
            (cx, cy), (cx + r + 3, cy - r - 2), text, 9.5, "start",
            label_boxes)
        if leadered:
            sh.line(cx, cy, lx, ly, w=0.6, color="#a33")
        sh.text(lx, ly, text, size=9.5, fill="#a33")

    # Position dimensions as a CHAIN — the gap from the edge to the first
    # feature, then feature to feature, then the last feature to the far
    # edge — each its own little arrowed span, the way the reference
    # sheet's own "26.5 / 90.5 / 50 / 3.5" reads: distances BETWEEN
    # things, not each thing's bare distance from a datum a reader has to
    # subtract by hand to get the distance between two holes.
    #
    # Clustered a second time here, in PIXEL space at this part's actual
    # scale — the first pass (in mm, sizing the margin above) can leave
    # two stops only a few pixels apart on a small part or a tight hole
    # pattern (a ventilation grille's slots, a dozen positions in 60 mm),
    # and the mm tolerance that is fine for one part is too fine for a
    # smaller one at a different scale.
    #
    # The tolerance is derived straight from _Sheet.NARROW, dim_h/dim_v's
    # own threshold for pushing a span's arrows and label outward instead
    # of fitting them inside it — so every surviving segment clears that
    # threshold by construction, rather than being clustered by an
    # unrelated pixel figure and then separately checked (and dropped
    # wholesale) against it. One span pushed outward reads fine; two
    # narrow spans next to each other push their overflow labels into
    # each other, which built-by-construction ones never do.
    min_px = _Sheet.NARROW + 8
    chain_x = _chain_points(pos_x, fw, min_gap=max(1.0, min_px / scale))
    chain_y = _chain_points(pos_y, fh, min_gap=max(1.0, min_px / scale))

    row_x, col_y = y0 + 34, x0 - 30
    skipped_axis = False
    if chain_x:
        for a, b in zip(chain_x, chain_x[1:]):
            sh.dim_h(X(a), X(b), row_x, b - a)
    else:
        skipped_axis = True
    if chain_y:
        for a, b in zip(chain_y, chain_y[1:]):
            sh.dim_v(Y(b), Y(a), col_y, b - a)
    else:
        skipped_axis = True

    sh.dim_h(left, right, bottom + 24, fw)
    sh.dim_v(top, bottom, right + 26, fh)

    # ── isometric, in its own clearly separated column ──────────────────
    bottom_edge = bottom
    if with_isometric:
        iso_x, iso_w = split + 30, W - M - split - 30
        iso = _project(shape, (1, -1.2, 1)) if shape is not None else None
        if iso:
            xmin, xmax, ymin, ymax = iso["bb"]
            s_iso = min(iso_w / max(xmax - xmin, 1e-6),
                       260 / max(ymax - ymin, 1e-6))
            sh.view(iso, iso_x, y_top, s_iso, iso_w, 260)
        else:
            sh.text(iso_x + iso_w / 2, y_top + 120,
                    "(view could not be drawn)", size=12, anchor="middle",
                    fill="#8a9098")
        bottom_edge = max(bottom_edge, y_top + 338)

    # ── hole-count summary, a quick tally under both sections — the
    # detail is already on the drawing itself; this is only "how many of
    # each size" for ordering tooling, not something that must be
    # cross-referenced to read the drawing. Material figures live here
    # too, whether or not there is an isometric column to have carried
    # them instead. ─────────────────────────────────────────────────────
    sy = max(bottom_edge + 60, y_top + 60)
    sh.text(M, sy, f"t≈{part['thickness_mm']:.2f} mm sheet · volume "
            f"{part['volume_cm3']:.2f} cm3 · {_weights(part['volume_cm3'], 'metal')}",
            size=11.5, fill="#444")
    sy += 20
    rows = flat["holes"]
    if rows:
        counts: dict[float, int] = {}
        for h in rows:
            counts[h["dia_mm"]] = counts.get(h["dia_mm"], 0) + 1
        summary = "  ·  ".join(f"Ø{dia:g} x {n}"
                               for dia, n in sorted(counts.items()))
        sh.text(M, sy, f"HOLE COUNT — {summary}", size=11.5, fill="#444")
        sy += 18
    if flat.get("faces_hidden_meaningful", 0) > 0:
        sh.text(M, sy,
                f"{flat['faces_hidden_meaningful']} face(s) off the main "
                "bend chain could not be unfolded and are not shown — "
                "check this part's flat pattern against the formed model "
                "before cutting", size=11, fill="#8a6d1f")
        sy += 20
    if skipped_axis:
        sh.text(M, sy,
                "A row or column of holes here is too closely packed to "
                "dimension individually (a vent, a repeated pattern) — "
                "shown to scale on the drawing, not called out one by "
                "one.", size=11, fill="#8a6d1f")
        sy += 20

    band_bottom = sy + 10
    sh.line(M, band_bottom, W - M, band_bottom, w=1.2, color="#222")
    sh.y = band_bottom + 22


def _title_block(sh: _Sheet, report: dict) -> None:
    M, W = sh.M, sh.W
    y = sh.y
    sh.text(M, y + 16, "NOTES:", size=12.5, weight="bold")
    any_flat = any(p.get("flat") for p in report["parts"])
    flat_note = (
        f"Flat pattern shown where the bends resolve to simple folds "
        f"(K-factor {report.get('k_factor', _DEFAULT_K_FACTOR):.4f}); the "
        "3-view drawing above is still the formed part as modelled."
        if any_flat else
        "Sizes are of the FORMED part as modelled; this part's flat "
        "pattern did not resolve to simple folds and is not shown."
    )
    notes = ["All dimensions are in millimetres (mm), to two decimals.",
             flat_note,
             "Thickness is an estimate (2 x volume / surface area).",
             "Hole counts are distinct hole positions; a slot reads as two."]
    for k, n in enumerate(notes):
        sh.text(M + 14, y + 36 + k * 18, f"{k + 1}. {n}", size=12, fill="#222")
    y += 36 + len(notes) * 18 + 16
    rows = [("JOB NAME:", report["file"], "DRAWN BY:", "Prism"),
            ("MATERIAL:", "CRC SHEET" if report["mode"] == "metal"
             else "MOULDED PLASTIC", "DATE:",
             _dt.date.today().strftime("%d-%m-%Y")),
            ("SCALE:", "NTS", "REMARKS:", "Measured offline by Prism — the "
                                         "STEP file never left this machine")]
    rh = 34
    sh.rect(M, y, W - 2 * M, rh * len(rows), sw=1.4)
    mid = (W + M) / 2 - 60
    for k, (a, b, c, d) in enumerate(rows):
        yy = y + k * rh
        if k:
            sh.line(M, yy, W - M, yy, w=0.8)
        sh.line(mid, yy, mid, yy + rh, w=0.8)
        sh.text(M + 12, yy + 22, a, size=11.5, fill="#444")
        sh.text(M + 140, yy + 22, b, size=15)
        sh.text(mid + 12, yy + 22, c, size=11.5, fill="#444")
        sh.text(mid + 120, yy + 22, d, size=14)
    sh.y = y + rh * len(rows) + sh.M


def _lay_out_sheet(sh, report: dict) -> None:
    """Every part's flat pattern (or formed 3-view), hole table, notes and
    title block, laid out onto `sh` — an _Sheet (SVG) or a _Canvas (PIL
    raster); this only calls the shared drawing methods, so the one
    layout serves both backends. Pure geometry and the measured figures;
    no AI."""
    o = report["overall_mm"]
    sh.text(sh.M, sh.y + 8, f"{report['file']} — measured drawing sheet",
            size=20, weight="bold")
    sh.text(sh.M, sh.y + 30,
            f"{report['mode']} moulding · assembly overall {o[0]:.2f} x "
            f"{o[1]:.2f} x {o[2]:.2f} mm · {len(report['parts'])} part(s) · "
            f"measured offline by Prism, "
            f"{_dt.date.today().strftime('%d-%m-%Y')}",
            size=13, fill="#555")
    sh.y += 56
    sh.line(sh.M, sh.y, sh.W - sh.M, sh.y, w=1.2, color="#222")
    sh.y += 22
    shapes = dict((name, s) for name, s in report.get("_shapes") or [])
    for i, part in enumerate(report["parts"], 1):
        shape = shapes.get(part["name"])
        if shape is None:
            sh.text(sh.M, sh.y + 22, f"{i}) {part['name'].upper()} — "
                    f"{_caption(part, report['mode'])}", size=16, weight="bold")
            sh.text(sh.M + 10, sh.y + 44, "(no geometry to draw)", size=12,
                    fill="#8a9098")
            sh.y += 70
            continue
        # Flat pattern + isometric is the section a shop-floor worker
        # actually reads, and it is what most of these parts get: the
        # formed 3-view (_draw_part) is now only the fallback for a part
        # whose bends did not resolve to simple folds, where it is the
        # only real drawing there is to show.
        if part.get("flat"):
            _draw_flat_pattern(sh, i, part, shape)
        else:
            _draw_part(sh, i, part, shape, report["mode"])
    _title_block(sh, report)


def sheet_svg(report: dict) -> str:
    """The whole dimensioned sheet as one SVG string — every part's three
    views with its overall sizes on dimension lines, hole table, notes and
    title block. Pure geometry and the measured figures; no AI."""
    sh = _Sheet()
    _lay_out_sheet(sh, report)
    return sh.svg(sh.y)


def render_sheet(report: dict, out_dir: str) -> dict:
    """The whole job's deliverable images, drawn straight to PNG — no
    HTML, no SVG file, no browser: Pillow rasterises the same layout
    _Sheet would have written as SVG (_Canvas, _lay_out_sheet), the way
    Reel already draws its own frames. Two kinds of file:

      · '<model> - drawing sheet.png'         — everything: every part's
        flat pattern (or formed 3-view) and isometric, one page;
      · '<model> - <part> - flat pattern.png' — one per part that has a
        flat pattern, that part alone, full page, no isometric column —
        the image a person actually cuts from.

    Returns {"png": <everything path>, "flats": {part_name: path, ...}}.
    A part whose bends never resolved to a flat pattern has no entry in
    "flats" — there is no flat cutting image for a part that has none,
    the same honesty unfold() itself already follows."""
    os.makedirs(out_dir, exist_ok=True)
    stem = _stem(report)
    out = names(stem)
    shapes = dict((name, s) for name, s in report.get("_shapes") or [])

    sh = _Canvas()
    _lay_out_sheet(sh, report)
    png_path = os.path.join(out_dir, out["png"])
    sh.image(sh.y).save(png_path)

    flats: dict[str, str] = {}
    for i, part in enumerate(report["parts"], 1):
        if not part.get("flat"):
            continue
        fsh = _Canvas()
        _draw_flat_pattern(fsh, i, part, shapes.get(part["name"]),
                           with_isometric=False)
        fpath = os.path.join(out_dir,
                             flat_image_name(stem, part["name"], i))
        fsh.image(fsh.y).save(fpath)
        flats[part["name"]] = fpath

    return {"png": png_path, "flats": flats}


def auto_brief(report: dict) -> str:
    """The prompt /step-auto hands the image agent.

    The security model lives IN the prompt: only these measured figures and
    Prism's own plain render travel to the tool — the customer's STEP file
    stays on this machine, and the brief says so out loud, so the agent
    neither asks for the model nor invents a number to fill a gap.
    """
    o = report["overall_mm"]
    lines = [
        "Draw ONE professional sheet-metal fabrication drawing sheet as a "
        "single portrait image, in the plain style of a manufacturer's "
        "hand-made dimension sheet: black line-work on white, one labelled "
        "view per part, dimension lines with arrowheads for length, width "
        "and height, hole callouts written as diameter x count, and a small "
        "title block at the bottom.",
        "",
        "Every figure below was MEASURED OFFLINE by Prism from the "
        "customer's CAD model. The model itself is confidential and is not "
        "shared — the attached image is Prism's own plain render of the "
        "parts, for view reference only.",
        "",
        f"Job: {report['file']} · {report['mode']} moulding · "
        f"assembly overall {o[0]:.2f} x {o[1]:.2f} x {o[2]:.2f} mm · "
        f"{len(report['parts'])} part(s)",
        "",
    ]
    for i, part in enumerate(report["parts"], 1):
        L, W, H = part["size_mm"]
        lines.append(f"{i}) {part['name']} — {L:.2f} x {W:.2f} x {H:.2f} mm "
                     f"· sheet t≈{part['thickness_mm']:.2f} mm · 1 nos")
        if part["holes"]:
            lines.append("   holes: " + " · ".join(
                f"Ø{h['dia_mm']:g} x {h['count']}" for h in part["holes"]))
    lines += [
        "",
        "Title block: job name, material "
        + ("CRC sheet" if report["mode"] == "metal" else "moulded plastic")
        + ", scale NTS, today's date, and the line "
          "'Measured offline by Prism'.",
        "Rules: use EXACTLY the numbers above — do not round, convert or "
        "invent any dimension, and do not add parts or holes that are not "
        "listed. Label every part with its name.",
        "Never label any view 'flat pattern' or 'developed' — no unfolded "
        "flat has been computed, and a fabricator would cut from it. Views "
        "are of the FORMED part only. Do not state any tolerance, grade or "
        "finish that is not written above; general notes may only say the "
        "dimensions are in millimetres and hole positions are indicative.",
    ]
    return "\n".join(lines) + _SK.addendum("step.auto")


# ── /step-ask: a question → Groq's advice → an agent's plan → applied ───────
# The customer's model still never leaves this machine. Groq gets measured
# numbers and the question; the browser agent gets those plus Groq's advice;
# and the geometry edits themselves happen HERE, in cadquery, on a copy.

PLAN_OPS = ("enlarge_hole", "scale")


def _part_lines(report: dict) -> str:
    lines = []
    for i, part in enumerate(report["parts"], 1):
        L, W, H = part["size_mm"]
        lines.append(f"{i}) {part['name']} — {L:.2f} x {W:.2f} x {H:.2f} mm "
                     f"· wall/sheet t≈{part['thickness_mm']:.2f} mm · "
                     f"volume {part['volume_cm3']:.2f} cm3")
        if part["holes"]:
            lines.append("   holes: " + " · ".join(
                f"Ø{h['dia_mm']:g} x {h['count']}" for h in part["holes"]))
    return "\n".join(lines)


def ask_prompt(report: dict, question: str) -> str:
    """What the chosen model is asked — Groq, or ChatGPT/Claude/Gemini via
    the fallback chain. Numbers and the question — never the model. The
    format instructions are deliberately explicit (plain prose, one-line
    lead answer, no tables, no citing reference material by name): left
    looser, the small in-house model fell back to dense markdown tables
    (unreadable in a plain text box) and parroted internal rule numbers at
    the reader, where a stronger model wrote the same advice in plain,
    readable prose without being told to. Same prompt for every model, so
    the difference is the model, not the ask."""
    o = report["overall_mm"]
    return (
        f"You are advising a {report['mode']} moulding shop on a customer's "
        "part. The CAD model is confidential and cannot be shown to you — "
        "everything known about it was measured offline and is below.\n\n"
        f"Job: {report['file']} · assembly overall "
        f"{o[0]:.2f} x {o[1]:.2f} x {o[2]:.2f} mm · "
        f"{len(report['parts'])} part(s)\n"
        f"{_part_lines(report)}\n\n"
        f"The customer asks: {question}\n\n"
        "Start with ONE direct sentence that answers the question itself, "
        "before any supporting detail. Then give short, practical "
        "suggestions grounded ONLY in the figures above — do not invent "
        "features you cannot see. Where a suggestion is a hole size change "
        "or an overall scale change, state it precisely: which part, "
        "current Ø, new Ø (or scale factor), and why. Say plainly which "
        "ONE change matters most if the others are minor by comparison — "
        "do not give every suggestion equal weight. Mark anything that "
        "would need the customer's designer (ribs, draft, wall changes) as "
        "their decision, not ours.\n\n"
        "Write in plain paragraphs under short section headers — no "
        "markdown tables; they do not render where this is read. Do not "
        "cite rule numbers or name any reference material — explain the "
        "reasoning itself, in your own words."
        + _SK.addendum("step.ask"))


def plan_prompt(report: dict, question: str, suggestions: str) -> str:
    """What the reviewing agent is asked: turn the advice into a strict
    machine plan of ONLY the operations Prism can execute locally."""
    return (
        "You are the reviewing engineer. Below are offline measurements of "
        "a confidential CAD model (the model itself is not shared) and a "
        "first advisor's suggestions. Decide which changes are right, then "
        "answer with ONE JSON object and nothing else.\n\n"
        f"MEASURED ({report['mode']} moulding, {report['file']}):\n"
        f"{_part_lines(report)}\n\n"
        f"THE CUSTOMER ASKED: {question}\n\n"
        f"FIRST ADVISOR SAID:\n{suggestions}\n\n"
        "Prism can execute exactly two operations on the model, locally:\n"
        '  {"op": "enlarge_hole", "part": "<part name or all>", '
        '"dia_mm": <current>, "new_dia_mm": <bigger>, "why": "..."}\n'
        '  {"op": "scale", "part": "<part name or all>", '
        '"factor": <0.2..5>, "why": "..."}\n\n'
        "Answer format:\n"
        '{"changes": [ ...only the two ops above, only if truly right... ],\n'
        ' "advice":  [ "every other worthwhile suggestion, as a sentence" ]}\n\n'
        "Rules: use only part names and hole diameters that appear in the "
        "measurements. A hole can only be enlarged, never shrunk. When no "
        "executable change is justified, return an empty changes list — an "
        "honest empty list beats an invented edit."
        + _SK.addendum("step.plan"))


def _valid_change(ch) -> dict | None:
    if not isinstance(ch, dict):
        return None
    part = str(ch.get("part") or "all").strip() or "all"
    why = str(ch.get("why") or "")[:240]
    try:
        if ch.get("op") == "enlarge_hole":
            dia, new = float(ch["dia_mm"]), float(ch["new_dia_mm"])
            if not 0 < dia < new:
                return None
            return {"op": "enlarge_hole", "part": part, "dia_mm": round(dia, 2),
                    "new_dia_mm": round(new, 2), "why": why}
        if ch.get("op") == "scale":
            factor = float(ch["factor"])
            if not 0.2 <= factor <= 5 or factor == 1:
                return None
            return {"op": "scale", "part": part,
                    "factor": round(factor, 4), "why": why}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def parse_plan(texts: list[str]) -> tuple[dict | None, str]:
    """Newest capture that parses — the tab also holds the prompt Prism
    typed, which carries the example schema inside it."""
    import json
    for t in reversed([t for t in texts if t and t.strip()]):
        s, e = t.find("{"), t.rfind("}") + 1
        if s == -1 or e <= s:
            continue
        try:
            raw = json.loads(t[s:e])
        except ValueError:
            continue
        if not isinstance(raw, dict) or not (
                "changes" in raw or "advice" in raw):
            continue
        changes = [c for c in map(_valid_change, raw.get("changes") or [])
                   if c]
        advice = [str(a).strip() for a in (raw.get("advice") or [])
                  if str(a).strip()][:12]
        return {"changes": changes, "advice": advice}, ""
    return None, "The agent returned no JSON plan Prism could read."


def _enlarged(shape, dia: float, new_dia: float):
    """Cut a bigger cylinder along every existing axis of the Ø`dia` holes.
    Enlarge only — shrinking would mean adding material, which a boolean
    cut cannot do and _valid_change refuses upstream."""
    bb = shape.BoundingBox()
    span = (bb.xlen ** 2 + bb.ylen ** 2 + bb.zlen ** 2) ** 0.5 or 1.0
    seen, cutters = set(), []
    for f in shape.Faces():
        if f.geomType() != "CYLINDER":
            continue
        cyl = BRepAdaptor_Surface(TopoDS.Face_s(f.wrapped)).Cylinder()
        if abs(2 * cyl.Radius() - dia) > 0.05:
            continue
        ax = cyl.Axis()
        loc, d = ax.Location(), ax.Direction()
        key = (round(loc.X(), 1), round(loc.Y(), 1), round(loc.Z(), 1))
        if key in seen:
            continue
        seen.add(key)
        start = _cq.Vector(loc.X(), loc.Y(), loc.Z()) - \
            _cq.Vector(d.X(), d.Y(), d.Z()) * span
        cutters.append(_cq.Solid.makeCylinder(
            new_dia / 2, 2 * span, pnt=start,
            dir=_cq.Vector(d.X(), d.Y(), d.Z())))
    for c in cutters:
        shape = shape.cut(c)
    return shape, len(seen)


def _scaled(shape, factor: float):
    """gp_Trsf, not transformGeometry: the general transform rewrites every
    cylinder as a b-spline, and a hole that is no longer a CYLINDER face
    vanishes from _holes_of — the re-measure would deny holes that exist."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf
    t = gp_Trsf()
    t.SetScale(gp_Pnt(0, 0, 0), factor)
    return _cq.Shape.cast(
        BRepBuilderAPI_Transform(shape.wrapped, t, True).Shape())


def _part_name_matches(ch_part: str, real_name: str) -> bool:
    """A reviewing agent's plan names a part by typing it, not by picking
    it off a list — and it reliably adds things the measured name never
    had, e.g. "PET CORE 02 06" came back as "PET CORE 02 06 — 1 NOS" (a
    quantity annotation it invented). An exact match then silently finds
    NO part at all: predicted_parts() shows no change, and apply_plan()
    either skips the edit or — if it was the only change in the plan —
    raises "Nothing in the plan could be applied", with a real, specific,
    reviewed plan never actually reaching the file. The real name as a
    case-insensitive PREFIX of what the plan wrote is accepted instead;
    the model's own real part names are the only things checked against,
    so this cannot match the WRONG part, only a wordier name for the
    right one."""
    ch_part, real_name = ch_part.strip(), real_name.strip()
    if ch_part == real_name:
        return True
    return ch_part.casefold().startswith(real_name.casefold())


def apply_plan(path: str, plan: dict, out_path: str) -> dict:
    """Execute the validated plan on a COPY; the original file is never
    written. Returns {"out": path, "log": [human lines]}."""
    parts = load_parts(path)
    by_name = dict(parts)
    log = []
    for ch in plan.get("changes") or []:
        names = ([n for n, _s in parts] if ch["part"] == "all"
                 else [n for n, _s in parts
                      if _part_name_matches(ch["part"], n)])
        if not names:
            log.append(f"! no part called '{ch['part']}' — skipped")
            continue
        for name in names:
            if ch["op"] == "enlarge_hole":
                shape, n = _enlarged(by_name[name],
                                     ch["dia_mm"], ch["new_dia_mm"])
                if n:
                    by_name[name] = shape
                    log.append(f"Ø{ch['dia_mm']:g} → Ø{ch['new_dia_mm']:g} "
                               f"on {name}: {n} hole(s) enlarged")
                elif ch["part"] != "all":
                    log.append(f"! no Ø{ch['dia_mm']:g} hole on {name} "
                               "— skipped")
            elif ch["op"] == "scale":
                by_name[name] = _scaled(by_name[name], ch["factor"])
                log.append(f"{name} scaled x {ch['factor']:g}")
    if not any(not line.startswith("!") for line in log):
        raise StepError("Nothing in the plan could be applied — "
                        "the model was not written.")
    asm = _cq.Assembly()
    for name, _s in parts:
        asm.add(by_name[name], name=name)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    asm.export(out_path)
    return {"out": out_path, "log": log}


def predicted_parts(report: dict, plan: dict) -> list[dict]:
    """What the parts SHOULD measure after the plan — arithmetic on the
    measured figures, for the review page. The real answer still comes from
    re-measuring the built file; this is the promise the user approves."""
    import copy
    parts = copy.deepcopy(report["parts"])
    for ch in plan.get("changes") or []:
        for p in parts:
            if ch["part"] != "all" and not _part_name_matches(
                    ch["part"], p["name"]):
                continue
            if ch["op"] == "enlarge_hole":
                for h in p["holes"]:
                    if abs(h["dia_mm"] - ch["dia_mm"]) <= 0.05:
                        h["dia_mm"] = ch["new_dia_mm"]
                merged: dict[float, int] = {}
                for h in p["holes"]:
                    merged[h["dia_mm"]] = merged.get(h["dia_mm"], 0) + h["count"]
                p["holes"] = [{"dia_mm": d, "count": c}
                              for d, c in sorted(merged.items())]
            elif ch["op"] == "scale":
                f = ch["factor"]
                p["size_mm"] = tuple(round(v * f, 2) for v in p["size_mm"])
                p["thickness_mm"] = round(p["thickness_mm"] * f, 2)
                p["volume_cm3"] = round(p["volume_cm3"] * f ** 3, 2)
                p["holes"] = [{"dia_mm": round(h["dia_mm"] * f, 2),
                               "count": h["count"]} for h in p["holes"]]
    return parts


def _holes_str(part: dict) -> str:
    return " · ".join(f"Ø{h['dia_mm']:g} x {h['count']}"
                      for h in part["holes"]) or "—"


def review_html(report: dict, plan: dict, out_dir: str,
                question: str = "", after: dict | None = None) -> str:
    """The approval page: every dimension before and after, side by side,
    with the drawing image — written BEFORE anything is built, so the user
    confirms against what they can see, not against terminal text. Called
    again with `after` (the re-measured report) once modified.step exists,
    so the same page becomes the record of what was actually done."""
    import html as _html

    before = report["parts"]
    shown = ([{"name": p["name"], "size_mm": p["size_mm"],
               "thickness_mm": p["thickness_mm"],
               "volume_cm3": p["volume_cm3"], "holes": p["holes"]}
              for p in after["parts"]] if after
             else predicted_parts(report, plan))
    by_after = {p["name"]: p for p in shown}

    if after:
        banner = (f"<div class='banner'>{_html.escape(names(report)['modified'])}"
                  " is BUILT — the After column is re-measured from the new "
                  "file, not predicted.</div>")
    else:
        # No "answer Y/N here" — this page is shown from two different
        # places (the terminal's own Y/N prompt, and the GUI's own
        # Build/Cancel buttons next to it) and used to name only the
        # terminal, which confused the GUI's own confirm into looking like
        # dead instructions nothing on screen matched.
        banner = ("<div class='banner'>Nothing is built yet. This is the "
                  "plan for "
                  f"{_html.escape(names(report)['modified'])} — confirm it "
                  "to build, or stop here.</div>")

    def fmt(size):
        return f"{size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f}"

    change_rows = []
    for ch in plan.get("changes") or []:
        what = (f"Hole Ø{ch['dia_mm']:g} → Ø{ch['new_dia_mm']:g}"
                if ch["op"] == "enlarge_hole"
                else f"Scale x {ch['factor']:g}")
        change_rows.append(
            f"<tr><td>{_html.escape(ch['part'])}</td>"
            f"<td>{_html.escape(what)}</td>"
            f"<td>{_html.escape(ch.get('why') or '')}</td></tr>")

    part_rows = []
    for p in before:
        q = by_after.get(p["name"], p)
        changed_size = p["size_mm"] != q["size_mm"]
        changed_holes = _holes_str(p) != _holes_str(q)
        mark = " class='changed'"
        part_rows.append(
            "<tr>"
            f"<td>{_html.escape(p['name'])}</td>"
            f"<td>{fmt(p['size_mm'])}</td>"
            f"<td{mark if changed_size else ''}>{fmt(q['size_mm'])}</td>"
            f"<td>{_holes_str(p)}</td>"
            f"<td{mark if changed_holes else ''}>{_holes_str(q)}</td>"
            f"<td>{p['thickness_mm']:.2f} → {q['thickness_mm']:.2f}</td>"
            "</tr>")

    advice = "".join(f"<li>{_html.escape(a)}</li>"
                     for a in plan.get("advice") or [])
    out = names(report)
    imgs = []
    if os.path.exists(os.path.join(out_dir, out["png"])):
        imgs.append(("The part as received", out["png"]))
    if after and os.path.exists(os.path.join(out_dir, out["png_after"])):
        imgs.append(("The modified model — drawn from the BUILT file",
                     out["png_after"]))
    img = "".join(f"<h2>{t}</h2><img src='{_quote(f)}'>" for t, f in imgs)

    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{_html.escape(report['file'])} — change review</title>"
        "<style>body{font-family:-apple-system,Segoe UI,sans-serif;margin:32px "
        "auto;max-width:1000px;color:#1c2733;background:#fafbfc;padding:0 16px}"
        "h1{font-size:22px}h2{font-size:15px;margin-top:28px}"
        "table{border-collapse:collapse;width:100%;font-size:13.5px}"
        "th,td{border:1px solid #d7dade;padding:7px 10px;text-align:left}"
        "th{background:#eef1f4}td.changed{background:#e7f6ec;font-weight:600}"
        ".banner{background:#fff4d6;border:1px solid #e3c96e;padding:12px "
        "16px;border-radius:8px;margin:14px 0;font-weight:600}"
        ".banner.built{background:#e7f6ec;border-color:#7cc796}"
        ".q{color:#41586e}img{max-width:100%;border:1px solid #d7dade;"
        "border-radius:8px}.note{color:#8a6d1f;font-size:12.5px}</style>"
        f"<h1>{_html.escape(report['file'])} — change review</h1>"
        f"<p class='q'>Asked: {_html.escape(question)}</p>"
        f"{banner}"
        "<h2>Changes Prism will make"
        + (" (made)" if after else "") + "</h2>"
        "<table><tr><th>Part</th><th>Change</th><th>Why</th></tr>"
        + "".join(change_rows) + "</table>"
        "<h2>Every dimension, before → after</h2>"
        "<table><tr><th>Part</th><th>Size before (mm)</th>"
        f"<th>Size {'after' if after else 'after (predicted)'} (mm)</th>"
        "<th>Holes before</th>"
        f"<th>Holes {'after' if after else 'after (predicted)'}</th>"
        "<th>t (mm)</th></tr>"
        + "".join(part_rows) + "</table>"
        + (f"<h2>For the designer (not applied)</h2><ul>{advice}</ul>"
           if advice else "")
        + img
        + "<p class='note'>Measured offline by Prism — the STEP file never "
          "left this machine; the edits run locally on a copy.</p>")

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, out["review"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path
