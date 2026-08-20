# Scalability & performance glossary

### stateless-request-handling
- **definition**: Request handling is stateless when any replica can serve any request without relying on replica-local sessions, uploads, or durable work. Mutable state is persisted in shared, durable services and request context carries only bounded, non-authoritative data.
- **implementation**:
  - Store sessions in a shared session store or use signed, bounded tokens with explicit revocation semantics.
  - Put uploads, exports, and generated artifacts in object storage; pass references rather than local paths between requests.
  - Persist background jobs and workflow state in a database or durable queue before acknowledging the request.
  - Keep caches disposable and document which shared service owns each durable state transition.
- **probe**: The assessor must inspect handlers for process globals, in-memory session maps, local upload paths, and fire-and-forget durable work; route two sequential requests to different replicas and verify session, upload, and job state remain available after restarting the first replica.
- **failure_modes**: Prevents a user being logged out or losing a cart after load-balancer rebalance; prevents an export disappearing when its worker is rescheduled; prevents acknowledged work vanishing with a crashed process.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/processes, https://12factor.net/backing-services

### replica-safe-coordination
- **definition**: Replica-safe coordination puts singleton ownership and mutual exclusion in a shared mechanism that survives process restarts and arbitrates concurrent replicas. Leases, fencing, and expiration make ownership recoverable rather than trusting an in-memory boolean.
- **implementation**:
  - Use a database advisory lock, transactional lease, or broker coordination primitive with owner identity, TTL, and renewal.
  - Fence stale workers with monotonically increasing lease epochs before allowing side effects.
  - Persist scheduled-job ownership and configure recovery when a lease holder dies.
  - Emit acquisition, renewal, expiry, contention, and duplicate-execution metrics.
- **probe**: The assessor must inspect singleton jobs and lock call graphs for process-local flags; run two replicas against the same schedule and kill the lease holder, verifying one owner at a time and takeover after expiry without concurrent side effects.
- **failure_modes**: Prevents every replica sending the same digest; prevents two workers applying a non-transactional settlement concurrently; prevents a dead leader silently stopping scheduled processing.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/processes, https://kubernetes.io/docs/concepts/cluster-administration/cluster-intro/

### ephemeral-local-disk
- **definition**: Local disk is limited to disposable scratch data whose loss is acceptable on restart or rescheduling. Durable uploads, exports, and artifacts are written to shared storage with ownership, retention, and access controls.
- **implementation**:
  - Define and enforce an allowlisted temporary directory for bounded intermediate files.
  - Configure object or network storage for uploads, generated files, and build artifacts, returning durable object identifiers.
  - Set size limits, cleanup jobs, and encryption/access policy for both scratch and shared storage.
  - Review container manifests and volume mounts so durable paths cannot silently resolve to ephemeral layers.
- **probe**: Scan application code and deployment manifests for writes outside the allowlisted temporary directory, then fail if upload, export, or artifact paths target replica-local storage; restart a replica mid-transfer and verify the shared object remains retrievable.
- **failure_modes**: Prevents downloads returning 404 after a pod move; prevents disk exhaustion from abandoned local exports; prevents a rollout from deleting the only copy of an uploaded document.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/backing-services, https://kubernetes.io/docs/concepts/storage/volumes/

### graceful-drain-on-scale-down
- **definition**: Scale-down drain first removes a replica from readiness, then stops admission of new work, honors SIGTERM, and completes or safely cancels in-flight work within a bounded grace period. Deregistration propagation and keep-alive behavior are part of that shutdown budget.
- **implementation**:
  - Transition to draining before closing listeners and fail readiness while the process remains alive.
  - Configure pre-stop/deregistration delay, keep-alive limits, termination grace, and forced-kill margin from measured request duration.
  - Propagate cancellation and use durable handoff or idempotent replay for work that cannot finish.
  - Flush acknowledgements and telemetry, then close pools and queues in a deterministic order.
- **probe**: Parse lifecycle and readiness settings, send SIGTERM to a running replica under active requests, and assert readiness fails before exit, new requests stop, and existing requests complete or are safely canceled without dropped or duplicated completions.
- **failure_modes**: Prevents a scale-down from resetting uploads or streaming responses; prevents clients retrying a mutation after a lost in-flight response and creating duplicate work; prevents a worker dying after acknowledging a job.
- **severity**: critical
- **applies_if**: all
- **merges_into**: graceful-lifecycle
- **sources**: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-flow

### retry-safe-mutations
- **definition**: A retry-safe mutation binds retries to an idempotency key or durable deduplication record whose scope, request fingerprint, and retention cover every retry path. Replays return the original result (or a documented equivalent) without repeating the committed side effect.
- **implementation**:
  - Require a key for externally retriable creates and scope it to tenant, operation, and endpoint.
  - Enforce a unique database constraint and atomically record request fingerprint, status, result, and expiry with the side effect.
  - Reject reuse with a different payload rather than silently applying a second interpretation.
  - Retain records longer than client, proxy, queue, and manual retry windows and redact keys from logs.
