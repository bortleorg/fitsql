"""
Tier 2 plate solving via ASTAP (local CLI solver).

Runs `astap.exe` against a light frame, optionally hinted with the mount-reported
RA/Dec and field-of-view for a fast near-instant solve. Reads ASTAP's `.wcs`
output (never modifies the original FITS) and parses it into the same WCS dict
shape as wcs_util.parse_wcs.

Requires ASTAP installed with a star database (e.g. H18). The astap.exe path is
configured in app settings.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import subprocess
import logging

logger = logging.getLogger(__name__)


def astap_available(astap_path: str | None) -> bool:
    return bool(astap_path) and os.path.exists(astap_path)


def _read_wcs_header(path):
    from astropy.io import fits
    with open(path, "r", errors="ignore") as fh:
        text = fh.read()

    # ASTAP .wcs embeds long COMMENT/HISTORY strings via CONTINUE cards that break
    # astropy's parser entirely. Filter at the text level — split into 80-char FITS
    # cards and keep only real WCS keywords — so astropy never sees the bad cards.
    if "\n" in text.strip():
        cards = text.replace("\r", "").split("\n")
    else:
        cards = [text[i:i + 80] for i in range(0, len(text), 80)]

    keep_prefixes = (
        "SIMPLE", "BITPIX", "NAXIS", "EXTEND", "WCSAXES",
        "CTYPE", "CRVAL", "CRPIX", "CDELT", "CD1_", "CD2_", "CD3_",
        "PC1_", "PC2_", "PC3_", "PV", "CROTA", "CUNIT", "CDi_",
        "A_", "B_", "AP_", "BP_",      # SIP distortion
        "EQUINOX", "RADESYS", "RADECSYS", "LONPOLE", "LATPOLE", "MJD", "DATE",
    )
    kept = [c for c in cards if c[:8].strip().upper().startswith(keep_prefixes)]
    kept.append("END".ljust(80))
    clean_text = "".join(c.ljust(80) for c in kept)
    return fits.Header.fromstring(clean_text)


def solve(astap_path, image_path, ra_deg=None, dec_deg=None, fov_deg=None,
          naxis1=None, naxis2=None, db_dir=None, timeout=120) -> dict:
    """
    Solve one frame. Returns {ok, wcs|error}. wcs matches wcs_util.parse_wcs shape.
    """
    if not astap_available(astap_path):
        return {"ok": False, "error": "ASTAP path not configured or not found"}
    if not os.path.exists(image_path):
        return {"ok": False, "error": "file missing"}

    tmpdir = tempfile.mkdtemp(prefix="astap_")
    base = os.path.join(tmpdir, "solve")
    args = [astap_path, "-f", image_path, "-o", base, "-wcs"]
    if db_dir:
        args += ["-d", str(db_dir)]
    if ra_deg is not None and dec_deg is not None:
        # ASTAP wants RA in hours and south-polar-distance (dec + 90)
        args += ["-ra", f"{ra_deg / 15.0:.6f}", "-spd", f"{dec_deg + 90.0:.6f}", "-r", "15"]
    else:
        args += ["-r", "180"]  # blind
    if fov_deg and fov_deg > 0:
        args += ["-fov", f"{fov_deg:.4f}"]

    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, text=True)
        # ASTAP may write outputs at the -o base or beside the input
        bases = [base, os.path.splitext(image_path)[0]]
        wcs_file = next((b + ".wcs" for b in bases if os.path.exists(b + ".wcs")), None)
        ini_file = next((b + ".ini" for b in bases if os.path.exists(b + ".ini")), None)

        solved = False
        if ini_file:
            ini = open(ini_file, errors="ignore").read().replace(" ", "")
            solved = "PLTSOLVD=T" in ini

        if solved and wcs_file:
            import wcs_util
            hdr = _read_wcs_header(wcs_file)
            # ASTAP's .wcs has the solution but often no NAXIS — supply image dims
            # from the original FITS so parse_wcs can derive center + FOV.
            if not naxis1 or not naxis2:
                try:
                    from astropy.io import fits
                    ih = fits.getheader(image_path, ignore_missing_simple=True)
                    naxis1 = naxis1 or ih.get("NAXIS1")
                    naxis2 = naxis2 or ih.get("NAXIS2")
                except Exception:
                    pass
            res = wcs_util.parse_wcs(hdr, naxis1, naxis2)
            _cleanup_beside(image_path)
            if res:
                res["solved"] = 1
                return {"ok": True, "wcs": res}
            preview = " ".join(str(hdr).split())[:200]
            logger.warning("solved but WCS unparseable (naxis=%s,%s) hdr: %s", naxis1, naxis2, preview)
            return {"ok": False, "error": "solved but WCS unparseable"}

        _cleanup_beside(image_path)
        tail = (proc.stdout or proc.stderr or "")[-160:].strip()
        return {"ok": False, "error": f"no solution ({tail})" if tail else "no solution"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "solver timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cleanup_beside(image_path):
    """Remove ASTAP side-output files if it ignored -o and wrote next to input."""
    stem = os.path.splitext(image_path)[0]
    for ext in (".wcs", ".ini"):
        try:
            p = stem + ext
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
