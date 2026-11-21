"""Shared decision context for weather inspection and snow accumulation."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from winter_audit.relief import distance

ZONE = ZoneInfo("Asia/Kamchatka")
PERIODS = {"evening": datetime(2026, 11, 20, 18, tzinfo=ZONE),
           "night": datetime(2026, 11, 20, 22, tzinfo=ZONE)}
REPLAY = PERIODS["evening"]
AREAS = {
    "center": ("Центр", (158.644, 53.034)),
    "north": ("Северо-Восток", (158.663, 53.075)),
    "seroglazka": ("Сероглазка", (158.618, 53.053)),
    "south": ("Завойко", (158.677, 52.959)),
}
POLICY = {"version": "weather-1", "snow_mm_3h": 2.0, "wind_mps": 10.0,
          "gust_mps": 15.0, "contradiction_distance_m": 1500,
          "observation_max_age_min": 90, "forecast_max_age_h": 12,
          "match_distance_m": 3000, "horizon_hours": 12}


def digest(data):
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def weather_status(snow, wind, gust=None, conflict=False):
    """Classify a weather signal. Missing snow alone cannot establish a quiet road."""
    if snow is None and wind is None and gust is None:
        return "missing", ["missing_input"]
    reasons = []
    if snow is not None and snow >= POLICY["snow_mm_3h"]:
        reasons.append("snow")
    if (wind is not None and wind >= POLICY["wind_mps"] or
            gust is not None and gust >= POLICY["gust_mps"]):
        reasons.append("wind")
    if conflict:
        reasons.append("source_conflict")
    return ("inspect" if reasons else "forecast_below_threshold", reasons)


def duplicate_free(rows):
    """Keep the first signal in each uninterrupted district episode."""
    last = {}
    for row in rows:
        key = row["district_id"]
        signature = tuple(row["reasons"]) if row["status"] == "inspect" else None
        if signature and last.get(key) != signature:
            yield row
        last[key] = signature


def weather_replay(archive, as_of=REPLAY):
    grouped = {}
    observations = []
    for row in archive.samples.values():
        w = row.weather
        if not w.valid_at or not w.retrieved_at or w.retrieved_at > as_of:
            continue
        if w.kind == "forecast":
            if (as_of <= w.valid_at <= as_of + timedelta(hours=POLICY["horizon_hours"])
                    and as_of - w.retrieved_at <= timedelta(hours=POLICY["forecast_max_age_h"])):
                grouped.setdefault(w.valid_at, []).append(row)
        elif as_of - timedelta(minutes=POLICY["observation_max_age_min"]) <= w.valid_at <= as_of:
            observations.append(row)

    def nearest(rows, point, radius):
        if not rows:
            return None
        ranked = [(distance(point, v.weather.coordinate_lon_lat), v) for v in rows]
        length, sample = min(ranked, key=lambda x: (x[0], -x[1].weather.retrieved_at.timestamp(), x[1].sample_id))
        return sample if length <= radius else None

    # Archive 5-day product is sampled on a three-hour UTC grid.
    first = as_of.replace(hour=as_of.hour // 3 * 3, minute=0, second=0, microsecond=0)
    if first < as_of:
        first += timedelta(hours=3)
    terms = [first + timedelta(hours=3*i) for i in range(5)
             if first + timedelta(hours=3*i) <= as_of + timedelta(hours=12)]
    records, baseline = [], []
    for term in terms:
        candidates = grouped.get(term, [])
        city_forecast = nearest(candidates, AREAS["center"][1], POLICY["match_distance_m"])
        city_snow = city_forecast.snow_mm_3h if city_forecast else None
        city_wind = city_forecast.wind.speed_mps if city_forecast else None
        simple_status, _ = weather_status(city_snow, city_wind)
        for key, (name, coord) in AREAS.items():
            sample = nearest(candidates, coord, POLICY["match_distance_m"])
            observed = nearest(observations, coord, POLICY["contradiction_distance_m"])
            snow = sample.snow_mm_3h if sample else None
            wind = sample.wind.speed_mps if sample else None
            gust = sample.gust_mps if sample else None
            conflict = bool(sample and observed and term-as_of <= timedelta(hours=1) and
                            abs(wind - observed.wind.speed_mps) >= 5)
            status, reasons = weather_status(snow, wind, gust, conflict)
            row = {"district_id": key, "district": name, "coordinates": coord,
                   "valid_at": term.isoformat(), "status": status, "reasons": reasons,
                   "sample": sample.model_dump(mode="json") if sample else None,
                   "observation": observed.model_dump(mode="json") if observed else None,
                   "baseline_status": simple_status}
            records.append(row)
            baseline.append({**row, "status": simple_status, "reasons": ["city_threshold"]})
    signal_count = sum(r["status"] == "inspect" for r in records)
    metrics = {"district_terms": len(records), "forecast_terms": sum(r["sample"] is not None for r in records),
               "with_recent_current": sum(r["observation"] is not None for r in records),
               "candidate_inspection_slots": signal_count,
               "baseline_inspection_slots": sum(r["status"] == "inspect" for r in baseline),
               "candidate_deduplicated": sum(1 for _ in duplicate_free(records)),
               "baseline_deduplicated": sum(1 for _ in duplicate_free(baseline)),
               "ground_truth_count": 0}
    result = {"as_of": as_of.isoformat(), "policy": POLICY, "rows": records, "metrics": metrics,
              "study": "archive_replay", "field_validated": False}
    return {**result, "run_id": digest(result)}


def weather_controls():
    # Explicit expected dispositions, independent from the archive replay.
    cases = [
        ("quiet", (0, 3, 5, False), "forecast_below_threshold"),
        ("local_snow", (3, 4, 7, False), "inspect"),
        ("wind_without_new_snow", (0, 13, 17, False), "inspect"),
        ("sources_disagree", (0, 3, 6, True), "inspect"),
        ("missing", (None, None, None, False), "missing"),
        ("gust", (0, 6, 16, False), "inspect"),
    ]
    results = [{"case": name, "input": data, "expected": expected, "actual": weather_status(*data)[0]}
               for name, data, expected in cases]
    repeated = [{"district_id": "center", "status": s, "reasons": ["snow"]}
                for s in ("inspect", "inspect", "forecast_below_threshold", "inspect")]
    results.append({"case": "deduplication", "expected": 2, "actual": len(list(duplicate_free(repeated)))})
    return results


def weather_benchmark():
    """Scripted expected actions are labels for the policy test only."""
    cases = [
        ("quiet", (0, 4), (0, 4, 7, False), False),
        ("local_snow", (.2, 4), (3, 4, 7, False), True),
        ("local_gust", (0, 6), (0, 6, 17, False), True),
        ("regional_wind", (0, 12), (0, 12, 16, False), True),
        ("source_conflict", (0, 4), (0, 4, 8, True), True),
        ("calm_district", (3, 4), (0, 4, 6, False), False),
        ("missing_district", (0, 4), (None, None, None, False), None),
    ]
    rows = [{"case": key, "expected_inspection": label,
             "baseline": weather_status(*city)[0], "candidate": weather_status(*district)[0],
             "expected_status": "missing" if label is None else ("inspect" if label else "forecast_below_threshold")}
            for key, city, district, label in cases]
    metrics = {}
    for variant in ("baseline", "candidate"):
        truth = [(r["expected_inspection"], r[variant] == "inspect") for r in rows
                 if r["expected_inspection"] is not None]
        tp, fp = sum(x and y for x, y in truth), sum(not x and y for x, y in truth)
        fn = sum(x and not y for x, y in truth)
        metrics[variant] = {"tp": tp, "fp": fp, "fn": fn,
                            "precision": round(tp/(tp+fp), 3) if tp+fp else None,
                            "recall": round(tp/(tp+fn), 3) if tp+fn else None}
    return {"scope": "scripted_policy_cases", "rows": rows, "metrics": metrics}


def inspection_network(city):
    """Associate each physical road with the nearest district anchor within 3 km."""
    from .terrain import BOUNDS

    roads = city.roads(BOUNDS)
    seen, features, length = set(), [], {}
    for road in sorted(roads["features"], key=lambda item: item["id"]):
        props = road["properties"]
        key = props["physical_road_id"]
        if key in seen:
            continue
        seen.add(key)
        a, b = road["geometry"]["coordinates"]
        middle = tuple((first+last)/2 for first, last in zip(a, b))
        closest, meters = min(((key, distance(middle, center)) for key, (_, center) in AREAS.items()),
                              key=lambda pair: pair[1])
        district = closest if meters <= POLICY["match_distance_m"] else "outside"
        length[district] = length.get(district, 0) + props["length_m"]
        features.append({"type": "Feature", "id": key, "geometry": road["geometry"],
                         "properties": {"district_id": district, "road_name": props["road_name"],
                                        "length_m": props["length_m"]}})
    return {"type": "FeatureCollection", "features": features, "length_m_by_district": length,
            "graph_version": roads["metadata"]["graph_version"]}
