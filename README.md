# PLAYBALL — KBO 예측 앱 프로토타입

KBO 당일 경기 승률과 포지션별 1안타 이상 확률을 보여주는 프로토타입입니다. 일정, 예고 선발, 라인업과 시즌 기록은 KBO 공식 홈페이지에서 가져옵니다.

## 권장 배포: 단일 컨테이너

Docker Compose로 앱 컨테이너 하나와 영속 데이터 볼륨을 실행합니다. PostgreSQL 컨테이너는 필요하지 않습니다.

```bash
git clone https://github.com/Hangyeol0516/kbo-game-predictor.git
cd kbo-game-predictor
docker compose up -d --build
```

브라우저에서 <http://localhost:8000>을 엽니다. 포트를 바꾸려면 `PLAYBALL_PORT=8080 docker compose up -d`처럼 실행합니다.

업데이트와 로그 확인:

```bash
git pull
docker compose up -d --build
docker compose logs -f
```

Compose 없이 Docker만 사용할 수도 있습니다.

```bash
docker build -t kbo-game-predictor:latest .
docker run -d \
  --name kbo-game-predictor \
  --restart unless-stopped \
  -p 8000:8000 \
  -v playball_data:/data \
  kbo-game-predictor:latest
```

컨테이너는 비루트 사용자로 실행되며 `/health`로 상태를 확인합니다. 예측 스냅샷은 `playball_data` 볼륨에 보존됩니다.

## 로컬 개발

Python 3.12 이상에서 외부 패키지 없이 실행할 수 있습니다.

```bash
python3 server.py
```

## 포함된 기능

- 반응형 데스크톱·모바일 화면
- 날짜 이동 및 직접 선택
- KBO 공식 일정·예고 선발·라인업·시즌 기록 수집
- 예고 선발 ERA·WHIP을 반영한 `stats-v2-starter` 승률
- 경기별 예측 근거 열기·닫기
- 전체·포수·내야 포지션·좌익수·중견수·우익수·지명타자별 안타 확률 상위 3명
- 데이터 출처·갱신 시각·라인업 상태 표시
- 예측 시점별 SQLite 스냅샷과 Docker 볼륨 영속화
- 경기 종료 후 결과 자동 수집과 적중률·Brier Score 표시
- PostgreSQL 전환용 스키마
- 저장된 데이터를 날짜순으로 평가하고 CSV로 내보내는 백테스트 도구

## 계산 범위와 한계

- 경기 승률은 팀 득점/경기, 팀 ERA, 출루율, 예고 선발 ERA·WHIP과 홈 이점을 결합한 설명형 통계 추정치입니다.
- 안타 확률은 시즌 타율을 리그 평균으로 표본 보정한 뒤 상대 선발 ERA와 타순별 예상 타수를 반영합니다.
- 아직 과거 경기로 학습하거나 백테스트한 ML 모델이 아닙니다. 결과는 참고용이며 경기 결과를 보장하지 않습니다.
- 공식 라인업 발표 전에는 KBO 게임센터가 제공하는 최근 라인업을 사용합니다.

상세 계산식과 데이터 누수 방침은 [모델 카드](docs/model-card.md)에 정리되어 있습니다.

## 데이터와 백테스트

단일 컨테이너의 기본 저장소는 `/data/playball.db` SQLite 파일입니다. 관리형 PostgreSQL로 전환할 때 사용할 DDL은 [schema/postgresql.sql](schema/postgresql.sql)에 있습니다.

누적된 예측을 시간순으로 평가하고 CSV 데이터셋으로 내보냅니다.

```bash
python3 scripts/backtest.py --db data/playball.db
python3 scripts/backtest.py --db data/playball.db --export data/evaluated_predictions.csv
```

과거 경기의 현재 시즌 최종 기록을 이용해 과거 예측을 재구성하면 미래 정보가 섞이므로 그렇게 하지 않습니다. 데이터셋은 실제 경기 전에 저장된 스냅샷부터 축적합니다.

## 남은 구현 순서

1. 최근 3일 불펜 투구 수와 연투 여부 수집
2. 타자·투수 좌우 유형과 상대 전적 수집
3. 충분한 경기 전 스냅샷 축적
4. 날짜순 검증과 calibration을 거친 학습 모델 도입
