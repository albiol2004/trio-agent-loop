# LLMOps production glossary

### prompt-registry
- **definition**: A prompt registry is the authoritative, version-controlled catalog of prompts used by production features. Each prompt has an immutable identifier and revision, a declared parameter/tool schema, an owning team, and a known rollback revision so the exact behavior can be reconstructed.
- **implementation**:
  - Store prompt templates, system instructions, tool descriptions, and metadata in reviewed source control; deploy by immutable commit or digest rather than mutable labels.
  - Require a manifest entry such as `{id, version, source, parameters, tools, owner, rollback_version}` and reject duplicate IDs or mutable revisions.
  - Resolve a prompt to a content hash at build time and include that hash in the release artifact and request telemetry.
  - Keep the previous known-good revision available and make rollback a single release/configuration operation.
- **probe**: Parse the production prompt manifest; for every referenced prompt assert nonempty immutable `id`, semantic or monotonically increasing `version`, committed `source` path, parameter schema, owner, content digest, and `rollback_version` that exists. Fail on duplicate IDs, uncommitted paths, or a mutable alias used as the deployed reference.
- **failure_modes**: An emergency wording edit silently changes refusal behavior with no reproducible prior version. A release cannot be rolled back because the provider only retained a mutable prompt name.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/prompt-engineering, https://platform.openai.com/docs/guides/production-best-practices

### prompt-release-metadata
- **definition**: Prompt-release metadata is the complete configuration record attached to an LLM release, not merely the application version. It identifies provider and model, prompt and tool revisions, decoding and safety settings, and the evaluation data revision needed to reproduce behavior and attribute changes.
- **implementation**:
  - Generate a release manifest containing `provider`, `model`, `prompt_version`, system/tool prompt digests, decoding parameters, safety-policy version, and evaluation-dataset revision.
  - Publish the manifest alongside the deploy artifact and embed its digest in traces, logs, and model responses where appropriate.
  - Treat omitted fields and unknown provider defaults as build failures; record explicit defaults (for example temperature and max output tokens).
  - Preserve manifests immutably for the retention period and link them to the change request and approver.
- **probe**: Parse each LLM deployment manifest and assert `provider`, `model`, `prompt_version`, tool/system prompt digests, decoding parameters, safety policy, and evaluation dataset revision are present and non-defaulted where required. Build a candidate artifact with one field removed and verify the release gate exits nonzero.
- **failure_modes**: An incident cannot be reproduced because a provider silently changed a default temperature. A cost or quality regression is blamed on code when the deployed model tier and safety policy were never recorded.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/production-best-practices, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### prompt-regression-suite
- **definition**: A prompt-regression suite is a repeatable set of task, safety, schema, and quality evaluations run whenever prompts, tools, models, or decoding settings change. It compares the candidate with a checked-in baseline and makes behavior changes visible before promotion.
- **implementation**:
  - Keep deterministic fixtures with stable IDs, expected schemas or rubrics, risk labels, and representative cohorts; control temperature, seeds, and provider versions where possible.
  - Evaluate task success, refusal/safety behavior, groundedness, schema validity, latency, and cost rather than relying on a single aggregate score.
  - Store candidate outputs, evaluator versions, thresholds, and baseline comparison artifacts for review.
  - Require explicit approval for an intentional regression or threshold change; otherwise block promotion.
- **probe**: Invoke the repository evaluator for the changed prompt set with a fixed dataset revision; compare candidate metrics to the baseline and exit nonzero if any required task, safety, groundedness, or schema metric falls below its checked-in threshold. Verify the report identifies failed fixture IDs.
- **failure_modes**: A harmless-looking instruction rewrite causes a tool argument to become invalid. A model upgrade improves average answers while introducing unsafe refusals for a small but important cohort.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/evals, https://mlflow.org/docs/latest/llms/llm-evaluate/index.html

### golden-dataset-versioning
- **definition**: A golden dataset is a versioned, consented evaluation corpus with stable examples, expected answers or rubrics, provenance, and cohort labels. It deliberately includes adversarial, multilingual, long-context, and long-tail cases so evaluation reflects the population and risks that matter.
- **implementation**:
  - Give every example a stable ID, dataset revision, provenance/license or consent record, rubric/label, and cohort tag.
  - Separate train, tuning, and holdout examples; restrict access to sensitive examples and record dataset transformations.
  - Add cases for refusal, injection, tool use, retrieval grounding, truncation, malformed output, languages, and context limits.
  - Review additions and removals, hash manifests, and make the dataset revision part of every evaluation and release record.
- **probe**: Parse the dataset manifest and fail when any example lacks stable `id`, revision, provenance/license or consent, rubric/label, or cohort. Check that holdout IDs do not overlap tuning IDs and that required adversarial, multilingual, long-context, and long-tail cohorts have nonzero counts.
- **failure_modes**: A benchmark passes because it contains only short English happy paths. A user deletion request cannot be honored because the evaluation corpus has no provenance or consent trail.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/evals, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### ci-eval-gate
- **definition**: A CI evaluation gate is an enforceable promotion check that compares a candidate LLM behavior against a baseline across required quality and operational metrics. It fails closed when task quality, safety, groundedness, schema validity, latency, or cost breaches a reviewed threshold.
- **implementation**:
  - Define thresholds per feature and cohort in version-controlled configuration, including minimum quality and maximum latency/cost/error budgets.
  - Run the evaluator from the CI workflow against the candidate artifact and pinned dataset/model configuration.
  - Emit a machine-readable report, fixture-level failures, and a human approval path for documented exceptions.
  - Ensure deployment jobs depend on the gate's successful status rather than merely running it in parallel.
