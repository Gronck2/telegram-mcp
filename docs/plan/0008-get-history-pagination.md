# Plan 0008 get_history pagination and truncation signal

Task: `docs/tasks/0008-get-history-pagination.md`.

Base: `ours` at 47a3908 (includes task 0007; `record_tool_error` is available in
`telegram_mcp/runtime.py:251`).

## Approach

Keep the legacy code path untouched and add a second path that is taken only
when `min_id`, `offset_id` or `since_date` is supplied.

1. Signature: add `min_id: Optional[int] = None`, `offset_id: Optional[int] =
   None`, `since_date: Optional[str] = None` after `topic_id` (appended last so
   positional callers are unaffected). Update the docstring (it is the tool
   description) including: ids exclusive, newest-first order, how to page
   (`offset_id=next_offset_id`), `since_date` UTC day start, `truncated` meaning.
2. Validation first, before `get_client` / `resolve_entity` (no network on bad
   input): helper `_parse_history_bounds(min_id, offset_id, since_date, limit)`
   returning parsed values or an error string; on error call
   `record_tool_error("invalid_argument")` and return the string. `min_id` /
   `offset_id`: `None` or int >= 0 (reject negatives and bools). `since_date`:
   `datetime.strptime(v, "%Y-%m-%d")` with `tzinfo=timezone.utc`, the same
   parsing as `list_messages` (`messages.py:1495-1510`); extract a small shared
   `_parse_utc_date(value)` and leave `list_messages` text and behaviour as is
   (optionally call the helper there; only if byte-identical errors are kept).
   `limit < 1` rejected on this path only (decision 5).
3. `topic_id` handling stays as is (positive int check, error text unchanged).
   Resolve `reply_to=tid` once and pass it through.
4. New path fetch: 
   ```
   kwargs = {"limit": limit + 1}
   if offset_id: kwargs["offset_id"] = offset_id
   if min_id: kwargs["min_id"] = min_id
   if tid: kwargs["reply_to"] = tid
   async for msg in cl.iter_messages(entity, **kwargs):
       if since and msg.date < since: break
       fetched.append(msg)
   has_more = len(fetched) > limit
   messages = fetched[:limit]
   ```
   Uses `iter_messages` rather than `get_messages` so the date stop can end the
   walk early. No `offset_date`, no `reverse`: `offset_date` is an exclusive
   upper bound (and ignored when `offset_id` is set) and `reverse` flips order
   and the meaning of `offset_id`/`min_id`. Telethon already stops at
   `id <= min_id` and yields nothing when `offset_id - min_id <= 1`. Message
   dates are monotonic in id within a chat, so the date stop cannot skip
   matching messages. Pitfall to comment in code: `limit` must be explicit
   (`None` yields one message).
5. Output: `metadata = topics_metadata(topics) or {}`; on the new path add
   `has_more` and `next_offset_id` (`messages[-1].id` if `has_more`, else
   `None`; empty page gives `False`/`None`). Pass `metadata or None` to
   `format_tool_result`, so the legacy path keeps `metadata=topics_metadata(...)`
   unchanged. Field order: `results`, `topics`, `has_more`, `next_offset_id`.
6. `truncated`:
   - `sanitize.py`: add `sanitize_user_content_checked(text, max_length=4096, *,
     preserve_emoji=False) -> tuple[str, bool]` holding the current body and
     returning whether the length check clipped; make `sanitize_user_content`
     return `[0]`. Output of the old function is byte-identical (equivalence test).
   - `message_to_dict` (`messages.py:332-340`): use the checked variant for
     `.message` and the rebuilt rich text; when either clipped, set
     `d["truncated"] = True` (insert right after `text`, before `rich`).
     Absent otherwise. No change for texts <= 4096.
   - Not derived from the `... [truncated]` suffix (user text can end with it).
7. Error handler: extend the `log_and_format_error("get_history", ...)` kwargs
   with the new params (logging only).
8. Docs: `docs/arch/overview.md` short subsection (paging contract, ids
   exclusive, `truncated` = server clip only). `CLAUDE.md` needs no change (no
   env var, no new module).

## Steps

1. Tests first (see strategy), failing.
2. `sanitize.py`: checked helper, `sanitize_user_content` delegates.
3. `message_to_dict`: `truncated` flag.
4. `get_history`: signature, validation helper, new fetch path, paging fields,
   docstring, error kwargs.
5. Docs: arch overview.
6. Gates; security review (touches `sanitize.py`).

## Files likely touched

