"""Discord notifications + auto-backup (no real network, no rsync)."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from projektor import notify as nt
from projektor.autobackup import AutoBackup, backoff_s


def wait_for(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def mk_settings(tmp_path, url="", mention="", interval=0.0, **backup):
    return SimpleNamespace(
        device=SimpleNamespace(name="dev1"),
        paths=SimpleNamespace(data_dir=tmp_path, net_status=tmp_path / "net.json"),
        notify=SimpleNamespace(discord_webhook=url, mention=mention, min_interval_s=interval,
                               temp_warn_c=75.0, temp_hysteresis_c=5.0),
        backup=SimpleNamespace(auto=True, auto_delay_s=0.1, **backup))


class Hook:
    def __init__(self, port=0, status=204, status_seq=None):
        self.got, self.status_seq = [], list(status_seq or [])
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                st = outer.status_seq.pop(0) if outer.status_seq else status
                if st == 429:
                    data = json.dumps({"retry_after": 0.2}).encode()
                    self.send_response(429); self.send_header("Content-Length", str(len(data))); self.end_headers()
                    self.wfile.write(data)
                    return
                outer.got.append(body)
                self.send_response(st); self.send_header("Content-Length", "0"); self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", port), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/api/webhooks/1/SECRET"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


@pytest.fixture
def hook():
    h = Hook()
    yield h
    h.close()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def test_delivery_mention_and_message(tmp_path, hook):
    (tmp_path / "net.json").write_text(json.dumps({"state": "connected", "ip": "192.168.1.9"}))
    n = nt.Notifier(mk_settings(tmp_path, hook.url, "<@123456789>"), version="9.9").start()
    n.event("a", "warning", "Hot", "details here")
    n.event("b", "info", "Fine")
    assert wait_for(lambda: len(hook.got) == 2)
    w, i = hook.got
    assert w["content"].startswith("<@123456789> ") and "192.168.1.9" in w["content"] and "dev1" in w["content"]
    assert w["allowed_mentions"] == {"parse": [], "users": ["123456789"]}
    assert "<@" not in i["content"] and i["allowed_mentions"] == {"parse": []}
    assert "SECRET" not in nt.mask_url(hook.url)


def test_disabled_and_test_result(tmp_path):
    n = nt.Notifier(mk_settings(tmp_path, "")).start()
    assert n.test() == "disabled" and n.event("x", "info", "t") is False


def test_rate_limit(tmp_path, hook):
    n = nt.Notifier(mk_settings(tmp_path, hook.url, interval=60)).start()
    assert n.event("x", "info", "one") and not n.event("x", "info", "two") and n.event("y", "info", "other")
    assert wait_for(lambda: len(hook.got) == 2)
    assert n.test() == "queued" and n.test() == "queued"  # test bypasses the limit


def test_truncation(tmp_path, hook):
    n = nt.Notifier(mk_settings(tmp_path, hook.url)).start()
    n.event("x", "error", "big", "z" * 5000)
    assert wait_for(lambda: hook.got) and len(hook.got[0]["content"]) <= 1900


def test_offline_outbox_then_delivered(tmp_path):
    port = free_port()
    url = f"http://127.0.0.1:{port}/api/webhooks/1/SECRET"
    n = nt.Notifier(mk_settings(tmp_path, url))
    n.backoff = (0.2,)
    n.start()
    n.event("x", "error", "offline event")
    outbox = tmp_path / ".notify_outbox.jsonl"
    assert wait_for(outbox.exists)
    assert len(outbox.read_text().splitlines()) == 1
    h = Hook(port)
    try:
        assert wait_for(lambda: len(h.got) == 1, 8)
        assert wait_for(lambda: not outbox.exists())
    finally:
        h.close()


def test_outbox_survives_restart_and_bounded(tmp_path, hook):
    (tmp_path / ".notify_outbox.jsonl").write_text("".join(
        json.dumps({"ts": time.time(), "type": "t", "payload": {"content": f"m{i}"}}) + "\n" for i in range(250)))
    n = nt.Notifier(mk_settings(tmp_path, hook.url)).start()
    assert wait_for(lambda: len(hook.got) == 200, 10)
    assert hook.got[0]["content"] == "m50"


def test_429_retry(tmp_path):
    h = Hook(status_seq=[429])
    try:
        n = nt.Notifier(mk_settings(tmp_path, h.url)).start()
        n.event("x", "info", "later")
        assert wait_for(lambda: len(h.got) == 1, 5)
    finally:
        h.close()


def test_crash_marker(tmp_path):
    assert nt.begin_run(tmp_path) is None
    assert (tmp_path / ".running").exists()
    prev = nt.begin_run(tmp_path)  # no clean exit in between
    assert prev is not None
    nt.end_run(tmp_path)
    assert not (tmp_path / ".running").exists() and nt.begin_run(tmp_path) is None


def test_startup_crash_events(tmp_path, hook):
    n = nt.Notifier(mk_settings(tmp_path, hook.url), version="1.2").start()
    n.startup(time.time() - 100)
    assert wait_for(lambda: len(hook.got) == 2)
    assert "Crash detected" in hook.got[0]["content"] and "1.2" in hook.got[1]["content"]


def test_shutdown_budget(tmp_path):
    n = nt.Notifier(mk_settings(tmp_path, f"http://127.0.0.1:{free_port()}/x")).start()
    t = time.monotonic()
    n.shutdown(1.0)
    assert time.monotonic() - t < 2.5


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_excepthook(tmp_path, hook):
    import sys
    old = (sys.excepthook, threading.excepthook)
    try:
        n = nt.Notifier(mk_settings(tmp_path, hook.url)).start()
        n.install_excepthooks()
        t = threading.Thread(target=lambda: 1 / 0, name="boom")
        t.start(); t.join()
        assert wait_for(lambda: hook.got) and "ZeroDivisionError" in hook.got[0]["content"]
    finally:
        sys.excepthook, threading.excepthook = old


class Rec:
    enabled = True

    def __init__(self):
        self.ev = []

    def event(self, etype, level, title, details="", force=False):
        self.ev.append((etype, level))


def test_temperature_hysteresis(tmp_path):
    f = tmp_path / "temp"
    r = Rec()
    m = nt.Monitors(r, mk_settings(tmp_path), temp_path=f, throttled_path=tmp_path / "nope", use_vcgencmd=False)
    for milli in (60000, 76000, 80000, 72000, 69000, 76000):
        f.write_text(str(milli)); m.check_temp()
    assert r.ev == [("temp_high", "warning"), ("temp_ok", "info"), ("temp_high", "warning")]


def test_throttled_new_flags(tmp_path):
    f = tmp_path / "thr"
    r = Rec()
    m = nt.Monitors(r, mk_settings(tmp_path), temp_path=tmp_path / "x", throttled_path=f, use_vcgencmd=False)
    for v in ("0x0", "0x1", "0x1", "0x50005", "0x0"):
        f.write_text(v); m.check_throttled()
    assert r.ev == [("throttled", "warning"), ("throttled", "warning")]  # undervoltage, then throttled bit


def test_lora_transitions(tmp_path):
    r = Rec()
    st = ["disabled"]
    m = nt.Monitors(r, mk_settings(tmp_path), lora_status=lambda: st[0])
    for s in ("disabled", "connected /dev/ttyUSB0", "connected /dev/ttyUSB0", "absent", "absent",
              "connected /dev/ttyUSB1", "disabled"):
        st[0] = s; m.check_lora()
    assert r.ev == [("lora_down", "warning"), ("lora_up", "info")]


# ------------------------------------------------------------------ auto backup


class FakeMgr:
    enabled = True
    configured = True

    def status(self):
        return {"state": "idle"}


def mk_auto(tmp_path, runner, **kw):
    a = AutoBackup(FakeMgr(), mk_settings(tmp_path), runner=runner, poll_s=0.02, **kw)
    return a.start()


def test_backoff_values():
    assert [backoff_s(i) for i in (1, 2, 3, 4, 5, 6, 7, 20)] == [30, 60, 120, 240, 480, 900, 900, 900]


def test_debounce_coalesce(tmp_path):
    calls = []
    a = mk_auto(tmp_path, lambda: calls.append(time.time()))
    for _ in range(5):
        a.trigger(); time.sleep(0.02)
    assert a.pending
    assert wait_for(lambda: len(calls) == 1 and not a.pending)
    time.sleep(0.4)
    assert len(calls) == 1 and a.status()["last_success"]
    a.stop()


def test_trigger_during_run_runs_again(tmp_path):
    started, release, calls = threading.Event(), threading.Event(), []

    def runner():
        calls.append(1); started.set(); release.wait(3)

    a = mk_auto(tmp_path, runner)
    a.trigger()
    assert started.wait(3)
    a.trigger()
    release.set()
    assert wait_for(lambda: len(calls) == 2 and not a.pending)
    a.stop()


def test_failure_keeps_pending_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("projektor.autobackup.backoff_s", lambda n: 0.1)
    n = {"c": 0}

    def runner():
        n["c"] += 1
        if n["c"] < 3:
            raise OSError("host unreachable")

    a = mk_auto(tmp_path, runner)
    a.trigger()
    assert wait_for(lambda: n["c"] >= 1 and a.status()["last_error"])
    assert a.pending and a.status()["next_retry"]
    assert wait_for(lambda: n["c"] == 3 and not a.pending)
    assert a.status()["last_error"] == ""
    a.stop()


def test_retry_when_net_connects(tmp_path, monkeypatch):
    monkeypatch.setattr("projektor.autobackup.backoff_s", lambda n: 3600)
    net, ok, calls = ["offline"], [False], []

    def runner():
        calls.append(1)
        if not ok[0]:
            raise RuntimeError("no route")

    a = mk_auto(tmp_path, runner, net_state=lambda: net[0])
    a.trigger()
    assert wait_for(lambda: len(calls) == 1)
    time.sleep(0.2)
    assert len(calls) == 1 and a.pending
    ok[0], net[0] = True, "connected"
    assert wait_for(lambda: len(calls) == 2 and not a.pending)
    a.stop()


def test_pending_flag_resumes_after_restart(tmp_path):
    (tmp_path / ".backup_pending").touch()
    calls = []
    a = mk_auto(tmp_path, lambda: calls.append(1))
    assert wait_for(lambda: calls and not a.pending)
    a.stop()


def test_disabled_does_nothing(tmp_path):
    s = mk_settings(tmp_path)
    mgr = FakeMgr(); mgr.enabled = False
    a = AutoBackup(mgr, s, runner=lambda: pytest.fail("ran")).start()
    a.trigger()
    time.sleep(0.3)
    assert not a.pending
