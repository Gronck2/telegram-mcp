# Plan 0011 Client MCP Roots narrow the server allowed roots; image hardening

Task: `docs/tasks/0011-client-roots-narrow-server-roots.md`.

Base: `ours` at 7b4062f (task 0009 merged).

## Approach

Two independent layers, one branch, separate commits so each can be reviewed
and reverted on its own: (A) the narrowing rule in `runtime.py`, (B) image and
compose hardening. A is the control; B limits the damage if A is bypassed or
no server root is configured.

### A. Narrowing in `runtime.py` (security-shaped path)

1. Add `ROOTS_STATUS_CLIENT_OUTSIDE_SERVER = "client_outside_server_roots"`
   next to the other `ROOTS_STATUS_*` constants (:1274-1281).
2. Add a pure helper next to `_path_is_within_any_root` (:3354):

   ```python
   def _narrow_client_roots(client_roots, server_roots):
       """Client roots that lie inside some server root (equal or under)."""
       return [c for c in client_roots if _path_is_within_any_root(c, server_roots)]
   ```

   Both arguments are already realpaths (`resolve(strict=True)`); the helper
   relies on `_path_is_within_root`, which also re-resolves the server root and
   handles a file root. Ancestors of a server root are not within it, so they
   are dropped (decision 1a). Do not compare strings.
3. Route every branch that returns client-derived roots through one small
   function, so no branch is forgotten:

   ```python
   def _apply_server_ceiling(client_roots, fallback_roots):
       if not fallback_roots:
           return _dedupe_paths(client_roots), ROOTS_STATUS_READY
       narrowed = _narrow_client_roots(client_roots, fallback_roots)
       if narrowed:
           return _dedupe_paths(narrowed), ROOTS_STATUS_READY
       logger.warning("... dropped client roots outside server roots ...")
       return [], ROOTS_STATUS_CLIENT_OUTSIDE_SERVER
   ```

   Call it at both places that return client-derived roots in
   `_get_effective_allowed_roots_with_status`: the bare-path recovery branch
   (:3514-3517) and the main branch (:3541-3542). Leave untouched:
   `ctx is None`, server-only, timeout, unsupported-roots, error, empty-list
   fallback and deny-all. The empty-intersection result must not consult
   `_server_roots_fallback_enabled()`.
