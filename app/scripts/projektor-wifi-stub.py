#!/usr/bin/env python3
"""Dev/test stand-in for /usr/local/sbin/projektor-wifi (§9).

Same CLI and JSON output shape, but backed by a JSON state file instead of
NetworkManager:

    projektor-wifi-stub.py [--state FILE] list|add <ssid> <psk> [prio]|add-json|remove <ssid>|scan|retry

`add-json` reads {"ssid", "psk", "priority", "eap": {...}} from stdin.

Output (stdout, one JSON document):
    list   {"ok": true, "networks": [{"ssid": "...", "priority": 10, "active": false,
                                       "security": "psk|eap|open", "identity": ""}]}
    add    {"ok": true, "ssid": "...", "priority": 0, "security": "psk"}
    remove {"ok": true, "ssid": "..."}
    scan   {"ok": true, "networks": [{"ssid": "...", "signal": 70, "security": "WPA2", "known": true}]}
    retry  {"ok": true}
Errors: {"ok": false, "error": "..."} with exit code 1.
"""

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_STATE = {
    "networks": [{"ssid": "Theater", "priority": 10}],
    "active": None,
    "visible": [
        {"ssid": "Theater", "signal": 72, "security": "WPA2"},
        {"ssid": "Buehne-Gast", "signal": 45, "security": "WPA2"},
        {"ssid": "FreeWifi", "signal": 20, "security": ""},
    ],
}


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return json.loads(json.dumps(DEFAULT_STATE))


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def out(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj))
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("PROJEKTOR_WIFI_STUB_STATE", "devdata/wifi-stub.json"))
    ap.add_argument("cmd", choices=["list", "add", "add-json", "remove", "scan", "retry"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    path = Path(a.state)
    st = load(path)
    nets = st.setdefault("networks", [])

    if a.cmd == "list":
        return out({"ok": True, "networks": [
            {"ssid": n["ssid"], "priority": n.get("priority", 0), "active": n["ssid"] == st.get("active"),
             "security": n.get("security", "psk"), "identity": n.get("identity", "")}
            for n in sorted(nets, key=lambda n: -n.get("priority", 0))
        ]})
    if a.cmd == "add":
        if len(a.args) < 2:
            return out({"ok": False, "error": "usage: add <ssid> <psk> [prio]"}, 1)
        ssid, psk = a.args[0], a.args[1]
        if psk and not 8 <= len(psk) <= 63:
            return out({"ok": False, "error": "WPA password must be 8..63 characters"}, 1)
        try:
            prio = int(a.args[2]) if len(a.args) > 2 else 0
        except ValueError:
            return out({"ok": False, "error": "priority must be an integer"}, 1)
        nets[:] = [n for n in nets if n["ssid"] != ssid] + [{"ssid": ssid, "priority": prio}]
        save(path, st)
        return out({"ok": True, "ssid": ssid, "priority": prio})
    if a.cmd == "add-json":
        try:
            req = json.loads(sys.stdin.read() or "{}")
            prio = int(req.get("priority") or 0)
        except (ValueError, TypeError, AttributeError) as e:
            return out({"ok": False, "error": f"bad request: {e}"}, 1)
        ssid, psk, eap = str(req.get("ssid") or ""), str(req.get("psk") or ""), req.get("eap")
        if not ssid:
            return out({"ok": False, "error": "SSID must be 1..32 bytes"}, 1)
        if eap is not None:
            if not eap.get("identity") or not eap.get("password"):
                return out({"ok": False, "error": "enterprise network needs identity and password"}, 1)
            security, identity = "eap", eap["identity"]
        else:
            if psk and not 8 <= len(psk) <= 63:
                return out({"ok": False, "error": "WPA password must be 8..63 characters"}, 1)
            security, identity = ("psk" if psk else "open"), ""
        nets[:] = [n for n in nets if n["ssid"] != ssid] + [
            {"ssid": ssid, "priority": prio, "security": security, "identity": identity}]
        save(path, st)
        return out({"ok": True, "ssid": ssid, "priority": prio, "security": security})
    if a.cmd == "remove":
        if not a.args:
            return out({"ok": False, "error": "usage: remove <ssid>"}, 1)
        ssid = a.args[0]
        if not any(n["ssid"] == ssid for n in nets):
            return out({"ok": False, "error": f"unknown network: {ssid}"}, 1)
        nets[:] = [n for n in nets if n["ssid"] != ssid]
        save(path, st)
        return out({"ok": True, "ssid": ssid})
    if a.cmd == "scan":
        known = {n["ssid"] for n in nets}
        return out({"ok": True, "networks": [dict(v, known=v["ssid"] in known) for v in st.get("visible", [])]})
    if a.cmd == "retry":
        return out({"ok": True})
    return out({"ok": False, "error": "unknown command"}, 1)


if __name__ == "__main__":
    sys.exit(main())
