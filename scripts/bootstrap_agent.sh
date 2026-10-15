#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT/.venv"
PYTHON_VERSION="$(<"$ROOT/.python-version")"

# optional uv path (UV_EXEC) from .env; otherwise the uv found by setup_gpu.sh
if [[ -f "$ROOT/.env" ]]; then
    source "$ROOT/.env"
fi
UV_EXEC="${UV_EXEC:-${SCAGENT_SDK_UV:-uv}}"

# pass --update-lock-file to re-solve uv.lock instead of requiring it up-to-date
LOCKED_ARGS=(--locked)
for arg in "$@"; do
    if [[ "$arg" == "--update-lock-file" ]]; then
        LOCKED_ARGS=()
    fi
done

# install python & create venv (skip if it already exists)
$UV_EXEC python install "$PYTHON_VERSION"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    $UV_EXEC venv \
        --python "$PYTHON_VERSION" \
        --python-preference only-managed \
        --prompt scagent-sdk \
        "$VENV_DIR"
fi

# sync dependencies into the venv
UV_PROJECT_ENVIRONMENT="$VENV_DIR" $UV_EXEC sync \
    "${LOCKED_ARGS[@]}" \
    --all-extras \
    --link-mode copy \
    --python "$PYTHON_VERSION" \
    --python-preference only-managed
