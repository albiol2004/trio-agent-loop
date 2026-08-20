# LLMOps / AI-feature production readiness — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 3). Raw item list, pre-synthesis.

### prompt-registry
- **what**: Keep every production prompt in version-controlled source with an immutable prompt ID, revision, parameter schema, and rollback target.
- **why**: Unreviewed prompt edits change behavior without reproducibility or an emergency rollback.
- **check**: probe
- **probe**: Parse the production prompt manifest and fail if any referenced prompt lacks an immutable `id`, `version`, committed source path, or rollback revision.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/prompt-engineering, https://platform.openai.com/docs/guides/production-best-practices

### prompt-release-metadata
- **what**: Record the model/provider, system and tool prompts, decoding parameters, safety configuration, and dataset revision in each release artifact.
- **why**: Missing configuration prevents incident reproduction and makes regressions impossible to attribute.
- **check**: probe
- **probe**: Parse release manifests and fail a build when any LLM deployment omits `provider`, `model`, `prompt_version`, decoding parameters, safety policy, or evaluation dataset revision.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/production-best-practices, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### prompt-regression-suite
- **what**: Run deterministic fixtures and rubric-based regressions for every prompt, tool, model, or decoding change before release.
- **why**: A harmless-looking wording or parameter change can break task quality, refusal behavior, or output contracts.
- **check**: probe
- **probe**: Invoke the repository evaluator on the changed prompt set and fail CI when required task, safety, or schema metrics fall below the checked-in thresholds.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/evals, https://mlflow.org/docs/latest/llms/llm-evaluate/index.html

### golden-dataset-versioning
- **what**: Maintain a versioned, consented golden dataset containing expected answers or rubrics plus adversarial, multilingual, long-context, and long-tail cases.
- **why**: A small happy-path corpus overfits evaluation and hides failures for important cohorts.
- **check**: probe
- **probe**: Parse the dataset manifest and fail when examples lack a stable ID, dataset revision, provenance/license or consent field, rubric/label, and cohort tag.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/evals, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### ci-eval-gate
- **what**: Block promotion when task quality, safety, groundedness, schema validity, latency, or cost regresses beyond explicitly reviewed thresholds.
- **why**: Known model or prompt regressions otherwise reach users because the build only proves that code compiles.
- **check**: probe
- **probe**: Parse CI workflows for an evaluation job that compares the candidate against a baseline and exits nonzero when every required metric threshold is not met.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/evals, https://sre.google/sre-book/service-level-objectives/

### human-sampling
- **what**: Sample production traces by risk, cohort, model, and confidence for blinded human review and feed adjudicated labels back into the evaluation set.
- **why**: Automated judges miss subtle hallucinations, bias, policy edge cases, and unacceptable user experience.
- **check**: judgment
- **applies_if**: all
- **severity**: important
- **sources**: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf, https://platform.openai.com/docs/guides/evals

### model-routing-policy
- **what**: Route each request by explicit capability, context length, latency, data-residency, availability, and cost policy rather than hard-coding one model.
- **why**: A single model choice either violates a requirement or spends and queues more than necessary.
- **check**: probe
- **probe**: Parse the router policy and model catalog, then fail if any production route lacks ordered predicates, supported capabilities, maximum context, owner, and cost/latency metadata.
- **applies_if**: ml-service
- **severity**: important
- **sources**: https://platform.openai.com/docs/models, https://platform.openai.com/docs/guides/production-best-practices

### provider-fallback-chain
- **what**: Implement a bounded, compatibility-tested fallback chain across providers or model tiers for timeouts, rate limits, and server errors while preserving user-visible semantics.
- **why**: Provider outage or quota exhaustion becomes a total application outage without an independent, tested path.
- **check**: probe
- **probe**: Load the fallback policy and run an integration harness that injects 429, timeout, and 5xx responses, asserting ordered fallback, a bounded attempt count, and a safe terminal error.
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/error-codes, https://sre.google/sre-book/handling-overload/

### circuit-breaker-retry
- **what**: Use deadline-aware exponential backoff with jitter, a retry budget, circuit breaking, and idempotency protection around provider calls.
- **why**: Synchronized retries amplify an outage and may duplicate tool actions or billable requests.
- **check**: probe
- **probe**: Parse retry and breaker settings, then inject repeated 429/5xx responses and assert jittered bounded retries, circuit opening, and no retry after the request deadline.
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://sre.google/sre-book/handling-overload/, https://platform.openai.com/docs/guides/error-codes

