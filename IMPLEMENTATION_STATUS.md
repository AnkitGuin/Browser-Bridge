# Implementation status

| Acceptance area | Result and evidence |
|---|---|
| Local startup and migration | Native smoke completed with five verified escalations. Alembic upgrade and schema check passed against a fresh PostgreSQL database. The Compose YAML and four pinned registry tags were validated. Container startup remains untested in the execution environment. |
| Gateway, worker and business portals | FastAPI and SQLAlchemy serve a React console plus CRM, support and inventory routes. PostgreSQL stores durable state and Redis wakes the worker. SQLite provides a native development fallback. |
| Permissions and tenancy | API and browser tests exercise role, origin, approval, cross-tenant, run scope and evidence restrictions. |
| Natural-language workflow | Six typed goal families support escalation, CRM/ticket/stock lookup, transfer and reorder. Task planning and sequencing use a deterministic controller. |
| Playwright and perception | Real Chromium executes scoped UI actions. DOM and Hybrid run without credentials. A mock provider transport verifies actual PNG input, normalized-point validation and Hybrid fallback. Live-provider behavior remains unverified. |
| Cross-app support | The browser enumerates pagination, deduplicates stable IDs, applies a strict age cutoff, resolves linked CRM records and reconciles every outcome. |
| Independent verification | Fresh UI reads check intent-bound business events, final revisions, linked records and stock invariants. The state-based grader separately checks authoritative SQL records. |
| Intent recovery | Tests cover timeout before and after commit, a real worker subprocess exit, misleading success, a version conflict, cancellation after submit and repeated actions. |
| Operator controls | Playwright drives task creation, status timeline, evidence, approval and the scenario lab. API tests cover event replay, cancel and report exports. |
| Benchmarks | The generator defines 120 scenarios. The full credential-free Hybrid run passed 115. Scripted, DOM and Hybrid each passed all eight smoke cases. Vision was unavailable. See EVALUATION.md for run artifacts. |
| Observability | Durable events, JSON run logs, redacted screenshots with TTL, a metrics endpoint and optional OTLP traces. OTLP export and raw Playwright traces are outside the verified path. |
| Documentation | Setup, demo, architecture, security, API, benchmark methodology and captured screenshots are included. |
| Test gate | 33 tests passed in 82.73 seconds with PostgreSQL 18.6, Redis 8.2.3 and Chromium headless shell 153. Ruff lint and format checks, the React/TypeScript build, npm audit and pip-audit passed. |

## Remaining scope

- The hosted multimodal adapter needs a compatible provider and credentials for end-to-end validation. Credential-free Hybrid leaves masked controls untouched when semantic targeting fails.
- The parser and planner support the documented workflow families. Model-proposed planning across unfamiliar sites and full vision-only record extraction remain outside the implemented workflow.
- The repository-held-out grader uses separate code and authoritative snapshots within the benchmark harness. Worker and grader database roles are shared in the local setup.
- The perturbation suite covers reordered layouts, alternate labels, compact pagination, confirmation dialogs, delayed responses, ambiguous outcomes and role/session faults. Infinite-scroll, iframe and virtualized-table variants remain outside the test suite.
- Compose deployment, OTLP export, BrowserGym and WorkArena still require verification or integration. The live test environment used native PostgreSQL and Redis processes.
