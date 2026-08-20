# Data integrity & database — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 2). Raw item list, pre-synthesis.

### migration-expand-contract
- **what**: Use an expand/contract (parallel-change) sequence: add backward-compatible schema, deploy readers and writers, backfill, switch reads, and remove the old shape only after every client has migrated.
- **why**: It prevents mixed-version deploys from breaking old clients or taking locks that cause production downtime.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://martinfowler.com/bliki/ParallelChange.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-bounded-backfill
- **what**: Run large backfills in idempotent, resumable batches with a checkpoint, throttling, and a defined pause or abort path.
- **why**: It prevents one transaction from exhausting WAL, locks, replica capacity, or maintenance windows and makes retries safe.
- **check**: probe
- **probe**: Parse migration and backfill jobs for a stable key-range or cursor, bounded batch size, persisted checkpoint, retry handling, and rate or lock controls, rejecting an unbounded full-table transaction.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/; https://www.postgresql.org/docs/current/continuous-archiving.html

### migration-rollback-forward-fix
- **what**: For every migration, document and test either a reversible down path or an explicitly safe forward fix, including data-loss boundaries and compatible application versions.
- **why**: It prevents an incident from becoming unrecoverable when a schema change partially succeeds or the new binary must be rolled back.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://documentation.red-gate.com/flyway/reference/commands/undo; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-lock-safety
- **what**: Preflight DDL lock duration and use online or concurrent forms, bounded transactions, and short lock timeouts for production-sized tables.
- **why**: It prevents queued requests, deadlocks, and cascading outage when schema changes contend with live traffic.
- **check**: probe
- **probe**: Parse migration SQL and database settings for online or concurrent operations, lock and statement timeouts, and bounded transaction scopes, rejecting table rewrites or unbounded DDL without an approved exception.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/; https://www.postgresql.org/docs/current/explicit-locking.html

### migration-schema-drift
- **what**: Make migration history the only schema authority and detect drift between a fresh database, the expected schema, and each deployed database.
- **why**: It prevents an untracked manual change from making deploys, restores, or replicas behave differently.
- **check**: probe
- **probe**: Create a disposable database, apply every migration, dump normalized DDL, and diff it against the checked-in schema snapshot and recorded production migration version.
- **applies_if**: all
- **severity**: important
- **sources**: https://documentation.red-gate.com/flyway/reference/concepts/migrations; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-deploy-order
- **what**: Encode schema-before-code and delayed contract cleanup in the deployment pipeline, with an overlap window that covers old and new binaries.
- **why**: It prevents a rollback or staggered instance from querying columns, indexes, or constraints it cannot understand.
- **check**: probe
- **probe**: Parse the CI/CD DAG and release manifests to verify expand migrations precede application rollout and contract migrations require an explicit later gate after old versions are drained.
- **applies_if**: all
- **severity**: critical
- **sources**: https://martinfowler.com/bliki/ParallelChange.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### migration-rehearsal
- **what**: Rehearse every high-risk migration on a production-sized clone with representative indexes, concurrency, traffic, and replica-lag measurement.
- **why**: It exposes table rewrites, lock waits, WAL growth, and runtime overruns before the production change.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/; https://www.postgresql.org/docs/current/monitoring-stats.html

### migration-reconciliation
- **what**: Gate cutover on automated old/new column or table reconciliation using counts, null and error rates, checksums, and sampled business invariants.
- **why**: It prevents silently promoting a backfill that dropped, duplicated, or transformed records incorrectly.
- **check**: probe
- **probe**: Run the repository reconciliation command against old and new representations and fail when primary-key sets, counts, hashes, or invariant-query results differ beyond an explicit threshold.
- **applies_if**: all
- **severity**: critical
- **sources**: https://martinfowler.com/bliki/ParallelChange.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### backup-rpo-retention
- **what**: Configure automated backups and WAL or change-log retention to meet a written recovery-point objective, recovery-time objective, and restore-window requirement.
- **why**: It prevents discovering after corruption that the newest usable recovery point is too old or has already expired.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/backup.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html

### backup-restore-drill
- **what**: Restore backups on a fixed schedule into an isolated environment and exercise application startup, representative reads and writes, and measured recovery time.
- **why**: It prevents backup-success metrics from masking unusable credentials, missing objects, incompatible versions, or corrupt data.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/backup.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html

