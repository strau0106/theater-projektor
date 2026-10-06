"""OSC-over-LoRa frame format (ARCHITECTURE.md section 8).

Wire format, one LoRa packet per frame, SLIP delimited (RFC 1055)::

    END [0x50 'P'][ver=1][seq u16 BE][flags u8][OSC packet bytes][CRC16-CCITT BE] END

* CRC16-CCITT ("CCITT-FALSE": poly 0x1021, init 0xFFFF, no reflection, no xorout)
  over everything from the magic byte up to the end of the OSC bytes.
* flags bit0 = reply (Pi -> bridge).
* The SLIP-encoded frame including both END bytes must fit into one packet of the
  Waveshare adapter (240 bytes), see ``MAX_WIRE_LEN``.

Only the standard library is used for framing. OSC building/parsing uses python-osc,
which is imported lazily so the framing part stays dependency-free.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

MAGIC = 0x50
VERSION = 1
FLAG_REPLY = 0x01

HEADER_LEN = 5  # magic, version, seq(2), flags
CRC_LEN = 2
MAX_WIRE_LEN = 240  # one Waveshare USB-TO-LoRa packet (bytes on the serial line / in the air)

SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD


class FrameError(ValueError):
    """Frame could not be decoded (bad magic, version, length or CRC)."""


class FrameTooLarge(ValueError):
    """Encoded frame would not fit into one LoRa packet."""


# --------------------------------------------------------------------------- CRC / SLIP


def _make_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return table


_CRC_TABLE = _make_crc_table()


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC16-CCITT-FALSE. ``crc16_ccitt(b"123456789") == 0x29B1``."""
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def slip_escape(data: bytes) -> bytes:
    return (
        data.replace(bytes([SLIP_ESC]), bytes([SLIP_ESC, SLIP_ESC_ESC]))
        .replace(bytes([SLIP_END]), bytes([SLIP_ESC, SLIP_ESC_END]))
    )


def slip_unescape(data: bytes) -> bytes:
    """Undo SLIP escaping. Raises FrameError on an invalid escape sequence."""
    if SLIP_ESC not in data:
        return data
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == SLIP_ESC:
            nxt = next(it, None)
            if nxt == SLIP_ESC_END:
                out.append(SLIP_END)
            elif nxt == SLIP_ESC_ESC:
                out.append(SLIP_ESC)
            else:
                raise FrameError("invalid SLIP escape")
        else:
            out.append(b)
    return bytes(out)


# --------------------------------------------------------------------------- frames


@dataclass(frozen=True)
class Frame:
    seq: int
    flags: int
    payload: bytes  # raw OSC packet (message or bundle)

    @property
    def is_reply(self) -> bool:
        return bool(self.flags & FLAG_REPLY)


def encode_body(seq: int, payload: bytes, flags: int = 0) -> bytes:
    """Unescaped frame body: header + payload + CRC."""
    if not 0 <= seq <= 0xFFFF:
        raise ValueError(f"seq out of range: {seq}")
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"flags out of range: {flags}")
    body = bytes([MAGIC, VERSION, (seq >> 8) & 0xFF, seq & 0xFF, flags]) + bytes(payload)
    crc = crc16_ccitt(body)
    return body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def encode_frame(seq: int, payload: bytes, flags: int = 0, max_len: int = MAX_WIRE_LEN) -> bytes:
    """Encode a complete SLIP-delimited frame ready to be written to the serial port.

    Raises FrameTooLarge if the result exceeds ``max_len`` bytes.
    """
    wire = bytes([SLIP_END]) + slip_escape(encode_body(seq, payload, flags)) + bytes([SLIP_END])
    if len(wire) > max_len:
        raise FrameTooLarge(
            f"frame is {len(wire)} bytes on the wire (OSC packet {len(payload)} bytes), "
            f"limit is {max_len}"
        )
    return wire


