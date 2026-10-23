"""Stand-ins for Controller / CommandDispatcher / ShowStore / Settings.

Used by the web tests and by ``app/scripts/web-dev.py`` to run the web UI
standalone (no GStreamer, no core modules needed). They follow the §7 contract
closely enough for the UI: a simulated clock advances ``position`` while playing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

try:
    from pydantic import BaseModel, Field, model_validator
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore


class FakeError(Exception):
    """Mimics ProjektorError (bad cue / video)."""


# ------------------------------------------------------------------ settings


def dev_settings(data_dir, password_hash: str = "", **backup) -> SimpleNamespace:
    data_dir = Path(data_dir)
    return SimpleNamespace(
        device=SimpleNamespace(name="projektor-dev"),
        paths=SimpleNamespace(
            data_dir=data_dir,
            net_status=data_dir / "net.json",
            tunnel_status=data_dir / "tunnel.json",
        ),
        display=SimpleNamespace(sink="window", status_on_boot=True, width=1920, height=1080),
        osc=SimpleNamespace(port=9000, reply_host="", reply_port=0, dedup_window_s=1.0),
        lora=SimpleNamespace(enabled=False, device="", baud=115200),
        web=SimpleNamespace(port=8081, password_hash=password_hash, max_upload_mb=8000),
        backup=SimpleNamespace(
            enabled=backup.get("enabled", True),
            host=backup.get("host", "INFMK1_HOST"),
            port=backup.get("port", 22),
            user=backup.get("user", "projbackup"),
            key=backup.get("key", str(data_dir / ".ssh" / "id_backup")),
            remote_dir=backup.get("remote_dir", ""),
        ),
    )


# ------------------------------------------------------------------ show model

if BaseModel is not None:

    class FakeVideo(BaseModel):
        file: str
        title: str = ""
        in_point: float = 0.0
        out_point: float | None = None
        pause_points: list[float] = Field(default_factory=list)
        fade_in: float = 0.0
        fade_out: float = 0.0
        end: str = "black"

        @model_validator(mode="after")
        def _check(self):
            if self.end not in ("black", "hold", "loop"):
                raise ValueError("end must be black|hold|loop")
            return self

    class FakeCue(BaseModel):
        id: str
        name: str = ""
        video: str
        fade_in: float | None = None
        fade_out: float | None = None
        pause_points: list[float] | None = None
        end: str | None = None
        start_at: float | None = None

    class FakeShow(BaseModel):
        version: int = 1
        videos: dict[str, FakeVideo] = Field(default_factory=dict)
        cues: list[FakeCue] = Field(default_factory=list)

        @model_validator(mode="after")
        def _refs(self):
            ids = set()
            for c in self.cues:
                if c.video not in self.videos:
                    raise ValueError(f"cue {c.id}: unknown video {c.video!r}")
                if c.id in ids:
                    raise ValueError(f"duplicate cue id {c.id!r}")
                ids.add(c.id)
            return self


class FakeShowStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "show.json"
        self.media_dir = self.data_dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.saves = 0

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {"version": 1, "videos": {}, "cues": []}
        return FakeShow.model_validate(data)

    def save(self, show) -> None:
        if isinstance(show, dict):
            show = FakeShow.model_validate(show)
        FakeShow.model_validate(show.model_dump())  # re-validate
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(show.model_dump(mode="json"), indent=2))
        os.replace(tmp, self.path)
        self.saves += 1


# ------------------------------------------------------------------ controller


class FakeController:
    """Simulated controller: records calls, advances position with wall clock."""

    def __init__(self, show_store: FakeShowStore | None = None, notify_thread: bool = False):
        self.store = show_store
        self.calls: list[tuple] = []
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._notify_thread = notify_thread
        self._show = None
        self._st = {
            "state": "idle", "pause_reason": None, "cue": None, "cue_index": None,
            "standby": None, "standby_index": None, "video": None, "position": 0.0,
            "duration": None, "status_screen": True, "last_command": None,
            "transports": {"osc": "listening :9000 (fake)", "lora": "disabled"},
        }
        self._t0 = None  # monotonic time at position 0 while playing
        self._pos = 0.0
        self.reload_show()

    # -- helpers
    def _cues(self):
        return list(self._show.cues) if self._show is not None else []

    def _resolve(self, ref):
        cues = self._cues()
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit() and not any(c.id == ref for c in cues)):
            i = int(ref) - 1
            if 0 <= i < len(cues):
                return i
            raise FakeError(f"unknown cue {ref}")
        for i, c in enumerate(cues):
            if c.id == str(ref):
                return i
        raise FakeError(f"unknown cue {ref}")

    def _record(self, name, *args):
        self.calls.append((name, *args))

    def record_command(self, address, args, source):
        with self._lock:
            self._st["last_command"] = {"address": address, "args": list(args), "source": source, "time": time.time()}
        self._notify()

    def _set_standby(self, idx):
        cues = self._cues()
        if idx is not None and 0 <= idx < len(cues):
            self._st["standby"], self._st["standby_index"] = cues[idx].id, idx
        else:
            self._st["standby"], self._st["standby_index"] = None, None

    def _start_video(self, vid, start=0.0):
        v = self._show.videos[vid] if self._show else None
        self._st.update(state="playing", pause_reason=None, video=vid, status_screen=False)
        self._st["duration"] = 60.0 if not v or v.out_point is None else v.out_point - v.in_point
        self._pos = start
        self._t0 = time.monotonic() - start

    def _notify(self):
        st = self.state()
        subs = list(self._subs)

        def fire():
            for cb in subs:
                try:
                    cb(st)
                except Exception:
                    pass

        if self._notify_thread:
            threading.Thread(target=fire, daemon=True).start()
        else:
            fire()

    # -- commands
    def go(self):
        with self._lock:
            self._record("go")
            if self._st["state"] == "paused":
                self._resume()
            elif self._st["standby_index"] is not None:
                self._run(self._st["standby_index"])
        self._notify()

    def _run(self, idx):
        c = self._cues()[idx]
        self._st["cue"], self._st["cue_index"] = c.id, idx
        self._start_video(c.video, c.start_at or 0.0)
        self._set_standby(idx + 1)

    def cue(self, ref):
        with self._lock:
            idx = self._resolve(ref)
            self._record("cue", ref)
            if not (self._st["cue_index"] == idx and self._st["state"] in ("playing", "paused", "holding")):
                self._run(idx)
            else:
                self._set_standby(idx + 1)
        self._notify()

    def standby(self, ref):
        with self._lock:
            idx = self._resolve(ref)
            self._record("standby", ref)
            self._set_standby(idx)
        self._notify()

    def play(self, video_id, fade_in=None):
        with self._lock:
            if not self._show or video_id not in self._show.videos:
                raise FakeError(f"unknown video {video_id}")
            self._record("play", video_id, fade_in)
            self._st["cue"], self._st["cue_index"] = None, None
            self._start_video(video_id)
        self._notify()

    def pause(self):
        with self._lock:
            self._record("pause")
            if self._st["state"] == "playing":
                self._pos = self._position()
                self._t0 = None
                self._st.update(state="paused", pause_reason="manual")
        self._notify()

    def _resume(self):
        self._t0 = time.monotonic() - self._pos
        self._st.update(state="playing", pause_reason=None)

    def resume(self):
        with self._lock:
            self._record("resume")
            if self._st["state"] == "paused":
                self._resume()
        self._notify()

    def fadeout(self, seconds=None):
        with self._lock:
            self._record("fadeout", seconds)
            self._idle()
        self._notify()

    def stop(self):
        with self._lock:
            self._record("stop")
            self._idle()
        self._notify()

    def _idle(self):
        self._st.update(state="idle", pause_reason=None, video=None, duration=None, cue=None, cue_index=None)
        self._t0, self._pos = None, 0.0

    def seek(self, seconds):
        with self._lock:
            self._record("seek", seconds)
            if self._st["video"]:
                self._pos = max(0.0, float(seconds))
                if self._t0 is not None:
                    self._t0 = time.monotonic() - self._pos
        self._notify()

    def show_status(self, on):
        with self._lock:
            self._record("status", 1 if on else 0)
            if self._st["state"] == "idle":
                self._st["status_screen"] = bool(on)
        self._notify()

    def reload_show(self):
        with self._lock:
            self.calls.append(("reload_show",))
            if self.store is not None:
                self._show = self.store.load()
            if self._st.get("standby") is None and self._cues():
                self._set_standby(0)
        self._notify()

    # -- queries
    def _position(self):
        if self._t0 is None:
            return self._pos
        return time.monotonic() - self._t0

    def state(self):
        with self._lock:
            st = dict(self._st)
            pos = self._position()
            dur = st.get("duration")
            if st["state"] == "playing" and dur and pos >= dur:
                # end of video: behave like end=black
                self._idle()
                st = dict(self._st)
                pos = 0.0
            st["position"] = round(pos, 3)
            return st

    def subscribe(self, cb):
        with self._lock:
            self._subs.append(cb)

        def unsubscribe():
            with self._lock:
                if cb in self._subs:
                    self._subs.remove(cb)

        return unsubscribe

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


class FakeDispatcher:
    """Maps a few OSC addresses onto the fake controller, records calls."""

    def __init__(self, controller: FakeController, settings=None):
        self.controller = controller
        self.calls: list[tuple] = []

    def handle(self, address: str, args: list, source: str, reply=None):
        self.calls.append((address, list(args), source))
        c = self.controller
        name = address.rsplit("/", 1)[-1]
        if address.startswith("/proj/"):
            c.record_command(address, args, source)
        try:
            if address == "/proj/ping":
                return [("/proj/pong", ["projektor-dev"])]
            if address == "/proj/state":
                return [("/proj/state", [json.dumps(c.state())])]
            if address == "/proj/status":
                c.show_status(bool(args and int(args[0])))
            elif address == "/proj/reload":
                c.reload_show()
            elif name in ("go", "pause", "resume", "stop", "fadeout", "cue", "standby", "play", "seek") and address.startswith("/proj/"):
                getattr(c, name)(*args)
            else:
                return [("/proj/error", [f"unknown address {address}"])]
        except Exception as e:
            return [("/proj/error", [str(e)])]
        return []
