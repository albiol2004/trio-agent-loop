# Testing & quality gates — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 1). Raw item list, pre-synthesis.

### test-pyramid-budget
- **what**: Set an explicit budget for unit, component/integration, contract, and end-to-end tests, with the cheapest layers carrying most assertions.
- **why**: An end-to-end-heavy suite makes feedback slow and failures expensive, while a unit-only suite misses wiring defects.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://martinfowler.com/articles/practical-test-pyramid.html

### critical-path-coverage
- **what**: Require tests for business invariants, error paths, authorization, and boundary transitions rather than accepting a global percentage alone.
- **why**: A high aggregate percentage can leave the code that can lose money or data untested.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://martinfowler.com/bliki/TestCoverage.html, https://martinfowler.com/articles/practical-test-pyramid.html

### changed-line-coverage
- **what**: Gate changed executable lines on a documented coverage threshold and require explicit review for justified exclusions.
- **why**: Coverage can silently fall in new code while a legacy baseline keeps the total green.
- **check**: probe
- **probe**: Parse the coverage report and merge-base diff, then fail when changed executable lines miss the threshold or an exclusion lacks an approved record.
- **applies_if**: all
- **severity**: important
- **sources**: https://martinfowler.com/bliki/TestCoverage.html, https://docs.codecov.com/docs/commit-status

### mutation-score-core
- **what**: Run mutation testing on critical packages at least on a scheduled or release gate and enforce a mutation-score floor.
- **why**: Line coverage can be satisfied by assertions that never detect a changed result, allowing real regressions through.
- **check**: probe
- **probe**: Run the configured mutation command, parse killed, survived, and equivalent mutants, and fail when surviving non-equivalent mutants exceed the package threshold.
- **applies_if**: all
- **severity**: important
- **sources**: https://pitest.org/quickstart/basic_concept/, https://martinfowler.com/bliki/TestCoverage.html

### openapi-contract-source
- **what**: Keep the OpenAPI document versioned with the implementation and validate request, response, status, and error shapes in provider tests.
- **why**: Undocumented or drifting schemas break generated clients and integration consumers after an apparently compatible deploy.
- **check**: probe
- **probe**: Parse every OpenAPI document with a standards validator and execute schema-conformance tests against each declared operation and response.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://spec.openapis.org/oas/latest.html, https://schemathesis.readthedocs.io/en/stable/

### pact-provider-verification
- **what**: Publish consumer contracts and require provider verification, including negative and error interactions, before a provider release.
- **why**: Provider unit tests cannot reveal that a real consumer depends on a field, status, or ordering the provider changed.
- **check**: probe
- **probe**: Run the Pact broker verification and can-I-deploy check for every changed provider and consumer version, failing on any unverified pact.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.pact.io/, https://docs.pact.io/pact_broker

### compatibility-window
- **what**: Declare backward-compatibility windows for APIs and events and test old clients or schemas against every supported provider version.
- **why**: Mobile and long-lived clients update asynchronously, so removing a field or changing an enum can strand deployed clients.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://spec.openapis.org/oas/latest.html, https://docs.pact.io/

### critical-journeys-only
- **what**: Use end-to-end tests for a small named set of revenue, authentication, data-loss, and release-critical journeys with stable user-facing selectors.
- **why**: A broad UI suite duplicates lower-layer checks and creates flaky gates that teams learn to ignore.
- **check**: judgment
- **applies_if**: spa
- **severity**: important
- **sources**: https://martinfowler.com/articles/practical-test-pyramid.html, https://playwright.dev/docs/best-practices

### isolated-e2e-contexts
- **what**: Give every end-to-end test a fresh browser context, account or data namespace, and cleanup path so it can run in any order and in parallel.
- **why**: Shared cookies, users, and records create order-dependent failures and make sharding unsafe.
- **check**: probe
- **probe**: Inspect runner fixtures for per-test context creation, then run the suite twice with randomized order and parallel workers and fail on cross-test state.
- **applies_if**: spa
- **severity**: important
- **sources**: https://playwright.dev/docs/browser-contexts, https://playwright.dev/docs/best-practices

