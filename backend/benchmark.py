"""
Benchmark harness — measure per-phase indexing cost on a fixed sample of real
files, comparing float32 vs float64 (interleaved to cancel disk-cache variance).

Runs the *actual* pipeline functions (scanner.thumbnail_from_array /
compute_quality / compute_wcs_fields / compute_sky_fields) so the numbers reflect
production. Nothing is written to the catalog. Results are aggregated per dtype
and per phase, including SEP sub-phases (background / extract / flux_radius) which
are the likely targets for future optimization.
"""
from __future__ import annotations

import os
import time
import json
import shutil
import random
import logging
import tempfile

import numpy as np

import scanner

logger = logging.getLogger("fitsql.benchmark")

PHASES = ["read", "thumb", "wcs", "quality", "sep_bg", "sep_extract", "sep_hfr", "sky", "total"]
DTYPES = ["float32", "float64"]
_NP = {"float32": np.float32, "float64": np.float64}

# Metrics that actually depend on dtype (the SEP / quality outputs). Each maps to
# a relative-difference tolerance; exceeding it flags the dtype change as risky.
COMPARE_METRICS = {
    "star_count": 0.005,         # 0.5% — a few near-threshold detections may flip
    "median_hfr": 0.01,          # 1%
    "median_fwhm": 0.01,
    "eccentricity": 0.01,
    "background_median": 0.01,
}

# Redis keys for live progress + last result
PROGRESS_KEY = "fitsql:benchmark:progress"
RESULT_KEY = "fitsql:benchmark:result"
CONFIG_KEY = "benchmark_files"   # AppConfig: JSON list of selected paths


def _mem_limit_bytes():
    """Container memory limit (cgroup v2 then v1); None if unconstrained."""
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as fh:
                v = fh.read().strip()
            if v and v != "max":
                n = int(v)
                if 0 < n < (1 << 62):
                    return n
        except Exception:
            pass
    return None


def _peek_megapixels(path):
    try:
        from astropy.io import fits
        h = fits.getheader(path, ignore_missing_simple=True)
        return (int(h.get("NAXIS1", 0)) * int(h.get("NAXIS2", 0))) / 1e6
    except Exception:
        return 0.0


def _f64_fits(path, limit_bytes):
    """True if a float64 pass on this frame should fit under the memory limit."""
    if not limit_bytes:
        return True
    mp = _peek_megapixels(path)
    if mp <= 0:
        return True
    # Empirically a 61 MP float64 pass peaked >3 GB: arr2d + data_sub + SEP
    # segmentation + numpy/astropy baseline ≈ 8 bytes/px × ~6, plus a safety floor.
    est = mp * 1e6 * 8 * 6
    return est < 0.7 * limit_bytes


def _site_default(db):
    from database import get_config
    lat, lon = get_config(db, "site_lat"), get_config(db, "site_lon")
    if lat is None or lon is None:
        return None
    try:
        return {"lat": float(lat), "lon": float(lon), "elev": float(get_config(db, "site_elev") or 0)}
    except (TypeError, ValueError):
        return None


def pick_files(db, n: int = 10) -> list:
    """Select n random indexed Light frames that still exist on disk; save the list."""
    from database import FitsFile, set_config
    rows = db.query(FitsFile.path).filter(FitsFile.imagetyp == "Light Frame").all()
    paths = [r.path for r in rows if os.path.exists(r.path)]
    random.shuffle(paths)
    chosen = paths[:n]
    set_config(db, CONFIG_KEY, json.dumps(chosen))
    return chosen


def get_files(db) -> list:
    from database import get_config
    raw = get_config(db, CONFIG_KEY)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def process_timed(path: str, npdtype, tmp_dir: str, site_default) -> dict:
    """Run one file through the pipeline at a given dtype, return phase timings."""
    timing = {}
    t = time.time()
    hdr, data = scanner.read_image(path)
    fields = scanner.extract_header(hdr)
    timing["read"] = time.time() - t

    t = time.time()
    scanner.thumbnail_from_array(data, tmp_dir, dtype=npdtype)
    timing["thumb"] = time.time() - t

    t = time.time()
    scanner.compute_wcs_fields(hdr, dict(fields))
    timing["wcs"] = time.time() - t

    t = time.time()
    qt = {}
    quality = scanner.compute_quality(data, dict(fields), dtype=npdtype, timing=qt)
    timing["quality"] = time.time() - t
    for k in ("sep_bg", "sep_extract", "sep_hfr"):
        if k in qt:
            timing[k] = qt[k]

    t = time.time()
    scanner.compute_sky_fields(dict(fields), site_default)
    timing["sky"] = time.time() - t

    timing["total"] = timing["read"] + timing["thumb"] + timing["wcs"] + timing["quality"] + timing["sky"]
    timing["_shape"] = list(data.shape) if data is not None else None
    timing["_megapixels"] = round(np.prod(data.shape) / 1e6, 1) if data is not None else None

    metrics = {k: quality.get(k) for k in COMPARE_METRICS}
    return {"timing": timing, "metrics": metrics}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def compare_metrics(results: list) -> dict:
    """Diff float32 vs float64 metrics per (file, repeat); aggregate + flag."""
    # Index metrics by (file, repeat, dtype)
    by_key = {}
    for r in results:
        if "metrics" in r:
            by_key[(r["file"], r["repeat"], r["dtype"])] = r["metrics"]

    per_metric = {m: {"max_rel_pct": 0.0, "max_abs": 0.0, "n": 0, "worst_file": None}
                  for m in COMPARE_METRICS}
    pairs = set((f, rep) for (f, rep, _dt) in by_key)
    for (f, rep) in pairs:
        m32 = by_key.get((f, rep, "float32"))
        m64 = by_key.get((f, rep, "float64"))
        if not m32 or not m64:
            continue
        for m in COMPARE_METRICS:
            a, b = m32.get(m), m64.get(m)
            if a is None or b is None:
                continue
            abs_d = abs(a - b)
            rel = abs_d / abs(b) if b else (0.0 if abs_d == 0 else 1.0)
            slot = per_metric[m]
            slot["n"] += 1
            if rel > slot["max_rel_pct"] / 100.0:
                slot["max_rel_pct"] = round(rel * 100.0, 4)
                slot["max_abs"] = round(abs_d, 6)
                slot["worst_file"] = f

    flags = []
    for m, tol in COMPARE_METRICS.items():
        slot = per_metric[m]
        slot["tol_pct"] = round(tol * 100.0, 3)
        slot["flagged"] = slot["max_rel_pct"] > tol * 100.0
        if slot["flagged"]:
            flags.append(f"{m}: up to {slot['max_rel_pct']}% diff (tol {slot['tol_pct']}%) on {slot['worst_file']}")

    return {"per_metric": per_metric, "flags": flags, "ok": len(flags) == 0}


