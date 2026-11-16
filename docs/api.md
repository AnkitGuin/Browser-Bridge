# API

The development OpenAPI explorer is at `/docs`. The console APIs are under `/api/v1`. Portal forms are separate HTML endpoints at `/crm`, `/support` and `/inventory`.

```bash
base=http://127.0.0.1:8000
curl -fsS -c session.cookies -H "Origin: $base" -H 'Content-Type: application/json' \
  -d '{"email":"operator@demo.test","password":"demo-local-only"}' "$base/api/v1/auth/login"
curl -fsS -b session.cookies -H "Origin: $base" -H 'Content-Type: application/json' \
  -d '{"goal":"Escalate unresolved high-priority Team A tickets older than 48 hours.","mode":"dom"}' \
  "$base/api/v1/runs"
```

Use a local scratch path for the cookie jar and remove it when finished. Create endpoints return a run ID. Error objects include `code`, `message`, `correlation_id` and optional validation locations.

| Endpoint | Behavior |
|---|---|
| `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Session setup and principal scope |
| `POST /runs`, `GET /runs`, `GET /runs/{id}` | Submit, filter and inspect runs |
| `GET /runs/{id}/steps?after=0&limit=50` | Ordered, cursor-paged event history |
| `GET /runs/{id}/events` | SSE replay with `Last-Event-ID` or `after` cursor |
| `POST /runs/{id}/pause`, `/resume`, `/cancel` | Owner control with durable status |
| `GET /runs/{id}/evidence/{evidence_id}` | Scoped masked PNG, hash in ETag |
| `GET /runs/{id}/report?format=json` | JSON, CSV or Markdown results |
| `GET /approvals`, `POST /approvals/{id}/decision` | Pending scoped review and exact action decision |
| `GET /benchmark/cases`, `POST /benchmark/runs` | Scenario metadata and queue evaluation |
| `GET /benchmark/runs`, `GET /benchmark/runs/{id}/results` | Evaluation history and current results |
| `GET /settings` | Retention, limits and configured model name |

A decision body contains `action_hash`, `decision` (`approve` or `deny`) and optional `reason`. Approvals expire by the run deadline or after 600 seconds, whichever comes first.

Use an affirmative escalation objective in the form shown above. The parser rejects extra clauses that could change which tickets are written. You can append `Check each linked CRM customer.` The escalation workflow checks CRM either way.

SSE IDs are per-run sequence numbers. A terminal run sends an `end` event after delivering its remaining history. Events carry `event_id`, `run_id`, `sequence`, timestamp, event type, before/after states, step and correlation IDs, and a scrubbed payload.
