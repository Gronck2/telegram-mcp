# Plan 0007 Structured tool errors

Task: `docs/tasks/0007-structured-errors.md`.

Base: `ours` at 0316bc6 (includes task 0006, PR #7, which edits `validate_id`;
this plan touches it too).

## Approach

Record, do not parse. Errors are classified where they are created, then
applied to the wire result in one place.

1. A per-call recorder: a `ContextVar` holding a mutable list, created in
   `annotated_handler` (`telegram_mcp/runtime.py:221`) before the handler is
   awaited. A mutable list is required because `asyncio.wait_for` and the
   fan-out `asyncio.gather` run children in copied contexts; they share the
   list object, so appends are visible to the hook.
2. `log_and_format_error` (`runtime.py:1792`) appends `(kind, retry_after,
   account)` to the recorder and still returns the same text. Classification
   is a pure helper `classify_error(error, prefix) -> (kind, retry_after)`:
   `AliasNeedsUser`/`ValidationError`/prefix `VALIDATION-001` ->
   `invalid_argument`; `ChatAccessDeniedError`/prefix PRIVACY ->
   `forbidden_allowlist`; `FloodWaitError` (+ `SlowModeWaitError`) -> `flood`
   with `seconds`; `RPCError` mapped by `_telegram_error_name` through a small
   table to `not_found`/`private`/`invalid_argument`, others `internal`;
   resolver `ValueError`s at not-found sites -> `not_found` (see step 3);
   else `internal`.
3. Resolver not-found: the `ValueError`s at `runtime.py:2751`, `2884` and
   `2904` are matched by a dedicated exception subclass (e.g.
   `EntityNotFoundError(ValueError)`) so the classifier tests `isinstance`,
   never message text. Existing messages stay unchanged.
4. Paths that bypass the formatter record explicitly: the `with_account`
   "account is required" return (`runtime.py:927`), and `ToolError`s raised in
   `with_account` (`943`) and `validate_id` (`1900`; already preceded by a
   formatter call, so the entry exists). The `groups.py:1657` refusal gets a
   recorded `forbidden_allowlist` at its call site or via `ChatAccessDeniedError`.
5. Hook: after the original handler returns (and in the timeout branch with
   kind `timeout`), when `TELEGRAM_STRUCTURED_ERRORS` is enabled: if the
   recorder has entries, set `meta = {"telegram-mcp/error": {...}}` and, per
   the fan-out rule, `isError = True` only if the call failed overall.
   "Failed overall" = single-account call with an entry, or a fan-out in which
   every account has an entry (with_account records the expected account
   count, or marks each failure with its label and the fan-out wrapper records
   the label list). An exception already surfaced by FastMCP as `isError=True`
   (wrapped `ToolError`) just gains `_meta`, using the recorded entry or
   `internal` when nothing was recorded.
   Disabled: recorder is still cheap and the result is untouched.
6. Never classify by text. A success result whose body contains "An error
   occurred" has no recorder entry and stays a success.
7. Config: `TELEGRAM_STRUCTURED_ERRORS` parsed next to
   `_tool_timeout_seconds`; document in CLAUDE.md env table, `README`/`.env.example`
   if it lists env vars, and `docs/arch/overview.md` (error model section).

## Steps

1. Tests first (see strategy), failing.
2. Add `classify_error`, the recorder `ContextVar`, and `EntityNotFoundError`
   in `telegram_mcp/runtime.py` (new section near `log_and_format_error`).
3. Wire recording into `log_and_format_error`, the `with_account` account
   check and fan-out, and the not-found raise sites.
4. Extend `_install_annotation_hook` to set the recorder, apply `isError` /
   `meta`, and add kind `timeout`.
5. Audit `telegram_mcp/tools/*.py` for returned error strings that do not go
   through `log_and_format_error` (grep for `return "` / `return f"` with error
   wording, `check_chat_access` results returned directly such as
   `tools/groups.py:1656-1661`, `messages.py:3199`) and route each through the
   recorder. Keep the text.
