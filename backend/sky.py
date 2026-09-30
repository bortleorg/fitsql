"""
Compute observing-geometry fields (altitude, azimuth, airmass, moon separation,
moon illumination, moon altitude) from DATE-OBS + target RA/Dec + site location.

Kept dependency-isolated and fully defensive: any missing input or failure
returns a dict of None so indexing never breaks on these optional fields.
"""
from __future__ import annotations

import logging
import numpy as np

logger = logging.getLogger(__name__)

_iers_configured = False


def _configure_iers():
    """Avoid network stalls — use bundled IERS data, no auto-download."""
    global _iers_configured
    if _iers_configured:
        return
    try:
        from astropy.utils import iers
        iers.conf.auto_download = False
        iers.conf.iers_degraded_accuracy = "ignore"
    except Exception:
        pass
    try:
        from astropy import log as astropy_log
        astropy_log.setLevel("ERROR")  # stop NonRotationTransformationWarning spam
    except Exception:
        pass
    _iers_configured = True


EMPTY = {
    "altitude": None, "azimuth": None, "airmass": None,
    "moon_sep": None, "moon_illum": None, "moon_alt": None,
}


def compute_sky(date_obs, ra_deg, dec_deg, lat, lon, elev) -> dict:
    if date_obs is None or ra_deg is None or dec_deg is None or lat is None or lon is None:
        return dict(EMPTY)
    try:
        import warnings
        _configure_iers()
        import astropy.units as u
        from astropy.time import Time
        from astropy.coordinates import EarthLocation, SkyCoord, AltAz, get_body

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # NonRotationTransformationWarning etc.
            # DATE-OBS is the exposure start; parse flexibly
            t = Time(str(date_obs), format="isot", scale="utc")
            loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg,
                                height=(elev if elev is not None else 0) * u.m)
            target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
            altaz = AltAz(obstime=t, location=loc)

            target_aa = target.transform_to(altaz)
            altitude = float(target_aa.alt.deg)
            azimuth = float(target_aa.az.deg)
            # Airmass only meaningful above the horizon
            airmass = float(target_aa.secz.value) if altitude > 3 else None
            if airmass is not None and (airmass < 0 or airmass > 40):
                airmass = None

            moon = get_body("moon", t, loc)
            sun = get_body("sun", t, loc)
            moon_sep = float(moon.separation(target).deg)
            moon_aa = moon.transform_to(altaz)
            moon_alt = float(moon_aa.alt.deg)
            # Illuminated fraction from sun-moon elongation
            elongation = sun.separation(moon).rad
            moon_illum = float((1.0 + np.cos(np.pi - elongation)) / 2.0)

        return {
            "altitude": round(altitude, 2),
            "azimuth": round(azimuth, 2),
            "airmass": round(airmass, 3) if airmass is not None else None,
            "moon_sep": round(moon_sep, 2),
            "moon_illum": round(moon_illum, 3),
            "moon_alt": round(moon_alt, 2),
        }
    except Exception as e:
        logger.warning(f"Sky computation failed: {e}")
        return dict(EMPTY)
