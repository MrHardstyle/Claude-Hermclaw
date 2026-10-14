#!/usr/bin/env bash
# Hermclaw Next – manage encrypted systemd credentials (research 20261008-020, P01 1.13).
#
# Usage (as root on the host that runs the service):
#   hermclaw-credential.sh set NAME [--force]           secret from stdin (not echoed on a TTY)
#   hermclaw-credential.sh generate NAME [--bytes N] [--force]   random secret (base64url)
#   hermclaw-credential.sh check NAME                   decrypts and reports OK/FAIL (never the value)
#   hermclaw-credential.sh list                         lists Hermclaw credentials
#
# A credential NAME is stored as $HERMCLAW_CREDSTORE_DIR/hermclaw-NAME (default
# /etc/credstore.encrypted), encrypted with `systemd-creds encrypt --name=NAME` so it can
# only be loaded by a unit as `LoadCredentialEncrypted=NAME:/etc/credstore.encrypted/hermclaw-NAME`.
# Secrets are passed through stdin only - never on the command line, never printed.
# Environment: HERMCLAW_CREDSTORE_DIR, HERMCLAW_CREDS_WITH_KEY (systemd-creds --with-key, default auto)
set -euo pipefail
umask 077

CREDSTORE="${HERMCLAW_CREDSTORE_DIR:-/etc/credstore.encrypted}"
WITH_KEY="${HERMCLAW_CREDS_WITH_KEY:-auto}"
PREFIX="hermclaw-"

die() { printf 'hermclaw-credential: %s\n' "$1" >&2; exit "${2:-1}"; }
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 64; }

valid_name() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9_.-]{0,62}$ && "$1" != *..* ]]
}

[[ "$WITH_KEY" =~ ^[a-z0-9+-]+$ ]] || die "invalid HERMCLAW_CREDS_WITH_KEY" 64
command -v systemd-creds >/dev/null 2>&1 || die "systemd-creds not found (systemd >= 250 required)" 69

[[ $# -ge 1 ]] || usage
cmd="$1"
shift

encrypt_to() {  # name, reads plaintext from stdin
    local name="$1" target="${CREDSTORE}/${PREFIX}$1" tmp
    mkdir -p "$CREDSTORE"
    chmod 0700 "$CREDSTORE"
    tmp=$(mktemp "${CREDSTORE}/.${PREFIX}${name}.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -f '$tmp'" EXIT
    systemd-creds encrypt --with-key="$WITH_KEY" --name="$name" - "$tmp" >/dev/null
    chmod 0600 "$tmp"
    mv -f "$tmp" "$target"
    trap - EXIT
    printf 'stored %s (LoadCredentialEncrypted=%s:%s)\n' "$target" "$name" "$target"
}

case "$cmd" in
    set|generate)
        [[ $# -ge 1 ]] || usage
        name="$1"
        shift
        valid_name "$name" || die "invalid credential name '${name}'" 64
        force=0
        bytes=32
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --force) force=1; shift ;;
                --bytes)
                    [[ $# -ge 2 && "$2" =~ ^[0-9]+$ && "$2" -ge 16 && "$2" -le 4096 ]] \
                        || die "--bytes needs a number between 16 and 4096" 64
                    bytes="$2"; shift 2 ;;
                *) usage ;;
            esac
        done
        if [[ -e "${CREDSTORE}/${PREFIX}${name}" && $force -eq 0 ]]; then
            die "${CREDSTORE}/${PREFIX}${name} exists (use --force to replace it)" 73
        fi
        if [[ "$cmd" == generate ]]; then
            head -c "$bytes" /dev/urandom | base64 -w0 | tr '+/' '-_' | tr -d '=' | encrypt_to "$name"
        else
            secret=""
            if [[ -t 0 ]]; then
                IFS= read -r -s -p "secret for ${name}: " secret
                printf '\n' >&2
            else
                IFS= read -r secret || true
            fi
            [[ -n "$secret" ]] || die "empty secret refused" 65
            printf '%s' "$secret" | encrypt_to "$name"
            unset secret
        fi
        ;;
    check)
        [[ $# -eq 1 ]] || usage
        name="$1"
        valid_name "$name" || die "invalid credential name '${name}'" 64
        file="${CREDSTORE}/${PREFIX}${name}"
        [[ -f "$file" ]] || die "${file} missing" 66
        if size=$(systemd-creds decrypt --name="$name" "$file" - 2>/dev/null | wc -c) && (( size > 0 )); then
            printf 'OK %s\n' "$name"
        else
            die "FAIL ${name}: cannot be decrypted on this host (or empty)" 65
        fi
        ;;
    list)
        [[ $# -eq 0 ]] || usage
        shopt -s nullglob
        for f in "${CREDSTORE}/${PREFIX}"*; do
            printf '%s\n' "${f##*/${PREFIX}}"
        done
        ;;
    -h|--help) usage ;;
    *) usage ;;
esac
