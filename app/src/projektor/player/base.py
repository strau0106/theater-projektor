"""Player interface.

A Player owns the display. All methods except the queries (`state`, `pause_reason`,
`clip`, `position()`, `duration()`) must be called on the player thread (the GLib main
loop in production). Callbacks are invoked on that same thread.

Player state machine (mirrors ARCHITECTURE.md §5):

    idle -> fading_in -> playing <-> paused(manual|pause_point) -> fading_out -> idle
    end of video: end=black -> idle, end=hold -> holding, end=loop -> keeps playing
    still image (clip.image): one frame, always end=hold -> holding after its fade-in
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Literal

from ..config import Clip

PlayerState = Literal["idle", "fading_in", "playing", "paused", "holding", "fading_out"]
PauseReason = Literal["manual", "pause_point"]

StateCallback = Callable[[str, "str | None"], None]
ErrorCallback = Callable[[str], None]

ACTIVE_STATES = ("fading_in", "playing", "paused", "holding", "fading_out")


def fade_value(curve: str, t: float) -> float:
    """Fade-in alpha for progress t in [0, 1] (clamped); 0 -> 0, 1 -> 1, monotonic.

    linear: alpha = t.  scurve: smoothstep 3t^2 - 2t^3 (soft start and end).
    perceptual: alpha = t ** 2.2, i.e. the inverse of a gamma-2.2 display response, so the
    emitted light (and roughly the perceived brightness) rises evenly over time.
    A fade-out uses the mirrored curve, see `fade_alpha`.
    """
    t = min(1.0, max(0.0, t))
    if curve == "scurve":
        return t * t * (3.0 - 2.0 * t)
    if curve == "perceptual":
        return t**2.2
    return t


def fade_alpha(curve: str, t: float, a0: float, target: float) -> float:
    """Alpha at progress t of a fade from a0 to target (0 or 1).

    Fade-in: a0 + (1 - a0) * fade_value(t); fade-out: a0 * fade_value(1 - t) (time-mirrored
    curve). Starting from the current alpha means a mid-fade reversal never jumps.
    """
    if target > a0:
        return a0 + (target - a0) * fade_value(curve, t)
    return target + (a0 - target) * fade_value(curve, 1.0 - t)


def compute_state(
    loaded: bool,
    paused: str | None,
    fade_dir: int,
    holding: bool,
) -> tuple[str, str | None]:
    """Derive (state, pause_reason) from player flags.

    fade_dir: +1 fading in, -1 fading out, 0 no fade running.
    """
    if not loaded:
        return "idle", None
    if fade_dir < 0:
        return "fading_out", None
    if paused:
        return "paused", paused
    if fade_dir > 0:  # before holding: an image (or a clip shorter than its fade) ends mid-fade
        return "fading_in", None
    if holding:
        return "holding", None
    return "playing", None


class Player(ABC):
    def __init__(self) -> None:
        self._on_state: StateCallback | None = None
        self._on_error: ErrorCallback | None = None
        self._state: str = "idle"
        self._pause_reason: str | None = None
        self._clip: Clip | None = None

    # ------------------------------------------------------------------ callbacks
    def set_callbacks(self, on_state: StateCallback | None, on_error: ErrorCallback | None) -> None:
        self._on_state = on_state
        self._on_error = on_error

    def _set_state(self, state: str, reason: str | None = None) -> None:
        if state == self._state and reason == self._pause_reason:
            return
        self._state = state
        self._pause_reason = reason
        if self._on_state:
            self._on_state(state, reason)

    def _emit_error(self, msg: str) -> None:
        if self._on_error:
            self._on_error(msg)

    # ------------------------------------------------------------------ queries (any thread)
    @property
    def state(self) -> str:
        return self._state

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def clip(self) -> Clip | None:
        return self._clip

    @abstractmethod
    def position(self) -> float | None:
        """Current position in the file (seconds), None when idle."""

    @abstractmethod
    def duration(self) -> float | None:
        """Effective end of the clip (out_point or file duration), None if unknown."""

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Create the display (status/black). Called once on the player thread."""

    def shutdown(self) -> None:
        """Release the display."""

    # ------------------------------------------------------------------ commands (player thread)
    @abstractmethod
    def play(self, clip: Clip) -> None:
        """Stop whatever plays (cut), then play `clip` with a fade-in of clip.fade_in."""

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def fadeout(self, seconds: float) -> None:
        """Fade to black over `seconds`, then stop (-> idle)."""

    @abstractmethod
    def stop(self) -> None:
        """Cut to black immediately (-> idle)."""

    @abstractmethod
    def seek(self, seconds: float) -> None:
        """Seek to absolute file time (clamped to in/out point)."""

    @abstractmethod
    def set_status(self, visible: bool, text: str = "") -> None:
        """Show/hide the status screen (only drawn while idle)."""

    def prepare(self, clip: Clip) -> None:
        """Preload `clip` (the standby cue) so a following `play(clip)` starts at once.

        At most one clip is prepared; preparing another discards the old one. Optional:
        the default does nothing.
        """

    def discard_prepared(self) -> None:
        """Drop the prepared clip, if any."""

    @property
    def prepared_clip(self) -> Clip | None:
        return None

    def snapshot_png(self) -> bytes | None:
        """PNG of the current output frame, if supported."""
        return None
