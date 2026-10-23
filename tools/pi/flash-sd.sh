#!/usr/bin/env bash
# Flash a projektor image onto an SD card and preload media + show.json + state (e.g. from an
# infmk1 backup) into /var/lib/projektor, so the projector starts with its last show.
#
#   sudo tools/pi/flash-sd.sh [--with-config] image/deploy/image_<date>-theater-projektor.img.xz /dev/sdX [RESTORE_DIR]
#
# RESTORE_DIR contains show.json and media/ (layout of /srv/theater-projektor on infmk1), and
# optionally
#   state/                 results.json, design cache, ... -> /var/lib/projektor
#   config/projektor.toml  -> /etc/projektor/projektor.toml, ONLY with --with-config (the image's
#                          file is kept next to it as projektor.toml.image-default)
#   config/wifi.json       -> appended to bootfs projektor-wifi.txt (imported by NetworkManager at
#                          the next boot, then renamed to .imported - the passwords stay on the
#                          FAT partition until you delete that file)
set -euo pipefail

WITH_CONFIG=0
ARGS=()
for a in "$@"; do
	case "${a}" in
	--with-config) WITH_CONFIG=1 ;;
	*) ARGS+=("${a}") ;;
	esac
done
set -- "${ARGS[@]}"

IMG="${1:?image .img.xz}"
DEV="${2:?block device, e.g. /dev/sda}"
RESTORE="${3:-}"

