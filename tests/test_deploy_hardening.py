"""Task 0011: static checks of the container image and compose hardening.

No Docker, no ``.env``: only the tracked files are parsed.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this image (development stage)")
    return path.read_text(encoding="utf-8")


def _production_stage() -> str:
    text = _read("Dockerfile")
    parts = re.split(r"^FROM .* AS production\s*$", text, flags=re.MULTILINE)
    assert len(parts) == 2, "Dockerfile needs a production stage"
    return parts[1]


def test_production_image_code_is_not_owned_by_appuser():
    stage = _production_stage()
    assert not re.search(r"chown\s+-R\s+appuser:appuser\s+/app\s*$", stage, re.MULTILINE)
    assert not re.search(r"chown\s+[^\n]*\s/app(\s|\\|$)", stage.replace("/app/data", ""))
    assert "chown -R appuser:appuser /app/data" in stage
    assert "/app/data/transcripts" in stage and "/app/data/files" in stage
    assert "/home/appuser/.local/state" in stage
    # Source is copied before USER appuser, i.e. as root.
    assert stage.index("COPY telegram_mcp") < stage.index("USER appuser")
    assert re.search(r"^USER appuser\s*$", stage, re.MULTILINE)


def test_bytecode_cannot_be_written_or_swapped():
    text = _read("Dockerfile")
    base = text.split("AS builder")[0]
    assert "PYTHONDONTWRITEBYTECODE=1" in base  # inherited by every stage
    assert "PYTHONNOUSERSITE=1" in base  # no .pth from a writable user site
    ignore = _read(".dockerignore").splitlines()
    for pattern in ("**/__pycache__/", "**/*.pyc", ".env", ".env.*", ".venv/", ".git/"):
        assert pattern in ignore
    assert any(line.startswith("**/*.session") for line in ignore)


def test_dockerignore_keeps_what_the_development_stage_copies():
    ignore = [
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for needed in ("tests", "pyproject.toml", "requirements.txt", "main.py", "telegram_mcp"):
        assert not any(line.rstrip("/") == needed for line in ignore), needed


def _compose():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_read("docker-compose.yml"))
    return data, data["services"]["telegram-mcp"]


def test_compose_runs_read_only_with_explicit_writable_mounts():
    data, service = _compose()
    assert service["read_only"] is True
    assert "/tmp" in service["tmpfs"]
    volumes = service["volumes"]
    assert "./transcript_cache:/app/data/transcripts" in volumes
    assert "telegram_state:/home/appuser/.local/state" in volumes
    assert "telegram_files:/app/data/files" in volumes
    assert {"telegram_state", "telegram_files"} <= set(data["volumes"])
    # Nothing may shadow the code directory.
    assert not any(str(v).split(":")[1:2] == ["/app"] for v in volumes)
    assert service["ports"] == ["127.0.0.1:8765:8765"]


def test_compose_sets_default_server_root_and_audit_log_on_state_volume():
    _, service = _compose()
    env = service["environment"]
    assert env["TELEGRAM_ALLOWED_ROOTS"] == "${TELEGRAM_ALLOWED_ROOTS:-/app/data/files}"
    assert env["TELEGRAM_AUDIT_LOG"].startswith("/home/appuser/.local/state/")


def test_image_session_file_and_audit_log_live_on_the_state_directory():
    """/app is read-only for appuser: a relative session name or the default audit
    log path would be unwritable (crash-loop / silently lost deletion audit)."""
    stage = _production_stage()
    state = "/home/appuser/.local/state/telegram-mcp/"
    session = re.search(r'TELEGRAM_SESSION_NAME="([^"]*)"', stage)
    audit = re.search(r'TELEGRAM_AUDIT_LOG="([^"]*)"', stage)
    assert session and session.group(1).startswith(state)
    assert audit and audit.group(1).startswith(state)
    assert "mkdir -p" in stage and state.rstrip("/") in stage


def test_env_example_does_not_preset_a_relative_session_name():
    for line in _read(".env.example").splitlines():
        if line.startswith("TELEGRAM_SESSION_NAME="):
            assert line.split("=", 1)[1].startswith("/"), line


def test_compose_drops_capabilities_and_privileges():
    _, service = _compose()
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]


def test_compose_does_not_inline_secrets():
    _, service = _compose()
    assert not any(
        key in service["environment"] for key in ("TELEGRAM_SESSION_STRING", "TELEGRAM_API_HASH")
    )


def test_runtime_sys_path_has_no_writable_state_or_data_directory():
    """Nothing under data/state/tmp locations is importable.

    The server is started as ``python main.py`` from /app (root-owned in the
    image); data directories are never put on sys.path or loaded as plugins.
    """
    import telegram_mcp.runtime as runtime

    source = Path(runtime.__file__).parent.parent
    offenders = []
    for path in source.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(
            r"sys\.path\.(insert|append)|site\.addsitedir|importlib\.(util\.)?spec_from_file", text
        ):
            offenders.append(str(path.relative_to(source)))
    assert offenders == []
    assert not any("data" in Path(p).parts[-1:] for p in sys.path if p)


def test_shared_example_compose_has_server_root_and_hardening():
    yaml = pytest.importorskip("yaml")
    path = ROOT / "examples" / "docker" / "docker-compose.shared.yml"
    if not path.exists():
        pytest.skip("shared compose example is absent")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    service = data["services"]["telegram-mcp"]
    env = service["environment"]
    assert env["TELEGRAM_ALLOWED_ROOTS"] == "${TELEGRAM_ALLOWED_ROOTS:-/app/data/files}"
    assert env["TELEGRAM_AUDIT_LOG"].startswith("/home/appuser/.local/state/")
    assert service["read_only"] is True
    assert "/tmp" in service["tmpfs"]
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["ports"] == ["127.0.0.1:8765:8765"]
    volumes = service["volumes"]
    assert "telegram_state:/home/appuser/.local/state" in volumes
    assert "telegram_files:/app/data/files" in volumes
    assert {"telegram_state", "telegram_files"} <= set(data["volumes"])
    assert not any(str(v).split(":")[1:2] == ["/app"] for v in volumes)


def test_readme_shared_docker_run_sets_server_root():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    block = text.split("### Shared server (recommended)")[1].split("```bash")[1].split("```")[0]
    for needed in (
        "TELEGRAM_ALLOWED_ROOTS=/app/data/files",
        "telegram_files:/app/data/files",
        "telegram_state:/home/appuser/.local/state",
        "--read-only",
        "--tmpfs /tmp",
        "127.0.0.1:8765:8765",
    ):
        assert needed in block, needed
