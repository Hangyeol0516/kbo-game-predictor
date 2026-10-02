import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from storage import PredictionStore


class PredictionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PredictionStore(str(Path(self.temp_dir.name) / "test.db"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_snapshot_is_idempotent_and_performance_is_scored(self):
        analysis = {
            "date": "2026-04-01", "updatedAt": "2026-04-01T12:00:00+09:00",
            "methodVersion": "stats-v4-matchup", "lineupStatus": "projected",
            "snapshotEligible": True,
            "games": [{
                "id": "20260401LGOB0", "away": "LG", "home": "두산",
                "awayProb": 40, "homeProb": 60,
                "awayProbability": 0.404321, "homeProbability": 0.595679, "pick": "두산",
                "valueBet": {
                    "available": True, "recommendation": True, "returnAdvantagePp": 15.0,
                    "favorite": {"team": "두산"},
                    "underdog": {
                        "team": "LG", "odds": 2.8, "modelProbability": 40.0,
                        "marketProbability": 34.0, "expectedReturnPct": 12.0,
                        "bookmaker": "Test Book",
                    },
                },
            }],
        }
        with patch("storage.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.date.return_value.isoformat.return_value = "2026-04-01"
            self.store.save_analysis(analysis)
            self.store.save_analysis(analysis)
        games = [{
            "G_ID": "20260401LGOB0", "G_DT": "20260401", "AWAY_NM": "LG", "HOME_NM": "두산",
            "T_SCORE_CN": "2", "B_SCORE_CN": "4", "GAME_RESULT_CK": 1,
        }]
        self.assertEqual(self.store.save_completed_games(games), 1)
        summary = self.store.performance_summary()
        self.assertEqual(summary["evaluatedGames"], 1)
        self.assertEqual(summary["accuracy"], 100.0)
        self.assertEqual(summary["brierScore"], 0.1635)
        self.assertEqual(summary["valueBet"]["settled"], 1)
        self.assertEqual(summary["valueBet"]["wins"], 0)
        self.assertEqual(summary["valueBet"]["roi"], -100.0)
        candidates = self.store.evaluated_value_candidates("stats-v4-matchup")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["underdog_team"], "LG")
        self.assertEqual(self.store.performance_summary("other")["evaluatedGames"], 0)
        self.assertEqual(self.store.value_bet_performance("other")["recommended"], 0)

    def test_snapshot_eligibility_and_lineup_status_are_per_game(self):
        analysis = {
            "date": "2026-04-01", "updatedAt": "2026-04-01T17:00:00+09:00",
            "methodVersion": "model-a", "lineupStatus": "projected", "snapshotEligible": True,
            "games": [
                {
                    "id": "eligible", "away": "LG", "home": "두산", "awayProb": 40, "homeProb": 60,
                    "awayProbability": 0.4, "homeProbability": 0.6, "pick": "두산",
                    "lineupConfirmed": True, "snapshotEligible": True, "valueBet": {"available": False},
                },
                {
                    "id": "finished", "away": "한화", "home": "삼성", "awayProb": 55, "homeProb": 45,
                    "awayProbability": 0.55, "homeProbability": 0.45, "pick": "한화",
                    "lineupConfirmed": False, "snapshotEligible": False, "valueBet": {"available": False},
                },
            ],
        }
        with patch("storage.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.date.return_value.isoformat.return_value = "2026-04-01"
            self.store.save_analysis(analysis)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT game_id, lineup_status FROM game_predictions ORDER BY game_id",
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("eligible", "confirmed")])

    def test_later_pregame_snapshot_replaces_earlier_market_price(self):
        analysis = {
            "date": "2026-04-01", "updatedAt": "2026-04-01T09:00:00+09:00",
            "methodVersion": "model-a", "lineupStatus": "projected", "snapshotEligible": True,
            "games": [{
                "id": "game-1", "away": "LG", "home": "두산", "awayProb": 45, "homeProb": 55,
                "pick": "두산", "snapshotEligible": True,
                "valueBet": {
                    "available": True, "recommendation": False, "returnAdvantagePp": 5,
                    "favorite": {"team": "두산"},
                    "underdog": {"team": "LG", "odds": 2.2, "modelProbability": 45,
                                 "marketProbability": 43, "expectedReturnPct": -1, "bookmaker": "A"},
                },
            }],
        }
        later = deepcopy(analysis)
        later["updatedAt"] = "2026-04-01T18:00:00+09:00"
        later["games"][0]["valueBet"]["underdog"]["odds"] = 2.6
        later["games"][0]["valueBet"]["underdog"]["expectedReturnPct"] = 17
        later["games"][0]["valueBet"]["recommendation"] = True
        with patch("storage.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.date.return_value.isoformat.return_value = "2026-04-01"
            self.store.save_analysis(analysis)
            self.store.save_analysis(later)
            # 이전 응답이 새 응답보다 늦게 저장되어도 최신 기록을 유지한다.
            self.store.save_analysis(analysis)
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT created_at, underdog_odds, recommended FROM value_bet_predictions",
            ).fetchone()
        self.assertEqual(tuple(row), ("2026-04-01T18:00:00+09:00", 2.6, 1))
        with self.store.connect() as connection:
            prediction = connection.execute("SELECT created_at FROM game_predictions").fetchone()
            snapshot = connection.execute("SELECT created_at FROM analysis_snapshots").fetchone()
        self.assertEqual(prediction[0], later["updatedAt"])
        self.assertEqual(snapshot[0], later["updatedAt"])

    def test_cached_snapshot_cannot_be_saved_at_or_after_first_pitch(self):
        now = datetime(2026, 4, 1, 18, tzinfo=timezone(timedelta(hours=9)))
        analysis = {
            "date": "2026-04-01", "updatedAt": "2026-04-01T17:59:00+09:00",
            "methodVersion": "model-a", "lineupStatus": "projected", "snapshotEligible": True,
            "games": [{"id": "game-1", "away": "LG", "home": "두산",
                       "awayProb": 40, "homeProb": 60, "pick": "두산", "snapshotEligible": True,
                       "startsAt": now.isoformat()}],
        }
        with patch("storage.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.store.save_analysis(analysis)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM game_predictions").fetchone()[0], 0)

    def test_only_games_that_have_not_started_are_saved(self):
        now = datetime(2026, 4, 1, 18, tzinfo=timezone(timedelta(hours=9)))
        base = {"away": "LG", "home": "두산", "awayProb": 40, "homeProb": 60,
                "pick": "두산", "snapshotEligible": True}
        analysis = {
            "date": "2026-04-01", "updatedAt": "2026-04-01T17:59:00+09:00",
            "methodVersion": "model-a", "lineupStatus": "projected", "snapshotEligible": True,
            "games": [
                {**base, "id": "started", "startsAt": now.isoformat()},
                {**base, "id": "later", "startsAt": (now + timedelta(minutes=30)).isoformat()},
                {**base, "id": "unknown", "startsAt": None},
            ],
        }
        with patch("storage.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.store.save_analysis(analysis)
        with self.store.connect() as connection:
            rows = connection.execute("SELECT game_id FROM game_predictions").fetchall()
        self.assertEqual([row[0] for row in rows], ["later"])

    def test_model_filter_is_applied_before_snapshot_ranking(self):
        with self.store.connect() as connection:
            for model, created_at, probability in (
                ("wanted", "2026-04-01T10:00:00+09:00", 0.61),
                ("other", "2026-04-01T11:00:00+09:00", 0.72),
            ):
                connection.execute(
                    """INSERT INTO game_predictions
                       (prediction_date, game_id, model_version, lineup_status, created_at,
                        away_team, home_team, away_probability, home_probability, predicted_winner)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("2026-04-01", "game-1", model, "projected", created_at,
                     "LG", "두산", 1 - probability, probability, "두산"),
                )
            connection.execute(
                """INSERT INTO game_results
                   (game_id, game_date, away_team, home_team, away_score, home_score, winner, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("game-1", "2026-04-01", "LG", "두산", 2, 4, "두산", "2026-04-01T22:00:00+09:00"),
            )
        rows = self.store.evaluated_predictions("wanted")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model_version"], "wanted")
        self.assertEqual(rows[0]["home_probability"], 0.61)

    def test_existing_database_gets_non_destructive_value_metadata_migration(self):
        path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE value_bet_predictions (
                   id INTEGER PRIMARY KEY, prediction_date TEXT, game_id TEXT,
                   model_version TEXT, lineup_status TEXT, created_at TEXT,
                   favorite_team TEXT, underdog_team TEXT, underdog_odds REAL,
                   model_probability REAL, market_probability REAL, expected_return REAL,
                   return_advantage REAL, bookmaker TEXT, recommended INTEGER)"""
            )
        migrated = PredictionStore(str(path))
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(value_bet_predictions)")}
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"bookmaker_count", "market_age_minutes", "betting_open"}.issubset(columns))
        self.assertEqual(version, 4)


if __name__ == "__main__":
    unittest.main()
