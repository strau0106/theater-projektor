from __future__ import annotations

import json
import time

from conftest import make_settings
from projektor import status
from projektor.status import build_status_text, read_json


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def test_missing_files_detect_local_ip(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    monkeypatch.setattr(status, "local_ip", lambda: "192.168.7.7")
    txt = build_status_text(s, {"transports": {"osc": "listening :9000", "lora": "disabled"},
                                "standby": "1"})
    assert "proj-test" in txt
    assert "WiFi     status unknown" in txt
    assert "192.168.7.7  (detected)" in txt
    assert "http://192.168.7.7:8080/" in txt
    assert "listening :9000" in txt
    assert "LoRa     disabled" in txt
    assert "Tunnel   unknown" in txt
    assert "Standby  cue 1" in txt
    assert "Waiting for first cue" in txt


def test_no_ip_at_all(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    monkeypatch.setattr(status, "local_ip", lambda: None)
    txt = build_status_text(s, {})
    assert "IP       none" in txt and "Web      unknown" in txt


def test_connected(tmp_path):
    s = make_settings(tmp_path, web={"port": 80})
    now = time.time()
    _write(s.paths.net_status, {"state": "connected", "ssid": "Theater", "ip": "192.168.1.42",
                                "attempt": 0, "updated": now})
    _write(s.paths.tunnel_status, {"state": "up", "remote": "infmk1:2222", "updated": now})
    txt = build_status_text(s, {"transports": {"lora": "connected /dev/ttyACM0"}}, now=now)
    assert 'connected  "Theater"' in txt
    assert "IP       192.168.1.42" in txt
    assert "http://192.168.1.42/" in txt  # port 80 omitted
    assert "LoRa     connected /dev/ttyACM0" in txt
    assert "Tunnel   up  infmk1:2222" in txt
    assert "stale" not in txt


def test_connecting_attempt_and_retry(tmp_path):
    s = make_settings(tmp_path)
    _write(s.paths.net_status, {"state": "connecting", "ssid": "Theater", "attempt": 3,
                                "next_retry_s": 12, "updated": time.time() - 600})
    txt = build_status_text(s, {})
    assert "connecting" in txt and "attempt 3" in txt and "next retry in 12 s" in txt
    assert "(stale)" in txt


def test_hotspot(tmp_path):
    s = make_settings(tmp_path)
    _write(s.paths.net_status, {"state": "hotspot", "hotspot_ssid": "projektor-ab12",
                                "hotspot_ip": "10.42.0.1", "attempt": 8})
    txt = build_status_text(s, {})
    assert "hotspot" in txt
    assert "Hotspot  projektor-ab12   10.42.0.1" in txt
    assert "http://10.42.0.1:8080/" in txt


def test_invalid_files_tolerated(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    monkeypatch.setattr(status, "local_ip", lambda: "10.0.0.5")
    _write(s.paths.net_status, "{garbage")
    _write(s.paths.tunnel_status, "[1, 2]")
    txt = build_status_text(s, {"error": "video file not found: x.mp4"})
    assert "status unknown" in txt and "Tunnel   unknown" in txt
    assert "Error    video file not found" in txt
    assert read_json(s.paths.net_status) is None
    assert read_json(None) is None


def test_local_ip_returns_str_or_none():
    ip = status.local_ip()
    assert ip is None or ip.count(".") == 3
