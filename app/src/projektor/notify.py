"""Discord notifications (webhook). Everything here is best effort and must never raise into
callers, block the GLib/player thread or slow the show: events go through a bounded queue to a
worker thread; delivery uses urllib with a short timeout; undelivered events are kept in a small
persisted outbox (data_dir/.notify_outbox.jsonl) and retried with backoff and when the network
comes up. Empty ``[notify] discord_webhook`` disables everything.
"""

from __future__ import annotations

import collections
import json
import logging
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MAX_CHARS = 1900
OUTBOX_MAX = 200
QUEUE_MAX = 100
HTTP_TIMEOUT_S = 5.0
BACKOFF_S = (30, 60, 120, 240, 480, 900)
THROTTLE_BITS = {0x1: "undervoltage", 0x2: "ARM frequency capped", 0x4: "throttled", 0x8: "soft temperature limit"}
_MENTION_RE = re.compile(r"<@(!?)(\d+)>|<@&(\d+)>")


def mask_url(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url or "")
    return (m.group(1) + "/...") if m else "<webhook>"


def parse_mentions(mention: str) -> dict:
    users = [m.group(2) for m in _MENTION_RE.finditer(mention or "") if m.group(2)]
    roles = [m.group(3) for m in _MENTION_RE.finditer(mention or "") if m.group(3)]
    allowed: dict = {"parse": []}
    if users:
        allowed["users"] = users
    if roles:
        allowed["roles"] = roles
    return allowed


# ------------------------------------------------------------------ crash marker


def begin_run(data_dir: Path) -> float | None:
    """Write the .running marker. Returns the previous run's start time if it never ended cleanly."""
    marker = Path(data_dir) / ".running"
    prev: float | None = None
    try:
        if marker.exists():
            try:
                prev = float(marker.read_text().strip())
            except ValueError:
                prev = marker.stat().st_mtime
        marker.write_text(f"{time.time():.0f}\n")
    except OSError as e:
        log.warning("cannot update %s: %s", marker, e)
    return prev


def end_run(data_dir: Path) -> None:
    try:
        (Path(data_dir) / ".running").unlink()
    except OSError:
        pass


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ------------------------------------------------------------------ notifier


