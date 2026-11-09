"""Round 4 of structured tool errors (task 0007): what a recorded refusal must NOT touch.

* a file-path helper refusal is only recorded where the caller returns it as the tool result;
  a call that ends in success never carries it (static scan of every caller + live runs);
* the unrecorded-failure hook is invisible with the switch OFF (golden recorded on the base
  commit, RECORD_STRUCTURED_GOLDEN=1 in a detached worktree of 0316bc6) and adds one entry
  at most with it ON;
* ``RateRefusal`` / ``_AskPayload`` (str subclasses) serialise exactly like plain ``str``;
* ``_PrivacyRefusal`` (now a ValidationError, not a ValueError) still answers with the same text.
"""

import ast
import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon import errors

from sanitize import format_tool_result
from telegram_mcp import runtime
from telegram_mcp.resolve_phone_limit import MIN_INTERVAL_ENV, RateRefusal, ResolvePhoneLimiter
from telegram_mcp.tools import media, profile

from test_structured_errors_returns import (  # noqa: F401  (_env is an autouse fixture)
    META,
    _env,
    _msgs,
    _TMP,
    call,
    fake,
    run,
    text,
)
from test_structured_errors_round3 import _file_msg, _roots

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "telegram_mcp"
HELPERS = {"_ensure_allowed_roots", "_resolve_readable_file_path", "_resolve_writable_file_path"}
GOLDEN = Path(__file__).parent / "data" / "structured_errors_off_golden_unrecorded.json"

# --- (1) every caller of the path helpers returns the refusal at once -------------------


def _calls(tree, names):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in names:
                yield node


def _helper_users():
    users = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        found = list(_calls(tree, HELPERS | {"_path_refusal"}))
        if found:
            users[path.relative_to(ROOT).as_posix()] = (tree, found)
    return users


def test_only_runtime_and_tool_modules_use_the_path_helpers():
    """No signed-URL, transcription, cache or export-writer module probes a path with them."""
    assert set(_helper_users()) == {
        "telegram_mcp/runtime.py",
        "telegram_mcp/tools/profile.py",
        "telegram_mcp/tools/media.py",
        "telegram_mcp/tools/messages.py",
        "telegram_mcp/tools/custom_emoji.py",
        "telegram_mcp/tools/groups.py",
    }
    for module in ("media_links", "audit", "transcribe", "transcription"):
        source = PACKAGE / f"{module}.py"
        if source.exists():
            assert not (HELPERS & {n for n in source.read_text().split() if n in HELPERS})


def _is_return_of_error(stmt, name):
    return (
        isinstance(stmt, ast.If)
        and isinstance(stmt.test, ast.Name)
        and stmt.test.id == name
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Return)
        and isinstance(stmt.body[0].value, ast.Name)
        and stmt.body[0].value.id == name
        and not stmt.orelse
    )


def _tool_callers():
    for rel, (tree, _) in _helper_users().items():
        if rel == "telegram_mcp/runtime.py":
            continue
        for block in ast.walk(tree):
            body = getattr(block, "body", None)
            if not isinstance(body, list):
                continue
            for index, stmt in enumerate(body):
                assigned = isinstance(stmt, ast.Assign) and any(
                    True for _ in _calls(stmt.value, HELPERS)
                )
                if assigned:
                    yield rel, stmt.lineno, body[index + 1 : index + 2], stmt


def test_every_tool_caller_returns_a_path_refusal_straight_away():
    seen = 0
    for rel, line, following, stmt in _tool_callers():
        seen += 1
        target = stmt.targets[0]
        assert isinstance(target, ast.Tuple) and len(target.elts) == 2, (rel, line)
        error_name = target.elts[1].id
        assert following and _is_return_of_error(following[0], error_name), (rel, line)
    assert seen == 13  # profile 1, media 8, messages 2, custom_emoji 1, groups 1


