#!/bin/sh
# Hot-deploy the local app sources (app/src/projektor) into the Pi's venv and restart.
D=$(dirname "$0")
ROOT=$(cd "$D/../.." && pwd)
tar -C "$ROOT/app/src" -czf - --exclude=__pycache__ projektor | "$D/pi.sh" 'cat > /tmp/projektor-src.tgz'
"$D/pisudo.sh" 'SP=$(/opt/projektor/venv/bin/python -c "import projektor,os;print(os.path.dirname(os.path.dirname(projektor.__file__)))") && tar -C "$SP" -xzf /tmp/projektor-src.tgz && rm /tmp/projektor-src.tgz && systemctl restart projektor && echo deployed to $SP'
