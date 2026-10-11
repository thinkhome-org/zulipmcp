# Security and upstream checks

`security.yml` runs on PRs, main pushes, manual dispatch and weekly schedules.
It audits the hash-locked runtime dependencies with pip-audit 2.10.1, scans
Python source/scripts with Bandit 1.9.4, and scans complete Git history with
checksum-pinned Gitleaks 8.30.1. All jobs have read-only repository permissions;
checkout credentials are not persisted and no production secrets are required.
`security-gate` fails if the scan job fails or cannot complete.

The exact eight fingerprints in `.gitleaksignore` are public examples from the
unchanged bundled Zulip schema, verified against the SHA-256 in
`data/provenance.json`. There is no path-wide or rule-wide secret exemption.
Bandit exceptions are local: XML generation and parsing of our own escaped
formatter output, a boolean OAuth validation flag, and a type-narrowing
assertion after the file tool's registration guard. Caller XML is not parsed.
The repository CODEOWNER owns these exceptions; review them by 2027-01-11 or
whenever the corresponding data flow changes, whichever comes first.

`upstream-drift.yml` runs manually and weekly on the default branch. It compares
the latest Zulip release tag, the commit behind the pinned stable tag, upstream
main, and the pinned schema's bytes. Drift or an unavailable/malformed upstream
response fails the check. A report artifact is retained for 30 days, including
failed verification output. It never changes provenance, the schema or the
catalog. This check does not audit reference MCP repositories or prove full API
coverage, and it is not a required PR check: upstream changes must not make an
unrelated PR unmergeable. Scheduled workflows start only after integration into
the default branch.

Dependabot proposes weekly uv and GitHub Actions updates through PRs. Scanner
tool versions and the Gitleaks archive checksum remain explicit manual updates;
no dependency update is automatically merged.

## Local evidence, 2026-10-11

- pip-audit: no known runtime dependency vulnerabilities.
- Bandit source and scripts: no findings after the narrow reviewed exceptions.
- Gitleaks: 29 Git commits scanned; no remaining findings. A temporary new
  credential fixture was rejected; it was not retained or committed.
- Drift script: latest release `12.3`, main
  `d7c57ed15b82b0bc8aefe2007f5ee2abeebd0ea2`, no changes.
- Drift tests: 2 passed in 0.06s; existing formatting/read-only tests:
  28 passed in 36.10s. Ruff, format, mypy, catalog consistency and wheel/sdist
  build passed. Source reproduction and live Zulip verification are separate
  gates.
- Installed-wheel stdio and HTTP authentication/identity smoke: 5 passed in
  35.00s on the final rebuilt artifact from a fresh environment, with the package imported from its installed
  wheel rather than the source tree. Runtime and test dependencies used the lock
  hashes; `uv pip check` found all 47 installed packages compatible. HTTP uses
  in-process ASGI requests; this is not Docker or live Zulip verification.
- Actionlint 1.7.12, downloaded from its official release and checksum-verified,
  accepted all four workflows.

The installed-wheel check exposed an sdist boundary bug: a local virtual
environment under `.superpowers` could enter the archive and prevent rebuilding
its wheel. The build now explicitly selects project source, tests, docs and
metadata rather than traversing arbitrary worktree files. CI creates an ignored
local marker before building, then checks both archives for forbidden runtime
paths and required schema/provenance/license files. This is an artifact check,
not a claim that Docker or publication has been verified.

Main protection was inspected: strict `ci-gate`, one independent approval,
stale approval dismissal, last-push approval, admin enforcement, no force push
or branch deletion. `CODEOWNERS` assigns the repository owner. Integration and
security gates are not yet required by branch protection; add them only after
the workflows exist on main and have passed. No protection was weakened.
Release workflow, Docker validation, SBOM/provenance and publication remain
open. No merge, release or deployment is part of this milestone.
