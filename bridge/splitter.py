"""Split models that are too big for the bed into glue-together parts with filament pins.

Cuts are axis-aligned planes. Each cut interface gets blind holes (default 2.0 mm wide,
6 mm deep into each side) at identical positions on both faces, so short pieces of
1.75 mm filament line the parts up while gluing. Booleans run on manifold3d: the holes
are subtracted from the whole model first (one cylinder straddling the plane per pin),
then the model is split, so both halves of a hole always match.

Coordinates: load() moves the model's min corner to the origin (x, y, z >= 0), so cut
positions are "mm from the model's min corner" in the (scaled) model frame.
"""
from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np
import trimesh
try:
    import manifold3d as mf
    from shapely.geometry import MultiPoint, Polygon, box
    from shapely.ops import unary_union
except ImportError:  # printer-link-only installs (the Fire tablet) never cut models
    mf = None

BED = (240.0, 240.0, 245.0)
AX = {"x": 0, "y": 1, "z": 2}
WALL = 2.0          # material kept around a pin hole in the section
FLOOR = 1.5         # material kept under the bottom of a hole
MIN_HOLE = 2.0      # shorter holes are useless; skip the pin
SEGS = 24
SLIVER_T = 0.5      # mm
# cyclic permutations taking the cut axis to Z (det +1); the section (u, v) = first two coords
_PERM = {0: [1, 2, 0], 1: [2, 0, 1], 2: [0, 1, 2]}


# ---------------------------------------------------------------- mesh helpers

def load(path: str) -> trimesh.Trimesh:
    """One merged mesh, Z-up as given, min corner moved to the origin (on the bed)."""
    m = _load_production_3mf(path) if path.lower().endswith(".3mf") else None
    if m is None:
        m = trimesh.load(path, force="mesh")
    if isinstance(m, trimesh.Scene):  # force="mesh" normally handles this
        m = m.to_geometry() if hasattr(m, "to_geometry") else m.dump(concatenate=True)
    m.merge_vertices()
    m.remove_unreferenced_vertices()
    m.apply_translation(-m.bounds[0])
    return m


