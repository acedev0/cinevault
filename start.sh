#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${CINEVAULT_DATA_DIR:-$PROJECT_DIR/data}"
VENV_DIR="${CINEVAULT_VENV_DIR:-$PROJECT_DIR/.venv}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
  "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi
if [[ ! -f "$DATA_DIR/secrets.enc" ]]; then
  "$VENV_DIR/bin/python" -m cinevault --data-dir "$DATA_DIR" setup
fi
SAVED_PORT="$(awk -F= '$1 == "CINEVAULT_PORT" { print $2 }' "$DATA_DIR/runtime.env" | tail -n 1 | tr -d '[:space:]')"
BIND_PORT="${PORT:-${CINEVAULT_PORT:-${SAVED_PORT:-8765}}}"
[[ "$BIND_PORT" =~ ^[0-9]+$ ]] || { printf 'Invalid web port: %s\n' "$BIND_PORT" >&2; exit 1; }
export CINEVAULT_DATA_DIR="$DATA_DIR"
exec "$VENV_DIR/bin/uvicorn" cinevault.app:create_app --factory --host 0.0.0.0 --port "$BIND_PORT" --workers 1 --no-proxy-headers
