"""End-to-end: UDP OSC -> projektor-bridge -> socat virtual serial link -> LoRa transport."""

import os
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest

from projektor import lora_proto as lp
from projektor.bridge import Bridge, DutyCycle, build_parser, parse_hostport
from projektor.transport.lora import LoraTransport

from test_lora_transport import FakeDispatcher, settings, wait_for

pytestmark = pytest.mark.skipif(shutil.which("socat") is None, reason="socat not installed")


class Socat:
    def __init__(self, a, b):
        self.a, self.b = a, b
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["socat", f"pty,raw,echo=0,link={self.a}", f"pty,raw,echo=0,link={self.b}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert wait_for(lambda: os.path.exists(self.a) and os.path.exists(self.b), 5)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc = None
        for p in (self.a, self.b):
            if os.path.lexists(p):
                os.unlink(p)


@pytest.fixture
def link(tmp_path):
    s = Socat(str(tmp_path / "ttyBRIDGE"), str(tmp_path / "ttyPI"))
    s.start()
    yield s
    s.stop()


def udp_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(3.0)
    return s


def recv_osc(sock):
    data, _ = sock.recvfrom(65535)
    return lp.parse_osc(data)


@pytest.fixture
def system(link):
    disp = FakeDispatcher()
    pi = LoraTransport(settings(link.b), disp, poll_s=0.1).start()
    bridge = Bridge(serial=link.a, listen=("127.0.0.1", 0), repeat=2, repeat_gap_ms=10, poll_s=0.1).start()
    assert wait_for(lambda: pi.status().startswith("connected") and bridge.link.connected)
    qlab = udp_socket()
    yield SimpleNamespace(disp=disp, pi=pi, bridge=bridge, qlab=qlab, link=link)
    qlab.close()
    bridge.stop()
    pi.stop()


def test_udp_to_lora_to_dispatcher_and_reply(system):
    s = system
    s.qlab.sendto(lp.build_osc("/proj/cue", [3]), s.bridge.listen)
    assert wait_for(lambda: len(s.disp.calls) == 1)
    assert s.disp.calls[0] == ("/proj/cue", [3], "lora")
    # second copy of the repeat must have been sent and deduped
    assert wait_for(lambda: s.bridge.sent_frames == 2)
    time.sleep(0.2)
    assert len(s.disp.calls) == 1

    # ping -> pong travels back over the link and is forwarded to the sender
    s.qlab.sendto(lp.build_osc("/proj/ping", []), s.bridge.listen)
    assert recv_osc(s.qlab) == [("/proj/pong", ["projektor"])]
    assert [c[0] for c in s.disp.calls] == ["/proj/cue", "/proj/ping"]


def test_order_preserved_for_bursts(system):
    s = system
    for i in range(5):
        s.qlab.sendto(lp.build_osc("/proj/cue", [i]), s.bridge.listen)
    assert wait_for(lambda: len(s.disp.calls) == 5)
    assert [c[1][0] for c in s.disp.calls] == [0, 1, 2, 3, 4]


def test_oversize_dropped_and_bundle_split(system):
    from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
    from pythonosc.osc_message_builder import OscMessageBuilder

    s = system
    s.qlab.sendto(lp.build_osc("/proj/play", ["x" * 400]), s.bridge.listen)
    b = OscBundleBuilder(IMMEDIATELY)
    for i in range(12):  # 12 * ~28 B > 240 B -> split into messages
        m = OscMessageBuilder("/proj/standby")
        m.add_arg(f"cue-{i}")
        b.add_content(m.build())
    s.qlab.sendto(b.build().dgram, s.bridge.listen)
    assert wait_for(lambda: len(s.disp.calls) == 12, 10)
    assert [c[1][0] for c in s.disp.calls] == [f"cue-{i}" for i in range(12)]
    assert all(c[0] == "/proj/standby" for c in s.disp.calls)


def test_wifi_forward_and_reply_to(link):
    disp = FakeDispatcher()
    pi = LoraTransport(settings(link.b), disp, poll_s=0.1).start()
    fake_pi_udp = udp_socket()
    reply_sink = udp_socket()
    bridge = Bridge(
        serial=link.a, listen=("127.0.0.1", 0), repeat=1,
        wifi_target=fake_pi_udp.getsockname(), reply_to=reply_sink.getsockname(), poll_s=0.1,
    ).start()
    qlab = udp_socket()
    try:
        assert wait_for(lambda: pi.status().startswith("connected") and bridge.link.connected)
        qlab.sendto(lp.build_osc("/proj/ping", []), bridge.listen)
        # WiFi path: raw OSC arrives at the "Pi" UDP port
        data, bridge_addr = fake_pi_udp.recvfrom(65535)
        assert lp.parse_osc(data) == [("/proj/ping", [])]
        # LoRa path reply goes to --reply-to, not to the sender
        assert recv_osc(reply_sink) == [("/proj/pong", ["projektor"])]
        # a UDP reply from the Pi to the bridge's forward socket is forwarded as well
        fake_pi_udp.sendto(lp.build_osc("/proj/pong", ["wifi"]), bridge_addr)
        assert recv_osc(reply_sink) == [("/proj/pong", ["wifi"])]
    finally:
        qlab.close()
        bridge.stop()
        pi.stop()
        fake_pi_udp.close()
        reply_sink.close()


def test_reconnect_after_socat_restart(link):
    disp = FakeDispatcher()
    pi = LoraTransport(settings(link.b), disp, poll_s=0.1).start()
    bridge = Bridge(serial=link.a, listen=("127.0.0.1", 0), repeat=1, poll_s=0.1).start()
    qlab = udp_socket()
    try:
        assert wait_for(lambda: pi.status().startswith("connected") and bridge.link.connected)
        qlab.sendto(lp.build_osc("/proj/cue", [1]), bridge.listen)
        assert wait_for(lambda: len(disp.calls) == 1)

        link.stop()  # both adapters "unplugged"
        assert wait_for(lambda: pi.status() == "absent" and not bridge.link.connected)

        link.start()  # replugged
        assert wait_for(lambda: pi.status().startswith("connected") and bridge.link.connected)
        qlab.sendto(lp.build_osc("/proj/cue", [2]), bridge.listen)
        assert wait_for(lambda: len(disp.calls) == 2)
        assert disp.calls[-1] == ("/proj/cue", [2], "lora")
    finally:
        qlab.close()
        bridge.stop()
        pi.stop()


# ---------------------------------------------------------------- units (no socat needed,
# but kept here with the bridge)


def test_parse_hostport():
    assert parse_hostport("10.0.0.5:9000") == ("10.0.0.5", 9000)
    assert parse_hostport("9001") == ("127.0.0.1", 9001)
    assert parse_hostport(":9001", "0.0.0.0") == ("0.0.0.0", 9001)


def test_cli_defaults():
    a = build_parser().parse_args(["--serial", "/dev/x"])
    assert a.repeat == 2 and a.repeat_gap_ms is None and a.listen == ("0.0.0.0", 9001)
    a = build_parser().parse_args(["--serial", "/dev/x", "--repeat-gap-ms", "120", "--wifi-target", "1.2.3.4:9000"])
    assert a.repeat_gap_ms == 120.0 and a.wifi_target == ("1.2.3.4", 9000)


def test_duty_cycle_tracker():
    t = [0.0]
    d = DutyCycle(0.01, 3600, clock=lambda: t[0])
    assert d.budget_s() == pytest.approx(36.0)
    for _ in range(250):
        d.add(0.144)
    assert d.used_s() == pytest.approx(36.0)
    assert d.would_exceed(0.072)
    t[0] = 3601
    assert d.used_s() == 0
    assert not d.would_exceed(0.072)


def test_auto_gap_exceeds_airtime():
    b = Bridge(serial="/dev/null-nope", listen=("127.0.0.1", 0))
    wire = len(lp.encode_frame(1, lp.build_osc("/proj/cue", [12])))
    assert b._gap_s(wire) > b.airtime_s(wire) > 0.01  # SF7/500 kHz: ~18 ms
