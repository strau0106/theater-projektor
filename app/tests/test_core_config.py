from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import SAMPLE_SHOW
from projektor import ProjektorError
from projektor.config import (
    DEV_CONFIG,
    Settings,
    Show,
    ShowStore,
    load_settings,
    parse_show,
    resolve_cue,
    resolve_cue_ref,
    resolve_video,
)

ARCH_TOML = """
[device]
name = "projektor"

[paths]
data_dir = "/var/lib/projektor"
net_status = "/run/projektor/net.json"
tunnel_status = "/run/projektor/tunnel.json"

[display]
sink = "auto"
status_on_boot = true
width = 1920
height = 1080

[osc]
port = 9000
reply_host = ""
reply_port = 0
dedup_window_s = 1.0

[lora]
enabled = true
device = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_*"
baud = 115200

[web]
port = 80
password_hash = ""
max_upload_mb = 8000

[backup]
enabled = true
host = "INFMK1_HOST"
port = 22
user = "projbackup"
key = "/var/lib/projektor/.ssh/id_backup"
remote_dir = ""
"""


def test_settings_defaults_match_architecture_example():
    parsed = Settings.from_toml(ARCH_TOML)
    default = Settings()
    assert parsed.model_dump() == default.model_dump()
    assert default.osc.port == 9000
    assert default.display.sink == "auto"
    assert default.web.port == 80
    assert default.show_path == Path("/var/lib/projektor/show.json")
    assert default.media_dir == Path("/var/lib/projektor/media")


def test_settings_partial_and_unknown_keys(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[device]\nname = "x"\n[paths]\ndata_dir = "data"\n[wifi]\nhotspot_after_attempts = 8\n')
    s = Settings.load(p)
    assert s.device.name == "x"
    assert s.paths.data_dir == (tmp_path / "data").resolve()  # relative to the TOML file
    assert s.osc.dedup_window_s == 1.0
    assert s.model_extra["wifi"]["hotspot_after_attempts"] == 8
    assert s.config_path == p


def test_settings_invalid(tmp_path):
    with pytest.raises(ProjektorError):
        Settings.from_toml("[display]\nsink = 'hdmi'\n")
    with pytest.raises(ProjektorError):
        Settings.from_toml("this is not toml = = =")
    with pytest.raises(ProjektorError):
        Settings.load(tmp_path / "missing.toml")


def test_dev_settings_file():
    s = load_settings(dev=True)
    assert s.config_path == DEV_CONFIG
    assert s.dev
    assert s.display.sink == "window"
    assert s.web.port == 8080
    assert s.osc.port == 9000
    assert not s.lora.enabled
    app_dir = Path(__file__).resolve().parents[1]
    assert s.paths.data_dir == (app_dir / "devdata").resolve()
    assert s.paths.net_status == (app_dir / "devdata" / "run" / "net.json").resolve()
    assert s.paths.tunnel_status.parent == s.paths.net_status.parent


def test_show_parse_sample():
    show = parse_show(SAMPLE_SHOW)
    assert list(show.videos) == ["intro", "hold", "loop"]
    assert [c.id for c in show.cues] == ["1", "2", "3", "finale"]
    assert show.cues[1].fade_in is None


def test_show_defaults_and_sorting():
    show = parse_show({"videos": {"v": {"file": "v.mp4", "pause_points": [3, 1, 2, 2]}},
                       "cues": [{"id": 1, "video": "v"}]})
    assert show.version == 1
    assert show.videos["v"].pause_points == [1.0, 2.0, 3.0]
    assert show.videos["v"].end == "black"
    assert show.cues[0].id == "1"


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d["cues"].append({"id": "x", "video": "nope"}), "unknown video"),
        (lambda d: d["cues"].append({"id": "1", "video": "intro"}), "duplicate cue id"),
        (lambda d: d["videos"]["intro"].update(out_point=0.0), "out_point"),
        (lambda d: d["videos"]["intro"].update(pause_points=[-1.0]), "negative"),
        (lambda d: d["videos"]["loop"].update(pause_points=[7.0]), "after out_point"),
        (lambda d: d["videos"]["intro"].update(end="rewind"), "end"),
        (lambda d: d["videos"]["intro"].update(file="/etc/passwd"), "relative"),
        (lambda d: d["videos"]["intro"].update(file="../x.mp4"), "relative"),
        (lambda d: d["videos"]["intro"].update(fade_in=-1), "fade"),
        (lambda d: d["cues"][2].update(start_at=6.5), "start_at"),
        (lambda d: d["cues"][2].update(pause_points=[9.0]), "pause point"),
        (lambda d: d["cues"].append({"id": "  ", "video": "intro"}), "empty"),
    ],
)
def test_show_validation_errors(mutate, msg):
    data = json.loads(json.dumps(SAMPLE_SHOW))
    mutate(data)
    with pytest.raises(ProjektorError, match=msg):
        parse_show(data)


