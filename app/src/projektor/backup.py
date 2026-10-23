"""Backup / restore of everything needed to rebuild a projector, via rsync over SSH (§9).

Server side runs ``rrsync`` restricted to the backup root, so all remote paths are
relative to it::

    <root>/media/                     mirror of data_dir/media (never deletes)
    <root>/show.json                  latest show
    <root>/snapshots/show-<UTC>.json  one per backup run
    <root>/state/                     mirror of data_dir minus media/ and transient files
                                      (results.json, design/ cache, ... - see STATE_EXCLUDES;
                                      a generic rule, so new state files are covered)
    <root>/config/projektor.toml      the settings file
    <root>/config/wifi.json           WiFi networks incl. passwords (mode 0600), read through
                                      ``projektor-wifi export`` (NetworkManager is canonical)
    <root>/snapshots/config-<UTC>.toml, wifi-<UTC>.json
                                      timestamped copies, only written when the content changed

SSH keys (``data_dir/.ssh``) are never backed up. Restore: show (+ ``media``) by default;
``state`` pulls ``state/`` back into data_dir; ``config`` validates and replaces the settings
file (previous one kept as ``<name>.before-restore``, restart needed) and re-adds the WiFi
networks (existing ones are kept).

One job (backup or restore) runs at a time in a background thread; progress is
parsed from ``rsync --info=progress2``. For tests the ssh transport and the remote
location are injectable (``remote="/some/local/dir/"``, ``rsh=None``).
"""

from __future__ import annotations

import hashlib
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

# data_dir entries that are NOT part of the generic state mirror: media (own mirror), SSH keys,
# show.json (own file + snapshots), per-device secrets/status and transient files.
STATE_EXCLUDES = [
    "/media/", "/.ssh/", "/temp/", "/show.json", "/show.before-restore.json", "/.web_secret",
    "/.running", "/.backup_pending", "/.backup-status.json", "/.notify_outbox.jsonl",
    "/net.json", "/tunnel.json", "/snapshot.png", "/wifi-stub.json", "/design.old/",
    "id_backup*", "id_tunnel*", ".upload-*", ".rsync-*", ".design-*", "*.tmp", ".*.tmp",
]


class BackupError(Exception):
    pass


class BackupBusy(BackupError):
    pass


class _Cancelled(BackupError):
    pass


