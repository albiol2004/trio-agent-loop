# Operations economics, product & edge concerns — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 3). Raw item list, pre-synthesis.

### billable-unit-definition
- **what**: Define a versioned billable-unit model that distinguishes requests, active users, tenants, background jobs, retries, storage, egress, and third-party calls.
- **why**: Ambiguous meters hide unprofitable workloads and create surprise invoices when retries or fan-out are counted differently.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.finops.org/framework/ ; https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html

### unit-cost-measurement
- **what**: Compute cost per successful request and monthly active user from metered provider, compute, storage, network, and vendor spend, splitting fixed and marginal portions.
- **why**: Aggregate spend can look healthy while one endpoint or customer cohort destroys contribution margin.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.finops.org/framework/ ; https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html

### cost-attribution-tags
- **what**: Attach stable service, environment, plan or tenant, operation, and version dimensions to billing events and traces while keeping metric dimensions bounded.
- **why**: Missing attribution turns an actionable overage into an unowned shared bill.
- **check**: probe
- **probe**: Parse metric schemas, trace resource attributes, and cloud cost tags; fail if cost-bearing records lack `service`, `environment`, `operation`, and an approved tenant or plan dimension.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/cost-alloc-tags.html ; https://opentelemetry.io/docs/specs/semconv/general/

### cost-alert-policy
- **what**: Set owner-backed alerts for absolute spend, spend rate, forecast, and cost per request or user with documented warning and paging thresholds.
- **why**: Waiting for the monthly invoice lets runaway jobs or abusive tenants continue for days.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html ; https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html

### cost-alert-delivery
- **what**: Test that cost and unit-cost alerts reach an on-call destination and include service, owner, tenant or plan, and runbook context.
- **why**: A perfectly configured alert that no owner sees cannot stop a spend incident.
- **check**: probe
- **probe**: Parse alert rules and notification routes, then fire a staging alert and assert that the received event contains `service`, `owner`, and `runbook_url`.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### cost-to-serve-benchmark
- **what**: Benchmark representative load and record p50 and p95 latency, throughput, and marginal cost per request or user at target and surge capacity.
- **why**: Optimizing only average latency can trade reliability for a nonlinear cost spike at saturation.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html ; https://cloud.google.com/architecture/framework/cost-optimization

### ephemeral-environment-ttl
- **what**: Give every preview, test, and temporary environment an owner, creation timestamp, explicit TTL, and automatic teardown.
- **why**: Orphaned environments retain compute, databases, IPs, and secrets indefinitely.
- **check**: probe
- **probe**: Parse IaC and CI configuration; fail when an ephemeral environment lacks `owner`, `created_at` or `expires_at`, or an automatic destroy path.
- **applies_if**: all
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/ ; https://developer.hashicorp.com/terraform/cli/commands/destroy

### scheduled-resource-cleanup
- **what**: Run a recurring inventory job that deletes expired resources and reports undeletable dependencies with an owner.
- **why**: TTL hooks fail silently when resources are created outside the normal workflow.
- **check**: probe
- **probe**: Parse scheduler configuration for a cleanup job and run its dry-run against fixtures containing an expired environment and a protected dependency.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html ; https://developer.hashicorp.com/terraform/cli/commands/destroy

### teardown-guardrails
- **what**: Make teardown default-deny for production and require dependency, backup, retention, and approval checks before destructive actions.
- **why**: An overbroad cleanup selector can destroy production data or shared infrastructure.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://developer.hashicorp.com/terraform/cli/commands/destroy ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### authenticated-tenant-context
- **what**: Derive tenant identity only from authenticated, server-validated context and carry it through authorization, storage, jobs, logs, and caches.
- **why**: Trusting a client-supplied tenant ID enables cross-tenant reads, writes, or cache poisoning.
- **check**: probe
- **probe**: Trace request and job context code and fail paths where a repository, cache, or queue call accepts a tenant identifier not sourced from authenticated context.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html ; https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations

### tenant-keyed-schema
- **what**: Include tenant ID in partition keys, foreign keys, uniqueness constraints, and every tenant-owned query boundary.
- **why**: A single missing predicate or globally unique key can leak, overwrite, or collide with another tenant's records.
- **check**: probe
- **probe**: Parse migrations, schema metadata, and query builders; fail tenant-owned tables or update and delete statements that lack a tenant key or composite constraint.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations ; https://www.postgresql.org/docs/current/ddl-rowsecurity.html

