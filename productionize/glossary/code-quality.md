# Application code quality & API design glossary

### error-taxonomy
- **definition**: Define a small, stable taxonomy that distinguishes expected client or domain failures from dependency failures and programmer or invariant failures. Each category has a machine-readable code and an explicit transport and retry meaning.
- **implementation**:
  - Define an enum or registry for `validation`, `domain`, `dependency`, and `internal` categories with stable codes.
  - Attach category, public status, retryability, and correlation ID to every boundary error.
  - Map domain errors to transport responses in one adapter rather than in repositories or clients.
  - Version codes deliberately and keep an error catalog reviewed with the API contract.
- **probe**: Parse error declarations and transport mappings; assert distinct machine-readable categories exist for validation, domain, dependency, and internal failures, and that each exported code maps to exactly one public status and retry policy.
- **failure_modes**: A validation error was retried until a client was throttled; a dependency outage was returned as a successful empty result; an invariant defect was mislabeled as a user error and went unalerted.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### typed-domain-errors
- **definition**: Use typed errors or discriminated result values at service boundaries and map them explicitly to transport responses. Callers should branch on a stable type or code, never on message text.
- **implementation**:
  - Define a discriminated union or typed exception hierarchy for each service's expected failures.
  - Require exported service methods to declare their result and error contract in types or interface documentation.
  - Convert typed errors to Problem Details or equivalent responses in the transport adapter.
  - Preserve a generic internal branch for unexpected failures without exposing implementation details.
- **probe**: Run a static AST check over exported service functions; require a typed result or declared error type, flag string-only error comparisons, and verify each declared variant has an explicit transport mapping.
- **failure_modes**: A wording change broke a client that matched an error string; an authorization failure became a generic 500 because an untyped exception crossed the boundary; a missing error branch was treated as success by a caller.
- **severity**: important
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### error-cause-preservation
- **definition**: Wrap lower-level failures with operation context while preserving the original cause, stack, retryability, and safe classification. The resulting error should support both a useful public mapping and an actionable internal causal chain.
- **implementation**:
  - Use language-native cause chaining (`cause`, exception chaining, or equivalent) whenever adding context.
  - Carry structured operation, dependency, and retryability fields separately from the human-readable message.
  - Sanitize only at the outward transport boundary; retain the uncensored cause in access-controlled diagnostics.
  - Test that queue, repository, and HTTP adapters preserve causal identity through each wrapping layer.
- **probe**: The assessor must inspect representative repository, dependency, queue, and transport wrappers and confirm that each adds operation context, retains the original cause and stack, preserves retry classification, and sanitizes only at the public boundary.
- **failure_modes**: An outage report showed only “request failed” after a repository wrapper discarded the database error; operators retried a permanent schema error because retryability was lost; a stack was logged without the operation that identified the affected tenant.
- **severity**: important
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### exception-boundaries
- **definition**: Catch exceptions only at intentional request, worker, CLI, or process boundaries and translate them once. Lower layers either recover a known local condition or propagate a typed/cause-preserving failure.
- **implementation**:
  - Mark request, job, command, and process entrypoints as the approved exception translation boundaries.
  - Let repositories and domain services rethrow or return typed errors instead of choosing HTTP or exit semantics.
  - At each boundary, emit one structured diagnostic and one documented response or exit result.
  - Treat cancellation, shutdown, and fatal programmer errors distinctly from recoverable failures.
- **probe**: Enumerate catch or exception handlers and assert each either rethrows or wraps with a cause or returns a documented boundary response; flag catches inside domain code that translate to transport-specific behavior.
- **failure_modes**: Nested handlers logged one database failure five times and obscured the original event; a repository returned HTTP 404 semantics to a batch worker; a worker swallowed cancellation while draining and exceeded its shutdown deadline.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html

### no-swallowed-exceptions
- **definition**: Prohibit empty catches and catches that only log before returning success, a default value, or a partially applied result. Every caught failure must be rethrown, represented in an explicit error result, or justified as a documented best-effort operation.
- **implementation**:
  - Make static analysis reject empty catches, bare returns, and default-value returns in catch blocks.
  - Require best-effort handlers to record an explicit outcome and state what data loss is acceptable.
  - Keep logging structured and paired with a propagated failure when the operation is not best effort.
  - Add negative tests for malformed input, dependency exceptions, and cancellation paths.
