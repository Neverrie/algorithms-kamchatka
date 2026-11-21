"""Load a small, versioned OSM snapshot into directed road segments."""
import gzip
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from math import asin, ceil, cos, radians, sin, sqrt
from pathlib import Path
from xml.etree import ElementTree

ROAD_CLASSES = frozenset({
    "primary", "secondary", "tertiary", "unclassified", "residential",
    "living_street", "primary_link", "secondary_link", "tertiary_link",
})
MOTOR_KEYS = ("access", "vehicle", "motor_vehicle", "motorcar", "hgv")
VEHICLE_DIMENSIONS = ("maxweight", "maxaxleload", "maxheight", "maxwidth", "maxlength")
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_LICENSE_URL = "https://www.openstreetmap.org/copyright"
FACILITY_TAGS = {
    "amenity": {"hospital", "clinic", "fire_station", "school", "kindergarten", "police",
                "social_facility", "bus_station", "ferry_terminal", "university", "college"},
    "healthcare": {"hospital", "clinic", "dialysis"},
    "emergency": {"ambulance_station", "emergency_ward_entrance", "rescue_station", "assembly_point"},
    "man_made": {"water_works", "wastewater_plant", "water_tower"},
    "power": {"plant", "substation"},
    "railway": {"station"},
    "aeroway": {"terminal"},
}


@dataclass(frozen=True)
class Facility:
    site_id: str
    kind: str
    name: str
    lon_lat: tuple[float, float]


@dataclass(frozen=True)
class RoadEdge:
    edge_id: str
    physical_road_id: str
    way_id: int
    from_node: int
    to_node: int
    length_m: int
    road_class: str
    name: str
    source: str = "osm"
    slope_pct: float | None = None
    speed_limit_kmh: int | None = None


@dataclass(frozen=True)
class RoadGraph:
    version: str
    nodes: dict[int, tuple[float, float]]
    edges: dict[str, RoadEdge]
    forbidden_turns: frozenset[tuple[str, str]]
    audit: dict
    facilities: tuple[Facility, ...] = field(default_factory=tuple)


def tags(element) -> dict[str, str]:
    return {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}


def length_m(start, end) -> int:
    lon1, lat1 = map(radians, start)
    lon2, lat2 = map(radians, end)
    half_chord = sin((lat2 - lat1) / 2)**2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2)**2
    return ceil(12_742_017.6 * asin(sqrt(min(1, half_chord))))


def way_filter(metadata, inspection=False):
    if metadata.get("highway") not in ROAD_CLASSES or metadata.get("area") == "yes":
        return "outside_demo_road_classes"
    if any(metadata.get(key, "yes") not in {"yes", "permissive", "designated"}
           for key in MOTOR_KEYS):
        return "access_tag"
    limits = VEHICLE_DIMENSIONS if inspection else (*VEHICLE_DIMENSIONS, "incline")
    if any(key.startswith(limits) or key.endswith(":conditional")
           or (key.startswith("oneway:") and key.split(":")[1] in MOTOR_KEYS)
           for key in metadata):
        return "explicit_dimension_grade_or_special_rule"
    if metadata.get("oneway", "no") not in {"yes", "1", "true", "-1", "no", "0", "false"}:
        return "unsupported_oneway"
    return None


def _read_snapshot(path: Path):
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    root = ElementTree.fromstring(raw)
    if root.tag != "osm" or root.attrib.get("version") != "0.6":
        raise ValueError("expected OSM XML v0.6")
    return raw, root


def load_osm(path: Path) -> RoadGraph:
    raw, root = _read_snapshot(path)
    return _build_graph(root, sha256(raw).hexdigest(), "demo-v1")


