"""Display the archived atlas directly on the city scene at recorded timestamps."""
from base64 import b64encode
import csv
from datetime import datetime
from functools import cached_property, lru_cache
from hashlib import file_digest
from io import BytesIO
from math import isfinite

import numpy as np
from PIL import Image, ImageFilter
from shapely import intersects_xy

from weather_coverage.heatmap_fields import FIELDS, SOURCE_FIELDS, shown_value
from weather_coverage.heatmap_interpolate import interpolate
from .live_weather import LiveWeather, PROVIDER
from .operations import ZONE
from .terrain import BOUNDS, TO_METRIC

HISTORY_COLUMNS = dict(zip(FIELDS, ("temp_c", "feels_like_c", "wind_ms", "gust_ms", "wind_deg",
                                    "snowfall_cm_1h", "rain_mm_1h", "visibility_m", "humidity_pct",
                                    "cloud_pct", "pop_pct", "precip_mm_1h")))
HISTORY_UNITS = {"snow": ("Снег за час", "см/ч"), "rain": ("Дождь за час", "мм/ч")}
DATASETS = {"forecast": "OpenWeather Forecast", "history": "Open-Meteo Historical Forecast", "live": PROVIDER,
            "storm": "Open-Meteo · реанализ ECMWF IFS"}
STORM_FIELDS = {"temp": "temperature_c", "wind_speed": "wind_speed_ms", "wind_gust": "gust_ms",
                "wind_deg": "wind_from_deg", "snow": "snowfall_cm", "precip": "precipitation_mm"}
GRID_W, GRID_H = 288, 376


def number(column):
    if not column:
        return None
    value = float(column)
    return value if isfinite(value) else None


def describe(key, dataset):
    name, unit, span, colors = FIELDS[key]
    if dataset in {"history", "live", "storm"}:
        name, unit = HISTORY_UNITS.get(key, (name, unit))
    return {"name": name, "unit": unit, "range": span, "colors": colors}


