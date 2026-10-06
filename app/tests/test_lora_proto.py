import pytest

from projektor import lora_proto as lp


def osc(addr, *args):
    return lp.build_osc(addr, list(args))


def test_crc_known_vector():
    assert lp.crc16_ccitt(b"123456789") == 0x29B1


def test_frame_layout():
    payload = osc("/proj/go")
    wire = lp.encode_frame(0x1234, payload)
    assert wire[0] == lp.SLIP_END and wire[-1] == lp.SLIP_END
    body = lp.slip_unescape(wire[1:-1])
    assert body[:5] == bytes([0x50, 1, 0x12, 0x34, 0])
    assert body[5:-2] == payload
    crc = lp.crc16_ccitt(body[:-2])
    assert body[-2:] == bytes([crc >> 8, crc & 0xFF])


@pytest.mark.parametrize("seq", [0, 1, 0x7FFF, 0xFFFF])
@pytest.mark.parametrize("flags", [0, lp.FLAG_REPLY])
def test_round_trip(seq, flags):
    payload = osc("/proj/cue", 12)
    frame = lp.decode_frame(lp.encode_frame(seq, payload, flags))
    assert frame == lp.Frame(seq, flags, payload)
    assert frame.is_reply == bool(flags)
    assert lp.parse_osc(frame.payload) == [("/proj/cue", [12])]


def test_round_trip_with_slip_special_bytes():
    # payload and seq containing END/ESC bytes must be escaped
    payload = osc("/proj/play", "x\xc0y", b"\xc0\xdb\xdc\xdd", 1.5)
    wire = lp.encode_frame(0xC0DB, payload)
    assert lp.SLIP_END not in wire[1:-1]
    frame = lp.decode_frame(wire)
    assert frame.seq == 0xC0DB and frame.payload == payload


def test_crc_failure_rejected():
    wire = bytearray(lp.encode_frame(5, osc("/proj/stop")))
    wire[8] ^= 0x01
    with pytest.raises(lp.FrameError, match="CRC"):
        lp.decode_frame(bytes(wire))
    dec = lp.FrameDecoder()
    assert dec.feed(bytes(wire)) == []
    assert dec.frames_bad == 1


def test_bad_magic_and_version():
    body = bytearray(lp.encode_body(1, b"x"))
    body[0] = 0x51
    with pytest.raises(lp.FrameError, match="magic"):
        lp.decode_body(bytes(body))
    body = bytearray(lp.encode_body(1, b"x"))
    body[1] = 2
    with pytest.raises(lp.FrameError, match="version"):
        lp.decode_body(bytes(body))


def test_decoder_garbage_resync():
    f1 = lp.encode_frame(1, osc("/proj/go"))
    f2 = lp.encode_frame(2, osc("/proj/cue", "3"))
    stream = b"\x00garbage\xff\xdb\x01" + f1 + b"\x7f\x80junk" + b"\xc0\xc0" + f2 + b"tail"
    dec = lp.FrameDecoder()
    frames = dec.feed(stream)
    assert [f.seq for f in frames] == [1, 2]


def test_decoder_garbage_without_delimiter_before_frame():
    # garbage directly followed by a frame: the frame's leading END resyncs
    f1 = lp.encode_frame(9, osc("/proj/go"))
    dec = lp.FrameDecoder()
    assert [f.seq for f in dec.feed(b"noise" + f1)] == [9]


def test_decoder_long_garbage_overflow():
    dec = lp.FrameDecoder()
    assert dec.feed(bytes(range(1, 0xC0)) * 5) == []
    f1 = lp.encode_frame(3, osc("/proj/go"))
    assert [f.seq for f in dec.feed(f1)] == [3]


def test_decoder_split_delivery_byte_by_byte():
    f1 = lp.encode_frame(77, osc("/proj/fadeout", 2.5))
    dec = lp.FrameDecoder()
    out = []
    for b in f1:
        out += dec.feed(bytes([b]))
    assert len(out) == 1 and out[0].seq == 77
    assert lp.parse_osc(out[0].payload) == [("/proj/fadeout", [2.5])]


def test_decoder_split_in_chunks_and_concatenated():
    frames = [lp.encode_frame(i, osc("/proj/cue", i)) for i in range(10)]
    stream = b"".join(frames)
    dec = lp.FrameDecoder()
    got = []
    for i in range(0, len(stream), 7):
        got += dec.feed(stream[i:i + 7])
    assert [f.seq for f in got] == list(range(10))


