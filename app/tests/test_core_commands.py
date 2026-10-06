from __future__ import annotations

import json

import pytest

from projektor import ProjektorError
from projektor.commands import coerce_bool, coerce_float, coerce_ref, coerce_str


def test_ping_and_state(disp):
    assert disp.handle("/proj/ping", [], "osc") == [("/proj/pong", ["proj-test"])]
    replies = disp.handle("/proj/state", [], "osc")
    assert replies[0][0] == "/proj/state"
    st = json.loads(replies[0][1][0])
    assert st["state"] == "idle" and st["standby"] == "1"


def test_cue_go_and_last_command(disp, ctrl, player):
    assert disp.handle("/proj/cue", [1], "osc") == []
    st = ctrl.state()
    assert st["cue"] == "1"
    assert st["last_command"]["address"] == "/proj/cue"
    assert st["last_command"]["args"] == [1]
    assert st["last_command"]["source"] == "osc"
    disp.test_clock["t"] += 5
    disp.handle("/proj/go", [], "lora")
    st = ctrl.state()
    assert st["cue"] == "2" and st["last_command"]["source"] == "lora"


def test_all_commands_route(disp, ctrl, player):
    assert disp.handle("/proj/play", ["hold", 0], "web") == []
    assert ctrl.state()["video"] == "hold"
    for addr, args, check in [
        ("/proj/pause", [], lambda s: s["state"] == "paused"),
        ("/proj/resume", [], lambda s: s["state"] == "playing"),
        ("/proj/seek", [3], lambda s: s["position"] == 3.0),
        ("/proj/fadeout", [0.2], lambda s: s["state"] == "fading_out"),
        ("/proj/stop", [], lambda s: s["state"] == "idle"),
        ("/proj/standby", ["3"], lambda s: s["standby"] == "3"),
        ("/proj/status", [1], lambda s: s["status_screen"] is True),
        ("/proj/reload", [], lambda s: s["standby"] == "3"),
    ]:
        assert disp.handle(addr, args, "web") == [], addr
        assert check(ctrl.state()), addr


@pytest.mark.parametrize(
    "addr, args",
    [
        ("/proj/cue", [2.0]),  # integral float -> number
        ("/proj/cue", ["2"]),  # string id
        ("/proj/cue", [b"2"]),
    ],
)
def test_cue_arg_coercion(disp, ctrl, addr, args):
    assert disp.handle(addr, args, "osc") == []
    assert ctrl.state()["cue"] == "2"


def test_numeric_string_args(disp, ctrl, player):
    disp.handle("/proj/play", [" hold ", "1.5"], "osc")
    assert player.clip.fade_in == 1.5
    disp.handle("/proj/seek", ["2.5"], "osc")
    assert ctrl.state()["position"] == 2.5
    disp.handle("/proj/fadeout", [1], "osc")
    assert ("fadeout", 1.0) in player.calls


@pytest.mark.parametrize("val, expected", [(0, False), (1, True), (True, True), (False, False),
                                           ("1", True), ("0", False), ("on", True), ("off", False),
                                           (0.0, False), (1.0, True)])
def test_status_coercion(disp, ctrl, val, expected):
    disp.handle("/proj/status", [val], "osc")
    assert ctrl.state()["status_screen"] is expected


@pytest.mark.parametrize(
    "addr, args, msg",
    [
        ("/proj/nope", [], "unknown address"),
        ("/other/go", [], "unknown address"),
        ("/proj/cue", [], "usage"),
        ("/proj/cue", [1, 2], "usage"),
        ("/proj/cue", [99], "unknown cue"),
        ("/proj/cue", ["zzz"], "unknown cue"),
        ("/proj/cue", [True], "cue ref"),
        ("/proj/go", ["x"], "usage"),
        ("/proj/seek", ["abc"], "number"),
        ("/proj/seek", [], "usage"),
        ("/proj/seek", [-3], ">= 0"),
        ("/proj/play", ["nope"], "unknown video"),
        ("/proj/play", ["hold", "slow"], "number"),
        ("/proj/fadeout", ["x"], "number"),
        ("/proj/fadeout", [float("nan")], "invalid"),
        ("/proj/status", ["maybe"], "0 or 1"),
    ],
)
def test_errors_reply(disp, ctrl, addr, args, msg):
    replies = disp.handle(addr, args, "osc")
    assert len(replies) == 1
    assert replies[0][0] == "/proj/error"
    assert msg in replies[0][1][0]
    assert ctrl.state()["state"] == "idle"


def test_go_without_standby_error(disp):
    disp.handle("/proj/cue", ["finale"], "osc")
    r = disp.handle("/proj/go", [], "osc")
    assert r[0][0] == "/proj/error" and "standby" in r[0][1][0]


def test_internal_error_is_reported(disp, ctrl, monkeypatch):
    def boom():
        raise RuntimeError("kaputt")

    monkeypatch.setattr(ctrl, "stop", boom)
    r = disp.handle("/proj/stop", [], "osc")
    assert r == [("/proj/error", ["internal error: kaputt"])]


def test_cross_transport_dedup(disp, ctrl, player):
    disp.handle("/proj/go", [], "osc")
    assert ctrl.state()["cue"] == "1"
    disp.test_clock["t"] += 0.4
    assert disp.handle("/proj/go", [], "lora") == []  # duplicate via other transport: dropped
    assert ctrl.state()["cue"] == "1"
    assert ctrl.state()["last_command"]["source"] == "osc"
    # same transport is not deduped
    disp.handle("/proj/go", [], "osc")
    assert ctrl.state()["cue"] == "2"
    # outside the window: accepted
    disp.test_clock["t"] += 1.5
    disp.handle("/proj/go", [], "lora")
    assert ctrl.state()["cue"] == "3"


def test_dedup_considers_args(disp, ctrl):
    disp.handle("/proj/cue", [1], "osc")
    disp.handle("/proj/cue", [2], "lora")
    assert ctrl.state()["cue"] == "2"
    disp.handle("/proj/cue", [2], "osc")  # dup of lora's cue 2 -> dropped (idempotent anyway)
    assert ctrl.state()["last_command"]["source"] == "lora"


def test_dedup_disabled(settings, ctrl):
    from projektor.commands import CommandDispatcher

    settings.osc.dedup_window_s = 0
    d = CommandDispatcher(ctrl, settings, clock=lambda: 5.0)
    d.handle("/proj/go", [], "osc")
    d.handle("/proj/go", [], "lora")
    assert ctrl.state()["cue"] == "2"


def test_coercion_helpers():
    assert coerce_ref(3) == 3 and coerce_ref(3.0) == 3 and coerce_ref(" a ") == "a"
    assert coerce_ref(1.5) == "1.5"
    assert coerce_float("2") == 2.0 and coerce_float(3) == 3.0
    assert coerce_str(7, "x") == "7" and coerce_str(7.0, "x") == "7"
    assert coerce_bool("yes") is True
    for bad in (None, [], ""):
        with pytest.raises(ProjektorError):
            coerce_ref(bad)
    with pytest.raises(ProjektorError):
        coerce_float(True)
    with pytest.raises(ProjektorError):
        coerce_str("  ", "video_id")
