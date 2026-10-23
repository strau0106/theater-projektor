"""Settings (TOML) and Show (JSON) models, loading, saving and validation.

See ARCHITECTURE.md §6 for the file formats.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from . import ProjektorError

log = logging.getLogger(__name__)

__all__ = [
    "ProjektorError",
    "Settings",
    "Show",
    "Video",
    "Cue",
    "Clip",
    "ShowStore",
    "load_settings",
    "resolve_cue_ref",
    "resolve_cue",
    "resolve_video",
    "is_image",
    "IMAGE_EXTS",
    "DEV_CONFIG",
]

# app/dev/projektor.toml (only meaningful in a source checkout)
DEV_CONFIG = Path(__file__).resolve().parents[2] / "dev" / "projektor.toml"

EndMode = Literal["black", "hold", "loop"]
CueType = Literal["video", "results"]
SinkMode = Literal["auto", "gl", "kms", "window"]
# Fade shapes (player/base.py fade_value): linear alpha, smoothstep, linear in perceived lightness.
FadeCurve = Literal["linear", "scurve", "perceptual"]

# Still images (ARCHITECTURE.md §4): decoders on the Pi image: jpegdec, pngdec (good), webpdec (bad).
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def is_image(file: str | os.PathLike) -> bool:
    """True if `file` (a media file name or path) is a still image, by its extension."""
    return PurePosixPath(os.fspath(file)).suffix.lower() in IMAGE_EXTS


# --------------------------------------------------------------------------- settings


class _Section(BaseModel):
    # Unknown keys are kept (model_extra) so other components can add their own settings.
    model_config = ConfigDict(extra="allow")


class DeviceSettings(_Section):
    name: str = "projektor"


class PathSettings(_Section):
    data_dir: Path = Path("/var/lib/projektor")
    net_status: Path = Path("/run/projektor/net.json")
    tunnel_status: Path = Path("/run/projektor/tunnel.json")


class DisplaySettings(_Section):
    sink: SinkMode = "auto"
    status_on_boot: bool = True
    width: int = Field(1920, gt=0)
    height: int = Field(1080, gt=0)
    # Optional, not in the §6 example: output frame rate of the display pipeline.
    fps: int = Field(30, gt=0, le=120)
    # Default fade shape; videos/cues may override it with `fade_curve` in show.json.
    fade_curve: FadeCurve = "scurve"
    # GL output connector on the Pi (GBM/KMS), e.g. "HDMI-A-2" for HDMI1; "" = first connected.
    connector: str = ""


class PlayerSettings(_Section):
    # Preroll the standby cue in a second clip pipeline so GO starts within ~1 frame.
    # Costs a second decoder instance + its frame buffers (CMA on the Pi) while playing.
    # "auto": only with >= 1.5 GB RAM (Pi 4 1 GB measured CmaFree ~1 MB with preload on).
    preload: bool | Literal["auto"] = "auto"

    def preload_enabled(self) -> bool:
        if self.preload != "auto":
            return bool(self.preload)
        return _mem_total_kb() >= PRELOAD_AUTO_MIN_KB


PRELOAD_AUTO_MIN_KB = 1_500_000


def _mem_total_kb() -> int:
    """MemTotal from /proc/meminfo; large value if unknown (non-Linux dev machines)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 1 << 40


class OscSettings(_Section):
    port: int = Field(9000, ge=0, le=65535)
    reply_host: str = ""
    reply_port: int = Field(0, ge=0, le=65535)
    dedup_window_s: float = Field(1.0, ge=0)


class LoraSettings(_Section):
    enabled: bool = True
    device: str = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_*"
    baud: int = 115200
    # LoRa is half duplex: the bridge sends each command `repeat` times, a reply sent while
    # the other copies are still on air is lost. Replies wait this long plus the airtime of
    # the received frame (x repeats) before going out.
    reply_delay_ms: int = Field(250, ge=0, le=5000)
    sender_repeat: int = Field(2, ge=1, le=5)


class WebSettings(_Section):
    port: int = Field(80, ge=0, le=65535)
    password_hash: str = ""
    max_upload_mb: int = 8000


class BeamerSettings(_Section):
    """HDMI-CEC control of the beamer (docs/beamer-control.md)."""
    enabled: bool = True
    device: str = "/dev/cec0"
    phys_addr: str = "1.0.0.0"  # our CEC physical address; "" = auto (from EDID)
    power_on_at_boot: bool = False
    standby_at_shutdown: bool = False
    cec_ctl: str = "cec-ctl"
    off_method: Literal["power-key", "standby"] = "power-key"  # power-key = sleep on XGIMI


