# 0001. Use the dev-pipeline plugin on the fork-only `ours` branch

- **Status:** accepted
- **Date:** 2026-11-09

## Context

The fork Gronck2/telegram-mcp carries changes that stay out of upstream
chigwell/telegram-mcp (the first one: separate read/write chat allowlists,
#116). Work on it should follow a repeatable task → plan → implement → test →
review → PR flow. Upstream CI only runs for `main`, so pull requests inside
the fork were not checked at all.

## Decision

- Non-trivial work runs through the `dev-pipeline` plugin
  (`/dev-pipeline:run-task`). Project specifics live in the
  `## Pipeline contract` section of `CLAUDE.md`; there are no project-level
  copies of the plugin's agents.
- `ours` is the base branch and PR target, on Gronck2/telegram-mcp only.
  Nothing on `ours` (code, `CLAUDE.md`, `docs/`, the fork CI workflow) is
  proposed upstream.
- Upstream changes come in through a `sync/upstream-YYYYMMDD` branch that
  merges `origin/main` and is reviewed as a PR into `ours`. `ours` is not
  rebased or force-pushed.
- PRs into `ours` are opened without asking; merges, direct pushes,
  force-pushes and upstream syncs need the owner's explicit request. A PR
  with red or pending CI is never proposed for merge.
- `.github/workflows/fork-ci.yml` runs the contract's gates (black, flake8
  blocking selection, pytest with the 80% coverage gate) on pushes and pull
  requests to `ours`.

## Consequences

- The `dev-pipeline` plugin must be installed on the machine; sessions load
  its agents at start.
- GitHub Actions must be enabled once for the fork (Actions tab) before
  `fork-ci.yml` runs.
- `ours` history contains merge commits from upstream syncs; conflicts with
  upstream are resolved in the sync PR, where CI and review see them.
- Local `ours` in the deployment checkout tracks `fork/ours`, not
  `origin/main`.
