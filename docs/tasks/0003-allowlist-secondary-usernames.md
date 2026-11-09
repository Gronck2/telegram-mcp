# 0003 Match allowlisted usernames against secondary/collectible usernames

## Goal

Follow-up from the round 4 review of task 0002 (owner decision: separate task,
2026-11-09). Two places refuse peers the allowlist actually grants. Both errors
deny access that should be allowed, never the reverse: no data leaks.

## Findings

Both verified against the code (2026-11-09).

1. `telegram_mcp/tools/contacts.py` `_add_contact_by_username`: the final
   check `peer_allowed(user, CHAT_ACCESS_WRITE)` (contacts.py:448) matches only
   the marked id and `user.username` (`_matches_chat_allowlist`,
   runtime.py:1318-1328). When the allowlisted `@foo` is a collectible or
   secondary username (Telegram leaves `username` empty and lists `usernames`),
   the pre-check `deny_peer_param` (contacts.py:426) passes by string match and
   the final check refuses. The error is then built with
   `check_chat_access(username_clean, access=WRITE)` (contacts.py:449) without
   the entity, which matches the string and returns None, so
   `ChatAccessDeniedError(None)` and the agent gets the generic "An error
   occurred (code: ...)" instead of the privacy error. Suggested fix:
   `user.is_self or is_chat_allowed(username_clean, user, CHAT_ACCESS_WRITE)`
   (safe: `ResolveUsernameRequest` resolved exactly this string), and build the
   error with `check_chat_access(username_clean, user, WRITE)` so it is never
   None.
2. `telegram_mcp/runtime.py:1402` `peer_allowed(entity, access)` takes no
   identifier. `get_full_user` (profile.py:383), `get_bot_info` (profile.py:464)
   and `resolve_username` via `_resolved_peer_details` (chats.py:963), when
   given a username that is on the allowlist, still return only the public view
   if the entity's `username` field differs or is empty (multi-username users).
   This breaks the README promise "or their @username, when the tool is given
   the username". Suggested fix: optional `identifier` on `peer_allowed`,
   passed to `is_chat_allowed(identifier, entity, access)` only when
   `is_handle_like(identifier)` (runtime.py:2207; alias wording must never
   match by string); pass `username` / `bot_username` / the cleaned user string
   from those tools. Other `peer_allowed`/`view_for` callers
   (profile.py:124/135, contacts.py:402/485/887) have no username identifier
   and stay as they are.

## Acceptance

- Allowlisted secondary/collectible username: `add_contact` by that username
  succeeds; a refusal always carries the standard privacy error text.
- `get_full_user`, `get_bot_info`, `resolve_username` given an allowlisted
  username return the full output even when `entity.username` differs/is empty.
- Alias wording never matches the allowlist by string.
- No allowlist: unchanged, no extra Telegram calls. Tests under `tests/`, no
  real Telegram; CLAUDE.md gates pass.

## Owner decisions

1. **Decided (owner, 2026-11-09): option B** (match active
   `entity.usernames` everywhere, as plan step 5 in its own commit).

   Question: **should `is_chat_allowed` match every active entry of `entity.usernames`
   for all tools, not only the identifier the caller gave?**

   Analysis:
   - Trust model: an allowlisted `@foo` already means "whoever holds foo". A
     holder of foo as a secondary username is the same kind of holder as one
     with foo as primary (Telegram usernames are globally unique, and primary vs
     secondary is only a display choice of the holder). So matching a secondary
     entry grants nothing a primary entry does not.
   - Inactive usernames (`Username.active` false: deactivated, not resolvable,
     reclaimable by others): must not match. Only `active is True` entries; an
     entity without `usernames` (min entities, partial updates) simply does not
     match, fail closed.
   - Reassignment: if the owner releases foo and someone else takes it, an
     allowlisted `@foo` then grants the new holder, with or without this
     change. Not new, but it is a reason to prefer numeric ids in the
     allowlist; document it. Option B does not make it worse. A user who holds
     foo as secondary and also another allowlisted name is allowed either way.
   - Inconsistency without it: `send_message("@foo")` works (identifier match)
     but listings and filters that only have the entity (`get_chats`,
     `list_contacts` filter, events, search) hide that chat, and tools given
     the numeric id refuse it. With it, every path agrees.
   - Blast radius: it touches the core predicate in `runtime.py` (security
     path) and widens the match set for every tool, read and write.

   Options:
   - A. Identifier only (this plan, steps 1-4, 6). Smallest change, grants only
     what the caller named. Keeps the inconsistency above; listings of a chat
     allowlisted by secondary username stay incomplete (safe direction).
   - B. A plus matching active `entity.usernames` in `_matches_chat_allowlist`
     (plan step 5). Consistent everywhere, same trust model, still fail closed.
     Larger review surface.

   Recommendation: B, but as a separate last commit (step 5) so it can be
   dropped in review; Option A alone fixes the two reported findings. If the
   owner wants the minimum, take A and record B as a known limitation in
   README ("secondary usernames match only when given to the tool").

No other owner decision: no ADR needed (ADR 0002 unchanged; flag again
only if B changes the matching model enough that the owner wants one).
