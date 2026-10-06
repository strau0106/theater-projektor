# Theater-Projektor — Architecture

Battery-powered wireless stage projector: Raspberry Pi 4/5 + powerbank + battery beamer.
No data cable, no power cable. Video playback controlled from QLab via OSC over WiFi
and/or LoRa (Waveshare SX1262 USB adapters).

This file is the **contract** between components. Change it first, then code.

## 1. Decisions

| Topic | Decision |
|---|---|
| Hardware | Raspberry Pi 4 or 5, HDMI to beamer, no audio |
| OS | Raspberry Pi OS Lite 64-bit, current release (Trixie; Bookworm is legacy now), no desktop |
| Stack | Python 3 (system python3-gi + GStreamer from apt, app in venv `--system-site-packages`) |
| Player | GStreamer in-process (PyGObject), GL compositing for fades |
| Control | OSC/UDP (WiFi) + OSC-over-LoRa (serial). QLab is the show controller |
| Web | FastAPI + static single-page UI (no JS build step), single password |
| WiFi | NetworkManager, multiple known networks, retry every 15 s, fallback hotspot |
| Remote | autossh reverse tunnel to `infmk1` (dedicated restricted user + key) |
| Backup | rsync over SSH to `infmk1` (separate restricted user + key, `rrsync`) |
| Image | pi-gen (arm64 branch) custom stage, built in Docker |
| Devices | Single device (no device-ID addressing) |
| Idle HDMI | Status screen from boot until first show command, then black |

## 2. Repository layout

```
ARCHITECTURE.md            this file
README.md                  quick start
app/                       Python package "projektor" (Pi app + PC dev mode + LoRa bridge)
  pyproject.toml
  src/projektor/
    __main__.py            CLI: `projektor run [--dev] [--config PATH]`
    config.py              Settings (TOML) + Show (JSON) models, load/save/validate     [core]
    controller.py          Controller: cue engine / state machine                       [core]
    commands.py            OSC address -> Controller dispatcher, cross-transport dedup  [core]
    status.py              status-screen text from Controller + net/tunnel status      [core]
    player/base.py         Player interface                                             [core]
    player/gst.py          GStreamer player                                             [core]
    player/fake.py         Fake player for tests / headless                             [core]
    transport/osc.py       UDP OSC server + replies                                     [core]
    lora_proto.py          LoRa frame encode/decode (stdlib only)                       [lora]
    transport/lora.py      Serial LoRa receiver -> dispatcher                           [lora]
    bridge.py              `projektor-bridge`: OSC/UDP in -> LoRa serial out (QLab side)[lora]
    waveshare.py           `projektor-lora-config`: configure SX1262 USB adapter         [lora]
    web/                   FastAPI app + static UI                                       [web]
    backup.py              rsync backup/restore to infmk1                                [web]
  tests/
  dev/projektor.toml       dev-mode settings (PC)
system/                    files installed on the Pi                                    [system]
  systemd/                 projektor.service, projektor-wifi.service, projektor-tunnel.service
  sbin/projektor-wifi      WiFi manager + CLI (used by web via sudo)
  sbin/projektor-tunnel    autossh wrapper writing tunnel status
  sudoers.d/projektor
  boot/                    config.txt / cmdline.txt snippets, projektor-wifi.txt template
server/setup-infmk1.sh     one-time server setup: users, keys, rrsync, permitlisten     [system]
image/                     pi-gen custom stage + build.sh                               [system]
secrets/                   GITIGNORED: build.env, generated SSH keys
docs/                      QLab setup, bench test checklist, LoRa notes
```

Ownership tags in brackets = which work package writes the file.

## 3. Runtime on the Pi

Users:
- `theater` — admin login (sudo), SSH password + key.
- `projektor` — system user running the app. Groups: `video render dialout netdev`.
  Owns `/var/lib/projektor`. Holds SSH keys for backup + tunnel in `/var/lib/projektor/.ssh/`.

Paths (all configurable in settings TOML):

