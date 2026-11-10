"""Task 0011 round-1 review follow-ups: sink-check defense in depth, runner warning."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import ListRootsResult
from pydantic import ValidationError

from telegram_mcp import runner, runtime
from telegram_mcp.tools import media  # noqa: F401

TOOLS_DIR = Path(runtime.__file__).resolve().parent / "tools"
OUTSIDE = "Path is outside allowed roots."


class _Session:
    def __init__(self, roots=None, error=None):
        self._roots = roots or []
        self._error = error

    async def list_roots(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(roots=[SimpleNamespace(uri=u) for u in self._roots])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK",
        "TELEGRAM_SERVER_ROOTS_ONLY",
        "TELEGRAM_ALLOWED_ROOTS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def swap(tmp_path, monkeypatch):
    """Server root with a narrowed client root; swap() turns it into a symlink out."""
    server = (tmp_path / "server").resolve()
    client = server / "proj"
    client.mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [server])
    ctx = SimpleNamespace(session=_Session([client.as_uri()]))

    async def narrow_then_pin():
        roots, _ = await runtime._get_effective_allowed_roots_with_status(ctx)
        assert roots == [client]

        async def pinned(_ctx, _tool):
            return roots, None

        monkeypatch.setattr(runtime, "_ensure_allowed_roots", pinned)
        client.rmdir()
        client.symlink_to(outside, target_is_directory=True)

    return SimpleNamespace(server=server, client=client, outside=outside, run=narrow_then_pin)


@pytest.mark.asyncio
async def test_readable_sink_refuses_client_root_swapped_to_symlink(swap):
    await swap.run()
    path, error = await runtime._resolve_readable_file_path(
        raw_path="secret.txt", ctx=None, tool_name="send_file"
    )
    assert path is None
    assert OUTSIDE in error


@pytest.mark.asyncio
async def test_writable_sink_refuses_client_root_swapped_to_symlink(swap):
    await swap.run()
    path, error = await runtime._resolve_writable_file_path(
        raw_path="new.txt", default_filename="x.bin", ctx=None, tool_name="download_media"
    )
    assert path is None
    assert OUTSIDE in error
    assert not (swap.outside / "new.txt").exists()


@pytest.mark.asyncio
async def test_writable_sink_refuses_swapped_parent_only(swap, monkeypatch):
    # candidate inside server root lexically, parent swapped to a link out of it
    await swap.run()
    path, error = await runtime._resolve_writable_file_path(
        raw_path=str(swap.client / "sub" / "f.txt"),
        default_filename="x.bin",
        ctx=None,
        tool_name="download_media",
    )
    assert path is None and OUTSIDE in error


@pytest.mark.asyncio
async def test_sinks_unchanged_without_server_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [])
    root = tmp_path.resolve()
    (root / "a.txt").write_text("a", encoding="utf-8")
    ctx = SimpleNamespace(session=_Session([root.as_uri()]))
    path, error = await runtime._resolve_readable_file_path(
        raw_path="a.txt", ctx=ctx, tool_name="send_file"
    )
    assert error is None and path == root / "a.txt"


@pytest.mark.asyncio
async def test_recovery_branch_skips_missing_roots(tmp_path, monkeypatch):
    server = tmp_path.resolve()
    (server / "real").mkdir()
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [server])
    paths = [server / "real", server / "missing"]
    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate({"roots": [{"uri": str(p)} for p in paths]})
    ctx = SimpleNamespace(session=_Session(error=exc_info.value))
    roots, status = await runtime._get_effective_allowed_roots_with_status(ctx)
    assert roots == [server / "real"]
    assert status == runtime.ROOTS_STATUS_READY


def _caller_tools():
    names = set()
    for source in TOOLS_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                    "_resolve_readable_file_path",
                    "_resolve_writable_file_path",
                ):
                    names.add(fn.name)
                    break
    return {"send_album" if n == "_send_album" else n for n in names}


def test_roots_guarded_tools_equal_real_caller_set():
    assert set(runner._ROOTS_GUARDED_TOOLS) == _caller_tools()
    assert len(set(runner._ROOTS_GUARDED_TOOLS)) == len(runner._ROOTS_GUARDED_TOOLS)


def test_warning_fires_for_read_only_file_writing_tools(monkeypatch, capsys):
    registry = {t.name: t for t in runtime.mcp._tool_manager.list_tools()}
    for name in ("open_photo", "preview_custom_emoji"):
        assert registry[name].annotations.readOnlyHint is True
        only = [SimpleNamespace(name=name)]
        monkeypatch.setattr(runtime.mcp._tool_manager, "list_tools", lambda only=only: only)
        monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [])
        runner._warn_unrooted_network_transport("sse")
        err = capsys.readouterr().err
        assert err.startswith("WARNING:")
        assert "optionally with TELEGRAM_SERVER_ROOTS_ONLY=1" in err


def test_media_module_imported():  # keeps tool registration loaded for the registry scan
    assert media.send_file
