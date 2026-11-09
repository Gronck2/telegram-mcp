# Plan 0006 validate_id keeps "+"-led phone strings as strings

Task: `docs/tasks/0006-validate-id-phone-strings.md`. Follows tasks 0004/0005.

## Approach

Fix at the single conversion point, `validate_single_id` in
`telegram_mcp/runtime.py`. Decide phone-vs-id before `int()` runs: a
`+`-led string that `utils.parse_phone` accepts is returned as a string and
flows into the existing 0005 machinery (`deny_peer_param` ->
`_peer_item_allowed` -> `resolve_entity` -> `_phone_lookup` -> entity check +
`+`-phone entry). Round 2: `_lookup_callable` also routes phone strings through
`_phone_lookup` with or without an allowlist (via `_phone_routed`), so a phone
resolves only via the phone cache or contacts, never by a chat title.
Round 3: `_resolve` converts digit strings to ints in every mode (via
`_digit_string_to_id`), so a digit string is an id for every resolver call.
Round 4: `_digit_string_to_id` raises a clean range error for values Telethon
cannot pack (outside int64, or beyond int()'s 4300 digits), and
`get_participants` / `get_recent_actions` (`telegram_mcp/tools/groups.py`)
always go through `resolve_input_entity`, with or without an allowlist.
Round 5: a digit string beyond 4300 digits matches the numeric regex but `int()`
raises; `validate_single_id` now returns the same "ID is out of the valid
integer range." error as for int64 overflow, and `_identifier_matches_allowlist`
treats an un-convertible digit string as not allowed instead of raising. The
allowlist matcher is otherwise unchanged. The
numeric branch is tightened to the same `_NUMERIC_CHAT_ID_RE` the allowlist
code uses, so both sides agree on what an id string is.

## Steps

1. Tests first (`tests/test_validate_id_phone.py`, plus cases in
   `tests/test_validation.py` style, stub clients, no real Telegram):
   - unit: `"+15551234567"`, `" +15551234567 "`, `"+1555"`, list items stay
     strings; `"15551234567"`, `"-100123"`, int input stay ints; `"+1 555 ..."`,
     `"+1-555-..."` unchanged (strings); out-of-range digits keep the error;
     `"1_000"`, `"+5_5"` no longer ints (alias/ask path).
   - allowlist: allowlist `15551234567` only, caller `"+15551234567"` with a
     stub client whose GetContacts returns a different user -> denied and the
     tool body is not entered; contact user allowed by id -> allowed and tool
     body receives the string; `+15551234567` entry -> allowed by string and
     entity check; user/channel with id `15551234567` (and marked variants) is
     never queried; display-name cache hit titled `+15551234567` not used.
   - no allowlist: tool body receives the string and `resolve_entity` goes the
     phone route (`get_entity("+...")`), not `get_entity(15551234567)`.
   - `peer_subject=True` and `raise_errors=True` variants behave the same.
2. `runtime.py` `validate_single_id`: add the phone check before the `int()`
   try; replace `int(value)` with a regex-guarded conversion; keep range check,
   messages and the alias / `is_handle_like` / ask-payload fallthrough.
3. Grep tools for `int(chat_id)`-style conversions or `lstrip("+")` on
   parameters that bypass `validate_id`, to confirm none repeats the bug
   (record the result in the task if any is found; fix only inside this
   task's scope, else a new task).
4. Docs: README "Chat Access Privacy" (phones need `+`; bare digits are ids),
   `docs/arch/overview.md` one line.

## Files likely touched

`telegram_mcp/runtime.py`, `telegram_mcp/tools/groups.py` (round 4),
`tests/test_validate_id_phone.py` (new, plus later round test files),
`tests/test_validation.py` (if existing cases need adjusting),
`README.md`, `docs/arch/overview.md`.

## Tradeoffs

- Fix in `validate_id` rather than in the resolvers: the int is produced
  before any resolver or allowlist code sees the original string, so only
  there can the information be preserved.
- Return the string unchanged (not canonical `+digits`): keeps error wording
  and alias/ask behaviour; `_identifier_matches_allowlist` and `_phone_lookup`
  normalise through `parse_phone` themselves.
- `parse_phone` accepts any digit run ("+1"), so very short `+` strings are
  phones too; fine, a `+` is an explicit phone marker.
- Strict numeric regex is a small extra behaviour change (see task, owner
  call, recommended); alternative is to keep `int()` for non-`+` strings.
- Round 5 refuses the over-long digit string at the earliest point (the
  decorator) and keeps the matcher defensive as a second line; the
  decorator error text truncates the echoed value to 40 characters.
- Phone lookup (with or without an allowlist) can fail where a numeric id would have
  succeeded (phone not in contacts or phone cache): intended, the user must
  use the id then.

## Gates

`uv run flake8 --select=E9,F63,F7,F82 <touched>`, `uv run flake8 <touched>`
(no new warnings), `uv run black --check <touched>`,
`uv run pytest --cov --cov-report=term-missing` (fail_under 80).
`runtime.py` is a security-shaped path (review required). Tests never touch
real Telegram. Do not touch the live deployment or its session.
