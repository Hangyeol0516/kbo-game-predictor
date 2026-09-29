#!/usr/bin/env python3
"""정적 앱과 KBO 분석 API를 함께 제공하는 무의존성 개발 서버."""

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

from kbo_analysis import active_model_version, analyze, fetch_games, odds_provider_status
from storage import PredictionStore


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except ValueError:
        return default


ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = PredictionStore()
KST = timezone(timedelta(hours=9))
COLLECTOR_STATE = {"lastRun": None, "lastAnalysis": None, "lastResultSync": None, "lastError": None}
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
                or (odds_status["configured"] and odds_status["lastError"])
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
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                return self.send_json({"error": "날짜 형식은 YYYY-MM-DD여야 합니다."}, 400)
            force = params.get("refresh", ["0"])[0] == "1"
            if force:
                expected_token = os.environ.get("PLAYBALL_REFRESH_TOKEN", "")
                supplied_token = self.headers.get("X-Refresh-Token", "")
                if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
                    return self.send_json({"error": "강제 갱신 권한이 없습니다."}, 403)
            if not ANALYSIS_SLOTS.acquire(blocking=False):
                return self.send_json({"error": "분석 요청이 많습니다. 잠시 후 다시 시도해 주세요."}, 429)
            try:
                analysis = analyze(date, force=force)
                STORE.save_analysis(analysis)
                return self.send_json(analysis)
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


def background_collector() -> None:
    """페이지 방문 여부와 무관하게 경기 전 스냅샷과 종료 결과를 수집한다."""
    interval = env_int("PLAYBALL_COLLECT_INTERVAL_SECONDS", 900, 60)
    while True:
        errors = []
        now = datetime.now(KST)
        today = now.date().isoformat()
        if 9 <= now.hour < 20:
            try:
                STORE.save_analysis(analyze(today))
                COLLECTOR_STATE["lastAnalysis"] = datetime.now(KST).isoformat(timespec="seconds")
            except Exception as exc:
                errors.append(f"analysis: {type(exc).__name__}")
                print(f"background analysis failed: {exc}", flush=True)
        try:
            STORE.sync_results(fetch_games)
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
