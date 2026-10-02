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
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from artifact_io import atomic_write_json


BASE_URL = "https://www.koreabaseball.com"
KST = timezone(timedelta(hours=9))
USER_AGENT = "PLAYBALL/0.1 (+personal KBO analysis prototype)"
BASE_MODEL_VERSION = "stats-v7-game-context"
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
    "cacheRestored": False, "persistenceError": None,
}
_odds_restore_key = None

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


class DataContractError(ValueError):
    """공식 기록의 구조나 값이 예상 계약을 만족하지 않을 때 발생한다."""


def _record_entries(parser: TableParser, fields: tuple[str, ...], label: str) -> list[dict[str, str]]:
    header = next((row for row in parser.rows if row and row[0] == "순위"), None)
    if header is None or any(header.count(field) != 1 for field in fields):
        raise DataContractError(f"{label}: 기록 컬럼이 변경되었거나 누락됐습니다.")
    indices = {field: header.index(field) for field in fields}
    records = []
    for row in parser.rows:
        if not row or row[0] in ("순위", "합계") or "합계" in row[:2]:
            continue
        if len(row) != len(header):
            raise DataContractError(f"{label}: 기록 행의 컬럼 수가 맞지 않습니다.")
        records.append({field: row[index] for field, index in indices.items()})
    if not records:
        raise DataContractError(f"{label}: 사용할 수 있는 기록이 없습니다.")
    return records