- **probe**: Submit the same mutation concurrently and after a forced response timeout with one idempotency key, then assert one durable side effect and equivalent replay responses; submit the same key with a changed payload and assert a deterministic conflict.
- **failure_modes**: Prevents a payment being charged twice after a gateway timeout; prevents a deploy or job request being enqueued twice after a client retry; prevents duplicate state transitions after connection reset.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: idempotency-keys
- **sources**: https://docs.stripe.com/api/idempotent_requests

### connection-pool-budget
- **definition**: A connection-pool budget reserves database connections across the maximum replica count and all non-application consumers. Pool limits must remain below the database limit after migrations, operators, failover, and safety reserve are included.
- **implementation**:
  - Calculate `replica_max * pool_max + migration + admin + failover_reserve <= db_max_connections`.
  - Set pool maximums, minimums, and startup ramp explicitly per workload role.
  - Account for sidecars, poolers, read replicas, and maintenance jobs in the same capacity model.
  - Alert on configured capacity and live connection utilization before saturation.
- **probe**: Parse deployment replica maximum, per-process pool maximum, pooler limits, and database `max_connections`, then assert the inequality including documented reserve; exercise max replicas and verify no connection refusals.
- **failure_modes**: Prevents a horizontal rollout exhausting PostgreSQL before new pods can serve; prevents migration connections starving customer traffic; prevents failover leaving no connection reserve for recovery.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/runtime-config-connection.html, https://www.pgbouncer.org/config.html

### pool-timeouts-and-leak-detection
- **definition**: Pool safety uses finite acquisition, connect, idle, lifetime, and query deadlines plus telemetry that distinguishes waiting, in-use, idle, and leaked checkouts. A dependency stall must release capacity rather than pinning every worker indefinitely.
- **implementation**:
  - Set acquisition and connect timeouts shorter than the caller's end-to-end deadline.
  - Set idle and maximum lifetimes with jitter to avoid synchronized reconnects.
  - Use `defer`/`finally` cleanup for every checkout and expose pool wait, timeout, utilization, and long-hold metrics.
  - Alert on checkout age and correlate pool starvation with dependency latency.
- **probe**: Parse timeout settings and run a dependency-stall test, asserting bounded request time, pool-wait telemetry, checkout release after cancellation, and recovery when the dependency returns.
- **failure_modes**: Prevents a dead database causing all HTTP workers to hang; prevents one leaked transaction exhausting a pool; prevents synchronized connection expiry causing a thundering reconnect storm.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.pgbouncer.org/config.html, https://sre.google/sre-book/handling-overload/

### connection-pooler-boundary
- **definition**: A pooler boundary multiplexes many application clients onto a controlled set of database backends when replica fan-out would otherwise exceed limits. Transaction pooling is safe only when the application does not depend on session state surviving between transactions.
- **implementation**:
  - Document whether each workload uses session, transaction, or statement pooling and why.
  - Configure pooler client/server caps, reserve, authentication, TLS, and queue timeouts.
  - Audit prepared statements, temporary tables, session variables, advisory locks, and LISTEN/NOTIFY before transaction pooling.
  - Route session-state-dependent workloads to session pools or remove that dependency explicitly.
- **probe**: The assessor must inspect pooler mode and application session-state usage, then run representative transactions through failover and concurrent replicas, verifying prepared statements, tenant context, locks, and notifications retain their documented semantics.
- **failure_modes**: Prevents database backend exhaustion as replicas scale; prevents one tenant's session setting leaking into another transaction; prevents prepared statements or advisory locks failing only in production behind a pooler.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.pgbouncer.org/config.html, https://www.postgresql.org/docs/current/runtime-config-connection.html

### access-path-indexes
- **definition**: Access-path indexes are selected from measured production query predicates, joins, and ordering rather than column naming conventions. They provide selective paths for high-volume shapes while preserving acceptable write and storage cost.
- **implementation**:
  - Inventory normalized query shapes and rank them by traffic, latency, and scanned rows.
  - Add indexes matching actual `WHERE`, `JOIN`, and `ORDER BY` predicates, including partial or covering indexes where justified.
  - Validate selectivity and plan behavior with production-shaped cardinalities and statistics.
  - Record ownership, expected query coverage, and rollback/drop criteria for every index.
- **probe**: Run representative queries with `EXPLAIN (FORMAT JSON)` against production-shaped cardinalities and fail when a required selective access path is absent or estimated work exceeds its budget.
- **failure_modes**: Prevents a growing orders table turning a lookup into a full scan; prevents a join storm during a popular report; prevents an index chosen by convention from failing to support the actual sort.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/indexes.html, https://www.postgresql.org/docs/current/using-explain.html

### composite-index-order
- **definition**: Composite-index order places equality predicates first, then useful range and ordering columns, with a unique tie-breaker when pagination needs stable boundaries. The order is validated against real query shapes because a present index can still be unusable or expensive.
- **implementation**:
  - Derive the leftmost equality prefix from the highest-volume predicates.
  - Follow it with the range or sort columns required by the access path.
  - Add a unique, immutable tie-breaker for deterministic cursor pagination.
  - Check collation, null ordering, partial predicates, and direction against the query contract.
