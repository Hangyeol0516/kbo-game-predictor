# PLAYBALL — KBO 경기·역배 가치 분석

[![Container image](https://github.com/Hangyeol0516/kbo-game-predictor/actions/workflows/container.yml/badge.svg)](https://github.com/Hangyeol0516/kbo-game-predictor/actions/workflows/container.yml)

KBO 당일 경기 승률, 포지션별 1안타 이상 확률, 실제 moneyline 배당 기준 역배 기대수익을 한 화면에서 보여주는 설명형 통계 분석 앱입니다.

일정·예고 선발·라인업·1군 엔트리·시즌 기록은 KBO 공식 홈페이지, 날씨는 Open-Meteo, 배당은 The Odds API를 사용합니다. 배당 API가 없어도 경기와 타자 분석은 정상 작동합니다.

## 현재 상태

- `linux/amd64`, `linux/arm64`용 완성 이미지를 GHCR에 자동 배포
- Python Alpine 기반 단일 앱 컨테이너로 구성
- AMD64 로컬 기준 약 18.1MB 이미지(환경에 따라 달라질 수 있음)
- 외부 Python 패키지와 별도 데이터베이스 컨테이너 불필요
- SQLite 데이터와 확률 보정 파일을 Docker 볼륨에 영속화
- 비루트 사용자 `10001`로 실행
- `.env`, 소스, Git 메타데이터와 SQLite 파일의 HTTP 접근 차단
- 커밋별 테스트, 멀티아키텍처 빌드, SBOM과 이미지 출처 증명 자동 생성

## 가장 빠른 실행

```bash
git clone https://github.com/Hangyeol0516/kbo-game-predictor.git
cd kbo-game-predictor
cp .env.example .env
# .env에 PLAYBALL_ODDS_API_KEY 입력(역배 분석을 사용하지 않으면 비워도 됨)
docker compose up -d
```

브라우저에서 <http://localhost:8000>을 엽니다.

```bash
# 상태 확인
curl -fsS http://localhost:8000/health

# 로그 확인
docker compose logs -f playball
```

포트를 변경하려면 다음처럼 실행합니다.

```bash
PLAYBALL_PORT=8080 docker compose up -d
```

## 업데이트

```bash
git pull --ff-only
docker compose pull
docker compose up -d --remove-orphans
```

`latest` 대신 특정 커밋 이미지를 고정하려면 `.env`에 다음 값을 추가합니다. 각 커밋에는 `sha-<앞 7자리>` 태그가 생성됩니다.

```dotenv
PLAYBALL_IMAGE_TAG=sha-24ffb59
```

## Compose 없이 실행

```bash
docker pull ghcr.io/hangyeol0516/kbo-game-predictor:latest
docker run -d \
  --name kbo-game-predictor \
  --restart unless-stopped \
  -p 8000:8000 \
  -v playball_data:/data \
  --env-file .env \
  ghcr.io/hangyeol0516/kbo-game-predictor:latest
```

## 역배 판단 기준

핵심 질문은 **“정배를 포기하고 역배를 선택했을 때 추가 리턴이 충분한가?”**입니다.

```text
역배 기대수익(EV) = 모델 승률 × decimal 배당 - 1
시장 대비 엣지     = 모델 승률 - 무마진 시장확률
정배 대비 EV 우위  = 역배 EV - 정배 EV
```

기본적으로 다음 조건을 모두 충족할 때만 `역배 PICK`으로 표시합니다.

| 조건 | 기본값 |
| --- | ---: |
| 역배 기대수익 | 8% 이상 |
| 시장 대비 모델 엣지 | 5%p 이상 |
| 정배 대비 기대수익 우위 | 10%p 이상 |
| 비교 북메이커 | 2곳 이상 |
| 최고 배당 갱신 시각 | 60분 이내 |
| 추천 마감 | 경기 시작 10분 전 |

조건을 통과하지 못한 경기도 숨기지 않고 `NO BET`과 미달 기준을 표시합니다. 배당이 없거나 제공사 오류가 발생하면 임의의 값이나 오래된 배당으로 역배를 추천하지 않습니다.

## 환경 변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `PLAYBALL_ODDS_API_KEY` | 비어 있음 | The Odds API 키. 비어 있으면 역배 추천 비활성화 |
| `PLAYBALL_ODDS_REGIONS` | `eu` | 조회할 북메이커 지역. 지역 수가 늘면 API 비용도 증가 가능 |
| `PLAYBALL_ODDS_MIN_BOOKMAKERS` | `2` | 추천에 필요한 최소 북메이커 수 |
| `PLAYBALL_ODDS_MAX_AGE_MINUTES` | `60` | 최고 배당의 최대 허용 경과 시간 |
| `PLAYBALL_ODDS_CLOSE_BEFORE_MINUTES` | `10` | 경기 시작 전 추천 마감 시간 |
| `PLAYBALL_UPSET_MIN_EV` | `0.08` | 역배 최소 기대수익 |
| `PLAYBALL_UPSET_MIN_EDGE` | `0.05` | 시장 대비 최소 확률 엣지 |
| `PLAYBALL_UPSET_MIN_ADVANTAGE` | `0.10` | 정배 대비 최소 EV 우위 |
| `PLAYBALL_ANALYSIS_CACHE_SECONDS` | `600` | 날짜별 분석 캐시 시간 |
| `PLAYBALL_ANALYSIS_CACHE_ENTRIES` | `32` | 메모리에 보관할 최대 날짜 수 |
| `PLAYBALL_MAX_CONCURRENT_ANALYSES` | `4` | 동시에 실행할 수 있는 분석 요청 수 |
| `PLAYBALL_COLLECT_INTERVAL_SECONDS` | `900` | 스냅샷·결과 수집 간격 |
| `PLAYBALL_COLLECTOR_ENABLED` | `1` | `0`이면 백그라운드 수집기 비활성화 |
| `PLAYBALL_RESULT_LOOKBACK_DAYS` | `30` | 미정산 경기 결과를 다시 확인할 기간 |
| `PLAYBALL_REFRESH_TOKEN` | 비어 있음 | 강제 갱신 헤더용 비밀값. 비어 있으면 강제 갱신 비활성화 |
| `PLAYBALL_PORT` | `8000` | 호스트에 공개할 포트 |
| `PLAYBALL_IMAGE_TAG` | `latest` | 실행할 GHCR 이미지 태그 |

무료 API 쿼터를 보호하기 위해 기본 지역은 한 곳이며, 경기일에는 전체 KBO 배당을 오전 9시, 첫 경기 3시간 전, 첫 경기 30분 전의 최대 세 번만 갱신합니다. 15분 간격 분석과 일반 페이지 요청은 마지막 배당 응답만 공유하고, 경기가 없거나 첫 경기가 시작된 뒤에는 배당을 갱신하지 않습니다. 라인업 분석은 마지막 경기 시작 전까지 계속해 늦게 시작하는 경기의 확정 라인업도 저장합니다. 배당 슬롯은 호출 전에 SQLite에 영구 예약하므로 컨테이너 재시작·동시 수집·호출 실패에도 같은 슬롯을 재시도하지 않습니다. 실패한 슬롯은 다음 슬롯에서 다시 확인하며, 재시작 직후에는 다음 배당 수집까지 배당 자료가 없을 수 있습니다. 토큰으로 인증한 강제 갱신은 예외적으로 새 배당을 요청합니다. `/health`에서 마지막 요청 시각, 처리한 수집 슬롯, 오류, 이벤트 수와 제공사가 반환한 요청량 정보를 확인할 수 있습니다. API 키 자체는 응답이나 로그에 노출하지 않습니다.

같은 경기·모델·라인업 상태의 요약 기록은 경기 시작 전 가장 최근 스냅샷으로 갱신됩니다. 별도 `prediction_events`에 각 시점의 경기 예측·추천·배당을 보존하며 같은 캐시 응답을 반복 조회해도 이력은 중복 저장되지 않습니다. 과거 경기의 계산 근거를 펼치면 예측 변경 이력과 종료 결과·채점을 확인할 수 있습니다. 역배 성과와 임계값 재검증은 경기별 확정 라인업 우선·최신 시점 기준을 공유합니다. 최종 선택 스냅샷의 추천이 철회되거나 최신 분석에서 배당 자료가 사라지면 오전 추천을 ROI에 되살리지 않습니다. 추천 이력 전체는 별도로 확인할 수 있으며 ROI는 실제 체결 내역이 아닌 최종 스냅샷을 따른 가상 성과입니다.

캐시 응답에서도 현재 시각으로 추천 마감과 배당 신선도를 다시 판정합니다. 열린 화면은 새 분석 응답을 기다리는 동안에도 마감·만료 시각에 추천 표시를 갱신하며, 배당 제공사 오류가 발생하면 이전 배당 캐시와 추천을 사용하지 않습니다. 늦게 도착한 이전 분석은 최신 저장 기록을 덮어쓰지 않고, 경기 시작 후에는 캐시된 경기 전 분석을 새로 저장하지 않습니다. 결과 수집은 날짜별로 처리하므로 특정 날짜가 실패해도 다른 날짜의 채점을 계속합니다. 실패 날짜는 `/health`의 수집기 오류와 서버 로그에 표시하고 다음 수집 때 다시 확인합니다.

## 제공 기능

- 날짜별 KBO 공식 일정과 예고 선발 조회
- 더블헤더 경기 ID별 선발 유형·타자 매치업과 배당 이벤트 연결
- 같은 팀 동명이인의 선수 기록·투구 유형은 모호한 검색 순서로 선택하지 않고 평균 추정 상태로 표시
- 과거 날짜의 저장된 당시 예측 조회와 명시적인 현재 기록 재분석
- 팀 득점력·ERA·출루율을 이용한 경기 승률
- 선발 ERA·WHIP과 최근 3일 불펜 투구량 반영
- 1군 엔트리 이탈, 구장 득점 계수와 경기 시간 날씨 반영
- 좌투·우투·언더 유형별 타격 스플릿 기반 1안타 이상 확률
- 실제 배당의 무마진 시장확률과 모델 확률 비교
- 경기별 `역배 PICK / NO BET` 및 탈락 기준 표시
- 경기별 라인업 상태와 경기 상태를 보존하는 SQLite 스냅샷
- 모델 버전별 적중률, Brier Score, 역배 실현 ROI 표시
- 시간순 백테스트 데이터 내보내기
- 검증 성능이 개선될 때만 활성화되는 Platt 확률 보정기

## 데이터 흐름

```text
KBO 공식 기록 ─┐
Open-Meteo ────┼─> 통계 추정·확률 보정 ─> 웹 화면
The Odds API ──┘             │
                             └─> SQLite 스냅샷 ─> 채점·백테스트
```

현재 모델 버전은 `stats-v7-game-context`입니다. 경기별 연결·기록 검증과 승리 팀 선택이 바뀌었으므로 이전 버전의 보정기와 성능을 v7에 합치지 않습니다. 승리 팀은 반올림 전 확률로 선택하며 화면 승률은 소수점 한 자리까지 표시합니다. 과거 예측은 계속 조회할 수 있고 이전 성능은 `modelVersion=all`로 확인할 수 있습니다. 상세 계산식과 데이터 누수 방침은 [모델 카드](docs/model-card.md)에 정리되어 있습니다.

## 화면 사용법

- 관심 구단을 선택하면 경기·배당·타자 랭킹에 함께 적용되고 다음 방문에도 기억됩니다. 구단별 타자는 전체 TOP 3에서 탈락한 선수도 경기별 원자료에서 다시 선정합니다.
- 타자 분석 경기 선택으로 더블헤더의 각 경기 TOP 3를 따로 확인할 수 있습니다.
- 날짜·구단·조회 방식·타자 분석 경기·포지션이 주소에 반영되어 현재 화면의 링크를 공유할 수 있습니다. 날짜와 갱신 시각은 한국 시각 기준입니다.
- `새로 확인`은 일반 캐시 조회입니다. 관리용 강제 갱신 토큰이나 추가 배당 호출을 사용하지 않습니다. 오늘 화면은 5분마다 확인하며 숨겨진 탭과 과거 조회에서는 자동 확인을 멈춥니다.
- 자동 확인 실패 시 이전 화면과 오류를 표시하고 역배 추천을 보류합니다. 새 자료 수신 후 정상 표시로 복구합니다. 분석 조회는 3분, 성능 조회는 30초 내에 응답 본문까지 읽지 못하면 시간 초과를 표시하고 재시도할 수 있습니다. 날짜나 성능 범위를 바꾸면 이전 브라우저 요청을 취소하고 늦은 응답은 무시합니다.
- `분석 내려받기`는 현재 수신한 전체 분석을 JSON으로 저장합니다. 공유 링크의 실시간 결과는 조회 시점에 따라 바뀔 수 있습니다.
- 성능 범위에서 현재 모델과 전체 모델을 선택할 수 있습니다. 적중률의 Wilson 95% 구간(경기별 독립 표본 가정), 홈팀 고정 선택 기준선, 50:50 Brier 기준선, 확률 구간별 실제 승률을 함께 제공합니다. 작은 표본이나 날짜별 상관이 큰 자료는 신중하게 해석해야 합니다.
- 모바일 메뉴·키보드 포커스·바로가기 링크·동작 줄이기 설정을 지원합니다.

## HTTP 엔드포인트

| 경로 | 설명 |
| --- | --- |
| `/` | 웹 화면 |
| `/health` | DB, 수집기, 배당 제공사와 캐시 상태 |
| `/api/analysis?date=YYYY-MM-DD` | 오늘·미래는 분석, 과거는 저장된 당시 예측 조회 |
| `/api/analysis?date=YYYY-MM-DD&mode=historical` | 해당 날짜의 저장된 경기 전 예측 조회 |
| `/api/analysis?date=YYYY-MM-DD&mode=reanalysis` | 과거 경기를 현재 시즌 기록으로 재분석. 당시 예측·배당 추천·평가 저장과 구분 |
| `/api/analysis?date=YYYY-MM-DD&refresh=1` | `X-Refresh-Token`이 일치할 때만 분석 캐시 우회 |
| `/api/performance` | 현재 활성 모델의 적중률·Brier Score·역배 ROI |
| `/api/performance?modelVersion=all` | 모든 모델 버전을 합친 참고용 성능 |

화면에 필요한 정적 파일 이외의 경로는 `404`를 반환합니다.

과거 조회에서 기록이 없으면 `historyStatus=missing`으로 표시하며 외부 데이터를 요청해 당시 예측을 복원하지 않습니다. 경기별 상세 JSON 저장을 위해 SQLite에 열을 추가하는 비파괴 마이그레이션을 수행합니다. 이전 저장 형식은 `historyStatus=partial`이며 당시 승률만 제공합니다. 새 예측은 경기별 근거·타자 후보·자료 상태까지 보존합니다.

KBO 기록은 컬럼 이름으로 읽고 구조 변경, 팀 필터 실패, 누락·잘못된 숫자와 타수/안타 모순을 검증합니다. 필수 자료가 잘못되면 `502`와 `dataQuality.status=invalid`를 반환합니다. 표본 부족이나 엔트리·날씨 미제공 등은 `dataQuality.warnings`와 화면 안내로 보완 자료를 표시합니다. 미확인 선발 유형은 우투로 가정하지 않습니다.

## 로컬 개발

Python 3.12 이상에서 외부 패키지 없이 실행할 수 있습니다.

```bash
python3 server.py
```

테스트 실행:

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_app.js
```

로컬 소스로 이미지를 만들려면 Compose 오버라이드를 사용합니다.

```bash
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

### Compose 없이 단일 이미지 만들기

웹 화면, API, 백그라운드 수집기와 SQLite 저장소가 한 컨테이너에서 실행됩니다. 별도 DB·웹 서버 컨테이너나 외부 Python 패키지는 필요하지 않습니다.

```bash
docker build -t kbo-game-predictor:local .
docker run -d --name kbo-game-predictor --restart unless-stopped \
  -p 8000:8000 -v playball_data:/data kbo-game-predictor:local
```

브라우저에서 <http://localhost:8000>을 엽니다. 배당 분석은 실행 시 `--env-file .env`를 추가하고 API 키를 설정하면 활성화됩니다. 실시간 KBO·날씨·배당 수집에는 네트워크가 필요합니다. `/data` 볼륨에 DB와 보정 파일이 저장되므로 컨테이너를 교체해도 자료가 유지됩니다.

이미지를 파일로 옮길 수도 있습니다.

```bash
docker save kbo-game-predictor:local | gzip > kbo-game-predictor.tar.gz
# 다른 Docker 호스트에서
docker load -i kbo-game-predictor.tar.gz
docker run -d --name kbo-game-predictor --restart unless-stopped \
  -p 8000:8000 -v playball_data:/data kbo-game-predictor:local
```

로컬 빌드·이미지 파일은 빌드한 플랫폼용입니다. GHCR 배포 워크플로는 기존처럼 AMD64·ARM64 이미지를 함께 빌드합니다. 이미지 파일에는 실행 데이터 볼륨이 포함되지 않으므로 누적 기록을 다른 호스트로 옮길 때는 `/data`도 별도로 이전해야 합니다.

## 데이터와 백테스트

컨테이너의 기본 저장소는 `/data/playball.db`이며 `playball_data` 볼륨에 보존됩니다. 관리형 PostgreSQL로 전환할 때 사용할 DDL은 [schema/postgresql.sql](schema/postgresql.sql)에 있습니다.

```bash
# 누적 예측 성능 확인
docker compose exec playball python scripts/backtest.py --db /data/playball.db

# 평가 데이터를 CSV로 내보내기
docker compose exec playball python scripts/backtest.py \
  --db /data/playball.db \
  --export /data/evaluated_predictions.csv

# 저장된 모든 배당 후보에서 역배 임계값 조합 탐색
docker compose exec playball python scripts/evaluate_value_thresholds.py \
  --db /data/playball.db \
  --minimum-samples 20
```

백테스트는 기본적으로 현재 기본 모델 버전만 평가합니다. 모든 과거 버전을 합치려면 `scripts/backtest.py`에 `--all-models`를 지정합니다. 임계값 탐색 결과는 같은 표본에서 과적합될 수 있으므로 반드시 이후 기간에서 다시 검증해야 합니다.

현재 모델의 종료 경기 100개 이상이 쌓이면 날짜 앞 60%를 학습, 다음 20%를 검증, 마지막 20%를 별도 테스트에 사용해 Platt 보정기를 만들 수 있습니다. 같은 날짜는 구간을 나누지 않으며 검증과 테스트에 각각 최소 20경기가 필요합니다. 검증 Brier Score가 개선되지 않으면 보정 파일을 생성하지 않습니다. 테스트 구간은 학습·활성화 판단에 사용하지 않고 결과와 각 구간 날짜를 파일에 기록합니다.

```bash
docker compose exec playball python scripts/train_calibrator.py \
  --db /data/playball.db \
  --output /data/calibration.json
```

`/data/calibration.json`은 다음 분석부터 자동 적용됩니다. 과거 경기의 현재 시즌 최종 기록으로 과거 예측을 재구성하지 않으며, 실제 경기 전에 저장한 스냅샷만 평가에 사용합니다.

보정기 활성화 여부는 실제 서비스와 동일하게 20~80%로 제한한 확률의 검증 Brier Score로 판단합니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_app.js
```

CI는 단일 앱 이미지를 실행한 뒤 Chromium으로 구단·경기 필터, 공유 포지션 유지, 한국 날짜, 이력·결과 표시, 파일 다운로드, 갱신 대기 중 추천 마감·배당 만료, 시간 초과 후 복구와 320px·390px·1440px 화면을 검증합니다. 동일 화면의 WCAG 2 A/AA·2.1 AA 규칙을 axe로 점검합니다. 외부 데이터는 이 UI 검사에서 고정 응답을 사용하며 실제 KBO 수집 검증과 구분합니다. Playwright와 axe는 검사에만 설치하고 앱 이미지에는 포함하지 않습니다.

로컬에서 UI 검사를 실행하려면 별도 임시 폴더에 CI와 같은 버전의 `playwright`·`@axe-core/playwright`를 설치하고 Chromium을 설치한 뒤, 실행 중인 앱에 `PLAYBALL_UI_BASE_URL=http://127.0.0.1:8000 NODE_PATH=<검사용 node_modules 경로> node tests/test_ui.cjs`를 사용합니다. `PLAYBALL_UI_ARTIFACT_DIR`로 스크린샷·검사 결과의 저장 폴더를 지정할 수 있습니다.

## 한계와 주의사항

- 경기 승률은 팀·선발·불펜·엔트리·구장·날씨를 결합한 설명형 통계 추정치입니다.
- KBO가 부상 진단을 구조화 데이터로 제공하지 않으므로 `부상 확정` 대신 `1군 엔트리 이탈`로 표시합니다.
- 공식 라인업 발표 전에는 KBO 게임센터가 제공하는 최근 라인업을 사용합니다.
- 배당 시장이 열리지 않았거나 마감된 날짜에는 역배 분석이 표시되지 않습니다.
- 예측 확률과 기대수익은 경기 결과나 실제 수익을 보장하지 않습니다.

## 다음 검증 과제

1. 충분한 경기 전 스냅샷을 축적해 확률 보정기를 활성화
2. 축적 결과로 구장 득점 계수와 역배 EV 임계값 재검증
