"""
Worker process — runs in its own container (scale with more replicas).

Pulls jobs off the Redis queue and does the heavy lifting (FITS read, thumbnail,
SEP quality, WCS/ASTAP solve), writing results to the shared SQLite catalog
(WAL + busy_timeout handle concurrent writers). The web container stays
responsive because none of this runs in a request.

Job ops:
  scan          -> walk a watched path, enqueue `index` jobs for new/changed files
  index         -> process one FITS file, insert (or re-index) its row
  reanalyze_all -> enqueue `reanalyze` jobs for lights missing metrics
  reanalyze     -> recompute quality/sky/WCS for one row
  solve_all     -> enqueue `solve` jobs for unsolved lights
  solve         -> ASTAP plate-solve one row
"""
from __future__ import annotations

import os
import sys
import time
import signal
import socket
import logging
import threading

import config
import jobqueue
import scanner
from logging_setup import setup_logging

log = logging.getLogger("fitsql.worker")

FITS_EXTS = scanner.FITS_EXTENSIONS
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


# ── shared-config reads (from the catalog DB) ────────────────────────────────
def _site_default(db):
    from database import get_config
    lat, lon = get_config(db, "site_lat"), get_config(db, "site_lon")
    if lat is None or lon is None:
        return None
    try:
        return {"lat": float(lat), "lon": float(lon),
                "elev": float(get_config(db, "site_elev") or 0)}
    except (TypeError, ValueError):
        return None


def _astap_path(db):
    from database import get_config
    return get_config(db, "astap_path") or config.ASTAP_PATH


def _commit(db, attempts: int = 5):
    from sqlalchemy.exc import OperationalError
    for i in range(attempts):
        try:
            db.commit()
            return
        except OperationalError:
            db.rollback()
            time.sleep(0.25 * (i + 1))
    raise


# ── job handlers ─────────────────────────────────────────────────────────────
def handle_scan(db, job):
    from database import FitsFile
    path, wpid = job["path"], job.get("watched_path_id")
    if not os.path.isdir(path):
        log.warning("scan: path missing: %s", path)
        return
    existing = {p: m for p, m in db.query(FitsFile.path, FitsFile.file_mtime).all()}
    batch, total = [], 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() not in FITS_EXTS:
                continue
            full = os.path.join(root, f)
            if full in existing:
                try:
                    cur = os.path.getmtime(full)
                except OSError:
                    continue
                if existing[full] and abs(cur - existing[full]) > 1.0:
                    batch.append({"op": "index", "path": full, "watched_path_id": wpid, "reindex": True})
            else:
                batch.append({"op": "index", "path": full, "watched_path_id": wpid})
            if len(batch) >= 500:
                total += jobqueue.enqueue(batch); batch = []
    total += jobqueue.enqueue(batch)
    log.info("scan %s -> enqueued %d file jobs", path, total)


def handle_index(db, job):
    import os as _os
    from database import FitsFile
    if job.get("reindex"):
        db.query(FitsFile).filter(FitsFile.path == job["path"]).delete(synchronize_session=False)
        _commit(db)
    res = scanner.process_file(job["path"], job.get("watched_path_id"),
                               str(config.THUMBNAILS_DIR), _site_default(db))
    if not res.get("ok"):
        # Record a tombstone so scans skip this corrupt/unreadable file next time
        err = res.get("error", "process failed")
        try:
            mtime = _os.path.getmtime(job["path"])
        except OSError:
            mtime = None
        db.add(FitsFile(path=job["path"], filename=_os.path.basename(job["path"]),
                        watched_path_id=job.get("watched_path_id"),
                        file_mtime=mtime, index_error=err[:500]))
        _commit(db)
        raise RuntimeError(err)   # still counts as a failed job in the queue
    db.add(scanner._result_to_model(FitsFile, res))
    _commit(db)