- **probe**: Execute each high-volume query shape with `EXPLAIN` and assert the plan uses the intended equality prefix, range condition, and ordering without a large residual sort or scan.
- **failure_modes**: Prevents an index on `(created_at, tenant_id)` scanning every tenant for a tenant query; prevents deep pages sorting huge result sets; prevents duplicate or missing rows at equal timestamps.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/indexes-multicolumn.html, https://www.postgresql.org/docs/current/using-explain.html

### index-write-amplification
- **definition**: Index-write review measures read benefit against extra insert/update work, storage, cache pressure, replication, and backup volume. Unused or redundant indexes are removed only after confirming that uncommon but important query coverage is not lost.
- **implementation**:
  - Track index scans, tuples read, size, write latency, and maintenance cost over a representative window.
  - Identify duplicate prefixes and unused indexes while excluding recently created or seasonal ones.
  - Use staged removal or `CONCURRENTLY` operations with a rollback plan.
  - Recheck query plans and write throughput after every removal.
- **probe**: The assessor must inspect index-usage history, write amplification, storage/replication cost, and query coverage evidence; verify proposed drops account for low-frequency critical paths and have a rollback window.
- **failure_modes**: Prevents write throughput collapsing under a pile of speculative indexes; prevents replicas and backups lagging from needless index pages; prevents dropping the only index serving a rare compliance export.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/indexes.html, https://www.postgresql.org/docs/current/monitoring-stats.html

### plan-regression-gates
- **definition**: A plan-regression gate versions representative query plans and compares estimated rows, actual rows, buffers, and duration across schema, statistics, and engine changes. It fails before release when a critical query changes to materially more expensive work.
- **implementation**:
  - Keep production-shaped fixtures and normalized `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` baselines.
  - Compare plan nodes, scan types, row estimates, shared buffers, temp spills, and latency with tolerances.
  - Refresh statistics in the fixture and test both warm and cold cache where relevant.
  - Require an owner-approved exception with measured capacity impact for intentional regressions.
- **probe**: Run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on fixed production-shaped fixtures before and after a change and fail on configured plan-node, row-estimate, buffer, or latency regressions.
- **failure_modes**: Prevents a statistics change turning an indexed lookup into a sequential scan; prevents a schema migration introducing disk sorts; prevents a planner upgrade silently increasing database cost per request.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/using-explain.html

### n-plus-one-query-budget
- **definition**: An N+1 query budget limits database round trips per request independently of returned collection size. Instrumentation makes query count and database spans visible so related records are fetched in bounded batches or joins.
- **implementation**:
  - Count database operations per request and tag endpoint and query shape without high-cardinality user data.
  - Use eager loading, joins, batch loaders, or bounded set-based queries for relationships.
  - Set endpoint-specific query-count and database-time budgets in CI or staging.
  - Keep pagination and relationship limits explicit so one page cannot expand fan-out.
- **probe**: Exercise each collection endpoint with fixtures containing 1, 10, and 100 related records and fail if database-query count grows linearly with collection size.
- **failure_modes**: Prevents a feed of 100 items issuing 101 queries; prevents tenant size turning an admin endpoint into database saturation; prevents latency spikes hidden by an otherwise fast single-item test.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://opentelemetry.io/docs/specs/semconv/database/, https://www.postgresql.org/docs/current/using-explain.html

### endpoint-latency-and-query-budget
- **definition**: A per-endpoint budget assigns p95 and p99 end-to-end latency limits and allocates portions to application, database, and downstream dependencies. The budget is an explicit service decision tied to traffic, user experience, and an SLO rather than an informal average.
- **implementation**:
  - Define budgets by critical route, operation class, payload shape, and percentile with an owner and review date.
  - Set child budgets for database time, dependency time, serialization, and queueing that sum with margin below the end-to-end target.
  - Instrument traces and histograms with route templates, status, and dependency attribution.
  - Gate releases and capacity changes on budget compliance, including tail and error behavior.
- **probe**: Ask: “Which endpoint classes must meet which p95/p99 end-to-end, database, and dependency budgets over what measurement window?” Present options: (A) document route-specific values and owners now, (B) adopt a proposed baseline and review after one traffic window, or (C) explicitly defer the SLO with a named risk owner and deadline.
- **failure_modes**: Prevents average latency hiding a p99 timeout wave; prevents a database optimization consuming all dependency budget; prevents shipping a faster median that overloads a downstream service at the tail.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: slo-framework
- **sources**: https://sre.google/sre-book/service-level-objectives/, https://aws.amazon.com/builders-library/latency-aware-load-balancing/

### bounded-query-work
- **definition**: Bounded query work caps rows returned, sort and join effort, execution time, and memory for every externally influenced query. Bounds apply at the API, ORM, database statement, and resource-governance layers so one request cannot monopolize shared capacity.
- **implementation**:
  - Enforce server-side limits, maximum filters, timeouts, and selected fields at the API boundary.
  - Use indexed predicates, bounded joins, statement timeouts, and memory/work policies appropriate to the database.
  - Reject or asynchronously process exports and scans that exceed synchronous limits.
  - Monitor rows scanned/returned, temp spills, query duration, and cancellation counts.