def test_path_refusal_is_only_ever_returned_by_the_helpers():
    """_path_refusal(...) appears only as a return value inside the three helpers."""
    tree, calls = _helper_users()["telegram_mcp/runtime.py"]
    wanted = {
        "_ensure_allowed_roots",
        "_resolve_readable_file_path",
        "_resolve_writable_file_path",
    }
    inside = set()
    for func in ast.walk(tree):
        if isinstance(func, ast.AsyncFunctionDef) and func.name in wanted:
            for node in ast.walk(func):
                if isinstance(node, ast.Return):
                    inside.update(id(c) for c in _calls(node, {"_path_refusal"}))
    refusals = [c for c in calls if getattr(c.func, "id", None) == "_path_refusal"]
    assert refusals and all(id(c) in inside for c in refusals)
    for rel, (other, found) in _helper_users().items():
        if rel != "telegram_mcp/runtime.py":
            assert not [c for c in found if getattr(c.func, "id", None) == "_path_refusal"], rel


@pytest.mark.asyncio
async def test_helpers_record_nothing_when_they_succeed(monkeypatch):
    _roots(monkeypatch)
    root = (_TMP["path"] / "root").resolve()
    recorder = runtime._ErrorRecorder()
    token = runtime._error_recorder.set(recorder)
    try:
        for tool in ("send_file", "set_profile_photo"):
            path, error = await runtime._resolve_readable_file_path(
                raw_path="a.png", ctx=None, tool_name=tool
            )
            assert error is None and path == root / "a.png"
        path, error = await runtime._resolve_writable_file_path(
            raw_path=None, default_filename="x.bin", ctx=None, tool_name="download_media"
        )
        assert error is None and path.parent.is_dir()
        roots, error = await runtime._ensure_allowed_roots(None, "send_file")
        assert error is None and roots
    finally:
        runtime._error_recorder.reset(token)
    assert recorder.entries == []


async def _download_with_suffix(target, file=None, **kw):
    out = Path(str(file) + ".png")
    out.write_bytes(b"data")
    return str(out)


@pytest.mark.asyncio
async def test_download_media_resolving_its_path_twice_is_still_a_clean_success(monkeypatch):
    """The suffix-fixing second _resolve_writable_file_path call must record nothing."""
    results = []
    for switch in (False, True):
        _roots(monkeypatch)
        spec = (
            {"chat_id": 100, "message_id": 1, "file_path": "dl/photo.bin"},
            lambda: fake(get_messages=_msgs(_file_msg()), download_media=_download_with_suffix),
        )
        result = await run(monkeypatch, "download_media", spec, lambda mp: None, switch)
        assert "Media downloaded to" in text(result) and "photo.png" in text(result), text(result)
        assert (result.isError, result.meta) == (False, None)
        results.append(text(result))
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_download_media_default_dir_creation_then_success(monkeypatch):
    """Default (no file_path) probe of the download subdir succeeds with no refusal."""
    _roots(monkeypatch)
    spec = (
        {"chat_id": 100, "message_id": 1},
        lambda: fake(get_messages=_msgs(_file_msg()), download_media=_download_with_suffix),
    )
    result = await run(monkeypatch, "download_media", spec, lambda mp: None, True)
    assert "Media downloaded to" in text(result)
    assert (result.isError, result.meta) == (False, None)


