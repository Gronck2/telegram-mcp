# Plan 0012 events.py: alias-derived ids must not get marked-id guessing

Task: `docs/tasks/0012-events-alias-no-marked-guess.md`.

Base: `ours` at e9d85e0 (includes task 0010).

## Approach

Defensive call-site change in `telegram_mcp/tools/events.py`, no change to
`apply_alias`. Add a tiny private helper:

```python
def _exact_alias(chat_id):
    """apply_alias(), but an id taken from a saved alias stays exact."""
    resolved = apply_alias(chat_id)
    if isinstance(chat_id, str) and isinstance(resolved, int):
        return AliasID(resolved, chat_id)
    return resolved
```

- An `AliasID` input (the normal path: `@validate_id` already substituted it)
  is not a `str`, so it is returned unchanged by `apply_alias`; the helper
  leaves it alone.
- A `str` input that `apply_alias` turns into an int is, by definition, an
  alias lookup (digit strings never reach the body as `str`: `validate_id`
  makes them `int`) and is wrapped as `AliasID(id, wording)`.
- Plain int and username strings pass through unchanged, so their fallback and
  refusal texts are identical.

Use it in `_wait_target` (replaces `apply_alias(chat_id)` at L94) and in the
`wait_for_events` pre-check (L516). `AliasID` is already exported from
`runtime` (as used by `contacts.py`); import it in `events.py` next to
`apply_alias`.

Verified behaviour of the existing code (why the change is mostly defensive):
`wait_for_events` etc. are `@validate_id("chat_id")`, so the body already
receives an `AliasID` and `_resolve` (task 0010) skips the guess. The wrap
protects the call sites against direct calls and a race between the decorator
and the body, and makes the invariant local and testable. `_select_events`
re-filters every event by the read allowlist, so no leak exists either way.

## Steps

1. Tests first, via the real handler (see strategy). Capture the base outputs
   of the unchanged cases (plain int / digit string / username refusal texts
   and results) as expected strings. The race-simulation tests (direct body
   call with a plain-int input derived from an alias, or `_wait_target`
   called with the alias string) are the ones that fail on base; the handler
   path tests are expected to pass already on base and act as regression pins.
2. Add `_exact_alias` and use it at the two sites.
3. Run existing event tests (`tests/` files covering `wait_for_events`,
   `_wait_target`, `_select_events`) unchanged.
4. Gates; review.

## Files likely touched

- `telegram_mcp/tools/events.py` (helper, two call sites, import)
- New `tests/test_events_alias_no_marked_guess.py`

Not touched: `runtime.py`, `contacts.py`, `sanitize.py`, docs overview.

## Tradeoffs considered

- Call-site wrap (chosen, same as `set_contact_alias`) vs making `apply_alias`
  return `AliasID` (0010 decision 1: changes a security-shaped helper used by
  the allowlist; rejected again).
- Wrap vs only adding tests: tests alone pass today on the handler path; the
  wrap costs ~6 lines and closes the direct-call/race gap. Chosen because the
  coordinator approved the task; recoverable by dropping step 2.
- Skipping the pre-check `resolve_entity` for alias ids entirely: rejected,
  it changes the refusal behaviour when the exact entity is needed for a
  username/title-based allowlist entry.

## Test strategy

Stub clients only, never real Telegram; style of
`tests/test_alias_id_no_marked_guess.py` (task 0010): real
`TelegramClient(StringSession())` with `_call` replaced and
`session.process_entities` seeded with a channel and a basic group, recording
every request; `ensure_connected` monkeypatched; `TELEGRAM_ALIASES_FILE` in
`tmp_path`; `TELEGRAM_ALLOWED_CHAT_IDS` per case. Calls go through the real
`CallToolRequest` handler (`mcp.request_handlers`). Event log seeded with
events of the exact alias chat and of the marked variants.

Cases:

- Alias to positive id N without exact entity; cache has channel `-100N` and
  group `-N`; allowlist allows only `-100N`. `wait_for_events(chat_id=alias)`
  is refused with the same denial text as an unknown/denied chat; no recorded
  request names the marked variants; no events returned (also with events for
  `-100N` in the log).
- Same with allowlist allowing only `-N`.
- Direct body/`_wait_target` simulation of the race: call
  `_wait_target(alias_string)` and the `wait_for_events` pre-check with the
  alias string while the allowlist is on; assert no marked lookup (fails on
  base for the pre-check variant).
- Same for `wait_for_new_message` and `wait_for_settled_message` (they share
  `_wait_target`).
- Alias id N allowed exactly: works, returns the events of N; no entity lookup
  needed (`is_chat_allowed(resolved)` true).
- Alias id allowed but only readable via entity (username allowlist item):
  entity lookup is exact, no marked variant requested.
- Plain int N, digit string `"N"`, username: results and refusal texts equal
  the expected strings captured on base; plain int still falls back to the
  marked variant in the pre-check.
- Fan-out/`account`: alias with `account` given; no marked lookup on the other
  account's client.
- Allowlist disabled: `wait_for_events(alias)` returns events of N only and
  makes no resolver call.

## Gates

- `uv run flake8 --select=E9,F63,F7,F82 telegram_mcp/tools/events.py tests/test_events_alias_no_marked_guess.py`
  (blocking); `uv run flake8` on touched files adds no new warnings versus base.
- `uv run black --check telegram_mcp/tools/events.py tests/test_events_alias_no_marked_guess.py`
- `uv run pytest --cov --cov-report=term-missing` (80% gate).
- Review recommended (allowlist pre-check). Reviewer confirms refusal texts and
  plain-id behaviour are unchanged and that `apply_alias` is untouched.
