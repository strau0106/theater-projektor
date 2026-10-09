"""GStreamer player (ARCHITECTURE.md §4).

Two kinds of pipelines:

* **Display pipeline** (permanent, created once, never torn down while running): owns the
  video sink so there is never a flash of the console between clips.

      GL mode (sink gl / window / glfakesink):
        videotestsrc(black, 16x16, live) .................... glvideomixer.sink_0  (clock, alpha 0)
        appsrc(live) ........................................ glvideomixer.sink_1  (video, alpha = fade)
        glvideomixer(background=black) ! GL caps(W x H @ fps)
          ! textoverlay(status) ! gloverlaycompositor ! glimagesink

      CPU mode (sink kms, headless test sinks):
        intervideosrc ! videoconvert ! videoscale ! caps(W x H) ! videobalance(fade)
                      ! textoverlay(status) ! videoconvert ! kmssink

* **Clip pipeline** (one per playing video): playbin3 (video only). Pause/seek/EOS/position
  work on this pipeline independently of the display.

      GL mode:  video-sink = fakevideosink(sync, signal-handoffs). Every rendered (and the
                prerolled) frame is handed to the display appsrc as-is: zero copy, a Pi 4
                V4L2 decoder dmabuf is imported by the mixer's glupload as an EGLImage.
                The mixer keeps showing the last frame it got, so a paused / held clip
                stays on screen; black is produced via mixer alpha.
                Each frame travels with a segment that maps its file time to the display's
                running time (see _Display.push_video).
                Pi 5 (untested): the HEVC decoder outputs SAND/NC12 dmabufs; zero copy then
                depends on EGL import of that modifier in Mesa v3d.
      CPU mode: video-sink = queue ! videoconvert ! intervideosink(channel); intervideosrc
                repeats the last frame (CPU copies; only for the kms fallback and tests).

Why fakevideosink: it advertises GstVideoMeta in the allocation query. appsink and
intervideosink do not, so the V4L2 decoder cannot hand out its padded dmabufs and copies
every frame (from uncached CMA memory) into system memory, which the display then uploads
on the CPU - that was 80-110 % CPU on a Pi 4 for 1080p30 (the v0.1 player).

Status screen (GL): textoverlay on the mixer output only attaches an overlay composition
meta (no CPU blending), gloverlaycompositor draws it on the GPU. Not a mixer input: a
low-fps live status source would raise the mixer latency (it queued 15 RGBA 1080p frames).

Pause points: a blocking buffer probe on the clip sink pad lets the first frame with
stream time >= pause point through, blocks the following one and pauses the clip pipeline,
so the frame at/after the pause point stays on screen (frame accurate).

Loops (end=loop) use segment seeks (SEGMENT_DONE -> non-flushing seek to the in point), so
there is no flush and no gap at the loop point.

Fades (GL): the mixer pad's `alpha` is driven by a GstController (InterpolationControlSource
+ DirectControlBinding, control points in pipeline running time, ~1 per output frame), so
the curve is evaluated by the streaming thread and does not depend on the Python main loop
load. The state change at the end is a GLib timeout. Without GstController or in CPU mode a
16 ms GLib timer applies the same curve (base.fade_alpha).

Still images (clip.image): the same clip pipeline. playbin3 decodes the single frame, prerolls
it and posts EOS right after; image clips always have end="hold", so the frame stays on screen
(the mixer keeps the last frame) and fades work as for video. A playbin3 video-filter applies
the EXIF orientation and shrinks the picture to fit the display (never upscales: the GL mixer
scales up on the GPU, and a camera photo would exceed the Pi's 4096 px texture limit).

Preloading: a _Run is a self-contained clip pipeline; only the run in self._run feeds the
display (see _on_handoff). prepare(clip) builds ONE more run (self._prep), prerolled to
PAUSED at its start frame (accurate seek) but not connected to the display; the sink keeps
the prerolled sample. play(clip) with an equal clip promotes it: old run torn down, the
kept preroll frame pushed to the display, then the normal _begin path (PLAYING + fade).
GL mode only; in CPU mode (shared intervideosink channel) prepare() is a no-op.
"""

from __future__ import annotations

import logging
import os
from collections import deque
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

try:
    gi.require_version("GstController", "1.0")
    from gi.repository import GstController  # noqa: E402
except (ValueError, ImportError):  # pragma: no cover - depends on the GStreamer install
    GstController = None

from ..config import Clip, Settings  # noqa: E402
from .base import Player, compute_state, fade_alpha  # noqa: E402

log = logging.getLogger(__name__)

if not Gst.is_initialized():
    Gst.init(None)

# playbin3 flags: video (0x1) | native-video (0x40) -> no audio decoding, no extra converters
_PLAYBIN_FLAGS = 0x1 | 0x40
_PP_TOLERANCE = 0.001  # s; frame pts rounding
_FADE_TICK_MS = 16
_MAX_CTRL_POINTS = 3000
_POS_POLL_MS = 50
# CPU mode: raw formats passed to the display (system memory, intervideosink).
_CLIP_FORMATS = "{ I420, NV12, YV12, BGRA, RGBA, BGRx, RGBx }"
_MAX_U64 = 2**64 - 1
_HELD_FRAMES = 4  # GL: appsrc queue (2) + mixer pad queue + frame on screen
_RESYNC_NS = 50 * Gst.MSECOND  # GL: re-anchor the clip -> display time mapping beyond this


