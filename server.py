#!/usr/bin/env python3
"""정적 앱과 KBO 분석 API를 함께 제공하는 무의존성 개발 서버."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from kbo_analysis import analyze, fetch_games
from storage import PredictionStore


ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = PredictionStore()
KST = timezone(timedelta(hours=9))
COLLECTOR_STATE = {"lastRun": None, "lastError": None}


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self.send_json({"status": "ok", "database": str(STORE.path), "collector": COLLECTOR_STATE})
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
        return super().do_GET()

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
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()