def test_decoder_concatenated_without_double_end():
    # frames sharing one END byte between them
    f1 = lp.encode_frame(1, osc("/proj/go"))
    f2 = lp.encode_frame(2, osc("/proj/stop"))
    stream = f1[:-1] + f2
    assert [f.seq for f in lp.FrameDecoder().feed(stream)] == [1, 2]


def test_decoder_ignores_trailing_rssi_byte():
    f1 = lp.encode_frame(1, osc("/proj/go"))
    f2 = lp.encode_frame(2, osc("/proj/go"))
    dec = lp.FrameDecoder()
    assert [f.seq for f in dec.feed(f1 + b"\xa5" + f2 + b"\xa7")] == [1, 2]
    assert dec.frames_bad == 0


def test_oversize_rejected():
    big = osc("/proj/play", "x" * 300)
    with pytest.raises(lp.FrameTooLarge):
        lp.encode_frame(1, big)
    # largest payload that fits
    ok = b"\x00" * (lp.MAX_WIRE_LEN - 2 - lp.HEADER_LEN - lp.CRC_LEN)
    # pick a seq whose bytes and CRC do not need escaping
    for seq in range(100):
        body = lp.encode_body(seq, ok)
        if lp.SLIP_END not in body and lp.SLIP_ESC not in body:
            break
    assert len(lp.encode_frame(seq, ok)) == lp.MAX_WIRE_LEN
    with pytest.raises(lp.FrameTooLarge):
        lp.encode_frame(seq, ok + b"\x00")


def test_seq_and_flags_range():
    with pytest.raises(ValueError):
        lp.encode_frame(0x10000, b"")
    with pytest.raises(ValueError):
        lp.encode_frame(1, b"", flags=256)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_dedup_window():
    clk = FakeClock()
    d = lp.SeqDedup(10.0, clock=clk)
    assert d.is_new(5)
    assert not d.is_new(5)
    clk.t += 9.9
    assert not d.is_new(5)
    clk.t += 0.2
    assert d.is_new(5)  # expired, accepted again


def test_dedup_wraparound():
    clk = FakeClock()
    d = lp.SeqDedup(10.0, clock=clk)
    for s in (65534, 65535, 0, 1):
        assert d.is_new(s)
        clk.t += 0.1
    assert not d.is_new(65535)
    assert not d.is_new(0)
    assert d.is_new(2)


def test_dedup_sender_restart_new_random_start():
    clk = FakeClock()
    d = lp.SeqDedup(10.0, clock=clk)
    for s in range(100, 110):
        assert d.is_new(s)
    # restarted sender continues from an unrelated (random) start: accepted
    for s in range(40000, 40005):
        assert d.is_new(s)
    # restart reusing an old value after the window: accepted
    clk.t += 11
    assert d.is_new(100)
    assert len(d) == 1


def test_seq_counter_wraps():
    c = lp.SeqCounter(0xFFFE)
    assert [c.next() for _ in range(4)] == [0xFFFE, 0xFFFF, 0, 1]


def test_bundle_expansion():
    from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
    from pythonosc.osc_message_builder import OscMessageBuilder

    def msg(addr, *args):
        b = OscMessageBuilder(addr)
        for a in args:
            b.add_arg(a)
        return b.build()

    inner = OscBundleBuilder(IMMEDIATELY)
    inner.add_content(msg("/proj/standby", 4))
    outer = OscBundleBuilder(IMMEDIATELY)
    outer.add_content(msg("/proj/cue", "3"))
    outer.add_content(inner.build())
    outer.add_content(msg("/proj/fadeout", 1.0))
    data = outer.build().dgram
    expected = [("/proj/cue", ["3"]), ("/proj/standby", [4]), ("/proj/fadeout", [1.0])]
    assert lp.parse_osc(data) == expected
    frame = lp.decode_frame(lp.encode_frame(1, data))
    assert lp.parse_osc(frame.payload) == expected
    parts = lp.split_osc_messages(data)
    assert [lp.parse_osc(p)[0] for p in parts] == expected


def test_parse_osc_malformed():
    with pytest.raises(ValueError):
        lp.parse_osc(b"not osc at all")


def test_airtime_reference_values():
    # SF7/BW125/CR4/5, 8 symbol preamble, explicit header, CRC: 20 B -> 56.6 ms
    # (matches the Semtech LoRa calculator)
    assert lp.lora_airtime_s(20) == pytest.approx(0.05658, abs=1e-4)
    # SF12/BW125, 64 B PHY payload (LoRaWAN EU868 DR0: 51 B app + 13 B MAC): 2793.5 ms
    assert lp.lora_airtime_s(64, sf=12) == pytest.approx(2.7935, abs=1e-3)
