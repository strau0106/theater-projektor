#!/bin/bash -e
# Host side of the projektor stage (runs in the pi-gen container, cwd = this dir).
# files/ is assembled by image/build.sh (system files, app wheel, generated
# settings, secrets). It is never committed.

F=files
B="${ROOTFS_DIR}/root/projektor-build"

test -f "${F}/build.env" || { echo "files/build.env missing - run image/build.sh, not pi-gen directly"; false; }
# shellcheck disable=SC1091
. "${F}/build.env"

# ---- build inputs for the chroot part (removed again at the end of 01-run-chroot.sh)
rm -rf "${B}"
install -d -m 700 "${B}"
cp -a "${F}/." "${B}/"
chmod -R go-rwx "${B}"

# ---- system files
S="${F}/system"
install -m 755 "${S}/sbin/projektor-wifi"   "${ROOTFS_DIR}/usr/local/sbin/projektor-wifi"
install -m 755 "${S}/sbin/projektor-tunnel" "${ROOTFS_DIR}/usr/local/sbin/projektor-tunnel"
for u in projektor.service projektor-wifi.service projektor-tunnel.service; do
	install -m 644 "${S}/systemd/${u}" "${ROOTFS_DIR}/etc/systemd/system/${u}"
done
install -m 644 "${S}/tmpfiles.d/projektor.conf" "${ROOTFS_DIR}/etc/tmpfiles.d/projektor.conf"
install -m 440 "${S}/sudoers.d/projektor" "${ROOTFS_DIR}/etc/sudoers.d/projektor"
install -d "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
install -m 644 "${S}/conf/journald-projektor.conf" "${ROOTFS_DIR}/etc/systemd/journald.conf.d/projektor.conf"
install -d "${ROOTFS_DIR}/etc/NetworkManager/conf.d"
install -m 644 "${S}/conf/nm-projektor.conf" "${ROOTFS_DIR}/etc/NetworkManager/conf.d/projektor.conf"

# ---- boot partition: config.txt, cmdline.txt, WiFi list
BOOT="${ROOTFS_DIR}/boot/firmware"
CFG="${BOOT}/config.txt"
if ! grep -q -- '-- projektor' "${CFG}"; then
	# more CMA for 1080p GL video (two decoders during cross fades)
	sed -i -E 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,cma-256/' "${CFG}"
	# no camera / DSI display probing: HDMI-A-1 is the only output
	sed -i -E 's/^camera_auto_detect=1/camera_auto_detect=0/; s/^display_auto_detect=1/display_auto_detect=0/' "${CFG}"
	cat "${S}/boot/config-projektor.txt" >> "${CFG}"
fi
grep -q 'vc4-kms-v3d' "${CFG}" || { echo "config.txt: vc4-kms-v3d overlay missing"; false; }

CMD="${BOOT}/cmdline.txt"
line="$(tr -d '\n' < "${CMD}")"
for tok in $(cat "${S}/boot/cmdline-projektor.txt") "cfg80211.ieee80211_regdom=${WIFI_COUNTRY}"; do
	key="${tok%%=*}"
	# replace an existing key=..., else append
	if [[ " ${line} " == *" ${key}="* ]] && [[ "${tok}" == *=* ]]; then
		line="$(echo " ${line} " | sed -E "s#( )${key//./\\.}=[^ ]*#\\1${tok}#")"
	elif [[ " ${line} " != *" ${tok} "* ]]; then
		line="${line} ${tok}"
	fi
done
echo "${line}" | xargs > "${CMD}"
echo "cmdline.txt: $(cat "${CMD}")"

install -m 644 "${F}/projektor-wifi.txt" "${BOOT}/projektor-wifi.txt"
for ca in "${F}"/wifi-ca/*.pem; do
	[ -e "${ca}" ] && install -m 644 "${ca}" "${BOOT}/$(basename "${ca}")"
done

# ---- hostname (also set by pi-gen stage1; repeated here so --fast picks up changes)
echo "${DEVICE_NAME}" > "${ROOTFS_DIR}/etc/hostname"
sed -i -E "s/^127\.0\.1\.1.*/127.0.1.1\t${DEVICE_NAME}/" "${ROOTFS_DIR}/etc/hosts"
grep -q '^127\.0\.1\.1' "${ROOTFS_DIR}/etc/hosts" || printf '127.0.1.1\t%s\n' "${DEVICE_NAME}" >> "${ROOTFS_DIR}/etc/hosts"
