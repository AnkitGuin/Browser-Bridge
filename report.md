# BrowserBridge project report

## What it does

BrowserBridge is a local workflow automation prototype. An operator enters a supported objective in a React console, and a Python worker uses Playwright to perform it through synthetic support, CRM and inventory web portals. The console exposes run history, an event timeline, approval requests, redacted screenshot evidence and JSON, CSV and Markdown exports.

The six supported workflows are ticket escalation, ticket lookup, CRM lookup, stock lookup, stock transfer and reorder requests. The planner maps supported objectives to typed tasks. The default Hybrid mode uses semantic selectors and can fall back to a configured vision provider. With no provider configured, it uses the DOM path.

## The parts worth talking about

| Feature | What to explain in an interview |
|---|---|
| Cross-portal workflow | The worker paginates through the support desk, filters by team, status, priority and age, checks each linked CRM record, then revisits the ticket. |
| Write verification | The worker persists an intent before clicking. A fresh UI read checks the saved business effect and record versions. A separate SQL grader checks authoritative state in synthetic cases. |
| Approvals | Inventory writes require a second authorized user to approve the exact operation, product, quantities and versions. The original requester cannot approve their own request. |
| Recovery | A replacement worker resumes from a checkpoint. It looks for the original intent's effect before it considers another attempt. Recovery scenarios include a worker process exiting immediately after a write. |
| Stack | Python, FastAPI, SQLAlchemy, PostgreSQL/SQLite, Redis, Playwright, React and TypeScript. The database holds durable records. Redis wakes the worker. |

## Fixes in this version

- Escalation objectives now use an explicit command form. Extra clauses that could change the target set are rejected before the worker receives a task.
- Pause, cancel and deadline checks now run under the same lock as approval parking and run completion. Resuming clears the earlier stop reason, and parked runs report a timeout when their deadline passes.
- A later workflow failure preserves earlier verified results as partial success. Intent verification and its result checkpoint commit together so recovery sees a consistent record.
- Live run streams reevaluate the active session's permissions. Approval events include the captured evidence reference, and ambiguous pagination stops with an explicit error.
- The smoke script uses the configured browser origin and starts its local worker after its own API is ready. The Docker smoke client defaults to the internal application origin.

## Verification

The ZIP contains the source tested at commit `77b409b`. The checks below used Python 3.12, Node 24 and Chromium Headless Shell 151.

| Check | Result |
|---|---|
| Full test suite | **41 passed** with PostgreSQL 16.2 and Redis 8.0.2, including 22 browser-marked tests. |
| Freshly extracted ZIP | `make local-install` rebuilt the console and initialized SQLite. The first smoke test verified five escalations. A repeat at a different local port returned five no-ops and recorded zero new write intents. |
| Database schema | Alembic upgrade and drift checks passed on PostgreSQL and SQLite. |
| Build and dependency checks | Ruff lint and formatting, TypeScript type checking and the production console build passed. npm audit and pip-audit reported no known vulnerabilities in the locked dependencies at the time of the check. |

### Seeded benchmark rerun

Credential-free Hybrid passed **115/120 cases (95.8%)** on seed 42. The five failures occurred in the masked-label layout, where the escalation control could not be grounded with semantic selectors and a vision provider was unavailable.

| Measure | Fresh result |
|---|---:|
| Recovery scenarios passed | 11 / 11 |
| Claimed writes independently verified | 324 / 324 |
| Duplicate effects observed | 0 / 324 committed effects |
| Unauthorized effects observed | 0 / 15 forbidden-action opportunities |

In the eight-case smoke comparison, Scripted, DOM and credential-free Hybrid each passed 8/8. The Vision mode requires a live compatible provider and was unavailable.

To reproduce the full evaluation, start the API on a fresh local database, then run `.venv/bin/python -m benchmarks.run --suite local120 --mode hybrid --seed 42`. The benchmark seeds isolated synthetic tenants and grades the resulting database state.

## Résumé wording

Use the verbs that match your own contribution. For example, “built” fits work you implemented and can explain, while “validated” fits work you reproduced or tested.

**Systems engineering**

> Built a Playwright workflow automation prototype for three synthetic business portals, with role-based approvals, durable action checkpoints, and independent post-write verification.

**Evaluation**

> Validated a browser automation worker on 120 seeded synthetic scenarios, reaching 95.8% task success and independently verifying 324 writes with zero observed duplicate effects.

If you have room for one line, use the systems bullet. In an interview, explain what happens if the process exits after clicking Submit and before saving a successful result. Walk through the persisted intent, the next worker's fresh read and the decision to verify or retry. That example shows both the orchestration and the evaluation.

## Scope

The evidence covers the three synthetic portals and their seeded variants. General-purpose website navigation, deployment with production identity, live multimodal performance and a full Docker Compose startup were outside this verification. The vision adapter was exercised with a mock service. Treat the benchmark's success rate as the fraction of seeded scenarios that passed the independent grader.
