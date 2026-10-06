"""HDMI-CEC control of the beamer (Android TV) through `cec-ctl` (v4l-utils).

Every call is a short-lived subprocess with a timeout; calls are serialised by a lock.
Methods are meant for OSC/web/helper threads, never the GLib/player thread. `background()`
runs a call in a worker thread for fire-and-forget commands.
See docs/beamer-control.md.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from . import ProjektorError

log = logging.getLogger(__name__)
PLAYBACK_LA = "4"  # Playback Device 1

TV = "0"          # logical address of the TV/beamer
BROADCAST = "15"
CMD_TIMEOUT_S = 6.0

# name -> cec-ctl ui-cmd
KEYS = {
    "up": "up", "down": "down", "left": "left", "right": "right", "select": "select",
    "back": "exit", "home": "root-menu", "menu": "setup-menu",
    "volume-up": "volume-up", "volume-down": "volume-down", "mute": "mute",
}
_PWR_RE = re.compile(r"pwr-state:\s*([a-z-]+)", re.I)
_PHYS_RE = re.compile(r"Physical Address\s*:\s*([0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f])", re.I)


def _cfg(settings: Any, key: str, default: Any) -> Any:
    sec = getattr(settings, "beamer", None)
    v = getattr(sec, key, None) if sec is not None else None
    return default if v is None else v


class BeamerError(ProjektorError):
    pass


class BeamerUnavailable(BeamerError):
    """Disabled, or cec-ctl not installed."""


class Beamer:
    def __init__(self, settings: Any):
        self.enabled = bool(_cfg(settings, "enabled", True))
        self.device = str(_cfg(settings, "device", "/dev/cec0"))
        self.phys_addr = str(_cfg(settings, "phys_addr", "1.0.0.0")).strip()
        self.power_on_at_boot = bool(_cfg(settings, "power_on_at_boot", False))
        self.standby_at_shutdown = bool(_cfg(settings, "standby_at_shutdown", False))
        self.cec_ctl = str(_cfg(settings, "cec_ctl", "cec-ctl"))
        # "standby": CEC <Standby> (XGIMI: full shutdown); "power-key": remote power key (XGIMI: sleep)
        self.off_method = str(_cfg(settings, "off_method", "power-key"))
        self.retry_gap_s = 1.0
        self.register_wait_s = 0.5
        self.wait_on_s = 45.0  # power_on: wait this long for "on", then re-send <Active Source>  # logical address claim (polling) after re-registering
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ plumbing
    def _run(self, *args: str, timeout: float = CMD_TIMEOUT_S) -> str:
        if not self.enabled:
            raise BeamerUnavailable("beamer control disabled (beamer.enabled = false)")
        if shutil.which(self.cec_ctl) is None:
            raise BeamerUnavailable(f"{self.cec_ctl} not found (apt install v4l-utils)")
        cmd = [self.cec_ctl, "-d", self.device, *args]
        with self._lock:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as e:
                raise BeamerError(f"cec-ctl timed out ({' '.join(args)})") from e
            except OSError as e:
                raise BeamerError(f"cec-ctl failed: {e}") from e
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            msg = out.strip().splitlines()[-1] if out.strip() else f"exit {p.returncode}"
            raise BeamerError(f"cec-ctl {' '.join(args)}: {msg}")
        return out

    def background(self, fn: Callable[..., Any], *args: Any) -> threading.Thread:
        """Run fn(*args) in a worker thread; errors are logged."""
        def work() -> None:
            try:
                fn(*args)
            except ProjektorError as e:
                log.warning("beamer: %s", e)
            except Exception:  # noqa: BLE001
                log.exception("beamer command failed")

        t = threading.Thread(target=work, name="beamer", daemon=True)
        self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()
        return t

    def join(self, timeout: float = 10.0) -> None:
        for t in list(self._threads):
            t.join(timeout)

    # ------------------------------------------------------------------ commands
    def setup(self) -> None:
        """Register as playback device (and set the physical address if configured)."""
        args = []
        if self.phys_addr:
            args += ["-p", self.phys_addr]
        self._run(*args, "--playback", "-o", "Projektor")

    def ensure_registered(self) -> None:
        """Pick the sender address for the next commands.

        When the beamer sleeps it drops HDMI hotplug and the kernel resets the Pi's CEC
        adapter to physical address f.f.f.f with no logical address (re-registering does
        not stick). Commands then go out from "Unregistered" (15), which the XGIMI ignores.
        Sending explicitly from Playback Device 1 (`--from 4`) wakes it (verified)."""
        try:
            out = self._run()
        except BeamerError:
            out = ""
        m = _PHYS_RE.search(out)
        lost = m is None or m.group(1).lower() == "f.f.f.f" or "Logical Address Mask       : 0x0000" in out
        if lost:
            try:
                self.setup()  # harmless; sticks if hotplug is up
            except BeamerError:
                pass
        self._from = ["--from", PLAYBACK_LA] if lost else []

    def _our_phys_addr(self) -> str:
        if self.phys_addr:
            return self.phys_addr
        m = _PHYS_RE.search(self._run())
        if not m:
            raise BeamerError("CEC physical address unknown (set beamer.phys_addr)")
        return m.group(1)

    def power_on(self) -> None:
        self.ensure_registered()
        last: BeamerError | None = None
        for i in range(3):  # CEC from standby can be slow
            try:
                self._run(*self._from, "--to", TV, "--image-view-on")
                last = None
                break
            except BeamerError as e:
                last = e
                if i < 2 and self.retry_gap_s > 0:
                    time.sleep(self.retry_gap_s)
        if last is not None:
            raise last
        self._sent_standby = False
        # Switch the beamer to our HDMI input. It boots for ~20 s after waking and may miss
        # an early <Active Source>, so repeat it once it reports power "on".
        if self.retry_gap_s > 0:
            time.sleep(self.retry_gap_s)  # spec: give the TV time between the two messages
        active = (*self._from, "--to", BROADCAST, "--active-source", f"phys-addr={self._our_phys_addr()}")
        self._run(*active)
        deadline = time.monotonic() + self.wait_on_s
        while time.monotonic() < deadline:
            if self.status() == "on":
                # Android may still be starting its HDMI input service: send right away and
                # repeat shortly after instead of one late attempt.
                for pause in (0.0, 1.0, 2.0):
                    time.sleep(pause)
                    self._run(*active)
                break
            time.sleep(1.0)

    def select_source(self) -> None:
        """Switch the beamer to the Pi's HDMI input (<Active Source>)."""
        self.ensure_registered()
        self._run(*self._from, "--to", BROADCAST, "--active-source", f"phys-addr={self._our_phys_addr()}")

    def standby(self) -> None:
        self.ensure_registered()
        if self.off_method == "power-key":
            # Like the remote's power button: XGIMI Halo+ goes to sleep (LED on) instead of
            # the full shutdown <Standby> triggers. Neither state can be woken over CEC.
            self._run(*self._from, "--to", TV, "--user-control-pressed", "ui-cmd=power-toggle-function")
            self._run(*self._from, "--to", TV, "--user-control-released")
        else:
            self._run(*self._from, "--to", TV, "--standby")
        self._sent_standby = True

    def status(self) -> str:
        """"on" | "standby" | "off" | "unknown" | "unavailable" (no tool/device/disabled).

        "off": no answer after our own standby command - some beamers (XGIMI Halo+) power
        their CEC controller down completely and cannot be woken over CEC from that state."""
        try:
            out = self._run(*getattr(self, "_from", []), "--to", TV, "--give-device-power-status")
        except BeamerUnavailable:
            return "unavailable"
        except BeamerError as e:
            if "Failed to open" in str(e):
                return "unavailable"
            return "off" if getattr(self, "_sent_standby", False) else "unknown"
        m = _PWR_RE.search(out)
        if not m:
            return "off" if getattr(self, "_sent_standby", False) else "unknown"
        s = m.group(1).lower()
        if s in ("on", "to-on"):
            return "on"
        if s in ("standby", "to-standby"):
            return "standby"
        return "unknown"

    def key(self, name: str) -> None:
        n = str(name).strip().lower().replace("_", "-")
        if n not in KEYS:
            raise BeamerError(f"unknown key {name!r} (one of: {', '.join(KEYS)})")
        self._run("--to", TV, "--user-control-pressed", f"ui-cmd={KEYS[n]}")
        self._run("--to", TV, "--user-control-released")
