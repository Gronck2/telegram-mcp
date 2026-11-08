# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the server
uv run main.py

# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/test_sanitize.py

# Run tests with coverage (80% minimum gate for core modules)
uv run pytest --cov --cov-report=term-missing --cov-report=xml

# Format and lint
uv run black --check .
uv run flake8 .

# Generate a Telegram session string (required before first run)
uv run session_string_generator.py
uv run session_string_generator.py --qr    # QR-code login
uv run session_string_generator.py --phone # Phone + code login
```

## Architecture

This is an MCP server that exposes Telegram operations as tools via [FastMCP](https://github.com/jlowin/fastmcp) backed by [Telethon](https://docs.telethon.dev/).

**Entry points:**
- `main.py` — compatibility shim that re-exports from `telegram_mcp` and keeps the historic import paths working
- `telegram_mcp/runner.py` — real startup: connects all accounts, warms entity caches, starts stdio or SSE transport
- `telegram_mcp/runtime.py` — all shared infrastructure: account discovery, multi-account routing, connection management, file-path security, entity resolution, validation decorators, error handling

**Tool registration:** `telegram_mcp/tools/` has one module per domain. Importing these modules causes `@mcp.tool()` decorators to register the tools with the shared `FastMCP` instance defined in `runtime.py`. `telegram_mcp/tools/__init__.py` imports all of them.

**Sanitization:** Every user-controlled string returned in tool results goes through `sanitize.py` before inclusion. This prevents prompt injection from crafted Telegram content. Use `sanitize_user_content()` for message text, `sanitize_name()` for display names/usernames, `format_tool_result()` for structured JSON output.

**Multi-account routing:** `with_account(readonly=False)` decorator handles fan-out. Read-only tools (`readonly=True`) fan out to all accounts when `account` is omitted; write tools require an explicit `account`. The `clients` dict (label → `TelegramClient`) is built at import time from `TELEGRAM_SESSION_STRING_<LABEL>` env vars.

**Entity resolution:** `resolve_entity()` and `resolve_input_entity()` in `runtime.py` handle `StringSession`'s cold cache by calling `get_dialogs()` on miss, and also try Telethon-marked ID variants (`-1000000000000 - id` for channels, `-id` for basic groups) for bare positive integer IDs.

**File-path security:** File tools (`send_file`, `download_media`, etc.) are disabled until allowed roots are configured. Roots come from MCP client Roots API (takes priority) or CLI positional arguments (`uv run main.py /allowed/path`). Empty client Roots are deny-all by default; set `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1` to fall back to server CLI roots instead.

**Transport:** Default is stdio. Set `MCP_TRANSPORT=sse` with optional `MCP_HOST` and `MCP_PORT` (default `127.0.0.1:8765`) to run as an SSE server instead — useful when sharing one Telethon session across multiple local MCP clients.

**Install guard:** `telegram_mcp/install_guard.py` aborts startup if the installed `telegram-mcp` distribution doesn't trace back to a cloned checkout or explicit git/file install. This defends against the unrelated `telegram-mcp` PyPI package accidentally receiving credentials.

## Key environment variables

| Variable | Purpose |
|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Credentials from my.telegram.org/apps |
| `TELEGRAM_SESSION_STRING` | Single-account session |
| `TELEGRAM_SESSION_STRING_<LABEL>` | Multi-account session (label lowercased in tool `account` param) |
| `TELEGRAM_EXPOSED_TOOLS` | `all` (default) or `read-only` |
| `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK` | `1` to allow server CLI roots when client Roots is empty |
| `TELEGRAM_DEVICE_MODEL`, `TELEGRAM_SYSTEM_VERSION`, `TELEGRAM_APP_VERSION` | Device name shown in Telegram Settings > Devices |
| `TELEGRAM_PROXY_TYPE` | `socks5`, `socks4`, `http`, or `mtproxy` |
| `MCP_TRANSPORT` | `stdio` (default) or `sse` |
| `MCP_HOST`, `MCP_PORT` | SSE server bind address (default `127.0.0.1:8765`) |

Proxy and session variables accept a `_<LABEL>` suffix for per-account overrides.

## Adding a new tool

1. Add the function to the appropriate module in `telegram_mcp/tools/` (or create a new one and import it in `telegram_mcp/tools/__init__.py`).
2. Decorate with `@mcp.tool()`. For read-only tools, add `annotations=ToolAnnotations(readOnlyHint=True)`.
3. Add `@with_account(readonly=True/False)` for multi-account support.
4. Add `@validate_id("chat_id", ...)` for any ID parameters.
5. Call `await ensure_connected(get_client(account))` at the top, use `resolve_entity()` for entity lookups.
6. Sanitize all user-controlled strings before returning them.

## Test structure

Tests live in `tests/`. They use stub sessions to avoid real Telegram connections (`conftest.py` sets dummy credentials). Coverage is measured only over `main`, `sanitize`, and `telegram_mcp.runtime` — tool modules are live API adapters excluded from the unit coverage gate.

## Agent pipeline

Non-trivial work goes through the `dev-pipeline` plugin (`/dev-pipeline:run-task`);
project specifics are the Pipeline contract below. Do not add same-named
`.claude/agents/` copies unless a deliberate override is needed.

This is the **Gronck2/telegram-mcp fork**. The branch `ours` carries fork-only
work on top of upstream `chigwell/telegram-mcp` `main`; nothing on `ours`
(including this file and `docs/`) is ever proposed upstream. Fixes meant for
upstream go on a separate branch from `origin/main` and a PR to chigwell only
when the owner asks for it.

## Pipeline contract

- **Base branch:** `ours`
- **PR target:** `ours` on `Gronck2/telegram-mcp` — never `chigwell/telegram-mcp`.
- **Forge:** gh. Remotes: `origin` = upstream chigwell/telegram-mcp (read-only
  for us), `fork` = Gronck2/telegram-mcp (push here). `gh repo set-default
  Gronck2/telegram-mcp`; still pass it explicitly:
  `gh pr create --repo Gronck2/telegram-mcp --base ours --head <branch>`.
- **Branch naming:** task/NNNN-slug; docs-only work on docs/<slug>; upstream
  sync on sync/upstream-YYYYMMDD.
- **Docs layout:** tasks docs/tasks/, plans docs/plan/ (shared NNNN
  numbering), ADRs docs/adr/ (immutable), living docs: docs/arch/overview.md.
  Docs language: English.
- **Test paths:** `tests/`
- **Gate commands:**
  - lint: `uv run flake8 --select=E9,F63,F7,F82 <touched files>` (blocking,
    same selection as CI); `uv run flake8 <touched files>` must add no new
    warnings compared with the base branch (the rest of the codebase has
    known C901/E501 hits; CI runs it with --exit-zero).
  - format: `uv run black --check <touched files>`
  - tests: `uv run pytest --cov --cov-report=term-missing` (coverage gate
    `fail_under = 80` from pyproject.toml)
- **Security-shaped paths:** `telegram_mcp/runtime.py` (chat allowlists,
  `validate_id`, file-path security, `with_account`), `telegram_mcp/transport/`,
  `telegram_mcp/runner.py`, `telegram_mcp/media_links.py` (signed URLs),
  `telegram_mcp/audit.py`, `telegram_mcp/install_guard.py`,
  `telegram_mcp/singleton.py`, `sanitize.py`, `session_string_generator.py`,
  `migrate_session.py`, `.env*`, `Dockerfile*`, `docker-compose*`,
  `.github/workflows/`.
- **Merge/release policy:** PR into `ours` opened without asking; merging,
  direct pushes to `ours`, force-push, tags and upstream syncs only on the
  owner's explicit request; never propose merging a PR whose CI is red or
  pending. Upstream sync: branch `sync/upstream-YYYYMMDD` from `ours`,
  `git merge origin/main`, PR into `ours` (no rebase, no force-push of `ours`).
- **Extra rules:**
  - Fork-only: never open PRs or push to `origin` (chigwell) from pipeline work.
  - Secrets: never print `.env`, session strings or API hashes; never run
    `docker compose config` unfiltered (it expands `.env`).
  - The live deployment runs from `/Users/gronck/self/telegram-mcp` (branch
    `ours`, untracked `Dockerfile.local` + `docker-compose.override.yml`,
    container `telegram-mcp`, SSE on 127.0.0.1:8765, network
    `telegram-mcp-net`). Don't switch branches there, restart, rebuild or stop
    that container unless the owner asks. Port 8765 is reserved for it.
  - Never start a second process/container with the live session string:
    Telegram revokes auth keys used from two places at once.
  - Tests never hit real Telegram (`tests/conftest.py` sets dummy
    credentials); new tools follow "Adding a new tool" above, including
    `ToolAnnotations(readOnlyHint=True)` for read-only tools — the chat
    allowlists' read/write split and `TELEGRAM_EXPOSED_TOOLS=read-only` both
    depend on it.
  - Every user-controlled string returned by a tool goes through `sanitize.py`.
