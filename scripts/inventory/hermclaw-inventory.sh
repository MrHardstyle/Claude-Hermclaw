#!/usr/bin/env bash
# Hermclaw Next – read-only host inventory (P00 0.4–0.17).
# Prints one JSON document to stdout. Never modifies the host.
# Usage: hermclaw-inventory.sh [--role ROLE] > inventory-<host>.json
set -u
ROLE="unknown"
if [[ "${1:-}" == "--role" ]]; then ROLE="${2:-unknown}"; fi

json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip("\n")))'; }
cmd() { command -v "$1" >/dev/null 2>&1; }
run() { "$@" 2>/dev/null || true; }

HOST=$(hostname -f 2>/dev/null || hostname)
OS=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")
DEB=$(cat /etc/debian_version 2>/dev/null || echo "")
KERNEL=$(uname -r)
CPU_MODEL=$(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo | sed 's/^ *//')
CPU_THREADS=$(nproc 2>/dev/null || echo 0)
MEM_KB=$(awk '/MemTotal/{print $2}' /proc/meminfo)
DISK=$(run df -P -k / | awk 'NR==2{print $2" "$4}')
GPU="[]"
if cmd nvidia-smi; then
  GPU=$(nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader 2>/dev/null \
    | python3 -c 'import sys,json; print(json.dumps([dict(zip(["name","memory_total","driver","compute_cap"],[x.strip() for x in l.split(",")])) for l in sys.stdin if l.strip()]))')
fi
PORTS=$(run ss -ltnH | awk '{print $4}' | sed 's/.*://' | sort -n | uniq | python3 -c 'import sys,json; print(json.dumps([int(x) for x in sys.stdin.read().split() if x.isdigit()]))')
SERVICES=$(run systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}' | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().split()))')
IFACES=$(run ip -j link show | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
except Exception:
  d=[]
print(json.dumps([{"name":i.get("ifname"),"mac":i.get("address"),"state":i.get("operstate")} for i in d if i.get("link_type")=="ether"]))')
WOL="{}"
if cmd ethtool; then
  WOL=$(for i in $(ls /sys/class/net | grep -v lo); do s=$(ethtool "$i" 2>/dev/null | awk -F: '/Supports Wake-on/{gsub(/ /,"",$2);print $2}'); w=$(ethtool "$i" 2>/dev/null | awk -F: '/^\s*Wake-on/{gsub(/ /,"",$2);print $2}'); echo "$i $s $w"; done \
    | python3 -c 'import sys,json; print(json.dumps({p[0]:{"supports":p[1] if len(p)>1 else "","current":p[2] if len(p)>2 else ""} for p in (l.split() for l in sys.stdin) if p}))')
fi
v() { if cmd "$1"; then run "$@" | head -1 | json_escape; else echo null; fi; }
OLLAMA_VERSION=$(v ollama --version)
OLLAMA_MODELS="null"
if cmd curl; then
  OLLAMA_MODELS=$(curl -s -m 3 http://127.0.0.1:11434/api/tags | python3 -c 'import sys,json
try: print(json.dumps([m["name"] for m in json.load(sys.stdin).get("models",[])]))
except Exception: print("null")')
fi
LITELLM_VERSION=$(v litellm --version)
PODMAN_VERSION=$(v podman --version)
DOCKER_VERSION=$(v docker --version)
PG_VERSION=$(v psql --version)
PY_VERSION=$(v python3 --version)
NODE_VERSION=$(v node --version)
NGINX_VERSION=$(if cmd nginx; then nginx -v 2>&1 | json_escape; else echo null; fi)
GIT_VERSION=$(v git --version)
[[ -z "$WOL" ]] && WOL="{}"
[[ -z "$PORTS" ]] && PORTS="[]"
[[ -z "$SERVICES" ]] && SERVICES="[]"
[[ -z "$IFACES" ]] && IFACES="[]"
CGROUP=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo unknown)

cat <<JSON
{
  "schema": 1,
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "role": "$ROLE",
  "hostname": "$HOST",
  "os": $(echo "$OS" | json_escape),
  "debian_version": "$DEB",
  "kernel": "$KERNEL",
  "cpu": {"model": $(echo "$CPU_MODEL" | json_escape), "threads": $CPU_THREADS},
  "memory_kb": ${MEM_KB:-0},
  "root_disk_kb": {"total_and_free": "$DISK"},
  "gpus": $GPU,
  "listening_tcp_ports": $PORTS,
  "running_services": $SERVICES,
  "interfaces": $IFACES,
  "wake_on_lan": $WOL,
  "cgroup_fs": "$CGROUP",
  "versions": {
    "ollama": $OLLAMA_VERSION, "litellm": $LITELLM_VERSION, "podman": $PODMAN_VERSION,
    "docker": $DOCKER_VERSION, "postgresql_client": $PG_VERSION, "python": $PY_VERSION,
    "node": $NODE_VERSION, "nginx": $NGINX_VERSION, "git": $GIT_VERSION
  },
  "ollama_models": $OLLAMA_MODELS
}
JSON
