# Documentation & developer experience glossary

### readme-purpose
- **definition**: The README opening states who the product serves, the problem it solves, supported use cases, and explicit non-goals. It gives a prospective user enough scope information to decide whether this repository is the right component before installation.
- **implementation**:
  - Put an audience-and-purpose paragraph and a short "Use cases" list before installation instructions.
  - Add a "Non-goals" or "When not to use this" section with concrete exclusions.
  - Link each supported use case to a runnable example or task guide.
  - Keep claims aligned with the currently released feature set and review them with product ownership.
- **probe**: An assessor inspects the first screen of the README and verifies that audience, problem, supported use cases, and non-goals are explicit, mutually consistent, and linked to current documentation.
- **failure_modes**: A team adopts a library for an unsupported workload and discovers the limitation after a migration. A user treats an experimental integration as production-ready because the README never states its scope.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes

### readme-quickstart
- **definition**: The README quickstart is a copy-paste path from a clean checkout or install to one observable successful result. It includes commands, required inputs, expected output or URL, and enough cleanup or stop guidance to make the result reproducible.
- **implementation**:
  - Pin the command sequence, including dependency installation and environment initialization.
  - Use a minimal fixture or sample request that does not require undocumented private data.
  - Show the expected success marker, response, URL, or exit status immediately after the commands.
  - Run the sequence periodically in CI from a fresh temporary directory and update it when commands change.
- **probe**: `tmp=$(mktemp -d); git clone "$REPO" "$tmp/repo"; cd "$tmp/repo"; extract_readme_quickstart | sh; test "$?" -eq 0; assert_output_or_url_matches_readme`; run only the documented quickstart commands in the clean environment and fail if the documented result is absent.
- **failure_modes**: A new contributor cannot start the service because the README omits a generation step. An incident responder cannot reproduce a reported issue because the sample command now exits nonzero or points to a dead endpoint.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes

### readme-prerequisites
- **definition**: Prerequisites identify supported operating systems, runtimes, package managers, external services, credentials, and minimum versions before setup begins. Version and access requirements are explicit enough to distinguish an unsupported environment from a product defect.
- **implementation**:
  - Add a prerequisites table with component, supported versions, installation link, and whether it is required locally or only for optional features.
  - State required credentials, permission scope, network access, and safe secret provisioning without publishing secret values.
  - Pin lockfile/package-manager expectations and call out OS-specific dependencies.
  - Keep prerequisites synchronized with CI images and the devcontainer definition.
- **probe**: An assessor compares the README prerequisites with runtime manifests, lockfiles, CI images, container configuration, and integration setup; every required dependency has a supported minimum/version range and credential requirement, and no setup command relies on an unstated service.
- **failure_modes**: A command fails only on an older runtime and is misdiagnosed as a regression. A developer spends hours debugging authentication that was actually an undocumented cloud permission requirement.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes

### readme-configuration
- **definition**: Configuration documentation covers every required or materially behavior-changing value, including its type, default, example, secret treatment, and restart or reload semantics. It distinguishes safe defaults from values that affect availability, security, data handling, or cost.
- **implementation**:
  - Generate or maintain a configuration reference table with name, type, required status, default, valid range, and example.
  - Mark secrets explicitly and document injection through environment variables or a secret manager rather than checked-in files.
  - Describe precedence among defaults, files, environment variables, and flags.
  - State whether changes take effect on restart, hot reload, new requests, or the next deployment.
  - Validate unknown, malformed, and unsafe values at startup with actionable errors.
- **probe**: An assessor inventories configuration reads and schemas, compares them with the reference, and checks each entry for type/default/example/secret/reload information, including precedence and validation behavior.
- **failure_modes**: An operator enables a permissive setting believing it is harmless and exposes an endpoint. A changed timeout remains inactive until an undocumented restart, causing an apparently ineffective remediation.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/config

### devcontainer-reproducibility
- **definition**: A devcontainer or equivalent declares a reproducible contributor environment that installs the project’s tools and dependencies. It exposes the same setup, test, lint, docs, and run entrypoints documented for contributors rather than creating a separate workflow.
- **implementation**:
  - Pin the base image digest or a supported image tag and declare language/runtime versions.
  - Use `devcontainer.json`, a Dockerfile, or an equivalent environment manifest to install system and project dependencies.
  - Forward only documented environment variables and provide a safe example or secrets prompt for credentials.
  - Run setup and verification commands as container build or post-create checks.
  - Build the container in CI on dependency or image changes.
- **probe**: `devcontainer build --workspace-folder .`; `devcontainer up --workspace-folder .`; `devcontainer exec --workspace-folder . <documented-setup>`; `devcontainer exec --workspace-folder . <documented-version-check>`; assert each command exits zero and the declared runtime/dependency versions match the manifest.
- **failure_modes**: Two contributors use different compiler versions and produce incompatible generated artifacts. A fresh laptop lacks a system library that was silently present on maintainers’ machines.
- **severity**: critical
- **applies_if**: all
- **sources**: https://containers.dev/implementors/spec/

