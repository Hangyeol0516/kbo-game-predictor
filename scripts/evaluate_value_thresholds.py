#!/usr/bin/env python3
"""저장된 역배 후보로 EV·엣지·정배 대비 우위 임계값을 탐색한다."""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kbo_analysis import BASE_MODEL_VERSION  # noqa: E402
from storage import PredictionStore  # noqa: E402


def parse_grid(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="PLAYBALL 역배 임계값 사후 평가")
    parser.add_argument("--db", default="data/playball.db")
    parser.add_argument("--model-version", default=BASE_MODEL_VERSION)
    parser.add_argument("--ev", type=parse_grid, default=parse_grid("0.04,0.08,0.12,0.16"))
    parser.add_argument("--edge", type=parse_grid, default=parse_grid("0.02,0.05,0.08"))
    parser.add_argument("--advantage", type=parse_grid, default=parse_grid("0.05,0.10,0.15"))
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument("--minimum-bookmakers", type=int, default=2)
    parser.add_argument("--maximum-age-minutes", type=float, default=60)
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    rows = [
        row for row in PredictionStore(args.db).evaluated_value_candidates(args.model_version)
        if row["winner"] is not None
    ]
    results = []
    for minimum_ev, minimum_edge, minimum_advantage in itertools.product(args.ev, args.edge, args.advantage):
        selected = [
            row for row in rows
            if row["expected_return"] >= minimum_ev
            and row["model_probability"] - row["market_probability"] >= minimum_edge
            and row["return_advantage"] >= minimum_advantage
            and row["bookmaker_count"] >= args.minimum_bookmakers
            and row["market_age_minutes"] is not None
            and row["market_age_minutes"] <= args.maximum_age_minutes
            and bool(row["betting_open"])
        ]
        if len(selected) < args.minimum_samples:
            continue
        profit = sum(
            row["underdog_odds"] - 1 if row["winner"] == row["underdog_team"] else -1
            for row in selected
        )
        wins = sum(row["winner"] == row["underdog_team"] for row in selected)
        results.append((profit / len(selected), len(selected), wins, minimum_ev, minimum_edge, minimum_advantage))

    print(f"모델 버전: {args.model_version}")
    print(f"정산 가능한 전체 후보: {len(rows)}")
    if not results:
        print(f"최소 {args.minimum_samples}표본을 만족하는 임계값 조합이 없습니다.")
        return
    print("ROI      표본  적중  최소 EV  최소 엣지  최소 정배대비")
    for roi, samples, wins, minimum_ev, minimum_edge, minimum_advantage in sorted(results, reverse=True)[:args.limit]:
        print(
            f"{roi * 100:>6.1f}%  {samples:>4}  {wins:>4}  "
            f"{minimum_ev * 100:>7.1f}%  {minimum_edge * 100:>8.1f}%p  {minimum_advantage * 100:>11.1f}%p"
        )
    print("주의: 동일 데이터에서 고른 최적 임계값은 별도 미래 구간에서 다시 검증해야 합니다.")


if __name__ == "__main__":
    main()
