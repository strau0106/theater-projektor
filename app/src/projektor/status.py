"""Status-screen text (shown on HDMI from boot until the first show command).

Reads /run/projektor/net.json and tunnel.json (ARCHITECTURE.md §6); tolerates missing or
invalid files. Refreshed ~1 s by the controller tick while visible.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

from .config import Settings

STALE_S = 60


def read_json(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def local_ip() -> str | None:
    """Primary local IPv4 address (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("0.") else ip


def _stale(d: dict[str, Any], now: float) -> bool:
    upd = d.get("updated")
    return isinstance(upd, (int, float)) and now - upd > STALE_S


def _web_url(ip: str | None, port: int) -> str:
    if not ip:
        return "unknown"
    return f"http://{ip}/" if port == 80 else f"http://{ip}:{port}/"


def build_status_text(settings: Settings, state: dict[str, Any] | None = None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    state = state or {}
    lines: list[str] = []
    name = settings.device.name
    lines.append(f"THEATER-PROJEKTOR   {name}")
    lines.append("=" * 44)

    net = read_json(settings.paths.net_status)
    web_ip: str | None = None
    if net is None:
        ip = local_ip()
        lines.append("WiFi     status unknown (no net.json)")
        lines.append(f"IP       {ip or 'none'}" + ("  (detected)" if ip else ""))
        web_ip = ip
    else:
        st = str(net.get("state") or "unknown")
        ssid = net.get("ssid") or ""
        ip = net.get("ip") or ""
        stale = "  (stale)" if _stale(net, now) else ""
        if st == "connected":
            lines.append(f'WiFi     connected  "{ssid}"{stale}')
            lines.append(f"IP       {ip or 'unknown'}")
            web_ip = ip or None
        elif st == "connecting":
            att = net.get("attempt")
            nxt = net.get("next_retry_s")
            detail = []
            if ssid:
                detail.append(f'"{ssid}"')
            if att is not None:
                detail.append(f"attempt {att}")
            if nxt is not None:
                detail.append(f"next retry in {nxt} s")
            lines.append(f"WiFi     connecting  {', '.join(detail)}{stale}".rstrip())
        elif st == "hotspot":
            lines.append(f"WiFi     no known network -> hotspot{stale}")
        else:
            lines.append(f"WiFi     {st}{stale}")
        hs_ssid = net.get("hotspot_ssid")
        hs_ip = net.get("hotspot_ip")
        if st == "hotspot" and (hs_ssid or hs_ip):
            lines.append(f"Hotspot  {hs_ssid or '?'}   {hs_ip or ''}".rstrip())
            web_ip = web_ip or hs_ip
        if web_ip is None and st != "hotspot":
            web_ip = ip or None

    lines.append(f"Web      {_web_url(web_ip, settings.web.port)}")
    transports = state.get("transports") or {}
    lines.append(f"OSC      udp {settings.osc.port}   {transports.get('osc', '')}".rstrip())
    lines.append(f"LoRa     {transports.get('lora', 'disabled' if not settings.lora.enabled else 'unknown')}")

    tun = read_json(settings.paths.tunnel_status)
    if tun is None:
        lines.append("Tunnel   unknown")
    else:
        stale = "  (stale)" if _stale(tun, now) else ""
        remote = tun.get("remote") or ""
        lines.append(f"Tunnel   {tun.get('state') or 'unknown'}  {remote}{stale}".rstrip())

    sb = state.get("standby")
    lines.append(f"Standby  cue {sb}" if sb is not None else "Standby  -")
    err = state.get("error")
    if err:
        lines.append(f"Error    {str(err)[:60]}")
    lines.append("")
    lines.append("Waiting for first cue ...")
    lines.append(time.strftime("%H:%M:%S", time.localtime(now)))
    return "\n".join(lines)