- **probe**: Lint query construction for missing limits or timeouts, then run worst-case cardinality fixtures with `EXPLAIN (ANALYZE, BUFFERS)` and assert bounded rows, memory, and duration.
- **failure_modes**: Prevents a broad search consuming all database memory; prevents an accidental export query blocking checkout traffic; prevents malicious filters turning a cheap endpoint into a full-table scan.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/queries-limit.html, https://www.postgresql.org/docs/current/using-explain.html

### keyset-pagination
- **definition**: Keyset pagination uses an opaque cursor containing a stable, unique, indexed ordering position instead of asking the database to skip an ever-growing OFFSET. It provides predictable work and explicit behavior when rows are inserted or deleted between pages.
- **implementation**:
  - Choose an immutable ordering such as `(created_at, id)` and index it in cursor order.
  - Encode and authenticate or sign cursor contents; reject malformed, expired, or cross-tenant cursors.
  - Query with a strict tuple range and fetch one extra row to produce `next_cursor`.
  - Document snapshot consistency and duplicate/missing-row behavior under concurrent writes.
- **probe**: Request a deep page while capturing the query plan and assert it uses a cursor range predicate on the ordering index, not a growing OFFSET scan; tamper with the cursor and assert a documented error.
- **failure_modes**: Prevents page 10,000 timing out after scanning and discarding millions of rows; prevents offset pages duplicating or skipping rows during concurrent inserts; prevents clients forging cursors across tenants.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://cloud.google.com/apis/design/design_patterns#list_pagination, https://www.postgresql.org/docs/current/queries-limit.html

### bounded-page-contract
- **definition**: A bounded page contract defines server-enforced size limits, deterministic tie-broken ordering, allowed fields, and a valid next-cursor representation. Invalid or excessive requests fail predictably rather than allocating unbounded query and response work.
- **implementation**:
  - Clamp or reject `page_size` outside documented minimum and maximum values.
  - Select a fixed field set and reject unauthorized or unbounded expansions.
  - Use a stable unique ordering and return an opaque cursor plus explicit end-of-list behavior.
  - Apply the same bounds to internal callers and log rejected requests with safe reason codes.
- **probe**: Call the list endpoint with zero, negative, maximum-plus-one, and very large page sizes and assert bounded responses, deterministic ordering, and a valid next-cursor or documented error.
- **failure_modes**: Prevents `page_size=1000000` causing memory and database spikes; prevents equal-sort-key pages from missing records; prevents clients depending on an undocumented response shape that changes under load.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cloud.google.com/apis/design/design_patterns#list_pagination

### cache-control-contract
- **definition**: Cache-control explicitly classifies responses as public, private, or non-cacheable and assigns deliberate freshness and revalidation semantics. The policy prevents shared caches from storing sensitive data while reducing origin work for safe responses.
- **implementation**:
  - Set `Cache-Control` on success, mutation, authentication, and error responses rather than relying on defaults.
  - Use `private` or `no-store` for user-specific or sensitive responses and `public` only for safe shared representations.
  - Define `max-age`, `s-maxage`, stale behavior, and purge/versioning ownership per resource class.
  - Test CDN and browser behavior separately, including authorization and cookie presence.
- **probe**: Fetch representative public, authenticated, mutable, and error responses with `curl -I` and assert expected `Cache-Control`, freshness, and `no-store` or `private` directives.
- **failure_modes**: Prevents one user's response being served from a shared cache to another; prevents an origin overload caused by every asset being uncacheable; prevents stale error pages persisting longer than intended.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9111.html, https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control

### validators-and-revalidation
- **definition**: Validators let a client or cache prove that its representation is still current without downloading the body again. Strong or appropriately scoped ETags or Last-Modified values produce a correct 304 response when the representation is unchanged.
- **implementation**:
  - Generate ETags from the actual representation or use a safe version token with documented strength.
  - Handle `If-None-Match` precedence and `If-Modified-Since` according to HTTP semantics.
  - Return 304 without a body while preserving relevant cache headers and validator values.
  - Change validators whenever representation, authorization scope, or tenant-visible content changes.
- **probe**: Fetch a cacheable response, replay it with `If-None-Match` or `If-Modified-Since`, and assert a correct 304 with no response body and unchanged validator semantics; alter the resource and assert a full response with a new validator.
- **failure_modes**: Prevents clients redownloading unchanged large feeds; prevents a weak validator serving stale content after a mutation; prevents malformed 304 bodies confusing intermediaries.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9111.html, https://www.rfc-editor.org/rfc/rfc9110.html

### cache-key-variation
- **definition**: Cache-key variation includes every dimension that changes a representation, including encoding, locale, tenant, authorization, and content negotiation. Shared caches either vary correctly or are explicitly disabled for responses that cannot be safely shared.
- **implementation**:
  - Declare required `Vary` dimensions and configure equivalent CDN/cache key components.
  - Keep tenant and authorization context isolated; never use an untrusted header as an unbounded key without validation.
  - Include representation format and compression dimensions, or normalize them at the cache boundary.
  - Bound key cardinality and monitor hit rate by key class and variant.
- **probe**: Request the same URL across each supported encoding, locale, tenant, and authentication state and assert distinct authorized bodies or an explicit non-shared-cache policy.
- **failure_modes**: Prevents private tenant data leaking through a public cache key; prevents a gzip body being sent to a client that cannot decode it; prevents locale negotiation returning the wrong language.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9111.html

