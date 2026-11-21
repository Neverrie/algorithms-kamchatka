from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from city_sim.api import make_app
from city_sim.operations import weather_benchmark, weather_controls
from city_sim.reproduce_operations import FieldChecks, validate_field
from city_sim.snow_experiment import SnowStudy, SnowStudyRequest
from city_sim.weather import WeatherArchive


class WeatherOperationTests(unittest.TestCase):
    def test_control_cases_and_baselines(self):
        self.assertTrue(all(r["actual"] == r["expected"] for r in weather_controls()))
        benchmark = weather_benchmark()
        self.assertTrue(all(r["candidate"] == r["expected_status"] for r in benchmark["rows"]))
        self.assertEqual(benchmark["metrics"]["candidate"]["recall"], 1)
        self.assertLess(benchmark["metrics"]["baseline"]["recall"], 1)

    def test_archive_as_of_and_linked_weather(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            columns = ("point_id,lon,lat,source_product,request_time_utc,forecast_dt_utc,"
                       "wind_speed,wind_deg,snow_3h\n")
            before = "center,158.644,53.034,Forecast,2026-11-20T05:50:00+00:00,2026-11-20T06:00:00+00:00,8,270,3\n"
            after = "center,158.644,53.034,Forecast,2026-11-20T06:01:00+00:00,2026-11-20T06:00:00+00:00,50,90,30\n"
            (root / "forecast_frames.csv").write_text(columns + before + after)
            client = TestClient(make_app(db_path=root / "fixture.sqlite", weather_path=root))
            result = client.get("/api/v1/operations/weather").json()
            center = next(r for r in result["rows"] if r["district_id"] == "center")
            self.assertEqual(center["status"], "inspect")
            self.assertEqual(center["sample"]["wind"]["direction_from_deg"], 270)
            self.assertEqual(center["sample"]["snow_mm_3h"], 3)
            field = FieldChecks.model_validate({"road_checks": [
                {"district_id": "center", "valid_at": "2026-11-20T06:00:00+00:00",
                 "inspection_needed": True, "evidence": "fixture"}]})
            labels = validate_field(field, [result], [])
            self.assertEqual(labels["matching_road_labels"], 1)
            self.assertEqual(labels["road_metrics"]["regional"]["recall"], 1)
            self.assertIsNone(labels["snow_model_mae_cm"])

    def test_missing_archive_is_distinct_from_quiet(self):
        with TemporaryDirectory() as folder:
            client = TestClient(make_app(db_path=Path(folder) / "fixture.sqlite", weather_path=folder))
            result = client.get("/api/v1/operations/weather?period=night").json()
            self.assertTrue(result["rows"])
            self.assertTrue(all(r["status"] == "missing" for r in result["rows"]))
            self.assertEqual(result["metrics"]["ground_truth_count"], 0)


class SnowOperationTests(unittest.TestCase):
    def test_pinned_engine_spatial_controls_and_reproducibility(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            city = make_app(db_path=root / "fixture.sqlite").state.city
            archive = WeatherArchive(root)
            adapter = SnowStudy(city, archive)
            if not adapter.available:
                self.skipTest("Pinned upstream checkout not installed")
            spec = SnowStudyRequest(resolution=64)
            result = adapter.run(spec)
            metrics = result["metrics"]
            self.assertTrue(result["directional_control"]["passes"])
            self.assertTrue(result["directional_control"]["no_new_snow_zero"])
            self.assertTrue(metrics["calm_flat_pass"])
            self.assertTrue(metrics["building_mask_stable"])
            self.assertGreater(metrics["road_segments_covered"], 150)
            self.assertGreater(metrics["building_sides_covered"], 30)
            self.assertGreater(metrics["reverse_mae_cm"], 0)
            self.assertGreater(metrics["shorter_buildings_mae_cm"], 0)
            self.assertNotEqual(result["grids"]["combined"], result["grids"]["terrain"])
            self.assertFalse(result["field_validated"])
            self.assertEqual(result["run_id"], SnowStudy(city, archive).run(spec)["run_id"])
            still = adapter.run(SnowStudyRequest(resolution=64, wind_speed_ms=0))
            self.assertEqual(still["ranking"]["combined"], [])
            self.assertTrue(still["metrics"]["calm_flat_pass"])
