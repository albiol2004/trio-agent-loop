# Scalability & performance — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 2). Raw item list, pre-synthesis.

### stateless-request-handling
- **what**: Keep request processing free of replica-local mutable state and persist sessions, uploads, and durable work in shared services.
- **why**: A restart or horizontally added replica otherwise loses state or returns inconsistent results.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/processes, https://12factor.net/backing-services

### replica-safe-coordination
- **what**: Put leader election, distributed locks, scheduled-job ownership, and other singleton coordination in an external mechanism rather than process memory.
- **why**: Every replica may otherwise run the singleton work concurrently or silently stop it when one process dies.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/processes, https://kubernetes.io/docs/concepts/cluster-administration/cluster-intro/

### ephemeral-local-disk
- **what**: Restrict local filesystems to disposable temporary data and route durable files, exports, and artifacts to shared storage.
- **why**: Load balancing and rescheduling make a file written on one replica unavailable to the next request.
- **check**: probe
- **probe**: Scan application code and deployment manifests for writes outside an allowlisted temporary directory, then fail if upload, export, or artifact paths target replica-local storage.
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/backing-services, https://kubernetes.io/docs/concepts/storage/volumes/

### graceful-drain-on-scale-down
- **what**: Mark a replica unready before termination, stop accepting new work, honor SIGTERM, and drain existing requests within a bounded grace period.
- **why**: Abrupt scale-down drops in-flight requests and can trigger retry storms or duplicate work.
- **check**: probe
- **probe**: Parse deployment lifecycle and readiness settings, send SIGTERM to a running replica under active requests, and assert readiness fails before the process exits without dropped or duplicated completions.
- **applies_if**: all
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-flow

### retry-safe-mutations
- **what**: Make retried mutations idempotent with an idempotency key or deduplication record whose scope and retention cover the retry window.
- **why**: Network timeouts otherwise cause clients or load balancers to create duplicate payments, jobs, or state transitions.
- **check**: probe
- **probe**: Submit the same mutation concurrently and after a forced response timeout with one idempotency key, then assert one durable side effect and equivalent replay responses.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.stripe.com/api/idempotent_requests

### connection-pool-budget
- **what**: Size every client pool so the maximum replica count times per-replica connections plus migrations, administrators, and failover reserve stays below the database connection limit.
- **why**: Horizontal scaling otherwise exhausts database connections before application capacity increases.
- **check**: probe
- **probe**: Parse the deployment replica maximum, pool maximum, pooler limits, and database max-connections values and assert `replicas × pool_max + reserve <= database_max_connections`.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/runtime-config-connection.html, https://www.pgbouncer.org/config.html

### pool-timeouts-and-leak-detection
- **what**: Configure finite connection acquisition, connect, idle, lifetime, and query timeouts and expose pool wait, in-use, idle, and leak indicators.
- **why**: A dead database or leaked checkout can otherwise consume every worker and turn a dependency fault into total outage.
- **check**: probe
- **probe**: Parse pool timeout settings and run a dependency-stall test, asserting bounded request time, pool wait telemetry, and recovery after connections are released.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.pgbouncer.org/config.html, https://sre.google/sre-book/handling-overload/

### connection-pooler-boundary
- **what**: Introduce a pooler or proxy when replica fan-out would create excessive database backends and verify that transaction pooling is compatible with application session state.
- **why**: Direct per-process connections do not scale with replica count and session-bound features can fail invisibly behind transaction pooling.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://www.pgbouncer.org/config.html, https://www.postgresql.org/docs/current/runtime-config-connection.html

### access-path-indexes
- **what**: Create indexes from measured WHERE, JOIN, and ORDER BY access paths for the production query corpus rather than indexing columns by convention.
- **why**: Missing or nonselective access paths force scans and sorts as data volume grows.
- **check**: probe
- **probe**: Run representative queries with `EXPLAIN (FORMAT JSON)` against production-shaped cardinalities and fail when a required selective access path is absent or estimated work exceeds its budget.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/indexes.html, https://www.postgresql.org/docs/current/using-explain.html

### composite-index-order
- **what**: Order composite-index columns to match equality prefixes followed by range and ordering predicates, with a unique tie-breaker where pagination needs one.
- **why**: A syntactically present composite index can still leave deep scans when its leftmost order does not match the query.
- **check**: probe
- **probe**: Execute each high-volume query shape with `EXPLAIN` and assert the plan uses the intended equality prefix, range condition, and ordering without a large residual sort or scan.
- **applies_if**: all
- **severity**: important
- **sources**: https://www.postgresql.org/docs/current/indexes-multicolumn.html, https://www.postgresql.org/docs/current/using-explain.html

