# Deployment & release engineering glossary

### ci-stage-gates
- **definition**: A deployment pipeline is an ordered set of deterministic build, test, security, packaging, and deploy-validation stages. Production promotion is possible only when every required predecessor reports success, and a failed or skipped gate blocks the dependent release.
- **implementation**:
  - Define separate workflow jobs for build, unit, integration, security, package, and deployment validation.
  - Express dependencies with `needs` (or the CI system's equivalent) and make deploy jobs require successful results, not merely job completion.
  - Pin required checks to protected production branches/tags and prevent manual bypass except through an audited break-glass path.
  - Upload machine-readable test and scan results as artifacts for the release record.
- **probe**: Parse every CI workflow's production deployment jobs, recursively resolve `needs`, and assert paths include required test, security, package, and validation jobs. Verify a simulated non-zero predecessor result prevents deployment (for example, dispatch a staging workflow with a deliberately failing gate and assert no deploy event is emitted).
- **failure_modes**: A package with a failing dependency scan is promoted because the deploy job ran independently; a skipped integration job is treated as success and a regression reaches production; a reordered migration-validation step tests the wrong artifact.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions; https://slsa.dev/spec/v1.0/requirements

### ci-feedback-budget
- **definition**: CI spends its feedback budget by running cheap deterministic checks before expensive integration and end-to-end work, while independent jobs execute concurrently. Dependency caches are keyed by the lockfile and relevant toolchain so speed does not conceal input drift.
- **implementation**:
  - Put formatting, type, unit, and static checks in an early job and gate slower jobs on their results.
  - Remove unnecessary `needs` edges so independent matrices run in parallel.
  - Key caches with operating system, runtime/toolchain version, and a hash of each dependency lockfile.
  - Set explicit timeouts and report queue, execution, and cache-hit durations to identify budget regressions.
- **probe**: Parse workflow `needs`, matrices, timeout, and cache-key fields; construct the dependency graph and flag independent jobs with unnecessary edges. Assert cache keys contain the relevant lockfile or toolchain hash and run two jobs with changed lockfiles to confirm they do not reuse the prior cache.
- **failure_modes**: A stale dependency cache leaves CI green after a lockfile update; serial jobs turn a five-minute change into an hour-long queue; an expensive end-to-end job runs after an obvious compile failure.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows; https://docs.gitlab.com/ci/pipelines/

### ci-reproducible-inputs
- **definition**: CI builds from a clean checkout with pinned runners, toolchains, actions, base images, and lockfile-resolved dependencies. Mutable network inputs such as floating tags or unconstrained package ranges are not allowed to change the result of a rebuild.
- **implementation**:
  - Commit and enforce lockfiles for every package manager that supports them, using frozen/locked install modes.
  - Pin actions and container bases to immutable versions or digests, and pin language/toolchain versions in CI and image build files.
  - Build in a clean, isolated workspace with network access limited to declared dependency sources.
  - Record source revision, toolchain, dependency, and base-image identifiers in build metadata.
- **probe**: Parse manifests, lockfiles, Dockerfiles, and workflow setup steps. Fail on missing supported lockfiles, floating action or base-image tags, unpinned toolchain setup, or install commands that ignore lockfiles; rebuild the same revision twice in clean workers and compare artifact digests.
- **failure_modes**: A newly published transitive dependency breaks a rebuild of an old commit; `latest` changes the runtime between staging and production; a compromised package mirror injects different code into a release.
- **severity**: critical
- **applies_if**: all
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://reproducible-builds.org/docs/definition/

### ci-production-protection
- **definition**: Production deployment is reachable only from protected branches or tags after required status checks, reviewed changes, and an authenticated environment gate. The policy applies to every production path, including manual and emergency workflows.
- **implementation**:
  - Require branch or tag protection, review count, CODEOWNERS review where appropriate, and up-to-date required checks.
  - Bind production deploy jobs to an environment that requires authorized reviewers and restrict permitted branches/tags.
  - Require short-lived workload identity for CI and audit deployment approvals and overrides.
  - Deny credentials or deploy permissions to pull-request workflows from untrusted forks.
- **probe**: Query repository protection and deployment-environment settings; enumerate all workflows/jobs that can target production and assert required checks, reviews, ref restrictions, and approvals on each path. Attempt an unreviewed or unprotected ref in a staging policy test and confirm authorization is denied.
- **failure_modes**: A maintainer deploys an unreviewed branch directly to production; a manual workflow bypasses the normal checks; a forked pull request obtains production credentials through an inherited secret.
- **severity**: critical
- **applies_if**: all
- **sources**: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches; https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment

### artifact-build-once
- **definition**: CI produces one tested, immutable artifact identified by a digest or content hash and promotes that exact identifier through environments. Promotion changes references and configuration only; it never rebuilds or resolves a mutable tag.
- **implementation**:
  - Publish the artifact once after tests and capture its digest in a release manifest.
  - Pass the digest as an explicit immutable output to staging, canary, and production jobs.
  - Reject `latest`, branch tags, or environment-time builds in deployment manifests.
  - Verify the deployed runtime reports the same digest recorded by CI.
- **probe**: Inspect pipeline steps, artifact publication, and deployment manifests. Assert publication occurs once per revision and every environment consumes the same digest; deploy a test release and compare registry, release-record, and runtime digests.
- **failure_modes**: A rebuild picks up a changed dependency after tests; production runs a different image than the one approved in staging; rollback to a tag silently selects a newer image.
- **severity**: critical
- **applies_if**: all
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://github.com/opencontainers/image-spec/blob/main/descriptor.md

### artifact-provenance
- **definition**: Each release artifact has verifiable signed provenance and an SBOM that link it to its source revision, builder, inputs, and build steps. The attestations are addressable by the artifact digest and are checked before promotion.
- **implementation**:
  - Generate an in-toto/SLSA provenance attestation and SPDX or CycloneDX SBOM during the controlled build.
  - Sign attestations with a keyless or managed signing identity and publish them alongside the registry artifact.
  - Include source commit, builder identity, dependency inputs, build parameters, and artifact digest.
  - Require registry verification (for example, `cosign verify-attestation`) and SBOM presence in the promotion gate.
- **probe**: For a release digest, run `cosign verify-attestation --type slsaprovenance <image>` with the expected identity and issuer, then retrieve and parse its SBOM. Fail if either is absent, does not match the digest, or cannot be cryptographically verified.
- **failure_modes**: Responders cannot determine which source built a compromised image; an unsigned registry push is promoted after CI was bypassed; an untracked vulnerable dependency cannot be located during incident response.
- **merges_into**: sbom-provenance
- **severity**: critical
- **applies_if**: all
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://docs.sigstore.dev/cosign/verifying/verify-attestation/

### artifact-retention
- **definition**: The registry retains immutable release digests and their metadata for at least the documented rollback and forensic window. Production references use digests, while mutable production tags are rejected or cannot overwrite an existing release.
- **implementation**:
  - Configure retention and legal/incident holds to exceed rollback and investigation requirements.
  - Enable immutable tags or registry policies that prevent overwrites and deletion of referenced digests.
  - Record artifact digest, manifest, attestations, and deployment references in durable release metadata.
  - Periodically test restoring a retained artifact after ordinary garbage-collection cycles.
- **probe**: Query registry retention, immutability, and deletion policies and list deployment references. Assert every production reference is a retained digest beyond the rollback window; attempt tag overwrite/deletion in a non-production registry and expect denial.
- **failure_modes**: An outage removes the only known-good image through garbage collection; a reused tag makes rollback pull different bits; investigators lose provenance metadata before the forensic review begins.
- **severity**: important
- **applies_if**: all
- **sources**: https://github.com/opencontainers/image-spec/blob/main/descriptor.md; https://kubernetes.io/docs/concepts/containers/images/

### trunk-short-lived
- **definition**: The production-bound branch remains continuously releasable, with small changes merged quickly and incomplete work hidden behind controlled flags. Integration happens frequently rather than through long-lived divergence.
- **implementation**:
  - Set a target maximum branch lifetime and monitor age of open branches and pull requests.
  - Use small vertical changes, backward-compatible interfaces, and feature flags for incomplete behavior.
  - Keep the default branch protected, continuously tested, and deployable.
  - Require an owner and explicit removal plan for flags used to stage incomplete work.
- **probe**: **Evidence to inspect:** branch-age and merge-frequency reports, recent pull requests, release history, flag usage, and documented exceptions. An assessor should verify the default branch is releasable and any branch exceeding the stated lifetime has a recorded reason and owner.
- **failure_modes**: A quarter-long branch merges with hidden conflicts and breaks production; a security fix waits behind unrelated unfinished work; a release branch diverges and receives a different untested dependency update.
- **severity**: important
- **applies_if**: all
- **sources**: https://trunkbaseddevelopment.com/

### branch-policy
- **definition**: Every long-lived release branch has a documented purpose and lifecycle rather than becoming an accidental second mainline. Its owner, cut criteria, backport rules, and end-of-life date make differences from the production-bound branch intentional and reviewable.
- **implementation**:
  - Record branch owner, supported fixes, cut/release criteria, and end-of-life date in repository policy.
  - Automate branch protection and restrict who may push or backport.
  - Require backports to reference the originating change and run the branch's complete gates.
  - Alert before end-of-life and delete or archive branches on schedule.
- **probe**: **Evidence to inspect:** all branches with age beyond the short-lived threshold, policy records, owners, backport pull requests, and end-of-life status. For each long-lived branch, confirm a documented purpose, owner, cut policy, backport rule, and date.
- **failure_modes**: A hotfix is applied only to a release branch and never reaches main; an unsupported branch continues receiving security patches without tests; operators cannot tell which branch produced a deployed binary.
- **severity**: important
- **applies_if**: all
- **sources**: https://trunkbaseddevelopment.com/; https://docs.github.com/en/get-started/using-github/github-flow

### rolling-safety
- **definition**: A rolling release replaces instances while readiness gates and bounded surge/unavailable settings preserve service capacity. Revision history and disruption budgets provide a known rollback path and prevent voluntary disruption from removing too much capacity.
- **implementation**:
  - Define startup/readiness probes that represent actual dependency readiness and set bounded `maxUnavailable` and `maxSurge`.
  - Configure revision history sufficient for the rollback window and a PodDisruptionBudget or platform equivalent.
  - Set resource requests/limits and rollout progress deadlines so scheduling and stalled updates are visible.
  - Validate graceful termination and load-balancer deregistration before replacement.
- **probe**: Parse rollout manifests and assert startup/readiness probes, bounded `maxUnavailable`, `maxSurge`, revision history, and disruption-budget settings. In staging, roll a revision under representative load and verify capacity and traffic remain within the stated bounds.
- **failure_modes**: All replicas are terminated together because `maxUnavailable` was unbounded; traffic reaches a process before migrations/configuration finish; an operator cannot roll back because old revisions were garbage-collected.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/; https://kubernetes.io/docs/tasks/run-application/configure-pdb/

### blue-green-switch
- **definition**: Blue-green deployment keeps the known-good (blue) and candidate (green) versions independently runnable and validates green before one controlled traffic switch. Blue remains available for immediate reversal until compatibility and stability are established.
- **implementation**:
  - Deploy blue and green with distinct immutable versions and independently addressable services.
  - Route a validation hostname or internal traffic to green before changing the production route.
  - Perform smoke, compatibility, data-integrity, and capacity checks before an atomic DNS, load-balancer, or service-selector switch.
  - Retain blue and its dependencies for the rollback window, then garbage-collect through a controlled policy.
- **probe**: **Evidence to inspect:** routing configuration, two live deployment revisions, validation results, switch authorization, and rollback procedure. Confirm the traffic switch is atomic or bounded, green was tested before production routing, and blue remains runnable after the switch.
- **failure_modes**: The old version is destroyed before the new one is validated; a selector update sends only half the traffic to an incompatible schema; rollback fails because blue's dependencies were removed.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://martinfowler.com/bliki/BlueGreenDeployment.html

### canary-analysis
- **definition**: Canary analysis exposes a candidate to bounded traffic steps and evaluates version-scoped reliability, latency, saturation, and business metrics for a minimum observation window. Promotion occurs only when thresholds pass; a breach aborts or rolls back automatically.
- **implementation**:
  - Define traffic weights and hold durations for each canary step, including a small initial exposure.
  - Query metrics with candidate and baseline version labels, using minimum sample sizes and explicit thresholds.
  - Include error rate, tail latency, resource saturation, and domain-specific success metrics.
  - Configure abort action, promotion timeout, and notification with an auditable analysis result.
- **probe**: Parse rollout and analysis configuration for traffic steps, hold duration, metric queries, thresholds, sample windows, and abort action. In a non-production environment, inject a controlled candidate error/latency regression and assert analysis fails before full promotion.
- **failure_modes**: A canary passes because metrics average candidate and baseline together; promotion occurs before enough requests arrive; a business conversion regression is invisible to infrastructure health checks.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://sre.google/workbook/canarying-releases/

### release-observability
- **definition**: Release telemetry attributes logs, metrics, traces, and alerts to both the old and new version during rollout. Version identity is present before traffic shifts so analysis and responders can compare candidate behavior rather than mixed aggregates.
- **implementation**:
  - Emit a normalized release identifier, source revision, and artifact digest as resource attributes and structured log fields.
  - Propagate version labels through request traces, service metrics, dashboards, and alert queries without high-cardinality user data.
  - Keep baseline and candidate panels and alert thresholds separately queryable during rollout.
  - Validate telemetry from the canary path before enabling additional traffic.
- **probe**: Send a uniquely identifiable request through a staging rollout and query logs, metrics, and traces by release identifier. Assert old/new labels and source revision or digest appear consistently and that alerts can isolate candidate traffic.
- **failure_modes**: A bad candidate is averaged with healthy instances and promoted; responders spend an outage searching logs with no release boundary; tracing follows a request but loses the deployed version at a downstream service.
- **merges_into**: release-telemetry-attribution
- **severity**: important
- **applies_if**: web-api
- **sources**: https://sre.google/workbook/canarying-releases/; https://opentelemetry.io/docs/specs/semconv/

### automated-rollback
- **definition**: A release controller halts promotion and restores the last known-good artifact when health, analysis, or SLO gates breach. Rollback is based on the immutable prior identifier and leaves an auditable outcome rather than merely stopping new instances.
- **implementation**:
  - Store the prior known-good digest and rollout revision in controller state and the release record.
  - Configure abort thresholds, observation windows, rollback timeout, and notification recipients.
  - Ensure rollback restores compatible configuration and traffic routing, not only the container image.
  - Exercise rollback in staging with injected errors and verify post-rollback health and data integrity.
- **probe**: Parse rollout-controller policy and run a staging rollout with a controlled failing candidate. Assert promotion aborts, traffic returns to the prior revision/digest, health recovers, and the release event records the reason.
- **failure_modes**: A broken release receives all traffic while an alert waits for a human; rollback selects a mutable tag and restores the wrong image; the controller marks rollback complete while traffic still targets the candidate.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://sre.google/workbook/canarying-releases/; https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#rolling-back-a-deployment

### rollback-blockers
- **definition**: Rollback policy names conditions in which returning to the prior release is unsafe, including irreversible schema changes, unavailable artifacts, insufficient capacity, and incompatible dependencies. For each blocker it defines a safe stop, forward-fix, or recovery action and an owner.
- **implementation**:
  - Maintain a release checklist that evaluates schema, data, dependency, capacity, and artifact compatibility before promotion.
  - Mark migrations and configuration changes as reversible, backward-compatible, or forward-fix-only.
  - Keep a tested prior artifact and required runtime dependencies available for the rollback window.
  - Document who can stop traffic, freeze writes, or approve a forward fix when rollback is blocked.
- **probe**: **Evidence to inspect:** rollback runbooks, migration compatibility annotations, artifact retention, dependency support matrix, capacity calculations, and a recent drill. Verify each listed blocker has a detection signal and an executable safe response rather than an unconditional rollback command.
- **failure_modes**: An old binary cannot read a newly destructive schema; rollback overloads the reduced cluster; a missing image forces responders to improvise during an outage.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://martinfowler.com/articles/evodb.html; https://sre.google/sre-book/release-engineering/

### feature-flag-contract
- **definition**: Every release flag has explicit metadata describing its owner, purpose, type, default, target scope, and expiry or removal issue. The contract makes flag behavior and lifecycle reviewable instead of leaving semantics in scattered conditionals.
- **implementation**:
  - Store flags in a typed registry with owner, purpose, default, targeting rules, created date, and expiry/removal ticket.
  - Validate flag names and types at startup or build time and reject unknown or malformed definitions.
  - Define safe defaults per environment and restrict who can change production targeting.
  - Emit audit events for flag changes without logging sensitive targeting data.
- **probe**: Scan the flag registry/configuration and source annotations; assert required metadata, valid ownership, type consistency, and an expiry or removal reference for every flag. Fail on source references to unregistered flags.
- **failure_modes**: A flag defaults on during a flag-service outage and exposes an unfinished payment flow; an unowned flag remains active for years; a targeting typo enables an admin feature for all users.
- **severity**: important
- **applies_if**: spa
- **sources**: https://martinfowler.com/articles/feature-toggles.html

### feature-flag-cleanup
- **definition**: Temporary flags have an enforced expiry and cleanup path, and expired entries produce a blocking CI failure or an owned debt action. Removing a flag deletes its configuration and both obsolete code paths rather than merely turning it off.
- **implementation**:
  - Compare registry expiry dates to current time in CI and fail or open a ticket with an accountable owner.
  - Find source references, tests, documentation, and remote configuration for each expired flag.
  - Remove the conditional, retain the selected behavior, and delete obsolete variants and fixtures.
  - Record cleanup completion and prevent reintroduction of the old flag key.
- **probe**: Compare flag registry entries with source references and expiry dates; inject an expired test flag and assert CI fails or creates the documented owner action. Verify a cleanup change removes its branches and configuration references.
- **failure_modes**: Dead branches diverge and receive no security fix; a stale flag multiplies the test matrix; a deleted UI path remains reachable because remote configuration was not removed.
- **severity**: important
- **applies_if**: spa
- **sources**: https://martinfowler.com/articles/feature-toggles.html

### feature-flag-fail-safe
- **definition**: A kill switch has a documented safe default, authorized change boundary, audit trail, and explicit behavior when the flag service is unavailable. Failure of flag evaluation must not silently enable risky functionality or weaken authorization.
- **implementation**:
  - Classify each flag's failure mode and encode a local default that is safe for that classification.
  - Require authenticated, least-privilege operators for production changes and record actor, reason, old/new value, and time.
  - Cache only bounded, signed or integrity-protected values with a defined staleness limit.
  - Keep authorization decisions independent of an optional UI/rollout flag and test flag-service outage behavior.
- **probe**: **Evidence to inspect:** flag definitions, outage behavior tests, authorization code, access policy, audit events, and operator runbook. Confirm a service outage selects the documented safe default and that a flag cannot grant privilege by itself.
- **failure_modes**: A control-plane timeout enables a dangerous feature by default; an unauthorized operator changes a production kill switch with no audit record; cached rollout state remains active indefinitely after revocation.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://martinfowler.com/articles/feature-toggles.html; https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

### environment-parity
- **definition**: Environments deploy the same immutable artifact and versioned runtime/infrastructure definitions, with differences limited to an explicit configuration, scale, and isolated-data allowlist. Parity concerns execution semantics, not identical capacity or credentials.
- **implementation**:
  - Promote one artifact digest and one versioned IaC/module set through environments.
  - Keep environment differences in reviewed overlays or typed configuration, not ad hoc console edits.
  - Pin language runtimes, base images, service versions, and feature schemas consistently.
  - Maintain an allowlist for expected scale, domain, credential, and data differences.
- **probe**: Resolve each environment's manifests and IaC overlays and compare artifact digest, runtime versions, base images, and module versions. Fail on differences outside the approved variable/scale/data allowlist.
- **failure_modes**: Staging succeeds on a different runtime than production; a console-only production setting disappears during recreation; a staging dependency upgrade masks an incompatible production API.
- **severity**: critical
- **applies_if**: all
- **sources**: https://12factor.net/config; https://slsa.dev/spec/v1.0/requirements

### environment-data-isolation
- **definition**: Non-production environments use synthetic or scrubbed data and separately scoped services, credentials, domains, and quotas. Network and identity policy prevents staging or preview code from reading production data or mutating production systems.
- **implementation**:
  - Maintain separate cloud accounts/projects, namespaces, databases, service identities, and secret paths where feasible.
  - Generate synthetic fixtures or run an auditable scrub pipeline with tested removal of direct and quasi-identifiers.
  - Deny production CIDRs, endpoints, and credentials through network policy and identity conditions.
  - Label datasets and endpoints by environment and alert on cross-environment access attempts.
- **probe**: Scan environment manifests, secret references, endpoint configuration, and network policies for production references. From a non-production workload, attempt connections to production endpoints and assert deny; inspect dataset labels to confirm synthetic/scrubbed origin.
- **failure_modes**: A preview test reads customer records; a staging migration mutates the production database; production credentials copied into a shared secret namespace allow lateral access.
- **merges_into**: nonprod-data-isolation
- **severity**: critical
- **applies_if**: all
- **sources**: https://kubernetes.io/docs/concepts/security/secrets-good-practices/; https://kubernetes.io/docs/concepts/services-networking/network-policies/

### iac-review-plan
- **definition**: Infrastructure and deployment changes are version-controlled, reviewed, policy-checked, and applied only from a saved plan generated from the reviewed commit. The apply input is therefore the same intended change that reviewers examined.
- **implementation**:
  - Run formatting, validation, security policy, and plan jobs on pull requests using a read-only identity.
  - Store the plan as an integrity-protected artifact tied to commit, workspace, and tool version.
  - Require authorized approval before apply and verify the commit/plan checksum has not changed.
  - Restrict production apply credentials to the deployment environment rather than developer workstations.
- **probe**: Inspect IaC CI configuration and assert PR plan and policy checks, approval gates, and apply steps consume the same commit or saved plan. Change the reviewed commit after plan generation in a staging test and verify apply rejects the stale plan.
- **failure_modes**: A direct console change bypasses review; plan-after-review applies an unapproved resource; a stale plan deletes a resource after state changed.
- **severity**: critical
- **applies_if**: all
- **sources**: https://developer.hashicorp.com/terraform/tutorials/automation/automate-terraform; https://developer.hashicorp.com/terraform/cloud-docs/workspaces/run

### iac-drift
- **definition**: A scheduled read-only plan detects infrastructure changed outside version control and routes non-zero drift to an owner. Reconciliation or an exception is explicit, approved, and expires rather than silently accepting live state.
- **implementation**:
  - Schedule plans at a documented interval with the same provider/tool versions as apply.
  - Use detailed exit codes to distinguish no change, drift, and execution errors.
  - Notify the owning team with affected resources, severity, and a link to reconcile or approve an exception.
  - Require exception expiry and re-alert unresolved drift.
- **probe**: Inspect scheduler, read-only identity, plan command, exit-code handling, notifications, owner routing, and exception expiry. Introduce a harmless out-of-band change in a test workspace and assert drift is detected and routed without applying it.
- **failure_modes**: A firewall rule changed in the console remains open; the next apply unexpectedly replaces manually repaired infrastructure; drift notifications go nowhere and expire without ownership.
- **severity**: critical
- **applies_if**: all
- **sources**: https://developer.hashicorp.com/terraform/tutorials/cloud/drift-detection; https://developer.hashicorp.com/terraform/cli/commands/plan#detailed-exitcode

### iac-state-safety
- **definition**: IaC state is stored remotely with encryption, least-privilege access, concurrency locking, versioning/backups, and deliberate handling of sensitive values. State recovery and access are tested because losing or exposing it can affect the entire estate.
- **implementation**:
  - Use a remote encrypted backend with object versioning and a tested recovery process.
  - Enable state locking and fail concurrent writers rather than allowing last-write-wins.
  - Grant separate plan/apply/read roles and audit all state access.
  - Keep secrets out of state where possible; otherwise encrypt, restrict, and rotate them as sensitive data.
- **probe**: Parse backend and policy configuration and assert remote encryption, locking, backup/versioning, least-privilege roles, and audit logging. Run concurrent plan/apply and restore a prior state version in a test workspace, confirming safe failure and recovery.
- **failure_modes**: Concurrent applies overwrite state and orphan resources; a lost state file causes destructive recreation; a broadly readable state bucket exposes database passwords.
- **severity**: critical
- **applies_if**: all
- **sources**: https://developer.hashicorp.com/terraform/language/state/locking; https://developer.hashicorp.com/terraform/language/state/sensitive-data

### gitops-reconciliation
- **definition**: A GitOps controller continuously reconciles each environment to an auditable desired-state commit and reports divergence. CI publishes or approves the desired state; it does not require unrestricted imperative mutation of the live cluster.
- **implementation**:
  - Define one versioned application manifest or chart per environment with commit revision tracking.
  - Configure pull-based controller sync, health checks, drift reporting, and bounded retry behavior.
  - Give CI repository/write permissions but restrict direct cluster mutation credentials.
  - Require promotion PRs or signed image updates to be reviewable and traceable to a release record.
- **probe**: Inspect controller application configuration and CI steps; assert pull-based sync, commit revision tracking, health reporting, and no unrestricted `kubectl apply` (or equivalent) in production. Introduce test drift and confirm the controller reports or reconciles it according to policy.
- **failure_modes**: A CI script mutates live state that cannot be reproduced from Git; a hotfix is overwritten by reconciliation without an audit trail; controller health is green while the desired revision is unknown.
- **severity**: important
- **applies_if**: all
- **sources**: https://opengitops.dev/#principles; https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/

### gitops-break-glass
- **definition**: Emergency out-of-band changes are time-bound, authorized, and fully audited, with a required follow-up commit restoring Git as the source of truth. Break-glass is a controlled exception, not a second deployment process.
- **implementation**:
  - Provide a separate short-lived role requiring incident reference, approver, and reason.
  - Log actor, command/resource diff, start/end time, incident, and resulting live revision.
  - Alert on break-glass use and block reuse after expiry.
  - Require a reconciliation pull request and controller sync before closing the incident.
- **probe**: **Evidence to inspect:** break-glass role policy, access logs, recent emergency changes, incident links, and follow-up commits. Verify each exception has time-bounded authorization and that live state was reconciled to Git.
- **failure_modes**: An emergency console patch remains after the incident and is lost on the next sync; shared admin credentials prevent attribution; a temporary bypass becomes a standing privilege.
- **severity**: important
- **applies_if**: all
- **sources**: https://opengitops.dev/#principles; https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/

### secret-injection
- **definition**: Deployments obtain secrets from a managed store at release or runtime using workload identity, never embedding values in source, image layers, rendered manifests, or logs. Secret references and access policies are environment-scoped and auditable.
- **implementation**:
  - Use workload identity or short-lived token exchange to read a named secret path from a managed vault.
  - Inject through a runtime sidecar, CSI provider, or process environment only where necessary; do not commit rendered values.
  - Apply least-privilege read policies per workload and environment, with audit logging.
  - Add secret scanners for repository history, image layers, manifests, CI output, and crash/log pipelines.
- **probe**: Scan repository history, image layers, rendered manifests, and deployment logs for secret literals. Resolve deployment references and assert they point to external providers with identity-based access; use a canary secret to verify no value appears in logs or artifacts.
- **failure_modes**: A leaked image layer exposes a database password; a rendered manifest is copied into a public artifact store; a CI log prints a cloud token and permits account takeover.
- **merges_into**: secrets-management
- **severity**: critical
- **applies_if**: all
- **sources**: https://kubernetes.io/docs/concepts/security/secrets-good-practices/; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### secret-rotation
- **definition**: Secret rotation has a named owner, defined cadence, revocation path, and compatibility plan for consumers. Where simultaneous keys are required, a bounded dual-key overlap and deployment smoke test prevent rotation from causing an outage or leaving old credentials trusted indefinitely.
- **implementation**:
  - Track secret owner, class, last/next rotation, revocation procedure, and dependent workloads.
  - Prefer short-lived or automatically rotated credentials and revoke old values after confirmed cutover.
  - Support dual-key overlap only for a documented maximum interval and test both current and next values.
  - Alert on overdue rotation, failed consumers, and use of retired versions.
- **probe**: **Evidence to inspect:** secret inventory, rotation schedules, owner assignments, vault audit events, revocation runbooks, and a recent rotation drill. Confirm the consumer smoke test fails safely on invalid values and that the prior key is revoked at the documented deadline.
- **failure_modes**: A rotated database password takes down all workers because no overlap was supported; an ex-employee's static token remains valid; rotation succeeds in the vault but a cached application value keeps using the old credential.
- **merges_into**: secrets-management
- **severity**: important
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html; https://kubernetes.io/docs/concepts/security/secrets-good-practices/

### readiness-drain
- **definition**: A service distinguishes startup, readiness, and liveness, and removes itself from traffic before termination. Grace periods, connection timeouts, and idempotent retry behavior let in-flight work finish or be safely retried during replacement.
- **implementation**:
  - Use startup checks for initialization, readiness for dependency/traffic eligibility, and liveness only for process recovery.
  - On shutdown, fail readiness first, deregister from the load balancer, stop accepting new work, and drain existing connections.
  - Configure `preStop`/termination hooks, termination grace, server keep-alive, load-balancer deregistration delay, and request deadlines coherently.
  - Make retried operations idempotent and emit drain duration and forced-termination metrics.
- **probe**: Parse workload and service manifests for distinct probes, deregistration or `preStop`, termination grace, and timeout/retry policies. Exercise a rolling restart with active requests and assert new traffic stops before termination, in-flight requests complete or retry safely, and no forced drops exceed the policy.
- **failure_modes**: Deployments drop uploads when SIGTERM kills a pod immediately; a slow-starting process receives traffic and returns errors; clients retry a non-idempotent request and create duplicate orders.
- **merges_into**: health-check-contracts
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/; https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/

### migration-expand-contract
- **definition**: Schema changes are released in expand, migrate, and contract phases so old and new application versions remain mutually compatible during rollout and rollback. Destructive removal waits until all readers/writers and backfill validation have completed.
- **implementation**:
  - Expand with additive nullable columns, tables, indexes, or dual-write fields before changing application reads.
  - Deploy compatibility code, backfill in throttled resumable batches, and verify counts/checksums before switching reads.
  - Contract only after an observed compatibility window, removing old columns/indexes in a separate release.
  - Mark migrations transactional or online according to database behavior and acquire locks with bounded timeouts.
- **probe**: Parse migration files, application release ordering, and compatibility annotations; flag destructive DDL, non-null required columns, or renamed fields before the compatibility window/backfill. In a staging clone, run old and new versions concurrently and verify reads/writes during each phase.
- **failure_modes**: Rollback fails because the old binary queries a dropped column; a table rewrite locks production during peak traffic; an incomplete backfill makes new code return missing records.
- **merges_into**: db-migrations
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://martinfowler.com/articles/evodb.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-backup-restore
- **definition**: Before irreversible data changes, an automated backup and tested restore path are current, observable, and within agreed recovery point and recovery time objectives. A code rollback is not treated as data recovery.
- **implementation**:
  - Gate destructive migration jobs on a recent successful snapshot or point-in-time recovery marker.
  - Store backups encrypted and separately scoped, with retention covering the migration and forensic window.
  - Restore into an isolated database, run integrity checks and representative queries, and record restore duration and recovered timestamp.
  - Abort the migration when backup freshness, restore validation, or RPO/RTO evidence is missing.
- **probe**: Inspect migration pipeline gates and backup job records; assert a successful backup, restore validation result, recovered timestamp, and restore duration fall within documented RPO/RTO. Run a test restore from the selected backup and verify row/integrity checks.
- **failure_modes**: A destructive migration deletes customer data and the latest backup is corrupt; recovery takes days because restore was never timed; a backup contains an unusable schema version after the migration.
- **merges_into**: db-migrations
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://martinfowler.com/articles/evodb.html; https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-recovery-procedures.html

### capacity-surge-budget
- **definition**: Rollout policy reserves enough capacity for candidate and incumbent replicas together while respecting disruption budgets, autoscaling limits, and platform quota. Replacement concurrency is bounded by a calculated worst-case surge rather than an optimistic average.
- **implementation**:
  - Calculate `replicas × (requests + maxSurge)` against node/pod quota and autoscaler maximum.
  - Set resource requests and limits that reflect rollout-time scheduling and avoid overcommit assumptions.
  - Align `maxSurge`, `maxUnavailable`, PDB, HPA, and cluster quota values in one reviewed policy.
  - Alert on pending rollout pods, quota exhaustion, and capacity headroom below the surge budget.
- **probe**: Parse resource requests/limits, autoscaling, disruption budgets, quotas, and rollout parameters; compute worst-case candidate-plus-incumbent demand and assert it fits available or autoscalable capacity. Run a staging rollout at peak configured replicas and observe no unschedulable surge pods.
- **failure_modes**: New pods remain pending and old pods are terminated by timeout; autoscaling hits quota during a rollout; a PDB and maxSurge combination deadlocks replacement.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/; https://kubernetes.io/docs/tasks/run-application/configure-pdb/

### preview-environments
- **definition**: Each pull request can deploy its tested immutable artifact to an isolated, discoverable preview with a stable URL, smoke checks, owner metadata, and automatic expiry. Preview lifecycle is tied to the pull request so abandoned environments do not persist indefinitely.
- **implementation**:
  - Trigger creation on pull-request open/update and deploy the exact CI-tested digest.
  - Allocate an isolated namespace/project and report a stable URL and commit/owner in the pull request.
  - Run health, smoke, and teardown checks before marking the preview ready.
  - Set a TTL, delete on merge/close, and run a janitor for orphaned resources.
- **probe**: Parse CI and environment configuration and assert PR trigger, artifact digest pinning, URL reporting, smoke-job execution, owner metadata, and TTL teardown. Open a test pull request, verify URL and smoke status, then close it and assert resources are removed after the configured grace period.
- **failure_modes**: A preview runs untested code because it rebuilt from the branch; stale previews consume quota and expose old dependencies; a missing owner leaves a failed environment without cleanup.
- **merges_into**: preview-environments
- **severity**: important
- **applies_if**: spa
- **sources**: https://docs.gitlab.com/ci/review_apps/

### preview-data-safety
- **definition**: Preview environments treat pull-request code as untrusted and prevent access to production data or privileged services. They use synthetic/scrubbed fixtures and short-lived, narrowly scoped credentials with network deny rules.
- **implementation**:
  - Run previews in isolated namespaces/accounts with default-deny egress and explicit allowlists for test dependencies.
  - Provision synthetic or scrubbed fixture data per preview, with automatic deletion at teardown.
  - Issue short-lived identities scoped to preview resources and never mount production secret paths.
  - Add admission and policy checks that reject production endpoints, domains, CIDRs, or credential references.
- **probe**: Inspect preview namespace, network, identity, and secret policies; assert deny rules for production endpoints and absence of production secret references. From preview code, attempt production DNS/network and secret access and verify denial, then inspect fixture provenance and teardown.
- **failure_modes**: A malicious pull request exfiltrates production records through an allowed egress path; a preview uses a production API key; preview fixtures retain personal data after the pull request closes.
- **merges_into**: nonprod-data-isolation
- **severity**: critical
- **applies_if**: spa
- **sources**: https://kubernetes.io/docs/concepts/services-networking/network-policies/; https://kubernetes.io/docs/concepts/security/secrets-good-practices/

### dora-four-keys
- **definition**: The four DORA delivery metrics measure deployment frequency, lead time for changes, change fail rate, and time to restore service from consistent deployment, commit, and incident events. Each metric has a defined population and time window so speed is evaluated alongside reliability.
- **implementation**:
  - Emit deployment events with environment, revision, artifact, start/end, outcome, and rollback identifiers.
  - Link commits/merge requests to deployments and incidents through stable IDs rather than title matching.
  - Define change-failure and recovery rules, exclusions, timezone, and reporting windows in versioned metric code.
  - Dashboard distributions and trends with filters for service/environment, not only a single aggregate.
- **probe**: Inspect event schemas and dashboard queries, select a fixed reporting period, and recompute all four metrics from deployment, commit, and incident identifiers. Compare the independently computed values with dashboard output and document any excluded events.
- **failure_modes**: Deployment frequency looks high because retries are counted as separate releases; recovery time is missing because incidents are not linked to deploys; optimizing lead time increases failed changes unnoticed.
- **severity**: important
- **applies_if**: all
- **sources**: https://dora.dev/guides/dora-metrics-four-keys/; https://cloud.google.com/blog/products/devops-sre/using-the-four-keys-to-measure-your-devops-performance

### dora-event-integrity
- **definition**: Delivery metric events use a consistent timezone, stable commit-to-deployment association, explicit inclusion/exclusion rules, and versioned definitions across services. Recomputing a known sample produces the same result regardless of dashboard or service implementation.
- **implementation**:
  - Store timestamps in UTC with source clock and event-ingestion metadata where needed.
  - Require immutable deployment, commit, environment, outcome, and incident IDs with uniqueness constraints.
  - Version metric definitions and publish denominator/exclusion rules with the dashboard.
  - Reconcile provider events periodically and quarantine duplicates or late corrections rather than silently changing history.
- **probe**: Query event storage and metric code; assert required IDs/timestamps, timezone normalization, documented filters, and version identifiers. Recompute metrics for a known fixture containing retries, rollbacks, and late events and verify deterministic results.
- **failure_modes**: Services use local time and create negative lead times; duplicate webhook delivery inflates deployment frequency; changing the denominator makes this quarter's trend incomparable with last quarter's.
- **severity**: important
- **applies_if**: all
- **sources**: https://dora.dev/guides/dora-metrics-four-keys/; https://cloud.google.com/blog/products/devops-sre/using-the-four-keys-to-measure-your-devops-performance

### deployment-audit-trail
- **definition**: A searchable release record links approver, source revision, artifact digest and provenance, IaC plan, target environment, rollout outcome, and rollback reason. It provides a chain of custody from reviewed change to running bits during incidents and audits.
- **implementation**:
  - Generate one immutable release ID and use it across CI, registry, deployment controller, telemetry, and incident systems.
  - Store source commit, artifact digest, attestations/SBOM, builder, plan checksum, environment, approver, timestamps, and result.
  - Record rollout steps, health analysis, rollback/forward-fix decision, and operator or controller actor.
  - Retain records for the operational and compliance window with access audit and tamper resistance.
- **probe**: Inspect deployment event schema and query a recent release by ID; assert all listed fields are emitted, searchable, immutable, and linked to the deployed digest and outcome. Compare the record with controller and registry facts.
- **failure_modes**: During an outage nobody can identify the changed artifact; an approval exists but not the plan that was applied; rollback reason is lost and the same unsafe release is redeployed.
- **severity**: critical
- **applies_if**: all
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://opengitops.dev/#principles

### release-approval-boundary
- **definition**: The team explicitly decides which environments auto-promote and which require human approval, tying approvals to release evidence rather than blanket ceremony. The boundary is documented, enforced by the deployment system, and reviewed when risk changes.
- **implementation**:
  - Classify environments and change types by risk, specifying automatic gates and required approver groups.
  - Require approvers to see artifact digest/provenance, test and analysis results, change scope, and rollback readiness.
  - Enforce environment protections in CI/controller and audit approvals, denials, expiry, and overrides.
  - Define emergency approval and break-glass rules separately with post-incident review.
- **probe**: **User decision:** Which environments should auto-promote, and which require human approval? Present options: (A) auto-promote dev/preview and staging, require approval for production; (B) auto-promote through canary, require approval before full production; (C) require approval for every environment; (D) another named boundary with approver groups and evidence requirements. After selection, inspect policy to confirm it is enforced rather than only documented.
- **failure_modes**: A supposedly manual production gate is absent from one workflow and an unreviewed release ships; blanket approval causes reviewers to rubber-stamp risky changes without evidence; an emergency bypass becomes the permanent promotion path.
- **severity**: important
- **applies_if**: all
- **sources**: https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment; https://sre.google/sre-book/release-engineering/
