# Security glossary

### oidc-discovery-issuer
- **definition**: OIDC discovery supplies provider metadata, but the application must trust only configured providers and exact HTTPS endpoints. The issuer used to validate tokens must match the configured issuer byte-for-byte; request data must never choose verification endpoints.
- **implementation**:
  - Keep provider issuer URLs in reviewed server-side configuration, not tenant-controlled request fields.
  - Fetch and cache discovery metadata over HTTPS, validating the returned `issuer`, authorization, token, and JWKS endpoints against the configured origin and allowlist.
  - Reject HTTP, private-network, unexpected-host, and redirect-derived endpoints; refresh metadata with bounded timeouts.
  - Bind each provider to its own JWKS cache and token-validation policy.
- **probe**: Parse provider configuration and discovery responses; assert exact issuer equality, HTTPS for every endpoint, approved hosts, and no endpoint derived from request parameters. Send a request with a malicious provider URL and assert it is rejected before any outbound fetch.
- **failure_modes**: Prevents issuer mix-up accepting a token from the wrong tenant; prevents a malicious discovery document from redirecting JWKS fetches to attacker infrastructure; prevents SSRF through provider configuration.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://openid.net/specs/openid-connect-discovery-1_0.html, https://www.rfc-editor.org/rfc/rfc9700

### authorization-code-pkce
- **definition**: Authorization Code with PKCE uses a one-time code plus a verifier bound to the initiating client, with S256 as the challenge method. Public and browser clients must not use implicit or resource-owner-password grants because those expose tokens or credentials to the front channel.
- **implementation**:
  - Generate a cryptographically random verifier per login and send only its S256 challenge to the authorization server.
  - Exchange the code server-side or in the public client with the verifier over HTTPS, enforcing one-time code use and redirect matching.
  - Configure providers to allow `response_type=code` and `code_challenge_method=S256` only for applicable clients.
  - Keep access tokens out of redirect fragments, URLs, referrers, and browser history.
- **probe**: Capture an authorization request in an integration test and assert `response_type=code`, a unique `code_challenge`, and `code_challenge_method=S256`. Assert the callback URL contains no `access_token`, and reject configurations enabling implicit or password grants.
- **failure_modes**: Prevents an intercepted authorization code from being redeemed without the verifier; prevents access tokens leaking through browser history or referrers; prevents client applications collecting user passwords.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://www.rfc-editor.org/rfc/rfc7636, https://www.rfc-editor.org/rfc/rfc8252

### state-nonce-redirect-binding
- **definition**: Each login transaction carries high-entropy, single-use `state` and `nonce` values bound to the initiating session and redirect. Callback handling must permit only exact pre-registered redirect URIs and reject values from another transaction.
- **implementation**:
  - Generate and store state and nonce server-side or in an integrity-protected, short-lived transaction record.
  - Consume both values atomically on callback and enforce expiry, session binding, and provider binding.
  - Use an exact redirect URI set; reject prefixes, wildcards, user-controlled hosts, and scheme changes.
  - Clear transaction state after success or failure and avoid placing sensitive values in logs.
- **probe**: Run two concurrent logins, swap their state, nonce, and redirect URI in callbacks, and assert each altered callback returns 400/401 with no session. Parse redirect configuration and assert only exact allowlist matches are accepted.
- **failure_modes**: Prevents login CSRF that signs a victim into an attacker account; prevents authorization-response swapping between browser tabs; prevents tokens leaking through an open redirect.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://openid.net/specs/openid-connect-core-1_0.html, https://www.rfc-editor.org/rfc/rfc9700

### token-validation
- **definition**: A JWT is accepted only after signature verification against the intended issuer's trusted JWKS and an explicit algorithm allowlist. Required temporal and identity claims (`iss`, `aud`, `exp`, `nbf`, `iat`, and conditional `azp`) must satisfy the service's policy with bounded clock skew.
- **implementation**:
  - Pin the expected issuer, audience, and allowed algorithms in server-side configuration; never infer them from token headers.
  - Resolve keys only from the configured issuer's JWKS, cache them with safe refresh behavior, and reject unknown key IDs when refresh fails.
  - Validate signature before trusting claims, then enforce expiry, not-before, issued-at, issuer, audience, and authorized-party rules.
  - Bound token size, clock skew, and JWKS fetch time; fail closed on verifier or metadata errors.
- **probe**: Feed the verifier fixtures with invalid signatures, `alg=none`, unapproved algorithms, wrong `iss`/`aud`/`azp`, expired `exp`, and future `nbf`; assert every fixture returns 401. Exercise unknown `kid` and JWKS outage cases and assert no token is accepted.
- **failure_modes**: Prevents algorithm-confusion and forged-token acceptance; prevents expired or not-yet-valid tokens from authenticating; prevents a token for one issuer or service crossing a trust boundary.
- **severity**: critical
- **applies_if**: all
- **sources**: https://openid.net/specs/openid-connect-core-1_0.html, https://www.rfc-editor.org/rfc/rfc8725

### access-token-audience
- **definition**: An API bearer token is valid for that API only when its issuer, audience, and scopes match the API's contract. OIDC ID tokens describe authentication to a client and must never substitute for an access token at a resource server.
- **implementation**:
  - Assign a stable, exact audience to each API and document required scopes per route.
  - Validate audience and scope server-side on every protected request, including service-to-service calls.
  - Keep separate code paths and claim policies for ID tokens and access tokens.
  - Return 401 for invalid token type or audience and 403 for an otherwise valid token missing required scope, without revealing policy details.