def _record_table(url: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
    parser = TableParser()
    parser.feed(_get_text(url))
    return _record_entries(parser, fields, "팀 기록")


def _record_number(record: dict[str, str], field: str, upper: float | None = None) -> float:
    try:
        value = float(record[field].replace(",", ""))
    except (KeyError, ValueError):
        raise DataContractError(f"기록 {field}: 숫자 값이 누락되거나 잘못됐습니다.") from None
    if not math.isfinite(value) or value < 0 or (upper is not None and value > upper):
        raise DataContractError(f"기록 {field}: 허용 범위를 벗어났습니다.")
    return value


def fetch_games(date: str) -> list[dict[str, Any]]:
    raw = _post_json(
        "/ws/Main.asmx/GetKboGameList",
        {"leId": "1", "srId": "0,1,3,4,5,6,7,8,9", "date": date.replace("-", "")},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("game"), list):
        raise DataContractError("일정: 응답 구조가 변경됐습니다.")
    games = []
    seen = set()
    for game in raw["game"]:
        if not isinstance(game, dict):
            raise DataContractError("일정: 경기 행의 형식이 잘못됐습니다.")
        league_id = _record_number({"LE_ID": str(game.get("LE_ID", ""))}, "LE_ID")
        if league_id != 1:
            continue
        if not game.get("G_ID") or game["G_ID"] in seen or any(
            game.get(field) not in TEAM_CODES.values() for field in ("AWAY_NM", "HOME_NM")
        ):
            raise DataContractError("일정: 경기 ID 또는 팀 정보가 잘못됐습니다.")
        seen.add(game["G_ID"])
        result_flag = game.get("GAME_RESULT_CK")
        if result_flag not in (False, True, 0, 1, "0", "1"):
            raise DataContractError("일정: 경기 종료 상태가 잘못됐습니다.")
        game["GAME_RESULT_CK"] = result_flag in (True, 1, "1")
        games.append(game)
    return games


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
        key = os.environ.get("PLAYBALL_ODDS_API_KEY", "").strip()
        if key:
            _restore_odds_cache(hashlib.sha256(key.encode()).hexdigest()[:12],
                                os.environ.get("PLAYBALL_ODDS_REGIONS", "eu"))
        age = time.time() - float(_odds_cache["created"])
        return {
            "configured": bool(os.environ.get("PLAYBALL_ODDS_API_KEY", "").strip()),
            "regions": os.environ.get("PLAYBALL_ODDS_REGIONS", "eu"),
            "cacheAgeSeconds": round(age) if _odds_cache["created"] else None,
            **_odds_state,
        }


def _odds_cache_path() -> Path | None:
    configured = os.environ.get("PLAYBALL_ODDS_CACHE_PATH")
    if configured == "":
        return None
    database = Path(os.environ.get("PLAYBALL_DB_PATH", "data/playball.db")).expanduser().resolve()
    path = Path(configured).expanduser().resolve() if configured else database.with_name("odds-cache.json")
    calibration = Path(os.environ.get("PLAYBALL_CALIBRATION_PATH", "data/calibration.json")).expanduser().resolve()
    if path.suffix != ".json" or path in (database, calibration):
        raise ValueError("배당 캐시 경로는 DB·보정 파일과 다른 JSON 파일이어야 합니다.")
    return path


def _restore_odds_cache(credential: str, regions: str) -> None:
    """프로세스/키/리전별 최초 접근에만 복구하며 실패 상태도 보존한다."""
    global _odds_restore_key
    try:
        path = _odds_cache_path()
    except ValueError:
        _odds_state["persistenceError"] = "InvalidPath"
        return
    identity = (str(path), credential, regions)
    if _odds_restore_key == identity:
        return
    _odds_restore_key = identity
    if (_odds_cache["created"] and _odds_cache["credential"] == credential
            and _odds_cache["regions"] == regions):
        return
    _odds_cache.update({"created": 0.0, "credential": credential, "regions": regions, "eventsByDate": {}})
    _odds_state.update({"lastFetch": None, "lastError": None, "creditsRemaining": None,
                        "creditsUsed": None, "requestCost": None, "eventCount": 0,
                        "cacheRestored": False, "persistenceError": None})
    if path is None or not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("formatVersion") != 1 or payload.get("credential") != credential or payload.get("regions") != regions:
            return
        state = payload["state"]
        if not isinstance(state, dict):
            raise ValueError("배당 상태 형식 오류")
        _odds_state.update({key: state.get(key) for key in ("lastFetch", "lastError", "creditsRemaining", "creditsUsed", "requestCost")})
        # 최신 호출 실패는 이전 성공 응답을 복구하지 않는다.
        if state.get("lastError"):
            return
        created = float(payload["created"])
        if not math.isfinite(created) or not 0 <= time.time() - created <= _env_int("PLAYBALL_ODDS_CACHE_SECONDS", 3600, 1):
            return
        events = payload["eventsByDate"]
        if not isinstance(events, dict) or any(not isinstance(value, dict) for value in events.values()):
            raise ValueError("배당 응답 형식 오류")
        for day, markets in events.items():
            datetime.strptime(day, "%Y-%m-%d")
            for market in markets.values():
                teams = market["teams"]
                if (not isinstance(teams, dict) or len(teams) != 2
                        or set(teams) != {market["away"], market["home"]}
                        or not set(teams).issubset(TEAM_CODES.values())
                        or _parse_timestamp(market["commenceTime"]) is None
                        or not isinstance(market["bookmakerCount"], int) or market["bookmakerCount"] < 1):
                    raise ValueError("배당 시장 형식 오류")
                for metrics in teams.values():
                    price, probability = float(metrics["price"]), float(metrics["marketProbability"])
                    if (not math.isfinite(price) or price <= 1 or not math.isfinite(probability)
                            or not 0 <= probability <= 1 or _parse_timestamp(metrics["lastUpdate"]) is None):
                        raise ValueError("배당 가격 형식 오류")
        _odds_cache.update({"created": created, "eventsByDate": events})
        _odds_state.update({"cacheRestored": True, "eventCount": sum(len(values) for values in events.values())})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        _odds_state["persistenceError"] = "RestoreFailed"


def _persist_odds_cache() -> None:
    try:
        path = _odds_cache_path()
        if path is not None:
            atomic_write_json(path, {"formatVersion": 1, "credential": _odds_cache["credential"],
                "regions": _odds_cache["regions"], "created": _odds_cache["created"],
                "eventsByDate": _odds_cache["eventsByDate"], "state": _odds_state})
        _odds_state["persistenceError"] = None
    except (OSError, ValueError):
        _odds_state["persistenceError"] = "SaveFailed"


def _parse_market_odds(raw: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    results_by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
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
                if team in fair_samples and math.isfinite(price) and price > 1:
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
            event_id = str(event.get("id") or f"{away}:{home}:{commence}")
            results_by_date[event_date][event_id] = {
                "eventId": event_id, "away": away, "home": home,
                "teams": {
                    away: {**best[away], "marketProbability": statistics.median(fair_samples[away])},
                    home: {**best[home], "marketProbability": statistics.median(fair_samples[home])},
                },
                "bookmakerCount": min(len(fair_samples[away]), len(fair_samples[home])),
                "lastUpdate": last_update,
                "commenceTime": commence,
            }
    return dict(results_by_date)


def fetch_market_odds(date: str, refresh: bool = False) -> dict[str, dict[str, Any]]:
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
        _restore_odds_cache(credential, regions)
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
            # 파일 복구가 실패한 최신 호출 이전의 성공 응답을 되살리지 않게 한다.
            path = _odds_cache_path()
            if path is not None:
                path.unlink(missing_ok=True)
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
                    "cacheRestored": False,
                })
                _persist_odds_cache()
        except Exception as exc:
            error_name = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            with _odds_lock:
                _odds_cache.update({"created": 0.0, "eventsByDate": {}, "credential": credential, "regions": regions})
                _odds_state.update({"lastError": error_name, "lastFetch": datetime.now(KST).isoformat(timespec="seconds"),
                                    "eventCount": 0, "cacheRestored": False})
                _persist_odds_cache()
                return {}
        return events_by_date.get(date, {})


