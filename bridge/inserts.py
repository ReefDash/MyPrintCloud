"""Pauses for inserts: magnets, nuts, bearings or weights sealed inside a print.

find_pockets(mesh)  -> hidden cavities (closed voids) in a model, with the height where each gets its roof
add_pauses(gcode, heights) -> put the CC2's pause command (M600, what ElegooSlicer uses for "pause" on the
                              CC2) at the start of the first layer that would close each pocket.
The server sees the printer stop at one of these layers and sends "time to drop in your insert".
"""
from __future__ import annotations

import re

PAUSE_CMD = "M600"
MARK = "; MyPrintCloud: pause for insert"


def find_pockets(mesh, min_size: float = 1.0, max_size: float = 40.0) -> list[dict]:
    """Closed voids inside the model: separate inner shells whose volume comes out negative (normals face in).
    Heights are measured from the model's bottom (where it sits on the bed). Smallest roof first."""
    import trimesh
    try:
        bodies = mesh.split(only_watertight=False)
    except Exception:
        return []
    z0 = float(mesh.bounds[0][2])
    out = []
    for b in bodies:
        if len(b.faces) < 4 or not b.is_watertight:
            continue
        vol = float(b.volume)
        if vol >= 0:
            continue  # a normal solid, not a void
        lo, hi = b.bounds
        size = [float(hi[i] - lo[i]) for i in range(3)]
        if max(size) > max_size:   # 11/12: big sealed voids are float chambers / hollow bodies, not inserts
            continue
        if min(size) < min_size:
            continue  # sliver / modelling noise, not a pocket you can fill
        out.append({"bottom": round(float(lo[2]) - z0, 2), "top": round(float(hi[2]) - z0, 2),
                    "size": [round(s, 1) for s in size], "volume_mm3": round(-vol, 1)})
    out.sort(key=lambda p: p["top"])
    return out


def describe(p: dict) -> str:
    x, y, z = p["size"]
    fill = p.get("volume_mm3", 0) / max(x * y * z, 1e-6)    # a cylinder fills ~79 % of its box
    shape = "round" if abs(x - y) < 0.3 and fill < 0.9 else "box"
    return f"{shape} pocket {x:g} x {y:g} x {z:g} mm, roof at {p['top']:g} mm"


def _layers(text: str) -> list[tuple[int, float, int]]:
    """(layer number, top Z, char offset just after the line where the printer starts counting that layer)."""
    out = []
    for m in re.finditer(r";LAYER_CHANGE\s*\n;Z:([\d.]+)", text):
        z = float(m.group(1))
        cur = re.compile(r"^SET_PRINT_STATS_INFO [^\n]*CURRENT_LAYER=(\d+)[^\n]*\n", re.M).search(text, m.end())
        if cur:
            out.append((int(cur.group(1)), z, cur.end()))
    return out


def add_pauses(gcode_path: str, heights: list[float], labels: list[str] | None = None) -> list[dict]:
    """Pause before the first layer whose top is above each height (that layer would start the roof).
    Returns [{"height", "layer", "z"}] for the pauses actually placed. Idempotent: earlier marks are removed."""
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    text = re.sub(rf"{re.escape(MARK)}[^\n]*\n(?:M117 [^\n]*\n)?{PAUSE_CMD}\n", "", text)
    layers = _layers(text)
    placed, inserts = [], {}
    for i, h in enumerate(sorted(set(round(float(h), 3) for h in heights if h and h > 0))):
        nxt = next((L for L in layers if L[1] > h + 1e-3), None)
        if not nxt or nxt[0] <= 1:
            continue  # above the top of the print, or the pocket starts at the bed
        if nxt[2] in inserts:
            continue  # two pockets closing on the same layer: one pause does both
        label = (labels[i] if labels and i < len(labels) else f"{h:g} mm").replace("\n", " ")[:60]
        inserts[nxt[2]] = f"{MARK} at {h:g} mm ({label})\nM117 Add insert, then resume\n{PAUSE_CMD}\n"
        placed.append({"height": h, "layer": nxt[0], "z": nxt[1]})
    for pos in sorted(inserts, reverse=True):
        text = text[:pos] + inserts[pos] + text[pos:]
    with open(gcode_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return placed


def planned(gcode_path: str) -> list[int]:
    """Layer numbers that already carry an insert pause (to tell the phone alert what's expected)."""
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return [L for L, _, pos in _layers(text) if text.startswith(MARK, pos)]
