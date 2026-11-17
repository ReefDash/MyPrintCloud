"""Catalogue of purge objects. Each design is generated for one print: thick enough on every layer
to soak up that layer's colour-change purge, as tall as the last colour change, no supports
(overhangs held to 45 degrees). Purge objects print at 100 % infill, so a layer of area A at
layer height h holds about A * h * FILL mm^3 of purge.

Every design takes the needed "round equivalent" radius profile r(z) (area pi*r^2 per layer) and
returns a mesh whose cross-section is at least that big at every height. check() verifies it.
"""
from __future__ import annotations

import math

import numpy as np
import trimesh
import trimesh.boolean as tb

FIL_AREA = math.pi * 1.75 ** 2 / 4    # mm^2 of 1.75 mm filament
FILL = 0.95                           # share of a solid layer that is actually plastic
DZ = 0.5                              # profile resolution, mm
R_MIN = 5.0                           # keep thin sections sturdy


def needs(dem: dict, slots: int, catch: float, min_l: float) -> list[list[tuple[float, float]]]:
    """Per purge slot: [(z, area mm^2 needed)] from purge.demand(); the k-th change in a layer -> slot k."""
    zs = dem.get("zs") or []
    lh = {z: (z - p) for p, z in zip([0.0] + zs[:-1], zs)}
    out = [[] for _ in range(slots)]
    for z, ls in dem["layers"]:
        h = max(0.08, lh.get(z, 0.2))
        for k, L in enumerate(ls[:slots]):
            vol = max(0.0, L - min_l) * catch * FIL_AREA
            out[k].append((z, vol / (h * FILL)))
    return out


def radius_profile(need: list[tuple[float, float]], r_min: float = R_MIN) -> tuple[np.ndarray, np.ndarray]:
    """Radius of a solid round section with the needed area at each z, widened lower down so it never
    grows faster than 45 degrees going up (printable without supports)."""
    top = max(z for z, _ in need) + 1.0
    zs = np.arange(0.0, top + DZ, DZ)
    req = np.full(len(zs), r_min)
    for z, a in need:  # cover the samples on both sides, so the mesh doesn't thin out between them
        for i in (int(z / DZ), int(math.ceil(z / DZ)), int(math.ceil(z / DZ)) + 1):
            i = min(len(zs) - 1, i)
            req[i] = max(req[i], math.sqrt(a / math.pi))
    r = req.copy()
    for i in range(len(zs) - 2, -1, -1):          # 45 deg rule: r[i] >= r[i+1] - dz
        r[i] = max(r[i], r[i + 1] - DZ)
    return zs, r


# ---------------------------------------------------------------- geometry helpers
def _revolve(profile, sections: int) -> trimesh.Trimesh:
    m = trimesh.creation.revolve(np.array(profile, float), sections=sections)
    m.merge_vertices()
    m.fix_normals()
    return m


def _poly_k(n: int) -> float:
    """Radius factor so an n-sided revolve (corner radius) has the area of the circle it stands in for."""
    return math.sqrt(math.pi / (n / 2 * math.sin(2 * math.pi / n)))


def _unit(pts: np.ndarray) -> tuple[np.ndarray, float]:
    """Scale a closed loop (centred, star-shaped around 0) to area 1; returns (loop, max vertex radius)."""
    x, y = pts[:, 0], pts[:, 1]
    a = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    pts = pts / math.sqrt(a)
    if 0.5 * (np.dot(pts[:, 0], np.roll(pts[:, 1], -1)) - np.dot(pts[:, 1], np.roll(pts[:, 0], -1))) < 0:
        pts = pts[::-1]
    return pts, float(np.max(np.hypot(pts[:, 0], pts[:, 1])))


