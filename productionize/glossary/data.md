# Data integrity & database glossary

### migration-expand-contract
- **definition**: An expand/contract migration changes schema in backward-compatible phases: add the new shape, deploy readers and writers that tolerate both shapes, backfill and verify, switch reads, then remove the old shape. The contract phase is delayed until every client and binary version that can access the database has migrated.
- **implementation**:
  - Add nullable columns, shadow tables, or dual-write fields before changing readers; keep old columns readable during the overlap window.
  - Gate dual writes and read cutover with a versioned feature flag or deployment step, with metrics for old/new read and write paths.
  - Backfill in resumable bounded batches, recording a checkpoint and transformation errors.
  - Reconcile primary-key sets, counts, checksums, null rates, and business invariants before enabling the new read path.
  - Drop old columns, indexes, or tables only in a later migration after old binaries and consumers are demonstrably drained.
- **probe**: An assessor must inspect the migration sequence, compatibility matrix, deployment gates, dual-read/write metrics, backfill checkpointing, reconciliation evidence, and the explicit approval or gate for destructive contract cleanup. Confirm that a rollback to the previous application version remains compatible at every intermediate schema state.
- **failure_modes**: A rolling deployment starts new code before every instance can tolerate a missing or renamed column and returns widespread database errors. A large rewrite takes an exclusive lock while traffic is live, causing request timeouts and a cascading outage. A partially backfilled field is promoted without reconciliation, leaving silently incorrect records.
- **severity**: critical
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://martinfowler.com/bliki/ParallelChange.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-bounded-backfill
- **definition**: A bounded backfill processes a stable key range or cursor in small, idempotent batches rather than one unbounded transaction. It persists progress, throttles resource use, and has an explicit pause, retry, and abort path so operators can resume safely.
- **implementation**:
  - Select rows using a monotonic primary-key cursor or durable checkpoint, never an offset that shifts during writes.
  - Configure batch size, commit frequency, maximum runtime, rate limits, and lock/WAL thresholds as deployment parameters.
  - Make each batch restart-safe with deterministic transformations and an upsert or “already processed” guard.
  - Persist checkpoint, rows processed, failures, and last error; expose progress and remaining-estimate metrics.
  - Stop or reduce concurrency when replica lag, lock waits, WAL volume, or database saturation exceeds thresholds.
- **probe**: Parse migration and backfill jobs for a stable key-range or cursor, bounded batch size, persisted checkpoint, retry handling, and rate or lock controls, rejecting an unbounded full-table transaction. In staging, interrupt the job between batches and assert that restart resumes from the checkpoint without duplicate or missing changes.
- **failure_modes**: A single transaction fills WAL and disk, forcing the database into an emergency read-only state. A backfill holds locks long enough to exhaust the request pool. A retry starts at the beginning and applies a non-idempotent transformation twice.
- **severity**: critical
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/; https://www.postgresql.org/docs/current/continuous-archiving.html

### migration-rollback-forward-fix
- **definition**: Every migration has a recovery strategy: a tested reversible down path where safe, or a documented forward fix when rollback would lose or ambiguously restore data. The strategy states data-loss boundaries and which application versions remain compatible after partial execution.
- **implementation**:
  - Store migration SQL and rollback/forward-fix procedures together under version control, including irreversible-operation warnings.
  - Test both clean execution and interruption at representative statements on a production-sized clone.
  - Preserve old data during destructive transitions until backups, reconciliation, and the rollback window have expired.
  - Define the exact command, owner, stop conditions, and application version matrix for a forward fix.
  - Record migration version and recovery outcome in deployment history and incident evidence.
- **probe**: An assessor must inspect the migration's down script or forward-fix runbook, interruption tests, compatibility matrix, data-loss declaration, and evidence that operators can execute it under the deployment system's permissions. Reject a generic “restore backup” plan that does not account for writes made after the migration.
- **failure_modes**: A failed constraint migration leaves half the rows transformed and no safe way to restart or undo it. Rolling the binary back makes it query a schema whose old column was already dropped. An emergency operator runs an untested down migration and deletes valid production data.
- **severity**: critical
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://documentation.red-gate.com/flyway/reference/commands/undo; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-lock-safety
- **definition**: Lock-safe migrations minimize blocking DDL and bound the time they can wait or hold locks on production-sized tables. They use online or concurrent forms where available, short transactions, and explicit lock and statement timeouts with an approved exception process for unavoidable rewrites.
- **implementation**:
  - Run `CREATE INDEX CONCURRENTLY`, online schema-change tooling, or equivalent engine-specific forms where supported.
  - Set migration-session `lock_timeout`, `statement_timeout`, and idle-in-transaction limits before touching hot tables.
  - Separate preparation, validation, and metadata swaps into short transactions; avoid wrapping concurrent operations in a transaction block.
  - Preflight estimated table size, lock mode, replica impact, and expected duration against production-like data.
  - Monitor blocked sessions and abort on thresholds rather than waiting indefinitely.
- **probe**: Parse migration SQL and database settings for online or concurrent operations, lock and statement timeouts, and bounded transaction scopes, rejecting table rewrites or unbounded DDL without an approved exception. Rehearse the migration under concurrent load and confirm a timeout releases the waiting session.
- **failure_modes**: An index build waits behind an idle transaction and blocks all writes. A table rewrite exhausts the maintenance window while connection pools queue requests. A migration deadlocks with an application transaction and causes repeated deployment failures.
- **severity**: critical
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/; https://www.postgresql.org/docs/current/explicit-locking.html

### migration-schema-drift
- **definition**: Migration history is the authoritative description of schema, and drift detection compares that authority with both a clean database and every deployed database. The check detects manual changes, missing migrations, altered migration history, and provider-side differences before they affect deploys, restores, or replicas.
- **implementation**:
  - Apply all migrations to a disposable database and produce a normalized DDL or schema snapshot.
  - Record an immutable applied-migration version/checksum in each environment and fail on changed historical files.
  - Compare production metadata against the expected snapshot while normalizing ownership, storage parameters, and generated names.
  - Require emergency manual changes to be captured immediately as reviewed migrations with an owner and expiry.
  - Run drift checks in CI and on a scheduled production read-only connection.
