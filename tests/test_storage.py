import tempfile
import unittest
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
                "awayProb": 40, "homeProb": 60, "pick": "두산",
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
        self.assertEqual(summary["brierScore"], 0.16)
        self.assertEqual(summary["valueBet"]["settled"], 1)
        self.assertEqual(summary["valueBet"]["wins"], 0)
        self.assertEqual(summary["valueBet"]["roi"], -100.0)


if __name__ == "__main__":
    unittest.main()
