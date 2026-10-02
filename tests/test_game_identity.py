import unittest
from unittest.mock import patch

from kbo_analysis import _parse_market_odds, match_market_odds, fetch_pitcher_hands, _hitter_predictions


class GameIdentityTest(unittest.TestCase):
    def games(self):
        return [{"G_ID": str(i), "G_TM": time, "AWAY_NM": "LG", "HOME_NM": "두산",
                 "T_PIT_P_NM": "원정선발", "B_PIT_P_NM": f"선발{i}"}
                for i, time in enumerate(("14:00", "18:30"), 1)]

    def events(self):
        return [{"id": f"event-{i}", "away_team": "LG Twins", "home_team": "Doosan Bears",
                 "commence_time": start, "bookmakers": [{"title": "Book", "last_update": start,
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "LG Twins", "price": 1.5 + i * 0.1},
                        {"name": "Doosan Bears", "price": 2.0 + i * 0.1},
                    ]}]}]}
                for i, start in enumerate(("2026-10-02T05:00:00Z", "2026-10-02T09:30:00Z"), 1)]

    def test_doubleheader_preserves_both_prices_and_matches_distinct_kbo_game_ids(self):
        markets = _parse_market_odds(self.events())["2026-10-02"]
        self.assertEqual(len(markets), 2)
        matched = match_market_odds(self.games(), "2026-10-02", markets)
        self.assertEqual(matched["1"]["eventId"], "event-1")
        self.assertEqual(matched["2"]["eventId"], "event-2")
        self.assertNotEqual(matched["1"]["teams"]["LG"]["price"], matched["2"]["teams"]["LG"]["price"])

    def test_one_event_cannot_be_assigned_to_two_games(self):
        games = self.games()
        games[1]["G_TM"] = "14:30"
        markets = _parse_market_odds(self.events()[:1])["2026-10-02"]
        self.assertEqual(match_market_odds(games, "2026-10-02", markets), {})

    def test_ambiguous_or_unknown_start_is_not_matched(self):
        events = self.events()
        events[1]["commence_time"] = events[0]["commence_time"]
        markets = _parse_market_odds(events)["2026-10-02"]
        self.assertEqual(match_market_odds(self.games(), "2026-10-02", markets), {})
        game = self.games()[0]
        game["G_TM"] = "1차전 종료 후"
        self.assertEqual(match_market_odds([game], "2026-10-02", markets), {})

    @patch("kbo_analysis.fetch_pitcher_hand")
    def test_doubleheader_hitters_use_each_games_opposing_pitcher_type(self, hand):
        hand.side_effect = lambda game, side: {
            "team": game["HOME_NM"] if side == "home" else game["AWAY_NM"],
            "split": "LO" if game["G_ID"] == "1" else "RO",
            "label": "좌투" if game["G_ID"] == "1" else "우투",
        }
        games = self.games()
        hands = fetch_pitcher_hands(games)
        self.assertEqual(len(hands), 4)
        lineups = {game["G_ID"]: {"confirmed": True, "away": [
            {"team": "LG", "name": "타자", "order": 1, "position": "중견수"}], "home": []} for game in games}
        result, _, by_game = _hitter_predictions(
            games, lineups, {("LG", "타자"): {"ab": 100, "hits": 30, "avg": 0.3}},
            {"LG": {"avg": 0.28, "era": 4}, "두산": {"avg": 0.27, "era": 5}}, {}, hands,
            {("LG", "타자", "LO"): {"ab": 100, "hits": 40, "avg": 0.4},
             ("LG", "타자", "RO"): {"ab": 100, "hits": 10, "avg": 0.1}}, {},
        )
        players = {player["gameId"]: player for player in result["전체"]}
        self.assertEqual(players["1"]["pitcherHand"], "좌투")
        self.assertEqual(players["2"]["pitcherHand"], "우투")
        self.assertGreater(players["1"]["probability"], players["2"]["probability"])
        self.assertEqual(by_game["2"]["전체"][0]["gameId"], "2")
