# Plan 0003 Match allowlisted usernames against secondary/collectible usernames

Task: `docs/tasks/0003-allowlist-secondary-usernames.md` (findings verified
against the code there; the open owner decision on `entity.usernames` is
recorded there too). Separate commits per group.

## Approach

Pass the identifier the caller typed down to the one place that judges a
resolved user, instead of widening what an entity matches. Nothing in this task
widens access beyond what the caller named; the optional broad match (Option B
in the task) is a separate, last commit that is only done if the owner picks it.

## Steps

1. **Tests first** (`tests/test_peer_allowlist.py`, stub entities with
   `username=None` and `usernames=[Username("foo", active=True)]`): add the
   failing cases for both findings, plus negatives (alias wording, a different
   user holding another username, no allowlist makes no extra call).
2. **`peer_allowed(entity, access, identifier=None)` in `runtime.py`.** After
   the existing self/marked-id path, if `identifier` is a `str` and
   `is_handle_like(identifier)`, also accept
   `is_chat_allowed(identifier, entity, access)`. Alias wording never reaches
   it (`is_handle_like` false), and an int identifier adds nothing (the marked
   id already covers it). No allowlist: still returns True first, no extra work.
   Also let `view_for(entity, builder, identifier=None)` forward it.
3. **Tools, commit 1.** `get_full_user` (profile.py:383, pass `username`),
   `get_bot_info` (profile.py:464, pass `bot_username`), `_resolved_peer_details`
   (chats.py:963, pass the cleaned `clean_user`; add a parameter, call site in
   `resolve_username`). Check `resolve_phone` / other `view_for` callers: they
   have no username identifier, leave as is.
4. **`add_contact`, commit 2** (contacts.py:448-449). Replace the final check
   with `user.is_self or is_chat_allowed(username_clean, user, CHAT_ACCESS_WRITE)`
   (the identifier is exactly what `ResolveUsernameRequest` resolved, so this is
   safe; a plain `peer_allowed(user, WRITE, username_clean)` is equivalent and
   keeps the `is_self` exemption). Build the error with
   `check_chat_access(username_clean, user, WRITE)` so it is never `None`; if
   it still were `None`, fall back to the standard text rather than crashing.
5. **Optional commit 3 (only if owner chooses Option B).** In
   `_matches_chat_allowlist` (runtime.py:1326-1328) also test
   `entity.usernames` entries where `active is True`, lowercased. Update the
   `is_chat_allowed` docstring and note the `min`-entity caveat (missing
   `usernames` simply means no match, fail closed). Add tests: secondary
   active username matches in listings and chat tools; inactive does not;
   channel with secondary username.
6. **Docs, commit 4.** README "Chat Access Privacy" (username given to the tool
   works even when it is a secondary username; if Option B, the list-matching
   rule), `docs/arch/overview.md` "Access control layers" one line. No new ADR:
   ADR 0002 stands; flag in the task only if Option B changes the matching
   model enough that the owner wants one.

## Files likely touched

- `telegram_mcp/runtime.py`
- `telegram_mcp/tools/profile.py`, `chats.py`, `contacts.py`
- `README.md`, `docs/arch/overview.md`
- `tests/test_peer_allowlist.py` (and `tests/test_chat_allowlist.py` for Option B)

## Tradeoffs considered

- Identifier passed down (chosen) vs. matching every `entity.usernames` for all
  tools: the first grants only what the caller named and resolved, so no change
  to listings; the second is consistent everywhere but widens the match set
  (see the task's owner decision).
- Optional `identifier` on `peer_allowed` vs. a new function: one extra
  parameter keeps the many existing callers untouched.
- Trusting the identifier string: safe for `ResolveUsernameRequest` and for
  `resolve_entity` of a handle-like string; `is_handle_like` blocks alias words
  that `resolve_entity` could map to a different user.

## Gates

`uv run flake8 --select=E9,F63,F7,F82 <touched>`, `uv run black --check
<touched>`, `uv run pytest --cov --cov-report=term-missing` (fail_under 80).
`runtime.py` is a security-shaped path: security review before the PR.
