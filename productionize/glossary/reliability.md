# Reliability glossary

### health-contract
- **definition**: A health contract exposes separate, cheap liveness, readiness, and startup endpoints with documented status semantics and bounded latency. Liveness answers whether the process can make progress; readiness answers whether it may receive required traffic; startup answers whether initialization has completed.
- **implementation**:
  - Register `/livez`, `/readyz`, and `/startupz` (or documented equivalents) separately, with ownership and response semantics in service documentation.
  - Keep liveness process-local; evaluate required dependencies only in readiness and initialization progress only in startup.
  - Return a small machine-readable body, stable status codes, and strict handler timeouts; do not perform unbounded fan-out.
  - Configure orchestrator and load-balancer probes with explicit path, period, timeout, and failure thresholds.
- **probe**: Parse route registrations and deployment manifests for `/livez`, `/readyz`, and `/startupz` (or documented equivalents), curl each during normal operation and an injected dependency failure, and assert bounded latency plus the expected status transition.
- **failure_modes**: Prevents healthy instances being restarted because a dependency is down; prevents traffic being sent to an instance before initialization; prevents slow recursive health checks from amplifying an outage.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: health-check-contracts
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring

### liveness-process-only
- **definition**: A liveness contract checks only that the local process is alive and able to make progress, not that databases, queues, or other dependencies are healthy. Dependency and traffic eligibility belong to readiness so a shared outage does not trigger restart storms.
- **implementation**:
  - Implement liveness using an in-process progress signal, event-loop watchdog, or lightweight self-check rather than network calls to dependencies.
  - Set liveness failure thresholds to tolerate transient scheduling pauses while keeping the detection objective explicit.
  - Put required dependency checks in readiness and document which failures are intentionally excluded from liveness.
  - Alert separately on liveness failures, readiness failures, and dependency health to distinguish process death from capacity withdrawal.
- **probe**: The assessor must inspect the liveness handler and its call graph for dependency, DNS, storage, or broker calls; inspect probe configuration and failure thresholds; and verify the runbook explains why each dependency is readiness-only.
- **failure_modes**: Prevents every replica from restarting when a database is unavailable; prevents a dependency outage from erasing recovery capacity; prevents misleading liveness alerts from hiding the actual dependency failure.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: health-check-contracts
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://sre.google/sre-book/addressing-cascading-failures/

### readiness-dependency-gating
- **definition**: Readiness gates traffic on dependencies required to execute the service's critical path, while optional dependencies remain outside the gate. An unready instance stays alive for recovery but is excluded from routing.
- **implementation**:
  - Maintain an explicit required-versus-optional dependency list for each endpoint or workload role.
  - Use bounded, shallow readiness checks for required database, broker, configuration, or credential access.
  - Configure the orchestrator or load balancer to stop routing when readiness fails and to restore routing only after a success threshold.
  - Emit readiness transition metrics with dependency reason codes, without exposing secrets or high-cardinality payloads.
- **probe**: Start the service with a required database or broker unreachable, curl readiness and liveness, and assert readiness fails, liveness remains successful, and the orchestrator or load balancer excludes the instance.
- **failure_modes**: Prevents traffic reaching an instance that cannot complete its critical path; prevents optional analytics outages from removing all capacity; prevents a failed dependency from becoming user-visible as random per-request errors across one pool.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring

### startup-probe
- **definition**: A startup probe grants initialization a separate failure budget before liveness and readiness enforcement begins. It protects legitimate slow cold starts such as migrations, cache warming, or model loading from premature termination.
- **implementation**:
  - Configure an explicit startup path or command with period, timeout, and failure threshold derived from measured worst-case initialization.
  - Keep liveness and readiness disabled or gated until startup succeeds, as supported by the orchestrator.
  - Record initialization phase and duration so the startup budget can be revised from evidence.
  - Make initialization retry-safe and bounded; fail visibly when the startup budget is genuinely exceeded.
- **probe**: Parse workload manifests for a startup probe with explicit period, timeout, and failure threshold, run a deliberately slow initialization fixture, and assert it is not killed before the startup budget expires.
- **failure_modes**: Prevents cold-start loops during deploys; prevents a one-time migration or model load from looking like process death; prevents traffic admission before required initialization completes.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/

### health-check-budget
- **definition**: Health checks have a bounded execution cost and a dedicated request, CPU, and rate budget. Their design avoids dependency fan-out and recursive checks so probing cannot consume capacity needed for user traffic or recovery.
- **implementation**:
  - Keep liveness constant-time and make readiness checks bounded, cached where safe, and limited to required dependencies.
  - Apply separate ingress rate limits, concurrency limits, and resource reservations to probe traffic.
  - Reject or time out probe work deterministically; never let a health handler wait on an unbounded queue.
  - Track probe latency, volume, and resource use separately from business traffic.
