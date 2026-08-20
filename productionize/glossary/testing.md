# Testing & quality gates glossary

### test-pyramid-budget
- **definition**: A test-pyramid budget allocates assertion volume and runtime across unit, component/integration, contract, and end-to-end layers. Lower layers should carry most deterministic business assertions, while expensive end-to-end tests prove only wiring and representative journeys.
- **implementation**:
  - Define per-layer targets for test count or runtime, total CI minutes, and maximum end-to-end share in a versioned policy file.
  - Tag tests by layer and publish duration and failure-rate reports by tag on every CI run.
  - Put domain rules in unit/component tests; reserve browser and deployed-environment tests for cross-system contracts and critical paths.
  - Review budget exceptions with an owner and expiry rather than allowing unbounded growth of a slow layer.
- **probe**: User decision: “What test-layer budget will this project enforce?” Present options for (a) unit-heavy, (b) balanced integration, or (c) a custom allocation with maximum CI duration and end-to-end percentage. Record targets and exception policy.
- **failure_modes**: A browser-heavy suite takes 45 minutes, so developers stop running it and merge regressions. A unit-only suite passes while routing, serialization, or service wiring is broken.
- **severity**: important
- **applies_if**: all
- **sources**: https://martinfowler.com/articles/practical-test-pyramid.html

### critical-path-coverage
- **definition**: Critical-path coverage demonstrates tests for invariants and transitions whose failure can lose money, data, access, or availability. It is a risk-based complement to aggregate line or branch coverage, not a replacement for those measurements.
- **implementation**:
  - Maintain a catalog mapping revenue, authorization, destructive-operation, data-integrity, and recovery invariants to test IDs.
  - Require happy-path, validation, authorization, retry/duplicate, boundary, and failure assertions for each critical operation.
  - Mark intentionally unreachable or environment-only branches with reviewed exclusions and an owner.
  - Display coverage by critical component and invariant in release review, alongside global coverage.
- **probe**: An assessor inspects the risk register and critical journey catalog, traces each invariant to executable tests, and checks that negative, authorization, boundary, and recovery cases assert outcomes rather than merely executing lines.
- **failure_modes**: Overall coverage remains high while an authorization branch is untested and permits an IDOR. A payment or deletion invariant is tested only on success, so duplicate or partial-failure behavior corrupts state.
- **severity**: critical
- **applies_if**: all
- **sources**: https://martinfowler.com/bliki/TestCoverage.html, https://martinfowler.com/articles/practical-test-pyramid.html

### changed-line-coverage
- **definition**: Changed-line coverage gates newly introduced executable code against a stated threshold instead of letting a large legacy baseline hide gaps. Every excluded changed line must have a documented, approved reason.
- **implementation**:
  - Compute the merge base and changed executable lines from the same commit tested by CI.
  - Parse a machine-readable coverage report and intersect covered locations with changed executable locations.
  - Fail below the configured threshold, and require exclusion records containing path, lines, reason, reviewer, and expiry.
  - Keep generated files and non-executable lines out of the denominator through explicit tool configuration.
- **probe**: `BASE=$(git merge-base HEAD origin/main); git diff --unified=0 "$BASE" HEAD > /tmp/diff; coverage-tool report --format=json > coverage.json; changed-coverage --diff /tmp/diff --report coverage.json --threshold "$CHANGED_COVERAGE_MIN" --exclusions coverage-exclusions.yml` should exit nonzero for uncovered changed lines or unapproved exclusions.
- **failure_modes**: A new authorization branch is untested but total coverage stays green because thousands of legacy lines dominate the denominator. A broad ignore pattern silently excludes production code from the gate.
- **severity**: important
- **applies_if**: all
- **sources**: https://martinfowler.com/bliki/TestCoverage.html, https://docs.codecov.com/docs/commit-status

### mutation-score-core
- **definition**: Mutation testing changes operators or values in selected critical packages and measures whether tests detect those changes. A mutation-score floor focuses review effort where assertions must prove behavior, not just execution.
- **implementation**:
  - Configure a package allowlist and mutation exclusions for generated or untestable code, with reasons.
  - Run a bounded PR smoke set and a full scheduled or release mutation campaign.
  - Fail when surviving non-equivalent mutants exceed the package floor; report equivalent mutants separately.
  - Cache baseline analysis and publish surviving mutant source locations as review artifacts.
