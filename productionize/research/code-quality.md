# Application code quality & API design — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 1). Raw item list, pre-synthesis.

### error-taxonomy
- **what**: Define a small stable taxonomy that separates expected client or domain failures, dependency failures, and programmer or invariant failures with machine-readable codes.
- **why**: Prevents callers from treating validation, transient infrastructure, and defects alike, causing unsafe retries and misleading responses.
- **check**: probe
- **probe**: Parse error declarations and transport mappings; assert distinct machine-readable categories for validation, domain, dependency, and internal failures.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### typed-domain-errors
- **what**: Use typed errors or discriminated result values at service boundaries and map them explicitly to transport responses.
- **why**: Prevents brittle string matching and accidental conversion of actionable failures into generic or successful results.
- **check**: probe
- **probe**: Run a static AST check over exported service functions; require a typed result or declared error type and reject string-only error comparisons.
- **applies_if**: all
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### error-cause-preservation
- **what**: Wrap lower-level failures with operation context while preserving the original cause, stack, retryability, and safe classification.
- **why**: Prevents incident responders from losing the root cause when errors cross repository, transport, or queue boundaries.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### exception-boundaries
- **what**: Catch exceptions only at intentional request, worker, CLI, or process boundaries and translate them once.
- **why**: Prevents duplicate logs, inconsistent status mapping, and lower layers silently deciding how callers should recover.
- **check**: probe
- **probe**: Enumerate catch or exception handlers and assert each either rethrows or wraps with cause or returns a documented boundary response; flag catches inside domain code.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html

### no-swallowed-exceptions
- **what**: Prohibit empty catches and catches that only log before returning success, a default value, or a partially applied result.
- **why**: Prevents silent data loss, false acknowledgements, and corrupted downstream state.
- **check**: probe
- **probe**: AST-scan catch blocks and fail on empty bodies, bare returns, default-value returns, or log-only handling without a rethrow or explicit error result.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html

### safe-error-disclosure
- **what**: Return stable client-safe errors without stacks, SQL, credentials, or internal paths while retaining a correlation identifier for internal diagnostics.
- **why**: Prevents information disclosure while preserving enough linkage to investigate the failed operation.
- **check**: probe
- **probe**: Exercise representative 4xx and 5xx responses and assert no stack, SQL, credential, or filesystem-path patterns while internal logs contain the returned correlation identifier.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### ingress-validation
- **what**: Validate every external input at its ingress boundary for required fields, types, ranges, formats, and allowed values before business logic runs.
- **why**: Prevents malformed or hostile data from reaching trusted code paths and causing crashes, injections, or invariant violations.
- **check**: probe
- **probe**: Enumerate HTTP, CLI, queue, webhook, and file handlers and assert each parses a schema before invoking application services; run missing, wrong-type, and out-of-range fixtures.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html; https://json-schema.org/specification

### schema-size-limits
- **what**: Enforce body, string, array, nesting, upload, and batch-size limits before expensive parsing or allocation.
- **why**: Prevents resource-exhaustion attacks and parser failures from oversized but syntactically valid input.
- **check**: probe
- **probe**: Send payloads just above each configured body, field, collection, and upload limit and assert rejection before handler work with the documented 4xx status.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html; https://owasp.org/API-Security/editions/2023/en/0x00-header/

### canonicalize-once
- **what**: Canonicalize encodings, paths, identifiers, and Unicode representations once before validation and authorization, then pass canonical values inward.
- **why**: Prevents alternate representations from bypassing checks or creating duplicate records for the same logical resource.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html; https://owasp.org/API-Security/editions/2023/en/0x00-header/

### twelve-factor-config
- **what**: Load deploy-varying configuration from environment or an approved external configuration system rather than checked-in runtime files or source literals.
- **why**: Prevents environment drift, accidental production defaults, and credentials or operational settings being committed with code.
- **check**: probe
- **probe**: Parse deployment manifests and config loaders; fail if deploy-varying values or credentials are literal constants outside test fixtures or if checked-in config is mutable runtime state.
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/config; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### boot-config-validation
- **what**: Validate required environment variables, types, ranges, URLs, credentials, and mutually exclusive settings before accepting work.
- **why**: Prevents a misconfigured process from serving partial traffic and failing unpredictably deep in a request or job.
- **check**: probe
- **probe**: Launch the application with each required variable absent and with representative invalid values, then assert nonzero exit before it listens or consumes work.
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/config; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### explicit-config-defaults
- **what**: Document every configuration default and forbid silent fallback for security, identity, persistence, billing, or data-loss settings.
- **why**: Prevents a missing variable from quietly selecting an unsafe tenant, database, auth mode, or destructive behavior.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/config; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### secret-storage
- **what**: Store secrets only in an approved secret manager or injected runtime secret and grant the narrowest identity, scope, and lifetime that works.
- **why**: Prevents credentials from spreading through source, images, artifacts, developer machines, or overprivileged workloads.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html; https://csrc.nist.gov/Projects/ssdf

