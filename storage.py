"""PLAYBALL 예측 스냅샷과 경기 결과를 저장하는 SQLite 저장소.

단일 컨테이너 배포의 기본 저장소는 SQLite다. PostgreSQL 운영 전환을 위한
동일 구조의 DDL은 schema/postgresql.sql에 별도로 제공한다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from collections.abc import Iterator


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
    payload_json TEXT,
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

CREATE TABLE IF NOT EXISTS value_bet_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_date TEXT NOT NULL,
    game_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    favorite_team TEXT NOT NULL,
    underdog_team TEXT NOT NULL,
    underdog_odds REAL NOT NULL CHECK (underdog_odds > 1),
    model_probability REAL NOT NULL CHECK (model_probability BETWEEN 0 AND 1),
    market_probability REAL NOT NULL CHECK (market_probability BETWEEN 0 AND 1),
    expected_return REAL NOT NULL,
    return_advantage REAL NOT NULL,
    bookmaker TEXT NOT NULL,
    bookmaker_count INTEGER NOT NULL DEFAULT 1,
    market_age_minutes REAL,
    market_quality_passed INTEGER NOT NULL DEFAULT 1 CHECK (market_quality_passed IN (0, 1)),
    betting_open INTEGER NOT NULL DEFAULT 1 CHECK (betting_open IN (0, 1)),
    market_updated_at TEXT,
    commence_at TEXT,
    recommended INTEGER NOT NULL CHECK (recommended IN (0, 1)),
    UNIQUE (prediction_date, game_id, model_version, lineup_status)
);

CREATE INDEX IF NOT EXISTS idx_game_predictions_date ON game_predictions(prediction_date);
CREATE INDEX IF NOT EXISTS idx_game_results_date ON game_results(game_date);
CREATE INDEX IF NOT EXISTS idx_value_bets_date ON value_bet_predictions(prediction_date);

CREATE TABLE IF NOT EXISTS collector_odds_slots (
    prediction_date TEXT NOT NULL,
    slot TEXT NOT NULL CHECK (slot IN ('morning', 'pregame', 'closing')),
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (prediction_date, slot)
);

CREATE TABLE IF NOT EXISTS prediction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_date TEXT NOT NULL,
    game_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (prediction_date, game_id, model_version, lineup_status, created_at)
);
CREATE INDEX IF NOT EXISTS idx_prediction_events_game ON prediction_events(prediction_date, game_id, created_at);
"""
KST = timezone(timedelta(hours=9))


class ResultSyncError(RuntimeError):
    """날짜별 결과 동기화 후 일부 날짜가 실패했음을 알린다."""

    def __init__(self, saved: int, failed_dates: list[str]) -> None:
        self.saved = saved
        self.failed_dates = failed_dates
        super().__init__(f"결과 {saved}건 저장, 실패 날짜: {', '.join(failed_dates)}")


