#!/usr/bin/env python3
"""정적 앱과 KBO 분석 API를 함께 제공하는 무의존성 개발 서버."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from kbo_analysis import analyze, fetch_games, odds_provider_status
from storage import PredictionStore


ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = PredictionStore()
KST = timezone(timedelta(hours=9))
COLLECTOR_STATE = {"lastRun": None, "lastError": None}
STATIC_PATHS = {"/", "/index.html", "/app.js", "/styles.css"}


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
            })
        if parsed.path == "/api/performance":
            try:
                STORE.sync_results(fetch_games)
                return self.send_json(STORE.performance_summary())
            except Exception as exc:
                self.log_error("performance sync failed: %s", exc)
                return self.send_json({"error": "성능 데이터를 갱신하지 못했습니다."}, 502)
        if parsed.path == "/api/analysis":
            params = parse_qs(parsed.query)
            date = params.get("date", [""])[0]
            force = params.get("refresh", ["0"])[0] == "1"
            if not date:
                return self.send_json({"error": "date가 필요합니다."}, 400)
            try:
                analysis = analyze(date, force=force)
                STORE.save_analysis(analysis)
                return self.send_json(analysis)
            except ValueError:
                return self.send_json({"error": "날짜 형식은 YYYY-MM-DD여야 합니다."}, 400)
            except Exception as exc:
                self.log_error("analysis failed: %s", exc)
                return self.send_json({"error": "KBO 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."}, 502)
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
    interval = max(int(os.environ.get("PLAYBALL_COLLECT_INTERVAL_SECONDS", "900")), 60)
    while True:
        try:
            now = datetime.now(KST)
            today = now.date().isoformat()
            if 9 <= now.hour < 20:
                STORE.save_analysis(analyze(today))
            STORE.sync_results(fetch_games)
            COLLECTOR_STATE["lastRun"] = datetime.now(KST).isoformat(timespec="seconds")
            COLLECTOR_STATE["lastError"] = None
        except Exception as exc:
            COLLECTOR_STATE["lastError"] = str(exc)
            print(f"background collection failed: {exc}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    host = os.environ.get("KBO_HOST", "0.0.0.0")
    port = int(os.environ.get("KBO_PORT", "8000"))
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
