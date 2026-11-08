# 0001 Fix chat allowlist / HTTP transport security review findings

## Goal

Close six findings from the security review of commits 8ddc31c ("harden chat
allowlist ID matching and HTTP origin checks") and 8094517 ("separate read and
write chat allowlists"). The owner approved doing all six in one task.

## Why

The allowlists are the privacy boundary between the agent and the owner's
Telegram account. Two medium findings let the agent read or join chats outside
the allowlist; the low/info findings are fail-open or inconsistent edge cases
in the same code.

## Scope

Security-shaped paths: `telegram_mcp/runtime.py`, `telegram_mcp/transport/`,
`telegram_mcp/tools/messages.py`, `telegram_mcp/tools/groups.py`, README.
This task touches security-shaped code, so a security review of the diff is
expected before merge.

1. (medium) `press_inline_button` needs read AND write access.
   - Today it has `readOnlyHint` unset, so `chat_access_for_tool` maps it to
     write-only. It also reads the chat: it scans the last 20 messages for a
     keyboard, a wrong `button_text` returns "Available buttons: [0] ...", URL
     and copy buttons return their URL / `copy_text`, and the bot's callback
     answer is returned. With `TELEGRAM_ALLOWED_WRITE_CHATS=<bot>` and no read
     access this leaks chat content.
   - Decision (made): require both modes, as `vote_poll` does, enforced in
     `validate_id` before any Telegram request. Implement as a "both" tool set
     (e.g. `BOTH_ACCESS_TOOLS`) consulted by `chat_access_for_tool`, returning
     `CHAT_ACCESS_BOTH`.
2. (medium) `join_chat_by_link` and `import_chat_invite` (groups.py) never
   check the allowlist; with any allowlist set the agent can join any chat.
   - Decision (made): when a chat allowlist is enabled
     (`is_chat_allowlist_enabled()`), first resolve the target via
     `messages.CheckChatInviteRequest`. `ChatInviteAlready` and `ChatInvitePeek`
     carry the chat, so check write access on that chat. A plain `ChatInvite`
     has no chat id, so refuse. Only then call `ImportChatInviteRequest`.
     Refuse with the standard privacy-policy error (`check_chat_access`
     message). No allowlist configured: behaviour unchanged (no extra
     `CheckChatInviteRequest` call required).
3. (low) `_parse_allowed_chat_ids`: only a bare `*` token is the wildcard.
   `@*` currently strips to `*` and becomes a wildcard; it must not. Treat it
   as a non-matching entry (it is still a "valid entry" for item 4's
   purposes, or at minimum must not become the wildcard; coder chooses the
   simplest consistent handling and documents it in a test). README: document
   that `*` now means "every chat" also in `TELEGRAM_ALLOWED_CHAT_IDS` (a
   literal `*` matched nothing before 8094517).
4. (low) A chat allowlist variable set to a non-empty value that parses to no
   valid entries (`@`, `,`, `@,@`) counts as unset and removes all
   restriction (fails open on a typo).
   - Decision (made): fail closed. A non-empty, non-whitespace raw value with
     no valid entries means deny-all (allowlist enabled, empty set) for
     `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ALLOWED_READ_CHATS` and
     `TELEGRAM_ALLOWED_WRITE_CHATS`. Empty or whitespace-only still means
     unset (`manifest.json` passes "" for blank fields).
   - `runtime.ALLOWED_CHAT_IDS` (module attribute, monkeypatched by
     `tests/main.py`) must keep working, including `get_effective_allowed_chat_ids`
     precedence of env over the attribute.
5. (low) `get_marked_id` (runtime.py) maps `ChatEmpty` to the bare id (user
   namespace). Map it to `-id` like `Chat` / `ChatForbidden`.
6. (info) `ProtocolNegotiationMiddleware` (`telegram_mcp/transport/http.py`)
   wraps only the streamable HTTP app. Under `MCP_TRANSPORT=sse` (the owner's
   live mode) the custom `/media/` and `/upload/` routes added by FastMCP's
   `sse_app` get no Origin check (`/sse` and `/messages/` are covered by
   FastMCP's own Host/Origin validation).
   - Preferred: apply the same Origin check to the SSE app. The coder must
     verify that the `/sse` stream and `/messages/` POST still work for
     no-Origin clients and same-host clients, and that the live setup in
     CLAUDE.md "Extra rules" (sibling containers reaching `telegram-mcp:8765`
     via that Host header) keeps working.
   - Fallback only if wrapping is unsafe (breaks the SSE stream or the live
     setup): correct the docstring/README claim to say the Origin check covers
     streamable HTTP only. Record in the PR which path was taken and why.

## Out of scope

- `contacts` / `resolve_username` / `get_full_user` / `user_id`-style tools
  not being filtered by the allowlist (separate future task).
- Any change to which tools are read-only beyond `press_inline_button`.

## Acceptance

- `press_inline_button` with only a write allowlist entry for the chat is
  refused by `validate_id` before any Telegram call; with read+write it works;
  `chat_access_for_tool("press_inline_button", ...)` returns `both`.
- With an allowlist enabled: invite to an allowed chat (Already/Peek) joins;
  to a non-allowed chat or a plain `ChatInvite` is refused with the privacy
  error and `ImportChatInviteRequest` is never called; read-only-allowed chat
  is refused (needs write). Without allowlist: unchanged. Covers both tools.
- `@*` is not a wildcard; `*` still is; README updated.
- `@`, `,`, `@,@` in each of the three variables give deny-all; `""` and
  `"  "` stay unset; `runtime.ALLOWED_CHAT_IDS` monkeypatching still works.
- `get_marked_id(ChatEmpty(id=5)) == -5`.
- SSE-mode custom routes (`/media/`, `/upload/`) reject a disallowed Origin;
  `/sse` and `/messages/` still work for no-Origin and same-host clients (or
  the documented fallback applies).
- Tests for each item under `tests/` (extend `test_chat_allowlist.py`,
  `test_chat_access_modes.py`, `test_protocol_negotiation.py`,
  `test_runner.py` as fitting); no real Telegram. Gates from CLAUDE.md pass.
- `docs/arch/overview.md` and README updated where behaviour changed.

## Open owner decisions

None outstanding. Notes:

- Item 3: `@*` handling (ignored entry vs. non-matching literal entry) is an
  implementation detail; recommendation: keep it as a non-matching entry so
  `@*` alone is deny-all (consistent with item 4).
- ADR: not a significant architectural decision; no ADR flagged. (Fail-closed
  parsing is a behaviour change worth a README/changelog note.)
