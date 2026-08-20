# Documentation & developer experience — research wave 1

Source: scout report (gpt-5.6-luna, wave 4). Raw item list, pre-synthesis.

### readme-purpose
- **what**: State the product's audience, problem solved, supported use cases, and non-goals near the top of README.
- **why**: Users otherwise adopt the wrong component or form incorrect expectations before discovering limitations.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes

### readme-quickstart
- **what**: Provide a copy-paste quickstart that takes a clean checkout to one successful, observable result.
- **why**: An undocumented first run turns evaluation and incident reproduction into guesswork and abandons new users.
- **check**: probe
- **probe**: In a clean temporary checkout, execute only the README quickstart commands and assert they exit zero and produce the documented result or URL.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes

### readme-prerequisites
- **what**: Pin or state supported operating systems, runtimes, package managers, external services, credentials, and minimum versions before setup.
- **why**: Hidden prerequisites cause environment-specific failures that appear to be product defects.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes

### readme-configuration
- **what**: Document every required and materially behavior-changing configuration value with type, default, example, secret handling, and restart or reload behavior.
- **why**: Operators otherwise change undocumented settings blindly or ship invalid and accidentally insecure configurations.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/config

### devcontainer-reproducibility
- **what**: Supply a devcontainer or equivalent reproducible environment that installs the declared tools and exposes the documented test and run commands.
- **why**: Contributor onboarding drifts across laptops when tool versions and system dependencies are implicit.
- **check**: probe
- **probe**: Parse `.devcontainer/devcontainer.json` or equivalent, build it in CI, run the documented setup command inside it, and assert the declared runtime and dependency versions are available.
- **applies_if**: all
- **severity**: critical
- **sources**: https://containers.dev/implementors/spec/

### make-entrypoints
- **what**: Provide a small, discoverable set of deterministic `make` or equivalent task entrypoints for setup, test, lint, docs, run, and clean operations.
- **why**: Contributors invoke inconsistent ad hoc commands and cannot reproduce CI or support instructions.
- **check**: probe
- **probe**: Parse the task file for `setup`, `test`, `lint`, `docs`, and `run` targets, invoke each in an isolated checkout, and assert documented exit statuses.
- **applies_if**: all
- **severity**: important
- **sources**: https://www.gnu.org/software/make/manual/make.html

### local-parity
- **what**: Explain which local services, versions, flags, fixtures, and data-loading steps intentionally match production and identify every remaining difference.
- **why**: Silent local-versus-production differences make bugs unreproducible and invalidate pre-release confidence.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/dev-prod-parity

### onboarding-path
- **what**: Define and periodically rehearse a shortest-path onboarding flow that gets a new contributor from clone to a first meaningful change in less than one working day.
- **why**: Excessive time-to-first-change signals undocumented dependencies and makes team capacity depend on tribal knowledge.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://diataxis.fr/tutorials/

### troubleshooting-guide
- **what**: Document common setup and runtime failures with symptoms, diagnostic commands, likely causes, fixes, and an escalation destination.
- **why**: Repeated failures consume maintainer time and encourage unsafe trial-and-error fixes.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://diataxis.fr/how-to-guides/

### architecture-overview
- **what**: Keep a current architecture overview that names major components, ownership boundaries, data flows, external dependencies, and trust boundaries.
- **why**: Missing system context causes changes that violate hidden coupling and slows incident diagnosis.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://c4model.com/

### architecture-source
- **what**: Store architecture diagrams as editable source beside the code and render them reproducibly with ownership and a last-reviewed signal.
- **why**: Hand-edited screenshots become stale, unreviewable, and impossible to update during a structural change.
- **check**: probe
- **probe**: Locate diagram source files and their render command, run the command in CI, and compare generated output or hashes with the checked-in artifact.
- **applies_if**: all
- **severity**: important
- **sources**: https://mermaid.js.org/intro/

### adr-records
- **what**: Record consequential architectural decisions with context, decision, alternatives considered, consequences, owner, date, and status.
- **why**: Future maintainers otherwise repeat rejected approaches or undo constraints without understanding their rationale.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://adr.github.io/

### adr-lifecycle
- **what**: Mark ADRs proposed, accepted, superseded, or deprecated and link each supersession from both the old and new record.
- **why**: An apparently authoritative obsolete decision misdirects implementation and operational work.
- **check**: probe
- **probe**: Parse ADR front matter or headings for an allowed status and verify every `superseded` or `deprecated` record links to an existing replacement ADR.
- **applies_if**: all
- **severity**: important
- **sources**: https://adr.github.io/

