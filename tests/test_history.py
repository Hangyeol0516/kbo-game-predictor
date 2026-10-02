import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from storage import PredictionStore, SCHEMA


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "test.db")
        self.store = PredictionStore(self.path)

    def analysis(self):
        return {
            "date": "2026-04-01", "updatedAt": "2026-04-01T17:00:00+09:00",
            "methodVersion": "test", "lineupStatus": "confirmed", "snapshotEligible": True,
            "sources": ["KBO 공식 홈페이지"], "games": [
                {"id": game_id, "time": time, "away": "LG", "home": "두산", "awayProb": 40,
                 "homeProb": 60, "pick": "두산", "snapshotEligible": True, "lineupConfirmed": True,
                 "startsAt": f"2026-04-01T{time}:00+09:00", "reasons": [f"{game_id} 당시 근거"]}
                for game_id, time in (("first", "18:00"), ("second", "19:00"))
            ],
            "hittersByGame": {game_id: {"전체": [{"gameId": game_id, "name": "타자", "probability": 70}]}
                              for game_id in ("first", "second")},
        }

    def test_history_uses_each_games_own_pregame_payload(self):
        analysis = self.analysis()
        later = deepcopy(analysis)
        later["updatedAt"] = "2026-04-01T18:15:00+09:00"
        later["games"][0].update({"snapshotEligible": False, "homeProb": 99, "reasons": ["경기 시작 후 재분석"]})
        later["games"][1]["homeProb"] = 65
        with patch("storage.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(analysis["updatedAt"])
            self.store.save_analysis(analysis)
            clock.now.return_value = datetime.fromisoformat(later["updatedAt"])
            self.store.save_analysis(later)
        history = self.store.historical_analysis("2026-04-01")
        games = {game["id"]: game for game in history["games"]}
        self.assertEqual(games["first"]["homeProb"], 60)
        self.assertEqual(games["first"]["reasons"], ["first 당시 근거"])
        self.assertEqual(games["first"]["predictionAt"], analysis["updatedAt"])
        self.assertEqual(games["second"]["homeProb"], 65)
        self.assertEqual(games["second"]["predictionAt"], later["updatedAt"])
        self.assertFalse(history["snapshotEligible"])
        self.assertEqual({p["gameId"] for p in history["hitters"]["전체"]}, {"first", "second"})

    def test_missing_history_is_explicit_instead_of_reconstructed(self):
        history = self.store.historical_analysis("2026-04-01")
        self.assertEqual(history["historyStatus"], "missing")
        self.assertEqual(history["games"], [])
        self.assertIsNone(history["updatedAt"])

    def test_old_database_preserves_predictions_and_marks_missing_details(self):
        old_path = str(Path(self.directory.name) / "old.db")
        with sqlite3.connect(old_path) as connection:
            connection.executescript(SCHEMA.replace("    payload_json TEXT,\n", ""))
            connection.execute("""INSERT INTO game_predictions (
                prediction_date, game_id, model_version, lineup_status, created_at,
                away_team, home_team, away_probability, home_probability, predicted_winner)
                VALUES ('2026-04-01','old','test','projected','2026-04-01T17:00:00+09:00',
                        'LG','두산',0.4,0.6,'두산')""")
        upgraded = PredictionStore(old_path)
        history = upgraded.historical_analysis("2026-04-01")
        self.assertEqual(history["historyStatus"], "partial")
        self.assertEqual(history["games"][0]["homeProb"], 60)
        self.assertIn("상세 근거", history["games"][0]["reasons"][0])
        self.assertEqual(history["hitters"], {})
