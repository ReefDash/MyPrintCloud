"""Failure watch: Obico's open spaghetti-detection model (AGPL-3.0, github.com/TheSpaghettiDetective/obico-server)
run on the camera frames the bridge sends every ~20 s while printing.

Raw detections are noisy, so like Obico we judge a smoothed score against this printer's own long-run
baseline: a short EWM of each frame's summed box confidence, minus the long rolling mean, crossing a
threshold. Obico's constants were tuned for 10 s frames; the frame counts here are halved for 20 s."""
from __future__ import annotations

import io
import json
import os
import threading

try:
    import numpy as np
except ImportError:  # the watch stays off; the rest of the server runs without it
    np = None

MODEL_URL = "https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx"
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
CONF, NMS = 0.08, 0.45
EWM_SPAN, WIN_SHORT, WIN_LONG = 6, 155, 3600
LOW, HIGH, SHORT_MULT, INIT_SAFE = 0.38, 0.78, 3.8, 15


NEEDS_MB = 350  # measured on the ARM server: model load ~215 MB + ~60 MB working set, plus margin


def _cgroup_room() -> int | None:
    """MB left under this process's systemd MemoryMax (cgroup v2), or None if unlimited / not Linux."""
    try:
        path = open("/proc/self/cgroup", encoding="utf-8").read().strip().split("::", 1)[1]
        base = "/sys/fs/cgroup" + path
        lim = open(base + "/memory.max", encoding="utf-8").read().strip()
        if lim == "max":
            return None
        cur = int(open(base + "/memory.current", encoding="utf-8").read().strip())
        return (int(lim) - cur) // (1024 * 1024)
    except (OSError, IndexError, ValueError):
        return None


def _nms(boxes: np.ndarray, conf: np.ndarray) -> list[int]:
    x1, y1, x2, y2 = boxes.T
    area = (x2 - x1) * (y2 - y1)
    order, keep = conf.argsort()[::-1], []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1, yy1 = np.maximum(x1[i], x1[order[1:]]), np.maximum(y1[i], y1[order[1:]])
        xx2, yy2 = np.minimum(x2[i], x2[order[1:]]), np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (area[i] + area[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= NMS]
    return keep


class Detector:
    def __init__(self, path: str) -> None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1  # leave the server's other core free
        self.sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.name, self.h, self.w = inp.name, int(inp.shape[2]), int(inp.shape[3])

    def detect(self, jpeg: bytes) -> list[tuple[float, tuple[float, float, float, float]]]:
        from PIL import Image
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
        W, H = im.size
        x = np.asarray(im.resize((self.w, self.h), Image.BILINEAR), dtype=np.float32) / 255.0
        x = x.transpose(2, 0, 1)[None]
        boxes, confs = self.sess.run(None, {self.name: x})[:2]
        b, c = boxes[0][:, 0, :], confs[0].max(axis=1)
        m = c > CONF
        b, c = b[m], c[m]
        out = []
        for i in _nms(b, c) if len(c) else []:
            x1, y1, x2, y2 = b[i]
            out.append((float(c[i]), (float(x1 * W), float(y1 * H), float(x2 * W), float(y2 * H))))
        return out


class Watch:
    """Per-print smoothed failure score. `feed` returns "pause", "alert" or "" for each frame."""

    def __init__(self, model_path: str, state_path: str) -> None:
        self.model_path, self.state_path = model_path, state_path
        self.det: Detector | None = None
        self.error = ""
        self.lock = threading.Lock()
        try:
            st = json.load(open(state_path, encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        self.long, self.lifetime = float(st.get("long", 0.0)), int(st.get("lifetime", 0))
        self.reset("")

    @property
    def ready(self) -> bool:
        if self.det is None and not self.error:
            room = _cgroup_room()
            if np is None:
                self.error = "numpy not installed"
            elif room is not None and room < NEEDS_MB:  # loading would get the whole server OOM-killed
                self.error = (f"not enough memory: the watch needs about {NEEDS_MB} MB and the server's limit leaves "
                              f"{room} MB (raise MemoryMax in the myprintcloud service)")
            elif not os.path.exists(self.model_path):
                self.error = "model not installed"
            else:
                try:
                    self.det = Detector(self.model_path)
                except Exception as e:  # onnxruntime missing or a bad file: watch stays off
                    self.error = f"model failed to load: {e}"
        return self.det is not None

    def reset(self, key: str) -> None:
        self.key, self.n, self.ewm, self.short, self.score, self.last = key, 0, 0.0, 0.0, 0.0, []

    def feed(self, key: str, jpeg: bytes) -> str:
        """key identifies the print; a new key starts the per-print state."""
        if not self.ready or not self.lock.acquire(blocking=False):
            return ""
        try:
            if key != self.key:
                self.reset(key)
            dets = self.det.detect(jpeg)
            p = sum(c for c, _ in dets)
            self.last = dets
            a = 2 / (EWM_SPAN + 1)
            self.ewm = p * a + self.ewm * (1 - a)
            self.short += (p - self.short) / (WIN_SHORT if WIN_SHORT <= self.n else self.n + 1)
            self.long += (p - self.long) / (WIN_LONG if WIN_LONG <= self.lifetime else self.lifetime + 1)
            self.n += 1
            self.lifetime += 1
            if self.lifetime % 20 == 0:
                try:
                    json.dump({"long": self.long, "lifetime": self.lifetime}, open(self.state_path, "w", encoding="utf-8"))
                except OSError:
                    pass
            tw = min(HIGH, max(LOW, (self.short - self.long) * SHORT_MULT))
            adj = self.ewm - self.long
            self.score = round(max(0.0, min(1.0, adj / (tw * 1.75 * 1.5))), 3)  # 0..1 for the app
            if self._failing(1.75):
                return "pause"
            if self._failing(1.0):
                return "alert"
            return ""
        finally:
            self.lock.release()

    def _failing(self, esc: float) -> bool:
        if self.n < INIT_SAFE:
            return False
        adj = (self.ewm - self.long) / esc
        if adj < LOW:
            return False
        return adj > HIGH or adj > (self.short - self.long) * SHORT_MULT
