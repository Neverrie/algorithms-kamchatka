import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from city_sim.api import make_app
from city_sim.live_weather import COORDINATES, VARIABLES


class LiveRevisionTests(unittest.TestCase):
    def test_same_forecast_reuses_saved_run_and_changed_values_rerun(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        times = [(now+timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(3)]

        def response(snow):
            rows = []
            for lon, lat in COORDINATES:
                hourly = {var: [0]*3 for var in VARIABLES.values()}
                hourly.update(time=times, snowfall=[snow]*3)
                rows.append({"longitude": lon, "latitude": lat, "hourly": hourly})
            return httpx.Response(200, request=httpx.Request("GET", "http://test/forecast"),
                                  content=json.dumps(rows).encode())

        with TemporaryDirectory() as folder, patch("city_sim.live_weather.httpx.get") as fetch:
            app = make_app(db_path=Path(folder)/"test.sqlite", weather_path=Path(folder))
            app.state.city_snow.study.engine
            client = TestClient(app)
            live = "/api/v1/operations/atlas/live"
            runs = "/api/v1/operations/city/runs"
            fetch.return_value = response(1)
            first = client.get(live).json()
            payload = {"source": "live", "valid_at": first["terms"][0], "hours": 1,
                       "weather_revision": first["sha256"],
                       "zone": [[158.641, 53.036], [158.643, 53.036], [158.642, 53.037]]}

            def submit(expected):
                start = client.post(runs, json=payload)
                self.assertEqual(start.status_code, 202, start.text)
                data = start.json()
                self.assertEqual(data["status"], "pending")
                path = f"{runs}/{data['run_id']}"
                deadline = monotonic()+25
                while monotonic() < deadline:
                    status = client.get(path).json()["status"]
                    if status == "completed":
                        break
                    self.assertNotEqual(status, "failed")
                    sleep(.1)
                else:
                    self.fail("Run did not complete.")
                result = client.get(path + "/sample", params={"lon": 158.642, "lat": 53.0363}).json()
                self.assertEqual(result["snow_cm"], expected)
                return data["run_id"]

            def refresh():
                app.state.city_snow.atlas.live.snapshot["retrieved_at"] -= timedelta(seconds=6)
                return client.get(live, params={"refresh": True}).json()

            old_id = submit(1)
            unchanged = refresh()
            self.assertEqual(unchanged["sha256"], first["sha256"])
            self.assertEqual(client.post(runs, json=payload).json(), {"status": "completed", "run_id": old_id})

            fetch.return_value = response(4)
            changed = refresh()
            self.assertNotEqual(changed["sha256"], first["sha256"])
            self.assertEqual(client.post(runs, json=payload).status_code, 409)
            prior = client.get("/api/v1/operations/atlas/frame", params={
                "dataset": "live", "field": "snow", "valid_at": first["terms"][0], "revision": first["sha256"]
            })
            self.assertEqual(prior.json()["points"][0]["snow"], 1)
            payload["weather_revision"] = changed["sha256"]
            self.assertNotEqual(submit(4), old_id)
