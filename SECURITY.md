# Security policy

Spooling indexes AI coding sessions on a developer's own machine. Because the
data it touches is, by definition, source code and chat history, security
matters here. This document describes how we handle vulnerabilities, how
releases are produced, and what users should verify before installing.

## Reporting a vulnerability

If you believe you have found a security vulnerability in `spooling`, please do
**not** open a public GitHub issue. Send a report to:

```
security@spooling.ai
```

Please include:

- A description of the issue and its impact.
- A reproduction (steps, sample data, or a proof-of-concept script).
- The affected version (`spooling --version`).
- Whether you intend to publish details, and on what timeline.

We will acknowledge receipt within 3 business days, share a triage
assessment within 7 business days, and aim to ship a fix within 30 days
for high-severity issues. We will credit the reporter in the changelog
unless asked otherwise.

Coordinated public disclosure: please give us 90 days from initial report
to ship a fix before publishing details. We are happy to negotiate a
shorter window for low-impact findings or a longer window for complex
ones.

## Supported versions

Only the latest minor release line receives security updates. While `spooling`
is pre-1.0, this means the latest `0.x.y` line is supported and earlier
`0.x` lines are not. Track the [Releases](https://github.com/sashimiboi/spooling/releases)
page for the current version.

## What's in scope

- The `spooling` CLI and its API server (`spooling ui`)
- The MCP endpoint Spooling exposes
- The packaging on PyPI as `spooling`
- Documented installation paths (pip, the OSS Docker compose, `spooling init`)

## What's out of scope

- Issues that require local code execution to exploit (Spooling is local-first;
  if an attacker is already on the user's machine, they have everything).
- Bugs in upstream packages we depend on. Report those upstream; we will
  bump our own pin when a patched version is released.
- Spooling Cloud (the hosted SaaS at `api.spooling.ai`). That is a
  separate product with its own security model. Reports about the Cloud
  also go to `security@spooling.ai` but are tracked separately.

## Verifying a release

Starting with `0.x.y` (TBD; see CI), every published release of `spooling` on
PyPI is signed via [Sigstore](https://www.sigstore.dev/) using GitHub
Actions OIDC as the identity. There are no long-lived PyPI API tokens.

To verify a release:

```bash
pip install --upgrade sigstore
pip download --no-deps spooling==<version>
sigstore verify identity \
  --cert-identity-regexp "https://github.com/sashimiboi/spooling/.+" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  spooling-<version>-py3-none-any.whl
```

A successful verify means the wheel was built and uploaded by the signed
GitHub Actions workflow on this repo, on a tagged commit, with no
intermediate hands. If verify fails, do not trust the wheel; report it via
the email above.

We treat any unsigned release on PyPI as a serious incident. If you ever
encounter one, please report it.

## Build provenance

The release pipeline lives in `.github/workflows/release.yml`. It runs
only on tagged commits matching `v*.*.*`, builds the wheel and sdist on a
clean GitHub-hosted runner, signs both with Sigstore, and publishes to
PyPI via the Trusted Publishers OIDC integration.

The repo enforces:

- Mandatory 2FA on the `sashimiboi` GitHub org for any account with write
  access.
- Branch protection on `main` (no direct pushes, signed commits required,
  status checks must pass).
- No long-lived PyPI tokens stored in GitHub secrets.

Anyone with maintainer access who wants to ship a release pushes a signed
tag. The workflow takes it from there.

## Spooling Cloud

If you use Spooling Cloud (the hosted SaaS), session data you choose to
sync leaves your machine, encrypted in transit, and lives in our AWS
account. Spooling itself does not phone home; Cloud is opt-in via an explicit
account login. The Cloud security model is documented separately and
shared with customers under a mutual NDA on request.

## Acknowledgements

Researchers credited for prior valid reports will be listed here. The list
is empty today.
