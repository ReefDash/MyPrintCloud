"""Preview picture for the printer's screen.

The slicer CLI skips thumbnails (it renders them with OpenGL only in the GUI), so the
printer shows a generic "gcode" icon. We render a shaded 3/4 view of the model in the
filament's colour and embed it the standard way (; thumbnail begin 144x144 <len>).
"""
from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image, ImageDraw

SIZE = 144          # what the CC2 machine preset asks for
SS = 3              # supersampling
MAX_FACES = 600_000


def _load(path: str):
    import trimesh
    m = trimesh.load(path, force="mesh")
    return np.asarray(m.vertices, float), np.asarray(m.faces, int)


def render(model_path: str, color: str = "#FF7A1A") -> bytes:
    v, f = _load(model_path)
    if len(f) > MAX_FACES:
        f = f[np.random.default_rng(0).choice(len(f), MAX_FACES, replace=False)]
    v = v - (v.min(0) + v.max(0)) / 2
    # 3/4 view from the front-right, looking slightly down
    yaw, pitch = np.radians(-35), np.radians(28)
    rz = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    p = v @ rz.T @ rx.T                       # x right, y depth (into screen), z up
    tri = p[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    vis = n[:, 1] < 0                         # facing the camera (camera looks along +y)
    tri, n = tri[vis], n[vis]
    light = np.array([-0.4, -0.75, 0.55]); light /= np.linalg.norm(light)
    shade = 0.35 + 0.65 * np.clip(n @ -(-light), 0, 1)
    order = np.argsort(-tri[:, :, 1].mean(1))  # far to near
    W = SIZE * SS
    xs, zs = tri[:, :, 0], tri[:, :, 2]
    span = max(xs.max() - xs.min(), zs.max() - zs.min()) or 1.0
    s = W * 0.86 / span
    cx, cz = (xs.max() + xs.min()) / 2, (zs.max() + zs.min()) / 2
    sx = (xs - cx) * s + W / 2
    sy = W / 2 - (zs - cz) * s
    base = np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)]) if len(color) == 7 else np.array([255, 122, 26])
    base = np.maximum(base, 40)  # keep black filament visible
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in order:
        c = tuple(int(x) for x in np.clip(base * shade[i], 0, 255)) + (255,)
        d.polygon([(sx[i, 0], sy[i, 0]), (sx[i, 1], sy[i, 1]), (sx[i, 2], sy[i, 2])], fill=c)
    out = io.BytesIO()
    img.resize((SIZE, SIZE), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def embed(gcode_path: str, png: bytes) -> None:
    """Insert a PrusaSlicer/Orca-style thumbnail block right after the header block."""
    b64 = base64.b64encode(png).decode()
    lines = [b64[i:i + 78] for i in range(0, len(b64), 78)]
    block = (";\n; THUMBNAIL_BLOCK_START\n"
             f"; thumbnail begin {SIZE}x{SIZE} {len(b64)}\n"
             + "".join(f"; {ln}\n" for ln in lines)
             + "; thumbnail end\n; THUMBNAIL_BLOCK_END\n")
    with open(gcode_path, encoding="utf-8", errors="surrogateescape") as fh:
        text = fh.read()
    if "; thumbnail begin" in text[:400_000]:
        return
    mark = "; HEADER_BLOCK_END\n"
    i = text.find(mark)
    i = i + len(mark) if 0 <= i < 400_000 else 0
    with open(gcode_path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        fh.write(text[:i] + block + text[i:])
