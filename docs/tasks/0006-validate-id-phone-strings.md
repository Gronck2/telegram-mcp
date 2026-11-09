# 0006 Keep "+"-led phone strings phone strings in validate_id

## Goal

Found during tasks 0004/0005 (pre-existing; owner wants it fixed now). A
`"+15551234567"` string passed as `chat_id` / `user_id` (or inside a list) must
address the user with that phone number, not the peer whose numeric id happens
to be `15551234567`. The allowlist must judge the peer that is actually
addressed.

## Findings

- `telegram_mcp/runtime.py` `validate_id` -> `validate_single_id`, string branch
  (lines ~1921-1931): `int(value)` runs first. Python's `int()` accepts a
  leading `+`, surrounding whitespace, `_` separators and non-ASCII digits, so
  `"+15551234567"` (and `" +15551234567 "`, `"+1_555"`) becomes the int
  `15551234567`. This is the only branch that converts; the `ValueError`
  fallback (alias, `is_handle_like`, ask payload) is never reached for them.
- Spellings: `"+1 555 123 4567"`, `"+1-555-123-4567"`, `"+1 (555) ..."` make
  `int()` raise, fall to `apply_alias` and then `is_handle_like`
  (`startswith("+")`, line ~2363), so they stay strings and are already handled
  as phones. Not affected. `"15551234567"` without `+` is a numeric id by
  design (`_NUMERIC_CHAT_ID_RE`, `-?\d+`, line 1156); unchanged. Only the
  unbroken `+` + digits form (and its `int()`-only quirks) is affected.
- What the allowlist sees: `deny_peer_param` gets the int, so `is_chat_allowed`
  checks `15551234567` against the id entries. A `+15551234567` allowlist entry
  (task 0005, `_identifier_matches_allowlist`, line ~1370, matches only
  `+`-led caller strings via `parse_phone`) can never match, and an allowlist
  that lists user id `15551234567` grants a phone string that names someone
  else. The tool body then resolves the same int, so the action lands on the
  id-peer, consistently wrong rather than inconsistently.
- Broader than a user id: a positive int makes `_resolve_with_retries` try
  `_marked_id_candidates` (`-1000000000000 - id`, `-id`, line 2611), so the
  string can also reach a channel or basic group with that number, subject
  only to the allowlist check on the int.
- Strings that stay strings take the 0005 path: `_resolve` -> `_get_entity`
  guard -> `_phone_lookup` (line ~2714), which answers only from the phone-row
  cache or `GetContactsRequest` and never from the display-name column; then
  `is_chat_allowed(item, entity, access)` checks the resolved entity, plus the
  `+`-phone entry by string. Once `validate_id` stops converting, that path
  works as designed with no change to `_phone_lookup` or
  `_identifier_matches_allowlist`.
- Telethon `get_input_entity(int)` for a bare positive int is a session lookup
  by `PeerUser(id)` (then the marked variants above via our retry). It matters
  here only as the reason the conversion is dangerous; no change needed.
- Tools without `validate_id` (e.g. `create_folder`, `forward_message(send_as)`)
  already keep the phone as a string, so after this fix the two classes of
  tool agree.

## Suggested approach

In `validate_single_id`, string branch, before the `int()` attempt: if the
stripped value starts with `+` and `utils.parse_phone(value)` accepts it,
return the string unchanged (it is a phone; no alias may shadow a `+`-led
string, `is_handle_like`). Make the numeric branch strict: convert only when
`_NUMERIC_CHAT_ID_RE.fullmatch(value.strip())`, so `int()` quirks (`+5`, `1_000`)
no longer turn into ids; those fall to the existing alias / handle / ask-payload
path. Range check and messages unchanged. Bare digits (`"123"`, `"-100123"`)
stay ints. List items use the same function. No allowlist: the phone string is
resolved as Telethon does by phone (`get_entity` via GetContacts, input lookup
via phone-row cache).

## Acceptance

- `validate_id` leaves `"+15551234567"` (also with surrounding whitespace, and
  inside a list) as a string; `"15551234567"` and `"-100123"` still become
  ints; out-of-range digits still give the existing error text.
- Under an allowlist listing user id `15551234567` only, `"+15551234567"` is
  denied unless the resolved phone's user (or the `+15551234567` entry) is
  allowed; with the `+15551234567` entry it is allowed and resolves via
  `_phone_lookup`; a same-titled cached chat or a channel with that id is never
  reached.
- Without an allowlist the phone resolves by phone, not by id.
- Tests under `tests/`, no real Telegram. Gates per CLAUDE.md pass.

## Owner decisions

None required for the main fix: it changes behaviour only for `+digits`
strings, which were silently wrong. Without an allowlist, existing callers who
relied on `"+123456"` meaning user id 123456 now get a phone lookup (intended;
the id form is `"123456"`).

Small call, recommendation given: the strict numeric branch also stops
`"1_000"` / `"+5"` / padded-sign oddities from becoming ids (they would get the
ask-the-user error). Recommend yes (no legitimate caller sends them); if the
owner prefers zero side effects, keep `int()` for non-`+` strings.

**Decided (owner, 2026-11-09): yes, strict numeric branch.** A chat parameter
string becomes an int only when it fully matches the allowlist parser's numeric
rule; other forms get the ask-the-user error.
