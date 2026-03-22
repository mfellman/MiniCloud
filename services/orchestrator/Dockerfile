# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS runtime
RUN useradd --create-home --uid 10001 appuser
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY app ./app
COPY workflows ./workflows
COPY connections ./connections
USER appuser
EXPOSE 8080
ENV WORKFLOWS_DIR=/app/workflows
ENV CONNECTIONS_DIR=/app/connections
ENV TRANSFORMERS_URL=http://transformers:8080
ENV EGRESS_HTTP_URL=http://egress-http:8080
ENV EGRESS_FTP_URL=http://egress-ftp:8080
ENV EGRESS_SSH_URL=http://egress-ssh:8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
