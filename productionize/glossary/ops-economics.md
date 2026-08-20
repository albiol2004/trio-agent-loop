### billable-unit-definition
- **definition**: A billable-unit model is a versioned contract that names what customers pay for and how requests, users, tenants, jobs, retries, storage, egress, and third-party calls are metered. It must state aggregation, rounding, retry treatment, free allowances, and effective dates so invoices are reproducible.
- **implementation**:
  - Define a versioned meter catalog with unit name, source event, aggregation window, rounding rule, price, and plan allowance.
  - Emit immutable usage events with request/job IDs and a meter-version field; do not infer usage from mutable invoices.
  - Specify whether failed work, retries, fan-out, cache hits, and vendor calls count, and document exclusions.
  - Reconcile usage events to provider invoices and expose an internal per-tenant usage ledger.
- **probe**: An assessor must inspect the meter catalog, pricing/version history, usage-event schema, invoice reconciliation, and examples showing retries and fan-out counted consistently. Evidence must identify an owner and a customer-visible explanation of each unit.
- **failure_modes**: Prevents a retry storm from silently multiplying invoices, a fan-out endpoint from hiding its true cost, and billing disputes caused by changing meter semantics.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.finops.org/framework/ ; https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html

### unit-cost-measurement
- **definition**: Unit-cost measurement allocates fixed and marginal provider, compute, storage, network, and vendor spend to successful requests, active users, tenants, or other defined units. It should expose distributions by endpoint and cohort rather than relying on an aggregate average.
- **implementation**:
  - Export provider billing, infrastructure usage, storage, egress, and vendor charges into a common cost ledger.
  - Join cost records to bounded service, operation, plan, tenant, and success dimensions using stable IDs.
  - Report p50/p95 and tail cost per successful request and monthly active user, with fixed-cost allocation stated.
  - Reconcile sampled allocation totals to the monthly invoice and alert on unexplained residuals.
- **probe**: An assessor must inspect a recent cost dashboard or query, its allocation formula, endpoint/cohort breakdowns, fixed-versus-marginal assumptions, and reconciliation against provider invoices. They should verify that failed requests and retries are treated according to the billable-unit contract.
- **failure_modes**: Prevents a profitable aggregate from masking one loss-making endpoint, a high-cost customer cohort from eroding margin, and autoscaling from appearing cheap because shared fixed costs were omitted.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.finops.org/framework/ ; https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html

### cost-attribution-tags
- **definition**: Cost attribution tags are stable dimensions attached to billing events and traces so spend can be assigned to a service, environment, operation, release, and approved tenant or plan dimension. Dimensions must remain bounded and avoid putting unbounded tenant IDs into high-cardinality metrics.
- **implementation**:
  - Require `service`, `environment`, `operation`, `version`, and approved `tenant_id` or `plan` fields on cost-bearing events.
  - Use cloud cost-allocation tags and resource labels with a documented naming and ownership convention.
  - Keep high-cardinality tenant attribution in logs or billing records, while aggregating metrics by bounded plan/cohort dimensions.
  - Validate tags at event-ingestion time and retain an `unknown` bucket with an owner for remediation.
- **probe**: Parse metric schemas, trace resource attributes, cloud cost tags, and billing-event producers; fail if cost-bearing records lack `service`, `environment`, `operation`, and an approved tenant or plan dimension. Check that metric dimensions have an explicit cardinality policy.
- **failure_modes**: Prevents an unowned shared cloud bill, inability to isolate an abusive tenant's spend, and dashboards that become unusable from unbounded labels.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/cost-alloc-tags.html ; https://opentelemetry.io/docs/specs/semconv/general/

### cost-alert-policy
- **definition**: A cost-alert policy defines owner-backed thresholds for absolute spend, spend rate, forecasted spend, and cost per request or user. It specifies warning, paging, escalation, suppression, and response actions for each service and environment.
- **implementation**:
  - Choose monthly budgets plus short-window rate and forecast thresholds for each cost center.
  - Set separate warning and paging levels, with owner, backup owner, escalation timer, and runbook URL.
  - Include unit-cost and anomaly alerts alongside absolute spend so traffic growth cannot hide margin loss.
  - Document exemptions, maintenance windows, and the action permitted at each threshold.
- **probe**: Present the exact decision: “Which thresholds and actions should govern spend alerts?” Options: (A) absolute budget only, (B) budget plus rate/forecast, (C) budget plus rate/forecast and unit-cost paging, or (D) custom values with named owners. Record thresholds, escalation targets, and whether an alert can pause expensive work.
- **failure_modes**: Prevents runaway background jobs continuing until month end, abuse being missed because total spend is still small, and alerts paging without an actionable owner.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html ; https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html

### cost-alert-delivery
- **definition**: Cost-alert delivery verifies that spend and unit-cost alerts reach an actively monitored on-call destination with enough context to act. A delivered alert identifies the affected service, owner, tenant or plan when applicable, and response runbook.
- **implementation**:
  - Route alerts through the same tested paging or incident system used for reliability incidents.
  - Include `service`, `environment`, `owner`, threshold, observed value, scope, and `runbook_url` in the payload.
  - Add a staging or synthetic alert path that does not affect production budgets.
  - Record delivery, acknowledgement, escalation, and suppression events for audit.
