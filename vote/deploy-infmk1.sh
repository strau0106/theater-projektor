#!/bin/sh
# Build the vote website image from the committed HEAD on this PC and run it on infmk1
# (docker compose in /opt/welle-vote, 127.0.0.1:3100, Apache vhost vote.rufusreglet.ch).
#
#   vote/deploy-infmk1.sh            build + ship + restart
#
# infmk1 has 1 CPU / 2 GB RAM next to Odoo, so the Next.js build runs here, not there.
# /opt/welle-vote/.env (root, 600) holds ADMIN_PASSWORD, SESSION_SECRET and VOTE_TAG.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SSH="ssh -F $ROOT/tools/pi/ssh_config infmk1"
TAG=$(git -C "$ROOT" rev-parse --short HEAD)
git -C "$ROOT" diff --quiet HEAD -- vote || echo "note: uncommitted changes in vote/ are NOT deployed (building $TAG)"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git -C "$ROOT" archive HEAD vote | tar -x -C "$TMP"
docker build -q -t "welle-vote:$TAG" "$TMP/vote"

echo "shipping welle-vote:$TAG to infmk1"
docker save "welle-vote:$TAG" | gzip -1 | $SSH 'gunzip | sudo -n docker load'
$SSH "sudo -n sh -c 'cd /opt/welle-vote && sed -i \"s/^VOTE_TAG=.*/VOTE_TAG=$TAG/\" .env && docker compose up -d && docker image prune -f >/dev/null' && sleep 4 && curl -sf -o /dev/null -w 'vote %{http_code}\n' http://127.0.0.1:3100/vote && curl -s http://127.0.0.1:3100/api/design | cut -c1-40"
