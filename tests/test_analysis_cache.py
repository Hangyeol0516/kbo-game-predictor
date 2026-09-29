import threading
import time
import unittest
from unittest.mock import patch

import kbo_analysis
from kbo_analysis import analyze


class AnalysisCacheTest(unittest.TestCase):
    def setUp(self):
        with kbo_analysis._cache_lock:
            kbo_analysis._cache.clear()
            kbo_analysis._inflight.clear()

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


if __name__ == "__main__":
    unittest.main()
