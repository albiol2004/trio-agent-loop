# Security — research wave 1

Source: scout report (gpt-5.6-luna, wave 5 batch 1). Raw item list, pre-synthesis.

### oidc-discovery-issuer
- **what**: Configure each OIDC provider from trusted discovery metadata and require an exact HTTPS issuer, authorization endpoint, token endpoint, and JWKS endpoint rather than accepting tenant-supplied URLs.
- **why**: This prevents issuer mix-up, malicious-provider, and token-verification requests from being redirected to attacker infrastructure.
- **check**: probe
- **probe**: Parse provider metadata and assert the issuer exactly matches configuration, every endpoint is HTTPS, and no endpoint is derived from an untrusted request parameter.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://openid.net/specs/openid-connect-discovery-1_0.html, https://www.rfc-editor.org/rfc/rfc9700

### authorization-code-pkce
- **what**: Use OAuth authorization code with PKCE using S256 for public and browser clients, and do not enable implicit or resource-owner-password grants.
- **why**: This prevents authorization-code interception and access-token exposure in browser history, referrers, and front-channel fragments.
- **check**: probe
- **probe**: Capture an authorization request in an integration test and assert `response_type=code`, `code_challenge_method=S256`, a unique challenge, and no `access_token` in the redirect URL, then reject implicit and password grant configurations.
- **applies_if**: spa
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc7636, https://www.rfc-editor.org/rfc/rfc8252

### state-nonce-redirect-binding
- **what**: Bind a high-entropy single-use state and nonce to each login transaction and permit only exact pre-registered redirect URIs.
- **why**: This prevents login CSRF, authorization-response swapping, replay, and open-redirect token leakage.
- **check**: probe
- **probe**: Run two concurrent logins, swap their state, nonce, and redirect URI in callbacks, and assert both callbacks fail with 400/401; parse the redirect allowlist for exact matches rather than prefixes or wildcards.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://openid.net/specs/openid-connect-core-1_0.html, https://www.rfc-editor.org/rfc/rfc9700

### token-validation
- **what**: Validate every JWT signature against the intended issuer's JWKS with an explicit algorithm allowlist and enforce required `iss`, `aud`, `exp`, `nbf`, `iat`, and conditional `azp` claims.
- **why**: This prevents algorithm-confusion, forged, expired, not-yet-valid, and cross-service token acceptance.
- **check**: probe
- **probe**: Feed the verifier fixtures with an invalid signature, `alg=none`, an unapproved algorithm, wrong issuer/audience/authorized party, expired `exp`, and future `nbf`, and assert each returns 401.
- **applies_if**: all
- **severity**: critical
- **sources**: https://openid.net/specs/openid-connect-core-1_0.html, https://www.rfc-editor.org/rfc/rfc8725

### access-token-audience
- **what**: Make each API accept only access tokens issued for its exact audience and scopes, and never treat an OIDC ID token as an API bearer token.
- **why**: This prevents token substitution in which a token valid for one client or service is replayed against another.
- **check**: probe
- **probe**: Send a correctly signed ID token and a correctly signed access token with the wrong `aud` to every protected API, assert 401/403 for both, and assert success only for a correctly-audienced access token with the required scope.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://openid.net/specs/openid-connect-core-1_0.html, https://www.rfc-editor.org/rfc/rfc9700

### browser-token-storage
- **what**: Keep browser access and refresh tokens in a BFF/server session or short-lived in-memory state and never persist bearer tokens in localStorage, sessionStorage, URLs, or browser-readable cookies.
- **why**: This limits token theft when XSS, browser extensions, history, referrer leakage, or a shared workstation is compromised.
- **check**: probe
- **probe**: Scan source and built bundles for `localStorage`/`sessionStorage` assignments containing `token`, `jwt`, `access_token`, or `refresh_token`, then run a browser test asserting no bearer token exists in storage, DOM, URL, or a non-HttpOnly cookie.
- **applies_if**: spa
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html

