# Plan 0009 Batch read of messages by id (get_messages_by_ids)

Task: `docs/tasks/0009-batch-get-messages.md`.

Base: `ours` at f7c68ef (includes tasks 0007 structured errors and 0008
`get_history` paging and `truncated`).

## Approach

Add one new read-only tool next to `get_history` in
`telegram_mcp/tools/messages.py`; do not change any existing tool. Reuse
`message_to_dict`, `format_tool_result`, `topics_for_messages` /
`topics_metadata`, `transcription.prefetch_transcripts` (the same calls
`get_history` makes) so records match.

1. Decorators, in the order used by `get_history`:
   `@mcp.tool(annotations=ToolAnnotations(title="Get Messages By Ids",
   openWorldHint=True, readOnlyHint=True))`, `@with_account(readonly=True)`,
   `@validate_id("chat_id")`. Signature:
   `get_messages_by_ids(chat_id: Union[int, str], message_ids: list[int],
   account: Optional[str] = None) -> str`. Module constant
   `MAX_BATCH_IDS = 100`. Check how the FastMCP schema renders `list[int]` and
   that a JSON array arrives as a list (existing tools with list params show the
   pattern).
2. Validation helper `_parse_batch_ids(message_ids) -> tuple[list[int] | None,
   str | None]`, run before `get_client` / `resolve_entity`:
   - not a list/tuple, or empty -> "Error: message_ids must be a non-empty list
     of positive integers."
   - any item that is not an `int` (reject `bool` explicitly, floats, strings),
     or `< 1` -> same message family naming the rule.
   - dedupe keeping first occurrence (`dict.fromkeys`), then if more than
     `MAX_BATCH_IDS` -> "Error: at most 100 message ids per call."
   On error the tool calls `record_tool_error("invalid_argument")` and returns
   the string (same as `get_history`). Keeping this in a helper keeps the tool
   under the C901 limit.
3. Fetch: `raw = await cl.get_messages(entity, ids=ids)`. Normalise: if the
   result is not a list (a stub or a one-id edge returns a single `Message` or
   `None`), wrap it (`[]` for `None`, `[raw]` otherwise). Pair with `ids` by
   position when lengths match; if a length mismatch ever occurs, fall back to
   matching by `msg.id` so no id is attributed to the wrong message. A slot is
   "missing" when it is `None`, or has no usable content (`MessageEmpty`
   instance / `getattr(msg, "id", None)` not in the requested set).
4. Output: found messages in request order -> `message_to_dict(msg,
   numeric_chat_id, forum)`; `missing_ids` is the list of requested ids with no
   found message, in request order. Metadata: `{"missing_ids": [...]}` merged with
   `topics_metadata(topics)` (key order: `results`, `topics`?, `missing_ids`,
   or `results`, `missing_ids`, `topics`; pick one and pin it in a golden test).
   `format_tool_result(records, metadata=metadata)`: metadata is non-empty
   (`missing_ids` is always present, possibly `[]`), so the key is always
   emitted.
5. Transcripts: call `transcription.prefetch_transcripts(cl, entity,
   numeric_chat_id, found)` only if `get_history` does so unconditionally for the
   same record shape (it does, line 3106); keep parity so voice records are
   rendered the same way.
6. Errors: `except Exception as e: return log_and_format_error(
   "get_messages_by_ids", e, chat_id=chat_id, ids=len(message_ids)...)`; log the
   count, not the whole list, and make sure the logging argument tolerates a
   non-list input that reached the handler.
7. Docstring (model-visible): arguments, request order, `missing_ids` meaning
   (deleted, non-existent, or in another chat), max 100, no reply context (use
   `get_message_context`), per-account blocks on fan-out so pass `account`,
   `truncated` meaning in one line, untrusted-content note. Mention in the
   `get_message_context` docstring nothing (unchanged).
8. Docs: `docs/arch/overview.md` short subsection or table row; README tool list
   if it enumerates tools (check; README is not a security path). `CLAUDE.md`
   needs no change.

Allowlists: `get_history` has no inline `is_chat_allowed` call, so access is
enforced through the shared path (`validate_id` / `resolve_entity` in
`runtime.py`). Do not add a second check; add a test that a chat outside the read
allowlist is refused and the client's `get_messages` is never called. If the test
shows the shared path does not cover this tool, stop and report: `runtime.py` is
a security-shaped path and that would be a separate decision.

## Steps

1. Tests first (see strategy), failing.
2. Validation helper and constant.
3. The tool: fetch, normalise, split found/missing, output, docstring.
4. Docs: arch overview (and README if it lists tools).
5. Gates; review (the tool reads chat content behind the allowlists and the
   sanitizer, so run the review gate even though no security-shaped file is
   edited, to confirm allowlist enforcement and the absence of unsanitized
   fields).

