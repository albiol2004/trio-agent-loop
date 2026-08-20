# Observability & incident response — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 2). Raw item list, pre-synthesis.

### telemetry-ownership
- **what**: Assign a named owner and review cadence for each production signal, dashboard, alert, and runbook.
- **why**: Unowned telemetry becomes stale and leaves responders without a maintained path during outages.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/workbook/monitoring/; https://sre.google/workbook/incident-response/

### structured-log-schema
- **what**: Emit machine-parseable structured log records with UTC timestamp, severity, service, version, environment, event name, and message fields.
- **why**: Free-form or inconsistent records defeat correlation, querying, and automated detection during incidents.
- **check**: probe
- **probe**: Parse representative stdout and collector output as JSON and assert the required fields and types are present.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### log-level-contract
- **what**: Define level semantics and default production thresholds so debug and trace logging are off unless explicitly and safely enabled.
- **why**: Misclassified or verbose logs either hide urgent failures or exhaust ingestion budgets.
- **check**: probe
- **probe**: Read runtime logging configuration and assert production defaults plus an auditable temporary override path.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### request-correlation
- **what**: Carry a request or correlation identifier through every hop and include it in logs, error responses, and support-facing references.
- **why**: Responders cannot reconstruct one customer transaction when each component emits unrelated identifiers.
- **check**: probe
- **probe**: Run a multi-hop fixture with a known identifier and assert every hop's record and response contains the same identifier.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://opentelemetry.io/docs/concepts/context-propagation/; https://www.w3.org/TR/trace-context/

### pii-secret-redaction
- **what**: Redact secrets, credentials, tokens, payment data, and unnecessary personal data before telemetry leaves the process and restrict access to retained records.
- **why**: Observability can become a breach channel that leaks credentials or violates privacy obligations.
- **check**: probe
- **probe**: Feed canary secrets and representative personal data through logging and exporters, then assert the values never appear and approved redaction markers do.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html; https://opentelemetry.io/docs/specs/otel/logs/

### log-sampling-policy
- **what**: Use documented sampling and rate limits for repetitive logs while retaining errors, security or audit events, and representative exemplars.
- **why**: Unbounded volume drives cost and can drop the rare evidence needed to diagnose an outage.
- **check**: probe
- **probe**: Inspect logger or collector sampling rules and replay synthetic info, error, and audit events to verify only permitted classes are sampled.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/collector/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### timestamp-clock-discipline
- **what**: Emit timestamps in one agreed format and synchronize host or container clocks across production environments.
- **why**: Clock skew misorders events and corrupts latency windows, incident timelines, and trace joins.
- **check**: probe
- **probe**: Parse sample timestamps for UTC and required precision, then check deployment configuration for an enabled time-sync mechanism.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### durable-log-delivery
- **what**: Ship logs through a durable, access-controlled pipeline with bounded local buffering, backpressure behavior, and drop visibility.
- **why**: Collector outages can silently erase the evidence needed to understand the outage they cause.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://opentelemetry.io/docs/collector/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### red-service-metrics
- **what**: Instrument each request or job with RED metrics for rate, errors, and latency distributions using stable service and operation dimensions.
- **why**: Without the golden signals operators detect symptoms late and cannot quantify user impact.
- **check**: probe
- **probe**: Generate successful, failed, and slow fixture traffic and assert counters and latency histograms change for each outcome.
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/monitoring/; https://prometheus.io/docs/practices/instrumentation/

### use-resource-metrics
- **what**: Measure every production resource with USE metrics for utilization, saturation, and errors, including CPU, memory, storage, network, queues, and pools.
- **why**: Resource exhaustion causes cascading failures before application-level error rates necessarily rise.
- **check**: probe
- **probe**: Parse metric descriptors and assert utilization, saturation or queue depth, and error metrics exist for each declared resource.
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/workbook/monitoring/; https://prometheus.io/docs/practices/instrumentation/