def snapshot_name(ts: float | None = None, kind: str = "show", ext: str = "json") -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime(f"{kind}-%Y%m%dT%H%M%SZ.{ext}")


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
        config_path: Path | str | None = None,
        wifi: Any = None,
    ):
        self.settings = settings
        self.show_store = show_store
        self.controller = controller
        self.cfg = getattr(settings, "backup", None)
        self.data_dir = Path(settings.paths.data_dir)
        self.rsync = rsync
        cp = config_path or getattr(settings, "config_path", None)
        self.config_path = Path(cp) if cp else None
        self.wifi = wifi  # WifiCli-like (export/add); None: WiFi is not backed up / restored
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
        """Start a backup job (media/, state/, show.json, config/, snapshots/)."""
        return self._start("backup", self._do_backup, wait=wait)

    def restore(self, snapshot: str | None = "latest", media: bool = False, state: bool = False,
                config: bool = False, *, wait: bool = False) -> dict:
        """Start a restore job: optional media / state pull, the show (latest or a snapshot) + reload,
        optionally the settings file and WiFi networks (`config`, always the latest backup)."""
        if snapshot in (None, "", "latest"):
            snapshot = None
        elif not SNAPSHOT_RE.match(snapshot):
            raise BackupError(f"invalid snapshot name: {snapshot}")
        return self._start("restore", lambda: self._do_restore(snapshot, media, state, config), wait=wait)

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
        notes: list[str] = []

        # --checksum: state files are small and may change within the same second and size
        self._rsync("state", common + ["--delete", "--checksum"] + [f"--exclude={e}" for e in STATE_EXCLUDES]
                    + [f"{self.data_dir}/", self.remote + "state/"])

        stamp = time.time()
        snap = None
        with tempfile.TemporaryDirectory(prefix="projektor-backup-") as tmp:
            stage = Path(tmp) / "show"
            (stage / "snapshots").mkdir(parents=True)
            if self.show_path.exists():
                snap = snapshot_name(stamp)
                shutil.copy2(self.show_path, stage / "show.json")
                shutil.copy2(self.show_path, stage / "snapshots" / snap)
                self._rsync("show", ["-rt", "--timeout=60", "--info=progress2", f"{stage}/", self.remote])
            else:
                notes.append("no show.json yet")
            # config: private files (settings may hold passwords, the WiFi list always does)
            cstage = Path(tmp) / "cfg"
            (cstage / "config").mkdir(parents=True)
            (cstage / "snapshots").mkdir()
            new_hashes = self._stage_config(cstage, stamp, notes)
            if any((cstage / "config").iterdir()):
                self._rsync("config", ["-rt", "--chmod=D700,F600", "--timeout=60", "--info=progress2",
                                       f"{cstage}/", self.remote])
                self._history.setdefault("hashes", {}).update(new_hashes)  # only after a successful upload
        msg = f"backup complete, snapshot {snap}" if snap else "backup complete"
        return msg + (f" ({'; '.join(notes)})" if notes else "")

    def _stage_config(self, cstage: Path, stamp: float, notes: list[str]) -> dict[str, str]:
        """Copy settings + WiFi export into the staging dir; timestamped copy only when changed."""
        hashes = self._history.get("hashes", {})
        new: dict[str, str] = {}
        items: list[tuple[str, str, bytes]] = []  # (kind, ext, data)
        if self.config_path is not None and self.config_path.is_file():
            try:
                items.append(("config", "toml", self.config_path.read_bytes()))
            except OSError as e:
                notes.append(f"settings not backed up: {e}")
        if self.wifi is not None:
            try:
                res = self.wifi.export()
                nets = res.get("networks") if isinstance(res, dict) else None
                if not isinstance(nets, list):
                    raise BackupError("unexpected reply")
                if nets:  # never overwrite a good backup with an empty list
                    data = json.dumps({"version": 1, "networks": nets}, indent=2, ensure_ascii=False)
                    items.append(("wifi", "json", data.encode()))
                if res.get("skipped"):
                    notes.append(f"{len(res['skipped'])} WiFi network(s) without readable password skipped")
            except Exception as e:  # noqa: BLE001  (old projektor-wifi, no sudo, ...)
                log.warning("WiFi export failed: %s", e)
                notes.append(f"WiFi not backed up: {e}")
        for kind, ext, data in items:
            name = "projektor.toml" if kind == "config" else "wifi.json"
            (cstage / "config" / name).write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            if hashes.get(kind) != digest:
                (cstage / "snapshots" / snapshot_name(stamp, kind, ext)).write_bytes(data)
                new[kind] = digest
        return new

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

    def _pull(self, phase: str, src: str, dst: str, what: str, args: list[str]) -> None:
        try:
            self._rsync(phase, args + [self.remote + src, dst])
        except BackupError as e:
            if "No such file" in str(e) or "[23]" in str(e):
                raise BackupError(f"no {what} in the backup yet (run a backup first)") from e
            raise

    def _do_restore(self, snapshot: str | None, media: bool, state: bool = False, config: bool = False) -> str:
        done: list[str] = []
        notes: list[str] = []
        if media:
            md = self.media_dir
            md.mkdir(parents=True, exist_ok=True)
            self._rsync("media", ["-a", "--partial", "--timeout=120", "--info=progress2", "--no-inc-recursive",
                                  self.remote + "media/", f"{md}/"])
            done.append("media")
        if state:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._pull("state", "state/", f"{self.data_dir}/", "state",
                       ["-a", "--checksum", "--timeout=120", "--info=progress2", "--no-inc-recursive"]
                       + [f"--exclude={e}" for e in STATE_EXCLUDES])
            self._update(phase="apply")
            reload_state = getattr(self.controller, "reload_state", None)
            if reload_state is not None:
                reload_state()
            done.append("state")
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
            done.insert(0, f"snapshot {snapshot}" if snapshot else "latest show")
            if config:
                self._restore_config(Path(tmp), done, notes)
        msg = "restored " + " + ".join(done)
        if notes:
            msg += " (" + "; ".join(notes) + ")"
        return msg

    def _restore_config(self, tmp: Path, done: list[str], notes: list[str]) -> None:
        """Settings file (validated, previous kept) and WiFi networks from config/."""
        cfg_dir = tmp / "config"
        cfg_dir.mkdir()
        self._pull("config", "config/", f"{cfg_dir}/", "config", ["-rt", "--timeout=60", "--info=progress2"])
        toml = cfg_dir / "projektor.toml"
        wifi_json = cfg_dir / "wifi.json"
        if toml.is_file():
            if self.config_path is None:
                notes.append("settings not restored: settings path unknown")
            else:
                self._install_config(toml.read_text(encoding="utf-8"))
                done.append("settings (restart needed)")
        if wifi_json.is_file():
            if self.wifi is None:
                notes.append("WiFi not restored: WiFi manager not available")
            else:
                self._update(phase="wifi")
                n, errs = self._restore_wifi(wifi_json)
                done.append(f"{n} WiFi network(s)")
                notes.extend(errs)
        if not toml.is_file() and not wifi_json.is_file():
            raise BackupError("no config in the backup yet (run a backup first)")

    def _install_config(self, text: str) -> None:
        from projektor.config import Settings
        from projektor import ProjektorError

        try:
            Settings.from_toml(text)
        except ProjektorError as e:
            raise BackupError(f"backed-up settings are invalid, not restored: {e}") from e
        path = self.config_path
        assert path is not None
        if path.exists():
            keep = path.with_name(path.name + ".before-restore")
            try:
                shutil.copy2(path, keep)
            except OSError as e:
                raise BackupError(f"cannot keep the previous settings ({keep}): {e}") from e
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".projektor-", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o640)
            os.replace(tmp, path)
        except OSError as e:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise BackupError(f"cannot write {path}: {e}") from e

    def _restore_wifi(self, path: Path) -> tuple[int, list[str]]:
        try:
            nets = json.loads(path.read_text(encoding="utf-8"))["networks"]
            if not isinstance(nets, list):
                raise TypeError("networks is not a list")
        except (ValueError, KeyError, TypeError, OSError) as e:
            raise BackupError(f"backed-up WiFi list is invalid: {e}") from e
        ok, errs = 0, []
        for n in nets:
            try:
                self.wifi.add(str(n["ssid"]), str(n.get("psk") or ""), int(n.get("priority") or 0), n.get("eap"))
                ok += 1
            except Exception as e:  # noqa: BLE001
                errs.append(f"WiFi '{n.get('ssid') if isinstance(n, dict) else '?'}': {e}")
        return ok, errs

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
