"""Plate planning and project-3MF building for combine.py (kept apart to keep that file readable)."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import zipfile

import numpy as np
import trimesh

import paint
from combine import MARGIN, arrange, extruders_used, footprint

PUCK_R = 5.0


def _local(obj: dict):
    """An object's parts moved so its footprint is centred on (0, 0) and it stands on z = 0."""
    lo, hi = footprint([obj])
    c = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    parts = []
    for p in obj["parts"]:
        m = p["mesh"].copy()
        m.apply_translation(-c)
        parts.append({**p, "mesh": m})
    return parts, (float(hi[0] - lo[0]), float(hi[1] - lo[1])), float(hi[2] - lo[2])


def _z_tops(placed: list[dict]) -> dict[int, float]:
    """How tall each filament's slice of the prime puck must be: up to where that filament is still
    used, but no higher than the last height where two or more filaments print (no colour changes
    happen above that, so a taller puck would only waste plastic). Painted parts count over their
    whole height."""
    spans: dict[int, list[tuple[float, float]]] = {}
    for ob in placed:
        for p in ob["parts"]:
            used = {p["extruder"]} | ({s for s in paint.states(p["paint"]) if s} if p.get("paint") else set())
            lo, hi = float(p["mesh"].bounds[0][2]), float(p["mesh"].bounds[1][2])
            for e in used:
                spans.setdefault(e, []).append((lo, hi))
    top = {e: max(h for _, h in v) for e, v in spans.items()}
    # highest z where at least two filaments are in use (sweep over all span edges)
    edges = sorted({z for v in spans.values() for span in v for z in span})
    last_mix = 0.0
    for a, b in zip(edges, edges[1:]):
        mid = (a + b) / 2
        if sum(1 for v in spans.values() if any(lo <= mid <= hi for lo, hi in v)) >= 2:
            last_mix = b
    return {e: min(t, last_mix + 0.6) for e, t in top.items()}


def puck(z_tops: dict[int, float]) -> list[dict]:
    """A small throwaway disc printed first after every colour change (primefirst.py moves it there):
    one pie slice per filament, each as tall as that filament is used on the plate."""
    import manifold3d as mf
    from shapely.geometry import Point, Polygon
    es = sorted(z_tops)
    disc = Point(0, 0).buffer(PUCK_R, 64)
    out = []
    for k, e in enumerate(es):
        angs = np.linspace(2 * math.pi * k / len(es), 2 * math.pi * (k + 1) / len(es), 24)
        wedge = Polygon([(0, 0)] + [(9 * math.cos(a), 9 * math.sin(a)) for a in angs]).intersection(disc)
        from shapely.geometry.polygon import orient
        cs = mf.CrossSection([list(orient(wedge, 1.0).exterior.coords)[:-1]])  # counter-clockwise = solid
        m = mf.Manifold.extrude(cs, max(0.6, z_tops[e])).to_mesh()
        out.append({"mesh": trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts), process=False),
                    "extruder": e, "paint": None})
    return out


def _slot_map(used: list[int], own, slots: list[int]) -> dict[int, int]:
    """The model's filaments in use (sorted) -> plate filament numbers. own[i] is the Canvas slot for
    used[i] (the i-th colour the model really uses, whatever its number in the file); missing ones get
    the first slot. Adds new slots to the plate's list `slots`."""
    own = list(own or [])
    mp = {}
    for i, e in enumerate(used):
        s = own[i] if i < len(own) else (own[0] if own else 0)
        if s not in slots:
            slots.append(s)
        mp[e] = slots.index(s) + 1
    return mp


