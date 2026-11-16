# Evaluation methodology

The generator creates 120 case definitions in five families. The repository-held-out split changes seeds, data combinations and scenario parameters. Public source makes the split inspectable.

| Family | Cases | Examples |
|---|---:|---|
| Single application | 30 | CRM and ticket lookups, stock inspection, approved transfers and reorders |
| Cross application | 30 | Ticket/CRM reconciliation across teams, priorities, age thresholds and missing links |
| Layout | 30 | Renamed controls, reversed columns, modal, delayed response, masked accessible label and compact pages |
| Recovery | 15 | Timeout before/after commit, rate limit, worker death, false success and a competing version |
| Security | 15 | Injected instructions, wrong role/team, session expiry and approval denial or expiry |

Each case has a fixed UTC clock. The harness creates a fresh synthetic tenant for each case/mode pair. Browser runs receive their typed objective and scoped identity. The benchmark module computes ground truth by independently reading before/after SQL snapshots and audit rows. It checks target identity, report reconciliation, authorization, exact inventory quantity and version, no-op boundaries and expected safe stops. The crash case terminates an actual worker subprocess after the browser submits, expires its lease and starts a replacement execution.

Run modes share the typed controller, action budgets and verifier. Scripted uses the first fixed locator name. DOM uses scoped semantic alternatives. Vision requests screen coordinates for interactive clicks and verifies the hit target. Hybrid uses the DOM branch when it resolves safely. Structured DOM reads, form field entry and authorization remain shared across the three agent modes, so the experiment isolates target grounding within the shared workflow.

The smoke suite selects eight named cases. The full suite includes 35 development and 85 repository-held-out cases. Outcome labels include safe handoff or denial where specified. An unavailable provider has a null score and is excluded from evaluated-case denominators.

## Metric conventions

- End-to-end task success is the number passing every required check divided by evaluated cases. The interval is the 95% Wilson binomial interval.
- Partial completion is the fraction of eligible ticket effects seen in authoritative audit records. For single-record tasks it uses the all-checks result.
- The unauthorized-effect denominator counts security scenarios with a forbidden-action opportunity. Policy denials and blocked requests are recorded separately.
- Recovery success includes cases with retry or restart opportunities. Safe conflict detection and false-toast rejection are graded as their own scenarios.
- Latency p50/p95 and action counts are computed from all evaluated runs, with separate successful-run action summaries.
- Duplicate effects and verification coverage use committed-effect and claimed-mutation denominators, respectively.
- Mode deltas pair runs by scenario ID. The aggregate JSON groups results by family, layout, fault and split. It records model tokens, but provider cost remains unavailable without a pricing input.

The generated cases evaluate the local synthetic environment. WorkArena remains an external evaluation option.
