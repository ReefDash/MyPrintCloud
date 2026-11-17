"""Calibration prints for one filament: generated test models, gcode tweaks per band/object, and
the question the app asks afterwards. The server turns the answer into saved values (see
server/app.py calib_answer); every normal job then slices with that filament's saved values.

Tests (each varies one thing; everything else uses the filament's current calibrated values):
  temp   5-block tower, nozzle temperature steps down 5 °C per block (M104 at each block)
  flow   7 squares, extrusion scaled per square (E values inside each object), marked by 1-7 bumps
  pa     hollow square tower, pressure advance rises with height (SET_PRESSURE_ADVANCE each layer)
  retract two pillars, retraction length rises per 5 mm band (retract/unretract moves scaled)
  dims   20 mm cube to measure with calipers -> XY size + elephant foot compensation
"""
from __future__ import annotations

import os
import re

import numpy as np
import trimesh

import slicer

TESTS = {
    "temp": {"title": "Temperature tower", "minutes": 45, "sets": "Nozzle temperature"},
    "flow": {"title": "Flow squares", "minutes": 30, "sets": "Flow ratio"},
    "pa": {"title": "Pressure advance chevrons", "minutes": 12, "sets": "Pressure advance (sharp corners)"},
    "pa_tower": {"title": "Pressure advance tower", "minutes": 20, "sets": "Pressure advance (sharp corners)"},
    "retract": {"title": "Stringing towers", "minutes": 25, "sets": "Retraction length"},
    "dims": {"title": "20 mm cube", "minutes": 15, "sets": "Elephant foot + XY size compensation"},
}
TEMP_BLOCKS, TEMP_STEP, BLOCK_H = 5, 5, 8.0
FLOW_FACTORS = [0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06]
PA_H, PA_Z0, PA_STEP = 40.0, 1.0, 0.002     # PA = (z - Z0) * STEP  -> 0 .. 0.078
# nested "<" chevrons side by side, one PA value each: tip at (x_k, 20), legs to (x_k + 20, 0) / (x_k + 20, 40)
CHEV_N, CHEV_DX, CHEV_W, CHEV_H, CHEV_PA_STEP = 9, 6.0, 1.3, 4.0, 0.01
RET_BANDS, RET_BAND_H, RET_START, RET_STEP = 8, 5.0, 0.2, 0.2


def _box(x, y, z, at=(0, 0, 0)) -> trimesh.Trimesh:
    b = trimesh.creation.box((x, y, z))
    b.apply_translation(np.array(at) - b.bounds[0])
    return b


def _union(parts) -> trimesh.Trimesh:
    try:
        return trimesh.boolean.union(parts, engine="manifold")
    except Exception:
        return trimesh.util.concatenate(parts)


def _save(m: trimesh.Trimesh, path: str) -> str:
    m.apply_translation(-m.bounds[0])
    m.export(path)
    return path


def base_values(preset: str, calib: dict | None, quality: str = "standard") -> dict:
    """The filament's current values: stock preset, then its saved calibration."""
    retract = slicer.filament_value(preset, "filament_retraction_length")
    if not isinstance(retract, float):
        slicer._index()
        retract = float(slicer._resolve(slicer.MACHINE)["retraction_length"][0])
    v = {"nozzle_temperature": slicer.filament_value(preset, "nozzle_temperature", 210.0),
         "filament_flow_ratio": slicer.filament_value(preset, "filament_flow_ratio", 0.98),
         "pressure_advance": slicer.filament_value(preset, "pressure_advance", 0.04),
         "filament_retraction_length": retract,
         "elefant_foot_compensation": slicer.process_value(quality, "elefant_foot_compensation", 0.15),
         "xy_contour_compensation": slicer.process_value(quality, "xy_contour_compensation", 0.0),
         "t_low": slicer.filament_value(preset, "nozzle_temperature_range_low", 190.0),
         "t_high": slicer.filament_value(preset, "nozzle_temperature_range_high", 240.0)}
    c = calib or {}
    for scope in ("filament", "process"):
        for k, val in (c.get(scope) or {}).items():
            try:
                v[k] = float(val)
            except (TypeError, ValueError):
                pass
    return v


