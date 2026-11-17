"""MyPrintCloud server: the cloud half. Phone/web app <-> this <-> home bridge <-> printer.

  python app.py --set-password       first-time setup (also prints the bridge key)
  python app.py [--host 127.0.0.1 --port 8770]

Data lives in ./data (or $MPC_DATA): config.json (secrets), settings.json (slicing
overrides), jobs/ (model files waiting for the bridge), jobs.json (recent jobs).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import secrets
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import models_sources as ms
import printables_feeds
import bedcheck
import watch

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MPC_DATA", os.path.join(HERE, "data"))
WEB = os.environ.get("MPC_WEB", os.path.join(HERE, "..", "web"))
JOBS_DIR = os.path.join(DATA, "jobs")
ASSETS_DIR = os.path.join(DATA, "assets")  # previews the bridge sends up: model.stl, plate<N>.tp.gz
CFG_PATH = os.path.join(DATA, "config.json")
SETTINGS_PATH = os.path.join(DATA, "settings.json")
JOBS_PATH = os.path.join(DATA, "jobs.json")
TOKEN_DAYS = 365
HISTORY_PATH = os.path.join(DATA, "history.json")
QUEUE_PATH = os.path.join(DATA, "queue.json")      # sliced plates waiting their turn
SERVICE_PATH = os.path.join(DATA, "service.json")  # maintenance tasks, due by print hours or days  # one row per finished/stopped print (ours + the printer's own list)
TL_DIR = os.path.join(DATA, "timelapses")  # mp4s the bridge builds from one camera frame per layer
SPOOLS_PATH = os.path.join(DATA, "spools.json")  # Canvas slot -> {"left": g, "full": g}; counted down per print
ALERTS_PATH = os.path.join(DATA, "alerts.json")  # phone alerts (ntfy); off until the user sets a topic URL
PHOTOS_DIR = os.path.join(DATA, "photos")  # the last camera frame of each print that ended (the gallery)
BED_REF_PATH = os.path.join(DATA, "bed_empty.jpg")  # the user's photo of the empty bed, for the bed-clear check
CAM_IDLE_S = 8.0  # stop the bridge's camera upload this long after the last viewer request
MAX_UPLOAD = 200 << 20
MODEL_EXT = (".stl", ".3mf", ".gcode", ".obj", ".step", ".stp")

# Stock CC2 values shown on the settings page (0.20mm Standard @Elegoo CC2 + our defaults)
DEFAULT_SETTINGS = {
    "quality": "standard",
    "overrides": {"elefant_foot_compensation": "0.15", "enable_support": "1", "support_type": "tree(auto)",
                  "support_threshold_angle": "30", "brim_type": "auto_brim"},
    "level": False, "timelapse": False,  # level = extra full-bed level; the gcode probes its own area anyway
    "materials": {},  # Canvas slot -> material the slot really holds ("PLA Wood"); the printer only says "PLA"
    "calib": {},      # filament preset -> {"filament": {...}, "process": {...}} from calibration prints / feedback
    "calib_log": [],  # newest first: {g, t, preset, scope, key, old, new, why}
}


def jload(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(TL_DIR, exist_ok=True)
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)
CFG: dict = jload(CFG_PATH, {})


# ---- auth ----------------------------------------------------------------------
def hash_pw(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()


def make_token() -> str:
    exp = str(int(time.time()) + TOKEN_DAYS * 86400)
    sig = hmac.new(CFG["secret"].encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


DL_PATHS = ("/api/history.csv", "/api/backup.zip")  # file downloads opened outside the app (system browser)


def dl_token(secs: int = 300) -> str:
    """A 5-minute token that only opens the download links above, so a 1-year login never lands in a URL."""
    exp = str(int(time.time()) + secs)
    return "dl." + exp + "." + hmac.new(CFG["secret"].encode(), ("dl." + exp).encode(), hashlib.sha256).hexdigest()


def dl_ok(tok: str | None) -> bool:
    parts = (tok or "").split(".")
    if len(parts) != 3 or parts[0] != "dl" or not parts[1].isdigit() or "secret" not in CFG:
        return False
    good = hmac.new(CFG["secret"].encode(), ("dl." + parts[1]).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts[2], good) and int(parts[1]) > time.time()


def dl_allowed(request: Request) -> bool:
    """A short-lived download key (never the login) may open the CSV / backup downloads."""
    if request.method != "GET":
        return False
    p, tok = request.url.path, req_token(request)
    return p in DL_PATHS and dl_ok(tok)


def token_ok(tok: str | None) -> bool:
    if not tok or "." not in tok or "secret" not in CFG:
        return False
    exp, sig = tok.split(".", 1)
    good = hmac.new(CFG["secret"].encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def req_token(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else request.query_params.get("token")


_login_fail: dict[str, list[float]] = {}


# ---- shared state ------------------------------------------------------------------
class Hub:
    def __init__(self) -> None:
        self.bridge: WebSocket | None = None   # home bridge: the printer + camera
        self.slicer: WebSocket | None = None   # cloud slicing worker on this server (optional)
        self.pcslicer: WebSocket | None = None  # slicing-only worker on the home PC: faster, used first when on
        self.bridge_seen = 0.0
        self.bridge_slices = True  # False for a printer-link-only bridge (the Fire tablet)
        self.status: dict = {}
        self.frame: bytes | None = None
        self.frame_t = 0.0
        self.frame_evt = asyncio.Event()
        self.cam_want_until = 0.0
        self.cam_sent = False
        self.apps: set[WebSocket] = set()
        self.pending: dict[str, asyncio.Future] = {}
        self.jobs: list[dict] = jload(JOBS_PATH, [])

    def job(self, jid: str) -> dict | None:
        return next((j for j in self.jobs if j["id"] == jid), None)

    def busy_job(self, j: dict) -> str:
        """Why this job must not be removed (empty = free to go): it's in the print queue, on its way to the
        printer, or the print running right now."""
        if j.get("stage") in ("uploading", "starting"):
            return "it's being sent to the printer"
        st = self.status or {}
        if j.get("remote") and j["remote"] == st.get("filename") and st.get("state") in ACTIVE:
            return "it's printing right now"
        if not st.get("state") and j.get("stage") == "printing" and not j.get("result"):
            return "it may be printing right now (the printer hasn't reported yet)"  # just restarted: play safe (11/15)
        if any(x.get("job") == j["id"] for x in Queue.load()):
            return "it's in the print queue"
        return ""

    def save_jobs(self) -> None:
        if len(self.jobs) > 50:  # trim the oldest, but never one that's waiting to slice, queued, sending or printing (11/14)
            self.jobs = [j for k, j in enumerate(self.jobs)  # ... or starred (11/14 b)
                         if k < 50 or j.get("star") or j.get("stage") in WAIT_STAGES or self.busy_job(j)]
        jsave(JOBS_PATH, self.jobs)
        keep = {j["id"] for j in self.jobs}
        for d in os.listdir(ASSETS_DIR):
            if d not in keep:
                shutil.rmtree(os.path.join(ASSETS_DIR, d), ignore_errors=True)

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in list(self.apps):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.apps.discard(ws)

    async def to_bridge(self, msg: dict) -> None:
        if not self.bridge:
            raise HTTPException(503, "Your printer bridge is offline (is the tablet or PC bridge running?)")
        await self.bridge.send_text(json.dumps(msg))

    def slicers(self) -> list:
        return [w for w in (self.pcslicer, self.slicer) if w]

    def _ws(self, name: str):
        return {"pcs": self.pcslicer, "cloud": self.slicer,
                "pc": self.bridge if self.bridge and self.bridge_slices else None}.get(name)

    def worker(self) -> tuple[str, WebSocket]:
        """Where models get prepared / sliced: the home PC's slicer when it's on (fast), else the cloud
        slicer, else a home bridge that slices."""
        if self.pcslicer:
            return "pcs", self.pcslicer
        if self.slicer:
            return "cloud", self.slicer
        if self.bridge and self.bridge_slices:
            return "pc", self.bridge
        raise HTTPException(503, "Slicing is offline: the cloud slicer isn't running" + ("" if self.bridge else " and your printer bridge is offline"))

    async def to_worker(self, msg: dict, job: dict | None = None) -> str:
        """Send a slicing message to the worker that holds this job's files (or the best one for a new job)."""
        want = (job or {}).get("worker")
        if want and self._ws(want):
            ws, name = self._ws(want), want
        else:
            name, ws = self.worker()
            if want and msg.get("t") != "job":
                # the worker that holds this job's files is offline: hand the job over to this one, which
                # fetches the model again from here and measures it before doing what was asked
                if msg.get("t") == "job_send":
                    raise HTTPException(409, "This was sliced on a slicer that's offline now. Tap Slice again to redo it.")
                await ws.send_text(json.dumps(self.rebuild_job_msg(job)))
        await ws.send_text(json.dumps(msg))
        if job is not None:
            job["worker"] = name
        return name

    @staticmethod
    def rebuild_job_msg(job: dict) -> dict:
        """The original 'fetch and measure' message for a job, to give it to another worker."""
        settings = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
        r = job.get("request") or {}
        return {"t": "job", "id": job["id"], "filename": job["filename"], "url": f"/bridge/file/{job['id']}",
                "quality": r.get("quality") or settings["quality"],
                "settings": {**settings["overrides"], **(r.get("model_overrides") or {})},
                "trays": r.get("trays"), "plate": int(r.get("plate") or 1),
                "level": settings.get("level", False), "timelapse": settings.get("timelapse", False),
                "start": False, "materials": settings.get("materials") or {}, "mode": "prepare",
                "calib": settings.get("calib") or {}}

    async def command(self, name: str, args: dict | None = None, timeout: float = 30) -> object:
        rid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.to_bridge({"t": "cmd", "rid": rid, "name": name, "args": args or {}})
            res = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504, "The printer did not answer in time")
        finally:
            self.pending.pop(rid, None)
        if not res.get("ok"):
            raise HTTPException(502, res.get("error") or "printer command failed")
        return res.get("data")

    def public_status(self) -> dict:
        online = self.bridge is not None
        return {"bridge": online, "slicer": bool(self.slicer or self.pcslicer), "pc_slicer": self.pcslicer is not None, "printer": self.status if online else {},
                "job": self.jobs[0] if self.jobs else None, "server_time": time.time(), "eta_factor": HISTORY.factor,
                "watch": {"ready": WATCH.det is not None, "score": WATCH.score if WATCH.n else None,
                          "frames": WATCH.n, "error": WATCH.error}}

    async def cam_watch(self) -> None:
        while True:
            want = time.time() < self.cam_want_until
            if self.bridge and want != self.cam_sent:
                try:
                    await self.to_bridge({"t": "cam", "on": want})
                    self.cam_sent = want
                except Exception:
                    pass
            await asyncio.sleep(1)


HUB: Hub


@asynccontextmanager
async def lifespan(app: FastAPI):
    global HUB, SOURCES
    HUB = Hub()
    SOURCES = build_sources()
    t = asyncio.create_task(HUB.cam_watch())
    yield
    t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
