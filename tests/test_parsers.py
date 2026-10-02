import json
import unittest
from unittest.mock import patch

from kbo_analysis import (
    DataContractError, FormParser, TableParser, fetch_lineup, _record_entries,
    _record_number, fetch_pitcher_hand, fetch_games, fetch_boxscore_pitchers,
)


class UpstreamParserContractTest(unittest.TestCase):
    def test_table_parser_accepts_expected_kbo_table_classes(self):
        parser = TableParser()
        parser.feed(
            '<table class="tData"><tr><th>팀</th><th>선수</th></tr>'
            '<tr><td>LG</td><td><a>홍창기</a></td></tr></table>'
        )
        self.assertEqual(parser.rows, [["팀", "선수"], ["LG", "홍창기"]])

    def test_form_parser_keeps_hidden_fields_and_selected_options(self):
        parser = FormParser()
        parser.feed(
            '<input type="hidden" name="__VIEWSTATE" value="state" />'
            '<select name="team"><option value="">전체</option>'
            '<option value="LG" selected>LG</option></select>'
        )
        self.assertEqual(parser.fields["__VIEWSTATE"], "state")
        self.assertEqual(parser.fields["team"], "LG")

    def test_record_columns_are_resolved_by_name_when_order_changes(self):
        parser = TableParser()
        parser.rows = [["순위", "H", "선수명", "AB", "팀명"], ["1", "30", "타자", "100", "LG"]]
        row = _record_entries(parser, ("선수명", "팀명", "AB", "H"), "test")[0]
        self.assertEqual(_record_number(row, "AB"), 100)
        self.assertEqual(_record_number(row, "H"), 30)

    def test_missing_columns_empty_rows_and_invalid_numbers_are_rejected(self):
        parser = TableParser()
        parser.rows = [["순위", "AB"], ["1", "100"]]
        with self.assertRaises(DataContractError):
            _record_entries(parser, ("AB", "H"), "test")
        parser.rows = [["순위", "AB", "H"]]
        with self.assertRaises(DataContractError):
            _record_entries(parser, ("AB", "H"), "test")
        for value in ("-", "NaN", "inf", "-1", "문자"):
            with self.subTest(value=value), self.assertRaises(DataContractError):
                _record_number({"AB": value}, "AB")

    @patch("kbo_analysis._post_json", return_value={"now": []})
    def test_missing_pitcher_type_is_unknown_instead_of_assumed_right_handed(self, post_json):
        hand = fetch_pitcher_hand({"T_PIT_P_NM": "투수", "AWAY_NM": "LG"}, "away")
        self.assertIsNone(hand["split"])
        self.assertEqual(hand["label"], "유형 미확인")

    @patch("kbo_analysis._post_json", return_value={"changed": []})
    def test_changed_schedule_contract_is_not_treated_as_no_games(self, post_json):
        with self.assertRaises(DataContractError):
            fetch_games("2026-10-02")

    @patch("kbo_analysis._post_json")
    def test_string_zero_in_schedule_is_not_interpreted_as_a_finished_game(self, post_json):
        post_json.return_value = {"game": [{"LE_ID": 1, "G_ID": "game", "AWAY_NM": "LG",
                                            "HOME_NM": "두산", "GAME_RESULT_CK": "0"}]}
        self.assertFalse(fetch_games("2026-10-02")[0]["GAME_RESULT_CK"])

    @patch("kbo_analysis._post_json")
    def test_invalid_pitch_count_does_not_silently_become_zero(self, post_json):
        cells = ["투수", "선발", "", "", "", "", "", "", "변경된 값"]
        table = json.dumps({"rows": [{"row": [{"Text": text} for text in cells]}]})
        post_json.return_value = {"arrPitcher": [{"table": table}, {"table": table}]}
        game = {"LE_ID": 1, "SR_ID": 0, "SEASON_ID": 2026, "G_ID": "game", "AWAY_NM": "LG", "HOME_NM": "두산"}
        with self.assertRaises(DataContractError):
            fetch_boxscore_pitchers(game)

    @patch("kbo_analysis._post_json")
    def test_lineup_contract_parses_both_teams_and_confirmation(self, post_json):
        away = {"rows": [{"row": [{"Text": "1"}, {"Text": "중견수"}, {"Text": "박해민"}]}]}
        home = {"rows": [{"row": [{"Text": "1"}, {"Text": "유격수"}, {"Text": "김재호"}]}]}
        post_json.return_value = [[{"LINEUP_CK": 1}], [], [], [json.dumps(home)], [json.dumps(away)]]
        game = {"LE_ID": 1, "SR_ID": 0, "SEASON_ID": 2026, "G_ID": "game", "AWAY_NM": "LG", "HOME_NM": "두산"}
        lineup = fetch_lineup(game)
        self.assertTrue(lineup["confirmed"])
        self.assertEqual(lineup["away"][0]["name"], "박해민")
        self.assertEqual(lineup["home"][0]["team"], "두산")


if __name__ == "__main__":
    unittest.main()
