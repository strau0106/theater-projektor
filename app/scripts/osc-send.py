#!/usr/bin/env python3
"""Send one OSC message over UDP and print replies.

    osc-send.py /proj/cue 1
    osc-send.py --host 192.168.1.42 /proj/play intro 2.5
    osc-send.py /proj/cue s:1        # force string argument
    osc-send.py /proj/state          # prints /proj/state <json>

Arguments are sent as int if they parse as int, float if they parse as float, else string.
Prefix with "s:", "i:" or "f:" to force a type.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder


def parse_arg(a: str):
    if a.startswith("s:"):
        return a[2:]
    if a.startswith("i:"):
        return int(a[2:])
    if a.startswith("f:"):
        return float(a[2:])
    try:
        return int(a)
    except ValueError:
        pass
    try:
        return float(a)
    except ValueError:
        return a


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--wait", type=float, default=0.5, help="seconds to wait for replies")
    ap.add_argument("--pretty", action="store_true", help="pretty-print /proj/state JSON")
    ap.add_argument("address")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    b = OscMessageBuilder(address=a.address)
    for x in a.args:
        v = parse_arg(x)
        b.add_arg(v, OscMessageBuilder.ARG_TYPE_FLOAT if isinstance(v, float) else None)
    msg = b.build()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(a.wait)
    sock.sendto(msg.dgram, (a.host, a.port))
    print(f"> {a.address} {msg.params}")
    rc = 0
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except (socket.timeout, TimeoutError):
            break
        except OSError as e:
            print(f"! {e}", file=sys.stderr)
            rc = 1
            break
        try:
            m = OscMessage(data)
        except Exception as e:  # noqa: BLE001
            print(f"< (unparseable from {addr}: {e})")
            continue
        params = list(m.params)
        if m.address == "/proj/state" and params and a.pretty:
            print(f"< {m.address}\n{json.dumps(json.loads(params[0]), indent=2)}")
        else:
            print(f"< {m.address} {' '.join(map(str, params))}")
        if m.address == "/proj/error":
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
