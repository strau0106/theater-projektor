#!/usr/bin/env bash
# Build the theater-projektor Raspberry Pi OS image (pi-gen in Docker).
#
#   image/build.sh            full build: pi-gen stage0-2 (Lite) + stage-projektor (~1 h)
#   image/build.sh --fast     rebuild only stage-projektor on top of the previous stage2
#                             (app / system file changes; needs one full build first)
#   image/build.sh --purge    delete the build container, its volumes and the pi-gen image
#
# Inputs:  secrets/build.env (gitignored), app/, system/, image/stage-projektor/
# Output:  image/deploy/image_<date>-theater-projektor.img.xz
# Keys:    secrets/id_backup, secrets/id_tunnel (generated once, ed25519)
#
# Requirements: docker (user in group docker), qemu-user-static + binfmt (aarch64),
# python3 with pip, git, ssh-keygen, openssl.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "${HERE}")"
WORK="${HERE}/work"
PIGEN="${WORK}/pi-gen"
DEPLOY="${HERE}/deploy"
SECRETS="${ROOT}/secrets"
ENV_FILE="${SECRETS}/build.env"

# pi-gen pinned to the release tag of the current 64-bit Raspberry Pi OS (Debian 13 trixie)
PIGEN_REPO="https://github.com/RPi-Distro/pi-gen.git"
PIGEN_TAG="2026-09-15-raspios-trixie-arm64"          # on branch arm64
PIGEN_COMMIT="2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268"
CONTAINER="pigen_theater_projektor"
IMG_NAME="theater-projektor"

MODE=full
case "${1:-}" in
	"") ;;
	--fast) MODE=fast ;;
	--purge) MODE=purge ;;
	-h|--help) sed -n '2,15p' "$0"; exit 0 ;;
	*) echo "unknown option: $1" >&2; exit 2 ;;
esac

