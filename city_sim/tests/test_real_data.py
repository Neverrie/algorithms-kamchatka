import tempfile
import unittest
from io import BytesIO
from math import asinh, pi, radians, tan
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from city_sim.api import make_app
from city_sim.audit_heights import audit
from city_sim.physics import sample_assets
from city_sim.schema import Region, WindFieldResult
from city_sim.terrain import TO_METRIC, Terrain

PILOT = dict(west=158.628, south=53.018, east=158.658, north=53.047)


class RealGeodataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.app = make_app(db_path=Path(cls.directory.name) / "scenario.sqlite")
        cls.client = cls.enterClassContext(TestClient(cls.app))

    def test_city_snapshot_and_south_coverage(self):
        response = self.client.get("/api/v1/city/metadata")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["road_count_total"], 10567)
        self.assertEqual(data["terrain"]["vertical_crs"], "EPSG:3855")
        self.assertGreater(data["heights"]["multipolygons"], 50)
        self.assertGreater(data["heights"]["unknown"], data["heights"]["osm_height"])
        south = self.client.get("/api/v1/city/buildings", params={"bbox": "158.643,52.944,158.704,52.977"})
        self.assertGreater(len(south.json()["features"]), 500)
        roads = self.client.get("/api/v1/city/roads").json()
        self.assertGreater(len(roads["features"]), 2000)
        self.assertTrue(any(road["properties"]["slope_pct"] is not None for road in roads["features"]))

    def test_municipal_floors_and_overture_provenance(self):
        data = self.client.get("/api/v1/city/metadata").json()
        evidence = data["building_evidence"]
        self.assertEqual(evidence["overture"]["added"], 5584)
        self.assertEqual(evidence["register_addresses"], 582)
        self.assertGreater(evidence["register_matched"], 400)
        self.assertEqual(evidence["register_with_floors"], 397)
        buildings = self.app.state.city.osm_layers[0].features
        features = {item["id"]: item["properties"] for item in buildings}
        house = features["osm:way:406692020"]
        self.assertEqual(house["construction_year"], 1970)
        self.assertEqual(house["registered_floors"], 5)
        self.assertEqual(house["render_height_m"], 15)
        self.assertIsNone(house["physical_height_m"])
        self.assertFalse(house["height_verified"])
        self.assertEqual(len(features), len(buildings))
        response = self.client.get("/api/v1/city/building-candidates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metadata"]["status"], "unverified_candidate_outlines")

    def test_terrain_rgb_and_cross_tile_seam(self):
        terrain = Terrain()
        self.assertGreater(float(terrain.sample(158.63856, 53.04342)), 350)
        for lat in (52.99999, 53.0, 53.00001):
            height = float(terrain.sample(158.66, lat))
            self.assertTrue(-10 < height < 600)
        lon, lat, level = 158.64, 53.043, 14
        x = int((lon + 180) / 360 * 2**level)
        y = int((1 - asinh(tan(radians(lat))) / pi) / 2 * 2**level)
        response = self.client.get(f"/api/v1/tiles/dem/{level}/{x}/{y}.png")
        with Image.open(BytesIO(response.content)) as tile:
            self.assertEqual(tile.size, (256, 256))
            hi, mid, lo = tile.getpixel((128, 128))
            value = (hi * 65536 + mid * 256 + lo) * .1 - 10000
            self.assertGreater(value, 0)
        self.assertEqual(self.client.get("/api/v1/tiles/dem/999/1/1.png").status_code, 404)

    def test_historical_sheet_provenance(self):
        expected = audit()
        received = self.client.get("/api/v1/city/height-audit").json()
        self.assertEqual(received["metadata"]["edition_year"], 1990)
        self.assertEqual(received["metadata"]["survey_epoch_year"], 1987)
        self.assertEqual(len(received["features"]), 5)
        for actual, regenerated in zip(received["features"], expected["features"]):
            self.assertAlmostEqual(actual["properties"]["dsm_m"], regenerated["properties"]["dsm_m"], delta=.8)

    def test_opposite_wind_inputs_roundtrip_with_explicit_unknown_heights(self):
        for degrees in (90, 270):
            scenario = {"title": f"Ветер {degrees}", "region": PILOT,
                        "wind": {"speed_mps": 9, "direction_from_deg": degrees}}
            response = self.client.post("/api/v1/scenarios", json=scenario)
            self.assertEqual(response.status_code, 201, response.text)
            url = f"/api/v1/scenarios/{response.json()['scenario_id']}"
            self.assertEqual(self.client.get(url).json()["wind"]["direction_from_deg"], degrees)
            inputs = self.client.get(f"{url}/solver-input").json()
            self.assertEqual(inputs["horizontal_crs"], "EPSG:32657")
            self.assertEqual(inputs["terrain"]["vertical_crs"], "EPSG:3855")
            self.assertNotIn(None, inputs["terrain"]["elevation_m"])
            self.assertTrue(any(b["height_m"] is None for b in inputs["buildings"]))
            self.assertTrue(all(not b["height_verified"] for b in inputs["buildings"]))
            self.assertEqual(self.client.get(f"{url}/solver-input").json()["request_hash"], inputs["request_hash"])
            if degrees == 90:
                east = inputs["request_hash"]
            else:
                self.assertNotEqual(east, inputs["request_hash"])


    def test_city_graph_extends_audited_graph_in_zavoyko(self):
        bbox = "158.654,52.945,158.697,52.977"
        path = f"/api/v1/city/roads?bbox={bbox}"
        base = self.client.get(path).json()
        city = self.client.get(path + "&network=city").json()
        self.assertNotEqual(base["metadata"]["graph_version"], city["metadata"]["graph_version"])
        self.assertGreater(len(city["features"]), len(base["features"]))
        street = [f for f in city["features"] if f["properties"]["road_name"] == "улица Петра Ильичёва"]
        self.assertTrue(street)
        self.assertTrue(all(f["properties"]["network_extension"] and
                            f["properties"]["slope_pct"] is None for f in street))
        self.assertEqual(len({f["id"] for f in city["features"]}), len(city["features"]))
        core = self.app.state.city.graph_layers[0]
        expanded = self.app.state.city.extended_graph_layers[0]
        city_only_ways = {edge.way_id for edge in expanded.edges.values() if edge.edge_id not in core.edges}
        # The inspection map retains this road's mapped slope and this road's speed sign as traversable geometry.
        self.assertIn(129214933, city_only_ways)
        self.assertIn(128466677, city_only_ways)

    def test_field_samples_keep_road_and_infrastructure_ids(self):
        region = Region(**PILOT)
        west, south, east, north = TO_METRIC.transform_bounds(
            region.west, region.south, region.east, region.north)
        field = WindFieldResult(
            model_name="test fixture", model_version="0", source_kind="synthetic_test",
            pressure_reference="gauge", pressure_pa=[-4],
            grid={"origin_xy_m": (west, south), "cell_size_m": max(east-west, north-south) + 1,
                  "width": 1, "height": 1, "reference_height_m": 15})
        version, sampled = sample_assets(self.app.state.city, region, field)
        self.assertEqual(version, self.client.get("/api/v1/city/metadata").json()["graph_version"])
        self.assertEqual({item["kind"] for item in sampled}, {"road_arc", "infrastructure"})
        self.assertTrue(all(item["asset_id"].startswith("osm:") for item in sampled))
        self.assertTrue(all(item["values"] == {"pressure_pa": -4} for item in sampled))


