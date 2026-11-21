"""Rebuild the small local DEM subset from the two original Copernicus tiles."""
import argparse
import gzip
import json
import math

import numpy as np
from pathlib import Path

from winter_audit.relief import DEM_URL, Surface
from .terrain import BOUNDS, DATA


def prepare(north, south):
    west, bottom, east, top = BOUNDS
    tiles, arrays = [], {}
    for latitude, path in ((52, south), (53, north)):
        surface = Surface(path)
        if (surface.west, surface.north) != (158, latitude + 1):
            raise ValueError(f"Unexpected tile origin for {path}")
        tile_id = f"N{latitude}E158"
        left = max(0, math.floor((west - surface.west) / surface.dx) - 1)
        right = min(surface.image.width, math.ceil((east - surface.west) / surface.dx) + 2)
        upper = max(0, math.floor((surface.north - top) / surface.dy) - 1)
        lower = min(surface.image.height, math.ceil((surface.north - bottom) / surface.dy) + 2)
        arrays[tile_id] = np.asarray(surface.image.crop((left, upper, right, lower)))
        tiles.append(dict(id=tile_id, west=surface.west + left * surface.dx,
                          north=surface.north - upper * surface.dy, step_lon=surface.dx,
                          step_lat=surface.dy, nodata=surface.nodata,
                          sha256=surface.sha256, source=DEM_URL.replace("N53_", f"N{latitude}_")))
    south_tile, north_tile = tiles
    south_samples, north_samples = arrays["N52E158"], arrays["N53E158"]
    if (south_tile["west"], south_tile["step_lon"], south_tile["step_lat"], south_samples.shape[1]) != (
        north_tile["west"], north_tile["step_lon"], north_tile["step_lat"], north_samples.shape[1]
    ):
        raise ValueError("The two source tiles use different sample lattices")
    last_lat = north_tile["north"] - (north_samples.shape[0] - 1) * north_tile["step_lat"]
    if math.isclose(last_lat - north_tile["step_lat"], south_tile["north"], abs_tol=1e-9):
        arrays["N53E158"] = np.concatenate((north_samples, south_samples[:1]), axis=0)
    elif not math.isclose(last_lat, south_tile["north"], abs_tol=1e-9):
        raise ValueError("Non-contiguous Copernicus tiles")
    DATA.mkdir(exist_ok=True)
    np.savez_compressed(DATA / "terrain.npz", **arrays)
    (DATA / "terrain.json").write_text(json.dumps({
        "source": "Copernicus DEM GLO-30", "kind": "digital_surface_model",
        "horizontal_crs": "EPSG:4326", "vertical_crs": "EPSG:3855", "vertical_datum": "EGM2008",
        "nominal_resolution_m": 30, "bounds": BOUNDS, "tiles": tiles,
        "attribution": "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved",
    }, indent=2) + "\n")


def prepare_grades(path):
    features = json.loads(path.read_text())["features"]
    versions = {item["properties"]["graph_version"] for item in features}
    if len(versions) != 1:
        raise ValueError("Expected a single audited graph version")
    edges = {}
    for item in features:
        props = item["properties"]
        edges[props["edge_id"]] = {
            "grade_pct": props["mean_grade_pct"], "quality": props["quality"],
            "start_m": round(props["dsm_start_m"], 2) if props["dsm_start_m"] is not None else None,
            "end_m": round(props["dsm_end_m"], 2) if props["dsm_end_m"] is not None else None,
        }
    payload = json.dumps({"graph_version": versions.pop(), "edges": edges}, separators=(",", ":")).encode()
    (DATA / "road-grades.json.gz").write_bytes(gzip.compress(payload, mtime=0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("north", type=Path)
    parser.add_argument("south", type=Path)
    parser.add_argument("--road-slopes", type=Path)
    args = parser.parse_args()
    prepare(args.north, args.south)
    if args.road_slopes:
        prepare_grades(args.road_slopes)
