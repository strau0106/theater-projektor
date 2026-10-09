# Bench test — real hardware checklist

Run this before the first rehearsal with a freshly flashed image, and again after
image/app updates. Tick the boxes, note times and anything odd.

**Equipment:** Pi 4 or 5 with the flashed SD card, powerbank (the one used in the show),
USB power meter (inline), battery beamer (or any HDMI display), Mac with QLab +
`projektor-bridge`, 2× Waveshare SX1262 LoRa USB adapters, a WiFi router/phone hotspot
you control, a laptop for SSH/web, a stopwatch.

Admin login: `theater` (password from `secrets/build.env`), `ssh theater@projektor.local`.
Useful on the Pi: `journalctl -u projektor -f`, `journalctl -u projektor-wifi -f`,
`cat /run/projektor/net.json`, `sudo projektor-wifi list`.

## 1. Boot without WiFi

- [ ] No `projektor-wifi.txt` entries, no known networks. Power on with beamer connected.
- [ ] No rainbow splash, no boot text / blinking cursor on the beamer (at most a short
      firmware/kernel logo-free black screen).
- [ ] Status screen appears. Time from power-on: ______ s (target < 40 s).
- [ ] Status screen shows WiFi state `connecting`, attempt counter and seconds to the next
      retry; with no known networks it switches to `hotspot` after the first attempt.
- [ ] Device name, version, LoRa state (`absent`), tunnel state (`disabled`/`down`) visible.

## 2. WiFi via SD card file

- [ ] Power off, put the SD card in a PC, edit `projektor-wifi.txt` on the `bootfs`
      partition: `MyNet;password;10`. Boot.
- [ ] `net.json` → `connected`, status screen shows SSID + IP. Time from power-on: ______ s
- [ ] File is renamed to `projektor-wifi.txt.imported`; `sudo projektor-wifi list` shows the network.
- [ ] Two networks with different priorities, both visible → the higher priority one is used.

## 3. Retry and hotspot fallback

- [ ] Switch the router off. Status screen: `connecting`, attempt counter increases every ~15 s.
- [ ] Switch the router back on before attempt 8 → reconnects automatically. Time: ______ s
- [ ] Router off again and wait: after 8 attempts (~2 min) hotspot `projektor-XXXX` appears.
- [ ] Join the hotspot with the laptop (password = `HOTSPOT_PASSWORD`), get 10.42.0.x by DHCP,
      open <http://10.42.0.1/> → web UI login works.
- [ ] Add/replace a network in the web UI WiFi page → projector leaves the hotspot and connects.
- [ ] Router on while in hotspot mode, nobody connected → within ≤ 5 min (rescan) it connects.
- [ ] While a laptop is connected to the hotspot, the rescan is postponed (max. 3×).
- [ ] "Retry now" button in the web UI → immediate attempt.

## 4. OSC from QLab (WiFi)

Set up QLab as in `docs/qlab.md` (network patch to `projektor.local:9000` or the IP).
For each command check the beamer and the reply (`/proj/state` or QLab log):

- [ ] `/proj/ping` → reply `/proj/pong <name>`
- [ ] `/proj/cue 1` → video 1 fades in; status screen is gone for good
- [ ] `/proj/cue 1` again while playing → no restart (idempotent)
- [ ] `/proj/go` → standby cue runs; standby advances
- [ ] `/proj/standby 3` then `/proj/go` → cue 3
- [ ] `/proj/play <video_id>` and `/proj/play <video_id> 3.0` (3 s fade)
- [ ] `/proj/pause` freezes the frame; `/proj/resume` continues without a jump
- [ ] `/proj/seek 30` jumps to 0:30
- [ ] `/proj/fadeout` (default) and `/proj/fadeout 5` → smooth fade to black, then idle
- [ ] `/proj/stop` → black immediately
- [ ] `/proj/status 1` (while idle) shows the status screen, `/proj/status 0` hides it
- [ ] `/proj/reload` after editing the show in the web UI
- [ ] `/proj/state` → JSON reply
- [ ] Unknown address / bad args / unknown cue → `/proj/error …` reply, app keeps running
- [ ] Latency QLab GO → picture change, estimated: ______ ms (with a phone slow-mo video)

## 5. Pause points and fades

- [ ] Video with pause points: playback stops exactly at each pause point (frame accurate
      within ~1 frame), `/proj/go` continues to the next.
- [ ] Cue overrides (`fade_in`, `fade_out`, `pause_points`, `end`) win over video defaults.
- [ ] `end = hold` keeps the last frame, `end = loop` loops seamlessly, `end = black` goes idle.
- [ ] Cross-cue: start cue 2 while cue 1 plays → fade out/in without black flash or stutter.
- [ ] Fades are smooth (no banding steps visible on the beamer), CPU load during a fade
      (`htop`): ______ %
