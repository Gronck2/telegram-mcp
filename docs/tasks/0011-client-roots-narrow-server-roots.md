# 0011 Client MCP Roots may only narrow the server allowed roots; harden the container image

## Goal

Close a file-tool authorization hole found by code reading on 2026-11-10 (not
exploited live). With server roots configured (`TELEGRAM_ALLOWED_ROOTS` / CLI
arguments, `SERVER_ALLOWED_ROOTS`), a client's non-empty `roots/list` answer
fully replaces them. The HTTP/SSE transport has no authentication (only
Host/Origin checks), so any local process on 127.0.0.1:8765 or any container
on the docker network can declare `file:///` and get the file tools over the
whole container filesystem. Make client roots only narrow the server roots,
and make the production image not writable at the code location, so the
hole is closed twice.

## Why

Findings (line numbers on `ours` at 7b4062f):

- `_get_effective_allowed_roots_with_status`
  (`telegram_mcp/runtime.py:3482`): after `ctx.session.list_roots()`, any
  client root that resolves (`_coerce_root_uri_to_path`, :3332) is returned as
  the effective roots with `ROOTS_STATUS_READY` (:3541-3542). The same holds
  for the bare-path recovery branch for Cursor-style clients (:3514-3517).
  `SERVER_ALLOWED_ROOTS` is consulted only for the server-only mode, the
  opt-in fallbacks, and unsupported-roots clients. README states it: "Client
  MCP Roots replace server CLI roots when available" (README.md:754).
- Read: `send_file`, `send_album`, `upload_file`, `send_voice`, `send_sticker`,
  `set_profile_photo`, `edit_chat_photo` all use `_resolve_readable_file_path`
  (:3605), which checks only `_path_is_within_any_root(candidate, roots)` with
  the replaced roots. With root `file:///` a caller can upload any readable
  file in the container to Telegram, e.g. the transcript cache under
  `/app/data` (plaintext personal-chat transcripts) or mounted media.
- Write (worse): `download_media`, `export_*` (`messages.py:3908`, `:4064`),
  and the kept-photo paths (`media.py:568`, `:612`, `:1027`,
  `custom_emoji.py:316`) use `_resolve_writable_file_path` (:3646): any
  extension, overwrite allowed, parent directories created. A caller can write
  Telegram-controlled bytes into `/app/telegram_mcp/*.py`. In the tracked
  Dockerfile (`chown -R appuser:appuser /app`, :55) and in the owner's local
  `Dockerfile.local` the code is owned by `appuser`, i.e. writable by the
  server process, so this is code execution on the next restart and from
  there theft of the session string.
- `/proc/self/environ` (holds the session string) is readable, but reports
  `st_size == 0`, so Telethon uploads zero parts. That is accidental
  protection, not a control.
- Other client-chosen write paths do not bypass roots: `prepare_upload` and
  the signed `/media/` and `/upload/` routes are confined to the first server
  root (`media._first_server_root`, `media_links`).

## Scope

1. `telegram_mcp/runtime.py`: when `SERVER_ALLOWED_ROOTS` is non-empty,
   client roots (from the `roots/list` answer and from the recovered bare
   paths) only narrow it; see "Behaviour".
2. New status and user-facing message for "client roots exist but none lies
   inside the server roots".
3. Tests, including the mandatory write-into-`/app/telegram_mcp/*.py`
   scenario.
4. Tracked `Dockerfile` and `docker-compose.yml` hardening (code not writable
   by `appuser`, read-only root filesystem, explicit writable mounts).
5. Docs: README "File Path Security" and "Clients on another machine"
   wording, `docs/arch/overview.md` item 3 (File roots), compose comments.
6. A "Live deployment follow-up" note for the owner (below). The owner's
   `/Users/gronck/self/telegram-mcp/Dockerfile.local` and
   `docker-compose.override.yml` are not edited by this task.

## Out of scope

- Authentication of the SSE/HTTP transport (a separate, larger decision).
- Changing behaviour when `SERVER_ALLOWED_ROOTS` is empty (see decision 2).
- A built-in deny list of sensitive paths (`/proc`, `/etc`, the code
  directory) and extension limits for writes.
- Changing `prepare_upload`, signed URLs, `media_links.py`, `singleton.py`.
- Editing or restarting the live deployment.

## Behaviour (decision 1, below)

Let `S = SERVER_ALLOWED_ROOTS` (resolved with `strict=True` at startup) and
`C` = the client's roots after `_coerce_root_uri_to_path` /
bare-path recovery (resolved with `strict=True`; unresolvable entries are
already dropped).

