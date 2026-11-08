# Plan 0001 Fix chat allowlist / HTTP transport security review findings

Task: `docs/tasks/0001-allowlist-security-review-fixes.md`.

## Approach

Six small, independent changes, mostly in `telegram_mcp/runtime.py`. Do them
as separate commits in the order below so each is reviewable alone.

## Steps

1. **get_marked_id / ChatEmpty (item 5).** In `runtime.get_marked_id` (~L108)
   return `-entity.id` for `ChatEmpty` alongside `Chat` / `ChatForbidden`.
   Test: `ChatEmpty(id=5)` gives `-5`; allowlist `-5` matches, `5` does not.
2. **Parser (items 3, 4).** In `_parse_allowed_chat_ids` (~L1182):
   - Wildcard stays a bare-`*` token check only. After `lstrip("@")`, a handle
     that equals `*` must not become the wildcard: skip wildcard promotion
     (store nothing or a non-matching sentinel; prefer counting it as an
     entry that matches nothing).
   - Fail closed: track whether the raw value was non-blank. If tokens exist
     but `allowed` ends up empty, return an empty `set()` instead of `None`
     (`set()` is "enabled, deny all"). Blank/whitespace-only raw stays `None`.
     Check the iterable branch the same way (non-empty iterable, no valid
     entries gives `set()`; empty iterable stays `None`).
   - Verify callers distinguish `None` from `set()` with `is None`, never
     truthiness: `get_effective_allowed_chat_ids`, `get_chat_allowlist`
     (`set(shared or ())` is fine), `is_chat_allowlist_enabled`,
     `_matches_chat_allowlist` (empty set matches nothing, good).
   - Keep `ALLOWED_CHAT_IDS` module attribute and its env-over-attribute
     precedence in `get_effective_allowed_chat_ids`; `tests/main.py`
     monkeypatches it, so test with a patched attribute set to `set()` and to
     a populated set.
   - README: `*` means every chat in all three variables, including
     `TELEGRAM_ALLOWED_CHAT_IDS`; unparseable non-empty values now deny all.
3. **press_inline_button (item 1).** Add `BOTH_ACCESS_TOOLS: frozenset[str] =
   frozenset({"press_inline_button", "vote_poll"})` next to
   `READ_ACCESS_TOOLS`. In `chat_access_for_tool`, after the
   `READ_CHAT_PARAM_NAMES` / `READ_ACCESS_TOOLS` early return, return
   `CHAT_ACCESS_BOTH` if `tool_name in BOTH_ACCESS_TOOLS` (check how vote_poll
   is handled today and fold it in only if that does not change its current
   behaviour; otherwise list only `press_inline_button`). Confirm
   `validate_id` passes the mode through `check_chat_access` before the tool
   body runs and that the error names the denied mode. Test via
   `chat_access_for_tool` and an end-to-end `validate_id` call with write-only
   and read+write env, asserting no client call.
4. **Invite joins (item 2).** In `tools/groups.py` add a helper, e.g.
   `_check_invite_allowed(client, hash)`, used by `join_chat_by_link` (~L1643)
   and `import_chat_invite` (~L1731) after the invite hash is extracted and
   before `ImportChatInviteRequest`:
   - `if not is_chat_allowlist_enabled(): return None`.
   - `res = await client(functions.messages.CheckChatInviteRequest(hash))`.
   - `ChatInviteAlready` / `ChatInvitePeek`: take `res.chat`, call
     `check_chat_access(res.chat.id marked, res.chat, CHAT_ACCESS_WRITE)`
     (marked id via `get_marked_id`).
   - Plain `ChatInvite` or anything else: refuse with the standard
     privacy-policy style error (no chat id to check).
   - Return the error string through the existing `log_and_format_error` /
     return convention of those tools. Keep the tools' existing error paths.
   Test with fake clients returning each response type; assert
   `ImportChatInviteRequest` call count is 0 on refusal and 1 on allow;
   no-allowlist case makes no `CheckChatInviteRequest`.
5. **SSE Origin check (item 6).** Read `telegram_mcp/transport/http.py` and
   `runner.py` (how the streamable app is wrapped, how SSE is served). Wrap the
   SSE app (`mcp.sse_app(...)`) in the same Origin-checking middleware, taking
   care that:
   - requests with no Origin header pass (non-browser clients);
   - same-host Origin and the sibling-container Host (`telegram-mcp:8765`,
     per CLAUDE.md Extra rules) pass; the allowed host/origin set comes from the
     same config the streamable path uses;
   - the long-lived `/sse` stream is not buffered (pure ASGI middleware, not
     `BaseHTTPMiddleware`, if the existing one is ASGI; reuse that class);
   - protocol-negotiation behaviour that only makes sense for streamable HTTP
     (e.g. `/mcp` header rewriting) is not applied to SSE paths, or is a no-op
     there. If the existing class bundles both, split the Origin check into a
     reusable piece.
   Tests (`test_protocol_negotiation.py` / `test_runner.py`): ASGI-level calls
   against the wrapped SSE app for `/media/x` and `/upload/x` with bad Origin
   (403), no Origin, and good Origin; `/messages/` POST and `/sse` GET with
   no Origin pass through. If this cannot be shown to work, take the fallback:
   fix the docstring/README claim only, and say so in the PR.
6. **Docs.** README (wildcard, fail-closed parse, press_inline_button needs
   both modes, invite joins under allowlist, SSE Origin coverage), and
   `docs/arch/overview.md` "Access control layers" section.

## Files likely touched

- `telegram_mcp/runtime.py`
- `telegram_mcp/tools/groups.py`
- `telegram_mcp/tools/messages.py` (only if annotations/docstring need changes)
- `telegram_mcp/transport/http.py`, `telegram_mcp/runner.py`
- `README.md`, `docs/arch/overview.md`
- `tests/test_chat_allowlist.py`, `tests/test_chat_access_modes.py`,
  `tests/test_protocol_negotiation.py`, `tests/test_runner.py`, possibly a new
  `tests/test_invite_allowlist.py`

## Tradeoffs considered

- Item 1: a "both" set in `chat_access_for_tool` vs. marking the tool
  `readOnlyHint=True`. The hint would also expose it under
  `TELEGRAM_EXPOSED_TOOLS=read-only`, which is wrong because it presses
  buttons (a write). The set keeps both semantics correct.
- Item 2: refusing a plain `ChatInvite` is strict (the agent cannot join new
  chats by invite while an allowlist is on) but the chat id is unknowable
  before joining, so any alternative would be join-then-leave, which is a side
  effect. Accepted by the owner.
- Item 4: fail closed can lock out a user who typos the variable, but the
  failure is visible (everything denied) instead of silent exposure.
- Item 6: wrapping the SSE app risks breaking the streaming response or the
  sibling-container Host setup; hence the documented fallback.
- ADR: none needed; behaviour tightening within the existing allowlist design.

## Gates

`uv run flake8 --select=E9,F63,F7,F82 <touched>`, `uv run black --check
<touched>`, `uv run pytest --cov --cov-report=term-missing` (fail_under 80).
