# 0009 Batch read of messages by id (get_messages_by_ids) and the t.me/NeuralShit/7763 finding

## Goal

Requested by the owner's jeeves agent system (an SSE consumer of this server),
approved by the owner. Backfilling the posts referenced by `t.me/<chat>/<id>`
links currently costs one `get_message_context` call per link (each also fetches
`context_size` messages before and after and the replied-to message). Wanted: read
several messages of one chat by a list of ids in a single call and get the same
sanitized JSON records as `get_history`, plus a clear list of ids that do not
exist.

Second part: explain why `get_message_context` returned non-JSON for
`https://t.me/NeuralShit/7763` (a finding, recorded below; no code change
proposed for it).

## Findings

Line numbers are on `ours` at f7c68ef (tasks 0007 and 0008 merged).

### Why the non-JSON answer for NeuralShit/7763

Read-only investigation through the live server on 2026-11-10: chat NeuralShit
has ids 7760, 7761, 7762, 7764, 7765, 7766 (7761/7762 and 7764/7765 share dates,
probably albums). Id 7763 does not exist (the message was deleted).
`get_message_context` (`telegram_mcp/tools/messages.py:1752`) fetches the central
message with `cl.get_messages(chat, ids=message_id)`, which returns `None` for a
missing or deleted id, and then answers (line 1789-1791) with the plain text
`Message with ID 7763 not found in chat NeuralShit.` after
`record_tool_error("not_found")`. By default that is an ordinary successful
result (`isError` false) with a non-JSON body; with `TELEGRAM_STRUCTURED_ERRORS=1`
(ADR 0003) the same call is `isError` with `_meta["telegram-mcp/error"].kind =
"not_found"`. So the consumer got a missing-message sentence, not a crash and not
a malformed JSON. Every other not-found path of this tool family also returns
text; only the success path is JSON.

### Existing tools

- `get_messages` (`messages.py:582`) is paginated (`page`, `page_size`, via
  `add_offset`) and returns plain text lines (`format_message_line`), not JSON.
  It cannot fetch given ids and its output format differs from `get_history`.
- `get_history` (`messages.py:3028`) returns `{"results": [...], "topics"?}` built
  with `message_to_dict` and `format_tool_result`; it walks ranges, not id lists.
- `get_message_context` fetches one id plus neighbours and replied messages.

### Telethon 1.45 semantics (installed `.venv`, `telethon/client/messages.py`)

- `cl.get_messages(entity, ids=[...])` goes through `_IDsIter` (285-330). The
  result is a list aligned with the requested ids; an id that does not exist (or
  was deleted) yields `None` in its slot. A single int (not a list) returns a
  single `Message` or `None` (this is why `get_message_context` has a "fix"
  comment). For a non-channel peer, messages that belong to a different chat are
  also reported as `None` (the `from_id` check).
- Chunking: `_MAX_CHUNK_SIZE = 100` (line 10); `_IDsIter` slices the id list in
  chunks of 100 and sends one `messages.GetMessages` /
  `channels.GetMessages` request per chunk, so there is no hard failure above
  100 ids, only more requests. When `limit > 300` Telethon inserts a 10 s wait
  between chunks (flood protection), so a very large list is slow.
- If every id in a chunk is invalid for a channel, Telegram raises
  `MessageIdsEmptyError`; Telethon catches it and fills `None`s, so an all-missing
  list still returns a list of `None`.
- Order and duplicates: the result follows the order and length of the request;
  duplicates are sent as given. Our tool normalises the list before the call.

### Output and access conventions to reuse

- Records: `message_to_dict(msg, numeric_chat_id, forum)` (sanitized text and
  names, `truncated` flag since task 0008), wrapped by
  `format_tool_result(records, metadata=...)` (`sanitize.py:189`), i.e.
  `{"results": [...], ...metadata}`. `topics_for_messages` /
  `topics_metadata` add the forum `"topics"` map.
- Read tools: `@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))`,
  `@with_account(readonly=True)` (fan-out over accounts when `account` is
  omitted), `@validate_id("chat_id")`, `resolve_entity`. The per-chat allowlists
  are applied through the shared resolve/validate path exactly as for
  `get_history` (the plan tells the implementer to verify this with a test
  rather than adding a second check).