# the Android app's pages load from https://localhost (Capacitor) and call this server cross-origin
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(CORSMiddleware, allow_origins=["https://localhost", "http://localhost", "capacitor://localhost"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    if (p.startswith("/api/") and request.method != "OPTIONS"  # CORS preflights carry no token
            and p not in ("/api/login", "/api/ping") and not token_ok(req_token(request))
            and not dl_allowed(request)):
        return JSONResponse({"detail": "login required"}, 401)
    resp = await call_next(request)
    if p.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache"  # app files: Cloudflare would otherwise serve old code for hours
    return resp


@app.get("/api/ping")
async def ping():
    return {"ok": True, "name": "MyPrintCloud"}


@app.post("/api/login")
async def login(request: Request):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    recent = [t for t in _login_fail.get(ip, []) if time.time() - t < 900]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many attempts; wait 15 minutes")
    body = await request.json()
    pw = str(body.get("password", ""))
    if "pw_hash" not in CFG or not hmac.compare_digest(hash_pw(pw, bytes.fromhex(CFG["pw_salt"])), CFG["pw_hash"]):
        _login_fail[ip] = recent + [time.time()]
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    return {"token": make_token()}


@app.post("/api/password")
async def change_password(request: Request):
    """Change the app password (needs the current one). sign_out_all: every other phone/browser has to sign in
    again (a new signing secret makes all old sign-ins invalid); this device gets a fresh token back."""
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    recent = [t for t in _login_fail.get(ip, []) if time.time() - t < 900]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many attempts; wait 15 minutes")
    body = await request.json()
    old, new = str(body.get("old", "")), str(body.get("new", ""))
    if "pw_hash" not in CFG or not hmac.compare_digest(hash_pw(old, bytes.fromhex(CFG["pw_salt"])), CFG["pw_hash"]):
        _login_fail[ip] = recent + [time.time()]
        await asyncio.sleep(1)
        raise HTTPException(403, "The current password is wrong")  # not 401: the app would sign itself out
    if len(new) < 8:
        raise HTTPException(400, "Use at least 8 characters for the new password")
    salt = secrets.token_bytes(16)
    CFG.update(pw_salt=salt.hex(), pw_hash=hash_pw(new, salt))
    out = bool(body.get("sign_out_all"))
    if out:
        CFG["secret"] = secrets.token_hex(32)
    jsave(CFG_PATH, CFG)
    if out:  # live app connections reconnect; the ones holding an old token are then refused
        for w in list(HUB.apps):
            try:
                await w.close(code=1012)
            except Exception:
                pass
            HUB.apps.discard(w)
    return {"token": make_token(), "signed_out": out}


@app.get("/api/status")
async def status():
    return HUB.public_status()


# ---- camera ---------------------------------------------------------------------------
@app.get("/api/cam.jpg")
async def cam_jpg(peek: bool = False):
    if peek and HUB.frame and time.time() - HUB.frame_t < 90:  # lock-screen picture: the failure watch's 20 s frame will do
        return Response(HUB.frame, media_type="image/jpeg")
    HUB.cam_want_until = time.time() + CAM_IDLE_S
    if not HUB.frame or time.time() - HUB.frame_t > 30:
        try:
            await asyncio.wait_for(HUB.frame_evt.wait(), 6)
        except asyncio.TimeoutError:
            raise HTTPException(503, "No camera picture yet")
    return Response(HUB.frame, media_type="image/jpeg")


@app.get("/api/cam.mjpg")
async def cam_mjpg():
    async def gen():
        last = 0.0
        while True:
            HUB.cam_want_until = time.time() + CAM_IDLE_S
            try:
                await asyncio.wait_for(HUB.frame_evt.wait(), 10)
            except asyncio.TimeoutError:
                continue
            if HUB.frame and HUB.frame_t != last:
                last = HUB.frame_t
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(HUB.frame)).encode() + b"\r\n\r\n" + HUB.frame + b"\r\n"
            await asyncio.sleep(0.05)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/rewind")
async def rewind_index(since: int = 0):
    """The print being recorded (or the last one): its frames' times/layers and the head track from row `since` on."""
    m = REWIND.latest()
    if not m:
        return {"id": None}
    live = REWIND.run is not None and REWIND.run["id"] == m["id"]
    track = REWIND.track if live else jload(os.path.join(REWIND_DIR, m["id"], "track.json"), [])
    since = max(0, int(since))
    return {"id": m["id"], "file": m["file"], "start": m["start"], "live": live, "now": round(time.time() - m["start"], 1),
            "frames": m.get("frames") or [], "track": track[since:], "track_n": len(track)}


@app.get("/api/rewind/{rid}/{n}.jpg")
async def rewind_frame(rid: str, n: int):
    if not re.fullmatch(r"\d{8}-\d{6}", rid) or not 0 <= n < 100000:
        raise HTTPException(404, "No such frame")
    fp = os.path.join(REWIND_DIR, rid, f"{n:05d}.jpg")
    if not os.path.isfile(fp):
        raise HTTPException(404, "No such frame")
    return FileResponse(fp, media_type="image/jpeg")


# ---- printer commands ---------------------------------------------------------------------
ALLOWED_CMDS = {"restart", "pause", "resume", "stop", "light", "speed", "auto_refill", "history", "files", "print_file",
                "set_tray", "load", "unload", "heat", "home", "level", "shaper", "pid", "estop", "sleep", "wake"}


@app.post("/api/cmd/{name}")
async def cmd(name: str, request: Request):
    if name not in ALLOWED_CMDS:
        raise HTTPException(404, "unknown command")
    args = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    if name in ("pause", "stop"):
        ALERTS.user_cmd = (name, time.time())
    if name == "print_file":  # same levelling / timelapse choice as prints sent from the app
        await need_clear_bed(bool(args.pop("force", False)), "the print")
        st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
        args = {**args, "level": bool(st.get("level", False)), "timelapse": bool(st.get("timelapse", False))}
    return {"ok": True, "data": await HUB.command(name, args)}


# ---- phone alerts (ntfy) ---------------------------------------------------------------------
ALERT_DEFAULTS = {"url": "", "done": True, "failed": True, "paused": True, "first_layers": True, "almost": True, "photo": True}
ALMOST_S = 600  # "almost done" ping this long before the (history-corrected) finish, for prints of 30 min or more
ACTIVE = {"PRINTING", "PAUSED", "PAUSING", "RESUMING", "HEATING", "PREPARING", "HOMING", "LEVELING", "FILAMENT SWAP"}


class Alerts:
    """Watch printer status for moments worth a phone ping and publish them to the user's ntfy topic."""

    def __init__(self) -> None:
        self.prev: dict = {}
        self.sent: set[str] = set()
        self.user_cmd: tuple[str, float] = ("", 0.0)
        self.client: httpx.AsyncClient | None = None
        self.lost = False  # we told the phone the home bridge dropped mid-print
        self.keys: dict[str, dict] = {}  # one-use keys behind notification buttons -> what they do
        self.run = 0  # counts print starts, so printing the same file again gets its own alerts (11/14)

    def bridge_down(self) -> None:
        """The home bridge dropped. If a print was running and it stays gone 5 min, say so (PC asleep, Wi-Fi, power)."""
        c = self.cfg()
        if c["url"] and c["failed"] and self.prev.get("state") in ACTIVE:
            asyncio.create_task(self._lost_later())

    async def _lost_later(self) -> None:
        await asyncio.sleep(300)
        if HUB.bridge is None and not self.lost:
            self.lost = True
            name = re.sub(r"\.gcode$", "", self.prev.get("filename") or "your print")
            await self.publish(f"Lost contact: {name}", "The app hasn't heard from the printer for 5 minutes, mid-print. "
                               "The print itself likely carries on, but check the PC running the bridge is awake and online.",
                               "electric_plug", 4, False)

    def bridge_up(self) -> None:
        if self.lost:
            self.lost = False
            asyncio.create_task(self.publish("Back in contact", "The app can see the printer again.", "link", 2, False))

    def cfg(self) -> dict:
        return {**ALERT_DEFAULTS, **jload(ALERTS_PATH, {})}

    def _user_did(self, name: str) -> bool:
        return self.user_cmd[0] == name and time.time() - self.user_cmd[1] < 120

    def check(self, cur: dict) -> None:
        prev, self.prev = self.prev, dict(cur)
        c = self.cfg()
        if not c["url"] or not prev:
            return
        ps, cs = prev.get("state", ""), cur.get("state", "")
        if cs in ACTIVE and ps in ("IDLE", "COMPLETE", "STOPPED", "ERROR"):
            self.run += 1  # a new print (not a link blip: OFFLINE -> PRINTING keeps the same run)
        name = re.sub(r"\.gcode$", "", cur.get("filename") or prev.get("filename") or "your print")
        key = f"{name}|{cur.get('total_layers')}|{self.run}"
        if cs != ps:
            if cs == "COMPLETE" and ps in ACTIVE and c["done"]:
                q = Queue.load()
                if q:
                    text = f" Next in the queue: {q[0]['label']} ({len(q)} waiting)."
                    act = self.button(f"Bed's clear: start {q[0]['label']}"[:60], {"do": "queue"})
                else:
                    text, jid, n = self.next_plate(cur.get("filename") or prev.get("filename") or "")
                    act = self.button(f"Bed's clear: start plate {n}", {"do": "plate", "job": jid, "plate": n}) if n else ""
                self.fire(key + "|done", f"Done: {name}", "Your print finished. Take it off the plate when it's cool." + text,
                          "white_check_mark", 4, c["photo"], act)
            elif cs == "ERROR" and c["failed"]:
                self.fire(key + "|error", f"Printer error: {name}", cur.get("error") or "The printer reported an error.",
                          "warning", 5, c["photo"])
            elif cs == "STOPPED" and ps in ACTIVE and c["failed"] and not self._user_did("stop"):
                self.fire(key + "|stopped", f"Stopped: {name}", "The print stopped before it finished.", "x", 5, c["photo"])
            elif (cs in ("PAUSED", "FILAMENT SWAP") and ps in ACTIVE and ps not in ("PAUSED", "FILAMENT SWAP") and c["paused"]
                  and self.insert_pause(cur.get("filename") or prev.get("filename") or "", int(cur.get("layer") or 0))):
                left = self.insert_pause(cur.get("filename") or prev.get("filename") or "", int(cur.get("layer") or 0))
                more = f" {left - 1} more insert pause{'s' if left > 2 else ''} later in this print." if left > 1 else ""
                self.fire(key + f"|insert{cur.get('layer')}", f"Time for your insert: {name}",
                          f"Paused at layer {cur.get('layer')}, right before the pocket gets its roof. Drop the magnet / nut in "
                          f"(push it fully down, it must not stick up), keep hands clear of the nozzle, then resume.{more}",
                          "magnet", 5, c["photo"], self.button("Resume print", {"do": "resume"}, hours=48))
            elif cs == "PAUSED" and ps in ACTIVE and c["paused"] and not self._user_did("pause"):
                self.fire(key + f"|paused{cur.get('layer')}", f"Paused: {name}",
                          f"The printer paused itself at layer {cur.get('layer')} (often filament runout or a jam). "
                          "Check it, then resume from the app.", "pause_button", 5, c["photo"])
        layer, total = int(cur.get("layer") or 0), int(cur.get("total_layers") or 0)
        if cs == "PRINTING" and c["first_layers"] and total >= 10 and 3 <= layer <= 6:
            self.fire(key + "|first", f"First layers down: {name}",
                      f"Layer {layer} of {total}. Here's how it's sticking.", "mag", 3, True)
        left = float(cur.get("remaining_s") or 0) * (HISTORY.factor or 1)
        if (cs == "PRINTING" and c["almost"] and 0 < left <= ALMOST_S
                and float(cur.get("elapsed_s") or 0) + left >= 1800 and int(cur.get("progress") or 0) < 100):
            self.fire(key + "|almost", f"Almost done: {name}",
                      f"About {max(1, round(left / 60))} min to go (layer {layer} of {total})." if total else
                      f"About {max(1, round(left / 60))} min to go.", "hourglass_flowing_sand", 3, c["photo"])

    @staticmethod
    def insert_pause(fname: str, layer: int) -> int:
        """If this print has a planned pause-for-insert at (or right around) this layer: how many are left
        counting this one; else 0. The bridge puts the plan on the job's plates when it slices."""
        for j in HUB.jobs:
            for p in j.get("plates") or []:
                if fname and p.get("sent") == fname and p.get("pauses"):
                    layers = [int(x.get("layer") or 0) for x in p["pauses"]]
                    if any(abs(layer - L) <= 1 for L in layers):
                        return len([L for L in layers if L >= layer - 1])
        return 0

    @staticmethod
    def next_plate(fname: str) -> tuple[str, str, int]:
        j = next((x for x in HUB.jobs if x.get("remote") == fname and x.get("plates")), None)
        if not j:
            return "", "", 0
        plates = j["plates"]
        cur = next((p["n"] for p in plates if p.get("sent") == fname), 0)
        nxt = next((p for p in plates if p["n"] > cur), None)
        if not nxt:
            return "", "", 0
        return (f" Next: plate {nxt['n']} of {len(plates)}. Clear the bed, then tap the button below "
                f"(or start it from Jobs)."), j["id"], nxt["n"]

    def button(self, label: str, what: dict, hours: float = 12) -> str:
        """An ntfy 'http' action: the phone POSTs to a one-use link here, no login needed."""
        k = secrets.token_urlsafe(18)
        self.keys = {a: b for a, b in self.keys.items() if b["exp"] > time.time()}
        self.keys[k] = {**what, "exp": time.time() + hours * 3600}
        base = (jload(CFG_PATH, {}).get("public_url") or "https://print.example.com").rstrip("/")
        return f"http, {label}, {base}/hook/{k}, method=POST, clear=true"

    def fire(self, key: str, title: str, msg: str, tags: str, prio: int, photo: bool, actions: str = "") -> None:
        if key in self.sent:
            return
        self.sent.add(key)
        if len(self.sent) > 500:
            self.sent = set(list(self.sent)[-200:])
        asyncio.create_task(self.publish(title, msg, tags, prio, photo, actions=actions))

    async def snapshot(self) -> bytes | None:
        HUB.cam_want_until = max(HUB.cam_want_until, time.time() + CAM_IDLE_S)
        t0 = time.time()
        while time.time() - t0 < 10:  # a fresh frame, not one from before the event
            if HUB.frame and HUB.frame_t > t0:
                return HUB.frame
            try:
                await asyncio.wait_for(HUB.frame_evt.wait(), 2)
            except asyncio.TimeoutError:
                pass
        return HUB.frame if HUB.frame and time.time() - HUB.frame_t < 60 else None

    async def publish(self, title: str, msg: str, tags: str, prio: int, photo: bool, url: str = "",
                      actions: str = "") -> None:
        url = url or self.cfg()["url"]
        if not url:
            return
        self.client = self.client or httpx.AsyncClient(timeout=30)
        params = {"title": title, "message": msg, "tags": tags, "priority": str(prio),
                  "click": jload(CFG_PATH, {}).get("public_url") or "https://print.example.com"}
        if actions:
            params["actions"] = actions
        try:
            jpg = await self.snapshot() if photo else None
            if jpg:
                await self.client.put(url, params={**params, "filename": "printer.jpg"}, content=jpg)
            else:
                await self.client.post(url, params=params, content=msg.encode("utf-8"))
        except httpx.HTTPError as e:
            print("alert publish failed:", e)


ALERTS = Alerts()


# ---- spool tracking ---------------------------------------------------------------------------
class Spools:
    """Count each Canvas slot's filament down by what the app's prints actually used (per colour)."""

    def __init__(self) -> None:
        self.prev: dict = {}

    @staticmethod
    def load() -> dict:
        return jload(SPOOLS_PATH, {})

    def check(self, cur: dict) -> None:
        prev, self.prev = self.prev, dict(cur)
        ps, cs = prev.get("state", ""), cur.get("state", "")
        if not prev or cs == ps or ps not in ACTIVE or cs not in ("COMPLETE", "STOPPED", "ERROR"):
            return
        fname = cur.get("filename") or prev.get("filename") or ""
        j = next((x for x in HUB.jobs if x.get("stage") == "printing" and x.get("remote") == fname
                  and not x.get("spool_done")), None)
        if not j or not j.get("trays"):
            return  # started outside the app: we don't know what it used
        frac = 1.0 if cs == "COMPLETE" else max(0.0, min(1.0, float(prev.get("progress") or cur.get("progress") or 0) / 100))
        st = j.get("stats") or {}
        each = st.get("grams_each") or ([st["grams"]] if st.get("grams") else [])
        sp, used = self.load(), {}
        for slot, g in zip(j["trays"], each):
            k = str(int(slot))
            used[k] = used.get(k, 0.0) + g * frac
        for k, g in used.items():
            if k in sp and sp[k].get("left") is not None:
                sp[k]["left"] = round(max(0.0, float(sp[k]["left"]) - g), 1)
        j["spool_done"] = True
        j["spool_used"] = {k: round(g, 1) for k, g in used.items()}
        jsave(SPOOLS_PATH, sp)
        HUB.save_jobs()
        asyncio.create_task(HUB.broadcast({"t": "spools", "spools": sp}))


SPOOLS = Spools()


# ---- print history + learned finish times -------------------------------------------------------
PRINTER_RESULT = {1: "complete", 2: "stopped", 3: "error"}  # task_status in the printer's own list (1036)


class History:
    """A row per print that ended. Our own rows carry the printer's early time estimate, which teaches
    `factor`: how much longer (or shorter) prints really take than this printer predicts."""

    def __init__(self) -> None:
        self.prev: dict = {}
        self.run: dict | None = None
        self.factor = 1.0
        self.synced = 0.0

    @staticmethod
    def load() -> list:
        return jload(HISTORY_PATH, [])

    def learn(self, rows: list | None = None) -> None:
        r = [x["secs"] / x["est"] for x in (rows if rows is not None else self.load())
             if x.get("result") == "complete" and x.get("est", 0) > 300 and x.get("secs", 0) > 300][-10:]
        if len(r) >= 2:
            r.sort()
            self.factor = round(max(0.6, min(1.6, r[len(r) // 2])), 3)

    def check(self, cur: dict) -> None:
        prev, self.prev = self.prev, dict(cur)
        ps, cs = prev.get("state", ""), cur.get("state", "")
        fname = cur.get("filename") or prev.get("filename") or ""
        if cs in ACTIVE and fname and (not self.run or self.run["file"] != fname):
            self.run = {"file": fname, "start": time.time() - float(cur.get("elapsed_s") or 0), "est": 0.0}
        if self.run and cs in ACTIVE:
            try:
                self.watch(cur, cs)
            except Exception as e:
                print("stall watch failed:", e)
        if self.run and cs == "PRINTING" and not self.run["est"]:
            el, rem, pr = float(cur.get("elapsed_s") or 0), float(cur.get("remaining_s") or 0), float(cur.get("progress") or 0)
            if pr >= 3 and rem > 0:  # the printer's own estimate once it's really printing
                self.run["est"] = round(el + rem)
        if prev and ps in ACTIVE and cs == "IDLE" and self.run:  # ended without us seeing COMPLETE/STOPPED
            cs = "COMPLETE" if float(prev.get("progress") or 0) >= 99 else "STOPPED"
        if prev and ps in ACTIVE and cs in ("COMPLETE", "STOPPED", "ERROR") and self.run:
            run, self.run = self.run, None
            end = time.time()
            self.hold_over(run, end)
            j = next((x for x in HUB.jobs if x.get("remote") == run["file"]), None)
            grams = (j or {}).get("spool_used") or {}
            if j and not grams:  # trays known but no spools tracked: still count it
                st = j.get("stats") or {}
                each = st.get("grams_each") or ([st["grams"]] if st.get("grams") else [])
                frac = 1.0 if cs == "COMPLETE" else max(0.0, min(1.0, float(prev.get("progress") or 0) / 100))
                for slot, g in zip(j.get("trays") or [0], each):
                    grams[str(int(slot))] = round(grams.get(str(int(slot)), 0) + g * frac, 1)
            row = {"t": round(run["start"]), "end": round(end), "file": run["file"], "result": cs.lower(),
                   "secs": round(end - run["start"]), "est": run["est"], "job": (j or {}).get("id"),
                   "grams": grams, "cost": cost_of(grams)}
            if run.get("why"):  # 11/15: what the printer said when it sat stuck (codes + where)
                row["why"] = run["why"]
                if run.get("events"):
                    row["events"] = run["events"][-120:]
            if j:  # the job card says how it ended ("Printed ✓" / "Stopped" / "Failed") instead of "Started"
                if run.get("why"):
                    j["why"] = run["why"]
                set_job_result(j, row["result"], row["end"])
                for k in JOB_META:  # order / customer note travel into the history (and its CSV)
                    if j.get(k):
                        row[k] = j[k]
            if j:  # the size it was printed at (kept here: jobs are cleaned up after a few days)
                req = j.get("request") or {}
                row["scale"] = float(req.get("scale") or 1)
                sizes = []
                for x in req.get("extras") or []:
                    xj = next((q for q in HUB.jobs if q.get("id") == x.get("job")), {})
                    sizes.append({"name": ((xj.get("origin") or {}).get("title") or xj.get("filename") or "model")[:60],
                                  "scale": float(x.get("scale") or 1), "copies": int(x.get("copies") or 1)})
                if sizes:
                    row["extras"] = sizes
            rows = self.load() + [row]
            jsave(HISTORY_PATH, rows[-2000:])
            self.learn(rows)
            Service.check(rows)
            asyncio.create_task(self.photo(row["t"], row["file"]))

    STUCK_S = float(os.environ.get("MPC_STUCK_S") or 300)  # a colour change normally takes 1-2 min; 5 min parked = the printer is waiting on someone

    def watch(self, cur: dict, cs: str) -> None:
        """11/15: the CC2 can sit at a colour change (Canvas feed / cut trouble) showing the reason only on its own
        screen, and ignores the app meanwhile. Log the printer's status codes during holds, and when it sits there
        5 min, keep why (on the job + its history row) and ping the phone."""
        run, now = self.run, time.time()
        code = cur.get("code") or []
        exc = cur.get("exceptions") or []
        layer = int(cur.get("layer") or 0)
        hold = cs in ("PAUSED", "FILAMENT SWAP", "PAUSING") or bool(exc)
        sig = json.dumps([cs, code, exc], sort_keys=True)
        if sig != run.get("sig") and (hold or run.get("hold_t")):
            ev = run.setdefault("events", [])
            if len(ev) < 400:
                ev.append([round(now - run["start"]), cs, code, exc, layer])
        run["sig"] = sig
        if not hold:
            self.hold_over(run, now)
            return
        run.setdefault("hold_t", now)
        run["hold_state"], run["hold_layer"], run["hold_code"] = cs, layer, code
        if exc:
            run["hold_exc"] = exc
        mins = (now - run["hold_t"]) / 60
        if mins * 60 >= self.STUCK_S and not run.get("stuck_said"):
            run["stuck_said"] = True
            run["why"] = self.why(run, mins, False)
            c = ALERTS.cfg()
            if c["url"] and c["paused"] and not ALERTS._user_did("pause"):
                name = re.sub(r"\.gcode$", "", run["file"])
                ALERTS.fire(f"{name}|stuck|{round(run['hold_t'])}", f"Stuck: {name}",
                            run["why"] + ". Check the printer's screen: it shows the reason there and won't take app "
                            "commands until you answer it.", "warning", 5, c["photo"])

    def hold_over(self, run: dict, now: float) -> None:
        """A hold ended (or the print did): if it had counted as stuck, finish the note with how long it lasted."""
        if run.get("hold_t"):
            if run.get("stuck_said"):
                run["why"] = self.why(run, (now - run["hold_t"]) / 60, True)
            run.pop("hold_t", None)
            run.pop("stuck_said", None)
            run.pop("hold_exc", None)

    @staticmethod
    def why(run: dict, mins: float, over: bool) -> str:
        st = run.get("hold_state")
        what = {"FILAMENT SWAP": "at a colour change", "PAUSED": "paused", "PAUSING": "pausing"}.get(st, "waiting")
        s = f"Stuck {what} {'for' if over else 'for over'} {max(1, round(mins))} min at layer {run.get('hold_layer') or '?'}"
        code, exc = run.get("hold_code") or [], run.get("hold_exc") or []
        if exc:
            s += " · printer error " + json.dumps(exc, separators=(",", ":"))[:120]
        if code:
            s += " · status code " + "/".join(str(x) for x in code)
        return s

    @staticmethod
    async def photo(t: int, fname: str) -> None:
        """Keep the camera's view of how it came out, for the gallery."""
        jpg = await ALERTS.snapshot()
        if not jpg:
            return
        name = f"{t}.jpg"
        with open(os.path.join(PHOTOS_DIR, name), "wb") as f:
            f.write(jpg)
        rows = HISTORY.load()
        for x in rows:
            if x["t"] == t and x["file"] == fname:
                x["photo"] = name
        jsave(HISTORY_PATH, rows)
        old = sorted(os.listdir(PHOTOS_DIR), key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0)
        for x in old[:-300]:  # ~40 KB each
            try:
                os.remove(os.path.join(PHOTOS_DIR, x))
            except OSError:
                pass

    async def sync(self) -> None:
        """Pull the printer's own list (prints started on its screen, and before this app) every few minutes."""
        if time.time() - self.synced < 300 or not HUB.bridge:
            return
        self.synced = time.time()
        try:
            tasks = await HUB.command("history", {}, timeout=12) or []
        except HTTPException:
            return
        rows = self.load()
        seen = [(x["file"], x["t"]) for x in rows]
        added = 0
        for t in tasks:
            name, b, e = t.get("task_name") or "", int(t.get("begin_time") or 0), int(t.get("end_time") or 0)
            if not name or not b or e <= b or any(f == name and abs(tt - b) < 900 for f, tt in seen):
                continue
            res = PRINTER_RESULT.get(t.get("task_status"), "other")
            # a print we lost track of (server restarted mid-print): the printer's list says how the app's job ended
            j = next((x for x in HUB.jobs if x.get("remote") == name and not x.get("result")
                      and x.get("stage") in ("printing", "ready") and float(x.get("printed") or x.get("updated") or 0) <= b + 900), None)
            if j and res != "other":
                set_job_result(j, res, e)
            rows.append({"t": b, "end": e, "file": name, "result": res,
                         "secs": e - b, "est": 0, "job": j["id"] if j else None, "grams": {}, "cost": None, "src": "printer"})
            added += 1
        if added:
            rows.sort(key=lambda x: x["t"])
            jsave(HISTORY_PATH, rows[-2000:])


JOB_RESULTS = ("complete", "stopped", "error")
# free-text fields a job can carry (max length each): an order / customer reference, and a note
JOB_META = {"order": 80, "note": 500}


def set_job_result(j: dict, result: str, end: float) -> None:
    """Remember how a job's latest print ended; cleared again when it's started again."""
    if result not in JOB_RESULTS:
        return
    j["result"], j["ended"] = result, int(end)
    HUB.save_jobs()
    try:
        asyncio.get_running_loop().create_task(HUB.broadcast({"t": "job", "job": j}))
    except RuntimeError:
        pass


def clear_job_result(j: dict) -> None:
    j.pop("result", None)
    j.pop("why", None)
    j.pop("ended", None)
    j["printed"] = time.time()


def cost_of(grams: dict) -> float | None:
    prices = jload(SETTINGS_PATH, DEFAULT_SETTINGS).get("prices") or {}
    if not grams or not any(str(k) in prices for k in grams):
        return None
    return round(sum(g / 1000 * float(prices.get(str(k), 0)) for k, g in grams.items()), 2)


HISTORY = History()
HISTORY.learn()


# ---- rewind (11/15): scrub back through the print that's running, like a live video's seek bar -------------------
REWIND_DIR = os.path.join(DATA, "rewind")
os.makedirs(REWIND_DIR, exist_ok=True)


class Rewind:
    """Keeps, for the current print, every camera frame the bridge sends (the failure watch's one per ~20 s, more while
    someone watches; at most one per 10 s) and the head's track (X/Y/Z/layer about once a second). The app's seek bar
    shows the frame + the bed view for any moment since the start. Only the last KEEP prints are kept on disk."""
    KEEP, GAP, MAX_FRAMES, MAX_TRACK = 2, 10.0, 6000, 200000

    def __init__(self) -> None:
        self.run: dict | None = None
        self.track: list = []
        self.frames: list = []
        self.last_f = 0.0
        self.saved_n = 0
        self.saved_t = 0.0

    def _dir(self) -> str:
        return os.path.join(REWIND_DIR, self.run["id"])

    def _start(self, file: str, start: float) -> None:
        for d in sorted(os.listdir(REWIND_DIR), reverse=True):  # same print after a server restart: carry on with it
            m = jload(os.path.join(REWIND_DIR, d, "meta.json"), None)
            if m and m.get("file") == file and abs(float(m.get("start", 0)) - start) < 180:
                self.run = m
                self.track = jload(os.path.join(REWIND_DIR, d, "track.json"), [])
                self.frames = m.get("frames") or []
                self.saved_n, self.last_f = len(self.track), 0.0
                return
        rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
        self.run = {"id": rid, "file": file, "start": round(start, 1), "frames": []}
        self.track, self.frames, self.last_f, self.saved_n = [], self.run["frames"], 0.0, 0
        os.makedirs(self._dir(), exist_ok=True)
        jsave(os.path.join(self._dir(), "meta.json"), self.run)
        olds = sorted(d for d in os.listdir(REWIND_DIR) if os.path.isdir(os.path.join(REWIND_DIR, d)))
        for d in olds[:-self.KEEP]:
            shutil.rmtree(os.path.join(REWIND_DIR, d), ignore_errors=True)

    def status(self, s: dict) -> None:
        run = HISTORY.run
        if not run or s.get("state") not in ACTIVE:
            if self.run and self.saved_n != len(self.track):
                self.flush()
            if self.run and not run:  # the print is over: the seek bar goes away (files stay for the next KEEP prints)
                self.flush()
                self.run = None
            return
        if not self.run or self.run["file"] != run["file"] or abs(self.run["start"] - run["start"]) > 180:
            if self.run:
                self.flush()
            self._start(run["file"], run["start"])
        pos = s.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) < 3 or pos[0] is None:
            return
        try:
            row = [round(time.time() - self.run["start"], 1), round(float(pos[0]), 2), round(float(pos[1]), 2),
                   round(float(pos[2]), 2), int(s.get("layer") or 0)]
        except (TypeError, ValueError):
            return
        if self.track and self.track[-1][1:] == row[1:]:
            return  # head hasn't moved
        if len(self.track) < self.MAX_TRACK:
            self.track.append(row)
        if time.time() - self.saved_t > 30:
            self.flush()

    def frame(self, jpeg: bytes) -> None:
        if not self.run or not HISTORY.run or (HUB.status or {}).get("state") not in ACTIVE:
            return
        now = time.time()
        if now - self.last_f < self.GAP or len(self.frames) >= self.MAX_FRAMES or len(jpeg) > 2_000_000:
            return
        self.last_f = now
        n = len(self.frames)
        try:
            with open(os.path.join(self._dir(), f"{n:05d}.jpg"), "wb") as f:
                f.write(jpeg)
        except OSError:
            return
        self.frames.append([round(now - self.run["start"], 1), int((HUB.status or {}).get("layer") or 0)])

    def flush(self) -> None:
        if not self.run:
            return
        try:
            jsave(os.path.join(self._dir(), "track.json"), self.track)
            jsave(os.path.join(self._dir(), "meta.json"), self.run)
        except OSError:
            pass
        self.saved_n, self.saved_t = len(self.track), time.time()

    def latest(self) -> dict | None:
        if self.run:
            return self.run
        ds = sorted(d for d in os.listdir(REWIND_DIR) if os.path.isdir(os.path.join(REWIND_DIR, d)))
        return jload(os.path.join(REWIND_DIR, ds[-1], "meta.json"), None) if ds else None


REWIND = Rewind()
WATCH = watch.Watch(os.path.join(HERE, "models", "obico.onnx"), os.path.join(DATA, "watch.json"))
_watch_t = [0.0]


async def watch_frame(jpeg: bytes) -> None:
    """A camera frame while printing: score it, and alert (or pause) when the print looks like it's failing."""
    st = HUB.status or {}
    mode = jload(SETTINGS_PATH, DEFAULT_SETTINGS).get("watch", "alert")
    if mode == "off" or st.get("state") != "PRINTING" or time.time() - _watch_t[0] < 15:
        return
    _watch_t[0] = time.time()
    key = f"{st.get('filename')}|{int(HISTORY.run['start']) if HISTORY.run else 0}"
    verdict = await asyncio.to_thread(WATCH.feed, key, jpeg)
    if not verdict:
        return
    name = re.sub(r"\.gcode$", "", st.get("filename") or "your print")
    c = ALERTS.cfg()
    if verdict == "pause" and mode == "pause":
        if ALERTS.sent.isdisjoint({key + "|watch-paused"}):
            ALERTS.user_cmd = ("pause", time.time())  # our own pause: no "paused itself" alert as well
            try:
                await HUB.command("pause", {})
            except HTTPException:
                pass
            ALERTS.fire(key + "|watch-paused", f"Paused: {name} looks like it's failing",
                        "The failure watch saw what looks like spaghetti or a loose part, so it paused the print. "
                        "Check the photo, then resume or stop from the app.", "rotating_light", 5, True)
        return
    if c["failed"]:
        ALERTS.fire(key + "|watch", f"Check {name}: it may be failing",
                    "The failure watch sees what looks like spaghetti or a loose part. Check the photo.",
                    "rotating_light", 5, True, ALERTS.button("Pause the print", {"do": "pause"}, hours=2))


def tl_name(fname: str, start: float) -> str:
    return f"{int(start)}_{re.sub(r'[^A-Za-z0-9_-]+', '_', re.sub(r'.gcode$', '', fname))[:60]}.mp4"


def tl_for(row: dict) -> str | None:
    stem = tl_name(row["file"], 0).split("_", 1)[1]
    for f in os.listdir(TL_DIR):
        if f.endswith("_" + stem) and f.split("_", 1)[0].isdigit() and abs(int(f.split("_", 1)[0]) - row["t"]) < 900:
            return f
    return None


def hist_match(rows: list, q: str) -> list:
    """History rows whose file name, order or note contains every word typed (case-insensitive)."""
    words = [w for w in (q or "").casefold().split() if w]
    if not words:
        return rows
    word = {"complete": "printed done ok", "stopped": "stopped", "error": "failed error"}
    hay = lambda x: " ".join([str(x.get("file") or ""), str(x.get("order") or ""), str(x.get("note") or ""),
                              word.get(x.get("result"), "")]).casefold()
    return [x for x in rows if all(w in hay(x) for w in words)]


@app.get("/api/stats")
async def stats(q: str = ""):
    await HISTORY.sync()
    rows = HISTORY.load()
    if q.strip():  # a search: matching prints (newest first), the totals stay for everything
        found = hist_match(rows, q)
        return {**(await stats("")), "q": q, "matches": len(found),
                "recent": [{**x, "tl": tl_for(x)} for x in reversed(found[-100:])]}
    now = time.localtime()
    month0 = time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, -1))

    def summ(rs):
        done = [x for x in rs if x["result"] == "complete"]
        ended = [x for x in rs if x["result"] in ("complete", "stopped", "error")]
        costs = [x["cost"] for x in rs if x.get("cost") is not None]
        return {"prints": len(rs), "done": len(done), "success": round(100 * len(done) / len(ended)) if ended else None,
                "hours": round(sum(x["secs"] for x in rs) / 3600, 1),
                "grams": round(sum(sum(x.get("grams", {}).values()) for x in rs)),
                "counted": sum(1 for x in rs if x.get("grams")), "cost": round(sum(costs), 2) if costs else None}
    recent = [{**x, "tl": tl_for(x)} for x in reversed(rows[-40:])]
    return {"month": summ([x for x in rows if x["t"] >= month0]), "all": summ(rows),
            "factor": HISTORY.factor, "samples": sum(1 for x in rows if x.get("est")), "recent": recent}


@app.post("/api/dl_token")
async def make_dl_token(request: Request):
    """A short-lived link token for the CSV / backup downloads (the app opens them in the phone's browser)."""
    return {"token": dl_token()}


RESULT_WORD = {"complete": "Printed", "stopped": "Stopped", "error": "Failed", "other": "Other"}


def csv_safe(v):
    """Spreadsheet formula injection guard: a text cell starting with = + - @ tab or CR gets a leading ' (11/15)."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@app.get("/api/history.csv")
async def history_csv(q: str = "", tz: int = 0):
    """Every print (or the ones matching a search) with time, filament and cost, for a spreadsheet.
    tz = the phone's getTimezoneOffset() in minutes, so times are the user's local times."""
    import csv
    tz = max(-900, min(900, tz))
    when = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(t) - tz * 60)) if t else ""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Started", "Ended", "File", "Result", "Minutes", "Printer estimate (min)", "Filament (g)",
                "Filament per slot", "Cost", "Scale", "Order", "Note", "Source", "Job"])
    for x in reversed(hist_match(HISTORY.load(), q)):
        grams = x.get("grams") or {}
        w.writerow([csv_safe(v) for v in [when(x.get("t")), when(x.get("end")), re.sub(r"\.gcode$", "", x.get("file") or ""),
                    RESULT_WORD.get(x.get("result"), x.get("result") or ""), round((x.get("secs") or 0) / 60),
                    round(x["est"] / 60) if x.get("est") else "",
                    round(sum(grams.values()), 1) if grams else "",
                    "; ".join(f"slot {int(k) + 1}: {g:g} g" for k, g in sorted(grams.items(), key=lambda kv: int(kv[0]))),
                    f"{x['cost']:.2f}" if x.get("cost") is not None else "",
                    f"{round(float(x['scale']) * 100)}%" if x.get("scale") is not None else "",
                    x.get("order") or "", x.get("note") or "", "printer screen" if x.get("src") == "printer" else "app",
                    x.get("job") or ""]])
    name = "myprintcloud-history" + ("-search" if q.strip() else "") + time.strftime("-%Y%m%d") + ".csv"
    return Response(out.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",  # BOM: Excel reads UTF-8
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# what a backup holds: settings (with calibration), spools, history, service log, queue, jobs list, alert switches.
# Never config.json: password hash, signing secret, bridge key and site logins stay on the server.
BACKUP_FILES = [SETTINGS_PATH, SPOOLS_PATH, HISTORY_PATH, SERVICE_PATH, QUEUE_PATH, JOBS_PATH, ALERTS_PATH, BED_REF_PATH]


@app.get("/api/backup.zip")
async def backup_zip():
    import zipfile
    buf = io.BytesIO()
    stamp = time.strftime("%Y%m%d-%H%M")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        inside = []
        for p in BACKUP_FILES:
            if not os.path.exists(p):
                continue
            name = os.path.basename(p)
            if p == ALERTS_PATH:  # the switches, not the ntfy topic (anyone with the topic can read the alerts)
                a = {k: v for k, v in jload(p, {}).items() if k != "url"}
                z.writestr(name, json.dumps(a, indent=1))
            else:
                z.write(p, name)
            inside.append(name)
        z.writestr("README.txt", "MyPrintCloud backup " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) + "\r\n\r\n"
                   "settings.json  slicing settings, slot materials, prices, calibration values + log\r\n"
                   "spools.json    grams left per Canvas slot\r\n"
                   "history.json   every print: times, filament, cost, order notes\r\n"
                   "service.json   service log (maintenance tasks)\r\n"
                   "queue.json     print queue   jobs.json  the Jobs list (models/plates are not included)\r\n"
                   "alerts.json    phone-alert switches (the ntfy topic is left out on purpose)\r\n"
                   "bed_empty.jpg  empty-bed photo for the bed-clear check\r\n\r\n"
                   "Not included on purpose: the password, sign-in secret, bridge key and model-site logins.\r\n"
                   "Included: " + ", ".join(inside) + "\r\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="myprintcloud-backup-{stamp}.zip"'})


@app.get("/api/photo/{name}")
async def photo_get(name: str):
    if not re.fullmatch(r"\d+\.jpg", name) or not os.path.exists(os.path.join(PHOTOS_DIR, name)):
        raise HTTPException(404, "No such photo")
    return FileResponse(os.path.join(PHOTOS_DIR, name), media_type="image/jpeg")


@app.get("/api/timelapse/{name}")
async def timelapse_get(name: str):
    if not re.fullmatch(r"\d+_[A-Za-z0-9_-]+\.mp4", name) or not os.path.exists(os.path.join(TL_DIR, name)):
        raise HTTPException(404, "No such timelapse")
    return FileResponse(os.path.join(TL_DIR, name), media_type="video/mp4")


@app.post("/bridge/timelapse")
async def timelapse_put(request: Request, file: str, start: float, x_bridge_key: str | None = Header(None)):
    if not bridge_ok(x_bridge_key):
        raise HTTPException(403)
    body = await request.body()
    if not body or len(body) > 200 * 1024 * 1024:
        raise HTTPException(400, "empty or too big")
    name = tl_name(file, start)
    with open(os.path.join(TL_DIR, name), "wb") as f:
        f.write(body)
    old = sorted(os.listdir(TL_DIR), key=lambda x: int(x.split("_", 1)[0]) if x.split("_", 1)[0].isdigit() else 0)
    for x in old[:-40]:  # keep the newest 40
        try:
            os.remove(os.path.join(TL_DIR, x))
        except OSError:
            pass
    await HUB.broadcast({"t": "timelapse", "name": name})
    return {"ok": True, "name": name}


# ---- bed-clear check: don't start a print on top of the last one -----------------------------------
BED_MSG = "Something still seems to be on the bed"


async def bed_clear() -> tuple[bool | None, int | None]:
    """(clear, score). clear is None when we can't tell: no empty-bed photo saved, or no camera picture."""
    if not bedcheck.available() or not os.path.exists(BED_REF_PATH):
        return None, None
    jpg = await ALERTS.snapshot()
    if not jpg:
        return None, None
    with open(BED_REF_PATH, "rb") as f:
        ref = f.read()
    s = await asyncio.to_thread(bedcheck.score, ref, jpg)
    return s < bedcheck.TOL, s


async def need_clear_bed(force: bool, what: str) -> None:
    """Refuse to start `what` while the camera sees something on the bed (unless the user said start anyway)."""
    if force:
        return
    clear, _ = await bed_clear()
    if clear is False:
        raise HTTPException(409, f"{BED_MSG}, so {what} didn't start. Take it off, or start anyway.")


@app.get("/api/bed")
async def bed_info():
    have = os.path.exists(BED_REF_PATH)
    return {"available": bedcheck.available(), "ref": have, "ref_t": os.path.getmtime(BED_REF_PATH) if have else None}


@app.post("/api/bed/empty")
async def bed_save():
    """The bed is empty right now: remember what that looks like."""
    jpg = await ALERTS.snapshot()
    if not jpg:
        raise HTTPException(503, "No camera picture (is the bridge on?)")
    with open(BED_REF_PATH, "wb") as f:
        f.write(jpg)
    return await bed_info()


@app.delete("/api/bed/empty")
async def bed_forget():
    try:
        os.remove(BED_REF_PATH)
    except OSError:
        pass
    return await bed_info()


@app.get("/api/bed/empty.jpg")
async def bed_ref_jpg():
    if not os.path.exists(BED_REF_PATH):
        raise HTTPException(404, "No empty-bed photo yet")
    return FileResponse(BED_REF_PATH, media_type="image/jpeg")


@app.post("/api/bed/check")
async def bed_check_now():
    clear, s = await bed_clear()
    if clear is None:
        raise HTTPException(409, "Save a photo of the empty bed first" if not os.path.exists(BED_REF_PATH)
                            else "No camera picture (is the bridge on?)")
    return {"clear": clear, "score": s, "tol": bedcheck.TOL}


# ---- print queue: sliced plates lined up to print one after another --------------------------------
def secs_of(t: str | None) -> int:
    return sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)\s*([dhms])", t or ""))


class Queue:
    started = 0.0  # when start_next last sent a print off

    @staticmethod
    def load() -> list:
        return jload(QUEUE_PATH, [])

    @staticmethod
    async def save(q: list) -> None:
        jsave(QUEUE_PATH, q)
        await HUB.broadcast({"t": "queue", "queue": q})

    @staticmethod
    async def start_next(force: bool = False) -> dict:
        q = Queue.load()
        if not q:
            raise HTTPException(404, "the queue is empty")
        state = (HUB.status or {}).get("state", "")
        if not HUB.bridge:
            raise HTTPException(503, "the bridge is offline")
        if state not in ("IDLE", "COMPLETE", "STOPPED", ""):
            raise HTTPException(409, f"the printer is busy ({state.lower()})")
        if time.time() - Queue.started < 180:  # the last one is still uploading/starting (the printer still says idle)
            raise HTTPException(409, "the last print from the queue is still starting")
        item = q[0]
        j = HUB.job(item["job"])
        if not j:
            await Queue.save(q[1:])
            raise HTTPException(410, "that job is gone (removed from the queue)")
        await need_clear_bed(force, f"“{item['label']}”")
        if time.time() - Queue.started < 180:  # a second tap landed while we looked at the bed
            raise HTTPException(409, "the last print from the queue is still starting")
        await HUB.to_worker({"t": "job_send", "id": j["id"], "plate": int(item["plate"]), "start": True}, j)
        Queue.started = time.time()
        j.update(stage="queued", pct=0, msg=f"Starting {item['label']}", error=False)
        await HUB.broadcast({"t": "job", "job": j})
        await Queue.save(q[1:])
        return item


@app.get("/api/queue")
async def queue_get():
    return Queue.load()


@app.post("/api/queue")
async def queue_add(request: Request):
    """Add one plate ({"job", "plate"}) or every plate of a job ({"job"}) that's been sliced."""
    body = await request.json()
    j = job_or_404(str(body.get("job") or ""))
    plates = j.get("plates") or []
    if not plates:
        raise HTTPException(400, "Slice it first: only sliced plates can be queued")
    want = [p for p in plates if body.get("plate") in (None, p["n"])]
    if not want:
        raise HTTPException(404, "No such plate")
    q = Queue.load()
    title = re.sub(r"\.(stl|3mf|obj|gcode|step|stp)$", "", (j.get("origin") or {}).get("title") or j.get("filename") or "model", flags=re.I)
    for p in want:
        if any(x["job"] == j["id"] and x["plate"] == p["n"] for x in q):
            continue
        name = p.get("name") or (str(p["n"]) if len(plates) > 1 else "")
        q.append({"id": uuid.uuid4().hex[:8], "job": j["id"], "plate": p["n"],
                  "label": f"{title} · plate {name}" if name else title,
                  "secs": secs_of((p.get("stats") or {}).get("time")), "grams": (p.get("stats") or {}).get("grams")})
    await Queue.save(q[:60])
    return q


@app.delete("/api/queue/{qid}")
async def queue_del(qid: str):
    q = [x for x in Queue.load() if x["id"] != qid]
    await Queue.save(q)
    return q


@app.delete("/api/queue")
async def queue_clear():
    await Queue.save([])
    return []


@app.post("/api/queue/order")
async def queue_order(request: Request):
    ids = (await request.json()).get("ids") or []
    q = Queue.load()
    pos = {i: k for k, i in enumerate(ids)}
    q.sort(key=lambda x: pos.get(x["id"], len(ids)))
    await Queue.save(q)
    return q


@app.post("/api/queue/start")
async def queue_start(request: Request):
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    return await Queue.start_next(bool(body.get("force")))


# ---- service logbook: maintenance reminders by print hours / days ------------------------------------
SERVICE_DEFAULTS = [("Clean the build plate (warm water + dish soap)", 40, 0),
                    ("Clean the nozzle, wiper and purge chute", 100, 0),
                    ("Clean the Canvas cutter and feed paths", 150, 0),
                    ("Lube the Z screws and linear rods", 200, 90),
                    ("Check the belt tension", 300, 0),
                    ("Tighten the frame and bed screws", 500, 180)]


class Service:
    @staticmethod
    def hours(rows: list | None = None) -> float:
        return round(sum(x.get("secs", 0) for x in (rows if rows is not None else HISTORY.load())) / 3600, 1)

    @staticmethod
    def load() -> list:
        tasks = jload(SERVICE_PATH, None)
        if tasks is None:  # first use: the usual jobs, counted from now
            h, now = Service.hours(), time.time()
            tasks = [{"id": uuid.uuid4().hex[:8], "name": n, "hours": hh, "days": d, "done_h": h, "done_t": now}
                     for n, hh, d in SERVICE_DEFAULTS]
            jsave(SERVICE_PATH, tasks)
        return tasks

    @staticmethod
    def view(tasks: list, hours: float) -> list:
        out = []
        for t in tasks:
            left_h = t["hours"] - (hours - t["done_h"]) if t.get("hours") else None
            left_d = t["days"] - (time.time() - t["done_t"]) / 86400 if t.get("days") else None
            due = (left_h is not None and left_h <= 0) or (left_d is not None and left_d <= 0)
            out.append({**t, "left_h": None if left_h is None else round(left_h, 1),
                        "left_d": None if left_d is None else round(left_d, 1), "due": due})
        return out

    @staticmethod
    def check(rows: list) -> None:
        """After each print: ping once when a task falls due."""
        for t in Service.view(Service.load(), Service.hours(rows)):
            if t["due"]:
                ALERTS.fire(f"service|{t['id']}|{int(t['done_t'])}", f"Maintenance due: {t['name']}",
                            "Tick it off under Printer -> Service log once it's done.", "wrench", 3, False)


@app.get("/api/service")
async def service_get():
    h = Service.hours()
    return {"hours": h, "tasks": Service.view(Service.load(), h)}


@app.put("/api/service")
async def service_put(request: Request):
    """The edited list: names and intervals; new tasks start counting now."""
    body = await request.json()
    old = {t["id"]: t for t in Service.load()}
    h, now, out = Service.hours(), time.time(), []
    for t in (body.get("tasks") or [])[:30]:
        name = str(t.get("name") or "").strip()[:80]
        hh, d = max(0.0, float(t.get("hours") or 0)), max(0.0, float(t.get("days") or 0))
        if not name or not (hh or d):
            continue
        o = old.get(str(t.get("id")), {})
        out.append({"id": o.get("id") or uuid.uuid4().hex[:8], "name": name, "hours": hh, "days": d,
                    "done_h": o.get("done_h", h), "done_t": o.get("done_t", now)})
    jsave(SERVICE_PATH, out)
    return {"hours": h, "tasks": Service.view(out, h)}


@app.post("/api/service/{tid}/done")
async def service_done(tid: str):
    tasks = Service.load()
    h = Service.hours()
    for t in tasks:
        if t["id"] == tid:
            t["done_h"], t["done_t"] = h, time.time()
    jsave(SERVICE_PATH, tasks)
    return {"hours": h, "tasks": Service.view(tasks, h)}


# ---- notification buttons (ntfy "http" actions: one-use links, no login) --------------------------
def bed_alert(a: dict, key: str, msg: str) -> None:
    """The camera still sees something on the bed: say so with the photo, and offer to start anyway."""
    ALERTS.keys.pop(key, None)
    act = ALERTS.button("It's clear: start anyway", {k: v for k, v in a.items() if k != "exp"} | {"force": True})
    asyncio.create_task(ALERTS.publish("Bed not clear yet", msg, "warning", 4, True, actions=act))


async def hook_bed(a: dict, key: str, what: str) -> None:
    try:
        await need_clear_bed(bool(a.get("force")), what)
    except HTTPException as e:
        bed_alert(a, key, str(e.detail))
        raise

@app.post("/hook/{key}")
async def hook(key: str):
    a = ALERTS.keys.get(key)
    if not a or a["exp"] < time.time():
        raise HTTPException(404, "This button has expired")
    if a["do"] == "plate":
        state = (HUB.status or {}).get("state", "")
        if not HUB.bridge or state not in ("IDLE", "COMPLETE", "STOPPED", ""):
            msg = "the bridge is offline" if not HUB.bridge else f"the printer is busy ({state.lower()})"
            asyncio.create_task(ALERTS.publish("Couldn't start the next plate", f"Not started: {msg}.", "x", 3, False))
            raise HTTPException(409, msg)
        j = job_or_404(a["job"])
        await hook_bed(a, key, f"plate {a['plate']}")
        ALERTS.keys.pop(key, None)
        await HUB.to_worker({"t": "job_send", "id": j["id"], "plate": int(a["plate"]), "start": True}, j)
        j.update(stage="queued", pct=0, msg=f"Starting plate {a['plate']}", error=False)
        await HUB.broadcast({"t": "job", "job": j})
        return {"ok": True}
    if a["do"] == "queue":
        try:
            item = await Queue.start_next(bool(a.get("force")))
        except HTTPException as e:
            if str(e.detail).startswith(BED_MSG):
                bed_alert(a, key, str(e.detail))
            else:
                asyncio.create_task(ALERTS.publish("Couldn't start the next print", f"Not started: {e.detail}.", "x", 3, False))
            raise
        ALERTS.keys.pop(key, None)
        return {"ok": True, "started": item["label"]}
    if a["do"] == "resume":  # after dropping in an insert
        ALERTS.keys.pop(key, None)
        state = (HUB.status or {}).get("state", "")
        if not HUB.bridge or state not in ("PAUSED", "FILAMENT SWAP"):
            raise HTTPException(409, "The printer isn't paused" if HUB.bridge else "The bridge is offline")
        ALERTS.user_cmd = ("resume", time.time())
        await HUB.command("resume", {})
        return {"ok": True}
    if a["do"] == "pause":
        ALERTS.keys.pop(key, None)
        ALERTS.user_cmd = ("pause", time.time())
        await HUB.command("pause", {})
        return {"ok": True}
    raise HTTPException(400)


@app.get("/api/spools")
async def spools_get():
    return Spools.load()


@app.put("/api/spools")
async def spools_put(request: Request):
    body = await request.json()
    out = {}
    for k, v in (body or {}).items():
        if str(k) in ("0", "1", "2", "3") and isinstance(v, dict) and v.get("left") not in (None, ""):
            left = max(0.0, min(5000.0, float(v["left"])))
            out[str(k)] = {"left": round(left, 1), "full": max(left, min(5000.0, float(v.get("full") or 1000)))}
    jsave(SPOOLS_PATH, out)
    await HUB.broadcast({"t": "spools", "spools": out})
    return out


@app.get("/api/alerts")
async def alerts_get():
    return ALERTS.cfg()


@app.put("/api/alerts")
async def alerts_put(request: Request):
    body = await request.json()
    url = str(body.get("url") or "").strip()
    if url and not re.fullmatch(r"https://[\w.-]+(:\d+)?/[\w-]{6,64}", url):
        raise HTTPException(400, "Use an ntfy topic URL like https://ntfy.sh/your-secret-topic")
    c = {"url": url, **{k: bool(body.get(k, ALERT_DEFAULTS[k])) for k in ALERT_DEFAULTS if k != "url"}}
    jsave(ALERTS_PATH, c)
    return c


@app.post("/api/alerts/test")
async def alerts_test():
    c = ALERTS.cfg()
    if not c["url"]:
        raise HTTPException(400, "Save a topic URL first")
    await ALERTS.publish("MyPrintCloud test", "Alerts work. You'll get pings for finished, failed and paused prints.",
                         "bell", 3, bool(c["photo"]) and HUB.bridge is not None)
    return {"ok": True}


# ---- slicing settings ----------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings():
    return jload(SETTINGS_PATH, DEFAULT_SETTINGS)


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    old = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    s = {"quality": str(body.get("quality", "standard")),
         "overrides": {str(k): str(v) for k, v in (body.get("overrides") or {}).items()
                       if re.fullmatch(r"[a-z0-9_]{2,60}", str(k)) and str(v).strip() != ""},
         "level": bool(body.get("level", old.get("level", False))), "timelapse": bool(body.get("timelapse", False)),   # 11/12: omitted level keeps the old value (was: switched ON)
         "materials": {str(int(k)): str(v)[:40] for k, v in (body.get("materials") or {}).items()
                       if str(k).isdigit() and str(v).strip()},
         "calib": old.get("calib") or {}, "calib_log": old.get("calib_log") or [],
         "prices": {str(int(k)): round(max(0.0, min(500.0, float(v))), 2) for k, v in
                    (body.get("prices", old.get("prices")) or {}).items() if str(k).isdigit() and str(v).strip() != ""},
         "cam_timelapse": bool(body.get("cam_timelapse", old.get("cam_timelapse", True))),
         "watch": body.get("watch", old.get("watch", "alert")) if body.get("watch", old.get("watch", "alert")) in ("off", "alert", "pause") else "alert"}
    jsave(SETTINGS_PATH, s)
    await push_settings()
    return s


async def push_settings() -> None:
    """The bridge needs the slot materials and calibrations to show/compute each filament's values."""
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    for ws in [w for w in (HUB.bridge, HUB.slicer, HUB.pcslicer) if w]:
        try:
            await ws.send_text(json.dumps({"t": "settings", "materials": st.get("materials") or {}, "calib": st.get("calib") or {},
                                           "cam_timelapse": st.get("cam_timelapse", True), "watch": st.get("watch", "alert")}))
        except Exception:
            pass


# ---- community models --------------------------------------------------------------------------
SOURCES: dict = {}
TOKEN_SITES = ("nexprint", "makerworld", "thingiverse", "cults3d", "printables")


@app.get("/api/tokens")
async def get_tokens():
    t = CFG.get("tokens", {})
    return {k: bool(t.get(k)) for k in TOKEN_SITES}


@app.put("/api/tokens")
async def put_tokens(request: Request):
    global SOURCES
    body = await request.json()
    t = CFG.setdefault("tokens", {})
    for k in TOKEN_SITES:
        if k in body:
            v = str(body[k] or "").strip()
            if v.lower().startswith("bearer "):
                v = v[7:].strip()
            t[k] = v
    jsave(CFG_PATH, CFG)
    SOURCES = build_sources()
    return {k: bool(t.get(k)) for k in TOKEN_SITES}


def build_sources() -> dict:
    t = CFG.get("tokens", {})
    out = {"printables": ms.Printables(t.get("printables") or None), "nexprint": ms.Nexprint(t.get("nexprint") or None),
           "makerworld": ms.MakerWorld(t.get("makerworld") or None),
           "thingiverse": ms.Thingiverse(t.get("thingiverse") or None)}
    if hasattr(ms, "Cults3D"):  # newer models_sources only
        out["cults3d"] = ms.Cults3D(t.get("cults3d") or None)
    return out


_cache: dict[str, tuple[float, object]] = {}


async def cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return val


def source(name: str):
    if name not in SOURCES:
        raise HTTPException(404, "unknown model site")
    return SOURCES[name]


async def source_call(coro):
    try:
        return await coro
    except ms.AuthRequired as e:
        raise HTTPException(412, str(e))  # needs a site login token
    except ms.Blocked as e:
        raise HTTPException(503, str(e))
    except (ms.SourceError, httpx.HTTPError) as e:
        raise HTTPException(502, str(e))


@app.get("/api/models/{src}/search")
async def models_search(src: str, q: str = "", page: int = 1, adult: int = 0):
    s = source(src)
    kw = {"safe": False} if adult and src in ("cults3d", "printables") else {}   # Settings > "Adult models in ..." (off by default)
    a = ":adult" if kw else ""
    if q.strip():
        return await source_call(cached(f"s:{src}:{q}:{page}{a}", 600, lambda: s.search(q.strip(), page, **kw)))
    return await source_call(cached(f"t:{src}:{page}{a}", 900, lambda: s.trending(page, **kw)))


PRINTABLES = printables_feeds.Feeds()


@app.get("/api/printables/feed")
async def printables_feed(sort: str = "trending", page: int = 1):
    return await source_call(cached(f"pf:{sort}:{page}", 900, lambda: PRINTABLES.feed(sort, page)))


@app.get("/api/printables/contests")
async def printables_contests():
    return await source_call(cached("pc", 1800, PRINTABLES.contests))


@app.get("/api/printables/contests/{cid}")
async def printables_contest(cid: str):
    if not re.fullmatch(r"\d{1,8}", cid):
        raise HTTPException(404, "No such contest")
    return await source_call(cached(f"pc:{cid}", 1800, lambda: PRINTABLES.contest(cid)))


@app.get("/api/models/{src}/detail/{mid}")
async def models_detail(src: str, mid: str):
    s = source(src)
    return await source_call(cached(f"d:{src}:{mid}", 1800, lambda: s.detail(mid)))


IMG_HOSTS = re.compile(r"^https://([a-z0-9-]+\.)*(printables\.com|nexprint\.com|elegoo\.com|aliyuncs\.com|"
                       r"bblmw\.com|makerworld\.com|bambulab\.com|thingiverse\.com|cloudfront\.net|amazonaws\.com|"
                       r"cults3d\.com|cults\.co)(/|$)")
_img_client: httpx.AsyncClient | None = None


@app.get("/api/img")
async def img_proxy(u: str):
    """Model thumbnails through the server (some CDNs refuse hotlinks from an app)."""
    global _img_client
    if not IMG_HOSTS.match(u):
        raise HTTPException(400, "image host not allowed")
    _img_client = _img_client or httpx.AsyncClient(timeout=15, follow_redirects=True,
                                                   headers={"User-Agent": "Mozilla/5.0 MyPrintCloud"})

    hit = _img_cache.get(u)
    if hit and time.time() - hit[0] < 3600:
        _img_cache.move_to_end(u)
        data, ctype = hit[1], hit[2]
    else:
        try:
            r = await _img_client.get(u)
            r.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(502, "image fetch failed")
        data, ctype = await asyncio.to_thread(_shrink, r.content, r.headers.get("content-type", "image/jpeg"))
        _img_put(u, data, ctype)
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


# full-size model photos (1-3 MB each) cached raw ran the server out of memory: shrink them, and cap the cache by bytes
_img_cache: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()
_IMG_BUDGET = 40 * 1024 * 1024


def _shrink(data: bytes, ctype: str, edge: int = 720) -> tuple[bytes, str]:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) <= edge and len(data) < 150_000:
            return data, ctype
        im.thumbnail((edge, edge))
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (28, 30, 36))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        out = io.BytesIO()
        im.save(out, "JPEG", quality=80, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:  # not an image Pillow knows (or no Pillow): pass it through
        return data, ctype


def _img_put(u: str, data: bytes, ctype: str) -> None:
    old = _img_cache.pop(u, None)
    _img_put.total -= len(old[1]) if old else 0
    _img_cache[u] = (time.time(), data, ctype)
    _img_put.total += len(data)
    while _img_put.total > _IMG_BUDGET and _img_cache:
        _, (_, d, _) = _img_cache.popitem(last=False)
        _img_put.total -= len(d)


_img_put.total = 0


# ---- print jobs -------------------------------------------------------------------------------
def tray_list(v) -> list[int] | None:
    """Canvas slots the app picked. The app sends null slots when it loaded before the printer
    reported its trays (printer off / just reconnected); then let the bridge choose."""
    if not v or not isinstance(v, list) or any(t is None or t == "" for t in v):
        return None
    return [int(t) for t in v]


async def queue_job(filename: str, data: bytes, body: dict, origin: dict) -> dict:
    HUB.worker()  # 503 when nothing can slice
    settings = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    jid = uuid.uuid4().hex[:12]
    safe = re.sub(r"[^\w.\- ()]+", "_", filename)[:120] or "model.stl"
    msg = {"t": "job", "id": jid, "filename": safe, "url": f"/bridge/file/{jid}",  # built first: a bad request leaves no orphan job
           "quality": body.get("quality") or settings["quality"],
           "settings": {**settings["overrides"], **(body.get("overrides") or {})},
           "trays": tray_list(body.get("trays")), "plate": int(body.get("plate") or 1),
           "level": settings.get("level", False), "timelapse": settings.get("timelapse", False),
           "start": bool(body.get("start", True)), "materials": settings.get("materials") or {},
           "mode": "prepare" if body.get("mode") == "prepare" else "print", "calib": settings.get("calib") or {}}
    with open(os.path.join(JOBS_DIR, jid), "wb") as f:
        f.write(data)
    job = {"id": jid, "filename": safe, "created": time.time(), "stage": "queued", "pct": 0,
           "msg": "Waiting for the bridge", "origin": origin, "size": len(data)}
    for k, n in JOB_META.items():  # an order no. / note can come with the upload (future order queue)
        v = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(body.get(k) or "")).strip()[:n]  # like /meta
        if v:
            job[k] = v
    HUB.jobs.insert(0, job)
    HUB.save_jobs()
    job["request"] = {k: msg[k] for k in ("quality", "settings", "trays", "plate", "start", "materials", "mode")}
    if body.get("overrides"):  # settings that belong to this model (e.g. crisp earrings): kept for later slices too
        job["request"]["model_overrides"] = {str(k): v for k, v in body["overrides"].items()}
    await HUB.to_worker(msg, job)
    HUB.save_jobs()
    await HUB.broadcast({"t": "job", "job": job})
    return job