- **probe**: Parse CI workflow files and assert a required evaluation job consumes the candidate, compares against a baseline, and exits nonzero on threshold failure. Run a deliberately degraded fixture or threshold in a temporary branch and verify the deployment job is blocked.
- **failure_modes**: A broken output schema reaches production because CI only compiled application code. A latency increase exhausts workers during rollout despite passing functional tests.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/evals, https://sre.google/sre-book/service-level-objectives/

### human-sampling
- **definition**: Human sampling is a privacy-aware review process that selects production traces by risk, cohort, model, and confidence for blinded adjudication. Reviewer labels and disagreements are retained as quality evidence and fed back into the evaluation corpus and guardrail tuning.
- **implementation**:
  - Define sampling strata and rates that over-sample high-risk, low-confidence, novel-model, and complaint-linked requests while preserving representative traffic.
  - Remove or mask unnecessary identifiers, obtain consent or another lawful basis, and enforce reviewer role separation and retention limits.
  - Use a rubric for correctness, groundedness, safety, bias, and user experience; require dual review for high-impact cases and adjudicate disagreements.
  - Link labels to trace, prompt/model revision, and dataset revision without exposing raw payloads by default.
- **probe**: **Evidence assessor inspects** the sampling policy, consent/privacy controls, blinded reviewer workflow, stratified sample reports, inter-reviewer agreement, adjudication records, and examples showing labels feed a versioned evaluation set. Confirm high-risk cohorts are not excluded and reviewer access is audited.
- **failure_modes**: An automated judge misses a subtle hallucination that a human reviewer would catch. Reviewers systematically see only easy cases, hiding poor performance for a vulnerable cohort.
- **severity**: important
- **applies_if**: all
- **sources**: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf, https://platform.openai.com/docs/guides/evals

### model-routing-policy
- **definition**: A model-routing policy maps a request's explicit requirements to an approved model/provider route. It considers capability, context length, latency, residency, availability, and cost, and makes the decision explainable and testable instead of embedding a single model choice in application code.
- **implementation**:
  - Maintain a model catalog with capability tags, context limits, regions, owners, price/unit, latency baseline, lifecycle status, and compatibility constraints.
  - Express ordered predicates and route priorities in version-controlled policy, with a safe default and an explicit refusal when no compliant model exists.
  - Validate prompt/tool/schema compatibility before selecting a route; never route sensitive data to a provider or region disallowed by policy.
  - Emit the matched rule, candidate set, and selected model in redacted telemetry for audit and cost analysis.
- **probe**: Parse router policy and model catalog; for every production route assert ordered predicates, supported capabilities, maximum context, owner, region/residency, and cost/latency metadata. Run fixtures varying context, residency, capability, and budget and verify the selected route is the first compliant one or a safe no-route error.
- **failure_modes**: A long-context request is sent to a model that truncates critical instructions. A residency requirement is violated when an outage fallback ignores region policy.
- **severity**: important
- **applies_if**: ml-service
- **sources**: https://platform.openai.com/docs/models, https://platform.openai.com/docs/guides/production-best-practices

### provider-fallback-chain
- **definition**: A provider fallback chain is an ordered, bounded set of compatible provider or model-tier alternatives used when the primary cannot serve a request. It preserves the feature's output, safety, residency, and schema semantics, and terminates with a safe error when no compliant alternative remains.
- **implementation**:
  - Declare ordered routes with compatibility tests for prompts, tools, context limits, structured outputs, safety policy, region, and data handling.
  - Classify retryable provider failures (timeouts, 429, selected 5xx) separately from non-retryable policy, authentication, validation, and content errors.
  - Bound attempts and total deadline; carry a correlation/idempotency key and avoid fallback for side-effecting work unless the operation is safely idempotent.
  - Record selected route, fallback reason, and terminal status without logging sensitive payloads; alert on fallback rate and exhaustion.
- **probe**: Load the fallback policy and use a stub provider harness to inject 429, timeout, and 5xx responses. Assert ordered compatible fallback, bounded attempts and wall-clock deadline, preserved schema/safety/residency settings, and a safe terminal error after exhaustion; inject a non-retryable 4xx and assert no fallback.
- **failure_modes**: One provider quota outage becomes a total application outage because no independent route exists. A fallback model emits a format the downstream parser cannot consume. A retrying fallback repeats a tool action.
- **merges_into**: provider-fallback-chain
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://platform.openai.com/docs/guides/error-codes, https://sre.google/sre-book/handling-overload/

### circuit-breaker-retry
- **definition**: Provider-call retry protection combines a request deadline, exponential backoff with jitter, a shared retry budget, and a circuit breaker. It limits load during an outage and prevents a failed call from being duplicated after its useful deadline or from repeating non-idempotent work.
- **implementation**:
  - Configure per-operation connect/read deadlines, overall deadline, maximum attempts, exponential delay cap, and full/equal jitter; honor provider `Retry-After` within the deadline.
  - Use a retry budget per service/tenant and classify retryable status codes and transport errors; never retry validation, authorization, policy, or unsafe tool errors.
  - Implement closed/open/half-open breaker states with failure thresholds, cooldown, probe limits, and metrics for opens and rejected calls.
  - Require idempotency keys or an outbox/transactional contract before retrying any billable or side-effecting operation.
