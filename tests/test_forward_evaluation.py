import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbo_analysis import BASE_MODEL_VERSION
from scripts.forward_evaluation import evaluate, main, raw_probability
from scripts.train_calibrator import main as train
from tests import test_calibration


class ForwardEvaluationTest(unittest.TestCase):
    def row(self, day="2026-10-02", winner="LG", calibrated=True):
        return {"prediction_date": day, "game_id": day, "home_team": "LG", "winner": winner,
                "predicted_winner": "LG", "home_probability": .6,
                "model_version": BASE_MODEL_VERSION + "+platt-test" if calibrated else BASE_MODEL_VERSION,
                "payload_json": json.dumps({"game": {"metrics": {"rawHomeProbability": .5}}})}

    def bet(self, day="2026-10-02", recommended=True):
        return {"prediction_date": day, "recommended": recommended, "market_quality_passed": 1,
                "betting_open": 1, "underdog_team": "LG", "winner": "LG", "underdog_odds": 2.5}

    def test_paired_brier_and_roi_use_saved_final_decisions_and_ignore_draws(self):
        rows = [self.row(), self.row("2026-10-03"), self.row("2026-10-04", None)]
        bets = [self.bet(), self.bet("2026-10-03", False), {**self.bet("2026-10-04"), "winner": None}]
        result = evaluate(rows, bets, resamples=100)
        self.assertAlmostEqual(result["metrics"]["brierImprovementVsRaw"], .09)
        self.assertEqual(result["metrics"]["games"], 2)
        self.assertEqual(result["metrics"]["settledBets"], 1)
        self.assertEqual(result["metrics"]["roi"], 1.5)
        self.assertFalse(result["sufficientSample"])
        self.assertTrue(result["warnings"])

    def test_date_cluster_intervals_are_deterministic_and_single_day_has_none(self):
        single = evaluate([self.row()] * 100, [], resamples=100)
        self.assertEqual(single["metrics"]["dates"], 1)
        self.assertIsNone(single["interval95"]["accuracy"])
        rows = [self.row("2026-10-02")] * 5 + [self.row("2026-10-03", "두산")] * 5
        result = evaluate(rows, [], resamples=100, seed=1)
        self.assertEqual(result, evaluate(rows, [], resamples=100, seed=1))
        self.assertEqual(result["interval95"]["accuracy"], [0.0, 1.0])

    def test_forward_report_excludes_all_periods_used_by_the_calibrator(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            with patch("scripts.forward_evaluation.PredictionStore") as store, \
                 patch("scripts.forward_evaluation.load_calibrator", return_value={
                     "dateRanges": {"train": ["2026-09-01", "2026-09-20"], "test": ["2026-09-21", "2026-10-02"]}}), \
                 patch("sys.argv", ["forward_evaluation", "--after", "2026-09-01", "--output", str(output), "--resamples", "100"]):
                store.return_value.evaluated_predictions.return_value = [self.row(), self.row("2026-10-03")]
                store.return_value.evaluated_value_candidates.return_value = []
                main()
            result = json.loads(output.read_text())
            self.assertEqual(result["after"], "2026-10-02")
            self.assertEqual(result["metrics"]["games"], 1)
            self.assertEqual(result["dateRange"], ["2026-10-03", "2026-10-03"])

    def test_missing_raw_probability_is_excluded_instead_of_using_calibrated_probability(self):
        row = self.row()
        row["payload_json"] = None
        self.assertIsNone(raw_probability(row))
        row["model_version"] = BASE_MODEL_VERSION
        self.assertEqual(raw_probability(row), .6)

    def test_retraining_uses_raw_probabilities_from_new_calibrated_snapshots(self):
        rows = test_calibration.CalibrationTest().training_rows([(.72, 1)] * 28)
        for row in rows:
            raw = row["home_probability"]
            row["model_version"] = BASE_MODEL_VERSION + "+platt-test"
            row["home_probability"] = .8
            row["payload_json"] = json.dumps({"game": {"metrics": {"rawHomeProbability": raw}}})
        with tempfile.TemporaryDirectory() as folder:
            with patch("scripts.train_calibrator.PredictionStore") as store, \
                 patch("scripts.train_calibrator.fit_platt", return_value=(0., 30.)) as fit, \
                 patch("sys.argv", ["train_calibrator", "--output", str(Path(folder) / "calibration.json")]):
                store.return_value.evaluated_predictions.return_value = rows
                train()
            self.assertEqual(set(fit.call_args.args[0]), {.5})
