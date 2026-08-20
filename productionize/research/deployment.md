# Deployment & release engineering — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 3). Raw item list, pre-synthesis.

### ci-stage-gates
- **what**: The pipeline executes deterministic build, unit, integration, security, packaging, and deploy-validation stages with explicit pass/fail gates.
- **why**: Missing or reorderable gates let regressions and unsafe artifacts reach production.
- **check**: probe
- **probe**: Parse CI workflow files and assert every production path depends on required test, security, and package jobs and that failed dependencies prevent deploy.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions; https://slsa.dev/spec/v1.0/requirements

### ci-feedback-budget
- **what**: Fast deterministic checks run before slower integration and end-to-end jobs, while independent jobs run in parallel and caches are keyed by lockfiles and toolchains.
- **why**: Serial work and unsafe cache reuse turn every change into a slow queue or hide dependency drift.
- **check**: probe
- **probe**: Parse workflow `needs`, matrix, and cache-key fields and assert independent jobs have no unnecessary dependencies and cache keys include relevant lockfile or toolchain hashes.
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows; https://docs.gitlab.com/ci/pipelines/

### ci-reproducible-inputs
- **what**: CI pins runner and toolchain versions and resolves dependencies from lockfiles in a clean checkout without mutable network inputs.
- **why**: Rebuilding one commit with different dependencies produces irreproducible tests and artifacts.
- **check**: probe
- **probe**: Parse manifests, lockfiles, Dockerfiles, and workflow setup steps and flag missing lockfiles where supported, floating action or base-image tags, and unpinned toolchain versions.
- **applies_if**: all
- **severity**: critical
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://reproducible-builds.org/docs/definition/

### ci-production-protection
- **what**: Production deployment requires protected branch or tag status checks, reviewed changes, and an authenticated environment gate.
- **why**: A green local build or unreviewed commit must not bypass release controls.
- **check**: probe
- **probe**: Query repository branch-protection and deployment-environment settings and assert required checks, review rules, and production approvals are enabled for every production path.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches; https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment

### artifact-build-once
- **what**: CI builds and tests one immutable artifact identified by a digest or content hash, then promotes that exact identifier across environments without rebuilding.
- **why**: Environment-specific rebuilds can differ from tested bits and make rollback ambiguous.
- **check**: probe
- **probe**: Inspect pipeline steps and deployment manifests and assert publishing occurs once and every environment consumes the same digest rather than `latest` or a fresh build.
- **applies_if**: all
- **severity**: critical
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://github.com/opencontainers/image-spec/blob/main/descriptor.md

### artifact-provenance
- **what**: Each release artifact carries signed provenance and an SBOM linking source revision, builder, inputs, and build steps.
- **why**: Without verifiable provenance, a registry artifact cannot be trusted or investigated after compromise.
- **check**: probe
- **probe**: Inspect CI and registry configuration and run `cosign verify-attestation` plus an SBOM lookup for a release digest, failing when either is absent or unverifiable.
- **applies_if**: all
- **severity**: critical
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://docs.sigstore.dev/cosign/verifying/verify-attestation/

### artifact-retention
- **what**: The registry retains immutable release digests and metadata for the rollback and forensic window while rejecting mutable production tags.
- **why**: Garbage collection or tag reuse can remove the only known-good rollback target.
- **check**: probe
- **probe**: Query registry retention policy and deployment references and assert production references are digests and retained longer than the documented rollback window.
- **applies_if**: all
- **severity**: important
- **sources**: https://github.com/opencontainers/image-spec/blob/main/descriptor.md; https://kubernetes.io/docs/concepts/containers/images/

### trunk-short-lived
- **what**: The default production-bound branch stays releasable through small short-lived changes merged behind flags when incomplete.
- **why**: Long-lived divergence increases merge conflicts, integration surprises, and lead time.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://trunkbaseddevelopment.com/

### branch-policy
- **what**: Any long-lived release branch has a documented purpose, owner, cut policy, backport rule, and end-of-life date.
- **why**: Undocumented branch lines silently accumulate fixes and produce unreproducible release differences.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://trunkbaseddevelopment.com/; https://docs.github.com/en/get-started/using-github/github-flow

### rolling-safety
- **what**: Rolling releases define readiness gates, maximum unavailable and surge values, revision history, and disruption budgets that preserve capacity during replacement.
- **why**: A nominally rolling update can take every healthy instance offline or route traffic to an unready process.
- **check**: probe
- **probe**: Parse rollout manifests and assert startup or readiness probes, `maxUnavailable`, `maxSurge`, revision history, and disruption-budget settings are present and bounded.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/; https://kubernetes.io/docs/tasks/run-application/configure-pdb/

