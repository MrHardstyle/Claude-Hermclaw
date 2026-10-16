#!/usr/bin/env bash
# Push the live job fixtures (tests/live/fixtures/<name>) as branches acceptance/<name> of the test repository.
#
#   sudo -u hermclaw scripts/ops/acceptance-fixtures.sh git@192.168.178.226:local-ai/hermclaw-test.git [name...]
#
# Every branch is a single root commit with exactly the fixture's files, force-pushed, so each battery run starts
# from the same state. Only refs/heads/acceptance/* are written; no other branch of the repository is touched.
# SSH: the orchestrator's deploy key (/etc/hermclaw/ssh/id_ed25519, known_hosts next to it) unless GIT_SSH_COMMAND
# is set.
set -euo pipefail

die() { echo "acceptance-fixtures: $*" >&2; exit 1; }

[[ $# -ge 1 ]] || die "usage: $0 REPO_URL [fixture...]"
url="$1"
shift
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixtures="$(cd "${here}/../../tests/live/fixtures" && pwd)"
key_dir="${HERMCLAW_SSH_DIR:-/etc/hermclaw/ssh}"
if [[ -z "${GIT_SSH_COMMAND:-}" && -r "${key_dir}/id_ed25519" ]]; then
    export GIT_SSH_COMMAND="ssh -i ${key_dir}/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${key_dir}/known_hosts"
fi

names=("$@")
if [[ ${#names[@]} -eq 0 ]]; then
    for d in "${fixtures}"/*/; do names+=("$(basename "$d")"); done
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
for name in "${names[@]}"; do
    [[ "$name" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid fixture name '$name'"
    src="${fixtures}/${name}"
    [[ -d "$src" ]] || die "no fixture '$name' in ${fixtures}"
    repo="${work}/${name}"
    mkdir -p "$repo"
    cp -a "${src}/." "$repo/"
    find "$repo" -name __pycache__ -type d -prune -exec rm -rf {} +
    git -C "$repo" init -q -b "acceptance/${name}"
    git -C "$repo" add -A
    git -C "$repo" -c user.name="Hermclaw acceptance" -c user.email="acceptance@hermclaw.invalid" \
        commit -q -m "acceptance fixture: ${name}"
    git -C "$repo" push -q --force "$url" "HEAD:refs/heads/acceptance/${name}"
    echo "pushed acceptance/${name} ($(git -C "$repo" rev-parse --short HEAD))"
done
