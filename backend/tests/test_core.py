"""
Unit tests for the pure logic that has bitten us before — the expression engine,
header normalization, coordinate math, WCS parsing, benchmark aggregation, and the
selector's reference/facet/export helpers. No DB, Redis, or network needed.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import expression as ex        # noqa: E402
import scanner                 # noqa: E402
import wcs_util                # noqa: E402
import benchmark               # noqa: E402


class Row:
    """Minimal stand-in for a FitsFile row; unknown attrs read as None."""
    def __init__(self, **k):
        self._d = k

    def __getattr__(self, n):
        return self._d.get(n)


# ── expression engine ─────────────────────────────────────────────────────────
def _vars(n, **cols):
    return {k: np.array(v, dtype=float) for k, v in cols.items()}, n


def test_expr_basic_and_stats():
    v, n = _vars(5, hfr=[2.0, 2.5, 8.0, 2.2, 3.0])
    assert ex.evaluate("hfr < 3", v, n).tolist() == [True, True, False, True, False]
    # median(2,2.2,2.5,3,8)=2.5, mad=0.3 -> < 2.5+2*0.3=3.1
    assert ex.evaluate("hfr < median(hfr) + 2*mad(hfr)", v, n).tolist() == [True, True, False, True, True]


def test_expr_nan_is_rejected():
    v, n = _vars(3, hfr=[2.0, np.nan, 2.5])
    assert ex.evaluate("hfr < 3", v, n).tolist() == [True, False, True]


def test_expr_operators_and_chained():
    v, n = _vars(3, a=[1, 5, 9], b=[10, 10, 10])
    assert ex.evaluate("a > 2 && a < 8", v, n).tolist() == [False, True, False]
    assert ex.evaluate("a > 8 || a < 2", v, n).tolist() == [True, False, True]
    assert ex.evaluate("2 < a < 8", v, n).tolist() == [False, True, False]


def test_expr_not_leading():
    v = {"rejected": np.array([False, True, False])}
    assert ex.evaluate("!rejected", v, 3).tolist() == [True, False, True]


def test_expr_string_equality_and_bools():
    v = {"filter": np.array(["h", "o", "h"], dtype=object),
         "mono": np.array([True, True, False])}
    assert ex.evaluate("filter == 'h'", v, 3).tolist() == [True, False, True]
    assert ex.evaluate("mono && filter == 'h'", v, 3).tolist() == [True, False, False]


def test_expr_isnan_missing_present():
    v = {"astap_ra": np.array([10.0, np.nan, 50.0])}
    assert ex.evaluate("isnan(astap_ra)", v, 3).tolist() == [False, True, False]
    assert ex.evaluate("missing(astap_ra)", v, 3).tolist() == [False, True, False]
    assert ex.evaluate("present(astap_ra)", v, 3).tolist() == [True, False, True]


def test_expr_like_and_not_like():
    v = {"path": np.array(["/a/m31/reject/1.fits", "/a/m31/good/2.fits", "/a/m33/3.fits"], dtype=object),
         "hfr": np.array([2.0, 2.0, 9.0])}
    assert ex.evaluate("path like '%reject%'", v, 3).tolist() == [True, False, False]
    assert ex.evaluate("path not like '%reject%'", v, 3).tolist() == [False, True, True]
    assert ex.evaluate("like(path, '%/M3_/%')", v, 3).tolist() == [True, True, True]   # _ = one char, case-insensitive
    assert ex.evaluate("hfr < 3 && path NOT LIKE '%reject%'", v, 3).tolist() == [False, True, False]
    assert ex.evaluate("!(path like '%m33%')", v, 3).tolist() == [True, True, False]
    # operator rewriting must not touch string literals
    v2 = {"path": np.array(["/x/a!b&&c.fits", "/x/ab.fits"], dtype=object)}
    assert ex.evaluate("path like '%!b&&%'", v2, 2).tolist() == [True, False]
    with pytest.raises(ex.ExpressionError):
        ex.evaluate("like(path, 3)", v, 3)


def test_expr_between():
    v, n = _vars(4, hfr=[1.0, 2.0, 3.0, np.nan])
    assert ex.evaluate("hfr between 2 and 3", v, n).tolist() == [False, True, True, False]
    assert ex.evaluate("hfr BETWEEN 2 && 3", v, n).tolist() == [False, True, True, False]
    # missing values are rejected by NOT BETWEEN too (SQL NULL semantics)
    assert ex.evaluate("hfr not between 2 and 3", v, n).tolist() == [True, False, False, False]
    assert ex.evaluate("hfr between -1 and 1.5e0 and hfr > 0", v, n).tolist() == [True, False, False, False]
    assert ex.evaluate("between(hfr, 1.5, median(hfr))", v, n).tolist() == [False, True, False, False]


def test_expr_dates():
    v = {"date": np.array(["2026-05-13T04:00:00", "2026-05-31T23:10:00", "NaT"], dtype="datetime64[ms]")}
    ev = lambda e: ex.evaluate(e, v, 3).tolist()
    assert ev("date == '2026-05-13'") == [True, False, False]                    # whole day
    assert ev("date between '2026-05-01' and '2026-05-31'") == [True, True, False]  # inclusive end day
    assert ev("date between '2026-05-14' and '2026-05'") == [False, True, False]   # month precision
    assert ev("date not between '2026-05-14' and '2026-05'") == [True, False, False]
    assert ev("date >= '2026-05-31 23:10' && '2026-05-31t23:05' < date") == [False, True, False]
    assert ev("'2026-05-01' <= date <= '2026-05-13'") == [True, False, False]
    assert ev("missing(date)") == [False, False, True]
    with pytest.raises(ex.ExpressionError):
        ex.evaluate("date < 'last tuesday'", v, 3)


@pytest.mark.parametrize("bad", [
    "__import__('os').system('x')",
    "hfr.__class__",
    "open('x')",
    "[x for x in range(3)]",
    "lambda: 1",
])
def test_expr_blocks_code_injection(bad):
    with pytest.raises(ex.ExpressionError):
        ex.evaluate(bad, {"hfr": np.array([1.0])}, 1)


def test_expr_unknown_variable():
    with pytest.raises(ex.ExpressionError):
        ex.evaluate("nope > 1", {}, 0)


# ── normalization ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("LIGHT", "Light Frame"), ("Light Frame", "Light Frame"), ("science", "Light Frame"),
    ("dark", "Dark Frame"), ("FLAT", "Flat Frame"), ("bias", "Bias Frame"),
])
def test_normalize_imagetyp(raw, want):
    assert scanner._normalize_imagetyp(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("L", "L"), ("Luminance", "L"), ("Lum", "L"),
    ("R", "R"), ("Red", "R"), ("Green", "G"), ("Blue", "B"),
    ("H", "Ha"), ("Ha", "Ha"), ("Halpha", "Ha"),
    ("O", "OIII"), ("OIII", "OIII"), ("S", "SII"), ("SII", "SII"),
    ("4", "4"), ("7", "7"), ("V", "V"), ("", None),
])
def test_normalize_filter(raw, want):
    assert scanner._normalize_filter(raw) == want


# ── coordinate math ───────────────────────────────────────────────────────────
def test_coord_sep_arcmin():
    assert scanner.coord_sep_arcmin(10.0, 41.0, 10.0, 41.0) == 0.0
    # 0.05 deg dec offset = 3 arcmin
    assert scanner.coord_sep_arcmin(10.0, 41.0, 10.0, 41.05) == pytest.approx(3.0, abs=0.01)
    assert scanner.coord_sep_arcmin(10.0, 41.0, None, None) is None


def test_quality_score_monotonic():
    sharp = scanner.quality_score(1.5, 3.0, 0.3, 500)
    soft = scanner.quality_score(5.5, 11.0, 0.7, 100)
    assert sharp > soft
    assert scanner.quality_score(None, None, None, None) is None


def test_quality_score_caps_trailed_frames(monkeypatch):
    import config
    monkeypatch.setattr(config, "TRAIL_THRESHOLD", 0.03)
    monkeypatch.setattr(config, "TRAIL_QUALITY_CAP", 25.0)
    sharp = scanner.quality_score(1.5, 3.0, 0.3, 500)
    assert scanner.quality_score(1.5, 3.0, 0.3, 500, trail=0.0) == sharp
    assert scanner.quality_score(1.5, 3.0, 0.3, 500, trail=None) == sharp    # not analyzed: no penalty
    assert scanner.quality_score(1.5, 3.0, 0.3, 500, trail=0.2) == 25.0
    soft = scanner.quality_score(5.5, 11.0, 0.7, 100)
    assert scanner.quality_score(5.5, 11.0, 0.7, 100, trail=0.2) == soft     # already below cap
    assert scanner.quality_score(None, None, None, None, trail=0.9) is None


def test_trail_score_separates_trailed_from_round_and_coma():
    rng = np.random.default_rng(0)
    n = 200
    # Round stars: mild elongation, random orientation
    a = rng.uniform(2.0, 2.4, n); b = a / rng.uniform(1.0, 1.3, n)
    assert scanner.trail_score(a, b, rng.uniform(-np.pi / 2, np.pi / 2, n)) < 0.05
    # Trailed: strongly elongated along one axis (θ wraps at ±π/2 — same axis)
    a = rng.uniform(4, 8, n); b = a / rng.uniform(2.5, 6, n)
    theta = np.where(rng.random(n) < 0.5, np.pi / 2 - 0.05, -np.pi / 2 + 0.05)
    assert scanner.trail_score(a, b, theta) > 0.9
    # Coma/tilt: just as elongated, but directions spread across the field
    assert scanner.trail_score(a, b, rng.uniform(-np.pi / 2, np.pi / 2, n)) < 0.1
    assert scanner.trail_score([2, 2], [2, 2], [0, 0]) is None   # too few sources


def _star_field(trail_px=0, n=80, size=600, seed=1):
    """Synthetic frame: Gaussian stars on noise, optionally smeared along 30°."""
    rng = np.random.default_rng(seed)
    img = rng.normal(1000, 10, (size, size)).astype(np.float32)
    steps = max(1, trail_px)
    for _ in range(n):
        x0, y0 = rng.uniform(40, size - 60, 2)
        amp = rng.uniform(2000, 6000)
        for k in range(steps):
            cx, cy = x0 + k * np.cos(np.radians(30)), y0 + k * np.sin(np.radians(30))
            xs, ys = np.meshgrid(np.arange(int(cx) - 8, int(cx) + 9), np.arange(int(cy) - 8, int(cy) + 9))
            img[ys, xs] += (amp / steps) * np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * 1.5 ** 2))
    return img


def test_analyze_stars_trail_score_on_synthetic_frames():
    pytest.importorskip("sep")
    round_m = scanner.analyze_stars(_star_field(trail_px=0))
    trailed_m = scanner.analyze_stars(_star_field(trail_px=25))
    assert round_m["star_count"] > 40 and trailed_m["star_count"] > 40
    assert round_m["trail_score"] < 0.05
    assert trailed_m["trail_score"] > 0.5


def test_streak_fragments_are_not_trailing():
    rng = np.random.default_rng(3)
    # Round stars over a 6000×4000 field plus a satellite streak SEP deblended into
    # 20 long thin fragments end to end along one line
    n, k = 180, 20
    ang = np.arctan2(1, 2)
    along = np.arange(k) * 300.0
    a = np.concatenate([rng.uniform(2.0, 2.4, n), np.full(k, 150.0)])
    b = np.concatenate([a[:n] / rng.uniform(1.0, 1.3, n), np.full(k, 1.5)])
    theta = np.concatenate([rng.uniform(-np.pi / 2, np.pi / 2, n), np.full(k, ang)])
    x = np.concatenate([rng.uniform(0, 6000, n), 200 + along * np.cos(ang)])
    y = np.concatenate([rng.uniform(0, 4000, n), 500 + along * np.sin(ang)])
    assert scanner.streak_fragments(a, b, theta, x, y).tolist() == [False] * n + [True] * k
    assert scanner.trail_score(a, b, theta) > 0.05                 # positions unknown: reads as trailing
    assert scanner.trail_score(a, b, theta, x, y) < 0.01
    # Real trailing: short smears scattered over the field don't chain, even crowded
    # onto a small sensor
    m = 200
    a = rng.uniform(4, 8, m); b = a / rng.uniform(2.5, 6, m)
    theta = 0.3 + rng.normal(0, 0.03, m)
    assert scanner.streak_fragments(a, b, theta, rng.uniform(0, 6000, m), rng.uniform(0, 4000, m)).sum() == 0
    assert scanner.streak_fragments(a, b, theta, rng.uniform(0, 1920, m), rng.uniform(0, 1080, m)).mean() < 0.1
    # Guiding wander: a bright star deblended into a row of parallel dashes tilted 37°
    # while the row runs horizontally — tracking failure, not a streak
    row = np.arange(10)
    a = np.concatenate([rng.uniform(2.0, 2.4, n), np.full(10, 3.8)])
    b = np.concatenate([a[:n] / rng.uniform(1.0, 1.3, n), np.full(10, 1.7)])
    theta = np.concatenate([rng.uniform(-np.pi / 2, np.pi / 2, n), np.full(10, np.radians(37))])
    x = np.concatenate([rng.uniform(0, 6000, n), 2860 + 15.5 * row])
    y = np.concatenate([rng.uniform(0, 4000, n), np.full(10, 4805.0)])
    assert not scanner.streak_fragments(a, b, theta, x, y)[n:].any()


def _dashed_streak(img, x0, y0, deg, dash=60, gap=20, amp=400.0):
    """Satellite trail SEP detects in pieces: bright dashes along one line."""
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    ys, xs = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    along = (xs - x0) * c + (ys - y0) * s
    across = (ys - y0) * c - (xs - x0) * s
    on = (along >= 0) & (np.mod(along, dash + gap) < dash)
    return img + (amp * np.exp(-across ** 2 / (2 * 1.2 ** 2)) * on).astype(np.float32)


def test_analyze_stars_ignores_satellite_streak(monkeypatch):
    pytest.importorskip("sep")
    import config
    img = _dashed_streak(_star_field(trail_px=0), 20, 60, 35)
    assert scanner.analyze_stars(img)["trail_score"] < config.TRAIL_THRESHOLD
    monkeypatch.setattr(config, "TRAIL_LINE_MIN", 10 ** 6)       # chain filter off: the dashes read as trailing
    assert scanner.analyze_stars(img)["trail_score"] > config.TRAIL_THRESHOLD


def _psf_frame(size=600, n=120, sigma=1.5, bright=0, seed=5):
    """Gaussian stars (σ px) on noise, clipped at 16 bits; the first `bright` saturate."""
    rng = np.random.default_rng(seed)
    img = rng.normal(1000, 10, (size, size))
    for k in range(n):
        x0, y0 = rng.uniform(30, size - 30, 2)
        amp = 400000 if k < bright else rng.uniform(2000, 8000)
        xs, ys = np.meshgrid(np.arange(int(x0) - 12, int(x0) + 13), np.arange(int(y0) - 12, int(y0) + 13))
        img[ys, xs] += amp * np.exp(-((xs - x0) ** 2 + (ys - y0) ** 2) / (2 * sigma ** 2))
    return np.clip(img, 0, 65535).astype(np.float32)


def test_star_codes_classify_edge_saturated_blended():
    src = np.zeros(4, dtype=[("x", float), ("y", float), ("peak", float), ("flag", int)])
    src["x"] = [100, 5, 100, 100]
    src["y"] = [100, 100, 100, 100]
    src["peak"] = [1000, 1000, 64000, 1000]
    src["flag"] = [0, 0, 0, 1]                      # 1 = sep.OBJ_MERGED
    codes = scanner.star_codes(src, 400, 400, sat_level=65535, back_level=1000)
    assert codes.tolist() == [scanner.STAR_CLEAN, scanner.STAR_EDGE,
                              scanner.STAR_SATURATED, scanner.STAR_BLENDED]


def test_saturated_stars_left_out_of_shape_metrics(monkeypatch):
    pytest.importorskip("sep")
    import config
    img = _psf_frame(bright=60)
    clean = scanner.analyze_stars(img)
    monkeypatch.setattr(config, "STAR_SAT_FRACTION", 10.0)     # nothing counts as saturated
    naive = scanner.analyze_stars(img)
    assert clean["diagnostics"]["saturated"] >= 40
    assert naive["diagnostics"]["saturated"] == 0
    assert clean["star_count"] == naive["star_count"]          # detection itself unchanged
    assert clean["median_fwhm"] < naive["median_fwhm"]
    assert 2.4 < clean["median_fwhm"] < 4.2                    # true FWHM = 2.3548 × 1.5 ≈ 3.5 px
    assert clean["psf_fwhm"] == pytest.approx(3.53, rel=0.1)   # the profile fit lands closer
    assert all(s[5] != scanner.STAR_SATURATED for s in clean["diagnostics"]["stars"] if s[6])


def test_grid_metrics_flag_empty_columns_and_sparse_fields():
    rng = np.random.default_rng(3)
    w, h = 800, 600
    x, y = rng.uniform(0, w, 2000), rng.uniform(0, h, 2000)
    back = np.full((h, w), 1000.0)
    even = scanner.grid_metrics(x, y, back, 10.0, w, h)
    assert even["empty_cells"] == 0.0
    assert even["bg_spread"] == pytest.approx(0.0, abs=1e-6)
    # Tree line: nothing detected in the left quarter, and that sky is brighter
    keep = x > w / 4
    lit = back.copy()
    lit[:, : w // 4] += 80.0
    blocked = scanner.grid_metrics(x[keep], y[keep], lit, 10.0, w, h)
    assert blocked["empty_cells"] == pytest.approx(0.25)       # 2 of 8 columns
    assert len(blocked["cells"]["empty_cluster"]) == 12
    assert blocked["bg_spread"] == pytest.approx(8.0)          # 80 ADU / RMS 10
    assert len(blocked["cells"]["cell_stars"]) == 48
    # Scattered empty cells (noise, vignetted corners) aren't an occlusion
    cell = (y * 6 / h).astype(int) * 8 + (x * 8 / w).astype(int)
    holes = ~np.isin(cell, [0, 7, 19, 40, 47])                 # four corners + one interior cell
    scattered = scanner.grid_metrics(x[holes], y[holes], back, 10.0, w, h)
    assert scattered["empty_cells"] == pytest.approx(1 / 48)
    # Too few stars to say anything
    assert scanner.grid_metrics(x[:20], y[:20], back, 10.0, w, h)["empty_cells"] is None


def test_analyze_stars_empty_cells_on_occluded_frame():
    pytest.importorskip("sep")
    full = scanner.analyze_stars(_psf_frame(size=800, n=1500, seed=7))
    img = _psf_frame(size=800, n=1500, seed=7)
    img[:, :200] = np.random.default_rng(8).normal(1000, 10, (800, 200))   # left quarter blocked
    blocked = scanner.analyze_stars(img)
    assert full["empty_cells"] == 0.0
    assert blocked["empty_cells"] >= 0.2
    d = blocked["diagnostics"]
    assert d["width"] == 800 and len(d["cell_stars"]) == d["grid"][0] * d["grid"][1]


def _raw_sky(w=1800, h=1200, seed=11):
    """Raw-light-like sky: gradient, vignetting, noise (σ 25) and stars."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    r2 = ((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / (w / 2) ** 2
    img = (1100.0 + 0.04 * xx) * (1.0 - 0.25 * r2) + rng.normal(0, 25, (h, w))
    for _ in range(600):
        x0, y0 = rng.uniform(10, w - 10), rng.uniform(10, h - 10)
        gy, gx = np.mgrid[int(y0) - 6:int(y0) + 7, int(x0) - 6:int(x0) + 7]
        img[gy, gx] += rng.uniform(300, 20000) * np.exp(-((gx - x0) ** 2 + (gy - y0) ** 2) / (2 * 1.8 ** 2))
    return img, xx, yy


