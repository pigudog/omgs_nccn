#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "$#" -eq 0 ]; then
  PAGES=(OV-1 OV-2 OV-3)
else
  PAGES=("$@")
fi

PDF_PATH="${OMGS_NCCN_PDF_PATH:-data/ref/nccn_ovarian_cancer_v3_2025.pdf}"
IMAGE_ROOT="${OMGS_NCCN_IMAGE_ROOT:-data/raw/ov_2025/page_assets}"
INVENTORY_PATH="${OMGS_NCCN_PAGE_INVENTORY:-data/raw/ov_2025/page_assets/page_inventory.json}"
MODEL_NAME="${OMGS_NCCN_LLM_MODEL:-gpt-5.1}"
CONDA_ENV_NAME="${OMGS_NCCN_CONDA_ENV:-omgs_nccn}"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase1 init-layout

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase1 build-page-inventory \
  --pdf "$PDF_PATH"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase1 build-llm-drafts \
  --pages "${PAGES[@]}" \
  --image-root "$IMAGE_ROOT" \
  --inventory "$INVENTORY_PATH" \
  --model "$MODEL_NAME"

conda run -n "$CONDA_ENV_NAME" env PYTHONPATH=src python -m omgs_nccn.cli.main \
  phase1 build-page-graph-drafts \
  --pages "${PAGES[@]}"