- **probe**: `mutation-tool run --targets critical-packages --report mutation.json`; parse `killed`, `survived`, and `equivalent`, then fail if `survived / (killed + survived)` exceeds each package threshold. Verify the CI job is scheduled or attached to the release gate.
- **failure_modes**: Tests assert only that a handler returns, so a changed comparison or removed validation survives. A mutation campaign runs without a threshold and produces reports nobody uses.
- **severity**: important
- **applies_if**: all
- **sources**: https://pitest.org/quickstart/basic_concept/, https://martinfowler.com/bliki/TestCoverage.html

### openapi-contract-source
- **definition**: The versioned OpenAPI document is the reviewable source of the HTTP contract, including inputs, outputs, statuses, and errors. Provider tests must prove implementation responses conform to the declared operations so generated clients and consumers see the same interface.
- **implementation**:
  - Store the OpenAPI document beside the service and validate it in CI against the applicable OAS version.
  - Generate or run request/response schema tests for every declared operation, status, content type, and error shape.
  - Compare the proposed document with the default branch and classify breaking changes before release.
  - Fail when undocumented endpoints or implementation responses bypass the contract validator.
- **probe**: `openapi lint openapi.yaml && schemathesis run openapi.yaml --base-url "$STAGING_URL" --checks all --report-junit contract.xml` should validate every operation and declared response; separately compare route inventory with document paths.
- **failure_modes**: An endpoint changes an enum or error body and generated clients fail after deployment. A route returns HTML or an undocumented status that integration consumers cannot parse.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html, https://schemathesis.readthedocs.io/en/stable/

### pact-provider-verification
- **definition**: Consumer-driven contracts record the fields, statuses, and interactions a real consumer requires, while provider verification runs those contracts against the provider build. Verification must include negative and error interactions, not just the successful example.
- **implementation**:
  - Publish versioned consumer pacts to a broker with branch, commit, and environment metadata.
  - Run provider verification against the candidate artifact and its configured dependencies.
  - Use a can-I-deploy gate that checks every changed consumer/provider version and records verification results.
  - Include authorization failures, validation errors, empty responses, and ordering/optional-field expectations in contracts.
- **probe**: `pact-broker can-i-deploy --pacticipant "$SERVICE" --version "$GIT_SHA" --to-environment production` must pass after `pact-provider-verifier --broker "$PACT_BROKER" --provider "$SERVICE" --publish`; fail on any unverified or missing pact.
- **failure_modes**: A provider removes a field that one consumer still reads, despite provider unit tests passing. A consumer assumes a 404 or error field that was never verified and crashes in production.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://docs.pact.io/, https://docs.pact.io/pact_broker

### compatibility-window
- **definition**: A compatibility window states how long API and event producers support old clients, schemas, and versions during asynchronous upgrades. Tests exercise every supported old consumer against each provider version that may serve it.
- **implementation**:
  - Document supported client, API, event-schema, and deprecation versions with end dates.
  - Keep old-client fixtures or contract versions runnable in the provider CI matrix.
  - Make additive evolution the default; preserve fields and enum behavior until the window expires.
  - Require telemetry showing old-version traffic is zero or accepted before removal.
- **probe**: User decision: “Which old API/client/event versions must remain compatible, for how long, and what evidence authorizes removal?” Present options of one release, a fixed duration, or indefinite support; record versions, expiry, and removal signal.
- **failure_modes**: A mobile client that updates slowly receives a removed response field and cannot render. An event consumer rejects a newly emitted enum or schema version while the producer rollout is otherwise healthy.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html, https://docs.pact.io/

### critical-journeys-only
- **definition**: Critical-journey E2E testing limits browser or UI tests to named flows whose failure directly affects revenue, authentication, data safety, or release confidence. Stable user-facing selectors and lower-layer tests prevent the suite from becoming a duplicate of unit checks.
- **implementation**:
  - Maintain a short catalog of journeys with business owner, entry point, expected outcome, and release impact.
  - Use accessible roles, labels, or dedicated test IDs rather than CSS structure or text incidental to presentation.
  - Keep setup API-driven and assertions focused on user-visible outcomes; push permutations to lower layers.
  - Track duration and flake rate per journey and remove redundant flows when coverage moves downward.
- **probe**: An assessor inspects the journey catalog and samples E2E tests for named risk linkage, stable selectors, deterministic setup, and assertions on user outcomes. Unnamed UI tests or tests duplicating unit permutations are findings.
- **failure_modes**: A broad selector-based suite flakes on harmless markup changes, training the team to rerun until green. A critical checkout journey is absent even though many low-risk screens have tests.
- **severity**: important
- **applies_if**: spa
- **sources**: https://martinfowler.com/articles/practical-test-pyramid.html, https://playwright.dev/docs/best-practices

