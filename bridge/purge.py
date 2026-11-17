"""Purge objects: catch colour-change waste in something worth keeping instead of the chute.

On the CC2 the colour change is one firmware command, `M6211 T<tool> L<flush mm> ...`, which squirts
L mm of filament out of the chute ("poop"), and the slicer's own "flush into object" option does
nothing for it. So we do it ourselves on the sliced gcode:

  * after each colour change, the purge object's block for that layer is moved to print first,
    with the new filament: the mixed, half-old-colour plastic ends up in the purge object;
  * that change's L is reduced by what the block extrudes (never below MIN_L), so the total
    flushed before the real model resumes is the same as before: the model's colours stay clean.

A layer with several colour changes uses one purge object per change (purge1, purge2, ...).
Retract/prime moves are re-balanced after moving blocks so the extruder state stays exact.
"""
from __future__ import annotations

import math
import os
import re
import zipfile

MIN_L = 30.0            # mm still purged through the chute at every change (cleans the worst of it)
_E = re.compile(r"E(-?\d*\.?\d+)")
_CHANGE = re.compile(r"^M6211 T(\d+) L(\d*\.?\d+)")
_START = re.compile(r"^; printing object (\S+)")


def _e(line: str) -> float | None:
    code = line.split(";", 1)[0]
    if not code.startswith("G1 ") and not code.startswith("G0 "):
        return None
    m = _E.search(code)
    return float(m.group(1)) if m else None


def _pure(line: str) -> bool:
    code = line.split(";", 1)[0]
    return " X" not in code and " Y" not in code and " Z" not in code


GRAMS_PER_MM = math.pi * 1.75 ** 2 / 4 / 1000 * 1.24   # 1.75 mm PLA


def mark_objects(project: str, names: list[str], settings: dict) -> None:
    """Give the purge objects in a slicer-written project 3MF their own settings (e.g. 100 % infill)."""
    tmp = project + ".tmp"
    with zipfile.ZipFile(project) as zi, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in zi.namelist():
            d = zi.read(n)
            if n == "Metadata/model_settings.config":
                def one(m):
                    block = m.group(0)
                    nm = re.search(r'key="name" value="([^"]+)"', block)
                    if not nm or nm.group(1) not in names:
                        return block
                    extra = "".join(f'    <metadata key="{k}" value="{v}"/>\n' for k, v in settings.items())
                    return block.replace("\n    <part", "\n" + extra + "    <part", 1)
                d = re.sub(r'<object id="\d+">.*?</object>', one, d.decode("utf-8"), flags=re.S).encode("utf-8")
            zo.writestr(n, d)
    os.replace(tmp, project)


def chute_mm(gcode_path: str) -> float:
    """Filament (mm) the colour changes still send out of the chute."""
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        return sum(float(m.group(2)) for m in (_CHANGE.match(ln) for ln in f) if m)


def demand(gcode_path: str) -> dict:
    """Per-layer colour changes and flush lengths of a normally sliced multi-colour gcode."""
    layers, z, changes, zs = [], 0.0, [], []
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if ln.startswith(";Z:"):
                z = float(ln[3:])
                zs.append(z)
                layers.append([z, []])
            m = _CHANGE.match(ln)
            if m and layers:
                layers[-1][1].append(float(m.group(2)))
                changes.append((z, float(m.group(2))))
    per_layer = [(lz, ls) for lz, ls in layers if ls]
    return {"changes": len(changes), "flush_mm": sum(c[1] for c in changes),
            "max_per_layer": max((len(ls) for _, ls in per_layer), default=0),
            "last_z": max((lz for lz, _ in per_layer), default=0.0), "layers": per_layer, "zs": zs}


