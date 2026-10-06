# Beamer control from the Pi (Android TV projector)

Goal: OSC commands such as `/proj/beamer/power on` drive the battery beamer (Android TV) from the Pi.
Status: CEC backend implemented (see "Implemented"); other backends are research only. Everything below marked "test" must be verified on the real beamer.

## Implemented (CEC)
Code: `app/src/projektor/beamer.py`, settings `[beamer]` (`enabled`, `device="/dev/cec0"`, `phys_addr="1.0.0.0"` or `""` = auto,
`power_on_at_boot`, `standby_at_shutdown`, `cec_ctl="cec-ctl"`). Package `v4l-utils` is in the image.
- OSC: `/proj/beamer/power on|off|1|0`, `/proj/beamer/status` (reply `/proj/beamer/status on|standby|unknown|unavailable`), `/proj/beamer/key <name>`.
- Web: `GET /api/beamer`, `POST /api/beamer/power {"on": bool}`, `POST /api/beamer/key {"key": name}`; Live tab "Beamer" row, key pad on the System tab.
- Power/key via OSC run in a worker thread; every `cec-ctl` call has a 6 s timeout. Missing tool/device: status `unavailable`, other calls report an error, the app keeps running.
- `cec-ctl` calls (all with `-d /dev/cec0`): setup `-p 1.0.0.0 --playback -o Projektor`; on `--to 0 --image-view-on` (3 tries, 2 s gap) then
  `--to 15 --active-source phys-addr=1.0.0.0`; standby `--to 0 --standby`; status `--to 0 --give-device-power-status` (parses `pwr-state:`);
  key `--to 0 --user-control-pressed ui-cmd=<cmd>` then `--to 0 --user-control-released`
  (`back`=exit, `home`=root-menu, `menu`=setup-menu; untested on the beamer).

## Facts from the repo
- HDMI is KMS (`vc4-kms-v3d`), `hdmi_force_hotplug` is set for both ports, so the Pi outputs even when the beamer is off.
  CEC needs a valid physical address (read from the sink EDID), so after a forced-hotplug boot the beamer may have to be
  connected/on once, or the address set by hand (`cec-ctl -p 1.0.0.0`).
- Bluetooth is disabled (`dtoverlay=disable-bt`) because WiFi and BT share one radio/antenna.
- Not installed today: `v4l-utils`, `ir-ctl` (also v4l-utils), `androidtvremote2`, `adb`.
- OSC namespace is `/proj/...` (ARCHITECTURE.md section 5); beamer commands would be added there.

## Comparison

| | 1. HDMI-CEC | 2. Android TV Remote v2 (WiFi) | 3. ADB over WiFi | 4. IR from GPIO | 5. Bluetooth HID |
|---|---|---|---|---|---|
| Wake from standby | Yes if the beamer keeps CEC alive in standby (many do, many cheap ones do not) | Only if WiFi stays up in standby (usually not on projectors) | Same as 2, usually not | Yes, works from full standby | Only if BT wake is supported, rarely |
| Power off | Standby message | KEYCODE_POWER / sleep | `input keyevent 223` | Yes | Partly |
| HDMI input select | Active Source / Image View On often switches input | Key `TV_INPUT` / HDMI keys, depends on firmware | `am start` TV input intent, firmware dependent | Yes, if remote has the key | Partly |
| Menu, nav, volume, keystone/focus | Nav/volume keys via User Control Pressed, keystone/focus usually not | Standard keys only | Standard keys, settings via `settings put` (brightness maybe) | Whatever the original remote sends, incl. keystone/focus | Standard HID keys |
| Latency | ~100 ms | 100-300 ms (TLS session), cold connect 1-2 s | 100-500 ms | ~100 ms | ~100 ms |
| Reliability on stage | Good if supported; no network dependency | Needs shared WiFi, pairing state, TV network stack | Needs shared WiFi, authorization, can be disabled by firmware | Best: no network, but line of sight | Fiddly pairing, shares radio with WiFi |
| Setup effort | `apt install v4l-utils`, 0 hardware | pip lib, one-time pairing code on screen | Developer options, `adb`, authorize key | IR LED + transistor + resistor, overlays, learn codes | Re-enable BT, BlueZ HID, pair |
| Verdict | Try first | Fallback for keys/apps when beamer is awake | Only if 2 fails | Wake/power insurance | Not recommended |

