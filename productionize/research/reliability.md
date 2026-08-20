# Reliability & resilience engineering — research wave 1

Source: scout report (gpt-5.6-luna, wave 4). Raw item list, pre-synthesis.

### health-contract
- **what**: Expose separate, cheap liveness, readiness, and startup health contracts with documented status semantics and bounded latency.
- **why**: Conflated or slow probes can restart healthy processes, route traffic to broken instances, or amplify an outage.
- **check**: probe
- **probe**: Parse route registrations and deployment manifests for `/livez`, `/readyz`, and `/startupz` (or documented equivalents), then curl each during normal operation and an injected dependency failure and assert bounded latency and the expected status transition.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring

### liveness-process-only
- **what**: Keep liveness checks limited to whether the local process can make progress, leaving dependency and traffic eligibility checks to readiness.
- **why**: A dependency outage that makes every instance fail liveness causes restart storms and removes the capacity needed for recovery.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://sre.google/sre-book/addressing-cascading-failures/

### readiness-dependency-gating
- **what**: Make readiness fail for dependencies that are required for correct requests while keeping optional dependencies out of the readiness gate.
- **why**: Serving traffic from an instance that cannot execute its critical path turns a partial dependency outage into widespread user errors.
- **check**: probe
- **probe**: Start the service with a required database or broker unreachable, curl readiness and liveness, and assert readiness fails, liveness remains successful, and the orchestrator or load balancer excludes the instance.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring

### startup-probe
- **what**: Configure a startup-specific failure budget so slow initialization completes before liveness and readiness enforcement begins.
- **why**: Cold starts, migrations, or model loading can be mistaken for a dead process and repeatedly killed before becoming useful.
- **check**: probe
- **probe**: Parse workload manifests for a startup probe with explicit period, timeout, and failure threshold, then run a deliberately slow initialization fixture and assert it is not killed before the startup budget expires.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/

### health-check-budget
- **what**: Keep health handlers bounded and low-cost, avoid dependency fan-out, and give probe traffic its own rate and resource budget.
- **why**: Probe storms or recursive health checks can consume the capacity required to serve users and recover from the original fault.
- **check**: judgment
- **applies_if**: web-api
- **severity**: important
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring, https://sre.google/sre-book/handling-overload/

### sigterm-shutdown
- **what**: Handle SIGTERM by stopping admission of new work, canceling or completing in-flight work according to policy, closing resources, and exiting within the termination grace period.
- **why**: Abrupt termination drops requests, corrupts work, and causes duplicate processing during deploys, evictions, or node loss.
- **check**: probe
- **probe**: Parse signal handlers and termination-grace configuration, launch with an in-flight request, send SIGTERM, and assert new work is refused, in-flight work follows the documented policy, and the process exits before the grace deadline.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination, https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html

### connection-draining
- **what**: Coordinate endpoint deregistration, connection draining, keep-alive limits, and termination grace so existing connections finish without admitting new traffic.
- **why**: Killing a serving instance while clients hold connections produces avoidable resets, partial responses, and retry bursts.
- **check**: probe
- **probe**: Parse load-balancer deregistration and server keep-alive settings, then hold a long-lived request and keep-alive connection across termination and assert existing work completes or is canceled cleanly while new connections are not routed there.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination, https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html

### cancellation-propagation
- **what**: Propagate request, job, and shutdown cancellation through handlers, workers, and every downstream operation.
- **why**: Work that continues after its caller has gone away consumes scarce capacity and can outlive shutdown, creating cascading overload.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://sre.google/sre-book/addressing-cascading-failures/

### outbound-deadlines
- **what**: Set finite connect, handshake, operation, and total deadlines on every outbound HTTP, RPC, database, queue, DNS, and subprocess call.
- **why**: One unbounded dependency call can pin threads or workers indefinitely and exhaust the service under a slow or partitioned dependency.
- **check**: probe
- **probe**: Enumerate outbound client constructors and call sites with a language-aware scanner, assert each has finite connect and operation deadlines, and run a blackhole or delayed-server fixture to verify calls return within the configured bound.
- **applies_if**: all
- **severity**: critical
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://grpc.io/docs/guides/deadlines/

