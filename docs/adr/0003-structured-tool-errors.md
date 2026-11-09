# 0003. Structured tool errors travel in `_meta`, opt-in

- **Status:** accepted
- **Date:** 2026-11-10

## Context

Most tool failures were returned as ordinary successful results whose text says
"An error occurred ...". A client (the owner's jeeves agents, reading the SSE
server) had to parse prose to tell success from failure, and flood waits had no
machine-readable retry delay. Existing consumers parse both success and error
text, so neither may change. Setting `isError` changes how some LLM clients
treat a result (retry, truncate, hide), so it cannot be switched on silently for
every consumer of the live deployment.

## Decision

- **Channel:** the `CallToolResult` `_meta` field, key `telegram-mcp/error`,
  plus the standard `isError` flag. Set in one place, the call hook in
  `_install_annotation_hook`, so none of the ~190 call sites changes and the
  text stays byte-for-byte identical.
- **Recorded, not parsed:** `log_and_format_error` classifies the exception and
  records it in a per-call recorder; paths that bypass the formatter call
  `record_tool_error`. A result is an error only because the server recorded
  one, never because its text looks like one.
- **Shape:** `{kind, retry_after?, rpc_error?, needs_user?, accounts?}`.
  Kinds: `invalid_argument`, `not_found`, `private`, `forbidden_allowlist`,
  `flood`, `timeout`, `internal`. `retry_after` (int seconds) only for `flood`.
  `rpc_error` is Telegram's constant error name or the Telethon class name
  (for example `CHAT_ADMIN_REQUIRED` is kind `private`). `needs_user: true`
  marks the ask-the-user alias prompt (kind `invalid_argument`). `_meta`
  carries only these fixed names, ints and account labels: no peer identifiers,
  Telegram content or caller-supplied strings.
- **Fan-out:** a read-only tool run over all accounts is `isError` only if
  every account failed; `accounts` lists each failing account's details either
  way.
- **Opt-in:** `TELEGRAM_STRUCTURED_ERRORS=1`, one switch for both `isError` and
  `_meta`. Default off; with it off nothing is recorded and results are
  unchanged. Enabling it on the live deployment is the owner's call. To flip
  the default later: confirm every consumer tolerates `isError` results, make
  the unset value mean on (keeping `0` as an opt-out for one release), update
  README and CLAUDE.md, and record the change in a new ADR.

## Consequences

- Consumers opt in without text parsing changes; text-only clients are
  unaffected either way.
- New error sources must either go through `log_and_format_error` or call
  `record_tool_error`, or they stay plain successes.
- Resolver not-found and malformed-reference failures use dedicated
  `ValueError` subclasses so classification stays type-based.

## Rejected

- **Text prefix** (for example `[flood] ...`): changes the text contract that
  existing parsers and tests rely on.
- **`structuredContent`:** bound to the tool's generated `outputSchema`
  (`-> str` wrapper); extra keys can fail client validation.
- **Raising `ToolError` everywhere:** FastMCP prefixes "Error executing tool",
  changing every error text, and needs edits at every call site.