### backup-integrity-check
- **what**: Verify backup manifests, checksums, and required WAL continuity before declaring a backup usable.
- **why**: It prevents restoring an incomplete, corrupted, or tampered artifact during an outage.
- **check**: probe
- **probe**: For PostgreSQL base backups, run `pg_verifybackup` and validate the required WAL segment range; for the selected provider, parse backup-verification status and fail on missing checksum or manifest evidence.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/app-pgverifybackup.html; https://www.postgresql.org/docs/current/continuous-archiving.html

### pitr-capability
- **what**: Archive WAL or equivalent change logs continuously and prove recovery to an arbitrary target timestamp before the retention horizon.
- **why**: It limits data loss from operator error or corruption to the committed recovery-point objective instead of the last snapshot.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/continuous-archiving.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html

### backup-access-isolation
- **what**: Encrypt backups in transit and at rest, isolate them from the primary account or project, and grant restore access through least-privilege break-glass credentials.
- **why**: It prevents a database compromise or operator mistake from destroying or exposing every recovery copy.
- **check**: probe
- **probe**: Parse infrastructure and provider backup settings for encryption keys, TLS transfer, an isolated or immutable destination, retention lock, and a separate restore role, failing when any required control is absent.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html; https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final

### backup-erasure-policy
- **what**: Define how retention, legal holds, and subject erasure interact with snapshots, WAL, replicas, exports, and backup expiry, with an accountable owner.
- **why**: It prevents both unlawful indefinite retention of personal data and destructive deletion that violates a legal hold.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html

### restore-consistency-checks
- **what**: After restore or failover, run referential-integrity, row-count or checksum, sequence, and critical-aggregate checks before serving traffic.
- **why**: It prevents a technically successful recovery from serving a logically inconsistent database.
- **check**: probe
- **probe**: Run a versioned post-restore SQL suite that checks constraint violations, orphan counts, primary-key uniqueness, sequence high-water marks, and selected aggregate totals, returning nonzero on any failure.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/amcheck.html; https://www.postgresql.org/docs/current/ddl-constraints.html

### replication-topology
- **what**: Document the authoritative writer, synchronous or asynchronous standbys, read replicas, regions, quorum, and each link's recovery-point, recovery-time, and consistency contract.
- **why**: It prevents an undocumented replica from becoming a stale or split-brain source during incident response.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/warm-standby.html; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html

### replication-lag-freshness
- **what**: Monitor write or replay LSN and equivalent lag, and gate read routing, jobs, and failover promotion on explicit freshness thresholds.
- **why**: It prevents stale reads and promotion of a replica that would violate the declared recovery-point objective.
- **check**: probe
- **probe**: Query database or provider replication metrics, alert when byte or time lag exceeds the configured threshold, and verify in an integration probe that the router refuses stale replicas.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/monitoring-stats.html; https://www.postgresql.org/docs/current/warm-standby.html

### failover-runbook
- **what**: Maintain a versioned failover runbook covering detection, writer fencing, promotion, endpoint and secret rotation, pool draining, verification, and return to service.
- **why**: It prevents improvisation that creates dual writers, lost writes, or a prolonged outage.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/34/r1/final; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html

### failover-drill
- **what**: Exercise planned and unplanned failover on a fixed cadence and record measured recovery time, recovery-point loss, replica lag, client retry behavior, and reconciliation results.
- **why**: It prevents a failover plan from failing due to stale DNS, incompatible clients, or untested application assumptions.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/34/r1/final; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html

### split-brain-fencing
- **what**: Require an authoritative lease, quorum, or provider fencing mechanism that makes the old writer unable to accept writes before promotion.
- **why**: It prevents divergent histories and unreconcilable duplicate or conflicting records.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/warm-standby.html; https://patroni.readthedocs.io/en/latest/

### transaction-boundary
- **what**: Keep every business invariant's database mutations in one explicit transaction and commit its outbox record in that same transaction.
- **why**: It prevents partial state when a process, connection, or dependency fails between two writes.
- **check**: probe
- **probe**: Use a static or transaction-trace probe to assert that the business write and outbox insert share one `BEGIN` and `COMMIT` or framework transaction and that no external call occurs inside the commit-critical section.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/tutorial-transactions.html; https://microservices.io/patterns/data/transactional-outbox.html

