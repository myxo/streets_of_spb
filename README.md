# Streets of Saint Petersburg

`streets_left.py` compares all GPX recordings in `tracks/` with current named
streets from OpenStreetMap. It produces:

- `output/streets.csv` — every street, its estimated completion, and metres left;
- `output/map.html` — an interactive red/yellow/green coverage map;
- `output/coverage.geojson` — the raw coverage layer for QGIS or another map app.

The program has no third-party Python dependencies. Python 3.10 or newer is
recommended.

## Run it

```sh
python3 streets_left.py
open output/map.html
```

The first run downloads named streets using the OpenStreetMap Overpass API and
caches the response in `.cache/`. Later runs reuse that cache. Refresh it after
map edits with:

```sh
python3 streets_left.py --refresh
```

The default boundary is `59.90,30.18,60.00,30.40`, chosen to include the area
covered by the GPX files currently in this project. Set the exact area that you
mean by “centre” with `SOUTH,WEST,NORTH,EAST`, for example:

```sh
python3 streets_left.py --bbox 59.90,30.24,59.97,30.39
```

This is important: changing the box changes both the street list and the
completion percentage.

## How matching works

The program samples every named, publicly accessible OSM road and checks whether
a GPX line passes within 25 metres. It deliberately ignores motorways, private
roads, construction, and unnamed paths. A street is considered complete at 90%
coverage; these defaults accommodate ordinary phone GPS error and gaps at
intersections.

Useful adjustments:

```sh
# Stricter GPS matching and completion
python3 streets_left.py --tolerance 15 --complete-at 95

# Keep using an Overpass JSON export supplied manually
python3 streets_left.py --osm-file streets.json

# See every option
python3 streets_left.py --help
```

The result is an estimate. Parallel streets closer than the tolerance, tunnels,
bridges, inaccurate GPS points, and incomplete OpenStreetMap data can affect it.
The colored map is intended for visually checking those cases.

## Tests

```sh
python3 -m unittest -v
```
