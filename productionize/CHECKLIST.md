# Production-readiness checklist — master index (draft)

Synthesis of research wave 1: 11 domain reports in `research/`, **406 items** total.
This file is the registry; `research/<domain>.md` holds the full item bodies.

## Item schema (contract for all downstream tooling)

Every item carries:
- `id` (kebab-case, unique within the registry after dedupe)
- `what` / `why` — one sentence each
- `check`: `probe` | `judgment` | `user-decision`
- `probe`: concrete verification recipe when check=probe
- `applies_if`: web-api, spa, cli, library, mobile, ml-service, data-pipeline, monorepo, or `all`
- `severity`: critical | important | nice-to-have
- `sources`: canonical references

Distribution: 273 probe / 103 judgment / 27 user-decision (+3 judgment with probe hints) · 250 critical / 152 important / 4 nice-to-have.

## Domains

| # | Domain | File | Items | Focus |
|---|--------|------|------:|-------|
| 1 | Code quality & API design | `research/code-quality.md` | 35 | error taxonomy, input validation, 12-factor config, dependency hygiene, API versioning/pagination/rate-limit headers, concurrency safety |
| 2 | Testing & quality gates | `research/testing.md` | 32 | pyramid budget, contract testing, flake governance, hermeticity, fuzzing, migration tests, perf gates, CI merge checks |
| 3 | Reliability & resilience | `research/reliability.md` | 37 | health contracts, graceful shutdown, timeouts/retries/circuit breakers, backpressure, DR/backup restore, chaos drills |
| 4 | Scalability & performance | `research/scalability.md` | 39 | statelessness, connection pools, indexing/N+1/query budgets, caching layers & stampede control, LB/autoscaling, load/soak testing |
| 5 | Security | `research/security.md` | 37 | OIDC/OAuth correctness, sessions/CSRF, deny-by-default authz + IDOR, injection classes, secrets/Vault/KMS, supply chain (SLSA/SBOM), TLS/headers/CORS, IR |
| 6 | Observability | `research/observability.md` | 40 | structured logs, correlation, RED/USE, SLI/SLO/error budgets, OTel traces, alert quality/routing, dashboards-as-code, incident command, postmortems |
| 7 | Data integrity & database | `research/data.md` | 39 | expand/contract migrations, lock safety, backups/PITR/drills, replication/failover/fencing, transactions/outbox, DB constraints, GDPR erasure, PII inventory |
| 8 | Deployment & release | `research/deployment.md` | 37 | CI gates, build-once-promote, provenance, rollout strategies (rolling/blue-green/canary), automated rollback, feature flags, env parity, IaC/GitOps, DORA |
| 9 | LLMOps / AI features | `research/llmops.md` | 32 | prompt registry/evals/golden sets, model routing/fallback, token/cost budgets, streaming, guardrails/jailbreak, LLM tracing, RAG freshness/grounding, fine-tune lineage |
| 10 | Ops economics & product edge | `research/ops-economics.md` | 40 | unit cost/FinOps, env TTL, multi-tenant isolation + quotas, WCAG a11y, i18n, API deprecation/sunset, abuse economics, license/EOL, decommission |
| 11 | Documentation & DX | `research/docs-dx.md` | 38 | README contract, quickstart probe, runbooks, API docs, changelogs, ADRs, onboarding, docs accessibility |

## Cross-domain duplicates to merge in glossary wave

These items describe the same control from different angles — the glossary assigns ONE canonical id and cross-references the rest:

