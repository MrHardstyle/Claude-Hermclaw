#!/usr/bin/env bash
# Dependency audit (P36.8): pip-audit over the runtime venv and the LiteLLM venv, npm audit over the UI.
# Usage: scripts/dev/dependency-audit.sh [RUNTIME_VENV] [LITELLM_VENV]
#   defaults: .venv and /opt/litellm/venv ; writes reports to ./audit-reports/
set -euo pipefail
cd "$(dirname "$0")/../.."
RUNTIME_VENV="${1:-.venv}"
LITELLM_VENV="${2:-/opt/litellm/venv}"
OUT=audit-reports
mkdir -p "$OUT"
TOOL_VENV="$(mktemp -d)/pip-audit"
python3 -m venv "$TOOL_VENV"
"$TOOL_VENV/bin/pip" install -q pip-audit
freeze() {  # pip or uv venvs
  if [ -x "$1/bin/pip" ]; then "$1/bin/pip" freeze --exclude-editable; else uv pip freeze --python "$1/bin/python"; fi
}
rc=0
for name in runtime litellm; do
  venv="$RUNTIME_VENV"; [ "$name" = litellm ] && venv="$LITELLM_VENV"
  [ -x "$venv/bin/python" ] || { echo "skip $name: $venv not found"; continue; }
  freeze "$venv" | grep -v '^-e ' > "$OUT/$name-requirements.txt"
  "$TOOL_VENV/bin/pip-audit" -r "$OUT/$name-requirements.txt" --no-deps --progress-spinner off -f json -o "$OUT/$name-audit.json" || rc=1
  echo "$name: $(wc -l < "$OUT/$name-requirements.txt") packages audited -> $OUT/$name-audit.json"
done
if [ -f ui/package-lock.json ] && command -v npm >/dev/null; then
  (cd ui && npm audit --omit=dev --json > "../$OUT/ui-npm-audit.json") || rc=1
  echo "ui: npm audit -> $OUT/ui-npm-audit.json"
fi
exit $rc