### index-write-amplification
- **what**: Review index usage, storage, and write cost periodically and remove redundant or unused indexes only after confirming their read coverage.
- **why**: Unbounded indexing slows inserts and updates, increases replication and backup volume, and consumes cache capacity.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://www.postgresql.org/docs/current/indexes.html, https://www.postgresql.org/docs/current/monitoring-stats.html

### plan-regression-gates
- **what**: Version representative query plans and compare estimated rows, actual rows, buffers, and execution time across schema and statistics changes.
- **why**: A small data-distribution or planner change can silently turn an indexed query into a sequential scan.
- **check**: probe
- **probe**: Run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on fixed production-shaped fixtures before and after a change and fail on configured plan-node, row-estimate, buffer, or latency regressions.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/using-explain.html

### n-plus-one-query-budget
- **what**: Instrument database spans or query counters per request and require collection endpoints to use a bounded query count independent of returned item count.
- **why**: N+1 loading turns a seemingly cheap endpoint into thousands of round trips as one page grows.
- **check**: probe
- **probe**: Exercise each collection endpoint with fixtures containing 1, 10, and 100 related records and fail if database-query count grows linearly with collection size.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://opentelemetry.io/docs/specs/semconv/database/, https://www.postgresql.org/docs/current/using-explain.html

### endpoint-latency-and-query-budget
- **what**: Set explicit p95 and p99 end-to-end latency, database-time, and dependency-time budgets for each critical request path.
- **why**: Without budgets, faster code can still ship while tail latency and downstream saturation accumulate.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://sre.google/sre-book/service-level-objectives/, https://aws.amazon.com/builders-library/latency-aware-load-balancing/

### bounded-query-work
- **what**: Bound result rows, sort and join work, execution time, and memory for every externally triggered query.
- **why**: An unbounded query lets one request consume shared database resources and block otherwise healthy traffic.
- **check**: probe
- **probe**: Lint query construction for missing limits or timeouts, then run worst-case cardinality fixtures with `EXPLAIN (ANALYZE, BUFFERS)` and assert bounded rows, memory, and duration.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/queries-limit.html, https://www.postgresql.org/docs/current/using-explain.html

### keyset-pagination
- **what**: Use an opaque cursor over a unique, indexed, stable ordering instead of deep OFFSET pagination for large datasets.
- **why**: OFFSET makes the database walk and discard an ever-growing prefix and becomes slower and less stable under concurrent writes.
- **check**: probe
- **probe**: Request a deep page while capturing the query plan and assert it uses a cursor range predicate on the ordering index, not a growing OFFSET scan.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://cloud.google.com/apis/design/design_patterns#list_pagination, https://www.postgresql.org/docs/current/queries-limit.html

### bounded-page-contract
- **what**: Enforce a server-side maximum page size, deterministic tie-broken ordering, field selection, and an explicit next-cursor contract.
- **why**: A client-controlled page can create oversized queries, responses, memory spikes, and inconsistent page boundaries.
- **check**: probe
- **probe**: Call the list endpoint with zero, negative, maximum-plus-one, and very large page sizes and assert bounded responses, deterministic ordering, and a valid next-cursor or documented error.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cloud.google.com/apis/design/design_patterns#list_pagination

### cache-control-contract
- **what**: Emit explicit Cache-Control directives that distinguish public, private, and non-cacheable responses and set a deliberate freshness lifetime.
- **why**: Ambiguous cache policy either overloads the origin or serves private or stale data from shared caches.
- **check**: probe
- **probe**: Fetch representative public, authenticated, mutable, and error responses with `curl -I` and assert expected `Cache-Control`, freshness, and `no-store` or `private` directives.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9111.html, https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control

### validators-and-revalidation
- **what**: Provide strong or appropriately scoped ETags or Last-Modified validators and return 304 for unchanged conditional requests.
- **why**: Clients and CDNs otherwise redownload unchanged payloads and consume origin bandwidth and serialization capacity.
- **check**: probe
- **probe**: Fetch a cacheable response, replay it with `If-None-Match` or `If-Modified-Since`, and assert a correct 304 with no response body and unchanged validator semantics.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9111.html, https://www.rfc-editor.org/rfc/rfc9110.html

### cache-key-variation
- **what**: Include representation, encoding, locale, tenant, and authorization dimensions in cache keys and declare required Vary dimensions.
- **why**: A cache key that omits a response dimension can return the wrong variant or achieve misleadingly poor hit rates.
- **check**: probe
- **probe**: Request the same URL across each supported encoding, locale, tenant, and authentication state and assert distinct authorized bodies or an explicit non-shared-cache policy.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9111.html