- **probe**: Send a correctly signed ID token and a correctly signed access token with the wrong `aud` to every protected API and assert 401/403. Assert success only for a correctly audienced access token carrying the required scope.
- **failure_modes**: Prevents a token issued to one service being replayed against another; prevents ID-token claims from being mistaken for API authorization; prevents scope-less tokens reaching privileged routes.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://openid.net/specs/openid-connect-core-1_0.html, https://www.rfc-editor.org/rfc/rfc9700

### browser-token-storage
- **definition**: Browser applications should keep bearer credentials in a server-side BFF session or, where unavoidable, short-lived memory. Tokens must not be persisted in Web Storage, URLs, DOM content, or cookies readable by JavaScript.
- **implementation**:
  - Prefer an HttpOnly, Secure session cookie whose server-side record holds access and refresh tokens.
  - If a SPA must hold an access token, keep it in memory, minimize its lifetime, and use refresh-token rotation through a protected flow.
  - Disable token-bearing query parameters and fragments after callbacks using a server-side redirect.
  - Enforce CSP and XSS defenses because in-memory tokens remain exposed to active script.
- **probe**: Scan source and built bundles for `localStorage`/`sessionStorage` assignments containing token names. Run a browser login test and assert no bearer token exists in storage, DOM, URL, or a non-HttpOnly cookie.
- **failure_modes**: Prevents XSS or extensions stealing persistent refresh tokens; prevents browser history and referrers retaining access tokens; limits credential exposure on shared workstations.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html

### native-token-storage
- **definition**: Mobile long-lived credentials belong in the platform-protected keystore or keychain, not ordinary preferences or databases. Authorization callbacks must use claimed HTTPS universal/app links or loopback redirects that establish an unambiguous application identity.
- **implementation**:
  - Store refresh tokens using Android Keystore-backed storage or iOS Keychain with appropriate device-access constraints.
  - Keep access tokens short-lived and memory-resident where practical; never include secrets in logs, backups, screenshots, or crash reports.
  - Register claimed HTTPS app/universal links or loopback redirect handling and verify the callback state.
  - Remove credentials on logout, account removal, and detected device compromise.
- **probe**: Inspect the mobile package and end-to-end login flow for Keystore/Keychain use, plaintext preferences/database writes, logs, and backup exclusions. Complete login and assert the redirect is claimed and uniquely bound to the app.
- **failure_modes**: Prevents another installed application intercepting a custom-scheme callback; prevents filesystem backups exposing refresh tokens; prevents support/crash artifacts becoming credential stores.
- **severity**: critical
- **applies_if**: mobile
- **sources**: https://www.rfc-editor.org/rfc/rfc8252, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

### refresh-token-rotation
- **definition**: Every refresh-token redemption yields a replacement token and invalidates the predecessor. Reuse detection revokes the entire token family, and logout, password reset, or confirmed compromise invalidates the associated sessions.
- **implementation**:
  - Persist token-family state with an atomic compare-and-swap so concurrent redemptions cannot both succeed.
  - Mark a family compromised on reuse and revoke all descendants before returning `invalid_grant`.
  - Bind refresh records to client/user/session context and enforce absolute and idle lifetimes.
  - Provide revocation hooks for logout, password reset, account disablement, and incident response.
- **probe**: Redeem one refresh token twice, including concurrent requests, and assert the second and all descendants fail with `invalid_grant`. Execute logout and password-reset flows and assert subsequent access-token requests return 401.
- **failure_modes**: Prevents a copied refresh token creating an indefinite session; prevents a race allowing two valid token branches; limits damage after password compromise or logout.
- **severity**: critical
- **applies_if**: all
- **sources**: https://www.rfc-editor.org/rfc/rfc9700, https://www.rfc-editor.org/rfc/rfc7009

### session-cookie-hardening
- **definition**: Session cookies contain only high-entropy opaque identifiers that map to server-side state. They must use `Secure`, `HttpOnly`, appropriate `SameSite`, narrow scope, and preferably the `__Host-` prefix to reduce theft and injection.
- **implementation**:
  - Generate at least 128 bits of unpredictable identifier entropy with a CSPRNG; never encode user data or permissions in the value.
  - Set `Secure; HttpOnly; SameSite=Lax` or `Strict`, `Path=/`, and omit `Domain` for host-only cookies where compatible.
  - Use `__Host-` cookies for HTTPS applications that can meet the prefix requirements.
  - Keep separate cookie names and scopes for unrelated applications and invalidate server-side records on logout.
- **probe**: Run `curl -sS -D -` against login and session-refresh endpoints, parse every session `Set-Cookie`, and assert the required flags, no broad `Domain`, and at least 128 bits of unpredictability across samples.
- **failure_modes**: Prevents script-based token theft; prevents plaintext network capture; prevents subdomain cookie injection and cross-site request leakage.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

