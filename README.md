# IAMS Starter Application (Python)

This repository includes an executable starter implementation for core IAMS domain logic:

- Risk scoring (inherent/residual + rating bands)
- Engagement/finding lifecycle state transitions with authorization guards and allowed-next-state workflow error guidance
- Finding lifecycle with SoD enforcement
- Overdue finding automation
- Due-date rescheduling workflow with authorization and overdue recovery
- Closed/rejected findings are protected from due-date rescheduling
- Finding-level timeline retrieval from immutable audit events (optional validated+trimmed string event-type filter from known event names, strict positive integer limit, and non-negative offset)
- Timeline page API with `total`/`count` plus `has_more`/`next_offset` metadata for UI pagination (server-enforced max page size), including validated `asc`/`desc` sort order support
- Immutable audit event hash chain integrity checks and strict emitted event-type allowlist guardrails (with canonical allowed-event listing support)
- Immutable events now include structured metadata (e.g., state transitions and due-date changes) that is scalar-validated, serialized in timeline API responses, and protected by deterministic hash-chain payloads
- Duplicate ID and not-found guardrails via domain errors
- Remediation action-plan assignment with role/workflow validation
- IN_REMEDIATION transition requires a validated remediation plan assignment
- Standardized finding severity validation and KPI summary metrics
- Finding creation constrained to active engagement lifecycle states
- Injectable time provider for deterministic validation/testing
- JSON-serializable snapshot export (datetime/enum normalization)

## Run tests

```bash
python -m unittest discover -s tests -v
```