class ArchiveTests(unittest.TestCase):
    def test_saved_forecast_uses_atlas_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            headers = ("source_product,lon,lat,point_id,forecast_dt_utc,request_time_utc,"
                       "wind_speed,wind_deg,temp,snow_3h,raw_file\n")
            row = ("Forecast,158.642,53.0334,fixture,2026-11-20T06:00:00Z,"
                   "2026-11-20T05:50:00Z,9,90,-3,1,fixture.json\n")
            (root / "forecast_frames.csv").write_text(headers + row)
            client = TestClient(make_app(db_path=root / "fixture.db", weather_path=root))
            sample = client.get("/api/v1/weather?lon=158.642&lat=53.0334").json()[0]
            payload = {"title": "Архивный тест", "region": PILOT, "wind": sample["wind"],
                       "weather": {"kind": "forecast", "sample_id": sample["sample_id"], "provider": "custom"}}
            saved = client.post("/api/v1/scenarios", json=payload).json()
            self.assertEqual(saved["weather"]["provider"], "OpenWeather")
            self.assertFalse(saved["weather"]["interpolated"])
            self.assertIsNone(saved["weather"]["issued_at"])
            self.assertEqual(saved["weather"]["retrieved_at"], "2026-11-20T05:50:00Z")
            self.assertEqual(len(saved["weather"]["data_revision"]), 64)
            payload["wind"]["speed_mps"] = 30
            self.assertEqual(client.post("/api/v1/scenarios", json=payload).status_code, 422)