### session-lifecycle
- **definition**: A session has explicit transitions and bounded idle and absolute lifetimes, with identifier rotation at authentication and privilege changes. Logout and account disablement invalidate server-side state rather than merely deleting a browser cookie.
- **implementation**:
  - Rotate the session identifier on login, reauthentication, role elevation, and other trust-boundary changes.
  - Store `created_at`, `last_seen_at`, authentication context, and revocation state server-side.
  - Enforce idle and absolute expiry on every request, with bounded clock skew and a reauthentication path.
  - Revoke all relevant sessions on logout-all, password reset, account disablement, or compromise.
- **probe**: Compare pre-auth and post-auth identifiers, perform a role change, wait through configured idle and absolute limits, then use old and expired sessions after logout or disablement; assert each receives 401.
- **failure_modes**: Prevents session fixation after login; prevents abandoned sessions remaining valid forever; prevents disabled users retaining access through an already-issued cookie.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

### csrf-state-changes
- **definition**: Cookie-authenticated state changes require an unpredictable framework CSRF token and a trusted Origin or Referer check. SameSite cookies reduce exposure but are defense in depth, not the sole control.
- **implementation**:
  - Use a framework synchronizer token or signed double-submit token bound to the session and validate it server-side.
  - Require an exact approved Origin; use Referer only as a carefully documented fallback when Origin is absent.
  - Apply the check to every mutating route, including JSON, uploads, logout, and administrative actions.
  - Reject missing, malformed, stale, or cross-session tokens before invoking business logic and avoid state changes on errors.
- **probe**: For each state-changing route, send an untrusted Origin with no token and assert 403 plus no state change. Repeat with a valid token and trusted origin and assert success; test missing/null origins according to documented policy.
- **failure_modes**: Prevents cross-site fund transfers and account changes; prevents attacker-controlled logout or credential updates; prevents relying on browser cookie defaults that vary by client.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html

### deny-by-default-authorization
- **definition**: Every route, RPC, background job, and tool action must authenticate its principal and pass an explicit server-side policy. Unknown actions, missing bindings, and policy-engine errors deny access rather than failing open.
- **implementation**:
  - Maintain a route/action registry requiring an authorization policy binding as part of review and CI.
  - Centralize authentication context and policy evaluation while keeping resource checks close to the protected operation.
  - Return 401 for absent authentication and 403 for denied authorization without performing side effects.
  - Define fail-closed behavior and bounded timeouts for policy-service outages, with emergency access separately audited.
- **probe**: Enumerate the route/action registry, call each without authentication, invoke unknown actions, and inject policy-service errors; assert 401/403, no side effect, and CI failure for any unbound action.
- **failure_modes**: Prevents a newly added endpoint becoming public; prevents policy outages exposing data; prevents hidden jobs or tool actions bypassing HTTP authorization.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/IndexASVS.html

### object-level-authorization
- **definition**: Authorization is evaluated against the object loaded from the database and the caller's tenant, ownership, and action rights. Client-supplied IDs, hidden fields, roles, and bulk arrays are untrusted selectors, not permission decisions.
- **implementation**:
  - Scope queries by tenant or owner before loading the object, then check the requested action server-side.
  - Centralize object-policy helpers and apply them to reads, writes, deletes, exports, downloads, and asynchronous jobs.
  - Use opaque IDs only as defense in depth; do not treat unpredictability as authorization.
  - Ensure bulk operations authorize every object and fail safely on mixed-authority batches.
- **probe**: Create equivalent objects for users or tenants A and B, replay A's read/update/delete/export/download requests with B identifiers and bulk arrays, and assert 403/404 with no partial side effect.
- **failure_modes**: Prevents IDOR data disclosure; prevents cross-tenant updates and deletes; prevents bulk endpoints bypassing checks applied to single-object routes.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

### function-level-authorization
- **definition**: Privileged functions require explicit role and scope checks independent of client navigation or UI visibility. Administrative, debug, export, impersonation, billing, and bulk actions are protected even when invoked directly or asynchronously.
- **implementation**:
  - Maintain a role-by-function policy matrix with default denial and named owners for exceptions.
  - Authorize at the API/RPC/job handler immediately before the sensitive operation, not only in route middleware or the UI.
  - Separate break-glass and impersonation permissions, require reauthentication or step-up controls where appropriate, and audit every use.
  - Hide diagnostics and admin routes from ordinary deployments when they are not needed, without treating hiding as authorization.
- **probe**: Execute a role-by-route negative matrix against admin, debug, export, impersonation, billing, and bulk endpoints with client controls bypassed; assert ordinary roles always receive 403 and create no side effect.
- **failure_modes**: Prevents users calling hidden admin URLs directly; prevents debug endpoints exposing secrets; prevents a UI-only role check from protecting exports or background jobs.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/IndexASVS.html

### least-privilege-scopes
- **definition**: Identities receive only the minimum OAuth scopes, roles, resources, and actions required for their workload. Least privilege applies to users, services, databases, queues, cloud identities, and operators, with grants reviewed and expired deliberately.
- **implementation**:
  - Define permission sets per workload and environment; avoid wildcard principals, actions, and resources.
  - Separate read, write, administrative, production, and break-glass identities.
  - Use short-lived federated credentials and just-in-time elevation for exceptional operations.
  - Review effective IAM, OAuth, database, and queue policies regularly; attach owners and expiry to exceptions.