def plan(items: list[dict]) -> dict:
    """items: [{"objects": combine.read(...), "slots": [Canvas slot for the model's filament 1, 2, ...],
    "copies": n, "name": str}]. Returns {"slots": the plate's Canvas slots in filament order,
    "plates": [[placed objects]], "notes": [...]}, every part renumbered to the plate's filaments."""
    slots: list[int] = []
    notes: list[str] = []
    units = []
    for it in items:
        used = extruders_used(it["objects"])
        mp = _slot_map(used, it.get("slots"), slots)
        for c in range(max(1, int(it.get("copies") or 1))):
            for ob in it["objects"]:
                if "prime puck" in ob["name"].lower():
                    continue  # the model's own throwaway puck: the plate gets one shared puck instead
                parts, size, h = _local(ob)
                parts = [{**p, "extruder": mp.get(p["extruder"], 1),
                          "paint": paint.remap(p["paint"], mp) if p.get("paint") else None} for p in parts]
                units.append({"name": (f'{it.get("name") or ""} {ob["name"]}'.strip() + (f" #{c + 1}" if c else "")),
                              "parts": parts, "size": size, "h": h, "settings": dict(it.get("settings") or {})})
    if len(slots) > 4:
        raise ValueError(f"These models need {len(slots)} different Canvas slots; the CC2 has 4")
    need_puck = len(slots) > 1
    sizes = [u["size"] for u in units] + ([(2 * PUCK_R, 2 * PUCK_R)] if need_puck else [])
    pos = arrange(sizes)
    n_pl = max(p[0] for p in pos) + 1
    plates: list[list[dict]] = [[] for _ in range(n_pl)]
    for u, (pi, x, y) in zip(units, pos):
        u["at"] = (x + u["size"][0] / 2, y + u["size"][1] / 2)
        plates[pi].append(u)
    if need_puck:
        ppi, px, py = pos[-1]
        for pi, pl in enumerate(plates):
            if len({p["extruder"] for ob in pl for p in ob["parts"]}) < 2 and not any(ob["parts"][0].get("paint") for ob in pl):
                continue  # this plate has one colour only
            at = (px + PUCK_R, py + PUCK_R) if pi == ppi else (MARGIN + PUCK_R, MARGIN + PUCK_R)
            pl.insert(0, {"name": "0 prime puck (throw away)", "parts": puck(_z_tops(pl)), "at": at, "size": (2 * PUCK_R,) * 2,
                          "settings": {"enable_support": "0", "brim_type": "no_brim"}})
        notes.append("Added a small prime puck: it takes the first bit of each new colour, so the models come out clean")
    if n_pl > 1:
        notes.append(f"It didn't all fit on one plate, so it's {n_pl} plates")
    return {"slots": slots, "plates": plates, "notes": notes}


def _fix_colours(path: str, colours: list[str]) -> None:
    """One colour per plate filament and a purge table of that size (the slicer drops parts on filaments
    it has no colour for, and refuses a purge table of the wrong size)."""
    n = len(colours)
    with zipfile.ZipFile(path) as z:
        files = {k: z.read(k) for k in z.namelist()}
    d = json.loads(files["Metadata/project_settings.config"])
    d["filament_colour"] = [c.upper() for c in colours]
    d["flush_volumes_matrix"] = [("0" if i == j else "280") for i in range(n) for j in range(n)]
    d["flush_volumes_vector"] = ["140"] * (2 * n)
    for k, v in list(d.items()):
        if k.startswith("filament_") and isinstance(v, list) and v and len(v) != n and k != "filament_colour":
            d[k] = (v * n)[:n]
    files["Metadata/project_settings.config"] = json.dumps(d, indent=4).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)


def _inject_paint(path: str, painted: dict[str, list[str]]) -> None:
    """Put painted triangle codes back on their parts (the trip through STL drops them). The CLI keeps
    each STL's triangle order, which is checked by count."""
    if not painted:
        return
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    cfg = files["Metadata/model_settings.config"].decode("utf-8")
    src = {}
    for m in re.finditer(r'<part id="(\d+)"[^>]*>(.*?)</part>', cfg, re.S):
        f = re.search(r'key="source_file" value="([^"]+)"', m.group(2))
        if f:
            src[m.group(1)] = os.path.basename(f.group(1))
    done = set()
    for name in [n for n in files if n.startswith("3D/") and n.endswith(".model")]:
        xml = files[name].decode("utf-8")
        out, pos = [], 0
        for m in re.finditer(r'<object id="(\d+)"[^>]*>(.*?)</object>', xml, re.S):
            codes = painted.get(src.get(m.group(1), ""))
            if not codes:
                continue
            body = m.group(2)
            tris = re.findall(r"<triangle [^>]*?/>", body)
            if len(tris) != len(codes):
                raise RuntimeError(f"Paint would be lost on {src[m.group(1)]}: {len(tris)} triangles, expected {len(codes)}")
            it = iter(codes)

            def tag(t):
                c = next(it)
                return f'<triangle {t.group(1)} paint_color="{c}"/>' if c else t.group(0)
            new = re.sub(r"<triangle ([^>]*?)/>", tag, body)
            out.append(xml[pos:m.start(2)] + new)
            pos = m.end(2)
            done.add(src[m.group(1)])
        if out:
            files[name] = ("".join(out) + xml[pos:]).encode("utf-8")
    missing = set(painted) - done
    if missing:
        raise RuntimeError(f"Couldn't find painted parts {sorted(missing)} in the combined file")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)