def build(test: str, preset: str, calib: dict | None, outdir: str) -> dict:
    """Write the test model(s); returns a plan: models, slicing overrides, gcode tweaks and the question."""
    os.makedirs(outdir, exist_ok=True)
    b = base_values(preset, calib)
    fil = {k: b[k] for k in ("nozzle_temperature", "filament_flow_ratio", "pressure_advance", "filament_retraction_length")}
    fil["nozzle_temperature_initial_layer"] = b["nozzle_temperature"]
    proc = {"elefant_foot_compensation": b["elefant_foot_compensation"], "xy_contour_compensation": b["xy_contour_compensation"],
            "enable_support": "0", "brim_type": "no_brim", "gcode_label_objects": "1", "exclude_object": "0",
            "layer_height": "0.2", "initial_layer_print_height": "0.2"}
    plan = {"test": test, "preset": preset, "title": TESTS[test]["title"], "base": b}

    if test == "temp":
        hi = min(b["t_high"], b["nozzle_temperature"] + 10)
        temps = [hi - TEMP_STEP * i for i in range(TEMP_BLOCKS)]
        temps = [max(b["t_low"], t) for t in temps]
        blocks = []
        for i in range(TEMP_BLOCKS):  # two pillars + a 10 mm bridge per block: shows stringing and bridging
            z = i * BLOCK_H
            blocks += [_box(6, 10, BLOCK_H - 1.6 + 0.01, (0, 0, z)), _box(6, 10, BLOCK_H - 1.6 + 0.01, (16, 0, z)),
                       _box(22, 10, 1.6 + 0.01, (0, 0, z + BLOCK_H - 1.6))]
        blocks.append(_box(30, 16, 1.0, (-4, -3, -0.99)))  # foot so it can't topple
        plan["models"] = [_save(_union(blocks), os.path.join(outdir, "temp_tower.stl"))]
        fil["nozzle_temperature"] = fil["nozzle_temperature_initial_layer"] = temps[0]
        plan["bands"] = {"kind": "temp", "z0": 1.0, "h": BLOCK_H, "values": temps}
        plan["question"] = {"kind": "pick", "key": "nozzle_temperature", "scope": "filament",
                            "prompt": "Which block looks best? Count from the bottom. Look for the cleanest bridge, "
                                      "least stringing between the pillars and smooth, shiny-but-not-melty walls.",
                            "options": [{"label": f"Block {i + 1} (from bottom) · {t:g} °C", "value": t} for i, t in enumerate(temps)]}

    elif test == "flow":
        plan["models"] = []
        for i, k in enumerate(FLOW_FACTORS):  # i+1 bumps on the front edge identify each square
            parts = [_box(28, 28, 3.0)] + [_box(2.4, 2.4, 1.6, (2 + j * 3.6, -2.39, 0)) for j in range(i + 1)]
            plan["models"].append(_save(_union(parts), os.path.join(outdir, f"flow_{i + 1}.stl")))
        proc.update({"top_shell_layers": "4", "bottom_shell_layers": "3", "sparse_infill_density": "15%",
                     "top_surface_pattern": "monotonic", "ironing_type": "no ironing"})
        plan["objects"] = {f"flow_{i + 1}.stl": k for i, k in enumerate(FLOW_FACTORS)}
        plan["question"] = {"kind": "pick", "key": "filament_flow_ratio", "scope": "filament",
                            "prompt": "Count the bumps on each square's edge. Which top surface is smoothest: no gaps "
                                      "between lines, but no ridges or rough bumps either? Run a fingertip over them.",
                            "options": [{"label": f"{i + 1} bump{'s' if i else ''} · flow {b['filament_flow_ratio'] * k:.3f}",
                                         "value": round(b["filament_flow_ratio"] * k, 4)} for i, k in enumerate(FLOW_FACTORS)]}

    elif test == "pa":
        from shapely.geometry import LineString
        pas = [round(CHEV_PA_STEP * k, 4) for k in range(CHEV_N)]  # 0 .. 0.08 covers PLA, PETG and wood
        bodies, cents = [], []
        for k in range(CHEV_N):
            x = k * CHEV_DX
            line = [(x + 20, 0), (x, 20), (x + 20, 40)]
            poly = LineString(line).buffer(CHEV_W / 2, cap_style=2, join_style=2, mitre_limit=4)
            bodies.append(trimesh.creation.extrude_polygon(poly, CHEV_H))
            cents.append(line)
        # a numbered-by-size marker under chevron 1's leg end so "the left one" is never in doubt
        bodies.append(_box(3, 3, 1.2, (18, -6, 0)))
        m = trimesh.util.concatenate(bodies)
        lo = m.bounds[0].copy()
        path = _save(m, os.path.join(outdir, "pa_chevrons.stl"))
        m.apply_translation([128 - m.extents[0] / 2, 128 - m.extents[1] / 2, 0])  # placed by us: the slicer's
        m.export(path)                                                             # arrange would rotate it
        plan["models"], plan["arrange"] = [path], False
        proc.update({"wall_loops": "3", "sparse_infill_density": "0%", "top_shell_layers": "0", "bottom_shell_layers": "0",
                     "outer_wall_speed": "200", "inner_wall_speed": "200", "seam_position": "back",
                     "detect_thin_wall": "0", "only_one_wall_first_layer": "0"})
        plan["bands"] = {"kind": "pa_islands", "lines": [[[px - lo[0], py - lo[1]] for px, py in ln] for ln in cents],
                         "values": pas}
        plan["question"] = {"kind": "pick", "key": "pressure_advance", "scope": "filament",
                            "prompt": "Look down at the chevrons (the tips point left; the small square is under "
                                      "chevron 1). Pick the one with the sharpest tip and legs the same width all the "
                                      "way in: too little makes a blobby, rounded tip; too much leaves a thin or gappy tip.",
                            "options": [{"label": f"Chevron {k + 1} from the left · PA {v:g}", "value": v}
                                        for k, v in enumerate(pas)]}

    elif test == "pa_tower":
        outer = _box(50, 50, PA_H)
        inner = _box(50 - 1.7, 50 - 1.7, PA_H, (0.85, 0.85, 0.6))
        try:
            tower = trimesh.boolean.difference([outer, inner], engine="manifold")
        except Exception:
            tower = outer
        plan["models"] = [_save(tower, os.path.join(outdir, "pa_tower.stl"))]
        proc.update({"wall_loops": "2", "sparse_infill_density": "0%", "top_shell_layers": "0", "bottom_shell_layers": "3",
                     "outer_wall_speed": "200", "inner_wall_speed": "200", "seam_position": "back"})
        plan["bands"] = {"kind": "pa", "z0": PA_Z0, "step": PA_STEP}
        plan["question"] = {"kind": "height", "key": "pressure_advance", "scope": "filament", "max": PA_H,
                            "z0": PA_Z0, "step": PA_STEP,
                            "prompt": "Look at the corners (best on the front ones). Low on the tower they bulge, high up "
                                      "they get rounded or gappy. Measure the height (mm, from the bed side) where "
                                      "the corners look sharpest and the walls are even."}

    elif test == "retract":
        pillars = _union([_box(8, 8, RET_BANDS * RET_BAND_H), _box(8, 8, RET_BANDS * RET_BAND_H, (60, 0, 0)),
                          _box(68, 8, 0.6)])
        plan["models"] = [_save(pillars, os.path.join(outdir, "stringing.stl"))]
        vals = [round(RET_START + RET_STEP * i, 2) for i in range(RET_BANDS)]
        fil["filament_retraction_length"] = vals[0]
        plan["bands"] = {"kind": "retract", "z0": 0.6, "h": RET_BAND_H, "values": vals,
                         "base": vals[0]}
        plan["question"] = {"kind": "pick", "key": "filament_retraction_length", "scope": "filament",
                            "prompt": "Each 5 mm band of the towers uses more retraction (bottom = least). Which is the "
                                      "lowest band with no strings between the towers? Pick the one above it if it's borderline.",
                            "options": [{"label": f"Band {i + 1} (from bottom) · {v:g} mm", "value": v} for i, v in enumerate(vals)]}

    elif test == "dims":
        plan["models"] = [_save(_box(20, 20, 20), os.path.join(outdir, "cube20.stl"))]
        proc.update({"wall_loops": "2", "sparse_infill_density": "15%"})
        plan["question"] = {"kind": "dims", "prompt": "Measure with calipers. X and Y across the middle of the cube, "
                                                      "then X and Y right at the bottom edge (the first layer).",
                            "nominal": 20.0}
    else:
        raise ValueError(f"unknown test {test}")

    plan["filament"], plan["process"] = fil, proc
    return plan


