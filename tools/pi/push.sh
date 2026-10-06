#!/bin/sh
# Copy a local file to the Pi:  tools/pi/push.sh LOCAL REMOTE_PATH
D=$(dirname "$0")
"$D/pi.sh" "cat > '$2'" < "$1"
