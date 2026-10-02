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
            f"{self.base_url}/api/analysis?date=2026-09-29&refresh=1&mode=reanalysis",
            headers={"X-Refresh-Token": "test-token"},
        )
        with (
            patch.dict("server.os.environ", {"PLAYBALL_REFRESH_TOKEN": "test-token"}),
            patch("server.analyze", return_value=payload) as analyze,
            patch.object(server.STORE, "save_analysis"),
        ):
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response)["date"], "2026-09-29")
        analyze.assert_called_once_with("2026-09-29", force=True, refresh_odds=False)

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

    def test_odds_persistence_failure_is_visible_as_degraded_health(self):
        with patch("server.odds_provider_status", return_value={"configured": True, "lastError": None,
                                                               "persistenceError": "SaveFailed"}), \
             patch.dict(server.COLLECTOR_STATE, {"lastError": None}):
            with urlopen(f"{self.base_url}/health", timeout=2) as response:
                payload = json.load(response)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["odds"]["persistenceError"], "SaveFailed")

    def test_past_date_reads_saved_predictions_without_upstream_requests(self):
        payload = {"date": "2026-04-01", "viewMode": "historical", "games": [], "historyStatus": "missing"}
        with patch.object(server.STORE, "historical_analysis", return_value=payload) as history, patch("server.analyze") as analyze:
            with urlopen(f"{self.base_url}/api/analysis?date=2026-04-01", timeout=2) as response:
                self.assertEqual(json.load(response)["viewMode"], "historical")
        history.assert_called_once_with("2026-04-01")
        analyze.assert_not_called()

    def test_explicit_past_reanalysis_is_marked_and_never_saved_or_recommends_bets(self):
        payload = {"date": "2026-04-01", "snapshotEligible": True,
                   "games": [{"id": "fixture", "snapshotEligible": True, "valueBet": {"available": True, "recommendation": True}}]}
        with patch("server.analyze", return_value=payload) as analyze, patch.object(server.STORE, "save_analysis") as save:
            with urlopen(f"{self.base_url}/api/analysis?date=2026-04-01&mode=reanalysis", timeout=2) as response:
                result = json.load(response)
        self.assertEqual(result["viewMode"], "reanalysis")
        self.assertFalse(result["snapshotEligible"])
        self.assertFalse(result["games"][0]["valueBet"]["recommendation"])
        analyze.assert_called_once_with("2026-04-01", force=False, refresh_odds=False)
        save.assert_not_called()

    def test_performance_filters_are_validated_and_passed_to_store(self):
        from urllib.parse import urlencode
        params = {"start": "2026-04-01", "end": "2026-05-31", "team": "두산", "page": 2, "pageSize": 5, "modelVersion": "all"}
        with patch.object(server.STORE, "performance_summary", return_value={"recent": []}) as summary:
            with urlopen(f"{self.base_url}/api/performance?{urlencode(params)}", timeout=2):
                pass
        summary.assert_called_once_with(None, start="2026-04-01", end="2026-05-31", team="두산", page=2, page_size=5)
        for query in ("start=2026-02-30", "start=2026-05-01&end=2026-04-01", "team=unknown", "page=0", "pageSize=51"):
            with self.subTest(query=query), patch.object(server.STORE, "performance_summary") as summary:
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{self.base_url}/api/performance?{query}", timeout=2)
                self.assertEqual(error.exception.code, 400)
                summary.assert_not_called()

    def test_live_analysis_exposes_only_saved_event_history_and_collector_state(self):
        payload = {"date": "2099-05-01", "games": [{"id": "fixture"}], "snapshotEligible": False}
        events = {"fixture": [{"at": "2099-05-01T16:00:00+09:00", "homeProb": 45}]}
        with patch("server.analyze", return_value=payload), patch.object(server.STORE, "save_analysis") as save, \
             patch.object(server.STORE, "prediction_history", return_value=events) as history, \
             patch.dict(server.COLLECTOR_STATE, {"lastError": "results: OSError"}):
            with urlopen(f"{self.base_url}/api/analysis?date=2099-05-01", timeout=2) as response:
                result = json.load(response)
        history.assert_called_once_with("2099-05-01")
        self.assertEqual(result["games"][0]["predictionHistory"], events["fixture"])
        self.assertEqual(result["collectorStatus"]["lastError"], "results: OSError")
        self.assertTrue(save.called)

    def test_invalid_upstream_records_report_a_data_quality_error(self):
        with patch("server.analyze", side_effect=server.DataContractError("팀 기록: 컬럼 누락")):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}/api/analysis?date=2026-04-01&mode=reanalysis", timeout=2)
        self.assertEqual(error.exception.code, 502)
        self.assertEqual(json.load(error.exception)["dataQuality"]["status"], "invalid")


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
