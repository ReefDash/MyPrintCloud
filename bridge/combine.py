"""Several (multi-colour) models on one plate, each keeping its own colours.

read(path)      -> the model's objects, each a list of parts {mesh, extruder (1-based), painted}
                   in build coordinates (Bambu/Orca/Elegoo project 3MFs, PrusaSlicer 3MFs, STL/OBJ).
arrange(...)    -> bed positions for many footprints, with the gaps the slicer CLI needs
build(...)      -> one project 3MF: every item's parts on their plate filament (its Canvas slots mapped
                   into the plate's list of slots), plus a prime puck when colours change

Painted models (colour painted onto triangles rather than separate parts) keep their paint: the
per-triangle codes are re-numbered to the plate's filament list (see paint.remap).
"""
from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import trimesh

NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PNS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
PAINT_KEYS = ("paint_color", "{http://schemas.slic3r.org/3mf/2017/06}mmu_segmentation", "slic3rpe:mmu_segmentation")
GAP = 6.0          # mm between items: the CLI refuses closer parts ("G-code conflicts"), ~4 mm is its limit
BED = (256.0, 256.0)
MARGIN = 6.0


def _matrix(t: str | None) -> np.ndarray:
    m = np.eye(4)
    if t:
        v = [float(x) for x in t.split()]
        m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
        m[:3, 3] = v[9:12]
    return m


def _cfg_extruders(z: zipfile.ZipFile) -> tuple[dict, dict]:
    """Bambu/Orca model_settings.config: ({object id: extruder}, {(object id, part id): extruder})."""
    obj, part = {}, {}
    if "Metadata/model_settings.config" not in z.namelist():
        return obj, part
    cfg = z.read("Metadata/model_settings.config").decode("utf-8", "replace")
    for m in re.finditer(r'<object id="(\d+)">(.*?)</object>', cfg, re.S):
        oid, body = m.group(1), m.group(2)
        head = body.split("<part", 1)[0]
        e = re.search(r'key="extruder" value="(\d+)"', head)
        obj[oid] = int(e.group(1)) if e else 1
        for p in re.finditer(r'<part id="(\d+)"[^>]*>(.*?)</part>', body, re.S):
            pe = re.search(r'key="extruder" value="(\d+)"', p.group(2))
            if pe:
                part[(oid, p.group(1))] = int(pe.group(1))
    return obj, part


def _prusa_volumes(z: zipfile.ZipFile) -> dict:
    """PrusaSlicer Slic3r_PE_model.config: {object id: [(first tri, last tri, extruder)]}."""
    out = {}
    if "Metadata/Slic3r_PE_model.config" not in z.namelist():
        return out
    root = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
    for o in root.iter("object"):
        oe = next((int(md.get("value")) for md in o.findall("metadata") if md.get("key") == "extruder"), 1) or 1
        vols = []
        for v in o.iter("volume"):
            ve = next((int(md.get("value")) for md in v.findall("metadata") if md.get("key") == "extruder"), 0)
            vols.append((int(v.get("firstid")), int(v.get("lastid")), ve or oe))
        out[o.get("id")] = vols
    return out