- **probe**: The assessor must inspect health handler call graphs, dependency fan-out, timeout and caching behavior, probe ingress limits, and resource reservations; evidence must show probe volume cannot starve ordinary requests during a dependency incident.
- **failure_modes**: Prevents a probe storm from causing an outage; prevents recursive health checks from multiplying dependency load; prevents health traffic from starving recovery workers.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring, https://sre.google/sre-book/handling-overload/

### sigterm-shutdown
- **definition**: SIGTERM handling initiates an orderly shutdown: stop admitting new work, apply the policy for in-flight work, close resources, and exit within the termination grace period. The policy must distinguish safe completion, cancellation, and durable handoff rather than relying on process-kill behavior.
- **implementation**:
  - Install one idempotent signal handler that transitions admission to draining before closing listeners or clients.
  - Propagate cancellation to request handlers, workers, queues, and downstream calls, with an explicit completion deadline.
  - Configure termination grace, pre-stop or deregistration hooks, and forced-kill behavior with margin for cleanup.
  - Flush durable acknowledgements and telemetry within bounded time, then close database, broker, and HTTP resources.
- **probe**: Parse signal handlers and termination-grace configuration, launch with an in-flight request, send SIGTERM, and assert new work is refused, in-flight work follows the documented policy, and the process exits before the grace deadline.
- **failure_modes**: Prevents deploys from dropping requests; prevents workers from acknowledging unfinished jobs; prevents duplicate processing and resource corruption after eviction or node loss.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: graceful-lifecycle
- **sources**: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination, https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html

### connection-draining
- **definition**: Connection draining coordinates endpoint deregistration, keep-alive behavior, listener closure, and termination grace so existing connections can finish while new traffic stops. It treats load-balancer propagation delay as part of the shutdown budget.
- **implementation**:
  - Mark the instance unready before deregistering it and wait for routing state to propagate.
  - Set load-balancer deregistration delay, server keep-alive limits, and termination grace from measured request durations.
  - Stop accepting new connections while allowing bounded existing requests to complete or cancel cleanly.
  - Ensure clients receive retryable responses only for work that was not committed, with idempotency protection for replay.
- **probe**: Parse load-balancer deregistration and server keep-alive settings, hold a long-lived request and keep-alive connection across termination, and assert existing work completes or is canceled cleanly while new connections are not routed there.
- **failure_modes**: Prevents connection resets and partial responses during deploys; prevents retry bursts caused by killing keep-alive sockets; prevents new traffic arriving after shutdown has begun.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: graceful-lifecycle
- **sources**: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination, https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html

### cancellation-propagation
- **definition**: Cancellation propagates from the request, job lease, or shutdown signal through handlers, workers, and every downstream operation. Once useful work is no longer needed, all child work must stop or be explicitly handed off.
- **implementation**:
  - Pass a cancellation token or context through every service, repository, queue, and subprocess boundary.
  - Bind HTTP, RPC, database, DNS, and message operations to that token and a finite deadline.
  - Make worker pools remove canceled tasks and release permits, connections, and locks in cleanup paths.
  - Test cancellation at each fan-out branch, including background tasks spawned from request handlers.
- **probe**: The assessor must trace cancellation from ingress and shutdown through handler, worker, and downstream APIs; inspect whether every blocking operation accepts the context; and review tests or traces showing canceled work releases resources promptly.
- **failure_modes**: Prevents abandoned requests from consuming workers; prevents shutdown from hanging on orphaned downstream calls; prevents cancellation leaks from creating cascading overload.
- **severity**: important
- **applies_if**: all
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://sre.google/sre-book/addressing-cascading-failures/

### outbound-deadlines
- **definition**: Every outbound call has finite connect, handshake, operation, and total deadlines appropriate to its dependency and caller budget. This applies to HTTP, RPC, databases, queues, DNS, and subprocesses, including retries and connection acquisition.
- **implementation**:
  - Centralize client defaults but allow explicitly reviewed per-operation budgets for known long-running calls.
  - Set connection, TLS handshake, pool-acquisition, read/write, and total deadlines rather than only a socket timeout.
  - Propagate the caller's remaining deadline to downstream protocols and reject calls when no useful budget remains.
  - Instrument deadline expiry by dependency and operation, distinguishing connect, queue, and server time.
- **probe**: Enumerate outbound client constructors and call sites with a language-aware scanner, assert finite connect and operation deadlines, and run a blackhole or delayed-server fixture to verify calls return within the configured bound.
- **failure_modes**: Prevents hung dependencies from pinning all workers; prevents connection-pool exhaustion during partitions; prevents tail latency from becoming an indefinite request queue.
- **severity**: critical
- **applies_if**: all
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://grpc.io/docs/guides/deadlines/