def decode_body(body: bytes) -> Frame:
    """Decode an unescaped frame body (without SLIP END bytes)."""
    if len(body) < HEADER_LEN + CRC_LEN:
        raise FrameError(f"frame too short ({len(body)} bytes)")
    if body[0] != MAGIC:
        raise FrameError(f"bad magic 0x{body[0]:02x}")
    if body[1] != VERSION:
        raise FrameError(f"unsupported version {body[1]}")
    crc_rx = (body[-2] << 8) | body[-1]
    crc = crc16_ccitt(body[:-2])
    if crc != crc_rx:
        raise FrameError(f"CRC mismatch (got 0x{crc_rx:04x}, expected 0x{crc:04x})")
    seq = (body[2] << 8) | body[3]
    return Frame(seq=seq, flags=body[4], payload=bytes(body[HEADER_LEN:-CRC_LEN]))


def decode_frame(wire: bytes) -> Frame:
    """Decode exactly one SLIP frame (END bytes optional)."""
    data = bytes(wire).strip(bytes([SLIP_END]))
    if bytes([SLIP_END]) in data:
        raise FrameError("more than one frame")
    return decode_body(slip_unescape(data))


class FrameDecoder:
    """Streaming decoder for the serial byte stream.

    Tolerates garbage between frames, frames split across reads, several frames in
    one read and corrupt frames (dropped and counted). ``feed()`` returns the
    complete, valid frames found so far.
    """

    def __init__(self, max_len: int = MAX_WIRE_LEN, on_error: Callable[[str], None] | None = None):
        self._buf = bytearray()
        self._overflow = False
        self._max_body = max_len  # escaped body can never legitimately be longer
        self._on_error = on_error
        self.frames_ok = 0
        self.frames_bad = 0
        self.bytes_dropped = 0

    def reset(self) -> None:
        self._buf.clear()
        self._overflow = False

    def feed(self, data: bytes) -> list[Frame]:
        frames: list[Frame] = []
        for b in data:
            if b == SLIP_END:
                if self._overflow:
                    self._bad("oversized frame dropped")
                elif self._buf:
                    self._finish(frames)
                self._buf.clear()
                self._overflow = False
            elif self._overflow:
                self.bytes_dropped += 1
            else:
                self._buf.append(b)
                if len(self._buf) > self._max_body:
                    # garbage without END bytes (e.g. module debug output): discard
                    self.bytes_dropped += len(self._buf)
                    self._buf.clear()
                    self._overflow = True
        return frames

    def _finish(self, frames: list[Frame]) -> None:
        raw = bytes(self._buf)
        try:
            frame = decode_body(slip_unescape(raw))
        except FrameError as exc:
            # Short junk between frames (e.g. an RSSI byte appended by the module)
            # is expected and not worth an error message.
            self.bytes_dropped += len(raw)
            if len(raw) >= HEADER_LEN + CRC_LEN:
                self._bad(str(exc))
            return
        self.frames_ok += 1
        frames.append(frame)

    def _bad(self, msg: str) -> None:
        self.frames_bad += 1
        if self._on_error:
            self._on_error(msg)


# --------------------------------------------------------------------------- dedup


