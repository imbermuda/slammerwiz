"""
Tooltip vision pipeline.

  [mss capture]  ->  [xxhash dirty-check]  ->  [preprocess]  ->  [OCR]
        ~3ms              ~0.04ms                 ~2ms            see README

OCR backends, tried in order:
  1. RapidOCR (ONNX) — pure pip install, no system deps, ships weights.
  2. pytesseract CLI — if user has Tesseract on PATH or in config.
  3. none — vision disabled, clipboard still works.

Emits ``VisionFrame(text, changed, ms_total, ms_ocr, hash, ok)`` to a callback.
Safe by design: reads pixels from the OS, never the game.
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

import numpy as np

try:
    import xxhash
    def _fast_hash(buf: bytes) -> int:
        return xxhash.xxh3_64_intdigest(buf)
except Exception:
    import hashlib
    def _fast_hash(buf: bytes) -> int:
        return int.from_bytes(hashlib.blake2b(buf, digest_size=8).digest(), "big")


@dataclass
class VisionFrame:
    text: str
    changed: bool
    ms_total: float
    ms_ocr: float
    roi_hash: int
    ok: bool
    backend: str = ""
    error: Optional[str] = None


# ---------- Tesseract discovery (fallback) -----------------------------

def _find_tesseract() -> Optional[str]:
    env = os.environ.get("TESSERACT_PATH") or os.environ.get("TESSERACT_CMD")
    if env and os.path.isfile(env):
        return env
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    if platform.system() == "Windows":
        for p in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        ):
            if os.path.isfile(p):
                return p
    return None


# ---------- preprocessing ----------------------------------------------

def _preprocess_binarized(rgba: np.ndarray) -> bytes:
    """Return a PNG byte stream (grayscale, inverted, binarized, 2x upscale).

    Used for Tesseract fallback.
    """
    b = rgba[..., 0].astype(np.int32)
    g = rgba[..., 1].astype(np.int32)
    r = rgba[..., 2].astype(np.int32)
    gray = ((r * 299 + g * 587 + b * 114) // 1000).astype(np.uint8)
    inv = 255 - gray
    bw = np.where(inv > 140, 255, 0).astype(np.uint8)
    bw = np.repeat(np.repeat(bw, 2, axis=0), 2, axis=1)
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(bw, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def _bgra_to_rgb(rgba: np.ndarray) -> np.ndarray:
    # mss returns BGRA. Drop alpha, swap to RGB.
    return rgba[..., [2, 1, 0]].copy()


def _crop_to_dark_band(rgba: np.ndarray, pad: int = 8) -> np.ndarray:
    """Return a sub-array of ``rgba`` containing just the dark tooltip region.

    PoE tooltip is a dark near-opaque band (luminance ≤ ~80) surrounded by
    scene or UI at varied brightness. We project darkness along rows and
    columns and keep the contiguous "dark" stretch. Typically halves the
    OCR input size, cutting inference time roughly in half.

    Falls back to the original array if no clear dark band is found.
    """
    if rgba is None or rgba.size == 0:
        return rgba
    # Grayscale from BGRA — cheap weighted sum. uint32 so the sum can't
    # overflow (255 * 1000 = 255000 is safe in uint32, would overflow uint16).
    gray = (
        rgba[..., 0].astype(np.uint32) * 114
        + rgba[..., 1].astype(np.uint32) * 587
        + rgba[..., 2].astype(np.uint32) * 299
    ) // 1000
    dark = (gray < 90)
    # Rows/cols with ≥ 20% dark pixels are "tooltip rows".
    row_score = dark.mean(axis=1)
    col_score = dark.mean(axis=0)
    row_mask = row_score > 0.20
    col_mask = col_score > 0.20
    if not row_mask.any() or not col_mask.any():
        return rgba
    ys = np.where(row_mask)[0]
    xs = np.where(col_mask)[0]
    y0, y1 = max(0, int(ys[0]) - pad), min(rgba.shape[0], int(ys[-1]) + pad + 1)
    x0, x1 = max(0, int(xs[0]) - pad), min(rgba.shape[1], int(xs[-1]) + pad + 1)
    # Sanity check: at least 40% of the original in each dimension,
    # otherwise keep the full array (avoid clipping a partially-dark tooltip).
    if (y1 - y0) < rgba.shape[0] * 0.25 or (x1 - x0) < rgba.shape[1] * 0.25:
        return rgba
    return rgba[y0:y1, x0:x1]


# ---------- auto tooltip detection -------------------------------------


def auto_detect_tooltip(
    cursor_pos: Tuple[int, int],
    search_radius: int = 1200,
    min_w: int = 400,
    min_h: int = 220,
    debug_dir: Optional[object] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """Find the PoE 2 tooltip near the cursor by its **colored border**.

    PoE 2 tooltips have a distinctive rarity-colored frame:

      * Magic  — blue  (HSV H≈105–130, high saturation, medium value)
      * Rare   — gold  (HSV H≈15–30,  high saturation, medium value)
      * Unique — brown (HSV H≈5–15,   medium saturation, medium value)
      * Normal — white (low saturation, high value)

    We build a mask from any of those ranges, close it so the 2–4 px frame
    becomes a filled rectangle, then pick the best rectangular contour near
    the cursor. This is much more discriminating than "largest dark blob"
    which gets confused when the tooltip overlaps the inventory panel.

    If ``debug_dir`` is given, the crop + mask + annotated candidates are
    saved there regardless of success/failure — invaluable when a specific
    in-game tooltip defeats the heuristic.
    """
    import cv2
    import mss

    with mss.mss() as sct:
        mon = sct.monitors[0]
        shot = sct.grab(mon)
        screen_bgra = np.asarray(shot, dtype=np.uint8)

    base_x, base_y = mon["left"], mon["top"]
    cx_local = cursor_pos[0] - base_x
    cy_local = cursor_pos[1] - base_y
    H, W = screen_bgra.shape[:2]

    x0 = max(0, cx_local - search_radius)
    y0 = max(0, cy_local - search_radius)
    x1 = min(W, cx_local + search_radius)
    y1 = min(H, cy_local + search_radius)
    region = screen_bgra[y0:y1, x0:x1]

    bgr = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

    # Rarity-colored border ranges in HSV
    mask_gold   = cv2.inRange(hsv, (12, 80, 70),  (32, 255, 230))   # rare
    mask_blue   = cv2.inRange(hsv, (95, 80, 70),  (130, 255, 230))  # magic
    mask_orange = cv2.inRange(hsv, (0, 80, 70),   (12, 255, 230))   # unique
    mask_white  = cv2.inRange(hsv, (0, 0, 170),   (180, 40, 255))   # normal
    mask = mask_gold | mask_blue | mask_orange | mask_white

    # Thicken the 2-4 px border into something contour-findable without
    # flood-filling unrelated specks into one giant blob.
    k_small = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(mask, k_small, iterations=2)
    # Close small gaps along the border so a continuous rectangle forms.
    k_mid = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    filled = cv2.morphologyEx(dilated, cv2.MORPH_CLOSE, k_mid, iterations=2)

    # RETR_LIST so we also see the inner edges of hollow border rings — we
    # take their bounding box, which is the tooltip rectangle.
    contours, _ = cv2.findContours(filled, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    cur_local = (cx_local - x0, cy_local - y0)
    best: Optional[Tuple[int, int, int, int]] = None
    best_score = 0.0
    rejects: List[dict] = []

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        reason = None
        if w < min_w or h < min_h:
            reason = f"too small ({w}x{h})"
        else:
            ar = w / max(h, 1)
            # PoE 2 tooltips are wider than tall (usually 1.2-2.5x).
            # Reject near-square or tall panels — those are inventory tabs,
            # stash panels, etc.
            if ar < 0.9 or ar > 3.5:
                reason = f"aspect {ar:.2f} (not tooltip-shaped)"
            else:
                area = cv2.contourArea(c)
                rectness = area / (w * h) if w * h else 0.0
                if rectness < 0.40:
                    reason = f"rectness {rectness:.2f}"
                else:
                    nearest_dx = 0 if x <= cur_local[0] <= x + w else min(
                        abs(cur_local[0] - x), abs(cur_local[0] - (x + w)))
                    nearest_dy = 0 if y <= cur_local[1] <= y + h else min(
                        abs(cur_local[1] - y), abs(cur_local[1] - (y + h)))
                    dist = (nearest_dx ** 2 + nearest_dy ** 2) ** 0.5
                    if dist > search_radius * 1.2:
                        reason = f"too far ({dist:.0f}px)"
                    else:
                        screen_coverage = (w * h) / (W * H)
                        # Tooltips typically ≤ 20% of screen. Anything bigger
                        # is a UI panel (inventory, stash, etc.).
                        if screen_coverage > 0.28:
                            reason = f"covers {screen_coverage:.0%} of screen (panel)"
                        else:
                            dist_penalty = 1.0 + (dist / 400.0)
                            score = (w * h) / dist_penalty
                            if score > best_score:
                                best_score = score
                                best = (x + x0 + base_x, y + y0 + base_y, w, h)
                                reason = f"CANDIDATE score={score:.0f}"
                            else:
                                reason = f"dominated score={score:.0f}"
        rejects.append({"bbox": (x, y, w, h), "reason": reason})

    if debug_dir is not None:
        try:
            _save_debug(cv2, debug_dir, region, filled, cur_local, rejects, best,
                        (x0, y0), (base_x, base_y))
        except Exception:
            pass

    return best


def _save_debug(cv2, out_dir, region, mask, cur_local, rejects, best, region_origin, desktop_origin):
    from pathlib import Path
    import json
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ann = region.copy()
    for r in rejects:
        x, y, w, h = r["bbox"]
        cv2.rectangle(ann, (x, y), (x + w, y + h), (0, 0, 255, 255), 1)
    if best is not None:
        bx, by, bw, bh = best
        lx = bx - desktop_origin[0] - region_origin[0]
        ly = by - desktop_origin[1] - region_origin[1]
        cv2.rectangle(ann, (lx, ly), (lx + bw, ly + bh), (0, 255, 0, 255), 3)
    cv2.circle(ann, cur_local, 8, (255, 255, 0, 255), 2)
    cv2.imwrite(str(out / "tooltip_crop.png"), ann)
    cv2.imwrite(str(out / "tooltip_mask.png"), mask)
    with (out / "tooltip_candidates.json").open("w", encoding="utf-8") as f:
        json.dump({
            "cursor_local": list(cur_local),
            "best": list(best) if best else None,
            "candidates": [
                {"bbox": list(r["bbox"]), "reason": r["reason"]}
                for r in rejects
            ],
        }, f, indent=2)


# ---------- OCR backends -----------------------------------------------


class _OcrBackend:
    name: str = "none"

    def ocr(self, rgba: np.ndarray) -> str:  # pragma: no cover
        raise NotImplementedError


class _RapidBackend(_OcrBackend):
    name = "rapidocr"

    # Hard cap for OCR input dimension. 800 px keeps a full tooltip legible
    # (text renders at ~14 px high after INTER_AREA downsampling) while
    # cutting inference time roughly in half vs 1100 px.
    _MAX_DIM = 800

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "onnxruntime" in msg.lower() and "dll load failed" in msg.lower():
                raise RuntimeError(
                    "onnxruntime DLL failed to load. Install Microsoft VC++ "
                    "Redistributable x64 from https://aka.ms/vs/17/release/vc_redist.x64.exe "
                    "then restart, or pin onnxruntime==1.19.2."
                ) from e
            raise
        # use_cls=False: tooltip is always upright; saves ~20-40ms per call.
        self._engine = RapidOCR(use_cls=False)

    def _downscale(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        m = max(w, h)
        if m <= self._MAX_DIM:
            return rgb
        scale = self._MAX_DIM / m
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        # cv2.INTER_AREA is the correct downsampling filter for text —
        # nearest-neighbor produced jaggies that made OCR output vary
        # between otherwise-identical captures.
        try:
            import cv2
            return cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
        except Exception:
            # Fallback to numpy indexing if cv2 isn't available.
            ys = (np.arange(new_h) * (h / new_h)).astype(np.int32)
            xs = (np.arange(new_w) * (w / new_w)).astype(np.int32)
            return rgb[ys][:, xs]

    def ocr(self, rgba: np.ndarray) -> str:
        rgb = _bgra_to_rgb(rgba)
        rgb = self._downscale(rgb)
        result, _ = self._engine(rgb)
        if not result:
            return ""
        lines: List[str] = []
        for entry in result:
            # Entry shape: [bbox, text, score]
            if len(entry) >= 2:
                lines.append(str(entry[1]))
        return "\n".join(lines)


class _TesseractBackend(_OcrBackend):
    name = "tesseract"

    def __init__(self, tesseract_path: Optional[str], tessdata_dir: Optional[str],
                 psm: int, lang: str):
        import pytesseract  # type: ignore
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
        self._pyt = pytesseract
        self._tessdata_dir = tessdata_dir
        self._psm = psm
        self._lang = lang

    def ocr(self, rgba: np.ndarray) -> str:
        from PIL import Image
        png = _preprocess_binarized(rgba)
        img = Image.open(io.BytesIO(png))
        cfg = f"--psm {self._psm} --oem 1 -l {self._lang} -c tessedit_do_invert=0"
        if self._tessdata_dir:
            cfg = f'--tessdata-dir "{self._tessdata_dir}" ' + cfg
        return self._pyt.image_to_string(img, config=cfg)


def _build_backend(cfg: dict) -> Tuple[Optional[_OcrBackend], Optional[str]]:
    # Try RapidOCR first — no system deps, ships models inline.
    forced = (cfg.get("backend") or "").strip().lower()
    order: List[str] = []
    if forced:
        order = [forced]
    else:
        order = ["rapidocr", "tesseract"]
    errors: List[str] = []
    for name in order:
        try:
            if name == "rapidocr":
                return _RapidBackend(), None
            if name == "tesseract":
                tess = cfg.get("tesseract_path") or _find_tesseract()
                if not tess:
                    errors.append("tesseract: binary not found")
                    continue
                return _TesseractBackend(
                    tesseract_path=tess,
                    tessdata_dir=cfg.get("tessdata_dir") or None,
                    psm=int(cfg.get("psm", 6)),
                    lang=cfg.get("lang", "eng"),
                ), None
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
    return None, "; ".join(errors) or "no OCR backend available"


# ---------- worker -----------------------------------------------------


class VisionWorker:
    def __init__(
        self,
        roi: Tuple[int, int, int, int],
        on_frame: Callable[[VisionFrame], None],
        poll_ms: int = 8,
        backend_cfg: Optional[dict] = None,
    ):
        self.roi = roi
        self.on_frame = on_frame
        self.poll = poll_ms / 1000.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_hash: int = 0
        self._last_text: str = ""
        self._cfg = backend_cfg or {}
        self._backend: Optional[_OcrBackend] = None
        self._backend_error: Optional[str] = None
        self._init_backend()

    def _init_backend(self) -> None:
        self._backend, self._backend_error = _build_backend(self._cfg)

    # ------ public -----------------------------------------------------

    def set_roi(self, roi: Tuple[int, int, int, int]) -> None:
        self.roi = roi
        self._last_hash = 0

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else "none"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vision", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------ core loop --------------------------------------------------

    def _run(self) -> None:
        import mss
        with mss.mss() as sct:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                frame_err: Optional[str] = self._backend_error if self._backend is None else None
                changed = False
                ocr_ms = 0.0
                text = self._last_text
                h = self._last_hash
                ok = True

                try:
                    x, y, w, height = self.roi
                    if w <= 0 or height <= 0:
                        raise RuntimeError("ROI not calibrated")
                    shot = sct.grab({"left": x, "top": y, "width": w, "height": height})
                    arr = np.asarray(shot, dtype=np.uint8)  # BGRA
                    h = _fast_hash(arr.tobytes())
                    if h != self._last_hash:
                        changed = True
                        if self._backend is None:
                            ok = False
                        else:
                            cropped = _crop_to_dark_band(arr)
                            t_ocr = time.perf_counter()
                            text = self._backend.ocr(cropped).strip()
                            ocr_ms = (time.perf_counter() - t_ocr) * 1000.0
                        self._last_hash = h
                        self._last_text = text
                except Exception as e:
                    frame_err = f"{type(e).__name__}: {e}"
                    ok = False

                total_ms = (time.perf_counter() - t0) * 1000.0
                try:
                    self.on_frame(
                        VisionFrame(
                            text=text,
                            changed=changed,
                            ms_total=total_ms,
                            ms_ocr=ocr_ms,
                            roi_hash=h,
                            ok=ok,
                            backend=self.backend_name,
                            error=frame_err,
                        )
                    )
                except Exception:
                    pass

                self._stop.wait(self.poll)
