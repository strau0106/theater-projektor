# LoRa link (QLab Mac ⇄ Pi)

```
QLab ──OSC/UDP──▶ projektor-bridge (Mac) ──USB serial──▶ LoRa adapter
                                                          ~~ 868.0 MHz ~~
Pi: projektor ◀──USB serial── LoRa adapter ◀──────────────┘
```

Two **Waveshare USB-TO-LoRa-HF** adapters (SX1262, TCXO, 850–930 MHz, CH343 USB-UART)
in transparent "stream" mode. Software:

| Piece | Where | What |
|---|---|---|
| `projektor-bridge` | QLab Mac | OSC/UDP in → framed, repeated, written to the adapter; LoRa replies → OSC/UDP |
| `projektor.transport.lora` | Pi (inside `projektor run`) | adapter → decode → dedup → command dispatcher; replies back over LoRa |
| `projektor-lora-config` | anywhere | read/write adapter settings via AT commands |
| `projektor.lora_proto` | both | frame format, SLIP, CRC, dedup, airtime math |

Frame format: see ARCHITECTURE.md §8 (`SLIP( 'P' | ver 1 | seq u16 | flags | OSC | CRC16 )`,
≤ 240 bytes on the wire = one adapter packet).

## 1. What the adapter does (research summary)

Facts from the Waveshare wiki [W] and the AT presets shipped with Waveshare's serial tool [S]:

- USB: CH343 (USB VID 0x1a86). Linux: `/dev/ttyACM0`, stable name
  `/dev/serial/by-id/usb-1a86_USB_Single_Serial_<serial>-if00` (default in `projektor.toml`).
  macOS: `/dev/cu.usbmodem…` (Apple driver) or `/dev/cu.wchusbserial…` (WCH driver).
  Use `projektor-bridge --list-ports`.
- UART 115200 8N1, no flow control (default; 1200–115200 possible).
- Starts in **transparent stream mode**: bytes written to the port are sent over the air,
  received packets come out of the port. Max **240 bytes per packet**; longer writes are
  split automatically; 960 byte buffer. LoRa CRC is always on (corrupted packets never reach
  the UART).
- **No jumpers / config pins.** Configuration via AT commands on the same port:
  `+++\r\n` enters AT mode, `AT+EXIT\r\n` leaves it, settings apply after `AT+EXIT`.
  Firmware V1.1 fixed "losing AT parameters when power off" [F] → settings persist.
- Commands (set `AT+X=v`, query `AT+X?`):

  | Cmd | Meaning | Range / default |
  |---|---|---|
  | `AT+SF` | spreading factor | 7–12, default 7 |
  | `AT+BW` | bandwidth | 125/250/500 kHz, default 125 (encoding unclear, see §6) |
  | `AT+CR` | coding rate | 1–4 = 4/5…4/8, default 1 |
  | `AT+PWR` | TX power | 10–22 dBm, default **22** |
  | `AT+NETID` | network id | 0–255, default 0 |
  | `AT+LBT` | listen before talk | 0/1, default 0 (if on: up to 2 s delay) |
  | `AT+MODE` | 1 stream, 2 packet (3-byte addr+ch prefix), 3 relay | default 1 |
  | `AT+TXCH` / `AT+RXCH` | channel | 0–80 ↔ 850–930 MHz, default 18 = 868 MHz |
  | `AT+RSSI` | append RSSI byte to each received packet | 0/1, default 0 |
  | `AT+ADDR` | address | 0–65535, default 0; 65535 = listen to all |
  | `AT+KEY` | AES key | 0–65535, 0 = off |
  | `AT+BAUD`, `AT+COMM`, `AT+PORT` | UART settings | 115200, "8N1", 3 |
  | `AT+VER`, `AT+HELP`, `AT+AllP?`, `AT+RESTORE=1`, `ATE` | info / all params / factory reset / echo | |

- Stream mode addressing: sender and receiver need the **same channel and address**
  (address 65535 hears everyone on the channel).
- KEY button: hold 2 s *after* 3 s of power-on = factory reset (FW V1.2); hold within 3 s
  of power-on = firmware update mode.