### make-entrypoints
- **definition**: Make or equivalent task entrypoints provide a small, discoverable, deterministic interface for setup, test, lint, documentation, run, and clean operations. Each target owns command composition and returns meaningful exit status so local use and CI use the same path.
- **implementation**:
  - Define explicit `setup`, `test`, `lint`, `docs`, `run`, and `clean` targets or documented equivalents.
  - Make targets fail on the first failed command and avoid hidden interactive prompts.
  - Use `.PHONY` declarations, stable working-directory behavior, and help output listing available tasks.
  - Keep target commands aligned with CI and state required environment variables or services.
  - Make cleanup scoped to generated artifacts, never implicit source or data deletion.
- **probe**: `for target in setup test lint docs run; do make -n "$target" >/dev/null || exit 1; done; for target in setup test lint docs run; do make "$target"; test $? -eq 0; done`; execute in an isolated checkout with documented prerequisites and assert expected exit status and artifacts.
- **failure_modes**: CI runs a different lint command than contributors, allowing a review-breaking error through. An ad hoc cleanup command deletes a local fixture needed to reproduce an incident.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.gnu.org/software/make/manual/make.html

### local-parity
- **definition**: Local-parity documentation records which local services, versions, flags, fixtures, and data-loading steps intentionally match production. It also names every accepted difference and explains what confidence that difference does or does not provide.
- **implementation**:
  - Maintain a local-to-production comparison covering runtime image, databases, queues, feature flags, auth, network topology, and data shape.
  - Provide a deterministic seed or fixture loader and document its limits relative to production data.
  - Mark emulators and mocks clearly, including unsupported behaviors and failure semantics.
  - Require a review when a production dependency or local default changes.
- **probe**: An assessor compares the parity matrix with deployment manifests, local compose/devcontainer configuration, feature-flag defaults, fixtures, and staging setup; every material mismatch is listed with an explicit risk and validation boundary.
- **failure_modes**: A mock queue hides an ordering bug that appears after deployment. Local authentication bypasses tenant checks, so a data-isolation defect is found only in production.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/dev-prod-parity

### onboarding-path
- **definition**: The onboarding path is the shortest maintained sequence from clone through environment setup to a first meaningful, reviewable change. It has an explicit time objective of less than one working day and is periodically rehearsed by someone without maintainer tribal knowledge.
- **implementation**:
  - Provide a numbered clone, prerequisite, setup, verification, edit, test, and review flow.
  - Link each step to authoritative commands instead of duplicating divergent instructions.
  - Include one small starter change that exercises the normal build and review path.
  - Record rehearsal date, participant, blockers, and remediation owners.
- **probe**: An assessor inspects a recent onboarding rehearsal record and follows the documented path in a clean environment, verifying it reaches a meaningful change and review-ready checks within the stated time objective without private oral instructions.
- **failure_modes**: A new hire loses days to an undocumented generated-file step. Only one maintainer knows how to run the service, creating a bottleneck during an urgent fix.
- **severity**: critical
- **applies_if**: all
- **sources**: https://diataxis.fr/tutorials/

### troubleshooting-guide
- **definition**: A troubleshooting guide maps common setup and runtime symptoms to diagnostic commands, likely causes, safe fixes, and an escalation destination. It separates reversible diagnostics from potentially destructive remediation and states what evidence to collect.
- **implementation**:
  - Organize entries by symptom with prerequisites, commands, expected output, cause branches, and verification.
  - Use copy-paste-safe commands that redact credentials and avoid destructive defaults.
  - Include service/version context, log locations, correlation identifiers, and escalation contacts.
  - Add new entries after recurring support cases and test the commands on supported environments.
- **probe**: An assessor selects the top recurring failures, follows each guide from symptom through verification, and confirms commands are safe, causes are plausible, fixes are current, and escalation information resolves to an owned channel.
- **failure_modes**: Responders repeatedly restart a healthy dependency instead of diagnosing a certificate expiry. A user pastes a secret-bearing diagnostic command into a public issue because the guide did not specify redaction.
- **severity**: important
- **applies_if**: all
- **sources**: https://diataxis.fr/how-to-guides/

### architecture-overview
- **definition**: The architecture overview explains major components, ownership boundaries, data flows, external dependencies, and trust boundaries at a level useful for design and incident work. It identifies where requests and data cross process, tenant, privilege, and network boundaries.
- **implementation**:
  - Maintain a context/container diagram plus a concise component and data-flow narrative.
  - Label owners, persistence systems, queues, third-party services, authentication boundaries, and failure handoffs.
  - Link components to source directories, deployable services, dashboards, and runbooks.
  - Review the overview whenever a component, dependency, trust boundary, or critical flow changes.
