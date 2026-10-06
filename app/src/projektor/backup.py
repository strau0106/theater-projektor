"""Backup / restore of show.json + media to the backup server via rsync over SSH (§9).

Server side runs ``rrsync`` restricted to the backup root, so all remote paths are
relative to it::

    <root>/media/                     mirror of data_dir/media (never deletes)
    <root>/show.json                  latest show
    <root>/snapshots/show-<UTC>.json  one per backup run

One job (backup or restore) runs at a time in a background thread; progress is
parsed from ``rsync --info=progress2``. For tests the ssh transport and the remote
location are injectable (``remote="/some/local/dir/"``, ``rsh=None``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SNAPSHOT_RE = re.compile(r"^show-[0-9]{8}T[0-9]{6}Z\.json$")
_PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d{1,3})%\s+(\S+/s)\s+(\d+:\d{2}:\d{2})")
_LIST_RE = re.compile(r"^([-dl])\S*\s+([\d,]+)\s+(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+)$")
_DEFAULT = object()


class BackupError(Exception):
    pass


class BackupBusy(BackupError):
    pass


class _Cancelled(BackupError):
    pass


def snapshot_name(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("show-%Y%m%dT%H%M%SZ.json")


def parse_progress(line: str) -> dict | None:
    m = _PROGRESS_RE.match(line)
    if not m:
        return None
    return {
        "bytes": int(m.group(1).replace(",", "")),
        "percent": int(m.group(2)),
        "rate": m.group(3),
        "eta": m.group(4),
    }


def parse_list(output: str) -> list[dict]:
    """Parse ``rsync --list-only`` output into snapshot entries (newest first)."""
    snaps = []
    for line in output.splitlines():
        m = _LIST_RE.match(line.strip())
        if not m or m.group(1) != "-":
            continue
        name = m.group(5).strip()
        if not SNAPSHOT_RE.match(name):
            continue
        ts = datetime.strptime(name[5:20], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        snaps.append({"name": name, "size": int(m.group(2).replace(",", "")), "timestamp": ts,
                      "mtime": f"{m.group(3)} {m.group(4)}"})
    snaps.sort(key=lambda s: s["name"], reverse=True)
    return snaps


class BackupManager:
    def __init__(
        self,
        settings: Any,
        show_store: Any,
        controller: Any = None,
        *,
        remote: str | None = None,
        rsh: Any = _DEFAULT,
        rsync: str = "rsync",
        state_file: Path | None = None,
        extra_args: list[str] | None = None,
    ):
        self.settings = settings
        self.show_store = show_store
        self.controller = controller
        self.cfg = getattr(settings, "backup", None)
        self.data_dir = Path(settings.paths.data_dir)
        self.rsync = rsync
        self.extra_args = list(extra_args or [])
        self._injected_remote = remote is not None
        if remote is None:
            remote = self._default_remote()
        self.remote = remote
        self.rsh = self._default_rsh() if rsh is _DEFAULT else rsh
        self.state_file = Path(state_file) if state_file else self.data_dir / ".backup-status.json"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._log: deque[str] = deque(maxlen=30)
        self._status: dict = {"job": None, "state": "idle"}
        self._history = self._load_history()

    # ------------------------------------------------------------- config

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False)) or self._injected_remote

    @property
    def configured(self) -> bool:
        if self._injected_remote:
            return True
        host = str(getattr(self.cfg, "host", "") or "")
        return bool(host) and host != "INFMK1_HOST"

    def _default_remote(self) -> str:
        cfg = self.cfg
        host = getattr(cfg, "host", "") or ""
        user = getattr(cfg, "user", "") or ""
        rdir = str(getattr(cfg, "remote_dir", "") or "").strip("/")
        prefix = f"{user}@{host}" if user else host
        return f"{prefix}:{rdir + '/' if rdir else ''}"

    def _default_rsh(self) -> str:
        cfg = self.cfg
        parts = ["ssh"]
        key = getattr(cfg, "key", "") or ""
        if key:
            parts += ["-i", str(key)]
        parts += ["-p", str(getattr(cfg, "port", 22) or 22)]
        parts += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
                  "-o", "ServerAliveInterval=15"]
        return shlex.join(parts)

    @property
    def media_dir(self) -> Path:
        md = getattr(self.show_store, "media_dir", None)
        return Path(md) if md else self.data_dir / "media"

    @property
    def show_path(self) -> Path:
        p = getattr(self.show_store, "path", None)
        return Path(p) if p else self.data_dir / "show.json"

    def _base_cmd(self) -> list[str]:
        cmd = [self.rsync]
        if self.rsh:
            cmd += ["-e", self.rsh]
        return cmd + self.extra_args

    def _check(self) -> None:
        if not self.enabled:
            raise BackupError("backup is disabled in settings")
        if not self.configured:
            raise BackupError("backup server not configured (settings [backup] host)")

    # ------------------------------------------------------------- status

    def _load_history(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_history(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._history))
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("cannot write %s: %s", self.state_file, e)

    def status(self) -> dict:
        with self._lock:
            st = dict(self._status)
            st["log"] = list(self._log)
        st["enabled"] = self.enabled
        st["configured"] = self.configured
        st["running"] = st.get("state") == "running"
        st["remote"] = self.remote
        st["last_backup"] = self._history.get("backup")
        st["last_restore"] = self._history.get("restore")
        auto = getattr(self, "auto", None)
        st["auto"] = auto.status() if auto is not None else {"enabled": False, "active": False, "pending": False}
        return st

    def _update(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    # ------------------------------------------------------------- jobs

    def backup_now(self, *, wait: bool = False) -> dict:
        """Start a backup job (media/, show.json, snapshots/show-<UTC>.json)."""
        return self._start("backup", self._do_backup, wait=wait)

    def restore(self, snapshot: str | None = "latest", media: bool = False, *, wait: bool = False) -> dict:
        """Start a restore job: optional media pull, then show (latest or a snapshot) + reload."""
        if snapshot in (None, "", "latest"):
            snapshot = None
        elif not SNAPSHOT_RE.match(snapshot):
            raise BackupError(f"invalid snapshot name: {snapshot}")
        return self._start("restore", lambda: self._do_restore(snapshot, media), wait=wait)

    def cancel(self) -> bool:
        with self._lock:
            running = self._status.get("state") == "running"
            proc = self._proc
        if not running:
            return False
        self._cancel.set()
        if proc and proc.poll() is None:
            proc.terminate()
        return True

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t:
            t.join(timeout)

    def _start(self, job: str, fn: Callable[[], str], *, wait: bool) -> dict:
        self._check()
        with self._lock:
            if self._status.get("state") == "running":
                raise BackupBusy(f"a {self._status.get('job')} job is already running")
            self._cancel.clear()
            self._log.clear()
            self._status = {
                "job": job, "state": "running", "phase": "starting", "percent": 0, "bytes": 0,
                "rate": "", "eta": "", "message": "", "started": time.time(), "finished": None,
            }
            self._thread = threading.Thread(target=self._run, args=(job, fn), name=f"projektor-{job}", daemon=True)
            self._thread.start()
        if wait:
            self._thread.join()
        return self.status()

    def _run(self, job: str, fn: Callable[[], str]) -> None:
        try:
            msg = fn()
            state = "done"
        except _Cancelled:
            state, msg = "cancelled", "cancelled"
        except Exception as e:
            log.exception("%s failed", job)
            state, msg = "error", str(e)
        finished = time.time()
        self._update(state=state, message=msg, finished=finished, phase="", **({"percent": 100} if state == "done" else {}))
        with self._lock:
            started = self._status.get("started")
        self._history[job] = {"state": state, "message": msg, "started": started, "finished": finished}
        self._save_history()
        log.info("%s %s: %s", job, state, msg)

    def _rsync(self, phase: str, args: list[str]) -> str:
        """Run rsync with progress parsing. Returns non-progress output."""
        if self._cancel.is_set():
            raise _Cancelled()
        cmd = self._base_cmd() + args
        self._update(phase=phase, percent=0, rate="", eta="")
        log.info("rsync: %s", shlex.join(cmd))
        env = dict(os.environ, LC_ALL="C")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        with self._lock:
            self._proc = proc
        out_lines: list[str] = []
        buf = b""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                self._handle_line(raw.decode("utf-8", "replace"), out_lines)
        if buf:
            self._handle_line(buf.decode("utf-8", "replace"), out_lines)
        rc = proc.wait()
        with self._lock:
            self._proc = None
        if self._cancel.is_set():
            raise _Cancelled()
        if rc != 0:
            tail = "; ".join(out_lines[-3:]) or f"exit code {rc}"
            raise BackupError(f"rsync ({phase}) failed [{rc}]: {tail}")
        return "\n".join(out_lines)

    def _handle_line(self, line: str, out_lines: list[str]) -> None:
        if not line.strip():
            return
        p = parse_progress(line)
        if p:
            self._update(**p)
            return
        out_lines.append(line.rstrip())
        with self._lock:
            self._log.append(line.rstrip())

    # ------------------------------------------------------------- backup

    def _do_backup(self) -> str:
        media = self.media_dir
        media.mkdir(parents=True, exist_ok=True)
        common = ["-a", "--partial", "--timeout=120", "--info=progress2", "--no-inc-recursive"]
        self._rsync("media", common + ["--exclude=.upload-*", "--exclude=.rsync-*", f"{media}/", self.remote + "media/"])

        if not self.show_path.exists():
            return "media backed up (no show.json yet)"
        snap = snapshot_name()
        with tempfile.TemporaryDirectory(prefix="projektor-backup-") as tmp:
            stage = Path(tmp)
            (stage / "snapshots").mkdir()
            shutil.copy2(self.show_path, stage / "show.json")
            shutil.copy2(self.show_path, stage / "snapshots" / snap)
            self._rsync("show", ["-rt", "--timeout=60", "--info=progress2", f"{stage}/", self.remote])
        return f"backup complete, snapshot {snap}"

    # ------------------------------------------------------------- restore

    def list_snapshots(self) -> list[dict]:
        self._check()
        cmd = self._base_cmd() + ["--list-only", "--timeout=30", self.remote + "snapshots/"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, LC_ALL="C"))
        except subprocess.TimeoutExpired as e:
            raise BackupError("listing snapshots timed out") from e
        if p.returncode != 0:
            if "No such file" in p.stderr or "does not exist" in p.stderr:
                return []
            raise BackupError(f"listing snapshots failed: {p.stderr.strip() or p.returncode}")
        return parse_list(p.stdout)

    def _do_restore(self, snapshot: str | None, media: bool) -> str:
        if media:
            md = self.media_dir
            md.mkdir(parents=True, exist_ok=True)
            self._rsync("media", ["-a", "--partial", "--timeout=120", "--info=progress2", "--no-inc-recursive",
                                  self.remote + "media/", f"{md}/"])
        src = self.remote + (f"snapshots/{snapshot}" if snapshot else "show.json")
        with tempfile.TemporaryDirectory(prefix="projektor-restore-") as tmp:
            dst = Path(tmp) / "show.json"
            self._rsync("show", ["-t", "--timeout=60", "--info=progress2", src, str(dst)])
            try:
                data = json.loads(dst.read_text())
            except ValueError as e:
                raise BackupError(f"restored show is not valid JSON: {e}") from e
        self._update(phase="apply")
        self._apply_show(data)
        return f"restored {'snapshot ' + snapshot if snapshot else 'latest show'}" + (" + media" if media else "")

    def _apply_show(self, data: dict) -> None:
        from projektor.web.showedit import dict_to_show

        cls = None
        try:
            cls = type(self.show_store.load())
        except Exception:
            try:
                from projektor.config import Show  # type: ignore

                cls = Show
            except Exception:
                cls = None
        if self.show_path.exists():
            try:
                shutil.copy2(self.show_path, self.data_dir / "show.before-restore.json")
            except OSError as e:
                log.warning("could not keep previous show.json: %s", e)
        try:
            show = dict_to_show(data, cls)
            self.show_store.save(show)
        except Exception as e:
            raise BackupError(f"restored show is invalid: {e}") from e
        if self.controller is not None:
            self.controller.reload_show()
