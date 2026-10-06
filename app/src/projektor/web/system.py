"""System helpers for the web UI: net/tunnel status files, WiFi CLI, power, device info."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

WIFI_CLI = "/usr/local/sbin/projektor-wifi"
DEFAULT_WIFI_CMD = f"sudo -n {WIFI_CLI}"
DEFAULT_REBOOT_CMD = "sudo -n /usr/bin/systemctl reboot"  # must match sudoers.d/projektor
DEFAULT_POWEROFF_CMD = "sudo -n /usr/bin/systemctl poweroff"
STUB_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "projektor-wifi-stub.py"


class CommandError(Exception):
    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status


def read_status_file(path) -> dict:
    """Read net.json / tunnel.json, tolerating missing/invalid files (§6)."""
    if not path:
        return {"state": "unknown", "error": "not configured"}
    try:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return {"state": "unknown", "error": "no status file"}
    except (OSError, ValueError) as e:
        return {"state": "unknown", "error": f"invalid status file: {e}"}
    upd = data.get("updated")
    if isinstance(upd, (int, float)):
        data["age_s"] = round(time.time() - upd, 1)
    return data


def _split(cmd) -> list[str]:
    if isinstance(cmd, (list, tuple)):
        return [str(c) for c in cmd]
    return shlex.split(str(cmd))


def system_installed() -> bool:
    """True on the real Pi image (the WiFi manager CLI is installed)."""
    return os.path.exists(WIFI_CLI)


def default_wifi_cmd(data_dir: Path, dev: bool = False) -> list[str]:
    """`$PROJEKTOR_WIFI_CMD`, else the dev stub in dev mode (or when the CLI is not
    installed and the stub is available), else the sudo CLI."""
    env = os.environ.get("PROJEKTOR_WIFI_CMD")
    if env:
        return _split(env)
    if not STUB_SCRIPT.exists() or (system_installed() and not dev):
        return _split(DEFAULT_WIFI_CMD)
    return [sys.executable, str(STUB_SCRIPT), "--state", str(Path(data_dir) / "wifi-stub.json")]


def default_power_cmds(dev: bool = False) -> dict[str, list[str] | None]:
    """Reboot/poweroff commands. In dev mode / off the Pi image they are disabled (dry run)
    unless `$PROJEKTOR_REBOOT_CMD` / `$PROJEKTOR_POWEROFF_CMD` are set - never reboot a dev PC."""
    installed = system_installed() and not dev
    out: dict[str, list[str] | None] = {}
    for key, env, default in (
        ("reboot", "PROJEKTOR_REBOOT_CMD", DEFAULT_REBOOT_CMD),
        ("shutdown", "PROJEKTOR_POWEROFF_CMD", DEFAULT_POWEROFF_CMD),
    ):
        val = os.environ.get(env)
        out[key] = _split(val) if val else (_split(default) if installed else None)
    return out


class WifiCli:
    def __init__(self, cmd, timeout_s: float = 60.0):
        self.cmd = _split(cmd)
        self.timeout_s = timeout_s

    def run(self, *args: str, timeout_s: float | None = None, stdin: str | None = None) -> object:
        argv = self.cmd + [str(a) for a in args]
        try:
            p = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout_s or self.timeout_s,
                input=stdin, stdin=subprocess.DEVNULL if stdin is None else None,
            )
        except FileNotFoundError as e:
            raise CommandError(f"WiFi manager not available: {e}", 503) from e
        except subprocess.TimeoutExpired as e:
            raise CommandError(f"WiFi manager timed out ({args[0] if args else ''})", 504) from e
        out = p.stdout.strip()
        data: object = None
        if out:
            try:
                data = json.loads(out)
            except ValueError:
                # tolerate log lines before a final JSON line
                for line in reversed(out.splitlines()):
                    try:
                        data = json.loads(line)
                        break
                    except ValueError:
                        continue
        if p.returncode != 0:
            msg = None
            if isinstance(data, dict):
                msg = data.get("error") or data.get("message")
            msg = msg or p.stderr.strip() or out or f"exit code {p.returncode}"
            raise CommandError(f"projektor-wifi {args[0] if args else ''}: {msg}", 502)
        if data is None:
            data = {"ok": True, "output": out}
        return data

    # convenience wrappers
    def list(self):
        return self.run("list")

    def scan(self):
        return self.run("scan", timeout_s=max(self.timeout_s, 60))

    def add(self, ssid: str, psk: str, priority: int | None = None, eap: dict | None = None):
        # JSON on stdin keeps passwords out of the process list
        req: dict = {"ssid": ssid, "psk": psk, "priority": int(priority or 0)}
        if eap is not None:
            req["eap"] = eap
        return self.run("add-json", stdin=json.dumps(req))

    def remove(self, ssid: str):
        return self.run("remove", ssid)

    def retry(self):
        return self.run("retry")


def run_power(cmd: list[str] | None, what: str) -> dict:
    if not cmd:
        log.warning("%s requested, but power commands are disabled (not on the Pi image)", what)
        return {"ok": True, "dry_run": True, "message": f"{what}: disabled in dev mode"}
    log.warning("%s requested via web UI: %s", what, " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CommandError(f"{what} failed: {e}") from e
    if p.returncode != 0:
        raise CommandError(f"{what} failed: {p.stderr.strip() or p.returncode}")
    return {"ok": True}


def device_info(data_dir: Path) -> dict:
    info: dict = {"time": time.time()}
    try:
        du = shutil.disk_usage(data_dir)
        info["disk"] = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        info["disk"] = None
    try:
        info["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        info["uptime_s"] = None
    try:
        info["cpu_temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        info["cpu_temp_c"] = None
    try:
        info["load"] = os.getloadavg()[0]
    except OSError:
        info["load"] = None
    # Raspberry Pi firmware throttling flags (undervoltage matters on battery power)
    for p in ("/sys/devices/platform/soc/soc:firmware/get_throttled",):
        try:
            val = int(Path(p).read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        info["throttled"] = {
            "raw": hex(val),
            "undervoltage_now": bool(val & 0x1),
            "throttled_now": bool(val & 0x4),
            "undervoltage_since_boot": bool(val & 0x10000),
            "throttled_since_boot": bool(val & 0x40000),
        }
    try:
        info["model"] = Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except OSError:
        info["model"] = None
    return info
