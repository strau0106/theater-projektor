from __future__ import annotations

import json

import pytest

from projektor import ProjektorError
from projektor.controller import Controller, SyncExecutor

STATE_KEYS = {"state", "pause_reason", "cue", "cue_index", "standby", "standby_index", "video",
              "position", "duration", "status_screen", "last_command", "transports"}


def test_initial_state(ctrl, player):
    st = ctrl.state()
    assert STATE_KEYS <= set(st)
    assert st["state"] == "idle"
    assert st["standby"] == "1" and st["standby_index"] == 0
    assert st["cue"] is None and st["video"] is None and st["position"] is None
    assert st["status_screen"] is True
    assert st["transports"] == {"osc": "stopped", "lora": "disabled"}
    assert player.started
    assert player.status_visible and player.status_text == "STATUS standby=1"
    json.dumps(st)  # must be JSON serialisable


def test_cue_runs_and_advances_standby(ctrl, player):
    ctrl.cue(1)
    st = ctrl.state()
    assert st["state"] == "fading_in"
    assert (st["cue"], st["cue_index"], st["standby"], st["standby_index"]) == ("1", 0, "2", 1)
    assert st["video"] == "intro" and st["duration"] == 20.0
    assert not player.status_visible  # boot status screen hidden by first show command
    assert st["status_screen"] is False
    player.advance(1.0)
    assert ctrl.state()["state"] == "playing"


def test_cue_by_id_and_last_cue_clears_standby(ctrl):
    ctrl.cue("finale")
    st = ctrl.state()
    assert st["cue"] == "finale" and st["standby"] is None and st["position"] == 12.0
    with pytest.raises(ProjektorError, match="no standby"):
        ctrl.go()


def test_unknown_cue_raises(ctrl, player):
    with pytest.raises(ProjektorError):
        ctrl.cue(99)
    with pytest.raises(ProjektorError):
        ctrl.cue("nope")
    assert player.state == "idle"
    assert ctrl.state()["status_screen"] is True  # failed command does not hide status


def test_go_runs_standby_then_advances(ctrl, player):
    ctrl.go()
    assert ctrl.state()["cue"] == "1"
    ctrl.go()
    st = ctrl.state()
    assert st["cue"] == "2" and st["standby"] == "3" and st["video"] == "hold"


def test_pause_points_and_go_resumes(ctrl, player):
    ctrl.cue(1)
    player.advance(4.9)
    assert ctrl.state()["state"] == "playing"
    player.advance(0.5)
    st = ctrl.state()
    assert st["state"] == "paused" and st["pause_reason"] == "pause_point"
    assert st["position"] == 5.0
    player.advance(3.0)
    assert ctrl.state()["position"] == 5.0  # stays frozen
    ctrl.go()  # resumes, does NOT run standby cue
    st = ctrl.state()
    assert st["state"] == "playing" and st["cue"] == "1" and st["standby"] == "2"
    player.advance(6.0)
    st = ctrl.state()
    assert st["pause_reason"] == "pause_point" and st["position"] == 10.0
    ctrl.resume()
    player.advance(11.0)  # EOS at 20 -> end=black -> idle
    st = ctrl.state()
    assert st["state"] == "idle" and st["video"] is None


def test_manual_pause_and_go_runs_next(ctrl, player):
    ctrl.cue(1)
    player.advance(2.0)
    ctrl.pause()
    st = ctrl.state()
    assert st["state"] == "paused" and st["pause_reason"] == "manual"
    player.advance(1.0)
    assert ctrl.state()["position"] == 2.0
    ctrl.resume()
    assert ctrl.state()["state"] == "playing"
    ctrl.pause()
    ctrl.go()  # manual pause -> go runs standby cue
    assert ctrl.state()["cue"] == "2"