### 1. CEC details
- Pi 4: `vc4_hdmi` registers `/dev/cec0` (HDMI0, the port next to USB-C) and `/dev/cec1`. Use HDMI0 for the beamer.
- Android TV typically honours: Image View On / Text View On (wake and switch to source), Standby, Active Source,
  Set Stream Path, User Control Pressed/Released (Power, Up/Down/Left/Right, Select, Back, Menu/Root Menu, Volume, Mute).
  Keystone, focus and brightness are normally not reachable via CEC.
- Caveat: CEC must be enabled in the beamer settings (often named "HDMI-CEC", "Anynet+", "Simplink", "Device Auto Power On").
  Many cheap Android projectors have no or very limited CEC, and some drop CEC in standby.
- Python: no good library needed; call `cec-ctl` via subprocess (kernel API, no daemon). libcec/`cec-client` is the older alternative; on KMS use `cec-ctl`.

### 2. Android TV Remote v2
- `androidtvremote2` is the library used by the Home Assistant Android TV Remote integration (maintained by the HA team,
  packaged in Debian). Async API: pair once (code shown on TV, ports 6466 pairing / 6467 remote), then `send_key_command("POWER" | "HOME" | "DPAD_UP" ...)`
  and `send_launch_app_command(url)`. Certificates must be persisted in the app config.
- It has no wake-on-LAN; power on works only if the beamer's network stack is alive in standby (test).
- Needs the beamer and the Pi on the same network (see below).

### 3. ADB
- Developer options + network debugging, `adb connect <ip>:5555`, `adb shell input keyevent 26` (power toggle) / `224` (wake) / `223` (sleep),
  `settings put system screen_brightness N`. Security: unauthenticated-ish control of the beamer for anyone on the network; only use on the private show network. Many Android TV firmwares disable ADB after reboot or in standby.

### 4. IR
- TX: `dtoverlay=gpio-ir-tx,gpio_pin=18` (creates `/dev/lirc0`), IR LED (940 nm) via NPN transistor (e.g. 2N2222, ~100 ohm LED resistor, base resistor 1k), ideally 2 LEDs for range.
- RX for learning: `dtoverlay=gpio-ir,gpio_pin=17` plus a TSOP38238 receiver (different pin; the TX and RX use separate lirc devices). Both overlays are in the stock Raspberry Pi kernel.
- Learn: `ir-ctl -d /dev/lirc1 --receive=power.txt`, replay: `ir-ctl -d /dev/lirc0 --send=power.txt`. Decode with `ir-keytable` if the protocol is NEC.
- Needs line of sight to the beamer's IR window (many beamers have it front and rear). Works from full standby, no network. Best on-stage insurance.

### 5. Bluetooth HID
Possible with BlueZ (HID device profile / `bluez-hid2hci`-style tools, or a USB BLE dongle running a HOGP gadget), but requires re-enabling BT (conflicts with the WiFi radio decision),
pairing state is fragile, and it only gives standard keys. Skip; a USB BT dongle would be the only sane variant if ever needed.

## Network
The beamer must be on a network reachable by the Pi to use options 2 and 3. If the Pi falls back to its own NetworkManager hotspot
(`ipv4.method shared`), the beamer would have to be joined to that hotspot, and when the Pi is on the venue WiFi the beamer needs credentials for that too.
Android TV remembers several networks, but roaming between them in standby is not reliable. Treat WiFi-based control as
"works only when the beamer is awake and on the same network". CEC and IR do not depend on this.

## Recommendation
1. CEC first (zero hardware, no network): power on (Image View On + Active Source), standby, input, navigation keys.
2. Android TV Remote v2 as fallback for keys/apps while the beamer is awake and on the show network.
3. Build the IR blaster (about 5 EUR of parts) if the beamer's CEC does not wake it from standby, or if keystone/focus control is wanted. This is the only option that
   is certain to work from deep standby; decide after the tests below.
