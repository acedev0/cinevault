#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

export CINEVAULT_DATA_DIR="${CINEVAULT_DATA_DIR:-$project_dir/data}"
export CINEVAULT_VENV_DIR="${CINEVAULT_VENV_DIR:-$project_dir/.venv}"

exec bash "$project_dir/start.sh"
