#!/usr/bin/env python3
"""시점별 예측 데이터로 Platt 확률 보정기를 학습한다."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import PredictionStore  # noqa: E402


KST = timezone(timedelta(hours=9))


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(min(value, 30), -30)))


def brier(probabilities: list[float], outcomes: list[float]) -> float:
    return sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / len(outcomes)


def fit_platt(probabilities: list[float], outcomes: list[float]) -> tuple[float, float]:
    inputs = [math.log(min(max(p, 0.001), 0.999) / (1 - min(max(p, 0.001), 0.999))) for p in probabilities]
    slope, intercept = 1.0, 0.0
    for step in range(6000):
        predictions = [sigmoid(slope * value + intercept) for value in inputs]
        slope_gradient = sum((prediction - outcome) * value for prediction, outcome, value in zip(predictions, outcomes, inputs)) / len(inputs)
        intercept_gradient = sum(prediction - outcome for prediction, outcome in zip(predictions, outcomes)) / len(inputs)
        learning_rate = 0.04 / (1 + step / 2500)
        slope -= learning_rate * slope_gradient
        intercept -= learning_rate * intercept_gradient
    return slope, intercept


def main() -> None:
    parser = argparse.ArgumentParser(description="PLAYBALL Platt calibration 학습")
    parser.add_argument("--db", default="data/playball.db")
    parser.add_argument("--output", default="data/calibration.json")
    parser.add_argument("--minimum-games", type=int, default=100)
    args = parser.parse_args()
    rows = [row for row in PredictionStore(args.db).evaluated_predictions() if row["winner"] is not None]
    if len(rows) < args.minimum_games:
        raise SystemExit(f"학습 중단: 최소 {args.minimum_games}경기가 필요하지만 현재 {len(rows)}경기입니다.")
    split = max(1, int(len(rows) * 0.8))
    train, validation = rows[:split], rows[split:]
    if not validation:
        raise SystemExit("학습 중단: 검증 구간이 없습니다.")
    train_p = [row["home_probability"] for row in train]
    train_y = [float(row["winner"] == row["home_team"]) for row in train]
    if len(set(train_y)) < 2:
        raise SystemExit("학습 중단: 홈 승리와 패배 표본이 모두 필요합니다.")
    slope, intercept = fit_platt(train_p, train_y)
    validation_p = [row["home_probability"] for row in validation]
    validation_y = [float(row["winner"] == row["home_team"]) for row in validation]
    calibrated_p = [
        sigmoid(slope * math.log(min(max(p, 0.001), 0.999) / (1 - min(max(p, 0.001), 0.999))) + intercept)
        for p in validation_p
    ]
    baseline_brier = brier(validation_p, validation_y)
    calibrated_brier = brier(calibrated_p, validation_y)
    if calibrated_brier >= baseline_brier:
        raise SystemExit(f"활성화 중단: 검증 Brier가 개선되지 않았습니다 ({baseline_brier:.4f} → {calibrated_brier:.4f}).")
    payload = {
        "kind": "platt", "enabled": True, "slope": slope, "intercept": intercept,
        "trainedAt": datetime.now(KST).isoformat(timespec="seconds"), "samples": len(rows),
        "trainSamples": len(train), "validationSamples": len(validation),
        "baselineBrier": round(baseline_brier, 6), "calibratedBrier": round(calibrated_brier, 6),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"보정기 저장: {output} ({baseline_brier:.4f} → {calibrated_brier:.4f})")


if __name__ == "__main__":
    main()
