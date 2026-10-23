"""Shared pytest configuration and core fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "gst: needs real GStreamer (headless, fakesink)")


SAMPLE_SHOW = {
    "version": 1,
    "videos": {
        "intro": {"file": "intro.mp4", "title": "Intro", "in_point": 0.0, "out_point": None,
                  "pause_points": [5.0, 10.0], "fade_in": 1.0, "fade_out": 2.0, "end": "black"},
        "hold": {"file": "hold.mp4", "title": "Hold", "pause_points": [], "fade_in": 0.0,
                 "fade_out": 1.0, "end": "hold"},
        "loop": {"file": "loop.mp4", "title": "Loop", "in_point": 2.0, "out_point": 6.0,
                 "fade_in": 0.5, "fade_out": 0.5, "end": "loop"},
    },
    "cues": [
        {"id": "1", "name": "Opening", "video": "intro", "fade_in": None, "fade_out": None,
         "pause_points": None, "end": None, "start_at": None},
        {"id": "2", "name": "Hold", "video": "hold"},
        {"id": "3", "name": "Loop", "video": "loop"},
        {"id": "finale", "name": "Intro again", "video": "intro", "fade_in": 0.0, "fade_out": 4.0,
         "pause_points": [], "end": "hold", "start_at": 12.0},
    ],
}

DURATIONS = {"intro.mp4": 20.0, "hold.mp4": 8.0, "loop.mp4": 10.0}


def make_settings(tmp_path: Path, **overrides):
    from projektor.config import Settings

    data = {
        "device": {"name": "proj-test"},
        "paths": {"data_dir": str(tmp_path), "net_status": str(tmp_path / "run" / "net.json"),
                  "tunnel_status": str(tmp_path / "run" / "tunnel.json")},
        "osc": {"port": 0},
        "lora": {"enabled": False},
        "web": {"port": 8080},
        "results": {"design_url": ""},  # tests never reach the real vote website
    }
    for k, v in overrides.items():
        data.setdefault(k, {}).update(v)
    return Settings.model_validate(data)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def store(settings):
    from projektor.config import ShowStore

    st = ShowStore.from_settings(settings)
    st.media_dir.mkdir(parents=True, exist_ok=True)
    st.path.write_text(json.dumps(SAMPLE_SHOW))
    return st


@pytest.fixture
def player():
    from projektor.player.fake import FakePlayer

    return FakePlayer(durations=DURATIONS)


@pytest.fixture
def ctrl(settings, store, player):
    from projektor.controller import Controller

    c = Controller(settings, store, player, status_builder=lambda st: f"STATUS standby={st['standby']}")
    c.start()
    return c


@pytest.fixture
def disp(ctrl, settings):
    from projektor.commands import CommandDispatcher

    clock = {"t": 1000.0}
    d = CommandDispatcher(ctrl, settings, clock=lambda: clock["t"])
    d.test_clock = clock  # type: ignore[attr-defined]
    return d
