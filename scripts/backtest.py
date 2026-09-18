#!/usr/bin/env python3
"""저장된 시점별 예측을 날짜순으로 평가한다."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import PredictionStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="PLAYBALL 시간순 백테스트")
    parser.add_argument("--db", default="data/playball.db", help="SQLite 데이터베이스 경로")
    parser.add_argument("--export", help="평가 데이터 CSV 출력 경로")
    args = parser.parse_args()
    store = PredictionStore(args.db)
    rows = store.evaluated_predictions()
    summary = store.performance_summary()
    print(f"평가 경기: {summary['evaluatedGames']}")
    print(f"적중률: {summary['accuracy'] if summary['accuracy'] is not None else '-'}%")
    print(f"Brier score: {summary['brierScore'] if summary['brierScore'] is not None else '-'}")
    if args.export:
        output = Path(args.export)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8-sig") as stream:
            if rows:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
        print(f"CSV 저장: {output}")


if __name__ == "__main__":
    main()