def _ngon(n: int, rot: float = 0.0, seg: int = 1) -> np.ndarray:
    a = np.arange(n * seg) * 2 * math.pi / (n * seg) + rot
    if seg == 1:
        return np.c_[np.cos(a), np.sin(a)]
    # subdivided polygon (edges straight, more vertices for twisting)
    corners = np.c_[np.cos(np.arange(n) * 2 * math.pi / n + rot), np.sin(np.arange(n) * 2 * math.pi / n + rot)]
    out = []
    for i in range(n):
        p, q = corners[i], corners[(i + 1) % n]
        for t in np.arange(seg) / seg:
            out.append(p + (q - p) * t)
    return np.array(out)


def _star(n: int, ratio: float) -> np.ndarray:
    a = np.arange(2 * n) * math.pi / n
    rad = np.where(np.arange(2 * n) % 2 == 0, 1.0, ratio)
    return np.c_[rad * np.cos(a), rad * np.sin(a)]


def _gear(teeth: int, depth: float = 0.12) -> np.ndarray:
    a = np.arange(teeth * 8) * 2 * math.pi / (teeth * 8)
    phase = (np.arange(teeth * 8) % 8)
    rad = 1.0 - depth * np.isin(phase, [5, 6, 7, 0]).astype(float)
    return np.c_[rad * np.cos(a), rad * np.sin(a)]


def _loft(zs: np.ndarray, loops: list[np.ndarray]) -> trimesh.Trimesh:
    """Solid through closed loops (same vertex count, star-shaped around the z axis) at heights zs."""
    n, m = len(loops[0]), len(zs)
    V = np.vstack([np.c_[lp, np.full(n, z)] for z, lp in zip(zs, loops)] +
                  [[np.r_[loops[0].mean(0), zs[0]]], [np.r_[loops[-1].mean(0), zs[-1]]]])
    F = []
    for i in range(m - 1):
        for j in range(n):
            a, b = i * n + j, i * n + (j + 1) % n
            c, d = (i + 1) * n + (j + 1) % n, (i + 1) * n + j
            F += [(a, b, c), (a, c, d)]
    bc, tc = m * n, m * n + 1
    for j in range(n):
        F.append((bc, (j + 1) % n, j))
        F.append((tc, (m - 1) * n + j, (m - 1) * n + (j + 1) % n))
    mesh = trimesh.Trimesh(V, np.array(F), process=True)
    mesh.fix_normals()
    return mesh


def _scale_profile(r: np.ndarray, rmax_unit: float, extra: float = 0.0) -> np.ndarray:
    """Area-equivalent scale for a unit-area loop, with the 45 degree rule applied to its outermost point."""
    s = np.sqrt(math.pi) * r + extra
    for i in range(len(s) - 2, -1, -1):
        s[i] = max(s[i], s[i + 1] - DZ / rmax_unit)
    return s


def _taper_top(zs, s, rmax_unit, height):
    """Rings that close the top to a near-point (shrinking is always printable)."""
    k = max(3, int(height / DZ))
    z0 = zs[-1]
    return [z0 + height * (i + 1) / k for i in range(k)], [s[-1] * max(0.06, 1 - (i + 1) / k) for i in range(k)]


def _union(parts):
    return tb.union(parts, engine="manifold") if len(parts) > 1 else parts[0]


def _box(x, y, z, at=(0, 0, 0)):
    b = trimesh.creation.box((x, y, z))
    b.apply_translation(np.array(at, float) - b.bounds[0])
    return b


# ================================================================ designs
# ---- statues / decor
def totem(zs, r):
    """Faceted column with raised bands every 12 mm and a pointed cap."""
    band = np.clip(1.5 - np.abs((zs % 12.0) - 6.0), 0, None)   # 45 deg triangular bands, 1.5 mm proud
    rr = r * _poly_k(10) + band + 1.0
    top = zs[-1]
    prof = [(0.0, 0.0)] + list(zip(rr, zs)) + [(0.0, top + rr[-1] * 0.8)]
    return _revolve(prof, sections=10)


