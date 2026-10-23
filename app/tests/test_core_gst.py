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


def test_preload_promote_start_at_exact(gplayer, clip_path):
    clip = Clip("t", clip_path, start_at=1.0, pause_points=(1.5,), fade_in=0.0, end="hold")
    gplayer.prepare(clip)
    if gplayer.kind in ("fakesink", "kms"):
        assert gplayer.prepared_clip is None  # CPU display: prepare() is a no-op
    else:
        assert gplayer.prepared_clip == clip
        assert run_until(lambda: gplayer._prep.phase == "ready", 10)
    gplayer.play(clip)
    assert gplayer.prepared_clip is None
    assert run_until(lambda: gplayer.state == "paused", 10), gplayer.events
    assert gplayer.pause_reason == "pause_point"
    assert abs(gplayer.position() - 1.5) < 1.0 / FPS + 0.001, gplayer.position()
    assert gplayer.events["errors"] == []


def test_preload_mismatch_and_discard(gplayer, clip_path):
    if gplayer.kind == "fakesink":
        pytest.skip("no preload on the CPU display")
    gplayer.prepare(Clip("t", clip_path, start_at=1.0))
    assert run_until(lambda: gplayer._prep.phase == "ready", 10)
    gplayer.play(Clip("t", clip_path, start_at=0.5, fade_in=0.0, end="hold"))
    assert gplayer.prepared_clip is None
    assert run_until(lambda: gplayer.state == "playing", 5)
    gplayer.prepare(Clip("t", clip_path, start_at=1.0))
    gplayer.discard_prepared()
    assert gplayer.prepared_clip is None and gplayer._prep is None


def test_fade_curve_controller_no_jump(gplayer, clip_path):
    if gplayer.kind == "fakesink":
        pytest.skip("controller fades are GL only")
    gplayer.play(Clip("t", clip_path, fade_in=1.0, end="hold", fade_curve="scurve"))
    assert run_until(lambda: gplayer._fade_ctrl, 5)
    assert run_until(lambda: 0.2 < gplayer._alpha() < 0.8, 3)
    a = gplayer._alpha()
    gplayer.fadeout(1.0)
    assert abs(gplayer._alpha() - a) < 0.15
    assert run_until(lambda: gplayer.state == "idle", 4)


def _exif_orient(jpeg: bytes, orientation: int) -> bytes:
    """Insert a minimal EXIF APP1 segment (IFD0 Orientation) after the JPEG SOI marker."""
    import struct

    tiff = (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
            + struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack("<I", 0))
    app1 = b"Exif\x00\x00" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + jpeg[2:]


@pytest.fixture(scope="module")
def image_paths(tmp_path_factory):
    d = tmp_path_factory.mktemp("images")
    png, raw = d / "white.png", d / "raw.jpg"
    for enc, out, w, h in (("pngenc", png, 640, 480), ("jpegenc", raw, 400, 300)):
        subprocess.run(["gst-launch-1.0", "-q", "videotestsrc", "pattern=white", "num-buffers=1", "!",
                        f"video/x-raw,width={w},height={h}", "!", "videoconvert", "!", enc, "!",
                        "filesink", f"location={out}"], check=True, timeout=30)
    rotated = d / "rotated.jpg"
    rotated.write_bytes(_exif_orient(raw.read_bytes(), 6))  # 6 = rotate 90° clockwise
    return {"png": png, "rotated": rotated}


def _clip_caps(player):
    caps = player._run.sinkpad.get_current_caps()
    st = caps.get_structure(0)
    return st.get_int("width")[1], st.get_int("height")[1]


def test_image_fade_in_hold_fade_out(gplayer, image_paths):
    gplayer.play(Clip("img", image_paths["png"], fade_in=0.4, fade_out=0.4, end="hold", image=True))
    assert gplayer.state == "fading_in"
    assert run_until(lambda: gplayer.state == "holding", 10), gplayer.events
    assert brightest(gplayer) > 200  # white image on screen
    time.sleep(0.3)
    run_until(lambda: False, 0.1)
    assert gplayer.state == "holding" and brightest(gplayer) > 200  # still there
    w, h = _clip_caps(gplayer)
    assert (w, h) == (240, 180)  # 640x480 fitted into the 320x180 display, aspect kept
    gplayer.pause()
    gplayer.seek(1.0)
    assert gplayer.state == "holding"  # pause / seek don't apply to images
    gplayer.fadeout(0.4)
    assert gplayer.state == "fading_out"
    assert run_until(lambda: gplayer.state == "idle", 5)
    assert brightest(gplayer) < 40
    states = [s for s, _ in gplayer.events["states"]]
    assert states == ["fading_in", "holding", "fading_out", "idle"], states
    assert gplayer.events["errors"] == []


