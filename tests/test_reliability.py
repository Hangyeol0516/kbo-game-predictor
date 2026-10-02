import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import server
from kbo_analysis import fetch_pitcher_hand, fetch_hitter_stats, fetch_pitcher_stats
from storage import PredictionStore


class CollectorContinuityTest(unittest.TestCase):
    def test_first_game_start_cannot_reset_the_daily_odds_schedule(self):
        now = datetime(2026, 10, 2, 14, 15, tzinfo=server.KST)
        games = [{"G_TM": "14:00", "GAME_STATE_SC": "2", "GAME_RESULT_CK": False},
                 {"G_TM": "18:30", "GAME_STATE_SC": "1", "GAME_RESULT_CK": False}]
        self.assertIsNone(server.collector_odds_slot(games, now))
        self.assertTrue(server.collector_analysis_due(games, now))
        games[0].update(GAME_STATE_SC="3", GAME_RESULT_CK=True)
        self.assertIsNone(server.collector_odds_slot(games, now.replace(hour=17)))
        self.assertTrue(server.collector_analysis_due(games, now.replace(hour=17)))

    def test_result_sync_failure_is_visible_in_collector_state_with_date(self):
        def sync(_fetcher, on_error=None):
            on_error("2026-09-30", OSError("temporary"))
            return 1

        with patch("server.fetch_games", return_value=[]), \
             patch("server.collector_analysis_due", return_value=False), \
             patch.object(server.STORE, "sync_results", side_effect=sync), \
             patch("server.time.sleep", side_effect=KeyboardInterrupt):
            server.COLLECTOR_STATE["lastError"] = None
            with self.assertRaises(KeyboardInterrupt):
                server.background_collector()
        self.assertEqual(server.COLLECTOR_STATE["lastError"], "results 2026-09-30: OSError")


class StorageReliabilityTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "test.db")
        self.store = PredictionStore(self.path)
        self.clock = patch("storage.datetime", wraps=datetime).start()
        self.addCleanup(patch.stopall)
        self.clock.now.return_value = datetime.fromisoformat("2026-10-02T17:00:00+09:00")

    def analysis(self, hour="09", confirmed=False, recommended=True):
        return {"date": "2026-10-02", "updatedAt": f"2026-10-02T{hour}:00:00+09:00",
                "methodVersion": "test", "lineupStatus": "confirmed" if confirmed else "projected",
                "snapshotEligible": True, "games": [{
                    "id": "game", "away": "LG", "home": "두산", "awayProb": 45, "homeProb": 55,
                    "pick": "두산", "lineupConfirmed": confirmed, "startsAt": "2026-10-02T18:30:00+09:00",
                    "valueBet": {"available": True, "recommendation": recommended,
                        "returnAdvantagePp": 20, "favorite": {"team": "두산"}, "bookmakerCount": 2,
                        "quality": {"marketFresh": True, "bettingOpen": True, "enoughBookmakers": True},
                        "underdog": {"team": "LG", "odds": 2.8, "modelProbability": 45,
                                     "marketProbability": 35, "expectedReturnPct": 26, "bookmaker": "Test"}}}]}

    def result(self, away_score=2, home_score=4):
        self.store.save_completed_games([{"G_ID": "game", "G_DT": "20261002", "AWAY_NM": "LG",
            "HOME_NM": "두산", "T_SCORE_CN": away_score, "B_SCORE_CN": home_score, "GAME_RESULT_CK": True}])

    def test_claim_survives_restart_and_concurrent_workers_only_claim_once(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(lambda _: self.store.claim_odds_slot("2026-10-02", "morning"), range(8)))
        self.assertEqual(sum(claims), 1)
        reopened = PredictionStore(self.path)
        self.assertFalse(reopened.claim_odds_slot("2026-10-02", "morning"))
        self.assertTrue(reopened.claim_odds_slot("2026-10-02", "closing"))
        self.assertTrue(reopened.claim_odds_slot("2026-10-03", "morning"))

    def test_confirmed_no_bet_supersedes_a_projected_recommendation_in_roi(self):
        self.store.save_analysis(self.analysis())
        self.store.save_analysis(self.analysis("17", confirmed=True, recommended=False))
        self.result()
        self.assertEqual(self.store.value_bet_performance()["recommended"], 0)
        history = self.store.historical_analysis("2026-10-02")["games"][0]
        self.assertFalse(history["valueBet"]["recommendation"])
        self.assertEqual([entry["recommended"] for entry in history["predictionHistory"]], [True, False])

    def test_same_status_changes_are_auditable_and_duplicate_reads_do_not_add_events(self):
        self.store.save_analysis(self.analysis())
        later = self.analysis("17", recommended=False)
        self.store.save_analysis(later)
        self.store.save_analysis(later)
        self.store.save_analysis(self.analysis())
        self.result()
        history = self.store.historical_analysis("2026-10-02")["games"][0]
        self.assertEqual(len(history["predictionHistory"]), 2)
        self.assertEqual(history["homeProb"], 55)
        self.assertEqual(history["result"], {"awayScore": 2, "homeScore": 4, "winner": "두산", "correct": True})

    def test_missing_latest_market_invalidates_old_roi_but_preserves_audit_history(self):
        self.store.save_analysis(self.analysis())
        later = self.analysis("17", confirmed=True)
        later["games"][0]["valueBet"] = {"available": False, "recommendation": False}
        self.store.save_analysis(later)
        self.result()
        self.assertEqual(self.store.value_bet_performance()["recommended"], 0)
        self.assertEqual(self.store.evaluated_value_candidates(), [])
        self.assertEqual(len(self.store.historical_analysis("2026-10-02")["games"][0]["predictionHistory"]), 2)

    def test_projected_market_loss_does_not_override_a_valid_confirmed_snapshot(self):
        self.store.save_analysis(self.analysis("16", confirmed=True))
        later = self.analysis("17")
        later["games"][0]["valueBet"] = {"available": False}
        self.store.save_analysis(later)
        self.result()
        self.assertEqual(self.store.value_bet_performance()["recommended"], 1)
        self.assertTrue(self.store.historical_analysis("2026-10-02")["games"][0]["valueBet"]["recommendation"])

    def test_old_response_without_market_cannot_erase_a_new_recommendation(self):
        self.store.save_analysis(self.analysis("17"))
        older = self.analysis()
        older["games"][0]["valueBet"] = {"available": False}
        self.store.save_analysis(older)
        self.result()
        self.assertEqual(self.store.value_bet_performance()["recommended"], 1)

    def test_repeated_cached_reads_do_not_write_to_the_database(self):
        analysis = self.analysis()
        self.store.save_analysis(analysis)
        with self.store.connect() as observer:
            before = observer.execute("PRAGMA data_version").fetchone()[0]
            for _ in range(5):
                self.store.save_analysis(analysis)
            after = observer.execute("PRAGMA data_version").fetchone()[0]
        self.assertEqual(before, after)

    def test_connections_close_and_a_failed_save_rolls_back_all_tables(self):
        with self.store.connect() as connection:
            connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        invalid = self.analysis()
        invalid["games"][0]["homeProbability"] = 1.5
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_analysis(invalid)
        with self.store.connect() as connection:
            for table in ("analysis_snapshots", "game_predictions", "prediction_events"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_scorecard_reports_baseline_and_sample_uncertainty_and_ignores_draws(self):
        self.store.save_analysis(self.analysis())
        self.result()
        summary = self.store.performance_summary()
        self.assertEqual(summary["accuracy"], 100)
        self.assertEqual(summary["accuracyInterval95"], [20.7, 100.0])
        self.assertEqual(summary["baseline"]["homeWinAccuracy"], 100)
        self.assertEqual(summary["calibration"], [{"samples": 1, "predicted": 55.0, "observed": 100.0}])
        self.result(3, 3)
        summary = self.store.performance_summary()
        self.assertIsNone(summary["accuracyInterval95"])
        self.assertIsNone(summary["baseline"]["homeWinAccuracy"])
        self.assertEqual(summary["calibration"], [])

    def test_incomplete_or_invalid_final_scores_never_leave_a_settled_result(self):
        self.store.save_analysis(self.analysis())
        for invalid in (None, "", "-", -1, 2.5, True):
            with self.subTest(score=invalid), self.assertRaisesRegex(ValueError, "점수가 누락되거나 잘못"):
                self.store.save_completed_games([{"G_ID": "game", "G_DT": "20261002", "AWAY_NM": "LG",
                    "HOME_NM": "두산", "T_SCORE_CN": invalid, "B_SCORE_CN": 4, "GAME_RESULT_CK": True}])
            self.assertEqual(self.store.evaluated_predictions(), [])
            self.assertEqual(self.store.pending_dates(), ["2026-10-02"])
        self.result()
        self.assertEqual(self.store.evaluated_predictions()[0]["winner"], "두산")

    def test_recent_official_correction_updates_scores_and_performance(self):
        self.store.save_analysis(self.analysis())
        self.result(4, 2)
        self.assertEqual(self.store.performance_summary()["accuracy"], 0)
        self.assertEqual(self.store.pending_dates(), ["2026-10-02"])
        self.store.sync_results(lambda _: [{"G_ID": "game", "G_DT": "20261002", "AWAY_NM": "LG",
            "HOME_NM": "두산", "T_SCORE_CN": 2, "B_SCORE_CN": 4, "GAME_RESULT_CK": True}])
        self.assertEqual(self.store.performance_summary()["accuracy"], 100)
        with patch.dict("storage.os.environ", {"PLAYBALL_RESULT_CORRECTION_DAYS": "0"}):
            self.assertEqual(self.store.pending_dates(), [])

    def test_unchanged_corrected_results_do_not_write_again(self):
        self.result()
        with self.store.connect() as observer:
            before = observer.execute("PRAGMA data_version").fetchone()[0]
            self.result()
            self.assertEqual(observer.execute("PRAGMA data_version").fetchone()[0], before)

    def test_calibrator_training_family_is_filtered_before_snapshot_selection(self):
        from kbo_analysis import BASE_MODEL_VERSION
        older = self.analysis("16", confirmed=True)
        older["methodVersion"] = "stats-v6-context-value"
        self.store.save_analysis(older)
        current = self.analysis("17", confirmed=False)
        current["methodVersion"] = BASE_MODEL_VERSION + "+platt-new"
        self.store.save_analysis(current)
        self.result()
        rows = self.store.evaluated_predictions(BASE_MODEL_VERSION, include_payload=True, include_calibrated=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model_version"], current["methodVersion"])


class AmbiguousPlayerTest(unittest.TestCase):
    def test_same_team_same_name_search_results_do_not_choose_the_first_hand(self):
        candidates = [{"P_NM": "동명이인", "T_NM": "LG", "P_TYPE": hand} for hand in ("좌투좌타", "우투우타")]
        for order in (candidates, list(reversed(candidates))):
            with patch("kbo_analysis._post_json", return_value={"now": order}):
                self.assertIsNone(fetch_pitcher_hand({"T_PIT_P_NM": "동명이인", "AWAY_NM": "LG"}, "away")["split"])

    def test_ambiguous_player_tables_do_not_overwrite_one_players_stats_with_anothers(self):
        base = {"선수명": "동명이인", "팀명": "LG", "AVG": ".300", "G": "10", "PA": "40",
                "AB": "30", "H": "9", "HR": "1", "ERA": "3.00", "W": "1", "L": "0", "IP": "10", "WHIP": "1.00"}
        records = [base, {**base, "선수명": "* 동명이인", "ERA": "6.00"}, {**base, "선수명": "유일한 선수"}]
        with patch("kbo_analysis._fetch_team_filtered_records", return_value=records):
            self.assertEqual(set(fetch_hitter_stats(["LG"])), {("LG", "유일한 선수")})
            self.assertEqual(set(fetch_pitcher_stats(["LG"])), {("LG", "유일한 선수")})


class PredictionPrecisionTest(unittest.TestCase):
    def test_rounding_near_fifty_percent_does_not_change_the_predicted_winner(self):
        from kbo_analysis import _game_prediction
        game = {"G_ID": "near-tie", "G_TM": "18:30", "S_NM": "잠실", "AWAY_NM": "LG",
                "HOME_NM": "두산", "GAME_STATE_SC": "1", "GAME_RESULT_CK": False}
        stats = {team: {"runs_per_game": 4.5, "era": 4, "obp": .35, "whip": 1.3}
                 for team in ("LG", "두산")}
        bullpen = {team: {"fatigueScore": 50, "pitches": 50, "backToBack": 0} for team in stats}
        weather = {"parkRunFactor": 1, "weatherRunFactor": 1, "summary": "맑음"}
        for probability, expected in ((.496, "LG"), (.504, "두산"), (.5, "두산")):
            with self.subTest(probability=probability), patch("kbo_analysis.calibrate_probability", return_value=probability):
                prediction = _game_prediction(game, stats, {}, bullpen, {}, {}, weather, None, None, False)
            self.assertEqual(prediction["pick"], expected)
            self.assertEqual(prediction["homeProbability"], probability)
            self.assertEqual(prediction["homeProb"], round(probability * 100, 1))
            self.assertAlmostEqual(prediction["homeProb"] + prediction["awayProb"], 100)
