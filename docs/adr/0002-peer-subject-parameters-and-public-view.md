# 0002. Peer-subject parameters are checked as the user's private chat, and outside-read peers get a public view

- **Status:** accepted
- **Date:** 2026-11-09

## Context

The chat allowlists (`TELEGRAM_ALLOWED_CHAT_IDS`, `_READ_CHATS`, `_WRITE_CHATS`)
only covered tools taking a chat parameter (`CHAT_PARAM_NAMES`). Tools that list
the contact book or name a single user (`list_contacts`, `resolve_username`,
`get_full_user`, `block_user`, `delete_contact`, ...) returned names, phones,
bios or status of people outside the allowlist, or acted on them. The README
said other conversations stay completely invisible.

`user_id` cannot simply join `CHAT_PARAM_NAMES`: in `ban_user`, `promote_admin`,
`invite_to_group` and the like it names a member inside an already-checked
chat, who is rarely on any list.

## Decision

- A parameter that names a user the tool looks up or acts on is a
  peer-subject parameter. It is checked like a chat: the user's private chat
  (marked id = user id, or username after resolving) must be allowed in the
  tool's mode (read for `readOnlyHint` tools, write otherwise). Implemented by
  `validate_id(..., peer_subject=True)`, `require_peer_access` and
  `deny_peer_param` in `runtime.py`. `"me"` is always allowed as a sender.
  Member `user_id` parameters of group-admin tools stay unchecked.
- Whole-contact-book tools (`list_contacts`, `search_contacts`,
  `export_contacts`, `get_contact_ids`, `get_blocked_users`) are refused with
  the standard privacy error while read access is restricted: a chat allowlist
  is set and the effective read list has no `*`. With read `*` nothing is
  hidden, so they work even when the write list is narrow.
- Lookups of one peer outside read access return a public view built from an
  explicit field list: id, type, name or title, username(s), bot flag, badges.
  Phone, bio, photos, status, birthday, personal channel, common-chats count,
  contact flags and bot descriptions are never included. Tools that would only
  return such fields (`get_user_photos`, `get_user_status`) are refused.
- `add_contact` by phone and `import_contacts` stay open (owner decision); user
  details they return follow the public-view rule.
- `tests/test_peer_allowlist.py` lists every tool with a peer-naming
  parameter and fails when a newly registered one is not classified.

## Consequences

- The contact tools are unusable under any narrow read allowlist; a user who
  needs them must grant read `*`.
- Resolving a peer by name still works outside the allowlist, but yields only
  public data. `resolve_phone` spends its quota anyway.
- A numeric id that is not on the list is not resolved to a username entry:
  usernames on the list match only when the tool is given the username (or the
  alias resolves to it), as for chat parameters.
- `list_contact_aliases` matches stored alias ids only; username entries do not
  widen it.
- `get_send_as` is unchanged: it can list sender identities (for example owned
  channels) that are not on the allowlist.
- Deviation: `forward_message.send_as`, which the task inventory did not list,
  is treated like `set_default_send_as.send_as` (write access to the sender).

## Rejected

- Adding `user_id` to `CHAT_PARAM_NAMES`: breaks the member tools in allowed
  chats.
- Filtering the contact tools instead of refusing them: needs per-entry matching
  of ids and usernames, and leaks through counts and ordering; refusing is
  simpler and owner-approved.
- Building the public view by stripping fields from the full output: new
  Telethon fields would leak by default.