@pytest.mark.asyncio
async def test_a_later_refusal_in_the_second_resolve_is_the_one_recorded(monkeypatch):
    """First resolve ok, suffix re-resolve refused (extension): exactly one entry, error ON."""
    _roots(monkeypatch)
    real = runtime._resolve_writable_file_path
    calls = {"n": 0}

    async def second_refuses(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return None, runtime._path_refusal("Extension not allowed.")
        return await real(**kwargs)

    monkeypatch.setattr(media, "_resolve_writable_file_path", second_refuses)
    spec = (
        {"chat_id": 100, "message_id": 1},
        lambda: fake(get_messages=_msgs(_file_msg()), download_media=_download_with_suffix),
    )
    on = await run(monkeypatch, "download_media", spec, lambda mp: None, True)
    assert text(on) == "Extension not allowed."
    assert on.isError is True and on.meta == {META: {"kind": "invalid_argument"}}
    calls["n"] = 0
    off = await run(monkeypatch, "download_media", spec, lambda mp: None, False)
    assert text(off) == "Extension not allowed." and (off.isError, off.meta) == (False, None)


# --- (2) the unrecorded-failure hook ----------------------------------------------------


@pytest.fixture
def probe():
    @runtime.mcp.tool(name="zz_round4_probe")
    @runtime.with_account(readonly=True)
    async def tool(mode: str, account: str = None) -> str:
        if mode == "escaped":
            raise RuntimeError("backend exploded")
        if mode == "toolerror":
            raise ToolError("backend exploded")
        if mode == "rpc":
            raise errors.ChannelPrivateError(request=None)
        if mode == "recorded_then_toolerror":
            runtime.record_tool_error("not_found")
            raise ToolError("after a recorded failure")
        if mode == "sleep":
            await asyncio.sleep(5)
        return "fine"

    yield
    runtime.mcp._tool_manager._tools.pop("zz_round4_probe", None)


def _dump(result):
    return json.loads(result.model_dump_json(by_alias=True))


SCENARIOS = {
    "escaped": ("zz_round4_probe", {"mode": "escaped"}),
    "toolerror": ("zz_round4_probe", {"mode": "toolerror"}),
    "rpc": ("zz_round4_probe", {"mode": "rpc"}),
    "pydantic": ("zz_round4_probe", {"mode": ["not", "a", "string"]}),
    "missing_argument": ("zz_round4_probe", {}),
    "unknown_tool": ("zz_round4_no_such_tool", {}),
    "timeout": ("zz_round4_probe", {"mode": "sleep"}),
    "fine": ("zz_round4_probe", {"mode": "fine"}),
}
LABELS = {"single": ["one"], "fan-out": ["a", "b"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("labels", LABELS, ids=list(LABELS))
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_switch_off_unrecorded_failures_match_the_base_commit(
    monkeypatch, probe, scenario, labels
):
    """OFF results equal what 0316bc6 produced (no hook there, no meta anywhere)."""
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(runtime, "clients", {label: fake() for label in LABELS[labels]})
    name, args = SCENARIOS[scenario]
    result = _dump(await call(name, **args))
    key = f"{labels}/{scenario}"
    if os.environ.get("RECORD_STRUCTURED_GOLDEN"):
        recorded = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        recorded[key] = result
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(recorded, indent=1, sort_keys=True, ensure_ascii=False))
        return
    assert result == json.loads(GOLDEN.read_text())[key]
    assert "_meta" not in result or result["_meta"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("labels", LABELS, ids=list(LABELS))
@pytest.mark.parametrize(
    "scenario, kind",
    [
        ("escaped", "internal"),
        ("toolerror", "internal"),
        ("pydantic", "invalid_argument"),
        ("missing_argument", "invalid_argument"),
        ("unknown_tool", "invalid_argument"),
        ("timeout", "timeout"),
        ("recorded_then_toolerror", "not_found"),
    ],
)
async def test_switch_on_records_exactly_one_entry(monkeypatch, probe, scenario, kind, labels):
    entries = []
    original = runtime._ErrorRecorder

    class Spy(original):
        def __init__(self):
            super().__init__()
            self.entries = _Spy(entries)

    monkeypatch.setattr(runtime, "_ErrorRecorder", Spy)
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(runtime, "clients", {label: fake() for label in LABELS[labels]})
    name, args = SCENARIOS.get(scenario, ("zz_round4_probe", {"mode": scenario}))
    result = await call(name, **args)
    assert result.isError is True
    assert result.meta[META]["kind"] == kind
    per_call = {}
    for entry in entries:
        per_call.setdefault(entry.get("account"), []).append(entry["kind"])
    if scenario in ("pydantic", "missing_argument", "unknown_tool", "timeout"):
        # never reaches the tool body: at most one entry for the whole call
        assert sum(len(v) for v in per_call.values()) <= 1, per_call
    else:
        assert all(len(v) == 1 for v in per_call.values()), per_call


class _Spy(list):
    """A list that also appends into a shared sink."""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def append(self, item):
        super().append(item)
        self.sink.append(item)


@pytest.mark.asyncio
async def test_hook_is_installed_exactly_once():
    manager = runtime.mcp._tool_manager
    wrapper = manager.call_tool
    assert wrapper.__qualname__.endswith("call_tool") and wrapper.__closure__
    inner = [c.cell_contents for c in wrapper.__closure__ if callable(c.cell_contents)]
    assert all(getattr(f, "__name__", "") != "call_tool" or f is not wrapper for f in inner)
    assert not any(
        getattr(f, "__qualname__", "").startswith("_install_unrecorded_failure_hook")
        for f in inner
    )


# --- (3) str subclasses serialise like plain str -----------------------------------------


def test_rate_refusal_serialises_like_a_plain_string(monkeypatch):
    monkeypatch.setenv(MIN_INTERVAL_ENV, "30")
    limiter = ResolvePhoneLimiter(clock=lambda: 7.0)
    assert limiter.reserve() is None
    refusal = limiter.reserve()
    assert type(refusal) is RateRefusal and refusal.retry_after == 30
    plain = str(refusal)
    assert type(plain) is str and refusal == plain and hash(refusal) == hash(plain)
    assert json.dumps(refusal) == json.dumps(plain)
    assert json.dumps({"k": [refusal]}, sort_keys=True) == json.dumps({"k": [plain]})
    assert format_tool_result([{"error": refusal}], {"note": refusal}) == format_tool_result(
        [{"error": plain}], {"note": plain}
    )


def test_ask_payload_serialises_like_a_plain_string():
    ask = runtime._AskPayload('Ask the user: "which Ann?" {"a": 1}\n')
    plain = str(ask)
    assert type(ask) is not str and ask == plain
    assert json.dumps(ask) == json.dumps(plain)
    assert format_tool_result([{"x": ask}]) == format_tool_result([{"x": plain}])
    assert json.loads(format_tool_result([{"x": ask}]))["results"][0]["x"] == plain


@pytest.mark.asyncio
async def test_rate_refusal_as_a_tool_result_is_the_same_wire_text(monkeypatch):
    """The CallToolResult for a limiter refusal is byte-identical to a plain-str return."""
    monkeypatch.setenv(MIN_INTERVAL_ENV, "30")
    limiter = ResolvePhoneLimiter(clock=lambda: 7.0)
    assert limiter.reserve() is None
    assert isinstance(limiter.reserve(), RateRefusal)
    from telegram_mcp.tools import contacts

    monkeypatch.setattr(contacts, "_resolve_phone_limiter", limiter)
    monkeypatch.setattr(runtime, "clients", {"one": fake()})
    off = await call("resolve_phone", phone="+44 20 7946 0958", account="one")
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    on = await call("resolve_phone", phone="+44 20 7946 0958", account="one")
    assert text(off) == text(on) and "Retry in" in text(off)
    assert _dump(off)["content"][0]["text"] == text(on)
    assert on.meta[META]["kind"] == "flood" and on.meta[META]["retry_after"] >= 1


# --- (4) _PrivacyRefusal ------------------------------------------------------------------


def test_privacy_refusal_is_no_longer_caught_by_except_value_error():
    assert issubclass(profile._PrivacyRefusal, runtime.ValidationError)
    assert not issubclass(profile._PrivacyRefusal, ValueError)
    source = (PACKAGE / "tools" / "profile.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            names = {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
            assert "ValueError" not in names, node.lineno


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [False, True])
async def test_privacy_refusals_keep_their_text_through_the_real_handler(monkeypatch, switch):
    from telethon import types

    from test_privacy_settings import install

    if switch:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    else:
        monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    monkeypatch.setattr(runtime, "clients", {"one": fake()})
    args = {"key": "phone", "disallow_users": ["@ivan_k"], "account": "one"}

    install(monkeypatch, [types.PrivacyValueAllowUsers(users=[999])])
    refused = await call("set_privacy_settings", **args)
    assert text(refused) == (
        "Error: Privacy rule names a user Telegram did not return. "
        "Privacy settings were not changed."
    )
    assert refused.isError is switch
    assert refused.meta == ({META: {"kind": "invalid_argument"}} if switch else None)

    install(monkeypatch, [types.PrivacyValueDisallowAll()])
    done = await call("set_privacy_settings", **args)
    assert text(done) == "Privacy settings for phone updated successfully."
    assert (done.isError, done.meta) == (False, None)
