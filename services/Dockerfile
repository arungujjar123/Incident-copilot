FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG SERVICE
COPY common/ common/
COPY ${SERVICE}/ ${SERVICE}/

ENV SERVICE_DIR=${SERVICE} \
    PYTHONUNBUFFERED=1
# Web services have a PORT; loadgen doesn't, so it runs as a plain script
CMD if [ -n "$PORT" ]; then exec uvicorn "$SERVICE_DIR.main:app" --host 0.0.0.0 --port "$PORT" --no-access-log; \
    else exec python -m "$SERVICE_DIR.main"; fi