### database-row-isolation
- **what**: Enforce database row-level security or an equivalent deny-by-default policy independently of ORM filters, including background workers and admin paths.
- **why**: New code or an alternate code path can bypass application-level filters and expose the entire tenant dataset.
- **check**: probe
- **probe**: Parse database migrations and role grants; require RLS and policies or a documented equivalent on tenant tables and deny table access to roles that bypass them.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/ddl-rowsecurity.html ; https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

### tenant-scoped-secrets-and-logs
- **what**: Scope encryption keys, object-store prefixes, caches, logs, and backups by tenant and redact tenant data from shared operational channels.
- **why**: Shared backups and observability systems often become the overlooked cross-tenant exfiltration path.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://csrc.nist.gov/publications/detail/sp/800-57-part-1/rev-5/final ; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### noisy-neighbor-controls
- **what**: Enforce per-tenant concurrency, CPU and memory, queue, storage, and request-rate budgets with fair scheduling and backpressure.
- **why**: One high-volume or expensive tenant can exhaust shared pools and violate every other tenant's SLO.
- **check**: probe
- **probe**: Parse gateway, queue, and runtime policy; require tenant-keyed rate and concurrency limits plus a bounded queue or fair-share scheduler.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/policy/resource-quotas/ ; https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations

### tenant-quota-policy
- **what**: Publish and enforce quotas per tenant, plan, operation, and time window with an explicit overage or rejection policy and documented `429` and `Retry-After` behavior.
- **why**: An undocumented tenant cap creates either uncontrolled cost or a customer-visible outage with no predictable remedy.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://kubernetes.io/docs/concepts/policy/resource-quotas/

### authenticated-user-quota
- **what**: Apply a separate authenticated-user quota within the tenant so one credential cannot consume the tenant's entire allowance.
- **why**: Shared tenant quotas alone allow a compromised or runaway user to starve colleagues.
- **check**: probe
- **probe**: Parse quota keys and enforcement middleware; require the key tuple to include authenticated `user_id` and `tenant_id` for expensive operations.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://kubernetes.io/docs/concepts/policy/resource-quotas/

### quota-reservation-atomicity
- **what**: Reserve quota atomically before expensive work, release failed reservations, and make retries idempotent.
- **why**: Concurrent requests can oversubscribe limits or charge the same work multiple times when accounting occurs after execution.
- **check**: probe
- **probe**: Inspect quota-store operations and job admission; require an atomic conditional increment or reservation before enqueue or external spend and a compensating release on failure.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://docs.stripe.com/api/idempotent_requests

### wcag-22-aa-target
- **what**: Set WCAG 2.2 Level AA as the product target, document any exceptions, and assign an owner for remediation.
- **why**: Undefined accessibility criteria let keyboard, screen-reader, low-vision, and cognitive barriers ship as permanent defects.
- **check**: judgment
- **applies_if**: spa
- **severity**: critical
- **sources**: https://www.w3.org/TR/WCAG22/ ; https://www.w3.org/WAI/test-evaluate/

### automated-a11y-gates
- **what**: Run automated axe or equivalent accessibility checks on representative routes and critical states in CI.
- **why**: Missing names, roles, labels, contrast, and landmarks otherwise reach production unnoticed.
- **check**: probe
- **probe**: Parse CI workflows and test dependencies; require an accessibility job invoking `axe-core` or an equivalent scanner against authenticated and unauthenticated critical routes.
- **applies_if**: spa
- **severity**: important
- **sources**: https://github.com/dequelabs/axe-core ; https://www.w3.org/WAI/test-evaluate/

### manual-a11y-journeys
- **what**: Manually verify keyboard-only navigation, focus order, visible focus, screen-reader announcements, zoom and reflow, and error recovery for sign-in, purchase, and destructive flows.
- **why**: Automated scanners cannot establish task completion, focus behavior, or meaningful announcements.
- **check**: judgment
- **applies_if**: spa
- **severity**: critical
- **sources**: https://www.w3.org/TR/WCAG22/ ; https://www.w3.org/WAI/ARIA/apg/

### i18n-string-catalogs
- **what**: Externalize all user-visible strings, plural and select rules, validation messages, and accessibility labels into versioned locale catalogs.
- **why**: Concatenated literals and missing translations produce broken grammar, untranslated errors, and inaccessible controls.
- **check**: probe
- **probe**: Parse UI source and fail user-facing literal assignments outside approved locale catalogs or translation APIs, then verify every default-locale key has a catalog entry.
- **applies_if**: spa
- **severity**: important
- **sources**: https://cldr.unicode.org/ ; https://www.w3.org/International/