6. Docs: env var, arch overview, error table.
7. Run gates; request security review (touches `runtime.py` `validate_id`,
   `with_account`).

## Files likely touched

- `telegram_mcp/runtime.py` (formatter, classifier, recorder, `with_account`,
  `validate_id` error path, call hook, resolver raise sites)
- `telegram_mcp/tools/groups.py` (invite refusal, `raise_errors` tool) and a
  few other tool modules only where an error string bypasses the formatter
- `CLAUDE.md` env table, `docs/arch/overview.md`
- New tests `tests/test_structured_errors.py` (plus small additions to the
  fan-out and validate_id tests if shared fixtures help)

Not touched: `sanitize.py` (no new caller-controlled content), success
formatting, `format_tool_result`.

## Tradeoffs considered

- `_meta` vs text prefix vs `structuredContent`: a text prefix changes the
  text contract and breaks existing parsers; `structuredContent` is bound to
  the generated `outputSchema` (`-> str` wrapper) and extra keys can fail
  client validation; `_meta` is additive and ignored by text-only clients.
  Chosen: `_meta` (flag as ADR candidate).
- Hook plus recorder vs editing 191 call sites to return `CallToolResult` or
  raise: FastMCP converts return values and wraps raised exceptions with an
  "Error executing tool" text prefix (`mcp/server/fastmcp/tools/base.py:116`),
  so raising would change text everywhere. The hook keeps text identical.
- Recording vs classifying returned strings: text sniffing misclassifies
  messages that quote error wording and breaks silently when wording changes.
- Opt-in default: avoids surprising other consumers of the live SSE
  deployment (owner decision 1 in the task).
- ContextVar list vs return-value plumbing: plumbing would change every
  tool's return type; the shared mutable list works across
  `wait_for`/`gather` task boundaries.
- Parallel fan-out: entries carry the account label; ordering is by label so
  output is deterministic.

## Test strategy

All under `tests/`, stub clients, never real Telegram (`conftest.py` dummy
credentials). Drive the real `mcp` call path (`mcp._mcp_server
.request_handlers[CallToolRequest]`) with a `CallToolRequest`, so the hook,
FastMCP wrapping and `isError` are the real ones.

- Per kind: flood (`FloodWaitError(request=None, capture=30)` -> kind `flood`,
  `retry_after` 30, text unchanged), allowlist refusal (enable allowlist ->
  `forbidden_allowlist`), validate_id out-of-range -> `invalid_argument`,
  `account` missing -> `invalid_argument`, resolver miss -> `not_found`,
  `ChannelPrivateError` -> `private`, unknown exception -> `internal`,
  timeout (small `TELEGRAM_TOOL_TIMEOUT_SECONDS`) -> `timeout`.
- `raise_errors=True` tool (`get_participants`) carries kind and `isError`.
- Fan-out: two stub accounts, one raising and one succeeding -> `isError`
  false, `_meta` lists the failing label; both failing -> `isError` true.
- Compatibility: with the env var unset, `isError` stays false and `_meta` is
  absent (if opt-in is chosen); with it set, error text equals the baseline
  string; a golden comparison of success results (text and structuredContent)
  before/after for a handful of tools including a `format_tool_result` JSON
  tool; a tool whose success message contains "An error occurred" is not
  flagged.
- Leak check: `_meta` of an error for a hostile chat name / peer string
  contains only the fixed keys and kind values.
- Existing text-asserting tests must pass unchanged.

## Gates

Per the Pipeline contract:

- `uv run flake8 --select=E9,F63,F7,F82 <touched files>` (blocking) and
  `uv run flake8 <touched files>` with no new warnings versus base (watch
  C901 in `log_and_format_error`; keep the classifier a separate function).
- `uv run black --check <touched files>`
- `uv run pytest --cov --cov-report=term-missing` (80% gate; new code lives in
  `telegram_mcp.runtime`, which is measured).
- Security review: `runtime.py` (`validate_id`, `with_account`) is a
  security-shaped path.
