# 0007 Structured tool errors (isError + machine-readable kind)

## Goal

Requested by the owner's jeeves agent system (an SSE consumer of this server).
Today most tool failures reach the client as a normal successful result whose
text happens to say "An error occurred ...". The client must parse prose to
tell success from failure. Wanted:

- A failed tool call is marked `isError: true` in the MCP `CallToolResult`.
- Each error carries a machine-readable `kind` (and `retry_after` seconds for
  flood waits) that a client can read without parsing text.
- Success results stay byte-for-byte identical (existing consumers parse them).

## Findings

All line numbers are on `ours` at 0316bc6 (task 0006 merged).

How errors are produced today:

- `log_and_format_error` (`telegram_mcp/runtime.py:1792`) is the central
  formatter and always returns a `str`. 191 call sites across
  `telegram_mcp/tools/*.py` and runtime. Branches: `AliasNeedsUser` -> returns
  the ask-the-user payload verbatim (1817); `FloodWaitError` -> "Rate limit
  exceeded (FloodWait) ... waiting N seconds" (1842-1851, `seconds` read from
  `error.seconds`); caller-supplied `user_message` (1858); TypeNotFoundError ->
  "MTProto schema mismatch" (1866); any `RPCError` -> "An error occurred (code:
  X): Telegram returned NAME (Class)." (1876-1881); else "An error occurred
  (code: X)." (1883). The message never includes `str(error)`, request details
  or user content beyond the constant Telegram error name and class name.
- Privacy refusals: `check_chat_access` (1490) builds "<Read|Write> access to
  chat '<identifier>' is restricted by privacy policy (...)"; `deny_peer_param`
  (1676-1705) and `refuse_if_read_restricted` (1583-1597) pass it through
  `log_and_format_error(..., ChatAccessDeniedError(err), prefix=PRIVACY,
  user_message=err)`. Other sites do the same
  (`tools/contacts.py:464-469`, `events.py:524,613`, `chats.py:837,1086`,
  `messages.py:600,978,2500,3124`; `messages.py:3199` raises
  `ChatAccessDeniedError` directly). One path returns a bare string
  (`tools/groups.py:1657-1661`, invite target not identifiable) and is later
  logged by its caller, so the structured kind must be derived from the
  exception/prefix at the formatter, not from the text.
- `validate_id` (`runtime.py:1886-2020`): errors are built through
  `log_and_format_error(..., ValidationError(msg), prefix="VALIDATION-001",
  user_message=msg)` (1977-1999) and returned via `error_result`, which
  returns the string, or raises `ToolError` when `raise_errors=True`
  (1898-1901). Only one tool uses `raise_errors=True`
  (`tools/groups.py:299`, `get_participants`). The ask-the-user alias payload
  (`alias_ask_payload`, 1964) also flows through here.
- `with_account` (`runtime.py:900-957`): non-readonly without `account` returns
  the plain string "Error: 'account' is required. Available accounts: ..."
  (927) with no formatter involved. Read-only fan-out gathers per-account
  results; a `ToolError` from one account is turned into text (936-938) and
  only if every account raised does it re-raise one combined `ToolError`
  (942-945). A returned-string error from one account is indistinguishable from
  a success string and is merged as `[label]\n<text>` (946-947). Mixed results
  therefore have no error signal at all today.
- Not-found has no dedicated path: peer resolution failures are `ValueError`
  (`runtime.py:2751-2760`, `2884`, `2904`) or Telethon `RPCError`s such as
  `PEER_ID_INVALID` / `USERNAME_NOT_OCCUPIED` and land in the generic branches
  above (the kind is currently only visible as "Telegram returned NAME").
- Timeout: the call hook already returns a hand-built `CallToolResult` with
  `isError=True` and code `GEN-TIMEOUT` (`runtime.py:229-245`).

How FastMCP (installed `mcp` in `.venv`, `mcp/server/fastmcp/`) maps results:

- A tool that returns a value gives `CallToolResult(isError=False, content=...,
  structuredContent=...)` (`mcp/server/lowlevel/server.py:578-583`). A returned
  error string is therefore a success on the wire; with a `-> str` annotation
  `structuredContent` is the generated wrapper object validated against the
  tool's `outputSchema`, so extra keys must not be added there.