### token-budget-enforcement
- **what**: Enforce per-request input/output token caps, context truncation or refusal policy, and per-tenant/workflow quotas before calling a provider.
- **why**: Unbounded context causes rejected requests, runaway spend, and latency collapse.
- **check**: probe
- **probe**: Read gateway limits and issue fixtures above input, output, and combined caps, asserting deterministic truncation or rejection before an upstream request is made.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/production-best-practices, https://platform.openai.com/docs/guides/rate-limits

### cost-accounting
- **what**: Attribute input/output tokens, model pricing, retries, cache hits, tenant, feature, and prompt version for every billable call and reconcile totals with provider usage.
- **why**: Teams cannot identify spend regressions, charge tenants, or explain provider invoices without complete attribution.
- **check**: probe
- **probe**: Send a tagged test request, verify the trace contains both token directions and cost dimensions, and compare an aggregate export with the provider usage report.
- **applies_if**: ml-service
- **severity**: important
- **sources**: https://platform.openai.com/docs/api-reference/usage, https://platform.openai.com/docs/guides/production-best-practices

### budget-alerts
- **what**: Define warning and hard-stop budgets for spend, tokens, volume, retries, and queue depth with alerts routed to an accountable owner.
- **why**: Silent quota burn causes billing shocks, throttling, or mid-period service denial.
- **check**: probe
- **probe**: Parse alert rules and budget files, then replay usage above warning and hard-stop thresholds and assert notification plus request blocking or downgrade behavior.
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://sre.google/sre-book/monitoring-distributed-systems/, https://platform.openai.com/docs/guides/rate-limits

### latency-slo
- **what**: Set separate time-to-first-token, completion, queue, retrieval, and end-to-end deadlines for each user journey, model tier, and fallback path.
- **why**: Slow generations exhaust concurrency and violate user-facing availability even when requests eventually succeed.
- **check**: probe
- **probe**: Parse SLO configuration and metric names, then run a timed request fixture that records TTFT and completion latency and fails if the configured deadline or percentile budget is absent.
- **applies_if**: all
- **severity**: critical
- **sources**: https://sre.google/sre-book/service-level-objectives/, https://platform.openai.com/docs/guides/latency-optimization

### streaming-backpressure
- **what**: Stream responses with client cancellation, bounded buffers, disconnect cleanup, and a documented non-streaming fallback.
- **why**: Abandoned clients leak provider work and unbounded buffering saturates workers and memory.
- **check**: probe
- **probe**: Open a streaming request, disconnect after partial output, and assert the upstream call is cancelled, buffers return to baseline, and the fallback path remains usable.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://platform.openai.com/docs/guides/streaming-responses, https://platform.openai.com/docs/guides/latency-optimization

### response-cache-safety
- **what**: Cache only deterministic or explicitly safe responses using keys that include tenant/auth scope, normalized input, prompt revision, model, policy, and knowledge revision with bounded TTLs.
- **why**: Incorrect cache keys leak cross-user data or return stale behavior after prompts, models, or source documents change.
- **check**: probe
- **probe**: Inspect cache-key construction and run same-input fixtures across tenants, prompt revisions, model revisions, and knowledge revisions, asserting isolation and invalidation.
- **applies_if**: all
- **severity**: important
- **sources**: https://platform.openai.com/docs/guides/prompt-caching, https://owasp.org/www-project-top-10-for-large-language-model-applications/

### request-deduplication
- **what**: Deduplicate concurrent identical, side-effect-free requests with an idempotency key and single-flight lock, while excluding actions with external side effects.
- **why**: Retries and double-clicks multiply cost and can execute the same action more than once.
- **check**: probe
- **probe**: Fire concurrent requests with one idempotency key and assert exactly one provider call and shared result, then verify side-effecting tools bypass deduplication.
- **applies_if**: web-api
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc9110, https://platform.openai.com/docs/guides/production-best-practices

