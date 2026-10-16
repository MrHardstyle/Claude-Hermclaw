#!/usr/bin/env bash
# Hermclaw Next – live acceptance on the real hosts.
#
# The release was built and tested without access to the LAN (BLOCKER-001): every check that needs the real
# hosts is a `pytest -m live` test. This script runs them after the installation (docs/operations/INSTALLATION.md):
#
#   1. `hermclaw doctor` of the installed release (environment + credentials of the services)
#   2. the live test suites of the release's source checkout, as the service user, with the production
#      configuration (/etc/hermclaw) and the real systemd credentials (systemd-run + LoadCredentialEncrypted=)
#
# Run it as root from a source checkout of the installed version that has a dev venv:
#   git clone <repo> /opt/hermclaw/src && cd /opt/hermclaw/src && git checkout v<version>
#   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
#
# Usage:
#   sudo scripts/ops/live-acceptance.sh --prepare-db     # once, orchestrator: test role + pgvector template database
#   sudo scripts/ops/live-acceptance.sh [--suite NAME]... [--full-wol] [--no-doctor]
#   sudo scripts/ops/live-acceptance.sh --cleanup-db     # drop the test role, template and leftover test databases
#   scripts/ops/live-acceptance.sh --list
#
# Suites: orchestrator (.225): workers models repo_intelligence research sshtools infratools wol gitops backup
#         execution worker (.222): sandbox
# `backup` writes one real backup set to Terra (.60), restore-tests it and applies the retention policy.
# Default: every suite of this host's role (orchestrator: /etc/hermclaw/hermclaw.env, execution worker:
# /etc/hermclaw/worker.env + the hermclaw-exec-worker unit). `gitops` needs a throw-away GitLab project:
#   HERMCLAW_LIVE_GITLAB_PROJECT=hermclaw/sandbox HERMCLAW_LIVE_GITLAB_API=http://192.168.178.226 \
#   HERMCLAW_LIVE_GITLAB_REPO_URL=git@192.168.178.226:hermclaw/sandbox.git
# `--full-wol` also runs the full Wake-on-LAN pipeline (suspends and wakes the model worker; no job may hold it).
#
# Environment: HERMCLAW_ACCEPTANCE_USER (hermclaw), HERMCLAW_CONFIG_DIR (/etc/hermclaw),
# HERMCLAW_CREDSTORE (/etc/credstore.encrypted), HERMCLAW_VENV (/opt/hermclaw/venv),
# HERMCLAW_ACCEPTANCE_DIR (/var/lib/hermclaw/acceptance), HERMCLAW_LIVE_EXEC_ID (exec-222),
# HERMCLAW_LIVE_MODEL_ID (model-224), HERMCLAW_LIVE_WOL_WORKER (model-224), HERMCLAW_LIVE_SEARXNG_URL,
# HERMCLAW_ACCEPTANCE_PGPORT (5432).
#
# Exit: 0 all selected suites passed, 1 a check failed, 2 usage/precondition error.
set -euo pipefail

CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="${HERMCLAW_ACCEPTANCE_USER:-hermclaw}"
CONFIG_DIR="${HERMCLAW_CONFIG_DIR:-/etc/hermclaw}"
CREDSTORE="${HERMCLAW_CREDSTORE:-/etc/credstore.encrypted}"
VENV="${HERMCLAW_VENV:-/opt/hermclaw/venv}"
OUT_DIR="${HERMCLAW_ACCEPTANCE_DIR:-/var/lib/hermclaw/acceptance}"
EXEC_ID="${HERMCLAW_LIVE_EXEC_ID:-exec-222}"
MODEL_ID="${HERMCLAW_LIVE_MODEL_ID:-model-224}"
PGPORT_="${HERMCLAW_ACCEPTANCE_PGPORT:-5432}"
TEST_ROLE=hermclaw_acceptance
TEMPLATE_DB=hermclaw_acceptance_tpl
URL_FILE="${CONFIG_DIR}/secrets/acceptance-test-admin-url"
ORCHESTRATOR_SUITES=(workers models repo_intelligence research sshtools infratools wol gitops backup)
WORKER_SUITES=(sandbox)

log() { printf '%s\n' "live-acceptance: $*" >&2; }
die() {
    log "$*"
    exit 2
}

