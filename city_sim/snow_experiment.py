"""Adapter to the pinned Danil engine and a separate terrain sensitivity model."""
import base64
from collections import Counter
from functools import cached_property, lru_cache
from hashlib import file_digest
from io import BytesIO
import json
import math
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from shapely.affinity import scale, translate
from shapely import intersects_xy, union_all
from shapely.geometry import box, shape

from .geodata import polygons
from .operations import AREAS, digest
from .terrain import DATA

ENGINE_LOCK = json.loads((DATA / "snow-engine-lock.json").read_text())
DANIL_REV = ENGINE_LOCK["commit"]
ALGORITHM = "drift-pilot-1"
TOP = 10


class SnowStudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    district_id: str = "center"
    wind_from_deg: float = Field(default=270, ge=0, lt=360)
    wind_speed_ms: float = Field(default=8, ge=0, le=70)
    snowfall_cm_h: float = Field(default=2, ge=0, le=30)
    hours: int = Field(default=3, ge=1, le=6)
    resolution: int = Field(default=96, ge=40, le=160)
    forecast_sample_id: str | None = None
    # mm water equivalent multiplied by snow-to-liquid ratio gives mm fresh snow.
    snow_to_liquid_ratio: float = Field(default=10, ge=2, le=30)


def rows_json(values):
    return [[round(float(value), 4) if math.isfinite(value) else None for value in row] for row in values]


def overlay(values):
    flag = np.isfinite(values)
    data = np.nan_to_num(values).clip(0, 1)
    colors = np.array([[38, 71, 94], [79, 148, 187], [255, 211, 132], [242, 105, 77]])
    tone = np.stack([np.interp(data, [0, .12, .40, 1], channel) for channel in colors.T], axis=-1)
    rgba = np.dstack((tone, flag * (115 + data * 130))).astype("uint8")
    out = BytesIO()
    Image.fromarray(rgba).save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def top_rows(roads, field):
    return [{"edge_id": key, "name": name, "lon": round(lon, 6), "lat": round(lat, 6),
             "value": round(float(field[j, i]), 4)}
            for key, name, lon, lat, j, i in sorted(roads, key=lambda r: (-field[r[4], r[5]], r[0]))[:TOP]]


def overlap(a, b):
    one, two = ({r["edge_id"] for r in rows} for rows in (a, b))
    return round(len(one & two) / len(one | two), 4) if one or two else None