Skip ADB (security, fragile) and Bluetooth.

## Proposed OSC commands
| Address | Args | Action |
|---|---|---|
| `/proj/beamer/power` | `on\|off` | on: CEC Image View On + Active Source (fallback: IR power); off: CEC Standby |
| `/proj/beamer/input` | `hdmi1\|hdmi2\|...` | CEC Active Source / Set Stream Path; fallback IR or Remote v2 key |
| `/proj/beamer/key` | `name` | named key (`up down left right ok back menu home vol_up vol_down mute`, IR-only: `keystone_up keystone_down focus_up focus_down`) |
| `/proj/beamer/status` | - | reply with `cec-ctl --to 0 --give-device-power-status` result (`on\|standby\|unknown`) |

Backends sit behind one `beamer.py` interface (`cec`, `atvremote`, `ir`), configured in the device config with a priority order. Commands run in a worker thread
(never block the OSC/player loop), are idempotent, and report failures as `/proj/error`. Power-on should retry (e.g. 3 x with 2 s gap) since CEC from standby can be slow.

## Test on the real beamer (on the Pi)
```
sudo apt install v4l-utils
cec-ctl -d /dev/cec0 --playback -S                   # configure as playback device, show topology; is the beamer (logical address 0 = TV) listed?
cec-ctl -d /dev/cec0 --show-topology
cec-ctl -d /dev/cec0 --to 0 --give-device-vendor-id
cec-ctl -d /dev/cec0 --to 0 --give-device-power-status   # reports On / Standby
cec-ctl -d /dev/cec0 --to 0 --standby                    # beamer goes to standby?
# wait 30 s and 5 min, then:
cec-ctl -d /dev/cec0 --to 0 --image-view-on              # wakes from standby?
cec-ctl -d /dev/cec0 --to 0 --active-source phys-addr=1.0.0.0   # switches input to the Pi?
cec-ctl -d /dev/cec0 --to 0 --user-control-pressed ui-cmd=up; cec-ctl -d /dev/cec0 --to 0 --user-control-released   # navigation keys
cec-ctl -d /dev/cec0 --to 0 --user-control-pressed ui-cmd=volume-up
cec-ctl -d /dev/cec0 -M                                   # monitor: press keys on the original remote, see what the beamer emits
```
Notes: if "no devices found", enable CEC in the beamer settings and try `-p 1.0.0.0` / the other HDMI port (`/dev/cec1`).
Test both with the beamer freshly powered via the Pi's HDMI and in "forced hotplug" boot order. Repeat the wake test after the beamer has been in standby for 30+ minutes
(deep standby is where many beamers lose CEC).

Remote v2 test (needs `pip install androidtvremote2` on the Pi, same network):
```
python3 -c "import asyncio,androidtvremote2 as a; print(a.__file__)"   # install check
# use the library's pairing flow (async_start_pairing / async_finish_pairing with the code on the TV), then send_key_command('HOME'), then put beamer in standby and try 'POWER'
```
Also check: does the beamer answer `ping` and port 6466/6467 (`nc -zv <ip> 6467`) while in standby? If not, WiFi cannot wake it.

IR test (only if CEC wake fails): add the overlays below to `system/boot/config-projektor.txt`, reboot, learn and replay `power.txt`, `hdmi.txt`, `keystone*.txt`.

## Packages and overlays
- CEC: `v4l-utils` (no overlay; the vc4 KMS driver provides `/dev/cec0`).
- Remote v2: `androidtvremote2` (pip, into the app venv), `cryptography`, `aiofiles` pulled in as dependencies.
- IR: `v4l-utils` (`ir-ctl`, `ir-keytable`); `dtoverlay=gpio-ir-tx,gpio_pin=18` and `dtoverlay=gpio-ir,gpio_pin=17`; parts: IR LED 940 nm, NPN transistor, resistors, TSOP38238.
- Keep `dtoverlay=disable-bt`.

