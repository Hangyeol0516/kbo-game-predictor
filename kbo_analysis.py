"""KBO 공식 페이지를 읽어 당일 경기의 통계 기반 추정치를 만든다.

공식 공개 페이지의 데이터만 사용하며 결과는 학습된 예측 모델이 아니라
설명 가능한 휴리스틱 추정치다. 외부 요청은 메모리에서 10분간 캐시한다.
"""

from __future__ import annotations

import html
import json
import math
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


BASE_URL = "https://www.koreabaseball.com"
USER_AGENT = "PLAYBALL/0.1 (+personal KBO analysis prototype)"
TEAM_HITTER_1 = f"{BASE_URL}/Record/Team/Hitter/Basic1.aspx"
TEAM_HITTER_2 = f"{BASE_URL}/Record/Team/Hitter/Basic2.aspx"
TEAM_PITCHER_1 = f"{BASE_URL}/Record/Team/Pitcher/Basic1.aspx"
PLAYER_HITTER_1 = f"{BASE_URL}/Record/Player/HitterBasic/Basic1.aspx"

TEAM_CODES = {
    "KT": "KT", "SS": "삼성", "LG": "LG", "HT": "KIA", "OB": "두산",
    "SK": "SSG", "HH": "한화", "LT": "롯데", "NC": "NC", "WO": "키움",
}

POSITION_GROUPS = {
    "포수": "포수", "1루수": "1루수", "2루수": "2루수", "3루수": "3루수",
    "유격수": "유격수", "좌익수": "좌익수", "중견수": "중견수", "우익수": "우익수",
    "지명타자": "지명타자",
}


class TableParser(HTMLParser):
    def __init__(self, accepted_classes: tuple[str, ...] = ("tData", "tData01")) -> None:
        super().__init__()
        self.accepted_classes = accepted_classes
        self.in_table = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if tag == "table" and classes.intersection(self.accepted_classes) and not self.in_table:
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.current_row = []
        elif self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"td", "th"} and self.in_cell:
            value = " ".join("".join(self.current_cell).split())
            self.current_row.append(html.unescape(value))
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.current_row:
            self.rows.append(self.current_row)
        elif self.in_table and tag == "table":
            self.in_table = False


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "input" and attr.get("name"):
            self.inputs[attr["name"]] = attr.get("value") or ""


def _number(value: str, default: float = 0.0) -> float:
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return default


def _get_text(url: str, opener=None) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Referer": BASE_URL})
    response = (opener or urlopen).open(request, timeout=15) if opener else urlopen(request, timeout=15)
    return response.read().decode("utf-8")


def _post_json(path: str, payload: dict[str, str]) -> Any:
    body = urlencode(payload).encode()
    request = Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE_URL}/Schedule/GameCenter/Main.aspx",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def _table_rows(url: str) -> list[list[str]]:
    parser = TableParser()
    parser.feed(_get_text(url))
    return [row for row in parser.rows if row and row[0] != "순위"]


def fetch_games(date: str) -> list[dict[str, Any]]:
    raw = _post_json(
        "/ws/Main.asmx/GetKboGameList",
        {"leId": "1", "srId": "0,1,3,4,5,6,7,8,9", "date": date.replace("-", "")},
    )
    return [game for game in raw.get("game", []) if int(game.get("LE_ID", 0)) == 1]


def fetch_lineup(game: dict[str, Any]) -> dict[str, Any]:
    raw = _post_json(
        "/ws/Schedule.asmx/GetLineUpAnalysis",
        {
            "leId": str(game["LE_ID"]), "srId": str(game["SR_ID"]),
            "seasonId": str(game["SEASON_ID"]), "gameId": game["G_ID"],
        },
    )

    def parse_side(index: int, team: str) -> list[dict[str, Any]]:
        if not raw[index]:
            return []
        grid = json.loads(raw[index][0])
        players = []
        for item in grid.get("rows", []):
            cells = [str(cell.get("Text", "")).strip() for cell in item.get("row", [])]
            if len(cells) >= 3:
                players.append({"order": int(cells[0]), "position": cells[1], "name": cells[2], "team": team})
        return players

    return {
        "confirmed": bool(raw[0][0].get("LINEUP_CK")) if raw and raw[0] else False,
        "away": parse_side(4, game["AWAY_NM"]),
        "home": parse_side(3, game["HOME_NM"]),
    }