class SnowStudy:
    def __init__(self, city, archive, root=None):
        self.city = city
        self.archive = archive
        self.root = Path(root or os.getenv("CITY_SIM_DANIL", Path(__file__).parent / ".state/danil"))
        self._cached = lru_cache(maxsize=12)(self._run)

    @property
    def available(self):
        return (self.root / "snowmap/model.py").exists() and (self.root / "kernels").is_dir()

    @cached_property
    def engine(self):
        if not self.available:
            raise FileNotFoundError("Расчёт Данила не подключён. См. инструкцию запуска.")
        if self.provenance != ENGINE_LOCK["sha256"]:
            raise FileNotFoundError("Код или шаблоны Данила не совпадают с закреплённой версией.")
        sys.path.insert(0, str(self.root.resolve()))
        from snowmap import foam, model, sources
        foam.KERNELS = model.KERNELS = self.root.resolve() / "kernels"
        return model, sources

    @cached_property
    def provenance(self):
        result = {}
        for path in sorted([*self.root.glob("kernels/*.json"), *self.root.glob("snowmap/*.py")]):
            with path.open("rb") as f:
                result[str(path.relative_to(self.root))] = file_digest(f, "sha256").hexdigest()
        return result

    @cached_property
    def directional_control(self):
        """An isolated north–south building must swap west/east accumulation."""
        model, sources = self.engine
        bounds = (158.64, 53.03, 158.644, 53.032)
        _, _, width, height = sources.projection(bounds)
        cx, cy = width/2, height/2
        building = sources.Building(box(cx-6, cy-12, cx+6, cy+12), 15, "control")
        fields = []
        for angle in (270, 90):
            result = model.estimate(bounds, [building], [
                {"wind_from_deg": angle, "wind_speed_ms": 8, "snowfall_cm": 6}], 96, True)
            grid = np.array(result["snow_depth_cm"], dtype=float)
            rows, cols = grid.shape
            xs = (np.arange(cols)+.5)*width/cols
            ys = height-(np.arange(rows)+.5)*height/rows
            x, y = np.meshgrid(xs-cx, ys-cy)
            west = float(np.nanmean(grid[(x < -15) & (x > -60) & (abs(y) < 8)]))
            east = float(np.nanmean(grid[(x > 15) & (x < 60) & (abs(y) < 8)]))
            fields.append({"wind_from_deg": angle, "west_cm": round(west, 3), "east_cm": round(east, 3)})
        dry = model.estimate(bounds, [building], [
            {"wind_from_deg": 270, "wind_speed_ms": 8, "snowfall_cm": 0}], 40, True)
        dry_field = np.array(dry["snow_depth_cm"], dtype=float)
        return {"passes": fields[0]["east_cm"] > fields[0]["west_cm"] and
                fields[1]["west_cm"] > fields[1]["east_cm"],
                "no_new_snow_zero": bool(np.all(dry_field[np.isfinite(dry_field)] == 0)),
                "sides": fields}

    def run(self, spec):
        return self._cached(self.city.data_revision, spec.model_dump_json())

    def _run(self, revision, payload):
        spec = SnowStudyRequest.model_validate_json(payload)
        if spec.district_id not in AREAS:
            raise ValueError("Неизвестный район.")
        model, sources = self.engine
        district, (lon, lat) = AREAS[spec.district_id]
        bounds = (lon-.006, lat-.004, lon+.006, lat+.004)
        west, south, east, north = bounds
        sx, sy, width, height = sources.projection(bounds)
        speed, direction, snow, hours = spec.wind_speed_ms, spec.wind_from_deg, spec.snowfall_cm_h, spec.hours
        weather = None
        if spec.forecast_sample_id:
            note = self.archive.samples.get(spec.forecast_sample_id)
            if note is None or note.weather.kind != "forecast" or note.snow_mm_3h is None:
                raise ValueError("Для выбранного срока нет количества снегопада.")
            from winter_audit.relief import distance
            if distance((lon, lat), note.weather.coordinate_lon_lat) > 3000:
                raise ValueError("Погодная точка за пределами радиуса района.")
            speed, direction = note.wind.speed_mps, note.wind.direction_from_deg
            snow, hours = note.snow_mm_3h * spec.snow_to_liquid_ratio / 30.0, 3
            weather = note.model_dump(mode="json")
        accumulation = snow * hours
        buffer_x, buffer_y = 360/sx, 360/sy
        neighbors = self.city.buildings((west-buffer_x, south-buffer_y, east+buffer_x, north+buffer_y))
        buildings = []
        inspected_buildings = {}
        target = box(*bounds)
        for item in neighbors["features"]:
            p = item["properties"]
            source = p["height_source"]
            height_m = p["height_m"] if source != "unknown" else 9.0
            footprint = shape(item["geometry"])
            if target.covers(footprint) and footprint.area:
                inspected_buildings[item["id"]] = footprint.bounds
            for building in polygons(footprint):
                local = scale(translate(building, xoff=-west, yoff=-south),
                              xfact=sx, yfact=sy, origin=(0, 0))
                if local.area >= 4:
                    buildings.append(sources.Building(local, height_m, source))

        def estimate(heading, wind, structures=buildings, use_foam=True):
            return model.estimate(bounds, structures, [{"wind_from_deg": heading, "wind_speed_ms": wind,
                                  "snowfall_cm": accumulation}], spec.resolution, use_foam)

        current = estimate(direction, speed)
        opposite = estimate((direction+180) % 360, speed)
        calm = estimate(direction, 0)
        analytic = estimate(direction, speed, use_foam=False)
        changed = [sources.Building(b.footprint, b.height if b.height_source == "osm_height"
                                    else b.height * .75, b.height_source) for b in buildings]
        lower = estimate(direction, speed, changed)
        actual, reverse, still, simple, short = [np.array(value["snow_depth_cm"], dtype=float)
                                                for value in (current, opposite, calm, analytic, lower)]
        rows, cols = actual.shape
        lons = west + (np.arange(cols)+.5)*(east-west)/cols
        lats = north - (np.arange(rows)+.5)*(north-south)/rows
        x, y = np.meshgrid(lons, lats)
        terrain = self.city.terrain
        center = terrain.sample(x, y)
        surrounding = np.array([terrain.sample(x+dx/sx, y+dy/sy)
                                for dx, dy in ((90, 0), (-90, 0), (0, 90), (0, -90))])
        relief = np.clip((surrounding.mean(axis=0)-center)/12, 0, 1)
        wet = union_all([shape(f["geometry"]) for f in self.city.osm_layers[1]["features"]
                         if f["properties"]["kind"] == "water" and shape(f["geometry"]).intersects(box(*bounds))])
        ground = ~intersects_xy(wet, x, y)
        valid = np.isfinite(actual) & ground
        actual = np.where(valid, actual, np.nan)
        terrain_ok = np.isfinite(relief)
        plain = np.where(valid, 0., np.nan)
        relief = np.where(valid & terrain_ok, relief, np.nan)

        def index(depth):
            if accumulation <= 0 or speed <= 2:
                return plain.copy()
            anomaly = np.clip((depth / accumulation - 1)/1.5, 0, 1)
            return np.where(valid, .75*anomaly+.25*np.nan_to_num(relief), np.nan)

        combined, alternate, shorter = map(index, (actual, reverse, short))
        roads = []
        seen = set()
        graph = self.city.roads(bounds)
        for f in sorted(graph["features"], key=lambda x: x["id"]):
            props = f["properties"]
            first, last = f["geometry"]["coordinates"]
            ax, ay = [(a+b)/2 for a, b in zip(first, last)]
            if not (west <= ax < east and south < ay <= north):
                continue
            i = int((ax-west)/(east-west)*cols)
            j = int((north-ay)/(north-south)*rows)
            key = props["physical_road_id"]
            if valid[j, i] and key not in seen:
                roads.append((f["id"], props["road_name"], ax, ay, j, i))
                seen.add(key)

        reverse = np.where(valid, reverse, np.nan)
        strips = {"west": lambda a, b, c, d: (x >= a-30/sx) & (x < a-5/sx) & (y >= b) & (y <= d),
                  "east": lambda a, b, c, d: (x > c+5/sx) & (x <= c+30/sx) & (y >= b) & (y <= d),
                  "south": lambda a, b, c, d: (y >= b-30/sy) & (y < b-5/sy) & (x >= a) & (x <= c),
                  "north": lambda a, b, c, d: (y > d+5/sy) & (y <= d+30/sy) & (x >= a) & (x <= c)}
        sides = {}
        for key, outline in inspected_buildings.items():
            bands = {}
            for side, strip in strips.items():
                mask = strip(*outline) & valid
                if mask.sum() >= 2:
                    bands[side] = {"cells": int(mask.sum()),
                                   "combined_cm": round(float(actual[mask].mean()), 3),
                                   "opposite_cm": round(float(reverse[mask].mean()), 3)}
            if len(bands) >= 2:
                sides[key] = bands

        total = int(valid.sum())
        ranking = {name: top_rows([r for r in roads if np.isfinite(field[r[4], r[5]])], field)
                   for name, field in (("uniform", plain), ("terrain", relief), ("combined", combined),
                                       ("opposite", alternate), ("height_minus_25pct", shorter))}
        check = lambda values: round(float(np.mean(values[valid])), 4) if total else None
        if accumulation <= 0 or speed <= 2:
            ranking["combined"] = ranking["opposite"] = ranking["height_minus_25pct"] = []
        road_cells = {edge: (j, i) for edge, _, _, _, j, i in roads}
        for name, depth in (("combined", actual), ("opposite", reverse)):
            for row in ranking[name]:
                j, i = road_cells[row["edge_id"]]
                row["fresh_snow_cm"] = round(float(depth[j, i]), 3)
                row["excess_cm"] = round(float(depth[j, i] - accumulation), 3)
                row["terrain_weight"] = round(float(relief[j, i]), 3) if terrain_ok[j, i] else None
        source_counts = dict(Counter(b.height_source for b in buildings))
        provenance = {"engine_commit": DANIL_REV, "engine_hashes": self.provenance,
                      "city_revision": revision, "graph_version": graph["metadata"]["graph_version"],
                      "algorithm": ALGORITHM}
        result = {
            "district_id": spec.district_id, "district": district, "bounds": bounds,
            "input": spec.model_dump(mode="json"), "weather": weather,
            "resolved_weather": {"wind_from_deg": direction, "wind_speed_ms": speed,
                                 "snowfall_cm": accumulation, "hours": hours},
            "provenance": provenance, "model": current["model"], "cfd_coverage": current["cfd_coverage"],
            "field_validated": False, "rows": rows, "cols": cols, "building_height_sources": source_counts,
            "metrics": {
                "ground_cells": total, "road_segments_covered": len(roads), "building_sides_covered": len(sides),
                "dem_coverage": check(terrain_ok), "calm_flat_pass": bool(np.allclose(still[valid], accumulation, atol=.001)),
                "building_mask_stable": bool(np.array_equal(valid, np.isfinite(reverse) & ground)),
                "reverse_mae_cm": check(np.abs(actual-reverse)),
                "hybrid_analytic_mae_cm": check(np.abs(actual-simple)),
                "shorter_buildings_mae_cm": check(np.abs(actual-short)),
                "top_k_reverse_overlap": overlap(ranking["combined"], ranking["opposite"]),
                "top_k_relief_overlap": overlap(ranking["combined"], ranking["terrain"]),
                "top_k_uniform_overlap": overlap(ranking["combined"], ranking["uniform"]),
                "top_k_shorter_overlap": overlap(ranking["combined"], ranking["height_minus_25pct"]),
            },
            "ranking": ranking, "directional_control": self.directional_control, "building_sides": sides,
            "grids": {name: rows_json(field) for name, field in
                      (("snow_cm", actual), ("uniform", plain), ("terrain", relief),
                       ("combined", combined), ("opposite", alternate))},
            "validation_scope": "model_controls_and_sensitivity",
        }
        run_id = digest(result)
        from importlib.metadata import version
        return {**result, "run_id": run_id,
                "runtime": {"python": sys.version.split()[0], **{
                    package: version(package) for package in ("numpy", "shapely", "pyproj", "pillow")}},
                "overlays": {name: overlay(field) for name, field in
                             (("uniform", plain), ("terrain", relief), ("combined", combined),
                              ("opposite", alternate), ("snow_cm", np.clip(actual/15, 0, 1)))}}
