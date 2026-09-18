import unittest

from kbo_analysis import calibrate_probability
from scripts.train_calibrator import brier, fit_platt, sigmoid


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
        calibrator = {"enabled": True, "kind": "platt", "slope": 3.0, "intercept": 0.0}
        self.assertEqual(calibrate_probability(0.99, calibrator), 0.8)
        self.assertEqual(calibrate_probability(0.01, calibrator), 0.2)


if __name__ == "__main__":
    unittest.main()