- **probe**: Parse effective IAM, OAuth, database, and service-account policies for wildcard grants and compare them with an approved permission set. Fail on unowned, unexpired unexplained administrative access and verify a workload cannot read or mutate an unrelated resource.
- **failure_modes**: Limits data exposure after a service token leak; prevents a queue consumer deleting production records; reduces blast radius from compromised operators or dependencies.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final

### sql-injection-prevention
- **definition**: Data queries keep code and values separate through parameter binding or safe ORM APIs, and dynamic identifiers come only from strict allowlists. Database identities additionally limit the consequence of any missed validation by denying DDL and unnecessary data access.
- **implementation**:
  - Use prepared statements or parameterized query builders for every value, including filters and sort parameters.
  - Map user-selected table, column, and sort names through a finite server-side allowlist; never interpolate raw identifiers.
  - Validate types, ranges, lengths, and collection sizes before query construction.
  - Run the application with a restricted database role and separate migrations from runtime credentials.
- **probe**: Send quote, comment, boolean, union, time-delay, and type-confusion payloads through every query input; assert no SQL error, row-set expansion, or timing oracle. Statically inspect query construction and parse database grants for excess privileges.
- **failure_modes**: Prevents account or tenant data exfiltration; prevents attacker-controlled deletes or updates; limits impact when a query bug reaches production.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/IndexASVS.html

### xss-prevention
- **definition**: Output is encoded for its actual context and rendered through safe templating; rich HTML is sanitized under an explicit policy. Unsafe DOM sinks and script evaluation are avoided, with Trusted Types and CSP providing additional containment where supported.
- **implementation**:
  - Use framework auto-escaping for HTML, attribute, URL, JavaScript, and CSS contexts rather than one generic encoder.
  - Sanitize user-authored rich text with a maintained allowlist and safe URL scheme policy.
  - Remove or gate `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval`, and string-to-code APIs; enforce Trusted Types where feasible.
  - Set a restrictive CSP and ensure error, markdown, email, and admin preview paths use the same controls.
- **probe**: Inject script tags, quote payloads, URL schemes, SVG, and event-handler strings into every rendered field; assert encoded/sanitized output and no script execution in a headless browser. Scan source and bundles for unreviewed unsafe sinks.
- **failure_modes**: Prevents stored XSS stealing sessions; prevents reflected XSS in search and error pages; prevents rich-text or SVG uploads executing in privileged admin views.
- **severity**: critical
- **applies_if**: spa
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html

### command-injection-prevention
- **definition**: User input never becomes shell syntax or an uncontrolled executable path. When process execution is unavoidable, the program, arguments, environment, identity, filesystem, and resources are constrained independently.
- **implementation**:
  - Prefer library APIs over shell commands and invoke a fixed executable with an argument array and disabled shell mode.
  - Validate each argument against type, length, option, and path allowlists; prevent option injection with explicit separators where supported.
  - Run the process as a non-root identity in a sandbox with a read-only filesystem, seccomp or equivalent, and CPU/memory/time limits.
  - Capture output safely, bound it, and avoid reflecting command details into logs or responses.
- **probe**: Fuzz every process input with `;id`, `&&id`, `$(id)`, newlines, option-injection, NULs, and argument-boundary payloads; assert no child beyond the fixed executable and no shell expansion. Inspect process-spawn configuration for shell execution and privilege.
- **failure_modes**: Prevents remote code execution through filename or conversion features; prevents option injection deleting or reading files; limits a compromised helper process from escaping its sandbox.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html

### ssrf-egress-controls
- **definition**: Server-side fetchers allow only approved schemes, hosts, ports, and redirect destinations, and validate the resolved address at connection time. Network egress policy provides a second boundary against private, loopback, link-local, metadata, and control-plane destinations.
- **implementation**:
  - Parse URLs with a standards-compliant parser and allowlist `https` hosts and ports rather than filtering strings.
  - Resolve DNS, reject private/link-local/loopback/metadata ranges for every address, and re-check after redirects and connection establishment to resist rebinding.
  - Disable or tightly constrain redirects, alternate schemes, proxy environment variables, and userinfo/encoded-host tricks.
  - Apply firewall or service-mesh egress rules and short connect/read/total deadlines with response-size limits.
- **probe**: Exercise every server-side fetcher with loopback, RFC1918, IPv6-local, link-local/metadata, DNS-rebinding, non-HTTP, and redirect-chain targets; assert application rejection and blocked network egress. Verify approved external hosts still work.
- **failure_modes**: Prevents cloud credential theft from metadata endpoints; prevents access to internal admin panels; prevents DNS rebinding turning an approved hostname into a private service.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html

### path-traversal-prevention
- **definition**: External file identifiers map to server-owned paths, and the canonical resolved path must remain beneath the intended root. Encoded separators, absolute paths, NUL bytes, mixed separators, and unsafe symlinks are rejected before filesystem access.
- **implementation**:
  - Prefer opaque database IDs mapped to stored paths; never concatenate a user path into a filesystem root.
  - Canonicalize after decoding once according to the platform, then enforce a directory-boundary-aware prefix check.
  - Reject absolute paths, traversal segments, NULs, alternate separators, double encoding, and symlinks escaping the root.
  - Use separate identities and mount permissions for upload, processing, and serving paths; keep secrets outside served roots.