usage() {
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

suite_files() {
    case "$1" in
        workers) echo tests/integration/test_workers_live.py ;;
        models) echo tests/integration/test_models_live.py ;;
        repo_intelligence) echo tests/integration/test_repo_intelligence_live.py ;;
        research) echo tests/integration/test_research_fetch_search.py tests/integration/test_research_engine.py ;;
        sshtools) echo tests/integration/test_sshtools_live.py ;;
        infratools) echo tests/integration/test_infratools_live.py ;;
        wol) echo tests/integration/test_wol_live.py ;;
        gitops) echo tests/integration/test_gitops_live.py ;;
        backup) echo tests/integration/test_backup_live.py ;;
        sandbox) echo tests/integration/test_sandbox_podman.py tests/integration/test_egress_live.py ;;
        *) return 1 ;;
    esac
}

env_value() { # first value of NAME=... in an env file (no shell evaluation)
    local file="$1" name="$2"
    [[ -r "$file" ]] || return 0
    sed -n "s/^${name}=//p" "$file" | head -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

need_root() { [[ $(id -u) -eq 0 ]] || die "run as root (sudo): the tests read systemd credentials of the services"; }

# ----------------------------------------------------------------------------------------------- test database
prepare_db() {
    need_root
    command -v psql >/dev/null || die "psql not found – run --prepare-db on the orchestrator"
    id "$RUN_USER" >/dev/null 2>&1 || die "user $RUN_USER does not exist"
    local pw url
    pw="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
    # password via stdin (never argv); the role can only create databases and log in over 127.0.0.1/::1
    runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d postgres <<SQL
SELECT 'CREATE ROLE ${TEST_ROLE} LOGIN CREATEDB' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${TEST_ROLE}')\\gexec
ALTER ROLE ${TEST_ROLE} LOGIN CREATEDB NOSUPERUSER NOCREATEROLE PASSWORD '${pw}';
SELECT 'CREATE DATABASE ${TEMPLATE_DB}' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${TEMPLATE_DB}')\\gexec
\\c ${TEMPLATE_DB}
CREATE EXTENSION IF NOT EXISTS vector;
\\c postgres
ALTER DATABASE ${TEMPLATE_DB} IS_TEMPLATE true;
SQL
    url="postgresql://${TEST_ROLE}:${pw}@127.0.0.1:${PGPORT_}/postgres"
    install -d -m 0750 -o root -g "$(id -gn "$RUN_USER")" "${CONFIG_DIR}/secrets"
    (umask 027 && printf '%s\n' "$url" >"${URL_FILE}.new")
    chown "root:$(id -gn "$RUN_USER")" "${URL_FILE}.new"
    mv -f "${URL_FILE}.new" "$URL_FILE"
    log "test role ${TEST_ROLE} and template ${TEMPLATE_DB} ready; URL in ${URL_FILE} (0640 root:$(id -gn "$RUN_USER"))"
}

cleanup_db() {
    need_root
    runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d postgres <<SQL
SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', datname) FROM pg_database
 WHERE datname LIKE 'hermclaw\\_test\\_%' AND datdba = (SELECT oid FROM pg_roles WHERE rolname = '${TEST_ROLE}')\\gexec
SELECT 'ALTER DATABASE ${TEMPLATE_DB} IS_TEMPLATE false' WHERE EXISTS (SELECT FROM pg_database WHERE datname = '${TEMPLATE_DB}')\\gexec
DROP DATABASE IF EXISTS ${TEMPLATE_DB};
DROP ROLE IF EXISTS ${TEST_ROLE};
SQL
    rm -f "$URL_FILE"
    log "test role, template and leftover test databases dropped"
}

# ----------------------------------------------------------------------------------------------- running
UNIT_PROPS=()
UNIT_ENV=()
add_cred() { # add_cred <credential name> – encrypted host credential of the services
    local file="${CREDSTORE}/hermclaw-$1"
    [[ -r "$file" ]] || {
        log "credential $1 missing ($file)"
        return 1
    }
    UNIT_PROPS+=(-p "LoadCredentialEncrypted=$1:${file}")
}

run_unit() { # run_unit <unit name> <command...> – transient unit as the service user, output on the terminal
    local unit="$1"
    shift
    local home
    home="$(getent passwd "$RUN_USER" | cut -d: -f6)"
    systemd-run --quiet --pipe --wait --collect --service-type=exec --unit="$unit" \
        --uid="$RUN_USER" --gid="$(id -gn "$RUN_USER")" \
        -p WorkingDirectory="$CHECKOUT" -p UMask=0077 \
        -E HOME="${home:-/var/lib/hermclaw}" -E PYTHONDONTWRITEBYTECODE=1 -E HERMCLAW_CONFIG_DIR="$CONFIG_DIR" \
        "${UNIT_PROPS[@]}" "${UNIT_ENV[@]}" "$@"
}

run_doctor() {
    [[ -x "${VENV}/bin/hermclaw" ]] || {
        log "doctor: ${VENV}/bin/hermclaw not installed on this host – skipped"
        return 0
    }
    [[ -r "${CONFIG_DIR}/hermclaw.env" ]] || {
        log "doctor: no ${CONFIG_DIR}/hermclaw.env (not the orchestrator) – skipped"
        return 0
    }
    UNIT_PROPS=(-p "EnvironmentFile=${CONFIG_DIR}/hermclaw.env")
    UNIT_ENV=()
    local f name
    for f in "${CREDSTORE}"/hermclaw-*; do
        [[ -e "$f" ]] || continue
        name="${f##*/hermclaw-}"
        UNIT_PROPS+=(-p "LoadCredentialEncrypted=${name}:${f}")
    done
    log "== doctor"
    run_unit "hermclaw-acceptance-doctor-$$" "${VENV}/bin/hermclaw" doctor
}

configure_suite() { # sets UNIT_PROPS/UNIT_ENV for a suite; returns 3 to skip it (reason logged)
    local suite="$1" unit="$2"
    UNIT_PROPS=()
    UNIT_ENV=()
    local creds_dir="/run/credentials/${unit}.service"
    if [[ "$suite" != sandbox ]]; then
        [[ -r "$URL_FILE" ]] || {
            log "$suite: no test database ($URL_FILE) – run --prepare-db first"
            return 3
        }
        UNIT_PROPS+=(-p "LoadCredential=test-admin-url:${URL_FILE}")
        UNIT_ENV+=(-E "HERMCLAW_TEST_TEMPLATE_DB=${TEMPLATE_DB}")
    fi
    case "$suite" in
        workers)
            add_cred "worker-token-${EXEC_ID}" || return 3
            add_cred "worker-token-${MODEL_ID}" || return 3
            UNIT_ENV+=(-E "HERMCLAW_LIVE_EXEC_ID=${EXEC_ID}" -E "HERMCLAW_LIVE_MODEL_ID=${MODEL_ID}"
                -E "HERMCLAW_LIVE_EXEC_TOKEN_FILE=${creds_dir}/worker-token-${EXEC_ID}"
                -E "HERMCLAW_LIVE_MODEL_TOKEN_FILE=${creds_dir}/worker-token-${MODEL_ID}"
                -E "HERMCLAW_LIVE_DATABASE_URL=$(env_value "${CONFIG_DIR}/hermclaw.env" HERMCLAW_DATABASE_URL)")
            [[ -n "${HERMCLAW_LIVE_EXEC_URL:-}" ]] && UNIT_ENV+=(-E "HERMCLAW_LIVE_EXEC_URL=${HERMCLAW_LIVE_EXEC_URL}")
            [[ -n "${HERMCLAW_LIVE_MODEL_URL:-}" ]] && UNIT_ENV+=(-E "HERMCLAW_LIVE_MODEL_URL=${HERMCLAW_LIVE_MODEL_URL}")
            [[ -n "${HERMCLAW_LIVE_MODEL:-}" ]] && UNIT_ENV+=(-E "HERMCLAW_LIVE_MODEL=${HERMCLAW_LIVE_MODEL}")
            ;;
        models | repo_intelligence)
            add_cred litellm-master-key || return 3
            UNIT_ENV+=(-E "HERMCLAW_LIVE_LITELLM_KEY_REF=cred:litellm-master-key")
            ;;
        research)
            [[ -n "${HERMCLAW_LIVE_SEARXNG_URL:-}" ]] && UNIT_ENV+=(-E "HERMCLAW_LIVE_SEARXNG_URL=${HERMCLAW_LIVE_SEARXNG_URL}")
            add_cred litellm-master-key || true
            ;;
        sshtools | infratools) ;;
        wol)
            UNIT_ENV+=(-E "HERMCLAW_LIVE_WOL_WORKER=${HERMCLAW_LIVE_WOL_WORKER:-${MODEL_ID}}")
            if [[ "$FULL_WOL" == 1 ]]; then
                add_cred "worker-token-${HERMCLAW_LIVE_WOL_WORKER:-${MODEL_ID}}" || return 3
                UNIT_ENV+=(-E HERMCLAW_LIVE_WOL_DB=1
                    -E "HERMCLAW_DATABASE_URL=$(env_value "${CONFIG_DIR}/hermclaw.env" HERMCLAW_DATABASE_URL)")
            fi
            ;;
        gitops)
            local v
            for v in HERMCLAW_LIVE_GITLAB_PROJECT HERMCLAW_LIVE_GITLAB_API HERMCLAW_LIVE_GITLAB_REPO_URL; do
                [[ -n "${!v:-}" ]] || {
                    log "gitops: $v not set (throw-away GitLab project) – skipped"
                    return 3
                }
                UNIT_ENV+=(-E "$v=${!v}")
            done
            add_cred gitlab-token || return 3
            UNIT_ENV+=(-E "HERMCLAW_LIVE_GITLAB_TOKEN_REF=cred:gitlab-token"
                -E "HERMCLAW_LIVE_SSH_KEY_REF=${HERMCLAW_LIVE_SSH_KEY_REF:-file:${CONFIG_DIR}/ssh/id_ed25519}"
                -E "HERMCLAW_LIVE_KNOWN_HOSTS=${HERMCLAW_LIVE_KNOWN_HOSTS:-${CONFIG_DIR}/ssh/known_hosts}")
            ;;
        backup)
            [[ -r "${CONFIG_DIR}/backup.yaml" ]] || {
                log "backup: no ${CONFIG_DIR}/backup.yaml – skipped"
                return 3
            }
            # the production database, data paths and the manifest signing key of the backup service
            UNIT_PROPS+=(-p "EnvironmentFile=${CONFIG_DIR}/hermclaw.env")
            [[ -r "${CREDSTORE}/hermclaw-backup-manifest-key" ]] && add_cred backup-manifest-key
            UNIT_ENV+=(-E "HERMCLAW_BACKUP_CONFIG=${CONFIG_DIR}/backup.yaml")
            ;;
        sandbox)
            local uid
            uid="$(id -u "$RUN_USER")"
            [[ -d "/run/user/${uid}" ]] || {
                log "sandbox: /run/user/${uid} missing (loginctl enable-linger ${RUN_USER}) – skipped"
                return 3
            }
            UNIT_ENV+=(-E "XDG_RUNTIME_DIR=/run/user/${uid}")
            ;;
    esac
    return 0 # the optional `[[ -n … ]] && …` overrides above must not decide the status
}

