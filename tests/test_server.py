import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server
from server import AppHandler


class StaticServerSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_public_asset_has_security_headers(self):
        with urlopen(f"{self.base_url}/app.js", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_private_files_are_not_served(self):
        for path in ("/.env", "/server.py", "/.git/config", "/data/playball.db"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{self.base_url}{path}", timeout=2)
                self.assertEqual(error.exception.code, 404)

    def test_head_cannot_probe_private_files(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(f"{self.base_url}/.env", method="HEAD"), timeout=2)
        self.assertEqual(error.exception.code, 404)

    def test_force_refresh_requires_a_token(self):
        with patch.dict("server.os.environ", {}, clear=True), patch("server.analyze") as analyze:
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}/api/analysis?date=2026-09-29&refresh=1", timeout=2)
        self.assertEqual(error.exception.code, 403)
        analyze.assert_not_called()

    def test_force_refresh_accepts_the_configured_header(self):
        payload = {"date": "2026-09-29", "games": [], "snapshotEligible": False}
        request = Request(
            f"{self.base_url}/api/analysis?date=2026-09-29&refresh=1",
            headers={"X-Refresh-Token": "test-token"},
        )
        with (
            patch.dict("server.os.environ", {"PLAYBALL_REFRESH_TOKEN": "test-token"}),
            patch("server.analyze", return_value=payload) as analyze,
            patch.object(server.STORE, "save_analysis"),
        ):
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response)["date"], "2026-09-29")
        analyze.assert_called_once_with("2026-09-29", force=True, refresh_odds=True)

    def test_performance_read_does_not_synchronously_fetch_results(self):
        payload = {"evaluatedGames": 0, "recent": [], "valueBet": {}}
        with (
            patch.object(server.STORE, "performance_summary", return_value=payload),
            patch.object(server.STORE, "sync_results") as sync_results,
        ):
            with urlopen(f"{self.base_url}/api/performance", timeout=2) as response:
                self.assertEqual(json.load(response)["evaluatedGames"], 0)
        sync_results.assert_not_called()

    def test_database_failure_makes_healthcheck_unready(self):
        with patch.object(server.STORE, "connect", side_effect=OSError("unavailable")):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}/health", timeout=2)
        self.assertEqual(error.exception.code, 503)


class CollectorScheduleTest(unittest.TestCase):
    games = [{"G_TM": "18:30", "GAME_RESULT_CK": 0}]

    def at(self, hour, minute=0):
        return datetime(2026, 9, 29, hour, minute, tzinfo=server.KST)

    def test_skips_days_without_games_and_hours_before_morning(self):
        self.assertIsNone(server.collector_odds_slot([], self.at(10)))
        self.assertIsNone(server.collector_odds_slot(self.games, self.at(8, 59)))
        self.assertFalse(server.collector_analysis_due([], self.at(10)))
        self.assertFalse(server.collector_analysis_due(self.games, self.at(8, 59)))

    def test_uses_three_schedule_relative_odds_slots(self):
        self.assertEqual(server.collector_odds_slot(self.games, self.at(9)), "morning")
        self.assertEqual(server.collector_odds_slot(self.games, self.at(15, 30)), "pregame")
        self.assertEqual(server.collector_odds_slot(self.games, self.at(18)), "closing")

    def test_stops_analysis_and_odds_refresh_at_first_pitch(self):
        self.assertTrue(server.collector_analysis_due(self.games, self.at(18, 29)))
        self.assertFalse(server.collector_analysis_due(self.games, self.at(18, 30)))
        self.assertIsNone(server.collector_odds_slot(self.games, self.at(18, 30)))

    def test_uses_earliest_start_when_game_times_differ(self):
        games = self.games + [{"G_TM": "14:00", "GAME_RESULT_CK": 0}]
        self.assertEqual(server.collector_odds_slot(games, self.at(13, 30)), "closing")
        self.assertIsNone(server.collector_odds_slot(games, self.at(14)))

    def test_ignores_cancelled_and_completed_games(self):
        cancelled = [{"G_TM": "18:30", "GAME_RESULT_CK": 0, "GAME_STATE_SC": 4}]
        completed = [{"G_TM": "18:30", "GAME_RESULT_CK": 1, "GAME_STATE_SC": 1}]
        self.assertIsNone(server.collector_odds_slot(cancelled, self.at(10)))
        self.assertFalse(server.collector_analysis_due(completed, self.at(10)))


if __name__ == "__main__":
    unittest.main()
