# Pi bench tools

Helpers used for testing on the real Pi (need `sshpass`, `secrets/build.env`).
Host defaults to `projektor.local`; override with `PI_HOST=192.168.178.88`.

| Script | Use |
|---|---|
| `pi.sh CMD` | run as admin user |
| `pisudo.sh CMD` | run as root |
| `push.sh LOCAL REMOTE` | copy file to Pi |
| `deploy-app.sh` | hot-deploy app/src/projektor into the Pi venv + restart |
| `snapshot.sh out.png` | frame currently on HDMI |
| `gst-cpu.sh "<gst-launch pipeline>"` | (copy to Pi) avg CPU / shmem / drops over 12 s |

Measurements Pi 4 1 GB, 1080p30 H.264 (v4l2h264dec), 2026-10-06:

| Pipeline | CPU | 
|---|---|
| decode → glimagesink | 10 % |
| decode → glvideomixer → glimagesink | 17 % |
| decode → intervideosink ⇢ intervideosrc → glvideomixer (v0.1 player) | 82–111 % |
| decode → appsink ⇢ appsrc → glvideomixer | 87–99 % |
| avdec_h264 (software) → glimagesink | 294 % |
| status layer 1080p30 BGRA textoverlay | 93 %, +310 MB shmem |
| status layer 960x540@2 | 10 % |

## Key-based access (preferred)

`tools/pi/dev.sh` uses a dedicated key `~/.ssh/projektor_test` and a limited passwordless
sudo helper on the Pi (`projektor-dev`: deploy, restart/stop/start, snapshot, stats, journal,
setting), so no password from `secrets/` is needed. Host `projektor.local` (see `ssh_config`).

One-time Pi setup (done on the bench Pi 2026-10-06):
```
ssh-keygen -t ed25519 -N "" -f ~/.ssh/projektor_test
# append ~/.ssh/projektor_test.pub to /home/theater/.ssh/authorized_keys on the Pi
# install tools/pi/projektor-dev as /usr/local/sbin/projektor-dev (root, 755)
# /etc/sudoers.d/projektor-dev:  theater ALL=(root) NOPASSWD: /usr/local/sbin/projektor-dev
```
