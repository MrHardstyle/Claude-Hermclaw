#!/usr/bin/env bash
# Hermclaw Next – prepare the Ansible control node (the admin machine), P01 1.14.
#
#   * installs ansible-core into a dedicated virtualenv (pinned range), or reuses an
#     existing installation (--ansible-bin-dir)
#   * creates the inventory from inventory/hosts.example.yml if it does not exist yet
#     (an existing inventory is never overwritten)
#   * validates the inventory and runs a syntax check of every playbook
#
# Usage: bootstrap-control-node.sh [--venv DIR] [--inventory FILE] [--ansible-bin-dir DIR]
#                                  [--python PYTHON] [--skip-syntax-check]
# Defaults: --venv ~/.local/share/hermclaw/ansible-venv
#           --inventory <repo>/infra/ansible/inventory/hosts.yml
# Environment: HERMCLAW_ANSIBLE_CORE_SPEC (default "ansible-core>=2.19,<2.22")
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)
ANSIBLE_DIR="${REPO_ROOT}/infra/ansible"
EXAMPLE_INVENTORY="${ANSIBLE_DIR}/inventory/hosts.example.yml"

VENV="${HOME:-/root}/.local/share/hermclaw/ansible-venv"
INVENTORY="${ANSIBLE_DIR}/inventory/hosts.yml"
ANSIBLE_BIN_DIR=""
PYTHON="python3"
SYNTAX_CHECK=1
ANSIBLE_CORE_SPEC="${HERMCLAW_ANSIBLE_CORE_SPEC:-ansible-core>=2.19,<2.22}"

log() { printf '[bootstrap-control-node] %s\n' "$*"; }
die() { printf '[bootstrap-control-node] ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv) [[ $# -ge 2 ]] || die "--venv needs a value"; VENV="$2"; shift 2 ;;
        --inventory) [[ $# -ge 2 ]] || die "--inventory needs a value"; INVENTORY="$2"; shift 2 ;;
        --ansible-bin-dir) [[ $# -ge 2 ]] || die "--ansible-bin-dir needs a value"; ANSIBLE_BIN_DIR="$2"; shift 2 ;;
        --python) [[ $# -ge 2 ]] || die "--python needs a value"; PYTHON="$2"; shift 2 ;;
        --skip-syntax-check) SYNTAX_CHECK=0; shift ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ "$ANSIBLE_CORE_SPEC" =~ ^ansible-core([<>=!~]=?[0-9][0-9.]*)?(,[<>=!~]=?[0-9][0-9.]*)*$ ]] \
    || die "HERMCLAW_ANSIBLE_CORE_SPEC must look like 'ansible-core>=2.19,<2.22'"

# --- 1. ansible-core ------------------------------------------------------------------------
if [[ -n "$ANSIBLE_BIN_DIR" ]]; then
    [[ -x "${ANSIBLE_BIN_DIR}/ansible-playbook" ]] || die "no ansible-playbook in ${ANSIBLE_BIN_DIR}"
    log "using existing Ansible in ${ANSIBLE_BIN_DIR}"
else
    command -v "$PYTHON" >/dev/null 2>&1 || die "${PYTHON} not found (Debian: apt install python3 python3-venv)"
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "${PYTHON} is older than 3.11 (ansible-core 2.19+ needs >= 3.11)"
    if [[ ! -x "${VENV}/bin/python" ]]; then
        log "creating virtualenv ${VENV}"
        mkdir -p "$(dirname "$VENV")"
        "$PYTHON" -m venv "$VENV" || die "python venv module missing (Debian: apt install python3-venv)"
    fi
    # pip is a no-op (and needs no network) when the installed version satisfies the spec
    log "ensuring ${ANSIBLE_CORE_SPEC} in ${VENV}"
    "${VENV}/bin/python" -m pip install --quiet --disable-pip-version-check "$ANSIBLE_CORE_SPEC"
    ANSIBLE_BIN_DIR="${VENV}/bin"
fi
ansible_version=$("${ANSIBLE_BIN_DIR}/ansible-playbook" --version </dev/null) || die "ansible-playbook does not run"
log "${ansible_version%%$'\n'*}"

# --- 2. inventory ---------------------------------------------------------------------------
if [[ -e "$INVENTORY" ]]; then
    log "inventory ${INVENTORY} exists - left unchanged"
else
    mkdir -p "$(dirname "$INVENTORY")"
    install -m 0640 "$EXAMPLE_INVENTORY" "$INVENTORY"
    log "created ${INVENTORY} from hosts.example.yml - fill in wol_mac and host names before bootstrapping"
fi

if grep -q '00:00:00:00:00:00' "$INVENTORY"; then
    log "WARNING: ${INVENTORY} still contains placeholder MAC addresses (wol_mac)"
fi

export ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg"
"${ANSIBLE_BIN_DIR}/ansible-inventory" -i "$INVENTORY" --graph </dev/null >/dev/null \
    || die "inventory ${INVENTORY} does not parse"
for group in orchestrator webui execution_workers model_workers; do
    count=$("${ANSIBLE_BIN_DIR}/ansible" -i "$INVENTORY" "$group" --list-hosts </dev/null 2>/dev/null \
        | sed -n 's/^ *hosts (\([0-9]*\)):.*/\1/p')
    [[ "${count:-0}" -ge 1 ]] || die "inventory group '${group}' has no hosts"
done
log "inventory OK"

# --- 3. syntax check --------------------------------------------------------------------------
if (( SYNTAX_CHECK == 1 )); then
    for pb in "${ANSIBLE_DIR}"/playbooks/*.yml; do
        "${ANSIBLE_BIN_DIR}/ansible-playbook" -i "$INVENTORY" --syntax-check "$pb" </dev/null >/dev/null \
            || die "syntax check failed: ${pb}"
        log "syntax OK: $(basename "$pb")"
    done
fi

if grep -Eq '^[[:space:]]*ansible_user:[[:space:]]*hermclaw-admin[[:space:]]*$' "$INVENTORY"; then
    log "WARNING: ansible_user is hermclaw-admin, the restricted runtime SSH account - use your own sudo admin login"
fi

cat <<EOF
[bootstrap-control-node] done. Next steps (docs/operations/bootstrap.md):
  export ANSIBLE_CONFIG=${ANSIBLE_DIR}/ansible.cfg PATH=${ANSIBLE_BIN_DIR}:\$PATH
  ansible -i ${INVENTORY} 'all:!gitlab:!backup' -m ansible.builtin.ping
  ansible-playbook -i ${INVENTORY} ${ANSIBLE_DIR}/playbooks/bootstrap.yml --tags preflight   # read-only
  ansible-playbook -i ${INVENTORY} ${ANSIBLE_DIR}/playbooks/bootstrap.yml                    # first run
  ansible-playbook -i ${INVENTORY} ${ANSIBLE_DIR}/playbooks/bootstrap.yml --check --diff     # reruns: expect changed=0
EOF
