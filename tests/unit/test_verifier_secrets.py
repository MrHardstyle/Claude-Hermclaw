"""Secret scanner of the verifier (P21 21.7). Secret-shaped strings are assembled at runtime so this file holds none."""

from __future__ import annotations

import pytest

from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from hermclaw.verifier.secrets import REDACTION_PATTERNS, SecretScanner, is_placeholder, shannon_entropy
from hermclaw.verifier.types import AddedLine

GH = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GL = "gl" + "pat-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm"
AWS = "AK" + "IA" + "Q3EGRZ7XN4WVDLMK"
SK = "s" + "k-" + "proj9Fh2Kd8Lq0Zx3Vb7Nm1Tr"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
OPENSSH = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
RANDOM_TOKEN = "q8Zr3Kx7Lm2Np9Vb4Tc6Wd1Ys5Hf0Ju8Ge3Ra7"
JWT = "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"


def scan(path: str, *lines: str) -> list[str]:
    scanner = SecretScanner()
    return [f.rule for f in scanner.scan_file(path, [AddedLine(i + 1, t) for i, t in enumerate(lines)])]


@pytest.mark.parametrize(
    ("path", "line", "rule"),
    [
        ("app.py", f'TOKEN = "{GH}"', "github_token"),
        ("ci.yml", f"token: {GL}", "gitlab_token"),
        ("deploy.sh", f"export AWS_ACCESS_KEY_ID={AWS}", "aws_access_key"),
        ("client.ts", f'const key = "{SK}";', "api_key"),
        ("key.txt", PEM, "private_key"),
        ("id.txt", OPENSSH, "private_key"),
        ("settings.py", 'DB_PASSWORD = "hunter2-correct-horse"', "secret_assignment"),
        ("config.yaml", "password: s3cr3t-v4lue", "secret_assignment"),
        (".env.production", "API_KEY=abcd1234efgh", "secret_assignment"),
        ("settings.json", '{"client_secret": "x9f8e7d6c5"}', "secret_assignment"),
        ("db.toml", 'url = "postgresql://app:Sup3rS3cret@db.internal:5432/app"', "url_credential"),
        ("http.txt", "Authorization: Bearer abcdef0123456789xyz", "bearer_token"),
        ("auth.py", f'SAMPLE = "{JWT}"', "jwt"),
        ("cfg.py", f'SIGNING = "{RANDOM_TOKEN}"', "high_entropy"),
    ],
)
def test_secret_shapes_are_found(path: str, line: str, rule: str) -> None:
    assert rule in scan(path, line)


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("app.py", 'password = os.environ["DB_PASSWORD"]'),
        ("app.py", "token = get_token()"),
        ("app.py", "api_key = settings.api_key"),
        ("app.ts", "const token = process.env.TOKEN;"),
        ("app.ts", "password: string;"),
        ("models.py", "password: str = Field(min_length=8)"),
        ("config.yaml", "password: ${DB_PASSWORD}"),
        ("config.yaml", "token: <your-token-here>"),
        ("settings.py", 'SECRET_KEY = "changeme"'),
        ("settings.py", 'api_key = "your_api_key_here"'),
        ("docs.md", "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
        ("db.yaml", "url: postgresql://postgres:postgres@localhost:5432/test"),
        ("db.yaml", "url: postgresql://app:${DB_PASSWORD}@db:5432/app"),
        ("package-lock.json", '"integrity": "sha512-Zq8Zr3Kx7Lm2Np9Vb4Tc6Wd1Ys5Hf0Ju8Ge3Ra7Qq8Zr3Kx7Lm2Np9Vb4Tc6Wd1Ys5Hf0Ju8Ge3Ra7=="'),
        ("index.html", '<script integrity="sha384-Zq8Zr3Kx7Lm2Np9Vb4Tc6Wd1Ys5Hf0Ju8Ge3Ra7Qq8Zr3Kx7Lm2"></script>'),
        ("app.py", 'COMMIT = "4f3c2b1a9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a"'),
        ("app.py", 'ID = "123e4567-e89b-12d3-a456-426614174000"'),
        ("app.py", 'NAME = "getUserAccountSettingsForTenantAndRegion2025"'),
        ("app.py", 'CONST = "THIS_IS_A_VERY_LONG_CONSTANT_NAME_FOR_TESTS_42"'),
        ("app.py", "max_tokens = 4096"),
        ("app.py", 'logo = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"'),
        ("app.py", "x = foo_bar_baz_qux_quux_corge_grault_garply_waldo(1)"),
    ],
)
def test_ordinary_code_is_not_reported(path: str, line: str) -> None:
    assert scan(path, line) == []


def test_findings_never_contain_the_secret_value() -> None:
    scanner = SecretScanner()
    findings = scanner.scan_file("settings.py", [AddedLine(3, f'GITHUB = "{GH}"'), AddedLine(4, 'PASSWORD = "hunter2-correct-horse"')])
    assert [(f.line, f.rule) for f in findings] == [(3, "github_token"), (4, "secret_assignment")]
    for f in findings:
        assert GH not in f.preview and "hunter2" not in f.preview
        assert REDACTED in f.preview
        assert GH not in str(f.to_dict())


def test_every_redaction_pattern_is_covered() -> None:
    """Anything the runtime redactor would mask in a log may not be committed either."""
    samples = [
        f"{PEM}\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "Authorization: Bearer abcdef0123456789xyz",
        "password=hunter2-correct-horse",
        GL,
        GH,
        SK,
        AWS,
        "postgresql://app:Sup3rS3cret@db:5432/app",
    ]
    assert len(samples) >= len(REDACTION_PATTERNS)
    plain = Redactor()
    for sample in samples:
        first_line = sample.split("\n", 1)[0]
        assert plain.text(sample) != sample
        assert scan("notes.txt", first_line), sample


def test_registered_literal_secrets_are_detected() -> None:
    literal = "corporate-db-pass-2026"
    DEFAULT_REDACTOR.add_literal(literal)
    try:
        assert "known_secret" in scan("app.py", f"conn = connect(password_value='{literal}')")
        finding = SecretScanner().scan_file("app.py", [AddedLine(1, f"x = '{literal}'")])[0]
        assert literal not in finding.preview
    finally:
        DEFAULT_REDACTOR._literals.remove(literal)


def test_lines_already_redacted_by_a_git_reader_count_as_findings() -> None:
    assert scan("app.py", f"token = '{REDACTED}'") == ["redacted_secret"]


def test_entropy_heuristic_skips_lock_files_and_needs_literal_context() -> None:
    assert scan("yarn.lock", f'  resolved "{RANDOM_TOKEN}"') == []
    assert scan("app.py", f"value = compute({RANDOM_TOKEN})") == []  # unquoted identifier-like argument
    assert scan("app.py", f'value = "{RANDOM_TOKEN}"') == ["high_entropy"]
    assert scan("app.py", 'API_KEY_HEX = "4f3c2b1a9d8e7f6a5b4c3d2e1f0a9b8c"') != []  # hex with keyword context


def test_long_lines_are_bounded() -> None:
    line = "x" * 50_000 + f' "{GH}"'
    assert scan("min.js", line) == []  # beyond the scanned head
    assert scan("min.js", f'"{GH}" ' + "x" * 50_000) == ["github_token"]


def test_helpers() -> None:
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("abcd") == pytest.approx(2.0)
    for value in ("${X}", "<token>", "changeme", "xxxxxxxx", "123456", "", "your_token", "{{ secret }}"):
        assert is_placeholder(value), value
    assert not is_placeholder("hunter2-correct-horse")
