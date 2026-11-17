"""MyPrintCloud bridge: sits on the home PC next to the Centauri Carbon 2.

Holds the only connections to the printer (MQTT control + one camera stream) and an
outbound WebSocket to the cloud server, so nothing at home is exposed to the internet.

  python bridge.py           real printer (config.json)
  python bridge.py --mock    simulated printer + camera
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import requests
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import calib  # noqa: E402
import inserts  # noqa: E402
import preview  # noqa: E402
import purge  # noqa: E402
import purge_shapes  # noqa: E402
import slicer  # noqa: E402
import colourcut
import splitter  # noqa: E402
import thumbnail  # noqa: E402
import varlayer  # noqa: E402

log = logging.getLogger("bridge")
STATUS_EVERY_S = 2.0
WORK = os.path.join(HERE, "work")
KEEP_WORK_S = 3 * 86400
MAINT = {  # app command -> (printer method, reply timeout s)
    "set_tray": (2003, 10), "load": (2001, 20), "unload": (2002, 20), "heat": (1028, 10), "home": (1026, 60),
    "level": (1032, 20), "shaper": (1033, 20), "pid": (1034, 20), "estop": (1007, 10)}  # prepared/sliced jobs stay re-printable this long


def load_config(path: str = "") -> dict:
    path = path or os.path.join(HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_path"] = path
    return cfg


def save_config(cfg: dict) -> None:
    path = cfg.get("_path") or os.path.join(HERE, "config.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in cfg.items() if k != "_path"}, f, indent=2)
    os.replace(tmp, path)


def relocate_printer(cfg: dict) -> bool:
    """The saved printer address doesn't answer: look for the printer (by serial) on the LAN and
    save its new address. True when it moved."""
    from cc2 import discover, find_printer
    p = cfg["printer"]
    try:
        discover(p["host"], 2.0)
        return False  # still where we left it
    except Exception:
        pass
    ip = find_printer(p.get("serial", ""))
    if not ip or ip == p["host"]:
        return False
    log.warning("printer moved from %s to %s; saving the new address", p["host"], ip)
    p["host"] = ip
    save_config(cfg)
    return True


class RemotePrinter:
    """The printer as seen from the cloud slicer: status comes from the server (relayed from the home
    bridge), and finished files go to the server and then through the home bridge to the printer."""

    def __init__(self, bridge: "Bridge") -> None:
        self.b = bridge
        self.snap: dict = {}

    def start(self) -> None:
        pass

    def snapshot(self) -> dict:
        return dict(self.snap)

    def upload(self, path: str, remote_name: str, progress=None) -> None:
        tok = uuid.uuid4().hex
        url = self.b.cfg["server"].rstrip("/") + f"/bridge/xfer/{tok}"
        with open(path, "rb") as f:
            r = requests.post(url, data=f, headers={"X-Bridge-Key": self.b.cfg["bridge_key"]}, timeout=(10, 300))
        r.raise_for_status()
        if progress:
            progress(1, 2)
        self.b.rpc("fetch_upload", {"xfer": tok, "remote": remote_name}, timeout=900)
        if progress:
            progress(2, 2)

    def start_print(self, filename: str, slot_map: list[dict] | None = None, level: bool = False, timelapse: bool = False):
        return self.b.rpc("start_raw", {"filename": filename, "slot_map": slot_map, "level": level, "timelapse": timelapse})


class Bridge:
    def __init__(self, cfg: dict, mock: bool, role: str = "home", name: str = "cloud") -> None:
        self.cfg = cfg
        self.role = role
        self.name = name  # slicer role: "cloud" (on the server) or "pc" (the home PC, preferred when on)
        self.rpcs: dict[str, concurrent.futures.Future] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        if role == "slicer":  # cloud slicer: no printer or camera of its own
            self.printer = RemotePrinter(self)
            self.camera = None
        elif mock:
            from mock import MockCamera, MockPrinter
            self.printer = MockPrinter()
            self.camera = MockCamera(self.printer)
        else:
            from camera import Camera
            from cc2 import CC2
            p = cfg["printer"]
            self.printer = CC2(p["host"], p["access_code"], p.get("serial", ""))
            self.camera = Camera(f"http://{p['host']}:8080/")
        self.cam_on = False
        self.cam_fps = float(cfg.get("camera_fps", 12))
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.heavy = asyncio.Lock()  # one download/slice/upload at a time
        self.materials: dict = {}  # app settings, pushed by the server: slot -> real material
        self.calib: dict = {}      # filament preset -> {"filament": {...}, "process": {...}}
        self.last_job: dict = {}
        self.cancelled: set[str] = set()  # jobs cancelled while waiting for the one ahead: skipped when their turn comes
        self.cam_timelapse = True  # app setting: build a timelapse from one camera frame per layer
        self.watch = "alert"       # app setting: failure watch ("off" stops the 20 s frames it needs)
        self._clean_work()

    # ---- plumbing ----------------------------------------------------------
    async def send(self, obj) -> None:
        ws = self.ws
        if ws is None:
            return
        async with self.send_lock:
            try:
                await ws.send(obj if isinstance(obj, bytes) else json.dumps(obj))
            except websockets.ConnectionClosed:
                pass

    def rpc(self, name: str, args: dict, timeout: float = 60):
        """From a worker thread: run a printer command on the home bridge (via the server), wait for the reply."""
        if self.ws is None or self.loop is None:
            raise RuntimeError("Not connected to the server")
        rid = uuid.uuid4().hex[:12]
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.rpcs[rid] = fut
        try:
            asyncio.run_coroutine_threadsafe(self.send({"t": "relay", "rid": rid, "name": name, "args": args}), self.loop)
            res = fut.result(timeout)
        except concurrent.futures.TimeoutError:
            raise RuntimeError("The home bridge didn't answer in time")
        finally:
            self.rpcs.pop(rid, None)
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or f"{name} failed")
        return res.get("data")

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.printer.start()
        if self.camera:
            self.camera.start()
        if self.role != "slicer" and "printer" in self.cfg and not getattr(self, "mock", False):
            asyncio.create_task(self.printer_watch())
        url = self.cfg["server"].rstrip("/").replace("http", "ws", 1) + "/ws/bridge"
        hdr = {"X-Bridge-Key": self.cfg["bridge_key"], "X-Bridge-Role": self.role, "X-Slicer-Name": self.name}
        delay = 2
        while True:
            try:
                async with websockets.connect(url, additional_headers=hdr, max_size=64 << 20,
                                              ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws
                    delay = 2
                    log.info("connected to %s", url)
                    await self.send({"t": "hello", "version": 1, "role": self.role})
                    if self.last_job:
                        await self.send(self.last_job)
                    tasks = [] if self.role == "slicer" else [asyncio.create_task(self.status_loop()),
                                                              asyncio.create_task(self.camera_loop())]
                    if self.role != "slicer" and not getattr(self, "_tl_task", None):
                        self._tl_task = asyncio.create_task(self.timelapse_loop())  # survives reconnects
                    try:
                        async for msg in ws:
                            if isinstance(msg, str):
                                asyncio.create_task(self.handle(json.loads(msg)))
                    finally:
                        for t in tasks:
                            t.cancel()
                        self.ws = None
                        self.cam_on = False
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:  # timeout: slow handshake
                log.warning("server connection failed: %s; retrying in %ss", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def printer_watch(self) -> None:
        """Printer offline for a while: it may have a new address (router lost power). Find it,
        save it and restart, so the printer and camera reconnect to the right place."""
        down_since = None
        while True:
            await asyncio.sleep(30)
            try:
                online = bool(self.printer.snapshot().get("online"))
            except Exception:
                online = False
            if online:
                down_since = None
                continue
            down_since = down_since or time.time()
            if time.time() - down_since >= 60 and await asyncio.to_thread(relocate_printer, self.cfg):
                log.warning("restarting to use the printer's new address")
                os._exit(3)  # run.sh / start_bridge.cmd start us again

    async def status_loop(self) -> None:
        while True:
            s = await asyncio.to_thread(self.printer.snapshot)
            s["camera"] = getattr(self.camera, "status", "")
            try:
                for tr in s.get("trays", []):
                    if tr.get("loaded"):
                        tr["preset"] = self._presets([tr["id"]], self.materials, s)[0]
                s["presets"] = {tr["preset"]: self._base(tr["preset"]) for tr in s.get("trays", []) if tr.get("preset")}
            except Exception:
                log.exception("preset lookup failed")
            await self.send({"t": "status", "s": s})
            await asyncio.sleep(STATUS_EVERY_S)

    async def camera_loop(self) -> None:
        last, pushed = -1, 0.0
        while True:
            await asyncio.sleep(1.0 / self.cam_fps)
            if not self.cam_on:
                # nobody watching: still one frame every 20 s while printing, for the server's failure watch
                if (self.watch != "off" and time.time() - pushed >= 20
                        and self.printer.snapshot().get("state") == "PRINTING"):
                    fid, jpeg = self.camera.latest()
                    if jpeg and not getattr(self.camera, "stalled", False):
                        pushed = time.time()
                        await self.send(jpeg)
                continue
            fid, jpeg = self.camera.latest()
            if jpeg and fid != last:
                last = fid
                await self.send(jpeg)

    # ---- commands from the server -----------------------------------------
    async def handle(self, m: dict) -> None:
        t = m.get("t")
        if t == "reply" and m.get("rid") in self.rpcs:  # answer to a relayed printer command (slicer role)
            fut = self.rpcs[m["rid"]]
            if not fut.done():
                fut.set_result(m)
        elif t == "status" and self.role == "slicer":
            self.printer.snap = m.get("s") or {}
        elif t == "cam":
            self.cam_on = bool(m.get("on"))
        elif t == "cmd":
            try:
                data = await asyncio.to_thread(self.command, m["name"], m.get("args") or {})
                await self.send({"t": "reply", "rid": m.get("rid"), "ok": True, "data": data})
            except Exception as e:  # report every failure back to the phone
                log.exception("command %s failed", m.get("name"))
                await self.send({"t": "reply", "rid": m.get("rid"), "ok": False, "error": str(e)})
        elif t == "settings":
            self.materials, self.calib = m.get("materials") or {}, m.get("calib") or {}
            self.cam_timelapse, self.watch = bool(m.get("cam_timelapse", True)), m.get("watch") or "alert"
            self._bases = {}
        elif t == "job":
            asyncio.create_task(self.run_job(m))
        elif t == "calib_job":
            asyncio.create_task(self.calib_job(m))
        elif t == "job_slice":
            asyncio.create_task(self.slice_job(m))
        elif t == "job_send":
            asyncio.create_task(self.send_job(m))
        elif t == "job_cancel":
            self.cancelled.add(self._jid(m["id"]))
            shutil.rmtree(os.path.join(WORK, self._jid(m["id"])), ignore_errors=True)
            await self.job_update(m["id"], "cancelled", 0, "Cancelled")

    def command(self, name: str, a: dict):
        p = self.printer
        simple = {"pause": p.pause, "resume": p.resume, "stop": p.stop,
                  "history": p.history, "files": p.files}
        if name in ("pause", "resume", "stop"):
            res = simple[name]()
            state = (p.snapshot() or {}).get("state", "")
            if res is None:  # no reply at all: the CC2 ignores app commands while it's preparing
                raise RuntimeError(f"The printer didn't answer the {name}"
                                   + (f" (it's {state.lower()}: it ignores the app until that finishes)" if state else "")
                                   + ". Use the printer's own screen, or try again in a moment.")
            if res.get("error_code", 0) != 0:
                raise RuntimeError(f"The printer refused the {name} (error {res['error_code']})"
                                   + (f" while {state.lower()}" if state else ""))
            return res
        if name in simple:
            return simple[name]()
        if name == "light":
            return p.set_light(bool(a.get("on")))
        if name == "sleep":
            state = (p.snapshot() or {}).get("state", "")
            if state not in ("IDLE", "COMPLETE", "STOPPED", "ERROR", ""):
                raise RuntimeError(f"Not while the printer is {state.lower()}")
            return p.sleep()
        if name == "wake":
            return p.wake()
        if name == "speed":
            return p.set_speed(int(a["mode"]))
        if name == "auto_refill":
            return p.set_auto_refill(bool(a.get("on")))
        if name in MAINT:
            return self._maint(name, a)
        if name == "print_file":
            trays = [int(t) for t in (a.get("trays") or [])][:4] or None  # one Canvas slot per colour in the file
            return self._start(a["filename"], trays, len(trays) if trays else 1, a)
        if name == "fetch_upload":  # a file the cloud slicer made: server -> here -> printer
            if not re.fullmatch(r"[0-9a-f]{32}", str(a.get("xfer"))):
                raise ValueError("bad transfer id")
            remote = re.sub(r"[^\w.\- ()]+", "_", str(a["remote"]))[:120]
            os.makedirs(WORK, exist_ok=True)
            tmp = os.path.join(WORK, f"xfer_{a['xfer']}.gcode")
            try:
                self._download(self.cfg["server"].rstrip("/") + f"/bridge/xfer/{a['xfer']}", tmp)
                p.upload(tmp, remote)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return {"remote": remote}
        if name == "restart":  # the app's "Restart bridge": exit after replying, the run loop starts us again
            threading.Timer(1.0, os._exit, (3,)).start()
            return {"restarting": True}
        if name == "start_raw":
            return p.start_print(str(a["filename"]), a.get("slot_map"), level=bool(a.get("level")),
                                 timelapse=bool(a.get("timelapse")))
        raise ValueError(f"unknown command {name}")

    def _maint(self, name: str, a: dict):
        """Canvas + maintenance methods of the stock firmware (ids as Elegoo's own web UI uses them)."""
        state = self.printer.snapshot().get("state", "")
        busy = state not in ("IDLE", "COMPLETE", "STOPPED", "ERROR", "")
        if busy and name not in ("estop", "set_tray"):  # incl. heat: cooling down mid-print would ruin it
            raise RuntimeError(f"Not while the printer is {state.lower()}")
        method, timeout = MAINT[name]
        if name in ("load", "unload"):
            params = {"canvas_id": 0, "tray_id": int(a["tray"])}
        elif name == "set_tray":
            params = {"canvas_id": 0, "tray_id": int(a["tray"]), "brand": str(a.get("brand") or "Generic")[:20],
                      "filament_type": str(a["type"])[:12], "filament_name": str(a["name"])[:30],
                      "filament_code": str(a["code"])[:8], "filament_color": "#" + str(a["color"]).lstrip("#").upper()[:6],
                      "filament_min_temp": int(a["min"]), "filament_max_temp": int(a["max"])}
        elif name == "heat":
            params = {}
            if a.get("nozzle") is not None:
                params["extruder"] = max(0, min(300, int(a["nozzle"])))
            if a.get("bed") is not None:
                params["heater_bed"] = max(0, min(110, int(a["bed"])))
        elif name == "home":
            params = {"homed_axes": "xyz"}
        else:
            params = {}
        res = self.printer.request(method, params, timeout=timeout)
        if res is None:
            raise RuntimeError("The printer didn't accept that")
        if name in ("set_tray", "load", "unload"):
            self.printer.refresh_canvas()
        return res

    def _start(self, filename: str, trays: list[int] | None, n_tools: int, opts: dict) -> dict:
        trays = trays or self.default_trays(n_tools)
        slot_map = [{"t": i, "canvas_id": 0, "tray_id": int(tr)} for i, tr in enumerate(trays[:n_tools])]
        res = self.printer.start_print(filename, slot_map, level=bool(opts.get("level", False)),
                                       timelapse=bool(opts.get("timelapse", False)))
        if res is None:
            if not self.printer.registered.is_set():
                raise RuntimeError("The printer isn't taking commands right now (this happens after an emergency stop). "
                                   "Turn it off and on again, then retry.")
            raise RuntimeError("printer did not answer the start command")
        if res.get("error_code", 0) != 0:
            code = res["error_code"]
            raise RuntimeError({1009: "printer is busy", 1003: "printer rejected the parameters"}.get(code, f"printer error {code}"))
        return res

    def default_trays(self, n: int) -> list[int]:
        trays = [t for t in self.printer.snapshot().get("trays", []) if t.get("loaded")]
        if not trays:
            return list(range(n))
        active = [t["id"] for t in trays if t.get("active")]
        order = active + [t["id"] for t in trays if t["id"] not in active]
        return (order * n)[:n]

    # ---- print jobs: download -> slice -> upload -> start ------------------
    async def job_update(self, jid: str, stage: str, pct: float = 0, msg: str = "", **extra) -> None:
        self.last_job = {"t": "job", "id": jid, "stage": stage, "pct": round(pct, 1), "msg": msg, **extra}
        await self.send(self.last_job)

    async def run_job(self, job: dict) -> None:
        jid = self._jid(job["id"])
        wd = os.path.join(WORK, jid)
        os.makedirs(wd, exist_ok=True)
        keep = False
        if self.heavy.locked():
            await self.job_update(jid, "queued", 0, "Waiting for the job ahead of it")
        async with self.heavy:
            if jid in self.cancelled:
                return
            try:
                keep = await self._run_job(job, jid, wd)
            except Exception as e:
                log.exception("job %s failed", jid)
                await self.job_update(jid, "failed", 0, str(e)[:400])
            finally:
                if not keep:
                    shutil.rmtree(wd, ignore_errors=True)

    async def _run_job(self, job: dict, jid: str, wd: str) -> bool:
        """Download, then either measure for the prepare screen (returns True: keep the folder)
        or slice -> upload -> start in one go."""
        if True:
            await self.job_update(jid, "downloading", 0, "Fetching the model")
            name = re.sub(r"[^\w.\- ]+", "_", job["filename"])[:120] or "model.stl"
            src = os.path.join(wd, name)
            url = job["url"] if job["url"].startswith("http") else self.cfg["server"].rstrip("/") + job["url"]
            await asyncio.to_thread(self._download, url, src)
            if job.get("mode") == "prepare" and not name.lower().endswith(".gcode"):
                await self.job_update(jid, "analyzing", 0, "Measuring the model")
                fplates = slicer.file_plates(src)
                if fplates:  # laid out on several plates by its designer: show plate 1, print them as laid out
                    first = os.path.join(wd, "plate1_preview.stl")
                    await asyncio.to_thread(self._plate_stl, src, fplates[0], first)
                    info = await asyncio.to_thread(preview.light_mesh, first, os.path.join(wd, "model.stl"))
                    names = slicer.object_names(src)
                    fp_info = []
                    for p in fplates:  # each part as its own light mesh, so the app can show / remove / move them
                        parts, files = await asyncio.to_thread(self._plate_parts, src, p, names, os.path.join(wd, "fparts"))
                        for k, f in enumerate(files):
                            await asyncio.to_thread(self._put_asset, jid, f"fo{p['n']}_{k}.stl", f)
                        fp_info.append({"n": p["n"], "name": p["name"], "objects": len(p["objects"]), "parts": parts})
                    info.update(fits=True, auto_cuts=[], colors=1, file_plates=fp_info)
                    try:  # a multi-colour file laid out on several plates: say how many colours it really uses
                        import combine
                        ids = combine.used_fast(src)
                        if len(ids) > 1:
                            fc = combine.file_colours(src)
                            info.update(colors=len(ids), filament_ids=ids, palette=[fc.get(e) or "" for e in ids])
                    except Exception:
                        log.exception("colour scan failed")
                    for p in fplates:
                        png = slicer.plate_png(src, p["n"])
                        if png:
                            pp = os.path.join(wd, f"fplate{p['n']}.png")
                            with open(pp, "wb") as fh:
                                fh.write(png)
                            await asyncio.to_thread(self._put_asset, jid, f"fplate{p['n']}.png", pp)
                else:
                    info = await asyncio.to_thread(preview.light_mesh, src, os.path.join(wd, "model.stl"))
                    info["colors"] = slicer.model_filament_count(src)
                    info["painted"] = slicer._is_painted(src)
                    try:  # the colours the model really uses (painted ones too), with the designer's swatches
                        import combine
                        ids = combine.used_fast(src)
                        if src.lower().endswith(".3mf"):
                            fc = combine.file_colours(src)
                            info.update(colors=len(ids), filament_ids=ids, palette=[fc.get(e) or "" for e in ids])
                    except Exception:
                        log.exception("colour scan failed")
                info["author"] = slicer.author_settings(src)
                await asyncio.to_thread(self._put_asset, jid, "model.stl", os.path.join(wd, "model.stl"))
                self._save_ctx(jid, {"job": job, "src": src, "name": name, "info": info})
                await self.job_update(jid, "prepare", 100, "Ready to prepare", info=info)
                return True
            trays = job.get("trays")
            if name.lower().endswith(".gcode"):
                gcode, n_tools, stats = src, max(1, len(trays or [0])), slicer.gcode_stats(src)
            else:
                n_tools = len(trays) if trays else slicer.model_filament_count(src)
                trays = trays or self.default_trays(n_tools)
                snap = {t["id"]: t for t in self.printer.snapshot().get("trays", [])}
                presets = self._presets(trays, job.get("materials"))
                await self.job_update(jid, "slicing", 0, f"Slicing ({job.get('quality', 'standard')}, "
                                      + ", ".join(p.replace(" @ECC2", "") for p in presets) + ")")
                fil, proc = self._calibrated(presets, job.get("settings") or {}, job.get("calib"))
                gcode = await asyncio.to_thread(slicer.slice_model, src, wd, presets,
                                                job.get("quality", "standard"), int(job.get("plate", 1)),
                                                proc, filament_overrides=fil, arrange=None)
                stats = slicer.gcode_stats(gcode)
                try:  # preview picture for the printer's screen (the CLI doesn't make one)
                    png = await asyncio.to_thread(thumbnail.render, src, snap.get(trays[0], {}).get("color") or "#FF7A1A")
                    await asyncio.to_thread(thumbnail.embed, gcode, png)
                except Exception:
                    log.exception("thumbnail failed (printing without one)")
            remote = os.path.splitext(name)[0][:60] + ".gcode"
            await self.job_update(jid, "uploading", 0, "Sending to the printer", stats=stats,
                                  presets=presets if not name.lower().endswith(".gcode") else [])
            loop = asyncio.get_running_loop()

            def prog(done, total):
                asyncio.run_coroutine_threadsafe(
                    self.job_update(jid, "uploading", 100 * done / total, "Sending to the printer", stats=stats), loop)
            await asyncio.to_thread(self.printer.upload, gcode, remote, prog)
            if job.get("start", True):
                await self.job_update(jid, "starting", 100, "Starting the print", stats=stats)
                await asyncio.to_thread(self._start, remote, trays, n_tools, job)
                await self.job_update(jid, "printing", 100, "Printing", stats=stats, remote=remote,
                                      trays=list(trays or self.default_trays(n_tools))[:n_tools])
            else:
                await self.job_update(jid, "ready", 100, "On the printer, not started", stats=stats, remote=remote)
            return False

    # ---- prepare -> slice into plates -> review -> send ---------------------
    @staticmethod
    def _jid(jid: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{12}", str(jid)):
            raise ValueError("bad job id")
        return jid

    def _save_ctx(self, jid: str, ctx: dict) -> None:
        with open(os.path.join(WORK, jid, "ctx.json"), "w", encoding="utf-8") as f:
            json.dump(ctx, f)

    def _combo_plates(self, items: list[dict], m: dict, ctx: dict, wd: str) -> dict | None:
        """Every item keeps its own colours (its own Canvas slots) on a shared plate: either all printed
        together (colours swap each layer, shared by all) or, with together="blocks", one model at a time
        (one colour change per model). None = nothing to gain (falls back to the one-colour path)."""
        import combine
        import combine_build
        import trimesh
        jid = self._jid(m["id"])
        full = []
        for it in items:
            ictx = ctx if it["job"] == jid else self._ctx_for(it["job"], it.get("filename"))
            objs = combine.read(ictx["src"])
            sc = max(0.05, min(10.0, float(it.get("scale") or 1)))
            if sc != 1.0:
                for o in objs:
                    for p in o["parts"]:
                        p["mesh"].apply_scale(sc)
            lo, hi = combine.footprint(objs)
            if any(max(p["mesh"].bounds[1][a] - p["mesh"].bounds[0][a] for p in o["parts"]) > combine.BED[a] - 2 * combine.MARGIN
                   for o in objs for a in (0, 1)) or hi[2] - lo[2] > 250:
                return None  # something needs cutting first: the old path handles that
            full.append({"name": os.path.splitext(ictx["name"])[0][:40], "objects": objs, "copies": it["copies"],
                         "slots": it.get("trays") or None, "settings": slicer.author_layout_settings(ictx["src"])})
        snap_trays = self.printer.snapshot().get("trays", [])
        share_notes = []
        for it, src_it in zip(full, items):
            ictx = ctx if src_it["job"] == jid else self._ctx_for(src_it["job"], src_it.get("filename"))
            pal = (ictx.get("info") or {}).get("palette") or []
            ids = combine.extruders_used(it["objects"])
            own = list(it["slots"] or [])
            if len(own) < len(ids):  # colours without a chosen slot: the loaded slot closest in colour
                rest = pal[len(own):len(ids)] if len(pal) >= len(ids) else []
                ref = own[0] if own else next((t["id"] for t in snap_trays if t.get("active")), None)
                kind = next((t.get("type") for t in snap_trays if t["id"] == ref), None) if ref is not None else None
                own += (combine.nearest_slots(rest, snap_trays, kind) if rest
                        else [own[0] if own else (items[0].get("trays") or [0])[0]] * (len(ids) - len(own)))
            it["slots"] = own
            dup = [s for s in set(own[:len(ids)]) if own[:len(ids)].count(s) > 1]
            if dup:
                share_notes.append(f"{it['name']} uses {len(ids)} colours: " + "; ".join(
                    f"colours {' + '.join(str(i + 1) for i, s in enumerate(own[:len(ids)]) if s == d)} share slot {d + 1}" for d in dup))
        mode = m.get("together") or "layers"
        pl = combine_build.plan_blocks(full) if mode == "blocks" else combine_build.plan(full)
        snap = {t["id"]: t for t in self.printer.snapshot().get("trays", [])}
        colours = [snap.get(s, {}).get("color") or "#FF7A1A" for s in pl["slots"]]
        presets = self._presets(pl["slots"], m.get("materials"))
        groups, thumbs = [], []
        for k, placed in enumerate(pl["plates"]):
            out = os.path.join(wd, f"combo{k + 1}.3mf")
            combine_build.build_plate(placed, os.path.join(wd, f"combo{k + 1}"), out, presets, colours)
            groups.append([out])
            th = os.path.join(wd, f"combo{k + 1}_thumb.stl")
            meshes = []
            for ob in placed:
                for p in ob["parts"]:
                    mm = p["mesh"].copy()
                    mm.apply_translation([ob["at"][0], ob["at"][1], 0])
                    meshes.append(mm)
            trimesh.util.concatenate(meshes).export(th)
            thumbs.append(th)
        return {"groups": groups, "thumbs": thumbs, "slots": pl["slots"], "notes": share_notes + pl["notes"],
                "process": pl.get("process") or {}}

    def _ctx_for(self, jid: str, filename: str | None) -> dict:
        """Another model added to this print: its prepared context, or (prepared on the other worker,
        e.g. the PC vs the cloud) fetched from the server and measured here."""
        try:
            return self._load_ctx(jid)
        except RuntimeError:
            if not filename:
                raise
        import combine
        wd = os.path.join(WORK, jid)
        os.makedirs(wd, exist_ok=True)
        name = re.sub(r"[^\w.\- ]+", "_", filename)[:120] or "model.stl"
        src = os.path.join(wd, name)
        self._download(self.cfg["server"].rstrip("/") + f"/bridge/file/{jid}", src)
        info = preview.light_mesh(src, os.path.join(wd, "model.stl"))
        info["colors"] = slicer.model_filament_count(src)
        info["painted"] = slicer._is_painted(src)
        if src.lower().endswith(".3mf"):
            ids = combine.used_fast(src)
            fc = combine.file_colours(src)
            info.update(colors=len(ids), filament_ids=ids, palette=[fc.get(e) or "" for e in ids])
        info["author"] = slicer.author_settings(src)
        c = {"job": {"id": jid, "filename": filename}, "src": src, "name": name, "info": info}
        self._save_ctx(jid, c)
        log.info("fetched %s (%s) for a combined print: it was prepared on the other worker", jid, name)
        return c

    def _load_ctx(self, jid: str) -> dict:
        try:
            with open(os.path.join(WORK, jid, "ctx.json"), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            where = "the PC" if self.role != "slicer" or self.name == "pc" else "the cloud slicer"
            raise RuntimeError(f"This job's files are gone from {where} (older than 3 days?). Send the model again.")

    def _base(self, preset: str) -> dict:
        """The filament's current values (stock preset + its calibration), cached until settings change."""
        if not hasattr(self, "_bases"):
            self._bases = {}
        if preset not in self._bases:
            v = calib.base_values(preset, self.calib.get(preset))
            self._bases[preset] = {k: v[k] for k in ("nozzle_temperature", "filament_flow_ratio", "pressure_advance",
                                                     "filament_retraction_length", "elefant_foot_compensation",
                                                     "xy_contour_compensation")}
        return self._bases[preset]

    def _calibrated(self, presets: list[str], settings: dict, cal: dict | None) -> tuple[list[dict], dict]:
        """Per-filament overrides + process settings with the first filament's calibrated values on top."""
        cal = cal if cal is not None else self.calib
        fil = [dict((cal.get(p) or {}).get("filament") or {}) for p in presets]
        proc = {**(settings or {}), **((cal.get(presets[0]) or {}).get("process") or {})} if presets else dict(settings or {})
        return fil, proc

    def _presets(self, trays: list[int], mats: dict | None, snapshot: dict | None = None) -> list[str]:
        snap = {t["id"]: t for t in (snapshot or self.printer.snapshot()).get("trays", [])}
        mats = mats or {}  # the app's "what's really in this slot" setting (the Canvas only says "PLA")
        return [slicer.filament_preset(mats.get(str(t)) or snap.get(t, {}).get("type", "PLA"),
                                       "" if mats.get(str(t)) else snap.get(t, {}).get("name", "")) for t in trays]

    async def slice_job(self, m: dict) -> None:
        jid = self._jid(m["id"])
        if self.heavy.locked():
            await self.job_update(jid, "queued", 0, "Waiting for the job ahead of it")
        async with self.heavy:
            if jid in self.cancelled:
                return
            try:
                ctx = self._load_ctx(jid)
                wd, src, info = os.path.join(WORK, jid), ctx["src"], ctx["info"]
                scale = max(0.05, min(10.0, float(m.get("scale") or 1)))
                cuts = m.get("cuts")  # None = cut automatically if it doesn't fit, [] = never cut
                trays = [int(t) for t in (m.get("trays") or [])]
                n_tools = len(trays) or int(info.get("colors") or 1)
                ids0 = info.get("filament_ids") or []
                if not trays and ids0 and (ids0 != list(range(1, len(ids0) + 1)) or len(ids0) > 4):
                    trays = []  # matched by colour later (_combo_plates)
                else:
                    trays = trays or self.default_trays(n_tools)
                notes: list[str] = []
                # this model (+ copies) and any other prepared models added to the same print
                items = [{"job": jid, "scale": scale, "cuts": cuts, "copies": m.get("copies"), "trays": trays}]
                items += [{"job": self._jid(x["job"]), "scale": x.get("scale"), "cuts": None, "copies": x.get("copies"),
                           "trays": x.get("trays"), "filename": x.get("filename")}
                          for x in (m.get("extras") or [])[:8] if x.get("job") != jid]
                for it in items:
                    it["copies"] = max(1, min(50, int(it.get("copies") or 1)))
                style = m.get("style") if m.get("style") in ("hollow", "vase") else None
                ins = m.get("inserts") or {}
                pause_heights: list[tuple[float, str]] = [(float(h), f"{float(h):g} mm") for h in (ins.get("heights") or [])]
                if ins.get("auto", True) and style != "vase" and not any(it.get("cuts") for it in items):
                    for it in items:  # models keep their orientation (orient=False), so pocket heights hold on the bed
                        try:
                            ii = info if it["job"] == jid else self._load_ctx(it["job"])["info"]
                        except Exception:
                            continue
                        sc = float(it.get("scale") or 1)
                        for p in ii.get("pockets") or []:
                            pause_heights.append((round(p["top"] * sc, 3), inserts.describe(p)))
                variable = m.get("variable") if m.get("variable") in varlayer.LEVELS and style != "vase" else None
                if variable and n_tools > 1:  # variable layers re-wrap the model and would drop its colour layout
                    variable = None
                    notes.append("Variable layers skipped: they'd turn this multi-colour model into one colour")
                simple = len(items) == 1 and items[0]["copies"] == 1 and not variable
                units: list[tuple[str, list]] = []  # (STL with its own name, footprint) per piece to place
                fplates = slicer.file_plates(src) if simple and scale == 1.0 and not cuts else []
                plate_names: list[str] = []
                thumbs: list[str] = []  # whole-plate STLs for the printer-screen picture
                combo = None
                ids = info.get("filament_ids") or []
                remap = bool(ids) and (ids != list(range(1, len(ids) + 1)) or len(ids) > 4)  # e.g. a 5-colour file
                if len(ids) > 1 and info.get("file_plates") and not m.get("fplan"):
                    remap = True  # the file's own plates would each print in one colour: lay it out keeping colours
                if (not simple or remap) and not variable and not style and not cuts and not (m.get("purge") or {}).get("design"):
                    await self.job_update(jid, "splitting", 0, "Laying out the models, each in its own colours")
                    try:
                        combo = await asyncio.to_thread(self._combo_plates, items, m, ctx, wd)
                    except ValueError as e:  # e.g. more than 4 slots / blocks that don't fit: say so, don't guess
                        raise RuntimeError(str(e))
                    except Exception:
                        log.exception("combined plate failed; falling back to one colour")
                        notes.append("Couldn't keep each model's colours on a shared plate, so it's one colour")
                if not combo and not trays:
                    trays = self.default_trays(n_tools)
                if combo:
                    fplates = []
                    groups, thumbs, trays = combo["groups"], combo["thumbs"], combo["slots"]
                    n_tools = len(trays)
                    notes += combo["notes"]
                    if info.get("file_plates") and len(ids) > 1 and simple:
                        notes.insert(0, f"This file came on {len(info['file_plates'])} plates; they were laid out again so "
                                        "every part keeps its colours (the designer's plates would each print in one colour)")
                    combo_proc = combo["process"]
                elif fplates:  # the designer's own plates, one at a time (one colour each)
                    await self.job_update(jid, "splitting", 0, f"Reading the file's {len(fplates)} plates")
                    if m.get("fplan"):  # the app removed parts or moved them between plates
                        known = {o for p in fplates for o in p["objects"]}
                        orig = {p["n"]: set(p["objects"]) for p in fplates}
                        fplates = [{"n": i + 1, "name": str(p.get("name") or ""),
                                    "objects": [str(o) for o in p["objects"] if str(o) in known],
                                    "added": bool(set(map(str, p["objects"])) - orig.get(p.get("from"), set()))}
                                   for i, p in enumerate(m["fplan"])]
                        fplates = [p for p in fplates if p["objects"]]
                        for i, p in enumerate(fplates):
                            p["n"] = i + 1
                        if not fplates:
                            raise RuntimeError("Every part was removed, so there's nothing to print")
                    groups = []
                    for p in fplates:
                        pdir = os.path.join(wd, "fplates", str(p["n"]))
                        shutil.rmtree(pdir, ignore_errors=True)
                        groups.append(await asyncio.to_thread(self._plate_stl, src, p, None, pdir))
                        plate_names.append(p["name"])
                        thumbs.append(os.path.join(wd, "fplates", f"thumb{p['n']}.stl"))
                        await asyncio.to_thread(self._plate_stl, src, p, thumbs[-1])
                    if n_tools > 1:
                        n_tools, trays = 1, trays[:1]
                    notes.append(f"Printing the file's own {len(fplates)} plates as the designer laid them out, "
                                 "one colour each" if not m.get("fplan") else
                                 f"Printing {len(fplates)} plates with your changes to the file's own plates, one colour each")
                elif simple and scale == 1.0 and not cuts and (info["fits"] or cuts == []):
                    groups = [[src]]
                elif (m.get("keep_colours", True) and len(items) == 1 and n_tools > 1 and not variable and not style
                      and not (m.get("purge") or {}).get("design") and slicer._is_painted(src)
                      and not slicer.file_plates(src)):
                    # "Keep colours": scale / cut the painted model and keep its paint on every part
                    await self.job_update(jid, "splitting", 0, "Scaling / cutting, keeping the colours")
                    it = items[0]
                    pdir = os.path.join(wd, "colourparts")
                    shutil.rmtree(pdir, ignore_errors=True)
                    res = await asyncio.to_thread(colourcut.prepare_painted, src, pdir,
                                                  max(0.05, min(10.0, float(it.get("scale") or 1))), it["cuts"],
                                                  bool(m.get("pins", True)), int(it.get("copies") or 1))
                    groups = [[pth] for pth in res["plates"]]
                    notes += res["notes"]
                else:
                    groups = None  # filled plate by plate below, checking what the slicer really placed
                    await self.job_update(jid, "splitting", 0, "Scaling / cutting / arranging")
                    shutil.rmtree(os.path.join(wd, "parts"), ignore_errors=True)
                    shutil.rmtree(os.path.join(wd, "units"), ignore_errors=True)
                    os.makedirs(os.path.join(wd, "units"))
                    for k, it in enumerate(items):
                        ictx = ctx if it["job"] == jid else self._ctx_for(it["job"], it.get("filename"))
                        isc = max(0.05, min(10.0, float(it.get("scale") or 1)))
                        icuts = it["cuts"]
                        iname = os.path.splitext(ictx["name"])[0]
                        whole = isc == 1.0 and not icuts and (ictx["info"]["fits"] or icuts == [])
                        ifp = slicer.file_plates(ictx["src"]) if not icuts else []
                        if ifp:  # a multi-plate 3MF resized/copied: every object of every plate, packed afresh
                            parts = await asyncio.to_thread(self._file_objects, ictx["src"], ifp, isc,
                                                            os.path.join(wd, "parts", str(k)))
                        elif whole and ictx["src"].lower().endswith(".stl"):
                            parts = [(ictx["src"], ictx["info"]["size"][:2])]
                        else:  # 3MF/OBJ or scaled/cut: plain STL parts
                            res = await asyncio.to_thread(splitter.prepare, ictx["src"], os.path.join(wd, "parts", str(k)),
                                                          isc, [] if whole else icuts, True, bool(m.get("pins", True)))
                            parts = [(pt["path"], pt["size"][:2]) for pt in res["parts"]]
                            notes += [(f"{iname}: " if len(items) > 1 else "") + re.sub(r"\((?:\d+ \+ )+\d+; ", "(", n)
                                      for n in res["notes"]]
                        writers: dict = {}
                        for _ in range(it["copies"]):
                            for path, size in parts:
                                u = os.path.join(wd, "units", f"u{len(units):03d}." + ("3mf" if variable else "stl"))  # unique name = traceable in gcode
                                if variable:  # the model wrapped in a 3MF that carries its layer height ranges
                                    if path not in writers:
                                        writers[path] = await asyncio.to_thread(self._var_writer, path, variable)
                                    await asyncio.to_thread(writers[path].save, u, f"u{len(units):03d}")
                                else:
                                    try:
                                        os.link(path, u)
                                    except OSError:
                                        shutil.copy(path, u)
                                units.append((u, size))
                            if len(units) > 200:
                                raise RuntimeError("That's more than 200 pieces; use fewer copies")
                    if n_tools > 1:
                        notes.append("Scaled/cut/combined models lose their colour layout: printing in one colour"
                                     if m.get("keep_colours", True) else "Keep colours is off: printing in one colour")
                        n_tools, trays = 1, trays[:1]
                presets = self._presets(trays, m.get("materials"))
                snap = {t["id"]: t for t in self.printer.snapshot().get("trays", [])}
                purge_names, purge_info = [], None
                pc = m.get("purge") or {}
                if pc.get("design") in purge_shapes.CATALOG and n_tools > 1 and groups == [[src]]:
                    try:
                        groups, purge_names, purge_info = await self._add_purge(jid, wd, src, presets, m, pc)
                    except Exception as e:
                        log.exception("purge object failed")
                        notes.append(f"Purge object skipped: {str(e)[:160]}")
                elif pc.get("design") and n_tools < 2:
                    notes.append("Purge object skipped: this print uses a single colour, so nothing is purged")
                plates = []
                remaining = list(range(len(units)))
                k = 0
                while (groups is not None and k < len(groups)) or (groups is None and remaining):
                    if groups is not None:
                        group, todo = groups[k], None
                    else:
                        guess = splitter.pack_sizes([units[u][1] for u in remaining])
                        # small jobs: hand the slicer all of it (its arrange packs tighter than our guess)
                        todo = list(remaining) if len(remaining) <= 24 else [remaining[x] for x in guess[0]]
                        group = [units[u][0] for u in todo]
                    pdir = os.path.join(wd, f"plate{k + 1}")
                    shutil.rmtree(pdir, ignore_errors=True)
                    left = f", {len(remaining)} pieces to place" if groups is None else ""
                    await self.job_update(jid, "slicing", 0 if groups is None else 100 * k / len(groups),
                                          f"Slicing plate {k + 1}{left} ("
                                          + ", ".join(p.replace(" @ECC2", "") for p in presets) + ")")
                    fil, proc = self._calibrated(presets, m.get("settings") or {}, m.get("calib"))
                    proc = {**proc, "gcode_label_objects": "1", **(combo_proc if combo else {})}
                    proc.update(self._support_overrides(m))
                    if style:  # vases / open boxes: shell only, no infill, no top (see _style_overrides)
                        proc.update(self._style_overrides(style, m))
                    if variable and str(proc.get("support_type", slicer.PROC_OVR["support_type"])).startswith("tree")                             and proc.get("support_style", "default") in ("default", "organic"):
                        proc["support_style"] = varlayer.SUPPORT_STYLE  # the slicer refuses variable layers + organic supports
                        if "Variable layers" not in " ".join(notes):
                            notes.append("Variable layers: tree supports use the slim style (the default style can't be combined with them)")
                    gcode = await asyncio.to_thread(slicer.slice_model, group, pdir, presets,
                                                    m.get("quality", "standard"), 1, proc,
                                                    orient=False, filament_overrides=fil,
                                                    arrange=None if group == [src] or combo or (fplates and not fplates[k].get("added")) else True)
                    if groups is not None and fplates and fplates[k].get("added"):  # parts moved onto this plate
                        with open(gcode, encoding="utf-8", errors="replace") as f:
                            placed_names = set(re.findall(r"^; printing object (o\d{3})", f.read(), re.M))
                        if len(placed_names) < len(group):
                            raise RuntimeError(f"Plate {k + 1} is too full: only {len(placed_names)} of its {len(group)} parts "
                                               "fit. Move some to another plate or a new one.")
                    if todo is not None:  # the slicer silently leaves out what its arrange can't fit
                        with open(gcode, encoding="utf-8", errors="replace") as f:
                            placed_names = set(re.findall(r"^; printing object (u\d{3})", f.read(), re.M))
                        placed = [u for u in todo if os.path.splitext(os.path.basename(units[u][0]))[0] in placed_names]
                        if not placed:
                            raise RuntimeError("A piece doesn't fit on the bed even on its own (try a smaller size or a cut)")
                        remaining = [u for u in remaining if u not in placed]
                        group = [units[u][0] for u in placed]
                    try:  # preview picture for the printer's screen (the CLI doesn't make one)
                        png = await asyncio.to_thread(thumbnail.render, thumbs[k] if k < len(thumbs) else group[0],
                                                      snap.get(trays[0], {}).get("color") or "#FF7A1A")
                        await asyncio.to_thread(thumbnail.embed, gcode, png)
                    except Exception:
                        log.exception("thumbnail failed (printing without one)")
                    purge_stats = None
                    if purge_names:  # route each colour change's purge into the purge object(s)
                        with open(gcode, encoding="utf-8", errors="replace") as f:
                            body = f.read()
                        present = [n for n in purge_names if f"; printing object {n}" in body]
                        title = purge_shapes.CATALOG[pc["design"]]["title"]
                        if present:
                            st = await asyncio.to_thread(purge.postprocess, gcode, present)
                            g, before, after = purge.GRAMS_PER_MM, purge_info["chute_mm"], purge.chute_mm(gcode)
                            purge_stats = {"design": pc["design"], "objects": len(present),
                                           "chute_before_g": round(before * g, 1), "chute_after_g": round(after * g, 1),
                                           "caught_g": round(st["chute_saved_mm"] * g, 1)}
                            notes.insert(0, f"Purge catcher ({title} x{len(present)}): chute waste {before * g:.0f} g -> "
                                            f"{after * g:.0f} g; {st['chute_saved_mm'] * g:.0f} g of the colour-change purge "
                                            f"goes into the {title.lower()} instead. It comes out multicoloured: "
                                            f"{purge_info['height']:.0f} mm tall, about {purge_info['grams']:.0f} g in total.")
                        else:
                            notes.insert(0, f"Purge object ({title}) didn't fit on the bed next to the model, so it was left out")
                    pauses = []
                    if pause_heights:  # magnets / nuts dropped in mid-print: M600 before the layer that closes the pocket
                        pauses = await asyncio.to_thread(inserts.add_pauses, gcode, [h for h, _ in pause_heights],
                                                         [t for _, t in pause_heights])
                    tp = os.path.join(pdir, "plate.tp.gz")
                    await asyncio.to_thread(preview.encode_toolpaths, gcode, tp)
                    await asyncio.to_thread(self._put_asset, jid, f"plate{k + 1}.tp.gz", tp)
                    pstats = slicer.gcode_stats(gcode)
                    if purge_stats:
                        pstats["purge"] = purge_stats
                    plates.append({"n": k + 1, "parts": len(group), "stats": pstats, "gcode": gcode, "sent": "", "pauses": pauses,
                                   "name": plate_names[k] if k < len(plate_names) else ""})
                    k += 1
                    if k > 40:
                        raise RuntimeError("More than 40 plates; use fewer copies")
                if any(p.get("pauses") for p in plates):
                    notes.insert(0, "Pauses for inserts: " + "; ".join(
                        (f"plate {p['n']}: " if len(plates) > 1 else "") + ", ".join(
                            f"layer {x['layer']} ({x['height']:g} mm)" for x in p["pauses"]) for p in plates if p.get("pauses"))
                        + ". The printer stops there; drop the magnet / nut in, then resume (you get a phone alert).")
                elif pause_heights:
                    notes.insert(0, "Insert pauses asked for, but none landed inside the print (heights above the top?)")
                if style:
                    notes.insert(0, {"vase": "Vase mode: one continuous spiral wall, " + str(m.get("vase_wall") or "0.8")
                                     + " mm thick, no infill and no top", "hollow": "Hollow: " + str(m.get("walls") or "3")
                                     + " walls, no infill, open top"}[style])
                total_g = sum(p["stats"].get("grams") or 0 for p in plates)
                area = info.get("area_cm2")
                if area and not style and len(items) == 1 and not purge_names and not fplates:  # meant to be hollow, printed as a block
                    hollow = area * scale ** 2 * items[0]["copies"] * 0.08 * 1.24  # g for ~0.8 mm of wall all over (3 walls)
                    if total_g > 250 or (total_g > 120 and total_g > 3.5 * hollow):
                        notes.insert(0, f"Heavy: {total_g:.0f} g (a hollow version of this would be about {hollow:.0f} g). "
                                        "If it's meant to be a vase or box, go back and pick the Hollow or Vase print style.")
                long_ = [p for p in plates if self._seconds(p["stats"].get("time")) > 12 * 3600]
                if long_:
                    notes.insert(0, "Long print: " + ", ".join(f"plate {p['n']} {p['stats']['time']}" for p in long_)
                                 + ". Cutting it into parts, a lower infill or thicker layers would shorten it.")
                if variable and not any("Variable layers:" in n for n in notes):
                    notes.insert(0, f"Variable layer height ({variable})")
                if len(plates) > 1 and groups is None:
                    notes.append(f"Didn't all fit on one plate: {len(plates)} plates "
                                 f"({', '.join(str(p['parts']) for p in plates)} pieces)")
                ctx.update(plates=plates, trays=trays, n_tools=n_tools, opts=m, notes=notes, presets=presets)
                self._save_ctx(jid, ctx)
                await self.job_update(jid, "review", 100,
                                      f"{len(plates)} plate{'s' if len(plates) > 1 else ''} ready to check",
                                      plates=self._public_plates(plates), notes=notes, presets=presets, trays=trays[:n_tools],
                                      stats=plates[0]["stats"] if len(plates) == 1 else None)
            except Exception as e:
                log.exception("slicing job %s failed", jid)
                await self.job_update(jid, "prepare", 0, "Slicing failed: " + str(e)[:300], error=True)

    async def calib_job(self, m: dict) -> None:
        """Generate a calibration test for one Canvas slot, slice, tweak, upload and (normally) start it."""
        jid = self._jid(m["id"])
        wd = os.path.join(WORK, jid)
        os.makedirs(wd, exist_ok=True)
        if self.heavy.locked():
            await self.job_update(jid, "queued", 0, "Waiting for the job ahead of it")
        async with self.heavy:
            if jid in self.cancelled:
                return
            try:
                slot = int(m["slot"])
                snap = self.printer.snapshot()
                preset = self._presets([slot], m.get("materials") or self.materials, snap)[0]
                cal = (m.get("calib") if m.get("calib") is not None else self.calib).get(preset)
                await self.job_update(jid, "slicing", 0, f"Making the {calib.TESTS[m['test']]['title'].lower()} "
                                                         f"for {preset.replace(' @ECC2', '')}")
                plan = await asyncio.to_thread(calib.build, m["test"], preset, cal, os.path.join(wd, "models"))
                gcode = await asyncio.to_thread(slicer.slice_model, plan["models"], wd, [preset], "standard", 1,
                                                plan["process"], orient=False, filament_overrides=[plan["filament"]],
                                                arrange=plan.get("arrange", True))
                await asyncio.to_thread(calib.postprocess, gcode, plan)
                stats = slicer.gcode_stats(gcode)
                try:
                    tray = {t["id"]: t for t in snap.get("trays", [])}.get(slot, {})
                    png = await asyncio.to_thread(thumbnail.render, plan["models"][0], tray.get("color") or "#FF7A1A")
                    await asyncio.to_thread(thumbnail.embed, gcode, png)
                except Exception:
                    log.exception("thumbnail failed (printing without one)")
                pub = {"test": m["test"], "slot": slot, "preset": preset, "title": plan["title"],
                       "question": plan["question"], "base": self._base_full(plan["base"])}
                remote = f"Calib {m['test']} slot{slot + 1}.gcode"
                loop = asyncio.get_running_loop()

                def prog(done, total):
                    asyncio.run_coroutine_threadsafe(self.job_update(jid, "uploading", 100 * done / total,
                                                                     "Sending to the printer", stats=stats, calib=pub), loop)
                await asyncio.to_thread(self.printer.upload, gcode, remote, prog)
                if m.get("start", True):
                    await self.job_update(jid, "starting", 100, "Starting the print", stats=stats, calib=pub)
                    await asyncio.to_thread(self._start, remote, [slot], 1, m)
                    await self.job_update(jid, "printing", 100, "Printing: answer the question when it's done",
                                          stats=stats, remote=remote, calib=pub, presets=[preset], trays=[slot])
                else:
                    await self.job_update(jid, "ready", 100, "On the printer, not started", stats=stats,
                                          remote=remote, calib=pub, presets=[preset])
            except Exception as e:
                log.exception("calibration job %s failed", jid)
                await self.job_update(jid, "failed", 0, str(e)[:400])
            finally:
                shutil.rmtree(wd, ignore_errors=True)

    @staticmethod
    def _base_full(b: dict) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in b.items()}

    async def _add_purge(self, jid: str, wd: str, src: str, presets: list[str], m: dict, pc: dict):
        """Slice once to measure the colour-change purge, design purge objects to soak it up, and
        combine them with the model into one project for the real slice."""
        await self.job_update(jid, "slicing", 0, "Measuring the colour-change purge")
        fil, proc = self._calibrated(presets, m.get("settings") or {}, m.get("calib"))
        proc.update(self._support_overrides(m))
        g0 = await asyncio.to_thread(slicer.slice_model, [src], os.path.join(wd, "purge_probe"), presets,
                                     m.get("quality", "standard"), 1, proc, orient=False, filament_overrides=fil)
        dem = purge.demand(g0)
        if not dem["changes"]:
            raise RuntimeError("this print has no colour changes")
        catch = max(0.25, min(1.0, float(pc.get("catch") or 1.0)))
        slots = max(1, min(4, dem["max_per_layer"], int(pc.get("max_objects") or 4)))
        needs = purge_shapes.needs(dem, slots, catch, purge.MIN_L)
        pdir = os.path.join(wd, "purge")
        shutil.rmtree(pdir, ignore_errors=True)
        os.makedirs(pdir)
        stls, names, sizes = [], [], []
        for k, need in enumerate(needs, start=1):
            if not need:
                continue
            mesh = await asyncio.to_thread(purge_shapes.make, pc["design"], need)
            p = os.path.join(pdir, f"purge{k}.stl")
            mesh.export(p)
            sizes.append((float(mesh.extents[2]), abs(float(mesh.volume)) / 1000 * 1.24))
            stls.append(p)
            names.append(f"purge{k}.stl")
        await self.job_update(jid, "slicing", 0, f"Adding {len(names)} purge object(s)")
        combined = os.path.join(pdir, "with_purge.3mf")
        await asyncio.to_thread(slicer.export_3mf, [src] + stls, os.path.join(pdir, "export"), presets, combined,
                                m.get("quality", "standard"), proc)
        purge.mark_objects(combined, names, {"sparse_infill_density": "100%", "sparse_infill_pattern": "rectilinear"})
        return [[combined]], names, {"chute_mm": dem["flush_mm"], "changes": dem["changes"], "slots": slots,
                                     "height": max(h for h, _ in sizes), "grams": sum(g for _, g in sizes)}

    @staticmethod
    def _support_overrides(m: dict) -> dict:
        """The Prepare screen's per-print supports choice (wins over the Settings tab and the file)."""
        return {"plate": {"enable_support": "1", "support_on_build_plate_only": "1"},
                "everywhere": {"enable_support": "1", "support_on_build_plate_only": "0"},
                "off": {"enable_support": "0"}}.get(m.get("supports") or "", {})

    @staticmethod
    def _style_overrides(style: str, m: dict) -> dict:
        o = {"sparse_infill_density": "0%", "top_shell_layers": "0", "ironing_type": "no ironing"}
        if style == "vase":  # one continuous spiral wall; the line width IS the wall thickness
            wall = str(m.get("vase_wall")) if str(m.get("vase_wall")) in ("0.42", "0.6", "0.8") else "0.8"
            o.update(spiral_mode="1", wall_loops="1", outer_wall_line_width=wall, bottom_shell_layers="5",
                     enable_support="0")
        else:  # hollow: ordinary walls, open top
            walls = str(m.get("walls")) if str(m.get("walls")) in ("2", "3", "4") else "3"
            o.update(wall_loops=walls, bottom_shell_layers="4")
        return o

    @staticmethod
    def _seconds(t: str) -> float:
        parts = {u: float(n) for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", t or "")}
        return parts.get("d", 0) * 86400 + parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)

    @staticmethod
    def _var_writer(path: str, level: str):
        import trimesh
        m = trimesh.load(path, force="mesh")
        m.apply_translation(-m.bounds[0])
        return varlayer.Writer3MF(m, varlayer.ranges(m, level))

    @staticmethod
    def _public_plates(plates: list[dict]) -> list[dict]:
        return [{k: p.get(k, "") for k in ("n", "parts", "stats", "sent", "name", "pauses")} for p in plates]

    @staticmethod
    def _file_objects(src: str, plates: list[dict], scale: float, folder: str) -> list[tuple[str, list]]:
        os.makedirs(folder, exist_ok=True)
        out = []
        for p in plates:
            for m in splitter.plate_objects(src, p["objects"]):
                if scale != 1.0:
                    m.apply_scale(scale)
                m.apply_translation(-m.bounds[0])
                f = os.path.join(folder, f"p{p['n']}_{len(out):03d}.stl")
                m.export(f, file_type="stl")
                out.append((f, [float(m.extents[0]), float(m.extents[1])]))
        return out

    @staticmethod
    def _plate_parts(src: str, plate: dict, names: dict, folder: str) -> tuple[list[dict], list[str]]:
        """A file plate's parts for the prepare view: [{id, name, pos (on the plate, mm), size}] + a light STL each
        (min corner at the origin)."""
        import numpy as np
        import trimesh
        os.makedirs(folder, exist_ok=True)
        ms = splitter.plate_objects_by_id(src, plate["objects"])
        if not ms:
            return [], []
        lo = np.min([m.bounds[0] for _, m in ms], axis=0)
        budget = max(4000, 150_000 // len(ms))
        parts, files = [], []
        for k, (oid, m) in enumerate(ms):
            pos = (m.bounds[0] - lo)[:2]
            m.apply_translation(-m.bounds[0])
            if len(m.faces) > budget:
                try:
                    m = preview._decimate_by_body(m, budget)       # vertex clustering: keeps the shape
                except Exception:
                    pass
            files.append(os.path.join(folder, f"fo{plate['n']}_{k}.stl"))
            m.export(files[-1], file_type="stl")
            parts.append({"id": oid, "name": names.get(oid) or f"Part {k + 1}",
                          "pos": [round(float(v), 1) for v in pos], "size": [round(float(v), 2) for v in m.extents]})
        return parts, files

    @staticmethod
    def _plate_stl(src: str, plate: dict, one: str | None = None, folder: str | None = None) -> list[str]:
        """A file plate's objects as STLs on the bed: all in one file (`one`, for the preview) or one each in `folder`."""
        import numpy as np
        import trimesh
        ms = splitter.plate_objects(src, plate["objects"])
        if not ms:
            raise RuntimeError(f"Plate {plate.get('name') or plate['n']} has nothing on it")
        lo = np.min([m.bounds[0] for m in ms], axis=0)
        for m in ms:  # keep the layout; just bring it to the bed corner, each object down on the bed
            m.apply_translation([-lo[0], -lo[1], -m.bounds[0][2]])
        if one:
            trimesh.util.concatenate(ms).export(one, file_type="stl")
            return [one]
        os.makedirs(folder, exist_ok=True)
        paths = []
        for k, m in enumerate(ms):
            paths.append(os.path.join(folder, f"o{k:03d}.stl"))
            m.export(paths[-1], file_type="stl")
        return paths

    async def send_job(self, m: dict) -> None:
        jid = self._jid(m["id"])
        async with self.heavy:
            if jid in self.cancelled:
                return
            try:
                ctx = self._load_ctx(jid)
                plates = ctx["plates"]
                want, skip = m.get("plate"), set(m.get("skip") or [])
                targets = [p for p in plates if want in (None, p["n"]) and p["n"] not in skip]
                if not targets:
                    raise RuntimeError("no such plate")
                stem = re.sub(r"[^\w\- ]+", " ", os.path.splitext(ctx["name"])[0]).strip()[:44] or "model"
                loop = asyncio.get_running_loop()
                for p in targets:
                    remote = stem + (f" P{p['n']} of {len(plates)}" if len(plates) > 1 else "") + ".gcode"

                    def prog(done, total, n=p["n"]):
                        asyncio.run_coroutine_threadsafe(self.job_update(
                            jid, "uploading", 100 * done / total, f"Sending plate {n} to the printer",
                            plates=self._public_plates(plates)), loop)
                    await asyncio.to_thread(self.printer.upload, p["gcode"], remote, prog)
                    p["sent"] = remote
                    self._save_ctx(jid, ctx)
                pub = self._public_plates(plates)
                if m.get("start") and len(targets) == 1:
                    p = targets[0]
                    await self.job_update(jid, "starting", 100, "Starting the print", plates=pub)
                    await asyncio.to_thread(self._start, p["sent"], ctx["trays"], ctx["n_tools"], ctx.get("opts") or {})
                    await self.job_update(jid, "printing", 100,
                                          f"Printing plate {p['n']}" if len(plates) > 1 else "Printing",
                                          plates=pub, stats=p["stats"], remote=p["sent"], trays=ctx["trays"][:ctx["n_tools"]])
                else:
                    await self.job_update(jid, "ready", 100, "On the printer, not started", plates=pub)
            except Exception as e:
                log.exception("send job %s failed", jid)
                await self.job_update(jid, "review", 0, str(e)[:400], error=True)

    # ---- timelapse: one camera frame per layer, an mp4 for the server when the print ends ---------------
    async def timelapse_loop(self) -> None:
        run = None
        while True:
            await asyncio.sleep(2)
            try:
                s = self.printer.snapshot()
                st, f, layer = s.get("state", ""), s.get("filename") or "", int(s.get("layer") or 0)
                if st == "PRINTING" and f and self.cam_timelapse and self.camera:
                    if not run or run["file"] != f:
                        d = os.path.join(WORK, "tl_" + re.sub(r"[^\w\-]+", "_", f)[:60])
                        if os.path.isdir(d) and time.time() - os.path.getmtime(d) > 6 * 3600:
                            shutil.rmtree(d, ignore_errors=True)  # an old run of the same file
                        os.makedirs(d, exist_ok=True)  # a bridge restart mid-print keeps its frames
                        run = {"file": f, "start": time.time() - float(s.get("elapsed_s") or 0), "dir": d}
                    p = os.path.join(run["dir"], f"{layer:05d}.jpg")
                    fid, jpeg = self.camera.latest()
                    if layer > 0 and not os.path.exists(p) and jpeg and not getattr(self.camera, "stalled", False):
                        with open(p, "wb") as fh:
                            fh.write(jpeg)
                elif run and st not in ("PAUSED", "PAUSING", "RESUMING", "PRINTING", "FILAMENT SWAP"):
                    r, run = run, None
                    asyncio.create_task(self._timelapse_finish(r, st))
            except Exception:
                log.exception("timelapse loop")

    async def _timelapse_finish(self, r: dict, state: str) -> None:
        try:
            frames = sorted(x for x in os.listdir(r["dir"]) if x.endswith(".jpg"))
            if len(frames) < 10 or state not in ("COMPLETE", "STOPPED", "ERROR", "IDLE"):
                return
            if state == "COMPLETE":  # the finished part, head parked: hold it at the end
                await asyncio.sleep(8)
                fid, jpeg = self.camera.latest()
                if jpeg:
                    with open(os.path.join(r["dir"], "99999.jpg"), "wb") as fh:
                        fh.write(jpeg)
                    frames.append("99999.jpg")
            await asyncio.to_thread(self._encode_upload, r, frames)
        except Exception:
            log.exception("timelapse for %s failed", r["file"])
        finally:
            shutil.rmtree(r["dir"], ignore_errors=True)

    def _encode_upload(self, r: dict, frames: list[str]) -> None:
        ff = shutil.which("ffmpeg") or self.cfg.get("ffmpeg")
        if not ff:
            log.warning("no ffmpeg: skipping the timelapse")
            return
        fps = max(10, min(30, round(len(frames) / 8)))  # at least ~8 s long
        seq = frames + [frames[-1]] * round(fps * 1.5)
        lst = os.path.join(r["dir"], "list.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for x in seq:
                fh.write(f"file '{x}'\nduration {1 / fps:.5f}\n")
        out = os.path.join(r["dir"], "out.mp4")
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                        "-vf", f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "27", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
                       check=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        url = self.cfg["server"].rstrip("/") + "/bridge/timelapse"
        with open(out, "rb") as fh:
            requests.post(url, params={"file": r["file"], "start": int(r["start"])}, data=fh,
                          headers={"X-Bridge-Key": self.cfg["bridge_key"]}, timeout=(10, 600)).raise_for_status()
        log.info("timelapse for %s: %d frames uploaded", r["file"], len(frames))

    def _put_asset(self, jid: str, name: str, path: str) -> None:
        url = self.cfg["server"].rstrip("/") + f"/bridge/asset/{jid}/{name}"
        with open(path, "rb") as f:
            r = requests.post(url, data=f, headers={"X-Bridge-Key": self.cfg["bridge_key"]}, timeout=(10, 300))
        r.raise_for_status()

    def _clean_work(self) -> None:
        if not os.path.isdir(WORK):
            return
        for d in os.listdir(WORK):
            p = os.path.join(WORK, d)
            if os.path.isdir(p) and time.time() - os.path.getmtime(p) > KEEP_WORK_S:
                shutil.rmtree(p, ignore_errors=True)

    def _download(self, url: str, dest: str) -> None:
        with requests.get(url, headers={"X-Bridge-Key": self.cfg["bridge_key"]}, stream=True, timeout=(10, 120)) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--config", default="")
    ap.add_argument("--role", choices=("home", "link", "slicer"), default="home",
                    help="home = next to the printer; link = printer only, no slicing (the Fire tablet); "
                         "slicer = slicing worker only (no printer): on the server, or the home PC with --name pc")
    ap.add_argument("--name", choices=("cloud", "pc"), default="cloud", help="which slicer slot (slicer role)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(os.path.join(HERE, "bridge.log"), encoding="utf-8")])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    if not args.mock and args.role != "slicer":
        try:
            relocate_printer(cfg)  # after a power cut the router may have moved the printer
        except Exception:
            logging.getLogger("bridge").exception("printer lookup failed")
    b = Bridge(cfg, args.mock, args.role, args.name)
    b.mock = args.mock
    asyncio.run(b.run())


if __name__ == "__main__":
    main()
