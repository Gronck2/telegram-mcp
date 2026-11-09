# 0010 Do not guess marked-id variants for alias-derived ids

## Goal

An `AliasID` produced by `@validate_id` reaches `_resolve` with
`from_alias=False`, so marked-id variant guessing still runs for ids that came
from a saved alias, although the `_resolve` comment says that is unsafe: an
exact, user-confirmed id must never be "corrected" to a different chat.
Pre-existing; found in the task 0006 round 5 review (2026-11-10). Affects every
resolver user (all tools with `@validate_id` that go on to `resolve_entity` /
`resolve_input_entity`).

## Findings

- `telegram_mcp/runtime.py` `validate_single_id` (inside `validate_id`) returns
  `AliasID(resolved, value)` for a saved alias; the decorator substitutes it
  into the tool kwargs.
- `telegram_mcp/runtime.py` `_resolve` (~L2905-2928) computes
  `from_alias = identifier is not original`. For an `AliasID`, `apply_alias`
  returns the same object (it is already an int), so `from_alias` is False.
- `_resolve_with_retries(..., try_marked=not from_alias)` therefore tries
  `_marked_id_candidates` (`-100<id>`, `-<id>`, ~L2628, used ~L2875) for a
  positive alias-derived id, which can land on an unrelated chat.
- Contradicts the comment above `from_alias` ("An id that came from a saved
  alias is exact; guessing marked variants of it could deliver to a completely
  unrelated chat").

## Suggested approach

In `_resolve`, treat an `AliasID` as alias-derived:
`from_alias = identifier is not original or isinstance(identifier, AliasID)`
(and pass the same through any other resolver entry that calls
`_resolve_with_retries`). Keep the stale-alias wording behaviour unchanged.

## Acceptance

- Test: an alias mapping to a positive id that has no entity, while a channel
  `-100<id>` (and a basic group `-<id>`) exists in the cache, resolves to
  neither; the user gets the stale-alias question, and no request names the
  marked variants. Via `resolve_entity`, `resolve_input_entity` and one
  `@validate_id` tool.
- Plain positive ids (not from an alias) keep their marked-variant fallback.
- Gates as in the pipeline contract; `runtime.py` is security-shaped, so review
  is required.