### sli-formalization
- **what**: Define every user-facing SLI as an explicit good-event numerator, total-event denominator, eligibility rule, and measurement window.
- **why**: Ambiguous SLIs make availability claims incomparable and let monitoring exclude real failures.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/implementing-slos/; https://sre.google/workbook/monitoring/

### slo-error-budget
- **what**: Set an approved SLO target, rolling window, and error-budget policy for each critical SLI and version the decision.
- **why**: Teams cannot prioritize reliability or release risk without an explicit budget for failure.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/implementing-slos/; https://sre.google/workbook/error-budget-policy/

### bounded-cardinality
- **what**: Bound metric label values and prohibit unbounded dimensions such as user IDs, request IDs, URLs, and raw exception text.
- **why**: Cardinality explosions overload collectors and monitoring backends exactly when traffic or failures spike.
- **check**: probe
- **probe**: Parse metric descriptors and fail if a label key matches an identifier pattern or if observed distinct values exceed its configured budget.
- **applies_if**: all
- **severity**: critical
- **sources**: https://prometheus.io/docs/practices/naming/; https://prometheus.io/docs/practices/instrumentation/

### otel-trace-instrumentation
- **what**: Use OpenTelemetry APIs or supported auto-instrumentation for inbound and outbound calls, databases, queues, and background jobs with semantic attributes.
- **why**: Inconsistent hand-built spans leave blind spots and make cross-service traces impossible to compare.
- **check**: probe
- **probe**: Inspect dependencies and configuration and run a representative transaction, asserting spans for each declared boundary carry service and operation metadata.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/concepts/signals/traces/; https://opentelemetry.io/docs/specs/semconv/

### trace-context-propagation
- **what**: Propagate W3C trace context across HTTP, gRPC, messaging, and asynchronous task boundaries while linking fan-out and batch parents.
- **why**: Broken propagation fragments one incident into misleading per-service traces and hides causal latency.
- **check**: probe
- **probe**: Send a trace through each supported boundary and assert exported spans share the incoming trace ID with correct parent or link relationships.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.w3.org/TR/trace-context/; https://opentelemetry.io/docs/concepts/context-propagation/

### trace-sampling-policy
- **what**: Document trace sampling rates and preserve errors, high-latency outliers, rare routes, and representative baseline traces with tail or adaptive sampling where needed.
- **why**: Sampling that keeps only healthy traffic discards the exact traces responders need.
- **check**: probe
- **probe**: Inspect collector or SDK policies and replay healthy, failed, slow, and rare traces to verify the required retention classes.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/concepts/sampling/; https://opentelemetry.io/docs/collector/

### trace-attribute-safety
- **what**: Keep secrets and unnecessary personal data out of span attributes and enforce TLS, authentication, and least privilege on exporters and backends.
- **why**: Distributed traces cross trust boundaries and can expose more customer context than logs.
- **check**: probe
- **probe**: Static-scan instrumentation attribute keys and exporter configuration, then assert transport security and credentials are required.
- **applies_if**: all
- **severity**: critical
- **sources**: https://opentelemetry.io/docs/concepts/signals/traces/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### slo-burn-rate-alerts
- **what**: Page on fast- and slow-burn error-budget consumption derived from SLI events, with nonpaging trend notifications.
- **why**: Raw infrastructure thresholds produce alert fatigue while missing sustained user-visible degradation.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/alerting-on-slos/; https://sre.google/workbook/error-budget-policy/

### symptom-based-pages
- **what**: Make paging alerts assert customer-impacting symptoms and keep likely-cause diagnostics as linked nonpaging context.
- **why**: Cause-based pages wake responders for harmless fluctuations and miss novel failure modes.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/alerting-on-slos/; https://sre.google/workbook/monitoring/

### actionable-alert-metadata
- **what**: Require each page to name owner, severity, affected SLI, observed impact, first action, runbook URL, and dashboard or trace link.
- **why**: A page without context increases time to acknowledge and encourages unsafe trial-and-error.
- **check**: probe
- **probe**: Parse alert definitions and assert required labels or annotations and resolvable runbook URLs for every paging rule.
- **applies_if**: all
- **severity**: critical
- **sources**: https://prometheus.io/docs/alerting/latest/overview/; https://sre.google/workbook/incident-response/

