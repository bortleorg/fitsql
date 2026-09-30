#!/usr/bin/env python
"""
Standalone benchmark runner — no Docker, no Redis, no DB.

Runs the real indexing pipeline (the same functions production/workers use) over a
curated dataset of FITS files, comparing float32 vs float64 with per-phase timing,
and records a versioned result (committed to git) so performance can be tracked
over time and across machines.

Usage (from anywhere):
    python backend/bench_cli.py
    python backend/bench_cli.py --dataset /path/to/fits --dtypes float32 --repeats 3
    python backend/bench_cli.py --no-save        # don't write a result file

Curate a dataset by dropping representative FITS into benchmarks/dataset/
(gitignored — frames are large; only results are committed).
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
import platform
import subprocess
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# Match the worker's thread pinning so local numbers reflect production-style runs.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np
import benchmark  # noqa: E402

FITS_EXTS = (".fit", ".fits", ".fts", ".xisf")
BENCH_DIR = os.path.join(ROOT, "benchmarks")
DATASET_DIR = os.path.join(BENCH_DIR, "dataset")
RESULTS_DIR = os.path.join(BENCH_DIR, "results")
HISTORY = os.path.join(BENCH_DIR, "HISTORY.md")


def _git(*args):
    try:
        return subprocess.check_output(["git", "-C", ROOT, *args],
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _ram_gb():
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1e9, 1)
    except Exception:
        pass
    try:  # Linux
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
    except Exception:
        return None


def _versions():
    out = {"python": platform.python_version(), "numpy": np.__version__}
    for mod in ("astropy", "sep", "PIL"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:
            out[mod] = None
    return out


def collect_env():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "ram_gb": _ram_gb(),
        "thread_pin": os.environ.get("OMP_NUM_THREADS"),
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "versions": _versions(),
    }


def find_fits(dataset):
    # Accept a single FITS file (debug one frame) or a folder to walk
    if os.path.isfile(dataset):
        return [dataset] if os.path.splitext(dataset)[1].lower() in FITS_EXTS else []
    files = []
    for root, _dirs, names in os.walk(dataset):
        for n in names:
            if os.path.splitext(n)[1].lower() in FITS_EXTS:
                files.append(os.path.join(root, n))
    return sorted(files)


def print_table(result):
    per, sp, cmp = result["per_dtype"], result["speedup"], result.get("comparison")
    print(f"\n{result['runs']} runs, ~{result.get('megapixels')} MP/frame,"
          f" skipped={result.get('skipped', 0)} errors={result.get('errors', 0)}")
    print(f"{'phase':<12}{'float32':>10}{'float64':>10}{'f32x':>8}")
    for p in benchmark.PHASES:
        f32 = per.get("float32", {}).get(p)
        f64 = per.get("float64", {}).get(p)
        print(f"{p:<12}{(f'{f32*1000:.0f}ms' if f32 else '-'):>10}"
              f"{(f'{f64*1000:.0f}ms' if f64 else '-'):>10}"
              f"{(f'{sp.get(p)}x' if sp.get(p) else '-'):>8}")
    if cmp:
        print(f"\nmetric accuracy f32 vs f64: {'MATCH' if cmp['ok'] else 'DIFFER'}")
        for f in cmp.get("flags", []):
            print(f"  ! {f}")


def save(env, result):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = env["timestamp"].replace(":", "").replace("-", "")
    name = f"{ts}_{env['machine'] or 'host'}.json".replace(" ", "_")
    path = os.path.join(RESULTS_DIR, name)
    # Trim the bulky per-run list — keep aggregates for versioning
    trimmed = {k: v for k, v in result.items() if k != "results"}
    with open(path, "w") as fh:
        json.dump({"env": env, "result": trimmed}, fh, indent=2)
    _append_history(env, result)
    return path


def _append_history(env, result):
    per = result["per_dtype"].get("float32", {})
    row = ("| {date} | {machine} | {commit}{dirty} | {mp} | {frames} | "
           "{total} | {read} | {thumb} | {quality} | {sky} | {acc} |").format(
        date=env["timestamp"][:16].replace("T", " "),
        machine=(env["machine"] or "?")[:16],
        commit=env["git_commit"] or "?",
        dirty="*" if env["git_dirty"] else "",
        mp=result.get("megapixels"),
        frames=result["runs"],
        total=_ms(per.get("total")), read=_ms(per.get("read")),
        thumb=_ms(per.get("thumb")), quality=_ms(per.get("quality")),
        sky=_ms(per.get("sky")),
        acc="ok" if (result.get("comparison", {}) or {}).get("ok", True) else "DIFF",
    )
    new = not os.path.exists(HISTORY)
    with open(HISTORY, "a") as fh:
        if new:
            fh.write("# Benchmark history\n\n"
                     "float32 mean per-phase (ms). Machines/commits vary — read with the env.\n\n"
                     "| date (UTC) | machine | commit | MP | frames | total | read | thumb | quality | sky | acc |\n"
                     "|---|---|---|---|---|---|---|---|---|---|---|\n")
        fh.write(row + "\n")


def _ms(s):
    return f"{s*1000:.0f}" if s else "-"


def main():
    ap = argparse.ArgumentParser(description="fitsql standalone benchmark")
    ap.add_argument("--dataset", default=DATASET_DIR, help="folder of FITS files")
    ap.add_argument("--dtypes", default="float32,float64", help="comma list")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--site", default=None, help="lat,lon[,elev] for sky phase if headers lack it")
    ap.add_argument("--no-save", action="store_true", help="don't write a result file")
    args = ap.parse_args()

    files = find_fits(args.dataset)
    if not files:
        print(f"No FITS files in {args.dataset}\n"
              f"Curate a dataset there (or pass --dataset).", file=sys.stderr)
        return 1
    dtypes = [d.strip() for d in args.dtypes.split(",") if d.strip()]

    site = None
    if args.site:
        p = [float(x) for x in args.site.split(",")]
        site = {"lat": p[0], "lon": p[1], "elev": p[2] if len(p) > 2 else 0}

    env = collect_env()
    print(f"machine={env['machine']} commit={env['git_commit']}{'*' if env['git_dirty'] else ''} "
          f"cpu={env['cpu_count']} ram={env['ram_gb']}GB")
    print(f"benchmarking {len(files)} files x {dtypes} x {args.repeats} "
          f"(omp_threads={env['thread_pin']})...")

    t0 = time.time()
    n = len(files) * len(dtypes) * args.repeats

    def prog(done, total):
        sys.stdout.write(f"\r  {done}/{total}")
        sys.stdout.flush()

    result = benchmark.run(files, dtypes, args.repeats, site_default=site, progress_cb=prog)
    print(f"\rdone in {time.time()-t0:.1f}s" + " " * 10)
    print_table(result)

    if not args.no_save:
        path = save(env, result)
        print(f"\nsaved {os.path.relpath(path, ROOT)} + appended benchmarks/HISTORY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