| Path | Content |
|---|---|
| `/etc/projektor/projektor.toml` | settings (see §6) |
| `/var/lib/projektor/show.json` | show: videos + cues (edited by web UI) |
| `/var/lib/projektor/media/` | video files |
| `/run/projektor/net.json` | WiFi state, written by `projektor-wifi` |
| `/run/projektor/tunnel.json` | tunnel state, written by `projektor-tunnel` |
| `/boot/firmware/projektor-wifi.txt` | optional WiFi list, imported at boot (SD card editable on any PC) |

Services (systemd):
- `projektor.service` — `projektor run --config /etc/projektor/projektor.toml`, User=projektor,
  `AmbientCapabilities=CAP_NET_BIND_SERVICE` (web on port 80), Restart=always.
- `projektor-wifi.service` — WiFi manager loop.
- `projektor-tunnel.service` — autossh reverse tunnel, Restart=always.

Process model inside `projektor run`:
- Main thread: GLib MainLoop (GStreamer needs it). **All player calls happen on this thread.**
- Thread: OSC UDP server. Thread: LoRa serial reader. Thread: uvicorn (web).
- Controller public methods are thread-safe: they marshal onto the GLib loop
  (`GLib.idle_add`) and return immediately (fire-and-forget); `state()` returns a
  lock-protected snapshot.

## 4. Display / player

- Pi: GStreamer GL without X/Wayland: `glimagesink` with `GST_GL_WINDOW=gbm`, `GST_GL_PLATFORM=egl`,
  fullscreen on HDMI-A-1. Fallback: `kmssink` (no GPU fades → videobalance brightness fade in CPU).
  Player selects sink via settings `display.sink = "auto" | "gl" | "kms" | "window"`.
- PC dev mode: window (`glimagesink`/`autovideosink`), same pipeline otherwise.
- Pipeline concept (details in `player/gst.py` docstring): one permanent **display pipeline**
  `glvideomixer` (black background, video pad alpha animated for fades; a 16×16 live black
  source keeps it producing frames) `! textoverlay ! gloverlaycompositor ! glimagesink`, plus one
  **clip pipeline** per video: `playbin3` → `fakevideosink` whose handoff passes every frame
  unchanged to an `appsrc` on the mixer (a segment maps file time → display running time).
  Zero copy on the Pi 4: the V4L2 decoder's dmabufs are imported by `glupload` as EGLImages.
  The sink must advertise `GstVideoMeta` in the allocation query (fakevideosink does; appsink /
  intervideosink do not → the decoder copies every frame out of CMA and the display uploads
  it on the CPU: 80–110 % CPU in v0.1). Pause/seek/EOS work on the clip pipeline; the mixer keeps
  showing the last frame it got (pause, hold). Loops use segment seeks (no flush, no gap).
  Status screen = textoverlay on the mixer output, drawn by the GPU, toggled via `silent`.
  kms fallback / headless tests: CPU display pipeline fed through intervideosink/src.
  Measured Pi 4 1 GB, 1080p30 H.264: playback 20–25 % CPU (app process, 4 cores = 400 %), idle
  black ~9 %, status screen ~14 %; v3d memory ~140 MB idle, ~200–230 MB while playing.
  Preloading the standby cue: a clip pipeline is self-contained and only the current one feeds
  the display, so a second one can be prerolled (PAUSED at its start frame) and switched to on GO
  (not implemented yet; costs a second decoder instance + its CMA buffers).
- Video recommendation: Pi 4 → H.264 1080p (HW decode); Pi 5 → HEVC 1080p (HW) or H.264 (SW, ok ≤1080p30).
  Web UI shows codec/resolution of uploads (GstPbutils Discoverer) and warns.
- Pause points: a blocking pad probe on the clip sink lets the frame at/after the pause point
  through, holds back the next one and pauses the clip pipeline (frame accurate);
  `go`/`resume` continues.

## 5. OSC command set

Default UDP port **9000**. All addresses under `/proj`. Arguments in brackets are optional.
`ref` = cue id (string) or cue number (int, 1-based index into cue list).