4. `_ensure_allowed_roots` (:3563): add a branch for the new status with the
   text from the task (before the generic "disabled until allowed roots are
   configured" default). Existing branches and texts stay byte-identical.
   Keep `_path_refusal` so `invalid_argument` is recorded.
5. Nothing changes in `_resolve_readable_file_path` /
   `_resolve_writable_file_path`: they already confine to whatever roots they
   get. `_first_resolution_root(roots)` now yields the first narrowed root.
6. `main.py` re-exports: check whether `main.py` mirrors `ROOTS_STATUS_*`
   names or the helper (it star-reexports runtime names via `__all__`); add
   the new constant to any explicit list if one exists.
7. Windows: no new code. `_coerce_root_uri_to_path` already maps
   `file:///C:/x`; `Path` equality is case-insensitive on `nt`. State in the
   review that Windows was not run.

Keep complexity low (the module already has C901 hits); the helper functions
above exist to keep `_get_effective_allowed_roots_with_status` from growing
branches.

### B. Image and compose hardening

`Dockerfile` (production stage only):

- Keep `COPY` as root. Replace `chown -R appuser:appuser /app` with creating
  and chowning only the data directories:
  `mkdir -p /app/data/transcripts /app/data/files /home/appuser/.local/state/telegram-mcp`
  then `chown -R appuser:appuser /app/data /home/appuser`. Verify `adduser`
  creates `/home/appuser` on alpine busybox (it does without `-H`).
- Named volumes copy ownership from the image path on first use, so the
  directories must exist and be owned by `appuser` in the image.
- The development stage is unchanged (runs tests as root, no network use).

`docker-compose.yml`:

- `read_only: true`; `tmpfs: ["/tmp"]` (single-instance locks, `mkstemp`
  files); the existing bind mount `./transcript_cache:/app/data/transcripts`
  stays.
- Volumes: `telegram_state:/home/appuser/.local/state` (alias store, incoming
  feed, resolve_phone state) and, per decision 3a, `telegram_files:/app/data/files`
  with `TELEGRAM_ALLOWED_ROOTS: ${TELEGRAM_ALLOWED_ROOTS:-/app/data/files}`.
  Declare the named volumes at the top level.
- `TELEGRAM_AUDIT_LOG: /home/appuser/.local/state/telegram-mcp/audit.log`
  (decision 4a). The `/app/mcp_errors.log` handler falls back to console
  logging on a read-only root (existing `except` in `runtime.py:1239`); note it
  in a compose comment.
- Comments updated: the "mount ./telegram_sessions:/app" example would
  shadow the code and break read-only; change it to a dedicated session
  directory comment (if a file session is used, mount a dedicated directory
  and set `TELEGRAM_SESSION_NAME` to a path in it).
- Optional, only if a local container test passes (decision 5):
  `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`.
- Do not run `docker compose config` unfiltered (expands `.env`). Validate
  YAML with `python -c "import yaml..."`-style parsing in a test instead.

### C. Docs

- `README.md`: rewrite the "Security behavior" bullets (:754, :759-767): server
  roots are a ceiling, client roots narrow them; ancestors are dropped; the
  empty-intersection deny-all and that the fallback flag does not apply to
  it; with no server roots the client roots decide, so shared HTTP/SSE
  deployments must set `TELEGRAM_ALLOWED_ROOTS` (optionally with `TELEGRAM_SERVER_ROOTS_ONLY`).
  Adjust the "Clients on another machine" paragraph (:793-794) and the
  troubleshooting line (:1102) as needed. README is not a security path.
- `docs/arch/overview.md` item 3 (File roots, :97): one or two sentences
  with the rule and the new status.
- `CLAUDE.md` "File-path security" paragraph: update the one-line summary
  (roots come from client or CLI; server roots are the ceiling). CLAUDE.md
  is not a security path but is project policy; keep to one sentence.
- `.env.example`: if it documents `TELEGRAM_ALLOWED_ROOTS`, add the ceiling
  note (check; `.env*` is a security-shaped path, so review it).

## Steps

1. Tests first (failing), see strategy.
2. Constants, `_narrow_client_roots`, `_apply_server_ceiling`, wiring in the
   two branches, `_ensure_allowed_roots` message.
3. Dockerfile and docker-compose changes (separate commit).
4. Docs.
5. Gates, then the mandatory deep review (security-shaped paths:
   `runtime.py`, `Dockerfile`, `docker-compose.yml`, possibly `.env.example`).
   Reviewer checklist: every client-derived return path narrows; no path
   consults the fallback flag after an empty intersection; ancestor/symlink/
   file-root cases; `S` empty unchanged; message text byte-compat; compose
   write-path table complete (re-grep for `open(`, `mkdir`, `sqlite3`,
   `mkstemp`, `FileHandler`, `write_bytes`, `os.replace`).
6. Optional local container check, if Docker is available and without touching
   the live deployment: `docker build --target production -t telegram-mcp:task0011 .`
   then `docker run --rm --read-only --tmpfs /tmp --entrypoint sh
   telegram-mcp:task0011 -c 'touch /app/telegram_mcp/x; echo $?'` expecting
   a failure and `touch /app/data/files/x` succeeding. Never start the
   application there (no session string, never publish port 8765, container
   name must differ from `telegram-mcp`). If Docker is not available, say so
   in the PR; the static tests cover the file contents.

## Files likely touched

- `telegram_mcp/runtime.py` (constant, helpers, two call sites,
  `_ensure_allowed_roots` branch)
- `Dockerfile`, `docker-compose.yml`
- `README.md`, `docs/arch/overview.md`, `CLAUDE.md` (one sentence),
  possibly `.env.example`
- New `tests/test_client_roots_ceiling.py`, new `tests/test_deploy_hardening.py`;
  existing roots tests in `tests/test_runtime.py` (:1261-1567) and
  `tests/test_file_path_security.py` may need adjustment where a test uses
  client roots outside the server roots on purpose (expected change; list
  each in the PR, do not weaken assertions otherwise).

Not touched: `media_links.py`, `singleton.py`, `sanitize.py`, transport, the
tool modules (confinement already happens in the shared resolvers),
`runner.py` unless decision 2b is accepted, and the owner's live
`Dockerfile.local` / `docker-compose.override.yml`.

## Tradeoffs considered

- Narrow versus replace: replace is the current, exploitable behaviour;
  narrowing keeps the legitimate use (a client scoped to a project folder
  inside the server root) and removes the escalation.
- Ancestor dropped versus clipped to the server root (1a): clipping would make
  `file:///` mean "the whole server root", i.e. the client gets a grant it
  never named, and hides misconfiguration. Dropping gives an explicit
  deny-all with a message. Cost: a client rooted at a parent of the server
  root now needs its root changed.
- No fallback after an empty intersection: falling back to `S` would let an
  attacker-declared out-of-bounds root grant the full server roots.
  Deny-all is the same shape as the existing empty-list default.
- A new status versus reusing `client_deny_all`: the old text says the client
  sent an empty list, which would mislead the operator.
- Ceiling check at the roots level versus in each resolver: one place, shared
  by read and write tools and by any future tool; the resolvers stay as is.
- Leaving `S`-empty behaviour (decision 2): changing it breaks stdio use where
  the client roots are the only configuration. Documented plus optional
  warning instead.
- Read-only rootfs plus root-owned `/app` versus only root-owned `/app`: the
  second already stops the code overwrite; the first also stops writes into
  `/home` and elsewhere and makes the write-path list explicit. Cost: the
  state volume and tmpfs configuration.
- Making the error log path configurable (runtime.py edit) versus console
  fallback: console fallback needs no code and `docker logs` is the normal
  sink in a container.
- A path deny list (`/proc`, code dir): defence in depth that can be added
  later; with narrowing and a read-only image it is not needed to close this
  hole.

## Test strategy

All under `tests/`, stub sessions only: `ctx` is a small object whose
`session.list_roots()` returns an object with `.roots` (each with `.uri`),
as in the existing roots tests; never real Telegram. Use `tmp_path` and
`monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [...])`.

New `tests/test_client_roots_ceiling.py`:

- Narrowing unit tests on `_get_effective_allowed_roots_with_status`: client
  root equal to the server root; client root under it (kept, only that root
  effective); sibling directory (dropped, status
  `ROOTS_STATUS_CLIENT_OUTSIDE_SERVER`); ancestor (`tmp_path.parent`, and the
  real `/` via `file:///`) dropped; mixed list keeps only the valid roots;
  duplicates deduped; server root that is a file admits only that file.
- No fallback: empty intersection with
  `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1` still returns `[]` and the new
  status. Empty client list with the flag still falls back (unchanged).
- Server-only mode unchanged; unsupported-roots client unchanged; timeout and
  error unchanged.
- Bare-path recovery branch (a stub `list_roots` raising a pydantic-style
  error with `errors()` payload, see the existing recovery tests): narrowed
  against the server roots too.
- `S` empty: client roots decide (regression, same as before).
- Symlink: client root is a symlink in `tmp_path/inside` pointing to
  `tmp_path/outside`; with `S = [inside-dir]` it is dropped; a symlink inside
  the server root that points inside it is kept.
- Mandatory scenario, end to end through the real resolvers and one real write
  tool: build `app/telegram_mcp/runtime_stub.py` and `app/data/files` under
  `tmp_path`, `S = [app/data/files]`, client roots `[file://<app>]` and
  `[file:///]`; call `_resolve_writable_file_path` with
  `raw_path=<app>/telegram_mcp/runtime_stub.py` and also `download_media`
  (stub client, stub `download_media`) targeting that file: refused, file
  bytes unchanged, stub client never asked to download. Same for
  `_resolve_readable_file_path` and `send_file` (stub client; `send_file`
  never called). Positive control: a target under `app/data/files` works.
- Messages: `_ensure_allowed_roots` returns the new text for the new status
  and records `invalid_argument` (with `TELEGRAM_STRUCTURED_ERRORS=1` like
  the 0007 tests); existing statuses' texts unchanged (assert the strings).
- Windows: skipped test marker `skipif(os.name != "nt")` for a drive-letter
  case, plus a pure `PureWindowsPath` unit test of the comparison semantics
  if the helper can take pure paths; otherwise state "unverified on Windows".

New `tests/test_deploy_hardening.py` (static, no Docker):

- Dockerfile production stage has no `chown -R appuser:appuser /app` and
  does chown `/app/data`; `USER appuser` present.
- Compose parsed with `yaml` (add to dev deps only if not already available;
  otherwise parse with a minimal regexp-free approach through `tomllib`-style
  fallback is not possible, so use `yaml` if installed in `.venv` and
  `importorskip` otherwise): `read_only` true, `/tmp` in `tmpfs`, the
  transcript mount, state volume, `TELEGRAM_AUDIT_LOG` set, and the port
  still `127.0.0.1:8765:8765`. Never read or print `.env`.

Existing tests: run the whole suite; adjust only tests that relied on client
roots outside the server roots, each with a note in the PR.

## Gates

Per the Pipeline contract:

- `uv run flake8 --select=E9,F63,F7,F82 <touched files>` (blocking) and
  `uv run flake8 <touched files>` with no new warnings versus base (watch
  C901 in `runtime.py`).
- `uv run black --check <touched files>`
- `uv run pytest --cov --cov-report=term-missing` (80% gate).
- Deep review (mandatory) as listed in step 5; PR into `ours` opened without
  asking; merge on green CI per the coordinator decision (the contract still
  requires the owner's explicit request for merging, which the coordinator
  has given on the owner's behalf for this task). Live deployment follow-up
  is reported to the owner after merge.