- **probe**: Parse alert rules and notification routes, then fire a staging alert and assert that the received event contains `service`, `owner`, and `runbook_url`; also verify tenant/plan context for scoped alerts. Confirm the destination has a current on-call recipient.
- **failure_modes**: Prevents a budget alarm going to a retired channel, a generic page lacking enough context to stop abuse, and silent notification failures during a spend incident.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### cost-to-serve-benchmark
- **definition**: A cost-to-serve benchmark measures latency, throughput, capacity, and marginal cost for representative workloads at target and surge load. It captures tail behavior and saturation rather than treating average latency or idle cost as the operating point.
- **implementation**:
  - Define representative requests, tenant mixes, payload sizes, cache states, and vendor-call patterns.
  - Run repeatable target and surge load tests with p50/p95 latency, throughput, error rate, queue depth, and marginal cost.
  - Mark the saturation knee and record autoscaling, egress, and third-party cost assumptions.
  - Store benchmark artifacts with build version, region, data shape, and infrastructure configuration.
- **probe**: An assessor must inspect recent benchmark runs and confirm representative workloads, target/surge scenarios, p50/p95 metrics, marginal-cost calculation, saturation point, and comparison to capacity/SLO limits. They should verify the test build and infrastructure are identified.
- **failure_modes**: Prevents optimizing mean latency while p95 cost explodes at saturation, underprovisioning for a surge, and choosing a fast but uneconomical implementation.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html ; https://cloud.google.com/architecture/framework/cost-optimization

### ephemeral-environment-ttl
- **definition**: An ephemeral environment has an owner, creation timestamp, explicit expiry, and an automated teardown path. The lifecycle applies to preview, test, and temporary infrastructure and includes attached databases, IPs, secrets, and other resources.
- **implementation**:
  - Stamp `owner`, `created_at`, `expires_at`, environment type, and source change ID on every ephemeral resource.
  - Enforce a maximum TTL server-side and permit only controlled extensions with an audit record.
  - Have CI or an environment controller destroy the complete resource graph at expiry.
  - Ensure teardown revokes credentials and deletes or expires dependent data according to retention policy.
- **probe**: Parse IaC and CI configuration; fail when an ephemeral environment lacks `owner`, `created_at`, or `expires_at`, or lacks an automatic destroy path. Create a fixture environment, advance or simulate expiry, and assert that dependent resources are selected for teardown.
- **failure_modes**: Prevents orphaned preview databases accumulating recurring charges, expired environments retaining secrets, and temporary public endpoints remaining reachable.
- **severity**: critical
- **applies_if**: all
- **merges_into**: preview-environments
- **sources**: https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/ ; https://developer.hashicorp.com/terraform/cli/commands/destroy

### scheduled-resource-cleanup
- **definition**: Scheduled resource cleanup is a recurring inventory and deletion process for expired or unowned resources outside the normal environment workflow. It reports protected or undeletable dependencies with an owner instead of silently skipping them.
- **implementation**:
  - Run a least-privilege scheduled job against cloud, cluster, database, DNS, and secret inventories.
  - Select only resources with explicit ephemeral markers and expired `expires_at` values; never use broad name matching alone.
  - Execute dry-run and dependency-aware deletion, with retries and a quarantine path for failures.
  - Emit per-resource outcome, owner, reason, and residual-cost data to an operations dashboard.
- **probe**: Parse scheduler configuration for a cleanup job and run its dry-run against fixtures containing an expired environment and a protected dependency. Assert the expired resource is selected, the protected dependency is retained, and an owner/actionable error is emitted.
- **failure_modes**: Prevents resources created outside CI from becoming permanent, cleanup failures being invisible, and dependency deletion errors causing unsafe partial teardown.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html ; https://developer.hashicorp.com/terraform/cli/commands/destroy

### teardown-guardrails
- **definition**: Teardown guardrails make destructive cleanup deny-by-default for production and shared infrastructure. A teardown must pass environment classification, dependency, backup, retention, and approval checks before deletion.
- **implementation**:
  - Require an immutable environment classification and reject production targets in ephemeral cleanup jobs.
  - Resolve dependency graphs and verify backups, retention/legal holds, and export requirements before deletion.
  - Require a human approval or two-person review for shared or high-impact resources.
  - Use dry-run plans, explicit resource IDs, and append-only audit logs for every destructive action.
- **probe**: An assessor must inspect deletion IAM policies, environment classification, Terraform or controller plans, backup/retention checks, approval rules, and audit records. They should confirm a production-target fixture is rejected before any delete call.
- **failure_modes**: Prevents a broad selector deleting production data, cleanup bypassing a legal hold, and a shared database being removed because it looked like a preview dependency.
- **severity**: critical
- **applies_if**: all
- **sources**: https://developer.hashicorp.com/terraform/cli/commands/destroy ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### authenticated-tenant-context
- **definition**: Authenticated tenant context derives tenant identity from server-validated credentials and propagates it through authorization, storage, jobs, logs, and caches. Client-provided tenant identifiers are treated as untrusted selectors and never as the authority for access.
- **implementation**:
  - Validate token/session membership and construct an immutable request context containing authenticated `tenant_id` and actor.
  - Pass that context explicitly to repositories, cache namespaces, queue payloads, and background-job authorization.
  - Reject or ignore body, query, and header tenant IDs that disagree with authenticated context.
  - Test admin, asynchronous, retry, and cache paths with cross-tenant identifiers.
- **probe**: Trace request and job context code and fail paths where a repository, cache, or queue call accepts a tenant identifier not sourced from authenticated context. Inspect tests or runtime traces showing forged client IDs are rejected.
- **failure_modes**: Prevents cross-tenant reads, writes performed under a forged tenant ID, and cache poisoning that serves one tenant's data to another.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html ; https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations

### tenant-keyed-schema
- **definition**: A tenant-keyed schema makes tenant identity part of partitioning, foreign-key relationships, uniqueness constraints, and every tenant-owned query boundary. The database model must make accidental cross-tenant joins and mutations structurally difficult.
- **implementation**:
  - Add non-null `tenant_id` to tenant-owned tables and composite indexes for common access paths.
  - Include tenant identity in unique constraints and foreign keys where records must not cross tenants.
  - Require query builders and update/delete operations to include tenant predicates or derive them from session context.
  - Review migrations for backfill, null handling, and prevention of new unscoped tables.
- **probe**: Parse migrations, schema metadata, and query builders; fail tenant-owned tables or update/delete statements that lack a tenant key or composite constraint. Inspect representative joins and uniqueness constraints for cross-tenant safety.
- **failure_modes**: Prevents a missing predicate leaking records, a globally unique slug overwriting another tenant's value, and a background migration joining unrelated tenant rows.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations ; https://www.postgresql.org/docs/current/ddl-rowsecurity.html

### database-row-isolation
- **definition**: Database row isolation enforces deny-by-default tenant policies at the database layer, independently of ORM filters. It covers application roles, workers, reporting jobs, and administrative paths, with narrowly audited break-glass access.
- **implementation**:
  - Enable row-level security on tenant tables and define policies from a trusted session tenant setting.
  - Separate application, worker, reporting, and break-glass roles; revoke bypass privileges by default.
  - Set tenant context on every connection and clear it when connections return to a pool.
  - Add negative tests for missing, forged, and stale tenant context, including background jobs.
- **probe**: Parse database migrations and role grants; require RLS and policies or a documented equivalent on tenant tables and deny table access to roles that bypass them. Execute representative queries with absent and alternate tenant contexts and assert no rows are returned.
- **failure_modes**: Prevents new ORM code bypassing filters, pooled connections retaining the previous tenant, and workers exposing all tenant rows through a direct SQL path.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.postgresql.org/docs/current/ddl-rowsecurity.html ; https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

### tenant-scoped-secrets-and-logs
- **definition**: Tenant-scoped secrets and logs isolate keys, object prefixes, caches, backups, and operational records by tenant while minimizing sensitive tenant data in shared channels. Access and retention must be auditable across both live and archived copies.
- **implementation**:
  - Use tenant-scoped encryption-key policy or envelope-key metadata and tenant-specific object-store prefixes.
  - Namespace caches and backups with authenticated tenant identity; prohibit client-controlled namespace components.
  - Redact payloads, tokens, and personal data from logs and traces, retaining stable tenant references where needed.
  - Restrict support and analytics access and apply retention/deletion rules to logs and backups.
- **probe**: An assessor must inspect key policies, object-store paths, cache naming, backup access, log redaction rules, and support-role permissions. Evidence should include a cross-tenant access-denial test and confirmation that archived copies follow retention policy.
- **failure_modes**: Prevents shared backup access exposing a tenant, logs becoming a cross-tenant data exfiltration channel, and cache keys returning another tenant's secret or response.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://csrc.nist.gov/publications/detail/sp/800-57-part-1/rev-5/final ; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### noisy-neighbor-controls
- **definition**: Noisy-neighbor controls cap each tenant's resource consumption and schedule work fairly across shared pools. They combine request rate, concurrency, CPU/memory, queue, storage, and backpressure controls so one tenant cannot exhaust capacity.
- **implementation**:
  - Key rate and concurrency limiters by authenticated tenant and operation, with plan-specific budgets.
  - Apply queue partitions, weighted fair scheduling, bounded queue depth, and admission backpressure.
  - Set per-tenant CPU/memory and storage quotas at the runtime or cluster layer.
  - Expose usage, throttling, queue age, and rejected-work metrics by bounded tenant cohort and plan.
- **probe**: Parse gateway, queue, and runtime policy; require tenant-keyed rate and concurrency limits plus a bounded queue or fair-share scheduler. Run a load fixture for one tenant and assert other tenants retain admission and SLO capacity.
- **failure_modes**: Prevents one customer exhausting worker pools, a large export starving interactive requests, and queue growth turning a tenant spike into a platform-wide outage.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: quota-policy
- **sources**: https://kubernetes.io/docs/concepts/policy/resource-quotas/ ; https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations

### tenant-quota-policy
- **definition**: A tenant quota policy publishes limits by tenant, plan, operation, and time window together with overage, throttling, and rejection semantics. It makes `429` responses, `Retry-After`, reset timing, and upgrade or appeal paths predictable.
- **implementation**:
  - Store versioned quota rules with dimensions, window algorithm, burst allowance, and plan override.
  - Enforce quotas at admission and return structured error fields plus `Retry-After` and reset metadata.
  - Define whether overage is billed, queued, degraded, or rejected, and publish that choice to customers.
  - Provide dashboards and audit records for consumption, reservations, overrides, and quota changes.
- **probe**: Present the exact decision: “For each tenant and operation, what should happen at quota exhaustion?” Options: (A) reject with `429` and `Retry-After`, (B) queue until reset, (C) allow metered overage, (D) degrade to a cheaper mode, or (E) custom hybrid. Record limits, burst, reset window, customer notice, and escalation owner.
- **failure_modes**: Prevents uncontrolled spend from unbounded tenants, clients retrying indefinitely because reset behavior is undocumented, and a quota change breaking customers without a remedy.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: quota-policy
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://kubernetes.io/docs/concepts/policy/resource-quotas/