def test_background_dip_finds_frost_shadow_and_droplets():
    img, xx, yy = _raw_sky()
    blob = np.exp(-((xx - 900) / 300.0) ** 2 - ((yy - 560) / 180.0) ** 2)
    assert scanner.background_dip(img.astype(np.float32), 25.0)[0] < 0.01    # vignetting + gradient fitted away

    # Smooth shadow (frost well in front of focus), 5% at its centre
    depth, dip = scanner.background_dip((img * (1 - 0.05 * blob)).astype(np.float32), 25.0)
    assert 0.035 < depth < 0.06
    assert abs(dip["x"] - 900) < 100 and abs(dip["y"] - 560) < 80
    x0, y0, x1, y1 = dip["box"]
    assert x0 < 900 < x1 and y0 < 560 < y1 and 200 < x1 - x0 < 900

    # Droplets on the cover glass: tiny dark specks a median / mode background clips away
    specks = img.copy()
    specks[np.random.default_rng(12).random(img.shape) < 0.25 * blob] *= 0.6
    assert scanner.background_dip(specks.astype(np.float32), 25.0)[0] > 0.05


def test_background_dip_ignores_nebula_edge_gradients_and_unjudgeable_frames():
    img, xx, yy = _raw_sky(seed=13)
    nebula = img + 350.0 * np.exp(-((xx - 500) / 160.0) ** 2 - ((yy - 450) / 120.0) ** 2)
    assert scanner.background_dip(nebula.astype(np.float32), 25.0)[0] < 0.012
    # Cloud / twilight darkening a corner runs off the frame edge: not a shadow on the optics
    corner = img * (1 - 0.08 * np.exp(-((xx - 1800) / 450.0) ** 2 - (yy / 350.0) ** 2))
    assert scanner.background_dip(corner.astype(np.float32), 25.0)[0] < 0.015
    subtracted = img - np.median(img)                     # background-extracted: no level to compare with
    assert scanner.background_dip(subtracted.astype(np.float32), 25.0) == (None, None)
    assert scanner.background_dip(img[:100, :100].astype(np.float32), 25.0) == (None, None)


