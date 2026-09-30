"""
Public read-only API (/api/v1) over a throwaway SQLite DB: field selection,
filters, pagination, thumbnails, auth, error shape, aggregates and the schema.
"""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PUBLIC_API_KEYS", raising=False)
    for m in ("config", "database", "main", "public_api"):
        sys.modules.pop(m, None)
    import config
    import database as d
    import main
    from fastapi.testclient import TestClient
    from PIL import Image

    d.create_tables()
    Image.new("RGB", (64, 40)).save(config.THUMBNAILS_DIR / "t1.jpg")
    s = d.SessionLocal()
    a = d.FitsFile(path="/mnt/astro/M31/good/a.fits", filename="a.fits", imagetyp="Light Frame", object="M 31",
                   filter="L", exptime=300.0, date_obs="2026-05-13T04:00:00", median_hfr=2.0, quality_score=80.0,
                   thumbnail="t1.jpg", solved=1, wcs_ra=10.68, wcs_dec=41.27, fov_w=2.0, fov_h=1.5, wcs_rotation=0.0,
                   naxis1=6000, naxis2=4000, file_mtime=1778644800.0, indexed_at=datetime(2026, 5, 14, 10),
                   header_json=json.dumps({"EXPTIME": 300.0, "CCD-TEMP": -10.0, "FILTER": "L"}))
    b = d.FitsFile(path="/mnt/astro/M31/reject/b.fits", filename="b.fits", imagetyp="Light Frame", object="M 31",
                   filter="Ha", exptime=600.0, date_obs="2026-05-14T03:00:00", median_hfr=4.0, quality_score=40.0,
                   ra=10.7, dec=41.3, rejected=1, bayer="RGGB", indexed_at=datetime(2026, 5, 15))
    c = d.FitsFile(path="/mnt/astro/darks/c.fits", filename="c.fits", imagetyp="Dark Frame", exptime=300.0,
                   indexed_at=datetime(2026, 5, 10))
    bad = d.FitsFile(path="/mnt/astro/bad.fits", filename="bad.fits", index_error="corrupt")
    saved = d.SavedQuery(name="sharp", folder="Mono", query_json=json.dumps({"expression": "hfr < 3", "imagetyp": "Light Frame"}))
    s.add_all([a, b, c, bad, saved])
    s.commit()
    ids = {"a": a.id, "b": b.id, "c": c.id, "bad": bad.id, "saved": saved.id}
    s.close()
    return TestClient(main.app), config, ids


def _names(res):
    return [i["file"]["filename"] if "file" in i else i["id"] for i in res.json()["items"]]


def test_default_listing_shape(api):
    client, _, ids = api
    r = client.get("/api/v1/frames")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3                                   # tombstone excluded
    assert _names(r) == ["b.fits", "a.fits", "c.fits"]          # -date_obs, nulls last
    a = body["items"][1]
    assert set(a) == {"id", "file", "capture", "pointing", "plate_solve", "position", "quality",
                      "conditions", "status", "thumbnail", "links"}   # no header by default
    assert a["file"]["modified_at"] == "2026-05-13T04:00:00Z"
    assert a["file"]["indexed_at"] == "2026-05-14T10:00:00Z"
    assert a["thumbnail"] == {"available": True, "url": f"http://testserver/api/v1/frames/{ids['a']}/thumbnail",
                              "content_type": "image/jpeg", "width_px": 64, "height_px": 40}
    assert a["links"]["self"] == f"http://testserver/api/v1/frames/{ids['a']}"
    assert a["position"]["source"] == "plate_solve" and a["plate_solve"]["solved"] is True
    b = body["items"][0]
    assert b["position"] == {"ra_deg": 10.7, "dec_deg": 41.3, "source": "header", "pixel_scale_arcsec": None,
                             "fov_width_deg": None, "fov_height_deg": None}
    assert b["capture"]["color"] == "osc" and b["status"]["rejected"] is True
    assert b["thumbnail"]["available"] is False and b["thumbnail"]["url"] is None