def handle_reanalyze_all(db, job):
    from database import FitsFile
    q = db.query(FitsFile.id, FitsFile.path).filter(FitsFile.imagetyp == "Light Frame")
    if job.get("only_missing", True):
        # Cheap pass first (no FITS reads): apply current trail rules / quality cap to
        # analyzed rows, so short exposures stop counting as missing a trail score
        log.info("rescored %d rows from stored metrics", scanner.rescore_stored(db))
        q = q.filter(scanner.needs_analysis(FitsFile))
    batch, total = [], 0
    for r in q.all():
        batch.append({"op": "reanalyze", "id": r.id, "path": r.path})
        if len(batch) >= 500:
            total += jobqueue.enqueue(batch); batch = []
    total += jobqueue.enqueue(batch)
    log.info("reanalyze_all -> enqueued %d", total)


def handle_reanalyze(db, job):
    from database import FitsFile
    res = scanner._reanalyze_worker(job["path"], _site_default(db))
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "reanalyze failed"))
    tc = time.time()
    db.bulk_update_mappings(FitsFile, [{"id": job["id"], **res["updates"]}])
    _commit(db)
    t = res.get("timing", {})
    t["commit"] = round(time.time() - tc, 2)
    # Surface where the time goes — IO (read) vs CPU (compute) vs DB (commit)
    log.info("reanalyze %s read=%ss compute=%ss commit=%ss",
             os.path.basename(job["path"]), t.get("read"), t.get("compute"), t["commit"])


def handle_solve_all(db, job):
    from sqlalchemy import or_
    from database import FitsFile
    q = db.query(FitsFile.id).filter(FitsFile.imagetyp == "Light Frame")
    if job.get("only_unsolved", True):
        q = q.filter(or_(FitsFile.solved.is_(None), FitsFile.solved == 0))
    batch, total = [], 0
    for r in q.all():
        batch.append({"op": "solve", "id": r.id})
        if len(batch) >= 500:
            total += jobqueue.enqueue(batch); batch = []
    total += jobqueue.enqueue(batch)
    log.info("solve_all -> enqueued %d", total)


def handle_solve(db, job):
    from database import FitsFile
    row = db.query(
        FitsFile.id, FitsFile.path, FitsFile.ra, FitsFile.dec,
        FitsFile.naxis1, FitsFile.naxis2, FitsFile.pixel_scale, FitsFile.wcs_scale,
    ).filter(FitsFile.id == job["id"]).first()
    if not row:
        return
    res = scanner._solve_worker(
        _astap_path(db),
        (row.id, row.path, row.ra, row.dec, row.naxis1, row.naxis2, row.pixel_scale, row.wcs_scale),
        str(config.ASTAP_DB_DIR),
    )
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "solve failed"))
    wcs = dict(res["wcs"])
    # Flag acquisition-vs-solved pointing disagreement (bad header RA/Dec)
    wcs["coord_sep_arcmin"] = scanner.coord_sep_arcmin(
        row.ra, row.dec, wcs.get("wcs_ra"), wcs.get("wcs_dec"))
    db.bulk_update_mappings(FitsFile, [{"id": row.id, **wcs}])
    _commit(db)


def handle_benchmark(db, job):
    import json
    import benchmark
    r = jobqueue.client()
    files = job.get("files") or benchmark.get_files(db)
    if not files:
        r.set(benchmark.RESULT_KEY, json.dumps({"error": "no benchmark files selected"}))
        return
    dtypes = job.get("dtypes") or benchmark.DTYPES
    repeats = int(job.get("repeats", 1))

    def progress(done, total):
        r.set(benchmark.PROGRESS_KEY, json.dumps({"done": done, "total": total}))

    r.delete(benchmark.RESULT_KEY)
    r.set(benchmark.PROGRESS_KEY, json.dumps({"done": 0, "total": len(files) * len(dtypes) * repeats}))
    log.info("benchmark: %d files × %s × %d repeats", len(files), dtypes, repeats)
    try:
        result = benchmark.run(files, dtypes, repeats,
                               site_default=benchmark._site_default(db), progress_cb=progress)
        r.set(benchmark.RESULT_KEY, json.dumps(result))
        log.info("benchmark done: %s", result.get("speedup"))
    except Exception as e:
        log.error("benchmark failed: %s", e)
        r.set(benchmark.RESULT_KEY, json.dumps({"error": str(e)}))
    finally:
        # Always clear progress so a failed/partial run doesn't show "running" forever
        r.delete(benchmark.PROGRESS_KEY)