@app.post("/api/print/model")
async def print_model(request: Request):
    body = await request.json()
    s = source(body["source"])
    HUB.worker()
    name, data = await source_call(s.download(str(body["id"]), str(body["file_id"])))
    hint = os.path.basename(str(body.get("file_name") or ""))
    if hint.lower().endswith(MODEL_EXT) and not name.lower().endswith(MODEL_EXT + (".bgcode",)):
        name = hint  # some CDNs hand back an id with no extension
    if not name.lower().endswith(MODEL_EXT + (".bgcode",)):
        raise HTTPException(415, f"{name} isn't a printable model file (STL, 3MF or G-code)")
    if name.lower().endswith(".bgcode"):
        raise HTTPException(415, "That file is pre-sliced for a Prusa printer; pick the STL/3MF instead")
    return await queue_job(name, data, body, {"source": body["source"], "id": body["id"],
                                              "title": body.get("title", ""), "thumb": body.get("thumb", "")})


@app.post("/api/print/upload")
async def print_upload(file: UploadFile = File(...), options: str = Form("{}")):
    if not (file.filename or "").lower().endswith(MODEL_EXT):
        raise HTTPException(415, "Send an STL, 3MF or G-code file")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (200 MB max)")
    return await queue_job(file.filename, data, json.loads(options or "{}"), {"source": "upload"})