- **probe**: An assessor traces representative user and operational flows through the diagram and code/deployment manifests, checking that components, owners, external dependencies, data stores, and trust boundaries are present and current.
- **failure_modes**: A change bypasses an authorization boundary that was absent from the diagram. During an outage, responders page the wrong team because ownership and dependency direction are unclear.
- **severity**: critical
- **applies_if**: all
- **sources**: https://c4model.com/

### architecture-source
- **definition**: Architecture diagrams are stored as editable, reviewable source beside the code and rendered through a deterministic command. The repository records diagram ownership and a last-reviewed signal so the rendered artifact can be regenerated after structural changes.
- **implementation**:
  - Store Mermaid, PlantUML, Structurizr, or equivalent source in a documented diagrams directory.
  - Pin the renderer version and expose one command that generates checked-in or published output.
  - Add owner and `last_reviewed` metadata, and review diagrams with related architecture changes.
  - Make CI fail when rendering errors occur or generated output differs from source.
- **probe**: `run_diagram_render_command`; `git diff --exit-code -- generated/diagrams`; assert source files, renderer command/version, owner metadata, and review signal exist; fail on render errors or generated-artifact drift.
- **failure_modes**: A diagram is updated manually in a presentation but not in the repository, so engineers design against a stale dependency map. A renderer upgrade silently changes output and hides a missing trust-boundary edge.
- **severity**: important
- **applies_if**: all
- **sources**: https://mermaid.js.org/intro/

### adr-records
- **definition**: An architecture decision record (ADR) captures a consequential decision and the reasoning that makes it durable. Each record includes context, decision, alternatives, consequences, owner, date, and status so later changes can distinguish intent from implementation detail.
- **implementation**:
  - Use a numbered or otherwise stable ADR filename and a shared template.
  - Require context/problem, decision, alternatives considered, positive and negative consequences, owner, date, and status.
  - Link the ADR from relevant architecture, code, issue, or migration documentation.
  - Create an ADR during review for decisions that affect boundaries, interfaces, data, reliability, security, or cost.
- **probe**: An assessor samples consequential changes and verifies each has a linked ADR or an explicit rationale for exemption, with all required fields populated and alternatives and consequences specific enough to guide future work.
- **failure_modes**: A team reintroduces a rejected storage design after the original author leaves. A maintainer removes a compatibility constraint because its reason existed only in a private conversation.
- **severity**: important
- **applies_if**: all
- **sources**: https://adr.github.io/

### adr-lifecycle
- **definition**: ADR lifecycle metadata distinguishes proposed, accepted, superseded, and deprecated decisions. When one decision supersedes another, both records link to the replacement relationship so readers do not follow obsolete guidance as if it were current.
- **implementation**:
  - Restrict status values to a documented set and place status in front matter or a standard heading.
  - Add `supersedes`/`superseded_by` links using stable repository paths or identifiers.
  - Preserve old records for historical context while marking them non-authoritative.
  - Update the old and new ADR in the same change and check links in CI.
- **probe**: `parse_adr_status_and_links`; `for adr in superseded_or_deprecated; do test -f "$(replacement_link "$adr")" || exit 1; done`; assert statuses are allowed, replacements exist, and both sides of each supersession are linked.
- **failure_modes**: A superseded ADR remains marked accepted, so a new service follows a retired deployment constraint. A replacement record is moved without updating the old link, leaving maintainers unable to find the current decision.
- **severity**: important
- **applies_if**: all
- **sources**: https://adr.github.io/

### rationale-comments
- **definition**: Inline comments explain non-obvious rationale, invariants, compatibility constraints, or rejected alternatives rather than narrating statements already evident in code. Implementation facts belong in code and tests, while comments preserve the reasoning a future simplification could otherwise erase.
- **implementation**:
  - Add comments at the constraint or decision point and name the failure avoided.
  - Prefer links to ADRs, issues, specifications, or compatibility evidence for durable context.
  - Remove comments that merely paraphrase the next line or assert behavior tests already enforce.
  - Review rationale comments when the referenced dependency, invariant, or workaround changes.
- **probe**: An assessor samples comments around compatibility branches, invariants, workarounds, and rejected alternatives; each non-obvious comment explains why the code must be shaped that way and points to evidence where appropriate, without duplicating implementation.
- **failure_modes**: A maintainer removes a compatibility workaround because a comment only described what the code did, reintroducing a client break. A stale rationale claims an invariant that no longer exists and misdirects future debugging.
- **severity**: important
- **applies_if**: all
- **sources**: https://diataxis.fr/explanation/

