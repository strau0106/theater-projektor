"""`projektor-bridge`: OSC/UDP from QLab -> OSC-over-LoRa on a serial adapter.

Runs on the QLab Mac (also works on Linux). Every received OSC packet gets a new
sequence number, is framed (lora_proto) and written to the Waveshare adapter
``--repeat`` times, spaced at least one airtime apart so each copy becomes its own
LoRa packet. Optionally the same OSC packet is also forwarded over UDP/WiFi to the
Pi (``--wifi-target``); the Pi drops whichever copy arrives second (cross-transport
dedup, ARCHITECTURE.md section 5). Replies from the Pi (LoRa reply frames, and UDP
replies to the WiFi forward) are sent to ``--reply-to`` or the last QLab sender.

Example::

    projektor-bridge --serial '/dev/cu.usbmodem*' --wifi-target 192.168.1.42:9000
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import queue
import select
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import lora_proto as lp
from .transport.lora import SerialLink, resolve_device

log = logging.getLogger("projektor.bridge")

DUTY_WINDOW_S = 3600.0
RESULTS_TIMEOUT_S = 3.0


def fetch_results(url: str, timeout: float = RESULTS_TIMEOUT_S) -> tuple[int, int, int]:
    """GET the vote website's /api/results -> (ja, nein, poll_id). Raises ValueError (message for QLab)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "projektor-bridge", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1 << 16)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"results: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError(f"results: {getattr(exc, 'reason', exc)}") from exc
    try:
        d = json.loads(body)
        ja, nein, poll = int(d["ja"]), int(d["nein"]), int(d.get("poll_id", 0))
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"results: bad response ({exc})") from exc
    if min(ja, nein, poll) < 0:
        raise ValueError("results: negative numbers in response")
    return ja, nein, poll