- **probe**: Parse retry and breaker settings, then inject repeated 429/5xx responses with timestamps. Assert delays are bounded and jittered, total attempts and wall time stay within budget, the breaker opens and rejects calls, half-open probes are limited, and no attempt occurs after the request deadline.
- **failure_modes**: Synchronized clients retry together and amplify a provider outage. A timed-out request completes upstream and a retry bills the user twice. An open circuit is absent, so every worker waits on a failing provider.
- **merges_into**: retry-backoff-breakers
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://sre.google/sre-book/handling-overload/, https://platform.openai.com/docs/guides/error-codes

### token-budget-enforcement
- **definition**: Token-budget enforcement applies deterministic input, output, and combined token limits before an upstream provider call. It defines whether excess context is truncated, summarized, queued, downgraded, or rejected and also enforces per-tenant/workflow usage quotas.
- **implementation**:
  - Tokenize with the target model's tokenizer (or a conservative documented estimate) before provider invocation and reserve output budget against context-window limits.
  - Set per-request input/output/total caps and per-tenant daily or rolling-window quotas in configuration with explicit units and owners.
  - Apply safe, ordered truncation or summarization that preserves system instructions and required evidence; return a typed limit error when policy forbids loss.
  - Meter rejected, truncated, provider-billed, and fallback tokens separately and expose remaining budget to admission control.
- **probe**: Read gateway limits and issue fixtures just below, exactly at, and above input, output, and combined caps. Assert deterministic truncation or rejection occurs before an upstream call, tenant quota is enforced, and the recorded token count matches the selected tokenizer.
- **failure_modes**: A large retrieved context is rejected by the provider after expensive preprocessing. An unbounded conversation consumes the tenant's budget and creates latency collapse for everyone.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/production-best-practices, https://platform.openai.com/docs/guides/rate-limits

### cost-accounting
- **definition**: LLM cost accounting attributes every billable attempt to its input/output tokens, model pricing, retries, cache outcome, tenant, feature, and prompt revision. It reconciles application estimates with provider usage so operators can explain invoices and locate spend regressions.
- **implementation**:
  - Capture provider-reported prompt and completion tokens per attempt, including cached-token fields where available, and record the pricing-table revision used to calculate cost.
  - Tag requests with tenant, feature/workflow, environment, model/provider, prompt version, route, and trace ID; keep tags bounded and prevent user content from becoming a label.
  - Aggregate by day and billing period, separate successful, failed, fallback, and retry attempts, and reconcile totals to provider usage exports with a tolerance policy.
  - Version pricing tables and alert on missing usage, unknown models, or reconciliation variance.
- **probe**: Send a tagged canary request that exercises a cache hit/miss and a retry; query its trace/export and assert input/output token directions, pricing revision, tenant, feature, prompt version, and attempt count are present. Compare a period aggregate with the provider usage report and fail if variance exceeds the documented tolerance.
- **failure_modes**: Retries are invisible, so an outage doubles spend without triggering a feature budget. A tenant cannot be charged accurately because cached and generated tokens were conflated.
- **severity**: important
- **applies_if**: ml-service
- **sources**: https://platform.openai.com/docs/api-reference/usage, https://platform.openai.com/docs/guides/production-best-practices

### budget-alerts
- **definition**: LLM budget alerts turn spend and capacity limits into warning and hard-stop controls with an accountable responder. Budgets cover money, tokens, request volume, retries, and queue depth over explicit windows and define downgrade, admission, or blocking behavior at the hard limit.
- **implementation**:
  - Define per-service, tenant, feature, and environment budgets with period, warning threshold, hard threshold, owner, escalation route, and reset behavior.
  - Drive alerts from reconciled cost/token metrics and capacity signals; use deduplication, hysteresis, and multi-window thresholds to avoid alert storms.
  - At hard stop, reject or queue new work before provider invocation, or select a documented lower-cost route without weakening safety or residency policy.
  - Exercise alert delivery and record acknowledgements, override authority, expiry, and all budget changes.
- **probe**: Parse budget and alert configuration; replay usage across warning and hard-stop thresholds. Assert an owner notification at warning, request blocking/downgrade before upstream invocation at hard stop, correct `Retry-After`/typed error, and alert recovery after the window resets.
- **failure_modes**: A runaway agent loop exhausts the monthly provider budget overnight. An alert fires but no hard stop exists, so traffic continues until the provider throttles the service.
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://sre.google/sre-book/monitoring-distributed-systems/, https://platform.openai.com/docs/guides/rate-limits

### latency-slo
- **definition**: An LLM latency SLO defines user-journey-specific budgets for queueing, retrieval, time to first token (TTFT), completion, fallback, and end-to-end response time. Percentiles and deadlines are explicit for each model tier and streaming mode, so eventual success cannot conceal unacceptable waiting time.
- **implementation**:
  - Configure SLI dimensions for journey, tenant/cohort where appropriate, model tier, route, stream mode, and status; define target percentile and measurement window.
  - Propagate one deadline through queue, retrieval, guardrails, provider, fallback, and response streaming; reserve time for cleanup and never start work after it expires.
  - Record TTFT, token inter-arrival, completion, queue, retrieval, and end-to-end histograms, plus timeout and deadline-exceeded counts.
  - Tie error budgets to rollout, routing, concurrency, and fallback decisions; alert on burn rate rather than averages alone.
