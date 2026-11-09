# Plan 0010 Do not guess marked-id variants for alias-derived ids

Task: `docs/tasks/0010-alias-id-marked-guessing.md`.

Base: `ours` at 7b4062f (includes tasks 0006-0009).

## Approach

A one-line semantic fix in `telegram_mcp/runtime.py` `_resolve`, plus tests.
No new API, no change to `apply_alias`, `validate_id`, `AliasID` or the
allowlist code.

Current behaviour (verified):

- `validate_single_id` turns a saved alias into `AliasID(id, wording)` (an
  `int` subclass). `apply_alias` returns non-`str` input unchanged, so in
  `_resolve` `identifier is original` and `from_alias` is False.
  `try_marked=True` then lets `_resolve_with_retries` try
  `_marked_id_candidates(id)` = `[-1000000000000 - id, -id]` after the
  cold-cache retry fails (L3121-3126).
- When everything fails `_resolve_with_retries` raises `EntityNotFoundError`
  (a `ValueError`); `_resolve` catches it (and `_PEER_ERRORS`) and
  `alias_failure(original, identifier)` builds the stale-alias question because
  `alias_wording(AliasID)` is the wording and `identifier` is an `int`
  (`kind="stale"`, `stored_id=id`). Wording text: `alias_ask_payload` kind
  `stale` ("Nothing was sent. The saved contact for «...» (id N) no longer
  resolves ... replace=True ..."). Structured-errors kind is
  `invalid_argument` with `needs_user: True` (L1984-1985).

Change:

```python
from_alias = identifier is not original or isinstance(identifier, AliasID)
```

and fix the comment to say it covers both a string alias resolved here and an
`AliasID` resolved earlier by `@validate_id`. Nothing downstream changes: the
identifier still goes through `_digit_string_to_id` (no-op for an int) and the
same warm-cache/reconnect retries, so a cold cache is still warmed with
`get_dialogs()` and the exact id retried; only the marked guess is skipped.

Entry audit:

- `_resolve_with_retries` has exactly one caller, `_resolve`; `resolve_entity`
  and `resolve_input_entity` both go through `_resolve`. So one fix covers both
  getters, the allowlist-guarded getters (`_guarded_entity_getter`,
  `_guarded_input_getter`, they receive the same int) and `_phone_routed`
  (strings only, never an `AliasID`).
- `_marked_id_candidates` is used only at L3122 and in the error text L3130.
  The error text lists variants that were not tried when `try_marked` is False;
  make the message say "including marked variants" only when they were tried
  (it is chained via `from error` and replaced by the stale question for an
  alias, but keep it truthful). Plain-id message stays byte-identical.
- Allowlist: `_peer_item_allowed` resolves only `str` items and compares ints
  exactly (`is_chat_allowed`, marked-id semantics); it never guesses. An
  `AliasID` therefore needs no change there. `deny_peer_param` uses
  `alias_wording(item) or item` only for the error text.
- `apply_alias` already hides an alias whose id is outside both allowlists
  (returns the string), so that case never produces an `AliasID`.
- Fan-out (`with_account(readonly=True)`): each account's call runs the same
  `_resolve` with the same `AliasID`; the fix applies per account, so an alias
  id that exists only in one account no longer lands on a guessed chat in the
  other; the other block gets the stale question as before.
- Negative alias ids (already marked) were never guessed
  (`_marked_id_candidates` returns `[]` for `<= 0`); behaviour unchanged.
- Task 0006 digit strings: a numeric string becomes a plain `int` (not an
  `AliasID`) in `validate_single_id` and keeps the fallback; alias keys cannot
  be digit-like (set_contact_alias refuses them), so the two do not overlap.
- Not changed, documented as residual: `tools/events.py` `_wait_target` /
  `wait_for_events` (L94, L516-519) pass the plain int from `apply_alias` into
  `resolve_entity`, which still guesses. See task decision 1.

## Steps

1. Tests first (see strategy); the alias-no-guess tests fail on the base.
2. Edit `_resolve` (`from_alias` expression and comment) and the
   `EntityNotFoundError` message in `_resolve_with_retries`.
3. Run the existing resolver tests unchanged
   (`tests/test_runtime.py` marked-candidate tests, `tests/test_aliases.py`
   stale tests, `tests/test_peer_allowlist.py`, `tests/test_input_entity_guard*`).
4. Docs: `CLAUDE.md` Entity-resolution paragraph says marked variants are tried
   "for bare positive integer IDs"; it needs no change (an alias id is not
   "bare"), but add one sentence to `docs/arch/overview.md` if it describes
   resolution (grep found no mention today, so likely none).
5. Gates; review (security-shaped).

## Files likely touched

- `telegram_mcp/runtime.py` (`_resolve`, message in `_resolve_with_retries`)
- New `tests/test_alias_id_no_marked_guess.py`

Not touched: `validate_id`, `apply_alias`, `AliasID`, allowlist functions,
`tools/*`, `sanitize.py`.

## Tradeoffs considered

- `isinstance(identifier, AliasID)` in `_resolve` (chosen) vs making
  `apply_alias` return `AliasID`: the latter also fixes `events.py` but changes
  the contract of a helper the allowlist depends on and touches every caller;
  deferred (decision 1, recommendation A).
- Skip the guess vs keep it but verify the result against the alias: the guess
  is exactly what the comment forbids, and verifying has no ground truth (the
  alias stores only an id), so skipping is the safe option.
- Cost: an alias saved with a bare positive id that really denotes a channel or
  group (the user typed `1234567` and it worked via the guess) now gets the
  stale question once and must be re-saved with the marked id. That is the
  intended safe failure; the question text already tells the agent to
  `set_contact_alias(..., replace=True)`.
- Truthful error text vs leaving it: tiny edit, avoids a misleading
  "including marked variants" when none were tried.

## Test strategy

Stub clients only, never real Telegram. Two stub styles, both already used:

- Recording fake client (as `_ResolvingClient` in `tests/test_runtime.py`): a
  `get_entity` / `get_input_entity` that raises `ValueError` for the positive
  id and returns an entity for `-1000000000000 - id` / `-id`, recording
  `calls`, plus `get_dialogs`.
- Real `TelegramClient(StringSession())` with `_call` replaced and
  `session.process_entities` seeded with a channel and a basic group (style of
  `tests/test_validate_id_phone_adversarial.py`), recording every request, for
  the end-to-end cases.

`ensure_connected` monkeypatched; `TELEGRAM_ALIASES_FILE` in `tmp_path`.

Cases:

- `resolve_entity(AliasID(ID, "мой контакт"), client)` where the id has no
  entity but `-100<ID>` (channel) and `-<ID>` (group) are resolvable: raises
  `AliasNeedsUser`, payload `error == "stale_contact"`, `reference` is the
  wording, `stored_id` text has the id; `client.calls` contains no
  `-1000000000000 - ID` and no `-ID`; the cold-cache retry still happens
  (`get_dialogs` once, `[ID, ID]`).
- Same for `resolve_input_entity`.
- Same via the string alias: `resolve_entity("мой контакт", client)` with the
  alias saved (pins the already-working `identifier is not original` path).
- Real-client variant: no recorded request is a `GetChannelsRequest` /
  `GetChatsRequest` / `GetUsersRequest` naming the marked variants.
- One `@validate_id` tool through the real `CallToolRequest` handler (register
  a small test tool with `mcp.tool()` + `validate_id("chat_id")` whose body
  calls `resolve_entity`; remove it afterwards), alias saved in the aliases
  file: the result carries the stale question, `isError` handling per
  `TELEGRAM_STRUCTURED_ERRORS=1` gives kind `invalid_argument` with
  `needs_user` (task 0007), and no marked variant was requested. Also a list
  parameter holding an alias and a plain id.
- Positive control: an alias whose id does resolve directly returns that
  entity; a plain positive int (`resolve_entity(ID, client)`) with the same
  stubs still resolves via the marked variant (existing
  `test_runtime.py` tests stay and a new one with the channel/group seeded);
  a digit string `"<ID>"` through `@validate_id` still falls back.
- Negative alias id (already marked) resolves exactly as before.
- Allowlist: with `TELEGRAM_ALLOWED_CHAT_IDS` containing only the alias id,
  an alias call does not reach the channel `-100<ID>` even if the stub has
  it; the unlisted marked variant is never requested.
- Fan-out: two stub accounts, alias id known only in one; the other block is
  the stale question and its client saw no marked variants.
- `_resolve_with_retries` error text: plain id keeps "including marked
  variants [...]"; `try_marked=False` does not claim them.
- Existing tests pass unchanged.

## Gates

Per the Pipeline contract:

- `uv run flake8 --select=E9,F63,F7,F82 telegram_mcp/runtime.py <test file>`
  (blocking); `uv run flake8` on touched files adds no new warnings versus base.
- `uv run black --check telegram_mcp/runtime.py <test file>`
- `uv run pytest --cov --cov-report=term-missing` (80% gate).
- Review required: `runtime.py` is security-shaped. Reviewer confirms the
  fallback still works for plain ids and digit strings, no alias-derived id can
  reach `_marked_id_candidates`, the stale wording and structured-error kind
  are unchanged, and the allowlist paths are untouched. No ADR needed (the
  change enforces an existing documented invariant).