class BackupSettings(_Section):
    enabled: bool = True
    host: str = "INFMK1_HOST"
    port: int = 22
    user: str = "projbackup"
    key: Path = Path("/var/lib/projektor/.ssh/id_backup")
    remote_dir: str = ""
    auto: bool = True          # instant backup after uploads / show edits (debounced)
    auto_delay_s: float = Field(10.0, ge=0)


class NotifySettings(_Section):
    """Discord notifications (notify.py); empty webhook = disabled."""
    discord_webhook: str = ""
    mention: str = ""          # e.g. "<@123456789>"; prefixed to warning/error messages
    temp_warn_c: float = 75.0
    temp_hysteresis_c: float = 5.0
    min_interval_s: float = Field(30.0, ge=0)   # rate limit per event type


class ResultsSettings(_Section):
    """Audience-vote results graph (docs/results.md)."""
    design_url: str = ""       # design manifest URL for /proj/design/fetch
    in_s: float = Field(1.3, ge=0)    # overlay fade + fade-in of one paragraph
    out_s: float = Field(0.8, ge=0)   # total out-animation
    para_gap_s: float = Field(2.5, ge=0)  # start of one paragraph to the start of the next


class Settings(_Section):
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    player: PlayerSettings = Field(default_factory=PlayerSettings)
    osc: OscSettings = Field(default_factory=OscSettings)
    lora: LoraSettings = Field(default_factory=LoraSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    beamer: BeamerSettings = Field(default_factory=BeamerSettings)
    results: ResultsSettings = Field(default_factory=ResultsSettings)

    # Not read from TOML: where the settings came from, and whether we run in PC dev mode.
    config_path: Path | None = Field(default=None, exclude=True)
    dev: bool = Field(default=False, exclude=True)

    @property
    def show_path(self) -> Path:
        return self.paths.data_dir / "show.json"

    @property
    def media_dir(self) -> Path:
        return self.paths.data_dir / "media"

    @classmethod
    def from_toml(cls, text: str, base_dir: Path | None = None) -> "Settings":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ProjektorError(f"settings: invalid TOML: {e}") from e
        data.pop("config_path", None)
        data.pop("dev", None)
        try:
            s = cls.model_validate(data)
        except ValidationError as e:
            raise ProjektorError(f"settings: {_fmt_validation(e)}") from e
        if base_dir is not None:
            s._resolve_relative(base_dir)
        return s

    @classmethod
    def load(cls, path: str | os.PathLike, dev: bool = False) -> "Settings":
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise ProjektorError(f"settings: cannot read {p}: {e}") from e
        s = cls.from_toml(text, base_dir=p.resolve().parent)
        s.config_path = p
        s.dev = dev
        return s

    def _resolve_relative(self, base: Path) -> None:
        """Relative paths in the TOML are relative to the TOML file's directory."""

        def fix(p: Path) -> Path:
            p = p.expanduser()
            return p if p.is_absolute() else (base / p).resolve()

        self.paths.data_dir = fix(self.paths.data_dir)
        self.paths.net_status = fix(self.paths.net_status)
        self.paths.tunnel_status = fix(self.paths.tunnel_status)
        self.backup.key = fix(self.backup.key)


def load_settings(path: str | os.PathLike | None = None, dev: bool = False) -> Settings:
    """Load settings; with dev=True and no path, use app/dev/projektor.toml."""
    if path is None:
        if dev:
            path = DEV_CONFIG
        else:
            path = Path("/etc/projektor/projektor.toml")
    return Settings.load(path, dev=dev)


# --------------------------------------------------------------------------- show


def _check_times(values: list[float] | None, what: str) -> list[float] | None:
    if values is None:
        return None
    out = sorted({float(v) for v in values})
    if out and out[0] < 0:
        raise ValueError(f"{what}: negative time {out[0]}")
    return out


class Video(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str
    title: str = ""
    in_point: float = 0.0
    out_point: float | None = None
    pause_points: list[float] = Field(default_factory=list)
    fade_in: float = 1.0
    fade_out: float = 2.0
    end: EndMode = "black"
    fade_curve: FadeCurve | None = None  # null = settings display.fade_curve

    @property
    def image(self) -> bool:
        return is_image(self.file)

    @field_validator("pause_points")
    @classmethod
    def _pp(cls, v: list[float]) -> list[float]:
        return _check_times(v, "pause_points") or []

    @field_validator("file")
    @classmethod
    def _file(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("file must not be empty")
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or "\\" in v:
            raise ValueError(f"file must be a path relative to the media dir: {v!r}")
        return v

    @model_validator(mode="after")
    def _times(self) -> "Video":
        if self.in_point < 0:
            raise ValueError("in_point must be >= 0")
        if self.out_point is not None and self.out_point <= self.in_point:
            raise ValueError("out_point must be > in_point")
        if self.fade_in < 0 or self.fade_out < 0:
            raise ValueError("fade times must be >= 0")
        if self.out_point is not None and self.pause_points and self.pause_points[-1] > self.out_point:
            raise ValueError(f"pause point {self.pause_points[-1]} after out_point {self.out_point}")
        return self


class Cue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    type: CueType = "video"  # "results": audience-vote graph, no video (docs/results.md)
    video: str = ""
    fade_in: float | None = None
    fade_out: float | None = None
    pause_points: list[float] | None = None
    end: EndMode | None = None
    start_at: float | None = None
    fade_curve: FadeCurve | None = None  # null = inherit from the video

    @field_validator("id", "video", mode="before")
    @classmethod
    def _str(cls, v: Any, info: ValidationInfo) -> Any:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(int(v)) if float(v).is_integer() else str(v)
        if v is None:
            v = ""
        if isinstance(v, str):
            v = v.strip()
            if not v and info.field_name == "id":
                raise ValueError("must not be empty")
        return v

    @field_validator("pause_points")
    @classmethod
    def _pp(cls, v: list[float] | None) -> list[float] | None:
        return _check_times(v, "pause_points")

    @model_validator(mode="after")
    def _times(self) -> "Cue":
        for name in ("fade_in", "fade_out", "start_at"):
            val = getattr(self, name)
            if val is not None and val < 0:
                raise ValueError(f"{name} must be >= 0")
        return self


class Show(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    videos: dict[str, Video] = Field(default_factory=dict)
    cues: list[Cue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> "Show":
        for vid in self.videos:
            if not vid or not vid.strip():
                raise ValueError("video id must not be empty")
        seen: set[str] = set()
        for i, c in enumerate(self.cues):
            if c.id in seen:
                raise ValueError(f"duplicate cue id {c.id!r}")
            seen.add(c.id)
            if c.type == "results":
                continue
            if not c.video:
                raise ValueError(f"cue {c.id!r} (#{i + 1}) needs a video")
            v = self.videos.get(c.video)
            if v is None:
                raise ValueError(f"cue {c.id!r} (#{i + 1}) references unknown video {c.video!r}")
            if v.out_point is not None and not v.image:  # images: timeline fields ignored
                if c.start_at is not None and c.start_at >= v.out_point:
                    raise ValueError(f"cue {c.id!r}: start_at after out_point of video {c.video!r}")
                if c.pause_points and c.pause_points[-1] > v.out_point:
                    raise ValueError(f"cue {c.id!r}: pause point after out_point of video {c.video!r}")
        return self

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def _fmt_validation(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_show(data: Any) -> Show:
    """Validate a show from a dict (or JSON text). Raises ProjektorError."""
    try:
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        return Show.model_validate(data)
    except json.JSONDecodeError as e:
        raise ProjektorError(f"show: invalid JSON: {e}") from e
    except ValidationError as e:
        raise ProjektorError(f"show: {_fmt_validation(e)}") from e


class ShowStore:
    """Reads/writes show.json (atomic + fsync) and knows the media directory."""

    def __init__(self, data_dir: str | os.PathLike):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "show.json"
        self.media_dir = self.data_dir / "media"
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ShowStore":
        return cls(settings.paths.data_dir)

    def load(self) -> Show:
        with self._lock:
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return Show()
            except OSError as e:
                raise ProjektorError(f"show: cannot read {self.path}: {e}") from e
        return parse_show(text)

    def save(self, show: Show) -> None:
        # Re-validate (the object may have been mutated after construction).
        show = parse_show(show.model_dump(mode="json"))
        data = show.to_json().encode("utf-8")
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".show-", suffix=".json.tmp", dir=self.data_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            _fsync_dir(self.data_dir)


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Clip:
    """Everything the player needs to play one video (cue fields resolved)."""

    video_id: str
    path: Path
    in_point: float = 0.0
    out_point: float | None = None
    start_at: float = 0.0  # absolute file time to start from (>= in_point)
    pause_points: tuple[float, ...] = ()  # only points after start_at
    fade_in: float = 0.0
    fade_out: float = 0.0
    end: EndMode = "black"
    cue_id: str | None = None
    cue_index: int | None = None
    title: str = field(default="", compare=False)
    fade_curve: FadeCurve | None = None  # None = player default (settings display.fade_curve)
    image: bool = False  # still image: one frame, always end="hold", no in/out/pause points


def resolve_cue_ref(show: Show, ref: str | int) -> int:
    """Return the 0-based index of cue `ref` (id string or 1-based number)."""
    if isinstance(ref, bool):
        raise ProjektorError(f"invalid cue ref {ref!r}")
    if isinstance(ref, float):
        if not ref.is_integer():
            raise ProjektorError(f"invalid cue ref {ref!r}")
        ref = int(ref)
    if isinstance(ref, int):
        if 1 <= ref <= len(show.cues):
            return ref - 1
        for i, c in enumerate(show.cues):  # lenient: numeric id not matching an index
            if c.id == str(ref):
                return i
        raise ProjektorError(f"unknown cue {ref} (show has {len(show.cues)} cues)")
    if isinstance(ref, str):
        r = ref.strip()
        for i, c in enumerate(show.cues):
            if c.id == r:
                return i
        try:
            n = int(r)
        except ValueError:
            n = None
        if n is not None and 1 <= n <= len(show.cues):
            return n - 1
        raise ProjektorError(f"unknown cue {ref!r}")
    raise ProjektorError(f"invalid cue ref {ref!r}")


def _make_clip(
    video_id: str,
    v: Video,
    media_dir: Path,
    fade_in: float | None = None,
    fade_out: float | None = None,
    pause_points: list[float] | None = None,
    end: str | None = None,
    start_at: float | None = None,
    cue_id: str | None = None,
    cue_index: int | None = None,
    fade_curve: str | None = None,
) -> Clip:
    image = v.image
    if image:  # one frame that stays until the next command; timeline fields don't apply
        in_point, out_point, start, pps, end_mode = 0.0, None, 0.0, (), "hold"
    else:
        in_point, out_point = v.in_point, v.out_point
        start = v.in_point if start_at is None else max(float(start_at), 0.0)
        pps = v.pause_points if pause_points is None else pause_points
        pps = tuple(p for p in pps if p > start + 1e-6 and (v.out_point is None or p <= v.out_point))
        end_mode = v.end if end is None else end
    return Clip(
        video_id=video_id,
        path=(media_dir / v.file),
        in_point=in_point,
        out_point=out_point,
        start_at=start,
        pause_points=pps,
        fade_in=v.fade_in if fade_in is None else float(fade_in),
        fade_out=v.fade_out if fade_out is None else float(fade_out),
        end=end_mode,  # type: ignore[arg-type]
        cue_id=cue_id,
        cue_index=cue_index,
        title=v.title,
        fade_curve=v.fade_curve if fade_curve is None else fade_curve,  # type: ignore[arg-type]
        image=image,
    )


def resolve_cue(show: Show, index: int, media_dir: Path) -> Clip:
    """Resolve cue at 0-based `index` into a Clip (null cue fields inherit from the video)."""
    try:
        c = show.cues[index]
    except IndexError:
        raise ProjektorError(f"unknown cue #{index + 1}") from None
    if c.type == "results":
        raise ProjektorError(f"cue {c.id!r} is a results cue (no clip)")
    v = show.videos.get(c.video)
    if v is None:
        raise ProjektorError(f"cue {c.id!r} references unknown video {c.video!r}")
    return _make_clip(
        c.video, v, media_dir,
        fade_in=c.fade_in, fade_out=c.fade_out, pause_points=c.pause_points,
        end=c.end, start_at=c.start_at, cue_id=c.id, cue_index=index, fade_curve=c.fade_curve,
    )


def resolve_video(show: Show, video_id: str, media_dir: Path, fade_in: float | None = None) -> Clip:
    v = show.videos.get(video_id)
    if v is None:
        raise ProjektorError(f"unknown video {video_id!r}")
    return _make_clip(video_id, v, media_dir, fade_in=fade_in)