- **probe**: AST-scan catch blocks and fail on empty bodies, bare/default-value returns, or log-only handling without a rethrow or explicit error result; permit only annotated, documented best-effort handlers.
- **failure_modes**: A failed notification was acknowledged as sent; a cache read exception returned an empty permissions set; a partial batch was committed after one item failed and downstream state diverged.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html

### safe-error-disclosure
- **definition**: Return stable client-safe errors without stacks, SQL, credentials, or internal paths while retaining a correlation identifier for internal diagnostics. Public details describe the failure class and next action, not implementation internals.
- **implementation**:
  - Centralize public error serialization and use an allowlist of fields.
  - Generate or propagate a request/correlation ID and include it in the response and structured logs.
  - Redact exception messages, query text, file paths, tokens, and stack traces from 4xx/5xx bodies.
  - Add response-contract checks for representative validation, authorization, and server failures.
- **probe**: Exercise representative 4xx and 5xx responses and assert no stack, SQL, credential, or filesystem-path patterns occur; then verify internal logs contain the returned correlation identifier without exposing secrets.
- **failure_modes**: A SQL error disclosed table names useful to an attacker; a 500 response exposed a cloud credential from an exception; support could not correlate a generic client error to the internal incident.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html; https://www.rfc-editor.org/rfc/rfc9457

### ingress-validation
- **definition**: Validate every external input at its ingress boundary for required fields, types, ranges, formats, and allowed values before business logic runs. Treat HTTP, CLI, queue, webhook, and file payloads as untrusted until schema validation succeeds.
- **implementation**:
  - Define versioned schemas for every public request, message, webhook, and import format.
  - Reject unknown or unsafe fields according to the endpoint's documented compatibility policy.
  - Validate types, bounds, formats, encodings, and cross-field constraints before invoking services.
  - Return structured field errors and never pass raw parsed input into persistence or command execution.
- **probe**: Enumerate HTTP, CLI, queue, webhook, and file handlers and assert each parses a schema before invoking application services; run missing-field, wrong-type, out-of-range, malformed-encoding, and unknown-field fixtures.
- **failure_modes**: A malformed webhook crashed a worker before acknowledgement; an unchecked field enabled an injection path; an out-of-range batch size exhausted memory and took down the process.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html; https://json-schema.org/specification

### schema-size-limits
- **definition**: Enforce body, string, array, nesting, upload, and batch-size limits before expensive parsing or allocation. Limits must be explicit per ingress and enforced consistently at the edge and application parser.
- **implementation**:
  - Set proxy/server maximum body and upload sizes before buffering request bodies.
  - Configure parser limits for string length, collection cardinality, nesting depth, and decompressed size.
  - Bound queue batches, pagination parameters, file records, and fan-out operations independently.
  - Return a documented 4xx response and metric when a limit is exceeded.
- **probe**: Send payloads just above each configured body, field, collection, nesting, decompression, and upload limit; assert rejection before handler work with the documented 4xx status and no unbounded allocation.
- **failure_modes**: A deeply nested JSON document exhausted parser stack; a compressed upload expanded to fill disk; an oversized batch monopolized workers and caused request timeouts.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html; https://owasp.org/API-Security/editions/2023/en/0x00-header/

### canonicalize-once
- **definition**: Canonicalize encodings, paths, identifiers, and Unicode representations once before validation and authorization, then pass canonical values inward. Do not repeatedly normalize values differently across layers.
- **implementation**:
  - Establish one canonicalization routine for each identifier, path, and encoding accepted at ingress.
  - Normalize Unicode and percent/path encodings before allowlist, tenant, and authorization checks.
  - Store and compare canonical identifiers while retaining the original only when audit requirements demand it.
  - Make downstream services consume the validated canonical form rather than reinterpreting raw input.
- **probe**: The assessor must inspect ingress, authorization, routing, and persistence paths for a single documented normalization point and verify that alternate encodings, Unicode forms, case variants, and traversal representations cannot produce divergent authorization or identity results.
- **failure_modes**: A path traversal variant bypassed a prefix check; two Unicode spellings created duplicate accounts; authorization checked a decoded identifier while storage used the encoded one.
- **severity**: important
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html; https://owasp.org/API-Security/editions/2023/en/0x00-header/

