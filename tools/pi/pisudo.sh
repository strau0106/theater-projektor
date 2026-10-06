#!/bin/sh
# Run a command string on the Pi as root. The command is shipped as a file (no quoting
# issues), the sudo password is fed via stdin.
#   tools/pi/pisudo.sh 'systemctl restart projektor'
D=$(dirname "$0")
ROOT=$(cd "$D/../.." && pwd)
# unique remote file name: parallel calls must not overwrite each other's command
F="/tmp/.pisudo-$(hostname)-$$-$(date +%s%N).sh"
printf '%s\n' "$*" | "$D/pi.sh" "cat > '$F'"
sed -n 's/^ADMIN_PASSWORD=//p' "$ROOT/secrets/build.env" | "$D/pi.sh" "sudo -S -p '' sh '$F'; rm -f '$F'"
