"""FastAPI app: REST API + WebSocket live state + static single-page UI.

    create_app(settings, controller, dispatcher, show_store) -> FastAPI

Everything under ``/api`` (except login/auth status) requires a valid session cookie
when ``settings.web.password_hash`` is set. Transport endpoints call the Controller
directly (§7); the raw command box goes through ``dispatcher.handle(..., "web")``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .. import ProjektorError
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from projektor.backup import BackupBusy, BackupError, BackupManager
from projektor.web import auth
from projektor.web.media import UPLOAD_PREFIX, Prober, codec_warnings, list_media_files, safe_filename
from projektor.web.showedit import (
    ShowAccess,
    ShowEditError,
    apply_cue_fields,
    apply_video_fields,
    cues_using,
    find_cue,
    new_video_entry,
    next_cue_id,
    slug,
    unique_id,
)
from projektor.web.system import (
    CommandError,
    WifiCli,
    default_power_cmds,
    default_wifi_cmd,
    device_info,
    read_status_file,
    run_power,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
WRITE_CHUNK = 4 * 1024 * 1024
DISK_RESERVE = 256 * 1024 * 1024  # keep free for show.json, logs, ...
STATE_PUSH_INTERVAL = 0.5


def _cfg(obj: Any, dotted: str, default: Any = None) -> Any:
    for part in dotted.split("."):
        if obj is None:
            return default
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
    return default if obj is None else obj


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("projektor")
    except Exception:
        return "dev"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def parse_command_line(line: str) -> tuple[str, list]:
    """'/proj/cue 3' -> ('/proj/cue', [3]); numbers become int/float, quotes allowed."""
    try:
        toks = shlex.split(line)
    except ValueError as e:
        raise HTTPException(400, f"cannot parse command: {e}") from e
    if not toks:
        raise HTTPException(400, "empty command")
    args: list = []
    for t in toks[1:]:
        try:
            args.append(int(t))
            continue
        except ValueError:
            pass
        try:
            args.append(float(t))
            continue
        except ValueError:
            pass
        args.append(t)
    return toks[0], args


def create_app(
    settings: Any,
    controller: Any,
    dispatcher: Any,
    show_store: Any,
    *,
    wifi_cmd: Any = None,
    power_cmds: dict | None = None,
    backup: BackupManager | None = None,
    prober: Prober | None = None,
    beamer: Any = None,
    notifier: Any = None,
    static_dir: Path | None = None,
) -> FastAPI:
    data_dir = Path(_cfg(settings, "paths.data_dir", "."))
    data_dir.mkdir(parents=True, exist_ok=True)
    media_dir = Path(getattr(show_store, "media_dir", None) or data_dir / "media")
    media_dir.mkdir(parents=True, exist_ok=True)
    device_name = str(_cfg(settings, "device.name", "projektor"))
    max_upload = int(float(_cfg(settings, "web.max_upload_mb", 8000)) * 1024 * 1024)

    sessions = auth.SessionManager(
        auth.load_or_create_secret(data_dir / ".web_secret"), str(_cfg(settings, "web.password_hash", "") or "")
    )
    access = ShowAccess(show_store, controller)
    dev_mode = bool(getattr(settings, "dev", False))
    wifi = WifiCli(wifi_cmd if wifi_cmd is not None else _cfg(settings, "web.wifi_cmd") or default_wifi_cmd(data_dir, dev_mode))
    power = power_cmds if power_cmds is not None else default_power_cmds(dev_mode)
    backup_mgr = backup or BackupManager(settings, show_store, controller)
    auto_backup = None
    if backup is None:  # injected managers (tests) get no background auto-backup
        from projektor.autobackup import AutoBackup

        auto_backup = AutoBackup(backup_mgr, settings).start()
        backup_mgr.auto = auto_backup
        access.on_change = auto_backup.trigger
    prober = prober or Prober()
    notifier = notifier or getattr(dispatcher, "notifier", None)
    static_dir = Path(static_dir or STATIC_DIR)
    if beamer is None:
        from ..beamer import Beamer

        beamer = getattr(dispatcher, "beamer", None) or Beamer(settings)

    # leftovers of interrupted uploads (power loss) - no upload can be running yet
    for p in media_dir.glob(UPLOAD_PREFIX + "*"):
        try:
            p.unlink()
            log.info("removed stale partial upload %s", p.name)
        except OSError:
            pass

    app = FastAPI(title="Projektor", version=_version(), docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.controller = controller
    app.state.dispatcher = dispatcher
    app.state.show_store = show_store
    app.state.backup = backup_mgr
    app.state.sessions = sessions

    @app.exception_handler(ShowEditError)
    async def _show_err(request: Request, exc: ShowEditError):
        return JSONResponse({"detail": exc.detail if exc.detail is not None else str(exc)}, status_code=exc.status)

    @app.exception_handler(CommandError)
    async def _cmd_err(request: Request, exc: CommandError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(BackupError)
    async def _backup_err(request: Request, exc: BackupError):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, BackupBusy) else 400)

    def require_auth(request: Request) -> None:
        if not sessions.valid(request.cookies.get(auth.COOKIE_NAME)):
            raise HTTPException(401, "login required")

    public = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    # ------------------------------------------------------------------ auth

    @public.get("/auth")
    def auth_status(request: Request):
        return {
            "auth_required": sessions.auth_required,
            "authenticated": sessions.valid(request.cookies.get(auth.COOKIE_NAME)),
            "device": device_name,
        }

    @public.post("/login")
    def login(body: dict = Body(...)):
        pw = str(body.get("password", ""))
        if sessions.auth_required and not sessions.check_password(pw):
            time.sleep(1.0)  # slow down guessing
            raise HTTPException(401, "wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            auth.COOKIE_NAME, sessions.issue(), max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax", path="/"
        )
        return resp

    @public.post("/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE_NAME, path="/")
        return resp

    # ------------------------------------------------------------------ info / state

    @api.get("/info")
    def info():
        return {
            "device": device_name,
            "version": _version(),
            "max_upload_mb": max_upload // (1024 * 1024),
            "auth_required": sessions.auth_required,
            "backup_enabled": backup_mgr.enabled,
            "power_enabled": {k: bool(v) for k, v in power.items()},
        }

    @api.get("/state")
    def get_state():
        return controller.state()

    # ------------------------------------------------------------------ show

    @api.get("/show")
    def get_show():
        return access.get()

    @api.put("/show")
    def put_show(body: Any = Body(...)):
        return access.put(body)

    @api.get("/show/download")
    def download_show():
        show = access.get()
        import json as _json

        fname = f"show-{slug(device_name)}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(show, indent=2, ensure_ascii=False) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # ------------------------------------------------------------------ videos

    def _video_view(vid: str, v: dict, show: dict, files: dict, probe: bool) -> dict:
        f = files.get(v.get("file"))
        out = {"id": vid, **v, "exists": f is not None, "size": f["size"] if f else None, "used_by": cues_using(show, vid)}
        if probe and f:
            p = prober.probe(media_dir / v["file"])
            out["probe"] = p
            out["warnings"] = codec_warnings(p)
        elif not f:
            out["warnings"] = [f"file missing in media dir: {v.get('file')}"]
        else:
            out["warnings"] = []
        return out

    @api.get("/videos")
    def list_videos(probe: bool = True):
        show = access.get()
        files = {f["name"]: f for f in list_media_files(media_dir)}
        videos = [_video_view(vid, v, show, files, probe) for vid, v in show.get("videos", {}).items()]
        referenced = {v.get("file") for v in show.get("videos", {}).values()}
        orphans = [f for name, f in files.items() if name not in referenced]
        if probe:
            for f in orphans:
                f["probe"] = prober.probe(media_dir / f["name"])
                f["warnings"] = codec_warnings(f["probe"])
        try:
            du = shutil.disk_usage(media_dir)
            disk = {"total": du.total, "free": du.free}
        except OSError:
            disk = None
        return {"videos": videos, "files": orphans, "disk": disk, "max_upload_mb": max_upload // (1024 * 1024)}

    @api.get("/videos/{vid}")
    def get_video(vid: str, probe: bool = True):
        show = access.get()
        if vid not in show.get("videos", {}):
            raise HTTPException(404, f"unknown video: {vid}")
        files = {f["name"]: f for f in list_media_files(media_dir)}
        return _video_view(vid, show["videos"][vid], show, files, probe)

    @api.post("/videos")
    def create_video(body: dict = Body(...)):
        file = body.get("file")
        if not file or not (media_dir / str(file)).is_file():
            raise HTTPException(400, f"file not found in media dir: {file}")
        fields = {k: v for k, v in body.items() if k not in ("id", "file")}

        def mut(show):
            vids = show.setdefault("videos", {})
            vid = str(body.get("id") or "").strip() or unique_id(slug(Path(file).stem), vids)
            if vid in vids:
                raise ShowEditError(f"video id exists: {vid}", 409)
            entry = new_video_entry(str(file))
            apply_video_fields(entry, fields)
            vids[vid] = entry
            return vid

        vid = access.modify(mut)
        return {"ok": True, "id": vid}

    @api.patch("/videos/{vid}")
    def patch_video(vid: str, body: dict = Body(...)):
        fields = {k: v for k, v in body.items() if k not in ("id", "file")}

        def mut(show):
            v = show.get("videos", {}).get(vid)
            if v is None:
                raise ShowEditError(f"unknown video: {vid}", 404)
            apply_video_fields(v, fields)
            return v

        return {"ok": True, "video": access.modify(mut)}

    @api.post("/videos/{vid}/rename")
    def rename_video(vid: str, body: dict = Body(...)):
        new_id = str(body.get("new_id") or "").strip() or None
        new_file = body.get("file")
        with access.lock:
            show = access.get()
            vids = show.get("videos", {})
            if vid not in vids:
                raise HTTPException(404, f"unknown video: {vid}")
            if new_id and new_id != vid:
                if new_id in vids:
                    raise HTTPException(409, f"video id exists: {new_id}")
                show["videos"] = {(new_id if k == vid else k): v for k, v in vids.items()}
                for c in show.get("cues", []):
                    if c.get("video") == vid:
                        c["video"] = new_id
                vid = new_id
            renamed = None
            if new_file:
                try:
                    fname = safe_filename(str(new_file))
                except ValueError as e:
                    raise HTTPException(400, str(e)) from e
                old = show["videos"][vid]["file"]
                if fname != old:
                    src, dst = media_dir / old, media_dir / fname
                    if dst.exists():
                        raise HTTPException(409, f"file exists: {fname}")
                    if src.exists():
                        os.rename(src, dst)
                        _fsync_dir(media_dir)
                        renamed = (src, dst)
                    for v in show["videos"].values():
                        if v.get("file") == old:
                            v["file"] = fname
            try:
                access.put(show)
            except Exception:
                if renamed:
                    os.rename(renamed[1], renamed[0])
                raise
        return {"ok": True, "id": vid, "file": show["videos"][vid]["file"]}

    @api.delete("/videos/{vid}")
    def delete_video(vid: str, force: bool = False, delete_file: bool = True):
        with access.lock:
            show = access.get()
            v = show.get("videos", {}).get(vid)
            if v is None:
                raise HTTPException(404, f"unknown video: {vid}")
            used = cues_using(show, vid)
            if used and not force:
                raise HTTPException(409, {"msg": f"video {vid} is used by cues {', '.join(used)}", "cues": used})
            del show["videos"][vid]
            show["cues"] = [c for c in show.get("cues", []) if c.get("video") != vid]
            access.put(show)
            removed_file = False
            still_used = any(o.get("file") == v.get("file") for o in show["videos"].values())
            if delete_file and not still_used:
                try:
                    (media_dir / v["file"]).unlink()
                    _fsync_dir(media_dir)
                    removed_file = True
                except FileNotFoundError:
                    pass
        return {"ok": True, "removed_cues": used, "removed_file": removed_file}

    @api.delete("/files/{name}")
    def delete_file(name: str):
        try:
            fname = safe_filename(name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        with access.lock:
            show = access.get()
            users = [vid for vid, v in show.get("videos", {}).items() if v.get("file") == fname]
            if users:
                raise HTTPException(409, f"file is used by video(s) {', '.join(users)} - delete those first")
            try:
                (media_dir / fname).unlink()
            except FileNotFoundError:
                raise HTTPException(404, f"no such file: {fname}") from None
            _fsync_dir(media_dir)
        if auto_backup is not None:
            auto_backup.trigger("file deleted")
        return {"ok": True}

    @api.get("/videos/{vid}/probe")
    def probe_video(vid: str):
        show = access.get()
        v = show.get("videos", {}).get(vid)
        if v is None:
            raise HTTPException(404, f"unknown video: {vid}")
        p = prober.probe(media_dir / v["file"])
        return {"probe": p, "warnings": codec_warnings(p)}

    @api.put("/upload")
    async def upload(request: Request, name: str, overwrite: bool = False, add: bool = True):
        """Streaming raw-body upload (XHR ``send(file)``): temp file in media dir,
        fsync, atomic rename. Query: ``name`` (file name), ``overwrite``, ``add``
        (create a video entry for a new file)."""
        try:
            fname = safe_filename(name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        dest = media_dir / fname
        if dest.exists() and not overwrite:
            raise HTTPException(409, f"file exists: {fname}")
        length = request.headers.get("content-length")
        expected = int(length) if length and length.isdigit() else None
        if expected is not None and expected > max_upload:
            raise HTTPException(413, f"file too large (max {max_upload // (1024 * 1024)} MB)")
        free = shutil.disk_usage(media_dir).free
        if expected is not None and expected > free - DISK_RESERVE:
            raise HTTPException(507, f"not enough disk space ({free // (1024 * 1024)} MB free)")

        tmp = media_dir / f"{UPLOAD_PREFIX}{uuid.uuid4().hex[:8]}-{fname}"
        total = 0
        t0 = time.monotonic()
        f = await run_in_threadpool(open, tmp, "wb")
        try:
            buf = bytearray()
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_upload:
                    raise HTTPException(413, f"file too large (max {max_upload // (1024 * 1024)} MB)")
                buf += chunk
                if len(buf) >= WRITE_CHUNK:
                    data, buf = bytes(buf), bytearray()
                    await run_in_threadpool(f.write, data)
            if buf:
                await run_in_threadpool(f.write, bytes(buf))
            if expected is not None and total != expected:
                raise HTTPException(400, f"upload incomplete ({total} of {expected} bytes)")
            if total == 0:
                raise HTTPException(400, "empty upload")

            def finish():
                f.flush()
                os.fsync(f.fileno())
                f.close()
                os.replace(tmp, dest)
                _fsync_dir(media_dir)

            await run_in_threadpool(finish)
        except ClientDisconnect:
            log.warning("upload of %s aborted by client after %d bytes", fname, total)
            raise HTTPException(400, "upload aborted") from None
        finally:
            if not f.closed:
                f.close()
            if tmp.exists():
                tmp.unlink()
        dt = time.monotonic() - t0
        log.info("uploaded %s: %d bytes in %.1fs (%.1f MB/s)", fname, total, dt, total / 1e6 / max(dt, 1e-6))

        video_id = None
        if add:
            def mut(show):
                vids = show.setdefault("videos", {})
                for vid, v in vids.items():
                    if v.get("file") == fname:
                        return vid
                vid = unique_id(slug(Path(fname).stem), vids)
                vids[vid] = new_video_entry(fname)
                return vid

            video_id = await run_in_threadpool(access.modify, mut)
        if auto_backup is not None:
            auto_backup.trigger("upload")
        probe = await run_in_threadpool(prober.probe, dest)
        return {"ok": True, "file": fname, "size": total, "video_id": video_id, "probe": probe,
                "warnings": codec_warnings(probe)}

    # ------------------------------------------------------------------ cues

    @api.get("/cues")
    def list_cues():
        return access.get().get("cues", [])

    @api.post("/cues")
    def create_cue(body: dict = Body(...)):
        index = body.pop("index", None)

        def mut(show):
            cues = show.setdefault("cues", [])
            cue = {"id": None, "name": "", "video": None, "fade_in": None, "fade_out": None,
                   "pause_points": None, "end": None, "start_at": None}
            fields = dict(body)
            if not str(fields.get("id") or "").strip():
                fields["id"] = next_cue_id(cues)
            apply_cue_fields(cue, fields)
            if cue["video"] not in show.get("videos", {}):
                raise ShowEditError(f"unknown video: {cue['video']}", 422)
            if any(str(c.get("id")) == cue["id"] for c in cues):
                raise ShowEditError(f"cue id exists: {cue['id']}", 409)
            if index is None:
                cues.append(cue)
            else:
                cues.insert(max(0, min(int(index), len(cues))), cue)
            return cue

        return {"ok": True, "cue": access.modify(mut)}

    @api.put("/cues/{cue_id}")
    @api.patch("/cues/{cue_id}")
    def update_cue(cue_id: str, body: dict = Body(...)):
        def mut(show):
            cues = show.get("cues", [])
            i = find_cue(cues, cue_id)
            cue = dict(cues[i])
            apply_cue_fields(cue, body)
            if cue["id"] != cue_id and any(str(c.get("id")) == cue["id"] for c in cues):
                raise ShowEditError(f"cue id exists: {cue['id']}", 409)
            if cue["video"] not in show.get("videos", {}):
                raise ShowEditError(f"unknown video: {cue['video']}", 422)
            cues[i] = cue
            return cue

        return {"ok": True, "cue": access.modify(mut)}

    @api.delete("/cues/{cue_id}")
    def delete_cue(cue_id: str):
        def mut(show):
            cues = show.get("cues", [])
            del cues[find_cue(cues, cue_id)]

        access.modify(mut)
        return {"ok": True}

    @api.post("/cues/reorder")
    def reorder_cues(body: dict = Body(...)):
        order = [str(x) for x in body.get("order", [])]

        def mut(show):
            cues = show.get("cues", [])
            by_id = {str(c.get("id")): c for c in cues}
            if sorted(order) != sorted(by_id) or len(order) != len(cues):
                raise ShowEditError("order must list every cue id exactly once", 422)
            show["cues"] = [by_id[i] for i in order]
            return order

        return {"ok": True, "order": access.modify(mut)}

    # ------------------------------------------------------------------ transport

    def _num(body: dict, key: str, required: bool = False) -> float | None:
        v = body.get(key)
        if v is None or v == "":
            if required:
                raise HTTPException(400, f"{key} required")
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key}: not a number") from None

    def _ref(body: dict):
        ref = body.get("ref")
        if ref is None or ref == "":
            raise HTTPException(400, "ref required (cue id or 1-based number)")
        return ref

    actions = {
        "go": lambda b: controller.go(),
        "cue": lambda b: controller.cue(_ref(b)),
        "standby": lambda b: controller.standby(_ref(b)),
        "play": lambda b: controller.play(str(b.get("video") or b.get("video_id") or ""), _num(b, "fade_in")),
        "pause": lambda b: controller.pause(),
        "resume": lambda b: controller.resume(),
        "fadeout": lambda b: controller.fadeout(_num(b, "seconds")),
        "stop": lambda b: controller.stop(),
        "seek": lambda b: controller.seek(_num(b, "seconds", required=True)),
        "status": lambda b: controller.show_status(bool(b.get("on"))),
        "reload": lambda b: controller.reload_show(),
    }

    def _osc_args(action: str, b: dict) -> list:
        """OSC-equivalent args, only for last_command display."""
        if action in ("cue", "standby"):
            return [b.get("ref")]
        if action == "play":
            return [x for x in (b.get("video") or b.get("video_id"), b.get("fade_in")) if x not in (None, "")]
        if action in ("fadeout", "seek"):
            return [b["seconds"]] if b.get("seconds") not in (None, "") else []
        if action == "status":
            return [1 if b.get("on") else 0]
        return []

    @api.post("/transport/{action}")
    def transport(action: str, body: dict | None = Body(None)):
        fn = actions.get(action)
        if fn is None:
            raise HTTPException(404, f"unknown action: {action}")
        body = body or {}
        record = getattr(controller, "record_command", None)
        if callable(record):
            try:
                record(f"/proj/{action}", _osc_args(action, body), "web")
            except Exception:
                log.exception("record_command failed")
        try:
            fn(body)
        except HTTPException:
            raise
        except Exception as e:  # ProjektorError: unknown cue / video, bad args
            log.info("transport %s failed: %s", action, e)
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "state": controller.state()}

    @api.post("/command")
    def raw_command(body: dict = Body(...)):
        if body.get("line"):
            address, args = parse_command_line(str(body["line"]))
        else:
            address, args = str(body.get("address", "")), body.get("args") or []
            if not isinstance(args, list):
                raise HTTPException(400, "args must be a list")
        if not address.startswith("/"):
            raise HTTPException(400, "OSC address must start with '/'")
        try:
            replies = dispatcher.handle(address, args, "web") or []
        except Exception as e:
            log.exception("dispatcher failed for %s", address)
            raise HTTPException(500, f"dispatcher error: {e}") from e
        return {"ok": True, "address": address, "args": args,
                "replies": [{"address": a, "args": list(r)} for a, r in replies]}

    # ------------------------------------------------------------------ beamer (HDMI-CEC)

    @api.get("/beamer")
    def beamer_status():
        return {"enabled": beamer.enabled, "status": beamer.status()}

    @api.post("/beamer/power")
    def beamer_power(body: dict = Body(...)):
        try:
            beamer.power_on() if bool(body.get("on")) else beamer.standby()
        except ProjektorError as e:
            raise HTTPException(502, str(e)) from e
        return {"ok": True}

    @api.post("/beamer/key")
    def beamer_key(body: dict = Body(...)):
        try:
            beamer.key(str(body.get("key", "")))
        except ProjektorError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

    # ------------------------------------------------------------------ system

    @api.get("/system")
    def system_status():
        return {
            "device": device_name,
            "net": read_status_file(_cfg(settings, "paths.net_status")),
            "tunnel": read_status_file(_cfg(settings, "paths.tunnel_status")),
            "info": device_info(data_dir),
            "power_enabled": {k: bool(v) for k, v in power.items()},
        }

    @api.get("/wifi")
    def wifi_list():
        return wifi.list()

    @api.post("/wifi")
    def wifi_add(body: dict = Body(...)):
        ssid = str(body.get("ssid") or "").strip()
        security = str(body.get("security") or "psk").lower()
        psk = str(body.get("psk") or "")
        if not ssid:
            raise HTTPException(400, "ssid required")
        prio = body.get("priority")
        try:
            prio = int(prio) if prio not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "priority must be an integer") from None
        eap = None
        if security == "eap":
            eap = {k: str(body.get(k) or "") for k in (
                "identity", "password", "method", "phase2", "anonymous_identity", "domain_suffix", "ca_cert")}
            if not eap["identity"] or not eap["password"]:
                raise HTTPException(400, "enterprise network needs identity and password")
            psk = ""
        elif security == "open":
            psk = ""
        elif security == "psk":
            if not 8 <= len(psk) <= 63:
                raise HTTPException(400, "WPA password must be 8..63 characters")
        else:
            raise HTTPException(400, "security must be psk, eap or open")
        return wifi.add(ssid, psk, prio, eap)

    @api.delete("/wifi/{ssid}")
    def wifi_remove(ssid: str):
        return wifi.remove(ssid)

    @api.post("/wifi/scan")
    def wifi_scan():
        return wifi.scan()

    @api.post("/wifi/retry")
    def wifi_retry():
        return wifi.retry()

    @api.post("/system/{what}")
    def power_action(what: str, body: dict = Body(default={})):
        if what not in ("reboot", "shutdown"):
            raise HTTPException(404, f"unknown action: {what}")
        if not (body or {}).get("confirm"):
            raise HTTPException(400, 'confirmation required: {"confirm": true}')
        return run_power(power.get(what), what)

    # ------------------------------------------------------------------ backup

    @api.get("/backup")
    def backup_status():
        return backup_mgr.status()

    @api.post("/backup/run")
    def backup_run():
        return backup_mgr.backup_now()

    @api.get("/backup/snapshots")
    def backup_snapshots():
        return {"snapshots": backup_mgr.list_snapshots()}

    @api.post("/backup/restore")
    def backup_restore(body: dict = Body(default={})):
        body = body or {}
        return backup_mgr.restore(body.get("snapshot") or "latest", bool(body.get("media")))

    @api.post("/notify/test")
    def notify_test():
        return {"result": notifier.test() if notifier is not None else "disabled"}

    @api.post("/backup/cancel")
    def backup_cancel():
        return {"ok": backup_mgr.cancel()}

    app.include_router(public)
    app.include_router(api)

    # ------------------------------------------------------------------ websocket

    @app.websocket("/api/ws")
    async def ws_state(ws: WebSocket):
        if not sessions.valid(ws.cookies.get(auth.COOKIE_NAME)):
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()
        latest: dict = {}

        def _deliver(st: dict) -> None:  # runs in the event loop
            latest["state"] = st
            changed.set()

        def on_change(st: dict) -> None:  # called from controller/GLib thread
            try:
                loop.call_soon_threadsafe(_deliver, st)
            except RuntimeError:
                pass  # loop closed

        unsubscribe = controller.subscribe(on_change)

        async def sender():
            await ws.send_json({"type": "state", "state": controller.state()})
            while True:
                try:
                    await asyncio.wait_for(changed.wait(), STATE_PUSH_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                if changed.is_set():
                    changed.clear()
                    st = latest.pop("state", None) or controller.state()
                else:
                    st = controller.state()
                await ws.send_json({"type": "state", "state": st})

        async def receiver():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") == "ping":
                    await ws.send_json({"type": "pong"})

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError, OSError)):
                    log.debug("websocket closed: %r", exc)
        finally:
            for t in tasks:
                t.cancel()
            unsubscribe()

    # ------------------------------------------------------------------ static UI

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app
