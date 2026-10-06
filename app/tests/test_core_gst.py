"""Real GstPlayer, headless: CPU display pipeline (fakesink) and, if GL is available
(EGL surfaceless / any display), the GL pipeline with the zero-copy appsrc handoff."""

from __future__ import annotations

import subprocess
import time

import pytest

pytestmark = pytest.mark.gst

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GLib, Gst, GstVideo

    Gst.init(None)
except (ValueError, ImportError):  # pragma: no cover
    pytest.skip("GStreamer not available", allow_module_level=True)

NEEDED = ["videotestsrc", "x264enc", "h264parse", "mp4mux", "playbin3", "intervideosink",
          "intervideosrc", "textoverlay", "videobalance", "fakesink"]
_missing = [e for e in NEEDED if Gst.ElementFactory.find(e) is None]
if _missing or not any(Gst.ElementFactory.find(d) for d in ("avdec_h264", "openh264dec", "vah264dec")):
    pytest.skip(f"GStreamer elements missing: {_missing or 'h264 decoder'}", allow_module_level=True)

from conftest import make_settings  # noqa: E402
from projektor.config import Clip  # noqa: E402
from projektor.player.gst import GstPlayer  # noqa: E402

FPS = 30
SECONDS = 2.5


@pytest.fixture(scope="module")
def clip_path(tmp_path_factory):
    out = tmp_path_factory.mktemp("media") / "test.mp4"
    cmd = [
        "gst-launch-1.0", "-q", "videotestsrc", f"num-buffers={int(SECONDS * FPS)}", "!",
        f"video/x-raw,width=320,height=240,framerate={FPS}/1", "!", "timeoverlay", "!",
        "x264enc", "speed-preset=ultrafast", "key-int-max=15", "!", "h264parse", "!",
        "mp4mux", "!", "filesink", f"location={out}",
    ]
    subprocess.run(cmd, check=True, timeout=60)
    return out


def run_until(cond, timeout=10.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.005)
    return False


def frame(player) -> bytes:
    """Luma of the frame currently on the (headless) display."""
    run_until(lambda: False, 0.15)  # let the display render a few frames
    sample = player._display.sink.get_property("last-sample")
    gray = GstVideo.video_convert_sample(sample, Gst.Caps.from_string("video/x-raw,format=GRAY8"), Gst.SECOND)
    buf = gray.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    try:
        return bytes(info.data)
    finally:
        buf.unmap(info)


def brightest(player) -> int:
    return max(frame(player))


GL_ELEMENTS = ["glvideomixer", "gloverlaycompositor", "fakevideosink", "appsrc", "gldownload"]


@pytest.fixture(params=["fakesink", "glfakesink"])
def gplayer(request, tmp_path):
    if request.param == "glfakesink" and any(Gst.ElementFactory.find(e) is None for e in GL_ELEMENTS):
        pytest.skip("GL elements missing")
    s = make_settings(tmp_path, display={"width": 320, "height": 180})
    p = GstPlayer(s, sink=request.param)
    events = {"states": [], "errors": []}
    p.set_callbacks(lambda st, r: events["states"].append((st, r)), events["errors"].append)
    p.events = events  # type: ignore[attr-defined]
    try:
        p.start()
    except RuntimeError as e:
        pytest.skip(f"display {request.param} not available: {e}")
    yield p
    p.shutdown()
    run_until(lambda: False, 0.1)


def test_pause_point_resume_eos(gplayer, clip_path):
    gplayer.set_status(True, "status")
    gplayer.play(Clip("t", clip_path, pause_points=(1.0,), fade_in=0.2, end="black"))
    assert gplayer.state == "fading_in"
    assert run_until(lambda: gplayer.state == "paused", 10), gplayer.events
    assert gplayer.pause_reason == "pause_point"
    pos = gplayer.position()
    assert abs(pos - 1.0) < 1.0 / FPS + 0.001, pos
    assert abs(gplayer.duration() - SECONDS) < 0.1
    time.sleep(0.3)
    run_until(lambda: False, 0.1)
    assert gplayer.state == "paused" and abs(gplayer.position() - pos) < 0.001  # frozen
    assert gplayer.snapshot_png()[:8] == b"\x89PNG\r\n\x1a\n"
    assert brightest(gplayer) > 150  # the paused frame is on screen
    gplayer.resume()
    assert gplayer.state == "playing"
    assert run_until(lambda: gplayer.state == "idle", 10)  # end=black
    assert brightest(gplayer) > 150  # idle again: status screen text
    states = [s for s, _ in gplayer.events["states"]]
    assert states == ["fading_in", "playing", "paused", "playing", "idle"], states
    assert gplayer.events["errors"] == []


def test_hold_seek_and_fadeout(gplayer, clip_path):
    gplayer.play(Clip("t", clip_path, start_at=1.5, fade_in=0.0, end="hold"))
    assert run_until(lambda: gplayer.state == "holding", 10), gplayer.events
    assert gplayer.position() > 2.3
    time.sleep(0.3)
    assert brightest(gplayer) > 150  # last frame held
    gplayer.seek(0.5)
    assert gplayer.state == "playing"
    assert run_until(lambda: (gplayer.position() or 0) > 0.8, 5)
    gplayer.fadeout(0.3)
    assert gplayer.state == "fading_out"
    assert run_until(lambda: gplayer.state == "idle", 5)
    assert brightest(gplayer) < 40


def test_loop_and_manual_pause(gplayer, clip_path):
    gplayer.play(Clip("t", clip_path, in_point=0.5, out_point=1.0, start_at=0.5, fade_in=0.0, end="loop"))
    run_until(lambda: False, 1.6)  # > 2 loop iterations of 0.5 s
    assert gplayer.state == "playing"
    assert 0.5 <= gplayer.position() <= 1.05
    assert frame(gplayer) != frame(gplayer)  # moving picture (timeoverlay)
    gplayer.pause()
    assert gplayer.state == "paused" and gplayer.pause_reason == "manual"
    a = frame(gplayer)
    assert a == frame(gplayer) and max(a) > 150  # frozen frame stays on screen
    gplayer.stop()
    assert gplayer.state == "idle"


def test_missing_file(gplayer, tmp_path):
    gplayer.play(Clip("x", tmp_path / "nope.mp4"))
    assert gplayer.state == "idle"
    assert gplayer.events["errors"] and "not found" in gplayer.events["errors"][0]


def test_broken_file(gplayer, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video" * 100)
    gplayer.play(Clip("x", bad, fade_in=0.0))
    assert run_until(lambda: gplayer.events["errors"], 10)
    assert gplayer.state == "idle"