def test_analyze_stars_reports_frost_shadow():
    pytest.importorskip("sep")
    img = _psf_frame(size=800, n=800, seed=9)
    yy, xx = np.mgrid[0:800, 0:800]
    shade = 1 - 0.06 * np.exp(-((xx - 420) / 130.0) ** 2 - ((yy - 380) / 90.0) ** 2)
    clean = scanner.analyze_stars(img)
    shaded = scanner.analyze_stars((img * shade).astype(np.float32))
    assert clean["bg_dip"] < 0.01
    assert 0.04 < shaded["bg_dip"] < 0.07
    assert shaded["star_count"] == pytest.approx(clean["star_count"], rel=0.02)   # star counts barely move
    d = shaded["diagnostics"]
    assert d["v"] == scanner.DIAGNOSTICS_VERSION
    x0, y0, x1, y1 = d["dip"]["box"]
    assert x0 < 420 < x1 and y0 < 380 < y1


def test_in_sensor_pixels_maps_dip_overlay():
    m = scanner.in_sensor_pixels({"bg_dip": 0.05, "diagnostics": {"dip": {"depth": 0.05, "x": 100, "y": 50,
                                                                          "box": [80, 40, 120, 60]}}}, 2, (400, 600))
    assert m["bg_dip"] == 0.05
    assert m["diagnostics"]["dip"] == {"depth": 0.05, "x": 200, "y": 100, "box": [160, 80, 240, 120]}
    none = {"depth": 0.0, "x": None, "y": None, "box": None}
    assert scanner.in_sensor_pixels({"diagnostics": {"dip": dict(none)}}, 2, (400, 600))["diagnostics"]["dip"] == none


def test_tile_windows_cover_frame_with_overlap():
    whole = ((0, 500, 0, 700), (0, 500, 0, 700))
    assert scanner.tile_windows(500, 700, 0, 64) == [whole]               # tiling off
    assert scanner.tile_windows(500, 700, 1024, 64) == [whole]            # fits in one tile
    tiles = scanner.tile_windows(1000, 2100, 1024, 64)
    assert len(tiles) == 3                                                # one row, three even columns
    cover = np.zeros((1000, 2100), dtype=int)
    for (y0, y1, x0, x1), (ya, yb, xa, xb) in tiles:
        assert y1 - y0 <= 1024 and x1 - x0 <= 1024
        assert (ya, yb, xa, xb) == (max(0, y0 - 64), min(1000, y1 + 64), max(0, x0 - 64), min(2100, x1 + 64))
        cover[y0:y1, x0:x1] += 1
    assert (cover == 1).all()                                             # cores cover the frame exactly once


