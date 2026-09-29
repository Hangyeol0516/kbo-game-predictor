"""KBO 공식 페이지를 읽어 당일 경기의 통계 기반 추정치를 만든다.

공식 공개 페이지의 데이터만 사용하며 결과는 학습된 예측 모델이 아니라
설명 가능한 휴리스틱 추정치다. 외부 요청은 메모리에서 10분간 캐시한다.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import statistics
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


BASE_URL = "https://www.koreabaseball.com"
KST = timezone(timedelta(hours=9))
USER_AGENT = "PLAYBALL/0.1 (+personal KBO analysis prototype)"
BASE_MODEL_VERSION = "stats-v5-context-value"
TEAM_HITTER_1 = f"{BASE_URL}/Record/Team/Hitter/Basic1.aspx"
TEAM_HITTER_2 = f"{BASE_URL}/Record/Team/Hitter/Basic2.aspx"
TEAM_PITCHER_1 = f"{BASE_URL}/Record/Team/Pitcher/Basic1.aspx"
PLAYER_HITTER_1 = f"{BASE_URL}/Record/Player/HitterBasic/Basic1.aspx"
PLAYER_PITCHER_1 = f"{BASE_URL}/Record/Player/PitcherBasic/Basic1.aspx"
PLAYER_HITTER_SITUATION = f"{BASE_URL}/Record/Player/HitterBasic/Situation.aspx"
PLAYER_REGISTER_ALL = f"{BASE_URL}/Player/RegisterAll.aspx"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/baseball_kbo/odds"

TEAM_CODES = {
    "KT": "KT", "SS": "삼성", "LG": "LG", "HT": "KIA", "OB": "두산",
    "SK": "SSG", "HH": "한화", "LT": "롯데", "NC": "NC", "WO": "키움",
}

ODDS_TEAM_NAMES = {
    "lg twins": "LG", "doosan bears": "두산", "hanwha eagles": "한화",
    "samsung lions": "삼성", "kia tigers": "KIA", "lotte giants": "롯데",
    "ssg landers": "SSG", "kt wiz": "KT", "nc dinos": "NC", "kiwoom heroes": "키움",
    "lg 트윈스": "LG", "두산 베어스": "두산", "한화 이글스": "한화",
    "삼성 라이온즈": "삼성", "kia 타이거즈": "KIA", "롯데 자이언츠": "롯데",
    "ssg 랜더스": "SSG", "kt 위즈": "KT", "nc 다이노스": "NC", "키움 히어로즈": "키움",
}

# 좌표와 보수적인 득점 환경 사전계수. 실내 구장은 날씨 보정을 적용하지 않는다.
PARKS = {
    "잠실": {"lat": 37.5122, "lon": 127.0719, "runFactor": 0.94, "indoor": False},
    "고척": {"lat": 37.4982, "lon": 126.8671, "runFactor": 0.98, "indoor": True},
    "문학": {"lat": 37.4369, "lon": 126.6933, "runFactor": 1.03, "indoor": False},
    "수원": {"lat": 37.2997, "lon": 127.0097, "runFactor": 1.03, "indoor": False},
    "대전": {"lat": 36.3171, "lon": 127.4291, "runFactor": 1.02, "indoor": False},
    "대구": {"lat": 35.8411, "lon": 128.6812, "runFactor": 1.06, "indoor": False},
    "사직": {"lat": 35.1940, "lon": 129.0616, "runFactor": 0.97, "indoor": False},
    "광주": {"lat": 35.1681, "lon": 126.8891, "runFactor": 1.00, "indoor": False},
    "창원": {"lat": 35.2225, "lon": 128.5822, "runFactor": 0.98, "indoor": False},
    "포항": {"lat": 36.0082, "lon": 129.3594, "runFactor": 1.00, "indoor": False},
    "울산": {"lat": 35.5322, "lon": 129.2656, "runFactor": 1.00, "indoor": False},
}

_odds_lock = threading.Lock()
_odds_fetch_lock = threading.Lock()
_odds_cache: dict[str, Any] = {"created": 0.0, "regions": None, "credential": None, "eventsByDate": {}}
_odds_state: dict[str, Any] = {
    "lastFetch": None, "lastError": None, "creditsRemaining": None,
    "creditsUsed": None, "requestCost": None, "eventCount": 0,
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
        self.selects: dict[str, str] = {}
        self._select_name: str | None = None
        self._first_option: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "input" and attr.get("name"):
            self.inputs[attr["name"]] = attr.get("value") or ""
        elif tag == "select":
            self._select_name = attr.get("name")
            self._first_option = None
        elif tag == "option" and self._select_name:
            value = attr.get("value") or ""
            if self._first_option is None:
                self._first_option = value
            if "selected" in attr:
                self.selects[self._select_name] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._select_name:
            self.selects.setdefault(self._select_name, self._first_option or "")
            self._select_name = None
            self._first_option = None

    @property
    def fields(self) -> dict[str, str]:
        return {**self.inputs, **self.selects}


def _number(value: str, default: float = 0.0) -> float:
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(value, minimum) if minimum is not None else value


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(value, minimum) if minimum is not None else value


def _get_text(url: str, opener=None) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Referer": BASE_URL})
    response = (opener or urlopen).open(request, timeout=15) if opener else urlopen(request, timeout=15)
    return response.read().decode("utf-8")


def _get_json_response(url: str, headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    with urlopen(Request(url, headers=request_headers), timeout=15) as response:
        return json.loads(response.read()), {key.lower(): value for key, value in response.headers.items()}


def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    return _get_json_response(url, headers)[0]


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


def _post_webform(opener, url: str, current_html: str, target: str, updates: dict[str, str]) -> str:
    parser = FormParser()
    parser.feed(current_html)
    fields = parser.fields
    fields["__EVENTTARGET"] = target
    fields["__EVENTARGUMENT"] = ""
    fields.update(updates)
    request = Request(
        url, data=urlencode(fields).encode(),
        headers={"User-Agent": USER_AGENT, "Referer": url, "Content-Type": "application/x-www-form-urlencoded"},
    )
    return opener.open(request, timeout=15).read().decode("utf-8")


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


def _parse_registered_players(rows: list[list[str]]) -> dict[str, set[str]]:
    rosters: dict[str, set[str]] = {team: set() for team in TEAM_CODES.values()}
    for row in rows:
        if len(row) != 7:
            continue
        team = next((name for name in rosters if re.fullmatch(rf"{re.escape(name)}\d+명", row[0])), None)
        if not team:
            continue
        for cell in row[3:]:
            rosters[team].update(re.findall(r"([A-Za-z·.가-힣]+)\(\d+\)", cell))
    return rosters


def _parse_roster_movements(rows: list[list[str]]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    sections: list[dict[str, list[str]]] = [defaultdict(list), defaultdict(list)]
    section = -1
    for row in rows:
        if row == ["선수", "포지션", "팀"]:
            section += 1
        elif 0 <= section < 2 and len(row) == 3 and row[2] in TEAM_CODES.values():
            sections[section][row[2]].append(row[0])
    return dict(sections[0]), dict(sections[1])


def fetch_roster_status(date: str) -> dict[str, Any]:
    """KBO 공식 1군 엔트리와 당일 등록·말소 내역을 가져온다."""
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    current_html = _get_text(PLAYER_REGISTER_ALL, opener)
    date_field = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$hfSearchDate"
    target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$btnSearch"
    parser = FormParser()
    parser.feed(current_html)
    if parser.fields.get(date_field) != date.replace("-", ""):
        current_html = _post_webform(
            opener, PLAYER_REGISTER_ALL, current_html, target,
            {date_field: date.replace("-", "")},
        )
    table_parser = TableParser()
    table_parser.feed(current_html)
    registered, deregistered = _parse_roster_movements(table_parser.rows)
    return {
        "active": _parse_registered_players(table_parser.rows),
        "registeredToday": registered,
        "deregisteredToday": deregistered,
    }


def _weather_run_factor(temperature: float | None) -> float:
    if temperature is None:
        return 1.0
    return min(max(1 + (temperature - 20) * 0.003, 0.95), 1.05)


def fetch_game_weather(game: dict[str, Any], date: str) -> dict[str, Any]:
    park_name = game.get("S_NM", "")
    park = PARKS.get(park_name, {"runFactor": 1.0, "indoor": False})
    context = {
        "available": False, "indoor": bool(park.get("indoor")), "temperature": None,
        "humidity": None, "precipitationProbability": None, "windSpeed": None,
        "parkRunFactor": float(park["runFactor"]), "weatherRunFactor": 1.0,
    }
    if context["indoor"]:
        context.update({"available": True, "summary": "실내 구장 · 날씨 보정 없음"})
        return context
    if "lat" not in park:
        context["summary"] = "구장 날씨 위치 미등록"
        return context
    query = urlencode({
        "latitude": park["lat"], "longitude": park["lon"],
        "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,wind_speed_10m",
        "timezone": "Asia/Seoul", "start_date": date, "end_date": date,
    })
    try:
        raw = _get_json(f"{OPEN_METEO_URL}?{query}")
        hourly = raw["hourly"]
        target = f"{date}T{str(game.get('G_TM') or '18:00')[:2]}:00"
        index = min(range(len(hourly["time"])), key=lambda i: abs(datetime.fromisoformat(hourly["time"][i]).timestamp() - datetime.fromisoformat(target).timestamp()))
        temperature = hourly["temperature_2m"][index]
        context.update({
            "available": True, "temperature": temperature,
            "humidity": hourly["relative_humidity_2m"][index],
            "precipitationProbability": hourly["precipitation_probability"][index],
            "windSpeed": hourly["wind_speed_10m"][index],
            "weatherRunFactor": _weather_run_factor(temperature),
        })
        context["summary"] = (
            f"{temperature:.0f}℃ · 강수 {context['precipitationProbability']:.0f}% · "
            f"바람 {context['windSpeed']:.0f}km/h"
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        context["summary"] = "날씨 정보 일시 미제공"
    return context


def fetch_game_weathers(games: list[dict[str, Any]], date: str) -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=min(5, len(games))) as executor:
        values = list(executor.map(lambda game: fetch_game_weather(game, date), games))
    return {game["G_ID"]: value for game, value in zip(games, values)}


def _odds_team_name(value: str) -> str | None:
    normalized = " ".join(re.sub(r"[^a-z가-힣 ]", " ", value.lower()).split())
    exact = ODDS_TEAM_NAMES.get(normalized)
    if exact:
        return exact
    return next((team for alias, team in ODDS_TEAM_NAMES.items() if alias in normalized), None)


def odds_provider_status() -> dict[str, Any]:
    with _odds_lock:
        age = time.time() - float(_odds_cache["created"])
        return {
            "configured": bool(os.environ.get("PLAYBALL_ODDS_API_KEY", "").strip()),
            "regions": os.environ.get("PLAYBALL_ODDS_REGIONS", "eu"),
            "cacheAgeSeconds": round(age) if _odds_cache["created"] else None,
            **_odds_state,
        }


def _parse_market_odds(raw: list[dict[str, Any]]) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    results_by_date: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for event in raw:
        away, home = _odds_team_name(event.get("away_team", "")), _odds_team_name(event.get("home_team", ""))
        if not away or not home:
            continue
        commence = event.get("commence_time", "")
        try:
            event_date = datetime.fromisoformat(commence.replace("Z", "+00:00")).astimezone(KST).date().isoformat()
        except ValueError:
            continue
        best: dict[str, dict[str, Any]] = {}
        fair_samples: dict[str, list[float]] = {away: [], home: []}
        last_update = None
        for bookmaker in event.get("bookmakers", []):
            market = next((item for item in bookmaker.get("markets", []) if item.get("key") == "h2h"), None)
            if not market:
                continue
            prices = {}
            for outcome in market.get("outcomes", []):
                team = _odds_team_name(outcome.get("name", ""))
                price = _number(str(outcome.get("price", 0)))
                if team in fair_samples and price > 1:
                    prices[team] = price
                    if price > best.get(team, {}).get("price", 0):
                        best[team] = {
                            "price": price, "bookmaker": bookmaker.get("title") or bookmaker.get("key"),
                            "lastUpdate": bookmaker.get("last_update"),
                        }
            if away in prices and home in prices:
                raw_away, raw_home = 1 / prices[away], 1 / prices[home]
                total = raw_away + raw_home
                fair_samples[away].append(raw_away / total)
                fair_samples[home].append(raw_home / total)
                last_update = max(last_update or "", bookmaker.get("last_update") or "")
        if away in best and home in best and fair_samples[away] and fair_samples[home]:
            results_by_date[event_date][(away, home)] = {
                "teams": {
                    away: {**best[away], "marketProbability": statistics.median(fair_samples[away])},
                    home: {**best[home], "marketProbability": statistics.median(fair_samples[home])},
                },
                "bookmakerCount": min(len(fair_samples[away]), len(fair_samples[home])),
                "lastUpdate": last_update,
                "commenceTime": commence,
            }
    return dict(results_by_date)


def fetch_market_odds(date: str, refresh: bool = False) -> dict[tuple[str, str], dict[str, Any]]:
    """북메이커별 moneyline을 정규화하고 공유 캐시로 API 쿼터를 보호한다.

    평상시에는 컨테이너가 보유한 마지막 전체 응답을 재사용한다. 백그라운드
    수집기가 경기 일정에 맞는 시점에만 ``refresh``를 요청한다.
    """
    api_key = os.environ.get("PLAYBALL_ODDS_API_KEY", "").strip()
    if not api_key:
        return {}
    regions = os.environ.get("PLAYBALL_ODDS_REGIONS", "eu")
    credential = hashlib.sha256(api_key.encode()).hexdigest()[:12]
    with _odds_lock:
        cache_matches = (
            _odds_cache["regions"] == regions
            and _odds_cache["credential"] == credential
            and bool(_odds_cache["created"])
        )
        if cache_matches and not refresh:
            return _odds_cache["eventsByDate"].get(date, {})
        if not refresh:
            return {}

    # 네트워크 호출만 직렬화하고 상태 조회와 헬스체크는 막지 않는다.
    with _odds_fetch_lock:
        with _odds_lock:
            cache_matches = (
                _odds_cache["regions"] == regions
                and _odds_cache["credential"] == credential
                and bool(_odds_cache["created"])
            )
            if cache_matches and not refresh:
                return _odds_cache["eventsByDate"].get(date, {})
        query = urlencode({
            "apiKey": api_key, "regions": regions, "markets": "h2h",
            "oddsFormat": "decimal", "dateFormat": "iso",
        })
        try:
            raw, headers = _get_json_response(f"{ODDS_API_URL}?{query}")
            events_by_date = _parse_market_odds(raw)
            now = datetime.now(KST).isoformat(timespec="seconds")
            with _odds_lock:
                _odds_cache.update({
                    "created": time.time(), "regions": regions,
                    "credential": credential, "eventsByDate": events_by_date,
                })
                _odds_state.update({
                    "lastFetch": now, "lastError": None,
                    "creditsRemaining": _number(headers.get("x-requests-remaining", ""), None),
                    "creditsUsed": _number(headers.get("x-requests-used", ""), None),
                    "requestCost": _number(headers.get("x-requests-last", ""), None),
                    "eventCount": len(raw),
                })
        except Exception as exc:
            error_name = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            with _odds_lock:
                _odds_state.update({"lastError": error_name, "lastFetch": datetime.now(KST).isoformat(timespec="seconds")})
                return {}
        return events_by_date.get(date, {})


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


def fetch_pitcher_hand(game: dict[str, Any], side: str) -> dict[str, str]:
    name_key, team_key = (("T_PIT_P_NM", "AWAY_NM") if side == "away" else ("B_PIT_P_NM", "HOME_NM"))
    name = game.get(name_key, "").strip()
    raw = _post_json("/ws/Controls.asmx/GetSearchPlayer", {"name": name}) if name else {"now": []}
    candidates = [item for item in raw.get("now", []) if item.get("P_NM") == name and item.get("T_NM") == game[team_key]]
    pitcher_type = candidates[0].get("P_TYPE", "") if candidates else ""
    if pitcher_type.startswith("좌"):
        split, label = "LO", "좌투"
    elif "언" in pitcher_type:
        split, label = "LU,RU", "언더"
    else:
        split, label = "RO", "우투"
    return {"team": game[team_key], "name": name, "type": pitcher_type, "split": split, "label": label}


def fetch_pitcher_hands(games: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    jobs = [(game, side) for game in games for side in ("away", "home")]
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as executor:
        values = list(executor.map(lambda job: fetch_pitcher_hand(*job), jobs))
    return {value["team"]: value for value in values}


def fetch_matchup_hitter_stats(team_splits: dict[str, str]) -> dict[tuple[str, str, str], dict[str, float]]:
    """상대 선발 유형(좌/우/언더)별 타자의 시즌 스플릿을 팀 단위로 가져온다."""
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    current_html = _get_text(PLAYER_HITTER_SITUATION, opener)
    situation_target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlSituation$ddlSituation"
    detail_target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlSituationDetail$ddlSituationDetail"
    team_target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlTeam$ddlTeam"
    current_html = _post_webform(opener, PLAYER_HITTER_SITUATION, current_html, situation_target, {situation_target: "41"})
    result: dict[tuple[str, str, str], dict[str, float]] = {}
    for split in ("LO", "RO", "LU,RU"):
        teams = [team_id for team_id, requested_split in team_splits.items() if requested_split == split]
        if not teams:
            continue
        current_html = _post_webform(
            opener, PLAYER_HITTER_SITUATION, current_html, detail_target,
            {situation_target: "41", detail_target: split, team_target: ""},
        )
        for team_id in teams:
            current_html = _post_webform(
                opener, PLAYER_HITTER_SITUATION, current_html, team_target,
                {situation_target: "41", detail_target: split, team_target: team_id},
            )
            parser = TableParser(("tData01",))
            parser.feed(current_html)
            for row in parser.rows:
                if len(row) >= 14 and row[0] != "순위":
                    name, team = row[1].lstrip("* "), row[2]
                    result[(team, name, split)] = {
                        "avg": _number(row[3]), "ab": _number(row[4]), "hits": _number(row[5]),
                    }
    return result


def fetch_boxscore_pitchers(game: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """종료 경기 박스스코어에서 팀별 투수와 투구 수를 가져온다."""
    raw = _post_json(
        "/ws/Schedule.asmx/GetBoxScoreScroll",
        {
            "leId": str(game["LE_ID"]), "srId": str(game["SR_ID"]),
            "seasonId": str(game["SEASON_ID"]), "gameId": game["G_ID"],
        },
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for team, item in zip((game["AWAY_NM"], game["HOME_NM"]), raw.get("arrPitcher", [])):
        grid = json.loads(item["table"])
        pitchers = []
        for row in grid.get("rows", []):
            cells = [html.unescape(str(cell.get("Text", ""))).replace("&nbsp;", "").strip() for cell in row.get("row", [])]
            if len(cells) >= 9:
                pitchers.append({
                    "name": cells[0], "starter": cells[1] == "선발",
                    "pitches": int(_number(cells[8])),
                })
        result[team] = pitchers
    return result


def fetch_recent_bullpen(target_date: str, team_names: list[str]) -> dict[str, dict[str, Any]]:
    """경기일 직전 3일의 불펜 투구 수와 연투 인원을 계산한다."""
    base_date = datetime.strptime(target_date, "%Y-%m-%d")
    recent_games: list[dict[str, Any]] = []
    for days_ago in range(1, 4):
        game_date = (base_date - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        for game in fetch_games(game_date):
            if bool(game.get("GAME_RESULT_CK")) and (game["AWAY_NM"] in team_names or game["HOME_NM"] in team_names):
                game["_analysis_date"] = game_date
                recent_games.append(game)

    workloads = {
        team: {"pitches": 0, "appearances": 0, "relievers": set(), "appearance_dates": defaultdict(set)}
        for team in team_names
    }
    if recent_games:
        with ThreadPoolExecutor(max_workers=min(6, len(recent_games))) as executor:
            boxscores = list(executor.map(fetch_boxscore_pitchers, recent_games))
        for game, boxscore in zip(recent_games, boxscores):
            for team, pitchers in boxscore.items():
                if team not in workloads:
                    continue
                for pitcher in pitchers:
                    if pitcher["starter"]:
                        continue
                    workloads[team]["pitches"] += pitcher["pitches"]
                    workloads[team]["appearances"] += 1
                    workloads[team]["relievers"].add(pitcher["name"])
                    workloads[team]["appearance_dates"][pitcher["name"]].add(game["_analysis_date"])

    yesterday = (base_date - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (base_date - timedelta(days=2)).strftime("%Y-%m-%d")
    result = {}
    for team, workload in workloads.items():
        back_to_back = sum(
            yesterday in dates and two_days_ago in dates
            for dates in workload["appearance_dates"].values()
        )
        result[team] = {
            "pitches": workload["pitches"], "appearances": workload["appearances"],
            "relievers": len(workload["relievers"]), "backToBack": back_to_back,
            "fatigueScore": workload["pitches"] + 12 * back_to_back,
        }
    return result


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


def _fetch_team_filtered_rows(url: str, team_ids: list[str]) -> list[list[str]]:
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    current_html = _get_text(url, opener)
    target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlTeam$ddlTeam"
    rows: list[list[str]] = []
    for team_id in team_ids:
        current_html = _post_webform(opener, url, current_html, target, {target: team_id})
        parser = TableParser(("tData01",))
        parser.feed(current_html)
        rows.extend(row for row in parser.rows if row and row[0] != "순위")
    return rows


def fetch_hitter_stats(team_ids: list[str]) -> dict[tuple[str, str], dict[str, float]]:
    """WebForms 팀 필터를 순차 적용해 당일 참가 팀의 모든 타자를 가져온다."""
    result: dict[tuple[str, str], dict[str, float]] = {}
    for row in _fetch_team_filtered_rows(PLAYER_HITTER_1, team_ids):
        if len(row) >= 16:
            name, team = row[1].lstrip("* "), row[2]
            result[(team, name)] = {
                "avg": _number(row[3]), "games": _number(row[4]), "pa": _number(row[5]),
                "ab": _number(row[6]), "hits": _number(row[8]), "hr": _number(row[11]),
            }
    return result


def fetch_pitcher_stats(team_ids: list[str]) -> dict[tuple[str, str], dict[str, float]]:
    """당일 참가 팀 투수의 시즌 ERA·WHIP·이닝을 가져온다."""
    result: dict[tuple[str, str], dict[str, float]] = {}
    for row in _fetch_team_filtered_rows(PLAYER_PITCHER_1, team_ids):
        if len(row) >= 19:
            name, team = row[1].lstrip("* "), row[2]
            result[(team, name)] = {
                "era": _number(row[3]), "games": _number(row[4]), "wins": _number(row[5]),
                "losses": _number(row[6]), "innings": row[10], "whip": _number(row[18]),
            }
    return result


def _mean_std(values: list[float]) -> tuple[float, float]:
    return statistics.mean(values), max(statistics.pstdev(values), 0.001)


def load_calibrator() -> dict[str, Any] | None:
    path = os.environ.get("PLAYBALL_CALIBRATION_PATH", "data/calibration.json")
    try:
        with open(path, encoding="utf-8") as stream:
            calibrator = json.load(stream)
        if (
            calibrator.get("enabled") and calibrator.get("kind") == "platt"
            and calibrator.get("baseModelVersion") == BASE_MODEL_VERSION
        ):
            return calibrator
    except (OSError, ValueError, TypeError):
        pass
    return None


def calibrate_probability(probability: float, calibrator: dict[str, Any] | None) -> float:
    if not calibrator:
        return probability
    bounded = min(max(probability, 0.001), 0.999)
    logit = math.log(bounded / (1 - bounded))
    score = max(min(calibrator["slope"] * logit + calibrator["intercept"], 30), -30)
    calibrated = 1 / (1 + math.exp(-score))
    return min(max(calibrated, 0.20), 0.80)


def active_model_version() -> str:
    return f"{BASE_MODEL_VERSION}+platt-v1" if load_calibrator() else BASE_MODEL_VERSION


def _availability_context(
    team: str, hitter_stats: dict[tuple[str, str], dict[str, float]], roster_status: dict[str, Any],
) -> dict[str, Any]:
    active = roster_status.get("active", {}).get(team, set())
    candidates = sorted(
        ((name, stats) for (player_team, name), stats in hitter_stats.items() if player_team == team and stats.get("pa", 0) >= 50),
        key=lambda item: item[1].get("pa", 0), reverse=True,
    )[:6]
    if not active or not candidates:
        return {"available": False, "coreAbsent": [], "penalty": 0.0}
    max_pa = max(stats["pa"] for _, stats in candidates)
    absent = [
        {"name": name, "pa": int(stats["pa"]), "avg": round(stats.get("avg", 0), 3)}
        for name, stats in candidates if name not in active
    ]
    penalty = min(sum(0.012 + 0.018 * item["pa"] / max_pa for item in absent), 0.09)
    return {
        "available": True, "coreAbsent": absent, "penalty": penalty,
        "registeredToday": roster_status.get("registeredToday", {}).get(team, []),
        "deregisteredToday": roster_status.get("deregisteredToday", {}).get(team, []),
    }


def evaluate_value_bet(
    away: str, home: str, away_probability: float, home_probability: float,
    market: dict[str, Any] | None,
) -> dict[str, Any]:
    if not market:
        return {"available": False, "recommendation": False, "reason": "배당 미연결"}
    model = {away: away_probability, home: home_probability}
    teams = market["teams"]
    favorite = max((away, home), key=lambda team: teams[team]["marketProbability"])
    underdog = home if favorite == away else away

    def metrics(team: str) -> dict[str, Any]:
        price = teams[team]["price"]
        market_probability = teams[team]["marketProbability"]
        expected_return = model[team] * price - 1
        return {
            "modelProbabilityValue": round(model[team], 6),
            "marketProbabilityValue": round(market_probability, 6),
            "expectedReturnValue": round(expected_return, 6),
            "team": team, "modelProbability": round(model[team] * 100, 1),
            "marketProbability": round(market_probability * 100, 1),
            "edgePp": round((model[team] - market_probability) * 100, 1),
            "odds": round(price, 2), "bookmaker": teams[team]["bookmaker"],
            "expectedReturnPct": round(expected_return * 100, 1),
        }

    favorite_metrics, underdog_metrics = metrics(favorite), metrics(underdog)
    advantage = underdog_metrics["expectedReturnValue"] - favorite_metrics["expectedReturnValue"]
    min_ev = _env_float("PLAYBALL_UPSET_MIN_EV", 0.08)
    min_edge = _env_float("PLAYBALL_UPSET_MIN_EDGE", 0.05)
    min_advantage = _env_float("PLAYBALL_UPSET_MIN_ADVANTAGE", 0.10)
    min_bookmakers = _env_int("PLAYBALL_ODDS_MIN_BOOKMAKERS", 2, 1)
    max_age_minutes = _env_float("PLAYBALL_ODDS_MAX_AGE_MINUTES", 60, 1)
    cutoff_minutes = _env_float("PLAYBALL_ODDS_CLOSE_BEFORE_MINUTES", 10, 0)
    now = datetime.now(timezone.utc)

    def parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    price_updates = [parse_timestamp(teams[team].get("lastUpdate")) for team in (away, home)]
    commence_at = parse_timestamp(market.get("commenceTime"))
    age_minutes = (
        max(max((now - updated_at).total_seconds() / 60, 0) for updated_at in price_updates)
        if all(price_updates) else None
    )
    market_fresh = age_minutes is not None and age_minutes <= max_age_minutes
    betting_open = commence_at is not None and now < commence_at - timedelta(minutes=cutoff_minutes)
    bookmaker_count = int(market.get("bookmakerCount", 0))
    recommendation = (
        underdog_metrics["expectedReturnValue"] >= min_ev
        and underdog_metrics["modelProbabilityValue"] - underdog_metrics["marketProbabilityValue"] >= min_edge
        and advantage >= min_advantage
        and bookmaker_count >= min_bookmakers
        and market_fresh
        and betting_open
    )
    return {
        "available": True, "recommendation": recommendation,
        "favorite": favorite_metrics, "underdog": underdog_metrics,
        "returnAdvantageValue": round(advantage, 6),
        "returnAdvantagePp": round(advantage * 100, 1),
        "bookmakerCount": bookmaker_count,
        "lastUpdate": market.get("lastUpdate"),
        "commenceTime": market.get("commenceTime"),
        "quality": {
            "marketFresh": market_fresh, "ageMinutes": round(age_minutes, 1) if age_minutes is not None else None,
            "bettingOpen": betting_open, "enoughBookmakers": bookmaker_count >= min_bookmakers,
        },
        "criterion": {
            "minimumEvPct": min_ev * 100, "minimumEdgePp": min_edge * 100,
            "minimumAdvantagePp": min_advantage * 100,
            "minimumBookmakers": min_bookmakers, "maximumAgeMinutes": max_age_minutes,
            "closeBeforeMinutes": cutoff_minutes,
        },
    }


def _game_prediction(
    game: dict[str, Any], team_stats: dict[str, dict[str, float]],
    pitcher_stats: dict[tuple[str, str], dict[str, float]], bullpen_stats: dict[str, dict[str, Any]],
    hitter_stats: dict[tuple[str, str], dict[str, float]], roster_status: dict[str, Any],
    weather: dict[str, Any], market_odds: dict[str, Any] | None,
    calibrator: dict[str, Any] | None, lineup_confirmed: bool,
) -> dict[str, Any]:
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

    away_pitcher_name = game.get("T_PIT_P_NM", "").strip()
    home_pitcher_name = game.get("B_PIT_P_NM", "").strip()
    away_starter = pitcher_stats.get((away_name, away_pitcher_name), {})
    home_starter = pitcher_stats.get((home_name, home_pitcher_name), {})
    whip_mean, whip_std = _mean_std([team["whip"] for team in team_stats.values()])

    def starter_rating(starter: dict[str, float], fallback: dict[str, float]) -> float:
        era = starter.get("era", fallback["era"])
        whip = starter.get("whip", fallback["whip"])
        return 0.7 * ((e_mean - era) / e_std) + 0.3 * ((whip_mean - whip) / whip_std)

    away_starter_rating = starter_rating(away_starter, away)
    home_starter_rating = starter_rating(home_starter, home)
    fatigue_mean, fatigue_std = _mean_std([float(team["fatigueScore"]) for team in bullpen_stats.values()])
    away_fatigue = (bullpen_stats[away_name]["fatigueScore"] - fatigue_mean) / fatigue_std
    home_fatigue = (bullpen_stats[home_name]["fatigueScore"] - fatigue_mean) / fatigue_std
    away_availability = _availability_context(away_name, hitter_stats, roster_status)
    home_availability = _availability_context(home_name, hitter_stats, roster_status)
    run_environment = weather["parkRunFactor"] * weather["weatherRunFactor"]
    home_logit = (
        0.13 + 0.50 * run_environment * (rating(home) - rating(away))
        + 0.20 * (home_starter_rating - away_starter_rating)
        + 0.10 * (away_fatigue - home_fatigue)
        + away_availability["penalty"] - home_availability["penalty"]
    )
    home_probability = 1 / (1 + math.exp(-home_logit))
    # 보정 전 설명형 추정치의 과신을 막는다.
    home_probability = min(max(home_probability, 0.28), 0.72)
    raw_home_probability = home_probability
    home_probability = calibrate_probability(home_probability, calibrator)
    home_percent = round(home_probability * 100)
    away_percent = 100 - home_percent
    pick = home_name if home_percent >= away_percent else away_name
    margin = abs(home_percent - away_percent)
    confidence = "높음" if margin >= 20 else "보통" if margin >= 10 else "접전"
    if (weather.get("precipitationProbability") or 0) >= 60:
        confidence = "날씨 변수"
    better_offense = home_name if home["runs_per_game"] > away["runs_per_game"] else away_name
    better_pitching = home_name if home["era"] < away["era"] else away_name

    value_bet = evaluate_value_bet(
        away_name, home_name, away_probability=1 - home_probability,
        home_probability=home_probability, market=market_odds,
    )
    away_absent = ", ".join(item["name"] for item in away_availability["coreAbsent"]) or "없음"
    home_absent = ", ".join(item["name"] for item in home_availability["coreAbsent"]) or "없음"
    return {
        "id": game["G_ID"], "time": game["G_TM"], "park": game["S_NM"],
        "away": away_name, "home": home_name,
        "awayPitcher": away_pitcher_name or "미정", "homePitcher": home_pitcher_name or "미정",
        "awayProb": away_percent, "homeProb": home_percent,
        "awayProbability": round(1 - home_probability, 6),
        "homeProbability": round(home_probability, 6),
        "pick": pick, "confidence": confidence,
        "lineupConfirmed": lineup_confirmed,
        "snapshotEligible": str(game.get("GAME_STATE_SC")) == "1" and not bool(game.get("GAME_RESULT_CK")),
        "weather": weather, "availability": {"away": away_availability, "home": home_availability},
        "valueBet": value_bet,
        "reasons": [
            f"{better_offense} 시즌 득점력 우위 ({team_stats[better_offense]['runs_per_game']:.2f}점/경기)",
            f"{better_pitching} 팀 평균자책점 우위 ({team_stats[better_pitching]['era']:.2f})",
            f"선발 ERA: {away_name} {away_starter.get('era', away['era']):.2f} · {home_name} {home_starter.get('era', home['era']):.2f}",
            f"최근 3일 불펜 투구: {away_name} {bullpen_stats[away_name]['pitches']}구 · {home_name} {bullpen_stats[home_name]['pitches']}구",
            f"1군 엔트리 이탈 핵심 타자(부상 확정 아님): {away_name} {away_absent} · {home_name} {home_absent}",
            f"{game['S_NM']} 득점환경 계수 {run_environment:.2f} · {weather['summary']}",
            f"{away_name} 출루율 {away['obp']:.3f} · {home_name} 출루율 {home['obp']:.3f}",
            "홈 경기 기본 보정 3.2% 적용",
        ],
        "metrics": {
            "awayRpg": round(away["runs_per_game"], 2), "homeRpg": round(home["runs_per_game"], 2),
            "awayEra": away["era"], "homeEra": home["era"],
            "awayStarterEra": away_starter.get("era"), "homeStarterEra": home_starter.get("era"),
            "awayBullpenPitches3d": bullpen_stats[away_name]["pitches"],
            "homeBullpenPitches3d": bullpen_stats[home_name]["pitches"],
            "awayBackToBackRelievers": bullpen_stats[away_name]["backToBack"],
            "homeBackToBackRelievers": bullpen_stats[home_name]["backToBack"],
            "awayAvailabilityPenalty": round(away_availability["penalty"], 4),
            "homeAvailabilityPenalty": round(home_availability["penalty"], 4),
            "runEnvironmentFactor": round(run_environment, 4),
            "rawHomeProbability": round(raw_home_probability, 4),
            "calibrated": bool(calibrator),
        },
    }


def _hitter_predictions(
    games: list[dict[str, Any]], lineups: dict[str, dict[str, Any]],
    hitter_stats: dict[tuple[str, str], dict[str, float]], team_stats: dict[str, dict[str, float]],
    pitcher_stats: dict[tuple[str, str], dict[str, float]],
    pitcher_hands: dict[str, dict[str, str]],
    matchup_hitter_stats: dict[tuple[str, str, str], dict[str, float]],
    roster_status: dict[str, Any],
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
                active = roster_status.get("active", {}).get(player["team"], set())
                if not lineup["confirmed"] and active and player["name"] not in active:
                    continue
                stats = hitter_stats.get((player["team"], player["name"]), {})
                ab, hits = stats.get("ab", 0), stats.get("hits", 0)
                pa_average = (hits + 60 * league_avg) / (ab + 60)
                hand = pitcher_hands.get(opponent, {"split": "RO", "label": "우투"})
                split_stats = matchup_hitter_stats.get((player["team"], player["name"], hand["split"]), {})
                split_ab, split_hits = split_stats.get("ab", 0), split_stats.get("hits", 0)
                matchup_average = (split_hits + 35 * pa_average) / (split_ab + 35) if split_ab else pa_average
                opponent_pitcher_name = game.get("B_PIT_P_NM" if side == "away" else "T_PIT_P_NM", "").strip()
                opponent_pitcher = pitcher_stats.get((opponent, opponent_pitcher_name), {})
                opponent_era = opponent_pitcher.get("era", team_stats[opponent]["era"])
                pitcher_factor = 1 + 0.055 * (opponent_era - league_era) / league_era_std
                pitcher_factor = min(max(pitcher_factor, 0.91), 1.09)
                per_ab = min(max(matchup_average * pitcher_factor, 0.12), 0.42)
                expected_ab = max(3.55, 4.45 - 0.10 * (player["order"] - 1))
                probability = round((1 - (1 - per_ab) ** expected_ab) * 100)
                position = POSITION_GROUPS.get(player["position"])
                if not position:
                    continue
                hitters.append({
                    "name": player["name"], "team": player["team"], "position": position,
                    "rawPosition": player["position"], "probability": probability,
                    "opponent": f"vs {opponent}", "pitcher": f"상대 선발 {opponent_pitcher_name or '미정'}",
                    "order": f"{player['order']}번 타자", "avg": round(stats.get("avg", league_avg), 3),
                    "ab": int(ab), "matchupAvg": round(split_stats.get("avg", matchup_average), 3),
                    "matchupAb": int(split_ab), "pitcherHand": hand["label"],
                    "estimated": not bool(stats), "lineupConfirmed": lineup["confirmed"],
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


_cache: OrderedDict[str, CacheEntry] = OrderedDict()
_cache_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def _analyze_uncached(date: str, refresh_odds: bool = False) -> dict[str, Any]:
    games = fetch_games(date)
    calibrator = load_calibrator()
    method_version = f"{BASE_MODEL_VERSION}+platt-v1" if calibrator else BASE_MODEL_VERSION
    odds_state = odds_provider_status()
    if not games:
        value_status = "not-configured" if not odds_state["configured"] else "no-market"
        result = {
            "date": date, "games": [], "hitters": {}, "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
            "source": "KBO 공식 홈페이지", "sources": ["KBO 공식 홈페이지", "Open-Meteo"],
            "methodVersion": method_version, "valueBetStatus": value_status, "odds": odds_state,
            "valueBets": [], "snapshotEligible": False,
        }
    else:
        team_stats = fetch_team_stats()
        relevant_team_ids = list(dict.fromkeys([code for game in games for code in (game["AWAY_ID"], game["HOME_ID"])]))
        pitcher_hands = fetch_pitcher_hands(games)
        team_id_by_name = {name: code for code, name in TEAM_CODES.items()}
        team_splits: dict[str, str] = {}
        for game in games:
            team_splits[team_id_by_name[game["AWAY_NM"]]] = pitcher_hands[game["HOME_NM"]]["split"]
            team_splits[team_id_by_name[game["HOME_NM"]]] = pitcher_hands[game["AWAY_NM"]]["split"]
        with ThreadPoolExecutor(max_workers=max(4, min(10, len(games) + 6))) as executor:
            lineup_futures = [executor.submit(fetch_lineup, game) for game in games]
            hitter_future = executor.submit(fetch_hitter_stats, relevant_team_ids)
            pitcher_future = executor.submit(fetch_pitcher_stats, relevant_team_ids)
            bullpen_future = executor.submit(fetch_recent_bullpen, date, [TEAM_CODES[team_id] for team_id in relevant_team_ids])
            matchup_future = executor.submit(fetch_matchup_hitter_stats, team_splits)
            roster_future = executor.submit(fetch_roster_status, date)
            weather_future = executor.submit(fetch_game_weathers, games, date)
            odds_future = executor.submit(fetch_market_odds, date, refresh_odds)
            lineup_results = [future.result() for future in lineup_futures]
            hitter_stats = hitter_future.result()
            pitcher_stats = pitcher_future.result()
            bullpen_stats = bullpen_future.result()
            matchup_hitter_stats = matchup_future.result()
            try:
                roster_status = roster_future.result()
            except Exception:
                roster_status = {"active": {}, "registeredToday": {}, "deregisteredToday": {}}
            try:
                weather_by_game = weather_future.result()
            except Exception:
                weather_by_game = {
                    game["G_ID"]: {
                        "available": False, "indoor": False, "temperature": None, "humidity": None,
                        "precipitationProbability": None, "windSpeed": None, "parkRunFactor": 1.0,
                        "weatherRunFactor": 1.0, "summary": "날씨 정보 일시 미제공",
                    } for game in games
                }
            try:
                market_odds = odds_future.result()
            except Exception:
                market_odds = {}
        odds_state = odds_provider_status()
        lineups = {game["G_ID"]: lineup for game, lineup in zip(games, lineup_results)}
        game_predictions = [
            _game_prediction(
                game, team_stats, pitcher_stats, bullpen_stats, hitter_stats, roster_status,
                weather_by_game[game["G_ID"]], market_odds.get((game["AWAY_NM"], game["HOME_NM"])),
                calibrator, lineups[game["G_ID"]]["confirmed"],
            )
            for game in games
        ]
        hitter_predictions, all_confirmed = _hitter_predictions(
            games, lineups, hitter_stats, team_stats, pitcher_stats, pitcher_hands, matchup_hitter_stats, roster_status,
        )
        value_available = any(game["valueBet"]["available"] for game in game_predictions)
        if value_available:
            value_status = "connected"
        elif not odds_state["configured"]:
            value_status = "not-configured"
        elif odds_state["lastError"]:
            value_status = "provider-error"
        else:
            value_status = "no-market"
        result = {
            "date": date, "games": game_predictions, "hitters": hitter_predictions,
            "lineupStatus": "confirmed" if all_confirmed else "projected",
            "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
            "source": "KBO 공식 홈페이지", "sources": ["KBO 공식 홈페이지", "Open-Meteo"] + (["The Odds API"] if odds_state["configured"] else []),
            "methodVersion": method_version,
            "valueBetStatus": value_status, "odds": odds_state,
            "valueBets": [game["valueBet"] | {"gameId": game["id"], "away": game["away"], "home": game["home"]} for game in game_predictions if game["valueBet"].get("recommendation")],
            "snapshotEligible": any(game["snapshotEligible"] for game in game_predictions),
            "disclaimer": "공식 기록을 사용한 설명형 통계 추정치이며, 배당 기대수익은 수익을 보장하지 않습니다.",
        }

    return result


def analyze(date: str, force: bool = False, refresh_odds: bool = False) -> dict[str, Any]:
    """날짜별 분석을 단일 실행하고 완료 결과만 제한된 메모리 캐시에 저장한다."""
    compact_date = date.replace("-", "")
    datetime.strptime(compact_date, "%Y%m%d")
    cache_ttl = _env_int("PLAYBALL_ANALYSIS_CACHE_SECONDS", 600, 30)
    cache_limit = _env_int("PLAYBALL_ANALYSIS_CACHE_ENTRIES", 32, 1)

    with _cache_lock:
        cached = _cache.get(compact_date)
        if cached and not force and not refresh_odds and time.time() - cached.created < cache_ttl:
            _cache.move_to_end(compact_date)
            return cached.value
        event = _inflight.get(compact_date)
        if event is None:
            event = threading.Event()
            _inflight[compact_date] = event
            owner = True
        else:
            owner = False

    if not owner:
        if not event.wait(timeout=120):
            raise TimeoutError("같은 날짜의 분석이 아직 완료되지 않았습니다.")
        with _cache_lock:
            cached = _cache.get(compact_date)
            if cached:
                _cache.move_to_end(compact_date)
                return cached.value
        raise RuntimeError("같은 날짜의 분석이 완료되지 않았습니다.")

    result: dict[str, Any] | None = None
    try:
        result = _analyze_uncached(date, refresh_odds=True) if refresh_odds else _analyze_uncached(date)
        return result
    finally:
        with _cache_lock:
            if result is not None:
                _cache[compact_date] = CacheEntry(time.time(), result)
                _cache.move_to_end(compact_date)
                while len(_cache) > cache_limit:
                    _cache.popitem(last=False)
            _inflight.pop(compact_date, None)
            event.set()