- **probe**: Exercise file endpoints with `../`, encoded and double-encoded separators, absolute paths, NUL bytes, mixed separators, and symlink fixtures; assert rejection and verify no file outside the configured root is read or written.
- **failure_modes**: Prevents downloading environment files and application source; prevents overwriting configuration or executable files; prevents symlink-based escape from an upload directory.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Path_Traversal_Cheat_Sheet.html

### vault-kms-secret-delivery
- **definition**: Production secrets are stored in a managed Vault or KMS and delivered at runtime through workload identity and path-level policy. Secret values do not appear in source, images, frontend bundles, static manifests, or long-lived CI artifacts.
- **implementation**:
  - Use workload identity or short-lived federated credentials to fetch secrets at startup or on bounded refresh, never a shared static bootstrap key.
  - Partition secret paths and KMS keys by environment, service, and role; grant read access only to the needed paths.
  - Inject values through an ephemeral runtime mechanism and prevent them from appearing in process arguments, images, manifests, or client bundles.
  - Audit reads and failed reads, cache only as long as required, and define behavior for Vault/KMS outage without logging values.
- **probe**: Scan source, git history, manifests, images, and built bundles with an approved secret scanner. Parse deployment policy for runtime Vault/KMS references, workload identity, and absence of static credential values; inspect one deployed workload for secret access logs.
- **failure_modes**: Prevents repository or image-registry compromise exposing production credentials; prevents CI artifacts leaking shared keys; prevents frontend users receiving server-only secrets.
- **severity**: critical
- **applies_if**: all
- **merges_into**: secrets-management
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final

### secret-rotation-revocation
- **definition**: Every credential and cryptographic key has an owner, purpose, maximum age, and emergency revocation path. Automated rotation changes consumers without source edits, validates the replacement before cutover, and makes the old value unusable within the documented window.
- **implementation**:
  - Maintain secret metadata for owner, consumers, creation, last rotation, maximum age, and revocation procedure.
  - Use dual-key or overlapping validity windows for zero-downtime rotation, then revoke the predecessor after confirmed adoption.
  - Automate rotation for API keys, database credentials, OAuth secrets, certificates, encryption keys, and signing keys with bounded retries and alerts.
  - Exercise emergency revocation in staging and keep a break-glass procedure with audited access.
- **probe**: Parse secret metadata for owner, maximum age, last rotation, and revocation capability. Run a staging rotation, assert the new credential works without source changes, the old credential fails after cutover, and dependent services recover.
- **failure_modes**: Limits the useful lifetime of leaked credentials; prevents certificate or key expiry outages; prevents emergency response depending on an untested manual rotation.
- **severity**: critical
- **applies_if**: all
- **merges_into**: secrets-management
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final

### secret-leak-prevention
- **definition**: Secret detection covers working trees, history, CI output, release artifacts, images, support exports, and registries, with push protection before publication. Detection triggers immediate revocation and replacement because deleting one copy cannot erase retained history or caches.
- **implementation**:
  - Run a high-confidence secret scanner in pre-commit, pull requests, CI, artifact publication, and registry admission.
  - Mask secret-shaped values in CI logs and block uploads containing known credentials or canaries.
  - Scan full git history, container layers, build caches, release bundles, crash dumps, and support exports on a schedule.
  - Treat findings as incidents: identify owner, revoke the credential, issue a replacement, preserve evidence, and record closure.
- **probe**: Run a secret scanner against full repository history, CI artifacts, container layers, release bundles, and support exports. Inject a canary credential into a test change and assert publication is blocked and the canary is revoked after detection.
- **failure_modes**: Prevents deleted secrets remaining exploitable in git history; prevents build logs or support bundles publishing credentials; prevents a detected leak being left active while teams debate cleanup.
- **severity**: critical
- **applies_if**: all
- **merges_into**: secrets-management
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/218/final

### log-redaction
- **definition**: Sensitive fields are removed or irreversibly transformed before data enters logs, traces, metrics, crash reports, or support exports. Redaction must cover authorization headers, cookies, tokens, passwords, keys, raw bodies, and unnecessary PII at every instrumentation boundary.
- **implementation**:
  - Centralize structured logging and telemetry processors that drop sensitive field names and redact nested payloads before serialization.
  - Never log full request/response bodies, credentials, URLs containing secrets, or raw identity documents; use stable non-secret hashes only when correlation is necessary.
  - Apply the same processors to application logs, access logs, tracing attributes, metrics labels, exceptions, queues, and vendor exporters.
  - Restrict telemetry access and retention, and add regression canaries for new middleware and SDKs.
- **probe**: Send canary secrets, tokens, passwords, and PII through success and error paths; export logs, traces, metrics, crash reports, and support data; assert exact and decoded canary values are absent from every sink.
- **failure_modes**: Prevents observability platforms becoming credential stores; prevents traces exposing customer PII to broad engineering audiences; prevents error serialization leaking request bodies to third parties.
- **severity**: critical
- **applies_if**: all
- **merges_into**: telemetry-pii-redaction
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### dependency-vulnerability-gates
- **definition**: Dependency policy covers direct and transitive packages, lockfiles, generated bundles, and licenses, and blocks release when exploitable high or critical findings lack a current approved exception. Resolution must be deterministic so the scanned dependency set is the shipped dependency set.
- **implementation**:
  - Commit lockfiles and pin or constrain direct dependencies; make CI fail on unexpected lockfile drift.
  - Run package-manager audit and an approved SCA scanner for every language ecosystem and build artifact.
  - Define severity, exploitability, reachability, license, remediation SLA, owner, and expiry for exceptions.
  - Rescan images and release artifacts after build, and alert when newly disclosed issues affect retained releases.