### deadline-budget
- **definition**: A deadline budget is one end-to-end time limit propagated across service hops, with explicit reservations for queueing, downstream work, retries, and response handling. Per-hop limits must be shorter than the remaining caller budget rather than independent full-duration timers.
- **implementation**:
  - Accept and validate an inbound deadline, then pass remaining time in the native HTTP or RPC deadline mechanism.
  - Reserve fixed or policy-based time for serialization, retries, commit, and response delivery before starting downstream work.
  - Derive retry delays and attempt limits from remaining budget; stop when the next attempt cannot complete usefully.
  - Emit remaining-budget and deadline-exceeded metrics by route and dependency.
- **probe**: The assessor must inspect deadline propagation across each service boundary, retry calculations, queue and response reservations, and traces showing remaining time decreases monotonically rather than resetting at each hop.
- **failure_modes**: Prevents stacked timeouts from multiplying latency; prevents retries after the caller's useful deadline; prevents coordinated tail-latency and retry cascades.
- **severity**: critical
- **applies_if**: all
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://grpc.io/docs/guides/deadlines/

### retry-classification
- **definition**: Retry classification permits retries only for transient failures and operations that are idempotent or protected by an idempotency identity. Validation, authorization, malformed-request, and other permanent failures must return without retry.
- **implementation**:
  - Define a typed error matrix mapping transport status, dependency code, and operation safety to retry, fail-fast, or fallback.
  - Require idempotency keys, conditional writes, or transactional outboxes before retrying non-idempotent mutations.
  - Bound attempts by the caller deadline and avoid retrying after a response may have committed unless deduplication is guaranteed.
  - Log classification decisions with sanitized operation and dependency labels.
- **probe**: The assessor must inspect the error taxonomy and retry middleware for explicit permanent/transient classification, verify non-idempotent operations require deduplication, and review cases for timeout-after-commit handling.
- **failure_modes**: Prevents retry storms on invalid requests; prevents duplicate charges or writes; prevents authorization failures from wasting dependency and worker capacity.
- **severity**: critical
- **applies_if**: all
- **merges_into**: retry-backoff-breakers
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://learn.microsoft.com/en-us/azure/architecture/patterns/retry

### retry-exponential-jitter
- **definition**: Eligible retries use capped exponential backoff with randomized jitter and honor server retry hints when safe. The cap, jitter algorithm, and maximum attempts are explicit so clients do not synchronize during recovery.
- **implementation**:
  - Use a documented schedule such as full jitter over an exponentially increasing cap, with a maximum delay.
  - Parse and validate `Retry-After` or protocol-specific retry hints, never allowing them to exceed the caller deadline or local cap.
  - Generate jitter independently per request and avoid a shared deterministic retry clock.
  - Record attempt number, chosen delay, and final outcome without logging sensitive payloads.
- **probe**: Parse retry policies for exponential growth, a maximum delay, and jitter; run a failed-call harness with multiple clients; and assert bounded increasing delays with non-identical schedules.
- **failure_modes**: Prevents synchronized retry storms; prevents a recovering dependency from being overwhelmed by immediate repeats; prevents malicious or erroneous retry hints from causing unbounded waits.
- **severity**: critical
- **applies_if**: all
- **merges_into**: retry-backoff-breakers
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://learn.microsoft.com/en-us/azure/architecture/patterns/retry

### retry-budget
- **definition**: A retry budget limits attempts per request, elapsed retry time, and aggregate retry traffic to a configured fraction of normal traffic. It is enforced independently of individual client loops so fleet-wide failure traffic remains bounded.
- **implementation**:
  - Configure maximum attempts and total retry duration from the propagated deadline.
  - Maintain a token bucket or equivalent aggregate retry budget per service, dependency, and workload class.
  - Stop retries when the budget is exhausted and expose a deterministic fallback or error.
  - Alert on budget consumption and retry-to-primary traffic ratio before saturation.
- **probe**: Parse retry and deadline configuration, inject a dependency that fails for a fixed interval, and assert per-request attempts and aggregate retry calls remain below declared caps.
- **failure_modes**: Prevents a small dependency failure becoming fleet-wide overload; prevents retry loops from blocking recovery; prevents hidden retry traffic from exceeding capacity planning assumptions.
- **severity**: critical
- **applies_if**: all
- **merges_into**: retry-backoff-breakers
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://sre.google/sre-book/addressing-cascading-failures/

### idempotent-retries
- **definition**: Retried mutations use an idempotency key, request identity, conditional operation, or transactional outbox so replay produces one logical effect. The identity retention window covers the full client, queue, and recovery retry period.
- **implementation**:
  - Require a stable key at the API boundary and scope it to tenant, operation, and resource where appropriate.
  - Persist key, request fingerprint, status, and result atomically with the business effect; reject conflicting payload reuse.
  - Retain and replicate deduplication records longer than the maximum retry and reconciliation window.
  - Use an outbox or idempotent consumer for effects that cross a database and message boundary.
