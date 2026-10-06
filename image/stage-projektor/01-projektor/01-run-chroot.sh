#!/bin/bash -e
# Chroot side of the projektor stage (runs inside the target rootfs under qemu).
B=/root/projektor-build
# shellcheck disable=SC1091
. "${B}/build.env"

# ---------------------------------------------------------------- users
for g in video render dialout netdev; do
	groupadd -f -r "${g}"
done
if ! id -u projektor >/dev/null 2>&1; then
	useradd --system --user-group --home-dir /var/lib/projektor --create-home \
		--shell /usr/sbin/nologin --comment "theater projektor app" projektor
fi
usermod -a -G video,render,dialout,netdev projektor

# admin user (created by pi-gen as FIRST_USER_NAME); password + keys repeated here
# so that `build.sh --fast` applies changes from secrets/build.env
id -u "${ADMIN_USER}" >/dev/null
usermod -p "$(cat "${B}/secrets/admin_password_hash")" "${ADMIN_USER}"
usermod -a -G sudo,video,render,dialout,netdev,adm "${ADMIN_USER}"
install -d -m 700 -o "${ADMIN_USER}" -g "${ADMIN_USER}" "/home/${ADMIN_USER}/.ssh"
if [ -s "${B}/secrets/authorized_keys" ]; then
	install -m 600 -o "${ADMIN_USER}" -g "${ADMIN_USER}" "${B}/secrets/authorized_keys" \
		"/home/${ADMIN_USER}/.ssh/authorized_keys"
fi

# ---------------------------------------------------------------- data dirs + keys
install -d -m 750 -o projektor -g projektor /var/lib/projektor
install -d -m 750 -o projektor -g projektor /var/lib/projektor/media
install -d -m 700 -o projektor -g projektor /var/lib/projektor/.ssh
for k in id_backup id_tunnel; do
	install -m 600 -o projektor -g projektor "${B}/secrets/${k}" "/var/lib/projektor/.ssh/${k}"
	install -m 644 -o projektor -g projektor "${B}/secrets/${k}.pub" "/var/lib/projektor/.ssh/${k}.pub"
done
install -m 600 -o projektor -g projektor "${B}/system/conf/ssh-projektor.conf" /var/lib/projektor/.ssh/config
if [ -s "${B}/secrets/known_hosts" ]; then
	install -m 644 -o projektor -g projektor "${B}/secrets/known_hosts" /var/lib/projektor/.ssh/known_hosts
fi

# ---------------------------------------------------------------- app venv
rm -rf /opt/projektor/venv
install -d -m 755 /opt/projektor
python3 -m venv --system-site-packages /opt/projektor/venv
/opt/projektor/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
	--progress-bar off "${B}"/wheels/projektor-*.whl
if /opt/projektor/venv/bin/projektor --help >/dev/null; then
	echo "projektor --help: ok"
else
	echo "WARNING: 'projektor --help' failed - the image is built, but the app will not start"
fi
echo "${APP_VERSION}" > /opt/projektor/VERSION

# ---------------------------------------------------------------- settings
install -d -m 775 -o root -g projektor /etc/projektor
HASH=""
if [ -s "${B}/secrets/web_password" ]; then
	HASH="$(/opt/projektor/venv/bin/python -m projektor.web.auth hash < "${B}/secrets/web_password" 2>/dev/null | tail -n 1 || true)"
	if ! printf '%s' "${HASH}" | grep -Eq '^(pbkdf2_sha256|scrypt)\$'; then
		echo "projektor.web.auth hash not usable - falling back to stdlib pbkdf2_sha256"
		HASH="$(python3 - "${B}/secrets/web_password" <<'PY'
import base64, hashlib, os, sys
pw = open(sys.argv[1], encoding="utf-8").readline().rstrip("\r\n").encode()
salt = os.urandom(16)
it = 200_000
dk = hashlib.pbkdf2_hmac("sha256", pw, salt, it)
b64 = lambda b: base64.b64encode(b).decode().rstrip("=")
print(f"pbkdf2_sha256${it}${b64(salt)}${b64(dk)}")
PY
)"
	fi
else
	echo "WARNING: WEB_PASSWORD empty - web UI without password"
fi
python3 - "${B}/projektor.toml" "${HASH}" <<'PY'
import sys
src, h = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read().replace("@WEB_PASSWORD_HASH@", h)
open("/etc/projektor/projektor.toml", "w", encoding="utf-8").write(text)
PY
chown projektor:projektor /etc/projektor/projektor.toml
chmod 640 /etc/projektor/projektor.toml
python3 -c 'import tomllib; tomllib.load(open("/etc/projektor/projektor.toml", "rb"))'

# ---------------------------------------------------------------- services
visudo -c -f /etc/sudoers.d/projektor
visudo -c
systemctl enable NetworkManager.service ssh.service \
	projektor.service projektor-wifi.service projektor-tunnel.service

# The app must be DRM master on HDMI: no login prompt on tty1 (admin console:
# Alt+F2 = tty2). Kernel/console output is hidden by quiet + no cursor.
systemctl mask getty@tty1.service autovt@tty1.service

# Boot speed / fewer moving parts. Bluetooth is disabled (also via dtoverlay in
# config.txt): not used, and BT shares the radio with WiFi.
for u in bluetooth.service hciuart.service apt-daily.timer apt-daily-upgrade.timer man-db.timer \
	nftables.service ModemManager.service cloud-init.service cloud-init-local.service \
	cloud-config.service cloud-final.service; do
	if [ -e "/usr/lib/systemd/system/${u}" ] || [ -e "/etc/systemd/system/${u}" ]; then
		systemctl disable "${u}" || true
	fi
done
# remote desktop/shell service of Raspberry Pi Connect: not wanted on a show device
if dpkg -s rpi-connect-lite >/dev/null 2>&1; then
	apt-get purge -y rpi-connect-lite
fi

# ---------------------------------------------------------------- cleanup
rm -rf "${B}"