def test_idempotent_cue(ctrl, player):
    ctrl.cue(1)
    player.advance(3.0)
    plays = [c for c in player.calls if c[0] == "play"]
    ctrl.cue(1)
    ctrl.cue("1")
    assert [c for c in player.calls if c[0] == "play"] == plays
    assert ctrl.state()["position"] == 3.0
    player.advance(2.5)  # paused at pause point 5
    ctrl.cue(1)
    st = ctrl.state()
    assert st["state"] == "paused" and st["position"] == 5.0
    # after it ended, the same cue restarts
    ctrl.stop()
    ctrl.cue(1)
    assert len([c for c in player.calls if c[0] == "play"]) == 2


def test_go_on_current_cue_restarts_not_idempotent(ctrl, player):
    ctrl.cue(1)
    ctrl.standby(1)
    ctrl.go()
    assert len([c for c in player.calls if c[0] == "play"]) == 2


def test_standby_sets_without_playback_change(ctrl, player):
    ctrl.standby("3")
    st = ctrl.state()
    assert st["standby"] == "3" and st["state"] == "idle"
    assert st["status_screen"] is True  # standby is not a show command
    ctrl.go()
    assert ctrl.state()["video"] == "loop"


def test_fadeout_default_and_explicit(ctrl, player):
    ctrl.cue(1)
    player.advance(1.5)
    ctrl.fadeout()
    assert ctrl.state()["state"] == "fading_out"
    assert ("fadeout", 2.0) in player.calls  # cue inherits video fade_out
    player.advance(1.0)
    assert ctrl.state()["state"] == "fading_out"
    player.advance(1.1)
    assert ctrl.state()["state"] == "idle"
    ctrl.cue("finale")
    ctrl.fadeout(0.5)
    assert ("fadeout", 0.5) in player.calls
    player.advance(0.6)
    assert ctrl.state()["state"] == "idle"


def test_fadeout_cue_override(ctrl, player):
    ctrl.cue("finale")
    ctrl.fadeout()
    assert ("fadeout", 4.0) in player.calls


def test_fadeout_ignores_pause_points(ctrl, player):
    ctrl.cue(1)
    player.advance(4.0)
    ctrl.fadeout(3.0)
    player.advance(2.0)  # passes pause point 5 while fading
    assert ctrl.state()["state"] == "fading_out"
    player.advance(1.1)
    assert ctrl.state()["state"] == "idle"


def test_fadeout_idle_noop(ctrl, player):
    ctrl.fadeout()
    assert ctrl.state()["state"] == "idle"
    assert not any(c[0] == "fadeout" for c in player.calls)


def test_stop_cuts_to_black(ctrl, player):
    ctrl.cue(2)
    ctrl.stop()
    st = ctrl.state()
    assert st["state"] == "idle" and player.alpha == 0.0
    assert not player.status_visible  # status stays hidden after show commands


def test_end_hold(ctrl, player):
    ctrl.cue(2)
    player.advance(9.0)
    st = ctrl.state()
    assert st["state"] == "holding" and st["position"] == 8.0
    ctrl.pause()
    assert ctrl.state()["state"] == "holding"
    ctrl.go()  # next cue from hold
    assert ctrl.state()["video"] == "loop"


def test_end_loop(ctrl, player):
    ctrl.cue(3)
    assert ctrl.state()["position"] == 2.0  # starts at in_point
    player.advance(4.5)  # 2 -> 6 (out) -> loops to 2
    st = ctrl.state()
    assert st["state"] == "playing"
    assert 2.0 <= st["position"] < 3.0
    assert st["duration"] == 6.0


def test_end_black(ctrl, player):
    ctrl.cue(1)
    ctrl.seek(18.0)
    player.advance(2.5)
    assert ctrl.state()["state"] == "idle"


def test_seek_resets_pause_points(ctrl, player):
    ctrl.cue(1)
    player.advance(1.0)
    ctrl.seek(7.0)
    assert ctrl.state()["position"] == 7.0
    player.advance(5.0)
    st = ctrl.state()
    assert st["pause_reason"] == "pause_point" and st["position"] == 10.0
    with pytest.raises(ProjektorError):
        ctrl.seek(-1)