def build_plate(placed: list[dict], workdir: str, out: str, presets: list[str], colours: list[str]) -> str:
    """One project 3MF for one plate of plan()'s placed objects."""
    import merge3mf
    import slicer
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "stl"))
    stls, groups, painted = [], [], {}
    for k, ob in enumerate(placed):
        parts = []
        for j, p in enumerate(ob["parts"]):
            fn = f"u{k:03d}_{j}.stl"
            p["mesh"].export(os.path.join(workdir, "stl", fn))
            stls.append(os.path.join(workdir, "stl", fn))
            parts.append((fn, int(p["extruder"])))
            if p.get("paint"):
                painted[fn] = p["paint"]
        safe = re.sub(r'[<>&"]', "", ob["name"])[:60] or f"object {k}"
        if any(g["name"] == safe for g in groups):
            safe = f"{safe[:54]} ({k})"
        ob["_name"] = safe
        groups.append({"name": safe, "at": ob["at"], "parts": parts})
    raw = os.path.join(workdir, "raw.3mf")
    slicer.export_3mf(stls, workdir, presets, raw)
    merge3mf.merge(raw, out, groups)
    _fix_colours(out, colours)
    _inject_paint(out, painted)
    _object_settings(out, {ob["_name"]: ob.get("settings") or {} for ob in placed})
    return out


def _object_settings(path: str, per_name: dict[str, dict]) -> None:
    """Per-object print settings (e.g. a designer's "no supports" for just their model) in the project."""
    if not any(per_name.values()):
        return
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    cfg = files["Metadata/model_settings.config"].decode("utf-8")

    def add(m):
        extra = "".join(f'{chr(10)}    <metadata key="{k}" value="{v}"/>' for k, v in per_name.get(m.group(2), {}).items())
        return m.group(0) + extra
    cfg = re.sub(r'(<object id="\d+">\s*<metadata key="name" value="([^"]*)"/>)', add, cfg)
    files["Metadata/model_settings.config"] = cfg.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)


ROD_CLEARANCE = 36.0      # CC2 profile: extruder_clearance_height_to_rod (gantry over finished blocks)
HEAD_CLEARANCE = 65.0     # CC2 profile: extruder_clearance_radius (gap the slicer wants between blocks)