### blue-green-switch
- **what**: Blue-green releases keep known-good and candidate versions independently runnable and switch traffic atomically only after validation.
- **why**: In-place replacement removes the immediate fallback and can expose partially upgraded state.
- **check**: judgment
- **applies_if**: web-api
- **severity**: important
- **sources**: https://martinfowler.com/bliki/BlueGreenDeployment.html

### canary-analysis
- **what**: Canary releases increase traffic in bounded steps and automatically promote or abort using version-scoped SLO, error, latency, saturation, and business metrics after a minimum observation window.
- **why**: A small traffic slice can reveal regressions that generic health checks miss, while premature promotion amplifies them.
- **check**: probe
- **probe**: Parse rollout and analysis configuration and assert traffic steps, hold duration, metric queries, thresholds, and abort action, then run a controlled failing canary in a non-production environment.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://sre.google/workbook/canarying-releases/

### release-observability
- **what**: Every rollout exposes old and new version labels in logs, metrics, traces, dashboards, and alerts before traffic is shifted.
- **why**: Without version attribution, automated analysis can average away a bad candidate and operators cannot localize impact.
- **check**: probe
- **probe**: Inspect telemetry schemas and alert queries and send a candidate request through a staging rollout, asserting the release identifier propagates to logs, metrics, and traces.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://sre.google/workbook/canarying-releases/; https://opentelemetry.io/docs/specs/semconv/

### automated-rollback
- **what**: The release controller automatically halts promotion and reverts to the last known-good artifact when candidate health or SLO gates breach.
- **why**: Waiting for a human to notice and act lets a bad release multiply its blast radius.
- **check**: probe
- **probe**: Parse rollout-controller policy and run a staging rollout with a controlled failing candidate, asserting promotion aborts and the prior artifact digest is restored.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://sre.google/workbook/canarying-releases/; https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#rolling-back-a-deployment

### rollback-blockers
- **what**: Rollback policy explicitly lists blockers such as irreversible schema changes, missing artifacts, insufficient capacity, or incompatible dependencies and defines safe stop or forward-fix behavior for each.
- **why**: Blind rollback can corrupt data or worsen an outage when the old version cannot safely run.
- **check**: judgment
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://martinfowler.com/articles/evodb.html; https://sre.google/sre-book/release-engineering/

### feature-flag-contract
- **what**: Every release flag has an owner, purpose, type, default, target scope, and expiry or removal issue recorded beside its definition.
- **why**: Unowned flags become permanent branching complexity and make future releases impossible to reason about.
- **check**: probe
- **probe**: Scan the flag registry or configuration and source annotations and assert required metadata, valid ownership, and an expiry or removal reference for every flag.
- **applies_if**: spa
- **severity**: important
- **sources**: https://martinfowler.com/articles/feature-toggles.html

### feature-flag-cleanup
- **what**: Expired temporary flags fail CI or generate an owned debt queue, and the removal change deletes both branches and configuration.
- **why**: Stale code paths multiply test matrices and can leave insecure or inconsistent behavior active.
- **check**: probe
- **probe**: Compare flag registry entries with source references and expiry dates and assert CI fails or creates a tracked owner action for every expired flag.
- **applies_if**: spa
- **severity**: important
- **sources**: https://martinfowler.com/articles/feature-toggles.html

### feature-flag-fail-safe
- **what**: Kill switches have a documented safe default, authorization boundary, audit log, and behavior when the flag service is unavailable.
- **why**: An outage in flag evaluation must not silently enable risky functionality or bypass access controls.
- **check**: judgment
- **applies_if**: spa
- **severity**: critical
- **sources**: https://martinfowler.com/articles/feature-toggles.html; https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

### environment-parity
- **what**: All environments deploy the same artifact and versioned runtime and infrastructure definitions, with differences limited to explicit configuration, scale, and isolated data.
- **why**: Staging-only success does not predict production when binaries, runtimes, or topology differ.
- **check**: probe
- **probe**: Resolve each environment's manifests and IaC overlays and assert artifact digest, runtime versions, and module versions match except for an approved variable allowlist.
- **applies_if**: all
- **severity**: critical
- **sources**: https://12factor.net/config; https://slsa.dev/spec/v1.0/requirements