| Address | Args | Effect |
|---|---|---|
| `/proj/go` | — | If paused at a pause point: resume. Else run the standby cue and advance standby. |
| `/proj/cue` | `ref` | Run cue `ref` (idempotent: if `ref` is already playing, no restart). Standby := next. |
| `/proj/standby` | `ref` | Set standby cue, no playback change. |
| `/proj/play` | `video_id [fade_in_s]` | Play video directly (no cue), defaults from video entry. |
| `/proj/pause` | — | Freeze current frame. |
| `/proj/resume` | — | Resume from pause (manual or pause point). |
| `/proj/fadeout` | `[seconds]` | Fade to black, then stop. Default = cue/video `fade_out`. |
| `/proj/stop` | — | Cut to black immediately. |
| `/proj/seek` | `seconds` | Seek in current video. |
| `/proj/status` | `0\|1` | Hide/show status screen (only when nothing plays). |
| `/proj/reload` | — | Reload show.json. |
| `/proj/ping` | — | Reply `/proj/pong <device_name>`. |
| `/proj/state` | — | Reply `/proj/state <json-string>`. |

Rules:
- Any show command (`go cue play pause resume fadeout stop seek`) permanently hides the boot status
  screen (until `/proj/status 1`).
- Replies are sent (UDP) to the sender's address and port, or to `osc.reply_host:osc.reply_port` if set.
  Over LoRa, replies go back over LoRa (best effort).
- Errors (unknown address, bad args, unknown cue) are logged, reported as `/proj/error <msg>` reply,
  and never crash the app.
- **Cross-transport dedup:** the same `(address, args)` arriving via a *different* transport within
  `osc.dedup_window_s` (default 1.0 s) is dropped. Lets QLab send over WiFi and LoRa in parallel.
  Same-transport repeats are not deduped here (LoRa layer dedups by sequence number).
  Recommend QLab use idempotent `/proj/cue <n>` rather than `/proj/go` when sending on both paths.

Controller state machine: `idle` (black or status) → `fading_in` → `playing` ⇄ `paused`
(`pause_reason`: `manual` | `pause_point`) → `fading_out` → `idle`. End of video: per `end` = `black`
(→ idle) | `hold` (stay on last frame, state `holding`) | `loop`.

## 6. Data formats

### Settings — `projektor.toml`

```toml
[device]
name = "projektor"

[paths]
data_dir = "/var/lib/projektor"          # show.json + media/
net_status = "/run/projektor/net.json"
tunnel_status = "/run/projektor/tunnel.json"

[display]
sink = "auto"            # auto | gl | kms | window
status_on_boot = true
width = 1920
height = 1080

[osc]
port = 9000
reply_host = ""          # empty = reply to sender
reply_port = 0
dedup_window_s = 1.0

[lora]
enabled = true
device = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_*"   # glob allowed
baud = 115200

[web]
port = 80
password_hash = ""       # pbkdf2/argon2 hash; empty = no auth (dev only)
max_upload_mb = 8000

[backup]
enabled = true
host = "INFMK1_HOST"
port = 22
user = "projbackup"
key = "/var/lib/projektor/.ssh/id_backup"
remote_dir = ""          # relative to rrsync root on server
```

### Show — `show.json`

```json
{
  "version": 1,
  "videos": {
    "intro": {
      "file": "intro.mp4",
      "title": "Intro",
      "in_point": 0.0,
      "out_point": null,
      "pause_points": [12.5, 40.0],
      "fade_in": 1.0,
      "fade_out": 2.0,
      "end": "black"
    }
  },
  "cues": [
    {"id": "1", "name": "Opening", "video": "intro",
     "fade_in": null, "fade_out": null, "pause_points": null, "end": null, "start_at": null}
  ]
}
```
Cue fields set to `null` inherit from the video. `end` ∈ `black | hold | loop`. Times in seconds (float).

### `/run/projektor/net.json` (written by `projektor-wifi`, read by status screen + web)

```json
{"state": "connecting|connected|hotspot|offline", "ssid": "Theater", "ip": "192.168.1.42",
 "attempt": 3, "next_retry_s": 12, "hotspot_ssid": "projektor-ab12", "hotspot_ip": "10.42.0.1",
 "updated": 1730000000}
```