- **probe**: Parse SLO configuration and metric names; assert every production journey/tier has TTFT and end-to-end percentile targets plus queue/retrieval/deadline budgets. Run timed streaming and non-streaming fixtures and verify recorded timestamps and that injected delay causes the configured deadline/error-budget failure.
- **merges_into**: slo-framework
- **failure_modes**:
  - A support chatbot's p99 completion hits 45s during provider congestion; users abandon chats and agents re-ask, doubling load and cost.
  - A fallback model is 4x slower than primary; without per-tier budgets the fallback path technically 'works' while the user journey times out.
- **severity**: critical
- **applies_if**: all
- **sources**: https://sre.google/sre-book/service-level-objectives/, https://platform.openai.com/docs/guides/latency-optimization

### streaming-backpressure
- **definition**: Streaming backpressure bounds the work and memory used while delivering incremental model output and promptly reacts to a slow or disconnected consumer. Cancellation must propagate upstream, buffers must be bounded, and a non-streaming fallback must be explicit for clients or paths that cannot stream.
- **implementation**:
  - Use bounded queues with a defined overflow policy and apply transport flow control rather than accumulating unbounded tokens in application memory.
  - Propagate client disconnect/cancel to the provider request, worker, retrieval stream, and tool execution; close all resources in a `finally` path.
  - Set idle, total, and inter-token timeouts and expose stream cancellation, buffer depth, and upstream abort metrics.
  - Negotiate stream capability and return a documented buffered response or typed retryable error when streaming is unavailable.
- **probe**: Open a streaming request, consume partial output, then disconnect; assert the upstream call receives cancellation, worker/buffer counts return to baseline, no orphan tool continues, and a non-streaming request remains usable. Hold a client below producer speed and assert queue memory stays under its configured bound.
- **failure_modes**: Mobile clients disappear while provider generation continues and consumes all concurrency. A slow client causes unbounded token buffering and an out-of-memory restart.
- **severity**: important
- **applies_if**: web-api
- **sources**: https://platform.openai.com/docs/guides/streaming-responses, https://platform.openai.com/docs/guides/latency-optimization

### response-cache-safety
- **definition**: A response cache stores only deterministic or explicitly safe results and scopes each entry to the identity, policy, and knowledge state that produced it. Keys and invalidation prevent cross-tenant disclosure and stale answers after prompts, models, policies, or source documents change.
- **implementation**:
  - Canonicalize input and include tenant/auth scope, feature, prompt revision, model/provider, safety policy, locale, tool permissions, and knowledge/index revision in the key.
  - Cache only approved operations; exclude personalized, sensitive, nondeterministic, tool-mutating, or policy-dependent responses unless their safety contract is explicit.
  - Use bounded TTL, size limits, encryption/access controls where data is sensitive, and invalidation on prompt/model/policy/index revision changes.
  - Track hit/miss, stale, eviction, and cross-scope-denial metrics without logging raw prompts or responses.
- **probe**: Inspect key construction and run identical-input fixtures across two tenants, auth scopes, prompt/model revisions, safety policies, and knowledge revisions. Assert isolated results, misses after each revision, no cache write for side-effecting/sensitive cases, and bounded TTL expiry.
- **failure_modes**: A shared cache returns one user's private answer to another user. A policy or source update leaves old unsafe guidance available until an unbounded cache expires.
- **severity**: important
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/prompt-caching, https://owasp.org/www-project-top-10-for-large-language-model-applications/

### request-deduplication
- **definition**: Request deduplication coalesces concurrent, identical, side-effect-free LLM requests under a caller-provided idempotency key and returns one shared result. It is distinct from caching and must bypass coalescing for external side effects unless the action has its own idempotent contract.
- **implementation**:
  - Require a key scoped to tenant, operation, and request fingerprint; reject reuse when the normalized payload, authorization scope, or prompt/model revision differs.
  - Implement a distributed single-flight record with `in_flight`, `succeeded`, and bounded `failed` states, lease expiry, and atomic ownership acquisition.
  - Return the owner result to waiters, bound waiter time and fan-out, and recover abandoned records after lease expiry without allowing concurrent duplicate owners.
  - Mark tool/action requests as non-deduplicable by default and use explicit downstream idempotency keys for approved side effects.
- **probe**: Fire N concurrent requests with the same key and identical fingerprint against a provider stub; assert exactly one provider call and identical shared result. Repeat with mismatched payload, tenant, prompt revision, and a side-effecting tool; assert conflict or bypass and verify lease recovery.
- **failure_modes**: A client timeout and automatic retry generate two expensive completions. A double-click executes an external action twice because an LLM request was treated as harmless.
- **merges_into**: idempotency-keys
- **severity**: important
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9110, https://platform.openai.com/docs/guides/production-best-practices

