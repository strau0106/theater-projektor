"""`projektor-lora-config`: read / write the config of a Waveshare USB-TO-LoRa-HF adapter.

Protocol as documented in the Waveshare wiki (https://www.waveshare.com/wiki/USB-TO-LoRa-xF)
and the preset command list shipped with Waveshare's sscom tool
(SX1262-LoRa-DTU-sscom5.13.1.zip, sscom51.ini):

* UART 115200 8N1, module starts in transparent ("stream") mode.
* ``+++\\r\\n`` enters AT mode, ``AT+EXIT\\r\\n`` leaves it. Settings only take effect
  after AT+EXIT (wiki FAQ). Since firmware V1.1 they survive power-off.
* Set: ``AT+SF=7``, ``AT+BW=...``, ``AT+CR=1``, ``AT+PWR=10``, ``AT+NETID=0``, ``AT+LBT=0``,
  ``AT+MODE=1``, ``AT+TXCH=18``, ``AT+RXCH=18``, ``AT+RSSI=0``, ``AT+ADDR=0``, ``AT+KEY=0``,
  ``AT+BAUD=115200``, ``AT+COMM="8N1"``, ``AT+PORT=3``, ``AT+RESTORE=1``.
* Query: the same names with ``?`` (``AT+SF?``), ``AT+AllP?``, ``AT+VER``, ``AT+HELP``.

Uncertain (see docs/lora.md, "Open points"):

* TODO: the response format is not documented. Parsing here is tolerant (last integer in
  the answer) and raw answers are always printed.
* TODO: AT+BW takes 0/1/2 per the wiki but 125/250/500 per the sscom presets. ``--bw-style
  auto`` (default) queries AT+BW? first and uses the style the module answers with.
* TODO: channel -> frequency. The wiki only says "0~80 ... 850~930MHz", default 18 = 868 MHz,
  i.e. f = 850 + ch MHz (1 MHz raster). Not verified with a spectrum analyser.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

HF_BASE_MHZ = 850.0
CHANNELS = range(0, 81)

# Profile used for BOTH adapters (QLab bridge and Pi). Radio values = factory defaults
# except power and address, so an unconfigured pair still talks to each other.
PROFILE_PROJEKTOR = {
    "sf": 7,          # spreading factor
    "bw": 500,        # kHz; tested 2026-10-06: RTT 62 ms vs 178 ms at 125 kHz, 0 % loss
    "cr": 1,          # 1 = 4/5
    "power": 10,      # dBm conducted; 6 dBi stock antenna -> ~14 dBm ERP (EU limit 25 mW ERP)
    "netid": 0,
    "lbt": 0,         # off: deterministic latency (LBT can delay up to 2 s)
    "mode": 1,        # stream / transparent
    "channel": 17,    # 867.0 MHz: 500 kHz fits inside 865-868 MHz (1 %, 25 mW); ch 18 straddles 868.0
    "rssi": 0,        # no RSSI byte appended to received data
    "addr": 0,        # both adapters must use the same address in stream mode (factory 0)
    "key": 0,         # no AES
}
PROFILES = {"projektor": PROFILE_PROJEKTOR, "factory": {
    "sf": 7, "bw": 125, "cr": 1, "power": 22, "netid": 0, "lbt": 0, "mode": 1,
    "channel": 18, "rssi": 0, "addr": 0, "key": 0,
}}

AIR_RATES = {  # name -> (sf, bw kHz)
    "fast": (7, 250),
    "default": (7, 125),
    "robust": (9, 125),
    "far": (12, 125),
}

BW_INDEX = {125: 0, 250: 1, 500: 2}
BW_KHZ = {v: k for k, v in BW_INDEX.items()}

QUERY_KEYS = ["SF", "BW", "CR", "PWR", "NETID", "LBT", "MODE", "TXCH", "RXCH", "RSSI", "ADDR",
              "PORT", "COMM", "BAUD", "KEY"]


def channel_for_freq(freq_mhz: float) -> int:
    ch = round(freq_mhz - HF_BASE_MHZ)
    if ch not in CHANNELS:
        raise ValueError(f"{freq_mhz} MHz outside 850..930 MHz (HF adapter)")
    return ch


def freq_for_channel(ch: int) -> float:
    return HF_BASE_MHZ + ch


def eu868_note(ch: int, power_dbm: int, antenna_dbi: float = 6.0) -> list[str]:
    """Regulatory hints (ETSI EN 300 220 / ERC REC 70-03 annex 1)."""
    notes = []
    f = freq_for_channel(ch)
    if not 865.0 < f < 868.6:
        notes.append(
            f"WARNING: {f:.1f} MHz is outside 865-868.6 MHz (the 1 % duty cycle sub-bands); "
            "check EU868 rules (863-865/868.7-869.2: 0.1 %, >870: not licence-exempt in the EU)."
        )
    erp = power_dbm + antenna_dbi - 2.15
    if erp > 14.0 + 0.05:
        notes.append(
            f"WARNING: {power_dbm} dBm + {antenna_dbi:g} dBi antenna = {erp:.1f} dBm ERP > 14 dBm (25 mW) EU limit."
        )
    return notes


@dataclass
class Target:
    sf: int
    bw: int
    cr: int
    power: int
    netid: int
    lbt: int
    mode: int
    channel: int
    rssi: int
    addr: int
    key: int

    def commands(self, bw_style: str = "index") -> list[str]:
        bw_val = BW_INDEX[self.bw] if bw_style == "index" else self.bw
        return [
            f"AT+MODE={self.mode}",
            f"AT+SF={self.sf}",
            f"AT+BW={bw_val}",
            f"AT+CR={self.cr}",
            f"AT+PWR={self.power}",
            f"AT+NETID={self.netid}",
            f"AT+LBT={self.lbt}",
            f"AT+TXCH={self.channel}",
            f"AT+RXCH={self.channel}",
            f"AT+RSSI={self.rssi}",
            f"AT+ADDR={self.addr}",
            f"AT+KEY={self.key}",
        ]

    def expected(self) -> dict[str, int]:
        return {"SF": self.sf, "CR": self.cr, "PWR": self.power, "NETID": self.netid,
                "LBT": self.lbt, "MODE": self.mode, "TXCH": self.channel, "RXCH": self.channel,
                "RSSI": self.rssi, "ADDR": self.addr}


# --------------------------------------------------------------------------- serial session


ANSWER_IDLE_S = 0.3  # an answer is complete after this much silence


class AtSession:
    def __init__(self, ser: Any, verbose: bool = False, idle_s: float | None = None, timeout_s: float = 2.0):
        self.ser = ser
        self.verbose = verbose
        self.idle_s = ANSWER_IDLE_S if idle_s is None else idle_s
        self.timeout_s = timeout_s

    def _read_answer(self) -> str:
        buf = bytearray()
        start = last = time.monotonic()
        while True:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            now = time.monotonic()
            if chunk:
                buf += chunk
                last = now
            elif buf and now - last >= self.idle_s:
                break
            if now - start >= self.timeout_s:
                break
        return buf.decode("ascii", errors="replace")

    def send(self, cmd: str) -> str:
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(cmd.encode("ascii") + b"\r\n")
        self.ser.flush()
        ans = self._read_answer()
        if self.verbose:
            print(f">> {cmd}\n<< {ans.strip()!r}")
        return ans

    def enter(self) -> str:
        time.sleep(0.2)  # guard time before +++ (not documented; harmless)
        return self.send("+++")

    def exit(self) -> str:
        return self.send("AT+EXIT")


def strip_echo(cmd: str, answer: str) -> str:
    lines = [ln.strip() for ln in answer.replace("\r", "\n").split("\n") if ln.strip()]
    return " | ".join(ln for ln in lines if ln != cmd)


def parse_value(answer: str) -> int | None:
    """Best-effort: last integer in the answer (format undocumented)."""
    nums = re.findall(r"-?\d+", answer)
    return int(nums[-1]) if nums else None


def looks_like_error(answer: str) -> bool:
    a = answer.upper()
    return "ERR" in a or "FAIL" in a or "INVALID" in a


def open_port(port: str, baud: int) -> Any:
    import serial

    from .transport.lora import resolve_device

    path = resolve_device(port)
    if path is None:
        raise SystemExit(f"no serial device matching {port}")
    return serial.Serial(path, baud, timeout=0.05, write_timeout=1.0)


def show(sess: AtSession) -> dict[str, str]:
    out: dict[str, str] = {}
    for cmd in ["AT+VER"] + [f"AT+{k}?" for k in QUERY_KEYS] + ["AT+AllP?"]:
        ans = strip_echo(cmd, sess.send(cmd))
        out[cmd] = ans
        print(f"{cmd:12s} {ans or '(no answer)'}")
    return out


def detect_bw_style(sess: AtSession) -> str:
    val = parse_value(strip_echo("AT+BW?", sess.send("AT+BW?")))
    if val in (125, 250, 500):
        return "khz"
    if val in (0, 1, 2):
        return "index"
    print(f"could not detect AT+BW style (answer value {val}); using wiki style (0/1/2)")
    return "index"


def apply(sess: AtSession, target: Target, bw_style: str) -> bool:
    if bw_style == "auto":
        bw_style = detect_bw_style(sess)
        print(f"AT+BW style: {bw_style}")
    ok = True
    for cmd in target.commands(bw_style):
        ans = strip_echo(cmd, sess.send(cmd))
        bad = looks_like_error(ans)
        ok &= not bad
        print(f"{cmd:16s} {'FAILED ' if bad else ''}{ans or '(no answer)'}")
    # verify what we can parse
    for key, want in target.expected().items():
        cmd = f"AT+{key}?"
        got = parse_value(strip_echo(cmd, sess.send(cmd)))
        if got is None:
            print(f"verify {key}: no numeric answer (cannot verify)")
        elif got != want:
            print(f"verify {key}: MISMATCH module says {got}, wanted {want}")
            ok = False
    return ok


# --------------------------------------------------------------------------- CLI


def build_target(args: argparse.Namespace) -> Target:
    vals = dict(PROFILES[args.profile])
    if args.air_rate:
        rate = args.air_rate.lower()
        m = re.fullmatch(r"sf(\d+)[/-]?bw(\d+)", rate)
        if rate in AIR_RATES:
            vals["sf"], vals["bw"] = AIR_RATES[rate]
        elif m:
            vals["sf"], vals["bw"] = int(m.group(1)), int(m.group(2))
        else:
            raise SystemExit(f"--air-rate: use one of {', '.join(AIR_RATES)} or e.g. SF7/BW125")
    for name in ("sf", "bw", "cr", "power", "netid", "addr", "key", "lbt", "rssi"):
        v = getattr(args, name)
        if v is not None:
            vals[name] = v
    if args.channel is not None:
        vals["channel"] = args.channel
    elif args.freq is not None:
        ch = channel_for_freq(args.freq)
        if abs(freq_for_channel(ch) - args.freq) > 0.001:
            print(f"note: {args.freq} MHz not on the 1 MHz channel raster, using channel {ch} "
                  f"= {freq_for_channel(ch):.1f} MHz (both adapters must use the same channel)")
        vals["channel"] = ch
    t = Target(**vals)
    if not 7 <= t.sf <= 12:
        raise SystemExit("--sf must be 7..12")
    if t.bw not in BW_INDEX:
        raise SystemExit("--bw must be 125, 250 or 500")
    if not 1 <= t.cr <= 4:
        raise SystemExit("--cr must be 1..4 (4/5..4/8)")
    if not 10 <= t.power <= 22:
        raise SystemExit("--power must be 10..22 dBm")
    if t.channel not in CHANNELS:
        raise SystemExit("channel must be 0..80")
    if not 0 <= t.addr <= 65535 or not 0 <= t.key <= 65535 or not 0 <= t.netid <= 255:
        raise SystemExit("addr/key 0..65535, netid 0..255")
    return t


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="projektor-lora-config",
        description="Read / write the configuration of a Waveshare USB-TO-LoRa-HF adapter (AT commands). "
                    "Configure BOTH adapters with the same values.",
    )
    p.add_argument("--port", help="serial port (glob allowed)")
    p.add_argument("--baud", type=int, default=115200, help="current UART baud of the module (default 115200)")
    p.add_argument("--show", action="store_true", help="print the current configuration")
    p.add_argument("--apply", action="store_true", help="write the profile (+ overrides) to the module")
    p.add_argument("--profile", choices=sorted(PROFILES), default="projektor")
    p.add_argument("--freq", type=float, help="MHz, mapped to channel = round(freq - 850)")
    p.add_argument("--channel", type=int, help="channel 0..80 (overrides --freq)")
    p.add_argument("--air-rate", help="fast|default|robust|far or SF<n>/BW<khz>")
    p.add_argument("--sf", type=int)
    p.add_argument("--bw", type=int, help="kHz: 125|250|500")
    p.add_argument("--cr", type=int, help="1..4 = 4/5..4/8")
    p.add_argument("--power", type=int, help="dBm 10..22")
    p.add_argument("--netid", type=int)
    p.add_argument("--addr", type=int)
    p.add_argument("--key", type=int, help="AES key 0..65535 (0 = off)")
    p.add_argument("--lbt", type=int, choices=(0, 1))
    p.add_argument("--rssi", type=int, choices=(0, 1))
    p.add_argument("--antenna-dbi", type=float, default=6.0, help="for the ERP check (stock antenna: 6 dBi)")
    p.add_argument("--bw-style", choices=("auto", "index", "khz"), default="auto",
                   help="how AT+BW is encoded (wiki: 0/1/2, sscom presets: 125/250/500)")
    p.add_argument("--raw", action="append", metavar="CMD", help="send raw AT command(s) in AT mode, e.g. --raw AT+HELP")
    p.add_argument("--restore", action="store_true", help="factory reset (AT+RESTORE=1)")
    p.add_argument("--dry-run", action="store_true", help="print the commands, do not touch the port")
    p.add_argument("-v", "--verbose", action="store_true", help="show raw serial traffic")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.show or args.apply or args.raw or args.restore):
        args.show = True
    target = build_target(args) if args.apply else None
    if target is not None:
        f = freq_for_channel(target.channel)
        print(f"target: channel {target.channel} ({f:.1f} MHz), SF{target.sf}/BW{target.bw}/CR4/{target.cr + 4}, "
              f"{target.power} dBm, netid {target.netid}, addr {target.addr}, key {target.key}, "
              f"lbt {target.lbt}, rssi {target.rssi}, mode {target.mode} (stream)")
        for n in eu868_note(target.channel, target.power, args.antenna_dbi):
            print(n)

    if args.dry_run:
        style = "index" if args.bw_style == "auto" else args.bw_style
        print("# dry run - would send (each terminated by CR LF):")
        print("+++")
        if args.show:
            for cmd in ["AT+VER"] + [f"AT+{k}?" for k in QUERY_KEYS] + ["AT+AllP?"]:
                print(cmd)
        for cmd in args.raw or []:
            print(cmd)
        if args.restore:
            print("AT+RESTORE=1")
        if target is not None:
            if args.bw_style == "auto":
                print("AT+BW?        # auto-detect 0/1/2 vs 125/250/500 style (TODO: unverified)")
            for cmd in target.commands(style):
                print(cmd)
            for key in target.expected():
                print(f"AT+{key}?")
        print("AT+EXIT       # settings take effect after exit")
        return 0

    if not args.port:
        print("--port is required (use projektor-bridge --list-ports)", file=sys.stderr)
        return 2
    ser = open_port(args.port, args.baud)
    sess = AtSession(ser, verbose=args.verbose)
    ok = True
    try:
        ans = sess.enter()
        print(f"+++          {strip_echo('+++', ans) or '(no answer)'}")
        if args.show:
            show(sess)
        for cmd in args.raw or []:
            print(f"{cmd:12s} {strip_echo(cmd, sess.send(cmd)) or '(no answer)'}")
        if args.restore:
            print(f"AT+RESTORE=1 {strip_echo('AT+RESTORE=1', sess.send('AT+RESTORE=1')) or '(no answer)'}")
        if target is not None:
            ok = apply(sess, target, args.bw_style)
    finally:
        ans = sess.exit()
        print(f"AT+EXIT      {strip_echo('AT+EXIT', ans) or '(no answer)'}")
        ser.close()
    if target is not None:
        print("done" if ok else "finished WITH PROBLEMS - check output above (use -v for raw traffic)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
