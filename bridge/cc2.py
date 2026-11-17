"""Centauri Carbon 2 client: JSON-RPC over MQTT (:1883), auth = 'elegoo' + LAN access code.

Topics (serial = printer SN):
  elegoo/<sn>/api_register                -> {"client_id", "request_id"}
  elegoo/<sn>/<request_id>/register_response
  elegoo/<sn>/<client_id>/api_request     -> {"id", "method", "params"}
  elegoo/<sn>/<client_id>/api_response    <- replies + method 6000 status deltas

Methods used: 1001 attrs, 1002 full status, 1029 light, 1036 history,
1042 camera enable, 1044 file list (total layers), 2005 Canvas.
The printer forgets a client that goes quiet, so an app-level PING is sent
every 30 s. 3+ back-to-back requests trip a short cooldown, so requests are
serialised and spaced.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger("cc2")

MQTT_PORT = 1883
DISCOVERY_PORT = 52700
PING_INTERVAL_S = 30.0
FULL_REFRESH_S = 60.0
CANVAS_REFRESH_S = 20.0
REQUEST_SPACING_S = 0.6
REGISTER_RETRY_S = 20  # no register_response this long: send the registration again

SPEED_MODES = {0: "Silent", 1: "Balanced", 2: "Sport", 3: "Ludicrous"}
_PURGE_ZONE_Y = 258.0  # head parked behind the bed mid-print = Canvas filament swap


def discover(host: str, timeout: float = 3.0) -> dict[str, Any]:
    """Ask the printer for its identity (UDP 52700, method 7000)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(json.dumps({"id": 0, "method": 7000}).encode(), (host, DISCOVERY_PORT))
        data = json.loads(s.recvfrom(4096)[0].decode("utf-8"))
        return data.get("result", data)
    finally:
        s.close()


