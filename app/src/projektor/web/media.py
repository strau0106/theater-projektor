"""Media files: probing (GstPbutils Discoverer), codec warnings, safe file names."""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

UPLOAD_PREFIX = ".upload-"
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".mts", ".mpg", ".mpeg", ".h264", ".h265"}

_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str) -> str:
    """Reduce a client-supplied name to a plain file name inside the media dir."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_RE.sub("_", name).strip(" .")
    if not name or name.startswith(UPLOAD_PREFIX):
        raise ValueError("invalid file name")
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 200 - len(ext) - 1] + "." + ext) if dot else name[:200]
    return name


def list_media_files(media_dir: Path) -> list[dict]:
    out = []
    try:
        entries = list(os.scandir(media_dir))
    except FileNotFoundError:
        return out
    for e in entries:
        if not e.is_file() or e.name.startswith("."):
            continue
        st = e.stat()
        out.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: f["name"].lower())
    return out


# ---------------------------------------------------------------- probing


class Prober:
    """Caches Discoverer results per (path, size, mtime). Degrades if gi is missing."""

    def __init__(self, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[tuple, dict]] = {}
        self._lock = threading.Lock()
        self._gst = None  # (Gst, GstPbutils) or False if unavailable

    def _load_gst(self):
        if self._gst is None:
            try:
                import gi

                gi.require_version("Gst", "1.0")
                gi.require_version("GstPbutils", "1.0")
                from gi.repository import Gst, GstPbutils

                Gst.init(None)
                self._gst = (Gst, GstPbutils)
            except Exception as e:  # ImportError, ValueError (namespace missing)
                log.warning("GStreamer Discoverer unavailable: %s", e)
                self._gst = False
        return self._gst

    def probe(self, path: Path) -> dict:
        path = Path(path)
        try:
            st = path.stat()
        except OSError as e:
            return {"error": f"missing: {e.strerror}"}
        key = (st.st_size, st.st_mtime_ns)
        with self._lock:
            hit = self._cache.get(str(path))
            if hit and hit[0] == key:
                return hit[1]
            info = self._probe_uncached(path)
            self._cache[str(path)] = (key, info)
            return info

    def _probe_uncached(self, path: Path) -> dict:
        gst = self._load_gst()
        if not gst:
            return {"error": "probe unavailable (no GStreamer)"}
        Gst, GstPbutils = gst
        try:
            disc = GstPbutils.Discoverer.new(int(self.timeout_s * Gst.SECOND))
            info = disc.discover_uri(path.resolve().as_uri())
        except Exception as e:
            msg = getattr(e, "message", None) or str(e)
            return {"error": f"not a playable media file ({msg})"}
        res: dict = {}
        dur = info.get_duration()
        res["duration"] = round(dur / Gst.SECOND, 3) if dur and dur > 0 else None
        try:
            ccaps = info.get_stream_info().get_caps()
            res["container"] = GstPbutils.pb_utils_get_codec_description(ccaps) if ccaps else None
        except Exception:
            res["container"] = None
        vids = info.get_video_streams()
        if vids:
            v = vids[0]
            caps = v.get_caps()
            name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
            res["codec"] = name
            try:
                res["codec_name"] = GstPbutils.pb_utils_get_codec_description(caps)
            except Exception:
                res["codec_name"] = name
            res["width"] = v.get_width()
            res["height"] = v.get_height()
            num, den = v.get_framerate_num(), v.get_framerate_denom()
            res["fps"] = round(num / den, 3) if den else None
            res["bitrate"] = v.get_bitrate() or v.get_max_bitrate() or None
            st = caps.get_structure(0) if caps and caps.get_size() else None
            if st is not None and st.has_field("profile"):
                res["profile"] = st.get_string("profile")
            try:
                res["interlaced"] = bool(v.is_interlaced())
            except Exception:
                pass
        else:
            res["codec"] = None
        res["audio"] = len(info.get_audio_streams()) > 0
        return res


def codec_warnings(probe: dict | None) -> list[str]:
    """Human-readable warnings about Pi 4 / Pi 5 decode capabilities."""
    if not probe:
        return []
    if probe.get("error"):
        return [probe["error"]]
    codec = (probe.get("codec") or "").lower()
    if not codec:
        return ["no video stream found"]
    w, h = probe.get("width") or 0, probe.get("height") or 0
    fps = probe.get("fps") or 0
    warn: list[str] = []
    big = w > 1920 or h > 1080
    if codec == "video/x-h264":
        if big:
            warn.append(f"H.264 {w}x{h}: Pi 4 hardware decode is limited to 1080p; Pi 5 decodes H.264 in software - re-encode to 1920x1080")
        elif fps > 30.5:
            warn.append(f"H.264 at {fps:g} fps: Pi 5 decodes H.264 in software, keep <= 1080p30 (fine on Pi 4)")
    elif codec == "video/x-h265":
        warn.append("HEVC: ideal for Pi 5 (hardware); on Pi 4 use H.264")
        if big:
            warn.append(f"HEVC {w}x{h}: larger than the 1080p output - re-encode to 1920x1080 to save CPU")
    else:
        warn.append(
            f"codec {probe.get('codec_name') or codec} is not hardware-decoded on the Pi - "
            "convert to H.264 (Pi 4) or HEVC/H.264 1080p30 (Pi 5)"
        )
        if big:
            warn.append(f"resolution {w}x{h} exceeds 1080p")
    if probe.get("interlaced"):
        warn.append("interlaced video - deinterlace before use")
    profile = (probe.get("profile") or "").lower()
    hevc_main10 = codec == "video/x-h265" and profile == "main-10"
    if not hevc_main10 and any(t in profile for t in ("10", "12", "444", "422")):
        warn.append(f"profile {profile}: use 8-bit 4:2:0 (H.264 High / HEVC Main) for hardware decode")
    return warn
