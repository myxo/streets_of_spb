# Streets of Saint Petersburg

`streets_left.py` compares all GPX recordings in `tracks/` with current named
streets from OpenStreetMap. It produces:

- `output/streets.csv` — every street, its estimated completion, and metres left;
- `output/streets_left.csv` — only streets below the completion threshold;
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

The default boundary is the checked-in [`center.geojson`](center.geojson). It is
built from a convex hull around Vasilyevsky, Krestovsky, Kamenny, Petrogradsky,
Aptekarsky, Yelagin, and Monastyrsky islands plus the main historic centre down
to the Obvodny Canal. Petrovsky Island and the gaps between these areas are
therefore inside the polygon as well. The hull is clipped along the Neva and
Bolshaya Nevka to exclude Vyborgskaya Storona and the east-bank
Krasnogvardeysky/Malaya Okhta area. The northern edge follows the full
OpenStreetMap centerline of the Bolshaya Nevka, and the southern edge follows
the Obvodny Canal centerline. On the southwest, the edge follows the
Ekateringofka from its Bolshaya Neva entrance to the Obvodny mouth, excluding
Gutuevsky Island. The purple dashed line on the map shows this project boundary.
Its source points are based on OpenStreetMap geometry.

To experiment with a rectangular area instead, pass `SOUTH,WEST,NORTH,EAST`:

```sh
python3 streets_left.py --bbox 59.90,30.24,59.97,30.39
```

`--bbox` overrides the GeoJSON area. You can also supply a different polygon or
multipolygon with `--area my-area.geojson`. Changing the area changes both the
street list and the completion percentage.

## How matching works

The program samples every named, publicly accessible OSM road and checks whether
a GPX line passes within 25 metres. It deliberately ignores motorways, private
roads, construction, and unnamed paths. A street is considered complete at 30%
coverage; these defaults accommodate ordinary phone GPS error and gaps at
intersections. Names containing `проезд` or `переулок` are excluded from the
analysis, map, reports, and statistics.

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

Map colors are applied to approximately 12-metre street parts: green means that
part was matched to a GPS track; yellow means it was not walked but its street
passed the completion threshold; red means it was not walked and its street has
not passed the threshold. The map shows either statistics or tracks, never both:
use **Show tracks** and **Show statistics** to switch views. GPS tracks are drawn
as opaque bright-blue lines with a white outline for visibility.

## Publish the map with GitHub Pages

The generated `output/map.html` is committed to the repository and deployed as
the GitHub Pages home page by `.github/workflows/pages.yml`. After refreshing
the data, commit the updated map and push it:

```sh
python3 streets_left.py --refresh
git add output/map.html
git commit -m "update street coverage map"
git push
```

For the first deployment, open the repository's **Settings → Pages** and set
**Source** to **GitHub Actions**. The map contains simplified versions of the
GPS tracks, so publishing it also makes those routes public.

## Tests

```sh
python3 -m unittest -v
```