### native-token-storage
- **what**: Store mobile refresh tokens only in the platform keystore/keychain and use claimed HTTPS universal/app links or loopback redirects instead of unclaimed custom URI schemes.
- **why**: This prevents another installed application or a filesystem backup from stealing long-lived credentials.
- **check**: probe
- **probe**: Inspect the mobile package and integration test that completes login, asserting use of Android Keystore/iOS Keychain, no plaintext token in preferences/database/logs, and a claimed redirect with an unambiguous app identity.
- **applies_if**: mobile
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc8252, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

### refresh-token-rotation
- **what**: Rotate refresh tokens on every redemption, detect reuse and revoke the token family, and revoke sessions on logout, password reset, or confirmed compromise.
- **why**: This prevents a copied refresh token from silently creating an indefinitely usable session.
- **check**: probe
- **probe**: Redeem one refresh token twice and execute logout/password-reset flows, asserting the second redemption returns `invalid_grant` and all subsequent access-token requests fail with 401.
- **applies_if**: all
- **severity**: critical
- **sources**: https://www.rfc-editor.org/rfc/rfc9700, https://www.rfc-editor.org/rfc/rfc7009

### session-cookie-hardening
- **what**: Issue high-entropy server-side session identifiers in `Secure`, `HttpOnly`, and appropriate `SameSite` cookies with narrow Path/Domain scope and preferably the `__Host-` prefix.
- **why**: This reduces theft through script access or plaintext transport and limits cookie injection, fixation, and cross-site sending.
- **check**: probe
- **probe**: Run `curl -sS -D -` against login and session-refresh endpoints, parse every session `Set-Cookie`, and assert `Secure`, `HttpOnly`, `SameSite=Lax`/`Strict`, no broad `Domain`, and at least 128 bits of unpredictable value.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

### session-lifecycle
- **what**: Rotate the session identifier after authentication and privilege changes, enforce idle and absolute lifetimes, and invalidate server-side state on logout and account disablement.
- **why**: This prevents session fixation and keeps stolen or abandoned sessions usable after a security boundary should have ended.
- **check**: probe
- **probe**: In an end-to-end test compare the pre-auth and post-auth identifiers, perform a role change, wait through configured idle and absolute limits, and assert each old or expired session receives 401 after logout or disablement.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

### csrf-state-changes
- **what**: Require a framework CSRF token and trusted Origin/Referer validation for every state-changing cookie-authenticated request, with SameSite cookies as defense in depth.
- **why**: This prevents an attacker-controlled site from causing authenticated transfers, account changes, or destructive actions.
- **check**: probe
- **probe**: Issue each state-changing request from an untrusted Origin without a CSRF token and assert 403 with no state change, then assert a valid token and trusted origin succeed.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html

### deny-by-default-authorization
- **what**: Require every route, RPC, job, and tool action to authenticate the principal and pass an explicit server-side policy, with unknown actions and policy errors denied.
- **why**: This prevents a newly added endpoint, missing rule, or authorization-service outage from silently becoming public or fail-open.
- **check**: probe
- **probe**: Enumerate the route/action registry, send unauthenticated requests and unknown actions, and assert 401/403 with no side effect while failing CI for any route lacking an explicit policy binding.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/IndexASVS.html

### object-level-authorization
- **what**: Check ownership, tenant, and action permissions server-side on the object loaded from the database rather than trusting an object ID, hidden field, or client role.
- **why**: This prevents IDOR and horizontal privilege escalation through guessed, changed, or bulk-supplied identifiers.
- **check**: probe
- **probe**: Create equivalent objects for users or tenants A and B, replay A's read/update/delete/export/download requests with B's identifier and in bulk arrays, and assert 403/404 for every endpoint.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

### function-level-authorization
- **what**: Enforce separate role and scope policies for administrative, debug, export, impersonation, billing, and bulk operations regardless of whether the UI exposes them.
- **why**: This prevents ordinary users from invoking privileged functions directly through crafted routes or API calls.
- **check**: probe
- **probe**: Run a role-by-route negative matrix against admin, debug, export, impersonation, and bulk endpoints and assert ordinary roles always receive 403 even when all client-side controls are bypassed.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/IndexASVS.html

