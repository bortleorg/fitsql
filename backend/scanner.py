from __future__ import annotations

import os
import re
import json
import uuid
import logging
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

logger = logging.getLogger(__name__)

# Image files the catalog indexes: FITS, plus XISF from PixInsight / N.I.N.A.
FITS_EXTENSIONS = {".fit", ".fits", ".fts", ".xisf"}

# Worker threads for parallel file processing. numpy/PIL/SEP release the GIL
# during heavy compute, so threads give real speedup for the CPU-bound work.
MAX_WORKERS = max(1, min(8, (os.cpu_count() or 4)))

# Commit indexed rows in batches instead of one transaction per file.
COMMIT_BATCH = 50


def read_image(path: str):
    """
    (header, data) for a FITS or XISF file: the primary FITS header (for XISF, its
    FITS keywords as an astropy Header) and the first image with 2+ dimensions —
    (H, W) or (C, H, W) — or None.
    """
    if str(path).lower().endswith(".xisf"):
        import xisf
        return xisf.read(path)
    from astropy.io import fits
    with fits.open(path, memmap=False, ignore_missing_simple=True) as hdul:
        hdr = hdul[0].header
        data = next((np.asarray(hdu.data) for hdu in hdul
                     if hdu.data is not None and len(hdu.data.shape) >= 2), None)
    return hdr, data


def generate_thumbnail(fits_path: str, out_dir: str) -> Optional[str]:
    """
    Read first image HDU, auto-stretch using asinh, save as JPEG (max 512px).
    Returns thumbnail filename or None on failure.
    Kept for compatibility — opens the file itself. The scan path uses
    thumbnail_from_array to avoid re-opening FITS already loaded in memory.
    """
    try:
        _, data = read_image(fits_path)
        return thumbnail_from_array(data, out_dir)
    except Exception as e:
        logger.warning(f"Thumbnail generation failed for {fits_path}: {e}")
        return None