- LEDs: PWR, TXD (lights while sending), RXD (lights while receiving) – very useful on stage.
- Antenna: SMA, supplied 6 dBi omni.

## 2. Chosen radio parameters (profile `projektor`, same on both adapters)

| Parameter | Value | Why |
|---|---|---|
| Channel | 18 → **868.0 MHz** | factory default, EU868 1 % sub-bands (see below) |
| SF / BW / CR | **SF7 / 125 kHz / 4/5** | fastest robust setting at 125 kHz; factory default (an unconfigured adapter still talks to us) |
| Power | **10 dBm** (minimum) | 10 dBm + 6 dBi antenna − 2.15 = 13.9 dBm ERP ≤ 14 dBm (25 mW) EU limit. The factory default 22 dBm is ~12 dB over the limit with the stock antenna |
| Mode | 1 (stream) | transparent, we do our own framing |
| Address | 20562 (0x5052, "PR") | not the default 0 → does not talk to other people's default adapters |
| NET ID | 0 | semantics in stream mode undocumented, only must match |
| LBT | off | deterministic latency (LBT may delay up to 2 s) |
| RSSI | off | (our decoder would ignore the extra byte anyway) |
| Key | 0 (off) | only a 16-bit key, unknown padding/latency; integrity comes from our CRC |
| UART | 115200 8N1 | default |

Link budget: 10 dBm + 6 dBi + 6 dBi + ~124 dBm sensitivity (SX1262, SF7/125 kHz [D])
≈ 146 dB. That is km-range outdoors; indoors through walls, scenery and people it is still
far more than a theatre needs. If range is ever a problem, go to SF9 (`--air-rate robust`,
+5 dB, but 3.4× airtime) on **both** adapters and start the bridge with `--sf 9`.

### Frequency / regulation (EU)

ERC REC 70-03 Annex 1 / ETSI EN 300 220 non-specific SRD bands [E]:
865–868 MHz and 868.0–868.6 MHz: 25 mW ERP, **1 % duty cycle** (or LBT+AFA);
868.7–869.2 MHz: 0.1 %; 869.4–869.65 MHz: 500 mW, 10 %.
The adapter has a 1 MHz channel raster (850 + ch MHz), so 868.1/868.3/868.5 (LoRaWAN) are not
selectable. Channel 18 = 868.0 MHz occupies 867.94–868.06 MHz, i.e. it touches both 1 % bands,
which have identical limits (25 mW ERP, 1 %). If you want to stay strictly inside a single
sub-band use channel 17 (867.0 MHz, entirely in 865–868 MHz) – on **both** adapters.
Channel 19 (869.0 MHz) would be the 0.1 % band: don't.

### Airtime and duty cycle

Time on air (Semtech formula, SX1261/2 datasheet §6.1.4 [D], 8 symbol preamble, explicit header,
CRC on; `lora_proto.lora_airtime_s`). The adapter adds an undocumented over-the-air header; we
assume 4 bytes (`MODULE_OVERHEAD_GUESS`).

| Command | OSC | Frame on wire | SF7/125 | SF7/250 | SF9/125 |
|---|---|---|---|---|---|
| `/proj/go` | 16 B | 25 B | 67 ms | 33 ms | 226 ms |
| `/proj/cue 12` | 20 B | 29 B | **72 ms** | 36 ms | 247 ms |
| `/proj/play "intro_video" 1.0` | 32 B | 41 B | 92 ms | 46 ms | 308 ms |
| max frame | 231 B | 240 B | 384 ms | 192 ms | — |

**1 % duty cycle = 36 s of airtime per hour per transmitter.**
A typical cue with `--repeat 2` costs 2 × 72 ms = 144 ms → **≈ 250 commands per hour**
(≈ 500 with `--repeat 1`). A normal show (tens to ~150 cues/hour) is fine, but do not
use the LoRa path for fast repeated stuff (e.g. a "go" every second for a minute during
programming = 8.6 s airtime, ¼ of the hourly budget).
The bridge accounts airtime over a sliding hour; when the budget would be exceeded it logs
a warning and sends single copies only (it never drops the first copy – the show goes on).
`/bridge/results` is handled by the bridge itself (HTTP GET of `--results-url`, timeout 3 s, not
forwarded); the resulting `/proj/results/set ja nein poll_id` frame is 49 bytes max (~15 ms airtime at
SF7/500 kHz) and goes through the normal repeat / duty-cycle path. Its reply `/proj/results/ok` is small enough for LoRa.
The Pi is a separate transmitter with its own budget; it only sends replies (`/proj/pong`,
`/proj/error`, ...). `/proj/state` replies are usually > 240 B and are not sent over LoRa.

