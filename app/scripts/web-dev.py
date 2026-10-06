#!/usr/bin/env python3
"""Run the web UI standalone with fake controller/dispatcher/show store (no GStreamer).

    app/.venv/bin/python app/scripts/web-dev.py [--port 8081] [--data DIR] [--password PW]

Data dir defaults to app/devdata/webdev/ (seeded from app/devdata/show.json + media
copies on first start, if present). WiFi uses projektor-wifi-stub.py, power buttons
are dry runs, backup goes to a local directory (DATA/backup-remote/) via rsync.
For the real core with a fake player use `projektor run --dev --player fake` instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))

from projektor.backup import BackupManager  # noqa: E402
from projektor.web.app import create_app  # noqa: E402
from projektor.web.auth import hash_password  # noqa: E402
from projektor.web.fakes import FakeController, FakeDispatcher, FakeShowStore, dev_settings  # noqa: E402


def seed(data: Path) -> None:
    src = APP / "devdata"
    (data / "media").mkdir(parents=True, exist_ok=True)
    if (data / "show.json").exists() or not (src / "show.json").exists():
        return
    shutil.copy(src / "show.json", data / "show.json")
    for f in (src / "media").glob("*"):
        if f.is_file() and not (data / "media" / f.name).exists():
            shutil.copy(f, data / "media" / f.name)
    now = int(time.time())
    (data / "net.json").write_text(json.dumps({"state": "connected", "ssid": "Theater", "ip": "192.168.1.42",
                                               "attempt": 0, "updated": now}))
    (data / "tunnel.json").write_text(json.dumps({"state": "up", "remote": "infmk1:2222", "updated": now}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--data", type=Path, default=APP / "devdata" / "webdev")
    ap.add_argument("--password", default="", help="enable login with this password")
    ap.add_argument("--max-upload-mb", type=int, default=8000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    data = args.data.resolve()
    seed(data)
    settings = dev_settings(data, password_hash=hash_password(args.password) if args.password else "", host="localhost")
    settings.web.max_upload_mb = args.max_upload_mb
    settings.web.port = args.port
    store = FakeShowStore(data)
    ctrl = FakeController(store, notify_thread=True)
    disp = FakeDispatcher(ctrl)
    remote = data / "backup-remote"
    remote.mkdir(exist_ok=True)
    backup = BackupManager(settings, store, ctrl, remote=f"{remote}/", rsh=None)
    app = create_app(settings, ctrl, disp, store, backup=backup,
                     power_cmds={"reboot": None, "shutdown": None})

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