### isolated-e2e-contexts
- **definition**: Each E2E test receives an isolated browser context and account or data namespace, plus deterministic teardown. Isolation makes order randomization and parallel sharding safe and prevents one test's cookies or records from affecting another.
- **implementation**:
  - Create a fresh Playwright browser context and unique tenant/user/resource prefix in a per-test fixture.
  - Inject only the test's credentials and seed data; avoid shared accounts and mutable global fixtures.
  - Register teardown in a finally/after fixture and make cleanup idempotent for failed tests.
  - Use worker-level namespaces only when resources cannot be per-test, and assert no cross-worker collisions.
- **probe**: `e2e-runner --workers 4 --fully-parallel --order random --repeat 2`; inspect fixtures for per-test context/namespace creation, then query the datastore for leftover test-prefixed resources after teardown. Fail on order-dependent outcomes or undeleted resources.
- **failure_modes**: A logged-in cookie from one test causes another test to run as the wrong user. Parallel tests update one shared record and pass or fail based on scheduling.
- **severity**: important
- **applies_if**: spa
- **sources**: https://playwright.dev/docs/browser-contexts, https://playwright.dev/docs/best-practices

### flake-rate-measurement
- **definition**: Flake measurement distinguishes first-attempt failures from tests that pass on a rerun and tracks the rate over time by test, owner, and commit. It turns intermittent failures into an owned reliability budget instead of invisible CI noise.
- **implementation**:
  - Persist normalized test IDs, attempt number, commit, suite, owner, duration, and final status from CI result files.
  - Calculate first-attempt failure and pass-after-retry rates over a rolling window, excluding infrastructure failures only by documented rule.
  - Set a flake budget and alert owners when a test or suite exceeds it.
  - Show trend and top offenders on a dashboard linked to quarantine issues.
- **probe**: `flake-report --results ci-history/ --window 30d --group-by test,owner --fail-above "$FLAKE_BUDGET"` must count first-attempt failures and retry passes independently and emit the offending test IDs.
- **failure_modes**: A timeout passes on its second attempt for weeks and is mistaken for a healthy gate. CI instability accumulates until developers ignore all failures, including real regressions.
- **severity**: important
- **applies_if**: all
- **sources**: https://playwright.dev/docs/test-retries, https://martinfowler.com/articles/nonDeterminism.html

### bounded-retries
- **definition**: Retries are a small diagnostic allowance, not a way to turn an initial failure into a clean pass. A retry-passed test remains visibly flaky and retains the original failure and artifacts.
- **implementation**:
  - Set a finite retry count per suite or CI job, normally zero for deterministic unit tests and at most a small number for E2E diagnosis.
  - Preserve attempt-level result, stdout, traces, and exit status in the report.
  - Mark pass-after-retry as flaky/unstable in branch protection and trend reporting.
  - Prevent per-test annotations or CI wrappers from increasing retries without review.
- **probe**: `config-inspect --tests --retries`; parse runner and CI configuration, reject missing/unbounded values, then run a deliberately failing test that passes on retry and assert its final report has a flaky marker and failed first attempt.
- **failure_modes**: An assertion regression is hidden by unlimited retries. A transient outage appears as a clean green build, so the underlying dependency instability is never repaired.
- **severity**: critical
- **applies_if**: all
- **sources**: https://playwright.dev/docs/test-retries

### expiring-quarantine
- **definition**: Quarantine temporarily removes a known flaky test from the blocking path while preserving its execution and visibility. Every quarantine record has an owner, issue, reason, expiry, and count/age limit so it cannot become a permanent skip list.
- **implementation**:
  - Encode metadata in a structured annotation or registry: test ID, owner, issue URL, reason, created-at, expires-at, and replacement plan.
  - Run quarantined tests in a separate non-blocking job and upload their artifacts and status.
  - Enforce maximum quarantine count and age in a CI policy check.
  - Require renewal or removal by the owner before expiry; expired entries fail CI.
- **probe**: `quarantine-lint --registry tests/quarantine.yml --max-count "$MAX_QUARANTINE" --max-age 14d`; verify each entry has owner/issue/expiry and fail on expired, over-age, or over-count entries. Confirm the separate job executes them.
- **failure_modes**: A skipped payment test remains disabled for months while the behavior regresses. An expired investigation silently persists because the runner treats every skip as ordinary metadata.
- **severity**: important
- **applies_if**: all
- **sources**: https://playwright.dev/docs/test-retries, https://martinfowler.com/articles/nonDeterminism.html