### secret-history-scanning
- **what**: Scan the working tree, full repository history, generated artifacts, CI logs, and release bundles for secrets and revoke any confirmed exposure.
- **why**: Prevents a credential committed months ago or emitted in a build artifact from remaining usable after the visible line is removed.
- **check**: probe
- **probe**: Run `gitleaks detect --redact --source . --log-opts=--all` plus artifact and CI-log scans, fail on verified findings, and require revocation records for historical hits.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### secret-redaction
- **what**: Redact tokens, passwords, API keys, personal data, and signed URLs from logs, errors, traces, dumps, metrics labels, and request URLs.
- **why**: Prevents observability and support systems from becoming durable credential-exfiltration stores.
- **check**: probe
- **probe**: Inject fixture secrets through each logging, tracing, error, and URL path and assert the exact values and recognizable token forms never occur in captured output.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html; https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html

### lockfile-reproducibility
- **what**: Commit one authoritative lockfile per package manager and install from it in frozen or immutable mode in CI and release builds.
- **why**: Prevents developers and deploys from resolving different transitive graphs for the same source revision.
- **check**: probe
- **probe**: Detect the package manager from manifests, require its lockfile in version control, and run a clean temporary-directory frozen install twice with identical dependency checksums.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html; https://csrc.nist.gov/Projects/ssdf

### immutable-dependency-pins
- **what**: Pin release dependencies to reviewed immutable versions and pin CI or container actions to immutable digests where the ecosystem supports it.
- **why**: Prevents a floating range, tag, or mutable action from changing production code without a corresponding review.
- **check**: probe
- **probe**: Parse manifests, lockfiles, Dockerfiles, and CI workflows and fail on unbounded ranges, floating tags, or unpinned action references except documented development-only cases.
- **applies_if**: all
- **severity**: important
- **sources**: https://scorecard.dev/; https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html

### dependency-update-sla
- **what**: Automate dependency advisories and define owner, severity-based remediation deadlines, and expiry dates for accepted exceptions.
- **why**: Prevents known vulnerable or obsolete packages from remaining indefinitely because no one owns the update.
- **check**: probe
- **probe**: Parse Dependabot or Renovate configuration and vulnerability reports; assert every production dependency has an owner and every open finding is within its severity SLA or has an unexpired exception.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/about-dependabot-security-updates; https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html

### dependency-provenance-sbom
- **what**: Record dependency checksums, provenance, and a release SBOM and review unexpected direct or transitive additions.
- **why**: Prevents a compromised mirror, typosquat, or unreviewed transitive package from entering the shipped artifact unnoticed.
- **check**: probe
- **probe**: Generate an SPDX or CycloneDX SBOM from the lockfile, compare it with the previous release, and fail on additions without an owner or provenance evidence.
- **applies_if**: all
- **severity**: important
- **sources**: https://csrc.nist.gov/Projects/ssdf; https://scorecard.dev/

### license-policy
- **what**: Inventory direct and transitive dependency licenses and enforce an approved policy with required notices and attribution in shipped artifacts.
- **why**: Prevents release-time legal violations from an overlooked transitive copyleft or missing attribution obligation.
- **check**: probe
- **probe**: Generate a lockfile-derived SPDX license report and fail CI for disallowed, unknown, or missing licenses and for absent required notice files.
- **applies_if**: all
- **severity**: important
- **sources**: https://spdx.dev/specifications/; https://www.apache.org/legal/resolved.html

### dependency-abandonment-risk
- **what**: Assess maintainer activity, release health, bus factor, issue responsiveness, takeover signals, and a replacement plan for every critical dependency.
- **why**: Prevents an unmaintained or compromised package from becoming an unplanned single point of failure.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://scorecard.dev/; https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html

### api-version-compatibility
- **what**: Publish an explicit API versioning and compatibility policy and preserve old behavior until a documented retirement date.
- **why**: Prevents clients from breaking when a server treats a removal or semantic change as a harmless evolution.
- **check**: user-decision
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://google.aip.dev/185; https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md

### api-schema-compatibility
- **what**: Maintain a machine-readable OpenAPI or equivalent contract and run backward-compatibility checks for every published API change.
- **why**: Prevents accidental changes to field types, requiredness, enum values, headers, or status codes that generated and existing clients cannot handle.
- **check**: probe
- **probe**: Parse the current and previous API schemas with a compatibility checker and fail CI on removed or tightened request fields, changed response types, or incompatible status and header changes.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://spec.openapis.org/oas/latest.html; https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md

### bounded-pagination
- **what**: Define bounded pagination with deterministic ordering, validated limits, opaque cursors or explicit offsets, and documented continuation links or metadata.
- **why**: Prevents unbounded scans, timeouts, duplicate or missing records, and clients that cannot safely resume enumeration.
- **check**: probe
- **probe**: Request the default, maximum, zero, negative, oversized, and malformed page parameters while records change between pages; assert bounded work, deterministic ordering, and valid next or previous links.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc8288; https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md