def twist(zs, r):
    """Six-pointed star column that twists as it rises, ending in a point."""
    loop, rm = _unit(_star(6, 0.62))
    s = _scale_profile(r, rm, 0.5)
    rate = min(math.pi / 60, 0.55 / (float(s.max()) * rm))      # keep the twisting tips under 45 degrees
    tz, ts = _taper_top(zs, s, rm, float(s[-1]) * rm * 0.6)
    Z = list(zs) + tz
    S = list(s) + ts
    loops = [_rot(loop * sc, z * rate) for z, sc in zip(Z, S)]
    return _loft(np.array(Z), loops)


def _rot(p, a):
    c, s_ = math.cos(a), math.sin(a)
    return p @ np.array([[c, s_], [-s_, c]])


def obelisk(zs, r):
    """Tapering square obelisk with a pyramid tip."""
    loop, rm = _unit(_ngon(4, math.pi / 4))
    s = _scale_profile(r, rm, 0.5)
    H = zs[-1]
    s = np.maximum(s, float(s.max()) * (1 + 0.25 * (1 - zs / max(H, 1))))
    tz, ts = _taper_top(zs, s, rm, float(s[-1]) * 0.55)
    return _loft(np.array(list(zs) + tz), [loop * v for v in list(s) + ts])


def lighthouse(zs, r):
    """Tapered lighthouse tower with a gallery, lantern room and domed cap."""
    H = float(zs[-1])
    R = float(r.max()) + 1.0
    body = [(R + 3 * (1 - z / max(H, 1)), z) for z in zs]
    rb = R
    lan = rb * 0.65
    prof = [(0.0, 0.0)] + body + [(rb + 3, H + 3), (rb + 3, H + 5), (lan, H + 5), (lan, H + 13),
                                   (lan + 1.5, H + 14.5), (lan + 1.5, H + 15.5), (0.0, H + 15.5 + lan * 0.9)]
    return _revolve(prof, sections=48)


def rocket(zs, r):
    """Rocket with a pointed nose cone and three fins."""
    H = float(zs[-1])
    R = float(r.max()) + 0.5
    nose = [(R * math.sqrt(max(0.0, 1 - (t ** 2))), H + 1.8 * R * t) for t in np.linspace(0, 1, 16)]
    body = _revolve([(0.0, 0.0), (R, 0.0)] + nose[:-1] + [(0.0, H + 1.8 * R)], sections=48)
    fins = []
    from shapely.geometry import Polygon
    fh = min(max(12.0, H * 0.35), H)
    prof = Polygon([(R - 1, 0), (R + 9, 0), (R + 9, 4), (R - 1, 4 + 10 + fh * 0.4)])
    for k in range(3):
        f = trimesh.creation.extrude_polygon(prof, 2.4)
        f.apply_translation([0, 0, -1.2])
        f.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0]))
        f.apply_transform(trimesh.transformations.rotation_matrix(k * 2 * math.pi / 3, [0, 0, 1]))
        fins.append(f)
    return _union([body] + fins)


def rook(zs, r):
    """Castle tower (chess rook) with a flared base and battlements."""
    H = float(zs[-1])
    R = float(r.max()) + 0.5
    prof = [(0.0, 0.0), (R + 4, 0.0), (R + 4, 2.0), (R, 6.0), (R, H), (R + 2.5, H + 2.5), (R + 2.5, H + 9), (0.0, H + 9)]
    t = _revolve(prof, sections=64)
    cuts = [_box(2 * R + 10, 5, 5, (-(R + 5), -2.5, H + 4.5))]
    for k in range(1, 4):
        c = cuts[0].copy()
        c.apply_transform(trimesh.transformations.rotation_matrix(k * math.pi / 4, [0, 0, 1]))
        cuts.append(c)
    well = trimesh.creation.cylinder(radius=R - 1.0, height=5, sections=64)
    well.apply_translation([0, 0, H + 6 + 2.5])
    return tb.difference([t] + cuts + [well], engine="manifold")


