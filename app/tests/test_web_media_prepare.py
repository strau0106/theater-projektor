"""Web probe: prepared marker and keyframe-interval warnings."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from projektor.web.media import Prober, codec_warnings

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="ffmpeg not installed")


def _clip(path, gop, comment=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=12",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", str(gop),
           "-keyint_min", str(gop), "-sc_threshold", "0"]
    if comment:
        cmd += ["-metadata", f"comment={comment}"]
    subprocess.run(cmd + ["-movflags", "+faststart", str(path)], check=True)


def test_prepared_clip_has_tag(tmp_path):
    p = tmp_path / "a.mp4"
    _clip(p, 12, "projektor-prepared v1 gop=0.5 fps=25")
    pr = Prober().probe(p)
    assert pr["prepared"] is True and pr["marker"]["gop"] == 0.5
    assert pr["keyframe_interval"] == pytest.approx(0.5, abs=0.05)
    assert not any("keyframe" in w for w in codec_warnings(pr))


def test_long_gop_warns(tmp_path):
    p = tmp_path / "b.mp4"
    _clip(p, 250)
    pr = Prober().probe(p)
    assert pr["prepared"] is False
    assert pr["keyframe_interval"] > 5
    assert any("keyframe" in w and "projektor-prepare" in w for w in codec_warnings(pr))