- **probe**: Submit the same mutating request twice with the same idempotency identity and once with a new identity, then assert exactly one effect for the replay and one additional effect for the new request.
- **failure_modes**: Prevents duplicate orders or charges after timeout-after-commit; prevents duplicate jobs after broker redelivery; prevents state transitions from being applied twice during failover.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: retry-backoff-breakers
- **sources**: https://www.rfc-editor.org/rfc/rfc9110.html, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### circuit-breaker
- **definition**: A circuit breaker stops calls to a failing dependency after an explicit threshold, then permits bounded half-open probes after a reset interval. Closed, open, and half-open transitions, failure classification, and recovery thresholds are observable and documented.
- **implementation**:
  - Configure breaker state per dependency and workload isolation domain, not as one global switch.
  - Count only eligible dependency failures and timeouts, with sliding-window thresholds and minimum volume.
  - In open state fail fast or use a correctness-preserving fallback; in half-open allow a small probe quota.
  - Emit state transitions, suppressed calls, and recovery outcomes, and cap reset/probe timing by caller policy.
- **probe**: Inject consecutive dependency failures, assert the breaker opens and suppresses calls after its threshold, restore the dependency, and assert only bounded half-open probes precede closure.
- **failure_modes**: Prevents a failing dependency from consuming all local resources; prevents caller cascades during an outage; prevents a thundering herd when recovery begins.
- **severity**: important
- **applies_if**: all
- **merges_into**: retry-backoff-breakers
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker, https://sre.google/sre-book/addressing-cascading-failures/

### bulkhead-isolation
- **definition**: Bulkheads isolate tenants, dependencies, and workload classes with independent concurrency, connection, queue, and resource limits. Saturation in one compartment must not consume the shared capacity needed by unrelated traffic.
- **implementation**:
  - Allocate separate worker pools or semaphores for critical, background, and tenant-specific work.
  - Set per-dependency connection-pool, in-flight, queue-depth, and memory limits with deterministic overflow behavior.
  - Reserve minimum capacity for critical traffic and prevent low-priority work from borrowing it without policy.
  - Monitor utilization and rejection by compartment, not only at the aggregate service level.
- **probe**: Saturate one tenant or dependency pool with delayed work and assert an independent tenant or pool continues within its own latency and capacity limits.
- **failure_modes**: Prevents a noisy tenant from starving others; prevents one slow dependency from exhausting all workers; prevents background jobs from taking down interactive traffic.
- **severity**: important
- **applies_if**: all
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead, https://sre.google/sre-book/handling-overload/

### self-rate-limiting
- **definition**: A service enforces local outbound QPS and in-flight limits per dependency and workload even when upstream callers are within quota. This controls fan-out and protects both the service and fragile dependencies from self-created overload.
- **implementation**:
  - Use per-dependency token buckets plus concurrency semaphores around every outbound client.
  - Reserve independent budgets for critical and optional calls and define rejection or fallback when tokens are unavailable.
  - Include queue wait in the caller deadline; do not let rate-limit queues grow without a bound.
  - Export allowed, delayed, rejected, and dependency-error counts by bounded labels.
- **probe**: Inject a slow dependency and run concurrent outbound traffic, asserting the client stays below configured per-dependency QPS and in-flight limits while unrelated dependencies remain usable.
- **failure_modes**: Prevents fan-out from overwhelming a dependency; prevents slow calls from creating an unbounded local queue; prevents one dependency's saturation from blocking other dependencies.
- **severity**: important
- **applies_if**: all
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/throttling, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### client-rate-limiting
- **definition**: Client or tenant rate limits enforce explicit quotas with a global emergency ceiling and a machine-readable over-limit response. Limits are applied before expensive work and communicate retry timing without exposing internal capacity details.
- **implementation**:
  - Define quota dimensions, windows, burst allowance, identity precedence, and emergency override in configuration.
  - Apply distributed counters or token buckets at the admission edge, with safe behavior when the limiter store is unavailable.
  - Return HTTP 429 with a stable error schema and validated `Retry-After` or reset metadata.
  - Separate quota accounting from sensitive identity payloads and monitor rejected versus allowed traffic.
- **probe**: Send concurrent requests from several clients above configured quotas and assert over-limit clients receive 429 with the documented retry signal while an in-quota client succeeds.
- **failure_modes**: Prevents one abusive client from starving others; prevents accidental hot loops from exhausting service capacity; prevents inconsistent clients from receiving ambiguous overload responses.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc6585.html, https://learn.microsoft.com/en-us/azure/architecture/patterns/throttling

### admission-control
- **definition**: Admission control bounds server concurrency and waiting-room size, rejecting or canceling excess work before it consumes unbounded memory or worker time. It makes overload behavior explicit instead of allowing queue collapse.
- **implementation**:
  - Place a bounded semaphore and queue at the earliest expensive boundary, with separate limits by priority or tenant.
  - Set queue wait deadlines and return a deterministic overload response when no slot is available.
  - Reserve capacity for health, control-plane, and critical operations.
  - Track in-flight, queued, rejected, canceled, and queue-wait distributions against capacity policy.
