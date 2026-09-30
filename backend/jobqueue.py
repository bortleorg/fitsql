"""
Simple Redis-backed job queue.

A single list (`fitsql:q`) holds pending jobs (JSON). Worker containers BRPOP from
it. Progress counters live in a hash so the UI can show throughput / ETA, and the
queue can be inspected and flushed.

Deliberately framework-free (no Celery/RQ) so the queue is transparent: you can
see exactly what's pending and flush it.

Degrades gracefully: if Redis is unreachable, status reports unavailable and
enqueues raise QueueUnavailable (the API surfaces a clear message).
"""
from __future__ import annotations

import json
import time
import logging

import config

logger = logging.getLogger("fitsql.jobqueue")

Q_KEY = "fitsql:q"             # list of pending job JSON strings
INFLIGHT_KEY = "fitsql:inflight"  # int, jobs currently being processed
PROG_KEY = "fitsql:prog"       # hash: enqueued, done, failed, started_at
DURS_KEY = "fitsql:durs"       # list of recent per-job durations (seconds)
WORKERS_KEY = "fitsql:workers"  # hash: worker_id -> last-seen epoch
_DURS_MAX = 200
_WORKER_TTL = 30             # seconds; workers heartbeat more often than this

# Leaf job ops that represent actual file work (counted in progress).
LEAF_OPS = {"index", "reanalyze", "solve"}


class QueueUnavailable(RuntimeError):
    pass


_client = None
_blocking = None


def client():
    """Shared client for fast, non-blocking ops (fast-fail read timeout)."""
    global _client
    if _client is None:
        import redis
        _client = redis.from_url(config.REDIS_URL, decode_responses=True,
                                 socket_connect_timeout=3, socket_timeout=5)
    return _client


def blocking_client():
    """Separate client for BRPOP — no read timeout, so the blocking pop never
    races its own socket deadline on an idle queue."""
    global _blocking
    if _blocking is None:
        import redis
        _blocking = redis.from_url(config.REDIS_URL, decode_responses=True,
                                   socket_connect_timeout=3, socket_timeout=None)
    return _blocking


def available() -> bool:
    try:
        return bool(client().ping())
    except Exception:
        return False


def _maybe_reset_run(r):
    """Start a fresh progress run if the queue is idle (nothing pending/inflight)."""
    if r.llen(Q_KEY) == 0 and int(r.get(INFLIGHT_KEY) or 0) <= 0:
        r.delete(PROG_KEY, DURS_KEY)
        r.hset(PROG_KEY, mapping={"enqueued": 0, "done": 0, "failed": 0,
                                  "started_at": time.time()})


def enqueue(jobs: list[dict]) -> int:
    """Push jobs onto the queue. Returns count enqueued. Bumps the leaf counter."""
    if not jobs:
        return 0
    try:
        r = client()
        _maybe_reset_run(r)
        pipe = r.pipeline()
        leaf = 0
        for job in jobs:
            pipe.lpush(Q_KEY, json.dumps(job))
            if job.get("op") in LEAF_OPS:
                leaf += 1
        pipe.execute()
        if leaf:
            r.hincrby(PROG_KEY, "enqueued", leaf)
        return len(jobs)
    except QueueUnavailable:
        raise
    except Exception as e:
        raise QueueUnavailable(str(e))


def enqueue_one(job: dict) -> int:
    return enqueue([job])


# ── Worker-side ──────────────────────────────────────────────────────────────
def dequeue(timeout: int = 5):
    """Blocking pop. Returns (raw, job_dict) or (None, None) on timeout."""
    res = blocking_client().brpop(Q_KEY, timeout=timeout)
    if not res:
        return None, None
    _, raw = res
    client().incr(INFLIGHT_KEY)
    try:
        return raw, json.loads(raw)
    except Exception:
        client().decr(INFLIGHT_KEY)
        return None, None


def complete(job: dict, duration: float, ok: bool):
    r = client()
    try:
        if r.decr(INFLIGHT_KEY) < 0:
            r.set(INFLIGHT_KEY, 0)   # don't let a crashed/flushed run push it negative
        if job.get("op") in LEAF_OPS:
            r.hincrby(PROG_KEY, "done" if ok else "failed", 1)
            if ok:
                pipe = r.pipeline()
                pipe.lpush(DURS_KEY, duration)
                pipe.ltrim(DURS_KEY, 0, _DURS_MAX - 1)
                pipe.execute()
    except Exception:
        logger.warning("Failed to record job completion", exc_info=True)


def heartbeat(worker_id: str):
    try:
        client().hset(WORKERS_KEY, worker_id, time.time())
    except Exception:
        pass


# ── Status / control ─────────────────────────────────────────────────────────
def status() -> dict:
    try:
        r = client()
    except Exception:
        return {"available": False}
    try:
        pending = r.llen(Q_KEY)
        inflight = max(0, int(r.get(INFLIGHT_KEY) or 0))
        prog = r.hgetall(PROG_KEY) or {}
        enqueued = int(float(prog.get("enqueued", 0)))
        done = int(float(prog.get("done", 0)))
        failed = int(float(prog.get("failed", 0)))
        started_at = float(prog.get("started_at", 0) or 0)
        elapsed = max(0.001, time.time() - started_at) if started_at else 0

        # Throughput from wall clock (files/sec across all workers)
        rate = (done / elapsed) if elapsed else 0.0
        remaining = max(0, enqueued - done - failed)
        eta = (remaining / rate) if rate > 0 else None

        # Live workers (heartbeat within TTL)
        now = time.time()
        workers = r.hgetall(WORKERS_KEY) or {}
        live = sum(1 for ts in workers.values() if now - float(ts) < _WORKER_TTL)

        return {
            "available": True,
            "pending": pending,
            "inflight": inflight,
            "enqueued": enqueued,
            "done": done,
            "failed": failed,
            "remaining": remaining,
            "rate_per_sec": round(rate, 2),
            "eta_sec": round(eta) if eta is not None else None,
            "workers": live,
            "active": pending > 0 or inflight > 0,
        }
    except Exception as e:
        logger.warning("queue status failed: %s", e)
        return {"available": False}


def flush() -> dict:
    """Clear pending jobs + reset all progress (incl. a stuck benchmark run)."""
    r = client()
    cleared = r.llen(Q_KEY)
    pipe = r.pipeline()
    # also clear benchmark progress so a crashed benchmark doesn't stay "running"
    pipe.delete(Q_KEY, PROG_KEY, DURS_KEY, "fitsql:benchmark:progress")
    pipe.set(INFLIGHT_KEY, 0)
    pipe.execute()
    return {"cleared": cleared}


def peek(n: int = 20) -> list:
    """Return up to n pending jobs (most-recent first) without removing them."""
    try:
        r = client()
        raws = r.lrange(Q_KEY, 0, n - 1)
        return [json.loads(x) for x in raws]
    except Exception:
        return []