def pagoda(zs, r):
    """Stacked pagoda with flared roofs every 14 mm and a spire."""
    loop, rm = _unit(_ngon(4, math.pi / 4))
    s = _scale_profile(r, rm, 0.6)
    T = 14.0
    u = zs % T
    eave = np.where(u < T - 6, 0.0, np.where(u < T - 3, u - (T - 6), (T - u)))   # out at 45, back in over 3 mm
    s = s + eave / rm
    tz, ts = _taper_top(zs, s, rm, 10.0)
    return _loft(np.array(list(zs) + tz), [loop * v for v in list(s) + ts])


def crystals(zs, r):
    """A cluster of pointed hexagonal crystals."""
    H = float(zs[-1])
    loop, rm = _unit(_ngon(6))
    s0 = math.sqrt(math.pi) * float(r.max()) + 0.6
    def crystal(scale, h, at):
        Z = [0.0, h] + [h + scale * rm * 0.9 * (i + 1) / 4 for i in range(4)]
        S = [scale, scale] + [scale * max(0.06, 1 - (i + 1) / 4) for i in range(4)]
        c = _loft(np.array(Z), [loop * v for v in S])
        c.apply_translation([at[0], at[1], 0])
        return c
    parts = [crystal(s0, H, (0, 0))]
    rng = np.random.default_rng(7)
    R0 = s0 * rm
    for k in range(5):
        sc = s0 * rng.uniform(0.35, 0.55)
        a = k * 2 * math.pi / 5 + rng.uniform(-0.3, 0.3)
        d = R0 + sc * rm * 0.35
        parts.append(crystal(sc, max(4.0, H * rng.uniform(0.3, 0.7)), (d * math.cos(a), d * math.sin(a))))
    return _union(parts)


def tree(zs, r):
    """Stylised pine tree: tiers that flare out at 45 degrees, topped with a point."""
    T, ext = 10.0, 4.0
    u = zs % T
    flare = np.where(u < ext, u, ext * (T - u) / (T - ext))
    rr = r * _poly_k(9) + 0.5 + flare
    H = float(zs[-1])
    prof = [(0.0, 0.0)] + list(zip(rr, zs)) + [(0.0, H + rr[-1] * 1.5)]
    return _revolve(prof, sections=9)


def skyline(zs, r):
    """A block of skyscrapers around one tall tower with an antenna."""
    H = float(zs[-1])
    a0 = math.sqrt(math.pi) * float(r.max()) + 0.8
    parts = [_box(a0, a0, H, (-a0 / 2, -a0 / 2, 0)), _box(2, 2, 10, (-1, -1, H - 0.5))]
    rng = np.random.default_rng(3)
    for k in range(8):
        w, d = rng.uniform(8, 15), rng.uniform(8, 15)
        h = max(5.0, H * rng.uniform(0.25, 0.85))
        a = k * 2 * math.pi / 8
        cx, cy = math.cos(a) * (a0 / 2 + w * 0.3), math.sin(a) * (a0 / 2 + d * 0.3)
        parts.append(_box(w, d, h, (cx - w / 2, cy - d / 2, 0)))
    return _union(parts)


# ---- functional
def pen_cup(zs, r):
    """Round cup for pens, brushes or tools; the purge goes into its thick wall and solid floor."""
    R = max(20.0, float(r.max()) / 0.72 + 1.0)
    floor = 2.4
    ri = np.sqrt(np.clip(R ** 2 - r ** 2, 0, None))
    ri = np.minimum(ri, R - 1.8)
    for i in range(len(zs) - 2, -1, -1):          # inside wall may only move inward at 45 degrees going up
        ri[i] = min(ri[i], ri[i + 1] + DZ)
    H = max(float(zs[-1]), 25.0)
    inner = [(float(a), float(z)) for a, z in zip(ri, zs) if z >= floor] or [(R - 2.0, floor)]
    inner = inner + [(inner[-1][0], H)] if inner[-1][1] < H else inner
    prof = [(0.0, 0.0), (R, 0.0), (R, H)] + list(reversed(inner)) + [(0.0, floor)]
    return _revolve(prof, sections=96)