@app.get("/api/jobs")
async def jobs():
    return [j for k, j in enumerate(HUB.jobs) if k < 30 or j.get("star")]  # starred ones always come along


@app.post("/api/jobs/{jid}/meta")
async def job_meta(jid: str, request: Request):
    """Star a job (kept forever, filterable) and/or set its free-text fields: order no. / customer, note."""
    j = job_or_404(jid)
    body = await request.json()
    if "star" in body:
        if body["star"]:
            j["star"] = True
        else:
            j.pop("star", None)
    for k, n in JOB_META.items():
        if k in body:
            v = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(body[k] or "")).strip()[:n]
            if v:
                j[k] = v
            else:
                j.pop(k, None)
    HUB.save_jobs()
    await HUB.broadcast({"t": "job", "job": j})
    return j


def job_or_404(jid: str) -> dict:
    j = HUB.job(jid) if re.fullmatch(r"[0-9a-f]{12}", jid) else None
    if not j:
        raise HTTPException(404, "No such job")
    return j


@app.post("/api/jobs/{jid}/slice")
async def job_slice(jid: str, request: Request):
    """Prepare screen -> slice: scale, cuts (None = auto when too big, [] = never), pins, filament."""
    j = job_or_404(jid)
    body = await request.json()
    settings = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    cuts = body.get("cuts")
    if cuts is not None:
        cuts = [{"axis": str(c["axis"]).lower()[:1], "at": float(c["at"])} for c in cuts[:12]
                if str(c.get("axis", "")).lower()[:1] in ("x", "y", "z")]
    trays = body.get("trays")
    extras = []
    for x in (body.get("extras") or [])[:8]:  # other prepared models printed alongside this one
        xj = job_or_404(str(x.get("job")))
        if not xj.get("info"):
            raise HTTPException(400, f"{xj['filename']} isn't prepared yet")
        extras.append({"job": xj["id"], "copies": max(1, min(50, int(x.get("copies") or 1))),
                       "scale": max(0.05, min(10.0, float(x.get("scale") or 1))),
                       "trays": tray_list(x.get("trays")),  # this model's own Canvas slots (keeps its colours)
                       "filename": xj["filename"]})  # lets a worker fetch it if another worker prepared it
    msg = {"t": "job_slice", "id": jid, "scale": float(body.get("scale") or 1), "cuts": cuts,
           "copies": max(1, min(50, int(body.get("copies") or 1))), "extras": extras,
           # several models on one plate: "layers" = all together, colours swap each layer (shared);
           # "blocks" = each model finished before the next, one colour change per model
           "together": body.get("together") if body.get("together") in ("layers", "blocks") else None,
           "variable": body.get("variable") if body.get("variable") in ("fast", "balanced", "fine") else None,
           "style": body.get("style") if body.get("style") in ("hollow", "vase") else None,
           "purge": ({"design": str((body.get("purge") or {}).get("design")),
                      "catch": max(0.25, min(1.0, float((body.get("purge") or {}).get("catch") or 1)))}
                     if re.fullmatch(r"[a-z]{2,16}", str((body.get("purge") or {}).get("design") or "")) else None),
           "vase_wall": str(body.get("vase_wall") or "0.8"), "walls": str(body.get("walls") or "3"),
           # per-print supports: "" = Settings tab / file default, plate / everywhere / off
           "supports": body.get("supports") if body.get("supports") in ("plate", "everywhere", "off") else "",
           "keep_colours": bool(body.get("keep_colours", True)),   # scale / cut painted models keeping their colours
           # pause-for-insert: auto = at the model's hidden pockets, heights = extra pauses typed in (mm)
           "inserts": {"auto": bool((body.get("inserts") or {}).get("auto", True)),
                       "heights": [round(float(h), 2) for h in ((body.get("inserts") or {}).get("heights") or [])[:20]
                                   if 0 < float(h) < 400]},
           # a multi-plate 3MF's plates as the app rearranged them: [{name, from (original plate n), objects: [ids]}]
           "fplan": [{"name": str(p.get("name") or "")[:40], "from": int(p.get("from") or 0),
                      "objects": [str(o)[:12] for o in (p.get("objects") or [])[:300]]}
                     for p in (body.get("fplan") or [])[:40]] or None,
           "pins": bool(body.get("pins", True)), "trays": tray_list(trays),
           "quality": body.get("quality") or settings["quality"],
           "settings": {**settings["overrides"], **((j.get("request") or {}).get("model_overrides") or {}),
                        **(body.get("overrides") or {})},
           "materials": settings.get("materials") or {}, "calib": settings.get("calib") or {},
           "level": settings.get("level", False), "timelapse": settings.get("timelapse", False)}
    where = await HUB.to_worker(msg, j)
    j.update(stage="queued", pct=0, msg="Sent to the cloud slicer" if where == "cloud" else "Sent to your PC for slicing",
             plates=None, notes=None, error=False)
    j["request"] = {**j.get("request", {}), **{k: msg[k] for k in ("scale", "cuts", "pins", "trays", "quality", "copies", "extras", "together", "variable", "style", "vase_wall", "walls", "supports", "keep_colours", "purge", "fplan")}}
    j.pop("dropped", None)  # a fresh slice makes fresh plates
    HUB.save_jobs()
    await HUB.broadcast({"t": "job", "job": j})
    return j


