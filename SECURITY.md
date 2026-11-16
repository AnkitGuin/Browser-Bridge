# Security

The repository contains synthetic data and a development identity provider. Bind demo deployments to loopback. Operational deployment requires separate identity integration, TLS, database role isolation and managed egress controls.

| Boundary | Enforcement |
|---|---|
| Tenant and role | Server-side sessions, tenant-scoped database queries, Team A membership and composite tenant foreign keys |
| Business writes | Operation-specific authorizers, transactional state changes, optimistic versions and durable audit events |
| Browser run | Short-lived cookie bound to the worker lease, authorized app routes, no operator API access |
| Approval | Single action hash, current requested parameters, expiry and a different active manager in the same tenant |
| Web origin | SameSite HttpOnly cookies, strict configured Origin/Referer, CSP and same-origin forms |
| Browser network | Local or Compose host, permitted paths, redirect inspection, disabled service workers and blocked WebSockets |
| Model | Untrusted screenshot prompt, masked fields, normalized points and hit tests against scoped controls |
| Evidence | Tenant/run authorization, SHA-256 hashes, configurable TTL and screenshots masked at capture |

Set `DEMO_IDENTITY=false` to disable seeded account login. Compose ships with local test credentials. Supply your own identity adapter before permitting external users.

See [threat model](docs/threat-model.md) and [deployment](docs/deployment.md) for the verified controls and deployment boundaries.
