"""Colour paint on 3MF triangles (Orca/Bambu `paint_color`, PrusaSlicer `mmu_segmentation`).

Each painted triangle carries a hex string: a bit stream (read from the END of the string, one hex
digit = one 4-bit nibble, bit 0 first) describing how the triangle is split and the state of each
leaf. Format per node (from OrcaSlicer TriangleSelector::serialize):
    nibble: bits 0-1 = number of split sides (0 = leaf), bits 2-3 = special side / leaf state
    leaf state 0..2 : stored in bits 2-3
    leaf state >= 3 : bits 2-3 = 0b11, then a nibble (state-3) for 3..17, or 0xF + nibble (state-18)
    split node      : followed by its (sides+1) children
State 0 = the part's own filament, 1..N = filament number. remap() renumbers states for a plate
whose filament list differs from the model's own.
"""
from __future__ import annotations


def _nibbles(code: str) -> list[int]:
    return [int(c, 16) for c in reversed(code.strip())]


def _hex(nibbles: list[int]) -> str:
    return "".join("0123456789ABCDEF"[n] for n in reversed(nibbles))


def _walk(nib: list[int], i: int, fn, out: list[int]) -> int:
    """Copy one node (recursively) from nib[i:] to out, passing every leaf state through fn."""
    code = nib[i]
    i += 1
    sides = code & 0b11
    if sides:
        out.append(code)
        for _ in range(sides + 1):
            i = _walk(nib, i, fn, out)
        return i
    if (code & 0b1100) == 0b1100:
        n = nib[i]
        i += 1
        if n == 0b1111:
            state = nib[i] + 18
            i += 1
        else:
            state = n + 3
    else:
        state = code >> 2
    state = fn(state)
    if state >= 3:
        out.append(0b1100)
        if state <= 17:
            out.append(state - 3)
        else:
            out += [0b1111, state - 18]
    else:
        out.append(state << 2)
    return i


def _apply(code: str, fn) -> str:
    if not code:
        return code
    nib = _nibbles(code)
    out: list[int] = []
    i = 0
    while i < len(nib):   # normally one root node per triangle; tolerate padding
        i = _walk(nib, i, fn, out)
    return _hex(out)


def states(codes) -> set[int]:
    seen: set[int] = set()

    def note(s):
        seen.add(s)
        return s
    for c in codes:
        if c:
            _apply(c, note)
    return seen


def remap(codes, mapping: dict[int, int]):
    """New codes with every painted state s > 0 replaced by mapping.get(s, s)."""
    fn = lambda s: mapping.get(s, s) if s else 0
    return [_apply(c, fn) if c else c for c in codes]


if __name__ == "__main__":  # self-test: round trips and renumbering
    samples = ["4", "8", "0C", "1C", "3", "48C3", "C4483", "FFC"[1:] + "0C"]
    for s in samples:
        try:
            assert _apply(s, lambda x: x) == s.upper(), s
        except (AssertionError, IndexError) as e:
            print("sample", s, "->", e)
    print(states(["4", "8", "0C"]), remap(["4", "8"], {1: 2, 2: 4}), states(remap(["4", "8"], {1: 2, 2: 4})))
    print(remap(["0C"], {3: 1}), states(["1FC"]), remap(["1FC"], {19: 2}))
