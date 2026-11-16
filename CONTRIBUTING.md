# Contributing

Install and build with `make local-install`. Start the API and worker independently so each process can be restarted during recovery tests.

Run `make lint`, `make test` and `make test-e2e` before changing behavior. Browser tests require a built console and a functioning Chromium. Set `DATABASE_URL` and `REDIS_URL` in a test environment to exercise PostgreSQL and Redis. The test suite creates isolated synthetic tenants.

When adding a workflow, define its typed input, supported apps, policy and independent predicates. Add a generator case with an authoritative expected result. Run the grader against deliberate wrong or missing effects to confirm it fails. Keep state-changing actions behind scoped intents and verify them using a fresh read. Keep fixture labels and expected effects outside the browser runtime.