def _load_production_3mf(path: str) -> trimesh.Trimesh | None:
    """Orca/Bambu/Elegoo project 3MFs keep meshes in 3D/Objects/*.model with object ids that repeat
    across files; trimesh's loader mixes those up (a 2-part sign came back as the base twice, with a
    copy of its magnet pockets floating 3 mm up). Read the build items ourselves; None = not that kind."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            if not any(n.startswith("3D/Objects/") for n in z.namelist()):
                return None
            import xml.etree.ElementTree as ET
            ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
            root = ET.fromstring(z.read("3D/3dmodel.model"))
            ids = [i.get("objectid") for i in root.find(ns + "build").iter(ns + "item")]
        parts = [m for _, m in plate_objects_by_id(path, ids)]
        return trimesh.util.concatenate(parts) if parts else None
    except Exception:
        return None


def _3mf_matrix(t: str | None) -> np.ndarray:
    """3MF "m00 m01 m02 m10 ... m32" (row-vector convention) -> 4x4 for column vectors."""
    if not t:
        return np.eye(4)
    v = [float(x) for x in t.split()]
    m = np.eye(4)
    m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
    m[:3, 3] = v[9:12]
    return m


def plate_objects(path: str, object_ids: list[str]) -> list[trimesh.Trimesh]:
    return [m for _, m in plate_objects_by_id(path, object_ids)]


def plate_objects_by_id(path: str, object_ids: list[str]) -> list[tuple[str, trimesh.Trimesh]]:
    """The built objects with these ids from a (Bambu/Orca) 3MF, each as one mesh in build coordinates.

    Read straight from the package (objects -> components in 3D/Objects/*.model), so it works for
    multi-plate files whose other plates sit far off the bed."""
    import xml.etree.ElementTree as ET
    import zipfile
    ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
    pns = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
    with zipfile.ZipFile(path) as z:
        docs: dict[str, ET.Element] = {}

        def doc(name: str) -> ET.Element:
            name = name.lstrip("/")
            if name not in docs:
                docs[name] = ET.fromstring(z.read(name))
            return docs[name]

        def mesh_of(name: str, oid: str, tf: np.ndarray) -> list[trimesh.Trimesh]:
            obj = next((o for o in doc(name).iter(ns + "object") if o.get("id") == oid), None)
            if obj is None:
                return []
            out = []
            me = obj.find(ns + "mesh")
            if me is not None:
                v = np.array([[float(x.get("x")), float(x.get("y")), float(x.get("z"))]
                              for x in me.find(ns + "vertices").iter(ns + "vertex")])
                f = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                              for t in me.find(ns + "triangles").iter(ns + "triangle")])
                m = trimesh.Trimesh(v, f, process=False)
                m.apply_transform(tf)
                out.append(m)
            comps = obj.find(ns + "components")
            for c in comps if comps is not None else []:
                sub = c.get(pns + "path") or ("/" + name)
                out += mesh_of(sub, c.get("objectid"), tf @ _3mf_matrix(c.get("transform")))
            return out

        root = doc("3D/3dmodel.model")
        items = {i.get("objectid"): i for i in root.find(ns + "build").iter(ns + "item")}
        res = []
        for oid in object_ids:
            if oid in items:
                parts = mesh_of("3D/3dmodel.model", oid, _3mf_matrix(items[oid].get("transform")))
                if parts:
                    res.append((oid, trimesh.util.concatenate(parts)))
        return res


def _fits(size, bed=BED) -> bool:
    return all(s <= b + 1e-6 for s, b in zip(size, bed))


def info(path: str) -> dict:
    m = load(path)
    size = [round(float(s), 2) for s in m.extents]
    return {"size": size, "faces": int(len(m.faces)), "watertight": bool(m.is_watertight), "fits": _fits(size)}


def _to_manifold(m: trimesh.Trimesh) -> mf.Manifold | None:
    """Manifold from a trimesh, trying light repairs; None if it is not a closed solid."""
    def conv(t):
        mm = mf.Manifold(mf.Mesh64(vert_properties=np.ascontiguousarray(t.vertices, np.float64),
                                   tri_verts=np.ascontiguousarray(t.faces, np.uint64)))
        if mm.is_empty():
            return None
        comps = mm.decompose()
        # separate overlapping shells (common in game/art exports): union them so the cut
        # faces and pins see one solid; skip when there are inside-out shells (hollow models)
        if 1 < len(comps) <= 500 and all(c.volume() > 0 for c in comps):
            mm = mf.Manifold.batch_boolean(comps, mf.OpType.Add)
        return mm
    out = conv(m)
    if out is not None:
        return out
    t = m.copy()
    t.update_faces(t.nondegenerate_faces())
    t.update_faces(t.unique_faces())
    t.merge_vertices(digits_vertex=4)
    t.remove_unreferenced_vertices()
    trimesh.repair.fill_holes(t)
    trimesh.repair.fix_normals(t)
    return conv(t)


def _to_trimesh(m: mf.Manifold) -> trimesh.Trimesh:
    g = m.to_mesh64()
    return trimesh.Trimesh(np.asarray(g.vert_properties)[:, :3], np.asarray(g.tri_verts, np.int64), process=False)


def _perm_matrix(axis: int) -> np.ndarray:
    p = np.zeros((3, 4))
    for row, src in enumerate(_PERM[axis]):
        p[row, src] = 1.0
    return p


def _to3d(axis: int, u: float, v: float, at: float) -> np.ndarray:
    pt = np.zeros(3)
    a, b, c = _PERM[axis]
    pt[a], pt[b], pt[c] = u, v, at
    return pt


# ---------------------------------------------------------------- planning

def plan_cuts(mesh, bed=BED) -> list[dict]:
    """Fewest evenly spaced cuts per axis so every part fits; Z first, X/Y only if the footprint is too big."""
    size = mesh.extents
    cuts = []
    for name in ("z", "x", "y"):
        i = AX[name]
        n = max(0, math.ceil(size[i] / bed[i] - 1e-9) - 1)
        cuts += [{"axis": name, "at": round(float(size[i] * k / (n + 1)), 2)} for k in range(1, n + 1)]
    return cuts


def _section(man: mf.Manifold, axis: int, at: float) -> list[Polygon]:
    """Islands of the cross-section at the plane, as shapely polygons in (u, v)."""
    cs = man.transform(_perm_matrix(axis)).slice(at)
    out = []
    for isl in cs.decompose():
        rings = [np.asarray(r) for r in isl.to_polygons() if len(r) >= 3]
        if not rings:
            continue
        rings.sort(key=lambda r: -abs(Polygon(r).area))
        p = Polygon(rings[0], [r for r in rings[1:]]).buffer(0)
        if not p.is_empty and p.area > 0.01:
            out.append(p)
    return out


def _thickness(man: mf.Manifold, p: np.ndarray, d: np.ndarray, reach: float) -> float:
    """Distance from interior point p to the surface along d (capped at reach)."""
    hits = man.ray_cast(tuple(p), tuple(p + d * reach))
    ts = [h.distance * reach for h in hits if h.distance * reach > 1e-4]
    return min(ts) if ts else reach


def _pick_points(region, n_max: int, spacing: float) -> list[tuple[float, float]]:
    """Up to n_max points in region, spread far apart (farthest pair, then max-min third)."""
    geoms = list(getattr(region, "geoms", [region]))
    cand = []
    for g in geoms:
        if g.is_empty:
            continue
        ring = g.exterior.segmentize(1.0)
        cand += list(ring.coords)[:-1]
        rp = g.representative_point()
        cand.append((rp.x, rp.y))
    c = np.asarray(cand, float)
    if len(c) > 400:
        c = c[np.linspace(0, len(c) - 1, 400).astype(int)]
    if len(c) == 0:
        return []
    if n_max == 1 or len(c) == 1:
        rp = region.representative_point()
        return [(rp.x, rp.y)]
    dist = np.linalg.norm(c[:, None] - c[None], axis=2)
    i, j = np.unravel_index(dist.argmax(), dist.shape)
    if dist[i, j] < spacing:
        rp = region.representative_point()
        return [(rp.x, rp.y)]
    pts = [i, j]
    if n_max >= 3:
        md = np.minimum(dist[i], dist[j])
        k = int(md.argmax())
        if md[k] >= max(spacing, 10.0):
            pts.append(k)
    return [tuple(c[k]) for k in pts]


def _place_pins(man, cuts, ci, pin_d, pin_depth, notes) -> list[dict]:
    """Pins for cut ci: [{"c": xyz on plane, "axis": i, "depth": mm}]."""
    axis, at = AX[cuts[ci]["axis"]], float(cuts[ci]["at"])
    r = pin_d / 2
    label = f"Cut {ci + 1} at {cuts[ci]['axis'].upper()} {at:g} mm"
    islands = _section(man, axis, at)
    if not islands:
        notes.append(f"{label}: no material at this plane")
        return []
    lo, hi = man.bounding_box()[:3], man.bounding_box()[3:]
    # keep pins away from the other cuts: perpendicular ones (holes would collide or straddle)
    a_u, a_v = _PERM[axis][0], _PERM[axis][1]
    wide, narrow = pin_depth + r + 1.0, r + WALL
    bands_w, bands_n = [], []
    for k, c in enumerate(cuts):
        ax2 = AX[c["axis"]]
        if k == ci or ax2 == axis:
            continue
        for bands, w in ((bands_w, wide), (bands_n, narrow)):
            if ax2 == a_u:
                bands.append(box(c["at"] - w, -1e5, c["at"] + w, 1e5))
            else:
                bands.append(box(-1e5, c["at"] - w, 1e5, c["at"] + w))
    par = [float(c["at"]) for k, c in enumerate(cuts) if k != ci and AX[c["axis"]] == axis]
    n = np.zeros(3)
    n[axis] = 1.0
    perp = [np.eye(3)[a_u], np.eye(3)[a_v]]
    pins, counts, skipped = [], [], 0
    for isl in islands:
        reg = isl.buffer(-(r + WALL))
        cut_reg = reg.difference(unary_union(bands_w)) if bands_w and not reg.is_empty else reg
        if cut_reg.is_empty and bands_n and not reg.is_empty:
            cut_reg = reg.difference(unary_union(bands_n))
        if cut_reg.is_empty:
            skipped += 1
            counts.append(0)
            continue
        mnx, mny, mxx, mxy = isl.bounds
        n_max = 3 if max(mxx - mnx, mxy - mny) > 80 else 2
        got = 0
        for u, v in _pick_points(cut_reg, n_max, pin_d + 3.0):
            c = _to3d(axis, u, v, at)
            avail = []
            for s in (1.0, -1.0):
                reach = pin_depth + FLOOR + 1.0
                t = min(_thickness(man, c + perp_off, s * n, reach)
                        for perp_off in [np.zeros(3)] + [q * (r + 0.5) for p_ in perp for q in (p_, -p_)])
                for pa in par:
                    if (pa - at) * s > 0:
                        t = min(t, abs(pa - at))
                avail.append(t)
            depth = min(pin_depth, min(avail) - FLOOR)
            if depth < MIN_HOLE:
                continue
            pins.append({"c": c, "axis": axis, "depth": round(float(depth), 2)})
            got += 1
        if got == 0:
            skipped += 1
        counts.append(got)
    msg = f"{label}: {len(pins)} pin{'s' if len(pins) != 1 else ''}"
    if len(islands) > 1:
        msg += f" over {len(islands)} islands ({' + '.join(map(str, counts))}" + \
               (f"; {skipped} without room for a pin" if skipped else "") + ")"
    elif not pins:
        msg += " (section too small/thin for a pin hole)"
    shorter = [p["depth"] for p in pins if p["depth"] < pin_depth]
    if shorter:
        msg += f"; {len(shorter)} hole(s) shortened to fit"
    notes.append(msg)
    return pins


def _pin_cylinder(p: dict, pin_d: float) -> mf.Manifold:
    # vertices on the circumscribed circle so the flats still clear pin_d
    r = pin_d / 2 / math.cos(math.pi / SEGS)
    cyl = mf.Manifold.cylinder(2 * p["depth"], r, r, SEGS, True)
    cyl = cyl.transform(np.linalg.inv(np.vstack([_perm_matrix(p["axis"]), [0, 0, 0, 1]]))[:3])
    return cyl.translate(tuple(p["c"]))


# ---------------------------------------------------------------- split

def _inside(man: mf.Manifold, q: np.ndarray) -> bool:
    lo, hi = np.asarray(man.bounding_box()[:3]), np.asarray(man.bounding_box()[3:])
    if np.any(q < lo) or np.any(q > hi):
        return False
    d = np.array([0.1234, 0.2345, 0.9641])
    hits = man.ray_cast(tuple(q), tuple(q + d * 1e4))
    return len(hits) % 2 == 1


def split(mesh, cuts: list[dict], pins: bool = True, pin_d: float = 2.0, pin_depth: float = 6.0,
          _meta: dict | None = None) -> list[trimesh.Trimesh]:
    """Cut at the planes (capped, watertight); disconnected bodies become separate parts.

    _meta (optional dict) receives "notes", "pins" (per-part hole counts) and "normals"
    (per-part outward cut-face normals) for prepare().
    """
    meta = _meta if _meta is not None else {}
    notes = meta.setdefault("notes", [])
    man = _to_manifold(mesh)
    if man is None:
        return _split_fallback(mesh, cuts, meta)
    holes = []
    if pins:
        for ci in range(len(cuts)):
            holes += _place_pins(man, cuts, ci, pin_d, pin_depth, notes)
    else:
        notes += [f"Cut {ci + 1} at {c['axis'].upper()} {float(c['at']):g} mm: no pins" for ci, c in enumerate(cuts)]
    total = man.volume()
    work = mf.Manifold.batch_boolean([man] + [_pin_cylinder(p, pin_d) for p in holes], mf.OpType.Subtract) \
        if holes else man
    pieces = [work]
    for c in cuts:
        i = AX[c["axis"]]
        nrm = [0.0, 0.0, 0.0]
        nrm[i] = 1.0
        nxt = []
        for p in pieces:
            for h in p.split_by_plane(nrm, float(c["at"])):
                if not h.is_empty():
                    nxt.append(h)
        pieces = nxt
    bodies, dropped = [], 0
    for p in pieces:
        for b in p.decompose():
            lo, hi = np.asarray(b.bounding_box()[:3]), np.asarray(b.bounding_box()[3:])
            if (hi - lo).min() < SLIVER_T or b.volume() < max(5.0, 1e-5 * total):
                dropped += 1
                continue
            # drop micron-scale edges from the cuts; they collapse in float32 STL and open the mesh
            bodies.append(b.simplify(1e-3))
    if dropped:
        notes.append(f"Dropped {dropped} sliver{'s' if dropped > 1 else ''} (thinner than {SLIVER_T} mm or tiny)")
    bodies.sort(key=lambda b: tuple(np.round(np.asarray(b.bounding_box()[:3])[[2, 1, 0]], 3)))
    counts, normals = [0] * len(bodies), [[] for _ in bodies]
    for p in holes:
        n = np.eye(3)[p["axis"]]
        side = np.eye(3)[_PERM[p["axis"]][0]] * (pin_d / 2 + 0.6)
        for s in (1.0, -1.0):
            q = p["c"] + n * s * p["depth"] / 2 + side
            for k, b in enumerate(bodies):
                if _inside(b, q):
                    counts[k] += 1
                    break
    for k, b in enumerate(bodies):
        lo, hi = b.bounding_box()[:3], b.bounding_box()[3:]
        for c in cuts:
            i, at = AX[c["axis"]], float(c["at"])
            if abs(hi[i] - at) < 1e-3:
                normals[k].append(np.eye(3)[i])
            elif abs(lo[i] - at) < 1e-3:
                normals[k].append(-np.eye(3)[i])
    meta["pins"], meta["normals"] = counts, normals
    return [_to_trimesh(b) for b in bodies]


def _split_fallback(mesh, cuts, meta) -> list[trimesh.Trimesh]:
    """Broken (non-closed) input: plain trimesh plane slicing, no pins, caps best-effort."""
    meta["notes"].append("Model is not a closed solid even after repair: cut without pins; "
                         "parts may have open cut faces")
    parts = [mesh]
    for c in cuts:
        i, at = AX[c["axis"]], float(c["at"])
        n = np.eye(3)[i]
        o = n * at
        nxt = []
        for p in parts:
            for s in (1, -1):
                try:
                    h = trimesh.intersections.slice_mesh_plane(p, n * s, o, cap=True)
                except Exception:  # capping needs closed sections
                    h = trimesh.intersections.slice_mesh_plane(p, n * s, o, cap=False)
                if h is not None and len(h.faces):
                    nxt.append(h)
        parts = nxt
    out = [p for p in parts if p.extents.min() >= SLIVER_T]
    meta["pins"] = [0] * len(out)
    meta["normals"] = []
    for p in out:
        lo, hi = p.bounds
        nl = []
        for c in cuts:
            i, at = AX[c["axis"]], float(c["at"])
            if abs(hi[i] - at) < 1e-3:
                nl.append(np.eye(3)[i])
            elif abs(lo[i] - at) < 1e-3:
                nl.append(-np.eye(3)[i])
        meta["normals"].append(nl)
    return out


# ---------------------------------------------------------------- layout

def _face_area(part: trimesh.Trimesh, n: np.ndarray) -> float:
    """Area of the flat face(s) at the part's extreme along outward normal n."""
    fn = part.face_normals
    proj = part.vertices @ n
    top = proj.max()
    sel = (fn @ n > 0.999) & (np.abs(proj[part.faces].mean(1) - top) < 1e-3)
    return float(part.area_faces[sel].sum())


def orient_for_print(part, cut_normals, bed=BED) -> trimesh.Trimesh:
    """Lay the largest X/Y cut face on the bed if that doesn't make the part taller
    (or the part only fits that way); Z-cut parts keep their orientation. Then drop to
    z=0 and centre on the bed."""
    p = part.copy()
    side = [np.asarray(n, float) for n in cut_normals if abs(np.asarray(n)[2]) < 0.5]
    if side:
        n = max(side, key=lambda n: _face_area(p, n))
        rot = trimesh.geometry.align_vectors(n, [0, 0, -1])
        q = p.copy().apply_transform(rot)
        if _fits(q.extents, bed) and (q.extents[2] <= p.extents[2] + 1e-6 or not _fits(p.extents, bed)):
            p = q
    lo, hi = p.bounds
    p.apply_translation([bed[0] / 2 - (lo[0] + hi[0]) / 2, bed[1] / 2 - (lo[1] + hi[1]) / 2, -lo[2]])
    return p


def pack(parts: list[trimesh.Trimesh], bed=BED, gap: float = 6.0) -> list[list[int]]:
    """Shelf-pack XY footprints (no rotation) into as few plates as possible, biggest first."""
    return pack_sizes([p.extents[:2] for p in parts], bed, gap)


def pack_positions(sizes, bed=BED, gap: float = 6.0) -> list[list[tuple[int, float, float]]]:
    """Max-rects packing (no rotation), biggest first: [[(index, x, y), ...] per plate].
    Unlike shelves it uses the space left beside / above shorter parts."""
    sizes = [(float(s[0]), float(s[1])) for s in sizes]
    W, H = bed[0] + gap, bed[1] + gap  # every part carries its gap on the far sides
    order = sorted(range(len(sizes)), key=lambda i: (-(sizes[i][0] * sizes[i][1]), -sizes[i][1]))
    plates: list[dict] = []  # {"items": [...], "free": [[x, y, w, h]]}

    def place(pl, w, h):
        best = None
        for fx, fy, fw, fh in pl["free"]:
            if w <= fw + 1e-6 and h <= fh + 1e-6 and (best is None or (fy, fx) < (best[1], best[0])):
                best = (fx, fy)
        if best is None:
            return None
        x, y = best
        new = []
        for f in pl["free"]:
            fx, fy, fw, fh = f
            if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
                new.append(f)
                continue
            if x > fx: new.append([fx, fy, x - fx, fh])
            if x + w < fx + fw: new.append([x + w, fy, fx + fw - x - w, fh])
            if y > fy: new.append([fx, fy, fw, y - fy])
            if y + h < fy + fh: new.append([fx, y + h, fw, fy + fh - y - h])
        pl["free"] = [a for k, a in enumerate(new) if not any(
            k != m and b[0] <= a[0] and b[1] <= a[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]
            and (a != b or m < k) for m, b in enumerate(new))]
        return x, y

    for i in order:
        w, h = sizes[i][0] + gap, sizes[i][1] + gap
        for pl in plates:
            xy = place(pl, w, h)
            if xy:
                pl["items"].append((i, *xy))
                break
        else:
            pl = {"items": [], "free": [[0.0, 0.0, W, H]]}
            xy = place(pl, w, h) or (0.0, 0.0)  # bigger than the bed: alone on its plate (cut first)
            pl["items"].append((i, *xy))
            plates.append(pl)
    return [p["items"] for p in plates]


def pack_sizes(sizes, bed=BED, gap: float = 6.0) -> list[list[int]]:
    """Which footprints go on which plate (see pack_positions)."""
    return [sorted(i for i, _, _ in pl) for pl in pack_positions(sizes, bed, gap)]


# ---------------------------------------------------------------- pipeline

def prepare(model_path: str, outdir: str, scale: float = 1.0, cuts: list[dict] | None = None,
            auto_fit: bool = True, pins: bool = True, pin_d: float = 2.0, pin_depth: float = 6.0) -> dict:
    """Load, scale, cut (given cuts, or auto when it doesn't fit), pin, orient, pack, write STLs.

    `cuts` positions are mm in the scaled model frame measured from the model's min corner.
    Always writes at least one part file (uncut models too), so the caller can treat every
    job the same way.
    """
    os.makedirs(outdir, exist_ok=True)
    mesh = load(model_path)
    if scale != 1.0:
        mesh.apply_scale(scale)
    notes: list[str] = []
    if cuts is None:
        cuts = plan_cuts(mesh) if (auto_fit and not _fits(mesh.extents)) else []
    cuts = [{"axis": str(c["axis"]).lower(), "at": float(c["at"])} for c in cuts]
    meta: dict = {"notes": notes}
    if cuts:
        parts = split(mesh, cuts, pins, pin_d, pin_depth, _meta=meta)
    else:
        parts, meta["pins"], meta["normals"] = [mesh], [0], [[]]
    parts = [orient_for_print(p, n) for p, n in zip(parts, meta["normals"])]
    stem = os.path.splitext(os.path.basename(model_path))[0]
    out = []
    for k, p in enumerate(parts):
        path = os.path.join(outdir, f"{stem}_part{k + 1:02d}.stl")
        p.export(path, file_type="stl")
        size = [round(float(s), 2) for s in p.extents]
        out.append({"path": path, "name": f"Part {k + 1}", "size": size, "pins": int(meta["pins"][k])})
        if not _fits(size):
            notes.append(f"Part {k + 1} ({size[0]:g} x {size[1]:g} x {size[2]:g} mm) does not fit the bed")
    return {"parts": out, "plates": pack(parts), "cuts": cuts, "notes": notes,
            "scaled_size": [round(float(s), 2) for s in mesh.extents]}


def _cut_arg(s: str) -> dict:
    a, v = s.split(":")
    if a.lower() not in AX:
        raise argparse.ArgumentTypeError(f"bad axis in {s!r}")
    return {"axis": a.lower(), "at": float(v)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Split a model into bed-sized, pinned parts.")
    ap.add_argument("model")
    ap.add_argument("outdir")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--cut", type=_cut_arg, action="append", help="axis:mm, e.g. z:100 (repeatable)")
    ap.add_argument("--no-pins", action="store_true")
    ap.add_argument("--no-auto", action="store_true", help="don't auto-cut oversize models")
    ap.add_argument("--pin-d", type=float, default=2.0)
    ap.add_argument("--pin-depth", type=float, default=6.0)
    a = ap.parse_args()
    res = prepare(a.model, a.outdir, a.scale, a.cut, not a.no_auto, not a.no_pins, a.pin_d, a.pin_depth)
    print(json.dumps(res, indent=1, ensure_ascii=False))
