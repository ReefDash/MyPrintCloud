"""Turn a slicer-written project 3MF of loose per-colour parts into multi-colour objects.

The slicer CLI only accepts project 3MFs in its own exact layout, so: export every colour piece as
an STL in the earring's own coordinates, let the CLI combine them (slicer.export_3mf), then here
regroup the pieces: one object per earring, one part per colour (with its extruder), placed where
we want it. Each CLI piece is stored centred on its own bounding box with the offset recorded as
source_offset_x/y/z, so a part's transform is that offset."""
from __future__ import annotations

import re
import zipfile


def merge(src: str, dst: str, groups: list[dict]) -> None:
    """groups: [{"name": earring name, "at": (x, y), "parts": [(stl basename, extruder)]}]"""
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    model = files["3D/3dmodel.model"].decode("utf-8")
    cfg = files["Metadata/model_settings.config"].decode("utf-8")

    # CLI objects: wrapper id -> (component xml, part block, piece name, offset)
    objs = {}
    for m in re.finditer(r'<object id="(\d+)">(.*?)</object>', cfg, re.S):
        wid, body = m.group(1), m.group(2)
        part = re.search(r"<part .*?</part>", body, re.S).group(0)
        name = re.search(r'key="name" value="([^"]+)"', body).group(1)
        off = [float(re.search(rf'key="source_offset_{a}" value="([^"]+)"', part).group(1)) for a in "xyz"]
        comp = re.search(rf'<object id="{wid}"[^>]*>\s*<components>\s*(<component [^>]*/>)', model, re.S).group(1)
        objs[name.split("/")[-1]] = {"wid": wid, "part": part, "comp": comp, "off": off}

    res, build, cfg_objs, inst = [], [], [], []
    moved: set[str] = set()
    for k, g in enumerate(groups):
        first = objs[g["parts"][0][0]]
        wid = first["wid"]
        comps, parts = [], []
        for stl, ext in g["parts"]:
            o = objs[stl]
            x, y, zz = o["off"]
            tr = f"1 0 0 0 1 0 0 0 1 {x:.6f} {y:.6f} {zz:.6f}"
            comps.append(re.sub(r'transform="[^"]*"', f'transform="{tr}"', o["comp"]))
            p = re.sub(r'key="matrix" value="[^"]*"', f'key="matrix" value="1 0 0 {x:.6f} 0 1 0 {y:.6f} 0 0 1 {zz:.6f} 0 0 0 1"', o["part"])
            p = re.sub(r'(<part [^>]*>)', rf'\1\n      <metadata key="extruder" value="{ext}"/>', p, count=1)
            p = re.sub(r'key="name" value="[^"]*"', f'key="name" value="{g["name"]} c{ext}"', p, count=1)
            parts.append("    " + p + "\n")
        # all of an object's part meshes must live in ONE mesh file (the slicer reads only the first file)
        main_path = re.search(r'p:path="([^"]+)"', first["comp"]).group(1).lstrip("/")
        for stl, _ in g["parts"][1:]:
            path = re.search(r'p:path="([^"]+)"', objs[stl]["comp"]).group(1).lstrip("/")
            if path == main_path:
                continue
            extra = re.search(r"<object .*?</object>", files.pop(path).decode("utf-8"), re.S).group(0)
            mf_ = files[main_path].decode("utf-8")
            files[main_path] = mf_.replace("</resources>", extra + "\n </resources>", 1).encode("utf-8")
            moved.add(path)
        comps = [re.sub(r'p:path="[^"]+"', f'p:path="/{main_path}"', c) for c in comps]
        uuid = re.search(rf'<object id="{wid}" (p:UUID="[^"]*")', model).group(1)
        res.append(f'  <object id="{wid}" {uuid} type="model">\n   <components>\n    ' + "\n    ".join(comps) + "\n   </components>\n  </object>\n")
        bx, by = g["at"]
        build.append(f'  <item objectid="{wid}" transform="1 0 0 0 1 0 0 0 1 {bx:.4f} {by:.4f} 0" printable="1"/>\n')
        cfg_objs.append(f'  <object id="{wid}">\n    <metadata key="name" value="{g["name"]}"/>\n'
                        f'    <metadata key="extruder" value="{g["parts"][0][1]}"/>\n' + "".join(parts) + "  </object>\n")
        inst.append(f'    <model_instance>\n      <metadata key="object_id" value="{wid}"/>\n'
                    f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{200 + k}"/>\n    </model_instance>\n')

    model = re.sub(r"<resources>.*</resources>", "<resources>\n" + "".join(res) + " </resources>", model, flags=re.S)
    model = re.sub(r"<build([^>]*)>.*</build>", lambda m: f"<build{m.group(1)}>\n" + "".join(build) + " </build>", model, flags=re.S)
    plate_meta = re.search(r"<plate>(.*?)<model_instance>", cfg, re.S).group(1)
    # many parts: the CLI spreads them over several plates; keep one plate (everything is placed on it)
    tail = "</plate>" + cfg.rsplit("</plate>", 1)[1]
    cfg = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(cfg_objs) + "  <plate>" + plate_meta + "".join(inst) + "  " + tail
    if "3D/_rels/3dmodel.model.rels" in files and moved:
        rels = files["3D/_rels/3dmodel.model.rels"].decode("utf-8")
        for path in moved:
            rels = re.sub(rf'<Relationship [^>]*Target="/{re.escape(path)}"[^>]*/>', "", rels)
        files["3D/_rels/3dmodel.model.rels"] = rels.encode("utf-8")
    files["3D/3dmodel.model"] = model.encode("utf-8")
    files["Metadata/model_settings.config"] = cfg.encode("utf-8")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
