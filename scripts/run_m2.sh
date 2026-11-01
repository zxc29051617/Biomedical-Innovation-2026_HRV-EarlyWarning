#!/bin/sh
# Run from any directory. Keep patient-level products in the ignored work dir.
set -eu
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
hrv_python="${HRV_PYTHON:-python}"
hrv_paths="${HRV_PATHS:-configs/paths.local.toml}"
hrv_work="${HRV_WORK_DIR:-artifacts/m2}"
"$hrv_python" -m hrv_early_warning feasibility --paths "$hrv_paths" --output reports/local/feasibility.json
"$hrv_python" -m hrv_early_warning vitals --paths "$hrv_paths" --work-dir "$hrv_work" --output reports/local/vitals.json
"$hrv_python" scripts/run_signals_managed.py --paths "$hrv_paths" --work-dir "$hrv_work" --workers 2
"$hrv_python" -m hrv_early_warning develop --paths "$hrv_paths" --work-dir "$hrv_work" --output reports/local/development.json
"$hrv_python" -m hrv_early_warning compare --paths "$hrv_paths" --work-dir "$hrv_work" --replicates 200
