# Hidden checks of the coder tournament

One directory per task (A1–A9). The coder never sees these files: the tournament runner copies the delivered
workspace, adds the task's directory as `_hidden/` and runs the task's check command there (see
`tests/live/tournament/coder-tournament.yaml`). Each check tests the requirements of the task statement, not the
implementation the fixture had; `_base/` holds the fixture's original files for "unchanged" comparisons.

Python tasks use pytest; A2 uses `node --test` and A9 a PowerShell runner (`run.ps1`) that prints TAP like
`node --test`, so the grader counts both with the same parser. A check whose name contains `security` (A7, A8)
disqualifies the run when it fails. Every check was validated against the untouched fixture (fails) and a reference
solution (passes all).
