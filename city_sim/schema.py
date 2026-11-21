from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator

class Wind(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    speed_mps: float = Field(ge=0, le=100)
    direction_from_deg: float = Field(ge=0, lt=360)
    reference_height_m: float = Field(default=10, gt=0)

class Region(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def validate_bounds(self):
        if not (self.west < self.east and self.south < self.north):
            raise ValueError("invalid bbox")
        if self.east - self.west > .08 or self.north - self.south > .08:
            raise ValueError("pilot regions must be <= .08 degrees on each axis")
        return self

class WeatherSource(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    kind: Literal["manual_scenario", "forecast", "observation"] = "manual_scenario"
    provider: str | None = None
    issued_at: datetime | None = None
    valid_at: datetime | None = None
    observed_at: datetime | None = None
    data_revision: str | None = None
    sample_id: str | None = None
    source_file: str | None = None
    raw_locator: str | None = None
    retrieved_at: datetime | None = None
    interpolated: bool = False
    coordinate_lon_lat: tuple[float, float] | None = None
    provider_coordinate_lon_lat: tuple[float, float] | None = None

class ScenarioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=120)
    region: Region
    wind: Wind
    available_loose_snow: Literal["unknown", "yes", "no"] = "unknown"
    weather: WeatherSource = Field(default_factory=WeatherSource)
    notes: str | None = Field(default=None, max_length=1000)

class ScenarioRecord(ScenarioCreate):
    scenario_id: str
    created_at: datetime
    data_revision: str

class RunRecord(BaseModel):
    run_id: str
    scenario_id: str
    created_at: datetime
    status: Literal["solver_not_connected", "completed", "failed"]
    result: WindFieldResult | None = None
    error: str | None = None

class GridSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    crs: Literal["EPSG:32657"] = "EPSG:32657"
    origin_xy_m: tuple[float, float]
    cell_size_m: float = Field(gt=0)
    width: int = Field(gt=0, le=10000)
    height: int = Field(gt=0, le=10000)
    reference_height_m: float = Field(gt=0)
    vertical_reference: Literal["above_dsm"] = "above_dsm"
    order: Literal["row_major_south_to_north"] = "row_major_south_to_north"

class WindFieldResult(BaseModel):
    """For MVP: small inline arrays; use external binary formats for larger meshes."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model_name: str
    model_version: str
    grid: GridSpec
    pressure_reference: Literal["absolute", "gauge", "unspecified"] = "unspecified"
    request_hash: str | None = None
    pressure_pa: list[float | None] | None = None
    velocity_east_mps: list[float | None] | None = None
    velocity_north_mps: list[float | None] | None = None
    velocity_up_mps: list[float | None] | None = None
    source_kind: Literal["physical_solver", "synthetic_test"]

    @model_validator(mode="after")
    def dimensions(self):
        cells = self.grid.width * self.grid.height
        if cells > 250_000:
            raise ValueError("MVP inline grid exceeds 250000 cells; use external array storage")
        for key in ("pressure_pa", "velocity_east_mps", "velocity_north_mps", "velocity_up_mps"):
            values = getattr(self, key)
            if values is not None and len(values) != cells:
                raise ValueError(f"{key}: expected {cells} cells")
        if all(getattr(self, key) is None for key in ("pressure_pa", "velocity_east_mps", "velocity_north_mps", "velocity_up_mps")):
            raise ValueError("no fields returned")
        return self


class CitySnapshot(BaseModel):
    data_revision: str
    bbox: tuple[float, float, float, float]
    pilot_bbox: tuple[float, float, float, float]
    building_count_total: int
    road_count_total: int
    infrastructure_count_total: int
    heights: dict[str, int]
    building_evidence: dict = Field(default_factory=dict)
    graph_version: str | None
    crs_geometry: Literal["EPSG:4326"]
    crs_solver: Literal["EPSG:32657"]
    terrain: dict | None
    physical_dem_loaded: bool


class WeatherSample(BaseModel):
    sample_id: str
    wind: Wind
    weather: WeatherSource
    point_id: str
    temperature_c: float | None
    gust_mps: float | None
    snow_mm_3h: float | None
    snow_mm_1h: float | None
    distance_m: int | None = None


class TerrainInput(BaseModel):
    origin_xy_m: tuple[float, float]
    cell_size_m: float
    width: int
    height: int
    elevation_m: list[float | None]
    vertical_crs: Literal["EPSG:3855"] = "EPSG:3855"
    kind: Literal["digital_surface_model"] = "digital_surface_model"
    order: Literal["row_major_south_to_north"] = "row_major_south_to_north"


class SolverBuilding(BaseModel):
    asset_id: str
    # Polygon, ring, coordinate; the first ring is the exterior.
    polygons_xy_m: list[list[list[tuple[float, float]]]]
    height_m: float | None
    height_source: Literal["osm_height", "estimated_from_levels_3m_per_level", "estimated_from_register_floors",
                           "estimated_from_gba", "unknown"]
    height_flags: list[str]
    height_verified: bool


class WindRequest(BaseModel):
    contract_version: Literal["1.0"] = "1.0"
    scenario_id: str
    city_revision: str
    request_hash: str
    horizontal_crs: Literal["EPSG:32657"] = "EPSG:32657"
    region: Region
    wind: Wind
    weather: WeatherSource
    available_loose_snow: Literal["unknown", "yes", "no"]
    buildings: list[SolverBuilding]
    terrain: TerrainInput | None


class AssetFieldSample(BaseModel):
    asset_id: str
    kind: Literal["road_arc", "infrastructure"]
    coordinate_lon_lat: tuple[float, float]
    row: int
    column: int
    values: dict[str, float]


class AssetFieldResult(BaseModel):
    run_id: str
    scenario_id: str
    graph_version: str | None
    aggregation: Literal["cell_at_arc_midpoint_or_object_point"] = "cell_at_arc_midpoint_or_object_point"
    reference_height_m: float
    pressure_reference: Literal["absolute", "gauge", "unspecified"]
    samples: list[AssetFieldSample]


class SnowDriftResult(BaseModel):
    """Future output of a separate snow transport model, in SI units."""
    model_name: str
    model_version: str
    wind_run_id: str
    grid: GridSpec
    snow_mass_change_kg_m2: list[float | None]
    duration_s: float = Field(gt=0)
