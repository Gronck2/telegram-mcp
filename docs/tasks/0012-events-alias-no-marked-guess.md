# 0012 events.py: alias-derived ids must not get marked-id guessing

## Goal

Follow-up of task 0010 (its "Follow-ups and audit of direct `apply_alias`
callers" section deferred `tools/events.py`). Make sure that in
`wait_for_events` (and the shared `_wait_target`) an id that came from a saved
alias is never passed to `resolve_entity` as a plain `int`, so the marked-id
variant guessing (`-100<id>`, `-<id>`) can never run for it, and pin that with
tests through the real tool handler.

## Why

0010 fixed `_resolve` for `AliasID` values. The call sites in `events.py` call
`apply_alias(chat_id)` themselves; if their input were ever a plain int derived
from an alias, `resolve_entity` would guess a marked variant, and a guessed
chat that the allowlist allows could make the allowlist pre-check pass for an
alias whose exact id is not allowed. The comment in `_resolve` states the
invariant: an alias id is exact.

## Findings

Verified against `ours` at e9d85e0 (2026-11-10).

- `wait_for_events` (L465), `wait_for_new_message` (L683),
  `wait_for_settled_message` (L758) and `get_messages_after` (L568) are all
  decorated with `@validate_id("chat_id")`. `_wait_target` (L90) is called only
  from the first three bodies (L514, L710, L788).
- `validate_single_id` (runtime.py L2193-2197) turns a saved alias string into
  `AliasID(id, wording)` before the body runs. `apply_alias` returns non-`str`
  input unchanged (runtime.py L2725), so in `_wait_target` and in the
  `wait_for_events` pre-check (L516) `resolved` is that same `AliasID`, not a
  plain int. After 0010, `_resolve` skips marked guessing for an `AliasID`.
  So, on the normal path through the MCP handler, the reported guess does not
  happen today. The "plain int from `apply_alias`" premise in the 0010 audit
  holds only for a body called without the decorator's substitution or when the
  alias store / allowlist changes between the decorator and the body (a narrow
  race, also reachable by direct calls from tests or internal code).
- Plain ints and digit strings (`validate_single_id` makes them `int`) keep the
  marked fallback by design (0010 decision, out of scope here).
- No leak known: `target` is the exact alias id, and `_select_events` (L438)
  filters every event with `is_chat_allowed(rec["chat_id"], rec.get("_chat"))`
  again, so even a wrong pre-check pass would not deliver events of a denied
  chat. The pre-check only decides between "refused" and "proceed"; its
  failure mode is a different refusal/acceptance for a denied alias, not event
  disclosure. Plan step 1 re-confirms this with a test.
- `_wait_target`: `isinstance(resolved, int)` returns the alias int without
  resolving (no guess). A non-alias string goes to `resolve_entity` (usernames,
  fine, no numeric guess). Only a non-int, non-alias value reaches it.

## Scope

- `telegram_mcp/tools/events.py`: at the two `apply_alias(chat_id)` sites
  (`_wait_target`, `wait_for_events` pre-check) keep an `AliasID` as is and
  wrap any other int that `apply_alias` produced from a string input as
  `AliasID(value, chat_id)`, the same call-site pattern as `set_contact_alias`
  after 0010. `apply_alias` itself is not changed.
- Tests through the real `CallToolRequest` handler with stub clients.

## Out of scope

- Changing `apply_alias`, `validate_id`, `AliasID`, `_resolve` or any allowlist
  function.
- Marked fallback for plain ints, digit strings, usernames (unchanged).
- Other tools (`get_messages_after` etc. do not call `apply_alias`).
- Refusal texts; they stay byte-identical.

## Acceptance

- Test (real handler, stub client, `TELEGRAM_ALLOWED_CHAT_IDS` allowing only
  the marked variant): `wait_for_events(chat_id="<alias>")` where the alias id
  has no exact entity and `-100<id>` / `-<id>` are cached is refused exactly as
  for an unknown/denied chat, and no marked lookup is recorded on the client.
  Also the same through `_wait_target` called directly with a plain-int
  alias-derived input path simulated (alias changes between decorator and body),
  and for `wait_for_new_message` / `wait_for_settled_message`.
- Alias id allowed (exact id in the allowlist): works, returns events.
- Plain int, digit string and username: behaviour and refusal text identical to
  base (compare against recorded expected strings captured on the base commit).
- No event of a denied chat is returned in any of the above.
- Gates as in the pipeline contract. `tools/events.py` is not on the
  security-shaped list, but the change guards an allowlist pre-check, so a
  review is recommended.

## Owner decisions (with recommendations)

None. Note for the owner: the finding is already neutralised on the handler
path by 0010 plus the `validate_id` substitution; this task is a defensive
change plus regression tests, small by design. If the owner prefers no code
change, the tests alone (plan step 1) still pin the invariant; the coordinator
already approved doing the task, so the plan keeps the small call-site wrap.

No ADR needed.
