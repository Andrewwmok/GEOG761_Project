#!/usr/bin/env python3
"""
Extract metadata from phone or camera photographs.

Reads the EXIF data stored in JPEG, PNG, TIFF, WebP and (optionally) HEIC/HEIF
images and reports when, where and how each photograph was taken. The GPS
position can be used to select candidate satellite tiles automatically, and
the compass direction and field of view show which way the camera was facing.

Usage
    Put this script in the same folder as your photos and click Run in your editor
    (VS Code, Spyder, PyCharm, IDLE). It reads every image in that folder and saves
    results.csv there. Or from a terminal:

    python extract_metadata.py photo.jpg
    python extract_metadata.py photos_folder/ --csv metadata.csv
    python extract_metadata.py photos_folder/ --json metadata.json --radius 250

Requirements
    pip install pillow
    pip install pillow-heif        # optional, only needed for iPhone .HEIC files
"""

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags

try:  # iPhone photographs are often saved as HEIC
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif"}

TAGS = ExifTags.TAGS
GPS_TAGS = ExifTags.GPSTAGS
EXIF_IFD = 0x8769   # pointer to the main EXIF block
GPS_IFD = 0x8825    # pointer to the GPS block

# Sensor diagonal of a full-frame (35 mm) camera, used for field of view
FULL_FRAME_DIAGONAL_MM = 43.27
FULL_FRAME_WIDTH_MM = 36.0


# --------------------------------------------------------------------------- helpers
def to_float(value):
    """Convert EXIF rational numbers (or tuples of them) to float."""
    if value is None:
        return None
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return value[0] / value[1] if value[1] else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def dms_to_decimal(dms, ref):
    """Convert degrees, minutes and seconds to signed decimal degrees."""
    if not dms or len(dms) != 3:
        return None
    d, m, s = (to_float(x) for x in dms)
    if d is None or m is None or s is None:
        return None
    decimal = d + m / 60 + s / 3600
    if ref in ("S", "W", b"S", b"W"):
        decimal = -decimal
    return round(decimal, 7)


def clean_text(value):
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        value = value.strip("\x00 ").strip()
        return value or None
    return value


def parse_datetime(text, offset=None):
    """Return an ISO 8601 timestamp, adding the time-zone offset when the camera stored one."""
    text = clean_text(text)
    if not text:
        return None
    try:
        dt = datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return text
    iso = dt.isoformat()
    offset = clean_text(offset)
    return iso + offset if offset else iso


def bounding_box(lat, lon, radius_m):
    """Square box of +/- radius_m around a point, as (min_lon, min_lat, max_lon, max_lat)."""
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    return (round(lon - dlon, 7), round(lat - dlat, 7), round(lon + dlon, 7), round(lat + dlat, 7))


def horizontal_fov(focal_35mm):
    """Horizontal field of view in degrees from the 35 mm equivalent focal length."""
    if not focal_35mm:
        return None
    return round(math.degrees(2 * math.atan(FULL_FRAME_WIDTH_MM / (2 * focal_35mm))), 1)


# --------------------------------------------------------------------------- extraction
def read_exif(img):
    """Return the main EXIF tags and the GPS tags as name -> value dictionaries."""
    exif = img.getexif()
    main = {TAGS.get(k, k): v for k, v in exif.items()}
    try:
        main.update({TAGS.get(k, k): v for k, v in exif.get_ifd(EXIF_IFD).items()})
    except Exception:
        pass
    gps = {}
    try:
        gps = {GPS_TAGS.get(k, k): v for k, v in exif.get_ifd(GPS_IFD).items()}
    except Exception:
        pass
    return main, gps