def hex_bin(zs, r):
    """Hexagonal storage bin with a solid floor."""
    A = math.pi * r ** 2
    k = 3 * math.sqrt(3) / 2                       # hex area = k * R^2 (R = corner radius)
    Ro = max(22.0, math.sqrt(float(A.max()) / k) / 0.7)
    Ri = np.sqrt(np.clip(Ro ** 2 - A / k, 0, None))
    Ri = np.minimum(Ri, Ro - 2.6)
    for i in range(len(zs) - 2, -1, -1):
        Ri[i] = min(Ri[i], Ri[i + 1] + DZ)
    H = max(float(zs[-1]), 25.0)
    floor = 2.4
    hexl = _ngon(6)
    outer = _loft(np.array([0.0, H]), [hexl * Ro, hexl * Ro])
    zi = [floor] + [float(z) for z in zs if z > floor] + [H + 1.0]
    rr = [float(np.interp(z, zs, Ri)) for z in zi]
    inner = _loft(np.array(zi), [hexl * v for v in rr])
    return tb.difference([outer, inner], engine="manifold")


def planter(zs, r):
    """Flared round planter with a rolled rim."""
    H = max(float(zs[-1]), 30.0)
    R0 = max(18.0, float(r.max()) / 0.75)
    zz = np.arange(0.0, H + DZ, DZ)
    Ro = R0 + 0.6 * zz                              # flares out at ~31 degrees from vertical
    need = np.interp(zz, zs, r, right=0.0)
    ri = np.sqrt(np.clip(Ro ** 2 - need ** 2, 0, None))
    ri = np.minimum(ri, Ro - 2.0)
    for i in range(len(zz) - 2, -1, -1):
        ri[i] = min(ri[i], ri[i + 1] + DZ)
    floor = 3.0
    outer = [(float(a), float(z)) for a, z in zip(Ro, zz)]
    rim = [(Ro[-1] + 2.0, H + 2.0), (Ro[-1] + 2.0, H + 3.5), (ri[-1], H + 3.5)]
    inner = [(float(a), float(z)) for a, z in zip(ri, zz) if z >= floor]
    prof = [(0.0, 0.0)] + outer + rim + list(reversed(inner)) + [(0.0, floor)]
    return _revolve(prof, sections=72)


def bookend(zs, r):
    """L-shaped bookend with a 45 degree brace."""
    H = max(float(zs[-1]), 60.0)
    A = float(math.pi * r.max() ** 2)
    W = 60.0
    t = max(5.0, A / W)
    if t > 20:
        W, t = A / 20.0, 20.0
    L = max(70.0, H * 0.75)
    base = _box(L, W, 3.0)
    upright = _box(t, W, H)
    from shapely.geometry import Polygon
    brace = trimesh.creation.extrude_polygon(Polygon([(t - 0.1, 2.9), (t + 25, 2.9), (t - 0.1, 28)]), 4.0)
    brace.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0]))
    brace.apply_translation([0, W / 2 + 2.0, 0])
    return _union([base, upright, brace])


def bit_holder(zs, r):
    """Block of hex holes for 1/4 inch driver bits."""
    H = max(float(zs[-1]), 24.0)
    A = float(math.pi * r.max() ** 2)
    flats = 6.9
    hole_a = math.sqrt(3) / 2 * flats ** 2
    cols, pitch = 6, 11.0
    X = cols * pitch + 4
    rows = 1
    while X * (rows * pitch + 4) - cols * rows * hole_a < A:
        rows += 1
    Y = rows * pitch + 4
    block = _box(X, Y, H)
    depth = min(18.0, H - 4)
    holes = []
    for i in range(cols):
        for j in range(rows):
            h = trimesh.creation.cylinder(radius=flats / math.sqrt(3), height=depth + 1, sections=6)
            h.apply_translation([2 + pitch * (i + 0.5), 2 + pitch * (j + 0.5), H - depth + (depth + 1) / 2])
            holes.append(h)
    return tb.difference([block] + holes, engine="manifold")