@app.post("/api/jobs/{jid}/send")
async def job_send(jid: str, request: Request):
    """Review screen -> printer: one plate (optionally started) or every plate."""
    j = job_or_404(jid)
    body = await request.json()
    plate = body.get("plate")
    if not HUB.bridge:
        raise HTTPException(503, "Your printer bridge is offline (is the tablet or PC bridge running?)")
    if body.get("start") and plate:
        await need_clear_bed(bool(body.get("force")), "the print")
    if plate and int(plate) in (j.get("dropped") or []):
        raise HTTPException(404, "That plate was removed")
    await HUB.to_worker({"t": "job_send", "id": jid, "plate": int(plate) if plate else None,
                         "skip": j.get("dropped") or [],
                         "start": bool(body.get("start")) and bool(plate)}, j)
    j.update(stage="queued", pct=0, msg="Sending to the printer", error=False)
    await HUB.broadcast({"t": "job", "job": j})
    return j


@app.post("/api/jobs/{jid}/reprint")
async def job_reprint(jid: str, request: Request):
    """Start a file this job already put on the printer again (same Canvas slots, counted against the spools)."""
    j = job_or_404(jid)
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    plates = j.get("plates") or []
    if plates:
        n = int(body.get("plate") or 0) or next((p["n"] for p in plates if p.get("sent") == j.get("remote")), plates[0]["n"])
        p = next((x for x in plates if x["n"] == n), None)
        if not p or not p.get("sent"):
            raise HTTPException(400, "That plate isn't on the printer yet: send it from the review screen")
        fname, stats = p["sent"], p.get("stats")
    else:
        fname, stats = j.get("remote"), j.get("stats")
    if not fname:
        raise HTTPException(400, "This job never reached the printer")
    state = (HUB.status or {}).get("state", "")
    if state not in ("IDLE", "COMPLETE", "STOPPED", ""):
        raise HTTPException(409, f"The printer is busy ({state.lower()})")
    await need_clear_bed(bool(body.get("force")), "the print")
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    await HUB.command("print_file", {"filename": fname, "trays": j.get("trays") or None,
                                     "level": bool(st.get("level", False)), "timelapse": bool(st.get("timelapse", False))})
    j.update(stage="printing", pct=100, msg="Printing again", remote=fname, stats=stats or j.get("stats"), error=False)
    j.pop("spool_done", None)
    clear_job_result(j)
    j["updated"] = time.time()
    HUB.save_jobs()
    await HUB.broadcast({"t": "job", "job": j})
    return j


