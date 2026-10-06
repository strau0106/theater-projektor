"""Instant ("auto") backup: debounced, coalescing, retrying wrapper around BackupManager (§9).

``trigger()`` is cheap and never raises or blocks; a worker thread runs the job after
``auto_delay_s``. A persisted flag ``data_dir/.backup_pending`` survives restarts and is
cleared only after a successful run. Failures (offline, rsync error) retry with backoff
30 s, 1 min, 2 min ... max 15 min, and immediately when net.json turns "connected".
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

BACKOFF_S = (30, 60, 120, 240, 480, 900)


def backoff_s(failures: int) -> float:
    return BACKOFF_S[min(max(failures, 1), len(BACKOFF_S)) - 1]


def _default_runner(manager) -> Callable[[], None]:
    def run() -> None:
        manager.backup_now(wait=True)
        st = manager.status()
        if st.get("state") != "done":
            raise RuntimeError(st.get("message") or f"backup {st.get('state')}")
    return run


class AutoBackup:
    def __init__(self, manager, settings, *, runner: Callable[[], None] | None = None,
                 net_state: Callable[[], str | None] | None = None, poll_s: float = 1.0,
                 clock: Callable[[], float] = time.monotonic):
        self.manager = manager
        cfg = getattr(settings, "backup", None)
        self.auto = bool(getattr(cfg, "auto", True))
        self.delay = float(getattr(cfg, "auto_delay_s", 10.0))
        self.flag = Path(settings.paths.data_dir) / ".backup_pending"
        self.net_path = getattr(settings.paths, "net_status", None)
        self.runner = runner or _default_runner(manager)
        self._net_state = net_state or self._read_net
        self.poll_s = poll_s
        self.clock = clock
        self._cv = threading.Condition()
        self._due: float | None = None
        self._gen = 0           # bumped by every trigger
        self._failures = 0
        self._running = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self._last_net: str | None = None
        self._last_ok: float | None = None
        self._last_fail: float | None = None
        self._last_error = ""
        self._next_wall: float | None = None

    # ---------------------------------------------------------------- public

    @property
    def active(self) -> bool:
        try:
            return bool(self.auto and self.manager.enabled and self.manager.configured)
        except Exception:  # noqa: BLE001
            return False

    @property
    def pending(self) -> bool:
        try:
            return self.flag.exists()
        except OSError:
            return False

    def start(self) -> "AutoBackup":
        if not self.active or self._thread:
            return self
        if self.pending:
            self._schedule(self.delay)
        self._thread = threading.Thread(target=self._loop, name="projektor-autobackup", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def trigger(self, reason: str = "") -> None:
        """Something changed: back up after the debounce delay. Never raises."""
        try:
            if not self.active:
                return
            try:
                self.flag.touch()
            except OSError as e:
                log.warning("cannot write %s: %s", self.flag, e)
            with self._cv:
                self._gen += 1
                if not self._running:
                    self._set_due(self.clock() + self.delay)
                self._cv.notify_all()
        except Exception:  # noqa: BLE001
            log.exception("auto-backup trigger failed")

    def status(self) -> dict:
        with self._cv:
            nxt = self._next_wall
            return {"enabled": self.auto, "active": self.active, "pending": self.pending,
                    "running": self._running, "failures": self._failures,
                    "last_success": self._last_ok, "last_failure": self._last_fail,
                    "last_error": self._last_error, "next_retry": nxt}

    # ---------------------------------------------------------------- worker

    def _schedule(self, delay: float) -> None:
        with self._cv:
            self._set_due(self.clock() + delay)
            self._cv.notify_all()

    def _set_due(self, due: float | None) -> None:  # call with lock held (or before start)
        self._due = due
        self._next_wall = None if due is None else time.time() + max(0.0, due - self.clock())

    def _read_net(self) -> str | None:
        import json

        try:
            return json.loads(Path(self.net_path).read_text()).get("state")
        except (OSError, ValueError, AttributeError, TypeError):
            return None

    def _loop(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
                now = self.clock()
                due = self._due
                if due is None or now < due:
                    self._cv.wait(timeout=self.poll_s if due is None else min(self.poll_s, due - now))
                    go = False
                else:
                    go = True
            self._net_check()
            if go:
                self._run_job()

    def _net_check(self) -> None:
        try:
            st = self._net_state()
        except Exception:  # noqa: BLE001
            return
        if st == "connected" and self._last_net not in (None, "connected") and self.pending and not self._running:
            log.info("network is up: retrying pending backup")
            self._schedule(0)
        self._last_net = st

    def _run_job(self) -> None:
        with self._cv:
            self._running = True
            gen = self._gen
            self._set_due(None)
        err = ""
        try:
            self.runner()
        except Exception as e:  # noqa: BLE001  (offline, rsync error, busy, ...)
            err = str(e) or type(e).__name__
        with self._cv:
            self._running = False
            if not err:
                self._failures = 0
                self._last_ok = time.time()
                self._last_error = ""
                if self._gen == gen:
                    try:
                        self.flag.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        log.warning("cannot remove %s: %s", self.flag, e)
                else:  # changed while running: run once more
                    self._set_due(self.clock() + self.delay)
            else:
                self._failures += 1
                self._last_fail = time.time()
                self._last_error = err[:300]
                wait = backoff_s(self._failures)
                self._set_due(self.clock() + wait)
                log.warning("auto-backup failed (%s); retry in %ds", self._last_error, wait)
