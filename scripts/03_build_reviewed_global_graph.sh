#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

INPUT_ROOT="${OMGS_NCCN_TYPED_PAGE_ROOT:-data/processed/ov_2025/pages}"
STITCH_MAP="${OMGS_NCCN_STITCH_MAP:-data/manifests/ov_2025_stitch_map.json}"
CONDA_ENV_NAME="${OMGS_NCCN_CONDA_ENV:-omgs_nccn}"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase3 build-reviewed-global-graph \
  --input-root "$INPUT_ROOT" \
  --stitch-map "$STITCH_MAP" \
  "$@"