### transaction-isolation
- **what**: Choose and document isolation, locking, and bounded retry behavior for every cross-row invariant or concurrent workflow.
- **why**: It prevents write skew, lost updates, phantom decisions, and deadlocks that only appear under production concurrency.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/transaction-iso.html; https://www.postgresql.org/docs/current/explicit-locking.html

### idempotent-replay
- **what**: Give retried API commands, jobs, migrations, and event consumers an idempotency key or deduplication constraint with a defined duplicate response.
- **why**: It prevents at-least-once delivery and client retries from charging, provisioning, or mutating the same business fact twice.
- **check**: probe
- **probe**: Inspect schemas for a unique idempotency or deduplication key and run the operation twice with the same key, asserting one committed effect and a deterministic response.
- **applies_if**: all
- **severity**: critical
- **sources**: https://docs.stripe.com/api/idempotent_requests; https://microservices.io/patterns/data/transactional-outbox.html

### transactional-outbox
- **what**: Persist each externally published event in an outbox table within the business transaction, then relay it with retries and observable delivery state.
- **why**: It prevents the database commit and message publish from diverging when either side fails.
- **check**: probe
- **probe**: Locate the outbox table, transaction write, relay polling or CDC, retry and backoff logic, and sent or lease state; kill the relay between commit and publish and verify eventual delivery without a duplicate business effect.
- **applies_if**: all
- **severity**: critical
- **sources**: https://microservices.io/patterns/data/transactional-outbox.html; https://www.postgresql.org/docs/current/tutorial-transactions.html

### outbox-delivery-order
- **what**: Specify per-aggregate event ordering, duplicate handling, poison-message quarantine, and outbox retention or compaction.
- **why**: It prevents consumers from applying stale state or blocking an entire stream on one malformed event.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://microservices.io/patterns/data/transactional-outbox.html

### db-integrity-constraints
- **what**: Enforce non-null, unique, primary-key, foreign-key, check, exclusion, and domain invariants in the database rather than only in application code.
- **why**: It prevents alternate writers, race conditions, and bugs from committing impossible state.
- **check**: probe
- **probe**: Introspect `pg_constraint` and `information_schema` or the engine equivalent and compare required invariants with migration definitions, failing when a rule exists only in application validation.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html

### foreign-key-delete-semantics
- **what**: Choose and test explicit foreign-key delete and update actions, including whether historical records are retained, anonymized, restricted, or cascaded.
- **why**: It prevents orphaned rows, accidental tenant-wide cascades, and deletion workflows that violate retention requirements.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html; https://eur-lex.europa.eu/eli/reg/2016/679/oj

### concurrency-invariant
- **what**: Represent uniqueness and allocation races with database uniqueness or exclusion constraints or serializable or locked transactions, not check-then-insert code.
- **why**: It prevents two concurrent requests from both passing a pre-check and creating conflicting state.
- **check**: probe
- **probe**: Run concurrent writers against each declared invariant and assert exactly one succeeds while the losing transaction returns the documented conflict, then inspect the supporting constraint or lock.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html; https://www.postgresql.org/docs/current/transaction-iso.html

### db-validation
- **what**: Validate ranges, enums, precision, temporal relationships, encoding, and currency or unit semantics at the database boundary.
- **why**: It prevents malformed or ambiguous values from propagating into reports, billing, and downstream systems.
- **check**: probe
- **probe**: Enumerate `CHECK`, domain, enum, and precision definitions and execute boundary fixtures for null, minimum or maximum, invalid enum, overflow, timezone, and reversed-interval cases, expecting rejection.
- **applies_if**: all
- **severity**: important
- **sources**: https://www.postgresql.org/docs/current/ddl-constraints.html

### constraint-online-validation
- **what**: Add new constraints in a non-blocking compatibility phase, validate existing rows separately, and only then make the constraint mandatory for writes.
- **why**: It prevents a constraint deployment from locking a hot table or failing halfway because legacy rows violate it.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.postgresql.org/docs/current/sql-altertable.html; https://docs.gitlab.com/development/database/avoiding_downtime_in_migrations/