### controlled-clock
- **definition**: Time-dependent tests use an injected fake or monotonic clock and explicitly advance it rather than sleeping on wall time. This makes TTL, timer, scheduling, and DST behavior fast and deterministic.
- **implementation**:
  - Inject a `Clock`/time provider into production code and use a monotonic timer for elapsed durations.
  - Provide fixtures that freeze, set, and advance time, including timer-drain operations.
  - Test expiry, clock jumps, leap/DST boundaries, and cancellation without real sleeps.
  - Allow direct system-clock access only in an audited adapter or integration test.
- **probe**: `test-scan --patterns 'Date.now|time.time|sleep|setTimeout' tests/`; run time-sensitive targets with the controlled-clock fixture and fail uncatalogued wall-clock reads or real sleeps above the policy threshold.
- **failure_modes**: A TTL test intermittently expires early on a loaded runner. A scheduler test takes minutes and crosses a daylight-saving transition differently in CI and production.
- **severity**: important
- **applies_if**: all
- **sources**: https://martinfowler.com/articles/nonDeterminism.html, https://jestjs.io/docs/timer-mocks

### network-egress-block
- **definition**: Unit and integration test jobs deny real outbound network access and permit only declared interceptors or local ephemeral dependencies. Hermetic egress prevents DNS and third-party state from changing results and prevents accidental credential leakage.
- **implementation**:
  - Run test containers or CI jobs with network policy denying external egress by default.
  - Mock HTTP at the client boundary or provision pinned local service containers for integration behavior.
  - Fail unexpected sockets with destination, test ID, and call-site diagnostics.
  - Keep explicit contract/staging tests in a separately labeled job with scoped credentials and allowlist.
- **probe**: `network-policy deny-egress --job tests`; `test-runner`; assert the suite passes and the socket interceptor reports no undeclared destination. A real external request or test failure caused by denied egress is a finding.
- **failure_modes**: A test depends on a mutable SaaS response and fails during a provider outage. A fixture sends a CI token to a real endpoint because a mock was not registered.
- **severity**: critical
- **applies_if**: all
- **sources**: https://martinfowler.com/articles/nonDeterminism.html, https://bazel.build/basics/hermeticity

### reproducible-randomness
- **definition**: Randomized tests use an injected, recorded seed and preserve the generated case when they fail. Replaying the same seed must recreate the failure before shrinking or debugging changes the input.
- **implementation**:
  - Pass a seedable RNG or property-testing random source through production and test code.
  - Emit seed, generator version, test ID, and minimized case in CI output and artifacts.
  - Add a replay command that accepts the seed/case and runs one test deterministically.
  - Avoid ambient language RNGs and make parallel workers derive stable, distinct seeds.
- **probe**: `property-tests --seed 918273 --report failure.json`; on failure, run `property-tests --replay failure.json` and compare the counterexample. Scan for unseeded RNG use and fail missing seed metadata.
- **failure_modes**: A rare parser failure cannot be reproduced after CI cleans its workspace. Parallel tests share a global RNG and produce different failures on every rerun.
- **severity**: important
- **applies_if**: all
- **sources**: https://martinfowler.com/articles/nonDeterminism.html, https://hypothesis.readthedocs.io/en/latest/quick-start.html

### timezone-locale-matrix
- **definition**: CI fixes timezone, locale, calendar, encoding, newline, and collation for deterministic assertions while running a deliberately small matrix of supported variations. The matrix exposes user-visible and persisted-value differences without making every job combinatorial.
- **implementation**:
  - Set and log `TZ`, locale, encoding, line-ending, and collation settings in the test process.
  - Define representative UTC plus supported regional timezone/locale combinations in the CI matrix.
  - Assert canonical serialization and explicitly test localized display, sorting, parsing, and calendar boundaries.
  - Pin ICU/runtime data where output compatibility matters.
- **probe**: `test-env --print`; assert expected timezone/locale/encoding, then `ci-matrix-run --matrix test-matrix.yml --target format-and-parse-tests` and compare outputs against checked-in expected fixtures.
- **failure_modes**: Dates shift to the previous day for users west of UTC. Locale-dependent sorting changes authorization or billing order; newline/encoding differences corrupt an imported file.
- **severity**: important
- **applies_if**: all
- **sources**: https://martinfowler.com/articles/nonDeterminism.html

### hermetic-toolchain
- **definition**: A hermetic test toolchain resolves runtimes, compilers, packages, browsers, databases, and images from pinned versions or immutable digests. CI verifies those identities so a commit can be rerun with the same dependencies and tools.
- **implementation**:
  - Commit dependency lockfiles and pin language/runtime, browser, database, and container image versions.
  - Prefer image digests and checksum-verified downloads over floating tags or latest installers.
  - Capture toolchain manifests and checksums as CI artifacts.
  - Fail dependency refreshes unless they are an explicit, reviewed change.
