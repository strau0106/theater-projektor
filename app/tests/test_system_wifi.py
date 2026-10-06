"""Tests for system/sbin/projektor-wifi (boot file parsing, nmcli arguments) with nmcli faked."""

import importlib.machinery
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "system" / "sbin" / "projektor-wifi"


@pytest.fixture
def pw(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJEKTOR_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("PROJEKTOR_WIFI_FILE", str(tmp_path / "boot" / "projektor-wifi.txt"))
    monkeypatch.setenv("PROJEKTOR_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("PROJEKTOR_WIFI_CA_DIR", str(tmp_path / "ca"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader("projektor_wifi_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    calls = []

    def fake_nmcli(*args, timeout=30, check=True):
        calls.append(list(args))
        return ""

    monkeypatch.setattr(mod, "nmcli", fake_nmcli)
    monkeypatch.setattr(mod, "known_networks", lambda: [])
    monkeypatch.setattr(mod, "_trigger_if_not_connected", lambda cfg: None)
    mod.calls = calls
    return mod


def _opts(args):
    return dict(zip(args[::2], args[1::2]))


def test_parse_psk_lines(pw):
    assert pw.parse_wifi_line("TestNet;test-pass-123;10") == ("TestNet", "test-pass-123", 10, None)
    assert pw.parse_wifi_line("Cafe;;") == ("Cafe", "", 0, None)
    assert pw.parse_wifi_line("a;pw;with;semicolons;5") == ("a", "pw;with;semicolons", 5, None)
    assert pw.parse_wifi_line("# comment") is None
    assert pw.parse_wifi_line("   ") is None


def test_parse_enterprise_lines(pw):
    ssid, psk, prio, eap = pw.parse_wifi_line(
        "eap;eduroam;user@uni.de;s3cret;7;phase2=mschapv2;anon=anonymous@uni.de;domain=radius.uni.de")
    assert (ssid, psk, prio) == ("eduroam", "", 7)
    assert eap == {"identity": "user@uni.de", "password": "s3cret", "phase2": "mschapv2",
                   "anonymous_identity": "anonymous@uni.de", "domain_suffix": "radius.uni.de"}
    _, _, prio, eap = pw.parse_wifi_line("EAP;Corp;bob;pw;method=ttls;phase2=pap;ca=corp.pem")
    assert prio == 0 and eap["method"] == "ttls" and eap["ca_file"] == "corp.pem"
    with pytest.raises(pw.WifiError):
        pw.parse_wifi_line("eap;x;y")
    with pytest.raises(pw.WifiError):
        pw.parse_wifi_line("eap;x;user;pw;bogus=1")


def test_add_psk_network_args(pw):
    res = pw.add_network(pw.Config(), "TestNet", "test-pass-123", 10)
    assert res["security"] == "psk"
    o = _opts(pw.calls[-1][2:])
    assert o["ssid"] == "TestNet" and o["wifi-sec.key-mgmt"] == "wpa-psk" and o["wifi-sec.psk"] == "test-pass-123"


def test_add_enterprise_network_args(pw, tmp_path):
    ca = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
    res = pw.add_network(pw.Config(), "Corp WLAN", "", 4, {
        "identity": "bob", "password": "pw", "method": "ttls", "phase2": "pap",
        "anonymous_identity": "anon", "domain_suffix": "radius.corp.de", "ca_cert": ca})
    assert res == {"ok": True, "ssid": "Corp WLAN", "priority": 4, "security": "eap"}
    o = _opts(pw.calls[-1][2:])
    assert o["wifi-sec.key-mgmt"] == "wpa-eap"
    assert o["802-1x.eap"] == "ttls" and o["802-1x.phase2-auth"] == "pap"
    assert o["802-1x.identity"] == "bob" and o["802-1x.password"] == "pw"
    assert o["802-1x.anonymous-identity"] == "anon" and o["802-1x.domain-suffix-match"] == "radius.corp.de"
    ca_path = Path(o["802-1x.ca-cert"])
    assert ca_path.parent == tmp_path / "ca" and ca_path.read_text().startswith("-----BEGIN CERTIFICATE-----")


def test_add_enterprise_defaults_no_ca(pw):
    pw.add_network(pw.Config(), "Uni", "", 0, {"identity": "u", "password": "p"})
    o = _opts(pw.calls[-1][2:])
    assert o["802-1x.eap"] == "peap" and o["802-1x.phase2-auth"] == "mschapv2"
    assert "802-1x.ca-cert" not in o


@pytest.mark.parametrize("eap", [
    {"identity": "", "password": "p"},
    {"identity": "u", "password": ""},
    {"identity": "u", "password": "p", "method": "tls"},
    {"identity": "u", "password": "p", "ca_cert": "not a cert"},
])
def test_add_enterprise_rejects_bad_input(pw, eap):
    with pytest.raises(pw.WifiError):
        pw.add_network(pw.Config(), "Uni", "", 0, eap)
    assert pw.calls == []


def test_cli_add_json_reads_stdin(pw, monkeypatch, capsys):
    req = {"ssid": "Uni", "priority": 2, "eap": {"identity": "u", "password": "p"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(req)))
    assert pw.main(["add-json"]) == 0
    assert json.loads(capsys.readouterr().out)["security"] == "eap"
    assert _opts(pw.calls[-1][2:])["802-1x.identity"] == "u"


def test_boot_import_mixed(pw, tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "corp.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    (boot / "projektor-wifi.txt").write_text(
        "# test\n"
        "TestNet;test-pass-123;10\n"
        "eap;Corp;bob;pw;5;ca=corp.pem\n"
        "eap;broken\n")
    assert pw.import_boot_file(pw.Config()) == 2
    assert (boot / "projektor-wifi.txt.imported").exists()
    ssids = [_opts(c[2:])["ssid"] for c in pw.calls if c[:2] == ["connection", "add"]]
    assert ssids == ["TestNet", "Corp"]
    assert "802-1x.ca-cert" in _opts(pw.calls[-1][2:])
