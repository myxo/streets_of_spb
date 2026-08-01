#!/usr/bin/env python3
"""Compare GPX tracks with named OpenStreetMap streets.

The program deliberately uses only Python's standard library.  It downloads an
Overpass JSON response once and caches it, so subsequent runs are fast and can
be performed offline.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


DEFAULT_BBOX = (59.90, 30.18, 60.00, 30.40)
DEFAULT_OVERPASS_URL = "https://overpass.private.coffee/api/interpreter"
FALLBACK_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
EARTH_RADIUS_M = 6_371_008.8

# These do not represent streets that can normally be completed on foot.
EXCLUDED_HIGHWAYS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "construction",
    "proposed",
    "raceway",
    "platform",
}


LatLon = tuple[float, float]
XY = tuple[float, float]


@dataclass(frozen=True)
class Projection:
    lat0: float
    lon0: float

    def xy(self, point: LatLon) -> XY:
        lat, lon = point
        return (
            math.radians(lon - self.lon0)
            * EARTH_RADIUS_M
            * math.cos(math.radians(self.lat0)),
            math.radians(lat - self.lat0) * EARTH_RADIUS_M,
        )


@dataclass
class StreetResult:
    name: str
    total_m: float = 0.0
    walked_m: float = 0.0

    @property
    def completion(self) -> float:
        return self.walked_m / self.total_m if self.total_m else 0.0


@dataclass(frozen=True)
class StreetSegment:
    name: str
    highway: str
    start: LatLon
    end: LatLon


@dataclass(frozen=True)
class Area:
    """A collection of GeoJSON polygons in (latitude, longitude) form."""

    polygons: list[list[list[LatLon]]]
    geojson: dict

    def contains(self, point: LatLon) -> bool:
        for polygon in self.polygons:
            if not polygon or not point_in_ring(point, polygon[0]):
                continue
            if not any(point_in_ring(point, hole) for hole in polygon[1:]):
                return True
        return False

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        points = [point for polygon in self.polygons for ring in polygon for point in ring]
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )


class SegmentIndex:
    """Small spatial grid used for point-to-GPX-segment distance queries."""

    def __init__(self, cell_size_m: float) -> None:
        self.cell_size_m = cell_size_m
        self.cells: dict[tuple[int, int], list[tuple[XY, XY]]] = defaultdict(list)
        self.segment_count = 0

    def _cell(self, point: XY) -> tuple[int, int]:
        return (
            math.floor(point[0] / self.cell_size_m),
            math.floor(point[1] / self.cell_size_m),
        )

    def add(self, start: XY, end: XY, padding_m: float) -> None:
        min_cell = self._cell((min(start[0], end[0]) - padding_m,
                               min(start[1], end[1]) - padding_m))
        max_cell = self._cell((max(start[0], end[0]) + padding_m,
                               max(start[1], end[1]) + padding_m))
        segment = (start, end)
        for x in range(min_cell[0], max_cell[0] + 1):
            for y in range(min_cell[1], max_cell[1] + 1):
                self.cells[(x, y)].append(segment)
        self.segment_count += 1

    def is_near(self, point: XY, distance_m: float) -> bool:
        limit_sq = distance_m * distance_m
        return any(
            point_segment_distance_sq(point, start, end) <= limit_sq
            for start, end in self.cells.get(self._cell(point), ())
        )


def point_segment_distance_sq(point: XY, start: XY, end: XY) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return (point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (
        dx * dx + dy * dy
    )
    t = max(0.0, min(1.0, t))
    nearest = (start[0] + t * dx, start[1] + t * dy)
    return (point[0] - nearest[0]) ** 2 + (point[1] - nearest[1]) ** 2


def distance(start: XY, end: XY) -> float:
    return math.hypot(end[0] - start[0], end[1] - start[1])


def point_in_ring(point: LatLon, ring: Sequence[LatLon]) -> bool:
    """Ray-casting point-in-polygon test, with boundary points included."""
    if len(ring) < 3:
        return False
    y, x = point
    inside = False
    previous_y, previous_x = ring[-1]
    for current_y, current_x in ring:
        # First handle points exactly on an edge (within floating-point noise).
        cross = ((x - previous_x) * (current_y - previous_y)
                 - (y - previous_y) * (current_x - previous_x))
        if abs(cross) < 1e-12 and (
            min(previous_x, current_x) - 1e-12 <= x <= max(previous_x, current_x) + 1e-12
            and min(previous_y, current_y) - 1e-12 <= y <= max(previous_y, current_y) + 1e-12
        ):
            return True
        if (current_y > y) != (previous_y > y):
            crossing_x = previous_x + (
                (current_x - previous_x) * (y - previous_y) / (current_y - previous_y)
            )
            if x < crossing_x:
                inside = not inside
        previous_y, previous_x = current_y, current_x
    return inside


def _geometry_polygons(geometry: dict) -> Iterator[list[list[LatLon]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    raw_polygons = [coordinates] if geometry_type == "Polygon" else coordinates
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        return
    for raw_polygon in raw_polygons:
        polygon: list[list[LatLon]] = []
        for raw_ring in raw_polygon:
            ring: list[LatLon] = []
            for coordinate in raw_ring:
                try:
                    ring.append((float(coordinate[1]), float(coordinate[0])))
                except (IndexError, TypeError, ValueError):
                    continue
            if len(ring) >= 3:
                polygon.append(ring)
        if polygon:
            yield polygon


def area_from_geojson(data: dict) -> Area:
    polygons: list[list[list[LatLon]]] = []
    data_type = data.get("type")
    if data_type == "FeatureCollection":
        for feature in data.get("features", []):
            polygons.extend(_geometry_polygons(feature.get("geometry", {})))
    elif data_type == "Feature":
        polygons.extend(_geometry_polygons(data.get("geometry", {})))
    else:
        polygons.extend(_geometry_polygons(data))
    if not polygons:
        raise ValueError("GeoJSON contains no Polygon or MultiPolygon geometry")
    return Area(polygons, data)


def rectangular_area(bbox: tuple[float, float, float, float]) -> Area:
    south, west, north, east = bbox
    coordinates = [[[west, south], [east, south], [east, north], [west, north], [west, south]]]
    return area_from_geojson({"type": "Polygon", "coordinates": coordinates})


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_gpx(path: Path) -> list[list[LatLon]]:
    """Read tracks and routes, without relying on a particular GPX namespace."""
    root = ET.parse(path).getroot()
    lines: list[list[LatLon]] = []
    for element in root.iter():
        if local_name(element.tag) not in {"trkseg", "rte"}:
            continue
        line: list[LatLon] = []
        for child in element.iter():
            if local_name(child.tag) not in {"trkpt", "rtept"}:
                continue
            try:
                line.append((float(child.attrib["lat"]), float(child.attrib["lon"])))
            except (KeyError, ValueError):
                continue
        if line:
            lines.append(line)
    return lines


def load_tracks(directory: Path) -> tuple[list[list[LatLon]], list[str]]:
    lines: list[list[LatLon]] = []
    warnings: list[str] = []
    for path in sorted(directory.glob("*.gpx")):
        try:
            file_lines = read_gpx(path)
            if not file_lines:
                warnings.append(f"{path}: contains no track or route points")
            lines.extend(file_lines)
        except (ET.ParseError, OSError) as error:
            warnings.append(f"{path}: {error}")
    return lines, warnings


def build_track_index(
    tracks: Iterable[Sequence[LatLon]],
    projection: Projection,
    tolerance_m: float,
    max_gap_m: float,
) -> SegmentIndex:
    index = SegmentIndex(max(tolerance_m, 5.0))
    for track in tracks:
        projected = [projection.xy(point) for point in track]
        for start, end in zip(projected, projected[1:]):
            # GPS being switched off and on must not create an imaginary walk.
            if distance(start, end) <= max_gap_m:
                index.add(start, end, tolerance_m)
    return index


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        south, west, north, east = (float(part) for part in value.split(","))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "bbox must be SOUTH,WEST,NORTH,EAST"
        ) from error
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise argparse.ArgumentTypeError("bbox coordinates or ordering are invalid")
    return south, west, north, east


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    box = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:180];
(
  way[\"highway\"][\"name\"]({box});
);
out tags geom;"""


