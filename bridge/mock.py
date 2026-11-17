"""Fake CC2 for testing the bridge without the printer (python bridge.py --mock)."""
from __future__ import annotations

import io
import threading
import time

from PIL import Image, ImageDraw

TRAYS = [("#E8E8E8", "PLA", "PLA Basic"), ("#FF7A1A", "PLA", "PLA Matte"),
         ("#2E86DE", "PETG", "PETG"), ("#222222", "PLA", "PLA Basic")]


class MockPrinter:
    def __init__(self) -> None:
        self.connected = True
        self.registered = threading.Event()
        self.registered.set()
        self.last_error = ""
        self.files_ = [{"filename": "calicat.gcode", "layer": 100}]
        self.history_: list[dict] = []
        self.job: dict | None = None  # {"file", "t0", "dur", "paused_at", "state"}
        self.light = True
        self.auto_refill = True
        self.speed = 1
        self.trays_ = [{"color": c, "type": t, "name": n, "loaded": True} for c, t, n in TRAYS]
        self.requests: list = []

    def start(self) -> None: ...
    def close(self) -> None: ...

    def set_light(self, on: bool) -> bool:
        self.light = on
        return True

    def upload(self, path, remote_name, progress=None) -> None:
        import os
        total = os.path.getsize(path)
        for i in range(5):
            time.sleep(0.2)
            if progress:
                progress(total * (i + 1) // 5, total)
        self.files_.insert(0, {"filename": remote_name, "layer": 120})

    def start_print(self, filename, slot_map=None, **kw):
        if self.job and self.job["state"] in ("PRINTING", "PAUSED", "HEATING"):
            return {"error_code": 1009}
        self.job = {"file": filename, "t0": time.time(), "dur": 90.0, "paused": 0.0, "pause_t": None,
                    "state": "HEATING", "slot_map": slot_map}
        return {"error_code": 0}

    def pause(self):
        if self.job and self.job["pause_t"] is None:
            self.job["pause_t"] = time.time()
        return {"error_code": 0}

    def resume(self):
        if self.job and self.job["pause_t"] is not None:
            self.job["paused"] += time.time() - self.job["pause_t"]
            self.job["pause_t"] = None
        return {"error_code": 0}

    def stop(self):
        if self.job:
            self._finish(2)
        return {"error_code": 0}

    def set_speed(self, mode):
        self.speed = int(mode)
        return {"error_code": 0}

    def request(self, method, params=None, timeout=10.0):
        """The stock-firmware methods the app's controls use; trays change like the real Canvas."""
        params = params or {}
        self.requests.append((method, params))
        if method == 2003:
            t = self.trays_[int(params["tray_id"])]
            t.update(name=params["filament_name"], type=params["filament_type"], color=params["filament_color"], loaded=True)
        elif method in (2001, 2002):
            self.trays_[int(params["tray_id"])]["loaded"] = method == 2001
        return {"error_code": 0}

    def refresh_canvas(self):
        pass

    def set_auto_refill(self, on):
        self.auto_refill = bool(on)
        return {"error_code": 0}

    def history(self):
        return self.history_

    def files(self):
        return self.files_

    def _finish(self, status: int) -> None:
        self.history_.insert(0, {"task_name": self.job["file"], "task_status": status,
                                 "begin_time": int(self.job["t0"]), "end_time": int(time.time())})
        self.job = None

    def _progress(self) -> float:
        j = self.job
        now = j["pause_t"] or time.time()
        return max(0.0, min(1.0, (now - j["t0"] - j["paused"] - 6) / j["dur"]))

    def snapshot(self) -> dict:
        state, p, fn = "IDLE", 0.0, ""
        if self.job:
            fn = self.job["file"]
            p = self._progress()
            t = time.time() - self.job["t0"]
            if self.job["pause_t"] is not None:
                state = "PAUSED"
            elif t < 6:
                state = "HEATING"
            elif p < 1:
                state = "PRINTING"
            else:  # like the real printer: COMPLETE stays up a while, then IDLE
                if not self.job.get("done_t"):
                    self.history_.insert(0, {"task_name": fn, "task_status": 1,
                                             "begin_time": int(self.job["t0"]), "end_time": int(time.time())})
                    self.job["done_t"] = time.time()
                state, p = "COMPLETE", 1.0
                if time.time() - self.job["done_t"] > 15:
                    self.job, state, p, fn = None, "IDLE", 0.0, ""
        busy = state != "IDLE"
        dur = self.job["dur"] if self.job else 0
        return {
            "online": True, "error": "", "state": state, "filename": fn,
            "progress": int(p * 100), "layer": int(p * 120), "total_layers": 120 if fn else 0,
            "elapsed_s": p * dur, "remaining_s": (1 - p) * dur if fn else 0,
            "nozzle": 220 if busy else 26, "nozzle_target": 220 if busy else 0,
            "bed": 60 if busy else 25, "bed_target": 60 if busy else 0, "chamber": 31,
            "head_speed": 200 if state == "PRINTING" else 0,
            "speed_mode": ["Silent", "Balanced", "Sport", "Ludicrous"][self.speed], "light": self.light,
            "filament_detected": 1, "exceptions": [],
            "trays": [{"id": i, **t, "active": busy and i == 0} for i, t in enumerate(self.trays_)],
            "auto_refill": self.auto_refill, "updated": time.time(),
        }


class MockCamera:
    def __init__(self, printer: MockPrinter) -> None:
        self.p = printer
        self.frame_id = 0
        self.status = "live"

    def start(self) -> None: ...

    def latest(self):
        s = self.p.snapshot()
        img = Image.new("RGB", (640, 360), (30, 32, 38))
        d = ImageDraw.Draw(img)
        d.rectangle((120, 300, 520, 320), fill=(80, 80, 90))
        h = int(140 * s["progress"] / 100)
        d.rectangle((280, 300 - h, 360, 300), fill=(255, 122, 26))
        d.text((10, 10), f"MOCK CAM  {time.strftime('%H:%M:%S')}  {s['state']} {s['progress']}%", fill=(230, 230, 230))
        b = io.BytesIO()
        img.save(b, "JPEG", quality=70)
        self.frame_id += 1
        return self.frame_id, b.getvalue()
