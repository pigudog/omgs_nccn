#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

RULE_GRAPH="${OMGS_NCCN_RULE_GRAPH:-data/processed/ov_2025/rule_graph/ov_2025_global.rule_graph.json}"
NATIVE_MD="${OMGS_NCCN_NATIVE_PRIMARY_MD:-data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/primary.md}"
PAGES_JSON="${OMGS_NCCN_NATIVE_PAGES_JSON:-data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/pages.json}"
OVERRIDES="${OMGS_NCCN_FOOTNOTE_OVERRIDES:-data/manifests/ov_2025_footnote_link_overrides.json}"
CONDA_ENV_NAME="${OMGS_NCCN_CONDA_ENV:-omgs_nccn}"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase5 build-engine-handoff-assets \
  --rule-graph "$RULE_GRAPH" \
  --native-md "$NATIVE_MD" \
  --pages-json "$PAGES_JSON" \
  --overrides "$OVERRIDES" \
  "$@"
