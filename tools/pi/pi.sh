#!/bin/sh
# Run a command on the Pi as the admin user (password login, password from secrets/build.env).
#   PI_HOST=projektor.local tools/pi/pi.sh 'uptime'
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ENV="$ROOT/secrets/build.env"
HOST=${PI_HOST:-projektor.local}
USER_=$(sed -n 's/^ADMIN_USER=//p' "$ENV")
export SSHPASS=$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV")
exec sshpass -e ssh -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$ROOT/secrets/known_hosts" \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no -o LogLevel=ERROR \
  "${USER_:-theater}@$HOST" "$@"