### openapi-contract
- **definition**: The OpenAPI contract is a complete, versioned description of every supported HTTP operation, including paths, parameters, request and response schemas, authentication, errors, and examples. It is published where consumers can retrieve the exact contract for the API version they call.
- **implementation**:
  - Define operation IDs, parameter locations and constraints, request/response schemas, content types, auth schemes, and standard error envelopes.
  - Include success, validation, authentication, authorization, rate-limit, and representative server-error responses.
  - Version the document with the API and publish it at a stable URL or artifact location.
  - Generate client-facing examples from the contract where possible and review route changes with spec changes.
- **probe**: `oas_validator openapi.yaml`; `routes=$(enumerate_registered_http_routes)`; `ops=$(enumerate_openapi_operations)`; compare route identities and fail when any supported route lacks an operation, required parameter/schema, auth declaration, or response.
- **failure_modes**: A newly deployed endpoint is absent from the published contract, so generated clients cannot call it safely. A missing error schema leads consumers to parse an HTML or undocumented payload and fail open.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html

### openapi-generation
- **definition**: OpenAPI generation derives the contract from authoritative code annotations, route metadata, or schema sources instead of maintaining a second hand-edited description. CI regenerates the artifact and fails when the generated result differs from the committed or published specification.
- **implementation**:
  - Choose one authoritative source and document the generation command and tool version.
  - Commit generated output only if consumers need it, clearly marking it as generated.
  - Run generation in CI from a clean checkout and compare semantically or byte-for-byte with the checked-in artifact.
  - Review generated contract changes alongside route, validation, and compatibility changes.
- **probe**: `make openapi-generate`; `git diff --exit-code -- openapi.yaml`; then validate the regenerated document with an OAS validator; fail on generation errors or any unreviewed diff.
- **failure_modes**: A route validation change ships without a spec update because the hand-maintained document looked current. Client generation uses stale schemas and rejects valid production responses.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html

### api-examples
- **definition**: API examples are minimal runnable workflows that demonstrate authentication, valid requests, expected responses, sequencing, and representative failures. They supplement the schema with the practical interaction a consumer must perform.
- **implementation**:
  - Provide copy-paste examples for each important workflow in curl and at least one supported client language where relevant.
  - Use placeholders for host, credentials, IDs, and secrets, with an explicit safe setup step.
  - Show headers, payloads, status codes, response shape, retries or polling, and expected error handling.
  - Keep examples versioned with the API and execute them against a local, mock, or staging target in CI.
- **probe**: `extract_api_examples`; `for example in examples; do run_example "$example" --target "$LOCAL_API"; assert_status_and_response_shape "$example"; done`; require each documented workflow to have an executable success and representative failure case.
- **failure_modes**: An integration sends the right payload before obtaining the required token or resource, so every first-time setup fails. An example omits a required header and causes consumers to copy a consistently unauthorized request.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html

### api-behavior
- **definition**: API behavior documentation specifies operational semantics that schemas cannot express, including idempotency, pagination, ordering, retries, rate limits, eventual consistency, and webhook delivery. It defines what clients may safely assume and how they should react to transient or ambiguous outcomes.
- **implementation**:
  - Document idempotency-key scope and replay behavior, pagination cursor rules, ordering guarantees, and consistency delays.
  - State retryable statuses, backoff expectations, rate-limit headers and limits, and request timeout guidance.
  - Describe webhook authentication, event ordering, duplication, retry schedule, acknowledgment, and signature validation.
  - Include examples for timeout/unknown-result and stale-read handling, not just successful responses.
- **probe**: An assessor traces implementation, tests, gateway policy, and webhook worker behavior against the documented semantics; evidence must cover retries, duplicate requests/events, pagination boundaries, ordering, rate limits, and eventual-consistency windows.
- **failure_modes**: A client retries a non-idempotent request after a timeout and creates duplicate orders. A webhook consumer assumes ordered, single delivery and loses state when events arrive duplicated or out of order.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://spec.openapis.org/oas/latest.html

### api-versioning
- **definition**: API versioning defines supported versions, compatibility guarantees, deprecation windows, sunset communication, and migration paths with concrete examples. Consumers can determine whether a change is safe, when an old version stops receiving support, and how to move to the replacement.
- **implementation**:
  - Declare the versioning mechanism and compatibility policy for URLs, headers, schemas, and behavior.
  - Publish a support matrix with release dates, deprecation and sunset dates, and security-support boundaries.
  - Send deprecation signals through documentation, response headers or metadata, changelog, and release notices.
  - Provide before/after migration examples and maintain contract tests for supported versions.
- **probe**: An assessor compares the version support matrix with deployed routes, gateway configuration, response deprecation signals, release history, and migration docs; every advertised version has an owner, compatibility promise, and actionable sunset path.
- **failure_modes**: A client continues calling a removed version and suffers an outage because no sunset date or migration path was published. Consumers implement unsafe retry behavior because rate limits and eventual consistency were undocumented.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://semver.org/