def test_show_invalid_json():
    with pytest.raises(ProjektorError, match="JSON"):
        parse_show("{nope")


def test_showstore_missing_file_is_empty(tmp_path):
    st = ShowStore(tmp_path / "data")
    show = st.load()
    assert show.videos == {} and show.cues == []
    assert st.media_dir == tmp_path / "data" / "media"


def test_showstore_save_atomic_roundtrip(tmp_path):
    st = ShowStore(tmp_path)
    show = parse_show(SAMPLE_SHOW)
    st.save(show)
    assert st.load() == show
    assert sorted(p.name for p in tmp_path.iterdir()) == ["show.json"]  # no temp files left
    # invalid show (mutated after construction) is rejected and the old file stays intact
    before = st.path.read_bytes()
    show.cues.append(show.cues[0])
    with pytest.raises(ProjektorError, match="duplicate"):
        st.save(show)
    assert st.path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["show.json"]


def test_showstore_save_uses_replace(tmp_path, monkeypatch):
    st = ShowStore(tmp_path)
    st.save(parse_show(SAMPLE_SHOW))
    calls = []
    real_replace = os.replace
    real_fsync = os.fsync
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(("replace", Path(a).name)), real_replace(a, b)))
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(("fsync",)), real_fsync(fd)))
    st.save(Show())
    assert calls[0] == ("fsync",)  # file data synced before the rename
    assert calls[1][0] == "replace" and calls[1][1].startswith(".show-")
    assert ("fsync",) in calls[2:]  # directory synced after
    assert st.load() == Show()


def test_showstore_load_invalid(tmp_path):
    st = ShowStore(tmp_path)
    st.path.write_text('{"videos": {}, "cues": [{"id": "1", "video": "x"}]}')
    with pytest.raises(ProjektorError):
        st.load()


def test_resolve_cue_ref():
    show = parse_show(SAMPLE_SHOW)
    assert resolve_cue_ref(show, 1) == 0
    assert resolve_cue_ref(show, 4) == 3
    assert resolve_cue_ref(show, "finale") == 3
    assert resolve_cue_ref(show, "2") == 1
    assert resolve_cue_ref(show, 3.0) == 2
    for bad in (0, 5, "nope", 1.5, True, None):
        with pytest.raises(ProjektorError):
            resolve_cue_ref(show, bad)


def test_resolve_ref_prefers_id_over_number():
    show = parse_show({"videos": {"v": {"file": "v.mp4"}},
                       "cues": [{"id": "2", "video": "v"}, {"id": "1", "video": "v"}]})
    assert resolve_cue_ref(show, "1") == 1  # string -> id
    assert resolve_cue_ref(show, 1) == 0  # int -> position


def test_resolve_cue_inheritance(tmp_path):
    show = parse_show(SAMPLE_SHOW)
    c = resolve_cue(show, 0, tmp_path)
    assert c.path == tmp_path / "intro.mp4"
    assert (c.fade_in, c.fade_out, c.end, c.start_at) == (1.0, 2.0, "black", 0.0)
    assert c.pause_points == (5.0, 10.0)
    assert (c.cue_id, c.cue_index) == ("1", 0)
    f = resolve_cue(show, 3, tmp_path)
    assert (f.fade_in, f.fade_out, f.end, f.start_at, f.pause_points) == (0.0, 4.0, "hold", 12.0, ())
    lp = resolve_cue(show, 2, tmp_path)
    assert (lp.in_point, lp.out_point, lp.start_at) == (2.0, 6.0, 2.0)


def test_resolve_cue_start_at_drops_earlier_pause_points(tmp_path):
    data = json.loads(json.dumps(SAMPLE_SHOW))
    data["cues"][0]["start_at"] = 7.0
    c = resolve_cue(parse_show(data), 0, tmp_path)
    assert c.pause_points == (10.0,)


def test_resolve_video(tmp_path):
    show = parse_show(SAMPLE_SHOW)
    v = resolve_video(show, "hold", tmp_path, fade_in=3.0)
    assert v.fade_in == 3.0 and v.end == "hold" and v.cue_id is None
    with pytest.raises(ProjektorError):
        resolve_video(show, "nope", tmp_path)


def test_preload_auto_depends_on_ram(monkeypatch):
    from projektor import config as cfg

    ps = cfg.PlayerSettings()
    assert ps.preload == "auto"
    monkeypatch.setattr(cfg, "_mem_total_kb", lambda: 900_000)  # Pi 4 1 GB
    assert ps.preload_enabled() is False
    monkeypatch.setattr(cfg, "_mem_total_kb", lambda: 1_900_000)  # Pi 4 2 GB
    assert ps.preload_enabled() is True
    assert cfg.PlayerSettings(preload=False).preload_enabled() is False
    assert cfg.PlayerSettings(preload=True).preload_enabled() is True
