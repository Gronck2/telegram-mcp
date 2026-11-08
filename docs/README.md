# Fork docs

Working documents for the Gronck2/telegram-mcp fork (branch `ours`). They are
fork-only and never proposed upstream. User-facing documentation stays in the
top-level `README.md`.

- **`tasks/`** — what to build and why: one file per task (`NNNN-slug.md`),
  with the requirement, constraints and acceptance criteria.
- **`plan/`** — how: the approach for a task, written before implementing it.
  Shares the `NNNN` number with its task.
- **`adr/`** — architecture decision records. Immutable once accepted; a
  changed decision gets a new ADR that supersedes the old one.
- **`arch/`** — the current shape of the system (`overview.md`), kept up to
  date as the code changes.
