#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PDF_PATH="${OMGS_NCCN_PDF_PATH:-data/ref/nccn_ovarian_cancer_v3_2025.pdf}"
CONDA_ENV_NAME="${OMGS_NCCN_CONDA_ENV:-omgs_nccn}"

CMD=(
  conda run -n "$CONDA_ENV_NAME"
  env PYTHONPATH=src
  python -m omgs_nccn.pipeline.phase0_raw
  --pdf "$PDF_PATH"
)

if [ -n "${OMGS_NCCN_MARKER_DEVICE:-}" ]; then
  CMD+=(--marker-device "${OMGS_NCCN_MARKER_DEVICE}")
fi

if [ "${OMGS_NCCN_MARKER_DISABLE_MULTIPROCESSING:-0}" = "1" ]; then
  CMD+=(--marker-disable-multiprocessing)
fi

if [ -n "${OMGS_NCCN_RENDER_SCALE:-}" ]; then
  CMD+=(--render-scale "${OMGS_NCCN_RENDER_SCALE}")
fi

CMD+=("$@")
"${CMD[@]}"
