# 0002 Close chat-allowlist coverage gaps (contacts, resolvers, user_id tools)

## Goal

Make the chat allowlists (`TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ALLOWED_READ_CHATS`,
`TELEGRAM_ALLOWED_WRITE_CHATS`) cover every tool that lists or names a specific
peer outside `validate_id`'s chat parameters. Finding 7 of the security review.
Owner decisions are recorded below; the README promise is adjusted where the
owner chose a public view instead of full invisibility.

## Why

README "Chat Access Privacy" says other conversations "remain completely
invisible". Several tools return metadata (names, usernames, phones, bios,
status, photo ids) about chats and users outside the allowlist, or act on such
users, without any check. None return message content, but the contact book and
the ability to resolve any user are a privacy leak, and write-style tools
(`block_user`, `delete_contact`, ...) can act on peers the agent was never
granted.

## Inventory (from source; the tool list was not imported at runtime)

Built by reading `telegram_mcp/tools/*.py`; the analyst environment had no
shell. The coder must re-verify by importing the server with dummy env
`TELEGRAM_API_ID=1 TELEGRAM_API_HASH=x TELEGRAM_SESSION_NAME=t`, listing
`runtime.mcp._tool_manager.list_tools()`, and deleting any `t.session*`. A test
must fail when a registered tool with a peer-naming parameter is in neither the
checked set nor an explicit exempt set.

Already covered, no change: every tool taking `chat_id`, `from_chat_id`,
`to_chat_id`, `channel`, `contact_id`, `group_id` through `@validate_id`;
`list_chats`, `get_chats`, `search_public_chats`, `search_global`, `get_drafts`,
`get_common_chats` (results), `get_contact_chats`, `get_last_interaction`,
`get_direct_chat_by_contact`, folder tools, `get_chatlist_invite` (hides
unallowed peers already), event tools.

- **Group A, contact-book listings (read-only, unfiltered):** `list_contacts`,
  `search_contacts` (incl. local alias hits), `export_contacts`,
  `get_contact_ids`, `get_blocked_users`, `list_contact_aliases`.
- **Group B, lookups on one named peer (read-only):** `resolve_username`,
  `get_full_user`, `get_bot_info`, `resolve_phone`, `get_user_photos`,
  `get_user_status`, `get_privacy_settings` (rules name users).
- **Group C, acts on a peer (write):** `block_user`, `unblock_user`,
  `delete_contact`, `set_privacy_settings` (`allow_users`, `disallow_users`),
  `add_contact`, `import_contacts`, `set_bot_commands`,
  `set_default_send_as` (`send_as`), `set_contact_alias` (`chat_id` is in
  `CHAT_PARAM_NAMES` but the tool lacks `@validate_id`; it returns
  `format_entity` incl. phone).
- **Group D, user_id names a member inside a checked chat (MUST stay
  allowed):** `ban_user`, `unban_user`, `remove_user`, `promote_admin`,
  `demote_admin`, `edit_admin_rights`, `get_member_admin_status`,
  `invite_to_group` (`user_ids`).
- **Group E, no peer lookup, leave as is:** `get_me`, `list_accounts`,
  `update_profile`, profile photo tools, folder limits, stickers/GIFs/custom
  emoji, `prepare_upload`/`upload_file`, incoming-feed toggles,
  `list_forum_topic_icons`, `get_send_as`.

## Decisions (owner, made)

1. **Group A: refuse entirely.** `list_contacts`, `search_contacts`,
   `export_contacts`, `get_contact_ids`, `get_blocked_users` return the standard
   privacy-policy error (`check_chat_access` style) whenever read access is
   restricted, before any Telegram request. "Restricted" = the effective read
   list `get_chat_allowlist("read")` is not None and does not contain the `*`
   wildcard. Owner follow-up (2026-11-09): with read access to every chat
   (`TELEGRAM_ALLOWED_READ_CHATS=*` or `TELEGRAM_ALLOWED_CHAT_IDS=*`) nothing is
   hidden, so the contact tools work even if the write list is narrow. No
   allowlist: unchanged.