def resolve_sink(sink: str) -> str:
    """Map settings `display.sink` to a concrete sink kind."""
    if sink == "auto":
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            return "window"
        return "gl"
    return sink


def _prepare_gl_env(kind: str, connector: str = "") -> None:
    if kind == "gl":
        if connector:
            os.environ.setdefault("GST_GL_GBM_DRM_CONNECTOR", connector)
        # Pi without X/Wayland: EGL on GBM/KMS directly (fullscreen on the first connected
        # connector; set GST_GL_GBM_DRM_CONNECTOR=HDMI-A-1 in the service env to force one).
        os.environ.setdefault("GST_GL_WINDOW", "gbm")
        os.environ.setdefault("GST_GL_PLATFORM", "egl")
        os.environ.setdefault("GST_GL_API", "gles2")
        # Without this GstGL picks a 16-bit (RGB565) EGL config on the Pi 4: visible banding,
        # artifacts and wrong colours on the beamer (kmssink / mpv looked correct).
        os.environ.setdefault(
            "GST_GL_CONFIG", "gst-gl-context-config,red-size=8,green-size=8,blue-size=8,alpha-size=8")


class _Display:
    """The permanent output pipeline."""

    def __init__(self, kind: str, width: int, height: int, fps: int, channel: str, on_error):
        self.kind = kind  # gl | window | kms | fakesink | glfakesink
        self.gl = kind in ("gl", "window", "glfakesink")
        self.width = width
        self.height = height
        self.fps = fps
        self.channel = channel
        self._on_error = on_error
        self.pipeline: Gst.Pipeline | None = None
        self.sink: Gst.Element | None = None
        self._video_pad = None
        self._balance = None
        self._text = None
        self.vsrc: Gst.Element | None = None  # GL: appsrc fed by the clip pipeline
        self._vseg: Gst.Segment | None = None  # GL: maps clip timestamps to display time
        self._held: deque[Gst.Buffer] = deque(maxlen=_HELD_FRAMES)
        self._bus_watch = False
        self.alpha = 0.0
        self._binding = None  # GL: active alpha control binding
        self.status_visible = False
        self.status_text = ""

    @staticmethod
    def _textprops(w: int, h: int) -> str:
        # ~ 20 lines fit on screen
        size = max(8, int(h / 26))
        return (
            f'font-desc="DejaVu Sans Mono Bold {size}" valignment=top halignment=left line-alignment=left '
            f"xpad={int(w * 0.04)} ypad={int(h * 0.05)} auto-resize=false shaded-background=false "
            f"draw-shadow=false wait-text=false"
        )

    def build(self) -> None:
        W, H, F = self.width, self.height, self.fps
        textprops = self._textprops(W, H)
        self._vseg = None
        if self.gl:
            if self.kind == "glfakesink":
                sink = "gldownload ! videoconvert ! fakesink name=out sync=true enable-last-sample=true"
            else:
                # sync=false: the live mixer already paces output at the display framerate.
                # last-sample only while taking a snapshot: it pins one more 1080p texture.
                sink = "glimagesink name=out force-aspect-ratio=true enable-last-sample=false sync=false"
            desc = (
                f"glvideomixer name=mix background=black "
                f"! video/x-raw(memory:GLMemory),width={W},height={H},framerate={F}/1 "
                # status text: textoverlay only attaches an overlay meta, the GPU draws it
                f"! textoverlay name=stext {textprops} ! gloverlaycompositor "
                f"! video/x-raw(memory:GLMemory) ! {sink} "
                # tiny live source: keeps the mixer producing frames at F fps (latency 1/F)
                f"videotestsrc pattern=black is-live=true "
                f"! video/x-raw,format=RGBA,width=16,height=16,framerate={F}/1 ! mix.sink_0 "
                f"appsrc name=vsrc is-live=true format=time handle-segment-change=true min-latency=0 "
                f"max-buffers=2 max-bytes=0 leaky-type=downstream ! mix.sink_1 "
            )
        else:
            if self.kind == "kms":
                sink = "queue max-size-buffers=2 ! kmssink name=out enable-last-sample=true"
            else:
                sink = "queue max-size-buffers=2 ! fakesink name=out sync=true enable-last-sample=true"
            desc = (
                f"intervideosrc name=vsrc channel={self.channel} timeout={_MAX_U64} "
                f"! videoconvert ! videoscale "
                f"! video/x-raw,width={W},height={H},pixel-aspect-ratio=1/1 "
                f"! videobalance name=bal contrast=0 saturation=0 "
                f"! textoverlay name=stext {textprops} ! videoconvert ! {sink}"
            )
        log.info("display pipeline (%s): %s", self.kind, desc)
        p = Gst.parse_launch(desc)
        self.pipeline = p
        self.sink = p.get_by_name("out")
        self._text = p.get_by_name("stext")
        if self.gl:
            mix = p.get_by_name("mix")
            clock_pad = mix.get_static_pad("sink_0")
            clock_pad.set_property("alpha", 0.0)
            self._video_pad = mix.get_static_pad("sink_1")
            self._video_pad.set_property("zorder", 1)
            self._video_pad.set_property("width", W)
            self._video_pad.set_property("height", H)
            self._video_pad.set_property("sizing-policy", 1)  # keep-aspect-ratio (letterbox)
            self.vsrc = p.get_by_name("vsrc")
        else:
            self._balance = p.get_by_name("bal")
        self.set_video_alpha(self.alpha)
        self.set_status(self.status_visible, self.status_text)
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self._bus_watch = True
        ret = p.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"display pipeline ({self.kind}) failed to start")

    def destroy(self) -> None:
        p = self.pipeline
        if p is None:
            return
        self.pipeline = None
        self.vsrc = None
        self._binding = None
        self._held.clear()
        if self._bus_watch:
            p.get_bus().remove_signal_watch()
            self._bus_watch = False
        p.set_state(Gst.State.NULL)

    def _on_message(self, bus, msg) -> None:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("display error from %s: %s (%s)", msg.src.get_name(), err.message, dbg)
            self._on_error(err.message)
        elif t == Gst.MessageType.WARNING:
            w, dbg = msg.parse_warning()
            log.warning("display warning from %s: %s", msg.src.get_name(), w.message)

    def current_alpha(self) -> float:
        """Alpha now (reads the pad while a controller fade runs)."""
        if self._binding is not None and self._video_pad is not None:
            return float(self._video_pad.get_property("alpha"))
        return self.alpha

    def clear_fade(self) -> None:
        """Remove a running controller fade, freezing alpha at its current value."""
        b = self._binding
        if b is None:
            return
        self._binding = None
        if self._video_pad is not None:
            a = float(self._video_pad.get_property("alpha"))
            self._video_pad.remove_control_binding(b)
            self._video_pad.set_property("alpha", a)
            self.alpha = a

    def ctrl_fade(self, a0: float, target: float, seconds: float, curve: str) -> bool:
        """GL: fade alpha a0 -> target in `seconds` with a GstController. False if unsupported."""
        p = self.pipeline
        clock = p.get_clock() if p is not None else None
        if not self.gl or GstController is None or clock is None or self._video_pad is None:
            return False
        try:
            self.clear_fade()
            self._video_pad.set_property("alpha", a0)
            cs = GstController.InterpolationControlSource()
            cs.set_property("mode", GstController.InterpolationMode.LINEAR)
            b = GstController.DirectControlBinding.new(self._video_pad, "alpha", cs)
            self._video_pad.add_control_binding(b)
            self._binding = b
            now = clock.get_time() - p.get_base_time()
            n = 1 if curve == "linear" else min(_MAX_CTRL_POINTS, max(2, int(seconds * self.fps)))
            cs.set(now, a0)
            for i in range(1, n + 1):
                t = i / n
                cs.set(now + int(t * seconds * Gst.SECOND), fade_alpha(curve, t, a0, target))
            self.alpha = a0
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("controller fade unavailable (%s); using timer", e)
            self._binding = None
            return False

    def set_video_alpha(self, a: float) -> None:
        a = min(1.0, max(0.0, a))
        self.clear_fade()
        self.alpha = a
        if self.pipeline is None:
            return
        if self.gl:
            self._video_pad.set_property("alpha", a)
        else:
            self._balance.set_property("contrast", a)
            self._balance.set_property("saturation", a)

    def set_status(self, visible: bool, text: str) -> None:
        self.status_visible = visible
        self.status_text = text
        if self.pipeline is None:
            return
        if self._text.get_property("text") != text:
            self._text.set_property("text", text)
        self._text.set_property("silent", not visible)

    def push_video(self, buf: Gst.Buffer, caps: Gst.Caps | None) -> None:
        """GL: show a clip frame (called from the clip streaming thread).

        The buffer is pushed unchanged (zero copy; PyGObject can't make it writable to
        re-timestamp it). Instead it travels with a segment that maps its file timestamp
        to the display's running time "now"; the segment is re-anchored whenever the clip
        timeline jumps relative to the display clock (start, seek, loop, resume).
        """
        src = self.vsrc
        p = self.pipeline
        clock = p.get_clock() if p is not None else None
        if src is None or caps is None or clock is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return
        now = clock.get_time() - p.get_base_time()
        seg = self._vseg
        if seg is None or buf.pts < seg.start or abs(now - (seg.base + buf.pts - seg.start)) > _RESYNC_NS:
            seg = Gst.Segment()
            seg.init(Gst.Format.TIME)
            seg.start = seg.position = seg.time = buf.pts
            seg.base = now
            self._vseg = seg
        # Keep the decoder buffers of the frames the display may still hold (appsrc queue,
        # mixer queue + frame on screen) out of the decoder pool, so the decoder can't
        # overwrite a frame that is still on screen (paused / held clip).
        self._held.append(buf)
        src.emit("push-sample", Gst.Sample.new(buf, caps, seg, None))

    def release_video(self) -> None:
        """GL: make the mixer drop the frame it holds (video alpha must be 0)."""
        src = self.vsrc
        if src is None:
            return
        src.send_event(Gst.Event.new_flush_start())
        src.send_event(Gst.Event.new_flush_stop(False))
        self._held.clear()
        self._vseg = None

    def snapshot_png(self) -> bytes | None:
        if self.sink is None:
            return None
        if self.sink.get_property("enable-last-sample"):
            sample = self.sink.get_property("last-sample")
        else:
            # the sink renders in its own streaming thread, so a short sleep here is enough
            self.sink.set_property("enable-last-sample", True)
            time.sleep(3 / self.fps)
            sample = self.sink.get_property("last-sample")
            self.sink.set_property("enable-last-sample", False)
        if sample is None:
            return None
        caps = sample.get_caps()
        s = caps.get_structure(0)
        feat = caps.get_features(0)
        if feat is not None and feat.contains("memory:GLMemory"):
            # Download GL texture via map (GstGLMemory supports CPU mapping).
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            raw = Gst.Caps.from_string(
                f"video/x-raw,format={s.get_value('format')},width={s.get_value('width')},"
                f"height={s.get_value('height')},framerate=0/1"
            )
            sample = Gst.Sample.new(Gst.Buffer.new_wrapped(data), raw, None, None)
        out = GstVideo.video_convert_sample(sample, Gst.Caps.from_string("image/png"), 5 * Gst.SECOND)
        if out is None:
            return None
        buf = out.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            return bytes(info.data)
        finally:
            buf.unmap(info)


