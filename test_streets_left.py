import csv
import json
import tempfile
import unittest
from pathlib import Path

import streets_left as app


class StreetsLeftTests(unittest.TestCase):
    def test_default_completion_threshold_is_thirty_percent(self):
        self.assertEqual(app.make_parser().parse_args([]).complete_at, 30.0)

    def test_project_center_covers_selected_islands_and_excludes_vyborg_side(self):
        with Path("center.geojson").open(encoding="utf-8") as source:
            area = app.area_from_geojson(json.load(source))
        self.assertEqual(len(area.polygons), 1)
        for point in (
            (59.94, 30.25),   # Vasilyevsky
            (59.97, 30.25),   # Krestovsky
            (59.978, 30.29),  # Kamenny
            (59.96, 30.31),   # Petrogradsky
            (59.973, 30.32),  # Aptekarsky
            (59.98, 30.255),  # Yelagin
            (59.92, 30.389),  # Monastyrsky
            (59.96, 30.25),   # Petrovsky, enclosed by the hull
            (59.93, 30.35),   # mainland centre
            (59.916, 30.35),  # north bank of the Obvodny Canal
            (59.980, 30.30),  # south side of the Bolshaya Nevka
        ):
            self.assertTrue(area.contains(point), point)

        for point in (
            (59.96, 30.35),
            (59.97, 30.35),
            (59.93, 30.399),  # Malookhtinskaya Embankment, east bank
            (59.914, 30.35),  # south bank of the Obvodny Canal
            (59.983, 30.30),  # north side of the Bolshaya Nevka
        ):
            self.assertFalse(area.contains(point), point)

    def test_gpx_parser_accepts_namespaced_track(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "walk.gpx"
            path.write_text(
                """<gpx xmlns="http://www.topografix.com/GPX/1/1">
                <trk><trkseg>
                  <trkpt lat="59.93" lon="30.30"/>
                  <trkpt lat="59.94" lon="30.31"/>
                </trkseg></trk></gpx>""",
                encoding="utf-8",
            )
            self.assertEqual(
                app.read_gpx(path), [[(59.93, 30.30), (59.94, 30.31)]]
            )

    def test_spatial_index_uses_distance_to_line(self):
        index = app.SegmentIndex(25)
        index.add((0, 0), (100, 0), 25)
        self.assertTrue(index.is_near((50, 24), 25))
        self.assertFalse(index.is_near((50, 26), 25))

    def test_bbox_can_be_split_into_four_exact_tiles(self):
        tiles = app.split_bbox((0, 10, 2, 14), rows=2, columns=2)
        self.assertEqual(
            tiles,
            [(0, 10, 1, 12), (0, 12, 1, 14), (1, 10, 2, 12), (1, 12, 2, 14)],
        )

    def test_geojson_area_includes_outer_ring_but_excludes_hole(self):
        area = app.area_from_geojson(
            {
                "type": "Polygon",
                "coordinates": [
                    [[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]],
                    [[30.4, 59.4], [30.6, 59.4], [30.6, 59.6], [30.4, 59.6], [30.4, 59.4]],
                ],
            }
        )
        self.assertTrue(area.contains((59.2, 30.2)))
        self.assertFalse(area.contains((59.5, 30.5)))
        self.assertFalse(area.contains((61, 32)))
        self.assertEqual(area.bbox, (59, 30, 60, 31))

    def test_osm_filtering_and_coverage(self):
        bbox = (59.90, 30.20, 60.00, 30.40)
        osm = {
            "elements": [
                {
                    "type": "way",
                    "tags": {"name": "Тестовая улица", "highway": "residential"},
                    "geometry": [
                        {"lat": 59.93, "lon": 30.30},
                        {"lat": 59.93, "lon": 30.31},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"name": "Private", "highway": "service", "access": "private"},
                    "geometry": [
                        {"lat": 59.94, "lon": 30.30},
                        {"lat": 59.94, "lon": 30.31},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"name": "Тестовый Проезд", "highway": "residential"},
                    "geometry": [
                        {"lat": 59.95, "lon": 30.30},
                        {"lat": 59.95, "lon": 30.31},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"name": "Тестовый переулок", "highway": "residential"},
                    "geometry": [
                        {"lat": 59.96, "lon": 30.30},
                        {"lat": 59.96, "lon": 30.31},
                    ],
                },
            ]
        }
        segments = list(app.osm_segments(osm, app.rectangular_area(bbox)))
        self.assertEqual(len(segments), 1)

        projection = app.Projection(59.95, 30.30)
        tracks = [[(59.93, 30.30), (59.93, 30.31)]]
        index = app.build_track_index(tracks, projection, 25, 1000)
        results, features = app.analyze(segments, projection, index, 12, 25, 0.30)
        self.assertEqual(results[0].name, "Тестовая улица")
        self.assertAlmostEqual(results[0].completion, 1.0)
        self.assertEqual(features[0]["properties"]["status"], "walked")

    def test_map_colors_unwalked_parts_by_whole_street_threshold(self):
        projection = app.Projection(59.93, 30.30)
        walked = app.StreetSegment(
            "Passed Street", "residential", (59.93, 30.300), (59.93, 30.301)
        )
        remaining = app.StreetSegment(
            "Passed Street", "residential", (59.93, 30.302), (59.93, 30.303)
        )
        failed = app.StreetSegment(
            "Failed Street", "residential", (59.94, 30.300), (59.94, 30.301)
        )
        index = app.build_track_index(
            [[walked.start, walked.end]], projection, tolerance_m=10, max_gap_m=1000
        )
        _results, features = app.analyze(
            [walked, remaining, failed], projection, index, 12, 10, 0.30
        )
        statuses = {}
        for feature in features:
            statuses.setdefault(feature["properties"]["name"], set()).add(
                feature["properties"]["status"]
            )
        self.assertEqual(
            statuses["Passed Street"], {"walked", "remaining_complete"}
        )
        self.assertEqual(statuses["Failed Street"], {"remaining_incomplete"})

    def test_osm_tile_merge_deduplicates_ways(self):
        way = {"type": "way", "id": 7, "tags": {"name": "A"}}
        merged = app.merge_osm_data([{"elements": [way]}, {"elements": [way]}])
        self.assertEqual(merged["elements"], [way])

    def test_end_to_end_writes_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracks = root / "tracks"
            tracks.mkdir()
            (tracks / "walk.gpx").write_text(
                """<gpx><trk><trkseg>
                <trkpt lat="59.9300" lon="30.3000"/>
                <trkpt lat="59.9300" lon="30.3010"/>
                </trkseg></trk></gpx>""",
                encoding="utf-8",
            )
            osm_path = root / "osm.json"
            osm_path.write_text(
                json.dumps(
                    {
                        "elements": [
                            {
                                "type": "way",
                                "tags": {"name": "Example Street", "highway": "residential"},
                                "geometry": [
                                    {"lat": 59.9300, "lon": 30.3000},
                                    {"lat": 59.9300, "lon": 30.3010},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = root / "result"
            result = app.main(
                [
                    "--tracks", str(tracks),
                    "--osm-file", str(osm_path),
                    "--bbox", "59.92,30.29,59.94,30.31",
                    "--output", str(output),
                ]
            )
            self.assertEqual(result, 0)
            self.assertTrue((output / "map.html").is_file())
            self.assertTrue((output / "coverage.geojson").is_file())
            self.assertTrue((output / "streets_left.csv").is_file())
            with (output / "streets.csv").open(encoding="utf-8-sig") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["street"], "Example Street")
            self.assertEqual(rows[0]["status"], "complete")
            with (output / "streets_left.csv").open(encoding="utf-8-sig") as source:
                self.assertEqual(list(csv.DictReader(source)), [])


if __name__ == "__main__":
    unittest.main()