def fetch_overpass_tile(
    bbox: tuple[float, float, float, float],
    cache_dir: Path,
    endpoints: Sequence[str],
    refresh: bool,
) -> dict:
    query = overpass_query(bbox)
    digest = hashlib.sha256(query.encode()).hexdigest()[:16]
    cache_path = cache_dir / f"overpass-{digest}.json"
    if cache_path.exists() and not refresh:
        with cache_path.open(encoding="utf-8") as source:
            return json.load(source)

    last_error: Exception | None = None
    payload: bytes | None = None
    for endpoint in endpoints:
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode({"data": query}).encode(),
            headers={
                "User-Agent": "streets-of-spb/1.0 (personal street coverage project)",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=210) as response:
                payload = response.read()
            break
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            print(f"Warning: Overpass request failed at {endpoint}: {error}", file=sys.stderr)
    if payload is None:
        if cache_path.exists():
            print("Warning: using cached map tile instead", file=sys.stderr)
            with cache_path.open(encoding="utf-8") as source:
                return json.load(source)
        raise RuntimeError(f"could not download OpenStreetMap data: {last_error}")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("Overpass returned a response that is not JSON") from error
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return data


def split_bbox(
    bbox: tuple[float, float, float, float], rows: int = 3, columns: int = 3
) -> list[tuple[float, float, float, float]]:
    south, west, north, east = bbox
    latitude_step = (north - south) / rows
    longitude_step = (east - west) / columns
    return [
        (
            south + row * latitude_step,
            west + column * longitude_step,
            south + (row + 1) * latitude_step,
            west + (column + 1) * longitude_step,
        )
        for row in range(rows)
        for column in range(columns)
    ]