### authenticated-user-quota
- **definition**: An authenticated-user quota imposes a second allowance within a tenant so one credential cannot consume all shared capacity. The enforcement key includes both trusted `tenant_id` and authenticated `user_id`, with operation-specific limits for expensive work.
- **implementation**:
  - Define user, tenant, plan, operation, and time-window dimensions in the quota key.
  - Apply user limits before tenant-wide admission and return consistent reset and retry metadata.
  - Treat service accounts and delegated actors as explicit principals with separate policy.
  - Emit quota decisions and consumption without logging credentials or sensitive payloads.
- **probe**: Parse quota keys and enforcement middleware; require the key tuple to include authenticated `user_id` and `tenant_id` for expensive operations. Exercise two users in one tenant and assert one user's burst cannot consume the other's reserved allowance.
- **failure_modes**: Prevents a compromised user starving colleagues, a runaway automation token consuming the tenant budget, and a client bypassing limits by rotating spoofed user IDs.
- **severity**: important
- **applies_if**: web-api
- **merges_into**: quota-policy
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://kubernetes.io/docs/concepts/policy/resource-quotas/

### quota-reservation-atomicity
- **definition**: Quota reservation atomically admits capacity before expensive work and releases or settles it when work succeeds or fails. Retries and redeliveries use an idempotent reservation key so concurrent requests cannot oversubscribe or double-charge.
- **implementation**:
  - Use an atomic conditional increment, transactional row lock, or compare-and-swap in the quota store.
  - Persist reservation ID, tenant/user, operation, amount, expiry, and idempotency key before enqueueing or calling an external provider.
  - Settle successful reservations and compensate failed or cancelled work with bounded reconciliation.
  - Reject duplicate reservation keys or return the original decision and make reservation expiry recoverable.
- **probe**: Inspect quota-store operations and job admission; require an atomic conditional increment or reservation before enqueue or external spend and a compensating release on failure. Run concurrent admission and retry fixtures and assert total reserved capacity never exceeds the limit and duplicate keys create one reservation.
- **failure_modes**: Prevents concurrent requests oversubscribing a quota, queue redelivery paying for work twice, and failed external calls permanently consuming customer allowance.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: quota-policy
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://docs.stripe.com/api/idempotent_requests

### wcag-22-aa-target
- **definition**: The WCAG 2.2 Level AA target is an explicit product accessibility baseline for keyboard, screen-reader, visual, cognitive, and input interactions. Exceptions must be documented with affected criteria, risk, owner, and remediation date.
- **implementation**:
  - Record WCAG 2.2 AA as a release requirement in product and engineering policy.
  - Map critical journeys to success criteria and assign an accessibility owner.
  - Maintain an exception register with user impact, compensating support, due date, and approval.
  - Include accessibility acceptance criteria in design reviews and release gates.
- **probe**: An assessor must inspect the accessibility policy, journey-to-criterion mapping, exception register, owners, and recent release evidence. They should verify that exceptions have bounded remediation dates rather than blanket waivers.
- **failure_modes**: Prevents keyboard-only users being unable to complete purchases, screen-reader users missing errors, and known barriers becoming permanent because no target exists.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://www.w3.org/TR/WCAG22/ ; https://www.w3.org/WAI/test-evaluate/

### automated-a11y-gates
- **definition**: Automated accessibility gates scan representative routes and critical states during CI using axe-core or an equivalent ruleset. They provide fast regression detection but do not replace manual journey validation.
- **implementation**:
  - Add an accessibility CI job covering authenticated and unauthenticated critical routes and states.
  - Run axe-core or equivalent against built UI with deterministic fixtures and fail on agreed severity thresholds.
  - Upload machine-readable reports tied to commit and route, with reviewed suppressions for known exceptions.
  - Keep scanner rules and browser versions pinned enough for reproducible results.
- **probe**: Parse CI workflows and test dependencies; require an accessibility job invoking `axe-core` or an equivalent scanner against authenticated and unauthenticated critical routes. Verify it fails on a seeded missing-label fixture and that suppressions reference the exception register.
- **failure_modes**: Prevents missing form names, invalid roles, inadequate contrast, and absent landmarks reaching production unnoticed.
- **severity**: important
- **applies_if**: spa
- **sources**: https://github.com/dequelabs/axe-core ; https://www.w3.org/WAI/test-evaluate/

### manual-a11y-journeys
- **definition**: Manual accessibility journeys validate that people can complete high-value and destructive flows using keyboard, screen reader, zoom, reflow, and visible focus. They inspect focus order, announcements, error recovery, and task meaning that automated rules cannot establish.
- **implementation**:
  - Maintain scripted sign-in, purchase, settings, and destructive-flow journeys with expected focus and announcement checkpoints.
  - Test keyboard-only operation, a representative screen reader/browser pair, zoom and reflow, and reduced-motion settings.
  - Record defects with WCAG criterion, reproduction steps, user impact, owner, and retest evidence.
  - Run before major releases and after changes to navigation, dialogs, forms, or error handling.
- **probe**: An assessor must inspect dated manual test recordings or reports for sign-in, purchase, and destructive flows, including keyboard path, visible focus, screen-reader output, zoom/reflow, and error recovery. They should verify unresolved failures are tracked to the accessibility target.
- **failure_modes**: Prevents focus being trapped in a modal, a screen reader missing a dynamic error, and zoomed layouts hiding the submit or recovery control.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://www.w3.org/TR/WCAG22/ ; https://www.w3.org/WAI/ARIA/apg/

