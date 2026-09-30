"""
On-demand ASTAP star-database download.

Star databases are large and FOV-dependent, so rather than bloat the container
image we fetch the chosen one into the data volume (ASTAP_DB_DIR) at the user's
request, streaming progress. ASTAP is then pointed at that directory with `-d`.
"""
from __future__ import annotations

import os
import zipfile
import tempfile
import urllib.request

# Curated ASTAP databases (files in the SourceForge star_databases folder).
# A custom URL can also be supplied by the UI. D50 is the recommended default.
DATABASES = {
    "d50": {
        "name": "D50 — Gaia (recommended)",
        "file": "d50_star_database.zip",
        "size_mb": 901,
        "note": "Best general default. Works narrow → wide, to mag ~18.",
    },
    "d20": {
        "name": "D20 — Gaia, mid",
        "file": "d20_star_database.zip",
        "size_mb": 400,
        "note": "Smaller. Good for medium / wider fields.",
    },
    "d05": {
        "name": "D05 — Gaia, light",
        "file": "d05_star_database.zip",
        "size_mb": 102,
        "note": "Compact. Wide / medium fields.",
    },
    "w08": {
        "name": "W08 — wide field only",
        "file": "w08_star_database_mag08_astap.zip",
        "size_mb": 1,
        "note": "Tiny (mag 8). Very wide fields only.",
    },
}

_SF_PROJECT = "astap-program"
_SF_FOLDER = "star_databases"
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _candidate_urls(filename):
    # Several SourceForge host forms — mirror behaviour varies, so try each.
    return [
        f"https://master.dl.sourceforge.net/project/{_SF_PROJECT}/{_SF_FOLDER}/{filename}?viasf=1",
        f"https://downloads.sourceforge.net/project/{_SF_PROJECT}/{_SF_FOLDER}/{filename}",
        f"https://sourceforge.net/projects/{_SF_PROJECT}/files/{_SF_FOLDER}/{filename}/download",
    ]


def _open(url, extra_headers=None):
    headers = {"User-Agent": _BROWSER_UA, "Accept": "application/zip,application/octet-stream,*/*"}
    if extra_headers:
        headers.update(extra_headers)
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def _resolve_url(filename):
    """Probe candidate URLs (4-byte range) and return the first serving a real zip."""
    for url in _candidate_urls(filename):
        try:
            r = _open(url, {"Range": "bytes=0-3"})
            head = r.read(4)
            r.close()
            if head == b"PK\x03\x04":
                return url
        except Exception:
            continue
    return None

# ASTAP database files use these extensions (numeric-ish). Presence => installed.
_DB_SUFFIXES = (".290", ".1476", ".001", ".0101", ".0606")


def installed_keys(db_dir) -> list:
    db_dir = str(db_dir)
    if not os.path.isdir(db_dir):
        return []
    return [k for k in DATABASES if os.path.exists(os.path.join(db_dir, f".installed_{k}"))]


def db_ready(db_dir) -> bool:
    db_dir = str(db_dir)
    if not os.path.isdir(db_dir):
        return False
    for f in os.listdir(db_dir):
        if f.endswith(_DB_SUFFIXES):
            return True
    return bool(installed_keys(db_dir))


def list_databases(db_dir) -> dict:
    keys = set(installed_keys(db_dir))
    return {
        "databases": [
            {"key": k, **v, "installed": k in keys} for k, v in DATABASES.items()
        ],
        "ready": db_ready(db_dir),
    }


def _flatten(db_dir):
    """Move any files nested in subfolders up to db_dir, then drop empty dirs."""
    import shutil
    for root, _dirs, files in os.walk(db_dir):
        if os.path.abspath(root) == os.path.abspath(db_dir):
            continue
        for f in files:
            src = os.path.join(root, f)
            dst = os.path.join(db_dir, f)
            if not os.path.exists(dst):
                shutil.move(src, dst)
    # Remove now-empty subdirectories (deepest first)
    for root, dirs, _files in os.walk(db_dir, topdown=False):
        if os.path.abspath(root) == os.path.abspath(db_dir):
            continue
        try:
            os.rmdir(root)
        except OSError:
            pass


def download_stream(db_dir, key, url=None):
    """Generator yielding progress dicts while downloading + extracting a DB."""
    os.makedirs(str(db_dir), exist_ok=True)
    meta = DATABASES.get(key)
    if url:
        target_url = url
    elif meta:
        # Find a SourceForge host/mirror that actually serves the zip
        target_url = _resolve_url(meta["file"])
        if not target_url:
            yield {"type": "error", "message":
                   "Could not reach a working SourceForge mirror for this database. "
                   "Check the container's internet access, or paste a direct mirror URL."}
            return
    else:
        yield {"type": "error", "message": f"Unknown database: {key}"}
        return

    yield {"type": "start", "db": key}
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        with _open(target_url) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            read = 0
            last_emit = 0
            chunk = 1024 * 256
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                tmp.write(buf)
                read += len(buf)
                # Throttle progress events to ~ every 4 MB
                if read - last_emit >= 4 * 1024 * 1024:
                    last_emit = read
                    yield {
                        "type": "progress", "phase": "download",
                        "mb": round(read / 1024 / 1024, 1),
                        "total_mb": round(total / 1024 / 1024, 1) if total else None,
                        "pct": round(read / total * 100, 1) if total else None,
                    }
        tmp.close()

        # Guard against HTML interstitials / mirror errors masquerading as the file
        if not zipfile.is_zipfile(tmp.name):
            with open(tmp.name, "rb") as fh:
                head = fh.read(256).lower()
            if b"<html" in head or b"<!doctype" in head:
                yield {"type": "error", "message":
                       "Got an HTML page instead of the zip (SourceForge mirror issue). "
                       "Try again, or paste a direct mirror URL."}
            else:
                yield {"type": "error", "message": "Downloaded file is not a valid zip."}
            return

        yield {"type": "progress", "phase": "extract"}
        with zipfile.ZipFile(tmp.name) as z:
            z.extractall(str(db_dir))
        # ASTAP's -d expects DB files directly in the dir; flatten any subfolder
        _flatten(str(db_dir))
        open(os.path.join(str(db_dir), f".installed_{key}"), "w").close()
        yield {"type": "done", "db": key, "ready": db_ready(db_dir)}
    except Exception as e:
        yield {"type": "error", "message": str(e)}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
