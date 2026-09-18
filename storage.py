"""PLAYBALL 예측 스냅샷과 경기 결과를 저장하는 SQLite 저장소.

단일 컨테이너 배포의 기본 저장소는 SQLite다. PostgreSQL 운영 전환을 위한
동일 구조의 DDL은 schema/postgresql.sql에 별도로 제공한다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_date TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (prediction_date, model_version, lineup_status)
);

CREATE TABLE IF NOT EXISTS game_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_date TEXT NOT NULL,
    game_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_probability REAL NOT NULL CHECK (away_probability BETWEEN 0 AND 1),
    home_probability REAL NOT NULL CHECK (home_probability BETWEEN 0 AND 1),
    predicted_winner TEXT NOT NULL,
    UNIQUE (prediction_date, game_id, model_version, lineup_status)
);

CREATE TABLE IF NOT EXISTS game_results (
    game_id TEXT PRIMARY KEY,
    game_date TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_score INTEGER NOT NULL,
    home_score INTEGER NOT NULL,
    winner TEXT,
    completed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_game_predictions_date ON game_predictions(prediction_date);
CREATE INDEX IF NOT EXISTS idx_game_results_date ON game_results(game_date);
"""
KST = timezone(timedelta(hours=9))


class PredictionStore:
    def __init__(self, db_path: str | None = None) -> None:
        configured = db_path or os.environ.get("PLAYBALL_DB_PATH", "data/playball.db")
        self.path = Path(configured).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def save_analysis(self, analysis: dict[str, Any]) -> None:
        if (
            not analysis.get("games")
            or not analysis.get("snapshotEligible")
            or analysis.get("date") != datetime.now(KST).date().isoformat()
        ):
            return
        created_at = analysis["updatedAt"]
        model_version = analysis["methodVersion"]
        lineup_status = analysis.get("lineupStatus", "projected")
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO analysis_snapshots
                   (prediction_date, model_version, lineup_status, created_at, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (analysis["date"], model_version, lineup_status, created_at, json.dumps(analysis, ensure_ascii=False)),
            )
            for game in analysis["games"]:
                connection.execute(
                    """INSERT OR IGNORE INTO game_predictions
                       (prediction_date, game_id, model_version, lineup_status, created_at,
                        away_team, home_team, away_probability, home_probability, predicted_winner)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        analysis["date"], game["id"], model_version, lineup_status, created_at,
                        game["away"], game["home"], game["awayProb"] / 100,
                        game["homeProb"] / 100, game["pick"],
                    ),
                )

    def pending_dates(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT p.prediction_date
                   FROM game_predictions p
                   LEFT JOIN game_results r ON r.game_id = p.game_id
                   WHERE r.game_id IS NULL AND p.prediction_date <= ?
                   ORDER BY p.prediction_date""",
                (datetime.now(KST).date().isoformat(),),
            ).fetchall()
        return [row[0] for row in rows]

    def save_completed_games(self, games: list[dict[str, Any]]) -> int:
        saved = 0
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            for game in games:
                if not bool(game.get("GAME_RESULT_CK")):
                    continue
                away_score = int(game.get("T_SCORE_CN") or 0)
                home_score = int(game.get("B_SCORE_CN") or 0)
                winner = None
                if away_score > home_score:
                    winner = game["AWAY_NM"]
                elif home_score > away_score:
                    winner = game["HOME_NM"]
                connection.execute(
                    """INSERT INTO game_results
                       (game_id, game_date, away_team, home_team, away_score, home_score, winner, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(game_id) DO UPDATE SET
                         away_score=excluded.away_score, home_score=excluded.home_score,
                         winner=excluded.winner, completed_at=excluded.completed_at""",
                    (
                        game["G_ID"], _iso_game_date(game["G_DT"]), game["AWAY_NM"], game["HOME_NM"],
                        away_score, home_score, winner, now,
                    ),
                )
                saved += 1
        return saved

    def sync_results(self, game_fetcher: Callable[[str], list[dict[str, Any]]]) -> int:
        saved = 0
        for prediction_date in self.pending_dates():
            saved += self.save_completed_games(game_fetcher(prediction_date))
        return saved

    def evaluated_predictions(self) -> list[dict[str, Any]]:
        query = """
        WITH ranked AS (
          SELECT p.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY p.game_id
                   ORDER BY CASE p.lineup_status WHEN 'confirmed' THEN 0 ELSE 1 END, p.created_at DESC
                 ) AS choice
          FROM game_predictions p
        )
        SELECT p.prediction_date, p.game_id, p.model_version, p.lineup_status,
               p.away_team, p.home_team, p.away_probability, p.home_probability,
               p.predicted_winner, r.away_score, r.home_score, r.winner
        FROM ranked p
        JOIN game_results r ON r.game_id = p.game_id
        WHERE p.choice = 1
        ORDER BY p.prediction_date, p.game_id
        """
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def performance_summary(self) -> dict[str, Any]:
        rows = self.evaluated_predictions()
        decided = [row for row in rows if row["winner"] is not None]
        correct = sum(row["predicted_winner"] == row["winner"] for row in decided)
        brier_values = []
        for row in rows:
            outcome = 0.5 if row["winner"] is None else float(row["winner"] == row["home_team"])
            brier_values.append((row["home_probability"] - outcome) ** 2)
        recent = []
        for row in reversed(rows[-10:]):
            recent.append({
                "date": row["prediction_date"], "away": row["away_team"], "home": row["home_team"],
                "score": f"{row['away_score']} : {row['home_score']}", "pick": row["predicted_winner"],
                "winner": row["winner"] or "무승부",
                "correct": None if row["winner"] is None else row["predicted_winner"] == row["winner"],
                "homeProbability": round(row["home_probability"] * 100),
                "lineupStatus": row["lineup_status"],
            })
        return {
            "evaluatedGames": len(rows), "decidedGames": len(decided), "correctGames": correct,
            "accuracy": round(correct / len(decided) * 100, 1) if decided else None,
            "brierScore": round(sum(brier_values) / len(brier_values), 4) if brier_values else None,
            "recent": recent,
            "message": None if rows else "저장된 예측의 경기가 종료되면 성능 지표가 표시됩니다.",
        }


def _iso_game_date(value: str) -> str:
    compact = value.replace("-", "")
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
