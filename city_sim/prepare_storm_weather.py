"""Normalize fixed historical episodes downloaded from Open-Meteo Archive."""
from datetime import datetime, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlencode

from .operations import ZONE
from .terrain import DATA

CASES = (
    ("march-2025", "22–25 марта 2025", "2025-03-22", "2025-03-25"),
    ("december-2025", "7–15 декабря 2025", "2025-12-07", "2025-12-15"),
    ("january-2026", "12–17 января 2026", "2026-01-12", "2026-01-17"),
)
PARAMS = {"latitude": "53.110,53.076,53.034,52.958,52.982,53.045",
          "longitude": "158.639,158.663,158.644,158.677,158.658,158.617",
          "hourly": "snowfall,precipitation,snow_depth,wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
          "models": "ecmwf_ifs", "wind_speed_unit": "ms", "timezone": "Asia/Kamchatka"}
ROOT_URL = "https://archive-api.open-meteo.com/v1/archive"


def normalize(folder):
    results = {}
    for key, title, start, end in CASES:
        raw = (folder / f"{start}.json").read_bytes()
        rows = json.loads(raw)
        locations = {}
        moment = datetime.fromisoformat(start).replace(tzinfo=ZONE)
        count = ((datetime.fromisoformat(end).date()-moment.date()).days+1)*24
        for row in rows:
            units = row["hourly_units"]
            if units["snowfall"] != "cm" or units["wind_speed_10m"] != "m/s" or row["utc_offset_seconds"] != 43200:
                raise ValueError(f"Unexpected units or time offset in {key}")
            hourly = row["hourly"]
            coordinates = (row["longitude"], row["latitude"])
            hours = [
                {"valid_at": (moment + timedelta(hours=i)).isoformat(),
                 "snowfall_cm": hourly["snowfall"][i],
                 "wind_speed_ms": hourly["wind_speed_10m"][i],
                 "wind_from_deg": hourly["wind_direction_10m"][i],
                 "precipitation_mm": hourly["precipitation"][i],
                 "gust_ms": hourly["wind_gusts_10m"][i],
                 "temperature_c": hourly["temperature_2m"][i]}
                for i in range(count)
            ]
            if [h["valid_at"][:16] for h in hours] != hourly["time"]:
                raise ValueError(f"Archive hours are not continuous in {key}")
            if any(h["snowfall_cm"] is None or h["wind_speed_ms"] is None or h["wind_from_deg"] is None for h in hours):
                raise ValueError(f"Missing hourly model inputs in {key}")
            locations.setdefault(coordinates, {"coordinates": coordinates, "hours": hours})
        results[key] = {"title": title, "kind": "reanalysis", "start_at": moment.isoformat(),
                        "end_at": (moment+timedelta(hours=count-1)).isoformat(), "provider": "Open-Meteo",
                        "model": "ECMWF IFS", "license": "CC BY 4.0",
                        "url": ROOT_URL + "?" + urlencode(dict(PARAMS, start_date=start, end_date=end)),
                        "raw_sha256": sha256(raw).hexdigest(), "locations": list(locations.values())}
    data = json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    (DATA / "storm-weather.json.gz").write_bytes(gzip.compress(data, mtime=0))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    normalize(parser.parse_args().directory)
