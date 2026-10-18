# Live job fixtures

Starting points of the live job battery (`config/acceptance-jobs.example.yaml`). `scripts/ops/acceptance-fixtures.sh`
pushes each directory as the branch `acceptance/<name>` of the test repository. The fixtures are deliberately plain
(standard library, built-in test runners) and contain no hints for the runtime; `service` has a known bug for J6.

`form` (contact form with faulty validation, `node --test`) and `legacy` (inherited CSV ledger tool with several
bugs, `unittest`) are the starting points of K2 and M2 in the test cycle battery (`config/acceptance-jobs-cycle.example.yaml`).