def fetch_osm(
    bbox: tuple[float, float, float, float],
    cache_dir: Path,
    endpoint: str,
    refresh: bool,
) -> dict:
    """Download a small tile set and deduplicate ways crossing tile edges."""
    endpoints = (endpoint,) if endpoint != DEFAULT_OVERPASS_URL else (
        DEFAULT_OVERPASS_URL,
        *FALLBACK_OVERPASS_URLS,
    )
    elements_by_id: dict[tuple[str, int], dict] = {}
    tiles = split_bbox(bbox)
    for number, tile in enumerate(tiles, 1):
        print(f"Downloading/checking map tile {number}/{len(tiles)}...")
        data = fetch_overpass_tile(tile, cache_dir, endpoints, refresh)
        for element in data.get("elements", []):
            try:
                key = (str(element["type"]), int(element["id"]))
            except (KeyError, TypeError, ValueError):
                continue
            elements_by_id[key] = element
    return {"elements": list(elements_by_id.values())}


def in_bbox(point: LatLon, bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def osm_segments(data: dict, area: Area) -> Iterator[StreetSegment]:
    for element in data.get("elements", []):
        if element.get("type") != "way":
            continue
        tags = element.get("tags", {})
        name = " ".join(str(tags.get("name", "")).split())
        highway = str(tags.get("highway", ""))
        access = str(tags.get("access", ""))
        if not name or highway in EXCLUDED_HIGHWAYS or access in {"private", "no"}:
            continue
        geometry: list[LatLon] = []
        for node in element.get("geometry", []):
            try:
                geometry.append((float(node["lat"]), float(node["lon"])))
            except (KeyError, TypeError, ValueError):
                continue
        for start, end in zip(geometry, geometry[1:]):
            # Several points catch ordinary bridge/canal crossings while still
            # preventing complete ways from leaking outside the polygon.
            checkpoints = (
                start,
                end,
                ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2),
            )
            if any(area.contains(point) for point in checkpoints):
                yield StreetSegment(name, highway, start, end)