### problem-details-envelope
- **what**: Use `application/problem+json` with stable type, title, status, detail, and instance members plus a documented field-error extension.
- **why**: Prevents every client from parsing ad hoc messages and gives operators a stable identifier for a failure class.
- **check**: probe
- **probe**: Trigger representative validation, authorization, not-found, conflict, rate-limit, and server failures and validate content type, required Problem Details members, stable type, and field-error shape.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9457

### status-retry-contract
- **what**: Document status-code and retryability semantics for each failure class and include `Retry-After` when a client should wait before retrying.
- **why**: Prevents clients from retrying permanent errors or amplifying transient overload with uncontrolled retries.
- **check**: probe
- **probe**: Invoke representative failure fixtures and compare status, Problem Details type, and `Retry-After` behavior against the published retry matrix.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9110; https://www.rfc-editor.org/rfc/rfc6585

### rate-limit-headers
- **what**: Enforce documented quota scope and window and emit `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` with a defined 429 response.
- **why**: Prevents clients from overloading the service because they cannot calculate when or how aggressively to back off.
- **check**: probe
- **probe**: Send requests past each documented quota from one identity and across identities, then parse the three RateLimit fields, 429 status, and reset timing for consistency.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9458; https://www.rfc-editor.org/rfc/rfc6585

### deprecation-lifecycle
- **what**: Announce deprecated operations with a replacement, effective date, migration guide, and removal criteria while emitting standardized deprecation or sunset signals.
- **why**: Prevents clients from discovering removal only after an outage and gives owners evidence that migration is progressing.
- **check**: probe
- **probe**: Invoke every listed deprecated route or field and assert the documented `Deprecation` and `Sunset` headers, replacement link, and date agree with the API catalog.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9745; https://www.rfc-editor.org/rfc/rfc8594

### mutation-idempotency-key
- **what**: Require an idempotency key for externally retryable non-idempotent mutations and bind it to the authenticated principal, operation, and request fingerprint.
- **why**: Prevents timeouts and client retries from duplicating charges, jobs, messages, or records.
- **check**: probe
- **probe**: Submit the same mutation repeatedly with one key, with the same key and a changed payload, and with different principals; assert one side effect, replayed result, and deterministic conflict isolation.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.stripe.com/api/idempotent_requests; https://www.rfc-editor.org/rfc/rfc9110

### idempotency-atomic-replay
- **what**: Persist in-flight and completed idempotency state atomically with the mutation result, define key expiry, and return a deterministic conflict for unsafe key reuse.
- **why**: Prevents concurrent duplicate requests from racing before the key is recorded or replaying an incomplete outcome.
- **check**: probe
- **probe**: Fire concurrent identical requests and crash or interrupt between reservation and commit; assert a unique key constraint, one committed effect, and a documented replay or retry result after recovery.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://docs.stripe.com/api/idempotent_requests; https://www.rfc-editor.org/rfc/rfc9110

### mutation-atomicity
- **what**: Make a mutation's state changes and side-effect intent atomic through a transaction, transactional outbox, or an equivalent durable protocol.
- **why**: Prevents partial commits that leave state changed without its event, notification, charge, or downstream work.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html; https://cwe.mitre.org/data/definitions/662.html

### shared-state-synchronization
- **what**: Identify every shared mutable state location and protect it with a transaction, lock, atomic operation, compare-and-swap, or explicit ownership model.
- **why**: Prevents lost updates, corrupted caches, and nondeterministic results caused by unsynchronized access.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://cwe.mitre.org/data/definitions/362.html; https://cwe.mitre.org/data/definitions/662.html

### optimistic-concurrency
- **what**: Expose a version or ETag and reject stale writes with compare-and-swap semantics instead of silently using last-write-wins.
- **why**: Prevents one concurrent editor or worker from overwriting another's committed change without detection.
- **check**: probe
- **probe**: Read one resource twice, submit two updates using the same version or ETag, and assert exactly one succeeds while the other receives the documented conflict or precondition failure.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9110; https://cwe.mitre.org/data/definitions/362.html

### race-stress-detection
- **what**: Run race detectors, stress tests, and controlled-schedule tests around shared state and make failures reproducible with deterministic seeds.
- **why**: Prevents ordinary serial tests from certifying timing-dependent corruption that appears only under load.
- **check**: probe
- **probe**: Execute the language's race detector or equivalent stress harness repeatedly with controlled scheduling and fail on race reports, invariant violations, or nondeterministic final state.
- **applies_if**: all
- **severity**: important
- **sources**: https://cwe.mitre.org/data/definitions/362.html; https://cwe.mitre.org/data/definitions/662.html

### distributed-lock-fencing
- **what**: Give distributed critical sections explicit ownership, lease expiry, fencing tokens, and recovery behavior rather than relying on an unbounded lock.
- **why**: Prevents process death, delayed packets, or clock assumptions from causing duplicate workers or permanent deadlock.
- **check**: judgment
- **applies_if**: data-pipeline
- **severity**: important
- **sources**: https://cwe.mitre.org/data/definitions/662.html; https://cwe.mitre.org/data/definitions/367.html