### Latency

UART 29 B @ 115200 ≈ 3 ms + adapter packetising (undocumented, a few ms) + 72 ms air +
receiver UART ≈ 3 ms → expect **~80–110 ms** from QLab to the Pi for the first copy. The second copy
(only used if the first is lost) follows one gap later (auto gap = airtime + 20 ms ≈ 100 ms).
Commands are sent strictly in order; a burst of N commands takes N × repeat × ~100 ms.
WiFi is usually faster (few ms) – with both paths active, WiFi normally wins and the LoRa copy is
dropped by the Pi's cross-transport dedup.

## 3. Setting up the adapters

Do this once per adapter, on any computer with the app installed (Mac: `pipx install …/app`,
see docs/qlab.md). On the Pi stop the app first: `sudo systemctl stop projektor`.

```sh
projektor-bridge --list-ports                           # find the port (VID 1a86)
projektor-lora-config --port /dev/cu.usbmodemXXXX --show     # read current settings (prints raw answers)
projektor-lora-config --port /dev/cu.usbmodemXXXX --apply --dry-run   # see what would be sent
projektor-lora-config --port /dev/cu.usbmodemXXXX --apply -v          # write profile "projektor"
projektor-lora-config --port /dev/cu.usbmodemXXXX --show     # verify
```

Repeat with the second adapter. Both must end up with identical SF/BW/CR/channel/address/netid/key.
Options: `--freq 868.0` / `--channel 18`, `--air-rate fast|default|robust|far|SF7/BW125`,
`--power`, `--netid`, `--addr`, `--key`, `--raw AT+HELP` (send arbitrary commands),
`--restore` (factory reset), `--bw-style auto|index|khz`.

After configuring, plug one adapter into the Pi (any USB port; the by-id glob finds it)
and one into the QLab Mac. The Pi status screen / web UI shows `lora: connected /dev/ttyACM0`.

Quick link test (Mac):

```sh
projektor-bridge --serial '/dev/cu.usbmodem*' --reply-to 127.0.0.1:9999 -v &
python3 -c 'import socket;s=socket.socket(2,2);s.sendto(b"/proj/ping\0\0,\0\0\0",("127.0.0.1",9001))'
# bridge log: "lora seq=… x2 … /proj/ping" then "reply via lora … /proj/pong 'projektor'"
```

TXD on the Mac adapter and RXD on the Pi adapter flash for each copy.

## 4. Range / placement tips

- Screw the antennas on before powering up (TX into an open SMA can damage the PA).
- Antennas **vertical**, both the same orientation. A 6 dBi omni has a flat doughnut pattern:
  very little gain straight up/down – don't put the Mac adapter on the fly gallery directly above
  the projector.
- Use a short USB extension cable to get the adapter away from the laptop's / Pi's metal
  and electronics (USB 3 ports and HDMI cables radiate broadband noise; distance helps).
  Mount the Pi-side adapter at the top of the projector rig, not behind the powerbank.
- Bodies, steel truss, fire curtains and water (audience!) attenuate. Test with the house full
  in mind: check at the technical rehearsal with the curtain down.
- Keep it ≥ 1 m from other 868 MHz kit (wireless DMX is mostly 2.4 GHz, but some radio mics /
  IEM / intercom use 863–865 MHz).

## 5. Troubleshooting

