"""
Full-resolution frames for pixel peeping — the tiles behind the 100% viewer.

A frame is read once, stretched to 8 bits at full resolution and kept in a small
in-memory LRU (PEEP_CACHE_MB), so panning around it only slices and PNG-encodes
tiles. The stretch is like PixInsight's screen transfer function: black point just
below the sky (median − 2.8·MAD), white point at the frame's brightest pixel and a
midtone transfer that lifts the sky median to 25% grey. Faint background shows
while star cores stay unclipped, so their profiles can be judged pixel by pixel.

Raw one-shot-colour frames stay a mosaic — 100% means photosites, not a debayered
guess — with the four CFA channels scaled to one background level so the pattern
doesn't swamp the view.
"""
from __future__ import annotations

import io
import threading
import warnings
from collections import OrderedDict
from typing import Optional

import numpy as np

_TARGET_MEDIAN = 0.25        # sky median lands at 25% grey
_SHADOWS_MAD = 2.8           # black point this many MADs below the median
_CHUNK_ROWS = 512            # even, so every chunk starts on the same CFA row


def _sample(a: np.ndarray, target: int = 1 << 21) -> np.ndarray:
    """About `target` evenly spaced finite pixels of a 2-D array."""
    k = max(1, int(np.sqrt(a.size / target)))
    s = a[::k, ::k]
    return s[np.isfinite(s)]


def cfa_gains(mosaic: np.ndarray) -> Optional[np.ndarray]:
    """2×2 multipliers that bring the four photosite channels of a raw mosaic to one
    background level; None when a channel has no usable level."""
    meds = np.array([[np.median(_sample(mosaic[dy::2, dx::2])) for dx in (0, 1)] for dy in (0, 1)],
                    dtype=float)
    if not np.all(np.isfinite(meds)) or np.any(meds <= 0):
        return None
    return np.median(meds) / meds


def stf_params(channel: np.ndarray, gains: Optional[np.ndarray] = None):
    """(black, white, midtone) of the screen stretch for a 2-D channel (after `gains`),
    or None when it is empty or flat."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-NaN input
        if gains is None:
            s = _sample(channel)
            white = float(np.nanmax(channel)) if s.size else np.nan
        else:
            s = np.concatenate([_sample(channel[dy::2, dx::2]) * gains[dy, dx] for dy in (0, 1) for dx in (0, 1)])
            white = max(float(np.nanmax(channel[dy::2, dx::2])) * gains[dy, dx] for dy in (0, 1) for dx in (0, 1))
    if s.size == 0 or not np.isfinite(white):
        return None
    med = float(np.median(s))
    mad = 1.4826 * float(np.median(np.abs(s - med)))
    black = max(float(s.min()), med - _SHADOWS_MAD * mad)
    if white <= black:
        return None
    xm = (med - black) / (white - black)
    t = _TARGET_MEDIAN
    # Midtones balance m with MTF(m, xm) = t, where MTF(m, x) = (m − 1)x / ((2m − 1)x − m)
    midtone = xm * (t - 1) / (2 * t * xm - t - xm) if 0 < xm < 1 else 0.5
    return black, white, float(np.clip(midtone, 1e-6, 1 - 1e-6))


def stretch(channel: np.ndarray, gains: Optional[np.ndarray] = None) -> np.ndarray:
    """uint8 screen stretch of a 2-D channel, computed in row chunks to bound memory."""
    h, w = channel.shape
    out = np.zeros((h, w), dtype=np.uint8)
    params = stf_params(channel, gains)
    if params is None:
        return out
    black, white, m = params
    scale = 1.0 / (white - black)
    for r0 in range(0, h, _CHUNK_ROWS):
        x = channel[r0:r0 + _CHUNK_ROWS].astype(np.float32)
        if gains is not None:
            for dy in (0, 1):
                for dx in (0, 1):
                    x[dy::2, dx::2] *= gains[dy, dx]
        x -= black
        x *= scale
        np.clip(x, 0.0, 1.0, out=x)
        y = ((m - 1) * x) / ((2 * m - 1) * x - m)
        out[r0:r0 + _CHUNK_ROWS] = np.nan_to_num(y * 255.0 + 0.5, nan=0.0).astype(np.uint8)
    return out


def render(data: np.ndarray, cfa: Optional[str] = None) -> np.ndarray:
    """The whole image as 8 bits: (H, W) for mono frames and raw mosaics (`cfa` set),
    (H, W, 3) for colour images, each channel stretched on its own."""
    a = np.asarray(data)
    if a.ndim == 3:
        if a.shape[0] == 3:
            return np.stack([stretch(a[c]) for c in range(3)], axis=2)
        if a.shape[2] == 3:
            return np.stack([stretch(a[:, :, c]) for c in range(3)], axis=2)
        a = a[0]
    if a.ndim != 2:
        raise ValueError(f"unsupported image shape {np.shape(data)}")
    return stretch(a, cfa_gains(a) if cfa else None)


def png(crop: np.ndarray) -> bytes:
    """Lossless PNG of an 8-bit crop (grey or RGB); fast compression, tiles are transient."""
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(crop)).save(buf, "PNG", compress_level=1)
    return buf.getvalue()


class FrameCache:
    """Stretched frames by key, least recently used dropped once over `limit_bytes` (the
    newest always stays). A frame is built once even when its tiles arrive in parallel."""

    def __init__(self):
        self._frames = OrderedDict()
        self._lock = threading.Lock()
        self._building = {}

    def _hit(self, key):
        frame = self._frames.get(key)
        if frame is not None:
            self._frames.move_to_end(key)
        return frame

    def get(self, key, build, limit_bytes: int) -> np.ndarray:
        with self._lock:
            frame = self._hit(key)
            if frame is not None:
                return frame
            gate = self._building.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                frame = self._hit(key)
                if frame is not None:
                    return frame
            try:
                frame = build()
            except BaseException:
                with self._lock:
                    self._building.pop(key, None)
                raise
            with self._lock:
                self._frames[key] = frame
                total = sum(f.nbytes for f in self._frames.values())
                while total > limit_bytes and len(self._frames) > 1:
                    total -= self._frames.popitem(last=False)[1].nbytes
                self._building.pop(key, None)
            return frame


_cache = FrameCache()


def frame(key, path: str, bayer=None) -> np.ndarray:
    """The image at `path` stretched at full resolution, cached under `key` (include the
    file's mtime so an edited file is re-read)."""
    import config
    import scanner

    def build():
        _, data = scanner.read_image(path)
        if data is None:
            raise ValueError("no image data")
        return render(data, scanner.cfa_pattern(bayer) if np.ndim(data) == 2 else None)

    return _cache.get(key, build, config.PEEP_CACHE_MB * 1024 * 1024)
