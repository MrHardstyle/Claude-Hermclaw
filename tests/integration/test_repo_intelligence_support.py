"""Shared helpers for the repository-intelligence tests (P11).

* :func:`build_fixture_repo` – a small multi-language git repository (FastAPI app, Express app, PHP app, SQL/alembic
  migrations, Dockerfile/compose, GitLab + GitHub CI, configs, a secret file, a binary asset);
* :class:`HashingEmbedder` – deterministic fake :class:`~hermclaw.models.protocols.EmbeddingModel` (hashed
  bag-of-words, 768 dimensions) used against the real pgvector store;
* git helpers (real git CLI).

Imported by the other ``test_repo_intelligence_*`` modules; the tests here smoke-test the helpers themselves.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.models.protocols import CallContext

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

FIXTURE_FILES: dict[str, str] = {
    "README.md": "# Billing Platform\n\nA small demo platform with an API, a web frontend and a PHP portal.\n\napi_key = sk-live-should-not-leak\n",
    ".gitignore": "*.log\nbuild/\n.env\n",
    "pyproject.toml": (
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n\n"
        "[project]\nname = 'billing'\nversion = '0.1.0'\ndependencies = ['fastapi', 'sqlalchemy']\n\n"
        "[project.optional-dependencies]\ndev = ['pytest']\n\n"
        "[project.scripts]\nbilling-cli = 'app.cli:main'\n\n"
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
    ),
    "uv.lock": "version = 1\n",
    "app/__init__.py": "",
    "app/__main__.py": "from app.cli import main\n\nmain()\n",
    "app/cli.py": "import sys\n\n\ndef main() -> None:\n    print(sys.argv)\n\n\nif __name__ == '__main__':\n    main()\n",
    "app/main.py": (
        "from fastapi import APIRouter, FastAPI\n\n"
        "from app.services.billing import calculate_invoice_total\n\n"
        "app = FastAPI()\n"
        "router = APIRouter(prefix='/invoices')\n\n\n"
        "@app.get('/health')\n"
        "async def health() -> dict:\n"
        "    return {'ok': True}\n\n\n"
        "@router.post('/{invoice_id}/total')\n"
        "async def invoice_total(invoice_id: int) -> dict:\n"
        "    return {'total': calculate_invoice_total([1, 2, 3])}\n\n\n"
        "app.include_router(router)\n"
    ),
    "app/models.py": (
        "from sqlalchemy.orm import DeclarativeBase\n\n\n"
        "class Base(DeclarativeBase):\n    pass\n\n\n"
        "class Invoice(Base):\n    __tablename__ = 'invoices'\n"
    ),
    "app/services/__init__.py": "",
    "app/services/billing.py": (
        '"""Invoice arithmetic."""\n\n'
        "TAX_RATE = 0.19\n\n\n"
        "def calculate_invoice_total(amounts: list[int]) -> float:\n"
        "    subtotal = sum(amounts)\n"
        "    return round(subtotal * (1 + TAX_RATE), 2)\n\n\n"
        "class InvoiceFormatter:\n"
        "    def render(self, total: float) -> str:\n"
        "        return format_money(total)\n\n\n"
        "def format_money(value: float) -> str:\n"
        "    return f'{value:.2f} EUR'\n"
    ),
    "tests/__init__.py": "",
    "tests/test_billing.py": (
        "import pytest\n\nfrom app.services.billing import calculate_invoice_total\n\n\n"
        "def test_total() -> None:\n    assert calculate_invoice_total([100]) == pytest.approx(119.0)\n"
    ),
    "web/package.json": (
        '{\n  "name": "web",\n  "main": "server.js",\n  "bin": {"web-admin": "bin/admin.js"},\n'
        '  "scripts": {"start": "node server.js", "test": "jest"},\n'
        '  "dependencies": {"express": "^4.19.0"},\n  "devDependencies": {"jest": "^29.0.0"}\n}\n'
    ),
    "web/package-lock.json": '{"lockfileVersion": 3}\n',
    "web/server.js": (
        "const express = require('express');\n"
        "const { slugify } = require('./lib/util');\n\n"
        "const app = express();\n"
        "const router = express.Router();\n\n"
        "app.get('/api/items', (req, res) => res.json([]));\n"
        "router.post('/orders', function createOrder(req, res) {\n"
        "  res.json({ slug: slugify(req.body.name) });\n"
        "});\n"
        "app.use('/api', router);\n\n"
        "function startServer(port) {\n  return app.listen(port);\n}\n\n"
        "module.exports = { app, startServer };\n"
    ),
    "web/lib/util.js": "function slugify(text) {\n  return text.toLowerCase().replace(/\\s+/g, '-');\n}\n\nmodule.exports = { slugify };\n",
    "web/src/cart.ts": (
        "import { slugify } from '../lib/util';\n\n"
        "export interface CartItem {\n  sku: string;\n  qty: number;\n}\n\n"
        "export class ShoppingCart {\n"
        "  private items: CartItem[] = [];\n\n"
        "  addItem(item: CartItem): void {\n    this.items.push(item);\n  }\n\n"
        "  checkoutLabel(): string {\n    return slugify('checkout ' + this.items.length);\n  }\n"
        "}\n\n"
        "export const cartTotal = (items: CartItem[]): number => items.reduce((a, i) => a + i.qty, 0);\n"
    ),
    "web/__tests__/server.test.js": (
        "const { startServer } = require('../server');\n\ntest('starts', () => {\n  expect(typeof startServer).toBe('function');\n});\n"
    ),
    "php/composer.json": (
        '{\n  "name": "acme/portal",\n  "autoload": {"psr-4": {"App\\\\": "src/"}},\n'
        '  "require-dev": {"phpunit/phpunit": "^11.0"},\n  "scripts": {"test": "phpunit"}\n}\n'
    ),
    "php/phpunit.xml": '<phpunit bootstrap="vendor/autoload.php"></phpunit>\n',
    "php/public/index.php": (
        "<?php\nrequire_once __DIR__ . '/../src/helpers.php';\n\n"
        "if ($_SERVER['REQUEST_METHOD'] === 'POST') {\n"
        "    $name = $_POST['name'];\n    save_profile($name);\n}\n"
        "$id = $_GET['id'];\necho render_profile($id);\n"
    ),
    "php/src/helpers.php": (
        "<?php\n\nfunction render_profile($id) {\n    return htmlspecialchars((string) $id);\n}\n\n"
        "function save_profile($name) {\n    file_put_contents('/tmp/profile', $name);\n}\n"
    ),
    "php/routes/web.php": (
        "<?php\nuse App\\Controller\\ProfileController;\nuse Illuminate\\Support\\Facades\\Route;\n\n"
        "Route::get('/profile', [ProfileController::class, 'show']);\n"
        "Route::post('/profile/avatar', [ProfileController::class, 'upload']);\n"
    ),
    "php/src/Controller/ProfileController.php": (
        "<?php\nnamespace App\\Controller;\n\nclass ProfileController\n{\n"
        "    public function show(int $id): string\n    {\n        return render_profile($id);\n    }\n\n"
        "    public function upload(): void\n    {\n    }\n}\n"
    ),
    "php/tests/ProfileControllerTest.php": (
        "<?php\nuse App\\Controller\\ProfileController;\n\nclass ProfileControllerTest\n{\n"
        "    public function testShow(): void\n    {\n        (new ProfileController())->show(1);\n    }\n}\n"
    ),
    "migrations/001_init.sql": (
        "CREATE TABLE IF NOT EXISTS customers (\n  id serial PRIMARY KEY,\n  name text NOT NULL\n);\n\n"
        "CREATE TABLE payments (\n  id serial PRIMARY KEY,\n  customer_id int REFERENCES customers(id)\n);\n"
    ),
    "alembic.ini": "[alembic]\nscript_location = alembic\n",
    "alembic/versions/a1b2c3_create_users.py": (
        '"""create users"""\nfrom alembic import op\nimport sqlalchemy as sa\n\n'
        "revision = 'a1b2c3'\ndown_revision = None\n\n\n"
        "def upgrade() -> None:\n    op.create_table('users', sa.Column('id', sa.Integer, primary_key=True))\n\n\n"
        "def downgrade() -> None:\n    op.drop_table('users')\n"
    ),
    "Dockerfile": (
        'FROM python:3.12-slim AS base\nWORKDIR /srv\nCOPY . .\nFROM base AS runtime\nEXPOSE 8000\nCMD ["uvicorn", "app.main:app"]\n'
    ),
    "docker-compose.yml": "services:\n  api:\n    build: .\n  db:\n    image: postgres:16\n",
    ".gitlab-ci.yml": "stages:\n  - test\n  - deploy\n\nvariables:\n  X: '1'\n\nunit:\n  stage: test\n  script: pytest\n\n.hidden:\n  script: echo\n",
    ".github/workflows/ci.yml": "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n",
    "Makefile": "test:\n\tpytest -q\n\nlint:\n\truff check .\n",
    "config/settings.yaml": "log_level: info\n",
    ".env.example": "DATABASE_URL=postgresql://user@localhost/db\n",
    "nginx/site.conf": "server { listen 80; location / { proxy_pass http://api:8000; } }\n",
    "bin/run-report": "#!/usr/bin/env python3\nprint('report')\n",
    "deploy/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\nc2VjcmV0LWtleS1tYXRlcmlhbA==\n-----END OPENSSH PRIVATE KEY-----\n",
}
BINARY_FILES: dict[str, bytes] = {"assets/logo.png": b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + bytes(range(64))}


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], check=True, env=GIT_ENV, capture_output=True, text=True)
    return res.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def build_fixture_repo(path: Path, *, git_init: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    write_files(path, FIXTURE_FILES)
    for rel, data in BINARY_FILES.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (path / "bin/run-report").chmod(0o755)
    (path / "debug.log").write_text("ignored by .gitignore\n", encoding="utf-8")
    (path / "build").mkdir(exist_ok=True)
    (path / "build/out.py").write_text("IGNORED_BUILD_OUTPUT = 1\n", encoding="utf-8")
    if git_init:
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=GIT_ENV)
        commit_all(path, "fixture")
    return path


def workspace_handle(root: Path, key: str | None = None) -> WorkspaceHandle:
    return WorkspaceHandle(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        path=root,
        branch="main",
        base_branch="main",
        base_sha="0" * 40,
        repository_key=key or f"fixture-{uuid.uuid4().hex[:10]}",
    )


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def bag_of_words(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        for part in tok.split("_"):
            out.extend(m.group(0).lower() for m in _CAMEL.finditer(part))
    return [t for t in out if len(t) >= 3]


@dataclass
class HashingEmbedder:
    """Deterministic fake embedding model: L2-normalised hashed bag-of-words vectors (768 dims)."""

    dimensions: int = 768
    model_name: str = "fake-hashing-embedder"
    calls: list[int] = field(default_factory=list)  # batch sizes
    texts: list[str] = field(default_factory=list)
    fail_with: Exception | None = None
    contexts: list[CallContext] = field(default_factory=list)

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for tok in bag_of_words(text):
            h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
            vec[h % self.dimensions] += 1.0
        if not any(vec):
            vec[0] = 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec]

    async def embed(self, texts: list[str], *, ctx: CallContext) -> list[list[float]]:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(len(texts))
        self.texts.extend(texts)
        self.contexts.append(ctx)
        return [self.vector(t) for t in texts]


# ============================================================================================= helper smoke tests
def test_fixture_repo_is_committed(tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    tracked = set(git(repo, "ls-files").splitlines())
    assert "app/main.py" in tracked
    assert "debug.log" not in tracked and "build/out.py" not in tracked
    assert git(repo, "status", "--porcelain") == ""


async def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    emb = HashingEmbedder()
    [a, b] = await emb.embed(["invoice total", "invoice total"], ctx=CallContext(purpose="embedding"))
    assert a == b and len(a) == 768
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    [c] = await emb.embed(["express router"], ctx=CallContext(purpose="embedding"))
    assert sum(x * y for x, y in zip(a, c, strict=True)) < 0.5


pytestmark = pytest.mark.integration