def organiser(zs, r):
    """Desk organiser: an open box with four compartments."""
    H = max(float(zs[-1]), 30.0)
    A = float(math.pi * r.max() ** 2)
    s = 60.0
    while s * s - (s - 3 * 2.0) ** 2 < A:
        s += 5.0
    t = max(2.0, (s - math.sqrt(max(0.0, s * s - A))) / 3)
    c = (s - 3 * t) / 2
    floor = 2.4
    pockets = [_box(c, c, H, (t + i * (c + t), t + j * (c + t), floor)) for i in range(2) for j in range(2)]
    return tb.difference([_box(s, s, H)] + pockets, engine="manifold")


def phone_stand(zs, r):
    """Phone stand: a back rest leaning back 20 degrees with a front lip."""
    from shapely.geometry import Polygon
    H = max(float(zs[-1]), 70.0)
    A = float(math.pi * r.max() ** 2)
    tb_ = 10.0
    W = max(60.0, A / tb_)
    if W > 110:
        W, tb_ = 110.0, A / 110.0
    lean = math.tan(math.radians(20))
    bx = tb_ + H * lean + 4
    Lb = bx + 45
    prof = Polygon([(0, 0), (Lb, 0), (Lb, 14), (Lb - 6, 14), (Lb - 6, 6), (bx, 6),
                    (bx - H * lean, H), (bx - H * lean - tb_, H), (bx - tb_, 4), (0, 4)])
    m = trimesh.creation.extrude_polygon(prof, W)
    m.apply_transform(trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0]))
    m.apply_translation([0, W, 0])
    return m


# ---- fidgets / toys
def _ring_stack(zs, r, shape: str):
    """Rings stacked on a post, printed in place; each ring spins on its own. Rings are slices between
    parallel 45 degree cones (nothing overhangs past 45 degrees), with 0.5 mm vertical gaps and
    0.45 mm clearance around the post; a cap keeps them on."""
    ro = max(14.0, float(r.max()) + 1.5)
    rp, clr, gap, T = 5.0, 0.45, 0.5, 9.0
    r0 = rp + clr
    H = max(float(zs[-1]), 2 * T)
    n = max(2, int(round(H / T)))
    base_t = 3.0

    def slab(z_lo, z_hi, r_out):
        prof = [(r0, z_lo), (r_out, z_lo + (r_out - r0)), (r_out, z_hi + (r_out - r0)), (r0, z_hi), (r0, z_lo)]
        m = _revolve(prof, sections=96)
        if not m.is_volume:
            raise ValueError("ring profile did not revolve into a solid")
        return m

    base = _revolve([(0, 0), (ro + 2, 0), (ro + 2, base_t + (ro + 2 - r0)), (rp, base_t + (rp - r0)),
                     (0, base_t + (rp - r0))], sections=96)
    top_z = base_t + n * T
    post = trimesh.creation.cylinder(radius=rp, height=top_z + 2.0, sections=64)
    post.apply_translation([0, 0, (top_z + 2.0) / 2])
    cap = _revolve([(0, top_z + 2.0), (rp, top_z + 2.0), (rp + 2.5, top_z + 4.5), (rp + 2.5, top_z + 6.5),
                    (0, top_z + 6.5)], sections=64)
    parts = [_union([base, post, cap])]
    for k in range(n):
        lo, hi = base_t + k * T + gap, base_t + (k + 1) * T
        rk = ro * (1 - 0.05 * k) if shape == "round" else ro
        rk = max(rk, float(np.interp(hi, zs, r)) + 1.0)
        ring = slab(lo, hi, rk * 1.25)
        if shape == "hex":
            prism = trimesh.creation.cylinder(radius=rk / math.cos(math.pi / 6), height=H * 3, sections=6)
        elif shape == "gear":
            prism = trimesh.creation.extrude_polygon(__import__("shapely.geometry", fromlist=["Polygon"]).Polygon(_gear(18, 0.1) * rk * 1.05), H * 3)
            prism.apply_translation([0, 0, -H * 1.5])
        else:
            prism = trimesh.creation.cylinder(radius=rk, height=H * 3, sections=96)
        if shape != "gear":
            prism.apply_translation([0, 0, 0])
        ring = tb.intersection([ring, prism], engine="manifold")
        parts.append(ring)
    m = trimesh.util.concatenate(parts)
    m.apply_translation([0, 0, -m.bounds[0][2]])
    return m


