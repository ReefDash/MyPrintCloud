"""Compact previews for the phone: toolpaths of a sliced plate, and a light copy of the model.

Toolpath file (gzip), little-endian:
  b"MPTP" u8 version=1, u8 n_types, then n_types x (u8 len, utf-8 name)
  u32 n_layers, then per layer: f32 z, u32 n_polylines,
    per polyline: u8 type, u16 n_points, n_points x (i16 x, i16 y) in 0.01 mm
Only extruding moves are kept (travel is dropped); start-gcode purges (;TYPE:Custom) are skipped.
"""
from __future__ import annotations

import gzip
import re
import struct

TYPES = ["Outer wall", "Inner wall", "Overhang wall", "Sparse infill", "Internal solid infill", "Solid infill",
         "Top surface", "Bottom surface", "Bridge", "Internal Bridge", "Gap infill", "Support", "Support interface",
         "Support transition", "Brim", "Skirt", "Prime tower", "Ironing", "Other"]
_TI = {t.lower(): i for i, t in enumerate(TYPES)}
_NUM = re.compile(r"([XYZEF])(-?\d*\.?\d+)")
MIN_STEP = 0.05  # mm: drop points closer than this to the previous one (keeps files small)


def encode_toolpaths(gcode_path: str, out_path: str) -> dict:
    layers: list[tuple[float, list]] = []
    cur: list | None = None
    z, x, y, ftype = 0.0, 0.0, 0.0, len(TYPES) - 1
    rel_e, last_e = True, 0.0
    line_pts: list | None = None  # polyline being extended
    skip = False
    n_pts = 0

    def close():
        nonlocal line_pts
        if line_pts is not None and len(line_pts) >= 4:
            cur.append((ftype, line_pts))
        line_pts = None

    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            c = ln[0] if ln else ""
            if c == ";":
                if ln.startswith(";LAYER_CHANGE"):
                    close()
                    cur = []
                    layers.append((z, cur))
                elif ln.startswith(";Z:"):
                    try:
                        z = float(ln[3:])
                        if layers:
                            layers[-1] = (z, layers[-1][1])
                    except ValueError:
                        pass
                elif ln.startswith(";TYPE:"):
                    close()
                    name = ln[6:].strip()
                    skip = name.lower() == "custom"
                    ftype = _TI.get(name.lower(), len(TYPES) - 1)
                continue
            if c == "M":
                if ln.startswith("M82"):
                    rel_e = False
                elif ln.startswith("M83"):
                    rel_e = True
                continue
            if c != "G" or not (ln.startswith("G1 ") or ln.startswith("G0 ") or ln.startswith("G92")):
                continue
            code = ln.split(";", 1)[0]
            vals = dict(_NUM.findall(code))
            if ln.startswith("G92"):
                if "E" in vals:
                    last_e = float(vals["E"])
                continue
            nx = float(vals["X"]) if "X" in vals else x
            ny = float(vals["Y"]) if "Y" in vals else y
            e = 0.0
            if "E" in vals:
                ev = float(vals["E"])
                e = ev if rel_e else ev - last_e
                last_e = ev if not rel_e else last_e
            moved = nx != x or ny != y
            if moved and e > 0 and cur is not None and not skip:
                if line_pts is None:
                    line_pts = [x, y]
                if abs(nx - line_pts[-2]) + abs(ny - line_pts[-1]) >= MIN_STEP:
                    line_pts += [nx, ny]
                    n_pts += 1
            elif moved:
                close()
            x, y = nx, ny
    close()

    out = bytearray(b"MPTP") + struct.pack("<BB", 1, len(TYPES))
    for t in TYPES:
        b = t.encode()
        out += struct.pack("<B", len(b)) + b
    layers = [(lz, lines) for lz, lines in layers if lines]
    out += struct.pack("<I", len(layers))
    for lz, lines in layers:
        chunks = []
        for t, pts in lines:
            for s in range(0, len(pts) // 2 - 1, 65000):  # split very long polylines
                chunks.append((t, pts[2 * s: 2 * (s + 65001)]))
        out += struct.pack("<fI", lz, len(chunks))
        for t, pts in chunks:
            q = [max(-32767, min(32767, round(v * 100))) for v in pts]
            out += struct.pack("<BH", t, len(q) // 2) + struct.pack(f"<{len(q)}h", *q)
    data = gzip.compress(bytes(out), 6)
    with open(out_path, "wb") as f:
        f.write(data)
    return {"layers": len(layers), "points": n_pts, "bytes": len(data)}


def _decimate_by_body(m, max_faces: int):
    """Preview-only mesh reduction by vertex clustering: snap corners to a grid, drop collapsed triangles. Unlike
    quadric simplification it can't drag a surface more than half a cell, so thin discs and smooth domes keep
    their shape; the cell grows until the face budget is met (the slicer always gets the original file)."""
    import numpy as np
    import trimesh
    v, f = np.asarray(m.vertices, float), np.asarray(m.faces)
    cell = float(np.max(m.extents)) / 1500.0
    best = m
    for _ in range(8):
        key = np.floor((v - v.min(0)) / cell).astype(np.int64)
        uniq, inv = np.unique(key, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        nv = np.zeros((len(uniq), 3))
        np.add.at(nv, inv, v)
        nv /= np.bincount(inv, minlength=len(uniq))[:, None]
        nf = inv[f]
        nf = nf[(nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) & (nf[:, 0] != nf[:, 2])]
        best = trimesh.Trimesh(nv, nf, process=False)
        if len(nf) <= max_faces:
            break
        cell *= 1.6
    return best


def light_mesh(model_path: str, out_path: str, max_faces: int = 120_000) -> dict:
    """Binary STL of the model (decimated if heavy), min corner at the origin, for the 3D prepare view."""
    import splitter
    m = splitter.load(model_path)
    faces = len(m.faces)
    size = [round(float(s), 2) for s in m.extents]
    res = {"size": size, "faces": faces, "fits": splitter._fits(size), "auto_cuts": splitter.plan_cuts(m),
           "volume_cm3": round(abs(float(m.volume)) / 1000, 2) if m.is_watertight else None,
           "area_cm2": round(float(m.area) / 100, 1)}
    if faces <= 600_000:  # hidden pockets for magnets / nuts (pause-for-insert); cheap connectivity split
        try:
            import inserts
            res["pockets"] = inserts.find_pockets(m)[:12]
        except Exception:
            res["pockets"] = []
    try:   # supports trapped in narrow channels (filament paths, tubes) -> suggest "from the plate only"
        import supportcheck
        res["supports"] = supportcheck.trapped(m)
    except Exception:
        res["supports"] = None
    if faces > max_faces:
        try:
            m = _decimate_by_body(m, max_faces)
        except Exception:
            pass
    m.export(out_path, file_type="stl")
    return res
