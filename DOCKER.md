# Running fitsql in Docker (Unraid)

Three services from one image:

- **web** — FastAPI + the built React UI (host **8400** → container 8000)
- **worker** — does the heavy lifting (FITS read, thumbnails, SEP quality, ASTAP
  solve). Scales horizontally; default **2 replicas**.
- **redis** — the job queue between web and workers.

You provide:

1. A **data volume** (`/config`) for persistent state: the SQLite database,
   thumbnails, and the downloaded ASTAP star database. Mounted to **both** web
   and worker.
2. Your **astrophotography library**, mounted read-only to both, which you index.

Heavy work runs in the workers, so the UI stays responsive. Scan/analyze/solve
buttons enqueue jobs; the Settings page shows a live queue with throughput + ETA
and a **Flush** button.

---

## Quick start (docker compose)

```bash
docker compose up -d --build          # starts redis + web + 2 workers
docker compose up -d --scale worker=4 # run more workers
```

Then open `http://<host>:8400`. Edit `docker-compose.yml` to point the media
mount at your library.

---

## Unraid setup (manual container)

**Add Container** → fill in:

| Field | Value |
|---|---|
| Repository | your built image (e.g. `ghcr.io/youruser/fitsql`) or build locally |
| Network Type | Bridge |
| Port | `8400` (host) → `8000` (container) |

**Paths:**

| Container path | Host path | Mode | Purpose |
|---|---|---|---|
| `/config` | `/mnt/user/appdata/fitsql` | Read/Write | DB, thumbnails, star DB |
| `/mnt/astro` | `/mnt/user/astro` (your library) | **Read Only** | FITS files to index |

You can add several library mounts (`/mnt/astro2`, …) — add each as a Watched
Path in the UI.

**Environment variables (all optional):**

| Variable | Example | Purpose |
|---|---|---|
| `WATCH_PATHS` | `/mnt/astro` | Auto-add watched paths on first boot (comma-separated) |
| `SITE_LAT` | `37.5` | Default site latitude (° N+) for altitude/moon when a header lacks it |
| `SITE_LON` | `-122.0` | Default site longitude (° E+) |
| `SITE_ELEV` | `100` | Default site elevation (m) |
| `EXPORT_MAP_FROM` | `/mnt/astro` | Container path prefix to rewrite on export |
| `EXPORT_MAP_TO` | `P:\astro` | Path prefix PixInsight sees (also converts `/`→`\`) |
| `REDIS_URL` | `redis://redis:6379/0` | Job queue connection (web + workers) |
| `WORKER_CONCURRENCY` | `2` | Threads per worker container (scale containers for more) |
| `BROWSE_ROOTS` | `/mnt` | Roots the folder picker may browse |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose diagnostics; slow queries/requests always warn |
| `PUBLIC_API_KEYS` | `key1,key2` | Require one of these keys on the read-only API at `/api/v1` (unset = open) |

All env values can also be set later in the **Settings** page (except `PUBLIC_API_KEYS`).

> **Public API:** read-only, documented at `http://<host>:8400/api/v1/docs`
> (OpenAPI JSON at `/api/v1/openapi.json`). `PUBLIC_API_KEYS` only protects `/api/v1`
> — the UI's own `/api/*` endpoints (scan, reset, settings) have no auth, so don't
> expose the web port to the internet directly; put a reverse proxy in front that
> only forwards `/api/v1/`.

> **Scaling workers:** `docker compose up -d --scale worker=N`. Each worker runs
> `WORKER_CONCURRENCY` threads, so total parallelism ≈ `N × WORKER_CONCURRENCY`.
> All workers and the web share the SQLite catalog (WAL); writers serialise.

---

## First run

1. **Settings → Watched Paths**: add `/mnt/astro` (or set `WATCH_PATHS`).
2. **Settings → Scan All Paths**: index your library (streamed progress).
3. **Settings → Plate Solving → Star database**: download one (saved to
   `/config`, survives container updates):
   - **D50** (~900 MB) — recommended default; works narrow → wide.
   - **D20** (~400 MB) — smaller, medium/wide fields.
   - **D05** (~100 MB) — compact, wide/medium fields.
   - **W08** (~1 MB) — very wide fields only (mag 8).
   ASTAP itself is already bundled; you only need the star database.
4. **Settings → Plate solve unsolved lights** — solves frames whose headers
   lack a WCS. Frames already solved by your capture software are read for free.
5. **Settings → Export Path Mapping**: set `from` = `/mnt/astro`, `to` = the
   path PixInsight uses (e.g. `P:\astro`) so exported LIGHT lists are valid on
   your imaging PC.
6. **Selector**: build an approval expression, then **Export approved** → drop
   the `.txt` into PixInsight WBPP via *Add Files*.

---

## Notes

- **ASTAP binary**: bundled at build time from the upstream CLI zip. If the
  build fails at that step (upstream URL changed), override it:
  ```bash
  docker build --build-arg ASTAP_CLI_URL=<new-url> -t fitsql .
  ```
- **Originals are never modified** — plate solving writes to a temp dir and
  parses the result.
- **Permissions**: the container writes only to `/config`; the library mount is
  read-only. Ensure `/config` is writable by the container.
- **Backups**: everything persistent is under `/config`. Back up that folder.
