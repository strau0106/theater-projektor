"""GStreamer player (ARCHITECTURE.md §4).

Two kinds of pipelines:

* **Display pipeline** (permanent, created once, never torn down while running): owns the
  video sink so there is never a flash of the console between clips.

      GL mode (sink gl / window):
        videotestsrc(black, live) ! textoverlay ............ glvideomixer.sink_0  (status)
        intervideosrc(channel) .............................. glvideomixer.sink_1  (video, alpha = fade)
        glvideomixer(background=black) ! GL caps(W x H @ fps) ! glimagesink

      CPU mode (sink kms, headless test sinks):
        intervideosrc ! videoconvert ! videoscale ! caps(W x H) ! videobalance(fade)
                      ! textoverlay(status) ! videoconvert ! kmssink

* **Clip pipeline** (one per playing video): playbin3 (video only) whose video sink is
  `queue ! videoconvert ! intervideosink(channel)`. Pause/seek/EOS/position work on this
  pipeline independently of the display. intervideosrc repeats the last frame it got, so a
  paused / held clip keeps its frame on screen; black is produced via mixer alpha.

Pause points: a blocking buffer probe on the intervideosink pad lets the first frame with
stream time >= pause point through, blocks the following one and pauses the clip pipeline,
so the frame at/after the pause point stays on screen (frame accurate).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from ..config import Clip, Settings  # noqa: E402
from .base import Player, compute_state  # noqa: E402

log = logging.getLogger(__name__)

if not Gst.is_initialized():
    Gst.init(None)

# playbin3 flags: video (0x1) | native-video (0x40) -> no audio decoding, no extra converters
_PLAYBIN_FLAGS = 0x1 | 0x40
_PP_TOLERANCE = 0.001  # s; frame pts rounding
_FADE_TICK_MS = 16
_POS_POLL_MS = 50
# Raw formats passed to the display (system memory). Pi-specific uncertainty: the Pi 4
# V4L2 H.264 decoder should negotiate NV12/I420 in system memory here (one glupload copy per
# frame); the Pi 5 HEVC decoder (v4l2slh265dec) may only offer tiled NV12_128C8 / DMA_DRM,
# in which case videoconvert falls back to a CPU conversion (or negotiation fails) - verify
# on hardware and add a dedicated conversion path if needed.
_CLIP_FORMATS = "{ I420, NV12, YV12, BGRA, RGBA, BGRx, RGBx }"
_MAX_U64 = 2**64 - 1


def resolve_sink(sink: str) -> str:
    """Map settings `display.sink` to a concrete sink kind."""
    if sink == "auto":
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            return "window"
        return "gl"
    return sink


def _prepare_gl_env(kind: str) -> None:
    if kind == "gl":
        # Pi without X/Wayland: EGL on GBM/KMS directly (fullscreen on the first connected
        # connector; set GST_GL_GBM_DRM_CONNECTOR=HDMI-A-1 in the service env to force one).
        os.environ.setdefault("GST_GL_WINDOW", "gbm")
        os.environ.setdefault("GST_GL_PLATFORM", "egl")
        os.environ.setdefault("GST_GL_API", "gles2")


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
        self._status_pad = None
        self._balance = None
        self._text = None
        self._valve = None
        self._bus_watch = False
        self.alpha = 0.0
        self.status_visible = False
        self.status_text = ""

    def _font(self) -> str:
        # ~ 20 lines fit on screen
        size = max(10, int(self.height / 26))
        return f"DejaVu Sans Mono Bold {size}"

    def build(self) -> None:
        W, H, F = self.width, self.height, self.fps
        textprops = (
            f'font-desc="{self._font()}" valignment=top halignment=left line-alignment=left '
            f"xpad={int(W * 0.04)} ypad={int(H * 0.05)} auto-resize=false shaded-background=false "
            f"draw-shadow=false wait-text=false"
        )
        if self.gl:
            if self.kind == "glfakesink":
                sink = "gldownload ! videoconvert ! fakesink name=out sync=true enable-last-sample=true"
            else:
                sink = "glimagesink name=out force-aspect-ratio=true enable-last-sample=true"
            desc = (
                f"glvideomixer name=mix background=black "
                f"! video/x-raw(memory:GLMemory),width={W},height={H},framerate={F}/1 ! {sink} "
                # status layer; the valve stops the CPU work while the status screen is hidden
                f"videotestsrc pattern=black is-live=true "
                f"! video/x-raw,format=BGRA,width={W},height={H},framerate={F}/1 "
                f"! valve name=svalve drop=false ! textoverlay name=stext {textprops} "
                f"! queue max-size-buffers=3 ! mix.sink_0 "
                f"intervideosrc name=vsrc channel={self.channel} timeout={_MAX_U64} "
                f"! queue max-size-buffers=3 ! mix.sink_1 "
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
            self._status_pad = mix.get_static_pad("sink_0")
            self._video_pad = mix.get_static_pad("sink_1")
            self._valve = p.get_by_name("svalve")
            for pad, z in ((self._status_pad, 0), (self._video_pad, 1)):
                pad.set_property("zorder", z)
                pad.set_property("width", W)
                pad.set_property("height", H)
                pad.set_property("xpos", 0)
                pad.set_property("ypos", 0)
                pad.set_property("sizing-policy", 1)  # keep-aspect-ratio (letterbox)
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

    def set_video_alpha(self, a: float) -> None:
        a = min(1.0, max(0.0, a))
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
        if self.gl:
            self._status_pad.set_property("alpha", 1.0 if visible else 0.0)
            self._valve.set_property("drop", not visible)
        else:
            self._text.set_property("silent", not visible)

    def snapshot_png(self) -> bytes | None:
        if self.sink is None:
            return None
        sample = self.sink.get_property("last-sample")
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


class GstPlayer(Player):
    def __init__(self, settings: Settings, sink: str | None = None):
        super().__init__()
        self.settings = settings
        self.requested_sink = sink or settings.display.sink
        self.kind = resolve_sink(self.requested_sink)
        self._channel = f"projektor-{os.getpid()}-{id(self)}"
        self._display: _Display | None = None
        self._run: _Run | None = None
        self._lock = threading.Lock()  # protects _Run fields touched by the streaming thread
        self._paused: str | None = None
        self._holding = False
        self._fade_dir = 0
        self._fade_src: int | None = None
        self._fade_from = 0.0
        self._fade_to = 0.0
        self._fade_t0 = 0.0
        self._fade_len = 0.0
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
        _prepare_gl_env(kind)
        d = _Display(
            kind, self.settings.display.width, self.settings.display.height,
            self.settings.display.fps, self._channel, self._on_display_error,
        )
        d.status_visible = self._status_visible and self._state == "idle"
        d.status_text = self._status_text
        d.alpha = 1.0 if self._run is not None and self._run.phase == "started" and self._fade_dir == 0 else 0.0
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
        return self._display.alpha if self._display is not None else 0.0

    # ------------------------------------------------------------------ fades
    def _start_fade(self, target: float, seconds: float) -> None:
        self._stop_fade()
        self._fade_from = self._alpha()
        self._fade_to = target
        self._fade_t0 = time.monotonic()
        self._fade_len = max(0.0, seconds)
        self._fade_dir = 1 if target > self._fade_from else -1
        if self._fade_len <= 0:
            self._set_alpha(target)
            self._fade_done()
            return
        self._fade_src = GLib.timeout_add(_FADE_TICK_MS, self._fade_tick)

    def _stop_fade(self) -> None:
        if self._fade_src is not None:
            GLib.source_remove(self._fade_src)
            self._fade_src = None
        self._fade_dir = 0

    def _fade_tick(self) -> bool:
        f = min(1.0, (time.monotonic() - self._fade_t0) / self._fade_len)
        self._set_alpha(self._fade_from + (self._fade_to - self._fade_from) * f)
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
        p = r.pipeline
        r.pipeline = None
        if p is not None:
            bus = p.get_bus()
            bus.remove_signal_watch()
            p.set_state(Gst.State.NULL)

    def play(self, clip: Clip) -> None:
        self._unload()
        path = Path(clip.path)
        if not path.is_file():
            self._update()
            self._fail(f"video file not found: {path}")
            return
        r = _Run(clip)
        try:
            pb = Gst.ElementFactory.make("playbin3", None)
            if pb is None:
                raise RuntimeError("playbin3 not available")
            pb.set_property("uri", path.resolve().as_uri())
            pb.set_property("flags", _PLAYBIN_FLAGS)
            sinkbin = Gst.parse_bin_from_description(
                f"queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! videoconvert "
                f"! video/x-raw,format={_CLIP_FORMATS} "
                f"! intervideosink name=ivsink channel={self._channel} enable-last-sample=false",
                True,
            )
            pb.set_property("video-sink", sinkbin)
            ivsink = sinkbin.get_by_name("ivsink")
            r.sinkpad = ivsink.get_static_pad("sink")
            r.pipeline = pb
            r.probe_id = r.sinkpad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BLOCK, self._probe, r
            )
            bus = pb.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_clip_message, r)
        except Exception as e:  # noqa: BLE001
            self._fail(f"cannot create pipeline for {path.name}: {e}")
            return
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

    def _fail(self, msg: str) -> None:
        log.error("%s", msg)
        self._unload()
        self._update()
        self._emit_error(msg)

    def _on_clip_message(self, bus, msg, r: _Run) -> None:
        if r is not self._run:
            return
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            self._on_async_done(r)
        elif t == Gst.MessageType.EOS:
            self._on_eos(r)
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

    def _seek(self, r: _Run, start: float) -> bool:
        c = r.clip
        stop_type = Gst.SeekType.SET if c.out_point is not None else Gst.SeekType.NONE
        stop = int(c.out_point * Gst.SECOND) if c.out_point is not None else -1
        return r.pipeline.seek(
            1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET, int(start * Gst.SECOND), stop_type, stop,
        )

    def _on_async_done(self, r: _Run) -> None:
        if r.phase == "preroll":
            self._query_duration(r)
            c = r.clip
            if c.start_at > 0.0005 or c.out_point is not None:
                r.phase = "seeking"
                if not self._seek(r, c.start_at):
                    log.warning("initial seek failed; starting from the beginning")
                    r.phase = "ready"
            else:
                r.phase = "ready"
            if r.phase == "ready":
                self._schedule_begin(r)
        elif r.phase == "seeking":
            r.phase = "ready"
            self._schedule_begin(r)

    def _schedule_begin(self, r: _Run) -> None:
        # Give the display ~2 frames to pick up the prerolled first frame before raising alpha.
        delay = max(40, int(2000 / self.settings.display.fps))
        r.begin_source = GLib.timeout_add(delay, self._begin, r)

    def _begin(self, r: _Run) -> bool:
        r.begin_source = None
        if r is not self._run:
            return False
        r.phase = "started"
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
        if r is None or self._paused or self._holding:
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
        if r is None or r.phase not in ("ready", "started"):
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