def test_tiled_extraction_matches_single_pass(monkeypatch):
    pytest.importorskip("sep")
    import config
    img = _psf_frame(size=900, n=1500, seed=13)
    # Extra stars centred on the tile borders (300 px cores at SEP_TILE_PX=300)
    rng = np.random.default_rng(14)
    for border in (300, 600):
        for along in np.linspace(40, 860, 12):
            for x0, y0 in ((border + rng.uniform(-1, 1), along), (along, border + rng.uniform(-1, 1))):
                xs, ys = np.meshgrid(np.arange(int(x0) - 12, int(x0) + 13), np.arange(int(y0) - 12, int(y0) + 13))
                img[ys, xs] += 5000 * np.exp(-((xs - x0) ** 2 + (ys - y0) ** 2) / (2 * 1.5 ** 2))
    monkeypatch.setattr(config, "SEP_TILE_PX", 0)
    single = scanner.analyze_stars(img)
    monkeypatch.setattr(config, "SEP_TILE_PX", 300)
    tiled = scanner.analyze_stars(img)
    assert (single["diagnostics"]["tiles"], tiled["diagnostics"]["tiles"]) == (1, 9)
    assert tiled["star_count"] == single["star_count"]                    # border stars neither lost nor doubled
    for key in ("median_fwhm", "median_hfr", "psf_fwhm", "eccentricity", "trail_score", "empty_cells", "star_flux"):
        assert tiled[key] == pytest.approx(single[key], rel=1e-4), key
    # Catalogs pair up one-to-one, shifted back to frame coordinates. Deblended children
    # share pixels SEP hands out at random, so their shapes wobble slightly with call
    # order — for those only the position is compared.
    import sep
    bkg = sep.Background(img)
    sub = img - bkg
    monkeypatch.setattr(config, "SEP_TILE_PX", 0)
    whole, n_whole = scanner.extract_sources(sub, bkg.globalrms, sep)
    monkeypatch.setattr(config, "SEP_TILE_PX", 300)
    parts, n_parts = scanner.extract_sources(sub, bkg.globalrms, sep)
    assert (n_whole, n_parts) == (1, 9) and len(parts) == len(whole)
    dist = np.hypot(whole["x"][:, None] - parts["x"][None], whole["y"][:, None] - parts["y"][None])
    nearest = dist.argmin(axis=1)
    assert (dist.min(axis=1) < 1.0).all() and len(set(nearest.tolist())) == len(whole)
    solo = (whole["flag"] & sep.OBJ_MERGED) == 0
    for f in ("x", "y", "a", "b", "theta", "flux", "peak", "flag", "xmin", "xmax", "ymin", "ymax", "xpeak", "ypeak"):
        np.testing.assert_allclose(parts[f][nearest[solo]], whole[f][solo], rtol=1e-5, atol=1e-6, err_msg=f)


# ── sequence screening ────────────────────────────────────────────────────────
_SEQ_LIMITS = dict(gap_hours=3, baseline_frames=5, star_drop=0.3, hfr_rise=0.25, empty_rise=0.15, spread_rise=1.0)


def _seq_times(n, start="2026-05-13T04:00:00", step_min=5):
    t0 = np.datetime64(start, "ms")
    return np.array([t0 + np.timedelta64(step_min * i, "m") for i in range(n)], dtype="datetime64[ms]")


def _screen(stars, hfr=None, flux=None, dip=None):
    import sequence
    n = len(stars)
    return sequence.screen(["m31"] * n, _seq_times(n), stars, hfr if hfr is not None else [2.0] * n,
                           [0.0] * n, [4.0] * n, flux=flux, dip=dip, **_SEQ_LIMITS)


def test_sequence_flags_thin_veil_by_star_flux():
    veil = _screen([500] * 10, flux=[50000.0] * 5 + [32000.0] + [50000.0] * 4)
    assert veil["seq_anomaly"].tolist() == [False] * 5 + [True] + [False] * 4
    assert veil["flux_drop"][5] == pytest.approx(0.36, abs=0.01)
    assert np.isnan(_screen([500] * 4)["flux_drop"]).all()     # no flux measured: no opinion


def test_sequence_flags_passing_cloud_soft_frame_and_slow_decline():
    steady = _screen([500, 510, 495, 505, 500, 498, 502, 507, 499, 503])
    assert not steady["seq_anomaly"].any() and steady["seq_ok"].all()

    cloud = _screen([500, 510, 495, 505, 200, 150, 480, 500, 505, 498])
    assert cloud["seq_anomaly"].tolist() == [False] * 4 + [True, True] + [False] * 4
    assert cloud["star_drop"][5] == pytest.approx(0.70, abs=0.01)

    soft = _screen([500] * 10, hfr=[2.0] * 5 + [2.8] + [2.0] * 4)
    assert soft["seq_anomaly"].tolist() == [False] * 5 + [True] + [False] * 4

    # Each step is small, but the session median remembers the good start
    slow = _screen(list(np.linspace(600, 250, 12)))
    assert slow["seq_anomaly"][-2:].all() and not slow["seq_anomaly"][:4].any()


def test_sequence_regime_change_and_unjudged_frames():
    import sequence
    # Refocus halfway: after 2×baseline flagged frames the new level becomes normal
    res = _screen([800] * 6 + [400] * 14)
    assert res["seq_anomaly"].tolist() == [False] * 6 + [True] * 10 + [False] * 4

    times = np.concatenate([_seq_times(4), _seq_times(4, start="2026-05-14T04:00:00")])
    assert [len(s) for s in sequence.sessions(["a"] * 8, times, 3)] == [4, 4]

    few = sequence.screen(["b", "b"], _seq_times(2), [500, 100], [2, 2], [0, 0], [1, 1], **_SEQ_LIMITS)
    assert not few["seq_anomaly"].any() and not few["seq_ok"].any()      # too short to judge


def test_sequence_flags_frost_forming_and_frost_at_dusk():
    # Frost grows late in the night: flagged once the dip is 1.5 points past the session
    forming = _screen([500] * 10, dip=[0.008, 0.009, 0.007, 0.008, 0.008, 0.009, 0.012, 0.02, 0.035, 0.05])
    assert forming["seq_anomaly"].tolist() == [False] * 8 + [True, True]
    assert forming["dip_rise"][9] == pytest.approx(0.041, abs=0.001)
    # Frosted while the camera was still cooling: the session median remembers clear glass
    dusk = _screen([500] * 10, dip=[0.06, 0.04, 0.03] + [0.008] * 7)
    assert dusk["seq_anomaly"].tolist() == [True] * 3 + [False] * 7
    assert np.isnan(_screen([500] * 4)["dip_rise"]).all()      # no dip measured: no opinion


def test_selector_shadow_and_setpoint_vars(monkeypatch):
    import main
    import config
    monkeypatch.setattr(config, "SHADOW_THRESHOLD", 0.025)
    monkeypatch.setattr(config, "SETPOINT_TOLERANCE_C", 1.0)
    cands = [Row(imagetyp="Light Frame", bg_dip=0.008, ccd_temp=-10.0, set_temp=-10.0),
             Row(imagetyp="Light Frame", bg_dip=0.045, ccd_temp=-7.1, set_temp=-10.0),   # frosted, cooler behind
             Row(imagetyp="Light Frame", ccd_temp=-10.0)]                                 # not analyzed, no SET-TEMP
    ev = lambda e: main._evaluate_candidates(cands, e).tolist()
    assert ev("shadow") == [False, True, False]
    assert ev("!shadow") == [True, False, True]
    assert ev("off_setpoint") == [False, True, False]
    assert ev("temp_delta > 2") == [False, True, False]
    assert ev("bg_dip < 0.02 && set_temp == -10") == [True, False, False]


def test_selector_grid_grade_and_sequence_vars(monkeypatch):
    import main
    import config
    monkeypatch.setattr(config, "OCCLUDED_THRESHOLD", 0.15)

    def light(i, stars, **kw):
        return Row(imagetyp="Light Frame", object="M 31", filter="L", instrume="cam", telescop="scope",
                   exptime=300.0, date_obs=f"2026-05-13T04:{i * 5:02d}:00", star_count=stars,
                   median_hfr=2.0, **kw)

    cands = [light(0, 500, empty_cells=0.0, rejected=0),
             light(1, 510, empty_cells=0.3, rejected=1),
             light(2, 490),                                  # grid not measured
             light(3, 150, empty_cells=0.0),                 # cloud
             light(4, 505, empty_cells=0.02)]
    ev = lambda e: main._evaluate_candidates(cands, e).tolist()
    assert ev("occluded") == [False, True, False, False, False]
    assert ev("accepted") == [True, False, False, False, False]
    assert ev("rejected") == [False, True, False, False, False]
    assert ev("seq_anomaly") == [False, True, False, True, False]
    assert ev("seq_ok") == [True, False, True, False, True]
    assert ev("star_drop > 0.5") == [False, False, False, True, False]


def test_extract_header_reads_setpoint():
    from astropy.io import fits
    f = scanner.extract_header(fits.Header({"CCD-TEMP": -7.1, "SET-TEMP": -10}))
    assert (f["ccd_temp"], f["set_temp"]) == (-7.1, -10.0)
    assert scanner.extract_header(fits.Header({"CCD-TEMP": 0.5}))["set_temp"] is None