HANDLERS = {
    "scan": handle_scan,
    "index": handle_index,
    "reanalyze_all": handle_reanalyze_all,
    "reanalyze": handle_reanalyze,
    "solve_all": handle_solve_all,
    "solve": handle_solve,
    "benchmark": handle_benchmark,
}


def process_job(job: dict):
    from database import SessionLocal
    op = job.get("op")
    handler = HANDLERS.get(op)
    if not handler:
        log.warning("unknown op: %s", op)
        return False
    db = SessionLocal()
    # Log the target BEFORE processing so a hard crash/OOM leaves the offending
    # file as the last line — copy that path to debug it locally (bench_cli, etc).
    target = job.get("path") or f"id={job.get('id')}"
    log.info("processing %s: %s", op, target)
    t0 = time.time()
    try:
        handler(db, job)
        dt = time.time() - t0
        name = os.path.basename(job.get("path", "")) or job.get("id", "")
        if dt > 5:
            log.warning("slow job %s %s took %.1fs", op, name, dt)
        else:
            log.info("job %s %s ok (%.2fs)", op, name, dt)
        return True
    except Exception as e:
        log.error("job %s failed: %s", op, e)
        return False
    finally:
        db.close()


def _heartbeat_loop(stop):
    while not stop.is_set():
        jobqueue.heartbeat(WORKER_ID)
        stop.wait(10)


def _consumer(stop):
    """One concurrent slot: block on the queue, process, repeat (natural backpressure)."""
    while not stop.is_set():
        try:
            _raw, job = jobqueue.dequeue(timeout=5)
        except Exception as e:
            log.warning("dequeue error: %s", e)
            time.sleep(2)
            continue
        if job is None:
            continue
        t0 = time.time()
        ok = process_job(job)
        jobqueue.complete(job, time.time() - t0, ok)


def run():
    setup_logging()
    log.info("worker %s starting, concurrency=%d, redis=%s",
             WORKER_ID, config.WORKER_CONCURRENCY, config.REDIS_URL)

    # Wait for Redis to be reachable
    while not jobqueue.available():
        log.warning("waiting for Redis at %s ...", config.REDIS_URL)
        time.sleep(3)

    from database import create_tables
    create_tables()  # ensure schema/migrations (safe if web already did it)

    stop = threading.Event()

    # Graceful shutdown: SIGTERM (docker stop / scale-down) + SIGINT.
    # Consumers finish their current job, call complete() (so inflight doesn't
    # leak), then exit. Docker's stop_grace_period must exceed a job's runtime.
    def _on_signal(signum, _frame):
        log.info("signal %s received — draining, finishing in-flight jobs", signum)
        stop.set()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    threading.Thread(target=_heartbeat_loop, args=(stop,), daemon=True).start()

    threads = [threading.Thread(target=_consumer, args=(stop,))
               for _ in range(config.WORKER_CONCURRENCY)]
    for t in threads:
        t.start()
    log.info("worker ready with %d consumer threads", len(threads))

    while not stop.is_set():
        time.sleep(0.5)

    log.info("draining — waiting for in-flight jobs to finish")
    for t in threads:
        t.join()
    # Drop our heartbeat so the queue's live-worker count updates promptly
    try:
        jobqueue.client().hdel(jobqueue.WORKERS_KEY, WORKER_ID)
    except Exception:
        pass
    log.info("worker %s stopped cleanly", WORKER_ID)


if __name__ == "__main__":
    sys.exit(run())