def test_field_selection_and_header(api):
    client, _, ids = api
    r = client.get("/api/v1/frames", params={"fields": "capture.object,quality", "sort": "id"})
    item = r.json()["items"][0]
    assert set(item) == {"id", "capture", "quality"}
    assert item["capture"] == {"object": "M 31"}

    r = client.get(f"/api/v1/frames/{ids['a']}", params={"fields": "file.filename", "expand": "header"})
    assert r.json() == {"id": ids["a"], "file": {"filename": "a.fits"},
                        "header": {"EXPTIME": 300.0, "CCD-TEMP": -10.0, "FILTER": "L"}}

    r = client.get(f"/api/v1/frames/{ids['a']}", params={"fields": "id,header.exptime,header.CCD-TEMP"})
    assert r.json() == {"id": ids["a"], "header": {"EXPTIME": 300.0, "CCD-TEMP": -10.0}}


def test_non_finite_header_values_become_null(api):
    client, _, _ = api
    import database as d
    s = d.SessionLocal()
    f = d.FitsFile(path="/mnt/astro/nan.fits", filename="nan.fits", header_json='{"GOOD": 1.5, "BAD": NaN, "BIG": Infinity}')
    s.add(f); s.commit()
    r = client.get(f"/api/v1/frames/{f.id}", params={"fields": "header"})
    s.close()
    assert r.status_code == 200
    assert r.json()["header"] == {"GOOD": 1.5, "BAD": None, "BIG": None}


@pytest.mark.parametrize("params,want", [
    ({"path_not_like": "reject", "image_type": "Light Frame"}, ["a.fits"]),
    ({"filter": "L,Ha"}, ["b.fits", "a.fits"]),
    ({"object": "m 3"}, ["b.fits", "a.fits"]),
    ({"where": "hfr < 3"}, ["a.fits"]),
    ({"where": "date between '2026-05-14' and '2026-05-14'"}, ["b.fits"]),
    ({"date_from": "2026-05-14"}, ["b.fits"]),
    ({"date_to": "2026-05-13"}, ["a.fits"]),
    ({"covers_ra": 10.68, "covers_dec": 41.27}, ["a.fits"]),
    ({"near_ra": 10.7, "near_dec": 41.3, "radius_deg": 0.5}, ["b.fits", "a.fits"]),
    ({"indexed_since": "2026-05-14T00:00:00Z"}, ["b.fits", "a.fits"]),
    ({"rejected": "false"}, ["a.fits", "c.fits"]),
    ({"solved": "true"}, ["a.fits"]),
    ({"color": "osc"}, ["b.fits"]),
    ({"quality_min": 50, "exposure_max": 300}, ["a.fits"]),
    ({"indexing": "failed"}, ["bad.fits"]),
])
def test_filters(api, params, want):
    client, _, _ = api
    r = client.get("/api/v1/frames", params={**params, "fields": "file.filename"})
    assert r.status_code == 200, r.text
    assert _names(r) == want


def test_tracking_filters_and_fields(api):
    client, config, ids = api
    import database as d
    s = d.SessionLocal()
    s.get(d.FitsFile, ids["a"]).trail_score = 0.01
    s.get(d.FitsFile, ids["b"]).trail_score = 0.4
    s.commit()
    s.close()
    assert 0.01 <= config.TRAIL_THRESHOLD < 0.4

    # Public API: filters + quality.trailed (null until analyzed)
    get = lambda **p: client.get("/api/v1/frames", params={**p, "fields": "file.filename,quality.trailed"})
    assert _names(get(good_tracking="true")) == ["a.fits"]
    assert _names(get(good_tracking="false")) == ["b.fits"]
    assert _names(get(trail_score_max=0.1)) == ["a.fits"]
    trailed = {i["file"]["filename"]: i["quality"]["trailed"] for i in get().json()["items"]}
    assert trailed == {"a.fits": False, "b.fits": True, "c.fits": None}

    # Catalog grid endpoint: tracking filter, trailed flag, trail_score sort
    files = lambda **p: {f["filename"]: f["trailed"] for f in client.get("/api/files", params=p).json()["items"]}
    assert files(tracking="good") == {"a.fits": False}
    assert files(tracking="trailed") == {"b.fits": True}
    items = client.get("/api/files", params={"sort": "trail_score", "order": "desc"}).json()["items"]
    assert [f["filename"] for f in items] == ["b.fits", "a.fits", "c.fits"]   # unanalyzed last


