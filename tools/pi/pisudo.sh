#!/bin/sh
# Run a command string on the Pi as root. The command is shipped as a file (no quoting
# issues), the sudo password is fed via stdin.
#   tools/pi/pisudo.sh 'systemctl restart projektor'
D=$(dirname "$0")
ROOT=$(cd "$D/../.." && pwd)
printf '%s\n' "$*" | "$D/pi.sh" 'cat > /tmp/.cmd.sh'
sed -n 's/^ADMIN_PASSWORD=//p' "$ROOT/secrets/build.env" | "$D/pi.sh" "sudo -S -p '' sh /tmp/.cmd.sh; rm -f /tmp/.cmd.sh"
