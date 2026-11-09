# 0008 get_history pagination (min_id, offset_id, since_date) and truncation signal

## Goal

Requested by the owner's jeeves agent system (an SSE consumer of this server),
approved by the owner. `get_history` returns only the newest `limit` messages
and has no way to continue, so backfills and catch-up reads are awkward.
Wanted on `get_history`:

- `min_id`: return only messages with `id > min_id` (catch-up since the last
  seen message).
- `offset_id`: start before this id (return messages with `id < offset_id`),
  for paging backwards.
- `since_date`: only messages at or after this date.
- All three work together with `topic_id` (forum thread via `reply_to`) and
  `limit`.
- A signal when a message `text` was cut, so the consumer can tell a short
  message from a clipped one.
- A paging signal so a caller knows whether and where to continue.

## Findings

Line numbers are on `ours` at 47a3908 (task 0007 merged).

Current tool (`telegram_mcp/tools/messages.py:2945-3000`): `get_history(chat_id,
limit=100, account=None, topic_id=None)`; read-only
(`readOnlyHint=True`), `@with_account(readonly=True)`, `@validate_id("chat_id")`.
Without a topic it calls `cl.get_messages(entity, limit=limit)`; with one,
`cl.get_messages(entity, limit=limit, reply_to=tid)` (a bad `topic_id` records
`invalid_argument` and returns "Error: topic_id must be a positive integer.").
Output: `format_tool_result(records, metadata=topics_metadata(topics))`, i.e.
`{"results": [...], "topics": {...}?}`, newest first.

Telethon semantics (installed 1.45, `telethon/client/messages.py`
`_MessagesIter`), non-reverse (the default, newest -> oldest):

- `offset_id`: exclusive upper bound; Telegram returns ids `< offset_id`
  (lines 47, 145-154, `add_offset=0`).
- `min_id`: Telegram is not sent it (the request always has `min_id=0`, line
  150 and 102 for replies) because low values make the request return nothing.
  Telethon emulates it client-side: the iterator stops at the first message with
  `id <= min_id` (`_message_in_range`, 252). If `offset_id - min_id <= 1` it
  yields nothing at once (48-50). So `min_id` is exclusive and works the same
  with `reply_to` (the `GetReplies` request, 93-104) as without.
- `offset_date`: sent as `offset_date` to `GetHistory` / `GetReplies`, an
  exclusive upper bound ("messages older than this"), and `offset_id` takes
  priority over it. It is NOT a lower bound. A "since" bound therefore needs
  either `reverse=True` (oldest first, which flips the output order and the
  meaning of `offset_id`/`min_id`: lines 38-58) or a client-side stop while
  walking newest -> oldest. `list_messages` uses the first approach for
  `from_date` (`messages.py:1557-1568`).
- `get_messages(entity, limit=N, ...)` is `iter_messages` collected into a list;
  `limit=None` returns a single message, so `limit` must always be passed.
- `min_id` combined with `limit` returns the NEWEST `limit` messages above
  `min_id`, not the first ones after it. A forward catch-up with more than
  `limit` new messages would silently lose the oldest ones unless the caller
  pages back with `offset_id` until it reaches `min_id`. Hence the paging
  signal below is not optional.

Text truncation:

- The server-side clip is `sanitize_user_content(max_length=4096)`
  (`sanitize.py:84-132`), applied to `msg.message` in `message_to_dict`
  (`messages.py:332`) and to rebuilt rich text (339). It runs after control and
  invisible characters are stripped and cuts to 4096 characters, then appends the
  literal suffix `"... [truncated]"` inside the string. The suffix is the only
  existing signal; there is no structured flag.
- Telegram's own limit for a plain message is 4096 characters (and shorter for
  captions), and the sanitized string is never longer than the original, so an
  ordinary message cannot hit the server clip. The clip fires for text longer
  than 4096 after sanitation: in practice rich/block-format posts rebuilt from
  `rich_message` (the `rich: true` records) and any other source that can exceed
  the Telegram limit. A consumer that sees text ending at about 4096 characters
  is seeing this clip (the marker is in the text), not a Telegram cut. If a
  message is exactly at Telegram's limit it is not touched by the server.
- The flag must reflect only the server-side clip. Telegram's own length limit
  is not a truncation of data we hold, so it is not flagged.
- Text-sniffing the `"... [truncated]"` suffix is not reliable (user text can end
  with the same string), so the signal has to come from the sanitizer itself.
  `sanitize.py` is a security-shaped path; the change there must be purely
  additive and leave the output of `sanitize_user_content` byte-for-byte equal.