### deadline-budget
- **what**: Propagate one end-to-end deadline and reserve explicit budget for downstream work, retries, and response handling instead of stacking full per-hop timeouts.
- **why**: Independent hop timeouts multiply latency and allow retries to continue after the caller's useful deadline, causing tail-latency and retry cascades.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://grpc.io/docs/guides/deadlines/

### retry-classification
- **what**: Retry only transient failures and operations that are idempotent or protected by an idempotency key, never blindly retrying validation, authorization, or permanent errors.
- **why**: Retrying permanent failures wastes capacity, while retrying non-idempotent mutations can duplicate charges, writes, or jobs.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://learn.microsoft.com/en-us/azure/architecture/patterns/retry

### retry-exponential-jitter
- **what**: Use capped exponential backoff with randomized jitter and respect server retry hints when retrying eligible calls.
- **why**: Fixed or immediate retries synchronize clients into retry storms precisely while a dependency is recovering.
- **check**: probe
- **probe**: Parse retry policies for exponential growth, a maximum delay, and jitter, then run a failed-call harness with multiple clients and assert bounded increasing delays with non-identical schedules.
- **applies_if**: all
- **severity**: critical
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://learn.microsoft.com/en-us/azure/architecture/patterns/retry

### retry-budget
- **what**: Bound retries by attempts, elapsed deadline, and an aggregate retry budget so failure traffic cannot exceed a configured fraction of normal traffic.
- **why**: Unbounded retries amplify a small dependency failure into a system-wide overload and prevent recovery.
- **check**: probe
- **probe**: Parse retry and deadline configuration, inject a dependency that fails for a fixed interval, and assert per-request attempts and aggregate retry calls remain below the declared caps.
- **applies_if**: all
- **severity**: critical
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://sre.google/sre-book/addressing-cascading-failures/

### idempotent-retries
- **what**: Make retried mutations deduplicable with idempotency keys, request identities, or transactional outboxes and define key retention long enough for the retry window.
- **why**: Timeouts after a successful commit make clients retry, creating duplicate orders, payments, messages, or state transitions.
- **check**: probe
- **probe**: Submit the same mutating request twice with the same idempotency identity and once with a new identity, then assert exactly one effect for the replay and one additional effect for the new request.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9110.html, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### circuit-breaker
- **what**: Configure per-dependency circuit breakers with explicit closed, open, and half-open behavior, bounded recovery probes, and reset thresholds.
- **why**: Continuing to call a failing dependency consumes local resources and spreads its outage through the caller graph.
- **check**: probe
- **probe**: Inject consecutive dependency failures, assert the breaker opens and suppresses calls after its threshold, then restore the dependency and assert only bounded half-open probes precede closure.
- **applies_if**: all
- **severity**: important
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker, https://sre.google/sre-book/addressing-cascading-failures/

### bulkhead-isolation
- **what**: Isolate tenants, dependency pools, and workload classes with independent concurrency, connection, and queue limits.
- **why**: A slow or noisy tenant/dependency otherwise consumes shared workers and makes unrelated traffic fail.
- **check**: probe
- **probe**: Saturate one tenant or dependency pool with delayed work and assert an independent tenant or pool continues within its own latency and capacity limits.
- **applies_if**: all
- **severity**: important
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead, https://sre.google/sre-book/handling-overload/