log() { printf '\n=== %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

if [ "${MODE}" = purge ]; then
	docker rm -v "${CONTAINER}" 2>/dev/null || true
	docker rmi pi-gen 2>/dev/null || true
	echo "removed container ${CONTAINER} (with work volumes) and image pi-gen"
	exit 0
fi

T0=$(date +%s)

# ---------------------------------------------------------------- settings
[ -f "${ENV_FILE}" ] || die "${ENV_FILE} missing (see README.md)"
# shellcheck disable=SC1090
. "${ENV_FILE}"
ADMIN_USER="${ADMIN_USER:-theater}"
DEVICE_NAME="${DEVICE_NAME:-projektor}"
INFMK1_HOST="${INFMK1_HOST:-INFMK1_HOST}"
INFMK1_PORT="${INFMK1_PORT:-22}"
TUNNEL_SSH_PORT="${TUNNEL_SSH_PORT:-2222}"
TUNNEL_WEB_PORT="${TUNNEL_WEB_PORT:-8080}"
WIFI_NETWORKS="${WIFI_NETWORKS:-}"
WIFI_CA_FILES="${WIFI_CA_FILES:-}"
WIFI_COUNTRY="${WIFI_COUNTRY:-DE}"
HDMI_PORT="${HDMI_PORT:-0}"   # 0 = HDMI0 (next to USB-C power), 1 = HDMI1
[ -n "${ADMIN_PASSWORD:-}" ] || die "ADMIN_PASSWORD empty in build.env"
[[ "${ADMIN_USER}" =~ ^[a-z][-a-z0-9_]*$ ]] || die "invalid ADMIN_USER"
[[ "${DEVICE_NAME}" =~ ^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}$ ]] || die "invalid DEVICE_NAME (hostname)"
[[ "${WIFI_COUNTRY}" =~ ^[A-Z]{2}$ ]] || die "WIFI_COUNTRY must be a 2-letter code"
[[ "${HDMI_PORT}" =~ ^[01]$ ]] || die "HDMI_PORT must be 0 or 1"
HDMI_CONNECTOR="HDMI-A-$((HDMI_PORT + 1))"
if [ -n "${HOTSPOT_PASSWORD:-}" ] && { [ ${#HOTSPOT_PASSWORD} -lt 8 ] || [ ${#HOTSPOT_PASSWORD} -gt 63 ]; }; then
	die "HOTSPOT_PASSWORD must be 8..63 characters"
fi
command -v docker >/dev/null || die "docker missing"
docker ps >/dev/null 2>&1 || die "cannot talk to docker (are you in group docker?)"

# ---------------------------------------------------------------- pi-gen checkout
log "pi-gen ${PIGEN_TAG} (${PIGEN_COMMIT:0:7})"
mkdir -p "${WORK}" "${DEPLOY}"
if [ ! -d "${PIGEN}/.git" ]; then
	git clone --quiet "${PIGEN_REPO}" "${PIGEN}"
fi
if ! git -C "${PIGEN}" cat-file -e "${PIGEN_COMMIT}^{commit}" 2>/dev/null; then
	git -C "${PIGEN}" fetch --quiet --tags origin
fi
git -C "${PIGEN}" checkout --quiet --detach "${PIGEN_COMMIT}"
# Lite only: no stage2 image, no stage3-5 (not in STAGE_LIST), no cloud-init/netplan
# (NetworkManager keyfiles are managed by projektor-wifi)
touch "${PIGEN}/stage2/SKIP_IMAGES" "${PIGEN}/stage2/04-cloud-init/SKIP"
for s in stage0 stage1 stage2; do
	if [ "${MODE}" = fast ]; then touch "${PIGEN}/${s}/SKIP"; else rm -f "${PIGEN}/${s}/SKIP"; fi
done

# ---------------------------------------------------------------- SSH keys for backup + tunnel
umask 077
mkdir -p "${SECRETS}"
for k in id_backup id_tunnel; do
	if [ ! -f "${SECRETS}/${k}" ]; then
		log "generating ${SECRETS}/${k}"
		ssh-keygen -q -t ed25519 -N '' -C "${DEVICE_NAME}-${k#id_}" -f "${SECRETS}/${k}"
	fi
done
umask 022

# ---------------------------------------------------------------- app wheel
log "building app wheel"
rm -rf "${WORK}/app-src" "${WORK}/wheels"
mkdir -p "${WORK}/app-src"
# copy (not in place) so the build never writes into app/
tar -C "${ROOT}/app" --exclude=.venv --exclude=devdata --exclude=build --exclude='*.egg-info' \
	--exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -C "${WORK}/app-src" -xf -
python3 -m pip wheel --quiet --no-deps --disable-pip-version-check --wheel-dir "${WORK}/wheels" "${WORK}/app-src"
WHEEL="$(ls "${WORK}"/wheels/projektor-*.whl)"
APP_VERSION="$(basename "${WHEEL}" | cut -d- -f2)"
if git -C "${ROOT}" rev-parse --short HEAD >/dev/null 2>&1; then
	APP_VERSION="${APP_VERSION}+g$(git -C "${ROOT}" rev-parse --short HEAD)"
	git -C "${ROOT}" diff --quiet HEAD -- app 2>/dev/null || APP_VERSION="${APP_VERSION}.dirty"
fi
echo "app ${APP_VERSION}"

# ---------------------------------------------------------------- assemble stage
log "assembling stage-projektor"
STAGE="${WORK}/stage-projektor"
rm -rf "${STAGE}"
cp -a "${HERE}/stage-projektor" "${STAGE}"
F="${STAGE}/01-projektor/files"
mkdir -p "${F}/wheels"
cp -a "${ROOT}/system" "${F}/system"
cp "${WHEEL}" "${F}/wheels/"
install -d -m 700 "${F}/secrets"
(
	umask 077
	printf '%s' "${ADMIN_PASSWORD}" | openssl passwd -6 -stdin > "${F}/secrets/admin_password_hash"
	printf '%s\n' "${WEB_PASSWORD:-}" | sed '/^$/d' > "${F}/secrets/web_password"
	: > "${F}/secrets/authorized_keys"
	for f in $(echo "${ADMIN_SSH_PUBKEYS_FILE:-}" | tr ':,' '  '); do
		[ -f "${f}" ] || die "ADMIN_SSH_PUBKEYS_FILE: ${f} not found"
		grep -E '^(ssh-|ecdsa-|sk-)' "${f}" >> "${F}/secrets/authorized_keys"
	done
	cp "${SECRETS}"/id_backup "${SECRETS}"/id_backup.pub "${SECRETS}"/id_tunnel "${SECRETS}"/id_tunnel.pub "${F}/secrets/"
	: > "${F}/secrets/known_hosts"
	if [ "${INFMK1_HOST}" != "INFMK1_HOST" ] && command -v ssh-keyscan >/dev/null; then
		if ssh-keyscan -T 5 -p "${INFMK1_PORT}" "${INFMK1_HOST}" > "${F}/secrets/known_hosts" 2>/dev/null \
				&& [ -s "${F}/secrets/known_hosts" ]; then
			echo "pinned infmk1 host keys ($(wc -l < "${F}/secrets/known_hosts") entries)"
		else
			echo "infmk1 not reachable for ssh-keyscan - device will accept its host key on first connect"
			: > "${F}/secrets/known_hosts"
		fi
	fi
)
[ -s "${F}/secrets/authorized_keys" ] || echo "WARNING: no SSH public key for ${ADMIN_USER} (password login only)"

{
	printf 'ADMIN_USER=%q\n' "${ADMIN_USER}"
	printf 'DEVICE_NAME=%q\n' "${DEVICE_NAME}"
	printf 'WIFI_COUNTRY=%q\n' "${WIFI_COUNTRY}"
	printf 'HDMI_CONNECTOR=%q\n' "${HDMI_CONNECTOR}"
	printf 'APP_VERSION=%q\n' "${APP_VERSION}"
} > "${F}/build.env"

# settings TOML (§6 + [wifi] / [tunnel] for the system services); the web password
# hash is filled in inside the chroot with the app's own hasher
DEVICE_NAME="${DEVICE_NAME}" INFMK1_HOST="${INFMK1_HOST}" INFMK1_PORT="${INFMK1_PORT}" \
TUNNEL_SSH_PORT="${TUNNEL_SSH_PORT}" TUNNEL_WEB_PORT="${TUNNEL_WEB_PORT}" WIFI_COUNTRY="${WIFI_COUNTRY}" \
HOTSPOT_PASSWORD="${HOTSPOT_PASSWORD:-}" DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}" \
DISCORD_MENTION="${DISCORD_MENTION:-}" HDMI_PORT="${HDMI_PORT}" HDMI_CONNECTOR="${HDMI_CONNECTOR}" \
python3 - > "${F}/projektor.toml" <<'PY'
import json, os
e = os.environ
s = lambda v: json.dumps(v, ensure_ascii=False)   # JSON strings are valid TOML basic strings
host = e["INFMK1_HOST"]
real = host not in ("", "INFMK1_HOST")
print(f"""# /etc/projektor/projektor.toml - generated by image/build.sh
# Sections [device]..[backup]: app settings (ARCHITECTURE.md §6).
# Sections [wifi], [tunnel]: read by projektor-wifi / projektor-tunnel (system services).

[device]
name = {s(e["DEVICE_NAME"])}

[paths]
data_dir = "/var/lib/projektor"
net_status = "/run/projektor/net.json"
tunnel_status = "/run/projektor/tunnel.json"

[display]
connector = {s(e["HDMI_CONNECTOR"])}   # HDMI output used by the GL display
sink = "auto"
status_on_boot = true
width = 1920
height = 1080

[osc]
port = 9000
reply_host = ""
reply_port = 0
dedup_window_s = 1.0

[lora]
enabled = true
device = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_*"
baud = 115200

[web]
port = 80
password_hash = "@WEB_PASSWORD_HASH@"
max_upload_mb = 8000

[backup]
enabled = {str(real).lower()}
host = {s(host)}
port = {int(e["INFMK1_PORT"])}
user = "projbackup"
key = "/var/lib/projektor/.ssh/id_backup"
remote_dir = ""

[wifi]
country = {s(e["WIFI_COUNTRY"])}
interface = "wlan0"
retry_interval_s = 15
hotspot_after_attempts = 8
hotspot_rescan_s = 300
hotspot_password = {s(e["HOTSPOT_PASSWORD"])}

[tunnel]
enabled = {str(real).lower()}
host = {s(host)}
port = {int(e["INFMK1_PORT"])}
user = "projtunnel"
key = "/var/lib/projektor/.ssh/id_tunnel"
remote_ssh_port = {int(e["TUNNEL_SSH_PORT"])}
remote_web_port = {int(e["TUNNEL_WEB_PORT"])}

[beamer]
device = {s("/dev/cec" + e["HDMI_PORT"])}
power_on_at_boot = true   # wake the beamer via HDMI-CEC when the projector boots

[notify]
discord_webhook = {s(e.get("DISCORD_WEBHOOK_URL", ""))}
mention = {s(e.get("DISCORD_MENTION", ""))}""")
PY
python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "${F}/projektor.toml"

# WiFi list on the boot partition: template + WIFI_NETWORKS ("ssid;psk;prio|...")
{
	cat "${ROOT}/system/boot/projektor-wifi.txt"
	if [ -n "${WIFI_NETWORKS}" ]; then
		echo
		echo "${WIFI_NETWORKS}" | tr '|' '\n' | sed '/^[[:space:]]*$/d'
	fi
} > "${F}/projektor-wifi.txt"
# CA certificates for WPA-Enterprise lines (`ca=<name>.pem`): WIFI_CA_FILES="/path/a.pem /path/b.pem"
mkdir -p "${F}/wifi-ca"
for ca in ${WIFI_CA_FILES:-}; do
	install -m 644 "${ca}" "${F}/wifi-ca/$(basename "${ca}")"
done
chmod -R go-rwx "${F}"

# ---------------------------------------------------------------- pi-gen config
CONFIG="${WORK}/config"
(
	umask 077
	{
		printf 'IMG_NAME=%q\n' "${IMG_NAME}"
		printf 'PI_GEN_RELEASE=%q\n' "theater-projektor (Raspberry Pi OS Lite trixie)"
		printf 'RELEASE=trixie\n'
		printf 'DEPLOY_COMPRESSION=xz\n'
		printf 'COMPRESSION_LEVEL=6\n'
		printf 'LOCALE_DEFAULT=de_DE.UTF-8\n'
		printf 'KEYBOARD_KEYMAP=de\n'
		printf 'KEYBOARD_LAYOUT=German\n'
		printf 'TIMEZONE_DEFAULT=Europe/Berlin\n'
		printf 'TARGET_HOSTNAME=%q\n' "${DEVICE_NAME}"
		printf 'FIRST_USER_NAME=%q\n' "${ADMIN_USER}"
		printf 'FIRST_USER_PASS=%q\n' "${ADMIN_PASSWORD}"
		printf 'DISABLE_FIRST_BOOT_USER_RENAME=1\n'
		printf 'ENABLE_SSH=1\n'
		printf 'PUBKEY_ONLY_SSH=0\n'
		printf 'PUBKEY_SSH_FIRST_USER=%q\n' "$(cat "${F}/secrets/authorized_keys")"
		printf 'WPA_COUNTRY=%q\n' "${WIFI_COUNTRY}"
		printf 'ENABLE_CLOUD_INIT=0\n'
		printf 'STAGE_LIST=%q\n' "/pi-gen/stage0 /pi-gen/stage1 /pi-gen/stage2 /pi-gen/stage-projektor"
		printf 'CLEAN=1\n'
		printf 'CONTAINER_NAME=%q\n' "${CONTAINER}"
		printf 'PRESERVE_CONTAINER=1\n'
		printf 'PIGEN_DOCKER_OPTS=%q\n' "-v ${STAGE}:/pi-gen/stage-projektor:ro"
	} > "${CONFIG}"
)
cleanup() {
	rm -f "${CONFIG}"
	rm -rf "${F}/secrets" "${F}/projektor.toml"
}
trap cleanup EXIT

# ---------------------------------------------------------------- container
EXISTS="$(docker ps -a --filter "name=^${CONTAINER}\$" -q)"
if [ "${MODE}" = fast ]; then
	[ -n "${EXISTS}" ] || die "no previous build container ${CONTAINER} - run a full build first"
	export CONTINUE=1
	# old images in the deploy volume would only waste space
	docker run --rm --volumes-from "${CONTAINER}" pi-gen bash -c 'rm -rf /pi-gen/deploy/*' || true
else
	if [ -n "${EXISTS}" ]; then
		log "removing previous build container ${CONTAINER}"
		docker rm -v "${CONTAINER}" >/dev/null
	fi
	export CONTINUE=0
fi

# build-docker.sh tries `sudo` to (re)register binfmt when the host's qemu-aarch64
# interpreter path differs from `which qemu-aarch64`; the host registration
# (flag F) works fine, so never prompt for a password.
SHIM="${WORK}/shim"
mkdir -p "${SHIM}"
printf '#!/bin/sh\necho "build.sh: skipping: sudo $*" >&2\nexit 1\n' > "${SHIM}/sudo"
chmod +x "${SHIM}/sudo"

log "running pi-gen (${MODE}) - log: ${WORK}/build-${MODE}.log"
rm -rf "${WORK}/deploy"
set +e
(cd "${WORK}" && PATH="${SHIM}:${PATH}" "${PIGEN}/build-docker.sh" -c "${CONFIG}") 2>&1 | tee "${WORK}/build-${MODE}.log"
RC=${PIPESTATUS[0]}
set -e
[ "${RC}" -eq 0 ] || die "pi-gen failed (exit ${RC}); see ${WORK}/build-${MODE}.log; container ${CONTAINER} kept"

IMG="$(ls -t "${WORK}"/deploy/*.img.xz 2>/dev/null | head -n 1 || true)"
[ -n "${IMG}" ] || die "no .img.xz produced"
rm -f "${DEPLOY}"/*.img.xz
mv "${IMG}" "${DEPLOY}/"
IMG="${DEPLOY}/$(basename "${IMG}")"
cp "${WORK}"/deploy/*.info "${WORK}/" 2>/dev/null || true
rm -rf "${WORK}/deploy"

T1=$(date +%s)
log "done in $(( (T1 - T0) / 60 )) min $(( (T1 - T0) % 60 )) s"
cat <<EOF

Image:     ${IMG} ($(du -h "${IMG}" | cut -f1))
App:       ${APP_VERSION}
Hostname:  ${DEVICE_NAME}  (ssh ${ADMIN_USER}@${DEVICE_NAME}.local, web http://${DEVICE_NAME}.local/)

Flash:     xzcat '${IMG}' | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
           (or Raspberry Pi Imager -> "Use custom", without OS customisation)

Public keys baked into the image (user projektor):
  backup: $(cat "${SECRETS}/id_backup.pub")
  tunnel: $(cat "${SECRETS}/id_tunnel.pub")

infmk1 setup (run once as root on infmk1, see docs/infmk1.md):
  scp server/setup-infmk1.sh secrets/id_backup.pub secrets/id_tunnel.pub root@<infmk1>:/root/
  ssh root@<infmk1> 'bash /root/setup-infmk1.sh --backup-key /root/id_backup.pub --tunnel-key /root/id_tunnel.pub --ssh-port ${TUNNEL_SSH_PORT} --web-port ${TUNNEL_WEB_PORT}'
EOF
if [ "${INFMK1_HOST}" = "INFMK1_HOST" ]; then
	echo
	echo "NOTE: INFMK1_HOST is still the placeholder - backup and tunnel are disabled in this image."
	echo "      Set it in secrets/build.env and run 'image/build.sh --fast'."
fi