def test_play_direct(ctrl, player):
    ctrl.play("hold", 0.25)
    st = ctrl.state()
    assert st["video"] == "hold" and st["cue"] is None and st["standby"] == "1"
    assert player.clip.fade_in == 0.25
    with pytest.raises(ProjektorError):
        ctrl.play("nope")
    with pytest.raises(ProjektorError):
        ctrl.play("hold", -1)


def test_status_screen_toggle(ctrl, player):
    ctrl.cue(1)
    assert not player.status_visible
    ctrl.show_status(True)
    st = ctrl.state()
    assert st["status_screen"] is True
    assert not player.status_visible  # only drawn while nothing plays
    ctrl.stop()  # stop is a show command -> hides it again
    assert not player.status_visible and ctrl.state()["status_screen"] is False
    ctrl.show_status(True)
    assert player.status_visible
    ctrl.show_status(False)
    assert not player.status_visible


def test_status_refreshed_by_tick(ctrl, player):
    ctrl.standby(3)
    assert player.status_text == "STATUS standby=3"
    ctrl.tick()
    assert player.status_visible


def test_subscribe(ctrl, player):
    seen = []
    unsub = ctrl.subscribe(seen.append)
    ctrl.cue(1)
    assert seen and seen[-1]["state"] == "fading_in"
    n = len(seen)
    player.advance(1.0)
    assert len(seen) > n and seen[-1]["state"] == "playing"
    unsub()
    unsub()  # idempotent
    n = len(seen)
    ctrl.stop()
    assert len(seen) == n


def test_subscriber_exception_does_not_break(ctrl):
    def bad(_):
        raise RuntimeError("boom")

    ctrl.subscribe(bad)
    ctrl.cue(1)
    assert ctrl.state()["cue"] == "1"


def test_missing_file_reports_error(ctrl, player):
    player.missing.add("intro.mp4")
    ctrl.cue(1)
    st = ctrl.state()
    assert st["state"] == "idle"
    assert "not found" in st["error"]
    ctrl.cue(2)
    assert ctrl.state()["error"] is None


def test_decode_error_goes_idle(ctrl, player):
    ctrl.cue(1)
    player.fail("broken stream")
    st = ctrl.state()
    assert st["state"] == "idle" and st["error"] == "broken stream"


def test_reload_show_keeps_standby_by_id(ctrl, store):
    ctrl.standby("3")
    data = json.loads(store.path.read_text())
    data["cues"].insert(0, {"id": "0", "video": "hold"})
    store.path.write_text(json.dumps(data))
    ctrl.reload_show()
    st = ctrl.state()
    assert st["standby"] == "3" and st["standby_index"] == 3
    store.path.write_text('{"cues": [{"id": "1", "video": "gone"}]}')
    with pytest.raises(ProjektorError):
        ctrl.reload_show()
    assert ctrl.state()["standby"] == "3"  # old show kept


def test_reload_removed_standby_clamps(ctrl, store):
    ctrl.standby("finale")
    data = json.loads(store.path.read_text())
    data["cues"] = data["cues"][:2]
    store.path.write_text(json.dumps(data))
    ctrl.reload_show()
    assert ctrl.state()["standby"] == "2"


def test_record_command_and_transports(ctrl):
    ctrl.record_command("/proj/go", [], "osc")
    lc = ctrl.state()["last_command"]
    assert lc["address"] == "/proj/go" and lc["source"] == "osc" and lc["args"] == []
    assert isinstance(lc["time"], float)
    ctrl.set_transport_status("osc", "listening :9000")
    ctrl.set_transport_status("lora", lambda: "connected /dev/ttyACM0")
    assert ctrl.state()["transports"] == {"osc": "listening :9000", "lora": "connected /dev/ttyACM0"}


def test_empty_show(settings, tmp_path, player):
    from projektor.config import ShowStore

    c = Controller(settings, ShowStore(tmp_path / "empty"), player, SyncExecutor())
    c.start()
    st = c.state()
    assert st["standby"] is None and st["state"] == "idle"
    with pytest.raises(ProjektorError):
        c.go()