die() { echo "error: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ -f "${IMG}" ] || die "${IMG} not found"
[ -b "${DEV}" ] || die "${DEV} is not a block device"
NAME="$(basename "${DEV}")"
[ "$(cat "/sys/block/${NAME}/removable" 2>/dev/null)" = 1 ] || die "${DEV} is not removable"
if [ -n "${RESTORE}" ]; then
	[ -f "${RESTORE}/show.json" ] && [ -d "${RESTORE}/media" ] || die "${RESTORE}: needs show.json + media/"
	if [ "${WITH_CONFIG}" = 1 ]; then
		[ -f "${RESTORE}/config/projektor.toml" ] || die "--with-config: ${RESTORE}/config/projektor.toml not found"
		python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "${RESTORE}/config/projektor.toml" \
			|| die "${RESTORE}/config/projektor.toml is not valid TOML"
	fi
elif [ "${WITH_CONFIG}" = 1 ]; then
	die "--with-config needs RESTORE_DIR"
fi

lsblk -o NAME,SIZE,MODEL,MOUNTPOINTS "${DEV}"
read -r -p "Overwrite ALL data on ${DEV}? Type the device name (${NAME}) to continue: " answer
[ "${answer}" = "${NAME}" ] || die "aborted"

for p in $(lsblk -ln -o PATH,MOUNTPOINT "${DEV}" | awk 'NF==2 {print $1}'); do umount "${p}"; done

echo "writing ${IMG} -> ${DEV}"
xz -dc "${IMG}" | dd of="${DEV}" bs=4M conv=fsync status=progress
sync
partprobe "${DEV}" 2>/dev/null || blockdev --rereadpt "${DEV}"
udevadm settle

if [ -n "${RESTORE}" ]; then
	ROOTFS="$(lsblk -ln -o PATH,LABEL "${DEV}" | awk '$2=="rootfs" {print $1}')"
	[ -n "${ROOTFS}" ] || ROOTFS="${DEV}2"
	[ -b "${ROOTFS}" ] || ROOTFS="${DEV}p2"
	MNT="$(mktemp -d)"
	trap 'umount "${MNT}" 2>/dev/null || true; rmdir "${MNT}"' EXIT
	mount "${ROOTFS}" "${MNT}"
	DATA="${MNT}/var/lib/projektor"
	[ -d "${DATA}/media" ] || die "${DATA}/media missing - not a projektor image?"
	OWNER="$(stat -c %u:%g "${DATA}")"
	echo "copying show.json + $(find "${RESTORE}/media" -type f | wc -l) media files (owner ${OWNER})"
	rsync -rt --chown="${OWNER}" --chmod=D750,F640 "${RESTORE}/media/" "${DATA}/media/"
	install -m 640 -o "${OWNER%:*}" -g "${OWNER#*:}" "${RESTORE}/show.json" "${DATA}/show.json"
	if [ -d "${RESTORE}/state" ]; then
		echo "copying state/ (results, design cache, ...)"
		rsync -rt --chown="${OWNER}" --chmod=D750,F640 "${RESTORE}/state/" "${DATA}/"
	fi
	CONF="${MNT}/etc/projektor/projektor.toml"
	if [ "${WITH_CONFIG}" = 1 ]; then
		[ -f "${CONF}" ] || die "${CONF} missing - not a projektor image?"
		COWNER="$(stat -c %u:%g "${CONF}")"
		[ -e "${CONF}.image-default" ] || cp -p "${CONF}" "${CONF}.image-default"
		install -m 640 -o "${COWNER%:*}" -g "${COWNER#*:}" "${RESTORE}/config/projektor.toml" "${CONF}"
		echo "settings restored (image default kept as ${CONF#"${MNT}"}.image-default)"
	elif [ -f "${RESTORE}/config/projektor.toml" ]; then
		echo "note: config/projektor.toml not applied (use --with-config)"
	fi
	if [ -f "${RESTORE}/config/wifi.json" ]; then
		BOOTFS="$(lsblk -ln -o PATH,LABEL "${DEV}" | awk '$2=="bootfs" {print $1}')"
		if [ -n "${BOOTFS}" ] && [ -b "${BOOTFS}" ]; then
			BMNT="$(mktemp -d)"
			trap 'umount "${BMNT}" 2>/dev/null || true; rmdir "${BMNT}" 2>/dev/null || true; umount "${MNT}" 2>/dev/null || true; rmdir "${MNT}"' EXIT
			mount "${BOOTFS}" "${BMNT}"
			python3 - "${RESTORE}/config/wifi.json" "${BMNT}" <<'PY'
import json, os, re, sys

src, boot = sys.argv[1:3]
nets = json.load(open(src, encoding="utf-8"))["networks"]
lines, n = [], 0
for net in nets:
    ssid, psk, prio, eap = net["ssid"], net.get("psk") or "", int(net.get("priority") or 0), net.get("eap")
    if ";" in ssid or "\n" in ssid or "\n" in psk or (not eap and ";" in psk):
        print(f"  skipped '{ssid}': characters the bootfs list cannot hold (add it in the web UI)")
        continue
    if eap:
        if ";" in eap["password"] or ";" in eap["identity"] or "\n" in eap["password"]:
            print(f"  skipped '{ssid}': ';' in identity/password (add it in the web UI)")
            continue
        opts = [f"method={eap.get('method') or 'peap'}"]
        if eap.get("phase2"):
            opts.append(f"phase2={eap['phase2']}")
        if eap.get("anonymous_identity"):
            opts.append(f"anon={eap['anonymous_identity']}")
        if eap.get("domain_suffix"):
            opts.append(f"domain={eap['domain_suffix']}")
        if eap.get("ca_cert"):
            ca = re.sub(r"[^A-Za-z0-9._-]", "_", ssid) + ".pem"
            open(os.path.join(boot, ca), "w").write(eap["ca_cert"])
            opts.append(f"ca={ca}")
        lines.append(";".join(["eap", ssid, eap["identity"], eap["password"], str(prio)] + opts))
    else:
        lines.append(f"{ssid};{psk};{prio}")
    n += 1
path = os.path.join(boot, "projektor-wifi.txt")
with open(path, "a", encoding="utf-8") as f:
    f.write("\n# restored from backup\n" + "\n".join(lines) + "\n")
print(f"{n} WiFi network(s) written to bootfs projektor-wifi.txt")
PY
			sync
		else
			echo "note: no bootfs partition found - WiFi networks not preloaded (add them in the web UI)"
		fi
	fi
	sync
	du -sh "${DATA}/media"
fi
echo "done - remove the card"
