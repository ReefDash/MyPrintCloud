"""One persistent MJPEG connection to the printer camera (:8080), shared by everything.

The CC2's vendor streamer is fragile: lots of connects/disconnects have crashed it
until a power cycle. So PrintCast is the *only* client, reconnects with a slow
backoff, and re-serves frames to the dashboard instead of letting browsers hit it.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger("camera")

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
STALL_S = 12.0          # no new frame for this long = stalled
BACKOFF = [5, 10, 20, 40, 60]


class Camera:
    def __init__(self, url: str) -> None:
        self.url = url
        self.jpeg: bytes | None = None
        self.frame_id = 0
        self.frame_t = 0.0
        self.fps = 0.0
        self.reconnects = 0
        self.status = "starting"
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="camera", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def stalled(self) -> bool:
        return self.jpeg is None or time.time() - self.frame_t > STALL_S

    def latest(self) -> tuple[int, bytes | None]:
        return self.frame_id, self.jpeg

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            got_frames = self._read_stream()
            if self._stop.is_set():
                break
            attempt = 0 if got_frames else min(attempt + 1, len(BACKOFF) - 1)
            delay = BACKOFF[attempt]
            self.reconnects += 1
            self.status = f"reconnecting in {delay}s"
            log.warning("camera stream lost; retrying in %ss", delay)
            self._stop.wait(delay)

    def _read_stream(self) -> bool:
        """Read until the stream ends or stalls. Returns True if any frames arrived."""
        got = False
        try:
            # read timeout doubles as the stall watchdog
            with requests.get(self.url, stream=True, timeout=(5, STALL_S)) as r:
                if r.status_code != 200:
                    self.status = f"HTTP {r.status_code}"
                    return False
                self.status = "live"
                buf = bytearray()
                count, t0 = 0, time.time()
                for chunk in r.iter_content(chunk_size=65536):
                    if self._stop.is_set():
                        return got
                    buf.extend(chunk)
                    while True:
                        s = buf.find(SOI)
                        if s < 0:
                            buf.clear()
                            break
                        e = buf.find(EOI, s + 2)
                        if e < 0:
                            if s > 0:
                                del buf[:s]
                            break
                        self.jpeg = bytes(buf[s:e + 2])
                        self.frame_id += 1
                        self.frame_t = time.time()
                        got = True
                        del buf[:e + 2]
                        count += 1
                    if len(buf) > 8 * 1024 * 1024:
                        buf.clear()
                    dt = time.time() - t0
                    if dt >= 5:
                        self.fps = count / dt
                        count, t0 = 0, time.time()
        except requests.RequestException as e:
            self.status = f"offline ({type(e).__name__})"
        return got