- **probe**: Create a disposable database, apply every migration, dump normalized DDL, and diff it against the checked-in schema snapshot and recorded production migration version. Fail if production contains unmanaged objects or if a migration checksum differs from the deployed history.
- **failure_modes**: A manually added production index is absent after restore, causing a query latency outage. A replica or fresh environment applies a different schema and fails only when a rare code path runs. Edited migration history makes a deploy skip a required change.
- **severity**: important
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://documentation.red-gate.com/flyway/reference/concepts/migrations; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-deploy-order
- **definition**: Deployment order guarantees that additive schema changes are present before application code depends on them, while contract cleanup waits until old binaries and consumers are gone. The overlap window explicitly covers rolling deploys, delayed workers, replicas, and rollback to the prior version.
- **implementation**:
  - Represent expand migration, application rollout, backfill/cutover, and contract migration as separate CI/CD DAG nodes.
  - Add a health gate proving all instances and workers report the new compatible version before cleanup.
  - Keep old and new binary compatibility in a release matrix, including background jobs and scheduled tasks.
  - Require an explicit, separately approved later release for dropping or renaming old schema objects.
  - Block rollback when the contract phase would make the previous binary incompatible, or provide a tested forward recovery path.
- **probe**: Parse the CI/CD DAG and release manifests to verify expand migrations precede application rollout and contract migrations require an explicit later gate after old versions are drained. Exercise a rolling deployment with one old worker and assert it remains functional during the overlap window.
- **failure_modes**: A canary references an additive column before the migration runs and crashes at startup. A delayed queue worker writes the old representation after cutover, corrupting data. A rollback deploy fails because cleanup already removed the previous version's schema.
- **severity**: critical
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://martinfowler.com/bliki/ParallelChange.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-rehearsal
- **definition**: A migration rehearsal runs the change against a production-sized clone with realistic indexes, data distribution, concurrency, and traffic patterns. It measures lock waits, rewrite duration, WAL growth, replica lag, errors, and rollback or pause behavior before production approval.
- **implementation**:
  - Refresh a sanitized or synthetic clone with production-scale row counts and representative skew.
  - Replay representative reads/writes and concurrent workers while applying the exact migration artifact.
  - Capture lock graphs, query latency, WAL/disk growth, replica lag, and total runtime with thresholds tied to the change window.
  - Include interruption, retry, rollback/forward-fix, and post-migration reconciliation scenarios.
  - Store signed rehearsal results with the migration version and approve only when observed limits are acceptable.
- **probe**: An assessor must inspect the clone's scale and data-shape rationale, exact artifact identity, concurrency workload, resource measurements, abort/recovery exercise, and dated sign-off. A small developer database or a timing-only run is insufficient evidence.
- **failure_modes**: A migration that is fast on a toy database rewrites a multi-terabyte table beyond the outage window. Production concurrency exposes a lock cycle absent from the local test. WAL growth saturates a replica during the change and invalidates failover.
- **severity**: important
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/; https://www.postgresql.org/docs/current/monitoring-stats.html

### migration-reconciliation
- **definition**: Reconciliation compares old and new representations before a migration cutover using set membership, counts, checksums, null/error rates, and business invariants. It turns a backfill or dual-write transition into a gated decision rather than trusting job completion alone.
- **implementation**:
  - Compare primary-key sets and row counts in both directions, then compare normalized hashes for deterministic fields.
  - Report null, conversion-error, duplicate, and lagging-row counts by tenant or partition, not only globally.
  - Run sampled domain invariants and aggregate totals such as balances, quantities, or order counts.
  - Define explicit tolerances and a zero-tolerance policy for keys, duplicates, and financial totals.
  - Block read cutover until reconciliation is green and preserve the report with the release artifact.
- **probe**: Run the repository reconciliation command against old and new representations and fail when primary-key sets, counts, hashes, or invariant-query results differ beyond an explicit threshold. Include a deliberately corrupted fixture to verify the command returns nonzero and identifies the affected partition or key range.
- **failure_modes**: A parser silently converts invalid values to null during backfill and reports success. A dual-write race omits a subset of records that aggregate counts do not reveal. A retry duplicates rows in the new table and produces incorrect billing totals.
- **severity**: critical
- **applies_if**: all
- **merges_into**: db-migrations
- **sources**: https://martinfowler.com/bliki/ParallelChange.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### backup-rpo-retention
- **definition**: Backup and change-log retention must be configured to meet a written recovery-point objective, recovery-time objective, and restore-window requirement. The policy specifies how often usable recovery points are created, how long they remain available, and where the recovery process is expected to run.
- **implementation**:
  - Ask for numeric maximum data loss (RPO), maximum service restoration time (RTO), and required historical restore window.
  - Configure snapshot frequency, WAL/change-log retention, cross-region copies, lifecycle expiry, and storage capacity from those targets.
  - Alert on missed backup jobs, stale newest recovery point, failed uploads, and retention below policy.
  - Document backup scope, excluded objects, encryption keys, provider limits, and the owner who accepts residual risk.
  - Recalculate the policy when data volume, traffic, regulatory retention, or architecture changes.
- **probe**: Present the exact decision: “What are the maximum acceptable committed data loss, service restoration time, and historical restore window?” Require options in numeric units (RPO: 0, ≤5 minutes, ≤1 hour, >1 hour; RTO: ≤15 minutes, ≤1 hour, ≤4 hours, >4 hours; restore window: 7, 30, 90, or 365+ days), plus an owner and written exception for any other value.
- **failure_modes**: An incident reveals the newest valid snapshot is 24 hours old despite a five-minute business RPO. Backups expire before an audit or delayed corruption is discovered. The team meets a nominal backup schedule but cannot restore within the promised outage window.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://www.postgresql.org/docs/current/backup.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html

### backup-restore-drill
- **definition**: A restore drill periodically restores a real backup into an isolated environment and exercises database recovery plus application startup and representative reads and writes. It measures actual recovery time and validates credentials, extensions, objects, versions, and data usability rather than trusting a successful backup job.
- **implementation**:
  - Schedule drills at a cadence tied to RTO/RPO and after material database or provider changes.
  - Restore into a network-isolated account/project with synthetic or approved data access and no production write path.
  - Start the exact application artifact, run smoke queries and representative workflows, and verify dependencies and secrets.
  - Record restore duration, time to serve checks, recovery point, missing objects, errors, and cleanup evidence.
  - Alert or block release when measured RTO/RPO or validation checks fail.