### alert-runbook-links
- **definition**: Every actionable alert carries a stable runbook URL, and the runbook identifies the exact alert rule, alert identity, and affected service. The link remains resolvable from the alerting system during an incident rather than pointing to an ephemeral review artifact.
- **implementation**:
  - Add a required `runbook_url` annotation/label to actionable alert rules.
  - Use stable documentation URLs and include the exact alert name or rule ID in the runbook.
  - Validate links during CI and check that generated alert labels preserve the annotation.
  - Keep runbooks access-compatible with the responders who receive the alert.
- **probe**: `rules=$(parse_alert_rules)`; `for rule in $rules; do url=$(runbook_url "$rule"); test -n "$url"; curl --fail --location --max-time 10 "$url" >/dev/null; runbook_contains_identifier "$url" "$(alert_identifier "$rule")" || exit 1; done`; fail if any actionable rule lacks a reachable, matching runbook.
- **failure_modes**: An alert fires during an outage but its runbook URL is broken, delaying diagnosis. A generic runbook omits the specific alert identity, so responders investigate the wrong symptom.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/workbook/alerting-on-slos/

### runbook-procedure
- **definition**: A runbook is an executable incident procedure, not a narrative overview. It states impact, prerequisites, the first diagnostic commands, safe mitigations, verification, rollback, and stop conditions in an order a responder can follow under pressure.
- **implementation**:
  - Begin with impact and severity cues, ownership, access prerequisites, and safety warnings.
  - Provide the first five commands with expected signals and branch to likely causes.
  - Separate read-only diagnostics from mitigations; include approval or blast-radius limits.
  - Define success verification, rollback steps, escalation triggers, and stop conditions.
  - Use stable dashboard, log, and command links and test procedures during game days or incidents.
- **probe**: An assessor executes the first diagnostic sequence in a safe environment and verifies that impact, prerequisites, commands, expected signals, mitigation, verification, rollback, and stop conditions are all present and internally consistent.
- **failure_modes**: Responders execute an unsafe mitigation without a blast-radius warning and worsen an outage. A narrative runbook lacks rollback or verification, leaving a partial recovery undetected.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/workbook/alerting-on-slos/

### runbook-escalation
- **definition**: Runbook escalation guidance tells responders who owns the service, which severity-specific contacts to use, when to page an incident commander, how to involve vendors, and when to declare or hand off an incident. It turns an ambiguous failure into an explicit escalation decision.
- **implementation**:
  - Map impact thresholds to severity and required roles, including incident commander and communications owner.
  - List reachable primary and backup contacts, team channels, on-call schedules, and vendor support paths.
  - State acknowledgment, handoff, and declaration expectations with time or impact triggers.
  - Review contacts and vendor contracts whenever ownership or support coverage changes.
- **probe**: An assessor simulates each documented severity branch and verifies contacts resolve, roles are unambiguous, vendor dependencies have escalation instructions, and handoff/declaration criteria are actionable.
- **failure_modes**: An on-call engineer cannot reach the listed owner during a severe incident and loses time searching for escalation coverage. A vendor dependency is not escalated until its support window has passed.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/sre-book/being-on-call/

### runbook-freshness
- **definition**: Every runbook has an accountable owner, a review cadence or due date, and an update trigger for incidents and alert-rule changes. Freshness checks catch retired commands, dashboards, owners, and alert identities before responders need them.
- **implementation**:
  - Add machine-readable owner, `last_reviewed`, and `review_interval` metadata to each runbook.
  - Require review after an incident, service architecture change, alert-rule change, or dependency retirement.
  - Link referenced alert IDs, dashboards, commands, and contacts to canonical current locations.
  - Run a scheduled freshness check and route overdue items to the owner.
- **probe**: `parse_runbook_metadata`; fail if owner is missing or `today > last_reviewed + review_interval`; extract alert IDs and compare them with current alert definitions; resolve referenced URLs and report stale or missing dependencies.
- **failure_modes**: A runbook points to a retired dashboard during an incident because no review date was enforced. An alert rename leaves the old identifier in the procedure, causing responders to miss the relevant investigation steps.
- **severity**: important
- **applies_if**: all
- **sources**: https://sre.google/sre-book/part-III-practices/

### changelog-format
- **definition**: The changelog is a human-readable, release-grouped record of user-relevant changes organized by change type. It retains an `Unreleased` section and links release entries to tags, release notes, migrations, or other authoritative references.
- **implementation**:
  - Follow a documented format such as Added, Changed, Deprecated, Removed, Fixed, and Security.
  - Put newest releases first and preserve an explicit unreleased section at the top.
  - Write entries in user impact language and link to issue, pull request, migration, or release references.
  - Keep version dates and headings consistent with published artifacts and tags.