### twelve-factor-config
- **definition**: Load deploy-varying configuration from environment or an approved external configuration system rather than checked-in runtime files or source literals. Code remains identical across environments while configuration is supplied at deploy time.
- **implementation**:
  - Define a typed configuration loader with an inventory of required and optional settings.
  - Inject environment-specific values through the platform secret/config mechanism, not source or images.
  - Keep checked-in files limited to non-sensitive, immutable defaults and schema/documentation.
  - Prevent runtime mutation of deployment configuration except through an audited control plane.
- **probe**: Parse deployment manifests and config loaders; fail if deploy-varying values or credentials are literal constants outside test fixtures, or if checked-in config is mutable runtime state rather than injected configuration.
- **failure_modes**: A production image used a development database URL; a committed credential was copied into every environment; changing a setting required rebuilding code and caused configuration drift.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/config; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### boot-config-validation
- **definition**: Validate required environment variables, types, ranges, URLs, credentials, and mutually exclusive settings before accepting work. A process with invalid configuration must fail closed before listening or consuming jobs.
- **implementation**:
  - Parse all configuration through a startup schema with type, range, URL, and enum checks.
  - Check required credentials and connectivity prerequisites without logging their values.
  - Reject contradictory combinations such as incompatible auth, storage, or mode settings.
  - Run validation before opening listeners, registering consumers, or advertising readiness.
- **probe**: Launch the application with each required variable absent and with representative invalid values; assert nonzero exit before it listens or consumes work, and verify diagnostics identify the setting without printing its secret value.
- **failure_modes**: A worker consumed jobs with an empty encryption key; a server accepted traffic using a malformed callback URL; a missing tenant setting silently routed writes to the wrong database.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/config; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### explicit-config-defaults
- **definition**: Document every configuration default and forbid silent fallback for security, identity, persistence, billing, or data-loss settings. Defaults that can change trust, cost, or durability require an explicit operator decision.
- **implementation**:
  - Maintain a configuration reference listing type, source, default, risk, and owner for every setting.
  - Make sensitive settings fail closed when absent instead of selecting an implicit mode.
  - Require explicit opt-in for destructive, billable, cross-tenant, or insecure behavior.
  - Emit the effective non-secret configuration at startup for audit and troubleshooting.
- **probe**: Present this exact question for each security, identity, persistence, billing, or data-loss setting: “When this setting is absent, which behavior is approved?” Options: “fail startup,” “use the documented safe default,” or “use an explicitly approved fallback with owner and expiry.” Record the selected option and evidence of review.
- **failure_modes**: A missing auth flag enabled anonymous access; a missing storage setting selected ephemeral disk and lost data; an absent billing mode silently enabled an expensive provider.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/config; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### secret-storage
- **definition**: Store secrets only in an approved secret manager or injected runtime secret and grant the narrowest identity, scope, and lifetime that works. Secrets must not be embedded in source, images, artifacts, or broad shared configuration.
- **implementation**:
  - Use a managed vault/KMS or platform secret injection with encryption at rest and access audit logs.
  - Bind each workload to a distinct identity with least-privilege secret paths and operations.
  - Inject secrets at runtime and keep them out of command lines, build layers, and source-controlled files.
  - Define rotation, revocation, expiry, and break-glass procedures with owners.
- **probe**: The assessor must inspect secret-manager configuration, workload identities, IAM scopes, deployment manifests, image layers, and runtime injection; confirm no plaintext secret is persisted outside approved stores and that access, rotation, and revocation are auditable.
- **failure_modes**: A shared CI credential granted production-wide access; a container layer retained a deleted API key; a developer copied a long-lived database password into a checked-in config file.
- **severity**: critical
- **applies_if**: all
- **merges_into**: secrets-management
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html; https://csrc.nist.gov/Projects/ssdf

### secret-history-scanning
- **definition**: Scan the working tree, full repository history, generated artifacts, CI logs, and release bundles for secrets, then revoke any confirmed exposure. Removing a secret from the latest commit does not make the old value safe.
- **implementation**:
  - Run a detector such as Gitleaks with full-history and redacted output in CI and release workflows.
  - Scan packaged artifacts, container layers, build logs, caches, and generated files before publication.
  - Route verified findings to the credential owner and record revocation, replacement, and incident evidence.
  - Permit suppressions only with an expiry, rationale, and proof that the value is non-secret.