- **probe**: `toolchain-audit --lockfiles --ci-config --dockerfiles --require-digests`; rerun the job and compare emitted toolchain manifest checksums, failing on floating references or mismatch.
- **failure_modes**: A browser auto-upgrade changes rendering and breaks a previously green suite. A mutable base image receives an incompatible compiler patch, making a release unreproducible.
- **severity**: critical
- **applies_if**: all
- **sources**: https://bazel.build/basics/hermeticity

### property-invariant-generators
- **definition**: Property-based tests generate broad valid and invalid inputs and shrink failures to minimal counterexamples. They are appropriate for parsers, serializers, state machines, and domain laws where examples cannot cover the input space.
- **implementation**:
  - Define generators from domain constraints rather than unconstrained random strings alone.
  - State invariants such as round-trip identity, conservation, ordering, idempotence, and legal state transitions.
  - Configure deterministic seeds, bounded cases, and shrinking; retain minimized examples as regression fixtures.
  - Review generator quality so it reaches boundary and malformed cases rather than producing only happy paths.
- **probe**: An assessor inspects critical parsers/serializers/state machines for explicit properties, generator constraints, boundary coverage, shrinking, and retained counterexamples; a property that only checks “does not throw” is insufficient evidence.
- **failure_modes**: A serializer mishandles nested empty values missed by hand-picked examples. A state transition accepts an impossible sequence because tests cover only nominal commands.
- **severity**: important
- **applies_if**: all
- **sources**: https://hypothesis.readthedocs.io/en/latest/quick-start.html, https://hackage.haskell.org/package/QuickCheck

### continuous-fuzz-targets
- **definition**: Fuzz targets continuously exercise parsers, protocol handlers, deserializers, and public input boundaries with malformed and stateful inputs. Pull requests get bounded smoke runs while scheduled campaigns run longer and preserve corpus and crash artifacts.
- **implementation**:
  - Register each target with a build command, input contract, timeout, memory limit, and owner.
  - Run a short deterministic budget on pull requests and longer jobs nightly or on release candidates.
  - Persist corpus, minimized crashes, hangs/timeouts, sanitizer output, and campaign metadata.
  - Monitor target execution and fail the scheduled job when targets disappear, crash, hang, or produce no expected artifacts.
- **probe**: `fuzz list --registered`; for each target run `fuzz run TARGET --time-limit "$PR_FUZZ_SECONDS" --artifacts out/TARGET`; assert zero crashes/timeouts and that scheduled jobs upload corpus and sanitizer artifacts.
- **failure_modes**: A malformed protocol frame crashes a service only after a rare byte sequence. A fuzzer hangs on a pathological input and consumes all scheduled runner capacity.
- **severity**: important
- **applies_if**: library
- **sources**: https://llvm.org/docs/LibFuzzer.html, https://google.github.io/oss-fuzz/

### fuzz-regression-seeds
- **definition**: Every minimized fuzz crash, timeout, and security finding becomes a deterministic regression test or seed-corpus entry. The regression remains runnable independently of whether a future campaign rediscovers the input.
- **implementation**:
  - Store minimized inputs with stable IDs, expected outcome, discovery issue, and target version.
  - Make CI execute the regression corpus before ordinary fuzzing and fail on crash, timeout, or changed expected safety behavior.
  - Keep corpus formats versioned and reject silently ignored or malformed seed files.
  - Link security findings to the test and preserve sensitive inputs under controlled access when necessary.
- **probe**: `fuzz regress --targets all --corpus fuzz/corpus --report regress.json`; enumerate recorded finding IDs and assert each maps to a runnable input and passes within its timeout.
- **failure_modes**: A fixed crash returns after a refactor because fuzzing does not happen to revisit its exact path. A timeout input is stored but excluded by a changed corpus glob.
- **severity**: important
- **applies_if**: all
- **sources**: https://llvm.org/docs/LibFuzzer.html, https://google.github.io/oss-fuzz/

### performance-thresholds
- **definition**: Performance thresholds turn user and capacity expectations into measurable budgets for latency, throughput, errors, startup, and resources. They apply to representative requests, jobs, queries, and cold-start paths and use percentile metrics rather than averages alone.
- **implementation**:
  - Select representative data volumes, concurrency, request mix, and warm/cold conditions.
  - Define p95/p99 latency, throughput, error-rate, CPU, memory, and startup thresholds per critical operation.
  - Version scenarios and thresholds with the service; compare candidate against baseline on stable runners.
  - Make threshold breaches block only where the owner has accepted the corresponding capacity or SLO risk.
