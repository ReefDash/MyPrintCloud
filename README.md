# MyPrintCloud

A self-hosted cloud for the **Elegoo Centauri Carbon 2** (with or without the Canvas multi-colour unit).
Watch, slice, queue and start prints from your phone, from anywhere, on a server **you** own. Nothing goes
through Elegoo's cloud, and no ports are opened on your home network.

> Not affiliated with or endorsed by Elegoo. Use at your own risk: it drives a real printer.

## What it does

- **Live printer page:** status, temperatures, progress, Canvas slots and the camera, from anywhere.
- **Print from your phone:** upload an STL / 3MF, or browse Printables (plus Cults3D, Thingiverse and Nexprint
  with your own logins), then slice with Elegoo's own CC2 presets and send.
- **Multi-colour:** map a model's colours to Canvas slots, painted 3MFs, a plate editor (arrange, scale, split
  parts that don't fit).
- **Filament calibration:** temperature, flow, pressure advance, retraction and size tests. The app asks what you
  see, then uses the tuned values for every print with that filament.
- **Queue, history and costs:** a print queue, every print logged with time, filament and cost, a CSV export and a
  backup download.
- **Watching for you:** optional AI spaghetti detection on the camera, an empty-bed check, timelapses and phone
  alerts through [ntfy](https://ntfy.sh).
- **Android app:** the same UI, plus a progress notification while printing.

## How it fits together

```
 phone app / browser ──https──►  SERVER  (any small Linux box with a public HTTPS name)
                                    ▲
                                    │  outbound websocket only: nothing opened at home
                                    │
                        BRIDGE  (PC or Android tablet at home) ──LAN──►  Centauri Carbon 2
                                                                         (MQTT, upload, camera)
```

- **`server/`**: FastAPI app. It serves the web UI, keeps jobs, queue and history, and relays commands.
- **`bridge/`**: runs at home next to the printer. It talks to the printer on your LAN and slices with
  ElegooSlicer. It can run in three roles:
  - `home` (default): printer + slicing, on one Windows PC.
  - `link`: printer only, for an always-on device such as an Android tablet.
  - `slicer`: slicing only (`--name pc` on a home PC, or the optional cloud slicer on the server).
- **`web/`**: the app UI (plain HTML/JS, served by the server).
- **`app/`**: an Android wrapper (Capacitor) around the same UI.

---

## What you need

| | |
|---|---|
| Printer | Centauri Carbon 2 on your home network, with **LAN mode** and its **access code** (on the printer's screen, in its network settings) |
| Server | A small Linux box reachable over HTTPS: a cheap VPS, Oracle Cloud's free tier, or a home server behind a Cloudflare Tunnel. 1 GB RAM is enough (2 GB with spaghetti detection). Python 3.10+ |
| A domain name | For example `print.example.com` pointing at the server (through Cloudflare or Caddy, below) |
| Home PC (Windows) | Python 3.10+ and **[ElegooSlicer](https://www.elegoo.com/pages/elegoo-slicer)** installed. It runs the bridge and does the slicing |
| Optional | An old Android tablet to be the always-on printer link (Termux), so the PC can sleep |

---

## 1. Set up the server

```bash
# on the server
sudo apt install -y python3-venv git
git clone https://github.com/ReefDash/MyPrintCloud.git ~/myprintcloud
cd ~/myprintcloud
python3 -m venv venv
venv/bin/pip install -r server/requirements.txt

# first-time setup: choose the app password; it prints your BRIDGE KEY (keep it secret)
cd server && MPC_DATA=$PWD/data ../venv/bin/python app.py --set-password
```

Tell the server its public address (used in alert links). Edit `server/data/config.json` and add:

```json
"public_url": "https://print.example.com"
```

Run it as a service:

```bash
sudo cp ~/myprintcloud/deploy/myprintcloud.service /etc/systemd/system/
sudo nano /etc/systemd/system/myprintcloud.service      # fix User= and the paths if yours differ
sudo systemctl daemon-reload && sudo systemctl enable --now myprintcloud
curl -s 127.0.0.1:8770/api/ping                         # {"ok":true,"name":"MyPrintCloud"}
```

The server only listens on `127.0.0.1:8770`. Put HTTPS in front of it, either way works:

- **Option A, Cloudflare Tunnel** (no open ports; works on a home server too): install `cloudflared`, create a
  tunnel, and add a public hostname `print.example.com → http://localhost:8770`.
- **Option B, Caddy** (ports 80 and 443 open on the server): see `deploy/Caddyfile.example`. Caddy fetches the
  certificate itself.

Open `https://print.example.com` and sign in with your password. The printer shows offline until the bridge runs.

## 2. Set up the bridge (home PC)

1. Install **Python 3.10+** (tick "Add to PATH") and **ElegooSlicer**. If ElegooSlicer isn't in
   `C:\Program Files\ElegooSlicer`, set the `MPC_SLICER` and `MPC_PROFILES` environment variables to your paths.
2. Get the code onto the PC (download the ZIP or `git clone`), then in a terminal:
   ```bat
   cd MyPrintCloud\bridge
   pip install -r requirements.txt
   python find_printer.py
   ```
   `find_printer.py` prints your printer's address.
3. Copy `bridge\config.example.json` to `bridge\config.json` and fill it in:
   - `server`: your server URL.
   - `bridge_key`: the key from step 1.
   - `printer.host`: from `find_printer.py`.
   - `printer.access_code`: from the printer's screen.
   - `serial`: can stay empty with one printer.
4. Double-click **`Start_Bridge.bat`**. The window should say `connected to wss://…/ws/bridge`, and the app now
   shows your printer live.

If the router gives the printer a new address after a power cut, the bridge finds it again by itself.

### Optional: an Android tablet as the always-on printer link

This keeps the printer online while the PC is off. The tablet does printer + camera only; slicing stays on the PC
or the cloud slicer.

1. Install **Termux** and **Termux:Boot** from F-Droid (not the Play Store builds). Open Termux:Boot once.
2. In Termux:
   ```sh
   pkg install -y python python-numpy python-pillow
   pip install websockets paho-mqtt requests
   ```
3. Copy the `bridge/` folder to `~/mpc` on the tablet (with your `config.json`), and put `tablet/run.sh` and
   `tablet/mpc-bridge` in `~/mpc` too. Then:
   ```sh
   chmod +x ~/mpc/run.sh
   mkdir -p ~/.termux/boot && cp ~/mpc/mpc-bridge ~/.termux/boot/ && chmod +x ~/.termux/boot/mpc-bridge
   ~/mpc/run.sh &
   ```
4. On the PC, stop `Start_Bridge.bat` and run the PC as a **slicer only**: `python bridge.py --role slicer --name pc`
   (or put a shortcut to `bridge\pc_slicer_autostart.vbs` in `shell:startup` to run it hidden).

**Only ever run one printer link** (`home` or `link`). Two would keep kicking each other off the server.

Fire tablets: allow Termux to run in the background (battery settings), and use a charger that keeps up with the
screen on.

## 3. The phone app

- **Browser:** just open your server URL. On Android or iOS, "Add to Home screen" works fine.
- **Android APK** (adds a print-progress notification). Needs Node 18+ and Android Studio:
  ```bash
  # set DEFAULT_SERVER at the top of web/app.js to your URL first (you can still change it at sign-in)
  cd app
  npm install
  npm run sync                      # copies ../web into the app and syncs Capacitor
  cd android && ./gradlew assembleDebug
  # -> app/android/app/build/outputs/apk/debug/app-debug.apk
  ```

## Optional extras

### Spaghetti detection

It uses the open [Obico](https://www.obico.io) failure-detection model on the camera feed. Download it on the
server:

```bash
mkdir -p ~/myprintcloud/server/models
curl -L -o ~/myprintcloud/server/models/obico.onnx \
  https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx
sudo systemctl restart myprintcloud
```

Turn it on in the app's settings. It needs about 350 MB of free RAM; without the model the rest runs normally.

### Phone alerts

Install the ntfy app, pick a private topic name, and enter it under alerts in the app's settings.

### Cloud slicer (slice while the PC is off)

The server can slice with **OrcaSlicer**, fed the same Elegoo presets.

1. Download the OrcaSlicer Linux AppImage for your server's CPU and extract it (`./OrcaSlicer.AppImage
   --appimage-extract`).
2. Copy `bridge/` to `~/myprintcloud/slicer`, make a venv there and `pip install -r requirements.txt`.
3. Copy the Elegoo profiles folder from an ElegooSlicer install (`resources/profiles/Elegoo`) to
   `~/myprintcloud/slicer/profiles/Elegoo`.
4. Create `slicer.json` (same shape as `config.example.json`; the printer part is ignored).
5. Install `deploy/myprintcloud-slicer.service`, pointing `MPC_SLICER` at the OrcaSlicer binary.

On x86 servers the AppImage runs as-is. On ARM servers (e.g. Oracle Ampere) OrcaSlicer's libraries may need more
work. The PC slicer is the easy path.

---

## Security notes

- The app is behind your password; sessions are signed tokens. Use a long password.
- The **bridge key** lets a device act as your bridge. Treat it like a password. To rotate it, delete
  `bridge_key` from `server/data/config.json`, run `--set-password` again, and update the bridge's `config.json`.
- The printer's **access code never leaves your home**: it only lives in the bridge's `config.json`.
- Backups from the app deliberately leave out the password hash, bridge key and site logins.
- `.gitignore` keeps `config.json`, `server/data/` and logs out of git. Keep it that way.

## Troubleshooting

| Problem | Try |
|---|---|
| App says the printer is offline | Is the bridge window running and saying `connected`? Is only **one** printer link running? |
| Bridge can't reach the printer | Run `python find_printer.py`; check LAN mode and the access code; PC and printer on the same network |
| Slicing fails | Check that ElegooSlicer is installed (or `MPC_SLICER` is set); the error shown in the app comes from the slicer's log |
| Changes on the server don't show | `sudo systemctl restart myprintcloud`; servers keep old code in memory until restarted |
| Tablet link stops overnight | Battery optimisation: let Termux run unrestricted; keep it on a charger |

## License

MIT, see `LICENSE`. three.js (in `web/vendor`) is MIT-licensed by its authors. ElegooSlicer / OrcaSlicer and the
Elegoo printer profiles are not included; you install them yourself under their own licences.