### least-privilege-scopes
- **what**: Issue minimum OAuth scopes and roles and give each workload, database account, queue consumer, and cloud identity only the exact resources and actions it needs.
- **why**: This limits data exposure and destructive blast radius after a token, service, dependency, or operator account is compromised.
- **check**: probe
- **probe**: Parse effective IAM, OAuth, database, and service-account policies for wildcard principals/actions/resources, compare them with an approved permission set, and fail unexpired unexplained administrative grants.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final

### sql-injection-prevention
- **what**: Use parameterized queries or safe ORM query APIs, allowlist any dynamic identifiers, validate types and bounds, and run with a database identity that lacks DDL and unnecessary data access.
- **why**: This prevents attacker input from changing query structure or reading, modifying, or deleting records.
- **check**: probe
- **probe**: Send a corpus containing quote, comment, boolean, union, and time-delay payloads through every query input, assert no syntax/error or row-set expansion, statically scan query construction, and parse database grants.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/IndexASVS.html

### xss-prevention
- **what**: Apply context-aware output encoding and safe templating, sanitize explicitly for permitted rich HTML, avoid unsafe DOM sinks, and use Trusted Types where supported.
- **why**: This prevents attacker content from executing script that steals sessions, reads PII, or performs actions as the victim.
- **check**: probe
- **probe**: Inject `<script>alert(1)</script>` and context-specific quote payloads into every rendered field, assert encoded text and no script execution in a headless browser, and grep for unreviewed `innerHTML`, `eval`, or equivalent sinks.
- **applies_if**: spa
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html

### command-injection-prevention
- **what**: Avoid shell execution, invoke a fixed executable with an argument array and strict allowlists, and isolate any unavoidable process with a non-root identity and resource limits.
- **why**: This prevents input metacharacters or argument boundaries from becoming arbitrary operating-system commands.
- **check**: probe
- **probe**: Fuzz every process input with `;id`, `&&id`, `$(id)`, newline, option-injection, and argument-boundary payloads, assert no child process beyond the fixed executable, and inspect that shell execution is disabled.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html

### ssrf-egress-controls
- **what**: Allowlist outbound schemes, hosts, and ports, resolve and re-check every address while blocking loopback/private/link-local/metadata ranges, limit redirects, and enforce network egress policy.
- **why**: This prevents a URL feature from reaching cloud metadata, internal control planes, private services, or attacker-chosen destinations.
- **check**: probe
- **probe**: Exercise every server-side fetcher with loopback, RFC1918, IPv6-local, link-local/metadata, DNS-rebinding, non-HTTP, and redirect-chain targets and assert rejection plus blocked egress.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html

### path-traversal-prevention
- **what**: Map client file IDs to server-side paths, canonicalize and enforce the intended base directory after resolution, and reject encoded separators, absolute paths, NUL bytes, and unsafe symlinks.
- **why**: This prevents reads or writes of application secrets, configuration, source, or host files outside the intended directory.
- **check**: probe
- **probe**: Exercise file endpoints with `../`, encoded and double-encoded separators, absolute paths, NUL bytes, mixed separators, and symlink fixtures, asserting rejection and no access outside the configured root.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Path_Traversal_Cheat_Sheet.html

### vault-kms-secret-delivery
- **what**: Store secrets in a managed Vault or KMS and retrieve them at runtime with workload identity and path-level policy instead of embedding values in source, images, frontend bundles, or static manifests.
- **why**: This prevents repository, image registry, CI artifact, and client-bundle compromise from exposing production credentials.
- **check**: probe
- **probe**: Scan source, git history, manifests, images, and built bundles with a secret scanner, then inspect deployment policy to assert runtime Vault/KMS references and no static credential values.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final

### secret-rotation-revocation
- **what**: Assign owners and lifetimes and automate rotation and emergency revocation for API keys, database credentials, OAuth client secrets, certificates, encryption keys, and signing keys.
- **why**: This limits the useful lifetime of a leaked credential and provides a tested response to compromise.
- **check**: probe
- **probe**: Parse secret metadata for owner, maximum age, last rotation, and revocation capability, then run a staging rotation test proving the old credential fails and the new one works without changing application source.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final

