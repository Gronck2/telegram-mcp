# Plan 0004 Allowlisted handle must not resolve to a chat found by display name

Task: `docs/tasks/0004-allowlist-input-entity-fallback.md` (mechanism,
inventory and owner decisions are there). Separate commits per group. Assumes
the recommendations (central guard, allowlist-gated, refuse malformed
handles); step 3 changes if the owner picks differently.

## Approach

Fix the resolver, not the many callers. `resolve_input_entity` stops using
Telethon's `get_input_entity` for a username-shaped string whose username is
not in the session cache, because that call tries the display-name column
before the network. Instead it resolves with `resolve_entity`
(`ResolveUsernameRequest`, no name lookup for a valid username) and converts
with `utils.get_input_peer`. A cached username still goes through
`get_input_entity` (the cache hit comes first, names are never consulted).
Three tools that hand a raw string to Telethon are moved onto the guarded
path. Everything is gated by `is_chat_allowlist_enabled()`, so without an
allowlist the code path is byte-for-byte the old one.

## Steps

1. **Tests first** (`tests/test_peer_allowlist.py` or a new
   `tests/test_input_entity_guard.py`). Use a real, never-connected
   `TelegramClient(StringSession(), ...)` (dummy credentials from `conftest.py`),
   fill `client.session` via `process_entities` with a `Channel`/`User` whose
   title/first name is `foo` and no username, patch `client._call` (or
   `client.__call__`) to answer `ResolveUsernameRequest("foo")` with the real
   target, and stub `ensure_connected`. Cases:
   - allowlist `foo`, collision row, uncached real `@foo`: `resolve_input_entity("@foo")`
     returns the real peer (fails before the fix: returns the impostor, zero
     requests);
   - real `@foo` cached by primary username: returns it, `_call` never invoked;
   - secondary username (row has `username=None`): real peer, one request;
   - no allowlist: result is the impostor exactly as before, `get_entity` not
     called (pins "unchanged");
   - ints, `me`, `+phone`, saved alias: unchanged;
   - malformed handle `abcde_` under an allowlist: `ValueError`, also for
     `resolve_entity` (only if decision 3 = refuse);
   - tool-level (stubbed client): `get_participants`, `get_recent_actions`,
     `send_typing` use the real peer; one parametrised test over the
     `resolve_input_entity` tools (reactions, drafts, send-as, folders) is
     enough since they share the resolver.
2. **`resolve_input_entity` guard, `runtime.py`.** Add a small helper
   `_username_cached(client, identifier)`: `parse_username(identifier)` ->
   name; `client.session.get_entity_rows_by_username(name)`; any attribute error
   or exception counts as a miss (falls to the safe, network path). In
   `resolve_input_entity`, after `apply_alias`, when
   `is_chat_allowlist_enabled()` and `_is_username_identifier(identifier)` and
   not `_username_cached(...)`: `entity = await resolve_entity(identifier, client)`
   and return `utils.get_input_peer(entity)`. Keep the alias-failure wording
   (`_resolve` wraps it) by implementing this inside the existing `_resolve`
   getter plumbing (e.g. a getter name `"get_input_entity"` special case in
   `_resolve_with_retries`, or a thin wrapper passed as `getter`), so the
   cold-cache `get_dialogs()` retry and marked-id fallback keep working.
   Phones, ids, invite links, `me`/`self` are excluded by
   `_is_username_identifier`.
3. **Malformed handles (decision 3, refuse).** In `_resolve`, under an
   allowlist, for a `str` that `is_handle_like` accepts but that is not a
   numeric id, `me`/`self`, phone or valid username
   (`utils.parse_username` -> `(None, False)`), raise `ValueError("... is not
   a username or an id")`. Applies to both resolvers. Skip this step if the
   owner chooses to document instead.
4. **Call-site edits (separate commit).**
   - `groups.py:343` `get_participants`: `peer = await resolve_input_entity(chat_id, cl)`
     then `cl.iter_participants(peer, ...)` (no behaviour change without an
     allowlist; `ensure_connected` already called).
   - `groups.py:1829` `get_recent_actions`: `channel=await resolve_input_entity(chat_id, cl)`
     (or `utils.get_input_channel` of it; keep the "not a channel" error text
     unchanged).
   - `messages.py:2506` `send_typing`: `peer = utils.get_input_peer(entity)` from
     the entity already resolved and checked at 2494, so the checked and the
     used peer are one object. Update `tests/test_send_typing.py` accordingly.
   - Review the other `resolve_input_entity` callers in the inventory: no code
     change needed, covered by step 2. Update mocks in `test_reactions`,
     `test_save_draft_formatting`, `test_default_send_as`,
     `test_folder_organization`, `test_forward_message_routing` only if their
     stubs break.
5. **Docs (separate commit).** README "Chat Access Privacy": an allowlisted
   `@handle` always resolves by username (never by chat title) when an
   allowlist is on; recommend numeric ids and list secondary-username and
   malformed-handle caveats. `docs/arch/overview.md` "Access control layers":
   one line on the resolver guard. No ADR (see task).

## Files likely touched

- `telegram_mcp/runtime.py` (`resolve_input_entity`, `_resolve`, new helper)
- `telegram_mcp/tools/groups.py`, `telegram_mcp/tools/messages.py`
- `README.md`, `docs/arch/overview.md`
- `tests/test_peer_allowlist.py` or new `tests/test_input_entity_guard.py`,
  `tests/test_send_typing.py`, possibly the mock-based tool tests listed above

## Tradeoffs considered

- Central resolver guard (chosen) vs. resolve-then-check in every tool vs.
  replacing the string by the resolved id in `validate_id`: the last two add a
  `ResolveUsernameRequest` per allowlisted-handle call (flood limits) and touch
  ~15 tools or every tool's arguments; the guard adds none in the common case
  (a cache miss already ended in `ResolveUsernameRequest`; a hit stays free).
- Reading `client.session.get_entity_rows_by_username` couples to a Telethon
  session method (present on `MemorySession`, `SQLiteSession`, `StringSession`
  in 1.45). A missing method degrades to the safe network path, never to the
  name lookup. Alternative `client.get_entity` for every username under an
  allowlist avoids the coupling but loses cache hits.
- Gated on the allowlist vs. always: always would remove the quirk globally but
  breaks title addressing for users without an allowlist.
- The string shortcut in `_peer_item_allowed` stays: removing it would make
  every allowlisted call resolve twice. With the guard, string equality now
  implies "ResolveUsername or the cached row for exactly that username", which
  is what the shortcut assumed.
- Cached-then-reassigned usernames remain a documented Telethon limitation.

## Gates

`uv run flake8 --select=E9,F63,F7,F82 <touched>`, `uv run flake8 <touched>`
(no new warnings vs. base), `uv run black --check <touched>`,
`uv run pytest --cov --cov-report=term-missing` (fail_under 80). `runtime.py`
is a security-shaped path: security review before the PR. Never start a second
process with the live session string; all tests use a never-connected client.
