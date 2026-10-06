"""Media files: probing (GstPbutils Discoverer, MP4 sample tables, optional ffprobe),
codec / keyframe warnings, "prepared" marker, safe file names."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import warnings
from pathlib import Path

from projektor.prepare import parse_marker

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


# ---------------------------------------------------------------- keyframes / marker
#
# "Prepared" files (projektor-prepare, see docs/videos.md) carry the comment
# "projektor-prepared v1 gop=<s> fps=<f>". For all files the keyframe interval is
# measured: seeks, start_at / in points and loops decode from the previous keyframe,
# so long GOPs make them slow. MP4/MOV (stss sample table) are parsed in pure Python,
# which is exact and cheap and needs no ffprobe (the Pi image has none); other
# containers use ffprobe when it is installed, else the interval stays unknown.

KEYFRAME_WARN_S = 1.0          # warn above this keyframe interval
KEYFRAME_SCAN_S = 30.0         # ffprobe: only scan the first 30 s
_MAX_MOOV = 64 * 1024 * 1024


def _iter_boxes(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, i)
        hdr = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, i + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            return
        yield typ, i + hdr, i + size
        i += size


def _child(buf: bytes, start: int, end: int, typ: bytes):
    for t, s, e in _iter_boxes(buf, start, end):
        if t == typ:
            return s, e
    return None


def _path(buf: bytes, start: int, end: int, *types: bytes):
    rng = (start, end)
    for t in types:
        rng = _child(buf, rng[0], rng[1], t)
        if rng is None:
            return None
    return rng


def _read_moov(path: Path) -> bytes | None:
    """Return the moov box payload, or None (not ISO-BMFF / fragmented / too big)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        fsize = f.tell()
        pos = 0
        first = True
        while pos + 8 <= fsize:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                return None
            size, typ = struct.unpack_from(">I4s", hdr, 0)
            hlen = 8
            if size == 1:
                if len(hdr) < 16:
                    return None
                size = struct.unpack_from(">Q", hdr, 8)[0]
                hlen = 16
            elif size == 0:
                size = fsize - pos
            if first and typ not in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid"):
                return None
            first = False
            if size < hlen:
                return None
            if typ == b"moov":
                if size > _MAX_MOOV:
                    return None
                f.seek(pos + hlen)
                data = f.read(size - hlen)
                return data if len(data) == size - hlen else None
            pos += size
    return None


def _ilst_comment(buf: bytes, s: int, e: int) -> str | None:
    for t, cs, ce in _iter_boxes(buf, s, e):
        if t == b"\xa9cmt":
            d = _child(buf, cs, ce, b"data")
            if d and d[1] - d[0] >= 8:
                return buf[d[0] + 8:d[1]].decode("utf-8", "replace")
    return None


def _mp4_comment(moov: bytes) -> str | None:
    udta = _child(moov, 0, len(moov), b"udta")
    if not udta:
        return None
    meta = _child(moov, udta[0], udta[1], b"meta")
    if meta:
        s, e = meta
        if moov[s + 4:s + 8] != b"hdlr":  # ISO meta is a full box (version/flags), QuickTime's is not
            s += 4
        ilst = _child(moov, s, e, b"ilst")
        if ilst:
            c = _ilst_comment(moov, *ilst)
            if c is not None:
                return c
    q = _child(moov, udta[0], udta[1], b"\xa9cmt")  # QuickTime: u16 len, u16 lang, text
    if q and q[1] - q[0] >= 4:
        n = struct.unpack_from(">H", moov, q[0])[0]
        return moov[q[0] + 4:q[0] + 4 + n].decode("utf-8", "replace")
    return None


