"""
Shared logging configuration + diagnostics (slow-query logging) for both the web
and worker processes.
"""
import time
import logging

import config

_configured = False


def setup_logging():
    global _configured
    if _configured:
        return
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Tame noisy access logs; keep our app + warnings.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _configured = True
    _install_slow_query_logging()


def _install_slow_query_logging():
    """Log any SQL statement slower than SLOW_QUERY_MS."""
    from sqlalchemy import event
    from database import engine

    log = logging.getLogger("fitsql.sql")

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("_q_start", []).append(time.time())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        try:
            start = conn.info["_q_start"].pop(-1)
        except (KeyError, IndexError):
            return
        ms = (time.time() - start) * 1000.0
        if ms >= config.SLOW_QUERY_MS:
            stmt = " ".join(statement.split())[:160]
            log.warning("slow query %.0fms: %s", ms, stmt)