2. **Group B: public view for targets outside read access.** One shared helper
   builds the "public view". Allowed fields only: id, type (user, bot, group,
   channel), first/last name or title, username(s), bot flag, and badges
   (verified, scam, fake, premium and similar). Never shown for a target outside
   read access: phone, about/bio, photos, online status / last seen, birthday,
   personal channel, common-chats count, contact / mutual-contact flags, or any
   field not in the allowed list. Targets inside read access: unchanged full
   output.
   - `resolve_username`, `get_full_user`: public view outside read.
   - `get_bot_info`: public view outside read (bot description and commands
     count as bio-like and are omitted).
   - `resolve_phone`: return the public view for outside-read users; the lookup
     quota is still spent (accepted).
   - `get_user_photos`, `get_user_status`: return nothing beyond public data, so
     refuse (standard privacy error) outside read access.
   - `get_privacy_settings`: users named in the rules that are outside read
     access appear as the public view only.
3. **Group C: write access required** to the user's private chat (marked id =
   user id) for `block_user`, `unblock_user`, `delete_contact`,
   `set_privacy_settings`, `set_bot_commands`, `set_default_send_as.send_as`,
   `set_contact_alias`, and `add_contact` by username. Add the missing
   `@validate_id`/check on `set_contact_alias.chat_id`.
4. **`add_contact` by phone and `import_contacts`: allowed**, no allowlist gate on
   the action. Any user details they return follow decision 2 (public view for
   anyone outside read access).
5. **Local alias store:** `list_contact_aliases` is filtered by read access
   (`search_contacts` is refused anyway per decision 1).
6. **Traces:** new code shows no hidden counts; refusals reuse the standard
   privacy error. Folder / chatlist behaviour unchanged.
7. **`get_send_as`:** left as is; documented in the README.
8. **One task**, separate commits per group.

Group D is unchanged (constraint). `user_id` is not added to `CHAT_PARAM_NAMES`.

## Scope

Security-shaped: `telegram_mcp/runtime.py` (peer-subject check, public-view
helper), plus `telegram_mcp/tools/contacts.py`, `profile.py`, `chats.py`,
README, `docs/arch/overview.md`. A security review of the diff is expected
before merge. A user's private chat is allowed when
`is_chat_allowed(user_marked_id, user_entity, access)` holds. No allowlist
configured: behaviour unchanged, no extra Telegram requests.

## Out of scope

- `create_group` / `create_channel` and their invite lists.
- Message-content tools.
- Changing which tools are read-only.
- Filtering folder / chatlist output further.

## Acceptance

- With read access restricted (any allowlist enabled and no `*` in the
  effective read list), group A tools (except `list_contact_aliases`)
  return the privacy error and make no Telegram call; with read `*` they work
  as before even when the write list is narrow; `list_contact_aliases`
  lists only read-allowed contacts, with no hidden count.
- Group B: for an outside-read target the output contains only the public-view
  fields (tests assert phone, bio, photos, status, birthday, personal channel,
  common-chats count, contact flags, bot description are absent); inside read
  access the output is unchanged; `get_user_photos` / `get_user_status` refuse
  outside read. Username/phone inputs are checked after resolving.
- Group C: refused before any state change without write access to the user's
  private chat; `set_contact_alias` checks `chat_id`.
- `add_contact` by phone and `import_contacts` work with an allowlist set; any
  returned user detail for an outside-read user is the public view.
- Group D tools still work for a non-allowed member in an allowed chat
  (regression test per tool).
- An inventory test fails on an unclassified tool with a peer-naming parameter.
- No allowlist: every tool behaves as before, no new Telegram calls.
- README "Chat Access Privacy" and `docs/arch/overview.md` updated (public view,
  refused contact tools, `get_send_as` note). ADR `docs/adr/0002-...` written.
  Tests under `tests/`, no real Telegram; CLAUDE.md gates pass.

## Open owner decisions

None. The `TELEGRAM_ALLOWED_READ_CHATS=*` question was answered by the owner:
read `*` exempts group A (see decision 1); say so in the README.

ADR: required. The coder writes `docs/adr/0002-...`: "peer-subject parameters
are checked as the user's private chat, and outside-read peers get a public
view".