### locale-aware-formatting
- **what**: Use locale-aware ICU or `Intl` formatting for dates, numbers, currencies, units, names, and time zones rather than hand-built strings.
- **why**: Hard-coded separators, currencies, and time zones display incorrect values or legally misleading prices in other locales.
- **check**: probe
- **probe**: Static-scan UI and presentation code for locale-unsafe date and number formatting and require locale and zone arguments or approved ICU wrappers for displayed values.
- **applies_if**: spa
- **severity**: critical
- **sources**: https://cldr.unicode.org/ ; https://www.w3.org/International/

### rtl-and-expansion-locales
- **what**: Test long translations, plural forms, bidirectional scripts, RTL layouts, Unicode normalization, and locale fallback before release.
- **why**: Text expansion or bidi controls can clip controls, reverse meaning, or make an otherwise functional workflow unusable.
- **check**: probe
- **probe**: Parse supported-locale configuration and DOM or screenshot fixtures; require at least one RTL and one expansion locale plus fallback and normalization cases.
- **applies_if**: spa
- **severity**: important
- **sources**: https://www.unicode.org/reports/tr9/ ; https://www.unicode.org/reports/tr15/

### deterministic-locale-negotiation
- **what**: Determine locale from an explicit user or request setting with an allowlist and deterministic fallback, and include the locale in any response-cache variant key.
- **why**: Inconsistent negotiation displays the wrong language and causes cache variants to serve one user's translation to another.
- **check**: probe
- **probe**: Parse locale middleware and cache-key construction; require allowlisted BCP 47 locales, deterministic fallback, and locale inclusion in every localized response cache key.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.w3.org/International/questions/qa-html-language-declarations ; https://cldr.unicode.org/

### api-deprecation-policy
- **what**: Publish a versioned API deprecation policy with notice period, support window, owner, migration guide, and exception process.
- **why**: Consumers cannot plan upgrades when a breaking change appears without an enforceable sunset contract.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9745 ; https://www.rfc-editor.org/rfc/rfc8594

### deprecation-sunset-signals
- **what**: Mark deprecated responses with standards-based `Deprecation` and `Sunset` headers and expose equivalent documentation and changelog metadata.
- **why**: Client operators and automated tooling miss a prose-only warning and discover removal only after failures.
- **check**: probe
- **probe**: Curl each deprecated endpoint or version and assert valid `Deprecation` and `Sunset` headers, a migration link, and a documented removal date.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9745 ; https://www.rfc-editor.org/rfc/rfc8594

### sunset-consumer-inventory
- **what**: Identify active clients, versions, tenants, and request volume before a sunset and notify owners through an auditable channel.
- **why**: Removing an endpoint with unknown consumers creates avoidable outages and emergency rollback.
- **check**: probe
- **probe**: Parse access-log and telemetry schemas and the deprecation runbook; require client, version, and tenant fields plus a report query covering the proposed sunset window.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc8594 ; https://spec.openapis.org/oas/latest.html

### sunset-removal-response
- **what**: Remove a sunset API only after the announced date and migration evidence are recorded, and return a documented `410 Gone` response or equivalent final status.
- **why**: Premature removal breaks clients, while indefinite compatibility leaves unsupported code and security exposure alive.
- **check**: judgment
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9110#name-410-gone ; https://www.rfc-editor.org/rfc/rfc8594

### expensive-operation-friction
- **what**: Require progressive friction such as CAPTCHA, proof-of-work, step-up authentication, or manual review before anonymous or low-trust expensive operations.
- **why**: Attackers can convert free requests into paid inference, messaging, scraping, or third-party calls faster than rate limits detect them.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://owasp.org/www-project-automated-threats-to-web-applications/ ; https://cloud.google.com/recaptcha/docs

### expensive-operation-caps
- **what**: Bound payload size, fan-out, recursion, execution time, concurrency, retries, and third-party spend per operation and tenant.
- **why**: A single crafted request can trigger unbounded compute, queue growth, or vendor charges even when request rate is low.
- **check**: probe
- **probe**: Parse route schemas and job definitions; require finite maximums for bytes, items, depth, duration, retries, concurrency, and external-call budget on expensive operations.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html

