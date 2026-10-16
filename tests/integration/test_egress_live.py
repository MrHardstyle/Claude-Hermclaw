"""D-014 live on ``.222``: dependency installs through the allow-listing egress proxy (rootless podman).

Uses its own networks, subnets and proxy container (``*-test``), so the production proxy is never touched, and
removes them afterwards. Needs the toolchain images ``hermclaw-sandbox-python:1`` and ``hermclaw-sandbox-generic:1``
and outbound HTTPS from the worker to pypi.org / files.pythonhosted.org. Run by the acceptance suite ``sandbox``."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import EgressPolicy, SandboxPolicy
from hermclaw.tools.engine import python_install_command
from worker.execution.egress import host_nameservers
from worker.execution.sandbox import PodmanSandbox

PYTHON = "localhost/hermclaw-sandbox-python:1"
NODE = "localhost/hermclaw-sandbox-node:1"
GENERIC = "localhost/hermclaw-sandbox-generic:1"


def _ready() -> str | None:
    if shutil.which("podman") is None:
        return "podman not installed"
    for image in (PYTHON, NODE, GENERIC):
        if subprocess.run(["podman", "image", "exists", image], capture_output=True, timeout=60, check=False).returncode != 0:
            return f"{image} not built (scripts/ops/hermclaw-build-sandbox-images)"
    if not host_nameservers():
        return "no non-loopback nameserver in /etc/resolv.conf"
    return None


_SKIP = _ready()
pytestmark = [pytest.mark.live, pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")]

EGRESS = EgressPolicy(
    enabled=True,
    internal_network="hermclaw-egress-test",
    outbound_network="hermclaw-egress-test-out",
    internal_subnet="10.89.248.0/24",
    outbound_subnet="10.89.249.0/24",
    proxy_ip="10.89.248.2",
    proxy_container="hermclaw-egress-proxy-test",
    proxy_image=GENERIC,
    allowed_hosts=["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"],
)


def _req(command: str, **kw: object) -> CommandRequest:
    return CommandRequest(
        request_id=f"egress-{abs(hash(command)) % 10**8}", job_id="j", step_id="s", workspace="ws1", command=command, **kw
    )  # type: ignore[arg-type]


@pytest.fixture
def box(tmp_path: Path):  # type: ignore[no-untyped-def]
    sandbox = PodmanSandbox(
        SandboxPolicy(engine="podman", image=PYTHON, images={"node": NODE}, egress=EGRESS, default_timeout_seconds=300),
        deps_root=tmp_path / "deps",
        managed_label="hermclaw.egresstest=true",
        pull="never",
        selinux_relabel=False,
    )
    yield sandbox
    subprocess.run(["podman", "rm", "-f", "-t", "0", EGRESS.proxy_container], capture_output=True, check=False)
    subprocess.run(["podman", "network", "rm", EGRESS.internal_network, EGRESS.outbound_network], capture_output=True, check=False)


async def test_live_install_through_the_proxy_and_nothing_else(box: PodmanSandbox, tmp_path: Path) -> None:
    ws = tmp_path / "workspaces" / "ws1"
    ws.mkdir(parents=True)
    (ws / "requirements.txt").write_text("idna==3.10\n", encoding="utf-8")
    install = await box.run(
        _req(python_install_command("requirements.txt"), egress=True, timeout_seconds=600),
        ws,
    )
    assert install.exit_code == 0, (install.error, install.stderr[-2000:])
    assert "idna==3.10" in install.stdout
    # later commands: offline, the dependency importable from the read-only /deps
    use = await box.run(_req("python3 -c 'import idna; print(idna.encode(\"bücher.de\").decode())' && touch /deps/x"), ws)
    assert "xn--bcher-kva.de" in use.stdout and "Read-only file system" in use.stderr
    # an install request reaches nothing but the allow-listed registries
    probe = (
        "python3 - <<'PY'\n"
        "import socket, urllib.request\n"
        "try:\n    urllib.request.urlopen('https://example.com', timeout=8); print('EXAMPLE OPEN')\n"
        "except Exception as exc:\n    print('example blocked', exc)\n"
        "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=5); print('DIRECT OPEN')\n"
        "except OSError as exc:\n    print('direct blocked', exc)\n"
        "PY"
    )
    leak = await box.run(_req(probe, egress=True), ws)
    assert "example blocked" in leak.stdout and "403" in leak.stdout, leak.stdout
    assert "direct blocked" in leak.stdout and "OPEN" not in leak.stdout
    decisions = await box.egress.decisions()
    assert any(d.get("decision") == "blocked" and str(d.get("target", "")).startswith("example.com") for d in decisions)
    assert not list(ws.parent.parent.joinpath("workspaces", "ws1").glob("**/idna"))  # nothing landed in the workspace


async def test_live_npm_install_lands_in_the_mounted_node_modules(box: PodmanSandbox, tmp_path: Path) -> None:
    """npm resolves modules relative to the project: the workspace's node_modules is the mounted dependency dir,
    so the packages work in later commands, are not part of the workspace (sync, git) and install scripts never run."""
    ws = tmp_path / "workspaces" / "ws1"
    ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"name": "egress-live", "version": "1.0.0", "dependencies": {"is-number": "7.0.0"}}\n')
    install = await box.run(
        _req(
            "npm install --ignore-scripts --no-audit --no-fund --no-save --package-lock=false --cache /tmp/npm-cache && npm ls --depth=0",
            image="node",
            egress=True,
            timeout_seconds=600,
        ),
        ws,
    )
    assert install.exit_code == 0, (install.error, install.stderr[-2000:])
    assert "is-number@7.0.0" in install.stdout
    use = await box.run(_req("node -e \"console.log(require('is-number')(5))\"", image="node"), ws)
    assert use.stdout.strip() == "true", (use.stdout, use.stderr[-1000:])
    assert not (ws / "package-lock.json").exists()
    leftovers = [p for p in (ws / "node_modules").rglob("*")] if (ws / "node_modules").exists() else []
    assert leftovers == []  # only the (empty) mount point exists in the workspace itself


async def test_live_install_keeps_the_image_test_toolchain(box: PodmanSandbox, tmp_path: Path) -> None:
    """BUG-161 (live job bec4c5e9): a requirements pin of an older pytest landed in /deps/python, which comes first on
    PYTHONPATH, and broke the image's pytest plugins. The image's packages are constraints: the conflicting pin fails
    with pip's resolution error, nothing of it stays in /deps, and after relaxing it the image's pytest still runs."""
    ws = tmp_path / "workspaces" / "ws1"
    ws.mkdir(parents=True)
    (ws / "requirements.txt").write_text("idna==3.10\npytest==7.4.3\n", encoding="utf-8")
    pinned = await box.run(_req(python_install_command("requirements.txt"), egress=True, timeout_seconds=600), ws)
    assert pinned.exit_code == 1 and "ResolutionImpossible" in pinned.stderr, (pinned.error, pinned.stderr[-2000:])
    (ws / "requirements.txt").write_text("idna==3.10\n", encoding="utf-8")
    install = await box.run(_req(python_install_command("requirements.txt"), egress=True, timeout_seconds=600), ws)
    assert install.exit_code == 0 and "idna==3.10" in install.stdout and "pytest" not in install.stdout, install.stderr[-2000:]
    (ws / "test_idna.py").write_text("import idna\n\n\ndef test_idna():\n    assert idna.encode('bücher.de') == b'xn--bcher-kva.de'\n")
    tests = await box.run(_req("python3 -m pytest -q -p no:cacheprovider"), ws)
    assert tests.exit_code == 0 and "1 passed" in tests.stdout, tests.stdout[-2000:]


async def test_live_project_package_is_importable_with_plain_pytest(box: PodmanSandbox, tmp_path: Path) -> None:
    """BUG-167 (live job 0a98a5eb): `pytest app/test_main.py` failed with "No module named 'app'" because plain
    pytest does not put the working directory on sys.path. The repository root is on PYTHONPATH in the container,
    with and without a dependency directory."""
    ws = tmp_path / "workspaces" / "ws1"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "main.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (ws / "app" / "test_main.py").write_text("from app.main import answer\n\n\ndef test_answer():\n    assert answer() == 42\n")
    plain = await box.run(_req("pytest -q -p no:cacheprovider app/test_main.py"), ws)
    assert plain.exit_code == 0 and "1 passed" in plain.stdout, plain.stdout[-2000:] + plain.stderr[-1000:]
    (ws / "requirements.txt").write_text("idna==3.10\n", encoding="utf-8")
    install = await box.run(_req(python_install_command("requirements.txt"), egress=True, timeout_seconds=600), ws)
    assert install.exit_code == 0, install.stderr[-2000:]
    with_deps = await box.run(_req("cd app && pytest -q -p no:cacheprovider test_main.py && python3 -c 'import idna'"), ws)
    assert with_deps.exit_code == 0 and "1 passed" in with_deps.stdout, with_deps.stdout[-2000:] + with_deps.stderr[-1000:]
