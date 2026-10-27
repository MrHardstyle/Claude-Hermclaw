# Sandbox toolchain images (R-022)

The execution sandbox on `.222` runs every command in a rootless, read-only, **network-less** container. The stock
images (`python:3.12-slim`, …) contain no test tooling and none of a project's dependencies, so a verifier command
such as `pytest -q` would fail. These Containerfiles add the toolchains; project images add the dependencies.

| Alias (`policies.sandbox.images`) | Image | Contents |
|---|---|---|
| `python` | `localhost/hermclaw-sandbox-python:2` | Python 3.12, pytest, pytest-asyncio, pytest-cov, pytest-mock, pytest-timeout, hypothesis, ruff, mypy |
| `node` | `localhost/hermclaw-sandbox-node:2` | Node 22, npm, TypeScript |
| `php` | `localhost/hermclaw-sandbox-php:2` | PHP 8.3 CLI, Composer |
| `generic` | `localhost/hermclaw-sandbox-generic:2` | bash, bats, make, gcc, jq, shellcheck, yamllint, sqlite3, psql, python3 (also as `python`), unzip, xz |
| `powershell` | `localhost/hermclaw-sandbox-powershell:2` | PowerShell 7 LTS (`pwsh`), Pester 5, PSScriptAnalyzer |
| `go` | `localhost/hermclaw-sandbox-go:2` | Go (stable), gofmt; caches in `/tmp`, `GOPROXY=off` |
| `cpp` | `localhost/hermclaw-sandbox-cpp:2` | gcc/g++, make, CMake, Ninja, GoogleTest/GoogleMock, clang-format |
| `java` | `localhost/hermclaw-sandbox-java:2` | Temurin JDK 21, JUnit 5 console launcher (`$JUNIT_JAR`), `junit-run` (compile all .java into `/tmp`, run all tests) |
| `dotnet` | `localhost/hermclaw-sandbox-dotnet:2` | .NET 8 SDK for C# and VB.NET; xUnit/NUnit/MSTest template packages baked in (`/opt/nuget/versions.txt`), restore only from there |
| `lua` | `localhost/hermclaw-sandbox-lua:2` | Lua 5.4, LuaRocks, busted, luacheck |
| `rust` | `localhost/hermclaw-sandbox-rust:2` | Rust stable, cargo, clippy, rustfmt (`CARGO_HOME`/`CARGO_TARGET_DIR` in `/tmp`; crates need a project image or egress) |

Since image 2 (2026-10-27) every image also has the shell basics `git`, `rg` (ripgrep), `jq`, `file` and `ps`/`pgrep`
(procps). Runtime libraries (`requests`, …) stay out on purpose: a project's dependencies come from its project image,
so code that needs an undeclared library still fails in the sandbox instead of only on the user's machine.

Build on `.222` as the worker user (rootless podman):

```bash
sudo -u hermclaw /opt/hermclaw/bin/hermclaw-build-sandbox-images             # all toolchain images
sudo -u hermclaw /opt/hermclaw/bin/hermclaw-build-sandbox-images \
     --project myapp --base python --requirements /srv/myapp/requirements.txt     # project image with deps
```

Then on **both** `.225` (`/etc/hermclaw/policies.yaml`) and `.222` (worker `WORKER_POLICIES_FILE`):

```yaml
sandbox:
  image: localhost/hermclaw-sandbox-generic:1
  images:
    python: localhost/hermclaw-sandbox-python:1
    node: localhost/hermclaw-sandbox-node:1
    php: localhost/hermclaw-sandbox-php:1
    generic: localhost/hermclaw-sandbox-generic:1
    myapp: localhost/hermclaw-sbx-myapp:1      # project image
```

and bind the project image to the repository: `hermclaw repo-add myapp <url> --update --sandbox-image myapp`.
Without a repository image the runtime picks the toolchain by the repository's primary language
(`hermclaw/runtime/images.py`).

Alternative without project images (D-014): with `policies.sandbox.egress.enabled` the coder installs the
manifest's dependencies itself (`install_dependencies`, registry-only proxy, packages in `<data>/deps/<workspace>`).
The generic image also runs that proxy (`worker/execution/egress_proxy.py`, needs `python3`).
