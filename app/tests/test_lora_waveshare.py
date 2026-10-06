"""projektor-lora-config against a simulated AT-command module."""

import re

import pytest

from projektor import waveshare as ws


class FakeModule:
    """Minimal simulation of the documented AT interface (answer format is a guess)."""

    def __init__(self, bw_style="index"):
        self.bw_style = bw_style
        self.at_mode = False
        self.params = {"SF": 7, "BW": 0 if bw_style == "index" else 125, "CR": 1, "PWR": 22, "NETID": 0,
                       "LBT": 0, "MODE": 1, "TXCH": 18, "RXCH": 18, "RSSI": 0, "ADDR": 0, "KEY": 0,
                       "PORT": 3, "BAUD": 115200}
        self.applied = {}
        self.sent = []
        self._out = b""
        self.in_waiting = 0

    # pyserial-ish API
    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        self._out += self._answer(cmd).encode() + b"\r\n"
        self.in_waiting = len(self._out)

    def _answer(self, cmd):
        if cmd == "+++":
            self.at_mode = True
            return "Entry AT"
        if not self.at_mode:
            return ""
        if cmd == "AT+EXIT":
            self.at_mode = False
            self.applied = dict(self.params)
            return "Exit AT"
        if cmd == "AT+VER":
            return "V1.2"
        m = re.fullmatch(r"AT\+(\w+)\?", cmd)
        if m:
            return f"+{m.group(1)}={self.params.get(m.group(1), 'x')}" if m.group(1) in self.params else "ERROR"
        m = re.fullmatch(r"AT\+(\w+)=(.*)", cmd)
        if m and m.group(1) in self.params:
            val = int(m.group(2))
            if m.group(1) == "BW" and val not in ((0, 1, 2) if self.bw_style == "index" else (125, 250, 500)):
                return "ERROR"
            self.params[m.group(1)] = val
            return "OK"
        return "ERROR"

    def read(self, n=1):
        out, self._out = self._out[:n], self._out[n:]
        self.in_waiting = len(self._out)
        return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)
    monkeypatch.setattr(ws, "ANSWER_IDLE_S", 0.0)


def run(monkeypatch, module, argv):
    monkeypatch.setattr(ws, "open_port", lambda port, baud: module)
    return ws.main(argv)


@pytest.mark.parametrize("style", ["index", "khz"])
def test_apply_projektor_profile(monkeypatch, fast, capsys, style):
    mod = FakeModule(style)
    rc = run(monkeypatch, mod, ["--port", "/dev/fake", "--apply"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert mod.sent[0] == "+++" and mod.sent[-1] == "AT+EXIT"
    p = mod.applied
    assert (p["SF"], p["CR"], p["PWR"], p["TXCH"], p["RXCH"], p["ADDR"], p["MODE"], p["LBT"], p["RSSI"]) == \
        (7, 1, 10, 17, 17, 0, 1, 0, 0)
    assert p["BW"] == (2 if style == "index" else 500)


def test_apply_overrides(monkeypatch, fast, capsys):
    mod = FakeModule()
    rc = run(monkeypatch, mod, ["--port", "x", "--apply", "--freq", "868.1", "--air-rate", "fast",
                                "--power", "12", "--netid", "7", "--addr", "4242"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "not on the 1 MHz channel raster" in out
    assert "ERP" in out  # 12 dBm + 6 dBi > 14 dBm ERP warning
    p = mod.applied
    assert (p["TXCH"], p["SF"], p["BW"], p["PWR"], p["NETID"], p["ADDR"]) == (18, 7, 1, 12, 7, 4242)


def test_show(monkeypatch, fast, capsys):
    mod = FakeModule()
    assert run(monkeypatch, mod, ["--port", "x", "--show"]) == 0
    out = capsys.readouterr().out
    assert "AT+TXCH?" in out and "+TXCH=18" in out
    assert mod.sent[-1] == "AT+EXIT"


def test_dry_run_does_not_open_port(monkeypatch, capsys):
    def boom(*a):
        raise AssertionError("port opened in dry run")

    monkeypatch.setattr(ws, "open_port", boom)
    assert ws.main(["--apply", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for cmd in ("+++", "AT+MODE=1", "AT+SF=7", "AT+BW=2", "AT+PWR=10", "AT+TXCH=17", "AT+RXCH=17",
                "AT+ADDR=0", "AT+EXIT"):
        assert cmd in out


def test_channel_mapping_and_eu_notes():
    assert ws.channel_for_freq(868.0) == 18
    assert ws.channel_for_freq(868.1) == 18
    assert ws.freq_for_channel(18) == 868.0
    with pytest.raises(ValueError):
        ws.channel_for_freq(940)
    assert ws.eu868_note(18, 10) == []
    assert any("outside" in n for n in ws.eu868_note(65, 10))
    assert any("ERP" in n for n in ws.eu868_note(18, 22))