### retention-enforcement
- **what**: Assign every dataset a retention duration, deletion trigger, legal-basis or hold behavior, owner, and automated purge or partition-drop mechanism.
- **why**: It prevents indefinite accumulation of sensitive data and unpredictable storage and cost growth.
- **check**: user-decision
- **applies_if**: all
- **severity**: critical
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj; https://www.postgresql.org/docs/current/ddl-partitioning.html

### gdpr-erasure
- **what**: Implement a verified subject-erasure workflow that locates identifiers, deletes or irreversibly anonymizes eligible records, and records exceptions and completion evidence.
- **why**: It prevents incomplete responses when personal data exists in normalized tables, replicas, indexes, caches, exports, or derived stores.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://eur-lex.europa.eu/eli/reg/2016/679/oj; https://csrc.nist.gov/pubs/sp/800/122/final

### deletion-lineage
- **what**: Maintain lineage from each personal-data field to replicas, search indexes, analytics copies, logs, exports, and backup expiry so erasure propagation is testable.
- **why**: It prevents deleting the source row while leaving recoverable copies that still identify the subject.
- **check**: probe
- **probe**: Parse the data-catalog or lineage manifest and run a synthetic subject through deletion, asserting every declared sink receives a tombstone or deletion and no query returns the identifier.
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/122/final; https://eur-lex.europa.eu/eli/reg/2016/679/oj

### pii-inventory
- **what**: Keep a machine-readable inventory of PII fields, identifiers, processing purpose, storage location, owners, access paths, and retention.
- **why**: It prevents unknown personal-data stores from escaping access controls, deletion requests, and breach scope.
- **check**: probe
- **probe**: Compare the catalog against schema columns, JSON fields, event schemas, and config-selected exports using a repository scanner, failing on unclassified candidate fields.
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/122/final

### data-classification
- **what**: Classify each dataset and field with an approved sensitivity level and handling rules for access, encryption, replication, logs, backups, and non-production use.
- **why**: It prevents low-sensitivity defaults from exposing restricted data through ordinary operational paths.
- **check**: user-decision
- **applies_if**: all
- **severity**: important
- **sources**: https://csrc.nist.gov/glossary/term/data_classification; https://csrc.nist.gov/pubs/sp/800/122/final

### synthetic-nonprod
- **what**: Use deterministic synthetic or properly de-identified fixtures in development, CI, staging, and migration rehearsals instead of production PII.
- **why**: It prevents test dumps, logs, and developer environments from becoming uncontrolled personal-data copies.
- **check**: probe
- **probe**: Scan fixtures, snapshots, seed files, and CI artifacts for secret or PII signatures, and verify that every non-production dataset is synthetic or has an approved de-identification record.
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/188/final; https://csrc.nist.gov/pubs/sp/800/122/final

### deterministic-seeds
- **what**: Version seed and reference data, make it idempotent and environment-scoped, and keep required production bootstrap data separate from test fixtures.
- **why**: It prevents non-reproducible deployments, duplicate reference rows, and destructive startup behavior.
- **check**: probe
- **probe**: Provision a clean database, apply migrations and seeds twice, then compare normalized row counts and hashes and assert the second run makes no changes outside an explicit mutable-data list.
- **applies_if**: all
- **severity**: important
- **sources**: https://documentation.red-gate.com/flyway/reference/concepts/migrations; https://www.postgresql.org/docs/current/ddl-constraints.html

### audit-trail
- **what**: Record append-only audit events for sensitive data changes with actor, request or correlation ID, timestamp, reason, data classification, and retention controls.
- **why**: It prevents untraceable mutations and makes integrity, erasure, and incident reconstruction unverifiable.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html; https://eur-lex.europa.eu/eli/reg/2016/679/oj

### long-transaction-guardrails
- **what**: Set statement, lock, and idle-in-transaction timeouts and alert on long-running transactions, blocked sessions, replication slots, and WAL growth.
- **why**: It prevents abandoned sessions from blocking migrations, retaining dead tuples or WAL, and exhausting storage.
- **check**: probe
- **probe**: Query `pg_stat_activity`, `pg_locks`, replication slots, and WAL or replication metrics in a staging load test, failing when configured age, lag, or storage thresholds are exceeded or timeouts are absent.
- **applies_if**: all
- **severity**: important
- **sources**: https://www.postgresql.org/docs/current/monitoring-stats.html; https://www.postgresql.org/docs/current/explicit-locking.html
