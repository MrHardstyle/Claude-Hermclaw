#!/usr/bin/env bash
# Hermclaw Next – deploy a built release without handing the become password to anyone (operator wrapper).
#
# Installed once by the operator, owned by root:
#   sudo install -o root -g root -m 0755 scripts/deploy/hermclaw-deploy.sh /usr/local/sbin/hermclaw-deploy
# Used by the operator or by an agent session the operator allowed to deploy:
#   sudo -n /usr/local/sbin/hermclaw-deploy <version> [--check] [--allow-active-jobs]
#
#   <version>            release to deploy, e.g. 0.1.0rc65; dist/release-<version>/ must have been built
#                        (scripts/deploy/build-release.sh)
#   --check              preflight only (version, release, inventory, active jobs, lock); decrypts nothing
#   --allow-active-jobs  deploy although jobs are queued or running (the deploy restarts API and scheduler)
#
# The script decrypts the become password (systemd credential "ansible-become", host key: root only) into a private
# file in /dev/shm, runs playbooks/deploy.yml as the deploy user with --become-password-file and shreds the file on
# every exit. The password is never printed and never passes through the caller's shell. Paths come from the
# constants below or from the root-owned /etc/hermclaw/deploy.env – never from the caller's environment.
#
# Exit codes: 0 deployed (or preflight ok), 1 not root / environment broken, 2 usage, 3 refused by a preflight check,
# 4 another deploy is running, otherwise the exit code of ansible-playbook.
set -euo pipefail
umask 077

DEPLOY_USER=kiworker
DEPLOY_HOME=/home/kiworker
REPO_DIR=/home/kiworker/hermclaw-next
CREDENTIAL=/home/kiworker/.config/hermclaw-deploy/become-password.cred
ANSIBLE_PLAYBOOK=/home/kiworker/.local/share/hermclaw/ansible-venv/bin/ansible-playbook
DATABASE=hermclaw
LOG_DIR=/var/log/hermclaw-deploy
LOCK_FILE=/run/hermclaw-deploy.lock
CONFIG=/etc/hermclaw/deploy.env

usage() {
  sed -n '6,12p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
fail() {
  local code=$1
  shift
  echo "hermclaw-deploy: $*" >&2
  exit "$code"
}

version=""
check=0
allow_active=0
for arg in "$@"; do
  case "$arg" in
    --check) check=1 ;;
    --allow-active-jobs) allow_active=1 ;;
    -h | --help) usage ;;
    -*) usage ;;
    *) [[ -z "$version" ]] || usage; version=$arg ;;
  esac
done
[[ -n "$version" ]] || usage
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+|\.post[0-9]+)?$ ]] || fail 2 "not a release version: '$version'"
[[ $EUID -eq 0 ]] || fail 1 "must run as root (sudo -n $0 $version)"

if [[ -f "$CONFIG" ]]; then
  [[ "$(stat -c %u "$CONFIG")" == 0 ]] || fail 1 "$CONFIG must be owned by root"
  # shellcheck source=/dev/null
  source "$CONFIG"
fi
ansible_dir=$REPO_DIR/infra/ansible
release_dir=$REPO_DIR/dist/release-$version

# ---- preflight
[[ -x "$ANSIBLE_PLAYBOOK" ]] || fail 1 "ansible-playbook not found: $ANSIBLE_PLAYBOOK"
[[ -f "$ansible_dir/playbooks/deploy.yml" ]] || fail 1 "playbook not found: $ansible_dir/playbooks/deploy.yml"
[[ -f "$release_dir/release.json" ]] || fail 3 "release $version is not built: $release_dir/release.json missing"
inventory_version=$(sed -n 's/^ *hermclaw_version: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$ansible_dir/inventory/hosts.yml" | head -n1)
[[ "$inventory_version" == "$version" ]] ||
  fail 3 "inventory/hosts.yml names hermclaw_version '$inventory_version', not '$version' – set it first"
active=$(runuser -u postgres -- psql -d "$DATABASE" -Atc \
  "select count(*) from jobs where status not in ('succeeded', 'failed', 'cancelled')" 2>/dev/null) ||
  fail 1 "cannot count the active jobs in database $DATABASE"
if [[ "$active" != 0 && $allow_active -eq 0 ]]; then
  fail 3 "$active job(s) queued or running; the deploy restarts the scheduler (--allow-active-jobs to deploy anyway)"
fi
exec 9>"$LOCK_FILE"
flock -n 9 || fail 4 "another deploy is running"
echo "hermclaw-deploy: preflight ok for $version (inventory $inventory_version, active jobs $active)"
[[ $check -eq 0 ]] || exit 0

# ---- deploy
mkdir -p "$LOG_DIR"
chgrp "$DEPLOY_USER" "$LOG_DIR" && chmod 0750 "$LOG_DIR"
log=$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$version.log
install -m 0640 -g "$DEPLOY_USER" /dev/null "$log"
secret=$(mktemp /dev/shm/hc-become.XXXXXX)
trap 'shred -u "$secret" 2>/dev/null || rm -f "$secret"' EXIT
systemd-creds decrypt --name=ansible-become "$CREDENTIAL" - >"$secret" 2>/dev/null ||
  fail 1 "cannot decrypt the become password ($CREDENTIAL)"
chown "$DEPLOY_USER" "$secret"
echo "hermclaw-deploy: deploying $version, log $log"
set +e
runuser -u "$DEPLOY_USER" -- env -i -C "$ansible_dir" HOME="$DEPLOY_HOME" USER="$DEPLOY_USER" LOGNAME="$DEPLOY_USER" \
  PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
  "$ANSIBLE_PLAYBOOK" -i inventory/hosts.yml playbooks/deploy.yml --become-password-file "$secret" \
  -e "hermclaw_version=$version" >"$log" 2>&1
rc=$?
set -e
tail -n 15 "$log"
if [[ $rc -eq 0 ]]; then
  echo "hermclaw-deploy: $version deployed (log $log)"
else
  echo "hermclaw-deploy: $version FAILED with exit code $rc (log $log)" >&2
fi
exit "$rc"
