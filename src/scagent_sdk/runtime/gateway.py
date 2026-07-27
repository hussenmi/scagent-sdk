"""Lifecycle manager for an optional per-user LiteLLM gateway."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

from scagent_sdk.errors import GatewayProbeError
from scagent_sdk.models.profile import ModelProfile


class GatewaySupervisor:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        config_path: str | Path | None,
        log_path: str | Path,
        auto_start: bool = True,
        startup_timeout: float = 30.0,
    ):
        self.profile = profile
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.log_path = Path(log_path).expanduser().resolve()
        self.auto_start = auto_start
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen[str] | None = None
        self._log_handle: IO[str] | None = None

    @property
    def managed(self) -> bool:
        return self.process is not None

    def _ready(self) -> bool:
        if not self.profile.base_url:
            return True
        api_key = self.profile.resolve_api_key()
        headers = {"authorization": f"Bearer {api_key}", "x-api-key": api_key}
        for health_path in self.profile.health_paths:
            request = urllib.request.Request(
                self.profile.base_url.rstrip("/") + health_path,
                headers=headers,
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=1.0) as response:
                    if 200 <= int(response.status) < 300:
                        return True
            except (OSError, urllib.error.HTTPError):
                continue
        return False

    def ensure(self) -> bool:
        if self._ready():
            return False
        if not self.auto_start:
            raise GatewayProbeError(
                f"model gateway is unavailable at {self.profile.base_url} "
                "and auto-start is disabled"
            )
        if self.config_path is None or not self.config_path.is_file():
            raise GatewayProbeError(
                f"gateway is unavailable and no LiteLLM config exists for {self.profile.name}"
            )
        executable = shutil.which("litellm")
        if executable is None:
            raise GatewayProbeError(
                "LiteLLM executable is unavailable; source setup_gpu.sh or install .[gateway]"
            )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                executable,
                "--config",
                str(self.config_path),
                "--host",
                "127.0.0.1",
                "--port",
                "4000",
            ],
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise GatewayProbeError(f"LiteLLM exited during startup; inspect {self.log_path}")
            if self._ready():
                return True
            time.sleep(0.25)
        self.stop()
        raise GatewayProbeError(
            f"LiteLLM did not become ready within {self.startup_timeout:g}s; "
            f"inspect {self.log_path}"
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def __enter__(self) -> GatewaySupervisor:
        self.ensure()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.stop()
