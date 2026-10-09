"""Still images as cues: show model, controller + fake player, web API / probe, projektor-prepare."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from projektor.config import Show, ShowStore, is_image, resolve_cue, resolve_video

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

IMAGE_SHOW = {
    "version": 1,
    "videos": {
        "intro": {"file": "intro.mp4", "title": "Intro", "fade_in": 0.0, "fade_out": 1.0, "end": "hold"},
        # timeline fields on an image are ignored (end is always hold)
        "poster": {"file": "poster.JPG", "title": "Poster", "fade_in": 2.0, "fade_out": 3.0,
                   "end": "black", "in_point": 4.0, "out_point": 5.0, "pause_points": [4.5]},
        "logo": {"file": "logo.png", "title": "Logo", "fade_in": 0.0, "fade_out": 0.0},
    },
    "cues": [
        {"id": "1", "name": "Poster", "video": "poster"},
        {"id": "2", "name": "Poster slow", "video": "poster", "fade_in": 4.0, "fade_out": 0.5,
         "start_at": 9.0, "pause_points": [1.0], "end": "loop"},
        {"id": "3", "name": "Film", "video": "intro"},
        {"id": "4", "name": "Logo", "video": "logo"},
    ],
}


def _exif_orient(jpeg: bytes, orientation: int) -> bytes:
    """Insert a minimal EXIF APP1 segment (IFD0 Orientation) after the JPEG SOI marker."""
    tiff = (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
            + struct.pack("<HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack("<I", 0))
    app1 = b"Exif\x00\x00" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + jpeg[2:]


def _ffmpeg_image(path: Path, size: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}",
                    "-frames:v", "1", "-update", "1", str(path)], check=True, timeout=60)


# ------------------------------------------------------------------ show model


def test_is_image():
    assert is_image("a.jpg") and is_image("dir/b.JPEG") and is_image("c.png") and is_image(Path("d.webp"))
    assert not is_image("a.mp4") and not is_image("jpg") and not is_image("a.jpg.mov")


def test_image_clip_ignores_timeline_fields(tmp_path):
    show = Show.model_validate(IMAGE_SHOW)
    assert show.videos["poster"].image and not show.videos["intro"].image
    c = resolve_cue(show, 0, tmp_path)
    assert c.image and c.end == "hold"
    assert (c.in_point, c.out_point, c.start_at, c.pause_points) == (0.0, None, 0.0, ())
    assert (c.fade_in, c.fade_out) == (2.0, 3.0)
    # cue overrides: fades apply, start_at / pause points / end don't
    c2 = resolve_cue(show, 1, tmp_path)
    assert (c2.fade_in, c2.fade_out) == (4.0, 0.5)
    assert (c2.start_at, c2.pause_points, c2.end) == (0.0, (), "hold")
    d = resolve_video(show, "poster", tmp_path, fade_in=0.25)
    assert d.image and d.fade_in == 0.25 and d.fade_out == 3.0
    assert not resolve_cue(show, 2, tmp_path).image


def test_image_cue_start_at_beyond_out_point_is_valid():
    # for a video this is rejected; for an image both values are ignored
    data = json.loads(json.dumps(IMAGE_SHOW))
    Show.model_validate(data)
    data["videos"]["poster"]["file"] = "poster.mp4"
    with pytest.raises(ValueError, match="start_at after out_point"):
        Show.model_validate(data)


# ------------------------------------------------------------------ controller + fake player


@pytest.fixture
def ictrl(settings, player):
    from projektor.controller import Controller

    st = ShowStore.from_settings(settings)
    st.media_dir.mkdir(parents=True, exist_ok=True)
    st.path.write_text(json.dumps(IMAGE_SHOW))
    c = Controller(settings, st, player)
    c.start()
    return c


def test_image_cue_fades_in_holds_and_fades_out(ictrl, player):
    ictrl.go()  # cue 1: poster, fade in 2 s
    st = ictrl.state()
    assert st["state"] == "fading_in" and st["video"] == "poster"
    player.advance(1.0)
    assert ictrl.state()["state"] == "fading_in"
    assert 0.2 < player.alpha < 0.8
    player.advance(1.1)
    assert ictrl.state()["state"] == "holding" and player.alpha == 1.0
    player.advance(30.0)
    assert ictrl.state()["state"] == "holding"  # stays until the next command
    ictrl.pause()
    ictrl.seek(1.0)
    assert ictrl.state()["state"] == "holding"  # no pause / seek on images
    ictrl.fadeout()  # default: the image's fade_out (3 s)
    assert player.calls[-1] == ("fadeout", 3.0)
    assert ictrl.state()["state"] == "fading_out"
    player.advance(2.9)
    assert ictrl.state()["state"] == "fading_out"
    player.advance(0.2)
    assert ictrl.state()["state"] == "idle"


def test_image_cue_overrides_and_next_cue(ictrl, player):
    ictrl.cue("2")  # fade in 4 s, fade out 0.5 s
    player.advance(3.9)
    assert ictrl.state()["state"] == "fading_in"
    player.advance(0.2)
    assert ictrl.state()["state"] == "holding"
    ictrl.fadeout()
    assert player.calls[-1] == ("fadeout", 0.5)
    player.advance(0.6)
    assert ictrl.state()["state"] == "idle"
    ictrl.cue("4")  # no fade: on screen at once
    assert ictrl.state()["state"] == "playing"
    player.advance(0.1)
    assert ictrl.state()["state"] == "holding" and player.alpha == 1.0
    assert ictrl.state()["standby"] is None
    ictrl.cue("3")  # a video after the image
    assert ictrl.state()["video"] == "intro"


# ------------------------------------------------------------------ web


@pytest.fixture
def wenv(tmp_path):
    from fastapi.testclient import TestClient

    from projektor.web.app import create_app
    from projektor.web.fakes import FakeController, FakeDispatcher, FakeShowStore, dev_settings

    data = tmp_path / "data"
    store = FakeShowStore(data)
    store.path.write_text(json.dumps({"version": 1, "videos": {}, "cues": []}))
    ctrl = FakeController(store)
    app = create_app(dev_settings(data), ctrl, FakeDispatcher(ctrl), store,
                     power_cmds={"reboot": None, "shutdown": None})
    return TestClient(app), store


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
def test_upload_image_creates_entry_and_probes(wenv, tmp_path):
    c, store = wenv
    small, big = tmp_path / "small.jpg", tmp_path / "big.png"
    _ffmpeg_image(small, "640x360")
    _ffmpeg_image(big, "2560x1440")
    r = c.put("/api/upload?name=Small Pic.jpg", content=small.read_bytes())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["video_id"] == "small-pic"
    assert j["probe"]["image"] is True and (j["probe"]["width"], j["probe"]["height"]) == (640, 360)
    assert j["warnings"] == []
    r = c.put("/api/upload?name=big.png", content=big.read_bytes())
    assert any("larger than the 1080p output" in w for w in r.json()["warnings"])
    lst = c.get("/api/videos").json()
    v = {x["id"]: x for x in lst["videos"]}
    assert v["small-pic"]["image"] is True and v["big"]["image"] is True
    assert v["small-pic"]["probe"]["duration"] is None and v["small-pic"]["probe"]["prepared"] is False
    # fades are the only timing fields that matter for images; editing them works as for videos
    assert c.patch("/api/videos/small-pic", json={"fade_in": 1.5, "fade_out": 4}).status_code == 200
    assert c.post("/api/cues", json={"video": "small-pic", "fade_out": 0.5}).status_code == 200
    show = c.get("/api/show").json()
    assert show["videos"]["small-pic"]["fade_in"] == 1.5 and show["videos"]["small-pic"]["fade_out"] == 4.0
    assert show["cues"][0]["fade_out"] == 0.5


def test_orphan_image_flag(wenv):
    c, store = wenv
    (store.media_dir / "loose.webp").write_bytes(b"1234")
    (store.media_dir / "loose.mp4").write_bytes(b"1234")
    files = {f["name"]: f for f in c.get("/api/videos?probe=false").json()["files"]}
    assert files["loose.webp"]["image"] is True and files["loose.mp4"]["image"] is False


def test_image_warnings():
    from projektor.web.media import codec_warnings

    ok = {"image": True, "codec": "image/jpeg", "width": 1920, "height": 1080}
    assert codec_warnings(ok) == []
    assert "larger than the 1080p" in codec_warnings({**ok, "width": 4000, "height": 3000})[0]
    assert "not a JPEG, PNG or WebP" in codec_warnings({**ok, "codec": "image/bmp", "codec_name": "BMP"})[0]
    # never the video codec / keyframe warnings
    assert not any("H.264" in w or "keyframe" in w for w in codec_warnings({**ok, "width": 4000}))


# ------------------------------------------------------------------ projektor-prepare


def test_image_output_naming(tmp_path):
    from projektor.prepare import image_output_path, is_image_file

    assert is_image_file(Path("a/IMG_1.HEIC")) and is_image_file(Path("b.tif")) and not is_image_file(Path("c.mov"))
    assert image_output_path(Path("a/photo.heic"), tmp_path) == tmp_path / "photo.jpg"
    assert image_output_path(Path("a/logo.png")) == Path("a/logo.png").with_name("logo.prepared.png")
    assert image_output_path(Path("a/photo.jpg")) == Path("a/photo.prepared.jpg")
    assert image_output_path(Path("a/photo.webp")) == Path("a/photo.jpg")


def test_image_plan():
    from projektor.prepare import Options, SourceInfo, build_image_plan

    info = SourceInfo(width=4000, height=3000, codec="mjpeg")
    p = build_image_plan(Path("in.jpg"), Path("out.jpg"), info, Options(), tmp=Path(".out.part.jpg"))
    assert (p.width, p.height) == (1440, 1080)
    assert p.cmd == [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", "in.jpg", "-map", "0:0", "-map_metadata", "-1", "-frames:v", "1",
        "-vf", "scale=w='min(iw,1920)':h='min(ih,1080)':force_original_aspect_ratio=decrease:flags=lanczos,setsar=1",
        "-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj420p", "-update", "1", "-f", "image2", ".out.part.jpg",
    ]
    assert p.notes == ["scaling 4000x3000 to fit into 1920x1080"]
    small = build_image_plan(Path("l.png"), Path("o.png"), SourceInfo(width=800, height=600, codec="png"),
                             Options(height=720))
    assert (small.width, small.height) == (800, 600) and small.notes == []
    assert "scale=w='min(iw,1280)':h='min(ih,720)'" in small.cmd[small.cmd.index("-vf") + 1]
    assert small.cmd[small.cmd.index("-c:v") + 1] == "png" and "-pix_fmt" not in small.cmd
    heic = build_image_plan(Path("i.heic"), Path("i.jpg"), SourceInfo(width=100, height=100, codec="hevc"), Options())
    assert heic.notes == ["converting hevc to JPEG"]


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
def test_prepare_images_end_to_end(tmp_path, capsys):
    from projektor.prepare import main

    raw = tmp_path / "raw.jpg"
    _ffmpeg_image(raw, "4000x3000")
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(_exif_orient(raw.read_bytes(), 6))  # camera portrait: rotate 90° clockwise
    logo = tmp_path / "logo.png"
    _ffmpeg_image(logo, "320x240")
    out = tmp_path / "out"
    assert main([str(photo), str(logo), "-o", str(out)]) == 0

    def probe(p):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                            "-of", "json", str(p)], capture_output=True, text=True, check=True)
        return json.loads(r.stdout)["streams"][0]

    st = probe(out / "photo.jpg")
    assert (st["codec_name"], st["width"], st["height"]) == ("mjpeg", 810, 1080)  # upright, fitted
    assert b"Exif" not in (out / "photo.jpg").read_bytes()[:4096]  # no orientation left to re-apply
    st = probe(out / "logo.png")
    assert (st["codec_name"], st["width"], st["height"]) == ("png", 320, 240)  # never upscaled
    assert not list(out.glob(".*part*"))
    # second run: outputs exist -> skipped
    assert main([str(photo), "-o", str(out)]) == 0
    assert "exists, skipping" in capsys.readouterr().out


def test_expand_inputs(tmp_path):
    from projektor.prepare import expand_inputs

    for rel in ["show/b.JPG", "show/a.png", "show/sub/c.heic", "show/clip.mov", "show/notes.txt",
                "show/.hidden.jpg", "show/prepared/old.jpg", "show/.cache/x.png"]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    got = expand_inputs([str(tmp_path / "show"), str(tmp_path / "missing.jpg")])
    rel = [str(p.relative_to(tmp_path)) for p in got]
    assert rel == ["show/a.png", "show/b.JPG", "show/clip.mov", "show/sub/c.heic", "missing.jpg"]


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
def test_prepare_folder(tmp_path, capsys):
    from projektor.prepare import main

    show = tmp_path / "Bilder"
    (show / "Akt 2").mkdir(parents=True)
    _ffmpeg_image(show / "poster.png", "2560x1440")
    _ffmpeg_image(show / "Akt 2" / "szene 1.jpg", "640x480")
    _ffmpeg_image(show / "Akt 2" / "poster.webp", "320x240")  # -> poster.jpg: no collision with poster.png
    _ffmpeg_image(show / "Akt 2" / "szene 1.bmp", "320x240")  # -> szene 1.jpg: collides
    (show / "prepared").mkdir()
    _ffmpeg_image(show / "prepared" / "old.jpg", "320x240")  # earlier output: ignored
    assert main([str(show)]) == 1  # one error (the collision), the rest done
    out = capsys.readouterr()
    assert "4 file(s) -> " in out.out and "also written for" in out.err
    assert sorted(p.name for p in (show / "prepared").iterdir()) == ["old.jpg", "poster.jpg", "poster.png", "szene 1.jpg"]
    assert not (show / "prepared" / "prepared").exists()
