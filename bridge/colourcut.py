"""Scale / cut a colour-painted 3MF and keep its colours ("Keep colours" on the Prepare screen).

prepare_painted(src, outdir, scale, cuts, pins, copies) -> {"plates": [3mf paths], "parts": [...], "notes": [...]}

Painted triangles carry a paint code (Orca/Bambu `paint_color`, PrusaSlicer `mmu_segmentation`). The
model is merged into one mesh with one paint STATE per triangle (0 = filament 1 ... like the slicer),
scaled, cut with manifold3d while tracking which input triangle every output triangle came from
(face_id of the original run), and written back as painted 3MF plates. New faces (the flat cut faces,
pin holes) take the colour of the nearest original triangle.
Codes that subdivide a triangle (brush painting) are reduced to the triangle's main colour."""
from __future__ import annotations

import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import trimesh
import manifold3d as mf

import paint
import splitter

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
      "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"}
LEAF = {1: "4", 2: "8", 3: "0C", 4: "1C", 5: "2C", 6: "3C", 7: "4C", 8: "5C"}   # filament n -> whole-triangle code
BED_OFF = 8.0   # splitter packs into its 240 mm usable square; the CC2 bed is 256


def _matrix(t: str | None) -> np.ndarray:
    m = np.eye(4)
    if t:
        v = [float(x) for x in t.split()]
        if len(v) == 12:
            m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
            m[:3, 3] = v[9:12]
    return m


def _main_state(code: str | None) -> int:
    """Filament (1-based) a triangle prints in: unpainted -> 0 (= the object's own filament)."""
    if not code:
        return 0
    seen: dict[int, int] = {}

    def fn(s):
        seen[s] = seen.get(s, 0) + 1
        return s
    try:
        paint._apply(code, fn)
    except Exception:
        return 0
    return max(seen, key=seen.get) if seen else 0


def _object_extruders(z: zipfile.ZipFile, names) -> dict[str, int]:
    out = {}
    for cfg in ("Metadata/model_settings.config", "Metadata/Slic3r_PE_model.config"):
        if cfg in names:
            txt = z.read(cfg).decode("utf-8", "replace")
            for oid, body in re.findall(r'<object id="(\d+)"(.*?)</object>', txt, re.S):
                m = re.search(r'key="extruder" value="(\d+)"', body)
                if m:
                    out[oid] = int(m.group(1))
    return out


def read_painted(path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Whole build plate as one mesh: vertices (mm, build transforms applied), faces, state per face
    (1-based filament; objects' own extruder applied to unpainted triangles)."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        ext = _object_extruders(z, names)
        models = {n: ET.fromstring(z.read(n)) for n in names if n.endswith(".model")}
    main = "3D/3dmodel.model" if "3D/3dmodel.model" in models else next(iter(models))
    objs: dict[tuple[str, str], ET.Element] = {}
    for n, root in models.items():
        for o in root.iter(f"{{{NS['m']}}}object"):
            objs[("/" + n.lstrip("/"), o.get("id"))] = o
    V, F, S = [], [], []

    def emit(model: str, oid: str, T: np.ndarray, own: int):
        o = objs.get((model, oid))
        if o is None:
            return
        mesh = o.find("m:mesh", NS)
        if mesh is not None:
            verts = np.array([[float(v.get("x")), float(v.get("y")), float(v.get("z"))]
                              for v in mesh.find("m:vertices", NS)], float)
            verts = (np.c_[verts, np.ones(len(verts))] @ T.T)[:, :3]
            base = sum(len(v) for v in V)
            tris = mesh.find("m:triangles", NS)
            f, s = [], []
            for t in tris:
                f.append([int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))])
                code = t.get("paint_color") or t.get("{http://schemas.slic3r.org/3mf/2017/06}mmu_segmentation")
                st = _main_state(code)
                s.append(st if st else own)
            V.append(verts)
            F.append(np.array(f, int) + base)
            S.append(np.array(s, int))
        comps = o.find("m:components", NS)
        if comps is not None:
            for c in comps:
                ref = c.get(f"{{{NS['p']}}}path") or model
                emit("/" + ref.lstrip("/"), c.get("objectid"), T @ _matrix(c.get("transform")), own)

    root = models[main]
    for it in root.find("m:build", NS):
        oid = it.get("objectid")
        emit("/" + main, oid, _matrix(it.get("transform")), ext.get(oid, 1))
    if not V:
        raise ValueError("no meshes in the 3MF")
    return np.vstack(V), np.vstack(F), np.concatenate(S)


def _code(state: int) -> str | None:
    return None if state <= 1 else LEAF.get(state, LEAF[8])


def _mesh_with_ids(v: np.ndarray, f: np.ndarray) -> mf.Manifold:
    m = mf.Manifold(mf.Mesh(vert_properties=np.asarray(v, np.float32), tri_verts=np.asarray(f, np.uint32),
                            face_id=np.arange(len(f), dtype=np.uint32)))
    if m.is_empty():
        raise ValueError("the model isn't a closed solid")
    return m.as_original()