class WeatherAtlas:
    def __init__(self, root, land, storms=lambda: {}):
        self.root = root
        self.land = land
        self.live = LiveWeather()
        self.storms = storms

    @cached_property
    def datasets(self):
        result = {}
        for dataset, filename, time_key in (("forecast", "forecast_frames.csv", "forecast_dt_utc"),
                                           ("history", "history/historical_frames.csv", "time_utc")):
            path = self.root / filename
            frames = {}
            revision = None
            if path.is_file():
                with path.open("rb") as stream:
                    revision = file_digest(stream, "sha256").hexdigest()
                with path.open(newline="", encoding="utf-8") as stream:
                    for row in csv.DictReader(stream):
                        if dataset == "forecast" and row.get("source_product") != "Forecast":
                            continue
                        stamp = datetime.fromisoformat(row[time_key])
                        retrieved = datetime.fromisoformat(row["request_time_utc"]) if dataset == "forecast" else stamp
                        if retrieved > stamp:
                            continue
                        records = frames.setdefault(stamp, {})
                        old = records.get(row["point_id"])
                        if not old or retrieved > old[0]:
                            records[row["point_id"]] = (retrieved, row)
            result[dataset] = (revision, {key: [r for _, r in sorted(rows.values(), key=lambda pair: pair[1]["point_id"])]
                                          for key, rows in frames.items()})
        return result

    def catalog(self):
        archived = {key: {"title": DATASETS[key], "terms": [t.astimezone(ZONE).isoformat() for t in sorted(frames)],
                      "fields": {field: describe(field, key) for field in FIELDS}}
                for key, (_, frames) in self.datasets.items()}
        archived["storms"] = {}
        for key, event in self.storms().items():
            _, frames = self.storm_frames(key)
            peak = max(frames, key=lambda stamp: sum(point["snow"] or 0 for point in frames[stamp]))
            archived["storms"][key] = {"title": event["title"], "terms": [stamp.isoformat() for stamp in frames],
                                        "peak_at": peak.isoformat(),
                                        "fields": {field: describe(field, "storm") for field in STORM_FIELDS}}
        return archived

    @lru_cache(maxsize=4)
    def storm_frames(self, event_id):
        event = self.storms().get(event_id)
        if not event:
            raise ValueError("Выберите историческую серию.")
        frames = {}
        for i, site in enumerate(event["locations"]):
            for hour in site["hours"]:
                stamp = datetime.fromisoformat(hour["valid_at"])
                frames.setdefault(stamp, []).append({"id": str(i), "coordinates": tuple(site["coordinates"]),
                                                    **{k: hour[v] for k, v in STORM_FIELDS.items()}})
        return event["raw_sha256"], frames

    def live_catalog(self, refresh=False):
        snapshot = self.live.get(refresh)
        return {"title": PROVIDER, "terms": [t.isoformat() for t in sorted(snapshot["frames"])],
                "fields": {field: describe(field, "live") for field in FIELDS},
                "retrieved_at": snapshot["retrieved_at"].isoformat(), "sha256": snapshot["revision"],
                "provider_locations": snapshot["provider_locations"]}

    @lru_cache(maxsize=12)
    def points(self, dataset, stamp):
        revision, frames = self.datasets[dataset]
        if stamp not in frames:
            raise ValueError("Нет погодной карты на выбранное время.")
        points = []
        for row in frames[stamp]:
            lon, lat = ("lon", "lat") if dataset == "forecast" else ("longitude", "latitude")
            coordinates = (float(row[lon]), float(row[lat]))
            if dataset == "forecast":
                values = {column: number(row.get(column)) for column in
                          (SOURCE_FIELDS.get(key, key) for key in FIELDS)}
                # Valid Forecast responses omit snow/rain when the phenomenon is absent.
                values["snow_3h"] = values["snow_3h"] or 0
                values["rain_3h"] = values["rain_3h"] or 0
                values["precip_intensity_mm_h"] = (values["snow_3h"]+values["rain_3h"])/3
                readings = {key: shown_value(values, key) for key in FIELDS}
            else:
                readings = {key: number(row.get(column)) for key, column in HISTORY_COLUMNS.items()}
                if readings["visibility"] is not None:
                    readings["visibility"] /= 1000
            points.append({"id": row["point_id"], "coordinates": coordinates, **readings})
        return revision, points

    @cached_property
    def grid(self):
        west, south, east, north = BOUNDS
        xs = west+(east-west)*(np.arange(GRID_W)+.5)/GRID_W
        mercator = np.arcsinh(np.tan(np.deg2rad([north, south])))
        my = mercator[0]+(mercator[1]-mercator[0])*(np.arange(GRID_H)+.5)/GRID_H
        ys = np.rad2deg(np.arctan(np.sinh(my)))
        x, y = np.meshgrid(xs, ys)
        pixels = np.flatnonzero(intersects_xy(self.land(), x, y))
        return pixels, np.column_stack(TO_METRIC.transform(x.ravel()[pixels], y.ravel()[pixels]))

    @lru_cache(maxsize=4)
    def neighbors(self, coordinates, limit=1000):
        pixels, xy = self.grid
        locations = np.column_stack(TO_METRIC.transform(*np.asarray(coordinates).T))
        count = min(12, len(locations))
        distances, indices = [], []
        for chunk in np.array_split(xy, max(1, int(np.ceil(len(xy)/2048)))):
            d2 = np.square(chunk[:, None]-locations).sum(axis=2)
            chosen = np.argpartition(d2, count-1, axis=1)[:, :count]
            subset = np.take_along_axis(d2, chosen, axis=1)
            order = subset.argsort(axis=1)
            distances.append(np.sqrt(np.take_along_axis(subset, order, axis=1)))
            indices.append(np.take_along_axis(chosen, order, axis=1))
        meters, ids = np.concatenate(distances), np.concatenate(indices)
        return {"nx": GRID_W, "ny": GRID_H, "pixels": pixels,
                "dist": meters, "indices": np.where(meters <= limit, ids, -1)}

    @lru_cache(maxsize=32)
    def _frame(self, dataset, stamp, field, method, revision=None, event_id=None, haze=False):
        if dataset == "live":
            snapshot = self.live.by_revision(revision)
            if stamp not in snapshot["frames"]:
                raise ValueError("Прогноз обновился. Откройте свежие сроки.")
            points = snapshot["frames"][stamp]
        elif dataset == "storm":
            revision, frames = self.storm_frames(event_id)
            if stamp not in frames or field not in STORM_FIELDS:
                raise ValueError("Такого слоя или срока в исторической серии нет.")
            points = frames[stamp]
        else:
            revision, points = self.points(dataset, stamp)
        palette = describe(field, dataset)
        value = np.asarray([p[field] for p in points], dtype=float)
        limit = {"storm": 18000, "live": 15000}.get(dataset, 1000)
        grid = interpolate(self.neighbors(tuple(p["coordinates"] for p in points), limit), value, method, field == "wind_deg")
        low, high = palette["range"]
        normal = np.clip((np.nan_to_num(grid, nan=low)-low)/(high-low), 0, 1)
        rgb = np.array([tuple(bytes.fromhex(c[1:])) for c in palette["colors"]])
        positions = np.linspace(0, 1, len(rgb))
        tone = np.stack([np.interp(normal, positions, c) for c in rgb.T], axis=-1)
        rgba = np.dstack([tone, 215*np.isfinite(grid)]).astype(np.uint8)
        bitmap = Image.fromarray(rgba)
        if haze:
            opacity = (180*np.power(normal, 1.5)*np.isfinite(grid)).astype(np.uint8)
            bitmap = Image.new("RGBA", (grid.shape[1], grid.shape[0]), "#e9f1fb")
            bitmap.putalpha(Image.fromarray(opacity).filter(ImageFilter.GaussianBlur(2)))
        image = BytesIO()
        bitmap.save(image, format="PNG")
        return {"source": DATASETS[dataset], "valid_at": stamp.astimezone(ZONE).isoformat(),
                "sha256": revision, "bounds": BOUNDS, "field": palette, "grid_cells": int(np.isfinite(grid).sum()),
                "overlay": "data:image/png;base64,"+b64encode(image.getvalue()).decode(), "points": points}


    def frame(self, dataset, stamp, field, method, event_id=None, revision=None, haze=False):
        if dataset == "live" and not revision:
            revision = self.live.get()["revision"]
        return self._frame(dataset, stamp, field, method, revision, event_id, haze)