### immutable-versioned-assets
- **what**: Fingerprint deployable static assets and serve them with long-lived immutable caching while keeping the entry manifest short-lived.
- **why**: Reusing filenames forces revalidation or risks stale JavaScript and CSS after deployment.
- **check**: probe
- **probe**: Parse the build manifest for content hashes and fetch hashed and unhashed assets, asserting hashed responses have a long `max-age` with `immutable` and the manifest has a short revalidation policy.
- **applies_if**: spa
- **severity**: important
- **sources**: https://web.dev/articles/performance-budgets, https://www.rfc-editor.org/rfc/rfc9111.html

### app-cache-stampede-control
- **what**: Implement cache-aside misses with single-flight or lease protection, TTL jitter, bounded stale serving, and negative caching where safe.
- **why**: Simultaneous expiry otherwise sends a synchronized miss burst to the database or downstream service.
- **check**: probe
- **probe**: Expire one hot key and issue concurrent misses while counting origin calls, then assert one refresh, bounded waiters, and an explicit stale or error behavior on refresh failure.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Strategies.html, https://sre.google/sre-book/handling-overload/

### cache-invalidation-ownership
- **what**: Assign an owner and tested mechanism for invalidating or versioning every mutable cached object, with TTL as a safety bound rather than the only correctness mechanism.
- **why**: Unowned invalidation leaves stale data indefinitely or causes broad purges that overload the origin.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Invalidation.html, https://www.rfc-editor.org/rfc/rfc9111.html

### cache-hit-observability
- **what**: Measure cache hits, misses, age, evictions, stale serves, origin fallback, and cache latency by region and key class.
- **why**: A cache can appear healthy while silently missing hot keys or amplifying origin load during an incident.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Strategies.html, https://sre.google/sre-book/monitoring/

### cdn-origin-offload
- **what**: Route static and safely cacheable public responses through a CDN with an explicit cache policy, compression policy, and origin shield or equivalent where scale warrants it.
- **why**: Serving every asset and cacheable response from application replicas wastes compute and concentrates origin failures.
- **check**: probe
- **probe**: Fetch representative objects through the public CDN URL and assert cache status or Age headers, expected TTL and encoding, and a measurable reduction in origin requests during repeated loads.
- **applies_if**: spa
- **severity**: important
- **sources**: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/UnderstandCacheBehavior.html, https://www.rfc-editor.org/rfc/rfc9111.html

### load-balancer-affinity-policy
- **what**: Disable sticky sessions by default and document any required affinity key, lifetime, rebalance behavior, and failure fallback.
- **why**: Unbounded stickiness creates uneven load and makes a single replica failure disproportionately disruptive.
- **check**: judgment
- **applies_if**: web-api
- **severity**: important
- **sources**: https://nginx.org/en/docs/http/load_balancing.html, https://12factor.net/processes

### hash-routing-stability
- **what**: Use consistent hashing only with an explicit stable key, bounded remapping, node-change behavior, and a fallback for missing or overloaded nodes.
- **why**: Unspecified hash routing causes cache churn and hot reassignment whenever replicas or shards change.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://nginx.org/en/docs/http/ngx_http_upstream_module.html#hash

### health-check-and-drain
- **what**: Configure load balancing with dependency-aware readiness, active health checks where needed, outlier removal, and connection draining.
- **why**: A process that is alive but overloaded or disconnected can continue receiving traffic and amplify an outage.
- **check**: probe
- **probe**: Make a replica fail each critical dependency and enter drain mode, then assert the balancer stops new requests while healthy replicas continue serving and existing connections finish.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://nginx.org/en/docs/http/load_balancing.html, https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/

### autoscale-on-work-signals
- **what**: Scale on CPU together with the bottleneck signal such as queue depth, active concurrency, throughput, or tail latency rather than treating CPU as a universal proxy.
- **why**: I/O-bound or queued services can be overloaded at low CPU, while CPU-only policies can scale unnecessarily during noncritical work.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/, https://sre.google/sre-book/handling-overload/

### autoscale-stability-guards
- **what**: Set minimum and maximum capacity, startup grace, stabilization windows, cooldowns, and scale-up limits based on warm-up and dependency behavior.
- **why**: An aggressive controller can oscillate, amplify cold starts, or add replicas faster than dependencies can accept them.
- **check**: probe
- **probe**: Parse autoscaler policy and run a step-load and step-down test, asserting bounded scale rate, no oscillation, sufficient warm capacity, and convergence after load changes.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/

