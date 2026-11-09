# 0004 Allowlisted handle must not resolve to a chat found by display name

## Goal

Under a chat allowlist, a tool that checked the allowlist on the raw string
(`@foo`) must act on the chat that `@foo` names, never on a different cached
chat whose display name or title happens to be `foo`. Today `resolve_input_entity`
(and Telethon calls that use `get_input_entity` internally) can return such a
chat. Not reachable without an allowlist in any way that matters (see "Why").

Origin: round 5 deep review of task 0003. Mechanism verified here by reading
`telegram_mcp/runtime.py` and Telethon 1.45 in `.venv` (no code was run in this
analysis step, so the first plan step is a reproducing test that must fail
before the fix).

## Why: verified mechanism

Telethon `client/users.py` `get_input_entity(str)` (line 354-480), in order:

1. `utils.get_input_peer`, `_mb_entity_cache` (ints and Peer objects only),
   `'me'`/`'self'`: not relevant for handle strings.
2. **`self.session.get_input_entity(peer)` (line 438), before any network
   call.** `sessions/memory.py` `get_input_entity` (199-246), for a `str`:
   phone -> `get_entity_rows_by_phone`; otherwise `parse_username`; if it
   yields a username -> `get_entity_rows_by_username(username)`; and then, **if
   `result` is still empty, unconditionally** (line 232-233)
   `get_entity_rows_by_name(key)` with the raw key. The name column is
   `utils.get_display_name(e)`: first[+last] name for users, `title` for
   chats and channels. The match is exact and case sensitive on the raw string
   (including a leading `@` if the caller typed one). First hit in an
   unordered set wins.
3. Only if the session raises `ValueError` does it go to the network:
   `_get_entity_from_string` -> `ResolveUsernameRequest`.

So the display-name fallback is reachable, for a **valid** username string,
**on every username cache miss, before `ResolveUsernameRequest` is tried**. It
is not limited to strings `parse_username` rejects. A cache miss happens
(a) always for a secondary/collectible username (session rows store only
`entity.username`, memory.py:128-130), and (b) for any primary username whose
holder is not in the session cache yet. A hit needs a cached peer (with an
access hash) whose display name/title equals the string the tool received, e.g.
a group titled `foo` or a contact whose first name is `foo`.

`get_entity(str)` (`resolve_entity`) differs: `users.py:285-298` sends strings
straight to `_get_entity_from_string`, which calls `ResolveUsernameRequest`
first and turns `UsernameNotOccupiedError` into `ValueError` (line 564-566)
without consulting names. The display-name lookup (576-581) is reached there
only when `parse_username` returns no username (`abcde_`, `1abcde`, `ab__cde`,
over 32 characters, all of which pass `_HANDLE_RE` in `is_handle_like`) or in the
rare "result lacks the peer" case. So `resolve_entity` is safe for a real
username and exposed only for such malformed handles.

Allowlist side (`runtime.py`): `validate_id` (1896-1905) calls `deny_peer_param`
-> `_peer_item_allowed` (1565-1583), which returns True on
`is_chat_allowed(item, access=...)` (string match, 1308-1316) **without
resolving**. The tool then resolves the same string. The mismatch exists only
when the string itself matched the allowlist, so the exposure is exactly "an
allowlisted handle". `_peer_item_allowed`'s resolve-then-check branch (strings
that did not match) uses `resolve_entity` and is not exposed for real usernames.

Impact: a write (or read) lands on a chat the allowlist does not name, in
tools that take the peer from `resolve_input_entity` or from a Telethon call
that resolves a string internally. Needs the attacker (or noisy data) to place
a chat with that exact name in the account's cache, and the allowlisted chat
itself to be uncached or secondary-named. Real but narrow; ADR 0002's
"holder of the username" trust model is what is being violated.

## Inventory

File:line at the base commit cbdfecf. "Gate" = how the allowlist is checked
on the string before the resolve.

Affected (string allowlist match, then resolution that can hit the name
fallback):