- `S` empty: unchanged (client roots decide, or the existing deny-all /
  not-configured statuses). Residual risk, see decision 2.
- `S` non-empty and the client returned a usable `C`: effective roots =
  `[c for c in C if c is equal to or under some s in S]`, in the client's
  order, deduped.
  - A client root that is an ancestor of a server root (e.g. `file:///` or
    `/home/user` when `S = [/home/user/downloads]`) is dropped, not clipped
    to the server root. A client never gets anything it did not ask for in
    the form it asked, and an over-broad client declaration is treated as
    not usable (decision 1a).
  - A client root that is a file counts when it equals, or lies under, a
    server root; a server root that is a file admits only that exact file.
    This is the existing `_path_is_within_root` rule.
  - Symlinks: both sides are `resolve(strict=True)` realpaths, so a client
    root that is a symlink pointing outside `S` is dropped.
  - Windows: `file:///C:/x` is converted by `_coerce_root_uri_to_path`
    (existing code), and `Path` comparison is case-insensitive per drive;
    a client root on another drive than every server root is dropped. Not
    testable on the dev machine (posix); covered by a unit test that fakes
    the pure-path comparison where practical, otherwise stated as unverified
    in the review.
- Empty intersection (client sent roots, none inside `S`): deny-all with a new
  status `ROOTS_STATUS_CLIENT_OUTSIDE_SERVER`
  (`"client_outside_server_roots"`). No fallback to `S`, regardless of
  `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK`: a client that declared roots must
  never end up with more than it asked for.
- Interplay with the existing switches:
  - `TELEGRAM_SERVER_ROOTS_ONLY=1` with `S` non-empty: unchanged
    (`ROOTS_STATUS_SERVER_ONLY`, `roots/list` skipped).
  - `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1`: still applies only when the
    client's list is empty or `list_roots` times out or fails (as today). It
    does not apply to a non-empty client list whose roots are all outside `S`.
  - Client without roots support (`ROOTS_STATUS_UNSUPPORTED_FALLBACK`):
    unchanged, `S` is used.
- Status values and messages (`_ensure_allowed_roots`): existing statuses
  and texts stay byte-identical. New text for the new status, recorded as
  `invalid_argument` like the others: `<tool> is disabled because none of the
  client's MCP Roots lies inside the server's allowed roots
  (TELEGRAM_ALLOWED_ROOTS). Narrow the client roots to a server root or add
  the folder to the server roots.` The text does not list server paths. A
  server-side warning log names the dropped client roots.
- Relative paths resolve under the first effective (narrowed) root, in client
  order, via the existing `_first_resolution_root`.

## Container hardening

Tracked `Dockerfile` (production stage) and `docker-compose.yml`:

- `/app` and everything under it owned by root, not writable by `appuser`;
  only explicitly created data directories are owned by `appuser`
  (`/app/data/transcripts`, a file-tools root, a state directory).
- `docker-compose.yml`: `read_only: true`, `tmpfs` for `/tmp`, and writable
  mounts for every runtime write path. Enumerated from the code (the
  implementer re-greps to confirm before finishing):