- **probe**: An assessor inspects recent releases and the unreleased section for consistent headings, user-visible entries, dates, and links; entries must reconcile with release artifacts and known breaking or migration changes.
- **failure_modes**: Users upgrade across a breaking change without noticing it because the change was buried outside a consistent changelog category. Maintainers cannot correlate a regression with the release that introduced it.
- **severity**: important
- **applies_if**: all
- **sources**: https://keepachangelog.com/en/1.1.0/

### changelog-gate
- **definition**: Review policy requires every user-visible change to add a changelog entry or receive an explicit, reviewable waiver. The gate runs on relevant source, API, configuration, migration, and documentation changes rather than relying on a release-time manual sweep.
- **implementation**:
  - Add a pull-request check that detects user-facing paths and requires a changelog diff.
  - Define a narrowly scoped waiver label or declaration with reviewer ownership and rationale.
  - Exclude generated files and internal-only refactors only when the policy documents those exclusions.
  - Report the required action directly in the check output and block merge on failure.
- **probe**: Create or inspect a workflow test with a user-facing change and no changelog: expect failure; repeat with a changelog entry: expect success; repeat with an authorized waiver: expect success and recorded rationale.
- **failure_modes**: A small configuration change reaches users without a release note because no pull-request gate required one. The release process later omits the only migration warning that downstream operators needed.
- **severity**: critical
- **applies_if**: all
- **sources**: https://keepachangelog.com/en/1.1.0/

### release-notes
- **definition**: Release notes explain the operational and user impact of a release, not merely its version number. Each release covers highlights, fixes, breaking changes, migrations, known limitations, upgrade steps, and a link to the complete changelog.
- **implementation**:
  - Use a stable template with impact summary, highlights, fixes, breaking changes, migrations, limitations, and upgrade/rollback steps.
  - Identify affected versions, configurations, data migrations, permissions, and compatibility risks.
  - Link each notable item to a changelog entry, issue, PR, or migration guide.
  - Publish notes with the artifact and make them discoverable from the release index and upgrade documentation.
- **probe**: An assessor samples releases and cross-checks notes against commits, changelog, migration scripts, and known limitations; required sections are present and upgrade steps are executable for affected deployment types.
- **failure_modes**: Operators deploy a release that changes a data format without migration or rollback instructions. A known limitation is discovered only after adoption because release notes listed highlights but not impact.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes

### release-reproducibility
- **definition**: Release documentation makes an artifact reproducible and traceable from source revision through approvals and validation to publication. It also defines rollback, yanking, or replacement procedures so a bad release can be contained without guesswork.
- **implementation**:
  - Record immutable source revision, build toolchain/dependency inputs, artifact checksums, and provenance metadata.
  - Document release commands, required approvals, environment protections, signing, and validation gates.
  - Publish the exact artifact-to-source mapping and retain build logs or attestations.
  - Define rollback/yank triggers, owner, commands, communication, and post-release verification.
- **probe**: An assessor takes a published release and traces its source revision, build configuration, approvals, checksums/provenance, and validation evidence; a dry-run of rollback or yanking identifies a deterministic owner and command.
- **failure_modes**: A compromised or faulty artifact cannot be traced to source or build inputs, blocking containment and forensic analysis. A bad release remains live because nobody knows the approved yank or rollback command.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes

### contributing-guide
- **definition**: The contributing guide explains how to set up the project, choose branches, format commits, run tests and documentation checks, prepare a review, and keep local commands aligned with CI. It gives a contributor a complete path from change to merge without private process knowledge.
- **implementation**:
  - Document prerequisites, setup, verification, branch/commit expectations, and required checks.
  - State when code, tests, docs, changelog, migrations, or release notes are required.
  - Link review templates, code ownership, security reporting, and escalation policies.
  - Use the same task entrypoints and versions as CI, and update the guide in the same change when workflow changes.
- **probe**: An assessor follows the guide in a clean checkout through setup, a small change, required checks, and pull-request preparation; every command exists, exits as documented, and matches CI configuration.
- **failure_modes**: A contributor follows outdated local commands and submits changes that CI cannot reproduce. Required migration or documentation work is omitted because the review obligations were tribal knowledge.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors

### docs-codeowners
- **definition**: CODEOWNERS rules assign accountable, reachable reviewers to source, API schemas, architecture diagrams, runbooks, and user documentation. Coverage is explicit for monorepo subtrees, and owners are valid repository users or teams with the needed review responsibility.
- **implementation**:
  - Add rules for each documentation and operational path, including generated-source owners where applicable.
  - Use team handles or named users that resolve in repository metadata and have repository access.
  - Order rules carefully so specific paths are not shadowed by broad patterns.
  - Review ownership during team changes and require CODEOWNERS review for ownership-file changes.
