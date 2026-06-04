# External actions checklist

This file lists the security-hardening steps that cannot be done from code.
Each one needs a human (you) clicking in a browser. Run through this list
once, and re-run the parts that need quarterly attention.

Treat this as the companion to `SECURITY.md`. Code-side hardening is in
the repos. Account-side hardening is here.

## One-time setup

### 1. Enforce 2FA on the GitHub org

- Sign in to https://github.com/organizations/sashimiboi/settings/security
- Under **Authentication security**, enable
  *Require two-factor authentication for everyone in the sashimiboi
  organization*.
- Confirm. Any account without 2FA gets removed; you can re-invite once
  they enable it.

### 2. Configure PyPI Trusted Publisher for `spooling`

The release workflow (`.github/workflows/release.yml`) uses GitHub Actions
OIDC to publish to PyPI. No long-lived token. Enable it once:

- Sign in to https://pypi.org and visit the project page for `spooling` (or
  the publisher settings if the project is not yet created).
- Under **Publishing**, click *Add a new publisher*.
- Choose **GitHub Actions**.
- Fill in:
  - Owner: `sashimiboi`
  - Repository: `spooling`
  - Workflow name: `release.yml`
  - Environment name: `pypi`
- Save.

### 3. Add a `pypi` deployment environment to the GitHub repo

This gives the release workflow a review gate before it publishes.

- Repo → **Settings → Environments → New environment**.
- Name it `pypi`.
- Add yourself as a **required reviewer**.
- Optional: restrict the environment to tags matching `v*.*.*`.

### 4. Lock down `main` on both repos

For `sashimiboi/spooling` and `sashimiboi/spooling-ee`:

- **Settings → Branches → Branch protection rules → Add rule** for `main`.
- Enable:
  - Require a pull request before merging
  - Require approvals (at least 1)
  - Require status checks to pass before merging
  - Require branches to be up to date before merging
  - Require signed commits
  - Require linear history
  - Restrict who can push to matching branches (only you for now)

### 5. Enable Dependabot security updates

- Repo → **Settings → Code security and analysis**.
- Turn on **Dependabot alerts**, **Dependabot security updates**, and
  **Dependabot version updates** (with `dependabot.yml` configured for
  pip/npm/github-actions).
- Apply to both `spooling` and `spooling-ee`.

### 6. Set up secret scanning + push protection

Same page as Dependabot:

- Turn on **Secret scanning**.
- Turn on **Push protection** so a token committed accidentally is
  rejected before it lands on origin.

### 7. Configure `security@spooling.ai`

- Set up the email address (Google Workspace or whichever provider you use
  for spooling.ai mail).
- Forward to your inbox. Triage like any other production-critical inbox.
- Keep the published response SLA in `SECURITY.md` (acknowledge in 3
  business days, triage in 7, fix in 30 for high severity).

## Quarterly checklist

Run these every three months. Calendar reminder is the easiest enforcement.

- Refresh the Dockerfile base-image digests in `spooling-ee` via
  `./scripts/lock-base-images.sh`. Commit the diff.
- Audit the org's active members and outside collaborators. Remove anyone
  who no longer needs access.
- Review the PyPI Trusted Publisher configuration. Confirm the workflow
  name, environment, and repo still match.
- Rotate any human-held secrets (DB admin password if not in Secrets
  Manager, dashboard service-account passwords, AWS profile credentials
  if you use static keys instead of SSO).
- Pull the GitHub audit log for the org and skim it for anything
  unexpected (new SSH keys, new webhooks, new OAuth apps).

## Per-release checklist

When cutting a new release of `spooling`:

- Bump the version in `pyproject.toml` on a release branch.
- Open the PR. Land it on `main` after CI is green.
- Tag the merge commit: `git tag -s v0.2.0 -m "v0.2.0"` (signed tag) and
  `git push --tags`.
- The release workflow runs. Approve the `pypi` environment when
  prompted.
- After PyPI shows the new version, verify the signature locally per the
  snippet in `SECURITY.md`.
- Publish a GitHub Release with the changelog.

## Per-incident checklist

When a vulnerability is reported or detected:

- Acknowledge the reporter within 3 business days.
- Triage: blast radius, affected versions, reproduction.
- Patch in a feature branch. Open a PR. Land on `main`.
- Cut a patch release using the per-release checklist above.
- Notify affected users (security advisory on the GitHub Releases page,
  email to the security list if there is one).
- Add an entry to `SECURITY.md` under Acknowledgements.

## What's intentionally not on this list

- SOC 2 audit prep. That's a Spooling EE concern, not an OSS spooling
  concern. Track that separately on the EE side.
- Bug bounty program. Defer until there's enough usage to justify a
  budget.
- Security training for org members. There's only one member.
