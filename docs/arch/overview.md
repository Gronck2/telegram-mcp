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
   comes from `readOnlyHint` (see `chat_access_for_tool`), except
   `READ_ACCESS_TOOLS` (read) and `BOTH_ACCESS_TOOLS` (`press_inline_button`,
   `vote_poll`: read and write). A non-blank value with no valid entry denies
   all; `@*` is not the wildcard. Entries that are not numeric ids, `*`,
   usernames or phone numbers (free text, invite links, malformed handles)
   are parsed to an inert entry that matches nothing, with a stderr warning at
   startup (`_parse_chat_id_token`, task 0005). While a list is set, invite joins resolve the
   invite first and need write access to its chat (plain invites are refused).
   Tools that name a user rather than a chat are covered by peer helpers in
   `runtime.py` (ADR 0002): `validate_id(..., peer_subject=True)` /
   `require_peer_access` check the user as their private chat in the tool's
   mode; contact-book listings are refused while read access is restricted
   (`refuse_if_read_restricted`, read list without `*`); lookups outside read
   access return `public_view` (id, type, name, usernames, bot flag, badges).
   Username entries match the entity's primary username and its active
   `usernames` entries (never inactive ones; without `usernames` only the id
   and primary username can match). Lookup tools also pass the @username they
   were given (`peer_allowed(..., identifier=)`); it is accepted only if
   Telethon's `parse_username` takes it as a real handle (never alias wording)
   and the resolved entity holds it as primary or active secondary username,
   so it adds nothing beyond the entity match (defence in depth). A released
   username can be claimed by someone else, so prefer numeric ids in the
   allowlist.
   Member `user_id` parameters of group-admin tools are deliberately not
   checked. `tests/test_peer_allowlist.py` fails on a registered tool with a
   peer-naming parameter that is not classified.
   Resolver guard (task 0004): while a list is set, `resolve_input_entity`
   resolves a username that is not in the session cache through
   `ResolveUsernameRequest` instead of Telethon's `get_input_entity`, which would
   answer from a cached chat title/display name; `_resolve` refuses handle-shaped
   strings that are no valid username (`abcde_`); `get_participants`,
   `get_recent_actions` and `send_typing` use the guarded or already checked peer.
   Invariant: `resolve_input_entity(s)` names the same peer as `resolve_entity(s)`;
   the cache answers directly only for own-account refs and username/phone row hits,
   everything else (invites, links, misses) goes through `get_entity`. `@ name` and
   `tg://` forms are refused like other malformed handles under a list. In every
   mode (task 0006 round 4) `_resolve` turns a digit string (`-?\d+`) into an int
   before any lookup, so it is never a phone/name lookup; a value outside int64
   (or beyond `int()`'s 4300 digits) is a `ValueError` ("The id is out of the valid
   id range."), never a `struct.error` from Telethon. `get_participants` and
   `get_recent_actions` always resolve the peer through `resolve_input_entity`,
   with or without a list, so a raw `+digits` string never reaches Telethon.
   Phones and self (task 0005, round 4; task 0006 round 2 extends it to the
   no-allowlist case, so `_lookup_callable` routes phones through `_phone_lookup`
   with or without a list, costing at most one `GetContactsRequest` and only for
   phone strings): a string that `utils.parse_phone` accepts is resolved only by
   `_phone_lookup` (session
   phone-row hit, else a `GetContactsRequest` match on `user.phone`), in both
   resolvers; a miss is refused, never handed to `session.get_input_entity`,
   whose phone-row-miss fallback matches a cached chat titled like the phone
   (`client.get_entity` is pinned too, defence in depth; Telethon 1.45 raises
   there on a contacts miss). Entries `self`, `@self`, `t.me/self` are inert with a warning and
   a self-ref caller string is never granted by string (use the own numeric id,
   ADR 0002). `validate_id` (task 0006) converts a string to an int only when it
   fully matches `-?\d+`; a `+`-led string `parse_phone` accepts stays a string, so
   `"+15551234567"` is never read as user/channel id 15551234567.
   `set_contact_alias` refuses every key Telethon reads as a
   username (`is_unshadowable_identifier`, incl. 3-4 char Latin nicknames) in
   both modes with `alias_shadows_real_identifier`; under a list, legacy aliases
   of that shape are skipped by `apply_alias`.
   Without a list, usernames, aliases, `me` and free-text titles keep Telethon's
   own lookup; digit strings (ints) and phone strings (`_phone_lookup`) are
   handled as described above.
3. **File roots** — file tools are disabled until allowed roots come from the
   client Roots API or server CLI arguments.
4. **HTTP transport security** — Host/Origin validation always on
   (localhost by default, `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`); the
   Origin check wraps both the streamable HTTP and the SSE app (`/media/`,
   `/upload/` included).

## Error model

Errors are recorded where they are created, never inferred from result text.
`log_and_format_error` classifies the exception (`classify_error`, types and
constant Telegram error names only) and appends the result to a per-call
recorder (`ContextVar` holding a shared list, so `wait_for` and the fan-out
`gather` tasks all write to it). `with_account` and the few bare-string
refusals in tools (early returns, with or without an "Error:" prefix, and the file-path
security helpers) record explicitly with `record_tool_error`; RPC `except` handlers record
`classify_error(e)`. Deliberate gaps, left as plain successes: the `premium_required_result`
JSON refusals, `set_contact_alias` `saved: false` payloads, empty results, idempotent no-ops
and per-item errors. Unknown RPC errors fall back by base class (400 invalid_argument, 403
private, FloodError flood, else internal). A failure nothing recorded (escaped exception, raw
`ToolError`) is classified from its cause by a wrapper around the tool manager's `call_tool`
(`_install_unrecorded_failure_hook`); FastMCP's own rejections are `invalid_argument`. The call hook in
`_install_annotation_hook` applies the record to the wire result.

With `TELEGRAM_STRUCTURED_ERRORS=1` a failed call gets `isError: true` and
`_meta["telegram-mcp/error"] = {kind, retry_after?, rpc_error?, needs_user?,
accounts?}`; text is untouched. Kinds: `invalid_argument`, `not_found`,
`private`, `forbidden_allowlist`, `flood`, `timeout`, `internal`. A read-only
fan-out is an error only if every account failed. With the switch off nothing
is recorded and results are byte-for-byte as before. Rationale and the plan to
flip the default: `docs/adr/0003-structured-tool-errors.md`.

## Deployment (owner's machine)

Docker from `/Users/gronck/self/telegram-mcp` on branch `ours`, with
untracked `Dockerfile.local` and `docker-compose.override.yml`: SSE transport,
published on 127.0.0.1:8765, reachable by sibling containers on the external
network `telegram-mcp-net` as `telegram-mcp:8765`.
