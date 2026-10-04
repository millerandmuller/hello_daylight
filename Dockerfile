# One image, two entry points: the web service (default CMD) and the night job (`-m app.night`).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/srv/services/web
WORKDIR /srv
COPY services/web/requirements.txt services/web/requirements.txt
RUN pip install -r services/web/requirements.txt
COPY services/web services/web
RUN useradd --create-home --uid 10001 daylight && rm -rf services/web/tests
USER daylight
WORKDIR /srv/services/web
# Cloud Run sets PORT. Forwarded headers are trusted so the rate limit sees the visitor's address, not the proxy's.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
