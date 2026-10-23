"""Audience-vote results: numbers, store, design fetch/cache, renderer + animation, commands, cues."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from projektor import ProjektorError
from projektor.config import Show
from projektor.results import (DEFAULT_DIR, DesignRenderer, DesignStore, Presenter, ResultsStore,
                               load_bundle, percentages, substitute, validate_template)

W, H = 480, 270


# ------------------------------------------------------------------ numbers


@pytest.mark.parametrize("ja,nein,want", [
    (0, 0, (0, 0)), (1, 0, (100, 0)), (0, 5, (0, 100)), (1, 2, (33, 67)), (2, 1, (67, 33)),
    (41, 17, (71, 29)), (1, 7, (13, 87)), (7, 1, (88, 12)), (1, 1, (50, 50)), (1, 3, (25, 75)),
])
def test_percentages(ja, nein, want):
    assert percentages(ja, nein) == want
    assert sum(percentages(ja, nein)) in (0, 100)


def test_percentages_sum_always_100():
    for ja in range(0, 40):
        for nein in range(0, 40):
            if ja + nein:
                assert sum(percentages(ja, nein)) == 100


def test_substitute_tokens():
    out = substitute(b"{pct_ja}|{pct_nein}|{count_ja}|{count_nein}|{total}", 41, 17)
    assert out == b"71|29|41|17|58"
    assert substitute(b"{pct_ja} {pct_nein}", 0, 0) == b"0 0"


def test_results_store_persists(tmp_path):
    s = ResultsStore(tmp_path)
    assert s.get() is None
    s.set(3, 4, 9)
    again = ResultsStore(tmp_path).get()
    assert (again["ja"], again["nein"], again["poll_id"]) == (3, 4, 9) and again["time"] > 0
    (tmp_path / "results.json").write_text("garbage")
    assert ResultsStore(tmp_path).get() is None  # unreadable file is ignored


# ------------------------------------------------------------------ design bundle


def test_default_bundle_valid():
    d = load_bundle(DEFAULT_DIR, "default")
    assert (d.width, d.height) == (1920, 1080) and d.version


@pytest.mark.parametrize("svg,msg", [
    (b"<svg", "not valid XML"),
    (b'<html xmlns="x"/>', "not <svg>"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><g id="layer-static"/></svg>', "missing"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><g id="layer-static"/><g id="layer-bars"/>'
     b'<g id="layer-labels"/><rect id="bar-ja"/><rect id="bar-nein"/></svg>', "inside #layer-bars"),
])
def test_validate_template_rejects(svg, msg):
    with pytest.raises(ProjektorError, match=msg):
        validate_template(svg)


class Site:
    """Local HTTP server for the design bundle; counts requests per path."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.hits: dict[str, int] = {}
        site = self

        class H_(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                site.hits[self.path] = site.hits.get(self.path, 0) + 1
                body = site.files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H_)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _bundle_files(version="v1", svg: bytes | None = None, font: bytes | None = None) -> dict[str, bytes]:
    svg = svg if svg is not None else (DEFAULT_DIR / "template.svg").read_bytes()
    font = font if font is not None else (DEFAULT_DIR / "fonts" / "Arimo-Bold.ttf").read_bytes()
    man = {"version": version, "width": 1920, "height": 1080, "template": "design/template.svg",
           "fonts": [{"family": "Arimo", "file": "Arimo-Bold.ttf", "url": "design/fonts/Arimo-Bold.ttf"}]}
    return {"/welle/api/design": json.dumps(man).encode(), "/welle/api/design/template.svg": svg,
            "/welle/api/design/fonts/Arimo-Bold.ttf": font}


@pytest.fixture
def site():
    made = []

    def make(**kw):
        s = Site(_bundle_files(**kw))
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_design_fetch_installs_and_skips_same_version(tmp_path, site):
    s = site()
    store = DesignStore(tmp_path)
    assert store.load().source == "default"
    assert store.fetch(s.base + "/welle/api/design") == ("v1", True)
    d = store.load()
    assert d.source == "cache" and d.version == "v1" and (tmp_path / "design" / "fonts" / "Arimo-Bold.ttf").is_file()
    assert DesignStore(tmp_path).load().version == "v1"  # survives a restart
    n = s.hits["/welle/api/design/template.svg"]
    assert store.fetch(s.base + "/welle/api/design") == ("v1", False)  # unchanged: nothing downloaded
    assert s.hits["/welle/api/design/template.svg"] == n
    s.files.update(_bundle_files(version="v2"))
    assert store.fetch(s.base + "/welle/api/design") == ("v2", True)
    assert store.load().version == "v2"
    assert not list(tmp_path.glob(".design-*")) and not (tmp_path / "design.old").exists()


