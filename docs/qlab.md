# QLab 5 setup

The projector is controlled with OSC over UDP. Two transport paths exist:

- **WiFi**: QLab → Pi `PI_IP:9000` directly.
- **LoRa**: QLab → `projektor-bridge` on the QLab Mac (`127.0.0.1:9001`) → USB LoRa adapter → Pi.
  Radio details: docs/lora.md.

The Pi accepts both at the same time and drops the second copy of an identical command
(same address + arguments) arriving via the other path within 1 s.

## 1. Network patches

QLab 5: **Workspace Settings → Network**, add patches (type OSC/UDP):

| Patch | Destination | Port | Use |
|---|---|---|---|
| (a) `Projektor WiFi` | Pi IP, e.g. `192.168.1.42` (or `10.42.0.1` on the projector hotspot) | `9000` | WiFi only |
| (b) `Projektor LoRa` | `127.0.0.1` | `9001` | via the bridge on this Mac |

(c) **Both in parallel** – two ways:

1. **Recommended:** only patch (b), and start the bridge with `--wifi-target PI_IP:9000`. The
   bridge sends every command over LoRa *and* forwards it unchanged over WiFi. One cue, two paths.
2. Two patches (a) and (b), and for each command a **Group cue** ("Start all children
   simultaneously") containing two Network cues, one per patch. More cues to maintain, but works
   without the bridge's WiFi forwarding (e.g. if the bridge runs on another machine).

Give the Pi a fixed IP (DHCP reservation on the theatre router) so the patch does not change.

## 2. Network cues

Network cue → Inspector → **Settings**: Type `OSC message`, choose the patch, enter the message.
QLab sends unquoted numbers as int/float and `"quoted"` text as string.

`ref` (cue reference): an **int** is the 1-based position in the projector's cue list, a
**string** is the cue id. `/proj/cue 3` = third cue; `/proj/cue "3"` = cue with id "3".
Pick one style and stick to it (ids are safer if you reorder cues in the web UI).

| Message | Effect |
|---|---|
| `/proj/cue "1"` | run cue id 1 (idempotent: no restart if it already plays); standby := next |
| `/proj/go` | resume at a pause point, else run the standby cue |
| `/proj/standby "4"` | set the standby cue, nothing else |
| `/proj/play "intro"` / `/proj/play "intro" 2.0` | play video or image `intro` directly (optional fade-in seconds) |
| `/proj/pause` | freeze frame |
| `/proj/resume` | continue after pause / pause point |
| `/proj/fadeout` / `/proj/fadeout 3.5` | fade to black (default: cue/video fade_out; also ends an image cue) |
| `/proj/stop` | cut to black |
| `/proj/seek 42.0` | jump to 42 s in the current video |
| `/proj/status 1` / `/proj/status 0` | show / hide the status screen (only when nothing plays) |
| `/proj/reload` | reload show.json |
| `/proj/ping` | reply `/proj/pong <device name>` |
| `/proj/state` | reply `/proj/state <json>` (WiFi only – too large for LoRa) |

Replies go back to the sender (WiFi) or via the bridge to `--reply-to` / the last sender.
QLab ignores them; to watch replies use a tool such as Protokol and `--reply-to 127.0.0.1:<port>`.

### Redundancy: prefer `/proj/cue <n>`

When sending on both paths, use **`/proj/cue <ref>`** in show cues rather than `/proj/go`.
`/proj/cue` is idempotent: if a delayed duplicate slips past the 1 s dedup window (e.g. WiFi
hiccup, LoRa repeat), the projector just confirms the cue that is already running. A late
duplicate `/proj/go` would advance one cue too far.
Likewise `/proj/stop`, `/proj/fadeout`, `/proj/pause`, `/proj/standby` are harmless if repeated.

Also note the dedup consequence: two *intentionally identical* commands within 1 s are
treated as one. That is never a problem with `/proj/cue`.

Budget: over LoRa, each command costs ~144 ms of airtime (2 copies); the EU duty-cycle limit
allows ~250 commands per hour (docs/lora.md). Fine for a show; during long programming
sessions use the WiFi patch.

## 3. Running the bridge on the Mac

Install (once; needs Python ≥ 3.11, e.g. from Homebrew):

```sh
brew install pipx && pipx ensurepath
pipx install /path/to/theater-projektor/app      # installs projektor-bridge + projektor-lora-config
projektor-bridge --list-ports                    # find the adapter, e.g. /dev/cu.usbmodem14201
```

Update after pulling new code: `pipx install --force /path/to/theater-projektor/app`.

Run manually:

```sh
projektor-bridge --serial '/dev/cu.usbmodem*' --wifi-target 192.168.1.42:9000
```

Options: `--listen 0.0.0.0:9001` (default), `--repeat 2`, `--repeat-gap-ms auto`,
`--reply-to HOST:PORT`, `--sf/--bw` (must match the adapters, only for airtime math),
`--duty-cycle 1.0`, `-v`. The serial device may be a glob; the bridge waits for the adapter
and reconnects automatically (2 s poll) if it is unplugged. Each command is logged with path and seq:

```
lora seq=20189 x2 (72 ms air each, 29 B)  /proj/cue '3'
wifi -> 192.168.1.42:9000  /proj/cue '3'
```

### Autostart with launchd

`~/Library/LaunchAgents/theater.projektor-bridge.plist` (replace `USER`, the IP and the
port glob – no shell quoting needed, the bridge expands the glob itself):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>theater.projektor-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USER/.local/bin/projektor-bridge</string>
    <string>--serial</string>
    <string>/dev/cu.usbmodem*</string>
    <string>--wifi-target</string>
    <string>192.168.1.42:9000</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/USER/Library/Logs/projektor-bridge.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USER/Library/Logs/projektor-bridge.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/theater.projektor-bridge.plist
launchctl kickstart -k gui/$(id -u)/theater.projektor-bridge     # restart
launchctl bootout gui/$(id -u)/theater.projektor-bridge          # stop / before editing the plist
tail -f ~/Library/Logs/projektor-bridge.log
```

macOS "Local Network" privacy: if the WiFi forward (`--wifi-target`) works from Terminal but not
from launchd, allow the Python binary under System Settings → Privacy & Security → Local Network,
or run the bridge once from Terminal so the prompt appears.

## 4. Pre-show check

1. Network cue `/proj/ping` on the LoRa patch → bridge log shows `reply via lora … /proj/pong`
   (TXD/RXD LEDs flash on both adapters).
2. Web UI / status screen: `lora: connected …`, OSC listening.
3. Pull the WiFi (or the LoRa adapter) and fire a `/proj/cue` – the other path must carry it.
