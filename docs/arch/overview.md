# Architecture overview

Current shape of the fork (`ours` = upstream `main` + fork-only changes).
Keep this file up to date when a change alters it.

## Process

- `main.py` — compatibility shim re-exporting `telegram_mcp` (historic import
  paths, e.g. `main.ALLOWED_CHAT_IDS`).
- `telegram_mcp/runner.py` — startup: connects every configured account,
  warms entity caches in the background, configures HTTP transport security
  and serves stdio, streamable HTTP (`/mcp`) or legacy SSE (`/sse`).
- `telegram_mcp/runtime.py` — shared infrastructure: the `FastMCP` instance,
  account discovery and multi-account routing (`with_account`), connection
  and session locking, entity resolution, `validate_id`, chat allowlists,
  file-path security, error formatting.
- `telegram_mcp/tools/` — one module per domain (accounts, chats, contacts,
  custom_emoji, events, folders, groups, media, messages, profile, stickers);
  importing a module registers its `@mcp.tool` functions.
- `telegram_mcp/transport/` — MCP protocol-version negotiation, HTTP
  middleware (Origin checks, CORS, discovery probe), stdio wrapper.
- Supporting modules: `sanitize.py` (prompt-injection hygiene for returned
  text), `transcription.py` (voice transcription engines + SQLite cache),
  `media_links.py` (signed media/upload URLs), `audit.py` (deletion audit
  log), `singleton.py` (per-host session lock), `install_guard.py`.

## Access control layers

1. **Exposed tools** — `TELEGRAM_EXPOSED_TOOLS` (`all`, `read-only`, `+`/`-`
   adjustments), driven by `ToolAnnotations.readOnlyHint`.
2. **Chat allowlists** — `TELEGRAM_ALLOWED_CHAT_IDS` (read + write),
   `TELEGRAM_ALLOWED_READ_CHATS`, `TELEGRAM_ALLOWED_WRITE_CHATS` (fork-only,
   #116). Marked IDs only, fail closed once any list is set; a tool's mode
   comes from `readOnlyHint` (see `chat_access_for_tool`).
3. **File roots** — file tools are disabled until allowed roots come from the
   client Roots API or server CLI arguments.
4. **HTTP transport security** — Host/Origin validation always on
   (localhost by default, `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`).

## Deployment (owner's machine)

Docker from `/Users/gronck/self/telegram-mcp` on branch `ours`, with
untracked `Dockerfile.local` and `docker-compose.override.yml`: SSE transport,
published on 127.0.0.1:8765, reachable by sibling containers on the external
network `telegram-mcp-net` as `telegram-mcp:8765`.
