"""CLI: `projektor run [--dev] [--config PATH]`.

Process model (ARCHITECTURE.md §3): main thread runs the GLib main loop (player + controller),
OSC server, LoRa reader and uvicorn (web) run in their own threads.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

log = logging.getLogger("projektor")


def _setup_logging(level: str, dev: bool) -> None:
    fmt = "%(levelname)s %(name)s: %(message)s"
    if dev:
        fmt = "%(asctime)s.%(msecs)03d " + fmt
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt,
                        datefmt="%H:%M:%S", stream=sys.stderr)


def _start_web(settings, controller, dispatcher, show_store, beamer):
    try:
        from .web.app import create_app  # type: ignore[import-not-found]
    except ImportError as e:
        log.error("web UI not available (%s)", e)
        return None, None
    try:
        import uvicorn

        app = create_app(settings, controller, dispatcher, show_store, beamer=beamer)
        config = uvicorn.Config(app, host="0.0.0.0", port=settings.web.port, log_level="info",
                                access_log=False)
        server = uvicorn.Server(config)
    except Exception:  # noqa: BLE001
        log.exception("cannot create web app")
        return None, None
    t = threading.Thread(target=server.run, name="web", daemon=True)
    t.start()
    log.info("web UI on http://0.0.0.0:%d/", settings.web.port)
    return server, t


def _start_lora(settings, dispatcher, controller):
    if not settings.lora.enabled:
        controller.set_transport_status("lora", "disabled")
        return None
    try:
        from .transport import lora as lora_mod  # type: ignore[attr-defined]
    except ImportError as e:
        log.error("LoRa transport not available (%s)", e)
        controller.set_transport_status("lora", "unavailable")
        return None
    try:
        obj = lora_mod.start_lora(settings, dispatcher, controller)
    except Exception as e:  # noqa: BLE001
        log.exception("LoRa transport failed to start")
        controller.set_transport_status("lora", f"error: {e}")
        return None
    status_fn = getattr(obj, "status", None) or getattr(lora_mod, "status", None)
    if callable(status_fn):
        controller.set_transport_status("lora", status_fn)
    return obj


def cmd_run(args: argparse.Namespace) -> int:
    from .config import ProjektorError, ShowStore, load_settings

    try:
        settings = load_settings(args.config, dev=args.dev)
    except ProjektorError as e:
        log.error("%s", e)
        return 2
    log.info("settings: %s", settings.config_path)
    settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)

    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from .commands import CommandDispatcher
    from .controller import Controller, GLibExecutor
    from .status import build_status_text

    if args.player == "fake":
        from .player.fake import FakePlayer

        player = FakePlayer(check_files=True, realtime=True)
    else:
        from .player.gst import GstPlayer

        player = GstPlayer(settings, sink=args.sink)

    show_store = ShowStore.from_settings(settings)
    controller = Controller(settings, show_store, player, GLibExecutor(),
                            status_builder=lambda st: build_status_text(settings, st))
    from .beamer import Beamer

    beamer = Beamer(settings)
    from . import __version__, notify as notify_mod

    prev_run = notify_mod.begin_run(settings.paths.data_dir)
    notifier = notify_mod.Notifier(settings, version=__version__).start()
    notifier.install_excepthooks()
    notifier.startup(prev_run)
    dispatcher = CommandDispatcher(controller, settings, beamer=beamer, notifier=notifier)
    loop = GLib.MainLoop()

    controller.start()

    osc = None
    try:
        from .transport.osc import OscServer

        osc = OscServer(settings, dispatcher, controller).start()
    except OSError as e:
        log.error("cannot start OSC server on udp :%d: %s", settings.osc.port, e)
        controller.set_transport_status("osc", f"error: {e.strerror or e}")

    lora = _start_lora(settings, dispatcher, controller)
    monitors = notify_mod.Monitors(notifier, settings, lora_status=getattr(lora, "status", None)).start()
    web_server, web_thread = _start_web(settings, controller, dispatcher, show_store, beamer)

    def beamer_boot() -> None:
        beamer.setup()
        if beamer.power_on_at_boot:
            beamer.power_on()

    if beamer.enabled:
        beamer.background(beamer_boot)

    GLib.timeout_add(1000, controller.tick)

    def on_quit(signame: str) -> bool:
        log.info("%s received, shutting down", signame)
        loop.quit()
        return False

    def on_snapshot() -> bool:
        data = player.snapshot_png()
        out = settings.paths.data_dir / "snapshot.png"
        if data:
            out.write_bytes(data)
            log.info("snapshot written to %s", out)
        else:
            log.warning("snapshot not available")
        return True

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, on_quit, "SIGINT")
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, on_quit, "SIGTERM")
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, on_snapshot)

    try:
        loop.run()
    finally:
        if osc is not None:
            osc.stop()
        monitors.stop()
        if lora is not None:
            try:
                lora.stop()
            except Exception:  # noqa: BLE001
                log.exception("LoRa stop failed")
        if web_server is not None:
            web_server.should_exit = True
        if beamer.enabled and beamer.standby_at_shutdown:
            try:
                beamer.standby()
            except Exception as e:  # noqa: BLE001
                log.warning("beamer standby at shutdown failed: %s", e)
        controller.shutdown()
        if web_thread is not None:
            web_thread.join(timeout=3)
    notifier.shutdown(2.0)
    notify_mod.end_run(settings.paths.data_dir)
    log.info("bye")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="projektor", description="Theater-Projektor video player")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the projector app")
    run.add_argument("--dev", action="store_true", help="PC dev mode (default config app/dev/projektor.toml)")
    run.add_argument("--config", type=Path, default=None, help="settings TOML")
    run.add_argument("--sink", choices=["auto", "gl", "kms", "window", "fakesink", "glfakesink"],
                     default=None, help="override display.sink")
    run.add_argument("--player", choices=["gst", "fake"], default="gst",
                     help="fake = no video output (headless testing)")
    run.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    _setup_logging(args.log_level, getattr(args, "dev", False))
    if args.cmd == "run":
        return cmd_run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
