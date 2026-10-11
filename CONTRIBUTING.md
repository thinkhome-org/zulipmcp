# Contributing

Use short `codex/<topic>` branches. Commit code, tests and documentation together
with Conventional Commit messages. Push the topic branch, then open a draft PR.
Wait for required checks and independent review before merging; never force-push
main or bypass protection. During bootstrap, dependent local branches may be
stacked until earlier PRs are reviewed and merged.

Run all commands in README before requesting review. Add a failing regression
or behavior test before nontrivial implementation. Use synthetic test credentials
and disposable test realms. Never test mutations against production Zulip.

Keep secrets out of Git, command arguments, test fixtures, logs and PRs. Store
local secrets outside the checkout, or in ignored environment files. Only
sanitized examples may be tracked. Review staged changes before every commit.

Dependency changes must update uv.lock and explain the need. GitHub Actions are
pinned to full commit SHAs. CI uses no production secrets and read-only repository
permissions. Release and production deployment are separate actions.