### flake-rate-measurement
- **what**: Track pass-after-retry and intermittent failure rates by test, owner, and commit over a rolling window.
- **why**: Without a flake signal, nondeterministic tests become invisible reliability debt and erode trust in every gate.
- **check**: probe
- **probe**: Parse CI test-result history, calculate first-attempt failures and rerun passes for each test over the window, and report or fail when the flake budget is exceeded.
- **applies_if**: all
- **severity**: important
- **sources**: https://playwright.dev/docs/test-retries, https://martinfowler.com/articles/nonDeterminism.html

### bounded-retries
- **what**: Allow only a small configured retry count for diagnosis and preserve the original failure as a failed or flaky outcome when a rerun passes.
- **why**: Unlimited or silent retries convert real regressions into green builds and conceal environment instability.
- **check**: probe
- **probe**: Parse runner and CI retry settings and result annotations, then fail if retries are unbounded or a retry-passed test is reported as a clean pass without a flaky marker.
- **applies_if**: all
- **severity**: critical
- **sources**: https://playwright.dev/docs/test-retries

### expiring-quarantine
- **what**: Quarantine a flaky test only with an owner, issue, reason, expiry date, separate non-blocking reporting, and a capped quarantine count.
- **why**: Permanent skip lists remove coverage exactly where failures are most likely to hide.
- **check**: probe
- **probe**: Parse quarantine annotations and metadata, then fail when any entry lacks an owner or expiry, is expired, or exceeds the configured count or age budget.
- **applies_if**: all
- **severity**: important
- **sources**: https://playwright.dev/docs/test-retries, https://martinfowler.com/articles/nonDeterminism.html

### controlled-clock
- **what**: Inject a fake or monotonic clock and advance it explicitly in tests that depend on time, timers, TTLs, or scheduling.
- **why**: Wall-clock reads, sleeps, and DST boundaries create slow tests that pass or fail based on machine timing.
- **check**: probe
- **probe**: Statically scan test code for direct wall-clock or sleep calls and execute time-sensitive tests with a frozen or controlled-clock fixture, failing uncatalogued uses.
- **applies_if**: all
- **severity**: important
- **sources**: https://martinfowler.com/articles/nonDeterminism.html, https://jestjs.io/docs/timer-mocks

### network-egress-block
- **what**: Block real outbound network access in unit and integration tests and use explicit mocks or ephemeral service dependencies.
- **why**: DNS, rate limits, third-party outages, and mutable remote data make tests non-hermetic and can leak credentials.
- **check**: probe
- **probe**: Run the test job with egress denied and assert that all tests pass using declared interceptors or local service containers, failing on any unexpected socket.
- **applies_if**: all
- **severity**: critical
- **sources**: https://martinfowler.com/articles/nonDeterminism.html, https://bazel.build/basics/hermeticity

### reproducible-randomness
- **what**: Inject seeded random-number generators into randomized code and emit the seed and generated case on failure.
- **why**: A random failure cannot be reproduced or fixed when the test does not preserve its input.
- **check**: probe
- **probe**: Run randomized tests with a fixed seed, replay the seed from failure metadata, and fail if unseeded randomness or missing seed artifacts are observed.
- **applies_if**: all
- **severity**: important
- **sources**: https://martinfowler.com/articles/nonDeterminism.html, https://hypothesis.readthedocs.io/en/latest/quick-start.html

### timezone-locale-matrix
- **what**: Fix timezone, locale, calendar, newline, encoding, and collation in CI while covering a small matrix of supported variations.
- **why**: Formatting, parsing, sorting, and date logic can differ by runner and corrupt user-visible or persisted values.
- **check**: probe
- **probe**: Print and assert locale, timezone, and encoding in the test process, then run the configured matrix and compare deterministic expected outputs.
- **applies_if**: all
- **severity**: important
- **sources**: https://martinfowler.com/articles/nonDeterminism.html

