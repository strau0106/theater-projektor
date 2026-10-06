#!/usr/bin/env bash
# One-time (idempotent) setup of the infmk1 server for the theater projector.
# Run as root on a Debian/Ubuntu server. See docs/infmk1.md.
#
#   setup-infmk1.sh --backup-key id_backup.pub --tunnel-key id_tunnel.pub \
#                   [--ssh-port 2222] [--web-port 8080] [--backup-root /srv/theater-projektor]
#
# Key arguments are files or literal public keys ("ssh-ed25519 AAAA... comment").
# Re-running replaces the keys (key rotation) and re-checks everything.
#
# Creates:
#   projtunnel  no shell; key may only open the reverse forwards
#               127.0.0.1:<ssh-port> and 127.0.0.1:<web-port> (permitlisten)
#   projbackup  key forced into rrsync, confined to <backup-root>
#   <backup-root>/{media,snapshots}  (owned by projbackup)
#   /etc/ssh/sshd_config.d/theater-projektor.conf  (Match User blocks)
set -euo pipefail

BACKUP_KEY=""
TUNNEL_KEY=""
SSH_PORT=2222
WEB_PORT=8080
BACKUP_ROOT=/srv/theater-projektor
SSHD_SNIPPET=/etc/ssh/sshd_config.d/theater-projektor.conf

usage() { sed -n '2,20p' "$0"; exit "${1:-0}"; }
die() { echo "ERROR: $*" >&2; exit 1; }
ok() { echo "  ok: $*"; }

while [ $# -gt 0 ]; do
	case "$1" in
		--backup-key) BACKUP_KEY="$2"; shift 2 ;;
		--tunnel-key) TUNNEL_KEY="$2"; shift 2 ;;
		--ssh-port) SSH_PORT="$2"; shift 2 ;;
		--web-port) WEB_PORT="$2"; shift 2 ;;
		--backup-root) BACKUP_ROOT="$2"; shift 2 ;;
		-h|--help) usage 0 ;;
		*) echo "unknown argument: $1" >&2; usage 2 ;;
	esac
done

