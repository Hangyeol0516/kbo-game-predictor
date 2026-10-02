#!/usr/bin/env python3
"""정적 앱과 KBO 분석 API를 함께 제공하는 무의존성 단일 컨테이너 서버."""

from __future__ import annotations

import hmac
import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from kbo_analysis import DataContractError, active_model_version, analyze, fetch_games, odds_provider_status
from storage import PredictionStore


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except ValueError:
        return default


ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = PredictionStore()
KST = timezone(timedelta(hours=9))
COLLECTOR_STATE = {
    "lastRun": None, "lastAnalysis": None, "lastResultSync": None,
    "lastOddsSlot": None, "lastError": None,
}
STATIC_PATHS = {"/", "/index.html", "/app.js", "/styles.css"}
ANALYSIS_SLOTS = threading.BoundedSemaphore(env_int("PLAYBALL_MAX_CONCURRENT_ANALYSES", 4, 1))


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "PLAYBALL"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            try:
                with STORE.connect() as connection:
                    connection.execute("SELECT 1").fetchone()
                database_status = "ready"
            except Exception:
                database_status = "error"
            odds_status = odds_provider_status()
            status = "degraded" if (
                COLLECTOR_STATE["lastError"] or database_status == "error"
                or (odds_status["configured"] and (odds_status["lastError"] or odds_status.get("persistenceError")))
            ) else "ok"
            return self.send_json({
                "status": status, "database": database_status,
                "collector": COLLECTOR_STATE, "odds": odds_status,
            }, 503 if database_status == "error" else 200)
        if parsed.path == "/api/performance":
            try:
                requested_version = parse_qs(parsed.query).get("modelVersion", [""])[0]
                model_version = None if requested_version == "all" else (requested_version or active_model_version())
                return self.send_json(STORE.performance_summary(model_version))
            except Exception as exc:
                self.log_error("performance read failed: %s", exc)
                return self.send_json({"error": "성능 데이터를 읽지 못했습니다."}, 500)
        if parsed.path == "/api/analysis":
            params = parse_qs(parsed.query)
            date = params.get("date", [""])[0]
            if not date:
                return self.send_json({"error": "date가 필요합니다."}, 400)
            try:
                selected_date = datetime.strptime(date, "%Y-%m-%d").date()
                date = selected_date.isoformat()
            except ValueError:
                return self.send_json({"error": "날짜 형식은 YYYY-MM-DD여야 합니다."}, 400)
            force = params.get("refresh", ["0"])[0] == "1"
            mode = params.get("mode", ["auto"])[0]
            if mode not in ("auto", "historical", "reanalysis"):
                return self.send_json({"error": "지원하지 않는 조회 방식입니다."}, 400)
            if force:
                expected_token = os.environ.get("PLAYBALL_REFRESH_TOKEN", "")
                supplied_token = self.headers.get("X-Refresh-Token", "")
                if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
                    return self.send_json({"error": "강제 갱신 권한이 없습니다."}, 403)
            is_past = selected_date < datetime.now(KST).date()
            if mode == "historical" or (is_past and mode == "auto"):
                try:
                    return self.send_json(STORE.historical_analysis(date))
                except Exception as exc:
                    self.log_error("history read failed: %s", exc)
                    return self.send_json({"error": "저장된 예측을 읽지 못했습니다."}, 500)
            if not ANALYSIS_SLOTS.acquire(blocking=False):
                return self.send_json({"error": "분석 요청이 많습니다. 잠시 후 다시 시도해 주세요."}, 429)
            try:
                analysis = analyze(date, force=force, refresh_odds=force and not is_past)
                analysis["viewMode"] = "reanalysis" if is_past else "live"
                if is_past:
                    analysis["snapshotEligible"] = False
                    analysis["valueBetStatus"] = "reanalysis"
                    analysis["valueBets"] = []
                    for game in analysis["games"]:
                        game["snapshotEligible"] = False
                        game["valueBet"] = {"available": False, "recommendation": False, "reason": "현재 기록을 사용한 재분석"}
                else:
                    STORE.save_analysis(analysis)
                return self.send_json(analysis)
            except DataContractError as exc:
                self.log_error("upstream data invalid: %s", exc)
                return self.send_json({"error": str(exc), "dataQuality": {"status": "invalid"}}, 502)
            except Exception as exc:
                self.log_error("analysis failed: %s", exc)
                return self.send_json({"error": "KBO 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."}, 502)
            finally:
                ANALYSIS_SLOTS.release()
        if parsed.path not in STATIC_PATHS:
            return self.send_error(404, "Not found")
        return super().do_GET()

    def do_HEAD(self):
        if urlparse(self.path).path not in STATIC_PATHS:
            return self.send_error(404, "Not found")
        return super().do_HEAD()

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        super().end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _game_start_times(games: list[dict], now: datetime, include_started: bool = False) -> list[datetime]:
    starts = []
    for game in games:
        state = game.get("GAME_STATE_SC")
        try:
            hour, minute = map(int, str(game.get("G_TM", "")).strip()[:5].split(":"))
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            pending = not bool(game.get("GAME_RESULT_CK")) and (state is None or str(state) == "1")
            started = start <= now and (bool(game.get("GAME_RESULT_CK")) or str(state) == "2")
            if pending or (include_started and started):
                starts.append(start)
        except (TypeError, ValueError):
            continue
    return sorted(starts)


