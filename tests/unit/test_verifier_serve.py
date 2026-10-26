"""Which serve problems a single step answers for (BUG-361)."""

from __future__ import annotations

import json

from hermclaw.verifier.serve import parse_serve_output

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