suite_marker() { # pytest -m expression of a suite
    case "$1" in
        # every test of the podman module is the live verification of the rootless sandbox on .222, not only the
        # one marked live (BUG-146)
        sandbox) echo "integration or live" ;;
        *) echo live ;;
    esac
}

SANDBOX_TEST_IMAGE=docker.io/library/alpine:3.20
SANDBOX_IMAGE_PULLED=0
as_run_user_podman() { # podman of the service user (rootless storage of the worker)
    local uid
    uid="$(id -u "$RUN_USER")"
    (cd / && runuser -u "$RUN_USER" -- env XDG_RUNTIME_DIR="/run/user/${uid}" podman "$@")
}

ensure_sandbox_image() { # the podman tests need their small test image in the worker's storage (pulled, never built)
    as_run_user_podman image exists "$SANDBOX_TEST_IMAGE" && return 0
    log "sandbox: pulling test image $SANDBOX_TEST_IMAGE as $RUN_USER (removed again afterwards)"
    if as_run_user_podman pull -q "$SANDBOX_TEST_IMAGE" >/dev/null; then
        SANDBOX_IMAGE_PULLED=1
    else
        log "sandbox: could not pull $SANDBOX_TEST_IMAGE – the podman tests will be skipped"
    fi
}

remove_sandbox_image() {
    [[ $SANDBOX_IMAGE_PULLED -eq 1 ]] || return 0
    as_run_user_podman rmi -f "$SANDBOX_TEST_IMAGE" >/dev/null 2>&1 || log "sandbox: could not remove $SANDBOX_TEST_IMAGE"
}