def spinner(zs, r):
    return _ring_stack(zs, r, "hex")


def gear_spinner(zs, r):
    return _ring_stack(zs, r, "gear")


def ring_stacker(zs, r):
    return _ring_stack(zs, r, "round")


def screw_nut(zs, r):
    """A twisting screw with a captive nut that spins up and down it (printed in place)."""
    rr = max(7.0, float(r.max()) + 0.5)
    e, pitch, clr = 1.6, 16.0, 0.55
    Rn0, rh0 = rr + e + 7.0, rr + e + clr
    H = max(float(zs[-1]), 2.0 + 6 + 3.0 + 2 * (Rn0 * 1.16 - rh0) + 6.0 + 14.0)   # room for the nut to travel
    Ht = H + 6.0
    circ = np.c_[np.cos(np.linspace(0, 2 * math.pi, 48, endpoint=False)), np.sin(np.linspace(0, 2 * math.pi, 48, endpoint=False))]

    def helix(rad, z0, z1):
        Z = np.arange(z0, z1 + DZ, DZ)
        return _loft(Z, [circ * rad + [e * math.cos(2 * math.pi * z / pitch), e * math.sin(2 * math.pi * z / pitch)] for z in Z])

    Rb = rr + e + 6
    base = _revolve([(0, 0), (Rb, 0), (Rb, 2.0), (rr + e - 0.5, 2.0 + Rb - (rr + e - 0.5)), (0, 2.0 + Rb - (rr + e - 0.5))], 64)
    cap = _revolve([(0, Ht), (rr - 0.5, Ht), (rr + e + 3, Ht + e + 3.5), (rr + e + 3, Ht + e + 5.5), (0, Ht + e + 5.5)], 64)
    rod = helix(rr, 0.0, Ht + 0.5)
    core = _union([base, rod, cap])
    Rn = rr + e + 7.0
    rh = rr + e + clr
    z1 = max(2.0 + Rb - rr + 3.0, min(H * 0.35, Ht - 2 * (Rn * 1.16 - rh) - 6.0 - 4.0))
    hn = 2 * (Rn * 1.16 - rh) + 6.0
    z2 = z1 + hn
    body = _revolve([(rh - 0.2, z1), (Rn * 1.2, z1 + Rn * 1.2 - rh + 0.2), (Rn * 1.2, z2 - (Rn * 1.2 - rh + 0.2)),
                     (rh - 0.2, z2), (rh - 0.2, z1)], 64)
    hexp = trimesh.creation.cylinder(radius=Rn, height=Ht * 3, sections=6)
    nut = tb.intersection([body, hexp], engine="manifold")
    nut = tb.difference([nut, helix(rr + clr, z1 - 1.0, z2 + 1.0)], engine="manifold")
    if z2 > Ht - 2:
        raise ValueError("print too short for the screw and nut")
    return trimesh.util.concatenate([core, nut])