- **probe**: An assessor must inspect dated drill records showing the selected backup, isolated target, exact application/version, startup and representative workflow results, measured restore time and recovery point, defects, and remediation. A provider “backup succeeded” metric alone is not evidence.
- **failure_modes**: Restores fail because the database extension or KMS permission was never included in the runbook. The application starts against a restored database but cannot serve because required buckets or credentials are absent. Recovery exceeds the contractual RTO because snapshot transfer time was never measured.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://www.postgresql.org/docs/current/backup.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html

### backup-integrity-check
- **definition**: Backup integrity checks validate manifests, checksums, metadata, and required WAL or change-log continuity before an artifact is marked usable. Verification is performed automatically and records enough evidence to identify an incomplete, corrupted, or tampered recovery chain.
- **implementation**:
  - Run `pg_verifybackup` for PostgreSQL base backups and retain its output with the backup manifest.
  - Validate that every required WAL segment or equivalent log interval exists and is readable through the target restore point.
  - Verify object checksums, encryption metadata, size/age expectations, and provider backup-verification status.
  - Quarantine failed artifacts and prevent retention cleanup from removing the last known-good copy.
  - Alert on missing manifests, checksum failures, continuity gaps, and unverifiable provider status.
- **probe**: For PostgreSQL base backups, run `pg_verifybackup` and validate the required WAL segment range; for the selected provider, parse backup-verification status and fail on missing checksum or manifest evidence. Include a negative fixture with a missing WAL segment and assert it is rejected.
- **failure_modes**: A corrupted object is discovered only after the primary database is lost. A base backup is valid but the WAL segment needed for the target timestamp is missing. A tampered artifact passes a size-only check and is restored as authoritative data.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://www.postgresql.org/docs/current/app-pgverifybackup.html; https://www.postgresql.org/docs/current/continuous-archiving.html

### pitr-capability
- **definition**: Point-in-time recovery continuously archives WAL or an equivalent change log and can restore to a chosen timestamp within the retention horizon. It is proven against an actual target time, not inferred from the existence of periodic snapshots.
- **implementation**:
  - Enable continuous archiving with monitored delivery, encryption, retention, and cross-region durability.
  - Select target timestamps before and after representative errors, and preserve the required base backup plus log chain.
  - Record commit timestamps or application markers so the recovered boundary can be verified against known writes.
  - Monitor archive lag, failed segments, storage pressure, and gaps; page before the declared RPO is breached.
  - Keep the PITR target isolated until consistency checks and operator approval complete.
- **probe**: An assessor must inspect archive configuration, continuity/lag alerts, retention horizon, and a recent drill that restored to an arbitrary timestamp and verified which known writes were present or absent. Evidence must demonstrate the declared RPO, not just snapshot availability.
- **failure_modes**: An operator deletes records at 14:05 but only a midnight snapshot exists, forcing unacceptable data loss. WAL archiving silently fails for hours and leaves no usable target near the incident. A restore stops at the wrong boundary because clocks and transaction markers were not validated.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://www.postgresql.org/docs/current/continuous-archiving.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html

### backup-access-isolation
- **definition**: Backups are encrypted in transit and at rest, stored outside the primary trust boundary, and restorable only through least-privilege break-glass access. Isolation must preserve recovery availability while preventing a compromised primary account or ordinary operator from deleting or exposing every copy.
- **implementation**:
  - Use a separate account/project and preferably an immutable or retention-locked destination for recovery artifacts.
  - Require TLS for transfers and customer-managed or provider-managed encryption keys with rotation and access logging.
  - Separate backup-write, backup-read, and restore permissions; deny primary runtime identities delete and restore privileges.
  - Store break-glass credentials in a controlled vault with MFA, approval, time limits, and post-use rotation.
  - Test restore access from the incident role and alert on policy, key, or retention changes.
- **probe**: Parse infrastructure and provider backup settings for encryption keys, TLS transfer, an isolated or immutable destination, retention lock, and a separate restore role, failing when any required control is absent. Verify the production application role cannot list, delete, or restore backup objects.
- **failure_modes**: Ransomware compromises the primary account and deletes snapshots and WAL. A leaked database credential grants unrestricted access to unencrypted exports. A break-glass restore fails because its key policy was never tested outside the primary account.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html; https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final

### backup-erasure-policy
- **definition**: A backup-erasure policy reconciles retention, legal holds, subject erasure, snapshots, WAL, replicas, exports, and expiry into an owned decision. It states when personal data remains recoverable in historical media, what can be deleted or cryptographically destroyed, and how holds override ordinary expiry.
- **implementation**:
  - Map each backup and log class to retention duration, legal-hold behavior, deletion mechanism, and accountable owner.
  - Document whether subject erasure is propagated immediately, deferred until backup expiry, or handled through restoration-time suppression.
  - Apply immutable retention and legal-hold controls so automated purge cannot destroy evidence.
  - Record backup-key destruction, expiry, or exception evidence without copying the subject's personal data into tickets.
  - Review the policy with privacy, legal, security, and incident-response owners before changing schedules.
- **probe**: Present the exact decision: “When a subject-erasure request conflicts with a backup retention or legal hold, which policy applies?” Options: (A) delete/sanitize eligible backup copies immediately, (B) retain backups until expiry but suppress on restore and document completion, (C) legal hold overrides erasure until a named release date, or (D) another reviewed policy with owner and date. Require evidence of enforcement for every backup/log class.
- **failure_modes**: A privacy request is marked complete while an export and long-lived snapshot still identify the subject. An automated purge deletes evidence protected by a legal hold. An emergency restore reintroduces erased records because no suppression or exception workflow exists.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html

### restore-consistency-checks
- **definition**: A restored or promoted database is not traffic-ready until logical consistency checks pass, in addition to storage-level recovery. Checks cover referential integrity, uniqueness, sequences, checksums or counts, and critical business aggregates.
- **implementation**:
  - Version a post-restore SQL suite with foreign-key/orphan checks, duplicate primary-key checks, and constraint validation.
  - Compare row counts/checksums for critical tables and verify sequence high-water marks exceed referenced identifiers.
  - Check domain totals such as balances, inventory, subscription state, and event offsets against known markers.
  - Run checks with read-only credentials before changing the application endpoint or promoting the writer.
  - Fail closed, quarantine the recovered instance, and attach results to the incident or drill record.
