FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KBO_HOST=0.0.0.0 \
    KBO_PORT=8000

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser . /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python", "server.py"]