def test_image_exif_orientation_and_preload(gplayer, image_paths):
    clip = Clip("rot", image_paths["rotated"], fade_in=0.0, end="hold", image=True)
    gplayer.prepare(clip)
    if gplayer.kind != "fakesink":
        assert run_until(lambda: gplayer._prep.phase == "ready", 10)
    gplayer.play(clip)
    assert run_until(lambda: gplayer.state == "holding", 10), gplayer.events
    w, h = _clip_caps(gplayer)
    assert h == 180 and w < h, (w, h)  # 400x300 rotated to portrait 300x400, fitted to height 180
    assert brightest(gplayer) > 200
    gplayer.stop()
    assert gplayer.state == "idle"
    assert gplayer.events["errors"] == []


def test_v4l2_jpeg_decoder_not_autoplugged():
    # Pi 4: v4l2jpegdec never outputs a single still image (image clips would never preroll)
    import projektor.player.gst as g

    for name in g._NO_AUTOPLUG:
        f = Gst.Registry.get().lookup_feature(name)
        assert f is None or f.get_rank() == Gst.Rank.NONE


def _content_x(player):
    """First and last column with picture content (letterbox / pillarbox bars are black)."""
    f = frame(player)
    w, h = 320, 180
    cols = [x for x in range(w) if any(f[y * w + x] > 40 for y in range(0, h, 3))]
    return cols[0], cols[-1]


def test_letterbox_follows_input_aspect(gplayer, image_paths):
    # glvideomixer kept its first keep-aspect box: a portrait picture after a 4:3 one was stretched
    if gplayer.kind == "fakesink":
        pytest.skip("GL mixer geometry")
    for path, want in ((image_paths["png"], (40, 279)), (image_paths["rotated"], (92, 227)),
                       (image_paths["png"], (40, 279))):
        gplayer.play(Clip("i", path, fade_in=0.0, end="hold", image=True))
        assert run_until(lambda: gplayer.state == "holding", 10)
        lo, hi = _content_x(gplayer)
        assert abs(lo - want[0]) <= 2 and abs(hi - want[1]) <= 2, (path.name, lo, hi)


def _results_player(gplayer, tmp_path):
    from projektor.results import DesignStore

    gplayer.settings.results.in_s = 0.26
    gplayer.settings.results.out_s = 0.16
    gplayer.settings.results.para_gap_s = 0.5
    gplayer.results_design = DesignStore(tmp_path / "rdata")
    return gplayer


def test_results_overlay_needs_gl_display(gplayer, tmp_path):
    if gplayer.kind != "fakesink":
        pytest.skip("GL display supports the overlay")
    from projektor import ProjektorError

    assert not gplayer.supports_results
    with pytest.raises(ProjektorError, match="needs GL display"):
        gplayer.results_show(1, 2)


def test_results_overlay_show_hold_hide(gplayer, tmp_path):
    if gplayer.kind == "fakesink":
        pytest.skip("results overlay is GL only")
    p = _results_player(gplayer, tmp_path)
    assert p.supports_results and brightest(p) < 40  # black before
    p.results_show(41, 17)
    assert p.results_visible
    assert run_until(lambda: p.presenter._phase == "shown" and p.presenter._renderer is not None, 10)
    run_until(lambda: False, 0.3)
    shown = frame(p)
    assert sum(1 for b in shown if b > 40) > len(shown) * 0.02  # white text on the black slide
    # no frames are pushed any more: the mixer keeps showing the last one
    n = p.presenter.frames_pushed
    run_until(lambda: False, 1.0)
    assert p.presenter.frames_pushed == n and frame(p) == shown
    p.results_hide()
    assert run_until(lambda: p.presenter._phase == "hidden", 5)
    run_until(lambda: False, 0.3)
    assert brightest(p) < 40  # released: black again
    # stop cuts at once
    p.results_show(1, 1)
    assert run_until(lambda: p.presenter._phase == "in" or p.presenter._phase == "shown", 5)
    p.results_clear()
    assert run_until(lambda: p.presenter._phase == "hidden", 5)
    run_until(lambda: False, 0.3)
    assert brightest(p) < 40
    assert p.events["errors"] == []


def test_results_overlay_over_video_fades_with_alpha(gplayer, tmp_path, clip_path):
    if gplayer.kind == "fakesink":
        pytest.skip("results overlay is GL only")
    p = _results_player(gplayer, tmp_path)
    p.play(Clip("t", clip_path, fade_in=0.0, end="hold"))
    assert run_until(lambda: p.state == "holding", 10)
    p.results_show(3, 1)
    assert run_until(lambda: p.presenter._phase == "shown", 10)
    pad = p._display._ov_pad
    run_until(lambda: False, 0.2)
    assert pad.get_property("alpha") == 1.0 and pad.get_property("blend-constant-color-alpha") == 1.0
    p.results_hide()
    assert run_until(lambda: p.presenter._phase == "hidden", 5)
    run_until(lambda: False, 0.2)
    assert pad.get_property("alpha") == 0.0
    assert p.state == "holding"  # the video underneath is untouched
