# Hidden checks of the coder tournament

One directory per task (A1–A10). The coder never sees these files: the tournament runner copies the delivered
workspace, adds the task's directory as `_hidden/` and runs the task's check command there (see
`tests/live/tournament/coder-tournament.yaml`). Each check tests the requirements of the task statement, not the
implementation the fixture had; `_base/` holds the fixture's original files for "unchanged" comparisons.

Python tasks use pytest; A2 uses `node --test` and A9 a PowerShell runner (`run.ps1`) that prints TAP like
`node --test`, so the grader counts both with the same parser. A check whose name contains `security` (A7, A8)
disqualifies the run when it fails. Every check was validated against the untouched fixture (fails) and a reference
solution (passes all).

`R1-<language>` are stage R1 of the language learning ladder (`tests/live/tournament/languages.yaml`, system runs):
the same roman numeral task in Rust, Go, C++, Java, C#, VB.NET, Lua and Bash, each checked with the language's own
test tool in its sandbox image (cargo test, go test, GoogleTest via CMake, the JUnit console launcher, xUnit via
`dotnet test`, busted, bats). Every R1 check was validated against a reference solution (passes all) and against the
same solution without its canonical-form check (fails exactly one check). The C# and VB.NET checks share one xUnit
project that references the delivered `src/Roemisch/Roemisch.csproj` or `.vbproj` and restores offline from the
packages of the dotnet image.