- **probe**: Run a versioned post-restore SQL suite that checks constraint violations, orphan counts, primary-key uniqueness, sequence high-water marks, and selected aggregate totals, returning nonzero on any failure. Confirm endpoint promotion is blocked when the suite fails.
- **failure_modes**: A snapshot restores successfully but contains orphaned child rows from an interrupted workflow. Sequences lag behind restored identifiers and the next insert collides. A promotion serves a stale financial aggregate that causes duplicate settlement.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://www.postgresql.org/docs/current/amcheck.html; https://www.postgresql.org/docs/current/ddl-constraints.html

### replication-topology
- **definition**: Replication topology names the authoritative writer, synchronous and asynchronous standbys, read replicas, regions, quorum, and each link's recovery-point, recovery-time, and consistency contract. It makes failover authority and stale-read behavior explicit rather than leaving incident responders to infer them from infrastructure.
- **implementation**:
  - Maintain a versioned diagram or machine-readable inventory of writer, replicas, replication mode, region, endpoint, and promotion eligibility.
  - State per-link maximum lag, read-after-write behavior, quorum requirements, and whether writes can be acknowledged without a standby.
  - Tag replicas by allowed workload and prevent general routing to a replica that is not fresh enough for that workload.
  - Monitor topology changes, unexpected writers, replication slots, and region connectivity.
  - Review topology after every provider, schema, or routing change and during failover drills.
- **probe**: Present the exact decision: “Which database is authoritative for writes, which replicas may be promoted, and what consistency/RPO contract applies to each read path?” Options: single writer with async replicas; synchronous quorum across regions; provider-managed multi-zone writer/standby; or another documented topology. Require named endpoints, promotion authority, lag thresholds, and owner.
- **failure_modes**: Responders promote a read replica that was never eligible and lose acknowledged writes. A stale regional replica becomes the source for a user-visible read after a routing change. Two operators act on contradictory topology diagrams and create competing writers.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/warm-standby.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html

### replication-lag-freshness
- **definition**: Replication freshness measures write/replay LSN or equivalent byte and time lag and uses explicit thresholds to gate reads, jobs, and failover promotion. A replica that exceeds its contract is unavailable for that use, even if its connection is healthy.
- **implementation**:
  - Export replay LSN, write LSN, byte lag, time lag, apply errors, and last-replay timestamp per replica.
  - Configure separate freshness thresholds for read-after-write traffic, analytics, background jobs, and promotion eligibility.
  - Make routers reject or reroute stale replicas and expose the reason rather than silently serving old data.
  - Alert before RPO breach and remove a replica from promotion candidates until it catches up.
  - Test lag injection and verify routing, queue consumers, and failover automation honor the thresholds.
- **probe**: Query database or provider replication metrics, alert when byte or time lag exceeds the configured threshold, and verify in an integration probe that the router refuses stale replicas. Inject or simulate lag and assert promotion is blocked at the declared RPO boundary.
- **failure_modes**: A user reads an old account state immediately after a successful write because the router ignores replay lag. A lagging replica is promoted and loses more acknowledged transactions than the RPO allows. A long-running slot retains WAL until the primary disk fills.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/monitoring-stats.html; https://www.postgresql.org/docs/current/warm-standby.html

### failover-runbook
- **definition**: A failover runbook is a versioned, executable procedure for detecting failure, fencing the old writer, promoting the target, rotating endpoints and secrets, draining pools, validating data, and returning to service. It names decision authority, stop conditions, and rollback or reconciliation steps.
- **implementation**:
  - Include preconditions, incident roles, provider/API commands, DNS or service-discovery changes, and expected timings.
  - Fence or revoke the old writer before promotion and record evidence that it cannot accept writes.
  - Rotate connection endpoints, credentials, leases, and pool state; drain stale clients and workers.
  - Run post-promotion consistency, freshness, smoke, and write/read checks before reopening traffic.
  - Keep a rollback-to-old-site or forward-reconciliation procedure and review the runbook after every drill or incident.
- **probe**: An assessor must inspect a versioned runbook containing detection, authority, fencing, promotion, endpoint/secret rotation, pool draining, verification, return-to-service, stop conditions, and owner assignments. Execute it in a drill and compare each recorded command and timing with the documented procedure.
- **failure_modes**: DNS changes lag while clients continue writing to the failed site. Operators promote a standby without revoking the old writer and create divergent histories. Secret rotation leaves half the worker fleet connected to an obsolete endpoint.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://csrc.nist.gov/pubs/sp/800/34/r1/final; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html

### failover-drill
- **definition**: A failover drill deliberately exercises planned and unplanned promotion on a fixed cadence, measuring recovery time, recovery-point loss, lag, client retry behavior, and reconciliation. It validates both infrastructure automation and application assumptions under the same conditions as an incident.
- **implementation**:
  - Schedule a non-production or controlled production exercise with a documented blast radius and abort authority.
  - Test planned switchover, abrupt writer loss, stale-DNS/client pool behavior, worker retries, and in-flight transactions.
  - Capture detection-to-service time, last acknowledged commit, lost/replayed writes, lag, error rate, and reconciliation outcome.
  - Verify fencing, endpoint rotation, secret refresh, health/readiness behavior, and consistency checks.
  - Track defects to owners and repeat until measured RTO/RPO and recovery invariants pass.
- **probe**: An assessor must inspect dated planned and unplanned drill records with injected failure, measured RTO/RPO, replica lag, client and worker retry results, fencing evidence, and reconciliation output. Require remediation evidence for every failed threshold.
- **failure_modes**: A standby is healthy but clients cache the old DNS address for longer than the outage budget. An application retries a timed-out write and duplicates a business operation after promotion. The drill uncovers that a replica was hours behind only when it is needed.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://csrc.nist.gov/pubs/sp/800/34/r1/final; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html

### split-brain-fencing
- **definition**: Split-brain fencing guarantees that an old writer cannot accept writes before a new writer is promoted. It relies on an authoritative lease, quorum, provider fencing, network isolation, or equivalent mechanism whose failure mode is fail-closed.
- **implementation**:
  - Use a single authoritative leader lease or provider promotion API with an epoch/term that writers validate.
  - Revoke credentials, isolate the old node, or power it off before enabling writes on the target.
  - Make clients reject stale writer epochs and prevent DNS/service discovery alone from being the only fence.
  - Set lease expiry and clock/partition behavior so loss of authority stops writes rather than permitting both sides.
  - Exercise network partitions and delayed old writers in a drill, then reconcile evidence before reopening traffic.
