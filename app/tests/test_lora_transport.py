"""LoRa transport over a pseudo-terminal pair (no hardware)."""

import os
import select
import threading
import time
import tty
from types import SimpleNamespace

import pytest

from projektor import lora_proto as lp
from projektor.transport.lora import LoraTransport, resolve_device, start_lora


class FakeDispatcher:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def handle(self, address, args, source):
        with self.lock:
            self.calls.append((address, list(args), source))
        if address == "/proj/ping":
            return [("/proj/pong", ["projektor"])]
        if address == "/proj/state":
            return [("/proj/state", ["{" + '"x": 1, ' * 60 + "}"])]  # too big for LoRa
        if address == "/proj/boom":
            raise RuntimeError("boom")
        return []


def settings(device, enabled=True):
    return SimpleNamespace(
        lora=SimpleNamespace(enabled=enabled, device=device, baud=115200),
        device=SimpleNamespace(name="projektor"),
    )


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Pty:
    """Master side of a pty; the slave path is published via a symlink."""

    def __init__(self, link):
        self.master, slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(slave)
        self.slave_path = os.ttyname(slave)
        self._slave_fd = slave
        self.link = link
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(self.slave_path, link)
        self.dec = lp.FrameDecoder()

    def write(self, data):
        os.write(self.master, data)

    def read_frames(self, n=1, timeout=3.0):
        out = []
        end = time.monotonic() + timeout
        while len(out) < n and time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    out += self.dec.feed(os.read(self.master, 1024))
                except OSError:
                    break
        return out

    def close(self):
        for fd in (self._slave_fd, self.master):
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.lexists(self.link):
            os.unlink(self.link)


@pytest.fixture
def pty_pair(tmp_path):
    p = Pty(str(tmp_path / "ttyLORA0"))
    yield p
    p.close()


def frame(seq, addr, *args, flags=0):
    return lp.encode_frame(seq, lp.build_osc(addr, list(args)), flags)


def test_disabled():
    h = start_lora(settings("/nonexistent", enabled=False), FakeDispatcher(), None)
    assert h.status() == "disabled"
    h.stop()


def test_absent_device():
    h = LoraTransport(settings("/dev/does-not-exist-*"), FakeDispatcher(), poll_s=0.1).start()
    try:
        time.sleep(0.3)
        assert h.status() == "absent"
    finally:
        h.stop()


def test_resolve_device_glob(tmp_path):
    (tmp_path / "usb-1a86_USB_Single_Serial_ABC-if00").touch()
    assert resolve_device(str(tmp_path / "usb-1a86_*")).endswith("ABC-if00")
    assert resolve_device(str(tmp_path / "nope*")) is None


def test_receive_commands_dedup_and_reply(pty_pair, tmp_path):
    disp = FakeDispatcher()
    h = LoraTransport(settings(str(tmp_path / "ttyLORA*")), disp, poll_s=0.1).start()
    try:
        assert wait_for(lambda: h.status().startswith("connected"))
        assert h.status() == f"connected {pty_pair.slave_path}"

        # repeated frame (bridge repeat) + garbage + split delivery
        f = frame(100, "/proj/cue", 3)
        pty_pair.write(b"\x00junk" + f + f)
        g = frame(101, "/proj/go")
        pty_pair.write(g[:4])
        time.sleep(0.05)
        pty_pair.write(g[4:])
        # corrupt frame must be dropped
        bad = bytearray(frame(102, "/proj/stop"))
        bad[6] ^= 0xFF
        pty_pair.write(bytes(bad))
        # reply frames from another node are ignored
        pty_pair.write(frame(103, "/proj/pong", "x", flags=lp.FLAG_REPLY))
        assert wait_for(lambda: len(disp.calls) >= 2)
        time.sleep(0.2)
        assert disp.calls == [("/proj/cue", [3], "lora"), ("/proj/go", [], "lora")]

        # ping -> pong reply goes back over the serial link with the reply flag
        pty_pair.write(frame(104, "/proj/ping"))
        replies = pty_pair.read_frames(1)
        assert len(replies) == 1 and replies[0].is_reply
        assert lp.parse_osc(replies[0].payload) == [("/proj/pong", ["projektor"])]

        # oversize reply is dropped, dispatcher exceptions do not kill the reader
        pty_pair.write(frame(105, "/proj/state"))
        pty_pair.write(frame(106, "/proj/boom"))
        pty_pair.write(frame(107, "/proj/ping"))
        replies = pty_pair.read_frames(1)
        assert [lp.parse_osc(r.payload)[0][0] for r in replies] == ["/proj/pong"]
        assert wait_for(lambda: len(disp.calls) == 6)
    finally:
        h.stop()


def test_bundle_in_frame(pty_pair):
    from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
    from pythonosc.osc_message_builder import OscMessageBuilder

    b = OscBundleBuilder(IMMEDIATELY)
    for addr in ("/proj/stop", "/proj/standby"):
        m = OscMessageBuilder(addr)
        b.add_content(m.build())
    disp = FakeDispatcher()
    h = LoraTransport(settings(pty_pair.link), disp, poll_s=0.1).start()
    try:
        assert wait_for(lambda: h.status().startswith("connected"))
        pty_pair.write(lp.encode_frame(1, b.build().dgram))
        assert wait_for(lambda: len(disp.calls) == 2)
        assert [c[0] for c in disp.calls] == ["/proj/stop", "/proj/standby"]
    finally:
        h.stop()


def test_reconnect_after_unplug(tmp_path):
    link = str(tmp_path / "ttyLORA0")
    disp = FakeDispatcher()
    p1 = Pty(link)
    h = LoraTransport(settings(link), disp, poll_s=0.1).start()
    try:
        assert wait_for(lambda: h.status().startswith("connected"))
        p1.write(frame(1, "/proj/cue", 1))
        assert wait_for(lambda: len(disp.calls) == 1)

        p1.close()  # "unplug": master closed, symlink gone
        assert wait_for(lambda: h.status() == "absent")

        p2 = Pty(link)  # "replug": new pty under the same name
        try:
            assert wait_for(lambda: h.status() == f"connected {p2.slave_path}")
            p2.write(frame(2, "/proj/cue", 2))
            assert wait_for(lambda: len(disp.calls) == 2)
            assert disp.calls[-1] == ("/proj/cue", [2], "lora")
        finally:
            p2.close()
    finally:
        h.stop()