class SeqDedup:
    """Drop repeated frames by sequence number.

    A seq is a duplicate if it was seen within the last ``window_s`` seconds.
    Entries expire after the window, so u16 wraparound is harmless: at any sane
    command rate a seq value is not reused within 10 s. A restarted sender starts at
    a random seq (see ``random_seq()``), so it is very unlikely to collide with seqs
    still remembered from its previous run.
    """

    def __init__(self, window_s: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._seen: dict[int, float] = {}

    def _expire(self, now: float) -> None:
        if not self._seen:
            return
        limit = now - self.window_s
        # dict keeps insertion order == time order (re-seen seqs are not refreshed)
        for seq in list(self._seen):
            if self._seen[seq] >= limit:
                break
            del self._seen[seq]

    def is_new(self, seq: int) -> bool:
        """Return True (and remember seq) if seq was not seen within the window."""
        now = self._clock()
        self._expire(now)
        if seq in self._seen:
            return False
        self._seen[seq] = now
        return True

    def __len__(self) -> int:
        return len(self._seen)


def random_seq() -> int:
    import random

    return random.randrange(0x10000)


class SeqCounter:
    """u16 sequence counter with random start and wraparound."""

    def __init__(self, start: int | None = None):
        self._next = random_seq() if start is None else start & 0xFFFF

    def next(self) -> int:
        seq = self._next
        self._next = (self._next + 1) & 0xFFFF
        return seq


# --------------------------------------------------------------------------- OSC helpers


def build_osc(address: str, args: Iterable[Any] = ()) -> bytes:
    """Build an OSC message datagram (types inferred: int i, float f, str s, ...)."""
    from pythonosc.osc_message_builder import OscMessageBuilder

    builder = OscMessageBuilder(address=address)
    for arg in args:
        builder.add_arg(arg)
    return builder.build().dgram


def parse_osc(data: bytes) -> list[tuple[str, list]]:
    """Parse an OSC message or bundle into a list of (address, args).

    Bundles (also nested) are expanded into their messages, in order; time tags are
    ignored (executed immediately). Raises ValueError on malformed data.
    """
    from pythonosc.osc_bundle import OscBundle
    from pythonosc.osc_message import OscMessage

    def walk(dgram: bytes, out: list[tuple[str, list]]) -> None:
        if OscBundle.dgram_is_bundle(dgram):
            for content in OscBundle(dgram):
                walk(content.dgram, out)
        else:
            msg = OscMessage(dgram)
            out.append((msg.address, list(msg.params)))

    out: list[tuple[str, list]] = []
    try:
        walk(bytes(data), out)
    except Exception as exc:  # python-osc raises several ParseError types
        raise ValueError(f"malformed OSC packet: {exc}") from exc
    return out


def split_osc_messages(data: bytes) -> list[bytes]:
    """Split a bundle into individual message datagrams (a message is returned as is)."""
    from pythonosc.osc_bundle import OscBundle

    if not OscBundle.dgram_is_bundle(data):
        return [bytes(data)]
    out: list[bytes] = []
    for content in OscBundle(data):
        out.extend(split_osc_messages(content.dgram))
    return out


def format_msg(address: str, args: list) -> str:
    return address + ("" if not args else " " + " ".join(repr(a) for a in args))


# --------------------------------------------------------------------------- airtime


def lora_airtime_s(
    payload_len: int,
    sf: int = 7,
    bw_hz: int = 125_000,
    cr: int = 1,
    preamble: int = 8,
    explicit_header: bool = True,
    crc: bool = True,
) -> float:
    """Time on air of one LoRa packet (Semtech AN1200.13 / SX1262 datasheet 6.1.4).

    ``cr``: 1..4 for coding rate 4/5..4/8. Low data rate optimisation is assumed
    on when the symbol time exceeds 16 ms (as Semtech recommends).
    """
    tsym = (2**sf) / bw_hz
    de = 1 if tsym > 0.016 else 0
    h = 0 if explicit_header else 1
    num = 8 * payload_len - 4 * sf + 28 + (16 if crc else 0) - 20 * h
    n_payload = 8 + max(math.ceil(num / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preamble + 4.25) * tsym + n_payload * tsym


# Bytes the Waveshare firmware adds over the air in stream mode (address/net id
# header). Not documented; 4 bytes is a conservative guess for airtime estimates.
MODULE_OVERHEAD_GUESS = 4


def frame_airtime_s(wire_len: int, sf: int = 7, bw_hz: int = 125_000, cr: int = 1) -> float:
    return lora_airtime_s(wire_len + MODULE_OVERHEAD_GUESS, sf=sf, bw_hz=bw_hz, cr=cr)