### bounded-concurrency-backpressure
- **what**: Cap in-flight work, queue length, request body size, and downstream concurrency and return an explicit overload response when capacity is exhausted.
- **why**: Unbounded admission converts a traffic spike into memory exhaustion, timeout cascades, and total service failure.
- **check**: probe
- **probe**: Apply load beyond configured capacity and assert in-flight work and memory remain bounded while excess requests receive documented 429, 503, or queue responses.
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/

### capacity-headroom-model
- **what**: Maintain a capacity model covering peak and burst workload, per-replica throughput, dependency saturation, failure of one or more replicas, and explicit headroom.
- **why**: Average throughput estimates hide peak, failover, and scaling-lag conditions that cause production saturation.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/wellarchitected/latest/performance-efficiency-pillar/welcome.html, https://sre.google/sre-book/handling-overload/

### representative-load-and-soak-tests
- **what**: Run ramp, peak, spike, soak, and dependency-failure tests with production-shaped data volume, request mix, concurrency, and cache state.
- **why**: A short synthetic happy-path test misses memory leaks, cache warm-up, queue buildup, and long-tail degradation.
- **check**: probe
- **probe**: Parse load-test scenarios for ramp, steady, spike, soak, data-shape, and failure phases, execute them in an isolated environment, and record latency, errors, throughput, saturation, and recovery.
- **applies_if**: all
- **severity**: critical
- **sources**: https://jmeter.apache.org/usermanual/best-practices.html, https://docs.aws.amazon.com/wellarchitected/latest/performance-efficiency-pillar/test-and-measure-performance.html

### performance-regression-gate
- **what**: Compare release candidates with a versioned baseline for p50, p95, p99, error rate, throughput, and resource-per-request against the agreed budgets.
- **why**: Functional tests can pass while a small regression compounds into higher infrastructure cost and tail-latency SLO failure.
- **check**: probe
- **probe**: Run the fixed benchmark workload for baseline and candidate, calculate confidence-bounded metric deltas, and fail the release when configured latency, throughput, error, or resource thresholds regress.
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/sre-book/service-level-objectives/, https://jmeter.apache.org/usermanual/best-practices.html

### javascript-css-budget
- **what**: Enforce route-level compressed transfer, parse, and execution budgets with code splitting and dependency-size checks.
- **why**: Unbounded bundles delay first interaction and make every client download work unrelated to the requested route.
- **check**: probe
- **probe**: Build the application, measure raw and Brotli or gzip sizes per entry and route chunk, and fail when transfer or execution budgets are exceeded.
- **applies_if**: spa
- **severity**: important
- **sources**: https://web.dev/articles/performance-budgets

### compression-content-encoding
- **what**: Negotiate Brotli or gzip for compressible text exactly once and emit the corresponding Content-Encoding and Vary headers.
- **why**: Missing compression wastes bandwidth while double compression, incompatible encoding, or missing variation headers causes errors and cache corruption.
- **check**: probe
- **probe**: Request representative text assets with and without `Accept-Encoding` using `curl`, then assert negotiated encoding, correct `Vary: Accept-Encoding`, valid decompression, and no duplicate encoding.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9110.html, https://www.rfc-editor.org/rfc/rfc9111.html

### responsive-image-delivery
- **what**: Deliver appropriately sized modern image formats with width and height metadata, lazy loading below the fold, and an explicit quality or byte limit.
- **why**: Oversized images dominate transfer and decode cost and can shift layout or block interaction on constrained clients.
- **check**: probe
- **probe**: Parse rendered image markup and the asset manifest, asserting responsive source variants, dimensions, modern formats, below-fold lazy loading, and per-image byte limits.
- **applies_if**: spa
- **severity**: important
- **sources**: https://web.dev/learn/images, https://web.dev/articles/fast

### hot-partition-safe-keys
- **what**: Choose high-cardinality partition keys that distribute traffic and storage and avoid monotonic timestamps or a single dominant tenant as the sole key.
- **why**: A hot partition throttles one shard while aggregate cluster capacity remains unused.
- **check**: judgment
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html

### bucketed-partitions-and-rebalance
- **what**: Bucket or salt unavoidable hot keys with bounded fan-out and provide an online repartitioning or rebalancing path before growth makes migration urgent.
- **why**: A large tenant or time window can still overload one partition even when the logical key is otherwise correct.
- **check**: judgment
- **applies_if**: data-pipeline
- **severity**: important
- **sources**: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html, https://kafka.apache.org/documentation/#intro_concepts_and_terms
