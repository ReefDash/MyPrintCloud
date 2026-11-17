"""Slice STL/3MF with the ElegooSlicer CLI using Elegoo's own Centauri Carbon 2 presets.

"Best settings" = the stock 0.20mm Standard CC2 process with tree supports switched on
(auto: only where overhangs need it) and the stock Elegoo filament preset matching what
is loaded in each Canvas tray. Presets are resolved from the slicer's install folder
(their "inherits" chains flattened) because the CLI only takes standalone JSONs.
"""
from __future__ import annotations

import html
import glob
import json
import logging
import math
import os
import re
import subprocess
import zipfile

log = logging.getLogger("slicer")

SLICER = os.environ.get("MPC_SLICER", r"C:\Program Files\ElegooSlicer\elegoo-slicer.exe")
PROFILES = os.environ.get("MPC_PROFILES", r"C:\Program Files\ElegooSlicer\resources\profiles\Elegoo")
# on the cloud server: OrcaSlicer (ElegooSlicer is Windows/x86 only) fed the same flattened Elegoo presets
ORCA = os.name != "nt"
if ORCA:
    SLICER = os.environ.get("MPC_SLICER", os.path.expanduser("~/orca/orca-slicer"))
    PROFILES = os.environ.get("MPC_PROFILES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles", "Elegoo"))
MACHINE = "Elegoo Centauri Carbon 2 0.4 nozzle"
QUALITY = {
    "draft": "0.24mm Draft @Elegoo CC2 0.4 nozzle",
    "standard": "0.20mm Standard @Elegoo CC2 0.4 nozzle",
    "fine": "0.12mm Fine @Elegoo CC2 0.4 nozzle",
    "strong": "0.20mm Strength @Elegoo CC2 0.4 nozzle",
    "showpiece": "0.16mm Optimal @Elegoo CC2 0.4 nozzle",
    "jewel": "0.12mm Fine @Elegoo CC2 0.4 nozzle",
}
# "Show piece": looks over speed, for figures / masks / jewellery (on top of the 0.16 mm profile)
SHOWPIECE = {
    "outer_wall_speed": "80", "outer_wall_acceleration": "2000",     # the visible wall: slow + gentle
    "inner_wall_speed": "150", "small_perimeter_speed": "30%",
    "precise_outer_wall": "1",
    "top_surface_speed": "80", "top_surface_acceleration": "1500",   # smooth, gap-free tops
    "seam_slope_type": "external", "seam_slope_conditional": "1", "scarf_angle_threshold": "155",
    "seam_slope_steps": "10", "seam_slope_min_length": "10", "wipe_on_loops": "1",
    "wipe_before_external_loop": "1", "staggered_inner_seams": "1", "role_based_wipe_speed": "1",
    "overhang_2_4_speed": "40", "overhang_3_4_speed": "20",
    "support_interface_top_layers": "3", "support_interface_spacing": "0.2",  # cleaner supported undersides
    "reduce_crossing_wall": "1", "z_hop_types": "Slope Lift",       # no travel scars or zits on outer faces
}
# "Tiny & crisp": earrings, charms, small multi-colour parts. Slow and gentle so tiny features stay sharp,
# and a prime tower so every colour change starts clean (no jade smudged into the gold).
JEWEL = {**SHOWPIECE,
    "outer_wall_speed": "25", "inner_wall_speed": "40", "small_perimeter_speed": "15", "small_perimeter_threshold": "8",
    "top_surface_speed": "30", "internal_solid_infill_speed": "40", "sparse_infill_speed": "50", "gap_infill_speed": "25",
    "initial_layer_speed": "20", "initial_layer_infill_speed": "30", "travel_speed": "200",
    "default_acceleration": "1500", "outer_wall_acceleration": "800", "inner_wall_acceleration": "1200",
    "top_surface_acceleration": "800", "initial_layer_acceleration": "500",
    "wall_loops": "3", "top_shell_layers": "6", "bottom_shell_layers": "4",
    "seam_position": "back", "enable_prime_tower": "1", "prime_tower_width": "30",
    "brim_type": "no_brim",
}
PROC_OVR = {
    "enable_support": "1",
    "support_type": "tree(auto)",
    "support_style": "default",
    "support_threshold_angle": "30",
    "brim_type": "auto_brim",
    "elefant_foot_compensation": "0.15",
    # the CLI ignores the machine's default_bed_type and assumes Cool Plate, which PETG etc. refuse
    "curr_bed_type": "Textured PEI Plate",
}

_by_name: dict[str, dict] = {}


def _index() -> None:
    if _by_name:
        return
    for p in glob.glob(os.path.join(PROFILES, "**", "*.json"), recursive=True):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "name" in d:
            _by_name.setdefault(d["name"], d)


def _resolve(name: str) -> dict:
    d = _by_name[name]
    out = dict(_resolve(d["inherits"])) if d.get("inherits") else {}
    out.update(d)
    out.pop("inherits", None)
    return out


def filament_preset(ftype: str, fname: str = "") -> str:
    """Best Elegoo @ECC2 preset for a tray: exact name, then type, then PLA."""
    _index()
    for cand in (f"{fname} @ECC2", f"Elegoo {fname} @ECC2", f"Elegoo {ftype} @ECC2",
                 f"Elegoo {ftype.split('-')[0]} @ECC2"):
        if cand.strip() and cand in _by_name:
            return cand
    return "Elegoo PLA @ECC2"


