# Live job fixtures

Starting points of the live job battery (`config/acceptance-jobs.example.yaml`). `scripts/ops/acceptance-fixtures.sh`
pushes each directory as the branch `acceptance/<name>` of the test repository. The fixtures are deliberately plain
(standard library, built-in test runners) and contain no hints for the runtime; `service` has a known bug for J6.