- **probe**: `parse_codeowners`; for each required path, resolve the effective owner and verify the owner exists and is reachable; test representative files in source, API, diagrams, runbooks, and docs paths for coverage and rule precedence.
- **failure_modes**: A schema change lands without review from the API owner, breaking consumers silently. A runbook changes without an accountable owner and becomes stale across repeated service changes.
- **severity**: critical
- **applies_if**: monorepo
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners

### pr-conventions
- **definition**: Pull-request conventions provide a concise checklist for scope, required evidence, migration notes, UI screenshots, documentation impact, and reviewer responsibilities. They make compatibility and operational consequences part of normal review rather than optional author memory.
- **implementation**:
  - Include change summary, scope/non-goals, tests/checks, screenshots for UI changes, and rollout/rollback evidence.
  - Require API/config/data compatibility and migration notes when relevant.
  - Ask authors to identify docs, changelog, security, performance, and operational impact.
  - Define reviewer roles, required approvals, and acceptable evidence for risky changes.
- **probe**: An assessor inspects the pull-request template and samples merged requests, verifying the checklist requests the required evidence and that reviewers use it for code, API, migration, UI, and operational changes.
- **failure_modes**: A pull request changes a UI or migration without screenshots or rollout evidence, so reviewers miss a user-visible regression. Compatibility impact is discovered after merge because the author checklist never asked for it.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests

### docs-ci
- **definition**: Documentation CI builds the site and validates navigation, links, includes, metadata, references, and generated pages before publication. It fails on unresolved internal links, missing included files, malformed metadata, placeholders, or stale generated output.
- **implementation**:
  - Run the same docs build used for publication in a clean checkout.
  - Add internal-link, anchor, image, include, front-matter, and reference validation.
  - Check generated pages are regenerated and have no unresolved placeholders or template errors.
  - Publish actionable diagnostics and block merge or deployment on configured failures.
- **probe**: `make docs-build`; `make docs-linkcheck`; `make docs-validate`; `test $? -eq 0`; scan generated output for unresolved-link markers, missing includes, malformed metadata, and placeholder tokens; assert all checks return success.
- **failure_modes**: A broken include or internal link is published and sends users to missing operational guidance. A stale generated page masks changed setup instructions because application tests never build the docs.
- **severity**: critical
- **applies_if**: all
- **sources**: https://diataxis.fr/

### docs-example-tests
- **definition**: Documentation example testing executes code samples, commands, configuration fragments, and API examples, or marks intentionally illustrative snippets as non-runnable. Executable examples are tested against supported versions and expected output so copy-paste guidance remains trustworthy.
- **implementation**:
  - Tag fenced blocks with language, environment, and runnable/illustrative intent.
  - Extract examples through a documented docs-test command with isolated fixtures and safe credentials.
  - Assert exit status, response shape, and stable output while normalizing nondeterministic values.
  - Run examples in CI against supported runtime versions and report the source page/line on failure.
- **probe**: `make docs-examples-test`; enumerate fenced blocks and example scripts, execute every runnable item in an isolated environment, and fail on nonzero status or drifted expected output; ensure non-runnable snippets carry an explicit marker.
- **failure_modes**: A copied command fails after a dependency update and blocks adoption. An illustrative snippet is mistaken for executable configuration because the page never states its status.
- **severity**: important
- **applies_if**: all
- **sources**: https://diataxis.fr/tutorials/

### docs-navigation
- **definition**: Documentation navigation organizes content by user task and by the appropriate Diátaxis mode: tutorials, how-to guides, reference, explanations, API, and operations. Required landing pages are reachable and intentional index/generated pages are the only pages without inbound navigation links.
- **implementation**:
  - Maintain a primary navigation tree with audience/task labels and links to README, tutorials, how-to, reference, explanation, API, and operations.
  - Use descriptive labels and breadcrumbs or previous/next links for deep sections.
  - Add every new page to navigation or explicitly mark it as an index/generated page.
  - Generate a link graph in CI and remove stale, duplicate, and orphaned paths.
- **probe**: `build_docs_site`; `build_docs_link_graph`; for each required landing page, assert reachable from navigation; fail for orphan pages except allowlisted index/generated pages and for broken navigation links.
- **failure_modes**: A critical migration guide becomes unreachable after a navigation restructure, so users search for unofficial and unsafe instructions. Orphan pages accumulate conflicting setup advice.
- **severity**: important
- **applies_if**: all
- **sources**: https://diataxis.fr/

### docs-versioning
- **definition**: Versioned documentation labels the behavior, configuration, and API version each page describes. It clearly distinguishes the default/current version from archived and end-of-life versions so operators do not apply incompatible instructions to older deployments.
- **implementation**:
  - Publish a version selector and put an unmistakable version label on version-sensitive pages.
  - Define which version is default, which are supported, archived, or end-of-life, with dates and support links.
  - Preserve immutable version snapshots and avoid silently rewriting historical instructions.
  - Add migration links from archived versions to supported equivalents and update redirects deliberately.
