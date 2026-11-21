"""Reproduce the spot check on topographic sheet N-57-102 and compare two DEMs."""
import argparse
import gzip
import json
from pathlib import Path

import numpy as np
from pyproj import Transformer

from .terrain import DATA, TO_GEOGRAPHIC, TO_METRIC, Terrain, bilinear

SHEET = "https://maps.vlasenko.net/smtm100/n-57-102.jpg"
# Locations of the symbols in the original 2816 x 3114 scan, read manually.
MARKS = (
    ("Мишенная сопка", 819, 2636, 382.5),
    ("Никольская сопка", 840, 2837, 103.6),
    ("Синичкина сопка", 1110, 2462, 278.5),
    ("Зеркальная сопка", 964, 2656, 186.4),
    ("Отметка между Култучным озером и портом", 906, 2784, 269.9),
)
# Nearby two-kilometre grid, in the coordinate system printed on the sheet.
SHEET_TO_WGS84 = Transformer.from_crs(28427, 4326, always_xy=True)


def sheet_coordinate(pixel_x, pixel_y):
    x, y = pixel_x - 570, pixel_y - 2370
    easting = 27476000 + (x - (278 + .011 * (y - 234))) * 2000 / 156
    northing = 5880000 + (234 - .005 * (x - 278) - y) * 2000 / 157
    return SHEET_TO_WGS84.transform(easting, northing)


def audit():
    terrain = Terrain()
    checks = []
    for name, px, py, reference in MARKS:
        lon, lat = sheet_coordinate(px, py)
        x, y = TO_METRIC.transform(lon, lat)
        offsets = np.arange(-60, 61, 5)
        dx, dy = np.meshgrid(offsets, offsets)
        nearby_lon, nearby_lat = TO_GEOGRAPHIC.transform(x + dx, y + dy)
        values = terrain.sample(nearby_lon, nearby_lat)[dx**2 + dy**2 <= 60**2]
        dsm = float(terrain.sample(lon, lat))
        checks.append({
            "type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"name": name, "map_m": reference, "dsm_m": round(dsm, 2),
                           "delta_m": round(dsm - reference, 2),
                           "nearby_min_m": round(float(np.nanmin(values)), 2),
                           "nearby_max_m": round(float(np.nanmax(values)), 2),
                           "scan_pixel_x": px, "scan_pixel_y": py},
        })
    return {
        "type": "FeatureCollection", "features": checks,
        "metadata": {"sheet": "N-57-102", "source": SHEET, "edition_year": 1990, "survey_epoch_year": 1987,
                     "scale": "1:100000", "neighborhood_m": 60,
                     "status": "historical_topographic_spot_check"},
    }


def compare_dem(directory, checks):
    terrain = Terrain()
    convert_geoid = Transformer.from_pipeline(
        f"+proj=pipeline +step +proj=vgridshift +grids={directory / 'egm96.tif'} +multiplier=1 "
        f"+step +inv +proj=vgridshift +grids={directory / 'egm08.tif'} +multiplier=1")
    with gzip.open(directory / "N53E158.hgt.gz", "rb") as stream:
        samples = np.frombuffer(stream.read(), dtype=">i2").reshape(3601, 3601)

    def skadi(lon, lat):
        values = bilinear(samples, (lon - 158) * 3600, (54 - lat) * 3600, -32768)
        _, _, corrected = convert_geoid.transform(lon, lat, values)
        return corrected

    for f in checks["features"]:
        lon, lat = f["geometry"]["coordinates"]
        print(f["properties"]["name"], round(float(skadi(lon, lat)), 2))
    lon, lat = np.meshgrid(np.linspace(158.62, 158.68, 97), np.linspace(53.018, 53.063, 123))
    source = terrain.sample(lon, lat)
    reference = skadi(lon, lat)
    land = (source > 10) & np.isfinite(reference)
    residual = (source - reference)[land]
    print(json.dumps({
        "n": int(residual.size), "bias_m": round(float(np.mean(residual)), 2),
        "median_m": round(float(np.median(residual)), 2), "mae_m": round(float(np.mean(np.abs(residual))), 2),
        "p90_absolute_m": round(float(np.quantile(np.abs(residual), .9)), 2),
    }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", type=Path, help="directory with Skadi N53E158 and two PROJ geoid grids")
    args = parser.parse_args()
    checks = audit()
    (DATA / "topo-audit.geojson").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n")
    if args.compare:
        compare_dem(args.compare.resolve(), checks)
