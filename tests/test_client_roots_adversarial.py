"""Task 0011 (tester): adversarial roots/list payloads through the real resolvers and tools.

Invariant under test: with non-empty server roots, no payload a client can send
makes any file tool read or write outside the server roots; a refused call leaves
the target file unchanged and never reaches the Telegram client.
"""

import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult

from telegram_mcp import runtime
from telegram_mcp.tools import media, messages, profile

ENV_COMBOS = [
    pytest.param({}, id="default"),
    pytest.param({"TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK": "1"}, id="fallback"),
    pytest.param({"TELEGRAM_SERVER_ROOTS_ONLY": "1"}, id="roots_only"),
    pytest.param(
        {"TELEGRAM_SERVER_ROOTS_ONLY": "1", "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK": "1"},
        id="roots_only+fallback",
    ),
]


class _Session:
    def __init__(self, uris):
        self._uris = uris

    async def list_roots(self):
        return SimpleNamespace(roots=[SimpleNamespace(uri=u) for u in self._uris])


def _ctx(*uris):
    return SimpleNamespace(session=_Session(list(uris)))


class _Client:
    """Any Telegram call is a test failure."""

    def __init__(self):
        self.calls = []

    def _hit(self, name):
        self.calls.append(name)
        raise AssertionError(f"{name} must not be reached on refusal")

    async def get_messages(self, entity, ids=None, **k):
        # download_media reads the message before it resolves the target path
        return SimpleNamespace(id=ids, media=object(), file=SimpleNamespace(size=3))

    async def download_media(self, *a, **k):
        self._hit("download_media")

    async def send_file(self, *a, **k):
        self._hit("send_file")

    async def upload_file(self, *a, **k):
        self._hit("upload_file")

    async def iter_messages(self, *a, **k):
        self._hit("iter_messages")

    async def __call__(self, *a, **k):
        self._hit("rpc")


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in (
        "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK",
        "TELEGRAM_SERVER_ROOTS_ONLY",
        "TELEGRAM_ALLOWED_ROOTS",
        "TELEGRAM_STRUCTURED_ERRORS",
    ):
        monkeypatch.delenv(name, raising=False)
    base = tmp_path.resolve()
    srv = base / "srv"
    (srv / "sub").mkdir(parents=True)
    srv2 = base / "srv2"
    srv2.mkdir()
    outside = base / "outside"
    outside.mkdir()
    code = base / "app" / "telegram_mcp"
    code.mkdir(parents=True)
    victim = code / "x.py"
    victim.write_text("ORIGINAL")
    secret = outside / "secret.txt"
    secret.write_text("secret")
    sibling_secret = srv2 / "secret.txt"
    sibling_secret.write_text("secret2")
    (srv / "link_out").symlink_to(outside)
    (srv / "link_in").symlink_to(srv / "sub")
    (srv / "file_link_out.txt").symlink_to(secret)
    (srv / "plain.txt").write_text("fine")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [srv])
    client = _Client()
    for mod in (media, messages, profile):
        monkeypatch.setattr(mod, "get_client", lambda account=None, c=client: c)

    async def _entity(chat_id, cl):
        return "entity"

    monkeypatch.setattr(media, "resolve_entity", _entity)
    monkeypatch.setattr(messages, "resolve_entity", _entity)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(profile, "ensure_connected", _noop)
    monkeypatch.setattr(messages, "ensure_connected", _noop)
    return SimpleNamespace(
        base=base,
        srv=srv,
        srv2=srv2,
        outside=outside,
        victim=victim,
        secret=secret,
        sibling_secret=sibling_secret,
        client=client,
    )


