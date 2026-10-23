"""Deterministic fake player for tests and headless runs.

Time only moves when `advance(seconds)` is called (or, with `realtime=True` and a GLib
loop, from a timer). Simulates fades, pause points, out points, end modes, still images
(duration 0: end of stream right after the start, always held) and errors.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Clip
from .base import Player, compute_state, fade_alpha

log = logging.getLogger(__name__)

EPS = 1e-9


class FakePlayer(Player):
    def __init__(
        self,
        durations: dict[str, float] | None = None,
        default_duration: float = 60.0,
        check_files: bool = False,
        fps: float = 30.0,
        realtime: bool = False,
    ) -> None:
        super().__init__()
        self.durations = dict(durations or {})  # keyed by file name (Path.name)
        self.default_duration = default_duration
        self.check_files = check_files
        self.fps = fps
        self.realtime = realtime
        self.missing: set[str] = set()  # file names that "do not exist"
        self.calls: list[tuple] = []
        self.status_visible = False
        self.status_text = ""
        self.alpha = 0.0
        self.started = False
        self._pos = 0.0
        self._dur: float | None = None
        self._paused: str | None = None
        self._holding = False
        self._fade_dir = 0
        self._fade_rate = 0.0
        self._fade_t = 0.0
        self._fade_a0 = 0.0
        self._fade_target = 1.0
        self.prepared: Clip | None = None  # tests: the preloaded standby clip
        self.promoted = False  # tests: last play() used the prepared clip
        self._pp: list[float] = []
        self._timer = None
        self.results_supported = True
        self.results: tuple[int, int] | None = None  # shown graph (ja, nein)

    # ------------------------------------------------------------------ queries
    def position(self) -> float | None:
        return None if self._clip is None else round(self._pos, 6)

    def duration(self) -> float | None:
        if self._clip is None:
            return None
        return self._end()

    def _end(self) -> float:
        assert self._clip is not None
        d = self._dur if self._dur is not None else self.default_duration
        if self._clip.out_point is not None:
            d = min(d, self._clip.out_point)
        return d

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.started = True
        self.calls.append(("start",))
        if self.realtime:
            from gi.repository import GLib

            self._timer = GLib.timeout_add(40, self._rt_tick)

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        if self._timer is not None:
            from gi.repository import GLib

            GLib.source_remove(self._timer)
            self._timer = None
        self._unload()

    def _rt_tick(self) -> bool:
        self.advance(0.04)
        return True

    # ------------------------------------------------------------------ helpers
    def _update(self) -> None:
        st, reason = compute_state(self._clip is not None, self._paused, self._fade_dir, self._holding)
        self._set_state(st, reason)

    def _unload(self) -> None:
        self._clip = None
        self._dur = None
        self._paused = None
        self._holding = False
        self._fade_dir = 0
        self.alpha = 0.0
        self._pos = 0.0
        self._pp = []

    def _next_pps(self, after: float, inclusive: bool = False) -> list[float]:
        assert self._clip is not None
        if inclusive:
            return [p for p in self._clip.pause_points if p >= after - EPS]
        return [p for p in self._clip.pause_points if p > after + EPS]

    # ------------------------------------------------------------------ commands
    @property
    def prepared_clip(self) -> Clip | None:
        return self.prepared

    def prepare(self, clip: Clip) -> None:
        self.calls.append(("prepare", clip.video_id, clip.cue_id))
        name = Path(clip.path).name
        if name in self.missing or (self.check_files and not Path(clip.path).exists()):
            self.prepared = None
            return
        self.prepared = clip

    def discard_prepared(self) -> None:
        self.calls.append(("discard_prepared",))
        self.prepared = None

    def _curve(self) -> str:
        assert self._clip is not None
        return self._clip.fade_curve or "scurve"

    def _start_fade(self, target: float, seconds: float) -> None:
        self._fade_a0 = self.alpha
        self._fade_target = target
        self._fade_t = 0.0
        self._fade_rate = 1.0 / seconds
        self._fade_dir = 1 if target > self.alpha else -1

    def play(self, clip: Clip) -> None:
        self.calls.append(("play", clip.video_id, clip.cue_id))
        self.promoted = self.prepared is not None and self.prepared == clip
        self.prepared = None
        if self._clip is not None:
            self._unload()
        name = Path(clip.path).name
        if name in self.missing or (self.check_files and not Path(clip.path).exists()):
            self._update()
            msg = f"video file not found: {clip.path}"
            log.error(msg)
            self._emit_error(msg)
            return
        self._clip = clip
        self._dur = 0.0 if clip.image else self.durations.get(name, self.default_duration)
        self._pos = clip.start_at
        self._pp = self._next_pps(clip.start_at)
        if clip.fade_in > 0:
            self.alpha = 0.0
            self._start_fade(1.0, clip.fade_in)
        else:
            self.alpha = 1.0
            self._fade_dir = 0
        self._update()

    def pause(self) -> None:
        self.calls.append(("pause",))
        if self._clip is None or self._holding or self._paused or self._clip.image:
            return
        self._paused = "manual"
        self._update()

    def resume(self) -> None:
        self.calls.append(("resume",))
        if self._clip is None or not self._paused:
            return
        self._paused = None
        self._update()

    def fadeout(self, seconds: float) -> None:
        self.calls.append(("fadeout", seconds))
        if self._clip is None:
            return
        if seconds <= 0 or self.alpha <= 0:
            self.stop()
            return
        self._start_fade(0.0, seconds)
        self._update()

    def stop(self) -> None:
        self.calls.append(("stop",))
        self._unload()
        self._update()

    def seek(self, seconds: float) -> None:
        self.calls.append(("seek", seconds))
        if self._clip is None or self._clip.image:
            return
        lo = self._clip.in_point
        t = max(lo, min(float(seconds), self._end()))
        self._pos = t
        self._pp = self._next_pps(t)
        if self._holding:
            self._holding = False
        self._update()

    def set_status(self, visible: bool, text: str = "") -> None:
        self.status_visible = bool(visible)
        self.status_text = text

    # ------------------------------------------------------------------ results overlay
    @property
    def supports_results(self) -> bool:
        return self.results_supported

    @property
    def results_visible(self) -> bool:
        return self.results is not None

    def results_show(self, ja: int, nein: int) -> None:
        self.calls.append(("results_show", ja, nein))
        self.results = (ja, nein)

    def results_hide(self, seconds: float | None = None) -> None:
        self.calls.append(("results_hide", seconds))
        self.results = None

    def results_clear(self) -> None:
        self.calls.append(("results_clear",))
        self.results = None

    def reload_results_design(self) -> None:
        self.calls.append(("reload_results_design",))

    # ------------------------------------------------------------------ test helpers
    def fail(self, msg: str = "decode error") -> None:
        """Simulate a decode/playback error."""
        if self._clip is None:
            return
        log.error("fake player error: %s", msg)
        self._unload()
        self._update()
        self._emit_error(msg)

    def advance(self, seconds: float) -> None:
        """Advance simulated time by `seconds` in frame-sized steps."""
        step = 1.0 / self.fps
        remaining = float(seconds)
        while remaining > EPS:
            dt = min(step, remaining)
            remaining -= dt
            self._step(dt)

    def _step(self, dt: float) -> None:
        if self._clip is None:
            return
        # fades run in wall time, independent of pause
        if self._fade_dir > 0:
            self._fade_t = min(1.0, self._fade_t + dt * self._fade_rate)
            self.alpha = fade_alpha(self._curve(), self._fade_t, self._fade_a0, 1.0)
            if self._fade_t >= 1.0 - EPS:
                self.alpha = 1.0
                self._fade_dir = 0
                self._update()
        elif self._fade_dir < 0:
            self._fade_t = min(1.0, self._fade_t + dt * self._fade_rate)
            self.alpha = fade_alpha(self._curve(), self._fade_t, self._fade_a0, 0.0)
            if self._fade_t >= 1.0 - EPS:
                self._unload()
                self._update()
                return
        if self._paused or self._holding:
            return
        new = self._pos + dt
        if self._pp and new >= self._pp[0] - EPS and self._fade_dir >= 0:
            self._pos = self._pp.pop(0)
            self._paused = "pause_point"
            self._update()
            return
        end = self._end()
        if new >= end - EPS:
            self._pos = end
            self._on_eos()
            return
        self._pos = new

    def _on_eos(self) -> None:
        assert self._clip is not None
        if self._fade_dir < 0:
            return  # hold last frame until fade-out completes
        mode = self._clip.end
        if mode == "hold":
            self._holding = True
            self._update()
        elif mode == "loop":
            self._pos = self._clip.in_point
            self._pp = self._next_pps(self._clip.in_point, inclusive=False)
        else:
            self._unload()
            self._update()