- **probe**: User decision: “Which operations require performance gates, under what traffic/data profile, and what p95/p99, throughput, error, and resource limits apply?” Present options for no gate, SLO-derived gate, or explicit per-operation budgets and record the chosen thresholds.
- **failure_modes**: A query remains functionally correct but p99 latency breaches the customer SLO. A memory regression passes unit tests and causes workers to be OOM-killed under normal concurrency.
- **severity**: important
- **applies_if**: all
- **sources**: https://grafana.com/docs/k6/latest/using-k6/thresholds/

### scheduled-load-soak
- **definition**: Load, stress, spike, and soak tests exercise production-like traffic and data in an isolated environment on a schedule and before high-risk releases. They reveal saturation, leaks, queue buildup, autoscaling behavior, and long-lived state failures that short tests cannot.
- **implementation**:
  - Define scenarios for baseline load, ramp, spike, sustained soak, and recovery with representative traffic mix.
  - Run against production-like topology and safe synthetic data, with resource and dependency telemetry enabled.
  - Configure latency percentile, error, throughput, queue, CPU, memory, and recovery thresholds.
  - Schedule campaigns and record environment, scenario version, result, and artifacts for trend comparison.
- **probe**: `k6 run --out json=load.json scenarios/critical.js --env BASE_URL="$LOAD_URL"`; parse thresholds and resource telemetry, failing on configured breaches. Verify scheduled and pre-release workflows invoke the scenarios against an isolated target.
- **failure_modes**: A queue grows without bound after two hours even though a 10-minute test passes. Autoscaling lags a traffic spike and causes a customer-visible error storm.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://grafana.com/docs/k6/latest/using-k6/thresholds/, https://grafana.com/docs/k6/latest/using-k6/scenarios/

### benchmark-noise-control
- **definition**: Benchmark noise control makes performance comparisons statistically meaningful by controlling warmup, data, runner, sampling, and environmental variance. A gate compares distributions or confidence intervals, not one elapsed-time sample.
- **implementation**:
  - Use dedicated or otherwise stable runners, fixed datasets, pinned toolchains, and isolated background load.
  - Warm caches/JITs deliberately and separate cold-start measurements from steady-state runs.
  - Collect enough repeated samples, report median and percentile distributions, and compare against a stored baseline.
  - Set an allowable effect size and investigate variance before accepting a regression or improvement.
- **probe**: An assessor inspects benchmark scripts for fixed data, warmups, runner controls, repeated samples, environment metadata, and statistical comparison. One-shot timings or unrecorded environment changes are insufficient.
- **failure_modes**: A noisy shared runner causes false performance alarms and the team disables the gate. A real 20% regression is hidden by comparing a lucky fast sample with a slow baseline sample.
- **severity**: nice-to-have
- **applies_if**: all
- **sources**: https://grafana.com/docs/k6/latest/using-k6/thresholds/

### factory-based-fixtures
- **definition**: Fixture factories create minimal, immutable test data with explicit overrides so each test declares its dependencies. They prevent copy-pasted records, hidden shared mutation, and expensive setup from becoming correctness risks.
- **implementation**:
  - Provide builders/factories with valid defaults and named overrides for the fields relevant to a scenario.
  - Return fresh objects and records per invocation; freeze value objects where the language permits.
  - Keep fixture creation close to the test layer and use traits for distinct states instead of giant universal fixtures.
  - Delete or update factories with schema changes and make invalid combinations explicit.
- **probe**: An assessor samples fixtures for fresh-per-test allocation, minimal fields, explicit overrides, deterministic defaults, and absence of shared mutable records. Tests that depend on an opaque global fixture or mutate one used by others fail review.
- **failure_modes**: A copied fixture omits a newly required field and fails far from the schema change. A shared user record mutated by one test changes authorization outcomes in another.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.pytest.org/en/stable/how-to/fixtures.html, https://martinfowler.com/articles/practical-test-pyramid.html

### privacy-safe-test-data
- **definition**: Test data is synthetic or irreversibly de-identified, contains no production secrets, and is governed by retention and access controls. This entry merges into the broader non-production data-isolation control because safe fixtures must also be isolated from production systems and users.
- **implementation**:
  - Generate synthetic identities and domain records, or apply a documented irreversible de-identification method with re-identification risk review.
  - Scan fixtures, snapshots, logs, traces, reports, and uploaded artifacts for secrets and personal data before publication.
  - Use separate credentials, storage, encryption, access groups, and retention/expiry policies for test artifacts.
  - Block test jobs from production databases and prevent production exports from entering non-production without approval and sanitization.