| Symptom | Check |
|---|---|
| Pi shows `lora: absent` | adapter plugged? `ls /dev/serial/by-id/`; user `projektor` in group `dialout`; reconnect is automatic (2 s poll) |
| `lora: error: …` | port busy (another program, e.g. config tool or screen) or permissions |
| Bridge logs `NOT sent (serial absent)` | adapter unplugged / wrong `--serial` glob; it reconnects every 2 s |
| TXD flashes on Mac, no RXD on Pi | channel/address/SF/BW/netid/key differ → `--show` both; range/antenna |
| RXD flashes but nothing happens | run `projektor run` with debug logging: CRC errors → baud mismatch (both 115200?) |
| `projektor-lora-config` gets "(no answer)" | wrong baud, port held by another program, or adapter stuck in AT mode: unplug/replug; factory reset via KEY button (hold 2 s, >3 s after power-on); firmware ≥ V1.1 (`AT+VER`) |
| Commands arrive twice | they don't: same-seq copies are deduped (10 s window); WiFi+LoRa copies are deduped by the dispatcher (1 s). Use `/proj/cue <n>` for redundancy (docs/qlab.md) |
| Duty-cycle warning in the bridge log | too many commands over LoRa in the last hour; use `--repeat 1` or rely on WiFi for rehearsals |

## 6. Open points / TODO (needs the real hardware)

The Waveshare documentation is thin. Verify these with the first real adapter and update this file:

- **TODO** AT answer format (e.g. `OK`, `+SF=7`, `7`?) is not documented. The config tool prints
  raw answers and parses "the last integer" for verification. Run `--show -v` and record the output.
- **TODO** `AT+BW` encoding: wiki says `0/1/2`, Waveshare's sscom presets say `125/250/500`.
  `--bw-style auto` reads `AT+BW?` first and uses the style of the answer (default 125 kHz must
  read back as `0` or `125`).
