"""Prepare metric map geometry and IDW/nearest-neighbor sampling."""
import base64
import math

import numpy as np
from scipy.spatial import cKDTree
from shapely import contains_xy
from shapely.geometry import box, shape
from shapely.ops import transform

from .analysis import xy
from .experiment import coordinates
from .geography import TO_METRIC
from .heatmap_interpolate import interpolate

STEP_M = 65
MAX_M = 1000
NEIGHBORS = 12
LABELS = {
    "center":(16, 8), "seroglazka":(-14, 0), "ne-upper":(15, -4),
    "ne-lower":(14, -1), "horizon-n":(13, -2), "horizon-s":(13, 10),
    "dachnaya":(-13,-2), "srv":(15, 0), "zavoyko":(-14, 0),
}


def packed(data, dtype):
    return base64.b64encode(np.asarray(data,dtype=dtype).tobytes()).decode("ascii")


def geometry_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type=="Polygon":
        return [[geometry.exterior.coords,*(r.coords for r in geometry.interiors)]]
    if geometry.geom_type in {"LineString","LinearRing"}:
        return [[geometry.coords]]
    if hasattr(geometry,"geoms"):
        return [part for g in geometry.geoms for part in geometry_parts(g)]
    return []


def geometry_model(plan):
    doc=coordinates()
    kinds={}
    for f in doc["features"]:
        kind=f["properties"]["kind"]
        if kind in {"city","water","arterials","footprint"}:
            kinds[kind]=transform(TO_METRIC,shape(f["geometry"]))
    x1,y1,x2,y2=kinds["footprint"].bounds
    x1=math.floor((x1-1350)/STEP_M)*STEP_M
    x2=math.ceil((x2+1350)/STEP_M)*STEP_M
    y1=math.floor((y1-500)/STEP_M)*STEP_M
    y2=math.ceil((y2+500)/STEP_M)*STEP_M
    domain=box(x1,y1,x2,y2)
    nx,ny=round((x2-x1)/STEP_M),round((y2-y1)/STEP_M)
    rows,cols=np.mgrid[:ny,:nx]
    centers_x=x1+(cols+0.5)*STEP_M
    centers_y=y2-(rows+0.5)*STEP_M
    # Include edge pixels for heat. Drawing clips again at the actual map mask.
    edge_mask=kinds["footprint"].buffer(STEP_M*0.72)
    pixels=np.flatnonzero(contains_xy(edge_mask,centers_x,centers_y))
    locations=np.array([xy(p) for p in plan])
    if len(plan)<NEIGHBORS:
        raise ValueError("Нужны опорные точки для построения карт.")
    cxy=np.column_stack([centers_x.ravel()[pixels],centers_y.ravel()[pixels]])
    dist,index=cKDTree(locations).query(cxy,k=NEIGHBORS,distance_upper_bound=MAX_M)
    index=np.where(np.isfinite(dist),index,-1)
    # Transform once to compact grid coordinates. South is down in the viewport.
    def to_grid(x,y):
        return round((x-x1)/STEP_M,2),round((y2-y)/STEP_M,2)

    layers={}
    for kind,g in kinds.items():
        simplify=5 if kind in {"footprint","arterials"} else 12
        if kind=="city":
            g=g.boundary
        context=box(x1-15000,y1-5000,x2+15000,y2+5000) if kind in {"water","city"} else domain
        clipped=g.intersection(context).simplify(simplify,preserve_topology=True)
        layers[kind]=[[[to_grid(*z[:2]) for z in ring] for ring in polygon]
                      for polygon in geometry_parts(clipped)]
    ui_points=[]
    for point, location in zip(plan,locations):
        ui_points.append({
            "id":point["id"], "xy":to_grid(*location),"district":point.get("district"),
            "road":point.get("road"), "kind":point["atlas_kind"], "received":point["received"],
            "height":point.get("elevation_m_dsm"),
            "label_offset":LABELS.get(point["id"]),
            "lat":round(point["latitude"],6), "lon":round(point["longitude"],6),
        })
    return {
        "nx":nx,"ny":ny,"step":STEP_M,"max_m":MAX_M,"k":NEIGHBORS,"layers":layers,
        "points":ui_points,"pixels":pixels,"indices":index,"dist":dist,
    }
