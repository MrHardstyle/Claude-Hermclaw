# SSH/admin tools (P26)

`hermclaw/sshtools/` gives `ssh`/`deploy` steps runtime-controlled access to the admin hosts of `hosts.yaml`
(Bauplan §29, phase 26, steps 26.1–26.8; research `docs/research/20261008-021-ssh-policies.md`,
`-020-systemd-credentials.md`). The coder shell has no SSH. Every remote command goes through one gate chain:
capability, host allow-list, command policy, approval, pre-flight, execution and audit.

## 1. Code map

| module | content | step |
|---|---|---|
| `registry.py` | `HostRegistry` (hosts with an `ssh` section **and** listed in `policies.ssh.allowed_hosts`), `SshHost`, `valid_address`, `SAFE_PATH_RE` | 26.1 |
| `keys.py` | `KeyManager` (`key_ref` → key file, private runtime dir, `known_hosts` checks), `check_key_file`, `check_known_hosts_file`, `purge_stale_keys`, `generate_keypair`, `authorized_keys_entry` | 26.2 |
| `policy.py` | `SshCommandPolicy` (`classify` → `read`/`mutate`/`destructive`/`forbidden`, `decide` → `allow`/`approval_required`/`deny`), `scan_shell`, `approval_token` | 26.3 |
| `approvals.py` | `grant_approval`, `revoke_approval`, `approval_state`, `consume_approval` (`jobs.metadata['ssh_approvals']`) | 26.5 |
| `client.py` | `SshRunner` (OpenSSH client, timeout + kill, output caps, redaction; satisfies `hermclaw.wol.idle.RemoteCommandRunner`), `SshResult` | 26.4/26.7 |
| `tools.py` | `SshTools.ssh_read` / `ssh_command` / `ssh_upload` / `ssh_service_status`, `SshCallContext`, `SshToolResult`, `SshOutcome` | 26.4–26.6 |
| `provider.py` | `SshOpsProvider` (tool-engine `OpsToolProvider` adapter), `resolve_workspace_file` (upload sources from the job workspace) | 26.4/26.5 |
| `separation.py` | `assert_ssh_allowed`, `ssh_allowed`, `sandbox_isolation_issues`, `scrub_ssh_environment` | 26.8 |
| `errors.py` | stable error codes (`SSH_*`), `SshPolicyError(PolicyViolation)`, `SshConfigError(ConfigError)`, `SshTransportError`, `SshTimeoutError(TimeoutError)` | – |
| `scripts/ops/hermclaw-ssh-gate` | POSIX `sh` forced command for `authorized_keys` on the target hosts | 26.8 |

## 2. Public interfaces

```python
registry = HostRegistry.from_config(config)          # raises SshConfigError for a broken entry at startup
registry.get(host_id) -> SshHost                     # SshPolicyError SSH_HOST_UNKNOWN / SSH_HOST_NOT_ALLOWED

tools = SshTools.from_config(config, sessionmaker, secret_store=None, runtime_dir=None)  # compound_allowlist defaults to policies.ssh.compound_allowlist
await tools.ssh_read(host, command, ctx=SshCallContext(...), timeout=None) -> SshToolResult
await tools.ssh_command(host, command, approval_token=None, ctx=..., timeout=None) -> SshToolResult
await tools.ssh_upload(host, source: Path | bytes, remote_path, mode="0644", approval_token=None, ctx=...) -> SshToolResult
await tools.ssh_service_status(host, unit, ctx=...) -> SshToolResult    # data: active_state, active, systemd_available

runner = tools.runner                                 # SshRunner
await runner.run(host_id, command, timeout) -> tuple[int, str, str]     # RemoteCommandRunner (WOL idle hook); SshTimeoutError
await runner.execute(host_id, command, timeout=None, stdin=None) -> SshResult

assert_ssh_allowed(step_capability)                   # SshPolicyError SSH_CAPABILITY_DENIED unless 'ssh' | 'deploy'
await grant_approval(session, job_id, token, approved_by="operator")   # operator/API side, caller commits
```

