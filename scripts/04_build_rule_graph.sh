#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

REVIEWED_GRAPH="${OMGS_NCCN_REVIEWED_GLOBAL_GRAPH:-data/processed/ov_2025/reviewed_graph/ov_2025_global.reviewed_graph.json}"
CONDA_ENV_NAME="${OMGS_NCCN_CONDA_ENV:-omgs_nccn}"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase4 build-rule-graph \
  --reviewed-graph "$REVIEWED_GRAPH" \
  "$@"
