#!/usr/bin/env bash
set -euo pipefail

rpc_env_file="${EIMEMORY_RPC_ENV_FILE:-/etc/eimemory/rpc.env}"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
hermes_python="${EIMEMORY_HERMES_PYTHON:-$hermes_home/hermes-agent/venv/bin/python}"

if [ ! -r "$rpc_env_file" ] || [ ! -x "$hermes_python" ]; then
  echo "hermes_gateway_start=failed runtime_prerequisite_missing" >&2
  exit 2
fi

mapfile -t rpc_lines <"$rpc_env_file"
if [ "${#rpc_lines[@]}" != "1" ] || [[ "${rpc_lines[0]}" != EIMEMORY_RPC_AUTH_TOKEN=* ]]; then
  echo "hermes_gateway_start=failed invalid_rpc_auth_file" >&2
  exit 2
fi
rpc_token="${rpc_lines[0]#EIMEMORY_RPC_AUTH_TOKEN=}"
if [ -z "$rpc_token" ]; then
  echo "hermes_gateway_start=failed empty_rpc_auth_token" >&2
  exit 2
fi

export EIMEMORY_RPC_TOKEN="$rpc_token"
export PYTHONDONTWRITEBYTECODE=1
unset EIMEMORY_RPC_AUTH_TOKEN rpc_token rpc_lines
exec "$hermes_python" -B -m hermes_cli.main gateway run --replace
