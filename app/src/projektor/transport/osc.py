"""UDP OSC server -> CommandDispatcher, with replies (ARCHITECTURE.md §5).

Replies go to the sender's address/port, or to osc.reply_host:osc.reply_port if set.
They are sent from the server socket (source port = listening port).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.osc_server import BlockingOSCUDPServer

from ..config import Settings

log = logging.getLogger(__name__)


def build_message(address: str, args: list) -> bytes:
    b = OscMessageBuilder(address=address)
    for a in args:
        if isinstance(a, bool):
            b.add_arg(a)
        elif isinstance(a, int):
            b.add_arg(a, OscMessageBuilder.ARG_TYPE_INT if -(2**31) <= a < 2**31 else None)
        elif isinstance(a, float):
            b.add_arg(a, OscMessageBuilder.ARG_TYPE_FLOAT)
        elif a is None:
            b.add_arg(None)
        else:
            b.add_arg(str(a), OscMessageBuilder.ARG_TYPE_STRING)
    return b.build().dgram


class OscServer:
    """Serves OSC on UDP in a background thread (commands are handled in arrival order)."""

    def __init__(self, settings: Settings, dispatcher, controller=None, host: str = "0.0.0.0",
                 port: int | None = None):
        self.settings = settings
        self.dispatcher = dispatcher
        self.controller = controller
        d = Dispatcher()
        d.set_default_handler(self._handle, needs_reply_address=True)
        self._server = BlockingOSCUDPServer((host, settings.osc.port if port is None else port), d)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def status(self) -> str:
        return f"listening :{self.port}"

    def start(self) -> "OscServer":
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="osc", daemon=True)
        self._thread.start()
        log.info("OSC listening on udp :%d", self.port)
        if self.controller is not None:
            self.controller.set_transport_status("osc", self.status())
        return self

    def stop(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.controller is not None:
            self.controller.set_transport_status("osc", "stopped")

    def _handle(self, client_address: tuple[str, int], address: str, *args: Any) -> None:
        try:
            replies = self.dispatcher.handle(address, list(args), "osc")
        except Exception:  # noqa: BLE001 - never kill the server thread
            log.exception("dispatcher failed for %s", address)
            return
        if not replies:
            return
        host = self.settings.osc.reply_host or client_address[0]
        port = self.settings.osc.reply_port or client_address[1]
        for addr, rargs in replies:
            try:
                self._server.socket.sendto(build_message(addr, rargs), (host, port))
            except OSError as e:
                log.warning("OSC reply to %s:%s failed: %s", host, port, e)