### alert-routing-escalation
- **what**: Route alerts by service and environment to a staffed primary with tested escalation, deduplication, inhibition, and maintenance-silence controls.
- **why**: Misrouted or duplicate pages delay ownership and exhaust on-call attention.
- **check**: probe
- **probe**: Load routing policy, send a labeled test alert, and assert recipient, escalation timer, deduplication key, and silence behavior.
- **applies_if**: all
- **severity**: important
- **sources**: https://prometheus.io/docs/alerting/latest/overview/; https://sre.google/sre-book/being-on-call/

### service-dashboard
- **what**: Give each service a maintained dashboard showing RED, SLO or error budget, dependency health, saturation, deploys, logs, traces, and runbooks.
- **why**: Responders waste critical minutes assembling a view of impact and scope from disconnected tools.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/workbook/monitoring/; https://sre.google/workbook/incident-response/

### dashboards-as-code
- **what**: Version dashboards, recording rules, alert rules, and panel queries as reviewed configuration with an owner and smoke validation.
- **why**: Manual dashboards drift from deployed behavior and disappear during migrations or incident handoffs.
- **check**: probe
- **probe**: Locate dashboard and alert definitions in version control and render or lint them in CI against a test data source.
- **applies_if**: all
- **severity**: important
- **sources**: https://prometheus.io/docs/alerting/latest/overview/; https://sre.google/workbook/monitoring/

### synthetic-journeys
- **what**: Run authenticated-safe black-box probes for critical user journeys from relevant regions and alert on availability and latency SLOs.
- **why**: Internal component health can remain green while real users cannot complete the journey.
- **check**: judgment
- **applies_if**: web-api
- **severity**: important
- **sources**: https://sre.google/workbook/monitoring/

### error-event-tracking
- **what**: Capture unhandled exceptions and handled failures with stable fingerprints, stack traces, release, environment, request or trace IDs, and regression status.
- **why**: Aggregated error rates alone hide new crash signatures and make regressions hard to prioritize.
- **check**: probe
- **probe**: Trigger a known exception and assert one grouped event includes stack, release, environment, and correlation fields without sensitive values.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### safe-production-profiling
- **what**: Enable low-overhead, access-controlled production profiling for CPU, allocation, lock, and I/O hot spots with deployment correlation and a disable switch.
- **why**: Metrics identify symptoms but not the code-level resource path causing latency, leaks, or saturation.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/concepts/signals/profiles/; https://go.dev/blog/pprof

### retention-cost-governance
- **what**: Set per-signal retention, access, deletion, archive, and sampling tiers with an owner, legal-hold path, and cost budget.
- **why**: Indefinite high-cardinality telemetry creates uncontrolled spend and increases breach impact.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html; https://opentelemetry.io/docs/specs/otel/logs/

### telemetry-pipeline-health
- **what**: Monitor collector or exporter availability, queue depth, export latency, dropped records, scrape gaps, and backend ingestion lag with a separate meta-alert path.
- **why**: A broken observability pipeline can make a severe outage appear healthy.
- **check**: probe
- **probe**: Inject a canary log, metric, and trace and assert end-to-end arrival plus alerts when each signal is delayed or dropped.
- **applies_if**: all
- **severity**: critical
- **sources**: https://opentelemetry.io/docs/collector/; https://sre.google/workbook/monitoring/

### change-release-correlation
- **what**: Attach deploy, configuration, feature-flag, schema, and build identifiers to telemetry and annotate dashboards with change events.
- **why**: Responders cannot distinguish a release regression from ambient load or dependency failure without change context.
- **check**: probe
- **probe**: Deploy a canary version and assert its identifier appears in logs, metrics, traces, error events, and dashboard annotations.
- **applies_if**: all
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/semconv/; https://sre.google/workbook/monitoring/