| Tool / helper | Resolve site | Gate |
|---|---|---|
| `get_send_as` | messages.py:1807 `resolve_input_entity(chat_id)` | `validate_id("chat_id")` |
| `set_default_send_as` | messages.py:1862 (`chat_id`), 1865 (`send_as`) | `validate_id` on both (`send_as` with `peer_subject`) |
| `forward_message` (`send_as`) | messages.py:1965 | `require_peer_access("send_as")` (same string shortcut); `from`/`to` use `resolve_entity` |
| `send_typing` | messages.py:2506 `resolve_input_entity(chat_id)` | `resolve_entity` at 2494 is checked, but 2496 passes on the string alone and the peer actually used comes from a second, different lookup |
| `send_reaction` | messages.py:3254 | `validate_id` |
| `remove_reaction` | messages.py:3291 | `validate_id` |
| `get_message_reactions` | messages.py:3329 | `validate_id` |
| `save_draft` | messages.py:3428 | `validate_id` |
| `clear_draft` | messages.py:3554 | `validate_id` |
| `add_chat_to_folder` | folders.py:797 | `validate_id`; folder gets the wrong chat |
| `remove_chat_from_folder` | folders.py:899 | `validate_id` |
| `create_folder` (`chat_ids`) | folders.py:696 | own check 701-709: `is_chat_allowed(chat_id)` string OR resolved peer id; the string branch passes while the peer is the impostor |
| `mute_chat`, `unmute_chat` | chats.py:1153, 1196 | `validate_id`; only the `except (ImportError, AttributeError)` fallback uses `resolve_input_entity`, main path uses `resolve_entity` |
| `get_participants` | groups.py:343 `cl.iter_participants(chat_id)` (Telethon `client/chats.py:260` calls `get_input_entity(str)`) | `validate_id("chat_id", raise_errors=True)`; bypasses `resolve_input_entity`, so a fix in the resolver alone does not cover it |
| `get_recent_actions` | groups.py:1829-1831 raw `GetAdminLogRequest(channel=chat_id)`; Telethon resolves the `channel` string through `get_input_entity` in the request's `resolve` | `validate_id("chat_id")`; same bypass |

Not affected:

- Every tool that resolves with `resolve_entity` and passes the entity object
  on (messages.py `send_message`, `get_messages`, `edit_message`, ...; all of
  groups.py, media.py, events.py, contacts.py, profile.py, the rest of
  chats.py), and `_wait_target` (events.py:90-98). Real username -> 
  `ResolveUsernameRequest`, no name lookup. Residual exposure only for
  malformed handles (see "Out of scope" / plan step 5).
- `update_folder` (folders.py:362): peers are int ids and the result is
  checked with `_stable_peer_id(peer) != peer_id` (369).
- `transcription.py:455` and `chats.py:889`: `get_input_entity(entity)` gets an
  already resolved entity object, not a string. `chats.py:27,99,101` are the
  custom raw requests' `resolve`; their callers (chats.py:278, 465) pass
  entities.
- `deny_peer_param` / `_peer_item_allowed` themselves: the string shortcut is
  the gate, not the leak; the leak is the later resolution.

Unclear (not verified, noted for the reviewer): positive bare integer ids go
through `get_input_entity(int)` -> `_mb_entity_cache.get(bare id)` and
`session.get_entity_rows_by_id(..., exact=False)`, which can match a user, chat
or channel with the same bare number. Same class of "resolver is looser than
the allowlist" problem, different mechanism (id namespaces), not the subject of
this task. Worth a separate look.

## Scope

- Under an enabled chat allowlist, `resolve_input_entity` does not use the
  display-name fallback for a username-shaped string: on a username cache miss
  it resolves through `resolve_entity` (`ResolveUsernameRequest`) and converts
  the entity to an input peer.
- `get_participants`, `get_recent_actions` and `send_typing` take their peer
  from the guarded resolver (or from the entity they already checked) instead
  of letting Telethon resolve the raw string.
- Tests that reproduce the mechanism with a real Telethon session cache, plus
  README / `docs/arch/overview.md` lines.

## Out of scope