### immutable-versioned-assets
- **definition**: Immutable asset delivery gives each deployable static asset a content-derived filename and long-lived cache lifetime, while the entry manifest remains short-lived and revalidatable. A new asset URL is used whenever bytes change.
- **implementation**:
  - Emit content hashes in JavaScript, CSS, font, and image filenames and reference them from a manifest.
  - Serve hashed assets with long `max-age` and `immutable`; keep HTML/manifest TTL short enough for rollout.
  - Upload assets atomically and retain old versions through the rollback window.
  - Fail builds when a mutable asset bypasses fingerprinting or exceeds route budgets.
- **probe**: Parse the build manifest for content hashes and fetch hashed and unhashed assets, asserting hashed responses have a long `max-age` with `immutable` and the manifest has a short revalidation policy.
- **failure_modes**: Prevents old JavaScript loading against a new API after deploy; prevents cache purges for every release; prevents rollback failing because the prior asset was deleted immediately.
- **severity**: important
- **applies_if**: spa
- **sources**: https://web.dev/articles/performance-budgets, https://www.rfc-editor.org/rfc/rfc9111.html

### app-cache-stampede-control
- **definition**: Stampede control coordinates concurrent cache misses so one request refreshes a hot key while bounded waiters reuse its result or a safe stale value. TTL jitter, leases, and negative caching prevent synchronized expiry from turning into an origin flood.
- **implementation**:
  - Use single-flight per key or a distributed lease with owner expiry and fencing.
  - Add randomized TTL jitter and bounded stale-while-revalidate behavior where correctness permits.
  - Cache safe negative results briefly and distinguish absent, error, and stale states.
  - Bound waiter count and refresh time; define fallback behavior when the origin fails.
- **probe**: Expire one hot key and issue concurrent misses while counting origin calls, then assert one refresh, bounded waiters, and explicit stale or error behavior on refresh failure.
- **failure_modes**: Prevents a synchronized midnight expiry overwhelming the database; prevents a failed refresh making every request wait indefinitely; prevents a hot nonexistent key becoming a repeated expensive lookup.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Strategies.html, https://sre.google/sre-book/handling-overload/

### cache-invalidation-ownership
- **definition**: Every mutable cached object has an owner and a tested invalidation or versioning mechanism, with TTL serving only as a bounded safety net. The owner defines ordering, scope, failure recovery, and acceptable staleness.
- **implementation**:
  - Record resource owner, cache key schema, mutation event, invalidation consumer, and TTL in a registry.
  - Prefer versioned keys or transactional outbox events so mutations and invalidations cannot silently diverge.
  - Make invalidation idempotent, observable, replayable, and scoped to affected tenants/resources.
  - Define purge authority and emergency broad-purge rate limits.
- **probe**: Ask: “For each mutable cache, who owns freshness and what happens when invalidation delivery fails?” Present options: (A) event/version-based invalidation with bounded TTL, (B) synchronous purge on every mutation, or (C) explicitly accept bounded stale data with owner, TTL, and incident procedure.
- **failure_modes**: Prevents changed permissions remaining cached after revocation; prevents a lost event leaving prices stale indefinitely; prevents broad emergency purges overwhelming the origin.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Invalidation.html, https://www.rfc-editor.org/rfc/rfc9111.html

### cache-hit-observability
- **definition**: Cache observability measures hit, miss, age, eviction, stale-serve, origin-fallback, and cache-latency behavior by region and key class. It connects cache state to origin load so a nominally healthy cache cannot hide a capacity failure.
- **implementation**:
  - Emit counters and latency histograms for hits, misses, refreshes, stale responses, evictions, and errors.
  - Tag metrics by bounded resource class, region, cache tier, and outcome rather than raw keys.
  - Correlate origin request rate and dependency saturation with cache misses and evictions.
  - Alert on hit-rate drops, hot-key churn, stale/error fallback, and cache capacity thresholds.
- **probe**: The assessor must inspect metric definitions, bounded labels, dashboards, and alerts; expire a hot key and induce cache/origin failure to verify hit/miss, stale, fallback, latency, and origin-load signals distinguish each state.
- **failure_modes**: Prevents an eviction storm being mistaken for normal traffic; prevents stale fallback hiding a failing origin until TTL exhaustion; prevents regional cache degradation going unnoticed in global averages.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Strategies.html, https://sre.google/sre-book/monitoring/

### cdn-origin-offload
- **definition**: CDN origin offload routes static and safely cacheable public responses through edge caches with deliberate cache, compression, and origin-shield policies. It reduces application-origin requests while preserving correctness for dynamic or private traffic.
- **implementation**:
  - Configure origin, cache key, TTL, compression, TLS, and purge policy per resource class.
  - Use origin shield or equivalent aggregation where regional fan-out justifies it.
  - Set immutable asset and validator policies, and bypass shared caching for private responses.
  - Measure edge hits, origin requests, bandwidth, age, and failover behavior by region.