WAIT_STAGES = ("queued", "downloading", "analyzing", "splitting", "slicing")


async def cancel_job(j: dict) -> None:
    for ws in [w for w in (HUB.bridge, HUB.slicer, HUB.pcslicer) if w]:
        try:
            await ws.send_text(json.dumps({"t": "job_cancel", "id": j["id"]}))
        except Exception:
            pass
    j.update(stage="cancelled", msg="Cancelled", pct=0, error=False)
    shutil.rmtree(os.path.join(ASSETS_DIR, j["id"]), ignore_errors=True)
    try:
        os.remove(os.path.join(JOBS_DIR, j["id"]))
    except OSError:
        pass
    q = Queue.load()
    if any(x["job"] == j["id"] for x in q):
        await Queue.save([x for x in q if x["job"] != j["id"]])
    await HUB.broadcast({"t": "job", "job": j})


@app.post("/api/jobs/cancel_waiting")
async def jobs_cancel_waiting():
    """Clear the slicing line: cancel every job that's still waiting, downloading, cutting or slicing."""
    gone = [j for j in HUB.jobs if j.get("stage") in WAIT_STAGES]
    for j in gone:
        await cancel_job(j)
    HUB.save_jobs()
    return {"cancelled": len(gone)}


async def remove_job(j: dict) -> None:
    """Take a job off the list for good: its model file, previews and queue entries go too (print history stays)."""
    if j.get("stage") in WAIT_STAGES:
        await cancel_job(j)  # tell the workers to drop it first
    HUB.jobs = [x for x in HUB.jobs if x["id"] != j["id"]]
    shutil.rmtree(os.path.join(ASSETS_DIR, j["id"]), ignore_errors=True)
    try:
        os.remove(os.path.join(JOBS_DIR, j["id"]))
    except OSError:
        pass
    await HUB.broadcast({"t": "job_gone", "id": j["id"]})