def extract(path, radius_m=250):
    record = {"file": str(path)}
    try:
        with Image.open(path) as img:
            record["format"] = img.format
            record["width_px"], record["height_px"] = img.size
            main, gps = read_exif(img)
    except Exception as e:
        record["error"] = f"could not open image ({e})"
        return record

    # When and with what
    record["date_taken"] = parse_datetime(
        main.get("DateTimeOriginal") or main.get("DateTimeDigitized") or main.get("DateTime"),
        main.get("OffsetTimeOriginal") or main.get("OffsetTime"),
    )
    record["camera_make"] = clean_text(main.get("Make"))
    record["camera_model"] = clean_text(main.get("Model"))
    record["lens"] = clean_text(main.get("LensModel"))
    record["software"] = clean_text(main.get("Software"))
    record["orientation"] = main.get("Orientation")

    focal = to_float(main.get("FocalLength"))
    focal35 = to_float(main.get("FocalLengthIn35mmFilm"))
    record["focal_length_mm"] = round(focal, 2) if focal else None
    record["focal_length_35mm"] = focal35
    record["horizontal_fov_deg"] = horizontal_fov(focal35)

    # Where, and which way the camera faced
    lat = dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
    lon = dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
    alt = to_float(gps.get("GPSAltitude"))
    if alt is not None and gps.get("GPSAltitudeRef") in (1, b"\x01"):
        alt = -alt
    record["latitude"] = lat
    record["longitude"] = lon
    record["altitude_m"] = round(alt, 1) if alt is not None else None
    record["gps_accuracy_m"] = to_float(gps.get("GPSHPositioningError"))
    direction = to_float(gps.get("GPSImgDirection"))
    record["camera_direction_deg"] = round(direction, 1) if direction is not None else None
    record["direction_reference"] = {"T": "true north", "M": "magnetic north"}.get(
        clean_text(gps.get("GPSImgDirectionRef")))

    if lat is not None and lon is not None:
        record["map_link"] = f"https://www.google.com/maps?q={lat},{lon}"
        record["tile_bbox"] = bounding_box(lat, lon, radius_m)
        record["status"] = "location found"
    elif main or gps:
        record["status"] = "no GPS position (location services off, or removed by a sharing app)"
    else:
        record["status"] = "no metadata (likely stripped by social media, a screenshot or an edit)"
    return record


def find_images(inputs):
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            yield from sorted(f for f in p.glob("*") if f.suffix.lower() in IMAGE_EXTENSIONS)
        elif p.is_file():
            yield p
        else:
            print(f"Skipping {item}: not found", file=sys.stderr)


# --------------------------------------------------------------------------- output
def print_record(r):
    print(f"\n{Path(r['file']).name}")
    if "error" in r:
        print(f"  Error: {r['error']}")
        return
    rows = [
        ("Status", r["status"]),
        ("Date taken", r["date_taken"]),
        ("Camera", " ".join(x for x in (r["camera_make"], r["camera_model"]) if x) or None),
        ("Lens", r["lens"]),
        ("Image size", f"{r['width_px']} x {r['height_px']} px"),
        ("Latitude, longitude", f"{r['latitude']}, {r['longitude']}" if r["latitude"] is not None else None),
        ("Altitude", f"{r['altitude_m']} m" if r["altitude_m"] is not None else None),
        ("GPS accuracy", f"{r['gps_accuracy_m']:.0f} m" if r["gps_accuracy_m"] else None),
        ("Camera direction", f"{r['camera_direction_deg']} deg from {r['direction_reference'] or 'north'}"
         if r["camera_direction_deg"] is not None else None),
        ("Field of view", f"{r['horizontal_fov_deg']} deg horizontal" if r["horizontal_fov_deg"] else None),
        ("Tile bounding box", r.get("tile_bbox")),
        ("Map", r.get("map_link")),
    ]
    for label, value in rows:
        if value not in (None, ""):
            print(f"  {label:<20} {value}")


def main():
    ap = argparse.ArgumentParser(description="Extract date, location and camera metadata from photographs.")
    ap.add_argument("inputs", nargs="*", help="image files and/or folders (default: the folder this script is in)")
    ap.add_argument("--csv", help="save results to this CSV file")
    ap.add_argument("--json", help="save results to this JSON file")
    ap.add_argument("--radius", type=float, default=250,
                    help="half-width in metres of the satellite tile box around each GPS point (default 250)")
    args = ap.parse_args()

    # Clicking Run in an editor passes no arguments, so fall back to the bundled
    # test_images folder and save the results next to the script.
    if not args.inputs:
        here = Path(__file__).resolve().parent
        args.inputs = [str(here)]
        args.csv = args.csv or str(here / "results.csv")
        print(f"No images given, so reading {args.inputs[0]}")

    files = list(find_images(args.inputs))
    if not files:
        sys.exit("No images found.")
    if not HEIC_SUPPORTED and any(f.suffix.lower() in (".heic", ".heif") for f in files):
        print("Note: install pillow-heif to read .HEIC files (pip install pillow-heif)", file=sys.stderr)

    records = [extract(f, args.radius) for f in files]
    for r in records:
        print_record(r)

    located = sum(1 for r in records if r.get("latitude") is not None)
    print(f"\n{len(records)} image(s) read, {located} with a GPS position.")

    if args.csv:
        fields = sorted({k for r in records for k in r}, key=lambda k: list(records[0]).index(k)
                        if k in records[0] else 999)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
        print(f"Saved {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)
        print(f"Saved {args.json}")


if __name__ == "__main__":
    main()
