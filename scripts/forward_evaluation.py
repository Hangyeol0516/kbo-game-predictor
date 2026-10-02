#!/usr/bin/env python3
"""경기 전 저장 예측을 기간별로 평가하고 날짜 단위 불확실성을 보고한다."""

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifact_io import atomic_write_json
from kbo_analysis import active_model_version, load_calibrator
from storage import PredictionStore


def raw_probability(row: dict) -> float | None:
    try:
        if "+platt-" not in row["model_version"]:
            return row["home_probability"]
        payload = json.loads(row.get("payload_json") or "{}")
        probability = payload.get("game", {}).get("metrics", {}).get("rawHomeProbability")
        if probability is None and "+platt-" not in row["model_version"]:
            probability = row["home_probability"]
        if isinstance(probability, (int, float)) and not isinstance(probability, bool) and 0 <= probability <= 1:
            return float(probability)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def metrics(rows: list[dict], bets: list[dict]) -> dict:
    decided = [row for row in rows if row["winner"] is not None]
    brier, raw_brier = [], []
    for row in decided:
        outcome = float(row["winner"] == row["home_team"])
        current = (row["home_probability"] - outcome) ** 2
        brier.append(current)
        raw = raw_probability(row)
        if raw is not None:
            raw_brier.append(((raw - outcome) ** 2, current))
    settled = [row for row in bets if row["winner"] is not None]
    mean = lambda values: sum(values) / len(values) if values else None
    return {"games": len(decided), "dates": len({row["prediction_date"] for row in decided}),
            "accuracy": mean([float(row["predicted_winner"] == row["winner"]) for row in decided]),
            "homeBaselineAccuracy": mean([float(row["winner"] == row["home_team"]) for row in decided]),
            "brier": mean(brier), "coinFlipBaselineBrier": .25,
            "pairedRawGames": len(raw_brier), "rawBrier": mean([pair[0] for pair in raw_brier]),
            "brierImprovementVsRaw": mean([pair[0] - pair[1] for pair in raw_brier]),
            "settledBets": len(settled),
            "roi": mean([row["underdog_odds"] - 1 if row["winner"] == row["underdog_team"] else -1
                         for row in settled])}


def evaluate(rows: list[dict], candidates: list[dict], *, minimum_games=100, minimum_dates=20,
             resamples=1000, seed=20261002) -> dict:
    # 최종 선택 스냅샷의 실제 추천만 평가하고 사후 임계값 탐색 결과는 적용하지 않는다.
    bets = [row for row in candidates if row["recommended"] and row["market_quality_passed"] and row["betting_open"]]
    observed = metrics(rows, bets)
    warnings = []
    if observed["games"] < minimum_games or observed["dates"] < minimum_dates:
        warnings.append(f"표본 부족: 최소 {minimum_games}경기·{minimum_dates}개 경기일이 필요합니다.")
    if observed["settledBets"] < 30:
        warnings.append("역배 정산 30건 미만: ROI의 변동이 클 수 있습니다.")
    if observed["pairedRawGames"] < observed["games"]:
        warnings.append("일부 구버전 기록에 보정 전 확률이 없어 같은 경기의 보정 효과 비교에서 제외했습니다.")
    by_day, bets_by_day = defaultdict(list), defaultdict(list)
    for row in rows:
        if row["winner"] is not None:
            by_day[row["prediction_date"]].append(row)
    for row in bets:
        bets_by_day[row["prediction_date"]].append(row)
    dates = sorted(by_day)
    samples = defaultdict(list)
    if len(dates) >= 2:
        rng = random.Random(seed)
        for _ in range(resamples):
            sampled_days = rng.choices(dates, k=len(dates))
            measured = metrics([row for day in sampled_days for row in by_day[day]],
                               [row for day in sampled_days for row in bets_by_day[day]])
            for key in ("accuracy", "brier", "brierImprovementVsRaw", "roi"):
                if measured[key] is not None:
                    samples[key].append(measured[key])
    else:
        warnings.append("서로 다른 경기일이 2개 미만이라 날짜 단위 신뢰구간을 계산하지 않습니다.")
    intervals = {}
    for key in ("accuracy", "brier", "brierImprovementVsRaw", "roi"):
        ordered = sorted(samples[key])
        intervals[key] = [ordered[int((len(ordered) - 1) * p)] for p in (.025, .975)] if ordered else None
    periods = {}
    for month in sorted({row["prediction_date"][:7] for row in rows}):
        period = metrics([row for row in rows if row["prediction_date"].startswith(month)],
                         [row for row in bets if row["prediction_date"].startswith(month)])
        period["sufficientSample"] = period["games"] >= minimum_games and period["dates"] >= minimum_dates
        periods[month] = period
    return {"metrics": observed, "dateRange": [dates[0], dates[-1]] if dates else None,
            "sufficientSample": observed["games"] >= minimum_games and observed["dates"] >= minimum_dates,
            "interval95": intervals, "periods": periods, "warnings": warnings,
            "uncertaintyMethod": "date-cluster-bootstrap", "bootstrapResamples": resamples, "seed": seed,
            "roiPolicy": "최종 선택 스냅샷의 유효 추천당 1단위, 무승부 제외, 실제 체결·수수료·배당 변동 미반영",
            "limitations": "같은 날짜의 상관을 반영한 참고 구간입니다. 날짜 사이의 상관·자료 선택·모델 반복 조정의 영향은 제거하지 않습니다."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("PLAYBALL_DB_PATH", "data/playball.db"))
    parser.add_argument("--model-version", default=active_model_version())
    parser.add_argument("--after", type=date.fromisoformat, help="이 날짜 다음의 경기만 평가")
    parser.add_argument("--output", help="JSON 보고서 파일")
    parser.add_argument("--resamples", type=int, default=1000)
    args = parser.parse_args()
    if not 100 <= args.resamples <= 10000:
        raise SystemExit("반복 횟수는 100~10000으로 지정하세요.")
    calibrator = load_calibrator()
    try:
        ranges = (calibrator or {}).get("dateRanges", {})
        if not isinstance(ranges, dict):
            raise ValueError
        ends = []
        for value in ranges.values():
            if not isinstance(value, list) or len(value) != 2 or date.fromisoformat(value[0]) > date.fromisoformat(value[1]):
                raise ValueError
            ends.append(date.fromisoformat(value[1]).isoformat())
        used_until = max(ends, default=None)
    except (ValueError, TypeError):
        raise SystemExit("보정기의 학습·평가 기간 정보가 잘못됐습니다. 올바른 dateRanges를 확인하세요.") from None
    cutoff = max(filter(None, [args.after.isoformat() if args.after else None, used_until]), default=None)
    store = PredictionStore(args.db)
    rows = store.evaluated_predictions(args.model_version, include_payload=True)
    candidates = store.evaluated_value_candidates(args.model_version)
    if cutoff:
        rows = [row for row in rows if row["prediction_date"] > cutoff]
        candidates = [row for row in candidates if row["prediction_date"] > cutoff]
    report = evaluate(rows, candidates, resamples=args.resamples)
    report.update({"modelVersion": args.model_version, "after": cutoff,
                   "mode": "forward-window" if cutoff else "descriptive"})
    if cutoff is None:
        report["warnings"].append("미래 검증 기준일이 없습니다. 이 보고서는 회고적 요약이며 새로운 미래 검증으로 간주하지 않습니다.")
    if args.output:
        atomic_write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