class _Run:
    """One clip pipeline instance."""

    def __init__(self, clip: Clip):
        self.clip = clip
        self.pipeline: Gst.Element | None = None
        self.sinkpad: Gst.Pad | None = None
        self.probe_id: int | None = None
        self.phase = "preroll"  # preroll -> seeking -> ready -> started
        self.pp: list[float] = list(clip.pause_points)
        self.block_next = False
        self.blocked = False
        self.fading_out = False
        self.duration: float | None = None
        self.position: float | None = clip.start_at
        self.desired_paused = False
        self.begin_source: int | None = None
        self.prepared = False  # built by prepare(), not yet promoted to current
        self.was_prepared = False
        self.sink: Gst.Element | None = None
        self.t_trigger: float | None = None


class GstPlayer(Player):
    def __init__(self, settings: Settings, sink: str | None = None):
        super().__init__()
        self.settings = settings
        self.requested_sink = sink or settings.display.sink
        self.kind = resolve_sink(self.requested_sink)
        self._channel = f"projektor-{os.getpid()}-{id(self)}"
        self._display: _Display | None = None
        self._run: _Run | None = None
        self._prep: _Run | None = None  # prerolled standby clip (GL mode)
        self._lock = threading.Lock()  # protects _Run fields touched by the streaming thread
        self._paused: str | None = None
        self._holding = False
        self._fade_dir = 0
        self._fade_src: int | None = None
        self._fade_from = 0.0
        self._fade_to = 0.0
        self._fade_t0 = 0.0
        self._fade_len = 0.0
        self._fade_curve = "linear"
        self._fade_ctrl = False
        self._poll_src: int | None = None
        self._status_visible = False
        self._status_text = ""
        self._display_ok = False
        self._rebuild_src: int | None = None
        self._shutting_down = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._start_display(self.kind)
        self._poll_src = GLib.timeout_add(_POS_POLL_MS, self._poll)

    def _start_display(self, kind: str) -> None:
        _prepare_gl_env(kind, getattr(self.settings.display, "connector", "") or "")
        d = _Display(
            kind, self.settings.display.width, self.settings.display.height,
            self.settings.display.fps, self._channel, self._on_display_error,
        )
        d.status_visible = self._status_visible and self._state == "idle"
        d.status_text = self._status_text
        d.alpha = 1.0 if self._run is not None and self._run.phase == "started" and self._fade_dir == 0 else 0.0
        self.discard_prepared()  # built for the old display
        self._display = d
        self.kind = kind
        self._display_ok = False
        try:
            d.build()
        except Exception as e:  # noqa: BLE001
            log.error("cannot start display (%s): %s", kind, e)
            d.destroy()
            if kind == "gl" and self.requested_sink in ("auto", "gl"):
                log.warning("falling back to kmssink (no GPU fades)")
                self._start_display("kms")
                return
            raise
        # consider the display healthy after a few seconds without error
        GLib.timeout_add(3000, self._mark_display_ok, d)

    def _mark_display_ok(self, d: _Display) -> bool:
        if d is self._display:
            self._display_ok = True
        return False

    def _on_display_error(self, msg: str) -> None:
        if self._shutting_down or self._rebuild_src is not None:
            return
        d = self._display
        if d is not None:
            d.destroy()
        if not self._display_ok and self.kind == "gl" and self.requested_sink in ("auto", "gl"):
            log.warning("GL display failed (%s); falling back to kmssink", msg)
            self._rebuild_src = GLib.timeout_add(200, self._rebuild_display, "kms")
        else:
            log.warning("display failed (%s); rebuilding in 2 s", msg)
            self._rebuild_src = GLib.timeout_add(2000, self._rebuild_display, self.kind)

    def _rebuild_display(self, kind: str) -> bool:
        self._rebuild_src = None
        if self._shutting_down:
            return False
        try:
            self._start_display(kind)
        except Exception as e:  # noqa: BLE001
            log.error("display rebuild failed: %s; retrying in 5 s", e)
            self._rebuild_src = GLib.timeout_add(5000, self._rebuild_display, kind)
        return False

    def shutdown(self) -> None:
        self._shutting_down = True
        for attr in ("_poll_src", "_rebuild_src"):
            src = getattr(self, attr)
            if src is not None:
                GLib.source_remove(src)
                setattr(self, attr, None)
        self.discard_prepared()
        self._unload()
        if self._display is not None:
            self._display.destroy()
            self._display = None

    # ------------------------------------------------------------------ queries
    def position(self) -> float | None:
        r = self._run
        if r is None:
            return None
        return r.position

    def duration(self) -> float | None:
        r = self._run
        if r is None:
            return None
        if r.clip.out_point is not None:
            if r.duration is None:
                return r.clip.out_point
            return min(r.clip.out_point, r.duration)
        return r.duration

    # ------------------------------------------------------------------ state helpers
    def _update(self) -> None:
        st, reason = compute_state(self._run is not None, self._paused, self._fade_dir, self._holding)
        self._set_state(st, reason)
        self._apply_status()

    def _apply_status(self) -> None:
        if self._display is not None:
            self._display.set_status(self._status_visible and self._state == "idle", self._status_text)

    def _set_alpha(self, a: float) -> None:
        if self._display is not None:
            self._display.set_video_alpha(a)

    def _alpha(self) -> float:
        return self._display.current_alpha() if self._display is not None else 0.0

    def _curve(self) -> str:
        r = self._run
        return (r.clip.fade_curve if r is not None else None) or self.settings.display.fade_curve

    # ------------------------------------------------------------------ fades
    def _start_fade(self, target: float, seconds: float) -> None:
        self._stop_fade()
        self._fade_from = self._alpha()
        self._fade_to = target
        self._fade_t0 = time.monotonic()
        self._fade_len = max(0.0, seconds)
        self._fade_dir = 1 if target > self._fade_from else -1
        self._fade_curve = self._curve()
        if self._fade_len <= 0:
            self._set_alpha(target)
            self._fade_done()
            return
        d = self._display
        if d is not None and d.ctrl_fade(self._fade_from, target, self._fade_len, self._fade_curve):
            self._fade_ctrl = True
            self._fade_src = GLib.timeout_add(max(1, int(self._fade_len * 1000)), self._fade_ctrl_done)
            return
        self._fade_src = GLib.timeout_add(_FADE_TICK_MS, self._fade_tick)

    def _stop_fade(self) -> None:
        if self._fade_src is not None:
            GLib.source_remove(self._fade_src)
            self._fade_src = None
        if self._fade_ctrl:
            self._fade_ctrl = False
            if self._display is not None:
                self._display.clear_fade()  # freezes alpha where the fade got to
        self._fade_dir = 0

    def _fade_ctrl_done(self) -> bool:
        self._fade_src = None
        self._fade_ctrl = False
        d = self._display
        if d is not None:
            d.clear_fade()
            d.set_video_alpha(self._fade_to)
        self._fade_done()
        return False

    def _fade_tick(self) -> bool:
        f = min(1.0, (time.monotonic() - self._fade_t0) / self._fade_len)
        self._set_alpha(fade_alpha(self._fade_curve, f, self._fade_from, self._fade_to))
        if f >= 1.0:
            self._fade_src = None
            self._fade_done()
            return False
        return True

    def _fade_done(self) -> None:
        d = self._fade_dir
        self._fade_dir = 0
        if d < 0:
            self._unload()
        self._update()

    # ------------------------------------------------------------------ clip pipeline
    def _unload(self) -> None:
        """Cut to black and destroy the clip pipeline (no state notification)."""
        self._stop_fade()
        self._set_alpha(0.0)
        with self._lock:
            r = self._run
            self._run = None
        self._clip = None
        self._paused = None
        self._holding = False
        if r is None:
            return
        if r.begin_source is not None:
            GLib.source_remove(r.begin_source)
            r.begin_source = None
        d = self._display
        if d is not None:
            d.release_video()  # give the decoder buffers back before the decoder goes away
        p = r.pipeline
        r.pipeline = None
        if p is not None:
            bus = p.get_bus()
            bus.remove_signal_watch()
            p.set_state(Gst.State.NULL)
            if d is not None:
                d.release_video()  # a frame the streaming thread pushed meanwhile

    def _make_run(self, clip: Clip, path: Path, prepared: bool = False) -> _Run:
        r = _Run(clip)
        r.prepared = r.was_prepared = prepared
        pb = Gst.ElementFactory.make("playbin3", None)
        if pb is None:
            raise RuntimeError("playbin3 not available")
        pb.set_property("uri", path.resolve().as_uri())
        pb.set_property("flags", _PLAYBIN_FLAGS)
        pb.set_property("video-sink", self._clip_sink(r))
        if clip.image:
            pb.set_property("video-filter", self._image_filter())
        r.pipeline = pb
        r.probe_id = r.sinkpad.add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BLOCK, self._probe, r
        )
        bus = pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_clip_message, r)
        return r

    def play(self, clip: Clip) -> None:
        t_trigger = time.monotonic()
        prep = self._prep
        d = self._display
        if prep is not None and prep.clip == clip and prep.pipeline is not None and d is not None and d.gl:
            self._prep = None
            self._unload()
            self._promote(prep, t_trigger)
            return
        self.discard_prepared()
        self._unload()
        path = Path(clip.path)
        if not path.is_file():
            self._update()
            self._fail(f"video file not found: {path}")
            return
        try:
            r = self._make_run(clip, path)
        except Exception as e:  # noqa: BLE001
            self._fail(f"cannot create pipeline for {path.name}: {e}")
            return
        r.t_trigger = t_trigger
        pb = r.pipeline
        with self._lock:
            self._run = r
        self._clip = clip
        self._paused = None
        self._holding = False
        self._fade_dir = 1 if clip.fade_in > 0 else 0
        log.info("loading %s (start %.3f, out %s, pp %s, end %s)", path.name, clip.start_at,
                 clip.out_point, list(clip.pause_points), clip.end)
        self._update()  # -> fading_in / playing (load is asynchronous)
        if pb.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self._fail(f"cannot open {path.name}")

    # ------------------------------------------------------------------ preloading
    @property
    def prepared_clip(self) -> Clip | None:
        return self._prep.clip if self._prep is not None else None

    def prepare(self, clip: Clip) -> None:
        d = self._display
        if self._shutting_down or d is None or d.pipeline is None or not d.gl:
            return  # CPU mode shares one intervideosink channel: a second run would show up
        if self._prep is not None and self._prep.clip == clip:
            return
        self.discard_prepared()
        path = Path(clip.path)
        if not path.is_file():
            return
        try:
            r = self._make_run(clip, path, prepared=True)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot prepare %s: %s", path.name, e)
            return
        self._prep = r
        log.info("preparing %s (start %.3f)", path.name, clip.start_at)
        if r.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            log.warning("cannot preload %s", path.name)
            self.discard_prepared()

    def discard_prepared(self) -> None:
        r = self._prep
        self._prep = None
        if r is None:
            return
        p = r.pipeline
        r.pipeline = None
        if p is not None:
            p.get_bus().remove_signal_watch()
            p.set_state(Gst.State.NULL)

    def _promote(self, r: _Run, t_trigger: float) -> None:
        """Make the prepared run current (the old one is already unloaded)."""
        r.prepared = False
        r.t_trigger = t_trigger
        clip = r.clip
        with self._lock:
            self._run = r
        self._clip = clip
        self._paused = None
        self._holding = False
        self._fade_dir = 1 if clip.fade_in > 0 else 0
        log.info("promoting preloaded %s (start %.3f, out %s, pp %s, end %s)", Path(clip.path).name,
                 clip.start_at, clip.out_point, list(clip.pause_points), clip.end)
        self._update()
        if r.phase == "ready":
            self._ready(r)
        # else: still prerolling / seeking; ASYNC_DONE continues as for a cold start

    def _push_preroll(self, r: _Run) -> None:
        """Show the prerolled frame a prepared run kept (its handoff was ignored)."""
        sink = r.sink
        d = self._display
        if sink is None or d is None:
            return
        sample = sink.get_property("last-sample")
        sink.set_property("enable-last-sample", False)  # don't pin a decoder buffer while playing
        if sample is not None:
            d.push_video(sample.get_buffer(), sample.get_caps())

    def _ready(self, r: _Run) -> None:
        if r is not self._run:
            return  # prepared, waiting for play()
        if r.was_prepared:
            self._push_preroll(r)
            if r.clip.fade_in > 0:  # alpha starts at 0: no need to wait for the display
                r.begin_source = GLib.timeout_add(1, self._begin, r)
                return
        self._schedule_begin(r)

    def _image_filter(self) -> Gst.Element:
        """EXIF orientation + fit into the display size (aspect kept, never upscaled)."""
        W, H = self.settings.display.width, self.settings.display.height
        return Gst.parse_bin_from_description(
            "videoconvert ! videoflip video-direction=auto ! videoscale method=4-tap "
            f'! capsfilter caps="video/x-raw,width=(int)[1,{W}],height=(int)[1,{H}],'
            'pixel-aspect-ratio=(fraction)1/1"',
            True,
        )

    def _clip_sink(self, r: _Run) -> Gst.Element:
        d = self._display
        if d is not None and d.gl:
            # Hands every frame to the display appsrc (see module docstring). fakevideosink
            # advertises GstVideoMeta, so the V4L2 decoder passes its dmabufs on uncopied.
            sink = Gst.ElementFactory.make("fakevideosink", None)
            sink.set_property("sync", True)
            sink.set_property("signal-handoffs", True)
            sink.set_property("enable-last-sample", r.prepared)  # prepared: keep the preroll frame
            r.sink = sink
            sink.connect("handoff", self._on_handoff, r)
            sink.connect("preroll-handoff", self._on_handoff, r)
            r.sinkpad = sink.get_static_pad("sink")
            # fakevideosink accepts ANY caps, so a VA-API decoder (PC) would hand over
            # memory:VAMemory frames that the display mixer can't take (not-negotiated).
            # Only offer dmabuf (Pi V4L2, zero copy), GL and plain system memory: VA
            # decoders then download. The caps are unchanged on the Pi.
            bin_ = Gst.Bin.new(None)
            cf = Gst.ElementFactory.make("capsfilter", None)
            cf.set_property("caps", Gst.Caps.from_string(
                "video/x-raw(memory:DMABuf);video/x-raw(memory:GLMemory);video/x-raw"))
            bin_.add(cf)
            bin_.add(sink)
            cf.link(sink)
            bin_.add_pad(Gst.GhostPad.new("sink", cf.get_static_pad("sink")))
            return bin_
        sinkbin = Gst.parse_bin_from_description(
            f"queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! videoconvert "
            f"! video/x-raw,format={_CLIP_FORMATS} "
            f"! intervideosink name=ivsink channel={self._channel} enable-last-sample=false",
            True,
        )
        r.sinkpad = sinkbin.get_by_name("ivsink").get_static_pad("sink")
        return sinkbin

    def _on_handoff(self, sink, buf: Gst.Buffer, pad: Gst.Pad, r: _Run) -> None:
        # clip streaming thread; only the current run feeds the display
        d = self._display
        if r is self._run and d is not None:
            d.push_video(buf, pad.get_current_caps())

    def _fail(self, msg: str) -> None:
        log.error("%s", msg)
        self._unload()
        self._update()
        self._emit_error(msg)

    def _on_clip_message(self, bus, msg, r: _Run) -> None:
        if r is not self._run:
            if r is self._prep and r.prepared:
                if msg.type == Gst.MessageType.ASYNC_DONE:
                    self._on_async_done(r)
                elif msg.type == Gst.MessageType.ERROR:
                    err, _dbg = msg.parse_error()
                    log.warning("preload of %s failed: %s", Path(r.clip.path).name, err.message)
                    self.discard_prepared()
            return
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            self._on_async_done(r)
        elif t == Gst.MessageType.EOS:
            self._on_eos(r)
        elif t == Gst.MessageType.SEGMENT_DONE:
            self._on_segment_done(r)
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.debug("clip error debug: %s", dbg)
            self._fail(f"playback error ({Path(r.clip.path).name}): {err.message}")
        elif t == Gst.MessageType.WARNING:
            w, _dbg = msg.parse_warning()
            log.warning("clip warning: %s", w.message)
        elif t == Gst.MessageType.DURATION_CHANGED:
            self._query_duration(r)

    def _query_duration(self, r: _Run) -> None:
        ok, dur = r.pipeline.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            r.duration = dur / Gst.SECOND

    def _seek(self, r: _Run, start: float, flush: bool = True) -> bool:
        c = r.clip
        stop_type = Gst.SeekType.SET if c.out_point is not None else Gst.SeekType.NONE
        stop = int(c.out_point * Gst.SECOND) if c.out_point is not None else -1
        flags = Gst.SeekFlags.ACCURATE
        if flush:
            flags |= Gst.SeekFlags.FLUSH
        if c.end == "loop":
            flags |= Gst.SeekFlags.SEGMENT  # -> SEGMENT_DONE instead of EOS, gapless loop
        return r.pipeline.seek(
            1.0, Gst.Format.TIME, flags, Gst.SeekType.SET, int(start * Gst.SECOND), stop_type, stop,
        )

    def _on_async_done(self, r: _Run) -> None:
        if r.phase == "preroll":
            self._query_duration(r)
            c = r.clip
            if c.start_at > 0.0005 or c.out_point is not None or c.end == "loop":
                r.phase = "seeking"
                if not self._seek(r, c.start_at):
                    log.warning("initial seek failed; starting from the beginning")
                    r.phase = "ready"
            else:
                r.phase = "ready"
            if r.phase == "ready":
                self._ready(r)
        elif r.phase == "seeking":
            r.phase = "ready"
            self._ready(r)

    def _schedule_begin(self, r: _Run) -> None:
        # Give the display ~2 frames to pick up the prerolled first frame before raising alpha.
        delay = max(40, int(2000 / self.settings.display.fps))
        r.begin_source = GLib.timeout_add(delay, self._begin, r)

    def _begin(self, r: _Run) -> bool:
        r.begin_source = None
        if r is not self._run:
            return False
        r.phase = "started"
        if r.t_trigger is not None:
            log.info("first frame after %d ms (%s)", (time.monotonic() - r.t_trigger) * 1000,
                     "preloaded" if r.was_prepared else "cold")
            r.t_trigger = None
        if self._paused is None:
            r.pipeline.set_state(Gst.State.PLAYING)
        if r.clip.fade_in > 0:
            self._start_fade(1.0, r.clip.fade_in)
        else:
            self._set_alpha(1.0)
            self._fade_dir = 0
        self._update()
        return False

    def _on_eos(self, r: _Run) -> None:
        if r.fading_out:
            return  # keep the last frame until the fade-out completes
        mode = r.clip.end
        log.info("end of %s (%s)", Path(r.clip.path).name, mode)
        if mode == "hold":
            self._holding = True
            self._update()
        elif mode == "loop":
            with self._lock:
                r.pp = [p for p in r.clip.pause_points if p > r.clip.in_point + 1e-6]
                r.block_next = False
            self._unblock(r)
            if not self._seek(r, r.clip.in_point):
                self._fail("loop seek failed")
        else:
            self._unload()
            self._update()

    def _on_segment_done(self, r: _Run) -> None:
        # end=loop: queue the next pass without flushing (the last frame stays until the
        # first frame of the next pass replaces it)
        if r.fading_out:
            return
        with self._lock:
            r.pp = [p for p in r.clip.pause_points if p > r.clip.in_point + 1e-6]
        if not self._seek(r, r.clip.in_point, flush=False):
            self._fail("loop seek failed")

    # ------------------------------------------------------------------ pause points
    def _probe(self, pad, info, r: _Run):
        buf = info.get_buffer()
        with self._lock:
            if r is not self._run:
                return Gst.PadProbeReturn.PASS
            if r.blocked:
                return Gst.PadProbeReturn.OK
            if r.block_next:
                r.block_next = False
                r.blocked = True
                GLib.idle_add(self._on_pause_point, r)
                return Gst.PadProbeReturn.OK  # keep this buffer blocked
            if not r.pp or r.fading_out or buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
                return Gst.PadProbeReturn.PASS
            ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            t = buf.pts
            if ev is not None:
                seg = ev.parse_segment()
                st = seg.to_stream_time(Gst.Format.TIME, buf.pts)
                if st != Gst.CLOCK_TIME_NONE and st >= 0:
                    t = st
            ts = t / Gst.SECOND
            if ts >= r.pp[0] - _PP_TOLERANCE:
                while r.pp and r.pp[0] - _PP_TOLERANCE <= ts:
                    r.pp.pop(0)
                r.block_next = True  # this frame is shown, the next one is held back
                r.position = ts
        return Gst.PadProbeReturn.PASS

    def _on_pause_point(self, r: _Run) -> bool:
        if r is not self._run or r.fading_out:
            self._unblock(r)
            return False
        r.pipeline.set_state(Gst.State.PAUSED)
        self._paused = "pause_point"
        log.info("pause point reached at %.3f", r.position or 0.0)
        self._update()
        return False

    def _unblock(self, r: _Run) -> None:
        with self._lock:
            if not r.blocked or r.sinkpad is None:
                return
            r.blocked = False
            r.block_next = False
            if r.probe_id is not None:
                r.sinkpad.remove_probe(r.probe_id)  # releases the held buffer
            r.probe_id = r.sinkpad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BLOCK, self._probe, r
            )

    def _poll(self) -> bool:
        r = self._run
        if (r is not None and r.pipeline is not None and r.phase in ("ready", "started")
                and not r.blocked and not r.block_next and self._paused is None):
            ok, pos = r.pipeline.query_position(Gst.Format.TIME)
            if ok and pos >= 0:
                r.position = pos / Gst.SECOND
        return True

    # ------------------------------------------------------------------ commands
    def pause(self) -> None:
        r = self._run
        if r is None or self._paused or self._holding or r.clip.image:
            return
        if r.phase == "started":
            r.pipeline.set_state(Gst.State.PAUSED)
        self._paused = "manual"
        self._update()

    def resume(self) -> None:
        r = self._run
        if r is None or not self._paused:
            return
        self._paused = None
        if r.phase == "started":
            r.pipeline.set_state(Gst.State.PLAYING)
            self._unblock(r)
        self._update()

    def fadeout(self, seconds: float) -> None:
        r = self._run
        if r is None:
            return
        if seconds <= 0 or r.phase != "started" or self._alpha() <= 0.0:
            self.stop()
            return
        with self._lock:
            r.fading_out = True
            r.block_next = False
        if r.blocked and not self._paused:
            self._unblock(r)
        self._start_fade(0.0, seconds)
        self._update()

    def stop(self) -> None:
        self._unload()
        self._update()

    def seek(self, seconds: float) -> None:
        r = self._run
        if r is None or r.phase not in ("ready", "started") or r.clip.image:
            return
        c = r.clip
        hi = self.duration()
        t = max(c.in_point, float(seconds))
        if hi is not None:
            t = min(t, max(c.in_point, hi - 0.001))
        with self._lock:
            r.pp = [p for p in c.pause_points if p > t + 1e-6]
            r.block_next = False
        self._unblock(r)
        r.position = t
        if self._holding:
            self._holding = False
        if not self._seek(r, t):
            log.warning("seek to %.3f failed", t)
        self._update()

    def set_status(self, visible: bool, text: str = "") -> None:
        self._status_visible = bool(visible)
        self._status_text = text
        self._apply_status()

    def snapshot_png(self) -> bytes | None:
        if self._display is None:
            return None
        try:
            return self._display.snapshot_png()
        except Exception as e:  # noqa: BLE001
            log.warning("snapshot failed: %s", e)
            return None
