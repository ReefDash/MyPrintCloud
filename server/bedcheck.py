"""Bed-clear check: is anything still on the build plate? Compares a fresh camera frame with the user's
photo of the empty bed.

The light rides on the print head, so brightness and shading change with wherever the head parked;
comparing pixels gives false alarms. Instead we compare edges: outlines that are in the new frame but
not (within a couple of pixels) in the empty-bed photo. Only the lower part of the frame is judged (the
bed; the head parks above it), and the score is the densest 12x12 patch of new edges, so scattered
noise doesn't add up the way a part's outline does.

Measured on the CC2 camera (11/02): same scene, head moved = 0-42; a ~2 cm part pasted on = 68+."""
from __future__ import annotations

import io

try:
    import numpy as np
    from PIL import Image, ImageFilter
except ImportError:  # the check stays off; queue starts go ahead as before
    np = None

W, H = 320, 180
BED_TOP = 0.56  # fraction of the frame height where the bed region starts
EDGE = 0.35     # edge strength, as a share of the frame's 99th percentile
WIN = 12        # patch size (px at W x H)
TOL = 60        # new-edge pixels in the densest patch that count as "something's on the bed"


def _edges(jpeg: bytes) -> "np.ndarray":
    im = Image.open(io.BytesIO(jpeg)).convert("L").resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.2))
    a = np.asarray(im, dtype=np.float32)[int(H * BED_TOP):]
    g = np.abs(np.diff(a, axis=1))[:-1] + np.abs(np.diff(a, axis=0))[:, :-1]
    return g / (np.percentile(g, 99) + 1e-6)


def score(ref: bytes, cur: bytes) -> int:
    """New-edge pixels in the densest patch: 0 = same as the empty bed; >= TOL = something's there."""
    old = Image.fromarray(((_edges(ref) > EDGE) * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
    new = ((_edges(cur) > EDGE) & ~(np.asarray(old) > 0)).astype(np.float32)
    c = np.pad(new.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    win = c[WIN:, WIN:] - c[:-WIN, WIN:] - c[WIN:, :-WIN] + c[:-WIN, :-WIN]
    return int(win.max()) if win.size else 0


def available() -> bool:
    return np is not None
