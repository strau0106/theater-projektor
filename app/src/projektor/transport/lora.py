"""LoRa serial transport (Pi side) and a reconnecting serial link shared with the bridge.

Data path: Waveshare USB-TO-LoRa adapter (transparent stream mode) -> serial bytes ->
``FrameDecoder`` -> ``SeqDedup`` -> OSC parse (bundles expanded) ->
``dispatcher.handle(address, args, "lora")`` -> replies encoded with the reply flag and
written back to the adapter (best effort, sent once).
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time
from typing import Any, Callable

from .. import lora_proto as lp

log = logging.getLogger("projektor.lora")

POLL_S = 2.0
_GLOB_CHARS = set("*?[")


def resolve_device(pattern: str) -> str | None:
    """Return the first existing device matching ``pattern`` (glob allowed), or None."""
    if not pattern:
        return None
    if _GLOB_CHARS & set(pattern):
        matches = sorted(glob.glob(pattern))
        return matches[0] if matches else None
    return pattern if os.path.exists(pattern) else None


class SerialLink:
    """Serial port that (re)opens itself and hands received bytes to ``on_data``.

    A background thread polls for the device every ``poll_s`` seconds while it is
    absent, reads while it is present, and reconnects after unplug/replug.
    ``status()`` is one of "connected <path>", "absent", "error: <msg>", "stopped".
    """

    def __init__(
        self,
        pattern: str,
        baud: int,
        on_data: Callable[[bytes], None],
        *,
        poll_s: float = POLL_S,
        on_connect: Callable[[str], None] | None = None,
        name: str = "lora-serial",
        serial_factory: Callable[..., Any] | None = None,
    ):
        self.pattern = pattern
        self.baud = baud
        self.poll_s = poll_s
        self._on_data = on_data
        self._on_connect = on_connect
        self._name = name
        self._serial_factory = serial_factory
        self._ser: Any = None
        self._path: str | None = None
        self._status = "absent"
        self._stop = threading.Event()
        self._wlock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    # -- public -----------------------------------------------------------------

    def start(self) -> "SerialLink":
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._status = "stopped"

    def status(self) -> str:
        return self._status

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def write(self, data: bytes) -> bool:
        """Write bytes; returns False if not connected or the write failed."""
        with self._wlock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(data)
                ser.flush()
                return True
            except Exception as exc:  # serial.SerialException, OSError
                log.warning("%s: write failed: %s", self._name, exc)
                self._drop(f"write failed: {exc}")
                return False

    def wait_connected(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._ser is not None:
                return True
            time.sleep(0.02)
        return self._ser is not None

    # -- internals ----------------------------------------------------------------

    def _open(self, path: str) -> Any:
        if self._serial_factory is not None:
            return self._serial_factory(path, self.baud)
        import serial  # pyserial

        ser = serial.Serial(path, self.baud, timeout=0.1, write_timeout=1.0)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        return ser

    def _close(self) -> None:
        with self._wlock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _drop(self, reason: str) -> None:
        """Called on I/O error: close and let the run loop reconnect."""
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self._status = "absent"
        if reason != self._last_error:
            log.warning("%s: %s lost (%s), waiting for device", self._name, self._path, reason)
            self._last_error = reason

    def _run(self) -> None:
        while not self._stop.is_set():
            path = resolve_device(self.pattern)
            if path is None:
                if self._status != "absent":
                    log.info("%s: no device matching %s", self._name, self.pattern)
                self._status = "absent"
                self._stop.wait(self.poll_s)
                continue
            try:
                ser = self._open(path)
            except Exception as exc:
                msg = f"error: {exc}"
                if self._status != msg:
                    log.warning("%s: cannot open %s: %s", self._name, path, exc)
                self._status = msg
                self._stop.wait(self.poll_s)
                continue
            if self._stop.is_set():
                ser.close()
                break
            with self._wlock:
                self._ser = ser
            self._path = path
            self._last_error = None
            real = os.path.realpath(path)
            self._status = f"connected {real}"
            log.info("%s: connected %s%s", self._name, path, "" if real == path else f" ({real})")
            if self._on_connect:
                try:
                    self._on_connect(path)
                except Exception:
                    log.exception("%s: on_connect failed", self._name)
            self._read_loop(ser, path)
        self._close()

    def _read_loop(self, ser: Any, path: str) -> None:
        last_check = time.monotonic()
        while not self._stop.is_set() and self._ser is ser:
            try:
                n = getattr(ser, "in_waiting", 0) or 1
                data = ser.read(n)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self._drop(str(exc) or exc.__class__.__name__)
                self._stop.wait(min(self.poll_s, 0.5))
                return
            if data:
                try:
                    self._on_data(bytes(data))
                except Exception:
                    log.exception("%s: data handler failed", self._name)
            now = time.monotonic()
            if now - last_check >= self.poll_s:
                last_check = now
                # some drivers (macOS) keep a stale fd after unplug: check the node
                if not os.path.exists(path):
                    self._drop("device node disappeared")
                    return


class LoraTransport:
    """Pi-side LoRa receiver. Use ``start_lora()``."""

    def __init__(
        self,
        settings: Any,
        dispatcher: Any,
        controller: Any = None,
        *,
        poll_s: float = POLL_S,
        dedup_window_s: float = 10.0,
    ):
        self.settings = settings
        self.dispatcher = dispatcher
        self.controller = controller
        lora = settings.lora
        self.enabled = bool(getattr(lora, "enabled", False))
        self.pattern = str(getattr(lora, "device", "") or "")
        self.baud = int(getattr(lora, "baud", 115200) or 115200)
        self.reply_delay_s = int(getattr(lora, "reply_delay_ms", 250)) / 1000.0
        self.sender_repeat = max(1, int(getattr(lora, "sender_repeat", 2)))
        self.decoder = lp.FrameDecoder(on_error=lambda m: log.debug("lora: dropped frame: %s", m))
        self.dedup = lp.SeqDedup(dedup_window_s)
        self.seq = lp.SeqCounter()
        self.link: SerialLink | None = None
        if self.enabled:
            self.link = SerialLink(
                self.pattern, self.baud, self._on_data, poll_s=poll_s,
                on_connect=lambda _p: self.decoder.reset(), name="lora",
            )

    def start(self) -> "LoraTransport":
        if self.link is None:
            log.info("lora: disabled in settings")
        else:
            log.info("lora: waiting for %s @ %d baud", self.pattern, self.baud)
            self.link.start()
        return self

    def stop(self) -> None:
        if self.link is not None:
            self.link.stop()

    def status(self) -> str:
        if self.link is None:
            return "disabled"
        st = self.link.status()
        return "absent" if st == "stopped" else st

    # -- receive path -------------------------------------------------------------

    def _on_data(self, data: bytes) -> None:
        for frame in self.decoder.feed(data):
            self._on_frame(frame)

    def _on_frame(self, frame: lp.Frame) -> None:
        if frame.is_reply:
            log.debug("lora: ignoring reply frame seq=%d", frame.seq)
            return
        if not self.dedup.is_new(frame.seq):
            log.debug("lora: duplicate seq=%d dropped", frame.seq)
            return
        try:
            messages = lp.parse_osc(frame.payload)
        except ValueError as exc:
            log.warning("lora: seq=%d: %s", frame.seq, exc)
            return
        for address, args in messages:
            log.info("lora: seq=%d %s", frame.seq, lp.format_msg(address, args))
            try:
                replies = self.dispatcher.handle(address, args, "lora") or []
            except Exception:
                log.exception("lora: dispatcher failed for %s", address)
                continue
            if not replies:
                continue
            # Half duplex: wait until the sender's remaining copies of this frame are off air.
            wire_len = len(lp.encode_frame(frame.seq, frame.payload))
            delay = self.reply_delay_s + (self.sender_repeat - 1) * 2 * lp.frame_airtime_s(wire_len)
            for r_addr, r_args in replies:
                self._send_reply_later(delay, r_addr, list(r_args))

    def _send_reply_later(self, delay_s: float, address: str, args: list) -> None:
        if delay_s <= 0:
            self.send_reply(address, args)
            return
        t = threading.Timer(delay_s, self.send_reply, (address, args))
        t.daemon = True
        t.start()

    def send_reply(self, address: str, args: list) -> bool:
        """Send one reply over LoRa (best effort, once). Oversized replies are dropped."""
        if self.link is None:
            return False
        try:
            payload = lp.build_osc(address, args)
            seq = self.seq.next()
            wire = lp.encode_frame(seq, payload, flags=lp.FLAG_REPLY)
        except lp.FrameTooLarge as exc:
            log.info("lora: reply %s not sent over LoRa: %s", address, exc)
            return False
        except Exception as exc:
            log.warning("lora: cannot encode reply %s: %s", address, exc)
            return False
        ok = self.link.write(wire)
        log.info("lora: reply seq=%d %s%s", seq, lp.format_msg(address, args), "" if ok else " (not sent)")
        return ok


def start_lora(settings: Any, dispatcher: Any, controller: Any = None) -> LoraTransport:
    """Start the LoRa transport. Returns a handle with ``.stop()`` and ``.status()``.

    status(): "connected /dev/ttyX" | "absent" | "disabled" | "error: ..."
    """
    return LoraTransport(settings, dispatcher, controller).start()