### i18n-string-catalogs
- **definition**: Versioned locale catalogs externalize every user-visible string, plural/select rule, validation message, and accessibility label. Translation lookup keys and fallback behavior are explicit so UI text is not assembled from locale-unsafe literals.
- **implementation**:
  - Store default and translated messages in versioned catalogs with stable keys and metadata.
  - Use ICU MessageFormat or an equivalent plural/select mechanism rather than concatenation.
  - Make missing-key behavior observable and fail builds or review for missing default-locale entries.
  - Include accessible names, validation errors, emails, notifications, and server-generated user text in the catalog workflow.
- **probe**: Parse UI source and fail user-facing literal assignments outside approved locale catalogs or translation APIs, then verify every default-locale key has a catalog entry. Include plural and accessibility-label fixtures in the scan.
- **failure_modes**: Prevents concatenated strings with broken grammar, untranslated validation errors, and controls that are inaccessible because their labels bypass translation.
- **severity**: important
- **applies_if**: spa
- **sources**: https://cldr.unicode.org/ ; https://www.w3.org/International/

### locale-aware-formatting
- **definition**: Locale-aware formatting uses ICU or `Intl` with explicit locale, time zone, currency, and unit rules for displayed dates, numbers, names, and prices. It avoids hand-built separators and implicit server-local time assumptions.
- **implementation**:
  - Centralize date, number, currency, unit, list, and relative-time formatting in approved wrappers.
  - Pass the user/request locale and intended time zone explicitly; store instants in an unambiguous format.
  - Use currency metadata and minor-unit rules rather than concatenating symbols.
  - Add locale fixtures for decimal separators, calendars, time zones, negative values, and long numbers.
- **probe**: Static-scan UI and presentation code for locale-unsafe date and number formatting and require locale and zone arguments or approved ICU wrappers for displayed values. Render representative values in at least two locales and time zones and compare expected output.
- **failure_modes**: Prevents a US-formatted date being interpreted as another day, a currency symbol hiding the wrong charge, and server time zones showing an incorrect deadline.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://cldr.unicode.org/ ; https://www.w3.org/International/

### rtl-and-expansion-locales
- **definition**: RTL and expansion testing verifies long translations, plural variants, bidirectional scripts, RTL layout, Unicode normalization, and locale fallback. It ensures localized content remains legible and semantically ordered under real text expansion.
- **implementation**:
  - Include at least one RTL locale and one known expansion-heavy locale in release fixtures.
  - Set document direction from locale and use logical CSS properties instead of left/right assumptions.
  - Test mixed-direction user content with bidi isolation and normalize catalog keys/content at defined boundaries.
  - Capture screenshots or DOM assertions for clipping, overlap, overflow, and fallback behavior.
- **probe**: Parse supported-locale configuration and DOM or screenshot fixtures; require at least one RTL and one expansion locale plus fallback and normalization cases. Assert critical controls remain reachable and text is not clipped or reordered incorrectly.
- **failure_modes**: Prevents Arabic or Hebrew layouts reversing action meaning, long German strings clipping buttons, and mixed-script content spoofing visual order.
- **severity**: important
- **applies_if**: spa
- **sources**: https://www.unicode.org/reports/tr9/ ; https://www.unicode.org/reports/tr15/

### deterministic-locale-negotiation
- **definition**: Deterministic locale negotiation selects an allowlisted locale from an explicit user or request setting and applies a stable fallback order. Localized response caches include the selected locale in their variant key to prevent cross-locale reuse.
- **implementation**:
  - Parse and canonicalize BCP 47 tags, then intersect them with an allowlist.
  - Define precedence among user profile, explicit request, cookie, `Accept-Language`, and default locale.
  - Include locale and relevant time-zone/format variants in response-cache keys and `Vary` behavior.
  - Log selected locale and fallback reason without recording unnecessary personal data.
- **probe**: Parse locale middleware and cache-key construction; require allowlisted BCP 47 locales, deterministic fallback, and locale inclusion in every localized response cache key. Send conflicting locale requests through a shared-cache fixture and assert each receives the correct variant.
- **failure_modes**: Prevents one user's translation being cached for another, unsupported tags producing inconsistent language, and locale changes appearing nondeterministically across requests.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.w3.org/International/questions/qa-html-language-declarations ; https://cldr.unicode.org/

### api-deprecation-policy
- **definition**: An API deprecation policy is a versioned contract defining notice period, support window, owner, migration guidance, and exceptions for obsolete endpoints or fields. It gives consumers a predictable sunset process rather than treating breaking changes as ad hoc releases.
- **implementation**:
  - Publish supported versions, minimum notice and support windows, and a deprecation owner.
  - Require migration guides, replacement API details, changelog entry, and communication channels for each deprecation.
  - Define exception approval, extension criteria, and final removal decision evidence.
  - Track deprecated consumers and milestones in a versioned register.
- **probe**: Present the exact decision: “What deprecation contract should customers receive?” Options: (A) documented notice plus fixed support window, (B) notice plus support window and migration assistance, (C) indefinite compatibility for selected clients, or (D) custom policy. Record notice length, owner, exception process, and removal evidence required.
- **failure_modes**: Prevents clients discovering breaking removal only after deployment, unsupported versions lingering indefinitely, and emergency extensions with no accountable owner.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9745 ; https://www.rfc-editor.org/rfc/rfc8594