def _payloads(w):
    """Hostile roots/list answers, each a list of URI strings."""
    b, s = str(w.base), str(w.srv)
    return {
        "root": ["file:///"],
        "root_authority_empty": ["file://"],
        "localhost_root": ["file://localhost/"],
        "localhost_outside": [f"file://localhost{w.outside}"],
        "remote_host": ["file://host/share"],
        "remote_host_root": ["file://evil/"],
        "base_parent": [f"file://{b}"],
        "sibling_prefix": [f"file://{w.srv2}"],
        "sibling_prefix_slash": [f"file://{w.srv2}/"],
        "dotdot_in_uri": [f"file://{s}/../outside"],
        "dotdot_to_root": [f"file://{s}/../../../../../.."],
        "pct_dotdot": [f"file://{s}/%2e%2e/outside"],
        "pct_slash_dotdot": [f"file://{s}%2f..%2foutside"],
        "pct_double": [f"file://{s}/%252e%252e/outside"],
        "pct_encoded_root": ["file://%2f"],
        "pct_encoded_outside": [f"file://{str(w.outside).replace('/', '%2F')}"],
        "symlink_escape_root": [f"file://{s}/link_out"],
        "symlink_escape_file": [f"file://{s}/file_link_out.txt"],
        "query_fragment": [f"file://{w.outside}?x=1#frag", f"file://{s}/../outside#a"],
        "null_byte": [f"file://{s}/sub%00/../../outside", "file:///\x00"],
        "other_scheme": ["http:///", "ftp://x/", "data:text/plain,x", "", "file:", "not a uri"],
        "relative_file_uri": ["file:outside", "file:../outside", "file://./outside"],
        "case_variant_outside": [f"file://{str(w.outside).upper()}", f"file://{b.upper()}"],
        "case_variant_server": [f"file://{s.upper()}"],
        "all_invalid_mix": ["file:///", f"file://{w.srv2}", "bogus", f"file://{s}/nonexistent"],
        "huge_all_outside": [f"file://{w.outside}"] * 5000 + ["file:///"] * 5000,
    }


def _assert_inside_server(roots, w):
    for r in roots:
        real = Path(os.path.realpath(r))
        assert real == w.srv or w.srv in real.parents, f"{r} escapes the server root"


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ENV_COMBOS)
async def test_no_payload_yields_roots_outside_server_roots(world, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for name, uris in _payloads(world).items():
        roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(*uris))
        _assert_inside_server(roots, world)
        if not roots:
            assert status != runtime.ROOTS_STATUS_READY, name
        if "TELEGRAM_SERVER_ROOTS_ONLY" in env:
            assert (roots, status) == ([world.srv], runtime.ROOTS_STATUS_SERVER_ONLY), name


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ENV_COMBOS)
async def test_all_outside_payloads_are_deny_all_even_with_fallback(world, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "TELEGRAM_SERVER_ROOTS_ONLY" in env:
        pytest.skip("roots/list is skipped in server-only mode")
    must_deny = [
        "root",
        "localhost_root",
        "localhost_outside",
        "base_parent",
        "sibling_prefix",
        "sibling_prefix_slash",
        "dotdot_in_uri",
        "dotdot_to_root",
        "pct_dotdot",
        "pct_slash_dotdot",
        "symlink_escape_root",
        "symlink_escape_file",
        "all_invalid_mix",
        "huge_all_outside",
    ]
    payloads = _payloads(world)
    for name in must_deny:
        roots, status = await runtime._get_effective_allowed_roots_with_status(
            _ctx(*payloads[name])
        )
        assert (roots, status) == (
            [],
            runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER,
        ), name


@pytest.mark.asyncio
async def test_unusable_payloads_never_fall_back_to_wider_roots(world, monkeypatch):
    """Garbage-only lists are the pre-existing empty-list behaviour (deny-all or opt-in
    server fallback), never anything wider than the server roots."""
    garbage = ["bogus", "http:///", "", "file://host/share", "file://evil/"]
    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(*garbage))
    assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_DENY_ALL)
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(*garbage))
    assert (roots, status) == ([world.srv], runtime.ROOTS_STATUS_SERVER_FALLBACK)


@pytest.mark.asyncio
async def test_percent_encoded_and_trailing_slash_inside_forms_are_accepted(world):
    s = str(world.srv)
    for uri in (
        f"file://{s}/",
        f"file://{s}//sub/",
        f"file://{s}/sub/../sub",
        f"file://localhost{s}/sub",
        f"file://{s}/%73ub",
        f"file://{s}/link_in",
    ):
        roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(uri))
        assert status == runtime.ROOTS_STATUS_READY, uri
        _assert_inside_server(roots, world)
        assert len(roots) == 1


@pytest.mark.asyncio
async def test_duplicates_and_huge_valid_list_are_deduped(world):
    uris = [f"file://{world.srv}/sub"] * 3000 + ["file:///"] * 3000 + [f"file://{world.srv}/sub/"]
    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(*uris))
    assert (roots, status) == ([world.srv / "sub"], runtime.ROOTS_STATUS_READY)


@pytest.mark.asyncio
async def test_symlinked_server_root_is_compared_by_realpath(tmp_path, monkeypatch):
    real = (tmp_path / "real").resolve()
    (real / "in").mkdir(parents=True)
    other = (tmp_path / "other").resolve()
    other.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real)
    # startup resolves server roots (strict); mimic that
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [link.resolve(strict=True)])
    via_link = f"file://{link}/in"
    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(via_link))
    assert (roots, status) == ([real / "in"], runtime.ROOTS_STATUS_READY)
    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(f"file://{other}"))
    assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER)