### self-rate-limiting
- **what**: Enforce local outbound QPS and in-flight budgets per dependency and workload, even when upstream callers are within their quotas.
- **why**: A healthy caller can overwhelm a fragile dependency or itself through fan-out, causing a self-created cascade.
- **check**: probe
- **probe**: Inject a slow dependency and run concurrent outbound traffic, asserting the client stays below configured per-dependency QPS and in-flight limits while unrelated dependencies remain usable.
- **applies_if**: all
- **severity**: important
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/throttling, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### client-rate-limiting
- **what**: Enforce explicit per-client or per-tenant quotas with a global emergency limit and a machine-readable over-limit response.
- **why**: A single abusive or accidentally hot client can starve other clients and turn overload into an availability incident.
- **check**: probe
- **probe**: Send concurrent requests from several clients above their configured quotas and assert over-limit clients receive 429 with the documented retry signal while an in-quota client succeeds.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc6585.html, https://learn.microsoft.com/en-us/azure/architecture/patterns/throttling

### admission-control
- **what**: Bound server concurrency and waiting-room size, rejecting or canceling excess work before it consumes unbounded memory or worker time.
- **why**: Accepting unlimited concurrent work creates queue collapse, out-of-memory termination, and extreme tail latency.
- **check**: probe
- **probe**: Drive load above the configured in-flight and queue limits and assert memory and wait time stay bounded while excess requests receive a deterministic rejection or cancellation.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/

### priority-load-shedding
- **what**: Shed low-priority and optional work before critical traffic when saturation signals cross a defined threshold.
- **why**: Treating every request equally lets background or cosmetic work consume capacity needed for core user operations.
- **check**: probe
- **probe**: Run mixed critical and low-priority load until saturation and assert low-priority work is rejected or degraded first while critical traffic stays within its latency and error policy.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/

### backpressure
- **what**: Propagate bounded demand from consumers to producers and pause or slow intake when downstream capacity is exhausted.
- **why**: Ignoring downstream saturation turns a temporary slowdown into unbounded buffers, memory exhaustion, and lost work.
- **check**: probe
- **probe**: Attach a deliberately slow consumer or sink, then assert producer throughput falls or pauses and memory plus queue depth remain within configured bounds.
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling

### queue-load-leveling
- **what**: Use durable queues to absorb bursts, with explicit maximum depth or age, visibility timeout, retention, and consumer scaling policies.
- **why**: Synchronous burst handling overloads workers and dependencies even when average throughput is sufficient.
- **check**: probe
- **probe**: Parse queue and consumer configuration for bounded depth or age, visibility, retention, and scaling, then inject a burst and assert producers remain bounded and consumers drain it without violating message-loss policy.
- **applies_if**: data-pipeline
- **severity**: important
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling, https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers

### bounded-queue-overflow
- **what**: Make every in-memory and durable queue capacity and overflow action explicit—reject, shed, spill, or dead-letter—instead of allowing unbounded buffering.
- **why**: An unbounded queue converts overload into out-of-memory failure and hides the point at which work was lost or refused.
- **check**: probe
- **probe**: Parse queue capacities and overflow policies, fill each queue beyond capacity in a harness, and assert the documented action occurs without process termination or silent loss.
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling

### poison-message-dlq
- **what**: Cap delivery attempts for permanently failing messages, move them to a durable dead-letter queue with failure context, and provide controlled replay.
- **why**: Poison messages that are retried forever starve healthy work and can keep a queue permanently unhealthy.
- **check**: probe
- **probe**: Publish a deliberately invalid message and assert it reaches the DLQ after the configured attempt cap with reason and original identity preserved, then verify replay can be invoked without bypassing the cap.
- **applies_if**: data-pipeline
- **severity**: important
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers, https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling

### graceful-degradation
- **what**: Define and test stale-cache, partial-response, or feature-off fallbacks for optional dependencies while preserving correctness for critical paths.
- **why**: An optional recommendation, enrichment, or analytics outage should not become a total outage for the core operation.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: important
- **sources**: https://sre.google/sre-book/handling-overload/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

### fallback-correctness
- **what**: Ensure every fallback is bounded, explicitly labeled where users can observe staleness, and never presents an unknown or default value as authoritative.
- **why**: A graceful-looking but incorrect fallback can silently corrupt business decisions and be more damaging than an explicit error.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

