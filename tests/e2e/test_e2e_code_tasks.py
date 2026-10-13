"""P37 end-to-end matrix – code tasks through the full runtime (harness: tests/e2e/harness.py).

Every scenario: triage (fast router) → inventory/context → Gemma plan → scope → Qwen3-Coder tool turns → deterministic
verifier (acceptance commands really run: pytest, php, tsc/node, python checks) → heavy review → verified commit →
regression rerun → push → final report."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.harness import Scenario, act, done, replace, run_scenario, write_file
from tests.unit.test_planner_support import plan, step

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.slow]
PY = sys.executable


def _review(dep: str) -> dict[str, Any]:
    return step("S099", "review", "review", "Review the whole change.", depends_on=[dep])


# ------------------------------------------------------------------------------------------- 37.1 FastAPI feature
FASTAPI_SEED = {
    "app/__init__.py": "",
    "app/main.py": 'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get("/items")\ndef items() -> list[str]:\n    return ["a", "b"]\n',
    "tests/__init__.py": "",
    "tests/test_items.py": (
        "from fastapi.testclient import TestClient\n\nfrom app.main import app\n\n\n"
        'def test_items() -> None:\n    assert TestClient(app).get("/items").json() == ["a", "b"]\n'
    ),
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
}
HEALTH_ROUTE = '\n\n@app.get("/health")\ndef health() -> dict[str, str]:\n    return {"status": "ok"}\n'
HEALTH_TEST = (
    "from fastapi.testclient import TestClient\n\nfrom app.main import app\n\n\n"
    'def test_health() -> None:\n    r = TestClient(app).get("/health")\n    assert r.status_code == 200 and r.json() == {"status": "ok"}\n'
)


def fastapi_scenario() -> Scenario:
    return Scenario(
        name="fastapi",
        title="Add a health endpoint",
        prompt="Add GET /health to the FastAPI app returning {'status': 'ok'} with a test.",
        seed=FASTAPI_SEED,
        plans=[
            plan(
                "Health endpoint",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Add GET /health returning {'status': 'ok'} to app/main.py and a pytest test in tests/test_health.py.",
                        repo_hints=["app/main.py"],
                        allowed_new_paths=["tests/test_health.py"],
                        acceptance=[
                            {"type": "test", "command": f"{PY} -m pytest -q", "framework": "pytest", "min_passed": 2},
                            {"type": "presence", "path_glob": "app/main.py", "pattern": '@app.get\\("/health"\\)'},
                        ],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                act("read_file", path="app/main.py"),
                replace("app/main.py", '    return ["a", "b"]\n', '    return ["a", "b"]\n' + HEALTH_ROUTE),
                write_file("tests/test_health.py", HEALTH_TEST),
                act("run_test", command=f"{PY} -m pytest -q"),
                done("GET /health added with a test", ["app/main.py", "tests/test_health.py"], [f"{PY} -m pytest -q"]),
            ]
        },
    )


async def test_37_1_python_fastapi_feature(sessionmaker: Any, tmp_path: Path) -> None:
    out = await run_scenario(sessionmaker, fastapi_scenario(), tmp_path)
    assert '@app.get("/health")' in out.remote_file("app/main.py")
    assert "def test_health" in out.remote_file("tests/test_health.py")
    assert len(out.commits()) == 1 and out.steps["S099"].result["verdict"] == "pass"
    assert "test.passed" in out.event_types() or "verifier.finished" in out.event_types()
    assert out.report and "S001" in out.report


# ------------------------------------------------------------------------------------------- 37.2 SQLite migration
MIGRATE_PY = '''"""Apply migrations/*.sql in order to a SQLite database."""
import sqlite3
import shutil
import sys
from pathlib import Path


def migrate(db: str) -> list[str]:
    con = sqlite3.connect(db)
    applied = []
    for sql in sorted(Path(__file__).resolve().parents[1].joinpath("migrations").glob("*.sql")):
        con.executescript(sql.read_text())
        applied.append(sql.name)
    con.commit()
    con.close()
    return applied


if __name__ == "__main__":
    print(migrate(sys.argv[1]))
'''
SQLITE_SEED = {
    "migrations/001_init.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n",
    "tools/__init__.py": "",
    "tools/migrate.py": MIGRATE_PY,
    "tests/__init__.py": "",
    "tests/test_migrations.py": (
        "import sqlite3\n\nfrom tools.migrate import migrate\n\n\n"
        "def test_users_table(tmp_path):\n    db = str(tmp_path / 'a.db')\n    migrate(db)\n"
        "    cols = [r[1] for r in sqlite3.connect(db).execute('PRAGMA table_info(users)')]\n    assert 'name' in cols\n"
    ),
}
EMAIL_SQL = (
    "ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT '';\nCREATE UNIQUE INDEX ix_users_email ON users(email) WHERE email <> '';\n"
)
EMAIL_TEST = (
    "import sqlite3\n\nfrom tools.migrate import migrate\n\n\n"
    "def test_email_column_and_index(tmp_path):\n    db = str(tmp_path / 'b.db')\n    assert migrate(db)[-1] == '002_add_email.sql'\n"
    "    con = sqlite3.connect(db)\n    assert 'email' in [r[1] for r in con.execute('PRAGMA table_info(users)')]\n"
    "    assert 'ix_users_email' in [r[1] for r in con.execute('PRAGMA index_list(users)')]\n"
)


async def test_37_2_sqlite_schema_migration(sessionmaker: Any, tmp_path: Path) -> None:
    sc = Scenario(
        name="sqlite",
        title="Add users.email",
        prompt="Add an email column with a unique index to the users table (SQLite migration).",
        seed=SQLITE_SEED,
        plans=[
            plan(
                "users.email",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Create migrations/002_add_email.sql adding users.email (NOT NULL DEFAULT '') with a partial unique index, plus a migration test.",
                        repo_hints=["migrations/001_init.sql", "tools/migrate.py"],
                        allowed_new_paths=["migrations/002_add_email.sql", "tests/test_email_migration.py"],
                        acceptance=[
                            {"type": "test", "command": f"{PY} -m pytest -q", "framework": "pytest", "min_passed": 2},
                            {"type": "presence", "path_glob": "migrations/002_add_email.sql", "pattern": "ADD COLUMN email"},
                            {"type": "diff", "must_not_change": ["migrations/001_init.sql"]},
                        ],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                write_file("migrations/002_add_email.sql", EMAIL_SQL),
                write_file("tests/test_email_migration.py", EMAIL_TEST),
                done("email migration + test", ["migrations/002_add_email.sql", "tests/test_email_migration.py"]),
            ]
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert "ADD COLUMN email" in out.remote_file("migrations/002_add_email.sql")
    assert out.remote_file("migrations/001_init.sql") == SQLITE_SEED["migrations/001_init.sql"].strip()


# ------------------------------------------------------------------------------------------- TS / TSX toolchain
TSC = shutil.which("tsc")
NODE = shutil.which("node")
PHP = shutil.which("php")
needs_node = pytest.mark.skipif(not (TSC and NODE), reason="tsc/node missing")

H_TS = (
    "export function h(tag: string, props: Record<string, string> | null, ...children: unknown[]): string {\n"
    "  const attrs = Object.entries(props ?? {}).map(([k, v]) => ` ${k === 'className' ? 'class' : k}=\"${v}\"`).join('');\n"
    "  return `<${tag}${attrs}>${children.flat().join('')}</${tag}>`;\n}\n"
)
JSX_D_TS = (
    "declare namespace JSX {\n  type Element = string;\n  interface IntrinsicElements { [name: string]: Record<string, string>; }\n}\n"
)
NAV_TSX = (
    "import { h } from './h.js';\n\nexport function Nav(current: string): string {\n"
    "  return (\n    <nav className=\"nav\">\n      <a href=\"/\" className={current === '/' ? 'active' : ''}>Home</a>\n    </nav>\n  );\n}\n"
)
NAV_TSX_NEW = (
    "import { h } from './h.js';\n\nconst LINKS: [string, string][] = [['/', 'Home'], ['/about', 'About']];\n\n"
    "export function Nav(current: string): string {\n"
    "  return <nav className=\"nav\">{LINKS.map(([href, label]) => <a href={href} className={current === href ? 'active' : ''}>{label}</a>)}</nav>;\n}\n"
)
TSCONFIG_JSX = (
    '{"compilerOptions": {"target": "es2022", "module": "es2022", "moduleResolution": "bundler", "jsx": "react", '
    '"jsxFactory": "h", "outDir": "dist", "rootDir": "src", "strict": true, "skipLibCheck": true, "types": []}, "include": ["src"]}\n'
)
NAV_TEST = (
    "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\nimport { Nav } from '../dist/Nav.js';\n\n"
    "test('about link is active on /about', () => {\n  const html = Nav('/about');\n"
    '  assert.match(html, /<a href="\\/about" class="active">About<\\/a>/);\n  assert.match(html, /Home/);\n});\n'
)


@needs_node
async def test_37_5_react_css_navigation(sessionmaker: Any, tmp_path: Path) -> None:
    seed = {
        "package.json": '{"name": "nav", "type": "module", "private": true}\n',
        "tsconfig.json": TSCONFIG_JSX,
        "src/h.ts": H_TS,
        "src/jsx.d.ts": JSX_D_TS,
        "src/Nav.tsx": NAV_TSX,
        "styles/nav.css": ".nav { display: flex; gap: 1rem; }\n",
    }
    css_new = ".nav { display: flex; gap: 1rem; }\n.nav a.active { font-weight: 700; border-bottom: 2px solid currentColor; }\n"
    cmd = f"{TSC} -p . && {NODE} --test test/*.test.mjs"
    sc = Scenario(
        name="tsx-nav",
        title="Navigation: About link + active state",
        prompt="Add an About link to the navigation, highlight the active link and style it.",
        seed=seed,
        plans=[
            plan(
                "nav",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Render Home and About links in src/Nav.tsx with class 'active' for the current path; style .nav a.active in styles/nav.css; add test/nav.test.mjs.",
                        repo_hints=["src/Nav.tsx", "styles/nav.css"],
                        allowed_new_paths=["test/nav.test.mjs"],
                        acceptance=[
                            {"type": "test", "command": cmd, "framework": "generic", "min_passed": 0, "timeout_seconds": 300},
                            {"type": "presence", "path_glob": "styles/nav.css", "pattern": "\\.nav a\\.active"},
                        ],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                write_file("src/Nav.tsx", NAV_TSX_NEW),
                write_file("styles/nav.css", css_new),
                write_file("test/nav.test.mjs", NAV_TEST),
                act("run_command", command=cmd),
                done("About link + active styling", ["src/Nav.tsx", "styles/nav.css", "test/nav.test.mjs"]),
            ]
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert "/about" in out.remote_file("src/Nav.tsx") and ".nav a.active" in out.remote_file("styles/nav.css")


# ------------------------------------------------------------------------------------------- 37.6 PHP form
VALIDATOR_PHP = (
    "<?php\ndeclare(strict_types=1);\n\nfinal class Validator\n{\n    /** @return array<string, string> */\n"
    "    public static function validate(array $input): array\n    {\n        $errors = [];\n"
    "        if (trim((string)($input['name'] ?? '')) === '') {\n            $errors['name'] = 'Name is required';\n        }\n"
    "        return $errors;\n    }\n}\n"
)
VALIDATOR_EMAIL = (
    "        if (filter_var((string)($input['email'] ?? ''), FILTER_VALIDATE_EMAIL) === false) {\n"
    "            $errors['email'] = 'A valid e-mail address is required';\n        }\n        return $errors;\n"
)
PHP_TESTS = (
    "<?php\nrequire __DIR__ . '/../src/Validator.php';\n\nfunction check(bool $ok, string $what): void { if (!$ok) { fwrite(STDERR, \"FAIL: $what\\n\"); exit(1); } }\n\n"
    "check(Validator::validate(['name' => 'Ann', 'email' => 'ann@example.org']) === [], 'valid input');\n"
    "check(isset(Validator::validate(['name' => 'Ann', 'email' => 'nope'])['email']), 'invalid email');\n"
    "check(isset(Validator::validate(['email' => 'a@b.de'])['name']), 'missing name');\necho \"OK\\n\";\n"
)