### rationale-comments
- **what**: Use inline comments only for non-obvious rationale, invariants, compatibility constraints, or rejected alternatives, while keeping implementation facts in code and tests.
- **why**: Comments that merely restate code go stale while missing rationale invites "simplifying" changes that reintroduce defects.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://diataxis.fr/explanation/

### openapi-contract
- **what**: Publish a complete, versioned OpenAPI document covering paths, parameters, request and response schemas, authentication, errors, and examples for every supported HTTP operation.
- **why**: An incomplete contract causes client incompatibilities and forces consumers to reverse-engineer behavior from implementation.
- **check**: probe
- **probe**: Parse the OpenAPI document, validate it with an OAS validator, enumerate registered routes, and fail when a route lacks an operation or required schema and response.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://spec.openapis.org/oas/latest.html

### openapi-generation
- **what**: Generate the OpenAPI artifact from code annotations or the authoritative schema source and fail CI when regeneration produces a diff.
- **why**: Hand-maintained API specifications silently diverge from deployed routes and validation behavior.
- **check**: probe
- **probe**: Run the repository's documented spec-generation command in a clean checkout and compare its output byte-for-byte or semantically against the committed or published spec.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://spec.openapis.org/oas/latest.html

### api-examples
- **what**: Provide minimal runnable examples for each important API workflow, including authentication, valid payloads, expected responses, and representative failures.
- **why**: Consumers can possess a schema yet still fail integration because sequencing, headers, or error handling are undocumented.
- **check**: probe
- **probe**: Extract examples from docs or an examples directory, run them against a local or mocked service, and assert each expected status and response shape.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://spec.openapis.org/oas/latest.html

### api-behavior
- **what**: Document API semantics that schemas cannot express, including idempotency, pagination, ordering, retries, rate limits, eventual consistency, and webhook delivery behavior.
- **why**: Clients implement unsafe assumptions when operational semantics are left implicit.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://spec.openapis.org/oas/latest.html

### api-versioning
- **what**: Define supported API versions, compatibility guarantees, deprecation windows, sunset communication, and migration examples.
- **why**: Consumers cannot plan upgrades and breaking changes arrive as surprise outages.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://semver.org/

### alert-runbook-links
- **what**: Attach a stable runbook URL to every actionable alert and ensure the alert identity in the runbook exactly matches the emitted rule.
- **why**: An alert without immediate diagnostic guidance increases mean time to acknowledge and recover.
- **check**: probe
- **probe**: Parse alert-rule files for runbook annotations or labels, resolve each URL, and fail if any actionable rule lacks a reachable runbook or if the runbook omits the rule identifier.
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/alerting-on-slos/

### runbook-procedure
- **what**: Make each runbook executable under pressure with impact, prerequisites, first five diagnostic commands, safe mitigations, verification, rollback, and stop conditions.
- **why**: Narrative-only documentation makes responders improvise during the highest-cost and least-context-rich failures.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/workbook/alerting-on-slos/

### runbook-escalation
- **what**: State severity-based escalation contacts, ownership, incident commander expectations, vendor dependencies, and when to declare or hand off an incident.
- **why**: Responders otherwise lose time locating an owner or escalate inconsistently while impact grows.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/sre-book/being-on-call/

### runbook-freshness
- **what**: Assign an owner and review cadence to every runbook, and update it after incidents or alert-rule changes.
- **why**: Unreviewed procedures encode retired dashboards, commands, and contacts that fail when needed.
- **check**: probe
- **probe**: Parse runbook metadata for owner and review date, compare dates with the policy threshold, and cross-check referenced alert IDs against current alert definitions.
- **applies_if**: all
- **severity**: important
- **sources**: https://sre.google/sre-book/part-III-practices/

### changelog-format
- **what**: Maintain a human-readable changelog grouped by release and change type with an unreleased section and links to release references.
- **why**: Users miss behavior changes and maintainers cannot reconstruct when regressions or migrations were introduced.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://keepachangelog.com/en/1.1.0/

### changelog-gate
- **what**: Require every user-visible change to add or deliberately waive a changelog entry during review.
- **why**: A changelog maintained only at release time omits precisely the small changes that break downstream users.
- **check**: probe
- **probe**: Inspect the pull-request workflow or changed-file policy for a required changelog path or explicit waiver label, and verify the rule runs on user-facing source changes.
- **applies_if**: all
- **severity**: critical
- **sources**: https://keepachangelog.com/en/1.1.0/

### release-notes
- **what**: Publish each release with user impact, highlights, fixed issues, breaking changes, migrations, known limitations, upgrade steps, and links to the full changelog.
- **why**: A version number alone leaves operators unable to assess risk or execute a safe upgrade.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes

