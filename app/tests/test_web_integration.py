"""Web app against the real core: Settings, ShowStore, Controller (+FakePlayer), CommandDispatcher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

config = pytest.importorskip("projektor.config")
controller_mod = pytest.importorskip("projektor.controller")
commands = pytest.importorskip("projektor.commands")
fake_player = pytest.importorskip("projektor.player.fake")

from projektor.web.app import create_app  # noqa: E402

STUB = Path(__file__).resolve().parents[1] / "scripts" / "projektor-wifi-stub.py"

SHOW = {
    "version": 1,
    "videos": {
        "a": {"file": "a.mp4", "title": "A", "pause_points": [5.0]},
        "b": {"file": "b.mp4", "title": "B", "end": "hold"},
    },
    "cues": [
        {"id": "1", "name": "One", "video": "a"},
        {"id": "2", "name": "Two", "video": "b"},
    ],
}


@pytest.fixture
def real(tmp_path):
    settings = config.Settings.model_validate({
        "device": {"name": "proj-int"},
        "paths": {"data_dir": str(tmp_path), "net_status": str(tmp_path / "net.json"),
                  "tunnel_status": str(tmp_path / "tunnel.json")},
        "lora": {"enabled": False},
        "backup": {"enabled": False},
        "results": {"design_url": ""},
    })
    store = config.ShowStore.from_settings(settings)
    store.media_dir.mkdir(parents=True)
    (store.media_dir / "a.mp4").write_bytes(b"a")
    (store.media_dir / "b.mp4").write_bytes(b"b")
    store.path.write_text(json.dumps(SHOW))
    player = fake_player.FakePlayer(default_duration=30.0)
    ctrl = controller_mod.Controller(settings, store, player)
    ctrl.start()
    disp = commands.CommandDispatcher(ctrl, settings)
    app = create_app(settings, ctrl, disp, store,
                     wifi_cmd=[sys.executable, str(STUB), "--state", str(tmp_path / "wifi.json")],
                     power_cmds={"reboot": None, "shutdown": None})
    return TestClient(app), ctrl, player, store


def test_show_and_edit_with_real_models(real):
    c, ctrl, player, store = real
    show = c.get("/api/show").json()
    assert show["videos"]["a"]["fade_in"] == 1.0  # real model default
    bad = json.loads(json.dumps(show))
    bad["cues"][0]["video"] = "ghost"
    r = c.put("/api/show", json=bad)
    assert r.status_code == 422 and "ghost" in json.dumps(r.json())
    # pause point after out_point is rejected by the real validator
    assert c.patch("/api/videos/a", json={"out_point": 4.0}).status_code == 422
    assert c.patch("/api/videos/a", json={"out_point": 20.0, "pause_points": [3, 7]}).status_code == 200
    assert store.load().videos["a"].pause_points == [3.0, 7.0]
    r = c.post("/api/cues", json={"name": "Three", "video": "a", "start_at": 2})
    assert r.status_code == 200 and r.json()["cue"]["id"] == "3"
    assert c.post("/api/cues/reorder", json={"order": ["3", "1", "2"]}).status_code == 200
    assert [cu.id for cu in store.load().cues] == ["3", "1", "2"]
    assert len(ctrl.show.cues) == 3  # controller reloaded
    # upload -> new video gets the real model's defaults
    r = c.put("/api/upload?name=new clip.mp4", content=b"data" * 100)
    assert r.status_code == 200
    v = c.get("/api/show").json()["videos"][r.json()["video_id"]]
    assert v["fade_in"] == 1.0 and v["fade_out"] == 2.0 and v["end"] == "black"


def test_transport_and_state(real):
    c, ctrl, player, store = real
    r = c.post("/api/transport/cue", json={"ref": "1"})
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert st["cue"] == "1" and st["video"] == "a" and st["standby"] == "2"
    assert st["last_command"]["source"] == "web" and st["last_command"]["address"] == "/proj/cue"
    player.advance(6.0)  # runs into pause point 5.0
    st = c.get("/api/state").json()
    assert st["state"] == "paused" and st["pause_reason"] == "pause_point"
    assert c.post("/api/transport/go").status_code == 200
    assert c.get("/api/state").json()["state"] in ("playing", "fading_in")
    assert c.post("/api/transport/cue", json={"ref": "99"}).status_code == 400
    assert c.post("/api/transport/play", json={"video": "nope"}).status_code == 400
    assert c.post("/api/transport/stop").status_code == 200
    player.advance(0.1)
    assert c.get("/api/state").json()["state"] == "idle"


def test_raw_command_and_ws(real):
    c, ctrl, player, store = real
    r = c.post("/api/command", json={"line": "/proj/ping"})
    assert r.json()["replies"] == [{"address": "/proj/pong", "args": ["proj-int"]}]
    r = c.post("/api/command", json={"line": "/proj/cue nope"})
    assert r.json()["replies"][0]["address"] == "/proj/error"
    with c.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "state"
        c.post("/api/command", json={"line": "/proj/cue 2"})
        for _ in range(10):
            st = ws.receive_json()["state"]
            if st["cue"] == "2":
                break
        assert st["cue"] == "2" and st["last_command"]["source"] == "web"


def test_wifi_and_system(real):
    c, *_ = real
    assert c.get("/api/wifi").status_code == 200
    s = c.get("/api/system").json()
    assert s["device"] == "proj-int" and s["net"]["state"] == "unknown"


def test_results_cue_editing_and_results_api(real):
    c, ctrl, player, store = real
    r = c.post("/api/cues", json={"name": "Ergebnis", "type": "results"})
    assert r.status_code == 200, r.text
    cue = r.json()["cue"]
    assert cue["type"] == "results" and cue["video"] == ""
    assert c.put(f"/api/cues/{cue['id']}", json={"name": "Graph"}).json()["cue"]["name"] == "Graph"
    assert c.post("/api/cues", json={"name": "x", "type": "bogus"}).status_code == 422
    assert c.post("/api/cues", json={"name": "x", "video": "nope"}).status_code == 422
    # stored results + design status
    assert c.get("/api/results").json()["results"] is None
    assert c.post("/api/results", json={"ja": 4, "nein": 5, "poll_id": 2}).json()["results"]["nein"] == 5
    d = c.get("/api/results").json()["design"]
    assert d["source"] == "default" and d["version"]
    assert c.post("/api/design/fetch", json={}).status_code == 400  # no design_url configured
    ctrl.cue(cue["id"])
    assert player.results == (4, 5)
