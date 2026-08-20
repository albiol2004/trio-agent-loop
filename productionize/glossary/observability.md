# Observability glossary

### telemetry-ownership
- **definition**: Every production signal, dashboard, alert, and runbook has a named team owner and a documented review cadence. Ownership includes keeping queries, thresholds, dependencies, access, and responder instructions current as the service changes.
- **implementation**:
  - Keep an inventory mapping signal IDs, dashboards, alerts, and runbooks to an owning team, escalation contact, and review date.
  - Put `owner`, `service`, `environment`, and `last_reviewed` metadata in telemetry and dashboard definitions.
  - Require an owner review on service or dependency changes and periodically remove unused signals.
  - Give responders a tested escalation route and permissions to update the owned artifacts.
- **probe**: An assessor must inspect the telemetry inventory and sample each production service for an owner, current review date, escalation route, and evidence that alert, dashboard, and runbook links are maintained.
- **failure_modes**: A renamed metric leaves an alert silently querying an empty series; an outage exposes a stale runbook whose owner has left; duplicate dashboards cause responders to act on conflicting thresholds.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/workbook/monitoring/; https://sre.google/workbook/incident-response/

### structured-log-schema
- **definition**: Log records use a machine-parseable schema with stable field names and typed values rather than relying on human-formatted text. At minimum, each production record carries UTC timestamp, severity, service, version, environment, event name, and message context.
- **implementation**:
  - Emit JSON (or the collector's equivalent structured format) on every production output path, including worker and exception paths.
  - Validate required fields and types at the logging boundary; keep event names and severity values from a controlled vocabulary.
  - Include correlation and trace IDs as separate fields, not only embedded in a message string.
  - Version schema changes and configure collectors to preserve fields without flattening collisions.
- **probe**: Parse representative stdout and collector output as JSON, then assert required fields are present with UTC timestamp, string service/version/environment/event values, an allowed severity, and a nonempty message or event payload.
- **failure_modes**: A multiline parser drops stack traces and incident search misses the crash; one service calls the version field `build` so release correlation fails; malformed exception logs cause the collector to discard the only evidence of an outage.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### log-level-contract
- **definition**: Log levels have explicit operational semantics and production defaults, so verbosity reflects diagnostic value and urgency. Debug and trace output remain disabled by default and can be enabled only through a bounded, auditable override.
- **implementation**:
  - Document which events belong to trace/debug, info, warn, and error, including whether expected client errors are warnings or info.
  - Set production defaults in deploy configuration rather than relying on developer-machine settings.
  - Scope temporary verbosity by service, instance, route, or time limit and record who changed it and why.
  - Add volume alerts and automatic expiry for overrides to prevent accidental log floods.
- **probe**: Read runtime logging configuration and assert production defaults, level semantics, and an auditable temporary override path with scope, expiry, authorization, and rollback.
- **failure_modes**: A debug flag left on multiplies ingestion cost during a traffic spike; everything is logged as error, hiding the true failure signal; a responder enables verbose logs globally and overwhelms a constrained collector.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### request-correlation
- **definition**: A request or correlation identifier is generated or accepted at the edge and propagated through every synchronous and asynchronous hop. The identifier appears in logs, error responses, traces, and support-facing references without exposing sensitive request content.
- **implementation**:
  - Use W3C trace context where available and maintain a separate opaque request ID for support or legacy integrations.
  - Forward identifiers through HTTP/gRPC metadata, message headers, and task/job context; create one when absent and reject malformed values safely.
  - Add IDs to structured logs and standardized error responses while redacting them from user-controlled display where needed.
  - Preserve parent/child relationships across retries and fan-out, and test context cleanup between pooled requests.
- **probe**: Run a multi-hop fixture with a known identifier through HTTP, queue, and worker boundaries; assert every hop's record, trace, and error response contains the same approved identifier and that an absent or malformed input receives a safe new ID.
- **failure_modes**: A queue consumer loses the request ID and support cannot find the downstream failure; pooled worker context leaks one customer's ID into another log; retries create unrelated IDs and make one transaction look like several incidents.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://opentelemetry.io/docs/concepts/context-propagation/; https://www.w3.org/TR/trace-context/

### pii-secret-redaction
- **definition**: Secrets, credentials, tokens, payment data, and unnecessary personal data are removed or irreversibly masked before telemetry leaves the process. Retained records are access-controlled and the redaction policy covers logs, traces, metrics, errors, and diagnostic exports.
- **implementation**:
  - Centralize redaction at logging/export boundaries with key- and pattern-based rules for credentials, authorization headers, cookies, payment fields, and direct identifiers.
  - Prefer allowlisted fields and structured event schemas over serializing arbitrary request, response, or exception objects.
  - Use deterministic non-secret hashes only when correlation is required, with documented salt/key handling and rotation.
  - Restrict telemetry backend access by role, encrypt in transit and at rest, and audit reads and exports.
  - Run canary-secret tests against every exporter and keep a short quarantine path for suspected leaks.
- **probe**: Feed canary secrets and representative personal data through logging, tracing, error capture, and exporters, then assert values never appear in payloads or backend records and approved redaction markers do; inspect retention access controls.
- **failure_modes**: An authorization header serialized in an exception reaches a third-party log store; a payment number in a trace attribute is copied into support exports; broad telemetry access lets a compromised analyst retrieve customer identifiers.
- **severity**: critical
- **applies_if**: all
- **merges_into**: telemetry-pii-redaction
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html; https://opentelemetry.io/docs/specs/otel/logs/

### log-sampling-policy
- **definition**: Repetitive logs are sampled or rate-limited according to a documented policy while errors, security events, audit records, and representative exemplars remain available. Sampling is observable and does not silently remove the evidence needed for an incident or compliance investigation.
- **implementation**:
  - Define per-event or per-level rates and burst limits in logger or collector configuration.
  - Exempt security, audit, error, and first-occurrence events, and preserve counts of dropped records.
  - Use deterministic or tail-aware sampling keys so all records for a useful transaction can be retained together.
  - Export sampling decisions and volume metrics, with an audited emergency override and expiry.
- **probe**: Inspect logger and collector sampling rules, replay synthetic info, error, security, and audit events above the rate limit, and verify permitted classes are sampled while exempt classes are retained and drop counters increment.
- **failure_modes**: A uniform sampler discards every failed request during a rare outage; rate limiting hides a brute-force event; independent hop sampling leaves an unusable partial transaction trace in logs.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/collector/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### timestamp-clock-discipline
- **definition**: Telemetry uses one agreed timestamp format, timezone, and precision, while production hosts and containers maintain synchronized clocks. Event time and ingestion time are both available when transport delay matters.
- **implementation**:
  - Emit RFC 3339 UTC timestamps with documented fractional-second precision and a consistent clock source.
  - Enable and monitor NTP/chrony or the platform time-sync mechanism on hosts and nodes.
  - Record collector receipt time separately and reject or flag timestamps outside an allowed skew window.
  - Alert on clock offset and test ordering across services in deployment environments.
- **probe**: Parse sample timestamps for UTC and required precision, then inspect deployment configuration for an enabled time-sync mechanism and verify a staging skew test flags or handles out-of-window events.
- **failure_modes**: Clock drift makes a downstream span appear before its parent; an incident timeline orders a rollback after the damage it caused; latency windows undercount events crossing a minute boundary.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### durable-log-delivery
- **definition**: Logs travel through a durable, access-controlled pipeline that defines local buffering, backpressure, retry, and drop behavior. Pipeline loss is visible and bounded so an exporter outage does not silently erase incident evidence or exhaust application resources.
- **implementation**:
  - Use an agent or collector with bounded disk or memory queues, acknowledgements, retry backoff, and a documented maximum loss window.
  - Apply backpressure or controlled shedding before unbounded application blocking, preserving error, security, and audit classes.
  - Encrypt and authenticate each hop, restrict collector and backend permissions, and isolate tenant data.
  - Export queue depth, send failures, dropped records, oldest buffered age, and recovery drain rate.
- **probe**: An assessor must inspect queue limits, retry/backpressure and priority-drop policy, access controls, and operational evidence from a collector outage or controlled staging failure showing bounded loss and visible drops.
- **failure_modes**: A backend outage fills collector memory and kills the application; an unbounded disk queue fills the node and prevents recovery; silent UDP loss removes the timeline for the collector outage itself.
- **severity**: critical
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/collector/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### red-service-metrics
- **definition**: Each request or job emits RED metrics: rate, error outcomes, and latency distributions. Dimensions identify stable service and operation boundaries without turning customer or request data into labels.
- **implementation**:
  - Expose request/job counters, outcome counters or error ratio, and histogram or summary latency metrics.
  - Label by service, operation/route template, protocol, and bounded status or outcome class; never raw URLs or IDs.
  - Record queue time and processing time separately for asynchronous work where both affect users.
  - Define scrape intervals, histogram buckets, aggregation rules, and dashboards for success and failure paths.
- **probe**: Generate successful, failed, and slow fixture traffic for each representative operation and assert rate counters, error outcomes, and latency histograms change with the expected labels and bounded values.
- **failure_modes**: A route reports only total requests so a 500 spike looks healthy; averaging latency hides a long tail that times out users; raw route paths create a cardinality explosion during an attack.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/workbook/monitoring/; https://prometheus.io/docs/practices/instrumentation/

### use-resource-metrics
- **definition**: Every production resource is monitored for USE: utilization, saturation, and errors. The inventory includes compute, memory, storage, network, queues, connection pools, worker pools, and relevant managed-service limits.
- **implementation**:
  - Maintain a resource inventory mapping each resource to utilization, saturation/queue depth, and error metrics.
  - Use platform exporters for host and managed resources and application instrumentation for pools and queues.
  - Set saturation thresholds below hard exhaustion and track capacity, throttling, rejected work, and wait time.
  - Link resource panels to service dashboards and alert on sustained or rapidly worsening exhaustion.
- **probe**: Parse metric descriptors and assert utilization, saturation or queue-depth, and error metrics exist for each declared resource; verify a staging exhaustion fixture changes all applicable signals.
- **failure_modes**: A connection pool exhausts while CPU remains low and requests time out; disk fills because retention was not measured; queue backlog grows until messages expire while service request rate still looks normal.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/workbook/monitoring/; https://prometheus.io/docs/practices/instrumentation/

### sli-formalization
- **definition**: Each user-facing SLI is a formal ratio or event definition with an explicit good-event numerator, total-event denominator, eligibility rule, and measurement window. The specification states which failures count, which traffic is excluded, and how missing data is treated.
- **implementation**:
  - Store SLI specifications in reviewed configuration with owner, query, event source, window, and version.
  - Define eligibility at the user or request boundary and exclude only documented, authorized classes such as client-cancelled work.
  - Use stable event schemas and recording rules so numerator and denominator are computed consistently.
  - Validate the specification against known good, failed, timeout, retry, and missing-telemetry cases before publishing it.
- **probe**: An assessor must inspect each critical SLI specification and query, reconcile numerator and denominator to user-visible outcomes, review eligibility exclusions and missing-data behavior, and compare it with a representative incident.
- **failure_modes**: A service claims 99.99% availability by excluding all timeout responses; retries double-count successes and hide failed attempts; a denominator outage makes an SLI appear perfect because no events were recorded.
- **severity**: critical
- **applies_if**: all
- **merges_into**: slo-framework
- **sources**: https://sre.google/workbook/implementing-slos/; https://sre.google/workbook/monitoring/

### slo-error-budget
- **definition**: Every critical SLI has an approved SLO target, rolling measurement window, and policy for spending its resulting error budget. The decision is versioned with owner, rationale, exclusions, and consequences for releases, operations, and reliability work.
- **implementation**:
  - Record target, window, SLI version, service tier, and effective date in a reviewed SLO registry.
  - Compute remaining and consumed budget from the formal SLI and expose it beside user impact and release context.
  - Define thresholds and actions for healthy, warning, and exhausted budget states, including release gates or remediation priorities.
  - Review targets against user needs and historical performance without silently changing the window or exclusions.
- **probe**: Present the exact question: “For each critical SLI, which approved target and rolling window should govern, and what happens when its error budget is consumed?” Options: “accept the proposed target/window and policy,” “edit target/window/policy,” or “defer approval”; inspect the versioned decision and approver.
- **failure_modes**: Teams ship a risky release after spending the entire budget because no policy exists; a one-day window masks a month-long reliability regression; an undocumented target change makes customer commitments incomparable.
- **severity**: critical
- **applies_if**: all
- **merges_into**: slo-framework
- **sources**: https://sre.google/workbook/implementing-slos/; https://sre.google/workbook/error-budget-policy/

### bounded-cardinality
- **definition**: Metric label keys and values come from bounded, intentionally enumerated domains. User IDs, request IDs, full URLs, raw exception text, timestamps, and other unbounded values are prohibited from metric dimensions.
- **implementation**:
  - Maintain an allowlist of label keys and expected maximum value counts per metric family.
  - Normalize route templates, status classes, exception types, regions, and feature states before labeling.
  - Enforce descriptor linting and runtime cardinality budgets in SDKs, collectors, or backend recording rules.
  - Alert on new label keys, series growth, scrape failures, and ingestion cost before backend limits are reached.
- **probe**: Parse metric descriptors and fail if a label key matches an identifier, URL, timestamp, or free-text pattern; replay varied traffic and fail if observed distinct values exceed each configured budget.
- **failure_modes**: A path parameter creates one series per customer and takes down the metrics backend; raw exception messages create thousands of series during a dependency outage; a request ID label exhausts collector memory on normal traffic.
- **severity**: critical
- **applies_if**: all
- **sources**: https://prometheus.io/docs/practices/naming/; https://prometheus.io/docs/practices/instrumentation/

### otel-trace-instrumentation
- **definition**: OpenTelemetry APIs or supported auto-instrumentation create spans for inbound and outbound calls, databases, queues, and background jobs. Spans use semantic service, operation, status, and dependency attributes consistently so traces describe declared transaction boundaries.
- **implementation**:
  - Install supported instrumentation for the framework, HTTP/gRPC clients, database drivers, messaging libraries, and job runtime.
  - Create explicit spans around custom business and batch boundaries, recording bounded operation names and status outcomes.
  - Configure resource attributes for service name, version, environment, and deployment identity.
  - Ensure spans end on success, error, timeout, and cancellation, and sample/export through an authenticated collector.
- **probe**: Inspect dependencies and configuration, run a representative transaction, and assert exported spans cover each declared boundary with service, operation, parent, status, and duration metadata.
- **failure_modes**: A database call is absent from traces and responders blame the API layer; custom spans never close and inflate latency; inconsistent operation names fragment dependency analysis.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/concepts/signals/traces/; https://opentelemetry.io/docs/specs/semconv/

### trace-context-propagation
- **definition**: W3C trace context crosses HTTP, gRPC, messaging, and asynchronous task boundaries, preserving trace IDs and parent relationships. Fan-out, batch, and delayed work use explicit links when one parent cannot accurately represent all causal inputs.
- **implementation**:
  - Enable W3C `traceparent`/`tracestate` propagation in every supported client, server, producer, and consumer.
  - Inject and extract context exactly once at boundaries, validate untrusted headers, and start a new trace when context is absent or invalid.
  - Carry context in message metadata and persist it with job records where delayed processing requires it.
  - Use span links for batch inputs or fan-out aggregation and test context isolation in pooled workers.
- **probe**: Send a trace through each supported boundary and assert exported spans share the incoming trace ID with correct parent or link relationships; test absent, malformed, and cross-tenant context cases.
- **failure_modes**: A message consumer starts a new trace and hides queue delay; a batch worker assigns one parent to unrelated customers; unvalidated incoming context lets an attacker join or pollute another trace.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.w3.org/TR/trace-context/; https://opentelemetry.io/docs/concepts/context-propagation/

### trace-sampling-policy
- **definition**: Trace sampling rates and retention classes are documented, with priority for errors, high-latency outliers, rare routes, and representative baseline traces. Tail or adaptive sampling is used where the decision requires observing the completed trace rather than only its first span.
- **implementation**:
  - Define head sampling defaults and tail-sampling rules by status, duration, route rarity, tenant policy, and diagnostic value.
  - Preserve complete traces for errors and selected slow or rare exemplars while enforcing bounded collector memory and decision windows.
  - Keep sampling decisions consistent across a transaction and expose sampled/dropped counts by reason.
  - Provide audited temporary overrides and account for storage, privacy, and egress budgets.
- **probe**: Inspect collector or SDK policies, replay healthy, failed, slow, and rare traces, and verify required retention classes are kept, sampling decisions are consistent, and drop reasons are measurable.
- **failure_modes**: Head sampling drops a trace that later fails downstream; a tail sampler runs out of memory during a traffic surge and loses all traces; only common healthy routes are retained, hiding a rare customer-facing regression.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/concepts/sampling/; https://opentelemetry.io/docs/collector/

### trace-attribute-safety
- **definition**: Span attributes contain only bounded, necessary, non-secret context, and exporters and trace backends enforce TLS, authentication, authorization, and least privilege. Instrumentation does not serialize complete requests, responses, credentials, or personal data by default.
- **implementation**:
  - Allowlist attribute keys and normalize route, method, status, service, and dependency fields.
  - Add static checks for credential, cookie, token, body, and direct-identifier attribute names and review custom instrumentation.
  - Require authenticated TLS exporters with scoped credentials, backend role controls, retention limits, and access audit logs.
  - Redact or hash approved correlation values before export and test error paths that attach exception context.
- **probe**: Static-scan instrumentation attribute keys and exporter configuration, then assert transport security, credential requirements, backend authorization, and absence of sensitive canaries in exported spans.
- **failure_modes**: HTTP headers captured as span attributes expose bearer tokens; an unauthenticated collector endpoint leaks traces on the network; a full SQL statement embeds customer data in a retained trace.
- **severity**: critical
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/concepts/signals/traces/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### slo-burn-rate-alerts
- **definition**: Paging alerts are derived from rapid and sustained error-budget consumption, while slower trends generate nonpaging notifications. Burn-rate windows and thresholds are tied to the approved SLO and distinguish customer impact from telemetry or dependency context.
- **implementation**:
  - Compute short- and long-window burn rates from the SLI's good and total events using reviewed recording rules.
  - Page only when combined windows indicate meaningful budget consumption; route warning trends to tickets or chat.
  - Include affected SLO, current burn, estimated budget exhaustion, and dashboard/runbook links in the alert.
  - Test alert delay, recovery, missing-data behavior, and inhibition during maintenance.
- **probe**: An assessor must inspect burn-rate formulas, windows, thresholds, routing, and runbook evidence, then confirm synthetic user-impact scenarios page at the intended urgency while benign infrastructure fluctuations do not.
- **failure_modes**: A CPU threshold pages repeatedly while users are healthy; a slow burn is missed until the monthly budget is gone; an inverted numerator pages on recovery and trains responders to ignore alerts.
- **severity**: critical
- **applies_if**: all
- **merges_into**: slo-framework
- **sources**: https://sre.google/workbook/alerting-on-slos/; https://sre.google/workbook/error-budget-policy/

### symptom-based-pages
- **definition**: Paging alerts assert a customer-impacting symptom such as failed requests, unavailable journeys, or SLO burn, rather than guessing a particular cause. Likely-cause metrics remain linked nonpaging context so novel failure modes still page on observed impact.
- **implementation**:
  - Define paging conditions from user-facing SLIs, journey probes, or error-budget consumption.
  - Attach dependency, saturation, deploy, and pipeline signals as annotations or dashboard panels, not independent pages by default.
  - Set severity and routing from affected users, scope, duration, and data/safety risk.
  - Review alerts against historical incidents and silence or inhibit duplicates without suppressing the primary symptom.
- **probe**: An assessor must inspect each page's expression and evidence of customer impact, verify likely-cause rules are nonpaging context, and replay a novel dependency failure to ensure the symptom page fires.
- **failure_modes**: A memory alert pages for harmless cache growth while a user-visible failure has no page; a new outage mode bypasses a cause-specific rule; duplicate cause pages split responders across teams.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/workbook/alerting-on-slos/; https://sre.google/workbook/monitoring/

### actionable-alert-metadata
- **definition**: Every paging alert carries enough context for a responder to identify ownership, urgency, impact, and the first safe action. Metadata is validated before deployment and links resolve to the relevant runbook, dashboard, and trace or query view.
- **implementation**:
  - Require owner, severity, affected SLI, observed impact, first action, runbook URL, and dashboard/trace link labels or annotations.
  - Use templates to derive service, environment, and alert identity consistently and forbid free-form missing fields.
  - Validate URLs and referenced recording rules in CI or alert deployment checks.
  - Include deduplication identity, start time, and recovery condition in notifications.
- **probe**: Parse alert definitions and assert required labels/annotations and resolvable runbook URLs for every paging rule; reject rules with empty owner, impact, first-action, SLI, or dashboard fields.
- **failure_modes**: A page reaches an unstaffed team because owner metadata is absent; responders spend minutes finding the affected dashboard; a stale runbook recommends a destructive action during a recovery.
- **severity**: critical
- **applies_if**: all
- **sources**: https://prometheus.io/docs/alerting/latest/overview/; https://sre.google/workbook/incident-response/

### alert-routing-escalation
- **definition**: Alerts route by service and environment to a staffed primary with tested escalation, deduplication, inhibition, and maintenance-silence controls. Routing behavior is explicit for unknown owners, unowned alerts, and failures in the notification provider.
- **implementation**:
  - Define label-based routes, receiver ownership, escalation timers, and secondary/fallback contacts as versioned policy.
  - Use stable grouping and deduplication keys while preserving distinct impacted services and severities.
  - Configure inhibition for redundant symptoms and time-bounded maintenance silences with audit trails.
  - Periodically send labeled test alerts and monitor notification delivery, acknowledgment, and escalation latency.
- **probe**: Load routing policy, send a labeled test alert, and assert recipient, escalation timer, deduplication key, inhibition, maintenance-silence behavior, and fallback when the primary receiver fails.
- **failure_modes**: Duplicate pages wake the entire organization; a silence accidentally suppresses a critical service; a departed owner causes alerts to disappear without escalation.
- **severity**: important
- **applies_if**: all
- **sources**: https://prometheus.io/docs/alerting/latest/overview/; https://sre.google/sre-book/being-on-call/

### service-dashboard
- **definition**: Each service has a maintained operational dashboard that connects user impact to system behavior. It includes RED, SLO/error budget, dependency health, saturation, deploys, logs, traces, and runbooks in a responder-oriented layout.
- **implementation**:
  - Put service and environment selectors first and use route-template and bounded dimensions.
  - Show current and historical RED plus SLO/burn, dependency, queue/pool, and resource panels with consistent time windows.
  - Add deploy/config/feature-flag annotations and direct links to representative logs, traces, alerts, and runbooks.
  - Assign an owner, review dashboard queries after schema changes, and keep a minimal incident view usable when dependencies are degraded.
- **probe**: An assessor must open each critical service dashboard during a representative incident scenario and verify impact, scope, dependencies, saturation, recent changes, logs, traces, and first-response documentation are reachable without ad hoc assembly.
- **failure_modes**: Responders cannot tell whether a latency spike affects one region or all users; a dashboard omits deploys and delays rollback; broken panel queries leave a green-looking empty view.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/workbook/monitoring/; https://sre.google/workbook/incident-response/

### dashboards-as-code
- **definition**: Dashboards, recording rules, alert rules, and panel queries are versioned, reviewed configuration rather than manually maintained mutable state. Changes have an owner and pass rendering or smoke validation against a representative data source.
- **implementation**:
  - Store definitions in the same review workflow as service configuration and require code ownership.
  - Parameterize environment and service identifiers while preserving stable panel and alert identities.
  - Validate syntax, query references, datasource permissions, required panels, and renderability in CI or a staging observability stack.
  - Deploy definitions automatically with rollback and record the configuration version in dashboard metadata.
- **probe**: Locate dashboard, recording-rule, and alert definitions in version control, then render or lint them in CI against a test data source and assert required panels and queries resolve.
- **failure_modes**: A manually edited alert disappears during migration; a typo deploys an empty panel and hides a live incident; dashboard drift causes staging and production responders to use different thresholds.
- **severity**: important
- **applies_if**: all
- **sources**: https://prometheus.io/docs/alerting/latest/overview/; https://sre.google/workbook/monitoring/

### synthetic-journeys
- **definition**: Authenticated-safe black-box probes exercise critical user journeys from relevant regions and report availability and latency against journey SLOs. Credentials, test data, side effects, and cleanup are controlled so the probe represents a user without creating customer impact.
- **implementation**:
  - Define a small set of critical flows with synthetic accounts, isolated data, idempotent operations, and cleanup.
  - Run probes from regions and network paths representative of users, with browser/API checks and dependency assertions.
  - Measure journey success, step latency, and failure class, retaining screenshots or traces without personal data.
  - Alert on sustained journey SLO burn and include probe region, release, and runbook context.
- **probe**: An assessor must review journey definitions, synthetic identity/data isolation, geographic coverage, cleanup, cadence, and alert thresholds, then execute a safe flow and confirm evidence reaches the dashboard.
- **failure_modes**: Internal health checks stay green while a broken login flow blocks all users; a synthetic checkout creates real orders; one probe region hides a regional DNS failure.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://sre.google/workbook/monitoring/

### error-event-tracking
- **definition**: Unhandled exceptions and handled failures are captured as grouped events with stable fingerprints, stack traces, release, environment, request/trace IDs, and regression status. Captured context is sufficient to diagnose the failure but excludes secrets and unnecessary personal data.
- **implementation**:
  - Install an exception SDK or OpenTelemetry exception event integration at process, framework, and worker boundaries.
  - Normalize fingerprints by exception type and code location while allowing meaningful operation-specific grouping.
  - Attach release/build, environment, service, deployment, and correlation context plus safe breadcrumbs.
  - Mark first-seen, regressed, resolved, and ignored states and link events to deploys and SLO impact.
- **probe**: Trigger a known exception and assert one grouped event includes stack, release, environment, and correlation fields, records handled/unhandled status, and contains no sensitive canary values.
- **failure_modes**: Every request creates a separate issue because stack fingerprints include IDs; a handled payment failure is never tracked; an exception event leaks request headers into the error vendor.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### safe-production-profiling
- **definition**: Production profiling samples CPU, allocations, locks, and I/O with bounded overhead and controlled access. Profiles are correlated to service, release, and time while collection can be disabled quickly if overhead, privacy, or security risk appears.
- **implementation**:
  - Use a supported low-overhead profiler with explicit per-signal duration, frequency, and resource budgets.
  - Restrict collection and retrieval to authorized operators, require TLS, and redact or avoid sensitive values in profile labels and symbols.
  - Tag profiles with service, version, environment, instance cohort, and start/end timestamps.
  - Provide a feature flag or kill switch, retention limit, audit log, and documented approval for continuous profiling.
- **probe**: Present the exact question: “Should this service enable production profiling, which signals, overhead budget, access group, and retention should apply?” Options: “enable CPU/allocation/lock/I/O within the proposed budget,” “enable only selected signals,” or “do not enable”; inspect the recorded approval and disable path.
- **failure_modes**: Continuous profiling adds enough CPU overhead to worsen a latency incident; profile labels expose tenant identifiers; an unprotected profile endpoint reveals code paths and operational secrets.
- **severity**: important
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/concepts/signals/profiles/; https://go.dev/blog/pprof

### retention-cost-governance
- **definition**: Each telemetry signal has an owner-approved retention, access, deletion, archive, and sampling tier with a cost budget. Governance also defines legal hold, subject deletion where applicable, and emergency preservation without turning indefinite retention into the default.
- **implementation**:
  - Maintain a signal catalog with data classification, retention duration, storage tier, estimated volume/cost, access roles, and deletion owner.
  - Apply backend lifecycle policies for hot, warm, archive, and purge states and verify deletion jobs complete.
  - Set per-signal sampling/cardinality limits and charge or alert on budget variance.
  - Document legal hold, incident preservation, export approval, and access audit procedures.
- **probe**: Present the exact question: “For each signal, what retention, access, deletion, archive, sampling, cost budget, and legal-hold policy is approved?” Options: “accept the proposed tier,” “edit the tier or budget,” or “defer approval”; inspect policy enforcement and deletion evidence.
- **failure_modes**:
  - A SaaS team keeps full-fidelity traces and debug logs indefinitely; the observability bill exceeds the compute bill and an unnoticed PII leak lives in 2-year-old logs.
  - During a breach investigation, required evidence was already aged out by an unreviewed 7-day default retention.
- **severity**: important
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html; https://opentelemetry.io/docs/specs/otel/logs/

### telemetry-pipeline-health
- **definition**: The observability pipeline itself is monitored for collector/exporter availability, queue depth, export latency, dropped records, scrape gaps, and backend ingestion lag. A separate meta-alert path detects blind spots when the primary telemetry pipeline is broken.
- **implementation**:
  - Export pipeline self-metrics for receiver input, queue age/depth, retries, failures, drops, batch size, and exporter latency.
  - Send independent canary log, metric, and trace signals through the full path with known arrival deadlines.
  - Alert through a provider or channel independent of the failing collector/backend and document degraded-mode response.
  - Monitor scrape freshness and backend ingestion delay by signal, tenant, and environment.
- **probe**: Inject a canary log, metric, and trace, assert end-to-end arrival, then delay or drop each signal in staging and assert the independent meta-alert fires with the correct failure class.
- **failure_modes**: A collector outage makes application dashboards appear healthy because no new data arrives; exporter retries fill memory and kill agents; backend ingestion lag causes responders to roll back based on stale metrics.
- **severity**: critical
- **applies_if**: all
- **sources**: https://opentelemetry.io/docs/collector/; https://sre.google/workbook/monitoring/

### change-release-correlation
- **definition**: Deploy, configuration, feature-flag, schema, and build identifiers are attached to telemetry and rendered as change annotations on operational views. Correlation lets responders compare impact before and after a specific change without guessing from timestamps.
- **implementation**:
  - Set OpenTelemetry resource attributes and structured fields for service, version, build, deployment, commit, environment, and release cohort.
  - Emit change events for deploys, config changes, flag transitions, migrations, and rollbacks with actor and timestamp.
  - Add release annotations to dashboards and filter logs, traces, metrics, and error events by version or cohort.
  - Keep identifiers immutable and bounded, and verify canary and rollback metadata is present across every signal.
- **probe**: Deploy a canary version and assert its identifier appears in logs, metrics, traces, error events, and dashboard annotations; perform a config/flag change and verify its change event and rollback annotation are queryable.
- **failure_modes**: A bad release is mistaken for ambient load because metrics have no version; a feature flag regression cannot be isolated by cohort; a schema migration breaks consumers but no change event links it to the timeline.
- **severity**: important
- **applies_if**: all
- **merges_into**: release-telemetry-attribution
- **sources**: https://opentelemetry.io/docs/specs/semconv/; https://sre.google/workbook/monitoring/

### health-check-separation
- **definition**: Liveness, readiness, startup, and dependency health checks are separate contracts with distinct consumers and failure semantics. Probes reflect whether an instance can serve traffic, not merely whether its process exists, and dependency failures do not automatically trigger restart loops.
- **implementation**:
  - Expose separate endpoints or handlers for liveness, readiness, and startup, with documented status codes and timeouts.
  - Keep liveness shallow and process-local; make readiness include required serving dependencies and startup allow initialization time.
  - Configure orchestrator probe periods, failure thresholds, grace periods, and routing behavior independently.
  - Do not use health handlers to perform expensive checks or leak dependency credentials/details; instrument probe latency and outcomes.
- **probe**: Inspect orchestrator configuration and exercise each endpoint through startup, process-alive/dependency-down, ready, and draining states; assert startup permits initialization, liveness remains process-local, readiness removes the instance from routing, and recovery restores it.
- **failure_modes**: A database blip makes liveness fail and every pod restarts, extending the outage; readiness reports green before migrations finish and traffic hits a broken instance; a slow dependency check times out the probe and causes churn.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: health-check-contracts
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/; https://sre.google/workbook/monitoring/

### pipeline-freshness-slis
- **definition**: Data pipelines define SLIs for freshness, completeness, correctness, throughput, lag, and successful recovery or replay. Each SLI specifies the data population, event time versus processing time, lateness tolerance, and treatment of missing, duplicate, or corrupted records.
- **implementation**:
  - Publish per-dataset timestamps, expected and received counts, quality checks, lag distributions, and replay outcomes.
  - Define freshness windows and completeness denominators from source manifests or watermarks rather than scheduler success alone.
  - Track duplicates, invalid records, late arrivals, backfills, and recovery duration as explicit outcomes.
  - Tie alerts and SLOs to downstream user or contractual impact and version schema/quality rules.
- **probe**: An assessor must inspect each critical pipeline's SLI definitions and evidence for on-time, missing, duplicate, corrupt, replayed, and late data; verify scheduler success cannot alone make the SLI green.
- **failure_modes**:
  - Nightly ETL silently stalls for 3 days; dashboards show green because the scheduler ran, while executives decide on stale data.
  - A Kafka consumer group lags behind a traffic spike and nobody notices until downstream ML features are hours old.
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://sre.google/workbook/implementing-slos/; https://sre.google/workbook/monitoring/

### model-service-quality
- **definition**: ML services measure inference availability, latency, and error rate together with data quality, drift, and model-specific safety or quality outcomes. Infrastructure health is not treated as evidence that model inputs and outputs remain useful or safe.
- **implementation**:
  - Instrument request outcome, queue/inference latency, timeout, resource, and dependency metrics by bounded model/version/cohort dimensions.
  - Define input schema validity, missingness, distribution drift, output range, abstention, and quality proxy metrics appropriate to the model.
  - Correlate quality events with model, feature, data, and release identifiers while avoiding raw sensitive payloads.
  - Establish review thresholds, rollback or quarantine actions, delayed-label evaluation, and human escalation for safety failures.
- **probe**: An assessor must inspect model-specific SLI definitions, drift/quality thresholds, cohort coverage, delayed-label handling, and rollback evidence; verify a serving-green but input-invalid fixture is detected as degraded quality.
- **failure_modes**:
  - A model provider ships a silent behavior change; infra metrics stay green while answer quality craters and support tickets spike.
  - Input drift after a client app update doubles the refusal rate; without quality SLIs the regression is attributed to 'user error' for weeks.
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://sre.google/workbook/implementing-slos/; https://opentelemetry.io/docs/specs/semconv/

### spa-user-telemetry
- **definition**: Key browser journeys emit privacy-safe real-user telemetry for page load, interaction latency, frontend errors, availability, and release version. Client events are sampled and bounded while retaining enough region, device, connection, and journey context to explain user impact.
- **implementation**:
  - Instrument navigation, resource, long-task, interaction, route-transition, and frontend exception events with stable journey names.
  - Attach release, environment, browser/OS cohort, region, and network class; never collect raw form values, tokens, URLs with identifiers, or keystrokes.
  - Use consent-aware collection, sampling, batching, offline handling, and a kill switch for client telemetry.
  - Link browser correlation/trace IDs to backend requests through safe propagation and show client/server views together.
- **probe**: Run a browser smoke flow and assert telemetry events contain journey, timing, error, cohort, and release fields without personal data; simulate an asset or API failure and verify a frontend error event is emitted.
- **failure_modes**: Backend RED is green while a broken JavaScript bundle blocks users; a regional network issue is invisible to server metrics; browser telemetry captures password input or full query-string identifiers.
- **severity**: important
- **applies_if**: spa
- **sources**: https://opentelemetry.io/docs/specs/semconv/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### mobile-crash-performance
- **definition**: Mobile telemetry captures crash-free sessions, startup and network latency, offline-queue health, release, OS, and device cohorts with user consent and bounded sampling. Events survive transient disconnection without retaining direct identifiers or unbounded local data.
- **implementation**:
  - Install crash and performance capture for supported platforms and include release, build, OS, device class, network, and app-state context.
  - Queue events locally with size/age limits, encrypt storage, upload after reconnect, and drop low-priority data before crash or consent data.
  - Redact payloads, avoid direct identifiers, honor consent/opt-out and deletion requirements, and rotate upload credentials.
  - Dashboard crash-free sessions and startup/network cohorts by release, region, OS, and device with regression thresholds.
- **probe**: Execute an offline and crash fixture on supported builds, assert events queue within limits, upload after reconnect, carry release/device context, and omit direct identifiers; verify opt-out prevents collection.
- **failure_modes**:
  - A release crashes only on Android 12 with a specific OEM WebView; server metrics are clean while 8% of users cannot open the app.
  - An offline-queue bug duplicates orders after reconnect; without queued-event telemetry the duplicates look like user double-taps.
- **severity**: important
- **applies_if**: mobile
- **sources**: https://opentelemetry.io/docs/specs/semconv/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### cli-diagnostics
- **definition**: CLI diagnostics are machine-readable and opt-in, while exit codes retain their documented automation semantics. Diagnostic output includes safe command and version context and offers a debug mode that never exposes secrets or local sensitive data by default.
- **implementation**:
  - Provide a stable JSON diagnostics format with schema version, command name, version, environment class, event, and safe message fields.
  - Keep stdout for requested machine output and stderr for human diagnostics; reserve stable nonzero exit codes for documented failure classes.
  - Make telemetry opt-in or explicitly consented, with redaction, local disable/configuration, and bounded buffering.
  - Gate debug output behind a flag, scrub environment variables, paths, tokens, and arguments, and include a support correlation ID when enabled.
- **probe**: An assessor must run representative success, validation, network, authentication, and debug invocations, inspect JSON/schema and stdout/stderr separation, verify documented exit codes, and confirm secrets and local data are absent unless explicitly supplied as safe context.
- **failure_modes**:
  - A CI pipeline parses a CLI's human-readable error text; a minor rewording silently breaks deployment automation for every team.
  - A debug flag dumps credentials into a shared log shipper because diagnostics had no redaction contract.
- **severity**: important
- **applies_if**: cli
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### incident-severity-matrix
- **definition**: Incident severity is assigned from user impact, scope, duration, and data or safety risk, with explicit paging, escalation, communication, and decision authority for each level. The matrix supports consistent initial classification and documented reassessment as impact changes.
- **implementation**:
  - Define severity levels using measurable impact bands, affected cohorts/regions, duration, and security/safety criteria.
  - Map each level to page recipients, incident commander requirement, escalation deadlines, status updates, and executive/customer communications.
  - Include downgrade/upgrade rules, uncertainty handling, and examples for partial outages and data integrity events.
  - Keep the matrix versioned, accessible offline, and reviewed after incidents.
- **probe**: Present the exact question: “Which severity applies to this incident given affected users, scope, duration, and data/safety risk, and what paging/escalation/comms obligations follow?” Options: “declare the matrix level,” “declare a higher level pending investigation,” or “request incident-lead review”; inspect the recorded rationale and reassessment.
- **failure_modes**:
  - A full payment outage is treated as SEV-3 because 'only one region' is affected; escalation is delayed 40 minutes.
  - Every warning pages the on-call as SEV-1; within a quarter the team ignores pages and misses a real one.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/workbook/incident-response/; https://sre.google/sre-book/managing-incidents/

### incident-command-protocol
- **definition**: Incidents use a documented command structure with incident commander, operations, communications, and scribe roles, a single source of truth, and explicit handoff protocol. Role separation keeps diagnosis, changes, communication, and timeline capture coordinated during long or high-severity events.
- **implementation**:
  - Define role responsibilities, authority boundaries, paging triggers, and a command channel or incident record template.
  - Maintain one UTC timeline containing observations, hypotheses, changes, approvals, and outcomes.
  - Require explicit handoff with current impact, risks, actions, owners, and next update time; record role changes.
  - Link alerts, dashboards, traces, deploys, customer updates, and decisions from the incident record.
- **probe**: An assessor must inspect a recent incident record or run a tabletop and verify roles were assigned, a single timeline captured decisions and changes, handoff included state and next actions, and communications were coordinated.
- **failure_modes**:
  - Three engineers simultaneously roll back, scale, and fail over during one outage; the conflicting actions extend it from 15 minutes to 2 hours.
  - A 6-hour incident crosses a shift change with no scribe; the incoming team re-investigates everything from scratch.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/managing-incidents/; https://sre.google/workbook/incident-response/

### on-call-readiness
- **definition**: A staffed primary and secondary on-call rotation has escalation contacts, handoff notes, access checks, alert tests, and explicit fatigue and backup policy. Readiness is continuously verified before an incident, not inferred from a schedule entry.
- **implementation**:
  - Publish current primary/secondary rotations, escalation timers, holiday coverage, and manager/service-owner fallbacks.
  - Run periodic page delivery and acknowledgment tests and check access to production, dashboards, runbooks, and incident tools.
  - Require shift handoff notes covering active risks, changes, silences, and known alerts.
  - Set maximum workload/fatigue limits, swap and backup procedures, and post-incident support expectations.
- **probe**: An assessor must inspect the current rotation, recent handoff, access-check results, page acknowledgment test, escalation path, and fatigue/backup policy for each critical service.
- **failure_modes**:
  - A critical alert fires at 03:00 to a rotation whose primary left the company; the page dies in an unmonitored inbox.
  - The on-call engineer lacks prod access after an IAM cleanup and spends the first hour of an outage requesting permissions.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/being-on-call/; https://sre.google/workbook/incident-response/

### status-page-comms
- **definition**: A status-page and customer-communication policy defines when and how to publish impact, scope, privacy-safe detail, update cadence, and recovery notices. Approved templates and ownership keep updates accurate and consistent across incident channels.
- **implementation**:
  - Define publication thresholds by severity, affected products/regions, audience, and regulatory or contractual obligations.
  - Maintain templates for investigation, identified issue, mitigation, monitoring, and resolved states with next-update times.
  - Assign communications owner and approver, protect customer and security-sensitive details, and synchronize support/internal messages.
  - Record update timestamps, impact duration, and final recovery confirmation; test status-page access during provider outages.
- **probe**: Present the exact question: “For this incident, should a status update be published, to which audience, with what impact scope, cadence, and privacy-safe detail?” Options: “publish now using the incident template,” “prepare but hold for approval,” or “do not publish with documented rationale”; inspect owner and next-update commitment.
- **failure_modes**:
  - During a visible outage the status page stays green for 90 minutes; customers learn about it from social media and churn follows.
  - An engineer posts an unreviewed update naming a vendor as the cause; the claim is wrong and the retraction damages trust further.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/sre-book/managing-incidents/; https://sre.google/workbook/incident-response/

### blameless-postmortems
- **definition**: Qualifying incidents produce blameless postmortems that explain system conditions rather than assign individual fault. The record contains a UTC timeline, user impact and SLO/error-budget analysis, contributing factors, and owned, dated corrective actions tracked to closure.
- **implementation**:
  - Define incident qualification, facilitator/owner, review deadline, and participants with psychological-safety expectations.
  - Capture detection, response, decisions, changes, mitigations, contributing conditions, and what worked as well as failures.
  - Link impact to SLI/SLO and error-budget consumption, and classify actions by prevention, detection, response, and learning.
  - Assign each action an owner and due date, track status in the normal work system, and verify completion effectiveness.
- **probe**: An assessor must inspect qualifying postmortems for timeline, impact/SLO analysis, contributing factors, blameless language, owned dated actions, and evidence that actions reached closure or were explicitly re-planned.
- **failure_modes**:
  - An engineer blamed for an outage stops reporting near-misses; the same failure mode recurs six months later with wider blast radius.
  - Postmortem action items land in a doc nobody tracks; the identical incident repeats three times in a year.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/workbook/postmortem-culture/; https://sre.google/workbook/error-budget-policy/