### deprecation-sunset-signals
- **definition**: Deprecation and sunset signals expose machine-readable removal intent through standards-based `Deprecation` and `Sunset` headers plus documentation metadata. Responses link consumers to migration guidance and state the planned removal date.
- **implementation**:
  - Add valid `Deprecation` and `Sunset` headers to every deprecated endpoint/version response.
  - Include a migration URL and matching changelog/API documentation entry.
  - Generate headers from the versioned deprecation registry to prevent date drift.
  - Monitor responses and client telemetry for missing signals before a sunset.
- **probe**: Curl each deprecated endpoint or version and assert valid `Deprecation` and `Sunset` headers, a migration link, and a documented removal date. Compare header dates and links to the deprecation registry and docs.
- **failure_modes**: Prevents prose-only warnings being missed by client operators, inconsistent sunset dates across endpoints, and automated tooling learning about removal only after failures.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9745 ; https://www.rfc-editor.org/rfc/rfc8594

### sunset-consumer-inventory
- **definition**: A sunset consumer inventory identifies active clients, versions, tenants, and request volume before removal and records how their owners were notified. It turns unknown API dependence into auditable migration evidence.
- **implementation**:
  - Capture client/application identity, API version, tenant, endpoint, last-seen time, and request volume in telemetry.
  - Query a defined lookback window and distinguish automated probes from real consumers.
  - Map consumers to accountable owners and record notification, migration, and exception status.
  - Re-run the inventory at milestones and retain snapshots with the sunset decision.
- **probe**: Parse access-log and telemetry schemas and the deprecation runbook; require client, version, and tenant fields plus a report query covering the proposed sunset window. Verify the report has owner mapping and notification evidence for every active consumer.
- **failure_modes**: Prevents removing a version used by an untracked enterprise integration, missing a low-volume but critical scheduled client, and rolling back because consumer discovery happened after shutdown.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc8594 ; https://spec.openapis.org/oas/latest.html

### sunset-removal-response
- **definition**: Sunset removal occurs only after the announced date, migration evidence, and exception decisions are recorded. The removed endpoint returns a documented `410 Gone` or equivalent final status that points remaining clients to the supported replacement.
- **implementation**:
  - Gate removal on registry date, consumer inventory, owner sign-off, and migration/exception evidence.
  - Deploy a final response handler with `410 Gone`, replacement link, and support information.
  - Preserve request metrics and rollback controls during the initial removal window.
  - Remove obsolete credentials, routes, tests, and operational dependencies after the final retention period.
- **probe**: An assessor must inspect the announced sunset date, migration evidence, exception approvals, deployment gate, and production response for a removed endpoint. They should verify the response is `410 Gone` (or documented equivalent) and directs consumers to the replacement.
- **failure_modes**: Prevents premature removal breaking active clients, indefinite compatibility retaining vulnerable code, and a removed route returning an ambiguous 404 that obscures migration action.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9110#name-410-gone ; https://www.rfc-editor.org/rfc/rfc8594

### expensive-operation-friction
- **definition**: Expensive-operation friction adds progressive trust checks before anonymous or low-trust actions that can incur substantial compute, messaging, inference, scraping, or vendor cost. The challenge escalates based on risk and preserves an accessible, explainable path for legitimate users.
- **implementation**:
  - Classify operations by expected cost and abuse risk, and require stronger identity or step-up checks as risk rises.
  - Use CAPTCHA, proof-of-work, verified email/phone, step-up authentication, quotas, or manual review as appropriate.
  - Bind successful friction to a short-lived operation token and prevent replay or token sharing.
  - Instrument challenge rate, pass/fail, abandonment, false positives, and attributed cost by operation.
- **probe**: Present the exact decision: “What friction should precede each expensive low-trust operation?” Options: (A) CAPTCHA, (B) proof-of-work, (C) verified identity/step-up authentication, (D) manual review, (E) layered escalation, or (F) no friction with an explicitly accepted cost limit. Record accessibility fallback, trigger threshold, and owner.
- **failure_modes**: Prevents bots converting free calls into paid inference, anonymous scraping generating vendor charges, and rate limits reacting only after an attack has consumed expensive work.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: abuse-friction
- **sources**: https://owasp.org/www-project-automated-threats-to-web-applications/ ; https://cloud.google.com/recaptcha/docs

### expensive-operation-caps
- **definition**: Expensive-operation caps place finite limits on payload bytes, fan-out, recursion depth, execution time, concurrency, retries, and third-party spend. Caps apply per operation and tenant before work can create unbounded resource or vendor cost.
- **implementation**:
  - Declare maximum bytes/items/depth/duration/retries/concurrency and external-call budget in route or job schemas.
  - Enforce limits at request validation, queue admission, worker runtime, and vendor-client layers.
  - Propagate remaining budget through nested calls and stop work when it is exhausted.
  - Return structured limit errors and measure rejected, truncated, and budget-exhausted work.
- **probe**: Parse route schemas and job definitions; require finite maximums for bytes, items, depth, duration, retries, concurrency, and external-call budget on expensive operations. Run boundary and over-limit fixtures to assert rejection occurs before external spend.
- **failure_modes**: Prevents a crafted recursive request exhausting workers, a large fan-out creating a vendor bill, and retries multiplying queue work after a partial timeout.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html