### environment-data-isolation
- **what**: Non-production uses synthetic or scrubbed data and separately scoped external services, credentials, domains, and quotas.
- **why**: Preview or staging tests can leak production data or mutate production systems.
- **check**: probe
- **probe**: Scan environment manifests and secret references for production endpoints or credentials and assert non-production network policies and dataset labels deny production access.
- **applies_if**: all
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/security/secrets-good-practices/; https://kubernetes.io/docs/concepts/services-networking/network-policies/

### iac-review-plan
- **what**: Infrastructure and deployment changes are version-controlled, reviewed, policy-checked, and applied only from a saved plan generated from the reviewed commit.
- **why**: Direct console edits and plan-after-review can deploy infrastructure nobody approved.
- **check**: probe
- **probe**: Inspect IaC CI configuration and assert pull-request plan and policy checks, approval gates, and apply steps that consume the same commit or saved plan.
- **applies_if**: all
- **severity**: critical
- **sources**: https://developer.hashicorp.com/terraform/tutorials/automation/automate-terraform; https://developer.hashicorp.com/terraform/cloud-docs/workspaces/run

### iac-drift
- **what**: A scheduled read-only IaC plan detects out-of-band drift and routes non-zero drift to an owner with explicit reconciliation or an approved expiring exception.
- **why**: Undetected drift means the next apply can unexpectedly replace resources or leave security fixes unapplied.
- **check**: probe
- **probe**: Inspect the scheduler and plan command and assert drift exit codes, notifications, owner routing, and exception expiry are configured.
- **applies_if**: all
- **severity**: critical
- **sources**: https://developer.hashicorp.com/terraform/tutorials/cloud/drift-detection; https://developer.hashicorp.com/terraform/cli/commands/plan#detailed-exitcode

### iac-state-safety
- **what**: IaC state is remote, encrypted, access-controlled, locked for concurrent writes, backed up, and scrubbed of unnecessary secrets.
- **why**: Lost or concurrently modified state can destroy infrastructure or expose credentials.
- **check**: probe
- **probe**: Parse backend and policy configuration and assert remote encryption, locking, backup or versioning, and least-privilege roles are enabled.
- **applies_if**: all
- **severity**: critical
- **sources**: https://developer.hashicorp.com/terraform/language/state/locking; https://developer.hashicorp.com/terraform/language/state/sensitive-data

### gitops-reconciliation
- **what**: A GitOps controller reconciles an auditable desired-state commit to each environment and reports divergence instead of letting CI mutate clusters directly.
- **why**: Imperative deploys hide configuration drift and make live state impossible to reproduce.
- **check**: probe
- **probe**: Inspect controller application configuration and CI steps and assert pull-based sync, commit revision tracking, health reporting, and no unrestricted `kubectl apply` or equivalent mutation.
- **applies_if**: all
- **severity**: important
- **sources**: https://opengitops.dev/#principles; https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/

### gitops-break-glass
- **what**: Emergency out-of-band changes require time-bound authorization, audit capture, and a follow-up commit that restores Git as the source of truth.
- **why**: A break-glass edit that is not reconciled becomes permanent undocumented drift.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://opengitops.dev/#principles; https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/

### secret-injection
- **what**: Deployments fetch secrets at runtime or release time from a managed secret store using workload identity, never baking values into source, images, manifests, or logs.
- **why**: A leaked build artifact or repository history exposes every environment until credentials are rotated.
- **check**: probe
- **probe**: Scan repository history, image layers, rendered manifests, and deployment logs for secret literals and assert references use external providers and identity-based access.
- **applies_if**: all
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/security/secrets-good-practices/; https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### secret-rotation
- **what**: Secret rotation has an owner, cadence, revocation path, dual-key overlap where needed, and a deployment smoke test that fails closed on missing or invalid values.
- **why**: Static credentials outlive their intended trust window and rotation can cause an outage if consumers are not compatible.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html; https://kubernetes.io/docs/concepts/security/secrets-good-practices/

### readiness-drain
- **what**: Services expose distinct startup, readiness, and liveness checks and drain traffic before termination while honoring grace periods, connection timeouts, and idempotent retry behavior.
- **why**: Process replacement otherwise drops in-flight requests or sends traffic to a process that has not finished initializing.
- **check**: probe
- **probe**: Parse workload and service manifests and assert distinct probes, deregistration or `preStop` hooks, termination grace, and timeout or retry policies, then exercise a rolling restart with active requests.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/; https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/