_E = re.compile(r"E(-?\d*\.?\d+)")


def postprocess(gcode_path: str, plan: dict) -> None:
    """Apply the per-band / per-object changes the slicer can't do by itself."""
    bands, objects = plan.get("bands"), plan.get("objects")
    if bands and bands["kind"] == "pa_islands":
        return _pa_islands(gcode_path, bands)
    out = []
    obj_k = None
    z, cur_band, started = 0.0, None, False
    ret_k = 1.0
    with open(gcode_path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().split("\n")
    for ln in lines:
        if ln.startswith("EXCLUDE_OBJECT"):
            continue  # don't rely on the firmware knowing these
        if ln.startswith(";LAYER_CHANGE"):
            started = True
        if started and (ln.startswith(";TYPE:Custom") or ln.startswith("; filament end gcode")
                        or ln.startswith("; EXECUTABLE_BLOCK_END")):
            started, bands, objects, obj_k, ret_k = False, None, None, None, 1.0  # end gcode: leave untouched
        if ln.startswith(";Z:") and started and bands:
            out.append(ln)
            try:
                z = float(ln[3:])
            except ValueError:
                continue
            kind = bands["kind"]
            if kind == "pa":
                out.append(f"SET_PRESSURE_ADVANCE ADVANCE={max(0.0, (z - bands['z0']) * bands['step']):.4f} ; MPC calib")
                continue
            i = max(0, min(len(bands["values"]) - 1, int((z - bands["z0"]) // bands["h"]))) if z > bands["z0"] else 0
            if i != cur_band:
                cur_band = i
                v = bands["values"][i]
                if kind == "temp":
                    out.append(f"M104 S{v:g} ; MPC calib block {i + 1}")
                elif kind == "retract":
                    ret_k = v / bands["base"]
                    out.append(f"; MPC calib retraction band {i + 1}: {v:g} mm")
            continue
        if objects is not None:
            if ln.startswith("; printing object "):
                name = ln[len("; printing object "):].split(" id:")[0].strip()
                obj_k = objects.get(name)
            elif ln.startswith("; stop printing object"):
                obj_k = None
            elif obj_k and ln.startswith("G1 ") and (" X" in ln or " Y" in ln):
                m = _E.search(ln.split(";")[0])
                if m and float(m.group(1)) > 0:
                    ln = ln[:m.start(1)] + f"{float(m.group(1)) * obj_k:.5f}" + ln[m.end(1):]
        if bands and bands["kind"] == "retract" and ret_k != 1.0 and ln.startswith("G1 "):
            code = ln.split(";")[0]
            m = _E.search(code)
            if m:
                e = float(m.group(1))
                pure = " X" not in code and " Y" not in code
                # retract / wipe-retract / unretract only (never purges): at most one base retraction long
                if abs(e) <= bands["base"] * 1.01 and (e < 0 or (pure and e > 0)):
                    ln = ln[:m.start(1)] + f"{e * ret_k:.5f}" + ln[m.end(1):]
        out.append(ln)
    with open(gcode_path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write("\n".join(out))


def _seg_dist(p, a, b) -> float:
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _pa_islands(gcode_path: str, bands: dict) -> None:
    """Chevron test: set PA whenever the nozzle starts extruding on a different chevron."""
    with open(gcode_path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().split("\n")
    xy = re.compile(r"\b([XY])(-?\d*\.?\d+)")

    def body():  # (index, x, y) of extruding moves in the printed layers
        started, x, y = False, 0.0, 0.0
        for i, ln in enumerate(lines):
            if ln.startswith(";LAYER_CHANGE"):
                started = True
            elif started and (ln.startswith(";TYPE:Custom") or ln.startswith("; EXECUTABLE_BLOCK_END")):
                return
            if ln.startswith("G1 ") or ln.startswith("G0 "):
                code = ln.split(";")[0]
                for k, v in xy.findall(code):
                    x, y = (float(v), y) if k == "X" else (x, float(v))
                m = _E.search(code)
                if started and m and float(m.group(1)) > 0 and ("X" in code or "Y" in code):
                    yield i, x, y
    pts = list(body())
    if not pts:
        return
    ox, oy = min(p[1] for p in pts), min(p[2] for p in pts)  # model min corner on the bed
    insert, cur = {}, None
    for i, x, y in pts:
        q = (x - ox, y - oy)  # bed -> model coordinates (within half a line width)
        k = min(range(len(bands["lines"])), key=lambda c: min(_seg_dist(q, bands["lines"][c][0], bands["lines"][c][1]),
                                                               _seg_dist(q, bands["lines"][c][1], bands["lines"][c][2])))
        if k != cur:
            insert[i] = f"SET_PRESSURE_ADVANCE ADVANCE={bands['values'][k]:.4f} ; MPC calib chevron {k + 1}"
            cur = k
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith("EXCLUDE_OBJECT"):
            continue
        if i in insert:
            out.append(insert[i])
        out.append(ln)
    with open(gcode_path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write("\n".join(out))
