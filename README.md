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
| `PLAYBALL_ODDS_CACHE_SECONDS` | `3600` | 배당 전체 응답 캐시 시간. 최소 300초 |
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

무료 API 쿼터를 보호하기 위해 기본 지역은 한 곳이며, 한 번 받은 전체 KBO 배당을 1시간 공유합니다. `/health`에서 마지막 요청 시각, 오류, 이벤트 수와 제공사가 반환한 요청량 정보를 확인할 수 있습니다. API 키 자체는 응답이나 로그에 노출하지 않습니다.

## 제공 기능

- 날짜별 KBO 공식 일정과 예고 선발 조회
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

현재 모델 버전은 `stats-v5-context-value`입니다. 상세 계산식과 데이터 누수 방침은 [모델 카드](docs/model-card.md)에 정리되어 있습니다.

## HTTP 엔드포인트

| 경로 | 설명 |
| --- | --- |
| `/` | 웹 화면 |
| `/health` | DB, 수집기, 배당 제공사와 캐시 상태 |
| `/api/analysis?date=YYYY-MM-DD` | 해당 날짜의 경기·타자·역배 분석 |
| `/api/analysis?date=YYYY-MM-DD&refresh=1` | `X-Refresh-Token`이 일치할 때만 분석 캐시 우회 |
| `/api/performance` | 현재 활성 모델의 적중률·Brier Score·역배 ROI |
| `/api/performance?modelVersion=all` | 모든 모델 버전을 합친 참고용 성능 |

화면에 필요한 정적 파일 이외의 경로는 `404`를 반환합니다.

## 로컬 개발

Python 3.12 이상에서 외부 패키지 없이 실행할 수 있습니다.

```bash
python3 server.py
```

테스트 실행:

```bash
python3 -m unittest discover -s tests -v
```

로컬 소스로 이미지를 만들려면 Compose 오버라이드를 사용합니다.

```bash
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

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

종료 경기 100개 이상이 쌓이면 날짜 앞 80%를 학습, 뒤 20%를 검증에 사용해 Platt 보정기를 만들 수 있습니다. 검증 Brier Score가 개선되지 않으면 보정 파일을 생성하지 않습니다.

```bash
docker compose exec playball python scripts/train_calibrator.py \
  --db /data/playball.db \
  --output /data/calibration.json
```

`/data/calibration.json`은 다음 분석부터 자동 적용됩니다. 과거 경기의 현재 시즌 최종 기록으로 과거 예측을 재구성하지 않으며, 실제 경기 전에 저장한 스냅샷만 평가에 사용합니다.

## 한계와 주의사항

- 경기 승률은 팀·선발·불펜·엔트리·구장·날씨를 결합한 설명형 통계 추정치입니다.
- KBO가 부상 진단을 구조화 데이터로 제공하지 않으므로 `부상 확정` 대신 `1군 엔트리 이탈`로 표시합니다.
- 공식 라인업 발표 전에는 KBO 게임센터가 제공하는 최근 라인업을 사용합니다.
- 배당 시장이 열리지 않았거나 마감된 날짜에는 역배 분석이 표시되지 않습니다.
- 예측 확률과 기대수익은 경기 결과나 실제 수익을 보장하지 않습니다.

## 다음 검증 과제

1. 충분한 경기 전 스냅샷을 축적해 확률 보정기를 활성화
2. 축적 결과로 구장 득점 계수와 역배 EV 임계값 재검증