- **probe**: An assessor must inspect the lease/quorum/provider configuration, writer epoch validation, credential/network fencing, fail-closed behavior, and partition drill results. Demonstrate that an isolated or delayed old writer receives a write rejection after promotion.
- **failure_modes**: A network partition lets both database nodes accept writes and produces irreconcilable conflicting records. Stale DNS sends a worker to the former primary after promotion. A lease expires but the client continues writing because it never validates the authority epoch.
- **severity**: critical
- **applies_if**: all
- **merges_into**: backup-dr
- **sources**: https://www.postgresql.org/docs/current/warm-standby.html; https://patroni.readthedocs.io/en/latest/

### transaction-boundary
- **definition**: A transaction boundary groups all database mutations required for one business invariant and its outbox record into one atomic commit. External calls occur outside the commit-critical section so a process or dependency failure cannot leave only half of the intended state.
- **implementation**:
  - Begin a database transaction before reading and changing rows that participate in the invariant.
  - Insert the transactional-outbox event in the same transaction and commit both together.
  - Keep external HTTP, queue, and filesystem calls out of the transaction; relay them from committed outbox records.
  - Set bounded transaction and lock timeouts and retry only known transient serialization/deadlock failures.
  - Instrument transaction IDs and commit/rollback outcomes for audit and incident analysis.
- **probe**: Use a static or transaction-trace probe to assert that the business write and outbox insert share one `BEGIN` and `COMMIT` or framework transaction and that no external call occurs inside the commit-critical section. Kill the process before commit and verify neither record is visible; kill it after commit and verify the relay can deliver.
- **failure_modes**: Payment state commits but the event publish fails, leaving downstream billing unaware. A process crashes after creating an order but before reserving inventory. A timeout causes an application retry that observes partial state and makes a second mutation.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/tutorial-transactions.html; https://microservices.io/patterns/data/transactional-outbox.html

### transaction-isolation
- **definition**: Transaction isolation defines how concurrent workflows see and lock data, and each cross-row invariant has a documented isolation level and bounded retry policy. The choice prevents write skew, lost updates, phantoms, or deadlocks without imposing stronger serialization than the workload needs.
- **implementation**:
  - Map each invariant to read/write sets, isolation level, row/advisory locks, or a uniqueness constraint.
  - Use `SELECT ... FOR UPDATE`, serializable transactions, or atomic updates where concurrent decisions must exclude one another.
  - Catch serialization and deadlock errors, retry with a bounded jittered policy, and return a deterministic conflict after exhaustion.
  - Set transaction/lock timeouts and keep lock acquisition order consistent across code paths.
  - Load-test concurrent workflows and record abort, retry, and latency rates.
- **probe**: An assessor must inspect concurrency design for each cross-row invariant, including isolation setting, lock order, timeout, retry limit, and documented conflict response. Require a concurrent test or production trace showing behavior under interleavings that could otherwise produce write skew or lost updates.
- **failure_modes**: Two seat-booking transactions both observe one available seat and commit, overselling inventory. Concurrent transfers each read a valid balance and together violate the account invariant. Inconsistent lock order causes a deadlock storm under peak traffic.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/transaction-iso.html; https://www.postgresql.org/docs/current/explicit-locking.html

### idempotent-replay
- **definition**: Retried commands, jobs, migrations, and event consumers associate a stable idempotency key or deduplication constraint with the business effect. Replaying the same key returns the documented deterministic result and does not create a second charge, resource, mutation, or event effect.
- **implementation**:
  - Require a client/job/event key scoped to the operation and tenant, and store it with the canonical result or status.
  - Enforce uniqueness in the database and atomically claim the key with the business mutation.
  - Return the original response for completed duplicates; return an explicit in-progress or conflict response for concurrent duplicates.
  - Define key retention/expiry longer than the maximum retry and replay window, with payload mismatch rejection.
  - Test API, worker, migration, and consumer retries after timeouts, process crashes, and redelivery.
- **probe**: Inspect schemas for a unique idempotency or deduplication key and run the operation twice with the same key, asserting one committed effect and a deterministic response. Repeat concurrently and with the same key but a different payload; the latter must be rejected rather than silently reused.
- **failure_modes**: A client retries after a lost response and is charged twice. A queue redelivers an event and provisions two identical resources. A migration resumes after a crash and applies a non-idempotent transformation twice.
- **severity**: critical
- **applies_if**: all
- **merges_into**: idempotency-keys
- **sources**: https://docs.stripe.com/api/idempotent_requests; https://microservices.io/patterns/data/transactional-outbox.html

### transactional-outbox
- **definition**: A transactional outbox persists each event to publish in the same database transaction as the business state change. A separate relay then publishes committed rows with retries, leases, and observable delivery state, allowing at-least-once transport without losing the business event.
- **implementation**:
  - Create an outbox table with event ID, aggregate key/version, type, payload, created time, attempt count, lease, and delivery/error state.
  - Insert the outbox row before the single transaction commits; never publish directly from the request's commit path.
  - Run a polling relay or CDC connector with leases, exponential backoff, dead-letter/quarantine handling, and idempotent broker publication/consumption.
  - Expose pending age, attempts, publish latency, failures, and poison-message metrics and alerts.
  - Retain or compact rows only after the delivery and replay policy permits it.
- **probe**: Locate the outbox table, transaction write, relay polling or CDC, retry and backoff logic, and sent or lease state; kill the relay between commit and publish and verify eventual delivery without a duplicate business effect. Kill it after broker publish and verify consumers tolerate the resulting duplicate.
- **failure_modes**: The database commits an order but a broker publish fails, permanently losing the event. A relay crash after publish causes redelivery that creates a duplicate downstream order. A stuck poison event blocks all later events because no quarantine policy exists.
- **severity**: critical
- **applies_if**: all
- **sources**: https://microservices.io/patterns/data/transactional-outbox.html; https://www.postgresql.org/docs/current/tutorial-transactions.html

### outbox-delivery-order
- **definition**: Outbox delivery rules specify ordering per aggregate, duplicate handling, poison-message quarantine, and retention or compaction. They preserve causality where consumers need it without allowing one malformed or permanently unavailable event to block unrelated work.
- **implementation**:
  - Assign a monotonic aggregate version or sequence and partition relay/broker work by aggregate key.
  - Have consumers reject, buffer, or reconcile gaps and deduplicate by event ID or aggregate version.
  - Use visibility leases and bounded retries, then move poison messages to an operator-visible quarantine stream.
  - Define whether ordering is global, per aggregate, per tenant, or intentionally best-effort.
  - Retain payloads and delivery evidence for the replay/audit window, then compact only with a documented recovery path.
