"""Task 0011: server roots are the ceiling for client MCP Roots."""

import inspect
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from telegram_mcp import runtime
from telegram_mcp.tools import media

OUTSIDE_TEXT = (
    "{tool} is disabled because none of the client's MCP Roots lies inside the "
    "server's allowed roots (TELEGRAM_ALLOWED_ROOTS). Narrow the client roots to a "
    "server root or add the folder to the server roots."
)


class _Session:
    def __init__(self, roots=None, error=None):
        self._roots = roots or []
        self._error = error

    async def list_roots(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(roots=[SimpleNamespace(uri=u) for u in self._roots])


def _ctx(*uris):
    return SimpleNamespace(session=_Session(list(uris)))


def _uri(path):
    return Path(path).as_uri()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK",
        "TELEGRAM_SERVER_ROOTS_ONLY",
        "TELEGRAM_ALLOWED_ROOTS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    server = (tmp_path / "server").resolve()
    (server / "proj").mkdir(parents=True)
    (tmp_path / "sibling").mkdir()
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [server])
    return SimpleNamespace(base=tmp_path.resolve(), server=server, proj=server / "proj")


async def _effective(ctx):
    return await runtime._get_effective_allowed_roots_with_status(ctx)


@pytest.mark.asyncio
async def test_client_root_equal_to_server_root_is_kept(layout):
    roots, status = await _effective(_ctx(_uri(layout.server)))
    assert (roots, status) == ([layout.server], runtime.ROOTS_STATUS_READY)


@pytest.mark.asyncio
async def test_client_root_under_server_root_is_the_only_effective_root(layout):
    roots, status = await _effective(_ctx(_uri(layout.proj)))
    assert (roots, status) == ([layout.proj], runtime.ROOTS_STATUS_READY)


@pytest.mark.asyncio
async def test_sibling_directory_is_dropped(layout):
    roots, status = await _effective(_ctx(_uri(layout.base / "sibling")))
    assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["parent", "filesystem_root"])
async def test_ancestor_of_server_root_is_dropped_not_clipped(layout, which):
    uri = _uri(layout.base) if which == "parent" else "file:///"
    roots, status = await _effective(_ctx(uri))
    assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER)


@pytest.mark.asyncio
async def test_mixed_list_keeps_valid_roots_in_client_order_and_dedupes(layout):
    roots, status = await _effective(
        _ctx(
            "file:///",
            _uri(layout.proj),
            _uri(layout.base / "sibling"),
            _uri(layout.server),
            _uri(layout.proj),
        )
    )
    assert status == runtime.ROOTS_STATUS_READY
    assert roots == [layout.proj, layout.server]


@pytest.mark.asyncio
async def test_symlink_escaping_server_root_is_dropped(layout):
    link = layout.server / "escape"
    link.symlink_to(layout.base / "sibling")
    roots, status = await _effective(_ctx(_uri(link)))
    assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER)


@pytest.mark.asyncio
async def test_symlink_staying_inside_server_root_is_kept(layout):
    link = layout.server / "alias"
    link.symlink_to(layout.proj)
    roots, status = await _effective(_ctx(_uri(link)))
    assert (roots, status) == ([layout.proj], runtime.ROOTS_STATUS_READY)


@pytest.mark.asyncio
async def test_server_root_that_is_a_file_admits_only_that_file(tmp_path, monkeypatch):
    allowed = (tmp_path / "one.txt").resolve()
    allowed.write_text("x")
    other = (tmp_path / "two.txt").resolve()
    other.write_text("y")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [allowed])
    assert await _effective(_ctx(_uri(allowed))) == ([allowed], runtime.ROOTS_STATUS_READY)
    assert await _effective(_ctx(_uri(other))) == (
        [],
        runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER,
    )


@pytest.mark.asyncio
async def test_empty_intersection_never_falls_back_to_server_roots(layout, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    roots, status = await _effective(_ctx("file:///"))
    assert (roots, status) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER)


