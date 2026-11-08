# Plan 0002 Close chat-allowlist coverage gaps (contacts, resolvers, user_id tools)

Task: `docs/tasks/0002-allowlist-peer-coverage.md` (owner decisions recorded
there). Separate commits per group.

## Approach

Add small central helpers in `runtime.py` and use them from the tools instead of
ad hoc `is_chat_allowed` calls. Keep `CHAT_PARAM_NAMES` as is (`user_id` stays
out: it names a member inside a checked chat).

## Steps

1. **Inventory test first.** Import the server with dummy env, list
   `runtime.mcp._tool_manager.list_tools()`, and write a test with explicit sets:
   peer-checked tools and exempt tools (groups D, E). It fails if a tool has a
   peer-naming parameter (`user_id`, `username`, `bot_username`, `phone`,
   `allow_users`, `disallow_users`, `send_as`, `user_ids`, plus
   `CHAT_PARAM_NAMES`) and is in neither set. Delete any `t.session*`.
2. **Helpers in `runtime.py`.**
   - `peer_allowed(entity, access)`: `is_chat_allowed(get_marked_id(entity),
     entity, access)`, True with no allowlist.
   - `refuse_if_read_restricted(tool_name)`: returns the standard privacy
     error (via `check_chat_access` wording / `ChatAccessDeniedError` path) when
     read access is restricted: `get_chat_allowlist("read")` is not None and
     does not contain the `*` wildcard (owner: read `*` exempts group A); used
     by group A.
   - `public_view(entity) -> dict`: only id, type (user/bot/group/channel),
     first/last name or title (sanitized), username(s), bot flag, badges
     (verified, scam, fake, premium). Built from an explicit allow-list of
     fields, never by copying and removing, so new fields stay out by default.
   - `view_for(entity, full_builder)`: returns the full output when
     `peer_allowed(entity, read)`, else `public_view(entity)`.
   - Subject-param check for user-subject tools: a `PEER_SUBJECT_TOOLS` set (or
     `@validate_id("user_id", subject=True)`), checking the resolved entity like
     the existing chat branch (cheap id/username match, then resolve and check),
     with the mode from `chat_access_for_tool`. Group D tools never set it.
3. **Group A (contacts.py), commit 1.** Refuse at the top of `list_contacts`,
   `search_contacts`, `export_contacts`, `get_contact_ids`, `get_blocked_users`
   before `get_client` / any request. `list_contact_aliases`: filter records by
   alias id (marked id) plus stored username if any, with read access, no
   hidden count.
4. **Group B (profile.py, chats.py, contacts.py), commit 2.**
   `resolve_username` (users and chats): build the full result only when the
   resolved entity is read-allowed, else `public_view`. `get_full_user`,
   `get_bot_info`: same through `view_for` (bot description omitted outside
   read). `resolve_phone`: resolve, then `view_for`; do not return
   `str(result)` outside read. `get_user_photos`, `get_user_status`: subject
   check (read), refuse outside read. `get_privacy_settings`: replace user
   entries outside read with `public_view`; avoid dumping `str(settings)` raw
   when the allowlist is enabled.
5. **Group C, commit 3.** Subject-param check (write) for `block_user`,
   `unblock_user`, `delete_contact`, `set_privacy_settings`
   (`allow_users`, `disallow_users`), `set_default_send_as.send_as`;
   resolved-entity check in `set_bot_commands`, `add_contact` (username branch),
   `set_contact_alias` (plus the missing `@validate_id("chat_id")`).
   `add_contact` by phone and `import_contacts`: no gate; route any returned user
   detail (`add_contact` failure text embeds `str(result)`) through
   `view_for`.
6. **Group D regression tests, commit 4.** For each group D tool: chat on the
   allowlist, user not on it, assert the user is accepted.
7. **Docs and ADR, commit 5.** README "Chat Access Privacy" (refused contact
   tools incl. the `*` note, public view and its field list, write access for
   acting on users, `get_send_as` note), `docs/arch/overview.md` "Access control
   layers", and `docs/adr/0002-<slug>.md`: "peer-subject parameters are checked
   as the user's private chat, and outside-read peers get a public view" (context,
   decision, consequences, rejected: adding `user_id` to `CHAT_PARAM_NAMES`,
   filtering instead of refusing).

## Files likely touched

- `telegram_mcp/runtime.py`
- `telegram_mcp/tools/contacts.py`, `profile.py`, `chats.py`, possibly
  `messages.py` (`set_default_send_as`)
- `README.md`, `docs/arch/overview.md`, `docs/adr/0002-*.md`
- `tests/test_chat_allowlist.py`, `tests/test_chat_access_modes.py`, new
  `tests/test_peer_allowlist.py`

## Tradeoffs considered

- `user_id` into `CHAT_PARAM_NAMES`: simplest but breaks group D. Rejected.
- Public view built from an explicit field allow-list, not by stripping: new
  Telethon fields stay hidden by default.
- Group A refused rather than filtered (owner): simple, no per-entry matching;
  makes the contact tools unusable under any allowlist, including `*` read.
- `resolve_phone` spends quota before the public view is returned (accepted).
- `add_contact` / `import_contacts` ungated (owner): the agent can add phone-book
  entries for people outside the allowlist, but only sees their public view.

## Gates

`uv run flake8 --select=E9,F63,F7,F82 <touched>`, `uv run black --check
<touched>`, `uv run pytest --cov --cov-report=term-missing` (fail_under 80).
