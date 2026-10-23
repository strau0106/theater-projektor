"""Backup/restore with real rsync against a local directory standing in for the server."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektor.backup import BackupBusy, BackupError, BackupManager, parse_list, parse_progress, snapshot_name
from projektor.web.app import create_app
from projektor.web.fakes import FakeController, FakeDispatcher, FakeShowStore, dev_settings

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")

SHOW = {
    "version": 1,
    "videos": {"a": {"file": "a.mp4", "title": "A", "in_point": 0.0, "out_point": None,
                     "pause_points": [], "fade_in": 0.0, "fade_out": 0.0, "end": "black"}},
    "cues": [{"id": "1", "name": "One", "video": "a", "fade_in": None, "fade_out": None,
              "pause_points": None, "end": None, "start_at": None}],
}


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    remote = tmp_path / "remote"
    remote.mkdir()
    settings = dev_settings(data)
    store = FakeShowStore(data)
    store.path.write_text(json.dumps(SHOW))
    (store.media_dir / "a.mp4").write_bytes(b"A" * 200_000)
    (store.media_dir / "b.mp4").write_bytes(b"B" * 1000)
    (store.media_dir / ".upload-1234-c.mp4").write_bytes(b"partial")
    ctrl = FakeController(store)
    mgr = BackupManager(settings, store, ctrl, remote=f"{remote}/", rsh=None)
    return settings, store, ctrl, mgr, remote


def test_parsers():
    p = parse_progress("     32,768  45%   10.00MB/s    0:00:12 (xfr#1, to-chk=2/4)")
    assert p == {"bytes": 32768, "percent": 45, "rate": "10.00MB/s", "eta": "0:00:12"}
    assert parse_progress("sending incremental file list") is None
    out = (
        "drwxr-xr-x          4,096 2026/10/06 09:00:00 .\n"
        "-rw-r--r--          1,234 2026/10/05 08:00:00 show-20261005T080000Z.json\n"
        "-rw-r--r--          1,300 2026/10/06 09:00:00 show-20261006T090000Z.json\n"
        "-rw-r--r--             12 2026/10/06 09:00:00 notes.txt\n"
    )
    snaps = parse_list(out)
    assert [s["name"] for s in snaps] == ["show-20261006T090000Z.json", "show-20261005T080000Z.json"]
    assert snaps[0]["size"] == 1300 and snaps[0]["timestamp"] == 1791277200.0
    assert snapshot_name(0) == "show-19700101T000000Z.json"


def test_default_ssh_command(tmp_path):
    settings = dev_settings(tmp_path, host="backup.example.org", port=2200, user="projbackup",
                            key="/k/id_backup", remote_dir="sub/dir/")
    mgr = BackupManager(settings, FakeShowStore(tmp_path))
    assert mgr.remote == "projbackup@backup.example.org:sub/dir/"
    assert mgr.rsh.startswith("ssh -i /k/id_backup -p 2200 -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    settings.backup.remote_dir = ""
    assert BackupManager(settings, FakeShowStore(tmp_path)).remote == "projbackup@backup.example.org:"
    # placeholder host -> not configured
    s2 = dev_settings(tmp_path)
    m2 = BackupManager(s2, FakeShowStore(tmp_path))
    assert not m2.configured
    with pytest.raises(BackupError):
        m2.backup_now()
    s2.backup.enabled = False
    s2.backup.host = "x"
    with pytest.raises(BackupError, match="disabled"):
        BackupManager(s2, FakeShowStore(tmp_path)).list_snapshots()


def test_backup_and_restore(setup):
    settings, store, ctrl, mgr, remote = setup
    assert mgr.list_snapshots() == []
    st = mgr.backup_now(wait=True)
    st = mgr.status()
    assert st["state"] == "done", st
    assert st["percent"] == 100 and st["last_backup"]["state"] == "done"
    assert (remote / "media" / "a.mp4").read_bytes() == b"A" * 200_000
    assert (remote / "media" / "b.mp4").exists()
    assert not (remote / "media" / ".upload-1234-c.mp4").exists()
    assert json.loads((remote / "show.json").read_text()) == SHOW
    snaps = mgr.list_snapshots()
    assert len(snaps) == 1 and json.loads((remote / "snapshots" / snaps[0]["name"]).read_text()) == SHOW
    first_snap = snaps[0]["name"]

    # change show locally, second backup -> second snapshot (wait 1 s for a new timestamp)
    time.sleep(1.05)
    show2 = json.loads(json.dumps(SHOW))
    show2["cues"][0]["name"] = "Changed"
    store.path.write_text(json.dumps(show2))
    mgr.backup_now(wait=True)
    assert mgr.status()["state"] == "done"
    snaps = mgr.list_snapshots()
    assert len(snaps) == 2 and snaps[1]["name"] == first_snap

    # local disaster: media + show gone
    shutil.rmtree(store.media_dir)
    store.path.write_text(json.dumps({"version": 1, "videos": {}, "cues": []}))
    ctrl.calls.clear()

    # restore older snapshot without media
    mgr.restore(first_snap, media=False, wait=True)
    assert mgr.status()["state"] == "done", mgr.status()
    assert json.loads(store.path.read_text())["cues"][0]["name"] == "One"
    assert ("reload_show",) in ctrl.calls
    assert not (store.media_dir / "a.mp4").exists()
    assert (store.data_dir / "show.before-restore.json").exists()

    # restore latest incl. media
    mgr.restore("latest", media=True, wait=True)
    assert mgr.status()["state"] == "done", mgr.status()
    assert json.loads(store.path.read_text())["cues"][0]["name"] == "Changed"
    assert (store.media_dir / "a.mp4").read_bytes() == b"A" * 200_000

    with pytest.raises(BackupError):
        mgr.restore("../../etc/passwd")


def test_restore_invalid_show_rejected(setup):
    settings, store, ctrl, mgr, remote = setup
    (remote / "show.json").write_text(json.dumps({"version": 1, "videos": {}, "cues": [{"id": "1", "video": "ghost"}]}))
    mgr.restore("latest", wait=True)
    st = mgr.status()
    assert st["state"] == "error" and "invalid" in st["message"]
    assert json.loads(store.path.read_text()) == SHOW  # untouched


def test_rsync_failure_reported(setup, tmp_path):
    settings, store, ctrl, mgr, remote = setup
    bad = BackupManager(settings, store, ctrl, remote=str(tmp_path / "missing" / "deeper") + "/", rsh=None)
    bad.backup_now(wait=True)
    st = bad.status()
    assert st["state"] == "error" and "rsync" in st["message"]


def test_single_job_and_cancel(setup, tmp_path):
    settings, store, ctrl, mgr, remote = setup
    # slow fake "rsync" so the job is still running when we start a second one
    slow = tmp_path / "slow-rsync"
    slow.write_text(f"#!{sys.executable}\nimport time,sys\nprint('  1,000  10%  1.00kB/s  0:00:09', end='\\r', flush=True)\ntime.sleep(5)\n")
    slow.chmod(0o755)
    m = BackupManager(settings, store, ctrl, remote=f"{remote}/", rsh=None, rsync=str(slow))
    m.backup_now()
    with pytest.raises(BackupBusy):
        m.backup_now()
    with pytest.raises(BackupBusy):
        m.restore()
    for _ in range(100):
        if m.status()["percent"] == 10:
            break
        time.sleep(0.02)
    assert m.status()["percent"] == 10 and m.status()["phase"] == "media"
    assert m.cancel() is True
    m.wait(5)
    assert m.status()["state"] == "cancelled"


def test_backup_api(setup):
    settings, store, ctrl, mgr, remote = setup
    app = create_app(settings, ctrl, FakeDispatcher(ctrl), store, backup=mgr)
    c = TestClient(app)
    assert c.get("/api/backup").json()["state"] == "idle"
    r = c.post("/api/backup/run")
    assert r.status_code == 200
    mgr.wait(30)
    st = c.get("/api/backup").json()
    assert st["state"] == "done" and st["last_backup"]["state"] == "done"
    snaps = c.get("/api/backup/snapshots").json()["snapshots"]
    assert len(snaps) == 1
    r = c.post("/api/backup/restore", json={"snapshot": snaps[0]["name"], "media": True})
    assert r.status_code == 200
    mgr.wait(30)
    assert c.get("/api/backup").json()["state"] == "done"
    assert c.post("/api/backup/restore", json={"snapshot": "evil"}).status_code == 400
    assert c.post("/api/backup/cancel").json()["ok"] is False


# ------------------------------------------------------------------ state / config / wifi

STUB = Path(__file__).resolve().parents[1] / "scripts" / "projektor-wifi-stub.py"
TOML = '[device]\nname = "restored-box"\n\n[web]\nport = 8099\n'


def _wifi(tmp_path, name="wifi.json"):
    from projektor.web.system import WifiCli

    return WifiCli([sys.executable, str(STUB), "--state", str(tmp_path / name)])


@pytest.fixture
def full(setup, tmp_path):
    settings, store, ctrl, _mgr, remote = setup
    data = store.data_dir
    (data / "results.json").write_text('{"ja": 3, "nein": 1, "poll_id": 7, "time": 1.0}')
    (data / "design").mkdir()
    (data / "design" / "manifest.json").write_text("{}")
    (data / "design" / "font.ttf").write_bytes(b"F" * 100)
    (data / ".ssh").mkdir()
    (data / ".ssh" / "id_backup").write_text("PRIVATE KEY")
    (data / ".web_secret").write_text("s3cret")
    (data / ".backup_pending").touch()
    (data / "scratch.tmp").write_text("x")
    cfg = tmp_path / "etc" / "projektor.toml"
    cfg.parent.mkdir()
    cfg.write_text(TOML)
    wifi = _wifi(tmp_path)
    wifi.add("Theater", "geheim123", 10)
    wifi.add("Uni", "", 3, {"identity": "u@uni", "password": "pw;x", "ca_cert": ""})
    mgr = BackupManager(settings, store, ctrl, remote=f"{remote}/", rsh=None, config_path=cfg, wifi=wifi)
    return settings, store, ctrl, mgr, remote, cfg, wifi


def test_backup_state_config_wifi(full):
    settings, store, ctrl, mgr, remote, cfg, wifi = full
    mgr.backup_now(wait=True)
    assert mgr.status()["state"] == "done", mgr.status()
    # generic state mirror: results + design, but no media, keys, secrets or transient files
    assert json.loads((remote / "state" / "results.json").read_text())["ja"] == 3
    assert (remote / "state" / "design" / "font.ttf").exists()
    for gone in ("media", ".ssh", ".web_secret", ".backup_pending", "scratch.tmp", "show.json"):
        assert not (remote / "state" / gone).exists(), gone
    assert not list(remote.rglob("id_backup"))
    assert (remote / "config" / "projektor.toml").read_text() == TOML
    nets = json.loads((remote / "config" / "wifi.json").read_text())["networks"]
    assert {n["ssid"]: n["psk"] for n in nets} == {"Theater": "geheim123", "Uni": ""}
    assert oct((remote / "config" / "wifi.json").stat().st_mode & 0o777) == "0o600"
    names = sorted(p.name for p in (remote / "snapshots").iterdir())
    assert sum(n.startswith("show-") for n in names) == 1
    assert sum(n.startswith("config-") and n.endswith(".toml") for n in names) == 1
    assert sum(n.startswith("wifi-") and n.endswith(".json") for n in names) == 1
    assert len(mgr.list_snapshots()) == 1  # show listing unchanged by config snapshots

    # unchanged config/wifi -> no new timestamped copies; state mirror follows deletions
    time.sleep(1.05)
    (store.data_dir / "design" / "font.ttf").unlink()
    mgr.backup_now(wait=True)
    names2 = sorted(p.name for p in (remote / "snapshots").iterdir())
    assert sum(n.startswith(("config-", "wifi-")) for n in names2) == 2
    assert sum(n.startswith("show-") for n in names2) == 2
    assert not (remote / "state" / "design" / "font.ttf").exists()
    cfg.write_text(TOML + "\n# changed\n")
    time.sleep(1.05)
    mgr.backup_now(wait=True)
    assert sum(p.name.startswith("config-") for p in (remote / "snapshots").iterdir()) == 2


def test_backup_wifi_failure_does_not_fail_backup(full, tmp_path):
    from projektor.web.system import WifiCli

    settings, store, ctrl, _m, remote, cfg, _w = full
    mgr = BackupManager(settings, store, ctrl, remote=f"{remote}/", rsh=None, config_path=cfg,
                        wifi=WifiCli(["/nonexistent/projektor-wifi"]))
    mgr.backup_now(wait=True)
    st = mgr.status()
    assert st["state"] == "done" and "WiFi not backed up" in st["message"]
    assert (remote / "config" / "projektor.toml").exists() and not (remote / "config" / "wifi.json").exists()


def test_restore_state_and_config(full, tmp_path):
    settings, store, ctrl, mgr, remote, cfg, wifi = full
    mgr.backup_now(wait=True)
    data = store.data_dir
    # disaster: state gone, settings changed, new wifi stub (fresh SD card)
    (data / "results.json").write_text('{"ja": 0, "nein": 0, "poll_id": 0, "time": 2.0}')
    shutil.rmtree(data / "design")
    (data / ".ssh" / "id_backup").write_text("LOCAL KEY")
    cfg.write_text('[device]\nname = "other"\n')
    wifi2 = _wifi(tmp_path, "wifi2.json")
    mgr.wifi = wifi2
    wifi2.add("Existing", "keepme1234")

    # default restore: show only
    mgr.restore("latest", wait=True)
    assert mgr.status()["state"] == "done"
    assert json.loads((data / "results.json").read_text())["ja"] == 0
    assert 'name = "other"' in cfg.read_text()

    # state only
    mgr.restore("latest", state=True, wait=True)
    st = mgr.status()
    assert st["state"] == "done" and "state" in st["message"], st
    assert json.loads((data / "results.json").read_text())["ja"] == 3
    assert (data / "design" / "font.ttf").exists()
    assert (data / ".ssh" / "id_backup").read_text() == "LOCAL KEY"
    assert 'name = "other"' in cfg.read_text()

    # config: settings replaced (old kept), wifi added (existing kept)
    mgr.restore("latest", config=True, wait=True)
    st = mgr.status()
    assert st["state"] == "done" and "restart needed" in st["message"], st
    assert cfg.read_text() == TOML
    assert 'name = "other"' in (cfg.parent / "projektor.toml.before-restore").read_text()
    assert {n["ssid"] for n in wifi2.list()["networks"]} == {"Existing", "Theater", "Uni"}
    exported = {n["ssid"]: n for n in wifi2.export()["networks"]}
    assert exported["Theater"]["psk"] == "geheim123" and exported["Uni"]["eap"]["password"] == "pw;x"


def test_restore_invalid_config_rejected(full):
    settings, store, ctrl, mgr, remote, cfg, wifi = full
    mgr.backup_now(wait=True)
    (remote / "config" / "projektor.toml").write_text("[web]\nport = \"not a port\"\n")
    cfg.write_text('[device]\nname = "keep"\n')
    mgr.restore("latest", config=True, wait=True)
    st = mgr.status()
    assert st["state"] == "error" and "invalid" in st["message"], st
    assert 'name = "keep"' in cfg.read_text()
    assert not (cfg.parent / "projektor.toml.before-restore").exists()
    (remote / "config" / "projektor.toml").write_text("this is [not toml")
    mgr.restore("latest", config=True, wait=True)
    assert mgr.status()["state"] == "error"


def test_restore_missing_parts_message(setup):
    settings, store, ctrl, mgr, remote = setup
    mgr.backup_now(wait=True)
    shutil.rmtree(remote / "state", ignore_errors=True)
    mgr.restore("latest", state=True, wait=True)
    st = mgr.status()
    assert st["state"] == "error" and "no state in the backup" in st["message"], st


def test_restore_api_options(full):
    settings, store, ctrl, mgr, remote, cfg, wifi = full
    app = create_app(settings, ctrl, FakeDispatcher(ctrl), store, backup=mgr)
    c = TestClient(app)
    c.post("/api/backup/run")
    mgr.wait(30)
    cfg.write_text('[device]\nname = "x"\n')
    assert c.post("/api/backup/restore", json={"state": True, "config": True}).status_code == 200
    mgr.wait(30)
    st = c.get("/api/backup").json()
    assert st["state"] == "done" and "settings" in st["message"] and "state" in st["message"], st
    assert cfg.read_text() == TOML
