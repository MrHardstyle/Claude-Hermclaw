"""P11 11.1-11.3: deterministic inventory, language detection, build/test discovery, routes (real git CLI, files on disk)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermclaw.repo_intelligence import RepoIntelConfig, build_inventory
from hermclaw.repo_intelligence.languages import category, detect_language, is_binary_name, language_from_shebang, looks_binary
from hermclaw.repo_intelligence.routes import extract_routes
from hermclaw.repo_intelligence.schemas import RepoInventory
from tests.integration.test_repo_intelligence_support import build_fixture_repo, write_files


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    return build_fixture_repo(tmp_path / "fx")


def _routes(inv: RepoInventory) -> set[tuple[str, str, str]]:
    return {(r.method, r.path, r.file) for r in inv.routes}


async def test_inventory_of_a_multi_language_repository(fixture_repo: Path) -> None:
    inv = await build_inventory(fixture_repo)
    paths = {f.path for f in inv.files}
    # files: tracked + untracked-not-ignored; .gitignore honoured
    assert {"app/main.py", "web/server.js", "php/public/index.php", "Dockerfile"} <= paths
    assert "debug.log" not in paths and "build/out.py" not in paths
    assert inv.file_count == len(inv.files) and inv.total_bytes == sum(f.size for f in inv.files)
    entry = {f.path: f for f in inv.files}
    assert entry["app/main.py"].language == "python" and entry["app/main.py"].category == "programming"
    assert entry["bin/run-report"].language == "python"  # shebang detection
    assert entry["assets/logo.png"].binary and entry["assets/logo.png"].language is None
    assert entry["deploy/id_rsa"].sensitive
    assert entry["tests/test_billing.py"].test and entry["web/__tests__/server.test.js"].test
    assert entry["php/tests/ProfileControllerTest.php"].test and not entry["app/main.py"].test
    # languages
    assert set(inv.primary_languages) >= {"python", "javascript", "php"}
    assert {s.language for s in inv.languages} >= {"python", "javascript", "typescript", "php", "sql", "yaml", "markdown"}
    # build systems + package managers incl. lockfiles
    assert {(b.name, b.path) for b in inv.build_systems} >= {
        ("python-pyproject", "pyproject.toml"),
        ("npm-package", "web/package.json"),
        ("composer", "php/composer.json"),
        ("make", "Makefile"),
    }
    pms = {(p.name, p.manifest, p.lockfile) for p in inv.package_managers}
    assert ("uv", "pyproject.toml", "uv.lock") in pms
    assert ("npm", "web/package.json", "web/package-lock.json") in pms
    assert ("composer", "php/composer.json", None) in pms
    # tests
    fws = {f.name: f for f in inv.tests.frameworks}
    assert {"pytest", "jest", "phpunit"} <= set(fws)
    assert "php/phpunit.xml" in fws["phpunit"].evidence
    assert set(inv.tests.test_dirs) >= {"tests", "web/__tests__", "php/tests"}
    assert inv.tests.scripts == {"composer(php):test": "phpunit", "npm(web):test": "jest"}
    # docker / ci
    assert inv.docker.dockerfiles == ["Dockerfile"] and inv.docker.compose_files == ["docker-compose.yml"]
    assert inv.docker.base_images == ["postgres:16", "python:3.12-slim"]  # "base" stage alias is not an image
    assert inv.docker.exposed_ports == ["8000"] and inv.docker.compose_services == ["api", "db"]
    ci = {c.provider: c for c in inv.ci}
    assert ci["gitlab"].stages == ["test", "deploy"] and ci["gitlab"].jobs == ["unit"]
    assert ci["github"].jobs == ["build"]
    # migrations
    mig = {(m.tool, m.path): m for m in inv.migrations}
    assert mig[("sql", "migrations")].tables == ["customers", "payments"]
    assert mig[("alembic", "alembic/versions")].tables == ["users"]
    # configuration
    kinds = {c.path: c.kind for c in inv.config_files}
    assert kinds[".env.example"] == "env-example" and kinds["nginx/site.conf"] == "nginx"
    assert kinds["config/settings.yaml"] == "yaml" and kinds["alembic.ini"] == "tooling"
    assert "deploy/id_rsa" not in kinds
    # entry points
    eps = {(e.kind, e.name) for e in inv.entry_points}
    assert ("console-script", "billing-cli") in eps and ("python-module-main", "app") in eps
    assert ("python-main", "cli.py") in eps and ("npm-script", "start") in eps and ("npm-bin", "web-admin") in eps
    assert ("php-front-controller", "php/public/index.php") in eps and ("container-cmd", "CMD") in eps
    assert ("script", "run-report") in eps
    # routes
    assert _routes(inv) >= {
        ("GET", "/health", "app/main.py"),
        ("POST", "/invoices/{invoice_id}/total", "app/main.py"),
        ("GET", "/api/items", "web/server.js"),
        ("POST", "/orders", "web/server.js"),
        ("GET", "/profile", "php/routes/web.php"),
        ("POST", "/profile/avatar", "php/routes/web.php"),
        ("GET", "/index.php", "php/public/index.php"),
        ("POST", "/index.php", "php/public/index.php"),
    }
    laravel = next(r for r in inv.routes if r.path == "/profile")
    assert laravel.handler == "ProfileController@show" and laravel.framework == "laravel"
    # README (redacted) and git
    assert inv.readme is not None and inv.readme.title == "Billing Platform"
    assert "sk-live-should-not-leak" not in inv.readme.excerpt
    assert inv.git.is_repo and inv.git.branch == "main" and len(inv.git.head or "") == 40 and not inv.git.dirty
    assert inv.warnings == []


async def test_inventory_is_deterministic_and_reflects_git_status(fixture_repo: Path) -> None:
    a = await build_inventory(fixture_repo)
    b = await build_inventory(fixture_repo)
    assert a.model_dump() == b.model_dump()
    (fixture_repo / "app/main.py").write_text("# changed\n", encoding="utf-8")
    write_files(fixture_repo, {"notes/todo.md": "- item\n"})
    (fixture_repo / "Makefile").unlink()
    c = await build_inventory(fixture_repo)
    assert c.git.dirty and c.git.status_counts == {"deleted": 1, "modified": 1, "untracked": 1}
    assert " M app/main.py" in c.git.changed and "?? notes/todo.md" in c.git.changed
    paths = {f.path for f in c.files}
    assert "notes/todo.md" in paths and "Makefile" not in paths
    summary = c.summary()
    assert summary["git"]["dirty"] is True and "files" not in summary and summary["file_count"] == c.file_count


async def test_inventory_of_a_plain_directory_and_detached_head(tmp_path: Path, fixture_repo: Path) -> None:
    plain = build_fixture_repo(tmp_path / "plain", git_init=False)
    inv = await build_inventory(plain)
    assert not inv.git.is_repo
    paths = {f.path for f in inv.files}
    assert "build/out.py" not in paths and "debug.log" not in paths  # ripgrep honours .gitignore without git
    assert "app/main.py" in paths
    no_rg = await build_inventory(plain, RepoIntelConfig(rg_binary="definitely-not-installed-rg"))
    walk_paths = {f.path for f in no_rg.files}
    assert "app/main.py" in walk_paths  # plain directory walk
    import subprocess

    head = subprocess.run(["git", "-C", str(fixture_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(fixture_repo), "checkout", "-q", "--detach", head], check=True)
    det = await build_inventory(fixture_repo)
    assert det.git.detached and det.git.branch is None and det.git.head == head


async def test_inventory_limits_and_invalid_manifests(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    write_files(
        root,
        {
            "package.json": "{not json",
            "pyproject.toml": "[project\nname=",
            "docker-compose.yml": "services: [unclosed",
            "a.py": "x = 1\n",
            "b.py": "y = 2\n",
            "c.py": "z = 3\n",
        },
    )
    inv = await build_inventory(root, RepoIntelConfig(max_files=2))
    assert inv.truncated and inv.file_count == 2
    full = await build_inventory(root)
    assert set(full.warnings) >= {"package.json: invalid JSON", "pyproject.toml: invalid TOML", "docker-compose.yml: invalid YAML"}
    assert {b.name for b in full.build_systems} >= {"npm-package", "python-pyproject"}


def test_language_detection() -> None:
    assert detect_language("src/app.tsx") == "tsx" and detect_language("x/Component.vue") == "vue"
    assert detect_language("views/home.blade.php") == "php"
    assert detect_language("Dockerfile.prod") == "dockerfile" and detect_language("CMakeLists.txt") == "cmake"
    assert detect_language("requirements-dev.txt") == "pip-requirements" and detect_language(".env.example") == "dotenv"
    assert detect_language("scripts/deploy", b"#!/usr/bin/env bash\nset -e\n") == "shell"
    assert detect_language("tool", b"#!/usr/bin/python3.12\n") == "python"
    assert detect_language("tool", b"#!/usr/bin/env -S node --no-warnings\n") == "javascript"
    assert language_from_shebang(b"#!/opt/custom/interpreter\n") is None
    assert detect_language("LICENSE") is None
    assert is_binary_name("a/b/model.safetensors") and not is_binary_name("a.py")
    assert looks_binary(b"\x00\x01abc") and not looks_binary("grüße\n".encode())
    assert category("python") == "programming" and category("yaml") == "config" and category(None) == "unknown"


@pytest.mark.parametrize(
    ("path", "lang", "source", "expected"),
    [
        (
            "app.py",
            "python",
            "from flask import Flask, Blueprint\napp = Flask(__name__)\nbp = Blueprint('b', __name__, url_prefix='/admin')\n"
            "@app.route('/login', methods=['GET', 'POST'])\ndef login():\n    pass\n@bp.get('/users/<int:uid>')\ndef user(uid):\n    pass\n"
            "app.add_url_rule('/ping', view_func=login)\n",
            {("GET", "/login", "login"), ("POST", "/login", "login"), ("GET", "/admin/users/<int:uid>", "user"), ("GET", "/ping", "login")},
        ),
        (
            "proj/urls.py",
            "python",
            "from django.urls import path\nurlpatterns = [path('articles/<int:pk>/', views.ArticleView.as_view())]\n",
            {("ANY", "/articles/<int:pk>/", "ArticleView")},
        ),
        (
            "src/users.controller.ts",
            "typescript",
            "import { Controller, Get, Post } from '@nestjs/common';\n@Controller('users')\nexport class UsersController {\n"
            "  @Get(':id')\n  findOne() {}\n  @Post()\n  create() {}\n}\n",
            {("GET", "/users/:id", "findOne"), ("POST", "/users", "create")},
        ),
        (
            "app/api/items/route.ts",
            "typescript",
            "export async function GET(req) {}\nexport async function POST(req) {}\n",
            {("GET", "/api/items", "GET"), ("POST", "/api/items", "POST")},
        ),
        (
            "server.js",
            "javascript",
            "const r = express.Router();\nr.route('/books').get(list).post(create);\nfastify.route({ method: 'PUT', url: '/books/:id', handler });\n",
            {("GET", "/books", None), ("POST", "/books", None), ("PUT", "/books/:id", None)},
        ),
        (
            "src/routes.php",
            "php",
            "<?php\n$app->get('/hello/{name}', function ($req) {});\nRoute::match(['get', 'post'], '/form', 'FormController@handle');\n",
            {("GET", "/hello/{name}", None), ("GET", "/form", "FormController@handle"), ("POST", "/form", "FormController@handle")},
        ),
        (
            "src/Controller/Blog.php",
            "php",
            "<?php\nclass Blog {\n    #[Route('/blog/{slug}', methods: ['GET'])]\n    public function show() {}\n}\n",
            {("GET", "/blog/{slug}", "show")},
        ),
        (
            "main.go",
            "go",
            'package main\nfunc main() {\n  http.HandleFunc("GET /status", h)\n  r.POST("/jobs", create)\n}\n',
            {("GET", "/status", None), ("POST", "/jobs", None)},
        ),
        ("Api.java", "java", '@GetMapping("/orders/{id}")\npublic Order get() {}\n', {("GET", "/orders/{id}", None)}),
    ],
)
def test_route_extraction_is_generic(path: str, lang: str, source: str, expected: set[tuple[str, str, str | None]]) -> None:
    got = {(r.method, r.path, r.handler) for r in extract_routes(path, lang, source)}
    assert expected <= got, got


def test_route_extraction_survives_broken_python() -> None:
    src = "@app.get('/broken')\ndef f(:\n"
    assert {(r.method, r.path) for r in extract_routes("x.py", "python", src)} == {("GET", "/broken")}


async def test_symlinks_and_special_files_are_not_listed(tmp_path: Path) -> None:
    root = build_fixture_repo(tmp_path / "fx")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret outside the workspace\n", encoding="utf-8")
    os.symlink(outside, root / "link.txt")
    inv = await build_inventory(root)
    assert "link.txt" not in {f.path for f in inv.files}


async def test_inventory_never_carries_credentials_from_manifests(tmp_path: Path) -> None:
    root = tmp_path / "creds"
    write_files(
        root,
        {
            "package.json": (
                '{"name": "x", "scripts": {"deploy": "curl -H \\"Authorization: Bearer abcdefghijkl123456\\" https://x",'
                ' "test": "API_TOKEN=\\"tok-very-secret-1\\" jest"}}'
            ),
            "Dockerfile": 'FROM alpine\nCMD ["run", "--password=hunter2hunter2"]\n',
        },
    )
    inv = await build_inventory(root)
    dumped = inv.model_dump_json()
    for secret in ("abcdefghijkl123456", "tok-very-secret-1", "hunter2hunter2"):
        assert secret not in dumped
    assert {e.name for e in inv.entry_points} >= {"deploy", "test", "CMD"}
