#!/usr/bin/env bash
# Hermclaw Next – host preflight (P01, Bauplan §2/§6).
#
# Read-only. Checks, for the given inventory group(s):
#   * Debian 13 "trixie" on x86_64
#   * CPU / RAM / free disk against conservative minima per role
#   * every port of the port plan (§6): free, or owned by the expected service
#     (a port held by any other process is a CONFLICT – never overwrite existing services)
#   * pre-existing systemd unit files with Hermclaw names that Hermclaw does not manage
#   * role specifics: cgroup v2 (execution worker), NVIDIA GPU (model worker)
#
# Usage: check-host.sh --role GROUP[,GROUP...] [--json] [--min-ram-mb N] [--min-cpus N]
#                      [--min-disk-gb N] [--allow-conflicts] [--port-plan FILE]
#                      [--os-release FILE]
# Groups: orchestrator, webui, execution_workers, model_workers (others are ignored).
# --port-plan FILE replaces the built-in port plan (same format: "group port service
#   expect[,expect...]" per line, # comments; the environment variable HERMCLAW_PORT_PLAN
#   with the same content is used when --port-plan is absent); --os-release FILE checks an
#   image/chroot.
# An expected owner is a process name (/proc/<pid>/comm) or "name~word": then the
# command line must also contain word (a foreign python3 on port 8000 is a conflict).
# Exit codes: 0 = no failures (warnings possible), 1 = at least one FAIL, 64 = usage error.
#
# Only /proc, /sys, coreutils and awk are used, so it also runs on minimal systems.
# Run as root to see which process owns a listening port (otherwise owners are unknown
# and an occupied port counts as a conflict).
set -euo pipefail
export LC_ALL=C

readonly MANAGED_MARKER="Managed by Hermclaw"
readonly SYSTEMD_DIR="/etc/systemd/system"

# --- port plan (keep in sync with infra/ansible/playbooks/group_vars/all.yml) ---------
# group port service expected-process-names(comma separated)
PORT_PLAN=$(cat <<'EOF'
orchestrator 8000 hermclaw-api hermclaw,python3~hermclaw,python~hermclaw,uvicorn~hermclaw
orchestrator 4000 litellm litellm,python3~litellm,python~litellm
orchestrator 5432 postgresql postgres
orchestrator 8888 searxng rootlessport,pasta,pasta.avx2,slirp4netns,conmon,uwsgi~searx,granian~searx,python3~searx
orchestrator 9100 metrics hermclaw,python3~hermclaw,python~hermclaw
webui 80 nginx-http nginx
webui 443 nginx-https nginx
execution_workers 8787 hermclaw-exec-worker hermclaw-exec-w,python3~hermclaw,python~hermclaw
model_workers 8787 hermclaw-model-worker hermclaw-model-,python3~hermclaw,python~hermclaw
model_workers 11434 ollama ollama
EOF
)

# --- unit files Hermclaw installs per group ---------------------------------------------
UNIT_PLAN=$(cat <<'EOF'
orchestrator hermclaw-api.service
orchestrator hermclaw-scheduler.service
orchestrator hermclaw-litellm.service
orchestrator hermclaw-backup.service
orchestrator hermclaw-backup.timer
execution_workers hermclaw-exec-worker.service
execution_workers hermclaw-wol@.service
model_workers hermclaw-model-worker.service
model_workers hermclaw-wol@.service
model_workers hermclaw-ollama-firewall.service
EOF
)

# --- minima per group: cpus ram_mb disk_gb (free space below /var/lib) -----------------
min_for_group() {
    case "$1" in
        orchestrator) echo "4 8192 20" ;;
        webui) echo "1 1024 2" ;;
        execution_workers) echo "2 4096 30" ;;
        model_workers) echo "4 16384 80" ;;
        *) echo "" ;;
    esac
}

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