@pytest.mark.skipif(PHP is None, reason="php missing")
async def test_37_6_php_backend_form(sessionmaker: Any, tmp_path: Path) -> None:
    seed = {
        "src/Validator.php": VALIDATOR_PHP,
        "public/contact.php": "<?php\nrequire __DIR__ . '/../src/Validator.php';\n$errors = Validator::validate($_POST);\nhttp_response_code($errors ? 422 : 200);\necho json_encode(['errors' => $errors]);\n",
    }
    cmd = f"{PHP} -l src/Validator.php && {PHP} tests/run.php"
    sc = Scenario(
        name="php-form",
        title="Contact form: validate e-mail",
        prompt="The contact form must reject invalid e-mail addresses with a clear message.",
        seed=seed,
        plans=[
            plan(
                "email validation",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Validate 'email' with FILTER_VALIDATE_EMAIL in src/Validator.php and add tests/run.php covering valid/invalid input.",
                        repo_hints=["src/Validator.php"],
                        allowed_new_paths=["tests/run.php"],
                        acceptance=[
                            {"type": "test", "command": cmd, "framework": "generic", "min_passed": 0},
                            {"type": "command", "command": f"{PHP} tests/run.php", "stdout_pattern": "OK"},
                        ],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                replace("src/Validator.php", "        return $errors;\n", VALIDATOR_EMAIL),
                write_file("tests/run.php", PHP_TESTS),
                act("run_command", command=cmd),
                done("e-mail validation", ["src/Validator.php", "tests/run.php"]),
            ]
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert "FILTER_VALIDATE_EMAIL" in out.remote_file("src/Validator.php")


# ------------------------------------------------------------------------------------------- 37.7 TypeScript utility
@needs_node
async def test_37_7_typescript_utility(sessionmaker: Any, tmp_path: Path) -> None:
    seed = {
        "package.json": '{"name": "strings", "type": "module", "private": true}\n',
        "tsconfig.json": '{"compilerOptions": {"target": "es2022", "module": "es2022", "moduleResolution": "bundler", "outDir": "dist", "rootDir": "src", "strict": true, "types": []}, "include": ["src"]}\n',
        "src/strings.ts": "export function capitalize(s: string): string {\n  return s.charAt(0).toUpperCase() + s.slice(1);\n}\n",
    }
    slugify = (
        "\nexport function slugify(s: string): string {\n  return s\n    .normalize('NFKD')\n    .replace(/[\\u0300-\\u036f]/g, '')\n"
        "    .toLowerCase()\n    .replace(/[^a-z0-9]+/g, '-')\n    .replace(/^-+|-+$/g, '');\n}\n"
    )
    test_js = (
        "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\nimport { slugify, capitalize } from '../dist/strings.js';\n\n"
        "test('slugify', () => {\n  assert.equal(slugify('  Über Café & Co! '), 'uber-cafe-co');\n  assert.equal(slugify('a--b'), 'a-b');\n});\n"
        "test('capitalize still works', () => assert.equal(capitalize('abc'), 'Abc'));\n"
    )
    cmd = f"{TSC} -p . && {NODE} --test test/*.test.mjs"
    sc = Scenario(
        name="ts-util",
        title="slugify utility",
        prompt="Add a slugify(s) utility (ASCII, lowercase, dash separated) with tests.",
        seed=seed,
        plans=[
            plan(
                "slugify",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Add export function slugify(s: string): string to src/strings.ts and node tests in test/strings.test.mjs.",
                        repo_hints=["src/strings.ts"],
                        allowed_new_paths=["test/strings.test.mjs"],
                        acceptance=[{"type": "test", "command": cmd, "framework": "generic", "min_passed": 0}],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                replace(
                    "src/strings.ts",
                    "  return s.charAt(0).toUpperCase() + s.slice(1);\n}\n",
                    "  return s.charAt(0).toUpperCase() + s.slice(1);\n}\n" + slugify,
                ),
                write_file("test/strings.test.mjs", test_js),
                done("slugify + tests", ["src/strings.ts", "test/strings.test.mjs"]),
            ]
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert "export function slugify" in out.remote_file("src/strings.ts")


# ------------------------------------------------------------------------------------------- 37.8 YAML-only config
async def test_37_8_yaml_only_config(sessionmaker: Any, tmp_path: Path) -> None:
    seed = {"config/app.yaml": "services:\n  web:\n    port: 8080\n    enabled: true\n", "README.md": "# app\n"}
    schema = {
        "type": "object",
        "required": ["services"],
        "properties": {
            "services": {
                "type": "object",
                "required": ["web", "cache"],
                "properties": {
                    "cache": {
                        "type": "object",
                        "required": ["port", "enabled"],
                        "properties": {"port": {"const": 6379}, "enabled": {"const": True}},
                    }
                },
            }
        },
    }
    sc = Scenario(
        name="yaml",
        title="Add the cache service",
        prompt="Add a cache service on port 6379 (enabled) to config/app.yaml; do not touch anything else.",
        seed=seed,
        triage={"intent": "mixed", "risk": "low"},
        plans=[
            plan(
                "cache service",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Add services.cache with port 6379 and enabled: true to config/app.yaml.",
                        repo_hints=["config/app.yaml"],
                        acceptance=[
                            {"type": "schema", "path": "config/app.yaml", "format": "yaml", "json_schema": schema},
                            {"type": "diff", "must_change": ["config/app.yaml"], "max_changed_files": 1},
                        ],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                replace("config/app.yaml", "    enabled: true\n", "    enabled: true\n  cache:\n    port: 6379\n    enabled: true\n"),
                done("cache service", ["config/app.yaml"]),
            ]
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert "cache:" in out.remote_file("config/app.yaml") and out.remote_file("README.md") == "# app"


# ------------------------------------------------------------------------------------------- 37.9 shell / linux admin
ROTATE_SH = (
    "#!/usr/bin/env bash\nset -euo pipefail\n# rotate-logs.sh DIR – gzip *.log files older than the newest one\n"
    'dir="$1"\nls -1t "$dir"/*.log | tail -n +2 | while read -r f; do gzip -f "$f"; done\n'
)
ROTATE_SH_NEW = (
    "#!/usr/bin/env bash\nset -euo pipefail\n# rotate-logs.sh [--keep N] DIR – gzip all but the N newest *.log files (default 1)\n"
    'keep=1\nif [ "${1:-}" = "--keep" ]; then keep="$2"; shift 2; fi\n'
    'case "$keep" in \'\'|*[!0-9]*) echo "invalid --keep: $keep" >&2; exit 2;; esac\n'
    'dir="$1"\nls -1t "$dir"/*.log | tail -n +$((keep + 1)) | while read -r f; do gzip -f "$f"; done\n'
)
ROTATE_TEST = (
    '#!/usr/bin/env bash\nset -euo pipefail\nd=$(mktemp -d)\nfor i in 1 2 3 4; do echo $i > "$d/app$i.log"; touch -d "-$i minutes" "$d/app$i.log"; done\n'
    'bash scripts/rotate-logs.sh --keep 2 "$d"\n[ "$(ls "$d"/*.log | wc -l)" -eq 2 ] && [ "$(ls "$d"/*.gz | wc -l)" -eq 2 ]\n'
    "if bash scripts/rotate-logs.sh --keep x \"$d\" 2>/dev/null; then echo 'accepted invalid keep'; exit 1; fi\necho PASS\n"
)


async def test_37_9_shell_linux_admin(sessionmaker: Any, tmp_path: Path) -> None:
    cmd = "bash -n scripts/rotate-logs.sh && bash tests/test_rotate.sh"
    sc = Scenario(
        name="shell",
        title="rotate-logs --keep N",
        prompt="Add a --keep N option to scripts/rotate-logs.sh (keep the N newest logs uncompressed) with a test.",
        seed={"scripts/rotate-logs.sh": ROTATE_SH},
        triage={"intent": "administration"},
        plans=[
            plan(
                "--keep",
                [
                    step(
                        "S001",
                        "implement",
                        "coding",
                        "Implement --keep N in scripts/rotate-logs.sh with input validation; add tests/test_rotate.sh.",
                        repo_hints=["scripts/rotate-logs.sh"],
                        allowed_new_paths=["tests/test_rotate.sh"],
                        acceptance=[
                            {"type": "test", "command": cmd, "framework": "generic", "min_passed": 0},
                            {"type": "command", "command": "bash tests/test_rotate.sh", "stdout_pattern": "PASS"},
                        ],
                    ),
                    _review("S001"),
                ],
            )
        ],
        coder={
            "S001": [
                write_file("scripts/rotate-logs.sh", ROTATE_SH_NEW),
                write_file("tests/test_rotate.sh", ROTATE_TEST),
                done("--keep", ["scripts/rotate-logs.sh", "tests/test_rotate.sh"]),
            ]
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert "--keep" in out.remote_file("scripts/rotate-logs.sh")


# ------------------------------------------------------------------------------------------- 37.15 replan after failure
async def test_37_15_planner_replan_after_failure(sessionmaker: Any, tmp_path: Path) -> None:
    first = plan(
        "health",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Add GET /health to app/main.py.",
                repo_hints=["app/main.py"],
                acceptance=[{"type": "test", "command": f"{PY} -m pytest -q", "framework": "pytest", "min_passed": 2}],
            ),
            _review("S001"),
        ],
    )
    second = plan(
        "health (new test file allowed)",
        [
            step(
                "S002",
                "implement",
                "coding",
                "Add GET /health to app/main.py and the test file tests/test_health.py (now explicitly allowed).",
                repo_hints=["app/main.py"],
                allowed_new_paths=["tests/test_health.py"],
                acceptance=[{"type": "test", "command": f"{PY} -m pytest -q", "framework": "pytest", "min_passed": 2}],
            ),
            step("S003", "review", "review", "Review the whole change.", depends_on=["S002"]),
        ],
        summary="replanned: the acceptance needs a new test file outside the first scope",
    )
    sc = Scenario(
        name="replan",
        title="Add a health endpoint (needs replan)",
        prompt="Add GET /health with a test.",
        seed=FASTAPI_SEED,
        plans=[first, second],
        coder={
            "S001": [
                act("request_replan", reason="Acceptance requires a second passing test, but tests/test_health.py is outside my scope."),
            ],
            "S002": [
                replace("app/main.py", '    return ["a", "b"]\n', '    return ["a", "b"]\n' + HEALTH_ROUTE),
                write_file("tests/test_health.py", HEALTH_TEST),
                done("health endpoint + test", ["app/main.py", "tests/test_health.py"]),
            ],
        },
    )
    out = await run_scenario(sessionmaker, sc, tmp_path)
    assert out.job.replan_count == 1 and "replan.created" in out.event_types()
    assert set(out.steps) == {"S002", "S003"} and all(s.status == "completed" for s in out.steps.values())
    assert "def test_health" in out.remote_file("tests/test_health.py")