- A tool that raises: `Tool.run` wraps it as `ToolError("Error executing tool
  <name>: <msg>")` (`mcp/server/fastmcp/tools/base.py:116-117`) and the
  low-level handler turns any exception into `isError=True` with that text
  (`server.py:589-590`). So `raise_errors` / `ToolError` already marks errors
  but changes the text (prefix added).
- `CallToolResult` has a `_meta` field (`meta`). Returning a `CallToolResult`
  from the tool function is not usable (FastMCP converts the return value).
- The server already wraps the low-level `CallToolRequest` handler in one place,
  `_install_annotation_hook` (`runtime.py:216-263`), which post-processes every
  `CallToolResult` (adds the `audience=["user"]` annotation and builds the
  timeout error). That is the single point where `isError` and `_meta` can be
  set for all tools without editing 191 call sites.

Constraints from the codebase:

- `sanitize.py` (`sanitize_user_content`, `sanitize_name`, `format_tool_result`,
  `sanitize.py:179`) covers success payloads. The kind values and
  `retry_after` are server-defined constants/ints, so they add no
  caller-controlled content. Error text must stay as it is now (it already
  omits `str(error)`); do not add the peer identifier or Telegram message text
  to the structured part.
- Many existing tests assert on error text (e.g. `tests/test_error_logging.py`,
  `tests/test_resolver_always_tools_and_id_range.py`); keeping text unchanged
  keeps them valid.

## Proposed error model

Text stays exactly as today. On failure the `CallToolResult` additionally has
`isError: true` and `_meta` containing a namespaced object, for example
`{"telegram-mcp/error": {"kind": "flood", "retry_after": 30}}`. A client that
reads only `content[].text` sees no difference except the standard `isError`
flag. `structuredContent` is left alone (it is bound to the output schema).
`retry_after` is present only for `flood` (integer seconds, 0 or absent when
Telegram did not say).

Kinds (final set proposed):

| kind | meaning | derived from |
|---|---|---|
| `invalid_argument` | bad parameter, validate_id range/type error, missing `account`, unresolvable reference with ask-the-user payload (see decision 3) | `ValidationError`, `with_account` account check, `AliasNeedsUser`, Telegram `*_INVALID` for params |
| `not_found` | peer/message/entity cannot be resolved | resolver `ValueError`s at the not-found sites, RPC names `PEER_ID_INVALID`, `USERNAME_NOT_OCCUPIED`, `USERNAME_INVALID`, `CHAT_ID_INVALID`, `MSG_ID_INVALID`, `MESSAGE_ID_INVALID`, `INVITE_HASH_*` |
| `private` | exists but this account has no access | `CHANNEL_PRIVATE`, `CHAT_FORBIDDEN`, `USER_NOT_PARTICIPANT`, `CHANNEL_BANNED`, `USER_BANNED_IN_CHANNEL`, `CHAT_WRITE_FORBIDDEN`, `CHAT_ADMIN_REQUIRED`, `USER_PRIVACY_RESTRICTED` and the matching Telethon classes |
| `forbidden_allowlist` | refused by this server's chat allowlist / privacy policy | `ChatAccessDeniedError` (and the groups.py:1657 refusal) |
| `flood` | Telegram rate limit, `retry_after` set | `FloodWaitError` (also `SlowModeWaitError`, `PEER_FLOOD` as flood without `retry_after`) |
| `timeout` | tool exceeded `TELEGRAM_TOOL_TIMEOUT_SECONDS`, completion unknown | existing hook branch |
| `internal` | everything else (schema drift, other RPC errors, unexpected exceptions) | fallback |

Multi-account fan-out (read-only tool, `account` omitted):

- `isError` is true only when every account failed (same rule as the existing
  all-`ToolError` branch, 942). If at least one account succeeded the result is
  a normal result (data is usable), so `isError` is false.
- In both cases `_meta` lists the per-account failures:
  `{"telegram-mcp/errors": [{"account": "<label>", "kind": ..., "retry_after": ...}]}`.
  The top-level single-error object is used when exactly one call produced the
  error; when all accounts failed the top-level `kind` is the common kind if
  they agree, else `internal`, with the list as detail.
- The merged text format (`[label]\n<text>`) is unchanged.

## Acceptance

