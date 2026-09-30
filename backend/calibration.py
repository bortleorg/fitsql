"""
Calibration coverage — which light setups in the catalog have darks, flats and
bias frames that could calibrate them (the matching WBPP does), and which don't.

Lights are grouped into setups: camera, binning, gain, offset, exposure, filter
and sensor temperature (nearest °C). For each setup:

* darks match camera + binning + gain + offset + exposure, within the
  temperature tolerance (frames without a temperature count as matching);
* flats match camera + binning + filter; the worst gap in days from a light
  night to the nearest flat night is reported, since flats go stale when the
  optical train changes;
* bias matches camera + binning + gain + offset (informational — dark flats or
  exposure-matched darks can stand in for it).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

FRAME_TYPES = ("Light Frame", "Dark Frame", "Flat Frame", "Bias Frame")


def _round(v, digits):
    return None if v is None else round(float(v), digits)


def _day(date_obs) -> Optional[date]:
    try:
        return date.fromisoformat(str(date_obs)[:10])
    except (TypeError, ValueError):
        return None


def report(rows, temp_tolerance: float, flat_max_days: float) -> dict:
    """`rows` need imagetyp, instrume, filter, xbinning, gain, offset, exptime,
    ccd_temp, date_obs and object attributes."""
    lights = {}
    darks = defaultdict(list)          # (cam, bin, gain, offset, exposure) -> [sensor temp or None]
    flat_nights = defaultdict(set)     # (cam, bin, filter) -> {night}
    flat_frames = defaultdict(int)
    bias = defaultdict(int)            # (cam, bin, gain, offset) -> frames

    for r in rows:
        cam = r.instrume or "Unknown camera"
        binning = int(r.xbinning or 1)
        gain, offset, exposure = _round(r.gain, 1), _round(r.offset, 0), _round(r.exptime, 2)
        if r.imagetyp == "Light Frame":
            temp = None if r.ccd_temp is None else int(round(float(r.ccd_temp)))
            g = lights.setdefault((cam, binning, gain, offset, exposure, r.filter or "", temp),
                                  {"frames": 0, "integration_sec": 0.0, "nights": set(),
                                   "objects": defaultdict(int)})
            g["frames"] += 1
            g["integration_sec"] += float(r.exptime or 0)
            night = _day(r.date_obs)
            if night:
                g["nights"].add(night)
            if r.object:
                g["objects"][r.object] += 1
        elif r.imagetyp == "Dark Frame":
            darks[(cam, binning, gain, offset, exposure)].append(
                None if r.ccd_temp is None else float(r.ccd_temp))
        elif r.imagetyp == "Flat Frame":
            key = (cam, binning, r.filter or "")
            flat_frames[key] += 1
            night = _day(r.date_obs)
            if night:
                flat_nights[key].add(night)
        elif r.imagetyp == "Bias Frame":
            bias[(cam, binning, gain, offset)] += 1

    setups = []
    for (cam, binning, gain, offset, exposure, flt, temp), g in lights.items():
        dark_n = sum(1 for t in darks.get((cam, binning, gain, offset, exposure), [])
                     if t is None or temp is None or abs(t - temp) <= temp_tolerance)
        flat_n = flat_frames.get((cam, binning, flt), 0)
        fn = flat_nights.get((cam, binning, flt), set())
        gap = (max(min(abs((night - f).days) for f in fn) for night in g["nights"])
               if fn and g["nights"] else None)
        nights = sorted(g["nights"])
        setups.append({
            "camera": cam, "binning": binning, "gain": gain, "offset": offset,
            "exposure_s": exposure, "filter": flt or None, "sensor_temp_c": temp,
            "frames": g["frames"], "integration_sec": g["integration_sec"],
            "nights": len(nights),
            "first_night": nights[0].isoformat() if nights else None,
            "last_night": nights[-1].isoformat() if nights else None,
            "objects": [o for o, _ in sorted(g["objects"].items(), key=lambda kv: -kv[1])[:3]],
            "darks": dark_n, "flats": flat_n, "bias": bias.get((cam, binning, gain, offset), 0),
            "flat_gap_days": gap,
            "stale_flats": gap is not None and gap > flat_max_days,
            "missing": [name for name, count in (("darks", dark_n), ("flats", flat_n)) if count == 0],
        })

    # Problems first, biggest integration first within each half
    setups.sort(key=lambda s: (not s["missing"] and not s["stale_flats"], -s["integration_sec"]))
    return {
        "setups": setups,
        "summary": {
            "setups": len(setups),
            "complete": sum(1 for s in setups if not s["missing"] and not s["stale_flats"]),
            "missing_darks": sum(1 for s in setups if "darks" in s["missing"]),
            "missing_flats": sum(1 for s in setups if "flats" in s["missing"]),
            "stale_flats": sum(1 for s in setups if s["stale_flats"]),
            "uncalibrated_integration_sec": sum(s["integration_sec"] for s in setups if s["missing"]),
        },
        "calibration_frames": {"darks": sum(len(v) for v in darks.values()),
                               "flats": sum(flat_frames.values()), "bias": sum(bias.values())},
        "tolerances": {"temp_c": temp_tolerance, "flat_max_days": flat_max_days},
    }