def plan_blocks(items: list[dict]) -> dict:
    """"One colour at a time" plates: each item (all its copies) becomes ONE block, printed start to finish
    before the next (the slicer's by-object sequence), so the colour changes once per block instead of
    every layer. Blocks are packed tight inside, kept HEAD_CLEARANCE apart, shortest first; every block
    except the last must be under the gantry (ROD_CLEARANCE)."""
    slots: list[int] = []
    blocks = []
    for it in items:
        used = extruders_used(it["objects"])
        mp = _slot_map(used, it.get("slots"), slots)
        objs = []
        for c in range(max(1, int(it.get("copies") or 1))):
            for ob in it["objects"]:
                if "prime puck" in ob["name"].lower():
                    continue
                parts, size, h = _local(ob)
                objs.append((parts, size, h))
        inner = arrange([s for _, s, _ in objs], margin=0.0)  # pack the block tight (square-ish)
        if max(p[0] for p in inner) > 0:
            raise ValueError("block too big")
        all_parts, lo, hi = [], np.array([1e9, 1e9]), np.array([-1e9, -1e9])
        for (parts, size, h), (_, x, y) in zip(objs, inner):
            for p in parts:
                m = p["mesh"].copy()
                m.apply_translation([x + size[0] / 2, y + size[1] / 2, 0])
                all_parts.append({**p, "mesh": m, "extruder": mp.get(p["extruder"], 1),
                                  "paint": paint.remap(p["paint"], mp) if p.get("paint") else None})
                lo, hi = np.minimum(lo, m.bounds[0][:2]), np.maximum(hi, m.bounds[1][:2])
        for p in all_parts:   # centre the block on its own origin
            p["mesh"].apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, 0])
        blocks.append({"name": it.get("name") or f"block {len(blocks) + 1}", "parts": all_parts,
                       "size": (float(hi[0] - lo[0]), float(hi[1] - lo[1])),
                       "h": max(float(p["mesh"].bounds[1][2]) for p in all_parts), "settings": dict(it.get("settings") or {})})
    if len(slots) > 4:
        raise ValueError(f"These models need {len(slots)} different Canvas slots; the CC2 has 4")
    blocks.sort(key=lambda b: b["h"])           # tallest last: everything before it must clear the gantry
    tall = [b for b in blocks[:-1] if b["h"] > ROD_CLEARANCE - 1]
    if tall:
        raise ValueError(f"{tall[0]['name']} is {tall[0]['h']:.0f} mm tall; only the last block may be over "
                         f"{ROD_CLEARANCE:.0f} mm (the gantry passes over finished blocks)")
    pos = corner_layout([b["size"] for b in blocks])
    if pos is None:
        raise ValueError("The blocks don't fit on one plate with the room the print head needs around each one "
                         f"({HEAD_CLEARANCE:.0f} mm); use fewer copies or print them as separate plates")
    for b, (x, y) in zip(blocks, pos):
        b["at"] = (x + b["size"][0] / 2, y + b["size"][1] / 2)
        b["settings"] = {**b["settings"]}
    def used(b):
        es = {p["extruder"] for p in b["parts"]} | {s for p in b["parts"] if p.get("paint") for s in paint.states(p["paint"]) if s}
        return sorted(es)
    order = " then ".join(f"{b['name']} (slot{'s' if len(used(b)) > 1 else ''} "
                          + "+".join(str(slots[e - 1] + 1) for e in used(b)) + ")" for b in blocks)
    multi = [b["name"] for b in blocks if len(used(b)) > 1]
    notes = [f"One model at a time: {order}."]
    if multi:
        notes.append(f"{', '.join(multi)} still changes colour every layer inside its own block (and has no prime puck "
                     "in this mode); for multi-colour models 'all together' usually comes out cleaner")
    else:
        notes.append(f"The colour changes {len(blocks) - 1} time{'s' if len(blocks) > 2 else ''} in total")
    return {"slots": slots, "plates": [blocks], "process": {"print_sequence": "by object"}, "notes": notes}


def _box_gap(a, b) -> float:
    """Shortest distance between two axis-aligned boxes (x, y, w, d)."""
    dx = max(b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]), 0.0)
    dy = max(b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]), 0.0)
    return math.hypot(dx, dy)


def corner_layout(sizes, bed=(256.0, 256.0), margin: float = 6.0, need: float = HEAD_CLEARANCE):
    """Blocks as far apart as the bed allows, for unimpeded head moves in by-object printing: in order
    front-left, back-right, front-right, back-left (block 1 and 2 diagonal), each pair at least `need` apart.
    Returns [(x, y) min corners] or None."""
    if len(sizes) > 4:
        return None
    x0, y0, x1, y1 = margin, margin, bed[0] - margin, bed[1] - margin
    spots = [lambda w, d: (x0, y0), lambda w, d: (x1 - w, y1 - d), lambda w, d: (x1 - w, y0), lambda w, d: (x0, y1 - d)]
    if len(sizes) == 1:
        w, d = sizes[0]
        return [((bed[0] - w) / 2, (bed[1] - d) / 2)]
    boxes = []
    for (w, d), spot in zip(sizes, spots):
        if w > x1 - x0 or d > y1 - y0:
            return None
        x, y = spot(w, d)
        boxes.append((x, y, w, d))
    if any(_box_gap(a, b) < need for i, a in enumerate(boxes) for b in boxes[i + 1:]):
        return None
    return [(b[0], b[1]) for b in boxes]