- **probe**: Fetch representative objects through the public CDN URL and assert cache status or Age headers, expected TTL and encoding, and measurable reduction in origin requests during repeated loads.
- **failure_modes**: Prevents every browser asset request reaching app replicas; prevents a regional traffic spike exhausting one origin; prevents CDN caching authenticated or mutable content under a public policy.
- **severity**: important
- **applies_if**: spa
- **sources**: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/UnderstandCacheBehavior.html, https://www.rfc-editor.org/rfc/rfc9111.html

### load-balancer-affinity-policy
- **definition**: Load-balancer affinity is disabled unless a documented stateful requirement justifies it. Any required stickiness specifies key, lifetime, rebalance behavior, and fallback so load remains distributable and replica loss is survivable.
- **implementation**:
  - Prefer shared session state and independent requests over cookie/IP affinity.
  - If affinity is required, use a bounded signed cookie or stable application key with explicit TTL.
  - Define behavior when a target is removed, overloaded, unhealthy, or newly added.
  - Monitor per-replica skew and provide a tested non-affinity fallback.
- **probe**: The assessor must inspect balancer policy, affinity key/lifetime, state dependencies, removal behavior, and per-replica distribution under scale-out and failure; verify a client can continue on another replica when its target disappears.
- **failure_modes**: Prevents one sticky replica becoming a hot spot; prevents all users behind one NAT address pinning to one target; prevents a target failure logging out users or trapping them on an unhealthy node.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://nginx.org/en/docs/http/load_balancing.html, https://12factor.net/processes

### hash-routing-stability
- **definition**: Consistent hash routing maps an explicit stable key to nodes while bounding remapping when membership changes. It must define missing-key, overloaded-node, and unavailable-node fallbacks rather than treating hash assignment as guaranteed capacity.
- **implementation**:
  - Select a key with documented cardinality and privacy properties, and use a ring or equivalent bounded-remap algorithm.
  - Configure virtual nodes/weights and health-aware exclusion.
  - Define fallback routing for absent keys, overloaded nodes, and ring changes.
  - Measure remapped keys, cache warm-up, skew, and hot-key concentration during membership changes.
- **probe**: The assessor must inspect hash key choice, ring/weight settings, node-change algorithm, health fallback, and skew metrics; add/remove a node and verify remapping stays within the stated bound while requests remain routable.
- **failure_modes**: Prevents adding one replica invalidating every cache assignment; prevents a hot tenant key overloading one node; prevents unhealthy hash targets black-holing requests.
- **severity**: important
- **applies_if**: all
- **sources**: https://nginx.org/en/docs/http/ngx_http_upstream_module.html#hash

### health-check-and-drain
- **definition**: Health-aware routing separates process liveness from dependency-aware readiness and combines it with active checks, outlier removal, and connection draining. An overloaded or dependency-isolated replica is withdrawn while healthy replicas continue serving and existing work drains safely.
- **implementation**:
  - Provide distinct liveness and readiness contracts with bounded dependency checks.
  - Configure active balancer checks, failure thresholds, recovery thresholds, and outlier ejection.
  - Mark readiness false before deregistration and honor keep-alive/drain deadlines.
  - Emit reason-coded readiness, ejection, drain, and connection-completion metrics.
- **probe**: Make a replica fail each critical dependency and enter drain mode, then assert the balancer stops new requests while healthy replicas continue serving and existing connections finish.
- **failure_modes**: Prevents an alive-but-disconnected pod receiving all traffic; prevents one overloaded target causing retries to amplify load; prevents deploy-time connection resets and partial responses.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: health-check-contracts
- **sources**: https://nginx.org/en/docs/http/load_balancing.html, https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/

### autoscale-on-work-signals
- **definition**: Autoscaling follows the resource or work signal that limits throughput, such as queue depth, active concurrency, request rate, or tail latency, alongside CPU when useful. The policy connects signal thresholds to capacity and avoids using CPU as a universal proxy.
- **implementation**:
  - Identify the bottleneck per workload role and expose a bounded, low-lag metric.
  - Set target values from measured per-replica throughput and dependency-safe concurrency.
  - Combine signals conservatively with explicit scale-up/scale-down precedence and missing-data behavior.
  - Alert when work grows while CPU remains low or when scaling cannot reduce the bottleneck.
- **probe**: The assessor must inspect the selected signals, target derivation, stabilization behavior, and missing-data policy; apply CPU-light I/O load and CPU-heavy load separately and verify each scales according to the actual bottleneck.
- **failure_modes**: Prevents a queue growing while idle CPU suppresses scale-out; prevents CPU-only scaling adding replicas that overwhelm a database; prevents tail-latency overload remaining invisible behind average utilization.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/, https://sre.google/sre-book/handling-overload/

### autoscale-stability-guards
- **definition**: Autoscaler stability guards bound capacity changes and account for startup, warm-up, dependency saturation, and metric lag. Minimum/maximum replicas, stabilization windows, cooldowns, and rate limits prevent oscillation and cold-start amplification.
- **implementation**:
  - Set minimum warm capacity and maximum safe capacity from failure and dependency budgets.
  - Configure startup/readiness grace so cold replicas do not count before serving.
  - Use scale-up limits, scale-down stabilization, cooldowns, and policy selection explicitly.
  - Test missing metrics, partial failure, and controller restart behavior.