def test_design_fetch_validation_failure_keeps_cache(tmp_path, site):
    s = site()
    store = DesignStore(tmp_path)
    store.fetch(s.base + "/welle/api/design")
    bad = site(version="v2", svg=b'<svg xmlns="http://www.w3.org/2000/svg"><g id="layer-static"/></svg>')
    with pytest.raises(ProjektorError, match="missing element id"):
        store.fetch(bad.base + "/welle/api/design")
    assert store.load().version == "v1"
    assert DesignStore(tmp_path).load().version == "v1"
    assert store.status()["error"]
    bad_font = site(version="v3", font=b"not a font")
    with pytest.raises(ProjektorError, match="TrueType"):
        store.fetch(bad_font.base + "/welle/api/design")
    assert store.load().version == "v1" and not list(tmp_path.glob(".design-*"))


def test_design_fetch_errors(tmp_path, site):
    store = DesignStore(tmp_path)
    with pytest.raises(ProjektorError, match="no design url"):
        store.fetch(None)
    with pytest.raises(ProjektorError, match="http"):
        store.fetch("ftp://x/y")
    s = site()
    with pytest.raises(ProjektorError, match="404"):
        store.fetch(s.base + "/nope")
    s.files["/welle/api/design"] = b"{not json"
    with pytest.raises(ProjektorError, match="manifest"):
        store.fetch(s.base + "/welle/api/design")
    assert store.load().source == "default"


def test_broken_cache_falls_back_to_default(tmp_path):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "manifest.json").write_text("{}")
    assert DesignStore(tmp_path).load().source == "default"


# ------------------------------------------------------------------ renderer + animation


class FakeSink:
    def __init__(self):
        self.t0 = time.monotonic()
        self.frames: list[bytes] = []
        self.ramps: list = []
        self.alpha = None
        self.released = 0

    def now_ns(self):
        return int((time.monotonic() - self.t0) * 1e9)

    def push(self, frame, pts_ns):
        self.frames.append(bytes(frame))

    def ramp_alpha(self, points):
        self.ramps.append(points)

    def set_alpha(self, a):
        self.alpha = a

    def release(self):
        self.released += 1


def px(frame: bytes, x: int, y: int) -> tuple:
    i = (y * W + x) * 4
    return tuple(frame[i:i + 4])


@pytest.fixture
def presenter(tmp_path):
    p = Presenter(DesignStore(tmp_path), W, H, 30, in_s=0.26, out_s=0.16, para_gap_s=0.5)  # 5x faster
    sink = FakeSink()
    p.attach(sink)
    yield p, sink
    p.close()


BAR_TEMPLATE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">'
    b'<g id="layer-static"><rect width="1920" height="1080" fill="#000"/></g>'
    b'<g id="layer-bars"><rect id="bar-ja" x="380" y="250" width="540" height="520" fill="#fff"/>'
    b'<rect id="bar-nein" x="1000" y="250" width="540" height="520" fill="#888"/></g>'
    b'<g id="layer-labels"><text x="960" y="900" font-size="100" fill="#fff">{pct_ja} %</text></g></svg>')


def _with_template(tmp_path, svg: bytes):
    import dataclasses

    return dataclasses.replace(DesignStore(tmp_path).load(), template=svg)


def test_default_template_has_paragraphs_and_no_bars():
    svg = (DEFAULT_DIR / "template.svg").read_bytes().decode()
    assert all(f'id="para-{n}"' in svg for n in (1, 2, 3)) and "layer-bars" not in svg


def test_renderer_layers_one_per_paragraph(tmp_path):
    r = DesignRenderer(DesignStore(tmp_path).load(), W, H)
    assert r.static is not None and r.bar_ja is None and r.bar_nein is None
    lab = r.labels(41, 17)
    assert len(lab.layers) == 3 and all(ly is not None for ly in lab.layers)
    ys = [ly.y for ly in lab.layers]
    assert ys == sorted(ys) and len(set(ys)) == 3  # paragraphs stack downwards, centred block
    assert r.labels(1, 2).layers[0] is not None