def mp4_info(path: Path) -> dict | None:
    """Keyframe times + comment of the first video track of an MP4/MOV file.

    Returns ``{"keyframes": [s, ...], "duration": s, "frames": n, "comment": str|None}``
    or None if the file is not a (non-fragmented) MP4/MOV with a video track."""
    try:
        moov = _read_moov(Path(path))
    except OSError:
        return None
    if not moov:
        return None
    comment = _mp4_comment(moov)
    for t, ts, te in _iter_boxes(moov, 0, len(moov)):
        if t != b"trak":
            continue
        mdia = _child(moov, ts, te, b"mdia")
        if not mdia:
            continue
        hdlr = _child(moov, mdia[0], mdia[1], b"hdlr")
        if not hdlr or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        mdhd = _child(moov, mdia[0], mdia[1], b"mdhd")
        stbl = _path(moov, mdia[0], mdia[1], b"minf", b"stbl")
        if not mdhd or not stbl:
            continue
        ver = moov[mdhd[0]]
        timescale = struct.unpack_from(">I", moov, mdhd[0] + (20 if ver == 1 else 12))[0]
        stts = _child(moov, stbl[0], stbl[1], b"stts")
        if not stts or not timescale:
            return None
        n_stts = struct.unpack_from(">I", moov, stts[0] + 4)[0]
        n_stts = min(n_stts, (stts[1] - stts[0] - 8) // 8)
        entries = [struct.unpack_from(">II", moov, stts[0] + 8 + 8 * k) for k in range(n_stts)]
        frames = sum(c for c, _ in entries)
        if not frames:
            return None  # fragmented MP4: samples live in moof boxes
        total = sum(c * d for c, d in entries)
        stss = _child(moov, stbl[0], stbl[1], b"stss")
        if stss is None:
            sync = None  # every sample is a sync sample
        else:
            n = struct.unpack_from(">I", moov, stss[0] + 4)[0]
            n = min(n, (stss[1] - stss[0] - 8) // 4)
            sync = struct.unpack_from(f">{n}I", moov, stss[0] + 8) if n else ()
        times: list[float] = []
        if sync is None:
            sample, t_acc = 1, 0
            for c, d in entries:
                for _ in range(c):
                    times.append(t_acc / timescale)
                    t_acc += d
                    sample += 1
        else:
            k, sample, t_acc = 0, 1, 0  # sample = number of the first sample of the current stts run
            for c, d in entries:
                while k < len(sync) and sync[k] < sample + c:
                    times.append((t_acc + (sync[k] - sample) * d) / timescale)
                    k += 1
                sample += c
                t_acc += c * d
        return {"keyframes": times, "duration": total / timescale, "frames": frames, "comment": comment}
    return None


def keyframe_gap(times: list[float], end: float | None) -> float | None:
    """Longest distance between keyframes (incl. the last keyframe to ``end``)."""
    if not times:
        return None
    ts = sorted(times)
    pts = ts + ([end] if end and end > ts[-1] else [])
    if len(pts) < 2:
        return None
    return round(max(b - a for a, b in zip(pts, pts[1:])), 3)


def _run_ffprobe(ffprobe: str, args: list[str], timeout: float) -> dict | None:
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-of", "json", *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ffprobe failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None


def ffprobe_keyframes(path: Path, ffprobe: str = "ffprobe", scan_s: float = KEYFRAME_SCAN_S,
                      timeout: float = 30.0) -> dict | None:
    """Keyframe times in the first ``scan_s`` seconds from packet flags (no decoding)."""
    d = _run_ffprobe(ffprobe, ["-select_streams", "v:0", "-read_intervals", f"%+{scan_s:g}",
                               "-show_entries", "packet=pts_time,flags", str(path)], timeout)
    if not d:
        return None
    kf, last = [], None
    for p in d.get("packets") or []:
        try:
            t = float(p.get("pts_time"))
        except (TypeError, ValueError):
            continue
        last = t if last is None else max(last, t)
        if "K" in (p.get("flags") or ""):
            kf.append(t)
    if last is None:
        return None
    return {"keyframes": kf, "end": last}


_FF_CODEC = {"h264": "video/x-h264", "hevc": "video/x-h265", "vp8": "video/x-vp8", "vp9": "video/x-vp9",
             "av1": "video/x-av1", "mpeg2video": "video/mpeg", "mpeg4": "video/mpeg", "prores": "video/x-prores"}


def ffprobe_streams(path: Path, ffprobe: str = "ffprobe", timeout: float = 30.0) -> dict:
    """Probe dict (same keys as the Discoverer path) from ffprobe."""
    d = _run_ffprobe(ffprobe, ["-show_format", "-show_streams", str(path)], timeout)
    if d is None:
        return {"error": "not a playable media file (ffprobe failed)"}
    fmt = d.get("format") or {}
    streams = d.get("streams") or []
    res: dict = {}
    try:
        res["duration"] = round(float(fmt.get("duration")), 3)
    except (TypeError, ValueError):
        res["duration"] = None
    res["container"] = fmt.get("format_long_name") or fmt.get("format_name")
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if vids:
        v = vids[0]
        name = v.get("codec_name") or ""
        res["codec"] = _FF_CODEC.get(name, f"video/x-{name}")
        res["codec_name"] = v.get("codec_long_name") or name
        res["width"], res["height"] = v.get("width"), v.get("height")
        fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or ""
        try:
            num, den = (int(x) for x in fr.split("/"))
            res["fps"] = round(num / den, 3) if den else None
        except ValueError:
            res["fps"] = None
        try:
            res["bitrate"] = int(v.get("bit_rate")) or None
        except (TypeError, ValueError):
            res["bitrate"] = None
        if v.get("profile"):
            res["profile"] = str(v["profile"]).lower().replace(" ", "-")
        lvl = v.get("level")
        if isinstance(lvl, int) and lvl > 0:
            res["level"] = f"{lvl / 10:g}" if name == "h264" else (f"{lvl / 30:g}" if name == "hevc" else str(lvl))
        if v.get("pix_fmt"):
            res["pix_fmt"] = v["pix_fmt"]
        res["interlaced"] = (v.get("field_order") or "") in ("tt", "bb", "tb", "bt")
    else:
        res["codec"] = None
    res["audio"] = any(s.get("codec_type") == "audio" for s in streams)
    tags = {k.lower(): val for k, val in (fmt.get("tags") or {}).items()}
    if tags.get("comment"):
        res["comment"] = tags["comment"]
    return res


# ---------------------------------------------------------------- probing


class Prober:
    """Caches probe results per (path, size, mtime).

    Stream info from GstPbutils Discoverer (ffprobe if GStreamer is missing), keyframe
    interval from the MP4 sample table (ffprobe for other containers, if installed),
    "prepared" marker from the container comment. Degrades if gi / ffprobe are missing."""

    def __init__(self, timeout_s: float = 10.0, ffprobe: str | bool | None = True):
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[tuple, dict]] = {}
        self._lock = threading.Lock()
        self._gst = None  # (Gst, GstPbutils) or False if unavailable
        self._ffprobe_opt = ffprobe  # True = find on PATH, False/None = never use, str = path
        self._ffprobe: str | None | bool = None  # resolved lazily; False = unavailable

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

    @property
    def ffprobe(self) -> str | None:
        if self._ffprobe is None:
            opt = self._ffprobe_opt
            if opt is True:
                self._ffprobe = shutil.which("ffprobe") or False
            elif opt:
                self._ffprobe = str(opt)
            else:
                self._ffprobe = False
            if not self._ffprobe:
                log.info("ffprobe not installed: keyframe interval only measured for MP4/MOV files")
        return self._ffprobe or None

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
        if gst:
            res = self._discover(path, gst)
        elif self.ffprobe:
            res = ffprobe_streams(path, self.ffprobe)
        else:
            return {"error": "probe unavailable (no GStreamer)"}
        if res.get("error") or not res.get("codec"):
            return res
        self._add_keyframes(path, res)
        comment = res.pop("comment", None)
        marker = parse_marker(comment)
        res["prepared"] = marker is not None
        res["marker"] = marker
        return res

    def _add_keyframes(self, path: Path, res: dict) -> None:
        res["keyframe_interval"] = None
        res["keyframe_source"] = None
        try:
            mp4 = mp4_info(path)
        except Exception as e:  # malformed file: never break the video list
            log.warning("mp4 parse of %s failed: %s", path.name, e)
            mp4 = None
        if mp4:
            res["keyframe_interval"] = keyframe_gap(mp4["keyframes"], mp4["duration"])
            res["keyframe_count"] = len(mp4["keyframes"])
            res["keyframe_source"] = "mp4"
            if mp4.get("comment") and not res.get("comment"):
                res["comment"] = mp4["comment"]
            return
        if self.ffprobe:
            kf = ffprobe_keyframes(path, self.ffprobe)
            if kf and kf["keyframes"]:
                res["keyframe_interval"] = keyframe_gap(kf["keyframes"], kf["end"])
                res["keyframe_source"] = "ffprobe"

    def _discover(self, path: Path, gst) -> dict:
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
            if st is not None:
                if st.has_field("profile"):
                    res["profile"] = st.get_string("profile")
                if st.has_field("level"):
                    res["level"] = st.get_string("level")
                chroma = st.get_string("chroma-format") if st.has_field("chroma-format") else None
                ok, depth = st.get_uint("bit-depth-luma") if st.has_field("bit-depth-luma") else (False, 0)
                if chroma:
                    pf = {"4:2:0": "yuv420p", "4:2:2": "yuv422p", "4:4:4": "yuv444p", "4:0:0": "gray"}.get(chroma)
                    if pf and ok and depth > 8:
                        pf += f"{depth}"
                    if pf:
                        res["pix_fmt"] = pf
            try:
                res["interlaced"] = bool(v.is_interlaced())
            except Exception:
                pass
        else:
            res["codec"] = None
        res["audio"] = len(info.get_audio_streams()) > 0
        res["comment"] = _discoverer_comment(info)
        return res


def _discoverer_comment(info) -> str | None:
    for get in (lambda: info.get_stream_info().get_tags(), lambda: info.get_tags()):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tags = get()
            if tags is None:
                continue
            ok, val = tags.get_string("comment")
            if ok and val:
                return val
        except Exception:
            continue
    return None


PREPARE_HINT = "run projektor-prepare (see docs/videos.md)"


def codec_warnings(probe: dict | None) -> list[str]:
    """Human-readable warnings about Pi 4 / Pi 5 decode capabilities and keyframe interval."""
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
        try:
            level = float(probe.get("level") or 0)
        except ValueError:
            level = 0
        if level > 4.2:
            warn.append(f"H.264 level {probe.get('level')}: above 4.2, the Pi 4 hardware decoder may refuse it - {PREPARE_HINT}")
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
    else:
        pf = probe.get("pix_fmt") or ""
        if pf and pf not in ("yuv420p", "yuvj420p", "nv12") and not (hevc_main10 and pf.startswith("yuv420p")):
            warn.append(f"pixel format {pf}: use 8-bit 4:2:0 (yuv420p) for hardware decode")
    ki = probe.get("keyframe_interval")
    if ki is not None and ki > KEYFRAME_WARN_S + 0.02:
        warn.append(f"keyframe every {ki:.3g} s: seeks/loops/start points slow - {PREPARE_HINT}")
    return warn