def test_set_temp_backfill_and_reanalysis_version(tmp_path, monkeypatch):
    import json
    from sqlalchemy import text
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "database", "main"):
        sys.modules.pop(m, None)
    import database as d
    d.create_tables()
    with d.engine.begin() as conn:                        # a catalog from before the setpoint column
        conn.execute(text("ALTER TABLE fits_files DROP COLUMN set_temp"))
        conn.execute(text("INSERT INTO fits_files (path, filename, imagetyp, header_json) VALUES "
                          "('/a.fits', 'a', 'Light Frame', :a), ('/b.fits', 'b', 'Light Frame', :b)"),
                     {"a": json.dumps({"CCD-TEMP": -7.1, "SET-TEMP": -10}), "b": json.dumps({"SET-TEMP": "cold"})})
    d.create_tables()                                     # adds the column and copies SET-TEMP out of the header
    s = d.SessionLocal()
    assert {f.filename: f.set_temp for f in s.query(d.FitsFile)} == {"a": -10.0, "b": None}

    analyzed = dict(imagetyp="Light Frame", median_hfr=2.0, altitude=40.0, trail_score=0.0)
    s.add(d.FitsFile(path="/old.fits", filename="old", diagnostics_json='{"v":1,"stars":[]}', **analyzed))
    s.add(d.FitsFile(path="/new.fits", filename="new", **analyzed,
                     diagnostics_json=f'{{"v":{scanner.DIAGNOSTICS_VERSION},"stars":[]}}'))
    s.commit()
    redo = {f.filename for f in s.query(d.FitsFile).filter(scanner.needs_analysis(d.FitsFile))}
    assert redo == {"a", "b", "old"}                      # never analyzed, or by an older version
    s.close()


def test_grade_and_diagnostics_endpoints(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "database", "main"):
        sys.modules.pop(m, None)
    import database as d
    import main as m
    from fastapi import HTTPException
    d.create_tables()
    s = d.SessionLocal()
    f = d.FitsFile(path="/a.fits", filename="a", imagetyp="Light Frame", diagnostics_json='{"v":1,"stars":[]}')
    g = d.FitsFile(path="/b.fits", filename="b", imagetyp="Light Frame")
    s.add_all([f, g])
    s.commit()

    assert m.set_grade(f.id, m.GradeBody(grade="accepted"), s)["rejected"] == 0
    assert m.file_to_dict(s.get(d.FitsFile, f.id))["grade"] == "accepted"
    assert m.set_grade(f.id, m.GradeBody(grade="rejected"), s)["rejected"] == 1
    assert m.set_grade(f.id, m.GradeBody(grade=None), s)["rejected"] is None
    assert m.set_reject(f.id, m.RejectBody(rejected=True), s)["rejected"] == 1
    assert m.set_reject(f.id, m.RejectBody(rejected=False), s)["rejected"] is None   # un-reject = unmarked

    assert json.loads(m.get_diagnostics(f.id, s).body)["v"] == 1
    with pytest.raises(HTTPException):
        m.get_diagnostics(g.id, s)                                     # not analyzed yet
    s.close()


def test_fit_moffat_recovers_fwhm_and_beta():
    yy, xx = np.mgrid[0:25, 0:25]
    alpha, beta = 3.0, 2.5
    img = 5000.0 * (1 + ((xx - 12.3) ** 2 + (yy - 11.8) ** 2) / alpha ** 2) ** -beta
    img += np.random.default_rng(1).normal(0, 5, img.shape)
    fwhm, b = scanner.fit_moffat(img, fwhm_guess=4.0, center=(12, 12))
    assert fwhm == pytest.approx(2 * alpha * np.sqrt(2 ** (1 / beta) - 1), rel=0.05)
    assert b == pytest.approx(beta, rel=0.15)
    assert scanner.fit_moffat(np.zeros((15, 15)), fwhm_guess=3.0) is None


def test_detect_streaks_finds_trail_not_stars():
    pytest.importorskip("sep")
    img = _psf_frame(size=1200, n=300, seed=11)
    assert scanner.analyze_stars(img)["streaks"] == 0
    rows = np.arange(100, 1100)
    cols = (0.6 * rows + 150).astype(int)
    for d in (-1, 0, 1):
        img[rows, cols + d] += 600.0                       # satellite: 1000 px tall, 3 px wide
    res = scanner.analyze_stars(img)
    assert res["streaks"] >= 1
    x1, y1, x2, y2 = res["diagnostics"]["streaks"][0]
    assert abs(abs(y2 - y1) - 1000) < 250 and abs(abs(x2 - x1) - 600) < 200


def test_render_preview_writes_bounded_jpeg(tmp_path):
    from astropy.io import fits
    from PIL import Image
    src = tmp_path / "frame.fits"
    fits.PrimaryHDU(_psf_frame(size=600, n=50).astype(np.uint16)).writeto(src)
    assert scanner.render_preview(str(src), str(tmp_path), "prev.jpg", max_size=256) == "prev.jpg"
    assert max(Image.open(tmp_path / "prev.jpg").size) == 256


def test_selector_psf_streak_and_transparency_vars():
    import main

    def light(i, **kw):
        return Row(imagetyp="Light Frame", object="M 31", filter="L", instrume="cam", telescop="scope",
                   exptime=300.0, date_obs=f"2026-05-13T04:{i * 5:02d}:00", star_count=500,
                   pixel_scale=2.0, **kw)

    cands = [light(0, star_flux=50000.0, streaks=0, psf_fwhm=3.0),
             light(1, star_flux=51000.0, streaks=2, psf_fwhm=3.2),
             light(2, star_flux=30000.0),                   # veiled, not analyzed for streaks/PSF
             light(3, star_flux=49000.0, streaks=0, psf_fwhm=2.9)]
    ev = lambda e: main._evaluate_candidates(cands, e).tolist()
    assert ev("satellite") == [False, True, False, False]
    assert ev("psf_fwhm_arcsec < 6.1") == [True, False, False, True]
    assert ev("transparency < 0.7") == [False, False, True, False]
    assert ev("seq_anomaly") == [False, False, True, False]


def test_calibration_report_matches_darks_flats_bias():
    import calibration

    def frame(imagetyp, **kw):
        base = dict(instrume="ASI6200", xbinning=1, gain=100, offset=50, exptime=300.0, ccd_temp=-10.0,
                    filter="L", date_obs="2026-05-13T04:00:00", object="M 31")
        base.update(kw)
        return Row(imagetyp=imagetyp, **base)

    rows = ([frame("Light Frame")] * 2
            + [frame("Light Frame", filter="Ha", date_obs="2026-05-20T04:00:00")]
            + [frame("Light Frame", exptime=60.0)]
            + [frame("Dark Frame", ccd_temp=-9.5, filter=None)] * 3
            + [frame("Dark Frame", ccd_temp=-20.0, filter=None)] * 2      # too cold to match
            + [frame("Flat Frame", exptime=2.0, date_obs="2026-05-12T20:00:00")] * 5
            + [frame("Bias Frame", exptime=0.0, filter=None)] * 10)
    rep = calibration.report(rows, temp_tolerance=2, flat_max_days=30)
    by = {(s["filter"], s["exposure_s"]): s for s in rep["setups"]}

    l300 = by[("L", 300.0)]
    assert (l300["frames"], l300["darks"], l300["flats"], l300["bias"]) == (2, 3, 5, 10)
    assert l300["flat_gap_days"] == 1 and l300["missing"] == []
    assert by[("Ha", 300.0)]["missing"] == ["flats"]
    assert by[("L", 60.0)]["missing"] == ["darks"]
    assert [s["missing"] != [] for s in rep["setups"]] == [True, True, False]   # problems first
    assert rep["summary"]["uncalibrated_integration_sec"] == 360.0
    assert rep["calibration_frames"] == {"darks": 5, "flats": 5, "bias": 10}


def test_short_exposures_cannot_trail():
    pytest.importorskip("sep")
    img = _star_field(trail_px=25)
    assert scanner.compute_quality(img, {"exptime": 300.0})["trail_score"] > 0.5
    # Lunar/planetary exposures: shapes are surface detail, never a tracking failure
    assert scanner.compute_quality(img, {"exptime": 0.001})["trail_score"] == 0.0
    assert scanner.compute_quality(img, {})["trail_score"] > 0.5   # unknown exposure: scored


