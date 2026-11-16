# Threat model

The trusted boundary comprises the user's authenticated principal, the backend role and tenant policy, and a distinct authorized approver. Browser text, notes, labels, images and provider outputs are untrusted.

| Threat | Control | Verification |
|---|---|---|
| Ticket text asks to export customer emails | Workflow uses typed objectives and scope. The description is excluded from semantic observations and masked in screenshots. | Injection case and browser origin tests |
| Cross-tenant record lookup or forged ID | Every business detail uses tenant scope and relevant role restrictions. Database foreign keys prohibit cross-tenant links. | Direct cross-tenant portal and report API tests |
| Browser calls privileged APIs | Run session cookies fail the human-console dependency. The browser routes only to authorized local portal paths. | Run session/API and browser URL tests |
| CSRF | Origin or Referer must match configuration, with HttpOnly SameSite=Strict cookies. | Forged Origin API test |
| Disabled or wrong visual target | One actionable, scoped target and coordinate hit testing. Role/name ambiguity is an explicit error. | Browser targeting and synthetic multimodal transport tests |
| Forged or expired approval | Exact digest, owner separation, role, expiry, lease and parameter revalidation on write. | API tests and denied/expired benchmark cases |
| Duplicate or racing updates | Intent ledger, optimistic versions, transactional row locks and fresh verification. | Concurrent transfer, timeout and worker-restart tests |
| Session or evidence leakage | Distinct contexts, cookie digests, lease grants, scoped evidence access and retention. | Session expiry and evidence access tests |
| Browser redirects or URL escape | Route allowlist, interception of redirect responses, no file or arbitrary shell actions. | Browser policy tests |

Screenshots are masked for marked private content, form inputs and untrusted ticket descriptions. The demo uses synthetic identifiers and company names. This is markup-based redaction and requires review when adapting the portals to new fields. Browser traffic restrictions are application-layer controls. A deployment with real data needs an independent network boundary, PII classification and limited database roles. Business records, run history and benchmark tenants persist until the local database is reset or explicitly archived.
