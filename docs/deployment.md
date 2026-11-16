# Deployment and development

`make up` builds the React assets and Python container, starts PostgreSQL and Redis, runs the Alembic/seed service, then starts the API and worker. The published application port binds only to loopback. Set `PUBLIC_PORT` and update `ALLOWED_ORIGINS` together when using another host port. The worker reaches the API at `http://app:8000`.

| Setting | Meaning |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL. Default native storage is `sqlite:///./browserbridge.db`. |
| `REDIS_URL` | Optional bounded wakeup queue. SQL polling remains active as a fallback. |
| `BROWSER_URL`, `BROWSER_CHROMIUM` | Authorized local origin and optional browser executable. |
| `MODEL_API_KEY`, `MODEL_NAME`, `MODEL_URL` | Server-only compatible chat completions provider with image input. |
| `ALLOWED_ORIGINS`, `SECURE_COOKIE` | Exact browser origins and HTTPS-only cookies. |
| `EVIDENCE_RETENTION_SECONDS` | Masked evidence TTL, default seven days. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Optional full OTLP HTTP traces endpoint. |
| `DEMO_IDENTITY`, `DEMO_PASSWORD` | Local login switch and shared synthetic account password. |

The API exposes `/health/live`, database-backed `/health/ready`, and aggregate run counts at `/health/metrics`. The worker emits JSON transition logs containing run ID, sequence, event and state. OTLP spans include run or request correlation IDs.

For a native smoke test on a different port, set `BROWSER_URL=http://127.0.0.1:8010` and `ALLOWED_ORIGINS=http://127.0.0.1:8010`. The smoke script starts its own API and worker on that origin. Compose runs its smoke client against the internal `BROWSER_URL`.

After `make benchmark-smoke`, copy the reports from the worker with `docker compose cp worker:/tmp/browserbridge-smoke ./benchmark-smoke`. On native startup, export variables in the same shell as the API, worker and harness. Chromium needs OS libraries. If the Playwright downloader is unavailable and you have a compatible distro Chromium or headless shell, set `BROWSER_CHROMIUM` to its absolute path.

Local benchmark runs persist synthetic fixtures in separate tenants. For a fresh local evaluation, use a dedicated database or temporary SQLite path. `docker compose down -v` deletes the local demo volumes and all their data.