- **probe**: Run `gitleaks detect --redact --source . --log-opts=--all` plus artifact and CI-log scans; fail on verified findings and require revocation records for historical hits. Check that detectors inspect all release bundle and image layers.
- **failure_modes**: A key deleted from the current branch remained usable in Git history; a build log exposed a token to every CI reader; an old image layer was pulled during rollback and restored a compromised credential.
- **severity**: critical
- **applies_if**: all
- **merges_into**: secrets-management
- **sources**: https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### secret-redaction
- **definition**: Redact tokens, passwords, API keys, personal data, and signed URLs from logs, errors, traces, dumps, metrics labels, and request URLs. Redaction must happen before data leaves the process and must cover structured and unstructured paths.
- **implementation**:
  - Use centralized structured-log and telemetry processors with field and pattern allowlists.
  - Remove query parameters and authorization material from URLs before recording requests.
  - Scrub exception messages, serialized payloads, stack dumps, trace attributes, and metric labels.
  - Inject fixture values in each sink path and review exported samples for exact and recognizable forms.
- **probe**: Inject fixture secrets through each logging, tracing, error, dump, metrics, and URL path; assert exact values and recognizable token forms never occur in captured output, including serialized nested fields.
- **failure_modes**: A signed download URL in access logs let anyone replay a private file; a bearer token in a trace attribute persisted in an observability vendor; a password appeared in a crash dump shared with support.
- **severity**: critical
- **applies_if**: all
- **merges_into**: secrets-management
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html; https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html

### lockfile-reproducibility
- **definition**: Commit one authoritative lockfile per package manager and install from it in frozen or immutable mode in CI and release builds. Repeated clean installs from the same revision must resolve the same dependency graph and checksums.
- **implementation**:
  - Detect and document the repository's single package manager and canonical lockfile.
  - Use frozen/immutable install flags in CI, release, and container build stages.
  - Verify lockfile integrity and fail when manifests and lockfiles diverge.
  - Keep dependency caches keyed by lockfile content and toolchain version.
- **probe**: Detect the package manager from manifests, require its lockfile in version control, and run a clean temporary-directory frozen install twice; compare dependency checksums and resolved graphs for identity.
- **failure_modes**: A mutable range pulled a vulnerable patch only during release; developer and CI installed different transitive versions; a missing lockfile made rollback impossible to reproduce.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html; https://csrc.nist.gov/Projects/ssdf

### immutable-dependency-pins
- **definition**: Pin release dependencies to reviewed immutable versions and pin CI or container actions to immutable digests where the ecosystem supports it. Mutable tags and broad ranges must be explicit, constrained exceptions rather than defaults.
- **implementation**:
  - Use exact versions and lockfile checksums for production dependencies.
  - Pin container base images and CI actions to content digests.
  - Separate development-only floating tools from the release dependency graph.
  - Require owner, rationale, and expiry for any unavoidable mutable reference.
- **probe**: Parse manifests, lockfiles, Dockerfiles, and CI workflows; fail on unbounded ranges, floating tags, or unpinned action references except documented development-only cases.
- **failure_modes**: A retagged base image introduced a vulnerable system library; a CI action changed behavior without a source review; a dependency range resolved an incompatible major release during deployment.
- **severity**: important
- **applies_if**: all
- **sources**: https://scorecard.dev/; https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html

### dependency-update-sla
- **definition**: Automate dependency advisories and define an owner, severity-based remediation deadline, and expiry date for every accepted exception. Vulnerability response is an owned operational process, not an ad hoc upgrade.
- **implementation**:
  - Enable Dependabot, Renovate, or equivalent vulnerability and update automation.
  - Tag every production dependency with an owning team and escalation path.
  - Set deadlines by severity and block release when an overdue finding lacks an approved exception.
  - Record exception rationale, compensating controls, and expiration in version control.
- **probe**: Parse Dependabot/Renovate configuration and vulnerability reports; assert every production dependency has an owner and every open finding is within its severity SLA or has an unexpired exception.
- **failure_modes**: A critical library remained vulnerable because alerts went to a departed maintainer; an accepted exception outlived its compensating control; a transitive issue was ignored because no team owned it.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/about-dependabot-security-updates; https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html