### health-check-separation
- **what**: Separate liveness, readiness, startup, and dependency health checks and ensure probes reflect whether traffic can be served rather than process existence.
- **why**: A single overloaded health endpoint causes restart loops or routes traffic to an instance that cannot serve requests.
- **check**: probe
- **probe**: Inspect orchestrator configuration and exercise each endpoint while simulating startup, dependency failure, and serving-readiness states.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/; https://sre.google/workbook/monitoring/

### pipeline-freshness-slis
- **what**: Define data-pipeline SLIs for freshness, completeness, correctness, throughput, lag, and successful recovery or replay.
- **why**: A green scheduler can still deliver late, missing, duplicated, or corrupted data to downstream users.
- **check**: judgment
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://sre.google/workbook/implementing-slos/; https://sre.google/workbook/monitoring/

### model-service-quality
- **what**: Define ML-service SLIs for inference availability, latency, error rate, data quality or drift signals, and safety or quality outcomes appropriate to the model.
- **why**: Serving infrastructure can be healthy while model inputs or outputs silently degrade product behavior.
- **check**: judgment
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://sre.google/workbook/implementing-slos/; https://opentelemetry.io/docs/specs/semconv/

### spa-user-telemetry
- **what**: Instrument key browser journeys with privacy-safe real-user telemetry for page load, interaction latency, frontend errors, availability, and release version.
- **why**: Backend RED metrics miss client-side failures, regional network problems, and broken assets that users experience.
- **check**: probe
- **probe**: Run a browser smoke flow and assert telemetry events contain journey, timing, error, and release fields without personal data.
- **applies_if**: spa
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/semconv/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### mobile-crash-performance
- **what**: Capture crash-free sessions, startup and network latency, offline-queue health, release, OS, and device cohorts with consent and bounded sampling.
- **why**: Mobile failures are often version- or device-specific and are invisible to server-only monitoring.
- **check**: probe
- **probe**: Execute an offline and crash fixture on supported builds and assert events queue, upload after reconnect, and omit direct identifiers.
- **applies_if**: mobile
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/semconv/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### cli-diagnostics
- **what**: Make CLI diagnostics machine-readable and opt-in, preserve exit-code semantics, include safe command and version context, and expose a debug mode without secrets.
- **why**: Automation breaks when diagnostics are ambiguous, while default telemetry can violate user trust or leak local data.
- **check**: judgment
- **applies_if**: cli
- **severity**: important
- **sources**: https://opentelemetry.io/docs/specs/otel/logs/; https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html

### incident-severity-matrix
- **what**: Define incident severities from user impact, scope, duration, and data or safety risk with paging, escalation, and communication requirements for each level.
- **why**: Inconsistent severity decisions delay escalation and produce either under-response or alert fatigue.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/incident-response/; https://sre.google/sre-book/managing-incidents/

### incident-command-protocol
- **what**: Use a documented incident command structure with incident commander, operations, communications, and scribe roles plus a single timeline and handoff protocol.
- **why**: Multiple responders otherwise duplicate work, make conflicting changes, and lose decisions during long incidents.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/managing-incidents/; https://sre.google/workbook/incident-response/

### on-call-readiness
- **what**: Maintain staffed primary and secondary on-call rotations, escalation contacts, handoff notes, access checks, alert tests, and fatigue or backup policy.
- **why**: A technically correct alert still fails when nobody can acknowledge or safely act on it.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/being-on-call/; https://sre.google/workbook/incident-response/

### status-page-comms
- **what**: Publish a status-page and customer-communication policy with approved templates, update cadence, impact scope, privacy rules, and recovery notice.
- **why**: Silence or contradictory updates increase customer harm and support load during visible incidents.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/sre-book/managing-incidents/; https://sre.google/workbook/incident-response/

### blameless-postmortems
- **what**: Require blameless postmortems for qualifying incidents with a UTC timeline, impact and SLO-budget analysis, contributing factors, and owned dated corrective actions tracked to closure.
- **why**: Incidents recur when organizations document blame or anecdotes instead of changing systems and verifying follow-through.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/workbook/postmortem-culture/; https://sre.google/workbook/error-budget-policy/
