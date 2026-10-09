"""``projektor-prepare``: transcode videos into the format the Pi plays best.

Runs on a PC/Mac (needs ``ffmpeg`` + ``ffprobe`` on PATH), not on the Pi. Output:
MP4, H.264 High (or HEVC Main for Pi 5), level <= 4.1, max 1920x1080 (never
upscaled), 8-bit 4:2:0, BT.709, constant frame rate, closed GOP with an IDR
keyframe every ``--gop-s`` seconds (default 0.5 s), no audio, ``+faststart``.
Short GOPs make seeks, ``start_at``/in points and loops cheap and exact: the
decoder only has to decode from the previous keyframe.

A marker ``comment=projektor-prepared v1 gop=<s> fps=<f>`` is written into the
file; the web UI shows such files as "prepared" and this tool skips them unless
``--force``.

Still images (``IMAGE_INPUT_EXTS``: JPEG, PNG, WebP, HEIC, TIFF, ...) are converted
to what the projector shows fastest: EXIF orientation applied (ffmpeg autorotate),
shrunk to fit max 16:9 x ``--height`` (default 1920x1080, never upscaled), metadata
stripped. Output ``<stem>.jpg`` (high quality JPEG), or ``<stem>.png`` for PNG inputs
(lossless, keeps transparency; transparent areas show black on the projector).

Inputs may be folders: they are searched recursively for videos and images
(``expand_inputs``; hidden files and ``prepared`` folders are skipped). A single folder
without ``-o`` writes to ``<folder>/prepared/``.

Standard library only (no projektor imports), so it can also run straight from a
checkout: ``python3 app/src/projektor/prepare.py`` / ``app/scripts/prepare-video.sh``.
Optional upload to the projector: ``--upload http://projektor.local`` (password
from ``--password-file`` or env ``PROJEKTOR_PASSWORD``, never from the command line).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

MARKER_PREFIX = "projektor-prepared"
MARKER_VERSION = 1

# Frame rates the Pi plays natively (display runs at 50/60 Hz).
_STD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30)]
_DEFAULT_RATE = Fraction(30)
_RATE_TOL = 0.01  # relative

H264_LEVEL = "4.1"
HEVC_LEVEL = "4.1"

# Still image inputs (anything ffmpeg decodes as one picture). The projector itself shows
# JPEG, PNG and WebP (projektor.config.IMAGE_EXTS); outputs are JPEG, or PNG for PNG inputs.
IMAGE_INPUT_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
                              ".heic", ".heif", ".avif"})
JPEG_QSCALE = 2  # ffmpeg mjpeg -q:v, 2 = best practical quality
# Video inputs picked up when a folder is given (files named explicitly are always tried).
VIDEO_INPUT_EXTS = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".mts", ".m2ts",
                              ".mpg", ".mpeg", ".mxf", ".wmv", ".flv", ".3gp", ".h264", ".h265"})
PREPARED_DIR = "prepared"


class PrepareError(Exception):
    pass


# ------------------------------------------------------------------ marker


def make_marker(gop_s: float, fps: Fraction) -> str:
    return f"{MARKER_PREFIX} v{MARKER_VERSION} gop={gop_s:g} fps={fmt_fps(fps)}"


def parse_marker(comment: Optional[str]) -> Optional[dict]:
    """``"projektor-prepared v1 gop=0.5 fps=30"`` -> ``{"version": 1, "gop": 0.5, "fps": 30.0}``."""
    if not comment or not isinstance(comment, str):
        return None
    parts = comment.strip().split()
    if not parts or parts[0] != MARKER_PREFIX:
        return None
    out: dict = {"version": None, "gop": None, "fps": None}
    for p in parts[1:]:
        if p.startswith("v") and p[1:].isdigit():
            out["version"] = int(p[1:])
        elif "=" in p:
            k, _, val = p.partition("=")
            if k in ("gop", "fps"):
                try:
                    out[k] = float(val)
                except ValueError:
                    pass
    return out


# ------------------------------------------------------------------ source info


@dataclass
class SourceInfo:
    width: int
    height: int
    fps: Optional[Fraction] = None
    duration: Optional[float] = None
    codec: str = ""
    stream_index: int = 0
    sar: Fraction = Fraction(1)
    rotation: int = 0  # degrees, as ffmpeg's autorotate will apply it
    interlaced: bool = False
    color_space: str = ""
    color_range: str = ""
    color_transfer: str = ""
    pix_fmt: str = ""
    comment: Optional[str] = None
    has_audio: bool = False

    @property
    def marker(self) -> Optional[dict]:
        return parse_marker(self.comment)


def _frac(s) -> Optional[Fraction]:
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        f = Fraction(str(s).replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def source_info_from_ffprobe(data: dict) -> SourceInfo:
    """Build SourceInfo from ``ffprobe -print_format json -show_format -show_streams``."""
    streams = data.get("streams") or []
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if not vids:
        raise PrepareError("no video stream")
    v = vids[0]
    fmt = data.get("format") or {}
    fps = _frac(v.get("avg_frame_rate")) or _frac(v.get("r_frame_rate"))
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rot and (v.get("tags") or {}).get("rotate"):
        try:
            rot = int((v.get("tags") or {})["rotate"])
        except ValueError:
            pass
    dur = None
    for d in (fmt.get("duration"), v.get("duration")):
        try:
            dur = float(d)
            break
        except (TypeError, ValueError):
            continue
    tags = {k.lower(): val for k, val in (fmt.get("tags") or {}).items()}
    return SourceInfo(
        width=int(v.get("width") or 0),
        height=int(v.get("height") or 0),
        fps=fps,
        duration=dur,
        codec=v.get("codec_name") or "",
        stream_index=int(v.get("index") or 0),
        sar=_frac(v.get("sample_aspect_ratio")) or Fraction(1),
        rotation=rot % 360,
        interlaced=(v.get("field_order") or "") in ("tt", "bb", "tb", "bt"),
        color_space=v.get("color_space") or "",
        color_range=v.get("color_range") or "",
        color_transfer=v.get("color_transfer") or "",
        pix_fmt=v.get("pix_fmt") or "",
        comment=tags.get("comment"),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def ffprobe_source(path: Path, ffprobe: str = "ffprobe") -> SourceInfo:
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise PrepareError(f"{ffprobe} not found - install ffmpeg") from None
    except subprocess.TimeoutExpired:
        raise PrepareError("ffprobe timed out") from None
    if r.returncode != 0:
        raise PrepareError(f"not a readable video ({r.stderr.strip() or 'ffprobe failed'})")
    try:
        return source_info_from_ffprobe(json.loads(r.stdout or "{}"))
    except json.JSONDecodeError:
        raise PrepareError("ffprobe returned invalid JSON") from None


# ------------------------------------------------------------------ decisions


def fmt_fps(f: Fraction) -> str:
    return f"{round(float(f), 3):g}"


def _fps_arg(f: Fraction) -> str:
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


def _snap(f: Fraction, rates=_STD_RATES) -> Optional[Fraction]:
    best = min(rates, key=lambda r: abs(float(f) - float(r)))
    return best if abs(float(f) - float(best)) / float(best) < _RATE_TOL else None


def parse_fps_option(s: str) -> str | Fraction:
    s = str(s).strip().lower()
    if s in ("auto", "keep"):
        return s
    f = _frac(s)
    if f is None:
        try:
            f = Fraction(s).limit_denominator(1001)
        except (ValueError, ZeroDivisionError):
            f = None
    if f is None or not (1 <= f <= 120):
        raise argparse.ArgumentTypeError(f"invalid fps: {s!r} (use auto, keep or e.g. 25, 30, 29.97)")
    return _snap(f) or f


def choose_fps(src: Optional[Fraction], policy: str | Fraction = "auto") -> Fraction:
    """Output frame rate. ``auto``: keep 23.976/24/25/29.97/30, halve 48/50/59.94/60,
    anything else -> 30. ``keep``: source rate (constant). A Fraction: that rate."""
    if isinstance(policy, Fraction):
        return policy
    if src is None:
        return _DEFAULT_RATE
    std = _snap(src)
    if policy == "keep":
        return std or src.limit_denominator(1001)
    if std:
        return std
    half = _snap(src / 2)
    if half:
        return half
    return _DEFAULT_RATE


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def target_size(info: SourceInfo, max_h: int = 1080, max_w: Optional[int] = None) -> tuple[int, int]:
    """Fit the display size (SAR, rotation applied) into max_w x max_h, keep aspect,
    even dimensions, never upscale."""
    if max_w is None:
        max_w = _even(max_h * 16 / 9)
    dw = info.width * float(info.sar)
    dh = float(info.height)
    if info.rotation in (90, 270):
        dw, dh = dh, dw
    if dw <= 0 or dh <= 0:
        raise PrepareError("video has no size")
    s = min(1.0, max_w / dw, max_h / dh)
    if s >= 1.0:
        return max(2, int(dw) // 2 * 2), max(2, int(dh) // 2 * 2)
    return min(max_w, _even(dw * s)), min(max_h, _even(dh * s))


def _in_matrix(info: SourceInfo) -> str:
    cs = info.color_space
    if cs in ("bt709",):
        return "bt709"
    if cs in ("bt470bg", "smpte170m", "bt601"):
        return "bt601"
    if cs in ("bt2020nc", "bt2020c"):
        return "bt2020"
    if cs == "smpte240m":
        return "smpte240m"
    if cs == "fcc":
        return "fcc"
    return "bt709" if info.height >= 720 or info.width >= 1280 else "bt601"


def _in_range(info: SourceInfo) -> str:
    if info.color_range in ("pc", "jpeg") or info.pix_fmt.startswith("yuvj"):
        return "pc"
    return "tv"


@dataclass
class Options:
    fps: str | Fraction = "auto"
    height: int = 1080
    gop_s: float = 0.5
    crf: int = 18
    preset: str = "slow"
    codec: str = "h264"  # h264 | hevc
    keep_audio: bool = False
    maxrate_mbps: float = 20.0
    ffmpeg: str = "ffmpeg"


@dataclass
class Plan:
    src: Path
    dst: Path
    width: int
    height: int
    fps: Fraction = Fraction(0)  # images: 0
    gop_frames: int = 0
    marker: str = ""
    cmd: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def output_path(src: Path, outdir: Optional[Path] = None) -> Path:
    """``<outdir or src dir>/<stem>.mp4``, or ``<stem>.prepared.mp4`` if that is the input."""
    src = Path(src)
    d = Path(outdir) if outdir else src.parent
    dst = d / (src.stem + ".mp4")
    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = False
    if same:
        dst = d / (src.stem + ".prepared.mp4")
    return dst


def is_image_file(path: Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_INPUT_EXTS


def expand_inputs(names: list[str]) -> list[Path]:
    """Files as given; folders searched recursively for videos and images (sorted per folder),
    skipping hidden files/folders and ``prepared`` folders (earlier output)."""
    out: list[Path] = []
    for name in names:
        p = Path(name)
        if not p.is_dir():
            out.append(p)  # missing files are reported when processed
            continue
        found = []
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != PREPARED_DIR)
            for f in files:
                ext = Path(f).suffix.lower()
                if not f.startswith(".") and (ext in IMAGE_INPUT_EXTS or ext in VIDEO_INPUT_EXTS):
                    found.append(Path(root) / f)
        out.extend(sorted(found, key=lambda f: str(f).lower()))
    return out


def planned_output(src: Path, outdir: Optional[Path] = None) -> Path:
    return image_output_path(src, outdir) if is_image_file(src) else output_path(src, outdir)


def image_output_path(src: Path, outdir: Optional[Path] = None) -> Path:
    """``<outdir or src dir>/<stem>.jpg`` (``.png`` for PNG inputs), or ``<stem>.prepared.<ext>``
    if that is the input."""
    src = Path(src)
    ext = ".png" if src.suffix.lower() == ".png" else ".jpg"
    d = Path(outdir) if outdir else src.parent
    dst = d / (src.stem + ext)
    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = False
    if same:
        dst = d / (src.stem + ".prepared" + ext)
    return dst


def build_image_plan(src: Path, dst: Path, info: SourceInfo, opts: Options, *,
                     tmp: Optional[Path] = None) -> Plan:
    """ffmpeg command line for one still image (pure function, tested exactly).

    The size is a fit-into-box filter, not computed here: ffmpeg applies the EXIF orientation
    while decoding, which ffprobe does not report, so only the filter sees the real orientation.
    width/height in the Plan are the expected size ignoring EXIF rotation (for messages).
    """
    max_h = opts.height
    max_w = _even(max_h * 16 / 9)
    if info.width <= 0 or info.height <= 0:
        raise PrepareError("image has no size")
    s = min(1.0, max_w / info.width, max_h / info.height)
    w, h = max(1, round(info.width * s)), max(1, round(info.height * s))
    png = Path(dst).suffix.lower() == ".png"
    notes: list[str] = []
    if s < 1.0:
        notes.append(f"scaling {info.width}x{info.height} to fit into {max_w}x{max_h}")
    if not png and info.codec != "mjpeg":
        notes.append(f"converting {info.codec or 'image'} to JPEG")
    vf = (f"scale=w='min(iw,{max_w})':h='min(ih,{max_h})':force_original_aspect_ratio=decrease"
          f":flags=lanczos,setsar=1")
    cmd = [opts.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(src), "-map", f"0:{info.stream_index}", "-map_metadata", "-1",
           "-frames:v", "1", "-vf", vf]
    if png:
        cmd += ["-c:v", "png"]
    else:
        cmd += ["-c:v", "mjpeg", "-q:v", str(JPEG_QSCALE), "-pix_fmt", "yuvj420p"]
    cmd += ["-update", "1", "-f", "image2", str(tmp or dst)]
    return Plan(src=Path(src), dst=Path(dst), width=w, height=h, cmd=cmd, notes=notes)


def build_plan(src: Path, dst: Path, info: SourceInfo, opts: Options, *, tmp: Optional[Path] = None,
               progress: bool = True) -> Plan:
    """The complete ffmpeg command line for one file (pure function, tested exactly)."""
    if opts.codec not in ("h264", "hevc"):
        raise PrepareError(f"unknown codec {opts.codec}")
    fps = choose_fps(info.fps, opts.fps)
    w, h = target_size(info, opts.height)
    gop = max(1, int(round(opts.gop_s * float(fps))))
    gop_s = float(f"{opts.gop_s:g}")
    marker = make_marker(gop_s, fps)
    notes: list[str] = []

    vf = []
    if info.interlaced:
        vf.append("bwdif=mode=send_frame")
        notes.append("interlaced source: deinterlacing")
    vf.append(f"fps={_fps_arg(fps)}")
    vf.append(
        f"scale={w}:{h}:flags=lanczos:in_color_matrix={_in_matrix(info)}:out_color_matrix=bt709"
        f":in_range={_in_range(info)}:out_range=tv"
    )
    vf.append("setsar=1")
    vf.append("format=yuv420p")
    if info.color_transfer in ("smpte2084", "arib-std-b67"):
        notes.append("HDR source: no tone mapping, colours will look flat - export SDR (BT.709) from your editor")
    if info.fps and fps != (_snap(info.fps) or info.fps):
        notes.append(f"frame rate {fmt_fps(info.fps)} -> {fmt_fps(fps)} fps")
    if (w, h) != (info.width, info.height):
        notes.append(f"scaling {info.width}x{info.height} -> {w}x{h}")

    maxrate = f"{opts.maxrate_mbps:g}M"
    bufsize = f"{opts.maxrate_mbps * 2:g}M"
    cmd = [opts.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += ["-i", str(src), "-map", f"0:{info.stream_index}"]
    if opts.keep_audio:
        cmd += ["-map", "0:a:0?"]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn"]
    cmd += ["-vf", ",".join(vf)]
    if opts.codec == "h264":
        cmd += [
            "-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf),
            "-profile:v", "high", "-level:v", H264_LEVEL,
            "-maxrate", maxrate, "-bufsize", bufsize,
            "-bf", "2", "-b-pyramid", "none", "-flags", "+cgop",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-forced-idr", "1",
        ]
    else:
        level = HEVC_LEVEL.replace(".", "")
        x265 = ":".join([
            f"keyint={gop}", f"min-keyint={gop}", "scenecut=0", "open-gop=0", "bframes=2", "b-pyramid=0",
            f"level-idc={level}", f"vbv-maxrate={int(opts.maxrate_mbps * 1000)}",
            f"vbv-bufsize={int(opts.maxrate_mbps * 2000)}", "log-level=error",
        ])
        cmd += [
            "-c:v", "libx265", "-preset", opts.preset, "-crf", str(opts.crf),
            "-profile:v", "main", "-tag:v", "hvc1",
            "-g", str(gop), "-keyint_min", str(gop),
            "-forced-idr", "1",
            "-x265-params", x265,
        ]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
    ]
    if opts.keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-metadata", f"comment={marker}", "-f", "mp4", str(tmp or dst)]
    return Plan(src=Path(src), dst=Path(dst), width=w, height=h, fps=fps, gop_frames=gop, marker=marker,
                cmd=cmd, notes=notes)


# ------------------------------------------------------------------ running ffmpeg


def _fmt_t(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def run_ffmpeg(cmd: list[str], duration: Optional[float], label: str = "",
               out=sys.stderr, cancel: Optional[Callable[[], bool]] = None) -> None:
    """Run ffmpeg with ``-progress pipe:1`` and print a progress line."""
    tty = hasattr(out, "isatty") and out.isatty()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        raise PrepareError(f"{cmd[0]} not found - install ffmpeg") from None
    err_lines: list[str] = []

    def _drain():
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line.rstrip())

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    last_print = 0.0
    kv: dict = {}
    t0 = time.monotonic()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            kv[k] = v
            if k != "progress":
                continue
            t = None
            for key, div in (("out_time_us", 1e6), ("out_time_ms", 1e6)):  # out_time_ms is us too
                try:
                    t = int(kv.get(key, "")) / div
                    break
                except ValueError:
                    continue
            now = time.monotonic()
            if v != "end" and now - last_print < (0.5 if tty else 10.0):
                continue
            last_print = now
            pct = f"{min(100.0, 100 * t / duration):5.1f}%" if (t is not None and duration) else "   ?  "
            speed = kv.get("speed", "").strip() or "?"
            eta = ""
            if t and duration and t > 0:
                rem = (now - t0) * (duration - t) / t
                eta = f" eta {_fmt_t(rem)}"
            msg = f"  {label} {pct} {_fmt_t(t or 0)}/{_fmt_t(duration or 0)} speed {speed}{eta}"
            if tty:
                out.write("\r" + msg.ljust(78)[:120])
            else:
                out.write(msg + "\n")
            out.flush()
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    th.join(2)
    if tty:
        out.write("\n")
    if proc.returncode != 0:
        tail = "\n".join(err_lines[-15:])
        raise PrepareError(f"ffmpeg failed (exit {proc.returncode})" + (f":\n{tail}" if tail else ""))


# ------------------------------------------------------------------ upload


def read_password(password_file: Optional[str]) -> Optional[str]:
    if password_file:
        try:
            return Path(password_file).expanduser().read_text().strip("\r\n")
        except OSError as e:
            raise PrepareError(f"cannot read password file: {e}") from None
    return os.environ.get("PROJEKTOR_PASSWORD")


class Uploader:
    """Upload to the projector web API (``POST /api/login``, ``PUT /api/upload``)."""

    def __init__(self, base_url: str, password: Optional[str], timeout: float = 60.0):
        base_url = base_url.strip().rstrip("/")
        if "://" not in base_url:
            base_url = "http://" + base_url
        self.base = base_url
        self.password = password
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._logged_in = False

    def _err(self, e: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(e.read().decode() or "{}")
            return str(body.get("detail") or body)
        except Exception:
            return e.reason or str(e.code)

    def login(self) -> None:
        req = urllib.request.Request(
            self.base + "/api/login", data=json.dumps({"password": self.password or ""}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                hint = "" if self.password else " (no password given: use --password-file or PROJEKTOR_PASSWORD)"
                raise PrepareError(f"login failed: {self._err(e)}{hint}") from None
            raise PrepareError(f"login failed: HTTP {e.code} {self._err(e)}") from None
        except urllib.error.URLError as e:
            raise PrepareError(f"cannot reach {self.base}: {e.reason}") from None
        self._logged_in = True

    def upload(self, path: Path, name: Optional[str] = None, overwrite: bool = False,
               out=sys.stderr) -> dict:
        if not self._logged_in:
            self.login()
        path = Path(path)
        size = path.stat().st_size
        name = name or path.name
        q = urllib.parse.urlencode({"name": name, "overwrite": "true" if overwrite else "false"})
        tty = hasattr(out, "isatty") and out.isatty()

        class _Reader:
            def __init__(self, f):
                self.f, self.done, self.t0, self.last = f, 0, time.monotonic(), 0.0

            def read(self, n=-1):
                b = self.f.read(min(n, 1 << 20) if n and n > 0 else 1 << 20)
                self.done += len(b)
                now = time.monotonic()
                if now - self.last > (0.5 if tty else 10.0) or self.done == size:
                    self.last = now
                    rate = self.done / max(now - self.t0, 1e-6) / 1e6
                    msg = f"  upload {name}: {100 * self.done / max(size, 1):5.1f}% {rate:.1f} MB/s"
                    out.write(("\r" + msg.ljust(70)) if tty else msg + "\n")
                    out.flush()
                return b

        with open(path, "rb") as f:
            req = urllib.request.Request(
                f"{self.base}/api/upload?{q}", data=_Reader(f), method="PUT",
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            )
            try:
                with self.opener.open(req, timeout=max(self.timeout, 600)) as r:
                    res = json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    raise PrepareError(f"{name} already exists on the projector (use --overwrite)") from None
                raise PrepareError(f"upload failed: HTTP {e.code} {self._err(e)}") from None
            except urllib.error.URLError as e:
                raise PrepareError(f"upload failed: {e.reason}") from None
            finally:
                if tty:
                    out.write("\n")
        return res


# ------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="projektor-prepare",
        description="Transcode videos into the format the projector (Raspberry Pi) plays best: "
        "H.264 High <= 1080p, constant frame rate, keyframe every 0.5 s, no audio. "
        "Images (jpg, png, webp, heic, tif, ...) are rotated upright and shrunk to fit 1920x1080 "
        "(JPEG; PNG stays PNG).",
        epilog="Password for --upload: --password-file FILE or env PROJEKTOR_PASSWORD. "
        "Details: docs/videos.md",
    )
    p.add_argument("inputs", nargs="+", metavar="INPUT",
                   help="video and/or image files, or folders (searched recursively for videos and images)")
    p.add_argument("-o", "--outdir", help="output directory (default: next to the input; "
                   f"for a single folder: <folder>/{PREPARED_DIR})")
    p.add_argument("--fps", type=parse_fps_option, default="auto",
                   help="output frame rate: auto (default: keep 24/25/30, halve 50/60, else 30), keep, or a number")
    p.add_argument("--height", type=int, default=1080, help="maximum height (default 1080; width max 16:9 of it)")
    p.add_argument("--gop-s", type=float, default=0.5, help="keyframe interval in seconds (default 0.5)")
    p.add_argument("--crf", type=int, default=18, help="quality, lower = better/larger (default 18)")
    p.add_argument("--preset", default="slow", help="x264/x265 preset (default slow; faster: medium, fast)")
    p.add_argument("--codec", choices=["h264", "hevc"], default="h264",
                   help="h264 (default; Pi 4 + Pi 5) or hevc (Pi 5 only)")
    p.add_argument("--maxrate", type=float, default=20.0, metavar="MBIT", help="peak bitrate cap in Mbit/s (default 20)")
    p.add_argument("--keep-audio", action="store_true", help="keep the first audio track (AAC); the projector ignores audio")
    p.add_argument("--force", action="store_true", help="re-encode files already prepared / overwrite outputs")
    p.add_argument("--dry-run", action="store_true", help="only print the ffmpeg commands")
    p.add_argument("--upload", metavar="URL", help="upload results to the projector, e.g. http://projektor.local")
    p.add_argument("--password-file", metavar="FILE", help="file containing the web password (for --upload)")
    p.add_argument("--overwrite", action="store_true", help="replace files of the same name on the projector")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not 0.04 <= args.gop_s <= 10:
        print("--gop-s must be between 0.04 and 10", file=sys.stderr)
        return 2
    if args.height < 144 or args.height > 2160:
        print("--height must be between 144 and 2160", file=sys.stderr)
        return 2
    opts = Options(fps=args.fps, height=args.height, gop_s=args.gop_s, crf=args.crf, preset=args.preset,
                   codec=args.codec, keep_audio=args.keep_audio, maxrate_mbps=args.maxrate)
    ffprobe = "ffprobe"
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"{tool} not found. Install ffmpeg (Debian/Ubuntu: sudo apt install ffmpeg, "
                  "macOS: brew install ffmpeg).", file=sys.stderr)
            return 2
    uploader = None
    if args.upload:
        try:
            uploader = Uploader(args.upload, read_password(args.password_file))
            if not args.dry_run:
                uploader.login()
        except PrepareError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    outdir = Path(args.outdir) if args.outdir else None
    if outdir is None and len(args.inputs) == 1 and Path(args.inputs[0]).is_dir():
        outdir = Path(args.inputs[0]) / PREPARED_DIR
    inputs = expand_inputs(args.inputs)
    if not inputs:
        print(f"no videos or images found in {' '.join(args.inputs)}", file=sys.stderr)
        return 1
    if len(inputs) != len(args.inputs) or any(Path(a).is_dir() for a in args.inputs):
        print(f"{len(inputs)} file(s)" + (f" -> {outdir}" if outdir else ""))
    if outdir and not args.dry_run:
        outdir.mkdir(parents=True, exist_ok=True)

    failed = 0
    n = len(inputs)
    planned: dict[Path, Path] = {}  # output -> input, catches e.g. a.heic + a.jpg -> a.jpg
    for i, src in enumerate(inputs, 1):
        tag = f"[{i}/{n}] {src.name}"
        try:
            dst = planned_output(src, outdir)
            other = planned.setdefault(dst, src)
            if other != src:
                raise PrepareError(f"output {dst.name} is also written for {other} - rename one of them")
            result = _process_one(src, outdir, opts, args, ffprobe, tag)
            if result and uploader:
                if args.dry_run:
                    print(f"{tag}: would upload {result.name} to {uploader.base}")
                else:
                    res = uploader.upload(result, overwrite=args.overwrite)
                    vid = res.get("video_id")
                    print(f"{tag}: uploaded as {res.get('file')}" + (f" (video \"{vid}\")" if vid else ""))
                    for w in res.get("warnings") or []:
                        print(f"  projector warns: {w}")
        except PrepareError as e:
            failed += 1
            print(f"{tag}: error: {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
    return 1 if failed else 0


def _process_one(src: Path, outdir: Optional[Path], opts: Options, args, ffprobe: str, tag: str) -> Optional[Path]:
    """Transcode one file; returns the file to upload (output, or the input if it is already prepared)."""
    if not src.is_file():
        raise PrepareError("no such file")
    if is_image_file(src):
        return _process_image(src, outdir, opts, args, ffprobe, tag)
    info = ffprobe_source(src, ffprobe)
    if info.marker and not args.force:
        print(f"{tag}: already prepared ({info.comment}), skipping (--force to re-encode)")
        return src
    dst = output_path(src, outdir)
    if dst.exists() and not args.force:
        try:
            out_info = ffprobe_source(dst, ffprobe)
        except PrepareError:
            out_info = None
        if out_info and out_info.marker:
            print(f"{tag}: output {dst} exists and is prepared, skipping (--force to redo)")
            return dst
        raise PrepareError(f"output {dst} exists and is not a prepared file (use --force or -o DIR)")
    tmp = dst.with_name(f".{dst.stem}.part.mp4")
    plan = build_plan(src, dst, info, opts, tmp=tmp)
    if args.dry_run:
        print(f"{tag} -> {dst}")
        for note in plan.notes:
            print(f"  {note}")
        cmd = list(plan.cmd)
        cmd[-1] = str(dst)
        print("  " + shlex.join(cmd))
        return dst
    print(f"{tag} -> {dst.name}: {plan.width}x{plan.height} {fmt_fps(plan.fps)} fps, "
          f"{opts.codec}, keyframe every {opts.gop_s:g} s ({plan.gop_frames} frames)")
    for note in plan.notes:
        print(f"  {note}")
    t0 = time.monotonic()
    try:
        run_ffmpeg(plan.cmd, info.duration, label=src.name)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    size = dst.stat().st_size
    dt = time.monotonic() - t0
    print(f"{tag}: done in {_fmt_t(dt)}, {size / 1e6:.1f} MB -> {dst}")
    return dst


def _process_image(src: Path, outdir: Optional[Path], opts: Options, args, ffprobe: str, tag: str) -> Optional[Path]:
    """Convert one still image; returns the file to upload."""
    info = ffprobe_source(src, ffprobe)
    dst = image_output_path(src, outdir)
    if dst.exists() and not args.force:
        print(f"{tag}: output {dst} exists, skipping (--force to redo)")
        return dst
    tmp = dst.with_name(f".{dst.stem}.part{dst.suffix}")
    plan = build_image_plan(src, dst, info, opts, tmp=tmp)
    if args.dry_run:
        print(f"{tag} -> {dst}")
        for note in plan.notes:
            print(f"  {note}")
        cmd = list(plan.cmd)
        cmd[-1] = str(dst)
        print("  " + shlex.join(cmd))
        return dst
    print(f"{tag} -> {dst.name}: image, fit into {_even(opts.height * 16 / 9)}x{opts.height}")
    for note in plan.notes:
        print(f"  {note}")
    try:
        run_ffmpeg(plan.cmd, None, label=src.name)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        out = ffprobe_source(dst, ffprobe)
        size = f"{out.width}x{out.height}, "
    except PrepareError:
        size = ""
    print(f"{tag}: done, {size}{dst.stat().st_size / 1e6:.1f} MB -> {dst}")
    return dst


if __name__ == "__main__":
    sys.exit(main())