### dependency-provenance-sbom
- **definition**: Record dependency checksums and provenance, publish an SBOM for each release, and review unexpected direct or transitive additions. The release inventory must be derived from the exact lockfile and artifact that shipped.
- **implementation**:
  - Generate SPDX or CycloneDX from the locked dependency graph during the release build.
  - Attach component versions, hashes, source references, and build identity to the signed release metadata.
  - Diff each release SBOM against the previous version and route additions to owners.
  - Retain provenance and SBOM artifacts with the release for incident response.
- **probe**: Generate an SPDX or CycloneDX SBOM from the lockfile, compare it with the previous release, and fail on additions without an owner or provenance evidence; verify listed hashes match the built artifact inputs.
- **failure_modes**: A typosquatted transitive package entered the release unnoticed; responders could not identify affected versions after a supply-chain advisory; a build used a dependency absent from the published inventory.
- **severity**: important
- **applies_if**: all
- **merges_into**: sbom-provenance
- **sources**: https://csrc.nist.gov/Projects/ssdf; https://scorecard.dev/

### license-policy
- **definition**: Inventory direct and transitive dependency licenses and enforce an approved policy with required notices and attribution in shipped artifacts. Unknown or changed licensing must block release until reviewed.
- **implementation**:
  - Generate a lockfile-derived SPDX license report for every release candidate.
  - Maintain an approved, conditional, and prohibited license policy with legal owners.
  - Fail CI on disallowed or unknown licenses and on missing required notice files.
  - Bundle attribution and notices in the distributions that include the dependency.
- **probe**: Generate a lockfile-derived SPDX license report and fail CI for disallowed, unknown, or missing licenses and for absent required notice files; compare the report with the shipped artifact contents.
- **failure_modes**: A transitive copyleft package created unplanned distribution obligations; a missing attribution caused a release takedown; a package changed license and no automated check detected it.
- **severity**: important
- **applies_if**: all
- **sources**: https://spdx.dev/specifications/; https://www.apache.org/legal/resolved.html

### dependency-abandonment-risk
- **definition**: Assess maintainer activity, release health, bus factor, issue responsiveness, takeover signals, and a replacement plan for every critical dependency. Dependency approval includes operational continuity, not only current vulnerability status.
- **implementation**:
  - Maintain an inventory that marks critical dependencies, owners, and business capabilities they support.
  - Review release cadence, maintainer identity, repository controls, open issues, and security response history.
  - Identify an alternative, fork, or internal replacement for each single-source critical dependency.
  - Reassess risk on major releases, ownership changes, suspicious publication events, and scheduled intervals.
- **probe**: The assessor must inspect the critical-dependency inventory, recent releases and issue response, maintainer and takeover signals, bus-factor evidence, and a tested replacement plan; record risk acceptance for unresolved items.
- **failure_modes**: An abandoned parser blocked a runtime upgrade; a compromised maintainer published a malicious update; a single maintainer's account loss delayed a security fix for weeks.
- **severity**: important
- **applies_if**: all
- **sources**: https://scorecard.dev/; https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html

### api-version-compatibility
- **definition**: Publish an explicit API versioning and compatibility policy and preserve old behavior until a documented retirement date. Versioning covers URL or media-type strategy, semantic compatibility, support duration, and migration ownership.
- **implementation**:
  - Choose and document URI, header/media-type, or equivalent version negotiation.
  - Define additive, breaking, and behavioral changes with compatibility rules.
  - Announce deprecations with replacement guidance, support end date, and migration examples.
  - Monitor traffic by version and remove an old version only after the retirement criteria are met.
- **probe**: Present this exact question: “Which versioning and compatibility policy will clients rely on?” Options: “versioned URI,” “versioned media type/header,” or “single backward-compatible contract with documented deprecation.” Require a published policy, support window, and retirement evidence.
- **failure_modes**: A required field added to an old response broke strict clients; a route removal caused an outage for an untracked integration; clients could not tell whether a semantic change was breaking.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://google.aip.dev/185; https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md