## Files likely touched

- `telegram_mcp/tools/messages.py` (new tool, `_parse_batch_ids`,
  `MAX_BATCH_IDS`)
- `docs/arch/overview.md`, README (tool list, if present)
- New `tests/test_get_messages_by_ids.py`

Not touched: `runtime.py`, `sanitize.py`, `get_message_context`, `get_messages`,
`get_history`, transport, `telegram_mcp/tools/__init__.py` (the module is
already imported).

## Tradeoffs considered

- New tool vs `ids` on `get_messages` / `get_history`: a separate tool avoids two
  output formats (text lines vs JSON) and two exclusive parameter sets in one
  tool, and leaves existing JSON/text untouched (task decision 1).
- Strict list-of-ints input vs lenient parsing: strict is easy to test and
  matches how MCP clients send arrays (decision 7).
- Cap 100 = Telethon `_MAX_CHUNK_SIZE`: one request per call, predictable
  latency; larger caps would trigger Telethon's 10 s inter-chunk wait for
  `limit > 300` and long tool calls (decision 5).
- `missing_ids` always present vs only when non-empty: always present is one
  fixed shape for the consumer; the cost is one extra key in the all-found case.
  New tool, so no compatibility concern.
- Missing as success vs `not_found` error for all-missing: keeps one response
  shape and follows ADR 0003's rule that empty results are plain successes
  (decision 3).
- Request-order output vs sorted: matches Telethon's alignment and lets the
  consumer correlate; sorting would add logic for no stated need (decision 6).
- Not changing `get_message_context`'s not-found text: text contract stability
  (ADR 0003) beats a consistency cleanup; structured errors already give a
  machine-readable kind to opt-in consumers (decision 2).
- Fetching replied messages: an extra request per message; deferred (decision 4).

## Test strategy

All under `tests/`, stub clients only (same style as
`tests/test_get_history_topic.py` and the task 0008 tests), never real Telegram.
The fake `get_messages(entity, ids=...)` must behave like Telethon: return a list
aligned with `ids`, `None` for ids not in its store, record every call and the
ids it received; a variant returns a single object / `None` for the one-id edge.

- Happy path: three ids, all present -> `results` in request order,
  `missing_ids: []`, one `get_messages` call with `ids=[...]` as a list, record
  keys equal those of `message_to_dict` for the same messages (compare against
  `get_history` output on the same stub).
- Order: request `[5, 2, 9]` -> records 5, 2, 9 (not sorted).
- Partial missing (the 7763 scenario): ids `[7760, 7763, 7764]` with 7763 absent
  -> two records, `missing_ids: [7763]`. All missing -> `results: []`,
  `missing_ids` full, and under `TELEGRAM_STRUCTURED_ERRORS=1` the result is not
  `isError`.
- Duplicates: `[3, 3, 4]` -> client receives `[3, 4]`, one record for 3.
- Validation (parametrised): `[]`, `None`, `"1,2"`, `5`, `[0]`, `[-1]`, `[1.5]`,
  `["1"]`, `[True]`, 101 distinct ids -> error string, `invalid_argument`
  recorded (assert via the recorder or the real call path with
  `TELEGRAM_STRUCTURED_ERRORS=1` like the 0007 tests), zero client calls.
  Exactly 100 ids accepted; 100 distinct plus duplicates accepted.
- Length-mismatch fallback: stub returns fewer items than requested -> matching
  by `msg.id`, no misattribution.
- `MessageEmpty`-like object returned in a slot -> goes to `missing_ids`.
- Sanitization: a message with control characters / injection-looking text and a
  hostile sender name comes out sanitized as in `get_history`; a 5000-character
  text carries `truncated: true`.
- Forum chat: records carry `topic`, and the `topics` map is present.
- Allowlist: with a read allowlist excluding the chat, the call is refused and
  `get_messages` is not called; an allowed chat works.
- Annotations: tool is registered with `readOnlyHint=True`, appears in the
  read-only exposed set, and is single-wrapped (see the task 0008 regression
  test for the duplicated-decorator bug). Fan-out with two stub accounts gives
  two blocks with their own `missing_ids`.
- Existing tests unchanged and passing.

## Gates

Per the Pipeline contract:

- `uv run flake8 --select=E9,F63,F7,F82 <touched files>` (blocking) and
  `uv run flake8 <touched files>` with no new warnings versus base (watch C901
  and E501; keep validation and splitting in helpers).
- `uv run black --check <touched files>`
- `uv run pytest --cov --cov-report=term-missing` (80% gate).
- Review: confirm allowlist enforcement for the new tool, that every
  user-controlled string goes through `message_to_dict` / `sanitize.py`, and that
  no existing tool's output changed.
