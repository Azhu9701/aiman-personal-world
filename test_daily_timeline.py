"""Only synthetic sources are used in these checks."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_config import atomic_json
from daily_timeline import CONFIG_NAME, snapshot
from server import ViewerServer
from world_model import WorldModel, date_bounds


DAY = "2026-11-25"
LOWER, UPPER = date_bounds(DAY, DAY)
PRIVATE = "SYNTHETIC_PRIVATE_MARKER"
MANIFEST = {"synthetic": True, "source": PRIVATE}
ROUTE = "/api/world/daily-timeline?date=" + DAY


class DailyTimelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.world = WorldModel(self.state)

    def archive_metadata(self):
        with closing(sqlite3.connect(self.state / "index.sqlite")) as db, db:
            db.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
            db.execute("INSERT INTO metadata VALUES ('manifest', ?)", (json.dumps(MANIFEST),))
            db.execute("CREATE TABLE messages (timestamp INTEGER)")
            db.executemany("INSERT INTO messages VALUES (?)", [
                (LOWER - 1,), (LOWER,), (LOWER + 3600,), (UPPER - 1,), (UPPER,),
            ])

    def configure(self, **config):
        atomic_json(self.state / CONFIG_NAME, config)

    def test_local_endpoint_remains_available_without_chat_and_rejects_other_origins(self):
        with ViewerServer(0, self.state, "synthetic-daily-session") as app:
            thread = threading.Thread(target=app.serve_forever, daemon=True)
            thread.start()
            def request(path, *, auth=True, method="GET", headers=None):
                h = {"Cookie": f"{app.cookie_name}={app.token}"} if auth else {}
                h.update(headers or {})
                with closing(http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=5)) as client:
                    client.request(method, path, headers=h)
                    response = client.getresponse()
                    return response.status, json.load(response), response.getheader("Cache-Control")

            try:
                self.assertEqual(request(ROUTE, auth=False)[0], 401)
                self.assertEqual(request(ROUTE, headers={"Origin": "http://localhost:1"})[0], 403)
                self.assertEqual(request(ROUTE, headers={"Sec-Fetch-Site": "same-site"})[0], 403)
                self.assertEqual(request(ROUTE, method="POST", headers={
                    "Origin": f"http://127.0.0.1:{app.server_port}",
                })[0], 405)
                for suffix in ("", "?date=2026-02-29", "?date=20261125", "?date=2026-11-25&date=2026-11-26",
                               "?date=2026-11-25&include=text"):
                    self.assertEqual(request("/api/world/daily-timeline" + suffix)[0], 400)

                with patch("daily_timeline.read_magpie") as magpie:
                    status, body, cache = request(ROUTE)
                    magpie.assert_not_called()
                self.assertEqual((status, cache), (200, "no-store"))
                self.assertEqual(body["sources"]["wechat"], {"status": "not_found", "observations": 0})
                self.assertEqual(body["sources"]["magpie"], {"status": "disabled", "observations": 0})
                self.assertEqual(body["hours"], [])
                self.assertEqual(list(self.state.iterdir()), [])
            finally:
                app.shutdown()
                thread.join()

    def test_joins_metadata_counts_with_interval_only_separate_from_hours(self):
        self.archive_metadata()
        screen = self.state / "screen.sqlite"
        with closing(sqlite3.connect(screen)) as db, db:
            db.execute("CREATE TABLE frames (ts REAL, app TEXT, blacklisted INTEGER)")
            db.executemany("INSERT INTO frames VALUES (?,?,?)", [
                (LOWER + 3600.25, PRIVATE, 0), (LOWER + 3601, PRIVATE, 1),
                (LOWER + 3602, None, 0), (UPPER, PRIVATE, 0),
            ])
        recorder = self.state / "recorder.json"
        recorder.write_text(json.dumps({"sessions": [{
            "id": PRIVATE, "createdAt": DAY + "T01:06:00+08:00",
            "durationMs": 1250, "title": PRIVATE, "transcript": PRIVATE,
        }]}))
        self.configure(magpie=True, magpie_limit=12, screen_db=str(screen), recorder_index=str(recorder))
        row = {"agent": "dsh", "id": PRIVATE, "start": "2026-11-24T15:00:00Z",
               "last": "2026-11-26T00:01:00+08:00", "title": PRIVATE, "path": PRIVATE}
        with patch("daily_timeline.read_magpie", return_value=[
            row, row, dict(row, id="SYNTHETIC_POINT", last=DAY+"T01:02:00+08:00"),
            dict(row, id="SYNTHETIC_CONFLICT"), dict(row, id="SYNTHETIC_CONFLICT", last=DAY+"T19:00:00+08:00"),
            dict(row, id="SYNTHETIC_BOUNDARY", last=DAY+"T00:00:00+08:00"), None,
        ]) as magpie:
            result = snapshot(self.state, self.world, DAY)
            magpie.assert_called_once_with(12)

        self.assertEqual(set(result), {"date", "timezone", "sources", "hours"})
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(result["sources"]["wechat"], {"status": "ok", "observations": 3, "synthetic": True})
        self.assertEqual(result["sources"]["magpie"], {
            "status": "partial", "observations": 2, "interval_only": 1, "sample_limit": 12,
            "sampled_rows": 7, "invalid_rows": 1, "conflicting_ids": 1,
        })
        self.assertEqual(result["hours"], [
            {"hour": 0, "counts": {"wechat": 1}},
            {"hour": 1, "counts": {"wechat": 1, "magpie": 1, "screen_memory": 1, "dsh_recorder": 1}},
            {"hour": 23, "counts": {"wechat": 1}},
        ])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn(str(self.state), json.dumps(result))

    def test_unusable_sources_are_distinguished_from_empty_sources(self):
        self.archive_metadata()
        self.configure(screen_db=str(self.state / "missing"), recorder_index=str(self.state / "invalid"))
        (self.state / "invalid").write_text(PRIVATE)
        result = snapshot(self.state, self.world, "2026-11-27")
        self.assertEqual(result["sources"]["wechat"], {"status": "ok", "observations": 0, "synthetic": True})
        self.assertEqual(result["sources"]["screen_memory"]["status"], "not_found")
        self.assertEqual(result["sources"]["dsh_recorder"]["status"], "read_failed")
        self.configure(screen_db=PRIVATE)
        result = snapshot(self.state, self.world, DAY)
        self.assertEqual(result["sources"]["screen_memory"]["status"], "config_error")
        self.assertEqual(result["sources"]["wechat"]["observations"], 3)
        self.assertNotIn(PRIVATE, json.dumps(result))