### api-schema-compatibility
- **definition**: Maintain a machine-readable OpenAPI or equivalent contract and run backward-compatibility checks for every published API change. Compatibility must cover request and response schemas, headers, status codes, and requiredness, not only route names.
- **implementation**:
  - Generate or validate the schema from the implementation at build time and review it as an artifact.
  - Compare the proposed schema with the last supported contract using a breaking-change checker.
  - Test generated clients and representative existing fixtures against the proposed contract.
  - Require an explicit version or migration path for intentional breaking changes.
- **probe**: Parse current and previous API schemas with a compatibility checker and fail CI on removed or tightened request fields, changed response types, or incompatible status and header changes; include examples for every changed operation.
- **failure_modes**: A response field changed from string to object and crashed generated clients; an enum value was removed while old clients still sent it; a formerly optional request field became required without notice.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html; https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md

### bounded-pagination
- **definition**: Define bounded pagination with deterministic ordering, validated limits, opaque cursors or explicit offsets, and documented continuation links or metadata. Every page request has a finite work bound and a defined behavior when records change between pages.
- **implementation**:
  - Set safe default and maximum page sizes and reject zero, negative, or oversized limits.
  - Use a stable unique tie-breaker in ordering, preferably with a signed or opaque cursor.
  - Return next/previous links or cursor metadata with expiration and scope semantics.
  - Bound database work and prevent clients from requesting unbounded scans.
- **probe**: Request default, maximum, zero, negative, oversized, and malformed page parameters while records change between pages; assert bounded work, deterministic ordering, no duplicate/missing records under the documented model, and valid continuation links.
- **failure_modes**: A default page size triggered a full-table scan; offset pagination skipped records during concurrent inserts; an unvalidated limit allowed one client to monopolize database memory.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc8288; https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md

### problem-details-envelope
- **definition**: Use `application/problem+json` with stable `type`, `title`, `status`, `detail`, and `instance` members plus a documented field-error extension. Problem types identify failure classes while details remain safe and actionable for the caller.
- **implementation**:
  - Centralize serialization and set the correct content type for all documented API errors.
  - Assign stable, resolvable problem types and keep titles independent of localized detail text.
  - Define a consistent field-error shape for validation and conflict responses.
  - Include correlation/instance identifiers without leaking stack or persistence details.
- **probe**: Trigger representative validation, authorization, not-found, conflict, rate-limit, and server failures; validate content type, required Problem Details members, stable type, safe detail, and field-error shape.
- **failure_modes**: Clients parsed human text differently by locale; operators could not aggregate failures because every response had a new message; validation clients lost the field path needed to correct input.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9457

### status-retry-contract
- **definition**: Document status-code and retryability semantics for each failure class and include `Retry-After` when a client should wait before retrying. The contract distinguishes permanent client errors from transient overload and dependency failures.
- **implementation**:
  - Maintain a matrix mapping status, Problem Details type, retryability, and client action.
  - Use 429 or appropriate 5xx statuses for retryable conditions and never mark permanent validation errors retryable.
  - Emit a bounded `Retry-After` value when waiting is appropriate.
  - Align SDK defaults, queue consumers, and API documentation with the same matrix.
- **probe**: Invoke representative failure fixtures and compare status, Problem Details type, and `Retry-After` behavior against the published retry matrix; assert permanent failures do not request retries and transient failures provide bounded guidance.
- **failure_modes**: Clients retried invalid requests until rate limits were exhausted; all clients retried a 503 simultaneously without a wait hint; a 429 omitted reset guidance and caused unnecessary polling.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: retry-backoff-breakers
- **sources**: https://www.rfc-editor.org/rfc/rfc9110; https://www.rfc-editor.org/rfc/rfc6585

### rate-limit-headers
- **definition**: Enforce documented quota scope and window and emit `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` with a defined 429 response. Header values must describe the same identity, resource, and clock semantics used by enforcement.
- **implementation**:
  - Define quota identity, dimensions, window algorithm, burst behavior, and reset units.
  - Apply limits at the intended edge/service scope and avoid trusting spoofable identity headers.
  - Return consistent headers on allowed and rejected requests, with `Retry-After` where applicable.
  - Monitor counter drift, rejected requests, and remaining-capacity anomalies.
- **probe**: Send requests past each documented quota from one identity and across identities; parse the three RateLimit fields, 429 status, and reset timing, and assert counters and scope are consistent with enforcement.
- **failure_modes**: Clients exhausted a shared tenant quota believing it was per user; reset timestamps used different units and caused retry storms; a spoofed forwarded header bypassed the intended identity limit.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9458; https://www.rfc-editor.org/rfc/rfc6585

