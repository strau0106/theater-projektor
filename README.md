# Theater-Projektor

Battery-powered, cable-free video projector for the stage: a Raspberry Pi 4/5 on a
powerbank plays videos and still images on a battery beamer via HDMI. QLab controls it over WiFi (OSC)
and/or over LoRa radio as a fallback. Videos and the cue list are managed in a web UI;
backups and remote maintenance go through a server (`infmk1`).

- Status screen on the beamer from boot until the first show command (WiFi, IP, LoRa,
  tunnel state), then black.
- Cues with fades, pause points (stop at a frame, continue with GO), hold/loop/black at
  the end. Image cues (JPEG/PNG/WebP) fade in, stay until the next cue and fade out on
  `/proj/fadeout`, with per-image / per-cue fade times.
- Audience-vote results: a results cue shows the animated results slide (design exported from the
  vote website, cached on the Pi) with the votes the bridge fetched; see `docs/results.md`.
- WiFi: known networks with priorities, retry every 15 s, fallback hotspot
  `projektor-XXXX` (web UI at <http://10.42.0.1/>).
- Remote: reverse SSH tunnel to infmk1, rsync backup/restore of media + show.

The design and all interfaces are in **[ARCHITECTURE.md](ARCHITECTURE.md)** (binding
contract between the components).

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 4 (≥ 2 GB) or Pi 5 | Pi 4: H.264 1080p hardware decoding; Pi 5: HEVC 1080p hardware, H.264 in software |
| microSD card, 16 GB+ (A1/A2, good brand) | larger for many/long videos |
| Powerbank, USB-C PD, 5 V/3 A (Pi 5: 5 A for full USB power) | measure runtime, see bench test |
| Battery beamer with HDMI input | 1080p recommended |
| Micro-HDMI (Pi) → HDMI cable, short | use the HDMI port next to the USB-C power jack (HDMI0 / HDMI-A-1) |
| 2× Waveshare SX1262 LoRa USB adapter (EU868) | one on the Pi, one on the QLab Mac (optional) |
| Mac with QLab | runs `projektor-bridge` for the LoRa path |

## Repository

```
app/       Python package "projektor": player, OSC/LoRa, web UI, PC dev mode, LoRa bridge
system/    files installed on the Pi: WiFi manager, tunnel, systemd units, sudoers, boot config
image/     pi-gen custom stage + build.sh (Raspberry Pi OS Lite 64-bit, trixie)
server/    setup-infmk1.sh (backup + tunnel users on the server)
docs/      qlab.md, lora.md, infmk1.md, bench-test.md
secrets/   GITIGNORED: build.env, generated SSH keys
```

## Quick start: development on a PC

Needs Python ≥ 3.11 and GStreamer with PyGObject from the distribution
(Debian/Ubuntu: `sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
gstreamer1.0-plugins-{base,good,bad} gstreamer1.0-libav gstreamer1.0-gl gstreamer1.0-x ffmpeg`).

```bash
cd app
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                       # tests
scripts/make-test-videos.sh            # test videos into devdata/
.venv/bin/projektor run --dev          # window output, web http://localhost:8080/, OSC :9000
scripts/osc-send.py /proj/cue 1        # send a command
```

## Build the SD card image

Requirements: Linux PC with Docker (your user in group `docker`), `qemu-user-static` +
`binfmt-support` (aarch64), ~20 GB free in Docker's storage, Python 3, git, openssl.

1. Create `secrets/build.env` (gitignored):
   ```bash
   ADMIN_USER=theater
   ADMIN_PASSWORD='…'                     # login + sudo on the Pi (SSH and console)
   ADMIN_SSH_PUBKEYS_FILE=~/.ssh/id_ed25519.pub   # one or more files, separated by ':'
   WEB_PASSWORD='…'                       # web UI password (stored hashed)
   HOTSPOT_PASSWORD='…'                   # fallback hotspot, 8..63 characters
   DEVICE_NAME=projektor                  # hostname -> projektor.local
   INFMK1_HOST=INFMK1_HOST                # placeholder = backup + tunnel disabled
   INFMK1_PORT=22
   TUNNEL_SSH_PORT=2222
   TUNNEL_WEB_PORT=8080
   WIFI_NETWORKS="Theater;password;10|Phone;password2;5"   # optional
   WIFI_COUNTRY=DE
   ```
2. Build:
   ```bash
   image/build.sh           # full build, about an hour (pi-gen under qemu)
   image/build.sh --fast    # only the projektor stage (app/system changes), a few minutes
   image/build.sh --purge   # remove the build container + work volumes when done
   ```
   Result: `image/deploy/image_<date>-theater-projektor.img.xz`. The script prints the
   public keys and the exact `setup-infmk1.sh` command.

## Flash and first boot

- **Raspberry Pi Imager:** "Choose OS" → "Use custom" → the `.img.xz`. Choose **no** OS
  customisation (users, WiFi, SSH are already configured in the image).
- **dd:** `xzcat image/deploy/*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress`
- **bmaptool:** `sudo bmaptool copy image/deploy/*.img.xz /dev/sdX` (without `.bmap` file
  add `--nobmap`).

First boot takes a little longer (the root filesystem is expanded to the card size and
SSH host keys are generated; it may reboot once). Then the status screen appears on HDMI.

Login: `ssh theater@projektor.local` (password or your SSH key). Logs:
`journalctl -u projektor -f`. The app runs as system user `projektor` from
`/opt/projektor/venv`, settings in `/etc/projektor/projektor.toml`, show and media in
`/var/lib/projektor/`.

## WiFi configuration via the SD card

Put the card into any PC; on the FAT partition **bootfs** edit `projektor-wifi.txt`:

```
# ssid;password;priority   (higher priority is tried first, empty password = open network)
Theater-Buehne;secret-password;10
Phone-Hotspot;other-password;5
# WPA2-Enterprise: eap;ssid;identity;password;priority[;method=peap|ttls|pwd;phase2=mschapv2|pap;anon=..;domain=..;ca=file.pem]
eap;eduroam;first.last@uni.de;secret;3;anon=anonymous@uni.de
```

Enterprise networks default to PEAP/MSCHAPv2 without server certificate check; add
`domain=` and/or `ca=<pem file on bootfs>` to verify the RADIUS server. The same options
are available in the web UI (Security: WPA2 Enterprise).

At the next boot the networks are imported and the file is renamed to
`projektor-wifi.txt.imported`. Networks can also be managed in the web UI (WiFi page),
or via SSH: `sudo projektor-wifi list|add <ssid> <psk> [prio]|add-json|remove <ssid>|scan|retry`.

Without a reachable known network the projector opens the hotspot `projektor-XXXX`
(last 4 hex digits of the WiFi MAC, password = `HOTSPOT_PASSWORD`) after 8 failed attempts
(~2 min; immediately if no network is configured). Web UI: <http://10.42.0.1/>. Every
5 minutes it briefly drops the hotspot to look for known networks again.

## Connect QLab

See **[docs/qlab.md](docs/qlab.md)** (OSC network patch, cue templates, LoRa bridge) and
[docs/lora.md](docs/lora.md). Default OSC port: UDP 9000 on `projektor.local`.

## infmk1 (backup + remote access)

Run `server/setup-infmk1.sh` once on the server (as root) with the two public keys from
the build, set `INFMK1_HOST` in `secrets/build.env`, rebuild (`--fast`). Details,
tunnel usage (`ssh -J you@infmk1 -p 2222 theater@localhost`, web UI via `ssh -L`) and key
rotation: **[docs/infmk1.md](docs/infmk1.md)**.

## Backup

In the web UI: *Backup* copies `media/` and `show.json` to infmk1 with rsync (only
changes are transferred) and keeps timestamped show snapshots; *Restore* fetches them
back. Server side everything is under `/srv/theater-projektor/`.

## Before a show

Run the hardware checklist **[docs/bench-test.md](docs/bench-test.md)** after every
new image (boot, WiFi, hotspot, every OSC command, LoRa, upload, backup, tunnel, power
loss, battery runtime, beamer switched on late).

## Preparing videos and images

Convert clips to the Pi-optimal format (H.264 High, 1080p, CFR, keyframe every 0.5 s) with
`projektor-prepare *.mov -o prepared/ --upload http://projektor.local --password-file pw.txt`.
The Media tab shows a "prepared" tag. Images given to the same tool are rotated upright and
shrunk to fit 1920x1080. Details: [docs/videos.md](docs/videos.md).
