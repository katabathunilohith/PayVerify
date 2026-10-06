FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INSTANCE_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY payverify ./payverify
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app

# Database, screenshots and the generated secret key live here: mount a volume.
VOLUME ["/data"]
EXPOSE 8000

# One worker (SQLite + in-process background jobs), several threads.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT:-8000} 'payverify:create_app()'"]