Date parsing: `list_messages` parses `from_date` / `to_date` with
`datetime.strptime(value, "%Y-%m-%d")`, UTC-aware, and returns "Invalid
from_date format. Use YYYY-MM-DD." after `record_tool_error("invalid_argument")`
(`messages.py:1495-1526`). `since_date` reuses the same format and UTC
interpretation (start of that day, inclusive).

Fan-out: with `account` omitted the read-only tool runs on every account and the
outputs are merged as `[label]\n<text>`. Ids are per chat per account view, so
paging fields stay inside each account's own JSON block; callers that page
should pass `account`.

## Proposed behaviour

New optional parameters on `get_history` (all default `None`, so omitted means
the code path and JSON are exactly as today):

| param | type | meaning |
|---|---|---|
| `min_id` | int >= 0 | only messages with `id > min_id` |
| `offset_id` | int >= 0 | only messages with `id < offset_id` (0 = same as omitted) |
| `since_date` | `YYYY-MM-DD` | only messages with `date >= 00:00 UTC` of that day |

Rules:

- Order stays newest -> oldest in every combination. `limit` keeps meaning "at
  most N messages", and with `topic_id` it counts messages of the topic.
- Validation before any network call: `min_id` and `offset_id` must be
  integers >= 0, `since_date` must parse as `YYYY-MM-DD`. A failure records
  `record_tool_error("invalid_argument")` and returns a plain "Error: ..." /
  "Invalid since_date format. Use YYYY-MM-DD." string (same style as the
  `topic_id` check and `list_messages`). `limit` validation is only added for
  the new-parameter path (see decision 5); the legacy path is untouched.
- Implementation uses `cl.iter_messages(entity, limit=..., offset_id=...,
  min_id=..., reply_to=tid)` and stops client-side when `msg.date <
  since_date`. It does not use `offset_date` or `reverse`.
- Paging signal: when at least one of the new parameters is supplied, the
  response gains two top-level fields next to `results` (and `topics`):
  `has_more` (bool) and `next_offset_id` (id of the oldest returned message, or
  `null` when `has_more` is false or the page is empty). To continue the caller
  repeats the call with `offset_id=next_offset_id` and the same other
  parameters. `has_more` is exact: the iterator is asked for `limit + 1`
  messages and the extra one is dropped. When no new parameter is supplied the
  fields are absent and the JSON is identical to today.
- Truncation: a record gets `"truncated": true` only when the server-side
  sanitizer clipped its text (key absent otherwise, the omit-empty convention
  of `message_to_dict`). The clip itself and its in-text marker are unchanged.
  The flag lives in `message_to_dict`, so any tool that serialises messages
  through it gains the key in the same rare case (see decision 2).

## Scope

- `get_history` parameters, validation, paging fields, docstring (tool
  description visible to the model).
- `truncated` flag via an additive sanitizer helper and `message_to_dict`.
- Tests with stub clients, `docs/arch/overview.md` short note.

## Out of scope

- Other tools (`list_messages`, `search_messages`, `get_pinned_messages`) get no
  new parameters. The batch `get_messages` by ids is task 0009.
- Raising or removing the 4096 clip, or returning the full text of long posts.
- Oldest-first ordering (`reverse`), `max_id`, `add_offset`, `to_date`.
- Changing the success JSON when the new parameters are omitted.
- `since_date` with a time of day (see decision 3).

## Acceptance

- With none of `min_id`, `offset_id`, `since_date` supplied, `get_history`
  calls `get_messages` exactly as before and returns byte-identical JSON
  (existing tests in `tests/test_get_history_topic.py`,
  `tests/test_message_topics.py` pass unchanged).
- `min_id=N` returns only ids > N, `offset_id=M` only ids < M, `since_date=D`
  only messages dated >= D 00:00 UTC; the three combine (intersection) and
  combine with `topic_id` and `limit`, newest first.
- A catch-up with more than `limit` new messages: first page has
  `has_more: true` and `next_offset_id`; repeating with `offset_id=next_offset_id`
  walks back to `min_id` with no gap and no duplicate, and the last page has
  `has_more: false`, `next_offset_id: null`.
- `has_more` is false when exactly `limit` messages remain; true when
  `limit + 1` exist. Empty page: `results: []`, `has_more: false`.
- Invalid `min_id` / `offset_id` (negative) and `since_date` (bad format,
  including a full datetime) return an error string, record `invalid_argument`
  (visible as `_meta` kind with `TELEGRAM_STRUCTURED_ERRORS=1`), and make no
  Telegram call.