@app.delete("/api/jobs/{jid}")
async def job_remove(jid: str):
    """Remove one job from the Jobs list (printed, prepared, cancelled...)."""
    j = job_or_404(jid)
    why = HUB.busy_job(j) or ("it's starred: unstar it first" if j.get("star") else "")
    if why:
        raise HTTPException(409, f"Can't remove it: {why}")
    await remove_job(j)
    HUB.save_jobs()
    return {"removed": jid}


@app.post("/api/jobs/clear")
async def jobs_clear():
    """Tidy the Jobs list: remove every cancelled or failed job."""
    gone = [j for j in HUB.jobs if j.get("stage") in ("cancelled", "failed") and not j.get("star") and not HUB.busy_job(j)]
    for j in gone:
        await remove_job(j)
    HUB.save_jobs()
    return {"removed": len(gone)}


@app.post("/api/jobs/{jid}/cancel")
async def job_cancel(jid: str):
    j = job_or_404(jid)
    if j.get("stage") in ("printing", "starting", "uploading"):
        raise HTTPException(409, "It's already on the printer: stop the print from the Printer tab instead")
    await cancel_job(j)
    HUB.save_jobs()
    return j


@app.delete("/api/jobs/{jid}/plates/{n}")
async def job_plate_remove(jid: str, n: int):
    """Drop one plate of a sliced job (print only the plates you want). The last plate can't go: discard the job."""
    j = job_or_404(jid)
    plates = j.get("plates") or []
    if not any(p["n"] == n for p in plates):
        raise HTTPException(404, "No such plate")
    if len(plates) < 2:
        raise HTTPException(400, "That's the only plate left: discard the whole model instead")
    j["plates"] = [p for p in plates if p["n"] != n]
    j["dropped"] = sorted(set((j.get("dropped") or []) + [n]))
    q = Queue.load()
    if any(x["job"] == jid and x["plate"] == n for x in q):
        await Queue.save([x for x in q if not (x["job"] == jid and x["plate"] == n)])
    HUB.save_jobs()
    await HUB.broadcast({"t": "job", "job": j})
    return j


# ---- calibration: test prints + answers -> per-filament values ---------------------------
CALIB_TESTS = {
    "temp": {"title": "Temperature tower", "minutes": 25, "sets": "Nozzle temperature",
             "why": "Hotter flows and bonds better but strings and droops; this finds the sweet spot."},
    "flow": {"title": "Flow squares", "minutes": 30, "sets": "Flow ratio",
             "why": "How much plastic comes out vs. what the slicer asks for: fixes gappy or rough top surfaces."},
    "pa": {"title": "Pressure advance chevrons", "minutes": 8, "sets": "Pressure advance",
           "why": "Sharp corners and even walls when the head speeds up and slows down. Nine side-by-side "
                  "chevrons, each with a different setting: pick the sharpest tip."},
    "retract": {"title": "Stringing towers", "minutes": 20, "sets": "Retraction length",
                "why": "How far filament is pulled back before travelling: less stringing."},
    "dims": {"title": "20 mm cube (calipers)", "minutes": 11, "sets": "Elephant foot + XY size",
             "why": "Parts come out the right size, and the first layer doesn't flare out."},
}
FEEDBACK = {  # issue -> (label, scope, key, step, lo, hi)
    "stringing": ("Stringy / hairy", "filament", "filament_retraction_length", 0.2, 0.1, 3.0),
    "elephant": ("Bottom flares out (elephant foot)", "process", "elefant_foot_compensation", 0.05, 0.0, 0.5),
    "gaps": ("Gaps in top / under-extruded", "filament", "filament_flow_ratio", 0.02, 0.8, 1.2),
    "rough": ("Rough, ridged top / over-extruded", "filament", "filament_flow_ratio", -0.02, 0.8, 1.2),
    "droopy": ("Droopy overhangs or bridges", "filament", "nozzle_temperature", -5, 170, 280),
    "weak": ("Layers split / weak", "filament", "nozzle_temperature", 5, 170, 280),
    "corners": ("Corners bulge", "filament", "pressure_advance", 0.005, 0.0, 0.2),
    "tight": ("Holes / fits too tight", "process", "xy_hole_compensation", 0.05, -0.5, 0.5),
    "warp": ("Corners lift / warps", "process", "brim_type", "outer_only", None, None),
}


def calib_save(changes: list[tuple], preset: str, why: str) -> list[dict]:
    """changes: (scope, key, new value or None to reset). Logged as one undoable group."""
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    cal = st.setdefault("calib", {}).setdefault(preset, {"filament": {}, "process": {}})
    log_ = st.setdefault("calib_log", [])
    g, done = uuid.uuid4().hex[:8], []
    for scope, key, new in changes:
        bucket = cal.setdefault(scope, {})
        old = bucket.get(key)
        if new is None:
            bucket.pop(key, None)
        else:
            bucket[key] = new
        e = {"g": g, "t": time.time(), "preset": preset, "scope": scope, "key": key, "old": old, "new": new, "why": why}
        log_.insert(0, e)
        done.append(e)
    st["calib_log"] = log_[:100]
    jsave(SETTINGS_PATH, st)
    return done


def calib_base(preset: str) -> dict:
    """Current values for a filament (stock + calibrated), as the bridge last reported them."""
    b = (HUB.status.get("presets") or {}).get(preset)
    if not b:
        raise HTTPException(409, "Load that filament in the Canvas (and keep the PC bridge on) first")
    return b


@app.get("/api/calib")
async def calib_get():
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    return {"tests": CALIB_TESTS, "feedback": {k: v[0] for k, v in FEEDBACK.items()},
            "calib": st.get("calib") or {}, "log": (st.get("calib_log") or [])[:30]}


@app.post("/api/calib/start")
async def calib_start(request: Request):
    body = await request.json()
    test, slot = str(body.get("test")), int(body.get("slot", -1))
    if test not in CALIB_TESTS or not 0 <= slot <= 3:
        raise HTTPException(400, "Pick a test and a Canvas slot")
    if not HUB.bridge:
        raise HTTPException(503, "Your printer bridge is offline (is the tablet or PC bridge running?)")
    state = (HUB.status or {}).get("state", "")
    if body.get("start", True) and state not in ("IDLE", "COMPLETE", "STOPPED", ""):
        raise HTTPException(409, f"The printer is busy ({state.lower()})")
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    jid = uuid.uuid4().hex[:12]
    t = CALIB_TESTS[test]
    job = {"id": jid, "filename": f"Calibration: {t['title']}", "created": time.time(), "stage": "queued", "pct": 0,
           "msg": "Waiting for the bridge", "origin": {"source": "calib", "title": f"{t['title']} · slot {slot + 1}"},
           "size": 0}
    HUB.jobs.insert(0, job)
    HUB.save_jobs()
    await HUB.to_worker({"t": "calib_job", "id": jid, "test": test, "slot": slot, "start": bool(body.get("start", True)),
                         "materials": st.get("materials") or {}, "calib": st.get("calib") or {},
                         "level": st.get("level", False), "timelapse": False}, job)
    HUB.save_jobs()
    await HUB.broadcast({"t": "job", "job": job})
    return job


@app.post("/api/calib/answer")
async def calib_answer(request: Request):
    body = await request.json()
    j = job_or_404(str(body.get("job")))
    c = j.get("calib")
    if not c:
        raise HTTPException(400, "Not a calibration job")
    q, b, a = c["question"], c.get("base") or {}, body.get("answer") or {}
    if a.get("keep"):  # "can't tell": the current value is good enough
        c["answered"] = "no visible difference, kept the current value"
        HUB.save_jobs()
        await HUB.broadcast({"t": "job", "job": j})
        return {"summary": c["answered"], "preset": c["preset"], "job": j}
    try:
        if q["kind"] == "pick":
            v = float(a["value"])
            if not any(abs(v - float(o["value"])) < 1e-6 for o in q["options"]):
                raise ValueError
            changes = [(q["scope"], q["key"], v)]
            if q["key"] == "nozzle_temperature":
                changes.append(("filament", "nozzle_temperature_initial_layer", v))
            summary = f"{q['key'].replace('_', ' ')}: {b.get(q['key'], '?')} → {v:g}"
        elif q["kind"] == "height":
            h = max(0.0, min(float(q["max"]), float(a["height"])))
            v = round(max(0.0, (h - float(q["z0"])) * float(q["step"])), 4)
            changes = [("filament", "pressure_advance", v)]
            summary = f"pressure advance: {b.get('pressure_advance', '?')} → {v:g}"
        elif q["kind"] == "dims":
            vals = [float(a[k]) for k in ("x_mid", "y_mid", "x_bot", "y_bot")]
            if not all(15 < x < 25 for x in vals):
                raise HTTPException(400, "Those don't look like measurements of a 20 mm cube (enter mm, e.g. 20.08)")
            nom = float(q.get("nominal", 20))
            mid, bot = (vals[0] + vals[1]) / 2, (vals[2] + vals[3]) / 2
            xy = round(max(-0.3, min(0.3, float(b.get("xy_contour_compensation", 0)) + (nom - mid) / 2)), 3)
            efc = round(max(0.0, min(0.5, float(b.get("elefant_foot_compensation", 0.15)) + (bot - mid) / 2)), 3)
            changes = [("process", "xy_contour_compensation", xy), ("process", "elefant_foot_compensation", efc)]
            summary = f"XY size {b.get('xy_contour_compensation', 0)} → {xy:g}, elephant foot {b.get('elefant_foot_compensation', '?')} → {efc:g}"
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Answer the question first")
    calib_save(changes, c["preset"], f"{c['title']} (slot {c['slot'] + 1})")
    c["answered"] = summary
    HUB.save_jobs()
    await push_settings()
    await HUB.broadcast({"t": "job", "job": j})
    return {"summary": summary, "preset": c["preset"], "job": j}


