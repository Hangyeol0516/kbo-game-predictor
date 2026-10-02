import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import kbo_analysis
from kbo_analysis import analyze, evaluate_value_bet


class AnalysisCacheTest(unittest.TestCase):
    def setUp(self):
        with kbo_analysis._cache_lock:
            kbo_analysis._cache.clear()
            kbo_analysis._inflight.clear()
        self.environment = patch.dict("kbo_analysis.os.environ", {
            "PLAYBALL_ODDS_API_KEY": "test-key", "PLAYBALL_ODDS_MAX_AGE_MINUTES": "60",
            "PLAYBALL_ODDS_CLOSE_BEFORE_MINUTES": "10", "PLAYBALL_ANALYSIS_CACHE_SECONDS": "600",
            "PLAYBALL_ODDS_MIN_BOOKMAKERS": "2", "PLAYBALL_UPSET_MIN_EV": "0.08",
            "PLAYBALL_UPSET_MIN_EDGE": "0.05", "PLAYBALL_UPSET_MIN_ADVANTAGE": "0.10",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.odds_state = patch.dict(kbo_analysis._odds_state, {"lastError": None})
        self.odds_state.start()
        self.addCleanup(self.odds_state.stop)

    def payload(self, now, start_minutes=11, price_age=0):
        updated = (now - timedelta(minutes=price_age)).isoformat()
        market = {
            "teams": {
                "LG": {"price": 1.55, "bookmaker": "A", "marketProbability": 0.62, "lastUpdate": updated},
                "두산": {"price": 2.70, "bookmaker": "B", "marketProbability": 0.38, "lastUpdate": updated},
            },
            "bookmakerCount": 2, "lastUpdate": updated,
            "commenceTime": (now + timedelta(minutes=start_minutes)).isoformat(),
        }
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            value = evaluate_value_bet("LG", "두산", 0.54, 0.46, market)
        return {
            "date": "2026-10-02", "updatedAt": now.isoformat(), "valueBets": [],
            "snapshotEligible": True, "valueBetStatus": "connected", "odds": {},
            "games": [{"id": "game-1", "away": "LG", "home": "두산", "valueBet": value,
                       "snapshotEligible": True, "startsAt": market["commenceTime"]}],
        }

    @patch("kbo_analysis._analyze_uncached")
    def test_cached_recommendation_expires_at_cutoff_without_refetch(self, uncached):
        now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        uncached.return_value = self.payload(now)
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            initial = analyze("2026-10-02")
            self.assertTrue(initial["games"][0]["valueBet"]["recommendation"])
            self.assertEqual(len(initial["valueBets"]), 1)
            clock.now.return_value = now + timedelta(minutes=1)
            expired = analyze("2026-10-02")
        self.assertFalse(expired["games"][0]["valueBet"]["recommendation"])
        self.assertFalse(expired["games"][0]["valueBet"]["quality"]["bettingOpen"])
        self.assertEqual(expired["valueBets"], [])
        self.assertEqual(uncached.call_count, 1)
        self.assertTrue(initial["games"][0]["valueBet"]["recommendation"])

    @patch("kbo_analysis._analyze_uncached")
    def test_cached_price_ages_past_freshness_limit(self, uncached):
        now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        uncached.return_value = self.payload(now, start_minutes=120, price_age=59)
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.assertTrue(analyze("2026-10-02")["games"][0]["valueBet"]["recommendation"])
            clock.now.return_value = now + timedelta(minutes=2)
            expired = analyze("2026-10-02")["games"][0]["valueBet"]
        self.assertFalse(expired["recommendation"])
        self.assertFalse(expired["quality"]["marketFresh"])
        self.assertEqual(expired["quality"]["ageMinutes"], 61.0)
        self.assertEqual(uncached.call_count, 1)

    @patch("kbo_analysis._analyze_uncached")
    def test_cached_pregame_snapshot_is_ineligible_after_first_pitch(self, uncached):
        now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        uncached.return_value = self.payload(now, start_minutes=1)
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.assertTrue(analyze("2026-10-02")["snapshotEligible"])
            clock.now.return_value = now + timedelta(minutes=1)
            expired = analyze("2026-10-02")
        self.assertFalse(expired["snapshotEligible"])
        self.assertFalse(expired["games"][0]["snapshotEligible"])

    @patch("kbo_analysis._analyze_uncached")
    def test_odds_failure_suppresses_recommendations_in_analysis_cache(self, uncached):
        now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        uncached.return_value = self.payload(now)
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.assertTrue(analyze("2026-10-02")["valueBets"])
            kbo_analysis._odds_state["lastError"] = "HTTP 429"
            failed = analyze("2026-10-02")
        self.assertEqual(failed["valueBetStatus"], "provider-error")
        self.assertFalse(failed["games"][0]["valueBet"]["available"])
        self.assertEqual(failed["valueBets"], [])

    @patch("kbo_analysis._analyze_uncached")
    def test_removing_api_key_disables_cached_recommendations(self, uncached):
        now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        uncached.return_value = self.payload(now)
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.assertTrue(analyze("2026-10-02")["valueBets"])
            with patch.dict("kbo_analysis.os.environ", {"PLAYBALL_ODDS_API_KEY": ""}):
                disabled = analyze("2026-10-02")
        self.assertEqual(disabled["valueBetStatus"], "not-configured")
        self.assertFalse(disabled["games"][0]["valueBet"]["available"])
        self.assertEqual(disabled["valueBets"], [])

    @patch("kbo_analysis._analyze_uncached")
    def test_same_date_is_analyzed_only_once_concurrently(self, analyze_uncached):
        def response(date):
            time.sleep(0.05)
            return {"date": date, "games": []}

        analyze_uncached.side_effect = response
        results = []
        threads = [threading.Thread(target=lambda: results.append(analyze("2026-09-29"))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(len(results), 6)
        self.assertEqual(analyze_uncached.call_count, 1)

    @patch("kbo_analysis._analyze_uncached")
    def test_cache_has_a_bounded_number_of_dates(self, analyze_uncached):
        analyze_uncached.side_effect = lambda date: {"date": date, "games": []}
        with patch.dict("kbo_analysis.os.environ", {"PLAYBALL_ANALYSIS_CACHE_ENTRIES": "1"}):
            analyze("2026-09-28")
            analyze("2026-09-29")
            analyze("2026-09-28")
        self.assertEqual(analyze_uncached.call_count, 3)
        self.assertEqual(list(kbo_analysis._cache), ["20260928"])

    def test_force_and_odds_refresh_are_not_lost_behind_an_inflight_request(self):
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                kbo_analysis._cache.clear()
                entered, release, joined = threading.Event(), threading.Event(), threading.Event()
                results, calls = [], []

                def compute(date, refresh_odds=False):
                    calls.append(refresh_odds)
                    if len(calls) == 1:
                        entered.set()
                        if not release.wait(2):
                            raise TimeoutError("test synchronization")
                    return {"date": date, "games": [], "revision": len(calls)}

                with patch("kbo_analysis._analyze_uncached", side_effect=compute):
                    owner = threading.Thread(target=lambda: results.append(analyze("2026-10-02")))
                    owner.start()
                    self.assertTrue(entered.wait(2))
                    flight = kbo_analysis._inflight["20261002"]
                    original_wait = flight.event.wait

                    def wait(timeout):
                        joined.set()
                        return original_wait(timeout)

                    with patch.object(flight.event, "wait", side_effect=wait):
                        waiter = threading.Thread(target=lambda: results.append(analyze("2026-10-02", force=True, refresh_odds=refresh)))
                        waiter.start()
                        self.assertTrue(joined.wait(2))
                        release.set()
                        owner.join(2)
                        waiter.join(2)
                self.assertEqual(calls, [False, refresh])
                self.assertEqual([result["revision"] for result in results], [2, 2])

    def test_failed_refresh_is_reported_to_waiters_instead_of_returning_old_cache(self):
        with patch("kbo_analysis._analyze_uncached", return_value={"date": "2026-10-02", "games": [], "revision": 0}):
            analyze("2026-10-02")
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def compute(date):
            entered.set()
            release.wait(2)
            raise ValueError("refresh failed")

        def call(force):
            try:
                analyze("2026-10-02", force=force)
            except ValueError as exc:
                errors.append(str(exc))

        with patch("kbo_analysis._analyze_uncached", side_effect=compute):
            owner = threading.Thread(target=call, args=(True,))
            owner.start()
            self.assertTrue(entered.wait(2))
            flight = kbo_analysis._inflight["20261002"]
            original_wait = flight.event.wait

            def wait(timeout):
                joined.set()
                return original_wait(timeout)

            with patch.object(flight.event, "wait", side_effect=wait):
                waiter = threading.Thread(target=call, args=(False,))
                waiter.start()
                self.assertTrue(joined.wait(2))
                release.set()
                owner.join(2)
                waiter.join(2)
        self.assertEqual(errors, ["refresh failed", "refresh failed"])


if __name__ == "__main__":
    unittest.main()
