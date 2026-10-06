"""Controller: cue engine / state machine (ARCHITECTURE.md §5, §7).

Public command methods are thread-safe and non-blocking: they validate their input in the
caller's thread (raising ProjektorError on bad input) and then marshal the actual work onto
the player thread via an Executor (GLib main loop in production, synchronous in tests).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

from . import ProjektorError
from .config import Settings, Show, ShowStore, resolve_cue, resolve_cue_ref, resolve_video
from .player.base import ACTIVE_STATES, Player

log = logging.getLogger(__name__)

__all__ = ["Controller", "ProjektorError", "Executor", "SyncExecutor", "GLibExecutor"]

SHOW_COMMANDS = ("go", "cue", "play", "pause", "resume", "fadeout", "stop", "seek")


class Executor(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any) -> None: ...


def _run_logged(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("error in %s", getattr(fn, "__name__", fn))


class SyncExecutor:
    """Runs submitted work immediately in the calling thread (tests, single-threaded use)."""

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        _run_logged(fn, *args)


class GLibExecutor:
    """Marshals work onto the GLib main loop (GLib.idle_add)."""

    def __init__(self) -> None:
        from gi.repository import GLib

        self._GLib = GLib

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        def cb() -> bool:
            _run_logged(fn, *args)
            return False

        self._GLib.idle_add(cb)


StatusBuilder = Callable[[dict], str]


class Controller:
    def __init__(
        self,
        settings: Settings,
        show_store: ShowStore,
        player: Player,
        executor: Executor | None = None,
        status_builder: StatusBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.show_store = show_store
        self.player = player
        self.executor: Executor = executor or SyncExecutor()
        self.status_builder = status_builder
        self._lock = threading.RLock()
        self._subs: list[Callable[[dict], None]] = []
        self._error: str | None = None
        try:
            self._show = show_store.load()
        except ProjektorError as e:
            log.error("cannot load show: %s", e)
            self._show = Show()
            self._error = str(e)
        self._cue_index: int | None = None
        self._standby_index: int | None = 0 if self._show.cues else None
        self._status_screen = bool(settings.display.status_on_boot)
        self._last_command: dict | None = None
        self._transports: dict[str, Any] = {
            "osc": "stopped",
            "lora": "disabled" if not settings.lora.enabled else "absent",
        }
        player.set_callbacks(self._on_player_state, self._on_player_error)

    # ================================================================== lifecycle
    def start(self) -> None:
        """Start the player and draw the initial screen (on the player thread)."""
        self.executor.submit(self._do_start)

    def _do_start(self) -> None:
        self.player.start()
        self._sync_prepare()
        self._apply_status()
        self._notify()

    def shutdown(self) -> None:
        """Must be called on the player thread."""
        try:
            self.player.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("player shutdown failed")

    def _sync_prepare(self, discard: bool = False) -> None:
        """Preload the standby cue in the player (player thread); idempotent."""
        p = self.player
        if discard:
            p.discard_prepared()
        if not self.settings.player.preload_enabled():
            return
        with self._lock:
            si = self._standby_index
            show = self._show
        if si is None or si >= len(show.cues):
            p.discard_prepared()
            return
        try:
            clip = resolve_cue(show, si, self._media_dir())
            if p.prepared_clip != clip:
                p.prepare(clip)
        except Exception as e:  # noqa: BLE001 - preloading is best effort
            log.warning("preload failed: %s", e)

    def tick(self) -> bool:
        """Periodic (≈1 s, player thread): refresh status text, push position updates."""
        self._sync_prepare()
        self._apply_status()
        if self.player.state != "idle":
            self._notify()
        return True

    # ================================================================== helpers
    @property
    def show(self) -> Show:
        return self._show

    def _hide_boot_status(self) -> None:
        with self._lock:
            self._status_screen = False

    def _resolve_ref(self, ref: str | int) -> int:
        with self._lock:
            return resolve_cue_ref(self._show, ref)

    def _media_dir(self):
        return self.show_store.media_dir

    def _status_visible(self) -> bool:
        return self._status_screen and self.player.state == "idle"

    def _apply_status(self) -> None:
        visible = self._status_visible()
        text = ""
        if visible and self.status_builder is not None:
            try:
                text = self.status_builder(self.state())
            except Exception:  # noqa: BLE001
                log.exception("status text failed")
                text = "status unavailable"
        self.player.set_status(visible, text)

    # ================================================================== commands
    def go(self) -> None:
        p = self.player
        at_pp = p.state == "paused" and p.pause_reason == "pause_point"
        with self._lock:
            if not at_pp and self._standby_index is None:
                raise ProjektorError("no standby cue (end of cue list)")
        self._hide_boot_status()
        self.executor.submit(self._do_go)

    def _do_go(self) -> None:
        p = self.player
        if p.state == "paused" and p.pause_reason == "pause_point":
            p.resume()
        else:
            with self._lock:
                idx = self._standby_index
            if idx is None:
                log.warning("go: no standby cue")
                self._set_error("no standby cue (end of cue list)")
            else:
                self._run_cue(idx, force=True)
        self._sync_prepare()
        self._apply_status()
        self._notify()

    def cue(self, ref: str | int) -> None:
        idx = self._resolve_ref(ref)
        self._hide_boot_status()
        with self._lock:
            cue_id = self._show.cues[idx].id
        self.executor.submit(self._do_cue, cue_id)

    def _do_cue(self, cue_id: str) -> None:
        with self._lock:
            idx = next((i for i, c in enumerate(self._show.cues) if c.id == cue_id), None)
        if idx is None:
            self._set_error(f"cue {cue_id!r} vanished (show reloaded)")
        else:
            self._run_cue(idx, force=False)
        self._sync_prepare()
        self._apply_status()
        self._notify()

    def _run_cue(self, idx: int, force: bool) -> None:
        """Run cue idx (player thread). force=False: idempotent if already playing."""
        with self._lock:
            show = self._show
            cue = show.cues[idx]
            nxt = idx + 1 if idx + 1 < len(show.cues) else None
        p = self.player
        playing_same = (
            p.state in ("fading_in", "playing", "paused", "holding")
            and p.clip is not None
            and p.clip.cue_id == cue.id
            and self._cue_index == idx
        )
        if playing_same and not force:
            log.info("cue %s already playing (idempotent)", cue.id)
            with self._lock:
                self._standby_index = nxt
            return
        clip = resolve_cue(show, idx, self._media_dir())
        log.info("cue %s (#%d) -> video %s", cue.id, idx + 1, clip.video_id)
        with self._lock:
            self._cue_index = idx
            self._standby_index = nxt
            self._error = None
        p.play(clip)

    def standby(self, ref: str | int) -> None:
        idx = self._resolve_ref(ref)
        with self._lock:
            cue_id = self._show.cues[idx].id
        self.executor.submit(self._do_standby, cue_id)

    def _do_standby(self, cue_id: str) -> None:
        with self._lock:
            idx = next((i for i, c in enumerate(self._show.cues) if c.id == cue_id), None)
            if idx is not None:
                self._standby_index = idx
        self._sync_prepare()
        self._apply_status()
        self._notify()

    def play(self, video_id: str, fade_in: float | None = None) -> None:
        video_id = str(video_id)
        if fade_in is not None:
            fade_in = float(fade_in)
            if fade_in < 0:
                raise ProjektorError("fade_in must be >= 0")
        with self._lock:
            if video_id not in self._show.videos:
                raise ProjektorError(f"unknown video {video_id!r}")
        self._hide_boot_status()
        self.executor.submit(self._do_play, video_id, fade_in)

    def _do_play(self, video_id: str, fade_in: float | None) -> None:
        with self._lock:
            show = self._show
        try:
            clip = resolve_video(show, video_id, self._media_dir(), fade_in=fade_in)
        except ProjektorError as e:
            self._set_error(str(e))
        else:
            log.info("play video %s", video_id)
            with self._lock:
                self._cue_index = None
                self._error = None
            self.player.play(clip)
        self._sync_prepare()
        self._apply_status()
        self._notify()

    def pause(self) -> None:
        self._hide_boot_status()
        self.executor.submit(self._do_simple, "pause")

    def resume(self) -> None:
        self._hide_boot_status()
        self.executor.submit(self._do_simple, "resume")

    def stop(self) -> None:
        self._hide_boot_status()
        self.executor.submit(self._do_simple, "stop")

    def _do_simple(self, what: str) -> None:
        p = self.player
        if what == "pause":
            if p.state in ("fading_in", "playing"):
                p.pause()
        elif what == "resume":
            if p.state == "paused":
                p.resume()
        elif what == "stop":
            p.stop()
        self._sync_prepare()
        self._apply_status()
        self._notify()

    def fadeout(self, seconds: float | None = None) -> None:
        if seconds is not None:
            seconds = float(seconds)
            if seconds < 0:
                raise ProjektorError("fade time must be >= 0")
        self._hide_boot_status()
        self.executor.submit(self._do_fadeout, seconds)

    def _do_fadeout(self, seconds: float | None) -> None:
        p = self.player
        if p.state != "idle" and p.clip is not None:
            s = p.clip.fade_out if seconds is None else seconds
            p.fadeout(s)
        self._apply_status()
        self._notify()

    def seek(self, seconds: float) -> None:
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            raise ProjektorError(f"invalid seek time {seconds!r}") from None
        if seconds < 0:
            raise ProjektorError("seek time must be >= 0")
        self._hide_boot_status()
        self.executor.submit(self._do_seek, seconds)

    def _do_seek(self, seconds: float) -> None:
        if self.player.state in ACTIVE_STATES:
            self.player.seek(seconds)
        self._apply_status()
        self._notify()

    def show_status(self, on: bool) -> None:
        with self._lock:
            self._status_screen = bool(on)
        self.executor.submit(self._do_refresh)

    def _do_refresh(self) -> None:
        self._apply_status()
        self._notify()

    def reload_show(self) -> None:
        show = self.show_store.load()  # raises ProjektorError on invalid show
        self.executor.submit(self._do_reload, show)

    def _do_reload(self, show: Show) -> None:
        with self._lock:
            old = self._show

            def remap(i: int | None) -> int | None:
                if i is None or i >= len(old.cues):
                    return None
                cid = old.cues[i].id
                return next((j for j, c in enumerate(show.cues) if c.id == cid), None)

            had_standby = self._standby_index is not None
            new_sb = remap(self._standby_index)
            if new_sb is None and had_standby and show.cues:
                new_sb = min(self._standby_index, len(show.cues) - 1)
            if self._standby_index is None and old.cues == [] and show.cues:
                new_sb = 0
            self._cue_index = remap(self._cue_index)
            self._standby_index = new_sb
            self._show = show
        log.info("show reloaded: %d videos, %d cues", len(show.videos), len(show.cues))
        self._sync_prepare(discard=True)
        self._apply_status()
        self._notify()

    # ================================================================== transport/dispatcher hooks
    def record_command(self, address: str, args: list, source: str) -> None:
        with self._lock:
            self._last_command = {
                "address": address,
                "args": list(args),
                "source": source,
                "time": time.time(),
            }
        self.executor.submit(self._notify)

    def set_transport_status(self, name: str, value: str | Callable[[], str]) -> None:
        with self._lock:
            self._transports[name] = value
        self.executor.submit(self._notify)

    def _set_error(self, msg: str | None) -> None:
        with self._lock:
            self._error = msg

    # ================================================================== player callbacks (player thread)
    def _on_player_state(self, state: str, reason: str | None) -> None:
        log.debug("player state %s %s", state, reason or "")
        self._apply_status()
        self._notify()

    def _on_player_error(self, msg: str) -> None:
        self._set_error(msg)
        self._apply_status()
        self._notify()

    # ================================================================== queries
    def state(self) -> dict:
        p = self.player
        pstate = p.state
        clip = p.clip
        with self._lock:
            show = self._show
            ci = self._cue_index
            si = self._standby_index
            transports = {}
            for k, v in self._transports.items():
                if callable(v):
                    try:
                        v = v()
                    except Exception as e:  # noqa: BLE001
                        v = f"error: {e}"
                transports[k] = v
            return {
                "state": pstate,
                "pause_reason": p.pause_reason if pstate == "paused" else None,
                "cue": show.cues[ci].id if ci is not None and ci < len(show.cues) else None,
                "cue_index": ci,
                "standby": show.cues[si].id if si is not None and si < len(show.cues) else None,
                "standby_index": si,
                "prepared": pc.cue_id if (pc := p.prepared_clip) is not None else None,
                "video": clip.video_id if clip is not None and pstate != "idle" else None,
                "position": _round(p.position()) if pstate != "idle" else None,
                "duration": _round(p.duration()) if pstate != "idle" else None,
                "status_screen": self._status_screen,
                "last_command": dict(self._last_command) if self._last_command else None,
                "transports": transports,
                "error": self._error,
            }

    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs.remove(cb)
                except ValueError:
                    pass

        return unsubscribe

    def _notify(self) -> None:
        with self._lock:
            subs = list(self._subs)
        if not subs:
            return
        st = self.state()
        for cb in subs:
            try:
                cb(st)
            except Exception:  # noqa: BLE001
                log.exception("state subscriber failed")


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 3)
