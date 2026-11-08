# 0003 Match allowlisted usernames against secondary/collectible usernames

## Goal

Follow-up from the round 4 review of task 0002 (owner decision: separate task,
2026-11-09). Two places refuse peers the allowlist actually grants. Both errors
deny access that should be allowed, never the reverse: no data leaks.

## Findings

1. `telegram_mcp/tools/contacts.py` `_add_contact_by_username`: the final
   check `peer_allowed(user, CHAT_ACCESS_WRITE)` matches only the marked id and
   `user.username`. When the allowlisted `@foo` is a collectible or secondary
   username (Telegram leaves `username` empty and lists `usernames`), the
   pre-check passes and the final check refuses. `check_chat_access(username_clean,
   WRITE)` then returns None, so the agent gets the generic "An error occurred
   (code: ...)" instead of the privacy error. Suggested fix:
   `user.is_self or is_chat_allowed(username_clean, user, CHAT_ACCESS_WRITE)`
   (safe: `ResolveUsernameRequest` resolved exactly this string), and build the
   error with `check_chat_access(username_clean, user, WRITE)` so it is never
   None.
2. `telegram_mcp/runtime.py` `peer_allowed(entity, access)` takes no identifier.
   `get_full_user`, `get_bot_info` and `resolve_username`, when given a username
   that is on the allowlist, still return only the public view if the entity's
   `username` field differs or is empty (multi-username users). This breaks the
   README promise "or their @username, when the tool is given the username".
   Suggested fix: optional `identifier` on `peer_allowed`, passed to
   `is_chat_allowed(identifier, entity, access)` only when
   `is_handle_like(identifier)` (alias wording must never match by string); pass
   `username` / `bot_username` / the cleaned user string from those tools.
   Consider whether `is_chat_allowed` should also match every entry of
   `entity.usernames` (active ones) for all tools.

## Acceptance

- Allowlisted secondary/collectible username: `add_contact` by that username
  succeeds; a refusal always carries the standard privacy error text.
- `get_full_user`, `get_bot_info`, `resolve_username` given an allowlisted
  username return the full output even when `entity.username` differs/is empty.
- Alias wording never matches the allowlist by string.
- No allowlist: unchanged, no extra Telegram calls. Tests under `tests/`, no
  real Telegram; CLAUDE.md gates pass.