def _body_states(body: mf.Manifold, orig_id: int, states: np.ndarray, centroids: np.ndarray, tree):
    g = body.to_mesh()
    v = np.asarray(g.vert_properties)[:, :3]
    f = np.asarray(g.tri_verts).reshape(-1, 3)
    fid = np.asarray(g.face_id) if len(g.face_id) else np.full(len(f), -1)
    st = np.zeros(len(f), int)
    ri, ro = list(g.run_index), list(g.run_original_id)
    known = np.zeros(len(f), bool)
    for r in range(len(ro)):
        a, b = ri[r] // 3, ri[r + 1] // 3
        if ro[r] == orig_id:
            ids = fid[a:b]
            ok = ids < len(states)
            st[a:b][ok] = states[ids[ok]]
            known[a:b] = ok
    if (~known).any():   # cut faces / pin holes: colour of the nearest original triangle
        c = v[f[~known]].mean(axis=1)
        _, nn = tree.query(c)
        st[~known] = states[nn]
    return v, f, st


def prepare_painted(src: str, outdir: str, scale: float = 1.0, cuts: list[dict] | None = None,
                    pins: bool = True, copies: int = 1, pin_d: float = 2.0, pin_depth: float = 6.0) -> dict:
    os.makedirs(outdir, exist_ok=True)
    from scipy.spatial import cKDTree
    v, f, states = read_painted(src)
    tm = trimesh.Trimesh(v, f, process=False)
    nf = len(tm.faces)
    tm.merge_vertices()
    assert len(tm.faces) == nf
    v = np.asarray(tm.vertices) - tm.bounds[0]
    v = v * float(scale)
    f = np.asarray(tm.faces)
    notes: list[str] = []
    extents = v.max(0) - v.min(0)
    if cuts is None:
        cuts = splitter.plan_cuts(trimesh.Trimesh(v, f, process=False)) if not splitter._fits(extents) else []
    cuts = [{"axis": str(c["axis"]).lower(), "at": float(c["at"])} for c in cuts]
    centroids = v[f].mean(axis=1)
    tree = cKDTree(centroids)
    parts = []
    if not cuts:
        parts.append((v, f, states, []))
    else:
        man = _mesh_with_ids(v, f)
        oid = man.original_id()
        holes = []
        if pins:
            for ci in range(len(cuts)):
                holes += splitter._place_pins(man, cuts, ci, pin_d, pin_depth, notes)
        work = mf.Manifold.batch_boolean([man] + [splitter._pin_cylinder(p, pin_d) for p in holes], mf.OpType.Subtract) \
            if holes else man
        pieces = [work]
        for c in cuts:
            nrm = [0.0, 0.0, 0.0]
            nrm[splitter.AX[c["axis"]]] = 1.0
            pieces = [h for p in pieces for h in p.split_by_plane(nrm, float(c["at"])) if not h.is_empty()]
        total = man.volume()
        for p in pieces:
            for b in p.decompose():
                lo, hi = np.asarray(b.bounding_box()[:3]), np.asarray(b.bounding_box()[3:])
                if (hi - lo).min() < splitter.SLIVER_T or b.volume() < max(5.0, 1e-5 * total):
                    continue
                bv, bf, bs = _body_states(b, oid, states, centroids, tree)
                normals = []
                for c in cuts:
                    i, at = splitter.AX[c["axis"]], float(c["at"])
                    if abs(hi[i] - at) < 1e-3:
                        normals.append(np.eye(3)[i])
                    elif abs(lo[i] - at) < 1e-3:
                        normals.append(-np.eye(3)[i])
                parts.append((bv, bf, bs, normals))
        notes.append(f"Cut into {len(parts)} parts, colours kept on every part")
    # lay each part down the way the plain cutter does (cut face on the bed), keep face order
    laid = []
    for pv, pf, ps, normals in parts:
        t = trimesh.Trimesh(pv, pf, process=False)
        o = splitter.orient_for_print(t, normals)
        laid.append((np.asarray(o.vertices), np.asarray(o.faces), ps))
    units = [k for k in range(len(laid)) for _ in range(max(1, int(copies)))]
    sizes = [(laid[k][0].max(0) - laid[k][0].min(0))[:2] for k in units]
    plates = splitter.pack_positions(sizes)
    stem = re.sub(r"[^\w-]+", "_", os.path.splitext(os.path.basename(src))[0])[:40]
    paths = []
    for pi, plate in enumerate(plates):
        objs = []
        for (ui, x, y) in plate:
            pv, pf, ps = laid[units[ui]]
            lo = pv.min(0)
            objs.append((pv - [lo[0] - x - BED_OFF, lo[1] - y - BED_OFF, lo[2]], pf, ps))
        p = os.path.join(outdir, f"{stem}_colours_plate{pi + 1}.3mf")
        write_painted(p, objs)
        paths.append(p)
    return {"plates": paths, "notes": notes, "cuts": cuts,
            "parts": [{"size": [round(float(s), 2) for s in (pv.max(0) - pv.min(0))]} for pv, _, _ in laid]}


def write_painted(path: str, objs) -> None:
    b = io.StringIO()
    b.write('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n<resources>\n')
    for i, (v, f, s) in enumerate(objs):
        b.write(f'<object id="{i + 1}" type="model" name="part{i + 1}"><mesh><vertices>')
        b.write("".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v))
        b.write("</vertices><triangles>\n")
        for (a, c, d), st in zip(f, s):
            code = _code(int(st))
            extra = f' paint_color="{code}" slic3rpe:mmu_segmentation="{code}"' if code else ""
            b.write(f'<triangle v1="{a}" v2="{c}" v3="{d}"{extra}/>\n')
        b.write("</triangles></mesh></object>\n")
    b.write("</resources>\n<build>\n")
    for i in range(len(objs)):
        b.write(f'<item objectid="{i + 1}" printable="1"/>\n')
    b.write("</build>\n</model>\n")
    ct = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", b.getvalue())
