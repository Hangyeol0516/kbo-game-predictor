FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/Hangyeol0516/kbo-game-predictor" \
      org.opencontainers.image.description="PLAYBALL KBO game prediction and underdog value analysis"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KBO_HOST=0.0.0.0 \
    KBO_PORT=8000 \
    PLAYBALL_DB_PATH=/data/playball.db \
    PLAYBALL_CALIBRATION_PATH=/data/calibration.json \
    PLAYBALL_ODDS_CACHE_SECONDS=3600 \
    PLAYBALL_COLLECT_INTERVAL_SECONDS=900

WORKDIR /app

RUN addgroup -S -g 10001 appuser \
    && adduser -S -D -H -u 10001 -G appuser appuser \
    && install -d -o appuser -g appuser /data

COPY --chown=appuser:appuser server.py kbo_analysis.py storage.py index.html app.js styles.css ./
COPY --chown=appuser:appuser scripts ./scripts

USER appuser

EXPOSE 8000

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python", "server.py"]