def merge_osm_data(items: Iterable[dict]) -> dict:
    elements_by_id: dict[tuple[str, int], dict] = {}
    anonymous: list[dict] = []
    for data in items:
        for element in data.get("elements", []):
            try:
                key = (str(element["type"]), int(element["id"]))
            except (KeyError, TypeError, ValueError):
                anonymous.append(element)
                continue
            elements_by_id[key] = element
    return {"elements": [*elements_by_id.values(), *anonymous]}


def sampled_coverage(
    segment: StreetSegment,
    projection: Projection,
    index: SegmentIndex,
    sample_step_m: float,
    tolerance_m: float,
) -> tuple[float, int, int]:
    start = projection.xy(segment.start)
    end = projection.xy(segment.end)
    length_m = distance(start, end)
    sample_count = max(1, math.ceil(length_m / sample_step_m))
    covered = 0
    for number in range(sample_count):
        fraction = (number + 0.5) / sample_count
        point = (
            start[0] + fraction * (end[0] - start[0]),
            start[1] + fraction * (end[1] - start[1]),
        )
        covered += index.is_near(point, tolerance_m)
    return length_m, covered, sample_count


def normalize_name(name: str) -> str:
    # Russian OSM names are occasionally split between е/ё spellings.
    return " ".join(name.casefold().replace("ё", "е").split())


def analyze(
    segments: Iterable[StreetSegment],
    projection: Projection,
    index: SegmentIndex,
    sample_step_m: float,
    tolerance_m: float,
) -> tuple[list[StreetResult], list[dict]]:
    grouped: dict[str, StreetResult] = {}
    features: list[dict] = []
    for segment in segments:
        length_m, covered, samples = sampled_coverage(
            segment, projection, index, sample_step_m, tolerance_m
        )
        key = normalize_name(segment.name)
        result = grouped.setdefault(key, StreetResult(segment.name))
        result.total_m += length_m
        result.walked_m += length_m * covered / samples
        segment_completion = covered / samples
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": segment.name,
                    "highway": segment.highway,
                    "completion": round(segment_completion * 100, 1),
                    "status": (
                        "walked" if covered == samples else
                        "unwalked" if covered == 0 else "partial"
                    ),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [segment.start[1], segment.start[0]],
                        [segment.end[1], segment.end[0]],
                    ],
                },
            }
        )
    results = sorted(grouped.values(), key=lambda item: (item.completion, -item.total_m, item.name))
    return results, features


def status(result: StreetResult, complete_at: float) -> str:
    if result.completion >= complete_at:
        return "complete"
    if result.walked_m > 0:
        return "partial"
    return "not started"