`SshCallContext(job_id, step_id, attempt_id, tool_call_id, capability, actor)` carries the audit identity and the
step capability. `SshToolResult.outcome` is one of `ok`, `failed`, `timed_out`, `transport_error`, `denied` or
`approval_required`. `code` holds the stable error code. `render()` gives bounded text for the model or UI, and
`to_dict()` gives the machine view. The approval token appears there under the key `approval`, because the event
store redacts keys that contain `token`.

## 3. Gate chain (one call)

1. **Capability (26.8).** `assert_ssh_allowed(ctx.capability)`. Only `ssh` and `deploy` steps pass. A call without
   a context has no capability and is denied.
2. **Host (26.1).** `HostRegistry.get`. The host must be configured with `ssh` and listed in
   `policies.ssh.allowed_hosts`. The registry validates entries at startup:
   - address: IP literal or RFC 1123 name, never something ssh could parse as an option;
   - user and port;
   - `key_ref`: `file:<abs path>` or `cred:<name>`. `env:` and `literal:` work only outside production;
   - `known_hosts`: mandatory absolute path, with no whitespace, `%` or `~`, because ssh splits and expands these
     options;
   - `allow_sudo_commands`.
3. **Policy (26.3).** `SshCommandPolicy.decide`, in this order:
   - Malformed input is forbidden: empty, control characters or newlines, unbalanced quotes, more than 8000 characters.
   - Privilege words (`sudo su doas pkexec run0 sudoedit`) are forbidden unless the whole command is exactly
     `sudo [-n] <entry>` for an `allow_sudo_commands` entry. The match is byte-exact. The entry is then classified
     on its own.
   - `policies.commands` patterns decide forbidden, destructive and mutate. The same `CommandClassifier` is used by
     the sandbox tool engine, and it also looks into quoted sub-commands.
   - **read** applies only when every top-level part of the pipeline or list meets all of these conditions:
     - it starts with a *bare tool name*: no `VAR=value` prefix, no path;
     - a read pattern matches the re-joined shell words from the first character and ends on a word boundary, so
       `cat.sh` and `ls-x` are not `cat` or `ls`;
     - nothing writes a file (`>`/`>>`/`&>`/`<>` other than `/dev/null`);
     - there is no command substitution, here-document or background `&`;
     - no flag turns the read tool into an executor, writer or remote hop. Examples: `find -exec/-delete/-fprint*`,
       `rg --pre/--hostname-bin`, `journalctl --vacuum-*/--rotate/--cursor-file`, `ss -K/-D`,
       `nginx -s/-g/-c/-p/-e`, `systemctl -H/-M`, and `ip` except `ip [opts] OBJECT [show|list|lst|ls|get …]`
       without `-batch`. iproute2 accepts abbreviations such as `ip a a` = `address add`.
     - Anything else, including unclassified commands, counts as at least `mutate`. On an admin host nothing is
       read-only by default.
   - Non-read commands that start an onward connection (`ssh scp sftp rsh telnet …`, also behind
     `env`/`nohup`/`timeout`) are forbidden, because they would bypass the host allow-list.
   - Mutate and destructive commands that chain are denied (`SSH_COMPOUND_NOT_ALLOWED`) unless the whole command
     full-matches an entry of the compound allow-list. Chaining means `; && || | &`, sub-shells, `` ` ``, `$(`,
     `<(`, here-documents, `sh -c`/`bash -lc`/`eval`.
   - Destructive commands always need an approval. Mutate commands need one when
     `policies.ssh.require_approval_for_mutations` is set. `ssh_read` denies everything that is not read
     (`SSH_NOT_READ_ONLY`).
4. **Approval (26.5).**
   - The decision `approval_required` carries `approval_token(host, command, step)`, which is
     `ssh-<48 hex>`, the SHA-256 of host + exact command (or upload path/mode/sha256) + step. A token identifies a
     command; it does not authorise one.
   - Only an operator entry in `jobs.metadata['ssh_approvals']` (`grant_approval`) authorises it.
     A client cannot plant the key (or `ssh_approvals_consumed`) when it creates the job: `POST /api/jobs` refuses
     reserved metadata keys with 422 `JOB_METADATA_RESERVED_KEY` ([api.md](api.md) §2, BUG-140).
   - A repeated call that passes the token consumes the approval under `SELECT … FOR UPDATE`, in the same
     transaction that writes the `command_runs` row with `exit_code NULL` ("started"). The approval moves to
     `ssh_approvals_consumed` (history with `approved_by`). Approvals are single use, and concurrent calls with one
     approval run it exactly once.
   - Writes merge only these two keys with `metadata || patch`, so other metadata writers are not overwritten.
5. **Pre-flight (26.2).** The pinned host key must be listed in `known_hosts`. The lookup uses `ssh-keygen -F`, so
   hashed entries work. The identity must be usable. This check runs **before** the approval is consumed. A broken
   key or a missing `known_hosts` file does not use up an approval.
6. **Execution (26.7).** See §4.
7. **Audit (26.6).**
   - The `command_runs` row is completed (`target='ssh'`, `classification`, `exit_code`, `timed_out`, redacted
     stdout/stderr excerpts, `duration_ms`, `worker_id=<host>`, `network=true`).
   - One `ssh.command` event is written in the same transaction. Its payload holds tool, host, outcome,
     classification, redacted command (≤ 500 characters), code, reason, exit code, approval, approver, byte counts
     and upload metadata. It holds no outputs.
   - Refusals (capability, host, policy, approval) write the event only. Nothing ran, so there is no
     `command_runs` row.
   - If the audit write fails after execution, the result is still returned, flagged
     `data['audit_incomplete']=True`. The "started" row remains as evidence.

## 4. Transport

```
ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<known_hosts>
    -o GlobalKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o ConnectTimeout=10
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o PreferredAuthentications=publickey
    -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=yes -o PermitLocalCommand=no -o RequestTTY=no
    -o UpdateHostKeys=no -o CheckHostIP=no -o ControlMaster=no -o ControlPath=none
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o LogLevel=ERROR
    -i <key> -p <port> <user>@<address> -- <command>
```

- `-F /dev/null` ignores user and system `ssh_config`: no `ProxyCommand`, agent or forwarding surprises.
- The environment is minimal (`PATH HOME LANG LC_ALL`). There is no `SSH_AUTH_SOCK`.
- The command is one argv element after `--`.
- At most four sessions run per host.
- ssh runs in its own process group. On timeout or caller cancellation the group gets SIGTERM, then SIGKILL after
  the grace time. Output read before the kill is kept.
- stdout and stderr are read concurrently. Each is capped at `policies.commands.max_output_bytes`; the rest is
  drained and counted. Both are decoded leniently and redacted with the default redactor. Literals of resolved
  secrets, including materialised keys, are registered there.
- Exit status 255 is reported as `transport_error`: connection, authentication or host-key failure, or a session
  dropped by the host.

**Keys.**
- `file:` keys are used in place after a check: regular file, mode 0600/0400, owned by the effective user.
- `cred:` keys (systemd `LoadCredentialEncrypted=`, `$CREDENTIALS_DIRECTORY`) are written for one ssh process to
  `key-<pid>-XXXXXXXX` (`mkstemp`, 0600). The file goes in `<$RUNTIME_DIRECTORY | $XDG_RUNTIME_DIR | tmp>/hermclaw-ssh-<uid>`,
  which is 0700 and refused if it is a symlink, has a foreign owner or has loose permissions. The file is deleted
  afterwards.
- Keys left behind by a process that died (SIGKILL) are purged on the next start.

**Upload.**
- `ssh_upload` streams the bytes on stdin to
  `sh -c '<fixed script>' hermclaw-upload <dir> <path> <sha256> <mode>`. All values are positional parameters.
- The script writes `mktemp <dir>/.hermclaw-upload.XXXXXX` with umask 077, verifies the SHA-256 when `sha256sum`
  exists, runs `chmod <mode>`, then `mv -f` into place. The replacement is atomic, and the target's symlink is not
  followed.
- A failed upload leaves no temp file behind.
- `remote_path` must be absolute, normalised and made only of `[A-Za-z0-9._@+-/]`. `mode` is `0?[0-7]{3}` without
  the world-write bit, so there are no setuid, setgid or sticky bits and no world-writable files (`0666`/`0777` are
  refused).
- An upload is `mutate`. It is `destructive`, so it needs an approval even when `require_approval_for_mutations` is
  off, when it replaces a security-critical file (`critical_remote_path`). That covers any `.ssh/` path or
  `authorized_keys*`, `/etc/sudoers*`, `passwd`/`shadow`/`group`, `/etc/ssh/`, `/etc/pam.d/`, `/etc/security/`,
  cron, `ld.so.preload`, `profile`, `/boot/`, `/root/`, and the gate itself (`/etc/hermclaw/`,
  `/usr/local/bin/hermclaw-ssh-gate`). Results and events carry `critical_path: true`.
- The approval subject includes path, mode and SHA-256. Changed content needs a new approval.
- Through the provider, the source is a workspace file. Traversal, empty or `.` segments, symlink escapes, `.git`
  and `policies.scope.always_forbidden` (`.env`, `*.pem` …) are refused, and file contents never reach the audit.
  After opening, the descriptor's real path (`/proc/self/fd`) must still lie inside the workspace. This closes the
  window in which a directory component could be swapped for a symlink between resolving and opening.

**Service status.** `ssh_service_status` runs two runtime-built read commands:
`systemctl is-active <unit>` and `systemctl status --no-pager --full --lines=40 <unit>`. The unit name is validated.
Exit codes 1–4 of `status` are states, not failures: 3 means inactive and 4 means `not-found`. A host without
systemd reports `SSH_SERVICE_UNAVAILABLE` with `active_state=unavailable` instead of crashing. Examples are
containers or chroots that print "not been booted with systemd" or "Failed to connect to bus", and hosts where
`systemctl` is missing (exit 127).

## 5. Sandbox vs. host separation (26.8)

- SSH tools run in the orchestrator. They are reachable only from `ssh`/`deploy` steps, checked twice: the tool
  engine's `OPS_TOOLS_BY_CAPABILITY` and `assert_ssh_allowed`.
- The execution sandbox never receives SSH material:
  - Keys and `known_hosts` live outside the job workspaces, which are the only host paths mounted into the
    sandbox.
  - Materialised keys exist only for the lifetime of one ssh process, in the orchestrator's private runtime dir.
  - `SSH_AUTH_SOCK`, `SSH_*` and `GIT_SSH*` are never in the sandbox env allow-list.
- `sandbox_isolation_issues(config, workspaces_dir=…)` checks these invariants. It flags:
  - SSH variables in `policies.sandbox.env_allowlist`;
  - key or `known_hosts` paths inside the workspaces dir;
  - `ssh`/`deploy` capabilities mapped to the execution worker.
- `scrub_ssh_environment` strips agent and override variables from any environment near the sandbox.

### Target hosts: `hermclaw-ssh-gate`

The gate is the host-side second line. It is a forced command: the host runs only allow-listed command lines,
even if the runtime is compromised.

```sh
install -o root -g root -m 0755 scripts/ops/hermclaw-ssh-gate /usr/local/bin/hermclaw-ssh-gate
install -o root -g root -m 0644 ssh-allowlist /etc/hermclaw/ssh-allowlist
# ~hermclaw-admin/.ssh/authorized_keys (authorized_keys_entry() builds this line):
restrict,from="192.168.178.225",command="/usr/local/bin/hermclaw-ssh-gate" ssh-ed25519 AAAA… hermclaw-admin
```

- The gate reads `SSH_ORIGINAL_COMMAND`. It rejects an empty command (interactive login), multi-line input and
  control characters.
- It refuses an allow-list that is missing, is a symlink, has a foreign owner, or is group/world-writable.
- The allow-list has one ERE per line, matched against the whole line (`grep -Ex`). Lines starting with `#` are
  comments.
- Commands containing `; & | ` `` ` `` ` $ ( ) < > \` (chaining, substitution, redirection) or the expansion
  characters `{ } * ? [ ] ~` match only rules written as `shell:<regex>`. A plain rule such as
  `systemctl restart [^ ]+` therefore cannot be widened by brace expansion (`{nginx,ssh}` where `/bin/sh` is bash),
  globs or `~user` into more or other arguments than its author saw. For example, the upload script needs
  `shell:sh -c .* hermclaw-upload /srv/app/.*`.
- Allowed commands run via `exec /bin/sh -c` and keep their exit status. stdin stays the SSH channel, because the
  allow-list is read on fd 4.
- Rejected commands exit 126.
- Every decision goes to syslog (`auth`, tag `hermclaw-ssh-gate`).
- Tested with dash, bash and busybox sh.

Key provisioning: `await generate_keypair(Path("/etc/hermclaw/ssh/id_ed25519"))` (ed25519, 0600, refuses to
overwrite). Pin host keys with `ssh-keyscan -p <port> <host>`, compare the fingerprint out of band, then append the
entry to `/etc/hermclaw/ssh/known_hosts`, which must not be group or world writable.

## 6. Tests

All tests run against a **real OpenSSH server**: `sshd -D` on a free 127.0.0.1 port with a temp host key, temp
`authorized_keys` (one plain key, one forced-command key) and `PasswordAuthentication no`.

| file | covers |
|---|---|
| `tests/unit/test_sshtools_policy.py` | classification matrix, sudo exactness, chaining, read bypass regressions (`VAR=` prefix, `cat.sh`, `ip a a`, `ss -D`, `systemctl -H`, `nginx -c`, …), tokens |
| `tests/unit/test_sshtools_registry_keys.py` | registry validation (option-like addresses, unsafe paths), key/known_hosts permission checks, hashed known_hosts lookup, cred materialisation, stale key purge, keypair + `authorized_keys` line, separation checks, critical remote paths, upload source containment after open |
| `tests/unit/test_sshtools_gate.py` | gate with `SSH_ORIGINAL_COMMAND` (dash/bash/busybox): whole-line rules, `shell:` rules for metacharacters and expansion characters, stdin passthrough, untrusted allow-list, syslog |
| `tests/integration/test_sshtools_sshd.py` | argv hardening, timeout kill, cancellation, output cap + redaction, stdin, host-key pinning (MITM refusal), cred keys, read/mutate/approval single-use/concurrency, sudo/compound allow-lists, capability gate, audit rows + events, upload (atomic, mode, no world-writable modes, checksum, approvals bound to content, critical paths always need approval), service status incl. no-systemd, gate over real ssh |
| `tests/integration/test_sshtools_provider.py` | `SshOpsProvider` dispatch, workspace upload source rules |
| `tests/failure/test_sshtools_failures.py` | host down, unroutable host (ConnectTimeout), missing ssh client, pre-flight failures keep approvals, session dropped by the host, audit failure flagged |
| `tests/integration/test_sshtools_live.py` | `@pytest.mark.live` checks of the real hosts (BLOCKER-001) |

Test-only system packages: `openssh-server`/`openssh-client` 9.6p1 (present in the build container), `shellcheck` 0.9.0 (installed with `apt-get install shellcheck` for the gate lint test; skipped when absent) and `busybox` 1.36.1 (installed with `apt-get install busybox` so the gate tests also run under busybox sh; that shell is skipped when absent).

## 7. Residual risks / limits

- Classification is static. Commands on the host are subject to the remote user's own shell start-up files and
  glob expansion: a file named `-delete` in the cwd of `find *` is not modelled. The gate allow-list and the
  restricted admin user bound this risk.
- Mutating commands that open network connections without an ssh-like tool (`rsync -e ssh`, `nc`, `curl`) are not
  forbidden. They are `mutate` and need an approval when `require_approval_for_mutations` is set, which is the
  default.
- `read` commands run with the admin user's permissions, so whatever that user can read, the model can see. Redaction
  only catches known secret shapes and registered literals. Keep the admin user unprivileged and grant `sudo`
  only for exact `allow_sudo_commands` entries.
- The gate logs the requested command (first 400 characters) to the host's auth syslog unredacted. Do not pass
  secrets on remote command lines; uploads stream their content on stdin.
- A remote process that ignores the closed connection may outlive a killed ssh client. The gate and `timeout` on
  the host bound that side.
- An approval consumed by a run that then fails at transport level is not restored. The operator re-approves, and
  the consumed history shows what happened.