- A tool call that today returns an error string from `log_and_format_error`,
  `validate_id`, or `with_account` ("account is required") now returns
  `isError: true`, with the same text, and `_meta["telegram-mcp/error"]["kind"]`
  set per the table; flood adds integer `retry_after`.
- Raised `ToolError` paths (`raise_errors=True`, fan-out all-failed) carry the
  same `_meta` kind.
- Success results (text and `structuredContent`) are byte-for-byte identical
  to before for all tools; the only difference allowed is none. A regression
  test compares a representative set of success outputs.
- Fan-out: one account errors, one succeeds -> `isError` false, per-account
  detail in `_meta`; all fail -> `isError` true.
- JSON success payloads (`format_tool_result`) are never marked as errors; a
  string is classified as an error only because the server recorded it as one,
  never by sniffing text (a message containing "An error occurred" is not an
  error).
- Timeout result carries kind `timeout`.
- `_meta` contains only the kind, `retry_after` and account labels; no peer
  identifiers, Telegram text or caller-controlled strings.
- Tests under `tests/`, no real Telegram. Gates per CLAUDE.md pass. Touches
  security-shaped paths (`runtime.py`: `validate_id`, `with_account`), so the
  review gate applies.
- `docs/arch/overview.md` gets a short section on the error model.

## Owner decisions

1. Always on or opt-in. Genuine trade-off: setting `isError: true` changes
   behaviour for existing clients (some LLM clients treat `isError` results as
   failures and may retry, truncate or hide content; a client that never
   looked at the flag is unaffected). Options: (a) always on, (b) opt-in via
   `TELEGRAM_STRUCTURED_ERRORS=1`, default off. Recommendation: (b) opt-in
   first, since the live deployment has other consumers; set it in the SSE
   deployment's env for jeeves, then flip the default in a later task once
   consumers are checked. The `_meta` kind could be always on (additive,
   invisible to text readers) while only `isError` is gated; recommend
   gating both behind the one variable to keep a single switch.
2. Kind spelling. The request uses `forbidden-allowlist` (hyphen) next to
   `not_found` (underscore). Recommendation: keep the requested spelling
   `forbidden-allowlist` exactly since the consumer already codes against it,
   and note it in the docs; the alternative is normalising to
   `forbidden_allowlist` (cleaner, needs jeeves to agree).
3. Ask-the-user alias payload (`AliasNeedsUser`, `alias_ask_payload`). It is
   control flow designed to be shown to the user, not a failure. Recommendation:
   `isError: true`, kind `invalid_argument`, plus `_meta` flag
   `"needs_user": true` so the agent can distinguish "ask the human" from a
   bad parameter; alternative is to leave it as a plain success.
4. Mixed fan-out `isError` (see model). Recommendation: false unless all
   accounts failed, with per-account detail in `_meta`.
5. Whether to also add `permission_denied` for `CHAT_ADMIN_REQUIRED` and
   similar "you lack rights in a chat you can see" errors instead of folding
   them into `private`. Recommendation: fold into `private` (the requester's
   definition is "no access") and revisit if jeeves needs the split.

## ADR flag

The error contract is a public API of this server and the hook-plus-recorder
design is a cross-cutting choice. Flagging for the main thread: an ADR may be
warranted (recording the metadata channel choice `_meta` over text prefix or
structuredContent, and the opt-in policy). Not created here.

## Decisions (coordinator on the owner's behalf, 2026-11-10)

1. Opt-in via `TELEGRAM_STRUCTURED_ERRORS=1`, one switch for both `isError`
   and `_meta`. Enabling it on the live deployment (override + recreate) is
   the owner's call.
2. Kind spelled `forbidden_allowlist` (underscore), consistent with the other
   kinds; jeeves is informed and adapts. This supersedes the hyphenated
   spelling elsewhere in this task and the plan.
3. Ask-the-user alias payload: `isError: true`, kind `invalid_argument`,
   `"needs_user": true` in `_meta`.
4. Mixed fan-out: `isError` false unless every account failed; per-account
   failures in `_meta`; merged text unchanged.
5. `CHAT_ADMIN_REQUIRED` and similar fold into `private`; the original RPC
   error name goes into `_meta` as `rpc_error` so it can be told apart without
   a new kind.
6. ADR: yes, short `docs/adr/0003-...`: `_meta` channel, kind list, opt-in
   policy and how the default is flipped later.