### hermetic-toolchain
- **what**: Pin runtimes, compilers, packages, browsers, databases, and container images with lockfiles or immutable digests and verify them in CI.
- **why**: Unplanned upgrades and mutable images make a green commit impossible to reproduce.
- **check**: probe
- **probe**: Parse lockfiles, image references, and CI setup steps, then fail on unpinned versions or on a rerun whose dependency and toolchain checksums differ.
- **applies_if**: all
- **severity**: critical
- **sources**: https://bazel.build/basics/hermeticity

### property-invariant-generators
- **what**: Use property-based generators and shrinking for parsers, serializers, state machines, and domain invariants, retaining every minimized counterexample.
- **why**: Hand-picked examples miss combinatorial boundaries and malformed inputs that cause correctness or security defects.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://hypothesis.readthedocs.io/en/latest/quick-start.html, https://hackage.haskell.org/package/QuickCheck

### continuous-fuzz-targets
- **what**: Maintain fuzz targets for parsers, protocol handlers, deserializers, and public input boundaries with bounded CI smoke budgets and longer scheduled campaigns.
- **why**: Rare malformed sequences and state combinations can crash or hang code that example-based tests never exercise.
- **check**: probe
- **probe**: Discover registered fuzz targets, run each within the pull-request time budget, and assert that scheduled jobs upload crash or timeout artifacts and corpus updates.
- **applies_if**: library
- **severity**: important
- **sources**: https://llvm.org/docs/LibFuzzer.html, https://google.github.io/oss-fuzz/

### fuzz-regression-seeds
- **what**: Promote every minimized fuzz crash, timeout, and security finding into a deterministic regression test or seed corpus.
- **why**: A one-off fuzz fix can regress silently when future campaigns do not rediscover the same path.
- **check**: probe
- **probe**: Enumerate corpus and regression fixtures referenced by each fuzz target, execute them in CI, and fail when a recorded finding has no runnable case.
- **applies_if**: all
- **severity**: important
- **sources**: https://llvm.org/docs/LibFuzzer.html, https://google.github.io/oss-fuzz/

### performance-thresholds
- **what**: Benchmark critical requests, jobs, queries, and startup paths on stable representative data with explicit p95 or p99, throughput, error, and resource budgets.
- **why**: Functional tests remain green while latency, memory, or throughput regressions breach user and capacity SLOs.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://grafana.com/docs/k6/latest/using-k6/thresholds/

### scheduled-load-soak
- **what**: Run load, stress, spike, and soak tests on a production-like isolated environment on a schedule and before high-risk releases.
- **why**: Short pull-request tests cannot expose saturation, leaks, queue buildup, autoscaling, or long-lived state failures.
- **check**: probe
- **probe**: Parse CI schedules and load-test scenarios, execute them against the target environment, and fail when configured latency, error, or resource thresholds are exceeded.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://grafana.com/docs/k6/latest/using-k6/thresholds/, https://grafana.com/docs/k6/latest/using-k6/scenarios/

### benchmark-noise-control
- **what**: Control performance benchmark variance with warmups, fixed data, stable runners, multiple samples, and statistical comparison instead of one timing.
- **why**: Noisy measurements create false alarms or mask real regressions, causing teams to disable the gate.
- **check**: judgment
- **applies_if**: all
- **severity**: nice-to-have
- **sources**: https://grafana.com/docs/k6/latest/using-k6/thresholds/

### factory-based-fixtures
- **what**: Build minimal immutable fixture factories or builders and make each test declare only the data it needs.
- **why**: Copy-pasted oversized fixtures become stale, while shared mutation creates hidden coupling and expensive setup.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.pytest.org/en/stable/how-to/fixtures.html, https://martinfowler.com/articles/practical-test-pyramid.html