- **probe**: An assessor must inspect event schema, aggregate sequence, partition key, consumer duplicate/gap behavior, retry/quarantine thresholds, and retention policy. Require evidence from an out-of-order, duplicate, and poison-message exercise showing unrelated aggregates continue to flow.
- **failure_modes**: A stale profile-update event arrives after a newer one and rolls customer state backward. One malformed message blocks a partition and delays every tenant sharing it. Compaction removes the only event needed to replay a consumer after repair.
- **severity**: important
- **applies_if**: all
- **sources**: https://microservices.io/patterns/data/transactional-outbox.html

### db-integrity-constraints
- **definition**: The database enforces structural and business invariants with non-null, primary-key, unique, foreign-key, check, exclusion, domain, and equivalent constraints. Application validation remains useful for user feedback, but it is not the final protection against alternate writers or races.
- **implementation**:
  - Define every non-negotiable invariant in migrations using the strongest engine-supported constraint.
  - Add unique or exclusion constraints for identity, intervals, and allocation conflicts rather than pre-checking in application code.
  - Use foreign keys with explicit delete/update actions and index referencing columns for predictable enforcement.
  - Add constraints online or as `NOT VALID` then validate before making them mandatory on hot tables.
  - Monitor constraint violations and route them to actionable operational errors, not silent drops.
- **probe**: Introspect `pg_constraint` and `information_schema` or the engine equivalent and compare required invariants with migration definitions, failing when a rule exists only in application validation. Attempt invalid null, duplicate, orphan, and check-violating fixtures and require database rejection.
- **failure_modes**: A direct admin script inserts a duplicate account because only the API checked uniqueness. A race creates an orphaned child after its parent is removed. A malformed status value breaks reporting because the database accepted an undocumented enum.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html

### foreign-key-delete-semantics
- **definition**: Foreign-key delete and update actions explicitly define whether dependent records cascade, restrict, null out, retain history, or are anonymized. The choice is tested against tenant boundaries, audit requirements, and privacy/retention obligations rather than inherited accidentally from ORM defaults.
- **implementation**:
  - Document the lifecycle for each parent/child relationship and choose `CASCADE`, `RESTRICT`, `NO ACTION`, or `SET NULL` deliberately.
  - Preserve historical records with immutable references or anonymization where financial/audit history must remain.
  - Prevent cross-tenant cascades with tenant-key constraints and scoped deletion procedures.
  - Run dry-run impact counts before destructive requests and require authorization for bulk cascades.
  - Test parent deletion, partial failures, retries, and restore/erasure interactions in a disposable database.
- **probe**: An assessor must inspect every foreign key's action, history/privacy rationale, tenant-scope protection, migration, and deletion test evidence. Require a dry-run showing affected row counts and verify that the selected action matches the documented business and legal policy.
- **failure_modes**: Deleting one user cascades through an entire tenant's shared records. Restrict semantics leave orphan-like soft-deleted rows that downstream code treats as active. A required audit history disappears because an ORM cascade was enabled without review.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html; https://eur-lex.europa.eu/eli/reg/2016/679/oj

### concurrency-invariant
- **definition**: Uniqueness and allocation races are resolved atomically by database constraints or locking/serializable transactions, not by a check-then-insert sequence. The losing concurrent operation receives a documented conflict or retryable error.
- **implementation**:
  - Add composite unique/exclusion constraints for the exact tenant, resource, and time dimensions of the invariant.
  - Use atomic `INSERT ... ON CONFLICT`, conditional updates, advisory locks, or serializable transactions as appropriate.
  - Translate unique/deadlock/serialization failures into deterministic API or job outcomes.
  - Keep retry bounds and idempotency keys so a retry cannot turn a conflict into a duplicate effect.
  - Run concurrency load fixtures with barriers so conflicting writers overlap predictably.
- **probe**: Run concurrent writers against each declared invariant and assert exactly one succeeds while the losing transaction returns the documented conflict, then inspect the supporting constraint or lock. Repeat after a client timeout to ensure retry remains safe.
- **failure_modes**: Two checkout requests reserve the same inventory because both pass an availability pre-check. Concurrent username creation creates duplicate identities in an eventually consistent application cache. A uniqueness race causes one request to report success despite its insert being rolled back.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html; https://www.postgresql.org/docs/current/transaction-iso.html

### db-validation
- **definition**: Database-boundary validation rejects values that violate range, enum, precision, temporal, encoding, currency, or unit semantics before they enter durable state. It gives downstream reports, billing, and integrations one canonical representation and rejection rule.
- **implementation**:
  - Use typed columns, domains/enums, `CHECK` constraints, precision/scale, and timezone-aware timestamps where supported.
  - Encode relationships such as start-before-end, nonnegative quantities, valid currency/unit combinations, and bounded percentages.
  - Normalize units and encodings before persistence and reject ambiguous or overflow values rather than truncating them.
  - Keep invalid-value error codes stable for callers and record rejected input without leaking sensitive data.
  - Add boundary fixtures for minimum, maximum, null, invalid enum, overflow, timezone, and reversed interval cases.
- **probe**: Enumerate `CHECK`, domain, enum, and precision definitions and execute boundary fixtures for null, minimum or maximum, invalid enum, overflow, timezone, and reversed-interval cases, expecting rejection. Compare the constraints with the domain data contract to find rules enforced only in application code.
- **failure_modes**: A truncated monetary value produces an incorrect charge. A timestamp without timezone is interpreted differently by regional workers. An invalid unit or enum enters storage and corrupts downstream reports.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html

### constraint-online-validation
- **definition**: New constraints are introduced in a compatibility phase that avoids blocking hot writes, existing rows are validated separately, and enforcement becomes mandatory only after violations are resolved. This separates legacy-data cleanup from the lock-sensitive schema change.
- **implementation**:
  - Add foreign keys or checks as not-valid/unenforced where the engine supports it, then validate with bounded work.
  - Deploy writers that satisfy the new rule before enabling enforcement for all new rows.
  - Measure validation scans, lock modes, replica impact, and timeout behavior on production-like data.
  - Quarantine or repair violating rows through an approved, reconciled backfill rather than disabling the constraint silently.
  - Add a later migration that makes the constraint fully validated and remove temporary compatibility paths.
