import csv
import json
import tempfile
import unittest
from pathlib import Path

import streets_left as app


class StreetsLeftTests(unittest.TestCase):
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

    def test_bbox_is_split_into_four_exact_tiles(self):
        tiles = app.split_bbox((0, 10, 2, 14))
        self.assertEqual(
            tiles,
            [(0, 10, 1, 12), (0, 12, 1, 14), (1, 10, 2, 12), (1, 12, 2, 14)],
        )

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
            ]
        }
        segments = list(app.osm_segments(osm, bbox))
        self.assertEqual(len(segments), 1)

        projection = app.Projection(59.95, 30.30)
        tracks = [[(59.93, 30.30), (59.93, 30.31)]]
        index = app.build_track_index(tracks, projection, 25, 1000)
        results, features = app.analyze(segments, projection, index, 12, 25)
        self.assertEqual(results[0].name, "Тестовая улица")
        self.assertAlmostEqual(results[0].completion, 1.0)
        self.assertEqual(features[0]["properties"]["status"], "walked")

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
            with (output / "streets.csv").open(encoding="utf-8-sig") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["street"], "Example Street")
            self.assertEqual(rows[0]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
