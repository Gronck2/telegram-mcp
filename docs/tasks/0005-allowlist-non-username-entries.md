# 0005 Make allowlist entries that are neither ids nor usernames inert

## Goal

Follow-up from the round 4 review of task 0004 (owner decision: separate task,
2026-11-09). An allowlist token that is neither a numeric id, `*`, nor a valid
non-invite username (free text such as `my group`, an invite link such as
`t.me/+hash`, `1abcd/`) is kept by `_parse_chat_id_token` as a string entry.
A caller string that matches it by string passes the string shortcut and is
then resolved by Telethon's display-name column, so the action lands on
whichever cached chat has that exact title (attacker-controllable).

## Findings

- `telegram_mcp/runtime.py` `_parse_chat_id_token` keeps any token as a string
  entry; `_matches_chat_allowlist` matches it after `strip().lstrip("@")
  .strip().lower()`.
- `_is_malformed_handle` returns False for such strings (the cleaned form is
  neither a username nor handle-like), so `_guarded_input_getter` sends them to
  `client.get_entity`, whose `_get_entity_from_string` "exact name/title"
  branch answers from the session name column.
- Reachable where `validate_id` is skipped: `create_folder(chat_ids=[...])`
  (folders.py, string check then `resolve_input_entity`) and
  `forward_message(send_as=...)` (`require_peer_access` → `deny_peer_param`
  string shortcut). `@t.me/+hash` against a `t.me/+hash` entry fails the same
  way. Tester observation: `@1abcd/` against a `1abcd/` entry.
- The `_is_malformed_handle` docstring claims "plain free text cannot match an
  allowlist entry by string", which is false for such entries.
- Precondition: the owner wrote a non-username, non-id entry. README documents
  ids and usernames only.

## Suggested approach

`_parse_chat_id_token`: turn tokens that are not ids, `*` or valid non-invite
usernames (Telethon `utils.parse_username`) into an inert sentinel (like `@*`)
and log a startup warning naming the ignored entry (sanitised, no secrets).
Fix the docstring. README: say only numeric ids and usernames are accepted and
other entries are ignored with a warning.

## Acceptance

- A free-text / invite-link / malformed allowlist token grants nothing by
  string; a same-titled cached chat is never used (create_folder,
  forward_message send_as, both resolvers).
- Startup logs one warning per ignored entry.
- A variable whose only entries are ignored still counts as set (deny-all),
  consistent with the existing "non-blank value with no usable entry" rule.
- Id and username entries unchanged. No allowlist: unchanged. Tests under
  `tests/`, no real Telegram; CLAUDE.md gates pass.

## Owner decisions

- Round 4 review findings 1-3 (phone entries matching a chat titled like the
  phone, `self` entries granting the own account by string, undocumented alias
  refusal of username-shaped keys) are fixed in this task, not deferred
  (owner, 2026-11-09).
