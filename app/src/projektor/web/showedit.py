"""Show (show.json) access for the web layer.

The web layer works on plain dicts (the §6 JSON shape) and converts to/from the
``Show`` object of ``ShowStore`` only at the edges, so it does not depend on the
concrete model implementation in ``config.py``.
"""

from __future__ import annotations

import copy
import dataclasses
import re
import threading
from typing import Any

VIDEO_FIELDS = ("file", "title", "in_point", "out_point", "pause_points", "fade_in", "fade_out", "end")
CUE_FIELDS = ("id", "name", "video", "fade_in", "fade_out", "pause_points", "end", "start_at")
END_MODES = ("black", "hold", "loop")


class ShowEditError(Exception):
    """Bad request on show data (-> HTTP 4xx)."""

    def __init__(self, msg: str, status: int = 400, detail: Any = None):
        super().__init__(msg)
        self.status = status
        self.detail = detail if detail is not None else msg


def show_to_dict(show: Any) -> dict:
    if isinstance(show, dict):
        return copy.deepcopy(show)
    if hasattr(show, "model_dump"):
        return show.model_dump(mode="json")
    if hasattr(show, "to_dict"):
        return show.to_dict()
    if dataclasses.is_dataclass(show):
        return dataclasses.asdict(show)
    raise TypeError(f"cannot convert {type(show).__name__} to dict")


def dict_to_show(data: dict, cls: type | None) -> Any:
    """Build a Show object of the same type ShowStore returns (validates)."""
    if cls is None or cls is dict:
        return data
    if hasattr(cls, "model_validate"):
        return cls.model_validate(data)
    if hasattr(cls, "from_dict"):
        return cls.from_dict(data)
    return cls(**data)


def validation_detail(exc: Exception) -> Any:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return [
                {"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in errors()
            ]
        except Exception:  # pragma: no cover - defensive
            pass
    return str(exc)


class ShowAccess:
    """Serialised read-modify-write access to the ShowStore."""

    def __init__(self, store: Any, controller: Any):
        self.store = store
        self.controller = controller
        self.lock = threading.RLock()
        self._cls: type | None = None

    def get(self) -> dict:
        with self.lock:
            show = self.store.load()
            self._cls = type(show)
            return show_to_dict(show)

    def put(self, data: dict, *, reload: bool = True) -> dict:
        """Validate + save ``data`` via the ShowStore, then reload the controller."""
        if not isinstance(data, dict):
            raise ShowEditError("show must be a JSON object", 422)
        with self.lock:
            if self._cls is None:
                try:
                    self._cls = type(self.store.load())
                except Exception:
                    self._cls = None
            try:
                show = dict_to_show(data, self._cls)
                self.store.save(show)
            except ShowEditError:
                raise
            except Exception as e:  # validation / ProjektorError / ValueError
                raise ShowEditError(f"invalid show: {e}", 422, validation_detail(e)) from e
            result = self.get()
        if reload:
            try:
                self.controller.reload_show()
            except Exception as e:  # saved, but the controller complained
                raise ShowEditError(f"saved, but reload failed: {e}", 500) from e
        return result

    def modify(self, fn) -> Any:
        """Apply ``fn(show_dict)`` (mutating) and save. Returns fn's return value."""
        with self.lock:
            show = self.get()
            ret = fn(show)
            self.put(show)
            return ret


# ---------------------------------------------------------------- helpers

_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def slug(text: str) -> str:
    s = _ID_RE.sub("-", text).strip("-_").lower()
    return s or "video"


def unique_id(base: str, existing) -> str:
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def new_video_entry(file: str, title: str | None = None) -> dict:
    """Minimal entry; timing defaults (fades, end mode) come from the Show model."""
    return {"file": file, "title": title if title is not None else file.rsplit(".", 1)[0]}


def _num(v, field: str, *, allow_none: bool) -> float | None:
    if v is None or v == "":
        if allow_none:
            return None
        raise ShowEditError(f"{field}: value required", 422)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ShowEditError(f"{field}: not a number: {v!r}", 422) from None
    if f < 0:
        raise ShowEditError(f"{field}: must be >= 0", 422)
    return f


def _points(v, field: str, *, allow_none: bool) -> list[float] | None:
    if v is None:
        if allow_none:
            return None
        return []
    if isinstance(v, str):
        v = [p for p in re.split(r"[,\s;]+", v.strip()) if p]
    if not isinstance(v, list):
        raise ShowEditError(f"{field}: must be a list of seconds", 422)
    return sorted({_num(p, field, allow_none=False) for p in v})


def apply_video_fields(entry: dict, fields: dict) -> None:
    for k, v in fields.items():
        if k == "title":
            entry["title"] = "" if v is None else str(v)
        elif k in ("in_point",):
            entry[k] = _num(v, k, allow_none=False)
        elif k in ("fade_in", "fade_out"):
            entry[k] = _num(v, k, allow_none=False)
        elif k == "out_point":
            entry[k] = _num(v, k, allow_none=True)
        elif k == "pause_points":
            entry[k] = _points(v, k, allow_none=False)
        elif k == "end":
            if v not in END_MODES:
                raise ShowEditError(f"end must be one of {END_MODES}", 422)
            entry[k] = v
        elif k == "file":
            entry[k] = str(v)
        else:
            raise ShowEditError(f"unknown video field: {k}", 422)
    if entry.get("out_point") is not None and entry["out_point"] <= entry.get("in_point", 0.0):
        raise ShowEditError("out_point must be after in_point", 422)


def apply_cue_fields(cue: dict, fields: dict) -> None:
    for k, v in fields.items():
        if k in ("id", "name", "video"):
            if k != "name" and (v is None or str(v).strip() == ""):
                raise ShowEditError(f"cue {k} required", 422)
            cue[k] = "" if v is None else str(v).strip() if k != "name" else str(v)
        elif k in ("fade_in", "fade_out", "start_at"):
            cue[k] = _num(v, k, allow_none=True)
        elif k == "pause_points":
            cue[k] = _points(v, k, allow_none=True)
        elif k == "end":
            if v in (None, ""):
                cue[k] = None
            elif v in END_MODES:
                cue[k] = v
            else:
                raise ShowEditError(f"end must be null or one of {END_MODES}", 422)
        else:
            raise ShowEditError(f"unknown cue field: {k}", 422)


def next_cue_id(cues: list[dict]) -> str:
    nums = []
    for c in cues:
        try:
            nums.append(float(c.get("id", "")))
        except ValueError:
            pass
    n = int(max(nums)) + 1 if nums else len(cues) + 1
    ids = {c.get("id") for c in cues}
    while str(n) in ids:
        n += 1
    return str(n)


def find_cue(cues: list[dict], cue_id: str) -> int:
    for i, c in enumerate(cues):
        if str(c.get("id")) == str(cue_id):
            return i
    raise ShowEditError(f"unknown cue: {cue_id}", 404)


def cues_using(show: dict, video_id: str) -> list[str]:
    return [str(c.get("id")) for c in show.get("cues", []) if c.get("video") == video_id]
