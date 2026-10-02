#!/usr/bin/env python3
"""지정한 기간의 공식 결과를 재수집해 과거 결과를 정정한다."""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kbo_analysis import fetch_games
from storage import PredictionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("PLAYBALL_DB_PATH", "data/playball.db"))
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.start > args.end or (args.end - args.start).days > 366:
        raise SystemExit("시작일~종료일은 순서대로, 최대 367일로 지정하세요.")
    store, current, failures = PredictionStore(args.db), args.start, []
    while current <= args.end:
        day = current.isoformat()
        try:
            saved = store.save_completed_games(fetch_games(day))
            print(f"{day}: {saved}건 저장")
        except Exception as exc:
            failures.append(day)
            print(f"{day}: {type(exc).__name__} 실패", file=sys.stderr)
        current += timedelta(days=1)
    if failures:
        raise SystemExit("실패 날짜: " + ", ".join(failures))


if __name__ == "__main__":
    main()
