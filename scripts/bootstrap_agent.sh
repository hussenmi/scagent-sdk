#!/usr/bin/env bash

set -u

_SCAGENT_BOOTSTRAP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_SCAGENT_BOOTSTRAP_UV="${SCAGENT_SDK_UV:-$(command -v uv 2>/dev/null || true)}"
_SCAGENT_BOOTSTRAP_STAMP="${_SCAGENT_BOOTSTRAP_ROOT}/.agent-env.stamp"
_SCAGENT_BOOTSTRAP_VENV="${_SCAGENT_BOOTSTRAP_ROOT}/.venv"

if [[ -z "${_SCAGENT_BOOTSTRAP_UV}" || ! -x "${_SCAGENT_BOOTSTRAP_UV}" ]]; then
  echo "scagent-sdk bootstrap requires uv" >&2
  exit 1
fi

_scagent_environment_digest() {
  (
    cd "${_SCAGENT_BOOTSTRAP_ROOT}" || exit 1
    sha256sum pyproject.toml uv.lock .python-version | sha256sum | awk '{print $1}'
  )
}

_SCAGENT_BOOTSTRAP_EXPECTED="$(_scagent_environment_digest)" || exit 1
if [[ -x "${_SCAGENT_BOOTSTRAP_VENV}/bin/python" \
      && -f "${_SCAGENT_BOOTSTRAP_STAMP}" \
      && "$(<"${_SCAGENT_BOOTSTRAP_STAMP}")" == "${_SCAGENT_BOOTSTRAP_EXPECTED}" ]]; then
  exit 0
fi

echo "Bootstrapping locked scagent-sdk agent environment..."
"${_SCAGENT_BOOTSTRAP_UV}" python install 3.12 >/dev/null || exit 1

if [[ -x "${_SCAGENT_BOOTSTRAP_VENV}/bin/python" ]]; then
  _SCAGENT_BOOTSTRAP_BASE="$(${_SCAGENT_BOOTSTRAP_VENV}/bin/python -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
  if [[ "${_SCAGENT_BOOTSTRAP_BASE}" == *"miniconda"* ]]; then
    _SCAGENT_BOOTSTRAP_BACKUP="${_SCAGENT_BOOTSTRAP_ROOT}/.venv.legacy-conda"
    if [[ -e "${_SCAGENT_BOOTSTRAP_BACKUP}" ]]; then
      _SCAGENT_BOOTSTRAP_BACKUP="${_SCAGENT_BOOTSTRAP_BACKUP}.$(date +%Y%m%d%H%M%S)"
    fi
    echo "Preserving Conda-linked agent venv at ${_SCAGENT_BOOTSTRAP_BACKUP}"
    mv "${_SCAGENT_BOOTSTRAP_VENV}" "${_SCAGENT_BOOTSTRAP_BACKUP}" || exit 1
  fi
fi

if [[ ! -x "${_SCAGENT_BOOTSTRAP_VENV}/bin/python" ]]; then
  "${_SCAGENT_BOOTSTRAP_UV}" venv \
    --python 3.12 \
    --python-preference only-managed \
    --prompt scagent-sdk \
    "${_SCAGENT_BOOTSTRAP_VENV}" || exit 1
fi

(
  cd "${_SCAGENT_BOOTSTRAP_ROOT}" || exit 1
  UV_PROJECT_ENVIRONMENT="${_SCAGENT_BOOTSTRAP_VENV}" \
    "${_SCAGENT_BOOTSTRAP_UV}" sync \
      --locked \
      --all-extras \
      --link-mode copy \
      --python 3.12 \
      --python-preference only-managed
) || exit 1

_scagent_environment_digest >"${_SCAGENT_BOOTSTRAP_STAMP}" || exit 1
echo "scagent-sdk agent environment is synchronized with uv.lock"

unset _SCAGENT_BOOTSTRAP_ROOT _SCAGENT_BOOTSTRAP_UV _SCAGENT_BOOTSTRAP_STAMP
unset _SCAGENT_BOOTSTRAP_VENV _SCAGENT_BOOTSTRAP_EXPECTED _SCAGENT_BOOTSTRAP_BASE
unset _SCAGENT_BOOTSTRAP_BACKUP