class PredictionStore:
    def __init__(self, db_path: str | None = None) -> None:
        configured = db_path or os.environ.get("PLAYBALL_DB_PATH", "data/playball.db")
        self.path = Path(configured).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """기존 볼륨에 새 메타데이터 열을 비파괴 방식으로 추가한다."""
        columns = {row[1] for row in connection.execute("PRAGMA table_info(value_bet_predictions)")}
        additions = {
            "bookmaker_count": "INTEGER NOT NULL DEFAULT 1",
            "market_age_minutes": "REAL",
            "market_quality_passed": "INTEGER NOT NULL DEFAULT 1",
            "betting_open": "INTEGER NOT NULL DEFAULT 1",
            "market_updated_at": "TEXT",
            "commence_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE value_bet_predictions ADD COLUMN {name} {definition}")
        prediction_columns = {row[1] for row in connection.execute("PRAGMA table_info(game_predictions)")}
        if "payload_json" not in prediction_columns:
            connection.execute("ALTER TABLE game_predictions ADD COLUMN payload_json TEXT")
        connection.execute("PRAGMA user_version=4")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def claim_odds_slot(self, prediction_date: str, slot: str) -> bool:
        """호출 전에 슬롯을 영구 예약해 재시작·실패·동시 수집의 중복 비용을 막는다."""
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO collector_odds_slots VALUES (?, ?, ?)",
                (prediction_date, slot, datetime.now(KST).isoformat(timespec="seconds")),
            )
            return cursor.rowcount == 1

    def save_analysis(self, analysis: dict[str, Any]) -> None:
        now = datetime.now(KST)
        if (
            not analysis.get("games")
            or analysis.get("viewMode") in ("historical", "reanalysis")
            or analysis.get("date") != now.date().isoformat()
        ):
            return
        eligible_games = [
            game for game in analysis["games"]
            if game.get("snapshotEligible", analysis.get("snapshotEligible", False))
            and self._before_start(game, analysis["updatedAt"], now)
        ]
        if not eligible_games:
            return
        created_at = analysis["updatedAt"]
        model_version = analysis["methodVersion"]
        snapshot_lineup_status = "confirmed" if all(
            game.get("lineupConfirmed", analysis.get("lineupStatus") == "confirmed")
            for game in eligible_games
        ) else "projected"
        with self.connect() as connection:
            existing_events = {
                (row["game_id"], row["lineup_status"])
                for row in connection.execute(
                    """SELECT game_id, lineup_status FROM prediction_events
                       WHERE prediction_date=? AND model_version=? AND created_at=?""",
                    (analysis["date"], model_version, created_at),
                )
            }
            if all((game["id"], "confirmed" if game.get(
                    "lineupConfirmed", analysis.get("lineupStatus") == "confirmed",
                ) else "projected") in existing_events for game in eligible_games):
                return  # 같은 캐시 응답은 읽기로 종료해 WAL·자동 증가 ID 쓰기도 피한다.
            connection.execute(
                """INSERT INTO analysis_snapshots
                   (prediction_date, model_version, lineup_status, created_at, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(prediction_date, model_version, lineup_status) DO UPDATE SET
                     created_at=excluded.created_at, payload_json=excluded.payload_json
                   WHERE excluded.created_at > analysis_snapshots.created_at""",
                (analysis["date"], model_version, snapshot_lineup_status, created_at, json.dumps(analysis, ensure_ascii=False)),
            )
            for game in eligible_games:
                lineup_status = "confirmed" if game.get(
                    "lineupConfirmed", analysis.get("lineupStatus") == "confirmed",
                ) else "projected"
                connection.execute(
                    """INSERT OR IGNORE INTO prediction_events
                       (prediction_date, game_id, model_version, lineup_status, created_at, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (analysis["date"], game["id"], model_version, lineup_status, created_at,
                     json.dumps(game, ensure_ascii=False)),
                )
                connection.execute(
                    """INSERT INTO game_predictions
                       (prediction_date, game_id, model_version, lineup_status, created_at,
                        away_team, home_team, away_probability, home_probability, predicted_winner, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(prediction_date, game_id, model_version, lineup_status) DO UPDATE SET
                         created_at=excluded.created_at,
                         away_probability=excluded.away_probability,
                         home_probability=excluded.home_probability,
                         predicted_winner=excluded.predicted_winner, payload_json=excluded.payload_json
                       WHERE excluded.created_at > game_predictions.created_at""",
                    (
                        analysis["date"], game["id"], model_version, lineup_status, created_at,
                        game["away"], game["home"],
                        game.get("awayProbability", game["awayProb"] / 100),
                        game.get("homeProbability", game["homeProb"] / 100), game["pick"],
                        json.dumps({
                            "game": game,
                            "hitters": analysis.get("hittersByGame", {}).get(game["id"], {
                                position: [player for player in players if player.get("gameId") == game["id"]]
                                for position, players in analysis.get("hitters", {}).items()
                            }),
                            "sources": analysis.get("sources", [analysis.get("source", "KBO 공식 홈페이지")]),
                            "dataQuality": analysis.get("dataQuality", {}),
                        }, ensure_ascii=False),
                    ),
                )
                value = game.get("valueBet") or {}
                if value.get("available"):
                    underdog = value["underdog"]
                    quality = value.get("quality") or {}
                    quality_passed = all((
                        quality.get("marketFresh", True), quality.get("bettingOpen", True),
                        quality.get("enoughBookmakers", True),
                    ))
                    model_probability = (
                        game.get("homeProbability", game["homeProb"] / 100)
                        if underdog["team"] == game["home"]
                        else game.get("awayProbability", game["awayProb"] / 100)
                    )
                    connection.execute(
                        """INSERT INTO value_bet_predictions
                           (prediction_date, game_id, model_version, lineup_status, created_at,
                            favorite_team, underdog_team, underdog_odds, model_probability,
                            market_probability, expected_return, return_advantage, bookmaker,
                            bookmaker_count, market_age_minutes, market_quality_passed,
                            betting_open, market_updated_at, commence_at, recommended)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(prediction_date, game_id, model_version, lineup_status) DO UPDATE SET
                             created_at=excluded.created_at,
                             favorite_team=excluded.favorite_team,
                             underdog_team=excluded.underdog_team,
                             underdog_odds=excluded.underdog_odds,
                             model_probability=excluded.model_probability,
                             market_probability=excluded.market_probability,
                             expected_return=excluded.expected_return,
                             return_advantage=excluded.return_advantage,
                             bookmaker=excluded.bookmaker,
                             bookmaker_count=excluded.bookmaker_count,
                             market_age_minutes=excluded.market_age_minutes,
                             market_quality_passed=excluded.market_quality_passed,
                             betting_open=excluded.betting_open,
                             market_updated_at=excluded.market_updated_at,
                             commence_at=excluded.commence_at,
                             recommended=excluded.recommended
                           WHERE excluded.created_at > value_bet_predictions.created_at""",
                        (
                            analysis["date"], game["id"], model_version, lineup_status, created_at,
                            value["favorite"]["team"], underdog["team"], underdog["odds"],
                            underdog.get("modelProbabilityValue", model_probability),
                            underdog.get("marketProbabilityValue", underdog["marketProbability"] / 100),
                            underdog.get("expectedReturnValue", underdog["expectedReturnPct"] / 100),
                            value.get("returnAdvantageValue", value["returnAdvantagePp"] / 100),
                            underdog["bookmaker"], int(value.get("bookmakerCount", 0)),
                            quality.get("ageMinutes"), int(quality_passed),
                            int(quality.get("bettingOpen", True)),
                            value.get("lastUpdate"), value.get("commenceTime"), int(value["recommendation"]),
                        ),
                    )

    @staticmethod
    def _before_start(game: dict[str, Any], created_at: str, now: datetime) -> bool:
        # 구버전 스냅샷은 startsAt 없이 저장되므로 기존 데이터 계약을 유지한다.
        if "startsAt" not in game:
            return True
        try:
            starts_at = datetime.fromisoformat(game["startsAt"])
            generated_at = datetime.fromisoformat(created_at)
            return (
                starts_at.tzinfo is not None and generated_at.tzinfo is not None
                and generated_at < starts_at and now < starts_at
            )
        except (TypeError, ValueError):
            return False

    def historical_analysis(self, prediction_date: str) -> dict[str, Any]:
        """경기마다 당시 저장한 예측을 조회한다. 현재 시즌 기록으로 복원하지 않는다."""
        with self.connect() as connection:
            rows = connection.execute(
                """WITH ranked AS (
                     SELECT p.*, ROW_NUMBER() OVER (
                       PARTITION BY game_id
                       ORDER BY CASE lineup_status WHEN 'confirmed' THEN 0 ELSE 1 END,
                                created_at DESC, id DESC
                     ) AS choice
                     FROM game_predictions p WHERE prediction_date = ?
                   ) SELECT p.*, r.away_score, r.home_score, r.winner
                     FROM ranked p LEFT JOIN game_results r ON r.game_id=p.game_id
                     WHERE choice = 1 ORDER BY p.game_id""", (prediction_date,),
            ).fetchall()
            events = connection.execute(
                """SELECT game_id, created_at, lineup_status, model_version, payload_json
                   FROM prediction_events WHERE prediction_date=? ORDER BY created_at, id""",
                (prediction_date,),
            ).fetchall()
        timeline = {}
        for event in events:
            payload = json.loads(event["payload_json"])
            value = payload.get("valueBet") or {}
            timeline.setdefault(event["game_id"], []).append({
                "at": event["created_at"], "lineupStatus": event["lineup_status"],
                "modelVersion": event["model_version"], "homeProb": payload.get("homeProb"),
                "recommended": bool(value.get("recommendation")),
                "underdog": (value.get("underdog") or {}).get("team"),
                "odds": (value.get("underdog") or {}).get("odds"),
            })
        games, hitters, sources, warnings = [], {}, set(), []
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            game = payload.get("game") or {
                "id": row["game_id"], "away": row["away_team"], "home": row["home_team"],
                "time": "—", "park": "기록 없음", "awayPitcher": "기록 없음", "homePitcher": "기록 없음",
                "awayProb": round(row["away_probability"] * 100),
                "homeProb": round(row["home_probability"] * 100), "pick": row["predicted_winner"],
                "confidence": "저장된 승률", "reasons": ["이전 저장 형식에는 상세 근거가 없습니다."],
                "valueBet": {"available": False, "recommendation": False},
            }
            game.update({"snapshotEligible": False, "predictionAt": row["created_at"],
                         "modelVersion": row["model_version"], "lineupConfirmed": row["lineup_status"] == "confirmed"})
            game["predictionHistory"] = timeline.get(row["game_id"], [])
            if row["away_score"] is not None:
                game["result"] = {"awayScore": row["away_score"], "homeScore": row["home_score"],
                                  "winner": row["winner"], "correct": None if row["winner"] is None
                                  else row["predicted_winner"] == row["winner"]}
            games.append(game)
            for position, players in payload.get("hitters", {}).items():
                hitters.setdefault(position, []).extend(players)
            sources.update(payload.get("sources", ["KBO 공식 홈페이지"]))
            warnings.extend(payload.get("dataQuality", {}).get("warnings", []))
        for position, players in hitters.items():
            hitters[position] = sorted(players, key=lambda player: player["probability"], reverse=True)[:3]
        return {
            "date": prediction_date, "viewMode": "historical",
            "historyStatus": "partial" if any(not row["payload_json"] for row in rows) else "available" if rows else "missing",
            "games": games, "hitters": hitters, "snapshotEligible": False,
            "hittersByGame": {row["game_id"]: (json.loads(row["payload_json"]).get("hitters", {})
                              if row["payload_json"] else {}) for row in rows},
            "updatedAt": max((row["created_at"] for row in rows), default=None),
            "source": "저장된 당시 예측", "sources": sorted(sources) or ["저장된 당시 예측"],
            "methodVersion": ", ".join(sorted({row["model_version"] for row in rows})) or "—",
            "lineupStatus": "confirmed" if rows and all(row["lineup_status"] == "confirmed" for row in rows) else "projected",
            "valueBetStatus": "historical", "valueBets": [
                game["valueBet"] | {"gameId": game["id"], "away": game["away"], "home": game["home"], "historical": True}
                for game in games if (game.get("valueBet") or {}).get("recommendation")
            ],
            "dataQuality": {"warnings": list(dict.fromkeys(warnings))},
            "message": None if rows else "이 날짜에 저장된 경기 전 예측이 없습니다. 현재 기록을 사용한 재분석은 별도로 선택할 수 있습니다.",
        }

    def pending_dates(self) -> list[str]:
        today = datetime.now(KST).date()
        try:
            lookback_days = max(int(os.environ.get("PLAYBALL_RESULT_LOOKBACK_DAYS", "30")), 1)
        except ValueError:
            lookback_days = 30
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT p.prediction_date
                   FROM game_predictions p
                   LEFT JOIN game_results r ON r.game_id = p.game_id
                   WHERE r.game_id IS NULL AND p.prediction_date BETWEEN ? AND ?
                   ORDER BY p.prediction_date""",
                ((today - timedelta(days=lookback_days)).isoformat(), today.isoformat()),
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

    def sync_results(
        self,
        game_fetcher: Callable[[str], list[dict[str, Any]]],
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> int:
        """각 날짜를 독립적으로 동기화하고, 실패는 선택 콜백으로 알린다."""
        saved = 0
        failed_dates = []
        for prediction_date in self.pending_dates():
            try:
                saved += self.save_completed_games(game_fetcher(prediction_date))
            except Exception as exc:
                failed_dates.append(prediction_date)
                if on_error is not None:
                    on_error(prediction_date, exc)
        if failed_dates and on_error is None:
            raise ResultSyncError(saved, failed_dates)
        return saved

    def evaluated_predictions(self, model_version: str | None = None) -> list[dict[str, Any]]:
        query = """
        WITH ranked AS (
          SELECT p.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY p.game_id
                   ORDER BY CASE p.lineup_status WHEN 'confirmed' THEN 0 ELSE 1 END, p.created_at DESC
                 ) AS choice
          FROM game_predictions p
          WHERE (? IS NULL OR p.model_version = ?)
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
            rows = connection.execute(query, (model_version, model_version)).fetchall()
        return [dict(row) for row in rows]

    def performance_summary(self, model_version: str | None = None) -> dict[str, Any]:
        rows = self.evaluated_predictions(model_version)
        decided = [row for row in rows if row["winner"] is not None]
        correct = sum(row["predicted_winner"] == row["winner"] for row in decided)
        brier_values = [
            (row["home_probability"] - float(row["winner"] == row["home_team"])) ** 2
            for row in decided
        ]
        count = len(decided)
        interval = None
        if count:
            # Wilson 구간은 작은 표본에서 단순 정규 근사보다 안정적이다.
            z = 1.959963984540054
            rate = correct / count
            denominator = 1 + z * z / count
            center = (rate + z * z / (2 * count)) / denominator
            radius = z * (rate * (1 - rate) / count + z * z / (4 * count * count)) ** 0.5 / denominator
            interval = [round((center - radius) * 100, 1), round((center + radius) * 100, 1)]
        calibration = []
        for low, high in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
            bucket = [row for row in decided if low <= row["home_probability"] < high]
            if bucket:
                calibration.append({"samples": len(bucket),
                    "predicted": round(sum(row["home_probability"] for row in bucket) / len(bucket) * 100, 1),
                    "observed": round(sum(row["winner"] == row["home_team"] for row in bucket) / len(bucket) * 100, 1)})
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
            "accuracyInterval95": interval, "calibration": calibration,
            "baseline": {"coinFlipBrier": .25,
                         "homeWinAccuracy": round(sum(row["winner"] == row["home_team"] for row in decided) / count * 100, 1) if count else None},
            "recent": recent,
            "modelVersion": model_version or "all",
            "valueBet": self.value_bet_performance(model_version),
            "message": None if rows else "저장된 예측의 경기가 종료되면 성능 지표가 표시됩니다.",
        }

    def value_bet_performance(self, model_version: str | None = None) -> dict[str, Any]:
        query = """
        WITH ranked AS (
          SELECT p.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY p.game_id
                   ORDER BY CASE p.lineup_status WHEN 'confirmed' THEN 0 ELSE 1 END, p.created_at DESC, p.id DESC
                 ) AS choice
          FROM game_predictions p
          WHERE (? IS NULL OR p.model_version = ?)
        )
        SELECT v.prediction_date, v.game_id, v.underdog_team, v.underdog_odds,
               v.expected_return, v.bookmaker, r.winner
        FROM ranked p
        JOIN value_bet_predictions v ON v.game_id=p.game_id AND v.prediction_date=p.prediction_date
          AND v.model_version=p.model_version AND v.lineup_status=p.lineup_status AND v.created_at=p.created_at
        JOIN game_results r ON r.game_id = p.game_id
        WHERE p.choice = 1 AND v.recommended = 1 AND v.market_quality_passed = 1 AND v.betting_open = 1
        ORDER BY v.prediction_date, v.game_id
        """
        with self.connect() as connection:
            rows = connection.execute(query, (model_version, model_version)).fetchall()
        settled = [row for row in rows if row["winner"] is not None]
        profit = sum(
            row["underdog_odds"] - 1 if row["winner"] == row["underdog_team"] else -1
            for row in settled
        )
        wins = sum(row["winner"] == row["underdog_team"] for row in settled)
        return {
            "recommended": len(rows), "settled": len(settled), "wins": wins,
            "profitUnits": round(profit, 3),
            "roi": round(profit / len(settled) * 100, 1) if settled else None,
        }

    def evaluated_value_candidates(self, model_version: str | None = None) -> list[dict[str, Any]]:
        """추천 여부와 무관하게 저장된 배당 후보를 경기당 가장 신뢰할 시점으로 평가한다."""
        query = """
        WITH ranked AS (
          SELECT p.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY p.game_id
                   ORDER BY CASE p.lineup_status WHEN 'confirmed' THEN 0 ELSE 1 END, p.created_at DESC, p.id DESC
                 ) AS choice
          FROM game_predictions p
          WHERE (? IS NULL OR p.model_version = ?)
        )
        SELECT v.prediction_date, v.game_id, v.model_version, v.lineup_status,
               v.underdog_team, v.underdog_odds, v.model_probability,
               v.market_probability, v.expected_return, v.return_advantage,
               v.bookmaker, v.bookmaker_count, v.market_age_minutes,
               v.market_quality_passed, v.betting_open, v.market_updated_at, v.commence_at, r.winner
        FROM ranked p
        JOIN value_bet_predictions v ON v.game_id=p.game_id AND v.prediction_date=p.prediction_date
          AND v.model_version=p.model_version AND v.lineup_status=p.lineup_status AND v.created_at=p.created_at
        JOIN game_results r ON r.game_id = p.game_id
        WHERE p.choice = 1
        ORDER BY v.prediction_date, v.game_id
        """
        with self.connect() as connection:
            rows = connection.execute(query, (model_version, model_version)).fetchall()
        return [dict(row) for row in rows]


def _iso_game_date(value: str) -> str:
    compact = value.replace("-", "")
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