def test_invalid_show_file_does_not_crash(settings, tmp_path, player):
    from projektor.config import ShowStore

    st = ShowStore(tmp_path / "bad")
    st.data_dir.mkdir()
    st.path.write_text("{broken")
    c = Controller(settings, st, player)
    assert c.state()["error"]


def test_deferred_executor_validates_synchronously(settings, store, player):
    """Commands only queue work; validation errors raise in the caller's thread."""

    class Queue:
        def __init__(self):
            self.items = []

        def submit(self, fn, *args):
            self.items.append((fn, args))

        def run(self):
            while self.items:
                fn, args = self.items.pop(0)
                fn(*args)

    q = Queue()
    c = Controller(settings, store, player, q)
    c.start()
    c.cue(1)
    assert player.state == "idle" and not player.started  # nothing ran yet
    with pytest.raises(ProjektorError):
        c.cue(42)
    q.run()
    assert player.started and c.state()["cue"] == "1"


# --------------------------------------------------------------------------- preload / fades
def _ctrl(tmp_path, store, player, **player_settings):
    from conftest import make_settings

    c = Controller(make_settings(tmp_path, player=player_settings), store, player, executor=SyncExecutor())
    c.start()
    return c


def test_preload_used_on_go(ctrl, player):
    assert player.prepared is not None and player.prepared.cue_id == "1"
    assert ctrl.state()["prepared"] == "1"
    ctrl.go()
    assert player.promoted
    st = ctrl.state()
    assert st["standby"] == "2" and st["prepared"] == "2"  # next standby preloaded


def test_preload_mismatch_falls_back(ctrl, player):
    ctrl.cue("3")  # standby 1 was prepared
    assert not player.promoted
    assert ctrl.state()["prepared"] == "finale"


def test_standby_change_and_reload_discard(ctrl, player, store):
    import json

    ctrl.standby("2")
    assert ctrl.state()["prepared"] == "2"
    player.calls.clear()
    show = json.loads(store.path.read_text())
    show["cues"] = show["cues"][:1]
    store.path.write_text(json.dumps(show))
    ctrl.reload_show()
    assert ("discard_prepared",) in player.calls
    assert ctrl.state()["prepared"] is None or ctrl.state()["standby"] == ctrl.state()["prepared"]


def test_preload_disabled_never_prepares(tmp_path, store, player):
    c = _ctrl(tmp_path, store, player, preload=False)
    c.go()
    c.standby("3")
    c.stop()
    assert not any(call[0] == "prepare" for call in player.calls)
    assert c.state()["prepared"] is None


def test_fade_value_curves():
    from projektor.player.base import fade_alpha, fade_value

    for curve in ("linear", "scurve", "perceptual"):
        assert fade_value(curve, 0) == 0 and fade_value(curve, 1) == 1
        vals = [fade_value(curve, i / 20) for i in range(21)]
        assert vals == sorted(vals)
    assert fade_value("linear", 0.25) == 0.25
    assert fade_value("scurve", 0.5) == 0.5 and fade_value("scurve", 0.25) < 0.25
    assert fade_value("perceptual", 0.5) < 0.25
    assert fade_alpha("scurve", 0.0, 1.0, 0.0) == 1.0 and fade_alpha("scurve", 1.0, 1.0, 0.0) == 0.0
    assert fade_alpha("linear", 0.0, 0.3, 1.0) == 0.3  # starts from the current alpha


def test_fake_mid_fade_reversal_no_jump(player):
    from projektor.config import Clip

    player.play(Clip("v", "/x/a.mp4", fade_in=2.0, fade_curve="scurve"))
    player.advance(1.0)
    a = player.alpha
    assert 0.0 < a < 1.0
    player.fadeout(2.0)
    player.advance(1 / 30)
    assert abs(player.alpha - a) < 0.1
    player.advance(2.5)
    assert player.state == "idle"
