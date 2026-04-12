#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV="${OMGS_NCCN_ENV_FILE:-$REPO_ROOT/.env}"
PYTHON_BIN="${PYTHON_BIN:-python}"

if [[ -z "${QWEN_COMPAT_API_KEY:-}" || -z "${QWEN_COMPAT_BASE_URL:-}" ]]; then
  if [[ -f "$LOCAL_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$LOCAL_ENV"
    set +a
  fi
fi

PYTHONPATH=src "$PYTHON_BIN" -m omgs_nccn.cli.main phase6 build-query-testset "$@"