junit_counts() { # junit_counts <xml> -> "<tests> <skipped>" (empty when unreadable)
    python3 - "$1" 2>/dev/null <<'PY' || true
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
print(sum(int(s.get("tests", 0)) for s in suites), sum(int(s.get("skipped", 0)) for s in suites))
PY
}

run_suite() {
    local suite="$1" unit files rc=0
    unit="hermclaw-acceptance-${suite//_/-}-$$"
    read -r -a files <<<"$(suite_files "$suite")"
    configure_suite "$suite" "$unit" || {
        RESULTS+=("SKIP  $suite")
        return 0
    }
    log "== $suite (${files[*]})"
    local xml="${OUT_DIR}/${STAMP}-${suite}.xml"
    local -a cmd=("${CHECKOUT}/.venv/bin/python" -m pytest -q -rs -p no:cacheprovider -m "$(suite_marker "$suite")"
        --junitxml="$xml" "${files[@]}")
    if [[ "$suite" == sandbox ]]; then
        ensure_sandbox_image
        run_unit "$unit" "${cmd[@]}" || rc=$?
        remove_sandbox_image
    else
        run_unit "$unit" "${CHECKOUT}/infra/systemd/hermclaw-cred-env" HERMCLAW_TEST_ADMIN_URL=test-admin-url -- "${cmd[@]}" || rc=$?
    fi
    local counts tests=0 skipped=0
    counts="$(junit_counts "$xml")"
    [[ -n "$counts" ]] && read -r tests skipped <<<"$counts"
    if [[ $rc -ne 0 ]]; then
        RESULTS+=("FAIL  $suite (exit $rc)")
        FAILED=1
    elif [[ $tests -eq 0 || $skipped -ge $tests ]]; then
        # nothing was verified: never reported as PASS (BUG-146); the reasons are in the log above (-rs)
        RESULTS+=("SKIP  $suite (all ${tests} test(s) skipped, see log)")
    else
        RESULTS+=("PASS  $suite (${tests} tests, ${skipped} skipped)")
    fi
}