class Notifier:
    def __init__(self, settings, *, version: str = "", net_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic):
        cfg = getattr(settings, "notify", None)
        self.url = str(getattr(cfg, "discord_webhook", "") or "").strip()
        self.mention = str(getattr(cfg, "mention", "") or "").strip()
        self.min_interval = float(getattr(cfg, "min_interval_s", 30.0))
        self.device = str(getattr(getattr(settings, "device", None), "name", "projektor"))
        self.version = version
        self.data_dir = Path(settings.paths.data_dir)
        self.net_path = Path(net_path) if net_path else getattr(settings.paths, "net_status", None)
        self.outbox_path = self.data_dir / ".notify_outbox.jsonl"
        self.clock = clock
        self.http_timeout = HTTP_TIMEOUT_S
        self.backoff = BACKOFF_S
        self.poll_s = 1.0
        self._q: queue.Queue = queue.Queue(QUEUE_MAX)
        self._outbox: collections.deque = collections.deque(maxlen=OUTBOX_MAX)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._next_try = 0.0
        self._fails = 0
        self._stop = False
        self._thread: threading.Thread | None = None
        self._last_net: str | None = None
        self.start_time = time.time()
        self.sent = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # -------------------------------------------------------------- public API

    def start(self) -> "Notifier":
        if not self.enabled or self._thread:
            return self
        self._load_outbox()
        self._thread = threading.Thread(target=self._worker, name="projektor-notify", daemon=True)
        self._thread.start()
        return self

    def event(self, etype: str, level: str, title: str, details: str = "", *, force: bool = False) -> bool:
        """Queue an event (level: info|warning|error). Never raises or blocks. False = dropped."""
        try:
            if not self.enabled:
                return False
            now = self.clock()
            with self._lock:
                last = self._last.get(etype)
                if not force and last is not None and now - last < self.min_interval:
                    return False
                self._last[etype] = now
            self._q.put_nowait({"type": etype, "level": level, "title": title, "details": details,
                                "ts": time.time()})
            return True
        except Exception:  # noqa: BLE001  (queue full etc.)
            return False

    def test(self) -> str:
        if not self.enabled:
            return "disabled"
        return "queued" if self.event("test", "info", "Test notification", "triggered manually", force=True) else "dropped"

    def startup(self, prev_run_started: float | None) -> None:
        if prev_run_started is not None:
            self.event("crash", "error", "Crash detected",
                       f"previous run (started {_fmt_time(prev_run_started)}) did not shut down cleanly "
                       "(crash, kill or power loss)", force=True)
        self.event("startup", "info", "Projektor started", f"version {self.version}", force=True)

    def shutdown(self, budget_s: float = 2.0) -> None:
        """Send 'clean shutdown' best effort, waiting at most budget_s."""
        try:
            if not self.enabled or not self._thread:
                return
            self.http_timeout = min(self.http_timeout, budget_s)
            self.event("shutdown", "info", "Clean shutdown", f"uptime {self._uptime()}", force=True)
            end = time.monotonic() + budget_s
            while time.monotonic() < end:
                if self._q.empty() and not self._busy:
                    break
                time.sleep(0.05)
            self._stop = True
        except Exception:  # noqa: BLE001
            pass

    _busy = False

    def install_excepthooks(self) -> None:
        prev_sys, prev_thr = sys.excepthook, threading.excepthook

        def sys_hook(tp, val, tb):
            self._report_exc("main", tp, val, tb)
            prev_sys(tp, val, tb)

        def thr_hook(args):
            if args.exc_type is not SystemExit:
                self._report_exc(getattr(args.thread, "name", "?"), args.exc_type, args.exc_value, args.exc_traceback)
            prev_thr(args)

        sys.excepthook, threading.excepthook = sys_hook, thr_hook

    def _report_exc(self, where: str, tp, val, tb) -> None:
        try:
            tbs = "".join(traceback.format_exception(tp, val, tb))
            log.error("unhandled exception in %s: %s", where, tbs[-800:])
            self.event("exception", "error", f"Unhandled exception in thread {where}", "```\n" + tbs[-900:] + "\n```")
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- message

    def _uptime(self) -> str:
        s = int(time.time() - self.start_time)
        return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s"

    def _ip(self) -> str:
        try:
            d = json.loads(Path(self.net_path).read_text())
            return str(d.get("ip") or d.get("hotspot_ip") or "no ip") + (f" ({d['state']})" if d.get("state") else "")
        except (OSError, ValueError, AttributeError, TypeError):
            return "no ip"

    def build_payload(self, ev: dict) -> dict:
        level = ev["level"]
        ping = level in ("warning", "error") and self.mention
        head = f"[{level.upper()}] {ev['title']}"
        lines = [head, f"{self.device} | {socket.gethostname()} | {self._ip()}"]
        if ev.get("details"):
            lines.append(ev["details"])
        if time.time() - ev["ts"] > 60:
            lines.append(f"(event time {_fmt_time(ev['ts'])})")
        text = "\n".join(lines)
        if ping:
            text = f"{self.mention} {text}"
        if len(text) > MAX_CHARS:
            text = text[: MAX_CHARS - 4] + "\n..."
        payload = {"content": text, "allowed_mentions": parse_mentions(self.mention) if ping else {"parse": []}}
        return {"ts": ev["ts"], "type": ev["type"], "payload": payload}

    # -------------------------------------------------------------- delivery

    def _post(self, payload: dict) -> tuple[str, float]:
        """Returns (result, retry_after): ok | retry | drop | ratelimit."""
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "projektor-notify/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                r.read(256)
            return "ok", 0.0
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = 5.0
                try:
                    ra = float(json.loads(e.read(2048)).get("retry_after", ra))
                except Exception:  # noqa: BLE001
                    try:
                        ra = float(e.headers.get("Retry-After", ra))
                    except Exception:  # noqa: BLE001
                        pass
                return "ratelimit", max(0.5, min(ra, 3600.0))
            log.warning("notify: webhook %s answered HTTP %d", mask_url(self.url), e.code)
            return ("retry" if e.code >= 500 else "drop"), 0.0
        except Exception as e:  # noqa: BLE001  (URLError, timeout, ssl, invalid url ...)
            log.info("notify: cannot reach %s: %s", mask_url(self.url), type(e).__name__)
            return "retry", 0.0

    def _load_outbox(self) -> None:
        try:
            for line in self.outbox_path.read_text().splitlines()[-OUTBOX_MAX:]:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and "payload" in item:
                        self._outbox.append(item)
                except ValueError:
                    pass
        except OSError:
            pass

    def _persist(self) -> None:
        try:
            if not self._outbox:
                self.outbox_path.unlink(missing_ok=True)
                return
            tmp = self.outbox_path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(i) + "\n" for i in self._outbox))
            tmp.replace(self.outbox_path)
        except OSError as e:
            log.warning("notify: cannot write outbox: %s", e)

    def _net_up(self) -> bool:
        try:
            st = json.loads(Path(self.net_path).read_text()).get("state")
        except (OSError, ValueError, AttributeError, TypeError):
            st = None
        up = st == "connected" and self._last_net not in (None, "connected")
        self._last_net = st
        return up

    def _worker(self) -> None:
        while not self._stop:
            try:
                ev = self._q.get(timeout=self.poll_s)
            except queue.Empty:
                ev = None
            try:
                if ev is not None:
                    self._busy = True
                    self._outbox.append(self.build_payload(ev))
                    while True:  # drain the rest
                        try:
                            self._outbox.append(self.build_payload(self._q.get_nowait()))
                        except queue.Empty:
                            break
                if self._net_up() and self._outbox:
                    self._next_try = 0.0
                self._busy = bool(self._outbox)
                self._flush()
            except Exception:  # noqa: BLE001
                log.exception("notify worker error")
            finally:
                self._busy = bool(self._outbox) and self.clock() >= self._next_try

    def _flush(self) -> None:
        changed = False
        while self._outbox and self.clock() >= self._next_try and not self._stop:
            res, ra = self._post(self._outbox[0]["payload"])
            if res in ("ok", "drop"):
                self._outbox.popleft()
                self._fails = 0
                self.sent += res == "ok"
                changed = True
            elif res == "ratelimit":
                self._next_try = self.clock() + ra
                break
            else:
                self._fails += 1
                self._next_try = self.clock() + self.backoff[min(self._fails, len(self.backoff)) - 1]
                break
        if changed or self._outbox:
            self._persist()