### input-guardrails
- **definition**: Input guardrails validate user and imported content before model or tool execution, covering size/type, prohibited content, prompt injection, and trust boundaries. Untrusted user, retrieval, and tool text is represented as data rather than instructions and cannot silently override trusted policy.
- **implementation**:
  - Enforce byte/token/attachment limits, MIME and encoding checks, malware scanning where relevant, and normalization before classification.
  - Run versioned moderation, injection, and secret/PII detectors with explicit allow, sanitize, quarantine, and block outcomes.
  - Delimit and label untrusted content, use separate message/tool fields, and remove instruction-like metadata from retrieved documents where policy requires.
  - Record guardrail version, decision, and reason code; never retain prohibited raw input by default and provide a safe user-facing explanation.
- **probe**: Run a versioned malicious-input corpus containing direct/indirect injection, oversized content, obfuscation, secrets, and disallowed material through the pre-call guardrail. Assert prohibited cases cause block/sanitize/quarantine with no provider or tool call, while allowed fixtures preserve required content and audit reason codes.
- **failure_modes**: A retrieved document tells the model to reveal system secrets and the instruction is obeyed. An oversized multipart input bypasses text checks and exhausts preprocessing memory.
- **severity**: critical
- **applies_if**: all
- **sources**: https://owasp.org/www-project-top-10-for-large-language-model-applications/, https://platform.openai.com/docs/guides/safety-best-practices

### output-guardrails
- **definition**: Output guardrails validate every model completion before it is returned to a user or used to invoke a tool. They enforce the required schema, safety and moderation policy, citation contract, and allowed action set, with bounded repair and explicit refusal/partial-result handling.
- **implementation**:
  - Parse strict structured output where supported and validate types, bounds, enums, citations, and business invariants server-side.
  - Moderate and classify generated text and tool calls; compare requested action and arguments against the server-side allowlist and caller authorization.
  - Permit a small configured number of repair attempts with the same safety checks; never execute a repaired output without revalidation.
  - Return typed safe errors/refusals and quarantine rejected output for privacy-safe review; include validator version in telemetry.
- **probe**: Feed valid, malformed, unsafe, refusal, truncated, citation-invalid, and unauthorized-tool fixtures to the post-call validator. Assert valid results pass, unsafe or unauthorized results are rejected before downstream execution, repair attempts never exceed the configured count, and unrecoverable outputs produce a typed safe response.
- **failure_modes**: Malformed JSON reaches a workflow and triggers a partial destructive action. A plausible completion contains unsafe instructions that are returned without moderation.
- **severity**: critical
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/structured-outputs, https://platform.openai.com/docs/guides/moderation

### jailbreak-red-team
- **definition**: A jailbreak red-team program continuously challenges model and application defenses with direct and indirect injection, tool abuse, data exfiltration, multilingual obfuscation, and refusal bypass. It tests the full system—including retrieval, orchestration, authorization, and output handling—not just a text classifier.
- **implementation**:
  - Maintain a versioned corpus with attack provenance, target capability, expected safe outcome, severity, and regression owner.
  - Schedule fresh internal and independent red-team exercises after model, prompt, tool, retrieval, or policy changes; include adaptive multi-turn attacks.
  - Isolate test credentials and tools, cap blast radius, and log attempts without storing unnecessary sensitive payloads.
  - Triage findings by exploitability and impact, add minimized reproductions to CI, and require remediation evidence and retest sign-off.
- **probe**: **Evidence assessor inspects** the current attack corpus, last scheduled exercise, coverage across direct/indirect injection, tools, exfiltration, multilingual and multi-turn cases, isolated test setup, severity triage, regression additions, and remediation/retest records. Confirm a failed high-severity attack blocks release or has an approved risk decision.
- **failure_modes**: A new obfuscation bypasses a static keyword filter. An indirect prompt in a connected document persuades a privileged tool to exfiltrate data.
- **severity**: critical
- **applies_if**: all
- **sources**: https://owasp.org/www-project-top-10-for-large-language-model-applications/, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### pii-redaction
- **definition**: PII redaction detects and removes or minimizes personal data, credentials, and secrets before they are persisted, sent to a provider, placed in an evaluation corpus, or emitted into telemetry. The policy preserves only the minimum context needed for the task and makes irreversible versus reversible tokenization explicit.
- **implementation**:
  - Apply layered detectors (structured field rules, secret scanners, DLP/PII recognizers, and custom entity patterns) at ingress, retrieval assembly, completion, and every telemetry/export sink.
  - Replace values with typed stable placeholders or irreversible masks; maintain a separately protected vault only when re-identification is necessary and authorized.
  - Test multilingual, formatted, encoded, nested, and near-miss sensitive values, and fail closed on detector errors for high-risk fields.
  - Version rules/models, measure false positives/negatives, restrict raw payload retention, and document provider data-use settings.
- **probe**: Run a DLP/PII fixture corpus containing names, contacts, identifiers, credentials, secrets, and obfuscated forms through ingress, prompt assembly, completion, logs, traces, datasets, and exports. Assert exact sensitive values are absent before storage or outbound provider requests, placeholders preserve required structure, and detector failure blocks high-risk transfer.
- **failure_modes**: A debug trace stores an API key and becomes an exfiltration path. A training/evaluation corpus retains a user's health information without consent.
- **merges_into**: telemetry-pii-redaction
- **severity**: critical
- **applies_if**: all
- **sources**: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf, https://microsoft.github.io/presidio/