- Behaviour without an allowlist: unchanged, no extra Telegram calls. (Display
  name lookups keep working there, e.g. `send_reaction("Family", ...)`.)
- Stale username cache (a cached primary username later reassigned): Telethon
  documents this; already out of scope in earlier tasks.
- The bare-integer namespace ambiguity noted above.
- Changing the string shortcut in `_peer_item_allowed` (it avoids a
  `ResolveUsernameRequest` per call; see plan, "Tradeoffs").

## Acceptance

- With an allowlist containing `foo`, a cached peer whose display name/title
  is `foo` (not holding the username) and an uncached or secondary-named real
  `@foo`, each tool in the "Affected" table acts on the real `@foo`, or fails;
  it never touches the same-named chat. Test written first, fails before the
  fix (real `telethon.sessions.MemorySession` rows + a stubbed `_call`, no
  Telegram).
- Cached primary username still resolves from the cache without a network call
  (guard does not regress the cache hit). Name-collision case does exactly one
  `ResolveUsernameRequest`.
- No allowlist: `resolve_input_entity` behaves exactly as before (same Telethon
  call, no `get_entity`, no extra calls); a test pins this.
- Ids, `me`/`self`, phones, aliases (resolved before the guard) unchanged.
- Existing tests that mock `resolve_input_entity` keep their intent after the
  `get_participants` / `get_recent_actions` / `send_typing` edits.
- CLAUDE.md gates pass; `runtime.py` is a security-shaped path: security review
  before the PR.

## Owner decisions (decided 2026-11-09)

- 1: **A** (central guard in `resolve_input_entity` plus the call-site edits).
- 2: **gated on the allowlist** (no allowlist: unchanged).
- 3: **refuse** malformed handles under an allowlist (guard in `_resolve`).

Options as analysed:

1. **Where to enforce: central in the resolver vs. per tool.**
   - A (recommended). `resolve_input_entity` itself: under an allowlist, for a
     username-shaped string whose username is not in the session cache, skip
     Telethon's `get_input_entity` and use `resolve_entity` + `get_input_peer`.
     Plus three small call-site edits for the tools that bypass the resolver.
     Costs nothing extra in the common case: on a miss the old code also ended
     at `ResolveUsernameRequest`; a cache hit stays free.
   - B. Decide on the resolved entity in every tool (resolve first, then
     `is_chat_allowed(item, entity)`), dropping the string shortcut. Most
     explicit, but touches about 15 tools and adds a `ResolveUsernameRequest`
     per call for every allowlisted handle (flood-wait risk).
   - C. `validate_id` replaces an allowlisted string with the resolved marked
     id. Same extra call as B, changes every tool's argument and echoed text,
     and routes through integer resolution, which has its own ambiguity.
   Recommendation: A.

2. **Scope of the guard: allowlist-gated or always.**
   - Gated (recommended): matches "no allowlist -> unchanged". Under an
     allowlist, display-name addressing of chats is already unavailable for
     the `validate_id` tools (a non-matching string is resolved by username and
     checked), so nothing is lost.
   - Always: removes the quirk everywhere, but breaks no-allowlist users who
     address a chat by title (`"Family"` would become a username lookup).
   Recommendation: gated.

3. **Malformed handles (`abcde_`, `1abcde`, `ab__cde`, >32 chars) that are
   allowlisted by string.** They cannot be real usernames, so only a display
   name can ever answer them, which also affects `resolve_entity`.
   - Refuse them under an allowlist (raise `ValueError` in the shared
     `_resolve`) (recommended; tiny, a guard on the already-computed
     `_is_username_identifier`/`parse_username` result), or
   - leave as a documented limitation: an allowlist entry that is not a valid
     username or numeric id is meaningless anyway; README already tells the
     owner to prefer numeric ids.
   Recommendation: refuse; if the reviewer finds it adds risk to existing
   behaviour, document instead.

ADR: not needed (ADR 0002 stands, this enforces its model). Flag again only if
the owner picks option B or C in decision 1, which change where the
authorisation decision is made.