- **probe**: An assessor inspects data lineage, generator or de-identification design, secret scans, artifact ACL/retention settings, and environment network/credential boundaries. Sample fixtures and CI artifacts must contain no reversible PII or live credentials.
- **failure_modes**: A CI screenshot or database dump exposes customer email addresses and tokens. A test accidentally writes destructive fixture data into production because environments share credentials and endpoints.
- **severity**: critical
- **applies_if**: all
- **merges_into**: nonprod-data-isolation
- **sources**: https://csrc.nist.gov/publications/detail/sp/800-122/final, https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts

### database-test-isolation
- **definition**: Database tests reset state per test or allocate an ephemeral database, schema, or namespace per worker, with deterministic seed and cleanup. Isolation prevents order-dependent rows, leaked schema changes, and cross-worker contamination.
- **implementation**:
  - Prefer a disposable container/database per suite or unique schema/tenant per worker, selected according to test cost and isolation needs.
  - Wrap compatible tests in transactions with rollback, but use real commit paths for transaction and migration behavior.
  - Seed deterministic baseline data and namespace all generated resources with run/worker IDs.
  - Run teardown in a finally path and provide an orphan cleanup job with bounded ownership labels.
- **probe**: `datastore provision --unique "$CI_RUN_ID"; test-runner --workers 4 --order random; datastore assert-empty --namespace "$CI_RUN_ID"; datastore destroy --unique "$CI_RUN_ID"` must pass twice with no cross-test failures or leftover rows/resources.
- **failure_modes**: A test passes alone but fails after another test inserts a conflicting row. Parallel workers overwrite one shared schema and intermittently observe each other's migrations.
- **severity**: important
- **applies_if**: all
- **sources**: https://testcontainers.com/, https://docs.pytest.org/en/stable/how-to/fixtures.html

### migration-upgrade-tests
- **definition**: Migration upgrade tests cover fresh installs, every supported schema upgrade, rollback or forward recovery, and safe old/new application ordering. This entry merges into the database-migrations control because deployment sequencing and recovery are part of migration correctness.
- **implementation**:
  - Materialize representative databases at every supported historical schema version, including realistic large and edge-case data.
  - Apply migrations with old application/new migration, new application/old schema, and the intended expand/contract ordering.
  - Exercise rollback where supported or verify forward recovery after an interrupted migration, with backup/restore checks where required.
  - Run post-migration invariants, compatibility queries, and performance/lock-budget checks before cleanup.
- **probe**: `for v in $(supported-schema-versions); do db-from-fixture "$v" "$DB_URL"; migrate up --db "$DB_URL"; app-compat-test --old "$OLD_APP" --new "$NEW_APP" --db "$DB_URL"; migration-recovery-test --db "$DB_URL"; done` should exit nonzero on data loss, invariant failure, incompatible ordering, or unrecoverable interruption.
- **failure_modes**: A migration succeeds on an empty database but fails on a large production index and holds locks during rollout. The new binary starts before a required column exists, or an interrupted migration leaves existing rows unreadable.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: db-migrations
- **sources**: https://documentation.red-gate.com/flyway/flyway-concepts/migrations

### required-merge-checks
- **definition**: Required merge checks protect the default branch with fresh, passing results for the exact commit and integration context being merged. Skipped, stale, failed, or bypassed required test jobs must make the change unmergeable.
- **implementation**:
  - Configure branch protection/rulesets with required status checks and restrict bypass to a documented emergency role.
  - Require checks to run on the merge commit or current head after the latest relevant change, not an obsolete branch commit.
  - Treat skipped required jobs, missing workflows, canceled jobs, and stale approvals as non-passing unless policy explicitly and safely handles them.
  - Audit branch rules and bypass events, and alert on configuration drift.
- **probe**: `repo-policy inspect --branch main --json`; `ci-status --commit "$MERGE_SHA" --required`; fail if any required test is absent, stale, skipped, failed, or mergeable through bypass. Verify a synthetic failed required check blocks merge in a protected test repository or policy simulation.
- **failure_modes**: A contributor updates code after CI and merges using an earlier green result. A workflow is skipped due to path filters while branch protection mistakenly considers the pull request green.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches

### impact-aware-sharding
- **definition**: Impact-aware sharding accelerates large-repository test execution using dependency-aware selection while retaining a safe broader-suite fallback. Selection must account for reverse dependencies through shared libraries, generated code, configuration, and test infrastructure.
- **implementation**:
  - Build and version a graph from targets to source files, libraries, generated outputs, and test suites.
  - Select affected tests plus declared reverse dependencies, then distribute selected targets by measured duration across workers.
  - Detect missing or stale graph data and run the broader suite rather than silently selecting nothing.
  - Periodically compare selected results with full-suite results to find under-selection and graph drift.
