"""projektor-prepare: ffmpeg argument building, marker, naming, one real transcode."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from projektor.prepare import (Options, SourceInfo, build_plan, main, make_marker, output_path, parse_marker)

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _plan(w, h, fps, **kw):
    info = SourceInfo(width=w, height=h, fps=Fraction(fps).limit_denominator(1001))
    return build_plan(Path("in.mov"), Path("out.mp4"), info, Options(**kw))


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_4k60_to_1080p30():
    p = _plan(3840, 2160, 60)
    assert (p.width, p.height) == (1920, 1080)
    assert p.fps == 30 and p.gop_frames == 15
    assert "fps=30" in _arg(p.cmd, "-vf") and "scale=1920:1080" in _arg(p.cmd, "-vf")
    assert _arg(p.cmd, "-g") == "15" and _arg(p.cmd, "-profile:v") == "high"
    assert "-an" in p.cmd and _arg(p.cmd, "-movflags") == "+faststart"
    assert _arg(p.cmd, "-metadata").startswith("comment=projektor-prepared v1")
    assert _arg(p.cmd, "-pix_fmt") == "yuv420p"


def test_720p25_kept():
    p = _plan(1280, 720, 25)
    assert (p.width, p.height) == (1280, 720)
    assert p.fps == 25 and p.gop_frames in (12, 13)
    assert "scale=1280:720" in _arg(p.cmd, "-vf")


def test_50fps_to_25():
    p = _plan(1920, 1080, 50)
    assert p.fps == 25


def test_marker_roundtrip_and_skip():
    m = make_marker(0.5, Fraction(25))
    assert parse_marker(m) == {"version": 1, "gop": 0.5, "fps": 25.0}
    assert parse_marker("hello") is None and parse_marker(None) is None


def test_output_naming(tmp_path):
    assert output_path(Path("a/clip.mov"), tmp_path) == tmp_path / "clip.mp4"
    assert output_path(Path("a/clip.mov")) == Path("a/clip.mp4")
    assert output_path(Path("a/clip.mp4")) == Path("a/clip.prepared.mp4")


def _ffprobe(path, *args):
    r = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
def test_end_to_end(tmp_path, capsys):
    src = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2",
                    "-c:v", "libx264", "-g", "250", str(src)], check=True)
    out = tmp_path / "out"
    assert main([str(src), "-o", str(out), "--preset", "veryfast"]) == 0
    dst = out / "src.mp4"
    d = _ffprobe(dst, "-show_entries", "format_tags=comment:stream=codec_name,profile,pix_fmt")
    assert d["format"]["tags"]["comment"].startswith("projektor-prepared v1")
    st = d["streams"][0]
    assert st["codec_name"] == "h264" and st["profile"] == "High" and st["pix_fmt"] == "yuv420p"
    k = _ffprobe(dst, "-select_streams", "v", "-skip_frame", "nokey", "-show_entries", "frame=pts_time")
    times = [float(f["pts_time"]) for f in k["frames"]]
    assert len(times) >= 4 and all(abs((b - a) - 0.5) < 0.05 for a, b in zip(times, times[1:]))
    # second run skips the already prepared output
    assert main([str(dst), "-o", str(out)]) == 0
    assert "already prepared" in capsys.readouterr().out