1. **Idempotency**: `scalability/retry-safe-mutations` + `data/idempotent-replay` + `ops-economics/idempotent-chargeable-operations` + `llmops/request-deduplication` → one node with per-surface probe variants (HTTP, jobs, consumers, LLM calls).
2. **Health checks**: `reliability/health-contract` + `reliability/liveness-scope` + `observability/health-check-separation` + `deployment/readiness-drain` + `scalability/health-check-and-drain` → one node.
3. **Secrets management**: `code-quality/no-hardcoded-secrets` + `security/vault-kms-secret-delivery` + `security/secret-rotation-revocation` + `security/secret-leak-prevention` + `deployment/secret-injection` + `deployment/secret-rotation` → one node.
4. **SBOM/provenance**: `security/release-sbom` + `security/provenance-signing-verification` + `deployment/artifact-provenance` + `ops-economics/sbom-per-shipped-artifact` + `ops-economics/build-provenance-reproducibility` → one node.
5. **PII redaction in telemetry**: `security/log-redaction` + `observability/pii-secret-redaction` + `llmops/pii-redaction` + `llmops/sensitive-trace-access` → one node.
6. **Retries/backoff/circuit breakers**: `reliability/*` (retry/breaker family) + `llmops/circuit-breaker-retry` + `llmops/provider-fallback-chain` → retry node + fallback-chain node (fallback is distinct).
7. **Rate limiting / quotas**: `security/abuse-rate-limiting` + `llmops/expensive-endpoint-rate-limit` + `ops-economics/tenant-quota-policy` + `ops-economics/authenticated-user-quota` + `ops-economics/noisy-neighbor-controls` → one quota-policy node + one abuse-friction node.
8. **SLOs & latency budgets**: `observability/sli-formalization` + `observability/slo-error-budget` + `scalability/endpoint-latency-and-query-budget` + `llmops/latency-slo` → SLI/SLO node + per-surface budget children.
9. **Migrations**: `data/migration-*` (8 items) + `deployment/migration-expand-contract` + `deployment/migration-backup-restore` + `testing/migration-upgrade-tests` → one migration cluster under data, deployment/testing items become edges/checks.
10. **Backups/DR**: `data/backup-*` + `data/pitr-capability` + `reliability` DR items → one backup/DR cluster.
11. **Environment isolation**: `deployment/environment-data-isolation` + `deployment/preview-data-safety` + `data/synthetic-nonprod` + `testing/privacy-safe-test-data` → one node.
12. **Release/version attribution in telemetry**: `observability/change-release-correlation` + `deployment/release-observability` → one node.
13. **Preview environments**: `deployment/preview-environments` + `testing/preview-environment-smoke` + `ops-economics/ephemeral-environment-ttl` → one node.
14. **Graceful shutdown/drain**: `reliability` shutdown items + `scalability/graceful-drain-on-scale-down` + `deployment/readiness-drain` → merged into health/lifecycle node.

Estimated post-merge registry: **~340 nodes**.

## Status

- **Wave 1 (research)**: done — `research/*.md`, 406 items across 11 domains.
- **Wave 2 (glossary)**: done — `glossary/*.md`, 406/406 expanded entries
  (definition/implementation/probe/failure_modes/severity/applies_if/sources),
  86 merges_into markers for the 16 canonical dedupe clusters.
- **Graph v1**: done — `graph/SCHEMA.md` + `driver/validate_graph.py`; pilot
  domain `graph/reliability.json` (37 nodes, 17 depends_on edges, validator
  clean). Node = one glossary entry; dedupe via `cluster` field, not merging.
- **Driver**: done — `driver/run.py` (plan / record / status; profile pruning,
  topo order, executor hints, JSONL verdict state). Smoke-tested.
- **Command**: done — `/trio-productionize` in `omp/commands/` and
  `opencode/commands/`.

## Next steps

1. Promote the remaining 10 domains to `graph/<domain>.json` (same recipe as
   the reliability pilot — one builder per domain, batches of 3).
2. Dogfood `/trio-productionize` on a real repo; tune probe recipes and
   batch sizes from the run's evidence quality.

## Provenance

Wave 1: 11 domain researchers (gpt-5.6-luna via omp subagents), disciplined to ≤2 batched web searches + knowledge-first authoring after earlier waves died to provider 402s, a ~3–4 min subagent wall, and 200K TPM org saturation. Throttled batches of 3 succeeded 9/9.
