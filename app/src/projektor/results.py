"""Audience-vote results graph (docs/results.md).

Three independent parts:

* `ResultsStore`: the last results received by `/proj/results/set` (persisted as
  `<data dir>/results.json`, survives a restart).
* `DesignStore`: the visual design (SVG template + fonts). Fetched before the show over
  HTTP into `<data dir>/design/`; without a valid cache the bundled `results_default/` is used.
* `Presenter`: renders the design with librsvg/cairo and animates it. All drawing and all
  animation timing live here; the player only provides an `OverlaySink` (a way to push BGRA
  frames and to fade the whole overlay).

Rendering: the template's layers (`#layer-static`, optional `#bar-ja` / `#bar-nein`,
`#layer-labels` or its `#para-N` children) are rendered once into ARGB surfaces cropped to their ink rectangle; labels
again for every new result (tokens substituted first). Each animation frame is composed with
cairo: static layer, bars (old templates only) clipped to a rectangle that grows from the
baseline, labels with alpha. Only the region the animation touches is redrawn. Frames are
pre-multiplied BGRA (cairo's native ARGB32).

Animation (overlay/paragraph fade times scaled by `[results] in_s / out_s` relative to the
1.3 s / 0.8 s of the contract; `para_gap_s` is absolute; every component is an independent
tween, so a new command mid-animation continues from the current values). The text is a list
of paragraphs (`<g id="para-N">` in `#layer-labels`; none = the whole layer is one paragraph):

    in   overlay alpha 0 -> 1        0.00 .. 0.50 s
         paragraph N alpha 0 -> 1    0.50 + (N-1)*para_gap_s, 0.80 s long (gap = start to start)
         bars (only if the template has them) 0 -> pct, ease-out, with the first paragraph
    out  all text alpha -> 0         0.00 .. 0.60 s   (bars shrink in the same time)
         overlay alpha -> 0                  0.40 .. 0.80 s
    live update (set again while shown): visible paragraphs crossfade, 0.5 s

Threading: one worker thread ("results") does all librsvg/pango work (pango's default font
map is per thread) and pushes the frames; callers (GLib main loop, tests) only enqueue.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import ProjektorError

log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).with_name("results_default")
REQUIRED_IDS = ("layer-static", "layer-labels")
BAR_IDS = ("bar-ja", "bar-nein")  # optional (bar-graph templates of older designs)
FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")
MAX_MANIFEST = 1 << 20
MAX_ASSET = 24 << 20

# nominal animation times (s) at in_s = 1.3 (overlay + one paragraph) / out_s = 0.8
IN_NOMINAL, OUT_NOMINAL = 1.3, 0.8
PARA_GAP_DEFAULT = 2.5  # start of one paragraph to the start of the next
T_ALPHA_IN = 0.5
T_BAR_START, T_BAR_LEN, T_NEIN_DELAY = 0.5, 1.0, 0.0  # bars: only templates that still have them
T_LABELS_IN = 0.8  # fade-in of one paragraph
T_LABELS_XFADE = 0.5  # text crossfade on a live update
T_LABELS_OUT, T_BARS_OUT_START, T_BARS_OUT, T_ALPHA_OUT_START, T_ALPHA_OUT = 0.6, 0.0, 0.6, 0.4, 0.4


# --------------------------------------------------------------------------- numbers


def percentages(ja: int, nein: int) -> tuple[int, int]:
    """Integer percents that sum to 100 (round half up for Ja); (0, 0) without votes."""
    total = ja + nein
    if total <= 0:
        return 0, 0
    pa = (200 * ja + total) // (2 * total)
    return pa, 100 - pa


def ease_out(u: float) -> float:
    return 1.0 - (1.0 - u) ** 3


def ease_in(u: float) -> float:
    return u**3


def linear(u: float) -> float:
    return u


@dataclass
class Tween:
    """v0 -> v1 from t0 for dur seconds (monotonic seconds), shaped by `ease`."""

    v0: float
    v1: float
    t0: float = 0.0
    dur: float = 0.0
    ease: Callable[[float], float] = linear

    def value(self, t: float) -> float:
        if self.dur <= 0:
            return self.v1 if t >= self.t0 else self.v0
        u = (t - self.t0) / self.dur
        if u <= 0:
            return self.v0
        if u >= 1:
            return self.v1
        return self.v0 + (self.v1 - self.v0) * self.ease(u)

    @property
    def end(self) -> float:
        return self.t0 + self.dur


# --------------------------------------------------------------------------- stored results


class ResultsStore:
    """Last results from `/proj/results/set`, persisted as JSON in the data dir."""

    def __init__(self, data_dir: str | os.PathLike):
        self.path = Path(data_dir) / "results.json"
        self._lock = threading.Lock()
        self._data: dict | None = None
        self.reload()

    def reload(self) -> None:
        """(Re-)read results.json (e.g. after a restore from backup)."""
        data = None
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            data = {"ja": int(d["ja"]), "nein": int(d["nein"]), "poll_id": int(d.get("poll_id", 0)),
                    "time": float(d.get("time", 0.0))}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)
        with self._lock:
            self._data = data

    def get(self) -> dict | None:
        with self._lock:
            return dict(self._data) if self._data else None

    def set(self, ja: int, nein: int, poll_id: int = 0) -> dict:
        data = {"ja": int(ja), "nein": int(nein), "poll_id": int(poll_id), "time": time.time()}
        with self._lock:
            self._data = data
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".results-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except OSError as e:
                log.warning("cannot persist results: %s", e)
        return dict(data)


# --------------------------------------------------------------------------- design bundle


@dataclass
class Design:
    version: str
    template: bytes
    width: int
    height: int
    fonts: list[Path] = field(default_factory=list)
    source: str = "default"  # default | cache
    directory: Path | None = None


def validate_template(svg: bytes) -> None:
    """Raise ProjektorError unless `svg` is a usable results template."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        raise ProjektorError(f"template.svg: not valid XML: {e}") from e
    if not root.tag.endswith("}svg") and root.tag != "svg":
        raise ProjektorError("template.svg: root element is not <svg>")
    ids: dict[str, ET.Element] = {el.get("id"): el for el in root.iter() if el.get("id")}
    missing = [i for i in REQUIRED_IDS if i not in ids]
    if missing:
        raise ProjektorError("template.svg: missing element id(s): " + ", ".join(missing))
    if "layer-bars" in ids:
        inside = {el.get("id") for el in ids["layer-bars"].iter()}
        for i in BAR_IDS:
            if i not in inside:
                raise ProjektorError(f"template.svg: #{i} must be inside #layer-bars")
    if _rsvg() is not None:
        try:
            _rsvg().Handle.new_from_data(svg)
        except Exception as e:  # noqa: BLE001 - GLib.Error
            raise ProjektorError(f"template.svg: librsvg cannot load it: {e}") from e