[ "$(id -u)" -eq 0 ] || die "run as root"
[[ "${SSH_PORT}" =~ ^[0-9]+$ && "${WEB_PORT}" =~ ^[0-9]+$ ]] || die "ports must be numbers"
[[ "${BACKUP_ROOT}" == /* ]] || die "--backup-root must be absolute"

# read a key from a file or a literal string; validate with ssh-keygen
read_key() {
	local src="$1" key
	if [ -f "${src}" ]; then key="$(grep -E '^(ssh-|ecdsa-|sk-)' "${src}" | head -n 1)"; else key="${src}"; fi
	[ -n "${key}" ] || die "no public key in '${src}'"
	ssh-keygen -l -f /dev/stdin <<< "${key}" >/dev/null 2>&1 || die "invalid public key: ${src}"
	printf '%s\n' "${key}"
}

echo "== packages"
need=()
command -v rsync >/dev/null || need+=(rsync)
command -v sshd >/dev/null || [ -x /usr/sbin/sshd ] || need+=(openssh-server)
command -v python3 >/dev/null || need+=(python3)
if [ ${#need[@]} -gt 0 ]; then
	apt-get update -q
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${need[@]}"
fi
ok "rsync $(rsync --version | head -n1 | awk '{print $3}'), $(/usr/sbin/sshd -V 2>&1 | head -n1 || ssh -V 2>&1)"

echo "== rrsync"
RRSYNC=""
for c in /usr/bin/rrsync /usr/local/bin/rrsync; do
	[ -x "${c}" ] && RRSYNC="${c}" && break
done
if [ -z "${RRSYNC}" ]; then
	if [ -f /usr/share/doc/rsync/scripts/rrsync.gz ]; then
		gunzip -c /usr/share/doc/rsync/scripts/rrsync.gz > /usr/local/bin/rrsync
	elif [ -f /usr/share/doc/rsync/scripts/rrsync ]; then
		cp /usr/share/doc/rsync/scripts/rrsync /usr/local/bin/rrsync
	else
		die "rrsync not found (expected /usr/bin/rrsync or /usr/share/doc/rsync/scripts/rrsync.gz)"
	fi
	chmod 755 /usr/local/bin/rrsync
	RRSYNC=/usr/local/bin/rrsync
fi
# old rrsync versions are perl scripts
head -n1 "${RRSYNC}" | grep -q perl && { command -v perl >/dev/null || die "rrsync needs perl"; }
ok "${RRSYNC}"

echo "== users"
make_user() {
	local user="$1" home="$2" shell="$3"
	if ! id -u "${user}" >/dev/null 2>&1; then
		useradd --system --create-home --home-dir "${home}" --shell "${shell}" --user-group \
			--comment "theater-projektor ${user#proj}" "${user}"
	else
		usermod --shell "${shell}" "${user}"
	fi
	passwd -l "${user}" >/dev/null 2>&1 || true
	install -d -m 755 -o "${user}" -g "${user}" "${home}"
	# ~/.ssh and authorized_keys owned by root: the restricted users cannot change their keys
	install -d -m 755 -o root -g root "${home}/.ssh"
	ok "${user} (home ${home}, shell ${shell})"
}
# projtunnel: never runs anything (ssh -N), nologin is enough
make_user projtunnel /var/lib/projtunnel /usr/sbin/nologin
# projbackup: needs a real shell so sshd can start the forced rrsync command
make_user projbackup /var/lib/projbackup /bin/sh

write_keys() {
	local user="$1" opts="$2" key="$3" home
	home="$(getent passwd "${user}" | cut -d: -f6)"
	local f="${home}/.ssh/authorized_keys" tmp
	tmp="$(mktemp)"
	printf '%s %s\n' "${opts}" "${key}" > "${tmp}"
	install -m 644 -o root -g root "${tmp}" "${f}"
	rm -f "${tmp}"
	ok "${f}: $(ssh-keygen -l -f /dev/stdin <<< "${key}" | awk '{print $2, $NF}')"
}

TUNNEL_OPTS="restrict,port-forwarding,permitlisten=\"127.0.0.1:${SSH_PORT}\",permitlisten=\"127.0.0.1:${WEB_PORT}\",command=\"/bin/false\""
BACKUP_OPTS="restrict,command=\"${RRSYNC} ${BACKUP_ROOT}\""

echo "== keys"
if [ -n "${TUNNEL_KEY}" ]; then
	write_keys projtunnel "${TUNNEL_OPTS}" "$(read_key "${TUNNEL_KEY}")"
elif [ -s /var/lib/projtunnel/.ssh/authorized_keys ]; then
	# keep the key, refresh the options (ports may have changed)
	k="$(sed -E 's/^.*command="[^"]*" //' /var/lib/projtunnel/.ssh/authorized_keys | head -n1)"
	write_keys projtunnel "${TUNNEL_OPTS}" "${k}"
else
	echo "  WARNING: no --tunnel-key given and none installed yet"
fi
if [ -n "${BACKUP_KEY}" ]; then
	write_keys projbackup "${BACKUP_OPTS}" "$(read_key "${BACKUP_KEY}")"
elif [ -s /var/lib/projbackup/.ssh/authorized_keys ]; then
	k="$(sed -E 's/^.*command="[^"]*" //' /var/lib/projbackup/.ssh/authorized_keys | head -n1)"
	write_keys projbackup "${BACKUP_OPTS}" "${k}"
else
	echo "  WARNING: no --backup-key given and none installed yet"
fi

echo "== backup root"
install -d -m 750 -o projbackup -g projbackup "${BACKUP_ROOT}"
install -d -m 750 -o projbackup -g projbackup "${BACKUP_ROOT}/media" "${BACKUP_ROOT}/snapshots"
ok "${BACKUP_ROOT}/{media,snapshots}"

echo "== sshd"
cat > "${SSHD_SNIPPET}.tmp" <<EOF
# theater-projektor (written by setup-infmk1.sh)
Match User projtunnel
	AllowTcpForwarding remote
	GatewayPorts no
	PermitListen 127.0.0.1:${SSH_PORT} 127.0.0.1:${WEB_PORT}
	PasswordAuthentication no
	KbdInteractiveAuthentication no
	AllowAgentForwarding no
	X11Forwarding no
	PermitTTY no
	ClientAliveInterval 30
	ClientAliveCountMax 3
Match User projbackup
	AllowTcpForwarding no
	PasswordAuthentication no
	KbdInteractiveAuthentication no
	AllowAgentForwarding no
	X11Forwarding no
	PermitTTY no
Match all
EOF
mv "${SSHD_SNIPPET}.tmp" "${SSHD_SNIPPET}"
chmod 644 "${SSHD_SNIPPET}"
if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
	echo "  WARNING: /etc/ssh/sshd_config does not include sshd_config.d/*.conf - add the content of"
	echo "           ${SSHD_SNIPPET} to the END of /etc/ssh/sshd_config manually"
fi
/usr/sbin/sshd -t || die "sshd config test failed (see above); fix it before reloading"
ok "sshd -t"

# effective settings for the tunnel user (connection-specific Match evaluation)
eff="$(/usr/sbin/sshd -T -C user=projtunnel,host=projektor,addr=192.0.2.1 2>/dev/null || true)"
fwd="$(awk '$1=="allowtcpforwarding"{print $2}' <<< "${eff}")"
gw="$(awk '$1=="gatewayports"{print $2}' <<< "${eff}")"
dis="$(awk '$1=="disableforwarding"{print $2}' <<< "${eff}")"
case "${fwd}" in remote|yes|all) ok "projtunnel AllowTcpForwarding=${fwd}" ;; *) echo "  WARNING: projtunnel AllowTcpForwarding=${fwd:-?} - reverse tunnel will not work" ;; esac
[ "${gw}" = "no" ] && ok "projtunnel GatewayPorts=no (forwards bind 127.0.0.1 only)" || echo "  WARNING: GatewayPorts=${gw:-?}"
[ "${dis}" = "yes" ] && echo "  WARNING: DisableForwarding=yes globally - reverse tunnel will not work"
for u in projtunnel projbackup; do
	au="$(/usr/sbin/sshd -T -C user=${u},host=projektor,addr=192.0.2.1 2>/dev/null | awk '$1=="allowusers"||$1=="allowgroups"{print}')"
	[ -z "${au}" ] || echo "  WARNING: ${au} is set - make sure ${u} is allowed"
done

if systemctl is-active --quiet ssh 2>/dev/null; then
	systemctl reload ssh
elif systemctl is-active --quiet sshd 2>/dev/null; then
	systemctl reload sshd
fi
ok "sshd reloaded"

H="$(hostname -f 2>/dev/null || hostname)"
cat <<EOF

== done. Tests
On the projector (as admin, after INFMK1_HOST is set in its settings):
  systemctl status projektor-tunnel;  cat /run/projektor/tunnel.json      # state "up"
  sudo -u projektor rsync -a -e "ssh -i /var/lib/projektor/.ssh/id_backup" /etc/hostname projbackup@${H}:test.txt
                                                       # -> ${BACKUP_ROOT}/test.txt

Manual tunnel test from the projector (stop the service first):
  sudo -u projektor ssh -i /var/lib/projektor/.ssh/id_tunnel -N -R 127.0.0.1:${SSH_PORT}:localhost:22 projtunnel@${H}

On infmk1:
  ss -ltn | grep -E ':(${SSH_PORT}|${WEB_PORT})\\b'     # 127.0.0.1:${SSH_PORT} and 127.0.0.1:${WEB_PORT} while the tunnel is up
  ls -la ${BACKUP_ROOT}

From your PC:
  ssh -J <you>@${H} -p ${SSH_PORT} theater@localhost                     # shell on the projector
  ssh -L 8080:localhost:${WEB_PORT} <you>@${H}   then open http://localhost:8080/   # web UI

Must FAIL (restrictions):
  ssh -i id_tunnel projtunnel@${H}                     # no shell
  ssh -i id_tunnel -N -R 127.0.0.1:9999:localhost:22 projtunnel@${H}   # port not permitted
  ssh -i id_backup projbackup@${H}                     # rrsync only
EOF