- **probe**: Parse autoscaler policy and run a step-load and step-down test, asserting bounded scale rate, no oscillation, sufficient warm capacity, and convergence after load changes.
- **failure_modes**: Prevents scale-up/scale-down thrashing around a threshold; prevents cold pods being counted before readiness; prevents rapid scale-out exhausting a database or quota.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/

### bounded-concurrency-backpressure
- **definition**: Backpressure bounds admitted work at the request, queue, body-size, and downstream-concurrency layers and returns an explicit overload outcome when capacity is exhausted. It preserves recovery capacity instead of allowing queues and memory to grow without limit.
- **implementation**:
  - Set per-route and global in-flight limits plus bounded queue depth and wait time.
  - Limit request bodies, batch sizes, fan-out, and downstream semaphore permits.
  - Shed or defer low-priority work first and return documented 429/503 or queue acknowledgements.
  - Propagate retry-after guidance and instrument admission, rejection, queue age, and saturation.
- **probe**: Apply load beyond configured capacity and assert in-flight work and memory remain bounded while excess requests receive documented 429, 503, or queue responses; verify recovery after load stops.
- **failure_modes**: Prevents a traffic spike exhausting heap through unbounded requests; prevents dependency timeouts creating a growing retry queue; prevents low-priority bulk work starving interactive traffic.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/handling-overload/, https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/

### capacity-headroom-model
- **definition**: A capacity model relates peak and burst workload to per-replica throughput, dependency limits, failure scenarios, scaling lag, and explicit headroom. It is a decision artifact with assumptions, measured inputs, and a review trigger rather than an average-throughput guess.
- **implementation**:
  - Record workload mix, concurrency, seasonality, burst duration, per-replica capacity, and warm-up time.
  - Model dependency quotas, database connections, queue limits, and N-1/N-2 replica failure.
  - Set headroom and alert thresholds separately for normal peak, burst, and recovery capacity.
  - Recalibrate after traffic, schema, instance, or dependency changes and assign an owner.
- **probe**: Ask: “What peak/burst demand, failure tolerance, per-replica capacity, dependency ceiling, scaling lag, and headroom should capacity guarantee?” Present options: (A) N+1 capacity at documented peak, (B) N+2 or regional-failure capacity, (C) lower headroom with explicit load shedding and risk acceptance; record numeric thresholds and owner.
- **failure_modes**: Prevents average traffic planning from failing during a launch burst; prevents losing one replica pushing the remainder past saturation; prevents autoscaling into a dependency quota ceiling.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/performance-efficiency-pillar/welcome.html, https://sre.google/sre-book/handling-overload/

### representative-load-and-soak-tests
- **definition**: Representative performance testing exercises ramp, peak, spike, soak, and dependency-failure behavior with production-shaped data, request mix, concurrency, and cache state. It measures degradation and recovery over enough time to expose leaks, queue buildup, and warm-up effects.
- **implementation**:
  - Version scenarios, traffic mix, datasets, concurrency, arrival process, cache state, and dependency stubs or quotas.
  - Include ramp-up, steady peak, sudden spike, long soak, scale events, and controlled dependency failure.
  - Capture latency percentiles, errors, throughput, saturation, queue age, memory, and recovery time.
  - Isolate test data and prohibit real customer side effects while preserving production topology and limits.
- **probe**: Parse load-test scenarios for ramp, steady, spike, soak, data-shape, and failure phases, execute them in an isolated environment, and record latency, errors, throughput, saturation, and recovery against documented thresholds.
- **failure_modes**: Prevents a memory leak appearing only after hours; prevents cache warm-up and queue buildup being missed by a five-minute test; prevents dependency failure causing unrehearsed cascading timeouts.
- **severity**: critical
- **applies_if**: all
- **sources**: https://jmeter.apache.org/usermanual/best-practices.html, https://docs.aws.amazon.com/wellarchitected/latest/performance-efficiency-pillar/test-and-measure-performance.html

### performance-regression-gate
- **definition**: A performance gate compares a release candidate with a versioned baseline on p50, p95, p99, error rate, throughput, and resource per request under the same workload. Release decisions use agreed thresholds and account for measurement variance rather than relying on functional success.
- **implementation**:
  - Pin workload, dataset, environment shape, cache state, and dependency behavior for baseline and candidate.
  - Run enough repetitions to calculate confidence intervals or robust deltas.
  - Gate latency, throughput, errors, CPU/memory, database work, and cost-per-request against route budgets.
  - Store artifacts and require an owner-approved exception with capacity impact for intentional regressions.
- **probe**: Run the fixed benchmark workload for baseline and candidate, calculate confidence-bounded metric deltas, and fail the release when configured latency, throughput, error, or resource thresholds regress.
- **failure_modes**: Prevents a feature passing tests while doubling p99 latency; prevents a small CPU increase multiplying infrastructure cost at scale; prevents a throughput regression surfacing only after rollout.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/sre-book/service-level-objectives/, https://jmeter.apache.org/usermanual/best-practices.html

