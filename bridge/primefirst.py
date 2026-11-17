"""Print the throwaway "prime puck" first after every colour change.

The CC2 profile has no working prime tower (the slicer drops it), so after each swap the nozzle goes
straight from the purge chute to the nearest object and that object gets the weak, under-pressured first
millimetres of the new colour (seen on the Kanao coins: one coin's inlay thin and speckled, the other
solid). With a puck object in the file, this moves the puck's block to the front of every swap.

Moving a block changes where the nozzle comes from, so every travel near a moved block is rebuilt in a
fixed, safe form: retract in place, lift by the z-hop, travel, drop, unretract (wipes become in-place
retracts, spiral lifts become straight lifts). E totals are unchanged. Everything else is left alone.
"""
from __future__ import annotations

import re

MARK = re.compile(r"^; printing object (.*)$")
STOP = re.compile(r"^; stop printing object ")
UNRET = re.compile(r"^G1 E(\d*\.?\d+) F(\d+(?:\.\d+)?)\s*$")
TOOL = re.compile(r"^T\d+\s*$")
G1 = re.compile(r"^G[01] ")
XYW = re.compile(r"\s[XY]-?\d*\.?\d+")
ZW = re.compile(r"\sZ(-?\d*\.?\d+)")
EW = re.compile(r"\sE(-?\d*\.?\d+)")
XYV = re.compile(r"X(-?\d*\.?\d+)\s+Y(-?\d*\.?\d+)")
HOP = 0.4


def _has_xy(ln: str) -> bool:
    return bool(G1.match(ln) and XYW.search(" " + ln[3:]))


def _travel_window(lines: list[str], a: int, b: int) -> dict | None:
    """Look at lines[a:b] (a travel between prints, ending with the unretract at b): the final travel
    target, the drop height, the retract total and the unretract line."""
    m = UNRET.match(lines[b]) if b < len(lines) else None
    if not m:
        return None
    target, drop, neg, keep = None, None, 0.0, []
    for ln in lines[a:b]:
        if G1.match(ln):
            e = EW.search(ln)
            if e and float(e.group(1)) < 0:
                neg += float(e.group(1))
            if _has_xy(ln) and not (e and float(e.group(1)) < 0):
                xy = XYV.search(ln)
                if xy:
                    target = (xy.group(1), xy.group(2))
            z = ZW.search(ln)
            if z and not _has_xy(ln):
                drop = float(z.group(1))
        elif ln.strip() not in (";WIPE_START", ";WIPE_END"):
            keep.append(ln)  # comments, M73 progress, fan, etc.
    if target is None or drop is None:
        return None
    return {"target": target, "drop": drop, "neg": neg, "u": m.group(1), "uf": m.group(2), "keep": keep}


def _canon(w: dict, lifted: bool) -> list[str]:
    out = list(w["keep"])
    if w["neg"] < 0:
        out.append(f"G1 E{w['neg']:.5f} F1800")
    if not lifted:
        out.append(f"G1 Z{w['drop'] + HOP:.3f}")
    out.append(f"G1 X{w['target'][0]} Y{w['target'][1]} F30000")
    out.append(f"G1 Z{w['drop']:.3f}")
    out.append(f"G1 E{w['u']} F{w['uf']}")
    return out


def _blocks(lines: list[str], a: int, b: int) -> list[tuple[int, int]]:
    """[start, stop] line indexes of the object blocks inside lines[a:b] (they follow each other)."""
    res, i = [], a
    while i < b:
        if MARK.match(lines[i]):
            j = i + 1
            while j < b and not STOP.match(lines[j]):
                j += 1
            if j >= b:
                break
            res.append((i, j))
            i = j + 1
        else:
            i += 1
    return res


def _next_unret(lines: list[str], i: int) -> int:
    while i < len(lines) and not UNRET.match(lines[i]):
        i += 1
    return i


def postprocess(path: str, key: str = "prime puck") -> int:
    """Rewrite the gcode in place; returns how many colour changes were reordered."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    moved = 0
    i = 0
    while i < len(lines):
        if not TOOL.match(lines[i]):
            i += 1
            continue
        t = i
        e = t + 1
        while e < len(lines) and not (lines[e].startswith(";LAYER_CHANGE") or lines[e].startswith("M6211") or TOOL.match(lines[e])):
            e += 1
        blocks = _blocks(lines, t + 1, e)
        p = next((k for k, (s, _) in enumerate(blocks) if key in lines[s]), None)
        if not p or len(blocks) < 2:  # None (no puck this swap) or already first
            i = e
            continue
        parts = []
        for s, stop in blocks:
            u = _next_unret(lines, s + 1)
            w = _travel_window(lines, s + 1, u) if u < stop else None
            if w is None:
                parts = None
                break
            parts.append({"mark": lines[s], "w": w, "core": lines[u + 1:stop], "stop": lines[stop]})
        if parts is None:
            i = e
            continue
        old_last = parts[-1]["mark"]
        first_w = parts[0]["w"]  # the post-swap state: no retract, toolchange unretract, nozzle up high
        order = [parts[p]] + parts[:p] + parts[p + 1:]
        # E bookkeeping: the new first block takes the old first block's lead amounts; the old first
        # block takes the puck's (retract + normal unretract). Sums stay identical.
        amounts = [(x["w"]["neg"], x["w"]["u"], x["w"]["uf"]) for x in parts]
        new_amounts = [amounts[0], amounts[p]] + [amounts[k] for k in range(1, len(parts)) if k != p]
        new = []
        for k, (blk, (neg, u, uf)) in enumerate(zip(order, new_amounts)):
            w = dict(blk["w"], neg=neg, u=u, uf=uf)
            if k == 0:
                w["keep"] = first_w["keep"]
            new.append(blk["mark"])
            new += _canon(w, lifted=(k == 0))
            new += blk["core"]
            new.append(blk["stop"])
        start, end = blocks[0][0], blocks[-1][1] + 1
        lines[start:end] = new
        e += len(new) - (end - start)
        if order[-1]["mark"] != old_last:
            # the travel after this region assumed the old last block's end point: rebuild it in place
            u = _next_unret(lines, e)
            if u < len(lines):
                last_xy = max((k for k in range(e, u) if _has_xy(lines[k]) and not
                               (EW.search(lines[k]) and float(EW.search(lines[k]).group(1)) < 0)), default=None)
                for k in range(e, u):
                    if k != last_xy and _has_xy(lines[k]):
                        stripped = XYW.sub("", " " + lines[k][3:]).strip()
                        lines[k] = (lines[k][:3] + stripped) if stripped and stripped != "F30000" else ";(travel removed by primefirst)"
        moved += 1
        i = e
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return moved