def load_osm_snapshots(paths) -> RoadGraph:
    """Merge comparable OSM extracts by object ID and highest mapped version."""
    merged = ElementTree.Element("osm", version="0.6")
    objects = {}
    revision = sha256()
    bounds = set()
    for path in paths:
        raw, root = _read_snapshot(path)
        boxes = root.findall("bounds")
        if not boxes:
            continue
        revision.update(sha256(raw).digest())
        for item in boxes:
            key = tuple(sorted(item.attrib.items()))
            if key not in bounds:
                bounds.add(key)
                merged.append(item)
        for item in root:
            if item.tag not in {"node", "way", "relation"}:
                continue
            key = (item.tag, int(item.attrib["id"]))
            version = int(item.get("version", "0"))
            if key not in objects or version > objects[key][0]:
                objects[key] = (version, item)
    merged.extend(item for _, item in objects.values() if item.get("visible") != "false")
    return _build_graph(merged, revision.hexdigest(), "city-v2", inspection=True)


def _build_graph(root, digest, version, inspection=False):
    boxes = [tuple(float(b.attrib[key]) for key in ("minlon", "minlat", "maxlon", "maxlat"))
             for b in root.findall("bounds")]
    if not boxes:
        raise ValueError("OSM snapshot is missing bounds")
    west, south, east, north = (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )

    def covered(points):
        return any(all(w <= lon <= e and s <= lat <= n for lon, lat in points)
                   for w, s, e, n in boxes)
    nodes = {
        int(node.attrib["id"]): (float(node.attrib["lon"]), float(node.attrib["lat"]))
        for node in root.findall("node")
    }
    node_limits = VEHICLE_DIMENSIONS if inspection else ("max", "incline")
    blocked_nodes = {
        int(node.attrib["id"]) for node in root.findall("node")
        if any(key == "barrier" or key in MOTOR_KEYS or key.startswith(node_limits)
               for key in tags(node))
    }
    resolved, unsupported, excluded_ways = [], [], set()
    for relation in root.findall("relation"):
        metadata = tags(relation)
        restriction = metadata.get("restriction", "")
        if not metadata.get("type", "").startswith("restriction"):
            continue
        members = defaultdict(list)
        for member in relation.findall("member"):
            members[member.attrib["role"]].append((member.attrib["type"], int(member.attrib["ref"])))
        from_ways = [ref for kind, ref in members["from"] if kind == "way"]
        supported = (len(from_ways) == len(members["from"]) == len(members["to"]) == len(members["via"]) == 1
                     and members["via"][0][0] == "node" and members["to"][0][0] == "way"
                     and restriction.startswith(("only_", "no_"))
                     and set(metadata).isdisjoint(
                         key for key in metadata if key.startswith("restriction:") or key == "except"))
        if supported:
            resolved.append((from_ways[0], members["via"][0][1], members["to"][0][1], restriction))
        else:
            unsupported.append(int(relation.attrib["id"]))
            excluded_ways.update(from_ways or (ref for _, ref in members["via"] + members["to"]))

    edges = {}
    outgoing, incoming = defaultdict(list), defaultdict(list)
    excluded = Counter()
    for way in sorted(root.findall("way"), key=lambda item: int(item.attrib["id"])):
        way_id = int(way.attrib["id"])
        metadata = tags(way)
        reason = way_filter(metadata, inspection)
        if way_id in excluded_ways:
            reason = "unsupported_turn_relation"
        if reason:
            excluded[reason] += 1
            continue
        refs = [int(node.attrib["ref"]) for node in way.findall("nd")]
        direction = metadata.get("oneway")
        if direction is None and metadata.get("junction") in {"roundabout", "circular"}:
            direction = "yes"
        for a, b in zip(refs, refs[1:]):
            if a == b or a in blocked_nodes or b in blocked_nodes or a not in nodes or b not in nodes:
                continue
            if not covered((nodes[a], nodes[b])):
                continue
            length = length_m(nodes[a], nodes[b])
            if not length:
                continue
            pairs = []
            if direction != "-1":
                pairs.append((a, b))
            if direction not in {"yes", "true", "1"}:
                pairs.append((b, a))
            physical = f"osm:{way_id}:{min(a, b)}:{max(a, b)}"
            for start, end in pairs:
                edge_id = f"osm:{way_id}:{start}:{end}"
                direction_tag = "forward" if start == a else "backward"
                limit = metadata.get(f"maxspeed:{direction_tag}", metadata.get("maxspeed", ""))
                cap = int(limit) if limit.isascii() and limit.isdigit() and int(limit) > 0 else None
                edge = RoadEdge(edge_id, physical, way_id, start, end, length,
                                metadata["highway"], metadata.get("name", ""), speed_limit_kmh=cap)
                edges[edge_id] = edge
                outgoing[start].append(edge)
                incoming[end].append(edge)

    forbidden = set()
    for source_way, node, dest_way, restriction in resolved:
        for source in incoming[node]:
            if source.way_id != source_way:
                continue
            peers = outgoing[node]
            matches = [edge for edge in peers if edge.way_id == dest_way]
            if source_way == dest_way:
                if restriction.endswith("_u_turn"):
                    matches = [edge for edge in matches if edge.to_node == source.from_node]
                elif restriction == "only_straight_on":
                    matches = [edge for edge in matches if edge.to_node != source.from_node]
                else:
                    # Without a distinct destination way, block ambiguous turns.
                    forbidden.update((source.edge_id, edge.edge_id) for edge in peers)
                    continue
            denied = (edge for edge in peers if edge not in matches) if restriction.startswith("only_") else matches
            forbidden.update((source.edge_id, edge.edge_id) for edge in denied)

    ways = {int(way.attrib["id"]): way for way in root.findall("way")}
    facilities = []
    for item in root:
        metadata = tags(item)
        kind = next((metadata[key] for key, values in FACILITY_TAGS.items()
                     if metadata.get(key) in values), None)
        if kind is None or (kind == "substation" and metadata.get("substation") in {"minor_distribution", "street_cabinet"}):
            continue
        node_id = int(item.attrib["id"])
        if item.tag == "node":
            coordinates = [nodes[node_id]]
        elif item.tag == "way":
            refs = [int(node.attrib["ref"]) for node in item.findall("nd")]
            coordinates = [nodes[ref] for ref in dict.fromkeys(refs) if ref in nodes]
        elif item.tag == "relation":
            outer = [ways.get(int(m.attrib["ref"])) for m in item.findall("member")
                     if m.attrib["type"] == "way" and m.attrib["role"] == "outer"]
            refs = {int(nd.attrib["ref"]) for member in outer if member is not None for nd in member.findall("nd")}
            coordinates = [nodes[ref] for ref in sorted(refs) if ref in nodes]
        else:
            continue
        if coordinates:
            point = tuple(sum(axis) / len(axis) for axis in zip(*coordinates))
            if covered((point,)):
                facilities.append(Facility(f"osm:{item.tag}:{node_id}", kind,
                                           metadata.get("name", ""), point))
    graph_nodes = {node: nodes[node] for node in sorted(set(incoming) | set(outgoing))}
    return RoadGraph(f"osm-{digest[:16]}-{version}", graph_nodes, edges, frozenset(forbidden), {
        "raw_sha256": digest, "bbox": [west, south, east, north], "nodes": len(graph_nodes),
        "edges": len(edges), "via_node_relations": len(resolved),
        "unsupported_relation_ids": sorted(unsupported), "excluded_ways": dict(excluded),
        "forbidden_turn_pairs": len(forbidden), "tile_bounds": boxes,
        "numeric_speed_limit_arcs": sum(edge.speed_limit_kmh is not None for edge in edges.values()),
        "speed_limit_histogram": dict(sorted(Counter(edge.speed_limit_kmh for edge in edges.values()
                                                   if edge.speed_limit_kmh is not None).items())),
    }, tuple(sorted(facilities, key=lambda site: site.site_id)))
