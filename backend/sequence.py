"""
Sequence screening — judge each frame against the frames captured around it.

A star count or HFR means little on its own (targets, filters, exposures and
skies all differ), but a sudden change against the same target's neighbouring
frames is what a passing cloud, a thin veil, dew, or a headlight looks like.

Frames are grouped by (target, filter, rig, exposure), ordered by capture time
and split into sessions at long gaps. Each frame is compared with two references:

* a rolling baseline — median of the last N frames that were *not* flagged, so a
  slow cloud bank can't drag the baseline down with it;
* the session median — so a gradual decline still shows up against the night
  as a whole.

The stricter of the two wins (more stars / flux, lower HFR / empty cells /
spread / background dip). After 2×N flagged frames in a row the conditions really
have changed (refocus, meridian flip) and both references re-seed from the most
recent frames.
"""
from __future__ import annotations

import warnings
from collections import deque

import numpy as np

# series name -> higher is better?
_METRICS = {"stars": True, "hfr": False, "empty": False, "spread": False, "flux": True, "dip": False}


def sessions(keys, times, gap_hours: float) -> list:
    """Frame indices grouped by key, in capture order, split at gaps > gap_hours.
    Frames without a capture time can't be sequenced and are left out."""
    groups = {}
    for i, (k, t) in enumerate(zip(keys, times)):
        if not np.isnat(t):
            groups.setdefault(k, []).append(i)
    gap = np.timedelta64(int(gap_hours * 3_600_000), "ms")
    out = []
    for idx in groups.values():
        idx.sort(key=lambda i: times[i])
        current = [idx[0]]
        for prev, i in zip(idx, idx[1:]):
            if times[i] - times[prev] > gap:
                out.append(current)
                current = []
            current.append(i)
        out.append(current)
    return out


def _median(values):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN metric
        return float(np.nanmedian(values)) if len(values) else np.nan


def screen(keys, times, stars, hfr, empty, spread, flux=None, dip=None, *, gap_hours: float,
           baseline_frames: int, star_drop: float, hfr_rise: float, empty_rise: float, spread_rise: float,
           flux_drop: float = 0.3, dip_rise: float = 0.015) -> dict:
    """
    Returns per-frame arrays aligned with the inputs:
      star_drop   – fraction of stars lost vs the reference (0.4 = 40% fewer)
      hfr_rise    – fractional HFR increase (0.3 = 30% softer)
      empty_rise  – increase in the empty-patch share of the grid (0.2 = +20 points)
      spread_rise – fractional increase of the background spread
      flux_drop   – fraction of bright-star flux lost (thin veil / transparency)
      dip_rise    – increase of the background dip (0.03 = 3 points deeper: frost / dew)
      seq_anomaly – any of the above past its threshold
      seq_ok      – judged and not anomalous
    Frames that can't be judged (no time, missing star count, < 3 frames in the
    session) are NaN and neither anomalous nor ok.
    """
    n = len(keys)
    nan = np.full(n, np.nan)
    series = {"stars": np.asarray(stars, dtype=float), "hfr": np.asarray(hfr, dtype=float),
              "empty": np.asarray(empty, dtype=float), "spread": np.asarray(spread, dtype=float),
              "flux": nan if flux is None else np.asarray(flux, dtype=float),
              "dip": nan if dip is None else np.asarray(dip, dtype=float)}
    limits = {"star_drop": star_drop, "hfr_rise": hfr_rise, "empty_rise": empty_rise,
              "spread_rise": spread_rise, "flux_drop": flux_drop, "dip_rise": dip_rise}
    out = {name: np.full(n, np.nan) for name in limits}
    anomaly = np.zeros(n, dtype=bool)
    judged = np.zeros(n, dtype=bool)

    def medians(idx):
        return {m: _median(series[m][idx]) for m in _METRICS}

    for session in sessions(keys, times, gap_hours):
        idx = [i for i in session if np.isfinite(series["stars"][i])]
        if len(idx) < 3:
            continue
        seed = medians(idx)
        window = deque(maxlen=baseline_frames)
        streak = []
        for i in idx:
            rolling = medians(list(window)) if len(window) >= min(3, baseline_frames) else None
            ref = {}
            for m, higher_better in _METRICS.items():
                candidates = [v for v in (seed[m], rolling[m] if rolling else np.nan) if np.isfinite(v)]
                ref[m] = (max if higher_better else min)(candidates) if candidates else np.nan

            def loss(m):      # fraction below a higher-is-better reference
                return 1.0 - series[m][i] / ref[m] if ref[m] > 0 else np.nan

            def gain(m):      # fraction above a lower-is-better reference
                return series[m][i] / ref[m] - 1.0 if ref[m] > 0 else np.nan

            with np.errstate(invalid="ignore", divide="ignore"):
                dev = {"star_drop": loss("stars"), "hfr_rise": gain("hfr"),
                       "empty_rise": series["empty"][i] - ref["empty"],
                       "spread_rise": gain("spread"), "flux_drop": loss("flux"),
                       "dip_rise": series["dip"][i] - ref["dip"]}
            bad = False
            for name, value in dev.items():
                out[name][i] = value
                bad |= bool(np.isfinite(value) and value > limits[name])
            judged[i] = True
            anomaly[i] = bad

            if not bad:
                window.append(i)
                streak = []
                continue
            streak.append(i)
            if len(streak) >= 2 * baseline_frames:
                recent = streak[-baseline_frames:]
                window = deque(recent, maxlen=baseline_frames)
                seed = medians(recent)
                streak = []

    out["seq_anomaly"] = anomaly
    out["seq_ok"] = judged & ~anomaly
    return out