def _check_font(name: str, data: bytes) -> None:
    if not data[:4] in FONT_MAGIC:
        raise ProjektorError(f"font {name}: not a TrueType/OpenType file")


def _safe_name(name: str) -> str:
    n = Path(str(name)).name
    if not n or n in (".", "..") or n != str(name).replace("\\", "/").split("/")[-1]:
        raise ProjektorError(f"bad file name {name!r}")
    return n


def load_bundle(directory: Path, source: str) -> Design:
    """Read manifest + template + fonts from a bundle directory (validates the template)."""
    try:
        man = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        svg = (directory / _safe_name(man.get("template_file") or "template.svg")).read_bytes()
        fonts = [directory / "fonts" / _safe_name(f["file"]) for f in man.get("fonts", [])]
        version = str(man["version"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ProjektorError(f"design bundle {directory}: {e}") from e
    validate_template(svg)
    for f in fonts:
        if not f.is_file():
            raise ProjektorError(f"design bundle {directory}: font file {f.name} missing")
    return Design(version=version, template=svg, width=int(man.get("width", 1920)),
                  height=int(man.get("height", 1080)), fonts=fonts, source=source, directory=directory)


def _http_get(url: str, limit: int, timeout: float = 15.0) -> bytes:
    if not url.lower().startswith(("http://", "https://")):
        raise ProjektorError(f"not an http(s) URL: {url!r}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "projektor"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise ProjektorError(f"GET {url}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ProjektorError(f"GET {url}: {getattr(e, 'reason', e)}") from e
    if len(data) > limit:
        raise ProjektorError(f"GET {url}: response too large")
    return data


class DesignStore:
    """Cached design under `<data dir>/design/`, falling back to the bundled default."""

    def __init__(self, data_dir: str | os.PathLike, default_url: str = "", default_dir: Path = DEFAULT_DIR):
        self.root = Path(data_dir) / "design"
        self.default_url = default_url
        self.default_dir = default_dir
        self._lock = threading.Lock()
        self._fetching = False
        self.last_error: str | None = None
        self.last_fetch: float | None = None
        self._current: Design | None = None

    # -- loading
    def _old(self) -> Path:
        return self.root.with_name(self.root.name + ".old")

    def load(self, force: bool = False) -> Design:
        with self._lock:
            if self._current is not None and not force:
                return self._current
            if not self.root.exists() and self._old().is_dir():  # interrupted swap
                try:
                    os.replace(self._old(), self.root)
                except OSError:
                    pass
            design = None
            if self.root.is_dir():
                try:
                    design = load_bundle(self.root, "cache")
                except ProjektorError as e:
                    log.warning("cached design unusable, using the default: %s", e)
            if design is None:
                design = load_bundle(self.default_dir, "default")
            self._current = design
            return design

    def status(self) -> dict:
        d = self.load()
        return {"version": d.version, "source": d.source, "fetching": self._fetching,
                "error": self.last_error, "fetched": self.last_fetch, "url": self.default_url}

    # -- fetching
    def fetch(self, url: str | None = None) -> tuple[str, bool]:
        """Download + validate + install the bundle. Returns (version, changed). Blocking."""
        url = (url or self.default_url or "").strip()
        if not url:
            raise ProjektorError("no design url (set [results] design_url or pass a url)")
        with self._lock:
            if self._fetching:
                raise ProjektorError("design fetch already running")
            self._fetching = True
        try:
            res = self._fetch(url)
            self.last_error = None
            return res
        except ProjektorError as e:
            self.last_error = str(e)
            raise
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            raise ProjektorError(f"design fetch failed: {e}") from e
        finally:
            self._fetching = False

    def _fetch(self, url: str) -> tuple[str, bool]:
        try:
            man = json.loads(_http_get(url, MAX_MANIFEST))
            version = str(man["version"])
            template = str(man["template"])
            fonts = [(str(f["file"]), str(f["url"])) for f in man.get("fonts", [])]
        except (ValueError, KeyError, TypeError) as e:
            raise ProjektorError(f"bad design manifest: {e}") from e
        if self.root.is_dir():
            try:
                if load_bundle(self.root, "cache").version == version:
                    log.info("design %s already cached", version)
                    self.last_fetch = time.time()
                    return version, False
            except ProjektorError:
                pass
        svg = _http_get(urllib.parse.urljoin(url, template), MAX_ASSET)
        validate_template(svg)
        blobs = []
        for name, rel in fonts:
            data = _http_get(urllib.parse.urljoin(url, rel), MAX_ASSET)
            _check_font(name, data)
            blobs.append((_safe_name(name), data))
        self.root.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".design-", dir=self.root.parent))
        try:
            (tmp / "fonts").mkdir()
            (tmp / "template.svg").write_bytes(svg)
            for name, data in blobs:
                (tmp / "fonts" / name).write_bytes(data)
            manifest = {"version": version, "width": int(man.get("width", 1920)),
                        "height": int(man.get("height", 1080)), "template_file": "template.svg",
                        "fonts": [{"family": f.get("family", ""), "file": n}
                                  for f, (n, _d) in zip(man.get("fonts", []), blobs)]}
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            load_bundle(tmp, "cache")  # final check of what will be installed
            old = self._old()
            shutil.rmtree(old, ignore_errors=True)
            if self.root.exists():
                os.replace(self.root, old)
            os.replace(tmp, self.root)
            shutil.rmtree(old, ignore_errors=True)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        self.last_fetch = time.time()
        self.load(force=True)
        log.info("design %s installed (%d font(s))", version, len(blobs))
        return version, True


# --------------------------------------------------------------------------- librsvg / fonts


_RSVG: Any = False


def _rsvg():
    """The Rsvg module, or None when librsvg's GIR is not installed."""
    global _RSVG
    if _RSVG is False:
        try:
            import gi

            gi.require_version("Rsvg", "2.0")
            from gi.repository import Rsvg

            _RSVG = Rsvg
        except (ImportError, ValueError):
            _RSVG = None
    return _RSVG


def register_fonts(files: list[Path]) -> int:
    """Add font files to fontconfig's current config (in-process, no system install)."""
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
    except OSError:
        log.warning("libfontconfig not found; bundle fonts not registered")
        return 0
    fc.FcConfigGetCurrent.restype = ctypes.c_void_p
    fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fc.FcConfigAppFontAddFile.restype = ctypes.c_int
    cfg = fc.FcConfigGetCurrent()
    n = 0
    for f in files:
        if fc.FcConfigAppFontAddFile(cfg, os.fsencode(str(f))):
            n += 1
        else:
            log.warning("fontconfig rejected %s", f)
    return n


def _reset_pango_fontmap() -> None:
    """Make pango (this thread's default font map) notice fonts added to fontconfig."""
    try:
        import gi

        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo

        PangoCairo.FontMap.set_default(PangoCairo.FontMap.new())
    except Exception as e:  # noqa: BLE001
        log.warning("cannot refresh pango font map: %s", e)


# --------------------------------------------------------------------------- layers


@dataclass
class Layer:
    surface: Any  # cairo.ImageSurface (ARGB32, pre-multiplied), cropped to its ink rectangle
    x: int
    y: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.surface.get_width(), self.surface.get_height()


@dataclass
class Bar:
    layer: Layer | None
    x: float  # the 100 % rectangle in output pixels
    y: float
    w: float
    h: float


@dataclass
class Labels:
    layers: list[Layer | None]  # one per paragraph
    key: tuple  # (ja, nein) the layers were rendered for


def _union(rects: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    rects = [r for r in rects if r[2] > 0 and r[3] > 0]
    if not rects:
        return None
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def _tokens(ja: int, nein: int) -> dict[str, str]:
    pa, pn = percentages(ja, nein)
    return {"{pct_ja}": str(pa), "{pct_nein}": str(pn), "{count_ja}": str(ja),
            "{count_nein}": str(nein), "{total}": str(ja + nein)}


def substitute(svg: bytes, ja: int, nein: int) -> bytes:
    text = svg.decode("utf-8")
    for k, v in _tokens(ja, nein).items():
        text = text.replace(k, v)
    return text.encode("utf-8")


class DesignRenderer:
    """Renders the layers of one design at the output size (worker thread only)."""

    def __init__(self, design: Design, width: int, height: int, use_layers: bool = True):
        import cairo

        self.cairo = cairo
        self.rsvg = _rsvg()
        if self.rsvg is None:
            raise ProjektorError("librsvg (gir1.2-rsvg-2.0) is not installed")
        self.design = design
        self.w, self.h = width, height
        # render_layer needs librsvg >= 2.52; older versions use per-layer SVG copies
        self.use_layers = use_layers and hasattr(self.rsvg.Handle, "render_layer")
        self.static: Layer | None = None
        self.bar_ja: Bar | None = None
        self.bar_nein: Bar | None = None
        self.baseline_fonts = register_fonts(design.fonts)
        if design.fonts:
            _reset_pango_fontmap()
        self._render_fixed()

    # -- helpers
    def _viewport(self):
        vp = self.rsvg.Rectangle()
        vp.x, vp.y, vp.width, vp.height = 0.0, 0.0, float(self.w), float(self.h)
        return vp

    def _handle(self, svg: bytes):
        return self.rsvg.Handle.new_from_data(svg)

    def _fallback_svg(self, svg: bytes, keep: str) -> bytes:
        """Copy of the template showing only element `keep` (and its ancestors) - old librsvg."""
        root = ET.fromstring(svg)
        ET.register_namespace("", "http://www.w3.org/2000/svg")

        def has(el: ET.Element) -> bool:
            return el.get("id") == keep or any(has(c) for c in el)

        def prune(el: ET.Element) -> None:
            for c in list(el):
                tag = c.tag.rsplit("}", 1)[-1]
                if tag in ("defs", "style"):
                    continue
                if c.get("id") == keep or has(c):
                    if c.get("id") != keep:
                        prune(c)
                else:
                    el.remove(c)

        prune(root)
        return ET.tostring(root)

    def _geometry(self, handle, elem_id: str, svg: bytes):
        """(ink rect, logical rect) of a layer in output pixels."""
        if self.use_layers:
            # asked at the design size, then scaled: librsvg 2.60 reports user units whatever the
            # viewport is, other versions report viewport pixels - both agree at 1:1
            vp = self.rsvg.Rectangle()
            vp.x, vp.y, vp.width, vp.height = 0.0, 0.0, float(self.design.width), float(self.design.height)
            ok, ink, logical = handle.get_geometry_for_layer("#" + elem_id, vp)
            if ok:
                sx, sy = self.w / self.design.width, self.h / self.design.height
                sc = lambda r: (r.x * sx, r.y * sy, r.width * sx, r.height * sy)  # noqa: E731
                return sc(ink), sc(logical)
        # fallback: geometry from the XML (rect x/y/width/height, no transforms)
        root = ET.fromstring(svg)
        sx, sy = self.w / self.design.width, self.h / self.design.height
        for el in root.iter():
            if el.get("id") == elem_id:
                try:
                    r = (float(el.get("x", 0)) * sx, float(el.get("y", 0)) * sy,
                         float(el.get("width", 0)) * sx, float(el.get("height", 0)) * sy)
                    if r[2] > 0 and r[3] > 0:
                        return r, r
                except ValueError:
                    break
        return (0.0, 0.0, float(self.w), float(self.h)), (0.0, 0.0, float(self.w), float(self.h))

    def _render(self, svg: bytes, handle, elem_id: str, ink) -> Layer | None:
        x0 = max(0, int(ink[0] // 1))
        y0 = max(0, int(ink[1] // 1))
        x1 = min(self.w, int(-(-(ink[0] + ink[2]) // 1)))
        y1 = min(self.h, int(-(-(ink[1] + ink[3]) // 1)))
        if x1 <= x0 or y1 <= y0:
            return None
        surf = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, x1 - x0, y1 - y0)
        cr = self.cairo.Context(surf)
        cr.translate(-x0, -y0)
        if self.use_layers:
            handle.render_layer(cr, "#" + elem_id, self._viewport())
        else:
            h2 = self._handle(self._fallback_svg(svg, elem_id))
            cr.scale(self.w / self.design.width, self.h / self.design.height)
            h2.render_cairo(cr)
        surf.flush()
        return Layer(surf, x0, y0)

    def _layer(self, svg: bytes, handle, elem_id: str) -> tuple[Layer | None, tuple]:
        ink, logical = self._geometry(handle, elem_id, svg)
        return self._render(svg, handle, elem_id, ink), logical

    # -- public
    def _render_fixed(self) -> None:
        svg = self.design.template
        handle = self._handle(svg)
        self.static, _ = self._layer(svg, handle, "layer-static")
        ids = {el.get("id") for el in ET.fromstring(svg).iter()}
        for name in ("ja", "nein"):
            if "bar-" + name in ids:
                layer, (x, y, w, h) = self._layer(svg, handle, "bar-" + name)
                setattr(self, "bar_" + name, Bar(layer, x, y, w, h))
            else:
                setattr(self, "bar_" + name, None)

    def labels(self, ja: int, nein: int) -> Labels:
        svg = substitute(self.design.template, ja, nein)
        handle = self._handle(svg)
        ids = [el.get("id") or "" for el in ET.fromstring(svg).iter()]
        paras = sorted((i for i in ids if re.fullmatch(r"para-\d+", i)), key=lambda i: int(i[5:]))
        layers = [self._layer(svg, handle, i)[0] for i in (paras or ["layer-labels"])]
        return Labels(layers, (ja, nein))


# --------------------------------------------------------------------------- presenter


class OverlaySink(Protocol):
    """What the player offers the presenter (all methods callable from any thread)."""

    def now_ns(self) -> int:
        """Pipeline running time in ns (the clock the alpha ramp uses)."""

    def push(self, frame: Any, pts_ns: int) -> None:
        """Show this BGRA frame (buffer object, W*H*4 bytes) from running time pts_ns on."""

    def ramp_alpha(self, points: list[tuple[int, float]]) -> None:
        """Overlay alpha follows these (running time ns, alpha) points, linear in between."""

    def set_alpha(self, a: float) -> None: ...

    def release(self) -> None:
        """Overlay is hidden: drop the held frame."""


class Presenter:
    def __init__(self, store: DesignStore, width: int, height: int, fps: int,
                 in_s: float = IN_NOMINAL, out_s: float = OUT_NOMINAL,
                 para_gap_s: float = PARA_GAP_DEFAULT):
        self.store = store
        self.w, self.h, self.fps = width, height, max(1, int(fps))
        self.in_k = in_s / IN_NOMINAL
        self.out_k = out_s / OUT_NOMINAL
        self.para_gap = max(0.0, para_gap_s)
        self.sink: OverlaySink | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.visible = False  # thread-safe flag: overlay shown or animating in/out
        self.values: tuple[int, int] | None = None
        self.error: str | None = None
        # worker-thread state
        self._renderer: DesignRenderer | None = None
        self._design_gen = 0
        self._tw: dict[str, Tween] = {}
        self._frame: Any = None  # persistent cairo surface
        self._labels_new: Labels | None = None
        self._labels_old: Labels | None = None
        self._phase = "hidden"  # hidden | in | shown | out
        self._pushed_key: tuple | None = None
        self._next_frame = 0.0
        self.frames_pushed = 0
        self.last_compose_ms = 0.0

    # -- public API (any thread) ---------------------------------------------------
    def attach(self, sink: OverlaySink | None) -> None:
        """(Re)connect to a (new) display; whatever was shown is dropped."""
        with self._lock:
            self.visible = False
        self._submit(self._do_attach, sink)

    def show(self, ja: int, nein: int) -> None:
        with self._lock:
            self.visible = True
            self.values = (ja, nein)
        self._submit(self._do_show, ja, nein)

    def hide(self, seconds: float | None = None) -> None:
        with self._lock:
            if not self.visible:
                return
            self.visible = False
        self._submit(self._do_hide, seconds)

    def clear(self) -> None:
        with self._lock:
            self.visible = False
        self._submit(self._do_clear)

    def reload_design(self) -> None:
        self._submit(self._do_reload)

    def close(self) -> None:
        t = self._thread
        if t is not None:
            self._jobs.put(None)
            t.join(3.0)
            self._thread = None

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Tests: wait until the queue is drained and no animation runs."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._jobs.empty() and self._phase in ("hidden", "shown") and not self._busy:
                return True
            time.sleep(0.01)
        return False

    _busy = False

    def _submit(self, fn: Callable, *args: Any) -> None:
        self._busy = True
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="results", daemon=True)
            self._thread.start()
        self._jobs.put((fn, args))

    # -- worker loop ---------------------------------------------------------------
    def _animating(self) -> bool:
        return self._phase in ("in", "out")

    def _run(self) -> None:
        period = 1.0 / self.fps
        while True:
            timeout = None
            if self._animating():
                timeout = max(0.0, self._next_frame - time.monotonic())
            try:
                job = self._jobs.get(timeout=timeout)
            except queue.Empty:
                job = False
            if job is None:
                break
            if job:
                fn, args = job
                try:
                    fn(*args)
                except Exception as e:  # noqa: BLE001 - never kill the worker
                    log.exception("results job %s failed", getattr(fn, "__name__", fn))
                    self.error = str(e)
                    self._fail_hidden()
            if self._animating() and time.monotonic() >= self._next_frame:
                try:
                    self._tick()
                except Exception as e:  # noqa: BLE001
                    log.exception("results frame failed")
                    self.error = str(e)
                    self._fail_hidden()
                self._next_frame = max(self._next_frame + period, time.monotonic())
            self._busy = not self._jobs.empty() or self._animating()

    def _fail_hidden(self) -> None:
        self._phase = "hidden"
        with self._lock:
            self.visible = False
        if self.sink is not None:
            self.sink.set_alpha(0.0)
            self.sink.release()

    # -- jobs (worker thread) ------------------------------------------------------
    def _do_attach(self, sink: OverlaySink | None) -> None:
        self.sink = sink
        self._phase = "hidden"
        self._tw = {}
        self._frame = None

    def _do_reload(self) -> None:
        self._renderer = None  # re-rendered on the next show
        self._design_gen += 1

    def _ensure_renderer(self) -> DesignRenderer:
        if self._renderer is None:
            t = time.perf_counter()
            self._renderer = DesignRenderer(self.store.load(), self.w, self.h)
            log.info("results design %s rendered in %.0f ms", self._renderer.design.version,
                     (time.perf_counter() - t) * 1000)
        return self._renderer

    def _cur(self, t: float) -> dict[str, float]:
        return {k: tw.value(t) for k, tw in self._tw.items()}

    def _now_s(self) -> float:
        return self.sink.now_ns() / 1e9

    def _do_show(self, ja: int, nein: int) -> None:
        sink = self.sink
        if sink is None:
            return
        import cairo

        r = self._ensure_renderer()
        labels = r.labels(ja, nein)
        t = self._now_s() + 0.04  # let the first frame reach the mixer before the ramp starts
        k = self.in_k
        pa, pn = (p / 100.0 for p in percentages(ja, nein))
        if self._phase == "hidden":
            self._tw = {
                "alpha": Tween(0.0, 1.0, t, T_ALPHA_IN * k),
                "ja": Tween(0.0, pa, t + T_BAR_START * k, T_BAR_LEN * k, ease_out),
                "nein": Tween(0.0, pn, t + (T_BAR_START + T_NEIN_DELAY) * k, T_BAR_LEN * k, ease_out),
            }
            for i in range(len(labels.layers)):
                self._tw[f"new{i}"] = Tween(0.0, 1.0, t + T_ALPHA_IN * k + i * self.para_gap, T_LABELS_IN * k)
                self._tw[f"old{i}"] = Tween(0.0, 0.0)
            self._labels_old, self._labels_new = None, labels
            self._frame = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.w, self.h)
            self._pushed_key = None
            self._draw_static()
            self._send(self._compose(t), sink.now_ns())  # alpha is still 0: nothing visible yet
            sink.ramp_alpha(self._alpha_points())
        else:
            cur = self._cur(t)
            same = self._labels_new is not None and self._labels_new.key == (ja, nein)
            if self._frame is None:
                self._frame = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.w, self.h)
                self._draw_static()
            self._tw["alpha"] = Tween(cur["alpha"], 1.0, t, T_ALPHA_IN * k * (1.0 - cur["alpha"]))
            self._tw["ja"] = Tween(cur["ja"], pa, t, T_LABELS_XFADE * k, ease_out)
            self._tw["nein"] = Tween(cur["nein"], pn, t, T_LABELS_XFADE * k, ease_out)
            if not same or self._phase == "out":
                old = self._labels_new
                self._labels_old = old
                self._labels_new = labels
                j = 0  # paragraphs not revealed yet start one after the other from now
                for i in range(len(labels.layers)):
                    tn = self._tw.get(f"new{i}")
                    vis = tn.value(t) if tn is not None else 0.0
                    if vis > 0.001:  # visible: crossfade old text -> new text
                        self._tw[f"old{i}"] = Tween(vis, 0.0, t, T_LABELS_XFADE * k)
                        self._tw[f"new{i}"] = Tween(0.0, 1.0, t, T_LABELS_XFADE * k)
                    else:
                        self._tw[f"old{i}"] = Tween(0.0, 0.0)
                        if tn is None or tn.t0 <= t:  # never scheduled / already finished hiding
                            self._tw[f"new{i}"] = Tween(0.0, 1.0, t + j * self.para_gap, T_LABELS_IN * k)
                            j += 1
                for n in [n for n in self._tw if n[:3] in ("new", "old") and int(n[3:]) >= len(labels.layers)]:
                    del self._tw[n]  # fewer paragraphs than before
            sink.ramp_alpha(self._alpha_points())
        self._phase = "in"
        self._next_frame = time.monotonic()

    def _do_hide(self, seconds: float | None) -> None:
        sink = self.sink
        if sink is None or self._phase == "hidden":
            return
        k = self.out_k if seconds is None else max(0.0, seconds) / OUT_NOMINAL
        t = self._now_s() + 0.02
        cur = self._cur(t)
        if k <= 0:
            self._do_clear()
            return
        shown = max([cur["ja"], cur["nein"]] + [v for n, v in cur.items() if n[:3] in ("new", "old")]) > 0.001
        a_start = T_ALPHA_OUT_START * k if shown else 0.0
        self._tw = {
            "alpha": Tween(cur["alpha"], 0.0, t + a_start, T_ALPHA_OUT * k),
            "ja": Tween(cur["ja"], 0.0, t + T_BARS_OUT_START * k, T_BARS_OUT * k, ease_in),
            "nein": Tween(cur["nein"], 0.0, t + T_BARS_OUT_START * k, T_BARS_OUT * k, ease_in),
        }
        for n, v in cur.items():
            if n[:3] in ("new", "old"):
                self._tw[n] = Tween(v, 0.0, t, T_LABELS_OUT * k)
        sink.ramp_alpha(self._alpha_points())
        self._phase = "out"
        self._next_frame = time.monotonic()

    def _do_clear(self) -> None:
        if self._phase == "hidden" and self._frame is None:
            return
        self._finish_hidden()

    def _finish_hidden(self) -> None:
        self._phase = "hidden"
        self._tw = {}
        self._frame = None
        self._labels_old = self._labels_new = None
        if self.sink is not None:
            self.sink.set_alpha(0.0)
            self.sink.release()

    def _alpha_points(self) -> list[tuple[int, float]]:
        a = self._tw["alpha"]
        t = self._now_s()
        pts = [(int(t * 1e9), a.value(t))]
        if a.t0 > t:
            pts.append((int(a.t0 * 1e9), a.v0))
        pts.append((int(a.end * 1e9), a.v1))
        return pts

    # -- frames --------------------------------------------------------------------
    def _draw_static(self) -> None:
        import cairo

        cr = cairo.Context(self._frame)
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        st = self._renderer.static
        if st is not None:
            cr.set_source_surface(st.surface, st.x, st.y)
            cr.paint()

    def _dirty(self) -> tuple[int, int, int, int] | None:
        r = self._renderer
        rects = [b.layer.rect for b in (r.bar_ja, r.bar_nein) if b is not None and b.layer is not None]
        for lab in (self._labels_new, self._labels_old):
            if lab is not None:
                rects += [ly.rect for ly in lab.layers if ly is not None]
        return _union(rects)

    def _compose(self, t: float) -> Any:
        """Draw the frame for time t (seconds) into the persistent surface; return its data."""
        import cairo

        t0 = time.perf_counter()
        v = self._cur(t)
        r = self._renderer
        dirty = self._dirty()
        F = self._frame
        if dirty is not None:
            cr = cairo.Context(F)
            cr.rectangle(*dirty)
            cr.clip()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            st = r.static
            if st is not None:
                cr.set_source_surface(st.surface, st.x, st.y)
                cr.paint()
            for bar, p in ((r.bar_ja, v["ja"]), (r.bar_nein, v["nein"])):
                if bar is None or bar.layer is None or p <= 0.0005:
                    continue
                bx, by, bw, bh = bar.layer.rect
                top = by if p >= 0.9995 else bar.y + bar.h * (1.0 - p)
                cr.save()
                cr.rectangle(bx, top, bw, by + bh - top)
                cr.clip()
                cr.set_source_surface(bar.layer.surface, bx, by)
                cr.paint()
                cr.restore()
            for lab, pre in ((self._labels_old, "old"), (self._labels_new, "new")):
                if lab is None:
                    continue
                for i, ly in enumerate(lab.layers):
                    a = v.get(f"{pre}{i}", 0.0)
                    if ly is not None and a > 0.001:
                        cr.set_source_surface(ly.surface, ly.x, ly.y)
                        cr.paint_with_alpha(min(1.0, a))
        F.flush()
        self.last_compose_ms = (time.perf_counter() - t0) * 1000
        return F.get_data()

    def _frame_key(self, t: float) -> tuple:
        v = self._cur(t)
        return tuple(round(v[k], 4) for k in sorted(v) if k != "alpha")

    def _send(self, data: Any, pts_ns: int) -> None:
        self.sink.push(data, pts_ns)
        self.frames_pushed += 1

    def _tick(self) -> None:
        sink = self.sink
        now = self._now_s()
        end = max(tw.end for tw in self._tw.values())
        done = now >= end
        t = end if done else now
        key = self._frame_key(t)
        if key != self._pushed_key:
            self._pushed_key = key
            self._send(self._compose(t), sink.now_ns())
        if done:
            if self._phase == "out":
                self._finish_hidden()
            else:
                self._phase = "shown"
                sink.set_alpha(1.0)