### javascript-css-budget
- **definition**: Frontend asset budgets cap compressed transfer, parse, and execution cost per route and entrypoint. Code splitting and dependency checks keep users from downloading and executing features unrelated to the requested route.
- **implementation**:
  - Define route-level Brotli/gzip, raw, parse, and execution budgets with device assumptions.
  - Split routes and lazy-load noncritical features, polyfills, and third-party integrations.
  - Track dependency size deltas and reject accidental duplicate or oversized packages.
  - Emit build manifests and retain bundle-analysis artifacts for review.
- **probe**: Build the application, measure raw and Brotli or gzip sizes per entry and route chunk, and fail when transfer or execution budgets are exceeded; verify a route does not eagerly load unrelated chunks.
- **failure_modes**: Prevents a dependency update pushing mobile bundles past usable load time; prevents every route shipping an admin editor; prevents duplicate libraries inflating transfer and parse work.
- **severity**: important
- **applies_if**: spa
- **sources**: https://web.dev/articles/performance-budgets

### compression-content-encoding
- **definition**: Content encoding negotiates Brotli or gzip once for compressible representations and advertises the selected encoding and variation to intermediaries. The origin must not double-compress or serve an encoding the client did not accept.
- **implementation**:
  - Compress text responses above a measured threshold at one layer only.
  - Negotiate `Accept-Encoding` with correct quality and identity fallback.
  - Emit `Content-Encoding` and `Vary: Accept-Encoding` consistently, including CDN behavior.
  - Exclude already compressed formats and validate decompression, content length, and cache key semantics.
- **probe**: Request representative text assets with and without `Accept-Encoding` using `curl`, then assert negotiated encoding, correct `Vary: Accept-Encoding`, valid decompression, and no duplicate encoding.
- **failure_modes**: Prevents large JSON responses wasting bandwidth; prevents a proxy double-compressing a body and making it unreadable; prevents a cache serving Brotli bytes to an unsupported client.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9110.html, https://www.rfc-editor.org/rfc/rfc9111.html

### responsive-image-delivery
- **definition**: Responsive image delivery selects an appropriately sized modern format for the client and viewport while declaring dimensions and deferring below-the-fold work. Byte and quality limits control transfer, decode, and layout cost.
- **implementation**:
  - Generate width/format variants and use `srcset`/`sizes` or an image CDN transformation policy.
  - Include intrinsic `width` and `height` to reserve layout space.
  - Lazy-load below-fold images while eagerly prioritizing the hero image deliberately.
  - Enforce per-image byte, pixel, quality, and content-type limits at build or upload time.
- **probe**: Parse rendered image markup and the asset manifest, asserting responsive source variants, dimensions, modern formats, below-fold lazy loading, and per-image byte limits.
- **failure_modes**: Prevents a desktop-resolution photo delaying mobile interaction; prevents image decode shifting the page under a user's pointer; prevents untrusted uploads consuming excessive bandwidth or memory.
- **severity**: important
- **applies_if**: spa
- **sources**: https://web.dev/learn/images, https://web.dev/articles/fast

### hot-partition-safe-keys
- **definition**: A hot-partition-safe key distributes traffic and storage across shards with sufficient cardinality and avoids monotonic or single-dominant values as the sole partition key. Logical access patterns and skew are evaluated together, not just aggregate throughput.
- **implementation**:
  - Choose a high-cardinality key with measured tenant, time, and operation distribution.
  - Avoid raw timestamps, sequential IDs, or one dominant tenant as the only partition dimension.
  - Use bounded compound keys when reads can target the additional dimension without fan-out.
  - Monitor per-partition request rate, size, throttles, and skew with alerts before hard limits.
- **probe**: The assessor must inspect key cardinality, write/read distribution, dominant-tenant behavior, and growth projections; replay production-shaped traffic and verify no partition approaches its throughput or size limit while aggregate capacity remains available.
- **failure_modes**: Prevents a timestamp prefix funneling writes to one shard; prevents a large tenant throttling all of its requests while other shards idle; prevents a hot key causing retries and queue growth.
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html

### bucketed-partitions-and-rebalance
- **definition**: Bucketed partitioning salts or buckets unavoidable hot logical keys with bounded fan-out, while an online repartitioning path moves data and traffic before growth makes a migration urgent. Reads and writes must share a discoverable bucket scheme.
- **implementation**:
  - Define a bounded bucket count and deterministic hash/salt assignment per logical key and time window.
  - Maintain a bucket directory or fan-out query contract with explicit read amplification limits.
  - Provide dual-read/dual-write or versioned migration with checkpoints, reconciliation, and rollback.
  - Trigger rebalancing from per-partition load, size, and skew thresholds; test new bucket counts before rollout.
- **probe**: The assessor must inspect bucket count, fan-out/read budget, assignment versioning, migration tooling, reconciliation, rollback, and trigger thresholds; load a dominant tenant/time window and verify online redistribution without lost or duplicated records.
- **failure_modes**: Prevents one enterprise tenant overwhelming a single partition; prevents a retention window becoming a Kafka/DynamoDB hot spot; prevents emergency repartitioning requiring downtime or unbounded read fan-out.
- **severity**: important
- **applies_if**: data-pipeline
- **sources**: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html, https://kafka.apache.org/documentation/#intro_concepts_and_terms
