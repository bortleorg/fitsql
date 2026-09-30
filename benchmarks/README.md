# Benchmarks

Standalone, versioned performance tracking — runs the real indexing pipeline over
a curated FITS dataset, no Docker/Redis/DB.

## Curate a dataset
Drop representative FITS into `benchmarks/dataset/` (gitignored — frames are large).
Use a stable, mixed set (filters, sizes, crowding) so runs are comparable over time.

## Run
```bash
python backend/bench_cli.py                       # float32 vs float64, 1 repeat
python backend/bench_cli.py --repeats 3
python backend/bench_cli.py --dataset /other/dir
python backend/bench_cli.py --dtypes float32      # float32 only
```

Each run writes `benchmarks/results/<timestamp>_<machine>.json` (full aggregate +
machine/lib/commit env) and appends a row to `HISTORY.md`. Commit those to track
performance across commits and machines. Numbers aren't apples-to-apples across
machines — the env block records cpu/ram/versions so you can read them in context.