- A message whose cleaned text exceeds 4096 characters is returned with
  `"truncated": true` and the unchanged `... [truncated]` text; a message of
  exactly 4096 characters and a short message carry no `truncated` key; a user
  text that merely ends with `... [truncated]` is not flagged.
- `sanitize_user_content` output is unchanged for all inputs (existing
  `tests/test_sanitize.py` passes; a new equivalence test over edge strings).
- Annotations unchanged (`readOnlyHint=True`), fan-out with `account` omitted
  still works (each block carries its own paging fields).
- Untrusted content handling unchanged: all text still goes through
  `sanitize.py`; the new fields hold only a bool and an int.
- Tests under `tests/`, stub clients only. Gates per CLAUDE.md pass. Touches a
  security-shaped path (`sanitize.py`), so the review gate applies.
- `docs/arch/overview.md` gets a short note on `get_history` paging and the
  `truncated` flag.

## Owner decisions (with recommendations)

1. Paging signal shape. Options: (a) `has_more` + `next_offset_id` top-level
   fields, present only when a new parameter is used; (b) always present (changes
   today's JSON); (c) `next_offset_id` only, with `has_more` inferred from
   `len(results) == limit`. Recommendation: (a). Exact via a `limit + 1` peek
   (one extra tiny request when `limit` is a multiple of 100), and no change for
   existing callers.
2. Scope of `truncated`. Options: (a) put it in `message_to_dict`, so every tool
   built on it can show the key when text was clipped (additive, only in the
   rare clipped case); (b) only `get_history` records. Recommendation: (a): one
   code path, omit-empty like the other keys, and a consumer that reads
   `list_messages` has the same blind spot. It is the one deliberate exception
   to "same JSON" and applies only to records that were clipped.
3. `since_date` granularity. Options: (a) `YYYY-MM-DD` only (UTC midnight),
   matching `list_messages`; (b) also accept an ISO datetime. Recommendation:
   (a); precise resumption is what `min_id` / `offset_id` are for. Revisit if
   jeeves needs time of day.
4. Ordering. Options: (a) keep newest first everywhere and page backwards;
   (b) add `oldest_first`/`reverse` for forward backfill. Recommendation: (a). It
   covers backfill and catch-up through `has_more`/`next_offset_id`, keeps one
   output order, and avoids the `reverse` semantic flips (`offset_id`,
   `min_id`, `offset_date`) and the search-in-reverse pitfall. Add (b) as a
   separate task if the consumer needs to process oldest to newest per page.
5. `limit` validation. Today `limit` is not validated (0 returns an empty list,
   a huge value walks the whole chat). Options: (a) leave the legacy path alone
   and reject `limit < 1` only when a new parameter is used (the peek needs a
   positive limit); (b) validate always (changes behaviour for `limit=0`);
   (c) also cap `limit` (for example 1000) on the new path. Recommendation: (a)
   plus no cap now, since a cap changes backfill ergonomics and is a separate
   policy call.
6. Where the "Telegram's own limit" ends. Recommendation: document in the tool
   docstring and overview that `truncated` means only the server clip at 4096
   characters (marker `... [truncated]` in the text); there is no flag for
   Telegram's own limit and none is needed.

## Decisions (coordinator on the owner's behalf, 2026-11-10)

All six recommendations accepted: 1a (`has_more` + `next_offset_id` only on
the new-parameter path, `limit+1` peek), 2a (`truncated` set in
`message_to_dict`, key present only on clipped records), 3a (`since_date` is
`YYYY-MM-DD` only, UTC midnight, inclusive), 4a (newest first only; an
oldest-first option only if jeeves asks, as a separate task), 5a (`limit < 1`
rejected only on the new path, no cap), 6 yes (document that `truncated`
covers the server clip at 4096 only).

Two additions:

1. When iteration stops at `since_date`, `has_more` is false (and
   `next_offset_id` null) even if the `limit+1` peek returned a message;
   covered by a test.
2. The `get_history` docstring and README state explicitly that with fan-out
   (`account` omitted) the paging fields sit inside each account's block, so a
   caller that pages must pass `account`.

## ADR flag

No new architectural decision: parameters and a response field on one tool plus
an additive sanitizer helper. The only cross-cutting point (the `truncated` key
reaching other tools through `message_to_dict`) is covered by decision 2 and
the overview note; an ADR is not warranted unless the owner wants the output
contract ("paging fields only when requested") recorded.