- **probe**: Drive load above configured in-flight and queue limits and assert memory and wait time stay bounded while excess requests receive deterministic rejection or cancellation.
- **failure_modes**: Prevents unbounded queues and out-of-memory termination; prevents extreme tail latency from accepted-but-never-served work; prevents overload from hiding as random downstream failures.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://sre.google/sre-book/handling-overload/, https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/

### priority-load-shedding
- **definition**: Load shedding rejects or degrades low-priority and optional work before critical traffic when saturation crosses a defined threshold. The threshold and response preserve the correctness and latency policy of core operations.
- **implementation**:
  - Classify requests and background jobs into explicit priority tiers at admission.
  - Configure saturation signals such as concurrency, queue age, memory, or dependency error rate with hysteresis.
  - Shed cosmetic, enrichment, and batch work first; provide bounded fallback for optional features.
  - Return machine-readable overload/degradation signals and alert when shedding persists.
- **probe**: Run mixed critical and low-priority load until saturation and assert low-priority work is rejected or degraded first while critical traffic remains within its latency and error policy.
- **failure_modes**: Prevents background processing from starving core requests; prevents queues from consuming all memory; prevents uniform rejection from turning a survivable overload into a total outage.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://sre.google/sre-book/handling-overload/, https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/

### backpressure
- **definition**: Backpressure propagates bounded demand from consumers to producers and pauses or slows intake when downstream capacity is exhausted. It keeps buffers, memory, and queue age within declared limits rather than hiding saturation.
- **implementation**:
  - Use bounded channels, consumer acknowledgements, flow-control windows, or pause/resume APIs at each pipeline boundary.
  - Couple producer concurrency to downstream permits and stop polling when the consumer or sink is saturated.
  - Set maximum buffer size, age, and overflow policy; persist or dead-letter work when it cannot remain in memory.
  - Expose producer throttling, queue depth, consumer lag, and memory pressure metrics.
- **probe**: Attach a deliberately slow consumer or sink, then assert producer throughput falls or pauses and memory plus queue depth remain within configured bounds.
- **failure_modes**: Prevents unbounded in-memory buffering; prevents slow sinks from causing out-of-memory failure; prevents silent loss when downstream capacity disappears.
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://sre.google/sre-book/handling-overload/, https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling

### queue-load-leveling
- **definition**: Durable queues absorb bursts between producers and consumers while making depth or age, visibility timeout, retention, and scaling policies explicit. The queue is a bounded shock absorber, not an excuse to accept work forever.
- **implementation**:
  - Configure durable delivery, maximum age or depth, visibility timeout, retention, and dead-letter behavior.
  - Scale consumers from queue age and work rate, with a cap that protects dependencies.
  - Keep producer acknowledgement semantics aligned with durable enqueue success.
  - Alert on oldest-message age, backlog growth, redelivery, and time-to-drain against policy.
- **probe**: Parse queue and consumer configuration for bounded depth or age, visibility, retention, and scaling; inject a burst; and assert producers remain bounded and consumers drain it without violating message-loss policy.
- **failure_modes**: Prevents synchronous bursts from overwhelming workers; prevents invisible backlog from violating freshness; prevents queue retention or visibility misconfiguration from causing loss and duplicate processing.
- **severity**: important
- **applies_if**: data-pipeline
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling, https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers

### bounded-queue-overflow
- **definition**: Every in-memory and durable queue declares capacity and an overflow action: reject, shed, spill, or dead-letter. No queue may grow without a limit or silently discard work.
- **implementation**:
  - Record maximum item count, bytes, age, and waiting time for each queue in configuration.
  - Choose overflow behavior by work criticality and make rejection visible to callers or operators.
  - Use durable spill or a dead-letter queue only with retention, replay, and ownership controls.
  - Alert before capacity is exhausted and expose overflow counts with bounded labels.
- **probe**: Parse queue capacities and overflow policies, fill each queue beyond capacity in a harness, and assert the documented action occurs without process termination or silent loss.
- **failure_modes**: Prevents unbounded queues from causing out-of-memory termination; prevents accepted work from disappearing silently; prevents saturation from being discovered only after recovery is impossible.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/handling-overload/, https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling

### poison-message-dlq
- **definition**: A poison-message policy caps delivery attempts for permanently failing messages, moves them to a durable dead-letter queue with failure context, and supports controlled replay. Replay must preserve identity and reapply the delivery cap.
- **implementation**:
  - Set a finite attempt count or age budget and classify non-retryable failures separately from transient ones.
  - Preserve original payload identity, headers, error reason, timestamps, and attempt history in the DLQ.
  - Restrict replay by authorization and provide filtering, quarantine, and audit records.
  - Revalidate and rate-limit replay so a bad batch cannot recreate the original outage.