def collector_odds_slot(games: list[dict], now: datetime) -> str | None:
    """현재 시각까지 도달한 가장 최근 배당 수집 슬롯을 반환한다."""
    starts = _game_start_times(games, now, include_started=True)
    if not starts or now >= starts[0]:
        return None
    slots = (
        ("morning", now.replace(hour=9, minute=0, second=0, microsecond=0)),
        ("pregame", starts[0] - timedelta(hours=3)),
        ("closing", starts[0] - timedelta(minutes=30)),
    )
    due = [name for name, scheduled_at in slots if now >= scheduled_at]
    return due[-1] if due else None


def collector_analysis_due(games: list[dict], now: datetime) -> bool:
    """오전 9시부터 마지막 미시작 경기의 라인업까지 분석한다."""
    starts = _game_start_times(games, now)
    morning = now.replace(hour=9, minute=0, second=0, microsecond=0)
    return bool(starts and morning <= now < starts[-1])


def background_collector() -> None:
    """페이지 방문 여부와 무관하게 경기 전 스냅샷과 종료 결과를 수집한다."""
    interval = env_int("PLAYBALL_COLLECT_INTERVAL_SECONDS", 900, 60)
    while True:
        errors = []
        now = datetime.now(KST)
        today = now.date().isoformat()
        schedule_ok = False
        try:
            games = fetch_games(today)
            schedule_ok = True
        except Exception as exc:
            games = []
            errors.append(f"schedule: {type(exc).__name__}")
            print(f"background schedule failed: {exc}", flush=True)
        if collector_analysis_due(games, now):
            odds_slot = collector_odds_slot(games, now)
            try:
                refresh_odds = bool(odds_slot and odds_provider_status()["configured"]
                                    and STORE.claim_odds_slot(today, odds_slot))
                STORE.save_analysis(analyze(today, force=True, refresh_odds=refresh_odds))
                if refresh_odds:
                    COLLECTOR_STATE["lastOddsSlot"] = f"{today}:{odds_slot}"
                COLLECTOR_STATE["lastAnalysis"] = datetime.now(KST).isoformat(timespec="seconds")
            except Exception as exc:
                errors.append(f"analysis: {type(exc).__name__}")
                print(f"background analysis failed: {exc}", flush=True)
        try:
            def report_result_error(date, exc):
                errors.append(f"results {date}: {type(exc).__name__}")
                print(f"background result sync failed for {date}: {exc}", flush=True)

            STORE.sync_results(
                lambda date: games if schedule_ok and date == today else fetch_games(date),
                on_error=report_result_error,
            )
            COLLECTOR_STATE["lastResultSync"] = datetime.now(KST).isoformat(timespec="seconds")
        except Exception as exc:
            errors.append(f"results: {type(exc).__name__}")
            print(f"background result sync failed: {exc}", flush=True)
        COLLECTOR_STATE["lastRun"] = datetime.now(KST).isoformat(timespec="seconds")
        COLLECTOR_STATE["lastError"] = "; ".join(errors) or None
        time.sleep(interval)


if __name__ == "__main__":
    host = os.environ.get("KBO_HOST", "0.0.0.0")
    port = int(os.environ.get("KBO_PORT", "8000"))
    if os.environ.get("PLAYBALL_COLLECTOR_ENABLED", "1") != "0":
        threading.Thread(target=background_collector, name="playball-collector", daemon=True).start()
    print(f"PLAYBALL server: http://{host}:{port}")
    server = ThreadingHTTPServer((host, port), AppHandler)

    def request_shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, name="playball-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
