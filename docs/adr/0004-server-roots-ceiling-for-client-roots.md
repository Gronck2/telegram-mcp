# 0004. Server roots are the ceiling for client MCP Roots

- **Status:** accepted
- **Date:** 2026-11-10

## Context

With server roots configured (`TELEGRAM_ALLOWED_ROOTS` / CLI arguments), a
client's non-empty `roots/list` answer used to replace them. The HTTP/SSE
transport has no authentication (only Host/Origin checks), so any local process
or container on the docker network could declare `file:///` and get the file
tools over the whole container filesystem: upload any readable file (for
example the plaintext transcript cache) and, worse, write Telegram-controlled
bytes into the server's own code (`download_media`, `export_*`), which the
image let `appuser` write. Code execution on the next restart would follow.

## Decision

- When `SERVER_ALLOWED_ROOTS` is non-empty, client roots (from `roots/list` and
  from the recovered bare paths) only narrow it: a client root equal to or under
  a server root is kept, in client order and deduplicated. Ancestors of a server
  root and symlinks resolving outside are dropped (not clipped).
- An empty intersection is deny-all with status `client_outside_server_roots`
  and its own message (recorded as `invalid_argument`). It never falls back to
  the server roots, whatever `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK` says: a
  client that declared roots must not end up with more than it asked for.
- Empty server roots: behaviour unchanged (client roots decide). A startup
  WARNING in `runner.py` flags a non-stdio transport with no server roots and
  file tools exposed; file tools are not disabled.
- The tracked image is hardened as a second layer: `/app` root-owned, only
  `/app/data` and the state directory owned by `appuser`, `.dockerignore`
  keeps host bytecode and secrets out, and `docker-compose.yml` runs
  `read_only: true` with explicit mounts, a default server root
  `/app/data/files`, `cap_drop: [ALL]` and `no-new-privileges`.
- Nothing under writable paths (`/app/data`, state volume, `/tmp`, home) is on
  `sys.path`; `PYTHONDONTWRITEBYTECODE=1` and a root-owned `/app` stop bytecode
  swapping.

## Consequences

- A client rooted at a parent of the server root must change its root, or the
  operator adds the folder to the server roots.
- Shared HTTP/SSE deployments without server roots keep the old exposure and
  must set `TELEGRAM_ALLOWED_ROOTS` or `TELEGRAM_SERVER_ROOTS_ONLY=1`.
- The error log (`/app/mcp_errors.log`) falls back to console output on the
  read-only root; the audit log goes to the state volume via
  `TELEGRAM_AUDIT_LOG`.
- The server-side warning for dropped client roots is a constant message
  without paths (log-privacy rule in `tests/test_error_logging.py`).
- Windows drive handling relies on existing `Path` semantics and was not run.
- A built-in path deny list and transport authentication remain future work.
