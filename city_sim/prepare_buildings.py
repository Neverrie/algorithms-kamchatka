"""Prepare compact building evidence from pinned public inputs.

PDF extraction additionally requires pdfplumber. All changes to the published
scene are reproducible using the source links in the building coverage report.
"""
import argparse
from collections import Counter, defaultdict
import gzip
from hashlib import file_digest
import json
from pathlib import Path
import re

from shapely import STRtree
from pyproj import Transformer
from shapely.geometry import MultiPolygon, box, mapping, shape
from shapely.ops import transform

from .building_evidence import (CHECKS, REGISTER, SUPPLEMENT, address_key,
                                overlap_candidates, projected)
from .geodata import ROOT, CityData, building_height, feature, polygons
from .terrain import BOUNDS

CITY = r"^г\s*\.?\s*петропавловск\s*[-‑–]\s*камчатский\s*,?\s*"
SOURCES = {
    "pkgo_plan": "https://pkgo.ru/upload/iblock/820/sd518j0y5kzsdl2k32isrbps2psmu8sq.pdf",
    "pkgo_plan_updated": "https://www.pkgo.ru/upload/iblock/f68/h3sd22x62dfah5wh63c0165a2oxme7zx.pdf",
    "pkgo_gazette_2012": "https://pkgo.ru/upload/iblock/3e2/3e29b6f3c95264d26853e55b25cf150a.pdf",
}
OVERTURE_URL = "https://stac.overturemaps.org/2026-11-18.0/buildings/building/00511/00511.json"
GBA_URL = "https://huggingface.co/datasets/zhu-xlab/GBA.LoD1"


def save(path, content):
    payload = json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    path.write_bytes(gzip.compress(payload, mtime=0))


def register_rows(pdf, source):
    import pdfplumber

    with pdfplumber.open(pdf) as document:
        for page in document.pages:
            if source == "pkgo_gazette_2012" and page.page_number not in (9, 10, 11):
                continue
            if source != "pkgo_gazette_2012" and page.width < page.height:
                continue
            for table in page.extract_tables():
                for row in table:
                    row = [" ".join((v or "").split()) for v in row]
                    if source == "pkgo_gazette_2012" and len(row) == 4 and row[0].isdigit():
                        _, street, number, year = row
                        floors = ""
                    elif len(row) in (21, 22) and re.match(CITY, row[1], re.I):
                        address = re.sub(CITY, "", row[1], flags=re.I)
                        match = re.fullmatch(r"(.+),\s*д\.?\s*(.+)", address, re.I)
                        if not match:
                            continue
                        street, number = match.groups()
                        year, floors = row[3], row[6]
                    else:
                        continue
                    key = address_key(street, number)
                    if not key:
                        continue
                    year = int(year) if year.isdigit() and 1800 <= int(year) <= 2026 else None
                    floors = int(floors) if floors.isdigit() and 1 <= int(floors) <= 40 else None
                    if year or floors:
                        yield key, f"{street}, {number}", year, floors, page.page_number
            page.close()


def prepare_register(paths):
    sources = {}
    evidence = defaultdict(list)
    for src, path in zip(SOURCES, paths):
        with path.open("rb") as handle:
            sources[src] = {"url": SOURCES[src], "sha256": file_digest(handle, "sha256").hexdigest()}
        for key, address, year, floors, page in register_rows(path, src):
            evidence[key].append((address, year, floors, src, page))

    data = {}
    for key, rows in evidence.items():
        row = {"address": rows[-1][0]}
        for value, at, field in (("year", 1, "year"), ("floors", 2, "floor")):
            eligible = [r for r in rows if r[at] is not None]
            if len({r[at] for r in eligible}) == 1:
                item = eligible[-1]
                row.update({value: item[at], f"{field}_source": item[3], f"{field}_page": item[4]})
            elif eligible:
                row[f"{field}_conflict"] = sorted({r[at] for r in eligible})
        data[key] = row
    save(REGISTER, {"sources": sources, "addresses": data})
    print("register", len(data), sum("floors" in r for r in data.values()), flush=True)


def prepare_overture(path):
    directory = ROOT / "weather_coverage/osm"
    supplements = (ROOT / "data/pkc/depot-east.osm.gz", *(
        p for p in sorted(directory.glob("*.osm.gz")) if p.name not in {"city-boundary.osm.gz", "stops.osm.gz"}))
    city = CityData(ROOT / "data/pkc/city.osm.gz", ROOT / "data/pkc/city.osm.gz", supplements,
                    building_evidence=False)
    baseline = city.osm_layers[0].features
    targets = projected(baseline)
    tree = STRtree(targets)
    scene_ids = {f["id"] for f in baseline}
    features, counts = [], Counter()
    overture = json.loads(path.read_text())["features"]
    for record, metric in zip(overture, projected(overture)):
        counts["source_features"] += 1
        p = record["properties"]
        source = next((s for s in p["sources"] if s.get("dataset") == "OpenStreetMap" and
                       not s.get("property") and re.fullmatch(r"[wr]\d+@\d+", s.get("record_id", ""))), None)
        if source is None:
            counts["skipped_non_osm"] += 1
            continue
        osm_id = source["record_id"].split("@")[0]
        asset = f"osm:{'way' if osm_id[0] == 'w' else 'relation'}:{osm_id[1:]}"
        if asset in scene_ids:
            counts["same_osm_id"] += 1
            continue
        if metric.is_empty or metric.area <= 0:
            counts["skipped_empty"] += 1
            continue
        # A changed or split outline from another snapshot must not overlap the primary map.
        if any(cover >= .20 for _, _, cover in overlap_candidates(metric, targets, tree)):
            counts["overlap_with_baseline"] += 1
            continue
        surfaces = polygons(shape(record["geometry"]))
        if not surfaces:
            continue
        outline = surfaces[0] if len(surfaces) == 1 else MultiPolygon(surfaces)
        tags = {"building": "yes"}
        if p.get("height"):
            tags["height"] = str(p["height"])
        if p.get("num_floors"):
            tags["building:levels"] = str(p["num_floors"])
        attrs = building_height(tags)
        features.append(feature(outline, asset, asset_id=asset, building_type="yes", name=None,
                                data_kind="osm_via_overture", overture_id=p["id"], **attrs))
        scene_ids.add(asset)
    counts["added"] = len(features)
    save(SUPPLEMENT, {"type": "FeatureCollection", "features": features,
                     "metadata": {"url": OVERTURE_URL, "license": "ODbL-1.0",
                                  "release": "2026-11-18.0", "bounds": BOUNDS, **counts}})
    print("overture", counts, flush=True)