def fetch_team_stats() -> dict[str, dict[str, float]]:
    hitting_1, hitting_2, pitching_1 = (_table_rows(url) for url in (TEAM_HITTER_1, TEAM_HITTER_2, TEAM_PITCHER_1))
    stats: dict[str, dict[str, float]] = {}
    for row in hitting_1:
        if len(row) >= 15 and row[1] != "합계":
            games = max(_number(row[3]), 1)
            stats[row[1]] = {
                "avg": _number(row[2]), "games": games, "runs": _number(row[6]),
                "runs_per_game": _number(row[6]) / games, "hits": _number(row[7]),
            }
    for row in hitting_2:
        if len(row) >= 11 and row[1] in stats:
            stats[row[1]].update({"slg": _number(row[8]), "obp": _number(row[9]), "ops": _number(row[10])})
    for row in pitching_1:
        if len(row) >= 18 and row[1] in stats:
            stats[row[1]].update({"era": _number(row[2]), "wins": _number(row[4]), "losses": _number(row[5]), "whip": _number(row[17])})
    return stats


def fetch_hitter_stats(team_ids: list[str]) -> dict[tuple[str, str], dict[str, float]]:
    """WebForms 팀 필터를 순차 적용해 당일 참가 팀의 모든 타자를 가져온다."""
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    current_html = _get_text(PLAYER_HITTER_1, opener)
    target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlTeam$ddlTeam"
    result: dict[tuple[str, str], dict[str, float]] = {}

    for team_id in team_ids:
        form_parser = FormParser()
        form_parser.feed(current_html)
        fields = form_parser.inputs
        fields["__EVENTTARGET"] = target
        fields["__EVENTARGUMENT"] = ""
        fields[target] = team_id
        request = Request(
            PLAYER_HITTER_1,
            data=urlencode(fields).encode(),
            headers={"User-Agent": USER_AGENT, "Referer": PLAYER_HITTER_1, "Content-Type": "application/x-www-form-urlencoded"},
        )
        current_html = opener.open(request, timeout=15).read().decode("utf-8")
        parser = TableParser(("tData01",))
        parser.feed(current_html)
        for row in parser.rows:
            if len(row) >= 16 and row[0] != "순위":
                name, team = row[1].lstrip("* "), row[2]
                result[(team, name)] = {
                    "avg": _number(row[3]), "games": _number(row[4]), "pa": _number(row[5]),
                    "ab": _number(row[6]), "hits": _number(row[8]), "hr": _number(row[11]),
                }
    return result


def _mean_std(values: list[float]) -> tuple[float, float]:
    return statistics.mean(values), max(statistics.pstdev(values), 0.001)


def _game_prediction(game: dict[str, Any], team_stats: dict[str, dict[str, float]]) -> dict[str, Any]:
    away_name, home_name = game["AWAY_NM"], game["HOME_NM"]
    away, home = team_stats[away_name], team_stats[home_name]
    r_mean, r_std = _mean_std([team["runs_per_game"] for team in team_stats.values()])
    e_mean, e_std = _mean_std([team["era"] for team in team_stats.values()])
    o_mean, o_std = _mean_std([team["obp"] for team in team_stats.values()])

    def rating(team: dict[str, float]) -> float:
        offense = (team["runs_per_game"] - r_mean) / r_std
        pitching = (e_mean - team["era"]) / e_std
        on_base = (team["obp"] - o_mean) / o_std
        return 0.45 * offense + 0.35 * pitching + 0.20 * on_base

    home_logit = 0.13 + 0.62 * (rating(home) - rating(away))
    home_probability = 1 / (1 + math.exp(-home_logit))
    # 선발 개인 지표와 부상 변수를 아직 반영하지 않는 v1이므로 과신을 막는다.
    home_probability = min(max(home_probability, 0.28), 0.72)
    home_percent = round(home_probability * 100)
    away_percent = 100 - home_percent
    pick = home_name if home_percent >= away_percent else away_name
    margin = abs(home_percent - away_percent)
    confidence = "높음" if margin >= 20 else "보통" if margin >= 10 else "접전"
    better_offense = home_name if home["runs_per_game"] > away["runs_per_game"] else away_name
    better_pitching = home_name if home["era"] < away["era"] else away_name

    return {
        "id": game["G_ID"], "time": game["G_TM"], "park": game["S_NM"],
        "away": away_name, "home": home_name,
        "awayPitcher": game.get("T_PIT_P_NM", "").strip() or "미정",
        "homePitcher": game.get("B_PIT_P_NM", "").strip() or "미정",
        "awayProb": away_percent, "homeProb": home_percent, "pick": pick, "confidence": confidence,
        "lineupConfirmed": bool(game.get("LINEUP_CK")),
        "reasons": [
            f"{better_offense} 시즌 득점력 우위 ({team_stats[better_offense]['runs_per_game']:.2f}점/경기)",
            f"{better_pitching} 팀 평균자책점 우위 ({team_stats[better_pitching]['era']:.2f})",
            f"{away_name} 출루율 {away['obp']:.3f} · {home_name} 출루율 {home['obp']:.3f}",
            "홈 경기 기본 보정 3.2% 적용",
        ],
        "metrics": {"awayRpg": round(away["runs_per_game"], 2), "homeRpg": round(home["runs_per_game"], 2), "awayEra": away["era"], "homeEra": home["era"]},
    }


