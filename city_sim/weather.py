"""Read normalized weather_coverage exports without fetching or copying observations."""
import csv
from datetime import datetime
from functools import cached_property
from hashlib import file_digest, sha256
from pathlib import Path

from winter_audit.relief import distance
from .schema import WeatherSample, WeatherSource, Wind


class WeatherArchive:
    def __init__(self, root: Path):
        self.root = root

    @cached_property
    def samples(self):
        result = {}
        for filename, product, kind in (("forecast_frames.csv", "Forecast", "forecast"),
                                         ("observations_full.csv", "Current", "observation"),
                                         ("observations.csv", "Current", "observation")):
            path = self.root / filename
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                revision = file_digest(handle, "sha256").hexdigest()
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("source_product") != product:
                        continue

                    def value(key):
                        return float(row[key]) if row.get(key) else None

                    speed, direction = value("wind_speed"), value("wind_deg")
                    if speed is None or direction is None or not 0 <= speed <= 100 or not 0 <= direction <= 360:
                        continue
                    valid = datetime.fromisoformat(row["forecast_dt_utc"])
                    retrieved = datetime.fromisoformat(row["request_time_utc"])
                    sid = sha256(f"{revision}|{row['point_id']}|{valid}|{retrieved}".encode()).hexdigest()[:24]
                    api_lon, api_lat = value("api_lon"), value("api_lat")
                    result[sid] = WeatherSample(
                        sample_id=sid, wind=Wind(speed_mps=speed, direction_from_deg=direction % 360),
                        weather=WeatherSource(
                            kind=kind, provider="OpenWeather", valid_at=valid, observed_at=valid if kind == "observation" else None,
                            retrieved_at=retrieved, data_revision=revision, sample_id=sid,
                            source_file=filename, raw_locator=row.get("raw_file"),
                            coordinate_lon_lat=(value("lon"), value("lat")),
                            provider_coordinate_lon_lat=(api_lon, api_lat) if api_lon is not None and api_lat is not None else None),
                        point_id=row["point_id"], temperature_c=value("temp"),
                        gust_mps=value("wind_gust"), snow_mm_3h=value("snow_3h"),
                        snow_mm_1h=value("current_snow_mm_h"))
        return result

    def nearest(self, lon, lat, kind):
        best = {}
        for sample in self.samples.values():
            if sample.weather.kind != kind:
                continue
            coords = sample.weather.coordinate_lon_lat
            length = round(distance((lon, lat), coords))
            if length > 3000:
                continue
            valid = sample.weather.valid_at
            if valid not in best or length < best[valid].distance_m:
                best[valid] = sample.model_copy(update={"distance_m": length})
        return [best[key] for key in sorted(best, reverse=kind == "observation")][:48]