### spof-inventory
- **what**: Inventory every critical-path component and operator dependency, then eliminate or explicitly accept each single point of failure with an owner and mitigation.
- **why**: Redundant application replicas do not help when one DNS zone, credential store, queue, control plane, or operator is the real SPOF.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### failure-domain-spread
- **what**: Spread capacity and stateful dependencies across independent hosts or zones with topology-aware routing, anti-affinity, and quorum-aware placement.
- **why**: A host or zone failure otherwise removes all replicas or a quorum at once.
- **check**: probe
- **probe**: Parse scheduling, topology-spread, anti-affinity, and service-routing configuration, then simulate or cordon one failure domain and assert capacity and quorum remain above policy.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

### regional-failover
- **what**: For region-sensitive services, define automated or rehearsed traffic cutover, replication mode, write-conflict or split-brain protection, and failback procedure.
- **why**: A regional outage can otherwise become prolonged downtime or divergent writes despite having a nominal second region.
- **check**: judgment
- **applies_if**: web-api
- **severity**: important
- **sources**: https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/plan-for-recovery.html

### rto-rpo
- **what**: Set service-specific RTO and RPO targets with business-owner sign-off, including degraded-mode behavior and dependency assumptions.
- **why**: An unowned target lets a technically successful recovery still violate acceptable downtime or data loss.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### backup-restore-drill
- **what**: Automate versioned backups and periodically restore them into an isolated environment while measuring integrity, actual RTO, and actual RPO.
- **why**: Unrestored, stale, or corrupt backups fail exactly when a disaster makes the primary unavailable.
- **check**: probe
- **probe**: Parse backup schedules, retention, and restore configuration, execute the latest isolated restore drill, and assert integrity plus measured recovery time and data loss meet the declared objectives.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### chaos-hypothesis-guardrails
- **what**: Run scoped fault-injection experiments with a stated steady-state hypothesis, bounded blast radius, abort conditions, and rollback.
- **why**: Uncontrolled experiments can cause the outage they are meant to prevent, while unmeasured experiments teach nothing actionable.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://principlesofchaos.org/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html

### partial-failure-chaos
- **what**: Exercise slow, refused, malformed, throttled, partitioned, resource-exhausted, clock-skewed, and process-kill faults rather than testing crashes alone.
- **why**: Distributed systems usually fail through hangs, partial responses, and resource starvation that a simple process-kill test does not expose.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://principlesofchaos.org/, https://sre.google/sre-book/addressing-cascading-failures/

### recovery-rehearsal
- **what**: Rehearse failover and restoration on a defined cadence and record detection, mitigation, recovery, and data-loss times with owned follow-up actions.
- **why**: A paper runbook and unpracticed operators routinely miss real RTO/RPO targets during incidents.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### dependency-failure-matrix
- **what**: Document and test each outbound dependency's behavior for timeout, refusal, throttling, malformed response, partial success, and stale data.
- **why**: An unmodeled failure mode becomes a hang, retry storm, silent corruption, or accidental all-or-nothing outage.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker

### lease-recovery-idempotence
- **what**: Make worker leases and heartbeats expire safely and resume processing idempotently after crashes, partitions, or consumer replacement.
- **why**: Abandoned leases create permanent backlog while duplicate delivery can lose or corrupt work without deduplication.
- **check**: probe
- **probe**: Kill workers at each processing phase and assert leases expire, items are eventually retried or deduplicated, and every item has exactly-once effect or a documented at-least-once outcome.
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### capacity-failure-headroom
- **what**: Validate peak capacity and failure headroom with load tests that include dependency latency, retry traffic, queue saturation, and loss of a failure domain.
- **why**: Average happy-path capacity hides tail amplification and leaves no margin when a replica, zone, or dependency is degraded.
- **check**: probe
- **probe**: Run the documented peak-load scenario with injected dependency delay and one capacity unit removed, then assert latency, error, queue, and recovery measures remain within declared limits.
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/sre-book/handling-overload/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html