def postprocess(gcode_path: str, names: list[str], min_l: float = MIN_L) -> dict:
    """Move purge-object blocks behind colour changes and shorten the chute flush. Returns stats."""
    with open(gcode_path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().split("\n")
    names = list(names)

    # ---- split into layers; inside a layer find object blocks [start, end) and colour changes
    layer_starts = [i for i, ln in enumerate(lines) if ln.startswith(";LAYER_CHANGE")]
    bounds = list(zip(layer_starts, layer_starts[1:] + [len(lines)]))
    out = lines[:layer_starts[0]] if layer_starts else lines[:]
    absorbed = 0.0
    saved_l, n_moved = 0.0, 0
    for a, b in bounds:
        seg = lines[a:b]
        # object blocks: from "; printing object X" to the line before the next block / layer end
        starts = [i for i, ln in enumerate(seg) if _START.match(ln)]
        blocks = {}
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(seg)
            # stop before a colour change that follows the block
            for j in range(s, e):
                if seg[j].startswith("; filament end gcode") or seg[j].startswith(";==========CC2_CHANGE"):
                    e = j
                    break
            name = _START.match(seg[s]).group(1)
            blocks.setdefault(name, []).append((s, e))  # an object can come in several blocks per layer
        changes = [i for i, ln in enumerate(seg) if _CHANGE.match(ln)]
        if not changes or not any(n in blocks for n in names):
            out += seg
            continue
        # pair each change with a purge object present on this layer
        plan = []  # (change index, insertion index, block range)
        free = [n for n in names if n in blocks]
        for ci in changes:
            if not free:
                break
            ins = next((i for i in range(ci, len(seg)) if _START.match(seg[i])), None)
            if ins is None:
                continue
            plan.append((ci, ins, blocks[free.pop(0)]))
        new: list[str] = []
        cut = set()
        for _, _, ranges in plan:
            for s, e in ranges:
                cut.update(range(s, e))
        inserts = {ins: (ci, r) for ci, ins, r in plan}
        for i, ln in enumerate(seg):
            if i in inserts:
                ci, ranges = inserts[i]
                block = [x for s, e in ranges for x in seg[s:e]]
                vol = sum(v for v in (_e(x) for x in block if not _pure(x)) if v and v > 0)
                m = _CHANGE.match(seg[ci])
                old_l = float(m.group(2))
                new_l = max(min_l, old_l - vol)
                # rewrite the change command in the output (it was emitted earlier in this layer)
                for k in range(len(new) - 1, -1, -1):
                    if new[k] == seg[ci]:
                        new[k] = re.sub(r" L\d*\.?\d+", f" L{new_l:.2f}", seg[ci], count=1) + f" ; MPC purge object takes {old_l - new_l:.1f} mm"
                        break
                saved_l += old_l - new_l
                absorbed += vol
                n_moved += 1
                new.append(f"; MPC purge object block moved here (after colour change)")
                new += block
                new.append(f"G1 Z{_layer_z(seg) + 0.6:.3f} F1200 ; MPC lift before travelling back to the model")
            if i in cut:
                continue
            new.append(ln)
        out += new
    out = _rebalance(out)
    with open(gcode_path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write("\n".join(out))
    return {"moved_blocks": n_moved, "chute_saved_mm": round(saved_l, 1), "absorbed_mm": round(absorbed, 1)}


def _layer_z(seg: list[str]) -> float:
    for ln in seg:
        if ln.startswith(";Z:"):
            return float(ln[3:])
    return 0.0


def _rebalance(lines: list[str]) -> list[str]:
    """After moving blocks, make every block's first prime exactly undo the retract before it.

    Tracks how far the filament is retracted (pure negative E moves and negative wipe moves add,
    pure positive moves subtract); the first pure positive E move after a "; printing object" line
    is rewritten to exactly the current retraction.
    """
    out, state, pending = [], 0.0, False
    for ln in lines:
        if _START.match(ln):
            pending = True
        v = _e(ln)
        if v is not None:
            if v < 0:
                state += -v
            elif _pure(ln) and v > 0:
                if pending:  # a block's first prime undoes exactly the retraction in front of it
                    want = state
                    if abs(want - v) > 1e-4:
                        ln = (_E.sub(f"E{want:.5f}", ln, count=1) + " ; MPC re-balanced prime") if want > 1e-4                             else "; MPC prime dropped (nothing retracted here)"
                    v = want
                    pending = False
                state = max(0.0, state - v)
        out.append(ln)
    return out