### deprecation-lifecycle
- **definition**: Announce deprecated operations with a replacement, effective date, migration guide, and removal criteria while emitting standardized deprecation or sunset signals. Deprecation is observable, owned, and tracked until traffic reaches the removal threshold.
- **implementation**:
  - Maintain an API catalog with deprecated routes/fields, owner, replacement, dates, and criteria.
  - Emit `Deprecation` and `Sunset` headers and a replacement link where applicable.
  - Measure usage by client/version and notify owners before the sunset date.
  - Require a migration guide and rollback plan before removal.
- **probe**: Invoke every listed deprecated route or field and assert documented `Deprecation` and `Sunset` headers, replacement link, and date agree with the API catalog; verify usage monitoring and removal criteria exist.
- **failure_modes**: A client discovered removal only after deployment; a sunset date passed while a major integration still depended on the route; undocumented replacement semantics caused migration data loss.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9745; https://www.rfc-editor.org/rfc/rfc8594

### mutation-idempotency-key
- **definition**: Require an idempotency key for externally retryable non-idempotent mutations and bind it to the authenticated principal, operation, and request fingerprint. Repeating the same logical request must return the original result without repeating its side effect.
- **implementation**:
  - Require a key header or message field and validate length, format, scope, and expiry.
  - Persist key, principal, operation, request fingerprint, status, and result under a unique constraint.
  - Return the stored result for an identical replay and a deterministic conflict for changed payloads or principals.
  - Document which operations require keys and how clients retry after timeouts.
- **probe**: Submit the same mutation repeatedly with one key, the same key and a changed payload, and different principals; assert one side effect, replayed result, and deterministic conflict isolation.
- **failure_modes**: A payment timeout led the client to charge twice; a retry duplicated a job enqueue; one tenant reused another tenant's key and received or altered the wrong result.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: idempotency-keys
- **sources**: https://docs.stripe.com/api/idempotent_requests; https://www.rfc-editor.org/rfc/rfc9110

### idempotency-atomic-replay
- **definition**: Persist in-flight and completed idempotency state atomically with the mutation result, define key expiry, and return a deterministic conflict for unsafe key reuse. Reservation, side effect, and replayable outcome must have a recovery protocol for crashes between steps.
- **implementation**:
  - Reserve keys with a unique database constraint and an explicit in-flight state.
  - Commit the business mutation and replay metadata in one transaction or durable outbox protocol.
  - Store a complete response or durable result reference and define expiry/garbage-collection semantics.
  - Recover abandoned in-flight keys with lease/timeout rules that cannot duplicate a committed effect.
- **probe**: Fire concurrent identical requests and crash or interrupt between reservation and commit; assert a unique key constraint, one committed effect, and a documented replay or retry result after recovery. Reuse the key with a changed payload and assert conflict.
- **failure_modes**: Two concurrent requests both observed an unused key and charged twice; a crash after the charge but before recording the response caused an unsafe retry; an abandoned reservation blocked all future retries indefinitely.
- **severity**: critical
- **applies_if**: web-api
- **merges_into**: idempotency-keys
- **sources**: https://docs.stripe.com/api/idempotent_requests; https://www.rfc-editor.org/rfc/rfc9110

### mutation-atomicity
- **definition**: Make a mutation's state changes and side-effect intent atomic through a transaction, transactional outbox, or an equivalent durable protocol. The system must make recovery and duplicate delivery explicit rather than leaving state and notifications independently committed.
- **implementation**:
  - Identify the business state and every event, notification, charge, or downstream intent it produces.
  - Commit state plus an outbox/event record in one transaction where possible.
  - Use a durable dispatcher with retries, deduplication, and observability for outbox delivery.
  - Define reconciliation for effects that cannot participate in the local transaction.
- **probe**: The assessor must inspect mutation transaction boundaries, outbox/event schemas, dispatcher recovery, and reconciliation procedures; verify crashes at each state/side-effect boundary produce no silent partial commit and that duplicate delivery is safe.
- **failure_modes**: An order committed without its fulfillment event; an email was sent but the transaction rolled back and support saw no order; a downstream charge succeeded while the local state remained pending with no reconciliation.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html; https://cwe.mitre.org/data/definitions/662.html