### `/run/projektor/tunnel.json`

```json
{"state": "up|connecting|down|disabled", "remote": "infmk1:2222", "updated": 1730000000}
```

Readers must tolerate missing/invalid files (show "unknown").

## 7. Controller API (contract between core and web)

```python
class Controller:
    # commands — thread-safe, non-blocking, raise ProjektorError on bad input (unknown cue/video)
    def go(self) -> None
    def cue(self, ref: str | int) -> None
    def standby(self, ref: str | int) -> None
    def play(self, video_id: str, fade_in: float | None = None) -> None
    def pause(self) -> None
    def resume(self) -> None
    def fadeout(self, seconds: float | None = None) -> None
    def stop(self) -> None
    def seek(self, seconds: float) -> None
    def show_status(self, on: bool) -> None
    def reload_show(self) -> None
    # queries
    def state(self) -> dict
    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]   # returns unsubscribe
```

`state()` dict:

```json
{"state": "idle|fading_in|playing|paused|holding|fading_out",
 "pause_reason": null, "cue": "1", "cue_index": 0, "standby": "2", "standby_index": 1,
 "video": "intro", "position": 12.5, "duration": 95.0, "status_screen": true,
 "last_command": {"address": "/proj/go", "args": [], "source": "osc|lora|web", "time": 1730000000.0},
 "transports": {"osc": "listening :9000", "lora": "connected /dev/ttyACM0|absent|disabled"}}
```

Implemented additions (additive, not part of the transport contract): `state()["error"]` (last playback
error), `record_command(address, args, source)` (used by web transport endpoints),
`set_transport_status(name, text)`, `tick()`, `Player.snapshot_png()` (SIGUSR1 → `<data_dir>/snapshot.png`),
setting `display.fps` (default 30). Settings accept unknown TOML sections/keys (e.g. `[wifi]` used by
`projektor-wifi`).

Behaviour notes: `go` while *manually* paused runs the standby cue (only a pause-point pause resumes);
`end = "black"` cuts (no auto fade); switching clips cuts the old clip before starting the new one.

`ShowStore` (in `config.py`): `load() -> Show`, `save(show: Show)` (atomic write, validation),
`media_dir: Path`. Web writes show.json via `ShowStore` then calls `controller.reload_show()`.

Dispatcher: `CommandDispatcher(controller, settings).handle(address: str, args: list, source: str)
-> list[tuple[str, list]]` (replies). All transports and the web "test command" box go through it.

## 8. LoRa

Hardware: 2× Waveshare SX1262 LoRa TCXO USB adapter (850–930 MHz, use EU868 band legally: 868.x MHz,
respect duty cycle). Both in transparent (UART) mode, same channel/address/air rate.

Path: `QLab --OSC/UDP--> projektor-bridge (on QLab Mac) --serial--> LoRa ~~air~~> LoRa --serial--> Pi`.

Frame (fits one LoRa packet, ≤ 240 bytes): SLIP-delimited
`[magic 0x50 'P'][ver 1][seq u16 BE][flags u8][OSC packet bytes][CRC16-CCITT BE]`.
- Receiver drops bad CRC, dedups by `seq` (window of recent seqs, 10 s).
- Bridge sends each frame `repeat` times (default 2, spaced ~ airtime) → reliability without ACK.
- flags bit0 = reply (Pi → bridge). Bridge forwards replies to QLab as OSC/UDP (optional).
- OSC packet > frame limit → rejected with log message.

`projektor-bridge` also optionally forwards the same OSC over UDP/WiFi to the Pi (`--wifi-target`),
giving two parallel paths; Pi dedups (§5).

## 9. Network & remote access

WiFi manager (`projektor-wifi`, bash or python, root):
- Known networks = NetworkManager connection profiles with `autoconnect-priority`.
- At boot import `/boot/firmware/projektor-wifi.txt` (`ssid;psk;priority` per line, WPA-Enterprise as
  `eap;ssid;identity;password;priority;method=peap;phase2=mschapv2;anon=..;domain=..;ca=file.pem`), then rename to
  `.imported` (avoid re-import, keep for reference).