# ── raw OSC (CFA) frames ──────────────────────────────────────────────────────
def test_cfa_pattern_superpixel_and_clip_map():
    assert scanner.cfa_pattern("RGGB") == "RGGB"
    assert scanner.cfa_pattern(" gbrg ") == "GBRG"
    assert scanner.cfa_pattern("MONO") is None and scanner.cfa_pattern(None) is None
    cfa = np.array([[1, 2, 5, 6, 9], [3, 4, 7, 8, 9], [9, 9, 9, 9, 9]], dtype=np.float32)
    assert scanner.superpixel(cfa).tolist() == [[2.5, 6.5]]          # odd row/column dropped
    raw = np.zeros((8, 8), dtype=np.float32)
    raw[4, 5] = 100.0                                                # one clipped photosite
    clipped = scanner.superpixel_clipped(raw)
    assert clipped.shape == (4, 4) and clipped[2, 2] and clipped[1, 1] and not clipped[0, 0]


def _cfa_frame(size=800, n=400, seed=21):
    """Raw RGGB mosaic: Gaussian stars (σ 1.5 sensor px) through R/G/B photosites with
    very different sensitivities and pedestals, like an uncalibrated OSC light."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    red, blue = (yy % 2 == 0) & (xx % 2 == 0), (yy % 2 == 1) & (xx % 2 == 1)
    gain = np.where(red, 0.5, np.where(blue, 0.35, 1.0))
    pedestal = np.where(red, 600.0, np.where(blue, 300.0, 2000.0))
    sky = np.zeros((size, size))
    for _ in range(n):
        x0, y0 = rng.uniform(30, size - 30, 2)
        ys, xs = slice(int(y0) - 10, int(y0) + 11), slice(int(x0) - 10, int(x0) + 11)
        sky[ys, xs] += rng.uniform(3000, 9000) * np.exp(
            -((xx[ys, xs] - x0) ** 2 + (yy[ys, xs] - y0) ** 2) / (2 * 1.5 ** 2))
    return np.clip(pedestal + gain * sky + rng.normal(0, 10, (size, size)), 0, 65535).astype(np.float32)


def test_osc_cfa_frames_measured_on_superpixels():
    pytest.importorskip("sep")
    import json
    img = _cfa_frame()
    as_mono = scanner.compute_quality(img, {"exptime": 300.0})       # mosaic pattern reads as noise
    osc = scanner.compute_quality(img, {"exptime": 300.0, "bayer": "RGGB"})
    assert osc["star_count"] > 300 and osc["star_count"] > (as_mono["star_count"] or 0)
    # Sensor pixels, superpixel box removed: true FWHM 3.53, HFR 1.77
    assert osc["psf_fwhm"] == pytest.approx(3.53, rel=0.12)
    assert osc["median_hfr"] == pytest.approx(1.77, rel=0.12)
    assert 2.5 < osc["median_fwhm"] < 5.5                            # moments: coarse on binned stars
    d = json.loads(osc["diagnostics_json"])
    assert (d["width"], d["height"], d["binned"]) == (800, 800, 2)
    assert max(s[0] for s in d["stars"]) > 400                       # positions back on the full frame


# ── XISF ──────────────────────────────────────────────────────────────────────
def _write_xisf(path, data, keywords=(), codec=None, storage="Planar", image_type=None):
    """Tiny single-image XISF 1.0 writer for tests (attached data block)."""
    import struct
    import zlib
    arr = np.asarray(data)
    arr = arr[np.newaxis] if arr.ndim == 2 else arr
    ch, h, w = arr.shape
    fmt = {"uint16": "UInt16", "float32": "Float32"}[arr.dtype.name]
    block = (arr.transpose(1, 2, 0) if storage == "Normal" else arr).astype(arr.dtype.newbyteorder("<")).tobytes()
    extra = ""
    if codec:
        name, shuffle, size = codec.replace("+sh", ""), codec.endswith("+sh"), len(block)
        raw = np.frombuffer(block, np.uint8).reshape(-1, arr.dtype.itemsize).T.tobytes() if shuffle else block
        if name == "zlib":
            packed = zlib.compress(raw)
        elif name == "zstd":
            import zstandard
            packed = zstandard.ZstdCompressor().compress(raw)
        else:
            import lz4.block
            packed = lz4.block.compress(raw, store_size=False)
        extra = f' compression="{codec}:{size}' + (f':{arr.dtype.itemsize}"' if shuffle else '"')
        block = packed
    if image_type:
        extra += f' imageType="{image_type}"'
    kws = "".join(f'<FITSKeyword name="{n}" value="{v}" comment="{c}"/>' for n, v, c in keywords)

    def header(pos):
        return (f'<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" xmlns="http://www.pixinsight.com/xisf">'
                f'<Image geometry="{w}:{h}:{ch}" sampleFormat="{fmt}" pixelStorage="{storage}"{extra} '
                f'location="attachment:{pos:010d}:{len(block)}">{kws}</Image></xisf>').encode()

    xml = header(16 + len(header(0)))
    with open(path, "wb") as f:
        f.write(b"XISF0100" + struct.pack("<I", len(xml)) + b"\0" * 4 + xml + block)


@pytest.mark.parametrize("codec", [None, "zlib", "zlib+sh", "lz4", "lz4hc+sh", "zstd+sh"])
def test_xisf_reads_pixels_and_fits_keywords(tmp_path, codec):
    if codec and codec.startswith("lz4"):
        pytest.importorskip("lz4")
    if codec and codec.startswith("zstd"):
        pytest.importorskip("zstandard")
    import xisf
    data = np.arange(12 * 7, dtype=np.uint16).reshape(7, 12) * 700
    path = tmp_path / "light.xisf"
    _write_xisf(path, data, codec=codec, keywords=[
        ("OBJECT", "'M 31'", "target"), ("EXPTIME", "300.", ""), ("XBINNING", "1", ""),
        ("IMAGETYP", "'LIGHT'", ""), ("BAYERPAT", "'RGGB'", ""), ("NOTE", "'it''s'", ""),
        ("ROWORDER", "T", ""), ("HISTORY", "", "calibrated")])
    hdr, img = xisf.read(str(path))
    assert img.shape == (7, 12) and img.dtype == np.uint16 and np.array_equal(img, data)
    assert (hdr["OBJECT"], hdr["EXPTIME"], hdr["XBINNING"], hdr["NOTE"], hdr["ROWORDER"]) == ("M 31", 300.0, 1, "it's", True)
    assert (hdr["NAXIS1"], hdr["NAXIS2"]) == (12, 7)
    fields = scanner.extract_header(hdr)
    assert (fields["object"], fields["imagetyp"], fields["exptime"], fields["bayer"]) == ("M 31", "Light Frame", 300.0, "RGGB")


def test_xisf_normal_storage_rgb_metadata_only_and_errors(tmp_path):
    import xisf
    rgb = np.random.default_rng(2).random((3, 5, 4)).astype(np.float32)
    path = tmp_path / "rgb.xisf"
    _write_xisf(path, rgb, storage="Normal", image_type="MasterLight")
    hdr, img = xisf.read(str(path))
    assert img.shape == (3, 5, 4) and np.array_equal(img, rgb)
    assert hdr["IMAGETYP"] == "MasterLight"
    assert xisf.read(str(path), with_data=False)[1] is None
    bad = tmp_path / "bad.xisf"
    bad.write_bytes(b"SIMPLE  =                    T")
    with pytest.raises(xisf.XISFError):
        xisf.read(str(bad))


def test_process_file_indexes_xisf(tmp_path):
    pytest.importorskip("sep")
    assert ".xisf" in scanner.FITS_EXTENSIONS
    path = tmp_path / "frame.xisf"
    _write_xisf(path, _psf_frame(size=600, n=120).astype(np.uint16), codec="zlib+sh",
                keywords=[("OBJECT", "'NGC 7000'", ""), ("IMAGETYP", "'LIGHT'", ""), ("EXPTIME", "180.", "")])
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    res = scanner.process_file(str(path), None, str(thumbs))
    assert res["ok"], res.get("error")
    assert res["fields"]["object"] == "NGC 7000" and res["fields"]["naxis1"] == 600
    assert res["star_count"] > 80 and res["thumbnail"]


def test_downsample_reduces_big_frame():
    big = np.zeros((6388, 9576), dtype=np.uint16)
    ds = scanner._downsample(big, target=1024)
    assert max(ds.shape) <= 1064   # 9576 // 9 = 1064
    small = np.zeros((500, 500), dtype=np.uint16)
    assert scanner._downsample(small, target=1024).shape == (500, 500)  # untouched


# ── WCS parsing ───────────────────────────────────────────────────────────────
def _wcs_header(with_naxis=True):
    from astropy.io import fits
    h = fits.Header()
    h["CTYPE1"] = "RA---TAN"; h["CTYPE2"] = "DEC--TAN"
    h["CRPIX1"] = 3000.0; h["CRPIX2"] = 2000.0
    h["CRVAL1"] = 315.6; h["CRVAL2"] = 68.16
    h["CD1_1"] = -0.0008; h["CD1_2"] = 0.0; h["CD2_1"] = 0.0; h["CD2_2"] = 0.0008
    if with_naxis:
        h["NAXIS1"] = 6000; h["NAXIS2"] = 4000
    return h


def test_parse_wcs_with_dims():
    r = wcs_util.parse_wcs(_wcs_header(), 6000, 4000)
    assert r["solved"] == 1
    assert r["wcs_ra"] == pytest.approx(315.6, abs=0.1)
    assert r["wcs_dec"] == pytest.approx(68.16, abs=0.1)
    assert r["fov_w"] and r["fov_h"]


def test_parse_wcs_no_dims_crval_fallback():
    r = wcs_util.parse_wcs(_wcs_header(with_naxis=False), None, None)
    assert r["solved"] == 1
    assert r["wcs_ra"] == pytest.approx(315.6, abs=0.1)
    assert r["fov_w"] is None   # can't compute FOV without dims


def test_parse_wcs_no_celestial_returns_none():
    from astropy.io import fits
    h = fits.Header(); h["OBJECT"] = "x"; h["NAXIS1"] = 100; h["NAXIS2"] = 100
    assert wcs_util.parse_wcs(h, 100, 100) is None


# ── benchmark aggregation ─────────────────────────────────────────────────────
def test_aggregate_compares_only_common_frames():
    # small frame ran both; BIG frame's float64 was skipped
    results = [
        {"file": "s", "repeat": 0, "dtype": "float32", "timing": {"total": 1.0, "_megapixels": 2.0}},
        {"file": "s", "repeat": 0, "dtype": "float64", "timing": {"total": 1.1, "_megapixels": 2.0}},
        {"file": "BIG", "repeat": 0, "dtype": "float32", "timing": {"total": 10.0, "_megapixels": 61.0}},
        {"file": "BIG", "repeat": 0, "dtype": "float64", "skipped": "too big"},
    ]
    agg = benchmark.aggregate(results)
    assert agg["compared"] == 1                       # BIG excluded
    assert agg["skipped"] == 1
    assert agg["megapixels"] == 2.0                   # not dragged up by BIG
    assert agg["speedup"]["total"] == pytest.approx(1.1, abs=0.01)  # fair, not 0.1x


def test_compare_metrics_flags_out_of_tolerance():
    results = [
        {"file": "a", "repeat": 0, "dtype": "float32", "metrics": {"median_hfr": 2.0, "star_count": 100,
         "median_fwhm": 4.0, "eccentricity": 0.4, "background_median": 100}},
        {"file": "a", "repeat": 0, "dtype": "float64", "metrics": {"median_hfr": 2.0, "star_count": 100,
         "median_fwhm": 4.4, "eccentricity": 0.4, "background_median": 100}},  # fwhm +10%
    ]
    cmp = benchmark.compare_metrics(results)
    assert cmp["ok"] is False
    assert cmp["per_metric"]["median_fwhm"]["flagged"] is True
    assert cmp["per_metric"]["median_hfr"]["flagged"] is False


# ── selector helpers (main) ───────────────────────────────────────────────────
def test_main_helpers():
    import main
    appr = [
        Row(path="/mnt/astro/a.fits", quality_score=60, fov_w=1.5, wcs_scale=2.0, pixel_scale=2.0, naxis1=6000),
        Row(path="/mnt/astro/b.fits", quality_score=90, fov_w=2.6, wcs_scale=3.1, pixel_scale=3.1, naxis1=9576),
        Row(path="/mnt/astro/c.fits", quality_score=85, fov_w=0.8, wcs_scale=0.6, pixel_scale=0.6, naxis1=9576),
    ]
    # reference picking by geometry (among above-median quality)
    assert main.pick_reference(appr, "max_fov").path.endswith("b.fits")
    assert main.pick_reference(appr, "min_fov").path.endswith("c.fits")
    assert main.pick_reference(appr, "min_scale").path.endswith("c.fits")

    # path rewrite (container -> host, windows sep)
    assert main._rewrite_path("/mnt/astro/x.fits", "/mnt/astro", "P:\\astro") == "P:\\astro\\x.fits"
    assert main._rewrite_path("/other/x.fits", "/mnt/astro", "P:\\astro") == "/other/x.fits"


def test_selector_path_and_filename_vars():
    import main
    cands = [
        Row(path="/mnt/astro/M31/Reject/a.fits", filename="a.fits", imagetyp="Light Frame"),
        Row(path="P:\\astro\\M31\\rejects\\b.fits", filename="b.fits", imagetyp="Light Frame"),
        Row(path="/mnt/astro/M31/good/LIGHT_H_300s.fits", filename="LIGHT_H_300s.fits", imagetyp="Light Frame"),
    ]
    ev = main._evaluate_candidates
    assert ev(cands, "path not like '%reject%'").tolist() == [False, False, True]
    # Windows separators in either the stored path or the pattern are folded to "/"
    assert ev(cands, "path like '%\\rejects\\%'").tolist() == [False, True, False]
    assert ev(cands, "path like '%/rejects/%'").tolist() == [False, True, False]
    assert ev(cands, "filename like 'light_h_%'").tolist() == [False, False, True]
    assert main._path_pattern("reject") == "%reject%"
    assert main._path_pattern("%\\2026-%") == "%/2026-%"


def test_selector_tracking_vars(monkeypatch):
    import main
    import config
    monkeypatch.setattr(config, "TRAIL_THRESHOLD", 0.05)
    cands = [Row(trail_score=0.0, imagetyp="Light Frame"),
             Row(trail_score=0.4, imagetyp="Light Frame"),
             Row(trail_score=None, imagetyp="Light Frame")]   # not analyzed yet
    ev = lambda e: main._evaluate_candidates(cands, e).tolist()
    assert ev("good_tracking") == [True, False, False]
    assert ev("trailed") == [False, True, False]
    assert ev("!trailed") == [True, False, True]
    assert ev("trail_score > 0.1") == [False, True, False]


def test_rescore_stored_applies_current_trail_rules(tmp_path, monkeypatch):
    """Rows analyzed under older rules get the sub-second trail rule + quality cap
    without re-reading FITS."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for mod in ("config", "database", "main"):
        sys.modules.pop(mod, None)
    import config
    import database as d
    d.create_tables()
    s = d.SessionLocal()
    metrics = dict(imagetyp="Light Frame", median_hfr=1.5, median_fwhm=3.0, eccentricity=0.3, star_count=500)
    good = scanner.quality_score(1.5, 3.0, 0.3, 500, 0.0)
    s.add_all([
        d.FitsFile(path="/moon.fits", filename="moon", exptime=0.001, trail_score=0.3, quality_score=99.0, **metrics),
        d.FitsFile(path="/moon2.fits", filename="moon2", exptime=0.0001, trail_score=None, quality_score=good, **metrics),
        d.FitsFile(path="/trail.fits", filename="trail", exptime=300.0, trail_score=0.3, quality_score=99.0, **metrics),
        d.FitsFile(path="/ok.fits", filename="ok", exptime=300.0, trail_score=0.0, quality_score=good, **metrics),
        d.FitsFile(path="/new.fits", filename="new", exptime=300.0, imagetyp="Light Frame"),   # never analyzed
    ])
    s.commit()

    assert scanner.rescore_stored(s) == 3
    rows = {f.filename: f for f in s.query(d.FitsFile)}
    assert rows["moon"].trail_score == 0.0 and rows["moon"].quality_score == good
    assert rows["moon2"].trail_score == 0.0                   # no longer "missing" a trail score
    assert rows["trail"].trail_score == 0.3 and rows["trail"].quality_score == config.TRAIL_QUALITY_CAP
    assert rows["ok"].quality_score == good
    assert rows["new"].trail_score is None and rows["new"].quality_score is None
    assert scanner.rescore_stored(s) == 0                     # idempotent
    s.close()