@app.post("/api/calib/feedback")
async def calib_feedback(request: Request):
    """'How did it turn out?' -> small nudges for the filament the job printed with."""
    body = await request.json()
    j = job_or_404(str(body.get("job")))
    presets = j.get("presets") or []
    if not presets:
        raise HTTPException(400, "This job doesn't say which filament it used")
    preset, base = presets[0], calib_base(presets[0])
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    saved = (st.get("calib") or {}).get(preset) or {}
    changes, lines = [], []
    for issue in body.get("issues") or []:
        if issue not in FEEDBACK:
            continue
        label, scope, key, step, lo, hi = FEEDBACK[issue]
        if isinstance(step, str):
            changes += [(scope, key, step), ("process", "brim_width", "8")]
            lines.append("brim on (8 mm)")
            continue
        cur = float((saved.get(scope) or {}).get(key, base.get(key, 0.0)))
        new = round(max(lo, min(hi, cur + step)), 4)
        if new != cur:
            changes.append((scope, key, new))
            if key == "nozzle_temperature":
                changes.append(("filament", "nozzle_temperature_initial_layer", new))
            lines.append(f"{key.replace('_', ' ')} {cur:g} → {new:g}")
    if not changes:
        raise HTTPException(400, "Nothing to change")
    calib_save(changes, preset, "Feedback on " + ((j.get("origin") or {}).get("title") or j["filename"]))
    j["feedback"] = lines
    HUB.save_jobs()
    await push_settings()
    await HUB.broadcast({"t": "job", "job": j})
    return {"preset": preset, "changes": lines}


@app.post("/api/calib/undo")
async def calib_undo(request: Request):
    body = await request.json()
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    log_ = st.get("calib_log") or []
    g = body.get("g") or (log_[0]["g"] if log_ else None)
    group = [e for e in log_ if e["g"] == g]
    if not group:
        raise HTTPException(404, "Nothing to undo")
    for e in group:
        bucket = st["calib"].setdefault(e["preset"], {}).setdefault(e["scope"], {})
        if e["old"] is None:
            bucket.pop(e["key"], None)
        else:
            bucket[e["key"]] = e["old"]
    st["calib_log"] = [e for e in log_ if e["g"] != g]
    jsave(SETTINGS_PATH, st)
    await push_settings()
    return {"undone": len(group)}


@app.post("/api/calib/reset")
async def calib_reset(request: Request):
    body = await request.json()
    preset = str(body.get("preset"))
    st = jload(SETTINGS_PATH, DEFAULT_SETTINGS)
    cur = (st.get("calib") or {}).get(preset) or {}
    keys = [(sc, k) for sc in ("filament", "process") for k in (cur.get(sc) or {}) if body.get("key") in (None, k)]
    calib_save([(sc, k, None) for sc, k in keys], preset, "Reset to Elegoo default")
    await push_settings()
    return {"reset": len(keys)}


ASSET_NAME = re.compile(r"model\.stl|plate\d{1,3}\.tp\.gz|fplate\d{1,2}\.png|fo\d{1,2}_\d{1,3}\.stl")  # fplate: a 3MF's own plate pictures; fo: its parts


@app.get("/api/jobs/{jid}/asset/{name}")
async def job_asset(jid: str, name: str):
    job_or_404(jid)
    p = os.path.join(ASSETS_DIR, jid, name)
    if not ASSET_NAME.fullmatch(name) or not os.path.exists(p):
        raise HTTPException(404, "Preview not available")
    return FileResponse(p, media_type="image/png" if name.endswith(".png") else "application/octet-stream")


# ---- bridge side -----------------------------------------------------------------------------
def bridge_ok(key: str | None) -> bool:
    return bool(key) and "bridge_key" in CFG and hmac.compare_digest(key, CFG["bridge_key"])


@app.get("/bridge/file/{jid}")
async def bridge_file(jid: str, x_bridge_key: str | None = Header(None)):
    if not bridge_ok(x_bridge_key) or not re.fullmatch(r"[0-9a-f]{12}", jid):
        raise HTTPException(403)
    p = os.path.join(JOBS_DIR, jid)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="application/octet-stream")


@app.post("/bridge/asset/{jid}/{name}")
async def bridge_asset(jid: str, name: str, request: Request, x_bridge_key: str | None = Header(None)):
    if not bridge_ok(x_bridge_key) or not re.fullmatch(r"[0-9a-f]{12}", jid) or not ASSET_NAME.fullmatch(name):
        raise HTTPException(403)
    d = os.path.join(ASSETS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    size = 0
    with open(os.path.join(d, name + ".part"), "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(413)
            f.write(chunk)
    os.replace(os.path.join(d, name + ".part"), os.path.join(d, name))
    return {"ok": True, "size": size}


XFER_DIR = os.path.join(DATA, "xfer")  # finished gcode on its way from the cloud slicer to the home bridge


@app.post("/bridge/xfer/{tok}")
async def xfer_put(tok: str, request: Request, x_bridge_key: str | None = Header(None)):
    if not bridge_ok(x_bridge_key) or not re.fullmatch(r"[0-9a-f]{32}", tok):
        raise HTTPException(403)
    os.makedirs(XFER_DIR, exist_ok=True)
    for f in os.listdir(XFER_DIR):  # anything older than a day was never collected
        if time.time() - os.path.getmtime(os.path.join(XFER_DIR, f)) > 86400:
            os.remove(os.path.join(XFER_DIR, f))
    size, p = 0, os.path.join(XFER_DIR, tok)
    with open(p + ".part", "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(413)
            f.write(chunk)
    os.replace(p + ".part", p)
    return {"ok": True, "size": size}


@app.get("/bridge/xfer/{tok}")
async def xfer_get(tok: str, x_bridge_key: str | None = Header(None)):
    if not bridge_ok(x_bridge_key) or not re.fullmatch(r"[0-9a-f]{32}", tok):
        raise HTTPException(403)
    p = os.path.join(XFER_DIR, tok)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="application/octet-stream")


async def relay(ws: WebSocket, msg: dict) -> None:
    """The cloud slicer asks for a printer action (upload a finished file, start it): pass it to the home bridge."""
    try:
        data = await HUB.command(str(msg.get("name")), msg.get("args") or {}, timeout=900)
        out = {"t": "reply", "rid": msg.get("rid"), "ok": True, "data": data}
    except HTTPException as e:
        out = {"t": "reply", "rid": msg.get("rid"), "ok": False, "error": e.detail}
    except Exception as e:
        out = {"t": "reply", "rid": msg.get("rid"), "ok": False, "error": str(e)}
    if msg.get("name") == "fetch_upload":
        try:
            os.remove(os.path.join(XFER_DIR, str((msg.get("args") or {}).get("xfer"))))
        except OSError:
            pass
    try:
        await ws.send_text(json.dumps(out))
    except Exception:
        pass


@app.websocket("/ws/bridge")
async def ws_bridge(ws: WebSocket):
    if not bridge_ok(ws.headers.get("x-bridge-key")):
        await ws.close(code=4403)
        return
    await ws.accept()
    if ws.headers.get("x-bridge-role") == "slicer":
        await ws_slicer(ws, "pcslicer" if ws.headers.get("x-slicer-name") == "pc" else "slicer")
        return
    old, HUB.bridge = HUB.bridge, ws
    HUB.bridge_slices = ws.headers.get("x-bridge-role") != "link"
    if old:
        try:
            await old.close(code=4000)
        except Exception:
            pass
    HUB.cam_sent = False
    await push_settings()
    await HUB.broadcast({"t": "bridge", "online": True})
    ALERTS.bridge_up()
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if m.get("bytes"):
                HUB.frame, HUB.frame_t = m["bytes"], time.time()
                HUB.frame_evt.set()
                HUB.frame_evt = asyncio.Event()
                asyncio.create_task(watch_frame(m["bytes"]))
                try:
                    REWIND.frame(m["bytes"])
                except Exception as e:
                    print("rewind frame failed:", e)
                continue
            msg = json.loads(m.get("text") or "{}")
            HUB.bridge_seen = time.time()
            t = msg.get("t")
            if t == "status":
                HUB.status = msg["s"]
                try:
                    ALERTS.check(HUB.status)
                except Exception as e:
                    print("alert check failed:", e)
                try:
                    SPOOLS.check(HUB.status)
                except Exception as e:
                    print("spool check failed:", e)
                try:
                    HISTORY.check(HUB.status)
                except Exception as e:
                    print("history check failed:", e)
                try:
                    REWIND.status(HUB.status)
                except Exception as e:
                    print("rewind failed:", e)
                await HUB.broadcast({"t": "status", "s": HUB.status, "eta_factor": HISTORY.factor,
                                     "watch": HUB.public_status()["watch"]})
                for sw in HUB.slicers():  # they pick filament presets from what's loaded
                    try:
                        await sw.send_text(json.dumps({"t": "status", "s": HUB.status}))
                    except Exception:
                        pass
            elif t == "reply":
                fut = HUB.pending.get(msg.get("rid"))
                if fut and not fut.done():
                    fut.set_result(msg)
            elif t == "job":
                await job_message(msg)
    except WebSocketDisconnect:
        pass
    finally:
        if HUB.bridge is ws:
            HUB.bridge = None
            HUB.cam_sent = False
            await HUB.broadcast({"t": "bridge", "online": False})
            ALERTS.bridge_down()


async def ws_slicer(ws: WebSocket, slot: str = "slicer") -> None:
    """A slicing worker: 'slicer' = the cloud one on this server, 'pcslicer' = the home PC's."""
    old = getattr(HUB, slot)
    setattr(HUB, slot, ws)
    if old:
        try:
            await old.close(code=4000)
        except Exception:
            pass
    await push_settings()
    if HUB.status:
        await ws.send_text(json.dumps({"t": "status", "s": HUB.status}))
    await HUB.broadcast({"t": "slicer", "online": True})
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            msg = json.loads(m.get("text") or "{}")
            if msg.get("t") == "job":
                await job_message(msg)
            elif msg.get("t") == "relay":
                asyncio.create_task(relay(ws, msg))
    except WebSocketDisconnect:
        pass
    finally:
        if getattr(HUB, slot) is ws:
            setattr(HUB, slot, None)
            await HUB.broadcast({"t": "slicer", "online": bool(HUB.slicer or HUB.pcslicer)})


async def job_message(msg: dict) -> None:
    """A job progress update from either worker (home bridge or cloud slicer)."""
    j = HUB.job(msg["id"])
    if j and j.get("stage") == "cancelled":
        return  # cancelled while the worker was busy with it: its late updates don't bring it back
    if j:
        if msg.get("plates") and j.get("dropped"):
            msg["plates"] = [p for p in msg["plates"] if p.get("n") not in j["dropped"]]
        j.update({k: msg[k] for k in ("stage", "pct", "msg", "stats", "remote", "info", "plates",
                                      "notes", "error", "presets", "trays") if k in msg})
        if msg.get("stage") == "printing":
            j.pop("spool_done", None)  # a (re)started print is counted again when it ends
            clear_job_result(j)  # ... and gets a fresh "Printed ✓ / Stopped / Failed" when it does
        if msg.get("calib"):  # keep the answer if one was given already
            j["calib"] = {**msg["calib"], **{k: v for k, v in (j.get("calib") or {}).items() if k == "answered"}}
        j["error"] = bool(msg.get("error"))
        j["updated"] = time.time()
        if j["stage"] in ("printing", "ready", "failed", "prepare", "review", "cancelled"):
            try:
                os.remove(os.path.join(JOBS_DIR, j["id"]))
            except OSError:
                pass
            HUB.save_jobs()
        await HUB.broadcast({"t": "job", "job": j})


@app.websocket("/ws/app")
async def ws_app(ws: WebSocket):
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    await ws.accept()
    HUB.apps.add(ws)
    try:
        await ws.send_json({"t": "hello", **HUB.public_status()})
        while True:
            await ws.receive_text()  # keepalive pings from the app
    except WebSocketDisconnect:
        pass
    finally:
        HUB.apps.discard(ws)


if os.path.isdir(WEB):
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


def strip_prefix(inner, prefix: str = "/mpc"):
    """Also serve everything under /mpc: the Cloudflare tunnel routes <host>/mpc/* here (no DNS change needed)."""
    async def asgi(scope, receive, send):
        if scope["type"] in ("http", "websocket") and (scope["path"] == prefix or scope["path"].startswith(prefix + "/")):
            path = scope["path"][len(prefix):] or "/"
            scope = {**scope, "path": path, "raw_path": path.encode()}
        await inner(scope, receive, send)
    return asgi


def setup() -> None:
    cfg = jload(CFG_PATH, {})
    pw = getpass.getpass("New app password: ")
    if len(pw) < 8:
        raise SystemExit("Use at least 8 characters")
    salt = secrets.token_bytes(16)
    cfg.update(pw_salt=salt.hex(), pw_hash=hash_pw(pw, salt))
    cfg.setdefault("secret", secrets.token_hex(32))
    cfg.setdefault("bridge_key", secrets.token_urlsafe(32))
    cfg.setdefault("tokens", {})
    jsave(CFG_PATH, cfg)
    print("Saved. Bridge key:", cfg["bridge_key"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-password", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args()
    if a.set_password:
        setup()
    else:
        import uvicorn
        uvicorn.run(strip_prefix(app), host=a.host, port=a.port, proxy_headers=True, log_level="info")
