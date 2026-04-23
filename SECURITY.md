# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in Obscura, please report it privately. **Do not open a public GitHub issue.**

**Email:** security@obscura.dev

Encrypt sensitive reports with the maintainer's PGP key (see `SECURITY.asc` in this repo once published; until then, contact the email above and we'll arrange an encrypted channel).

Please include:

- A description of the issue and its impact
- Steps to reproduce, or a proof of concept
- The affected versions (if known)
- Any suggested remediation

## What to expect

- **Acknowledgement:** within 3 business days of your report.
- **Triage update:** within 10 business days confirming severity and an initial plan.
- **Fix or mitigation:** on a timeline proportional to severity — critical issues get patched within 14 days where feasible.
- **Coordinated disclosure:** we'll agree a public disclosure date with you, typically 90 days after the fix is available, earlier with your consent.

We credit reporters in release notes unless you ask otherwise.

## Safe harbor

We consider security research conducted in good faith under this policy to be authorized. We will not initiate or support legal action against researchers who:

- Act in good faith to identify and report vulnerabilities
- Give us a reasonable opportunity to investigate and remediate before public disclosure
- Avoid privacy violations, data destruction, degradation of service, or disruption of other users
- Do not access data beyond what's necessary to demonstrate the vulnerability

This authorization is limited to Obscura's own code and hosted infrastructure. Testing against third-party services Obscura integrates with (LLM providers, MCP servers, cloud APIs) is outside this safe harbor — see their individual policies.

## Scope

**In scope:**

- The Obscura Python package (on PyPI when published, and all repositories under this organization)
- The Obscura FastAPI server (`obscura/server/`) and HTTP API
- The Obscura CLI (`obscura-auth`, `obscura` entry points)
- The Helm chart and published container images in this repo
- The browser extension under `packages/browser-extension/`
- Any hosted Obscura instance operated by this project

**Out of scope:**

- Third-party LLM provider behavior (Anthropic, OpenAI, GitHub Copilot, Moonshot, etc.) — report to them directly
- MCP servers installed by users from third-party sources
- A2A peer agents the user chooses to connect to
- Social engineering, physical access, or attacks on the reporter's own environment
- Issues in dependencies where Obscura does not substantially increase the attack surface — prefer reporting to the upstream project

**What's not a vulnerability (though we still want the feedback):**

- Model output quality, hallucinations, or incorrect agent actions — Obscura doesn't warrant model behavior. See our Acceptable Use Policy for the liability framing.
- User-directed tool execution — if a user instructs their agent to do something destructive and the tool succeeds, that's a feature of user-directed autonomy, not a vulnerability. Sandboxing, permission modes, and policy engine are the mitigations; bugs in *those* are in-scope.
- Missing security headers on routes that are not reachable in a supported deployment

## What we consider critical

- Remote code execution against the server
- Authentication or authorization bypass
- Secret/credential disclosure (user API keys, tokens, session material)
- Cross-tenant data leakage in the hosted service
- Bypass of the tool permission/policy engine in the default policy
- Persistence of attacker-controlled code across sessions

## Known-acceptable risks

These are documented, understood, and not vulnerabilities:

- Running the server with `OBSCURA_AUTH_ENABLED=false` and binding to non-loopback addresses — the server refuses to start in this configuration; you must explicitly set `OBSCURA_ALLOW_UNAUTHENTICATED=true` to override. Use that only for isolated environments.
- Upstream LLM providers see the prompts and tool-call arguments routed through them. Subprocessor list: `docs/subprocessors.md`.
- The CLI `~/.obscura/credentials.json` fallback is plaintext with mode 0600 when the OS keyring is unavailable.

## Hardening status

Obscura is working toward SOC2 readiness covering Security, Confidentiality, and Privacy Trust Services Criteria. Current control coverage is summarized in the private compliance repository (available to customers under NDA on request). Notable items already in CI:

- Secret scanning (gitleaks) on every PR and push
- Python SAST (bandit) on every PR and push
- Dependency CVE scanning (trivy)
- Container image CVE scan + keyless signing + CycloneDX SBOM
- Weekly re-scans against updated rule sets

## Version support

Security fixes are backported to the current and previous minor release. Older releases receive fixes only at our discretion.

---

Thanks for helping keep Obscura safe.
