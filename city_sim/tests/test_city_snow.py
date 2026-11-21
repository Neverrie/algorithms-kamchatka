from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from fastapi.testclient import TestClient
import numpy as np
from pydantic import ValidationError
from shapely.geometry import Point

from city_sim.api import make_app
from city_sim.city_snow import CitySnow, CitySnowRequest, administrative_boundary, checkpoints, remap
from city_sim.snow_experiment import SnowStudy
from city_sim.weather import WeatherArchive


class CitywideWeatherTests(unittest.TestCase):
    def test_historical_catalog_and_city_geometry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            client = TestClient(make_app(db_path=root / "fixture.sqlite", weather_path=root))
            catalog = client.get("/api/v1/operations/city/catalog").json()
            self.assertEqual(catalog["openweather"], [])
            self.assertEqual({e["event_id"] for e in catalog["storms"]},
                             {"march-2025", "december-2025", "january-2026"})
            self.assertTrue(all(e["source_cells"] == 3 for e in catalog["storms"]))
            self.assertEqual(client.get("/api/v1/operations/city/runs/malformed").status_code, 404)
            self.assertEqual(client.post("/api/v1/operations/city/runs", json={"source": "openweather"}).status_code, 422)
        boundary = administrative_boundary()
        self.assertTrue(boundary.covers(Point(158.644, 53.034)))
        self.assertTrue(boundary.covers(Point(158.677, 52.958)))
        self.assertFalse(boundary.covers(Point(158.4, 53.0)))

    def test_archive_does_not_use_forecasts_collected_after_valid_time(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "forecast_frames.csv").write_text(
                "point_id,lon,lat,source_product,request_time_utc,forecast_dt_utc,wind_speed,wind_deg,snow_3h,api_lon,api_lat\n"
                "g500-1,158.644,53.034,Forecast,2026-11-20T05:50+00:00,2026-11-20T06:00+00:00,8,270,,158.64,53.03\n"
                "g500-1,158.644,53.034,Forecast,2026-11-20T06:01+00:00,2026-11-20T06:00+00:00,50,90,30,158.64,53.03\n"
            )
            runner = CitySnow(SnowStudy(None, WeatherArchive(root)), root)
            select, source = runner.weather(CitySnowRequest(valid_at="2026-11-20T06:00Z"))
            _, rows, meters = select((158.644, 53.034))
            self.assertEqual(meters, 0)
            self.assertEqual(rows[0]["wind_speed_ms"], 8)
            self.assertEqual(rows[0]["snowfall_cm"], 0)
            self.assertTrue(rows[0]["snow_field_omitted"])
            self.assertEqual(source["snapshot_points"], 1)
            self.assertEqual(source["provider_coordinates_reported"], 1)
            self.assertGreater(select((158.72, 53.034))[2], 3000)
            self.assertIsNone(select((159.0, 53.034)))

    def test_resampling_retains_mask_and_grid_order(self):
        sampled = remap([[1, 2], [float("nan"), 3]], 4)
        self.assertEqual(sampled[0, 0], 1)
        self.assertEqual(sampled[0, 3], 2)
        self.assertEqual(sampled[3, 3], 3)
        self.assertTrue(np.isnan(sampled[3, 0]))

    def test_storm_weather_is_available_before_calculation(self):
        with TemporaryDirectory() as folder:
            client = TestClient(make_app(db_path=Path(folder) / "fixture.sqlite"))
            storms = client.get("/api/v1/operations/atlas").json()["storms"]
            self.assertEqual({name: len(event["terms"]) for name, event in storms.items()}, {
                "march-2025": 96, "december-2025": 216, "january-2026": 144
            })
            terms = storms["january-2026"]["terms"]
            frame = client.get("/api/v1/operations/atlas/frame", params={
                "dataset": "storm", "event_id": "january-2026", "valid_at": terms[80], "field": "snow"
            }).json()
            self.assertEqual(frame["valid_at"], terms[80])
            self.assertEqual(len(frame["points"]), 3)
            self.assertGreater(frame["grid_cells"], 0)
        self.assertEqual((checkpoints(216)[0], checkpoints(216)[-1], len(checkpoints(216))), (0, 215, 19))

    def test_drawn_outline_rejects_crossing_and_clips_on_land(self):
        corners = [(158.630, 53.028), (158.646, 53.028), (158.646, 53.040), (158.630, 53.040)]
        with self.assertRaises(ValidationError):
            CitySnowRequest(source="manual", zone=[corners[i] for i in (0, 2, 1, 3)])
        with TemporaryDirectory() as folder:
            app = make_app(db_path=Path(folder) / "fixture.sqlite")
            runner = app.state.city_snow
            outline = runner.domain(CitySnowRequest(source="manual", zone=corners))
            district = runner.domain(CitySnowRequest(source="manual", district_id="center"))
            self.assertTrue(runner.land.covers(outline.representative_point()))
            self.assertLessEqual(outline.area, 0.016*0.012 + 1e-12)
            self.assertTrue(district.covers(Point(158.644, 53.034)))

    def test_cell_probe_respects_the_selected_hour_and_reports_neighbors(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            runner = CitySnow(SnowStudy(None, WeatherArchive(root)), root)
            identifier = "f"*64
            bounds = [158.6400, 53.0300, 158.6404, 53.0304]
            (root / f"{identifier}.json").write_text(json.dumps({"bounds": bounds, "status": "completed"}))
            final = np.array([[np.nan, 5.0], [4.0, 6.0]])
            np.savez_compressed(root / f"{identifier}.npz", snow_cm=final, weather_snow=final-1,
                                combined=final/10, opposite_snow_cm=final+1, timeline=[final/2])
            first = runner.probe(identifier, 158.6403, 53.0303, frame=0)
            self.assertEqual(first["snow_cm"], 2.5)
            self.assertIsNone(first["weather_snow_cm"])
            masked = runner.probe(identifier, 158.6401, 53.0303)
            self.assertIsNone(masked["snow_cm"])
            nearby = runner.probe(identifier, 158.6401, 53.0303, nearby_m=26)
            self.assertEqual(nearby["snow_cm"], 5)
            self.assertGreater(nearby["sample_distance_m"], 0)