### idempotent-chargeable-operations
- **definition**: Chargeable mutations and side effects use a client or operation idempotency key whose result is durably recorded before external execution. Retries and queue redelivery return or reuse the original outcome instead of creating duplicate payments, jobs, messages, or vendor calls.
- **implementation**:
  - Require `Idempotency-Key` or an equivalent stable deduplication key for each chargeable mutation and worker admission.
  - Persist key, tenant/actor, request fingerprint, status, response, and provider reference with a uniqueness constraint.
  - Claim the key atomically before side effects; return the stored result for identical retries and reject conflicting payloads.
  - Retain records for the provider's retry window and reconcile uncertain outcomes with provider lookup APIs.
- **probe**: Parse API schemas, routes, and worker admission code; require `Idempotency-Key` or equivalent persisted deduplication before executing each chargeable operation. Replay identical requests and queue deliveries, then assert one side effect and a stable response; submit a conflicting payload and assert rejection.
- **failure_modes**: Prevents duplicate card charges after client timeout, duplicate emails or jobs after queue redelivery, and double vendor spend when a worker retries an uncertain request.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: idempotency-keys
- **sources**: https://docs.stripe.com/api/idempotent_requests ; https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/

### abuse-cost-kill-switch
- **definition**: An abuse-cost kill switch stops new expensive work for a precisely attributed tenant, operation, or actor while allowing safe completion or cancellation of in-flight state. Every activation and release is auditable and reversible.
- **implementation**:
  - Attribute spend and admission decisions to tenant, operation, actor, request, and provider reference.
  - Implement an operator/API switch with deny-new-work semantics, scoped selectors, expiry, and two-person approval for broad actions.
  - Check the switch at request, queue, retry, and scheduled-job admission points.
  - Emit activation, reason, approver, affected scope, in-flight handling, and restoration events.
- **probe**: Parse incident controls and job admission code; require an operator or API kill switch keyed by tenant and operation plus a traceable audit event. Activate it in staging and assert new work is rejected while an in-flight fixture reaches a defined safe state.
- **failure_modes**: Prevents a cost attack continuing while responders search for the tenant, an emergency global shutdown harming unaffected customers, and disabled work being silently re-enabled without review.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/ ; https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html

### sbom-per-shipped-artifact
- **definition**: Every shipped image, binary, package, and installer has a machine-readable SPDX or CycloneDX SBOM covering direct and transitive components and license identifiers. The SBOM is bound to the exact artifact digest so responders can identify affected releases.
- **implementation**:
  - Generate SBOMs in the release pipeline after dependency resolution and before publication.
  - Include component versions, package URLs or equivalent identifiers, licenses, and artifact/image digest.
  - Store and publish the SBOM beside the release with immutable retention and access controls.
  - Fail release when generation, schema validation, or artifact-digest matching fails.
- **probe**: Parse the release workflow and inspect a sample artifact; require a machine-readable SPDX or CycloneDX SBOM whose component digest matches the shipped artifact. Verify transitive dependencies and license identifiers are present and the SBOM is retrievable by release digest.
- **failure_modes**: Prevents incident response being unable to locate a vulnerable transitive library, license notices being omitted, and an SBOM describing a different binary than the shipped one.
- **severity**: critical
- **applies_if**: all
- **merges_into**: sbom-provenance
- **sources**: https://spdx.dev/specifications/ ; https://cyclonedx.org/specification/

### shipped-license-policy
- **definition**: A shipped-license policy defines which dependency licenses are approved, restricted, or prohibited and what notices, attribution, and source offers each requires. Release artifacts carry the required legal materials in a discoverable, version-matched location.
- **implementation**:
  - Maintain an approved license allowlist/exception register and map detected SPDX IDs to policy outcomes.
  - Scan direct and transitive dependencies before release and fail prohibited or unresolved licenses.
  - Generate `NOTICE`, attribution, and source-offer files from locked dependency metadata.
  - Review copyleft, dual-license, and bundled asset obligations with a named legal owner.
- **probe**: An assessor must inspect the license policy, dependency scan report, exception approvals, generated notices, and a sample distributable artifact. They should verify notice contents correspond to the exact shipped dependency set and source obligations are accessible.
- **failure_modes**: Prevents a release being blocked for missing copyleft notices, a bundled asset violating its license, and legal exposure from undocumented transitive dependencies.
- **severity**: critical
- **applies_if**: all
- **sources**: https://spdx.org/licenses/ ; https://spdx.dev/specifications/

### build-provenance-reproducibility
- **definition**: Build provenance records verifiable claims about source, builder, dependencies, parameters, and artifact digest, while reproducibility preserves the inputs needed to rebuild or explain a release. Signing evidence, lockfiles, and the matching SBOM let responders distinguish an authorized artifact from a compromised or divergent rebuild.
- **implementation**:
  - Emit SLSA-compatible provenance or an equivalent signed attestation for every release artifact.
  - Pin source revision, dependency lockfiles, builder image/toolchain, build parameters, and artifact digest.
  - Retain provenance, SBOM, signatures, build logs, and source references in immutable release storage.
  - Verify signatures and attestations before promotion and compare a rebuild's digest or documented reproducibility differences.
- **probe**: Parse release metadata and verify a sample artifact has a signature or provenance attestation, immutable source revision, lockfile digest, and matching SBOM digest. Attempt verification with the published identity and assert tampered metadata or artifact bytes fail.
- **failure_modes**: Prevents inability to prove which source shipped, accepting a malicious artifact from an untrusted builder, and rebuilding a release with silently changed dependencies.
- **severity**: important
- **applies_if**: all
- **merges_into**: sbom-provenance
- **sources**: https://slsa.dev/spec/v1.0/ ; https://spdx.dev/specifications/

