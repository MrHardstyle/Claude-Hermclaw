# 20261016-031 – Dependency installs through a registry-only egress proxy (D-014)

## Question
How can the coder install a project's third-party dependencies (Python, Node) without giving the sandbox general
network access, with rootless Podman on the execution worker (`.222`), and so that tests and the verifier can use
the packages afterwards?

## Why needed
The first test job on an empty repository (`hermclaw-test`, jobs `eadf1b6f…`, `c04f9fb8…`) planned a FastAPI service;
the network-less, read-only sandbox (Bauplan §28, R-022) made `pip install` fail and the step stalled. The operator
decided on 2026-10-16 to allow installs with network (D-014), restricted to the package registries.

## Sources (accessed 2026-10-16, primary)
- Podman `network create` (`--internal`, `--disable-dns`, `--subnet`): https://docs.podman.io/en/latest/markdown/podman-network-create.1.html
  and the man page of the installed podman 5.4.2 on `.222` (`man podman-network-create`, `man podman-run`)
- pip user guide, “Using a Proxy Server” (`--proxy` or the `http_proxy`/`https_proxy` environment variables):
  https://pip.pypa.io/en/stable/user_guide/ ; `pip install --target`: https://pip.pypa.io/en/stable/cli/pip_install/
- npm config, `https-proxy` (“If the HTTPS_PROXY or https_proxy or HTTP_PROXY or http_proxy environment variables are
  set, proxy settings will be honored”) and `ignore-scripts`: https://docs.npmjs.com/cli/v10/using-npm/config/
- Experiments on `.222` (podman 5.4.2, netavark 1.14.0, passt 2025-05-03, rootless as `hermclaw`), 2026-10-16

## Relevant facts
- `podman network create --internal` gives containers on the network no route outside (verified: a direct TCP connect
  to 1.1.1.1:443 fails with `Network is unreachable`). A container attached to an internal *and* a normal bridge
  network reaches the outside through the normal one – that container is the proxy.
- `.222` has **no aardvark-dns**: bridge networks get no DNS (podman warns “aardvark-dns binary not found”), and
  `resolv.conf` points at the network gateway, which does not answer. Fix: create both networks with `--disable-dns`
  and give the proxy container explicit `--dns` servers (the host's non-loopback resolvers); the install sandbox
  needs no DNS at all because it talks to the proxy by a fixed IP (`--network <internal>:ip=…` on the proxy).
- pip and npm send HTTPS through an HTTP proxy with `CONNECT host:443` when `HTTPS_PROXY` is set; no TLS
  interception is needed, the proxy filters on the CONNECT host name. pip installed `fastapi` + `httpx` and npm
  installed `is-number` through the prototype proxy; `https://example.com` got `403` from the proxy.
- Allow-listed names must still resolve to public addresses (the proxy refuses private/loopback results), otherwise a
  manipulated resolver could point an allowed name into the LAN.
- Podman copies the image's `/tmp` into the `--tmpfs=/tmp` mount (“tmpcopyup”): the node toolchain image contains a
  root-owned `/tmp/.npm`, so npm needs `--cache /tmp/npm-cache` (EACCES otherwise).
- `pip install --target DIR` writes plain files (no symlinks, unlike a venv's `bin/python` symlink, which the workspace
  sync refuses); `PYTHONPATH=DIR` makes them importable. Node resolves `node_modules` relative to the project, so the
  dependency directory is mounted at `/workspace/node_modules`; the workspace on disk keeps only an empty mount point
  (git and the sync ignore empty directories).
- `npm install --ignore-scripts --no-save --package-lock=false` runs no install scripts and changes no workspace file.
  pip may build an sdist (`setup.py` runs) – inside the sandbox, with registry-only egress.

## Decision for Hermclaw
D-014: a runtime tool `install_dependencies(ecosystem, manifest)`; the runtime builds the command. The worker runs it
with `CommandRequest.egress` on the internal network `hermclaw-egress` (proxy `10.89.240.2:3128`, allow-list
`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`) and a writable per-workspace `/deps`; every other command
gets `/deps` read-only and no network. Off by default (`policies.sandbox.egress.enabled`), enabled by the inventory
switch `hermclaw_config_dependency_installs`. Risks: R-043.

## Verification
`tests/unit/test_egress.py` (proxy decisions, gateway lifecycle, command line), `tests/integration/test_tools_engine_exec.py`
(tool → request), live `tests/integration/test_egress_live.py` on `.222` (pip and npm through the proxy, `example.com`
and direct connections blocked, `/deps` read-only, nothing in the workspace).