def read(path: str) -> list[dict]:
    """[{"name", "parts": [{"mesh", "extruder", "paint": list|None}]}] in build coordinates (mm)."""
    low = path.lower()
    if not low.endswith(".3mf"):
        m = trimesh.load(path, force="mesh")
        return [{"name": os.path.splitext(os.path.basename(path))[0], "parts": [{"mesh": m, "extruder": 1, "paint": None}]}]
    with zipfile.ZipFile(path) as z:
        docs: dict[str, ET.Element] = {}

        def doc(name: str) -> ET.Element:
            name = name.lstrip("/")
            if name not in docs:
                docs[name] = ET.fromstring(z.read(name))
            return docs[name]

        obj_ext, part_ext = _cfg_extruders(z)
        prusa = _prusa_volumes(z)
        names = {}
        if "Metadata/model_settings.config" in z.namelist():
            cfg = z.read("Metadata/model_settings.config").decode("utf-8", "replace")
            names = dict(re.findall(r'<object id="(\d+)">\s*<metadata key="name" value="([^"]*)"', cfg))

        def mesh_parts(name, oid, tf, top, depth=0):
            obj = next((o for o in doc(name).iter(NS + "object") if o.get("id") == oid), None)
            if obj is None or depth > 6:
                return []
            out = []
            me = obj.find(NS + "mesh")
            if me is not None:
                v = np.array([[float(x.get("x")), float(x.get("y")), float(x.get("z"))]
                              for x in me.find(NS + "vertices").iter(NS + "vertex")])
                tris = list(me.find(NS + "triangles").iter(NS + "triangle"))
                f = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))] for t in tris])
                paint = None
                for key in PAINT_KEYS:
                    codes = [t.get(key) or "" for t in tris]
                    if any(codes):
                        paint = codes
                        break
                vols = prusa.get(top) if depth == 0 else None
                if vols and len(vols) > 1:  # one mesh, several PrusaSlicer volumes (triangle ranges)
                    for a, b, e in vols:
                        sub = trimesh.Trimesh(v, f[a:b + 1], process=False)
                        sub.remove_unreferenced_vertices()
                        sub.apply_transform(tf)
                        out.append({"mesh": sub, "extruder": e, "paint": paint[a:b + 1] if paint else None})
                else:
                    m = trimesh.Trimesh(v, f, process=False)
                    m.apply_transform(tf)
                    e = part_ext.get((top, oid)) or (vols[0][2] if vols else None) or obj_ext.get(top, 1)
                    out.append({"mesh": m, "extruder": e, "paint": paint})
            comps = obj.find(NS + "components")
            for c in comps if comps is not None else []:
                sub = c.get(PNS + "path") or ("/" + name)
                out += mesh_parts(sub, c.get("objectid"), tf @ _matrix(c.get("transform")), top, depth + 1)
            return out

        root = doc("3D/3dmodel.model")
        res = []
        for it in root.find(NS + "build").iter(NS + "item"):
            if it.get("printable") == "0":
                continue
            oid = it.get("objectid")
            parts = mesh_parts("3D/3dmodel.model", oid, _matrix(it.get("transform")), oid)
            if parts:
                res.append({"name": names.get(oid) or f"object {oid}", "parts": parts})
    return res


def extruders_used(objects: list[dict]) -> list[int]:
    """The model's filament numbers (1-based) in use, painted ones included."""
    from paint import states
    used = set()
    for o in objects:
        for p in o["parts"]:
            used.add(p["extruder"])
            if p["paint"]:
                used |= states(p["paint"]) - {0}
    return sorted(used)