### llm-tracing
- **definition**: LLM tracing emits correlated, privacy-safe spans for orchestration, retrieval, model calls, tools, guardrails, and streaming. Spans capture enough metadata to debug behavior and attribute cost—provider/model, prompt revision, tokens, latency, retries, status, and redacted payload references—without making raw model I/O broadly available.
- **implementation**:
  - Propagate a trace/correlation ID through request, queue, retrieval, provider, fallback, tool, guardrail, and response spans; preserve parent-child timing.
  - Record provider/model, route, prompt/policy revisions, token usage, TTFT/completion, retry/fallback counts, status/error class, and cache outcome as bounded attributes.
  - Store redacted or hashed prompt/completion references separately from span metadata; apply sampling, retention, encryption, and access policy.
  - Define dashboards and alerts for latency, errors, fallback, token/cost, guardrail, retrieval, and tool outcomes; validate semantic conventions across providers.
- **probe**: Send a canary request with retrieval, guardrail, and a stub tool; query its trace by correlation ID and assert all required parent/child spans and fields exist, timing is coherent, provider/model and prompt revision are present, and raw payload policy is honored. Verify an error trace retains the failure class without sensitive content.
- **failure_modes**: Operators see a provider timeout but cannot tell whether retrieval or queueing consumed the deadline. Raw prompts in a shared trace backend expose secrets to unrelated support staff.
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://opentelemetry.io/docs/concepts/signals/traces/, https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md

### sensitive-trace-access
- **definition**: Sensitive trace access is the governance boundary around any retained raw prompt, completion, attachment, or tool payload. Raw content is encrypted and least-privilege, dashboards are redacted by default, retention and deletion are enforceable, and every exceptional access is audited.
- **implementation**:
  - Keep raw payloads in a separate encrypted store with tenant isolation, KMS-managed keys, rotation, and short configurable retention; store only references in ordinary spans.
  - Enforce role- and attribute-based access for support, engineering, privacy, and incident roles; require justification and time-bound elevation for raw access.
  - Make dashboards and exports show redacted content by default, disable bulk download where possible, and apply field-level masking.
  - Implement deletion/DSAR workflows that remove payloads and derived indexes, verify completion, and retain only an audit record without the content.
- **probe**: Parse telemetry ACL, encryption, retention, export, and deletion configuration; run access tests for unauthorized, ordinary operator, and approved incident roles. Assert unauthorized roles cannot retrieve raw content, approved access is audited with justification, retention deletes fixtures, and deletion removes both primary and indexed copies.
- **failure_modes**: A broad observability role lets an insider browse customer conversations. A deletion request removes the primary payload but leaves a searchable trace index.
- **merges_into**: telemetry-pii-redaction
- **severity**: critical
- **applies_if**: all
- **sources**: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf, https://opentelemetry.io/docs/specs/semconv/

### feedback-capture
- **definition**: Feedback capture records consented user ratings, corrections, overrides, and reviewer labels together with the exact behavior context needed to analyze it. It supports quality learning while resisting duplicate, abusive, or privacy-invasive submissions.
- **implementation**:
  - Store stable feedback ID, trace ID, prompt/model/provider revision, feature/cohort, dataset revision, label type/value, consent state, actor role, and timestamp.
  - Offer positive, negative, correction, and opt-out paths with clear purpose and withdrawal/deletion handling; do not infer consent from mere use.
  - Rate-limit and deduplicate feedback, separate user-submitted text from trusted labels, and moderate or quarantine free-form comments.
  - Join adjudicated feedback to evaluation datasets through a reviewed transformation, retaining provenance and annotator agreement.
- **probe**: Submit positive, negative, correction, reviewer, duplicate, and opt-out fixtures; assert required join keys and consent state, rate limits and deduplication, deletion behavior, and exclusion of opted-out content from training/evaluation exports.
- **failure_modes**: Quality drift is invisible because thumbs-down events cannot be joined to a model revision. A bot floods the evaluation set with fabricated positive ratings.
- **severity**: important
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/evals, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### rag-index-freshness
- **definition**: RAG index freshness is the measured and access-aware delay between source changes and their availability—or removal—in retrieval. It covers ingestion watermarks, failed jobs, source-to-index lag, ACL propagation, and tombstones, not merely the timestamp of the last successful batch.
- **implementation**:
  - Version ingestion, embedding, indexing, ACL, and deletion pipelines; persist source revision, event ID, watermark, job status, and retry/dead-letter state.
  - Expose freshness lag by source/type/tenant and separate new-content, update, ACL, and deletion SLOs.
  - Propagate source permissions and tombstones atomically or fail closed when ACL/deletion state is unknown; prevent retrieval from stale unauthorized shards.
  - Run canary documents through update and delete flows and alert on watermark stalls, failed jobs, and lag-budget burn.
- **probe**: Update and delete a canary source record, then compare source `updated_at`/deletion manifest with index watermark, retrievability, ACL state, and tombstone status. Assert each reaches the configured SLO, failed jobs/dead letters alert, and deleted or unauthorized content is not retrievable during and after propagation.
- **failure_modes**: A policy document remains retrievable after deletion. A silent ingestion failure causes answers to use stale pricing or procedures for days.
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### rag-retrieval-evaluation
- **definition**: RAG retrieval evaluation measures whether the retriever returns the right evidence, in enough rank positions and with acceptable noise, for labeled query cohorts. It evaluates chunking, overlap, metadata filters, embedding revision, top-k, reranking, and context packing together with latency and token impact.
- **implementation**:
  - Maintain a labeled query/document set with relevant source spans, document types, languages, ACL cases, and long-tail cohorts.
  - Version chunker, embedding model, index, filters, top-k, reranker, and context-packing configuration; compare candidate and baseline using recall@k, precision/nDCG, and latency/token metrics.
  - Test permission filters and deletion/tombstone behavior as retrieval correctness constraints, not just separate security tests.
  - Set per-cohort thresholds and inspect false negatives and noisy false positives before promotion.
