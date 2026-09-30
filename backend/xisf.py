"""
Minimal XISF 1.0 reader — what the catalog needs from PixInsight and N.I.N.A.
files: the first image's pixels and its FITS keywords.

Monolithic XISF layout: the signature "XISF0100", the XML header length (uint32,
little-endian), 4 reserved bytes, the UTF-8 XML header, then data blocks at
absolute file offsets. Supported: attachment, inline and embedded data blocks;
planar and normal pixel storage; integer and float samples in either byte order;
zlib, lz4 / lz4hc (`lz4` package) and zstd (`zstandard` package — PixInsight's
WBPP masters use zstd+sh) compression, with or without byte shuffling.
"""
from __future__ import annotations

import base64
import struct
import warnings
import zlib
import xml.etree.ElementTree as ET

import numpy as np

SIGNATURE = b"XISF0100"
_NS = "{http://www.pixinsight.com/xisf}"
_SAMPLE_FORMATS = {
    "UInt8": "u1", "UInt16": "u2", "UInt32": "u4", "UInt64": "u8",
    "Int8": "i1", "Int16": "i2", "Int32": "i4", "Int64": "i8",
    "Float32": "f4", "Float64": "f8",
    # Older spellings still found in the wild
    "Byte": "u1", "UShort": "u2", "UInt": "u4", "Short": "i2", "Int": "i4", "Float": "f4", "Double": "f8",
}


class XISFError(ValueError):
    """The file isn't a readable XISF image."""


def _children(elem, name):
    return [c for c in elem if c.tag in (_NS + name, name)]


def read_header(f) -> ET.Element:
    """Parse the XML header of an open XISF file (positioned at its start)."""
    if f.read(8) != SIGNATURE:
        raise XISFError("not a monolithic XISF 1.0 file")
    (length,) = struct.unpack("<I", f.read(4))
    f.read(4)                                           # reserved
    raw = f.read(length)
    if len(raw) != length:
        raise XISFError("truncated XISF header")
    try:
        return ET.fromstring(raw.rstrip(b"\0"))          # some writers NUL-pad the header
    except ET.ParseError as e:
        raise XISFError(f"invalid XISF header XML: {e}") from e


def _keyword_value(text):
    """A FITSKeyword value as FITS would read it: quoted string, T/F, int or float."""
    v = (text or "").strip()
    if v.startswith("'"):
        end = v.rfind("'")
        return (v[1:end] if end > 0 else v[1:]).replace("''", "'").rstrip()
    if v in ("T", "F"):
        return v == "T"
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _header(image, width: int, height: int):
    """astropy Header from the image's FITSKeyword elements, plus the geometry and
    frame type that XISF keeps in attributes rather than keywords."""
    from astropy.io import fits
    hdr = fits.Header()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                  # odd/long keyword names become HIERARCH cards
        for kw in _children(image, "FITSKeyword"):
            name = (kw.get("name") or "").strip().upper()
            if not name:
                continue
            value, comment = _keyword_value(kw.get("value")), kw.get("comment") or ""
            try:
                if name in ("COMMENT", "HISTORY"):
                    getattr(hdr, "add_" + name.lower())(comment or str(value))
                else:
                    hdr.append(fits.Card(name, value, comment), end=True)
            except (ValueError, TypeError):
                continue
        hdr["NAXIS1"] = width
        hdr["NAXIS2"] = height
        if "IMAGETYP" not in hdr and image.get("imageType"):
            hdr["IMAGETYP"] = image.get("imageType")
    return hdr


