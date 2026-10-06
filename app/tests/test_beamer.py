"""Beamer (HDMI-CEC) wrapper, OSC commands and web endpoints with a fake cec-ctl."""

from __future__ import annotations

import stat
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from projektor.beamer import KEYS, Beamer, BeamerError
from projektor.commands import CommandDispatcher
from projektor.web.app import create_app
from projektor.web.fakes import FakeController, FakeDispatcher, FakeShowStore, dev_settings


def fake_cec(tmp_path, pwr="on (0x00)", rc=0):
    log = tmp_path / "cec.log"
    script = tmp_path / "cec-ctl"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"if '--give-device-power-status' in sys.argv and {pwr!r}:\n"
        f"    print('Received from TV (0 to 4): CEC_MSG_REPORT_POWER_STATUS (0x90):')\n"
        f"    print('\\t\\tpwr-state: ' + {pwr!r})\n"
        f"sys.exit({rc})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def mk(tmp_path, **kw):
    script, log = fake_cec(tmp_path, **kw)
    b = Beamer(SimpleNamespace(beamer=SimpleNamespace(cec_ctl=str(script))))
    b.retry_gap_s = 0
    b.ensure_registered = lambda: setattr(b, "_from", [])
    b.wait_on_s = 0
    return b, log


def lines(log):
    return log.read_text().splitlines() if log.exists() else []


def test_commands(tmp_path):
    b, log = mk(tmp_path)
    b.ensure_registered = lambda: setattr(b, "_from", [])
    b.wait_on_s = 0
    b.setup()
    b.power_on()
    b.standby()
    b.key("back")
    assert lines(log) == [
        "-d /dev/cec0 -p 1.0.0.0 --playback -o Projektor",
        "-d /dev/cec0 --to 0 --image-view-on",
        "-d /dev/cec0 --to 15 --active-source phys-addr=1.0.0.0",
        "-d /dev/cec0 --to 0 --user-control-pressed ui-cmd=power-toggle-function",
        "-d /dev/cec0 --to 0 --user-control-released",
        "-d /dev/cec0 --to 0 --user-control-pressed ui-cmd=exit",
        "-d /dev/cec0 --to 0 --user-control-released",
    ]
    with pytest.raises(BeamerError):
        b.key("bogus")
    assert "back" in KEYS


def test_setup_auto_phys(tmp_path):
    b, log = mk(tmp_path)
    b.phys_addr = ""
    b.setup()
    assert lines(log) == ["-d /dev/cec0 --playback -o Projektor"]


@pytest.mark.parametrize("pwr,want", [("on (0x00)", "on"), ("standby (0x01)", "standby"),
                                      ("to-on (0x02)", "on"), ("", "unknown")])
def test_status(tmp_path, pwr, want):
    b, log = mk(tmp_path, pwr=pwr)
    assert b.status() == want
    assert lines(log) == ["-d /dev/cec0 --to 0 --give-device-power-status"]


def test_status_failure_and_unavailable(tmp_path):
    b, _ = mk(tmp_path, rc=1)
    assert b.status() == "unknown"
    b.cec_ctl = str(tmp_path / "missing")
    assert b.status() == "unavailable"
    with pytest.raises(BeamerError, match="not found"):
        b.power_on()
    b2 = Beamer(SimpleNamespace(beamer=SimpleNamespace(enabled=False)))
    assert b2.status() == "unavailable"


def test_osc(tmp_path):
    from projektor.config import Settings

    b, log = mk(tmp_path)
    d = CommandDispatcher(SimpleNamespace(record_command=lambda *a: None), Settings(), beamer=b)
    assert d.handle("/proj/beamer/status", [], "osc") == [("/proj/beamer/status", ["on"])]
    assert d.handle("/proj/beamer/power", ["off"], "osc") == []
    assert d.handle("/proj/beamer/key", ["up"], "osc") == []
    b.join()
    ls = lines(log)
    assert "-d /dev/cec0 --to 0 --user-control-pressed ui-cmd=power-toggle-function" in ls
    assert "-d /dev/cec0 --to 0 --user-control-pressed ui-cmd=up" in ls
    assert d.handle("/proj/beamer/power", [1], "lora") == []
    b.join()
    assert any("--image-view-on" in x for x in lines(log))
    assert d.handle("/proj/beamer/key", ["nope"], "osc")[0][0] == "/proj/error"
    assert d.handle("/proj/beamer/power", [], "osc")[0][0] == "/proj/error"


def test_web(tmp_path):
    b, log = mk(tmp_path)
    data = tmp_path / "data"
    store = FakeShowStore(data)
    ctrl = FakeController(store)
    app = create_app(dev_settings(data), ctrl, FakeDispatcher(ctrl), store, beamer=b,
                     power_cmds={"reboot": None, "shutdown": None})
    c = TestClient(app)
    assert c.get("/api/beamer").json() == {"enabled": True, "status": "on"}
    assert c.post("/api/beamer/power", json={"on": False}).json() == {"ok": True}
    assert c.post("/api/beamer/power", json={"on": True}).status_code == 200
    assert c.post("/api/beamer/key", json={"key": "mute"}).status_code == 200
    assert c.post("/api/beamer/key", json={"key": "x"}).status_code == 400
    ls = lines(log)
    assert "-d /dev/cec0 --to 0 --user-control-pressed ui-cmd=power-toggle-function" in ls and any("ui-cmd=mute" in x for x in ls)
    b.cec_ctl = str(tmp_path / "missing")
    assert c.get("/api/beamer").json()["status"] == "unavailable"
    assert c.post("/api/beamer/power", json={"on": True}).status_code == 200  # runs in background; errors are logged
    assert c.post("/api/beamer/source").status_code == 200


def test_status_off_after_standby_without_reply(tmp_path):
    """XGIMI Halo+: after CEC standby the beamer no longer answers -> report "off", not "unknown"."""
    from types import SimpleNamespace
    from projektor.beamer import Beamer

    tool = tmp_path / "cec-ctl"
    tool.write_text("#!/bin/sh\ncase \"$*\" in *give-device-power-status*) echo 'Timeout'; exit 1;; esac\nexit 0\n")
    tool.chmod(0o755)
    b = Beamer(SimpleNamespace(beamer=SimpleNamespace(enabled=True, device="/dev/null", phys_addr="1.0.0.0",
                                                      power_on_at_boot=False, standby_at_shutdown=False,
                                                      cec_ctl=str(tool))))
    assert b.status() == "unknown"
    b.standby()
    assert b.status() == "off"


def test_reregisters_when_adapter_lost_address(tmp_path):
    """Beamer asleep -> Pi adapter at f.f.f.f; power_on must re-register before Image View On."""
    from types import SimpleNamespace
    from projektor.beamer import Beamer

    log = tmp_path / "log"
    tool = tmp_path / "cec-ctl"
    tool.write_text(f"""#!/bin/sh
echo "$*" >> {log}
if [ "$*" = "-d /dev/null" ]; then echo "	Physical Address           : f.f.f.f"; echo "	Logical Address Mask       : 0x0000"; fi
exit 0
""")
    tool.chmod(0o755)
    b = Beamer(SimpleNamespace(beamer=SimpleNamespace(enabled=True, device="/dev/null", phys_addr="1.0.0.0",
                                                      power_on_at_boot=False, standby_at_shutdown=False,
                                                      cec_ctl=str(tool))))
    b.wait_on_s = 0
    b.retry_gap_s = 0
    b.power_on()
    ls = log.read_text().splitlines()
    assert "-d /dev/null --from 4 --to 0 --image-view-on" in ls
