"""Write a Bambu/Orca-style *project* 3MF: several objects, each with its own filament and
per-object settings (e.g. flush_into_objects), which the slicer CLI only honours in this layout
(a plain 3MF keeps the meshes but drops per-object filaments; a half-Bambu one crashes it).

Layout copied from a real Bambu Studio project: each mesh lives in 3D/Objects/object_<k>.model,
3D/3dmodel.model wraps it as a component object and places it in <build>, and
Metadata/model_settings.config carries name/extruder/settings per object plus plate 1's list.
"""
from __future__ import annotations

import uuid
import zipfile

import numpy as np
import trimesh

NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"')


def _mesh_xml(m: trimesh.Trimesh) -> str:
    v = "".join(f'<vertex x="{a:.5f}" y="{b:.5f}" z="{c:.5f}"/>' for a, b, c in m.vertices)
    f = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in m.faces)
    return f"<mesh><vertices>{v}</vertices><triangles>{f}</triangles></mesh>"


def write(path: str, objects: list[dict]) -> str:
    """objects: [{"mesh": Trimesh, "name": str, "extruder": 1-based int, "settings": {key: value},
    "at": (x, y) optional bed position of the mesh's min corner}]. Meshes should sit on z = 0."""
    n = len(objects)
    files: dict[str, str] = {}
    res, build, rels, cfg_objs, inst, asm = [], [], [], [], [], []
    for k, ob in enumerate(objects, start=1):
        m: trimesh.Trimesh = ob["mesh"]
        wid = n + k                                  # wrapper object id (what the settings refer to)
        u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
        files[f"3D/Objects/object_{k}.model"] = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>'
            f'<metadata name="BambuStudio:3mfVersion">1</metadata><resources>'
            f'<object id="{k}" p:UUID="{u1}" type="model">{_mesh_xml(m)}</object></resources><build/></model>')
        rels.append(f'<Relationship Target="/3D/Objects/object_{k}.model" Id="rel-{k}" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>')
        res.append(f'<object id="{wid}" p:UUID="{u2}" type="model"><components>'
                   f'<component p:path="/3D/Objects/object_{k}.model" objectid="{k}" p:UUID="{uuid.uuid4()}" '
                   'transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        x, y = ob.get("at") or (20 + 60 * (k - 1), 20)
        tr = f"1 0 0 0 1 0 0 0 1 {x:.3f} {y:.3f} 0"
        build.append(f'<item objectid="{wid}" p:UUID="{uuid.uuid4()}" transform="{tr}" printable="1"/>')
        meta = "".join(f'    <metadata key="{key}" value="{val}"/>\n' for key, val in (ob.get("settings") or {}).items())
        cfg_objs.append(
            f'  <object id="{wid}">\n    <metadata key="name" value="{ob["name"]}"/>\n'
            f'    <metadata key="extruder" value="{int(ob.get("extruder", 1))}"/>\n{meta}'
            f'    <part id="{k}" subtype="normal_part">\n      <metadata key="name" value="{ob["name"]}"/>\n'
            '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            f'      <metadata key="extruder" value="{int(ob.get("extruder", 1))}"/>\n    </part>\n  </object>\n')
        inst.append(f'    <model_instance>\n      <metadata key="object_id" value="{wid}"/>\n'
                    f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{100 + k}"/>\n'
                    '    </model_instance>\n')
        asm.append(f'   <assemble_item object_id="{wid}" instance_id="0" transform="{tr}" offset="0 0 0"/>\n')
    files["3D/3dmodel.model"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n'
        ' <metadata name="Application">BambuStudio-02.08.02.61</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        f' <resources>{"".join(res)}</resources>\n <build>{"".join(build)}</build>\n</model>')
    files["3D/_rels/3dmodel.model.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + "</Relationships>")
    files["Metadata/model_settings.config"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(cfg_objs)
        + '  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value=""/>\n'
          '    <metadata key="locked" value="false"/>\n' + "".join(inst) + '  </plate>\n'
        + '  <assemble>\n' + "".join(asm) + '  </assemble>\n</config>\n')
    files["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '<Default Extension="config" ContentType="application/xml"/></Types>')
    files["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


NL = "\n"


def _mesh_xml_paint(m, paint):
    v = "".join(f'<vertex x="{a:.5f}" y="{b:.5f}" z="{c:.5f}"/>' for a, b, c in m.vertices)
    if paint:
        f = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"' + (f' paint_color="{p}"/>' if p else "/>")
                    for (a, b, c), p in zip(m.faces, paint))
    else:
        f = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in m.faces)
    return f"<mesh><vertices>{v}</vertices><triangles>{f}</triangles></mesh>"


def write_parts(path, objects, plate_name=""):
    """Like write(), but each object can hold several parts with their own filament and painted triangles:
    objects: [{"name", "settings": {}, "parts": [{"mesh" (bed coordinates), "extruder", "paint": [code]|None,
    "name"}]}]. Parts stay exactly where their meshes are; each object's parts share one mesh file."""
    files = {}
    res, build, rels, cfg_objs, inst, asm = [], [], [], [], [], []
    nid = 1
    for k, ob in enumerate(objects, start=1):
        parts = ob["parts"]
        lo = np.min([p["mesh"].bounds[0] for p in parts], axis=0)
        hi = np.max([p["mesh"].bounds[1] for p in parts], axis=0)
        cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
        sub, comps, cparts = [], [], []
        for j, pt in enumerate(parts):
            m = pt["mesh"].copy()
            m.apply_translation([-cx, -cy, -lo[2]])
            pid = nid
            nid += 1
            sub.append(f'<object id="{pid}" p:UUID="{uuid.uuid4()}" type="model">{_mesh_xml_paint(m, pt.get("paint"))}</object>')
            comps.append(f'<component p:path="/3D/Objects/object_{k}.model" objectid="{pid}" p:UUID="{uuid.uuid4()}" '
                         'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            pname = pt.get("name") or f'{ob["name"]} part {j + 1}'
            cparts.append(f'    <part id="{pid}" subtype="normal_part">{NL}      <metadata key="name" value="{pname}"/>{NL}'
                          f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>{NL}'
                          f'      <metadata key="extruder" value="{int(pt.get("extruder", 1))}"/>{NL}    </part>{NL}')
        files[f"3D/Objects/object_{k}.model"] = (
            f'<?xml version="1.0" encoding="UTF-8"?>{NL}<model unit="millimeter" xml:lang="en-US" {NS}>'
            f'<metadata name="BambuStudio:3mfVersion">1</metadata><resources>{"".join(sub)}</resources><build/></model>')
        rels.append(f'<Relationship Target="/3D/Objects/object_{k}.model" Id="rel-{k}" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>')
        wid = nid
        nid += 1
        res.append(f'<object id="{wid}" p:UUID="{uuid.uuid4()}" type="model"><components>{"".join(comps)}</components></object>')
        tr = f"1 0 0 0 1 0 0 0 1 {cx:.4f} {cy:.4f} {lo[2]:.4f}"
        build.append(f'<item objectid="{wid}" p:UUID="{uuid.uuid4()}" transform="{tr}" printable="1"/>')
        meta = "".join(f'    <metadata key="{key}" value="{val}"/>{NL}' for key, val in (ob.get("settings") or {}).items())
        cfg_objs.append(f'  <object id="{wid}">{NL}    <metadata key="name" value="{ob["name"]}"/>{NL}'
                        f'    <metadata key="extruder" value="{int(parts[0].get("extruder", 1))}"/>{NL}{meta}' + "".join(cparts) + f"  </object>{NL}")
        inst.append(f'    <model_instance>{NL}      <metadata key="object_id" value="{wid}"/>{NL}'
                    f'      <metadata key="instance_id" value="0"/>{NL}      <metadata key="identify_id" value="{100 + k}"/>{NL}'
                    f'    </model_instance>{NL}')
        asm.append(f'   <assemble_item object_id="{wid}" instance_id="0" transform="{tr}" offset="0 0 0"/>{NL}')
    files["3D/3dmodel.model"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>{NL}<model unit="millimeter" xml:lang="en-US" {NS}>{NL}'
        f' <metadata name="Application">BambuStudio-02.08.02.61</metadata>{NL}'
        f' <metadata name="BambuStudio:3mfVersion">1</metadata>{NL}'
        f' <resources>{"".join(res)}</resources>{NL} <build>{"".join(build)}</build>{NL}</model>')
    files["3D/_rels/3dmodel.model.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>{NL}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + "</Relationships>")
    files["Metadata/model_settings.config"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>{NL}<config>{NL}' + "".join(cfg_objs)
        + f'  <plate>{NL}    <metadata key="plater_id" value="1"/>{NL}    <metadata key="plater_name" value="{plate_name}"/>{NL}'
          f'    <metadata key="locked" value="false"/>{NL}' + "".join(inst) + f'  </plate>{NL}'
        + f'  <assemble>{NL}' + "".join(asm) + f'  </assemble>{NL}</config>{NL}')
    files["[Content_Types].xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>{NL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '<Default Extension="config" ContentType="application/xml"/></Types>')
    files["_rels/.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>{NL}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def box(x: float, y: float, z: float) -> trimesh.Trimesh:
    b = trimesh.creation.box((x, y, z))
    b.apply_translation(-np.array(b.bounds[0]))
    return b