- **probe**: Run the retrieval evaluator against the pinned corpus and candidate configuration; assert checked-in recall@k/nDCG and latency/token thresholds for each required cohort and document type. Include ACL and deleted-document fixtures and fail if inaccessible or irrelevant chunks are returned.
- **failure_modes**: A chunking change omits the sentence needed to answer a policy question. Increasing top-k adds noisy context, cost, and hallucinations while aggregate recall appears better.
- **severity**: important
- **applies_if**: data-pipeline
- **sources**: https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview, https://platform.openai.com/docs/guides/retrieval

### rag-citation-grounding
- **definition**: Citation grounding requires each material claim to point to a retrievable source span and requires the system to abstain, qualify, or ask for clarification when evidence is insufficient. Citation IDs, spans, and claim support are validated rather than trusting fluent model-generated links.
- **implementation**:
  - Give retrieved chunks stable source/document/span IDs and pass only those IDs or structured references into the generation contract.
  - Parse citations and resolve every ID to the actual retrieved chunk, then run claim-level entailment/support checks with a documented threshold and unsupported-claim policy.
  - Distinguish direct quote, paraphrase, synthesis, and unsupported claims; expose source links and access checks to the user.
  - Return a typed abstention/clarification when retrieval is empty, stale, unauthorized, or below grounding confidence; log grounding metrics by cohort.
- **probe**: Parse every returned citation against retrieved chunk IDs and verify spans belong to the request's authorized context. Run supported, contradicted, uncited, and insufficient-evidence fixtures; assert claim-level thresholds, unresolved-citation rejection, and abstention/clarification for unsupported answers.
- **failure_modes**: A citation points to a real document but not the span supporting the claim. The model confidently fills a retrieval gap with invented policy details.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### fine-tune-data-lineage
- **definition**: Fine-tune data lineage is the immutable record connecting a training run to its data, preprocessing code, base model, hyperparameters, safety filters, licensing/consent, hashes, and removals. It makes a resulting model reproducible, auditable, and actionable when data must be corrected or deleted.
- **implementation**:
  - Create a run manifest with dataset IDs/revisions and content hashes, preprocessing code/image digest, base model revision, hyperparameters, random seeds, tokenizer, and safety-filter version.
  - Record license, consent, provenance, exclusions, PII handling, retention, and removal/tombstone events for each dataset component.
  - Store manifests and artifacts immutably with owner, environment, approval, and model output digest; link the run to evaluation and promotion records.
  - Block training when required lineage fields or hash verification are missing and support rebuilding without excluded data.
- **probe**: Parse each fine-tune run manifest and verify immutable hashes and nonempty data, preprocessing, base model, parameters, tokenizer, license, consent, safety-filter, owner, and exclusion/removal fields. Alter a referenced input after manifest creation and confirm hash verification rejects the run.
- **failure_modes**: A model behavior cannot be reproduced because the preprocessing version was not recorded. A legally removed example remains in a model-training source with no way to identify affected runs.
- **severity**: critical
- **applies_if**: data-pipeline
- **sources**: https://platform.openai.com/docs/guides/fine-tuning, https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

### fine-tune-eval-promotion
- **definition**: Fine-tune promotion is a gated release process that compares a candidate against the base model on holdout task and safety evaluations before limited canary exposure. Approval, observed canary health, and a tested rollback reference are required before broad traffic.
- **implementation**:
  - Keep a protected holdout set distinct from fine-tuning data and evaluate task quality, safety, memorization/privacy, schema, latency, and cost against the base model.
  - Package candidate lineage, evaluation report, threshold decisions, approver, model digest, and rollback target in the promotion artifact.
  - Canary by tenant/cohort or traffic percentage with automatic abort on safety, quality, latency, or error-budget regressions.
  - Test rollback to the base/previous model and verify prompt/tool compatibility before enabling full traffic.
- **probe**: Parse promotion workflow and reject a candidate lacking holdout evaluation, baseline comparison, safety report, canary result, approver, and rollback reference. In a staging harness inject a canary regression and assert rollout aborts and the previous model is restored.
- **failure_modes**: Fine-tuning improves a benchmark but memorizes customer text. A candidate with new unsafe behavior replaces the safer base because canary and rollback were never exercised.
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://platform.openai.com/docs/guides/fine-tuning, https://platform.openai.com/docs/guides/evals

