import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from kbo_analysis import calibrate_probability
from scripts.train_calibrator import brier, fit_platt, sigmoid, main


class CalibrationTest(unittest.TestCase):
    def test_platt_fit_reduces_overconfidence(self):
        probabilities = [0.8] * 50 + [0.2] * 50
        outcomes = ([1.0] * 30 + [0.0] * 20) + ([1.0] * 20 + [0.0] * 30)
        slope, intercept = fit_platt(probabilities, outcomes)
        calibrated = []
        for probability in probabilities:
            import math
            calibrated.append(sigmoid(slope * math.log(probability / (1 - probability)) + intercept))
        self.assertLess(brier(calibrated, outcomes), brier(probabilities, outcomes))

    def test_runtime_calibration_is_bounded(self):
        calibrator = {"enabled": True, "kind": "platt", "baseModelVersion": "stats-v5-context-value", "slope": 3.0, "intercept": 0.0}
        self.assertEqual(calibrate_probability(0.99, calibrator), 0.8)
        self.assertEqual(calibrate_probability(0.01, calibrator), 0.2)

    def training_rows(self, validation):
        train = [{"home_probability": 0.5, "winner": "LG" if i % 2 else "두산", "home_team": "LG"}
                 for i in range(112)]
        return train + [{"home_probability": p, "winner": "LG" if y else "두산", "home_team": "LG"}
                        for p, y in validation]

    def test_validation_reports_the_probability_used_in_production(self):
        rows = self.training_rows([(0.72, 1)] * 28)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "calibration.json"
            with patch("scripts.train_calibrator.PredictionStore") as store, \
                 patch("scripts.train_calibrator.fit_platt", return_value=(0.0, 30.0)), \
                 patch("sys.argv", ["train_calibrator", "--output", str(output)]):
                store.return_value.evaluated_predictions.return_value = rows
                main()
            payload = json.loads(output.read_text())
        self.assertEqual(payload["calibratedBrier"], 0.04)

    def test_rejects_improvement_that_disappears_after_runtime_clipping(self):
        # 제한 없는 확률 ~1은 Brier 0.0714지만 실제 적용 확률 0.8은 0.0829다.
        # 기본 예측은 0.0784이므로 실서비스 기준으로 보정기를 거부해야 한다.
        rows = self.training_rows([(0.72, 1)] * 26 + [(0.28, 0)] * 2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "calibration.json"
            with patch("scripts.train_calibrator.PredictionStore") as store, \
                 patch("scripts.train_calibrator.fit_platt", return_value=(0.0, 30.0)), \
                 patch("sys.argv", ["train_calibrator", "--output", str(output)]):
                store.return_value.evaluated_predictions.return_value = rows
                with self.assertRaisesRegex(SystemExit, "검증 Brier가 개선되지 않았습니다"):
                    main()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
