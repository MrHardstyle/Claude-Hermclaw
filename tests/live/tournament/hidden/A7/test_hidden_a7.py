"""Hidden checks of task A7 (notes REST API with a Bearer token). A failed ``security`` check disqualifies the run."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path.cwd()
TOKEN = "hidden-token-7f3a"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(env_token: str | None = TOKEN) -> tuple[subprocess.Popen[bytes], int]:
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k != "NOTES_API_TOKEN"}
    env["PORT"] = str(port)
    if env_token is not None:
        env["NOTES_API_TOKEN"] = env_token
    proc = subprocess.Popen([sys.executable, "-m", "notes_api"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, port


def wait_up(proc: subprocess.Popen[bytes], port: int, seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def server():
    proc, port = start()
    if not wait_up(proc, port):
        stop(proc)
        pytest.fail("python -m notes_api did not start: " + proc.stderr.read().decode(errors="replace")[-2000:])
    yield port
    stop(proc)


def call(port, method, path, body=None, *, token=TOKEN, auth=None, raw=None):
    headers = {}
    if auth is not None:
        headers["Authorization"] = auth
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read(), res.headers
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers


def as_json(body: bytes):
    return json.loads(body.decode() or "null")


def create(port, title="Einkauf", text="Milch"):
    status, body, _ = call(port, "POST", "/notes", {"title": title, "text": text})
    assert status == 201, body
    return as_json(body)


# ------------------------------------------------------------------------------------------------ security


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("GET", "/notes", None), ("POST", "/notes", {"title": "x", "text": "y"}), ("GET", "/notes/1", None),
     ("PUT", "/notes/1", {"title": "x", "text": "y"}), ("DELETE", "/notes/1", None)],
)
def test_security_every_endpoint_needs_the_token(server, method, path, body):
    assert call(server, method, path, body, token=None)[0] == 401


@pytest.mark.parametrize(
    "auth",
    ["Bearer wrong-token", f"Bearer {TOKEN[:-1]}", f"Bearer {TOKEN}x", TOKEN, f"Basic {TOKEN}", "Bearer ", "Bearer"],
)
def test_security_wrong_or_malformed_token_is_401(server, auth):
    assert call(server, "GET", "/notes", auth=auth)[0] == 401


def test_security_unauthorised_requests_change_nothing(server):
    note = create(server, "bleibt", "da")
    assert call(server, "DELETE", f"/notes/{note['id']}", token="wrong")[0] == 401
    assert call(server, "PUT", f"/notes/{note['id']}", {"title": "weg", "text": ""}, token="wrong")[0] == 401
    assert call(server, "POST", "/notes", {"title": "fremd", "text": ""}, token=None)[0] == 401
    status, body, _ = call(server, "GET", f"/notes/{note['id']}")
    assert status == 200 and as_json(body)["title"] == "bleibt"
    titles = [n["title"] for n in as_json(call(server, "GET", "/notes")[1])]
    assert "fremd" not in titles


def test_security_server_refuses_to_start_without_a_token():
    for token in (None, ""):
        proc, port = start(token)
        try:
            up = wait_up(proc, port, seconds=6)
            if up:
                status = call(port, "GET", "/notes", token="")[0]
                pytest.fail(f"the server serves without NOTES_API_TOKEN (GET /notes -> {status})")
            assert proc.wait(10) != 0, "the server must exit with a non-zero code without NOTES_API_TOKEN"
        finally:
            stop(proc)


# ------------------------------------------------------------------------------------------------ behaviour


def test_create_get_list_update_delete(server):
    note = create(server, "Termin", "Zahnarzt 10 Uhr")
    assert set(note) >= {"id", "title", "text"} and note["title"] == "Termin" and note["text"] == "Zahnarzt 10 Uhr"
    status, body, headers = call(server, "GET", f"/notes/{note['id']}")
    assert status == 200 and as_json(body) == note
    assert "application/json" in headers.get("Content-Type", "")
    status, body, _ = call(server, "GET", "/notes")
    assert status == 200 and note in as_json(body)
    status, body, _ = call(server, "PUT", f"/notes/{note['id']}", {"title": "Termin", "text": "verschoben"})
    assert status == 200 and as_json(body)["text"] == "verschoben" and as_json(body)["id"] == note["id"]
    assert as_json(call(server, "GET", f"/notes/{note['id']}")[1])["text"] == "verschoben"
    assert call(server, "DELETE", f"/notes/{note['id']}")[0] == 204
    assert call(server, "GET", f"/notes/{note['id']}")[0] == 404
    assert note["id"] not in [n["id"] for n in as_json(call(server, "GET", "/notes")[1])]


def test_ids_are_distinct(server):
    a, b = create(server, "a", ""), create(server, "b", "")
    assert a["id"] != b["id"]


@pytest.mark.parametrize(
    "raw",
    [b"{kaputt", b"[]", b'{"text": "ohne Titel"}', b'{"title": "", "text": "x"}', b'{"title": 5, "text": "x"}',
     b'{"title": "x", "text": 5}', b'{"title": "x"}', ('{"title": "%s", "text": ""}' % ("t" * 201)).encode()],
)
def test_invalid_input_is_400(server, raw):
    status, body, _ = call(server, "POST", "/notes", raw=raw)
    assert status == 400
    assert isinstance(as_json(body).get("error"), str)


def test_invalid_update_is_400_and_changes_nothing(server):
    note = create(server, "fest", "x")
    assert call(server, "PUT", f"/notes/{note['id']}", raw=b'{"title": ""}')[0] == 400
    assert as_json(call(server, "GET", f"/notes/{note['id']}")[1])["title"] == "fest"


def test_title_of_200_characters_is_accepted(server):
    assert create(server, "t" * 200, "")["title"] == "t" * 200


@pytest.mark.parametrize(("method", "body"), [("GET", None), ("PUT", {"title": "x", "text": "y"}), ("DELETE", None)])
def test_unknown_note_is_404(server, method, body):
    for path in ("/notes/987654", "/notes/abc"):
        status, raw, _ = call(server, method, path, body)
        assert status == 404, (method, path, status)
        assert isinstance(as_json(raw).get("error"), str)


def test_documentation_names_the_endpoints():
    doc = (ROOT / "docs/api.md").read_text(encoding="utf-8")
    for needle in ("POST /notes", "GET /notes", "PUT /notes/", "DELETE /notes/", "401", "400", "404", "Bearer", "NOTES_API_TOKEN"):
        assert needle in doc, needle


def test_own_tests_pass():
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        capture_output=True, text=True, timeout=300, cwd=ROOT,
    )
    assert res.returncode == 0, res.stdout[-3000:]
    passed = int(res.stdout.rsplit(" passed", 1)[0].split()[-1]) if " passed" in res.stdout else 0
    assert passed >= 6, f"only {passed} own tests"
