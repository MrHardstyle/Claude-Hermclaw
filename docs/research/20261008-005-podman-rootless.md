# 20261008-005 – rootless Podman als Execution Sandbox

## Question
Welche Podman-Optionen garantieren Isolation (Netz aus, read-only, Limits) für Step-Container auf `.222`?

## Why needed
P18 Execution Sandbox, P36 Security.

## Sources
- podman-run(1) (v4.9.3 / latest): https://docs.podman.io/en/latest/markdown/podman-run.1.html
- Red Hat: Rootless Podman user namespace modes: https://www.redhat.com/en/blog/rootless-podman-user-namespace-modes
- podman-py Container-Manager (network modes): https://podman-py.readthedocs.io/en/latest/podman.domain.containers_manager.html
- Debian trixie Podman 5.4.2: https://packages.debian.org/src:podman

## Source date/version
Podman 4.9.3 (Build-Umgebung), 5.4.2 (trixie). Abruf 2026-10-08.

## Relevant facts
- `--network=none` → keine Netzwerkschnittstelle außer Loopback.
- `--read-only` + `--tmpfs /tmp:size=…` → unveränderliches Root-FS.
- `--userns=keep-id` mappt die Host-UID in den Container → Bind-Mounts bleiben beschreibbar ohne Root.
- Limits: `--memory`, `--memory-swap`, `--cpus`, `--pids-limit`; erfordern cgroups v2 mit delegierten Controllern (systemd).
- `--label` erlaubt Wiederfinden verwaister Container; `--rm`, `--timeout`/`podman stop -t`.
- `--cap-drop=ALL`, `--security-opt no-new-privileges`.

## Compatibility with our hardware
`.222` CPU-Worker; cgroups v2 ist Debian-Standard.

## Compatibility with our versions
Alle Optionen existieren in Podman 4.9 und 5.4.

## Rejected alternatives
Docker als Standard – laut Architektur nur „unterstützter Adapter“.

## Decision fixed by architecture
rootless Podman, Netzwerk standardmäßig aus.

## Implementation consequences
- `PodmanSandbox` baut Kommando: `podman run --rm --label hermclaw.step=<id> --network=none --read-only --tmpfs /tmp --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges --pids-limit N --memory M --memory-swap M --cpus C -v <workspace>:/workspace:Z -w /workspace --env <allowlist> <image> sh -lc <cmd>`.
- Timeout über asyncio + `podman kill`; Recovery: `podman ps -a --filter label=hermclaw.step` → Aufräumen.
- Netzwerk nur, wenn Step-Capability `network` gewährt ist (`--network=slirp4netns` bzw. `pasta`).

## Open risks
In Containern ohne systemd-Delegation greifen Limits nicht → Health-Check prüft `podman info` (cgroupVersion, cgroupControllers).