### input-guardrails
- **what**: Validate input size and type, detect disallowed content and prompt injection, and isolate untrusted user, retrieval, and tool text from trusted instructions.
- **why**: Attacker-controlled input can bypass policy, exfiltrate secrets, or steer privileged tools.
- **check**: probe
- **probe**: Run a versioned malicious-input corpus through the pre-call guardrail and assert blocked, sanitized, or quarantined outcomes with no provider/tool call for prohibited cases.
- **applies_if**: all
- **severity**: critical
- **sources**: https://owasp.org/www-project-top-10-for-large-language-model-applications/, https://platform.openai.com/docs/guides/safety-best-practices

### output-guardrails
- **what**: Validate every completion against the required schema, safety policy, citation contract, and allowed tool/action set before returning or executing it.
- **why**: Malformed, unsafe, or over-privileged output otherwise reaches users or triggers irreversible side effects.
- **check**: probe
- **probe**: Feed malformed, unsafe, refusal, and unauthorized-tool fixtures to the post-call validator and assert rejection or safe handling before downstream execution.
- **applies_if**: all
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/structured-outputs, https://platform.openai.com/docs/guides/moderation

### jailbreak-red-team
- **what**: Maintain versioned adversarial tests for direct and indirect injection, tool abuse, data exfiltration, multilingual obfuscation, and refusal bypass, with scheduled red-team refreshes.
- **why**: Static filters can pass while adaptive attacks still defeat safeguards in production.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://owasp.org/www-project-top-10-for-large-language-model-applications/, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### pii-redaction
- **what**: Detect and redact or minimize PII, credentials, and secrets in prompts, retrieved context, completions, datasets, and telemetry before persistence or provider transfer.
- **why**: Raw sensitive data in logs or evaluation corpora creates a breach path and can violate privacy commitments.
- **check**: probe
- **probe**: Run a DLP/PII fixture corpus through every prompt and telemetry sink and assert sensitive values are redacted before storage or outbound provider requests.
- **applies_if**: all
- **severity**: critical
- **sources**: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf, https://microsoft.github.io/presidio/

### llm-tracing
- **what**: Emit correlated traces for LLM, retrieval, tool, and guardrail spans containing provider/model, prompt revision, token usage, latency, retries, status, and redacted prompt/completion references.
- **why**: Without end-to-end traces, incidents, quality regressions, and cost anomalies cannot be debugged or attributed.
- **check**: probe
- **probe**: Send a canary request and query its trace by correlation ID, failing if required spans or fields are absent or if raw payload policy is violated.
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://opentelemetry.io/docs/concepts/signals/traces/, https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md

### sensitive-trace-access
- **what**: Encrypt and restrict raw prompts/completions with least-privilege access, redacted-by-default dashboards, retention limits, deletion workflows, and audited access.
- **why**: Observability becomes a secondary exfiltration and insider-risk channel when sensitive model I/O is broadly visible.
- **check**: probe
- **probe**: Parse telemetry ACL, encryption, retention, and deletion configuration and run an access test proving unauthorized roles cannot retrieve raw prompt or completion content.
- **applies_if**: all
- **severity**: critical
- **sources**: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf, https://opentelemetry.io/docs/specs/semconv/

### feedback-capture
- **what**: Capture consented user ratings, corrections, overrides, and reviewer labels linked to trace ID, prompt/model revision, cohort, and dataset revision with abuse controls.
- **why**: Quality drift and false-positive guardrails remain invisible when feedback cannot be joined to the exact behavior that produced it.
- **check**: probe
- **probe**: Submit positive, negative, correction, and opt-out fixtures and validate stored feedback has the required join keys, consent state, and rate limits.
- **applies_if**: all
- **severity**: important
- **sources**: https://platform.openai.com/docs/guides/evals, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### rag-index-freshness
- **what**: Version ingestion and deletion pipelines and expose source-to-index freshness lag, failed jobs, watermark, ACL propagation, and tombstone status.
- **why**: Users receive obsolete or unauthorized answers when indexes silently fall behind or deletions do not propagate.
- **check**: probe
- **probe**: Compare source `updated_at` and deletion manifests with the index watermark for a canary corpus and fail when lag, failed-job count, or ACL propagation exceeds its SLO.
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### rag-retrieval-evaluation
- **what**: Evaluate chunk size and overlap, metadata filters, embedding revision, top-k, reranking, and context packing on a labeled retrieval set.
- **why**: Poor retrieval omits the evidence or adds noisy context that increases hallucination, latency, and token cost.
- **check**: probe
- **probe**: Run the retrieval evaluator and assert checked-in recall@k or nDCG thresholds for each important query cohort and document type.
- **applies_if**: data-pipeline
- **severity**: important
- **sources**: https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview, https://platform.openai.com/docs/guides/retrieval