### release-reproducibility
- **what**: Document the release process, source revision, artifact provenance, required approvals, validation gates, and rollback or yanking procedure.
- **why**: Ad hoc releases produce untraceable artifacts and make a bad release difficult to contain.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes

### contributing-guide
- **what**: Provide contribution instructions covering setup, branch and commit expectations, tests and docs required, review flow, and local commands that match CI.
- **why**: Contributors otherwise submit changes that cannot be reproduced, reviewed, or merged efficiently.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors

### docs-codeowners
- **what**: Assign CODEOWNERS for source, API schemas, architecture diagrams, runbooks, and user documentation with a reachable team owner.
- **why**: Documentation silently decays when no accountable reviewer is required for changes.
- **check**: probe
- **probe**: Parse every CODEOWNERS rule, verify documentation and operational paths are covered, and check each owner resolves to an existing team or user through repository metadata.
- **applies_if**: monorepo
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners

### pr-conventions
- **what**: Document pull-request scope, required evidence, migration notes, screenshots for UI changes, and reviewer responsibilities in a concise checklist.
- **why**: Inconsistent review evidence lets documentation, compatibility, and operational impact escape normal scrutiny.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests

### docs-ci
- **what**: Build documentation in CI and fail on broken internal links, missing included files, malformed metadata, unresolved references, and stale generated pages.
- **why**: Documentation defects reach users unnoticed because application tests do not exercise docs navigation or rendering.
- **check**: probe
- **probe**: Run the documented docs build and link checker in a clean checkout, assert zero non-success exit codes, and scan generated output for unresolved links or placeholders.
- **applies_if**: all
- **severity**: critical
- **sources**: https://diataxis.fr/

### docs-example-tests
- **what**: Execute code samples, command snippets, configuration fragments, and API examples in CI or explicitly mark them as illustrative rather than runnable.
- **why**: Copy-pasted examples that no longer compile or run are a high-frequency source of failed adoption.
- **check**: probe
- **probe**: Enumerate fenced code blocks and example scripts with the repository's docs-test command, execute supported-language snippets, and fail on nonzero status or drifted expected output.
- **applies_if**: all
- **severity**: important
- **sources**: https://diataxis.fr/tutorials/

### docs-navigation
- **what**: Organize docs by user task and link README, tutorials, how-to guides, reference, explanations, API docs, and operations without orphan pages.
- **why**: Users cannot find the right level of guidance and duplicate unofficial instructions proliferate.
- **check**: probe
- **probe**: Build the docs site or link graph, assert required landing pages are reachable from navigation, and fail on pages with no inbound link except intentional index or generated pages.
- **applies_if**: all
- **severity**: important
- **sources**: https://diataxis.fr/

### docs-versioning
- **what**: Version documentation that describes versioned behavior and clearly label the default, archived, and end-of-life versions.
- **why**: Users following current instructions against older deployments can trigger incompatible requests or migrations.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://diataxis.fr/reference/

### glossary
- **what**: Maintain a concise glossary for domain terms, acronyms, state names, and identifiers used across code, API, alerts, and runbooks.
- **why**: Ambiguous vocabulary causes incorrect implementation and slows cross-team incident communication.
- **check**: judgment
- **applies_if**: all
- **severity**: nice-to-have
- **sources**: https://diataxis.fr/explanation/

### support-deprecation
- **what**: Document support channels, response expectations, supported versions, deprecation policy, security-report path, and a clear way to request help.
- **why**: Users route urgent failures to the wrong place and continue relying on unsupported behavior without warning.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors

### issue-templates
- **what**: Provide issue and feature-request templates that request environment, reproduction steps, expected behavior, impact, and relevant logs without secrets.
- **why**: Maintainers cannot triage vague reports and responders may receive sensitive data in public tickets.
- **check**: probe
- **probe**: Parse `.github/ISSUE_TEMPLATE` or equivalent and assert templates contain fields for version, environment, reproduction, expected result, actual result, and impact.
- **applies_if**: all
- **severity**: nice-to-have
- **sources**: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests

### docs-accessibility
- **what**: Render public documentation with semantic headings, descriptive link text, alt text for informative images, keyboard-accessible controls, and sufficient contrast.
- **why**: Inaccessible documentation blocks users and hides operational guidance from assistive technology users.
- **check**: probe
- **probe**: Build the docs site and run an automated WCAG accessibility audit, failing on configured serious or critical violations and missing image alternatives.
- **applies_if**: all
- **severity**: nice-to-have
- **sources**: https://www.w3.org/WAI/standards-guidelines/wcag/
