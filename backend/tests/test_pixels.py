"""
Pixel peeping (100% viewer): the full-resolution stretch, raw-mosaic balance, the frame
cache and the /pixels and /tile endpoints.
"""
import io
import os
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pixels  # noqa: E402


def _sky(h=300, w=400, seed=0):
    """Noisy sky around 1000 ADU with one bright, unsaturated star at (200, 150)."""
    rng = np.random.default_rng(seed)
    img = rng.normal(1000.0, 20.0, (h, w))
    yy, xx = np.mgrid[0:h, 0:w]
    img += 30000.0 * np.exp(-((xx - 200) ** 2 + (yy - 150) ** 2) / (2 * 2.0 ** 2))
    return img.astype(np.float32)


def test_stretch_lifts_the_sky_and_keeps_star_cores_unclipped():
    img = _sky()
    out = pixels.stretch(img)
    assert out.dtype == np.uint8 and out.shape == img.shape
    assert 55 < np.median(out) < 75                         # sky median near 25% grey
    assert out[150, 200] == 255                             # the peak is the white point…
    assert out[150, 203] < 255                              # …and the star's wings aren't clipped
    holes = img.copy()
    holes[:10] = np.nan
    assert pixels.stretch(holes)[0, 0] == 0
    assert not pixels.stretch(np.full((10, 10), 5.0, dtype=np.float32)).any()   # flat frame


def test_raw_mosaic_channels_are_balanced():
    img = _sky(seed=1)
    response = np.array([[0.5, 1.0], [1.0, 1.6]])          # R G / G B photosites
    mosaic = img.copy()
    for dy in (0, 1):
        for dx in (0, 1):
            mosaic[dy::2, dx::2] *= response[dy, dx]

    def spread(out):
        medians = [np.median(out[dy::2, dx::2]) for dy in (0, 1) for dx in (0, 1)]
        return max(medians) - min(medians)

    assert spread(pixels.render(mosaic, cfa="RGGB")) <= 2   # no checkerboard in the sky
    assert spread(pixels.render(mosaic)) > 20               # what an unbalanced mosaic looks like


def test_colour_frames_and_png_tiles():
    from PIL import Image
    rgb = pixels.render(np.stack([_sky(seed=s) for s in range(3)]))      # (3, H, W) input
    assert rgb.shape == (300, 400, 3)
    tile = Image.open(io.BytesIO(pixels.png(rgb[280:, 390:])))
    assert tile.size == (10, 20) and tile.mode == "RGB"
    assert Image.open(io.BytesIO(pixels.png(pixels.render(_sky())[:16, :16]))).mode == "L"


def test_frame_cache_builds_once_and_drops_least_recent():
    cache = pixels.FrameCache()
    calls = []

    def build(key):
        def run():
            calls.append(key)
            time.sleep(0.05)
            return np.zeros((100, 100), dtype=np.uint8)     # 10 kB each
        return run

    threads = [threading.Thread(target=cache.get, args=("a", build("a"), 25_000)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["a"]                                   # a burst of tile requests reads the file once
    cache.get("b", build("b"), 25_000)
    cache.get("a", build("a"), 25_000)                      # hit: now the most recent
    cache.get("c", build("c"), 25_000)                      # over the limit, so "b" goes
    cache.get("a", build("a"), 25_000)
    cache.get("b", build("b"), 25_000)
    assert calls == ["a", "b", "c", "b"]

    def offline():
        raise OSError("source offline")

    with pytest.raises(OSError):
        cache.get("x", offline, 25_000)
    cache.get("x", build("x"), 25_000)                      # a failed read doesn't stick
    assert calls[-1] == "x"


def test_pixels_and_tile_endpoints(tmp_path, monkeypatch):
    from astropy.io import fits
    from fastapi.testclient import TestClient
    from PIL import Image
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "database", "main", "public_api"):
        sys.modules.pop(m, None)
    import database as d
    import main
    d.create_tables()
    client = TestClient(main.app)

    src, raw = tmp_path / "frame.fits", tmp_path / "osc.fits"
    fits.PrimaryHDU(_sky().astype(np.uint16)).writeto(src)
    fits.PrimaryHDU(_sky(seed=2).astype(np.uint16)).writeto(raw)
    s = d.SessionLocal()
    mono = d.FitsFile(path=str(src), filename="frame.fits", file_mtime=src.stat().st_mtime)
    osc = d.FitsFile(path=str(raw), filename="osc.fits", file_mtime=raw.stat().st_mtime, bayer="RGGB")
    gone = d.FitsFile(path=str(tmp_path / "missing.fits"), filename="missing.fits")
    s.add_all([mono, osc, gone])
    s.commit()
    ids = {"mono": mono.id, "osc": osc.id, "gone": gone.id}
    s.close()

    assert client.get(f"/api/files/{ids['mono']}/pixels").json() == {"width": 400, "height": 300, "channels": 1,
                                                                    "cfa": None}
    assert client.get(f"/api/files/{ids['osc']}/pixels").json()["cfa"] == "RGGB"
    r = client.get(f"/api/files/{ids['mono']}/tile", params={"x": 384, "y": 0, "w": 64, "h": 32})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (16, 32)                   # clipped at the frame edge
    assert client.get(f"/api/files/{ids['mono']}/tile", params={"x": 400}).status_code == 404
    assert client.get(f"/api/files/{ids['mono']}/tile", params={"w": 4096}).status_code == 422
    assert client.get(f"/api/files/{ids['gone']}/pixels").status_code == 404   # source offline
    assert client.get("/api/files/999999/tile").status_code == 404