### rag-citation-grounding
- **what**: Require citations to resolve to retrieved source spans, measure claim-level support, and abstain or ask for clarification when evidence is insufficient.
- **why**: Fluent but ungrounded claims destroy trust even when the retrieval layer technically returns documents.
- **check**: probe
- **probe**: Parse every returned citation ID against retrieved chunk IDs and run a claim-grounding fixture set, failing on unresolved citations or unsupported claims above the allowed rate.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### fine-tune-data-lineage
- **what**: Version fine-tuning data, preprocessing code, base model, hyperparameters, safety filters, hashes, licensing, consent, and removal provenance for every training run.
- **why**: Untracked training inputs make behavior irreproducible, unsafe to audit, and difficult to remove or legally explain.
- **check**: probe
- **probe**: Parse each fine-tune run manifest and verify immutable hashes and nonempty lineage fields for data, code, base model, parameters, license, consent, and exclusions.
- **applies_if**: data-pipeline
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/fine-tuning, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### fine-tune-eval-promotion
- **what**: Require holdout task and safety evaluations against the base model, canary rollout, approval, and tested rollback before promoting a fine-tuned model.
- **why**: Overfitting, memorization, or new unsafe behavior can replace a safer baseline without detection.
- **check**: probe
- **probe**: Parse the promotion workflow and reject candidates lacking a holdout evaluation artifact, baseline comparison, canary result, approver, and rollback reference.
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://platform.openai.com/docs/guides/fine-tuning, https://platform.openai.com/docs/guides/evals

### expensive-endpoint-rate-limit
- **what**: Apply authenticated per-user and per-tenant quotas, concurrency caps, weighted token-cost limits, and abuse detection to expensive LLM endpoints.
- **why**: Bots or noisy tenants can monopolize capacity, trigger provider throttling, and exhaust shared budgets.
- **check**: probe
- **probe**: Exceed each quota with an authenticated test client and assert a 429 response with `Retry-After`, bounded concurrency, and no upstream call after the hard limit.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9331, https://platform.openai.com/docs/guides/rate-limits

### tool-action-authorization
- **what**: Give model-invoked tools least privilege, validate arguments server-side, require confirmation for destructive actions, and log authorization decisions.
- **why**: Prompt injection can turn a text-generation feature into unauthorized external side effects.
- **check**: probe
- **probe**: Inspect the tool allowlist and argument schemas, then run a destructive-action fixture and assert authorization or human confirmation is required before execution.
- **applies_if**: all
- **severity**: critical
- **sources**: https://owasp.org/www-project-top-10-for-large-language-model-applications/, https://platform.openai.com/docs/guides/safety-best-practices

### model-deprecation-inventory
- **what**: Maintain a production model inventory with owner, provider, capabilities, cost and latency baselines, retirement date, replacement, and migration/evaluation plan.
- **why**: Provider retirement otherwise causes emergency migration with silent quality, compatibility, or cost regressions.
- **check**: probe
- **probe**: Parse the model inventory and fail when any production model lacks an owner, provider sunset status/date, compatible replacement, and completed migration-eval reference.
- **applies_if**: ml-service
- **severity**: critical
- **sources**: https://platform.openai.com/docs/deprecations, https://platform.openai.com/docs/guides/production-best-practices

### structured-output-contract
- **what**: Use strict structured-output schemas or equivalent validation with bounded repair attempts and explicit refusal or partial-result handling.
- **why**: Downstream parsers and actions fail when completions are malformed, ambiguous, or truncated.
- **check**: probe
- **probe**: Send valid, malformed, refusal, and truncated-output fixtures and assert schema validation, at most the configured repair count, and a safe typed error for unrecoverable results.
- **applies_if**: all
- **severity**: important
- **sources**: https://platform.openai.com/docs/guides/structured-outputs, https://platform.openai.com/docs/guides/safety-best-practices