@pytest.mark.asyncio
async def test_file_as_server_root_with_hostile_clients(tmp_path, monkeypatch):
    allowed = (tmp_path / "only.txt").resolve()
    allowed.write_text("ok")
    neighbour = (tmp_path / "only.txt2").resolve()
    neighbour.write_text("no")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [allowed])
    for uri in (f"file://{tmp_path.resolve()}", "file:///", f"file://{neighbour}"):
        roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx(uri))
        assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER), uri


def _bare_error(*paths):
    class _E(Exception):
        def errors(self):
            return [{"type": "url_parsing", "input": p} for p in paths]

    return _E("validation")


class _RaisingSession:
    def __init__(self, error):
        self._error = error

    async def list_roots(self):
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ENV_COMBOS)
async def test_bare_path_recovery_payloads_are_narrowed(world, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "TELEGRAM_SERVER_ROOTS_ONLY" in env:
        pytest.skip("roots/list is skipped in server-only mode")
    s = str(world.srv)
    hostile = [
        ["/"],
        [str(world.base)],
        [str(world.srv2)],
        [f"{s}/../outside"],
        [f"{s}/link_out"],
        ["/etc", "/proc", "/", str(world.outside)],
        [f"{s}/../../.."],
        ["/" + "../" * 50],
    ]
    for paths in hostile:
        ctx = SimpleNamespace(session=_RaisingSession(_bare_error(*paths)))
        roots, status = await runtime._get_effective_allowed_roots_with_status(ctx)
        assert (roots, status) == (
            [],
            runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER,
        ), paths
    ctx = SimpleNamespace(
        session=_RaisingSession(_bare_error("/", f"{s}/sub", f"{s}/sub/", str(world.srv2)))
    )
    roots, status = await runtime._get_effective_allowed_roots_with_status(ctx)
    assert (roots, status) == ([world.srv / "sub"], runtime.ROOTS_STATUS_READY)


# --------------------------------------------------------------------------
# Real tools: no read or write outside the server roots, no Telegram call.
# --------------------------------------------------------------------------

DENY_PAYLOADS = [
    "root",
    "localhost_root",
    "base_parent",
    "sibling_prefix",
    "dotdot_in_uri",
    "pct_dotdot",
    "symlink_escape_root",
    "symlink_escape_file",
    "all_invalid_mix",
    "huge_all_outside",
]


def _tool_attacks(w):
    return {
        "download_media": lambda ctx: inspect.unwrap(media.download_media)(
            1, 7, file_path=str(w.victim), ctx=ctx
        ),
        "download_media_default_path": lambda ctx: inspect.unwrap(media.download_media)(
            1, 7, ctx=ctx
        ),
        "export_chat_history": lambda ctx: inspect.unwrap(messages.export_chat_history)(
            1, str(w.victim), ctx=ctx
        ),
        "export_unread_messages": lambda ctx: inspect.unwrap(messages.export_unread_messages)(
            [1], str(w.victim), ctx=ctx
        ),
        "send_file": lambda ctx: inspect.unwrap(media.send_file)(1, str(w.secret), ctx=ctx),
        "send_file_sibling": lambda ctx: inspect.unwrap(media.send_file)(
            1, str(w.sibling_secret), ctx=ctx
        ),
        "send_file_symlink": lambda ctx: inspect.unwrap(media.send_file)(
            1, str(w.srv / "file_link_out.txt"), ctx=ctx
        ),
        "send_file_dotdot": lambda ctx: inspect.unwrap(media.send_file)(
            1, f"{w.srv}/../outside/secret.txt", ctx=ctx
        ),
        "send_album": lambda ctx: inspect.unwrap(media.send_album)(
            1, [str(w.secret), str(w.sibling_secret)], ctx=ctx
        ),
        "set_profile_photo": lambda ctx: inspect.unwrap(profile.set_profile_photo)(
            str(w.secret), ctx=ctx
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ENV_COMBOS)
async def test_real_tools_refuse_and_touch_nothing(world, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    payloads = _payloads(world)
    before = sorted(str(p) for p in world.base.rglob("*"))
    for pname in DENY_PAYLOADS:
        ctx = _ctx(*payloads[pname])
        for tname, attack in _tool_attacks(world).items():
            if tname == "download_media_default_path" and "TELEGRAM_SERVER_ROOTS_ONLY" in env:
                continue  # legitimately lands inside the server root in this mode
            result = await attack(ctx)
            refused = ("outside allowed roots", "disabled", "not found", "traversal")
            assert any(t in result.lower() for t in refused), (pname, tname, result)
            assert "ORIGINAL" == world.victim.read_text(), (pname, tname)
            assert world.secret.read_text() == "secret"
            assert world.client.calls == [], (pname, tname)
    assert sorted(str(p) for p in world.base.rglob("*")) == before


@pytest.mark.asyncio
async def test_real_tools_refuse_with_deny_message_status(world):
    ctx = _ctx("file:///")
    expect = (
        "{} is disabled because none of the client's MCP Roots lies inside the "
        "server's allowed roots (TELEGRAM_ALLOWED_ROOTS). Narrow the client roots to a "
        "server root or add the folder to the server roots."
    )
    for tname, tool in (
        ("download_media", "download_media"),
        ("export_chat_history", "export_chat_history"),
        ("send_file", "send_file"),
        ("set_profile_photo", "set_profile_photo"),
    ):
        got = await _tool_attacks(world)[tname](ctx)
        assert got == expect.format(tool), tname
    assert world.client.calls == []


@pytest.mark.asyncio
async def test_resolvers_with_narrowed_roots_still_refuse_siblings_and_symlinks(world):
    ctx = _ctx(f"file://{world.srv}/sub", "file:///")
    for raw in (
        str(world.srv / "plain.txt"),  # inside server but outside the narrowed client root
        str(world.srv / "file_link_out.txt"),
        str(world.sibling_secret),
        str(world.srv / "sub" / ".." / "plain.txt"),
    ):
        path, err = await runtime._resolve_readable_file_path(
            raw_path=raw, ctx=ctx, tool_name="send_file"
        )
        assert path is None and err, raw
    (world.srv / "sub" / "ok.txt").write_text("1")
    path, err = await runtime._resolve_readable_file_path(
        raw_path="ok.txt", ctx=ctx, tool_name="send_file"
    )
    assert err is None and path == world.srv / "sub" / "ok.txt"
    # writable: symlink dir inside the client root pointing outside
    (world.srv / "sub" / "esc").symlink_to(world.outside)
    path, err = await runtime._resolve_writable_file_path(
        raw_path=str(world.srv / "sub" / "esc" / "new.bin"),
        default_filename="d.bin",
        ctx=ctx,
        tool_name="download_media",
    )
    assert (path, err) == (None, "Path is outside allowed roots.")
    assert not (world.outside / "new.bin").exists()
    # writable: parent directories are not created outside the narrowed root
    path, err = await runtime._resolve_writable_file_path(
        raw_path=str(world.srv / "newdir" / "deep" / "f.bin"),
        default_filename="d.bin",
        ctx=ctx,
        tool_name="download_media",
    )
    assert (path, err) == (None, "Path is outside allowed roots.")
    assert not (world.srv / "newdir").exists()


@pytest.mark.asyncio
async def test_structured_errors_flag_new_refusal_as_invalid_argument(world, monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    recorder = runtime._ErrorRecorder()
    token = runtime._error_recorder.set(recorder)
    try:
        text = await inspect.unwrap(media.send_file)(1, str(world.secret), ctx=_ctx("file:///"))
    finally:
        runtime._error_recorder.reset(token)
    assert "none of the client's MCP Roots lies inside" in text
    result = CallToolResult(content=[])
    runtime._apply_structured_error(result, recorder, False)
    assert result.isError is True
    assert result.meta[runtime.STRUCTURED_ERRORS_META_KEY] == {"kind": "invalid_argument"}


@pytest.mark.asyncio
async def test_prepare_upload_and_signed_links_ignore_client_roots(world, monkeypatch):
    """Not changed by 0011, but must stay confined to the server root whatever roots/list says."""
    monkeypatch.setenv("TELEGRAM_MEDIA_BASE_URL", "https://example.test")
    monkeypatch.setattr(media._runtime, "SERVER_ALLOWED_ROOTS", [world.srv])
    for name in ("../../outside/x.bin", "/etc/passwd", "..\\..\\x", "a/../../b.txt"):
        raw = await inspect.unwrap(media.prepare_upload)(name)
        import json

        slot = json.loads(raw)
        fp = Path(slot["file_path"])
        assert world.srv in fp.parents, (name, fp)
    assert await inspect.unwrap(media.prepare_upload)("..") != ""
