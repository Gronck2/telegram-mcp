# Plan 0005 Allowlist entries that are neither ids nor usernames are inert

Task: `docs/tasks/0005-allowlist-non-username-entries.md`. Follow-up of task
0004; owner chose a separate task.

## Approach

Fix it at parse time. `_parse_chat_id_token` keeps only what the string
shortcut can safely grant: numeric ids, `*`, usernames and phone numbers. Every
other token (free text, invite links, malformed handles) becomes the existing
inert sentinel `@*`, which no identifier equals, so it counts as an entry but
matches nothing. A caller string can then never pass the shortcut through such
an entry, and the display-name lookup is unreachable through it.

## Steps

1. Tests first (`tests/test_allowlist_inert_entries.py`): free text
   (`my group`), invite links (`t.me/+hash`, `https://t.me/joinchat/x`) and
   malformed tokens (`1abcd/`, `abcde_`) grant nothing by string
   (`is_chat_allowed`); a same-titled cached chat is never used through
   `create_folder`, `forward_message(send_as=...)` and both resolvers; one
   warning per ignored entry; only-ignored entries deny all; id, username,
   `@Name`, `t.me/name` and `*` entries and the no-allowlist case unchanged.
2. `runtime.py`: in `_parse_chat_id_token` accept `utils.parse_username` ->
   name and not invite (stored as the lower-cased username, so `t.me/name`
   becomes `name`); keep phone numbers (`utils.parse_phone`, resolved from the
   phone column, pinned by existing tests); otherwise warn and return the
   sentinel. Thread a `source` (variable name) through `_parse_allowed_chat_ids`.
   Dedupe warnings per (variable, token) because the environment is re-parsed on
   every access check, and parse all three variables once at import so the
   warning appears at startup.
3. Fix the `_is_malformed_handle` docstring: free text cannot match an entry by
   string by construction.
4. Docs: README "Chat Access Privacy", `docs/arch/overview.md`.

## Files likely touched

`telegram_mcp/runtime.py`, `tests/test_allowlist_inert_entries.py`,
`README.md`, `docs/arch/overview.md`.

## Tradeoffs

- The warning goes to stderr, not the package logger: the logger only emits
  ERROR and `tests/test_error_logging.py` requires constant log messages, but
  the owner must see which entry was dropped. The token is passed through
  `sanitize_name` and truncated; it is an allowlist entry, not a secret.
- `t.me/name` is accepted (what `parse_username` accepts as a non-invite
  username) and normalised to `name`; previously the raw string was stored and
  only matched the identical caller string.
- Phone entries stay valid, but (round 4) Telethon does NOT answer a phone only
  from the phone column: `client.get_input_entity` goes to
  `session.get_input_entity`, which on a phone-row miss matches exact display
  names (Telethon 1.45 `get_entity` raises on a contacts miss; pinned as well,
  defence in depth). Under an
  allowlist `_phone_lookup` therefore resolves a phone string only from a
  phone-row hit or a `GetContactsRequest` match and refuses otherwise, in both
  resolvers (keeps `resolve_input_entity(s) == get_input_peer(resolve_entity(s))`).
- Round 4: `self`, `@self`, `t.me/self` entries are inert with the usual warning
  (Telethon resolves `self` to the own account); `_identifier_matches_allowlist`
  never grants a self-ref caller string. Use the numeric own id.
- Round 3/4 docs: `set_contact_alias` refuses every key Telethon reads as a
  username (incl. 3-4 char Latin nicknames like `bob`, `anna`) in both modes
  (`alias_shadows_real_identifier`); under an allowlist legacy aliases of that
  shape are ignored for resolution, unchanged without one.
- A variable with only ignored entries stays a non-empty set, so the allowlist
  is on and denies all (same rule as `@*`).

## Gates

`uv run flake8 --select=E9,F63,F7,F82 <touched>`, `uv run flake8 <touched>`
(no new warnings), `uv run black --check <touched>`,
`uv run pytest --cov --cov-report=term-missing` (fail_under 80).
`runtime.py` is a security-shaped path. Tests never touch real Telegram.