def prepare_gba(derived_polygons, odbl_polygons, heights):
    directory = ROOT / "weather_coverage/osm"
    supplements = (ROOT / "data/pkc/depot-east.osm.gz", *(
        p for p in sorted(directory.glob("*.osm.gz")) if p.name not in {"city-boundary.osm.gz", "stops.osm.gz"}))
    city = CityData(ROOT / "data/pkc/city.osm.gz", ROOT / "data/pkc/city.osm.gz", supplements,
                    building_evidence=False)
    from .building_evidence import BuildingEvidence

    scene = city.osm_layers[0].features + BuildingEvidence().supplement["features"]
    targets = projected(scene)
    region = box(*BOUNDS)
    to_wgs84 = Transformer.from_crs(3857, 4326, always_xy=True)
    model_heights = json.loads(heights.read_text())
    matched = {"image": {}, "osm": {}}
    counts = Counter()
    checks = {}
    candidates = []
    tree = STRtree(targets)
    for source, path in (("image", derived_polygons), ("osm", odbl_polygons)):
        for item in json.loads(path.read_text())["features"]:
            p = item["properties"]
            if source == "osm" and p["source"] != "osm":
                continue
            if source == "image" and not p["source"].startswith("ours"):
                continue
            geometry = transform(to_wgs84.transform, shape(item["geometry"]))
            if not geometry.intersects(region):
                continue
            counts[f"{source}_outlines"] += 1
            metric = projected([{"geometry": geometry.__geo_interface__}])[0]
            if metric.is_empty or metric.area <= 0:
                continue
            model = model_heights.get(p["source"] + p["id"] + p["region"], {})
            pairs = sorted(overlap_candidates(metric, targets, tree), key=lambda match: match[1], reverse=True)
            if source == "image" and (not pairs or all(cover < .20 for _, _, cover in pairs)):
                identifier = p["source"] + p["id"] + p["region"]
                candidates.append({"type": "Feature", "id": identifier, "geometry": mapping(geometry),
                                   "properties": {"source": "GlobalBuildingAtlas", "status": "unverified"}})
            if not pairs or pairs[0][1] < .50:
                continue
            idx, iou, _ = pairs[0]
            offset = metric.centroid.distance(targets[idx].centroid)
            if offset > 25 or (len(pairs) > 1 and pairs[1][1] >= iou * .80):
                continue
            record = (iou, offset, model.get("height"), p["source"] + p["id"] + p["region"])
            key = scene[idx]["id"]
            if key not in matched[source] or iou > matched[source][key][0]:
                matched[source][key] = record

    for f in scene:
        key = f["id"]
        evidence = {}
        if key in matched["image"]:
            iou, offset, _, _ = matched["image"][key]
            evidence.update(location_source="global_building_atlas_imagery",
                            location_overlap_iou=round(iou, 3), location_center_offset_m=round(offset, 1))
        for source in ("image", "osm"):
            record = matched[source].get(key)
            if record and record[2] is not None and 2 <= record[2] <= 150:
                evidence.update(model_height_m=round(record[2], 1), model_height_source="global_building_atlas_lod1",
                                model_height_record=record[3])
                break
        if evidence:
            checks[key] = evidence

    from statistics import median

    image = matched["image"]
    metadata = {
        "url": GBA_URL, "license": "CC BY-NC 4.0", "tile": "asiaeast/e155_n55_e160_n50",
        "matching_iou_min": .50, "max_center_offset_m": 25,
        **counts, "image_matches": len(image), "osm_outline_matches": len(matched["osm"]),
        "model_height_matches": sum("model_height_m" in row for row in checks.values()),
        "candidate_outlines": len(candidates),
    }
    if image:
        metadata.update(median_iou=round(median(r[0] for r in image.values()), 3),
                        median_center_offset_m=round(median(r[1] for r in image.values()), 2))
    save(CHECKS, {"buildings": checks, "candidates": candidates, "metadata": metadata})
    print("GlobalBuildingAtlas", metadata, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--municipal", nargs=3, type=Path, metavar=("PLAN", "UPDATE", "GAZETTE"))
    parser.add_argument("--overture", type=Path)
    parser.add_argument("--gba", nargs=3, type=Path, metavar=("DERIVED", "OSM", "LOD1"))
    args = parser.parse_args()
    if args.municipal:
        prepare_register(args.municipal)
    if args.overture:
        prepare_overture(args.overture)
    if args.gba:
        prepare_gba(*args.gba)
