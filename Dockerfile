FROM node:24.11.1-bookworm-slim AS frontend
WORKDIR /build
COPY console/package*.json ./
RUN npm ci --no-audit --no-fund
COPY console/ ./
RUN npm run build

FROM python:3.12.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BROWSER_CHROMIUM=/usr/bin/chromium
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 bridge
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY alembic.ini ./
COPY migrations ./migrations
COPY bridge ./bridge
COPY benchmarks ./benchmarks
COPY scripts ./scripts
COPY --from=frontend /build/dist ./console/dist
USER bridge
EXPOSE 8000
CMD ["python","-m","uvicorn","bridge.main:app","--host","0.0.0.0","--port","8000"]
