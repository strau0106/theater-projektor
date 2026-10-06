#!/bin/sh
# Key-based bench access to the test Pi (no passwords, no secrets/ access needed).
#   tools/pi/dev.sh ssh 'uptime'                 run a command as the admin user
#   tools/pi/dev.sh deploy                       push app/src/projektor + restart
#   tools/pi/dev.sh snapshot out.png             frame currently on HDMI
#   tools/pi/dev.sh stats|journal [since]|restart|stop|start|setting <sec.key> <val>|lora <args>
# One-time setup on the Pi: ~/.ssh/projektor_test.pub in authorized_keys, projektor-dev
# installed in /usr/local/sbin + sudoers rule (see tools/pi/README.md).
D=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$D/../.." && pwd)
SSH="ssh -F $D/ssh_config projektor-test"
cmd=$1; shift
case "$cmd" in
  ssh)      exec $SSH "$@" ;;
  deploy)   tar -C "$ROOT/app/src" -czf - --exclude=__pycache__ projektor | $SSH 'cat > /tmp/projektor-src.tgz' &&
            exec $SSH 'sudo -n /usr/local/sbin/projektor-dev deploy' ;;
  snapshot) $SSH 'sudo -n /usr/local/sbin/projektor-dev snapshot' | base64 -d > "${1:-snapshot.png}" && echo "${1:-snapshot.png}" ;;
  *)        exec $SSH sudo -n /usr/local/sbin/projektor-dev "$cmd" "$@" ;;
esac