- **probe**: An assessor selects pages covering API, configuration, and migrations and verifies version labels, selector behavior, support status, archive immutability, and migration links against deployed version support policy.
- **failure_modes**: An operator follows current API instructions against an older deployment and sends incompatible requests. An archived page is silently rewritten, making incident reproduction against the historical version impossible.
- **severity**: important
- **applies_if**: all
- **sources**: https://diataxis.fr/reference/

### glossary
- **definition**: A glossary is a concise, authoritative vocabulary for domain terms, acronyms, state names, and identifiers shared by code, APIs, alerts, and runbooks. Each term has one unambiguous meaning and, where useful, links to the contract or procedure that governs it.
- **implementation**:
  - Define terms in plain language with capitalization, allowed states, and distinctions from nearby concepts.
  - Preserve canonical spellings for identifiers, API fields, alert names, and lifecycle states.
  - Link entries to schemas, ADRs, runbooks, and examples rather than duplicating full reference material.
  - Review glossary changes with the owning domain when terminology or public contracts change.
- **probe**: An assessor samples terms across code, API docs, alerts, and runbooks, checks that each has one canonical definition and spelling, and verifies cross-references resolve and do not contradict contracts or state machines.
- **failure_modes**: Two teams use the same state name for different meanings and route a production incident incorrectly. An API field and alert label drift in spelling, causing automation and human responders to disagree about lifecycle state.
- **severity**: nice-to-have
- **applies_if**: all
- **sources**: https://diataxis.fr/explanation/

### support-deprecation
- **definition**: Support and deprecation documentation tells users where to ask for help, expected response times, supported versions, deprecation policy, security-report path, and how to request a feature or exception. It separates public support from private vulnerability reporting and makes unsupported behavior visibly unsafe to rely on.
- **implementation**:
  - Publish support channels, triage scope, response expectations, required report details, and escalation rules.
  - Maintain a supported-version matrix with end-of-support dates and upgrade guidance.
  - Define deprecation notice, warning, migration, sunset, and exception practices.
  - Link a private security advisory/reporting path and instruct users not to disclose secrets or vulnerabilities publicly.
- **probe**: An assessor follows support, security, and deprecation links, verifies channels are reachable and owned, compares supported versions with release policy, and checks an example deprecated feature has notice, timeline, and migration instructions.
- **failure_modes**: A user reports a security vulnerability in a public issue and exposes sensitive details because the private path was unclear. Customers continue using an unsupported version after its breaking deprecation because no support matrix or sunset notice exists.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors

### issue-templates
- **definition**: Issue and feature-request templates collect the environment, version, reproduction steps, expected and actual behavior, impact, and relevant logs while warning authors to remove secrets. Templates turn vague reports into triageable evidence without encouraging sensitive data disclosure.
- **implementation**:
  - Provide separate bug, feature, and security-report paths with required fields appropriate to each.
  - Ask for version, OS/runtime, deployment mode, minimal reproduction, expected result, actual result, and impact.
  - Include explicit instructions to redact tokens, credentials, personal data, and proprietary payloads from logs.
  - Add labels, ownership routing, and links to support or private security reporting.
- **probe**: `for template in .github/ISSUE_TEMPLATE/*; do parse_template "$template"; require_fields version environment reproduction expected actual impact; require_redaction_warning "$template"; done`; verify security issues route away from public templates.
- **failure_modes**: Maintainers receive a vague bug report with no version or reproduction and cannot triage it during an incident. A user pastes credentials into a public ticket because the template lacked redaction guidance.
- **severity**: nice-to-have
- **applies_if**: all
- **sources**: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests

### docs-accessibility
- **definition**: Public documentation is rendered with semantic structure and controls that work for keyboard and assistive-technology users. Informative images have meaningful alternative text, links describe their destination, headings are ordered, and contrast and focus states meet the configured WCAG target.
- **implementation**:
  - Use semantic headings, landmarks, lists, tables, code blocks, labels, and keyboard-operable navigation controls.
  - Require alt text for informative images and empty alt text for decorative images; avoid link text such as "click here."
  - Test color contrast, visible focus, responsive reflow, skip links, and accessible error/search states.
  - Run automated accessibility checks in CI and manually test representative pages with keyboard and a screen reader.
- **probe**: `make docs-build`; `pa11y --standard WCAG2AA --threshold 0 <published-or-local-docs-url>` (or equivalent); fail on configured serious/critical violations and missing image alternatives, then manually verify keyboard navigation and focus on representative pages.
- **failure_modes**: Keyboard and screen-reader users cannot reach a critical recovery procedure because navigation controls are not accessible. Missing image alternatives hide architecture or setup information from users relying on assistive technology.
- **severity**: nice-to-have
- **applies_if**: all
- **sources**: https://www.w3.org/WAI/standards-guidelines/wcag/