### shared-state-synchronization
- **definition**: Identify every shared mutable state location and protect it with a transaction, lock, atomic operation, compare-and-swap, or explicit ownership model. The chosen mechanism must cover all readers and writers and define visibility and failure semantics.
- **implementation**:
  - Inventory process-local, database, cache, queue, and distributed shared state.
  - Assign one synchronization strategy and owner to each mutable location.
  - Use atomic increments/updates or transactions for compound invariants instead of read-modify-write races.
  - Document lock ordering, timeout, retry, and recovery behavior for lock-based paths.
- **probe**: The assessor must inspect the shared-state inventory and representative read/write paths, then confirm each invariant has a transaction, lock, atomic operation, CAS, or owner boundary; verify lock timeout and failure behavior are documented.
- **failure_modes**: Concurrent workers overwrote counters; a cache update raced with invalidation and served stale authorization; inconsistent lock ordering deadlocked two request paths.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cwe.mitre.org/data/definitions/362.html; https://cwe.mitre.org/data/definitions/662.html

### optimistic-concurrency
- **definition**: Expose a version or ETag and reject stale writes with compare-and-swap semantics instead of silently using last-write-wins. Clients receive a documented conflict or precondition failure and can reread and reconcile.
- **implementation**:
  - Include a monotonically changing version or strong ETag in resource reads.
  - Require `If-Match` or an equivalent expected-version field on update operations.
  - Perform the version check and write atomically in the persistence layer.
  - Return 409 or 412 with a stable error type and avoid overwriting the newer state.
- **probe**: Read one resource twice, submit two updates using the same version or ETag, and assert exactly one succeeds while the other receives the documented conflict or precondition failure; verify the stored resource contains no lost update.
- **failure_modes**: Two editors silently overwrote each other's changes; a worker retried an old update after a newer workflow completed; a stale cache wrote obsolete permissions over a current policy.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9110; https://cwe.mitre.org/data/definitions/362.html

### race-stress-detection
- **definition**: Run race detectors, stress tests, and controlled-schedule tests around shared state and make failures reproducible with deterministic seeds. Serial unit tests are insufficient evidence for timing-dependent invariants.
- **implementation**:
  - Enable the language runtime's race detector or equivalent sanitizer in CI for concurrency-sensitive packages.
  - Add stress harnesses that vary interleavings, worker counts, cancellation, and injected delays.
  - Record deterministic seeds, schedules, traces, and minimized reproductions for failures.
  - Assert invariants and final state, not only absence of panics or error codes.
- **probe**: Execute the language's race detector or equivalent stress harness repeatedly with controlled scheduling and fail on race reports, invariant violations, or nondeterministic final state; rerun any failing seed to confirm reproducibility.
- **failure_modes**: A serial test suite passed while concurrent increments lost data; a rare cancellation race leaked a lock and stalled production workers; a cache corruption bug appeared only under a particular scheduling interleaving.
- **severity**: important
- **applies_if**: all
- **sources**: https://cwe.mitre.org/data/definitions/362.html; https://cwe.mitre.org/data/definitions/662.html

### distributed-lock-fencing
- **definition**: Give distributed critical sections explicit ownership, lease expiry, fencing tokens, and recovery behavior rather than relying on an unbounded lock. Every protected write must reject work from an expired or superseded owner.
- **implementation**:
  - Acquire a lease with an owner identity, TTL, renewal policy, and bounded acquisition timeout.
  - Issue a monotonically increasing fencing token on each successful acquisition.
  - Pass the token to the protected datastore and reject writes with an older token.
  - Define crash recovery, lease expiry, clock assumptions, and operator unlock procedures.
- **probe**: The assessor must inspect lock acquisition, renewal, expiry, fencing-token issuance, and protected-write enforcement; simulate a paused owner resuming after lease expiry and verify its stale token is rejected, while a new owner can progress without permanent deadlock.
- **failure_modes**: A paused worker resumed after lease expiry and overwrote work owned by a replacement; a crashed process left a lock held forever; clock skew caused two workers to believe they owned the same critical section.
- **severity**: important
- **applies_if**: data-pipeline
- **sources**: https://cwe.mitre.org/data/definitions/662.html; https://cwe.mitre.org/data/definitions/367.html