def _decompress(data: bytes, spec: str) -> bytes:
    """Undo `compression="codec[+sh]:uncompressed-size[:item-size]"`."""
    parts = spec.split(":")
    codec, size = parts[0].lower(), int(parts[1])
    item_size = int(parts[2]) if len(parts) > 2 else 1
    shuffled = codec.endswith("+sh")
    codec = codec[:-3] if shuffled else codec
    if codec == "zlib":
        out = zlib.decompress(data)
    elif codec in ("lz4", "lz4hc"):
        try:
            import lz4.block
        except ImportError as e:
            raise XISFError("LZ4-compressed XISF needs the 'lz4' package") from e
        out = lz4.block.decompress(data, uncompressed_size=size)
    elif codec == "zstd":
        try:
            import zstandard
        except ImportError as e:
            raise XISFError("zstd-compressed XISF needs the 'zstandard' package") from e
        out = zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    else:
        raise XISFError(f"unsupported XISF compression codec '{codec}'")
    if len(out) != size:
        raise XISFError(f"XISF block decompressed to {len(out)} bytes, expected {size}")
    if shuffled and item_size > 1:
        # Byte shuffling stores byte 0 of every sample, then byte 1, ... — interleave back
        n = size // item_size
        body = np.frombuffer(out, dtype=np.uint8, count=n * item_size).reshape(item_size, n).T.tobytes()
        out = body + out[n * item_size:]
    return out


def _data_block(f, image) -> bytes:
    location = (image.get("location") or "").split(":")
    kind = location[0]
    if kind == "attachment":
        position, size = int(location[1]), int(location[2])
        f.seek(position)
        data = f.read(size)
        if len(data) != size:
            raise XISFError("truncated XISF data block")
    elif kind in ("inline", "embedded"):
        if kind == "inline":
            encoding, text = (location[1] if len(location) > 1 else "base64"), image.text
        else:
            node = next(iter(_children(image, "Data")), None)
            if node is None:
                raise XISFError("embedded XISF data block without a Data element")
            encoding, text = node.get("encoding", "base64"), node.text
        text = "".join((text or "").split())
        if encoding == "base64":
            data = base64.b64decode(text)
        elif encoding == "hex":
            data = base64.b16decode(text.upper())
        else:
            raise XISFError(f"unsupported XISF block encoding '{encoding}'")
    else:
        raise XISFError(f"unsupported XISF data block location '{image.get('location')}'")
    compression = image.get("compression")
    return _decompress(data, compression) if compression else data


def read(path: str, with_data: bool = True):
    """
    (header, data) for the first image in an XISF file. `header` is an
    astropy.io.fits.Header built from the image's FITSKeyword elements (plus
    NAXIS1/NAXIS2, and IMAGETYP from the imageType attribute when no keyword
    has it). `data` is (H, W) for one channel or (C, H, W) — the layout astropy
    returns for FITS — or None when with_data is False.
    """
    with open(path, "rb") as f:
        root = read_header(f)
        images = _children(root, "Image")
        if not images:
            raise XISFError("XISF file contains no image")
        image = images[0]
        try:
            width, height, channels = (int(d) for d in (image.get("geometry") or "").split(":"))
        except ValueError:
            raise XISFError(f"only 2-D XISF images are supported (geometry '{image.get('geometry')}')")
        header = _header(image, width, height)
        if not with_data:
            return header, None
        fmt = image.get("sampleFormat", "")
        if fmt not in _SAMPLE_FORMATS:
            raise XISFError(f"unsupported XISF sampleFormat '{fmt}'")
        order = ">" if image.get("byteOrder", "little").lower() == "big" else "<"
        dtype = np.dtype(order + _SAMPLE_FORMATS[fmt])
        raw = _data_block(f, image)

    count = width * height * channels
    if len(raw) < count * dtype.itemsize:
        raise XISFError("XISF data block is smaller than its geometry")
    pixels = np.frombuffer(raw, dtype=dtype, count=count).astype(dtype.newbyteorder("="), copy=False)
    if image.get("pixelStorage", "Planar").lower() == "normal":
        pixels = pixels.reshape(height, width, channels).transpose(2, 0, 1)
    else:
        pixels = pixels.reshape(channels, height, width)
    return header, (pixels[0] if channels == 1 else pixels)
