# PLAYBALL — KBO 예측 앱 프로토타입

KBO 당일 경기 승률, 포지션별 1안타 이상 확률, 실제 배당 기준 역배 기대수익을 보여주는 프로토타입입니다. 일정, 예고 선발, 라인업, 1군 엔트리와 시즌 기록은 KBO 공식 홈페이지에서 가져옵니다.

## 권장 배포: 완성 이미지 받기

GitHub Container Registry에서 완성된 이미지를 받아 앱 컨테이너 하나와 영속 데이터 볼륨을 실행합니다. 배포 서버에서 빌드할 필요가 없으며 PostgreSQL 컨테이너도 필요하지 않습니다. 이미지는 Python Alpine 기반이며 실행에 필요한 파일만 담습니다.

```bash
git clone https://github.com/Hangyeol0516/kbo-game-predictor.git
cd kbo-game-predictor
docker compose up -d
```

역배 EV는 The Odds API의 KBO moneyline 배당을 사용합니다. API 키가 없으면 예측은 정상 작동하지만 역배를 추천하지 않습니다.

```bash
cp .env.example .env
# .env에 PLAYBALL_ODDS_API_KEY 입력
docker compose up -d
```

기본 역배 추천 기준은 EV 8%, 시장 대비 엣지 5%p, 정배 대비 EV 우위 10%p입니다. 추천하지 않는 경기에도 실제 수치와 미달 기준을 함께 표시하므로, 왜 정배를 포기하지 않았는지 확인할 수 있습니다. `.env.example`의 세 임계값으로 조정할 수 있습니다.

무료 배당 쿼터를 아끼기 위해 기본 지역은 `eu` 한 곳이며 응답 전체를 1시간 캐시합니다. `PLAYBALL_ODDS_REGIONS`에 지역을 추가하면 요청 비용도 늘어날 수 있습니다. 캐시 시간은 `PLAYBALL_ODDS_CACHE_SECONDS`로 조정합니다.

브라우저에서 <http://localhost:8000>을 엽니다. 포트를 바꾸려면 `PLAYBALL_PORT=8080 docker compose up -d`처럼 실행합니다.

업데이트와 로그 확인:

```bash
git pull
docker compose pull
docker compose up -d
docker compose logs -f
```

`latest` 대신 특정 이미지 태그를 고정하려면 `PLAYBALL_IMAGE_TAG`를 지정합니다. `sha-<Git 커밋 앞 7자리>` 태그가 커밋마다 자동 생성됩니다.

Compose 없이 Docker만 사용할 수도 있습니다.

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

컨테이너는 비루트 사용자로 실행되며 `/health`에서 수집기 상태, 배당 연결 상태와 남은 API 요청량을 확인합니다. 예측 스냅샷은 `playball_data` 볼륨에 보존됩니다. 웹 서버는 화면에 필요한 정적 파일만 공개하며 `.env`, 데이터베이스와 소스 파일은 제공하지 않습니다.

## 로컬 개발

Python 3.12 이상에서 외부 패키지 없이 실행할 수 있습니다.

```bash
python3 server.py
```

로컬에서 Docker 이미지를 직접 빌드하려면 오버라이드를 함께 사용합니다.

```bash
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

## 포함된 기능

- 반응형 데스크톱·모바일 화면
- 날짜 이동 및 직접 선택
- KBO 공식 일정·예고 선발·라인업·시즌 기록 수집
- 예고 선발 ERA·WHIP, 최근 3일 불펜, 1군 엔트리, 구장·날씨를 반영한 `stats-v5-context-value` 승률
- 실제 moneyline 배당의 무마진 시장확률과 모델 확률을 비교한 역배 EV 레이더와 경기별 `PICK / NO BET` 판정
- 역배 EV·엣지·정배 대비 EV 우위가 모두 기준을 넘을 때만 추천
- 경기별 예측 근거 열기·닫기
- 전체·포수·내야 포지션·좌익수·중견수·우익수·지명타자별 안타 확률 상위 3명
- 데이터 출처·갱신 시각·라인업 상태 표시
- 예측 시점별 SQLite 스냅샷과 Docker 볼륨 영속화
- 경기 종료 후 결과 자동 수집과 적중률·Brier Score 표시
- PostgreSQL 전환용 스키마
- 저장된 데이터를 날짜순으로 평가하고 CSV로 내보내는 백테스트 도구
- 15분 주기의 자동 스냅샷·결과 수집기
- 검증 성능이 개선될 때만 활성화되는 Platt 확률 보정 학습기
- 좌투·우투·언더 유형별 시즌 타격 스플릿을 반영한 안타 확률

## 계산 범위와 한계

- 경기 승률은 팀 득점/경기, 팀 ERA, 출루율, 예고 선발, 불펜 부하, 1군 엔트리 이탈, 구장·날씨와 홈 이점을 결합한 설명형 통계 추정치입니다.
- KBO는 부상 진단을 공식 구조화 데이터로 제공하지 않으므로, 부상 확정 대신 `1군 엔트리 이탈`로 표시합니다.
- 역배 기대수익은 `모델 승률 × decimal 배당 - 1`입니다. 배당과 예측은 수익을 보장하지 않습니다.
- 안타 확률은 시즌 타율과 상대 선발 유형별 타율을 표본 보정한 뒤 상대 선발 ERA와 타순별 예상 타수를 반영합니다.
- 아직 과거 경기로 학습하거나 백테스트한 ML 모델이 아닙니다. 결과는 참고용이며 경기 결과를 보장하지 않습니다.
- 공식 라인업 발표 전에는 KBO 게임센터가 제공하는 최근 라인업을 사용합니다.

상세 계산식과 데이터 누수 방침은 [모델 카드](docs/model-card.md)에 정리되어 있습니다.

## 데이터와 백테스트

단일 컨테이너의 기본 저장소는 `/data/playball.db` SQLite 파일입니다. 관리형 PostgreSQL로 전환할 때 사용할 DDL은 [schema/postgresql.sql](schema/postgresql.sql)에 있습니다.

누적된 예측을 시간순으로 평가하고 CSV 데이터셋으로 내보냅니다.

```bash
docker compose exec playball python scripts/backtest.py --db /data/playball.db
docker compose exec playball python scripts/backtest.py \
  --db /data/playball.db \
  --export /data/evaluated_predictions.csv
```

종료 경기 100개 이상이 쌓이면 날짜 앞 80%로 학습하고 뒤 20%로 검증하는 확률 보정기를 만들 수 있습니다. 검증 Brier Score가 개선되지 않으면 파일을 생성하지 않습니다.

```bash
docker compose exec playball python scripts/train_calibrator.py \
  --db /data/playball.db \
  --output /data/calibration.json
```

Docker에서는 `/data/calibration.json`이 자동으로 감지됩니다.

과거 경기의 현재 시즌 최종 기록을 이용해 과거 예측을 재구성하면 미래 정보가 섞이므로 그렇게 하지 않습니다. 데이터셋은 실제 경기 전에 저장된 스냅샷부터 축적합니다.

## 남은 구현 순서

1. 충분한 경기 전 스냅샷 축적 후 보정 학습기 활성화
2. 축적 결과로 구장 득점 계수와 역배 EV 임계값 재검증