### secret-leak-prevention
- **what**: Enable pre-commit and CI push protection, scan history and release artifacts, and revoke and replace any detected credential rather than merely deleting its current copy.
- **why**: Git history, build logs, support dumps, and registries retain deleted secrets and remain searchable by attackers.
- **check**: probe
- **probe**: Run a secret scanner against the full repository history, CI artifacts, container layers, release bundles, and exported support data, inject a canary credential to verify blocking, and assert the canary is revoked after detection.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/218/final

### log-redaction
- **what**: Redact or hash authorization headers, cookies, access and refresh tokens, passwords, keys, raw request bodies, and unnecessary PII before logs, traces, metrics, crash reports, and support exports.
- **why**: This prevents observability and debugging systems from becoming a second credential and PII exfiltration channel.
- **check**: probe
- **probe**: Send canary secrets, tokens, passwords, and PII through success and error paths, export logs/traces/metrics, and assert exact and decoded canary values are absent from every sink.
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

### dependency-vulnerability-gates
- **what**: Lock direct and transitive dependency versions, run SCA and license policy checks in CI, and block exploitable critical/high findings unless a time-bounded, owner-approved exception exists.
- **why**: This prevents known vulnerable or unreviewed packages from silently reaching production.
- **check**: probe
- **probe**: Run the package manager's audit plus an approved SCA scanner against every lockfile and build artifact, assert reproducible dependency resolution, and fail on unexpired policy violations.
- **applies_if**: monorepo
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Dependency_Management_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/218/final

### container-hardening-scanning
- **what**: Build from minimal digest-pinned base images, run as non-root with a read-only filesystem and dropped capabilities, and scan images, IaC, and runtime configuration before deployment.
- **why**: This reduces known-CVE exposure and limits damage from a compromised process or container escape.
- **check**: probe
- **probe**: Build the release image, inspect its effective user, root filesystem, capabilities, seccomp, and base digest, run an approved image/IaC CVE scanner, and parse deployment `securityContext` for violations.
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/190/final, https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html

### release-sbom
- **what**: Generate and retain a complete SPDX or CycloneDX SBOM for every release, including transitive, bundled, runtime, and container-layer components tied to the immutable artifact digest.
- **why**: This enables rapid impact analysis and targeted remediation when a component vulnerability is disclosed.
- **check**: probe
- **probe**: Generate the SBOM in CI, parse it for every package and container layer, assert the release digest and build ID are present, and fail publication when the artifact-to-SBOM mapping is missing.
- **applies_if**: monorepo
- **severity**: important
- **sources**: https://www.cisa.gov/topics/cyber-threats-and-advisories/software-bill-materials-sbom, https://cyclonedx.org/specification/

### provenance-signing-verification
- **what**: Produce SLSA provenance in isolated CI, sign artifacts and SBOMs with Sigstore or managed signing keys, and require deploy-time verification of the expected builder, source revision, digest, and policy.
- **why**: This prevents an altered build, compromised registry, or unauthorized pipeline from being deployed as a trusted release.
- **check**: probe
- **probe**: Verify a release signature and provenance against the expected repository, revision, builder identity, and artifact digest, then submit unsigned, mismatched, and locally rebuilt artifacts to deployment and assert rejection.
- **applies_if**: monorepo
- **severity**: critical
- **sources**: https://slsa.dev/spec/v1.0/, https://docs.sigstore.dev/

### tls-baseline
- **what**: Permit TLS 1.2 and 1.3 with modern cipher suites, disable SSLv3/TLS 1.0/1.1 and weak renegotiation, validate certificate hostname and chain, automate renewal, and use mTLS for sensitive internal service identities where warranted.
- **why**: This prevents downgrade attacks, interception, weak-cryptography exposure, and outages caused by expired or misissued certificates.
- **check**: probe
- **probe**: Run `openssl s_client` or an approved TLS scanner against every public and internal listener, assert legacy protocols and weak suites fail, verify hostname/chain and expiry thresholds, and test renewal before expiry.
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/52/r2/final, https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html

### content-security-policy
- **what**: Deploy a restrictive CSP using nonces or hashes, prohibit `unsafe-inline` and `unsafe-eval` unless documented, and collect violation reports during rollout.
- **why**: This limits exploitability and impact when an XSS or unsafe content injection defect slips past encoding controls.
- **check**: probe
- **probe**: Parse `curl -sS -D -` response headers for CSP, assert approved script/style sources and nonce/hash use with no unexplained unsafe directives, and run a browser injection test that records a blocked violation.
- **applies_if**: spa
- **severity**: important
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html