- **TODO** channel → frequency: wiki only gives "0–80 ↔ 850–930 MHz, 18 = 868 MHz" (assumed
  850 + ch MHz). EBYTE-style modules use 850.125 + ch. Check with an SDR (RTL-SDR + `rtl_power`/SDR#)
  if you care about the exact centre.
- **TODO** NET ID semantics in stream mode (documented only for relay addressing). Keep equal.
- **TODO** over-the-air overhead of the adapter (address/netid header, AES padding) – affects
  airtime estimates only (guess: 4 bytes).
- **TODO** how the adapter splits the UART stream into packets (idle timeout?). The bridge writes
  each frame in one `write()` and waits ≥ airtime between copies, so each copy should be its own
  packet; even if two copies were merged, the SLIP decoder handles concatenated frames.
- **TODO** guard time around `+++` (tool waits 200 ms before; worked for Waveshare's GUI tool,
  which sends `+++\r\n` as a normal line). Our SLIP frames never contain the text `+++\r\n`.
- `AT+AllP?` parameter order (from the sscom preset `AT+AllP=7,125,1,22,0,0,1,18,18,0,0,3,115200,"8N1",0`):
  SF, BW, CR, PWR, NETID, LBT, MODE, TXCH, RXCH, RSSI, ADDR, PORT, BAUD, COMM, KEY (deduced, unverified;
  the tool does not write via `AllP`).

## Sources

- [W] Waveshare wiki, USB-TO-LoRa-xF: <https://www.waveshare.com/wiki/USB-TO-LoRa-xF> (rev. oldid=108187)
- [S] Waveshare sscom AT presets: <https://files.waveshare.com/upload/0/0b/SX1262-LoRa-DTU-sscom5.13.1.zip> (`AT_Stream_Mode/sscom51.ini`)
- [F] Firmware package + changelog: <https://files.waveshare.com/upload/0/0b/USB-TO-LoRa-Update.zip> (`README.txt`, V1.2)
- [D] Semtech SX1261/2 datasheet (time-on-air §6.1.4, sensitivity): <https://files.waveshare.com/upload/e/e1/DS_SX1261-2_V1.2.pdf>
- Semtech AN1200.13 "LoRa Modem Designer's Guide" (time-on-air formula)
- [E] CEPT ERC Recommendation 70-03, Annex 1 (non-specific SRD, bands h1.3–h1.7) and ETSI EN 300 220-2:
  <https://docdb.cept.org/document/845>
- LoRaWAN Regional Parameters RP002 (EU868 channel plan, for comparison): <https://lora-alliance.org/wp-content/uploads/2019/11/rp_2-1.0.0_final_release.pdf>

## Hardware test results (2026-10-06, 2× USB-TO-LoRa-HF, firmware Ver1.2)

Tested PC (bridge) ↔ Pi 4 (projektor), same room, factory settings left as found:

- Both adapters answered `AT+ALLP?` with `+ALLP=7,0,1,10,0,0,1,18,18,0,0,1,"8N1",115200,0`,
  i.e. SF7, `AT+BW` uses the **index** style (`+BW=0` = 125 kHz), CR 4/5, **10 dBm**, net 0,
  LBT off, stream mode, TX/RX channel 18 (868 MHz), RSSI off, addr 0, 115200 8N1.
  The answer format is `\r\n+KEY=value\r\n\r\nOK`; `+++` itself gives no answer when the
  module is already idle. No `--apply` was needed for the link to work.
- Commands: 100 % delivered; one-way latency ≈ 90 ms (`/proj/ping` RTT 178 ms with a single copy).
- **Half duplex pitfall (fixed):** with `--repeat 2` the Pi's immediate reply collided with the
  bridge's second copy and was lost (0/5 pongs). The Pi now delays LoRa replies by
  `lora.reply_delay_ms` (250) + airtime × (`lora.sender_repeat` − 1) × 2 → 10/10 pongs,
  RTT ≈ 560 ms. Commands themselves are executed immediately.
- WiFi + LoRa in parallel (`--wifi-target`): WiFi copy arrives first, LoRa copy is dropped by the
  cross-transport dedup ("dedup: dropped ... from lora").
- Not yet tested: range on stage, behaviour with the beamer/powerbank nearby, `--apply` of the
  "projektor" profile (not required while both adapters keep identical factory settings).
- Pitfall: never run `projektor-lora-config` on the Pi while the `projektor` service runs — both
  open the same serial port and the AT answers get mixed up. `sudo systemctl stop projektor` first.

## Air-rate test (2026-10-06)

`/proj/ping` RTT via the bridge (`--repeat 1`, Pi `sender_repeat=1`, `reply_delay_ms=0`), 30 pings each, 10 dBm:

| Setting | RTT min/avg/max | Loss |
|---|---|---|
| SF7 / 125 kHz (old) | 178/178/179 ms | 1/30 |
| SF7 / 250 kHz | 100/101/102 ms | 0 |
| SF7 / 500 kHz | 60/62/63 ms | 0 |
| SF6 / 500 kHz | 61/62/63 ms | 0 |
| SF5 / 500 kHz | 61/62/63 ms | 0 |

SF5/SF6 are accepted by the module (`AT+SF=5` -> `+SF=5`) but `projektor-lora-config --sf` rejects them;
set them with `--raw "AT+SF=5"`. They give no RTT gain (about 60 ms is a fixed floor) and cost sensitivity.

Chosen: **SF7 / 500 kHz**, CR 4/5, 10 dBm (about 3x faster than SF7/125). With `--repeat 2` and the default Pi
reply delay the pong RTT is about 446 ms (10/10).

Apply to both adapters (Pi: stop the service first; `--power 10` is needed, the factory profile would set 22 dBm):

    projektor-lora-config --port /dev/ttyACM0 --apply --profile factory --sf 7 --bw 500 --bw-style index --power 10
    tools/pi/dev.sh stop; tools/pi/dev.sh lora --apply --profile factory --sf 7 --bw 500 --bw-style index --power 10; tools/pi/dev.sh start

Bridge flags: `--sf 7 --bw 500` (airtime and duty-cycle math).

Band note: channel steps are 1 MHz, channel 18 = 868.0 MHz centre. A 500 kHz signal there covers 867.75-868.25 MHz,
so half of it lies below the 868.0-868.6 MHz sub-band (1 % duty cycle); even 125 kHz at 868.0 straddles the edge.
A centre of 868.3 MHz would fit (868.05-868.55) but the module cannot do it with 1 MHz steps; check whether the
EU868 limits for 867.75-868.0 apply to this use, or choose the legal channel plan before production.