def test_renderer_fallback_without_render_layer(tmp_path):
    d = DesignStore(tmp_path).load()
    fb = DesignRenderer(d, W, H, use_layers=False)
    assert fb.static is not None and len(fb.labels(1, 2).layers) == 3
    fb = DesignRenderer(_with_template(tmp_path, BAR_TEMPLATE), W, H, use_layers=False)
    assert fb.bar_ja.layer is not None and fb.bar_ja.x == pytest.approx(380 / 4, abs=1)


def test_old_bar_template_still_works(tmp_path):
    validate_template(BAR_TEMPLATE)
    d = _with_template(tmp_path, BAR_TEMPLATE)
    r = DesignRenderer(d, W, H)
    assert r.bar_ja.layer is not None and r.bar_nein.layer is not None
    assert r.bar_ja.w == pytest.approx(540 / 4, abs=1) and r.bar_ja.x == pytest.approx(380 / 4, abs=1)
    assert len(r.labels(41, 17).layers) == 1  # no para-N: the labels layer is one paragraph


def test_no_para_labels_layer_is_one_paragraph(tmp_path):
    svg = (DEFAULT_DIR / "template.svg").read_bytes().decode()
    flat = svg.replace('<g id="para-1">', "<g>").replace('<g id="para-2">', "<g>").replace('<g id="para-3">', "<g>")
    r = DesignRenderer(_with_template(tmp_path, flat.encode()), W, H)
    assert len(r.labels(1, 2).layers) == 1