- [ ] Pi 4: H.264 1080p plays without dropped frames; Pi 5: HEVC 1080p plays without dropped frames.
- [ ] Image cue (prepared 1920x1080 JPEG, fade in 2 s, fade out 3 s): fades in smoothly, stays,
      `/proj/fadeout` fades out over 3 s; correct colours, no banding.
      GO → first picture: ______ ms
- [ ] Unprepared camera photo (e.g. 4000x3000, portrait with EXIF rotation): shown upright and
      letterboxed. GO → first picture: ______ ms (if slow: `projektor-prepare` it)
- [ ] Image → video → image cue sequence by GO, and an image as the standby cue (preloaded)

## 6. LoRa path

- [ ] Configure both adapters with `projektor-lora-config` (same channel/address/air rate, EU868).
- [ ] Bridge on the Mac: `projektor-bridge` receiving QLab OSC, LoRa dongle plugged in.
- [ ] Projector with WiFi **off** (router off / hotspot mode): `/proj/cue 1` via LoRa works.
      Latency: ______ ms
- [ ] Repeat all commands of section 4 over LoRa (replies come back via LoRa, best effort).
- [ ] WiFi + LoRa in parallel (bridge `--wifi-target`): every command executed exactly once
      (dedup), check the log for "duplicate dropped".
- [ ] Range test at the venue: furthest position that still works reliably: ______ m

## 7. LoRa dongle unplug / replug

- [ ] Unplug the dongle on the projector while idle: status shows LoRa `absent`, app keeps running.
- [ ] Replug: LoRa `connected` again within ~5 s, commands work.
- [ ] Unplug/replug during playback: video continues uninterrupted.
- [ ] Same on the bridge side: bridge recovers without restart (or restart documented).

## 8. Web UI upload of a large file

- [ ] Upload a 2–4 GB video over WiFi; progress shown; time: ______ min (______ MB/s).
- [ ] Upload over the hotspot connection works too.
- [ ] Codec/resolution shown after upload; warning for unsuitable codecs.
- [ ] Interrupted upload (close the laptop lid) leaves no broken file in the media list.
- [ ] A video playing during an upload does not stutter.
- [ ] Disk full behaviour: clear error message, no crash.

## 9. Backup and restore

- [ ] `INFMK1_HOST` set, `setup-infmk1.sh` run (see `docs/infmk1.md`).
- [ ] Web UI → Backup: media + `show.json` arrive in `/srv/theater-projektor/` on infmk1,
      a `snapshots/show-<timestamp>.json` is created.
- [ ] Second backup only transfers changes (fast).
- [ ] Delete a video + change the show on the projector, restore from backup → both back.
- [ ] Backup without internet: clear error, app unaffected.

## 10. Tunnel access

- [ ] `/run/projektor/tunnel.json` → `up` within ~30 s after WiFi is connected.
- [ ] `ssh -J you@infmk1 -p 2222 theater@localhost` works from outside the venue network.
- [ ] Web UI via `ssh -N -L 8080:localhost:8080 you@infmk1` → <http://localhost:8080/>.
- [ ] Router off → tunnel `connecting`/`down`; router on → `up` again without intervention.
      Time to recover: ______ s

## 11. Power-pull recovery

- [ ] Pull power while idle, re-power → boots normally, status screen.
- [ ] Pull power during playback → boots normally; show and media intact.
- [ ] Pull power right after saving the show in the web UI → the save is either complete or
      the previous version (never a corrupt `show.json`).
- [ ] Pull power during an upload → no corrupt show; partial file is ignored/cleaned up.
- [ ] Repeat 10× in a row; `sudo journalctl -b -p warning` / `dmesg | grep -i ext4` clean.

## 12. Battery runtime

- [ ] Fully charged powerbank, USB power meter inline, beamer on its own battery.
- [ ] Idle (status screen) power draw: ______ W
- [ ] Looping 1080p video power draw: ______ W, during fades: ______ W
- [ ] Runtime looping video until shutdown: ______ h ______ min (show length + 50 % margin?)
- [ ] `vcgencmd get_throttled` after the run = `0x0` (no undervoltage). If not: better
      cable/powerbank (Pi 5 needs 5 V/5 A for full USB power; 3 A is OK without USB loads).
- [ ] Temperature after 1 h: `vcgencmd measure_temp` = ______ °C (no throttling at < 80 °C).

## 13. HDMI beamer turned on after boot

- [ ] Boot with the beamer **off** (but connected). Switch it on after the status screen
      should be visible → picture appears (status screen) within a few seconds.
- [ ] Beamer off/on during playback → picture comes back, playback timing unaffected.
- [ ] Unplug/replug the HDMI cable → picture comes back.
- [ ] Beamer with a native resolution other than 1080p → scaled picture, no "no signal".

## Results

| Date | Image (`/opt/projektor/VERSION`) | Pi model | Tester | Notes |
|---|---|---|---|---|
| | | | | |