| Write path | Code | Handling |
|---|---|---|
| `/app/data/transcripts` (SQLite transcript cache; `cache_dir()` default `data/transcripts` relative to WORKDIR) | `transcription.py:258-278` | existing bind mount `./transcript_cache` |
| `/tmp/telegram-mcp-locks` (single-instance session lock) | `singleton.py:107,121` | tmpfs `/tmp` |
| `/tmp/telegram-mcp-session-locks` | `runtime.py:886-893` | tmpfs `/tmp` |
| `/app/mcp_errors.log` (error log, WORKDIR) | `runtime.py:1217-1243` | read-only fs makes the handler fall back to console logging (code already handles it); `docker logs` keeps the errors. No code change |
| `/app/telegram_audit.log` (deletion audit log, failure is silent) | `audit.py:20-25` | image `ENV TELEGRAM_AUDIT_LOG=/home/appuser/.local/state/telegram-mcp/audit.log` (state volume), so plain `docker run` without compose keeps the audit too; compose sets the same value |
| `~/.local/state/telegram-mcp/...` alias store | `runtime.py:2500-2610` | state volume (`/home/appuser/.local/state`) |
| `~/.local/state/telegram-mcp/...` incoming event feed | `tools/events.py:121-156` | same state volume |
| resolve_phone limiter state | `resolve_phone_limit.py:51-174` (`TELEGRAM_RESOLVE_PHONE_STATE_FILE`, default under state dir) | same state volume |
| `~/.cache/telegram-mcp/transcripts` (fallback if `data/transcripts` is not creatable) | `transcription.py:272` | not reached with the mount; no extra mount |
| `.session` file for `TELEGRAM_SESSION_NAME` accounts | `runtime.py` (`_resolve_session_path`, `_add_default_file_session`) | Telethon creates the file while constructing the client. Image `ENV TELEGRAM_SESSION_NAME=/home/appuser/.local/state/telegram-mcp/telegram_mcp_session` (state volume, dir created and owned by appuser); `.env.example` no longer presets a relative name (an env_file value would override the image value). If the file cannot be created and labelled accounts exist, the optional default account is skipped with a constant warning instead of crashing (base had a writable `/app`, so this never failed); a required default (no labelled accounts) still raises `sqlite3.OperationalError` |
| `__pycache__` | n/a | already `PYTHONDONTWRITEBYTECODE=1` |
| Whisper model dir (`TELEGRAM_TRANSCRIBE_WHISPER_MODEL_DIR`), temp dirs of `download_media` (`.telegram-mcp-download-*` next to the target) | `transcription.py:571`, `media.py:578` | follow their configured/allowed roots; only needed when enabled |
| File-tools root (downloads, `/upload/`, `prepare_upload`) | `media.py:1281-1340` | new server root + mount (decision 3) |

## Acceptance

- With `S = [root]` and a client answering `file:///` (or any ancestor of
  `root`, or a sibling directory), the effective roots exclude it: a write
  tool targeting a file under a code-like directory outside `root` (the
  `/app/telegram_mcp/*.py` scenario, reproduced with a tmp directory standing in
  for `/app`) is refused with "Path is outside allowed roots." or the new
  disabled message, and nothing is written. A read tool is refused likewise.
- A client root equal to or under a server root works and is the only
  effective root; ancestors are dropped, mixed lists keep the valid ones;
  an all-outside list is deny-all with the new status even with
  `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1`.
- `S` empty: every existing behaviour and test unchanged.
- `TELEGRAM_SERVER_ROOTS_ONLY`, empty-list deny-all, timeout, error,
  unsupported-roots and bare-path recovery behave as before, except that
  recovered bare paths are narrowed too when `S` is non-empty.
- Symlinked client root pointing outside `S` is dropped.
- Production image: `/app` and `/app/telegram_mcp` not writable by `appuser`;
  compose has `read_only: true`, `tmpfs` for `/tmp`, mounts for every row of
  the table that persists state; no `chown -R appuser:appuser /app`.
- README and overview describe the new rule (README.md:754 and :767 rewritten;
  "Clients on another machine" no longer relies on client roots).
- Tests use stub `ctx.session.list_roots`, never real Telegram. Existing
  tests pass; gates per CLAUDE.md pass; coverage gate holds.
- Deep review is mandatory (security-shaped paths: `runtime.py`,
  `Dockerfile*`, `docker-compose*`). It must confirm the narrowing is applied
  on every return path that carries client-derived roots.

## Live deployment follow-up (owner, not part of this task)