## Effort estimate
- CEC backend + OSC commands + status: about 0.5 day (plus beamer testing).
- Remote v2 backend (pairing CLI, key store, reconnect): about 1 day.
- IR: about 0.5 day hardware plus 0.5 day software (code learning, send).
- Docs/QLab cue examples: 1-2 hours.

## Bluetooth remote wake (XGIMI Halo+)

Measured: CEC `<Standby>` powers off fully, CEC power-toggle gives "sleep" (LED on) but CEC is dead there, `image-view-on` cannot wake it. The original remote wakes it, with no IR light, so the wake path is Bluetooth.

### How the wake most likely works
Only partly sourced; the rest is inference and must be verified by the experiment below.
- The XGIMI remote is a Bluetooth remote (pairing: hold Home + Return within 10 cm; blue LED flashes; the projector plays a "ding" on success; the manual says the power key turns the unit on with a 2 s press). Source: Halo manual (https://www.manualslib.mx/manual/707356/Xgimi-Halo.html) and AVNirvana review (https://www.avnirvana.com/threads/xgimi-halo-1080p-led-portable-projector-review.9773/).
- General BLE fact: a central cannot see a peripheral unless it advertises, so a sleeping host must keep its BT controller scanning with a filter and the remote must advertise (typically directed or undirected connectable advertising after a key press) and then reconnect (HOGP). Source: Nordic DevZone thread (https://devzone.nordicsemi.com/f/nordic-q-a/61126/remote-wake-up) and Nordic/TI HID keyboard docs: after a disconnect a HID device uses directed advertising to its bonded host (https://infocenter.nordicsemi.com/topic/com.nordic.infocenter.sdk5.v12.2.0/ble_sdk_app_hids_keyboard.html).
- Inference (not found documented for XGIMI): the SoC keeps the MediaTek BT controller powered in sleep with a wake/allow-list filter on the bonded remote's address (or its IRK-resolved RPA). A remote key press then makes the controller wake the SoC; the key itself is delivered after reconnect. I found no public spec of an XGIMI-specific wake packet or manufacturer data, and no remote model number tied to this behavior (replacement remote listings are for B932C-class remotes, see https://www.pbtech.com/product/MOAXGM12501/XGIMI-B932C-Remote-Controller-For-XGIMI-Halo--MoGo, unverified for Halo+).
- Precedent that a standard BLE keyboard can wake an Android TV box: Mi Box S kept Bluetooth on in sleep and an ESP32 BLE keyboard paired to it woke it by sending a key (Home Assistant community, ESPHome BLE keyboard: https://community.home-assistant.io/t/esphome-ble-keyboard/359552?page=2, projects https://github.com/dmamontov/esphome-blekeyboard and https://github.com/dionifreitas/playr-remote). This suggests a plain bonded HID keyboard is enough if the host filters on the bonded address, but XGIMI may differ (it may only accept its own remote or vendor GATT).

### Options, ranked
| # | Option | Feasibility | Reliability | Effort | Hardware |
|---|--------|-------------|-------------|--------|----------|
| 1 | Pi built-in BT as BLE HID keyboard (BlueZ), bond to the XGIMI, send power/key, rely on reconnect advertising for wake | medium; unproven for sleep | unknown; BlueZ HOGP peripheral mode is fiddly, Pi's radio is shared with WiFi | 1-2 days | none |
| 2 | ESP32 (BLE keyboard lib or ESPHome) on Pi USB/UART acting as the remote, Pi sends commands over serial | medium-high (same protocol, precedent on Android TV boxes); same unknown on wake filter | better than 1: dedicated radio, mature NimBLE HID stacks, reconnect/directed adv controllable | 1-2 days | ESP32 board, about 10 EUR |
| 3 | Original remote with optocoupler/relay across its power button, driven by Pi GPIO | certain | high (it is the real remote; needs its battery and BT link in range) | 0.5 day | opto/relay, soldering, remote kept inside the case |
| 4 | Keep beamer awake (disable sleep; CEC power only for picture), never "shutdown" | high | high, but energy and lamp-less LED heat; only if a "never sleep" setting exists | hours | none |
| 5 | Spoof the remote's BD_ADDR on the Pi (btmgmt/bdaddr) and replay captured adverts | low | low | days | none; the CYW43455 supports changing the public address only via vendor HCI (`bdaddr`-style tools exist for Broadcom, unverified on trixie); static random address via `btmgmt static-addr` is easy but is not the remote's public address; bonded RPAs with IRK cannot be reproduced without the remote's keys |

Recommendation: do the capture experiment first (cheap, 30 min). Then try option 2 (ESP32) or 1 depending on what the capture shows. Option 3 is the fallback that always works. Option 4 is worth checking in XGIMI settings regardless.

### Impact of enabling Bluetooth on the Pi 4
- Pi 4 CYW43455 does WiFi and BT on one chip with coexistence (separate UART/SDIO paths, shared 2.4 GHz antenna). On 5 GHz WiFi coexistence is not an issue; on 2.4 GHz expect occasional latency spikes during BLE scan/advertising. Check which band the Pi uses (`iw dev wlan0 link`) before enabling; prefer 5 GHz or Ethernet for OSC.
- LoRa is USB, unaffected. OSC latency: BLE advertising/connection events are short and low duty; keep scan windows off except during the experiment. (General knowledge, not measured here.)
- Enable: remove `dtoverlay=disable-bt` from `system/boot/config-projektor.txt` (image) and reboot; also `sudo systemctl enable --now bluetooth`.

### Next experiment (on the Pi)
1. Enable BT: comment out `dtoverlay=disable-bt` in /boot/firmware/config.txt (or the projektor config), `sudo apt install bluez`, reboot. Check `rfkill list; hciconfig -a` (or `btmgmt info`).
2. Put the beamer to sleep (LED on). Close to it, run in two terminals: `sudo btmon -w /tmp/wake.btsnoop` and `sudo bluetoothctl` then `scan on` (or `sudo hcitool lescan --duplicates`; `btmgmt find -l`).
3. Note whether the beamer itself appears while sleeping (name "XGIMI Halo", address); if it does not advertise, a Pi-as-remote can still work only if it initiates the connection, which a sleeping host typically only accepts from an allow-listed address.
4. Press power on the original remote (covered). In btmon look for the remote's advertising PDU (ADV_IND/ADV_DIRECT_IND), its address type (public/RPA), flags, appearance (0x03C1 keyboard / 0x0180 remote), service UUID 0x1812 (HID), manufacturer data, then the connection and HOGP traffic. Repeat 5 times to see if the payload is constant. Save the btsnoop and open it in Wireshark.
5. Check while awake: `bluetoothctl scan on` with the beamer on; `info <remote addr>` shows UUIDs. Then in beamer settings, add a new BT device and see whether the Pi (advertising as a keyboard via `bluetoothctl` `advertise on` / `btmgmt` with appearance 0x03C1 and UUID 1812) is discoverable as a remote; if the XGIMI only lists audio devices, option 1/2 via the Bluetooth settings menu will not work and options 3/4 stay.
6. If the XGIMI accepts a keyboard: pair, send a key (e.g. from an ESP32 or `bluez-hid2hci`/`hid-gadget`-style python-dbus HOGP script), then put it to sleep and press the key again.

### Risks
- XGIMI may only wake on its own remote (vendor-specific address filter or GATT/UUID); then only option 3 or 4 work.
- BlueZ is a weak HOGP peripheral: bonding keys, reconnect advertising and the `input` plugin may need configuration (`bluetoothd -P input`/experimental `-E`); an ESP32 is simpler.
- Radio coexistence with WiFi on 2.4 GHz; BT on in a show environment adds RF noise and security surface (set non-discoverable, no pairing outside tests).
- Pairing another BT device may evict the original remote if the projector supports one remote only; re-pair with Home + Return.
- BD_ADDR spoofing on CYW43455 is vendor-specific and unreliable; skip unless all else fails.