def filament_value(preset: str, key: str, default=None):
    """A stock filament preset's value (first entry of Orca's per-extruder lists), as a float when numeric."""
    _index()
    try:
        v = _resolve(preset).get(key, default)
    except KeyError:
        return default
    if isinstance(v, list):
        v = v[0] if v else default
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def process_value(quality: str, key: str, default=None):
    _index()
    v = {**_resolve(QUALITY.get(quality, QUALITY["standard"])), **PROC_OVR}.get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def write_profiles(outdir: str, quality: str, filaments: list[str], overrides: dict | None = None,
                   filament_overrides: list[dict] | None = None) -> list[str]:
    """Write machine/process/filament JSONs; returns [machine, process, filament...] paths.

    filament_overrides: per filament, e.g. calibrated {"filament_flow_ratio": 1.02} (scalars are
    wrapped into Orca's per-extruder lists)."""
    _index()
    os.makedirs(outdir, exist_ok=True)
    paths = []
    proc_name = QUALITY.get(quality, QUALITY["standard"])
    looks = {"showpiece": SHOWPIECE, "jewel": JEWEL}.get(quality, {})
    multi = {}
    if len(filaments) > 1:   # painted / multi-colour 3MFs: without these sized to the filament count the CLI
        n = len(filaments)   # silently prints everything in filament 1 (same values as combine_build)
        cols = []
        for f in filaments:
            c = _resolve(f).get("filament_colour") or ["#FFFFFF"]
            cols.append(c[0] if isinstance(c, list) else c)
        multi = {"filament_colour": cols,
                 "flush_volumes_matrix": [("0" if i == j else "280") for i in range(n) for j in range(n)],
                 "flush_volumes_vector": ["140"] * (2 * n)}
    jobs = [("machine", MACHINE, {}), ("process", proc_name, {**PROC_OVR, **looks, **multi, **(overrides or {})})]
    fo = list(filament_overrides or [])
    cool = ({"slow_down_layer_time": "8"} if quality == "showpiece"           # tiny parts get time to cool: sharper detail
            else {"slow_down_layer_time": "15", "slow_down_min_speed": "8"} if quality == "jewel" else {})
    jobs += [(f"filament{i}", f, {**cool, **(fo[i] if i < len(fo) else {})}) for i, f in enumerate(filaments)]
    if ORCA:  # Orca sizes its colour-change maths by filament_colour (one entry per filament, else it crashes)
        cols = []
        for f in filaments:
            c = _resolve(f).get("filament_colour") or ["#FFFFFF"]
            cols.append(c[0] if isinstance(c, list) else c)
        jobs[1] = ("process", proc_name, {"filament_colour": cols, "flush_multiplier": ["1"], **jobs[1][2]})
    for tag, name, ovr in jobs:
        d = _resolve(name)
        for k, v in ovr.items():
            d[k] = [str(v)] if isinstance(d.get(k), list) and not isinstance(v, list) else (v if isinstance(v, list) else str(v))
        d["from"] = "User"
        # the CLI rejects user presets that reuse a system preset's name
        if tag == "machine":
            d["name"] = d["printer_settings_id"] = "MPC CC2 0.4"
            d["inherits"] = name
        elif tag == "process":
            d["name"] = d["process_settings_id"] = "MPC " + quality
            # an empty list makes the CLI abort; it must name the system printer
            d["compatible_printers"], d["compatible_printers_condition"] = [MACHINE], ""
        else:
            d["filament_settings_id"] = d["name"]
            d["compatible_printers"], d["compatible_printers_condition"] = [], ""
        p = os.path.join(outdir, tag + ".json")
        json.dump(d, open(p, "w", encoding="utf-8"), indent=1)
        paths.append(p)
    return paths


def export_3mf(models: list[str], workdir: str, filaments: list[str], out_path: str, quality: str = "standard",
               overrides: dict | None = None) -> str:
    """Have the slicer combine models (a project 3MF plus extra STLs) into one project 3MF, so the
    extras get the slicer's own (valid) project layout. Raises RuntimeError on failure."""
    paths = write_profiles(os.path.join(workdir, "profiles"), quality, filaments, overrides)
    args = [SLICER, "--load-settings", f"{paths[0]};{paths[1]}", "--load-filaments", ";".join(paths[2:]),
            "--arrange", "1", "--orient", "0", "--export-3mf", out_path, *models]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not os.path.exists(out_path):
        raise RuntimeError(f"Couldn't combine the model with its purge object (exit {proc.returncode})")
    return out_path


def author_settings(path: str) -> dict:
    """What the model's author printed it with (a 3MF's embedded project settings), a few key fields."""
    if not path.lower().endswith(".3mf"):
        return {}
    try:
        with zipfile.ZipFile(path) as z:
            if "Metadata/project_settings.config" not in z.namelist():
                return {}
            d = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
    except (zipfile.BadZipFile, ValueError, OSError):
        return {}
    out = {}
    for k in ("spiral_mode", "sparse_infill_density", "top_shell_layers", "bottom_shell_layers", "wall_loops", "layer_height"):
        v = d.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v is not None:
            out[k] = v
    return out


