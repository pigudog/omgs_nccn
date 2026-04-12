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

DEFAULT_PROVIDER="qwen"
DEFAULT_MODEL="${QWEN_COMPAT_MODEL:-qwen3-max}"

if [[ $# -lt 1 ]]; then
  PYTHONPATH=src "$PYTHON_BIN" -m omgs_nccn.cli.main phase6 run-live-query \
    --case-id 0 \
    --provider "$DEFAULT_PROVIDER" \
    --model "$DEFAULT_MODEL"
  exit 0
fi

if [[ "${1:-}" == "--case-id" ]]; then
  CASE_ID="${2:-}"
  if [[ -z "${CASE_ID}" ]]; then
    echo "missing case id"
    exit 1
  fi
  shift 2 || true
  PYTHONPATH=src "$PYTHON_BIN" -m omgs_nccn.cli.main phase6 run-live-query \
    --case-id "${CASE_ID}" \
    --provider "$DEFAULT_PROVIDER" \
    --model "$DEFAULT_MODEL" \
    "$@"
  exit 0
fi

QUESTION="$1"
shift || true

PYTHONPATH=src "$PYTHON_BIN" -m omgs_nccn.cli.main phase6 run-live-query \
  --question "${QUESTION}" \
  --provider "$DEFAULT_PROVIDER" \
  --model "$DEFAULT_MODEL" \
  "$@"