### dependency-eol-register
- **definition**: A dependency EOL register tracks support end dates and owners for runtimes, operating systems, base images, frameworks, and direct or transitive dependencies. It connects each component to a remediation issue and records exceptions for components that cannot yet move.
- **implementation**:
  - Generate inventory from lockfiles, container manifests, runtime files, and deployment images.
  - Record component version, EOL date, support source, owner, risk, upgrade target, and issue URL.
  - Refresh EOL data on a schedule and alert before support ends.
  - Require risk acceptance, compensating controls, and expiry for exceptions.
- **probe**: Parse lockfiles, container manifests, runtime version files, and an EOL catalog; fail entries without a support end date, owner, or upgrade issue. Verify a transitive dependency and base image are both represented and stale entries alert before EOL.
- **failure_modes**: Prevents deploying an unsupported runtime, discovering a base-image EOL during an emergency patch, and leaving transitive components without an accountable upgrade path.
- **severity**: critical
- **applies_if**: all
- **sources**: https://endoflife.date/ ; https://google.github.io/osv-scanner/

### dependency-update-deadlines
- **definition**: Dependency update deadlines set maximum age or severity-based remediation windows for security and maintenance updates. They include an escalation and exception path so known-risk versions cannot remain indefinitely in a tracking system.
- **implementation**:
  - Define deadlines by vulnerability severity, dependency class, and whether the component is internet-facing.
  - Open automated update issues with due dates, owners, test evidence, and rollback plans.
  - Escalate overdue issues to service and security owners and block releases when policy requires.
  - Record exceptions with reason, compensating controls, approval, and expiration.
- **probe**: Present the exact decision: “What maximum age and remediation deadline should apply to dependencies?” Options: (A) fixed age for all updates, (B) severity-based windows, (C) severity-based windows plus release blocking, or (D) custom deadlines with named exception owner. Record values, escalation, and expiry behavior.
- **failure_modes**: Prevents a vulnerable package persisting until an exploit, routine maintenance accumulating into an emergency migration, and an overdue spreadsheet item losing ownership.
- **severity**: important
- **applies_if**: all
- **sources**: https://google.github.io/osv-scanner/ ; https://scorecard.dev/

### service-decommission-runbook
- **definition**: A service-decommission runbook is a versioned, owner-backed procedure for retiring compute and every connected dependency. It covers traffic, queues, schedules, data retention/export, backups, DNS, secrets, IAM, vendors, and billing through final verification.
- **implementation**:
  - Maintain a dependency graph with owners, resource IDs, data stores, queues, schedules, domains, credentials, and vendor contracts.
  - Define staged read-only, drain, disable, revoke, delete, and retention actions with rollback points.
  - Require data export/retention and backup decisions before deleting storage.
  - Capture approvals, timestamps, command outputs, and billing confirmation in a closure record.
- **probe**: An assessor must inspect a versioned runbook and a completed decommission record covering owner, dependency graph, traffic drain, queues, data, backups, DNS, secrets, IAM, vendors, and billing. They should verify rollback and retention decisions are explicit.
- **failure_modes**: Prevents dangling credentials after compute deletion, recurring vendor charges from an “idle” service, and data loss caused by deleting storage before export or retention review.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/decommissioning/ ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### decommission-exit-evidence
- **definition**: Decommission exit evidence proves that traffic and jobs are gone, queues and schedules are disabled, access is removed, data decisions are complete, and billing has stopped. Evidence is timestamped and scoped to the service and its dependency graph rather than relying on an operator assertion.
- **implementation**:
  - Query request volume, retries, queue depth, scheduled invocations, DNS, IAM, secrets, storage, licenses, and billing at closure.
  - Require a zero-traffic observation window appropriate to retry and schedule intervals.
  - Attach command results, dashboards, deletion IDs, final backup/retention decision, and approver to the closure record.
  - Recheck billing and external vendors after the normal invoice/usage lag.
- **probe**: Parse the runbook checklist and inventory queries; require timestamped checks for request volume, queue depth, schedulers, DNS, IAM, secrets, storage, and billing. Run the checks against a decommission fixture and assert missing or nonzero evidence blocks closure.
- **failure_modes**: Prevents scheduled jobs reviving a supposedly retired service, hidden retries continuing after traffic drain, and storage/license fees persisting after compute deletion.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/decommissioning/ ; https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html

### tenant-retention-erasure
- **definition**: Tenant retention and erasure policy defines offboarding export, deletion, backup expiry, legal-hold, and audit behavior for tenant data. It balances contractual/legal retention with cost and breach minimization and records completion per tenant.
- **implementation**:
  - Define retention classes for live data, object versions, backups, logs, derived data, and vendor copies.
  - Provide authenticated export and deletion workflows with authorization, dependency ordering, and idempotent job IDs.
  - Pause deletion for legal holds and record hold scope, authority, release, and expiry.
  - Emit an auditable completion record listing datasets, timestamps, residual retention, failures, and owner.
- **probe**: Present the exact decision: “What should happen to a tenant's data at offboarding?” Options: (A) export then delete on a fixed schedule, (B) retain for a defined contractual period, (C) preserve only legal holds, or (D) custom per-data-class policy. Record retention durations, backup expiry, export format, legal-hold authority, and completion evidence.
- **failure_modes**: Prevents abandoned tenant data inflating storage and breach impact, premature deletion violating a legal hold, and support being unable to prove an erasure completed.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj ; https://sre.google/sre-book/decommissioning/
