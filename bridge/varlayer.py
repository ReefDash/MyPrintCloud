"""Variable (adaptive) layer height: thin layers where the surface is shallow, thick where it's steep.

The slicer CLI has no switch for this, but a 3MF can carry height-range modifiers
(Metadata/layer_config_ranges.xml: "from z to z use layer_height h"). We work out the heights
from the mesh ourselves and wrap each model in a 3MF carrying the ranges. (The slicer ignores
a Metadata/layer_heights_profile.txt in a plain 3MF, and refuses variable layers together with
"organic" tree supports, so callers switch the support style, see SUPPORT_STYLE.)

Stair-step error on a surface whose normal has vertical component nz is cusp = h * |nz|
(0 on a vertical wall, h on a near-flat slope), so the layer height that keeps the error at
`cusp` is h = cusp / |nz|, clamped to [hmin, hmax]. Exactly flat faces (tops, bottoms) don't
stair-step and are ignored.
"""
from __future__ import annotations

import zipfile

import numpy as np
import trimesh

STEP = 0.05          # mm, resolution of the height analysis
FLAT = 0.985         # |nz| above this = a flat face (no stair-stepping)
TRANSITION = 0.5     # mm of layer height change allowed per mm of Z (smooth ramps)
QUANT = 0.04         # layer heights come in these steps (fewer, calmer ranges)
MIN_RANGE = 0.8      # mm: a height band shorter than this is merged into its finer neighbour
LEVELS = {           # name -> (cusp mm, min layer, max layer)
    "fast": (0.14, 0.12, 0.32),
    "balanced": (0.10, 0.08, 0.28),
    "fine": (0.06, 0.08, 0.20),
}
SUPPORT_STYLE = "tree_slim"   # the slicer rejects variable layers with its default (organic) tree supports


def analyse(mesh: trimesh.Trimesh, cusp: float, hmin: float, hmax: float) -> tuple[np.ndarray, np.ndarray]:
    """(z at bin starts, wanted layer height) for a mesh already sitting on z = 0."""
    zmax = float(mesh.bounds[1][2])
    nb = max(1, int(np.ceil(zmax / STEP)))
    tri = mesh.triangles
    zlo, zhi = tri[:, :, 2].min(1), tri[:, :, 2].max(1)
    nz = np.abs(mesh.face_normals[:, 2])
    area = mesh.area_faces
    keep = (nz < FLAT) & (area > 1e-9)
    zlo, zhi, nz, area = zlo[keep], zhi[keep], nz[keep], area[keep]
    if not len(nz):
        return np.arange(nb) * STEP, np.full(nb, hmax)
    b0 = np.clip((zlo / STEP).astype(int), 0, nb - 1)
    b1 = np.clip((zhi / STEP).astype(int), 0, nb - 1)
    n = b1 - b0 + 1
    idx = np.repeat(np.arange(len(n)), n)
    bins = b0[idx] + (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n))
    w = (area / n)[idx]                      # a face's area is spread over the bins it spans
    v = nz[idx]
    order = np.lexsort((-v, bins))            # per bin, highest nz first
    bins, v, w = bins[order], v[order], w[order]
    cw = np.cumsum(w)
    first = np.searchsorted(bins, np.arange(nb), side="left")
    last = np.searchsorted(bins, np.arange(nb), side="right")
    base = np.where(first > 0, cw[np.maximum(first - 1, 0)], 0.0)
    total = np.where(last > first, cw[np.maximum(last - 1, 0)] - base, 0.0)
    # ignore tessellation noise: take the nz that the top 12 % of the sloped area in the bin reaches
    pick = np.clip(np.searchsorted(cw, base + 0.12 * total, side="left"), first, np.maximum(last - 1, first))
    nz_eff = np.where(total > 0, v[np.minimum(pick, len(v) - 1)], 0.0)
    h = np.where(nz_eff > 1e-3, cusp / np.maximum(nz_eff, 1e-3), hmax)
    h = np.clip(h, hmin, hmax)
    s = TRANSITION * STEP                     # smooth: height may only change this much per bin
    for i in range(1, nb):
        h[i] = min(h[i], h[i - 1] + s)
    for i in range(nb - 2, -1, -1):
        h[i] = min(h[i], h[i + 1] + s)
    return np.arange(nb) * STEP, h


def ranges(mesh: trimesh.Trimesh, level: str | tuple = "balanced") -> list[tuple[float, float, float]]:
    """[(z_from, z_to, layer_height)] covering the whole object height, heights in QUANT steps."""
    cusp, hmin, hmax = LEVELS[level] if isinstance(level, str) else level
    z, h = analyse(mesh, cusp, hmin, hmax)
    q = np.maximum(np.floor(h / QUANT + 1e-6) * QUANT, hmin)       # round down: never coarser than wanted
    q = np.round(np.minimum(q, hmax), 3)
    runs: list[list[float]] = []                                    # [z0, z1, h]
    for zi, qi in zip(z, q):
        if runs and abs(runs[-1][2] - qi) < 1e-6:
            runs[-1][1] = zi + STEP
        else:
            runs.append([float(zi), float(zi + STEP), float(qi)])
    changed = True
    while changed and len(runs) > 1:                                # merge short bands into the finer neighbour
        changed = False
        for i, r in enumerate(runs):
            if r[1] - r[0] < MIN_RANGE:
                nb = [j for j in (i - 1, i + 1) if 0 <= j < len(runs)]
                j = min(nb, key=lambda k: runs[k][2])
                lo, hi = sorted((i, j))
                runs[lo] = [runs[lo][0], runs[hi][1], min(runs[lo][2], runs[hi][2])]
                del runs[hi]
                changed = True
                break
    runs[-1][1] = float(mesh.bounds[1][2]) + 1.0                     # past the top, so nothing is left uncovered
    runs[0][0] = 0.0
    return [(round(float(a), 3), round(float(b), 3), float(c)) for a, b, c in runs]


class Writer3MF:
    """Builds the XML for a mesh once, then writes a 3MF per copy (different object name) quickly."""

    def __init__(self, mesh: trimesh.Trimesh, rng: list[tuple[float, float, float]]) -> None:
        v, f = mesh.vertices, mesh.faces
        vx = "".join(f'<vertex x="{a:.5f}" y="{b:.5f}" z="{c:.5f}"/>' for a, b, c in v)
        fx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
        self.model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
                      'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
                      '<object id="1" name="@@NAME@@" type="model"><mesh><vertices>' + vx + '</vertices><triangles>' + fx +
                      '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
        self.ranges = ('<?xml version="1.0" encoding="UTF-8"?>\n<objects>\n <object id="1">\n' + "".join(
            f'  <range min_z="{a:g}" max_z="{b:g}">\n   <option opt_key="layer_height">{h:g}</option>\n  </range>\n'
            for a, b, h in rng) + ' </object>\n</objects>\n')

    TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/></Types>')
    RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '</Relationships>')

    def save(self, path: str, name: str) -> str:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr("[Content_Types].xml", self.TYPES)
            z.writestr("_rels/.rels", self.RELS)
            z.writestr("3D/3dmodel.model", self.model.replace("@@NAME@@", name))
            z.writestr("Metadata/layer_config_ranges.xml", self.ranges)
        return path