- **probe**: Publish a deliberately invalid message and assert it reaches the DLQ after the configured attempt cap with reason and original identity preserved; verify replay can be invoked without bypassing the cap.
- **failure_modes**: Prevents one poison message from starving healthy work; prevents infinite retry cost; prevents replay from creating a second outage or losing diagnostic context.
- **severity**: important
- **applies_if**: data-pipeline
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers, https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling

### graceful-degradation
- **definition**: Graceful degradation defines bounded stale-cache, partial-response, or feature-off behavior for optional dependencies while preserving critical-path correctness. It is a product decision, not an accidental exception handler.
- **implementation**:
  - Catalog optional dependencies and specify fallback, maximum staleness, user labeling, and recovery behavior for each.
  - Gate fallbacks behind explicit feature or policy configuration and keep fallback work bounded.
  - Return partial results with stable fields and machine-readable degradation metadata where clients can act on it.
  - Measure fallback activation, staleness, and user-impact duration separately from hard failures.
- **probe**: Present the exact question: “When this optional dependency is unavailable, which behavior is acceptable?” Options: “serve bounded stale data,” “serve a labeled partial response,” “disable the feature,” or “fail the whole request.” Require an owner, maximum staleness, and correctness rationale for the selected option.
- **failure_modes**: Prevents recommendation or enrichment outages from taking down core operations; prevents accidental fail-open behavior; prevents indefinite stale data from becoming silently authoritative.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://sre.google/sre-book/handling-overload/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

### fallback-correctness
- **definition**: A fallback is bounded, explicitly labeled when stale or partial, and never presents an unknown or default value as authoritative. Its correctness envelope and exit conditions are reviewed like the primary path.
- **implementation**:
  - Define allowable stale age, missing fields, default values, and forbidden business decisions for every fallback.
  - Carry provenance and freshness metadata through APIs and UI where users could confuse fallback data with current data.
  - Fail closed for authorization, billing, inventory, and other correctness-critical decisions unless an approved invariant-preserving fallback exists.
  - Add alerts and automatic expiry so stale fallback mode cannot persist unnoticed.
- **probe**: The assessor must inspect fallback code paths, data provenance/freshness labels, business-critical fail-open decisions, maximum staleness enforcement, and tests or incident evidence showing fallback exit after dependency recovery.
- **failure_modes**: Prevents stale prices or inventory from causing incorrect transactions; prevents default authorization or entitlement values from granting access; prevents partial responses from being mistaken for complete truth.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://sre.google/sre-book/handling-overload/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

### spof-inventory
- **definition**: A SPOF inventory lists every critical-path component and operator dependency, including DNS, credentials, queues, control planes, and human access. Each item is eliminated, replicated, or explicitly accepted with an owner and mitigation.
- **implementation**:
  - Map request, data, control, and operational dependency graphs from user action through recovery.
  - Record failure domain, redundancy mode, dependency owner, recovery procedure, and residual risk for each node.
  - Include provider accounts, DNS zones, secret stores, CI/CD, observability, and break-glass access—not only runtime replicas.
  - Review the inventory after architecture changes and during recovery rehearsals.
- **probe**: The assessor must inspect the dependency graph and SPOF register for critical-path and operator dependencies, confirm each has an owner and mitigation or signed acceptance, and verify mitigations are exercised rather than merely documented.
- **failure_modes**: Prevents redundant application replicas from depending on one failed DNS zone; prevents an unavailable credential store from blocking all recovery; prevents control-plane or operator-access SPOFs from extending incidents.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### failure-domain-spread
- **definition**: Failure-domain spreading places capacity and stateful dependencies across independent hosts or zones with topology-aware routing, anti-affinity, and quorum-aware placement. A single host or zone loss must leave enough serving capacity and quorum to meet policy.
- **implementation**:
  - Declare topology-spread constraints, anti-affinity, replica counts, and disruption budgets for stateless and stateful workloads.
  - Place replicas across independent zones or hosts and verify the provider's actual fault boundaries.
  - Configure load balancing to avoid routing all traffic to one domain and preserve quorum requirements.
  - Re-evaluate placement after autoscaling, maintenance, and capacity changes.
- **probe**: Parse scheduling, topology-spread, anti-affinity, and service-routing configuration, simulate or cordon one failure domain, and assert capacity and quorum remain above policy.
- **failure_modes**: Prevents one host failure from removing every replica; prevents a zone outage from destroying a database quorum; prevents autoscaling from accidentally concentrating capacity in one domain.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