### idempotent-chargeable-operations
- **what**: Require idempotency keys and durable deduplication for chargeable mutations, jobs, emails, payments, and external side effects.
- **why**: Client retries and queue redelivery can duplicate work and external charges.
- **check**: probe
- **probe**: Parse API schemas, routes, and worker admission code; require `Idempotency-Key` or an equivalent deduplication key persisted before executing each chargeable operation.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.stripe.com/api/idempotent_requests ; https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/

### abuse-cost-kill-switch
- **what**: Attribute suspicious spend to a tenant and operation and provide a kill switch that stops new expensive work without corrupting in-flight state.
- **why**: Teams cannot contain a cost attack or fairly charge it when telemetry aggregates all tenants.
- **check**: probe
- **probe**: Parse incident controls and job admission code; require an operator or API kill switch keyed by tenant and operation plus a traceable audit event.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html

### sbom-per-shipped-artifact
- **what**: Generate an SPDX or CycloneDX SBOM for every shipped image, binary, package, and installer, including transitive dependencies and license identifiers.
- **why**: Missing provenance and transitive license data makes incident response and notice obligations impossible.
- **check**: probe
- **probe**: Parse the release workflow and inspect a sample artifact; require a machine-readable SPDX or CycloneDX SBOM whose component digest matches the shipped artifact.
- **applies_if**: all
- **severity**: critical
- **sources**: https://spdx.dev/specifications/ ; https://cyclonedx.org/specification/

### shipped-license-policy
- **what**: Enforce an approved-license policy and ship required notices, attribution, and source offers alongside every distributable artifact.
- **why**: An otherwise working release can violate copyleft or notice obligations and be blocked, recalled, or legally exposed.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://spdx.org/licenses/ ; https://spdx.dev/specifications/

### build-provenance-reproducibility
- **what**: Produce verifiable build provenance and retain the exact source, dependency lockfiles, SBOM, and signing evidence for each release.
- **why**: You cannot prove what code shipped or safely distinguish a compromised artifact from a legitimate rebuild.
- **check**: probe
- **probe**: Parse release metadata and verify a sample artifact has a signature or provenance attestation, immutable source revision, lockfile digest, and matching SBOM digest.
- **applies_if**: all
- **severity**: important
- **sources**: https://slsa.dev/spec/v1.0/ ; https://spdx.dev/specifications/

### dependency-eol-register
- **what**: Track end-of-life dates and support owners for every runtime, OS or base image, framework, and direct or transitive dependency.
- **why**: Unsupported components stop receiving fixes and create emergency migrations or unpatchable vulnerabilities.
- **check**: probe
- **probe**: Parse lockfiles, container manifests, runtime version files, and an EOL catalog; fail entries without a support end date, owner, or upgrade issue.
- **applies_if**: all
- **severity**: critical
- **sources**: https://endoflife.date/ ; https://google.github.io/osv-scanner/

### dependency-update-deadlines
- **what**: Set a maximum age for security and maintenance updates and an escalation path for dependencies that cannot be upgraded.
- **why**: A tracking spreadsheet without deadlines allows known-risk versions to persist until a forced outage.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://google.github.io/osv-scanner/ ; https://scorecard.dev/

### service-decommission-runbook
- **what**: Maintain a versioned service-decommission runbook covering owner, dependency graph, traffic drain, queues, data retention and export, backups, DNS, secrets, IAM, vendors, and billing.
- **why**: Deleting compute alone leaves live credentials, dangling endpoints, retained data, or recurring charges.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/decommissioning/ ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### decommission-exit-evidence
- **what**: Require evidence of zero traffic and jobs, drained queues, disabled schedules, deleted access, final backup and retention decisions, and billing stop before closure.
- **why**: A service that appears idle may still receive retries, scheduled invocations, or incur storage and license fees.
- **check**: probe
- **probe**: Parse the runbook checklist and inventory queries; require timestamped checks for request volume, queue depth, schedulers, DNS, IAM, secrets, storage, and billing.
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/decommissioning/ ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### tenant-retention-erasure
- **what**: Define tenant offboarding retention, export, deletion, backup expiry, and legal-hold behavior with an auditable completion record.
- **why**: Keeping abandoned tenant data increases cost and breach impact, while premature deletion violates contractual or legal obligations.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj ; https://sre.google/sre-book/decommissioning/