- **probe**: `impact graph validate`; `impact select --base "$BASE" --head HEAD --explain > selected.json`; assert every changed target's reverse dependencies are represented, and force a full-suite fallback when graph validation fails.
- **failure_modes**: A shared authentication library changes but naive changed-file selection runs only the edited package's tests. A stale graph omits generated client tests and a breaking API change reaches merge.
- **severity**: important
- **applies_if**: monorepo
- **sources**: https://bazel.build/query/guide, https://docs.github.com/en/actions/using-jobs/using-a-matrix-for-your-jobs

### nightly-full-suite
- **definition**: The nightly and release-candidate suite runs the complete deterministic test set, compatibility and environment matrices, and longer mutation/fuzz campaigns without changed-file filters. It catches interactions and platform regressions intentionally omitted from pull-request acceleration.
- **implementation**:
  - Define an explicit full-suite target that includes unit, integration, contract, E2E, mutation, fuzz-regression, and supported environment jobs.
  - Schedule it with durable artifacts, alerting, ownership, and retention sufficient for triage.
  - Run the same target on release candidates before promotion and block release on unresolved critical failures.
  - Record toolchain, matrix, commit, duration, and skipped-target metadata to detect incomplete runs.
- **probe**: `workflow inspect nightly release-candidate`; `full-suite --commit "$COMMIT" --no-changed-filter --report full.json`; verify every expected target is invoked and fail on missing, stale, or incomplete scheduled/release runs.
- **failure_modes**: Pull requests select only changed packages and miss a cross-service interaction. A platform-specific regression appears only on the release runner because the matrix was silently reduced.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule, https://docs.github.com/en/actions/using-jobs/using-a-matrix-for-your-jobs

### preview-environment-smoke
- **definition**: Each change can receive an isolated preview environment seeded only with non-sensitive data, where deployment wiring, contracts, and critical smoke journeys run against the real URL. The environment is destroyed after merge or closure; this entry merges into preview-environments.
- **implementation**:
  - Trigger a namespaced deployment from the pull request and expose its URL and commit identity to CI.
  - Provision isolated dependencies or safe service fakes, deterministic synthetic seed data, and least-privilege credentials.
  - Wait on a readiness contract, then run OpenAPI/Pact checks and a small critical smoke suite against the deployed artifact.
  - Attach logs/results to the pull request and enforce teardown on close/merge plus TTL cleanup for abandoned previews.
- **probe**: `preview create --pr "$PR" --commit "$GIT_SHA"`; `preview wait --url "$PREVIEW_URL" --ready`; `contract-test --base-url "$PREVIEW_URL"`; `smoke --base-url "$PREVIEW_URL"`; `preview assert-isolated --id "$PREVIEW_ID"`; `preview destroy --id "$PREVIEW_ID"`. Fail on readiness, contract, smoke, isolation, or cleanup failure.
- **failure_modes**: Mocks pass while the deployed service has a broken route, migration, or environment variable. Two pull requests share a preview database and one changes the other's smoke result; abandoned environments exhaust capacity.
- **severity**: important
- **applies_if**: all
- **merges_into**: preview-environments
- **sources**: https://docs.gitlab.com/ci/review_apps/

### failure-artifacts-and-triage
- **definition**: Every failed or flaky test preserves structured results and enough diagnostic context to reproduce it: logs, traces, screenshots/video, seeds, environment metadata, and minimized inputs as applicable. Artifacts are linked to a stable test ID and retained under an explicit access and retention policy.
- **implementation**:
  - Emit JUnit/JSON results with test ID, attempt, commit, worker, duration, and failure location.
  - Upload failure-only or bounded logs, browser traces/screenshots/video, core dumps, random seeds, fuzz inputs, and toolchain/environment manifests.
  - Preserve artifacts even when the test job fails, and link them from the CI summary and issue automation.
  - Redact secrets and PII before upload; set retention appropriate to debugging and compliance needs.
- **probe**: `ci run --target deliberately-failing-test`; `artifact inspect --run "$RUN_ID" --test-id failing-test`; assert JUnit/JSON plus configured diagnostics are attached, readable, linked to the test ID, redacted, and retained per policy.
- **failure_modes**: A browser race disappears on rerun because the original trace and screenshot were discarded. A fuzz crash cannot be reproduced because the seed and minimized input were absent from the failed job.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts, https://playwright.dev/docs/trace-viewer
