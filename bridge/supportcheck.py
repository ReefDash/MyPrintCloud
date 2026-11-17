"""Would supports end up trapped inside the model? (e.g. filament channels, tubes, ducts)

trapped(mesh) looks at every overhang the slicer would support (steeper than the support angle, not on
the bed) whose support would have to stand on the model itself. If the space under it is a narrow
channel (walls close by all the way round), supports printed there can't be pulled out.
Returns {"trapped_mm2", "spots", "advice"}; advice "plate" = use "supports from the build plate only"."""
import numpy as np
import trimesh

ANGLE = 30.0          # the bridge's default support threshold angle
NARROW = 12.0         # walls within this distance in all eight directions = a channel
MIN_AREA = 15.0       # mm2 of trapped overhang before we change anything


def trapped(mesh: trimesh.Trimesh, angle: float = ANGLE, max_samples: int = 4000) -> dict:
    m = mesh
    if len(m.faces) < 400_000:                       # drop junk fragments and hidden inner voids
        try:
            bodies = m.split(only_watertight=False)
            if len(bodies) > 1:
                big = max(abs(b.volume) for b in bodies)
                keep = [b for b in bodies if b.volume > 0.01 * big]
                if keep:
                    m = trimesh.util.concatenate(keep)
        except Exception:
            pass
    z0 = float(m.bounds[0][2])
    n, c, a = m.face_normals, m.triangles_center, m.area_faces
    over = (n[:, 2] < -np.cos(np.radians(90 - angle))) & (c[:, 2] > z0 + 0.5) & (a > 1e-4)
    idx = np.where(over)[0]
    if not len(idx):
        return {"trapped_mm2": 0.0, "spots": 0, "advice": ""}
    if len(idx) > max_samples:                       # big models: sample by area
        p = a[idx] / a[idx].sum()
        idx = np.random.default_rng(0).choice(idx, max_samples, replace=False, p=p)
        weight = a[over].sum() / a[idx].sum()
    else:
        weight = 1.0
    start = c[idx] - n[idx] * 0.05                   # just outside the overhang surface, in the air under it
    down = np.tile([0.0, 0.0, -1.0], (len(idx), 1))
    on_model = m.ray.intersects_any(start, down)     # support would stand on the part, not the bed
    cand = idx[on_model]
    if not len(cand):
        return {"trapped_mm2": 0.0, "spots": 0, "advice": ""}
    s = c[cand] - n[cand] * 0.05 - np.array([0, 0, 0.6])
    close = np.ones(len(cand), bool)
    r2 = 0.7071
    for d in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [r2, r2, 0], [-r2, r2, 0], [r2, -r2, 0], [-r2, -r2, 0]):
        loc, ri, _ = m.ray.intersects_location(s, np.tile(d, (len(cand), 1)), multiple_hits=False)
        dist = np.full(len(cand), np.inf)
        if len(ri):
            dist[ri] = np.linalg.norm(np.asarray(loc).reshape(-1, 3) - s[ri], axis=1)
        close &= dist < NARROW
    area = float(a[cand][close].sum() * weight)
    return {"trapped_mm2": round(area, 1), "spots": int(close.sum()),
            "advice": "plate" if area >= MIN_AREA else ""}


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(p, trapped(trimesh.load(p, force="mesh")))