- Errors: input validation records `invalid_argument` and returns a plain
  `Error: ...` string (as `get_history`); an exception goes through
  `log_and_format_error`. Empty results are plain successes by design (ADR 0003,
  "deliberate gaps": "No ... found", empty results).

## Proposed behaviour

A new read-only tool `get_messages_by_ids(chat_id, message_ids, account=None)`.

| param | type | meaning |
|---|---|---|
| `chat_id` | int or str | chat id or username (as everywhere) |
| `message_ids` | list of int | message ids to fetch |
| `account` | str, optional | as other read tools |

Rules:

- Validation before any network call: `message_ids` must be a non-empty list
  (a JSON array; a tuple is fine for direct calls) of integers >= 1; bools,
  floats, strings and 0/negatives are rejected; more than `MAX_BATCH_IDS` (100)
  distinct ids is rejected. Failure: `record_tool_error("invalid_argument")` and a
  plain `Error: ...` string naming the rule. Nothing is sent to Telegram.
- Duplicates are removed keeping the first occurrence (a duplicate id counts once
  toward the cap and appears once in the output).
- One call: `cl.get_messages(entity, ids=ids)` with an explicit list (Telethon
  chunks by 100, so with the cap of 100 this is one request).
- Response JSON: `{"results": [records...], "missing_ids": [ids...], "topics"?}`.
  - `results` holds records for found messages only, in the order of the
    requested ids (not sorted by id, not by date). Records have exactly the
    `get_history` shape (`message_to_dict`).
  - `missing_ids` lists, in request order, the ids Telegram returned no message
    for (deleted, never existed, or not in this chat). It is always present
    (empty list when nothing is missing) so consumers do not branch on the key.
  - A message with `empty` content (service message, `MessageEmpty`) is treated
    as missing if Telethon returns it as `None`; any `MessageEmpty` object that
    slips through is also moved to `missing_ids` (check in implementation).
- All ids missing: success with `results: []` and the full `missing_ids`; not an
  error (decision 3).
- No reply context, no neighbours, no `replied_message` (decision 4); a caller
  that needs context uses `get_message_context`.
- Fan-out (`account` omitted): per-account blocks as for other read tools; ids
  are per account view, callers should pass `account`. Documented in the
  docstring.
- Docstring carries the untrusted-content note (`text`, `sender`, `topics`).

## Scope

- New tool `get_messages_by_ids` in `telegram_mcp/tools/messages.py`
  (docstring is the model-visible description), tests with stub clients,
  `docs/arch/overview.md` short note, README tool list line if the README lists
  tools.
- This task document's finding about NeuralShit/7763 (no code change).

## Out of scope

- Any change to `get_message_context`, `get_messages` or `get_history` behaviour
  or text (decision 2).
- Parsing `t.me/...` links inside the server (the consumer extracts chat and id).
- Reply context, neighbours, media download, transcripts prefetch beyond what
  `get_history` already does for the same records (see plan), a `since`/range
  mode (task 0008 covers ranges).
- Cross-chat batches (one chat per call).
- Raising the cap by looping inside the tool, retries, rate-limit policy.

## Acceptance

- `get_messages_by_ids(chat_id, [a, b, c])` returns one JSON object with
  `results` (records identical in shape to `get_history` records) and
  `missing_ids`; records appear in the order requested.
- A list containing a deleted/non-existent id returns the other messages and
  that id in `missing_ids`; all ids missing returns `results: []` and the ids in
  `missing_ids`, with `isError` false even with `TELEGRAM_STRUCTURED_ERRORS=1`.
- Duplicate ids are fetched and returned once.
- Invalid input (empty list, non-list, non-int / bool / float / string item,
  `0`, negative, more than 100 distinct ids) returns an error string, records
  `invalid_argument` (visible as `_meta` kind with
  `TELEGRAM_STRUCTURED_ERRORS=1`) and makes no Telegram call.
- Annotations `readOnlyHint=True`; included under
  `TELEGRAM_EXPOSED_TOOLS=read-only`; `with_account(readonly=True)` fan-out works.