CATALOG = {
    # statues / decor
    "totem": {"title": "Totem", "kind": "Statue", "make": totem, "about": "Faceted column with raised bands and a pointed top."},
    "twist": {"title": "Twist star", "kind": "Statue", "make": twist, "about": "Six-pointed star column that twists as it rises."},
    "obelisk": {"title": "Obelisk", "kind": "Statue", "make": obelisk, "about": "Tapering square obelisk with a pyramid tip."},
    "lighthouse": {"title": "Lighthouse", "kind": "Statue", "make": lighthouse, "about": "Tapered tower with a gallery, lantern room and dome."},
    "rocket": {"title": "Rocket", "kind": "Statue", "make": rocket, "about": "Rocket with a nose cone and three fins."},
    "rook": {"title": "Castle rook", "kind": "Statue", "make": rook, "about": "Castle tower with a flared base and battlements."},
    "pagoda": {"title": "Pagoda", "kind": "Statue", "make": pagoda, "about": "Stacked pagoda with flared roofs and a spire."},
    "crystals": {"title": "Crystal cluster", "kind": "Statue", "make": crystals, "about": "Cluster of pointed hexagonal crystals."},
    "tree": {"title": "Pine tree", "kind": "Statue", "make": tree, "about": "Stylised pine tree with flared tiers."},
    "skyline": {"title": "City block", "kind": "Statue", "make": skyline, "about": "Skyscrapers around a tall tower with an antenna."},
    # functional
    "cup": {"title": "Pen cup", "kind": "Functional", "make": pen_cup, "about": "Round cup for pens, brushes or tools; thick wall and floor."},
    "hexbin": {"title": "Hex bin", "kind": "Functional", "make": hex_bin, "about": "Hexagonal storage bin with a solid floor."},
    "planter": {"title": "Planter", "kind": "Functional", "make": planter, "about": "Flared round planter with a rolled rim."},
    "bookend": {"title": "Bookend", "kind": "Functional", "make": bookend, "about": "L-shaped bookend with a brace."},
    "bits": {"title": "Bit holder", "kind": "Functional", "make": bit_holder, "about": "Block of hex holes for 1/4 inch driver bits."},
    "organiser": {"title": "Desk organiser", "kind": "Functional", "make": organiser, "about": "Open box with four compartments."},
    "phonestand": {"title": "Phone stand", "kind": "Functional", "make": phone_stand, "about": "Leaning back rest with a front lip."},
    # fidgets / toys
    "spinner": {"title": "Spinner tower", "kind": "Fidget", "make": spinner, "about": "Hex rings on a post, printed in place; each spins."},
    "gears": {"title": "Gear spinner", "kind": "Fidget", "make": gear_spinner, "about": "Stack of spinning gear rings on a post."},
    "stacker": {"title": "Ring stacker", "kind": "Fidget", "make": ring_stacker, "about": "Round rings that get smaller going up; each spins."},
    "screw": {"title": "Screw and nut", "kind": "Fidget", "make": screw_nut, "about": "A nut that spins up and down a twisting screw."},
}


def make(design: str, need: list[tuple[float, float]]) -> trimesh.Trimesh:
    zs, r = radius_profile(need)
    m = CATALOG[design]["make"](zs, r)
    m.apply_translation([-m.bounds[0][0], -m.bounds[0][1], -m.bounds[0][2]])
    return m


def check(m: trimesh.Trimesh, need: list[tuple[float, float]]) -> dict:
    """Solid? overhangs past 45 degrees? enough material on every layer for the purge?"""
    bodies = m.split(only_watertight=False)
    solid = all(b.is_volume for b in bodies)
    nz = m.face_normals[:, 2]
    zc = m.triangles_center[:, 2]
    bad = (nz < -0.735) & (zc > 0.4)
    over = float(m.area_faces[bad].sum() / m.area)
    short, worst = 0, 1e9
    zs = sorted({round(z, 1) for z, _ in need})
    pick = zs[:: max(1, len(zs) // 25)]
    lookup = dict((round(z, 1), a) for z, a in need)
    for z in pick:
        sec = m.section(plane_origin=[0, 0, z + 0.05], plane_normal=[0, 0, 1])
        area = 0.0
        if sec is not None:
            planar, _ = sec.to_2D()
            area = float(sum(p.area for p in planar.polygons_full))
        ratio = area / max(lookup[z], 1e-6)
        worst = min(worst, ratio)
        short += ratio < 0.97
    return {"solid": solid, "bodies": len(bodies), "overhang_share": round(over, 4), "worst_capacity": round(worst, 2),
            "layers_short": short}