def footprint(objects: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    lo = np.min([p["mesh"].bounds[0] for o in objects for p in o["parts"]], 0)
    hi = np.max([p["mesh"].bounds[1] for o in objects for p in o["parts"]], 0)
    return lo, hi


def arrange(sizes: list[tuple[float, float]], bed=BED, gap: float = GAP, margin: float = MARGIN):
    """Shelf-pack footprints (w, d) onto as few plates as needed, as a compact (square-ish) group
    centred on the bed. Returns [(plate index, x, y of the footprint's min corner)]."""
    full = bed[0] - 2 * margin
    widest = max(w for w, _ in sizes)
    best = None
    for k in range(16):
        lim = widest + (full - widest) * k / 15
        out = _shelf(sizes, bed, gap, margin, lim)
        n = max(p[0] for p in out) + 1
        xs = [o[1] for o in out] + [o[1] + s[0] for o, s in zip(out, sizes)]
        ys = [o[2] for o in out] + [o[2] + s[1] for o, s in zip(out, sizes)]
        score = (n, max(max(xs) - min(xs), max(ys) - min(ys)))
        if best is None or score < best[0]:
            best = (score, out)
    return best[1]


def _shelf(sizes, bed, gap, margin, lim_w):
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    W, D = lim_w, bed[1] - 2 * margin
    FW = bed[0] - 2 * margin
    plates: list[list] = []          # per plate: list of shelves [y0, height, x cursor, [(i, x)]]
    place = {}
    for i in order:
        w, d = sizes[i]
        if w > W or d > D:
            raise ValueError(f"item {i} ({w:.0f} x {d:.0f} mm) is bigger than the bed")
        done = False
        for pi, shelves in enumerate(plates):
            for sh in shelves:
                if d <= sh[1] + 1e-6 and sh[2] + w <= W + 1e-6:
                    sh[3].append((i, sh[2])); sh[2] += w + gap; done = True
                    break
            if not done:
                y0 = (shelves[-1][0] + shelves[-1][1] + gap) if shelves else 0.0
                if y0 + d <= D + 1e-6:
                    shelves.append([y0, d, w + gap, [(i, 0.0)]]); done = True
            if done:
                break
        if not done:
            plates.append([[0.0, d, w + gap, [(i, 0.0)]]])
    out = [None] * len(sizes)
    for pi, shelves in enumerate(plates):
        used_w = max(sh[2] - gap for sh in shelves)
        used_d = shelves[-1][0] + shelves[-1][1]
        ox, oy = margin + (FW - used_w) / 2, margin + (D - used_d) / 2
        for sh in shelves:
            row_w = sh[2] - gap
            rx = (used_w - row_w) / 2      # centre each row too
            for i, x in sh[3]:
                w, d = sizes[i]
                out[i] = (pi, ox + rx + x, oy + sh[0] + (sh[1] - d) / 2)
    return out


def file_colours(path: str) -> dict[int, str]:
    """The colour the designer gave each filament in a project 3MF: {filament number: "#RRGGBB"}."""
    import json
    if not path.lower().endswith(".3mf"):
        return {}
    try:
        with zipfile.ZipFile(path) as z:
            d = json.loads(z.read("Metadata/project_settings.config"))
        return {i + 1: str(c)[:7] for i, c in enumerate(d.get("filament_colour") or [])}
    except (KeyError, ValueError, OSError, zipfile.BadZipFile):
        return {}


def _rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#808080").lstrip("#")[:6].ljust(6, "0")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def colour_distance(a: str, b: str) -> float:
    """Rough perceived difference between two colours (weighted RGB, "redmean")."""
    (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    rm = (r1 + r2) / 2
    return math.sqrt((2 + rm / 256) * (r1 - r2) ** 2 + 4 * (g1 - g2) ** 2 + (2 + (255 - rm) / 256) * (b1 - b2) ** 2)


def nearest_slots(model_colours: list[str], trays: list[dict], kind: str | None = None) -> list[int]:
    """For each model colour, the loaded Canvas slot whose colour is closest (only slots of material
    `kind` when given: the slicer refuses e.g. PLA and PETG in one print)."""
    loaded = [t for t in trays if t.get("loaded")] or trays
    same = [t for t in loaded if kind and (t.get("type") or "").upper() == kind.upper()]
    loaded = same or loaded
    if not loaded:
        return [0] * len(model_colours)
    return [min(loaded, key=lambda t: colour_distance(c, t.get("color") or "#808080"))["id"] for c in model_colours]


def used_fast(path: str) -> list[int]:
    """The filament numbers a 3MF really uses (part assignments + painted states), without building meshes."""
    import paint
    if not path.lower().endswith(".3mf"):
        return [1]
    used = set()
    with zipfile.ZipFile(path) as z:
        obj_ext, part_ext = _cfg_extruders(z)
        for v in _prusa_volumes(z).values():
            used |= {e for _, _, e in v}
        root = ET.fromstring(z.read("3D/3dmodel.model"))
        items = [it.get("objectid") for it in root.iter(NS + "item") if it.get("printable") != "0"]
        for oid in items:
            parts = [e for (o, _), e in part_ext.items() if o == oid]
            used |= set(parts) if parts else {obj_ext.get(oid, 1)}
        for n in z.namelist():
            if n.endswith(".model"):
                txt = z.read(n).decode("utf-8", "replace")
                codes = set(re.findall(r'(?:paint_color|mmu_segmentation)="([0-9A-Fa-f]+)"', txt))
                used |= {s for s in paint.states(codes) if s}
    return sorted(used) or [1]
