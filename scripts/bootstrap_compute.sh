#!/usr/bin/env bash

set -u

_SCAGENT_COMPUTE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_SCAGENT_COMPUTE_PIXI="${SCAGENT_SDK_PIXI:-$(command -v pixi 2>/dev/null || true)}"
_SCAGENT_COMPUTE_STAMP="${_SCAGENT_COMPUTE_ROOT}/.compute-env.stamp"

if [[ -z "${_SCAGENT_COMPUTE_PIXI}" ]]; then
  for _SCAGENT_COMPUTE_PIXI_CANDIDATE in \
    "${HOME}/.pixi/bin/pixi" \
    "/usersoftware/peerd/${USER}/.pixi/bin/pixi"; do
    if [[ -x "${_SCAGENT_COMPUTE_PIXI_CANDIDATE}" ]]; then
      _SCAGENT_COMPUTE_PIXI="${_SCAGENT_COMPUTE_PIXI_CANDIDATE}"
      break
    fi
  done
fi

if [[ -z "${_SCAGENT_COMPUTE_PIXI}" || ! -x "${_SCAGENT_COMPUTE_PIXI}" ]]; then
  echo "scagent-sdk compute bootstrap requires Pixi" >&2
  exit 1
fi

_SCAGENT_COMPUTE_STORAGE="${SCAGENT_SDK_COMPUTE_STORAGE:-/usersoftware/peerd/${USER}}"
_SCAGENT_COMPUTE_ENVS="${_SCAGENT_COMPUTE_STORAGE}/pixi-envs/scagent-sdk"
_SCAGENT_COMPUTE_CACHE="${_SCAGENT_COMPUTE_STORAGE}/pixi-cache/scagent-sdk"

_scagent_compute_digest() {
  (
    cd "${_SCAGENT_COMPUTE_ROOT}" || exit 1
    sha256sum pixi.toml pixi.lock | sha256sum | awk '{print $1}'
  )
}

_SCAGENT_COMPUTE_EXPECTED="$(_scagent_compute_digest)" || exit 1
if [[ -f "${_SCAGENT_COMPUTE_STAMP}" \
      && "$(<"${_SCAGENT_COMPUTE_STAMP}")" == "${_SCAGENT_COMPUTE_EXPECTED}" \
      && -x "${_SCAGENT_COMPUTE_ROOT}/.pixi/envs/rapids/bin/python" \
      && -x "${_SCAGENT_COMPUTE_ROOT}/.pixi/envs/cellbender/bin/python" \
      && -x "${_SCAGENT_COMPUTE_ROOT}/.pixi/envs/diffxpy/bin/python" ]]; then
  exit 0
fi

echo "Bootstrapping locked scagent-sdk compute environments..."
mkdir -p "${_SCAGENT_COMPUTE_ENVS}" "${_SCAGENT_COMPUTE_CACHE}" || exit 1

"${_SCAGENT_COMPUTE_PIXI}" config set \
  --local \
  --manifest-path "${_SCAGENT_COMPUTE_ROOT}/pixi.toml" \
  detached-environments "${_SCAGENT_COMPUTE_ENVS}" >/dev/null || exit 1

for _SCAGENT_COMPUTE_ENVIRONMENT in rapids cellbender diffxpy; do
  PIXI_CACHE_DIR="${_SCAGENT_COMPUTE_CACHE}" \
    PIXI_CACHE_NETFS_REDIRECT=never \
    "${_SCAGENT_COMPUTE_PIXI}" install \
      --locked \
      --environment "${_SCAGENT_COMPUTE_ENVIRONMENT}" \
      --manifest-path "${_SCAGENT_COMPUTE_ROOT}/pixi.toml" || exit 1
done

for _SCAGENT_COMPUTE_ENVIRONMENT in rapids cellbender diffxpy; do
  PIXI_CACHE_DIR="${_SCAGENT_COMPUTE_CACHE}" \
    "${_SCAGENT_COMPUTE_PIXI}" run \
      --locked \
      --environment "${_SCAGENT_COMPUTE_ENVIRONMENT}" \
      --manifest-path "${_SCAGENT_COMPUTE_ROOT}/pixi.toml" \
      check || exit 1
done

_scagent_compute_digest >"${_SCAGENT_COMPUTE_STAMP}" || exit 1
echo "scagent-sdk compute environments are synchronized with pixi.lock"

unset _SCAGENT_COMPUTE_ROOT _SCAGENT_COMPUTE_PIXI _SCAGENT_COMPUTE_STAMP
unset _SCAGENT_COMPUTE_PIXI_CANDIDATE _SCAGENT_COMPUTE_STORAGE
unset _SCAGENT_COMPUTE_ENVS _SCAGENT_COMPUTE_CACHE _SCAGENT_COMPUTE_EXPECTED
unset _SCAGENT_COMPUTE_ENVIRONMENT