ROLES=""
JSON=0
ALLOW_CONFLICTS=0
OVR_RAM=""
OVR_CPUS=""
OVR_DISK=""
PLAN_FILE=""
OS_RELEASE="/etc/os-release"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role) [[ $# -ge 2 ]] || { echo "--role needs a value" >&2; exit 64; }; ROLES="$2"; shift 2 ;;
        --role=*) ROLES="${1#--role=}"; shift ;;
        --json) JSON=1; shift ;;
        --allow-conflicts) ALLOW_CONFLICTS=1; shift ;;
        --min-ram-mb) [[ $# -ge 2 ]] || exit 64; OVR_RAM="$2"; shift 2 ;;
        --min-cpus) [[ $# -ge 2 ]] || exit 64; OVR_CPUS="$2"; shift 2 ;;
        --min-disk-gb) [[ $# -ge 2 ]] || exit 64; OVR_DISK="$2"; shift 2 ;;
        --port-plan) [[ $# -ge 2 ]] || exit 64; PLAN_FILE="$2"; shift 2 ;;
        --os-release) [[ $# -ge 2 ]] || exit 64; OS_RELEASE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

for v in "$OVR_RAM" "$OVR_CPUS" "$OVR_DISK"; do
    if [[ -n "$v" && ! "$v" =~ ^[0-9]+$ ]]; then
        echo "numeric value expected, got '$v'" >&2
        exit 64
    fi
done
if [[ -z "$ROLES" ]]; then
    echo "--role is required" >&2
    usage >&2
    exit 64
fi
if [[ ! "$ROLES" =~ ^[a-z_]+(,[a-z_]+)*$ ]]; then
    echo "invalid --role value" >&2
    exit 64
fi
if [[ -n "$PLAN_FILE" ]]; then
    [[ -f "$PLAN_FILE" && -r "$PLAN_FILE" ]] || { echo "--port-plan: cannot read ${PLAN_FILE}" >&2; exit 64; }
    PORT_PLAN=$(sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' "$PLAN_FILE")
elif [[ -n "${HERMCLAW_PORT_PLAN:-}" ]]; then
    # set by the Ansible common role from hermclaw_port_plan (inventory overrides apply)
    PORT_PLAN=$(printf '%s\n' "$HERMCLAW_PORT_PLAN" | sed -e 's/#.*$//' -e '/^[[:space:]]*$/d')
fi
plan_line_re='^[a-z_]+ [0-9]{1,5} [A-Za-z0-9_.-]+ [A-Za-z0-9_.~-]+(,[A-Za-z0-9_.~-]+)*$'
while read -r _g _p _s _e _extra; do
    [[ -z "$_g" ]] && continue
    if [[ -n "${_extra:-}" || ! "${_g} ${_p} ${_s} ${_e}" =~ $plan_line_re ]] || (( 10#$_p < 1 || 10#$_p > 65535 )); then
        echo "invalid port plan line: ${_g} ${_p} ${_s} ${_e} ${_extra:-}" >&2
        exit 64
    fi
done <<<"$PORT_PLAN"

# --- result collection -------------------------------------------------------------------
RESULTS=()   # "STATUS<TAB>check<TAB>message"
FAILS=0
WARNS=0

sanitize() {  # keep messages printable and JSON-safe without a JSON library
    printf '%s' "$1" | tr -c 'A-Za-z0-9 ._:/,()=<>+%@-' '_'
}

add() {
    local status="$1" check="$2" msg
    msg=$(sanitize "$3")
    RESULTS+=("${status}"$'\t'"${check}"$'\t'"${msg}")
    case "$status" in
        FAIL) FAILS=$((FAILS + 1)) ;;
        WARN) WARNS=$((WARNS + 1)) ;;
    esac
}

has_role() {
    local r
    IFS=',' read -r -a _roles <<<"$ROLES"
    for r in "${_roles[@]}"; do
        [[ "$r" == "$1" ]] && return 0
    done
    return 1
}

# --- OS ---------------------------------------------------------------------------------
check_os() {
    local id="" version_id="" codename="" line
    if [[ -r "$OS_RELEASE" ]]; then
        while IFS='=' read -r key value; do
            value="${value%\"}"; value="${value#\"}"
            case "$key" in
                ID) id="$value" ;;
                VERSION_ID) version_id="$value" ;;
                VERSION_CODENAME) codename="$value" ;;
            esac
        done <"$OS_RELEASE"
    fi
    if [[ "$id" == "debian" && "$version_id" == "13" ]]; then
        line="Debian ${version_id} (${codename:-unknown})"
        if [[ -r /etc/debian_version ]]; then
            line="${line}, point release $(head -c 32 /etc/debian_version | tr -d '\n')"
        fi
        add OK os "$line"
    else
        add FAIL os "expected Debian 13 trixie, found ID=${id:-?} VERSION_ID=${version_id:-?}"
    fi
    local arch
    arch=$(uname -m)
    if [[ "$arch" == "x86_64" ]]; then
        add OK arch "$arch"
    else
        add FAIL arch "expected x86_64, found $arch"
    fi
    if [[ -d /run/systemd/system ]]; then
        add OK systemd "systemd is the init system"
    else
        add WARN systemd "systemd is not running as init (container/chroot?) - services cannot be managed"
    fi
}

# --- resources ----------------------------------------------------------------------------
check_resources() {
    local need_cpus=0 need_ram=0 need_disk=0 role mins c r d
    IFS=',' read -r -a _roles <<<"$ROLES"
    for role in "${_roles[@]}"; do
        mins=$(min_for_group "$role")
        [[ -z "$mins" ]] && continue
        read -r c r d <<<"$mins"
        (( c > need_cpus )) && need_cpus=$c
        (( r > need_ram )) && need_ram=$r
        (( d > need_disk )) && need_disk=$d
    done
    [[ -n "$OVR_CPUS" ]] && need_cpus=$OVR_CPUS
    [[ -n "$OVR_RAM" ]] && need_ram=$OVR_RAM
    [[ -n "$OVR_DISK" ]] && need_disk=$OVR_DISK

    local cpus ram_kb ram_mb
    cpus=$(nproc 2>/dev/null || echo 0)
    ram_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    ram_mb=$(( ${ram_kb:-0} / 1024 ))
    if (( cpus >= need_cpus )); then
        add OK cpu "${cpus} logical CPUs (minimum ${need_cpus})"
    else
        add FAIL cpu "${cpus} logical CPUs, minimum ${need_cpus}"
    fi
    if (( ram_mb >= need_ram )); then
        add OK ram "${ram_mb} MiB RAM (minimum ${need_ram})"
    else
        add FAIL ram "${ram_mb} MiB RAM, minimum ${need_ram}"
    fi
    local target=/var/lib avail_kb avail_gb
    [[ -d "$target" ]] || target=/
    avail_kb=$(df -Pk "$target" 2>/dev/null | awk 'NR==2 {print $4}')
    avail_gb=$(( ${avail_kb:-0} / 1024 / 1024 ))
    if (( avail_gb >= need_disk )); then
        add OK disk "${avail_gb} GiB free below ${target} (minimum ${need_disk})"
    else
        add FAIL disk "${avail_gb} GiB free below ${target}, minimum ${need_disk}"
    fi
}

# --- listening TCP sockets from /proc (port -> owners) ------------------------------------
declare -A PORT_INODES=()
declare -A PORT_ADDRS=()
declare -A INODE_PROC=()
declare -A INODE_PID=()

collect_listeners() {
    local f
    for f in /proc/net/tcp /proc/net/tcp6; do
        [[ -r "$f" ]] || continue
        # columns: sl local_address rem_address st ... inode(10th)
        while read -r laddr inode; do
            local port_hex="${laddr##*:}" addr_hex="${laddr%:*}" port
            port=$((16#$port_hex))
            PORT_INODES[$port]="${PORT_INODES[$port]:-} ${inode}"
            PORT_ADDRS[$port]="${PORT_ADDRS[$port]:-} $(decode_addr "$addr_hex")"
        done < <(awk 'NR > 1 && $4 == "0A" {print $2, $10}' "$f")
    done
    # map socket inodes to process names (needs root for other users' processes)
    local fd target inode pid comm
    for fd in /proc/[0-9]*/fd/*; do
        target=$(readlink "$fd" 2>/dev/null) || continue
        [[ "$target" == socket:\[*\] ]] || continue
        inode="${target#socket:[}"; inode="${inode%]}"
        pid="${fd#/proc/}"; pid="${pid%%/*}"
        comm=$(head -c 64 "/proc/${pid}/comm" 2>/dev/null | tr -cd 'A-Za-z0-9._-') || continue
        INODE_PROC[$inode]="${comm}"
        INODE_PID[$inode]="${pid}"
    done
}

decode_addr() {  # hex (little-endian IPv4 or IPv6) -> printable
    local h="$1"
    if [[ ${#h} -eq 8 ]]; then
        printf '%d.%d.%d.%d' "$((16#${h:6:2}))" "$((16#${h:4:2}))" "$((16#${h:2:2}))" "$((16#${h:0:2}))"
    elif [[ "$h" == "00000000000000000000000000000000" ]]; then
        printf '[::]'
    elif [[ "$h" == "00000000000000000000000001000000" ]]; then
        printf '[::1]'
    else
        printf '[ipv6]'
    fi
}

owner_expected() {  # comm pid expect-list -> 0 when one entry matches
    local comm="$1" pid="$2" entry name word cmdline=""
    IFS=',' read -r -a _entries <<<"$3"
    for entry in "${_entries[@]}"; do
        name="${entry%%~*}"
        [[ "$name" == "$comm" ]] || continue
        if [[ "$entry" != *~* ]]; then
            return 0
        fi
        word="${entry#*~}"
        if [[ -z "$cmdline" ]]; then
            cmdline=$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null | head -c 4096) || cmdline=""
        fi
        [[ "$cmdline" == *"$word"* ]] && return 0
    done
    return 1
}

check_ports() {
    collect_listeners
    local group port service expect seen=""
    while read -r group port service expect; do
        [[ -z "$group" ]] && continue
        has_role "$group" || continue
        # a port shared by two groups on one host is checked once
        [[ " $seen " == *" ${port} "* ]] && continue
        seen="$seen $port"
        local inodes="${PORT_INODES[$port]:-}"
        if [[ -z "${inodes// /}" ]]; then
            add OK "port:${port}" "free (planned for ${service})"
            continue
        fi
        local owners="" unknown=0 foreign=0 inode name
        for inode in $inodes; do
            name="${INODE_PROC[$inode]:-}"
            if [[ -z "$name" ]]; then
                unknown=1
                continue
            fi
            [[ " $owners " == *" ${name} "* ]] || owners="${owners} ${name}"
            if ! owner_expected "$name" "${INODE_PID[$inode]:-}" "$expect"; then
                foreign=1
            fi
        done
        owners="${owners# }"
        local addrs
        addrs=$(tr ' ' '\n' <<<"${PORT_ADDRS[$port]}" | sed '/^$/d' | sort -u | tr '\n' ' ')
        addrs="${addrs% }"
        if (( foreign == 1 || unknown == 1 )); then
            local who="${owners:-unknown owner}"
            (( unknown == 1 && foreign == 0 )) && who="${who} (owner unknown - run as root)"
            if (( ALLOW_CONFLICTS == 1 )); then
                add WARN "port:${port}" "in use by ${who} on ${addrs}, planned for ${service} (conflict allowed)"
            else
                add FAIL "port:${port}" "CONFLICT in use by ${who} on ${addrs}, planned for ${service}"
            fi
        else
            add OK "port:${port}" "in use by expected service ${owners} on ${addrs} (${service})"
        fi
    done <<<"$PORT_PLAN"
}

# --- pre-existing unit files ------------------------------------------------------------------
check_units() {
    local group unit seen=""
    while read -r group unit; do
        [[ -z "$group" ]] && continue
        has_role "$group" || continue
        [[ " $seen " == *" ${unit} "* ]] && continue
        seen="$seen $unit"
        local path="${SYSTEMD_DIR}/${unit}"
        if [[ ! -e "$path" ]]; then
            add OK "unit:${unit}" "not installed yet"
        elif grep -q "$MANAGED_MARKER" "$path" 2>/dev/null; then
            add OK "unit:${unit}" "installed and managed by Hermclaw"
        else
            add FAIL "unit:${unit}" "exists but is NOT managed by Hermclaw - refusing to overwrite"
        fi
    done <<<"$UNIT_PLAN"
    if has_role model_workers && [[ -e "${SYSTEMD_DIR}/ollama.service" ]]; then
        if grep -q "$MANAGED_MARKER" "${SYSTEMD_DIR}/ollama.service" 2>/dev/null; then
            add OK "unit:ollama.service" "installed and managed by Hermclaw"
        else
            add WARN "unit:ollama.service" "existing ollama.service kept; Hermclaw only adds a drop-in"
        fi
    fi
}

# --- role specifics ----------------------------------------------------------------------------
check_specifics() {
    if has_role execution_workers || has_role orchestrator; then
        if [[ -r /sys/fs/cgroup/cgroup.controllers ]]; then
            add OK cgroup "cgroup v2 unified hierarchy ($(tr ' ' ',' </sys/fs/cgroup/cgroup.controllers))"
        else
            add WARN cgroup "cgroup v2 not detected - rootless Podman resource limits need cgroup v2"
        fi
    fi
    if has_role model_workers; then
        local found=0 dev
        for dev in /sys/bus/pci/devices/*; do
            [[ -r "$dev/vendor" && -r "$dev/class" ]] || continue
            if [[ "$(cat "$dev/vendor")" == "0x10de" && "$(cat "$dev/class")" == 0x03* ]]; then
                found=1
            fi
        done
        if (( found == 1 )); then
            add OK gpu "NVIDIA display controller present"
        else
            add FAIL gpu "no NVIDIA GPU found on the PCI bus (model worker needs the GTX 1080)"
        fi
    fi
    if command -v python3 >/dev/null 2>&1; then
        local pv
        pv=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
        if [[ "$(printf '%s\n3.12\n' "$pv" | sort -V | head -n1)" == "3.12" ]]; then
            add OK python "python3 ${pv}"
        else
            add WARN python "python3 ${pv} < 3.12 (bootstrap installs the Debian 13 python3)"
        fi
    else
        add WARN python "python3 missing (bootstrap installs it; Ansible needs it on the target)"
    fi
    if [[ $EUID -ne 0 ]]; then
        add WARN privileges "not running as root - port owners of other users are unknown"
    fi
}

# --- run -------------------------------------------------------------------------------------------
check_os
check_resources
check_ports
check_units
check_specifics

if (( JSON == 1 )); then
    printf '{"host":"%s","roles":"%s","failures":%d,"warnings":%d,"checks":[' \
        "$(sanitize "$(uname -n)")" "$ROLES" "$FAILS" "$WARNS"
    sep=""
    for r in "${RESULTS[@]}"; do
        IFS=$'\t' read -r status check msg <<<"$r"
        printf '%s{"status":"%s","check":"%s","message":"%s"}' "$sep" "$status" "$check" "$msg"
        sep=","
    done
    printf ']}\n'
else
    for r in "${RESULTS[@]}"; do
        IFS=$'\t' read -r status check msg <<<"$r"
        printf '[%-4s] %-18s %s\n' "$status" "$check" "$msg"
    done
    printf 'summary: %d failure(s), %d warning(s)\n' "$FAILS" "$WARNS"
fi

(( FAILS == 0 ))