def find_printer(serial: str, timeout: float = 3.0) -> str:
    """Broadcast the identity question on the LAN; return the IP of the printer with this serial
    (or of the only printer that answers when no serial is known), else "". The router hands out a
    new address after a power cut, so the bridge uses this when the saved one stops answering."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    found: list[str] = []
    try:
        s.sendto(json.dumps({"id": 0, "method": 7000}).encode(), ("255.255.255.255", DISCOVERY_PORT))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
                r = json.loads(data.decode("utf-8"))
                r = r.get("result", r) if isinstance(r, dict) else {}
            except socket.timeout:
                continue
            except ValueError:
                continue
            if serial and r.get("sn") == serial:
                return addr[0]
            if r.get("sn"):
                found.append(addr[0])
    except OSError:
        return ""
    finally:
        s.close()
    return found[0] if not serial and len(found) == 1 else ""


def _deep_merge(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def state_label(raw: dict[str, Any]) -> str:
    """Collapse machine_status/sub_status into one human label."""
    ms = raw.get("machine_status", {})
    st, sub = ms.get("status", 0), ms.get("sub_status", 0)
    y = raw.get("gcode_move", {}).get("y")
    progress = ms.get("progress")
    if st in (0, 1):
        return "IDLE"
    if st == 2:
        if sub in (2801, 2802):
            return "HOMING"
        if sub in (2901, 2902):
            return "LEVELING"
        if sub == 2501:
            return "PAUSING"
        if sub in (2502, 2505):
            return "PAUSED"
        if sub == 2401:
            return "RESUMING"
        if sub == 2503:
            return "STOPPING"
        if sub == 2504:
            return "STOPPED"
        if sub == 2077:
            return "COMPLETE"
        layer = raw.get("print_status", {}).get("current_layer") or 0
        # the head also parks back there while preheating/wiping before layer 1
        if y is not None and y >= _PURGE_ZONE_Y and layer > 0 and (progress is None or progress < 100):
            return "FILAMENT SWAP"
        if sub in (1045, 1096, 1405, 1906):
            return "HEATING"
        if layer == 0:
            return "PREPARING"
        return "PRINTING"
    if st in (3, 4):
        return "FILAMENT SWAP"
    if st == 5:
        return "LEVELING"
    if st == 6:
        return "ERROR"
    return f"CODE {st}/{sub}"


ACTIVE_STATES = {"HOMING", "LEVELING", "HEATING", "PREPARING", "PRINTING", "PAUSING", "PAUSED",
                 "RESUMING", "FILAMENT SWAP"}


class CC2:
    def __init__(self, host: str, access_code: str, serial: str = "",
                 on_update: Callable[[], None] | None = None) -> None:
        self.host = host
        self.access_code = access_code
        self.serial = serial
        self.on_update = on_update
        self.client_id = f"1_PC_{random.randint(10**12, 10**14)}"
        self.request_prefix = f"{self.client_id}_req"
        self.raw: dict[str, Any] = {}
        self.canvas: dict[str, Any] = {}
        self.attrs: dict[str, Any] = {}
        self.total_layers: dict[str, int] = {}
        self.connected = False
        self.registered = threading.Event()
        self._reg_t = 0.0  # when we last asked the printer to accept us
        self.last_error = ""
        self.last_update = 0.0
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, list] = {}
        self._last_req_t = 0.0
        self._stop = threading.Event()
        self._mqtt: mqtt.Client | None = None
        self.sleeping = False  # 11/13: app 'Sleep' = light, heaters and camera off until 'Wake'

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self.serial:
            self.serial = discover(self.host).get("sn", "")
            if not self.serial:
                raise RuntimeError(f"printer at {self.host} did not report a serial number")
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id=self.client_id, protocol=mqtt.MQTTv311)
        c.username_pw_set("elegoo", self.access_code)
        c.reconnect_delay_set(2, 60)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._mqtt = c
        c.connect_async(self.host, MQTT_PORT, keepalive=60)
        c.loop_start()
        threading.Thread(target=self._housekeeping, name="cc2-housekeeping", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        if self._mqtt:
            self._mqtt.disconnect()
            self._mqtt.loop_stop()

    # ---- public API ------------------------------------------------------
    def request(self, method: int, params: dict | None = None, timeout: float = 10.0) -> dict | None:
        """Blocking request; returns the result dict or None. Never call from the MQTT thread."""
        if not self._mqtt or not self.registered.is_set():
            return None
        with self._req_lock:
            wait = REQUEST_SPACING_S - (time.monotonic() - self._last_req_t)
            if wait > 0:
                time.sleep(wait)
            self._req_id += 1
            rid = self._req_id
            slot = [threading.Event(), None]
            self._pending[rid] = slot
            self._mqtt.publish(f"elegoo/{self.serial}/{self.client_id}/api_request",
                               json.dumps({"id": rid, "method": method, "params": params or {}}))
            self._last_req_t = time.monotonic()
        ok = slot[0].wait(timeout)
        self._pending.pop(rid, None)
        if not ok:
            log.debug("method %s timed out", method)
            return None
        result = slot[1] or {}
        if result.get("error_code", 0) != 0:
            log.warning("method %s error_code=%s", method, result.get("error_code"))
        return result

    def set_light(self, on: bool) -> bool:
        return self.request(1029, {"power": 1 if on else 0}) is not None

    def sleep(self) -> dict:
        """Night mode with the methods the stock firmware has (it has no real standby/screen-off call):
        chamber light off, both heaters to 0, camera stream off. The printer's own screen timeout dims the screen."""
        self.sleeping = True
        out = {"light": self.set_light(False),
               "heaters": self.request(1028, {"extruder": 0, "heater_bed": 0}, timeout=10) is not None,
               "camera": self.request(1042, {"enable": False}, timeout=4) is not None}
        return out

    def wake(self) -> dict:
        self.sleeping = False
        return {"light": self.set_light(True),
                "camera": self.request(1042, {"enable": True}, timeout=4) is not None}

    # ---- print control (method ids from Elegoo's elegoo-link CC2 adapter) --
    def upload(self, path: str, remote_name: str, progress: Callable[[int, int], None] | None = None) -> None:
        """PUT the file to http://<host>/upload in 1 MB Content-Range chunks (X-Token = access code)."""
        import hashlib
        import requests
        data = open(path, "rb").read()
        md5 = hashlib.md5(data).hexdigest()
        total, step = len(data), 1 << 20
        http = requests.Session()
        for off in range(0, total, step):
            chunk = data[off:off + step]
            # the printer rate-limits (HTTP 429 after ~3 quick chunks): back off and resend the same chunk
            for attempt in range(12):
                try:
                    r = http.put(f"http://{self.host}/upload", data=chunk, timeout=60, headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {off}-{off + len(chunk) - 1}/{total}",
                        "X-File-Name": remote_name, "X-File-MD5": md5, "X-Token": self.access_code})
                except requests.RequestException:
                    if attempt == 11:
                        raise
                    time.sleep(2)
                    continue
                if r.status_code not in (429, 503):
                    break
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(30.0, wait or 1.0 + attempt))
            if r.status_code == 401:
                raise RuntimeError("printer rejected the access code (HTTP 401)")
            if r.status_code not in (200, 201, 204):
                raise RuntimeError(f"upload failed at byte {off}: HTTP {r.status_code}")
            try:
                body = r.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("error_code") not in (0, None):
                raise RuntimeError(f"upload rejected: {body}")
            if progress:
                progress(off + len(chunk), total)

    def start_print(self, filename: str, slot_map: list[dict] | None = None,
                    level: bool = False, timelapse: bool = False, plate: str = "A") -> dict | None:
        """slot_map entries: {"t": tool index, "canvas_id": 0, "tray_id": 0-3}."""
        return self.request(1020, {"filename": filename, "storage_media": "local", "config": {
            "delay_video": timelapse, "printer_check": level, "print_layout": plate,
            "bedlevel_force": False, "slot_map": slot_map or []}}, timeout=20)

    def pause(self) -> dict | None:
        return self.request(1021, {}, timeout=20)

    def stop(self) -> dict | None:
        return self.request(1022, {}, timeout=20)

    def resume(self) -> dict | None:
        return self.request(1023, {}, timeout=20)

    def set_speed(self, mode: int) -> dict | None:
        return self.request(1031, {"mode": int(mode)})

    def refresh_canvas(self) -> None:
        """Ask for the Canvas state again now (tray names/colours, loads) instead of waiting for housekeeping."""
        res = self.request(2005, {}, timeout=6)
        if res:
            with self._lock:
                self.canvas = res
            self._notify()

    def set_auto_refill(self, on: bool) -> dict | None:
        return self.request(2004, {"auto_refill": bool(on)})

    def history(self) -> list[dict]:
        res = self.request(1036, {}, timeout=10) or {}
        return list(reversed(res.get("history_task_list") or []))  # newest first

    def files(self) -> list[dict]:
        res = self.request(1044, {"storage_media": "local", "offset": 0, "limit": 200}, timeout=10) or {}
        return res.get("file_list") or []

    def snapshot(self) -> dict[str, Any]:
        """A flat, display-ready view of the printer state."""
        with self._lock:
            raw = json.loads(json.dumps(self.raw)) if self.raw else {}
            canvas = json.loads(json.dumps(self.canvas)) if self.canvas else {}
        ms = raw.get("machine_status", {})
        ps = raw.get("print_status", {})
        gm = raw.get("gcode_move", {})
        ext = raw.get("extruder", {})
        bed = raw.get("heater_bed", {})
        box = raw.get("ztemperature_sensor", {})
        filename = ps.get("filename") or ""
        trays = []
        ci = canvas.get("canvas_info", canvas)
        active_tray = ci.get("active_tray_id", -1) if isinstance(ci, dict) else -1
        for unit in (ci.get("canvas_list") or []) if isinstance(ci, dict) else []:
            for t in unit.get("tray_list") or []:
                trays.append({
                    "id": t.get("tray_id", 0),
                    "color": t.get("filament_color") or "",
                    "type": t.get("filament_type") or "",
                    "name": t.get("filament_name") or "",
                    "loaded": t.get("status", 0) in (1, 2),  # 1 = loaded, 2 = feeding the nozzle
                    "feeding": t.get("status", 0) == 2,
                    "active": t.get("tray_id") == active_tray,
                })
        return {
            "online": self.connected and bool(raw),
            "error": self.last_error,
            "state": state_label(raw) if raw else "OFFLINE",
            "filename": filename,
            "progress": ms.get("progress", 0) or 0,
            "layer": ps.get("current_layer") or 0,
            "total_layers": self.total_layers.get(filename, 0),
            "elapsed_s": ps.get("print_duration") or 0,
            "remaining_s": ps.get("remaining_time_sec") or 0,
            "nozzle": ext.get("temperature") or 0, "nozzle_target": ext.get("target") or 0,
            "bed": bed.get("temperature") or 0, "bed_target": bed.get("target") or 0,
            "chamber": box.get("temperature") or 0,
            "head_speed": (gm.get("speed") or 0) / 60.0,
            # 11/12: live bed view on the phone (printer pushes gcode_move about once a second)
            "pos": [gm.get("x"), gm.get("y"), gm.get("z")] if gm.get("x") is not None else None,
            "speed_mode": SPEED_MODES.get(gm.get("speed_mode"), ""),
            "light": bool(raw.get("led", {}).get("status", 0)),
            "sleeping": self.sleeping,
            "filament_detected": ext.get("filament_detected"),
            "exceptions": ms.get("exception_status") or [],
            "code": [ms.get("status", 0), ms.get("sub_status", 0)] if ms else [],  # 11/15: raw codes, kept when it stalls
            "trays": trays,
            "auto_refill": bool(ci.get("auto_refill")) if isinstance(ci, dict) else False,
            "updated": self.last_update,
        }

    # ---- MQTT callbacks (paho thread: never block here) -------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0 and str(reason_code) != "Success":
            self.last_error = f"MQTT refused: {reason_code} (check the LAN access code)"
            log.error(self.last_error)
            return
        self.connected = True
        self.last_error = ""
        sn = self.serial
        client.subscribe(f"elegoo/{sn}/{self.request_prefix}/register_response")
        client.subscribe(f"elegoo/{sn}/{self.client_id}/api_response")
        client.subscribe(f"elegoo/{sn}/api_status")
        self._register()
        log.info("MQTT connected to %s, registering", self.host)

    def _register(self) -> None:
        self._reg_t = time.monotonic()
        self._mqtt.publish(f"elegoo/{self.serial}/api_register",
                           json.dumps({"client_id": self.client_id, "request_id": self.request_prefix}))

    def _on_disconnect(self, client, *args, **kwargs) -> None:
        self.connected = False
        self.registered.clear()
        if not self._stop.is_set():
            log.warning("MQTT disconnected (auto-reconnecting)")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        if msg.topic.endswith("/register_response"):
            self.registered.set()
            log.info("registered with printer %s", self.serial)
            return
        method = data.get("method")
        if method in (6000, 6008) and isinstance(data.get("result"), dict):
            with self._lock:
                if self.raw:  # deltas need a full snapshot to merge into
                    _deep_merge(self.raw, data["result"])
                    self.last_update = time.time()
            self._notify()
            return
        rid = data.get("id")
        if msg.topic.endswith(f"{self.client_id}/api_response") and isinstance(rid, int):
            slot = self._pending.get(rid)
            if slot:
                slot[1] = data.get("result", {})
                slot[0].set()

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update()
            except Exception:
                log.exception("on_update failed")

    # ---- background ------------------------------------------------------
    def _housekeeping(self) -> None:
        last_ping = last_full = last_canvas = 0.0
        camera_asked = False
        while not self._stop.is_set():
            if not self.registered.wait(1.0):
                camera_asked = False
                # e.g. after an emergency stop the printer keeps publishing status but ignores registrations
                # until it has been restarted: keep asking rather than wait for a reconnect that never comes
                if self.connected and time.monotonic() - self._reg_t > REGISTER_RETRY_S:
                    log.warning("printer has not accepted the bridge yet; asking again")
                    self._register()
                continue
            now = time.monotonic()
            if now - last_full >= FULL_REFRESH_S or not self.raw:
                res = self.request(1002)
                if res:
                    with self._lock:
                        self.raw = res
                        self.last_update = time.time()
                    self._notify()
                    last_full = now
                if not self.attrs:
                    self.attrs = self.request(1001) or {}
                    if self.attrs:
                        log.info("printer firmware: %s",
                                 self.attrs.get("software_version", {}).get("ota_version", "?"))
            if not camera_asked and not self.sleeping:
                # Ask the printer to start its MJPEG service; older firmware ignores this.
                self.request(1042, {"enable": True}, timeout=4)
                camera_asked = True
            if now - last_canvas >= CANVAS_REFRESH_S:
                res = self.request(2005)
                if res:
                    with self._lock:
                        self.canvas = res
                last_canvas = now
            if now - last_ping >= PING_INTERVAL_S and self._mqtt:
                self._mqtt.publish(f"elegoo/{self.serial}/{self.client_id}/api_request",
                                   '{"type": "PING"}')
                last_ping = now
            self._lookup_total_layers()
            self._stop.wait(1.0)

    def _lookup_total_layers(self) -> None:
        fn = (self.raw.get("print_status") or {}).get("filename") or ""
        if not fn or fn in self.total_layers:
            return
        self.total_layers[fn] = 0  # one attempt per file; 1044 is silent on some firmware
        for storage in ("local", "u-disk"):
            params = {"storage_media": storage, "offset": 0, "limit": 200}
            if storage == "u-disk":
                params["dir"] = "/"
            res = self.request(1044, params, timeout=6)
            for f in (res or {}).get("file_list", []):
                if f.get("filename") == fn and isinstance(f.get("layer"), int):
                    self.total_layers[fn] = f["layer"]
                    return