def parse_hostport(text: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    text = text.strip()
    if text.startswith("["):  # [ipv6]:port
        host, _, port = text[1:].partition("]:")
    elif ":" in text:
        host, _, port = text.rpartition(":")
    else:
        host, port = default_host, text
    if not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    return host or default_host, int(port)


class DutyCycle:
    """Sliding-window airtime accounting (EU868 g1 sub-band: 1 % over one hour)."""

    def __init__(self, limit: float = 0.01, window_s: float = DUTY_WINDOW_S, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._events: collections.deque[tuple[float, float]] = collections.deque()
        self._sum = 0.0

    def _expire(self, now: float) -> None:
        while self._events and self._events[0][0] < now - self.window_s:
            self._sum -= self._events.popleft()[1]
        if not self._events:
            self._sum = 0.0  # no float drift

    def used_s(self) -> float:
        self._expire(self._clock())
        return max(self._sum, 0.0)

    def budget_s(self) -> float:
        return self.limit * self.window_s

    def would_exceed(self, airtime_s: float) -> bool:
        return self.used_s() + airtime_s > self.budget_s()

    def add(self, airtime_s: float) -> None:
        now = self._clock()
        self._expire(now)
        self._events.append((now, airtime_s))
        self._sum += airtime_s


class Bridge:
    def __init__(
        self,
        serial: str,
        baud: int = 115200,
        listen: tuple[str, int] = ("0.0.0.0", 9001),
        repeat: int = 2,
        repeat_gap_ms: float | None = None,  # None = auto (airtime + margin)
        wifi_target: tuple[str, int] | None = None,
        reply_to: tuple[str, int] | None = None,
        sf: int = 7,
        bw_khz: int = 500,
        cr: int = 1,
        duty_limit: float = 0.01,
        poll_s: float = 2.0,
        results_url: str | None = None,
    ):
        self.results_url = results_url
        self.serial_pattern = serial
        self.baud = baud
        self.listen = listen
        self.repeat = max(1, int(repeat))
        self.repeat_gap_ms = repeat_gap_ms
        self.wifi_target = wifi_target
        self.reply_to = reply_to
        self.sf, self.bw_hz, self.cr = sf, bw_khz * 1000, cr
        self.duty = DutyCycle(duty_limit)
        self.seq = lp.SeqCounter()
        self.decoder = lp.FrameDecoder(on_error=lambda m: log.debug("serial: dropped frame: %s", m))
        self.reply_dedup = lp.SeqDedup(10.0)
        self.link = SerialLink(serial, baud, self._on_serial_data, poll_s=poll_s,
                               on_connect=lambda _p: self.decoder.reset(), name="bridge-serial")
        self._queue: queue.Queue[tuple[int, bytes, str] | None] = queue.Queue()
        self._stop = threading.Event()
        self._last_sender: tuple[str, int] | None = None
        self._threads: list[threading.Thread] = []
        self._duty_warned = 0.0
        self.sock_in: socket.socket | None = None
        self.sock_wifi: socket.socket | None = None
        self.sent_frames = 0  # for tests / stats

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> "Bridge":
        self.sock_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock_in.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock_in.bind(self.listen)
        self.listen = self.sock_in.getsockname()[:2]
        if self.wifi_target:
            self.sock_wifi = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock_wifi.bind(("0.0.0.0", 0))
        self.link.start()
        for target, name in ((self._udp_loop, "bridge-udp"), (self._send_loop, "bridge-tx")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        gap = self._gap_s(30)
        log.info(
            "listening on %s:%d, serial %s @ %d, repeat %d (gap %.0f ms for a typical frame), wifi %s, replies -> %s",
            self.listen[0], self.listen[1], self.serial_pattern, self.baud, self.repeat, gap * 1000,
            "%s:%d" % self.wifi_target if self.wifi_target else "off",
            "%s:%d" % self.reply_to if self.reply_to else "last sender",
        )
        return self

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        self.link.stop()
        for t in self._threads:
            t.join(2.0)
        for s in (self.sock_in, self.sock_wifi):
            if s is not None:
                s.close()

    def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass

    # -- timing ------------------------------------------------------------------

    def airtime_s(self, wire_len: int) -> float:
        return lp.frame_airtime_s(wire_len, sf=self.sf, bw_hz=self.bw_hz, cr=self.cr)

    def _gap_s(self, wire_len: int) -> float:
        if self.repeat_gap_ms is not None:
            return self.repeat_gap_ms / 1000.0
        # airtime + UART transfer + 20 ms margin for the module's packetiser
        uart = wire_len * 10 / self.baud
        return self.airtime_s(wire_len) + uart + 0.020

    # -- QLab -> LoRa ------------------------------------------------------------

    def _udp_loop(self) -> None:
        socks = [s for s in (self.sock_in, self.sock_wifi) if s is not None]
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(socks, [], [], 0.5)
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                raise
            for s in ready:
                try:
                    data, addr = s.recvfrom(65535)
                except OSError:
                    continue
                if s is self.sock_in:
                    self._last_sender = addr[:2]
                    self.handle_osc(data, addr)
                else:
                    self._on_wifi_reply(data, addr)

    def handle_osc(self, data: bytes, addr: Any = None) -> None:
        try:
            messages = lp.parse_osc(data)
        except ValueError as exc:
            log.warning("from %s: %s", addr, exc)
            return
        if any(a.startswith("/bridge/") for a, _ in messages):
            # handled here, never forwarded; the rest of a bundle goes out as usual
            for payload in lp.split_osc_messages(data):
                (addr0, args0), = lp.parse_osc(payload)
                if addr0.startswith("/bridge/"):
                    self._bridge_command(addr0, args0, addr)
                else:
                    self.forward_osc(payload, addr)
            return
        self.forward_osc(data, addr)

    def forward_osc(self, data: bytes, addr: Any = None) -> None:
        """Send an OSC packet to the Pi: WiFi (optional) + LoRa frames."""
        messages = lp.parse_osc(data)
        desc = "; ".join(lp.format_msg(a, p) for a, p in messages)
        if self.wifi_target and self.sock_wifi is not None:
            try:
                self.sock_wifi.sendto(data, self.wifi_target)
                log.info("wifi -> %s:%d  %s", *self.wifi_target, desc)
            except OSError as exc:
                log.warning("wifi -> %s:%d failed: %s", *self.wifi_target, exc)
        # one frame if it fits; a too large bundle is split into its messages
        payloads = [data]
        try:
            lp.encode_frame(0, data)
        except lp.FrameTooLarge:
            payloads = lp.split_osc_messages(data)
            if len(payloads) > 1:
                log.info("bundle too large for one LoRa frame, sending %d messages separately", len(payloads))
        for payload in payloads:
            seq = self.seq.next()
            try:
                wire = lp.encode_frame(seq, payload)
            except lp.FrameTooLarge as exc:
                log.error("NOT sent over LoRa: %s: %s", "; ".join(lp.format_msg(a, p) for a, p in lp.parse_osc(payload)), exc)
                continue
            text = "; ".join(lp.format_msg(a, p) for a, p in lp.parse_osc(payload))
            self._queue.put((seq, wire, text))

    # -- /bridge/... commands (QLab -> bridge) -----------------------------------------

    def _bridge_reply(self, address: str, args: list, to: Any) -> None:
        target = self.reply_to or (tuple(to[:2]) if to else None) or self._reply_target()
        payload = lp.build_osc(address, args)
        if target is None or self.sock_in is None:
            log.info("%s (no reply target): %s", address, lp.format_msg(address, args))
            return
        try:
            self.sock_in.sendto(payload, target)
        except OSError as exc:
            log.warning("reply -> %s:%d failed: %s", target[0], target[1], exc)

    def _bridge_command(self, address: str, args: list, addr: Any) -> None:
        if address == "/bridge/results":
            # blocking HTTP GET: not on the UDP thread (a slow website must not stall other cues)
            threading.Thread(target=self._bridge_results, args=(addr,), name="bridge-results", daemon=True).start()
        else:
            log.warning("unknown bridge command %s", address)
            self._bridge_reply("/bridge/error", [f"unknown bridge command {address}"], addr)

    def _bridge_results(self, addr: Any) -> None:
        if not self.results_url:
            log.warning("/bridge/results: no --results-url configured")
            self._bridge_reply("/bridge/error", ["results url not configured (--results-url)"], addr)
            return
        try:
            ja, nein, poll = fetch_results(self.results_url)
        except ValueError as exc:
            log.warning("/bridge/results failed: %s", exc)
            self._bridge_reply("/bridge/error", [str(exc)], addr)
            return
        log.info("/bridge/results: ja=%d nein=%d poll=%d", ja, nein, poll)
        self.forward_osc(lp.build_osc("/proj/results/set", [ja, nein, poll]), addr)
        self._bridge_reply("/bridge/results/ok", [ja, nein, poll], addr)

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            seq, wire, text = item
            if not self.link.connected:
                log.warning("lora seq=%d NOT sent (serial %s): %s", seq, self.link.status(), text)
                continue
            air = self.airtime_s(len(wire))
            copies = self.repeat
            if self.duty.would_exceed(air * copies):
                copies = 1
                now = time.monotonic()
                if now - self._duty_warned > 30:
                    self._duty_warned = now
                    log.warning(
                        "duty cycle: %.1f s of %.0f s airtime used in the last hour -> sending single copies only",
                        self.duty.used_s(), self.duty.budget_s(),
                    )
            gap = self._gap_s(len(wire))
            sent = 0
            for i in range(copies):
                if i:
                    time.sleep(gap)
                if self.link.write(wire):
                    sent += 1
                    self.sent_frames += 1
                    self.duty.add(air)
            log.info("lora seq=%d x%d (%.0f ms air each, %d B)  %s", seq, sent, air * 1000, len(wire), text)
            # keep the frames of consecutive commands apart as well
            if sent:
                time.sleep(gap)

    # -- replies -------------------------------------------------------------------

    def _reply_target(self) -> tuple[str, int] | None:
        return self.reply_to or self._last_sender

    def _forward_reply(self, payload: bytes, via: str, seq: int | None = None) -> None:
        try:
            msgs = lp.parse_osc(payload)
        except ValueError as exc:
            log.warning("reply via %s: %s", via, exc)
            return
        text = "; ".join(lp.format_msg(a, p) for a, p in msgs)
        target = self._reply_target()
        seq_txt = f" seq={seq}" if seq is not None else ""
        if target is None or self.sock_in is None:
            log.info("reply via %s%s (no reply target yet): %s", via, seq_txt, text)
            return
        try:
            self.sock_in.sendto(payload, target)
            log.info("reply via %s%s -> %s:%d  %s", via, seq_txt, target[0], target[1], text)
        except OSError as exc:
            log.warning("reply -> %s:%d failed: %s", target[0], target[1], exc)

    def _on_serial_data(self, data: bytes) -> None:
        for frame in self.decoder.feed(data):
            if not frame.is_reply:
                log.debug("ignoring non-reply frame seq=%d", frame.seq)
                continue
            if not self.reply_dedup.is_new(frame.seq):
                continue
            self._forward_reply(frame.payload, "lora", frame.seq)

    def _on_wifi_reply(self, data: bytes, addr: Any) -> None:
        self._forward_reply(data, "wifi")


# --------------------------------------------------------------------------- CLI


def list_ports() -> int:
    try:
        from serial.tools import list_ports as lpmod
    except ImportError:
        print("pyserial not installed", file=sys.stderr)
        return 1
    ports = sorted(lpmod.comports(), key=lambda p: p.device)
    if not ports:
        print("no serial ports found")
    for p in ports:
        vidpid = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else "----:----"
        hint = "  <- CH343 (Waveshare USB-TO-LoRa?)" if p.vid == 0x1A86 else ""
        print(f"{p.device:40s} {vidpid}  {p.description or ''}{hint}")
    return 0


def _gap_arg(text: str) -> float | None:
    if text == "auto":
        return None
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected milliseconds or 'auto'")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="projektor-bridge",
        description="Forward OSC/UDP (QLab) to the projector over LoRa (and optionally WiFi).",
    )
    p.add_argument("--serial", help="serial device of the LoRa adapter (glob allowed, e.g. '/dev/cu.usbmodem*')")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--listen", type=lambda t: parse_hostport(t, "0.0.0.0"), default=("0.0.0.0", 9001),
                   help="UDP HOST:PORT to receive OSC from QLab (default 0.0.0.0:9001)")
    p.add_argument("--repeat", type=int, default=2, help="send each frame N times (default 2)")
    p.add_argument("--repeat-gap-ms", type=_gap_arg, default=None, metavar="MS|auto",
                   help="gap between copies; auto = airtime + margin (default)")
    p.add_argument("--wifi-target", type=parse_hostport, metavar="PI_IP:9000",
                   help="also forward the raw OSC over UDP to the Pi")
    p.add_argument("--reply-to", type=parse_hostport, metavar="HOST:PORT",
                   help="send replies here (default: last OSC sender)")
    p.add_argument("--results-url", metavar="URL",
                   help="vote website results API (e.g. https://example.org/welle/api/results); "
                        "OSC /bridge/results fetches it and sends /proj/results/set to the Pi")
    p.add_argument("--sf", type=int, default=7, help="LoRa spreading factor configured on the adapters (airtime calc)")
    p.add_argument("--bw", type=int, default=500, choices=(125, 250, 500), help="LoRa bandwidth kHz (airtime calc)")
    p.add_argument("--duty-cycle", type=float, default=1.0, help="duty cycle limit in %% (default 1.0, EU868 g1)")
    p.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    if args.list_ports:
        return list_ports()
    if not args.serial:
        print("--serial is required (see --list-ports)", file=sys.stderr)
        return 2
    if resolve_device(args.serial) is None:
        log.warning("serial device %s not present yet, will keep retrying every 2 s", args.serial)
    bridge = Bridge(
        serial=args.serial, baud=args.baud, listen=args.listen, repeat=args.repeat,
        repeat_gap_ms=args.repeat_gap_ms, wifi_target=args.wifi_target, reply_to=args.reply_to,
        sf=args.sf, bw_khz=args.bw, duty_limit=args.duty_cycle / 100.0, results_url=args.results_url,
    )
    try:
        bridge.start()
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", args.listen[0], args.listen[1], exc)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: bridge._stop.set())
    bridge.run_forever()
    bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
