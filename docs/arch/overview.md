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
   everything else (invites, links, misses) goes through `get_entity`. Numeric id
   strings become ints (never a phone/name lookup); `@ name` and `tg://` forms are
   refused like other malformed handles.
   Phones and self (task 0005, round 4): under a list, a string that
   `utils.parse_phone` accepts is resolved only by `_phone_lookup` (session
   phone-row hit, else a `GetContactsRequest` match on `user.phone`), in both
   resolvers; a miss is refused, never handed to `client.get_entity` or
   `session.get_input_entity`, whose fallback matches a cached chat titled like
   the phone. Entries `self`, `@self`, `t.me/self` are inert with a warning and
   a self-ref caller string is never granted by string (use the own numeric id,
   ADR 0002). `set_contact_alias` refuses every key Telethon reads as a
   username (`is_unshadowable_identifier`, incl. 3-4 char Latin nicknames) in
   both modes with `alias_shadows_real_identifier`; under a list, legacy aliases
   of that shape are skipped by `apply_alias`.
   Without a list nothing changes.
3. **File roots** — file tools are disabled until allowed roots come from the
   client Roots API or server CLI arguments.
4. **HTTP transport security** — Host/Origin validation always on
   (localhost by default, `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`); the
   Origin check wraps both the streamable HTTP and the SSE app (`/media/`,
   `/upload/` included).

## Deployment (owner's machine)

Docker from `/Users/gronck/self/telegram-mcp` on branch `ours`, with
untracked `Dockerfile.local` and `docker-compose.override.yml`: SSE transport,
published on 127.0.0.1:8765, reachable by sibling containers on the external
network `telegram-mcp-net` as `telegram-mcp:8765`.
