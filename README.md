# Autorepo

Autorepo schedules GitHub Dependabot pull requests. Each repository defines its own Dependabot groups, CI jobs, security scanner, and merge policy. The controller uses GitHub as its state store and requires Python 3.11+ on the runner.

## Wiring a repository

1. Configure Dependabot version and security updates on the default branch, including the native groups you want to prioritize. The GitHub Dependabot settings page shows whether alerts and security updates are enabled. Add weekly schedules, a one-day cooldown for version updates where supported, and project-specific package exclusions in `.github/dependabot.yml`.
2. Copy [the example policy](examples/autorepo.json) to `.github/autorepo.json`. Group names map to P4–P7. The controller reads security priorities through GitHub's alert-to-PR associations when those are available. Unlinked alerts appear in the Actions summary, and the controller flags uncertain PR priority.
3. Add a trusted controller workflow on the default branch. Invoke `OWNER/autorepo@COMMIT_SHA` in a GitHub-hosted job with `actions: write`, `contents: write`, `pull-requests: write`, and `statuses: write`. Use a repository-wide concurrency group with `cancel-in-progress: false`. Useful wakeups are `pull_request_target` for the default branch, `workflow_run: completed` for the validation workflow, `push` on the default branch, `workflow_dispatch`, and a scheduled reconciliation. Keep the controller job free of candidate checkout and execution. Pull-request events are wakeups, and the controller reconstructs the queue through GitHub APIs.
4. Add `workflow_dispatch` string inputs `pr`, `head`, `base`, `advisories` and `key` to the project's existing CI. Use the run name expression `format('Autorepo #{0} {1} {2} {3}', inputs.pr, inputs.head, inputs.base, inputs.key)` for controller dispatches. Before running candidate code, call `OWNER/autorepo/validate@COMMIT_SHA` with those inputs and the trusted branch. Make every required validation job depend on that guard for Autorepo runs. Check out the validated head explicitly with credential persistence disabled. Use GitHub-hosted candidate jobs with read-only permissions, no repository secrets, and isolated caches. Run existing tests and an ecosystem scanner, comparing the findings against the base and checking the supplied advisory IDs. Keep all required jobs non-optional.
5. To enable security ranking, pass a short-lived installation token from a repository-scoped GitHub App with **Dependabot alerts: read**. Check the scheduling and validation results before suppressing regular full CI for queued Dependabot PRs. Keep the human and default-branch CI paths active.

A validation result identifies the head, base, and advisory set. The scheduler can reuse a success for that state. It updates one stale branch at a time. GitHub's branch update endpoint is conditional on the current head and will reject some conflicts. For rejected updates, inspect the lockfile conflict and request recreation via Dependabot or regenerate the lockfile with the project's package manager. Actions interruptions and infrastructure-level conclusions can retry with exponential backoff. Test failures are surfaced for diagnosis. GitHub workflow history and commit statuses record the attempts. Set `publish_status: true` in the JSON policy to publish the `autorepo/validation` commit status on managed PR heads.

## Merge policy

Supervised review is the default. Optional auto-merge requires an approved lockfile-only patch/minor version group, verified Dependabot commits, a successful baseline run on the current base, and classic branch protection with strict required GitHub Actions checks, enforced admins, and disabled force pushes and deletion. The optional `protection-token` lets a scoped App read that branch protection without giving the controller its administration credentials. For example:

```json
"merge": {
  "method": "squash",
  "required_checks": ["CI / Tests", "CI / Dependency scan"],
  "lockfiles": ["pnpm-lock.yaml"]
}
```

The controller re-reads the PR and default branch before asking GitHub to merge the verified head. GitHub's strict checks must be configured and enforced on the actual PR. Dispatch results in GitHub are normally associated with the workflow revision. A project that suppresses full PR CI can use `publish_status` and require the `autorepo/validation` status from GitHub Actions after verifying the live workflow and strict branch protection. Review security updates, major changes, workflow edits, and manifest edits manually. Caveats and setup details are described in [GitHub's branch protection documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).

## Current scope

Autonomous repair and automated publishing are disabled. Mistral Vibe requires a verified credential and runner isolation design before activation. The scheduler currently surfaces failed validation for human diagnosis. GitHub-hosted integration, repository settings, and live merge behaviour require validation in the target repository.

Run the local scheduler tests with `python3 -m unittest discover -s tests -v`.
