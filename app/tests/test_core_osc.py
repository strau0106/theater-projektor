"""OSC end-to-end over localhost UDP with the fake player."""

from __future__ import annotations

import json
import socket

import pytest
from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

from projektor.commands import CommandDispatcher
from projektor.transport.osc import OscServer, build_message


def _msg(address, *args):
    b = OscMessageBuilder(address=address)
    for a in args:
        b.add_arg(a)
    return b.build().dgram


@pytest.fixture
def server(settings, ctrl):
    srv = OscServer(settings, CommandDispatcher(ctrl, settings), ctrl, host="127.0.0.1", port=0).start()
    yield srv
    srv.stop()


@pytest.fixture
def client():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2.0)
    yield s
    s.close()


def _send_recv(client, port, address, *args):
    client.sendto(_msg(address, *args), ("127.0.0.1", port))
    data, addr = client.recvfrom(65535)
    assert addr[1] == port  # replies come from the server port
    m = OscMessage(data)
    return m.address, list(m.params)


def test_ping_pong(server, client):
    assert _send_recv(client, server.port, "/proj/ping") == ("/proj/pong", ["proj-test"])


def test_transport_status(server, ctrl):
    assert ctrl.state()["transports"]["osc"] == f"listening :{server.port}"


def test_cue_then_state(server, client, ctrl):
    client.sendto(_msg("/proj/cue", 2), ("127.0.0.1", server.port))
    addr, params = _send_recv(client, server.port, "/proj/state")  # handled in order
    assert addr == "/proj/state"
    st = json.loads(params[0])
    assert st["cue"] == "2" and st["video"] == "hold" and st["standby"] == "3"
    assert st["last_command"]["address"] == "/proj/state"
    assert st["status_screen"] is False


def test_error_reply(server, client):
    addr, params = _send_recv(client, server.port, "/proj/cue", 42)
    assert addr == "/proj/error" and "unknown cue" in params[0]
    addr, params = _send_recv(client, server.port, "/proj/bogus")
    assert addr == "/proj/error" and "unknown address" in params[0]
    # server still alive
    assert _send_recv(client, server.port, "/proj/ping")[0] == "/proj/pong"


def test_reply_host_override(settings, ctrl, client):
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.bind(("127.0.0.1", 0))
    other.settimeout(2.0)
    settings.osc.reply_host = "127.0.0.1"
    settings.osc.reply_port = other.getsockname()[1]
    srv = OscServer(settings, CommandDispatcher(ctrl, settings), ctrl, host="127.0.0.1", port=0).start()
    try:
        client.sendto(_msg("/proj/ping"), ("127.0.0.1", srv.port))
        data, _ = other.recvfrom(65535)
        assert OscMessage(data).address == "/proj/pong"
    finally:
        srv.stop()
        other.close()


def test_build_message_types():
    m = OscMessage(build_message("/x", [1, 2.5, "s", True]))
    assert list(m.params) == [1, 2.5, "s", True]
