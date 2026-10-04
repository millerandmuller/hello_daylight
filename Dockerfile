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
# Cloud Run sets PORT. The app reads the client address from the last X-Forwarded-For entry itself.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-proxy-headers"]