### expensive-endpoint-rate-limit
- **definition**: Expensive-endpoint rate limiting applies authenticated, weighted admission controls to LLM operations whose token, tool, or latency cost can exhaust shared capacity. It combines per-user and per-tenant quotas, concurrency caps, burst handling, and abuse signals while ensuring rejected work never reaches the provider.
- **implementation**:
  - Define token-weighted request units, per-user/tenant rolling quotas, concurrency limits, burst capacity, and `Retry-After` behavior in versioned policy.
  - Enforce identity and tenant authorization before admission; reserve budget atomically and release/settle it using actual provider usage.
  - Use distributed counters/leases with bounded TTL, fairness or weighted scheduling, and separate pools for high-priority workloads to prevent noisy neighbors.
  - Detect automation, credential sharing, repeated failures, and prompt/token abuse; challenge, throttle, or block with an appeal/owner path.
- **probe**: With authenticated clients, exceed each per-user, per-tenant, weighted-token, burst, and concurrency limit; assert `429` plus valid `Retry-After`, bounded in-flight count, fair behavior for another tenant, and no upstream call after hard rejection. Confirm abuse fixtures trigger the documented friction without bypassing normal authorized traffic.
- **merges_into**: quota-policy
- **failure_modes**:
  - A scraped API key runs an agentic loop overnight on an expensive model; the team discovers a five-figure invoice on Monday.
  - One enterprise tenant's batch job saturates the shared concurrency pool; every other tenant gets 429s during business hours.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://www.rfc-editor.org/rfc/rfc9331, https://platform.openai.com/docs/guides/rate-limits

### tool-action-authorization
- **definition**: Tool-action authorization is a server-enforced policy that limits what model-invoked tools may do for a specific caller, context, and workflow. The server validates arguments and authorization independently of model output and requires confirmation or stronger controls for destructive or externally visible actions.
- **implementation**:
  - Maintain an allowlisted tool registry with owner, purpose, scopes, argument schema, side-effect class, resource constraints, and timeout.
  - Validate every argument against schema, tenant/resource ownership, policy constraints, and current authorization at execution time; do not trust model-provided identity or permission claims.
  - Use least-privilege service credentials, read-only defaults, dry-run/preview modes, approval gates for destructive actions, and idempotency keys for approved mutations.
  - Log decision, caller, tool/version, normalized arguments or redacted hash, approver, result, and trace ID; alert on denied or anomalous calls.
- **probe**: Inspect the tool registry, scopes, schemas, and authorization middleware. Run fixtures for allowed read, cross-tenant read, malformed argument, destructive action, expired approval, and prompt-injected tool request; assert server-side denial/confirmation before execution and audit records for each decision.
- **failure_modes**: Prompt injection turns a summarizer into a credential-rotation tool. A model changes another tenant's record because the tool trusted an ID in its arguments.
- **severity**: critical
- **applies_if**: all
- **sources**: https://owasp.org/www-project-top-10-for-large-language-model-applications/, https://platform.openai.com/docs/guides/safety-best-practices

### model-deprecation-inventory
- **definition**: A model deprecation inventory is the maintained production catalog of deployed model/provider versions and their lifecycle obligations. It names an owner, capability/cost/latency baselines, retirement status/date, compatible replacement, and migration/evaluation plan for every production route.
- **implementation**:
  - Record model/provider ID, deployment environments, prompt/tool compatibility, context and residency constraints, owner, purchase/pricing assumptions, and baseline metrics.
  - Poll provider deprecation notices or require an owner review cadence; store announced sunset date, risk, replacement, and support end date.
  - Link replacement evaluation, shadow/canary results, approval, rollout schedule, and rollback target; alert before migration deadlines.
  - Reject new deployments of retired or unowned models and surface inventory drift against runtime telemetry.
- **probe**: Parse the inventory and compare it with deployed model IDs from configuration/telemetry; fail when any production model lacks owner, provider lifecycle status/date, compatible replacement, baseline, and migration-evaluation reference. Insert a near-sunset fixture and verify the alert/escalation path.
- **failure_modes**: A provider retires the only model overnight and the team performs an untested emergency migration. The replacement changes context or tool behavior and causes silent quality and cost regressions.
- **severity**: critical
- **applies_if**: ml-service
- **sources**: https://platform.openai.com/docs/deprecations, https://platform.openai.com/docs/guides/production-best-practices

### structured-output-contract
- **definition**: A structured-output contract specifies the typed schema, semantic invariants, refusal/partial-result states, and repair limits for a model response consumed by software. Validation is performed server-side before downstream parsing or action, even when a provider offers schema-constrained generation.
- **implementation**:
  - Version JSON Schema or equivalent with required fields, types, enums, bounds, additional-property policy, and compatibility rules; include schema ID in the prompt and telemetry.
  - Prefer provider strict structured outputs, then parse and validate again locally, including business authorization and citation invariants.
  - Allow at most a configured number of bounded repair attempts using a minimal error description; never loop indefinitely or execute unvalidated repaired output.
  - Model refusal, truncation, timeout, and partial results as explicit typed variants with safe user/downstream handling.
- **probe**: Send valid, malformed, refusal, truncated, extra-property, wrong-type, and semantically invalid fixtures. Assert schema validation and invariant checks, at most the configured repair count, no downstream action before success, and a safe typed error or explicit partial/refusal state when unrecoverable.
- **failure_modes**: A truncated JSON response is parsed as a false success and triggers an incomplete workflow. A schema-compatible but unauthorized resource ID causes a cross-tenant action without semantic validation.
- **severity**: important
- **applies_if**: all
- **sources**: https://platform.openai.com/docs/guides/structured-outputs, https://platform.openai.com/docs/guides/safety-best-practices