- **probe**: An assessor must inspect the compatibility migration, validation command, violation-remediation evidence, lock/timeouts, and final enforcement migration. Require proof that new writes cannot introduce additional violations during the validation window.
- **failure_modes**: A full-table validation locks a hot table and causes request timeouts. Legacy invalid rows make deployment fail after writers already depend on the new schema. A temporary unenforced constraint remains indefinitely and permits new violations.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/sql-altertable.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### retention-enforcement
- **definition**: Every dataset has an approved retention duration, deletion trigger, legal-basis or hold behavior, owner, and automated purge or partition-drop mechanism. Enforcement applies across primary tables, replicas, derived stores, exports, logs, and backups where the data remains identifiable.
- **implementation**:
  - Maintain a machine-readable retention catalog keyed to dataset, classification, owner, purpose, and jurisdiction.
  - Encode expiry timestamps, partition lifecycle jobs, tombstone propagation, and deletion retries in controlled automation.
  - Require legal holds to suspend expiry and record release authority and date.
  - Alert on overdue rows, failed purge jobs, undeclared copies, and retention-policy drift.
  - Produce deletion evidence while minimizing sensitive values in logs and tickets.
- **probe**: Present the exact decision: “For each dataset, what retention duration and deletion trigger apply, and how do legal holds override them?” Options: time-after-creation, time-after-last-use, subject/request completion, contract/account closure, or indefinite under named legal hold. Require a named owner, jurisdiction/basis, expiry mechanism, and exception date for each dataset.
- **failure_modes**: Unbounded event tables exhaust storage and degrade queries. Personal data remains in an analytics export after the source retention period. A purge job deletes records under legal hold because hold state was not connected to expiry.
- **severity**: critical
- **applies_if**: all
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj; https://www.postgresql.org/docs/current/ddl-partitioning.html

### gdpr-erasure
- **definition**: Subject erasure is a verified workflow that locates all identifiers, deletes or irreversibly anonymizes eligible records, records lawful exceptions, and produces completion evidence. It covers normalized tables and the operational copies that can still identify the subject.
- **implementation**:
  - Resolve a subject to stable identifiers, aliases, tenant scope, and linked records through a maintained identity map.
  - Execute deletion/anonymization with transactional or resumable steps, explicit foreign-key semantics, and authorization checks.
  - Propagate tombstones to replicas, search indexes, caches, queues, analytics, exports, and future derived records.
  - Handle legal holds and backup retention through the documented exception/suppression policy.
  - Record request ID, scope, completion time, exceptions, sink acknowledgements, and verifier without retaining unnecessary PII.
- **probe**: An assessor must inspect the request authorization, identifier map, sink inventory, deletion/anonymization code, legal-hold handling, retry behavior, and a completed synthetic-subject record. Verify a query across every declared store returns no identifying data or a documented lawful exception.
- **failure_modes**: The source account is deleted but a search index still exposes the user's email. A failed queue consumer leaves derived PII indefinitely. A restore reintroduces a deleted subject because no suppression marker is consulted.
- **severity**: critical
- **applies_if**: all
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj; https://csrc.nist.gov/pubs/sp/800/122/final

### deletion-lineage
- **definition**: Deletion lineage maps every personal-data field to replicas, indexes, analytics copies, logs, exports, queues, and backup expiry. It makes erasure propagation testable by identifying each sink, trigger, acknowledgement, retry, and residual-retention rule.
- **implementation**:
  - Store a versioned field-to-sink manifest with owner, transport, identifier, deletion mechanism, and expected completion time.
  - Generate lineage from schema, event, export, and analytics definitions, then review unrecognized sinks.
  - Emit deletion/tombstone events with stable subject ID and deduplication semantics.
  - Track sink acknowledgements and retry or quarantine failures; alert when completion exceeds policy.
  - Include restore-time suppression for copies that cannot be edited before expiry.
- **probe**: Parse the data-catalog or lineage manifest and run a synthetic subject through deletion, asserting every declared sink receives a tombstone or deletion and no query returns the identifier. Fail on undeclared stores or sinks without an acknowledgement path.
- **failure_modes**: A new export pipeline copies PII without joining the erasure workflow. A cache retains a deleted profile beyond its TTL. A backup restore rehydrates records because backup lineage and suppression were omitted.
- **severity**: critical
- **applies_if**: all
- **sources**: https://csrc.nist.gov/pubs/sp/800/122/final; https://eur-lex.europa.eu/eli/reg/2016/679/oj

### pii-inventory
- **definition**: A PII inventory is a machine-readable catalog of personal fields and identifiers, their purposes, stores, owners, access paths, classification, and retention. It is maintained as part of schema and event changes so unknown personal-data copies cannot escape controls.
- **implementation**:
  - Define field-level annotations for direct identifiers, quasi-identifiers, sensitive categories, purpose, owner, and retention.
  - Scan relational columns, JSON paths, event schemas, exports, logs, and configuration-selected fields for candidate PII.
  - Require code review or CI updates to the inventory when a classified field or sink changes.
  - Link each field to access roles, encryption, masking, erasure, and non-production handling rules.
  - Reconcile catalog entries with runtime data stores on a scheduled basis and triage unknown candidates.
- **probe**: Compare the catalog against schema columns, JSON fields, event schemas, and config-selected exports using a repository scanner, failing on unclassified candidate fields. Sample runtime stores and verify each candidate has owner, purpose, access, retention, and erasure metadata.
- **failure_modes**: A newly added JSON field bypasses access controls because the catalog scanner only checks columns. An unknown analytics sink is omitted from an erasure request. Incident responders cannot bound a breach because no owner knows where a sensitive field is copied.
- **severity**: critical
- **applies_if**: all
- **sources**: https://csrc.nist.gov/pubs/sp/800/122/final

### data-classification
- **definition**: Data classification assigns each dataset and field an approved sensitivity level with handling rules for access, encryption, replication, logging, backups, exports, and non-production use. The classification is a control input, not merely a label, and exceptions have owners and expiry dates.
- **implementation**:
  - Define a small approved taxonomy such as public, internal, confidential, and restricted/sensitive with examples.
  - Annotate schemas, event contracts, object stores, and exports with classification and responsible owner.
  - Map each level to RBAC/ABAC, encryption/key requirements, masking, logging redaction, retention, and environment restrictions.
  - Enforce classification metadata in CI and infrastructure policy checks before new stores or fields ship.
  - Review classifications when purpose, jurisdiction, data content, or sharing partners change.
