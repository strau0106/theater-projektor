"""Web API tests with fake controller / dispatcher / show store."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektor.web.app import create_app, parse_command_line
from projektor.web.auth import COOKIE_NAME, hash_password, verify_password
from projektor.web.fakes import FakeController, FakeDispatcher, FakeShowStore, dev_settings

STUB = Path(__file__).resolve().parents[1] / "scripts" / "projektor-wifi-stub.py"

SHOW = {
    "version": 1,
    "videos": {
        "intro": {"file": "intro.mp4", "title": "Intro", "in_point": 0.0, "out_point": None,
                  "pause_points": [12.5], "fade_in": 1.0, "fade_out": 2.0, "end": "black"},
        "outro": {"file": "outro.mp4", "title": "Outro", "in_point": 0.0, "out_point": None,
                  "pause_points": [], "fade_in": 0.0, "fade_out": 0.0, "end": "hold"},
    },
    "cues": [
        {"id": "1", "name": "Opening", "video": "intro", "fade_in": None, "fade_out": None,
         "pause_points": None, "end": None, "start_at": None},
        {"id": "2", "name": "Finale", "video": "outro", "fade_in": None, "fade_out": None,
         "pause_points": None, "end": None, "start_at": None},
    ],
}


class Env:
    def __init__(self, tmp_path: Path, password_hash: str = ""):
        self.data = tmp_path / "data"
        self.settings = dev_settings(self.data, password_hash=password_hash)
        self.store = FakeShowStore(self.data)
        (self.store.media_dir / "intro.mp4").write_bytes(b"x" * 100)
        (self.store.media_dir / "outro.mp4").write_bytes(b"y" * 50)
        self.store.path.write_text(json.dumps(SHOW))
        self.ctrl = FakeController(self.store)
        self.disp = FakeDispatcher(self.ctrl)
        self.app = create_app(
            self.settings, self.ctrl, self.disp, self.store,
            wifi_cmd=[sys.executable, str(STUB), "--state", str(tmp_path / "wifi.json")],
            power_cmds={"reboot": None, "shutdown": None},
        )
        self.client = TestClient(self.app)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ------------------------------------------------------------------ auth


def test_hash_roundtrip():
    h = hash_password("geheim")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("geheim", h)
    assert not verify_password("falsch", h)
    hs = hash_password("geheim", method="scrypt")
    assert verify_password("geheim", hs) and not verify_password("x", hs)
    assert not verify_password("geheim", "") and not verify_password("geheim", "garbage$1")


def test_auth_cli(capsys):
    from projektor.web import auth

    assert auth.main(["hash", "pw12345"]) == 0
    h = capsys.readouterr().out.strip()
    assert auth.main(["verify", h, "pw12345"]) == 0
    assert auth.main(["verify", h, "nope"]) == 1


def test_auth_flow(tmp_path):
    e = Env(tmp_path, password_hash=hash_password("bühne"))
    c = e.client
    assert c.get("/api/auth").json() == {"auth_required": True, "authenticated": False, "device": "projektor-dev"}
    assert c.get("/api/show").status_code == 401
    assert c.post("/api/transport/go").status_code == 401
    assert c.get("/").status_code == 200  # UI itself is public (shows login form)
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    r = c.post("/api/login", json={"password": "bühne"})
    assert r.status_code == 200 and COOKIE_NAME in r.cookies
    assert c.get("/api/auth").json()["authenticated"] is True
    assert c.get("/api/show").status_code == 200
    # secret persisted with 0600
    secret = e.data / ".web_secret"
    assert secret.exists() and (secret.stat().st_mode & 0o777) == 0o600
    # forged cookie rejected
    c2 = TestClient(e.app)
    c2.cookies.set(COOKIE_NAME, "forged.token.value")
    assert c2.get("/api/show").status_code == 401
    # logout
    c.post("/api/logout")
    c.cookies.clear()
    assert c.get("/api/show").status_code == 401
    # websocket also guarded
    with pytest.raises(Exception):
        with c.websocket_connect("/api/ws") as ws:
            ws.receive_json()


def test_session_survives_restart_and_dies_on_password_change(tmp_path):
    e = Env(tmp_path, password_hash=hash_password("a1"))
    e.client.post("/api/login", json={"password": "a1"})
    token = e.client.cookies.get(COOKIE_NAME)
    # same secret + same hash -> still valid in a new app instance
    app2 = create_app(e.settings, e.ctrl, e.disp, e.store)
    c2 = TestClient(app2)
    c2.cookies.set(COOKIE_NAME, token)
    assert c2.get("/api/state").status_code == 200
    # password changed -> session invalid
    e.settings.web.password_hash = hash_password("b2")
    app3 = create_app(e.settings, e.ctrl, e.disp, e.store)
    c3 = TestClient(app3)
    c3.cookies.set(COOKIE_NAME, token)
    assert c3.get("/api/state").status_code == 401


def test_no_auth_when_hash_empty(env):
    assert env.client.get("/api/auth").json()["auth_required"] is False
    assert env.client.get("/api/show").status_code == 200


# ------------------------------------------------------------------ show


def test_show_roundtrip_and_validation(env):
    c = env.client
    show = c.get("/api/show").json()
    assert show["videos"]["intro"]["pause_points"] == [12.5]
    show["cues"][0]["name"] = "Opening (new)"
    before = len(env.ctrl.calls)
    r = c.put("/api/show", json=show)
    assert r.status_code == 200, r.text
    assert ("reload_show",) in env.ctrl.calls[before:]
    assert c.get("/api/show").json()["cues"][0]["name"] == "Opening (new)"
    # invalid: cue references unknown video
    bad = json.loads(json.dumps(show))
    bad["cues"][0]["video"] = "nope"
    saves = env.store.saves
    r = c.put("/api/show", json=bad)
    assert r.status_code == 422
    assert env.store.saves == saves
    # invalid type
    bad2 = json.loads(json.dumps(show))
    bad2["videos"]["intro"]["fade_in"] = "slow"
    assert c.put("/api/show", json=bad2).status_code == 422
    assert c.put("/api/show", json=[1, 2]).status_code == 422
    # download
    r = c.get("/api/show/download")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.json()["cues"][0]["name"] == "Opening (new)"


# ------------------------------------------------------------------ videos


def test_upload_list_and_delete_guard(env):
    c = env.client
    payload = os.urandom(300_000)
    r = c.put("/api/upload?name=../evil dir/My Clip.mp4", content=payload)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["file"] == "My Clip.mp4" and j["size"] == len(payload)
    assert j["video_id"] == "my-clip"
    assert (env.store.media_dir / "My Clip.mp4").read_bytes() == payload
    assert not list(env.store.media_dir.glob(".upload-*"))
    # garbage file -> probe error, reported as warning (no crash)
    assert j["warnings"]
    # duplicate without overwrite
    assert c.put("/api/upload?name=My Clip.mp4", content=b"abc").status_code == 409
    assert c.put("/api/upload?name=My Clip.mp4&overwrite=true", content=b"abc").status_code == 200
    # list
    lst = c.get("/api/videos").json()
    ids = {v["id"] for v in lst["videos"]}
    assert ids == {"intro", "outro", "my-clip"}
    mc = next(v for v in lst["videos"] if v["id"] == "my-clip")
    assert mc["size"] == 3 and mc["exists"] and mc["used_by"] == []
    # delete guard: intro used by cue 1
    r = c.delete("/api/videos/intro")
    assert r.status_code == 409
    assert (env.store.media_dir / "intro.mp4").exists()
    r = c.delete("/api/videos/intro?force=true")
    assert r.status_code == 200 and r.json()["removed_cues"] == ["1"]
    show = c.get("/api/show").json()
    assert "intro" not in show["videos"] and [cu["id"] for cu in show["cues"]] == ["2"]
    assert not (env.store.media_dir / "intro.mp4").exists()
    # unused video deletes fine
    assert c.delete("/api/videos/my-clip").status_code == 200
    assert not (env.store.media_dir / "My Clip.mp4").exists()


def test_upload_limits(env):
    env.settings.web.max_upload_mb = 0.001  # ~1 KB
    app = create_app(env.settings, env.ctrl, env.disp, env.store)
    c = TestClient(app)
    assert c.put("/api/upload?name=big.mp4", content=b"z" * 5000).status_code == 413
    assert not (env.store.media_dir / "big.mp4").exists()
    assert not list(env.store.media_dir.glob(".upload-*"))
    assert c.put("/api/upload?name=..", content=b"z").status_code == 400
    assert c.put("/api/upload?name=empty.mp4", content=b"").status_code == 400


def test_orphan_files_and_create_video(env):
    c = env.client
    (env.store.media_dir / "loose.mp4").write_bytes(b"1234")
    lst = c.get("/api/videos?probe=false").json()
    assert [f["name"] for f in lst["files"]] == ["loose.mp4"]
    r = c.post("/api/videos", json={"file": "loose.mp4", "title": "Loose"})
    assert r.status_code == 200 and r.json()["id"] == "loose"
    assert c.get("/api/videos?probe=false").json()["files"] == []
    assert c.post("/api/videos", json={"file": "missing.mp4"}).status_code == 400
    # orphan file delete: refused while used
    assert c.delete("/api/files/loose.mp4").status_code == 409


def test_video_metadata_edit_and_rename(env):
    c = env.client
    r = c.patch("/api/videos/intro", json={
        "title": "Intro 2", "in_point": 1.5, "out_point": 80, "pause_points": "30, 12.5 ,45",
        "fade_in": 0.5, "fade_out": 3, "end": "loop"})
    assert r.status_code == 200, r.text
    v = c.get("/api/show").json()["videos"]["intro"]
    assert v["title"] == "Intro 2" and v["in_point"] == 1.5 and v["out_point"] == 80.0
    assert v["pause_points"] == [12.5, 30.0, 45.0] and v["end"] == "loop"
    assert c.patch("/api/videos/intro", json={"end": "explode"}).status_code == 422
    assert c.patch("/api/videos/intro", json={"out_point": 1.0}).status_code == 422
    assert c.patch("/api/videos/intro", json={"fade_in": -1}).status_code == 422
    assert c.patch("/api/videos/nope", json={"title": "x"}).status_code == 404
    # rename id + file; cues follow
    r = c.post("/api/videos/intro/rename", json={"new_id": "prolog", "file": "prolog.mp4"})
    assert r.status_code == 200, r.text
    show = c.get("/api/show").json()
    assert "prolog" in show["videos"] and show["videos"]["prolog"]["file"] == "prolog.mp4"
    assert show["cues"][0]["video"] == "prolog"
    assert (env.store.media_dir / "prolog.mp4").exists() and not (env.store.media_dir / "intro.mp4").exists()
    assert c.post("/api/videos/prolog/rename", json={"new_id": "outro"}).status_code == 409
    assert c.post("/api/videos/prolog/rename", json={"file": "outro.mp4"}).status_code == 409


# ------------------------------------------------------------------ cues


def test_cue_crud_and_reorder(env):
    c = env.client
    r = c.post("/api/cues", json={"name": "Middle", "video": "intro", "fade_in": "0.5", "pause_points": "3,1"})
    assert r.status_code == 200, r.text
    cue = r.json()["cue"]
    assert cue["id"] == "3" and cue["fade_in"] == 0.5 and cue["pause_points"] == [1.0, 3.0]
    assert c.post("/api/cues", json={"id": "1", "video": "intro"}).status_code == 409
    assert c.post("/api/cues", json={"video": "nope"}).status_code == 422
    r = c.post("/api/cues", json={"id": "0.5", "name": "Pre", "video": "outro", "index": 0})
    assert r.status_code == 200
    assert [x["id"] for x in c.get("/api/cues").json()] == ["0.5", "1", "2", "3"]
    # update
    r = c.put("/api/cues/3", json={"name": "Middle!", "end": "hold", "fade_in": None, "start_at": 4})
    assert r.status_code == 200
    cue = next(x for x in c.get("/api/cues").json() if x["id"] == "3")
    assert cue["name"] == "Middle!" and cue["end"] == "hold" and cue["fade_in"] is None and cue["start_at"] == 4.0
    assert c.put("/api/cues/3", json={"id": "1"}).status_code == 409
    assert c.put("/api/cues/3", json={"end": "bad"}).status_code == 422
    assert c.put("/api/cues/99", json={"name": "x"}).status_code == 404
    r = c.patch("/api/cues/3", json={"id": "2.5"})
    assert r.status_code == 200
    # reorder
    r = c.post("/api/cues/reorder", json={"order": ["2", "2.5", "1", "0.5"]})
    assert r.status_code == 200
    assert [x["id"] for x in c.get("/api/cues").json()] == ["2", "2.5", "1", "0.5"]
    assert c.post("/api/cues/reorder", json={"order": ["2", "1"]}).status_code == 422
    assert c.post("/api/cues/reorder", json={"order": ["2", "2", "1", "0.5"]}).status_code == 422
    # delete
    assert c.delete("/api/cues/2.5").status_code == 200
    assert c.delete("/api/cues/2.5").status_code == 404
    assert [x["id"] for x in c.get("/api/cues").json()] == ["2", "1", "0.5"]
    assert ("reload_show",) in env.ctrl.calls


# ------------------------------------------------------------------ transport / command


def test_transport_endpoints_call_controller(env):
    c = env.client
    env.ctrl.calls.clear()
    seq = [
        ("standby", {"ref": "2"}, ("standby", "2")),
        ("cue", {"ref": 1}, ("cue", 1)),
        ("go", None, ("go",)),
        ("pause", None, ("pause",)),
        ("resume", None, ("resume",)),
        ("seek", {"seconds": 12.5}, ("seek", 12.5)),
        ("play", {"video": "outro", "fade_in": 2}, ("play", "outro", 2.0)),
        ("fadeout", {"seconds": 3}, ("fadeout", 3.0)),
        ("fadeout", {}, ("fadeout", None)),
        ("stop", None, ("stop",)),
        ("status", {"on": True}, ("status", 1)),
        ("status", {"on": False}, ("status", 0)),
        ("reload", None, ("reload_show",)),
    ]
    for action, body, expected in seq:
        r = c.post(f"/api/transport/{action}", json=body) if body is not None else c.post(f"/api/transport/{action}")
        assert r.status_code == 200, (action, r.text)
        assert env.ctrl.calls[-1] == expected, action
        assert "state" in r.json()
    assert c.post("/api/transport/cue", json={"ref": "nope"}).status_code == 400
    assert c.post("/api/transport/cue", json={}).status_code == 400
    assert c.post("/api/transport/seek", json={}).status_code == 400
    assert c.post("/api/transport/explode").status_code == 404
    st = c.get("/api/state").json()
    assert st["state"] == "idle"


def test_raw_command_goes_through_dispatcher(env):
    c = env.client
    r = c.post("/api/command", json={"line": "/proj/cue 2"})
    assert r.status_code == 200, r.text
    assert env.disp.calls[-1] == ("/proj/cue", [2], "web")
    assert env.ctrl.calls[-1] == ("cue", 2)
    r = c.post("/api/command", json={"address": "/proj/ping", "args": []})
    assert r.json()["replies"] == [{"address": "/proj/pong", "args": ["projektor-dev"]}]
    r = c.post("/api/command", json={"line": "/proj/bogus 'a b' 1.5"})
    assert env.disp.calls[-1] == ("/proj/bogus", ["a b", 1.5], "web")
    assert r.json()["replies"][0]["address"] == "/proj/error"
    assert c.post("/api/command", json={"line": "proj/go"}).status_code == 400
    assert c.post("/api/command", json={"line": ""}).status_code == 400
    assert parse_command_line("/proj/seek -3") == ("/proj/seek", [-3])


# ------------------------------------------------------------------ websocket


def test_websocket_receives_state(env):
    c = env.client
    with c.websocket_connect("/api/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["state"]["state"] == "idle"
        assert env.ctrl.subscriber_count == 1
        # command from another thread (TestClient request -> threadpool -> callback)
        c.post("/api/transport/cue", json={"ref": "1"})
        for _ in range(10):
            msg = ws.receive_json()
            if msg["state"]["state"] == "playing":
                break
        assert msg["state"]["cue"] == "1" and msg["state"]["video"] == "intro"
        # periodic push continues without commands (position updates)
        msg2 = ws.receive_json()
        assert msg2["type"] == "state"
    # unsubscribed after disconnect
    import time

    for _ in range(50):
        if env.ctrl.subscriber_count == 0:
            break
        time.sleep(0.02)
    assert env.ctrl.subscriber_count == 0


def test_websocket_threaded_callbacks(tmp_path):
    e = Env(tmp_path)
    e.ctrl._notify_thread = True  # callbacks from a foreign thread
    with e.client.websocket_connect("/api/ws") as ws:
        ws.receive_json()
        e.ctrl.stop()  # direct call from the test thread
        e.ctrl.show_status(False)
        seen = [ws.receive_json()["state"]["status_screen"] for _ in range(3)]
        assert False in seen


# ------------------------------------------------------------------ system


def test_wifi_via_stub(env):
    c = env.client
    r = c.get("/api/wifi")
    assert r.status_code == 200
    assert [n["ssid"] for n in r.json()["networks"]] == ["Theater"]
    r = c.post("/api/wifi", json={"ssid": "Probe", "psk": "geheim123", "priority": 5})
    assert r.status_code == 200, r.text
    assert {n["ssid"] for n in c.get("/api/wifi").json()["networks"]} == {"Theater", "Probe"}
    assert c.post("/api/wifi", json={"ssid": "X", "psk": "short"}).status_code == 400
    assert c.post("/api/wifi", json={"psk": "longenough"}).status_code == 400
    scan = c.post("/api/wifi/scan").json()
    assert any(n["ssid"] == "Theater" and n["known"] for n in scan["networks"])
    assert c.post("/api/wifi/retry").json()["ok"] is True
    assert c.delete("/api/wifi/Probe").status_code == 200
    r = c.delete("/api/wifi/Unknown")
    assert r.status_code == 502 and "unknown network" in r.json()["detail"]


def test_wifi_missing_cli(tmp_path):
    e = Env(tmp_path)
    app = create_app(e.settings, e.ctrl, e.disp, e.store, wifi_cmd=["/nonexistent/projektor-wifi"])
    r = TestClient(app).get("/api/wifi")
    assert r.status_code == 503


def test_system_status_and_power(env, tmp_path):
    c = env.client
    st = c.get("/api/system").json()
    assert st["net"]["state"] == "unknown" and st["tunnel"]["state"] == "unknown"
    Path(env.settings.paths.net_status).write_text(json.dumps({"state": "connected", "ssid": "Theater", "ip": "1.2.3.4", "updated": 1}))
    Path(env.settings.paths.tunnel_status).write_text("{broken")
    st = c.get("/api/system").json()
    assert st["net"]["ssid"] == "Theater" and st["tunnel"]["state"] == "unknown"
    assert "disk" in st["info"]
    assert c.post("/api/system/reboot", json={}).status_code == 400
    r = c.post("/api/system/reboot", json={"confirm": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    # configured command is executed
    marker = tmp_path / "powered-off"
    app = create_app(env.settings, env.ctrl, env.disp, env.store,
                     power_cmds={"reboot": None, "shutdown": ["touch", str(marker)]})
    r = TestClient(app).post("/api/system/shutdown", json={"confirm": True})
    assert r.status_code == 200 and marker.exists()


def test_static_ui_served(env):
    r = env.client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert env.client.get("/static/app.js").status_code == 200
    assert env.client.get("/static/app.css").status_code == 200