def test_selector_capture_time_and_date_bounds():
    import main
    cands = [
        Row(date_obs="2026-05-13T04:30:12.1234567", imagetyp="Light Frame"),   # NINA-style 7 decimals
        Row(date_obs="2026-05-13T23:59:59.5Z", imagetyp="Light Frame"),
        Row(date_obs=None, imagetyp="Light Frame"),
        Row(date_obs="garbage", imagetyp="Light Frame"),
    ]
    ev = lambda e: main._evaluate_candidates(cands, e).tolist()
    assert ev("date between '2026-05-13' and '2026-05-13'") == [True, True, False, False]
    assert ev("hour between 4 and 5") == [True, False, False, False]
    assert ev("hour > 23.9 && date_obs == '2026-05-13'") == [False, True, False, False]
    assert ev("missing(date)") == [False, False, True, True]

    assert main._date_bound("2026-05-31") == "2026-05-31"
    assert main._date_bound("2026-05-31", end=True) == "2026-05-31T23:59:59.999999999"
    assert main._date_bound("2026-05-31 22:30", end=True) == "2026-05-31T22:30:59.999999999"
    assert main._date_bound("2026-05-31t22:30:10") == "2026-05-31T22:30:10"


def test_facets_and_xpsm():
    import main
    import xml.dom.minidom as minidom
    appr = [
        Row(path="/a.fits", exptime=300, instrume="ASI6200", telescop="GT71", object="NGC 7023", filter="L", date_obs="2026-05-13T04:00:00"),
        Row(path="/b.fits", exptime=300, instrume="ASI6200", telescop="GT71", object="NGC 7023", filter="L", date_obs="2026-05-13T05:00:00"),
        Row(path="/c.fits", exptime=180, instrume="ASI2600", telescop="Esprit", object="M 31", filter="H", date_obs="2026-05-14T03:00:00"),
    ]
    f = main._facets(appr)
    assert f["integration_sec"] == 780.0
    assert f["instrume"][0] == {"value": "ASI6200", "count": 2}
    assert {d["value"] for d in f["date"]} == {"2026-05-13", "2026-05-14"}

    xml = main.build_xpsm("P:\\astro\\ref.fits", ["P:\\astro\\a & b.fits", "P:\\astro\\ref.fits"])
    minidom.parseString(xml)                       # must be well-formed
    assert "a &amp; b.fits" in xml                 # escaped
    assert 'rows="2"' in xml