- Loop: if not connected, try known networks by priority, one attempt every **15 s**; write net.json each step.
- After `hotspot_after_attempts` (default 8 ≈ 2 min) failures: start hotspot `projektor-XXXX`
  (WPA2, password from settings, IP 10.42.0.1); web UI reachable there. Every `hotspot_rescan_s`
  (default 300) drop hotspot briefly, rescan, reconnect if a known network is visible.
- CLI for web (via sudoers, user projektor): `projektor-wifi list|add <ssid> <psk> [prio]|add-json|remove <ssid>|scan|retry`
  — JSON output. `add-json` reads `{"ssid","psk","priority","eap":{identity,password,method,phase2,
  anonymous_identity,domain_suffix,ca_cert}}` from stdin (web uses it: no passwords in argv).
  Enterprise: PEAP/TTLS/PWD, CA certificate optional (default: server cert not validated), CA PEMs stored in
  `/etc/projektor/wifi-ca/`.

Reverse tunnel (`projektor-tunnel`): `autossh -M 0 -N -o ServerAliveInterval=15 -o ServerAliveCountMax=3
-o ExitOnForwardFailure=yes -R 127.0.0.1:2222:localhost:22 -R 127.0.0.1:8080:localhost:80
-i /var/lib/projektor/.ssh/id_tunnel projtunnel@INFMK1_HOST`.
Access: `ssh -J <you>@infmk1 -p 2222 theater@localhost`.

infmk1 server (`server/setup-infmk1.sh`, run once as root):
- `projtunnel`: no shell, authorized_keys `restrict,port-forwarding,permitlisten="127.0.0.1:2222",permitlisten="127.0.0.1:8080",command="/bin/false"`.
- `projbackup`: authorized_keys `restrict,command="rrsync /srv/theater-projektor"`; backups at
  `/srv/theater-projektor/{media,show.json,snapshots/}`.
- Backup: `rsync -a --partial media/ show.json` → server; also `snapshots/show-<timestamp>.json`.
  Restore/download: reverse direction, selectable in web UI.

Keys: `image/build.sh` generates `secrets/id_backup`, `secrets/id_tunnel` (ed25519) if absent, bakes
them into the image, prints the public keys + exact `setup-infmk1.sh` invocation.

## 10. Image build

`image/build.sh`: clones pi-gen (arm64 branch, pinned commit) into `image/work/`, adds stage
`stage-projektor` after stage2 (Lite), builds via `build-docker.sh`. Output `image/deploy/*.img.xz`.
Secrets from `secrets/build.env` (gitignored): admin password, web password, hotspot password, infmk1 host,
initial WiFi list, SSH pubkey(s) for `theater`. Supports fast rebuild of only the custom stage.

Stage installs: apt (gstreamer1.0-{tools,plugins-base,good,bad,libav,gl}, python3-gi, gir1.2-gst-*,
python3-venv, network-manager, autossh, rsync), venv `/opt/projektor/venv` with the app,
users, systemd units, sudoers, boot config (`hdmi_force_hotplug`, KMS `video=HDMI-A-1:1920x1080@60D`,
disable console blanking/cursor, quiet boot), SSH (password + key for `theater`).

Power-loss note: battery device may lose power abruptly. ext4 journaling + `sync` after uploads/show saves.
Read-only overlay root is a possible later hardening step.

## 11. Testing

- `cd app && python -m venv --system-site-packages .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest`
- Dev mode on PC: `projektor run --dev` → windowed GStreamer output, web on :8080, OSC on :9000,
  data in `app/devdata/`. Test videos generated by `app/scripts/make-test-videos.sh`.
- `app/scripts/osc-send.py /proj/cue 1` for manual OSC.
- LoRa without hardware: tests use pty pairs; `socat` virtual serial link for end-to-end bridge test.
- Real-hardware bench checklist: `docs/bench-test.md`.
