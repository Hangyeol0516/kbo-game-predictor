import json
import unittest
from unittest.mock import patch

from kbo_analysis import FormParser, TableParser, fetch_lineup


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
