#!/bin/sh
# Grab the frame currently on HDMI (player SIGUSR1 -> snapshot.png):  tools/pi/snapshot.sh out.png
D=$(dirname "$0")
"$D/pisudo.sh" 'systemctl kill -s USR1 projektor; sleep 2; base64 -w0 /var/lib/projektor/snapshot.png' | base64 -d > "${1:-snapshot.png}"