### regional-failover
- **definition**: Regional failover defines traffic cutover, replication mode, conflict or split-brain protection, and failback for region-sensitive services. It specifies which region may accept writes and how operators verify consistency before restoring normal routing.
- **implementation**:
  - Choose active-active, active-passive, or backup-region operation with explicit data replication and write authority.
  - Automate or rehearse DNS, global load-balancer, and credential/configuration cutover with health and quorum gates.
  - Fence the failed region or use a lease/epoch mechanism to prevent split-brain writes.
  - Define failback reconciliation, replication catch-up, rollback, and customer communication steps.
- **probe**: The assessor must inspect the regional topology, replication and write-authority configuration, fencing mechanism, traffic-cutover automation, and failback runbook; evidence must include a rehearsal showing no conflicting writers and measured recovery/data-loss times.
- **failure_modes**: Prevents a regional outage from becoming prolonged because cutover is manual and ambiguous; prevents divergent writes during active-active failure; prevents failback from overwriting newer recovered data.
- **severity**: important
- **applies_if**: web-api
- **merges_into**: backup-dr
- **sources**: https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/plan-for-recovery.html

### rto-rpo
- **definition**: Recovery Time Objective (RTO) is the maximum acceptable service restoration time; Recovery Point Objective (RPO) is the maximum acceptable data loss interval. Targets are service-specific, dependency-aware, and approved by a business owner, including degraded-mode behavior.
- **implementation**:
  - Record RTO, RPO, availability mode, degraded behavior, and assumptions for each critical service and data set.
  - Map targets to backup frequency, replication lag, failover time, restore throughput, and operator staffing.
  - Obtain explicit business-owner sign-off and version targets with architecture changes.
  - Measure actual RTO/RPO during drills and track gaps to owned remediation.
- **probe**: Present the exact question: “For this service and data set, what maximum downtime and data loss are acceptable?” Options: “RTO ≤15 minutes / RPO ≤1 minute,” “RTO ≤1 hour / RPO ≤15 minutes,” “RTO ≤4 hours / RPO ≤1 hour,” or “custom targets.” Require degraded-mode behavior, dependency assumptions, and business-owner approval.
- **failure_modes**: Prevents technically successful recovery that exceeds acceptable downtime; prevents backups that cannot meet data-loss requirements; prevents teams from discovering during an incident that no one owns the target.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### backup-restore-drill
- **definition**: Backup and restore drills verify that versioned backups are complete, usable, and recoverable in an isolated environment. Each drill measures integrity, actual recovery time, and actual recovery point against declared objectives.
- **implementation**:
  - Configure encrypted, versioned backups with retention, cross-account or cross-region protection, and access controls.
  - Automate selection of a recent backup, isolated restore, schema/application compatibility checks, and integrity validation.
  - Measure restore start-to-service time, recovered timestamp, data loss, and operator actions.
  - Alert on missed schedules, restore failures, backup age, and RTO/RPO violations; retain drill evidence.
- **probe**: Parse backup schedules, retention, and restore configuration, execute the latest isolated restore drill, and assert integrity plus measured recovery time and data loss meet declared objectives.
- **failure_modes**: Prevents corrupt or incomplete backups being trusted during disaster; prevents restore procedures from exceeding RTO; prevents stale backups from silently violating RPO.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### chaos-hypothesis-guardrails
- **definition**: A chaos experiment states a steady-state hypothesis, limits its blast radius, defines abort conditions, and provides rollback before injecting faults. Experiments produce measured evidence rather than uncontrolled disruption.
- **implementation**:
  - Write hypothesis, target scope, expected signal, duration, owner, approvals, and customer-impact budget in the experiment plan.
  - Use staged environments or narrow production cohorts with denylisted critical resources and automatic stop conditions.
  - Define rollback, cleanup, communication, and incident escalation before starting the fault.
  - Capture baseline, fault, recovery, and follow-up action evidence with timestamps.
- **probe**: The assessor must inspect experiment plans for a falsifiable steady-state hypothesis, scoped target, blast-radius limit, abort thresholds, rollback steps, approval, and recorded outcome; reject experiments that only state “kill something” without measurable expectations.
- **failure_modes**: Prevents fault injection from causing an uncontrolled outage; prevents experiments that cannot distinguish resilience from luck; prevents repeated findings from lacking owners or remediation.
- **severity**: important
- **applies_if**: all
- **sources**: https://principlesofchaos.org/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html

### partial-failure-chaos
- **definition**: Partial-failure chaos exercises slow, refused, malformed, throttled, partitioned, resource-exhausted, clock-skewed, and process-kill conditions. It validates behavior under degraded dependencies, not only complete process crashes.
- **implementation**:
  - Maintain a fault matrix for each dependency and workload path, including latency, status, malformed payload, partition, quota, and clock faults.
  - Inject one bounded fault at a time before composing faults, with steady-state and abort metrics.
  - Verify timeout, retry, breaker, fallback, queue, cancellation, and recovery behavior for each fault class.
  - Store experiment results and convert uncovered or failed hypotheses into owned changes.