def file_plates(path: str) -> list[dict]:
    """The plates a Bambu/Orca-style 3MF was laid out on: [{"n", "name", "objects": [ids]}], or [] if one/none."""
    if not path.lower().endswith(".3mf"):
        return []
    try:
        with zipfile.ZipFile(path) as z:
            cfg = z.read("Metadata/model_settings.config").decode("utf-8", "replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return []
    out = []
    for m in re.finditer(r"<plate>(.*?)</plate>", cfg, re.S):
        b = m.group(1)
        pid = re.search(r'key="plater_id" value="(\d+)"', b)
        name = re.search(r'key="plater_name" value="([^"]*)"', b)
        objs = re.findall(r'key="object_id" value="(\d+)"', b)
        if pid and objs:
            out.append({"n": int(pid.group(1)), "name": (name.group(1) if name else "").strip(), "objects": objs})
    return sorted(out, key=lambda x: x["n"]) if len(out) > 1 else []


def object_names(path: str) -> dict[str, str]:
    """{object id: name} from a Bambu/Orca 3MF's model_settings.config (names the designer gave the parts)."""
    try:
        with zipfile.ZipFile(path) as z:
            cfg = z.read("Metadata/model_settings.config").decode("utf-8", "replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return {}
    out = {}
    for m in re.finditer(r'<object id="(\d+)">(.*?)</object>', cfg, re.S):
        name = re.search(r'<metadata key="name" value="([^"]*)"', m.group(2))
        if name:
            out[m.group(1)] = html.unescape(name.group(1)).strip()
    # tidy: "Sidetrack Assembly - R1.0.stl_7" -> "Part 7" when many parts share that name; drop file extensions
    bare = {k: re.sub(r"\.(stl|obj|step|stp|3mf)(?=_\d+$|$)", "", v, flags=re.I) for k, v in out.items()}
    stems = [re.sub(r"_\d+$", "", v) for v in bare.values()]
    for k, v in bare.items():
        mm = re.fullmatch(r"(.*)_(\d+)", v)
        if mm and stems.count(mm.group(1)) >= 3:
            out[k] = f"Part {mm.group(2)}"
        else:
            out[k] = v.replace("_", " ").strip() or out[k]
    return out


def plate_file(src: str, plate: dict, count: int, dest: str) -> str:
    """A copy of a multi-plate 3MF holding only `plate`'s objects, moved onto plate 1.

    Bambu/Orca lay plate i (0-based) out on a grid ceil(sqrt(count)) wide, each a bed width/depth * 1.2
    apart, and ElegooSlicer's CLI doesn't place other slicers' plates the same way, so we move them."""
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        model = z.read("3D/3dmodel.model").decode("utf-8")
        cfg = z.read("Metadata/model_settings.config").decode("utf-8")
        proj = z.read("Metadata/project_settings.config").decode("utf-8", "replace") if "Metadata/project_settings.config" in names else ""
        area = [tuple(float(v) for v in xy.split("x")) for xy in re.findall(r'"(\d+(?:\.\d+)?x\d+(?:\.\d+)?)"',
                (re.search(r'"printable_area":\s*\[(.*?)\]', proj, re.S) or re.search("()", "")).group(1))]
        w = max((x for x, _ in area), default=256.0)
        d = max((y for _, y in area), default=256.0)
        cols = max(1, math.ceil(math.sqrt(count)))
        i = plate["n"] - 1
        dx, dy = (i % cols) * w * 1.2, -(i // cols) * d * 1.2
        keep = set(plate["objects"])

        def item(m: re.Match) -> str:
            tag = m.group(0)
            oid = re.search(r'objectid="(\d+)"', tag).group(1)
            if oid not in keep:
                return ""
            def tr(t: re.Match) -> str:
                v = t.group(1).split()
                if len(v) == 12:
                    v[9], v[10] = repr(float(v[9]) - dx), repr(float(v[10]) - dy)
                return f'transform="{" ".join(v)}"'
            return re.sub(r'transform="([^"]*)"', tr, tag)
        build0 = model.index("<build")
        model = model[:build0] + re.sub(r"<item\s[^>]*/>", item, model[build0:])
        # the other plates' objects go entirely: the loader rejects settings for objects that aren't built
        drop = lambda m: m.group(0) if m.group(1) in keep else ""
        model = re.sub(r'\s*<object id="(\d+)"[^>]*>.*?</object>', drop, model, flags=re.S)
        cfg = re.sub(r'\s*<object id="(\d+)">.*?</object>', drop, cfg, flags=re.S)
        cfg = re.sub(r'\s*<assemble_item object_id="(\d+)"[^>]*/>', drop, cfg)
        mine = next(b for b in re.findall(r"<plate>.*?</plate>", cfg, re.S)
                    if re.search(r'key="plater_id" value="%d"' % plate["n"], b))
        mine = re.sub(r'(key="plater_id" value=")\d+', r"\g<1>1", mine)
        cfg = re.sub(r"\s*<plate>.*?</plate>", "", cfg, flags=re.S)
        tail = "  <assemble>" if "<assemble>" in cfg else "</config>"
        cfg = cfg.replace(tail.strip(), mine + "\n" + tail, 1)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
            for n in names:
                if n == "3D/3dmodel.model":
                    out.writestr(n, model)
                elif n == "Metadata/model_settings.config":
                    out.writestr(n, cfg)
                elif re.fullmatch(r"Metadata/plate_\d+\.gcode(\.md5)?", n):
                    continue  # pre-sliced Bambu gcode: not ours
                else:
                    out.writestr(z.getinfo(n), z.read(n))
    return dest


def plate_png(src: str, n: int) -> bytes | None:
    try:
        with zipfile.ZipFile(src) as z:
            return z.read(f"Metadata/plate_{n}.png")
    except (KeyError, OSError, zipfile.BadZipFile):
        return None


PAINT_EXTRUDER = {b"4": 1, b"8": 2, b"0C": 3, b"1C": 4, b"2C": 5, b"3C": 6, b"4C": 7, b"5C": 8}


def painted_filament_count(z, names) -> int:
    """Highest filament used by colour-painted triangles (Orca/Bambu paint_color, PrusaSlicer
    mmu_segmentation); unpainted = filament 1. Reads the codes' leading state only (enough for whole-
    triangle paint, which is what our own painted plates use)."""
    top = 1
    for nm in names:
        if not nm.endswith(".model"):
            continue
        with z.open(nm) as f:
            tail = b""
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                buf = tail + chunk
                for code in re.findall(rb'(?:paint_color|mmu_segmentation)="([0-9A-F]{1,2})"', buf):
                    top = max(top, PAINT_EXTRUDER.get(code, 1))
                tail = buf[-64:]
    return top


def model_filament_count(path: str) -> int:
    """How many filaments/extruders a 3MF asks for (1 for STL)."""
    if not path.lower().endswith(".3mf"):
        return 1
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            n = 1
            for cfg in ("Metadata/project_settings.config",):
                if cfg in names:
                    d = json.loads(z.read(cfg).decode("utf-8", "replace"))
                    n = max(n, len(d.get("filament_colour") or d.get("filament_type") or [1]))
            for cfg in ("Metadata/model_settings.config", "Metadata/Slic3r_PE_model.config"):
                if cfg in names:
                    ext = re.findall(rb'key="extruder" value="(\d+)"', z.read(cfg))
                    if ext:
                        n = max(n, max(int(e) for e in ext))
            n = max(n, painted_filament_count(z, names))
            return min(n, 4)
    except (zipfile.BadZipFile, ValueError, OSError):
        return 1


def _has_prime_puck(path: str) -> bool:
    """A project 3MF with a 'prime puck' object: a throwaway part laid out to be printed first after
    every colour change (the CC2 profile has no prime tower), so its layout must not be re-arranged."""
    if not path.lower().endswith(".3mf"):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return b"prime puck" in z.read("Metadata/model_settings.config")
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def author_layout_settings(path: str) -> dict:
    """Supports / brim the designer set in their 3MF (Bambu/Orca or PrusaSlicer project): turned off, or
    on but only from the build plate / a given type or angle.
    Their own tight layout relies on them: our default tree supports and brim would run into the
    neighbouring parts (the CLI then refuses the plate and scatters everything)."""
    out = {}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "Metadata/project_settings.config" in names:
                d = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
                one = lambda v: v[0] if isinstance(v, list) and v else v
                if str(one(d.get("enable_support"))) == "0":
                    out["enable_support"] = "0"
                elif str(one(d.get("enable_support"))) == "1":   # supports on: keep how the designer set them up
                    if str(one(d.get("support_on_build_plate_only"))) in ("1", "true"):
                        out["support_on_build_plate_only"] = "1"
                    if str(one(d.get("support_type")) or "").startswith(("tree", "normal")):
                        out["support_type"] = str(one(d.get("support_type")))
                    if str(one(d.get("support_threshold_angle")) or "").isdigit():
                        out["support_threshold_angle"] = str(one(d.get("support_threshold_angle")))
                bt = one(d.get("brim_type"))
                if bt in ("no_brim", "outer_only", "inner_only", "outer_and_inner", "auto_brim"):   # 11/12: the file's brim, not only "none"
                    out["brim_type"] = bt
                    bw = str(one(d.get("brim_width")) or "")
                    if bt != "no_brim" and re.fullmatch(r"\d+(\.\d+)?", bw) and 0 < float(bw) <= 20:
                        out["brim_width"] = bw
                if str(one(d.get("spiral_mode"))) in ("1", "true"):   # 11/12: vase-mode designs (one continuous wall, open top)
                    out["spiral_mode"] = "1"   # the CLI refuses vase mode unless these match it
                    out.update({"enable_support": "0", "wall_loops": "1", "top_shell_layers": "0",
                                "sparse_infill_density": "0%", "enable_prime_tower": "0"})
                    bsl = str(one(d.get("bottom_shell_layers")) or "")
                    if bsl.isdigit() and 0 < int(bsl) <= 12:
                        out["bottom_shell_layers"] = bsl
            elif "Metadata/Slic3r_PE.config" in names:
                c = dict(re.findall(r"^; (\w+) = (.*)$", z.read("Metadata/Slic3r_PE.config").decode("utf-8", "replace"), re.M))
                if c.get("support_material") == "0":
                    out["enable_support"] = "0"
                elif c.get("support_material") == "1" and c.get("support_material_buildplate_only") == "1":
                    out["support_on_build_plate_only"] = "1"
                if c.get("brim_width") in ("0", "0.0"):
                    out["brim_type"] = "no_brim"
    except (zipfile.BadZipFile, ValueError, OSError, KeyError):
        pass
    return out


LAYOUT_GAP = 4.5   # mm: the CLI rejects plates whose parts are closer ("G-code conflicts", return -101)
BED_CENTRE = (128.0, 128.0)
EXCLUDE = (246.0, 0.0, 256.0, 20.0)   # CC2 bed_exclude_area (x0, y0, x1, y1)
EX_MARGIN = 3.0


def _is_painted(path: str) -> bool:
    """Colour-painted triangles (Orca/Bambu paint_color, PrusaSlicer mmu_segmentation): flattening the
    project into plain STL parts would throw the second colour away."""
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith(".model"):
                    with z.open(n) as f:
                        while True:
                            chunk = f.read(1 << 20)
                            if not chunk:
                                break
                            if b"paint_color=" in chunk or b"mmu_segmentation=" in chunk:
                                return True
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return False


def flatten_3mf(path: str, folder: str) -> list[str]:
    """Every build item of a 3MF as its own STL, where the file placed it (mirrored copies included:
    the CLI misreads PrusaSlicer files' shared parts)."""
    import splitter
    with zipfile.ZipFile(path) as z:
        ids = re.findall(r'<item [^>]*?objectid="(\d+)"', z.read("3D/3dmodel.model").decode("utf-8", "replace"))
    ms = splitter.plate_objects(path, ids)
    os.makedirs(folder, exist_ok=True)
    out = []
    for k, m in enumerate(ms):
        out.append(os.path.join(folder, f"o{k:03d}.stl"))
        m.export(out[-1], file_type="stl")
    return out


def layout_stls(paths: list[str], folder: str, gap: float = LAYOUT_GAP) -> list[str]:
    """The parts kept in their own arrangement, gaps opened up just enough for the slicer
    (spread out from the group's centre), the group centred on the bed. Raises if it can't fit."""
    import numpy as np
    import trimesh
    from shapely.ops import unary_union
    from trimesh.path.polygons import projected
    ms = [trimesh.load(p, force="mesh") for p in paths]
    for m in ms:
        m.apply_translation([0, 0, -m.bounds[0][2]])
    feet = []
    for m in ms:
        try:
            feet.append(projected(m, normal=[0, 0, 1]))
        except Exception:
            feet.append(None)
    if any(f is None or f.is_empty for f in feet):
        from shapely.geometry import box
        feet = [box(*m.bounds[0][:2], *m.bounds[1][:2]) for m in ms]
    cen = np.array([[f.centroid.x, f.centroid.y] for f in feet])
    lo = np.min([m.bounds[0][:2] for m in ms], 0)
    hi = np.max([m.bounds[1][:2] for m in ms], 0)
    mid = (lo + hi) / 2
    from shapely import affinity

    def min_gap(k):
        moved = [affinity.translate(f, *((c - mid) * (k - 1))) for f, c in zip(feet, cen)]
        d = math.inf
        for i in range(len(moved)):
            for j in range(i + 1, len(moved)):
                if moved[i].distance(moved[j]) < d:
                    d = moved[i].distance(moved[j])
        return d
    k = 1.0
    if len(ms) > 1:
        while min_gap(k) < gap and k < 3.0:
            k += 0.03
    shifts = [(c - mid) * (k - 1) for c in cen]
    lo2 = np.min([m.bounds[0][:2] + s for m, s in zip(ms, shifts)], 0)
    hi2 = np.max([m.bounds[1][:2] + s for m, s in zip(ms, shifts)], 0)
    if k >= 3.0 or np.any(hi2 - lo2 > 2 * np.array(BED_CENTRE) - 6):
        raise RuntimeError("the file's layout doesn't fit the bed once spaced for the slicer")
    off = np.array(BED_CENTRE) - (lo2 + hi2) / 2
    # keep out of the bed's no-go corner (CC2: purge chute, front right): slide the layout left if a part reaches it
    ex0, ey0, ex1, ey1 = EXCLUDE
    reach = []
    for m, s in zip(ms, shifts):
        for b in (m.split(only_watertight=False) if len(ms) == 1 else [m]):
            if len(b.faces) < 12:
                continue
            (bx0, by0), (bx1, by1) = b.bounds[0][:2] + s + off, b.bounds[1][:2] + s + off
            if by0 < ey1 + EX_MARGIN and by1 > ey0 - EX_MARGIN:
                reach.append(bx1)
    need = (max(reach) - (ex0 - EX_MARGIN)) if reach else 0.0
    if need > 0:
        room = (lo2[0] + off[0]) - 1.0
        if need <= room:
            off[0] -= need
            log.info("slid the layout %.1f mm left, clear of the bed's no-go corner", need)
    os.makedirs(folder, exist_ok=True)
    out = []
    for p, m, s in zip(paths, ms, shifts):
        m.apply_translation([s[0] + off[0], s[1] + off[1], 0])
        out.append(os.path.join(folder, os.path.basename(p)))
        m.export(out[-1], file_type="stl")
    log.info("kept the file's layout: %d parts, spread x%.2f, %.0f x %.0f mm", len(ms), k, *(hi2 - lo2))
    return out


def _is_plate(path: str) -> bool:
    """An STL holding several separate parts (a ready-made print plate): never auto-orient it,
    the slicer would tip or turn the whole plate as one object and push it off the bed."""
    try:
        import trimesh
        m = trimesh.load(path, force="mesh")
        if len(m.faces) > 3_000_000:
            return False
        n = trimesh.graph.connected_components(m.face_adjacency, min_len=1, nodes=None)
        big = [c for c in n if len(c) >= 12]
        return len(big) > 1
    except Exception:
        return False


def _turn_layout(paths: list[str], folder: str, ang: float) -> list[str]:
    """Copies of the part files turned `ang` degrees round the bed centre (the whole layout moves together)."""
    import numpy as np
    import trimesh
    os.makedirs(folder, exist_ok=True)
    t = trimesh.transformations
    M = t.translation_matrix([*BED_CENTRE, 0]) @ t.rotation_matrix(math.radians(ang), [0, 0, 1])         @ t.translation_matrix([-BED_CENTRE[0], -BED_CENTRE[1], 0])
    out = []
    for p in paths:
        m = trimesh.load(p, force="mesh")
        m.apply_transform(M)
        out.append(os.path.join(folder, os.path.basename(p)))
        m.export(out[-1], file_type="stl")
    return out


def slice_model(model: str | list[str], workdir: str, filaments: list[str], quality: str = "standard",
                plate: int = 1, overrides: dict | None = None, timeout_s: int = 1800,
                orient: bool | None = None, filament_overrides: list[dict] | None = None, arrange: bool | None = True,
                _turned: bool = False, _rebuilt: bool = False) -> str:
    """Slice one plate of `model` (or several part files arranged onto one plate); returns the gcode path.

    orient None = let the slicer auto-orient STLs; False keeps the parts exactly as prepared
    (split parts must stay cut-face down). Raises RuntimeError with the log tail."""
    models = [model] if isinstance(model, str) else list(model)
    if orient is None:
        orient = all(m.lower().endswith(".stl") for m in models) and len(models) == 1 and not _is_plate(models[0])
    given = model
    keep_layout = arrange is None  # None = keep the file's own layout; arrange only if that won't slice
    one3mf = len(models) == 1 and models[0].lower().endswith(".3mf")
    if keep_layout:
        arrange = False
        if one3mf:  # the designer's support / brim choice, unless the user picked one
            mine = overrides or {}
            if "enable_support" in mine or "support_type" in mine:
                theirs = {k: v for k, v in author_layout_settings(models[0]).items() if k != "enable_support"}
            else:
                theirs = author_layout_settings(models[0])
            vase = author_layout_settings(models[0]) if theirs.get("spiral_mode") == "1" else {}
            theirs = {k: v for k, v in theirs.items() if k not in mine}
            if vase:   # 11/12: vase mode only slices with its own wall/infill/support values, so they beat the user's
                theirs.update({k: v for k, v in vase.items() if k in ("spiral_mode", "enable_support", "wall_loops",
                               "top_shell_layers", "sparse_infill_density", "enable_prime_tower")})
                mine = {k: v for k, v in mine.items() if k not in theirs}
            if theirs:
                log.info("using the designer's %s", theirs)
                overrides = {**mine, **theirs}
        else:
            try:
                model = models = layout_stls(models, os.path.join(workdir, "layout"))
            except Exception as e:
                log.info("can't keep the layout (%s); arranging", e)
                arrange, keep_layout = True, False
    if arrange and any(_has_prime_puck(m) for m in models):
        arrange = False  # the puck only works where the designer put it (printed first after each swap)
    paths = write_profiles(os.path.join(workdir, "profiles"), quality, filaments, overrides, filament_overrides)
    out = os.path.join(workdir, "out")
    os.makedirs(out, exist_ok=True)
    for f in glob.glob(os.path.join(out, "plate_*.gcode")):
        os.remove(f)
    args = [SLICER, "--load-settings", f"{paths[0]};{paths[1]}",
            "--load-filaments", ";".join(paths[2:]),
            "--arrange", "1" if arrange else "0", "--orient", "1" if orient else "0",
            "--slice", str(plate), "--outputdir", out, *models]
    log.info("slicing %s (%s, %d filament(s))", ", ".join(os.path.basename(m) for m in models), quality, len(filaments))
    logf = os.path.join(workdir, "slice.log")
    for attempt in range(3):   # the CLI is flaky on fine layers + painted colours (crash 0xC0000005 / exit -100)
        with open(logf, "w", encoding="utf-8", errors="replace") as lf:
            proc = subprocess.run(args, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout_s,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        found = sorted(glob.glob(os.path.join(out, "plate_*.gcode")))
        if found or (proc.returncode & 0xFFFFFFFF) not in (0xC0000005, 0xFFFFFF9C):
            break
        log.info("slicer exit %s, retrying (%d)", proc.returncode, attempt + 1)
    if not found and keep_layout:
        log.info("the file's own layout didn't slice (exit %s)", proc.returncode)
        if one3mf and model_filament_count(models[0]) == 1 and not _is_painted(models[0]):  # often parts too close, or misread copies
            try:
                parts = flatten_3mf(models[0], os.path.join(workdir, "flat"))
                return slice_model(parts, workdir, filaments, quality, plate, overrides, timeout_s, orient,
                                   filament_overrides, arrange=None)
            except Exception as e:
                log.info("re-laying the file's parts failed (%s); arranging", e)
        if not one3mf and not _turned:
            # a full plate can poke into the bed's no-go corner (the CC2's purge chute, bed_exclude_area):
            # turning the whole layout round usually clears it without changing the plate
            for ang in (180, 90, 270):
                try:
                    turned = _turn_layout(models, os.path.join(workdir, f"turn{ang}"), ang)
                    g = slice_model(turned, workdir, filaments, quality, plate, overrides, timeout_s, orient,
                                    filament_overrides, arrange=False, _turned=True)
                    log.info("sliced with the layout turned %d degrees (clear of the no-go corner)", ang)
                    return g
                except Exception as e:
                    log.info("turned %d degrees: %s", ang, e)
        return slice_model(given, workdir, filaments, quality, plate, overrides,
                           timeout_s, orient, filament_overrides, arrange=True, _turned=_turned)
    if not found and arrange and any(m.lower().endswith(".3mf") for m in models):
        # a packed project plate (e.g. 50 earrings + the prime tower): the CLI's arrange gives up
        # with "return -21", but the designer's own layout slices fine
        log.info("slice with arrange failed (exit %s); retrying with the file's own layout", proc.returncode)
        return slice_model(model, workdir, filaments, quality, plate, overrides, timeout_s, orient,
                           filament_overrides, arrange=False)
    if not found and not arrange and one3mf and not _rebuilt:
        # 11/15: some multi-colour 3MFs (e.g. our own coaster writers) slice in the GUI but the CLI rejects
        # them with exit -24 whenever a part uses filament 2+. Re-built through combine (the CLI's own
        # project layout + a prime puck) the same model slices.
        rebuilt = _rebuild_multicolour(models[0], workdir, filaments)
        if rebuilt:
            log.info("the file didn't slice as-is (exit %s); re-built it in the slicer's own layout", proc.returncode)
            return slice_model(rebuilt, workdir, filaments, quality, plate, overrides, timeout_s, False,
                               filament_overrides, arrange=False, _turned=True, _rebuilt=True)
    if not found:
        lines = [ln.strip() for ln in open(logf, encoding="utf-8", errors="replace")
                 if ln.strip() and not re.match(r"(orientation|best|n):", ln.strip()) and "found error, exit" not in ln]
        why = " ".join(lines[-3:])[-300:] or f"exit code {proc.returncode}"
        if proc.returncode < 0 and not lines:
            why = f"the slicer crashed (signal {-proc.returncode})"
        if proc.returncode & 0xFFFFFFFF in (0xFFFFFFE8, 0xFFFFFFEF) and not lines:
            why = ("the model doesn't fit the printer's build volume (or a setting is invalid): "
                   "try Fit the bed, or let the app cut it into parts")
        if proc.returncode & 0xFFFFFFFF == 0xFFFFFFCE:
            why = ("part of the plate lands outside the printable area or in the bed's no-go corner "
                   "(front right, by the purge chute): try a smaller size or Split into parts")
        raise RuntimeError(f"Slicing failed: {why}")
    if ORCA:
        orca_fix(found[0])
    if any(_has_prime_puck(m) for m in models):
        import primefirst
        n = primefirst.postprocess(found[0])
        log.info("prime puck: moved to the front of %d colour changes", n)
    return found[0]


def _rebuild_multicolour(path: str, workdir: str, filaments: list[str]) -> str | None:
    """Re-build a multi-colour project 3MF through combine (see slice_model). None when it doesn't apply
    (one colour, already a combined plate, filament numbers with gaps) or the rebuild itself fails."""
    try:
        if model_filament_count(path) < 2 or _has_prime_puck(path):
            return None
        import combine
        import combine_build as CB
        objs = combine.read(path)
        used = combine.extruders_used(objs)
        if len(used) < 2 or used != list(range(1, len(used) + 1)) or len(used) > len(filaments):
            return None
        pl = CB.plan([{"objects": objs, "slots": used, "copies": 1, "name": ""}])
        if len(pl["plates"]) != 1:
            return None
        cols = combine.file_colours(path)
        colours = [(cols.get(e) or "#808080").upper() for e in used]
        out = os.path.join(workdir, "rebuilt.3mf")
        CB.build_plate(pl["plates"][0], os.path.join(workdir, "rebuild"), out, list(filaments[:len(used)]), colours)
        return out
    except Exception as e:
        log.info("re-building the multi-colour file failed: %s", e)
        return None


def _hms(sec: float) -> str:
    sec = int(round(sec))
    d, h, m, s = sec // 86400, sec // 3600 % 24, sec // 60 % 60, sec % 60
    parts = [f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v]
    return " ".join(parts) or "0s"


def _secs(t: str) -> float:
    parts = {u: float(n) for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", t or "")}
    return parts.get("d", 0) * 86400 + parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)


def orca_fix(path: str) -> dict:
    """Make Orca's CC2 gcode match ElegooSlicer's: wrap each colour-change prime tower section in the
    firmware's SET_CONDITION A=WIPE_TOWER markers, and count the colour changes (the chute purge and
    the printer's filament load time) in the time / grams / progress figures, which Orca leaves out."""
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().split("\n")
    tail = "\n".join(lines[-3000:])
    load_s = float((re.search(r"^; machine_load_filament_time = ([\d.]+)", tail, re.M) or [0, 0])[1])
    dens = float((re.search(r"^; filament_density: ([\d.]+)", "\n".join(lines[:60]), re.M) or [0, 1.24])[1])
    g_per_mm = 3.14159265 * 0.875 ** 2 * dens / 1000

    # 1) SET_CONDITION around the tower sections that do a colour change
    out: list[str] = []
    open_cond, n_wrapped = False, 0
    for i, ln in enumerate(lines):
        if open_cond and (ln.startswith(("; printing object", ";LAYER_CHANGE", ";TYPE:Custom"))
                          or ln == "M106 S0" and ";TYPE:Custom" in lines[i + 1:i + 4]):  # end of print: before the fans go off
            out.append("SET_CONDITION_END A=WIPE_TOWER")
            open_cond = False
        if ln == ";TYPE:Prime tower" and not open_cond and any(
                x.startswith("; CP TOOLCHANGE START") for x in lines[i + 1:i + 6]):
            at = len(out) - 1 if out and out[-1].startswith(";HEIGHT:") else len(out)
            for back in range(1, min(14, len(out)) + 1):  # after the wipe that ends the last move, before the travel
                if out[-back] == ";WIPE_END":
                    at = len(out) - back + 1
                    break
                if out[-back].startswith((";TYPE:", "M6211", ";LAYER_CHANGE")):
                    break
            out.insert(at, "SET_CONDITION A=WIPE_TOWER")
            open_cond, n_wrapped = True, n_wrapped + 1
        out.append(ln)
    if open_cond:
        out.append("SET_CONDITION_END A=WIPE_TOWER")

    # 2) colour changes: load time + chute purge grams (charged to the incoming filament)
    chg = re.compile(r"^M6211 T(\d+) L(\d*\.?\d+)")
    changes = [(k, int(m.group(1)), float(m.group(2))) for k, ln in enumerate(out)
               for m in [chg.match(ln)] if m and float(m.group(2)) > 0]
    extra_s = load_s * len(changes)
    purge_g: dict[int, float] = {}
    for _, t, length in changes:
        purge_g[t] = purge_g.get(t, 0.0) + length * g_per_mm
    total_s = None
    for k, ln in enumerate(out):
        if ln.startswith("; estimated printing time (normal mode) = "):
            total_s = _secs(ln.split("= ", 1)[1]) + extra_s
            out[k] = "; estimated printing time (normal mode) = " + _hms(total_s)
        elif ln.startswith("; total filament used [g] = "):
            out[k] = f"; total filament used [g] = {float(ln.split('= ', 1)[1]) + sum(purge_g.values()):.2f}"
        elif ln.startswith("; filament used [g] = "):
            vals = [float(v) for v in ln.split("= ", 1)[1].split(",")]
            out[k] = "; filament used [g] = " + ", ".join(f"{v + purge_g.get(t, 0):.2f}" for t, v in enumerate(vals))

    # 3) the printer's progress / remaining time: add the colour changes still ahead of each M73
    if total_s and changes:
        idx = [k for k, _, _ in changes]
        m73 = re.compile(r"^M73 P(\d+) R(\d+)")
        j = 0
        for k, ln in enumerate(out):
            m = m73.match(ln)
            if not m:
                continue
            while j < len(idx) and idx[j] < k:
                j += 1
            rem = int(m.group(2)) * 60 + (len(idx) - j) * load_s
            out[k] = f"M73 P{max(0, min(100, round(100 * (1 - rem / total_s))))} R{round(rem / 60)}"
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write("\n".join(out))
    return {"wrapped": n_wrapped, "changes": len(changes), "extra_s": extra_s, "purge_g": round(sum(purge_g.values()), 2)}


def gcode_stats(path: str) -> dict:
    """Print time / filament grams from the slicer's header comments."""
    stats: dict = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.read(200_000)
        f.seek(max(0, os.path.getsize(path) - 60_000))
        tail = f.read()
    for txt in (head, tail):
        m = re.search(r"estimated printing time \(normal mode\) = (.+)", txt)
        if m:
            stats["time"] = m.group(1).strip()
        m = (re.search(r"total filament weight \[g\] : ([\d.]+)", txt) or re.search(r"; total filament used \[g\] = ([\d.]+)", txt)
             or re.search(r"filament used \[g\] = ([\d.]+)", txt))  # multi-colour: the total, not just filament 1
        if m:
            stats["grams"] = float(m.group(1))
        m = re.search(r"^; filament used \[g\] = ([\d., ]+)$", txt, re.M)
        if m:  # per filament (colour order = the job's trays)
            stats["grams_each"] = [float(v) for v in m.group(1).split(",") if v.strip()]
        m = re.search(r"; total layers count = (\d+)", txt) or re.search(r"; total layer number: (\d+)", txt)
        if m:
            stats["layers"] = int(m.group(1))
    return stats
