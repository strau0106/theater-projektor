"""OSC address -> Controller dispatcher with cross-transport dedup (ARCHITECTURE.md §5, §7).

All transports (OSC/UDP, LoRa) and the web "test command" box go through
`CommandDispatcher.handle(address, args, source) -> replies`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from . import ProjektorError
from .config import Settings

log = logging.getLogger(__name__)

Reply = tuple[str, list]

PREFIX = "/proj/"


# --------------------------------------------------------------------------- arg coercion


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise ProjektorError(f"{what}: expected a number, got {v!r}")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            pass
    raise ProjektorError(f"{what}: expected a number, got {v!r}")


def coerce_float(v: Any, what: str = "argument") -> float:
    f = _num(v, what)
    if f != f or f in (float("inf"), float("-inf")):
        raise ProjektorError(f"{what}: invalid number {v!r}")
    return f


def coerce_ref(v: Any) -> str | int:
    """Cue ref: int (1-based number) or str (cue id). Integral floats become ints."""
    if isinstance(v, bool):
        raise ProjektorError(f"cue ref: invalid value {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        # e.g. QLab cue number 1.5 -> look it up as id "1.5"
        return repr(v)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        s = v.strip()
        if not s:
            raise ProjektorError("cue ref: empty")
        return s
    raise ProjektorError(f"cue ref: invalid value {v!r}")


def coerce_str(v: Any, what: str) -> str:
    if isinstance(v, bool):
        raise ProjektorError(f"{what}: invalid value {v!r}")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str) and v.strip():
        return v.strip()
    raise ProjektorError(f"{what}: invalid value {v!r}")


def coerce_bool(v: Any, what: str = "argument") -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "on", "true", "yes", "show"):
            return True
        if s in ("0", "off", "false", "no", "hide"):
            return False
        try:
            return float(s) != 0
        except ValueError:
            pass
    raise ProjektorError(f"{what}: expected 0 or 1, got {v!r}")


def _dedup_key(address: str, args: list) -> tuple:
    norm = []
    for a in args:
        if isinstance(a, bytes):
            a = a.decode("utf-8", "replace")
        if isinstance(a, (list, tuple)):
            a = tuple(a)
        norm.append(a if isinstance(a, (str, int, float, bool, tuple, type(None))) else repr(a))
    return (address, tuple(norm))


# --------------------------------------------------------------------------- dispatcher


class CommandDispatcher:
    def __init__(self, controller, settings: Settings, clock: Callable[[], float] = time.monotonic,
                 beamer=None):
        from .beamer import Beamer

        self.beamer = beamer if beamer is not None else Beamer(settings)
        self.controller = controller
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._recent: dict[tuple, tuple[float, str]] = {}
        self._handlers: dict[str, Callable[[list], list[Reply]]] = {
            "go": self._go,
            "cue": self._cue,
            "standby": self._standby,
            "play": self._play,
            "pause": self._pause,
            "resume": self._resume,
            "fadeout": self._fadeout,
            "stop": self._stop,
            "seek": self._seek,
            "status": self._status,
            "reload": self._reload,
            "ping": self._ping,
            "state": self._state,
            "beamer/power": self._beamer_power,
            "beamer/status": self._beamer_status,
            "beamer/key": self._beamer_key,
        }

    # ------------------------------------------------------------------ dedup
    def _is_duplicate(self, address: str, args: list, source: str) -> bool:
        window = float(self.settings.osc.dedup_window_s)
        if window <= 0:
            return False
        key = _dedup_key(address, args)
        now = self.clock()
        with self._lock:
            # prune
            if len(self._recent) > 256:
                self._recent = {k: v for k, v in self._recent.items() if now - v[0] <= window}
            prev = self._recent.get(key)
            if prev is not None and prev[1] != source and now - prev[0] <= window:
                return True
            self._recent[key] = (now, source)
        return False

    # ------------------------------------------------------------------ entry point
    def handle(self, address: str, args: list | tuple | None, source: str) -> list[Reply]:
        args = list(args or [])
        try:
            if not isinstance(address, str) or not address.startswith(PREFIX):
                raise ProjektorError(f"unknown address {address!r}")
            name = address[len(PREFIX):].strip("/")
            handler = self._handlers.get(name)
            if handler is None:
                raise ProjektorError(f"unknown address {address!r}")
            if self._is_duplicate(address, args, source):
                log.info("dedup: dropped %s %s from %s", address, args, source)
                return []
            log.info("command %s %s (%s)", address, args, source)
            try:
                self.controller.record_command(address, args, source)
            except Exception:  # noqa: BLE001
                log.exception("record_command failed")
            return handler(args)
        except ProjektorError as e:
            log.warning("command error: %s %s (%s): %s", address, args, source, e)
            return [("/proj/error", [str(e)])]
        except Exception as e:  # noqa: BLE001
            log.exception("command failed: %s %s", address, args)
            return [("/proj/error", [f"internal error: {e}"])]

    # ------------------------------------------------------------------ handlers
    @staticmethod
    def _nargs(args: list, lo: int, hi: int, usage: str) -> None:
        if not (lo <= len(args) <= hi):
            raise ProjektorError(f"usage: {usage}")

    def _go(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 0, "/proj/go")
        self.controller.go()
        return []

    def _cue(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 1, "/proj/cue <ref>")
        self.controller.cue(coerce_ref(args[0]))
        return []

    def _standby(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 1, "/proj/standby <ref>")
        self.controller.standby(coerce_ref(args[0]))
        return []

    def _play(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 2, "/proj/play <video_id> [fade_in_s]")
        vid = coerce_str(args[0], "video_id")
        fade = coerce_float(args[1], "fade_in") if len(args) > 1 else None
        self.controller.play(vid, fade)
        return []

    def _pause(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 0, "/proj/pause")
        self.controller.pause()
        return []

    def _resume(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 0, "/proj/resume")
        self.controller.resume()
        return []

    def _fadeout(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 1, "/proj/fadeout [seconds]")
        secs = coerce_float(args[0], "seconds") if args else None
        self.controller.fadeout(secs)
        return []

    def _stop(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 0, "/proj/stop")
        self.controller.stop()
        return []

    def _seek(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 1, "/proj/seek <seconds>")
        self.controller.seek(coerce_float(args[0], "seconds"))
        return []

    def _status(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 1, "/proj/status <0|1>")
        self.controller.show_status(coerce_bool(args[0], "status"))
        return []

    def _reload(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 0, "/proj/reload")
        self.controller.reload_show()
        return []

    def _ping(self, args: list) -> list[Reply]:
        return [("/proj/pong", [self.settings.device.name])]

    def _state(self, args: list) -> list[Reply]:
        return [("/proj/state", [json.dumps(self.controller.state(), separators=(",", ":"))])]

    # beamer (HDMI-CEC); power/key run in a worker thread so OSC/LoRa threads never wait on CEC
    def _beamer_power(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 1, "/proj/beamer/power <on|off|1|0>")
        if coerce_bool(args[0], "power"):
            self.beamer.background(self.beamer.power_on)
        else:
            self.beamer.background(self.beamer.standby)
        return []

    def _beamer_status(self, args: list) -> list[Reply]:
        self._nargs(args, 0, 0, "/proj/beamer/status")
        return [("/proj/beamer/status", [self.beamer.status()])]

    def _beamer_key(self, args: list) -> list[Reply]:
        self._nargs(args, 1, 1, "/proj/beamer/key <name>")
        name = coerce_str(args[0], "key")
        from .beamer import KEYS

        if name.lower().replace("_", "-") not in KEYS:
            raise ProjektorError(f"unknown key {name!r} (one of: {', '.join(KEYS)})")
        self.beamer.background(self.beamer.key, name)
        return []
