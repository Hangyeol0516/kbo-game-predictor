import unittest
from unittest.mock import patch

import kbo_analysis
from kbo_analysis import (
    _parse_registered_players,
    _parse_roster_movements,
    _weather_run_factor,
    evaluate_value_bet,
    fetch_market_odds,
    odds_provider_status,
)


class ContextValueTest(unittest.TestCase):
    def setUp(self):
        kbo_analysis._odds_cache.update({"created": 0.0, "regions": None, "credential": None, "eventsByDate": {}})
        kbo_analysis._odds_state.update({
            "lastFetch": None, "lastError": None, "creditsRemaining": None,
            "creditsUsed": None, "requestCost": None, "eventCount": 0,
        })

    def test_registered_players_and_movements_are_parsed(self):
        rows = [
            ["구단", "감독(1)", "코치(1)", "투수(2)", "포수(1)", "내야수(1)", "외야수(1)"],
            ["LG6명", "감독(70)", "코치(71)", "임찬규(1)고우석(19)", "박동원(27)", "오지환(10)", "박해민(17)"],
            ["선수", "포지션", "팀"], ["홍창기", "외", "LG"],
            ["선수", "포지션", "팀"], ["문성주", "외", "LG"],
        ]
        self.assertEqual(
            _parse_registered_players(rows)["LG"],
            {"임찬규", "고우석", "박동원", "오지환", "박해민"},
        )
        registered, deregistered = _parse_roster_movements(rows)
        self.assertEqual(registered["LG"], ["홍창기"])
        self.assertEqual(deregistered["LG"], ["문성주"])

    def test_weather_factor_is_conservative_and_bounded(self):
        self.assertEqual(_weather_run_factor(None), 1.0)
        self.assertAlmostEqual(_weather_run_factor(30), 1.03)
        self.assertEqual(_weather_run_factor(50), 1.05)
        self.assertEqual(_weather_run_factor(-10), 0.95)

    def test_underdog_is_recommended_only_for_sufficient_incremental_return(self):
        market = {
            "teams": {
                "LG": {"price": 1.55, "bookmaker": "A", "marketProbability": 0.62},
                "두산": {"price": 2.70, "bookmaker": "B", "marketProbability": 0.38},
            },
            "bookmakerCount": 4,
            "lastUpdate": "2026-09-28T00:00:00Z",
        }
        with patch.dict("kbo_analysis.os.environ", {}, clear=False):
            value = evaluate_value_bet("LG", "두산", 0.54, 0.46, market)
        self.assertTrue(value["recommendation"])
        self.assertEqual(value["underdog"]["team"], "두산")
        self.assertEqual(value["underdog"]["expectedReturnPct"], 24.2)
        self.assertGreater(value["returnAdvantagePp"], 10)

        no_value = evaluate_value_bet("LG", "두산", 0.60, 0.40, market)
        self.assertFalse(no_value["recommendation"])

    def test_missing_market_never_creates_an_upset_pick(self):
        value = evaluate_value_bet("LG", "두산", 0.4, 0.6, None)
        self.assertFalse(value["available"])
        self.assertFalse(value["recommendation"])

    @patch("kbo_analysis._get_json_response", side_effect=RuntimeError("provider unavailable"))
    def test_provider_error_never_returns_stale_odds(self, _get_json_response):
        kbo_analysis._odds_cache.update({
            "created": 1.0, "regions": "eu", "credential": "old", "eventsByDate": {
                "2026-09-28": {("LG", "두산"): {"stale": True}},
            },
        })
        with patch.dict("kbo_analysis.os.environ", {
            "PLAYBALL_ODDS_API_KEY": "secret", "PLAYBALL_ODDS_REGIONS": "eu",
        }):
            self.assertEqual(fetch_market_odds("2026-09-28"), {})
            self.assertEqual(odds_provider_status()["lastError"], "RuntimeError")

    @patch("kbo_analysis._get_json_response")
    def test_market_odds_uses_best_price_and_devigged_consensus(self, get_json_response):
        get_json_response.return_value = ([{
            "away_team": "LG Twins", "home_team": "Doosan Bears",
            "commence_time": "2026-09-28T09:30:00Z",
            "bookmakers": [
                {"title": "Book A", "last_update": "2026-09-28T08:00:00Z", "markets": [{
                    "key": "h2h", "outcomes": [
                        {"name": "LG Twins", "price": 1.7}, {"name": "Doosan Bears", "price": 2.2},
                    ],
                }]},
                {"title": "Book B", "last_update": "2026-09-28T08:05:00Z", "markets": [{
                    "key": "h2h", "outcomes": [
                        {"name": "LG Twins", "price": 1.75}, {"name": "Doosan Bears", "price": 2.1},
                    ],
                }]},
            ],
        }], {"x-requests-remaining": "499", "x-requests-used": "1", "x-requests-last": "1"})
        with patch.dict("kbo_analysis.os.environ", {
            "PLAYBALL_ODDS_API_KEY": "secret", "PLAYBALL_ODDS_REGIONS": "eu",
        }):
            market = fetch_market_odds("2026-09-28")[("LG", "두산")]
            self.assertEqual(fetch_market_odds("2026-09-29"), {})
            status = odds_provider_status()
        self.assertEqual(market["teams"]["LG"]["price"], 1.75)
        self.assertEqual(market["teams"]["LG"]["bookmaker"], "Book B")
        self.assertEqual(market["bookmakerCount"], 2)
        self.assertAlmostEqual(
            market["teams"]["LG"]["marketProbability"]
            + market["teams"]["두산"]["marketProbability"],
            1.0,
        )
        self.assertEqual(get_json_response.call_count, 1)
        self.assertEqual(status["creditsRemaining"], 499.0)
        self.assertEqual(status["eventCount"], 1)


if __name__ == "__main__":
    unittest.main()