def _wait_tweens(p, name, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and name not in p._tw:
        time.sleep(0.01)
    assert name in p._tw


def test_sequential_paragraph_reveal(tmp_path):
    p = Presenter(DesignStore(tmp_path), W, H, 30)  # nominal timing: 0.5 s overlay, 0.8 s fades, 2.5 s gap
    p.attach(FakeSink())
    try:
        p.show(41, 17)
        _wait_tweens(p, "new2")
        base = p._tw["alpha"].t0
        v = lambda n, dt: p._tw[n].value(base + dt)  # noqa: E731
        assert v("alpha", 0.5) == pytest.approx(1.0) and v("alpha", 0.25) == pytest.approx(0.5)
        assert v("new0", 0.5) == 0.0 and v("new0", 0.9) == pytest.approx(0.5) and v("new0", 1.3) == pytest.approx(1.0)
        assert v("new1", 2.9) == 0.0 and v("new1", 3.4) == pytest.approx(0.5) and v("new1", 3.8) == pytest.approx(1.0)
        assert v("new2", 5.4) == 0.0 and v("new2", 5.9) == pytest.approx(0.5)
        assert v("new2", 6.3) == pytest.approx(1.0) and p._tw["new2"].end == pytest.approx(base + 6.3)
        assert v("new1", 1.3) == 0.0  # not yet revealed while paragraph 1 is complete
    finally:
        p.close()


def test_hide_mid_reveal_fades_current_state_out(tmp_path):
    p = Presenter(DesignStore(tmp_path), W, H, 30, para_gap_s=0.6)
    p.attach(FakeSink())
    try:
        p.show(3, 4)
        _wait_tweens(p, "new2")
        time.sleep(1.5)  # overlay up, paragraph 1 visible, paragraph 2 part way
        p.hide()
        end = time.monotonic() + 3
        while time.monotonic() < end and p._tw["alpha"].v1 != 0.0:
            time.sleep(0.01)
        t = p._tw["new0"].t0
        assert p._tw["new0"].v1 == 0.0 and p._tw["new0"].v0 > 0.9  # starts from the current state
        assert p._tw["new2"].v0 == 0.0 and p._tw["new2"].v1 == 0.0  # unrevealed stays hidden
        assert p._tw["new0"].dur == pytest.approx(0.6)
        assert p._tw["alpha"].t0 == pytest.approx(t + 0.4) and p._tw["alpha"].dur == pytest.approx(0.4)
    finally:
        p.close()


def test_update_crossfades_visible_paragraphs(presenter):
    p, sink = presenter
    p.show(10, 0)
    assert p.wait_idle(10)
    p.show(0, 10)
    end = time.monotonic() + 3
    while time.monotonic() < end and p._tw["old0"].v0 != 1.0:
        time.sleep(0.005)
    assert p._tw["old0"].v1 == 0.0 and p._tw["new0"].v1 == 1.0 and p._tw["new0"].v0 == 0.0
    assert p.wait_idle(10) and sink.released == 0


def test_animation_frames_show_text_progressively(presenter):
    p, sink = presenter
    p.show(41, 17)
    assert p.visible and p.wait_idle(15)
    assert sink.frames and sink.alpha == 1.0
    assert sink.ramps[0][-1][1] == 1.0  # alpha ramp towards 1
    first, last = sink.frames[0], sink.frames[-1]
    assert len(set(sink.frames)) > 3 and last != first
    r = p._renderer
    lab = r.labels(41, 17)

    def lit(frame, i):  # any bright pixel in the rows of paragraph i
        ly = lab.layers[i]
        return any(c > 128 for c in frame[ly.y * W * 4 + 1:(ly.y + ly.surface.get_height()) * W * 4:4])

    assert not any(lit(first, i) for i in range(3))  # first frame: black, no text yet
    assert all(lit(last, i) for i in range(3))  # last frame: all paragraphs
    # paragraph 1 shows up while paragraph 3 is still missing
    assert any(lit(f, 0) and not lit(f, 2) for f in sink.frames)


def test_hide_animates_out_and_releases(presenter):
    p, sink = presenter
    p.show(5, 5)
    assert p.wait_idle(10)
    n = len(sink.frames)
    p.hide()
    assert not p.visible
    assert p.wait_idle(10)
    assert len(sink.frames) > n and sink.released == 1 and sink.alpha == 0.0
    last_ramp = sink.ramps[-1]
    assert last_ramp[-1][1] == 0.0  # alpha ramps to 0, after the text faded
    assert last_ramp[-1][0] > last_ramp[0][0]


def test_update_while_shown_animates_to_new_values(presenter):
    p, sink = presenter
    p.show(10, 0)
    assert p.wait_idle(10)
    a = sink.frames[-1]
    p.show(0, 10)
    assert p.wait_idle(10)
    assert sink.frames[-1] != a and sink.released == 0 and p.visible


def test_clear_is_immediate(presenter):
    p, sink = presenter
    p.show(1, 1)
    p.clear()
    assert not p.visible
    assert p.wait_idle(10)
    assert sink.released >= 1 and sink.alpha == 0.0
    p.hide()  # nothing shown: no-op
    p.clear()


def test_zero_votes_render_zero_percent(presenter):
    p, sink = presenter
    p.show(0, 0)
    assert p.wait_idle(10)
    r = p._renderer
    assert r.labels(0, 0).key == (0, 0)


def test_compose_cost_1080p(tmp_path):
    p = Presenter(DesignStore(tmp_path), 1920, 1080, 30, in_s=0.26, out_s=0.16, para_gap_s=0.5)
    sink = FakeSink()
    p.attach(sink)
    p.show(41, 17)
    assert p.wait_idle(15)
    assert p.last_compose_ms < 100
    p.close()


# ------------------------------------------------------------------ commands + results cue


def _results_show(tmp_path):
    return {"version": 1, "videos": {"hold": {"file": "hold.mp4", "fade_in": 0.0, "fade_out": 1.0, "end": "hold"}},
            "cues": [{"id": "1", "name": "Video", "video": "hold"},
                     {"id": "2", "name": "Ergebnis", "type": "results"},
                     {"id": "3", "name": "Video 2", "video": "hold"}]}


@pytest.fixture
def rctrl(settings, store, player):
    from projektor.controller import Controller

    store.path.write_text(json.dumps(_results_show(None)))
    c = Controller(settings, store, player, status_builder=lambda st: "STATUS")
    c.start()
    return c


@pytest.fixture
def rdisp(rctrl, settings):
    from projektor.commands import CommandDispatcher

    return CommandDispatcher(rctrl, settings)


def test_show_model_results_cue():
    s = Show.model_validate({"cues": [{"id": 12, "name": "x", "type": "results", "fade_in": None}]})
    assert s.cues[0].type == "results" and s.cues[0].video == ""
    assert Show.model_validate({"videos": {"a": {"file": "a.mp4"}}, "cues": [{"id": "1", "video": "a"}]}).cues[0].type == "video"
    with pytest.raises(ValueError, match="needs a video"):
        Show.model_validate({"cues": [{"id": "1"}]})
    with pytest.raises(ValueError):
        Show.model_validate({"cues": [{"id": "1", "type": "bogus"}]})


def test_results_set_command(rdisp, rctrl, settings):
    assert rdisp.handle("/proj/results/set", [41, 17, 3], "osc") == [("/proj/results/ok", [41, 17, 3])]
    assert rdisp.handle("/proj/results/set", ["5", 6.0], "lora") == [("/proj/results/ok", [5, 6, 0])]
    assert rctrl.state()["results"]["stored"]["ja"] == 5
    assert ResultsStore(settings.paths.data_dir).get()["nein"] == 6  # persisted
    for bad in ([], [1], [1, 2, 3, 4], [-1, 2], ["x", 2], [1.5, 2]):
        r = rdisp.handle("/proj/results/set", bad, "osc")
        assert r and r[0][0] == "/proj/error", bad


def test_results_cue_flow(rdisp, rctrl, player):
    rdisp.handle("/proj/results/set", [41, 17, 3], "osc")
    rdisp.handle("/proj/cue", [1], "osc")
    player.advance(1.0)
    rdisp.handle("/proj/cue", [2], "osc")
    assert ("results_show", 41, 17) in player.calls
    assert player.results == (41, 17)
    st = rctrl.state()
    assert st["cue"] == "2" and st["standby"] == "3" and st["results"]["visible"] and st["state"] in ("holding", "fading_out")
    player.advance(1.5)
    assert rctrl.state()["state"] == "holding" and rctrl.state()["video"] is None  # holds like an image
    n = player.calls.count(("results_show", 41, 17))
    rdisp.handle("/proj/cue", [2], "osc")  # idempotent
    assert player.calls.count(("results_show", 41, 17)) == n
    rdisp.handle("/proj/cue", [3], "osc")  # next cue: graph animates out, video starts
    assert player.results is None and ("results_hide", None) in player.calls
    assert player.state in ("playing", "fading_in", "holding")


def test_results_cue_fadeout_and_stop(rdisp, rctrl, player):
    rdisp.handle("/proj/cue", [2], "osc")  # nothing stored: 0 / 0
    assert player.results == (0, 0)
    rdisp.handle("/proj/fadeout", [], "osc")
    assert player.results is None and ("results_hide", None) in player.calls
    rdisp.handle("/proj/cue", [2], "osc")
    rdisp.handle("/proj/stop", [], "osc")
    assert player.results is None and ("results_clear",) in player.calls


def test_results_cue_go_hides_boot_status_and_no_preload(rdisp, rctrl, player):
    rctrl.standby(2)
    assert player.prepared is None  # results cue has no clip
    assert rctrl.state()["status_screen"] is True
    rdisp.handle("/proj/go", [], "osc")
    assert rctrl.state()["status_screen"] is False and player.results is not None
    assert not player.status_visible


def test_results_cue_needs_gl(rdisp, rctrl, player):
    player.results_supported = False

    def boom(ja, nein):
        raise ProjektorError("results overlay needs GL display")

    player.results_show = boom
    rdisp.handle("/proj/cue", [2], "osc")
    assert rctrl.state()["error"] == "results overlay needs GL display"


def test_design_fetch_command(rdisp, rctrl, player, site, settings):
    s = site()
    got = []
    done = threading.Event()

    def reply(a, r):
        got.append((a, r))
        done.set()

    assert rdisp.handle("/proj/design/fetch", [s.base + "/welle/api/design"], "osc", reply=reply) == []
    assert done.wait(10) and got == [("/proj/design/ok", ["v1"])]
    assert rctrl.state()["design"]["version"] == "v1" and ("reload_results_design",) in player.calls
    done.clear()
    got.clear()
    rdisp.handle("/proj/design/fetch", [s.base + "/missing"], "lora", reply=reply)
    assert done.wait(10) and got[0][0] == "/proj/error"
    # synchronous errors
    assert rdisp.handle("/proj/design/fetch", [], "web")[0] == ("/proj/error", ["no design url (set [results] design_url or pass a url)"])
    assert rdisp.handle("/proj/design/fetch", ["ftp://x"], "web")[0][0] == "/proj/error"
    assert rdisp.handle("/proj/design/fetch", [1, 2], "web")[0][0] == "/proj/error"


def test_design_fetch_uses_configured_url(tmp_path, site):
    from conftest import make_settings
    from projektor.commands import CommandDispatcher
    from projektor.config import ShowStore
    from projektor.controller import Controller
    from projektor.player.fake import FakePlayer

    s = site()
    st = make_settings(tmp_path, results={"design_url": s.base + "/welle/api/design"})
    store = ShowStore.from_settings(st)
    c = Controller(st, store, FakePlayer())
    d = CommandDispatcher(c, st)
    done = threading.Event()
    out = []
    d.handle("/proj/design/fetch", [], "osc", reply=lambda a, r: (out.append(a), done.set()))
    assert done.wait(10) and out == ["/proj/design/ok"]


def test_results_settings_defaults(settings):
    assert (settings.results.design_url, settings.results.in_s, settings.results.out_s) == ("", 1.3, 0.8)