- **probe**: The assessor must inspect the fault matrix and experiment records for slow, refusal, malformed, throttled, partition, resource, clock, and kill scenarios; verify each has expected behavior and measured recovery evidence.
- **failure_modes**: Prevents hangs hidden by crash-only tests; prevents malformed partial responses from causing silent corruption; prevents resource starvation and clock faults from bypassing normal resilience controls.
- **severity**: important
- **applies_if**: all
- **sources**: https://principlesofchaos.org/, https://sre.google/sre-book/addressing-cascading-failures/

### recovery-rehearsal
- **definition**: Recovery rehearsals repeatedly exercise failover and restoration on a defined cadence and record detection, mitigation, recovery, and data-loss times. Findings become owned, dated follow-up actions rather than remaining in a paper runbook.
- **implementation**:
  - Schedule rehearsals for application, dependency, regional, and backup restore scenarios according to service criticality.
  - Use production-like data shape and access controls in an isolated or approved environment; protect customer data during the exercise.
  - Record timeline, operator decisions, actual RTO/RPO, integrity checks, and communications.
  - Track remediation to closure and repeat the scenario after material architecture or runbook changes.
- **probe**: The assessor must inspect rehearsal cadence, scope, runbooks, participant ownership, measured detection/mitigation/recovery/data-loss times, and closed follow-up actions; require evidence of a recent completed exercise.
- **failure_modes**: Prevents unpracticed operators from missing recovery targets; prevents stale runbooks from failing during an incident; prevents recurring drill findings from remaining unresolved.
- **severity**: important
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html, https://csrc.nist.gov/publications/detail/sp/800-34/rev-1/final

### dependency-failure-matrix
- **definition**: A dependency-failure matrix documents and tests each outbound dependency's behavior for timeout, refusal, throttling, malformed response, partial success, and stale data. It maps each condition to a bounded, correctness-preserving service response.
- **implementation**:
  - Maintain one matrix row per dependency and operation with fault, detection signal, timeout, retry, breaker, fallback, and user-visible result.
  - Include protocol-specific status codes, partial response schemas, stale-data limits, and side-effect safety.
  - Link matrix rows to fault fixtures, dashboards, alerts, and runbooks.
  - Review the matrix whenever a dependency, client library, or critical-path operation changes.
- **probe**: The assessor must inspect the matrix for every outbound dependency and operation, confirm all listed fault classes have explicit timeout/retry/fallback/correctness behavior, and review evidence that representative fixtures or drills exercise the rows.
- **failure_modes**: Prevents an unmodeled dependency hang from exhausting workers; prevents malformed responses from causing silent corruption; prevents partial failure from becoming accidental all-or-nothing outage.
- **severity**: important
- **applies_if**: all
- **sources**: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/, https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker

### lease-recovery-idempotence
- **definition**: Worker leases and heartbeats expire safely, and processing resumes idempotently after crashes, partitions, or consumer replacement. The design explicitly chooses exactly-once effect or documents and contains an at-least-once outcome.
- **implementation**:
  - Set lease duration, heartbeat cadence, renewal failure behavior, and maximum processing time with clock-skew margin.
  - Use fencing tokens or epochs so an expired worker cannot commit after ownership changes.
  - Make handlers deduplicable with operation identity, transactional state transitions, or an outbox.
  - Acknowledge only after durable effect and expose expired leases, redeliveries, duplicates, and stuck work.
- **probe**: Kill workers at each processing phase and assert leases expire, items are eventually retried or deduplicated, and every item has exactly-once effect or a documented at-least-once outcome.
- **failure_modes**: Prevents abandoned leases from creating permanent backlog; prevents a late worker from overwriting a replacement's result; prevents redelivery from duplicating or corrupting work.
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://learn.microsoft.com/en-us/azure/architecture/patterns/competing-consumers, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### capacity-failure-headroom
- **definition**: Capacity and failure-headroom validation measures peak demand while injecting dependency latency, retry traffic, queue saturation, and loss of a failure domain. The service must retain enough margin to meet latency, error, queue, and recovery policies under the combined scenario.
- **implementation**:
  - Define peak workload shape, concurrency, dependency mix, retry policy, queue limits, and minimum surviving capacity.
  - Load test with one replica, host, zone, or equivalent capacity unit removed and representative dependency delay.
  - Measure tail latency, errors, resource saturation, retry ratio, queue age, and time to recover after fault removal.
  - Set admission, autoscaling, and alert thresholds from observed headroom rather than average throughput.
- **probe**: Run the documented peak-load scenario with injected dependency delay and one capacity unit removed, then assert latency, error, queue, and recovery measures remain within declared limits.
- **failure_modes**: Prevents average-capacity estimates from hiding retry amplification; prevents one lost zone from causing overload; prevents queue and recovery collapse when a dependency is slow.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/sre-book/handling-overload/, https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-reliability.html