@pytest.mark.asyncio
async def test_empty_client_list_still_falls_back_when_opted_in(layout, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    roots, status = await _effective(_ctx())
    assert (roots, status) == ([layout.server], runtime.ROOTS_STATUS_SERVER_FALLBACK)


@pytest.mark.asyncio
async def test_empty_client_list_is_deny_all_by_default(layout):
    assert await _effective(_ctx()) == ([], runtime.ROOTS_STATUS_CLIENT_DENY_ALL)


@pytest.mark.asyncio
async def test_server_only_mode_unchanged(layout, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SERVER_ROOTS_ONLY", "1")
    roots, status = await _effective(_ctx("file:///"))
    assert (roots, status) == ([layout.server], runtime.ROOTS_STATUS_SERVER_ONLY)


@pytest.mark.asyncio
async def test_unsupported_roots_client_unchanged(layout):
    ctx = SimpleNamespace(session=object())
    assert await _effective(ctx) == ([layout.server], runtime.ROOTS_STATUS_UNSUPPORTED_FALLBACK)


@pytest.mark.asyncio
async def test_timeout_and_error_unchanged(layout, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ROOTS_TIMEOUT_SECONDS", "0.01")

    class Hang:
        async def list_roots(self):
            import asyncio

            await asyncio.sleep(5)

    assert await _effective(SimpleNamespace(session=Hang())) == (
        [],
        runtime.ROOTS_STATUS_TIMEOUT,
    )
    ctx = SimpleNamespace(session=_Session(error=RuntimeError("boom")))
    assert await _effective(ctx) == ([], runtime.ROOTS_STATUS_ERROR)


def _bare_path_error(*paths):
    from mcp.types import ListRootsResult
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate({"roots": [{"uri": str(p)} for p in paths]})
    return exc_info.value


@pytest.mark.asyncio
async def test_recovery_branch_is_narrowed_too(layout):
    error = _bare_path_error(layout.proj, layout.base)
    ctx = SimpleNamespace(session=_Session(error=error))
    assert await _effective(ctx) == ([layout.proj], runtime.ROOTS_STATUS_READY)


@pytest.mark.asyncio
async def test_recovery_branch_outside_server_roots_is_deny_all(layout, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    error = _bare_path_error(layout.base, layout.base / "sibling")
    ctx = SimpleNamespace(session=_Session(error=error))
    assert await _effective(ctx) == ([], runtime.ROOTS_STATUS_CLIENT_OUTSIDE_SERVER)


@pytest.mark.asyncio
async def test_empty_server_roots_leave_client_roots_in_charge(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [])
    root = tmp_path.resolve()
    assert await _effective(_ctx(_uri(root), "file:///")) == (
        [root, Path("/").resolve()],
        runtime.ROOTS_STATUS_READY,
    )
    error = _bare_path_error(root)
    ctx = SimpleNamespace(session=_Session(error=error))
    assert await _effective(ctx) == ([root], runtime.ROOTS_STATUS_READY)


def test_narrowing_uses_path_semantics_not_string_prefixes(tmp_path):
    server = (tmp_path / "data").resolve()
    server.mkdir()
    lookalike = (tmp_path / "data-evil").resolve()
    lookalike.mkdir()
    assert runtime._narrow_client_roots([lookalike, server], [server]) == [server]


def test_windows_path_comparison_semantics():
    server = PureWindowsPath("C:/Users/a/files")
    assert PureWindowsPath("c:/users/a/files/x") == PureWindowsPath("C:/Users/a/files/x")
    assert server in PureWindowsPath("C:/Users/a/files/x").parents
    assert PureWindowsPath("D:/files") != server
    assert server not in PureWindowsPath("D:/Users/a/files").parents


@pytest.mark.skipif(os.name != "nt", reason="drive-letter handling needs Windows")
def test_windows_other_drive_is_dropped():  # pragma: no cover - posix dev machine
    here = Path.cwd().resolve()
    other = Path("Z:\\") if here.drive.upper() != "Z:" else Path("Y:\\")
    assert runtime._narrow_client_roots([other], [here]) == []


@pytest.mark.asyncio
async def test_new_status_message_records_invalid_argument(layout, monkeypatch):
    kinds = []
    monkeypatch.setattr(runtime, "record_tool_error", lambda kind, **kw: kinds.append(kind))
    roots, error = await runtime._ensure_allowed_roots(_ctx("file:///"), "download_media")
    assert roots == []
    assert error == OUTSIDE_TEXT.format(tool="download_media")
    assert str(layout.server) not in error
    assert kinds == ["invalid_argument"]


@pytest.mark.asyncio
async def test_existing_status_messages_are_byte_identical(layout, monkeypatch):
    _, error = await runtime._ensure_allowed_roots(_ctx(), "send_file")
    assert (
        error
        == "send_file is disabled because the client provided an empty MCP Roots list (deny-all)."
    )
    ctx = SimpleNamespace(session=_Session(error=RuntimeError("boom")))
    _, error = await runtime._ensure_allowed_roots(ctx, "send_file")
    assert error == (
        "send_file is disabled because MCP Roots could not be verified safely. "
        "Check MCP client/server logs."
    )
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [])
    _, error = await runtime._ensure_allowed_roots(None, "send_file")
    assert error == (
        "send_file is disabled until allowed roots are configured. "
        "Provide server CLI roots and/or client MCP Roots."
    )


# ---------------------------------------------------------------------------
# End to end: the /app/telegram_mcp/*.py scenario through real resolvers/tools
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    app = (tmp_path / "app").resolve()
    code = app / "telegram_mcp"
    code.mkdir(parents=True)
    victim = code / "x.py"
    victim.write_text("ORIGINAL")
    files = app / "data" / "files"
    files.mkdir(parents=True)
    secret = app / "data" / "transcripts.db"
    secret.write_text("secret")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [files.resolve()])
    return SimpleNamespace(app=app, victim=victim, files=files.resolve(), secret=secret)


class _Client:
    def __init__(self):
        self.calls = []

    async def get_messages(self, entity, ids):
        self.calls.append("get_messages")
        return SimpleNamespace(id=ids, media=object(), file=SimpleNamespace(size=3))

    async def download_media(self, *args, **kwargs):
        self.calls.append("download_media")
        raise AssertionError("download must not be reached")

    async def send_file(self, *args, **kwargs):
        self.calls.append("send_file")
        raise AssertionError("send_file must not be reached")


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return "entity"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_root", ["app", "root"])
async def test_download_media_cannot_overwrite_code_via_client_root(
    fake_app, monkeypatch, client_root
):
    client = _Client()
    _patch_client(monkeypatch, client)
    uri = _uri(fake_app.app) if client_root == "app" else "file:///"
    result = await inspect.unwrap(media.download_media)(
        123, 7, file_path=str(fake_app.victim), ctx=_ctx(uri)
    )
    assert result == OUTSIDE_TEXT.format(tool="download_media")
    assert fake_app.victim.read_text() == "ORIGINAL"
    assert "download_media" not in client.calls
    assert list(fake_app.victim.parent.iterdir()) == [fake_app.victim]


@pytest.mark.asyncio
async def test_writable_resolver_refuses_code_path_and_allows_server_root(fake_app):
    ctx = _ctx("file:///", _uri(fake_app.files))
    path, error = await runtime._resolve_writable_file_path(
        raw_path=str(fake_app.victim),
        default_filename="d.bin",
        ctx=ctx,
        tool_name="download_media",
    )
    assert (path, error) == (None, "Path is outside allowed roots.")
    assert fake_app.victim.read_text() == "ORIGINAL"
    path, error = await runtime._resolve_writable_file_path(
        raw_path=str(fake_app.files / "ok.bin"),
        default_filename="d.bin",
        ctx=ctx,
        tool_name="download_media",
    )
    assert error is None and path == fake_app.files / "ok.bin"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_root", ["app", "root"])
async def test_send_file_cannot_read_outside_server_roots(fake_app, monkeypatch, client_root):
    client = _Client()
    _patch_client(monkeypatch, client)
    uri = _uri(fake_app.app) if client_root == "app" else "file:///"
    result = await inspect.unwrap(media.send_file)(123, str(fake_app.secret), ctx=_ctx(uri))
    assert result == OUTSIDE_TEXT.format(tool="send_file")
    assert client.calls == []


@pytest.mark.asyncio
async def test_readable_resolver_refuses_outside_and_allows_inside(fake_app):
    inside = fake_app.files / "a.txt"
    inside.write_text("ok")
    ctx = _ctx("file:///", _uri(fake_app.files))
    path, error = await runtime._resolve_readable_file_path(
        raw_path=str(fake_app.secret), ctx=ctx, tool_name="send_file"
    )
    assert (path, error) == (None, "Path is outside allowed roots.")
    path, error = await runtime._resolve_readable_file_path(
        raw_path=str(inside), ctx=ctx, tool_name="send_file"
    )
    assert error is None and path == inside


@pytest.mark.asyncio
async def test_export_path_refused_for_ancestor_client_root(fake_app):
    path, error = await runtime._resolve_writable_file_path(
        raw_path=str(fake_app.victim),
        default_filename="export.json",
        ctx=_ctx(_uri(fake_app.app)),
        tool_name="export_chat_history",
    )
    assert path is None
    assert error == OUTSIDE_TEXT.format(tool="export_chat_history")
    assert fake_app.victim.read_text() == "ORIGINAL"