# ----------------------------------------------------------------------------------------------- main
SUITES=()
FULL_WOL=0
DOCTOR=1
ACTION=run
while [[ $# -gt 0 ]]; do
    case "$1" in
        --suite)
            [[ $# -ge 2 ]] || die "--suite needs a name"
            suite_files "$2" >/dev/null || die "unknown suite '$2' (see --list)"
            SUITES+=("$2")
            shift 2
            ;;
        --full-wol) FULL_WOL=1 && shift ;;
        --no-doctor) DOCTOR=0 && shift ;;
        --prepare-db) ACTION=prepare && shift ;;
        --cleanup-db) ACTION=cleanup && shift ;;
        --list) ACTION=list && shift ;;
        -h | --help) usage 0 ;;
        *) die "unknown argument '$1' (--help)" ;;
    esac
done

case "$ACTION" in
    list)
        for s in "${ORCHESTRATOR_SUITES[@]}" "${WORKER_SUITES[@]}"; do printf '%-18s %s\n' "$s" "$(suite_files "$s")"; done
        exit 0
        ;;
    prepare) prepare_db && exit 0 ;;
    cleanup) cleanup_db && exit 0 ;;
esac

need_root
[[ -x "${CHECKOUT}/.venv/bin/python" ]] || die "no dev venv in ${CHECKOUT} (python3 -m venv .venv && .venv/bin/pip install -e '.[dev]')"
"${CHECKOUT}/.venv/bin/python" -c "import pytest" 2>/dev/null || die "pytest missing in ${CHECKOUT}/.venv (pip install -e '.[dev]')"
id "$RUN_USER" >/dev/null 2>&1 || die "user $RUN_USER does not exist"
command -v systemd-run >/dev/null || die "systemd-run not found"
if [[ ${#SUITES[@]} -eq 0 ]]; then
    [[ -r "${CONFIG_DIR}/hermclaw.env" ]] && SUITES+=("${ORCHESTRATOR_SUITES[@]}")
    [[ -r "${CONFIG_DIR}/worker.env" && -e /etc/systemd/system/hermclaw-exec-worker.service ]] && SUITES+=("${WORKER_SUITES[@]}")
    [[ ${#SUITES[@]} -gt 0 ]] || die "no Hermclaw role found on this host (${CONFIG_DIR}/hermclaw.env or worker.env); use --suite"
fi
install -d -m 0750 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESULTS=()
FAILED=0
if [[ $DOCTOR -eq 1 ]]; then
    if run_doctor; then RESULTS+=("PASS  doctor"); else
        RESULTS+=("FAIL  doctor")
        FAILED=1
    fi
fi
for s in "${SUITES[@]}"; do run_suite "$s"; done
{
    echo "Hermclaw live acceptance $(hostname) ${STAMP} (checkout $(git -C "$CHECKOUT" describe --tags --always 2>/dev/null || echo unknown))"
    printf '%s\n' "${RESULTS[@]}"
} | tee "${OUT_DIR}/${STAMP}-summary.txt"
exit "$FAILED"