def test_sort_and_pagination(api):
    client, _, _ = api
    r = client.get("/api/v1/frames", params={"sort": "-quality.score", "per_page": 1, "fields": "file.filename"})
    body = r.json()
    assert body["pages"] == 3 and _names(r) == ["a.fits"]
    assert body["links"]["prev"] is None and "page=2" in body["links"]["next"]
    r2 = client.get(body["links"]["next"])
    assert _names(r2) == ["b.fits"]


def test_thumbnail(api):
    client, _, ids = api
    r = client.get(f"/api/v1/frames/{ids['a']}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"
    r = client.get(f"/api/v1/frames/{ids['b']}/thumbnail")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("method,url,params,status,code", [
    ("get", "/api/v1/frames", {"fields": "nope"}, 400, "invalid_field"),
    ("get", "/api/v1/frames", {"expand": "quality"}, 400, "invalid_field"),
    ("get", "/api/v1/frames", {"sort": "bogus"}, 400, "invalid_sort"),
    ("get", "/api/v1/frames", {"where": "hfr <"}, 400, "invalid_expression"),
    ("get", "/api/v1/frames", {"near_ra": 10}, 400, "invalid_parameter"),
    ("get", "/api/v1/frames", {"per_page": 0}, 422, "invalid_parameter"),
    ("get", "/api/v1/frames/999999", {}, 404, "not_found"),
    ("post", "/api/v1/frames", {}, 405, "method_not_allowed"),
])
def test_errors_share_one_shape(api, method, url, params, status, code):
    client, _, _ = api
    r = getattr(client, method)(url, params=params)
    assert r.status_code == status
    err = r.json()["error"]
    assert err["code"] == code and err["message"]


def test_api_keys(api, monkeypatch):
    client, config, ids = api
    monkeypatch.setattr(config, "PUBLIC_API_KEYS", ["s3cret"])
    r = client.get("/api/v1/frames")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert client.get("/api/v1/frames", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/frames", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get(f"/api/v1/frames/{ids['a']}/thumbnail", params={"api_key": "s3cret"}).status_code == 200
    # docs + schema stay reachable so integrators can read them
    assert client.get("/api/v1/openapi.json").status_code == 200
    assert "/api/v1/openapi.json" in client.get("/api/v1/docs").text


def test_openapi_schema(api):
    client, _, _ = api
    spec = client.get("/api/v1/openapi.json").json()
    assert {"/frames", "/frames/{frame_id}", "/frames/{frame_id}/thumbnail", "/frames/{frame_id}/preview",
            "/frames/{frame_id}/diagnostics", "/fields", "/targets", "/targets/{object_name}", "/user-targets",
            "/user-targets/{target_id}", "/sessions", "/calibration", "/catalog",
            "/queries", "/queries/{query_id}", "/queries/{query_id}/frames"} <= set(spec["paths"])
    assert all(set(ops) == {"get"} for ops in spec["paths"].values())          # read-only
    schemas = spec["components"]["schemas"]
    assert {"Frame", "FramePage", "QualityInfo", "SequenceInfo", "FrameDiagnostics", "CalibrationReport",
            "ErrorResponse"} <= set(schemas)
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]
    assert "/api/frames" not in spec["paths"]                                    # internal API not leaked


def test_aggregates_and_saved_queries(api):
    client, _, ids = api
    targets = client.get("/api/v1/targets").json()
    assert targets["target_count"] == 1 and targets["targets"][0]["frames"] == 2
    assert client.get("/api/v1/targets/M 31").json()["night_count"] == 2
    assert client.get("/api/v1/targets/Nope").status_code == 404
    assert client.get("/api/v1/sessions").json()["session_count"] == 2
    assert client.get("/api/v1/catalog").json()["total"] == 3
    assert client.get("/api/v1/user-targets").json()["target_count"] == 0
    assert client.get("/api/v1/user-targets/5").status_code == 404

    fields = {f["path"]: f for f in client.get("/api/v1/fields").json()["fields"]}
    assert fields["quality.hfr_px"]["sortable"] and fields["quality.hfr_px"]["type"] == "number"
    assert fields["header"]["default"] is False

    assert client.get("/api/v1/queries").json()[0]["name"] == "sharp"
    r = client.get(f"/api/v1/queries/{ids['saved']}/frames", params={"fields": "file.filename"})
    assert r.status_code == 200 and _names(r) == ["a.fits"]


def test_screening_fields_grade_and_format_filters(api):
    client, config, ids = api
    import database as d
    s = d.SessionLocal()
    a, b, c = (s.get(d.FitsFile, ids[k]) for k in "abc")
    a.psf_fwhm, a.pixel_scale, a.empty_cells, a.streaks, a.bg_dip, a.set_temp = 3.0, 2.0, 0.3, 0, 0.004, -10.0
    b.psf_fwhm, b.wcs_scale, b.pixel_scale, b.empty_cells, b.streaks, b.bg_dip = 4.0, 1.5, 9.9, 0.0, 2, 0.06
    c.rejected = 0
    s.add(d.FitsFile(path="/mnt/astro/M31/master.xisf", filename="master.xisf", imagetyp="Master Light"))
    s.commit()
    s.close()
    assert config.OCCLUDED_THRESHOLD < 0.3

    fields = "file.filename,quality.psf_fwhm_arcsec,quality.occluded,quality.satellite,quality.shadow"
    q = {i["file"]["filename"]: i["quality"] for i in client.get("/api/v1/frames", params={"fields": fields}).json()["items"]}
    assert q["a.fits"] == {"psf_fwhm_arcsec": 6.0, "occluded": True, "satellite": False, "shadow": False}
    assert q["b.fits"] == {"psf_fwhm_arcsec": 6.0, "occluded": False, "satellite": True, "shadow": True}  # solved scale wins
    assert q["c.fits"] == {"psf_fwhm_arcsec": None, "occluded": None, "satellite": None, "shadow": None}  # not analyzed
    capture = client.get(f"/api/v1/frames/{ids['a']}", params={"fields": "capture.sensor_setpoint_c"}).json()["capture"]
    assert capture == {"sensor_setpoint_c": -10.0}

    names = lambda **p: _names(client.get("/api/v1/frames", params={**p, "fields": "file.filename"}))
    assert names(occluded="true") == ["a.fits"]
    assert names(occluded="false") == ["b.fits"]
    assert names(satellite="true") == ["b.fits"]
    assert names(satellite="false") == ["a.fits"]
    assert names(shadow="true") == ["b.fits"]
    assert names(shadow="false") == ["a.fits"]
    assert names(background_dip_max=0.01) == ["a.fits"]
    assert names(sort="-quality.background_dip") == ["b.fits", "a.fits", "c.fits", "master.xisf"]
    assert names(psf_fwhm_max=3.5) == ["a.fits"]
    assert names(empty_cells_max=0.1) == ["b.fits"]
    assert names(grade="accepted") == ["c.fits"]
    assert names(grade="rejected,accepted") == ["b.fits", "c.fits"]
    assert names(grade="unmarked") == ["a.fits", "master.xisf"]
    assert names(format="xisf") == ["master.xisf"]
    assert names(format="fits") == ["b.fits", "a.fits", "c.fits"]
    r = client.get("/api/v1/frames", params={"grade": "maybe"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_parameter"

    base = f"http://testserver/api/v1/frames/{ids['a']}"
    assert client.get(base, params={"fields": "file.format,links"}).json() == {
        "id": ids["a"], "file": {"format": "fits"},
        "links": {"self": base, "preview": f"{base}/preview", "diagnostics": f"{base}/diagnostics"}}


def test_sequence_group(api):
    client, _, ids = api
    import database as d
    s = d.SessionLocal()
    frames = [d.FitsFile(path=f"/mnt/astro/M81/L_{i}.fits", filename=f"L_{i}.fits", imagetyp="Light Frame",
                         object="M 81", filter="L", instrume="cam", telescop="scope", exptime=300.0,
                         date_obs=f"2026-05-20T04:{i * 5:02d}:00", star_count=n, median_hfr=2.0)
              for i, n in enumerate([500, 510, 490, 150, 505])]      # frame 3: cloud
    frames[0].object = "m 81"                                         # same session despite the case
    s.add_all(frames)
    s.commit()
    cloudy = frames[3].id
    s.close()

    r = client.get("/api/v1/frames", params={"object": "M 81", "sort": "capture.date_obs",
                                             "fields": "file.filename", "expand": "sequence"})
    seq = [i["sequence"] for i in r.json()["items"]]
    assert [x["anomaly"] for x in seq] == [False, False, False, True, False]
    assert all(x["judged"] for x in seq) and seq[3]["star_drop"] > 0.5 and seq[0]["transparency"] is None

    # The whole session is the reference, even when a single frame is requested
    one = client.get(f"/api/v1/frames/{cloudy}", params={"fields": "sequence.anomaly,sequence.star_drop"}).json()
    assert set(one["sequence"]) == {"anomaly", "star_drop"} and one["sequence"]["anomaly"] is True

    lone = client.get(f"/api/v1/frames/{ids['a']}", params={"fields": "sequence"}).json()["sequence"]
    assert lone["judged"] is False and lone["anomaly"] is None and lone["star_drop"] is None
    assert "sequence" not in client.get(f"/api/v1/frames/{ids['a']}").json()          # opt-in
    r = client.get("/api/v1/frames", params={"fields": "sequence.nope"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_field"
    catalog = {f["path"]: f for f in client.get("/api/v1/fields").json()["fields"]}
    assert catalog["sequence.transparency"]["default"] is False and "sequence" in client.get("/api/v1/fields").json()["groups"]


def test_diagnostics_preview_and_calibration(api, tmp_path):
    client, _, ids = api
    import numpy as np
    from astropy.io import fits
    import database as d
    diag = {"v": 1, "width": 6000, "height": 4000, "grid": [8, 6], "cell_stars": [40] * 48, "empty_below": 8.0,
            "judged": True, "empty_cluster": [], "cell_bg_sigma": None, "stars": [[1.5, 2.5, 1.2, 1.1, 30, 0, 1]],
            "streaks": [], "tiles": 1, "sample": 1, "saturated": 0}
    src = tmp_path / "frame.fits"
    fits.PrimaryHDU((np.random.default_rng(0).random((300, 400)) * 1000).astype(np.uint16)).writeto(src)
    s = d.SessionLocal()
    s.get(d.FitsFile, ids["a"]).diagnostics_json = json.dumps(diag)
    real = d.FitsFile(path=str(src), filename="frame.fits", file_mtime=src.stat().st_mtime)
    s.add(real)
    s.commit()
    real_id = real.id
    s.close()

    r = client.get(f"/api/v1/frames/{ids['a']}/diagnostics")
    assert r.status_code == 200 and r.json()["stars"] == diag["stars"] and r.json()["grid"] == [8, 6]
    for fid, code in ((ids["b"], "not_analyzed"), (999999, "not_found")):
        r = client.get(f"/api/v1/frames/{fid}/diagnostics")
        assert r.status_code == 404 and r.json()["error"]["code"] == code

    r = client.get(f"/api/v1/frames/{real_id}/preview", params={"size": 256})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    for fid, code in ((ids["b"], "source_unavailable"), (999999, "not_found")):
        r = client.get(f"/api/v1/frames/{fid}/preview")
        assert r.status_code == 404 and r.json()["error"]["code"] == code

    cal = client.get("/api/v1/calibration").json()
    assert cal["summary"]["setups"] == 2 and cal["summary"]["missing_flats"] == 2
    assert cal["calibration_frames"] == {"darks": 1, "flats": 0, "bias": 0}
    assert {s["exposure_s"]: s["missing"] for s in cal["setups"]} == {300.0: ["flats"], 600.0: ["darks", "flats"]}