- **probe**: Present the exact decision: “Which approved sensitivity class applies to each dataset/field, and what handling rules follow?” Options: public; internal; confidential; restricted/sensitive personal, financial, health, credential, or regulated data. Require purpose, owner, access scope, encryption/logging/non-production rules, and a dated exception for ambiguity.
- **failure_modes**: Restricted data is copied into a low-control log or development database. A backup lacks the encryption or access controls required for its fields. A team shares confidential exports externally because the handling rules were not attached to the classification.
- **severity**: important
- **applies_if**: all
- **sources**: https://csrc.nist.gov/glossary/term/data_classification; https://csrc.nist.gov/pubs/sp/800/122/final

### synthetic-nonprod
- **definition**: Development, CI, staging, and migration rehearsals use deterministic synthetic fixtures or properly de-identified data instead of production PII. Any exceptional production-derived dataset has a documented de-identification method, approval, access boundary, and expiry.
- **implementation**:
  - Generate referentially consistent synthetic identities, timestamps, amounts, and edge cases from versioned seeds.
  - Keep fixtures deterministic per environment while preventing production identifiers, secrets, and recognizable free text from appearing.
  - Block production dumps in repository, CI artifacts, logs, snapshots, support bundles, and developer databases with secret/PII scanners.
  - Require isolated access, encryption, retention, and approval records for any approved de-identified sample.
  - Rehearse migrations against scale- and skew-representative synthetic data rather than copying a production database.
- **probe**: Scan fixtures, snapshots, seed files, and CI artifacts for secret or PII signatures, and verify that every non-production dataset is synthetic or has an approved de-identification record. Fail on live-looking identifiers, production connection references, expired approvals, or artifacts accessible outside the intended environment.
- **failure_modes**: A staging dump leaks customer emails through a public CI artifact. Developers copy production data into laptops and it appears in debug logs. A migration rehearsal uses a sanitized-looking dump whose rare identifiers remain reversible.
- **severity**: critical
- **applies_if**: all
- **merges_into**: nonprod-data-isolation
- **sources**: https://csrc.nist.gov/pubs/sp/800/188/final; https://csrc.nist.gov/pubs/sp/800/122/final

### deterministic-seeds
- **definition**: Seed and reference data are versioned, idempotent, environment-scoped, and separated from test fixtures. Reapplying seeds to a clean or existing database produces the intended stable reference state without duplicate rows or destructive startup behavior.
- **implementation**:
  - Version seed scripts with migrations and identify reference rows by stable natural keys or explicit IDs.
  - Use upserts or guarded inserts and make updates explicit, never truncate application data on startup.
  - Separate required production bootstrap/reference data from CI fixtures and local demo data by environment.
  - Run seeds after schema migration with least-privilege credentials and report changed rows.
  - Hash normalized reference data after repeated runs and review any unexpected drift.
- **probe**: Provision a clean database, apply migrations and seeds twice, then compare normalized row counts and hashes and assert the second run makes no changes outside an explicit mutable-data list. Run in a non-production environment and verify production-only bootstrap values are not loaded from test fixtures.
- **failure_modes**: A restart inserts duplicate reference rows and breaks foreign-key or lookup assumptions. A local/demo seed truncates real application data during startup. Two environments receive different feature/reference values and behave inconsistently.
- **severity**: important
- **applies_if**: all
- **sources**: https://documentation.red-gate.com/flyway/reference/concepts/migrations; https://www.postgresql.org/docs/current/ddl-constraints.html

### audit-trail
- **definition**: An audit trail records append-only events for sensitive data changes with actor, request/correlation ID, timestamp, reason, classification, affected resource, and retention controls. It is separate from ordinary debug logs and is protected against silent alteration or premature deletion.
- **implementation**:
  - Write audit events transactionally with the sensitive mutation or use a durable outbox for the audit sink.
  - Include actor/service identity, authorization context, request ID, action, resource/tenant, reason, before/after classification-safe summary, and timestamp.
  - Store append-only with restricted write/read roles, integrity checks or tamper-evident storage, and clock/source policy.
  - Redact secrets and unnecessary PII while retaining enough identifiers for authorized reconstruction.
  - Alert on gaps, clock anomalies, failed writes, retention violations, and unauthorized audit access.
- **probe**: An assessor must inspect the audit schema, append-only permissions, transactional coupling, actor/correlation fields, redaction rules, retention, and tamper/access monitoring. Perform a sensitive mutation and verify one complete event is queryable; attempt alteration with the application role and require rejection.
- **failure_modes**: An administrator changes a sensitive record with no attributable actor or reason. Audit events are lost when the transaction commits but the logging call fails. Logs expose personal data or secrets while trying to prove an incident timeline.
- **severity**: important
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html; https://eur-lex.europa.eu/eli/reg/2016/679/oj

### long-transaction-guardrails
- **definition**: Long-transaction guardrails bound statement, lock-wait, and idle-in-transaction time and alert on sessions, blocked locks, replication slots, and WAL growth that threaten availability. They turn abandoned work into an observable, automatically recoverable condition.
- **implementation**:
  - Set role or workload-specific `statement_timeout`, `lock_timeout`, and `idle_in_transaction_session_timeout` with migration overrides reviewed explicitly.
  - Monitor `pg_stat_activity`, `pg_locks`, transaction age, replication slots, WAL volume, disk headroom, and replica lag.
  - Alert before thresholds that would breach storage, replication, or migration windows; include blocking PID/query owner.
  - Terminate or quarantine abandoned sessions through an authorized automation path with an incident record.
  - Load-test long reads, idle sessions, migrations, and replica disconnects to validate cleanup and recovery.
- **probe**: Query `pg_stat_activity`, `pg_locks`, replication slots, and WAL or replication metrics in a staging load test, failing when configured age, lag, or storage thresholds are exceeded or timeouts are absent. Hold an idle transaction deliberately and verify it is terminated or alerted within the policy window.
- **failure_modes**: An abandoned transaction retains WAL until the primary disk fills. An idle migration session blocks a schema change and queues all requests. A replication slot remains stale after a consumer dies and prevents vacuum or log cleanup.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.postgresql.org/docs/current/monitoring-stats.html; https://www.postgresql.org/docs/current/explicit-locking.html
