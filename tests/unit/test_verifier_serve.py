"""Which serve problems a single step answers for (BUG-361)."""

from __future__ import annotations

import json
from pathlib import Path

from hermclaw.verifier.serve import Server, parse_serve_output, serve_command, servers, start_function

G1_RC90 = {
    "serve": True,
    "listening": True,
    "port": 8080,
    "problems": [
        "/app.js calls POST /api/test, but the server answered 404: the route does not exist",
        "static/old.html is part of the project, but the running server does not serve it (tried /static/old.html)",
        "the page / loads /logo.png, but GET /logo.png answered 404",
    ],
    "sources": [["/app.js"], ["static/old.html"], ["/", "/logo.png"]],
    "probes": [],
}


def test_a_step_answers_for_the_problems_of_the_files_it_wrote() -> None:
    """Live G1 rc90: S006 wrote static/app.js, which called POST /api/test against the collector S005 had finished; the
    problem was a note on every step until the final review."""
    result = parse_serve_output("starting\n" + json.dumps(G1_RC90))
    assert result is not None
    frontend = result.caused_by("collector.py", ["static/app.js", "static/style.css"])
    assert frontend == [G1_RC90["problems"][0]]
    assert result.caused_by("collector.py", ["static/index.html"]) == [G1_RC90["problems"][2]], "'/' is an index.html"
    assert result.caused_by("collector.py", ["static/old.html"]) == [G1_RC90["problems"][1]]
    assert result.caused_by("collector.py", ["collector.py"]) == G1_RC90["problems"], "the server answers for all"
    assert result.caused_by("collector.py", ["scripts/launch.ps1", "web/app.js.map"]) == []
    # the output of an older driver has no sources: nothing is blamed on a single step
    old = parse_serve_output(json.dumps({k: v for k, v in G1_RC90.items() if k != "sources"}))
    assert old is not None and old.caused_by("collector.py", ["static/app.js"]) == []


SERVE = "from http.server import HTTPServer\n\n\ndef {sig}:\n    HTTPServer(('127.0.0.1', 0), None).serve_forever()\n"


def test_the_start_function_is_public_runs_a_server_and_needs_no_argument() -> None:
    assert start_function(SERVE.format(sig="serve(port=8080)")) == "serve"
    assert start_function(SERVE.format(sig="serve(port)")) is None, "a required argument the verifier cannot know"
    assert start_function(SERVE.format(sig="serve(*, config)")) is None
    assert start_function(SERVE.format(sig="_serve()")) is None
    both = SERVE.format(sig="helper()") + "\n\ndef main():\n    helper()\n    import uvicorn\n    uvicorn.run('app')\n"
    assert start_function(both) == "main", "main before any other name"
    assert start_function("def serve(:\n") is None


def test_a_script_with_main_block_wins_over_a_module_helper(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(SERVE.format(sig="serve()"))
    assert servers(tmp_path, ["pkg/api.py"]) == [Server("pkg/api.py", {}, "serve")]
    command, how = serve_command(servers(tmp_path, ["pkg/api.py"])[0], ["python3"], [])
    assert command == "python3 -c 'import pkg.api; pkg.api.serve()'" and "start function serve()" in how
    (tmp_path / "app.py").write_text(
        "from pkg.api import serve\nfrom http.server import HTTPServer\n\nif __name__ == '__main__':\n    serve()\n"
    )
    assert servers(tmp_path, ["app.py", "pkg/api.py"]) == [Server("app.py", {})]
    (tmp_path / "my-api.py").write_text(SERVE.format(sig="serve()"))
    assert servers(tmp_path, ["my-api.py"]) == [], "not importable as a module"