- `telegram_mcp/tools/messages.py` (`get_history`, `message_to_dict`, date helper)
- `sanitize.py` (additive helper)
- `docs/arch/overview.md`
- New `tests/test_get_history_pagination.py`; small addition to
  `tests/test_sanitize.py` for helper equivalence and the flag.

Not touched: `runtime.py` (recorder, `with_account`, `validate_id` as is),
`list_messages` behaviour, tool annotations, transport.

## Tradeoffs considered

- `iter_messages` + client-side date stop vs `reverse=True` + `offset_date`:
  reverse returns oldest first and shifts the `offset_id`/`min_id` semantics;
  mixing it with `reply_to` and `limit` makes "newest N since D" unobtainable
  without a second pass. The client-side stop is one code path for all
  combinations and an early exit costs nothing extra.
- `limit + 1` peek vs `len == limit` guess: the guess yields a false
  `has_more` on exact multiples and forces an empty extra round trip; the peek
  costs at most one message more (one small extra request at limit 100,
  Telethon chunk size).
- Server-side `min_id`: Telegram rejects/ignores low values (Telethon's own
  comment, `messages.py:33-35`), so the client-side stop is the supported way;
  consequence is that the iterator may fetch chunks it then discards only at the
  boundary (one chunk).
- Flag from the sanitizer vs suffix sniffing: sniffing misflags user text that
  ends with the marker; the sanitizer knows exactly when it clipped.
- `truncated` in `message_to_dict` vs `get_history` only: shared path avoids a
  second divergent text pipeline; cost is a key appearing in other tools in the
  rare clipped case (task decision 2).
- Always-present paging fields vs only on request: always-present would break
  the "same JSON" constraint.
- Per-account paging fields in fan-out: ids are only meaningful per account, so
  no attempt to merge; documented to pass `account` when paging.

## Test strategy

All under `tests/`, stub clients (same style as `tests/test_get_history_topic.py`
and `tests/test_message_topics.py`), never real Telegram. Fake `iter_messages`
should honour `offset_id`, `min_id` and `reply_to` the way Telethon does (ids
descending, exclusive bounds, stop at `id <= min_id`), so the test would fail if
the tool passed the wrong parameter.

- Backward compatibility: no new params -> `get_messages` called with the same
  arguments as before, `iter_messages` not called, JSON equals a golden string,
  no `has_more` / `next_offset_id` / `truncated` keys.
- `min_id`, `offset_id`, `since_date` each alone; all three combined; each with
  `topic_id` (assert `reply_to` forwarded); `limit` interplay.
- Paging walk: 25 messages, `limit=10`, `min_id=3`: three calls chained through
  `next_offset_id`, union equals ids 4..25 exactly once, last page
  `has_more: false`, `next_offset_id: null`. Exact-multiple case (`limit` equals
  remaining) gives `has_more: false`. Empty page.
- `since_date` boundary: message at 23:59:59 UTC the day before excluded, at
  00:00:00 included (aware UTC datetimes), stop happens early (fake records how
  many messages it yielded).
- Validation: negative `min_id` / `offset_id`, `since_date` in `2025-1-1`,
  `2025-01-01T00:00:00`, empty-ish values, `limit=0` on new path -> error string,
  `record_tool_error("invalid_argument")` recorded (assert via the recorder or via
  the real call path with `TELEGRAM_STRUCTURED_ERRORS=1` like the 0007 tests),
  and zero client calls. Bad `topic_id` still gives the old text.
- Truncation: 5000-char text -> `truncated: true` and text ending with
  `... [truncated]`; exactly 4096 chars -> no key; user text ending with
  `... [truncated]` but short -> no key; rich-message clip -> flag with
  `rich: true`.
- `sanitize_user_content` equivalence: parametrised edge inputs (None, empty,
  control chars, ZWJ emoji, exactly max_length, max_length+1) compared with the
  previous behaviour (golden values), and `max_length` variants.
- Annotations: `readOnlyHint` still true for `get_history`; fan-out with two
  stub accounts returns two blocks each with its own `has_more`.
- Existing tests unchanged and passing.

## Gates

Per the Pipeline contract:

- `uv run flake8 --select=E9,F63,F7,F82 <touched files>` (blocking) and
  `uv run flake8 <touched files>` with no new warnings versus base (watch C901 on
  `get_history`; keep validation in a separate helper).
- `uv run black --check <touched files>`
- `uv run pytest --cov --cov-report=term-missing` (80% gate; `sanitize` is in the
  measured set).
- Security review: `sanitize.py` is a security-shaped path; reviewer should
  confirm `sanitize_user_content` output is unchanged and the new fields carry
  no user-controlled strings.
