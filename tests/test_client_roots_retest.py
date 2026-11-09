"""Task 0011 re-test: sink-check TOCTOU, symlinked server roots, parity without server roots."""

from types import SimpleNamespace

import pytest
from mcp.types import ListRootsResult
from pydantic import ValidationError

from telegram_mcp import runtime

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
        "TELEGRAM_DOWNLOAD_SUBDIR",
    ):
        monkeypatch.delenv(name, raising=False)


async def _pin(monkeypatch, ctx):
    roots, _ = await runtime._get_effective_allowed_roots_with_status(ctx)

    async def pinned(_ctx, _tool):
        return roots, None

    monkeypatch.setattr(runtime, "_ensure_allowed_roots", pinned)
    return roots


@pytest.mark.asyncio
async def test_subdir_swapped_to_symlink_after_narrowing_refused(tmp_path, monkeypatch):
    server = (tmp_path / "server").resolve()
    client = server / "proj"
    (client / "sub").mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "s.txt").write_text("S", encoding="utf-8")
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [server])
    ctx = SimpleNamespace(session=_Session([client.as_uri()]))
    roots = await _pin(monkeypatch, ctx)
    assert roots == [client]
    (client / "sub").rmdir()
    (client / "sub").symlink_to(outside, target_is_directory=True)

    path, error = await runtime._resolve_readable_file_path(
        raw_path="sub/s.txt", ctx=None, tool_name="send_file"
    )
    assert path is None and OUTSIDE in error
    path, error = await runtime._resolve_writable_file_path(
        raw_path="sub/new/f.txt", default_filename="x", ctx=None, tool_name="download_media"
    )
    assert path is None and OUTSIDE in error
    assert not (outside / "new").exists()


@pytest.mark.asyncio
async def test_symlinked_server_root_still_works(tmp_path, monkeypatch):
    real = (tmp_path / "real").resolve()
    (real / "proj").mkdir(parents=True)
    (real / "proj" / "a.txt").write_text("a", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    # server roots are stored resolved by the loader; also exercise an unresolved entry
    for server_entry in (real, link):
        monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [server_entry])
        for client_uri in ((real / "proj").as_uri(), (link / "proj").as_uri()):
            ctx = SimpleNamespace(session=_Session([client_uri]))
            roots, _ = await runtime._get_effective_allowed_roots_with_status(ctx)
            assert roots, (server_entry, client_uri)
            path, error = await runtime._resolve_readable_file_path(
                raw_path="a.txt", ctx=ctx, tool_name="send_file"
            )
            assert error is None and path == real / "proj" / "a.txt"
            path, error = await runtime._resolve_writable_file_path(
                raw_path="out/new.bin", default_filename="x", ctx=ctx, tool_name="download_media"
            )
            assert error is None and path == real / "proj" / "out" / "new.bin"


@pytest.mark.asyncio
async def test_symlinked_server_root_no_client_escape(tmp_path, monkeypatch):
    real = (tmp_path / "real").resolve()
    real.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [link])
    ctx = SimpleNamespace(session=_Session([other.as_uri()]))
    roots, _ = await runtime._get_effective_allowed_roots_with_status(ctx)
    assert roots == []


@pytest.mark.asyncio
async def test_default_download_subdir_path(tmp_path, monkeypatch):
    server = (tmp_path / "server").resolve()
    client = server / "proj"
    client.mkdir(parents=True)
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [server])
    ctx = SimpleNamespace(session=_Session([client.as_uri()]))
    path, error = await runtime._resolve_writable_file_path(
        raw_path=None, default_filename="pic.jpg", ctx=ctx, tool_name="download_media"
    )
    assert error is None
    assert path.parent.parent == client and path.name == "pic.jpg"
    assert path.parent.is_dir()

    # the default subdir swapped to a symlink out of the server root is refused
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    sub = path.parent
    sub.rmdir()
    sub.symlink_to(outside, target_is_directory=True)
    path2, error = await runtime._resolve_writable_file_path(
        raw_path=None, default_filename="pic.jpg", ctx=ctx, tool_name="download_media"
    )
    assert path2 is None and OUTSIDE in error
    assert not (outside / "pic.jpg").exists()


@pytest.mark.asyncio
async def test_no_server_roots_client_roots_decide(tmp_path, monkeypatch):
    """Scenarios identical on base: no server roots -> client roots alone decide."""
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [])
    a = (tmp_path / "a").resolve()
    b = (tmp_path / "b").resolve()
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("f", encoding="utf-8")
    (b / "g.txt").write_text("g", encoding="utf-8")
    ctx = SimpleNamespace(session=_Session([a.as_uri(), b.as_uri()]))
    roots, status = await runtime._get_effective_allowed_roots_with_status(ctx)
    assert roots == [a, b] and status == runtime.ROOTS_STATUS_READY
    p, e = await runtime._resolve_readable_file_path(
        raw_path=str(b / "g.txt"), ctx=ctx, tool_name="send_file"
    )
    assert e is None and p == b / "g.txt"
    p, e = await runtime._resolve_readable_file_path(
        raw_path=str(tmp_path / "x.txt"), ctx=ctx, tool_name="send_file"
    )
    assert p is None
    p, e = await runtime._resolve_writable_file_path(
        raw_path=None, default_filename="d.bin", ctx=ctx, tool_name="download_media"
    )
    assert e is None and p.name == "d.bin" and a in p.parents
    # ancestor / filesystem-root client roots are honoured (no ceiling)
    ctx_root = SimpleNamespace(session=_Session([tmp_path.as_uri()]))
    roots, _ = await runtime._get_effective_allowed_roots_with_status(ctx_root)
    assert roots == [tmp_path.resolve()]
    # symlink inside a client root pointing out: resolved path is outside -> refused
    (a / "esc").symlink_to(b, target_is_directory=True)
    ctx_a = SimpleNamespace(session=_Session([a.as_uri()]))
    p, e = await runtime._resolve_readable_file_path(
        raw_path="esc/g.txt", ctx=ctx_a, tool_name="send_file"
    )
    assert p is None
    # empty client list -> deny-all
    ctx_empty = SimpleNamespace(session=_Session([]))
    roots, status = await runtime._get_effective_allowed_roots_with_status(ctx_empty)
    assert roots == [] and status != runtime.ROOTS_STATUS_READY


def test_recovery_recovers_existing_paths_from_validation_error(tmp_path, monkeypatch):
    real = (tmp_path / "ws").resolve()
    real.mkdir()
    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate(
            {"roots": [{"uri": str(real)}, {"uri": r"C:\Users\dev\missing"}]}
        )
    types_seen = {i["type"] for i in exc_info.value.errors()}
    assert types_seen <= {"url_parsing", "url_scheme"}
    assert runtime._coerce_paths_from_list_roots_validation_error(exc_info.value) == [real]


def test_recovery_url_scheme_branch_recovers_existing_drive_shaped_path(tmp_path, monkeypatch):
    # "C:/ws" exists relative to cwd here; same url_scheme code path as a Windows drive.
    (tmp_path / "C:" / "ws").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate({"roots": [{"uri": "C:/ws"}]})
    assert any(i["type"] == "url_scheme" for i in exc_info.value.errors())
    got = runtime._coerce_paths_from_list_roots_validation_error(exc_info.value)
    assert got == [(tmp_path / "C:" / "ws").resolve()]
