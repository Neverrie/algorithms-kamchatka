"""Fresh hourly Open-Meteo forecast snapshots for the city."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from threading import Lock

import httpx

from .operations import ZONE

PROVIDER = "Open-Meteo Forecast"
URL = "https://api.open-meteo.com/v1/forecast"
COORDINATES = ((158.61, 53.09), (158.65, 53.09), (158.70, 53.09), (158.62, 53.055),
               (158.66, 53.055), (158.70, 53.055), (158.62, 53.015), (158.66, 53.015),
               (158.70, 53.015), (158.64, 52.96), (158.68, 52.96), (158.72, 52.96))
VARIABLES = {"temp": "temperature_2m", "feels_like": "apparent_temperature", "wind_speed": "wind_speed_10m",
             "wind_gust": "wind_gusts_10m", "wind_deg": "wind_direction_10m", "snow": "snowfall", "rain": "rain",
             "visibility": "visibility", "humidity": "relative_humidity_2m", "clouds": "cloud_cover",
             "pop": "precipitation_probability", "precip": "precipitation"}

class ForecastRevisionError(ValueError):
    """The request refers to a different forecast snapshot."""


class LiveWeather:
    def __init__(self):
        self.snapshot = None
        self.recent = {}
        self.lock = Lock()

    def get(self, refresh=False):
        with self.lock:
            now = datetime.now(timezone.utc)
            age = timedelta(seconds=5) if refresh else timedelta(minutes=20)
            if self.snapshot and now-self.snapshot["retrieved_at"] < age:
                return self.snapshot
            response = httpx.get(os.getenv("CITY_SIM_FORECAST_URL") or URL, params={
                "latitude": ",".join(str(p[1]) for p in COORDINATES),
                "longitude": ",".join(str(p[0]) for p in COORDINATES),
                "hourly": ",".join(VARIABLES.values()), "wind_speed_unit": "ms",
                "timezone": "GMT", "past_hours": 1, "forecast_hours": 18,
            }, timeout=15)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or len(rows) != len(COORDINATES):
                raise ValueError("Поставщик вернул неполное покрытие.")
            frames, stations = {}, set()
            for i, (point, data) in enumerate(zip(COORDINATES, rows)):
                hourly = data["hourly"]
                stations.add((data["longitude"], data["latitude"]))
                for idx, value in enumerate(hourly["time"]):
                    stamp = datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone(ZONE)
                    readings = {name: hourly[raw][idx] for name, raw in VARIABLES.items()}
                    if readings["visibility"] is not None:
                        readings["visibility"] /= 1000
                    frames.setdefault(stamp, []).append({"id": f"live-{i}", "coordinates": point, **readings})
            weather = {stamp.isoformat(): points for stamp, points in frames.items()}
            revision = sha256(json.dumps(weather, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
            self.snapshot = {"retrieved_at": now, "revision": revision,
                             "frames": frames, "provider_locations": len(stations)}
            self.recent.pop(revision, None)
            self.recent[revision] = self.snapshot
            while len(self.recent) > 4:
                self.recent.pop(next(iter(self.recent)))
            return self.snapshot

    def by_revision(self, revision):
        with self.lock:
            if revision not in self.recent:
                raise ForecastRevisionError("Эта версия погоды больше не в памяти. Обновите прогноз.")
            return self.recent[revision]