def match_market_odds(games: list[dict[str, Any]], date: str, markets: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """팀과 시작 시각으로 제공사 이벤트를 KBO 경기 ID에 일대일 연결한다."""
    choices = {}
    for game in games:
        try:
            starts_at = datetime.fromisoformat(f"{date}T{str(game['G_TM']).strip()[:5]}").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        candidates = []
        for event_id, market in markets.items():
            if (market["away"], market["home"]) != (game["AWAY_NM"], game["HOME_NM"]):
                continue
            commence_at = _parse_timestamp(market.get("commenceTime"))
            if commence_at is not None:
                distance = abs((starts_at - commence_at).total_seconds())
                if distance <= 3600:
                    candidates.append((distance, event_id))
        candidates.sort()
        if candidates and (len(candidates) == 1 or candidates[0][0] < candidates[1][0]):
            choices[game["G_ID"]] = candidates[0][1]
    counts = {event_id: list(choices.values()).count(event_id) for event_id in choices.values()}
    return {game_id: markets[event_id] for game_id, event_id in choices.items() if counts[event_id] == 1}


def fetch_lineup(game: dict[str, Any]) -> dict[str, Any]:
    raw = _post_json(
        "/ws/Schedule.asmx/GetLineUpAnalysis",
        {
            "leId": str(game["LE_ID"]), "srId": str(game["SR_ID"]),
            "seasonId": str(game["SEASON_ID"]), "gameId": game["G_ID"],
        },
    )
    if not isinstance(raw, list) or len(raw) < 5:
        raise DataContractError("라인업: 응답 구조가 변경됐습니다.")

    def parse_side(index: int, team: str) -> list[dict[str, Any]]:
        if not raw[index]:
            return []
        grid = json.loads(raw[index][0])
        players = []
        for item in grid.get("rows", []):
            cells = [str(cell.get("Text", "")).strip() for cell in item.get("row", [])]
            if len(cells) < 3 or not cells[0].isdigit() or not cells[2] or cells[1] not in POSITION_GROUPS:
                raise DataContractError("라인업: 타순·포지션·선수명이 잘못됐습니다.")
            players.append({"order": int(cells[0]), "position": cells[1], "name": cells[2], "team": team})
        orders = [player["order"] for player in players]
        if len(orders) != len(set(orders)) or any(not 1 <= order <= 9 for order in orders):
            raise DataContractError("라인업: 타순이 중복되거나 범위를 벗어났습니다.")
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
    # 같은 팀의 동명이인도 있으므로 검색 순서로 선수를 결정하지 않는다.
    pitcher_type = candidates[0].get("P_TYPE", "") if len(candidates) == 1 else ""
    if pitcher_type.startswith("좌"):
        split, label = "LO", "좌투"
    elif "언" in pitcher_type:
        split, label = "LU,RU", "언더"
    elif pitcher_type.startswith("우"):
        split, label = "RO", "우투"
    else:
        split, label = None, "유형 미확인"
    return {"team": game[team_key], "name": name, "type": pitcher_type, "split": split, "label": label}


def fetch_pitcher_hands(games: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    jobs = [(game, side) for game in games for side in ("away", "home")]
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as executor:
        values = list(executor.map(lambda job: fetch_pitcher_hand(*job), jobs))
    return {(game["G_ID"], side): value for (game, side), value in zip(jobs, values)}


def fetch_matchup_hitter_stats(team_splits: dict[str, set[str]]) -> dict[tuple[str, str, str], dict[str, float]]:
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
        teams = [team_id for team_id, requested_splits in team_splits.items() if split in requested_splits]
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
            records = _record_entries(parser, ("선수명", "팀명", "AVG", "AB", "H"), "유형별 타격 기록")
            if any(record["팀명"] != TEAM_CODES[team_id] for record in records):
                raise DataContractError("유형별 타격 기록: 팀 필터가 적용되지 않았습니다.")
            for record in _unambiguous_player_records(records):
                name, team = record["선수명"].lstrip("* "), record["팀명"]
                ab, hits = _record_number(record, "AB"), _record_number(record, "H")
                if hits > ab:
                    raise DataContractError("유형별 타격 기록: 안타 수가 타수를 초과합니다.")
                result[(team, name, split)] = {
                    "avg": _record_number(record, "AVG", 1) if ab else 0.0, "ab": ab, "hits": hits,
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
    if not isinstance(raw, dict) or len(raw.get("arrPitcher", [])) != 2:
        raise DataContractError("불펜 기록: 양 팀 투수 기록이 누락됐습니다.")
    result: dict[str, list[dict[str, Any]]] = {}
    for team, item in zip((game["AWAY_NM"], game["HOME_NM"]), raw.get("arrPitcher", [])):
        grid = json.loads(item["table"])
        pitchers = []
        for row in grid.get("rows", []):
            cells = [html.unescape(str(cell.get("Text", ""))).replace("&nbsp;", "").strip() for cell in row.get("row", [])]
            if len(cells) < 9 or not cells[0]:
                raise DataContractError("불펜 기록: 투수 또는 투구 수 컬럼이 누락됐습니다.")
            pitchers.append({
                "name": cells[0], "starter": cells[1] == "선발",
                "pitches": int(_record_number({"NP": cells[8]}, "NP")),
            })
        if not pitchers:
            raise DataContractError("불펜 기록: 종료 경기의 투수 기록이 없습니다.")
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
    hitting = _record_table(TEAM_HITTER_1, ("팀명", "AVG", "G", "R", "H"))
    on_base = _record_table(TEAM_HITTER_2, ("팀명", "SLG", "OBP", "OPS"))
    pitching = _record_table(TEAM_PITCHER_1, ("팀명", "ERA", "W", "L", "WHIP"))
    stats = {}
    for row in hitting:
        games = _record_number(row, "G")
        if not games or row["팀명"] in stats:
            raise DataContractError("팀 타격 기록: 경기 수가 부족하거나 팀 기록이 중복됐습니다.")
        runs = _record_number(row, "R")
        stats[row["팀명"]] = {"avg": _record_number(row, "AVG", 1), "games": games,
                             "runs": runs, "runs_per_game": runs / games, "hits": _record_number(row, "H")}
    for row in on_base:
        if row["팀명"] in stats:
            stats[row["팀명"]].update({field.lower(): _record_number(row, field, 5 if field == "OPS" else 4 if field == "SLG" else 1)
                                      for field in ("SLG", "OBP", "OPS")})
    for row in pitching:
        if row["팀명"] in stats:
            stats[row["팀명"]].update({field.lower(): _record_number(row, field) for field in ("ERA", "WHIP")})
    if set(stats) != set(TEAM_CODES.values()) or any(not {"avg", "runs_per_game", "obp", "era", "whip"}.issubset(team) for team in stats.values()):
        raise DataContractError("팀 기록: 리그 팀 또는 필수 지표가 누락됐습니다.")
    return stats


def _fetch_team_filtered_records(url: str, team_ids: list[str], fields: tuple[str, ...]) -> list[dict[str, str]]:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    current_html = _get_text(url, opener)
    target = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$ddlTeam$ddlTeam"
    records = []
    for team_id in team_ids:
        current_html = _post_webform(opener, url, current_html, target, {target: team_id})
        parser = TableParser(("tData01",))
        parser.feed(current_html)
        team_records = _record_entries(parser, fields, "선수 시즌 기록")
        if any(row["팀명"] != TEAM_CODES[team_id] for row in team_records):
            raise DataContractError("선수 시즌 기록: 팀 필터가 적용되지 않았습니다.")
        records.extend(team_records)
    return records


def _unambiguous_player_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """선수 ID가 없는 표에서 동명이인 기록을 합치거나 덮어쓰지 않는다."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in records:
        counts[(row["팀명"], row["선수명"].lstrip("* "))] += 1
    return [row for row in records if counts[(row["팀명"], row["선수명"].lstrip("* "))] == 1]


def fetch_hitter_stats(team_ids: list[str]) -> dict[tuple[str, str], dict[str, float]]:
    records = _fetch_team_filtered_records(PLAYER_HITTER_1, team_ids, ("선수명", "팀명", "AVG", "G", "PA", "AB", "H", "HR"))
    result = {}
    for row in _unambiguous_player_records(records):
        ab, hits, pa = (_record_number(row, field) for field in ("AB", "H", "PA"))
        if hits > ab or ab > pa:
            raise DataContractError("타자 기록: 안타·타수·타석 수가 맞지 않습니다.")
        result[(row["팀명"], row["선수명"].lstrip("* "))] = {
            "avg": _record_number(row, "AVG", 1) if ab else 0.0, "games": _record_number(row, "G"),
            "pa": pa, "ab": ab, "hits": hits, "hr": _record_number(row, "HR"),
        }
    return result


def fetch_pitcher_stats(team_ids: list[str]) -> dict[tuple[str, str], dict[str, float]]:
    records = _fetch_team_filtered_records(PLAYER_PITCHER_1, team_ids, ("선수명", "팀명", "ERA", "G", "W", "L", "IP", "WHIP"))
    result = {}
    for row in _unambiguous_player_records(records):
        if row["IP"] in ("0", "0.0", "0 0/3"):
            continue  # 미등판 투수는 팀 기록으로 보완하고 데이터 상태에 표시한다.
        result[(row["팀명"], row["선수명"].lstrip("* "))] = {
            "era": _record_number(row, "ERA"), "games": _record_number(row, "G"),
            "wins": _record_number(row, "W"), "losses": _record_number(row, "L"),
            "innings": row["IP"], "whip": _record_number(row, "WHIP"),
        }
    return result


def _mean_std(values: list[float]) -> tuple[float, float]:
    return statistics.mean(values), max(statistics.pstdev(values), 0.001)


def valid_calibrator(calibrator: Any) -> bool:
    valid = (isinstance(calibrator, dict) and calibrator.get("enabled") is True
            and calibrator.get("kind") == "platt" and calibrator.get("baseModelVersion") == BASE_MODEL_VERSION
            and all(isinstance(calibrator.get(key), (int, float)) and not isinstance(calibrator[key], bool)
                    and math.isfinite(calibrator[key]) for key in ("slope", "intercept")))
    if valid:
        try:
            json.dumps(calibrator, allow_nan=False)
        except (ValueError, TypeError):
            return False
    return valid


def load_calibrator() -> dict[str, Any] | None:
    path = os.environ.get("PLAYBALL_CALIBRATION_PATH", "data/calibration.json")
    try:
        with open(path, encoding="utf-8") as stream:
            calibrator = json.load(stream)
        if valid_calibrator(calibrator):
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


def calibrator_model_version(calibrator: dict[str, Any] | None) -> str:
    if not calibrator:
        return BASE_MODEL_VERSION
    encoded = json.dumps(calibrator, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    revision = hashlib.sha256(encoded).hexdigest()[:16]
    return f"{BASE_MODEL_VERSION}+platt-{revision}"


def active_model_version() -> str:
    return calibrator_model_version(load_calibrator())


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
            "lastUpdate": teams[team].get("lastUpdate"),
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
    bookmaker_count = int(market.get("bookmakerCount", 0))
    value = {
        "available": True,
        "favorite": favorite_metrics, "underdog": underdog_metrics,
        "returnAdvantageValue": round(advantage, 6),
        "returnAdvantagePp": round(advantage * 100, 1),
        "bookmakerCount": bookmaker_count,
        "lastUpdate": market.get("lastUpdate"),
        "commenceTime": market.get("commenceTime"),
        "marketEventId": market.get("eventId"),
        "criterion": {
            "minimumEvPct": min_ev * 100, "minimumEdgePp": min_edge * 100,
            "minimumAdvantagePp": min_advantage * 100,
            "minimumBookmakers": min_bookmakers, "maximumAgeMinutes": max_age_minutes,
            "closeBeforeMinutes": cutoff_minutes,
        },
    }
    _update_value_bet_quality(value, datetime.now(timezone.utc))
    return value


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _update_value_bet_quality(value: dict[str, Any], now: datetime) -> None:
    """통계 캐시를 유지하면서 배당의 유효성과 추천 여부는 현재 시각으로 판정한다."""
    criterion = value["criterion"]
    price_updates = [_parse_timestamp(value[side].get("lastUpdate")) for side in ("favorite", "underdog")]
    commence_at = _parse_timestamp(value.get("commenceTime"))
    age_minutes = (
        max(max((now - updated_at).total_seconds() / 60, 0) for updated_at in price_updates)
        if all(price_updates) else None
    )
    quality = {
        "marketFresh": age_minutes is not None and age_minutes <= criterion["maximumAgeMinutes"],
        "ageMinutes": round(age_minutes, 1) if age_minutes is not None else None,
        "bettingOpen": commence_at is not None and now < commence_at - timedelta(minutes=criterion["closeBeforeMinutes"]),
        "enoughBookmakers": value["bookmakerCount"] >= criterion["minimumBookmakers"],
    }
    dog = value["underdog"]
    value["quality"] = quality
    value["recommendation"] = (
        dog["expectedReturnValue"] >= criterion["minimumEvPct"] / 100
        and dog["modelProbabilityValue"] - dog["marketProbabilityValue"] >= criterion["minimumEdgePp"] / 100
        and value["returnAdvantageValue"] >= criterion["minimumAdvantagePp"] / 100
        and quality["marketFresh"] and quality["bettingOpen"] and quality["enoughBookmakers"]
    )


def _current_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """공유 캐시를 변경하지 않고 응답 시점의 시간 의존 필드를 갱신한다."""
    result = deepcopy(analysis)
    now = datetime.now(timezone.utc)
    odds_state = odds_provider_status()
    provider_failed = odds_state["configured"] and bool(odds_state["lastError"])
    provider_disabled = not odds_state["configured"]
    for game in result.get("games", []):
        starts_at = _parse_timestamp(game.get("startsAt"))
        if "startsAt" in game and (starts_at is None or now >= starts_at):
            game["snapshotEligible"] = False
        value = game.get("valueBet") or {}
        if value.get("available"):
            if provider_failed or provider_disabled:
                game["valueBet"] = {
                    "available": False, "recommendation": False,
                    "reason": "배당 제공사 오류" if provider_failed else "배당 미연결",
                }
            else:
                _update_value_bet_quality(value, now)
    if "snapshotEligible" in result:
        result["snapshotEligible"] = any(game.get("snapshotEligible", False) for game in result["games"])
    if "odds" in result:
        result["odds"] = odds_state
    if "valueBetStatus" in result and provider_failed:
        result["valueBetStatus"] = "provider-error"
    elif "valueBetStatus" in result and provider_disabled:
        result["valueBetStatus"] = "not-configured"
    if "valueBets" in result:
        result["valueBets"] = [
            game["valueBet"] | {"gameId": game["id"], "away": game["away"], "home": game["home"]}
            for game in result["games"] if (game.get("valueBet") or {}).get("recommendation")
        ]
    return result


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
    home_percent = round(home_probability * 100, 1)
    away_percent = round(100 - home_percent, 1)
    pick = home_name if home_probability >= 0.5 else away_name
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
        "status": "completed" if bool(game.get("GAME_RESULT_CK")) else
                  "scheduled" if str(game.get("GAME_STATE_SC")) == "1" else
                  "in-progress" if str(game.get("GAME_STATE_SC")) == "2" else "unavailable",
        "result": {"awayScore": int(_record_number({"score": game.get("T_SCORE_CN")}, "score")),
                   "homeScore": int(_record_number({"score": game.get("B_SCORE_CN")}, "score"))}
                  if bool(game.get("GAME_RESULT_CK")) else None,
        "snapshotEligible": str(game.get("GAME_STATE_SC")) == "1" and not bool(game.get("GAME_RESULT_CK")),
        "weather": weather, "availability": {"away": away_availability, "home": home_availability},
        "valueBet": value_bet,
        "reasons": [
            f"{better_offense} 시즌 득점력 우위 ({team_stats[better_offense]['runs_per_game']:.2f}점/경기)",
            f"{better_pitching} 팀 평균자책점 우위 ({team_stats[better_pitching]['era']:.2f})",
            f"선발 ERA: {away_name} {away_starter.get('era', away['era']):.2f}{' (팀 기록 사용)' if not away_starter else ''} · {home_name} {home_starter.get('era', home['era']):.2f}{' (팀 기록 사용)' if not home_starter else ''}",
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
            "rawHomeProbability": raw_home_probability,
            "calibrated": bool(calibrator),
        },
    }


def _hitter_predictions(
    games: list[dict[str, Any]], lineups: dict[str, dict[str, Any]],
    hitter_stats: dict[tuple[str, str], dict[str, float]], team_stats: dict[str, dict[str, float]],
    pitcher_stats: dict[tuple[str, str], dict[str, float]],
    pitcher_hands: dict[tuple[str, str], dict[str, str]],
    matchup_hitter_stats: dict[tuple[str, str, str], dict[str, float]],
    roster_status: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], bool, dict[str, dict[str, list[dict[str, Any]]]]]:
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
                opponent_side = "home" if side == "away" else "away"
                hand = pitcher_hands[(game["G_ID"], opponent_side)]
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
                    "gameId": game["G_ID"], "gameTime": game["G_TM"],
                    "name": player["name"], "team": player["team"], "position": position,
                    "rawPosition": player["position"], "probability": probability,
                    "opponent": f"vs {opponent}", "pitcher": f"상대 선발 {opponent_pitcher_name or '미정'}",
                    "order": f"{player['order']}번 타자", "avg": round(stats.get("avg", league_avg), 3),
                    "ab": int(ab), "matchupAvg": round(split_stats.get("avg", matchup_average), 3),
                    "matchupAb": int(split_ab), "pitcherHand": hand["label"],
                    "estimated": not bool(ab), "matchupEstimated": not bool(split_ab),
                    "lineupConfirmed": lineup["confirmed"],
                })

    result: dict[str, list[dict[str, Any]]] = {"전체": sorted(hitters, key=lambda item: item["probability"], reverse=True)[:3]}
    for position in ("포수", "1루수", "2루수", "3루수", "유격수", "좌익수", "중견수", "우익수", "지명타자"):
        ranked = sorted((item for item in hitters if item["position"] == position), key=lambda item: item["probability"], reverse=True)[:3]
        if ranked:
            result[position] = ranked
    by_game = {}
    for game in games:
        players = [player for player in hitters if player["gameId"] == game["G_ID"]]
        by_game[game["G_ID"]] = {position: sorted(
            (player for player in players if position == "전체" or player["position"] == position),
            key=lambda player: player["probability"], reverse=True,
        ) for position in result}
    return result, all_confirmed, by_game


def _data_quality(games, lineups, hitters, pitchers, hands, matchup, rosters, weather) -> dict[str, Any]:
    warnings = []
    missing_hitters, missing_splits, missing_starters, unknown_hands = 0, 0, 0, 0
    for game in games:
        lineup = lineups[game["G_ID"]]
        if not lineup["away"] or not lineup["home"]:
            warnings.append(f"{game['AWAY_NM']} @ {game['HOME_NM']} {game['G_TM']}: 공개 라인업이 없습니다.")
        for side, team, name_key in (("away", game["AWAY_NM"], "T_PIT_P_NM"), ("home", game["HOME_NM"], "B_PIT_P_NM")):
            hand = hands[(game["G_ID"], "home" if side == "away" else "away")]
            unknown_hands += hand["split"] is None
            missing_starters += (team, (game.get(name_key) or "").strip()) not in pitchers
            for player in lineup[side]:
                key = (team, player["name"])
                missing_hitters += not hitters.get(key, {}).get("ab", 0)
                missing_splits += not matchup.get((*key, hand["split"]), {}).get("ab", 0)
    counts = {"missingHitterStats": missing_hitters, "missingMatchupStats": missing_splits,
              "missingStarterStats": missing_starters, "unknownPitcherHands": unknown_hands}
    for count, message in ((missing_hitters, "타자 시즌 기록 없음: 리그 평균으로 추정"),
                           (missing_splits, "상대 유형별 타격 기록 부족: 시즌 기록 사용"),
                           (missing_starters, "선발 시즌 기록 부족: 팀 기록 사용"),
                           (unknown_hands, "선발 투구 유형 미확인: 유형별 보정 생략")):
        if count:
            warnings.append(f"{message} ({count}건)")
    teams = {game[field] for game in games for field in ("AWAY_NM", "HOME_NM")}
    if any(not rosters.get("active", {}).get(team) for team in teams):
        warnings.append("일부 팀의 1군 엔트리 자료 미제공: 해당 팀의 엔트리 이탈 보정 생략")
    if any(not value.get("available") for value in weather.values()):
        warnings.append("일부 구장 날씨 미제공: 날씨 보정 생략")
    return {"status": "partial" if warnings else "complete", "warnings": warnings, **counts}


@dataclass
class CacheEntry:
    created: float
    value: dict[str, Any]
    model_version: str = ""


_cache: OrderedDict[str, CacheEntry] = OrderedDict()
_cache_lock = threading.Lock()
@dataclass
class AnalysisFlight:
    event: threading.Event
    force: bool = False
    refresh_odds: bool = False
    result: dict[str, Any] | None = None
    error: Exception | None = None
    model_version: str = ""


_inflight: dict[str, AnalysisFlight] = {}


def _analyze_uncached(date: str, refresh_odds: bool = False) -> dict[str, Any]:
    games = fetch_games(date)
    calibrator = load_calibrator()
    method_version = calibrator_model_version(calibrator)
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
        relevant_team_ids = list(dict.fromkeys([code for game in games for code in (game["AWAY_ID"], game["HOME_ID"])]))
        with ThreadPoolExecutor(max_workers=2) as executor:
            team_future = executor.submit(fetch_team_stats)
            hands_future = executor.submit(fetch_pitcher_hands, games)
            team_stats = team_future.result()
            pitcher_hands = hands_future.result()
        team_id_by_name = {name: code for code, name in TEAM_CODES.items()}
        team_splits: dict[str, set[str]] = defaultdict(set)
        for game in games:
            team_splits[team_id_by_name[game["AWAY_NM"]]].update(filter(None, [pitcher_hands[(game["G_ID"], "home")]["split"]]))
            team_splits[team_id_by_name[game["HOME_NM"]]].update(filter(None, [pitcher_hands[(game["G_ID"], "away")]["split"]]))
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
        market_by_game = match_market_odds(games, date, market_odds)
        lineups = {game["G_ID"]: lineup for game, lineup in zip(games, lineup_results)}
        game_predictions = [
            _game_prediction(
                game, team_stats, pitcher_stats, bullpen_stats, hitter_stats, roster_status,
                weather_by_game[game["G_ID"]], market_by_game.get(game["G_ID"]),
                calibrator, lineups[game["G_ID"]]["confirmed"],
            )
            for game in games
        ]
        for game in game_predictions:
            try:
                starts_at = datetime.fromisoformat(f"{date}T{str(game['time']).strip()[:5]}").replace(tzinfo=KST)
                game["startsAt"] = starts_at.isoformat(timespec="seconds")
            except ValueError:
                game["startsAt"] = None
        hitter_predictions, all_confirmed, hitters_by_game = _hitter_predictions(
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
            "hittersByGame": hitters_by_game,
            "dataQuality": _data_quality(games, lineups, hitter_stats, pitcher_stats, pitcher_hands,
                                         matchup_hitter_stats, roster_status, weather_by_game),
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
    """동일 날짜의 요청을 합치고 진행 중 추가된 강제·배당 갱신도 수행한다."""
    compact_date = date.replace("-", "")
    datetime.strptime(compact_date, "%Y%m%d")
    cache_ttl = _env_int("PLAYBALL_ANALYSIS_CACHE_SECONDS", 600, 30)
    cache_limit = _env_int("PLAYBALL_ANALYSIS_CACHE_ENTRIES", 32, 1)
    model_version = active_model_version()
    with _cache_lock:
        cached = _cache.get(compact_date)
        flight = _inflight.get(compact_date)
        if (flight is None and cached and cached.model_version == model_version and not force
                and not refresh_odds and time.time() - cached.created < cache_ttl):
            _cache.move_to_end(compact_date)
            return _current_analysis(cached.value)
        owner = flight is None
        if owner:
            flight = AnalysisFlight(threading.Event(), force or refresh_odds, refresh_odds)
            flight.model_version = model_version
            _inflight[compact_date] = flight
        else:
            flight.force = flight.force or force or refresh_odds or flight.model_version != model_version
            flight.refresh_odds = flight.refresh_odds or refresh_odds
    if not owner:
        if not flight.event.wait(timeout=180):
            raise TimeoutError("같은 날짜의 분석이 아직 완료되지 않았습니다.")
        if flight.error is not None:
            raise flight.error
        return _current_analysis(flight.result)
    try:
        model_retries = 0
        odds_refreshed = False
        while True:
            with _cache_lock:
                running_force, running_odds = flight.force, flight.refresh_odds
                running_model = flight.model_version
            result = (_analyze_uncached(date, refresh_odds=True)
                      if running_odds and not odds_refreshed else _analyze_uncached(date))
            odds_refreshed = odds_refreshed or running_odds
            current_version = active_model_version()
            with _cache_lock:
                if current_version != running_model or result.get("methodVersion", current_version) != current_version:
                    model_retries += 1
                    if model_retries > 3:
                        raise RuntimeError("분석 중 보정기가 반복 변경됐습니다. 잠시 후 다시 확인해 주세요.")
                    flight.model_version = current_version
                    continue
                if (flight.force and not running_force) or (flight.refresh_odds and not running_odds):
                    continue
                result_version = result.get("methodVersion", current_version)
                _cache[compact_date] = CacheEntry(time.time(), result, result_version)
                _cache.move_to_end(compact_date)
                while len(_cache) > cache_limit:
                    _cache.popitem(last=False)
                flight.result = result
                _inflight.pop(compact_date, None)
                flight.event.set()
                break
    except Exception as exc:
        with _cache_lock:
            flight.error = exc
            _cache.pop(compact_date, None)
            _inflight.pop(compact_date, None)
            flight.event.set()
        raise
    return _current_analysis(result)
