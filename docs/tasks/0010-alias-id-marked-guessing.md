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

Verified against `ours` at 7b4062f (2026-11-10); line numbers refreshed. All
four findings are correct.

- `telegram_mcp/runtime.py` `validate_single_id` (inside `validate_id`,
  L2150-2208) returns `AliasID(resolved, value)` for a saved alias (L2193-2197);
  the decorator substitutes it into the tool kwargs (L2226 / L2240). `AliasID`
  is defined at L2746.
- `_resolve` (L3152-3182) computes `from_alias = identifier is not original`
  (L3165). For an `AliasID`, `apply_alias` (L2712) returns the same object (it
  is not a `str`), so `from_alias` is False.
- `_resolve_with_retries(..., try_marked=not from_alias)` (L3173) therefore
  tries `_marked_id_candidates` (`-100<id>`, `-<id>`; defined L2875, used L3122)
  for a positive alias-derived id, which can land on an unrelated chat.
- Contradicts the comment above `from_alias` (L3163-3164, "An id that came from
  a saved alias is exact; guessing marked variants of it could deliver to a
  completely unrelated chat").
- Additional, found while planning: `_resolve` is the only caller of
  `_resolve_with_retries` and of `_marked_id_candidates` outside the error text
  (L3130), so there is no second resolver entry to patch. A narrower related
  path exists in `tools/events.py` (`_wait_target` L94 and `wait_for_events`
  L516-519): `apply_alias` yields a plain int that is then passed to
  `resolve_entity`, which still guesses. See decision 1.
- Under a chat allowlist the id is judged exactly (`is_chat_allowed` compares
  marked ids; `_peer_item_allowed` only resolves `str` items, so an `AliasID`
  is never resolved or guessed by the allowlist check), but the tool body then
  resolves with the guessing fallback, so the guess is not covered by the
  allowlist's judgement of the alias id. This is why the fix matters beyond
  convenience.

## Follow-ups and audit of direct `apply_alias` callers

Audit of `apply_alias(...)` callers outside `runtime.py` (round 1 deep review):

- `tools/contacts.py` `set_contact_alias`: resolves the target and persists the
  resulting marked id as a new alias, so a guessed chat would be saved
  permanently. Fixed in 0010: an int returned by `apply_alias` is wrapped as
  `AliasID(target, chat_id)` at the call site (the `apply_alias` contract is
  unchanged), so `_resolve` skips the guess and a stale alias gives the existing
  `target_not_found` refusal.
- `tools/events.py` `wait_for_events` (L516-519): `apply_alias` yields a plain
  int that `resolve_entity` may still guess for, but only for the allowlist
  pre-check; it neither sends nor saves, `target` stays the exact alias id and
  `_select_events` filters every event against the read allowlist again.
  `_wait_target` (L94) also calls `apply_alias` but returns the int without
  resolving it. Deferred to a follow-up task (events.py).
- `runtime.py` `validate_single_id` (L2193) already wraps the result in `AliasID`;
  `_resolve` (L3161) is the fixed resolver itself.

No other direct callers exist.

## Suggested approach

In `_resolve`, treat an `AliasID` as alias-derived:
`from_alias = identifier is not original or isinstance(identifier, AliasID)`.
Keep the stale-alias wording behaviour unchanged. Details in the plan.

## Acceptance

- Test: an alias mapping to a positive id that has no entity, while a channel
  `-100<id>` (and a basic group `-<id>`) exists in the cache, resolves to
  neither; the user gets the stale-alias question, and no request names the
  marked variants. Via `resolve_entity`, `resolve_input_entity` and one
  `@validate_id` tool.
- Plain positive ids (not from an alias) keep their marked-variant fallback.
- Gates as in the pipeline contract; `runtime.py` is security-shaped, so review
  is required.

## Owner decisions (with recommendations)

1. Scope of the fix. Option A: only `_resolve` (the `isinstance(..., AliasID)`
   check). Option B: additionally make `apply_alias` return an `AliasID`, so
   direct callers such as `wait_for_events` (events.py L516-519) also stop
   guessing. Recommendation: A. The events path only feeds an allowlist check,
   its target stays the exact alias id, and B changes the contract of a
   security-shaped helper used by the allowlist; record the events path as a
   follow-up if the owner wants it.
2. Out of scope, FYI only: a plain positive id typed by the user still gets the
   marked fallback under an allowlist too (task says keep it). Not a decision
   for this task; mention only if the owner wants a separate hardening task.

## Decisions (coordinator on the owner's behalf, 2026-11-10)

- Decision 1: option A, change only `_resolve` (treat an `AliasID` as
  alias-derived). Making `apply_alias` return an `AliasID` for
  `tools/events.py` (`_wait_target` / `wait_for_events`) is a follow-up task,
  not part of 0010.
- The order of entity resolution relative to the chat-allowlist check does not
  change.
- Deep review is mandatory (peer resolution is the area of the task 0006 bug
  where the allowlist looked at a different peer).