### migration-expand-contract
- **what**: Schema changes follow expand, migrate, contract ordering so old and new application versions remain mutually compatible during rollout and rollback.
- **why**: A destructive migration deployed before traffic drains makes rollback impossible and can corrupt requests.
- **check**: probe
- **probe**: Parse migration files and release ordering and flag destructive DDL or required columns that precede compatibility windows and completed backfills.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://martinfowler.com/articles/evodb.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-backup-restore
- **what**: Before irreversible data changes, an automated backup and tested restore path is current, observable, and meets an agreed recovery point and recovery time objective.
- **why**: Code rollback cannot restore data that a destructive migration has removed.
- **check**: probe
- **probe**: Inspect migration pipeline and backup job records and assert a recent successful backup, a restore validation result, and timestamps within the documented RPO and RTO.
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://martinfowler.com/articles/evodb.html; https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/test-recovery-procedures.html

### capacity-surge-budget
- **what**: Rollout policy reserves capacity for surge, honors disruption budgets, and limits concurrent replacements relative to autoscaling and quota.
- **why**: A safe strategy can still trigger an outage when the platform cannot schedule candidate and incumbent replicas together.
- **check**: probe
- **probe**: Parse resource requests and limits, autoscaling, disruption budgets, quotas, and rollout parameters and assert worst-case surge fits available capacity.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/; https://kubernetes.io/docs/tasks/run-application/configure-pdb/

### preview-environments
- **what**: Each pull request can deploy its tested immutable artifact to an isolated preview with a stable URL, smoke checks, owner, and automatic expiry.
- **why**: Without representative previews, integration defects surface only after merge, while forgotten previews consume cost and attack surface.
- **check**: probe
- **probe**: Parse CI and environment configuration and assert pull-request creation, artifact digest pinning, URL reporting, smoke-job execution, owner metadata, and TTL teardown.
- **applies_if**: spa
- **severity**: important
- **sources**: https://docs.gitlab.com/ci/review_apps/

### preview-data-safety
- **what**: Preview environments cannot reach production data or privileged services and use synthetic or scrubbed fixtures with short-lived credentials.
- **why**: Automatic pull-request deployments are untrusted code paths that otherwise become a data-exfiltration route.
- **check**: probe
- **probe**: Inspect preview namespace, network, and secret policies and assert deny rules for production endpoints and absence of production secret references.
- **applies_if**: spa
- **severity**: critical
- **sources**: https://kubernetes.io/docs/concepts/services-networking/network-policies/; https://kubernetes.io/docs/concepts/security/secrets-good-practices/

### dora-four-keys
- **what**: Deployment frequency, lead time for changes, change fail rate, and failed deployment recovery time are measured from consistent deployment, commit, and incident events.
- **why**: Optimizing only speed can increase failure rate, while missing recovery data hides release risk.
- **check**: probe
- **probe**: Inspect event schemas and dashboard queries and recompute each metric for a fixed period from deployment, commit, and incident identifiers.
- **applies_if**: all
- **severity**: important
- **sources**: https://dora.dev/guides/dora-metrics-four-keys/; https://cloud.google.com/blog/products/devops-sre/using-the-four-keys-to-measure-your-devops-performance

### dora-event-integrity
- **what**: Metric events use a single timezone, commit-to-deployment association, explicit inclusion and exclusion rules, and versioned definitions across services.
- **why**: Hand-counted or inconsistent denominators make DORA trends incomparable and invite gaming.
- **check**: probe
- **probe**: Query event storage and metric code and assert required IDs and timestamps, documented filters, and reproducible results for a known sample.
- **applies_if**: all
- **severity**: important
- **sources**: https://dora.dev/guides/dora-metrics-four-keys/; https://cloud.google.com/blog/products/devops-sre/using-the-four-keys-to-measure-your-devops-performance

### deployment-audit-trail
- **what**: A release record links approver, source revision, artifact digest and provenance, IaC plan, environment, rollout outcome, and rollback reason.
- **why**: During an incident responders cannot identify what changed or prove which bits are running without a complete chain of custody.
- **check**: probe
- **probe**: Inspect deployment event schema and query a recent release and assert all listed fields are emitted and searchable by release identifier.
- **applies_if**: all
- **severity**: critical
- **sources**: https://slsa.dev/spec/v1.0/requirements; https://opengitops.dev/#principles

### release-approval-boundary
- **what**: The team explicitly chooses which environments auto-promote and which require human approval, with approvals tied to evidence rather than blanket ceremony.
- **why**: Hidden manual gates create queueing and bypasses, while unreviewed production promotion expands blast radius.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment; https://sre.google/sre-book/release-engineering/
