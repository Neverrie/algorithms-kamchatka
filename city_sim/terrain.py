"""Copernicus DSM samples shared by the map and the solver export."""
from functools import cached_property, lru_cache
from io import BytesIO
from pathlib import Path
import json
import math

import numpy as np
from PIL import Image
from pyproj import Transformer

DATA = Path(__file__).with_name("data")
BOUNDS = (158.48, 52.91, 158.80, 53.16)
TO_METRIC = Transformer.from_crs(4326, 32657, always_xy=True)
TO_GEOGRAPHIC = Transformer.from_crs(32657, 4326, always_xy=True)


def bilinear(pixels, x, y, nodata):
    x, y = np.broadcast_arrays(x, y)
    inside = (x >= 0) & (y >= 0) & (x <= pixels.shape[1] - 1) & (y <= pixels.shape[0] - 1)
    i = np.clip(np.floor(x).astype(int), 0, pixels.shape[1] - 2)
    j = np.clip(np.floor(y).astype(int), 0, pixels.shape[0] - 2)
    a, b, c, d = pixels[j, i], pixels[j, i + 1], pixels[j + 1, i], pixels[j + 1, i + 1]
    valid = inside & (a != nodata) & (b != nodata) & (c != nodata) & (d != nodata)
    u, v = x - i, y - j
    values = (a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v
    return np.where(valid & (values > -500) & (values < 9000), values, np.nan)


class Terrain:
    def __init__(self, root=DATA):
        self.path = root / "terrain.npz"

    @cached_property
    def metadata(self):
        manifest = self.path.with_suffix(".json")
        return json.loads(manifest.read_text()) if manifest.exists() and self.path.exists() else None

    @lru_cache(maxsize=1)
    def tiles(self):
        if not self.metadata:
            return ()
        with np.load(self.path) as arrays:
            return tuple((tile, arrays[tile["id"]]) for tile in self.metadata["tiles"])

    def sample(self, lon, lat):
        lon, lat = np.broadcast_arrays(lon, lat)
        heights = np.full(lon.shape, np.nan)
        for tile, pixels in self.tiles():
            values = bilinear(pixels, (lon - tile["west"]) / tile["step_lon"],
                              (tile["north"] - lat) / tile["step_lat"], tile["nodata"])
            heights = np.where(np.isfinite(values), values, heights)
        return heights

    def grid(self, region):
        west, south, east, north = TO_METRIC.transform_bounds(
            region.west, region.south, region.east, region.north)
        step = 30
        x0, y0 = math.floor(west / step) * step, math.floor(south / step) * step
        width, height = math.ceil((east - x0) / step), math.ceil((north - y0) / step)
        x, y = np.meshgrid(x0 + (np.arange(width) + 0.5) * step,
                           y0 + (np.arange(height) + 0.5) * step)
        lon, lat = TO_GEOGRAPHIC.transform(x, y)
        return {
            "origin_xy_m": [x0, y0], "cell_size_m": step, "width": width, "height": height,
            "elevation_m": [round(float(z), 2) if np.isfinite(z) else None
                            for z in self.sample(lon, lat).ravel()],
        }

    @lru_cache(maxsize=512)
    def png(self, z, x, y, color=False):
        size = 256
        uv = (np.arange(size) + 0.5) / size
        scale = 2 ** z
        lon = (x + uv) / scale * 360 - 180
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + uv) / scale))))
        heights = self.sample(lon[None, :], lat[:, None])
        covered = np.isfinite(heights)
        if color:
            values = np.nan_to_num(heights)
            palette = np.array([[36, 79, 84], [87, 115, 105], [148, 150, 125],
                                [195, 181, 148], [227, 221, 203]])
            rgb = np.stack([np.interp(values, [0, 40, 130, 270, 480], channel)
                            for channel in palette.T], axis=-1)
            rgba = np.dstack((rgb, covered * 255)).astype("uint8")
            image = Image.fromarray(rgba)
        else:
            encoded = np.rint((np.nan_to_num(heights) + 10000) * 10).astype("uint32")
            rgb = np.stack((encoded >> 16, (encoded >> 8) & 255, encoded & 255), axis=-1)
            image = Image.fromarray(rgb.astype("uint8"))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
