from __future__ import annotations

from pathlib import Path

import pytest

from scagent_sdk.cli import _load_project_environment


def test_dotenv_loads_capability_key_without_overriding_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API_KEY=from-file\nEXTRA_TEST_KEY=loaded\n", encoding="utf-8")
    monkeypatch.setenv("SCAGENT_SDK_ENV_FILE", str(env_file))
    monkeypatch.setenv("TAVILY_API_KEY", "from-shell")
    monkeypatch.delenv("EXTRA_TEST_KEY", raising=False)

    assert _load_project_environment() == env_file
    assert __import__("os").environ["TAVILY_API_KEY"] == "from-shell"
    assert __import__("os").environ["EXTRA_TEST_KEY"] == "loaded"