def _hitter_predictions(
    games: list[dict[str, Any]], lineups: dict[str, dict[str, Any]],
    hitter_stats: dict[tuple[str, str], dict[str, float]], team_stats: dict[str, dict[str, float]],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    league_avg = statistics.mean(team["avg"] for team in team_stats.values())
    league_era, league_era_std = _mean_std([team["era"] for team in team_stats.values()])
    hitters: list[dict[str, Any]] = []
    all_confirmed = True

    for game in games:
        lineup = lineups[game["G_ID"]]
        all_confirmed = all_confirmed and lineup["confirmed"]
        for side, opponent in (("away", game["HOME_NM"]), ("home", game["AWAY_NM"])):
            for player in lineup[side]:
                stats = hitter_stats.get((player["team"], player["name"]), {})
                ab, hits = stats.get("ab", 0), stats.get("hits", 0)
                pa_average = (hits + 60 * league_avg) / (ab + 60)
                pitcher_factor = 1 + 0.055 * (team_stats[opponent]["era"] - league_era) / league_era_std
                pitcher_factor = min(max(pitcher_factor, 0.91), 1.09)
                per_ab = min(max(pa_average * pitcher_factor, 0.12), 0.42)
                expected_ab = max(3.55, 4.45 - 0.10 * (player["order"] - 1))
                probability = round((1 - (1 - per_ab) ** expected_ab) * 100)
                position = POSITION_GROUPS.get(player["position"])
                if not position:
                    continue
                hitters.append({
                    "name": player["name"], "team": player["team"], "position": position,
                    "rawPosition": player["position"], "probability": probability,
                    "opponent": f"vs {opponent}", "pitcher": f"상대 선발 {game.get('B_PIT_P_NM' if side == 'away' else 'T_PIT_P_NM', '').strip() or '미정'}",
                    "order": f"{player['order']}번 타자", "avg": round(stats.get("avg", league_avg), 3),
                    "ab": int(ab), "estimated": not bool(stats), "lineupConfirmed": lineup["confirmed"],
                })

    result: dict[str, list[dict[str, Any]]] = {"전체": sorted(hitters, key=lambda item: item["probability"], reverse=True)[:3]}
    for position in ("포수", "1루수", "2루수", "3루수", "유격수", "좌익수", "중견수", "우익수", "지명타자"):
        ranked = sorted((item for item in hitters if item["position"] == position), key=lambda item: item["probability"], reverse=True)[:3]
        if ranked:
            result[position] = ranked
    return result, all_confirmed


@dataclass
class CacheEntry:
    created: float
    value: dict[str, Any]


_cache: dict[str, CacheEntry] = {}
_cache_lock = threading.Lock()


def analyze(date: str, force: bool = False) -> dict[str, Any]:
    compact_date = date.replace("-", "")
    datetime.strptime(compact_date, "%Y%m%d")
    with _cache_lock:
        cached = _cache.get(compact_date)
        if cached and not force and time.time() - cached.created < 600:
            return cached.value

    games = fetch_games(date)
    if not games:
        result = {"date": date, "games": [], "hitters": {}, "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"), "source": "KBO 공식 홈페이지", "methodVersion": "stats-v1"}
    else:
        team_stats = fetch_team_stats()
        relevant_team_ids = list(dict.fromkeys([code for game in games for code in (game["AWAY_ID"], game["HOME_ID"])]))
        with ThreadPoolExecutor(max_workers=min(4, len(games))) as executor:
            lineup_results = list(executor.map(fetch_lineup, games))
        lineups = {game["G_ID"]: lineup for game, lineup in zip(games, lineup_results)}
        hitter_stats = fetch_hitter_stats(relevant_team_ids)
        game_predictions = [_game_prediction(game, team_stats) for game in games]
        hitter_predictions, all_confirmed = _hitter_predictions(games, lineups, hitter_stats, team_stats)
        result = {
            "date": date, "games": game_predictions, "hitters": hitter_predictions,
            "lineupStatus": "confirmed" if all_confirmed else "projected",
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "KBO 공식 홈페이지", "methodVersion": "stats-v1",
            "disclaimer": "공식 기록을 사용한 설명형 통계 추정치이며, 학습·백테스트된 예측 모델의 결과가 아닙니다.",
        }

    with _cache_lock:
        _cache[compact_date] = CacheEntry(time.time(), result)
    return result
