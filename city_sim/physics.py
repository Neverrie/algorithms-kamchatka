"""Versioned handoff to the external aerodynamics and future snow modules."""
from hashlib import sha256
from math import floor
import json
from typing import Protocol
from urllib.request import Request, urlopen

from .schema import ScenarioRecord, SnowDriftResult, WindFieldResult, WindRequest
from .terrain import TO_METRIC


class WindSolver(Protocol):
    def calculate(self, request: WindRequest) -> WindFieldResult: ...


class SnowDriftModel(Protocol):
    """Future model consuming surface/snow state and validated aerodynamics."""
    def calculate(self, request: WindRequest, air: WindFieldResult) -> SnowDriftResult: ...


class SolverNotConnected(RuntimeError):
    pass


class UnavailableWindSolver:
    def calculate(self, request: WindRequest) -> WindFieldResult:
        raise SolverNotConnected("Аэродинамический решатель пока не подключён.")


class HttpWindSolver:
    def __init__(self, url):
        self.url = url

    def calculate(self, request):
        message = Request(self.url, data=request.model_dump_json().encode(),
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urlopen(message, timeout=120) as response:
            payload = response.read(32 * 1024 * 1024 + 1)
        if len(payload) > 32 * 1024 * 1024:
            raise ValueError("Wind response exceeds 32 MiB")
        field = WindFieldResult.model_validate_json(payload)
        if field.source_kind != "physical_solver" or field.request_hash != request.request_hash:
            raise ValueError("The solver response must be physical and match request_hash")
        region = request.region
        west, south, east, north = TO_METRIC.transform_bounds(
            region.west, region.south, region.east, region.north)
        grid = field.grid
        x, y = grid.origin_xy_m
        if not (x < east and west < x + grid.width * grid.cell_size_m and
                y < north and south < y + grid.height * grid.cell_size_m):
            raise ValueError("Result grid is disjoint from the requested area")
        if field.pressure_pa is not None and field.pressure_reference == "unspecified":
            raise ValueError("The solver must declare the pressure reference")
        return field


def solver_request(scenario: ScenarioRecord, city):
    region = scenario.region
    bbox = (region.west, region.south, region.east, region.north)
    buildings = []
    for item in city.buildings(bbox)["features"]:
        properties = item["properties"]
        geometry = item["geometry"]
        shapes = geometry["coordinates"]
        if geometry["type"] == "Polygon":
            shapes = [shapes]
        buildings.append(dict(
            asset_id=properties["asset_id"], height_m=properties["physical_height_m"],
            height_source=properties["height_source"], height_verified=properties["height_verified"],
            height_flags=properties["height_flags"],
            polygons_xy_m=[[[tuple(round(v, 3) for v in TO_METRIC.transform(*point))
                             for point in ring] for ring in polygon] for polygon in shapes],
        ))
    buildings.sort(key=lambda building: building["asset_id"])
    terrain = city.terrain.grid(region) if city.terrain and city.terrain.metadata else None
    request = WindRequest(
        scenario_id=scenario.scenario_id, city_revision=scenario.data_revision,
        region=region, wind=scenario.wind, weather=scenario.weather,
        available_loose_snow=scenario.available_loose_snow, buildings=buildings,
        terrain=terrain, request_hash="")
    canonical = json.dumps(request.model_dump(mode="json", exclude={"request_hash", "scenario_id"}),
                           sort_keys=True, allow_nan=False, separators=(",", ":"))
    return request.model_copy(update={"request_hash": sha256(canonical.encode()).hexdigest()})


def display_field(field: WindFieldResult):
    """Decimate solver samples for rendering, preserving their signed values."""
    from .terrain import TO_GEOGRAPHIC

    grid = field.grid
    step_x = max(1, (grid.width + 59) // 60)
    step_y = max(1, (grid.height + 59) // 60)
    x0, y0 = grid.origin_xy_m
    rows = range(0, grid.height, step_y)
    columns = range(0, grid.width, step_x)
    points = [(x0 + (col + .5) * grid.cell_size_m, y0 + (row + .5) * grid.cell_size_m)
              for row in rows for col in columns]
    lon, lat = TO_GEOGRAPHIC.transform(*zip(*points))
    indexes = [row * grid.width + col for row in rows for col in columns]
    return {
        "positions": list(zip(lon, lat)),
        "reference_height_m": grid.reference_height_m,
        "pressure_reference": field.pressure_reference,
        "model": f"{field.model_name} {field.model_version}",
        "fields": {name: [values[index] for index in indexes]
                   for name in ("pressure_pa", "velocity_east_mps", "velocity_north_mps", "velocity_up_mps")
                   if (values := getattr(field, name)) is not None},
    }


def sample_assets(city, region, field):
    bbox = (region.west, region.south, region.east, region.north)
    fields = {key: values for key in ("pressure_pa", "velocity_east_mps",
                                      "velocity_north_mps", "velocity_up_mps")
              if (values := getattr(field, key)) is not None}
    grid = field.grid
    x0, y0 = grid.origin_xy_m
    roads = city.roads(bbox)
    tagged = [("road_arc", items) for items in roads["features"]]
    tagged.extend(("infrastructure", item) for item in city.infrastructure(bbox)["features"])
    result = []
    for kind, item in tagged:
        coordinates = item["geometry"]["coordinates"]
        lon, lat = ((a + b) / 2 for a, b in zip(*coordinates)) if kind == "road_arc" else coordinates
        if not (region.west <= lon <= region.east and region.south <= lat <= region.north):
            continue
        x, y = TO_METRIC.transform(lon, lat)
        col, row = floor((x - x0) / grid.cell_size_m), floor((y - y0) / grid.cell_size_m)
        if not (0 <= col < grid.width and 0 <= row < grid.height):
            continue
        index = row * grid.width + col
        values = {key: samples[index] for key, samples in fields.items() if samples[index] is not None}
        if values:
            result.append(dict(asset_id=item["id"], kind=kind, coordinate_lon_lat=(lon, lat),
                               row=row, column=col, values=values))
    return roads["metadata"]["graph_version"], result