### privacy-safe-test-data
- **what**: Use synthetic or irreversibly de-identified test data, keep secrets out of fixtures and artifacts, and define retention and access controls.
- **why**: Personal data or production credentials in tests and CI artifacts turn quality infrastructure into a breach and compliance liability.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/publications/detail/sp/800-122/final, https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts

### database-test-isolation
- **what**: Reset database state per test or allocate an ephemeral database, schema, or namespace per worker with deterministic seed and cleanup.
- **why**: Cross-test rows and schema state make failures order-dependent and can contaminate parallel jobs.
- **check**: probe
- **probe**: Start a clean datastore or unique namespace per worker, run tests in randomized and parallel order, and assert that no rows or resources remain after teardown.
- **applies_if**: all
- **severity**: important
- **sources**: https://testcontainers.com/, https://docs.pytest.org/en/stable/how-to/fixtures.html

### migration-upgrade-tests
- **what**: Test fresh installs, every supported upgrade path, rollback or forward recovery, and backward-compatible application/database deployment order.
- **why**: A migration that works only on a blank database can corrupt or strand existing production data during rollout.
- **check**: probe
- **probe**: Create databases at each supported schema version, apply migrations and recovery commands, then run compatibility tests with both old and new application binaries.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://documentation.red-gate.com/flyway/flyway-concepts/migrations

### required-merge-checks
- **what**: Protect the default branch with required, fresh, passing test checks and prevent merges when required jobs are skipped, stale, or bypassed.
- **why**: A green local run is not evidence that the exact commit and integration context passed the release safety net.
- **check**: probe
- **probe**: Query branch-protection settings and CI conclusions for the exact commit, failing if required test jobs are absent, stale, skipped, or mergeable despite failure.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches

### impact-aware-sharding
- **what**: For large repositories, shard tests and use dependency-aware test selection only as an acceleration layer with an explicit fallback to broader suites.
- **why**: Naive changed-file selection either makes pull requests unusably slow or silently omits tests affected through shared libraries.
- **check**: probe
- **probe**: Parse the test-selection graph and CI matrix, verify that selected tests cover declared reverse dependencies, and require a full fallback when graph data is missing.
- **applies_if**: monorepo
- **severity**: important
- **sources**: https://bazel.build/query/guide, https://docs.github.com/en/actions/using-jobs/using-a-matrix-for-your-jobs

### nightly-full-suite
- **what**: Run the complete deterministic suite, mutation and fuzz campaigns, compatibility matrix, and environment matrix on scheduled and release-candidate jobs.
- **why**: Pull-request optimization can hide interactions and platform regressions that appear only outside the changed package.
- **check**: probe
- **probe**: Parse scheduled and release workflows and verify that they invoke full test targets without changed-file filters, failing on missing or stale runs.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule, https://docs.github.com/en/actions/using-jobs/using-a-matrix-for-your-jobs

### preview-environment-smoke
- **what**: Create an isolated preview or test environment for each change, seed it with non-sensitive data, run contract and smoke end-to-end checks, and tear it down.
- **why**: Integration and deployment wiring can fail even when tests pass against mocks or shared staging.
- **check**: probe
- **probe**: Trigger a preview deployment from a pull request, wait for readiness, execute smoke and contract checks against its URL, and assert cleanup after merge or close.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.gitlab.com/ci/review_apps/

### failure-artifacts-and-triage
- **what**: Upload structured test reports plus logs, traces, screenshots or video, seeds, environment metadata, and minimized inputs for every failed or flaky test.
- **why**: Without actionable artifacts, engineers rerun blindly, lose the original failure, and cannot repair flakes.
- **check**: probe
- **probe**: Run a deliberately failing test in CI and assert that JUnit or JSON results and configured diagnostic artifacts are attached, retained, and linked to the test ID.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts, https://playwright.dev/docs/trace-viewer