def write_report(path: Path, results: Sequence[StreetResult], complete_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(
            ["street", "status", "completion_percent", "total_m", "walked_m", "remaining_m"]
        )
        for result in results:
            writer.writerow(
                [
                    result.name,
                    status(result, complete_at),
                    f"{result.completion * 100:.1f}",
                    f"{result.total_m:.0f}",
                    f"{result.walked_m:.0f}",
                    f"{max(0.0, result.total_m - result.walked_m):.0f}",
                ]
            )


def point_line_distance(point: XY, start: XY, end: XY) -> float:
    return math.sqrt(point_segment_distance_sq(point, start, end))


def simplify_line(line: Sequence[LatLon], projection: Projection, epsilon_m: float) -> list[LatLon]:
    """Iterative Ramer-Douglas-Peucker simplification for compact HTML output."""
    if len(line) <= 2:
        return list(line)
    xy = [projection.xy(point) for point in line]
    keep = {0, len(line) - 1}
    stack = [(0, len(line) - 1)]
    while stack:
        first, last = stack.pop()
        furthest_distance = -1.0
        furthest_index = first
        for index in range(first + 1, last):
            candidate = point_line_distance(xy[index], xy[first], xy[last])
            if candidate > furthest_distance:
                furthest_distance = candidate
                furthest_index = index
        if furthest_distance > epsilon_m:
            keep.add(furthest_index)
            stack.append((first, furthest_index))
            stack.append((furthest_index, last))
    return [point for index, point in enumerate(line) if index in keep]


def write_geojson(path: Path, features: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump({"type": "FeatureCollection", "features": features}, output, ensure_ascii=False)


def write_map(
    path: Path,
    features: Sequence[dict],
    tracks: Sequence[Sequence[LatLon]],
    projection: Projection,
    bbox: tuple[float, float, float, float],
    results: Sequence[StreetResult],
    complete_at: float,
    area_geojson: dict,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    track_coordinates = [
        [[lon, lat] for lat, lon in simplify_line(line, projection, 4.0)]
        for line in tracks
        if len(line) >= 2
    ]
    map_data = json.dumps(
        {"type": "FeatureCollection", "features": features},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    tracks_data = json.dumps(
        {"type": "MultiLineString", "coordinates": track_coordinates},
        separators=(",", ":"),
    )
    area_data = json.dumps(
        area_geojson, ensure_ascii=False, separators=(",", ":")
    ).replace("<", "\\u003c")
    incomplete = sum(result.completion < complete_at for result in results)
    escaped_title = html.escape(f"Streets left: {incomplete} of {len(results)}")
    south, west, north, east = bbox
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    .summary {{ background: white; padding: 8px 12px; border-radius: 4px;
      box-shadow: 0 1px 5px #777; font: 14px/1.4 system-ui, sans-serif; }}
    .legend i {{ display:inline-block; width:12px; height:4px; margin:0 6px 3px 0; }}
  </style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const coverage = {map_data};
const tracks = {tracks_data};
const centerArea = {area_data};
const map = L.map('map', {{preferCanvas:true}});
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}}).addTo(map);
const colors = {{walked:'#16803a', partial:'#e69f00', unwalked:'#d62728'}};
L.geoJSON(coverage, {{
  style: feature => ({{color:colors[feature.properties.status], weight:4, opacity:.85}}),
  onEachFeature: (feature, layer) => layer.bindPopup(
    '<b>' + feature.properties.name + '</b><br>' +
    feature.properties.completion + '% of this section'
  )
}}).addTo(map);
L.geoJSON(tracks, {{style:{{color:'#1769aa', weight:2, opacity:.35}}}}).addTo(map);
L.geoJSON(centerArea, {{style:{{color:'#6f42c1', weight:2, opacity:.9,
  fillOpacity:.03, dashArray:'6 5'}}}}).addTo(map);
map.fitBounds([[{south}, {west}], [{north}, {east}]]);
const summary = L.control({{position:'topright'}});
summary.onAdd = () => {{
  const div = L.DomUtil.create('div', 'summary legend');
  div.innerHTML = '<b>{escaped_title}</b><br>' +
    '<i style="background:#d62728"></i>not walked<br>' +
    '<i style="background:#e69f00"></i>partly walked<br>' +
    '<i style="background:#16803a"></i>walked<br>' +
    '<i style="background:#1769aa"></i>your GPS tracks';
  return div;
}};
summary.addTo(map);
</script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find named OpenStreetMap streets not covered by GPX tracks."
    )
    parser.add_argument("--tracks", type=Path, default=Path("tracks"), help="GPX directory")
    parser.add_argument(
        "--bbox",
        type=parse_bbox,
        default=None,
        metavar="S,W,N,E",
        help="rectangular boundary; overrides --area",
    )
    parser.add_argument(
        "--area",
        type=Path,
        default=Path("center.geojson"),
        help="GeoJSON Polygon/MultiPolygon boundary (default: %(default)s)",
    )
    parser.add_argument("--output", type=Path, default=Path("output"), help="output directory")
    parser.add_argument("--cache", type=Path, default=Path(".cache"), help="OSM cache directory")
    parser.add_argument("--refresh", action="store_true", help="redownload OSM map data")
    parser.add_argument("--tolerance", type=float, default=25.0, metavar="METRES")
    parser.add_argument("--sample-step", type=float, default=12.0, metavar="METRES")
    parser.add_argument(
        "--max-gps-gap",
        type=float,
        default=250.0,
        metavar="METRES",
        help="do not join GPX points further apart than this",
    )
    parser.add_argument(
        "--complete-at",
        type=float,
        default=90.0,
        metavar="PERCENT",
        help="street completion threshold",
    )
    parser.add_argument(
        "--osm-file",
        type=Path,
        action="append",
        help="use an Overpass JSON file instead of downloading; may be repeated",
    )
    parser.add_argument("--overpass-url", default=DEFAULT_OVERPASS_URL, help=argparse.SUPPRESS)
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.tracks.is_dir():
        parser.error(f"GPX directory does not exist: {args.tracks}")
    if args.tolerance <= 0 or args.sample_step <= 0 or args.max_gps_gap <= 0:
        parser.error("distance options must be greater than zero")
    if not 0 < args.complete_at <= 100:
        parser.error("--complete-at must be between 0 and 100")
    for osm_file in args.osm_file or []:
        if not osm_file.is_file():
            parser.error(f"OSM file does not exist: {osm_file}")
    if not args.bbox and not args.area.is_file():
        parser.error(f"area file does not exist: {args.area}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    try:
        if args.bbox:
            area = rectangular_area(args.bbox)
        else:
            with args.area.open(encoding="utf-8") as source:
                area = area_from_geojson(json.load(source))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(f"could not read analysis area: {error}")
    bbox = area.bbox
    projection = Projection((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

    tracks, warnings = load_tracks(args.tracks)
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if not tracks:
        parser.error(f"no GPX track points found in {args.tracks}")
    print(f"Loaded {len(tracks)} track segments from {args.tracks}")
    index = build_track_index(tracks, projection, args.tolerance, args.max_gps_gap)
    print(f"Indexed {index.segment_count:,} GPS segments")

    try:
        if args.osm_file:
            osm_items = []
            for osm_file in args.osm_file:
                with osm_file.open(encoding="utf-8") as source:
                    osm_items.append(json.load(source))
            osm_data = merge_osm_data(osm_items)
        else:
            print("Loading named streets from OpenStreetMap (the first run can take a minute)...")
            osm_data = fetch_osm(bbox, args.cache, args.overpass_url, args.refresh)
    except (OSError, json.JSONDecodeError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    segments = list(osm_segments(osm_data, area))
    if not segments:
        print("Error: OpenStreetMap response contains no usable named streets", file=sys.stderr)
        return 1
    results, features = analyze(
        segments, projection, index, args.sample_step, args.tolerance
    )
    threshold = args.complete_at / 100
    write_report(args.output / "streets.csv", results, threshold)
    write_geojson(args.output / "coverage.geojson", features)
    write_map(
        args.output / "map.html", features, tracks, projection, bbox, results, threshold,
        area.geojson,
    )

    incomplete = [result for result in results if result.completion < threshold]
    total_length = sum(result.total_m for result in results)
    walked_length = sum(result.walked_m for result in results)
    print(
        f"Found {len(results)} streets; {len(incomplete)} remain below "
        f"{args.complete_at:g}% completion."
    )
    if total_length:
        print(f"Estimated network coverage: {walked_length / total_length * 100:.1f}%")
    print(f"Report: {args.output / 'streets.csv'}")
    print(f"Map:    {args.output / 'map.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