- Chat allowlists apply as for `get_history` (a chat outside the read allowlist
  is refused, no `get_messages` call on the client).
- Text, names and topics in records are sanitized (reuses `message_to_dict`);
  `missing_ids` and any new fields hold only ints. A message text clipped at
  4096 carries `truncated: true` (task 0008 behaviour).
- Existing tests pass unchanged; new tests in `tests/` use stub clients only.
  Gates per CLAUDE.md pass.
- `docs/arch/overview.md` notes the tool and the `missing_ids` contract.
- No ADR needed (see ADR flag).

## Owner decisions (with recommendations)

1. Tool shape. Options: (a) new tool `get_messages_by_ids`; (b) add an optional
   `ids` parameter to `get_messages`; (c) add `ids` to `get_history`.
   Recommendation: (a). `get_messages` returns plain text lines and is page
   based, so an `ids` mode would make one tool return two unrelated formats and
   two parameter sets that exclude each other; `get_history` is range-oriented
   and its paging contract (`limit`, `has_more`) does not fit an id list. A
   separate name is clear to a model choosing tools and leaves existing output
   byte-identical.
2. `get_message_context` not-found text. Options: (a) keep the text, rely on
   structured errors (task 0007, opt-in) plus the new batch tool whose
   `missing_ids` is machine readable; (b) change the not-found answer to JSON
   (for example `{"results": [], "missing": true}`). Recommendation: (a). Changing
   the text breaks any consumer that already parses it (ADR 0003 kept the text
   contract for the same reason), and the batch tool removes the consumer's need
   to probe single ids.
3. All ids missing. Options: (a) `results: []` plus full `missing_ids` as a
   success; (b) `not_found` error (recorded, `isError` with structured errors).
   Recommendation: (a). A partially missing list is already a success with
   `missing_ids`; making the all-missing case an error would force consumers to
   handle two shapes, and "empty result is not an error" is the existing rule
   (ADR 0003).
4. Reply context. Options: (a) none, records only; (b) optional
   `include_replied: bool = False` adding `replied_message` like
   `get_message_context`. Recommendation: (a). Keep the tool simple; an extra
   fetch per message multiplies requests. Add (b) later only if jeeves asks.
5. Cap on ids per call. Options: (a) 100 (one Telegram request, Telethon's
   chunk size); (b) 200 or more with Telethon chunking; (c) no cap.
   Recommendation: (a) with an `Error:` text stating the limit. Larger backfills
   issue several calls; no cap risks a 10 s per-chunk throttle and long calls.
6. Output order. Options: (a) request order; (b) ascending id; (c) newest first
   like `get_history`. Recommendation: (a). Telethon already aligns results to
   the request, the caller can zip with its own list, and `missing_ids` uses the
   same order.
7. Non-list input. Options: (a) accept only a list of ints (strict); (b) also
   accept a comma-separated string or a bare int. Recommendation: (a). MCP
   clients send JSON arrays; leniency hides mistakes and widens the validation
   surface.

## Decisions (coordinator on the owner's behalf, 2026-11-10)

All seven recommendations accepted: 1a (new tool `get_messages_by_ids`),
2 keep the `get_message_context` not-found text, 3 all ids missing is a
success with `results: []` and the full `missing_ids` (no `not_found` error),
4 no reply context, 5 cap of 100 distinct ids per call (more is
`invalid_argument`), 6 request order with duplicates removed (first
occurrence kept), 7 strict `list[int]` with every id >= 1.

Conditions:

1. An allowlist test is mandatory. Without any chat allowlist the tool works
   as any read tool (backward compatible); with a read allowlist that does
   not include the chat, the call is refused with the same text as other
   read tools when `TELEGRAM_STRUCTURED_ERRORS` is off, and with
   `isError` + kind `forbidden_allowlist` when it is on. If the shared path
   does not cover the new tool, stop and report instead of editing
   `telegram_mcp/runtime.py`.
2. Any later widening of the allowlist logic or `runtime.py` for this tool
   goes back to the coordinator first.

## ADR flag

No significant architectural decision: one new read-only tool following the
existing conventions. The only contract worth noting is `missing_ids` (always
present) in the overview, not an ADR.