### hsts
- **what**: Send HSTS on HTTPS responses with a policy duration of at least one year and include subdomains only after confirming every subordinate host is HTTPS-safe.
- **why**: This prevents first-visit and downgrade attacks from sending cookies or PII over plaintext HTTP.
- **check**: probe
- **probe**: Run `curl -sS -D - https://host/` and assert `Strict-Transport-Security: max-age>=31536000` with the approved `includeSubDomains` decision, then assert HTTP requests redirect without serving content.
- **applies_if**: all
- **severity**: important
- **sources**: https://www.rfc-editor.org/rfc/rfc6797, https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html

### cors-allowlist
- **what**: Allow only exact, reviewed origins and required methods/headers, never combine wildcard origin with credentials, and emit `Vary: Origin` when responses vary by origin.
- **why**: This prevents hostile origins from reading authenticated API responses or abusing broad cross-origin trust.
- **check**: probe
- **probe**: Send requests with trusted, untrusted, `null`, wildcard-like, and credentialed origins, asserting CORS headers appear only for the exact allowlist and never contain wildcard origin with credentials.
- **applies_if**: web-api
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/CORS_OriginHeaderScrutiny_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html

### abuse-rate-limiting
- **what**: Apply endpoint-specific quotas and adaptive throttles keyed by account, IP, device, token, and tenant to login, recovery, enumeration, expensive, and write operations with consistent 429 responses.
- **why**: This limits credential stuffing, brute force, scraping, resource exhaustion, and costly abuse without relying on a single spoofable IP limit.
- **check**: probe
- **probe**: Send bursts that exceed each configured login, recovery, read, write, and expensive-operation quota from one and many keys, and assert 429, `Retry-After`, audit events, and no bypass through header spoofing.
- **applies_if**: all
- **severity**: important
- **sources**: https://owasp.org/API-Security/editions/2023/en/0x00-header/, https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html

### security-audit-logging
- **what**: Record an immutable, access-controlled audit trail for authentication, authorization failures, administrative changes, data exports, secret/key operations, and security configuration changes with actor, target, outcome, time, and correlation ID.
- **why**: This provides the evidence needed to detect abuse, investigate incidents, prove accountability, and distinguish operator actions from attacker actions.
- **check**: judgment (probe-able: trigger each event in staging and parse the audit sink schema; verify append-only)
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final

### pii-encryption-at-rest
- **what**: Classify and minimize PII and encrypt databases, files, queues, caches, and backups with envelope encryption using KMS-managed keys separated by environment and access role.
- **why**: This limits disclosure when disks, snapshots, backups, storage buckets, or database exports are accessed outside the application.
- **check**: judgment (probe-able: parse storage/backup config for KMS key IDs, sample a persisted record for ciphertext)
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html, https://csrc.nist.gov/pubs/sp/800/111/final

### pii-in-transit-and-minimization
- **what**: Require authenticated TLS on every PII hop, prohibit plaintext PII in URLs and telemetry, minimize collected fields, and apply explicit retention and deletion rules.
- **why**: This prevents network observers, proxy histories, referrers, logs, and unnecessary retained data from exposing PII beyond its operational purpose.
- **check**: judgment (probe-able: canary PII through requests/telemetry, assert absence in URLs/logs/traces)
- **applies_if**: all
- **severity**: critical
- **sources**: https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/User_Privacy_Protection_Cheat_Sheet.html

### incident-response-readiness
- **what**: Maintain owner-assigned playbooks for token or credential compromise, data breach, dependency/CVE, and ransomware covering containment, evidence preservation, eradication, recovery, notification, and tested contacts.
- **why**: This reduces containment delay, preserves forensic evidence, and prevents missed customer or regulatory obligations during a high-pressure incident.
- **check**: judgment
- **applies_if**: all
- **severity**: critical
- **sources**: https://csrc.nist.gov/pubs/sp/800/61/r3/final, https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final