# ── targets dashboard ─────────────────────────────────────────────────────────
def test_targets_rollup_and_detail(tmp_path, monkeypatch):
    """End-to-end over a throwaway SQLite DB: rollups, per-night detail, tombstone
    exclusion."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "database", "main"):
        sys.modules.pop(m, None)
    import database as d
    import main as m
    d.create_tables()
    s = d.SessionLocal()

    def light(**kw):
        base = dict(imagetyp="Light Frame", object="M 31", exptime=300.0,
                    telescop="GT71", instrume="ASI6200")
        base.update(kw)
        return d.FitsFile(**base)

    s.add_all([
        light(path="/a.fits", filename="a", filter="L", date_obs="2026-05-13T04:00:00",
              median_hfr=2.0, quality_score=80.0),
        light(path="/b.fits", filename="b", filter="L", date_obs="2026-05-13T05:00:00",
              median_hfr=2.4, quality_score=70.0),
        light(path="/c.fits", filename="c", filter="Ha", date_obs="2026-05-14T03:00:00",
              median_hfr=3.0, quality_score=60.0, rejected=1),
        light(path="/d.fits", filename="d", object="NGC 7000", filter="Ha",
              date_obs="2026-05-15T03:00:00", exptime=900.0),
        # tombstone: must be excluded everywhere
        d.FitsFile(path="/bad.fits", filename="bad", imagetyp="Light Frame",
                   object="M 31", exptime=300.0, index_error="corrupt"),
    ])
    s.commit()

    res = m.list_targets(s)
    by_name = {t["object"]: t for t in res["targets"]}
    assert set(by_name) == {"M 31", "NGC 7000"}

    m31 = by_name["M 31"]
    assert m31["frames"] == 3                      # tombstone excluded
    assert m31["integration_sec"] == 900.0         # 3 x 300, not 1200
    assert m31["nights"] == 2
    assert m31["first_night"] == "2026-05-13" and m31["last_night"] == "2026-05-14"
    assert m31["rejected"] == 1
    assert {f["filter"] for f in m31["filters"]} == {"L", "Ha"}
    # sorted by integration desc: L (600s) before Ha (300s)
    assert m31["filters"][0]["filter"] == "L"
    assert m31["rigs"][0]["instrume"] == "ASI6200"

    # targets sorted by integration desc: NGC 7000 (900s) > M 31 (900s) tie ok, check totals
    assert res["total_integration_sec"] == 1800.0
    assert res["target_count"] == 2

    detail = m.target_detail("M 31", s)
    assert detail["night_count"] == 2
    assert detail["frames"] == 3
    assert detail["integration_sec"] == 900.0
    assert detail["nights"][0]["night"] == "2026-05-14"   # newest first
    first_night = [n for n in detail["nights"] if n["night"] == "2026-05-13"][0]
    assert first_night["frames"] == 2
    assert first_night["filters"][0]["avg_hfr"] == pytest.approx(2.2, abs=0.01)
    s.close()


# ── user-defined targets (spatial matching) ───────────────────────────────────
def test_user_targets_match_by_fov_coverage(tmp_path, monkeypatch):
    """A wide frame counts toward EVERY target inside its FOV; narrow frames only
    toward what they actually cover."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "database", "main"):
        sys.modules.pop(m, None)
    import database as d
    import main as m
    d.create_tables()
    s = d.SessionLocal()

    # Two targets ~2 deg apart (like NGC 7000 and the Pelican)
    a_ra, a_dec = 314.0, 44.5
    b_ra, b_dec = 316.0, 44.0

    def light(path, ra, dec, fov_w, fov_h, **kw):
        return d.FitsFile(path=path, filename=path, imagetyp="Light Frame",
                          object="whatever", exptime=300.0, wcs_ra=ra, wcs_dec=dec,
                          fov_w=fov_w, fov_h=fov_h, wcs_rotation=0.0, **kw)

    s.add_all([
        # wide field centred between them — covers BOTH
        light("/wide.fits", 315.0, 44.25, 4.0, 4.0, filter="L", date_obs="2026-05-13T04:00:00"),
        # narrow field on A only
        light("/a.fits", a_ra, a_dec, 0.5, 0.5, filter="Ha", date_obs="2026-05-14T04:00:00"),
        # far away — covers neither
        light("/far.fits", 100.0, -20.0, 1.0, 1.0, filter="L", date_obs="2026-05-15T04:00:00"),
        # tombstone must never match
        d.FitsFile(path="/bad.fits", filename="bad", imagetyp="Light Frame",
                   wcs_ra=a_ra, wcs_dec=a_dec, fov_w=2.0, fov_h=2.0, index_error="corrupt"),
    ])
    s.commit()

    a_frames = {f.path for f in m.frames_for_target(s, a_ra, a_dec)}
    b_frames = {f.path for f in m.frames_for_target(s, b_ra, b_dec)}

    assert a_frames == {"/wide.fits", "/a.fits"}   # wide counts for A too
    assert b_frames == {"/wide.fits"}              # and for B — same frame, both targets
    assert "/far.fits" not in a_frames | b_frames
    assert "/bad.fits" not in a_frames             # tombstone excluded

    # radius match: a target bigger than the frame still picks up nearby centres
    near = {f.path for f in m.frames_for_target(s, a_ra + 1.0, a_dec, radius_deg=2.0)}
    assert "/a.fits" in near

    # rollup over a target's frames
    roll = m._rollup(m.frames_for_target(s, a_ra, a_dec))
    assert roll["frames"] == 2
    assert roll["integration_sec"] == 600.0
    assert {f["filter"] for f in roll["filters"]} == {"L", "Ha"}
    s.close()


def test_create_target_from_object_uses_median_position(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "database", "main"):
        sys.modules.pop(m, None)
    import database as d
    import main as m
    d.create_tables()
    s = d.SessionLocal()
    s.add_all([
        d.FitsFile(path=f"/{i}.fits", filename=str(i), imagetyp="Light Frame",
                   object="NGC 7000", wcs_ra=ra, wcs_dec=44.0)
        for i, ra in enumerate([313.9, 314.0, 314.1])
    ])
    s.commit()
    t = m.create_target_from_object(object="NGC 7000", db=s)
    assert t["name"] == "NGC 7000"
    assert t["ra"] == pytest.approx(314.0, abs=0.01)   # median
    assert t["dec"] == pytest.approx(44.0, abs=0.01)

    with pytest.raises(Exception):
        m.create_target_from_object(object="Nothing", db=s)
    s.close()
