# Beamer control from the Pi (Android TV projector)

Goal: OSC commands such as `/proj/beamer/power on` drive the battery beamer (Android TV) from the Pi.
Status: research only, nothing implemented. Everything below marked "test" must be verified on the real beamer.

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