def aggregate(results: list) -> dict:
    # Index completed timings by (file, repeat, dtype) and which keys each dtype ran.
    by = {}
    keys_by_dt = {}
    for r in results:
        if "timing" in r:
            k = (r["file"], r["repeat"])
            keys_by_dt.setdefault(r["dtype"], set()).add(k)
            by[(k, r["dtype"])] = r["timing"]

    # Apples-to-apples: only frames that completed in EVERY requested dtype.
    # (Skipped float64 on big frames would otherwise skew the per-dtype means.)
    common = set.intersection(*keys_by_dt.values()) if keys_by_dt else set()
    dts = [dt for dt in DTYPES if dt in keys_by_dt]

    agg = {}
    for dt in dts:
        rows = [by[(k, dt)] for k in common if (k, dt) in by]
        agg[dt] = {p: (round(_mean([row.get(p) for row in rows]), 3)
                       if _mean([row.get(p) for row in rows]) is not None else None)
                   for p in PHASES}

    speedup = {}
    f32, f64 = agg.get("float32", {}), agg.get("float64", {})
    for p in PHASES:
        a, b = f32.get(p), f64.get(p)
        speedup[p] = round(b / a, 2) if (a and b) else None

    mp_vals = [by[(k, dts[0])].get("_megapixels") for k in common if dts] if dts else []
    mp = round(_mean(mp_vals), 1) if _mean(mp_vals) is not None else None
    skipped = sum(1 for r in results if r.get("skipped"))
    errors = sum(1 for r in results if r.get("error"))
    return {"per_dtype": agg, "speedup": speedup, "runs": len(results),
            "compared": len(common), "skipped": skipped, "errors": errors,
            "megapixels": mp, "comparison": compare_metrics(results),
            "results": results}


def run(files: list, dtypes: list, repeats: int = 1, site_default=None, progress_cb=None) -> dict:
    """
    Execute the benchmark. Interleaves dtypes per file (alternating order).
    No DB dependency — `site_default` (lat/lon/elev) is optional; frame headers
    with site coords still drive the sky phase regardless.
    """
    site = site_default
    tmp = tempfile.mkdtemp(prefix="fitsql_bench_")
    runs = []
    for rep in range(repeats):
        for fi, f in enumerate(files):
            order = dtypes if fi % 2 == 0 else list(reversed(dtypes))
            for dt in order:
                runs.append((rep, f, dt))

    # Warm up astropy / SEP / numpy import + IERS + ephemeris caches so the first
    # timed sample isn't inflated by one-time initialisation.
    try:
        process_timed(files[0], _NP[dtypes[0]], tmp, site)
    except Exception:
        pass

    limit = _mem_limit_bytes()
    logger.info("benchmark: memory limit = %s MB",
                round(limit / 1e6) if limit else "unknown (no skip guard)")
    results = []
    total = len(runs)
    try:
        for i, (rep, f, dt) in enumerate(runs):
            mp = _peek_megapixels(f)
            entry = {"file": os.path.basename(f), "fullpath": f, "dtype": dt,
                     "repeat": rep, "megapixels": round(mp, 1)}
            # Loud per-run log so a hard crash leaves the culprit as the last line.
            logger.info("bench %d/%d: %s %s (~%.1f MP)", i + 1, total, dt, entry["file"], mp)

            # Skip the float64 leg on frames too big to fit — would OOM the worker.
            if dt == "float64" and not _f64_fits(f, limit):
                entry["skipped"] = "frame too large for float64 under the worker memory limit"
                logger.info("  -> skipped float64 (~%.1f MP too big for the memory limit)", mp)
                results.append(entry)
                if progress_cb:
                    progress_cb(i + 1, total)
                continue
            t0 = time.time()
            try:
                out = process_timed(f, _NP[dt], tmp, site)
                entry["timing"] = out["timing"]
                entry["metrics"] = out["metrics"]
                logger.info("  done %s in %.1fs", dt, time.time() - t0)
            except MemoryError:
                entry["error"] = "MemoryError (frame too large for this dtype)"
                logger.warning("  MemoryError on %s %s", dt, entry["file"])
            except Exception as e:
                entry["error"] = str(e)
                logger.warning("  error on %s %s: %s", dt, entry["file"], e)
            results.append(entry)
            if progress_cb:
                progress_cb(i + 1, total)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return aggregate(results)
