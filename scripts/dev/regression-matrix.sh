#!/usr/bin/env bash
# Code task regression matrix (P19.8 / P37): every supported task type through the full runtime
# (planner -> scope -> Qwen3-Coder tool loop -> deterministic verifier -> heavy review -> commit/push -> report).
# Usage: scripts/dev/regression-matrix.sh [-k EXPR]   (needs PostgreSQL, git, node/tsc, php, ffmpeg)
set -euo pipefail
cd "$(dirname "$0")/../.."
exec .venv/bin/pytest -q -p no:cacheprovider tests/e2e "$@"
