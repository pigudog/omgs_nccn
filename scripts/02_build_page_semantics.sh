#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

INPUT_ROOT="${OMGS_NCCN_REVIEWED_PAGE_ROOT:-data/processed/ov_2025/pages}"
PAGES_JSON="${OMGS_NCCN_NATIVE_PAGES_JSON:-data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/pages.json}"
MODEL="${OMGS_NCCN_PAGE_SEMANTICS_MODEL:-gpt-5.1}"
CONDA_ENV_NAME="${OMGS_NCCN_CONDA_ENV:-omgs_nccn}"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase2 build-page-semantics \
  --input-root "$INPUT_ROOT" \
  --pages-json "$PAGES_JSON" \
  --model "$MODEL" \
  "$@"