`/Users/gronck/self/telegram-mcp` runs untracked `Dockerfile.local` and
`docker-compose.override.yml`. After this task is merged and synced there,
make the equivalent changes by hand (rebuild and restart are the owner's call):

1. `Dockerfile.local`: remove `chown -R appuser:appuser /app`; chown only the
   data directories (`/app/data`, the file root, the state dir). Also set, as in
   the tracked Dockerfile, `ENV TELEGRAM_SESSION_NAME=/home/appuser/.local/state/telegram-mcp/telegram_mcp_session`
   and `ENV TELEGRAM_AUDIT_LOG=/home/appuser/.local/state/telegram-mcp/audit.log`
   (a relative session name would be created in the read-only `/app`), and make
   sure the owner's `.env` does not set a relative `TELEGRAM_SESSION_NAME`.
2. `docker-compose.override.yml`: `read_only: true`, `tmpfs: [/tmp]`, a state
   volume at `/home/appuser/.local/state`, `TELEGRAM_AUDIT_LOG` on it, the
   existing transcript mount, and a `TELEGRAM_ALLOWED_ROOTS` server root with
   its mount. Without a server root the narrowing does nothing (decision 2).
3. Until then the hole stays open on the live deployment: setting
   `TELEGRAM_ALLOWED_ROOTS` (or `TELEGRAM_SERVER_ROOTS_ONLY=1`) today already
   blocks it for the root case, because that mode skips `roots/list`.

## Owner decisions (coordinator decided 1-3 on the owner's behalf, 2026-11-10; the rest need an answer)

1. DECIDED. Client roots only narrow a non-empty `SERVER_ALLOWED_ROOTS`;
   empty intersection is deny-all without fallback to server roots.
   (1a) Client root that is an ancestor of a server root: dropped (chosen)
   versus clipped to the server root. Recommendation was dropped.
2. Server roots empty (open). Today client roots decide, so on a shared
   HTTP/SSE deployment without server roots any caller still gets any path.
   Options: (a) keep, document in README that shared SSE/HTTP deployments must
   set `TELEGRAM_ALLOWED_ROOTS` (optionally with `TELEGRAM_SERVER_ROOTS_ONLY`); (b) (a) plus a
   startup warning in `runner.py` when the transport is not stdio, no server
   roots are set and file tools are exposed; (c) refuse file tools over
   non-stdio transports without server roots (breaking). Recommendation: (b)
   if the owner accepts touching `runner.py` (security-shaped path, one log
   line, no behaviour change); otherwise (a). Not (c) now.
3. DECIDED. Image hardening in the tracked files plus mandatory deep review
   and the mandatory `/app/telegram_mcp/*.py` test; merge on green CI.
   Open sub-question 3a: should the tracked `docker-compose.yml` also set a
   default server root (`TELEGRAM_ALLOWED_ROOTS` defaulting to
   `/app/data/files` on a named volume), so the shipped compose file is
   protected by decision 1 and not only by the read-only filesystem?
   Recommendation: yes, `${TELEGRAM_ALLOWED_ROOTS:-/app/data/files}`, so
   anyone who sets it in `.env` overrides it. Without a server root decision 1
   does nothing for the shipped deployment.
4. Audit log and error log in the container. Options: (a) leave the error log
   console-only under a read-only root and move the audit log with
   `TELEGRAM_AUDIT_LOG` to the state volume (no code change); (b) make the
   error-log path configurable (edit to `runtime.py`). Recommendation: (a).
5. Hardening beyond this task: drop capabilities (`cap_drop: [ALL]`),
   `no-new-privileges`, a built-in path deny list. Recommendation: add
   `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` to compose
   only if the container test passes with them; defer the deny list.

## Decisions on the open questions (coordinator on the owner's behalf, 2026-11-10)

All five recommendations accepted:

1. Empty server roots: option (b), a one-line startup WARNING in
   `telegram_mcp/runner.py` for a non-stdio transport with no server roots and
   file tools exposed, plus README guidance. File tools are not disabled.
2. Shipped `docker-compose.yml` gets a default server root
   `TELEGRAM_ALLOWED_ROOTS=${TELEGRAM_ALLOWED_ROOTS:-/app/data/files}` on a
   named volume.
3. Error log stays console-only on a read-only rootfs; the audit log moves to
   the state volume via `TELEGRAM_AUDIT_LOG`; no code change for either.
4. `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` are added if
   the local container check (no session, app not started, port 8765 not
   published) passes with them; a built-in path deny list is deferred.
5. ADR "Server roots are the ceiling for client MCP Roots" is written in this
   branch.

The non-URI roots recovery branch is narrowed the same way (no bypass).

Additional checks required:

- (a) Nothing under `/app/data` (or any other writable path: the state
  volume, `/tmp`, `/home/appuser`) is ever imported or executed: no `.pth`
  files, plugin directories or bytecode caches are loaded from writable
  locations; `sys.path` at runtime contains no writable directory. Add a test
  or a documented check.
- (b) Bytecode cannot be swapped: `PYTHONDONTWRITEBYTECODE=1` in the
  production image, `/app` (including any `__pycache__`) root-owned and not
  writable by appuser. The live image currently contains host-built
  `__pycache__/*.pyc` because there is no `.dockerignore` and
  `COPY telegram_mcp` copies them; add a `.dockerignore` that excludes
  `__pycache__/`, `*.pyc`, `.env*`, `*.session*`, `.venv/`, `.git/` and test
  artefacts, so the image never carries host bytecode or secrets.

## ADR flag

Yes, probably. This changes the documented trust model of file roots (client
roots stop being authoritative when the operator configures server roots).
Per the contract an ADR is created by the main thread, not by this step:
suggest an ADR "Server roots are the ceiling for client MCP Roots", written
after the owner confirms the decisions above.