# ------------------------------------------------------------------ monitors


def read_temp_c(path: str | Path = "/sys/class/thermal/thermal_zone0/temp") -> float | None:
    try:
        return int(Path(path).read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


def read_throttled(path: str | Path = "/sys/devices/platform/soc/soc:firmware/get_throttled",
                   vcgencmd: bool = True) -> int | None:
    try:
        return int(Path(path).read_text().strip(), 16)
    except (OSError, ValueError):
        pass
    if vcgencmd:
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            return int(out.split("=")[1].strip(), 16)
        except Exception:  # noqa: BLE001
            pass
    return None


class Monitors:
    """Polls temperature / throttling (10 s) and the LoRa link (5 s) in one daemon thread."""

    def __init__(self, notifier: Notifier, settings, *, lora_status: Callable[[], str] | None = None,
                 temp_path: str | Path = "/sys/class/thermal/thermal_zone0/temp",
                 throttled_path: str | Path = "/sys/devices/platform/soc/soc:firmware/get_throttled",
                 use_vcgencmd: bool = True):
        cfg = getattr(settings, "notify", None)
        self.n = notifier
        self.warn = float(getattr(cfg, "temp_warn_c", 75.0))
        self.hyst = float(getattr(cfg, "temp_hysteresis_c", 5.0))
        self.lora_status = lora_status
        self.temp_path, self.throttled_path, self.use_vcgencmd = temp_path, throttled_path, use_vcgencmd
        self.hot = False
        self.flags = 0
        self.lora_ok: bool | None = None
        self._stop = threading.Event()

    def check_temp(self) -> None:
        t = read_temp_c(self.temp_path)
        if t is None:
            return
        if not self.hot and t >= self.warn:
            self.hot = True
            self.n.event("temp_high", "warning", "CPU over-temperature", f"{t:.1f} C (limit {self.warn:g} C)")
        elif self.hot and t < self.warn - self.hyst:
            self.hot = False
            self.n.event("temp_ok", "info", "CPU temperature back to normal", f"{t:.1f} C", force=True)

    def check_throttled(self) -> None:
        v = read_throttled(self.throttled_path, self.use_vcgencmd)
        if v is None:
            return
        cur = v & 0xF
        new = cur & ~self.flags
        self.flags = cur
        if new:
            names = ", ".join(n for b, n in THROTTLE_BITS.items() if new & b)
            self.n.event("throttled", "warning", f"Power/thermal problem: {names}", f"get_throttled={v:#x}")

    def check_lora(self) -> None:
        if self.lora_status is None:
            return
        try:
            st = str(self.lora_status())
        except Exception:  # noqa: BLE001
            return
        if st == "disabled":
            return
        ok = st.startswith("connected")
        if self.lora_ok is not None and ok != self.lora_ok:
            if ok:
                self.n.event("lora_up", "info", "LoRa adapter back", st, force=True)
            else:
                self.n.event("lora_down", "warning", "LoRa adapter dropped", st, force=True)
        self.lora_ok = ok

    def start(self) -> "Monitors":
        if self.n.enabled:
            threading.Thread(target=self._loop, name="projektor-monitors", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        n = 0
        while not self._stop.wait(5.0):
            try:
                self.check_lora()
                if n % 2 == 0:
                    self.check_temp()
                    self.check_throttled()
            except Exception:  # noqa: BLE001
                log.exception("monitor error")
            n += 1