- **probe**: Run each package manager's audit and approved SCA/license scanners against every lockfile, generated bundle, and release image. Assert reproducible resolution and fail on unexpired policy violations or missing exception owners.
- **failure_modes**: Prevents a transitive vulnerable package entering production unnoticed; prevents lockfile drift producing an unscanned release; prevents permanent risk being hidden behind an undated exception.
- **severity**: critical
- **applies_if**: monorepo
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/218/final

### container-hardening-scanning
- **definition**: Release containers use minimal digest-pinned bases and run with reduced privileges, while image, IaC, and runtime configuration are scanned before deployment. Hardening reduces both known vulnerability exposure and the blast radius of a compromised process.
- **implementation**:
  - Pin base images by immutable digest and rebuild on a defined vulnerability cadence.
  - Run as a non-root UID with a read-only root filesystem, dropped Linux capabilities, restricted seccomp, and no unnecessary host mounts.
  - Generate images through a reproducible CI builder and scan all layers, packages, IaC, and deployment security context.
  - Set resource limits, disable privilege escalation, and keep debug tools out of production images.
- **probe**: Build the release image and inspect effective user, root filesystem, capabilities, seccomp, mounts, and base digest. Run approved image/IaC CVE scanners and parse deployment `securityContext` for violations; assert policy failures block deployment.
- **failure_modes**: Prevents a known base-image CVE reaching production; limits host or filesystem damage after application compromise; prevents privileged containers turning an app bug into node compromise.
- **severity**: critical
- **applies_if**: all
- **sources**: https://csrc.nist.gov/pubs/sp/800/190/final, https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html

### release-sbom
- **definition**: Each immutable release has a complete machine-readable SPDX or CycloneDX inventory of direct, transitive, bundled, runtime, and container-layer components. The SBOM is retained and cryptographically or immutably tied to the exact artifact digest and build identifier.
- **implementation**:
  - Generate SBOMs in CI after dependency resolution and image assembly, covering application bundles and every container layer.
  - Include package versions, supplier, identifiers, licenses, dependency relationships, artifact digest, source revision, and build ID.
  - Publish the SBOM beside the release with access control and retention matching the artifact lifecycle.
  - Fail publication when generation fails, required components are missing, or the artifact-to-SBOM digest mapping is absent.
- **probe**: Generate the SBOM in CI, parse it for every package and container layer, and assert the release digest and build ID match the immutable artifact. Verify a published artifact cannot be promoted without its corresponding SBOM.
- **failure_modes**: Enables rapid scope analysis after a vulnerable library disclosure; prevents responders searching only direct dependencies while missing bundled code; prevents an SBOM being mistaken for a different release.
- **severity**: important
- **applies_if**: monorepo
- **merges_into**: sbom-provenance
- **sources**: https://www.cisa.gov/topics/cyber-threats-and-advisories/software-bill-materials-sbom, https://cyclonedx.org/specification/

### provenance-signing-verification
- **definition**: Isolated CI records how an artifact and SBOM were built, including source revision, builder identity, inputs, and digest, then signs the outputs. Deployment verifies those claims and rejects unsigned, mismatched, or untrusted artifacts before execution.
- **implementation**:
  - Use ephemeral, isolated builders with protected source checkout and least-privilege signing identity.
  - Emit SLSA provenance and sign artifacts and SBOMs using Sigstore or managed keys with verifiable identity and key rotation.
  - Bind verification policy to expected repository, revision, builder, artifact digest, and environment.
  - Verify signatures and provenance at registry admission or deployment, not only in CI; retain attestations with the release.
- **probe**: Verify a release signature and provenance against expected repository, revision, builder identity, and digest. Submit unsigned, mismatched, expired, and locally rebuilt artifacts to deployment and assert each is rejected.
- **failure_modes**: Prevents a compromised registry serving altered bytes; prevents an unauthorized CI pipeline publishing a trusted-looking release; prevents local rebuilds with unreviewed inputs entering production.
- **severity**: critical
- **applies_if**: monorepo
- **merges_into**: sbom-provenance
- **sources**: https://slsa.dev/spec/v1.0/, https://docs.sigstore.dev/

### tls-baseline
- **definition**: All listeners use authenticated modern TLS with TLS 1.2 or 1.3 and approved cipher suites, while legacy protocols and weak renegotiation are disabled. Certificates are hostname- and chain-validated, renewed automatically, and supplemented by mTLS for sensitive internal identities where warranted.
- **implementation**:
  - Configure TLS 1.2/1.3 minimums and an approved cipher/profile; disable SSLv3, TLS 1.0/1.1, weak suites, and unsafe renegotiation.
  - Validate certificate hostname, chain, key usage, and trust store on clients; never disable verification to fix an incident.
  - Automate issuance, renewal, deployment, and rollback with expiry alerts and staging validation.
  - Use mTLS or workload identity for high-value internal service-to-service paths and rotate client certificates.