def _downsample(arr, target=1024):
    """Stride-decimate the last two spatial axes so the thumbnail isn't built from
    a full 61MP frame (huge memory + time for a 512px output)."""
    if arr is None:
        return arr
    if arr.ndim == 2:
        h, w = arr.shape
        k = max(1, max(h, w) // target)
        return arr[::k, ::k] if k > 1 else arr
    if arr.ndim == 3:
        # find the two largest axes (spatial)
        if arr.shape[0] <= 4:        # (C, H, W)
            h, w = arr.shape[1], arr.shape[2]
            k = max(1, max(h, w) // target)
            return arr[:, ::k, ::k] if k > 1 else arr
        h, w = arr.shape[0], arr.shape[1]  # (H, W, C)
        k = max(1, max(h, w) // target)
        return arr[::k, ::k, :] if k > 1 else arr
    return arr


def thumbnail_from_array(data, out_dir: str, dtype=np.float32, max_size: int = 512,
                         filename: Optional[str] = None) -> Optional[str]:
    """Auto-stretch a FITS data array and save as JPEG (max_size px on the long side)."""
    try:
        from PIL import Image

        if data is None:
            return None
        data = _downsample(data, target=2 * max_size)   # ~2× the output, not full res

        # Handle color FITS (3D arrays)
        if data.ndim == 3:
            if data.shape[0] == 3:
                # (3, H, W) -> (H, W, 3)
                data = np.transpose(data, (1, 2, 0))
                channels = [_stretch_channel(data[:, :, c].astype(dtype)) for c in range(3)]
                img = Image.fromarray(np.stack(channels, axis=2), mode="RGB")
            elif data.shape[2] == 3:
                # (H, W, 3)
                channels = [_stretch_channel(data[:, :, c].astype(dtype)) for c in range(3)]
                img = Image.fromarray(np.stack(channels, axis=2), mode="RGB")
            else:
                # Take first slice
                img = Image.fromarray(_stretch_channel(data[0].astype(dtype)), mode="L")
        elif data.ndim == 2:
            img = Image.fromarray(_stretch_channel(data.astype(dtype)), mode="L")
        else:
            return None

        # Resize to max_size on the longest side
        w, h = img.size
        if w > max_size or h > max_size:
            ratio = min(max_size / w, max_size / h)
            img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)

        filename = filename or f"{uuid.uuid4().hex}.jpg"
        img.convert("RGB").save(os.path.join(out_dir, filename), "JPEG", quality=85)
        return filename

    except Exception as e:
        logger.warning(f"Thumbnail from array failed: {e}")
        return None


def render_preview(fits_path: str, out_dir: str, filename: str, max_size: int) -> Optional[str]:
    """Stretched JPEG of a FITS/XISF file at up to max_size px — the compare view's
    zoomable image (thumbnails stop at 512 px). Returns the filename or None."""
    _, data = read_image(fits_path)
    return thumbnail_from_array(data, out_dir, max_size=max_size, filename=filename)


def _stretch_channel(data: np.ndarray) -> np.ndarray:
    """Apply asinh stretch to a 2D float array, return uint8."""
    finite_data = data[np.isfinite(data)]
    if len(finite_data) == 0:
        return np.zeros(data.shape, dtype=np.uint8)

    p1, p99 = np.percentile(finite_data, [1, 99])
    clipped = np.clip(data, p1, p99)
    normalized = (clipped - p1) / (p99 - p1 + 1e-10)
    stretched = np.arcsinh(10 * normalized) / np.arcsinh(10)
    img_8bit = (stretched * 255).astype(np.uint8)
    return img_8bit


def count_stars(data_2d: np.ndarray) -> Optional[int]:
    """Back-compat shim — returns just the star count."""
    return analyze_stars(data_2d).get("star_count")


# Why a source was (not) used for shape metrics — also drives overlay colours.
STAR_CLEAN, STAR_SATURATED, STAR_EDGE, STAR_BLENDED = 0, 1, 2, 3


def star_codes(src, width: int, height: int, sat_level: float, back_level: float,
               clipped: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Classify SEP sources for shape measurement: STAR_CLEAN, STAR_SATURATED (peak
    at the frame's clip level — flat-topped, inflates FWHM/HFR), STAR_EDGE (centroid
    within STAR_EDGE_PX of the border, or SEP-truncated/singular), STAR_BLENDED
    (deblended from a merged detection). Saturation wins over edge over blend.
    `clipped`, when given, is a per-pixel saturation map that replaces the peak
    test (superpixel-binned CFA frames, where averaging hides clipped photosites).
    """
    import config
    flag = np.asarray(src["flag"]).astype(int)
    x, y = src["x"], src["y"]
    m = config.STAR_EDGE_PX
    codes = np.full(len(src), STAR_CLEAN, dtype=np.int8)
    codes[(flag & 1) != 0] = STAR_BLENDED                          # sep.OBJ_MERGED
    edge = ((x < m) | (y < m) | (x > width - 1 - m) | (y > height - 1 - m)
            | ((flag & (2 | 8)) != 0))                             # OBJ_TRUNC | OBJ_SINGU
    codes[edge] = STAR_EDGE
    if clipped is not None:
        yi = np.clip(np.rint(y).astype(int), 0, clipped.shape[0] - 1)
        xi = np.clip(np.rint(x).astype(int), 0, clipped.shape[1] - 1)
        codes[clipped[yi, xi]] = STAR_SATURATED
    elif np.isfinite(sat_level) and sat_level > 0:
        codes[src["peak"] + back_level >= config.STAR_SAT_FRACTION * sat_level] = STAR_SATURATED
    return codes


_CFA_PATTERN = re.compile(r"^[RGB]{4}$")


def cfa_pattern(bayer) -> Optional[str]:
    """The 2×2 colour-filter pattern ('RGGB', 'GBRG', ...) when a header marks a raw
    one-shot-colour mosaic; None for mono frames or values like COLORTYP='MONO'."""
    value = str(bayer or "").strip().upper()
    return value if _CFA_PATTERN.match(value) else None


def _cfa_blocks(cfa: np.ndarray):
    """The four photosites of every 2×2 block (an odd trailing row/column is dropped)."""
    h, w = cfa.shape[0] // 2 * 2, cfa.shape[1] // 2 * 2
    return cfa[0:h:2, 0:w:2], cfa[0:h:2, 1:w:2], cfa[1:h:2, 0:w:2], cfa[1:h:2, 1:w:2]


def superpixel(cfa: np.ndarray) -> np.ndarray:
    """Half-resolution luminance from a raw CFA mosaic: each pixel is the mean of one
    2×2 block (one R, two G, one B). The mosaic pattern — which the background model
    otherwise reads as enormous noise, so only saturated stars clear 8σ — averages out."""
    a, b, c, d = _cfa_blocks(cfa)
    return ((a + b) + (c + d)) / 4


def superpixel_clipped(cfa: np.ndarray) -> np.ndarray:
    """Superpixels with any photosite at the clip level (STAR_SAT_FRACTION of the frame
    max), grown by one pixel so a star's centroid lands inside its clipped core."""
    import config
    top = float(np.nanmax(cfa))
    a, b, c, d = _cfa_blocks(cfa)
    if not (np.isfinite(top) and top > 0):
        return np.zeros(a.shape, dtype=bool)
    limit = config.STAR_SAT_FRACTION * top
    clip = (a >= limit) | (b >= limit) | (c >= limit) | (d >= limit)
    rows = clip.copy()
    rows[1:] |= clip[:-1]
    rows[:-1] |= clip[1:]
    grown = rows.copy()
    grown[:, 1:] |= rows[:, :-1]
    grown[:, :-1] |= rows[:, 1:]
    return grown


def in_sensor_pixels(metrics: dict, k: int, shape) -> dict:
    """
    Metrics measured on a k×k-binned image, expressed in original sensor pixels
    (sizes ×k, flux ×k², overlay coordinates mapped back onto the full frame).

    A superpixel averages a k×k box, which widens every star by the box's variance
    (k²/12 px²); that is taken out in quadrature so sizes line up with mono frames.
    On synthetic stars seen by both a mono sensor and an RGGB mosaic, corrected HFR
    and Moffat FWHM agree within ~5% for FWHM ≥ 3.5 px (more for undersampled
    stars); moment FWHM of small stars and eccentricity (~+0.1, the G photosites
    dominate each block) stay inflated — compare those within OSC frames.
    """
    box_var = k * k / 12.0
    for key, sigmas in (("median_fwhm", 2.3548), ("median_hfr", 1.1774), ("psf_fwhm", 2.3548)):
        value = metrics.get(key)
        if value is not None:
            sigma2 = (value * k / sigmas) ** 2
            metrics[key] = float(sigmas * np.sqrt(max(sigma2 - box_var, 0.25 * sigma2)))
    if metrics.get("star_flux") is not None:
        metrics["star_flux"] *= k * k                  # a binned pixel holds the mean of k² photosites
    d = metrics.get("diagnostics")
    if d:
        off = (k - 1) / 2.0                            # binned pixel i is centred on sensor pixel k·i + off
        d["height"], d["width"] = int(shape[0]), int(shape[1])
        d["stars"] = [[round(x * k + off, 1), round(y * k + off, 1), round(a * k, 2), round(b * k, 2), t, c, u]
                      for x, y, a, b, t, c, u in d.get("stars", [])]
        d["streaks"] = [[round(v * k + off, 1) for v in line] for line in d.get("streaks", [])]
        dip = d.get("dip")
        if dip and dip.get("box"):
            dip["x"], dip["y"] = int(dip["x"] * k + off), int(dip["y"] * k + off)
            dip["box"] = [int(v * k) for v in dip["box"]]
        d["binned"] = k
    return metrics


def _largest_cluster(mask: np.ndarray) -> list:
    """Flat indices of the largest 4-connected group of True cells in a 2D grid."""
    rows, cols = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    best = []
    for r0 in range(rows):
        for c0 in range(cols):
            if not mask[r0, c0] or seen[r0, c0]:
                continue
            seen[r0, c0] = True
            stack, group = [(r0, c0)], []
            while stack:
                r, c = stack.pop()
                group.append(r * cols + c)
                for rr, cc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                    if 0 <= rr < rows and 0 <= cc < cols and mask[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        stack.append((rr, cc))
            if len(group) > len(best):
                best = group
    return sorted(best)


def grid_metrics(x, y, back, rms, width: int, height: int) -> dict:
    """
    Local screening on a GRID_COLS × GRID_ROWS grid (see config).

    empty_cells: share of the grid covered by the largest connected patch of cells
      holding fewer than GRID_EMPTY_FRACTION of the median cell's stars (tree line,
      dome, dense cloud bank). Only a connected patch counts: low-SNR, vignetted or
      nebula-clumped fields leave isolated empty cells scattered around, an
      occlusion doesn't. None when the field is too sparse to judge.
    bg_spread: p90 − p10 of the per-cell median background, in units of the global
      background RMS (gradients, stray light). None without a background map/RMS.
    `cells` holds per-cell star counts, the empty patch and background offsets (σ)
    for the overlay.
    """
    import config
    cols, rows = config.GRID_COLS, config.GRID_ROWS
    x = np.asarray(x, dtype=float); y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    cx = np.clip((x[ok] * cols / width).astype(int), 0, cols - 1)
    cy = np.clip((y[ok] * rows / height).astype(int), 0, rows - 1)
    counts = np.bincount(cy * cols + cx, minlength=cols * rows)
    median_stars = float(np.median(counts))
    empty_below = config.GRID_EMPTY_FRACTION * median_stars
    empty, cluster = None, []
    if median_stars >= config.GRID_MIN_MEDIAN_STARS:
        cluster = _largest_cluster((counts < empty_below).reshape(rows, cols))
        empty = len(cluster) / float(cols * rows)

    spread, offsets = None, None
    if back is not None:
        # The background map is smooth (mesh-interpolated): decimate before the medians
        k = max(1, max(back.shape) // 512)
        small = back[::k, ::k]
        cell_bg = np.array([[np.median(c) for c in np.array_split(r, cols, axis=1)]
                            for r in np.array_split(small, rows, axis=0)], dtype=float).ravel()
        if rms is not None and np.isfinite(rms) and rms > 0:
            p10, p90 = np.percentile(cell_bg, [10, 90])
            spread = float((p90 - p10) / rms)
            offsets = [round(float(v), 2) for v in (cell_bg - np.median(cell_bg)) / rms]

    return {
        "empty_cells": empty,
        "bg_spread": spread,
        "cells": {"grid": [cols, rows], "cell_stars": counts.tolist(),
                  "empty_below": round(empty_below, 2), "judged": empty is not None,
                  "empty_cluster": cluster,
                  "cell_bg_sigma": offsets},
    }


# Bump when analysis gains metrics that rows analyzed earlier lack: "Re-analyze missing"
# then redoes them (the version leads the stored diagnostics JSON).
DIAGNOSTICS_VERSION = 2


def needs_analysis(FitsFile):
    """SQL condition for the Light rows "Re-analyze missing" redoes: never analyzed, no sky
    geometry, or analyzed by an older DIAGNOSTICS_VERSION."""
    from sqlalchemy import or_
    return or_(FitsFile.median_hfr.is_(None), FitsFile.altitude.is_(None), FitsFile.trail_score.is_(None),
               FitsFile.diagnostics_json.is_(None),
               FitsFile.diagnostics_json.notlike(f'{{"v":{DIAGNOSTICS_VERSION},%'))


def _box_filter(m: np.ndarray, k: int) -> np.ndarray:
    """Mean over a k×k window (k odd), edges padded by repetition."""
    p = k // 2
    c = np.pad(np.cumsum(np.cumsum(np.pad(m, p, mode="edge"), axis=0), axis=1), ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / float(k * k)


def background_dip(data: np.ndarray, rms: float):
    """
    Frost / dew shadow: the deepest soft dark patch in the background.

    Frost or dew on the sensor window, a filter or a corrector dims sky and stars alike
    over thousands of pixels — a smooth shadow when it sits well in front of focus, or
    thousands of tiny dark donuts when droplets / ice crystals sit on the cover glass.
    Star counts, HFR and bg_spread barely move. SEP's background map hides the donut kind
    (its mode estimate clips them), so the background is mapped here: per block of at
    least DIP_BLOCK_MIN px, the mean of every second pixel below the block median + 3·rms
    (stars left out, dark specks kept). A DIP_FIT_DEGREE polynomial surface (vignetting,
    gradients) is fitted to that map with outlier blocks — nebulae, the dip itself —
    iteratively left out, and the residual is smoothed over DIP_SMOOTH of the frame.

    Returns (bg_dip, dip). bg_dip is the deepest smoothed residual at least DIP_EDGE from
    the edges, as a fraction of the fitted background (0.05 = 5% darker). A dip whose
    half-depth region runs off the frame edge is a gradient the surface didn't follow
    (clouds or twilight in a corner), not a shadow on the optics, and is skipped. None
    when the frame is too small or its background too close to zero to judge (background-
    subtracted data). dip = {depth, x, y, box} for the overlay in frame pixels, box =
    [x0, y0, x1, y1] around the connected region at least half as deep (x, y and box are
    null when there is no dip).
    """
    import config
    h, w = data.shape
    k = max(config.DIP_BLOCK_MIN, max(h, w) // config.DIP_MAP_PX, 2)
    kb = k // 2
    hb, wb = h // k, w // k
    if hb < 8 or wb < 8 or not np.isfinite(rms) or rms <= 0:
        return None, None
    sub = data[::2, ::2][:hb * kb, :wb * kb]
    blocks = sub.reshape(hb, kb, wb, kb).transpose(0, 2, 1, 3).reshape(hb, wb, kb * kb)
    median = np.median(blocks, axis=2)
    keep = blocks < median[..., None] + 3.0 * rms
    level = (np.where(keep, blocks, 0).sum(axis=2, dtype=np.float64)
             / np.maximum(keep.sum(axis=2), 1))

    yy, xx = np.mgrid[0:hb, 0:wb]
    x = ((xx + 0.5) / wb * 2.0 - 1.0).ravel()
    y = ((yy + 0.5) / hb * 2.0 - 1.0).ravel()
    deg = config.DIP_FIT_DEGREE
    design = np.stack([x ** i * y ** j for i in range(deg + 1) for j in range(deg + 1 - i)], axis=1)
    z = level.ravel()
    fit_mask = np.isfinite(z)
    min_blocks = 4 * design.shape[1]
    if fit_mask.sum() < min_blocks:
        return None, None
    for _ in range(8):
        model = design @ np.linalg.lstsq(design[fit_mask], z[fit_mask], rcond=None)[0]
        with np.errstate(divide="ignore", invalid="ignore"):
            resid = np.where(model > 0, z / model - 1.0, np.nan)
        ok = np.isfinite(resid)
        if not ok.any():
            return None, None
        centre = np.median(resid[ok])
        spread = 1.4826 * np.median(np.abs(resid[ok] - centre))
        new_mask = ok & (np.abs(resid - centre) < 2.5 * max(spread, 1e-4))
        if new_mask.sum() < min_blocks or np.array_equal(new_mask, fit_mask):
            break
        fit_mask = new_mask
    if not np.median(model) > 3.0 * rms:
        return None, None

    smooth = _box_filter(np.nan_to_num(resid).reshape(hb, wb),
                         max(3, int(round(config.DIP_SMOOTH * max(hb, wb))) | 1))
    ey, ex = int(np.ceil(config.DIP_EDGE * hb)), int(np.ceil(config.DIP_EDGE * wb))
    search = np.zeros((hb, wb), dtype=bool)
    search[ey:hb - ey, ex:wb - ex] = True
    if not search.any():
        return None, None
    no_dip = {"depth": 0.0, "x": None, "y": None, "box": None}
    for _ in range(8):
        iy, ix = np.unravel_index(int(np.argmin(np.where(search, smooth, np.inf))), smooth.shape)
        depth = -float(smooth[iy, ix])
        if not search[iy, ix] or depth <= 0:
            break
        # The connected region at least half as deep; one running off the frame edge is an
        # unfitted gradient (clouds, twilight in a corner): leave it out and look further in
        rows, cols = np.nonzero(_grow(smooth < -depth / 2.0, iy, ix))
        if rows.min() > 0 and cols.min() > 0 and rows.max() < hb - 1 and cols.max() < wb - 1:
            box = [int(cols.min() * k), int(rows.min() * k), int((cols.max() + 1) * k), int((rows.max() + 1) * k)]
            return depth, {"depth": round(depth, 4), "x": int((ix + 0.5) * k), "y": int((iy + 0.5) * k), "box": box}
        search[rows, cols] = False
    return 0.0, no_dip


def _grow(mask: np.ndarray, iy: int, ix: int) -> np.ndarray:
    """The 4-connected part of `mask` containing (iy, ix)."""
    grown = np.zeros_like(mask)
    grown[iy, ix] = True
    while True:
        nxt = grown.copy()
        nxt[1:] |= grown[:-1]
        nxt[:-1] |= grown[1:]
        nxt[:, 1:] |= grown[:, :-1]
        nxt[:, :-1] |= grown[:, 1:]
        nxt &= mask
        if np.array_equal(nxt, grown):
            return grown
        grown = nxt


_BETA_MIN, _BETA_MAX = 1.5, 10.0   # PixInsight's Moffat family spans the same range


def fit_moffat(stamp: np.ndarray, fwhm_guess: float, center=None, iterations: int = 50):
    """
    Fit a circular Moffat profile I = A·(1 + r²/α²)^−β + C to a cutout
    (Levenberg–Marquardt with analytic derivatives). The constant C absorbs local
    background left by the global subtraction — without it that pedestal gets
    fitted as fake wings (β → 1, FWHM far too small). `center` is the (x, y)
    starting guess inside the cutout, default its middle. Returns (fwhm_px, beta),
    or None when the fit fails, wanders off the cutout, or pins β to its lower bound.
    """
    h, w = stamp.shape
    yy, xx = np.mgrid[0:h, 0:w]
    x, y = xx.ravel().astype(float), yy.ravel().astype(float)
    z = np.asarray(stamp, dtype=float).ravel()
    border = np.concatenate([stamp[0], stamp[-1], stamp[1:-1, 0], stamp[1:-1, -1]]).astype(float)
    c0 = float(np.median(border))
    amp = float(np.max(z)) - c0
    if not np.isfinite(amp) or amp <= 0:
        return None
    cx, cy = center if center is not None else ((w - 1) / 2.0, (h - 1) / 2.0)
    beta0 = 3.0
    alpha0 = max(fwhm_guess, 1.0) / (2.0 * np.sqrt(2.0 ** (1.0 / beta0) - 1.0))
    p = np.array([amp, cx, cy, alpha0, beta0, c0])

    def model(p):
        a_, x0, y0, al, be, c = p
        dx, dy = x - x0, y - y0
        r2 = dx * dx + dy * dy
        q = 1.0 + r2 / (al * al)
        core = a_ * q ** (-be)
        k = 2.0 * a_ * be * q ** (-be - 1.0) / (al * al)
        jac = np.column_stack([q ** (-be), k * dx, k * dy, k * r2 / al, -core * np.log(q), np.ones_like(q)])
        return core + c, jac

    m, jac = model(p)
    res = m - z
    cost = res @ res
    lam = 1e-2
    with np.errstate(all="ignore"):
        for _ in range(iterations):
            jtj = jac.T @ jac
            try:
                step = np.linalg.solve(jtj + lam * np.diag(np.diag(jtj) + 1e-12), -(jac.T @ res))
            except np.linalg.LinAlgError:
                return None
            trial = p + step
            trial[3] = max(trial[3], 0.3)                            # α > 0
            trial[4] = min(max(trial[4], _BETA_MIN), _BETA_MAX)      # β in a physical range
            m2, jac2 = model(trial)
            res2 = m2 - z
            cost2 = res2 @ res2
            if np.isfinite(cost2) and cost2 < cost:
                done = cost - cost2 < 1e-6 * cost
                p, jac, res, cost = trial, jac2, res2, cost2
                lam = max(lam / 3.0, 1e-7)
                if done:
                    break
            else:
                lam *= 4.0
                if lam > 1e8:
                    break
    a_, x0, y0, al, be, _ = p
    if a_ <= 0 or not (0 <= x0 < w and 0 <= y0 < h) or be <= _BETA_MIN + 1e-3:
        return None                     # β pinned low = wings the model can't explain (blend, halo)
    fwhm = 2.0 * al * np.sqrt(2.0 ** (1.0 / be) - 1.0)
    if not np.isfinite(fwhm) or fwhm < 0.5 or fwhm > w:
        return None
    return float(fwhm), float(be)


def psf_metrics(data_sub: np.ndarray, x, y, fwhm_guess: float, width: int, height: int) -> dict:
    """Median Moffat FWHM (px) and beta over up to PSF_FIT_STARS stars, in the
    order given (brightest first). Empty dict with fewer than PSF_MIN_FITS fits."""
    import config
    if not fwhm_guess or not np.isfinite(fwhm_guess):
        return {}
    half = int(np.clip(np.ceil(2.5 * fwhm_guess), 4, 15))
    fwhms, betas = [], []
    for xi, yi in zip(x[:config.PSF_FIT_STARS], y[:config.PSF_FIT_STARS]):
        cx, cy = int(round(float(xi))), int(round(float(yi)))
        if cx - half < 0 or cy - half < 0 or cx + half >= width or cy + half >= height:
            continue
        stamp = data_sub[cy - half:cy + half + 1, cx - half:cx + half + 1]
        fit = fit_moffat(stamp, fwhm_guess, center=(half + float(xi) - cx, half + float(yi) - cy))
        if fit:
            fwhms.append(fit[0])
            betas.append(fit[1])
    if len(fwhms) < config.PSF_MIN_FITS:
        return {}
    return {"psf_fwhm": float(np.median(fwhms)), "psf_beta": float(np.median(betas))}


def detect_streaks(data: np.ndarray, sep) -> list:
    """
    Satellite / aircraft trails: long, thin, straight sources on a binned copy of
    the frame (binning keeps a trail connected and lifts its SNR while stars
    shrink to dots). Returns [[x1, y1, x2, y2], ...] in original pixels.
    """
    import config
    h, w = data.shape
    k = config.STREAK_BIN or max(1, int(np.ceil(max(h, w) / 1600)))
    hb, wb = h // k, w // k
    if hb < 64 or wb < 64:
        return []
    small = np.ascontiguousarray(
        data[:hb * k, :wb * k].reshape(hb, k, wb, k).mean(axis=(1, 3)), dtype=np.float32)
    bkg = sep.Background(small)
    src = sep.extract(small - bkg, thresh=config.STREAK_THRESH, err=bkg.globalrms,
                      minarea=15, deblend_cont=1.0)
    min_len = config.STREAK_MIN_LENGTH * np.hypot(hb, wb)
    lines = []
    for s in src:
        a, b = float(s["a"]), float(s["b"])
        if not (a > 0 and b > 0):
            continue
        length = a * np.sqrt(12.0)            # a uniform line of length L has rms extent L/√12
        if length < min_len or a / b < config.STREAK_MIN_RATIO:
            continue
        half_dx, half_dy = np.cos(s["theta"]) * length / 2.0, np.sin(s["theta"]) * length / 2.0
        cx, cy = s["x"] * k + (k - 1) / 2.0, s["y"] * k + (k - 1) / 2.0
        lines.append([round(float(cx - half_dx * k), 1), round(float(cy - half_dy * k), 1),
                      round(float(cx + half_dx * k), 1), round(float(cy + half_dy * k), 1)])
    return lines


def tile_windows(height: int, width: int, tile: int, overlap: int) -> list:
    """
    Tiles for extract_sources: [(core, window), ...], each (y0, y1, x0, x1) half-open.
    Cores split the frame evenly into cells of at most `tile` px; each window is its
    core grown by `overlap` px on every side, clipped to the frame. One whole-frame
    tile when `tile` is 0 or the frame already fits in one.
    """
    if tile <= 0 or (height <= tile and width <= tile):
        return [((0, height, 0, width), (0, height, 0, width))]
    ys = np.linspace(0, height, -(-height // tile) + 1).round().astype(int)
    xs = np.linspace(0, width, -(-width // tile) + 1).round().astype(int)
    return [((int(y0), int(y1), int(x0), int(x1)),
             (max(0, int(y0) - overlap), min(height, int(y1) + overlap),
              max(0, int(x0) - overlap), min(width, int(x1) + overlap)))
            for y0, y1 in zip(ys[:-1], ys[1:]) for x0, x1 in zip(xs[:-1], xs[1:])]


_SEP_X_FIELDS = ("x", "xmin", "xmax", "xpeak", "xcpeak")
_SEP_Y_FIELDS = ("y", "ymin", "ymax", "ypeak", "ycpeak")


def extract_sources(data_sub: np.ndarray, err: float, sep):
    """
    sep.extract with the SEP_* settings over SEP_TILE_PX tiles (see config): each
    window is extracted on its own and keeps the sources whose centroid lies in its
    core, shifted back to frame coordinates. Same detections as one whole-frame pass
    except sources bigger than the overlap, which a tile border can cut (flagged
    OBJ_TRUNC, like at the frame edge). Returns (sources, number of tiles).
    """
    import config
    height, width = data_sub.shape
    tiles = tile_windows(height, width, config.SEP_TILE_PX, config.SEP_TILE_OVERLAP)
    parts = []
    for (y0, y1, x0, x1), (ya, yb, xa, xb) in tiles:
        img = data_sub if len(tiles) == 1 else np.ascontiguousarray(data_sub[ya:yb, xa:xb])
        # Higher threshold + gentler deblend keeps crowded fields from exploding
        # the deblender (memory/OOM) — we only need real stars for the metrics.
        try:
            src = sep.extract(img, thresh=config.SEP_DETECT_THRESH, err=err,
                              minarea=config.SEP_MINAREA,
                              deblend_nthresh=config.SEP_DEBLEND_NTHRESH,
                              deblend_cont=config.SEP_DEBLEND_CONT)
        except Exception as e:
            # Last-resort: disable deblending entirely (e.g. extreme star fields)
            logger.warning(f"SEP extract retry without deblend: {e}")
            src = sep.extract(img, thresh=config.SEP_DETECT_THRESH, err=err,
                              minarea=config.SEP_MINAREA, deblend_cont=1.0)
        if len(tiles) == 1:
            return src, 1
        src = src[(src["x"] + xa >= x0) & (src["x"] + xa < x1)
                  & (src["y"] + ya >= y0) & (src["y"] + ya < y1)]
        for f in _SEP_X_FIELDS:
            src[f] += xa
        for f in _SEP_Y_FIELDS:
            src[f] += ya
        parts.append(src)
    return np.concatenate(parts), len(tiles)


def analyze_stars(data_2d: np.ndarray, dtype=np.float32, timing: dict = None,
                  clipped: Optional[np.ndarray] = None) -> dict:
    """
    SEP extraction → frame quality metrics. Returns dict with any of:
    star_count, median_hfr (px), median_fwhm (px), eccentricity, trail_score,
    background_median, empty_cells, bg_spread, bg_dip, psf_fwhm (px), psf_beta,
    star_flux, streaks, and `diagnostics` (per-star codes, grid cells and streak
    lines for the preview overlay).
    Empty dict if SEP unavailable or extraction fails.

    Shape metrics (FWHM, HFR, eccentricity) use the brightest ~200 *clean* stars —
    see star_codes. star_count, the grid and trail_score use every detection.
    Frames larger than SEP_TILE_PX are extracted in overlapping tiles (see
    extract_sources); star_count still counts every detection on the frame.

    `dtype` selects float32/float64 (benchmarking). `timing`, if given, is filled
    with SEP sub-phase durations (sep_bg / sep_extract / sep_hfr).
    """
    import time as _t
    try:
        import sep
    except ImportError:
        return {}

    import config
    try:
        sep.set_extract_pixstack(config.SEP_PIXSTACK)
    except Exception:
        pass

    try:
        data = np.ascontiguousarray(data_2d.astype(dtype))
        height, width = data.shape
        _s = _t.time()
        bkg = sep.Background(data)
        back = bkg.back()
        background_median = float(np.median(back))
        if timing is not None:
            timing["sep_bg"] = _t.time() - _s; _s = _t.time()
        data_sub = data - bkg
        sources, tiles = extract_sources(data_sub, bkg.globalrms, sep)
        if timing is not None:
            timing["sep_extract"] = _t.time() - _s; _s = _t.time()
        count = int(len(sources))
        result = {"star_count": count, "background_median": background_median}
        grid = grid_metrics(sources["x"], sources["y"], back, bkg.globalrms, width, height)
        result["empty_cells"] = grid["empty_cells"]
        result["bg_spread"] = grid["bg_spread"]
        diagnostics = {"v": DIAGNOSTICS_VERSION, "width": width, "height": height, **grid["cells"], "stars": [],
                       "tiles": tiles}
        result["diagnostics"] = diagnostics
        # Frost / dew shadow: needs no stars, so measured before the empty-frame exit
        try:
            result["bg_dip"], diagnostics["dip"] = background_dip(data, float(bkg.globalrms))
        except Exception as e:
            logger.warning(f"Background dip failed: {e}")
        if count == 0:
            return result

        a = sources["a"]; b = sources["b"]; flux = sources["flux"]
        valid = (np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
                 & np.isfinite(flux) & (flux > 0))
        src = sources[valid]
        if len(src) == 0:
            return result

        # Transparency proxy: typical flux of the brighter stars. Ranks 20–200 skip
        # the clipped tops and the noisy faint tail; compare within a session only.
        by_flux = np.sort(src["flux"])[::-1]
        if len(by_flux) >= 40:
            result["star_flux"] = float(np.median(by_flux[20:200]))

        def brightest(mask, n=200):
            idx = np.flatnonzero(mask)
            return idx[np.argsort(src["flux"][idx])[-n:]] if len(idx) > n else idx

        codes = star_codes(src, width, height, float(np.nanmax(data)), float(bkg.globalback), clipped)
        unclipped = (codes != STAR_SATURATED) & (codes != STAR_EDGE)
        shape_mask = codes == STAR_CLEAN
        if shape_mask.sum() < config.STAR_MIN_CLEAN:
            shape_mask = unclipped                     # crowded field: blends allowed back
        clean_sample = shape_mask.sum() >= 5
        if not clean_sample:
            shape_mask = np.ones(len(src), dtype=bool)  # nothing clean at all: use everything
        sample = brightest(shape_mask)
        a, b = src["a"][sample], src["b"][sample]
        x, y, flux = src["x"][sample], src["y"][sample], src["flux"][sample]

        # FWHM ≈ 2.3548·σ, with σ = geometric mean of the profile semi-axes (px)
        sigma = np.sqrt(a * b)
        result["median_fwhm"] = float(np.median(2.3548 * sigma))
        # Eccentricity = sqrt(1 - (b/a)²); 0 = perfectly round
        result["eccentricity"] = float(np.median(np.sqrt(np.clip(1.0 - (b / a) ** 2, 0.0, 1.0))))
        # Coherent trailing (tracking): catches partial trails that median eccentricity
        # misses. Brightest of *all* detections — trails deblend into fragments and
        # bright saturated stars smear too, so the clean-star filter would hide them.
        # Positions let it drop satellite-streak fragments, which point the same way too.
        track = brightest(np.ones(len(src), dtype=bool))
        result["trail_score"] = trail_score(src["a"][track], src["b"][track], src["theta"][track],
                                            src["x"][track], src["y"][track])

        # Half-flux radius (HFR) — the standard AP focus metric
        try:
            rmax = float(np.clip(6.0 * np.median(a), 5.0, 25.0))
            rad, flags = sep.flux_radius(
                data_sub, x, y, np.full(len(x), rmax), 0.5, normflux=flux, subpix=5
            )
            good = np.isfinite(rad) & (flags == 0) & (rad > 0)
            if good.any():
                result["median_hfr"] = float(np.median(rad[good]))
        except Exception as e:
            logger.warning(f"HFR computation failed: {e}")
        if timing is not None:
            timing["sep_hfr"] = _t.time() - _s

        # Moffat fit on the brightest clean stars (profile FWHM + wings). Skipped when
        # only saturated/edge stars exist — a fit to clipped cores is worse than none.
        if clean_sample:
            try:
                brightest_first = sample[np.argsort(src["flux"][sample])[::-1]]
                result.update(psf_metrics(data_sub, src["x"][brightest_first], src["y"][brightest_first],
                                          result["median_fwhm"], width, height))
            except Exception as e:
                logger.warning(f"PSF fit failed: {e}")
        # Satellite / aircraft trails
        try:
            diagnostics["streaks"] = detect_streaks(data, sep)
            result["streaks"] = len(diagnostics["streaks"])
        except Exception as e:
            logger.warning(f"Streak detection failed: {e}")

        # Overlay: the measured sample plus the brightest stars left out, and why
        left_out = brightest(~np.isin(np.arange(len(src)), sample), n=100)
        used = np.zeros(len(src), dtype=bool); used[sample] = True
        diagnostics["stars"] = [
            [round(float(src["x"][i]), 1), round(float(src["y"][i]), 1),
             round(float(src["a"][i]), 2), round(float(src["b"][i]), 2),
             round(float(np.degrees(src["theta"][i]))), int(codes[i]), int(used[i])]
            for i in np.concatenate([sample, left_out])
        ]
        diagnostics["sample"] = int(len(sample))
        diagnostics["saturated"] = int(np.sum(codes == STAR_SATURATED))
        return result
    except Exception as e:
        logger.warning(f"Star analysis failed: {e}")
        return {}


def streak_fragments(a, b, theta, x, y) -> np.ndarray:
    """
    Mask of sources that are pieces of a satellite / aircraft streak: chains of at
    least TRAIL_LINE_MIN elongated sources linked end to end along one straight line
    (see config). `a` is the rms semi-major axis, so a source spans about a·√12.
    """
    import config
    a, b, theta, x, y = (np.asarray(v, dtype=float) for v in (a, b, theta, x, y))
    out = np.zeros(len(a), dtype=bool)
    idx = np.flatnonzero((a / b) > config.TRAIL_ELONGATION)
    if len(idx) < config.TRAIL_LINE_MIN:
        return out
    t = theta[idx]
    length = a[idx] * np.sqrt(12.0)
    width = b[idx]
    # Offsets measured on each pair's shared axis (orientation is an axis: θ ≡ θ+π)
    axis = np.angle(np.exp(2j * t)[:, None] + np.exp(2j * t)[None, :]) / 2.0
    dx = x[idx][None, :] - x[idx][:, None]
    dy = y[idx][None, :] - y[idx][:, None]
    along = np.abs(dx * np.cos(axis) + dy * np.sin(axis))
    across = np.abs(dy * np.cos(axis) - dx * np.sin(axis))
    turn = np.abs((t[None, :] - t[:, None] + np.pi / 2) % np.pi - np.pi / 2)
    # The gap must run along the pieces themselves: side-by-side parallel blobs (a star
    # deblended into rows of dashes by a guiding wander) are not one line
    tan_align = np.tan(np.radians(config.TRAIL_ALIGN_DEG))
    max_across = np.minimum(config.TRAIL_LINE_BAND_PX, (width[:, None] + width[None, :]) / 2.0 + along * tan_align)
    linked = ((turn < np.radians(config.TRAIL_ALIGN_DEG)) & (across < max_across)
              & (along <= config.TRAIL_LINE_GAP * (length[:, None] + length[None, :]) / 2.0))
    # Connected components of the link graph
    label = np.full(len(idx), -1)
    for start in range(len(idx)):
        if label[start] >= 0:
            continue
        label[start] = start
        stack = [start]
        while stack:
            for j in np.flatnonzero(linked[stack.pop()] & (label < 0)):
                label[j] = start
                stack.append(j)
    out[idx] = np.bincount(label, minlength=len(idx))[label] >= config.TRAIL_LINE_MIN
    return out


def trail_score(a, b, theta, x=None, y=None) -> Optional[float]:
    """
    Coherent trailing (tracking failure / mount slip) from source shapes, 0-1.

    Share of sources that are clearly elongated (a/b > TRAIL_ELONGATION) and point
    within ±TRAIL_ALIGN_DEG of the frame's dominant elongation axis, minus the share
    expected by chance if those elongated sources pointed randomly. Trailing smears
    every star the same way (high); round stars have few elongated sources, and
    tilt/coma elongate them in directions that vary across the field (≈0).
    With source positions (x, y), satellite-streak fragments are left out first
    (see streak_fragments) — they all point along the streak and would read as trailing.
    None when there are too few sources to judge.
    """
    import config
    a = np.asarray(a, dtype=float); b = np.asarray(b, dtype=float); theta = np.asarray(theta, dtype=float)
    if x is not None and y is not None:
        keep = ~streak_fragments(a, b, theta, x, y)
        a, b, theta = a[keep], b[keep], theta[keep]
    if len(a) < 5:
        return None
    # Orientation is an axis (θ ≡ θ+π), so average on the doubled angle
    axis = np.angle(np.mean((1.0 - b / a) * np.exp(2j * theta))) / 2.0
    off = np.abs((theta - axis + np.pi / 2) % np.pi - np.pi / 2)
    elongated = (a / b) > config.TRAIL_ELONGATION
    aligned = np.mean(elongated & (off < np.radians(config.TRAIL_ALIGN_DEG)))
    chance = min(2.0 * config.TRAIL_ALIGN_DEG / 180.0, 0.99)
    return float(max(0.0, (aligned - np.mean(elongated) * chance) / (1.0 - chance)))


def quality_score(hfr, fwhm, eccentricity, star_count, trail=None) -> Optional[float]:
    """
    Heuristic 0-100 frame grade. Weighted toward focus (HFR/FWHM) then roundness.
    Transparent and tunable — meant for relative culling, not absolute truth.
    Trailed frames (trail > TRAIL_THRESHOLD) are capped at TRAIL_QUALITY_CAP: SEP
    deblends streaks into compact fragments, so their HFR/eccentricity can look good.
    """
    import config
    parts = []
    weights = []
    focus = hfr if hfr is not None else (fwhm / 2.0 if fwhm is not None else None)
    if focus is not None:
        # 1.5px → 100, 6px → 0
        parts.append(float(np.clip((6.0 - focus) / (6.0 - 1.5) * 100.0, 0, 100)))
        weights.append(0.6)
    if eccentricity is not None:
        # 0.3 → 100, 0.8 → 0
        parts.append(float(np.clip((0.8 - eccentricity) / (0.8 - 0.3) * 100.0, 0, 100)))
        weights.append(0.4)
    if not parts:
        return None
    score = round(float(np.average(parts, weights=weights)), 1)
    if trail is not None and trail > config.TRAIL_THRESHOLD:
        score = min(score, config.TRAIL_QUALITY_CAP)
    return score


_IMAGETYP_MAP = {
    "light": "Light Frame",
    "light frame": "Light Frame",
    "lframe": "Light Frame",
    "science": "Light Frame",
    "dark": "Dark Frame",
    "dark frame": "Dark Frame",
    "dframe": "Dark Frame",
    "flat": "Flat Frame",
    "flat frame": "Flat Frame",
    "flat field": "Flat Frame",
    "sky flat": "Flat Frame",
    "bias": "Bias Frame",
    "bias frame": "Bias Frame",
    "offset": "Bias Frame",
    "zero": "Bias Frame",
}

def _normalize_imagetyp(value) -> Optional[str]:
    if value is None:
        return None
    return _IMAGETYP_MAP.get(str(value).strip().lower(), str(value).strip())


# Common filter-name variants -> canonical. Unknown values (incl. filter-wheel
# slot numbers like "4") pass through unchanged.
_FILTER_MAP = {
    "l": "L", "lum": "L", "luminance": "L",
    "r": "R", "red": "R",
    "g": "G", "green": "G",
    "b": "B", "blue": "B",
    "h": "Ha", "ha": "Ha", "halpha": "Ha", "h-alpha": "Ha", "h_alpha": "Ha", "h-a": "Ha",
    "o": "OIII", "o3": "OIII", "oiii": "OIII", "o-iii": "OIII",
    "s": "SII", "s2": "SII", "sii": "SII", "s-ii": "SII",
    "clear": "Clear",
}


def _normalize_filter(value) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    return _FILTER_MAP.get(key.lower(), key)


def extract_header(hdr) -> dict:
    """Extract known fields from FITS header. Returns dict of field values."""
    def get_key(hdr, *keys):
        for k in keys:
            try:
                val = hdr.get(k)
                if val is not None and val != "":
                    return val
            except Exception:
                pass
        return None

    result = {}

    result["object"] = get_key(hdr, "OBJECT")
    raw_imagetyp = get_key(hdr, "IMAGETYP", "FRAME", "FRAMETYPE")
    result["imagetyp"] = _normalize_imagetyp(raw_imagetyp)
    result["date_obs"] = get_key(hdr, "DATE-OBS", "DATE_OBS", "DATE")
    result["exptime"] = get_key(hdr, "EXPTIME", "EXPOSURE", "EXP_TIME")
    result["focal_length"] = get_key(hdr, "FOCALLEN", "FOCAL", "FOCAL_LENGTH", "TELFOCAL")
    result["telescop"] = get_key(hdr, "TELESCOP", "TELESCOPE")
    result["instrume"] = get_key(hdr, "INSTRUME", "INSTRUMENT", "CAMERA")
    result["filter"] = _normalize_filter(get_key(hdr, "FILTER", "FILTER1"))
    # Bayer pattern => one-shot colour; absent => mono
    result["bayer"] = get_key(hdr, "BAYERPAT", "BAYERPATTERN", "COLORTYP")
    result["ra"] = get_key(hdr, "RA", "RA_DEG", "CRVAL1")
    result["dec"] = get_key(hdr, "DEC", "DEC_DEG", "CRVAL2")
    result["gain"] = get_key(hdr, "GAIN", "EGAIN", "GAIN1")
    result["offset"] = get_key(hdr, "OFFSET", "PEDESTAL", "BLKLEVEL")
    result["ccd_temp"] = get_key(hdr, "CCD-TEMP", "CCD_TEMP", "CCDTEMP", "SET-TEMP")
    # Cooler setpoint: a sensor still cooling down or unable to hold it won't match the darks
    result["set_temp"] = get_key(hdr, "SET-TEMP", "SET_TEMP", "SETTEMP")
    result["xbinning"] = get_key(hdr, "XBINNING", "BINX", "HBIN")
    result["ybinning"] = get_key(hdr, "YBINNING", "BINY", "VBIN")
    result["naxis1"] = get_key(hdr, "NAXIS1")
    result["naxis2"] = get_key(hdr, "NAXIS2")
    # Physical pixel size (microns) — used to derive arcsec/px pixel scale
    result["_pixsz"] = get_key(hdr, "XPIXSZ", "PIXSIZE1", "PIXSIZE", "XPIXELSZ")
    # Observing site (decimal degrees / meters) — for altitude & moon computation
    result["_site_lat"] = get_key(hdr, "SITELAT", "LAT-OBS", "LATITUDE", "OBSGEO-B")
    result["_site_lon"] = get_key(hdr, "SITELONG", "LONG-OBS", "LONGITUD", "LONGITUDE", "OBSGEO-L")
    result["_site_elev"] = get_key(hdr, "SITEELEV", "ALT-OBS", "ELEVATIO", "HEIGHT", "OBSGEO-H")

    # Coerce numeric types
    for float_field in ("exptime", "focal_length", "ra", "dec", "gain", "ccd_temp", "set_temp",
                        "_pixsz", "_site_lat", "_site_lon", "_site_elev"):
        if result[float_field] is not None:
            try:
                result[float_field] = float(result[float_field])
            except (ValueError, TypeError):
                result[float_field] = None

    for int_field in ("offset", "xbinning", "ybinning", "naxis1", "naxis2"):
        if result[int_field] is not None:
            try:
                result[int_field] = int(result[int_field])
            except (ValueError, TypeError):
                result[int_field] = None

    # Build header JSON (all cards)
    try:
        header_dict = {}
        for key in hdr.keys():
            if key and key.strip():
                try:
                    val = hdr[key]
                    if isinstance(val, (str, int, float, bool)) or val is None:
                        header_dict[key] = val
                    else:
                        header_dict[key] = str(val)
                except Exception:
                    pass
        result["header_json"] = json.dumps(header_dict)
    except Exception:
        result["header_json"] = None

    return result


def compute_quality(data, fields: dict, dtype=np.float32, timing: dict = None) -> dict:
    """
    Run star analysis + derive pixel scale / arcsec FWHM / quality score.
    Pops the transient fields["_pixsz"]. Returns the quality columns (incl. the
    grid metrics and the overlay diagnostics as JSON).
    `dtype` + `timing` are for benchmarking (see analyze_stars).
    """
    metrics = {}
    if data is not None:
        try:
            if data.ndim == 3:
                if data.shape[0] == 3:
                    arr2d = (0.299 * data[0] + 0.587 * data[1] + 0.114 * data[2]).astype(dtype)
                else:
                    arr2d = data[0].astype(dtype)
            else:
                arr2d = data.astype(dtype)
            if data.ndim == 2 and cfa_pattern(fields.get("bayer")):
                # Raw one-shot-colour mosaic: measure 2×2 superpixels, report in sensor pixels
                metrics = in_sensor_pixels(
                    analyze_stars(superpixel(arr2d), dtype=dtype, timing=timing,
                                  clipped=superpixel_clipped(arr2d)),
                    2, arr2d.shape)
            else:
                metrics = analyze_stars(arr2d, dtype=dtype, timing=timing)
        except Exception as e:
            logger.warning(f"Star analysis error: {e}")

    pixsz = fields.pop("_pixsz", None)
    focal = fields.get("focal_length")
    xbin = fields.get("xbinning") or 1
    pixel_scale = None
    if pixsz and focal:
        # arcsec/px = 206.265 · (effective pixel µm) / focal mm
        pixel_scale = 206.265 * (pixsz * xbin) / focal
    fwhm_px = metrics.get("median_fwhm")
    fwhm_arcsec = fwhm_px * pixel_scale if (fwhm_px and pixel_scale) else None
    diagnostics = metrics.get("diagnostics")
    # Sub-second exposures (lunar/planetary) can't show sidereal trailing, and their
    # detections are surface detail (craters, limb) whose shapes would read as trails
    import config
    trail = metrics.get("trail_score")
    exptime = fields.get("exptime")
    if metrics and exptime is not None and exptime < config.TRAIL_MIN_EXPOSURE_S:
        trail = 0.0

    return {
        "empty_cells": metrics.get("empty_cells"),
        "bg_spread": metrics.get("bg_spread"),
        "bg_dip": metrics.get("bg_dip"),
        "psf_fwhm": metrics.get("psf_fwhm"),
        "psf_beta": metrics.get("psf_beta"),
        "star_flux": metrics.get("star_flux"),
        "streaks": metrics.get("streaks"),
        "diagnostics_json": json.dumps(diagnostics, separators=(",", ":")) if diagnostics else None,
        "star_count": metrics.get("star_count"),
        "median_hfr": metrics.get("median_hfr"),
        "median_fwhm": fwhm_px,
        "fwhm_arcsec": fwhm_arcsec,
        "eccentricity": metrics.get("eccentricity"),
        "trail_score": trail,
        "background_median": metrics.get("background_median"),
        "pixel_scale": pixel_scale,
        "quality_score": quality_score(
            metrics.get("median_hfr"), fwhm_px,
            metrics.get("eccentricity"), metrics.get("star_count"), trail,
        ),
    }


_EMPTY_WCS = {
    "solved": 0, "wcs_ra": None, "wcs_dec": None, "wcs_scale": None,
    "wcs_rotation": None, "fov_w": None, "fov_h": None,
}


def compute_wcs_fields(hdr, fields: dict) -> dict:
    """Parse a header WCS solution (Tier 1). Returns solved=0 dict if none.
    Also computes acquisition-vs-solved pointing separation (arcmin)."""
    import wcs_util
    res = wcs_util.parse_wcs(hdr, fields.get("naxis1"), fields.get("naxis2"))
    res = res if res else dict(_EMPTY_WCS)
    res["coord_sep_arcmin"] = coord_sep_arcmin(
        fields.get("ra"), fields.get("dec"), res.get("wcs_ra"), res.get("wcs_dec"))
    return res


def coord_sep_arcmin(ra1, dec1, ra2, dec2):
    """Angular separation (arcmin) between two RA/Dec points, or None if missing."""
    if None in (ra1, dec1, ra2, dec2):
        return None
    try:
        r1, d1 = np.radians(ra1), np.radians(dec1)
        r2, d2 = np.radians(ra2), np.radians(dec2)
        a = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
        return round(float(np.degrees(2 * np.arcsin(np.sqrt(min(1.0, a)))) * 60.0), 3)
    except Exception:
        return None


def compute_sky_fields(fields: dict, site_default: Optional[dict]) -> dict:
    """
    Compute altitude / airmass / moon geometry. Uses per-frame site from header
    if present, else the configured default site. Pops transient _site_* keys.
    """
    import sky
    lat = fields.pop("_site_lat", None)
    lon = fields.pop("_site_lon", None)
    elev = fields.pop("_site_elev", None)
    if site_default:
        if lat is None:
            lat = site_default.get("lat")
        if lon is None:
            lon = site_default.get("lon")
        if elev is None:
            elev = site_default.get("elev")
    return sky.compute_sky(fields.get("date_obs"), fields.get("ra"), fields.get("dec"), lat, lon, elev)


def process_file(filepath: str, watched_path_id: int, thumbnails_dir: str,
                 site_default: Optional[dict] = None) -> dict:
    """
    Pure worker: open a FITS file ONCE, extract header + thumbnail + star count.
    No DB access — safe to run in a thread pool. Returns a result dict; the
    caller persists it on the main thread.
    """
    import time as _time
    try:
        filepath = str(filepath)
        filename = os.path.basename(filepath)
        stat = os.stat(filepath)
        file_size = stat.st_size

        t = _time.time(); timing = {}
        # First 2D+ image drives thumbnail + star analysis
        hdr, data = read_image(filepath)
        fields = extract_header(hdr)
        timing["read"] = _time.time() - t; t = _time.time()

        thumbnail_filename = thumbnail_from_array(data, thumbnails_dir)
        timing["thumb"] = _time.time() - t; t = _time.time()
        wcs_fields = compute_wcs_fields(hdr, fields)     # before quality (reads hdr)
        timing["wcs"] = _time.time() - t; t = _time.time()
        quality = compute_quality(data, fields)         # pops fields["_pixsz"]
        timing["quality"] = _time.time() - t; t = _time.time()
        sky_fields = compute_sky_fields(fields, site_default)  # pops fields["_site_*"]
        timing["sky"] = _time.time() - t

        if sum(timing.values()) > 5:
            logger.warning("phase timing %s: %s", filename,
                           " ".join(f"{k}={v:.1f}s" for k, v in timing.items()))

        return {
            "ok": True,
            "path": filepath,
            "filename": filename,
            "watched_path_id": watched_path_id,
            "file_size": file_size,
            "file_mtime": stat.st_mtime,
            "thumbnail": thumbnail_filename,
            "fields": fields,
            "_timing": timing,
            **quality,
            **sky_fields,
            **wcs_fields,
        }
    except Exception as e:
        logger.error(f"Failed to process {filepath}: {traceback.format_exc()}")
        return {"ok": False, "path": str(filepath), "filename": os.path.basename(str(filepath)),
                "error": f"{type(e).__name__}: {e}"}


def index_file(db, filepath: str, watched_path_id: int, thumbnails_dir: str) -> tuple:
    """Single-file index (compat). Returns (success, error_message)."""
    from database import FitsFile
    result = process_file(filepath, watched_path_id, thumbnails_dir)
    if not result["ok"]:
        return False, f"{result['path']}: {result['error']}"
    try:
        db.add(_result_to_model(FitsFile, result))
        db.commit()
        return True, None
    except Exception as e:
        db.rollback()
        return False, f"{filepath}: {type(e).__name__}: {e}"


def _result_to_model(FitsFile, result: dict):
    return FitsFile(
        path=result["path"],
        filename=result["filename"],
        watched_path_id=result["watched_path_id"],
        file_size=result["file_size"],
        file_mtime=result.get("file_mtime"),
        indexed_at=datetime.utcnow(),
        thumbnail=result["thumbnail"],
        star_count=result["star_count"],
        median_hfr=result.get("median_hfr"),
        median_fwhm=result.get("median_fwhm"),
        fwhm_arcsec=result.get("fwhm_arcsec"),
        eccentricity=result.get("eccentricity"),
        trail_score=result.get("trail_score"),
        background_median=result.get("background_median"),
        pixel_scale=result.get("pixel_scale"),
        quality_score=result.get("quality_score"),
        empty_cells=result.get("empty_cells"),
        bg_spread=result.get("bg_spread"),
        bg_dip=result.get("bg_dip"),
        psf_fwhm=result.get("psf_fwhm"),
        psf_beta=result.get("psf_beta"),
        star_flux=result.get("star_flux"),
        streaks=result.get("streaks"),
        diagnostics_json=result.get("diagnostics_json"),
        altitude=result.get("altitude"),
        azimuth=result.get("azimuth"),
        airmass=result.get("airmass"),
        moon_sep=result.get("moon_sep"),
        moon_illum=result.get("moon_illum"),
        moon_alt=result.get("moon_alt"),
        solved=result.get("solved"),
        wcs_ra=result.get("wcs_ra"),
        wcs_dec=result.get("wcs_dec"),
        wcs_scale=result.get("wcs_scale"),
        wcs_rotation=result.get("wcs_rotation"),
        fov_w=result.get("fov_w"),
        fov_h=result.get("fov_h"),
        coord_sep_arcmin=result.get("coord_sep_arcmin"),
        **result["fields"],
    )


def scan_paths_stream(db, paths: list, thumbnails_dir: str, site_default: Optional[dict] = None):
    """
    Generator scan for SSE streaming. Walks paths, then processes new files in a
    thread pool while committing rows in batches on this (main) thread.
    """
    from database import FitsFile

    scanned = 0
    added = 0
    error_count = 0

    # path -> stored mtime, for change detection
    existing = {row[0]: row[1] for row in db.query(FitsFile.path, FitsFile.file_mtime).all()}

    yield {"type": "start", "paths": [p.path for p in paths]}

    # ── Phase 1: walk filesystem, collect new/changed files, emit skipped inline ──
    new_files = []      # (full_path, watched_path_id, fname)
    reindex_paths = []  # paths whose old rows must be dropped before re-insert
    for watched_path in paths:
        path_str = watched_path.path
        if not os.path.isdir(path_str):
            error_count += 1
            yield {"type": "path_error", "path": path_str, "error": "Directory not found"}
            continue

        yield {"type": "scanning_path", "path": path_str}

        for root, dirs, files in os.walk(path_str):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if Path(fname).suffix.lower() not in FITS_EXTENSIONS:
                    continue
                full_path = os.path.join(root, fname)
                scanned += 1
                if full_path in existing:
                    # Re-index only if the file changed on disk (mtime differs)
                    stored_mtime = existing[full_path]
                    try:
                        current_mtime = os.path.getmtime(full_path)
                    except OSError:
                        current_mtime = None
                    if stored_mtime is not None and current_mtime is not None \
                            and abs(current_mtime - stored_mtime) > 1.0:
                        reindex_paths.append(full_path)
                        new_files.append((full_path, watched_path.id, fname))
                    else:
                        yield {"type": "file", "status": "skipped", "filename": fname, "path": full_path,
                               "scanned": scanned, "added": added, "errors": error_count}
                else:
                    new_files.append((full_path, watched_path.id, fname))

    # Drop stale rows for changed files so the re-insert doesn't hit the unique constraint
    if reindex_paths:
        db.query(FitsFile).filter(FitsFile.path.in_(reindex_paths)).delete(synchronize_session=False)
        db.commit()

    yield {"type": "processing", "new_files": len(new_files), "workers": MAX_WORKERS}

    # ── Phase 2: process new files in parallel, commit in batches ──
    pending = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_file, fp, wpid, thumbnails_dir, site_default): fname
            for fp, wpid, fname in new_files
        }
        for future in as_completed(futures):
            fname = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"ok": False, "path": fname, "filename": fname, "error": str(e)}

            if result["ok"]:
                pending.append(result)
                added += 1
                yield {"type": "file", "status": "added", "filename": result["filename"],
                       "path": result["path"], "scanned": scanned, "added": added, "errors": error_count}
                if len(pending) >= COMMIT_BATCH:
                    error_count += _flush(db, FitsFile, pending)
                    pending = []
            else:
                error_count += 1
                yield {"type": "file", "status": "error", "filename": result["filename"],
                       "path": result["path"], "error": result.get("error", ""),
                       "scanned": scanned, "added": added, "errors": error_count}

    if pending:
        error_count += _flush(db, FitsFile, pending)

    yield {"type": "done", "scanned": scanned, "added": added, "errors": error_count}


def _flush(db, FitsFile, results: list) -> int:
    """Commit a batch of processed results. Returns count of rows that failed."""
    try:
        db.add_all([_result_to_model(FitsFile, r) for r in results])
        db.commit()
        return 0
    except Exception:
        # Batch failed (e.g. one duplicate path) — fall back to per-row commit
        db.rollback()
        failed = 0
        for r in results:
            try:
                db.add(_result_to_model(FitsFile, r))
                db.commit()
            except Exception:
                db.rollback()
                failed += 1
        return failed


def scan_paths(db, paths: list, thumbnails_dir: str, site_default: Optional[dict] = None) -> dict:
    """Blocking version — consumes the stream generator."""
    errors = []
    result = {"scanned": 0, "added": 0, "errors": errors}
    for event in scan_paths_stream(db, paths, thumbnails_dir, site_default):
        if event["type"] == "done":
            result["scanned"] = event["scanned"]
            result["added"] = event["added"]
        elif event["type"] == "file" and event["status"] == "error":
            errors.append(event.get("error", event["path"]))
        elif event["type"] == "path_error":
            errors.append(event["error"] + ": " + event["path"])
    return result


def _reanalyze_worker(filepath: str, site_default: Optional[dict] = None) -> dict:
    """Pure worker: recompute quality + sky metrics for an existing file (no thumbnail)."""
    import time as _t
    try:
        t0 = _t.time()
        hdr, data = read_image(filepath)
        fields = extract_header(hdr)
        t_read = _t.time() - t0; t0 = _t.time()
        updates = compute_quality(data, fields)            # pops _pixsz
        updates.update(compute_sky_fields(fields, site_default))  # pops _site_*
        updates.update(compute_wcs_fields(hdr, fields))    # Tier 1 plate solution
        t_compute = _t.time() - t0
        return {"ok": True, "path": filepath, "updates": updates,
                "timing": {"read": round(t_read, 2), "compute": round(t_compute, 2)}}
    except Exception as e:
        return {"ok": False, "path": filepath, "error": f"{type(e).__name__}: {e}"}


def rescore_stored(db) -> int:
    """
    Re-derive the fields that depend only on stored metrics + current settings — the
    sub-second trail rule and quality_score (trailed cap) — without re-reading FITS.
    Keeps frames analyzed under older rules or thresholds consistent. Returns the
    number of rows changed.
    """
    import config
    from database import FitsFile
    rows = (db.query(FitsFile.id, FitsFile.median_hfr, FitsFile.median_fwhm, FitsFile.eccentricity,
                     FitsFile.star_count, FitsFile.trail_score, FitsFile.exptime, FitsFile.quality_score)
            .filter(FitsFile.imagetyp == "Light Frame", FitsFile.star_count.isnot(None)).all())
    changed = []
    for r in rows:
        trail = r.trail_score
        # Same rule as compute_quality: analyzed sub-second exposures can't trail
        if r.exptime is not None and r.exptime < config.TRAIL_MIN_EXPOSURE_S:
            trail = 0.0
        score = quality_score(r.median_hfr, r.median_fwhm, r.eccentricity, r.star_count, trail)
        if trail != r.trail_score or score != r.quality_score:
            changed.append({"id": r.id, "trail_score": trail, "quality_score": score})
    for i in range(0, len(changed), 1000):
        db.bulk_update_mappings(FitsFile, changed[i:i + 1000])
        db.commit()
    return len(changed)


def reanalyze_stream(db, only_missing: bool = True, site_default: Optional[dict] = None):
    """
    Recompute quality + sky metrics for already-indexed Light frames, updating rows
    in place. Used to backfill metrics on files indexed before the feature existed.
    """
    from database import FitsFile

    q = db.query(FitsFile.id, FitsFile.path, FitsFile.filename).filter(
        FitsFile.imagetyp == "Light Frame"
    )
    if only_missing:
        # Cheap pass first: apply current trail rules / quality cap to analyzed rows
        rescore_stored(db)
        # Backfill rows missing quality or sky geometry, or analyzed by an older version
        # that lacks newer metrics (see DIAGNOSTICS_VERSION)
        q = q.filter(needs_analysis(FitsFile))
    rows = q.all()
    id_by_path = {r.path: r.id for r in rows}

    done = 0
    updated = 0
    error_count = 0
    yield {"type": "start", "total": len(rows), "workers": MAX_WORKERS}

    batch = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_reanalyze_worker, r.path, site_default): (r.path, r.filename) for r in rows}
        for future in as_completed(futures):
            path, fname = futures[future]
            done += 1
            try:
                res = future.result()
            except Exception as e:
                res = {"ok": False, "path": path, "error": str(e)}

            if res["ok"]:
                fid = id_by_path.get(res["path"])
                if fid is not None:
                    batch.append({"id": fid, **res["updates"]})
                    updated += 1
                yield {"type": "file", "status": "updated", "filename": fname,
                       "done": done, "updated": updated, "errors": error_count}
                if len(batch) >= COMMIT_BATCH:
                    db.bulk_update_mappings(FitsFile, batch)
                    db.commit()
                    batch = []
            else:
                error_count += 1
                yield {"type": "file", "status": "error", "filename": fname,
                       "error": res.get("error", ""), "done": done,
                       "updated": updated, "errors": error_count}

    if batch:
        db.bulk_update_mappings(FitsFile, batch)
        db.commit()

    yield {"type": "done", "total": len(rows), "updated": updated, "errors": error_count}


# Plate solving spawns ASTAP subprocesses — keep the pool small to avoid thrash.
SOLVE_WORKERS = max(1, min(4, MAX_WORKERS))


def _solve_worker(astap_path, row, db_dir=None) -> dict:
    import solver
    rid, path, ra, dec, naxis1, naxis2, pixscale, wcs_scale = row
    scale = wcs_scale or pixscale
    fov = (naxis2 * scale / 3600.0) if (naxis2 and scale) else None
    res = solver.solve(astap_path, path, ra_deg=ra, dec_deg=dec, fov_deg=fov,
                       naxis1=naxis1, naxis2=naxis2, db_dir=db_dir)
    res["id"] = rid
    return res


def platesolve_stream(db, astap_path, only_unsolved: bool = True):
    """
    Solve un-solved Light frames with ASTAP, updating WCS columns in place.
    Streams progress for SSE. Only touches frames missing a solution.
    """
    from sqlalchemy import or_
    import solver
    from database import FitsFile

    if not solver.astap_available(astap_path):
        yield {"type": "error", "message": "ASTAP not configured. Set the astap.exe path in Settings."}
        yield {"type": "done", "total": 0, "solved": 0, "failed": 0}
        return

    q = db.query(
        FitsFile.id, FitsFile.path, FitsFile.ra, FitsFile.dec,
        FitsFile.naxis1, FitsFile.naxis2, FitsFile.pixel_scale, FitsFile.wcs_scale,
    ).filter(FitsFile.imagetyp == "Light Frame")
    if only_unsolved:
        q = q.filter(or_(FitsFile.solved.is_(None), FitsFile.solved == 0))
    rows = q.all()

    import config
    db_dir = str(config.ASTAP_DB_DIR)

    done = solved = failed = 0
    yield {"type": "start", "total": len(rows), "workers": SOLVE_WORKERS}

    batch = []
    with ThreadPoolExecutor(max_workers=SOLVE_WORKERS) as pool:
        futures = {
            pool.submit(_solve_worker, astap_path, (r.id, r.path, r.ra, r.dec,
                        r.naxis1, r.naxis2, r.pixel_scale, r.wcs_scale), db_dir):
            os.path.basename(r.path) for r in rows
        }
        for future in as_completed(futures):
            fname = futures[future]
            done += 1
            try:
                res = future.result()
            except Exception as e:
                res = {"ok": False, "error": str(e)}

            if res.get("ok"):
                solved += 1
                batch.append({"id": res["id"], **res["wcs"]})
                yield {"type": "file", "status": "solved", "filename": fname,
                       "done": done, "solved": solved, "failed": failed}
                if len(batch) >= COMMIT_BATCH:
                    db.bulk_update_mappings(FitsFile, batch)
                    db.commit()
                    batch = []
            else:
                failed += 1
                yield {"type": "file", "status": "failed", "filename": fname,
                       "error": res.get("error", ""), "done": done,
                       "solved": solved, "failed": failed}

    if batch:
        db.bulk_update_mappings(FitsFile, batch)
        db.commit()

    yield {"type": "done", "total": len(rows), "solved": solved, "failed": failed}
