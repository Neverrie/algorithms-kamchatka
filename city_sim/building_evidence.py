"""Address evidence and geometrical reconciliation for the city buildings."""
from collections import Counter, defaultdict
from functools import cached_property
import gzip
import json
import re

from shapely import STRtree, make_valid
from shapely.geometry import shape
from shapely.ops import transform

from .terrain import DATA, TO_METRIC

REGISTER = DATA / "building-register.json.gz"
SUPPLEMENT = DATA / "building-overture.geojson.gz"
CHECKS = DATA / "building-location-checks.json.gz"

STREET_TYPE = (r"улица|ул\.?|бульвар|б\s*[-‑]?\s*р\.?|проспект|пр\s*[-‑]?\s*кт\.?|"
               r"пр-т\.?|проезд|пр-д\.?|переулок|пер\.?|площадь|пл\.?|шоссе|наб\.?|набережная")
LETTERS = str.maketrans(dict(zip("abekmhopctyx", "авекмнорстух")))


def address_key(street, number):
    if not street or not number:
        return None
    street = " ".join(street.casefold().replace("ё", "е").split()).strip(" ,.")
    street = re.sub(rf"^(?:{STREET_TYPE})\s+|\s+(?:{STREET_TYPE})$", "", street)
    number = number.casefold().replace("ё", "е")
    number = re.sub(r"^\s*д(?:ом)?\.?\s*", "", number)
    number = re.sub(r"\s*корп(?:ус)?\.?\s*", "к", number)
    number = re.sub(r"\s+", "", number).translate(LETTERS)
    if not street or not re.fullmatch(r"\d+[а-я]?(?:[/к-]\d+[а-я]?)?", number):
        return None
    return f"{street}|{number}"


def public_address(meta):
    street = meta.get("addr:street") or meta.get("addr:place")
    number = meta.get("addr:housenumber")
    key = address_key(street, number)
    return (key, f"{street}, {number}") if key else (None, None)


def load_json(path, default):
    if not path.is_file():
        return default
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def projected(features):
    return [make_valid(transform(TO_METRIC.transform, shape(f["geometry"]))) for f in features]


def overlap_candidates(geometry, targets, tree):
    for index in tree.query(geometry, predicate="intersects"):
        other = targets[index]
        common = geometry.intersection(other).area
        if common:
            yield int(index), common / (geometry.area + other.area - common), common / min(geometry.area, other.area)


class BuildingEvidence:
    paths = (REGISTER, SUPPLEMENT, CHECKS)

    @cached_property
    def register(self):
        return load_json(REGISTER, {"addresses": {}, "sources": {}})

    @cached_property
    def supplement(self):
        return load_json(SUPPLEMENT, {"features": [], "metadata": {}})

    @cached_property
    def checks(self):
        return load_json(CHECKS, {"buildings": {}, "metadata": {}})

    def enrich(self, features, node_addresses):
        registry = self.register["addresses"]
        geometry = [shape(f["geometry"]) for f in features]
        index = STRtree(geometry)
        unique = defaultdict(set)

        for idx, f in enumerate(features):
            key = f["properties"].get("address_key")
            if key in registry:
                unique[key].add(idx)

        # Address points are used only if the point identifies one polygon and the polygon
        # does not carry a different building address. Candidate links stay one-to-one.
        for key, point in node_addresses:
            if key not in registry:
                continue
            candidates = list(index.query(point, predicate="within"))
            if len(candidates) == 1:
                idx = int(candidates[0])
                polygon_key = features[idx]["properties"].get("address_key")
                if polygon_key in (None, key):
                    unique[key].add(idx)

        reverse = defaultdict(list)
        for key, indexes in unique.items():
            if len(indexes) == 1:
                reverse[next(iter(indexes))].append(key)
        used = set()
        outcomes = Counter()
        for idx, f in enumerate(features):
            props = f["properties"]
            evidence = self.checks["buildings"].get(f["id"])
            if evidence:
                props.update(evidence)
                if props["height_source"] == "unknown" and evidence.get("model_height_m") is not None:
                    estimate = evidence["model_height_m"]
                    props.update(height_m=estimate, render_height_m=estimate,
                                 height_source="estimated_from_gba", render_height_is_placeholder=False)
                    outcomes["render_height_from_gba"] += 1
            keys = reverse[idx]
            if len(keys) != 1:
                continue
            key = keys[0]
            row = registry[key]
            used.add(key)
            props["address"] = row["address"]
            if row.get("year") is not None:
                props["construction_year"] = row["year"]
                props["construction_year_source"] = row["year_source"]
                props["construction_year_page"] = row["year_page"]
            if row.get("year_conflict") or row.get("floor_conflict"):
                props["height_flags"].append("municipal_register_disagreement")
            floors = row.get("floors")
            if floors is None:
                continue
            props["registered_floors"] = floors
            props["registered_floors_source"] = row["floor_source"]
            props["registered_floors_page"] = row["floor_page"]
            if props["levels"] is not None and props["levels"] != floors:
                props["height_flags"].append("register_osm_floors_disagree")
                outcomes["register_osm_floors_disagree"] += 1
            if props["physical_height_m"] is None:
                if props["height_source"] == "estimated_from_gba":
                    outcomes["render_height_from_gba"] -= 1
                props.update(height_m=floors * 3.0, render_height_m=floors * 3.0,
                             render_height_is_placeholder=False, height_source="estimated_from_register_floors",
                             height_low_m=round(floors * 2.7, 1), height_high_m=round(floors * 3.3, 1))
                outcomes["render_height_from_register"] += 1
        return {"register_addresses": len(registry), "register_matched": len(used),
                "register_unmatched": len(registry) - len(used),
                "register_with_floors": sum(row.get("floors") is not None for row in registry.values()),
                "overture": self.supplement["metadata"], "location_check": self.checks["metadata"],
                "sources": self.register["sources"], **outcomes}