- **probe**: Run `openssl s_client` or an approved TLS scanner against every public and internal listener; assert legacy protocols and weak suites fail, hostname/chain validation succeeds, expiry is above threshold, and renewal works in a staging fixture.
- **failure_modes**: Prevents downgrade and interception attacks; prevents invalid certificate acceptance by clients; prevents outages when certificates expire or are misissued.
- **severity**: critical
- **applies_if**: all
- **sources**: https://csrc.nist.gov/pubs/sp/800/52/r2/final, https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html

### content-security-policy
- **definition**: CSP constrains the origins and execution mechanisms a browser may use for scripts, styles, frames, and other content. Nonces or hashes authorize intended inline code, while `unsafe-inline` and `unsafe-eval` remain prohibited unless an explicit, temporary exception is owned and monitored.
- **implementation**:
  - Emit a restrictive per-response CSP with nonces or hashes for required scripts and explicit source allowlists.
  - Remove inline handlers, dynamic evaluation, and broad wildcard sources; use `frame-ancestors`, `base-uri`, and object restrictions as applicable.
  - Roll out with `Content-Security-Policy-Report-Only`, collect violation reports without sensitive payloads, then enforce.
  - Keep policy generation consistent across HTML, error pages, admin tools, and CDN paths.
- **probe**: Parse response headers and assert approved script/style sources, nonce/hash use, no unexplained unsafe directives, and required frame/object restrictions. Run a browser injection fixture and assert execution is blocked and a violation report is recorded.
- **failure_modes**: Limits impact of an XSS bug; prevents compromised third-party content loading arbitrary scripts; exposes unsafe inline regressions during frontend changes.
- **severity**: important
- **applies_if**: spa
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html

### hsts
- **definition**: HSTS tells browsers to use HTTPS for a host for a declared period, preventing first-visit and downgrade exposure. `includeSubDomains` is safe only after every subordinate host is confirmed HTTPS-capable and operationally owned.
- **implementation**:
  - Serve `Strict-Transport-Security` on HTTPS responses with `max-age` of at least 31536000 seconds after staged validation.
  - Redirect HTTP to HTTPS without serving application content or accepting credentials over plaintext.
  - Add `includeSubDomains` only after inventorying subordinate hosts and resolving legacy exceptions.
  - Monitor certificate renewal, redirect behavior, and HSTS policy changes as security-sensitive configuration.
- **probe**: Run `curl -sS -D - https://host/` and assert `Strict-Transport-Security: max-age>=31536000` with the approved subdomain decision. Request HTTP and assert a redirect with no content or cookie issuance before HTTPS.
- **failure_modes**: Prevents first-use network attackers downgrading a browser; prevents cookies and PII sent over HTTP after a bad link; prevents accidental subdomain lockout through an unvalidated blanket policy.
- **severity**: important
- **applies_if**: all
- **sources**: https://www.rfc-editor.org/rfc/rfc6797, https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html

### cors-allowlist
- **definition**: CORS grants browser read access only to exact, reviewed origins and required methods and headers. Credentialed responses must never use wildcard origins, and origin-varying responses must advertise `Vary: Origin` to prevent cache confusion.
- **implementation**:
  - Maintain an exact origin allowlist by environment; compare normalized scheme, host, and port without suffix or substring matching.
  - Allow only required methods and headers, handle preflight consistently, and reject `null` or opaque origins unless explicitly justified.
  - Never emit `Access-Control-Allow-Origin: *` with credentials; return no CORS grant for untrusted origins.
  - Add `Vary: Origin` on responses whose CORS headers vary, and test CDN/proxy cache behavior.
- **probe**: Send trusted, untrusted, `null`, wildcard-like, and credentialed origins to preflight and actual endpoints; assert CORS headers appear only for exact allowlist entries and never combine wildcard origin with credentials.
- **failure_modes**: Prevents hostile sites reading authenticated API responses; prevents origin suffix tricks granting tenant data; prevents shared caches serving a trusted-origin response to an untrusted origin.
- **severity**: critical
- **applies_if**: web-api
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/CORS_OriginHeaderScrutiny_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html

### abuse-rate-limiting
- **definition**: Abuse controls combine endpoint-specific quotas, adaptive throttles, and user friction for login, recovery, enumeration, expensive, and write operations. Limits use trusted signals such as account, tenant, device, token, and network identity rather than relying on one spoofable IP, and return consistent machine-readable backpressure.
- **implementation**:
  - Define separate token-bucket or sliding-window policies for authentication, recovery, enumeration, reads, writes, and expensive operations.
  - Key limits by account and tenant plus validated IP/device/token signals; ignore client-supplied identity headers unless trusted by the edge.
  - Return `429` with bounded `Retry-After`, audit events, and safe generic errors that do not reveal account existence.
  - Add adaptive abuse friction—progressive delays, proof-of-work or CAPTCHA, step-up authentication, or temporary challenge—only after thresholds and with accessible recovery paths.
  - Protect the limiter itself with bounded storage, fail-safe behavior, and dashboards for false positives and bypass attempts.
