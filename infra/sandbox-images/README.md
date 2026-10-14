# Sandbox toolchain images (R-022)

The execution sandbox on `.222` runs every command in a rootless, read-only, **network-less** container. The stock
images (`python:3.12-slim`, …) contain no test tooling and none of a project's dependencies, so a verifier command
such as `pytest -q` would fail. These Containerfiles add the toolchains; project images add the dependencies.

| Alias (`policies.sandbox.images`) | Image | Contents |
|---|---|---|
| `python` | `localhost/hermclaw-sandbox-python:1` | Python 3.12, pytest, pytest-asyncio, pytest-cov, ruff, mypy |
| `node` | `localhost/hermclaw-sandbox-node:1` | Node 22, npm, TypeScript |
| `php` | `localhost/hermclaw-sandbox-php:1` | PHP 8.3 CLI, Composer |
| `generic` | `localhost/hermclaw-sandbox-generic:1` | bash, make, gcc, jq, shellcheck, yamllint, sqlite3, psql, python3 |

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