- **probe**: Send bursts exceeding login, recovery, read, write, and expensive-operation quotas from one and many keys; assert 429, valid `Retry-After`, audit events, and no bypass through spoofed headers. Verify progressive friction activates for credential stuffing while an approved low-volume user remains usable.
- **failure_modes**: Prevents credential stuffing and password-reset flooding; limits scraping and expensive endpoint resource exhaustion; prevents attackers rotating IPs or spoofing headers to evade a single-key limit.
- **severity**: important
- **applies_if**: all
- **merges_into**: quota-policy
- **sources**: https://owasp.org/API-Security/editions/2023/en/0x00-header/, https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html

### security-audit-logging
- **definition**: Security audit logging is an immutable, access-controlled record of security-relevant actions, including authentication, authorization failures, administrative changes, exports, secret/key operations, and security configuration changes. Each event identifies actor, target, outcome, time, and correlation context without storing credentials or unnecessary PII.
- **implementation**:
  - Define a versioned schema with event type, actor/service identity, target, outcome, timestamp, request/correlation ID, source, and reason.
  - Write to an append-only or tamper-evident sink with restricted read access, retention, clock synchronization, and export controls.
  - Emit events at the decision point for both success and failure, including policy denials and break-glass use.
  - Alert on high-risk patterns and test that dropped or unavailable audit sinks fail according to an explicit safety policy without blocking unrelated recovery.
- **probe**: In staging, trigger each authentication, authorization, admin, export, secret, and configuration event; parse the audit sink schema and verify actor, target, outcome, time, and correlation ID. Attempt mutation/deletion with a normal operator identity and assert append-only controls.
- **failure_modes**: Prevents incidents becoming uninvestigable due to missing actor context; provides evidence for unauthorized exports and privilege changes; distinguishes operator actions from attacker activity.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final

### pii-encryption-at-rest
- **definition**: PII is classified, minimized, and encrypted wherever persisted, including databases, files, queues, caches, snapshots, and backups. Envelope encryption uses KMS-managed keys with separation by environment and access role, while application access remains policy-controlled.
- **implementation**:
  - Inventory PII fields and retention requirements; avoid storing fields that are not necessary for the product purpose.
  - Use envelope encryption with KMS-wrapped data-encryption keys and separate keys or key policies for production, nonproduction, and backup domains.
  - Enable encryption for primary stores, replicas, object storage, queues, caches where supported, and all backups and exports.
  - Restrict decrypt permission to the smallest service and operator set; rotate keys and test restore/decrypt procedures.
- **probe**: Parse storage, backup, queue, and cache configuration for KMS key IDs and environment separation; sample a persisted record or object and verify ciphertext at rest. Confirm an unauthorized service identity cannot decrypt and that a restore test succeeds.
- **failure_modes**: Limits disclosure from stolen disks or snapshots; prevents backup buckets becoming plaintext PII archives; prevents broad operator access to decrypt every tenant's data.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/111/final

### pii-in-transit-and-minimization
- **definition**: PII traverses authenticated encrypted channels only, is excluded from URLs and telemetry, and is collected for a documented purpose with bounded retention and deletion. Minimization reduces both exposure probability and the amount that must be protected across systems.
- **implementation**:
  - Require certificate-validated TLS for browser, service, database, queue, and third-party PII hops; reject plaintext fallbacks.
  - Keep PII out of query strings, paths, referrers, headers not required by the protocol, logs, traces, metrics, and analytics events.
  - Define field-level collection, retention, deletion, access, and export policies, including derived and cached copies.
  - Use synthetic or tokenized identifiers for analytics, support, and nonproduction workflows where the real value is unnecessary.
- **probe**: Send a canary PII value through representative requests and telemetry; inspect URLs, proxy/access logs, traces, metrics, queues, and exports and assert no plaintext canary appears. Parse TLS configuration and retention/deletion jobs for each PII store.
- **failure_modes**: Prevents proxies and referrer logs exposing email or identifiers; prevents unnecessary PII surviving indefinitely in analytics; prevents network observers reading service-to-service payloads.
- **severity**: critical
- **applies_if**: all
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/User_Privacy_Protection_Cheat_Sheet.html

### incident-response-readiness
- **definition**: Incident readiness is a maintained, owner-assigned set of playbooks for credential compromise, data breach, dependency/CVE, and ransomware. Each playbook defines containment, evidence preservation, eradication, recovery, notification, decision authority, and tested contacts before an incident occurs.
- **implementation**:
  - Maintain severity criteria, on-call and escalation contacts, communication channels, legal/privacy ownership, and customer notification decision points.
  - Provide actionable steps for token revocation, account/session containment, network isolation, artifact quarantine, forensic preservation, and safe recovery.
  - Keep inventories of secrets, assets, dependencies, backups, logging retention, and provider contacts referenced by the playbooks.
  - Run tabletop exercises and technical simulations on a schedule; record findings, owners, deadlines, and evidence that contacts and automation still work.
- **probe**: The assessor must inspect current playbooks for token/credential, breach, CVE, and ransomware scenarios, verify named owners and notification paths, and review a recent tabletop or technical exercise with closed findings. Confirm evidence-preservation and revocation steps are executable in staging.
- **failure_modes**: Reduces containment delay after leaked credentials; prevents forensic evidence being overwritten during rushed recovery; prevents missed regulatory or customer notifications during a breach.
- **severity**: critical
- **applies_if**: all
- **sources**: https://csrc.nist.gov/pubs/sp/800/61/r3/final, https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final
